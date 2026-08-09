from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from market_analysis.application.expert_ai import (
    CodexExpertAnalysisService,
    CommandResult,
)
from market_analysis.application.options import unconfigured_gold_options_snapshot


class _Runner:
    def __init__(self, *results: CommandResult | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], str | None, float]] = []

    async def __call__(
        self,
        command: tuple[str, ...],
        stdin: str | None,
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append((tuple(command), stdin, timeout_seconds))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ExpertAiServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_chatgpt_login_without_forwarding_cli_output(self) -> None:
        runner = _Runner(CommandResult(0, "Logged in using ChatGPT\n", ""))
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            command="codex",
            runner=runner,
        )

        status = await service.status()

        self.assertEqual(status.state, "ready")
        self.assertTrue(status.available)
        self.assertTrue(status.authenticated)
        self.assertEqual(status.auth_mode, "chatgpt")
        self.assertNotIn("Logged in", status.detail)
        self.assertEqual(runner.calls[0][0], ("codex", "login", "status"))

    async def test_status_is_honest_when_cli_is_missing(self) -> None:
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            command_finder=lambda _: None,
        )

        status = await service.status()

        self.assertEqual(status.state, "unavailable")
        self.assertFalse(status.available)
        self.assertIsNone(status.authenticated)

    async def test_analyze_runs_ephemeral_read_only_exec_and_reads_agent_message(self) -> None:
        output = "\n".join(
            (
                '{"type":"thread.started","thread_id":"private"}',
                '{"type":"item.completed","item":{"type":"command_execution",'
                '"text":"ignored"}}',
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"趋势仍偏强, 但需观察失效位。"}}',
            )
        )
        runner = _Runner(
            CommandResult(0, "Logged in using ChatGPT", ""),
            CommandResult(0, output, ""),
        )
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            command="codex",
            runner=runner,
        )
        snapshot = {
            "source_id": "jin10_client",
            "data_as_of": "2026-08-09T09:30:00+00:00",
            "bars": [{"close": "3380.12"}],
        }

        result = await service.analyze(
            snapshot,
            enabled_strategies=("macd", "structure"),
        )

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.analysis, "趋势仍偏强, 但需观察失效位。")
        self.assertEqual(result.source_id, "jin10_client")
        self.assertEqual(result.bar_count, 1)
        command, prompt, timeout = runner.calls[1]
        self.assertEqual(
            command,
            (
                "codex",
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ignore-rules",
                "-",
            ),
        )
        self.assertIn('"source_id":"jin10_client"', prompt or "")
        self.assertIn('"id":"macd"', prompt or "")
        self.assertIn('"definition":"基于收盘价的 12/26 EMA 与 9 周期信号线。"', prompt or "")
        self.assertNotIn("strategy_summary", prompt or "")
        self.assertEqual(timeout, 90.0)

    async def test_analyze_returns_timeout_without_exposing_process_details(self) -> None:
        runner = _Runner(
            CommandResult(0, "Logged in using ChatGPT", ""),
            TimeoutError("secret process output"),
        )
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            command="codex",
            runner=runner,
        )

        result = await service.analyze(
            {"source_id": "jin10_client", "bars": []},
            enabled_strategies=(),
        )

        self.assertEqual(result.state, "timeout")
        self.assertNotIn("secret", result.detail)
        self.assertIsNone(result.analysis)

    async def test_analyze_reports_expired_login_from_exec(self) -> None:
        runner = _Runner(
            CommandResult(0, "Logged in using ChatGPT", ""),
            CommandResult(1, "", "Error: authentication required"),
        )
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            command="codex",
            runner=runner,
        )

        result = await service.analyze(
            {"source_id": "jin10_client", "bars": []},
            enabled_strategies=(),
        )

        self.assertEqual(result.state, "not_authenticated")
        self.assertIsNone(result.auth_mode)

    def test_process_environment_excludes_market_and_api_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATH": "safe-path",
                "USERPROFILE": "safe-profile",
                "JIN10_LOCAL_SESSION_TOKEN": "market-secret",
                "OPENAI_API_KEY": "api-secret",
                "MARKET_ANALYSIS_DATABASE_URL": "database-secret",
            },
            clear=True,
        ):
            environment = CodexExpertAnalysisService._sanitized_environment()

        self.assertEqual(environment["PATH"], "safe-path")
        self.assertEqual(environment["USERPROFILE"], "safe-profile")
        self.assertNotIn("JIN10_LOCAL_SESSION_TOKEN", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("MARKET_ANALYSIS_DATABASE_URL", environment)

    def test_agent_message_with_credential_shape_is_rejected(self) -> None:
        stdout = (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"}}'
        )

        self.assertIsNone(CodexExpertAnalysisService._agent_message(stdout))

    def test_gold_options_status_contains_no_synthetic_market_values(self) -> None:
        status = unconfigured_gold_options_snapshot()

        self.assertEqual(status.contract_version, "gold-options-v2")
        self.assertEqual(status.state, "unconfigured")
        self.assertFalse(status.available)
        self.assertIsNone(status.provider_id)
        self.assertIsNone(status.observed_at)
        self.assertEqual(status.quote_count, 0)
        self.assertEqual(status.analysis_state, "blocked_without_market_data")
        self.assertEqual(
            {item.market_id for item in status.markets},
            {"shfe_gold_options", "cme_comex_gold_options"},
        )


if __name__ == "__main__":
    unittest.main()
