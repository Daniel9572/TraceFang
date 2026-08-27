import unittest
from datetime import UTC, datetime
from decimal import Decimal

from tracefang.application.sources import (
    MarketSourceManager,
    ProviderProbe,
    QuoteServiceTier,
    RealtimeSourceComposition,
    SourceAccessModel,
    SourceCapability,
    SourceRegistration,
    SourceRoutingRole,
)
from tracefang.domain.errors import ProviderUnavailableError
from tracefang.domain.models import QuoteSnapshot, SourceMetadata
from tracefang.infrastructure.providers.jin10 import SPOT_GOLD


class MemoryStore:
    def __init__(self):
        self.values = {}

    def load(self):
        return {key: value.copy() for key, value in self.values.items()}

    def save(self, values):
        self.values = {key: value.copy() for key, value in values.items()}


class FakeProvider:
    def __init__(self, name, price=None):
        self.name = name
        self.price = price
        self.calls = 0

    async def get_quote(self, instrument):
        self.calls += 1
        if self.price is None:
            raise ProviderUnavailableError(f"{self.name} offline")
        now = datetime.now(UTC)
        return QuoteSnapshot(
            instrument=instrument,
            last=Decimal(self.price),
            open=None,
            high=None,
            low=None,
            volume=None,
            change=None,
            change_percent=None,
            source=SourceMetadata(
                provider=self.name,
                provider_symbol="XAUUSD",
                observed_at=now,
                received_at=now,
            ),
        )

    async def get_candles(self, instrument, *, start=None, count=100):
        self.calls += 1
        if self.price is None:
            raise ProviderUnavailableError(f"{self.name} offline")
        return ()


class FakeConnector:
    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return ProviderProbe(
            available=True,
            state="ready",
            detail="connected by user",
            checked_at=datetime.now(UTC),
        )


def registration(source_id, priority, provider):
    return SourceRegistration(
        source_id=source_id,
        display_name=source_id,
        description=source_id,
        capabilities=frozenset({SourceCapability.QUOTE, SourceCapability.CANDLES}),
        default_enabled=True,
        default_priority=priority,
        delayed=False,
        requires_running_app=False,
        quote_provider=provider,
        candle_provider=provider,
    )


class MarketSourceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_realtime_source_is_excluded_from_runtime(self) -> None:
        store = MemoryStore()
        store.values = {"jin10_mcp": {"enabled": True, "priority": 1}}
        connector = FakeConnector()
        provider = FakeProvider("jin10_mcp", "4242")
        manager = MarketSourceManager(
            (
                SourceRegistration(
                    source_id="jin10_mcp",
                    display_name="frozen MCP",
                    description="frozen",
                    capabilities=frozenset({SourceCapability.QUOTE, SourceCapability.CANDLES}),
                    default_enabled=True,
                    default_priority=1,
                    delayed=False,
                    requires_running_app=False,
                    access_model=SourceAccessModel.LIMITED,
                    manual_connection_required=True,
                    connector=connector,
                    quote_provider=provider,
                    candle_provider=provider,
                    frozen=True,
                    frozen_reason="temporarily frozen",
                ),
            ),
            store=store,
        )

        descriptor = (await manager.list_sources())[0]
        self.assertTrue(descriptor.frozen)
        self.assertFalse(descriptor.enabled)
        self.assertEqual(descriptor.health, "frozen")
        self.assertFalse(store.values["jin10_mcp"]["enabled"])
        with self.assertRaisesRegex(ProviderUnavailableError, "temporarily frozen"):
            manager.configure("jin10_mcp", enabled=True)
        with self.assertRaisesRegex(ProviderUnavailableError, "temporarily frozen"):
            await manager.connect_source("jin10_mcp")
        with self.assertRaisesRegex(ProviderUnavailableError, "temporarily frozen"):
            await manager.get_quote(SPOT_GOLD, source="jin10_mcp")
        self.assertEqual(connector.calls, 0)
        self.assertEqual(provider.calls, 0)

    async def test_exposes_quote_service_tier_as_source_metadata(self) -> None:
        manager = MarketSourceManager(
            (
                SourceRegistration(
                    source_id="fast",
                    display_name="fast",
                    description="change-driven stream",
                    capabilities=frozenset({SourceCapability.QUOTE, SourceCapability.CANDLES}),
                    default_enabled=True,
                    default_priority=10,
                    delayed=False,
                    requires_running_app=False,
                    quote_streaming=True,
                    quote_service_tier=QuoteServiceTier.ENHANCED,
                    quote_provider=FakeProvider("fast", "4242"),
                    candle_provider=FakeProvider("fast-kline", "4242"),
                ),
            ),
            store=MemoryStore(),
        )

        sources = await manager.list_sources()

        self.assertEqual(sources[0].quote_service_tier, QuoteServiceTier.ENHANCED)

    async def test_internal_channels_are_not_exposed_as_realtime_sources(self) -> None:
        store = MemoryStore()
        store.values = {"jin10_web": {"enabled": False, "priority": 1}}
        manager = MarketSourceManager(
            (
                SourceRegistration(
                    source_id="jin10_client",
                    display_name="client",
                    description="explicit composition",
                    capabilities=frozenset({SourceCapability.QUOTE, SourceCapability.CANDLES}),
                    default_enabled=True,
                    default_priority=5,
                    delayed=False,
                    requires_running_app=False,
                    quote_streaming=True,
                    routing_role=SourceRoutingRole.REALTIME_SOURCE,
                    composition=RealtimeSourceComposition(
                        quote_channel_ids=("jin10_web",),
                        kline_channel_id="jin10_web",
                        kline_derived_from_quotes=True,
                    ),
                ),
                SourceRegistration(
                    source_id="jin10_web",
                    display_name="web",
                    description="raw channel",
                    capabilities=frozenset({SourceCapability.QUOTE}),
                    default_enabled=True,
                    default_priority=10,
                    delayed=False,
                    requires_running_app=False,
                    quote_streaming=True,
                    routing_role=SourceRoutingRole.INTERNAL_CHANNEL,
                    quote_provider=FakeProvider("jin10_web", "4242"),
                ),
            ),
            store=store,
        )

        sources = await manager.list_sources()
        all_sources = await manager.list_sources(include_internal=True)

        self.assertEqual([item.source_id for item in sources], ["jin10_client"])
        self.assertEqual(
            {item.source_id for item in all_sources},
            {"jin10_client", "jin10_web"},
        )
        self.assertEqual(manager.realtime_source_ids(), ("jin10_client",))
        self.assertNotIn("jin10_web", store.values)
        internal = next(item for item in all_sources if item.source_id == "jin10_web")
        self.assertTrue(internal.enabled)
        with self.assertRaisesRegex(ProviderUnavailableError, "internal channel"):
            manager.validate_realtime_source("jin10_web")
        with self.assertRaisesRegex(ValueError, "internal channel"):
            manager.configure("jin10_web", enabled=False)

    async def test_explicit_candle_source_failure_does_not_call_free_source(self) -> None:
        free = FakeProvider("free", "4242")
        metered = FakeProvider("metered")
        manager = MarketSourceManager(
            (
                SourceRegistration(
                    source_id="metered",
                    display_name="metered",
                    description="metered",
                    capabilities=frozenset({SourceCapability.CANDLES}),
                    default_enabled=True,
                    default_priority=5,
                    delayed=False,
                    requires_running_app=False,
                    access_model=SourceAccessModel.LIMITED,
                    candle_provider=metered,
                    routing_role=SourceRoutingRole.INTERNAL_CHANNEL,
                ),
                SourceRegistration(
                    source_id="free",
                    display_name="free",
                    description="free",
                    capabilities=frozenset({SourceCapability.CANDLES}),
                    default_enabled=True,
                    default_priority=20,
                    delayed=False,
                    requires_running_app=False,
                    candle_provider=free,
                    routing_role=SourceRoutingRole.INTERNAL_CHANNEL,
                ),
            ),
            store=MemoryStore(),
        )

        with self.assertRaisesRegex(ProviderUnavailableError, "metered offline"):
            await manager.get_candles(SPOT_GOLD, source="metered")
        self.assertEqual(metered.calls, 1)
        self.assertEqual(free.calls, 0)

    async def test_explicit_source_failure_does_not_call_another_provider(self) -> None:
        primary = FakeProvider("primary")
        fallback = FakeProvider("fallback", "4242.65")
        manager = MarketSourceManager(
            (
                registration("primary", 10, primary),
                registration("fallback", 20, fallback),
            ),
            store=MemoryStore(),
        )
        with self.assertRaisesRegex(ProviderUnavailableError, "primary offline"):
            await manager.get_quote(SPOT_GOLD, source="primary")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    async def test_priority_change_is_persisted_and_applied(self) -> None:
        store = MemoryStore()
        manager = MarketSourceManager(
            (
                registration("official", 10, FakeProvider("official", "4242")),
                registration("desktop", 20, FakeProvider("desktop", "4243")),
            ),
            store=store,
        )
        manager.configure("desktop", priority=5)
        quote = await manager.get_quote(SPOT_GOLD, source="desktop")
        self.assertEqual(quote.source.provider, "desktop")
        self.assertEqual(store.values["desktop"]["priority"], 5)

    async def test_auto_source_is_rejected(self) -> None:
        manager = MarketSourceManager(
            (
                registration("official", 20, FakeProvider("official", "4242")),
                registration("desktop", 10, FakeProvider("desktop", "4243")),
            ),
            store=MemoryStore(),
        )
        with self.assertRaisesRegex(ValueError, "automatic source fallback is disabled"):
            await manager.get_quote(SPOT_GOLD, source="auto")

    async def test_limited_source_requires_explicit_connection(self) -> None:
        provider = FakeProvider("official", "4242")
        connector = FakeConnector()
        manager = MarketSourceManager(
            (
                SourceRegistration(
                    source_id="official",
                    display_name="official",
                    description="limited official source",
                    capabilities=frozenset({SourceCapability.QUOTE, SourceCapability.CANDLES}),
                    default_enabled=True,
                    default_priority=20,
                    delayed=False,
                    requires_running_app=False,
                    access_model=SourceAccessModel.LIMITED,
                    manual_connection_required=True,
                    connector=connector,
                    quote_provider=provider,
                    candle_provider=provider,
                ),
            ),
            store=MemoryStore(),
        )

        sources = await manager.list_sources(refresh=True)
        self.assertFalse(sources[0].connection_active)
        self.assertEqual(sources[0].state, "manual_connection_required")
        self.assertEqual(connector.calls, 0)
        with self.assertRaisesRegex(ProviderUnavailableError, "尚未连接"):
            await manager.get_quote(SPOT_GOLD, source="official")
        self.assertEqual(provider.calls, 0)

        await manager.connect_source("official")
        quote = await manager.get_quote(SPOT_GOLD, source="official")
        self.assertEqual(connector.calls, 1)
        self.assertEqual(quote.source.provider, "official")
        self.assertTrue(manager.is_connected("official"))

    async def test_limited_source_can_be_ready_without_spending_a_tool_call(self) -> None:
        provider = FakeProvider("official", "4242")
        manager = MarketSourceManager(
            (
                SourceRegistration(
                    source_id="official",
                    display_name="official",
                    description="limited official source",
                    capabilities=frozenset({SourceCapability.QUOTE, SourceCapability.CANDLES}),
                    default_enabled=True,
                    default_priority=20,
                    delayed=False,
                    requires_running_app=False,
                    access_model=SourceAccessModel.LIMITED,
                    quote_provider=provider,
                    candle_provider=provider,
                ),
            ),
            store=MemoryStore(),
        )

        sources = await manager.list_sources(refresh=True)
        self.assertTrue(sources[0].connection_active)
        self.assertEqual(provider.calls, 0)

    async def test_rejects_a_user_selectable_source_without_quote_and_kline(self) -> None:
        with self.assertRaisesRegex(ValueError, "incomplete; missing candles"):
            MarketSourceManager(
                (
                    SourceRegistration(
                        source_id="quote-only",
                        display_name="quote-only",
                        description="incomplete product",
                        capabilities=frozenset({SourceCapability.QUOTE}),
                        default_enabled=True,
                        default_priority=5,
                        delayed=False,
                        requires_running_app=False,
                        quote_provider=FakeProvider("quote-only", "4242"),
                    ),
                ),
                store=MemoryStore(),
            )


if __name__ == "__main__":
    unittest.main()
