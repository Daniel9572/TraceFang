from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar

from market_analysis.application.sources import ProviderProbe
from market_analysis.domain.errors import (
    InstrumentNotSupportedError,
    ProviderDataError,
    ProviderUnavailableError,
)
from market_analysis.domain.models import Instrument, QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.providers.jin10.symbols import Jin10SymbolMapper


@dataclass(frozen=True, slots=True)
class _DesktopCapture:
    symbol: str
    raw_price: str
    captured_at: datetime


Runner = Callable[[str | None, bool], dict[str, Any]]


class Jin10DesktopProvider:
    """Reads visible Jin10 desktop quotes through PrintWindow and Windows OCR.

    This adapter intentionally does not inspect private network traffic or infer OHLC
    candles from pixels. The first layout profile covers the gold and silver rows in
    Jin10's desktop market watch.
    """

    name = "jin10_desktop"
    _decimal_places: ClassVar[dict[str, int]] = {"XAUUSD": 2, "XAGUSD": 3}
    _reasonable_ranges: ClassVar[dict[str, tuple[Decimal, Decimal]]] = {
        "XAUUSD": (Decimal("100"), Decimal("10000")),
        "XAGUSD": (Decimal("1"), Decimal("1000")),
    }

    def __init__(
        self,
        *,
        symbol_mapper: Jin10SymbolMapper | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.symbol_mapper = symbol_mapper or Jin10SymbolMapper()
        self._runner = runner or self._run_powershell

    async def probe(self) -> ProviderProbe:
        if sys.platform != "win32":
            return ProviderProbe(
                available=False,
                state="unsupported_platform",
                detail="Jin10 desktop capture is available on Windows only",
                checked_at=datetime.now(UTC),
            )
        payload = await asyncio.to_thread(self._runner, None, True)
        return ProviderProbe(
            available=bool(payload.get("available")),
            state=str(payload.get("state", "unknown")),
            detail=str(payload["error"]) if payload.get("error") else None,
            checked_at=self._timestamp(payload.get("checked_at"), "checked_at"),
        )

    async def get_quote(self, instrument: Instrument) -> QuoteSnapshot:
        code = self.symbol_mapper.to_provider_code(instrument)
        if code not in self._decimal_places:
            raise InstrumentNotSupportedError(
                f"jin10 desktop layout has no quote region for {code}"
            )
        if sys.platform != "win32":
            raise ProviderUnavailableError("jin10 desktop capture requires Windows")
        for attempt in range(2):
            payload = await asyncio.to_thread(self._runner, code, False)
            if not payload.get("success"):
                raise ProviderUnavailableError(
                    str(payload.get("error") or "jin10 desktop capture failed")
                )
            try:
                return self._normalize_quote(instrument, code, payload)
            except ProviderDataError:
                if attempt == 1:
                    raise
        raise AssertionError("desktop quote retry loop ended unexpectedly")

    def _normalize_quote(
        self,
        instrument: Instrument,
        code: str,
        payload: dict[str, Any],
    ) -> QuoteSnapshot:
        capture = _DesktopCapture(
            symbol=code,
            raw_price=str(payload.get("raw_price", "")),
            captured_at=self._timestamp(payload.get("captured_at"), "captured_at"),
        )
        last = self.parse_price(
            capture.raw_price,
            decimal_places=self._decimal_places[code],
        )
        lower, upper = self._reasonable_ranges[code]
        if not lower <= last <= upper:
            raise ProviderDataError(
                f"jin10 desktop OCR price {last} is outside the safety range for {code}"
            )
        return QuoteSnapshot(
            instrument=instrument,
            last=last,
            open=None,
            high=None,
            low=None,
            volume=None,
            change=None,
            change_percent=None,
            source=SourceMetadata(
                provider=self.name,
                provider_symbol=code,
                observed_at=capture.captured_at,
                received_at=datetime.now(UTC),
            ),
        )

    @staticmethod
    def parse_price(raw: str, *, decimal_places: int) -> Decimal:
        normalized = raw.translate(
            str.maketrans(
                {
                    "\uff0e": ".",
                    "\u00b7": ".",
                    "\u2022": ".",
                    "\u3002": ".",
                    "\uff0c": ",",
                    "\u2212": "-",
                    "\u2014": "-",
                }
            )
        )
        explicit = re.search(r"(\d{1,7})\s*\.\s*(\d{1,6})", normalized)
        if explicit:
            candidate = f"{explicit.group(1)}.{explicit.group(2)}"
        else:
            tokens = re.findall(r"\d+", normalized)
            if len(tokens) == 1 and len(tokens[0]) > decimal_places:
                candidate = f"{tokens[0][:-decimal_places]}.{tokens[0][-decimal_places:]}"
            elif len(tokens) >= 2 and len(tokens[1]) == decimal_places:
                candidate = f"{tokens[0]}.{tokens[1]}"
            else:
                raise ProviderDataError(f"cannot parse desktop OCR price from {raw!r}")
        try:
            value = Decimal(candidate)
        except InvalidOperation as error:
            raise ProviderDataError(f"invalid desktop OCR price {candidate!r}") from error
        if not value.is_finite():
            raise ProviderDataError("desktop OCR price is not finite")
        return value

    @staticmethod
    def _timestamp(value: Any, field: str) -> datetime:
        if not isinstance(value, str):
            if field == "checked_at":
                return datetime.now(UTC)
            raise ProviderDataError(f"jin10 desktop {field} is missing")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProviderDataError(f"jin10 desktop {field} is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    @staticmethod
    def _script_path() -> Path:
        return Path(__file__).with_name("capture.ps1")

    def _run_powershell(self, symbol: str | None, probe_only: bool) -> dict[str, Any]:
        powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        if not powershell.exists():
            raise ProviderUnavailableError("Windows PowerShell is not available")
        command = [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self._script_path()),
        ]
        if probe_only:
            command.append("-ProbeOnly")
        else:
            command.extend(["-Symbol", symbol or ""])
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8-sig",
                errors="replace",
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderUnavailableError(f"jin10 desktop capture failed: {error}") from error
        output = completed.stdout.strip()
        if not output:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise ProviderUnavailableError(f"jin10 desktop capture returned no data: {detail}")
        try:
            payload: Any = json.loads(output.splitlines()[-1])
        except json.JSONDecodeError as error:
            raise ProviderDataError("jin10 desktop capture returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ProviderDataError("jin10 desktop capture JSON must be an object")
        return payload
