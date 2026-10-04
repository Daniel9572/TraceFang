from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import asyncpg

from tracefang.domain.market_events import BarState, RealtimeBar
from tracefang.domain.models import QuoteSnapshot, SourceMetadata
from tracefang.infrastructure.postgres.market_time import MARKET_TIME_SCHEMA_SQL
from tracefang.infrastructure.postgres.schema import SCHEMA_SQL
from tracefang.infrastructure.postgres.settings import PostgresSettings
from tracefang.infrastructure.postgres.store import PostgresMarketDataStore
from tracefang.instruments import SHFE_GOLD_WEIGHTED


@unittest.skipUnless(os.getenv("TRACEFANG_TEST_DATABASE_URL"), "isolated PostgreSQL test")
class MarketTimeMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_clock_repair_preserves_raw_evidence_and_authoritative_bars(self):
        dsn = os.environ["TRACEFANG_TEST_DATABASE_URL"]
        schema = "test_market_time_" + uuid4().hex
        connection = await asyncpg.connect(dsn)
        store = PostgresMarketDataStore(PostgresSettings(dsn))
        try:
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            await connection.execute(f'SET search_path TO "{schema}"')
            await connection.execute(SCHEMA_SQL)
            await connection.execute("DELETE FROM market_data_migrations")
            store._pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=1, server_settings={"search_path": schema}
            )
            now = datetime(2026, 10, 2, 11, tzinfo=UTC)
            original = now - timedelta(days=2)
            source = SourceMetadata(
                "tonghuashun_futures",
                "qh_au8888",
                now,
                now,
                {
                    "bar_clock": "provider_frame.received_at",
                    "wire_observed_at": original.isoformat(),
                    "observation_kind": "snapshot",
                },
            )
            price = Decimal("912.19")
            quote = QuoteSnapshot(
                SHFE_GOLD_WEIGHTED,
                price,
                None,
                None,
                None,
                None,
                Decimal("12.78"),
                Decimal("1.42"),
                source,
            )
            await store.save_quote(quote)
            bar = RealtimeBar(
                instrument=SHFE_GOLD_WEIGHTED,
                interval=timedelta(minutes=1),
                open_time=now,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=None,
                source=replace(source, raw_payload={"derivation": "quote_event"}),
                evidence_channel_id=source.provider,
                state=BarState.PROVISIONAL_QUOTE,
            )
            authoritative = replace(
                bar,
                open_time=original,
                state=BarState.FINAL,
                finalized_at=now,
                source=replace(source, raw_payload={"derivation": "authoritative_history"}),
            )
            await store.save_realtime_bars((bar, authoritative))
            await connection.execute(MARKET_TIME_SCHEMA_SQL)
            await connection.execute(MARKET_TIME_SCHEMA_SQL)
            self.assertEqual(await connection.fetchval("SELECT count(*) FROM realtime_bars"), 1)
            self.assertEqual(
                await connection.fetchval("SELECT count(*) FROM invalid_quote_time_bars"), 1
            )
            self.assertEqual(await connection.fetchval("SELECT observed_at FROM quote_events"), now)
            self.assertEqual(
                await connection.fetchval("SELECT observed_at FROM normalized_quote_events"),
                original,
            )
            latest = await store.load_latest_quote(SHFE_GOLD_WEIGHTED, source.provider)
            self.assertEqual(latest.source.observed_at, original)
            self.assertEqual((latest.last, latest.change), (quote.last, quote.change))
            # Equal market time is ordered by arrival, just as in the in-memory cache.
            newer = replace(
                quote,
                last=price + 1,
                source=replace(
                    source,
                    observed_at=original,
                    received_at=now + timedelta(seconds=1),
                    raw_payload={"bar_clock": "source.observed_at"},
                ),
            )
            await store.save_quote(newer)
            await store.save_quote(
                replace(
                    newer,
                    last=price,
                    source=replace(
                        newer.source,
                        received_at=now,
                    ),
                )
            )
            latest = await store.load_latest_quote(SHFE_GOLD_WEIGHTED, source.provider)
            self.assertEqual(latest.last, newer.last)
        finally:
            await store.close()
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await connection.close()


if __name__ == "__main__":
    unittest.main()
