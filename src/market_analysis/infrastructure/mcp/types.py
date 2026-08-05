from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    structured_content: dict[str, Any]
    content: tuple[dict[str, Any], ...]
    meta: dict[str, Any] | None = None
