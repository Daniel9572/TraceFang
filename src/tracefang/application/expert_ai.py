from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

EXPERT_AI_MAX_BARS = 320
ExpertStrategyId = Literal[
    "structure",
    "ma-structure",
    "macd",
    "kdj",
    "rsi",
    "bollinger",
    "nine-count",
    "momentum-ensemble",
    "auto-trend",
    "multi-timeframe",
    "smart-money",
    "vix-gvz",
    "volume-open-interest",
    "fair-value",
    "poc-proxy",
    "order-flow-proxy",
    "volume-price",
]
EXPERT_STRATEGY_CATALOG: dict[ExpertStrategyId, dict[str, str]] = {
    "structure": {
        "label": "价格结构",
        "definition": (
            "仅在右侧 Bar 到齐后确认摆动高低点, 机械识别 W 底、M 顶与 2B 假突破, "
            "并给出颈线、支撑压力及失效条件。"
        ),
        "data_quality": "native",
    },
    "ma-structure": {
        "label": "MA 均线结构与动态支撑",
        "definition": (
            "按当前周期独立计算 MA20/60/120/250, 识别多空排列、斜率共振及"
            "经波动容差和收盘确认的动态支撑压力。"
        ),
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
    "rsi": {
        "label": "RSI 相对强弱",
        "definition": (
            "使用 Wilder 14 周期平滑计算 RSI; 仅把超买超卖区间的回穿视为恢复或回落信号, "
            "原始极值本身不等同于反转。"
        ),
        "data_quality": "native",
    },
    "bollinger": {
        "label": "布林带波动结构",
        "definition": "使用 20 周期均值及两倍滚动标准差描述相对位置、带宽压缩与扩张。",
        "data_quality": "native",
    },
    "nine-count": {
        "label": "九转计数 (Setup 9)",
        "definition": (
            "仅实现收盘价相对四根前收盘价的连续 Setup 9 与简化 perfected 条件, "
            "不声称完整 DeMARK Sequential。"
        ),
        "data_quality": "native",
    },
    "momentum-ensemble": {
        "label": "多尺度趋势动量集成",
        "definition": "对 20/60/120 周期因果回报做波动归一化和方向集成。",
        "data_quality": "native",
    },
    "auto-trend": {
        "label": "智能趋势线",
        "definition": "使用已确认摆动锚点、ATR 接触容差与穿越惩罚生成可审计趋势线。",
        "data_quality": "native",
    },
    "multi-timeframe": {
        "label": "多周期趋势差异",
        "definition": (
            "在同一信息截止时间内仅使用 1h/1d/1w 已收盘 Bar, 以 5/20 SMA 与 20 Bar "
            "回报描述周期方向及分歧; 分歧是交易上下文, 不是入场信号或预测概率。"
        ),
        "data_quality": "conditional",
    },
    "smart-money": {
        "label": "流动性与结构代理 (SMC)",
        "definition": (
            "以已确认摆动点的流动性扫掠及后续 BOS/CHOCH 共振作为价格行为代理; "
            "不能据此识别机构、订单身份或真实智能资金流。"
        ),
        "data_quality": "proxy",
    },
    "vix-gvz": {
        "label": "VIX / GVZ 风险与黄金波动",
        "definition": (
            "仅使用 Cboe 官方日频历史值描述股票与黄金隐含波动环境; "
            "波动率指数不提供金价方向。"
        ),
        "data_quality": "conditional",
    },
    "volume-open-interest": {
        "label": "期货量价持仓结构",
        "definition": (
            "使用 SHFE 延迟的单边成交量与总持仓量作为市场参与度上下文; "
            "总持仓不能辨别多空方向。"
        ),
        "data_quality": "conditional",
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
EXPERT_STRATEGY_COUNT = len(EXPERT_STRATEGY_CATALOG)

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
ExpertAiDiagnosticCode = Literal[
    "analysis_failed",
    "analysis_timeout",
    "cli_not_found",
    "cli_path_invalid",
    "cli_start_failed",
    "not_authenticated",
    "status_timeout",
    "status_unrecognized",
]


@dataclass(frozen=True, slots=True)
class _CodexCommandResolution:
    command: str | None
    diagnostic_code: ExpertAiDiagnosticCode | None = None


@dataclass(frozen=True, slots=True)
class ExpertAiStatus:
    provider: str
    state: str
    available: bool
    authenticated: bool | None
    auth_mode: str | None
    detail: str
    checked_at: datetime
    diagnostic_code: ExpertAiDiagnosticCode | None


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
    diagnostic_code: ExpertAiDiagnosticCode | None


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
        environment: Mapping[str, str] | None = None,
        fallback_commands: Sequence[Path] | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._working_directory = working_directory
        self._analysis_timeout_seconds = max(1.0, analysis_timeout_seconds)
        self._status_timeout_seconds = max(1.0, status_timeout_seconds)
        self._command_override = command
        self._command_finder = command_finder
        self._environment = environment if environment is not None else os.environ
        self._fallback_commands = tuple(
            fallback_commands
            if fallback_commands is not None
            else self._default_fallback_commands()
        )
        self._runner = runner or self._run_command
        self._analysis_lock = asyncio.Lock()

    async def status(self) -> ExpertAiStatus:
        return await self._status_for(self._resolve_command())

    async def _status_for(self, resolution: _CodexCommandResolution) -> ExpertAiStatus:
        checked_at = datetime.now(UTC)
        if resolution.command is None:
            invalid_override = resolution.diagnostic_code == "cli_path_invalid"
            return ExpertAiStatus(
                provider=self.provider,
                state="error" if invalid_override else "unavailable",
                available=False,
                authenticated=None,
                auth_mode=None,
                detail=(
                    "TRACEFANG_CODEX_CLI_PATH 指向的文件不存在或不可执行。"
                    if invalid_override
                    else "未检测到可执行的 Codex CLI。"
                ),
                checked_at=checked_at,
                diagnostic_code=resolution.diagnostic_code,
            )
        try:
            result = await self._runner(
                (resolution.command, "login", "status"),
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
                diagnostic_code="status_timeout",
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
                diagnostic_code="cli_start_failed",
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
                diagnostic_code=None,
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
                diagnostic_code="not_authenticated",
            )
        return ExpertAiStatus(
            provider=self.provider,
            state="error",
            available=True,
            authenticated=None,
            auth_mode=auth_mode,
            detail="无法确认本机 Codex 登录状态。",
            checked_at=checked_at,
            diagnostic_code="status_unrecognized",
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
        resolution = self._resolve_command()
        status = await self._status_for(resolution)
        if status.state != "ready" or resolution.command is None:
            return self._analysis_result(
                state=status.state,
                analysis=None,
                detail=status.detail,
                auth_mode=status.auth_mode,
                source_id=source_id,
                data_as_of=data_as_of,
                bar_count=bar_count,
                diagnostic_code=status.diagnostic_code,
            )

        prompt = self._build_prompt(
            snapshot,
            enabled_strategies=enabled_strategies,
        )
        command = (
            resolution.command,
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
                diagnostic_code="analysis_timeout",
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
                diagnostic_code="cli_start_failed",
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
                diagnostic_code=None,
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
                diagnostic_code="not_authenticated",
            )
        return self._analysis_result(
            state="failed",
            analysis=None,
            detail="本机 Codex 未返回可用的分析消息。",
            auth_mode=status.auth_mode,
            source_id=source_id,
            data_as_of=data_as_of,
            bar_count=bar_count,
            diagnostic_code="analysis_failed",
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
        diagnostic_code: ExpertAiDiagnosticCode | None,
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
            diagnostic_code=diagnostic_code,
        )

    def _resolve_command(self) -> _CodexCommandResolution:
        if self._command_override is not None:
            return _CodexCommandResolution(command=self._command_override)

        configured = self._environment.get("TRACEFANG_CODEX_CLI_PATH", "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_absolute() and self._is_executable(candidate):
                return _CodexCommandResolution(command=str(candidate))
            return _CodexCommandResolution(
                command=None,
                diagnostic_code="cli_path_invalid",
            )

        discovered = self._command_finder("codex")
        if discovered:
            return _CodexCommandResolution(command=discovered)

        for candidate in self._fallback_commands:
            if self._is_executable(candidate):
                return _CodexCommandResolution(command=str(candidate))
        return _CodexCommandResolution(command=None, diagnostic_code="cli_not_found")

    @staticmethod
    def _is_executable(candidate: Path) -> bool:
        return candidate.is_file() and os.access(candidate, os.X_OK)

    @staticmethod
    def _default_fallback_commands() -> tuple[Path, ...]:
        if sys.platform != "darwin":
            return ()
        relative = Path("ChatGPT.app/Contents/Resources/codex")
        return (
            Path("/Applications") / relative,
            Path.home() / "Applications" / relative,
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
        with tempfile.TemporaryDirectory(prefix="tracefang-codex-") as directory:
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
