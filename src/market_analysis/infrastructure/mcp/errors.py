from __future__ import annotations

from typing import Any


class McpError(RuntimeError):
    """Base class for MCP client failures."""


class McpTransportError(McpError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class McpSessionExpiredError(McpTransportError):
    """The server rejected a previously issued MCP session ID."""


class McpProtocolError(McpError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(f"MCP JSON-RPC error {code}: {message}")


class McpToolError(McpError):
    def __init__(self, tool_name: str, readable_content: str) -> None:
        self.tool_name = tool_name
        self.readable_content = readable_content
        detail = readable_content or "tool returned isError=true"
        super().__init__(f"MCP tool {tool_name!r} failed: {detail}")


class McpStructuredContentError(McpError):
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"MCP tool {tool_name!r} did not return structuredContent")
