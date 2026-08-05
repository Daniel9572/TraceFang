import json
import unittest

import httpx

from market_analysis.infrastructure.mcp.client import (
    StreamableHttpMcpClient,
    _decode_sse_messages,
)
from market_analysis.infrastructure.mcp.errors import McpStructuredContentError


class McpClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.method == "DELETE":
                return httpx.Response(405)
            body = json.loads(request.content)
            method = body["method"]
            if method == "initialize":
                return httpx.Response(
                    200,
                    headers={"MCP-Session-Id": "session-123"},
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {"tools": {}, "resources": {}},
                            "serverInfo": {"name": "mock", "version": "1"},
                        },
                    },
                )
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"tools": [{"name": "get_quote"}]},
                    },
                )
            if method == "tools/call":
                structured = None if body["params"]["name"] == "missing" else {"data": {"x": 1}}
                result = {
                    "content": [{"type": "text", "text": "not-machine-input"}],
                    "isError": False,
                }
                if structured is not None:
                    result["structuredContent"] = structured
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "result": result},
                )
            raise AssertionError(f"unexpected method {method}")

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.client = StreamableHttpMcpClient(
            endpoint="https://example.test/mcp",
            bearer_token="test-token",
            http_client=self.http,
        )

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.http.aclose()

    async def test_lifecycle_and_structured_content(self) -> None:
        await self.client.initialize()
        tools = await self.client.list_tools()
        result = await self.client.call_tool("get_quote", {"code": "XAUUSD"})

        self.assertEqual(tools["tools"][0]["name"], "get_quote")
        self.assertEqual(result.structured_content, {"data": {"x": 1}})
        self.assertEqual(self.client.session_id, "session-123")
        normal_requests = [
            request
            for request in self.requests
            if request.method == "POST" and json.loads(request.content)["method"] != "initialize"
        ]
        self.assertTrue(normal_requests)
        for request in normal_requests:
            self.assertEqual(request.headers["mcp-session-id"], "session-123")
            self.assertEqual(request.headers["mcp-protocol-version"], "2025-11-25")

    async def test_rejects_text_only_tool_result(self) -> None:
        await self.client.initialize()
        with self.assertRaises(McpStructuredContentError):
            await self.client.call_tool("missing")

    def test_decodes_multiline_sse_and_ignores_empty_priming_event(self) -> None:
        payload = (
            "id: prime\n"
            "data:\n\n"
            "event: message\n"
            'data: {"jsonrpc":"2.0",\n'
            'data: "id":7,"result":{"ok":true}}\n\n'
        )
        self.assertEqual(
            _decode_sse_messages(payload),
            [{"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}],
        )


if __name__ == "__main__":
    unittest.main()
