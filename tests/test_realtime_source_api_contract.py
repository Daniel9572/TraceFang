from __future__ import annotations

import asyncio
import inspect
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from tracefang import api
from tracefang.application.realtime_bars import RealtimeBarContract, RealtimeBarService
from tracefang.application.sources import (
    QuoteServiceTier,
    SourceAccessModel,
    SourceHealth,
    SourceRoutingRole,
)


class RealtimeSourceApiContractTests(unittest.TestCase):
    def test_chart_api_accepts_only_one_dataset_scoped_page_boundary(self) -> None:
        definition = api.instrument_definition("XAUUSD")
        schedule = api._MARKET_SCHEDULES[definition.market_schedule_id]
        before = datetime(2026, 8, 21, 19, 18, tzinfo=UTC)
        cursor = api.encode_chart_page_cursor(
            definition.instrument,
            source_id="jin10_client",
            period_id="1d",
            schedule=schedule,
            before=before,
        )

        self.assertEqual(
            api._chart_page_boundary(
                cursor,
                None,
                instrument=definition.instrument,
                source_id="jin10_client",
                period_id="1d",
                schedule=schedule,
            ),
            before,
        )
        with self.assertRaisesRegex(api.HTTPException, "either cursor or before"):
            api._chart_page_boundary(
                cursor,
                int(before.timestamp()),
                instrument=definition.instrument,
                source_id="jin10_client",
                period_id="1d",
                schedule=schedule,
            )

    def test_market_endpoints_do_not_accept_client_selected_source(self) -> None:
        self.assertEqual(tuple(inspect.signature(api.quote).parameters), ("code",))
        self.assertEqual(tuple(inspect.signature(api.last_quote).parameters), ("code",))
        self.assertEqual(
            tuple(inspect.signature(api.candles).parameters),
            ("code", "count", "time"),
        )
        self.assertEqual(
            tuple(inspect.signature(api.chart_bars).parameters),
            ("code", "period", "cursor", "before", "page_size"),
        )
        self.assertEqual(
            tuple(inspect.signature(api.ensure_chart_bar_history).parameters),
            ("code", "period", "cursor", "count_back"),
        )
        self.assertEqual(
            tuple(inspect.signature(api.timeline_samples).parameters),
            ("code", "cursor", "page_size"),
        )
        self.assertEqual(
            tuple(inspect.signature(api.backfill_candles).parameters),
            ("code", "count", "time", "revalidate"),
        )
        self.assertEqual(
            tuple(inspect.signature(api.quote_stream).parameters),
            ("websocket", "code"),
        )

    def test_market_history_transport_pages_have_hard_upper_bounds(self) -> None:
        schema = api.app.openapi()
        bars = {
            item["name"]: item["schema"]
            for item in schema["paths"]["/api/bars/{code}"]["get"]["parameters"]
        }
        chart_history = {
            item["name"]: item["schema"]
            for item in schema["paths"]["/api/bars/{code}/history"]["post"]["parameters"]
        }
        timeline = {
            item["name"]: item["schema"]
            for item in schema["paths"]["/api/timeline/{code}"]["get"]["parameters"]
        }

        self.assertEqual(bars["page_size"]["maximum"], 10_000)
        self.assertEqual(chart_history["count_back"]["maximum"], 10_000)
        self.assertEqual(timeline["page_size"]["maximum"], 20_000)

    def test_backfill_response_has_stable_states_and_aware_times(self) -> None:
        schema = api.app.openapi()
        response = schema["components"]["schemas"]["CandleBackfillResponse"]

        self.assertEqual(
            response["properties"]["state"]["enum"],
            ["cached", "joined", "fetched", "advanced", "exhausted", "deferred"],
        )
        for field in ("start", "end", "authoritative_through", "history_floor", "retry_after"):
            value = response["properties"][field]
            variants = value.get("anyOf", [value])
            self.assertTrue(
                any(item.get("format") == "date-time" for item in variants),
                field,
            )
        with self.assertRaisesRegex(ValueError, "timezone"):
            api.CandleBackfillResponse(
                source_id="realtime",
                state="cached",
                start=datetime(2026, 8, 1),
                end=datetime(2026, 8, 2),
                row_count=0,
            )

    def test_public_history_diagnostics_expose_metrics_without_channel_secrets(self) -> None:
        service = RealtimeBarService(
            None,
            contracts=(
                RealtimeBarContract(
                    source_id="realtime",
                    authoritative_bar_channel_id="private-history",
                    quote_channel_ids=("private-quote",),
                ),
            ),
        )

        payload = api._public_history_status(service, live_bar_count=3)

        self.assertEqual(payload["live_bar_count"], 3)
        self.assertEqual(
            set(payload["backfill_metrics"]),
            {
                "cache_hits",
                "upstream_calls",
                "joined_calls",
                "written_rows",
                "failures",
                "pending",
                "last_failure_at",
                "last_failure_type",
            },
        )
        serialized = repr(payload)
        self.assertNotIn("private-history", serialized)
        self.assertNotIn("private-quote", serialized)
        self.assertNotIn("session_token", serialized)
        self.assertNotIn("user_id", serialized)

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

        payload = api._public_source(
            descriptor,
            history_backfill_configured=False,
        )

        self.assertTrue(payload["selectable"])
        self.assertFalse(payload["history_backfill_configured"])
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
            self.assertEqual(schedule["sessions"][0]["open"], "18:00")
            self.assertEqual(schedule["sessions"][-1]["close"], "17:00")
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
        self.assertFalse(derived["history_backfill_supported"])


if __name__ == "__main__":
    unittest.main()
