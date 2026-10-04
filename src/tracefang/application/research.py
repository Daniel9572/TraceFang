"""Native-period research data, deliberately separate from the minute evidence ledger."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections.abc import Mapping
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from tracefang.akshare_worker import AK_UNDERLYINGS, contract_key


class ResearchError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class ResearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["eastmoney", "sina", "tushare", "alpaca", "tencent", "akshare"]
    symbol: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9.\-]+$")
    asset: Literal["equity", "etf", "index", "future", "option"] = "equity"
    period: Literal["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1M"] = "1d"
    adjustment: Literal["raw", "forward", "backward"] = "raw"
    before: AwareDatetime | None = None
    limit: int = Field(default=300, ge=1, le=1000)


SOURCE_SPECS = (
    {
        "id": "eastmoney",
        "name": "东方财富",
        "assets": ["equity", "etf", "index"],
        "periods": ["1d", "1w", "1M"],
        "credentials": [],
        "market": "沪深股票 / ETF / 指数",
        "note": "公开网页行情; 收盘研究用途, 无可用性保证。复权由来源计算。",
        "url": "https://quote.eastmoney.com/",
        "mode": "daily",
    },
    {
        "id": "sina",
        "name": "新浪期货",
        "assets": ["future"],
        "periods": ["1d"],
        "credentials": [],
        "market": "国内商品期货 / 连续合约",
        "note": "公开日线; 以 0 结尾为连续观察序列, 不能视为可下单合约。",
        "url": "https://finance.sina.com.cn/futures/",
        "mode": "daily",
    },
    {
        "id": "tencent",
        "name": "腾讯证券",
        "assets": ["equity", "etf", "index"],
        "periods": ["1d", "1w", "1M"],
        "credentials": [],
        "market": "沪深股票 / ETF / 指数",
        "note": "公开原生周期行情; 与东方财富分别读取和缓存, 可手动切换。不承诺实时性。",
        "url": "https://gu.qq.com/",
        "mode": "daily",
    },
    {
        "id": "tushare",
        "name": "Tushare Pro",
        "assets": ["equity", "etf", "index", "future", "option"],
        "periods": ["1d"],
        "credentials": ["TUSHARE_TOKEN"],
        "market": "中国股票 / ETF / 指数 / 期货 / 期权",
        "note": "官方授权接口; 日线及合约目录权限依账户积分而定。不复权。",
        "url": "https://tushare.pro/document/1?doc_id=108",
        "mode": "daily",
    },
    {
        "id": "alpaca",
        "name": "Alpaca",
        "assets": ["equity", "etf", "option"],
        "periods": ["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1M"],
        "credentials": ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"],
        "market": "美股 / ETF / 美股期权",
        "note": "股票使用 IEX(单交易所); 期权链使用 indicative(指示性延迟报价); 原生历史, 非推送。",
        "url": "https://docs.alpaca.markets/us/docs/historical-stock-data-1",
        "mode": "snapshot",
    },
    {
        "id": "akshare",
        "name": "AKShare · 国内市场",
        "assets": ["future", "option", "equity", "etf"],
        "periods": ["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1M"],
        "asset_periods": {
            "future": ["1m", "5m", "15m", "30m", "1h", "1d"],
            "option": ["1d"],
            "equity": ["1d", "1w", "1M"],
            "etf": ["1d", "1w", "1M"],
        },
        "credentials": [],
        "market": "国内股票 / ETF / 期货分钟线 / ETF、股指与商品期权",
        "note": "股票来自东方财富, 期货与期权报价来自新浪, 合约元数据来自 OpenCTP。"
        "分钟历史仅为来源可提供的近期窗口; 不承诺完整历史或实时推送。",
        "url": "https://github.com/akfamily/akshare",
        "mode": "snapshot",
    },
)


def research_catalog() -> list[dict[str, Any]]:
    groups = [
        (
            "tencent",
            "equity",
            "CNY",
            [
                ("600519.SH", "贵州茅台"),
                ("601318.SH", "中国平安"),
                ("600036.SH", "招商银行"),
                ("000001.SZ", "平安银行"),
                ("000858.SZ", "五粮液"),
                ("300750.SZ", "宁德时代"),
                ("002594.SZ", "比亚迪"),
                ("600900.SH", "长江电力"),
                ("601398.SH", "工商银行"),
            ],
        ),
        (
            "tencent",
            "etf",
            "CNY",
            [
                ("510300.SH", "沪深300 ETF"),
                ("510050.SH", "上证50 ETF"),
                ("588000.SH", "科创50 ETF"),
                ("159915.SZ", "创业板 ETF"),
                ("518880.SH", "黄金 ETF"),
            ],
        ),
        ("tencent", "index", "CNY", [("000001.SH", "上证指数"), ("399001.SZ", "深证成指")]),
        (
            "akshare",
            "future",
            "CNY",
            [
                ("AU0", "沪金连续"),
                ("AG0", "沪银连续"),
                ("CU0", "沪铜连续"),
                ("AL0", "沪铝连续"),
                ("RB0", "螺纹钢连续"),
                ("HC0", "热卷连续"),
                ("SC0", "原油连续"),
                ("RU0", "橡胶连续"),
                ("M0", "豆粕连续"),
                ("I0", "铁矿石连续"),
                ("Y0", "豆油连续"),
                ("P0", "棕榈油连续"),
                ("C0", "玉米连续"),
                ("CF0", "棉花连续"),
                ("SR0", "白糖连续"),
                ("TA0", "PTA连续"),
                ("MA0", "甲醇连续"),
                ("LC0", "碳酸锂连续"),
            ],
        ),
        (
            "alpaca",
            "equity",
            "USD",
            [
                ("AAPL", "Apple"),
                ("MSFT", "Microsoft"),
                ("NVDA", "NVIDIA"),
                ("AMZN", "Amazon"),
                ("GOOGL", "Alphabet"),
                ("TSLA", "Tesla"),
                ("META", "Meta"),
            ],
        ),
        (
            "alpaca",
            "etf",
            "USD",
            [
                ("SPY", "S&P 500 ETF"),
                ("QQQ", "Nasdaq 100 ETF"),
                ("GLD", "Gold ETF"),
                ("TLT", "20+ Year Treasury ETF"),
            ],
        ),
    ]
    return [
        dict(source=source, asset=asset, currency=currency, symbol=symbol, name=name)
        for source, asset, currency, entries in groups
        for symbol, name in entries
    ]


def _number(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite market value")
    return result


def _optional_number(value: Any) -> float | None:
    try:
        return _number(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def china_history_end(query: ResearchQuery) -> datetime:
    zone = ZoneInfo("Asia/Shanghai")
    if query.before is None:
        return datetime.now(zone)
    boundary = query.before.astimezone(zone)
    if query.period in {"1w", "1M"}:
        boundary = boundary.replace(hour=0, minute=0, second=0, microsecond=0)
        boundary = (
            boundary.replace(day=1)
            if query.period == "1M"
            else boundary - timedelta(days=boundary.weekday())
        )
    return boundary - timedelta(seconds=1)


def period_has_closed(stamp: datetime, query: ResearchQuery, now: datetime) -> bool:
    """Calendar bars close at the next local boundary, conservatively ignoring session hours.

    Eastmoney labels weekly/monthly bars by the last session, while Alpaca uses the start.
    Both labels belong to the same calendar period; adding a fixed 31 days is incorrect.
    """
    if query.period.endswith(("m", "h")):
        seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}[query.period]
        return stamp + timedelta(seconds=seconds) <= now
    zone = ZoneInfo("America/New_York" if query.source == "alpaca" else "Asia/Shanghai")
    local = stamp.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    if query.period == "1M":
        boundary = local.replace(
            year=local.year + (local.month == 12), month=local.month % 12 + 1, day=1
        )
    elif query.period == "1w":
        boundary = local + timedelta(days=7 - local.weekday())
    else:
        boundary = local + timedelta(days=1)
    return boundary <= now


def normalize_bars(rows: list[dict[str, Any]], query: ResearchQuery) -> tuple[list[dict], int]:
    """Reject malformed OHLC; preserve missing volume and never synthesize empty sessions."""
    normalized: dict[str, dict] = {}
    rejected = 0
    now = datetime.now(UTC)
    seconds = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "1d": 86400,
        "1w": 604800,
        "1M": 2678400,
    }[query.period]
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            stamp = stamp.astimezone(UTC)
            if stamp > now or (query.before is not None and stamp >= query.before):
                continue
            op, hi, lo, close = (_number(row[field]) for field in ("open", "high", "low", "close"))
            if lo > min(op, close) or hi < max(op, close) or lo > hi:
                raise ValueError("invalid OHLC range")
            if query.asset != "future" and lo < 0:
                raise ValueError("negative non-futures price")
            volume = None if row.get("volume") is None else _number(row["volume"])
            if volume is not None and volume < 0:
                raise ValueError("negative volume")
            open_interest = _optional_number(row.get("open_interest"))
            if open_interest is not None and open_interest < 0:
                open_interest = None
            iso = stamp.isoformat()
            normalized[iso] = {
                "instrument": {
                    "symbol": query.symbol.upper(),
                    "asset_class": query.asset,
                    "base": None,
                    "quote": "USD" if query.source == "alpaca" else "CNY",
                    "venue": None,
                },
                "open_time": iso,
                "interval": seconds,
                "open": op,
                "high": hi,
                "low": lo,
                "close": close,
                "volume": volume,
                **({"open_interest": open_interest} if query.source == "akshare" else {}),
                "source": {
                    "provider": query.source,
                    "provider_symbol": query.symbol.upper(),
                    "observed_at": iso,
                    "received_at": now.isoformat(),
                },
                "evidence_channel_id": query.source,
                "state": "final"
                if period_has_closed(stamp, query, now)
                else "provisional_authoritative",
                "revision": 1,
                "finalized_at": None,
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected += 1
    return sorted(normalized.values(), key=lambda row: row["open_time"]), rejected


class ResearchDataService:
    def __init__(
        self,
        cache_path: Path,
        *,
        client: httpx.AsyncClient | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.environment = os.environ if environment is None else environment
        self.client = client or httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        self.owns_client = client is None
        self.inflight: dict[str, asyncio.Task] = {}
        self.gates = {spec["id"]: asyncio.Lock() for spec in SOURCE_SPECS}
        self.last_request: dict[str, float] = {}
        self.diagnostics: dict[str, dict] = {}

    async def close(self) -> None:
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.owns_client:
            await self.client.aclose()

    def sources(self) -> list[dict]:
        return [
            {
                **spec,
                "configured": all(
                    bool(self.environment.get(key, "").strip()) for key in spec["credentials"]
                )
                and (spec["id"] != "akshare" or importlib.util.find_spec("akshare") is not None),
                "diagnostic": self.diagnostics.get(spec["id"]),
            }
            for spec in SOURCE_SPECS
        ]

    def validate(self, query: ResearchQuery) -> None:
        spec = next(item for item in self.sources() if item["id"] == query.source)
        if not spec["configured"]:
            if query.source == "akshare":
                raise ResearchError("AKShare 运行依赖尚未安装, 请更新应用运行版本。", 409)
            raise ResearchError(
                f"{spec['name']} 尚未配置。请在 .env.local 设置 "
                + "、".join(spec["credentials"])
                + " 后重启服务。",
                409,
            )
        periods = spec.get("asset_periods", {}).get(query.asset, spec["periods"])
        if query.asset not in spec["assets"] or query.period not in periods:
            raise ResearchError("该来源不支持此资产或周期, 请切换来源或周期。", 422)
        if query.source == "tencent" and query.limit > 639:
            raise ResearchError("腾讯行情每页最多 639 根, 请使用分页读取。", 422)
        if query.adjustment != "raw" and (
            query.source not in {"eastmoney", "tencent", "akshare"}
            or query.asset not in {"equity", "etf"}
        ):
            raise ResearchError("此来源/资产仅支持不复权研究。", 422)
        patterns = {
            "eastmoney": r"\d{6}\.(SH|SZ|BJ)",
            "tencent": r"\d{6}\.(SH|SZ)",
            "sina": r"[A-Z]{1,3}\d{1,4}",
            "tushare": r"[A-Z0-9-]{1,28}\.(SH|SZ|BJ|SHF|DCE|CZC|CFX|INE|GFE)",
            "alpaca": r"[A-Z][A-Z0-9.]{0,29}",
            "akshare": r"\d{6}\.(SH|SZ|BJ)"
            if query.asset in {"equity", "etf"}
            else r"[A-Z]{1,3}\d{1,4}"
            if query.asset == "future"
            else r"(?:[19]\d{7}(?:\.(?:SH|SZ))?|[A-Z]{1,3}\d{3,4}-?[CP]-?\d+(?:\.\d+)?)",
        }
        if not re.fullmatch(patterns[query.source], query.symbol.upper()):
            raise ResearchError("证券代码格式不正确, 请参考来源旁的示例。", 422)

    def _cache(self, key: str, payload: dict | None = None) -> dict | None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.cache_path, timeout=5)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS research_pages "
                "(id TEXT PRIMARY KEY, saved REAL, payload TEXT)"
            )
            if payload is not None:
                db.execute(
                    "INSERT OR REPLACE INTO research_pages VALUES (?, ?, ?)",
                    (key, time.time(), json.dumps(payload, allow_nan=False)),
                )
                db.execute(
                    "DELETE FROM research_pages WHERE id NOT IN "
                    "(SELECT id FROM research_pages ORDER BY saved DESC LIMIT 256)"
                )
                return None
            row = db.execute("SELECT payload FROM research_pages WHERE id = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    async def bars(self, query: ResearchQuery, *, refresh: bool = False) -> dict:
        query = query.model_copy(update={"symbol": query.symbol.upper()})
        self.validate(query)
        key = hashlib.sha256(("v3:" + query.model_dump_json()).encode()).hexdigest()
        try:
            cached = await asyncio.to_thread(self._cache, key)
        except (sqlite3.Error, OSError, ValueError):
            # A damaged or unwritable local cache must not block a healthy upstream.
            cached = None
        ttl = 30 if query.period.endswith(("m", "h")) else 300
        if not refresh and cached and time.time() - cached["cached_at"] < ttl:
            return {**cached, "cache_state": "cached"}
        if key not in self.inflight:
            if len(self.inflight) >= 24:
                raise ResearchError("数据请求较多, 请稍后重试。", 429)
            task = asyncio.create_task(self._load(query, key, cached))
            self.inflight[key] = task
            task.add_done_callback(lambda done: self._finish(key, done))
        return copy.deepcopy(await asyncio.shield(self.inflight[key]))

    def _finish(self, key: str, task: asyncio.Task) -> None:
        self.inflight.pop(key, None)
        if not task.cancelled():
            task.exception()  # Consume failures even if every HTTP waiter disconnected.

    async def _load(self, query: ResearchQuery, key: str, cached: dict | None) -> dict:
        try:
            # Bound queueing plus network time when rapid symbol switches contend for a source.
            async with asyncio.timeout(35):
                rows = await getattr(self, f"_{query.source}_bars")(query)
            bars, rejected = normalize_bars(rows, query)
            if not bars and rejected:
                raise ResearchError("来源返回的 OHLC 数据不合法, 已拒绝入库。")
            has_more = len(bars) > query.limit
            bars = bars[-query.limit :]
            warnings = []
            if rejected:
                warnings.append(f"已排除 {rejected} 条无效行情。")
            if query.asset == "future" and query.symbol.endswith("0"):
                warnings.append("连续期货是观察序列; 换月可能产生跳空, 不代表可交易合约回报。")
            if query.adjustment == "raw" and query.asset in {"equity", "etf"}:
                warnings.append("不复权价格可能包含除权除息跳空。")
            if query.source == "alpaca":
                warnings.append(
                    "IEX 仅覆盖单交易所。"
                    if query.asset != "option"
                    else "期权历史至少延迟 15 分钟, 数据权限依账户而定。"
                )
            if query.source == "akshare":
                warnings.append("AKShare 是采集适配器; 原始来源为东方财富或新浪, 非授权实时专线。")
                if query.asset == "future" and query.period != "1d":
                    warnings.append(
                        "分钟线仅覆盖上游近期窗口; 分页耗尽不代表上市以来的历史已完整。"
                    )
                if query.asset == "option":
                    warnings.append(
                        "期权无成交日可能缺少 K 线; 请核对截止时间, 读取成功不代表最新交易日。"
                    )
            payload = {
                "query": query.model_dump(mode="json"),
                "items": bars,
                "next_before": bars[0]["open_time"] if has_more and bars else None,
                "cache_state": "fresh",
                "cached_at": time.time(),
                "fetched_at": datetime.now(UTC).isoformat(),
                "data_as_of": bars[-1]["open_time"] if bars else None,
                "rejected_rows": rejected,
                "warnings": warnings,
                "volume_unit": "来源原始单位(市场定义为手/股)"
                if query.source == "tencent"
                else "张"
                if query.source == "akshare" and query.asset == "option"
                else "手"
                if query.source != "alpaca"
                else "张"
                if query.asset == "option"
                else "股",
                "currency": "USD" if query.source == "alpaca" else "CNY",
                "feed": "IEX"
                if query.source == "alpaca" and query.asset != "option"
                else "AKShare / 东方财富"
                if query.source == "akshare" and query.asset in {"equity", "etf"}
                else "AKShare / 新浪"
                if query.source == "akshare"
                else query.source,
                "frequency": "分钟快照"
                if query.period.endswith(("m", "h"))
                else "历史快照"
                if query.source == "alpaca"
                else "日频研究",
                "empty_reason": None
                if bars
                else "该范围没有行情。请检查代码、上市/到期日、来源权限或向前查询。",
            }
            try:
                await asyncio.to_thread(self._cache, key, payload)
            except (sqlite3.Error, OSError):
                warnings.append("本机缓存写入失败, 本次数据仅在内存显示。请检查存储空间与权限。")
            self.diagnostics[query.source] = {
                "state": "ok",
                "checked_at": payload["fetched_at"],
                "detail": f"最近读取 {len(bars)} 根 K 线",
            }
            return payload
        except (
            ResearchError,
            TimeoutError,
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            # Never expose provider response bodies, request URLs or authentication material.
            failure = (
                error
                if isinstance(error, ResearchError)
                else ResearchError("来源连接或数据格式异常, 请稍后重试。")
            )
            self.diagnostics[query.source] = {
                "state": "error",
                "checked_at": datetime.now(UTC).isoformat(),
                "detail": str(failure),
            }
            if cached:
                return {
                    **cached,
                    "cache_state": "stale",
                    "warnings": [*cached["warnings"], str(failure) + " 当前展示同源旧缓存。"],
                }
            raise failure from None

    async def _http(self, source: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self.gates[source]:
            delay = 0.35 - (time.monotonic() - self.last_request.get(source, 0))
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_request[source] = time.monotonic()
            for attempt in range(2):
                try:
                    response = await self.client.request(method, url, **kwargs)
                    if response.status_code < 500 or attempt:
                        break
                except httpx.TransportError:
                    if attempt:
                        raise ResearchError("行情连接中断, 已重试一次。请稍后重试。") from None
                await asyncio.sleep(0.5)
        if response.status_code in {401, 403}:
            raise ResearchError("来源拒绝访问: 请检查账户授权、数据权限或公开接口限制。", 403)
        if response.status_code == 429:
            raise ResearchError("来源请求额度已用尽或触发限流, 请稍后重试。", 429)
        if response.is_error:
            raise ResearchError(f"来源暂不可用(HTTP {response.status_code}), 请重试。")
        return response

    async def _eastmoney_bars(self, query: ResearchQuery) -> list[dict]:
        symbol, exchange = query.symbol.split(".")
        end = china_history_end(query)
        response = await self._http(
            "eastmoney",
            "GET",
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": f"{1 if exchange == 'SH' else 0}.{symbol}",
                "klt": {"1d": 101, "1w": 102, "1M": 103}[query.period],
                "fqt": {"raw": 0, "forward": 1, "backward": 2}[query.adjustment],
                "beg": "19900101",
                "end": end.strftime("%Y%m%d"),
                "lmt": query.limit + 1,
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56",
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
        )
        payload = response.json()
        if payload.get("rc", 0) != 0:
            raise ResearchError("东方财富返回错误状态, 请检查代码或稍后重试。")
        values = (payload.get("data") or {}).get("klines", [])
        result = []
        for value in values:
            parts = value.split(",")
            if len(parts) >= 6:
                result.append(
                    dict(
                        zip(
                            ("time", "open", "close", "high", "low", "volume"),
                            parts[:6],
                            strict=True,
                        )
                    )
                )
            else:
                result.append({})
        return result

    async def _tencent_bars(self, query: ResearchQuery) -> list[dict]:
        code, exchange = query.symbol.split(".")
        symbol = exchange.lower() + code
        period = {"1d": "day", "1w": "week", "1M": "month"}[query.period]
        adjustment = {"raw": "", "forward": "qfq", "backward": "hfq"}[query.adjustment]
        end = china_history_end(query)
        payload = (
            await self._http(
                "tencent",
                "GET",
                "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
                params={
                    "param": f"{symbol},{period},,{end:%Y-%m-%d},{query.limit + 1},{adjustment}"
                },
            )
        ).json()
        if payload.get("code") != 0:
            raise ResearchError("腾讯行情返回错误状态, 请检查代码或稍后重试。")
        data = payload.get("data", {}).get(symbol, {})
        # An adjusted request must never silently return a raw price series.
        values = data.get(adjustment + period)
        if values is None and adjustment and data.get(period):
            raise ResearchError("来源未返回所选复权口径, 请显式切换为不复权。", 422)
        return [
            dict(zip(("time", "open", "close", "high", "low", "volume"), row[:6], strict=True))
            if isinstance(row, list) and len(row) >= 6
            else {}
            for row in values or []
        ]

    async def _sina_bars(self, query: ResearchQuery) -> list[dict]:
        response = await self._http(
            "sina",
            "GET",
            "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_tracefang=/InnerFuturesNewService.getDailyKLine",
            params={"symbol": query.symbol},
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        raw = response.text
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end < start:
            raise ResearchError("新浪未返回日线数据, 请检查合约代码。")
        values = json.loads(raw[start : end + 1])
        return [
            dict(
                time=row.get("d"),
                open=row.get("o"),
                high=row.get("h"),
                low=row.get("l"),
                close=row.get("c"),
                volume=row.get("v"),
            )
            if isinstance(row, dict)
            else {}
            for row in values
        ]

    async def _tushare(self, api_name: str, params: dict, fields: str = "") -> list[dict]:
        response = await self._http(
            "tushare",
            "POST",
            "https://api.tushare.pro",
            json={
                "api_name": api_name,
                "token": self.environment.get("TUSHARE_TOKEN", ""),
                "params": params,
                "fields": fields,
            },
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise ResearchError("Tushare 请求被拒绝, 请检查 Token、接口积分权限和调用频率。", 403)
        data = payload.get("data") or {}
        return [
            dict(zip(data.get("fields", []), row, strict=True)) for row in data.get("items", [])
        ]

    async def _tushare_bars(self, query: ResearchQuery) -> list[dict]:
        api_name = {
            "equity": "daily",
            "etf": "fund_daily",
            "index": "index_daily",
            "future": "fut_daily",
            "option": "opt_daily",
        }[query.asset]
        end = (query.before - timedelta(seconds=1)) if query.before else datetime.now(UTC)
        values = await self._tushare(
            api_name,
            {
                "ts_code": query.symbol,
                "end_date": end.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"),
                "limit": query.limit + 1,
            },
            "ts_code,trade_date,open,high,low,close,vol",
        )
        return [
            {
                "time": datetime.strptime(row["trade_date"], "%Y%m%d").isoformat(),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("vol"),
            }
            for row in values
        ]

    def _alpaca_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.environment.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": self.environment.get("ALPACA_SECRET_KEY", ""),
        }

    async def _alpaca_bars(self, query: ResearchQuery) -> list[dict]:
        option = query.asset == "option"
        endpoint = "v1beta1/options/bars" if option else "v2/stocks/bars"
        period = {
            "1m": "1Min",
            "5m": "5Min",
            "15m": "15Min",
            "30m": "30Min",
            "1h": "1Hour",
            "1d": "1Day",
            "1w": "1Week",
            "1M": "1Month",
        }[query.period]
        end = query.before or datetime.now(UTC) - timedelta(minutes=16 if option else 0)
        params: dict[str, Any] = {
            "symbols": query.symbol,
            "timeframe": period,
            "start": "2016-01-01T00:00:00Z" if not option else "2024-02-01T00:00:00Z",
            "end": (end - timedelta(microseconds=1)).isoformat(),
            "sort": "desc",
            "limit": query.limit + 1,
        }
        if not option:
            params.update(feed="iex", adjustment="raw")
        result = []
        seen: set[str] = set()
        for _ in range(8):
            payload = (
                await self._http(
                    "alpaca",
                    "GET",
                    f"https://data.alpaca.markets/{endpoint}",
                    params=params,
                    headers=self._alpaca_headers(),
                )
            ).json()
            result.extend(payload.get("bars", {}).get(query.symbol, []))
            token = payload.get("next_page_token")
            if not token or len(result) > query.limit:
                break
            if token in seen:
                raise ResearchError("Alpaca 分页未推进, 请重试。")
            seen.add(token)
            params["page_token"] = token
        else:
            raise ResearchError("来源分页超过本次读取上限, 请缩小请求。")
        return [
            {
                "time": row.get("t"),
                "open": row.get("o"),
                "high": row.get("h"),
                "low": row.get("l"),
                "close": row.get("c"),
                "volume": row.get("v"),
            }
            for row in result
        ]

    async def _akshare_call(self, operation: str, params: dict) -> Any:
        process = None
        try:
            async with asyncio.timeout(35), self.gates["akshare"]:
                delay = 0.35 - (time.monotonic() - self.last_request.get("akshare", 0))
                if delay > 0:
                    await asyncio.sleep(delay)
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "tracefang.akshare_worker",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                output, _ = await process.communicate(
                    json.dumps(
                        {
                            "operation": operation,
                            "params": params,
                        }
                    ).encode()
                )
                self.last_request["akshare"] = time.monotonic()
                if process.returncode != 0:
                    raise ResearchError("AKShare 采集进程异常, 请重试。")
                packet = json.loads(output)
                if "error" in packet:
                    raise ResearchError("AKShare 上游连接或数据格式异常, 请稍后重试。")
                return packet["result"]
        except TimeoutError:
            raise ResearchError("AKShare 读取超时, 已停止本次采集; 可稍后重试。", 504) from None
        except (OSError, ValueError, KeyError):
            raise ResearchError("AKShare 运行环境或返回格式异常, 请更新应用后重试。") from None
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def _akshare_resource(self, operation: str, params: dict, ttl: int) -> dict:
        key = hashlib.sha256(
            ("ak-source-decimal-v4:" + operation + json.dumps(params, sort_keys=True)).encode()
        ).hexdigest()
        try:
            cached = await asyncio.to_thread(self._cache, key)
        except (sqlite3.Error, OSError, ValueError):
            cached = None
        if cached and time.time() - cached["cached_at"] < ttl:
            return {**cached, "cache_state": "cached"}
        if key not in self.inflight:
            if len(self.inflight) >= 24:
                raise ResearchError("数据请求较多, 请稍后重试。", 429)

            async def load() -> dict:
                try:
                    result = await self._akshare_call(operation, params)
                    payload = {
                        "result": result,
                        "cached_at": time.time(),
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "cache_state": "fresh",
                    }
                    with suppress(sqlite3.Error, OSError):
                        await asyncio.to_thread(self._cache, key, payload)
                    return payload
                except ResearchError:
                    if cached:
                        return {**cached, "cache_state": "stale"}
                    raise

            task = asyncio.create_task(load())
            self.inflight[key] = task
            task.add_done_callback(lambda done: self._finish(key, done))
        return copy.deepcopy(await asyncio.shield(self.inflight[key]))

    async def _akshare_bars(self, query: ResearchQuery) -> list[dict]:
        params = {
            **query.model_dump(mode="json"),
            "end_date": china_history_end(query).strftime("%Y%m%d"),
        }
        if re.fullmatch(r"[A-Z]{1,3}\d{3}(?:-?[CP]-?\d+(?:\.\d+)?)?", query.symbol):
            metadata = await self._akshare_resource("metadata", {}, 21600)
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            matches = [
                row
                for row in metadata["result"]["contracts"]
                if contract_key(row["symbol"] if query.asset == "option" else row["underlying"])
                == contract_key(query.symbol)
                and row["expiry"] >= today
            ]
            if metadata["cache_state"] == "stale" or not matches:
                raise ResearchError(
                    "三位月份代码无法核对年份; 请用两位年份的新浪代码, 如 SR2701 / SR2701C4700。",
                    422,
                )
            params["contract_year"] = int(matches[0]["month"][:4])
        packet = await self._akshare_call("bars", params)
        if not isinstance(packet, dict) or packet.get("precision_policy") != "source-decimal-lexeme-v1" or not isinstance(packet.get("bars"), list):
            raise ResearchError("来源没有返回精确历史价格，未使用浮点备用数据。")
        return packet["bars"]

    async def akshare_months(self, symbol: str) -> dict:
        if symbol not in {item["symbol"] for item in AK_UNDERLYINGS}:
            raise ResearchError("尚未支持该期权标的, 请从标的列表选择。", 422)
        metadata = await self._akshare_resource("metadata", {}, 21600)
        if metadata["cache_state"] == "stale":
            raise ResearchError("合约元数据更新失败, 请稍后重试, 暂不据旧目录加载期权链。")
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        contracts = [
            row
            for row in metadata["result"]["contracts"]
            if (
                row["underlying"] == symbol
                or re.fullmatch(re.escape(symbol) + r"\d{3,4}", row["underlying"])
            )
            and row["expiry"] >= today
        ]
        months = {}
        for row in contracts:
            months[row["month"]] = {
                "month": row["month"],
                "expiry": row["expiry"],
                "label": f"{row['month'][:4]}-{row['month'][4:]} · 到期 {row['expiry']}",
            }
        return {
            "symbol": symbol,
            "months": [months[key] for key in sorted(months)],
            "fetched_at": metadata["fetched_at"],
            "contracts": contracts,
            "metadata_evidence": metadata["result"].get("source_evidence"),
        }

    async def akshare_option_chain(self, symbol: str, month: str, report_date: str | None = None) -> dict:
        try:
            datetime.strptime(month, "%Y%m")
        except ValueError:
            raise ResearchError("合约月份无效。", 422) from None
        directory = await self.akshare_months(symbol)
        contracts = [row for row in directory["contracts"] if row["month"] == month]
        if not contracts:
            raise ResearchError("该标的月份没有有效合约, 请重新读取合约月份。", 404)
        spec = next(item for item in AK_UNDERLYINGS if item["symbol"] == symbol)
        params = {"symbol": symbol, "month": month, "contracts": contracts}
        if directory.get("metadata_evidence") is not None:
            params["metadata_evidence"] = directory["metadata_evidence"]
        if spec.get("quote_source") in ("czce-option-daily", "gfex-option-daily"):
            from tracefang.official_option_daily import DEFAULT_REPORT_DATE, report_date as validate_date
            try:
                day = validate_date(report_date or DEFAULT_REPORT_DATE)
            except ValueError as error:
                raise ResearchError(str(error), 422) from None
            source = await self._akshare_resource("daily_source", {"source_family": spec["quote_source"], "report_date": day}, 300)
            if source["cache_state"] == "stale" or source["result"]["source_date"] != day:
                raise ResearchError("所选日期的官方报告不可用；未使用其他日期。")
            params.update(report_date=day, daily_source_packet=source["result"])
        elif report_date is not None:
            raise ResearchError("该来源不是官方日行情，不能应用报告日期。", 422)
        page = await self._akshare_resource("chain", params, 30)
        result = {
            **page["result"],
            "cache_state": page["cache_state"],
            "fetched_at": page["result"].get("fetched_at", page["fetched_at"]),
            "read_at": page["fetched_at"],
        }
        if page["cache_state"] == "stale":
            result["warnings"] = [
                *result["warnings"],
                "上游读取失败, 当前为旧缓存; 暂不能导入报价。",
            ]
        return result

    async def contracts(self, asset: str, exchange: str, source: str = "tushare") -> list[dict]:
        if source == "akshare":
            if asset not in {"future", "option"} or exchange not in {
                "SSE",
                "SZSE",
                "SHFE",
                "DCE",
                "CZCE",
                "CFFEX",
                "INE",
                "GFEX",
            }:
                raise ResearchError("AKShare 目录支持指定交易所的期权及其期货标的。", 422)
            page = await self._akshare_resource("metadata", {}, 21600)
            if page["cache_state"] == "stale":
                raise ResearchError("合约元数据读取失败, 请稍后重试。")
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            rows = [
                row
                for row in page["result"]["contracts"]
                if row["exchange"] == exchange and row["expiry"] >= today
            ]
            directory = {}
            for row in rows:
                if asset == "future" and exchange in {"SSE", "SZSE", "CFFEX"}:
                    continue  # CFFEX index-option identifiers are not futures contracts.
                symbol = row["symbol"] if asset == "option" else row["underlying"]
                directory[symbol] = {
                    "source": "akshare",
                    "asset": asset,
                    "symbol": symbol,
                    "name": row["name"] if asset == "option" else symbol,
                    "currency": "CNY",
                    "expiry": row["expiry"] if asset == "option" else None,
                }
            return list(directory.values())[:6000]
        if source != "tushare":
            raise ResearchError("目录来源无效。", 422)
        if not self.environment.get("TUSHARE_TOKEN"):
            raise ResearchError("请配置 TUSHARE_TOKEN 后同步合约目录。", 409)
        if asset not in {"equity", "future", "option"}:
            raise ResearchError("目录类别无效。", 422)
        if exchange not in {"SSE", "SZSE", "BSE", "SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"}:
            raise ResearchError("交易所无效。", 422)
        api_name = {"equity": "stock_basic", "future": "fut_basic", "option": "opt_basic"}[asset]
        # One explicit exchange page; no unbounded whole-market loops.
        rows = await self._tushare(api_name, {"exchange": exchange, "limit": 6000})
        return [
            {
                "source": "tushare",
                "asset": asset,
                "symbol": row["ts_code"],
                "name": row.get("name") or row.get("ts_code"),
                "currency": "CNY",
                "expiry": row.get("maturity_date") or row.get("delist_date"),
            }
            for row in rows
            if row.get("ts_code")
        ]

    async def option_chain(self, symbol: str, expiry: str | None = None) -> dict:
        self.validate(ResearchQuery(source="alpaca", symbol=symbol))
        if not re.fullmatch(r"[A-Z][A-Z.]{0,8}", symbol):
            raise ResearchError("请输入美股标的代码, 例如 SPY。", 422)
        params: dict[str, Any] = {"feed": "indicative", "limit": 1000}
        if expiry:
            try:
                params["expiration_date"] = datetime.strptime(expiry, "%Y-%m-%d").date().isoformat()
            except ValueError:
                raise ResearchError("到期日格式无效。", 422) from None
        contracts = []
        token = None
        seen: set[str] = set()
        for _ in range(5):
            payload = (
                await self._http(
                    "alpaca",
                    "GET",
                    f"https://data.alpaca.markets/v1beta1/options/snapshots/{symbol}",
                    params=params,
                    headers=self._alpaca_headers(),
                )
            ).json()
            for code, snapshot in payload.get("snapshots", {}).items():
                match = re.fullmatch(r"([A-Z.]+)(\d{6})([CP])(\d{8})", code)
                if not match:
                    continue
                quote, trade = snapshot.get("latestQuote") or {}, snapshot.get("latestTrade") or {}
                contracts.append(
                    {
                        "symbol": code,
                        "underlying": symbol,
                        "expiry": datetime.strptime(match[2], "%y%m%d").date().isoformat(),
                        "kind": "call" if match[3] == "C" else "put",
                        "strike": int(match[4]) / 1000,
                        "bid": _optional_number(quote.get("bp")),
                        "ask": _optional_number(quote.get("ap")),
                        "last": _optional_number(trade.get("p")),
                        "observed_at": quote.get("t") or trade.get("t"),
                        "iv": _optional_number(snapshot.get("impliedVolatility")),
                        "greeks": {
                            key: _optional_number(value)
                            for key, value in (snapshot.get("greeks") or {}).items()
                        },
                    }
                )
            token = payload.get("next_page_token")
            if not token:
                break
            if token in seen:
                raise ResearchError("期权链分页未推进, 请重试。")
            seen.add(token)
            params["page_token"] = token
        return {
            "source": "alpaca",
            "feed": "indicative",
            "underlying": symbol,
            "fetched_at": datetime.now(UTC).isoformat(),
            "truncated": bool(token),
            "contracts": sorted(
                contracts, key=lambda row: (row["expiry"], row["strike"], row["kind"])
            ),
            "note": "指示性延迟行情; 仅返回已读取的合约, 非可成交承诺。"
            "标准美股乘数通常为100, 请核对调整合约。",
        }
