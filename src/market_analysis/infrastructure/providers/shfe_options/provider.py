from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx

from market_analysis.domain.errors import (
    ProviderDataError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from market_analysis.domain.options import OptionChainSnapshot, OptionDeliveryMode
from market_analysis.infrastructure.providers.shfe_options.protocol import (
    parse_shfe_gold_option_chain,
)
from market_analysis.infrastructure.providers.shfe_options.settings import (
    ShfeGoldOptionsSettings,
)

_TRADING_DAY_PATH = "/data/config/currentTradingday.dat"
_OPTION_DELAY_PATH = "/data/tradedata/option/delaymarket/delaymarket_auQ.dat"
_FUTURE_DELAY_PATH = "/data/tradedata/future/delaymarket/delaymarket_au.dat"
_CONTRACT_PATH = "/data/busiparamdata/option/ContractBaseInfo{date}.dat"
_DAILY_PATH = "/data/tradedata/option/dailydata/kx{date}.dat"

_logger = logging.getLogger(__name__)


def create_shfe_tls_context() -> ssl.SSLContext:
    """Use the OS trust store without disabling certificate or hostname checks.

    Python 3.13 enables X509 strict mode by default. Some enterprise HTTPS
    inspection roots trusted by Windows omit legacy authority-key metadata, so
    strict mode rejects them before the normal trust decision. Clearing only
    that flag restores the pre-3.13 validation policy; CERT_REQUIRED and
    hostname verification remain enabled.
    """

    context = ssl.create_default_context()
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    return context


class ShfeGoldOptionsProvider:
    """Official SHFE delayed gold-option chain and prior-day reference Greeks."""

    name = "shfe_official_delayed"
    market_id = "shfe_gold_options"
    market_label = "上海期货交易所黄金期权"
    delivery_mode = OptionDeliveryMode.EXCHANGE_DELAYED

    def __init__(
        self,
        settings: ShfeGoldOptionsSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_http_client = http_client is None
        self._http = http_client or self._create_http_client()
        self._http_reset_lock = asyncio.Lock()
        self._cache: tuple[float, OptionChainSnapshot] | None = None
        self._lock = asyncio.Lock()

    def _create_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.base_url,
            timeout=self.settings.request_timeout_seconds,
            verify=create_shfe_tls_context(),
            follow_redirects=True,
            headers={
                "Accept": "application/json,text/plain,*/*;q=0.8",
                "Referer": (
                    "https://www.shfe.com.cn/eng/reports/MarketData/DelayedQuotes/"
                ),
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.0.0 Safari/537.36"
                ),
            },
        )

    async def __aenter__(self) -> ShfeGoldOptionsProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        self._cache = None
        if self._owns_http_client:
            async with self._http_reset_lock:
                await self._http.aclose()

    async def get_chain(self) -> OptionChainSnapshot:
        cached = self._cached()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cached()
            if cached is not None:
                return cached
            trading_day_payload = await self._get_json(_TRADING_DAY_PATH)
            current_day = self._string_field(trading_day_payload, "currentTradingday")
            last_day = self._string_field(trading_day_payload, "lastTradingday")
            contract_path = _CONTRACT_PATH.format(date=current_day)
            daily_path = _DAILY_PATH.format(date=last_day)
            option_payload, future_payload, contract_payload, daily_payload = await asyncio.gather(
                self._get_json(_OPTION_DELAY_PATH),
                self._get_json(_FUTURE_DELAY_PATH),
                self._get_json(contract_path),
                self._get_json(daily_path),
            )
            source_urls = tuple(
                f"{self.settings.base_url}{path}"
                for path in (
                    _OPTION_DELAY_PATH,
                    _FUTURE_DELAY_PATH,
                    contract_path,
                    daily_path,
                )
            )
            try:
                snapshot = parse_shfe_gold_option_chain(
                    trading_day_payload=trading_day_payload,
                    delayed_option_payload=option_payload,
                    delayed_future_payload=future_payload,
                    contract_payload=contract_payload,
                    daily_payload=daily_payload,
                    retrieved_at=datetime.now(UTC),
                    source_urls=source_urls,
                )
            except ValueError as error:
                raise ProviderDataError("上期所官方期权数据未通过领域校验") from error
            self._cache = (monotonic(), snapshot)
            return snapshot

    def _cached(self) -> OptionChainSnapshot | None:
        if self._cache is None:
            return None
        cached_at, value = self._cache
        if monotonic() - cached_at >= self.settings.snapshot_cache_seconds:
            self._cache = None
            return None
        return value

    async def _get_json(self, path: str) -> dict[str, Any]:
        client = self._http
        try:
            response = await client.get(path)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            if not await self._recover_http_client(client, path, error):
                raise ProviderUnavailableError("上期所官方期权数据连接不可用") from error
            response = await self._retry_get(path)
        except RuntimeError as error:
            if not client.is_closed or not await self._recover_http_client(client, path, error):
                raise
            response = await self._retry_get(path)
        if response.status_code == 429:
            raise ProviderRateLimitError("上期所官方期权数据请求过于频繁")
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"上期所官方期权数据服务暂不可用 (HTTP {response.status_code})"
            )
        if response.status_code != 200:
            raise ProviderUnavailableError(
                f"上期所官方期权数据返回 HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderDataError("上期所官方期权数据不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise ProviderDataError("上期所官方期权数据根节点必须是对象")
        return payload

    async def _retry_get(self, path: str) -> httpx.Response:
        client = self._http
        try:
            return await client.get(path)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            _logger.warning(
                "SHFE options request still failed after rebuilding the HTTP client: %s",
                path,
                exc_info=(type(error), error, error.__traceback__),
            )
            raise ProviderUnavailableError("上期所官方期权数据连接不可用") from error
        except RuntimeError as error:
            if not client.is_closed:
                raise
            _logger.warning(
                "SHFE options retry used a client that was concurrently closed: %s",
                path,
                exc_info=(type(error), error, error.__traceback__),
            )
            raise ProviderUnavailableError("上期所官方期权数据连接不可用") from error

    async def _recover_http_client(
        self,
        failed_client: httpx.AsyncClient,
        path: str,
        error: Exception,
    ) -> bool:
        if not self._owns_http_client:
            return False
        async with self._http_reset_lock:
            if self._http is not failed_client:
                return True
            _logger.warning(
                "SHFE options transport failed; rebuilding the HTTP client and retrying: %s",
                path,
                exc_info=(type(error), error, error.__traceback__),
            )
            self._http = self._create_http_client()
            try:
                await failed_client.aclose()
            except Exception:
                _logger.warning(
                    "Failed to close the stale SHFE options HTTP client",
                    exc_info=True,
                )
        return True

    @staticmethod
    def _string_field(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ProviderDataError(f"上期所交易日字段 {field!r} 缺失")
        return value.strip()
