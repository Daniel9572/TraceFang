from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_analysis.application.period_bars import PeriodBarService, project_period_bars
from market_analysis.domain.market_events import BarState, RealtimeBar
from market_analysis.domain.models import AssetClass, Instrument, SourceMetadata

INSTRUMENT = Instrument("AU8888", AssetClass.INDEX, "AU", "CNY", "SHFE")
SHFE_SCHEDULE = {
    "time_zone": "Asia/Shanghai",
    "trading_day_rule": "shfe",
    "sessions": [
        {"weekday": weekday, "open": open_time, "close": close_time, "close_day_offset": offset}
        for weekday in range(1, 6)
        for open_time, close_time, offset in (
            ("09:00", "10:15", 0),
            ("10:30", "11:30", 0),
            ("13:30", "15:00", 0),
            ("21:00", "02:30", 1),
        )
    ],
}


def bar(at: str, value: str, *, state: BarState = BarState.FINAL, revision: int = 1) -> RealtimeBar:
    open_time = datetime.fromisoformat(at).astimezone(UTC)
    price = Decimal(value)
    received_at = open_time + timedelta(minutes=1)
    return RealtimeBar(
        instrument=INSTRUMENT,
        interval=timedelta(minutes=1),
        open_time=open_time,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1"),
        source=SourceMetadata(
            provider="tonghuashun_futures",
            provider_symbol="qh_au8888",
            observed_at=open_time,
            received_at=received_at,
        ),
        evidence_channel_id="tonghuashun_futures",
        state=state,
        revision=revision,
        finalized_at=received_at if state is BarState.FINAL else None,
    )


class PeriodBarProjectionTests(unittest.TestCase):
    def test_fixed_period_restarts_at_each_trading_session(self) -> None:
        rows = (
            bar("2026-08-10T09:01:00+08:00", "100"),
            bar("2026-08-10T10:14:00+08:00", "101"),
            bar("2026-08-10T10:30:00+08:00", "102"),
        )

        values = project_period_bars(
            rows,
            period_id="1h",
            schedule=SHFE_SCHEDULE,
            now=datetime(2026, 8, 11, tzinfo=UTC),
        )

        self.assertEqual(len(values), 3)
        self.assertEqual(
            [item.open_time for item in values],
            [
                datetime.fromisoformat("2026-08-10T09:00:00+08:00").astimezone(UTC),
                datetime.fromisoformat("2026-08-10T10:00:00+08:00").astimezone(UTC),
                datetime.fromisoformat("2026-08-10T10:30:00+08:00").astimezone(UTC),
            ],
        )

    def test_night_and_day_sessions_share_the_exchange_trading_day(self) -> None:
        values = project_period_bars(
            (
                bar("2026-08-10T21:00:00+08:00", "100"),
                bar("2026-08-11T09:00:00+08:00", "103"),
                bar("2026-08-11T14:59:00+08:00", "101"),
            ),
            period_id="1d",
            schedule=SHFE_SCHEDULE,
            now=datetime(2026, 8, 12, tzinfo=UTC),
        )

        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].open_time, datetime(2026, 8, 10, 16, tzinfo=UTC))
        self.assertEqual(values[0].open, Decimal("100"))
        self.assertEqual(values[0].high, Decimal("103"))
        self.assertEqual(values[0].close, Decimal("101"))
        self.assertEqual(values[0].state, BarState.FINAL)

    def test_daily_bar_stays_open_until_the_last_session_closes(self) -> None:
        [value] = project_period_bars(
            (bar("2026-08-10T21:00:00+08:00", "100"),),
            period_id="1d",
            schedule=SHFE_SCHEDULE,
            now=datetime.fromisoformat("2026-08-11T10:00:00+08:00").astimezone(UTC),
        )

        self.assertEqual(value.state, BarState.PROVISIONAL_AUTHORITATIVE)
        self.assertEqual(
            value.source.raw_payload["bucket_end"],
            datetime.fromisoformat("2026-08-11T15:00:00+08:00").astimezone(UTC).isoformat(),
        )

    def test_period_revision_and_provisional_state_derive_from_all_components(self) -> None:
        values = project_period_bars(
            (
                bar("2026-08-10T09:00:00+08:00", "100", revision=2),
                bar(
                    "2026-08-10T09:01:00+08:00",
                    "102",
                    state=BarState.PROVISIONAL_AUTHORITATIVE,
                    revision=3,
                ),
            ),
            period_id="5m",
            schedule=SHFE_SCHEDULE,
            now=datetime(2026, 8, 11, tzinfo=UTC),
        )

        self.assertEqual(values[0].revision, 5)
        self.assertEqual(values[0].state, BarState.PROVISIONAL_AUTHORITATIVE)
        self.assertIsNone(values[0].finalized_at)


class _PagedMinuteReader:
    def __init__(self, rows: tuple[RealtimeBar, ...]) -> None:
        self.rows = rows
        self.calls = 0

    async def get_bars_before(self, _instrument, *, source_id, before=None, count=10_000):
        del source_id, count
        self.calls += 1
        candidates = tuple(row for row in self.rows if before is None or row.open_time < before)
        return candidates[-200:]


class _StrictPagedMinuteReader:
    def __init__(self, rows: tuple[RealtimeBar, ...]) -> None:
        self.rows = rows
        self.requested_counts: list[int] = []

    async def get_bars_before(self, _instrument, *, source_id, before=None, count=10_000):
        del source_id
        if count > 10_000:
            raise ValueError("cursor page count must be between 1 and 10000")
        self.requested_counts.append(count)
        candidates = tuple(row for row in self.rows if before is None or row.open_time < before)
        return candidates[-count:]


class PeriodBarPagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_transport_pages_continue_until_chart_page_is_complete(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(510)
        )
        reader = _PagedMinuteReader(rows)
        service = PeriodBarService(reader)  # type: ignore[arg-type]

        page = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="1m",
            schedule=None,
        )

        self.assertEqual(len(page.items), 500)
        self.assertTrue(page.has_more)
        self.assertEqual(reader.calls, 3)
        self.assertEqual(page.items[0].open_time, start + timedelta(minutes=10))

    async def test_transport_page_size_is_configurable_without_limiting_total_history(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(510)
        )
        reader = _PagedMinuteReader(rows)
        service = PeriodBarService(reader)  # type: ignore[arg-type]

        page = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="1m",
            schedule=None,
            page_size=300,
        )

        self.assertEqual(len(page.items), 300)
        self.assertTrue(page.has_more)
        self.assertEqual(reader.calls, 2)

    async def test_large_period_reads_multiple_internal_pages_without_a_history_cap(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(15_030)
        )
        reader = _StrictPagedMinuteReader(rows)
        service = PeriodBarService(reader)  # type: ignore[arg-type]

        page = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="30m",
            schedule=None,
            page_size=500,
        )

        self.assertEqual(len(page.items), 500)
        self.assertTrue(page.has_more)
        self.assertGreater(len(reader.requested_counts), 1)
        self.assertLessEqual(max(reader.requested_counts), 10_000)


if __name__ == "__main__":
    unittest.main()
