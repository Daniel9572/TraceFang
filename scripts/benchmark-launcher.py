"""Opt-in local launcher experiments. Recovery/cold tests interrupt market acquisition."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = f"gui/{os.getuid()}/com.tracefang.local" if os.name != "nt" else ""


def pid() -> int:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/ready", timeout=2) as response:
        process_id = json.load(response).get("process_id")
    if isinstance(process_id, int) and process_id > 0:
        return process_id
    if os.name == "nt":
        raise RuntimeError("Update the runtime before running Windows experiments")
    output = subprocess.check_output(["launchctl", "print", TARGET], text=True)
    match = re.search(r"^\s*pid = (\d+)$", output, re.MULTILINE)
    if match is None:
        raise RuntimeError("Managed service has no running process")
    return int(match[1])


def service_command(action: str) -> list[str]:
    if os.name == "nt":
        # Exercise the common core without interactive error pauses in .cmd wrappers.
        os.environ["PYTHONPATH"] = str(ROOT / "src")
        return [sys.executable, "-m", "tracefang.service", action]
    return [str(ROOT / f"{action}.command")]


def start() -> float:
    began = time.monotonic()
    result = subprocess.run(
        [*service_command("start"), "--no-browser"],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return time.monotonic() - began


def summary(values: list[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "median_s": statistics.median(values),
        "p95_s": sorted(values)[math.ceil(len(values) * 0.95) - 1],
        "max_s": max(values),
    }


def report(name: str, result: object) -> None:
    print(json.dumps({name: result}), flush=True)


async def warm_experiments() -> None:
    from websockets.asyncio.client import connect

    before = pid()
    async with connect("ws://127.0.0.1:8000/api/stream/quotes/XAUUSD") as stream:
        await asyncio.wait_for(stream.recv(), timeout=10)

        async def drain() -> None:
            # A busy feed must not fill the receive queue and block control frames.
            async for _ in stream:
                pass

        reader = asyncio.create_task(drain())
        try:
            warm = [await asyncio.to_thread(start) for _ in range(30)]
            with ThreadPoolExecutor(max_workers=10) as pool:
                concurrent = await asyncio.to_thread(
                    lambda: list(pool.map(lambda _: start(), range(10)))
                )
            pong = await stream.ping()
            await asyncio.wait_for(pong, timeout=5)
            if reader.done():
                await reader
                raise RuntimeError("Existing WebSocket closed during repeated starts")
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        assert pid() == before, "Repeated start replaced the backend"
        report("warm", summary(warm))
        report("concurrent", summary(concurrent))
        report("continuity", "same backend PID; existing WebSocket answered ping after 40 starts")


def process_resources(process_id: int) -> tuple[float, int]:
    """Return CPU (seconds on Windows, ps percentage on macOS) and RSS in KiB."""
    if process_id <= 0:
        raise ValueError("Expected a positive backend PID")
    if os.name == "nt":
        output = subprocess.check_output(
            [
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                f"Get-Process -Id {int(process_id)} -ErrorAction Stop | "
                "Select-Object CPU,WorkingSet64 | ConvertTo-Json -Compress",
            ],
            text=True,
            timeout=10,
        )
        sample = json.loads(output)
        return float(sample["CPU"]), int(sample["WorkingSet64"]) // 1024
    cpu, rss = subprocess.check_output(
        ["ps", "-p", str(process_id), "-o", "%cpu=,rss="], text=True, timeout=10
    ).split()
    return float(cpu), int(rss)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cold-runs", type=int, default=0)
    parser.add_argument("--recovery", action="store_true")
    parser.add_argument("--idle-seconds", type=int, default=0)
    args = parser.parse_args()
    if args.cold_runs < 0 or args.idle_seconds < 0:
        parser.error("Experiment counts and durations cannot be negative")
    asyncio.run(warm_experiments())
    if args.cold_runs:
        samples = []
        for _ in range(args.cold_runs):
            subprocess.run(service_command("stop"), check=True, capture_output=True, timeout=90)
            samples.append(start())
        report("cold_docker_running", summary(samples))
    if args.recovery:
        before = pid()
        # The PID comes from this project's readiness endpoint, never a broad process match.
        began = time.monotonic()
        os.kill(before, signal.SIGTERM)
        deadline = began + 120
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8000/api/ready", timeout=2
                ) as response:
                    ready = json.load(response)["status"] == "ok"
                if ready and pid() != before:
                    report("automatic_recovery_s", time.monotonic() - began)
                    break
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Automatic recovery timed out")
    if args.idle_seconds:
        before = pid()
        samples = []
        deadline = time.monotonic() + args.idle_seconds
        last_cpu, _ = process_resources(before)
        last_time = time.monotonic()
        while time.monotonic() < deadline:
            if os.name == "nt":
                time.sleep(min(10, max(0, deadline - time.monotonic())))
            cpu, rss = process_resources(before)
            now = time.monotonic()
            percent = (cpu - last_cpu) / (now - last_time) * 100 if os.name == "nt" else cpu
            samples.append((percent, rss))
            last_cpu, last_time = cpu, now
            if len(samples) % 6 == 0:
                report("resource_progress", {"samples": len(samples), "rss_mib": int(rss) / 1024})
            if os.name != "nt":
                time.sleep(min(10, max(0, deadline - time.monotonic())))
        assert pid() == before, "Backend restarted during observation"
        report(
            "resources",
            {
                "seconds": args.idle_seconds,
                "samples": len(samples),
                "cpu_mean_percent": statistics.mean(s[0] for s in samples),
                "cpu_method": "process CPU time deltas" if os.name == "nt" else "ps %cpu",
                "rss_start_mib": samples[0][1] / 1024,
                "rss_end_mib": samples[-1][1] / 1024,
                "rss_peak_mib": max(s[1] for s in samples) / 1024,
            },
        )


if __name__ == "__main__":
    main()
