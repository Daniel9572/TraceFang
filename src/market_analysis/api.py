from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from market_analysis.application.acquisition import QuoteAcquisitionRouter
from market_analysis.application.quotes import (
    JIN10_CLIENT_SOURCE,
    JIN10_LOCAL_CHANNEL,
    JIN10_WEB_CHANNEL,
    TONGHUASHUN_FUTURES_SOURCE,
    LatestQuoteCache,
    QuoteViewService,
)
from market_analysis.application.realtime import QuoteStreamCoordinator
from market_analysis.application.realtime_klines import (
    RealtimeKlineBinding,
    RealtimeKlineService,
)
from market_analysis.application.sources import (
    MarketSourceManager,
    ProviderProbe,
    QuoteServiceTier,
    RealtimeSourceComposition,
    SourceAccessModel,
    SourceCapability,
    SourceHealth,
    SourceRegistration,
    SourceRoutingRole,
)
from market_analysis.cache import AsyncTtlCache
from market_analysis.domain.errors import (
    InstrumentNotSupportedError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from market_analysis.domain.models import Instrument
from market_analysis.environment import load_project_environment
from market_analysis.infrastructure.postgres import (
    BufferedMarketDataWriter,
    PostgresMarketDataStore,
    PostgresSettings,
)
from market_analysis.infrastructure.providers.jin10_local import (
    Jin10LocalProvider,
    Jin10LocalSettings,
)
from market_analysis.infrastructure.providers.jin10_web import (
    Jin10WebProvider,
    Jin10WebSettings,
)
from market_analysis.infrastructure.providers.tonghuashun_futures import (
    TonghuashunFuturesProvider,
    TonghuashunFuturesSettings,
)
from market_analysis.infrastructure.source_config import JsonSourceConfigurationStore
from market_analysis.instruments import (
    DEFAULT_WATCHLIST_CODES,
    INSTRUMENT_CATALOG,
    SPOT_GOLD,
    SPOT_GOLD_CNH_PER_GRAM,
    USD_CNH,
    InstrumentDefinition,
    definition_for_instrument,
    definition_for_symbol,
    direct_requirements,
    instrument_definition,
)

_repo_root = Path(__file__).resolve().parents[2]
load_project_environment(_repo_root)


class Runtime:
    def __init__(self) -> None:
        self.local_provider: Jin10LocalProvider | None = None
        self.web_provider: Jin10WebProvider | None = None
        self.tonghuashun_futures_provider: TonghuashunFuturesProvider | None = None
        self.manager: MarketSourceManager | None = None
        self.persistence: BufferedMarketDataWriter | None = None
        self.database_store: PostgresMarketDataStore | None = None
        self.persistence_setup_error: str | None = None
        self.quote_stream: QuoteStreamCoordinator | None = None
        self.quote_views: QuoteViewService | None = None
        self.acquisition: QuoteAcquisitionRouter | None = None
        self.realtime_klines: RealtimeKlineService | None = None
        self.instrument_sources: dict[str, str] = {}
        self.watchlist_codes: list[str] = list(DEFAULT_WATCHLIST_CODES)
        self.catalog_cache: AsyncTtlCache[Any] = AsyncTtlCache()

    def clear_caches(self) -> None:
        self.catalog_cache.clear()


runtime = Runtime()


_SPOT_METALS_MARKET_SCHEDULE: dict[str, Any] = {
    "time_zone": "America/New_York",
    "reference": "OTC 贵金属常规交易时段",
    "sessions": [
        {
            "weekday": weekday,
            "open": "18:05",
            "close": "16:59",
            "close_day_offset": 1,
        }
        for weekday in range(5)
    ],
}

_FOREX_MARKET_SCHEDULE: dict[str, Any] = {
    "time_zone": "America/New_York",
    "reference": "OTC 外汇常规交易时段",
    "sessions": [
        {
            "weekday": weekday,
            "open": "17:05",
            "close": "16:59",
            "close_day_offset": 1,
        }
        for weekday in range(5)
    ],
}

_SHFE_METALS_MARKET_SCHEDULE: dict[str, Any] = {
    "time_zone": "Asia/Shanghai",
    "reference": "上海期货交易所贵金属期货常规交易时段",
    "sessions": [
        session
        for weekday in range(1, 6)
        for session in (
            {
                "weekday": weekday,
                "open": "09:00",
                "close": "10:15",
                "close_day_offset": 0,
            },
            {
                "weekday": weekday,
                "open": "10:30",
                "close": "11:30",
                "close_day_offset": 0,
            },
            {
                "weekday": weekday,
                "open": "13:30",
                "close": "15:00",
                "close_day_offset": 0,
            },
            {
                "weekday": weekday,
                "open": "21:00",
                "close": "02:30",
                "close_day_offset": 1,
            },
        )
    ],
}

_SSE_MARKET_SCHEDULE: dict[str, Any] = {
    "time_zone": "Asia/Shanghai",
    "reference": "上海证券交易所指数常规行情时段",
    "sessions": [
        session
        for weekday in range(1, 6)
        for session in (
            {
                "weekday": weekday,
                "open": "09:30",
                "close": "11:30",
                "close_day_offset": 0,
            },
            {
                "weekday": weekday,
                "open": "13:00",
                "close": "15:00",
                "close_day_offset": 0,
            },
        )
    ],
}

_NASDAQ_MARKET_SCHEDULE: dict[str, Any] = {
    "time_zone": "America/New_York",
    "reference": "纳斯达克常规交易时段",
    "sessions": [
        {
            "weekday": weekday,
            "open": "09:30",
            "close": "16:00",
            "close_day_offset": 0,
        }
        for weekday in range(1, 6)
    ],
}

_USD_INDEX_MARKET_SCHEDULE: dict[str, Any] = {
    "time_zone": "UTC",
    "reference": "同花顺美元指数公开行情常规时段",
    "sessions": [
        {
            "weekday": weekday,
            "open": "00:00",
            "close": "00:00",
            "close_day_offset": 1,
        }
        for weekday in range(1, 6)
    ],
}

_ICE_BRENT_MARKET_SCHEDULE: dict[str, Any] = {
    "time_zone": "Europe/London",
    "reference": "ICE Futures Europe 布伦特原油常规电子交易时段",
    "sessions": [
        {
            "weekday": 0,
            "open": "23:00",
            "close": "23:00",
            "close_day_offset": 1,
        },
        *[
            {
                "weekday": weekday,
                "open": "01:00",
                "close": "23:00",
                "close_day_offset": 0,
            }
            for weekday in range(2, 6)
        ],
    ],
}

_MARKET_SCHEDULES: dict[str, dict[str, Any]] = {
    "spot_metals": _SPOT_METALS_MARKET_SCHEDULE,
    "forex": _FOREX_MARKET_SCHEDULE,
    "shfe_metals": _SHFE_METALS_MARKET_SCHEDULE,
    "sse": _SSE_MARKET_SCHEDULE,
    "nasdaq": _NASDAQ_MARKET_SCHEDULE,
    "usd_index": _USD_INDEX_MARKET_SCHEDULE,
    "ice_brent": _ICE_BRENT_MARKET_SCHEDULE,
}


class InstrumentSourceUpdate(BaseModel):
    source_id: str = Field(min_length=1)


def _source_store_path() -> Path:
    configured = os.environ.get("MARKET_ANALYSIS_SOURCE_CONFIG", "").strip()
    return Path(configured).expanduser() if configured else _repo_root / "data" / "sources.json"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    local_provider: Jin10LocalProvider | None = None
    local_settings: Jin10LocalSettings | None = None
    local_setup_error: str | None = None
    try:
        local_settings = Jin10LocalSettings.from_env()
        local_provider = Jin10LocalProvider(local_settings)
    except (ValueError, ProviderError) as error:
        local_setup_error = str(error)
        if local_provider is not None:
            await local_provider.close()
        local_provider = None

    web_provider: Jin10WebProvider | None = None
    web_settings: Jin10WebSettings | None = None
    web_setup_error: str | None = None
    try:
        web_settings = Jin10WebSettings.from_env()
        web_provider = Jin10WebProvider(web_settings)
    except (ValueError, ProviderError) as error:
        web_setup_error = str(error)
        if web_provider is not None:
            await web_provider.close()
        web_provider = None

    tonghuashun_futures_settings = TonghuashunFuturesSettings.from_env()
    tonghuashun_futures_provider = TonghuashunFuturesProvider(tonghuashun_futures_settings)

    async def probe_local_provider() -> ProviderProbe:
        if local_provider is None:
            return ProviderProbe(
                available=False,
                state="setup_required",
                detail=local_setup_error,
                checked_at=datetime.now(UTC),
            )
        available, state, detail = local_provider.health()
        return ProviderProbe(
            available=available,
            state=state,
            detail=detail,
            checked_at=datetime.now(UTC),
        )

    async def probe_web_provider() -> ProviderProbe:
        if web_provider is None:
            return ProviderProbe(
                available=False,
                state="setup_required",
                detail=web_setup_error,
                checked_at=datetime.now(UTC),
            )
        available, state, detail = web_provider.health()
        return ProviderProbe(
            available=available,
            state=state,
            detail=detail,
            checked_at=datetime.now(UTC),
        )

    async def probe_client_source() -> ProviderProbe:
        web_probe = await probe_web_provider()
        local_probe = await probe_local_provider()
        if not web_probe.available:
            if web_probe.state == "waiting_quote":
                return ProviderProbe(
                    available=True,
                    state="connected_waiting_quote",
                    detail=("高速行情连接已建立, 当前没有新的报价帧; 休市或行情静止时属于正常状态"),
                    checked_at=datetime.now(UTC),
                    health=SourceHealth.DEGRADED,
                )
            return ProviderProbe(
                available=False,
                state="unavailable",
                detail="金十客户端行情暂时没有可用的实时价格",
                checked_at=datetime.now(UTC),
                health=SourceHealth.UNAVAILABLE,
            )
        if not local_probe.available:
            return ProviderProbe(
                available=True,
                state="degraded",
                detail="实时价格可用, 部分日内统计暂时缺失或停滞",
                checked_at=datetime.now(UTC),
                health=SourceHealth.DEGRADED,
            )
        return ProviderProbe(
            available=True,
            state="ready",
            detail="完整聚合行情可用",
            checked_at=datetime.now(UTC),
            health=SourceHealth.HEALTHY,
        )

    client_composition = RealtimeSourceComposition(
        quote_channel_ids=(JIN10_WEB_CHANNEL, JIN10_LOCAL_CHANNEL),
        kline_channel_id=JIN10_LOCAL_CHANNEL,
        kline_derived_from_quotes=False,
    )
    registrations = (
        SourceRegistration(
            source_id=JIN10_CLIENT_SOURCE,
            display_name="金十客户端行情",
            description=(
                "完整实时数据源: 统一提供报价事件、当前/过去 K 线、涨跌和日内统计。"
                "页面与合约路由只面对这一份结果。"
            ),
            capabilities=frozenset({SourceCapability.QUOTE, SourceCapability.CANDLES}),
            default_enabled=True,
            default_priority=5,
            delayed=False,
            requires_running_app=False,
            structured=True,
            quote_poll_interval_seconds=0,
            quote_streaming=True,
            quote_service_tier=QuoteServiceTier.ENHANCED,
            routing_role=SourceRoutingRole.REALTIME_SOURCE,
            composition=client_composition,
            access_model=SourceAccessModel.UNMETERED,
            access_note="事件驱动的结构化行情, 不使用限额接口。",
            probe=probe_client_source,
        ),
        SourceRegistration(
            source_id=TONGHUASHUN_FUTURES_SOURCE,
            display_name="同花顺公开行情",
            description=(
                "覆盖沪金/沪银、美元指数、布伦特原油和中美股票指数的结构化实时源, "
                "直接提供报价、日内统计和同源公开一分钟 K 线。"
            ),
            capabilities=frozenset({SourceCapability.QUOTE, SourceCapability.CANDLES}),
            default_enabled=True,
            default_priority=20,
            delayed=False,
            requires_running_app=False,
            structured=True,
            quote_poll_interval_seconds=(tonghuashun_futures_settings.quote_poll_interval_seconds),
            quote_streaming=False,
            quote_service_tier=QuoteServiceTier.STANDARD,
            routing_role=SourceRoutingRole.REALTIME_SOURCE,
            access_model=SourceAccessModel.UNMETERED,
            access_note=(
                "同花顺公开网页结构化接口; 应用按观察品种节流轮询, 不依赖本地期货软件常驻。"
            ),
            quote_provider=tonghuashun_futures_provider,
            candle_provider=tonghuashun_futures_provider,
        ),
        SourceRegistration(
            source_id="jin10_local",
            display_name="金十桌面会话原始通道",
            description=(
                "内部同源通道: 使用本机金十客户端登录会话, 提供日内补充字段和"
                "可分页的过去 K 线; 建立会话后无需保持软件窗口运行。"
            ),
            capabilities=frozenset(
                {
                    SourceCapability.QUOTE,
                    SourceCapability.CANDLES,
                }
            ),
            default_enabled=True,
            default_priority=10,
            delayed=False,
            requires_running_app=False,
            structured=True,
            quote_poll_interval_seconds=0,
            quote_streaming=True,
            quote_service_tier=QuoteServiceTier.STANDARD,
            routing_role=SourceRoutingRole.INTERNAL_CHANNEL,
            quote_provider=local_provider,
            candle_provider=local_provider,
            probe=probe_local_provider if local_provider is not None else None,
            setup_error=local_setup_error,
        ),
        SourceRegistration(
            source_id="jin10_web",
            display_name="金十官网高速原始通道",
            description=(
                "独立原始通道: 金十官网公开、无需登录口令的结构化价格推送。"
                "在组合产品中只拥有实时价格与涨跌字段; 不消耗 MCP 额度。"
            ),
            capabilities=frozenset({SourceCapability.QUOTE}),
            default_enabled=True,
            default_priority=15,
            delayed=False,
            requires_running_app=False,
            structured=True,
            quote_poll_interval_seconds=0,
            quote_streaming=True,
            quote_service_tier=QuoteServiceTier.ENHANCED,
            routing_role=SourceRoutingRole.INTERNAL_CHANNEL,
            access_model=SourceAccessModel.UNMETERED,
            access_note="官网公开通道。无登录鉴权和调用次数额度。接口升级时需要重新验证协议。",
            quote_provider=web_provider,
            probe=probe_web_provider if web_provider is not None else None,
            setup_error=web_setup_error,
        ),
    )
    runtime.local_provider = local_provider
    runtime.web_provider = web_provider
    runtime.tonghuashun_futures_provider = tonghuashun_futures_provider
    runtime.manager = MarketSourceManager(
        registrations,
        store=JsonSourceConfigurationStore(_source_store_path()),
    )
    runtime.persistence = None
    runtime.database_store = None
    runtime.realtime_klines = None
    runtime.watchlist_codes = list(DEFAULT_WATCHLIST_CODES)
    runtime.persistence_setup_error = None
    try:
        database_settings = PostgresSettings.from_env()
    except ValueError as error:
        database_settings = None
        runtime.persistence_setup_error = str(error)
    if database_settings is None:
        runtime.persistence_setup_error = (
            runtime.persistence_setup_error or "MARKET_ANALYSIS_DATABASE_URL is not configured"
        )
    else:
        runtime.database_store = PostgresMarketDataStore(database_settings)
        runtime.persistence = BufferedMarketDataWriter(runtime.database_store)
        with suppress(Exception):
            await runtime.database_store.open()
        await runtime.persistence.start()
        if runtime.database_store.is_open:
            default_instruments = tuple(
                instrument_definition(code).instrument for code in DEFAULT_WATCHLIST_CODES
            )
            await runtime.database_store.initialize_watchlist(default_instruments)
            stored_symbols = await runtime.database_store.load_watchlist_symbols()
            stored_codes: list[str] = []
            for symbol in stored_symbols:
                with suppress(InstrumentNotSupportedError):
                    stored_codes.append(definition_for_symbol(symbol).code)
            runtime.watchlist_codes = stored_codes

    kline_store = (
        runtime.database_store
        if runtime.database_store and runtime.database_store.is_open
        else None
    )
    runtime.realtime_klines = RealtimeKlineService(
        kline_store,
        bindings=(
            RealtimeKlineBinding(
                realtime_source_id=JIN10_CLIENT_SOURCE,
                history_channel_id=client_composition.kline_channel_id,
                live_quote_channel_id=JIN10_WEB_CHANNEL,
                history_provider=local_provider,
            ),
            RealtimeKlineBinding(
                realtime_source_id=TONGHUASHUN_FUTURES_SOURCE,
                history_channel_id=TONGHUASHUN_FUTURES_SOURCE,
                live_quote_channel_id=TONGHUASHUN_FUTURES_SOURCE,
                history_provider=tonghuashun_futures_provider,
            ),
        ),
    )

    async def load_latest_quote(instrument, source_id):
        store = runtime.database_store
        if store is None or not store.is_open:
            return None
        return await store.load_latest_quote(instrument, source_id)

    stale_after_seconds = {
        JIN10_WEB_CHANNEL: web_settings.stale_after_seconds if web_settings else 12.0,
        JIN10_LOCAL_CHANNEL: local_settings.stale_after_seconds if local_settings else 12.0,
        TONGHUASHUN_FUTURES_SOURCE: (tonghuashun_futures_settings.stale_after_seconds),
    }
    quote_cache = LatestQuoteCache(load_latest_quote)
    runtime.quote_views = QuoteViewService(
        quote_cache,
        stale_after=lambda source_id: stale_after_seconds.get(source_id, 60.0),
    )
    runtime.quote_stream = QuoteStreamCoordinator(load_quote=runtime.quote_views.get)

    def accept_raw_quote(value):
        quote_views = runtime.quote_views
        if runtime.realtime_klines is not None:
            runtime.realtime_klines.accept_quote(value)
        # Persist every raw channel frame, including late or duplicate deliveries.
        # Only the latest presentation view applies the monotonic timestamp guard;
        # raw evidence must never be filtered by UI-cache semantics.
        if runtime.persistence is not None:
            runtime.persistence.submit_quote(value)
        if quote_views is None or not quote_views.accept(value):
            return
        stream = runtime.quote_stream
        if stream is None:
            return
        with suppress(ProviderError):
            stream.publish(quote_views.build_cached(value.instrument, value.source.provider))
        acquisition = runtime.acquisition
        if (
            acquisition is not None
            and acquisition.route_for(value.instrument) == JIN10_CLIENT_SOURCE
        ):
            with suppress(ProviderError):
                stream.publish(quote_views.build_cached(value.instrument, JIN10_CLIENT_SOURCE))
        if (
            "XAUCNHG" in runtime.watchlist_codes
            and value.source.provider == JIN10_WEB_CHANNEL
            and value.instrument in {SPOT_GOLD, USD_CNH}
        ):
            with suppress(ProviderError):
                derived = quote_views.build_cached(
                    SPOT_GOLD_CNH_PER_GRAM,
                    JIN10_CLIENT_SOURCE,
                )
                if runtime.realtime_klines is not None:
                    runtime.realtime_klines.accept_view(derived)
                stream.publish(derived)

    def report_acquisition_error(instrument, source_id, error):
        if runtime.quote_stream is not None:
            runtime.quote_stream.publish_unavailable(instrument, source_id, error)

    async def prepare_source(source_id):
        del source_id

    runtime.acquisition = QuoteAcquisitionRouter(
        push_channels={
            provider.name: provider
            for provider in (web_provider, local_provider)
            if provider is not None
        },
        poll_channels={
            tonghuashun_futures_provider.name: tonghuashun_futures_provider,
        },
        source_channels={
            JIN10_CLIENT_SOURCE: client_composition.quote_channel_ids,
            TONGHUASHUN_FUTURES_SOURCE: (TONGHUASHUN_FUTURES_SOURCE,),
        },
        source_enabled=_manager().is_enabled,
        prepare_source=prepare_source,
        poll_interval=_manager().quote_poll_interval,
        on_quote=accept_raw_quote,
        on_error=report_acquisition_error,
    )
    local_quote_listener = None
    if local_provider is not None:
        local_quote_listener = accept_raw_quote
        local_provider.add_quote_listener(local_quote_listener)
    web_quote_listener = None
    if web_provider is not None:
        web_quote_listener = accept_raw_quote
        web_provider.add_quote_listener(web_quote_listener)

    initial_routes: dict[Instrument, str] = {}
    runtime.instrument_sources.clear()
    for code in runtime.watchlist_codes:
        definition = instrument_definition(code)
        instrument = definition.instrument
        source_id = definition.source_ids[0]
        if runtime.database_store is not None and runtime.database_store.is_open:
            stored_source = await runtime.database_store.get_instrument_source(instrument)
            if stored_source is not None and (
                not _manager().is_realtime_source(stored_source)
                or stored_source not in definition.source_ids
            ):
                await runtime.database_store.set_instrument_source(instrument, source_id)
            elif stored_source is not None:
                source_id = stored_source
            else:
                await runtime.database_store.set_instrument_source(instrument, source_id)
        runtime.instrument_sources[instrument.symbol] = source_id
        for requirement in direct_requirements(definition):
            initial_routes[requirement] = source_id
            runtime.instrument_sources[requirement.symbol] = source_id
            if runtime.database_store is not None and runtime.database_store.is_open:
                await runtime.database_store.set_instrument_source(requirement, source_id)
    await runtime.acquisition.start(initial_routes)
    runtime.clear_caches()
    yield
    if runtime.acquisition is not None:
        await runtime.acquisition.stop()
    if local_provider is not None and local_quote_listener is not None:
        local_provider.remove_quote_listener(local_quote_listener)
    if web_provider is not None and web_quote_listener is not None:
        web_provider.remove_quote_listener(web_quote_listener)
    if runtime.quote_stream is not None:
        await runtime.quote_stream.close()
    if runtime.realtime_klines is not None:
        await runtime.realtime_klines.close()
    if runtime.persistence is not None:
        await runtime.persistence.stop()
    if runtime.local_provider is not None:
        await runtime.local_provider.close()
    if runtime.web_provider is not None:
        await runtime.web_provider.close()
    if runtime.tonghuashun_futures_provider is not None:
        await runtime.tonghuashun_futures_provider.close()
    runtime.local_provider = None
    runtime.web_provider = None
    runtime.tonghuashun_futures_provider = None
    runtime.manager = None
    runtime.persistence = None
    runtime.database_store = None
    runtime.persistence_setup_error = None
    runtime.quote_stream = None
    runtime.quote_views = None
    runtime.acquisition = None
    runtime.realtime_klines = None
    runtime.instrument_sources.clear()
    runtime.watchlist_codes = list(DEFAULT_WATCHLIST_CODES)
    runtime.clear_caches()


app = FastAPI(title="Market Analysis Platform", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["DELETE", "GET", "PATCH", "POST", "PUT"],
    allow_headers=["*"],
)


def _manager() -> MarketSourceManager:
    if runtime.manager is None:
        raise HTTPException(status_code=503, detail="market data runtime is not ready")
    return runtime.manager


def _quote_stream() -> QuoteStreamCoordinator:
    if runtime.quote_stream is None:
        raise HTTPException(status_code=503, detail="quote stream runtime is not ready")
    return runtime.quote_stream


def _quote_views() -> QuoteViewService:
    if runtime.quote_views is None:
        raise HTTPException(status_code=503, detail="local quote cache is not ready")
    return runtime.quote_views


def _acquisition() -> QuoteAcquisitionRouter:
    if runtime.acquisition is None:
        raise HTTPException(status_code=503, detail="quote acquisition runtime is not ready")
    return runtime.acquisition


def _realtime_klines() -> RealtimeKlineService:
    if runtime.realtime_klines is None:
        raise HTTPException(status_code=503, detail="Realtime Kline runtime is unavailable")
    return runtime.realtime_klines


def _public_source(value: Any) -> dict[str, Any]:
    """Expose realtime-source outcomes, never their internal channel topology."""

    return {
        "source_id": value.source_id,
        "display_name": value.display_name,
        "description": value.description,
        "capabilities": value.capabilities,
        "selectable": value.enabled and not value.frozen,
        "delayed": value.delayed,
        "requires_running_app": value.requires_running_app,
        "structured": value.structured,
        "quote_poll_interval_seconds": value.quote_poll_interval_seconds,
        "quote_streaming": value.quote_streaming,
        "quote_service_tier": value.quote_service_tier,
        "access_model": value.access_model,
        "access_note": value.access_note,
        "manual_connection_required": value.manual_connection_required,
        "connection_active": value.connection_active,
        "quotas": value.quotas,
        "health": value.health,
        "state": value.state,
        "error": value.error,
        "checked_at": value.checked_at,
        "last_success_at": value.last_success_at,
    }


def _public_acquisition_status(value: dict[str, object] | None) -> dict[str, object] | None:
    """Expose realtime routes without leaking their internal channel topology."""

    if value is None:
        return None
    return {
        "state": "running",
        "routes": value.get("routes", {}),
    }


@app.exception_handler(InstrumentNotSupportedError)
async def instrument_error(_: Request, error: InstrumentNotSupportedError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=404, content={"detail": str(error)})


@app.exception_handler(ProviderRateLimitError)
async def rate_limit_error(_: Request, error: ProviderRateLimitError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=429, content={"detail": str(error)})


@app.exception_handler(ProviderError)
async def provider_error(_: Request, error: ProviderError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=502, content={"detail": str(error)})


@app.get("/api/health")
async def health() -> dict[str, Any]:
    manager = _manager()
    sources = await manager.list_sources(refresh=True)
    healthy = any(item.enabled and item.health.value == "healthy" for item in sources)
    if runtime.persistence is None:
        database = {
            "state": "unconfigured",
            "detail": runtime.persistence_setup_error,
            "queue_depth": 0,
            "last_write_at": None,
        }
    else:
        database = asdict(runtime.persistence.health())
    database_healthy = database["state"] == "healthy"
    return {
        "status": "ok" if healthy and database_healthy else "degraded",
        "sources": [_public_source(item) for item in sources],
        "database": database,
        "acquisition": _public_acquisition_status(
            runtime.acquisition.status() if runtime.acquisition is not None else None
        ),
        "history": {
            "mode": "realtime_source_bound_cache",
            "governance": "frozen",
            "cross_source_fallback": False,
            "upstream_calls_on_read": False,
            "live_kline_count": (
                runtime.realtime_klines.live_count() if runtime.realtime_klines is not None else 0
            ),
            "backfill_pending": (
                runtime.realtime_klines.pending_backfill_count()
                if runtime.realtime_klines is not None
                else 0
            ),
        },
    }


@app.get("/api/sources")
async def sources(refresh: bool = Query(default=True)) -> list[dict[str, Any]]:
    values = await _manager().list_sources(refresh=refresh)
    return [_public_source(value) for value in values]


@app.post("/api/sources/{source_id}/test")
async def test_source(
    source_id: str,
    code: str = Query(default="XAUUSD"),
) -> dict[str, Any]:
    started = perf_counter()
    try:
        definition = instrument_definition(code)
        instrument = definition.instrument
        manager = _manager()
        manager.validate_realtime_source(
            source_id,
            require_connection=False,
        )
        if source_id not in definition.source_ids:
            raise ProviderUnavailableError(f"{definition.code} 不支持实时数据源 {source_id}")
        await manager.connect_source(source_id)
        if source_id == TONGHUASHUN_FUTURES_SOURCE:
            await _acquisition().sample_source(source_id, instrument)
        else:
            await _acquisition().reconcile()
        descriptors = await manager.list_sources(refresh=True)
        descriptor = next(item for item in descriptors if item.source_id == source_id)
        if descriptor.health in {
            SourceHealth.UNAVAILABLE,
            SourceHealth.UNCONFIGURED,
            SourceHealth.FROZEN,
        }:
            raise ProviderUnavailableError(descriptor.error or f"{source_id} 连接不可用")
        try:
            value = await _quote_views().get(instrument, source_id)
        except ProviderUnavailableError:
            if descriptor.state != "connected_waiting_quote":
                raise
            value = None
        kline_rows = await _realtime_klines().get_candles(
            instrument,
            source_id=source_id,
            count=1,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProviderUnavailableError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "source_id": source_id,
        "code": definition.code,
        "state": descriptor.state,
        "detail": descriptor.error,
        "data_fresh": value is not None,
        "last": value.quote.last if value is not None else None,
        "observed_at": value.quote.source.observed_at if value is not None else None,
        "latency_ms": max(1, round((perf_counter() - started) * 1000)),
        "quality": value.quality if value is not None else "unavailable",
        "unavailable_fields": value.unavailable_fields if value is not None else (),
        "stale_fields": value.stale_fields if value is not None else (),
        "kline_points": len(kline_rows),
        "kline_open_time": kline_rows[-1].open_time if kline_rows else None,
    }


@app.get("/api/instruments")
async def instruments() -> list[dict[str, Any]]:
    return [_public_instrument(item) for item in INSTRUMENT_CATALOG]


def _public_instrument(definition: InstrumentDefinition) -> dict[str, Any]:
    schedule = _MARKET_SCHEDULES[definition.market_schedule_id]
    return {
        "provider": "canonical",
        "provider_code": definition.code,
        "name": definition.name,
        "instrument": asdict(definition.instrument),
        "price_unit": definition.price_unit,
        "price_digits": definition.price_digits,
        "quote_kind": definition.quote_kind,
        "history_available": definition.history_available,
        "source_ids": list(definition.source_ids),
        "dependencies": [
            definition_for_symbol(item.symbol).code for item in definition.dependencies
        ],
        "market_schedule": schedule,
    }


def _public_watchlist() -> list[dict[str, Any]]:
    return [_public_instrument(instrument_definition(code)) for code in runtime.watchlist_codes]


async def _source_for_instrument(instrument: Instrument) -> str:
    definition = definition_for_instrument(instrument)
    default_source_id = definition.source_ids[0]
    source_id = runtime.instrument_sources.get(instrument.symbol)
    store = runtime.database_store
    if source_id is None and store is not None and store.is_open:
        source_id = await store.get_instrument_source(instrument)
    if (
        source_id is None
        or not _manager().is_realtime_source(source_id)
        or source_id not in definition.source_ids
    ):
        source_id = default_source_id
        if store is not None and store.is_open:
            with suppress(Exception):
                await store.set_instrument_source(instrument, source_id)
    runtime.instrument_sources[instrument.symbol] = source_id
    return source_id


async def _refresh_watchlist_routes() -> None:
    routes: dict[Instrument, str] = {}
    store = runtime.database_store
    for code in runtime.watchlist_codes:
        definition = instrument_definition(code)
        source_id = await _source_for_instrument(definition.instrument)
        for requirement in direct_requirements(definition):
            routes[requirement] = source_id
            runtime.instrument_sources[requirement.symbol] = source_id
            if store is not None and store.is_open:
                await store.set_instrument_source(requirement, source_id)
    await _acquisition().replace_routes(routes)


async def _instrument_source(code: str) -> tuple[str, Instrument, str]:
    definition = instrument_definition(code)
    source_id = await _source_for_instrument(definition.instrument)
    return definition.code, definition.instrument, source_id


@app.get("/api/watchlist")
async def watchlist() -> list[dict[str, Any]]:
    return _public_watchlist()


@app.post("/api/watchlist/{code}")
async def add_watchlist_instrument(code: str) -> list[dict[str, Any]]:
    definition = instrument_definition(code)
    if definition.code in runtime.watchlist_codes:
        return _public_watchlist()
    store = runtime.database_store
    if store is not None and store.is_open:
        await store.add_watchlist_instrument(definition.instrument)
    runtime.watchlist_codes.append(definition.code)
    await _source_for_instrument(definition.instrument)
    await _refresh_watchlist_routes()
    runtime.clear_caches()
    return _public_watchlist()


@app.delete("/api/watchlist/{code}")
async def remove_watchlist_instrument(code: str) -> list[dict[str, Any]]:
    definition = instrument_definition(code)
    if definition.code not in runtime.watchlist_codes:
        return _public_watchlist()
    if len(runtime.watchlist_codes) == 1:
        raise HTTPException(status_code=409, detail="观察列表至少保留一个品种")
    store = runtime.database_store
    if store is not None and store.is_open:
        await store.remove_watchlist_instrument(definition.instrument)
    runtime.watchlist_codes.remove(definition.code)
    await _refresh_watchlist_routes()
    runtime.clear_caches()
    return _public_watchlist()


@app.get("/api/instruments/{code}/source")
async def instrument_source(code: str) -> dict[str, Any]:
    normalized_code, _, source_id = await _instrument_source(code)
    return {
        "code": normalized_code,
        "source_id": source_id,
    }


@app.put("/api/instruments/{code}/source")
async def update_instrument_source(
    code: str,
    update: InstrumentSourceUpdate,
) -> dict[str, Any]:
    definition = instrument_definition(code)
    normalized_code = definition.code
    instrument = definition.instrument
    try:
        _manager().validate_realtime_source(update.source_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProviderUnavailableError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if update.source_id not in definition.source_ids:
        raise HTTPException(
            status_code=400,
            detail=f"{normalized_code} 不支持实时数据源 {update.source_id}",
        )
    runtime.instrument_sources[instrument.symbol] = update.source_id
    store = runtime.database_store
    if store is not None and store.is_open:
        with suppress(Exception):
            await store.set_instrument_source(instrument, update.source_id)
    for requirement in direct_requirements(definition):
        runtime.instrument_sources[requirement.symbol] = update.source_id
        if store is not None and store.is_open:
            with suppress(Exception):
                await store.set_instrument_source(requirement, update.source_id)
    await _refresh_watchlist_routes()
    runtime.clear_caches()
    return {"code": normalized_code, "source_id": update.source_id}


@app.get("/api/quotes/{code}")
async def quote(code: str) -> dict[str, Any]:
    _, instrument, source_id = await _instrument_source(code)
    value = await _quote_views().get(instrument, source_id)
    return asdict(value)


@app.get("/api/quotes/{code}/last")
async def last_quote(code: str) -> dict[str, Any]:
    """Read one explicitly stale-capable snapshot from same-source local storage."""

    _, instrument, source_id = await _instrument_source(code)
    value = await _quote_views().get_last(instrument, source_id)
    return asdict(value)


@app.get("/api/candles/{code}")
async def candles(
    code: str,
    count: int = Query(default=100, ge=1, le=2_000),
    time: int | None = Query(default=None, description="Unix seconds"),
) -> list[dict[str, Any]]:
    _, instrument, source_id = await _instrument_source(code)
    start = datetime.fromtimestamp(time, tz=UTC) if time else None
    values = await _realtime_klines().get_candles(
        instrument,
        source_id=source_id,
        start=start,
        count=count,
    )
    return [asdict(value) for value in values]


@app.post("/api/candles/{code}/backfill")
async def backfill_candles(
    code: str,
    count: int = Query(default=1_000, ge=1, le=10_000),
    time: int = Query(description="Inclusive range start as Unix seconds"),
) -> dict[str, Any]:
    """Explicitly fills one missing range from the contract's bound realtime source."""

    definition = instrument_definition(code)
    if not definition.history_available:
        raise HTTPException(
            status_code=409,
            detail="该换算品种目前只提供实时分钟线, 不提供历史回补",
        )
    _, instrument, source_id = await _instrument_source(code)
    start = datetime.fromtimestamp(time, tz=UTC)
    result = await _realtime_klines().backfill(
        instrument,
        source_id=source_id,
        start=start,
        count=count,
    )
    return asdict(result)


@app.websocket("/api/stream/quotes/{code}")
async def quote_stream(websocket: WebSocket, code: str) -> None:
    try:
        _, instrument, source_id = await _instrument_source(code)
    except ProviderError:
        await websocket.close(code=1008, reason="instrument is not supported")
        return
    await websocket.accept()
    try:
        async with _quote_stream().subscribe(instrument, source=source_id) as queue:
            while True:
                event = await queue.get()
                await websocket.send_json(jsonable_encoder(asdict(event)))
    except WebSocketDisconnect:
        return


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    include_in_schema=False,
)
async def missing_api(path: str) -> None:
    raise HTTPException(status_code=404, detail=f"API route not found: /api/{path}")


_web_dist = _repo_root / "web" / "dist"
if _web_dist.exists():
    _assets = _web_dist / "assets"
    if _assets.exists():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def web_app(path: str) -> FileResponse:
        requested = (_web_dist / path).resolve()
        if requested.is_file() and _web_dist.resolve() in requested.parents:
            headers = {"Cache-Control": "no-store"} if requested.name == "index.html" else None
            return FileResponse(requested, headers=headers)
        return FileResponse(_web_dist / "index.html", headers={"Cache-Control": "no-store"})


def run() -> None:
    host = os.environ.get("MARKET_ANALYSIS_HOST", "127.0.0.1")
    port = int(os.environ.get("MARKET_ANALYSIS_PORT", "8000"))
    uvicorn.run("market_analysis.api:app", host=host, port=port, reload=False)
