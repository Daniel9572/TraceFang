"""Windows Task Scheduler adapter; orchestration remains in tracefang.service."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

_job_handle: int | None = None


def attach_kill_on_exit_job() -> None:
    """Keep the Rust child in a job owned by its scheduled Python supervisor."""
    import ctypes
    from ctypes import wintypes

    global _job_handle
    if _job_handle is not None:
        return

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io_counters", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    # Keep the non-inheritable handle until process exit. Children inherit the job,
    # so terminating the scheduled task cannot leave a detached backend behind.
    _job_handle = job


def task_operation(action: str, *, project_root: Path | None = None) -> dict[str, object]:
    if action not in {"install", "stop", "status", "uninstall"}:
        raise ValueError("Unsupported task operation")
    if action == "install" and (
        project_root is None or not (project_root / ".venv" / "Scripts" / "pythonw.exe").is_file()
    ):
        raise OSError("Windows runtime is missing pythonw.exe")
    request = {"action": action, "root": str(project_root) if project_root else None}
    script = Path(__file__).with_name("windows_task.ps1").read_text(encoding="utf-8")
    command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        env={**os.environ, "TRACEFANG_TASK_REQUEST": json.dumps(request)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=70,
    )
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload.get("running"), bool):
            raise ValueError("missing running state")
    except (ValueError, AttributeError) as error:
        raise OSError("Invalid Windows task status response") from error
    return payload
