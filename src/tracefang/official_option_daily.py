"""Official option day reports. Source prices never pass through floating point."""
from __future__ import annotations

import base64
import gzip
import hashlib
import re
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx

from tracefang.sina_option_exact import exact_decimal, json_exact

DEFAULT_REPORT_DATE = "2026-09-30"  # Verified historical report, never described as latest.
MAX_BODY = 4 * 1024 * 1024
SOURCES = {
    "czce-option-daily": ("CZCE", {"AP", "CJ", "FG", "PF", "PL", "PR", "SA", "SF", "SM", "UR"}, "郑商所官方期权日行情"),
    "gfex-option-daily": ("GFEX", {"PS", "PD", "PT"}, "广期所官方期权日行情"),
}
LABELS = ["合约代码", "昨结算", "今开盘", "最高价", "最低价", "今收盘", "今结算", "涨跌1", "涨跌2", "成交量(手)", "持仓量", "增减量", "成交额(万元)", "DELTA", "隐含波动率", "行权量"]
GFEX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
    "Referer": "http://www.gfex.com.cn/gfex/rihq/hqsj_tjsj.shtml",
    "Accept": "application/json, text/javascript, */*; q=0.01", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "http://www.gfex.com.cn", "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


def report_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("报告日期须为 YYYY-MM-DD；不会自动猜测最近交易日。")
    return date.fromisoformat(value).isoformat()


def fetch_report(source: str, requested_date: str) -> dict:
    stamp = report_date(requested_date)
    if source not in SOURCES:
        raise ValueError("unsupported official day source")
    if source == "czce-option-daily":
        url = f"https://www.czce.com.cn/cn/DFSStaticFiles/Option/{stamp[:4]}/{stamp.replace('-', '')}/OptionDataDaily.txt"
        method, data, headers = "GET", None, {"User-Agent": "Mozilla/5.0"}
    else:
        url = "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList"
        method, data, headers = "POST", {"trade_date": stamp.replace("-", ""), "trade_type": "1"}, GFEX_HEADERS
    started = datetime.now(timezone.utc).isoformat()
    body = bytearray()
    with httpx.stream(method, url, data=data, headers=headers, timeout=httpx.Timeout(15, connect=5), follow_redirects=True) as response:
        response.raise_for_status()
        response_started = datetime.now(timezone.utc).isoformat()
        if int(response.headers.get("content-length", "0")) > MAX_BODY:
            raise ValueError("official report exceeds source bound")
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > MAX_BODY:
                raise ValueError("official report exceeds source bound")
            body.extend(chunk)
        actual_url, status = str(response.url), response.status_code
    raw = bytes(body)
    compressed = gzip.compress(raw, mtime=0)
    proof = {"url": actual_url, "requested_url": url, "method": method, "request_data": data,
             "status": status, "requested_at": started, "response_started_at": response_started,
             "received_at": datetime.now(timezone.utc).isoformat(), "body_sha256": hashlib.sha256(raw).hexdigest(),
             "encoding": "utf-8", "byte_count": len(raw), "body_codec": "gzip", "stored_body_bytes": len(compressed),
             "body_gzip_base64": base64.b64encode(compressed).decode("ascii")}
    actual_date, rows = parse_report(source, raw, stamp)
    if not rows:
        raise ValueError("所选报告日期没有可核实的期权日行情；未使用其他日期。")
    return {"source_family": source, "source_date": actual_date, "source_evidence": proof,
            "precision_policy": "source-decimal-lexeme-v1", "mapping_version": "official-option-day-exact-v1"}


def packet_body(packet: dict, source: str, stamp: str) -> tuple[bytes, dict]:
    if packet.get("source_family") != source or packet.get("source_date") != stamp:
        raise ValueError("official report cache scope mismatch")
    proof = packet["source_evidence"]
    count = proof.get("byte_count")
    if not isinstance(count, int) or not 0 < count <= MAX_BODY or proof.get("status", 200) != 200:
        raise ValueError("invalid official response evidence")
    if proof.get("body_codec") == "gzip":
        compressed = base64.b64decode(proof["body_gzip_base64"], validate=True)
        # Bounded decompression: no unbounded gzip.decompress before checking its size.
        from io import BytesIO
        with gzip.GzipFile(fileobj=BytesIO(compressed)) as reader:
            raw = reader.read(MAX_BODY + 1)
    else:
        raw = base64.b64decode(proof["body_base64"], validate=True)
    if len(raw) != count or len(raw) > MAX_BODY or hashlib.sha256(raw).hexdigest() != proof["body_sha256"]:
        raise ValueError("official source evidence integrity failure")
    return raw, proof


def number(value: str, *, quantity: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 4096 or not re.fullmatch(r"-?\d+(?:,\d{3})*(?:\.\d+)?", value):
        raise ValueError("invalid official decimal lexeme")
    exact = exact_decimal(value.replace(",", ""), signed=not quantity)
    if exact is None:
        raise ValueError("invalid official decimal")
    return exact


def parse_report(source: str, raw: bytes, requested_date: str) -> tuple[str, dict]:
    requested_date = report_date(requested_date)
    if source not in SOURCES or not 0 < len(raw) <= MAX_BODY:
        raise ValueError("invalid official report source or size")
    text, result = raw.decode("utf-8"), {}
    if source == "czce-option-daily":
        lines = text.splitlines()
        match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", lines[0]) if lines else None
        if not match or report_date(match[1]) != requested_date:
            raise ValueError("官方报告日期与所选日期不符；未使用其他日期。")
        headers = None
        for line_number, line in enumerate(lines, 1):
            fields = [field.strip() for field in line.split("|")]
            if fields[0] == "合约代码":
                if fields != LABELS:
                    raise ValueError("official day columns changed")
                headers = fields
                continue
            match = re.fullmatch(r"([A-Z]+)(\d{3,4})([CP])(\d+(?:\.\d+)?)", fields[0])
            if not match:
                continue  # Published subtotal/header rows are not contracts.
            if headers is None or len(fields) != len(LABELS):
                raise ValueError("invalid official day row")
            original = dict(zip(headers, fields))
            source_fields = {key: number(value, quantity=key in {"成交量(手)", "持仓量", "成交额(万元)", "行权量"}) for key, value in zip(headers[1:], fields[1:])}
            code = fields[0]
            quote = {key: source_fields[label] for key, label in {"close": "今收盘", "settlement": "今结算", "previous_settlement": "昨结算", "open": "今开盘", "high": "最高价", "low": "最低价", "source_change": "涨跌1", "source_settlement_change": "涨跌2", "volume": "成交量(手)", "open_interest": "持仓量", "turnover": "成交额(万元)"}.items()}
            quote.update(raw_fields=original, raw_line_number=line_number, quantity_units={"volume": "手", "turnover": "万元", "open_interest": None})
            add_row(result, code, match, quote)
    else:
        packet = json_exact(text)
        params = packet.get("param", {})
        if packet.get("code") != "0" or params.get("trade_date") != [requested_date.replace("-", "")] or params.get("trade_type") != ["1"]:
            raise ValueError("官方报告未证明所选日期；未使用其他日期。")
        rows = packet.get("data")
        if not isinstance(rows, list) or len(rows) > 30000:
            raise ValueError("invalid official day rows")
        for index, fields in enumerate(rows):
            code = fields["delivMonth"].upper().replace("-", "")
            match = re.fullmatch(r"([A-Z]+)(\d{4})([CP])(\d+(?:\.\d+)?)", code)
            if not match:
                continue
            if fields["varietyOrder"].upper() != match[1]:
                raise ValueError("official contract product mismatch")
            quote = {key: number(fields[label], quantity=key in {"volume", "open_interest", "turnover"}) for key, label in {"close": "close", "settlement": "clearPrice", "previous_settlement": "lastClear", "open": "open", "high": "high", "low": "low", "source_change": "diff", "source_settlement_change": "diff1", "volume": "volumn", "open_interest": "openInterest", "turnover": "turnover"}.items()}
            quote.update(raw_fields=fields, raw_row_index=index, quantity_units={"volume": None, "open_interest": None, "turnover": None})
            add_row(result, code, match, quote)
    return requested_date, result


def add_row(result: dict, code: str, match: re.Match, quote: dict) -> None:
    prefix, digits, side, strike = match.groups()
    if code in result:
        raise ValueError("duplicate official contract; no input-order overwrite")
    result[code] = {"prefix": prefix, "underlying": prefix + digits, "kind": {"C": "call", "P": "put"}[side], "strike": number(strike), "quote": quote}


def build_daily_chain(params: dict, spec: dict) -> dict:
    source = spec["quote_source"]
    exchange, allowed, feed = SOURCES[source]
    stamp = report_date(params.get("report_date", DEFAULT_REPORT_DATE))
    if params["symbol"] not in allowed:
        raise ValueError("product not supported by this official report")
    packet = params.get("daily_source_packet") or fetch_report(source, stamp)
    raw, proof = packet_body(packet, source, stamp)
    _, rows = parse_report(source, raw, stamp)
    contracts, conflicts = [], []
    seen = set()
    for meta in params["contracts"]:
        code = meta["symbol"]
        if code in seen or meta["exchange"] != exchange or meta["month"] != params["month"] or not re.fullmatch(re.escape(params["symbol"]) + r"\d{3,4}", meta["underlying"]):
            raise ValueError("invalid or duplicate official contract metadata")
        seen.add(code)
        row = rows.get(code)
        if row and (row["underlying"] != meta["underlying"] or row["kind"] != meta["kind"] or Decimal(row["strike"]) != Decimal(meta["strike"])):
            conflicts.append(code)
            row = None
        base = {**meta, "bid": None, "ask": None, "last": None, "observed_at": None, "observed_precision": "unknown", "volume": None, "open_interest": None, "iv": None, "greeks": {}, "quote_state": "not_returned_or_metadata_conflict"}
        if row:
            q = row["quote"]
            last = exact_decimal(q["close"], positive=True)
            base.update(last=last, daily_close=q["close"], settlement=q["settlement"], previous_settlement=q["previous_settlement"], previous_close=None,
                        daily_open=q["open"], daily_high=q["high"], daily_low=q["low"], price_semantics="official_daily_close", source_date=stamp,
                        source_family=source, observed_precision="day", source_received_at=proof["received_at"], volume=q["volume"], open_interest=q["open_interest"], turnover=q["turnover"],
                        quantity_units=q["quantity_units"], source_change=q["source_change"], source_settlement_change=q["source_settlement_change"], source_change_percent=None,
                        change_basis="official daily report; original close and settlement change labels retained separately", source_change_unit="unknown",
                        quote_state="daily_close_available" if last is not None else "daily_row_zero_close_unavailable",
                        source_quote={"mapping_version": "official-option-day-exact-v1", "body_sha256": proof["body_sha256"], "raw_fields": q["raw_fields"],
                                      "raw_contract_id": q["raw_fields"]["合约代码"] if source == "czce-option-daily" else q["raw_fields"]["delivMonth"],
                                      "source_family": source, "source_date": stamp, "observed_at": None, "observed_precision": "day", "clock_timezone": None,
                                      "received_at": proof["received_at"], "price_semantics": "official_daily_close; never settlement fallback", "quantity_units": q["quantity_units"], "price_unit": None,
                                      "numeric_policy": "source-decimal-lexeme-v1"})
        contracts.append(base)
    if not contracts:
        raise ValueError("no valid official contract metadata")
    quoted = sum("source_quote" in row for row in contracts)
    return {"source": "akshare", "source_family": source, "feed": feed, "underlying": params["symbol"], "month": params["month"], "currency": "CNY", "source_date": stamp,
            "observed_precision": "day", "price_semantics": "official_daily_close", "fetched_at": proof["received_at"], "contracts": sorted(contracts, key=lambda row: (Decimal(row["strike"]), row["kind"], row["symbol"])),
            "metadata_contract_count": str(len(contracts)), "quoted_contract_count": str(quoted), "positive_close_count": str(sum(row["last"] is not None for row in contracts)),
            "quote_status": "daily_rows_available" if quoted == len(contracts) else "partial_daily" if quoted else "metadata_only_quotes_unavailable", "truncated": False,
            "reference_spot": None, "reference_observed_at": None, "reference_date": None, "reference_precision": "unknown", "source_evidence": [proof], "metadata_evidence": params.get("metadata_evidence"),
            "precision_policy": "source-decimal-lexeme-v1", "pricing_model": "black76", "model_numeric_policy": "IV/Greeks/payoff scenarios use approximate binary floats; source prices remain exact strings",
            "warnings": ["这是所选历史报告日期的官方日收盘与日结算，不是实时报价；获取时刻不是成交时刻。", "零日收盘不补结算价；未标明的价格或数量单位保持未知。"] + (["目录属性冲突，未导入报价：" + ",".join(conflicts)] if conflicts else []),
            "note": "独立官方日行情来源，不证明与新浪或同花顺原渠道在同一时刻。"}


def build_metadata_only(params: dict, spec: dict) -> dict:
    contracts = [{**meta, "bid": None, "ask": None, "last": None, "observed_at": None, "observed_precision": "unknown", "volume": None, "open_interest": None, "iv": None, "greeks": {}, "quote_state": "metadata_only_quotes_unavailable"} for meta in params["contracts"]]
    if not contracts or any(meta.get("exchange") != "DCE" or meta.get("month") != params["month"] or not re.fullmatch(re.escape(params["symbol"]) + r"\d{4}", meta.get("underlying", "")) for meta in contracts):
        raise ValueError("invalid catalog-only DCE metadata")
    availability = spec["availability"]
    return {"source": "akshare", "source_family": "catalog-only", "feed": "OpenCTP合约目录（报价不可用）", "underlying": params["symbol"], "month": params["month"], "currency": "CNY", "truncated": False,
            "contracts": sorted(contracts, key=lambda row: (Decimal(row["strike"]), row["kind"], row["symbol"])), "metadata_contract_count": str(len(contracts)), "quoted_contract_count": "0",
            "quote_status": "metadata_only_quotes_unavailable", "source_availability": availability, "reference_spot": None, "reference_observed_at": None, "reference_date": None, "reference_precision": "unknown",
            "source_evidence": [], "metadata_evidence": params.get("metadata_evidence"), "precision_policy": "source-decimal-lexeme-v1", "pricing_model": "black76",
            "warnings": ["有效目录不代表价格可用。"], "note": "公开PP日行情本轮实际返回HTTP 412；本次未绕过来源限制。" if availability["attempted_product_request"] else "尚未取得该产品的原始报价响应；当前仅展示有效合约目录，未宣称已请求。"}
