from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tracefang import service, windows_service


class WindowsAdapterTests(unittest.TestCase):
    def test_request_paths_are_data_not_powershell_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TraceFang space '") as directory:
            root = Path(directory)
            executable = root / ".venv" / "Scripts" / "pythonw.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.object(windows_service.subprocess, "run") as run:
                run.return_value = SimpleNamespace(stdout='{"running":true}')
                self.assertEqual(
                    windows_service.task_operation("install", project_root=root), {"running": True}
                )
            command = run.call_args.args[0]
            script = base64.b64decode(command[-1]).decode("utf-16-le")
            self.assertNotIn(str(root), script)
            payload = json.loads(run.call_args.kwargs["env"]["TRACEFANG_TASK_REQUEST"])
            self.assertEqual(payload, {"action": "install", "root": str(root)})
            self.assertTrue(run.call_args.kwargs["check"])

    def test_malformed_task_response_is_not_reported_as_success(self) -> None:
        with patch.object(windows_service.subprocess, "run") as run:
            run.return_value = SimpleNamespace(stdout='{"unexpected":true}')
            with self.assertRaises(OSError):
                windows_service.task_operation("status")

    def test_windows_missing_task_is_not_loaded(self) -> None:
        with (
            patch.object(service, "IS_WINDOWS", True),
            patch.object(
                windows_service, "task_operation", return_value={"running": False}
            ) as operation,
        ):
            self.assertFalse(service.service_is_loaded())
        operation.assert_called_once_with("status")

    def test_windows_registration_reuses_the_shared_runtime_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(service, "IS_WINDOWS", True),
                patch.object(service, "SERVICE_REGISTRATION", root / "service.plist"),
                patch.object(service, "LOG_DIRECTORY", root / "logs"),
                patch.object(service, "virtualenv_python", return_value=root / "python.exe"),
                patch.object(windows_service, "task_operation") as operation,
            ):
                service.register_service(root)
                self.assertEqual(service.installed_runtime(), root)
            operation.assert_called_once_with("install", project_root=root)

    def test_windows_stop_uses_system_task_not_launchctl(self) -> None:
        with (
            patch.object(service, "IS_WINDOWS", True),
            patch.object(windows_service, "task_operation") as operation,
        ):
            service.stop_backend()
        operation.assert_called_once_with("stop")
