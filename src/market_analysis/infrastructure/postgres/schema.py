SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL,
    base_asset TEXT,
    quote_asset TEXT,
    venue TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS watchlists (
    profile_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    profile_id TEXT NOT NULL REFERENCES watchlists(profile_id) ON DELETE CASCADE,
    instrument_symbol TEXT NOT NULL REFERENCES instruments(symbol) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, instrument_symbol)
);

CREATE INDEX IF NOT EXISTS ix_watchlist_items_order
    ON watchlist_items (profile_id, position, added_at);

CREATE TABLE IF NOT EXISTS market_sources (
    source_id TEXT PRIMARY KEY,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quote_events (
    id BIGSERIAL PRIMARY KEY,
    instrument_symbol TEXT NOT NULL REFERENCES instruments(symbol),
    source_id TEXT NOT NULL REFERENCES market_sources(source_id),
    provider_symbol TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    persisted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last NUMERIC(38, 18) NOT NULL,
    open NUMERIC(38, 18),
    high NUMERIC(38, 18),
    low NUMERIC(38, 18),
    volume NUMERIC(38, 18),
    change NUMERIC(38, 18),
    change_percent NUMERIC(38, 18),
    raw_payload JSONB NOT NULL
);

ALTER TABLE quote_events DROP CONSTRAINT IF EXISTS uq_quote_event;

CREATE UNIQUE INDEX IF NOT EXISTS uq_quote_event_received
    ON quote_events (source_id, provider_symbol, received_at);

CREATE INDEX IF NOT EXISTS ix_quote_events_instrument_observed
    ON quote_events (instrument_symbol, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_quote_events_source_observed
    ON quote_events (source_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_quote_events_timeline_cursor
    ON quote_events (instrument_symbol, source_id, id DESC);

CREATE TABLE IF NOT EXISTS latest_quotes (
    instrument_symbol TEXT NOT NULL REFERENCES instruments(symbol),
    source_id TEXT NOT NULL REFERENCES market_sources(source_id),
    provider_symbol TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    last NUMERIC(38, 18) NOT NULL,
    open NUMERIC(38, 18),
    high NUMERIC(38, 18),
    low NUMERIC(38, 18),
    volume NUMERIC(38, 18),
    change NUMERIC(38, 18),
    change_percent NUMERIC(38, 18),
    raw_payload JSONB NOT NULL,
    PRIMARY KEY (instrument_symbol, source_id)
);

CREATE TABLE IF NOT EXISTS instrument_source_routes (
    instrument_symbol TEXT NOT NULL REFERENCES instruments(symbol) ON DELETE CASCADE,
    capability TEXT NOT NULL CHECK (capability = 'realtime'),
    source_id TEXT NOT NULL REFERENCES market_sources(source_id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_symbol, capability)
);

CREATE INDEX IF NOT EXISTS ix_instrument_source_routes_source
    ON instrument_source_routes (source_id, capability);

-- Releases before the realtime-source boundary stored one route per capability.
-- Preserve the former quote binding as the contract's complete realtime-source binding.
ALTER TABLE instrument_source_routes
    DROP CONSTRAINT IF EXISTS instrument_source_routes_capability_check;

UPDATE instrument_source_routes
SET capability = 'realtime'
WHERE capability = 'quote';

DELETE FROM instrument_source_routes
WHERE capability <> 'realtime';

ALTER TABLE instrument_source_routes
    ADD CONSTRAINT instrument_source_routes_capability_check
    CHECK (capability = 'realtime');

CREATE UNIQUE INDEX IF NOT EXISTS ux_instrument_source_routes_instrument
    ON instrument_source_routes (instrument_symbol);

CREATE TABLE IF NOT EXISTS candles (
    instrument_symbol TEXT NOT NULL REFERENCES instruments(symbol),
    source_id TEXT NOT NULL REFERENCES market_sources(source_id),
    provider_symbol TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    open_time TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    persisted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    open NUMERIC(38, 18) NOT NULL,
    high NUMERIC(38, 18) NOT NULL,
    low NUMERIC(38, 18) NOT NULL,
    close NUMERIC(38, 18) NOT NULL,
    volume NUMERIC(38, 18),
    raw_payload JSONB NOT NULL,
    PRIMARY KEY (source_id, provider_symbol, interval_seconds, open_time)
);

CREATE INDEX IF NOT EXISTS ix_candles_instrument_interval_time
    ON candles (instrument_symbol, interval_seconds, open_time DESC);

CREATE TABLE IF NOT EXISTS realtime_bars (
    instrument_symbol TEXT NOT NULL REFERENCES instruments(symbol),
    realtime_source_id TEXT NOT NULL REFERENCES market_sources(source_id),
    evidence_channel_id TEXT NOT NULL REFERENCES market_sources(source_id),
    provider_symbol TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    open_time TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    persisted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    open NUMERIC(38, 18) NOT NULL,
    high NUMERIC(38, 18) NOT NULL,
    low NUMERIC(38, 18) NOT NULL,
    close NUMERIC(38, 18) NOT NULL,
    volume NUMERIC(38, 18),
    state TEXT NOT NULL CHECK (
        state IN ('provisional_quote', 'provisional_authoritative', 'final')
    ),
    revision INTEGER NOT NULL CHECK (revision > 0),
    finalized_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL,
    CHECK (
        (state = 'final' AND finalized_at IS NOT NULL)
        OR (state <> 'final' AND finalized_at IS NULL)
    ),
    PRIMARY KEY (realtime_source_id, instrument_symbol, interval_seconds, open_time)
);

CREATE INDEX IF NOT EXISTS ix_realtime_bars_source_instrument_time
    ON realtime_bars (realtime_source_id, instrument_symbol, interval_seconds, open_time DESC);

CREATE TABLE IF NOT EXISTS realtime_candle_cache_ranges (
    instrument_symbol TEXT NOT NULL REFERENCES instruments(symbol) ON DELETE CASCADE,
    realtime_source_id TEXT NOT NULL REFERENCES market_sources(source_id),
    upstream_channel_id TEXT NOT NULL REFERENCES market_sources(source_id),
    provider_symbol TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (range_end > range_start),
    PRIMARY KEY (
        instrument_symbol,
        realtime_source_id,
        interval_seconds,
        range_start,
        range_end
    )
);

CREATE INDEX IF NOT EXISTS ix_realtime_candle_cache_coverage
    ON realtime_candle_cache_ranges (
        instrument_symbol,
        realtime_source_id,
        interval_seconds,
        range_start,
        range_end
    );

CREATE TABLE IF NOT EXISTS candle_validation_results (
    instrument_symbol TEXT NOT NULL REFERENCES instruments(symbol),
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    open_time TIMESTAMPTZ NOT NULL,
    validation_state TEXT NOT NULL CHECK (validation_state IN ('accepted', 'rejected')),
    source_count INTEGER NOT NULL CHECK (source_count > 0),
    max_close_deviation_ratio NUMERIC(38, 18) NOT NULL,
    evidence JSONB NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_symbol, interval_seconds, open_time)
);

CREATE TABLE IF NOT EXISTS standard_candles (
    instrument_symbol TEXT NOT NULL REFERENCES instruments(symbol),
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    open_time TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    validated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    open NUMERIC(38, 18) NOT NULL,
    high NUMERIC(38, 18) NOT NULL,
    low NUMERIC(38, 18) NOT NULL,
    close NUMERIC(38, 18) NOT NULL,
    volume NUMERIC(38, 18),
    primary_source_id TEXT NOT NULL REFERENCES market_sources(source_id),
    source_count INTEGER NOT NULL CHECK (source_count > 0),
    validation_method TEXT NOT NULL,
    max_close_deviation_ratio NUMERIC(38, 18) NOT NULL,
    evidence JSONB NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    CHECK (low <= open AND open <= high),
    CHECK (low <= close AND close <= high),
    PRIMARY KEY (instrument_symbol, interval_seconds, open_time)
);

CREATE INDEX IF NOT EXISTS ix_standard_candles_instrument_time
    ON standard_candles (instrument_symbol, interval_seconds, open_time DESC);
"""
