SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL,
    base_asset TEXT,
    quote_asset TEXT,
    venue TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
    capability TEXT NOT NULL CHECK (
        capability IN ('quote', 'candles', 'catalog', 'news', 'calendar')
    ),
    source_id TEXT NOT NULL REFERENCES market_sources(source_id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_symbol, capability)
);

CREATE INDEX IF NOT EXISTS ix_instrument_source_routes_source
    ON instrument_source_routes (source_id, capability);

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
"""
