from __future__ import annotations

import unittest

from market_analysis.infrastructure.postgres.schema import SCHEMA_SQL
from market_analysis.infrastructure.postgres.store import (
    _INSERT_QUOTE,
    _REMOVE_SOURCE_FROM_STANDARD_HISTORY,
    _SELECT_STANDARD_CANDLES_FROM,
    _SELECT_STANDARD_LATEST_CANDLES,
    _STANDARDIZE_CANDLES,
)


class QuoteEventPersistenceTests(unittest.TestCase):
    def test_distinguishes_every_received_frame_inside_the_same_source_second(self) -> None:
        unique_key = "source_id, provider_symbol, received_at"

        self.assertIn(f"ON quote_events ({unique_key})", SCHEMA_SQL)
        self.assertIn(f"ON CONFLICT ({unique_key}) DO NOTHING", _INSERT_QUOTE)
        self.assertNotIn(
            "UNIQUE (source_id, provider_symbol, observed_at, last)",
            SCHEMA_SQL,
        )

    def test_history_query_reads_only_validated_standard_rows(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS standard_candles", SCHEMA_SQL)
        self.assertIn("'validation_state', 'accepted'", _STANDARDIZE_CANDLES)
        self.assertIn("cross_source_consensus", _STANDARDIZE_CANDLES)
        self.assertIn("max_close_deviation_ratio <= $7", _STANDARDIZE_CANDLES)
        self.assertIn("source_id = ANY($3::text[])", _STANDARDIZE_CANDLES)
        for query in (_SELECT_STANDARD_CANDLES_FROM, _SELECT_STANDARD_LATEST_CANDLES):
            self.assertIn("FROM standard_candles", query)
            self.assertNotIn("FROM quote_events", query)
            self.assertNotIn("FROM candles\n", query)
            self.assertIn("primary_source_id = ANY", query)
            self.assertIn("jsonb_array_elements", query)

    def test_frozen_source_can_be_removed_from_standard_and_validation_history(self) -> None:
        self.assertIn("DELETE FROM standard_candles", _REMOVE_SOURCE_FROM_STANDARD_HISTORY)
        self.assertIn(
            "DELETE FROM candle_validation_results",
            _REMOVE_SOURCE_FROM_STANDARD_HISTORY,
        )
        self.assertIn("'source_id', $1", _REMOVE_SOURCE_FROM_STANDARD_HISTORY)


if __name__ == "__main__":
    unittest.main()
