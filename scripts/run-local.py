#!/usr/bin/env python3
"""Run TraceFang locally with one owner for every long-lived process."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def command(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(f"缺少命令 {name!r}, 请先完成项目安装")
    return resolved


def package_manager() -> str:
    package = json.loads((PROJECT_ROOT / "web" / "package.json").read_text())
    return str(package["packageManager"])


def start_infrastructure() -> None:
    docker = shutil.which("docker")
    env_file = PROJECT_ROOT / ".env.local"
    if docker is None or not env_file.is_file():
        print("[TraceFang] 未启动本机基础设施: Docker 或 .env.local 不可用")
        return
    probe = subprocess.run(
        [docker, "info"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        print("[TraceFang] Docker Desktop 未运行, 后端将报告依赖服务不可用")
        return
    subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_file),
            "up",
            "-d",
            "postgres",
            "nats",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )


def python_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = f"{source_path}{os.pathsep}{existing}" if existing else source_path
    return environment


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix" and process.pid > 1:
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()


def kill_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix" and process.pid > 1:
        os.killpg(process.pid, signal.SIGKILL)
    else:
        process.kill()


def stop_services(services: list[tuple[str, subprocess.Popen[bytes]]]) -> None:
    for _, process in services:
        terminate_process(process)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(process.poll() is None for _, process in services):
        time.sleep(0.05)
    for _, process in services:
        kill_process(process)
    for _, process in services:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)


def run_development() -> int:
    definitions = (
        (
            "后端",
            [
                command("uv"),
                "run",
                "uvicorn",
                "tracefang.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--no-access-log",
                "--reload",
                "--reload-dir",
                "src",
            ],
            PROJECT_ROOT,
        ),
        (
            "前端",
            [command("corepack"), package_manager(), "dev"],
            PROJECT_ROOT / "web",
        ),
    )
    services: list[tuple[str, subprocess.Popen[bytes]]] = []
    try:
        for name, argv, working_directory in definitions:
            print(f"[TraceFang] 启动{name}: {' '.join(argv)}")
            services.append(
                (
                    name,
                    subprocess.Popen(
                        argv,
                        cwd=working_directory,
                        env=python_environment() if name == "后端" else None,
                        start_new_session=os.name == "posix",
                    ),
                )
            )
        print("[TraceFang] 开发页面: http://127.0.0.1:5173")
        while True:
            for name, process in services:
                return_code = process.poll()
                if return_code is not None:
                    print(f"[TraceFang] {name}已退出 (状态 {return_code}), 正在停止另一项服务")
                    return return_code if return_code != 0 else 1
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\n[TraceFang] 收到停止请求")
        return 0
    finally:
        stop_services(services)


def run_production(*, build: bool) -> int:
    if build:
        subprocess.run(
            [command("corepack"), package_manager(), "build"],
            cwd=PROJECT_ROOT / "web",
            check=True,
        )
    print("[TraceFang] 应用页面: http://127.0.0.1:8000")
    python = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise RuntimeError("项目尚未安装, 请先完成 uv sync")
    os.chdir(PROJECT_ROOT)
    os.execve(
        str(python),
        [str(python), "-c", "from tracefang.api import run; run()"],
        python_environment(),
    )
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统一启动 TraceFang 本机服务")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="同时运行受统一生命周期管理的 Vite 与 FastAPI 开发服务",
    )
    parser.add_argument(
        "--no-database",
        action="store_true",
        help="不尝试启动项目 PostgreSQL 容器",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="普通模式下复用现有 web/dist, 不重新构建前端",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not args.no_database:
            start_infrastructure()
        return run_development() if args.dev else run_production(build=not args.no_build)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"[TraceFang] 启动失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
