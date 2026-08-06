from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_analysis.environment import load_project_environment


class ProjectEnvironmentTests(unittest.TestCase):
    def test_loads_values_from_local_env_file(self) -> None:
        key = "MARKET_ANALYSIS_TEST_LOCAL_ENV"
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
        key = "MARKET_ANALYSIS_TEST_LOCAL_ENV"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(f"{key}=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {key: "from-process"}, clear=False):
                self.assertTrue(load_project_environment(Path(directory)))
                self.assertEqual(os.environ[key], "from-process")


if __name__ == "__main__":
    unittest.main()
