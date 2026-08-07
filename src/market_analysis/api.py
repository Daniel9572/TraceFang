from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
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
from market_analysis.application.history import LocalCandleHistoryService
from market_analysis.application.quotes import (
    JIN10_CLIENT_SOURCE,
    JIN10_LOCAL_CHANNEL,
    JIN10_WEB_CHANNEL,
    LatestQuoteCache,
    QuoteViewService,
)
from market_analysis.application.realtime import QuoteStreamCoordinator
from market_analysis.application.sources import (
    MarketSourceManager,
    ProviderProbe,
    QuoteServiceTier,
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
from market_analysis.domain.models import InstrumentCatalogEntry
from market_analysis.environment import load_project_environment
from market_analysis.infrastructure.postgres import (
    BufferedMarketDataWriter,
    PostgresMarketDataStore,
    PostgresSettings,
)
from market_analysis.infrastructure.providers.jin10 import (
    SPOT_GOLD,
    SPOT_SILVER,
    Jin10SymbolMapper,
)
from market_analysis.infrastructure.providers.jin10_local import (
    Jin10LocalProvider,
    Jin10LocalSettings,
)
from market_analysis.infrastructure.providers.jin10_web import (
    Jin10WebProvider,
    Jin10WebSettings,
)
from market_analysis.infrastructure.source_config import JsonSourceConfigurationStore

_repo_root = Path(__file__).resolve().parents[2]
load_project_environment(_repo_root)


class Runtime:
    def __init__(self) -> None:
        self.local_provider: Jin10LocalProvider | None = None
        self.web_provider: Jin10WebProvider | None = None
        self.manager: MarketSourceManager | None = None
        self.persistence: BufferedMarketDataWriter | None = None
        self.database_store: PostgresMarketDataStore | None = None
        self.persistence_setup_error: str | None = None
        self.quote_stream: QuoteStreamCoordinator | None = None
        self.quote_views: QuoteViewService | None = None
        self.acquisition: QuoteAcquisitionRouter | None = None
        self.candle_history: LocalCandleHistoryService | None = None
        self.mapper = Jin10SymbolMapper()
        self.catalog_cache: AsyncTtlCache[Any] = AsyncTtlCache()

    def clear_caches(self) -> None:
        self.catalog_cache.clear()


runtime = Runtime()


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

    registrations = (
        SourceRegistration(
            source_id=JIN10_CLIENT_SOURCE,
            display_name="金十客户端行情",
            description=("统一提供实时价格、涨跌和日内统计。页面与合约路由只面对这一份聚合结果。"),
            capabilities=frozenset({SourceCapability.QUOTE}),
            default_enabled=True,
            default_priority=5,
            delayed=False,
            requires_running_app=False,
            structured=True,
            quote_poll_interval_seconds=0,
            quote_streaming=True,
            quote_service_tier=QuoteServiceTier.ENHANCED,
            routing_role=SourceRoutingRole.LOGICAL,
            access_model=SourceAccessModel.UNMETERED,
            access_note="事件驱动的结构化行情, 不使用限额接口。",
            probe=probe_client_source,
        ),
        SourceRegistration(
            source_id="jin10_local",
            display_name="金十桌面会话原始通道",
            description=(
                "独立原始通道: 使用本机金十客户端登录会话, 直连并解码鉴权结构化行情。"
                "在组合产品中只拥有日内补充字段; 建立会话后无需保持软件窗口运行。"
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
            history_priority=20,
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
            history_priority=15,
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
    runtime.manager = MarketSourceManager(
        registrations,
        store=JsonSourceConfigurationStore(_source_store_path()),
    )
    runtime.persistence = None
    runtime.database_store = None
    runtime.candle_history = None
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
        if runtime.database_store.is_open:
            await runtime.database_store.remove_source_from_standard_history("jin10_mcp")
        await runtime.persistence.start()

    if local_provider is not None and runtime.database_store is not None:
        for instrument in (SPOT_GOLD, SPOT_SILVER):
            with suppress(Exception):
                local_provider.seed_candles(
                    await runtime.database_store.load_quote_candles(
                        instrument,
                        source_id=local_provider.name,
                        count=2_000,
                    )
                )

    if runtime.database_store is not None:

        async def fetch_backfill_candles(instrument, source, start, count):
            return await _manager().get_candles(
                instrument,
                source=source,
                start=start,
                count=count,
            )

        runtime.candle_history = LocalCandleHistoryService(
            runtime.database_store,
            fetch_candles=fetch_backfill_candles,
            source_priority=_manager().history_source_priority,
            quote_derived_sources=_manager().history_quote_derived_sources,
            backfill_sources=_manager().history_backfill_sources,
        )
        validation_end = datetime.now(UTC).replace(second=0, microsecond=0)
        validation_start = validation_end - timedelta(hours=24)
        for instrument in (SPOT_GOLD, SPOT_SILVER):
            with suppress(Exception):
                await runtime.database_store.standardize_candles(
                    instrument,
                    source_priority=_manager().history_source_priority(),
                    quote_derived_sources=_manager().history_quote_derived_sources(),
                    start=validation_start,
                    end=validation_end,
                )

    async def load_latest_quote(instrument, source_id):
        store = runtime.database_store
        if store is None or not store.is_open:
            return None
        return await store.load_latest_quote(instrument, source_id)

    stale_after_seconds = {
        JIN10_WEB_CHANNEL: web_settings.stale_after_seconds if web_settings else 12.0,
        JIN10_LOCAL_CHANNEL: local_settings.stale_after_seconds if local_settings else 12.0,
    }
    quote_cache = LatestQuoteCache(load_latest_quote)
    runtime.quote_views = QuoteViewService(
        quote_cache,
        stale_after=lambda source_id: stale_after_seconds.get(source_id, 60.0),
    )
    runtime.quote_stream = QuoteStreamCoordinator(load_quote=runtime.quote_views.get)

    def accept_raw_quote(value):
        quote_views = runtime.quote_views
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
        poll_channels={},
        source_channels={
            JIN10_CLIENT_SOURCE: (JIN10_WEB_CHANNEL, JIN10_LOCAL_CHANNEL),
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

    initial_routes = {}
    for instrument in (SPOT_GOLD, SPOT_SILVER):
        source_id = JIN10_CLIENT_SOURCE
        if runtime.database_store is not None and runtime.database_store.is_open:
            stored_source = await runtime.database_store.get_instrument_source(instrument)
            if stored_source is not None and not _manager().is_logical_source(stored_source):
                source_id = JIN10_CLIENT_SOURCE
                await runtime.database_store.set_instrument_source(instrument, source_id)
            elif stored_source is not None:
                source_id = stored_source
            else:
                await runtime.database_store.set_instrument_source(instrument, source_id)
        initial_routes[instrument] = source_id
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
    if runtime.candle_history is not None:
        await runtime.candle_history.close()
    if runtime.persistence is not None:
        await runtime.persistence.stop()
    if runtime.local_provider is not None:
        await runtime.local_provider.close()
    if runtime.web_provider is not None:
        await runtime.web_provider.close()
    runtime.local_provider = None
    runtime.web_provider = None
    runtime.manager = None
    runtime.persistence = None
    runtime.database_store = None
    runtime.persistence_setup_error = None
    runtime.quote_stream = None
    runtime.quote_views = None
    runtime.acquisition = None
    runtime.candle_history = None
    runtime.clear_caches()


app = FastAPI(title="Market Analysis Platform", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "PATCH", "POST", "PUT"],
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


def _database_store() -> PostgresMarketDataStore:
    store = runtime.database_store
    if store is None or not store.is_open:
        raise HTTPException(status_code=503, detail="PostgreSQL source routing is unavailable")
    return store


def _candle_history() -> LocalCandleHistoryService:
    _database_store()
    if runtime.candle_history is None:
        raise HTTPException(status_code=503, detail="Local candle history is unavailable")
    return runtime.candle_history


def _public_source(value: Any) -> dict[str, Any]:
    """Expose logical-source outcomes, never their internal channel topology."""

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
    """Expose logical routes without leaking their physical channel topology."""

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
            "mode": "postgres_validated_standard",
            "query_table": "standard_candles",
            "validation_table": "candle_validation_results",
            "backfill_pending": (
                runtime.candle_history.pending_count() if runtime.candle_history is not None else 0
            ),
        },
    }


@app.get("/api/sources")
async def sources(refresh: bool = Query(default=True)) -> list[dict[str, Any]]:
    values = await _manager().list_sources(refresh=refresh)
    return [_public_source(value) for value in values]


@app.post("/api/sources/{source_id}/test")
async def test_source(source_id: str) -> dict[str, Any]:
    started = perf_counter()
    try:
        manager = _manager()
        manager.validate_logical_source(
            SourceCapability.QUOTE,
            source_id,
            require_connection=False,
        )
        await manager.connect_source(source_id)
        await _acquisition().sample_source(source_id, SPOT_GOLD)
        value = await _quote_views().get(SPOT_GOLD, source_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProviderUnavailableError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "source_id": source_id,
        "code": "XAUUSD",
        "last": value.quote.last,
        "observed_at": value.quote.source.observed_at,
        "latency_ms": max(1, round((perf_counter() - started) * 1000)),
        "quality": value.quality,
        "unavailable_fields": value.unavailable_fields,
        "stale_fields": value.stale_fields,
    }


@app.get("/api/instruments")
async def instruments() -> list[dict[str, Any]]:
    supported = [
        InstrumentCatalogEntry(
            provider="canonical",
            provider_code="XAUUSD",
            name="现货黄金",
            instrument=SPOT_GOLD,
        ),
        InstrumentCatalogEntry(
            provider="canonical",
            provider_code="XAGUSD",
            name="现货白银",
            instrument=SPOT_SILVER,
        ),
    ]
    return [asdict(entry) for entry in supported]


async def _instrument_source(code: str) -> tuple[str, Any, str]:
    normalized_code = code.upper()
    instrument = runtime.mapper.from_provider_code(normalized_code)
    store = _database_store()
    source_id = await store.get_instrument_source(instrument)
    if source_id is None or not _manager().is_logical_source(source_id):
        source_id = JIN10_CLIENT_SOURCE
        await store.set_instrument_source(instrument, source_id)
    return normalized_code, instrument, source_id


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
    normalized_code = code.upper()
    instrument = runtime.mapper.from_provider_code(normalized_code)
    try:
        _manager().validate_logical_source(SourceCapability.QUOTE, update.source_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProviderUnavailableError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await _database_store().set_instrument_source(instrument, update.source_id)
    await _acquisition().set_route(instrument, update.source_id)
    runtime.clear_caches()
    return {"code": normalized_code, "source_id": update.source_id}


@app.get("/api/quotes/{code}")
async def quote(code: str) -> dict[str, Any]:
    _, instrument, source_id = await _instrument_source(code)
    value = await _quote_views().get(instrument, source_id)
    return asdict(value)


@app.get("/api/candles/{code}")
async def candles(
    code: str,
    source: str | None = Query(default=None, deprecated=True),
    count: int = Query(default=100, ge=1, le=100),
    time: int | None = Query(default=None, description="Unix seconds"),
) -> list[dict[str, Any]]:
    # `source` is accepted temporarily for older clients but intentionally ignored:
    # all users and contracts read the same canonical history from PostgreSQL.
    del source
    normalized_code = code.upper()
    instrument = runtime.mapper.from_provider_code(normalized_code)
    start = datetime.fromtimestamp(time, tz=UTC) if time else None
    values = await _candle_history().get_candles(
        instrument,
        start=start,
        count=count,
    )
    return [asdict(value) for value in values]


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
            return FileResponse(requested)
        return FileResponse(_web_dist / "index.html")


def run() -> None:
    host = os.environ.get("MARKET_ANALYSIS_HOST", "127.0.0.1")
    port = int(os.environ.get("MARKET_ANALYSIS_PORT", "8000"))
    uvicorn.run("market_analysis.api:app", host=host, port=port, reload=False)
