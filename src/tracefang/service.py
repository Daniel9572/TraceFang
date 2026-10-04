from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import runpy
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

IS_WINDOWS = os.name == "nt"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE_LABEL = "com.tracefang.local"
SERVICE_DOMAIN = f"gui/{os.getuid()}" if not IS_WINDOWS else ""
SERVICE_TARGET = f"{SERVICE_DOMAIN}/{SERVICE_LABEL}"
LEGACY_REGISTRATION = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
APPLICATION_SUPPORT = Path.home() / "Library" / "Application Support" / "TraceFang"
if IS_WINDOWS:
    APPLICATION_SUPPORT = (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "TraceFang"
    )
SERVICE_REGISTRATION = APPLICATION_SUPPORT / "service.plist"
RUNTIME_ROOT = APPLICATION_SUPPORT / "runtime"
LOG_DIRECTORY = Path.home() / "Library" / "Logs" / "TraceFang"
if IS_WINDOWS:
    LOG_DIRECTORY = APPLICATION_SUPPORT / "logs"
UPDATE_ENTRY = "update.cmd" if IS_WINDOWS else "update.command"
STDOUT_LOG = LOG_DIRECTORY / "tracefang-server.log"
STDERR_LOG = LOG_DIRECTORY / "tracefang-server.error.log"
WEB_DIRECTORY = PROJECT_ROOT / "web"
WEB_INDEX = WEB_DIRECTORY / "dist" / "index.html"
DEFAULT_SERVICE_PATH = ":".join(
    (
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/Applications/ChatGPT.app/Contents/Resources",
    )
)


class ServiceError(RuntimeError):
    pass


class ApplicationAlreadyOpen(ServiceError):
    pass


def _file_lock(handle: object, *, unlock: bool = False) -> None:
    if IS_WINDOWS:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


def open_interface() -> None:
    if IS_WINDOWS:
        os.startfile("http://127.0.0.1:8000")
    else:
        subprocess.run(["open", "http://127.0.0.1:8000"], check=False)


@contextmanager
def operation_lock(
    *, timeout_seconds: float = 180, filename: str = "launcher.lock"
) -> Iterator[None]:
    APPLICATION_SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (APPLICATION_SUPPORT / filename).open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        announced = False
        while True:
            try:
                _file_lock(handle)
                break
            except OSError:
                if not announced:
                    print("[TraceFang] 另一个操作正在进行, 等待其完成", flush=True)
                    announced = True
                if time.monotonic() >= deadline:
                    if filename == "application.lock":
                        raise ApplicationAlreadyOpen("另一个应用窗口已持有项目服务") from None
                    raise ServiceError("等待启动操作超时, 请检查服务状态") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            _file_lock(handle, unlock=True)


def installed_runtime() -> Path:
    if SERVICE_REGISTRATION.is_file():
        with SERVICE_REGISTRATION.open("rb") as handle:
            return Path(plistlib.load(handle)["WorkingDirectory"])
    return RUNTIME_ROOT


def migrate_registration() -> None:
    """Move an unloaded legacy registration only during explicit installation."""
    if not IS_WINDOWS and LEGACY_REGISTRATION.is_file():
        if service_is_loaded():
            raise ServiceError("旧后台任务仍已加载; 须先完成正式停机交接, 原注册保持不变")
        APPLICATION_SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not SERVICE_REGISTRATION.exists():
            os.replace(LEGACY_REGISTRATION, SERVICE_REGISTRATION)
        else:
            # Keep an old registration recoverable, but outside LaunchAgents.
            os.replace(LEGACY_REGISTRATION, APPLICATION_SUPPORT / "legacy-service.plist")


def ensure_port_available() -> None:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=1):
            raise ServiceError("应用端口已被其他进程占用, 请先停止开发服务或占用程序")
    except (ConnectionRefusedError, TimeoutError):
        return


def virtualenv_python(project_root: Path = PROJECT_ROOT) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    executable = project_root / ".venv" / relative
    if not executable.is_file():
        raise ServiceError(
            f"缺少 Python 运行环境; 请先完成 uv sync, 再运行 {UPDATE_ENTRY} 安装运行版本"
        )
    return executable


def backend_executable(project_root: Path) -> Path:
    executable = (
        project_root / "bin" / ("tracefang-server.exe" if IS_WINDOWS else "tracefang-server")
    )
    if not executable.is_file():
        raise ServiceError(f"缺少 Rust 后端; 请运行 {UPDATE_ENTRY} 安装当前版本")
    return executable


def build_backend(project_root: Path | None = None, *, release: bool = True) -> Path:
    root = project_root or PROJECT_ROOT
    artifact_root = build_target_directory().parent / "build-artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact = Path(tempfile.mkdtemp(prefix="backend-", dir=artifact_root))
    snapshot = artifact / "input"
    snapshot.mkdir()
    freeze_files(root, snapshot, backend_source_hashes(root))
    source_hashes = backend_source_hashes(snapshot)
    if backend_source_hashes(root) != source_hashes:
        raise ServiceError("固定输入期间后端文件集合变化; 停止构建")
    cargo = shutil.which("cargo")
    if cargo is None:
        local_cargo = Path.home() / ".cargo" / "bin" / ("cargo.exe" if IS_WINDOWS else "cargo")
        if not local_cargo.is_file():
            raise ServiceError("缺少 Rust 工具链, 请先安装 Rust 和 Cargo")
        cargo = str(local_cargo)
    target = build_target_directory()
    command = [
        cargo,
        "build",
        "--locked",
        "--manifest-path",
        str(snapshot / "backend/Cargo.toml"),
        "--target-dir",
        str(target),
        "--bin",
        "tracefang-server",
    ]
    if release:
        command.append("--release")
    print("[TraceFang] 从固定输入构建 Rust 后端")
    subprocess.run(command, cwd=snapshot, check=True, timeout=1200)
    shared = (
        target
        / ("release" if release else "debug")
        / ("tracefang-server.exe" if IS_WINDOWS else "tracefang-server")
    )
    if not shared.is_file():
        raise ServiceError("Rust 构建未生成后端程序")
    (artifact / "bin").mkdir()
    executable = artifact / "bin" / shared.name
    copy_verified_file(shared, executable)
    info = binary_build_info(executable)
    config = info.get("backend_build_config")
    if (
        not isinstance(config, str)
        or source_fingerprint(snapshot, config) != info["backend_build_fingerprint"]
    ):
        raise ServiceError("固定二进制与本次固定源码/编译配置不一致; 产物未发布")
    if backend_source_hashes(snapshot) != source_hashes:
        raise ServiceError("固定构建输入被修改; 产物未发布")
    write_runtime_json(
        artifact / "build-manifest.json",
        {
            "schema": "tracefang-backend-build-v1",
            "build_info": info,
            "source_hashes": source_hashes,
            "binary_sha256": file_sha256(executable),
        },
    )
    return executable


def build_target_directory() -> Path:
    explicit = os.environ.get("CARGO_TARGET_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / "TraceFang/cache/rust-target"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/TraceFang/rust-target"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "TraceFang/rust-target"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        count = 0
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            count += len(chunk)
        _check_stable_read(path, before, os.fstat(handle.fileno()), count)
    return digest.hexdigest()


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _check_stable_read(
    path: Path, before: os.stat_result, after: os.stat_result, count: int
) -> None:
    if (
        count != before.st_size
        or _file_identity(before) != _file_identity(after)
        or _file_identity(before) != _file_identity(path.stat())
    ):
        raise ServiceError(f"文件读取不完整或读取期间发生变化, 停止打包: {path.name}")


def stable_read_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        content = handle.read()
        _check_stable_read(path, before, os.fstat(handle.fileno()), len(content))
    return content


def copy_verified_file(source: Path, target: Path) -> None:
    source, target = Path(source), Path(target)
    with source.open("rb") as handle, target.open("wb") as output:
        before = os.fstat(handle.fileno())
        count = 0
        while chunk := handle.read(1024 * 1024):
            output.write(chunk)
            count += len(chunk)
        output.flush()
        os.fsync(output.fileno())
        _check_stable_read(source, before, os.fstat(handle.fileno()), count)
    shutil.copystat(source, target)
    if target.stat().st_size != count or file_sha256(target) != file_sha256(source):
        raise ServiceError(f"复制期间输入发生变化, 停止打包: {source.name}")


def backend_source_hashes(root: Path) -> dict[str, str]:
    backend = root / "backend"
    paths = [
        path
        for folder in (backend / "src", backend / "assets")
        for path in folder.rglob("*")
        if path.suffix in {".rs", ".json", ".sql"}
    ]
    paths.extend(
        backend / name
        for name in ("Cargo.toml", "Cargo.lock", "build.rs", "schema.sql")
        if (backend / name).is_file()
    )
    return {str(path.relative_to(root)): file_sha256(path) for path in sorted(paths)}


def write_runtime_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(path.name + f".pending-{os.getpid()}-{time.time_ns()}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if not IS_WINDOWS:
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def binary_build_info(executable: Path) -> dict[str, object]:
    completed = subprocess.run(
        [str(executable), "--build-info"], check=True, capture_output=True, text=True, timeout=10
    )
    value = json.loads(completed.stdout)
    fingerprint = value.get("backend_build_fingerprint")
    if not isinstance(value, dict):
        raise ServiceError("后端构建身份不是有效对象")
    if (
        value.get("runtime") != "rust"
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
    ):
        raise ServiceError("后端程序未返回可核验的原生构建身份")
    return value


def ensure_duckdb_runtime(installer: Path) -> dict[str, object]:
    pins = runpy.run_path(str(installer))
    asset, archive_sha = pins["ASSETS"][(platform.system(), platform.machine())]
    executable_sha = pins["EXECUTABLE_SHA256"][asset]
    destination = APPLICATION_SUPPORT / "runtime/duckdb/1.5.6"
    executable = destination / ("duckdb.exe" if IS_WINDOWS else "duckdb")
    manifest_path = destination / "manifest.json"

    def installed_receipt() -> dict[str, object] | None:
        if (
            not manifest_path.is_file()
            or not executable.is_file()
            or manifest_path.stat().st_size > 1024 * 1024
        ):
            return None
        try:
            receipt = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (
            receipt.get("version") != "1.5.6"
            or receipt.get("platform") != asset
            or receipt.get("archive_sha256") != archive_sha
            or receipt.get("executable_sha256") != executable_sha
            or file_sha256(executable) != executable_sha
        ):
            return None
        return receipt

    receipt = installed_receipt()
    if receipt is None:
        subprocess.run(
            [sys.executable, str(installer), "--destination", str(destination)],
            check=True,
            timeout=120,
        )
        receipt = installed_receipt()
    if receipt is None:
        raise ServiceError("DuckDB 程序与固定官方归档/二进制摘要不一致")
    # Execute only a private copy whose bytes have already matched a source pin.
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=destination) as scratch:
        verified = Path(scratch) / executable.name
        copy_verified_file(executable, verified)
        if file_sha256(verified) != executable_sha:
            raise ServiceError("DuckDB 核验复制期间发生变化")
        verified.chmod(0o700)
        version = subprocess.check_output(
            [str(verified), "--version"], text=True, timeout=10
        ).strip()
    if (
        not version.startswith("v1.5.6 ")
        or "069cc9f9b5" not in version
        or version != receipt.get("version_output")
        or file_sha256(executable) != executable_sha
    ):
        raise ServiceError("DuckDB 启动版本与官方固定程序不一致")
    return {**receipt, "path": str(executable)}


def source_fingerprint(root: Path, config: str) -> str:
    digest = hashlib.sha256(config.encode())
    for name in backend_source_hashes(root):
        relative = str(Path(name).relative_to("backend")).encode()
        source = stable_read_bytes(root / name)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    return digest.hexdigest()


def freeze_files(root: Path, destination: Path, expected: dict[str, str]) -> None:
    for relative, digest in expected.items():
        source = root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_verified_file(source, target)
        if file_sha256(target) != digest:
            raise ServiceError(f"复制期间输入发生变化, 停止打包: {relative}")
    if any(
        not (root / name).is_file() or file_sha256(root / name) != digest
        for name, digest in expected.items()
    ):
        raise ServiceError("复制期间输入发生变化, 停止打包")


def runtime_input_hashes(root: Path) -> dict[str, str]:
    paths = [
        root / name
        for name in ("pyproject.toml", "uv.lock", "README.md", "scripts/install-duckdb.py")
    ]
    paths.extend(_web_inputs(root / "web"))
    if (root / "backend/src/research/source_period.rs").is_file():
        paths.append(root / "backend/tests/fixtures/source-period-min5-v1.json")
    for folder in (root / "src", root / "web/dist"):
        paths.extend(
            path
            for path in folder.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
            and not any(part.endswith(".egg-info") for part in path.parts)
        )
    if any(not path.is_file() for path in paths):
        raise ServiceError("运行包所需输入文件缺失")
    return {str(path.relative_to(root)): file_sha256(path) for path in sorted(set(paths))}


def publish_runtime_manifest(
    runtime_root: Path,
    build: dict[str, object],
    input_hashes: dict[str, str],
    duckdb: dict[str, object],
) -> None:
    executable = backend_executable(runtime_root)
    files = [
        path
        for folder in (
            runtime_root / "bin",
            runtime_root / "src",
            runtime_root / "web/dist",
            runtime_root / "scripts",
        )
        for path in folder.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    files.extend(
        runtime_root / name
        for name in (
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "web/pnpm-lock.yaml",
            "release-inputs.tar.gz",
        )
        if (runtime_root / name).is_file()
    )
    for relative, expected in input_hashes.items():
        packaged = relative.startswith(("src/", "web/dist/")) or relative in {
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "web/pnpm-lock.yaml",
            "scripts/install-duckdb.py",
        }
        if packaged and file_sha256(runtime_root / relative) != expected:
            raise ServiceError(f"固定输入与已打包文件不一致: {relative}")
    environment = backend_environment(runtime_root)
    data = Path(environment["TRACEFANG_DATA_DIR"])
    info = binary_build_info(executable)
    if info != build["build_info"] or file_sha256(executable) != build["binary_sha256"]:
        raise ServiceError("打包二进制与本次固定构建产物不一致")
    manifest = {
        "schema": "tracefang-native-release-v1",
        "state": "prepared",
        "runtime": "rust",
        "build_info": info,
        "binary_sha256": build["binary_sha256"],
        "source_hashes": build["source_hashes"],
        "packaging_input_hashes": input_hashes,
        "files": {str(path.relative_to(runtime_root)): file_sha256(path) for path in sorted(files)},
        "frontend_lock_sha256": file_sha256(runtime_root / "web/pnpm-lock.yaml"),
        "paths": {
            "data": str(data),
            "store": environment.get("TRACEFANG_STORE_PATH", str(data / "facts.redb")),
            "capture": environment.get("TRACEFANG_CAPTURE_PATH", str(data / "capture.redb")),
        },
        "duckdb": duckdb,
        "worker": {
            "kind": "isolated_akshare",
            "python": str(virtualenv_python(runtime_root)),
            "dependency_lock_sha256": file_sha256(runtime_root / "uv.lock"),
        },
        "secret_files": "local env/source configuration excluded from release evidence",
        "published_at_ns": str(time.time_ns()),
    }
    write_runtime_json(runtime_root / "release-manifest.json", manifest)


def verify_runtime_manifest(runtime_root: Path) -> dict[str, object]:
    with (runtime_root / "release-manifest.json").open("rb") as incoming:
        raw = incoming.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ServiceError("发布清单超过核验预算")
    manifest = json.loads(raw)
    if manifest.get("schema") != "tracefang-native-release-v1":
        raise ServiceError("安装版本缺少可核验的原生发布清单")
    for relative, digest in manifest["files"].items():
        path = (runtime_root / relative).resolve()
        if (
            not path.is_relative_to(runtime_root.resolve())
            or not path.is_file()
            or file_sha256(path) != digest
        ):
            raise ServiceError(f"安装文件与发布清单不一致: {relative}")
    if (
        binary_build_info(backend_executable(runtime_root)) != manifest["build_info"]
        or file_sha256(backend_executable(runtime_root)) != manifest["binary_sha256"]
    ):
        raise ServiceError("实际后端构建身份与安装清单不一致")
    duckdb = manifest["duckdb"]
    if (
        not Path(duckdb["path"]).is_file()
        or file_sha256(Path(duckdb["path"])) != duckdb["executable_sha256"]
    ):
        raise ServiceError("DuckDB 运行文件与发布清单不一致")
    return manifest


def _source_configuration_path(root: Path) -> Path:
    from dotenv import dotenv_values

    configured = os.environ.get("TRACEFANG_SOURCE_CONFIG")
    for name in (".env.local", ".env"):
        if configured is None:
            configured = dotenv_values(root / name).get("TRACEFANG_SOURCE_CONFIG")
    path = Path(configured).expanduser() if configured else Path("data/sources.json")
    return path if path.is_absolute() else root / path


def preserve_source_configuration(runtime_root: Path) -> None:
    destination = _source_configuration_path(runtime_root)
    # An explicitly configured external file remains shared across releases.
    if not destination.resolve().is_relative_to(runtime_root.resolve()):
        return
    for root in (installed_runtime(), PROJECT_ROOT):
        source = _source_configuration_path(root)
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_verified_file(source, destination)
            destination.chmod(0o600)
            return


def _web_inputs(web_directory: Path) -> list[Path]:
    inputs = [
        web_directory / "index.html",
        web_directory / "package.json",
        web_directory / "pnpm-lock.yaml",
        web_directory / "vite.config.ts",
    ]
    inputs.extend(web_directory.glob("tsconfig*.json"))
    for source_directory in (web_directory / "src", web_directory / "public"):
        if source_directory.is_dir():
            inputs.extend(path for path in source_directory.rglob("*") if path.is_file())
    return inputs


def web_build_required(
    web_directory: Path = WEB_DIRECTORY,
    web_index: Path = WEB_INDEX,
) -> bool:
    if not web_index.is_file():
        return True
    built_at = web_index.stat().st_mtime_ns
    return any(
        path.is_file() and path.stat().st_mtime_ns > built_at for path in _web_inputs(web_directory)
    )


def build_web(
    *, force: bool = False, web_directory: Path | None = None, project_root: Path | None = None
) -> None:
    web = web_directory or WEB_DIRECTORY
    root = project_root or PROJECT_ROOT
    if not force and not web_build_required(web, web / "dist/index.html"):
        print("[TraceFang] 网页构建已是最新")
        return
    corepack = shutil.which("corepack")
    if corepack is None:
        raise ServiceError("缺少 corepack, 请先完成项目安装")
    package_manager = json.loads((web / "package.json").read_text(encoding="utf-8"))[
        "packageManager"
    ]
    subprocess.run(
        [corepack, package_manager, "-C", str(web), "install", "--frozen-lockfile"],
        cwd=root,
        check=True,
        timeout=180,
    )
    print("[TraceFang] 构建网页")
    subprocess.run(
        [corepack, package_manager, "-C", str(web), "build"],
        cwd=root,
        check=True,
        timeout=180,
    )


def web_source_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): file_sha256(path)
        for path in sorted(_web_inputs(root / "web"))
        if path.is_file()
    }


def web_bundle_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): file_sha256(path)
        for path in sorted((root / "web/dist").rglob("*"))
        if path.is_file()
    }


def verify_web_build_receipt(root: Path, receipt_path: Path) -> dict[str, object]:
    receipt = json.loads(stable_read_bytes(receipt_path))
    if (
        receipt.get("schema") != "tracefang-web-source-build-v1"
        or receipt.get("complete") is not True
        or receipt.get("source_hashes") != web_source_hashes(root)
        or receipt.get("bundle_hashes") != web_bundle_hashes(root)
        or "web/dist/index.html" not in receipt.get("bundle_hashes", {})
    ):
        raise ServiceError("网页构建产物与固定源码收据不一致")
    return receipt


def _docker_command() -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise ServiceError("未找到 Docker, 请安装并启动 Docker Desktop")
    return docker


def _docker_is_ready(docker: str) -> bool:
    try:
        return (
            subprocess.run(
                [docker, "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


def ensure_docker_ready(*, timeout_seconds: float = 90) -> str:
    docker = _docker_command()
    if _docker_is_ready(docker):
        return docker
    if sys.platform != "darwin" and not IS_WINDOWS:
        raise ServiceError("Docker 服务未运行")
    print("[TraceFang] 正在启动 Docker Desktop")
    if IS_WINDOWS:
        executable = (
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Docker"
            / "Docker"
            / "Docker Desktop.exe"
        )
        os.startfile(str(executable))
    else:
        subprocess.run(
            ["open", "-gja", "Docker"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _docker_is_ready(docker):
            return docker
        time.sleep(2)
    raise ServiceError("Docker Desktop 未能在限定时间内启动")


def start_infrastructure() -> None:
    env_file = PROJECT_ROOT / ".env.local"
    if not env_file.is_file():
        raise ServiceError("缺少 .env.local, 请先运行 setup.cmd 初始化本机配置")
    docker = ensure_docker_ready()
    print("[TraceFang] 启动 PostgreSQL 与 NATS/JetStream")
    subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_file),
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "90",
            "postgres",
            "nats",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def deploy_runtime(
    runtime_root: Path,
    *,
    backend_artifact: Path | None = None,
    web_build_receipt: Path | None = None,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise ServiceError("缺少 uv, 请先完成项目安装")
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # All non-secret packaging inputs are copied and checked before the long build.
    build_target_directory().parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="package-input-", dir=build_target_directory().parent
    ) as scratch:
        snapshot = Path(scratch)
        input_hashes = runtime_input_hashes(PROJECT_ROOT)
        backend_root = (
            backend_artifact.parent.parent / "input" if backend_artifact else PROJECT_ROOT
        )
        frozen_backend = backend_source_hashes(backend_root)
        freeze_files(PROJECT_ROOT, snapshot, input_hashes)
        freeze_files(backend_root, snapshot, frozen_backend)
        if runtime_input_hashes(PROJECT_ROOT) != input_hashes or backend_source_hashes(
            backend_root
        ) != backend_source_hashes(snapshot):
            raise ServiceError("固定输入期间文件集合发生变化; 停止打包")
        if web_build_receipt is None:
            build_web(force=True, web_directory=snapshot / "web", project_root=snapshot)
        else:
            verify_web_build_receipt(snapshot, web_build_receipt)
        input_hashes = runtime_input_hashes(snapshot)
        binary = backend_artifact or build_backend(snapshot)
        build = json.loads(
            (binary.parent.parent / "build-manifest.json").read_text(encoding="utf-8")
        )
        if (
            build["source_hashes"] != backend_source_hashes(snapshot)
            or file_sha256(binary) != build["binary_sha256"]
            or binary_build_info(binary) != build["build_info"]
            or source_fingerprint(snapshot, build["build_info"]["backend_build_config"])
            != build["build_info"]["backend_build_fingerprint"]
        ):
            raise ServiceError("固定构建产物与打包源码证据不一致")
        for relative_path in (
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "web/pnpm-lock.yaml",
            "scripts/install-duckdb.py",
        ):
            target = runtime_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_verified_file(snapshot / relative_path, target)
        for relative_directory in (Path("src"), Path("web/dist")):
            shutil.copytree(
                snapshot / relative_directory,
                runtime_root / relative_directory,
                copy_function=copy_verified_file,
            )
        with tarfile.open(runtime_root / "release-inputs.tar.gz", "w:gz") as archive:
            for relative in sorted({*input_hashes, *build["source_hashes"]}):
                archive.add(snapshot / relative, arcname=relative, recursive=False)
        (runtime_root / "bin").mkdir()
        deployed_binary = runtime_root / "bin" / binary.name
        copy_verified_file(binary, deployed_binary)
    # Local secrets stay local and never enter the public release/source hash manifest.
    for name in (".env.local", ".env"):
        source = PROJECT_ROOT / name
        if source.is_file():
            copy_verified_file(source, runtime_root / name)
            (runtime_root / name).chmod(0o600)
    preserve_source_configuration(runtime_root)
    print("[TraceFang] 同步独立研究工作器运行环境")
    subprocess.run(
        [uv, "sync", "--project", str(runtime_root), "--python", "3.13", "--frozen", "--no-dev"],
        check=True,
        timeout=180,
    )
    subprocess.run(
        [str(virtualenv_python(runtime_root)), "-c", "import tracefang.service; import akshare"],
        cwd=runtime_root,
        env={**os.environ, "PYTHONPATH": str(runtime_root / "src")},
        check=True,
        timeout=30,
    )
    duckdb = ensure_duckdb_runtime(runtime_root / "scripts/install-duckdb.py")
    publish_runtime_manifest(runtime_root, build, input_hashes, duckdb)
    verify_runtime_manifest(runtime_root)


def launch_agent_payload(
    *,
    python: Path,
    project_root: Path = PROJECT_ROOT,
    log_directory: Path = LOG_DIRECTORY,
    environment_path: str | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    resolved = environment or {}
    data = Path(resolved.get("TRACEFANG_DATA_DIR", str(APPLICATION_SUPPORT)))
    values = {
        "PATH": environment_path or resolved.get("PATH", DEFAULT_SERVICE_PATH),
        "PYTHONPATH": str(project_root / "src"),
        "TRACEFANG_PYTHON": str(python),
        "TRACEFANG_WEB_DIST": str(project_root / "web/dist"),
        "TRACEFANG_DATA_DIR": str(data),
        "TRACEFANG_STORE_PATH": str(data / "facts.redb"),
        "TRACEFANG_CAPTURE_PATH": str(data / "capture.redb"),
        "TRACEFANG_QUANT_RESULTS_DIR": str(data / "quant-results"),
        "TRACEFANG_BATCH_SNAPSHOTS_DIR": str(data / "batch-snapshots"),
        "TRACEFANG_DUCKDB_PATH": str(APPLICATION_SUPPORT / "runtime/duckdb/1.5.6/duckdb"),
        "TRACEFANG_SHUTDOWN_FILE": str(project_root / ".runtime/shutdown"),
    }
    for key in (
        "TRACEFANG_STORE_PATH",
        "TRACEFANG_CAPTURE_PATH",
        "TRACEFANG_QUANT_RESULTS_DIR",
        "TRACEFANG_BATCH_SNAPSHOTS_DIR",
        "TRACEFANG_DUCKDB_PATH",
        "TRACEFANG_SOURCE_CONFIG",
        "TRACEFANG_CODEX_CLI_PATH",
        "TRACEFANG_ACQUISITION_ENABLED",
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        if key in resolved:
            values[key] = resolved[key]
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [str(project_root / "bin/tracefang-server")],
        "WorkingDirectory": str(project_root),
        "EnvironmentVariables": values,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(log_directory / "tracefang-server.log"),
        "StandardErrorPath": str(log_directory / "tracefang-server.error.log"),
    }


def stop_backend() -> None:
    if IS_WINDOWS:
        from tracefang.windows_service import task_operation

        task_operation("stop", project_root=installed_runtime())
        return
    state = subprocess.run(
        ["launchctl", "print", SERVICE_TARGET],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"^\s*pid = (\d+)$", state.stdout, re.MULTILINE)
    subprocess.run(
        ["launchctl", "bootout", SERVICE_TARGET],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if match:
        old_pid = int(match[1])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        raise ServiceError("旧后端尚未退出, 已暂停后续操作")


def register_service(project_root: Path | None = None) -> None:
    if sys.platform != "darwin" and not IS_WINDOWS:
        raise ServiceError("系统后台托管入口目前支持 macOS 和 Windows")
    project_root = project_root or installed_runtime()
    backend_executable(project_root)
    manifest = verify_runtime_manifest(project_root)
    (project_root / ".runtime/shutdown").unlink(missing_ok=True)
    python = virtualenv_python(project_root)
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    SERVICE_REGISTRATION.parent.mkdir(parents=True, exist_ok=True)
    environment = backend_environment(project_root)
    if environment.get("TRACEFANG_READ_ONLY_SHADOW") == "1" or environment.get(
        "TRACEFANG_REHEARSAL_GENERATION"
    ):
        raise ServiceError("只读核验不能注册为正式采集服务")
    for key, value in manifest.get("paths", {}).items():
        variable = {
            "data": "TRACEFANG_DATA_DIR",
            "store": "TRACEFANG_STORE_PATH",
            "capture": "TRACEFANG_CAPTURE_PATH",
        }[key]
        actual = environment.get(
            variable,
            str(
                Path(environment["TRACEFANG_DATA_DIR"])
                / ("facts.redb" if key == "store" else "capture.redb")
            ),
        )
        if Path(actual).resolve() != Path(value).resolve():
            raise ServiceError(f"正式注册路径与发布清单不一致: {key}")
    payload = (
        {"WorkingDirectory": str(project_root)}
        if IS_WINDOWS
        else launch_agent_payload(
            python=python,
            project_root=project_root,
            log_directory=LOG_DIRECTORY,
            environment=environment,
            environment_path=DEFAULT_SERVICE_PATH,
        )
    )
    temporary_path = SERVICE_REGISTRATION.with_name(
        SERVICE_REGISTRATION.name + f".pending-{os.getpid()}-{time.time_ns()}"
    )
    with temporary_path.open("wb") as handle:
        temporary_path.chmod(0o600)
        plistlib.dump(payload, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, SERVICE_REGISTRATION)
    if not IS_WINDOWS:
        descriptor = os.open(SERVICE_REGISTRATION.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    if IS_WINDOWS:
        from tracefang.windows_service import task_operation

        task_operation("install", project_root=project_root)
        return
    subprocess.run(["launchctl", "enable", SERVICE_TARGET], check=True)
    subprocess.run(
        ["launchctl", "bootstrap", SERVICE_DOMAIN, str(SERVICE_REGISTRATION)],
        check=True,
    )


def service_is_loaded() -> bool:
    if IS_WINDOWS:
        from tracefang.windows_service import task_operation

        return bool(task_operation("status")["running"])
    return (
        subprocess.run(
            ["launchctl", "print", SERVICE_TARGET],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def readiness_problem(
    payload: dict[str, object],
    manifest: dict[str, object],
    *,
    expected_paths: dict[str, str] | None = None,
    read_only: bool = False,
) -> str | None:
    if (
        payload.get("runtime") != "rust"
        or payload.get("backend_build_fingerprint")
        != manifest["build_info"]["backend_build_fingerprint"]
        or payload.get("build_info") != manifest["build_info"]
    ):
        return "服务程序与候选发布身份不一致"
    paths = expected_paths or manifest["paths"]
    for key, expected in paths.items():
        actual = payload.get("paths", {}).get(key)
        if not isinstance(actual, str) or Path(actual).resolve() != Path(expected).resolve():
            return f"服务持久路径不一致: {key}"
    version = payload.get("snapshot_version")
    bounds = payload.get("capture_retained_bounds")
    if (
        not isinstance(version, dict)
        or not version.get("store_epoch")
        or not version.get("active_generation")
        or payload.get("generation") != version["active_generation"]
    ):
        return "缺少实际事实版本或恢复 generation"
    if any(
        version.get(key) != manifest["build_info"].get(build_key)
        for key, build_key in (
            ("schema_version", "persistence_schema"),
            ("aggregation_version", "aggregation_version"),
        )
    ):
        return "事实格式或派生版本与候选发布身份不一致"
    if (
        not isinstance(bounds, dict)
        or not bounds.get("epoch")
        or bounds.get("state") not in {"ready", "empty"}
        or bounds.get("gaps") != []
    ):
        return "缺少已验证的原始捕获边界"
    if payload.get("database", {}).get("state") != "healthy":
        return "事实库未完成恢复"
    if read_only:
        if (
            payload.get("read_only") is not True
            or payload.get("production_ready") is not False
            or payload.get("status") != "read_only_shadow"
        ):
            return "只读核验未保持只读隔离"
    elif (
        payload.get("read_only") is not False
        or payload.get("production_ready") is not True
        or payload.get("acquisition", {}).get("state") != "running"
        or payload.get("capture", {}).get("state") != "connected"
        or payload.get("acquisition", {}).get("projection", {}).get("evidence_complete") is not True
    ):
        return "原始入口或投影恢复尚未就绪"
    return None


def wait_until_ready(
    *,
    timeout_seconds: float = 120,
    runtime_root: Path | None = None,
    port: int = 8000,
    read_only: bool = False,
    expected_paths: dict[str, str] | None = None,
) -> dict[str, object]:
    root = runtime_root or installed_runtime()
    manifest = verify_runtime_manifest(root)
    deadline = time.monotonic() + timeout_seconds
    last_error = "服务尚未响应"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/ready", timeout=2
            ) as response:
                payload = json.load(response)
            problem = readiness_problem(
                payload, manifest, expected_paths=expected_paths, read_only=read_only
            )
            if problem is None:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as page:
                    body = page.read(4 * 1024 * 1024 + 1)
                    if (
                        page.status != 200
                        or hashlib.sha256(body).hexdigest()
                        != manifest["files"]["web/dist/index.html"]
                    ):
                        raise ServiceError("实际网页与候选发布产物不一致")
                return payload
            last_error = problem
            if "不一致" in problem:
                raise ServiceError(problem)
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = str(error)
        time.sleep(1)
    raise ServiceError(f"服务启动超时: {last_error}; 请查看 {STDERR_LOG}")


def validate_candidate(
    runtime_root: Path, *, data_root: Path | None = None, read_only: bool = False
) -> dict[str, object]:
    manifest = verify_runtime_manifest(runtime_root)
    environment = backend_environment(runtime_root)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    if data_root is None:
        validation_root = APPLICATION_SUPPORT.parent / "TraceFang-validation"
        validation_root.mkdir(parents=True, exist_ok=True)
        data_root = Path(tempfile.mkdtemp(prefix="prepared-", dir=validation_root))
        store, capture = data_root / "facts.redb", data_root / "capture.redb"
    else:
        store, capture = Path(manifest["paths"]["store"]), Path(manifest["paths"]["capture"])
    expected = {"data": str(data_root), "store": str(store), "capture": str(capture)}
    environment.update(
        {
            "TRACEFANG_PORT": str(port),
            "TRACEFANG_DATA_DIR": str(data_root),
            "TRACEFANG_STORE_PATH": str(store),
            "TRACEFANG_CAPTURE_PATH": str(capture),
            "TRACEFANG_ACQUISITION_ENABLED": "0",
            "TRACEFANG_READ_ONLY_SHADOW": "1" if read_only else "0",
            "TRACEFANG_WEB_DIST": str(runtime_root / "web/dist"),
            "PYTHONPATH": str(runtime_root / "src"),
            "TRACEFANG_QUANT_RESULTS_DIR": str(data_root / "quant-results"),
            "TRACEFANG_BATCH_SNAPSHOTS_DIR": str(data_root / "batch-snapshots"),
        }
    )
    environment.pop("TRACEFANG_REHEARSAL_GENERATION", None)
    shutdown = runtime_root / f".validation-shutdown-{time.time_ns()}"
    environment["TRACEFANG_SHUTDOWN_FILE"] = str(shutdown)
    log = runtime_root / "candidate-validation.log"
    started = time.time_ns()
    with log.open("ab") as output:
        process = subprocess.Popen(
            [str(backend_executable(runtime_root))],
            cwd=runtime_root,
            env=environment,
            stdout=output,
            stderr=output,
        )
        try:
            payload = wait_until_ready(
                runtime_root=runtime_root,
                port=port,
                expected_paths=expected,
                read_only=read_only,
                timeout_seconds=60,
            )
            if payload.get("process_id") != process.pid:
                raise ServiceError("候选端口进程与本次启动身份不一致")
        finally:
            shutdown.write_text("stop", encoding="utf-8")
            try:
                code = process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise ServiceError("候选未能按协议关闭; 保留核验目录和日志") from None
            finally:
                shutdown.unlink(missing_ok=True)
    if code != 0:
        raise ServiceError("候选核验后退出失败; 保留日志和数据")
    write_runtime_json(
        runtime_root / "candidate-validation.json",
        {
            "schema": "tracefang-candidate-validation-v1",
            "ready": payload,
            "read_only": read_only,
            "acquisition_enabled": False,
            "started_at_ns": str(started),
            "closed_at_ns": str(time.time_ns()),
            "exit_code": code,
        },
    )
    return payload


def start_service(*, open_browser: bool, restart: bool = False) -> None:
    if not SERVICE_REGISTRATION.is_file():
        raise ServiceError(f"尚未注册独立运行版本, 请先运行 {UPDATE_ENTRY}")
    virtualenv_python(installed_runtime())
    backend_executable(installed_runtime())
    if service_is_loaded():
        if restart:
            stop_backend()
            register_service()
        else:
            try:
                wait_until_ready(timeout_seconds=2)
            except ServiceError:
                # A loaded service may still be starting. Give that attempt time to finish.
                try:
                    wait_until_ready(timeout_seconds=30)
                except ServiceError:
                    stop_backend()
                    register_service()
            else:
                print("[TraceFang] 服务已在运行")
                if open_browser:
                    open_interface()
                return
    else:
        ensure_port_available()
        register_service()
    try:
        wait_until_ready()
    except ServiceError:
        stop_backend()
        raise
    print("[TraceFang] 服务已独立运行: http://127.0.0.1:8000")
    if open_browser:
        open_interface()


def prepare_service(
    *,
    rebuild: bool = False,
    backend_artifact: Path | None = None,
    web_build_receipt: Path | None = None,
) -> Path:
    APPLICATION_SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate = Path(tempfile.mkdtemp(prefix="release-", dir=APPLICATION_SUPPORT))
    try:
        if backend_artifact is None and web_build_receipt is None:
            deploy_runtime(candidate)
        else:
            deploy_runtime(
                candidate, backend_artifact=backend_artifact, web_build_receipt=web_build_receipt
            )
        validate_candidate(candidate)
    except Exception:
        write_runtime_json(
            candidate / "preparation-status.json",
            {"state": "failed", "data_and_candidate_preserved": True},
        )
        raise
    print(f"[TraceFang] 候选已准备并隔离核验: {candidate}")
    return candidate


def rollback_is_safe(
    before: dict[str, object] | None,
    after: dict[str, object],
    previous_manifest: dict[str, object],
    candidate_manifest: dict[str, object],
) -> bool:
    if before is None:
        return False
    old, new = previous_manifest["build_info"], candidate_manifest["build_info"]
    if any(old.get(key) != new.get(key) for key in ("persistence_schema", "aggregation_version")):
        return False
    if before.get("paths") != after.get("paths"):
        return False
    a, b = before.get("capture_retained_bounds"), after.get("capture_retained_bounds")
    if not isinstance(a, dict) or not isinstance(b, dict) or a.get("epoch") != b.get("epoch"):
        return False
    if (
        a.get("last_position") != b.get("last_position")
        or a.get("last_sequence") != b.get("last_sequence")
        or b.get("gaps") != []
    ):
        return False
    # Preserve any facts/config commit made by the candidate; no inferred downgrade.
    return before.get("snapshot_version") == after.get("snapshot_version")


def activate_runtime(candidate: Path, *, open_browser: bool = False) -> None:
    manifest = verify_runtime_manifest(candidate)
    previous = installed_runtime()
    was_loaded = service_is_loaded()
    previous_plist = SERVICE_REGISTRATION.read_bytes() if SERVICE_REGISTRATION.exists() else None
    previous_manifest = None
    if (previous / "release-manifest.json").is_file():
        previous_manifest = verify_runtime_manifest(previous)
    if was_loaded and previous_manifest is None:
        raise ServiceError(
            f"旧版仍在采集, 候选已保留在 {candidate}; 须先完成稳定原始尾与最终事实的可核验交接"
        )
    before = wait_until_ready(runtime_root=previous, timeout_seconds=5) if was_loaded else None
    if previous_plist is not None and previous_manifest is None:
        proof = validate_candidate(
            candidate, data_root=Path(manifest["paths"]["data"]), read_only=True
        )
        boundary = proof.get("projection_start_boundary")
        if not isinstance(boundary, dict) or boundary.get("production_terminal") is not True:
            raise ServiceError("首次原生启动缺少最终停止/排空后的正式交接边界; 候选及数据已保留")
    if was_loaded:
        stop_backend()
    registered = False
    try:
        ensure_port_available()
        register_service(candidate)
        registered = True
        ready = wait_until_ready(runtime_root=candidate)
        write_runtime_json(
            candidate / "activation-status.json",
            {
                "state": "active",
                "ready": ready,
                "previous_runtime": str(previous),
                "activated_at_ns": str(time.time_ns()),
            },
        )
    except Exception:
        if registered:
            stop_backend()
        restored = False
        if was_loaded and previous_manifest is not None:
            try:
                after = validate_candidate(
                    candidate, data_root=Path(manifest["paths"]["data"]), read_only=True
                )
                if rollback_is_safe(before, after, previous_manifest, manifest):
                    register_service(previous)
                    wait_until_ready(runtime_root=previous)
                    restored = True
            except Exception:
                pass
        if not registered and previous_plist is not None:
            SERVICE_REGISTRATION.write_bytes(previous_plist)
        write_runtime_json(
            candidate / "activation-status.json",
            {
                "state": "failed",
                "previous_restored": restored,
                "data_and_candidate_preserved": True,
                "reason": "仅在原始尾、事实版本与格式政策均未变化时恢复旧原生版本",
            },
        )
        if restored:
            print("[TraceFang] 已核对无新输入/事实变更, 恢复上一原生版本")
        else:
            print(f"[TraceFang] 未证明安全回退, 保持停止并保留候选/数据: {candidate}")
        raise
    print("[TraceFang] 新原生版本已就绪; 上一版本和全部数据保留")
    if open_browser:
        open_interface()


def update_service(*, rebuild: bool, open_browser: bool) -> None:
    activate_runtime(prepare_service(rebuild=rebuild), open_browser=open_browser)


def stop_service(*, uninstall: bool = False, strict: bool = False) -> None:
    stop_backend()
    if IS_WINDOWS:
        if uninstall:
            from tracefang.windows_service import task_operation

            task_operation("uninstall")
    else:
        subprocess.run(["launchctl", "disable", SERVICE_TARGET], check=True)
    # Native shutdown never operates on the retained legacy containers or volumes.
    if uninstall and SERVICE_REGISTRATION.exists():
        SERVICE_REGISTRATION.unlink()
    print("[TraceFang] 服务已停止, 数据和已安装版本保留")


def application_session() -> None:
    """The native window owns stdin; close or crash releases the service lease."""
    # A second window must never acquire or stop the first window's backend.
    with operation_lock(timeout_seconds=0, filename="application.lock"):
        try:
            with operation_lock():
                start_service(open_browser=False)
            print("TRACEFANG_APP_READY", flush=True)
            sys.stdin.read()  # EOF on native window close, including a crashed UI.
        finally:
            with operation_lock():
                stop_service(strict=True)
            print("TRACEFANG_APP_STOPPED", flush=True)


def print_status() -> int:
    loaded = service_is_loaded()
    print(f"[TraceFang] 后台服务: {'运行中' if loaded else '未运行'}")
    if not loaded:
        return 1
    try:
        payload = wait_until_ready(timeout_seconds=2)
    except ServiceError as error:
        print(f"[TraceFang] 健康检查失败: {error}")
        return 1
    database = payload.get("database", {})
    acquisition = payload.get("acquisition", {})
    print(f"[TraceFang] 数据库: {database.get('state', 'unknown')}")
    print(f"[TraceFang] 行情采集: {acquisition.get('state', 'unknown')}")
    print(f"[TraceFang] 消息连接: {payload.get('capture', {}).get('state', 'unknown')}")
    return 0


def run_backend(
    executable: Path,
    *,
    project_root: Path | None = None,
    arguments: list[str] | None = None,
    managed: bool = False,
) -> int:
    root = project_root or PROJECT_ROOT
    environment = backend_environment(root)
    environment["PYTHONPATH"] = str(root / "src")
    environment["TRACEFANG_PYTHON"] = str(virtualenv_python(root))
    environment.setdefault("TRACEFANG_WEB_DIST", str(root / "web/dist"))
    shutdown = root / ".runtime/shutdown"
    shutdown.parent.mkdir(parents=True, exist_ok=True)
    if not managed:
        shutdown.unlink(missing_ok=True)
    environment["TRACEFANG_SHUTDOWN_FILE"] = str(shutdown)
    command = [str(executable), *(arguments or [])]
    if IS_WINDOWS:
        from tracefang.windows_service import attach_kill_on_exit_job

        attach_kill_on_exit_job()
        return subprocess.run(
            command, cwd=root, env=environment, stdout=sys.stdout, stderr=sys.stderr, check=False
        ).returncode
    os.chdir(root)
    os.execve(str(executable), command, environment)
    return 1


def backend_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    from dotenv import dotenv_values

    for name in (".env.local", ".env"):
        for key, value in dotenv_values(root / name).items():
            if value is not None:
                environment.setdefault(key, value)
    # Only the isolated acceptance runner may add fixed source bodies to its process.
    for key in (
        "TRACEFANG_SOURCE_PERIOD_FIXTURE_MANIFEST",
        "TRACEFANG_SOURCE_PERIOD_FIXTURE_SHA256",
    ):
        environment.pop(key, None)
    for protocol, proxy in urllib.request.getproxies().items():
        if protocol not in {"http", "https", "all"}:
            continue
        key = f"{protocol}_proxy"
        if key not in environment and key.upper() not in environment:
            environment[key] = proxy
    bypass = environment.get("no_proxy", environment.get("NO_PROXY", ""))
    environment["no_proxy"] = ",".join(
        dict.fromkeys([*filter(None, bypass.split(",")), "127.0.0.1", "localhost", "::1"])
    )
    environment["TRACEFANG_PYTHON"] = str(virtualenv_python(root))
    environment.setdefault("TRACEFANG_DATA_DIR", str(APPLICATION_SUPPORT))
    data = Path(environment["TRACEFANG_DATA_DIR"]).expanduser()
    if not data.is_absolute():
        data = root / data
    environment["TRACEFANG_DATA_DIR"] = str(data.resolve())
    environment.setdefault("TRACEFANG_QUANT_RESULTS_DIR", str(data / "quant-results"))
    environment.setdefault("TRACEFANG_BATCH_SNAPSHOTS_DIR", str(data / "batch-snapshots"))
    environment.setdefault(
        "TRACEFANG_DUCKDB_PATH",
        str(
            APPLICATION_SUPPORT
            / "runtime/duckdb/1.5.6"
            / ("duckdb.exe" if IS_WINDOWS else "duckdb")
        ),
    )
    for key in (
        "TRACEFANG_QUANT_RESULTS_DIR",
        "TRACEFANG_BATCH_SNAPSHOTS_DIR",
        "TRACEFANG_STORE_PATH",
        "TRACEFANG_CAPTURE_PATH",
        "TRACEFANG_DUCKDB_PATH",
    ):
        if key in environment:
            path = Path(environment[key]).expanduser()
            environment[key] = str((path if path.is_absolute() else root / path).resolve())
    return environment


def server_main() -> int:
    """Console entry for the Rust server, without a Python API fallback."""
    try:
        executable = backend_executable(PROJECT_ROOT)
    except ServiceError:
        executable = build_backend()
    return run_backend(executable, arguments=sys.argv[1:])


def run_server() -> int:
    if IS_WINDOWS:
        LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
        sys.stdout = STDOUT_LOG.open("a", encoding="utf-8", buffering=1)
        sys.stderr = STDERR_LOG.open("a", encoding="utf-8", buffering=1)
    return run_backend(backend_executable(PROJECT_ROOT), managed=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="管理 TraceFang 后台服务")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="打开已安装版本")
    start_parser.add_argument("--no-browser", action="store_true", help="启动后不打开浏览器")
    restart_parser = subparsers.add_parser("restart", help="重启已安装版本")
    restart_parser.add_argument("--no-browser", action="store_true")
    for command in ("install", "update"):
        update_parser = subparsers.add_parser(command, help="准备并启用本地代码运行版本")
        update_parser.add_argument("--rebuild", action="store_true")
        update_parser.add_argument("--no-browser", action="store_true")
    subparsers.add_parser("prepare", help="准备候选并隔离核验, 不停止当前服务")
    activation = subparsers.add_parser(
        "activate", help="启用已准备候选; 首次原生迁移要求已核验交接边界"
    )
    activation.add_argument("--runtime", type=Path, required=True)
    subparsers.add_parser("stop", help="停止项目服务并保留安装")
    subparsers.add_parser("uninstall", help="停止并移除系统托管注册, 保留数据")
    subparsers.add_parser("status", help="检查后台服务状态")
    subparsers.add_parser("run", help=argparse.SUPPRESS)
    subparsers.add_parser("session", help="由应用窗口持有服务生命周期")
    subparsers.add_parser("stop-app", help="确认项目服务全部停止")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "run":
            return run_server()
        if args.command == "session":
            application_session()
            return 0
        if args.command == "status":
            return print_status()
        with operation_lock():
            if args.command in ("install", "update", "activate"):
                migrate_registration()
            if args.command in ("stop", "stop-app", "uninstall"):
                stop_service(
                    uninstall=args.command == "uninstall", strict=args.command == "stop-app"
                )
            elif args.command == "prepare":
                prepare_service()
            elif args.command == "activate":
                activate_runtime(args.runtime.resolve())
            elif args.command in ("install", "update"):
                update_service(rebuild=args.rebuild, open_browser=not args.no_browser)
            else:
                start_service(open_browser=not args.no_browser, restart=args.command == "restart")
        return 0
    except ApplicationAlreadyOpen as error:
        print(f"[TraceFang] {error}", file=sys.stderr)
        return 3
    except (OSError, ServiceError, subprocess.SubprocessError) as error:
        print(f"[TraceFang] 操作失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
