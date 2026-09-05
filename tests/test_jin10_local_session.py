from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tracefang.infrastructure.providers.jin10_local.session import Jin10SessionResolver


class Jin10SessionResolverTests(unittest.TestCase):
    @staticmethod
    def _write_macos_session(
        home: Path,
        *,
        token: str = "d" * 36,
    ) -> Path:
        support = home / "Library" / "Application Support" / "com.jin10.desktop"
        support.mkdir(parents=True)
        storage = support / "local_storage.json"
        storage.write_text(json.dumps({"ji10_token": token}), encoding="utf-8")
        return storage

    def test_explicit_token_overrides_desktop_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self._write_macos_session(home)
            resolver = Jin10SessionResolver(
                env={
                    "JIN10_LOCAL_SESSION_TOKEN": "e" * 36,
                },
                home_directory=home,
                platform_name="darwin",
            )

            credentials = resolver.resolve()

        self.assertEqual(credentials.session_token, "e" * 36)
        self.assertEqual(credentials.origin, "environment")

    def test_discovers_macos_desktop_token_without_reading_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self._write_macos_session(home, token="a" * 36)
            log_directory = (
                home
                / "Library"
                / "Application Support"
                / "com.jin10.desktop"
                / "log"
            )
            log_directory.mkdir()
            (log_directory / "log_20260828_020304.txt").write_text(
                "发送行情登录 userId=2222222 tokenLen=36 vip=false\n",
                encoding="utf-8",
            )
            resolver = Jin10SessionResolver(
                env={},
                home_directory=home,
                platform_name="darwin",
            )

            credentials = resolver.resolve()

        self.assertEqual(credentials.session_token, "a" * 36)
        self.assertEqual(credentials.origin, "desktop")
        self.assertNotIn("a" * 36, repr(credentials))
        self.assertFalse(hasattr(credentials, "user_id"))

    def test_force_refresh_reads_replaced_desktop_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            storage = self._write_macos_session(home, token="a" * 36)
            resolver = Jin10SessionResolver(
                env={},
                home_directory=home,
                platform_name="darwin",
            )
            first = resolver.resolve()
            storage.write_text(json.dumps({"ji10_token": "b" * 36}), encoding="utf-8")

            cached = resolver.resolve()
            refreshed = resolver.resolve(refresh=True)

        self.assertEqual(cached, first)
        self.assertEqual(refreshed.session_token, "b" * 36)
        self.assertNotEqual(refreshed, first)

    def test_rejects_symlinked_or_foreign_owned_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory, "home")
            support = home / "Library" / "Application Support" / "com.jin10.desktop"
            support.mkdir(parents=True)
            outside = Path(directory, "outside.json")
            outside.write_text(json.dumps({"ji10_token": "a" * 36}), encoding="utf-8")
            (support / "local_storage.json").symlink_to(outside)
            resolver = Jin10SessionResolver(
                env={},
                home_directory=home,
                platform_name="darwin",
            )

            with self.assertRaisesRegex(ValueError, "regular client file"):
                resolver.resolve()

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self._write_macos_session(home)
            resolver = Jin10SessionResolver(
                env={},
                home_directory=home,
                platform_name="darwin",
                current_uid=-1,
            )

            with self.assertRaisesRegex(ValueError, "current user"):
                resolver.resolve()

    def test_rejects_short_desktop_token_without_echoing_it(self) -> None:
        token = "short-secret"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self._write_macos_session(home, token=token)
            resolver = Jin10SessionResolver(
                env={},
                home_directory=home,
                platform_name="darwin",
            )

            with self.assertRaisesRegex(ValueError, "36-character") as context:
                resolver.resolve()

        self.assertNotIn(token, str(context.exception))

    def test_windows_uses_explicit_token_without_a_log_dependency(self) -> None:
        resolver = Jin10SessionResolver(
            env={"JIN10_LOCAL_SESSION_TOKEN": "w" * 36},
            platform_name="win32",
        )

        credentials = resolver.resolve()

        self.assertEqual(credentials.session_token, "w" * 36)

    def test_redacts_current_and_rotated_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            storage = self._write_macos_session(home, token="a" * 36)
            resolver = Jin10SessionResolver(
                env={},
                home_directory=home,
                platform_name="darwin",
            )
            resolver.resolve()
            storage.write_text(json.dumps({"ji10_token": "b" * 36}), encoding="utf-8")
            resolver.resolve(refresh=True)

            message = resolver.redact(f"old={'a' * 36} new={'b' * 36}")

        self.assertNotIn("a" * 36, message)
        self.assertNotIn("b" * 36, message)
        self.assertEqual(message.count("<redacted>"), 2)


if __name__ == "__main__":
    unittest.main()
