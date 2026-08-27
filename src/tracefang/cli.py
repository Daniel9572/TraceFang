from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from tracefang.infrastructure.mcp import StreamableHttpMcpClient
from tracefang.infrastructure.providers.jin10 import (
    Jin10Provider,
    Jin10Settings,
    Jin10SymbolMapper,
)
from tracefang.infrastructure.quota import DailyToolBudget


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (datetime, Decimal, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _print_json(value: Any) -> None:
    print(json.dumps(value, default=_json_default, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracefang")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="验证标准 MCP 握手、工具、资源和品种映射")

    quote = subparsers.add_parser("quote", help="查询一个或多个已映射品种")
    quote.add_argument("codes", nargs="+", help="例如 XAUUSD XAGUSD")

    kline = subparsers.add_parser("kline", help="查询分钟K线")
    kline.add_argument("code", help="例如 XAUUSD")
    kline.add_argument("--count", type=int, default=20)
    kline.add_argument("--start", help="带时区的 ISO-8601 时间")

    flash = subparsers.add_parser("flash", help="搜索快讯")
    flash.add_argument("keyword")

    news = subparsers.add_parser("news", help="搜索深度文章")
    news.add_argument("keyword")

    subparsers.add_parser("calendar", help="获取本周财经日历")
    return parser


def _provider_from_settings(settings: Jin10Settings) -> Jin10Provider:
    client = StreamableHttpMcpClient(
        endpoint=settings.endpoint,
        bearer_token=settings.bearer_token,
        timeout_seconds=settings.timeout_seconds,
    )
    budget = DailyToolBudget(
        provider="jin10",
        daily_limit=settings.daily_tool_limit,
        reserve=settings.quota_reserve,
    )
    return Jin10Provider(client, budget=budget)


async def _run(args: argparse.Namespace) -> None:
    settings = Jin10Settings.from_env()
    mapper = Jin10SymbolMapper()
    provider = _provider_from_settings(settings)
    async with provider:
        if args.command == "doctor":
            instruments = await provider.list_instruments()
            mapped = [entry for entry in instruments if entry.instrument is not None]
            _print_json(
                {
                    "provider": provider.name,
                    "protocol_version": provider.client.negotiated_version,
                    "session_established": provider.client.session_id is not None,
                    "catalog_size": len(instruments),
                    "mapped_instruments": mapped,
                }
            )
            return
        if args.command == "quote":
            quotes = [
                await provider.get_quote(mapper.from_provider_code(code)) for code in args.codes
            ]
            _print_json(quotes)
            return
        if args.command == "kline":
            start = datetime.fromisoformat(args.start) if args.start else None
            candles = await provider.get_candles(
                mapper.from_provider_code(args.code),
                start=start,
                count=args.count,
            )
            _print_json(candles)
            return
        if args.command == "flash":
            _print_json(await provider.search_flash(args.keyword))
            return
        if args.command == "news":
            _print_json(await provider.search_news(args.keyword))
            return
        if args.command == "calendar":
            _print_json(await provider.list_calendar())


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (RuntimeError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0
