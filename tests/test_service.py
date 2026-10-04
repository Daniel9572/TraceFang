from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
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

    def test_launch_agent_points_directly_to_fixed_rust_binary(self) -> None:
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
            [str(project_root / "bin/tracefang-server")],
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
            patch.object(service, "backend_executable"),
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
            candidates = list(root.glob("release-*"))
            self.assertEqual(len(candidates), 1)
            self.assertTrue(
                json.loads((candidates[0] / "preparation-status.json").read_text())[
                    "data_and_candidate_preserved"
                ]
            )

    def test_legacy_activation_is_rejected_before_stopping_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registration = root / "agent.plist"
            registration.write_bytes(b"previous registration")
            with (
                patch.object(service, "SERVICE_REGISTRATION", registration),
                patch.object(service, "installed_runtime", return_value=root / "legacy"),
                patch.object(service, "service_is_loaded", return_value=True),
                patch.object(service, "verify_runtime_manifest", return_value={}),
                patch.object(service, "stop_backend") as stop,
                self.assertRaisesRegex(service.ServiceError, "旧版仍在采集"),
            ):
                service.activate_runtime(root / "candidate")
            stop.assert_not_called()
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
            patch.object(service, "backend_executable"),
            patch.object(service, "installed_runtime"),
            patch.object(service, "service_is_loaded", return_value=True),
            patch.object(service, "stop_backend", side_effect=lambda: events.append("exited")),
            patch.object(service, "register_service", side_effect=lambda: events.append("start")),
            patch.object(service, "wait_until_ready", side_effect=lambda: events.append("ready")),
        ):
            service.start_service(open_browser=False, restart=True)
        self.assertEqual(events, ["exited", "start", "ready"])

    def test_missing_rust_binary_does_not_launch_python_api(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(service.ServiceError, "Rust"),
        ):
            service.backend_executable(Path(directory))

    def test_deployment_uses_fixed_inputs_and_preserves_installed_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project, installed, candidate = (base / name for name in ("project", "old", "new"))
            project.mkdir()
            for name in (
                "pyproject.toml",
                "uv.lock",
                "README.md",
                ".env.local",
                "scripts/install-duckdb.py",
                "web/index.html",
                "web/package.json",
                "web/pnpm-lock.yaml",
                "web/vite.config.ts",
                "src/tracefang/service.py",
                "web/dist/index.html",
                "backend/src/research/source_period.rs",
                "backend/tests/fixtures/source-period-min5-v1.json",
            ):
                target = project / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("original", encoding="utf-8")
            binary = base / "artifact/bin/tracefang-server"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"native Rust binary")
            info = {
                "runtime": "rust",
                "backend_build_fingerprint": service.source_fingerprint(project, "test config"),
                "backend_build_config": "test config",
            }
            (binary.parent.parent / "build-manifest.json").write_text(
                json.dumps(
                    {
                        "build_info": info,
                        "source_hashes": service.backend_source_hashes(project),
                        "binary_sha256": service.file_sha256(binary),
                    }
                )
            )
            duckdb = base / "duckdb"
            duckdb.write_bytes(b"verified")
            receipt = {"path": str(duckdb), "executable_sha256": service.file_sha256(duckdb)}
            for root, value in ((project, "workspace"), (installed, "installed")):
                (root / "data").mkdir(parents=True)
                (root / "data/sources.json").write_text(value, encoding="utf-8")

            def build(snapshot):
                self.assertEqual((snapshot / "src/tracefang/service.py").read_text(), "original")
                (project / "src/tracefang/service.py").write_text("changed during build")
                return binary

            with (
                patch.dict(service.os.environ, {"TRACEFANG_SOURCE_CONFIG": ""}),
                patch.object(service, "PROJECT_ROOT", project),
                patch.object(service, "APPLICATION_SUPPORT", base / "support"),
                patch.object(service, "installed_runtime", return_value=installed),
                patch.object(service, "build_backend", side_effect=build),
                patch.object(service, "build_web"),
                patch.object(service, "build_target_directory", return_value=base / "cache/target"),
                patch.object(service, "binary_build_info", return_value=info),
                patch.object(service, "ensure_duckdb_runtime", return_value=receipt),
                patch.object(service, "virtualenv_python", return_value=candidate / "python"),
                patch.object(service.shutil, "which", return_value="uv"),
                patch.object(service.subprocess, "run") as run,
            ):
                service.deploy_runtime(candidate)
                manifest = service.verify_runtime_manifest(candidate)
                self.assertEqual(manifest["state"], "prepared")
            fixture = "backend/tests/fixtures/source-period-min5-v1.json"
            self.assertEqual(
                manifest["packaging_input_hashes"][fixture], service.file_sha256(project / fixture)
            )
            with tarfile.open(candidate / "release-inputs.tar.gz", "r:gz") as archive:
                self.assertEqual(
                    archive.extractfile(fixture).read(), (project / fixture).read_bytes()
                )
            self.assertEqual((candidate / "bin/tracefang-server").read_bytes(), binary.read_bytes())
            self.assertEqual((candidate / "src/tracefang/service.py").read_text(), "original")
            self.assertEqual((candidate / "data/sources.json").read_text(), "installed")
            self.assertFalse((candidate / "compose.yaml").exists())
            self.assertFalse((candidate / "nats-server.conf").exists())
            self.assertNotIn("tracefang.api", str(run.call_args_list))

    def test_rust_exec_uses_release_directory_and_preserves_pending_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutdown = root / ".runtime/shutdown"
            shutdown.parent.mkdir()
            shutdown.write_text("stop", encoding="utf-8")
            binary = root / "bin/tracefang-server"
            with (
                patch.object(service, "IS_WINDOWS", False),
                patch.object(service, "virtualenv_python", return_value=root / "python"),
                patch.object(service.os, "chdir") as chdir,
                patch.object(service.os, "execve", side_effect=RuntimeError("replaced")) as execute,
                self.assertRaisesRegex(RuntimeError, "replaced"),
            ):
                service.run_backend(binary, project_root=root, managed=True)
            self.assertTrue(shutdown.exists())
            chdir.assert_called_once_with(root)
            self.assertEqual(execute.call_args.args[:2], (str(binary), [str(binary)]))
            self.assertEqual(execute.call_args.args[2]["TRACEFANG_SHUTDOWN_FILE"], str(shutdown))

    def test_readiness_requires_capture_and_does_not_fall_back_to_health(self) -> None:
        payload = {"database": {"state": "healthy"}, "acquisition": {"state": "running"}}
        with (
            patch.object(service.time, "monotonic", side_effect=[0, 0, 2]),
            patch.object(service.time, "sleep"),
            patch.object(
                service.urllib.request, "urlopen", return_value=io.StringIO(json.dumps(payload))
            ) as request,
            self.assertRaises(service.ServiceError),
            patch.object(
                service,
                "verify_runtime_manifest",
                return_value={"build_info": {"backend_build_fingerprint": "a" * 64}, "paths": {}},
            ),
        ):
            service.wait_until_ready(timeout_seconds=1)
        request.assert_called_once_with("http://127.0.0.1:8000/api/ready", timeout=2)

    def test_system_proxy_is_inherited_without_overriding_explicit_settings(self) -> None:
        with (
            patch.dict(
                service.os.environ,
                {"HTTPS_PROXY": "http://custom:8080", "no_proxy": "internal.example"},
                clear=True,
            ),
            patch.object(
                service.urllib.request,
                "getproxies",
                return_value={"https": "http://system:7897", "http": "http://system:7897"},
            ),
            patch.object(service, "virtualenv_python", return_value=Path("/runtime/python")),
        ):
            environment = service.backend_environment(Path("/runtime"))
        self.assertEqual(environment["HTTPS_PROXY"], "http://custom:8080")
        self.assertNotIn("https_proxy", environment)
        self.assertEqual(environment["http_proxy"], "http://system:7897")
        self.assertEqual(environment["no_proxy"], "internal.example,127.0.0.1,localhost,::1")

    def test_fixed_original_source_bodies_never_enter_normal_service_environment(self) -> None:
        keys = (
            "TRACEFANG_SOURCE_PERIOD_FIXTURE_MANIFEST",
            "TRACEFANG_SOURCE_PERIOD_FIXTURE_SHA256",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / ".env.local"
            original = "\n".join(f"{key}=fixed-test-value" for key in keys)
            original += "\nTRACEFANG_SOURCE_CONFIG=data/real-sources.json\n"
            config.write_text(original)
            for inherited in (False, True):
                with (
                    self.subTest(inherited=inherited),
                    patch.dict(
                        service.os.environ,
                        dict.fromkeys(keys, "inherited-test-value") if inherited else {},
                        clear=True,
                    ),
                    patch.object(service.urllib.request, "getproxies", return_value={}),
                    patch.object(service, "virtualenv_python", return_value=root / "python"),
                ):
                    environment = service.backend_environment(root)
                for key in keys:
                    self.assertNotIn(key, environment)
                self.assertEqual(environment["TRACEFANG_SOURCE_CONFIG"], "data/real-sources.json")
                payload = service.launch_agent_payload(
                    python=root / "python",
                    project_root=root,
                    environment={**environment, **dict.fromkeys(keys, "poisoned-test-value")},
                )
                for key in keys:
                    self.assertNotIn(key, payload["EnvironmentVariables"])
                self.assertEqual(config.read_text(), original)


if __name__ == "__main__":
    unittest.main()
