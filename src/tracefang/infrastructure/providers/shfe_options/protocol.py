from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from tracefang.domain.errors import ProviderDataError
from tracefang.domain.options import (
    OptionChainSnapshot,
    OptionContractQuote,
    OptionDeliveryMode,
    OptionType,
    OptionUnderlyingQuote,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CONTRACT_PATTERN = re.compile(r"^(au\d{4})([CP])(\d+(?:\.\d+)?)$")
_DELTA_ROUNDING_TOLERANCE = Decimal("0.00001")


def _rows(payload: Mapping[str, Any], key: str, *, required: bool = True) -> Sequence[Any]:
    value = payload.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list):
        raise ProviderDataError(f"SHFE option payload field {key!r} must be an array")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderDataError(f"SHFE option field {field!r} must be an object")
    return value


def _required_string(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProviderDataError(f"SHFE option field {field!r} is missing")
    return value.strip()


def _optional_decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    value = row.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ProviderDataError(f"SHFE option field {field!r} is not numeric") from error
    if not result.is_finite():
        raise ProviderDataError(f"SHFE option field {field!r} is not finite")
    return result


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = _optional_decimal(row, field)
    if value is None:
        raise ProviderDataError(f"SHFE option field {field!r} is missing")
    return value


def _optional_delta(row: Mapping[str, Any]) -> Decimal | None:
    value = _optional_decimal(row, "DELTA")
    if value is None or Decimal("-1") <= value <= Decimal("1"):
        return value
    # SHFE's EOD model can emit +/-1.000001 for deep ITM options. Normalize
    # only this observed numeric boundary noise; larger violations are
    # treated as unavailable instead of being surfaced as a fabricated Greek.
    if Decimal("1") < value <= Decimal("1") + _DELTA_ROUNDING_TOLERANCE:
        return Decimal("1")
    if Decimal("-1") - _DELTA_ROUNDING_TOLERANCE <= value < Decimal("-1"):
        return Decimal("-1")
    return None


def _lots(row: Mapping[str, Any], field: str) -> int:
    value = _optional_decimal(row, field)
    if value is None:
        return 0
    integral = value.to_integral_value()
    if value != integral or integral < 0:
        raise ProviderDataError(f"SHFE option field {field!r} must be non-negative lots")
    return int(integral)


def _signed_lots(row: Mapping[str, Any], field: str) -> int:
    value = _optional_decimal(row, field)
    if value is None:
        return 0
    integral = value.to_integral_value()
    if value != integral:
        raise ProviderDataError(f"SHFE option field {field!r} must be integral lots")
    return int(integral)


def _parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as error:
        raise ProviderDataError(f"SHFE option field {field!r} is not YYYYMMDD") from error


def _parse_observed_at(value: str) -> datetime:
    try:
        local = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_SHANGHAI)
    except ValueError as error:
        raise ProviderDataError("SHFE option update time is invalid") from error
    return local.astimezone(UTC)


def parse_shfe_gold_option_chain(
    *,
    trading_day_payload: Mapping[str, Any],
    delayed_option_payload: Mapping[str, Any],
    delayed_future_payload: Mapping[str, Any],
    contract_payload: Mapping[str, Any],
    daily_payload: Mapping[str, Any],
    retrieved_at: datetime,
    source_urls: tuple[str, ...],
) -> OptionChainSnapshot:
    current_trading_day = _parse_date(
        _required_string(trading_day_payload, "currentTradingday"),
        "currentTradingday",
    )
    reference_date = _parse_date(
        _required_string(trading_day_payload, "lastTradingday"),
        "lastTradingday",
    )
    contract_rows = _rows(contract_payload, "OptionContractBaseInfo")
    contracts: dict[str, Mapping[str, Any]] = {}
    for raw in contract_rows:
        row = _mapping(raw, "OptionContractBaseInfo[]")
        if str(row.get("COMMODITYID", "")).strip().lower() != "au":
            continue
        contract_id = _required_string(row, "INSTRUMENTID")
        if _CONTRACT_PATTERN.fullmatch(contract_id):
            contracts[contract_id] = row
    if not contracts:
        raise ProviderDataError("SHFE contract master contains no gold options")

    daily_delta: dict[str, Decimal] = {}
    for raw in _rows(daily_payload, "o_curinstrument", required=False):
        row = _mapping(raw, "o_curinstrument[]")
        contract_id = str(row.get("INSTRUMENTID", "")).strip()
        if contract_id not in contracts:
            continue
        delta = _optional_delta(row)
        if delta is not None:
            daily_delta[contract_id] = delta

    reference_iv: dict[str, Decimal] = {}
    for raw in _rows(daily_payload, "o_cursigma", required=False):
        row = _mapping(raw, "o_cursigma[]")
        if str(row.get("PRODUCTID", "")).strip().lower() != "au_o":
            continue
        underlying_id = _required_string(row, "INSTRUMENTID")
        sigma = _optional_decimal(row, "SIGMA")
        if sigma is not None:
            reference_iv[underlying_id] = sigma

    underlyings: dict[str, OptionUnderlyingQuote] = {}
    observed_times: list[datetime] = []
    for raw in _rows(delayed_future_payload, "delaymarket"):
        row = _mapping(raw, "delaymarket[]")
        contract_id = _required_string(row, "contractname")
        if not contract_id.startswith("au"):
            continue
        observed_at = _parse_observed_at(_required_string(row, "updatetime"))
        observed_times.append(observed_at)
        underlyings[contract_id] = OptionUnderlyingQuote(
            contract_id=contract_id,
            bid=_optional_decimal(row, "bidprice"),
            ask=_optional_decimal(row, "askprice"),
            last=_optional_decimal(row, "lastprice"),
            previous_settlement=_optional_decimal(row, "presettlementprice"),
            volume=_lots(row, "volume"),
            open_interest=_lots(row, "openinterest"),
            observed_at=observed_at,
        )

    quotes: list[OptionContractQuote] = []
    missing_contracts: list[str] = []
    for raw in _rows(delayed_option_payload, "delaymarket"):
        row = _mapping(raw, "delaymarket[]")
        contract_id = _required_string(row, "contractname")
        match = _CONTRACT_PATTERN.fullmatch(contract_id)
        if match is None:
            continue
        contract = contracts.get(contract_id)
        if contract is None:
            missing_contracts.append(contract_id)
            continue
        underlying_id, option_code, strike_text = match.groups()
        observed_at = _parse_observed_at(_required_string(row, "updatetime"))
        observed_times.append(observed_at)
        quotes.append(
            OptionContractQuote(
                contract_id=contract_id,
                underlying_contract_id=underlying_id,
                expiry=_parse_date(_required_string(contract, "EXPIREDATE"), "EXPIREDATE"),
                strike=Decimal(strike_text),
                option_type=OptionType.CALL if option_code == "C" else OptionType.PUT,
                contract_multiplier=_required_decimal(contract, "TRADEUNIT"),
                bid=_optional_decimal(row, "bidprice"),
                ask=_optional_decimal(row, "askprice"),
                last=_optional_decimal(row, "lastprice"),
                previous_settlement=_optional_decimal(row, "presettlementprice"),
                volume=_lots(row, "volume"),
                open_interest=_lots(row, "openinterest"),
                open_interest_change=_signed_lots(row, "openinterestchg"),
                turnover=_optional_decimal(row, "turnover"),
                observed_at=observed_at,
                delta=daily_delta.get(contract_id),
                delta_as_of=reference_date if contract_id in daily_delta else None,
            )
        )
    if not quotes:
        raise ProviderDataError("SHFE delayed feed contains no gold option quotes")
    if len(missing_contracts) > max(5, len(quotes) // 100):
        raise ProviderDataError("SHFE option master and delayed quote set are inconsistent")
    if not observed_times:
        raise ProviderDataError("SHFE option feed has no observation timestamp")
    return OptionChainSnapshot(
        provider_id="shfe_official_delayed",
        market_id="shfe_gold_options",
        market_label="上海期货交易所黄金期权",
        delivery_mode=OptionDeliveryMode.EXCHANGE_DELAYED,
        trading_day=current_trading_day,
        reference_data_as_of=reference_date,
        observed_at=max(observed_times),
        retrieved_at=retrieved_at,
        quote_currency="CNY",
        price_unit="CNY_PER_GRAM",
        quotes=tuple(sorted(quotes, key=lambda item: (item.expiry, item.strike, item.option_type))),
        underlyings=underlyings,
        reference_iv_by_underlying=reference_iv,
        source_urls=source_urls,
        usage_notice=(
            "仅用于本地研究展示。期权交易信息归上期所管理; 未经许可不得对外发布或"
            "用于未获授权的商业再分发。"
        ),
    )
