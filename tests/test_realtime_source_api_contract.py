from __future__ import annotations

import asyncio
import inspect
import unittest
from types import SimpleNamespace

from market_analysis import api
from market_analysis.application.sources import (
    QuoteServiceTier,
    SourceAccessModel,
    SourceHealth,
    SourceRoutingRole,
)


class RealtimeSourceApiContractTests(unittest.TestCase):
    def test_market_endpoints_do_not_accept_client_selected_source(self) -> None:
        self.assertEqual(tuple(inspect.signature(api.quote).parameters), ("code",))
        self.assertEqual(tuple(inspect.signature(api.last_quote).parameters), ("code",))
        self.assertEqual(
            tuple(inspect.signature(api.candles).parameters),
            ("code", "count", "time"),
        )
        self.assertEqual(
            tuple(inspect.signature(api.chart_bars).parameters),
            ("code", "period", "before"),
        )
        self.assertEqual(
            tuple(inspect.signature(api.timeline_samples).parameters),
            ("code", "cursor"),
        )
        self.assertEqual(
            tuple(inspect.signature(api.backfill_candles).parameters),
            ("code", "count", "time"),
        )
        self.assertEqual(
            tuple(inspect.signature(api.quote_stream).parameters),
            ("websocket", "code"),
        )

    def test_public_source_payload_hides_physical_topology(self) -> None:
        descriptor = SimpleNamespace(
            source_id="realtime",
            display_name="Realtime",
            description="one complete result",
            capabilities=("candles", "quote"),
            enabled=True,
            frozen=False,
            delayed=False,
            requires_running_app=False,
            structured=True,
            quote_poll_interval_seconds=0,
            quote_streaming=True,
            quote_service_tier=QuoteServiceTier.ENHANCED,
            access_model=SourceAccessModel.UNMETERED,
            access_note=None,
            manual_connection_required=False,
            connection_active=True,
            quotas=(),
            health=SourceHealth.HEALTHY,
            state="ready",
            error=None,
            checked_at=None,
            last_success_at=None,
            routing_role=SourceRoutingRole.REALTIME_SOURCE,
            composition=("internal_a", "internal_b"),
        )

        payload = api._public_source(descriptor)

        self.assertTrue(payload["selectable"])
        self.assertNotIn("routing_role", payload)
        self.assertNotIn("composition", payload)
        self.assertNotIn("enabled", payload)

    def test_public_health_hides_physical_acquisition_channels(self) -> None:
        payload = api._public_acquisition_status(
            {
                "routes": {"XAU/USD": "jin10_client"},
                "active_channels": {"physical_a": ("XAU/USD",)},
                "poll_tasks": ("physical_b:XAU/USD",),
            }
        )

        self.assertEqual(
            payload,
            {
                "state": "running",
                "routes": {"XAU/USD": "jin10_client"},
            },
        )

    def test_instruments_expose_market_sessions_without_source_topology(self) -> None:
        payload = asyncio.run(api.instruments())

        self.assertEqual(
            {item["provider_code"] for item in payload},
            {
                "XAUUSD",
                "XAGUSD",
                "USDCNH",
                "XAUCNHG",
                "AU8888",
                "AG8888",
                "AU2610",
                "AG2706",
                "USDIND",
                "BRN0Y",
                "SHCOMP",
                "IXIC",
            },
        )
        spot = [
            item for item in payload if item["provider_code"] in {"XAUUSD", "XAGUSD", "XAUCNHG"}
        ]
        for item in spot:
            schedule = item["market_schedule"]
            self.assertEqual(schedule["time_zone"], "America/New_York")
            self.assertEqual(len(schedule["sessions"]), 5)
            self.assertEqual(schedule["sessions"][0]["open"], "18:05")
            self.assertEqual(schedule["sessions"][-1]["close"], "16:59")
        forex = next(item for item in payload if item["provider_code"] == "USDCNH")
        self.assertEqual(forex["market_schedule"]["time_zone"], "America/New_York")
        self.assertEqual(forex["market_schedule"]["sessions"][0]["open"], "17:05")
        shfe = [
            item
            for item in payload
            if item["provider_code"] in {"AU8888", "AG8888", "AU2610", "AG2706"}
        ]
        for item in shfe:
            self.assertEqual(item["source_ids"], ["tonghuashun_futures"])
            self.assertEqual(item["market_schedule"]["time_zone"], "Asia/Shanghai")
            self.assertEqual(item["market_schedule"]["trading_day_rule"], "shfe")
            self.assertEqual(len(item["market_schedule"]["sessions"]), 20)
            self.assertEqual(item["market_schedule"]["sessions"][0]["open"], "09:00")
            self.assertEqual(item["market_schedule"]["sessions"][-1]["close"], "02:30")
        schedules = {item["provider_code"]: item["market_schedule"] for item in payload}
        self.assertEqual(schedules["SHCOMP"]["time_zone"], "Asia/Shanghai")
        self.assertEqual(len(schedules["SHCOMP"]["sessions"]), 10)
        self.assertEqual(schedules["IXIC"]["time_zone"], "America/New_York")
        self.assertEqual(schedules["IXIC"]["sessions"][0]["open"], "09:30")
        self.assertEqual(schedules["USDIND"]["time_zone"], "UTC")
        self.assertEqual(schedules["USDIND"]["sessions"][0]["close_day_offset"], 1)
        self.assertEqual(schedules["BRN0Y"]["time_zone"], "Europe/London")
        self.assertEqual(schedules["BRN0Y"]["sessions"][0]["open"], "23:00")
        derived = next(item for item in payload if item["provider_code"] == "XAUCNHG")
        self.assertEqual(derived["quote_kind"], "derived")
        self.assertEqual(derived["dependencies"], ["XAUUSD", "USDCNH"])
        self.assertFalse(derived["history_available"])


if __name__ == "__main__":
    unittest.main()
