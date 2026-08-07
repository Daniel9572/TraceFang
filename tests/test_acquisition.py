from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from market_analysis.application.acquisition import QuoteAcquisitionRouter
from market_analysis.domain.models import QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.providers.jin10 import SPOT_GOLD, SPOT_SILVER


def quote(source: str, instrument=SPOT_GOLD) -> QuoteSnapshot:
    now = datetime.now(UTC)
    return QuoteSnapshot(
        instrument=instrument,
        last=Decimal("4250"),
        open=None,
        high=None,
        low=None,
        volume=None,
        change=None,
        change_percent=None,
        source=SourceMetadata(
            provider=source,
            provider_symbol=instrument.symbol,
            observed_at=now,
            received_at=now,
        ),
    )


class FakePushChannel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.subscriptions: tuple = ()

    async def set_subscriptions(self, instruments) -> None:
        self.subscriptions = tuple(instruments)

    async def get_quote(self, instrument):
        return quote(self.name, instrument)


class FakePollChannel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def get_quote(self, instrument):
        self.calls += 1
        return quote(self.name, instrument)


class QuoteAcquisitionRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.web = FakePushChannel("jin10_web")
        self.local = FakePushChannel("jin10_local")
        self.backup = FakePushChannel("backup_channel")
        self.polled = FakePollChannel("polled_channel")
        self.enabled = {
            "jin10_client": True,
            "backup_source": True,
            "polled_source": True,
        }
        self.received = []
        self.errors = []
        self.router = QuoteAcquisitionRouter(
            push_channels={
                "jin10_web": self.web,
                "jin10_local": self.local,
                "backup_channel": self.backup,
            },
            poll_channels={"polled_channel": self.polled},
            source_channels={
                "jin10_client": ("jin10_web", "jin10_local"),
                "backup_source": ("backup_channel",),
                "polled_source": ("polled_channel",),
            },
            source_enabled=lambda source: self.enabled[source],
            prepare_source=lambda _source: asyncio.sleep(0),
            poll_interval=lambda _source: 60,
            on_quote=self.received.append,
            on_error=lambda *args: self.errors.append(args),
        )

    async def asyncTearDown(self) -> None:
        await self.router.stop()

    async def test_each_contract_activates_only_its_declared_channels(self) -> None:
        await self.router.start(
            {
                SPOT_GOLD: "jin10_client",
                SPOT_SILVER: "backup_source",
            }
        )

        self.assertEqual(self.web.subscriptions, (SPOT_GOLD,))
        self.assertEqual(self.local.subscriptions, (SPOT_GOLD,))
        self.assertEqual(self.backup.subscriptions, (SPOT_SILVER,))
        self.assertEqual(self.polled.calls, 0)

    async def test_route_change_removes_obsolete_channel_subscription(self) -> None:
        await self.router.start({SPOT_GOLD: "jin10_client"})
        await self.router.set_route(SPOT_GOLD, "backup_source")

        self.assertEqual(self.web.subscriptions, ())
        self.assertEqual(self.local.subscriptions, ())
        self.assertEqual(self.backup.subscriptions, (SPOT_GOLD,))
        self.assertEqual(
            self.router.status()["active_channels"],
            {"backup_channel": ("XAU/USD",)},
        )

    async def test_polling_runs_without_any_ui_subscriber(self) -> None:
        await self.router.start({SPOT_GOLD: "polled_source"})
        await asyncio.sleep(0.01)

        self.assertEqual(self.polled.calls, 1)
        self.assertEqual(self.received[0].source.provider, "polled_channel")

    async def test_source_test_temporarily_subscribes_then_restores_route(self) -> None:
        await self.router.start({SPOT_GOLD: "backup_source"})

        values = await self.router.sample_source("jin10_client", SPOT_SILVER)

        self.assertEqual(set(values), {"jin10_web", "jin10_local"})
        self.assertEqual(self.web.subscriptions, ())
        self.assertEqual(self.local.subscriptions, ())
        self.assertEqual(self.backup.subscriptions, (SPOT_GOLD,))


if __name__ == "__main__":
    unittest.main()
