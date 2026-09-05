"""Windows Task Scheduler adapter; orchestration remains in tracefang.service."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path


def task_operation(action: str, *, project_root: Path | None = None) -> dict[str, object]:
    if action not in {"install", "stop", "status", "uninstall"}:
        raise ValueError("Unsupported task operation")
    if action == "install" and (
        project_root is None or not (project_root / ".venv" / "Scripts" / "pythonw.exe").is_file()
    ):
        raise OSError("Windows runtime is missing pythonw.exe")
    request = {"action": action, "root": str(project_root) if project_root else None}
    script = Path(__file__).with_name("windows_task.ps1").read_text(encoding="utf-8")
    command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        env={**os.environ, "TRACEFANG_TASK_REQUEST": json.dumps(request)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=45,
    )
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload.get("running"), bool):
            raise ValueError("missing running state")
    except (ValueError, AttributeError) as error:
        raise OSError("Invalid Windows task status response") from error
    return payload
