from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from tracefang import api
from tracefang.application.multi_timeframe import (
    MultiTimeframeTrendService,
    TimeframeComparisonState,
    TimeframeHorizon,
    TimeframeSummaryState,
    TrendDirection,
)
from tracefang.application.period_bars import PeriodBarPage
from tracefang.application.quotes import JIN10_CLIENT_SOURCE
from tracefang.domain.market_events import BarState, RealtimeBar
from tracefang.domain.models import SourceMetadata
from tracefang.instruments import SPOT_GOLD

INTERVALS = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1w": timedelta(days=7),
}
DECISION_AS_OF = datetime(2026, 8, 1, tzinfo=UTC)


def bar(
    period_id: str,
    open_time: datetime,
    close: Decimal,
    *,
    state: BarState = BarState.FINAL,
    received_at: datetime | None = None,
) -> RealtimeBar:
    interval = INTERVALS[period_id]
    bucket_end = open_time + interval
    received = received_at or bucket_end + timedelta(minutes=1)
    return RealtimeBar(
        instrument=SPOT_GOLD,
        interval=interval,
        open_time=open_time,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal(1),
        source=SourceMetadata(
            provider=JIN10_CLIENT_SOURCE,
            provider_symbol="XAUUSD.GOODS",
            observed_at=bucket_end - timedelta(seconds=1),
            received_at=received,
            raw_payload={"bucket_end": bucket_end.isoformat(), "period_id": period_id},
        ),
        evidence_channel_id="jin10_local",
        state=state,
        finalized_at=received if state is BarState.FINAL else None,
    )


def series(
    period_id: str,
    start: datetime,
    closes: list[int],
) -> tuple[RealtimeBar, ...]:
    interval = INTERVALS[period_id]
    return tuple(
        bar(period_id, start + interval * index, Decimal(close))
        for index, close in enumerate(closes)
    )


def complete_rows() -> dict[str, tuple[RealtimeBar, ...]]:
    short = series("1h", datetime(2026, 7, 1, tzinfo=UTC), list(range(100, 120)))
    short = (
        *short,
        bar(
            "1h",
            datetime(2026, 7, 2, tzinfo=UTC),
            Decimal(120),
            state=BarState.PROVISIONAL_AUTHORITATIVE,
        ),
        bar(
            "1h",
            datetime(2026, 7, 3, tzinfo=UTC),
            Decimal(121),
            received_at=DECISION_AS_OF + timedelta(hours=1),
        ),
    )
    return {
        "1h": short,
        "1d": series("1d", datetime(2026, 6, 1, tzinfo=UTC), list(range(200, 180, -1))),
        "1w": series("1w", datetime(2026, 1, 1, tzinfo=UTC), [150] * 20),
    }


class _PeriodBars:
    def __init__(self, values: dict[str, tuple[RealtimeBar, ...]]) -> None:
        self.values = values
        self.calls: list[dict[str, object]] = []

    async def get_page(self, instrument, **kwargs) -> PeriodBarPage:
        self.calls.append({"instrument": instrument, **kwargs})
        period_id = str(kwargs["period_id"])
        before = kwargs["before"]
        page_size = int(kwargs["page_size"])
        candidates = tuple(
            item
            for item in self.values.get(period_id, ())
            if before is None or item.open_time < before
        )
        items = candidates[-page_size:]
        return PeriodBarPage(
            period_id=period_id,
            items=items,
            next_before=items[0].open_time if items else None,
            has_more=len(candidates) > page_size,
        )


class MultiTimeframeTrendTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_one_causal_cutoff_and_reports_cross_horizon_divergence(self) -> None:
        period_bars = _PeriodBars(complete_rows())
        service = MultiTimeframeTrendService(period_bars)  # type: ignore[arg-type]

        context = await service.snapshot(
            SPOT_GOLD,
            source_id=JIN10_CLIENT_SOURCE,
            schedule=None,
            decision_as_of=DECISION_AS_OF,
        )

        self.assertEqual(context.state, "ready")
        self.assertEqual(context.decision_as_of, DECISION_AS_OF)
        by_horizon = {item.horizon: item for item in context.timeframes}
        short = by_horizon[TimeframeHorizon.SHORT]
        self.assertEqual(short.period_id, "1h")
        self.assertEqual(short.state, TimeframeSummaryState.READY)
        self.assertEqual(short.direction, TrendDirection.UP)
        self.assertEqual(short.eligible_final_bar_count, 20)
        self.assertEqual(short.excluded_non_final_bars, 1)
        self.assertEqual(short.excluded_after_as_of_bars, 1)
        self.assertEqual(by_horizon[TimeframeHorizon.MEDIUM].direction, TrendDirection.DOWN)
        self.assertEqual(by_horizon[TimeframeHorizon.LONG].direction, TrendDirection.MIXED)
        self.assertTrue(context.comparison.comparable)
        self.assertEqual(context.comparison.state, TimeframeComparisonState.DIVERGENT)
        self.assertTrue(
            any(
                item.left is TimeframeHorizon.SHORT and item.right is TimeframeHorizon.MEDIUM
                for item in context.comparison.differences
            )
        )
        self.assertEqual(
            {call["period_id"] for call in period_bars.calls},
            {"1h", "1d", "1w"},
        )
        self.assertTrue(all(call["before"] == DECISION_AS_OF for call in period_bars.calls))
        self.assertTrue(all(call["source_id"] == JIN10_CLIENT_SOURCE for call in period_bars.calls))

    async def test_marks_comparison_not_comparable_when_one_horizon_is_short(self) -> None:
        rows = complete_rows()
        rows["1w"] = rows["1w"][-7:]
        service = MultiTimeframeTrendService(_PeriodBars(rows))  # type: ignore[arg-type]

        context = await service.snapshot(
            SPOT_GOLD,
            source_id=JIN10_CLIENT_SOURCE,
            schedule=None,
            decision_as_of=DECISION_AS_OF,
        )

        long_term = next(
            item for item in context.timeframes if item.horizon is TimeframeHorizon.LONG
        )
        self.assertEqual(context.state, "partial")
        self.assertEqual(long_term.state, TimeframeSummaryState.INSUFFICIENT_DATA)
        self.assertEqual(long_term.direction, TrendDirection.UNAVAILABLE)
        self.assertEqual(long_term.used_bar_count, 7)
        self.assertIsNone(long_term.sma_slow)
        self.assertIsNone(long_term.window_return_percent)
        self.assertEqual(context.comparison.state, TimeframeComparisonState.NOT_COMPARABLE)
        self.assertFalse(context.comparison.comparable)
        self.assertEqual(
            context.comparison.incomparable_reasons,
            ("long:requires_20_final_bars_has_7",),
        )

    async def test_endpoint_returns_explicit_mapping_and_json_numbers(self) -> None:
        period_bars = _PeriodBars(complete_rows())
        with (
            patch.object(
                api,
                "_instrument_source",
                AsyncMock(return_value=("XAUUSD", SPOT_GOLD, JIN10_CLIENT_SOURCE)),
            ),
            patch.object(api, "_period_bars", return_value=period_bars),
        ):
            payload = await api.expert_multi_timeframe_trend(
                "xauusd",
                as_of=DECISION_AS_OF,
            )

        self.assertEqual(payload["contract_version"], "multi-timeframe-trend-v1")
        self.assertEqual(payload["profile_id"], "swing-1h-1d-1w-v1")
        self.assertEqual(payload["decision_as_of"], "2026-08-01T00:00:00+00:00")
        self.assertEqual(
            {key: item["period_id"] for key, item in payload["period_mapping"].items()},
            {"short": "1h", "medium": "1d", "long": "1w"},
        )
        self.assertIsInstance(payload["timeframes"][0]["last_close"], float)
        self.assertEqual(payload["comparison"]["state"], "divergent")
        self.assertTrue(payload["limitations"])
        methods_by_path = {
            route.path: route.methods for route in api.app.routes if hasattr(route, "methods")
        }
        self.assertEqual(
            methods_by_path["/api/expert/context/multi-timeframe/{code}"],
            {"GET"},
        )


if __name__ == "__main__":
    unittest.main()
