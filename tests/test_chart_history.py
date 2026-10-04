from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from tracefang.application.chart_history import (
    ChartHistoryCoordinator,
    ChartHistorySourceStatus,
    chart_history_demand_minutes,
    decode_chart_page_cursor,
    encode_chart_page_cursor,
)
from tracefang.application.period_bars import PERIOD_DEFINITIONS, PeriodBarPage
from tracefang.application.realtime_bars import BarBackfillResult, BarBackfillState
from tracefang.domain.models import AssetClass, Instrument

INSTRUMENT = Instrument("XAU/USD", AssetClass.SPOT, "XAU", "USD", "OTC")
BEFORE = datetime(2026, 8, 21, 19, 18, tzinfo=UTC)


class _PeriodBars:
    def __init__(self, pages: list[PeriodBarPage]) -> None:
        self.pages = pages
        self.get_calls = 0
        self.materialize_calls = 0

    async def get_page(self, *_args, **_kwargs) -> PeriodBarPage:
        self.get_calls += 1
        return self.pages.pop(0)

    async def materialize_page(self, *_args, **_kwargs) -> PeriodBarPage:
        self.materialize_calls += 1
        return self.pages.pop(0)


class _RealtimeBars:
    def __init__(self, result: BarBackfillResult) -> None:
        self.result = result
        self.calls: list[tuple[datetime, int]] = []

    async def backfill(self, _instrument, *, source_id, start, count, revalidate=False):
        del source_id, revalidate
        self.calls.append((start, count))
        return self.result


def _result(state: BarBackfillState = BarBackfillState.FETCHED) -> BarBackfillResult:
    return BarBackfillResult(
        source_id="jin10_client",
        state=state,
        start=BEFORE - timedelta(minutes=10_000),
        end=BEFORE,
        row_count=10,
        covered_start=BEFORE - timedelta(minutes=10_000),
        covered_end=BEFORE,
        authoritative_through=BEFORE,
        evidence_version="v1",
    )


class ChartHistoryCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def test_chart_page_cursor_is_opaque_and_dataset_scoped(self) -> None:
        token = encode_chart_page_cursor(
            INSTRUMENT,
            source_id="jin10_client",
            period_id="1d",
            schedule={"time_zone": "UTC", "sessions": []},
            before=BEFORE,
        )

        self.assertNotIn(BEFORE.isoformat(), token)
        self.assertEqual(
            decode_chart_page_cursor(
                token,
                INSTRUMENT,
                source_id="jin10_client",
                period_id="1d",
                schedule={"time_zone": "UTC", "sessions": []},
            ),
            BEFORE,
        )
        with self.assertRaisesRegex(ValueError, "dataset"):
            decode_chart_page_cursor(
                token,
                INSTRUMENT,
                source_id="jin10_client",
                period_id="1w",
                schedule={"time_zone": "UTC", "sessions": []},
            )

    def test_chart_page_cursor_rejects_malformed_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "cursor"):
            decode_chart_page_cursor(
                "not-a-valid-cursor",
                INSTRUMENT,
                source_id="jin10_client",
                period_id="1d",
                schedule=None,
            )

    async def test_local_page_satisfies_demand_without_upstream_command(self) -> None:
        local = PeriodBarPage("1h", (object(),), BEFORE - timedelta(hours=1), True)  # type: ignore[arg-type]
        periods = _PeriodBars([local])
        realtime = _RealtimeBars(_result())
        coordinator = ChartHistoryCoordinator(periods, realtime)  # type: ignore[arg-type]

        result = await coordinator.ensure_older(
            INSTRUMENT,
            source_id="jin10_client",
            period_id="1h",
            schedule=None,
            before=BEFORE,
            count_back=240,
            backfill_supported=True,
        )

        self.assertEqual(result.page, local)
        self.assertIsNone(result.backfill)
        self.assertEqual(realtime.calls, [])
        self.assertEqual(periods.materialize_calls, 0)

    async def test_every_period_uses_the_same_local_first_state_machine(self) -> None:
        for period_id in PERIOD_DEFINITIONS:
            with self.subTest(period_id=period_id):
                local = PeriodBarPage(
                    period_id,
                    (object(),),  # type: ignore[arg-type]
                    BEFORE - timedelta(minutes=1),
                    True,
                )
                periods = _PeriodBars([local])
                realtime = _RealtimeBars(_result())

                result = await ChartHistoryCoordinator(  # type: ignore[arg-type]
                    periods,
                    realtime,
                ).ensure_older(
                    INSTRUMENT,
                    source_id="jin10_client",
                    period_id=period_id,
                    schedule=None,
                    before=BEFORE,
                    count_back=240,
                    backfill_supported=True,
                )

                self.assertEqual(result.page, local)
                self.assertEqual(result.source_status, ChartHistorySourceStatus.AVAILABLE)
                self.assertEqual(realtime.calls, [])

    async def test_every_backfillable_period_plans_one_bounded_transport_step(self) -> None:
        for period_id in PERIOD_DEFINITIONS:
            if period_id in {"timeline", "1s"}:
                continue
            with self.subTest(period_id=period_id):
                empty = PeriodBarPage(period_id, (), None, False)
                filled = PeriodBarPage(
                    period_id,
                    (object(),),  # type: ignore[arg-type]
                    BEFORE - timedelta(minutes=1),
                    False,
                )
                periods = _PeriodBars([empty, filled])
                realtime = _RealtimeBars(_result())

                await ChartHistoryCoordinator(  # type: ignore[arg-type]
                    periods,
                    realtime,
                ).ensure_older(
                    INSTRUMENT,
                    source_id="jin10_client",
                    period_id=period_id,
                    schedule=None,
                    before=BEFORE,
                    count_back=240,
                    backfill_supported=True,
                )

                self.assertEqual(len(realtime.calls), 1)
                start, count = realtime.calls[0]
                self.assertEqual(count, chart_history_demand_minutes(period_id, 240))
                self.assertLessEqual(count, 10_000)
                self.assertEqual(start, BEFORE - timedelta(minutes=count))

    async def test_large_logical_demand_runs_one_bounded_server_transport_page(self) -> None:
        empty = PeriodBarPage("1w", (), None, False)
        filled = PeriodBarPage("1w", (object(),), BEFORE - timedelta(weeks=1), False)  # type: ignore[arg-type]
        periods = _PeriodBars([empty, filled])
        realtime = _RealtimeBars(_result())
        coordinator = ChartHistoryCoordinator(periods, realtime)  # type: ignore[arg-type]

        result = await coordinator.ensure_older(
            INSTRUMENT,
            source_id="jin10_client",
            period_id="1w",
            schedule=None,
            before=BEFORE,
            count_back=240,
            backfill_supported=True,
        )

        self.assertEqual(realtime.calls, [(BEFORE - timedelta(minutes=10_000), 10_000)])
        self.assertEqual(periods.materialize_calls, 1)
        self.assertEqual(result.page, filled)
        self.assertEqual(result.source_status, ChartHistorySourceStatus.AVAILABLE)

    async def test_empty_covered_window_returns_server_confirmed_resume_boundary(self) -> None:
        empty = PeriodBarPage("1d", (), None, False)
        periods = _PeriodBars([empty, empty])
        realtime = _RealtimeBars(_result(BarBackfillState.ADVANCED))
        coordinator = ChartHistoryCoordinator(periods, realtime)  # type: ignore[arg-type]

        result = await coordinator.ensure_older(
            INSTRUMENT,
            source_id="jin10_client",
            period_id="1d",
            schedule=None,
            before=BEFORE,
            count_back=240,
            backfill_supported=True,
        )

        self.assertEqual(result.next_before, BEFORE - timedelta(minutes=10_000))
        self.assertEqual(result.source_status, ChartHistorySourceStatus.AVAILABLE)

    async def test_one_second_history_is_explicitly_unsupported(self) -> None:
        for period_id in ("timeline", "1s"):
            with self.subTest(period_id=period_id):
                empty = PeriodBarPage(period_id, (), None, False)
                periods = _PeriodBars([empty])
                realtime = _RealtimeBars(_result())
                coordinator = ChartHistoryCoordinator(periods, realtime)  # type: ignore[arg-type]

                result = await coordinator.ensure_older(
                    INSTRUMENT,
                    source_id="jin10_client",
                    period_id=period_id,
                    schedule=None,
                    before=BEFORE,
                    count_back=240,
                    backfill_supported=True,
                )

                self.assertEqual(result.source_status, ChartHistorySourceStatus.UNSUPPORTED)
                self.assertEqual(realtime.calls, [])


if __name__ == "__main__":
    unittest.main()
