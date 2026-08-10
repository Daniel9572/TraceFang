from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

import httpx

from market_analysis.domain.errors import ProviderDataError, ProviderUnavailableError
from market_analysis.domain.market_context import (
    DirectionalInference,
    FuturesPositioningContext,
    MarketContextSource,
    PositionCountingMethod,
)
from market_analysis.infrastructure.providers.shfe_positioning.protocol import (
    parse_positioning_payload,
)
from market_analysis.infrastructure.providers.shfe_positioning.settings import (
    ShfePositioningSettings,
)


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    value: FuturesPositioningContext


class ShfePositioningProvider:
    """Derives non-directional AU/AG totals from real contracts in SHFE delayed files."""

    name = "shfe_positioning"

    def __init__(
        self,
        settings: ShfePositioningSettings | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings or ShfePositioningSettings()
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        self._monotonic = monotonic_clock
        self._utc_clock = utc_clock
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def get_context(self, product_code: str) -> FuturesPositioningContext:
        normalized = product_code.strip().lower()
        if normalized not in {"au", "ag"}:
            raise ValueError(f"unsupported SHFE positioning product {product_code!r}")
        async with self._lock:
            now = self._monotonic()
            cached = self._cache.get(normalized)
            if cached is not None and cached.expires_at > now:
                return cached.value
            url = self.settings.data_url(normalized)
            payload = await self._get_json(url)
            contracts = parse_positioning_payload(
                payload,
                expected_product_code=normalized,
            )
            available_changes = tuple(
                item.open_interest_change
                for item in contracts
                if item.open_interest_change is not None
            )
            observed_at = max(item.observed_at for item in contracts).astimezone(UTC)
            received_at = self._utc_clock().astimezone(UTC)
            value = FuturesPositioningContext(
                product_code=normalized.upper(),
                contracts=contracts,
                contract_count=len(contracts),
                volume=sum(item.volume for item in contracts),
                open_interest=sum(item.open_interest for item in contracts),
                open_interest_change=(
                    sum(available_changes) if len(available_changes) == len(contracts) else None
                ),
                open_interest_change_contracts=len(available_changes),
                source=MarketContextSource(
                    provider_id=self.name,
                    dataset_id=f"delaymarket_{normalized}.dat",
                    source_url=url,
                    observed_at=observed_at,
                    received_at=received_at,
                    published_at=None,
                    delayed=True,
                    declared_delay=timedelta(minutes=self.settings.declared_delay_minutes),
                ),
                counting_method=PositionCountingMethod.SINGLE_SIDE,
                directional_inference=DirectionalInference.UNAVAILABLE,
            )
            self._cache[normalized] = _CacheEntry(
                expires_at=now + self.settings.cache_ttl_seconds,
                value=value,
            )
            return value

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url, timeout=self.settings.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise ProviderUnavailableError("SHFE delayed positioning request failed") from error
        except ValueError as error:
            raise ProviderDataError("SHFE delayed positioning response is invalid JSON") from error
        if not isinstance(payload, dict):
            raise ProviderDataError("SHFE delayed positioning response has an invalid root")
        return payload
