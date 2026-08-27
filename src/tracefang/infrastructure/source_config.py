from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JsonSourceConfigurationStore:
    """Small atomic store for local source enablement and priority preferences."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, dict[str, int | bool]]:
        if not self._path.exists():
            return {}
        try:
            payload: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return {}
        sources = payload.get("sources")
        if not isinstance(sources, dict):
            return {}
        result: dict[str, dict[str, int | bool]] = {}
        for source_id, value in sources.items():
            if not isinstance(source_id, str) or not isinstance(value, dict):
                continue
            row: dict[str, int | bool] = {}
            if isinstance(value.get("enabled"), bool):
                row["enabled"] = value["enabled"]
            priority = value.get("priority")
            if isinstance(priority, int) and not isinstance(priority, bool):
                row["priority"] = priority
            result[source_id] = row
        return result

    def save(self, values: dict[str, dict[str, int | bool]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "sources": values},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._path)
        finally:
            temporary_path.unlink(missing_ok=True)
