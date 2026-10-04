"""Configured research bar protocols before SDK numeric conversion.

Fetch clocks describe our receipt. Source labels are not publication clocks.
"""
from __future__ import annotations

import re
from typing import Any

from tracefang.sina_option_exact import exact_decimal, fetch_raw, json_exact

MAPPING = "research-bars-source-lexeme-v1"
MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def jsonp(text: str) -> Any:
    # Captured public protocols are JSON inside an inert callback. Never eval JS.
    start, end = text.find("("), text.rfind(")")
    if start < 0 or end <= start:
        raise ValueError("unexpected research bar callback")
    return json_exact(text[start + 1:end])


def load_raw_bars(query: dict, provider_symbol: str) -> dict:
    asset, period = query["asset"], query["period"]
    minute = asset == "future" and period in MINUTES
    if asset in {"equity", "etf"}:
        code, exchange = query["symbol"].split(".")
        klt = {"1d": "101", "1w": "102", "1M": "103"}[period]
        params = {"fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                  "ut": "7eea3edcaed734bea9cbfc24409ed989", "klt": klt,
                  "fqt": {"raw": "0", "forward": "1", "backward": "2"}[query["adjustment"]],
                  "secid": f"{1 if exchange == 'SH' else 0}.{code}", "beg": "19700101", "end": query["end_date"]}
        text, proof = fetch_raw("https://push2his.eastmoney.com/api/qt/stock/kline/get", params)
        packet = json_exact(text)
        if str(packet.get("rc")) != "0":
            raise ValueError("research bar source rejected request")
        data = packet.get("data")
        if data is not None and (not isinstance(data, dict) or data.get("code") != code):
            raise ValueError("research bar source identity mismatch")
        values = [] if data is None else data.get("klines")
        if not isinstance(values, list):
            raise ValueError("invalid source bar directory")
        records = []
        for line in values:
            fields = line.split(",") if isinstance(line, str) else []
            if len(fields) not in {11, 12}:
                raise ValueError("unexpected Eastmoney bar fields")
            records.append({"d": fields[0], "o": fields[1], "c": fields[2], "h": fields[3], "l": fields[4], "v": fields[5], "raw": fields})
        protocol = "eastmoney-stock-kline"
    else:
        if asset == "future":
            if minute:
                url = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=/InnerFuturesNewService.getFewMinLine"
                params = {"symbol": provider_symbol, "type": str(MINUTES[period])}
            elif period == "1d":
                url = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_V21052021_4_12=/InnerFuturesNewService.getDailyKLine"
                params = {"symbol": provider_symbol, "type": "2021_04_12"}
            else:
                raise ValueError("unsupported configured future period")
            protocol = "sina-futures-minute" if minute else "sina-futures-daily"
        elif asset == "option" and period == "1d":
            if re.fullmatch(r"\d{8}", provider_symbol):
                url = "https://stock.finance.sina.com.cn/futures/api/jsonp_v2.php//StockOptionDaylineService.getSymbolInfo"
                params = {"symbol": "CON_OP_" + provider_symbol}
                protocol = "sina-etf-option-daily"
            else:
                symbol = re.sub(r"^[A-Z]+", lambda match: match[0].lower(), provider_symbol)
                # Captured CFFEX ids retain C/P; commodity dayline ids are lower-case.
                if not re.match(r"^(io|ho|mo)\d", symbol):
                    symbol = symbol.lower()
                # All three CFFEX and commodity SDKs use this same dayline method.
                url = "https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20_m2009C30002020_7_17=/FutureOptionAllService.getOptionDayline"
                params = {"symbol": symbol}
                protocol = "sina-futures-option-daily"
        else:
            raise ValueError("unsupported configured research asset/period")
        text, proof = fetch_raw(url, params)
        values = jsonp(text)
        # A null body is an unavailable range, not proof of a retention floor.
        records = [] if values is None else values
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("unexpected source bar shape; no SDK float fallback")
    rows = []
    for source in records:
        label = source.get("d")
        if not isinstance(label, str):
            raise ValueError("source bar label missing")
        # An unverified minute span cannot supply a real start or end boundary.
        # Keep its original period label only as a display/sort key.
        canonical = label
        rows.append({"time": canonical,
                     **{key: exact_decimal(source.get(field), signed=True) for key, field in [("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v"), ("open_interest", "p")]},
                     "source_payload": {"mapping_version": MAPPING, "protocol": protocol, "body_sha256": proof["body_sha256"],
                                        "source_label": label, "source_label_precision": "timestamp_without_zone" if minute else "day",
                                        "source_label_semantics": "source_period_label" if minute else "calendar_period_label",
                                        "span_start_unknown": minute, "span_end_unknown": minute, "display_time_role": "adapter_display_key" if minute else "source_calendar_label",
                                        "clock_policy": "sina-minute-source-label-span-unknown-v2" if minute else "source-calendar-label-retained-v1",
                                        "clock_policy_verified": False, "publication_time_unknown": True,
                                        "raw_fields": source.get("raw", source)}})
    return {"bars": rows, "source_evidence": [proof], "precision_policy": "source-decimal-lexeme-v1", "mapping_version": MAPPING,
            "source_response_state": "rows_available" if rows else "empty_response; retention_floor_not_proven", "temporal_authority_eligible": not minute,
            "time_policy": {"source_publication_clock_known": False, "clock_policy_verified": False,
                            "minute_label_policy": "source label for display/order only; span start/end and session truncation unknown" if minute else None,
                            "week_month_label_semantics": "unverified upstream start/end convention; source label retained" if period in {"1w", "1M"} else None}}
