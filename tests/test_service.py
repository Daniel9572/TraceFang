from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tracefang.service import (
    SERVICE_LABEL,
    launch_agent_payload,
    parse_args,
    virtualenv_python,
    web_build_required,
)


class LocalServiceTests(unittest.TestCase):
    def test_web_build_is_only_required_when_source_is_newer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            web_directory = Path(temporary_directory) / "web"
            source = web_directory / "src" / "main.tsx"
            index = web_directory / "dist" / "index.html"
            source.parent.mkdir(parents=True)
            index.parent.mkdir(parents=True)
            source.write_text("export {};", encoding="utf-8")
            index.write_text("<!doctype html>", encoding="utf-8")

            os.utime(source, ns=(100, 100))
            os.utime(index, ns=(200, 200))
            self.assertFalse(web_build_required(web_directory, index))

            os.utime(source, ns=(300, 300))
            self.assertTrue(web_build_required(web_directory, index))

    def test_missing_web_build_requires_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            web_directory = Path(temporary_directory) / "web"
            self.assertTrue(
                web_build_required(web_directory, web_directory / "dist" / "index.html")
            )

    def test_launch_agent_runs_project_service_without_terminal(self) -> None:
        project_root = Path("/tmp/TraceFang")
        log_directory = Path("/tmp/TraceFangLogs")
        python = project_root / ".venv" / "bin" / "python"
        payload = launch_agent_payload(
            python=python,
            project_root=project_root,
            log_directory=log_directory,
            environment_path="/opt/homebrew/bin:/usr/bin:/bin",
        )

        self.assertEqual(payload["Label"], SERVICE_LABEL)
        self.assertEqual(
            payload["ProgramArguments"],
            [str(python), "-m", "tracefang.service", "run"],
        )
        self.assertEqual(payload["WorkingDirectory"], str(project_root))
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        environment = payload["EnvironmentVariables"]
        self.assertEqual(environment["PYTHONPATH"], str(project_root / "src"))
        self.assertEqual(payload["StandardOutPath"], str(log_directory / "tracefang-server.log"))

    def test_virtualenv_python_uses_project_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            executable = project_root / ".venv" / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.touch()
            self.assertEqual(virtualenv_python(project_root), executable)

    def test_start_options_are_explicit(self) -> None:
        args = parse_args(["start", "--rebuild", "--no-browser"])
        self.assertEqual(args.command, "start")
        self.assertTrue(args.rebuild)
        self.assertTrue(args.no_browser)


if __name__ == "__main__":
    unittest.main()
