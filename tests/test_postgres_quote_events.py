from __future__ import annotations

import unittest

from market_analysis.infrastructure.postgres.schema import SCHEMA_SQL
from market_analysis.infrastructure.postgres.store import (
    _INSERT_QUOTE,
    _SELECT_CANDLE_CACHE_RANGES,
    _SELECT_INSTRUMENT_SOURCE,
    _SELECT_RANGE_QUOTE_CANDLES,
    _SELECT_RANGE_SOURCE_CANDLES,
    _SELECT_RECENT_QUOTE_CANDLES,
    _SELECT_RECENT_SOURCE_CANDLES,
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


if __name__ == "__main__":
    unittest.main()
