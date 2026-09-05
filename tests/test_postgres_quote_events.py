from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tracefang.application.realtime_bars import HistoricalBarBatch
from tracefang.domain.market_events import BarState, RealtimeBar
from tracefang.domain.models import AssetClass, Candle, Instrument, SourceMetadata
from tracefang.infrastructure.postgres.schema import SCHEMA_SQL
from tracefang.infrastructure.postgres.settings import PostgresSettings
from tracefang.infrastructure.postgres.store import (
    _ADVANCE_LIVE_REALTIME_BAR_SERIES_STATE,
    _AGGREGATE_REALTIME_BAR_BUCKET,
    _COUNT_SOURCE_CANDLES_IN_RANGE,
    _DELETE_MERGED_CANDLE_CACHE_RANGES,
    _INSERT_QUOTE,
    _LOCK_REALTIME_BAR_SERIES,
    _SELECT_CANDLE_CACHE_RANGES,
    _SELECT_INSTRUMENT_SOURCE,
    _SELECT_MATERIALIZED_PERIOD_BARS_BEFORE,
    _SELECT_QUOTE_CANDLES_BEFORE,
    _SELECT_QUOTE_EVENT_PAGE,
    _SELECT_RANGE_QUOTE_CANDLES,
    _SELECT_RANGE_REALTIME_BARS,
    _SELECT_RANGE_SOURCE_CANDLES,
    _SELECT_REALTIME_BAR_INPUT_CHANGES,
    _SELECT_REALTIME_BAR_SERIES_STATE,
    _SELECT_REALTIME_BARS_BEFORE,
    _SELECT_RECENT_QUOTE_CANDLES,
    _SELECT_RECENT_REALTIME_BARS,
    _SELECT_RECENT_SOURCE_CANDLES,
    _SELECT_SOURCE_CANDLES_BEFORE,
    _UPSERT_MATERIALIZED_PERIOD_BAR,
    _UPSERT_REALTIME_BAR,
    _UPSERT_REALTIME_BAR_SERIES_STATE,
    PostgresMarketDataStore,
)

_START = datetime(2026, 8, 10, 1, tzinfo=UTC)
_INSTRUMENT = Instrument("TEST/USD", AssetClass.SPOT, "TEST", "USD", "OTC")


def _history_candle(at: datetime) -> Candle:
    return Candle(
        instrument=_INSTRUMENT,
        interval=timedelta(minutes=1),
        open_time=at,
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("2"),
        volume=Decimal("3"),
        source=SourceMetadata(
            provider="history-channel",
            provider_symbol="TESTUSD.PROVIDER",
            observed_at=at,
            received_at=at + timedelta(seconds=1),
            raw_payload={"history_file": "fixture"},
        ),
    )


def _history_bar(value: Candle) -> RealtimeBar:
    return RealtimeBar(
        instrument=value.instrument,
        interval=value.interval,
        open_time=value.open_time,
        open=value.open,
        high=value.high,
        low=value.low,
        close=value.close,
        volume=value.volume,
        source=SourceMetadata(
            provider="realtime",
            provider_symbol=value.source.provider_symbol,
            observed_at=value.source.observed_at,
            received_at=value.source.received_at,
            raw_payload={
                "evidence_channel_id": "history-channel",
                "derivation": "authoritative_history",
            },
        ),
        evidence_channel_id="history-channel",
        state=BarState.FINAL,
        finalized_at=value.source.received_at,
    )


class _FakeTransaction:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events

    async def __aenter__(self):
        self.events.append(("begin",))

    async def __aexit__(self, error_type, _error, _traceback):
        self.events.append(("rollback" if error_type else "commit",))


class _FakeConnection:
    def __init__(self, *, fail_on_bars: bool = False) -> None:
        self.events: list[tuple] = []
        self.fail_on_bars = fail_on_bars

    def transaction(self):
        return _FakeTransaction(self.events)

    async def execute(self, query, *args):
        self.events.append(("execute", query, args))

    async def executemany(self, query, args):
        self.events.append(("executemany", query, tuple(args)))
        if self.fail_on_bars and query == _UPSERT_REALTIME_BAR:
            raise RuntimeError("simulated Bar failure")

    async def fetch(self, query, *args):
        self.events.append(("fetch", query, args))
        if query == _DELETE_MERGED_CANDLE_CACHE_RANGES:
            return [
                {
                    "range_start": _START - timedelta(minutes=2),
                    "range_end": _START,
                }
            ]
        return []

    async def fetchval(self, query, *args):
        self.events.append(("fetchval", query, args))
        return 4 if query == _COUNT_SOURCE_CANDLES_IN_RANGE else None

    async def fetchrow(self, query, *args):
        self.events.append(("fetchrow", query, args))
        if query != _UPSERT_REALTIME_BAR_SERIES_STATE:
            return None
        return {
            "instrument_symbol": _INSTRUMENT.symbol,
            "realtime_source_id": "realtime",
            "upstream_channel_id": "history-channel",
            "provider_symbol": "TESTUSD.PROVIDER",
            "interval_seconds": 60,
            "latest_authoritative_open_time": _START + timedelta(minutes=1),
            "authoritative_through": _START + timedelta(minutes=2),
            "history_floor": None,
            "tail_checked_through": None,
            "tail_checked_at": None,
            "evidence_version": "fixture-v1",
            "updated_at": _START + timedelta(minutes=3),
        }


class _Acquire:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


class QuoteEventPersistenceTests(unittest.TestCase):
    def test_watchlist_has_a_persistent_profile_and_ordered_items(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS watchlists", SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS watchlist_items", SCHEMA_SQL)
        self.assertIn(
            "PRIMARY KEY (profile_id, instrument_symbol)",
            SCHEMA_SQL,
        )
        self.assertIn(
            "ON watchlist_items (profile_id, position, added_at)",
            SCHEMA_SQL,
        )

    def test_each_contract_has_one_realtime_source_binding(self) -> None:
        self.assertIn(
            "DELETE FROM instrument_source_routes\nWHERE capability <> 'realtime'",
            SCHEMA_SQL,
        )
        self.assertIn(
            "UNIQUE INDEX IF NOT EXISTS ux_instrument_source_routes_instrument",
            SCHEMA_SQL,
        )
        self.assertIn("capability = 'realtime'", _SELECT_INSTRUMENT_SOURCE)
        self.assertNotIn("ORDER BY CASE capability", _SELECT_INSTRUMENT_SOURCE)

    def test_distinguishes_every_received_frame_inside_the_same_source_second(self) -> None:
        unique_key = "source_id, event_id"

        self.assertIn("event_id TEXT", SCHEMA_SQL)
        self.assertIn("uq_quote_event_identity", SCHEMA_SQL)
        self.assertIn(f"ON quote_events ({unique_key})", SCHEMA_SQL)
        self.assertIn(f"ON CONFLICT ({unique_key})", _INSERT_QUOTE)
        self.assertNotIn("source_id, provider_symbol, received_at", _INSERT_QUOTE)
        self.assertNotIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_quote_event_received",
            SCHEMA_SQL,
        )
        self.assertNotIn(
            "UNIQUE (source_id, provider_symbol, observed_at, last)",
            SCHEMA_SQL,
        )

    def test_lossless_quote_history_uses_storage_cursor_without_time_conflation(self) -> None:
        self.assertIn("ix_quote_events_timeline_cursor", SCHEMA_SQL)
        self.assertIn("instrument_symbol, source_id, id DESC", SCHEMA_SQL)
        self.assertIn("id < $3", _SELECT_QUOTE_EVENT_PAGE)
        self.assertIn("source_id = ANY($2::text[])", _SELECT_QUOTE_EVENT_PAGE)
        self.assertIn("ORDER BY id DESC", _SELECT_QUOTE_EVENT_PAGE)
        self.assertIn("event_id", _SELECT_QUOTE_EVENT_PAGE)
        self.assertIn("observation_kind", _SELECT_QUOTE_EVENT_PAGE)
        self.assertIn("raw_payload ->> 'observation_kind'", _SELECT_QUOTE_EVENT_PAGE)
        self.assertNotIn("GROUP BY", _SELECT_QUOTE_EVENT_PAGE)

    def test_internal_bar_pages_use_exclusive_time_cursors(self) -> None:
        for query in (
            _SELECT_QUOTE_CANDLES_BEFORE,
            _SELECT_SOURCE_CANDLES_BEFORE,
            _SELECT_REALTIME_BARS_BEFORE,
        ):
            self.assertIn("ORDER BY", query)
            self.assertIn("LIMIT", query)
        self.assertIn("observed_at < $4", _SELECT_QUOTE_CANDLES_BEFORE)
        self.assertIn("open_time < $4", _SELECT_SOURCE_CANDLES_BEFORE)
        self.assertIn("open_time < $4", _SELECT_REALTIME_BARS_BEFORE)

    def test_kline_cache_queries_one_exact_raw_channel(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS standard_candles", SCHEMA_SQL)
        for query in (_SELECT_RECENT_QUOTE_CANDLES, _SELECT_RANGE_QUOTE_CANDLES):
            self.assertIn("FROM quote_events", query)
            self.assertIn("source_id = $2", query)
            self.assertNotIn("source_id = ANY", query)
            self.assertNotIn("standard_candles", query)
            self.assertIn("date_bin($3::int * INTERVAL '1 second'", query)

    def test_history_rows_and_completed_ranges_are_source_scoped(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS realtime_candle_cache_ranges", SCHEMA_SQL)
        self.assertIn("realtime_source_id TEXT NOT NULL", SCHEMA_SQL)
        self.assertIn("upstream_channel_id TEXT NOT NULL", SCHEMA_SQL)
        self.assertIn("CHECK (range_end > range_start)", SCHEMA_SQL)
        for query in (_SELECT_RECENT_SOURCE_CANDLES, _SELECT_RANGE_SOURCE_CANDLES):
            self.assertIn("FROM candles", query)
            self.assertIn("source_id = $2", query)
            self.assertNotIn("standard_candles", query)
        self.assertIn("realtime_source_id = $2", _SELECT_CANDLE_CACHE_RANGES)
        self.assertIn("range_end > $4", _SELECT_CANDLE_CACHE_RANGES)
        self.assertIn("range_start < $5", _SELECT_CANDLE_CACHE_RANGES)

    def test_history_authority_state_is_series_scoped_and_monotonic(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS realtime_bar_series_state", SCHEMA_SQL)
        self.assertIn("latest_authoritative_open_time TIMESTAMPTZ", SCHEMA_SQL)
        self.assertIn("authoritative_through TIMESTAMPTZ NOT NULL", SCHEMA_SQL)
        self.assertIn("history_floor TIMESTAMPTZ", SCHEMA_SQL)
        self.assertIn("tail_checked_through TIMESTAMPTZ", SCHEMA_SQL)
        self.assertIn("evidence_version TEXT NOT NULL", SCHEMA_SQL)
        self.assertIn(
            "PRIMARY KEY (realtime_source_id, instrument_symbol, interval_seconds)",
            SCHEMA_SQL,
        )
        self.assertIn("realtime_source_id = $2", _SELECT_REALTIME_BAR_SERIES_STATE)
        self.assertIn("authoritative_through = GREATEST(", _UPSERT_REALTIME_BAR_SERIES_STATE)
        self.assertIn(
            "realtime_bar_series_state.authoritative_through",
            _UPSERT_REALTIME_BAR_SERIES_STATE,
        )

    def test_realtime_bar_projection_persists_lifecycle_and_lineage(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS realtime_bars", SCHEMA_SQL)
        self.assertIn("evidence_channel_id TEXT NOT NULL", SCHEMA_SQL)
        self.assertIn("state TEXT NOT NULL", SCHEMA_SQL)
        self.assertIn("revision INTEGER NOT NULL", SCHEMA_SQL)
        self.assertIn("finalized_at TIMESTAMPTZ", SCHEMA_SQL)
        for query in (_SELECT_RECENT_REALTIME_BARS, _SELECT_RANGE_REALTIME_BARS):
            self.assertIn("FROM realtime_bars", query)
            self.assertIn("realtime_source_id = $2", query)
        self.assertIn("EXCLUDED.revision > realtime_bars.revision", _UPSERT_REALTIME_BAR)

    def test_derived_period_bars_are_versioned_and_revision_aware(self) -> None:
        self.assertIn("CREATE SEQUENCE IF NOT EXISTS realtime_bar_mutation_id_seq", SCHEMA_SQL)
        self.assertIn("mutation_id BIGINT", SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS derived_period_bars", SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS period_bar_materializations", SCHEMA_SQL)
        self.assertIn("materialization_version TEXT NOT NULL", SCHEMA_SQL)
        self.assertIn("mutation_id > $3", _SELECT_REALTIME_BAR_INPUT_CHANGES)
        self.assertIn("ORDER BY mutation_id", _SELECT_REALTIME_BAR_INPUT_CHANGES)
        self.assertIn("FROM derived_period_bars", _SELECT_MATERIALIZED_PERIOD_BARS_BEFORE)
        self.assertIn(
            "first_component_open_time < $5",
            _SELECT_MATERIALIZED_PERIOD_BARS_BEFORE,
        )
        self.assertNotIn("OR open_time < $5", _SELECT_MATERIALIZED_PERIOD_BARS_BEFORE)
        self.assertIn("ORDER BY open_time DESC", _SELECT_MATERIALIZED_PERIOD_BARS_BEFORE)
        self.assertIn("materialized_at = now()", _UPSERT_MATERIALIZED_PERIOD_BAR)
        self.assertIn("max(high) AS high", _AGGREGATE_REALTIME_BAR_BUCKET)
        self.assertIn("min(low) AS low", _AGGREGATE_REALTIME_BAR_BUCKET)
        self.assertIn("open_time >= $3", _AGGREGATE_REALTIME_BAR_BUCKET)
        self.assertIn("open_time < $4", _AGGREGATE_REALTIME_BAR_BUCKET)


class HistoricalBatchTransactionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _batch() -> tuple[HistoricalBarBatch, tuple[RealtimeBar, ...]]:
        candles = (
            _history_candle(_START),
            _history_candle(_START + timedelta(minutes=1)),
        )
        return (
            HistoricalBarBatch(
                candles=candles,
                checked_start=_START,
                checked_end=_START + timedelta(minutes=2),
                authoritative_through=_START + timedelta(minutes=2),
                evidence_version="fixture-v1",
                checked_at=_START + timedelta(minutes=3),
            ),
            tuple(_history_bar(value) for value in candles),
        )

    @staticmethod
    def _store(connection: _FakeConnection) -> PostgresMarketDataStore:
        store = PostgresMarketDataStore(PostgresSettings("postgresql://unused"))
        store._pool = _FakePool(connection)  # type: ignore[assignment]
        return store

    async def test_history_batch_commits_all_records_and_compacts_coverage(self) -> None:
        connection = _FakeConnection()
        batch, bars = self._batch()

        state = await self._store(connection).commit_historical_bar_batch(
            _INSTRUMENT,
            realtime_source_id="realtime",
            upstream_channel_id="history-channel",
            provider_symbol="TESTUSD.PROVIDER",
            batch=batch,
            bars=bars,
        )

        self.assertEqual(connection.events[0], ("begin",))
        self.assertEqual(connection.events[-1], ("commit",))
        self.assertEqual(state.authoritative_through, _START + timedelta(minutes=2))
        lock = next(
            event
            for event in connection.events
            if event[0] == "fetchval" and event[1] == _LOCK_REALTIME_BAR_SERIES
        )
        self.assertIn("$3::integer::text", lock[1])
        self.assertEqual(lock[2], ("realtime", _INSTRUMENT.symbol, 60))
        coverage = next(
            event
            for event in connection.events
            if event[0] == "execute" and "realtime_candle_cache_ranges" in event[1]
        )
        self.assertEqual(coverage[2][5], _START - timedelta(minutes=2))
        self.assertEqual(coverage[2][6], _START + timedelta(minutes=2))
        self.assertEqual(coverage[2][7], 4)
        self.assertTrue(
            any(
                event[0] == "fetchrow" and event[1] == _UPSERT_REALTIME_BAR_SERIES_STATE
                for event in connection.events
            )
        )

    async def test_history_batch_failure_rolls_back_before_coverage_or_state(self) -> None:
        connection = _FakeConnection(fail_on_bars=True)
        batch, bars = self._batch()

        with self.assertRaisesRegex(RuntimeError, "simulated Bar failure"):
            await self._store(connection).commit_historical_bar_batch(
                _INSTRUMENT,
                realtime_source_id="realtime",
                upstream_channel_id="history-channel",
                provider_symbol="TESTUSD.PROVIDER",
                batch=batch,
                bars=bars,
            )

        self.assertEqual(connection.events[-1], ("rollback",))
        self.assertFalse(
            any(
                event[0] in {"fetch", "fetchrow"}
                and event[1]
                in {
                    _DELETE_MERGED_CANDLE_CACHE_RANGES,
                    _UPSERT_REALTIME_BAR_SERIES_STATE,
                }
                for event in connection.events
            )
        )

    async def test_final_live_authority_advances_the_persisted_series_checkpoint(self) -> None:
        connection = _FakeConnection()
        value = _history_bar(_history_candle(_START))

        await self._store(connection).save_realtime_bars((value,))

        checkpoint = next(
            event
            for event in connection.events
            if event[0] == "execute"
            and event[1] == _ADVANCE_LIVE_REALTIME_BAR_SERIES_STATE
        )
        self.assertEqual(
            checkpoint[2][0:5],
            (_INSTRUMENT.symbol, "realtime", "history-channel", "TESTUSD.PROVIDER", 60),
        )
        self.assertEqual(checkpoint[2][5], _START)
        self.assertEqual(checkpoint[2][6], _START + timedelta(minutes=1))


if __name__ == "__main__":
    unittest.main()
