"""Existing Sina option protocols, before SDK/DataFrame numeric conversion."""
from __future__ import annotations

import base64
import hashlib
import gzip
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

import httpx

MAX_BODY = 8 * 1024 * 1024
MAX_DECIMAL = 4096
HEADERS = {"Referer": "https://stock.finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}


def exact_decimal(value: Any, *, positive: bool = False, signed: bool = False) -> str | None:
    # Floats are never an exact source. An integer is exact (fixtures/metadata only).
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_DECIMAL:
        return None
    try:
        number = Decimal(text)
        parts = number.as_tuple()
        if not number.is_finite() or abs(parts.exponent) > MAX_DECIMAL or len(parts.digits) > MAX_DECIMAL:
            return None
        if (not signed and number < 0) or (positive and number <= 0):
            return None
        return format(number, "f")
    except (InvalidOperation, ValueError):
        return None


def json_exact(text: str) -> Any:
    def invalid(value: str) -> None:
        raise ValueError("non-finite JSON number: " + value)
    return json.loads(text, parse_float=str, parse_int=str, parse_constant=invalid)


def fetch_raw(url: str, params: dict | None = None, *, max_bytes: int | None = None) -> tuple[str, dict]:
    limit = MAX_BODY if max_bytes is None else max_bytes
    requested_at = datetime.now(timezone.utc).isoformat()
    body = bytearray()
    with httpx.stream("GET", url, params=params, headers=HEADERS,
                      timeout=httpx.Timeout(15, connect=5), follow_redirects=True) as response:
        response.raise_for_status()
        response_started_at = datetime.now(timezone.utc).isoformat()
        if int(response.headers.get("content-length", "0")) > limit:
            raise ValueError("source response exceeds bound")
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > limit:
                raise ValueError("source response exceeds bound")
            body.extend(chunk)
        actual_url = str(response.url)
    raw = bytes(body)
    try:
        text = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
        encoding = "gb18030"
    evidence = {"url": actual_url, "requested_at": requested_at, "response_started_at": response_started_at, "received_at": datetime.now(timezone.utc).isoformat(),
                "body_sha256": hashlib.sha256(raw).hexdigest(), "encoding": encoding, "byte_count": len(raw)}
    # Large contract directories remain byte-exact without overflowing the bounded worker wire.
    if len(raw) > 1024 * 1024:
        compressed = gzip.compress(raw,mtime=0)
        evidence.update(body_codec="gzip", stored_body_bytes=len(compressed), body_gzip_base64=base64.b64encode(compressed).decode("ascii"))
    else:
        evidence.update(body_codec="identity", stored_body_bytes=len(raw), body_base64=base64.b64encode(raw).decode("ascii"))
    return text, evidence


def assignment_fields(text: str, name: str) -> list[str]:
    match = re.fullmatch(r'\s*var\s+' + re.escape("hq_str_" + name) + r'\s*=\s*"([^"\r\n]*)";?\s*', text)
    if not match:
        raise ValueError("unexpected Sina assignment")
    return match[1].split(",")


class ProductLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.href: str | None = None
        self.label: list[str] = []
        self.products: dict[str, tuple[str, str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href:
            match = re.search(r"/optionsDP\.php/([a-z0-9_]+)/([a-z]+)(?:[/?#]|$)", self.href)
            if match:
                self.products["".join(self.label).strip()] = (match[1], match[2])
            self.href = None


def table_quotes(text: str) -> list[dict]:
    # SDK field order: up has strike at7/code8; down has code7 and corresponding up strike.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("unexpected option table")
    packet = json_exact(text[start:end + 1])
    data = packet["result"]["data"]
    # A real successful Sina response for a future listed month can contain no quotes.
    if data == {"info": []} and str(packet["result"]["status"]["code"]) == "0":
        return []
    up, down = data["up"], data["down"]
    if not isinstance(up, list) or not isinstance(down, list) or len(up) != len(down) or len(up) > 30000:
        raise ValueError("option side coverage mismatch")
    result = []
    for index, call in enumerate(up):
        put = down[index]
        if not isinstance(call, list) or not isinstance(put, list) or len(call) != 9 or len(put) != 8:
            raise ValueError("unexpected option table field count")
        for kind, fields, code in [("call", call, call[8]), ("put", put, put[7])]:
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", code):
                raise ValueError("invalid source contract id")
            result.append({"code": code, "kind": kind, "strike_raw": call[7], "bid_raw": fields[1],
                           "last_raw": fields[2], "ask_raw": fields[3], "open_interest_raw": fields[5],
                           "source_change_raw": fields[6], "observed_at_raw": None,
                           "source_fields": {"side": "up" if kind == "call" else "down", "row": index, "fields": fields}})
    return result


def etf_quote(text: str, code: str) -> dict:
    fields = assignment_fields(text, "CON_OP_" + code)
    # The original SDK maps the first43; Sina currently appends undocumented fields.
    # Preserve append-only extras as raw evidence without assigning their meaning.
    if not 43 <= len(fields) <= 128:
        raise ValueError("unexpected ETF option field count")
    return {"code": code, "strike_raw": fields[7], "bid_raw": fields[1], "last_raw": fields[2], "ask_raw": fields[3],
            "open_interest_raw": fields[5], "volume_raw": fields[41], "source_change_percent_raw": fields[6],
            "previous_close_raw": fields[8], "observed_at_raw": fields[32], "quote_underlying": fields[36],
            "source_fields": {"fields": fields}}
