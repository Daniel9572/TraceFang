from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import gcd
from pathlib import Path, PurePosixPath

from market_analysis.history_management.models import (
    ArtifactVerification,
    BarObservation,
    BarPriceBasis,
    FindingSeverity,
    HistoricalDatasetBundle,
    HistoricalDatasetDescriptor,
    HistoricalRecordKind,
    InstrumentType,
    TimestampSemantics,
    ValidationFinding,
    ValidationReport,
)
from market_analysis.history_management.validation import HistoricalDatasetValidator

_EXPECTED_HEADER = (
    "timestamp_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "price_side",
    "source",
)
_ONE_MINUTE = timedelta(minutes=1)


class HistDataPackageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HistDataLoadResult:
    bundle: HistoricalDatasetBundle
    validation: ValidationReport


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HistDataPackageError(f"{field_name} must be an object")
    return value


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistDataPackageError(f"{field_name} must be a non-empty string")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc(value: str, row_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise HistDataPackageError(
            f"invalid UTC timestamp at normalized CSV row {row_number}: {value}"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistDataPackageError(
            f"naive timestamp at normalized CSV row {row_number}: {value}"
        )
    return parsed.astimezone(UTC)


def _observed_precision(values: list[Decimal]) -> tuple[int, Decimal]:
    if not values:
        raise HistDataPackageError("cannot infer price precision from an empty dataset")
    storage_scale = max(max(0, -value.as_tuple().exponent) for value in values)
    unit_gcd = 0
    for value in values:
        scaled = value.scaleb(storage_scale)
        if scaled != scaled.to_integral_value():
            raise HistDataPackageError("price cannot be represented at inferred storage scale")
        unit_gcd = gcd(unit_gcd, abs(int(scaled)))
    if unit_gcd == 0:
        raise HistDataPackageError("cannot infer a positive price quantum")
    return storage_scale, Decimal(unit_gcd).scaleb(-storage_scale)


class HistDataPackageLoader:
    """Loads a previously downloaded HistData package without network access."""

    def __init__(self, package_root: Path) -> None:
        self._root = package_root.resolve()
        manifest_path = self._root / "manifest.json"
        if not manifest_path.is_file():
            raise HistDataPackageError(f"missing manifest: {manifest_path}")
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HistDataPackageError(f"invalid manifest: {manifest_path}") from error
        self._manifest = _require_mapping(parsed, "manifest")
        artifacts = self._manifest.get("artifacts")
        if not isinstance(artifacts, list):
            raise HistDataPackageError("manifest.artifacts must be an array")
        self._artifacts: dict[str, Mapping[str, object]] = {}
        for item in artifacts:
            artifact = _require_mapping(item, "manifest artifact")
            relative_path = _require_string(artifact.get("path"), "artifact.path")
            self._artifacts[relative_path] = artifact

    def load(self, symbol: str) -> HistDataLoadResult:
        normalized_relative = self._find_artifact(
            prefix=f"normalized/{symbol}_bid_1m_",
            suffix="_UTC.csv",
        )
        raw_relative = self._find_artifact(
            prefix=f"raw/DAT_ASCII_{symbol}_M1_",
            suffix=".zip",
        )
        report_relative = "reports/validation.json"
        verifications = tuple(
            self.verify_artifact(relative_path)
            for relative_path in (raw_relative, normalized_relative, report_relative)
        )
        failed = [item.relative_path for item in verifications if not item.integrity_verified]
        if failed:
            raise HistDataPackageError(
                "artifact integrity check failed before parsing: " + ", ".join(failed)
            )

        package_id = _require_string(self._manifest.get("package_id"), "package_id")
        dataset_id = f"{package_id}:{symbol}:bid:1m"
        bars, prices = self._read_normalized_csv(
            self._safe_path(normalized_relative),
            dataset_id=dataset_id,
        )
        storage_scale, price_quantum = _observed_precision(prices)
        source = _require_mapping(self._manifest.get("source"), "source")
        source_urls = _require_mapping(source.get("urls"), "source.urls")
        normalization = _require_mapping(
            self._manifest.get("normalization"),
            "normalization",
        )
        external_report = self._read_external_validation(symbol)

        descriptor = HistoricalDatasetDescriptor(
            dataset_id=dataset_id,
            provider_family="histdata",
            channel_id="histdata_free_ascii_download",
            feed_id="histdata_generic_ascii_m1_bid",
            independence_group="histdata",
            instrument_symbol=symbol,
            underlying=symbol[:-3] if symbol.endswith("USD") else symbol,
            quote_currency="USD" if symbol.endswith("USD") else None,
            instrument_type=InstrumentType.OTC_SPOT,
            venue="OTC",
            contract_code=None,
            record_kind=HistoricalRecordKind.BAR_OBSERVATION,
            bar_price_basis=BarPriceBasis.BID,
            bar_interval=_ONE_MINUTE,
            source_timezone=_require_string(source.get("source_timezone"), "source_timezone"),
            normalized_timezone="UTC",
            timestamp_semantics=TimestampSemantics.BAR_OPEN_TIME,
            timestamp_resolution=_ONE_MINUTE,
            storage_price_scale=storage_scale,
            effective_price_quantum=price_quantum,
            source_uri=_require_string(
                source_urls.get(f"{symbol.lower()}_2026_07"),
                f"source.urls.{symbol.lower()}_2026_07",
            ),
            parser_version="histdata-normalized-csv-v1",
            metadata={
                "package_id": package_id,
                "format": source.get("format"),
                "normalization": dict(normalization),
                "manifest_validation_result": self._manifest.get("validation_result"),
                "external_validation": external_report,
                "normalized_artifact": normalized_relative,
                "raw_artifact": raw_relative,
                "precision_kind": "observed lattice, not publisher guarantee",
            },
        )
        bundle = HistoricalDatasetBundle(
            descriptor=descriptor,
            artifacts=verifications,
            bars=bars,
        )
        validation = HistoricalDatasetValidator().validate(bundle)
        expected_rows = external_report.get("normalized_row_count")
        if not isinstance(expected_rows, int) or expected_rows != len(bars):
            validation = replace(
                validation,
                findings=(
                    *validation.findings,
                    ValidationFinding(
                        code="PACKAGE_ROW_COUNT_MISMATCH",
                        severity=FindingSeverity.ERROR,
                        message="parsed row count differs from the package validation report",
                        details={
                            "expected_rows": expected_rows,
                            "parsed_rows": len(bars),
                        },
                    ),
                ),
            )
        return HistDataLoadResult(bundle=bundle, validation=validation)

    def verify_artifact(self, relative_path: str) -> ArtifactVerification:
        metadata = self._artifacts.get(relative_path)
        if metadata is None:
            raise HistDataPackageError(f"artifact not listed in manifest: {relative_path}")
        path = self._safe_path(relative_path)
        if not path.is_file():
            raise HistDataPackageError(f"artifact is missing: {relative_path}")
        expected_bytes = metadata.get("bytes")
        if not isinstance(expected_bytes, int):
            raise HistDataPackageError(f"invalid artifact byte length: {relative_path}")
        expected_sha256 = _require_string(
            metadata.get("sha256"),
            f"artifact.sha256[{relative_path}]",
        )
        return ArtifactVerification(
            artifact_id=f"manifest:{relative_path}",
            relative_path=relative_path,
            expected_bytes=expected_bytes,
            actual_bytes=path.stat().st_size,
            expected_sha256=expected_sha256,
            actual_sha256=_sha256_file(path),
            publisher_authentication=None,
        )

    def _find_artifact(self, *, prefix: str, suffix: str) -> str:
        matches = [
            path
            for path in self._artifacts
            if path.startswith(prefix) and path.endswith(suffix)
        ]
        if len(matches) != 1:
            raise HistDataPackageError(
                f"expected one manifest artifact matching {prefix}*{suffix}, found {len(matches)}"
            )
        return matches[0]

    def _safe_path(self, relative_path: str) -> Path:
        posix_path = PurePosixPath(relative_path)
        if posix_path.is_absolute() or ".." in posix_path.parts:
            raise HistDataPackageError(f"unsafe artifact path: {relative_path}")
        resolved = (self._root / Path(*posix_path.parts)).resolve()
        if not resolved.is_relative_to(self._root):
            raise HistDataPackageError(f"artifact escapes package root: {relative_path}")
        return resolved

    @staticmethod
    def _read_normalized_csv(
        path: Path,
        *,
        dataset_id: str,
    ) -> tuple[tuple[BarObservation, ...], list[Decimal]]:
        bars: list[BarObservation] = []
        prices: list[Decimal] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            header_line = handle.readline()
            header = tuple(next(csv.reader([header_line])))
            if header != _EXPECTED_HEADER:
                raise HistDataPackageError(
                    f"unexpected normalized CSV header in {path.name}: {header}"
                )
            for row_number, raw_line in enumerate(handle, start=2):
                if not raw_line.strip():
                    continue
                values = next(csv.reader([raw_line]))
                if len(values) != len(_EXPECTED_HEADER):
                    raise HistDataPackageError(
                        f"unexpected field count at normalized CSV row {row_number}"
                    )
                row = dict(zip(_EXPECTED_HEADER, values, strict=True))
                if row["price_side"].lower() != "bid" or row["source"].lower() != "histdata":
                    raise HistDataPackageError(
                        f"unexpected price semantics at normalized CSV row {row_number}"
                    )
                try:
                    open_price = Decimal(row["open"])
                    high = Decimal(row["high"])
                    low = Decimal(row["low"])
                    close = Decimal(row["close"])
                    volume = Decimal(row["volume"])
                except Exception as error:
                    raise HistDataPackageError(
                        f"invalid decimal at normalized CSV row {row_number}"
                    ) from error
                prices.extend((open_price, high, low, close))
                bars.append(
                    BarObservation(
                        dataset_id=dataset_id,
                        open_time=_parse_utc(row["timestamp_utc"], row_number),
                        interval=_ONE_MINUTE,
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                        source_row_number=row_number,
                        raw_payload_sha256=hashlib.sha256(
                            raw_line.encode("utf-8")
                        ).hexdigest(),
                    )
                )
        return tuple(bars), prices

    def _read_external_validation(self, symbol: str) -> Mapping[str, object]:
        path = self._safe_path("reports/validation.json")
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HistDataPackageError("invalid package validation report") from error
        report_mapping = _require_mapping(report, "validation report")
        internal = _require_mapping(
            report_mapping.get("internal_validation"),
            "internal_validation",
        )
        return _require_mapping(internal.get(symbol), f"internal_validation.{symbol}")
