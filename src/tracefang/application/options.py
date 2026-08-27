from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol

from tracefang.domain.errors import ProviderError
from tracefang.domain.options import (
    OptionChainSnapshot,
    OptionContractQuote,
    OptionDeliveryMode,
    OptionType,
    OptionUnderlyingQuote,
)


class GoldOptionChainProvider(Protocol):
    name: str
    market_id: str
    market_label: str
    delivery_mode: OptionDeliveryMode

    async def get_chain(self) -> OptionChainSnapshot: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GoldOptionMarketStatus:
    market_id: str
    label: str
    state: str
    detail: str
    delivery_mode: str | None
    quote_count: int
    observed_at: datetime | None
    required_data: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoldOptionExpiryAnalysis:
    underlying_contract_id: str
    expiry: date
    underlying_price: Decimal | None
    option_count: int
    call_open_interest: int
    put_open_interest: int
    put_call_open_interest_ratio: Decimal | None
    call_volume: int
    put_volume: int
    put_call_volume_ratio: Decimal | None
    atm_strike: Decimal | None
    call_wall_strike: Decimal | None
    put_wall_strike: Decimal | None
    max_pain_strike: Decimal | None
    reference_iv: Decimal | None
    expected_move_percent: Decimal | None
    delta_coverage_ratio: Decimal
    positioning_state: str
    gamma_state: str
    gex: None = None


@dataclass(frozen=True, slots=True)
class GoldOptionsSnapshot:
    contract_version: str
    state: str
    available: bool
    provider_id: str | None
    market_id: str | None
    delivery_mode: str | None
    checked_at: datetime
    observed_at: datetime | None
    trading_day: date | None
    reference_data_as_of: date | None
    quote_currency: str | None
    price_unit: str | None
    quote_count: int
    markets: tuple[GoldOptionMarketStatus, ...]
    expiries: tuple[GoldOptionExpiryAnalysis, ...]
    contracts: tuple[OptionContractQuote, ...]
    underlyings: tuple[OptionUnderlyingQuote, ...]
    source_urls: tuple[str, ...]
    required_quote_fields: tuple[str, ...]
    analysis_state: str
    detail: str
    limitations: tuple[str, ...]
    usage_notice: str
    refresh_after_seconds: float


_SHFE_REQUIRED_DATA = (
    "官方延时期权买卖价、最新价、成交量与持仓量",
    "官方合约到期日、行权价、合约乘数与对应标的",
    "上一交易日逐合约 Delta 与分月份 IV 参考值",
)
_CME_REQUIRED_DATA = (
    "CME Real-Time Futures & Options WebSocket API 订阅",
    "CME API ID、认证凭据与 COMEX 贵金属数据授权",
    "若需官方 Greeks/IV, 另需 Options Analytics 数据权限",
)
_REQUIRED_QUOTE_FIELDS = (
    "contract_id",
    "underlying_contract_id",
    "expiry",
    "strike",
    "option_type",
    "bid",
    "ask",
    "last",
    "volume",
    "open_interest",
    "observed_at",
    "source_id",
)
_LIMITATIONS = (
    "上期所公开源为交易所延时行情, 不得标记为实时行情。",
    "逐合约 Delta 与月份 IV 来自上一交易日日报, 不代表盘中 Greeks。",
    "公开源不含逐合约 Gamma/Vega, 也没有做市商净头寸, 不能生成可信方向性 GEX。",
    "沪金以人民币/克计价, 与 XAUUSD 现货存在汇率、期限和基差差异。",
)


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))


def _wall(quotes: Sequence[OptionContractQuote], option_type: OptionType) -> Decimal | None:
    candidates = [item for item in quotes if item.option_type is option_type]
    if not candidates or max(item.open_interest for item in candidates) <= 0:
        return None
    return max(candidates, key=lambda item: (item.open_interest, -item.strike)).strike


def _max_pain(quotes: Sequence[OptionContractQuote]) -> Decimal | None:
    strikes = sorted({item.strike for item in quotes})
    if not strikes:
        return None
    best_strike: Decimal | None = None
    best_payout: Decimal | None = None
    for settlement in strikes:
        payout = Decimal("0")
        for quote in quotes:
            intrinsic = (
                max(Decimal("0"), settlement - quote.strike)
                if quote.option_type is OptionType.CALL
                else max(Decimal("0"), quote.strike - settlement)
            )
            payout += intrinsic * quote.open_interest * quote.contract_multiplier
        if best_payout is None or payout < best_payout:
            best_strike = settlement
            best_payout = payout
    return best_strike


def _positioning_state(put_call_open_interest_ratio: Decimal | None) -> str:
    if put_call_open_interest_ratio is None:
        return "insufficient"
    if put_call_open_interest_ratio >= Decimal("1.2"):
        return "put_open_interest_dominant"
    if put_call_open_interest_ratio <= Decimal("0.8"):
        return "call_open_interest_dominant"
    return "balanced_open_interest"


def _expiry_analyses(chain: OptionChainSnapshot) -> tuple[GoldOptionExpiryAnalysis, ...]:
    grouped: dict[tuple[str, date], list[OptionContractQuote]] = {}
    for quote in chain.quotes:
        grouped.setdefault((quote.underlying_contract_id, quote.expiry), []).append(quote)
    rows: list[GoldOptionExpiryAnalysis] = []
    for (underlying_id, expiry), quotes in grouped.items():
        calls = [item for item in quotes if item.option_type is OptionType.CALL]
        puts = [item for item in quotes if item.option_type is OptionType.PUT]
        call_oi = sum(item.open_interest for item in calls)
        put_oi = sum(item.open_interest for item in puts)
        call_volume = sum(item.volume for item in calls)
        put_volume = sum(item.volume for item in puts)
        put_call_oi = _ratio(put_oi, call_oi)
        underlying = chain.underlyings.get(underlying_id)
        underlying_price = underlying.last if underlying is not None else None
        atm_strike = (
            min({item.strike for item in quotes}, key=lambda strike: abs(strike - underlying_price))
            if underlying_price is not None
            else None
        )
        reference_iv = chain.reference_iv_by_underlying.get(underlying_id)
        days_to_expiry = max(0, (expiry - chain.trading_day).days)
        expected_move = (
            Decimal(str(float(reference_iv) * math.sqrt(days_to_expiry / 365) * 100)).quantize(
                Decimal("0.01")
            )
            if reference_iv is not None and days_to_expiry > 0
            else None
        )
        delta_count = sum(item.delta is not None for item in quotes)
        rows.append(
            GoldOptionExpiryAnalysis(
                underlying_contract_id=underlying_id,
                expiry=expiry,
                underlying_price=underlying_price,
                option_count=len(quotes),
                call_open_interest=call_oi,
                put_open_interest=put_oi,
                put_call_open_interest_ratio=put_call_oi,
                call_volume=call_volume,
                put_volume=put_volume,
                put_call_volume_ratio=_ratio(put_volume, call_volume),
                atm_strike=atm_strike,
                call_wall_strike=_wall(quotes, OptionType.CALL),
                put_wall_strike=_wall(quotes, OptionType.PUT),
                max_pain_strike=_max_pain(quotes),
                reference_iv=reference_iv,
                expected_move_percent=expected_move,
                delta_coverage_ratio=(
                    Decimal(delta_count) / Decimal(len(quotes))
                ).quantize(Decimal("0.0001")),
                positioning_state=_positioning_state(put_call_oi),
                gamma_state="unavailable_missing_contract_gamma_and_dealer_position",
            )
        )
    rows.sort(key=lambda item: (item.expiry, item.underlying_contract_id))
    return tuple(rows)


def unconfigured_gold_options_snapshot() -> GoldOptionsSnapshot:
    checked_at = datetime.now(UTC)
    return GoldOptionsSnapshot(
        contract_version="gold-options-v2",
        state="unconfigured",
        available=False,
        provider_id=None,
        market_id=None,
        delivery_mode=None,
        checked_at=checked_at,
        observed_at=None,
        trading_day=None,
        reference_data_as_of=None,
        quote_currency=None,
        price_unit=None,
        quote_count=0,
        markets=(
            GoldOptionMarketStatus(
                market_id="shfe_gold_options",
                label="上海期货交易所黄金期权",
                state="provider_required",
                detail="尚未配置可用的上期所黄金期权供应商。",
                delivery_mode=None,
                quote_count=0,
                observed_at=None,
                required_data=_SHFE_REQUIRED_DATA,
            ),
            GoldOptionMarketStatus(
                market_id="cme_comex_gold_options",
                label="CME/COMEX 黄金期权",
                state="provider_and_entitlement_required",
                detail="本机没有 CME API 订阅与贵金属行情授权。",
                delivery_mode=None,
                quote_count=0,
                observed_at=None,
                required_data=_CME_REQUIRED_DATA,
            ),
        ),
        expiries=(),
        contracts=(),
        underlyings=(),
        source_urls=(),
        required_quote_fields=_REQUIRED_QUOTE_FIELDS,
        analysis_state="blocked_without_market_data",
        detail="当前没有可用的黄金期权行情提供方。",
        limitations=_LIMITATIONS,
        usage_notice="不得把未授权或非实时数据重新标记为实时行情。",
        refresh_after_seconds=15,
    )


class GoldOptionsService:
    """One provider-neutral business pipeline for every gold-option source."""

    def __init__(
        self,
        providers: Sequence[GoldOptionChainProvider],
        *,
        refresh_after_seconds: float = 10,
    ) -> None:
        if refresh_after_seconds <= 0:
            raise ValueError("gold option refresh interval must be positive")
        self._providers = tuple(providers)
        self._refresh_after_seconds = refresh_after_seconds

    async def close(self) -> None:
        await asyncio.gather(*(provider.close() for provider in self._providers))

    async def snapshot(self) -> GoldOptionsSnapshot:
        checked_at = datetime.now(UTC)
        market_statuses: list[GoldOptionMarketStatus] = []
        available_chains: list[OptionChainSnapshot] = []
        for provider in self._providers:
            try:
                chain = await provider.get_chain()
            except ProviderError as error:
                market_statuses.append(
                    GoldOptionMarketStatus(
                        market_id=provider.market_id,
                        label=provider.market_label,
                        state="unavailable",
                        detail=str(error),
                        delivery_mode=provider.delivery_mode.value,
                        quote_count=0,
                        observed_at=None,
                        required_data=(
                            _SHFE_REQUIRED_DATA
                            if provider.market_id == "shfe_gold_options"
                            else _CME_REQUIRED_DATA
                        ),
                    )
                )
                continue
            available_chains.append(chain)
            market_statuses.append(
                GoldOptionMarketStatus(
                    market_id=chain.market_id,
                    label=chain.market_label,
                    state=(
                        "live"
                        if chain.delivery_mode is OptionDeliveryMode.LIVE
                        else "delayed"
                    ),
                    detail=(
                        f"已取得 {len(chain.quotes)} 个真实合约; "
                        f"行情截至 {chain.observed_at.isoformat()}。"
                    ),
                    delivery_mode=chain.delivery_mode.value,
                    quote_count=len(chain.quotes),
                    observed_at=chain.observed_at,
                    required_data=_SHFE_REQUIRED_DATA,
                )
            )
        if not any(item.market_id == "cme_comex_gold_options" for item in market_statuses):
            market_statuses.append(
                GoldOptionMarketStatus(
                    market_id="cme_comex_gold_options",
                    label="CME/COMEX 黄金期权",
                    state="provider_and_entitlement_required",
                    detail="本机没有 CME API 订阅与贵金属行情授权。",
                    delivery_mode=None,
                    quote_count=0,
                    observed_at=None,
                    required_data=_CME_REQUIRED_DATA,
                )
            )
        if not available_chains:
            fallback = unconfigured_gold_options_snapshot()
            state = "unavailable" if self._providers else fallback.state
            return replace(
                fallback,
                state=state,
                checked_at=checked_at,
                markets=tuple(market_statuses) or fallback.markets,
                detail=(
                    "黄金期权供应商请求失败。" if self._providers else fallback.detail
                ),
            )
        primary = available_chains[0]
        state = "live" if primary.delivery_mode is OptionDeliveryMode.LIVE else "delayed"
        return GoldOptionsSnapshot(
            contract_version="gold-options-v2",
            state=state,
            available=True,
            provider_id=primary.provider_id,
            market_id=primary.market_id,
            delivery_mode=primary.delivery_mode.value,
            checked_at=checked_at,
            observed_at=primary.observed_at,
            trading_day=primary.trading_day,
            reference_data_as_of=primary.reference_data_as_of,
            quote_currency=primary.quote_currency,
            price_unit=primary.price_unit,
            quote_count=len(primary.quotes),
            markets=tuple(market_statuses),
            expiries=_expiry_analyses(primary),
            contracts=primary.quotes,
            underlyings=tuple(primary.underlyings[key] for key in sorted(primary.underlyings)),
            source_urls=primary.source_urls,
            required_quote_fields=_REQUIRED_QUOTE_FIELDS,
            analysis_state="available_with_limitations",
            detail=(
                "已接入上期所官方公开延时期权链; 持仓墙、Put/Call 比和最大痛点"
                "来自真实报价与持仓, GEX 因缺少逐合约 Gamma 和做市商净头寸而不计算。"
            ),
            limitations=_LIMITATIONS,
            usage_notice=primary.usage_notice,
            refresh_after_seconds=self._refresh_after_seconds,
        )

    @staticmethod
    def ai_context(snapshot: GoldOptionsSnapshot) -> dict[str, object]:
        return {
            "state": snapshot.state,
            "provider_id": snapshot.provider_id,
            "market_id": snapshot.market_id,
            "delivery_mode": snapshot.delivery_mode,
            "observed_at": snapshot.observed_at.isoformat() if snapshot.observed_at else None,
            "reference_data_as_of": (
                snapshot.reference_data_as_of.isoformat()
                if snapshot.reference_data_as_of
                else None
            ),
            "quote_count": snapshot.quote_count,
            "unit": snapshot.price_unit,
            "scope_note": "SHFE gold futures options; not the same instrument as XAUUSD spot",
            "expiries": [
                {
                    "underlying": item.underlying_contract_id,
                    "expiry": item.expiry.isoformat(),
                    "underlying_price": (
                        float(item.underlying_price) if item.underlying_price is not None else None
                    ),
                    "put_call_oi_ratio": (
                        float(item.put_call_open_interest_ratio)
                        if item.put_call_open_interest_ratio is not None
                        else None
                    ),
                    "put_call_volume_ratio": (
                        float(item.put_call_volume_ratio)
                        if item.put_call_volume_ratio is not None
                        else None
                    ),
                    "call_wall": (
                        float(item.call_wall_strike)
                        if item.call_wall_strike is not None
                        else None
                    ),
                    "put_wall": (
                        float(item.put_wall_strike)
                        if item.put_wall_strike is not None
                        else None
                    ),
                    "max_pain": (
                        float(item.max_pain_strike)
                        if item.max_pain_strike is not None
                        else None
                    ),
                    "reference_iv": (
                        float(item.reference_iv) if item.reference_iv is not None else None
                    ),
                    "positioning_state": item.positioning_state,
                    "gamma_state": item.gamma_state,
                }
                for item in snapshot.expiries[:6]
            ],
            "limitations": list(snapshot.limitations),
        }
