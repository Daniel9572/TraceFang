from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

import asyncpg

from market_analysis.domain.models import Candle, Instrument, QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.postgres.schema import SCHEMA_SQL
from market_analysis.infrastructure.postgres.settings import PostgresSettings

_UPSERT_INSTRUMENT = """
INSERT INTO instruments (symbol, asset_class, base_asset, quote_asset, venue)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (symbol) DO UPDATE SET
    asset_class = EXCLUDED.asset_class,
    base_asset = EXCLUDED.base_asset,
    quote_asset = EXCLUDED.quote_asset,
    venue = EXCLUDED.venue,
    updated_at = now()
"""

_UPSERT_SOURCE = """
INSERT INTO market_sources (source_id)
VALUES ($1)
ON CONFLICT (source_id) DO UPDATE SET last_seen_at = now()
"""

_INSERT_QUOTE = """
INSERT INTO quote_events (
    instrument_symbol, source_id, provider_symbol, observed_at, received_at,
    last, open, high, low, volume, change, change_percent, raw_payload
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb)
ON CONFLICT (source_id, provider_symbol, observed_at, last) DO NOTHING
"""

_UPSERT_LATEST_QUOTE = """
INSERT INTO latest_quotes (
    instrument_symbol, source_id, provider_symbol, observed_at, received_at,
    last, open, high, low, volume, change, change_percent, raw_payload
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb)
ON CONFLICT (instrument_symbol, source_id) DO UPDATE SET
    provider_symbol = EXCLUDED.provider_symbol,
    observed_at = EXCLUDED.observed_at,
    received_at = EXCLUDED.received_at,
    last = EXCLUDED.last,
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    volume = EXCLUDED.volume,
    change = EXCLUDED.change,
    change_percent = EXCLUDED.change_percent,
    raw_payload = EXCLUDED.raw_payload
WHERE EXCLUDED.observed_at >= latest_quotes.observed_at
"""

_UPSERT_CANDLE = """
INSERT INTO candles (
    instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
    observed_at, received_at, open, high, low, close, volume, raw_payload
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb)
ON CONFLICT (source_id, provider_symbol, interval_seconds, open_time) DO UPDATE SET
    instrument_symbol = EXCLUDED.instrument_symbol,
    observed_at = EXCLUDED.observed_at,
    received_at = EXCLUDED.received_at,
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    raw_payload = EXCLUDED.raw_payload,
    persisted_at = now()
WHERE EXCLUDED.received_at >= candles.received_at
"""

_SELECT_INSTRUMENT_SOURCE = """
SELECT source_id
FROM instrument_source_routes
WHERE instrument_symbol = $1
ORDER BY CASE capability WHEN 'quote' THEN 0 ELSE 1 END
LIMIT 1
"""

_UPSERT_SOURCE_ROUTE = """
INSERT INTO instrument_source_routes (instrument_symbol, capability, source_id)
VALUES ($1, $2, $3)
ON CONFLICT (instrument_symbol, capability) DO UPDATE SET
    source_id = EXCLUDED.source_id,
    updated_at = now()
"""

_SELECT_CANONICAL_CANDLES_FROM = """
WITH persisted AS (
    SELECT
        instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
        observed_at, received_at, open, high, low, close, volume, raw_payload,
        0 AS record_rank
    FROM candles
    WHERE instrument_symbol = $1
      AND interval_seconds = $2
      AND open_time >= $5
), quote_derived AS (
    SELECT
        instrument_symbol,
        source_id,
        (array_agg(provider_symbol ORDER BY observed_at, id))[1] AS provider_symbol,
        60 AS interval_seconds,
        date_trunc('minute', observed_at) AS open_time,
        max(observed_at) AS observed_at,
        max(received_at) AS received_at,
        (array_agg(last ORDER BY observed_at, id))[1] AS open,
        max(last) AS high,
        min(last) AS low,
        (array_agg(last ORDER BY observed_at DESC, id DESC))[1] AS close,
        NULL::numeric AS volume,
        jsonb_build_object('derived_from', 'persisted_quote_events') AS raw_payload,
        1 AS record_rank
    FROM quote_events
    WHERE instrument_symbol = $1
      AND $2 = 60
      AND source_id = ANY($4::text[])
      AND observed_at >= $5
      AND observed_at < $6
    GROUP BY instrument_symbol, source_id, date_trunc('minute', observed_at)
), selected AS (
    SELECT DISTINCT ON (open_time)
        instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
        observed_at, received_at, open, high, low, close, volume, raw_payload
    FROM (
        SELECT * FROM persisted
        UNION ALL
        SELECT * FROM quote_derived
    ) AS candidates
    ORDER BY
        open_time ASC,
        COALESCE(array_position($3::text[], source_id), 2147483647) ASC,
        record_rank ASC,
        received_at DESC
)
SELECT
    instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
    observed_at, received_at, open, high, low, close, volume, raw_payload
FROM selected
ORDER BY open_time ASC
LIMIT $7
"""

_SELECT_CANONICAL_LATEST_CANDLES = """
WITH persisted AS (
    SELECT
        instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
        observed_at, received_at, open, high, low, close, volume, raw_payload,
        0 AS record_rank
    FROM candles
    WHERE instrument_symbol = $1
      AND interval_seconds = $2
), quote_derived AS (
    SELECT
        instrument_symbol,
        source_id,
        (array_agg(provider_symbol ORDER BY observed_at, id))[1] AS provider_symbol,
        60 AS interval_seconds,
        date_trunc('minute', observed_at) AS open_time,
        max(observed_at) AS observed_at,
        max(received_at) AS received_at,
        (array_agg(last ORDER BY observed_at, id))[1] AS open,
        max(last) AS high,
        min(last) AS low,
        (array_agg(last ORDER BY observed_at DESC, id DESC))[1] AS close,
        NULL::numeric AS volume,
        jsonb_build_object('derived_from', 'persisted_quote_events') AS raw_payload,
        1 AS record_rank
    FROM quote_events
    WHERE instrument_symbol = $1
      AND $2 = 60
      AND source_id = ANY($4::text[])
      AND observed_at >= $5
    GROUP BY instrument_symbol, source_id, date_trunc('minute', observed_at)
), selected AS (
    SELECT DISTINCT ON (open_time)
        instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
        observed_at, received_at, open, high, low, close, volume, raw_payload
    FROM (
        SELECT * FROM persisted
        UNION ALL
        SELECT * FROM quote_derived
    ) AS candidates
    ORDER BY
        open_time DESC,
        COALESCE(array_position($3::text[], source_id), 2147483647) ASC,
        record_rank ASC,
        received_at DESC
), recent AS (
    SELECT * FROM selected
    ORDER BY open_time DESC
    LIMIT $6
)
SELECT
    instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
    observed_at, received_at, open, high, low, close, volume, raw_payload
FROM recent
ORDER BY open_time ASC
"""

_SELECT_RECENT_QUOTE_CANDLES = """
SELECT *
FROM (
    SELECT
        date_trunc('minute', observed_at) AS open_time,
        (array_agg(provider_symbol ORDER BY observed_at, id))[1] AS provider_symbol,
        (array_agg(last ORDER BY observed_at, id))[1] AS open,
        max(last) AS high,
        min(last) AS low,
        (array_agg(last ORDER BY observed_at DESC, id DESC))[1] AS close,
        max(observed_at) AS observed_at,
        max(received_at) AS received_at
    FROM quote_events
    WHERE instrument_symbol = $1 AND source_id = $2 AND observed_at >= $3
    GROUP BY date_trunc('minute', observed_at)
    ORDER BY open_time DESC
    LIMIT $4
) AS recent
ORDER BY open_time
"""

_SELECT_RANGE_QUOTE_CANDLES = """
SELECT
    date_trunc('minute', observed_at) AS open_time,
    (array_agg(provider_symbol ORDER BY observed_at, id))[1] AS provider_symbol,
    (array_agg(last ORDER BY observed_at, id))[1] AS open,
    max(last) AS high,
    min(last) AS low,
    (array_agg(last ORDER BY observed_at DESC, id DESC))[1] AS close,
    max(observed_at) AS observed_at,
    max(received_at) AS received_at
FROM quote_events
WHERE instrument_symbol = $1
  AND source_id = $2
  AND observed_at >= $3
  AND observed_at < $4
GROUP BY date_trunc('minute', observed_at)
ORDER BY open_time
LIMIT $5
"""


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"cannot encode {type(value).__name__} as JSON")


def _payload_json(value: object, raw_payload: object | None) -> str:
    payload = raw_payload if raw_payload is not None else asdict(value)
    return json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":"))


def _instrument_values(instrument: Instrument) -> tuple[object, ...]:
    return (
        instrument.symbol,
        instrument.asset_class.value,
        instrument.base,
        instrument.quote,
        instrument.venue,
    )


def _quote_values(quote: QuoteSnapshot) -> tuple[object, ...]:
    source = quote.source
    return (
        quote.instrument.symbol,
        source.provider,
        source.provider_symbol,
        source.observed_at,
        source.received_at,
        quote.last,
        quote.open,
        quote.high,
        quote.low,
        quote.volume,
        quote.change,
        quote.change_percent,
        _payload_json(quote, source.raw_payload),
    )


def _candle_values(candle: Candle) -> tuple[object, ...]:
    source = candle.source
    return (
        candle.instrument.symbol,
        source.provider,
        source.provider_symbol,
        int(candle.interval.total_seconds()),
        candle.open_time,
        source.observed_at,
        source.received_at,
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume,
        _payload_json(candle, source.raw_payload),
    )


def _raw_payload(value: object) -> Mapping[str, object] | None:
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else None
    return value if isinstance(value, Mapping) else None


def _candle_from_row(row: Mapping[str, object], instrument: Instrument) -> Candle:
    return Candle(
        instrument=instrument,
        interval=timedelta(seconds=int(row["interval_seconds"])),
        open_time=row["open_time"],
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=Decimal(row["volume"]) if row["volume"] is not None else None,
        source=SourceMetadata(
            provider=str(row["source_id"]),
            provider_symbol=str(row["provider_symbol"]),
            observed_at=row["observed_at"],
            received_at=row["received_at"],
            raw_payload=_raw_payload(row["raw_payload"]),
        ),
    )


class PostgresMarketDataStore:
    def __init__(self, settings: PostgresSettings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    @property
    def is_open(self) -> bool:
        return self._pool is not None

    async def open(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(
            dsn=self._settings.dsn,
            min_size=self._settings.min_pool_size,
            max_size=self._settings.max_pool_size,
            command_timeout=self._settings.command_timeout_seconds,
        )
        try:
            async with pool.acquire() as connection:
                await connection.execute(SCHEMA_SQL)
        except BaseException:
            await pool.close()
            raise
        self._pool = pool

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgreSQL store is not connected")
        return self._pool

    async def save_quote(self, quote: QuoteSnapshot) -> None:
        pool = self._require_pool()
        values = _quote_values(quote)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(_UPSERT_INSTRUMENT, *_instrument_values(quote.instrument))
            await connection.execute(_UPSERT_SOURCE, quote.source.provider)
            await connection.execute(_INSERT_QUOTE, *values)
            await connection.execute(_UPSERT_LATEST_QUOTE, *values)

    async def save_candles(self, candles: Sequence[Candle]) -> None:
        if not candles:
            return
        pool = self._require_pool()
        instruments = {candle.instrument.symbol: candle.instrument for candle in candles}
        sources = {candle.source.provider for candle in candles}
        async with pool.acquire() as connection, connection.transaction():
            for instrument in instruments.values():
                await connection.execute(_UPSERT_INSTRUMENT, *_instrument_values(instrument))
            for source_id in sources:
                await connection.execute(_UPSERT_SOURCE, source_id)
            await connection.executemany(_UPSERT_CANDLE, [_candle_values(row) for row in candles])

    async def load_candles(
        self,
        instrument: Instrument,
        *,
        source_priority: Sequence[str],
        quote_derived_sources: Sequence[str],
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        if count < 1:
            raise ValueError("count must be positive")
        interval_seconds = int(interval.total_seconds())
        if interval_seconds < 1:
            raise ValueError("interval must be positive")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")
        priority = tuple(source_priority)
        derived_sources = tuple(quote_derived_sources)
        pool = self._require_pool()
        async with pool.acquire() as connection:
            if start is None:
                quote_cutoff = datetime.now(UTC) - timedelta(minutes=max(7 * 24 * 60, count * 3))
                rows = await connection.fetch(
                    _SELECT_CANONICAL_LATEST_CANDLES,
                    instrument.symbol,
                    interval_seconds,
                    priority,
                    derived_sources,
                    quote_cutoff,
                    count,
                )
            else:
                quote_end = start + timedelta(days=7)
                rows = await connection.fetch(
                    _SELECT_CANONICAL_CANDLES_FROM,
                    instrument.symbol,
                    interval_seconds,
                    priority,
                    derived_sources,
                    start,
                    quote_end,
                    count,
                )
        return tuple(_candle_from_row(row, instrument) for row in rows)

    async def get_instrument_source(self, instrument: Instrument) -> str | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            return await connection.fetchval(
                _SELECT_INSTRUMENT_SOURCE,
                instrument.symbol,
            )

    async def set_instrument_source(
        self,
        instrument: Instrument,
        source_id: str,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(_UPSERT_INSTRUMENT, *_instrument_values(instrument))
            await connection.execute(_UPSERT_SOURCE, source_id)
            await connection.execute(
                _UPSERT_SOURCE_ROUTE,
                instrument.symbol,
                "quote",
                source_id,
            )

    async def load_quote_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            if start is None:
                cutoff = datetime.now(UTC) - timedelta(minutes=max(180, count * 3))
                rows = await connection.fetch(
                    _SELECT_RECENT_QUOTE_CANDLES,
                    instrument.symbol,
                    source_id,
                    cutoff,
                    count,
                )
            else:
                rows = await connection.fetch(
                    _SELECT_RANGE_QUOTE_CANDLES,
                    instrument.symbol,
                    source_id,
                    start,
                    start + timedelta(minutes=count),
                    count,
                )
        return tuple(
            Candle(
                instrument=instrument,
                interval=timedelta(minutes=1),
                open_time=row["open_time"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=None,
                source=SourceMetadata(
                    provider=source_id,
                    provider_symbol=row["provider_symbol"],
                    observed_at=row["observed_at"],
                    received_at=row["received_at"],
                    raw_payload={"derived_from": "persisted_quote_events"},
                ),
            )
            for row in rows
        )
