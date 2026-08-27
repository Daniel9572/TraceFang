from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from market_analysis.domain.errors import ProviderDataError

SHANGHAI_TIME = ZoneInfo("Asia/Shanghai")
_CALLBACK = re.compile(r"^[A-Za-z0-9_]+$")
_DATE = re.compile(r"^\d{8}$")
_MINUTE = re.compile(r"^\d{12}$")
_CLOCK = re.compile(r"^\d{4}$")
_SESSION = re.compile(r"^(\d{4})-(\d{4})$")

TONGHUASHUN_HTTP_FRAME_VERSION = 1
TONGHUASHUN_HTTP_FRAME_ENCODING = "tonghuashun_http_v1"
TONGHUASHUN_LIVE_FRAME_CHANNEL = "tonghuashun_futures_live"
TONGHUASHUN_HISTORY_FRAME_CHANNEL = "tonghuashun_futures_history"
TONGHUASHUN_TIME_PRECISION = "minute"


class TonghuashunHttpFrameKind(StrEnum):
    TIME = "time"
    DAILY_LAST = "daily_last"
    MINUTE_LAST = "minute_last"
    MINUTE_YEAR = "minute_year"


def tonghuashun_frame_channel(kind: TonghuashunHttpFrameKind) -> str:
    """Keep finite line snapshots out of the realtime replay subject."""

    if kind in {
        TonghuashunHttpFrameKind.MINUTE_LAST,
        TonghuashunHttpFrameKind.MINUTE_YEAR,
    }:
        return TONGHUASHUN_HISTORY_FRAME_CHANNEL
    return TONGHUASHUN_LIVE_FRAME_CHANNEL


@dataclass(frozen=True, slots=True)
class TonghuashunHttpResponseFrame:
    """Versioned HTTP response plus the request context required for replay decoding."""

    version: int
    kind: TonghuashunHttpFrameKind
    provider_code: str
    capability: str
    request_url: str
    status_code: int
    content_type: str | None
    text_encoding: str
    content: bytes
    period: str | None = None
    file: str | None = None
    trade_date: str | None = None

    def __post_init__(self) -> None:
        if self.version != TONGHUASHUN_HTTP_FRAME_VERSION:
            raise ValueError("unsupported Tonghuashun HTTP frame version")
        for value, field in (
            (self.provider_code, "provider_code"),
            (self.capability, "capability"),
            (self.request_url, "request_url"),
            (self.text_encoding, "text_encoding"),
        ):
            if not value.strip():
                raise ValueError(f"Tonghuashun HTTP frame {field} cannot be empty")
        if not 100 <= self.status_code <= 599:
            raise ValueError("Tonghuashun HTTP frame status_code is invalid")
        if not isinstance(self.content, bytes):
            raise TypeError("Tonghuashun HTTP frame content must be bytes")
        if self.kind is TonghuashunHttpFrameKind.DAILY_LAST and (
            self.trade_date is None or not self.trade_date.strip()
        ):
            raise ValueError("a daily-last frame requires trade_date")
        if self.kind in {
            TonghuashunHttpFrameKind.DAILY_LAST,
            TonghuashunHttpFrameKind.MINUTE_LAST,
            TonghuashunHttpFrameKind.MINUTE_YEAR,
        } and (
            self.period is None
            or not self.period.strip()
            or self.file is None
            or not self.file.strip()
        ):
            raise ValueError("a line frame requires period and file")


def encode_http_response_frame(frame: TonghuashunHttpResponseFrame) -> bytes:
    """Serialize one lossless, versioned HTTP response recording."""

    value = {
        "version": frame.version,
        "kind": frame.kind.value,
        "provider_code": frame.provider_code,
        "capability": frame.capability,
        "request_url": frame.request_url,
        "status_code": frame.status_code,
        "content_type": frame.content_type,
        "text_encoding": frame.text_encoding,
        "content_base64": base64.b64encode(frame.content).decode("ascii"),
        "period": frame.period,
        "file": frame.file,
        "trade_date": frame.trade_date,
    }
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def decode_http_response_frame(body: bytes) -> TonghuashunHttpResponseFrame:
    """Purely restore the exact HTTP response and its request context."""

    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ProviderDataError("Tonghuashun recorded HTTP frame is invalid JSON") from error
    if not isinstance(value, Mapping):
        raise ProviderDataError("Tonghuashun recorded HTTP frame has an invalid root")
    try:
        required_text = {}
        for field in (
            "kind",
            "provider_code",
            "capability",
            "request_url",
            "text_encoding",
        ):
            item = value[field]
            if not isinstance(item, str) or not item.strip():
                raise TypeError(f"{field} must be non-empty text")
            required_text[field] = item
        raw_content = value["content_base64"]
        if not isinstance(raw_content, str):
            raise TypeError("content_base64 must be text")
        content = base64.b64decode(raw_content, validate=True)
        content_type = value.get("content_type")
        if content_type is not None and not isinstance(content_type, str):
            raise TypeError("content_type must be text or null")
        optional_text = {}
        for field in ("period", "file", "trade_date"):
            item = value.get(field)
            if item is not None and not isinstance(item, str):
                raise TypeError(f"{field} must be text or null")
            optional_text[field] = item
        status_code = value["status_code"]
        version = value["version"]
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise TypeError("status_code must be an integer")
        if isinstance(version, bool) or not isinstance(version, int):
            raise TypeError("version must be an integer")
        return TonghuashunHttpResponseFrame(
            version=version,
            kind=TonghuashunHttpFrameKind(required_text["kind"]),
            provider_code=required_text["provider_code"],
            capability=required_text["capability"],
            request_url=required_text["request_url"],
            status_code=status_code,
            content_type=content_type,
            text_encoding=required_text["text_encoding"],
            content=content,
            period=optional_text["period"],
            file=optional_text["file"],
            trade_date=optional_text["trade_date"],
        )
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise ProviderDataError("Tonghuashun recorded HTTP frame metadata is invalid") from error


def decode_http_response_jsonp(frame: TonghuashunHttpResponseFrame) -> Mapping[str, Any]:
    """Decode captured response bytes without reconstructing an ``httpx.Response``."""

    try:
        text = frame.content.decode(frame.text_encoding)
    except (LookupError, UnicodeDecodeError) as error:
        raise ProviderDataError("Tonghuashun recorded HTTP response encoding is invalid") from error
    return decode_jsonp(text)


@dataclass(frozen=True, slots=True)
class TonghuashunDecodedQuoteFrame:
    response: TonghuashunHttpResponseFrame
    connection_id: str
    sequence: int
    received_at: datetime
    quote: TonghuashunWireQuote


@dataclass(frozen=True, slots=True)
class TonghuashunDecodedDailyFrame:
    response: TonghuashunHttpResponseFrame
    connection_id: str
    sequence: int
    received_at: datetime
    stats: TonghuashunDailyStats | None


@dataclass(frozen=True, slots=True)
class TonghuashunDecodedLineFrame:
    response: TonghuashunHttpResponseFrame
    connection_id: str
    sequence: int
    received_at: datetime
    rows: tuple[TonghuashunWireCandle, ...]


TonghuashunDecodedFrame = (
    TonghuashunDecodedQuoteFrame | TonghuashunDecodedDailyFrame | TonghuashunDecodedLineFrame
)


@dataclass(frozen=True, slots=True)
class TonghuashunWireQuote:
    name: str
    trade_date: str
    observed_at: datetime
    last: Decimal
    previous_settlement: Decimal


@dataclass(frozen=True, slots=True)
class TonghuashunDailyStats:
    trade_date: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class TonghuashunWireCandle:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def decode_jsonp(text: str) -> Mapping[str, Any]:
    value = text.strip().rstrip(";")
    opening = value.find("(")
    closing = value.rfind(")")
    if opening <= 0 or closing <= opening or value[closing + 1 :].strip():
        raise ProviderDataError("Tonghuashun futures response is not valid JSONP")
    callback = value[:opening].strip()
    if _CALLBACK.fullmatch(callback) is None:
        raise ProviderDataError("Tonghuashun futures JSONP callback is invalid")
    try:
        payload = json.loads(value[opening + 1 : closing])
    except (json.JSONDecodeError, ValueError) as error:
        raise ProviderDataError("Tonghuashun futures response contains invalid JSON") from error
    if not isinstance(payload, Mapping):
        raise ProviderDataError("Tonghuashun futures response has an invalid root")
    return payload


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProviderDataError(f"Tonghuashun futures field {field} is not numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ProviderDataError(f"Tonghuashun futures field {field} is not numeric") from error
    if not parsed.is_finite():
        raise ProviderDataError(f"Tonghuashun futures field {field} is not finite")
    return parsed


def _text(value: object, field: str) -> str:
    parsed = str(value or "").strip()
    if not parsed:
        raise ProviderDataError(f"Tonghuashun futures field {field} is missing")
    return parsed


def _instrument_node(
    payload: Mapping[str, Any],
    *,
    expected_provider_code: str,
) -> Mapping[str, Any]:
    node = payload.get(expected_provider_code)
    if not isinstance(node, Mapping):
        raise ProviderDataError("Tonghuashun futures response returned a different symbol")
    return node


def _clock_minutes(value: str) -> int:
    return int(value[:2]) * 60 + int(value[2:])


def _trade_intervals(value: object) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProviderDataError("Tonghuashun public quote trade sessions are missing")
    intervals: list[tuple[int, int]] = []
    for item in value:
        match = _SESSION.fullmatch(str(item))
        if match is None:
            raise ProviderDataError("Tonghuashun public quote trade session is invalid")
        start, end = match.groups()
        intervals.append((_clock_minutes(start), _clock_minutes(end)))
    if not intervals:
        raise ProviderDataError("Tonghuashun public quote trade sessions are missing")
    return tuple(intervals)


def _calendar_date(
    clock: str,
    dates: tuple[str, ...],
    trade_date: str,
    trade_intervals: tuple[tuple[int, int], ...],
    *,
    mode: str,
) -> str:
    minutes = _clock_minutes(clock)
    cross_midnight = next(
        ((start, end) for start, end in trade_intervals if start > end),
        None,
    )
    if mode == "session_dates":
        if cross_midnight is not None:
            start, end = cross_midnight
            if minutes >= start:
                return dates[0]
            if minutes <= end:
                return dates[1] if len(dates) >= 2 else trade_date
        return dates[-1] if dates else trade_date
    if mode != "trade_date":
        raise ProviderDataError("Tonghuashun public quote calendar mode is invalid")
    try:
        calendar_date = datetime.strptime(trade_date, "%Y%m%d")
    except ValueError as error:
        raise ProviderDataError("Tonghuashun public quote trade date is invalid") from error
    if cross_midnight is not None and minutes <= cross_midnight[1]:
        calendar_date += timedelta(days=1)
    return calendar_date.strftime("%Y%m%d")


def parse_time_payload(
    payload: Mapping[str, Any],
    *,
    expected_provider_code: str,
    expected_name: str,
    calendar_mode: str = "session_dates",
) -> TonghuashunWireQuote:
    node = _instrument_node(payload, expected_provider_code=expected_provider_code)
    name = _text(node.get("name"), "name")
    if name != expected_name:
        raise ProviderDataError("Tonghuashun futures response returned a different instrument")
    trade_date = _text(node.get("date"), "date")
    if _DATE.fullmatch(trade_date) is None:
        raise ProviderDataError("Tonghuashun futures trade date is invalid")
    dates_value = node.get("dates")
    if not isinstance(dates_value, Sequence) or isinstance(dates_value, (str, bytes)):
        raise ProviderDataError("Tonghuashun futures session dates are missing")
    dates = tuple(str(item) for item in dates_value)
    if not dates or any(_DATE.fullmatch(item) is None for item in dates):
        raise ProviderDataError("Tonghuashun futures session date is invalid")
    trade_intervals = _trade_intervals(node.get("tradeTime"))
    data = _text(node.get("data"), "data")
    last_row: tuple[str, Decimal] | None = None
    for raw_row in data.split(";"):
        fields = raw_row.split(",")
        if len(fields) < 5 or _CLOCK.fullmatch(fields[0]) is None:
            raise ProviderDataError("Tonghuashun futures time row is truncated")
        price = _decimal(fields[1], "time.price")
        if price > 0:
            last_row = (fields[0], price)
    if last_row is None:
        raise ProviderDataError("Tonghuashun futures time response has no positive price")
    clock, last = last_row
    observed_date = _calendar_date(
        clock,
        dates,
        trade_date,
        trade_intervals,
        mode=calendar_mode,
    )
    try:
        observed_at = datetime.strptime(observed_date + clock, "%Y%m%d%H%M").replace(
            tzinfo=SHANGHAI_TIME
        )
    except ValueError as error:
        raise ProviderDataError("Tonghuashun futures quote timestamp is invalid") from error
    previous_settlement = _decimal(node.get("pre"), "pre")
    if previous_settlement <= 0:
        raise ProviderDataError("Tonghuashun futures previous settlement must be positive")
    return TonghuashunWireQuote(
        name=name,
        trade_date=trade_date,
        observed_at=observed_at,
        last=last,
        previous_settlement=previous_settlement,
    )


def parse_daily_stats_payload(
    payload: Mapping[str, Any],
    *,
    expected_name: str,
    expected_trade_date: str,
) -> TonghuashunDailyStats | None:
    name = _text(payload.get("name"), "name")
    if name != expected_name:
        raise ProviderDataError("Tonghuashun futures daily line returned another instrument")
    data = _text(payload.get("data"), "data")
    for raw_row in reversed(data.split(";")):
        fields = raw_row.split(",")
        if len(fields) < 7:
            raise ProviderDataError("Tonghuashun futures daily row is truncated")
        if fields[0] != expected_trade_date:
            continue
        open_price = _decimal(fields[1], "daily.open")
        high = _decimal(fields[2], "daily.high")
        low = _decimal(fields[3], "daily.low")
        close = _decimal(fields[4], "daily.close")
        volume = _decimal(fields[5], "daily.volume")
        if min(open_price, high, low, close) <= 0:
            raise ProviderDataError("Tonghuashun futures daily price must be positive")
        if low > high or not low <= open_price <= high or not low <= close <= high:
            raise ProviderDataError("Tonghuashun futures daily range is inconsistent")
        if volume < 0:
            raise ProviderDataError("Tonghuashun futures daily volume cannot be negative")
        return TonghuashunDailyStats(
            trade_date=fields[0],
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
        )
    return None


def parse_line_payload(
    payload: Mapping[str, Any],
    *,
    expected_name: str,
    time_zone: ZoneInfo = SHANGHAI_TIME,
) -> tuple[TonghuashunWireCandle, ...]:
    name_value = payload.get("name")
    if name_value is not None and _text(name_value, "name") != expected_name:
        raise ProviderDataError("Tonghuashun futures line returned another instrument")
    data = _text(payload.get("data"), "data")
    rows: list[TonghuashunWireCandle] = []
    previous_time: datetime | None = None
    for raw_row in data.split(";"):
        fields = raw_row.split(",")
        if len(fields) < 7 or _MINUTE.fullmatch(fields[0]) is None:
            raise ProviderDataError("Tonghuashun futures minute row is truncated")
        try:
            open_time = datetime.strptime(fields[0], "%Y%m%d%H%M").replace(tzinfo=time_zone)
        except ValueError as error:
            raise ProviderDataError("Tonghuashun futures minute timestamp is invalid") from error
        open_price = _decimal(fields[1], "minute.open")
        high = _decimal(fields[2], "minute.high")
        low = _decimal(fields[3], "minute.low")
        close = _decimal(fields[4], "minute.close")
        volume = _decimal(fields[5], "minute.volume")
        if min(open_price, high, low, close) <= 0:
            continue
        if low > high or not low <= open_price <= high or not low <= close <= high:
            raise ProviderDataError("Tonghuashun futures minute range is inconsistent")
        if volume < 0:
            raise ProviderDataError("Tonghuashun futures minute volume cannot be negative")
        if previous_time is not None and open_time <= previous_time:
            raise ProviderDataError("Tonghuashun futures minute rows are not ordered")
        previous_time = open_time
        rows.append(
            TonghuashunWireCandle(
                open_time=open_time,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    return tuple(rows)
