from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from tracefang.history_management.models import (
    AdmissionDecision,
    AdmissionTarget,
    CanonicalSegment,
    CompatibilityLevel,
    CrossValidationEvidence,
    DatasetState,
    HistoricalDatasetBundle,
    HistoricalRecordKind,
    ValidationReport,
    ValidationStatus,
)

ADMISSION_POLICY_VERSION = "history-admission-v1"


class HistoricalAdmissionPolicy:
    """Promotes validated datasets without averaging or copying fields between channels."""

    def __init__(self, *, minimum_independent_confirmations: int = 1) -> None:
        if minimum_independent_confirmations < 1:
            raise ValueError("at least one independent confirmation is required")
        self._minimum_independent_confirmations = minimum_independent_confirmations

    def evaluate(
        self,
        bundle: HistoricalDatasetBundle,
        report: ValidationReport,
        *,
        target: AdmissionTarget,
        cross_validation: Sequence[CrossValidationEvidence] = (),
        decided_at: datetime | None = None,
    ) -> AdmissionDecision:
        if report.dataset_id != bundle.descriptor.dataset_id:
            raise ValueError("validation report belongs to another dataset")

        blockers: list[str] = []
        expected_kind = (
            HistoricalRecordKind.QUOTE_EVENT
            if target is AdmissionTarget.TRUSTED_QUOTE_HISTORY
            else HistoricalRecordKind.BAR_OBSERVATION
        )
        if bundle.descriptor.record_kind is not expected_kind:
            blockers.append("TARGET_RECORD_KIND_MISMATCH")

        if report.status is ValidationStatus.FAIL:
            blockers.append("STRUCTURAL_VALIDATION_FAILED")

        if not self._has_verified_raw_lineage(bundle):
            blockers.append("RAW_LINEAGE_NOT_VERIFIED")

        accepted_evidence: list[str] = []
        independent_datasets: set[str] = set()
        for evidence in cross_validation:
            if not evidence.includes(bundle.descriptor.dataset_id):
                continue
            if (
                evidence.level is not CompatibilityLevel.EXACT
                or not evidence.independent
                or not evidence.passed
            ):
                continue
            other_dataset = (
                evidence.right_dataset_id
                if evidence.left_dataset_id == bundle.descriptor.dataset_id
                else evidence.left_dataset_id
            )
            independent_datasets.add(other_dataset)
            accepted_evidence.append(evidence.evidence_id)

        if len(independent_datasets) < self._minimum_independent_confirmations:
            blockers.append("INSUFFICIENT_INDEPENDENT_EXACT_CONFIRMATION")

        if report.status is ValidationStatus.FAIL:
            state = DatasetState.QUARANTINED
        elif blockers:
            state = DatasetState.VALIDATED_CANDIDATE
        else:
            state = DatasetState.TRUSTED

        return AdmissionDecision(
            decision_id=str(uuid4()),
            dataset_id=bundle.descriptor.dataset_id,
            target=target,
            resulting_state=state,
            decided_at=(decided_at or datetime.now(UTC)).astimezone(UTC),
            policy_version=ADMISSION_POLICY_VERSION,
            blockers=tuple(blockers),
            accepted_evidence_ids=tuple(dict.fromkeys(accepted_evidence)),
        )

    @staticmethod
    def _has_verified_raw_lineage(bundle: HistoricalDatasetBundle) -> bool:
        if bundle.artifacts:
            return all(artifact.integrity_verified for artifact in bundle.artifacts)
        records = bundle.quote_events or bundle.bars
        return bool(records) and all(record.raw_payload_sha256 is not None for record in records)


def create_canonical_segment(
    bundle: HistoricalDatasetBundle,
    decision: AdmissionDecision,
    *,
    segment_id: str,
    start: datetime,
    end: datetime,
    selection_reason: str,
) -> CanonicalSegment:
    """References one admitted dataset for a time range; it never merges record fields."""

    if decision.dataset_id != bundle.descriptor.dataset_id:
        raise ValueError("admission decision belongs to another dataset")
    if not decision.accepted:
        raise ValueError("only a trusted dataset can back a canonical segment")
    expected_target = (
        AdmissionTarget.TRUSTED_QUOTE_HISTORY
        if bundle.descriptor.record_kind is HistoricalRecordKind.QUOTE_EVENT
        else AdmissionTarget.TRUSTED_BAR_REFERENCE
    )
    if decision.target is not expected_target:
        raise ValueError("admission target does not match dataset record kind")
    return CanonicalSegment(
        segment_id=segment_id,
        dataset_id=bundle.descriptor.dataset_id,
        admission_decision_id=decision.decision_id,
        record_kind=bundle.descriptor.record_kind,
        instrument_symbol=bundle.descriptor.instrument_symbol,
        start=start,
        end=end,
        admission_policy_version=decision.policy_version,
        selection_reason=selection_reason,
    )
