from __future__ import annotations

import unittest
from datetime import UTC, datetime

from tracefang.application.gold_events import (
    GOLD_EVENT_FACTS,
    GOLD_EVENT_TYPES,
    gold_event_catalog_snapshot,
)


class GoldEventCatalogTests(unittest.TestCase):
    def test_catalog_uses_unique_referenced_types_and_explicit_source_lineage(self) -> None:
        type_ids = [value.event_type_id for value in GOLD_EVENT_TYPES]
        fact_ids = [value.event_id for value in GOLD_EVENT_FACTS]

        self.assertEqual(len(type_ids), len(set(type_ids)))
        self.assertEqual(len(fact_ids), len(set(fact_ids)))
        self.assertTrue({value.event_type_id for value in GOLD_EVENT_FACTS} <= set(type_ids))
        self.assertTrue(all(value.source_url.startswith("https://") for value in GOLD_EVENT_FACTS))
        self.assertTrue(
            all(value.source_published_at.tzinfo is not None for value in GOLD_EVENT_FACTS)
        )
        self.assertTrue(all(value.ingested_at.tzinfo is not None for value in GOLD_EVENT_FACTS))

    def test_snapshot_filters_by_marker_range_without_losing_known_future_schedule(self) -> None:
        snapshot = gold_event_catalog_snapshot(
            start=datetime(2026, 8, 7, tzinfo=UTC),
            end=datetime(2026, 8, 20, tzinfo=UTC),
            as_of=datetime(2026, 8, 10, 23, 59, tzinfo=UTC),
            generated_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
        fact_ids = [str(value["event_id"]) for value in snapshot.facts]

        self.assertEqual(
            fact_ids,
            ["bls-nfp-2026-08", "bls-cpi-2026-08", "fomc-minutes-2026-08"],
        )
        self.assertEqual(snapshot.contract_version, "gold-events-v1")
        self.assertEqual(snapshot.generated_at, datetime(2026, 8, 10, tzinfo=UTC))

    def test_as_of_hides_information_not_yet_published_to_the_source(self) -> None:
        snapshot = gold_event_catalog_snapshot(
            start=datetime(2026, 8, 7, tzinfo=UTC),
            end=datetime(2026, 8, 20, tzinfo=UTC),
            as_of=datetime(2026, 8, 9, tzinfo=UTC),
        )

        self.assertEqual(
            [value["event_id"] for value in snapshot.facts],
            ["bls-nfp-2026-08"],
        )

    def test_official_flow_separates_effective_period_from_publication_time(self) -> None:
        snapshot = gold_event_catalog_snapshot(
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 7, 3, tzinfo=UTC),
        )
        fact = next(
            value
            for value in snapshot.facts
            if value["event_id"] == "wgc-central-bank-gold-2026-07"
        )

        self.assertEqual(fact["flow_amount"], 41)
        self.assertEqual(fact["flow_unit"], "tonnes")
        self.assertEqual(fact["effective_period_start"], datetime(2026, 5, 1, tzinfo=UTC))
        self.assertEqual(fact["marker_at"], datetime(2026, 7, 2, 12, tzinfo=UTC))
        self.assertNotEqual(fact["effective_period_start"], fact["marker_at"])

    def test_unscheduled_risk_event_never_invents_a_scheduled_time(self) -> None:
        snapshot = gold_event_catalog_snapshot(
            start=datetime(2023, 3, 10, tzinfo=UTC),
            end=datetime(2023, 3, 11, tzinfo=UTC),
        )

        self.assertEqual(len(snapshot.facts), 1)
        self.assertEqual(snapshot.facts[0]["event_id"], "svb-failure-2023")
        self.assertIsNone(snapshot.facts[0]["scheduled_at"])
        self.assertEqual(snapshot.facts[0]["released_at"], datetime(2023, 3, 10, tzinfo=UTC))

    def test_score_contract_never_calls_price_or_volume_a_fund_flow(self) -> None:
        snapshot = gold_event_catalog_snapshot(generated_at=datetime(2026, 8, 10, tzinfo=UTC))
        regime = snapshot.score_methodology["regime"]

        self.assertEqual(regime["weights"]["durable_fund_flow"], 25)
        self.assertIn("不得冒充净资金流", regime["rule"])


if __name__ == "__main__":
    unittest.main()
