from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from tracefang.domain.errors import ProviderDataError
from tracefang.domain.market_context import FuturesContractPosition

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _lots(value: object, field: str, *, optional: bool = False) -> int | None:
    if optional and (value is None or str(value).strip() == ""):
        return None
    if isinstance(value, bool) or value is None:
        raise ProviderDataError(f"SHFE positioning field {field} is not numeric")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ProviderDataError(f"SHFE positioning field {field} is not numeric") from error
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise ProviderDataError(f"SHFE positioning field {field} is not an integer lot count")
    result = int(parsed)
    if not optional and result < 0:
        raise ProviderDataError(f"SHFE positioning field {field} cannot be negative")
    return result


def _price(value: object) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ProviderDataError("SHFE positioning last price is not numeric") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ProviderDataError("SHFE positioning last price must be positive")
    return parsed


def _observed_at(value: object) -> datetime:
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        raise ProviderDataError("SHFE positioning update timestamp is invalid") from error
    return parsed.replace(tzinfo=_SHANGHAI)


def parse_positioning_payload(
    payload: Mapping[str, Any],
    *,
    expected_product_code: str,
) -> tuple[FuturesContractPosition, ...]:
    product_code = expected_product_code.strip().lower()
    if product_code not in {"au", "ag"}:
        raise ValueError(f"unsupported SHFE positioning product {expected_product_code!r}")
    rows = payload.get("delaymarket")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ProviderDataError("SHFE positioning response has no delaymarket rows")
    contract_pattern = re.compile(rf"{re.escape(product_code)}\d{{4}}", re.IGNORECASE)
    parsed_rows: list[FuturesContractPosition] = []
    seen_contracts: set[str] = set()
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise ProviderDataError("SHFE positioning row is invalid")
        contract_code = str(raw_row.get("contractname", "")).strip().lower()
        if contract_code.upper() == "IMCI":
            continue
        if contract_pattern.fullmatch(contract_code) is None:
            raise ProviderDataError("SHFE positioning returned a non-contract aggregate row")
        instrument_id = str(raw_row.get("instrumentid", "")).strip().lower()
        if instrument_id != product_code:
            raise ProviderDataError("SHFE positioning returned another product")
        if contract_code in seen_contracts:
            raise ProviderDataError("SHFE positioning returned a duplicate contract")
        seen_contracts.add(contract_code)
        volume = _lots(raw_row.get("volume"), "volume")
        open_interest = _lots(raw_row.get("openinterest"), "openinterest")
        if volume is None or open_interest is None:
            raise ProviderDataError("SHFE positioning contract counts are missing")
        parsed_rows.append(
            FuturesContractPosition(
                product_code=product_code.upper(),
                contract_code=contract_code.upper(),
                volume=volume,
                open_interest=open_interest,
                open_interest_change=_lots(
                    raw_row.get("openinterestchg"),
                    "openinterestchg",
                    optional=True,
                ),
                last_price=_price(raw_row.get("lastprice")),
                observed_at=_observed_at(raw_row.get("updatetime")),
            )
        )
    if not parsed_rows:
        raise ProviderDataError("SHFE positioning response contains no real contracts")
    return tuple(sorted(parsed_rows, key=lambda item: item.contract_code))
