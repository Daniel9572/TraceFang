from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tracefang.application.codex_models import read_codex_models


def _entry(model: str = "test-model") -> dict[str, object]:
    return {
        "model": model,
        "displayName": model,
        "defaultReasoningEffort": "low",
        "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
    }


def _process(*messages: dict[str, object]) -> Mock:
    process = Mock(returncode=None)
    process.stdin.drain = AsyncMock()
    process.stdout.readline = AsyncMock(side_effect=[
        *(json.dumps(message).encode() + b"\n" for message in messages), b"",
    ])
    process.wait = AsyncMock(return_value=0)
    return process


class CodexModelsProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_pagination_and_cleanup_without_creating_conversations(self) -> None:
        process = _process(
            {"id": 0, "result": {}},
            {"method": "unrelated/notification"},
            {"id": 1, "result": {"data": [_entry()], "nextCursor": "page-two"}},
            {"id": 2, "result": {"data": [_entry("second-model")], "nextCursor": None}},
        )
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            models = await read_codex_models("codex", {})
        self.assertEqual([model.model for model in models], ["test-model", "second-model"])
        requests = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        self.assertEqual([request["method"] for request in requests], [
            "initialize", "initialized", "model/list", "model/list",
        ])
        self.assertEqual(requests[-1]["params"]["cursor"], "page-two")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_repeated_cursor_is_rejected_and_process_is_reaped(self) -> None:
        process = _process(
            {"id": 0, "result": {}},
            {"id": 1, "result": {"data": [_entry()], "nextCursor": "same"}},
            {"id": 2, "result": {"data": [_entry()], "nextCursor": "same"}},
        )
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            self.assertRaises(ValueError),
        ):
            await read_codex_models("codex", {})
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_protocol_error_never_returns_raw_error_text(self) -> None:
        process = _process({"id": 0, "error": {"message": "private diagnostic"}})
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            self.assertRaises(ValueError) as caught,
        ):
            await read_codex_models("codex", {})
        self.assertNotIn("private", str(caught.exception))
        process.wait.assert_awaited_once()

    async def test_connection_close_is_not_mistaken_for_an_empty_catalog(self) -> None:
        process = _process()
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            self.assertRaises(ValueError),
        ):
            await read_codex_models("codex", {})
        process.kill.assert_called_once()
