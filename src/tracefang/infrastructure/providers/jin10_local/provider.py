from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import time_ns
from urllib.parse import quote as quote_url
from uuid import uuid4

import httpx
from websockets.asyncio.client import ClientConnection, connect

from tracefang.application.provider_frames import ProviderFrame, RawFrameSink
from tracefang.domain.errors import (
    InstrumentNotSupportedError,
    ProviderDataError,
    ProviderUnavailableError,
)
from tracefang.domain.market_events import BarState
from tracefang.domain.models import Candle, Instrument, QuoteSnapshot, SourceMetadata
from tracefang.infrastructure.providers.jin10_local.protocol import (
    ADVANCED_QUOTE_PUSH_PROTOCOL,
    KLINE_HISTORY_PROTOCOL,
    KLINE_SNAPSHOT_PROTOCOL,
    KLINE_UPDATE_PROTOCOL,
    QUOTE_PUSH_PROTOCOL,
    RELOGIN_REQUEST_PROTOCOL,
    Jin10KlineHistoryFile,
    Jin10KlineHistoryManifest,
    Jin10KlineSnapshot,
    Jin10WireCandle,
    Jin10WireQuote,
    decode_message,
    derive_session_key,
    encode_kline_history_request,
    encode_kline_subscription,
    encode_login,
    encode_quote_subscription,
    parse_kline_history_file,
    parse_kline_history_manifest,
    parse_kline_snapshot,
    parse_kline_update,
    parse_quote,
    xor_cipher,
)
from tracefang.infrastructure.providers.jin10_local.settings import Jin10LocalSettings
from tracefang.infrastructure.providers.jin10_local.symbols import Jin10LocalSymbolMapper

_MICRO = Decimal("0.000001")
_QUOTE_PROTOCOLS = frozenset({QUOTE_PUSH_PROTOCOL, ADVANCED_QUOTE_PUSH_PROTOCOL})
_RECONNECT_MIN_SECONDS = 0.1
_RECONNECT_MAX_SECONDS = 1.0
_MAX_MINUTE_CANDLES_PER_SYMBOL = 43_200
_KLINE_TIME_TYPE = 1
_MAX_HISTORY_PAGES = 64
_MAX_HISTORY_FILE_CACHE = 32
QuoteListener = Callable[[QuoteSnapshot], None]
CandleListener = Callable[[Candle], None]
HistoryManifestListener = Callable[[Jin10KlineHistoryManifest], None]


def _manifest_record_count(
    item: Jin10KlineHistoryFile,
    timestamps: Sequence[int],
) -> int:
    if item.start_timestamp is None or item.end_timestamp is None:
        return len(timestamps)
    return sum(
        item.start_timestamp <= timestamp <= item.end_timestamp
        for timestamp in timestamps
    )


def _manifest_version(item: Jin10KlineHistoryFile) -> str:
    record_count = item.record_count if item.record_count is not None else "unknown"
    end_timestamp = item.end_timestamp if item.end_timestamp is not None else "unknown"
    return f"{record_count}-{end_timestamp}"


class Jin10LocalProvider:
    """Structured Jin10 quote stream authenticated by the local desktop session."""

    name = "jin10_local"

    def __init__(
        self,
        settings: Jin10LocalSettings,
        *,
        symbol_mapper: Jin10LocalSymbolMapper | None = None,
        frame_sink: RawFrameSink | None = None,
    ) -> None:
        self.settings = settings
        self.symbol_mapper = symbol_mapper or Jin10LocalSymbolMapper()
        self._frame_sink = frame_sink
        self._subscriptions = self.symbol_mapper.provider_codes
        self._latest: dict[str, QuoteSnapshot] = {}
        self._minute_candles: dict[str, dict[datetime, Candle]] = {}
        self._updates = {code: asyncio.Event() for code in self.symbol_mapper.provider_codes}
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._last_error: str | None = None
        self._quote_listeners: set[QuoteListener] = set()
        self._candle_listeners: set[CandleListener] = set()
        self._connection_had_quote = False
        self._sequence = 0
        self._connection_ready = asyncio.Event()
        self._active_socket: ClientConnection | None = None
        self._active_session_key: str | None = None
        self._send_lock = asyncio.Lock()
        self._history_request_lock = asyncio.Lock()
        self._history_waiters: dict[
            tuple[str, int, int], asyncio.Future[Jin10KlineHistoryManifest]
        ] = {}
        self._history_file_cache: dict[tuple[str, int, str], tuple[Candle, ...]] = {}

    async def __aenter__(self) -> Jin10LocalProvider:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def open(self) -> None:
        if not self._subscriptions:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="jin10-local-quotes")

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

    def provider_symbol(self, instrument: Instrument) -> str:
        return self.symbol_mapper.to_provider_code(instrument)

    async def close(self) -> None:
        task = self._task
        self._task = None
        self._connected = False
        self._connection_ready.clear()
        self._active_socket = None
        self._active_session_key = None
        self._fail_history_waiters(ProviderUnavailableError("Jin10 local connection closed"))
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def add_quote_listener(self, listener: QuoteListener) -> None:
        self._quote_listeners.add(listener)

    def remove_quote_listener(self, listener: QuoteListener) -> None:
        self._quote_listeners.discard(listener)

    def add_candle_listener(self, listener: CandleListener) -> None:
        self._candle_listeners.add(listener)

    def remove_candle_listener(self, listener: CandleListener) -> None:
        self._candle_listeners.discard(listener)

    def health(self) -> tuple[bool, str, str | None]:
        if not self._subscriptions:
            return True, "idle", "当前没有合约分配到金十桌面会话原始通道"
        now = datetime.now(UTC)
        fresh = [
            quote
            for quote in self._latest.values()
            if (now - quote.source.received_at).total_seconds() <= self.settings.stale_after_seconds
        ]
        if fresh:
            newest = max(fresh, key=lambda quote: quote.source.received_at)
            age = max(0.0, (now - newest.source.received_at).total_seconds())
            return True, "ready", f"结构化推送正常。最新帧距今 {age:.1f} 秒"
        if self._task is None:
            return False, "closed", "本地行情长连接尚未启动"
        if self._task.done():
            return False, "stopped", self._last_error or "本地行情任务已停止"
        if self._connected:
            return False, "waiting_quote", self._last_error or "已连接。正在等待首个行情帧"
        return False, "reconnecting", self._last_error or "正在连接本地金十会话"

    async def get_quote(self, instrument: Instrument) -> QuoteSnapshot:
        provider_code = self.symbol_mapper.to_provider_code(instrument)
        if provider_code not in self._subscriptions:
            raise ProviderUnavailableError(
                f"{instrument.symbol} is not subscribed on the Jin10 local channel"
            )
        if self._task is None or self._task.done():
            raise ProviderUnavailableError("Jin10 local quote stream is not open")
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
                detail = self._last_error or "已连接但尚未收到新的结构化行情帧"
                raise ProviderUnavailableError(detail)
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError as error:
                detail = self._last_error or "等待金十桌面会话结构化行情超时"
                raise ProviderUnavailableError(detail) from error

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")
        provider_code = self.symbol_mapper.to_provider_code(instrument)
        rows = sorted(
            self._minute_candles.get(provider_code, {}).values(),
            key=lambda candle: candle.open_time,
        )
        if start is not None:
            return tuple(candle for candle in rows if candle.open_time >= start)[:count]
        return tuple(rows[-count:])

    async def fetch_historical_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime,
        count: int,
    ) -> tuple[Candle, ...]:
        """Fetches one exact historical window from this channel's own Kline protocol."""

        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start must be timezone-aware")
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        provider_code = self.symbol_mapper.to_provider_code(instrument)
        if provider_code not in self._subscriptions:
            raise ProviderUnavailableError(
                f"{instrument.symbol} is not subscribed on the Jin10 local channel"
            )
        await self.open()
        try:
            await asyncio.wait_for(
                self._connection_ready.wait(),
                self.settings.kline_wait_timeout_seconds,
            )
        except TimeoutError as error:
            raise ProviderUnavailableError(
                self._last_error or "等待金十同源 K 线连接超时"
            ) from error

        range_start = start.astimezone(UTC).replace(microsecond=0)
        range_end = range_start + timedelta(minutes=count)
        collected: dict[datetime, Candle] = {}
        # Protocol 10006 accepts a target boundary and returns files immediately before it.
        boundary = int(range_end.timestamp())
        seen_boundaries: set[int] = set()
        seen_files: set[str] = set()

        async with self._history_request_lock:
            for _ in range(_MAX_HISTORY_PAGES):
                if boundary in seen_boundaries:
                    raise ProviderDataError("Jin10 Kline history pagination did not advance")
                seen_boundaries.add(boundary)
                manifest = await self._request_history_manifest(
                    provider_code,
                    time_type=_KLINE_TIME_TYPE,
                    boundary_timestamp=boundary,
                )
                new_files = tuple(
                    item for item in manifest.files if item.file_name not in seen_files
                )
                if not new_files:
                    break
                seen_files.update(item.file_name for item in new_files)
                downloaded = await self._download_history_files(
                    instrument,
                    provider_code,
                    manifest.time_type,
                    new_files,
                )
                for candle in downloaded:
                    if range_start <= candle.open_time < range_end:
                        collected[candle.open_time] = candle
                oldest = min(
                    (
                        item.start_timestamp
                        for item in new_files
                        if item.start_timestamp is not None
                    ),
                    default=None,
                )
                if oldest is None and downloaded:
                    oldest = int(min(item.open_time for item in downloaded).timestamp())
                if oldest is None or oldest <= int(range_start.timestamp()):
                    break
                boundary = oldest
            else:
                raise ProviderUnavailableError("金十同源 K 线回补超过安全分页上限")

        return tuple(collected[key] for key in sorted(collected))

    def seed_candles(self, candles: tuple[Candle, ...]) -> None:
        for candle in candles:
            if candle.source.provider != self.name or candle.interval != timedelta(minutes=1):
                continue
            rows = self._minute_candles.setdefault(candle.source.provider_symbol, {})
            current = rows.get(candle.open_time)
            if current is None or candle.source.received_at >= current.source.received_at:
                rows[candle.open_time] = candle
            self._trim_candles(rows)

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
                self._last_error = "金十行情服务器已关闭连接。正在重连"
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
            open_timeout=self.settings.connect_timeout_seconds,
            close_timeout=5,
            ping_interval=None,
            max_size=1024 * 1024,
        ) as socket:
            handshake = await asyncio.wait_for(socket.recv(), self.settings.connect_timeout_seconds)
            if not isinstance(handshake, bytes):
                raise ProviderDataError("Jin10 local handshake is not binary")
            key = derive_session_key(handshake)
            self._connected = True
            self._last_error = None
            await self._send_login(socket, key)
            quote_subscription = encode_quote_subscription(
                provider_codes=self._subscriptions,
                frequency_ms=self.settings.quote_frequency_ms,
            )
            kline_subscription = encode_kline_subscription(
                provider_codes=self._subscriptions,
                time_type=_KLINE_TIME_TYPE,
                frequency_ms=self.settings.kline_frequency_ms,
            )
            async with self._send_lock:
                await socket.send(xor_cipher(quote_subscription, key))
                await socket.send(xor_cipher(kline_subscription, key))
            self._active_socket = socket
            self._active_session_key = key
            self._connection_ready.set()
            heartbeat = asyncio.create_task(self._heartbeat(socket), name="jin10-local-heartbeat")
            try:
                async for message in socket:
                    if not isinstance(message, bytes):
                        continue
                    sequence += 1
                    await self._capture_live_frame(
                        xor_cipher(message, key),
                        connection_id=connection_id,
                        sequence=sequence,
                        received_at=datetime.now(UTC),
                        socket=socket,
                        session_key=key,
                    )
            finally:
                if self._active_socket is socket:
                    self._connection_ready.clear()
                    self._active_socket = None
                    self._active_session_key = None
                    self._fail_history_waiters(
                        ProviderUnavailableError("Jin10 local connection was interrupted")
                    )
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _capture_live_frame(
        self,
        body: bytes,
        *,
        connection_id: str,
        sequence: int,
        received_at: datetime,
        socket: ClientConnection | None = None,
        session_key: str | None = None,
    ) -> None:
        frame = ProviderFrame(
            version=1,
            channel=self.name,
            connection_id=connection_id,
            sequence=sequence,
            received_at=received_at,
            encoding="session-decrypted",
            body=body,
        )
        if self._frame_sink is not None:
            await self._frame_sink.capture(frame)
        try:
            await self.ingest_frame(
                frame,
                on_quote=self._accept_live_quote,
                on_candle=self._accept_live_candle,
                on_history_manifest=self._resolve_history_manifest,
                socket=socket,
                session_key=session_key,
            )
        except (InstrumentNotSupportedError, ProviderDataError):
            return

    async def ingest_frame(
        self,
        frame: ProviderFrame,
        *,
        on_quote: QuoteListener | None = None,
        on_candle: CandleListener | None = None,
        on_history_manifest: HistoryManifestListener | None = None,
        socket: ClientConnection | None = None,
        session_key: str | None = None,
    ) -> None:
        """Decode one decrypted frame without capturing or mutating live provider state."""

        if frame.channel != self.name or frame.encoding != "session-decrypted":
            raise ProviderDataError("frame does not belong to the Jin10 local wire channel")
        protocol, payload = decode_message(frame.body)
        if protocol == RELOGIN_REQUEST_PROTOCOL:
            if socket is not None:
                if session_key is None:
                    raise ProviderDataError("live relogin frame requires a session key")
                await self._send_login(socket, session_key)
            return
        if protocol in _QUOTE_PROTOCOLS:
            quote = self._quote_from_wire(
                parse_quote(payload),
                protocol=protocol,
                received_at=frame.received_at,
                connection_id=frame.connection_id,
                sequence=frame.sequence,
            )
            if on_quote is not None:
                on_quote(quote)
            return
        snapshot: Jin10KlineSnapshot | None = None
        if protocol == KLINE_SNAPSHOT_PROTOCOL:
            snapshot = parse_kline_snapshot(payload)
        elif protocol == KLINE_UPDATE_PROTOCOL:
            snapshot = parse_kline_update(payload)
        elif protocol == KLINE_HISTORY_PROTOCOL:
            manifest = parse_kline_history_manifest(payload)
            if on_history_manifest is not None:
                on_history_manifest(manifest)
            return
        if snapshot is None or snapshot.time_type != _KLINE_TIME_TYPE:
            return
        instrument = self.symbol_mapper.from_provider_code(snapshot.provider_code)
        candles = self._candles_from_wire(
            instrument,
            snapshot.provider_code,
            snapshot.candles,
            protocol=protocol,
            time_type=snapshot.time_type,
            received_at=frame.received_at,
            state=BarState.PROVISIONAL_AUTHORITATIVE,
            connection_id=frame.connection_id,
            sequence=frame.sequence,
        )
        if on_candle is not None:
            for candle in candles:
                on_candle(candle)

    async def _send_login(self, socket: ClientConnection, key: str) -> None:
        packet = encode_login(
            user_id=self.settings.user_id,
            session_token=self.settings.session_token,
            vip_type=self.settings.vip_type,
        )
        async with self._send_lock:
            await socket.send(xor_cipher(packet, key))

    async def _heartbeat(self, socket: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            async with self._send_lock:
                await socket.send("")

    async def _request_history_manifest(
        self,
        provider_code: str,
        *,
        time_type: int,
        boundary_timestamp: int,
    ) -> Jin10KlineHistoryManifest:
        socket = self._active_socket
        key = self._active_session_key
        if socket is None or key is None or not self._connection_ready.is_set():
            raise ProviderUnavailableError("金十同源 K 线连接尚未就绪")
        request_key = (provider_code, time_type, boundary_timestamp)
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[Jin10KlineHistoryManifest] = loop.create_future()
        self._history_waiters[request_key] = waiter
        packet = encode_kline_history_request(
            provider_code=provider_code,
            time_type=time_type,
            boundary_timestamp=boundary_timestamp,
        )
        try:
            async with self._send_lock:
                await socket.send(xor_cipher(packet, key))
            return await asyncio.wait_for(waiter, self.settings.kline_wait_timeout_seconds)
        except TimeoutError as error:
            raise ProviderUnavailableError("等待金十同源历史 K 线目录超时") from error
        finally:
            self._history_waiters.pop(request_key, None)

    def _resolve_history_manifest(self, manifest: Jin10KlineHistoryManifest) -> None:
        request_key = (
            manifest.provider_code,
            manifest.time_type,
            manifest.boundary_timestamp,
        )
        waiter = self._history_waiters.get(request_key)
        if waiter is None:
            candidates = [
                value
                for (code, time_type, _), value in self._history_waiters.items()
                if code == manifest.provider_code and time_type == manifest.time_type
            ]
            waiter = candidates[0] if len(candidates) == 1 else None
        if waiter is not None and not waiter.done():
            waiter.set_result(manifest)

    def _fail_history_waiters(self, error: Exception) -> None:
        for waiter in tuple(self._history_waiters.values()):
            if not waiter.done():
                waiter.set_exception(error)
        self._history_waiters.clear()

    async def _download_history_files(
        self,
        instrument: Instrument,
        provider_code: str,
        time_type: int,
        files: tuple[Jin10KlineHistoryFile, ...],
    ) -> tuple[Candle, ...]:
        async with httpx.AsyncClient(
            timeout=self.settings.kline_download_timeout_seconds,
            follow_redirects=True,
        ) as client:
            pages = await asyncio.gather(
                *(
                    self._download_history_file(
                        client,
                        instrument,
                        provider_code,
                        time_type,
                        item,
                    )
                    for item in files
                )
            )
        return tuple(candle for page in pages for candle in page)

    async def _download_history_file(
        self,
        client: httpx.AsyncClient,
        instrument: Instrument,
        provider_code: str,
        time_type: int,
        item: Jin10KlineHistoryFile,
    ) -> tuple[Candle, ...]:
        cache_key = (provider_code, time_type, item.file_name)
        cached = self._history_file_cache.get(cache_key)
        if cached is not None and (
            item.record_count is None
            or _manifest_record_count(
                item,
                tuple(int(candle.open_time.timestamp()) for candle in cached),
            )
            == item.record_count
        ):
            return cached
        url = "/".join(
            (
                self.settings.kline_file_endpoint,
                quote_url(provider_code, safe=""),
                str(time_type),
                quote_url(item.file_name, safe=""),
            )
        )

        async def request_wire_rows(*, refresh: int | None = None) -> tuple[Jin10WireCandle, ...]:
            params: dict[str, str | int] = {
                "manifest_version": _manifest_version(item),
            }
            if refresh is not None:
                params["refresh"] = refresh
            response = await client.get(url, params=params)
            response.raise_for_status()
            return parse_kline_history_file(response.content)

        try:
            wire_rows = await request_wire_rows()
            manifest_record_count = _manifest_record_count(
                item,
                tuple(row.timestamp for row in wire_rows),
            )
            if item.record_count is not None and manifest_record_count != item.record_count:
                wire_rows = await request_wire_rows(refresh=time_ns())
                manifest_record_count = _manifest_record_count(
                    item,
                    tuple(row.timestamp for row in wire_rows),
                )
        except httpx.HTTPError as error:
            raise ProviderUnavailableError("下载金十同源历史 K 线文件失败") from error
        if item.record_count is not None and manifest_record_count != item.record_count:
            raise ProviderDataError(
                "Jin10 Kline history file row count does not match manifest: "
                f"file={item.file_name}, expected={item.record_count}, "
                f"actual={manifest_record_count}, downloaded={len(wire_rows)}, "
                f"start={item.start_timestamp}, end={item.end_timestamp}"
            )
        rows = self._store_wire_candles(
            instrument,
            provider_code,
            wire_rows,
            protocol=KLINE_HISTORY_PROTOCOL,
            time_type=time_type,
            file_name=item.file_name,
        )
        self._history_file_cache[cache_key] = rows
        while len(self._history_file_cache) > _MAX_HISTORY_FILE_CACHE:
            del self._history_file_cache[next(iter(self._history_file_cache))]
        return rows

    def _store_kline_snapshot(self, value: Jin10KlineSnapshot, *, protocol: int) -> None:
        if value.time_type != _KLINE_TIME_TYPE:
            return
        instrument = self.symbol_mapper.from_provider_code(value.provider_code)
        self._sequence += 1
        candles = self._candles_from_wire(
            instrument,
            value.provider_code,
            value.candles,
            protocol=protocol,
            time_type=value.time_type,
            received_at=datetime.now(UTC),
            state=BarState.PROVISIONAL_AUTHORITATIVE,
            connection_id="legacy",
            sequence=self._sequence,
        )
        for candle in candles:
            self._accept_live_candle(candle)

    def _store_wire_candles(
        self,
        instrument: Instrument,
        provider_code: str,
        wire_rows: tuple[Jin10WireCandle, ...],
        *,
        protocol: int,
        time_type: int,
        file_name: str | None = None,
        state: BarState = BarState.FINAL,
        publish: bool = False,
    ) -> tuple[Candle, ...]:
        rows = self._candles_from_wire(
            instrument,
            provider_code,
            wire_rows,
            protocol=protocol,
            time_type=time_type,
            received_at=datetime.now(UTC),
            file_name=file_name,
            state=state,
        )
        target = self._minute_candles.setdefault(provider_code, {})
        for candle in rows:
            current = target.get(candle.open_time)
            if current is None or candle.source.received_at >= current.source.received_at:
                target[candle.open_time] = candle
                if publish:
                    for listener in tuple(self._candle_listeners):
                        with suppress(Exception):
                            listener(candle)
        self._trim_candles(target)
        return rows

    def _candles_from_wire(
        self,
        instrument: Instrument,
        provider_code: str,
        wire_rows: tuple[Jin10WireCandle, ...],
        *,
        protocol: int,
        time_type: int,
        received_at: datetime,
        file_name: str | None = None,
        state: BarState = BarState.FINAL,
        connection_id: str | None = None,
        sequence: int | None = None,
    ) -> tuple[Candle, ...]:
        rows: list[Candle] = []
        for wire in wire_rows:
            try:
                open_time = datetime.fromtimestamp(wire.timestamp, tz=UTC)
            except (OSError, OverflowError, ValueError) as error:
                raise ProviderDataError("Jin10 Kline timestamp is invalid") from error
            candle = Candle(
                instrument=instrument,
                interval=timedelta(minutes=1),
                open_time=open_time,
                open=self._price(wire.open_micros),
                high=self._price(wire.high_micros),
                low=self._price(wire.low_micros),
                close=self._price(wire.close_micros),
                volume=Decimal(wire.volume),
                source=SourceMetadata(
                    provider=self.name,
                    provider_symbol=provider_code,
                    observed_at=open_time,
                    received_at=received_at,
                    raw_payload={
                        "protocol": protocol,
                        "time_type": time_type,
                        "price_scale": 1_000_000,
                        "history_file": file_name,
                        "bar_state": state.value,
                        "connection_id": connection_id,
                        "sequence": sequence,
                    },
                ),
            )
            rows.append(candle)
        return tuple(rows)

    def _store_quote(self, wire: Jin10WireQuote, *, protocol: int) -> None:
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
        wire: Jin10WireQuote,
        *,
        protocol: int,
        received_at: datetime,
        connection_id: str,
        sequence: int,
    ) -> QuoteSnapshot:
        instrument = self.symbol_mapper.from_provider_code(wire.provider_code)
        last = self._price(wire.last_micros)
        if last <= 0:
            raise ProviderDataError("Jin10 local quote price must be positive")
        high = self._optional_price(wire.high_micros)
        low = self._optional_price(wire.low_micros)
        if high is not None and low is not None and not low <= last <= high:
            high = None
            low = None
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
            open=self._optional_price(wire.open_micros),
            high=high,
            low=low,
            volume=Decimal(wire.volume) if wire.volume >= 0 else None,
            change=change,
            change_percent=change_percent,
            source=SourceMetadata(
                provider=self.name,
                provider_symbol=wire.provider_code,
                observed_at=observed_at,
                received_at=received_at,
                raw_payload={
                    "protocol": protocol,
                    "buy": str(self._price(wire.buy_micros)),
                    "ask": str(self._price(wire.ask_micros)),
                    "previous_close": str(previous_close) if previous_close else None,
                    "turnover": wire.turnover,
                    "connection_id": connection_id,
                    "sequence": sequence,
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

    def _accept_live_candle(self, candle: Candle) -> None:
        provider_code = candle.source.provider_symbol
        target = self._minute_candles.setdefault(provider_code, {})
        current = target.get(candle.open_time)
        if current is not None and candle.source.received_at < current.source.received_at:
            return
        target[candle.open_time] = candle
        self._trim_candles(target)
        for listener in tuple(self._candle_listeners):
            with suppress(Exception):
                listener(candle)

    @staticmethod
    def _trim_candles(rows: dict[datetime, Candle]) -> None:
        overflow = len(rows) - _MAX_MINUTE_CANDLES_PER_SYMBOL
        if overflow <= 0:
            return
        for open_time in sorted(rows)[:overflow]:
            del rows[open_time]

    def _safe_error(self, error: Exception) -> str:
        message = str(error).replace(self.settings.session_token, "<redacted>")
        message = message.replace("\r", " ").replace("\n", " ").strip()
        return (message or type(error).__name__)[:240]

    @staticmethod
    def _price(value: int) -> Decimal:
        return Decimal(value) * _MICRO

    @classmethod
    def _optional_price(cls, value: int) -> Decimal | None:
        return cls._price(value) if value else None
