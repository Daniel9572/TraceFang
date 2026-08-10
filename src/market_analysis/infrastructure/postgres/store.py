from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

import asyncpg

from market_analysis.application.period_bars import (
    PeriodBarInputChange,
    PeriodBarMaterializationState,
)
from market_analysis.domain.market_events import BarState, QuoteSample, RealtimeBar
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

_INITIALIZE_WATCHLIST = """
INSERT INTO watchlists (profile_id)
VALUES ($1)
ON CONFLICT (profile_id) DO NOTHING
RETURNING profile_id
"""

_INSERT_WATCHLIST_ITEM = """
INSERT INTO watchlist_items (profile_id, instrument_symbol, position)
VALUES ($1, $2, $3)
ON CONFLICT (profile_id, instrument_symbol) DO NOTHING
"""

_ADD_WATCHLIST_ITEM = """
INSERT INTO watchlist_items (profile_id, instrument_symbol, position)
SELECT $1, $2, COALESCE(max(position) + 1, 0)
FROM watchlist_items
WHERE profile_id = $1
ON CONFLICT (profile_id, instrument_symbol) DO NOTHING
"""

_SELECT_WATCHLIST = """
SELECT instrument_symbol
FROM watchlist_items
WHERE profile_id = $1
ORDER BY position, added_at, instrument_symbol
"""

_REMOVE_WATCHLIST_ITEM = """
DELETE FROM watchlist_items
WHERE profile_id = $1 AND instrument_symbol = $2
"""

_INSERT_QUOTE = """
INSERT INTO quote_events (
    instrument_symbol, source_id, provider_symbol, observed_at, received_at,
    last, open, high, low, volume, change, change_percent, raw_payload
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb)
ON CONFLICT (source_id, provider_symbol, received_at) DO NOTHING
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

_SELECT_LATEST_QUOTE = """
SELECT
    instrument_symbol, source_id, provider_symbol, observed_at, received_at,
    last, open, high, low, volume, change, change_percent, raw_payload
FROM latest_quotes
WHERE instrument_symbol = $1 AND source_id = $2
"""

_REMOVE_SOURCE_FROM_STANDARD_HISTORY = """
WITH deleted_standard AS (
    DELETE FROM standard_candles
    WHERE primary_source_id = $1
       OR COALESCE(evidence -> 'candidates', '[]'::jsonb)
          @> jsonb_build_array(jsonb_build_object('source_id', $1))
    RETURNING 1
), deleted_validation AS (
    DELETE FROM candle_validation_results
    WHERE evidence @> jsonb_build_array(jsonb_build_object('source_id', $1))
    RETURNING 1
)
SELECT
    (SELECT count(*)::integer FROM deleted_standard) AS standard_rows,
    (SELECT count(*)::integer FROM deleted_validation) AS validation_rows
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

_SELECT_QUOTE_EVENT_PAGE = """
SELECT *
FROM (
    SELECT
        id, instrument_symbol, source_id, provider_symbol,
        observed_at, received_at, last
    FROM quote_events
    WHERE instrument_symbol = $1
      AND source_id = ANY($2::text[])
      AND ($3::bigint IS NULL OR id < $3)
    ORDER BY id DESC
    LIMIT $4
) AS recent
ORDER BY id ASC
"""

_UPSERT_REALTIME_BAR = """
INSERT INTO realtime_bars (
    instrument_symbol, realtime_source_id, evidence_channel_id, provider_symbol,
    interval_seconds, open_time, observed_at, received_at,
    open, high, low, close, volume, state, revision, finalized_at, raw_payload
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8,
    $9, $10, $11, $12, $13, $14, $15, $16, $17::jsonb
)
ON CONFLICT (realtime_source_id, instrument_symbol, interval_seconds, open_time)
DO UPDATE SET
    evidence_channel_id = EXCLUDED.evidence_channel_id,
    provider_symbol = EXCLUDED.provider_symbol,
    observed_at = EXCLUDED.observed_at,
    received_at = EXCLUDED.received_at,
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    state = EXCLUDED.state,
    revision = EXCLUDED.revision,
    finalized_at = EXCLUDED.finalized_at,
    mutation_id = nextval('realtime_bar_mutation_id_seq'),
    raw_payload = EXCLUDED.raw_payload,
    persisted_at = now()
WHERE EXCLUDED.revision > realtime_bars.revision
   OR (
       EXCLUDED.revision = realtime_bars.revision
       AND EXCLUDED.received_at > realtime_bars.received_at
   )
"""

_SELECT_INSTRUMENT_SOURCE = """
SELECT source_id
FROM instrument_source_routes
WHERE instrument_symbol = $1 AND capability = 'realtime'
"""

_UPSERT_SOURCE_ROUTE = """
INSERT INTO instrument_source_routes (instrument_symbol, capability, source_id)
VALUES ($1, $2, $3)
ON CONFLICT (instrument_symbol, capability) DO UPDATE SET
    source_id = EXCLUDED.source_id,
    updated_at = now()
"""

_STANDARDIZE_CANDLES = """
WITH persisted AS (
    SELECT
        instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
        observed_at, received_at, open, high, low, close, volume, raw_payload,
        0 AS record_rank
    FROM candles
    WHERE instrument_symbol = $1
      AND interval_seconds = $2
      AND source_id = ANY($3::text[])
      AND open_time >= $5
      AND open_time < $6
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
), within_source AS (
    SELECT DISTINCT ON (source_id, open_time)
        instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
        observed_at, received_at, open, high, low, close, volume, raw_payload,
        record_rank
    FROM (
        SELECT * FROM persisted
        UNION ALL
        SELECT * FROM quote_derived
    ) AS candidates
    ORDER BY source_id, open_time, record_rank, received_at DESC
), minute_stats AS (
    SELECT
        open_time,
        count(*)::integer AS source_count,
        COALESCE(
            (max(close) - min(close)) / NULLIF(abs(avg(close)), 0),
            0
        ) AS max_close_deviation_ratio,
        jsonb_agg(
            jsonb_build_object(
                'source_id', source_id,
                'provider_symbol', provider_symbol,
                'open', open::text,
                'high', high::text,
                'low', low::text,
                'close', close::text,
                'observed_at', observed_at,
                'received_at', received_at
            )
            ORDER BY
                COALESCE(array_position($3::text[], source_id), 2147483647),
                source_id
        ) AS candidate_evidence
    FROM within_source
    GROUP BY open_time
), validation_log AS (
    INSERT INTO candle_validation_results (
        instrument_symbol, interval_seconds, open_time, validation_state,
        source_count, max_close_deviation_ratio, evidence
    )
    SELECT
        $1,
        $2,
        open_time,
        CASE
            WHEN source_count = 1 OR max_close_deviation_ratio <= $7
                THEN 'accepted'
            ELSE 'rejected'
        END,
        source_count,
        max_close_deviation_ratio,
        candidate_evidence
    FROM minute_stats
    ON CONFLICT (instrument_symbol, interval_seconds, open_time) DO UPDATE SET
        validation_state = EXCLUDED.validation_state,
        source_count = EXCLUDED.source_count,
        max_close_deviation_ratio = EXCLUDED.max_close_deviation_ratio,
        evidence = EXCLUDED.evidence,
        evaluated_at = now()
    RETURNING open_time, validation_state
), accepted_minutes AS (
    SELECT stats.*
    FROM minute_stats AS stats
    JOIN validation_log AS logged USING (open_time)
    WHERE logged.validation_state = 'accepted'
), chosen AS (
    SELECT DISTINCT ON (candidate.open_time)
        candidate.*,
        stats.source_count,
        stats.max_close_deviation_ratio,
        stats.candidate_evidence
    FROM within_source AS candidate
    JOIN accepted_minutes AS stats USING (open_time)
    ORDER BY
        candidate.open_time,
        COALESCE(array_position($3::text[], candidate.source_id), 2147483647),
        candidate.record_rank,
        candidate.received_at DESC
), accepted AS (
    SELECT
        chosen.*,
        CASE
            WHEN chosen.source_count > 1 THEN 'cross_source_consensus'
            ELSE 'single_source_structural'
        END AS validation_method,
        jsonb_build_object(
            'validation_state', 'accepted',
            'primary_source_id', chosen.source_id,
            'source_count', chosen.source_count,
            'max_close_deviation_ratio', chosen.max_close_deviation_ratio::text,
            'candidates', chosen.candidate_evidence
        ) AS evidence
    FROM chosen
)
INSERT INTO standard_candles (
    instrument_symbol, interval_seconds, open_time, observed_at, received_at,
    open, high, low, close, volume, primary_source_id, source_count,
    validation_method, max_close_deviation_ratio, evidence
)
SELECT
    instrument_symbol, interval_seconds, open_time, observed_at, received_at,
    open, high, low, close, volume, source_id, source_count,
    validation_method, max_close_deviation_ratio, evidence
FROM accepted
ON CONFLICT (instrument_symbol, interval_seconds, open_time) DO UPDATE SET
    observed_at = EXCLUDED.observed_at,
    received_at = EXCLUDED.received_at,
    validated_at = now(),
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    primary_source_id = EXCLUDED.primary_source_id,
    source_count = EXCLUDED.source_count,
    validation_method = EXCLUDED.validation_method,
    max_close_deviation_ratio = EXCLUDED.max_close_deviation_ratio,
    evidence = EXCLUDED.evidence,
    revision = standard_candles.revision + 1
WHERE standard_candles.evidence IS DISTINCT FROM EXCLUDED.evidence
   OR EXCLUDED.received_at > standard_candles.received_at
"""

_SELECT_STANDARD_CANDLES_FROM = """
SELECT
    instrument_symbol, 'standard_history' AS source_id,
    instrument_symbol AS provider_symbol, interval_seconds, open_time,
    observed_at, received_at, open, high, low, close, volume,
    evidence || jsonb_build_object(
        'validation_method', validation_method,
        'validated_at', validated_at,
        'revision', revision
    ) AS raw_payload
FROM standard_candles
WHERE instrument_symbol = $1
  AND interval_seconds = $2
  AND open_time >= $3
  AND primary_source_id = ANY($4::text[])
  AND NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
          COALESCE(standard_candles.evidence -> 'candidates', '[]'::jsonb)
      ) AS candidate(value)
      WHERE NOT ((candidate.value ->> 'source_id') = ANY($4::text[]))
  )
ORDER BY open_time ASC
LIMIT $5
"""

_SELECT_STANDARD_LATEST_CANDLES = """
SELECT *
FROM (
    SELECT
        instrument_symbol, 'standard_history' AS source_id,
        instrument_symbol AS provider_symbol, interval_seconds, open_time,
        observed_at, received_at, open, high, low, close, volume,
        evidence || jsonb_build_object(
            'validation_method', validation_method,
            'validated_at', validated_at,
            'revision', revision
        ) AS raw_payload
    FROM standard_candles
    WHERE instrument_symbol = $1 AND interval_seconds = $2
      AND primary_source_id = ANY($3::text[])
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_array_elements(
              COALESCE(standard_candles.evidence -> 'candidates', '[]'::jsonb)
          ) AS candidate(value)
          WHERE NOT ((candidate.value ->> 'source_id') = ANY($3::text[]))
      )
    ORDER BY open_time DESC
    LIMIT $4
) AS recent
ORDER BY open_time ASC
"""

_SELECT_RECENT_QUOTE_CANDLES = """
SELECT *
FROM (
    SELECT
        date_bin($3::int * INTERVAL '1 second', observed_at, to_timestamp(0)) AS open_time,
        (array_agg(provider_symbol ORDER BY observed_at, id))[1] AS provider_symbol,
        (array_agg(last ORDER BY observed_at, id))[1] AS open,
        max(last) AS high,
        min(last) AS low,
        (array_agg(last ORDER BY observed_at DESC, id DESC))[1] AS close,
        max(observed_at) AS observed_at,
        max(received_at) AS received_at
    FROM quote_events
    WHERE instrument_symbol = $1 AND source_id = $2 AND observed_at >= $4
    GROUP BY date_bin($3::int * INTERVAL '1 second', observed_at, to_timestamp(0))
    ORDER BY open_time DESC
    LIMIT $5
) AS recent
ORDER BY open_time
"""

_SELECT_RANGE_QUOTE_CANDLES = """
SELECT
    date_bin($3::int * INTERVAL '1 second', observed_at, to_timestamp(0)) AS open_time,
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
  AND observed_at >= $4
  AND observed_at < $5
GROUP BY date_bin($3::int * INTERVAL '1 second', observed_at, to_timestamp(0))
ORDER BY open_time
LIMIT $6
"""

_SELECT_QUOTE_CANDLES_BEFORE = """
SELECT *
FROM (
    SELECT
        date_bin($3::int * INTERVAL '1 second', observed_at, to_timestamp(0)) AS open_time,
        (array_agg(provider_symbol ORDER BY observed_at, id))[1] AS provider_symbol,
        (array_agg(last ORDER BY observed_at, id))[1] AS open,
        max(last) AS high,
        min(last) AS low,
        (array_agg(last ORDER BY observed_at DESC, id DESC))[1] AS close,
        max(observed_at) AS observed_at,
        max(received_at) AS received_at
    FROM quote_events
    WHERE instrument_symbol = $1 AND source_id = $2 AND observed_at < $4
    GROUP BY date_bin($3::int * INTERVAL '1 second', observed_at, to_timestamp(0))
    ORDER BY open_time DESC
    LIMIT $5
) AS recent
ORDER BY open_time
"""

_SELECT_RECENT_SOURCE_CANDLES = """
SELECT *
FROM (
    SELECT
        instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
        observed_at, received_at, open, high, low, close, volume, raw_payload
    FROM candles
    WHERE instrument_symbol = $1 AND source_id = $2 AND interval_seconds = $3
    ORDER BY open_time DESC
    LIMIT $4
) AS recent
ORDER BY open_time
"""

_SELECT_RANGE_SOURCE_CANDLES = """
SELECT
    instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
    observed_at, received_at, open, high, low, close, volume, raw_payload
FROM candles
WHERE instrument_symbol = $1
  AND source_id = $2
  AND interval_seconds = $3
  AND open_time >= $4
  AND open_time < $5
ORDER BY open_time
LIMIT $6
"""

_SELECT_SOURCE_CANDLES_BEFORE = """
SELECT *
FROM (
    SELECT
        instrument_symbol, source_id, provider_symbol, interval_seconds, open_time,
        observed_at, received_at, open, high, low, close, volume, raw_payload
    FROM candles
    WHERE instrument_symbol = $1 AND source_id = $2 AND interval_seconds = $3
      AND open_time < $4
    ORDER BY open_time DESC
    LIMIT $5
) AS recent
ORDER BY open_time
"""

_SELECT_RECENT_REALTIME_BARS = """
SELECT *
FROM (
    SELECT
        instrument_symbol, realtime_source_id, evidence_channel_id, provider_symbol,
        interval_seconds, open_time, observed_at, received_at,
        open, high, low, close, volume, state, revision, finalized_at, raw_payload
    FROM realtime_bars
    WHERE instrument_symbol = $1
      AND realtime_source_id = $2
      AND interval_seconds = $3
    ORDER BY open_time DESC
    LIMIT $4
) AS recent
ORDER BY open_time
"""

_SELECT_RANGE_REALTIME_BARS = """
SELECT
    instrument_symbol, realtime_source_id, evidence_channel_id, provider_symbol,
    interval_seconds, open_time, observed_at, received_at,
    open, high, low, close, volume, state, revision, finalized_at, raw_payload
FROM realtime_bars
WHERE instrument_symbol = $1
  AND realtime_source_id = $2
  AND interval_seconds = $3
  AND open_time >= $4
  AND open_time < $5
ORDER BY open_time
LIMIT $6
"""

_SELECT_REALTIME_BARS_BEFORE = """
SELECT *
FROM (
    SELECT
        instrument_symbol, realtime_source_id, evidence_channel_id, provider_symbol,
        interval_seconds, open_time, observed_at, received_at,
        open, high, low, close, volume, state, revision, finalized_at, raw_payload
    FROM realtime_bars
    WHERE instrument_symbol = $1
      AND realtime_source_id = $2
      AND interval_seconds = $3
      AND open_time < $4
    ORDER BY open_time DESC
    LIMIT $5
) AS recent
ORDER BY open_time
"""

_LATEST_REALTIME_BAR_MUTATION_ID = """
SELECT COALESCE(max(mutation_id), 0)
FROM realtime_bars
WHERE instrument_symbol = $1
  AND realtime_source_id = $2
  AND interval_seconds = 60
"""

_SELECT_REALTIME_BAR_INPUT_CHANGES = """
SELECT mutation_id, open_time
FROM realtime_bars
WHERE instrument_symbol = $1
  AND realtime_source_id = $2
  AND interval_seconds = 60
  AND mutation_id > $3
  AND mutation_id <= $4
ORDER BY mutation_id
LIMIT $5
"""

_SELECT_PERIOD_BAR_MATERIALIZATION = """
SELECT source_cursor, oldest_bucket_open_time, history_exhausted, processed_mutation_id
FROM period_bar_materializations
WHERE instrument_symbol = $1
  AND realtime_source_id = $2
  AND period_id = $3
  AND materialization_version = $4
"""

_UPSERT_PERIOD_BAR_MATERIALIZATION = """
INSERT INTO period_bar_materializations (
    instrument_symbol, realtime_source_id, period_id, materialization_version,
    source_cursor, oldest_bucket_open_time, history_exhausted, processed_mutation_id
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (
    realtime_source_id, instrument_symbol, period_id, materialization_version
) DO UPDATE SET
    source_cursor = CASE
        WHEN period_bar_materializations.source_cursor IS NULL
            THEN EXCLUDED.source_cursor
        WHEN EXCLUDED.source_cursor IS NULL
            THEN period_bar_materializations.source_cursor
        ELSE LEAST(period_bar_materializations.source_cursor, EXCLUDED.source_cursor)
    END,
    oldest_bucket_open_time = CASE
        WHEN period_bar_materializations.oldest_bucket_open_time IS NULL
            THEN EXCLUDED.oldest_bucket_open_time
        WHEN EXCLUDED.oldest_bucket_open_time IS NULL
            THEN period_bar_materializations.oldest_bucket_open_time
        ELSE LEAST(
            period_bar_materializations.oldest_bucket_open_time,
            EXCLUDED.oldest_bucket_open_time
        )
    END,
    history_exhausted = (
        period_bar_materializations.history_exhausted OR EXCLUDED.history_exhausted
    ),
    processed_mutation_id = GREATEST(
        period_bar_materializations.processed_mutation_id,
        EXCLUDED.processed_mutation_id
    ),
    updated_at = now()
"""

_SELECT_MATERIALIZED_PERIOD_BARS_BEFORE = """
SELECT *
FROM (
    SELECT
        instrument_symbol, realtime_source_id, evidence_channel_id, provider_symbol,
        interval_seconds, open_time, observed_at, received_at,
        open, high, low, close, volume, state, revision, finalized_at, raw_payload
    FROM derived_period_bars
    WHERE instrument_symbol = $1
      AND realtime_source_id = $2
      AND period_id = $3
      AND materialization_version = $4
      AND ($5::timestamptz IS NULL OR open_time < $5)
    ORDER BY open_time DESC
    LIMIT $6
) AS recent
ORDER BY open_time
"""

_UPSERT_MATERIALIZED_PERIOD_BAR = """
INSERT INTO derived_period_bars (
    instrument_symbol, realtime_source_id, period_id, materialization_version,
    evidence_channel_id, provider_symbol, interval_seconds, open_time,
    first_component_open_time, bucket_end, observed_at, received_at,
    open, high, low, close, volume, state, revision, finalized_at, raw_payload
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
    $12, $13, $14, $15, $16, $17, $18, $19, $20, $21::jsonb
)
ON CONFLICT (
    realtime_source_id, instrument_symbol, period_id, materialization_version, open_time
) DO UPDATE SET
    evidence_channel_id = EXCLUDED.evidence_channel_id,
    provider_symbol = EXCLUDED.provider_symbol,
    interval_seconds = EXCLUDED.interval_seconds,
    first_component_open_time = EXCLUDED.first_component_open_time,
    bucket_end = EXCLUDED.bucket_end,
    observed_at = EXCLUDED.observed_at,
    received_at = EXCLUDED.received_at,
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    state = EXCLUDED.state,
    revision = EXCLUDED.revision,
    finalized_at = EXCLUDED.finalized_at,
    raw_payload = EXCLUDED.raw_payload,
    materialized_at = now()
"""

_DELETE_MATERIALIZED_PERIOD_BAR = """
DELETE FROM derived_period_bars
WHERE instrument_symbol = $1
  AND realtime_source_id = $2
  AND period_id = $3
  AND materialization_version = $4
  AND open_time = $5
"""

_SELECT_CANDLE_CACHE_RANGES = """
SELECT range_start, range_end
FROM realtime_candle_cache_ranges
WHERE instrument_symbol = $1
  AND realtime_source_id = $2
  AND interval_seconds = $3
  AND range_end > $4
  AND range_start < $5
ORDER BY range_start, range_end
"""

_UPSERT_CANDLE_CACHE_RANGE = """
INSERT INTO realtime_candle_cache_ranges (
    instrument_symbol, realtime_source_id, upstream_channel_id, provider_symbol,
    interval_seconds, range_start, range_end, row_count
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (
    instrument_symbol, realtime_source_id, interval_seconds, range_start, range_end
) DO UPDATE SET
    upstream_channel_id = EXCLUDED.upstream_channel_id,
    provider_symbol = EXCLUDED.provider_symbol,
    row_count = EXCLUDED.row_count,
    fetched_at = now()
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


def _realtime_bar_values(bar: RealtimeBar) -> tuple[object, ...]:
    source = bar.source
    return (
        bar.instrument.symbol,
        source.provider,
        bar.evidence_channel_id,
        source.provider_symbol,
        int(bar.interval.total_seconds()),
        bar.open_time,
        source.observed_at,
        source.received_at,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.state.value,
        bar.revision,
        bar.finalized_at,
        _payload_json(bar, source.raw_payload),
    )


def _period_bar_payload_time(
    bar: RealtimeBar,
    field: str,
    default: datetime,
) -> datetime:
    value = (bar.source.raw_payload or {}).get(field)
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else default
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"period Bar {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _materialized_period_bar_values(
    bar: RealtimeBar,
    *,
    source_id: str,
    period_id: str,
    materialization_version: str,
) -> tuple[object, ...]:
    source = bar.source
    if source.provider != source_id:
        raise ValueError("materialized period Bar must keep its logical realtime source")
    return (
        bar.instrument.symbol,
        source_id,
        period_id,
        materialization_version,
        bar.evidence_channel_id,
        source.provider_symbol,
        int(bar.interval.total_seconds()),
        bar.open_time,
        _period_bar_payload_time(bar, "bucket_first_open_time", bar.open_time),
        _period_bar_payload_time(bar, "bucket_end", bar.open_time + bar.interval),
        source.observed_at,
        source.received_at,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.state.value,
        bar.revision,
        bar.finalized_at,
        _payload_json(bar, source.raw_payload),
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


def _realtime_bar_from_row(row: Mapping[str, object], instrument: Instrument) -> RealtimeBar:
    return RealtimeBar(
        instrument=instrument,
        interval=timedelta(seconds=int(row["interval_seconds"])),
        open_time=row["open_time"],
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=Decimal(row["volume"]) if row["volume"] is not None else None,
        source=SourceMetadata(
            provider=str(row["realtime_source_id"]),
            provider_symbol=str(row["provider_symbol"]),
            observed_at=row["observed_at"],
            received_at=row["received_at"],
            raw_payload=_raw_payload(row["raw_payload"]),
        ),
        evidence_channel_id=str(row["evidence_channel_id"]),
        state=BarState(str(row["state"])),
        revision=int(row["revision"]),
        finalized_at=row["finalized_at"],
    )


def _quote_from_row(row: Mapping[str, object], instrument: Instrument) -> QuoteSnapshot:
    return QuoteSnapshot(
        instrument=instrument,
        last=Decimal(row["last"]),
        open=Decimal(row["open"]) if row["open"] is not None else None,
        high=Decimal(row["high"]) if row["high"] is not None else None,
        low=Decimal(row["low"]) if row["low"] is not None else None,
        volume=Decimal(row["volume"]) if row["volume"] is not None else None,
        change=Decimal(row["change"]) if row["change"] is not None else None,
        change_percent=(
            Decimal(row["change_percent"]) if row["change_percent"] is not None else None
        ),
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

    async def initialize_watchlist(
        self,
        instruments: Sequence[Instrument],
        *,
        profile_id: str = "default",
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            created = await connection.fetchval(_INITIALIZE_WATCHLIST, profile_id)
            if created is None:
                return
            for position, instrument in enumerate(instruments):
                await connection.execute(
                    _UPSERT_INSTRUMENT,
                    *_instrument_values(instrument),
                )
                await connection.execute(
                    _INSERT_WATCHLIST_ITEM,
                    profile_id,
                    instrument.symbol,
                    position,
                )

    async def load_watchlist_symbols(
        self,
        *,
        profile_id: str = "default",
    ) -> tuple[str, ...]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(_SELECT_WATCHLIST, profile_id)
        return tuple(str(row["instrument_symbol"]) for row in rows)

    async def add_watchlist_instrument(
        self,
        instrument: Instrument,
        *,
        profile_id: str = "default",
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(_INITIALIZE_WATCHLIST, profile_id)
            await connection.execute(
                _UPSERT_INSTRUMENT,
                *_instrument_values(instrument),
            )
            await connection.execute(
                _ADD_WATCHLIST_ITEM,
                profile_id,
                instrument.symbol,
            )

    async def remove_watchlist_instrument(
        self,
        instrument: Instrument,
        *,
        profile_id: str = "default",
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                _REMOVE_WATCHLIST_ITEM,
                profile_id,
                instrument.symbol,
            )

    async def save_quote(self, quote: QuoteSnapshot) -> None:
        pool = self._require_pool()
        values = _quote_values(quote)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(_UPSERT_INSTRUMENT, *_instrument_values(quote.instrument))
            await connection.execute(_UPSERT_SOURCE, quote.source.provider)
            await connection.execute(_INSERT_QUOTE, *values)
            await connection.execute(_UPSERT_LATEST_QUOTE, *values)

    async def load_latest_quote(
        self,
        instrument: Instrument,
        source_id: str,
    ) -> QuoteSnapshot | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _SELECT_LATEST_QUOTE,
                instrument.symbol,
                source_id,
            )
        return _quote_from_row(row, instrument) if row is not None else None

    async def load_quote_event_page(
        self,
        instrument: Instrument,
        *,
        source_ids: tuple[str, ...],
        before_id: int | None = None,
        page_size: int = 2_000,
    ) -> tuple[QuoteSample, ...]:
        if not source_ids:
            return ()
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if before_id is not None and before_id < 1:
            raise ValueError("before_id must be positive")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _SELECT_QUOTE_EVENT_PAGE,
                instrument.symbol,
                source_ids,
                before_id,
                page_size,
            )
        return tuple(
            QuoteSample(
                source_id=str(row["source_id"]),
                channel_id=str(row["source_id"]),
                event_id=f"persisted:{row['id']}",
                instrument=instrument,
                provider_symbol=str(row["provider_symbol"]),
                observed_at=row["observed_at"],
                received_at=row["received_at"],
                value=Decimal(row["last"]),
                storage_id=int(row["id"]),
            )
            for row in rows
        )

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

    async def save_realtime_bars(self, bars: Sequence[RealtimeBar]) -> None:
        if not bars:
            return
        pool = self._require_pool()
        instruments = {bar.instrument.symbol: bar.instrument for bar in bars}
        sources = {
            source_id
            for bar in bars
            for source_id in (bar.source.provider, bar.evidence_channel_id)
        }
        async with pool.acquire() as connection, connection.transaction():
            for instrument in instruments.values():
                await connection.execute(_UPSERT_INSTRUMENT, *_instrument_values(instrument))
            for source_id in sources:
                await connection.execute(_UPSERT_SOURCE, source_id)
            await connection.executemany(
                _UPSERT_REALTIME_BAR,
                [_realtime_bar_values(row) for row in bars],
            )

    async def load_period_bar_materialization(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
    ) -> PeriodBarMaterializationState | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _SELECT_PERIOD_BAR_MATERIALIZATION,
                instrument.symbol,
                source_id,
                period_id,
                materialization_version,
            )
        if row is None:
            return None
        return PeriodBarMaterializationState(
            source_cursor=row["source_cursor"],
            oldest_bucket_open_time=row["oldest_bucket_open_time"],
            history_exhausted=bool(row["history_exhausted"]),
            processed_mutation_id=int(row["processed_mutation_id"]),
        )

    async def save_period_bar_materialization(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
        state: PeriodBarMaterializationState,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(_UPSERT_INSTRUMENT, *_instrument_values(instrument))
            await connection.execute(_UPSERT_SOURCE, source_id)
            await connection.execute(
                _UPSERT_PERIOD_BAR_MATERIALIZATION,
                instrument.symbol,
                source_id,
                period_id,
                materialization_version,
                state.source_cursor,
                state.oldest_bucket_open_time,
                state.history_exhausted,
                state.processed_mutation_id,
            )

    async def latest_realtime_bar_mutation_id(
        self,
        instrument: Instrument,
        *,
        source_id: str,
    ) -> int:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                _LATEST_REALTIME_BAR_MUTATION_ID,
                instrument.symbol,
                source_id,
            )
        return int(value)

    async def load_realtime_bar_input_changes(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        after_mutation_id: int,
        through_mutation_id: int,
        count: int,
    ) -> tuple[PeriodBarInputChange, ...]:
        if after_mutation_id < 0 or through_mutation_id < after_mutation_id:
            raise ValueError("invalid realtime Bar mutation range")
        if not 1 <= count <= 10_000:
            raise ValueError("mutation page count must be between 1 and 10000")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _SELECT_REALTIME_BAR_INPUT_CHANGES,
                instrument.symbol,
                source_id,
                after_mutation_id,
                through_mutation_id,
                count,
            )
        return tuple(
            PeriodBarInputChange(
                mutation_id=int(row["mutation_id"]),
                open_time=row["open_time"],
            )
            for row in rows
        )

    async def load_materialized_period_bars_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
        before: datetime | None,
        count: int,
    ) -> tuple[RealtimeBar, ...]:
        if count < 1:
            raise ValueError("count must be positive")
        if before is not None and (before.tzinfo is None or before.utcoffset() is None):
            raise ValueError("before must be timezone-aware")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _SELECT_MATERIALIZED_PERIOD_BARS_BEFORE,
                instrument.symbol,
                source_id,
                period_id,
                materialization_version,
                before,
                count,
            )
        return tuple(_realtime_bar_from_row(row, instrument) for row in rows)

    async def save_materialized_period_bars(
        self,
        bars: Sequence[RealtimeBar],
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
    ) -> None:
        if not bars:
            return
        pool = self._require_pool()
        instruments = {bar.instrument.symbol: bar.instrument for bar in bars}
        sources = {candidate for bar in bars for candidate in (source_id, bar.evidence_channel_id)}
        values = [
            _materialized_period_bar_values(
                bar,
                source_id=source_id,
                period_id=period_id,
                materialization_version=materialization_version,
            )
            for bar in bars
        ]
        async with pool.acquire() as connection, connection.transaction():
            for instrument in instruments.values():
                await connection.execute(_UPSERT_INSTRUMENT, *_instrument_values(instrument))
            for candidate in sources:
                await connection.execute(_UPSERT_SOURCE, candidate)
            await connection.executemany(_UPSERT_MATERIALIZED_PERIOD_BAR, values)

    async def delete_materialized_period_bar(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
        open_time: datetime,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                _DELETE_MATERIALIZED_PERIOD_BAR,
                instrument.symbol,
                source_id,
                period_id,
                materialization_version,
                open_time,
            )

    async def remove_source_from_standard_history(
        self,
        source_id: str,
    ) -> Mapping[str, int]:
        if not source_id.strip():
            raise ValueError("source_id is required")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _REMOVE_SOURCE_FROM_STANDARD_HISTORY,
                source_id,
            )
        return {
            "standard_rows": int(row["standard_rows"]),
            "validation_rows": int(row["validation_rows"]),
        }

    async def standardize_candles(
        self,
        instrument: Instrument,
        *,
        source_priority: Sequence[str],
        quote_derived_sources: Sequence[str],
        start: datetime,
        end: datetime,
        interval: timedelta = timedelta(minutes=1),
        max_close_deviation_ratio: Decimal = Decimal("0.001"),
    ) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start must be timezone-aware")
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("end must be timezone-aware")
        if end <= start:
            raise ValueError("end must be after start")
        interval_seconds = int(interval.total_seconds())
        if interval_seconds < 1:
            raise ValueError("interval must be positive")
        if not Decimal(0) <= max_close_deviation_ratio <= Decimal("0.1"):
            raise ValueError("max close deviation ratio is outside the supported range")
        priority = tuple(dict.fromkeys((*source_priority, *quote_derived_sources)))
        pool = self._require_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                _STANDARDIZE_CANDLES,
                instrument.symbol,
                interval_seconds,
                priority,
                tuple(quote_derived_sources),
                start,
                end,
                max_close_deviation_ratio,
            )

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
        allowed_sources = tuple(dict.fromkeys((*source_priority, *quote_derived_sources)))
        if not allowed_sources:
            return ()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            if start is None:
                rows = await connection.fetch(
                    _SELECT_STANDARD_LATEST_CANDLES,
                    instrument.symbol,
                    interval_seconds,
                    allowed_sources,
                    count,
                )
            else:
                rows = await connection.fetch(
                    _SELECT_STANDARD_CANDLES_FROM,
                    instrument.symbol,
                    interval_seconds,
                    start,
                    allowed_sources,
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
                "realtime",
                source_id,
            )

    async def load_realtime_bars(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[RealtimeBar, ...]:
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")
        interval_seconds = int(interval.total_seconds())
        if interval_seconds < 1:
            raise ValueError("interval must be positive")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            if start is None:
                rows = await connection.fetch(
                    _SELECT_RECENT_REALTIME_BARS,
                    instrument.symbol,
                    source_id,
                    interval_seconds,
                    count,
                )
            else:
                rows = await connection.fetch(
                    _SELECT_RANGE_REALTIME_BARS,
                    instrument.symbol,
                    source_id,
                    interval_seconds,
                    start,
                    start + interval * count,
                    count,
                )
        return tuple(_realtime_bar_from_row(row, instrument) for row in rows)

    async def load_realtime_bars_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        before: datetime | None = None,
        count: int = 2_000,
    ) -> tuple[RealtimeBar, ...]:
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        pool = self._require_pool()
        interval_seconds = int(interval.total_seconds())
        async with pool.acquire() as connection:
            query = (
                _SELECT_RECENT_REALTIME_BARS
                if before is None
                else _SELECT_REALTIME_BARS_BEFORE
            )
            arguments = (
                (instrument.symbol, source_id, interval_seconds, count)
                if before is None
                else (instrument.symbol, source_id, interval_seconds, before, count)
            )
            rows = await connection.fetch(query, *arguments)
        return tuple(_realtime_bar_from_row(row, instrument) for row in rows)

    async def load_source_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")
        interval_seconds = int(interval.total_seconds())
        if interval_seconds < 1:
            raise ValueError("interval must be positive")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            if start is None:
                rows = await connection.fetch(
                    _SELECT_RECENT_SOURCE_CANDLES,
                    instrument.symbol,
                    source_id,
                    interval_seconds,
                    count,
                )
            else:
                rows = await connection.fetch(
                    _SELECT_RANGE_SOURCE_CANDLES,
                    instrument.symbol,
                    source_id,
                    interval_seconds,
                    start,
                    start + interval * count,
                    count,
                )
        return tuple(_candle_from_row(row, instrument) for row in rows)

    async def load_source_candles_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        before: datetime | None = None,
        count: int = 2_000,
    ) -> tuple[Candle, ...]:
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        pool = self._require_pool()
        interval_seconds = int(interval.total_seconds())
        async with pool.acquire() as connection:
            query = (
                _SELECT_RECENT_SOURCE_CANDLES
                if before is None
                else _SELECT_SOURCE_CANDLES_BEFORE
            )
            arguments = (
                (instrument.symbol, source_id, interval_seconds, count)
                if before is None
                else (instrument.symbol, source_id, interval_seconds, before, count)
            )
            rows = await connection.fetch(query, *arguments)
        return tuple(_candle_from_row(row, instrument) for row in rows)

    async def candle_missing_ranges(
        self,
        instrument: Instrument,
        *,
        realtime_source_id: str,
        start: datetime,
        end: datetime,
        interval: timedelta = timedelta(minutes=1),
    ) -> tuple[tuple[datetime, datetime], ...]:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start must be timezone-aware")
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("end must be timezone-aware")
        if end <= start:
            raise ValueError("end must be after start")
        interval_seconds = int(interval.total_seconds())
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _SELECT_CANDLE_CACHE_RANGES,
                instrument.symbol,
                realtime_source_id,
                interval_seconds,
                start,
                end,
            )
        missing: list[tuple[datetime, datetime]] = []
        covered_until = start
        for row in rows:
            range_start = max(start, row["range_start"])
            range_end = min(end, row["range_end"])
            if range_end <= covered_until:
                continue
            if range_start > covered_until:
                missing.append((covered_until, range_start))
            covered_until = max(covered_until, range_end)
            if covered_until >= end:
                break
        if covered_until < end:
            missing.append((covered_until, end))
        return tuple(missing)

    async def candle_range_is_cached(
        self,
        instrument: Instrument,
        *,
        realtime_source_id: str,
        start: datetime,
        end: datetime,
        interval: timedelta = timedelta(minutes=1),
    ) -> bool:
        return not await self.candle_missing_ranges(
            instrument,
            realtime_source_id=realtime_source_id,
            start=start,
            end=end,
            interval=interval,
        )

    async def record_candle_cache_range(
        self,
        instrument: Instrument,
        *,
        realtime_source_id: str,
        upstream_channel_id: str,
        provider_symbol: str,
        start: datetime,
        end: datetime,
        row_count: int,
        interval: timedelta = timedelta(minutes=1),
    ) -> None:
        if end <= start:
            raise ValueError("end must be after start")
        if row_count < 0:
            raise ValueError("row_count cannot be negative")
        interval_seconds = int(interval.total_seconds())
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(_UPSERT_INSTRUMENT, *_instrument_values(instrument))
            await connection.execute(_UPSERT_SOURCE, realtime_source_id)
            await connection.execute(_UPSERT_SOURCE, upstream_channel_id)
            await connection.execute(
                _UPSERT_CANDLE_CACHE_RANGE,
                instrument.symbol,
                realtime_source_id,
                upstream_channel_id,
                provider_symbol,
                interval_seconds,
                start,
                end,
                row_count,
            )

    async def load_quote_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")
        interval_seconds = int(interval.total_seconds())
        if interval_seconds <= 0:
            raise ValueError("interval must be positive")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            if start is None:
                cutoff = datetime.now(UTC) - timedelta(
                    seconds=max(180 * 60, count * interval_seconds * 3)
                )
                rows = await connection.fetch(
                    _SELECT_RECENT_QUOTE_CANDLES,
                    instrument.symbol,
                    source_id,
                    interval_seconds,
                    cutoff,
                    count,
                )
            else:
                rows = await connection.fetch(
                    _SELECT_RANGE_QUOTE_CANDLES,
                    instrument.symbol,
                    source_id,
                    interval_seconds,
                    start,
                    start + interval * count,
                    count,
                )
        return tuple(
            Candle(
                instrument=instrument,
                interval=interval,
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

    async def load_quote_candles_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        before: datetime | None = None,
        count: int = 2_000,
    ) -> tuple[Candle, ...]:
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        interval_seconds = int(interval.total_seconds())
        if interval_seconds <= 0:
            raise ValueError("interval must be positive")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            if before is None:
                cutoff = datetime.now(UTC) - timedelta(
                    seconds=max(180 * 60, count * interval_seconds * 3)
                )
                rows = await connection.fetch(
                    _SELECT_RECENT_QUOTE_CANDLES,
                    instrument.symbol,
                    source_id,
                    interval_seconds,
                    cutoff,
                    count,
                )
            else:
                rows = await connection.fetch(
                    _SELECT_QUOTE_CANDLES_BEFORE,
                    instrument.symbol,
                    source_id,
                    interval_seconds,
                    before,
                    count,
                )
        return tuple(
            Candle(
                instrument=instrument,
                interval=interval,
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
