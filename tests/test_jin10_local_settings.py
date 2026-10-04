from __future__ import annotations

import unittest

from tracefang.infrastructure.providers.jin10_local.settings import Jin10LocalSettings


class Jin10LocalSettingsTests(unittest.TestCase):
    def test_uses_explicit_session_token_without_an_account_id(self) -> None:
        settings = Jin10LocalSettings.from_env(
            {"JIN10_LOCAL_SESSION_TOKEN": "s" * 36}
        )

        credentials = settings.credentials()

        self.assertEqual(credentials.session_token, "s" * 36)
        self.assertFalse(hasattr(credentials, "user_id"))
        self.assertEqual(settings.quote_frequency_ms, 1000)
        self.assertNotIn("s" * 36, repr(settings))

    def test_session_availability_is_not_required_during_static_setup(self) -> None:
        settings = Jin10LocalSettings.from_env({})

        self.assertIsNotNone(settings.session_resolver)

    def test_history_backfill_settings_are_configurable(self) -> None:
        settings = Jin10LocalSettings.from_env(
            {
                "JIN10_LOCAL_SESSION_TOKEN": "s" * 36,
                "JIN10_LOCAL_KLINE_FILE_URL": "https://history.example.test/root/",
                "JIN10_LOCAL_KLINE_FREQUENCY_MS": "4250",
                "JIN10_LOCAL_KLINE_WAIT_TIMEOUT_SECONDS": "14",
                "JIN10_LOCAL_KLINE_DOWNLOAD_TIMEOUT_SECONDS": "23",
            }
        )

        self.assertEqual(settings.kline_file_endpoint, "https://history.example.test/root")
        self.assertEqual(settings.kline_frequency_ms, 4250)
        self.assertEqual(settings.kline_wait_timeout_seconds, 14)
        self.assertEqual(settings.kline_download_timeout_seconds, 23)

    def test_rejects_invalid_token_without_echoing_it(self) -> None:
        token = "too-short"
        with self.assertRaisesRegex(ValueError, "36-character") as context:
            Jin10LocalSettings.from_env({"JIN10_LOCAL_SESSION_TOKEN": token})
        self.assertNotIn(token, str(context.exception))


if __name__ == "__main__":
    unittest.main()
