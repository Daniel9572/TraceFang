from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracefang import service
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
            relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
            executable = project_root / ".venv" / relative
            executable.parent.mkdir(parents=True)
            executable.touch()
            self.assertEqual(virtualenv_python(project_root), executable)

    def test_start_options_are_explicit(self) -> None:
        args = parse_args(["start", "--no-browser"])
        self.assertEqual(args.command, "start")
        self.assertTrue(args.no_browser)
        self.assertTrue(parse_args(["update", "--rebuild"]).rebuild)

    def test_repeated_start_preserves_process_and_skips_deployment(self) -> None:
        with (
            patch.object(service, "SERVICE_REGISTRATION"),
            patch.object(service, "virtualenv_python"),
            patch.object(service, "installed_runtime"),
            patch.object(service, "service_is_loaded", return_value=True),
            patch.object(service, "wait_until_ready", return_value={}),
            patch.object(service, "deploy_runtime") as deploy,
            patch.object(service, "subprocess") as commands,
        ):
            service.start_service(open_browser=False)
        deploy.assert_not_called()
        commands.run.assert_not_called()

    def test_failed_preparation_keeps_running_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch.object(service, "APPLICATION_SUPPORT", root),
                patch.object(service, "SERVICE_REGISTRATION", root / "agent.plist"),
                patch.object(service, "installed_runtime", return_value=root / "previous"),
                patch.object(service, "service_is_loaded", return_value=True),
                patch.object(service, "build_web"),
                patch.object(service, "deploy_runtime", side_effect=service.ServiceError("failed")),
                patch.object(service, "stop_backend") as stop,
                self.assertRaises(service.ServiceError),
            ):
                service.update_service(rebuild=False, open_browser=False)
            stop.assert_not_called()
            self.assertEqual(list(root.glob("release-*")), [])

    def test_failed_activation_restores_previous_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registration = root / "agent.plist"
            registration.write_bytes(b"previous registration")
            previous = root / "previous"
            with (
                patch.object(service, "APPLICATION_SUPPORT", root),
                patch.object(service, "SERVICE_REGISTRATION", registration),
                patch.object(service, "installed_runtime", return_value=previous),
                patch.object(service, "service_is_loaded", return_value=True),
                patch.object(service, "build_web"),
                patch.object(service, "deploy_runtime"),
                patch.object(service, "ensure_port_available"),
                patch.object(service, "stop_backend"),
                patch.object(service, "register_service") as install,
                patch.object(
                    service, "wait_until_ready", side_effect=[service.ServiceError("failed"), {}]
                ),
                self.assertRaises(service.ServiceError),
            ):
                service.update_service(rebuild=False, open_browser=False)
            self.assertEqual(install.call_args.args, (previous,))
            self.assertEqual(registration.read_bytes(), b"previous registration")

    def test_lock_is_released_after_failure(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(service, "APPLICATION_SUPPORT", Path(temporary_directory)),
        ):
            with self.assertRaisesRegex(ValueError, "test"), service.operation_lock():
                with (
                    self.assertRaises(service.ServiceError),
                    service.operation_lock(timeout_seconds=0),
                ):
                    self.fail("Second operation acquired the lock")
                raise ValueError("test")
            with service.operation_lock(timeout_seconds=0):
                pass

    def test_unmanaged_port_is_never_taken_over(self) -> None:
        with (
            patch.object(service.socket, "create_connection"),
            self.assertRaises(service.ServiceError),
        ):
            service.ensure_port_available()

    def test_docker_timeout_is_treated_as_not_ready(self) -> None:
        with patch.object(
            service.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker info", 5)
        ):
            self.assertFalse(service._docker_is_ready("docker"))

    def test_docker_desktop_is_started_when_daemon_is_absent(self) -> None:
        with (
            patch.object(service, "_docker_command", return_value="docker"),
            patch.object(service, "_docker_is_ready", side_effect=[False, True]),
            patch.object(service.sys, "platform", "darwin"),
            patch.object(service, "IS_WINDOWS", False),
            patch.object(service.subprocess, "run") as command,
        ):
            self.assertEqual(service.ensure_docker_ready(), "docker")
        self.assertEqual(command.call_args.args[0], ["open", "-gja", "Docker"])

    def test_restart_waits_for_old_process_before_loading_new_one(self) -> None:
        events = []
        with (
            patch.object(service, "SERVICE_REGISTRATION"),
            patch.object(service, "virtualenv_python"),
            patch.object(service, "installed_runtime"),
            patch.object(service, "service_is_loaded", return_value=True),
            patch.object(service, "stop_backend", side_effect=lambda: events.append("exited")),
            patch.object(service, "register_service", side_effect=lambda: events.append("start")),
            patch.object(service, "wait_until_ready", side_effect=lambda: events.append("ready")),
        ):
            service.start_service(open_browser=False, restart=True)
        self.assertEqual(events, ["exited", "start", "ready"])


if __name__ == "__main__":
    unittest.main()
