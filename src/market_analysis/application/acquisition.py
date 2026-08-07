from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from market_analysis.domain.models import Instrument, QuoteSnapshot


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

    async def reconcile(self) -> None:
        async with self._lock:
            await self._reconcile_locked()

    def route_for(self, instrument: Instrument) -> str | None:
        return self._routes.get(instrument)

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
        for key in wanted_poll_keys:
            if key in self._poll_tasks:
                continue
            channel, instrument = key
            self._poll_tasks[key] = asyncio.create_task(
                self._poll(channel, instrument),
                name=f"quote-acquisition:{channel}:{instrument.symbol}",
            )

    async def _poll(self, source_id: str, instrument: Instrument) -> None:
        provider = self._poll_channels[source_id]
        loop = asyncio.get_running_loop()
        while True:
            started_at = loop.time()
            try:
                await self._prepare_source(source_id)
                quote = await provider.get_quote(instrument)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._on_error(instrument, source_id, error)
            else:
                self._on_quote(quote)
            active_instruments = max(
                1,
                sum(1 for channel, _ in self._poll_tasks if channel == source_id),
            )
            interval = max(
                0.25,
                self._poll_interval(source_id) * active_instruments,
            )
            elapsed = loop.time() - started_at
            await asyncio.sleep(max(0.0, interval - elapsed))
