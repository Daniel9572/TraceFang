from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracefang import service


class ApplicationSessionTests(unittest.TestCase):
    def test_eof_stops_services_after_readiness(self) -> None:
        events = []
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(service, "APPLICATION_SUPPORT", Path(directory)),
            patch.object(service, "migrate_registration"),
            patch.object(service, "start_service", side_effect=lambda **_: events.append("start")),
            patch.object(
                service, "stop_service", side_effect=lambda **_: events.append("stop")
            ) as stop,
            patch.object(service.sys, "stdin", io.StringIO("")),
            patch.object(service.sys, "stdout", io.StringIO()) as output,
        ):
            service.application_session()
        self.assertEqual(events, ["start", "stop"])
        self.assertIn("TRACEFANG_APP_READY", output.getvalue())
        stop.assert_called_once_with(strict=True)

    def test_start_failure_still_cleans_up(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(service, "APPLICATION_SUPPORT", Path(directory)),
            patch.object(service, "migrate_registration"),
            patch.object(service, "start_service", side_effect=service.ServiceError("failed")),
            patch.object(service, "stop_service") as stop,
            self.assertRaises(service.ServiceError),
        ):
            service.application_session()
        stop.assert_called_once_with(strict=True)

    def test_second_application_cannot_stop_the_first(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(service, "APPLICATION_SUPPORT", Path(directory)),
            patch.object(service, "start_service") as start,
            patch.object(service, "stop_service") as stop,
            service.operation_lock(timeout_seconds=0, filename="application.lock"),
            self.assertRaises(service.ServiceError),
        ):
            service.application_session()
        start.assert_not_called()
        stop.assert_not_called()

    def test_busy_session_exit_code_tells_native_window_not_to_offer_service_stop(self) -> None:
        with (
            patch.object(
                service, "application_session", side_effect=service.ApplicationAlreadyOpen()
            ),
            patch.object(service.sys, "stderr", io.StringIO()),
        ):
            self.assertEqual(service.main(["session"]), 3)

    def test_shutdown_failure_is_not_reported_as_stopped(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(service, "APPLICATION_SUPPORT", Path(directory)),
            patch.object(service, "migrate_registration"),
            patch.object(service, "start_service"),
            patch.object(service, "stop_service", side_effect=service.ServiceError("failed")),
            patch.object(service.sys, "stdin", io.StringIO("")),
            patch.object(service.sys, "stdout", io.StringIO()) as output,
            self.assertRaises(service.ServiceError),
        ):
            service.application_session()
        self.assertNotIn("TRACEFANG_APP_STOPPED", output.getvalue())

    def test_migration_preserves_runtime_without_login_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "LaunchAgents" / "service.plist"
            old.parent.mkdir()
            old.write_bytes(b"installed runtime")
            new = root / "support" / "service.plist"
            with (
                patch.object(service, "IS_WINDOWS", False),
                patch.object(service, "APPLICATION_SUPPORT", new.parent),
                patch.object(service, "SERVICE_REGISTRATION", new),
                patch.object(service, "LEGACY_REGISTRATION", old),
            ):
                service.migrate_registration()
            self.assertEqual(new.read_bytes(), b"installed runtime")
            self.assertFalse(old.exists())
