from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from market_analysis.application.sources import (
    MarketSourceManager,
    SourceCapability,
    SourceRegistration,
)
from market_analysis.cache import AsyncTtlCache
from market_analysis.domain.errors import (
    InstrumentNotSupportedError,
    ProviderError,
    ProviderRateLimitError,
)
from market_analysis.domain.models import InstrumentCatalogEntry
from market_analysis.infrastructure.mcp import StreamableHttpMcpClient
from market_analysis.infrastructure.providers.jin10 import (
    SPOT_GOLD,
    SPOT_SILVER,
    Jin10Provider,
    Jin10Settings,
    Jin10SymbolMapper,
)
from market_analysis.infrastructure.providers.jin10_desktop import Jin10DesktopProvider
from market_analysis.infrastructure.quota import DailyToolBudget
from market_analysis.infrastructure.source_config import JsonSourceConfigurationStore

_repo_root = Path(__file__).resolve().parents[2]


class Runtime:
    def __init__(self) -> None:
        self.mcp_provider: Jin10Provider | None = None
        self.desktop_provider: Jin10DesktopProvider | None = None
        self.manager: MarketSourceManager | None = None
        self.mapper = Jin10SymbolMapper()
        self.quote_cache: AsyncTtlCache[Any] = AsyncTtlCache()
        self.candle_cache: AsyncTtlCache[Any] = AsyncTtlCache()
        self.catalog_cache: AsyncTtlCache[Any] = AsyncTtlCache()

    def clear_caches(self) -> None:
        self.quote_cache.clear()
        self.candle_cache.clear()
        self.catalog_cache.clear()


runtime = Runtime()


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)


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
    )
    return Jin10Provider(client, budget=budget)


def _source_store_path() -> Path:
    configured = os.environ.get("MARKET_ANALYSIS_SOURCE_CONFIG", "").strip()
    return Path(configured).expanduser() if configured else _repo_root / "data" / "sources.json"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    mcp_provider: Jin10Provider | None = None
    mcp_setup_error: str | None = None
    try:
        settings = Jin10Settings.from_env()
        mcp_provider = _build_mcp_provider(settings)
        await mcp_provider.open()
    except (ValueError, ProviderError) as error:
        mcp_setup_error = str(error)
        if mcp_provider is not None:
            await mcp_provider.close()
        mcp_provider = None

    desktop_provider = Jin10DesktopProvider(symbol_mapper=runtime.mapper)
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
            default_priority=10,
            delayed=False,
            requires_running_app=False,
            quote_provider=mcp_provider,
            candle_provider=mcp_provider,
            setup_error=mcp_setup_error,
        ),
        SourceRegistration(
            source_id="jin10_desktop",
            display_name="本地金十软件",
            description="窗口截图与 Windows OCR。成本低、略有延迟。首版提供黄金和白银报价。",
            capabilities=frozenset({SourceCapability.QUOTE}),
            default_enabled=True,
            default_priority=20,
            delayed=True,
            requires_running_app=True,
            quote_provider=desktop_provider,
            probe=desktop_provider.probe,
        ),
    )
    runtime.mcp_provider = mcp_provider
    runtime.desktop_provider = desktop_provider
    runtime.manager = MarketSourceManager(
        registrations,
        store=JsonSourceConfigurationStore(_source_store_path()),
    )
    runtime.clear_caches()
    yield
    if runtime.mcp_provider is not None:
        await runtime.mcp_provider.close()
    runtime.mcp_provider = None
    runtime.desktop_provider = None
    runtime.manager = None
    runtime.clear_caches()


app = FastAPI(title="Market Analysis Platform", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "PATCH"],
    allow_headers=["*"],
)


def _manager() -> MarketSourceManager:
    if runtime.manager is None:
        raise HTTPException(status_code=503, detail="market data runtime is not ready")
    return runtime.manager


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
    return {
        "status": "ok" if healthy else "degraded",
        "sources": [asdict(item) for item in sources],
        "protocol_version": (
            runtime.mcp_provider.client.negotiated_version
            if runtime.mcp_provider is not None
            else None
        ),
    }


@app.get("/api/sources")
async def sources() -> list[dict[str, Any]]:
    values = await _manager().list_sources(refresh=True)
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


@app.get("/api/instruments")
async def instruments() -> list[dict[str, Any]]:
    if runtime.mcp_provider is not None:
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


@app.get("/api/quotes/{code}")
async def quote(
    code: str,
    source: str = Query(default="auto", min_length=1),
) -> dict[str, Any]:
    normalized_code = code.upper()
    instrument = runtime.mapper.from_provider_code(normalized_code)
    value = await runtime.quote_cache.get_or_load(
        f"quote:{source}:{normalized_code}",
        ttl_seconds=5 if source == "jin10_desktop" else 15,
        loader=lambda: _manager().get_quote(instrument, source=source),
    )
    return asdict(value)


@app.get("/api/quotes/{code}/compare")
async def compare_quotes(code: str) -> dict[str, Any]:
    normalized_code = code.upper()
    instrument = runtime.mapper.from_provider_code(normalized_code)
    results = await _manager().compare_quotes(instrument)
    successful = [item for item in results if item.quote is not None]
    reference = next(
        (item.quote for item in successful if item.source_id == "jin10_mcp"),
        successful[0].quote if successful else None,
    )
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for item in results:
        deviation: Decimal | None = None
        deviation_percent: Decimal | None = None
        sample_age_seconds: float | None = None
        if item.quote is not None:
            sample_age_seconds = max(
                0.0,
                (now - item.quote.source.observed_at.astimezone(UTC)).total_seconds(),
            )
            if reference is not None:
                deviation = item.quote.last - reference.last
                if reference.last != 0:
                    deviation_percent = deviation / reference.last * Decimal("100")
        rows.append(
            {
                "source_id": item.source_id,
                "quote": asdict(item.quote) if item.quote is not None else None,
                "error": item.error,
                "request_latency_ms": item.request_latency_ms,
                "sample_age_seconds": sample_age_seconds,
                "deviation": deviation,
                "deviation_percent": deviation_percent,
            }
        )
    return {
        "code": normalized_code,
        "reference_source": reference.source.provider if reference is not None else None,
        "items": rows,
    }


@app.get("/api/candles/{code}")
async def candles(
    code: str,
    source: str = Query(default="auto", min_length=1),
    count: int = Query(default=100, ge=1, le=100),
    time: int | None = Query(default=None, description="Unix seconds"),
) -> list[dict[str, Any]]:
    normalized_code = code.upper()
    instrument = runtime.mapper.from_provider_code(normalized_code)
    start = datetime.fromtimestamp(time, tz=UTC) if time else None
    cache_key = f"candles:{source}:{normalized_code}:{count}:{time or 'latest'}"
    values = await runtime.candle_cache.get_or_load(
        cache_key,
        ttl_seconds=30,
        loader=lambda: _manager().get_candles(
            instrument,
            source=source,
            start=start,
            count=count,
        ),
    )
    return [asdict(value) for value in values]


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
