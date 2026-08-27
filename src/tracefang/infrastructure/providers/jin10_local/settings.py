from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_JIN10_LOCAL_URL = "wss://app-quote-ws.jin10.com/"
DEFAULT_JIN10_KLINE_FILE_URL = "https://jiaoyixia-market.jin10.com"
_LOGIN_LINE = re.compile(r"发送行情登录\s+userId=(\d+)\s+tokenLen=36")
_LOG_TAIL_BYTES = 512 * 1024


def _default_log_directory(values: Mapping[str, str]) -> Path:
    app_data = values.get("APPDATA", "").strip()
    if not app_data:
        raise ValueError(
            "APPDATA is unavailable; set JIN10_LOCAL_USER_ID or "
            "JIN10_LOCAL_LOG_DIRECTORY explicitly"
        )
    return Path(app_data) / "com.jin10" / "金十数据" / "log"


def _read_log_tail(path: Path) -> str:
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - _LOG_TAIL_BYTES))
        return stream.read().decode("utf-8", errors="ignore")


def resolve_local_user_id(log_directory: Path) -> int:
    try:
        logs = sorted(
            log_directory.glob("log_*.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError as error:
        raise ValueError("cannot inspect the Jin10 local log directory") from error
    for path in logs:
        try:
            matches = _LOGIN_LINE.findall(_read_log_tail(path))
        except OSError:
            continue
        if matches:
            return int(matches[-1])
    raise ValueError(
        "Jin10 local user id was not found; sign in once with the Jin10 desktop app "
        "or set JIN10_LOCAL_USER_ID"
    )


@dataclass(frozen=True, slots=True)
class Jin10LocalSettings:
    session_token: str = field(repr=False)
    user_id: int
    endpoint: str = DEFAULT_JIN10_LOCAL_URL
    kline_file_endpoint: str = DEFAULT_JIN10_KLINE_FILE_URL
    quote_frequency_ms: int = 1000
    kline_frequency_ms: int = 3000
    quote_wait_timeout_seconds: float = 10.0
    kline_wait_timeout_seconds: float = 12.0
    kline_download_timeout_seconds: float = 20.0
    stale_after_seconds: float = 12.0
    connect_timeout_seconds: float = 10.0
    heartbeat_seconds: float = 10.0
    vip_type: int = 3

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Jin10LocalSettings:
        values = os.environ if env is None else env
        token = values.get("JIN10_LOCAL_SESSION_TOKEN", "").strip()
        if len(token) != 36:
            raise ValueError("JIN10_LOCAL_SESSION_TOKEN must contain the 36-character token")

        configured_user_id = values.get("JIN10_LOCAL_USER_ID", "").strip()
        if configured_user_id:
            try:
                user_id = int(configured_user_id)
            except ValueError as error:
                raise ValueError("JIN10_LOCAL_USER_ID must be an integer") from error
        else:
            configured_logs = values.get("JIN10_LOCAL_LOG_DIRECTORY", "").strip()
            log_directory = (
                Path(configured_logs).expanduser()
                if configured_logs
                else _default_log_directory(values)
            )
            user_id = resolve_local_user_id(log_directory)

        frequency_ms = int(values.get("JIN10_LOCAL_QUOTE_FREQUENCY_MS", "1000"))
        kline_frequency_ms = int(values.get("JIN10_LOCAL_KLINE_FREQUENCY_MS", "3000"))
        wait_timeout = float(values.get("JIN10_LOCAL_QUOTE_WAIT_TIMEOUT_SECONDS", "10"))
        kline_wait_timeout = float(values.get("JIN10_LOCAL_KLINE_WAIT_TIMEOUT_SECONDS", "12"))
        kline_download_timeout = float(
            values.get("JIN10_LOCAL_KLINE_DOWNLOAD_TIMEOUT_SECONDS", "20")
        )
        stale_after = float(values.get("JIN10_LOCAL_STALE_AFTER_SECONDS", "12"))
        connect_timeout = float(values.get("JIN10_LOCAL_CONNECT_TIMEOUT_SECONDS", "10"))
        heartbeat = float(values.get("JIN10_LOCAL_HEARTBEAT_SECONDS", "10"))
        vip_type = int(values.get("JIN10_LOCAL_VIP_TYPE", "3"))
        if not 250 <= frequency_ms <= 60_000:
            raise ValueError("JIN10_LOCAL_QUOTE_FREQUENCY_MS must be between 250 and 60000")
        if not 250 <= kline_frequency_ms <= 60_000:
            raise ValueError("JIN10_LOCAL_KLINE_FREQUENCY_MS must be between 250 and 60000")
        if (
            min(
                wait_timeout,
                kline_wait_timeout,
                kline_download_timeout,
                stale_after,
                connect_timeout,
                heartbeat,
            )
            <= 0
        ):
            raise ValueError("Jin10 local timeout values must be positive")
        if vip_type not in {0, 1, 3}:
            raise ValueError("JIN10_LOCAL_VIP_TYPE must be 0, 1, or 3")
        return cls(
            session_token=token,
            user_id=user_id,
            endpoint=values.get("JIN10_LOCAL_URL", DEFAULT_JIN10_LOCAL_URL).strip(),
            kline_file_endpoint=values.get(
                "JIN10_LOCAL_KLINE_FILE_URL",
                DEFAULT_JIN10_KLINE_FILE_URL,
            )
            .strip()
            .rstrip("/"),
            quote_frequency_ms=frequency_ms,
            kline_frequency_ms=kline_frequency_ms,
            quote_wait_timeout_seconds=wait_timeout,
            kline_wait_timeout_seconds=kline_wait_timeout,
            kline_download_timeout_seconds=kline_download_timeout,
            stale_after_seconds=stale_after,
            connect_timeout_seconds=connect_timeout,
            heartbeat_seconds=heartbeat,
            vip_type=vip_type,
        )
