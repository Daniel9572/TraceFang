from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from tracefang.domain.errors import ProviderDataError

_CHICAGO = ZoneInfo("America/Chicago")


@dataclass(frozen=True, slots=True)
class CboeDelayedQuote:
    index_code: str
    value: Decimal
    change: Decimal | None
    change_percent: Decimal | None
    session_open: Decimal | None
    session_high: Decimal | None
    session_low: Decimal | None
    previous_close: Decimal | None
    observed_at: datetime
    published_at: datetime


@dataclass(frozen=True, slots=True)
class CboeHistoryPoint:
    trading_date: date
    close: Decimal


def _decimal(value: object, field: str, *, optional: bool = False) -> Decimal | None:
    if optional and (value is None or str(value).strip() == ""):
        return None
    if isinstance(value, bool) or value is None:
        raise ProviderDataError(f"Cboe volatility field {field} is not numeric")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ProviderDataError(f"Cboe volatility field {field} is not numeric") from error
    if not parsed.is_finite():
        raise ProviderDataError(f"Cboe volatility field {field} is not finite")
    return parsed


def _published_at(value: object) -> datetime:
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        raise ProviderDataError("Cboe volatility publication timestamp is invalid") from error
    return parsed.replace(tzinfo=UTC)


def _observed_at(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise ProviderDataError("Cboe volatility trade timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_CHICAGO)
    return parsed.astimezone(UTC)


def parse_delayed_quote(
    payload: Mapping[str, Any],
    *,
    expected_index_code: str,
) -> CboeDelayedQuote:
    index_code = expected_index_code.upper()
    if str(payload.get("symbol", "")).upper() != f"_{index_code}":
        raise ProviderDataError("Cboe delayed quote returned another root symbol")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ProviderDataError("Cboe delayed quote data is missing")
    if str(data.get("symbol", "")).upper() != f"^{index_code}":
        raise ProviderDataError("Cboe delayed quote returned another index")
    value = _decimal(data.get("current_price"), "current_price")
    if value is None or value <= 0:
        raise ProviderDataError("Cboe volatility index value must be positive")
    return CboeDelayedQuote(
        index_code=index_code,
        value=value,
        change=_decimal(data.get("price_change"), "price_change", optional=True),
        change_percent=_decimal(
            data.get("price_change_percent"),
            "price_change_percent",
            optional=True,
        ),
        session_open=_decimal(data.get("open"), "open", optional=True),
        session_high=_decimal(data.get("high"), "high", optional=True),
        session_low=_decimal(data.get("low"), "low", optional=True),
        previous_close=_decimal(data.get("prev_day_close"), "prev_day_close", optional=True),
        observed_at=_observed_at(data.get("last_trade_time")),
        published_at=_published_at(payload.get("timestamp")),
    )


def parse_history_csv(text: str, *, expected_index_code: str) -> tuple[CboeHistoryPoint, ...]:
    index_code = expected_index_code.upper()
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames is None:
        raise ProviderDataError("Cboe volatility history has no header")
    normalized_names = {name.strip().upper(): name for name in reader.fieldnames if name}
    value_column = "CLOSE" if index_code == "VIX" else index_code
    if "DATE" not in normalized_names or value_column not in normalized_names:
        raise ProviderDataError("Cboe volatility history columns are invalid")
    by_date: dict[date, CboeHistoryPoint] = {}
    for row in reader:
        raw_date = row.get(normalized_names["DATE"])
        raw_value = row.get(normalized_names[value_column])
        if raw_date is None or raw_value is None or not raw_date.strip() or not raw_value.strip():
            continue
        try:
            trading_date = datetime.strptime(raw_date.strip(), "%m/%d/%Y").date()
        except ValueError as error:
            raise ProviderDataError("Cboe volatility history date is invalid") from error
        close = _decimal(raw_value, f"history.{value_column}")
        if close is None or close <= 0:
            raise ProviderDataError("Cboe volatility history value must be positive")
        by_date[trading_date] = CboeHistoryPoint(trading_date=trading_date, close=close)
    if not by_date:
        raise ProviderDataError("Cboe volatility history contains no values")
    return tuple(by_date[key] for key in sorted(by_date))


def trailing_percentile(
    history: tuple[CboeHistoryPoint, ...],
    *,
    value: Decimal,
    before: date,
    window: int = 252,
) -> tuple[Decimal | None, tuple[CboeHistoryPoint, ...]]:
    if window < 1:
        raise ValueError("percentile window must be positive")
    sample = tuple(item for item in history if item.trading_date < before)[-window:]
    if not sample:
        return None, ()
    rank = sum(item.close <= value for item in sample)
    percentile = (Decimal(rank) * Decimal(100) / Decimal(len(sample))).quantize(Decimal("0.01"))
    return percentile, sample
