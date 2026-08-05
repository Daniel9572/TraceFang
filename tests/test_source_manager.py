import unittest
from datetime import UTC, datetime
from decimal import Decimal

from market_analysis.application.sources import (
    MarketSourceManager,
    SourceCapability,
    SourceRegistration,
)
from market_analysis.domain.errors import ProviderUnavailableError
from market_analysis.domain.models import QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.providers.jin10 import SPOT_GOLD


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

    async def get_quote(self, instrument):
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


def registration(source_id, priority, provider):
    return SourceRegistration(
        source_id=source_id,
        display_name=source_id,
        description=source_id,
        capabilities=frozenset({SourceCapability.QUOTE}),
        default_enabled=True,
        default_priority=priority,
        delayed=False,
        requires_running_app=False,
        quote_provider=provider,
    )


class MarketSourceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_falls_back_by_priority(self) -> None:
        manager = MarketSourceManager(
            (
                registration("primary", 10, FakeProvider("primary")),
                registration("fallback", 20, FakeProvider("fallback", "4242.65")),
            ),
            store=MemoryStore(),
        )
        quote = await manager.get_quote(SPOT_GOLD)
        self.assertEqual(quote.source.provider, "fallback")

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
        quote = await manager.get_quote(SPOT_GOLD)
        self.assertEqual(quote.source.provider, "desktop")
        self.assertEqual(store.values["desktop"]["priority"], 5)

    async def test_compare_preserves_individual_failures(self) -> None:
        manager = MarketSourceManager(
            (
                registration("official", 10, FakeProvider("official", "4242")),
                registration("desktop", 20, FakeProvider("desktop")),
            ),
            store=MemoryStore(),
        )
        results = await manager.compare_quotes(SPOT_GOLD)
        self.assertIsNotNone(results[0].quote)
        self.assertIsNotNone(results[1].error)


if __name__ == "__main__":
    unittest.main()
