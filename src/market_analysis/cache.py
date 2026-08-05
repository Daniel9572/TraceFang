from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class _Entry(Generic[T]):
    value: T
    expires_at: float


class AsyncTtlCache(Generic[T]):
    """Small in-process cache with per-key request coalescing."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry[T]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_or_load(
        self,
        key: str,
        *,
        ttl_seconds: float,
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        now = monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            return entry.value
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = monotonic()
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > now:
                return entry.value
            value = await loader()
            self._entries[key] = _Entry(value=value, expires_at=monotonic() + ttl_seconds)
            return value

    def clear(self) -> None:
        self._entries.clear()
