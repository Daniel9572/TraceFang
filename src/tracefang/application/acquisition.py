from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from tracefang.application.sources import ProviderProbe, SourceHealth
from tracefang.domain.models import Instrument, QuoteSnapshot


class ManagedPushQuoteChannel(Protocol):
    name: str

    async def set_subscriptions(self, instruments: Sequence[Instrument]) -> None: ...

    async def get_quote(self, instrument: Instrument) -> QuoteSnapshot: ...


class ManagedPollQuoteChannel(Protocol):
    name: str

    async def get_quote(self, instrument: Instrument) -> QuoteSnapshot: ...


QuoteSink = Callable[[QuoteSnapshot], None]
ErrorSink = Callable[[Instrument, str, Exception], None]
PollInterval = Callable[[str], float]
PrepareSource = Callable[[str], Awaitable[None]]
SourceEnabled = Callable[[str], bool]
_PollKey = tuple[str, Instrument]


@dataclass(slots=True)
class _PollState:
    checked_at: datetime | None = None
    last_success_at: datetime | None = None
    error: str | None = None
    data_fresh: bool = False


class QuoteAcquisitionRouter:
    """Owns upstream acquisition independently from API/UI subscriptions."""

    def __init__(
        self,
        *,
        push_channels: Mapping[str, ManagedPushQuoteChannel],
        poll_channels: Mapping[str, ManagedPollQuoteChannel],
        source_channels: Mapping[str, Sequence[str]],
        source_enabled: SourceEnabled,
        prepare_source: PrepareSource,
        poll_interval: PollInterval,
        on_quote: QuoteSink,
        on_error: ErrorSink,
    ) -> None:
        self._push_channels = dict(push_channels)
        self._poll_channels = dict(poll_channels)
        self._source_channels = {
            source_id: tuple(dict.fromkeys(channels))
            for source_id, channels in source_channels.items()
        }
        self._source_enabled = source_enabled
        self._prepare_source = prepare_source
        self._poll_interval = poll_interval
        self._on_quote = on_quote
        self._on_error = on_error
        self._routes: dict[Instrument, str] = {}
        self._test_requirements: dict[str, set[Instrument]] = {}
        self._poll_tasks: dict[_PollKey, asyncio.Task[None]] = {}
        self._poll_states: dict[_PollKey, _PollState] = {}
        self._poll_locks = {channel: asyncio.Lock() for channel in self._poll_channels}
        self._poll_started: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def start(self, routes: Mapping[Instrument, str]) -> None:
        async with self._lock:
            self._routes = dict(routes)
            await self._reconcile_locked()

    async def set_route(self, instrument: Instrument, source_id: str) -> None:
        self._require_source(source_id)
        async with self._lock:
            self._routes[instrument] = source_id
            await self._reconcile_locked()

    async def replace_routes(self, routes: Mapping[Instrument, str]) -> None:
        for source_id in routes.values():
            self._require_source(source_id)
        async with self._lock:
            self._routes = dict(routes)
            await self._reconcile_locked()

    async def reconcile(self) -> None:
        async with self._lock:
            await self._reconcile_locked()

    def route_for(self, instrument: Instrument) -> str | None:
        return self._routes.get(instrument)

    def poll_refresh_interval(self, channel: str) -> float:
        count = len(self._desired_channels().get(channel, ()))
        return max(0.25, self._poll_interval(channel)) * max(1, count)

    def poll_probe(self, channel: str) -> ProviderProbe:
        """Transport health from every requested instrument, never from registration."""
        keys = [(channel, item) for item in self._desired_channels().get(channel, ())]
        states = [self._poll_states.get(key, _PollState()) for key in keys]
        successes = [state.last_success_at for state in states if state.last_success_at]
        errors = [
            f"{key[1].symbol}: {state.error}"
            for key, state in zip(keys, states, strict=True)
            if state.error
        ]
        checked = [state.checked_at for state in states if state.checked_at]
        stopped = any(key not in self._poll_tasks or self._poll_tasks[key].done() for key in keys)
        pending = any(state.checked_at is None for state in states)
        stale = any(state.last_success_at and not state.data_fresh for state in states)
        available = bool(successes) and len(errors) < len(keys) and not stopped
        return ProviderProbe(
            available=available or not keys,
            state=(
                "idle"
                if not keys
                else "stopped"
                if stopped
                else "request_failed"
                if errors
                else "connecting"
                if pending
                else "waiting_quote"
                if stale
                else "ready"
            ),
            detail=(
                errors[0]
                if errors
                else "等待首次采集"
                if pending
                else "采集连接正常; 部分品种仍是来源最后报价"
                if stale
                else None
            ),
            checked_at=max(checked) if checked else None,
            health=(
                SourceHealth.UNKNOWN
                if pending and not successes
                else SourceHealth.DEGRADED
                if (errors or stale or pending) and available
                else SourceHealth.HEALTHY
                if available or not keys
                else SourceHealth.UNAVAILABLE
            ),
            connection_active=available,
            last_success_at=max(successes) if successes else None,
        )

    def status(self) -> dict[str, object]:
        desired = self._desired_channels()
        return {
            "routes": {
                instrument.symbol: source_id
                for instrument, source_id in sorted(
                    self._routes.items(),
                    key=lambda item: item[0].symbol,
                )
            },
            "active_channels": {
                channel: tuple(sorted(instrument.symbol for instrument in instruments))
                for channel, instruments in sorted(desired.items())
            },
            "poll_tasks": tuple(
                sorted(
                    f"{source_id}:{instrument.symbol}" for source_id, instrument in self._poll_tasks
                )
            ),
        }

    async def sample_source(
        self,
        source_id: str,
        instrument: Instrument,
    ) -> Mapping[str, QuoteSnapshot]:
        channels = self._require_source(source_id)
        if not self._source_enabled(source_id):
            raise ValueError(f"{source_id} is disabled")
        async with self._lock:
            for channel in channels:
                if channel in self._push_channels:
                    self._test_requirements.setdefault(channel, set()).add(instrument)
            await self._reconcile_locked()
        try:
            results: dict[str, QuoteSnapshot] = {}
            for channel in channels:
                await self._prepare_source(channel)
                provider = self._push_channels.get(channel) or self._poll_channels.get(channel)
                if provider is None:
                    raise ValueError(f"{channel} has no acquisition provider")
                quote = await provider.get_quote(instrument)
                results[channel] = quote
                self._on_quote(quote)
                if channel in self._poll_channels:
                    self._record_poll_quote(channel, instrument, quote)
            return results
        finally:
            async with self._lock:
                for channel in channels:
                    if channel not in self._push_channels:
                        continue
                    instruments = self._test_requirements.get(channel)
                    if instruments is None:
                        continue
                    instruments.discard(instrument)
                    if not instruments:
                        self._test_requirements.pop(channel, None)
                await self._reconcile_locked()

    async def stop(self) -> None:
        async with self._lock:
            tasks = tuple(self._poll_tasks.values())
            self._poll_tasks.clear()
            self._poll_states.clear()
            self._poll_started.clear()
            self._routes.clear()
            self._test_requirements.clear()
            for provider in self._push_channels.values():
                await provider.set_subscriptions(())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _require_source(self, source_id: str) -> tuple[str, ...]:
        try:
            return self._source_channels[source_id]
        except KeyError as error:
            raise ValueError(f"unknown acquisition source {source_id!r}") from error

    def _desired_channels(self) -> dict[str, set[Instrument]]:
        desired: dict[str, set[Instrument]] = {}
        for instrument, source_id in self._routes.items():
            if not self._source_enabled(source_id):
                continue
            for channel in self._source_channels.get(source_id, ()):
                desired.setdefault(channel, set()).add(instrument)
        for channel, instruments in self._test_requirements.items():
            desired.setdefault(channel, set()).update(instruments)
        return desired

    async def _reconcile_locked(self) -> None:
        desired = self._desired_channels()
        for channel, provider in self._push_channels.items():
            instruments = tuple(sorted(desired.get(channel, ()), key=lambda item: item.symbol))
            await provider.set_subscriptions(instruments)

        wanted_poll_keys = {
            (channel, instrument)
            for channel, instruments in desired.items()
            if channel in self._poll_channels
            for instrument in instruments
        }
        for key, task in tuple(self._poll_tasks.items()):
            if key in wanted_poll_keys:
                continue
            task.cancel()
            self._poll_tasks.pop(key, None)
            self._poll_states.pop(key, None)
        for key in sorted(wanted_poll_keys, key=lambda key: (key[0], key[1].symbol)):
            if key in self._poll_tasks and not self._poll_tasks[key].done():
                continue
            channel, instrument = key
            self._poll_tasks[key] = asyncio.create_task(
                self._poll(channel, instrument),
                name=f"quote-acquisition:{channel}:{instrument.symbol}",
            )

    async def _poll(self, source_id: str, instrument: Instrument) -> None:
        provider = self._poll_channels[source_id]
        loop = asyncio.get_running_loop()
        state = self._poll_states.setdefault((source_id, instrument), _PollState())
        failures = 0
        while True:
            # One rate gate per upstream spreads startup and retry traffic. A slow
            # request doesn't hold the gate or block other instruments.
            async with self._poll_locks[source_id]:
                interval = max(0.25, self._poll_interval(source_id))
                previous = self._poll_started.get(source_id, loop.time() - interval)
                await asyncio.sleep(max(0.0, previous + interval - loop.time()))
                self._poll_started[source_id] = loop.time()
            started_at = loop.time()
            try:
                await self._prepare_source(source_id)
                quote = await provider.get_quote(instrument)
                self._on_quote(quote)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures = min(failures + 1, 6)
                state.error = str(error).replace("\n", " ")[:240]
                with suppress(Exception):
                    self._on_error(instrument, source_id, error)
            else:
                failures = 0
                self._record_poll_quote(source_id, instrument, quote)
            state.checked_at = datetime.now(UTC)
            interval = self.poll_refresh_interval(source_id)
            if failures:
                interval = max(interval, min(60, 2**failures))
            elapsed = loop.time() - started_at
            await asyncio.sleep(max(0.0, interval - elapsed))

    def _record_poll_quote(
        self, channel: str, instrument: Instrument, quote: QuoteSnapshot
    ) -> None:
        state = self._poll_states.setdefault((channel, instrument), _PollState())
        state.checked_at = state.last_success_at = datetime.now(UTC)
        state.error = None
        state.data_fresh = quote.source.is_fresh(
            state.checked_at, max(30, self.poll_refresh_interval(channel) * 2)
        )
