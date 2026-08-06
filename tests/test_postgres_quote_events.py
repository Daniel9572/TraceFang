from __future__ import annotations

import unittest

from market_analysis.infrastructure.postgres.schema import SCHEMA_SQL
from market_analysis.infrastructure.postgres.store import _INSERT_QUOTE


class QuoteEventPersistenceTests(unittest.TestCase):
    def test_distinguishes_every_received_frame_inside_the_same_source_second(self) -> None:
        unique_key = "source_id, provider_symbol, received_at"

        self.assertIn(f"ON quote_events ({unique_key})", SCHEMA_SQL)
        self.assertIn(f"ON CONFLICT ({unique_key}) DO NOTHING", _INSERT_QUOTE)
        self.assertNotIn(
            "UNIQUE (source_id, provider_symbol, observed_at, last)",
            SCHEMA_SQL,
        )


if __name__ == "__main__":
    unittest.main()
