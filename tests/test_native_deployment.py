from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tracefang import service


class StableSourceReadTests(unittest.TestCase):
    def test_web_bundle_receipt_rejects_changed_source_or_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, bundle = root / "web/src/main.ts", root / "web/dist/index.html"
            source.parent.mkdir(parents=True)
            bundle.parent.mkdir(parents=True)
            for name in ("index.html", "package.json", "pnpm-lock.yaml", "vite.config.ts"):
                (root / "web" / name).write_text("fixed input")
            source.write_text("fixed source")
            bundle.write_text("fresh bundle")
            receipt = root / "web-build.json"
            receipt.write_text(
                json.dumps(
                    {
                        "schema": "tracefang-web-source-build-v1",
                        "complete": True,
                        "source_hashes": service.web_source_hashes(root),
                        "bundle_hashes": service.web_bundle_hashes(root),
                    }
                )
            )
            service.verify_web_build_receipt(root, receipt)
            for changed in (source, bundle):
                before = changed.read_bytes()
                changed.write_bytes(b"different bytes")
                with self.assertRaisesRegex(service.ServiceError, "固定源码收据不一致"):
                    service.verify_web_build_receipt(root, receipt)
                changed.write_bytes(before)

    def test_cloud_short_read_cannot_hash_or_copy_nonempty_source(self):
        class ShortRead:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def fileno(self):
                return self.handle.fileno()

            def read(self, *args):
                return b""

        with tempfile.TemporaryDirectory() as temporary:
            source, copied = Path(temporary) / "source", Path(temporary) / "copied"
            source.write_bytes(b"nonempty cloud placeholder")
            original_open = Path.open

            def short_open(path, *args, **kwargs):
                handle = original_open(path, *args, **kwargs)
                return ShortRead(handle) if path == source else handle

            for operation in (
                lambda: service.file_sha256(source),
                lambda: service.stable_read_bytes(source),
                lambda: service.copy_verified_file(source, copied),
            ):
                with (
                    patch.object(Path, "open", autospec=True, side_effect=short_open),
                    self.assertRaisesRegex(service.ServiceError, "读取不完整"),
                ):
                    operation()

    def test_same_bytes_replaced_during_read_cannot_enter_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            replacement = Path(temporary) / "replacement"
            source.write_bytes(b"same bytes")
            replacement.write_bytes(b"same bytes")
            original_open = Path.open

            class ReplacedDuringRead:
                def __init__(self, handle):
                    self.handle, self.replaced = handle, False

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.handle.close()

                def fileno(self):
                    return self.handle.fileno()

                def read(self, *args):
                    content = self.handle.read(*args)
                    if not self.replaced:
                        replacement.replace(source)
                        self.replaced = True
                    return content

            def replaced_open(path, *args, **kwargs):
                handle = original_open(path, *args, **kwargs)
                return ReplacedDuringRead(handle) if path == source else handle

            with (
                patch.object(Path, "open", autospec=True, side_effect=replaced_open),
                self.assertRaisesRegex(service.ServiceError, "发生变化"),
            ):
                service.file_sha256(source)


class RegistrationBoundaryTests(unittest.TestCase):
    def test_prepare_and_status_never_move_registration(self) -> None:
        for command in ("prepare", "status"):
            with (
                self.subTest(command=command),
                patch.object(service, "operation_lock"),
                patch.object(service, "migrate_registration") as migrate,
                patch.object(service, "prepare_service"),
                patch.object(service, "print_status", return_value=0),
            ):
                self.assertEqual(service.main([command]), 0)
                migrate.assert_not_called()

    def test_loaded_legacy_registration_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, native = root / "old.plist", root / "native.plist"
            legacy.write_bytes(b"fixed legacy registration")
            with (
                patch.object(service, "IS_WINDOWS", False),
                patch.object(service, "LEGACY_REGISTRATION", legacy),
                patch.object(service, "SERVICE_REGISTRATION", native),
                patch.object(service, "APPLICATION_SUPPORT", root),
                patch.object(service, "service_is_loaded", return_value=True),
                self.assertRaisesRegex(service.ServiceError, "原注册保持不变"),
            ):
                service.migrate_registration()
            self.assertEqual(legacy.read_bytes(), b"fixed legacy registration")
            self.assertFalse(native.exists())

    def test_explicit_unloaded_legacy_move_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, native = root / "old.plist", root / "native.plist"
            legacy.write_bytes(b"fixed legacy registration")
            with (
                patch.object(service, "IS_WINDOWS", False),
                patch.object(service, "LEGACY_REGISTRATION", legacy),
                patch.object(service, "SERVICE_REGISTRATION", native),
                patch.object(service, "APPLICATION_SUPPORT", root),
                patch.object(service, "service_is_loaded", return_value=False),
            ):
                service.migrate_registration()
            self.assertFalse(legacy.exists())
            self.assertEqual(native.read_bytes(), b"fixed legacy registration")


def ready_fixture(root: Path) -> tuple[dict, dict]:
    info = {
        "runtime": "rust",
        "backend_build_fingerprint": "a" * 64,
        "persistence_schema": "native-v1",
        "aggregation_version": "aggregation-v4",
    }
    paths = {
        "data": str(root),
        "store": str(root / "facts.redb"),
        "capture": str(root / "capture.redb"),
    }
    position = {"epoch": "capture-epoch", "sequence": "9", "digest": "fixed-digest"}
    payload = {
        "runtime": "rust",
        "backend_build_fingerprint": "a" * 64,
        "build_info": info,
        "paths": paths,
        "read_only": False,
        "production_ready": True,
        "status": "ok",
        "database": {"state": "healthy"},
        "generation": "generation",
        "snapshot_version": {
            "store_epoch": "facts-epoch",
            "active_generation": "generation",
            "commit_id": "11",
            "schema_version": "native-v1",
            "aggregation_version": "aggregation-v4",
        },
        "capture_retained_bounds": {
            "epoch": "capture-epoch",
            "state": "ready",
            "last_sequence": "9",
            "last_position": position,
            "gaps": [],
        },
        "capture": {"state": "connected"},
        "acquisition": {"state": "running", "projection": {"evidence_complete": True}},
    }
    return payload, {"build_info": info, "paths": paths}


class NativeDeploymentTests(unittest.TestCase):
    def test_candidate_ready_must_belong_to_the_started_process(self):
        for actual_pid in (321, 999):
            with self.subTest(actual_pid=actual_pid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                payload, manifest = ready_fixture(root / "data")
                payload["process_id"] = actual_pid
                process = SimpleNamespace(pid=321, wait=lambda **kwargs: 0)
                with (
                    patch.object(service, "APPLICATION_SUPPORT", root / "support"),
                    patch.object(service, "verify_runtime_manifest", return_value=manifest),
                    patch.object(service, "backend_environment", return_value={}),
                    patch.object(service, "backend_executable", return_value=root / "server"),
                    patch.object(service.subprocess, "Popen", return_value=process),
                    patch.object(service, "wait_until_ready", return_value=payload),
                ):
                    if actual_pid == process.pid:
                        self.assertEqual(service.validate_candidate(root), payload)
                        self.assertEqual(
                            json.loads((root / "candidate-validation.json").read_text())[
                                "exit_code"
                            ],
                            0,
                        )
                    else:
                        with self.assertRaisesRegex(service.ServiceError, "本次启动身份不一致"):
                            service.validate_candidate(root)
                        self.assertFalse((root / "candidate-validation.json").exists())

    def test_runtime_identity_paths_and_prefix_are_required(self):
        payload, manifest = ready_fixture(Path("/persistent"))
        self.assertIsNone(service.readiness_problem(payload, manifest))
        for mutation in (
            lambda p: p.update(runtime="python"),
            lambda p: p.update(backend_build_fingerprint="b" * 64),
            lambda p: p["paths"].update(capture="/other/capture.redb"),
            lambda p: p.update(capture_retained_bounds=None),
            lambda p: p.update(snapshot_version=None),
            lambda p: p["snapshot_version"].update(schema_version="old-schema"),
            lambda p: p["snapshot_version"].update(aggregation_version="old-index"),
            lambda p: p["acquisition"]["projection"].update(evidence_complete=False),
        ):
            bad = copy.deepcopy(payload)
            mutation(bad)
            self.assertIsNotNone(service.readiness_problem(bad, manifest))

    def test_read_only_shadow_cannot_be_production_ready(self):
        payload, manifest = ready_fixture(Path("/persistent"))
        payload.update(read_only=True, production_ready=False, status="read_only_shadow")
        payload["acquisition"]["state"] = "unavailable"
        payload["capture"]["state"] = "unavailable"
        self.assertIsNone(service.readiness_problem(payload, manifest, read_only=True))
        self.assertIsNotNone(service.readiness_problem(payload, manifest))
        payload["production_ready"] = True
        self.assertIsNotNone(service.readiness_problem(payload, manifest, read_only=True))

    def test_rollback_requires_unchanged_exact_tail_facts_and_format(self):
        before, manifest = ready_fixture(Path("/persistent"))
        after = copy.deepcopy(before)
        self.assertTrue(service.rollback_is_safe(before, after, manifest, manifest))
        for mutation in (
            lambda p: p["capture_retained_bounds"].update(last_sequence="10"),
            lambda p: p["capture_retained_bounds"]["last_position"].update(digest="changed"),
            lambda p: p["snapshot_version"].update(commit_id="12"),
            lambda p: p["paths"].update(store="/other/facts.redb"),
        ):
            bad = copy.deepcopy(after)
            mutation(bad)
            self.assertFalse(service.rollback_is_safe(before, bad, manifest, manifest))
        incompatible = copy.deepcopy(manifest)
        incompatible["build_info"]["aggregation_version"] = "changed"
        self.assertFalse(service.rollback_is_safe(before, after, manifest, incompatible))
        self.assertFalse(service.rollback_is_safe(None, after, manifest, manifest))

    def test_failed_activation_preserves_candidate_and_does_not_restore_after_new_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous, candidate = root / "previous", root / "candidate"
            previous.mkdir()
            candidate.mkdir()
            (previous / "release-manifest.json").touch()
            registration = root / "service.plist"
            registration.write_bytes(b"old registration")
            before, manifest = ready_fixture(root / "data")
            after = copy.deepcopy(before)
            after["capture_retained_bounds"]["last_sequence"] = "10"
            registrations = []
            with (
                patch.object(service, "SERVICE_REGISTRATION", registration),
                patch.object(service, "installed_runtime", return_value=previous),
                patch.object(service, "service_is_loaded", return_value=True),
                patch.object(service, "verify_runtime_manifest", return_value=manifest),
                patch.object(
                    service,
                    "wait_until_ready",
                    side_effect=[before, service.ServiceError("startup failed")],
                ),
                patch.object(service, "stop_backend"),
                patch.object(service, "ensure_port_available"),
                patch.object(
                    service, "register_service", side_effect=lambda path: registrations.append(path)
                ),
                patch.object(service, "validate_candidate", return_value=after),
                self.assertRaises(service.ServiceError),
            ):
                service.activate_runtime(candidate)
            self.assertEqual(registrations, [candidate])
            self.assertTrue(candidate.exists())
            failure = json.loads((candidate / "activation-status.json").read_text())
            self.assertFalse(failure["previous_restored"])
            self.assertTrue(failure["data_and_candidate_preserved"])

    def test_fixed_backend_survives_shared_target_and_workspace_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            target = Path(directory) / "cache/target"
            for name in (
                "src/main.rs",
                "assets/catalog.json",
                "Cargo.toml",
                "Cargo.lock",
                "build.rs",
            ):
                path = root / "backend" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            target_binary = target / "release/tracefang-server"
            target_binary.parent.mkdir(parents=True)
            config = "release; independent test compiler"
            compiled = {}

            def compile_backend(command, **kwargs):
                snapshot = Path(command[command.index("--manifest-path") + 1]).parents[1]
                compiled["fingerprint"] = service.source_fingerprint(snapshot, config)
                (root / "backend/src/main.rs").write_text("changed during build")
                target_binary.write_bytes(b"compiled frozen source")

            with (
                patch.object(service, "build_target_directory", return_value=target),
                patch.object(service.shutil, "which", return_value="cargo"),
                patch.object(service.subprocess, "run", side_effect=compile_backend),
                patch.object(
                    service,
                    "binary_build_info",
                    side_effect=lambda _: {
                        "runtime": "rust",
                        "backend_build_fingerprint": compiled["fingerprint"],
                        "backend_build_config": config,
                    },
                ),
            ):
                binary = service.build_backend(root)
            self.assertNotEqual(binary, target_binary)
            target_binary.write_bytes(b"another concurrent cargo output")
            self.assertEqual(binary.read_bytes(), b"compiled frozen source")
            manifest = json.loads((binary.parent.parent / "build-manifest.json").read_text())
            original = hashlib.sha256(b"src/main.rs").hexdigest()
            self.assertEqual(manifest["source_hashes"]["backend/src/main.rs"], original)

    def test_wrong_shared_target_binary_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            (root / "backend/src").mkdir(parents=True)
            (root / "backend/src/main.rs").write_text("correct source")
            target = Path(directory) / "cache/target"
            (target / "release").mkdir(parents=True)
            (target / "release/tracefang-server").write_text("unrelated binary")
            with (
                patch.object(service, "build_target_directory", return_value=target),
                patch.object(service.shutil, "which", return_value="cargo"),
                patch.object(service.subprocess, "run"),
                patch.object(
                    service,
                    "binary_build_info",
                    return_value={
                        "runtime": "rust",
                        "backend_build_config": "release",
                        "backend_build_fingerprint": "0" * 64,
                    },
                ),
                self.assertRaisesRegex(service.ServiceError, "固定二进制"),
            ):
                service.build_backend(root)
            self.assertEqual(list(target.parent.glob("build-artifacts/*/build-manifest.json")), [])

    def test_mutating_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, destination = Path(directory) / "root", Path(directory) / "frozen"
            root.mkdir()
            path = root / "lock"
            path.write_text("original")
            expected = {"lock": service.file_sha256(path)}
            path.write_text("changed")
            with self.assertRaisesRegex(service.ServiceError, "输入发生变化"):
                service.freeze_files(root, destination, expected)

    def test_forged_duckdb_receipt_does_not_authorize_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            support = Path(directory)
            runtime = support / "runtime/duckdb/1.5.6"
            runtime.mkdir(parents=True)
            executable = runtime / "duckdb"
            executable.write_bytes(b"forged executable")
            pins = {
                "ASSETS": {("Darwin", "arm64"): ("osx-arm64", "official-archive")},
                "EXECUTABLE_SHA256": {"osx-arm64": "official-executable"},
            }
            (runtime / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": "1.5.6",
                        "platform": "osx-arm64",
                        "archive_sha256": "official-archive",
                        "executable_sha256": service.file_sha256(executable),
                    }
                )
            )
            with (
                patch.object(service, "APPLICATION_SUPPORT", support),
                patch.object(service, "IS_WINDOWS", False),
                patch.object(service.platform, "system", return_value="Darwin"),
                patch.object(service.platform, "machine", return_value="arm64"),
                patch.object(service.runpy, "run_path", return_value=pins),
                patch.object(
                    service.subprocess, "run", side_effect=service.ServiceError("installer refused")
                ) as install,
                patch.object(service.subprocess, "check_output") as execute,
                self.assertRaises(service.ServiceError),
            ):
                service.ensure_duckdb_runtime(Path("trusted-installer.py"))
            install.assert_called_once()
            execute.assert_not_called()

    def test_native_run_and_stop_never_start_legacy_containers(self):
        with (
            patch.object(service, "run_backend", return_value=0),
            patch.object(service, "backend_executable", return_value=Path("native")),
            patch.object(service, "start_infrastructure") as legacy,
            patch.object(service, "stop_backend"),
            patch.object(service, "IS_WINDOWS", False),
            patch.object(service.subprocess, "run") as command,
        ):
            self.assertEqual(service.run_server(), 0)
            service.stop_service()
        legacy.assert_not_called()
        self.assertNotIn("docker", str(command.call_args_list))

    def test_development_entry_does_not_start_legacy_containers(self):
        script = Path(__file__).parents[1] / "scripts/run-local.py"
        spec = importlib.util.spec_from_file_location("tracefang_run_local_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with (
            patch.object(
                module,
                "parse_args",
                return_value=SimpleNamespace(dev=True, no_database=False, no_build=False),
            ),
            patch.object(module, "run_development", return_value=0),
            patch.object(module, "start_infrastructure") as legacy,
        ):
            self.assertEqual(module.main(), 0)
        legacy.assert_not_called()

    def test_persistent_paths_do_not_follow_release_on_windows(self):
        with (
            patch.dict(service.os.environ, {"LOCALAPPDATA": "/user/local"}, clear=True),
            patch.object(service, "IS_WINDOWS", True),
            patch.object(service, "APPLICATION_SUPPORT", Path("/user/local/TraceFang")),
            patch.object(service, "virtualenv_python", return_value=Path("/release/python.exe")),
            patch.object(service.urllib.request, "getproxies", return_value={}),
        ):
            a = service.backend_environment(Path("/release/a"))
            b = service.backend_environment(Path("/release/b"))
            self.assertEqual(
                service.build_target_directory(), Path("/user/local/TraceFang/cache/rust-target")
            )
        for key in (
            "TRACEFANG_DATA_DIR",
            "TRACEFANG_QUANT_RESULTS_DIR",
            "TRACEFANG_BATCH_SNAPSHOTS_DIR",
            "TRACEFANG_DUCKDB_PATH",
        ):
            self.assertEqual(a[key], b[key])
            self.assertTrue(Path(a[key]).is_relative_to("/user/local/TraceFang"))
