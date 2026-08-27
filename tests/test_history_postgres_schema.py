from __future__ import annotations

import unittest

from tracefang.history_management.postgres import (
    _SELECT_TRUSTED_BARS,
    _SELECT_TRUSTED_QUOTES,
    HISTORY_SCHEMA_SQL,
    dataset_state_counts,
)


class HistoricalPostgresSchemaTests(unittest.TestCase):
    def test_candidate_quote_and_bar_records_are_physically_separate(self) -> None:
        self.assertIn("CREATE SCHEMA IF NOT EXISTS history", HISTORY_SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS history.quote_events", HISTORY_SCHEMA_SQL)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS history.bar_observations",
            HISTORY_SCHEMA_SQL,
        )
        self.assertIn("provider_family TEXT NOT NULL", HISTORY_SCHEMA_SQL)
        self.assertIn("channel_id TEXT NOT NULL", HISTORY_SCHEMA_SQL)
        self.assertIn("feed_id TEXT NOT NULL", HISTORY_SCHEMA_SQL)
        self.assertIn("timestamp_resolution_microseconds", HISTORY_SCHEMA_SQL)
        self.assertIn("effective_price_quantum", HISTORY_SCHEMA_SQL)

    def test_trusted_views_require_admitted_canonical_lineage(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS history.admission_decisions", HISTORY_SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS history.canonical_segments", HISTORY_SCHEMA_SQL)
        self.assertIn("dataset.state = 'trusted'", HISTORY_SCHEMA_SQL)
        self.assertIn("admission_decision_id", HISTORY_SCHEMA_SQL)
        self.assertNotIn("source_count = 1", HISTORY_SCHEMA_SQL)
        self.assertNotIn("standard_candles", HISTORY_SCHEMA_SQL)

    def test_history_queries_are_local_database_reads_only(self) -> None:
        self.assertIn("FROM history.trusted_quote_events", _SELECT_TRUSTED_QUOTES)
        self.assertIn("FROM history.trusted_bar_observations", _SELECT_TRUSTED_BARS)
        combined = f"{HISTORY_SCHEMA_SQL}{_SELECT_TRUSTED_QUOTES}{_SELECT_TRUSTED_BARS}".lower()
        self.assertNotIn("mcp", combined)
        self.assertNotIn("http://", combined)
        self.assertNotIn("https://", combined)

    def test_dataset_state_reporting_rejects_unknown_states(self) -> None:
        counts = dataset_state_counts(
            (
                {"state": "validated_candidate"},
                {"state": "validated_candidate"},
                {"state": "trusted"},
            )
        )

        self.assertEqual(counts["validated_candidate"], 2)
        self.assertEqual(counts["trusted"], 1)
        with self.assertRaisesRegex(ValueError, "unknown historical dataset state"):
            dataset_state_counts(({"state": "magically_trusted"},))


if __name__ == "__main__":
    unittest.main()
