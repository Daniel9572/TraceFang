from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from market_analysis.application.ports import CandleProvider, QuoteProvider
from market_analysis.domain.errors import (
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


class SourceAccessModel(StrEnum):
    UNMETERED = "unmetered"
    LIMITED = "limited"
    METERED = "metered"


class QuoteServiceTier(StrEnum):
    INSTITUTIONAL = "institutional"
    ENHANCED = "enhanced"
    STANDARD = "standard"
    REFERENCE = "reference"


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
Connector = Callable[[], Awaitable[ProviderProbe]]


@dataclass(frozen=True, slots=True)
class SourceQuota:
    key: str
    label: str
    used: int
    limit: int
    reserve: int
    available: int
    usage_percent: float
    warning_percent: float
    period: str
    resets_at: datetime
    scope: str


QuotaReporter = Callable[[], Awaitable[tuple[SourceQuota, ...]]]


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
    history_priority: int = 100
    structured: bool = True
    quote_poll_interval_seconds: float = 60.0
    quote_streaming: bool = False
    quote_service_tier: QuoteServiceTier = QuoteServiceTier.REFERENCE
    access_model: SourceAccessModel = SourceAccessModel.UNMETERED
    access_note: str | None = None
    manual_connection_required: bool = False
    connector: Connector | None = None
    quota_reporter: QuotaReporter | None = None
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
    structured: bool
    quote_poll_interval_seconds: float
    quote_streaming: bool
    quote_service_tier: QuoteServiceTier
    access_model: SourceAccessModel
    access_note: str | None
    manual_connection_required: bool
    connection_active: bool
    quotas: tuple[SourceQuota, ...]
    health: SourceHealth
    state: str
    error: str | None
    checked_at: datetime | None
    last_success_at: datetime | None


@dataclass(slots=True)
class _RuntimeState:
    connection_active: bool = True
    health: SourceHealth = SourceHealth.UNKNOWN
    state: str = "not_checked"
    error: str | None = None
    checked_at: datetime | None = None
    last_success_at: datetime | None = None


class MarketSourceManager:
    """Validates explicitly selected market-data sources and their capabilities."""

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
        self._runtime = {
            source_id: _RuntimeState(
                connection_active=not registration.manual_connection_required,
            )
            for source_id, registration in self._registrations.items()
        }
        self._connection_locks = {source_id: asyncio.Lock() for source_id in self._registrations}
        for source_id, registration in self._registrations.items():
            if registration.setup_error:
                self._runtime[source_id] = _RuntimeState(
                    connection_active=False,
                    health=SourceHealth.UNCONFIGURED,
                    state="setup_required",
                    error=registration.setup_error,
                    checked_at=datetime.now(UTC),
                )
            elif registration.manual_connection_required:
                self._runtime[source_id] = _RuntimeState(
                    connection_active=False,
                    health=SourceHealth.UNKNOWN,
                    state="manual_connection_required",
                )
            elif registration.probe is None:
                self._runtime[source_id] = _RuntimeState(
                    connection_active=True,
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
            quotas = (
                await registration.quota_reporter()
                if registration.quota_reporter is not None
                else ()
            )
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
                    structured=registration.structured,
                    quote_poll_interval_seconds=registration.quote_poll_interval_seconds,
                    quote_streaming=registration.quote_streaming,
                    quote_service_tier=registration.quote_service_tier,
                    access_model=registration.access_model,
                    access_note=registration.access_note,
                    manual_connection_required=registration.manual_connection_required,
                    connection_active=runtime.connection_active,
                    quotas=quotas,
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
        if (
            registration.manual_connection_required
            and not self._runtime[registration.source_id].connection_active
        ):
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
            if not enabled and self._registrations[source_id].manual_connection_required:
                runtime = self._runtime[source_id]
                runtime.connection_active = False
                runtime.health = SourceHealth.UNKNOWN
                runtime.state = "manual_connection_required"
                runtime.error = None
                runtime.checked_at = datetime.now(UTC)
        if priority is not None:
            value["priority"] = priority
        self._store.save(self._configuration)

    async def connect_source(self, source_id: str) -> None:
        registration = self._registration(source_id)
        if not bool(self._configuration[source_id]["enabled"]):
            raise ProviderUnavailableError(f"{source_id} is disabled")
        if not registration.manual_connection_required:
            await self._refresh_probe(registration)
            return
        if registration.setup_error:
            raise ProviderUnavailableError(registration.setup_error)
        state = self._runtime[source_id]
        if state.connection_active:
            return
        if registration.connector is None:
            raise ProviderUnavailableError(f"{source_id} has no connection handler")
        async with self._connection_locks[source_id]:
            if state.connection_active:
                return
            try:
                probe = await registration.connector()
            except ProviderError as error:
                self._mark_failure(source_id, error)
                raise
            if not probe.available:
                error = ProviderUnavailableError(probe.detail or f"{source_id} connection failed")
                self._mark_failure(source_id, error)
                raise error
            state.connection_active = True
            state.health = SourceHealth.HEALTHY
            state.state = probe.state
            state.error = probe.detail
            state.checked_at = probe.checked_at or datetime.now(UTC)

    def is_connected(self, source_id: str) -> bool:
        self._registration(source_id)
        return self._runtime[source_id].connection_active

    async def get_quote(
        self,
        instrument: Instrument,
        *,
        source: str,
    ) -> QuoteSnapshot:
        registration = self._selected(SourceCapability.QUOTE, source)
        provider = registration.quote_provider
        if provider is None:
            error = ProviderUnavailableError(
                registration.setup_error or f"{registration.source_id} is unavailable"
            )
            self._mark_failure(registration.source_id, error)
            raise error
        try:
            value = await provider.get_quote(instrument)
        except ProviderError as error:
            self._mark_failure(registration.source_id, error)
            raise
        self._mark_success(registration.source_id)
        return value

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        source: str,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        registration = self._selected(SourceCapability.CANDLES, source)
        provider = registration.candle_provider
        if provider is None:
            error = ProviderUnavailableError(
                registration.setup_error or f"{registration.source_id} is unavailable"
            )
            self._mark_failure(registration.source_id, error)
            raise error
        try:
            value = await provider.get_candles(instrument, start=start, count=count)
        except ProviderError as error:
            self._mark_failure(registration.source_id, error)
            raise
        self._mark_success(registration.source_id)
        return value

    def quote_poll_interval(self, source: str) -> float:
        registration = self._selected(SourceCapability.QUOTE, source)
        return registration.quote_poll_interval_seconds

    def quote_is_streaming(self, source: str) -> bool:
        registration = self._selected(SourceCapability.QUOTE, source)
        return registration.quote_streaming

    def history_source_priority(self) -> tuple[str, ...]:
        """Returns the global canonical-history precedence, independent of live routes."""

        registrations = (
            item
            for item in self._registrations.values()
            if SourceCapability.CANDLES in item.capabilities
        )
        return tuple(
            item.source_id
            for item in sorted(
                registrations,
                key=lambda item: (item.history_priority, item.source_id),
            )
        )

    def history_quote_derived_sources(self) -> tuple[str, ...]:
        """Returns structured push feeds whose quote events can form minute OHLC rows."""

        registrations = (
            item
            for item in self._registrations.values()
            if SourceCapability.QUOTE in item.capabilities
            and item.quote_streaming
            and item.structured
        )
        return tuple(
            item.source_id
            for item in sorted(
                registrations,
                key=lambda item: (item.history_priority, item.source_id),
            )
        )

    def history_backfill_sources(self) -> tuple[str, ...]:
        """Returns usable history providers with free channels ahead of limited ones."""

        access_rank = {
            SourceAccessModel.UNMETERED: 0,
            SourceAccessModel.LIMITED: 1,
            SourceAccessModel.METERED: 2,
        }
        registrations = []
        for source_id, registration in self._registrations.items():
            if SourceCapability.CANDLES not in registration.capabilities:
                continue
            if not bool(self._configuration[source_id]["enabled"]):
                continue
            if registration.candle_provider is None or registration.setup_error:
                continue
            if (
                registration.manual_connection_required
                and not self._runtime[source_id].connection_active
            ):
                continue
            registrations.append(registration)
        registrations.sort(
            key=lambda item: (
                access_rank[item.access_model],
                int(self._configuration[item.source_id]["priority"]),
                item.history_priority,
                item.source_id,
            )
        )
        return tuple(item.source_id for item in registrations)

    def validate_source(self, capability: SourceCapability, source: str) -> None:
        self._selected(capability, source)

    def _selected(self, capability: SourceCapability, source: str) -> SourceRegistration:
        if source == "auto":
            raise ValueError("automatic source fallback is disabled; select a concrete source")
        registration = self._registration(source)
        if capability not in registration.capabilities:
            raise ProviderUnavailableError(f"{source} does not provide {capability.value} data")
        if not bool(self._configuration[source]["enabled"]):
            raise ProviderUnavailableError(f"{source} is disabled")
        if registration.manual_connection_required and not self._runtime[source].connection_active:
            raise ProviderUnavailableError(
                f"{registration.display_name} 尚未连接, 请先在实时来源菜单中点击连接并测试"
            )
        return registration

    def _registration(self, source_id: str) -> SourceRegistration:
        try:
            return self._registrations[source_id]
        except KeyError as error:
            raise ValueError(f"unknown source {source_id!r}") from error

    def _mark_success(self, source_id: str) -> None:
        now = datetime.now(UTC)
        state = self._runtime[source_id]
        state.connection_active = True
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
