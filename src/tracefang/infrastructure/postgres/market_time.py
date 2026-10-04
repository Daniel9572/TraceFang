"""Preserve raw evidence while retiring projections made with the wrong clock."""

MARKET_TIME_SCHEMA_SQL = """
CREATE OR REPLACE VIEW normalized_quote_events AS
SELECT id, instrument_symbol, source_id, event_id, provider_symbol,
    CASE WHEN raw_payload ->> 'bar_clock' = 'provider_frame.received_at'
              AND raw_payload ->> 'wire_observed_at' IS NOT NULL
         THEN (raw_payload ->> 'wire_observed_at')::timestamptz
         ELSE observed_at END AS observed_at,
    received_at, persisted_at, last, open, high, low, volume, change, change_percent,
    raw_payload
FROM quote_events;

CREATE TABLE IF NOT EXISTS market_data_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM market_data_migrations WHERE migration_id = 'source-time-v1'
    ) THEN
        -- This is a recoverable quarantine, not destruction of recorded prices.
        CREATE TABLE IF NOT EXISTS invalid_quote_time_bars
            (LIKE realtime_bars INCLUDING ALL);
        WITH moved AS (
            DELETE FROM realtime_bars
            WHERE realtime_source_id = 'tonghuashun_futures'
              AND raw_payload ->> 'derivation' = 'quote_event'
              AND raw_payload ->> 'quote_time_basis' IS DISTINCT FROM 'source'
            RETURNING *
        )
        INSERT INTO invalid_quote_time_bars SELECT * FROM moved;

        -- Period pages are disposable projections of the corrected minute facts.
        DELETE FROM derived_period_bars
        WHERE realtime_source_id = 'tonghuashun_futures';
        DELETE FROM period_bar_materializations
        WHERE realtime_source_id = 'tonghuashun_futures';

        UPDATE latest_quotes
        SET observed_at = (raw_payload ->> 'wire_observed_at')::timestamptz,
            raw_payload = raw_payload || jsonb_build_object(
                'bar_clock', 'source.observed_at', 'timestamp_precision_seconds', 60)
        WHERE raw_payload ->> 'bar_clock' = 'provider_frame.received_at'
          AND raw_payload ->> 'wire_observed_at' IS NOT NULL;

        -- The old annual-file adapter wrongly confirmed the not-yet-published tail.
        UPDATE realtime_bar_series_state
        SET authoritative_through = LEAST(
                authoritative_through, latest_authoritative_open_time + interval '1 minute'),
            tail_checked_at = NULL, tail_checked_through = NULL
        WHERE realtime_source_id = 'tonghuashun_futures'
          AND latest_authoritative_open_time IS NOT NULL;
        DELETE FROM realtime_candle_cache_ranges AS ranges
        USING realtime_bar_series_state AS series
        WHERE ranges.realtime_source_id = 'tonghuashun_futures'
          AND ranges.realtime_source_id = series.realtime_source_id
          AND ranges.instrument_symbol = series.instrument_symbol
          AND ranges.interval_seconds = series.interval_seconds
          AND ranges.range_end > series.authoritative_through;

        INSERT INTO market_data_migrations (migration_id) VALUES ('source-time-v1');
    END IF;
END $$;
"""
