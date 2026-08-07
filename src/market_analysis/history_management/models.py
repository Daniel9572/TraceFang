from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class HistoricalRecordKind(StrEnum):
    QUOTE_EVENT = "quote_event"
    BAR_OBSERVATION = "bar_observation"


class InstrumentType(StrEnum):
    OTC_SPOT = "otc_spot"
    EXCHANGE_SPOT = "exchange_spot"
    FUTURE = "future"
    FORWARD = "forward"
    OPTION = "option"
    EQUITY = "equity"
    INDEX = "index"
    FX_SPOT = "fx_spot"
    CRYPTO_SPOT = "crypto_spot"
    UNKNOWN = "unknown"


class QuotePriceField(StrEnum):
    BID = "bid"
    ASK = "ask"
    LAST = "last"


class BarPriceBasis(StrEnum):
    BID = "bid"
    ASK = "ask"
    LAST_QUOTE = "last_quote"
    TRADE = "trade"
    SETTLEMENT = "settlement"
    FIXING = "fixing"
    PROVIDER_CLOSE = "provider_close"


class TimestampSemantics(StrEnum):
    PROVIDER_EVENT_TIME = "provider_event_time"
    RECEIPT_TIME = "receipt_time"
    BAR_OPEN_TIME = "bar_open_time"


class DatasetState(StrEnum):
    REGISTERED = "registered"
    INGESTED = "ingested"
    VALIDATED_CANDIDATE = "validated_candidate"
    QUARANTINED = "quarantined"
    TRUSTED = "trusted"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationStatus(StrEnum):
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL = "fail"


class CompatibilityLevel(StrEnum):
    EXACT = "exact"
    SANITY_ONLY = "sanity_only"
    INCOMPATIBLE = "incompatible"


class AdmissionTarget(StrEnum):
    TRUSTED_QUOTE_HISTORY = "trusted_quote_history"
    TRUSTED_BAR_REFERENCE = "trusted_bar_reference"


def require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def require_finite_positive(value: Decimal, field_name: str) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be finite and positive")


def require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class HistoricalDatasetDescriptor:
    dataset_id: str
    provider_family: str
    channel_id: str
    feed_id: str
    independence_group: str
    instrument_symbol: str
    underlying: str
    quote_currency: str | None
    instrument_type: InstrumentType
    venue: str
    contract_code: str | None
    record_kind: HistoricalRecordKind
    quote_fields: frozenset[QuotePriceField] = field(default_factory=frozenset)
    bar_price_basis: BarPriceBasis | None = None
    bar_interval: timedelta | None = None
    source_timezone: str = "UTC"
    normalized_timezone: str = "UTC"
    timestamp_semantics: TimestampSemantics = TimestampSemantics.PROVIDER_EVENT_TIME
    timestamp_resolution: timedelta = timedelta(seconds=1)
    storage_price_scale: int = 6
    effective_price_quantum: Decimal = Decimal("0.000001")
    source_uri: str = ""
    parser_version: str = "1"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required_strings = {
            "dataset_id": self.dataset_id,
            "provider_family": self.provider_family,
            "channel_id": self.channel_id,
            "feed_id": self.feed_id,
            "independence_group": self.independence_group,
            "instrument_symbol": self.instrument_symbol,
            "underlying": self.underlying,
            "venue": self.venue,
            "source_timezone": self.source_timezone,
            "parser_version": self.parser_version,
        }
        for field_name, value in required_strings.items():
            if not value.strip():
                raise ValueError(f"{field_name} cannot be empty")
        if self.normalized_timezone != "UTC":
            raise ValueError("historical timestamps must be normalized to UTC")
        if self.timestamp_resolution <= timedelta(0):
            raise ValueError("timestamp_resolution must be positive")
        if not 0 <= self.storage_price_scale <= 18:
            raise ValueError("storage_price_scale must be between 0 and 18")
        require_finite_positive(self.effective_price_quantum, "effective_price_quantum")
        if self.record_kind is HistoricalRecordKind.QUOTE_EVENT:
            if not self.quote_fields:
                raise ValueError("quote event datasets must declare quote_fields")
            if self.bar_price_basis is not None or self.bar_interval is not None:
                raise ValueError("quote event datasets cannot declare bar semantics")
            if self.timestamp_semantics is TimestampSemantics.BAR_OPEN_TIME:
                raise ValueError("quote event datasets cannot use bar_open_time semantics")
        else:
            if self.quote_fields:
                raise ValueError("bar datasets cannot declare quote_fields")
            if self.bar_price_basis is None or self.bar_interval is None:
                raise ValueError("bar datasets must declare price basis and interval")
            if self.bar_interval <= timedelta(0):
                raise ValueError("bar_interval must be positive")
            if self.timestamp_semantics is not TimestampSemantics.BAR_OPEN_TIME:
                raise ValueError("bar datasets must use bar_open_time semantics")

    @property
    def channel_identity(self) -> tuple[str, str, str]:
        return self.provider_family, self.channel_id, self.feed_id

    @property
    def instrument_identity(self) -> tuple[object, ...]:
        return (
            self.instrument_symbol,
            self.underlying,
            self.quote_currency,
            self.instrument_type,
            self.venue,
            self.contract_code,
        )


@dataclass(frozen=True, slots=True)
class QuoteObservation:
    dataset_id: str
    event_index: int
    source_observed_at: datetime
    received_at: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    source_event_id: str | None = None
    raw_payload_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id cannot be empty")
        if self.event_index < 0:
            raise ValueError("event_index cannot be negative")
        require_aware(self.source_observed_at, "source_observed_at")
        require_aware(self.received_at, "received_at")
        prices = {"bid": self.bid, "ask": self.ask, "last": self.last}
        if all(value is None for value in prices.values()):
            raise ValueError("a quote observation must contain at least one price")
        for field_name, value in prices.items():
            if value is not None:
                require_finite_positive(value, field_name)
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid cannot be greater than ask")
        if self.raw_payload_sha256 is not None:
            require_sha256(self.raw_payload_sha256, "raw_payload_sha256")

    @property
    def populated_fields(self) -> frozenset[QuotePriceField]:
        return frozenset(
            field_name
            for field_name, value in (
                (QuotePriceField.BID, self.bid),
                (QuotePriceField.ASK, self.ask),
                (QuotePriceField.LAST, self.last),
            )
            if value is not None
        )


@dataclass(frozen=True, slots=True)
class BarObservation:
    dataset_id: str
    open_time: datetime
    interval: timedelta
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    source_row_number: int | None = None
    raw_payload_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id cannot be empty")
        require_aware(self.open_time, "open_time")
        if self.interval <= timedelta(0):
            raise ValueError("interval must be positive")
        for field_name in ("open", "high", "low", "close"):
            require_finite_positive(getattr(self, field_name), field_name)
        if self.low > self.high:
            raise ValueError("low cannot be greater than high")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must be within low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must be within low and high")
        if self.volume is not None and (not self.volume.is_finite() or self.volume < 0):
            raise ValueError("volume must be finite and non-negative")
        if self.source_row_number is not None and self.source_row_number < 1:
            raise ValueError("source_row_number must be positive")
        if self.raw_payload_sha256 is not None:
            require_sha256(self.raw_payload_sha256, "raw_payload_sha256")


@dataclass(frozen=True, slots=True)
class ArtifactVerification:
    artifact_id: str
    relative_path: str
    expected_bytes: int
    actual_bytes: int
    expected_sha256: str
    actual_sha256: str
    publisher_authentication: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id.strip() or not self.relative_path.strip():
            raise ValueError("artifact identity cannot be empty")
        if self.expected_bytes < 0 or self.actual_bytes < 0:
            raise ValueError("artifact byte lengths cannot be negative")
        require_sha256(self.expected_sha256, "expected_sha256")
        require_sha256(self.actual_sha256, "actual_sha256")

    @property
    def integrity_verified(self) -> bool:
        return (
            self.expected_bytes == self.actual_bytes
            and self.expected_sha256 == self.actual_sha256
        )


@dataclass(frozen=True, slots=True)
class HistoricalDatasetBundle:
    descriptor: HistoricalDatasetDescriptor
    artifacts: tuple[ArtifactVerification, ...] = ()
    quote_events: tuple[QuoteObservation, ...] = ()
    bars: tuple[BarObservation, ...] = ()

    def __post_init__(self) -> None:
        if self.descriptor.record_kind is HistoricalRecordKind.QUOTE_EVENT:
            if self.bars:
                raise ValueError("quote event datasets cannot contain bar records")
            records: tuple[QuoteObservation | BarObservation, ...] = self.quote_events
        else:
            if self.quote_events:
                raise ValueError("bar datasets cannot contain quote event records")
            records = self.bars
        if any(record.dataset_id != self.descriptor.dataset_id for record in records):
            raise ValueError("all records must belong to the descriptor dataset")

    @property
    def record_count(self) -> int:
        return len(self.quote_events) + len(self.bars)


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: str
    severity: FindingSeverity
    message: str
    record_key: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ValueError("finding code and message cannot be empty")


@dataclass(frozen=True, slots=True)
class ValidationReport:
    validation_id: str
    dataset_id: str
    validator_version: str
    checked_at: datetime
    findings: tuple[ValidationFinding, ...]
    metrics: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.validation_id.strip() or not self.dataset_id.strip():
            raise ValueError("validation identity cannot be empty")
        if not self.validator_version.strip():
            raise ValueError("validator_version cannot be empty")
        require_aware(self.checked_at, "checked_at")

    @property
    def status(self) -> ValidationStatus:
        severities = {finding.severity for finding in self.findings}
        if FindingSeverity.ERROR in severities:
            return ValidationStatus.FAIL
        if FindingSeverity.WARNING in severities:
            return ValidationStatus.PASS_WITH_WARNINGS
        return ValidationStatus.PASS

    @property
    def error_count(self) -> int:
        return sum(finding.severity is FindingSeverity.ERROR for finding in self.findings)

    @property
    def warning_count(self) -> int:
        return sum(finding.severity is FindingSeverity.WARNING for finding in self.findings)


@dataclass(frozen=True, slots=True)
class DatasetCompatibility:
    left_dataset_id: str
    right_dataset_id: str
    level: CompatibilityLevel
    independent: bool
    comparable_price_fields: frozenset[QuotePriceField] = field(default_factory=frozenset)
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CrossValidationEvidence:
    evidence_id: str
    left_dataset_id: str
    right_dataset_id: str
    level: CompatibilityLevel
    independent: bool
    method: str
    passed: bool
    matched_records: int
    checked_at: datetime
    metrics: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_id.strip() or not self.method.strip():
            raise ValueError("cross-validation identity cannot be empty")
        if self.left_dataset_id == self.right_dataset_id:
            raise ValueError("cross-validation requires two different datasets")
        if self.matched_records < 0:
            raise ValueError("matched_records cannot be negative")
        require_aware(self.checked_at, "checked_at")

    def includes(self, dataset_id: str) -> bool:
        return dataset_id in {self.left_dataset_id, self.right_dataset_id}


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    decision_id: str
    dataset_id: str
    target: AdmissionTarget
    resulting_state: DatasetState
    decided_at: datetime
    policy_version: str
    blockers: tuple[str, ...]
    accepted_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.decision_id.strip() or not self.dataset_id.strip():
            raise ValueError("admission identity cannot be empty")
        require_aware(self.decided_at, "decided_at")

    @property
    def accepted(self) -> bool:
        return self.resulting_state is DatasetState.TRUSTED


@dataclass(frozen=True, slots=True)
class CanonicalSegment:
    segment_id: str
    dataset_id: str
    admission_decision_id: str
    record_kind: HistoricalRecordKind
    instrument_symbol: str
    start: datetime
    end: datetime
    admission_policy_version: str
    selection_reason: str

    def __post_init__(self) -> None:
        for field_name in (
            "segment_id",
            "dataset_id",
            "admission_decision_id",
            "instrument_symbol",
            "admission_policy_version",
            "selection_reason",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} cannot be empty")
        require_aware(self.start, "start")
        require_aware(self.end, "end")
        if self.end <= self.start:
            raise ValueError("canonical segment end must be after start")
