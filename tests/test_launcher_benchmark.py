from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "launcher_benchmark", Path(__file__).resolve().parents[1] / "scripts" / "benchmark-launcher.py"
)
assert spec is not None and spec.loader is not None
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class BenchmarkResourceTests(unittest.TestCase):
    def test_windows_reports_cpu_seconds_and_converts_working_set_to_kib(self) -> None:
        with (
            patch.object(benchmark, "os", SimpleNamespace(name="nt")),
            patch.object(
                benchmark.subprocess, "check_output",
                return_value='{"CPU":1.5,"WorkingSet64":1048576}',
            ) as run,
        ):
            self.assertEqual(benchmark.process_resources(123), (1.5, 1024))
        self.assertIn("Get-Process -Id 123 -ErrorAction Stop", run.call_args.args[0][-1])

    def test_macos_uses_ps_percentage_and_kib(self) -> None:
        with (
            patch.object(benchmark, "os", SimpleNamespace(name="posix")),
            patch.object(benchmark.subprocess, "check_output", return_value="2.5 2048"),
        ):
            self.assertEqual(benchmark.process_resources(123), (2.5, 2048))

    def test_invalid_pid_is_rejected_before_process_query(self) -> None:
        with (
            patch.object(benchmark.subprocess, "check_output") as run,
            self.assertRaises(ValueError),
        ):
            benchmark.process_resources(0)
        run.assert_not_called()
