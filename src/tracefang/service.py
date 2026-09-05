from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE_LABEL = "com.tracefang.local"
SERVICE_DOMAIN = f"gui/{os.getuid()}"
SERVICE_TARGET = f"{SERVICE_DOMAIN}/{SERVICE_LABEL}"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
APPLICATION_SUPPORT = Path.home() / "Library" / "Application Support" / "TraceFang"
RUNTIME_ROOT = APPLICATION_SUPPORT / "runtime"
LOG_DIRECTORY = Path.home() / "Library" / "Logs" / "TraceFang"
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


def virtualenv_python(project_root: Path = PROJECT_ROOT) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    executable = project_root / ".venv" / relative
    if not executable.is_file():
        raise ServiceError("项目尚未安装, 请先运行 setup.cmd 或完成 uv sync")
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
    print("[TraceFang] 构建网页")
    subprocess.run(
        [corepack, package_manager, "-C", str(WEB_DIRECTORY), "build"],
        cwd=PROJECT_ROOT,
        check=True,
    )


def _docker_command() -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise ServiceError("未找到 Docker, 请安装并启动 Docker Desktop")
    return docker


def _docker_is_ready(docker: str) -> bool:
    return (
        subprocess.run(
            [docker, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def ensure_docker_ready(*, timeout_seconds: float = 90) -> str:
    docker = _docker_command()
    if _docker_is_ready(docker):
        return docker
    if sys.platform != "darwin":
        raise ServiceError("Docker 服务未运行")
    print("[TraceFang] 正在启动 Docker Desktop")
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
    )


def deploy_runtime() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise ServiceError("缺少 uv, 请先完成项目安装")
    if not WEB_INDEX.is_file():
        raise ServiceError("缺少网页构建产物, 请先完成网页构建")

    APPLICATION_SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        destination = RUNTIME_ROOT / relative_path
        shutil.copy2(source, destination)
        if relative_path == ".env.local":
            destination.chmod(0o600)

    optional_environment = PROJECT_ROOT / ".env"
    deployed_environment = RUNTIME_ROOT / ".env"
    if optional_environment.is_file():
        shutil.copy2(optional_environment, deployed_environment)
        deployed_environment.chmod(0o600)
    elif deployed_environment.exists():
        deployed_environment.unlink()

    for relative_directory in (Path("src"), Path("web") / "dist"):
        source = PROJECT_ROOT / relative_directory
        destination = RUNTIME_ROOT / relative_directory
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)

    print("[TraceFang] 同步独立运行环境")
    subprocess.run(
        [uv, "sync", "--project", str(RUNTIME_ROOT), "--python", "3.13", "--frozen"],
        check=True,
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
        "ProcessType": "Background",
        "StandardOutPath": str(log_directory / "tracefang-server.log"),
        "StandardErrorPath": str(log_directory / "tracefang-server.error.log"),
    }


def _boot_out_service() -> None:
    subprocess.run(
        ["launchctl", "bootout", SERVICE_TARGET],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    time.sleep(1)


def install_launch_agent() -> None:
    if sys.platform != "darwin":
        raise ServiceError("系统后台托管入口目前仅支持 macOS")
    python = virtualenv_python(RUNTIME_ROOT)
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = launch_agent_payload(
        python=python,
        project_root=RUNTIME_ROOT,
        log_directory=LOG_DIRECTORY,
    )
    temporary_path = LAUNCH_AGENT_PATH.with_suffix(".plist.tmp")
    with temporary_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    os.replace(temporary_path, LAUNCH_AGENT_PATH)

    subprocess.run(
        ["launchctl", "bootstrap", SERVICE_DOMAIN, str(LAUNCH_AGENT_PATH)],
        check=True,
    )
    subprocess.run(["launchctl", "enable", SERVICE_TARGET], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", SERVICE_TARGET], check=True)


def service_is_loaded() -> bool:
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
            with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=2) as response:
                payload = json.load(response)
            if payload.get("status") == "ok":
                return payload
            last_error = f"健康状态为 {payload.get('status', 'unknown')}"
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = str(error)
        time.sleep(1)
    raise ServiceError(f"服务启动超时: {last_error}; 请查看 {STDERR_LOG}")


def start_service(*, rebuild: bool, open_browser: bool) -> None:
    virtualenv_python()
    build_web(force=rebuild)
    _boot_out_service()
    deploy_runtime()
    install_launch_agent()
    wait_until_ready()
    print("[TraceFang] 服务已独立运行: http://127.0.0.1:8000")
    if open_browser:
        subprocess.run(["open", "http://127.0.0.1:8000"], check=False)


def stop_service() -> None:
    _boot_out_service()
    if LAUNCH_AGENT_PATH.exists():
        LAUNCH_AGENT_PATH.unlink()
    print("[TraceFang] 服务已停止")


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
    return 0


def run_server() -> None:
    start_infrastructure()
    python = virtualenv_python()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    os.execve(
        str(python),
        [str(python), "-c", "from tracefang.api import run; run()"],
        environment,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="管理 TraceFang macOS 后台服务")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="安装并启动后台服务")
    start_parser.add_argument("--rebuild", action="store_true", help="强制重建网页")
    start_parser.add_argument("--no-browser", action="store_true", help="启动后不打开浏览器")
    subparsers.add_parser("restart", help="重装并重启后台服务")
    subparsers.add_parser("stop", help="停止并移除后台服务")
    subparsers.add_parser("status", help="检查后台服务状态")
    subparsers.add_parser("run", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "run":
            run_server()
        if args.command == "stop":
            stop_service()
            return 0
        if args.command == "status":
            return print_status()
        rebuild = args.rebuild if args.command == "start" else False
        no_browser = args.no_browser if args.command == "start" else False
        start_service(rebuild=rebuild, open_browser=not no_browser)
        return 0
    except (OSError, ServiceError, subprocess.CalledProcessError) as error:
        print(f"[TraceFang] 操作失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
