from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from market_analysis.application.ports import CandleProvider, QuoteProvider
from market_analysis.domain.errors import (
    ProviderChainExhaustedError,
    ProviderError,
    ProviderUnavailableError,
)
from market_analysis.domain.models import Candle, Instrument, QuoteSnapshot


class SourceCapability(StrEnum):
    QUOTE = "quote"
    CANDLES = "candles"
    CATALOG = "catalog"
    NEWS = "news"
    CALENDAR = "calendar"


class SourceHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNCONFIGURED = "unconfigured"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderProbe:
    available: bool
    state: str
    detail: str | None = None
    checked_at: datetime | None = None


class SourceConfigurationStore(Protocol):
    def load(self) -> dict[str, dict[str, int | bool]]: ...

    def save(self, values: dict[str, dict[str, int | bool]]) -> None: ...


Probe = Callable[[], Awaitable[ProviderProbe]]


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    source_id: str
    display_name: str
    description: str
    capabilities: frozenset[SourceCapability]
    default_enabled: bool
    default_priority: int
    delayed: bool
    requires_running_app: bool
    quote_provider: QuoteProvider | None = None
    candle_provider: CandleProvider | None = None
    probe: Probe | None = None
    setup_error: str | None = None


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    source_id: str
    display_name: str
    description: str
    capabilities: tuple[str, ...]
    enabled: bool
    priority: int
    delayed: bool
    requires_running_app: bool
    health: SourceHealth
    state: str
    error: str | None
    checked_at: datetime | None
    last_success_at: datetime | None


@dataclass(slots=True)
class _RuntimeState:
    health: SourceHealth = SourceHealth.UNKNOWN
    state: str = "not_checked"
    error: str | None = None
    checked_at: datetime | None = None
    last_success_at: datetime | None = None


class MarketSourceManager:
    """Selects providers by capability, persisted priority, and explicit overrides."""

    def __init__(
        self,
        registrations: Sequence[SourceRegistration],
        *,
        store: SourceConfigurationStore,
    ) -> None:
        if not registrations:
            raise ValueError("at least one data source registration is required")
        self._registrations = {item.source_id: item for item in registrations}
        if len(self._registrations) != len(registrations):
            raise ValueError("source ids must be unique")
        self._store = store
        self._configuration = self._merged_configuration(store.load())
        self._runtime = {source_id: _RuntimeState() for source_id in self._registrations}
        for source_id, registration in self._registrations.items():
            if registration.setup_error:
                self._runtime[source_id] = _RuntimeState(
                    health=SourceHealth.UNCONFIGURED,
                    state="setup_required",
                    error=registration.setup_error,
                    checked_at=datetime.now(UTC),
                )
            elif registration.probe is None:
                self._runtime[source_id] = _RuntimeState(
                    health=SourceHealth.HEALTHY,
                    state="ready",
                    checked_at=datetime.now(UTC),
                )

    def _merged_configuration(
        self, stored: dict[str, dict[str, int | bool]]
    ) -> dict[str, dict[str, int | bool]]:
        result: dict[str, dict[str, int | bool]] = {}
        for source_id, registration in self._registrations.items():
            configured = stored.get(source_id, {})
            result[source_id] = {
                "enabled": bool(configured.get("enabled", registration.default_enabled)),
                "priority": int(configured.get("priority", registration.default_priority)),
            }
        return result

    async def list_sources(self, *, refresh: bool = False) -> tuple[SourceDescriptor, ...]:
        if refresh:
            await asyncio.gather(
                *(self._refresh_probe(item) for item in self._registrations.values())
            )
        rows: list[SourceDescriptor] = []
        for source_id, registration in self._registrations.items():
            configured = self._configuration[source_id]
            runtime = self._runtime[source_id]
            rows.append(
                SourceDescriptor(
                    source_id=source_id,
                    display_name=registration.display_name,
                    description=registration.description,
                    capabilities=tuple(sorted(item.value for item in registration.capabilities)),
                    enabled=bool(configured["enabled"]),
                    priority=int(configured["priority"]),
                    delayed=registration.delayed,
                    requires_running_app=registration.requires_running_app,
                    health=runtime.health,
                    state=runtime.state,
                    error=runtime.error,
                    checked_at=runtime.checked_at,
                    last_success_at=runtime.last_success_at,
                )
            )
        rows.sort(key=lambda item: (item.priority, item.source_id))
        return tuple(rows)

    async def _refresh_probe(self, registration: SourceRegistration) -> None:
        if registration.probe is None or registration.setup_error:
            return
        try:
            probe = await registration.probe()
        except ProviderError as error:
            self._mark_failure(registration.source_id, error)
            return
        state = self._runtime[registration.source_id]
        state.health = SourceHealth.HEALTHY if probe.available else SourceHealth.UNAVAILABLE
        state.state = probe.state
        state.error = probe.detail
        state.checked_at = probe.checked_at or datetime.now(UTC)

    def configure(
        self,
        source_id: str,
        *,
        enabled: bool | None = None,
        priority: int | None = None,
    ) -> None:
        self._registration(source_id)
        if priority is not None and not 0 <= priority <= 1000:
            raise ValueError("priority must be between 0 and 1000")
        value = self._configuration[source_id]
        if enabled is not None:
            value["enabled"] = enabled
        if priority is not None:
            value["priority"] = priority
        self._store.save(self._configuration)

    async def get_quote(
        self,
        instrument: Instrument,
        *,
        source: str = "auto",
    ) -> QuoteSnapshot:
        failures: list[tuple[str, ProviderError]] = []
        for registration in self._candidates(SourceCapability.QUOTE, source):
            provider = registration.quote_provider
            if provider is None:
                error = ProviderUnavailableError(
                    registration.setup_error or f"{registration.source_id} is unavailable"
                )
                failures.append((registration.source_id, error))
                self._mark_failure(registration.source_id, error)
                continue
            try:
                value = await provider.get_quote(instrument)
            except ProviderError as error:
                failures.append((registration.source_id, error))
                self._mark_failure(registration.source_id, error)
                if source != "auto":
                    raise
            else:
                self._mark_success(registration.source_id)
                return value
        raise ProviderChainExhaustedError("quote", failures)

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        source: str = "auto",
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        failures: list[tuple[str, ProviderError]] = []
        for registration in self._candidates(SourceCapability.CANDLES, source):
            provider = registration.candle_provider
            if provider is None:
                error = ProviderUnavailableError(
                    registration.setup_error or f"{registration.source_id} is unavailable"
                )
                failures.append((registration.source_id, error))
                self._mark_failure(registration.source_id, error)
                continue
            try:
                value = await provider.get_candles(instrument, start=start, count=count)
            except ProviderError as error:
                failures.append((registration.source_id, error))
                self._mark_failure(registration.source_id, error)
                if source != "auto":
                    raise
            else:
                self._mark_success(registration.source_id)
                return value
        raise ProviderChainExhaustedError("candle", failures)

    def _candidates(
        self, capability: SourceCapability, source: str
    ) -> tuple[SourceRegistration, ...]:
        if source != "auto":
            registration = self._registration(source)
            if capability not in registration.capabilities:
                raise ProviderUnavailableError(f"{source} does not provide {capability.value} data")
            if not bool(self._configuration[source]["enabled"]):
                raise ProviderUnavailableError(f"{source} is disabled")
            return (registration,)
        rows = [
            item
            for item in self._registrations.values()
            if capability in item.capabilities
            and bool(self._configuration[item.source_id]["enabled"])
        ]
        rows.sort(
            key=lambda item: (int(self._configuration[item.source_id]["priority"]), item.source_id)
        )
        return tuple(rows)

    def _registration(self, source_id: str) -> SourceRegistration:
        try:
            return self._registrations[source_id]
        except KeyError as error:
            raise ValueError(f"unknown source {source_id!r}") from error

    def _mark_success(self, source_id: str) -> None:
        now = datetime.now(UTC)
        state = self._runtime[source_id]
        state.health = SourceHealth.HEALTHY
        state.state = "ready"
        state.error = None
        state.checked_at = now
        state.last_success_at = now

    def _mark_failure(self, source_id: str, error: ProviderError) -> None:
        state = self._runtime[source_id]
        if self._registrations[source_id].setup_error:
            state.health = SourceHealth.UNCONFIGURED
            state.state = "setup_required"
            state.error = self._registrations[source_id].setup_error
            state.checked_at = datetime.now(UTC)
            return
        state.health = SourceHealth.UNAVAILABLE
        state.state = "request_failed"
        state.error = str(error)
        state.checked_at = datetime.now(UTC)
