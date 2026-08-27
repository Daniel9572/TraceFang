from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracefang.environment import load_project_environment


class ProjectEnvironmentTests(unittest.TestCase):
    def test_loads_values_from_local_env_file(self) -> None:
        key = "TRACEFANG_TEST_LOCAL_ENV"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(f"{key}=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(key, None)
                try:
                    self.assertTrue(load_project_environment(Path(directory)))
                    self.assertEqual(os.environ[key], "from-file")
                finally:
                    os.environ.pop(key, None)

    def test_process_environment_takes_priority(self) -> None:
        key = "TRACEFANG_TEST_LOCAL_ENV"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(f"{key}=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {key: "from-process"}, clear=False):
                self.assertTrue(load_project_environment(Path(directory)))
                self.assertEqual(os.environ[key], "from-process")

    def test_dot_env_local_takes_priority_over_base_file(self) -> None:
        key = "TRACEFANG_TEST_LOCAL_ENV"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(f"{key}=from-base\n", encoding="utf-8")
            Path(directory, ".env.local").write_text(f"{key}=from-local\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(key, None)
                try:
                    self.assertTrue(load_project_environment(Path(directory)))
                    self.assertEqual(os.environ[key], "from-local")
                finally:
                    os.environ.pop(key, None)


if __name__ == "__main__":
    unittest.main()
