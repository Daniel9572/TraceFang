from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
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
    """Retain the installed release without leaving a login auto-start registration."""
    if not IS_WINDOWS and LEGACY_REGISTRATION.is_file():
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


def _web_inputs(web_directory: Path) -> list[Path]:
    inputs = [
        web_directory / "index.html",
        web_directory / "package.json",
        web_directory / "pnpm-lock.yaml",
        web_directory / "vite.config.ts",
    ]
    inputs.extend(web_directory.glob("tsconfig*.json"))
    source_directory = web_directory / "src"
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


def build_web(*, force: bool = False) -> None:
    if not force and not web_build_required():
        print("[TraceFang] 网页构建已是最新")
        return
    corepack = shutil.which("corepack")
    if corepack is None:
        raise ServiceError("缺少 corepack, 请先完成项目安装")
    package_manager = json.loads((WEB_DIRECTORY / "package.json").read_text(encoding="utf-8"))[
        "packageManager"
    ]
    subprocess.run(
        [corepack, package_manager, "-C", str(WEB_DIRECTORY), "install", "--frozen-lockfile"],
        cwd=PROJECT_ROOT,
        check=True,
        timeout=180,
    )
    print("[TraceFang] 构建网页")
    subprocess.run(
        [corepack, package_manager, "-C", str(WEB_DIRECTORY), "build"],
        cwd=PROJECT_ROOT,
        check=True,
        timeout=180,
    )


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


def deploy_runtime(runtime_root: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise ServiceError("缺少 uv, 请先完成项目安装")
    if not WEB_INDEX.is_file():
        raise ServiceError("缺少网页构建产物, 请先完成网页构建")

    APPLICATION_SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative_path in (
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "compose.yaml",
        "nats-server.conf",
        ".env.local",
    ):
        source = PROJECT_ROOT / relative_path
        if not source.is_file():
            raise ServiceError(f"运行时部署缺少文件: {relative_path}")
        destination = runtime_root / relative_path
        shutil.copy2(source, destination)
        if relative_path == ".env.local":
            destination.chmod(0o600)

    optional_environment = PROJECT_ROOT / ".env"
    deployed_environment = runtime_root / ".env"
    if optional_environment.is_file():
        shutil.copy2(optional_environment, deployed_environment)
        deployed_environment.chmod(0o600)
    elif deployed_environment.exists():
        deployed_environment.unlink()

    for relative_directory in (Path("src"), Path("web") / "dist"):
        source = PROJECT_ROOT / relative_directory
        destination = runtime_root / relative_directory
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
        )

    print("[TraceFang] 同步独立运行环境")
    subprocess.run(
        [uv, "sync", "--project", str(runtime_root), "--python", "3.13", "--frozen", "--no-dev"],
        check=True,
        timeout=180,
    )
    subprocess.run(
        [str(virtualenv_python(runtime_root)), "-c", "import tracefang.api"],
        cwd=runtime_root,
        env={**os.environ, "PYTHONPATH": str(runtime_root / "src")},
        check=True,
        timeout=30,
    )


def launch_agent_payload(
    *,
    python: Path,
    project_root: Path = PROJECT_ROOT,
    log_directory: Path = LOG_DIRECTORY,
    environment_path: str | None = None,
) -> dict[str, object]:
    path_value = environment_path or DEFAULT_SERVICE_PATH
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "tracefang.service",
            "run",
        ],
        "WorkingDirectory": str(project_root),
        "EnvironmentVariables": {
            "PATH": path_value,
            "PYTHONPATH": str(project_root / "src"),
            "PYTHONUNBUFFERED": "1",
        },
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

        task_operation("stop")
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
    python = virtualenv_python(project_root)
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    SERVICE_REGISTRATION.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        {"WorkingDirectory": str(project_root)}
        if IS_WINDOWS
        else launch_agent_payload(
            python=python, project_root=project_root, log_directory=LOG_DIRECTORY
        )
    )
    temporary_path = SERVICE_REGISTRATION.with_suffix(".plist.tmp")
    with temporary_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    os.replace(temporary_path, SERVICE_REGISTRATION)

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


def wait_until_ready(*, timeout_seconds: float = 120) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "服务尚未响应"
    while time.monotonic() < deadline:
        try:
            try:
                response = urllib.request.urlopen("http://127.0.0.1:8000/api/ready", timeout=2)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                # Compatibility with the installed version before readiness was introduced.
                response = urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=2)
            with response:
                payload = json.load(response)
            if (
                payload.get("database", {}).get("state") == "healthy"
                and payload.get("acquisition", {}).get("state") == "running"
                and payload.get("capture", {}).get("state", "connected") == "connected"
            ):
                with urllib.request.urlopen("http://127.0.0.1:8000/", timeout=2) as page:
                    if page.status != 200:
                        raise ServiceError("前端页面不可用")
                return payload
            last_error = f"健康状态为 {payload.get('status', 'unknown')}"
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = str(error)
        time.sleep(1)
    raise ServiceError(f"服务启动超时: {last_error}; 请查看 {STDERR_LOG}")


def start_service(*, open_browser: bool, restart: bool = False) -> None:
    if not SERVICE_REGISTRATION.is_file():
        raise ServiceError(f"尚未注册独立运行版本, 请先运行 {UPDATE_ENTRY}")
    virtualenv_python(installed_runtime())
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


def update_service(*, rebuild: bool, open_browser: bool) -> None:
    # Build and validate in a separate directory while the installed version keeps running.
    build_web(force=rebuild)
    APPLICATION_SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate = Path(tempfile.mkdtemp(prefix="release-", dir=APPLICATION_SUPPORT))
    previous = installed_runtime()
    was_loaded = service_is_loaded()
    previous_plist = SERVICE_REGISTRATION.read_bytes() if SERVICE_REGISTRATION.exists() else None
    try:
        deploy_runtime(candidate)
        if was_loaded:
            stop_backend()
        try:
            ensure_port_available()
            register_service(candidate)
            wait_until_ready()
        except Exception:
            stop_backend()
            if previous_plist is not None:
                SERVICE_REGISTRATION.write_bytes(previous_plist)
            elif SERVICE_REGISTRATION.exists():
                SERVICE_REGISTRATION.unlink()
            if was_loaded:
                register_service(previous)
                wait_until_ready()
                print("[TraceFang] 更新失败, 已恢复上一个运行版本")
            raise
    except Exception:
        # Only this operation's newly created, inactive candidate is disposable.
        if installed_runtime() != candidate:
            shutil.rmtree(candidate)
        raise
    print("[TraceFang] 新版本已就绪; 上一版本保留在本机")
    if open_browser:
        open_interface()


def stop_service(*, uninstall: bool = False, strict: bool = False) -> None:
    root = installed_runtime()
    stop_backend()
    if IS_WINDOWS:
        if uninstall:
            from tracefang.windows_service import task_operation

            task_operation("uninstall")
    else:
        subprocess.run(["launchctl", "disable", SERVICE_TARGET], check=True)
    docker = shutil.which("docker")
    if docker and _docker_is_ready(docker) and (root / "compose.yaml").is_file():
        subprocess.run(
            [docker, "compose", "--env-file", str(root / ".env.local"), "stop", "postgres", "nats"],
            cwd=root,
            check=True,
            timeout=60,
        )
    else:
        if strict:
            raise ServiceError("后端已停止, 但无法确认容器状态; 请检查 Docker 后重试停止")
        print("[TraceFang] Docker 不可用或缺少容器配置, 无法确认容器状态")
    if uninstall and SERVICE_REGISTRATION.exists():
        SERVICE_REGISTRATION.unlink()
    print("[TraceFang] 服务已停止, 数据和已安装版本保留")


def application_session() -> None:
    """The native window owns stdin; close or crash releases the service lease."""
    # A second window must never acquire or stop the first window's backend.
    with operation_lock(timeout_seconds=0, filename="application.lock"):
        try:
            with operation_lock():
                migrate_registration()
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


def run_server() -> None:
    if IS_WINDOWS:
        LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
        sys.stdout = STDOUT_LOG.open("a", encoding="utf-8", buffering=1)
        sys.stderr = STDERR_LOG.open("a", encoding="utf-8", buffering=1)
        docker_bin = (
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Docker"
            / "Docker"
            / "resources"
            / "bin"
        )
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(docker_bin)
    start_infrastructure()
    if IS_WINDOWS:
        # Run inside the scheduled task, so its exit status and lifetime are the server's.
        from tracefang.api import run

        run()
        return
    python = virtualenv_python()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    os.execve(
        str(python),
        [str(python), "-c", "from tracefang.api import run; run()"],
        environment,
    )


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
            run_server()
            return 0
        if args.command == "session":
            application_session()
            return 0
        if args.command == "status":
            with operation_lock():
                migrate_registration()
            return print_status()
        with operation_lock():
            migrate_registration()
            if args.command in ("stop", "stop-app", "uninstall"):
                stop_service(
                    uninstall=args.command == "uninstall", strict=args.command == "stop-app"
                )
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
