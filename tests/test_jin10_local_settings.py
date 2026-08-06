from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from market_analysis.infrastructure.providers.jin10_local.settings import Jin10LocalSettings


class Jin10LocalSettingsTests(unittest.TestCase):
    def test_resolves_user_id_from_latest_local_log(self) -> None:
        token = "s" * 36
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory, "log_20260806_002004.txt")
            log.write_text(
                "发送行情登录 userId=8616672 tokenLen=36 vip=false\n",
                encoding="utf-8",
            )
            settings = Jin10LocalSettings.from_env(
                {
                    "JIN10_LOCAL_SESSION_TOKEN": token,
                    "JIN10_LOCAL_LOG_DIRECTORY": directory,
                }
            )
        self.assertEqual(settings.user_id, 8616672)
        self.assertEqual(settings.quote_frequency_ms, 1000)
        self.assertNotIn(token, repr(settings))

    def test_explicit_user_id_does_not_require_desktop_log(self) -> None:
        settings = Jin10LocalSettings.from_env(
            {
                "JIN10_LOCAL_SESSION_TOKEN": "s" * 36,
                "JIN10_LOCAL_USER_ID": "8616672",
            }
        )
        self.assertEqual(settings.user_id, 8616672)

    def test_rejects_invalid_token_without_echoing_it(self) -> None:
        token = "too-short"
        with self.assertRaisesRegex(ValueError, "36-character") as context:
            Jin10LocalSettings.from_env({"JIN10_LOCAL_SESSION_TOKEN": token})
        self.assertNotIn(token, str(context.exception))


if __name__ == "__main__":
    unittest.main()
