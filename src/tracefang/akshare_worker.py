"""Bounded AKShare jobs. Imported by the API without importing pandas or AKShare."""

from __future__ import annotations

import contextlib
import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

CHINA = ZoneInfo("Asia/Shanghai")
AK_UNDERLYINGS = [
    {"symbol": "510050.SH", "name": "上证50 ETF", "category": "etf", "sina": "50ETF"},
    {"symbol": "510300.SH", "name": "沪深300 ETF", "category": "etf", "sina": "300ETF"},
    {"symbol": "IO", "name": "沪深300股指期权", "category": "index", "sina": "hs300"},
    {"symbol": "HO", "name": "上证50股指期权", "category": "index", "sina": "sz50"},
    {"symbol": "MO", "name": "中证1000股指期权", "category": "index", "sina": "zz1000"},
    *[
        {"symbol": symbol, "name": name, "category": "future", "sina": name}
        for symbol, name in [
            ("AU", "黄金期权"),
            ("CU", "沪铜期权"),
            ("RU", "橡胶期权"),
            ("M", "豆粕期权"),
            ("C", "玉米期权"),
            ("I", "铁矿石期权"),
            ("CF", "棉花期权"),
            ("SR", "白糖期权"),
            ("TA", "PTA期权"),
            ("MA", "甲醇期权"),
            ("RM", "菜籽粕期权"),
            ("PG", "液化石油气期权"),
            ("OI", "菜籽油期权"),
            ("PK", "花生期权"),
        ]
    ],
]


def contract_key(value: str) -> str:
    return re.sub(r"[\s-]", "", value.upper()).removesuffix(".SH").removesuffix(".SZ")


def sina_contract(value: str, year: int | None = None) -> str:
    """Sina uses YYMM for CZCE contracts whose exchange identifier uses YMM."""
    code = contract_key(value)
    match = re.fullmatch(r"([A-Z]+)(\d{3})([CP].*)?", code)
    if match:
        if year is None:
            raise ValueError("three-digit contract years require valid contract metadata")
        decade = year // 10 * 10
        contract_year = decade + int(match[2][0])
        if contract_year < year:
            contract_year += 10
        code = f"{match[1]}{contract_year % 100:02}{match[2][1:]}{match[3] or ''}"
    return code


def number(value: Any, *, positive: bool = False) -> float | None:
    try:
        result = float(value)
        return (
            result
            if math.isfinite(result) and result >= 0 and (not positive or result > 0)
            else None
        )
    except (ValueError, TypeError):
        return None


def date_string(value: Any) -> str:
    text = str(value)
    return (
        datetime.strptime(text, "%Y%m%d").date().isoformat()
        if len(text) == 8
        else datetime.fromisoformat(text).date().isoformat()
    )


def quote_time(value: Any) -> str | None:
    try:
        text = str(value)
        stamp = (
            datetime.strptime(text, "%Y%m%d%H%M%S")
            if re.fullmatch(r"\d{14}", text)
            else datetime.fromisoformat(text)
        )
        return (
            stamp.replace(tzinfo=CHINA).isoformat() if stamp.tzinfo is None else stamp.isoformat()
        )
    except (ValueError, TypeError):
        return None


def records(frame: Any) -> list[dict]:
    # pandas serializes NaN/NaT as null rather than leaking invalid JSON numbers.
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def normalize_metadata(rows: list[dict]) -> dict:
    contracts = []
    rejected = 0
    today = datetime.now(CHINA).date().isoformat()
    for row in rows:
        try:
            exchange = str(row["交易所ID"])
            provider_symbol = str(row["合约ID"]).strip()
            underlying = str(row["标的合约ID"]).strip().upper()
            expiry = date_string(row["最后交易日"])
            if expiry < today:
                continue
            kind = {"1": "call", "2": "put"}[str(row["期权类型"])]
            strike = number(row["行权价"], positive=True)
            multiplier = number(row["合约乘数"], positive=True)
            if (
                not underlying
                or not strike
                or not multiplier
                or not re.fullmatch(r"[A-Za-z0-9-]+", provider_symbol)
            ):
                raise ValueError("invalid contract metadata")
            suffix = {"SSE": ".SH", "SZSE": ".SZ"}.get(exchange, "")
            month = f"{int(row['交割年份']):04}{int(row['交割月份']):02}"
            if exchange not in {"SSE", "SZSE"}:
                # CTP delivery fields may describe the option expiry month (e.g. gold),
                # so use the actual underlying futures/index contract's delivery code.
                expiry_year = int(expiry[:4])
                digits = re.fullmatch(r"[A-Z]+(\d{3,4})", underlying)[1]
                if len(digits) == 3:
                    contract_year = expiry_year // 10 * 10 + int(digits[0])
                    if contract_year < expiry_year:
                        contract_year += 10
                else:
                    contract_year = expiry_year // 100 * 100 + int(digits[:2])
                month = f"{contract_year:04}{digits[-2:]}"
            datetime.strptime(month, "%Y%m")
            contracts.append(
                {
                    "symbol": contract_key(provider_symbol) + suffix,
                    "provider_symbol": provider_symbol,
                    "name": str(row.get("合约名称") or provider_symbol).strip(),
                    "exchange": exchange,
                    "underlying": underlying + suffix,
                    "expiry": expiry,
                    "month": month,
                    "kind": kind,
                    "strike": strike,
                    "multiplier": multiplier,
                    "currency": "CNY",
                }
            )
        except (KeyError, ValueError, TypeError, OverflowError):
            rejected += 1
    if len(contracts) > 30000:
        raise ValueError("contract directory exceeds limit")
    return {"contracts": contracts, "rejected_rows": rejected, "metadata_source": "openctp"}


def load_bars(ak: Any, query: dict) -> list[dict]:
    symbol, asset, period = query["symbol"], query["asset"], query["period"]
    year = query.get("contract_year")
    if asset == "future":
        frame = (
            ak.futures_zh_daily_sina(symbol=sina_contract(symbol, year))
            if period == "1d"
            else ak.futures_zh_minute_sina(
                symbol=sina_contract(symbol, year), period={"1h": "60"}.get(period, period[:-1])
            )
        )
    elif asset == "option":
        code = sina_contract(symbol, year)
        if re.fullmatch(r"\d{8}", code):
            frame = ak.option_sse_daily_sina(symbol=code)
        else:
            code = re.sub(r"^[A-Z]+", lambda match: match[0].lower(), code)
            prefix = re.match(r"[a-z]+", code)[0]
            cffex = {"io": "hs300", "ho": "sz50", "mo": "zz1000"}
            frame = (
                getattr(ak, f"option_cffex_{cffex[prefix]}_daily_sina")(symbol=code)
                if prefix in cffex
                else ak.option_commodity_hist_sina(symbol=code)
            )
    else:
        args = {
            "symbol": symbol.split(".")[0],
            "period": {"1d": "daily", "1w": "weekly", "1M": "monthly"}[period],
            "adjust": {"raw": "", "forward": "qfq", "backward": "hfq"}[query["adjustment"]],
            "end_date": query["end_date"],
        }
        frame = (
            ak.fund_etf_hist_em(**args)
            if asset == "etf"
            else ak.stock_zh_a_hist(**args, timeout=12)
        )
    result = []
    minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
    for row in records(frame):
        stamp = row.get("datetime", row.get("date", row.get("日期")))
        if asset == "future" and period in minutes:
            # Sina minute labels are the interval end, while our API uses open_time.
            stamp = (
                datetime.fromisoformat(str(stamp)) - timedelta(minutes=minutes[period])
            ).isoformat()
        result.append(
            {
                "time": str(stamp),
                "open": row.get("open", row.get("开盘")),
                "high": row.get("high", row.get("最高")),
                "low": row.get("low", row.get("最低")),
                "close": row.get("close", row.get("收盘")),
                "volume": row.get("volume", row.get("成交量")),
                "open_interest": row.get("hold"),
            }
        )
    return result


def build_chain(ak: Any, params: dict) -> dict:
    spec = next(item for item in AK_UNDERLYINGS if item["symbol"] == params["symbol"])
    metadata = {
        sina_contract(item["symbol"], int(item["month"][:4])): item for item in params["contracts"]
    }
    warnings = []
    quotes: dict[str, dict] = {}
    reference_spot = None
    reference_at = None
    truncated = False
    if spec["category"] == "etf":
        codes = []
        for kind in ("看涨期权", "看跌期权"):
            codes.extend(
                str(row["期权代码"])
                for row in records(
                    ak.option_sse_codes_sina(
                        symbol=kind, trade_date=params["month"], underlying=spec["symbol"][:6]
                    )
                )
            )
        codes = list(dict.fromkeys(codes))
        truncated = len(codes) > 160

        def quote(code: str) -> tuple[str, dict | None]:
            try:
                rows = records(ak.option_sse_spot_price_sina(symbol=code))
                return code, {row["字段"]: row["值"] for row in rows}
            except Exception:
                return code, None

        with ThreadPoolExecutor(max_workers=4) as pool:
            for code, fields in pool.map(quote, codes[:160]):
                if fields is None:
                    warnings.append(f"合约 {code} 报价读取失败。")
                    continue
                quotes[code] = {
                    "bid": number(fields.get("买价"), positive=True),
                    "ask": number(fields.get("卖价"), positive=True),
                    "last": number(fields.get("最新价"), positive=True),
                    "observed_at": quote_time(fields.get("行情时间")),
                    "quote_underlying": fields.get("标的股票"),
                    "quote_strike": number(fields.get("行权价"), positive=True),
                }
        try:
            rows = records(
                ak.option_sse_underlying_spot_price_sina(symbol="sh" + spec["symbol"][:6])
            )
            fields = {row["字段"]: row["值"] for row in rows}
            reference_spot = number(fields.get("最近成交价"), positive=True)
            reference_at = quote_time(f"{fields.get('行情日期')} {fields.get('行情时间')}")
        except Exception:
            warnings.append("未读取到标的价格, 请手工填写情景价格。")
    else:
        underlying = sina_contract(
            params["contracts"][0]["underlying"], int(params["month"][:4])
        ).lower()
        frame = (
            getattr(ak, f"option_cffex_{spec['sina']}_spot_sina")(symbol=underlying)
            if spec["category"] == "index"
            else ak.option_commodity_contract_table_sina(symbol=spec["sina"], contract=underlying)
        )
        for row in records(frame):
            for label in ("看涨", "看跌"):
                code = row.get(f"{label}合约-{label}期权合约", row.get(f"{label}合约-标识"))
                if not code:
                    continue
                quotes[contract_key(str(code))] = {
                    "bid": number(row.get(f"{label}合约-买价"), positive=True),
                    "ask": number(row.get(f"{label}合约-卖价"), positive=True),
                    "last": number(row.get(f"{label}合约-最新价"), positive=True),
                    "observed_at": None,
                    "quote_strike": number(row.get("行权价"), positive=True),
                }
        warnings.append("来源的期权 T 型报价未提供报价时间; 读取时间不等同于成交时间。")
        if spec["category"] == "future":
            try:
                history = records(ak.futures_zh_daily_sina(symbol=underlying.upper()))
                if history:
                    reference_spot = number(history[-1].get("close"), positive=True)
                    reference_at = quote_time(history[-1].get("date"))
            except Exception:
                warnings.append("未读取到标的价格, 请手工填写情景价格。")
    contracts = []
    unmatched = 0
    for code, fields in quotes.items():
        meta = metadata.get(code)
        if (
            meta is None
            or fields["quote_strike"] != meta["strike"]
            or (
                fields.get("quote_underlying")
                and contract_key(str(fields["quote_underlying"]))
                != contract_key(meta["underlying"])
            )
        ):
            unmatched += 1
            continue
        bid, ask = fields["bid"], fields["ask"]
        if bid is not None and ask is not None and bid > ask:
            fields.update(bid=None, ask=None)
            warnings.append(f"合约 {meta['symbol']} 买卖价倒挂, 已排除盘口价格。")
        contracts.append(
            {
                **meta,
                **{key: fields[key] for key in ("bid", "ask", "last", "observed_at")},
                "iv": None,
                "greeks": {},
            }
        )
    if unmatched:
        warnings.append(f"{unmatched} 条报价缺少匹配的有效合约元数据, 已排除。")
    if not contracts:
        raise ValueError("no matched option quotes")
    return {
        "source": "akshare",
        "feed": "AKShare / 新浪期权",
        "underlying": params["symbol"],
        "month": params["month"],
        "currency": "CNY",
        "truncated": truncated,
        "pricing_model": "black76" if spec["category"] == "future" else "black-scholes",
        "reference_spot": reference_spot,
        "reference_observed_at": reference_at,
        "contracts": sorted(contracts, key=lambda row: (row["strike"], row["kind"])),
        "warnings": warnings,
        "note": "新浪公开期权快照; 到期日、乘数及标的由 OpenCTP 合约目录核对。",
    }


def execute(operation: str, params: dict) -> Any:
    import akshare as ak

    if operation == "bars":
        return load_bars(ak, params)
    if operation == "metadata":
        return normalize_metadata(records(ak.option_contract_info_ctp()))
    if operation == "chain":
        return build_chain(ak, params)
    raise ValueError("unsupported AKShare operation")


def main() -> None:
    request = json.load(sys.stdin)
    try:
        # AKShare may print progress; only the protocol JSON belongs on stdout.
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(request["operation"], request["params"])
        # Keep the subprocess wire protocol ASCII even on Windows legacy code pages.
        json.dump({"result": result}, sys.stdout, allow_nan=False)
    except Exception:
        json.dump(
            {"error": "AKShare 上游连接或数据格式异常, 请稍后重试。"},
            sys.stdout,
            ensure_ascii=True,
        )


if __name__ == "__main__":
    main()
