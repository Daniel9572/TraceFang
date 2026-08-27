from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect

from tracefang.application.provider_frames import ProviderFrame, RawFrameSink
from tracefang.domain.errors import (
    InstrumentNotSupportedError,
    ProviderDataError,
    ProviderUnavailableError,
)
from tracefang.domain.models import Instrument, QuoteSnapshot, SourceMetadata
from tracefang.infrastructure.providers.jin10_web.protocol import (
    QUOTE_PUSH_PROTOCOL,
    SERVER_TIME_PROTOCOL,
    Jin10WebWireQuote,
    decode_message,
    encode_quote_subscription,
    parse_quote,
)
from tracefang.infrastructure.providers.jin10_web.settings import Jin10WebSettings
from tracefang.infrastructure.providers.jin10_web.symbols import Jin10WebSymbolMapper

_MICRO = Decimal("0.000001")
_RECONNECT_MIN_SECONDS = 0.1
_RECONNECT_MAX_SECONDS = 1.0
QuoteListener = Callable[[QuoteSnapshot], None]


class Jin10WebProvider:
    """High-frequency structured quotes exposed by Jin10's public web price client."""

    name = "jin10_web"

    def __init__(
        self,
        settings: Jin10WebSettings,
        *,
        symbol_mapper: Jin10WebSymbolMapper | None = None,
        frame_sink: RawFrameSink | None = None,
    ) -> None:
        self.settings = settings
        self.symbol_mapper = symbol_mapper or Jin10WebSymbolMapper()
        self._frame_sink = frame_sink
        self._subscriptions = self.symbol_mapper.provider_codes
        self._latest: dict[str, QuoteSnapshot] = {}
        self._updates = {code: asyncio.Event() for code in self.symbol_mapper.provider_codes}
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._last_error: str | None = None
        self._quote_listeners: set[QuoteListener] = set()
        self._connection_had_quote = False
        self._sequence = 0

    async def __aenter__(self) -> Jin10WebProvider:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def open(self) -> None:
        if not self._subscriptions:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="jin10-web-quotes")

    async def set_subscriptions(self, instruments: Sequence[Instrument]) -> None:
        subscriptions = tuple(
            dict.fromkeys(
                self.symbol_mapper.to_provider_code(instrument) for instrument in instruments
            )
        )
        if subscriptions == self._subscriptions:
            if subscriptions:
                await self.open()
            return
        await self.close()
        self._subscriptions = subscriptions
        if subscriptions:
            await self.open()

    @property
    def subscribed_provider_codes(self) -> tuple[str, ...]:
        return self._subscriptions

    async def close(self) -> None:
        task = self._task
        self._task = None
        self._connected = False
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def add_quote_listener(self, listener: QuoteListener) -> None:
        self._quote_listeners.add(listener)

    def remove_quote_listener(self, listener: QuoteListener) -> None:
        self._quote_listeners.discard(listener)

    def health(self) -> tuple[bool, str, str | None]:
        if not self._subscriptions:
            return True, "idle", "当前没有合约分配到金十官网原始通道"
        now = datetime.now(UTC)
        fresh = [
            quote
            for quote in self._latest.values()
            if (now - quote.source.received_at).total_seconds() <= self.settings.stale_after_seconds
        ]
        if fresh:
            newest = max(fresh, key=lambda quote: quote.source.received_at)
            age = max(0.0, (now - newest.source.received_at).total_seconds())
            return True, "ready", f"高速结构化推送正常。最新帧距今 {age:.1f} 秒"
        if self._task is None:
            return False, "closed", "金十官网高速行情长连接尚未启动"
        if self._task.done():
            return False, "stopped", self._last_error or "金十官网高速行情任务已停止"
        if self._connected:
            return False, "waiting_quote", self._last_error or "已连接。正在等待首个行情帧"
        return False, "reconnecting", self._last_error or "正在连接金十官网高速行情"

    async def get_quote(self, instrument: Instrument) -> QuoteSnapshot:
        provider_code = self.symbol_mapper.to_provider_code(instrument)
        if provider_code not in self._subscriptions:
            raise ProviderUnavailableError(
                f"{instrument.symbol} is not subscribed on the Jin10 web channel"
            )
        if self._task is None or self._task.done():
            raise ProviderUnavailableError("Jin10 web quote stream is not open")
        event = self._updates[provider_code]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.quote_wait_timeout_seconds
        while True:
            quote = self._fresh_quote(provider_code)
            if quote is not None:
                return quote
            event.clear()
            quote = self._fresh_quote(provider_code)
            if quote is not None:
                return quote
            remaining = deadline - loop.time()
            if remaining <= 0:
                detail = self._last_error or "已连接但尚未收到新的网页结构化行情帧"
                raise ProviderUnavailableError(detail)
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError as error:
                detail = self._last_error or "等待金十官网高速结构化行情超时"
                raise ProviderUnavailableError(detail) from error

    def _fresh_quote(self, provider_code: str) -> QuoteSnapshot | None:
        quote = self._latest.get(provider_code)
        if quote is None:
            return None
        age = (datetime.now(UTC) - quote.source.received_at).total_seconds()
        return quote if age <= self.settings.stale_after_seconds else None

    async def _run(self) -> None:
        delay = _RECONNECT_MIN_SECONDS
        while True:
            self._connection_had_quote = False
            try:
                await self._run_connection()
                self._last_error = "金十官网高速行情服务器已关闭连接。正在重连"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._last_error = self._safe_error(error)
            finally:
                self._connected = False
            await asyncio.sleep(_RECONNECT_MIN_SECONDS if self._connection_had_quote else delay)
            delay = (
                _RECONNECT_MIN_SECONDS
                if self._connection_had_quote
                else min(delay * 2, _RECONNECT_MAX_SECONDS)
            )

    async def _run_connection(self) -> None:
        connection_id = uuid4().hex
        sequence = 0
        async with connect(
            self.settings.endpoint,
            origin=self.settings.origin,
            open_timeout=self.settings.connect_timeout_seconds,
            close_timeout=5,
            ping_interval=None,
            max_size=1024 * 1024,
        ) as socket:
            subscription = encode_quote_subscription(
                provider_codes=self._subscriptions,
                frequency_ms=self.settings.quote_frequency_ms,
            )
            await socket.send(subscription)
            self._connected = True
            self._last_error = None
            async for message in socket:
                if not isinstance(message, bytes):
                    continue
                sequence += 1
                await self._capture_live_frame(
                    message,
                    connection_id=connection_id,
                    sequence=sequence,
                    received_at=datetime.now(UTC),
                    socket=socket,
                )

    async def _capture_live_frame(
        self,
        body: bytes,
        *,
        connection_id: str,
        sequence: int,
        received_at: datetime,
        socket: ClientConnection | None = None,
    ) -> QuoteSnapshot | None:
        frame = ProviderFrame(
            version=1,
            channel=self.name,
            connection_id=connection_id,
            sequence=sequence,
            received_at=received_at,
            encoding="wire",
            body=body,
        )
        if self._frame_sink is not None:
            await self._frame_sink.capture(frame)
        try:
            return await self.ingest_frame(
                frame,
                on_quote=self._accept_live_quote,
                socket=socket,
            )
        except (InstrumentNotSupportedError, ProviderDataError):
            return None

    async def ingest_frame(
        self,
        frame: ProviderFrame,
        *,
        on_quote: QuoteListener | None = None,
        socket: ClientConnection | None = None,
    ) -> QuoteSnapshot | None:
        """Decode one recorded frame without capturing or mutating live provider state."""

        if frame.channel != self.name or frame.encoding != "wire":
            raise ProviderDataError("frame does not belong to the Jin10 web wire channel")
        protocol, payload = decode_message(frame.body)
        if protocol == SERVER_TIME_PROTOCOL:
            if socket is not None:
                await socket.send("")
            return None
        if protocol != QUOTE_PUSH_PROTOCOL:
            return None
        quote = self._quote_from_wire(
            parse_quote(payload),
            protocol=protocol,
            received_at=frame.received_at,
            connection_id=frame.connection_id,
            sequence=frame.sequence,
        )
        if on_quote is not None:
            on_quote(quote)
        return quote

    def _store_quote(self, wire: Jin10WebWireQuote, *, protocol: int) -> None:
        received_at = datetime.now(UTC)
        self._sequence += 1
        quote = self._quote_from_wire(
            wire,
            protocol=protocol,
            received_at=received_at,
            connection_id="legacy",
            sequence=self._sequence,
        )
        self._accept_live_quote(quote)

    def _quote_from_wire(
        self,
        wire: Jin10WebWireQuote,
        *,
        protocol: int,
        received_at: datetime,
        connection_id: str,
        sequence: int,
    ) -> QuoteSnapshot:
        instrument = self.symbol_mapper.from_provider_code(wire.provider_code)
        last = self._price(wire.last_micros)
        if last <= 0:
            raise ProviderDataError("Jin10 web quote price must be positive")
        previous_close = self._optional_price(wire.previous_close_micros)
        change = last - previous_close if previous_close is not None else None
        change_percent = (
            change * Decimal(100) / abs(previous_close)
            if change is not None and previous_close
            else None
        )
        try:
            observed_at = datetime.fromtimestamp(wire.timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            observed_at = received_at
        if abs((received_at - observed_at).total_seconds()) > 7 * 24 * 3600:
            observed_at = received_at
        return QuoteSnapshot(
            instrument=instrument,
            last=last,
            open=None,
            high=None,
            low=None,
            volume=None,
            change=change,
            change_percent=change_percent,
            source=SourceMetadata(
                provider=self.name,
                provider_symbol=wire.provider_code,
                observed_at=observed_at,
                received_at=received_at,
                raw_payload={
                    "protocol": protocol,
                    "channel": "jin10_public_websocket",
                    "connection_id": connection_id,
                    "sequence": sequence,
                    "previous_close": str(previous_close) if previous_close else None,
                },
            ),
        )

    def _accept_live_quote(self, quote: QuoteSnapshot) -> None:
        provider_code = quote.source.provider_symbol
        self._latest[provider_code] = quote
        self._last_error = None
        self._connection_had_quote = True
        for listener in tuple(self._quote_listeners):
            with suppress(Exception):
                listener(quote)
        self._updates[provider_code].set()

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).replace("\r", " ").replace("\n", " ").strip()
        return (message or type(error).__name__)[:240]

    @staticmethod
    def _price(value: int) -> Decimal:
        return Decimal(value) * _MICRO

    @classmethod
    def _optional_price(cls, value: int) -> Decimal | None:
        return cls._price(value) if value else None
