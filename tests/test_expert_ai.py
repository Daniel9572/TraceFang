from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tracefang.application.codex_models import CodexModel, parse_model
from tracefang.application.expert_ai import (
    CodexExpertAnalysisService,
    CommandResult,
)
from tracefang.application.options import unconfigured_gold_options_snapshot


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
    async def test_model_catalog_is_cached_and_concurrent_requests_are_coalesced(self) -> None:
        model = CodexModel("test-model", "Test model", ("low", "high"), "low", True)
        reader = AsyncMock(return_value=(model,))
        service = CodexExpertAnalysisService(working_directory=Path.cwd(), command="codex")
        with patch("tracefang.application.expert_ai.read_codex_models", reader):
            results = await asyncio.gather(service.models(), service.models(), service.models())
        self.assertEqual(results, [(model,)] * 3)
        reader.assert_awaited_once()

    async def test_custom_prompt_and_selection_reach_codex(self) -> None:
        runner = _Runner(
            CommandResult(0, "Logged in using ChatGPT", ""),
            CommandResult(
                0,
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"缺少成交量, 无法判断量价关系。"}}',
                "",
            ),
        )
        model = CodexModel("test-model", "Test model", ("low", "high"), "low", True)
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            command="codex",
            runner=runner,
        )
        with patch.object(service, "models", AsyncMock(return_value=(model,))):
            result = await service.analyze(
                {"bars": []},
                enabled_strategies=(),
                custom_prompt="只解释缺失数据。",
                model="test-model",
                reasoning_effort="high",
            )
        self.assertEqual(result.state, "completed")
        command, prompt, _ = runner.calls[1]
        self.assertEqual(
            command[-5:],
            (
                "--model",
                "test-model",
                "-c",
                'model_reasoning_effort="high"',
                "-",
            ),
        )
        self.assertIn('"user_question":"只解释缺失数据。"', prompt or "")
        self.assertNotIn("只解释缺失数据", " ".join(command))

    async def test_invalid_model_or_effort_never_launches_analysis(self) -> None:
        model = CodexModel("test-model", "Test model", ("low", "high"), "low", True)
        runner = _Runner()
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            command="codex",
            runner=runner,
        )
        with patch.object(service, "models", AsyncMock(return_value=(model,))):
            for selected, effort in [("missing", "low"), ("test-model", "ultra"), (None, "low")]:
                with self.subTest(model=selected, effort=effort), self.assertRaises(ValueError):
                    await service.analyze(
                        {}, enabled_strategies=(), model=selected, reasoning_effort=effort
                    )
        self.assertEqual(runner.calls, [])

    async def test_catalog_failure_does_not_expose_process_details(self) -> None:
        service = CodexExpertAnalysisService(working_directory=Path.cwd(), command="codex")
        with (
            patch(
                "tracefang.application.expert_ai.read_codex_models",
                AsyncMock(side_effect=ValueError("private diagnostic")),
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            await service.models()
        self.assertNotIn("private", str(caught.exception))

    def test_catalog_preserves_effort_order_and_ignores_hidden_models(self) -> None:
        entry = {
            "model": "test-model",
            "displayName": "Test model",
            "isDefault": True,
            "defaultReasoningEffort": "high",
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort} for effort in ("low", "medium", "high", "ultra")
            ],
        }
        model = parse_model(entry)
        self.assertIsNotNone(model)
        self.assertEqual(model.reasoning_efforts, ("low", "medium", "high", "ultra"))
        self.assertEqual(model.default_reasoning_effort, "high")
        self.assertIsNone(parse_model({**entry, "hidden": True}))
        self.assertIsNone(parse_model({**entry, "model": "--injected-option"}))

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
        self.assertIsNone(status.diagnostic_code)
        self.assertNotIn("Logged in", status.detail)
        self.assertEqual(runner.calls[0][0], ("codex", "login", "status"))

    async def test_status_is_honest_when_cli_is_missing(self) -> None:
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            command_finder=lambda _: None,
            fallback_commands=(),
        )

        status = await service.status()

        self.assertEqual(status.state, "unavailable")
        self.assertFalse(status.available)
        self.assertIsNone(status.authenticated)
        self.assertEqual(status.diagnostic_code, "cli_not_found")

    async def test_status_discovers_macos_app_cli_outside_path(self) -> None:
        runner = _Runner(CommandResult(0, "Logged in using ChatGPT\n", ""))
        with tempfile.TemporaryDirectory() as directory:
            command = Path(directory) / "codex"
            command.write_text("#!/bin/sh\n", encoding="utf-8")
            command.chmod(0o700)
            service = CodexExpertAnalysisService(
                working_directory=Path.cwd(),
                command_finder=lambda _: None,
                fallback_commands=(command,),
                runner=runner,
            )

            status = await service.status()

        self.assertEqual(status.state, "ready")
        self.assertEqual(runner.calls[0][0], (str(command), "login", "status"))

    async def test_status_re_resolves_cli_after_startup(self) -> None:
        calls = 0

        def find_command(_: str) -> str | None:
            nonlocal calls
            calls += 1
            return None if calls == 1 else "codex"

        runner = _Runner(CommandResult(0, "Logged in using ChatGPT\n", ""))
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            command_finder=find_command,
            fallback_commands=(),
            runner=runner,
        )

        missing = await service.status()
        ready = await service.status()

        self.assertEqual(missing.diagnostic_code, "cli_not_found")
        self.assertEqual(ready.state, "ready")
        self.assertEqual(calls, 2)

    async def test_explicit_cli_path_takes_precedence_over_path_lookup(self) -> None:
        runner = _Runner(CommandResult(0, "Logged in using ChatGPT\n", ""))
        with tempfile.TemporaryDirectory() as directory:
            command = Path(directory) / "codex"
            command.write_text("#!/bin/sh\n", encoding="utf-8")
            command.chmod(0o700)
            service = CodexExpertAnalysisService(
                working_directory=Path.cwd(),
                environment={"TRACEFANG_CODEX_CLI_PATH": str(command)},
                command_finder=lambda _: "wrong-codex",
                fallback_commands=(),
                runner=runner,
            )

            status = await service.status()

        self.assertEqual(status.state, "ready")
        self.assertEqual(runner.calls[0][0], (str(command), "login", "status"))

    async def test_invalid_explicit_cli_path_is_a_configuration_error(self) -> None:
        runner = _Runner()
        service = CodexExpertAnalysisService(
            working_directory=Path.cwd(),
            environment={"TRACEFANG_CODEX_CLI_PATH": "/missing/codex"},
            command_finder=lambda _: "other-codex",
            fallback_commands=(),
            runner=runner,
        )

        status = await service.status()

        self.assertEqual(status.state, "error")
        self.assertFalse(status.available)
        self.assertEqual(status.diagnostic_code, "cli_path_invalid")
        self.assertEqual(runner.calls, [])

    async def test_analyze_runs_ephemeral_read_only_exec_and_reads_agent_message(self) -> None:
        output = "\n".join(
            (
                '{"type":"thread.started","thread_id":"private"}',
                '{"type":"item.completed","item":{"type":"command_execution","text":"ignored"}}',
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
        self.assertIsNone(result.diagnostic_code)
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
                "TRACEFANG_DATABASE_URL": "database-secret",
            },
            clear=True,
        ):
            environment = CodexExpertAnalysisService._sanitized_environment()

        self.assertEqual(environment["PATH"], "safe-path")
        self.assertEqual(environment["USERPROFILE"], "safe-profile")
        self.assertNotIn("JIN10_LOCAL_SESSION_TOKEN", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("TRACEFANG_DATABASE_URL", environment)

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
