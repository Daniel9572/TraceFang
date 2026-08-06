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

from market_analysis.application.history import LocalCandleHistoryService
from market_analysis.application.realtime import QuoteStreamCoordinator
from market_analysis.application.sources import (
    MarketSourceManager,
    ProviderProbe,
    SourceAccessModel,
    SourceCapability,
    SourceQuota,
    SourceRegistration,
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
from market_analysis.infrastructure.mcp import StreamableHttpMcpClient
from market_analysis.infrastructure.postgres import (
    BufferedMarketDataWriter,
    PostgresMarketDataStore,
    PostgresSettings,
)
from market_analysis.infrastructure.providers.jin10 import (
    SPOT_GOLD,
    SPOT_SILVER,
    Jin10Provider,
    Jin10Settings,
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
from market_analysis.infrastructure.quota import DailyToolBudget
from market_analysis.infrastructure.source_config import JsonSourceConfigurationStore

_repo_root = Path(__file__).resolve().parents[2]
load_project_environment(_repo_root)


class Runtime:
    def __init__(self) -> None:
        self.mcp_provider: Jin10Provider | None = None
        self.local_provider: Jin10LocalProvider | None = None
        self.web_provider: Jin10WebProvider | None = None
        self.manager: MarketSourceManager | None = None
        self.persistence: BufferedMarketDataWriter | None = None
        self.database_store: PostgresMarketDataStore | None = None
        self.persistence_setup_error: str | None = None
        self.quote_stream: QuoteStreamCoordinator | None = None
        self.candle_history: LocalCandleHistoryService | None = None
        self.mapper = Jin10SymbolMapper()
        self.catalog_cache: AsyncTtlCache[Any] = AsyncTtlCache()

    def clear_caches(self) -> None:
        self.catalog_cache.clear()


runtime = Runtime()


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)


class InstrumentSourceUpdate(BaseModel):
    source_id: str = Field(min_length=1)


def _build_mcp_provider(settings: Jin10Settings) -> Jin10Provider:
    client = StreamableHttpMcpClient(
        endpoint=settings.endpoint,
        bearer_token=settings.bearer_token,
        timeout_seconds=settings.timeout_seconds,
    )
    budget = DailyToolBudget(
        provider="jin10_mcp",
        daily_limit=settings.daily_tool_limit,
        reserve=settings.quota_reserve,
        timezone=settings.quota_timezone,
    )
    return Jin10Provider(client, budget=budget)


def _source_store_path() -> Path:
    configured = os.environ.get("MARKET_ANALYSIS_SOURCE_CONFIG", "").strip()
    return Path(configured).expanduser() if configured else _repo_root / "data" / "sources.json"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    mcp_provider: Jin10Provider | None = None
    mcp_settings: Jin10Settings | None = None
    mcp_setup_error: str | None = None
    try:
        mcp_settings = Jin10Settings.from_env()
        mcp_provider = _build_mcp_provider(mcp_settings)
    except (ValueError, ProviderError) as error:
        mcp_setup_error = str(error)
        if mcp_provider is not None:
            await mcp_provider.close()
        mcp_provider = None

    local_provider: Jin10LocalProvider | None = None
    local_setup_error: str | None = None
    try:
        local_settings = Jin10LocalSettings.from_env()
        local_provider = Jin10LocalProvider(local_settings)
        await local_provider.open()
    except (ValueError, ProviderError) as error:
        local_setup_error = str(error)
        if local_provider is not None:
            await local_provider.close()
        local_provider = None

    web_provider: Jin10WebProvider | None = None
    web_setup_error: str | None = None
    try:
        web_settings = Jin10WebSettings.from_env()
        web_provider = Jin10WebProvider(web_settings)
        await web_provider.open()
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

    async def connect_mcp_provider() -> ProviderProbe:
        if mcp_provider is None:
            raise ProviderUnavailableError(mcp_setup_error or "金十官方 MCP 尚未配置")
        await mcp_provider.open()
        return ProviderProbe(
            available=True,
            state="ready",
            detail="协议握手与能力检查完成",
            checked_at=datetime.now(UTC),
        )

    async def report_mcp_quotas() -> tuple[SourceQuota, ...]:
        if mcp_provider is None or mcp_settings is None:
            return ()
        labels = {"get_quote": "报价", "get_kline": "K 线"}
        snapshots = await mcp_provider.budget.snapshots(labels)
        return tuple(
            SourceQuota(
                key=snapshot.tool_name,
                label=labels[snapshot.tool_name],
                used=snapshot.used,
                limit=snapshot.limit,
                reserve=snapshot.reserve,
                available=snapshot.available,
                usage_percent=snapshot.usage_percent,
                warning_percent=mcp_settings.quota_warning_percent,
                period=snapshot.period,
                resets_at=snapshot.resets_at,
                scope=snapshot.scope,
            )
            for snapshot in snapshots
        )

    registrations = (
        SourceRegistration(
            source_id="jin10_mcp",
            display_name="金十官方 MCP",
            description="结构化官方接口。适合精确报价、K 线、资讯与日历。",
            capabilities=frozenset(
                {
                    SourceCapability.QUOTE,
                    SourceCapability.CANDLES,
                    SourceCapability.CATALOG,
                    SourceCapability.NEWS,
                    SourceCapability.CALENDAR,
                }
            ),
            default_enabled=True,
            default_priority=20,
            delayed=False,
            requires_running_app=False,
            history_priority=10,
            structured=True,
            quote_poll_interval_seconds=60,
            quote_streaming=False,
            access_model=SourceAccessModel.LIMITED,
            access_note=(
                "每个用户、每个工具每天最多调用 "
                f"{mcp_settings.daily_tool_limit if mcp_settings else 1500} 次; "
                "北京时间自然日重置。用量为本应用运行期间记录, 不包含其他客户端。"
            ),
            manual_connection_required=True,
            connector=connect_mcp_provider if mcp_provider is not None else None,
            quota_reporter=report_mcp_quotas,
            quote_provider=mcp_provider,
            candle_provider=mcp_provider,
            setup_error=mcp_setup_error,
        ),
        SourceRegistration(
            source_id="jin10_local",
            display_name="金十本地行情",
            description=(
                "读取本机金十登录会话。直连并解码结构化行情推送。首次需在金十软件登录。"
                "建立会话后无需保持软件窗口运行。"
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
            quote_provider=local_provider,
            candle_provider=local_provider,
            probe=probe_local_provider if local_provider is not None else None,
            setup_error=local_setup_error,
        ),
        SourceRegistration(
            source_id="jin10_web",
            display_name="金十极速行情",
            description=(
                "金十官网公开的结构化行情推送。价格变化后即时到达。"
                "不消耗 MCP 额度。无需运行桌面客户端。"
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
            access_model=SourceAccessModel.UNMETERED,
            access_note="公开网页通道。无调用次数额度。接口升级时需要重新验证协议。",
            quote_provider=web_provider,
            probe=probe_web_provider if web_provider is not None else None,
            setup_error=web_setup_error,
        ),
    )
    runtime.mcp_provider = mcp_provider
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

    async def fetch_quote(instrument, source):
        return await _manager().get_quote(instrument, source=source)

    def record_quote(value):
        if runtime.persistence is not None:
            runtime.persistence.submit_quote(value)

    runtime.quote_stream = QuoteStreamCoordinator(
        fetch_quote=fetch_quote,
        record_quote=record_quote,
        poll_interval=_manager().quote_poll_interval,
        is_push_source=_manager().quote_is_streaming,
    )
    local_quote_listener = None
    if local_provider is not None:
        local_quote_listener = runtime.quote_stream.publish_quote
        local_provider.add_quote_listener(local_quote_listener)
    web_quote_listener = None
    if web_provider is not None:
        web_quote_listener = runtime.quote_stream.publish_quote
        web_provider.add_quote_listener(web_quote_listener)
    runtime.clear_caches()
    yield
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
    if runtime.mcp_provider is not None:
        await runtime.mcp_provider.close()
    if runtime.local_provider is not None:
        await runtime.local_provider.close()
    if runtime.web_provider is not None:
        await runtime.web_provider.close()
    runtime.mcp_provider = None
    runtime.local_provider = None
    runtime.web_provider = None
    runtime.manager = None
    runtime.persistence = None
    runtime.database_store = None
    runtime.persistence_setup_error = None
    runtime.quote_stream = None
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


def _record_quote(value: Any) -> None:
    if runtime.persistence is not None:
        runtime.persistence.submit_quote(value)


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
    sources = await manager.list_sources(refresh=False)
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
        "sources": [asdict(item) for item in sources],
        "database": database,
        "history": {
            "mode": "postgres_local",
            "source_priority": manager.history_source_priority(),
            "quote_derived_sources": manager.history_quote_derived_sources(),
            "backfill_sources": manager.history_backfill_sources(),
            "backfill_pending": (
                runtime.candle_history.pending_count() if runtime.candle_history is not None else 0
            ),
        },
        "protocol_version": (
            runtime.mcp_provider.client.negotiated_version
            if runtime.mcp_provider is not None
            else None
        ),
    }


@app.get("/api/sources")
async def sources(refresh: bool = Query(default=True)) -> list[dict[str, Any]]:
    values = await _manager().list_sources(refresh=refresh)
    return [asdict(value) for value in values]


@app.patch("/api/sources/{source_id}")
async def update_source(source_id: str, update: SourceUpdate) -> dict[str, Any]:
    manager = _manager()
    try:
        manager.configure(
            source_id,
            enabled=update.enabled,
            priority=update.priority,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    runtime.clear_caches()
    values = await manager.list_sources(refresh=False)
    return asdict(next(item for item in values if item.source_id == source_id))


@app.post("/api/sources/{source_id}/test")
async def test_source(source_id: str) -> dict[str, Any]:
    started = perf_counter()
    try:
        manager = _manager()
        await manager.connect_source(source_id)
        value = await manager.get_quote(SPOT_GOLD, source=source_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if runtime.quote_stream is not None:
        runtime.quote_stream.publish_quote(value)
    else:
        _record_quote(value)
    return {
        "source_id": source_id,
        "code": "XAUUSD",
        "last": value.last,
        "observed_at": value.source.observed_at,
        "latency_ms": max(1, round((perf_counter() - started) * 1000)),
    }


@app.get("/api/instruments")
async def instruments() -> list[dict[str, Any]]:
    if runtime.mcp_provider is not None and _manager().is_connected("jin10_mcp"):
        entries = await runtime.catalog_cache.get_or_load(
            "jin10_mcp:catalog",
            ttl_seconds=3600,
            loader=runtime.mcp_provider.list_instruments,
        )
        supported = [entry for entry in entries if entry.instrument is not None]
    else:
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
    source_id = await store.get_instrument_source(instrument) or "jin10_local"
    # Only the real-time route is contract-specific. Historical candles are global.
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
        _manager().validate_source(SourceCapability.QUOTE, update.source_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    await _database_store().set_instrument_source(instrument, update.source_id)
    runtime.clear_caches()
    return {"code": normalized_code, "source_id": update.source_id}


@app.get("/api/instruments/{code}/source-routes", include_in_schema=False)
async def legacy_instrument_source_routes(code: str) -> dict[str, Any]:
    """Temporary read compatibility for clients from the split-route release."""

    value = await instrument_source(code)
    return {
        "code": value["code"],
        "routes": {
            SourceCapability.QUOTE.value: value["source_id"],
            SourceCapability.CANDLES.value: value["source_id"],
        },
    }


@app.put("/api/instruments/{code}/source-routes", include_in_schema=False)
async def legacy_update_instrument_source_route(
    code: str,
    update: InstrumentSourceUpdate,
) -> dict[str, Any]:
    value = await update_instrument_source(code, update)
    return {
        "code": value["code"],
        "routes": {
            SourceCapability.QUOTE.value: value["source_id"],
            SourceCapability.CANDLES.value: value["source_id"],
        },
    }


@app.get("/api/quotes/{code}")
async def quote(
    code: str,
    source: str = Query(min_length=1),
) -> dict[str, Any]:
    normalized_code = code.upper()
    instrument = runtime.mapper.from_provider_code(normalized_code)
    value = await _manager().get_quote(instrument, source=source)
    if runtime.quote_stream is not None:
        runtime.quote_stream.publish_quote(value)
    else:
        _record_quote(value)
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
    source = websocket.query_params.get("source", "").strip()
    if not source:
        await websocket.close(code=1008, reason="a concrete source is required")
        return
    try:
        instrument = runtime.mapper.from_provider_code(code.upper())
    except ProviderError:
        await websocket.close(code=1008, reason="instrument is not supported")
        return
    await websocket.accept()
    try:
        async with _quote_stream().subscribe(instrument, source=source) as queue:
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
