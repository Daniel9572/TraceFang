from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import suppress
from itertools import count
from typing import Any

import httpx

from tracefang.infrastructure.mcp.errors import (
    McpProtocolError,
    McpSessionExpiredError,
    McpStructuredContentError,
    McpToolError,
    McpTransportError,
)
from tracefang.infrastructure.mcp.types import ToolCallResult


def _decode_sse_messages(payload: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def flush() -> None:
        if not data_lines:
            return
        raw = "\n".join(data_lines).strip()
        data_lines.clear()
        if not raw:
            return
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            messages.append(decoded)

    for line in payload.splitlines():
        if not line:
            flush()
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
    flush()
    return messages


def _readable_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    texts = [
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return "\n".join(text for text in texts if isinstance(text, str))


class StreamableHttpMcpClient:
    """Minimal standards-compliant MCP 2025-11-25 Streamable HTTP client."""

    def __init__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        client_name: str = "tracefang",
        client_version: str = "0.1.0",
        protocol_version: str = "2025-11-25",
        timeout_seconds: float = 20.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("remote MCP endpoint must use HTTPS")
        if not bearer_token.strip():
            raise ValueError("bearer_token cannot be empty")
        self.endpoint = endpoint
        self.protocol_version = protocol_version
        self.client_name = client_name
        self.client_version = client_version
        self._bearer_token = bearer_token
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_http_client = http_client is None
        self._request_ids = count(1)
        self._session_id: str | None = None
        self._negotiated_version: str | None = None
        self._initialized = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def negotiated_version(self) -> str | None:
        return self._negotiated_version

    async def __aenter__(self) -> StreamableHttpMcpClient:
        await self.initialize()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _headers(self, *, initialized: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._bearer_token}",
            "Content-Type": "application/json",
        }
        if initialized:
            version = self._negotiated_version or self.protocol_version
            headers["MCP-Protocol-Version"] = version
            if self._session_id:
                headers["MCP-Session-Id"] = self._session_id
        return headers

    async def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return {
                "protocolVersion": self._negotiated_version,
                "sessionId": self._session_id,
            }
        response = await self._request_once(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
            initialized=False,
        )
        negotiated = response.get("protocolVersion")
        if not isinstance(negotiated, str):
            raise McpTransportError("initialize result omitted protocolVersion")
        if negotiated != self.protocol_version:
            raise McpTransportError(
                f"server negotiated unsupported protocol version {negotiated!r}"
            )
        self._negotiated_version = negotiated
        await self._notify("notifications/initialized")
        self._initialized = True
        return response

    async def _restart_session(self) -> None:
        self._session_id = None
        self._negotiated_version = None
        self._initialized = False
        await self.initialize()

    async def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            raise McpTransportError("MCP client must be initialized before normal requests")
        try:
            return await self._request_once(method, params, initialized=True)
        except McpSessionExpiredError:
            await self._restart_session()
            return await self._request_once(method, params, initialized=True)

    async def _request_once(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        initialized: bool,
    ) -> dict[str, Any]:
        request_id = next(self._request_ids)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        response = await self._post(payload, initialized=initialized)
        if not initialized:
            session_id = response.headers.get("MCP-Session-Id")
            if session_id:
                self._session_id = session_id
        message = self._decode_response(response, request_id=request_id)
        if "error" in message:
            error = message["error"]
            if not isinstance(error, dict):
                raise McpTransportError("invalid JSON-RPC error object")
            raise McpProtocolError(
                int(error.get("code", -32603)),
                str(error.get("message", "unknown error")),
                error.get("data"),
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpTransportError("JSON-RPC response omitted object result")
        return result

    async def _notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        response = await self._post(payload, initialized=True)
        if response.status_code not in (200, 202, 204):
            raise McpTransportError(
                f"MCP notification returned HTTP {response.status_code}",
                status_code=response.status_code,
            )

    async def _post(self, payload: Mapping[str, Any], *, initialized: bool) -> httpx.Response:
        try:
            response = await self._http.post(
                self.endpoint,
                headers=self._headers(initialized=initialized),
                json=dict(payload),
            )
        except httpx.HTTPError as error:
            raise McpTransportError(f"MCP transport failure: {error}") from error
        if response.status_code == 404 and initialized and self._session_id:
            raise McpSessionExpiredError("MCP session expired", status_code=404)
        if response.is_error:
            raise McpTransportError(
                f"MCP endpoint returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response

    @staticmethod
    def _decode_response(response: httpx.Response, *, request_id: int) -> dict[str, Any]:
        content_type = response.headers.get("Content-Type", "").lower()
        try:
            if content_type.startswith("text/event-stream"):
                messages = _decode_sse_messages(response.text)
            else:
                decoded = response.json()
                messages = [decoded] if isinstance(decoded, dict) else []
        except (json.JSONDecodeError, ValueError) as error:
            raise McpTransportError("MCP endpoint returned invalid JSON") from error
        for message in messages:
            if message.get("id") == request_id:
                return message
        raise McpTransportError(f"MCP response for request id {request_id} was not received")

    async def list_tools(self, cursor: str | None = None) -> dict[str, Any]:
        params = {"cursor": cursor} if cursor else {}
        return await self._request("tools/list", params)

    async def list_resources(self, cursor: str | None = None) -> dict[str, Any]:
        params = {"cursor": cursor} if cursor else {}
        return await self._request("resources/list", params)

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return await self._request("resources/read", {"uri": uri})

    async def read_json_resource(self, uri: str) -> dict[str, Any]:
        result = await self.read_resource(uri)
        contents = result.get("contents")
        if not isinstance(contents, list):
            raise McpTransportError(f"resource {uri!r} omitted contents")
        for content in contents:
            if not isinstance(content, dict) or not isinstance(content.get("text"), str):
                continue
            try:
                decoded = json.loads(content["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
        raise McpTransportError(f"resource {uri!r} did not contain a JSON object")

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> ToolCallResult:
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
        )
        content = result.get("content")
        readable_content = _readable_text(content)
        if result.get("isError") is True:
            raise McpToolError(name, readable_content)
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise McpStructuredContentError(name)
        normalized_content = tuple(item for item in content or [] if isinstance(item, dict))
        meta = result.get("_meta")
        return ToolCallResult(
            structured_content=structured,
            content=normalized_content,
            meta=meta if isinstance(meta, dict) else None,
        )

    async def close(self) -> None:
        if self._session_id:
            with suppress(httpx.HTTPError):
                await self._http.delete(
                    self.endpoint,
                    headers=self._headers(initialized=True),
                )
        self._session_id = None
        self._initialized = False
        if self._owns_http_client:
            await self._http.aclose()
