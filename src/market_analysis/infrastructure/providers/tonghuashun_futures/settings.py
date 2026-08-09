from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_TIME_ENDPOINT_TEMPLATE = (
    "https://d.10jqka.com.cn/v6/time/{provider_code}/last.js"
)
DEFAULT_LINE_ENDPOINT_TEMPLATE = (
    "https://d.10jqka.com.cn/v6/line/{provider_code}/{period}/{file}"
)


@dataclass(frozen=True, slots=True)
class TonghuashunFuturesSettings:
    time_endpoint_template: str = DEFAULT_TIME_ENDPOINT_TEMPLATE
    line_endpoint_template: str = DEFAULT_LINE_ENDPOINT_TEMPLATE
    request_timeout_seconds: float = 12.0
    quote_poll_interval_seconds: float = 2.0
    stale_after_seconds: float = 30.0
    daily_stats_cache_seconds: float = 15.0
    history_cache_seconds: float = 30.0
    minute_line_period: str = "61"
    daily_line_period: str = "01"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> TonghuashunFuturesSettings:
        values = os.environ if env is None else env
        time_template = values.get(
            "TONGHUASHUN_FUTURES_TIME_URL_TEMPLATE",
            DEFAULT_TIME_ENDPOINT_TEMPLATE,
        ).strip()
        line_template = values.get(
            "TONGHUASHUN_FUTURES_LINE_URL_TEMPLATE",
            DEFAULT_LINE_ENDPOINT_TEMPLATE,
        ).strip()
        request_timeout = float(
            values.get("TONGHUASHUN_FUTURES_REQUEST_TIMEOUT_SECONDS", "12")
        )
        poll_interval = float(
            values.get("TONGHUASHUN_FUTURES_POLL_INTERVAL_SECONDS", "2")
        )
        stale_after = float(
            values.get("TONGHUASHUN_FUTURES_STALE_AFTER_SECONDS", "30")
        )
        daily_cache = float(
            values.get("TONGHUASHUN_FUTURES_DAILY_CACHE_SECONDS", "15")
        )
        history_cache = float(
            values.get("TONGHUASHUN_FUTURES_HISTORY_CACHE_SECONDS", "30")
        )
        if not time_template.startswith("https://"):
            raise ValueError("TONGHUASHUN_FUTURES_TIME_URL_TEMPLATE must use HTTPS")
        if "{provider_code}" not in time_template:
            raise ValueError(
                "TONGHUASHUN_FUTURES_TIME_URL_TEMPLATE must contain {provider_code}"
            )
        if not line_template.startswith("https://"):
            raise ValueError("TONGHUASHUN_FUTURES_LINE_URL_TEMPLATE must use HTTPS")
        if any(
            placeholder not in line_template
            for placeholder in ("{provider_code}", "{period}", "{file}")
        ):
            raise ValueError(
                "TONGHUASHUN_FUTURES_LINE_URL_TEMPLATE must contain "
                "{provider_code}, {period}, and {file}"
            )
        if min(
            request_timeout,
            poll_interval,
            stale_after,
            daily_cache,
            history_cache,
        ) <= 0:
            raise ValueError("Tonghuashun futures timeout and cache values must be positive")
        return cls(
            time_endpoint_template=time_template,
            line_endpoint_template=line_template,
            request_timeout_seconds=request_timeout,
            quote_poll_interval_seconds=poll_interval,
            stale_after_seconds=stale_after,
            daily_stats_cache_seconds=daily_cache,
            history_cache_seconds=history_cache,
        )
