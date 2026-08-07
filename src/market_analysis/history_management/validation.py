from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from market_analysis.history_management.models import (
    BarObservation,
    CompatibilityLevel,
    CrossValidationEvidence,
    DatasetCompatibility,
    FindingSeverity,
    HistoricalDatasetBundle,
    HistoricalRecordKind,
    QuoteObservation,
    QuotePriceField,
    ValidationFinding,
    ValidationReport,
)

VALIDATOR_VERSION = "history-governance-v1"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _timestamp_aligned(value: datetime, resolution: timedelta) -> bool:
    elapsed_microseconds = (value.astimezone(UTC) - _EPOCH) // _MICROSECOND
    resolution_microseconds = resolution // _MICROSECOND
    return elapsed_microseconds % resolution_microseconds == 0


def _decimal_scale(value: Decimal) -> int:
    return max(0, -value.as_tuple().exponent)


def _on_price_lattice(value: Decimal, quantum: Decimal) -> bool:
    units = value / quantum
    return units == units.to_integral_value()


def _relative_deviation_bps(left: Decimal, right: Decimal) -> Decimal:
    midpoint = (abs(left) + abs(right)) / Decimal(2)
    if midpoint == 0:
        return Decimal(0) if left == right else Decimal("Infinity")
    return abs(left - right) / midpoint * Decimal(10_000)


def _median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)


def _price_values(record: QuoteObservation | BarObservation) -> Iterable[tuple[str, Decimal]]:
    if isinstance(record, QuoteObservation):
        for field_name in ("bid", "ask", "last"):
            value = getattr(record, field_name)
            if value is not None:
                yield field_name, value
        return
    for field_name in ("open", "high", "low", "close"):
        yield field_name, getattr(record, field_name)


class HistoricalDatasetValidator:
    """Validates one channel-pure dataset without making provider requests."""

    def validate(
        self,
        bundle: HistoricalDatasetBundle,
        *,
        checked_at: datetime | None = None,
    ) -> ValidationReport:
        descriptor = bundle.descriptor
        findings: list[ValidationFinding] = []
        metrics: dict[str, object] = {
            "record_count": bundle.record_count,
            "artifact_count": len(bundle.artifacts),
            "record_kind": descriptor.record_kind.value,
            "timestamp_resolution_microseconds": (
                descriptor.timestamp_resolution // _MICROSECOND
            ),
            "storage_price_scale": descriptor.storage_price_scale,
            "effective_price_quantum": str(descriptor.effective_price_quantum),
        }

        self._validate_integrity(bundle, findings)
        if bundle.record_count == 0:
            findings.append(
                ValidationFinding(
                    code="DATASET_EMPTY",
                    severity=FindingSeverity.ERROR,
                    message="dataset contains no historical records",
                )
            )
        elif descriptor.record_kind is HistoricalRecordKind.QUOTE_EVENT:
            self._validate_quotes(bundle, findings, metrics)
        else:
            self._validate_bars(bundle, findings, metrics)

        return ValidationReport(
            validation_id=str(uuid4()),
            dataset_id=descriptor.dataset_id,
            validator_version=VALIDATOR_VERSION,
            checked_at=(checked_at or datetime.now(UTC)).astimezone(UTC),
            findings=tuple(findings),
            metrics=metrics,
        )

    @staticmethod
    def _validate_integrity(
        bundle: HistoricalDatasetBundle,
        findings: list[ValidationFinding],
    ) -> None:
        if not bundle.artifacts:
            records: tuple[QuoteObservation | BarObservation, ...] = (
                bundle.quote_events or bundle.bars
            )
            if not records or any(record.raw_payload_sha256 is None for record in records):
                findings.append(
                    ValidationFinding(
                        code="RAW_LINEAGE_INCOMPLETE",
                        severity=FindingSeverity.WARNING,
                        message=(
                            "dataset has neither a verified artifact chain nor a raw payload "
                            "hash for every record"
                        ),
                    )
                )
            return
        for artifact in bundle.artifacts:
            if not artifact.integrity_verified:
                findings.append(
                    ValidationFinding(
                        code="ARTIFACT_INTEGRITY_FAILED",
                        severity=FindingSeverity.ERROR,
                        message=f"artifact integrity check failed: {artifact.relative_path}",
                        record_key=artifact.artifact_id,
                        details={
                            "expected_bytes": artifact.expected_bytes,
                            "actual_bytes": artifact.actual_bytes,
                            "expected_sha256": artifact.expected_sha256,
                            "actual_sha256": artifact.actual_sha256,
                        },
                    )
                )
            if artifact.publisher_authentication is None:
                findings.append(
                    ValidationFinding(
                        code="ARTIFACT_NOT_PUBLISHER_SIGNED",
                        severity=FindingSeverity.WARNING,
                        message=(
                            "checksum proves local package consistency, not publisher "
                            "authenticity"
                        ),
                        record_key=artifact.artifact_id,
                    )
                )

    def _validate_quotes(
        self,
        bundle: HistoricalDatasetBundle,
        findings: list[ValidationFinding],
        metrics: dict[str, object],
    ) -> None:
        descriptor = bundle.descriptor
        seen_indexes: set[int] = set()
        previous_index: int | None = None
        previous_source_time: datetime | None = None
        index_gaps = 0
        source_time_regressions = 0
        precision_violations = 0
        scale_violations = 0
        field_violations = 0

        for record in bundle.quote_events:
            record_key = str(record.event_index)
            if record.event_index in seen_indexes:
                findings.append(
                    ValidationFinding(
                        code="DUPLICATE_EVENT_INDEX",
                        severity=FindingSeverity.ERROR,
                        message="quote event_index is duplicated inside one dataset",
                        record_key=record_key,
                    )
                )
            seen_indexes.add(record.event_index)
            if previous_index is not None:
                if record.event_index <= previous_index:
                    findings.append(
                        ValidationFinding(
                            code="EVENT_INDEX_OUT_OF_ORDER",
                            severity=FindingSeverity.ERROR,
                            message="quote event_index must be strictly increasing",
                            record_key=record_key,
                        )
                    )
                elif record.event_index > previous_index + 1:
                    index_gaps += record.event_index - previous_index - 1
            previous_index = record.event_index

            if (
                previous_source_time is not None
                and record.source_observed_at < previous_source_time
            ):
                source_time_regressions += 1
            previous_source_time = record.source_observed_at

            if not _timestamp_aligned(
                record.source_observed_at,
                descriptor.timestamp_resolution,
            ):
                precision_violations += 1
            undeclared_fields = record.populated_fields - descriptor.quote_fields
            if undeclared_fields:
                field_violations += 1
                findings.append(
                    ValidationFinding(
                        code="UNDECLARED_QUOTE_FIELD",
                        severity=FindingSeverity.ERROR,
                        message="quote record contains fields not declared by its channel",
                        record_key=record_key,
                        details={"fields": sorted(field.value for field in undeclared_fields)},
                    )
                )
            record_scale, record_quantum = self._price_precision_violations(
                record,
                bundle,
            )
            scale_violations += record_scale
            precision_violations += record_quantum

        self._append_precision_findings(
            findings,
            timestamp_or_quantum_violations=precision_violations,
            scale_violations=scale_violations,
        )
        if index_gaps:
            findings.append(
                ValidationFinding(
                    code="EVENT_INDEX_GAPS",
                    severity=FindingSeverity.WARNING,
                    message="capture sequence contains missing event indexes",
                    details={"missing_event_indexes": index_gaps},
                )
            )
        if source_time_regressions:
            findings.append(
                ValidationFinding(
                    code="SOURCE_TIME_REGRESSION",
                    severity=FindingSeverity.WARNING,
                    message=(
                        "provider timestamps moved backwards; ingest order remains preserved "
                        "by event_index"
                    ),
                    details={"regressions": source_time_regressions},
                )
            )
        metrics.update(
            {
                "event_index_gaps": index_gaps,
                "source_time_regressions": source_time_regressions,
                "undeclared_field_records": field_violations,
                "first_source_time": (
                    bundle.quote_events[0].source_observed_at.isoformat()
                    if bundle.quote_events
                    else None
                ),
                "last_source_time": (
                    bundle.quote_events[-1].source_observed_at.isoformat()
                    if bundle.quote_events
                    else None
                ),
            }
        )

    def _validate_bars(
        self,
        bundle: HistoricalDatasetBundle,
        findings: list[ValidationFinding],
        metrics: dict[str, object],
    ) -> None:
        descriptor = bundle.descriptor
        seen_times: set[datetime] = set()
        previous_time: datetime | None = None
        duplicate_count = 0
        out_of_order_count = 0
        missing_intervals = 0
        alignment_violations = 0
        quantum_violations = 0
        scale_violations = 0

        for record in bundle.bars:
            record_key = record.open_time.isoformat()
            if record.interval != descriptor.bar_interval:
                findings.append(
                    ValidationFinding(
                        code="BAR_INTERVAL_MISMATCH",
                        severity=FindingSeverity.ERROR,
                        message="bar interval differs from its dataset descriptor",
                        record_key=record_key,
                    )
                )
            if record.open_time in seen_times:
                duplicate_count += 1
            seen_times.add(record.open_time)
            if previous_time is not None:
                if record.open_time < previous_time:
                    out_of_order_count += 1
                elif descriptor.bar_interval is not None:
                    elapsed = record.open_time - previous_time
                    if elapsed > descriptor.bar_interval:
                        missing_intervals += int(elapsed / descriptor.bar_interval) - 1
            previous_time = record.open_time
            if not _timestamp_aligned(record.open_time, descriptor.timestamp_resolution):
                alignment_violations += 1
            record_scale, record_quantum = self._price_precision_violations(
                record,
                bundle,
            )
            scale_violations += record_scale
            quantum_violations += record_quantum

        if duplicate_count:
            findings.append(
                ValidationFinding(
                    code="DUPLICATE_BAR_TIME",
                    severity=FindingSeverity.ERROR,
                    message="bar dataset contains duplicate open times",
                    details={"duplicate_records": duplicate_count},
                )
            )
        if out_of_order_count:
            findings.append(
                ValidationFinding(
                    code="BAR_TIME_OUT_OF_ORDER",
                    severity=FindingSeverity.ERROR,
                    message="bar dataset is not ordered by open time",
                    details={"transitions": out_of_order_count},
                )
            )
        if missing_intervals:
            findings.append(
                ValidationFinding(
                    code="BAR_GAPS_PRESERVED",
                    severity=FindingSeverity.WARNING,
                    message=(
                        "bar timeline contains gaps; no synthetic rows were generated by "
                        "validation"
                    ),
                    details={"naive_missing_intervals": missing_intervals},
                )
            )
        self._append_precision_findings(
            findings,
            timestamp_or_quantum_violations=alignment_violations + quantum_violations,
            scale_violations=scale_violations,
        )
        metrics.update(
            {
                "duplicate_open_times": duplicate_count,
                "out_of_order_transitions": out_of_order_count,
                "naive_missing_intervals": missing_intervals,
                "timestamp_alignment_violations": alignment_violations,
                "price_quantum_violations": quantum_violations,
                "first_open_time": (
                    bundle.bars[0].open_time.isoformat() if bundle.bars else None
                ),
                "last_open_time": (
                    bundle.bars[-1].open_time.isoformat() if bundle.bars else None
                ),
            }
        )

    @staticmethod
    def _price_precision_violations(
        record: QuoteObservation | BarObservation,
        bundle: HistoricalDatasetBundle,
    ) -> tuple[int, int]:
        descriptor = bundle.descriptor
        scale_violations = 0
        quantum_violations = 0
        for _, value in _price_values(record):
            if _decimal_scale(value) > descriptor.storage_price_scale:
                scale_violations += 1
            if not _on_price_lattice(value, descriptor.effective_price_quantum):
                quantum_violations += 1
        return scale_violations, quantum_violations

    @staticmethod
    def _append_precision_findings(
        findings: list[ValidationFinding],
        *,
        timestamp_or_quantum_violations: int,
        scale_violations: int,
    ) -> None:
        if timestamp_or_quantum_violations:
            findings.append(
                ValidationFinding(
                    code="DECLARED_PRECISION_VIOLATION",
                    severity=FindingSeverity.ERROR,
                    message=(
                        "record values do not align with the dataset's declared time or "
                        "price quantum"
                    ),
                    details={"violations": timestamp_or_quantum_violations},
                )
            )
        if scale_violations:
            findings.append(
                ValidationFinding(
                    code="STORAGE_SCALE_VIOLATION",
                    severity=FindingSeverity.ERROR,
                    message="record price uses more decimal places than declared",
                    details={"violations": scale_violations},
                )
            )


def evaluate_compatibility(
    left: HistoricalDatasetBundle,
    right: HistoricalDatasetBundle,
) -> DatasetCompatibility:
    left_descriptor = left.descriptor
    right_descriptor = right.descriptor
    independent = (
        left_descriptor.independence_group != right_descriptor.independence_group
    )
    reasons: list[str] = []

    if (
        left_descriptor.underlying != right_descriptor.underlying
        or left_descriptor.quote_currency != right_descriptor.quote_currency
    ):
        return DatasetCompatibility(
            left_dataset_id=left_descriptor.dataset_id,
            right_dataset_id=right_descriptor.dataset_id,
            level=CompatibilityLevel.INCOMPATIBLE,
            independent=independent,
            reasons=("different underlying or quote currency",),
        )

    if left_descriptor.instrument_identity != right_descriptor.instrument_identity:
        reasons.append("different instrument, venue, or contract identity")
    if left_descriptor.record_kind is not right_descriptor.record_kind:
        reasons.append("different record granularity")
    if left_descriptor.timestamp_semantics is not right_descriptor.timestamp_semantics:
        reasons.append("different timestamp semantics")

    comparable_fields: frozenset[QuotePriceField] = frozenset()
    if (
        left_descriptor.record_kind is HistoricalRecordKind.QUOTE_EVENT
        and right_descriptor.record_kind is HistoricalRecordKind.QUOTE_EVENT
    ):
        comparable_fields = left_descriptor.quote_fields & right_descriptor.quote_fields
        if not comparable_fields:
            reasons.append("quote channels have no common price field")
    elif (
        left_descriptor.record_kind is HistoricalRecordKind.BAR_OBSERVATION
        and right_descriptor.record_kind is HistoricalRecordKind.BAR_OBSERVATION
    ):
        if left_descriptor.bar_price_basis is not right_descriptor.bar_price_basis:
            reasons.append("different bar price basis")
        if left_descriptor.bar_interval != right_descriptor.bar_interval:
            reasons.append("different bar interval")

    level = CompatibilityLevel.EXACT if not reasons else CompatibilityLevel.SANITY_ONLY
    return DatasetCompatibility(
        left_dataset_id=left_descriptor.dataset_id,
        right_dataset_id=right_descriptor.dataset_id,
        level=level,
        independent=independent,
        comparable_price_fields=comparable_fields,
        reasons=tuple(reasons),
    )


def compare_bar_datasets(
    left: HistoricalDatasetBundle,
    right: HistoricalDatasetBundle,
    *,
    max_deviation_bps: Decimal = Decimal("5"),
    minimum_overlap: int = 10,
    checked_at: datetime | None = None,
) -> CrossValidationEvidence:
    compatibility = evaluate_compatibility(left, right)
    if (
        left.descriptor.record_kind is not HistoricalRecordKind.BAR_OBSERVATION
        or right.descriptor.record_kind is not HistoricalRecordKind.BAR_OBSERVATION
    ):
        raise ValueError("compare_bar_datasets requires two bar datasets")
    left_by_time = {record.open_time: record for record in left.bars}
    right_by_time = {record.open_time: record for record in right.bars}
    deviations: list[Decimal] = []
    matched_times = sorted(left_by_time.keys() & right_by_time.keys())
    for open_time in matched_times:
        left_record = left_by_time[open_time]
        right_record = right_by_time[open_time]
        deviations.extend(
            _relative_deviation_bps(
                getattr(left_record, field_name),
                getattr(right_record, field_name),
            )
            for field_name in ("open", "high", "low", "close")
        )
    maximum = max(deviations, default=Decimal(0))
    passed = (
        compatibility.level is CompatibilityLevel.EXACT
        and len(matched_times) >= minimum_overlap
        and maximum <= max_deviation_bps
    )
    return CrossValidationEvidence(
        evidence_id=str(uuid4()),
        left_dataset_id=left.descriptor.dataset_id,
        right_dataset_id=right.descriptor.dataset_id,
        level=compatibility.level,
        independent=compatibility.independent,
        method="aligned_bar_ohlc_deviation",
        passed=passed,
        matched_records=len(matched_times),
        checked_at=(checked_at or datetime.now(UTC)).astimezone(UTC),
        metrics={
            "maximum_ohlc_deviation_bps": str(maximum),
            "median_ohlc_deviation_bps": str(_median(deviations)),
            "threshold_bps": str(max_deviation_bps),
            "minimum_overlap": minimum_overlap,
            "compatibility_reasons": compatibility.reasons,
        },
    )


def _bucket_start(value: datetime, resolution: timedelta) -> datetime:
    elapsed_microseconds = (value.astimezone(UTC) - _EPOCH) // _MICROSECOND
    resolution_microseconds = resolution // _MICROSECOND
    bucket_microseconds = elapsed_microseconds - elapsed_microseconds % resolution_microseconds
    return _EPOCH + timedelta(microseconds=bucket_microseconds)


def compare_quote_datasets(
    left: HistoricalDatasetBundle,
    right: HistoricalDatasetBundle,
    *,
    max_deviation_bps: Decimal = Decimal("5"),
    minimum_overlap: int = 10,
    checked_at: datetime | None = None,
) -> CrossValidationEvidence:
    compatibility = evaluate_compatibility(left, right)
    if (
        left.descriptor.record_kind is not HistoricalRecordKind.QUOTE_EVENT
        or right.descriptor.record_kind is not HistoricalRecordKind.QUOTE_EVENT
    ):
        raise ValueError("compare_quote_datasets requires two quote event datasets")
    resolution = max(
        left.descriptor.timestamp_resolution,
        right.descriptor.timestamp_resolution,
    )

    def latest_by_bucket(
        records: Sequence[QuoteObservation],
    ) -> dict[datetime, QuoteObservation]:
        buckets: dict[datetime, QuoteObservation] = {}
        for record in records:
            bucket = _bucket_start(record.source_observed_at, resolution)
            current = buckets.get(bucket)
            if current is None or (
                record.source_observed_at,
                record.event_index,
            ) > (current.source_observed_at, current.event_index):
                buckets[bucket] = record
        return buckets

    left_by_bucket = latest_by_bucket(left.quote_events)
    right_by_bucket = latest_by_bucket(right.quote_events)
    matched_buckets = sorted(left_by_bucket.keys() & right_by_bucket.keys())
    deviations: list[Decimal] = []
    for bucket in matched_buckets:
        left_record = left_by_bucket[bucket]
        right_record = right_by_bucket[bucket]
        for price_field in compatibility.comparable_price_fields:
            left_value = getattr(left_record, price_field.value)
            right_value = getattr(right_record, price_field.value)
            if left_value is not None and right_value is not None:
                deviations.append(_relative_deviation_bps(left_value, right_value))

    maximum = max(deviations, default=Decimal(0))
    passed = (
        compatibility.level is CompatibilityLevel.EXACT
        and len(matched_buckets) >= minimum_overlap
        and bool(deviations)
        and maximum <= max_deviation_bps
    )
    return CrossValidationEvidence(
        evidence_id=str(uuid4()),
        left_dataset_id=left.descriptor.dataset_id,
        right_dataset_id=right.descriptor.dataset_id,
        level=compatibility.level,
        independent=compatibility.independent,
        method="latest_quote_per_precision_bucket",
        passed=passed,
        matched_records=len(matched_buckets),
        checked_at=(checked_at or datetime.now(UTC)).astimezone(UTC),
        metrics={
            "comparison_resolution_microseconds": resolution // _MICROSECOND,
            "compared_price_points": len(deviations),
            "maximum_price_deviation_bps": str(maximum),
            "median_price_deviation_bps": str(_median(deviations)),
            "threshold_bps": str(max_deviation_bps),
            "minimum_overlap": minimum_overlap,
            "compatibility_reasons": compatibility.reasons,
        },
    )
