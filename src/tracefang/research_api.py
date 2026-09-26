from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from tracefang.application.expert_ai import CodexExpertAnalysisService
from tracefang.application.research import (
    ResearchDataService,
    ResearchError,
    ResearchQuery,
    research_catalog,
)


def technical_evidence(bars: list[dict]) -> dict:
    """A deterministic evidence layer; AI interprets these facts, not invented indicators."""
    closed = [row for row in bars if row["state"] == "final"]
    closes = [row["close"] for row in closed]
    evidence: dict = {"closed_bars": len(closed), "excluded_open_bars": len(bars) - len(closed)}
    for length in (20, 60, 120):
        evidence[f"sma_{length}"] = (
            sum(closes[-length:]) / length if len(closes) >= length else None
        )
    evidence["return_20_percent"] = (
        100 * (closes[-1] / closes[-21] - 1) if len(closes) >= 21 and closes[-21] > 0 else None
    )
    evidence["range_20"] = (
        {
            "low": min(row["low"] for row in closed[-20:]),
            "high": max(row["high"] for row in closed[-20:]),
        }
        if len(closed) >= 20
        else None
    )
    if len(closed) >= 15:
        ranges = [
            max(
                row["high"] - row["low"],
                abs(row["high"] - prior["close"]),
                abs(row["low"] - prior["close"]),
            )
            for prior, row in zip(closed[-15:-1], closed[-14:], strict=True)
        ]
        evidence["true_range_14_mean"] = sum(ranges) / 14
    else:
        evidence["true_range_14_mean"] = None
    return evidence


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: ResearchQuery
    question: str = Field(
        default="分析趋势、关键价位和数据局限, 给出看多、看空和观望三种条件。", max_length=8000
    )
    model: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: str | None = Field(default=None, max_length=32)


class ResearchAnalysisJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    def start(
        self, request: AnalysisRequest, data: ResearchDataService, ai: CodexExpertAnalysisService
    ) -> dict:
        for key in list(self.jobs):
            if key not in self.tasks and time.time() - self.jobs[key]["created_at"] > 1800:
                self.jobs.pop(key)
        if self.tasks:
            raise HTTPException(409, "已有 AI 分析正在执行, 请先取消或等待完成。")
        while len(self.jobs) >= 20:
            self.jobs.pop(next(iter(self.jobs)))
        job_id = uuid4().hex
        job = {
            "id": job_id,
            "state": "loading",
            "stage": "正在读取同源行情",
            "created_at": time.time(),
            "query": request.query.model_dump(mode="json"),
            "result": None,
            "evidence": None,
            "error": None,
        }
        self.jobs[job_id] = job
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, request, data, ai))
        return dict(job)

    async def _run(
        self,
        job_id: str,
        request: AnalysisRequest,
        data: ResearchDataService,
        ai: CodexExpertAnalysisService,
    ) -> None:
        job = self.jobs[job_id]
        try:
            page = await data.bars(request.query.model_copy(update={"before": None, "limit": 320}))
            if not page["items"]:
                raise ResearchError("没有可分析的行情, 请先修复数据来源。", 409)
            if page["cache_state"] == "stale":
                raise ResearchError("来源读取失败, 当前只有过期缓存。请刷新成功后再分析。", 409)
            evidence = technical_evidence(page["items"])
            snapshot = {
                "instrument": request.query.symbol,
                "asset_class": request.query.asset,
                "source_id": request.query.source,
                "period": request.query.period,
                "data_as_of": page["data_as_of"],
                "fetched_at": page["fetched_at"],
                "adjustment": request.query.adjustment,
                "feed": page["feed"],
                "currency": page["currency"],
                "volume_unit": page["volume_unit"],
                "quality_warnings": page["warnings"],
                "computed_evidence": evidence,
                "bars": [
                    {
                        key: row[key]
                        for key in ("open_time", "open", "high", "low", "close", "volume", "state")
                    }
                    for row in page["items"]
                ],
            }
            evidence_id = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[
                :16
            ]
            job.update(
                state="analyzing",
                stage="正在分析行情证据",
                evidence=evidence,
                snapshot_id=evidence_id,
                data_as_of=page["data_as_of"],
            )
            result = await ai.analyze(
                snapshot,
                enabled_strategies=["ma-structure", "structure"],
                custom_prompt=request.question,
                model=request.model,
                reasoning_effort=request.reasoning_effort,
            )
            job["result"] = asdict(result)
            job["state"] = "completed" if result.analysis else "failed"
            job["stage"] = "分析完成" if result.analysis else "分析未完成"
            job["error"] = None if result.analysis else result.detail
        except asyncio.CancelledError:
            job.update(state="cancelled", stage="已取消")
            raise
        except (ResearchError, ValueError) as error:
            job.update(state="failed", stage="分析未完成", error=str(error))
        except Exception:
            job.update(
                state="failed", stage="分析未完成", error="分析服务异常, 请检查本机 AI 连接后重试。"
            )
        finally:
            job["finished_at"] = datetime.now(UTC).isoformat()
            self.tasks.pop(job_id, None)

    def get(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job and job_id not in self.tasks and time.time() - job["created_at"] > 1800:
            self.jobs.pop(job_id)
        if job_id not in self.jobs:
            raise HTTPException(404, "分析任务不存在或已过期。")
        return self.jobs[job_id]

    async def cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(job_id, None)
            job.update(state="cancelled", stage="已取消")
        return job


def research_router(
    data: Callable[[], ResearchDataService],
    ai: Callable[[], CodexExpertAnalysisService],
    jobs: ResearchAnalysisJobs,
) -> APIRouter:
    router = APIRouter(prefix="/api/research", tags=["research"])

    @router.get("/sources")
    async def sources() -> list[dict]:
        return data().sources()

    @router.get("/catalog")
    async def catalog(q: str = Query(default="", max_length=100)) -> list[dict]:
        return [
            row
            for row in research_catalog()
            if q.lower() in f"{row['name']} {row['symbol']}".lower()
        ]

    @router.post("/bars")
    async def bars(request: ResearchQuery, refresh: bool = False) -> dict:
        try:
            return await data().bars(request, refresh=refresh)
        except ResearchError as error:
            raise HTTPException(error.status, str(error)) from None

    @router.get("/contracts")
    async def contracts(asset: str = "future", exchange: str = "SHFE") -> list[dict]:
        try:
            return await data().contracts(asset, exchange)
        except ResearchError as error:
            raise HTTPException(error.status, str(error)) from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "合约目录读取失败, 请重试或检查来源权限。") from None

    @router.get("/options/{symbol}")
    async def options(
        symbol: str, expiry: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    ) -> dict:
        try:
            return await data().option_chain(symbol.upper(), expiry)
        except ResearchError as error:
            raise HTTPException(error.status, str(error)) from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "期权来源连接或格式异常, 请重试。") from None

    @router.post("/analysis", status_code=202)
    async def analyze(request: AnalysisRequest) -> dict:
        return jobs.start(request, data(), ai())

    @router.get("/analysis/{job_id}")
    async def analysis(job_id: str) -> dict:
        return jobs.get(job_id)

    @router.delete("/analysis/{job_id}")
    async def cancel_analysis(job_id: str) -> dict:
        return await jobs.cancel(job_id)

    return router
