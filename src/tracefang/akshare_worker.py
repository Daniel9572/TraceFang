"""Bounded AKShare jobs. Imported by the API without importing pandas or AKShare."""

from __future__ import annotations

import contextlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from decimal import Decimal

from tracefang.sina_option_exact import (exact_decimal, fetch_raw, json_exact, assignment_fields, ProductLinks, table_quotes, etf_quote)
from zoneinfo import ZoneInfo


CHINA = ZoneInfo("Asia/Shanghai")
AK_UNDERLYINGS = [
    {"symbol": "510050.SH", "name": "上证50 ETF", "category": "etf", "sina": "50ETF"},
    {"symbol": "510300.SH", "name": "沪深300 ETF", "category": "etf", "sina": "300ETF"},
    {"symbol": "159901.SZ", "name": "深100ETF", "category": "etf", "sina": "深100ETF"},
    {"symbol": "159915.SZ", "name": "创业板ETF", "category": "etf", "sina": "创业板"},
    {"symbol": "159919.SZ", "name": "深300ETF", "category": "etf", "sina": "沪深300"},
    {"symbol": "159922.SZ", "name": "深中证500ETF", "category": "etf", "sina": "500ETF"},
    {"symbol": "510500.SH", "name": "沪中证500ETF", "category": "etf", "sina": "中证500ETF南方"},
    {"symbol": "588000.SH", "name": "科创50ETF", "category": "etf", "sina": "科创50ETF华夏"},
    {"symbol": "588080.SH", "name": "科创50ETF易方达", "category": "etf", "sina": "科创50ETF易方达"},
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
    {"symbol": "A", "name": "豆一期权", "category": "future", "sina": "黄大豆1号期权"},
    {"symbol": "B", "name": "豆二期权", "category": "future", "sina": "黄大豆2号期权"},
    {"symbol": "EB", "name": "苯乙烯期权", "category": "future", "sina": "苯乙烯期权"},
    {"symbol": "EG", "name": "乙二醇期权", "category": "future", "sina": "乙二醇期权"},
    {"symbol": "LC", "name": "碳酸锂期权", "category": "future", "sina": "碳酸锂期权"},
    {"symbol": "PX", "name": "对二甲苯期权", "category": "future", "sina": "二甲苯期权"},
    {"symbol": "SH", "name": "烧碱期权", "category": "future", "sina": "烧碱期权"},
    {"symbol": "SI", "name": "工业硅期权", "category": "future", "sina": "工业硅期权"},
    {"symbol": "Y", "name": "豆油期权", "category": "future", "sina": "豆油期权"},
    {"symbol": "ZC", "name": "动力煤期权", "category": "future", "sina": "动力煤期权"},
    {'symbol': 'AP', 'name': '苹果期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'CJ', 'name': '红枣期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'FG', 'name': '玻璃期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'PF', 'name': '短纤期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'PL', 'name': '丙烯期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'PR', 'name': '瓶片期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'SA', 'name': '纯碱期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'SF', 'name': '硅铁期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'SM', 'name': '锰硅期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'UR', 'name': '尿素期权', 'category': 'future', 'quote_source': 'czce-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'PS', 'name': '多晶硅期权', 'category': 'future', 'quote_source': 'gfex-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'PD', 'name': '钯期权', 'category': 'future', 'quote_source': 'gfex-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'PT', 'name': '铂期权', 'category': 'future', 'quote_source': 'gfex-option-daily', 'quote_price_semantics': 'official_daily_close', 'daily_date': '2026-09-30'},
    {'symbol': 'PP', 'name': '聚丙烯期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'public_known_daily_pp_route_http412', 'attempted_product_request': True, 'retained_probe': {'url': 'http://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes', 'method': 'POST', 'status': 412, 'requested_at': '2026-10-04T04:02:32.552189+00:00', 'received_at': '2026-10-04T04:02:33.000682+00:00', 'body_sha256': '9857383e43ccbebec518866a2f5ec9ff2f60ef027a48c17eb9b63855fe2f5c39'}}},
    {'symbol': 'V', 'name': 'PVC期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
    {'symbol': 'L', 'name': '塑料期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
    {'symbol': 'P', 'name': '棕榈油期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
    {'symbol': 'JD', 'name': '鸡蛋期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
    {'symbol': 'CS', 'name': '玉米淀粉期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
    {'symbol': 'LH', 'name': '生猪期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
    {'symbol': 'LG', 'name': '原木期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
    {'symbol': 'BZ', 'name': '纯苯期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
    {'symbol': 'JM', 'name': '焦煤期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
    {'symbol': 'J', 'name': '焦炭期权', 'category': 'future', 'quote_source': 'catalog-only', 'availability': {'availability': 'no_original_quote_body_retained_for_this_product', 'attempted_product_request': False}},
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
        if not re.fullmatch(r"\d{14}", text) and not re.search(r"[T ]\d{2}:\d{2}:\d{2}", text):
            return None
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


def etf_underlying_symbol(symbol: str) -> str:
    match = re.fullmatch(r"(\d{6})\.(SH|SZ)", symbol)
    if match is None:
        raise ValueError("ETF market suffix must be SH or SZ")
    return match[2].lower() + match[1]


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
            strike = exact_decimal(row["行权价"], positive=True)
            multiplier = exact_decimal(row["合约乘数"], positive=True)
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


def load_metadata() -> dict:
    # AKShare's equivalent request has no timeout and can consume the whole
    # worker deadline. Read the same directory without importing pandas/AKShare.
    raw, source_evidence = fetch_raw("http://dict.openctp.cn/instruments?types=option",max_bytes=32*1024*1024)
    payload = json_exact(raw)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid OpenCTP contract directory")
    fields = {
        "ExchangeID": "交易所ID",
        "InstrumentID": "合约ID",
        "InstrumentName": "合约名称",
        "VolumeMultiple": "合约乘数",
        "DeliveryYear": "交割年份",
        "DeliveryMonth": "交割月份",
        "ExpireDate": "最后交易日",
        "UnderlyingInstrID": "标的合约ID",
        "OptionsType": "期权类型",
        "StrikePrice": "行权价",
    }
    result = normalize_metadata(
        [{dest: row.get(source) for source, dest in fields.items()} for row in rows]
    )
    if not result["contracts"]:
        raise ValueError("OpenCTP directory has no valid unexpired contracts")
    result["source_evidence"] = source_evidence
    result["metadata_snapshot_id"] = source_evidence["body_sha256"]
    result["precision_policy"] = "source-decimal-lexeme-v1"
    return result


def load_bars(ak: Any, query: dict) -> dict:
    # Retain the callable argument for callers; never consume SDK DataFrames.
    del ak
    from tracefang.research_bars_exact import load_raw_bars
    return load_raw_bars(query, sina_contract(query["symbol"], query.get("contract_year")))

def build_chain(ak: Any, params: dict) -> dict:
    # `ak` remains an API-compatible argument; source prices never pass through its DataFrames.
    del ak
    spec = next(item for item in AK_UNDERLYINGS if item["symbol"] == params["symbol"])
    if spec.get("quote_source") in ("czce-option-daily", "gfex-option-daily"):
        from tracefang.official_option_daily import build_daily_chain
        return build_daily_chain(params, spec)
    if spec.get("quote_source") == "catalog-only":
        from tracefang.official_option_daily import build_metadata_only
        return build_metadata_only(params, spec)
    metadata = {sina_contract(item["symbol"], int(item["month"][:4])): item for item in params["contracts"]}
    warnings: list[str] = []
    source_evidence: list[dict] = []
    def preserve(proof: dict) -> None:
        if sum(entry["byte_count"] for entry in source_evidence) + proof["byte_count"] > 8 * 1024 * 1024:
            raise ValueError("option source evidence exceeds bound; no partial completion")
        source_evidence.append(proof)
    quotes: dict[str, dict] = {}
    reference_spot = None
    reference_at = None
    reference_date = None
    reference_label = None
    reference_received_at = None
    if spec["category"] == "etf":
        underlying_symbol = etf_underlying_symbol(spec["symbol"])
        codes: list[str] = []
        for side in ("UP", "DOWN"):
            name = f"OP_{side}_{spec['symbol'][:6]}{params['month'][-4:]}"
            text, proof = fetch_raw("https://hq.sinajs.cn/list=" + name)
            preserve(proof)
            codes.extend(field.removeprefix("CON_OP_") for field in assignment_fields(text,name) if field.startswith("CON_OP_"))
        codes = list(dict.fromkeys(codes))
        if len(codes) > 30000 or any(not re.fullmatch(r"\d{8}", code) for code in codes):
            raise ValueError("invalid or excessive ETF contract list")
        def quote(code: str) -> tuple[str, dict | None, dict | None]:
            proof = None
            try:
                text, proof = fetch_raw("https://hq.sinajs.cn/list=CON_OP_" + code)
                return code, etf_quote(text,code), proof
            except Exception:
                return code, None, proof
        # All source codes, bounded concurrency. No160-code truncation.
        with ThreadPoolExecutor(max_workers=2) as pool:
            for code, fields, proof in pool.map(quote,codes):
                if proof is not None: preserve(proof)
                if fields is None:
                    warnings.append(f"合约 {code} 报价读取失败；保留目录并标未知。")
                    continue
                fields["source_body_sha256"] = proof["body_sha256"]
                fields["source_received_at"] = proof["received_at"]
                quotes[code] = fields
        try:
            text, proof = fetch_raw("https://hq.sinajs.cn/list=" + underlying_symbol)
            preserve(proof)
            fields = assignment_fields(text,underlying_symbol)
            if len(fields) < 32: raise ValueError("invalid ETF underlying quote")
            reference_spot = exact_decimal(fields[3],positive=True)
            reference_label = f"{fields[30]} {fields[31]}"
            reference_received_at = proof["received_at"]
            # This independent ETF-underlying protocol likewise has no verified timezone/clock role.
            reference_at = None
        except Exception:
            warnings.append("未读取到标的价格；情景价格需手工填写。")
    else:
        underlying = sina_contract(params["contracts"][0]["underlying"],int(params["month"][:4])).lower()
        if spec["category"] == "index":
            product, exchange = spec["symbol"].lower(), "cffex"
        else:
            text, proof = fetch_raw("https://stock.finance.sina.com.cn/futures/view/optionsDP.php/pg_o/dce")
            preserve(proof)
            parser = ProductLinks(); parser.feed(text)
            product, exchange = parser.products[spec["sina"]]
        text, proof = fetch_raw("https://stock.finance.sina.com.cn/futures/api/openapi.php/OptionService.getOptionData", {"type":"futures","product":product,"exchange":exchange,"pinzhong":underlying})
        preserve(proof)
        for fields in table_quotes(text):
            fields["source_body_sha256"] = proof["body_sha256"]
            quotes[contract_key(fields["code"])] = fields
        warnings.append("来源T型报价没有行情时刻；接收时间仅为本次获取证据，不当成交时间。")
        if spec["category"] == "future":
            try:
                text, proof = fetch_raw("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_V21052021_4_12=/InnerFuturesNewService.getDailyKLine", {"symbol":underlying.upper(),"type":"2021_04_12"})
                preserve(proof)
                match = re.search(r"=\((.*)\);?\s*$",text,re.S)
                history = json_exact(match[1]) if match else []
                if history:
                    last = history[-1]
                    # The public daily protocol uses d/c; date/close is the SDK shape.
                    day = last.get("d", last.get("date"))
                    close = exact_decimal(last.get("c", last.get("close")), positive=True)
                    if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and close is not None:
                        reference_date = date_string(day)
                        reference_spot = close
            except Exception:
                warnings.append("未读取到日线参考价；情景价格需手工填写。")
            if reference_date:
                warnings.append("标的参考价来自日线收盘，日期仅日精度，不是实时标的报价时刻。")
    contracts: list[dict] = []
    unmatched = 0
    for code, fields in quotes.items():
        meta = metadata.get(code)
        strike = exact_decimal(fields.get("strike_raw"),positive=True)
        if meta is None or strike is None or Decimal(strike) != Decimal(meta["strike"]) or (fields.get("kind") and fields["kind"] != meta["kind"]) or (fields.get("quote_underlying") and contract_key(str(fields["quote_underlying"])) != contract_key(meta["underlying"])):
            unmatched += 1
            continue
        prices = {key:exact_decimal(fields.get(key+"_raw"),positive=True) for key in ("bid","ask","last")}
        if prices["bid"] is not None and prices["ask"] is not None and Decimal(prices["bid"]) > Decimal(prices["ask"]):
            prices.update(bid=None,ask=None)
            warnings.append(f"合约 {meta['symbol']} 买卖价倒挂；原值保留，盘口不可用于情景。")
        observed_raw = fields.get("observed_at_raw")
        observed_at = None if spec["category"] == "etf" else quote_time(observed_raw)
        if spec["category"] == "etf" and isinstance(observed_raw,str) and observed_raw.endswith("00:00:00"):
            warnings.append(f"合约 {meta['symbol']} 来源仅返回午夜时间，无法核实实际报价时刻；原始标记保留。")
        contracts.append({**meta,**prices,"observed_at":observed_at,"observed_precision":"second" if observed_at else "day" if isinstance(observed_raw,str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}",observed_raw) else "unknown",
            "volume":exact_decimal(fields.get("volume_raw")),"open_interest":exact_decimal(fields.get("open_interest_raw")),
            "source_change":exact_decimal(fields.get("source_change_raw"),signed=True),"source_change_percent":exact_decimal(fields.get("source_change_percent_raw"),signed=True),
            "source_change_unit":"unknown","source_change_semantics":"unverified SDK label: 涨跌; raw value is not asserted to be an amount or percentage",
            "change_basis":"not_verified; preserve source change only","previous_close":exact_decimal(fields.get("previous_close_raw"),positive=True),
            "source_quote":{"mapping_version":"sina-option-sdk-core-fields-v1","body_sha256":fields["source_body_sha256"],"raw_fields":fields["source_fields"],"observed_label":observed_raw,"price_policy":"nonpositive source prices are unavailable for a scenario; original lexemes retained","numeric_policy":"source-decimal-lexeme-v1",**({"clock_qualification":"unverified_timezone_and_role","clock_timezone":None,"received_at":fields["source_received_at"]} if spec["category"]=="etf" else {})},"iv":None,"greeks":{},**({"source_received_at":fields["source_received_at"],"source_clock_label":observed_raw,"source_clock_qualification":"unverified_timezone_and_role"} if spec["category"]=="etf" else {})})
    quoted_ids = {row["symbol"] for row in contracts}
    for meta in metadata.values():
        if meta["symbol"] not in quoted_ids:
            contracts.append({**meta,"bid":None,"ask":None,"last":None,"observed_at":None,"observed_precision":"unknown","volume":None,"open_interest":None,"iv":None,"greeks":{},"quote_state":"not_returned_or_metadata_conflict"})
    if unmatched: warnings.append(f"{unmatched} 源报价与有效目录身份/行权价不符；原响应保留，不导入报价。")
    if not quotes: warnings.append("原通道该有效月份没有报价；有效合约目录不代表价格可用。")
    if spec["category"]=="etf": warnings.append("来源行情时间的时区与时钟角色尚未核实；仅保留原始标记与获取时刻，不能据此认定精确成交时间。")
    if not contracts: raise ValueError("no valid option metadata")
    return {"source":"akshare","feed":"AKShare / 新浪期权（原词法精确）","underlying":params["symbol"],"month":params["month"],"currency":"CNY","truncated":False,
        "pricing_model":"black76" if spec["category"]=="future" else "black-scholes","model_numeric_policy":"IV/Greeks/payoff scenarios use approximate binary floats; source prices remain exact strings",
        "reference_spot":reference_spot,"reference_observed_at":reference_at,"reference_date":reference_date,"reference_precision":"day" if reference_date else "second" if reference_at else "unknown",**({"reference_source_label":reference_label,"reference_received_at":reference_received_at,"reference_clock_qualification":"unverified_timezone_and_role"} if spec["category"]=="etf" else {}),
        "contracts":sorted(contracts,key=lambda row:(Decimal(row["strike"]),row["kind"],row["symbol"])),"metadata_contract_count":str(len(metadata)),"quoted_contract_count":str(sum(row.get("source_quote") is not None for row in contracts)),"quote_status":"metadata_only_quotes_unavailable" if not quotes else "partial" if len(quoted_ids)<len(metadata) else "source_rows_available",
        "source_evidence":source_evidence,"metadata_evidence":params.get("metadata_evidence"),"precision_policy":"source-decimal-lexeme-v1","warnings":warnings,"note":"既有新浪公开期权响应原词法保存；到期/乘数/标的由OpenCTP目录核对，未知源时间不补。"}


def execute(operation: str, params: dict) -> Any:
    if operation == "metadata":
        return load_metadata()

    if operation == "daily_source":
        from tracefang.official_option_daily import fetch_report
        return fetch_report(params["source_family"], params["report_date"])

    if operation == "chain":
        return build_chain(None, params)

    if operation == "bars":
        return load_bars(None, params)

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
