from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tracefang.history_management.admission import (
    HistoricalAdmissionPolicy,
    create_canonical_segment,
)
from tracefang.history_management.models import (
    AdmissionTarget,
    ArtifactVerification,
    BarObservation,
    BarPriceBasis,
    CompatibilityLevel,
    DatasetState,
    HistoricalDatasetBundle,
    HistoricalDatasetDescriptor,
    HistoricalRecordKind,
    InstrumentType,
    QuoteObservation,
    QuotePriceField,
    TimestampSemantics,
    ValidationStatus,
)
from tracefang.history_management.validation import (
    HistoricalDatasetValidator,
    compare_bar_datasets,
    evaluate_compatibility,
)

_START = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
_DIGEST = "a" * 64


def verified_artifact(dataset_id: str) -> ArtifactVerification:
    return ArtifactVerification(
        artifact_id=f"artifact:{dataset_id}",
        relative_path=f"{dataset_id}.csv",
        expected_bytes=10,
        actual_bytes=10,
        expected_sha256=_DIGEST,
        actual_sha256=_DIGEST,
        publisher_authentication="test signature",
    )


def bar_descriptor(
    dataset_id: str,
    *,
    provider_family: str,
    channel_id: str,
    independence_group: str,
) -> HistoricalDatasetDescriptor:
    return HistoricalDatasetDescriptor(
        dataset_id=dataset_id,
        provider_family=provider_family,
        channel_id=channel_id,
        feed_id="xauusd_bid_m1",
        independence_group=independence_group,
        instrument_symbol="XAUUSD",
        underlying="XAU",
        quote_currency="USD",
        instrument_type=InstrumentType.OTC_SPOT,
        venue="OTC",
        contract_code=None,
        record_kind=HistoricalRecordKind.BAR_OBSERVATION,
        bar_price_basis=BarPriceBasis.BID,
        bar_interval=timedelta(minutes=1),
        source_timezone="UTC",
        normalized_timezone="UTC",
        timestamp_semantics=TimestampSemantics.BAR_OPEN_TIME,
        timestamp_resolution=timedelta(minutes=1),
        storage_price_scale=2,
        effective_price_quantum=Decimal("0.01"),
        source_uri="fixture://bars",
        parser_version="fixture-v1",
    )


def bar_bundle(
    dataset_id: str,
    *,
    provider_family: str = "provider-a",
    channel_id: str = "download-a",
    independence_group: str = "provider-a",
    offset: Decimal = Decimal(0),
) -> HistoricalDatasetBundle:
    descriptor = bar_descriptor(
        dataset_id,
        provider_family=provider_family,
        channel_id=channel_id,
        independence_group=independence_group,
    )
    bars = tuple(
        BarObservation(
            dataset_id=dataset_id,
            open_time=_START + timedelta(minutes=index),
            interval=timedelta(minutes=1),
            open=Decimal("100.00") + index + offset,
            high=Decimal("101.00") + index + offset,
            low=Decimal("99.00") + index + offset,
            close=Decimal("100.50") + index + offset,
            volume=Decimal(0),
            source_row_number=index + 2,
            raw_payload_sha256=_DIGEST,
        )
        for index in range(2)
    )
    return HistoricalDatasetBundle(
        descriptor=descriptor,
        artifacts=(verified_artifact(dataset_id),),
        bars=bars,
    )


class HistoricalGovernanceTests(unittest.TestCase):
    def test_single_structurally_valid_source_is_not_automatically_trusted(self) -> None:
        bundle = bar_bundle("source-a")
        report = HistoricalDatasetValidator().validate(bundle)

        decision = HistoricalAdmissionPolicy().evaluate(
            bundle,
            report,
            target=AdmissionTarget.TRUSTED_BAR_REFERENCE,
        )

        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertEqual(decision.resulting_state, DatasetState.VALIDATED_CANDIDATE)
        self.assertIn("INSUFFICIENT_INDEPENDENT_EXACT_CONFIRMATION", decision.blockers)

    def test_bar_data_cannot_be_promoted_as_quote_event_history(self) -> None:
        bundle = bar_bundle("source-a")
        report = HistoricalDatasetValidator().validate(bundle)

        decision = HistoricalAdmissionPolicy().evaluate(
            bundle,
            report,
            target=AdmissionTarget.TRUSTED_QUOTE_HISTORY,
        )

        self.assertFalse(decision.accepted)
        self.assertIn("TARGET_RECORD_KIND_MISMATCH", decision.blockers)

    def test_independent_exact_confirmation_can_admit_whole_source_records(self) -> None:
        left = bar_bundle("source-a")
        right = bar_bundle(
            "source-b",
            provider_family="provider-b",
            channel_id="download-b",
            independence_group="provider-b",
            offset=Decimal("0.01"),
        )
        evidence = compare_bar_datasets(
            left,
            right,
            max_deviation_bps=Decimal("2"),
            minimum_overlap=2,
        )
        report = HistoricalDatasetValidator().validate(left)

        decision = HistoricalAdmissionPolicy().evaluate(
            left,
            report,
            target=AdmissionTarget.TRUSTED_BAR_REFERENCE,
            cross_validation=(evidence,),
        )
        segment = create_canonical_segment(
            left,
            decision,
            segment_id="xau-july-source-a",
            start=_START,
            end=_START + timedelta(minutes=2),
            selection_reason="independently corroborated bid M1 segment",
        )

        self.assertTrue(evidence.passed)
        self.assertTrue(evidence.independent)
        self.assertTrue(decision.accepted)
        self.assertEqual(segment.dataset_id, left.descriptor.dataset_id)
        self.assertEqual(segment.record_kind, HistoricalRecordKind.BAR_OBSERVATION)

    def test_two_channels_from_same_provider_are_not_independent_confirmation(self) -> None:
        left = bar_bundle(
            "provider-channel-a",
            provider_family="same-provider",
            channel_id="channel-a",
            independence_group="same-provider",
        )
        right = bar_bundle(
            "provider-channel-b",
            provider_family="same-provider",
            channel_id="channel-b",
            independence_group="same-provider",
        )
        evidence = compare_bar_datasets(left, right, minimum_overlap=2)
        decision = HistoricalAdmissionPolicy().evaluate(
            left,
            HistoricalDatasetValidator().validate(left),
            target=AdmissionTarget.TRUSTED_BAR_REFERENCE,
            cross_validation=(evidence,),
        )

        self.assertTrue(evidence.passed)
        self.assertFalse(evidence.independent)
        self.assertFalse(decision.accepted)

    def test_spot_bid_and_futures_close_are_sanity_only(self) -> None:
        spot = bar_bundle("spot-bid")
        future_descriptor = replace(
            spot.descriptor,
            dataset_id="future-close",
            provider_family="future-provider",
            channel_id="future-download",
            feed_id="gc_active_daily",
            independence_group="future-provider",
            instrument_symbol="GC=F",
            instrument_type=InstrumentType.FUTURE,
            venue="CMX",
            contract_code="active-continuous",
            bar_price_basis=BarPriceBasis.PROVIDER_CLOSE,
        )
        future = HistoricalDatasetBundle(
            descriptor=future_descriptor,
            artifacts=(verified_artifact("future-close"),),
            bars=tuple(replace(row, dataset_id="future-close") for row in spot.bars),
        )

        compatibility = evaluate_compatibility(spot, future)

        self.assertEqual(compatibility.level, CompatibilityLevel.SANITY_ONLY)
        self.assertIn(
            "different instrument, venue, or contract identity",
            compatibility.reasons,
        )
        self.assertIn("different bar price basis", compatibility.reasons)

    def test_every_delivered_quote_is_preserved_despite_one_second_source_time(self) -> None:
        descriptor = HistoricalDatasetDescriptor(
            dataset_id="quote-channel-a",
            provider_family="provider-a",
            channel_id="websocket-a",
            feed_id="xauusd-last",
            independence_group="provider-a",
            instrument_symbol="XAUUSD",
            underlying="XAU",
            quote_currency="USD",
            instrument_type=InstrumentType.OTC_SPOT,
            venue="OTC",
            contract_code=None,
            record_kind=HistoricalRecordKind.QUOTE_EVENT,
            quote_fields=frozenset({QuotePriceField.LAST}),
            source_timezone="UTC",
            timestamp_semantics=TimestampSemantics.PROVIDER_EVENT_TIME,
            timestamp_resolution=timedelta(seconds=1),
            storage_price_scale=6,
            effective_price_quantum=Decimal("0.000001"),
            source_uri="wss://fixture.invalid",
            parser_version="fixture-v1",
        )
        events = (
            QuoteObservation(
                dataset_id=descriptor.dataset_id,
                event_index=0,
                source_observed_at=_START,
                received_at=_START + timedelta(milliseconds=10),
                last=Decimal("4200.000001"),
                raw_payload_sha256=_DIGEST,
            ),
            QuoteObservation(
                dataset_id=descriptor.dataset_id,
                event_index=1,
                source_observed_at=_START,
                received_at=_START + timedelta(milliseconds=30),
                last=Decimal("4200.000002"),
                raw_payload_sha256=_DIGEST,
            ),
        )
        bundle = HistoricalDatasetBundle(descriptor=descriptor, quote_events=events)

        report = HistoricalDatasetValidator().validate(bundle)

        self.assertEqual(bundle.record_count, 2)
        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertNotIn(
            "DUPLICATE_EVENT_INDEX",
            {finding.code for finding in report.findings},
        )

    def test_quote_channel_cannot_store_an_undeclared_price_field(self) -> None:
        descriptor = HistoricalDatasetDescriptor(
            dataset_id="last-only",
            provider_family="provider-a",
            channel_id="channel-a",
            feed_id="last-only",
            independence_group="provider-a",
            instrument_symbol="XAUUSD",
            underlying="XAU",
            quote_currency="USD",
            instrument_type=InstrumentType.OTC_SPOT,
            venue="OTC",
            contract_code=None,
            record_kind=HistoricalRecordKind.QUOTE_EVENT,
            quote_fields=frozenset({QuotePriceField.LAST}),
            timestamp_resolution=timedelta(seconds=1),
            storage_price_scale=2,
            effective_price_quantum=Decimal("0.01"),
            parser_version="fixture-v1",
        )
        event = QuoteObservation(
            dataset_id=descriptor.dataset_id,
            event_index=0,
            source_observed_at=_START,
            received_at=_START,
            bid=Decimal("100.00"),
            raw_payload_sha256=_DIGEST,
        )

        report = HistoricalDatasetValidator().validate(
            HistoricalDatasetBundle(descriptor=descriptor, quote_events=(event,))
        )

        self.assertEqual(report.status, ValidationStatus.FAIL)
        self.assertIn(
            "UNDECLARED_QUOTE_FIELD",
            {finding.code for finding in report.findings},
        )

    def test_invalid_ohlc_is_rejected_before_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "open must be within low and high"):
            BarObservation(
                dataset_id="bad-bar",
                open_time=_START,
                interval=timedelta(minutes=1),
                open=Decimal("102"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
            )


if __name__ == "__main__":
    unittest.main()
