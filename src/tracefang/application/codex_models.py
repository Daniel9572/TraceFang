"""Read the installed Codex model catalog without creating a thread or running inference."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CodexModel:
    model: str
    display_name: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str
    is_default: bool


def parse_model(value: object) -> CodexModel | None:
    if not isinstance(value, dict) or value.get("hidden"):
        return None
    model = value.get("model")
    options = value.get("supportedReasoningEfforts")
    if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][\w./:-]{0,127}", model):
        return None
    if not isinstance(options, list):
        return None
    efforts = tuple(
        dict.fromkeys(
            option["reasoningEffort"]
            for option in options
            if isinstance(option, dict)
            and isinstance(option.get("reasoningEffort"), str)
            and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", option["reasoningEffort"])
        )
    )
    if not efforts:
        return None
    default = value.get("defaultReasoningEffort")
    name = value.get("displayName")
    return CodexModel(
        model=model,
        display_name=name if isinstance(name, str) and name else model,
        reasoning_efforts=efforts,
        default_reasoning_effort=default if default in efforts else efforts[0],
        is_default=value.get("isDefault") is True,
    )


async def read_codex_models(command: str, environment: Mapping[str, str]) -> tuple[CodexModel, ...]:
    # Use the same OpenAI provider as the isolated analysis invocation. No thread/start
    # or turn/start is sent, and the pipe is private to this short-lived child process.
    with tempfile.TemporaryDirectory(prefix="tracefang-models-") as directory:
        process = await asyncio.create_subprocess_exec(
            command,
            "app-server",
            "-c",
            'model_provider="openai"',
            cwd=directory,
            env=dict(environment),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=2 * 1024 * 1024,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdin is not None and process.stdout is not None

        async def send(message: dict[str, object]) -> None:
            assert process.stdin is not None
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()

        async def response(request_id: int) -> dict[str, object]:
            assert process.stdout is not None
            while line := await process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise ValueError("Codex catalog protocol error")
                return message["result"]
            raise ValueError("Codex catalog connection closed")

        try:
            async with asyncio.timeout(20):
                await send(
                    {
                        "id": 0,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "tracefang", "version": "0.1.0"},
                        },
                    }
                )
                await response(0)
                await send({"method": "initialized", "params": {}})
                models: dict[str, CodexModel] = {}
                cursor: str | None = None
                cursors: set[str] = set()
                for request_id in range(1, 21):
                    await send(
                        {
                            "id": request_id,
                            "method": "model/list",
                            "params": {
                                "limit": 100,
                                "includeHidden": False,
                                "cursor": cursor,
                            },
                        }
                    )
                    page = await response(request_id)
                    data = page.get("data")
                    if not isinstance(data, list):
                        raise ValueError("Invalid Codex model catalog")
                    for entry in data:
                        if model := parse_model(entry):
                            models[model.model] = model
                    next_cursor = page.get("nextCursor")
                    if next_cursor is None:
                        if not models:
                            raise ValueError("Empty Codex model catalog")
                        return tuple(models.values())
                    if not isinstance(next_cursor, str) or next_cursor in cursors:
                        raise ValueError("Codex model cursor did not advance")
                    cursors.add(next_cursor)
                    cursor = next_cursor
                raise ValueError("Codex model catalog exceeded page limit")
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
