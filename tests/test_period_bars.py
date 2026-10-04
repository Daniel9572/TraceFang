from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from tracefang.application.period_bars import (
    PeriodBarBucketAggregate,
    PeriodBarInputChange,
    PeriodBarMaterializationState,
    PeriodBarService,
    project_period_bars,
)
from tracefang.domain.market_events import BarState, RealtimeBar
from tracefang.domain.models import AssetClass, Instrument, SourceMetadata

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
    def test_one_second_is_a_normal_fixed_resolution(self) -> None:
        first = replace(
            bar("2026-08-10T09:00:00+08:00", "100"),
            interval=timedelta(seconds=1),
        )
        second = replace(
            bar("2026-08-10T09:00:01+08:00", "101"),
            interval=timedelta(seconds=1),
        )

        values = project_period_bars(
            (first, second),
            period_id="1s",
            schedule=SHFE_SCHEDULE,
            now=datetime(2026, 8, 11, tzinfo=UTC),
        )

        self.assertEqual([item.open_time for item in values], [first.open_time, second.open_time])
        self.assertTrue(all(item.interval == timedelta(seconds=1) for item in values))

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


class LivePeriodBarProjectionTests(unittest.TestCase):
    def test_active_component_correction_recomputes_complete_period_bar(self) -> None:
        service = PeriodBarService(object())  # type: ignore[arg-type]
        service.accept_live(
            bar("2026-08-10T09:00:00+08:00", "100"),
            schedule=SHFE_SCHEDULE,
        )
        latest = dict(
            service.accept_live(
                bar("2026-08-10T09:01:00+08:00", "105"),
                schedule=SHFE_SCHEDULE,
            )
        )["5m"]
        corrected = dict(
            service.accept_live(
                bar("2026-08-10T09:01:00+08:00", "102", revision=2),
                schedule=SHFE_SCHEDULE,
            )
        )["5m"]

        self.assertEqual(latest.high, Decimal("105"))
        self.assertEqual(corrected.open, Decimal("100"))
        self.assertEqual(corrected.high, Decimal("102"))
        self.assertEqual(corrected.close, Decimal("102"))

    def test_late_component_cannot_rewind_an_advanced_period_bucket(self) -> None:
        service = PeriodBarService(object())  # type: ignore[arg-type]
        service.accept_live(
            bar("2026-08-10T09:05:00+08:00", "105"),
            schedule=SHFE_SCHEDULE,
        )

        values = dict(
            service.accept_live(
                bar("2026-08-10T09:04:00+08:00", "99"),
                schedule=SHFE_SCHEDULE,
            )
        )

        self.assertNotIn("5m", values)


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


class _TrackedMinuteReader:
    def __init__(self, rows: tuple[RealtimeBar, ...]) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[datetime | None, int]] = []

    async def get_bars_before(self, _instrument, *, source_id, before=None, count=10_000):
        del source_id
        self.calls.append((before, count))
        candidates = tuple(row for row in self.rows if before is None or row.open_time < before)
        return candidates[-count:]

    def replace(self, value: RealtimeBar) -> None:
        self.rows = [row for row in self.rows if row.open_time != value.open_time]
        self.rows.append(value)
        self.rows.sort(key=lambda row: row.open_time)


class _IntervalReader:
    def __init__(self, rows: tuple[RealtimeBar, ...]) -> None:
        self.rows = rows
        self.intervals: list[timedelta] = []

    async def get_bars_before(
        self,
        _instrument,
        *,
        source_id,
        interval,
        before=None,
        count=10_000,
    ):
        del source_id
        self.intervals.append(interval)
        candidates = tuple(row for row in self.rows if before is None or row.open_time < before)
        return candidates[-count:]


class _MaterializedPeriodStore:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str, str, str], PeriodBarMaterializationState] = {}
        self.values: dict[tuple[str, str, str, str, datetime], RealtimeBar] = {}
        self.mutation_id = 0
        self.changes: list[PeriodBarInputChange] = []

    @staticmethod
    def _key(instrument, source_id, period_id, materialization_version):
        return instrument.symbol, source_id, period_id, materialization_version

    @staticmethod
    def _first_component_open_time(value: RealtimeBar) -> datetime:
        raw_value = value.source.raw_payload.get("bucket_first_open_time")
        return datetime.fromisoformat(str(raw_value)) if raw_value else value.open_time

    async def load_period_bar_materialization(
        self, instrument, *, source_id, period_id, materialization_version
    ):
        return self.states.get(self._key(instrument, source_id, period_id, materialization_version))

    async def save_period_bar_materialization(
        self,
        instrument,
        *,
        source_id,
        period_id,
        materialization_version,
        state,
    ):
        self.states[self._key(instrument, source_id, period_id, materialization_version)] = state

    async def load_materialized_period_bars_before(
        self,
        instrument,
        *,
        source_id,
        period_id,
        materialization_version,
        before,
        count,
    ):
        prefix = self._key(instrument, source_id, period_id, materialization_version)
        candidates = sorted(
            (
                value
                for key, value in self.values.items()
                if key[:4] == prefix
                and (before is None or self._first_component_open_time(value) < before)
            ),
            key=lambda value: value.open_time,
        )
        return tuple(candidates[-count:])

    async def save_materialized_period_bars(
        self,
        bars,
        *,
        source_id,
        period_id,
        materialization_version,
    ):
        for value in bars:
            key = (
                value.instrument.symbol,
                source_id,
                period_id,
                materialization_version,
                value.open_time,
            )
            self.values[key] = value

    async def delete_materialized_period_bar(
        self,
        instrument,
        *,
        source_id,
        period_id,
        materialization_version,
        open_time,
    ):
        self.values.pop(
            (*self._key(instrument, source_id, period_id, materialization_version), open_time),
            None,
        )

    async def latest_realtime_bar_mutation_id(self, instrument, *, source_id):
        del instrument, source_id
        return self.mutation_id

    async def load_realtime_bar_input_changes(
        self,
        instrument,
        *,
        source_id,
        after_mutation_id,
        through_mutation_id,
        count,
    ):
        del instrument, source_id
        candidates = tuple(
            value
            for value in self.changes
            if after_mutation_id < value.mutation_id <= through_mutation_id
        )
        return candidates[:count]

    def record_change(self, open_time: datetime) -> None:
        self.mutation_id += 1
        self.changes.append(PeriodBarInputChange(self.mutation_id, open_time))


class _AggregateMaterializedPeriodStore(_MaterializedPeriodStore):
    def __init__(self, reader: _TrackedMinuteReader) -> None:
        super().__init__()
        self.reader = reader
        self.aggregate_calls: list[tuple[datetime, datetime]] = []

    async def aggregate_realtime_bar_bucket(
        self,
        instrument,
        *,
        source_id,
        start,
        end,
    ):
        del instrument, source_id
        self.aggregate_calls.append((start, end))
        rows = tuple(row for row in self.reader.rows if start <= row.open_time < end)
        if not rows:
            return None
        first = rows[0]
        latest = rows[-1]
        volumes = [row.volume for row in rows if row.volume is not None]
        return PeriodBarBucketAggregate(
            first_open_time=first.open_time,
            open=first.open,
            high=max(row.high for row in rows),
            low=min(row.low for row in rows),
            close=latest.close,
            volume=sum(volumes, Decimal(0)) if volumes else None,
            all_final=all(row.state is BarState.FINAL for row in rows),
            any_authoritative=any(row.state is not BarState.PROVISIONAL_QUOTE for row in rows),
            finalized_at=max(row.finalized_at for row in rows if row.finalized_at is not None),
            revision=sum(row.revision for row in rows),
            component_count=len(rows),
            provider_symbol=latest.source.provider_symbol,
            evidence_channel_id=latest.evidence_channel_id,
            observed_at=max(row.source.observed_at for row in rows),
            received_at=max(row.source.received_at for row in rows),
        )


class PeriodBarPagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_second_page_reads_the_canonical_one_second_series(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        rows = tuple(
            replace(
                bar((start + timedelta(seconds=index)).isoformat(), str(100 + index)),
                interval=timedelta(seconds=1),
            )
            for index in range(4)
        )
        reader = _IntervalReader(rows)
        service = PeriodBarService(reader)  # type: ignore[arg-type]

        page = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="1s",
            schedule=None,
            page_size=3,
        )

        self.assertEqual(page.items, rows[-3:])
        self.assertTrue(page.has_more)
        self.assertEqual(reader.intervals, [timedelta(seconds=1)])

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

    async def test_derived_period_get_is_read_only(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(1_440)
        )
        reader = _TrackedMinuteReader(rows)
        store = _MaterializedPeriodStore()
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]

        first = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=3,
        )
        cold_read_calls = len(reader.calls)
        second = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=3,
        )

        self.assertEqual(first, second)
        self.assertGreater(cold_read_calls, 0)
        self.assertGreater(len(reader.calls), cold_read_calls)
        self.assertFalse(store.states)
        self.assertFalse(store.values)

    async def test_explicit_period_page_materialization_is_persistent_and_reused(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(1_440)
        )
        reader = _TrackedMinuteReader(rows)
        store = _MaterializedPeriodStore()
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]

        first = await service.materialize_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=3,
        )
        cold_read_calls = len(reader.calls)
        second = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=3,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first.items), 3)
        self.assertTrue(first.has_more)
        self.assertGreater(cold_read_calls, 0)
        self.assertEqual(len(reader.calls), cold_read_calls)
        self.assertTrue(store.states)
        self.assertTrue(store.values)

    async def test_materialized_partial_oldest_bucket_uses_component_cursor_exclusively(
        self,
    ) -> None:
        rows = (
            bar("2026-08-11T09:00:00+08:00", "100"),
            bar("2026-08-11T14:59:00+08:00", "101"),
        )
        reader = _TrackedMinuteReader(rows)
        store = _MaterializedPeriodStore()
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]

        latest = await service.materialize_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="1d",
            schedule=SHFE_SCHEDULE,
            page_size=1,
        )
        older = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="1d",
            schedule=SHFE_SCHEDULE,
            before=latest.next_before,
            page_size=1,
        )

        self.assertEqual(len(latest.items), 1)
        self.assertGreater(latest.next_before, latest.items[0].open_time)  # type: ignore[operator]
        self.assertEqual(older.items, ())
        self.assertIsNone(older.next_before)
        self.assertFalse(older.has_more)

    async def test_read_falls_back_when_materialized_mutation_cursor_is_stale(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(480)
        )
        reader = _TrackedMinuteReader(rows)
        store = _MaterializedPeriodStore()
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]
        await service.materialize_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=2,
        )
        calls_after_materialization = len(reader.calls)
        revised = bar((start + timedelta(minutes=479)).isoformat(), "99999", revision=2)
        reader.replace(revised)
        store.record_change(revised.open_time)

        page = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=2,
        )

        self.assertGreater(len(reader.calls), calls_after_materialization)
        self.assertEqual(page.items[-1].high, Decimal("99999"))

    async def test_materialized_overlay_uses_store_aggregate_instead_of_scanning_bucket(
        self,
    ) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(480)
        )
        reader = _TrackedMinuteReader(rows)
        store = _AggregateMaterializedPeriodStore(reader)
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]
        await service.materialize_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=2,
        )
        calls_after_materialization = len(reader.calls)
        revised = bar((start + timedelta(minutes=479)).isoformat(), "99999", revision=2)
        reader.replace(revised)
        store.record_change(revised.open_time)

        page = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=2,
        )

        self.assertEqual(len(reader.calls), calls_after_materialization)
        self.assertEqual(len(store.aggregate_calls), 1)
        self.assertEqual(page.items[-1].high, Decimal("99999"))

    async def test_older_canonical_input_reopens_an_exhausted_materialization(self) -> None:
        start = datetime(2025, 1, 2, tzinfo=UTC)
        initial = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(1_440)
        )
        reader = _TrackedMinuteReader(initial)
        store = _MaterializedPeriodStore()
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]
        await service.materialize_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=20,
        )
        state = next(iter(store.states.values()))
        self.assertTrue(state.history_exhausted)

        older = tuple(
            bar(
                (start - timedelta(days=1) + timedelta(minutes=index)).isoformat(),
                str(50 + index),
            )
            for index in range(1_440)
        )
        reader.rows = sorted([*reader.rows, *older], key=lambda row: row.open_time)
        store.record_change(older[-1].open_time)
        page = await service.materialize_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            before=start,
            page_size=3,
        )

        self.assertEqual(len(page.items), 3)
        self.assertTrue(all(item.open_time < start for item in page.items))

    async def test_derived_period_get_reflects_a_minute_revision_without_writing(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(1_200)
        )
        reader = _TrackedMinuteReader(rows)
        store = _MaterializedPeriodStore()
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]
        await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=2,
        )
        calls_before_revision = len(reader.calls)
        revised = bar(
            (start + timedelta(minutes=1_199)).isoformat(),
            "99999",
            revision=2,
        )
        reader.replace(revised)

        page = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=2,
        )

        revision_calls = reader.calls[calls_before_revision:]
        self.assertGreater(len(revision_calls), 0)
        self.assertEqual(page.items[-1].high, Decimal("99999"))
        self.assertFalse(store.states)
        self.assertFalse(store.values)

    async def test_provisional_tail_read_does_not_create_materialized_state(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar(
                (start + timedelta(minutes=index)).isoformat(),
                str(100 + index),
                state=BarState.PROVISIONAL_AUTHORITATIVE,
            )
            for index in range(240)
        )
        reader = _TrackedMinuteReader(rows)
        store = _MaterializedPeriodStore()
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]

        first = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=1,
        )
        cold_read_calls = len(reader.calls)
        second = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="4h",
            schedule=None,
            page_size=1,
        )

        self.assertEqual(first.items[0].state, BarState.PROVISIONAL_AUTHORITATIVE)
        self.assertEqual(first, second)
        self.assertGreater(len(reader.calls), cold_read_calls)
        self.assertFalse(store.states)
        self.assertFalse(store.values)

    async def test_daily_read_pages_match_one_shot_session_projection(self) -> None:
        rows = tuple(
            bar(at, value)
            for at, value in (
                ("2026-08-10T21:00:00+08:00", "100"),
                ("2026-08-11T09:00:00+08:00", "101"),
                ("2026-08-11T14:59:00+08:00", "102"),
                ("2026-08-11T21:00:00+08:00", "103"),
                ("2026-08-12T09:00:00+08:00", "104"),
                ("2026-08-12T14:59:00+08:00", "105"),
                ("2026-08-12T21:00:00+08:00", "106"),
                ("2026-08-13T09:00:00+08:00", "107"),
                ("2026-08-13T14:59:00+08:00", "108"),
            )
        )
        expected = project_period_bars(
            rows,
            period_id="1d",
            schedule=SHFE_SCHEDULE,
        )
        reader = _TrackedMinuteReader(rows)
        store = _MaterializedPeriodStore()
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]

        latest = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="1d",
            schedule=SHFE_SCHEDULE,
            page_size=2,
        )
        older = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="1d",
            schedule=SHFE_SCHEDULE,
            before=latest.next_before,
            page_size=2,
        )

        self.assertEqual((*older.items, *latest.items), expected)
        self.assertFalse(older.has_more)

    async def test_read_only_history_continues_beyond_twenty_thousand_minutes(
        self,
    ) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(20_130)
        )
        reader = _TrackedMinuteReader(rows)
        store = _MaterializedPeriodStore()
        service = PeriodBarService(reader, store=store)  # type: ignore[arg-type]

        first = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="30m",
            schedule=None,
            page_size=671,
        )
        cold_read_calls = len(reader.calls)
        second = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="30m",
            schedule=None,
            page_size=671,
        )

        self.assertEqual(len(first.items), 671)
        self.assertFalse(first.has_more)
        self.assertEqual(first, second)
        self.assertGreater(cold_read_calls, 2)
        self.assertLessEqual(max(count for _, count in reader.calls), 10_000)
        self.assertGreater(len(reader.calls), cold_read_calls)
        self.assertFalse(store.states)
        self.assertFalse(store.values)

    async def test_large_read_projects_accumulated_minutes_only_once(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(20_130)
        )
        reader = _TrackedMinuteReader(rows)
        service = PeriodBarService(reader)  # type: ignore[arg-type]

        with patch(
            "tracefang.application.period_bars.project_period_bars",
            wraps=project_period_bars,
        ) as projector:
            page = await service.get_page(
                INSTRUMENT,
                source_id="tonghuashun_futures",
                period_id="30m",
                schedule=None,
                page_size=671,
            )

        self.assertEqual(len(page.items), 671)
        self.assertEqual(projector.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class CompletePeriodReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_calendar_reads_include_the_previous_night_and_exclude_next_period(self):
        spot_schedule = {
            "time_zone": "America/New_York",
            "trading_day_rule": "session_end",
            "sessions": [
                {"weekday": day, "open": "18:00", "close": "17:00", "close_day_offset": 1}
                for day in range(5)
            ],
        }
        for period, times in (
            (
                "1d",
                (
                    "2026-09-28T18:00:00-04:00",
                    "2026-09-29T16:59:00-04:00",
                    "2026-09-29T18:00:00-04:00",
                ),
            ),
            (
                "1w",
                (
                    "2026-09-27T18:00:00-04:00",
                    "2026-10-02T16:59:00-04:00",
                    "2026-10-04T18:00:00-04:00",
                ),
            ),
            (
                "1mo",
                (
                    "2026-08-31T18:00:00-04:00",
                    "2026-09-30T16:59:00-04:00",
                    "2026-09-30T18:00:00-04:00",
                ),
            ),
        ):
            with self.subTest(period=period):
                rows = tuple(
                    bar(at, price) for at, price in zip(times, ("100", "105", "200"), strict=True)
                )
                reader = _TrackedMinuteReader(rows)
                store = _AggregateMaterializedPeriodStore(reader)
                service = PeriodBarService(reader, store=store)
                expected = project_period_bars(rows, period_id=period, schedule=spot_schedule)
                latest = await service.get_page(
                    INSTRUMENT,
                    source_id="tonghuashun_futures",
                    period_id=period,
                    schedule=spot_schedule,
                    page_size=1,
                )
                older = await service.get_page(
                    INSTRUMENT,
                    source_id="tonghuashun_futures",
                    period_id=period,
                    schedule=spot_schedule,
                    before=latest.next_before,
                    page_size=1,
                )
                self.assertEqual((*older.items, *latest.items), expected)
                self.assertEqual(
                    (older.items[0].open, older.items[0].high), (Decimal("100"), Decimal("105"))
                )
                self.assertTrue(all(count == 1 for _, count in reader.calls))
                self.assertFalse(store.states)
                self.assertFalse(store.values)

    async def test_live_subscription_restores_missing_open_and_hot_corrections(self):
        rows = tuple(
            bar(f"2026-08-10T09:0{i}:00+08:00", str(price))
            for i, price in enumerate((100, 95, 105, 102, 103))
        )
        reader = _TrackedMinuteReader(rows)
        store = _AggregateMaterializedPeriodStore(reader)
        service = PeriodBarService(reader, store=store)
        service.seed_live(rows[-2:], schedule=SHFE_SCHEDULE)
        await service.prepare_live_period(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="5m",
            schedule=SHFE_SCHEDULE,
        )
        corrected = replace(rows[-1], close=Decimal("104"), high=Decimal("104"), revision=2)
        live = dict(service.accept_live(corrected, schedule=SHFE_SCHEDULE, period_ids=("5m",)))[
            "5m"
        ]
        expected = project_period_bars(
            (*rows[:-1], corrected), period_id="5m", schedule=SHFE_SCHEDULE
        )[0]
        self.assertEqual(live, expected)
        correction = replace(
            rows[2],
            high=Decimal("101"),
            open=Decimal("101"),
            low=Decimal("101"),
            close=Decimal("101"),
            revision=2,
        )
        live = dict(service.accept_live(correction, schedule=SHFE_SCHEDULE, period_ids=("5m",)))[
            "5m"
        ]
        self.assertEqual(live.high, Decimal("104"))

    async def test_long_live_period_uses_a_bounded_complete_prefix(self):
        rows = (
            bar("2026-08-10T21:00:00+08:00", "90"),
            bar("2026-08-11T09:00:00+08:00", "110"),
            bar("2026-08-11T09:01:00+08:00", "100"),
        )
        reader = _TrackedMinuteReader(rows)
        store = _AggregateMaterializedPeriodStore(reader)
        service = PeriodBarService(reader, store=store)
        # Simulate a bounded minute cache with only the newest minute available.
        original_read = reader.get_bars_before

        async def bounded_read(*args, **kwargs):
            return (await original_read(*args, **kwargs))[-1:]

        reader.get_bars_before = bounded_read
        await service.prepare_live_period(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="1mo",
            schedule=SHFE_SCHEDULE,
        )
        updated = replace(rows[-1], close=Decimal("101"), high=Decimal("101"), revision=2)
        live = dict(service.accept_live(updated, schedule=SHFE_SCHEDULE, period_ids=("1mo",)))[
            "1mo"
        ]
        expected = project_period_bars(
            (*rows[:-1], updated),
            period_id="1mo",
            schedule=SHFE_SCHEDULE,
            now=updated.source.received_at,
        )[0]
        self.assertEqual(live, expected)


class BatchedPeriodReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_batched_pages_preserve_sessions_gaps_and_exclusive_cursor(self):
        class BatchStore(_AggregateMaterializedPeriodStore):
            batch_calls = 0

            async def aggregate_realtime_bar_buckets(self, instrument, *, source_id, bounds):
                self.batch_calls += 1
                return tuple(
                    [
                        await self.aggregate_realtime_bar_bucket(
                            instrument,
                            source_id=source_id,
                            start=start,
                            end=end,
                        )
                        for start, end in bounds
                    ]
                )

        rows = tuple(
            bar(at, str(100 + index))
            for index, at in enumerate(
                (
                    "2026-07-31T14:59:00+08:00",
                    "2026-07-31T21:00:00+08:00",
                    "2026-08-03T09:00:00+08:00",
                    "2026-08-03T10:14:00+08:00",
                    "2026-08-03T10:30:00+08:00",
                    "2026-08-03T14:59:00+08:00",
                    "2026-08-03T21:00:00+08:00",
                    "2026-08-04T09:00:00+08:00",
                    "2026-09-29T21:00:00+08:00",
                    "2026-09-30T09:00:00+08:00",
                )
            )
        )
        for schedule in (None, SHFE_SCHEDULE):
            for period in ("5m", "4h", "1d", "1w", "1mo", "1q", "1y"):
                with self.subTest(schedule=bool(schedule), period=period):
                    reader = _TrackedMinuteReader(rows)
                    store = BatchStore(reader)
                    service = PeriodBarService(reader, store=store)
                    expected = project_period_bars(rows, period_id=period, schedule=schedule)
                    received = []
                    before = None
                    while True:
                        page = await service.get_page(
                            INSTRUMENT,
                            source_id="tonghuashun_futures",
                            period_id=period,
                            schedule=schedule,
                            before=before,
                            page_size=2,
                        )
                        received[0:0] = page.items
                        if not page.has_more:
                            break
                        before = page.next_before
                    self.assertEqual(tuple(received), expected)
                    self.assertGreater(store.batch_calls, 0)
                    self.assertFalse(store.states)
                    self.assertFalse(store.values)


class LiveSnapshotConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_snapshot_overlays_unpersisted_minute_replacements(self):
        rows = tuple(
            bar(f"2026-08-11T09:0{index}:00+08:00", str(100 + index)) for index in range(5)
        )
        reader = _TrackedMinuteReader(rows)
        store = _AggregateMaterializedPeriodStore(reader)
        service = PeriodBarService(reader, store=store)
        service.seed_live(rows[-2:], schedule=SHFE_SCHEDULE)
        hot = replace(
            rows[-1],
            close=Decimal("107"),
            high=Decimal("107"),
            revision=2,
            state=BarState.PROVISIONAL_AUTHORITATIVE,
            finalized_at=None,
        )
        service.accept_live(hot, schedule=SHFE_SCHEDULE, period_ids=())
        page = await service.get_page(
            INSTRUMENT,
            source_id="tonghuashun_futures",
            period_id="5m",
            schedule=SHFE_SCHEDULE,
            page_size=1,
        )
        live = dict(service.accept_live(hot, schedule=SHFE_SCHEDULE, period_ids=("5m",)))["5m"]
        expected = project_period_bars((*rows[:-1], hot), period_id="5m", schedule=SHFE_SCHEDULE)[0]
        self.assertEqual(page.items, (expected,))
        self.assertEqual(live, expected)
        self.assertFalse(store.states)
        self.assertFalse(store.values)


class BatchedMaterializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_command_supports_older_pages_and_read_only_revision_overlay(self):
        class BatchStore(_AggregateMaterializedPeriodStore):
            async def aggregate_realtime_bar_buckets(self, instrument, *, source_id, bounds):
                return tuple(
                    [
                        await self.aggregate_realtime_bar_bucket(
                            instrument,
                            source_id=source_id,
                            start=start,
                            end=end,
                        )
                        for start, end in bounds
                    ]
                )

        start = datetime.fromisoformat("2026-08-11T09:00:00+08:00")
        rows = tuple(
            bar((start + timedelta(minutes=index)).isoformat(), str(100 + index))
            for index in range(13)
        )
        reader = _TrackedMinuteReader(rows)
        store = BatchStore(reader)
        service = PeriodBarService(reader, store=store)
        args = dict(source_id="tonghuashun_futures", period_id="5m", schedule=SHFE_SCHEDULE)
        latest = await service.materialize_page(INSTRUMENT, **args, page_size=2)
        self.assertTrue(latest.has_more)
        self.assertEqual(len(store.values), 2)
        corrected = replace(rows[-1], close=Decimal("200"), high=Decimal("200"), revision=2)
        reader.rows = (*rows[:-1], corrected)
        store.record_change(corrected.open_time)
        persisted = dict(store.values)
        revised = await service.get_page(INSTRUMENT, **args, page_size=2)
        self.assertEqual(revised.items[-1].close, Decimal("200"))
        self.assertEqual(store.values, persisted)
        older = await service.materialize_page(
            INSTRUMENT, **args, before=latest.next_before, page_size=2
        )
        self.assertFalse(older.has_more)
        complete = await service.get_page(INSTRUMENT, **args, page_size=5)
        self.assertEqual(
            complete.items, project_period_bars(reader.rows, period_id="5m", schedule=SHFE_SCHEDULE)
        )
        self.assertFalse(complete.has_more)
