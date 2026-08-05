from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_JIN10_MCP_URL = "https://mcp.jin10.com/mcp"


@dataclass(frozen=True, slots=True)
class Jin10Settings:
    bearer_token: str
    endpoint: str = DEFAULT_JIN10_MCP_URL
    timeout_seconds: float = 20.0
    daily_tool_limit: int = 1500
    quota_reserve: int = 25

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Jin10Settings:
        values = os.environ if env is None else env
        token = values.get("JIN10_MCP_BEARER_TOKEN", "").strip()
        if not token:
            raise ValueError("JIN10_MCP_BEARER_TOKEN is required")
        return cls(
            bearer_token=token,
            endpoint=values.get("JIN10_MCP_URL", DEFAULT_JIN10_MCP_URL),
            timeout_seconds=float(values.get("JIN10_MCP_TIMEOUT_SECONDS", "20")),
            daily_tool_limit=int(values.get("JIN10_MCP_DAILY_TOOL_LIMIT", "1500")),
            quota_reserve=int(values.get("JIN10_MCP_QUOTA_RESERVE", "25")),
        )
