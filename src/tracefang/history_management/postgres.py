from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

import asyncpg

from tracefang.history_management.models import (
    AdmissionDecision,
    AdmissionTarget,
    BarObservation,
    CanonicalSegment,
    CrossValidationEvidence,
    DatasetState,
    HistoricalDatasetBundle,
    HistoricalRecordKind,
    QuoteObservation,
    ValidationReport,
    ValidationStatus,
)
from tracefang.infrastructure.postgres.settings import PostgresSettings

HISTORY_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS history;

CREATE TABLE IF NOT EXISTS history.datasets (
    dataset_id TEXT PRIMARY KEY,
    provider_family TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    independence_group TEXT NOT NULL,
    instrument_symbol TEXT NOT NULL,
    underlying TEXT NOT NULL,
    quote_currency TEXT,
    instrument_type TEXT NOT NULL,
    venue TEXT NOT NULL,
    contract_code TEXT,
    record_kind TEXT NOT NULL CHECK (
        record_kind IN ('quote_event', 'bar_observation')
    ),
    quote_fields TEXT[] NOT NULL DEFAULT '{}',
    bar_price_basis TEXT,
    bar_interval_microseconds BIGINT,
    source_timezone TEXT NOT NULL,
    normalized_timezone TEXT NOT NULL CHECK (normalized_timezone = 'UTC'),
    timestamp_semantics TEXT NOT NULL,
    timestamp_resolution_microseconds BIGINT NOT NULL CHECK (
        timestamp_resolution_microseconds > 0
    ),
    storage_price_scale SMALLINT NOT NULL CHECK (
        storage_price_scale BETWEEN 0 AND 18
    ),
    effective_price_quantum NUMERIC(38, 18) NOT NULL CHECK (
        effective_price_quantum > 0
    ),
    source_uri TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    metadata JSONB NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'registered', 'ingested', 'validated_candidate', 'quarantined', 'trusted'
        )
    ),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (
            record_kind = 'quote_event'
            AND cardinality(quote_fields) > 0
            AND bar_price_basis IS NULL
            AND bar_interval_microseconds IS NULL
        )
        OR
        (
            record_kind = 'bar_observation'
            AND cardinality(quote_fields) = 0
            AND bar_price_basis IS NOT NULL
            AND bar_interval_microseconds > 0
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_history_datasets_instrument_state
    ON history.datasets (instrument_symbol, record_kind, state);
CREATE INDEX IF NOT EXISTS ix_history_datasets_channel
    ON history.datasets (provider_family, channel_id, feed_id);

CREATE TABLE IF NOT EXISTS history.artifacts (
    dataset_id TEXT NOT NULL REFERENCES history.datasets(dataset_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    expected_bytes BIGINT NOT NULL CHECK (expected_bytes >= 0),
    actual_bytes BIGINT NOT NULL CHECK (actual_bytes >= 0),
    expected_sha256 TEXT NOT NULL CHECK (expected_sha256 ~ '^[0-9a-f]{64}$'),
    actual_sha256 TEXT NOT NULL CHECK (actual_sha256 ~ '^[0-9a-f]{64}$'),
    publisher_authentication TEXT,
    integrity_verified BOOLEAN NOT NULL,
    PRIMARY KEY (dataset_id, artifact_id)
);

CREATE TABLE IF NOT EXISTS history.quote_events (
    dataset_id TEXT NOT NULL REFERENCES history.datasets(dataset_id) ON DELETE RESTRICT,
    event_index BIGINT NOT NULL CHECK (event_index >= 0),
    source_observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    bid NUMERIC(38, 18),
    ask NUMERIC(38, 18),
    last NUMERIC(38, 18),
    source_event_id TEXT,
    raw_payload_sha256 TEXT CHECK (
        raw_payload_sha256 IS NULL OR raw_payload_sha256 ~ '^[0-9a-f]{64}$'
    ),
    PRIMARY KEY (dataset_id, event_index),
    CHECK (bid IS NOT NULL OR ask IS NOT NULL OR last IS NOT NULL),
    CHECK (bid IS NULL OR bid > 0),
    CHECK (ask IS NULL OR ask > 0),
    CHECK (last IS NULL OR last > 0),
    CHECK (bid IS NULL OR ask IS NULL OR bid <= ask)
);

CREATE INDEX IF NOT EXISTS ix_history_quote_events_time
    ON history.quote_events (dataset_id, source_observed_at, event_index);

CREATE TABLE IF NOT EXISTS history.bar_observations (
    dataset_id TEXT NOT NULL REFERENCES history.datasets(dataset_id) ON DELETE RESTRICT,
    open_time TIMESTAMPTZ NOT NULL,
    interval_microseconds BIGINT NOT NULL CHECK (interval_microseconds > 0),
    open NUMERIC(38, 18) NOT NULL CHECK (open > 0),
    high NUMERIC(38, 18) NOT NULL CHECK (high > 0),
    low NUMERIC(38, 18) NOT NULL CHECK (low > 0),
    close NUMERIC(38, 18) NOT NULL CHECK (close > 0),
    volume NUMERIC(38, 18) CHECK (volume IS NULL OR volume >= 0),
    source_row_number BIGINT CHECK (
        source_row_number IS NULL OR source_row_number > 0
    ),
    raw_payload_sha256 TEXT CHECK (
        raw_payload_sha256 IS NULL OR raw_payload_sha256 ~ '^[0-9a-f]{64}$'
    ),
    PRIMARY KEY (dataset_id, open_time),
    CHECK (low <= open AND open <= high),
    CHECK (low <= close AND close <= high)
);

CREATE INDEX IF NOT EXISTS ix_history_bar_observations_time
    ON history.bar_observations (dataset_id, open_time);

CREATE TABLE IF NOT EXISTS history.validation_runs (
    validation_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES history.datasets(dataset_id) ON DELETE RESTRICT,
    validator_version TEXT NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pass', 'pass_with_warnings', 'fail')),
    error_count INTEGER NOT NULL CHECK (error_count >= 0),
    warning_count INTEGER NOT NULL CHECK (warning_count >= 0),
    metrics JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS history.validation_findings (
    validation_id TEXT NOT NULL REFERENCES history.validation_runs(validation_id)
        ON DELETE RESTRICT,
    finding_index INTEGER NOT NULL CHECK (finding_index >= 0),
    code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    message TEXT NOT NULL,
    record_key TEXT,
    details JSONB NOT NULL,
    PRIMARY KEY (validation_id, finding_index)
);

CREATE TABLE IF NOT EXISTS history.cross_validations (
    evidence_id TEXT PRIMARY KEY,
    left_dataset_id TEXT NOT NULL REFERENCES history.datasets(dataset_id)
        ON DELETE RESTRICT,
    right_dataset_id TEXT NOT NULL REFERENCES history.datasets(dataset_id)
        ON DELETE RESTRICT,
    compatibility_level TEXT NOT NULL CHECK (
        compatibility_level IN ('exact', 'sanity_only', 'incompatible')
    ),
    independent BOOLEAN NOT NULL,
    method TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    matched_records INTEGER NOT NULL CHECK (matched_records >= 0),
    checked_at TIMESTAMPTZ NOT NULL,
    metrics JSONB NOT NULL,
    CHECK (left_dataset_id <> right_dataset_id)
);

CREATE TABLE IF NOT EXISTS history.admission_decisions (
    decision_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES history.datasets(dataset_id) ON DELETE RESTRICT,
    target TEXT NOT NULL CHECK (
        target IN ('trusted_quote_history', 'trusted_bar_reference')
    ),
    resulting_state TEXT NOT NULL CHECK (
        resulting_state IN (
            'registered', 'ingested', 'validated_candidate', 'quarantined', 'trusted'
        )
    ),
    decided_at TIMESTAMPTZ NOT NULL,
    policy_version TEXT NOT NULL,
    blockers TEXT[] NOT NULL,
    accepted_evidence_ids TEXT[] NOT NULL
);

CREATE TABLE IF NOT EXISTS history.canonical_segments (
    segment_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES history.datasets(dataset_id) ON DELETE RESTRICT,
    admission_decision_id TEXT NOT NULL REFERENCES history.admission_decisions(decision_id)
        ON DELETE RESTRICT,
    record_kind TEXT NOT NULL CHECK (
        record_kind IN ('quote_event', 'bar_observation')
    ),
    instrument_symbol TEXT NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    admission_policy_version TEXT NOT NULL,
    selection_reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_at > start_at)
);

CREATE INDEX IF NOT EXISTS ix_history_canonical_segments_range
    ON history.canonical_segments (instrument_symbol, record_kind, start_at, end_at);

CREATE OR REPLACE VIEW history.trusted_quote_events AS
SELECT
    segment.segment_id,
    quote.dataset_id,
    dataset.provider_family,
    dataset.channel_id,
    dataset.feed_id,
    dataset.instrument_symbol,
    quote.event_index,
    quote.source_observed_at,
    quote.received_at,
    quote.bid,
    quote.ask,
    quote.last,
    quote.source_event_id,
    quote.raw_payload_sha256
FROM history.canonical_segments AS segment
JOIN history.datasets AS dataset
  ON dataset.dataset_id = segment.dataset_id
 AND dataset.state = 'trusted'
 AND dataset.record_kind = 'quote_event'
JOIN history.quote_events AS quote
  ON quote.dataset_id = segment.dataset_id
 AND quote.source_observed_at >= segment.start_at
 AND quote.source_observed_at < segment.end_at
WHERE segment.record_kind = 'quote_event';

CREATE OR REPLACE VIEW history.trusted_bar_observations AS
SELECT
    segment.segment_id,
    bar.dataset_id,
    dataset.provider_family,
    dataset.channel_id,
    dataset.feed_id,
    dataset.instrument_symbol,
    dataset.bar_price_basis,
    bar.open_time,
    bar.interval_microseconds,
    bar.open,
    bar.high,
    bar.low,
    bar.close,
    bar.volume,
    bar.source_row_number,
    bar.raw_payload_sha256
FROM history.canonical_segments AS segment
JOIN history.datasets AS dataset
  ON dataset.dataset_id = segment.dataset_id
 AND dataset.state = 'trusted'
 AND dataset.record_kind = 'bar_observation'
JOIN history.bar_observations AS bar
  ON bar.dataset_id = segment.dataset_id
 AND bar.open_time >= segment.start_at
 AND bar.open_time < segment.end_at
WHERE segment.record_kind = 'bar_observation';
"""

_INSERT_DATASET = """
INSERT INTO history.datasets (
    dataset_id, provider_family, channel_id, feed_id, independence_group,
    instrument_symbol, underlying, quote_currency, instrument_type, venue,
    contract_code, record_kind, quote_fields, bar_price_basis,
    bar_interval_microseconds, source_timezone, normalized_timezone,
    timestamp_semantics, timestamp_resolution_microseconds, storage_price_scale,
    effective_price_quantum, source_uri, parser_version, metadata, state
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
    $16, $17, $18, $19, $20, $21, $22, $23, $24::jsonb, 'registered'
)
ON CONFLICT (dataset_id) DO NOTHING
RETURNING dataset_id
"""

_INSERT_ARTIFACT = """
INSERT INTO history.artifacts (
    dataset_id, artifact_id, relative_path, expected_bytes, actual_bytes,
    expected_sha256, actual_sha256, publisher_authentication, integrity_verified
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""

_INSERT_VALIDATION = """
INSERT INTO history.validation_runs (
    validation_id, dataset_id, validator_version, checked_at, status,
    error_count, warning_count, metrics
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
"""

_INSERT_FINDING = """
INSERT INTO history.validation_findings (
    validation_id, finding_index, code, severity, message, record_key, details
)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
"""

_UPDATE_DATASET_STATE = """
UPDATE history.datasets
SET state = $2, updated_at = now()
WHERE dataset_id = $1
"""

_INSERT_CROSS_VALIDATION = """
INSERT INTO history.cross_validations (
    evidence_id, left_dataset_id, right_dataset_id, compatibility_level,
    independent, method, passed, matched_records, checked_at, metrics
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
ON CONFLICT (evidence_id) DO NOTHING
"""

_INSERT_ADMISSION = """
INSERT INTO history.admission_decisions (
    decision_id, dataset_id, target, resulting_state, decided_at,
    policy_version, blockers, accepted_evidence_ids
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""

_SELECT_DATASET_FOR_ADMISSION = """
SELECT state, record_kind
FROM history.datasets
WHERE dataset_id = $1
FOR UPDATE
"""

_SELECT_ACCEPTED_EVIDENCE = """
SELECT evidence_id
FROM history.cross_validations
WHERE evidence_id = ANY($1::text[])
  AND (left_dataset_id = $2 OR right_dataset_id = $2)
  AND compatibility_level = 'exact'
  AND independent
  AND passed
"""

_SELECT_SEGMENT_LINEAGE = """
SELECT
    dataset.state,
    dataset.record_kind,
    dataset.instrument_symbol,
    decision.resulting_state,
    decision.dataset_id AS decision_dataset_id
FROM history.datasets AS dataset
JOIN history.admission_decisions AS decision
  ON decision.decision_id = $2
WHERE dataset.dataset_id = $1
"""

_SELECT_OVERLAPPING_SEGMENT = """
SELECT segment_id
FROM history.canonical_segments
WHERE instrument_symbol = $1
  AND record_kind = $2
  AND start_at < $4
  AND end_at > $3
LIMIT 1
"""

_INSERT_CANONICAL_SEGMENT = """
INSERT INTO history.canonical_segments (
    segment_id, dataset_id, admission_decision_id, record_kind,
    instrument_symbol, start_at, end_at, admission_policy_version, selection_reason
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""

_SELECT_TRUSTED_QUOTES = """
SELECT
    dataset_id, event_index, source_observed_at, received_at,
    bid, ask, last, source_event_id, raw_payload_sha256
FROM history.trusted_quote_events
WHERE instrument_symbol = $1
  AND source_observed_at >= $2
  AND source_observed_at < $3
ORDER BY source_observed_at, received_at, dataset_id, event_index
"""

_SELECT_TRUSTED_BARS = """
SELECT
    dataset_id, open_time, interval_microseconds, open, high, low, close,
    volume, source_row_number, raw_payload_sha256
FROM history.trusted_bar_observations
WHERE instrument_symbol = $1
  AND open_time >= $2
  AND open_time < $3
ORDER BY open_time, dataset_id
"""


class HistoricalDatasetAlreadyExistsError(RuntimeError):
    pass


def _microseconds(value: timedelta | None) -> int | None:
    if value is None:
        return None
    return value // timedelta(microseconds=1)


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return _microseconds(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"cannot JSON encode {type(value).__name__}")


def _json(value: object) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class HistoricalPostgresRepository:
    """Stores validated history locally; it has no market-data provider dependency."""

    def __init__(self, settings: PostgresSettings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

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
                await connection.execute(HISTORY_SCHEMA_SQL)
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
            raise RuntimeError("historical PostgreSQL repository is not connected")
        return self._pool

    async def save_validated_candidate(
        self,
        bundle: HistoricalDatasetBundle,
        report: ValidationReport,
    ) -> DatasetState:
        if report.dataset_id != bundle.descriptor.dataset_id:
            raise ValueError("validation report belongs to another dataset")
        descriptor = bundle.descriptor
        resulting_state = (
            DatasetState.QUARANTINED
            if report.status is ValidationStatus.FAIL
            else DatasetState.VALIDATED_CANDIDATE
        )
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            inserted = await connection.fetchval(
                _INSERT_DATASET,
                descriptor.dataset_id,
                descriptor.provider_family,
                descriptor.channel_id,
                descriptor.feed_id,
                descriptor.independence_group,
                descriptor.instrument_symbol,
                descriptor.underlying,
                descriptor.quote_currency,
                descriptor.instrument_type.value,
                descriptor.venue,
                descriptor.contract_code,
                descriptor.record_kind.value,
                sorted(field.value for field in descriptor.quote_fields),
                descriptor.bar_price_basis.value if descriptor.bar_price_basis else None,
                _microseconds(descriptor.bar_interval),
                descriptor.source_timezone,
                descriptor.normalized_timezone,
                descriptor.timestamp_semantics.value,
                _microseconds(descriptor.timestamp_resolution),
                descriptor.storage_price_scale,
                descriptor.effective_price_quantum,
                descriptor.source_uri,
                descriptor.parser_version,
                _json(descriptor.metadata),
            )
            if inserted is None:
                raise HistoricalDatasetAlreadyExistsError(descriptor.dataset_id)
            if bundle.artifacts:
                await connection.executemany(
                    _INSERT_ARTIFACT,
                    [
                        (
                            descriptor.dataset_id,
                            artifact.artifact_id,
                            artifact.relative_path,
                            artifact.expected_bytes,
                            artifact.actual_bytes,
                            artifact.expected_sha256,
                            artifact.actual_sha256,
                            artifact.publisher_authentication,
                            artifact.integrity_verified,
                        )
                        for artifact in bundle.artifacts
                    ],
                )
            await self._copy_records(connection, bundle)
            await self._save_validation(connection, report)
            await connection.execute(
                _UPDATE_DATASET_STATE,
                descriptor.dataset_id,
                resulting_state.value,
            )
        return resulting_state

    @staticmethod
    async def _copy_records(
        connection: asyncpg.Connection,
        bundle: HistoricalDatasetBundle,
    ) -> None:
        if bundle.quote_events:
            await connection.copy_records_to_table(
                "quote_events",
                schema_name="history",
                columns=(
                    "dataset_id",
                    "event_index",
                    "source_observed_at",
                    "received_at",
                    "bid",
                    "ask",
                    "last",
                    "source_event_id",
                    "raw_payload_sha256",
                ),
                records=[
                    (
                        record.dataset_id,
                        record.event_index,
                        record.source_observed_at,
                        record.received_at,
                        record.bid,
                        record.ask,
                        record.last,
                        record.source_event_id,
                        record.raw_payload_sha256,
                    )
                    for record in bundle.quote_events
                ],
            )
        if bundle.bars:
            await connection.copy_records_to_table(
                "bar_observations",
                schema_name="history",
                columns=(
                    "dataset_id",
                    "open_time",
                    "interval_microseconds",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "source_row_number",
                    "raw_payload_sha256",
                ),
                records=[
                    (
                        record.dataset_id,
                        record.open_time,
                        _microseconds(record.interval),
                        record.open,
                        record.high,
                        record.low,
                        record.close,
                        record.volume,
                        record.source_row_number,
                        record.raw_payload_sha256,
                    )
                    for record in bundle.bars
                ],
            )

    @staticmethod
    async def _save_validation(
        connection: asyncpg.Connection,
        report: ValidationReport,
    ) -> None:
        await connection.execute(
            _INSERT_VALIDATION,
            report.validation_id,
            report.dataset_id,
            report.validator_version,
            report.checked_at,
            report.status.value,
            report.error_count,
            report.warning_count,
            _json(report.metrics),
        )
        if report.findings:
            await connection.executemany(
                _INSERT_FINDING,
                [
                    (
                        report.validation_id,
                        index,
                        finding.code,
                        finding.severity.value,
                        finding.message,
                        finding.record_key,
                        _json(finding.details),
                    )
                    for index, finding in enumerate(report.findings)
                ],
            )

    async def save_cross_validation(self, evidence: CrossValidationEvidence) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                _INSERT_CROSS_VALIDATION,
                evidence.evidence_id,
                evidence.left_dataset_id,
                evidence.right_dataset_id,
                evidence.level.value,
                evidence.independent,
                evidence.method,
                evidence.passed,
                evidence.matched_records,
                evidence.checked_at,
                _json(evidence.metrics),
            )

    async def apply_admission(self, decision: AdmissionDecision) -> None:
        if decision.accepted and decision.blockers:
            raise ValueError("a trusted admission decision cannot contain blockers")
        expected_kind = (
            HistoricalRecordKind.QUOTE_EVENT.value
            if decision.target is AdmissionTarget.TRUSTED_QUOTE_HISTORY
            else HistoricalRecordKind.BAR_OBSERVATION.value
        )
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            dataset = await connection.fetchrow(
                _SELECT_DATASET_FOR_ADMISSION,
                decision.dataset_id,
            )
            if dataset is None:
                raise KeyError(f"unknown historical dataset: {decision.dataset_id}")
            if dataset["record_kind"] != expected_kind:
                raise ValueError("admission target does not match persisted record kind")
            if decision.accepted:
                if dataset["state"] != DatasetState.VALIDATED_CANDIDATE.value:
                    raise ValueError("only a validated candidate can become trusted")
                if not decision.accepted_evidence_ids:
                    raise ValueError("trusted admission requires exact independent evidence")
                rows = await connection.fetch(
                    _SELECT_ACCEPTED_EVIDENCE,
                    list(decision.accepted_evidence_ids),
                    decision.dataset_id,
                )
                persisted_ids = {str(row["evidence_id"]) for row in rows}
                if persisted_ids != set(decision.accepted_evidence_ids):
                    raise ValueError("admission evidence is missing or not independently exact")
            await connection.execute(
                _INSERT_ADMISSION,
                decision.decision_id,
                decision.dataset_id,
                decision.target.value,
                decision.resulting_state.value,
                decision.decided_at,
                decision.policy_version,
                list(decision.blockers),
                list(decision.accepted_evidence_ids),
            )
            await connection.execute(
                _UPDATE_DATASET_STATE,
                decision.dataset_id,
                decision.resulting_state.value,
            )

    async def add_canonical_segment(self, segment: CanonicalSegment) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            lineage = await connection.fetchrow(
                _SELECT_SEGMENT_LINEAGE,
                segment.dataset_id,
                segment.admission_decision_id,
            )
            if lineage is None:
                raise ValueError("canonical segment is missing persisted admission lineage")
            if (
                lineage["state"] != DatasetState.TRUSTED.value
                or lineage["resulting_state"] != DatasetState.TRUSTED.value
                or lineage["decision_dataset_id"] != segment.dataset_id
            ):
                raise ValueError("canonical segment requires a trusted dataset and decision")
            if (
                lineage["record_kind"] != segment.record_kind.value
                or lineage["instrument_symbol"] != segment.instrument_symbol
            ):
                raise ValueError("canonical segment identity differs from its dataset")
            overlap = await connection.fetchval(
                _SELECT_OVERLAPPING_SEGMENT,
                segment.instrument_symbol,
                segment.record_kind.value,
                segment.start,
                segment.end,
            )
            if overlap is not None:
                raise ValueError(f"canonical segment overlaps existing segment {overlap}")
            await connection.execute(
                _INSERT_CANONICAL_SEGMENT,
                segment.segment_id,
                segment.dataset_id,
                segment.admission_decision_id,
                segment.record_kind.value,
                segment.instrument_symbol,
                segment.start,
                segment.end,
                segment.admission_policy_version,
                segment.selection_reason,
            )

    async def load_trusted_quotes(
        self,
        instrument_symbol: str,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[QuoteObservation, ...]:
        self._validate_range(instrument_symbol, start, end)
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _SELECT_TRUSTED_QUOTES,
                instrument_symbol,
                start,
                end,
            )
        return tuple(
            QuoteObservation(
                dataset_id=str(row["dataset_id"]),
                event_index=int(row["event_index"]),
                source_observed_at=row["source_observed_at"],
                received_at=row["received_at"],
                bid=row["bid"],
                ask=row["ask"],
                last=row["last"],
                source_event_id=row["source_event_id"],
                raw_payload_sha256=row["raw_payload_sha256"],
            )
            for row in rows
        )

    async def load_trusted_bars(
        self,
        instrument_symbol: str,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[BarObservation, ...]:
        self._validate_range(instrument_symbol, start, end)
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _SELECT_TRUSTED_BARS,
                instrument_symbol,
                start,
                end,
            )
        return tuple(
            BarObservation(
                dataset_id=str(row["dataset_id"]),
                open_time=row["open_time"],
                interval=timedelta(microseconds=int(row["interval_microseconds"])),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                source_row_number=row["source_row_number"],
                raw_payload_sha256=row["raw_payload_sha256"],
            )
            for row in rows
        )

    @staticmethod
    def _validate_range(instrument_symbol: str, start: datetime, end: datetime) -> None:
        if not instrument_symbol.strip():
            raise ValueError("instrument_symbol cannot be empty")
        for field_name, value in (("start", start), ("end", end)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if end <= start:
            raise ValueError("end must be after start")


def dataset_state_counts(rows: Sequence[Mapping[str, object]]) -> Mapping[str, int]:
    """Small reporting helper kept independent from provider/application services."""

    counts = {state.value: 0 for state in DatasetState}
    for row in rows:
        state = str(row["state"])
        if state not in counts:
            raise ValueError(f"unknown historical dataset state: {state}")
        counts[state] += 1
    return counts
