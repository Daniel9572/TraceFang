from __future__ import annotations

import unittest

from market_analysis.infrastructure.postgres.schema import SCHEMA_SQL
from market_analysis.infrastructure.postgres.store import (
    _INSERT_QUOTE,
    _SELECT_CANDLE_CACHE_RANGES,
    _SELECT_INSTRUMENT_SOURCE,
    _SELECT_MATERIALIZED_PERIOD_BARS_BEFORE,
    _SELECT_QUOTE_CANDLES_BEFORE,
    _SELECT_QUOTE_EVENT_PAGE,
    _SELECT_RANGE_QUOTE_CANDLES,
    _SELECT_RANGE_REALTIME_BARS,
    _SELECT_RANGE_SOURCE_CANDLES,
    _SELECT_REALTIME_BAR_INPUT_CHANGES,
    _SELECT_REALTIME_BARS_BEFORE,
    _SELECT_RECENT_QUOTE_CANDLES,
    _SELECT_RECENT_REALTIME_BARS,
    _SELECT_RECENT_SOURCE_CANDLES,
    _SELECT_SOURCE_CANDLES_BEFORE,
    _UPSERT_MATERIALIZED_PERIOD_BAR,
    _UPSERT_REALTIME_BAR,
)


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
        unique_key = "source_id, provider_symbol, received_at"

        self.assertIn(f"ON quote_events ({unique_key})", SCHEMA_SQL)
        self.assertIn(f"ON CONFLICT ({unique_key}) DO NOTHING", _INSERT_QUOTE)
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
        self.assertNotIn("GROUP BY", _SELECT_QUOTE_EVENT_PAGE)

    def test_internal_bar_pages_use_exclusive_time_cursors(self) -> None:
        for query in (
            _SELECT_QUOTE_CANDLES_BEFORE,
            _SELECT_SOURCE_CANDLES_BEFORE,
            _SELECT_REALTIME_BARS_BEFORE,
        ):
            self.assertIn("ORDER BY", query)
            self.assertIn("LIMIT", query)
        self.assertIn("observed_at < $3", _SELECT_QUOTE_CANDLES_BEFORE)
        self.assertIn("open_time < $4", _SELECT_SOURCE_CANDLES_BEFORE)
        self.assertIn("open_time < $4", _SELECT_REALTIME_BARS_BEFORE)

    def test_kline_cache_queries_one_exact_raw_channel(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS standard_candles", SCHEMA_SQL)
        for query in (_SELECT_RECENT_QUOTE_CANDLES, _SELECT_RANGE_QUOTE_CANDLES):
            self.assertIn("FROM quote_events", query)
            self.assertIn("source_id = $2", query)
            self.assertNotIn("source_id = ANY", query)
            self.assertNotIn("standard_candles", query)

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
        self.assertIn("ORDER BY open_time DESC", _SELECT_MATERIALIZED_PERIOD_BARS_BEFORE)
        self.assertIn("materialized_at = now()", _UPSERT_MATERIALIZED_PERIOD_BAR)


if __name__ == "__main__":
    unittest.main()
