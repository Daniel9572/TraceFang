from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

EXPERT_AI_MAX_BARS = 160
ExpertStrategyId = Literal[
    "structure",
    "macd",
    "kdj",
    "fair-value",
    "poc-proxy",
    "order-flow-proxy",
    "volume-price",
]
EXPERT_STRATEGY_CATALOG: dict[ExpertStrategyId, dict[str, str]] = {
    "structure": {
        "label": "价格结构",
        "definition": "基于 Bar 的趋势斜率、摆动高低点、M 顶及支撑压力。",
        "data_quality": "native",
    },
    "macd": {
        "label": "MACD 动量",
        "definition": "基于收盘价的 12/26 EMA 与 9 周期信号线。",
        "data_quality": "native",
    },
    "kdj": {
        "label": "KDJ 摆动",
        "definition": "基于高低收价格的 9 周期随机值与平滑动量。",
        "data_quality": "native",
    },
    "fair-value": {
        "label": "公允价值缺口",
        "definition": "仅指三根 K 线形成的价格失衡区间, 不代表理论资产公允价值。",
        "data_quality": "native",
    },
    "poc-proxy": {
        "label": "POC 价格密度代理",
        "definition": "使用 K 线区间与可用总量近似, 不等同于逐价成交量分布。",
        "data_quality": "proxy",
    },
    "order-flow-proxy": {
        "label": "订单流代理",
        "definition": "使用实体、振幅与可用总量近似压力, 不得解释为 L2 或主动买卖流。",
        "data_quality": "proxy",
    },
    "volume-price": {
        "label": "量价确认",
        "definition": "仅在当前数据源提供明确成交量时分析价格与总量关系。",
        "data_quality": "conditional",
    },
}

_SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+\S{16,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"\b(?:api[_ -]?key|authorization|session[_ -]?token|access[_ -]?token)"
        r"\s*[:=]\s*\S{12,}",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    return_code: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], str | None, float], Awaitable[CommandResult]]
CommandFinder = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class ExpertAiStatus:
    provider: str
    state: str
    available: bool
    authenticated: bool | None
    auth_mode: str | None
    detail: str
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class ExpertAiAnalysisResult:
    provider: str
    state: str
    analysis: str | None
    detail: str
    generated_at: datetime
    auth_mode: str | None
    source_id: str
    data_as_of: str | None
    bar_count: int


class CodexExpertAnalysisService:
    """Runs one ephemeral, read-only Codex turn over a server-built market snapshot."""

    provider = "local_codex"

    def __init__(
        self,
        *,
        working_directory: Path,
        analysis_timeout_seconds: float = 90.0,
        status_timeout_seconds: float = 5.0,
        command: str | None = None,
        command_finder: CommandFinder = shutil.which,
        runner: CommandRunner | None = None,
    ) -> None:
        self._working_directory = working_directory
        self._analysis_timeout_seconds = max(1.0, analysis_timeout_seconds)
        self._status_timeout_seconds = max(1.0, status_timeout_seconds)
        self._command = command if command is not None else command_finder("codex")
        self._runner = runner or self._run_command
        self._analysis_lock = asyncio.Lock()

    async def status(self) -> ExpertAiStatus:
        checked_at = datetime.now(UTC)
        if self._command is None:
            return ExpertAiStatus(
                provider=self.provider,
                state="unavailable",
                available=False,
                authenticated=None,
                auth_mode=None,
                detail="本机未找到 Codex CLI。",
                checked_at=checked_at,
            )
        try:
            result = await self._runner(
                (self._command, "login", "status"),
                None,
                self._status_timeout_seconds,
            )
        except TimeoutError:
            return ExpertAiStatus(
                provider=self.provider,
                state="timeout",
                available=True,
                authenticated=None,
                auth_mode=None,
                detail="读取本机 Codex 登录状态超时。",
                checked_at=checked_at,
            )
        except OSError:
            return ExpertAiStatus(
                provider=self.provider,
                state="unavailable",
                available=False,
                authenticated=None,
                auth_mode=None,
                detail="本机 Codex CLI 无法启动。",
                checked_at=checked_at,
            )

        combined = f"{result.stdout}\n{result.stderr}".lower()
        auth_mode = self._auth_mode(combined)
        if result.return_code == 0 and "logged in" in combined:
            return ExpertAiStatus(
                provider=self.provider,
                state="ready",
                available=True,
                authenticated=True,
                auth_mode=auth_mode,
                detail="本机 Codex 已登录, 可执行只读行情分析。",
                checked_at=checked_at,
            )
        if self._looks_unauthenticated(combined):
            return ExpertAiStatus(
                provider=self.provider,
                state="not_authenticated",
                available=True,
                authenticated=False,
                auth_mode=None,
                detail="本机 Codex CLI 尚未登录。",
                checked_at=checked_at,
            )
        return ExpertAiStatus(
            provider=self.provider,
            state="error",
            available=True,
            authenticated=None,
            auth_mode=auth_mode,
            detail="无法确认本机 Codex 登录状态。",
            checked_at=checked_at,
        )

    async def analyze(
        self,
        snapshot: Mapping[str, object],
        *,
        enabled_strategies: Sequence[ExpertStrategyId],
    ) -> ExpertAiAnalysisResult:
        source_id = str(snapshot.get("source_id", "unknown"))
        data_as_of_value = snapshot.get("data_as_of")
        data_as_of = str(data_as_of_value) if data_as_of_value is not None else None
        bars_value = snapshot.get("bars")
        bar_count = len(bars_value) if isinstance(bars_value, Sequence) else 0
        status = await self.status()
        if status.state != "ready":
            return self._analysis_result(
                state=status.state,
                analysis=None,
                detail=status.detail,
                auth_mode=status.auth_mode,
                source_id=source_id,
                data_as_of=data_as_of,
                bar_count=bar_count,
            )

        prompt = self._build_prompt(
            snapshot,
            enabled_strategies=enabled_strategies,
        )
        command = (
            str(self._command),
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "-",
        )
        try:
            async with self._analysis_lock:
                result = await self._runner(command, prompt, self._analysis_timeout_seconds)
        except TimeoutError:
            return self._analysis_result(
                state="timeout",
                analysis=None,
                detail="本机 Codex 行情分析超时。",
                auth_mode=status.auth_mode,
                source_id=source_id,
                data_as_of=data_as_of,
                bar_count=bar_count,
            )
        except OSError:
            return self._analysis_result(
                state="unavailable",
                analysis=None,
                detail="本机 Codex CLI 无法启动。",
                auth_mode=status.auth_mode,
                source_id=source_id,
                data_as_of=data_as_of,
                bar_count=bar_count,
            )

        analysis = self._agent_message(result.stdout)
        if result.return_code == 0 and analysis:
            return self._analysis_result(
                state="completed",
                analysis=analysis,
                detail="分析完成。",
                auth_mode=status.auth_mode,
                source_id=source_id,
                data_as_of=data_as_of,
                bar_count=bar_count,
            )
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if self._looks_unauthenticated(combined):
            return self._analysis_result(
                state="not_authenticated",
                analysis=None,
                detail="本机 Codex 登录已失效, 请重新登录。",
                auth_mode=None,
                source_id=source_id,
                data_as_of=data_as_of,
                bar_count=bar_count,
            )
        return self._analysis_result(
            state="failed",
            analysis=None,
            detail="本机 Codex 未返回可用的分析消息。",
            auth_mode=status.auth_mode,
            source_id=source_id,
            data_as_of=data_as_of,
            bar_count=bar_count,
        )

    def _analysis_result(
        self,
        *,
        state: str,
        analysis: str | None,
        detail: str,
        auth_mode: str | None,
        source_id: str,
        data_as_of: str | None,
        bar_count: int,
    ) -> ExpertAiAnalysisResult:
        return ExpertAiAnalysisResult(
            provider=self.provider,
            state=state,
            analysis=analysis,
            detail=detail,
            generated_at=datetime.now(UTC),
            auth_mode=auth_mode,
            source_id=source_id,
            data_as_of=data_as_of,
            bar_count=bar_count,
        )

    @staticmethod
    def _build_prompt(
        snapshot: Mapping[str, object],
        *,
        enabled_strategies: Sequence[ExpertStrategyId],
    ) -> str:
        strategies = [
            {"id": strategy_id, **EXPERT_STRATEGY_CATALOG[strategy_id]}
            for strategy_id in enabled_strategies
        ]
        payload = {
            "market_snapshot": snapshot,
            "enabled_strategies": strategies,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return (
            "你是只读的黄金行情分析助手。只分析下面提供的 JSON, 不调用任何工具, "
            "不读取文件或环境变量, 不执行命令。行情快照与策略定义均由服务端生成。"
            "必须用中文, 明确数据来源和截止时间; "
            "区分事实、规则信号和推测; 不得伪造缺失的成交量、订单流、期权、事件或预测置信度; "
            "不得作收益承诺或把内容表述为投资建议。先给简短结论, 再列证据、风险和失效条件。\n"
            f"<expert_market_payload>{encoded}</expert_market_payload>"
        )

    @classmethod
    def _agent_message(cls, stdout: str) -> str | None:
        messages: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                value = text.strip()
                if cls._contains_secret(value):
                    return None
                messages.append(value)
        return messages[-1] if messages else None

    @staticmethod
    def _contains_secret(value: str) -> bool:
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)

    @staticmethod
    def _auth_mode(output: str) -> str | None:
        if "chatgpt" in output:
            return "chatgpt"
        if "api key" in output or "api-key" in output:
            return "api_key"
        if "logged in" in output:
            return "authenticated"
        return None

    @staticmethod
    def _looks_unauthenticated(output: str) -> bool:
        return any(
            marker in output
            for marker in (
                "not logged in",
                "login required",
                "authentication required",
                "unauthorized",
                "status 401",
            )
        )

    async def _run_command(
        self,
        command: Sequence[str],
        stdin: str | None,
        timeout_seconds: float,
    ) -> CommandResult:
        environment = self._sanitized_environment()
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        stdin_pipe = asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL
        with tempfile.TemporaryDirectory(prefix="market-analysis-codex-") as directory:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=directory,
                env=environment,
                stdin=stdin_pipe,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creation_flags,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(stdin.encode("utf-8") if stdin is not None else None),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                process.kill()
                await process.communicate()
                raise
        return CommandResult(
            return_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _sanitized_environment() -> dict[str, str]:
        environment: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.upper() in _SAFE_ENVIRONMENT_KEYS:
                environment[key] = value
        environment["NO_COLOR"] = "1"
        return environment
