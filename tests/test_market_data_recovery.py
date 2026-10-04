from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tracefang.application.market_data_recovery import (
    MarketDataRecoveryCoordinator,
    market_open_ranges,
)
from tracefang.application.realtime_bars import (
    BarBackfillResult,
    BarBackfillState,
    RealtimeBarSeriesState,
)
from tracefang.domain.market_events import BarState, RealtimeBar
from tracefang.domain.models import AssetClass, Instrument, SourceMetadata

INSTRUMENT = Instrument("XAU/USD", AssetClass.SPOT, "XAU", "USD", "OTC")
SOURCE_ID = "jin10_client"
INTERVAL = timedelta(minutes=1)
SPOT_SCHEDULE = {
    "time_zone": "America/New_York",
    "sessions": [
        {
            "weekday": weekday,
            "open": "18:00",
            "close": "17:00",
            "close_day_offset": 1,
        }
        for weekday in range(5)
    ],
}


def bar(open_time: datetime) -> RealtimeBar:
    return RealtimeBar(
        instrument=INSTRUMENT,
        interval=INTERVAL,
        open_time=open_time,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=None,
        source=SourceMetadata(
            provider=SOURCE_ID,
            provider_symbol="XAUUSD.GOODS",
            observed_at=open_time,
            received_at=open_time,
        ),
        evidence_channel_id="jin10_web",
        state=BarState.PROVISIONAL_QUOTE,
    )


class _Bars:
    def __init__(
        self,
        rows: tuple[RealtimeBar, ...] = (),
        state: RealtimeBarSeriesState | None = None,
    ) -> None:
        self.rows = rows
        self.state = state
        self.calls: list[tuple[datetime, int, bool]] = []

    async def get_bars_before(self, *_args, **_kwargs):
        return self.rows

    async def backfill(
        self,
        _instrument,
        *,
        source_id,
        start,
        count,
        revalidate=False,
    ):
        self.calls.append((start, count, revalidate))
        return BarBackfillResult(
            source_id=source_id,
            state=BarBackfillState.FETCHED,
            start=start,
            end=start + INTERVAL * count,
            row_count=count,
            covered_start=start,
            covered_end=start + INTERVAL * count,
            authoritative_through=start + INTERVAL * count,
            evidence_version="test",
        )

    def history_backfill_configured(self, _source_id):
        return True

    def series_state(self, *_args, **_kwargs):
        return self.state


class MarketOpenRangeTests(unittest.TestCase):
    def test_daily_maintenance_is_not_a_recovery_range(self) -> None:
        # 2026-08-27 is in New York daylight time: the session closes at 21:00Z
        # and reopens at 22:00Z.
        start = datetime(2026, 8, 27, 20, 59, tzinfo=UTC)
        end = datetime(2026, 8, 27, 22, 1, tzinfo=UTC)

        ranges = market_open_ranges(start, end, SPOT_SCHEDULE)

        self.assertEqual(
            ranges,
            (
                (start, datetime(2026, 8, 27, 21, 0, tzinfo=UTC)),
                (datetime(2026, 8, 27, 22, 0, tzinfo=UTC), end),
            ),
        )


class MarketDataRecoveryCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_range_uses_the_same_calendar_aware_path(self) -> None:
        now = datetime(2026, 8, 27, 22, 1, tzinfo=UTC)
        start = datetime(2026, 8, 27, 20, 59, tzinfo=UTC)
        bars = _Bars()
        recovery = MarketDataRecoveryCoordinator(bars, clock=lambda: now)
        recovery.register_series(
            INSTRUMENT,
            source_id=SOURCE_ID,
            schedule=SPOT_SCHEDULE,
        )

        result = await recovery.backfill(
            INSTRUMENT,
            source_id=SOURCE_ID,
            start=start,
            count=62,
        )

        self.assertEqual(
            bars.calls,
            [
                (start, 1, False),
                (datetime(2026, 8, 27, 22, 0, tzinfo=UTC), 1, False),
            ],
        )
        self.assertEqual(result.covered_start, start)
        self.assertEqual(result.covered_end, now)

    async def test_startup_audit_repairs_an_internal_minute_gap_once(self) -> None:
        now = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
        rows = (
            bar(datetime(2026, 8, 28, 1, 36, tzinfo=UTC)),
            bar(datetime(2026, 8, 28, 1, 54, tzinfo=UTC)),
        )
        bars = _Bars(rows)
        recovery = MarketDataRecoveryCoordinator(bars, clock=lambda: now)
        recovery.register_series(
            INSTRUMENT,
            source_id=SOURCE_ID,
            schedule=SPOT_SCHEDULE,
            seed_rows=rows,
        )

        await recovery.start()
        await recovery.wait_idle()

        self.assertEqual(
            bars.calls,
            [(datetime(2026, 8, 28, 1, 37, tzinfo=UTC), 17, False)],
        )
        self.assertEqual(recovery.metrics().recovered_rows, 17)
        await recovery.close()

    async def test_live_resume_uses_the_same_recovery_path(self) -> None:
        now = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
        first = bar(datetime(2026, 8, 28, 1, 36, tzinfo=UTC))
        bars = _Bars((first,))
        recovery = MarketDataRecoveryCoordinator(bars, clock=lambda: now)
        recovery.register_series(
            INSTRUMENT,
            source_id=SOURCE_ID,
            schedule=SPOT_SCHEDULE,
            seed_rows=(first,),
        )
        await recovery.start()
        recovery.observe(
            INSTRUMENT,
            source_id=SOURCE_ID,
            observed_at=datetime(2026, 8, 28, 1, 54, 39, tzinfo=UTC),
        )

        await recovery.wait_idle()

        self.assertEqual(
            bars.calls,
            [(datetime(2026, 8, 28, 1, 37, tzinfo=UTC), 17, False)],
        )
        await recovery.close()

    async def test_authority_tail_is_split_around_scheduled_closure(self) -> None:
        now = datetime(2026, 8, 27, 22, 1, tzinfo=UTC)
        authority = datetime(2026, 8, 27, 20, 59, tzinfo=UTC)
        state = RealtimeBarSeriesState(
            realtime_source_id=SOURCE_ID,
            instrument_symbol=INSTRUMENT.symbol,
            upstream_channel_id="jin10_local",
            provider_symbol="XAUUSD.GOODS",
            interval=INTERVAL,
            latest_authoritative_open_time=authority - INTERVAL,
            authoritative_through=authority,
            history_floor=None,
            tail_checked_through=None,
            tail_checked_at=None,
            evidence_version="test",
            updated_at=authority,
        )
        bars = _Bars(state=state)
        recovery = MarketDataRecoveryCoordinator(bars, clock=lambda: now)
        recovery.register_series(
            INSTRUMENT,
            source_id=SOURCE_ID,
            schedule=SPOT_SCHEDULE,
        )

        await recovery.start()
        await recovery.wait_idle()

        self.assertEqual(
            bars.calls,
            [
                (authority, 1, False),
                (datetime(2026, 8, 27, 22, 0, tzinfo=UTC), 1, False),
            ],
        )
        await recovery.close()


if __name__ == "__main__":
    unittest.main()
