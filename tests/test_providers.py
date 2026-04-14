"""Tests for provider detection and capabilities."""

from sentinel.providers.claude import ClaudeProvider
from sentinel.providers.gemini import GeminiProvider
from sentinel.providers.interface import ProviderName
from sentinel.providers.local import LocalProvider
from sentinel.providers.openai import OpenAIProvider
from sentinel.providers.router import Router


class TestProviderCapabilities:
    def test_claude_supports_agentic_code(self) -> None:
        p = ClaudeProvider()
        assert p.capabilities.agentic_code is True
        assert p.capabilities.web_search is True

    def test_openai_supports_agentic_code(self) -> None:
        p = OpenAIProvider()
        assert p.capabilities.agentic_code is True

    def test_gemini_no_agentic_code(self) -> None:
        p = GeminiProvider()
        assert p.capabilities.agentic_code is False
        assert p.capabilities.web_search is True

    def test_local_minimal_capabilities(self) -> None:
        p = LocalProvider()
        assert p.capabilities.web_search is False
        assert p.capabilities.agentic_code is False
        assert p.capabilities.long_context is False

    def test_all_providers_support_chat(self) -> None:
        for cls in [ClaudeProvider, OpenAIProvider, GeminiProvider, LocalProvider]:
            assert cls.capabilities.chat is True


class TestProviderDetection:
    def test_detect_returns_provider_status(self) -> None:
        """All detect() methods return a ProviderStatus with install hints."""
        for cls in [ClaudeProvider, OpenAIProvider, GeminiProvider, LocalProvider]:
            provider = cls()
            status = provider.detect()
            assert status.install_hint  # every provider has an install hint

    def test_detect_all_returns_four_entries(self) -> None:
        results = Router.detect_all()
        assert len(results) == 4
        assert "claude" in results
        assert "codex" in results
        assert "gemini" in results
        assert "ollama" in results


class TestProviderNames:
    def test_claude_name(self) -> None:
        assert ClaudeProvider.name == ProviderName.CLAUDE

    def test_openai_name(self) -> None:
        assert OpenAIProvider.name == ProviderName.OPENAI

    def test_gemini_name(self) -> None:
        assert GeminiProvider.name == ProviderName.GEMINI

    def test_local_name(self) -> None:
        assert LocalProvider.name == ProviderName.LOCAL


class TestClaudeCodeCLIFlags:
    """The Coder path needs --dangerously-skip-permissions or every Edit
    gets denied in non-interactive -p mode. Without this, sentinel can
    never actually execute a refinement — exactly what sigint showed."""

    def test_code_invocation_passes_skip_permissions(self) -> None:
        import asyncio
        import subprocess
        from unittest.mock import AsyncMock, patch

        provider = ClaudeProvider()
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"result":"ok","is_error":false,"usage":{}}',
            stderr="",
        )
        mock_run = AsyncMock(return_value=fake_result)
        with patch("sentinel.providers.claude.run_cli_async", new=mock_run):
            asyncio.run(provider.code("hi"))

        passed_args = mock_run.call_args[0][0]
        assert "--dangerously-skip-permissions" in passed_args
        assert "--max-turns" in passed_args

    def test_max_turns_uses_configured_value(self) -> None:
        """Router sets provider.max_turns from config.coder.max_turns —
        the CLI invocation must honor that instance attribute, not a
        hardcoded 20."""
        import asyncio
        import subprocess
        from unittest.mock import AsyncMock, patch

        provider = ClaudeProvider()
        provider.max_turns = 60
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"result":"ok","is_error":false,"usage":{}}',
            stderr="",
        )
        mock_run = AsyncMock(return_value=fake_result)
        with patch("sentinel.providers.claude.run_cli_async", new=mock_run):
            asyncio.run(provider.code("hi"))

        passed_args = mock_run.call_args[0][0]
        turns_idx = passed_args.index("--max-turns")
        assert passed_args[turns_idx + 1] == "60"


class TestClaudeCodeSurfacesStderr:
    """Regression: Claude CLI failures used to collapse to `content='Error: '`
    with empty stderr on the returned ChatResponse — the one thing you need
    to debug the failure was thrown away."""

    def test_code_response_populates_stderr_on_failure(self) -> None:
        import asyncio
        import subprocess
        from unittest.mock import AsyncMock, patch

        provider = ClaudeProvider()
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=1,
            stdout="", stderr="auth token missing — run `claude auth login`",
        )
        with patch(
            "sentinel.providers.claude.run_cli_async",
            new=AsyncMock(return_value=fake_result),
        ):
            response = asyncio.run(provider.code("hi"))

        assert response.is_error is True
        assert "auth token missing" in response.stderr
        assert "auth token missing" in response.content  # still in the rollup

    def test_code_response_populates_raw_stdout_on_is_error_payload(self) -> None:
        """When Claude returns is_error=true with empty result, the raw
        JSON payload must survive in raw_stdout so transcripts show
        what happened."""
        import asyncio
        import subprocess
        from unittest.mock import AsyncMock, patch

        provider = ClaudeProvider()
        payload = (
            '{"type":"result","is_error":true,"result":"",'
            '"num_turns":20,"duration_ms":240000}'
        )
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=payload, stderr="",
        )
        with patch(
            "sentinel.providers.claude.run_cli_async",
            new=AsyncMock(return_value=fake_result),
        ):
            response = asyncio.run(provider.code("hi"))

        assert response.is_error is True
        assert '"num_turns":20' in response.raw_stdout
        # And the content says SOMETHING useful, not just "Error: "
        assert response.content.startswith("Error:")
        assert response.content != "Error: "

    def test_code_passes_working_directory_as_cwd(self) -> None:
        """Claude must run inside the target project, not the caller's
        cwd. Otherwise `sentinel work --project /other` edits land in
        the wrong tree."""
        import asyncio
        import subprocess
        from unittest.mock import AsyncMock, patch

        provider = ClaudeProvider()
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"result":"ok","is_error":false,"usage":{}}',
            stderr="",
        )
        mock_run = AsyncMock(return_value=fake_result)
        with patch("sentinel.providers.claude.run_cli_async", new=mock_run):
            asyncio.run(provider.code("hi", working_directory="/tmp/target"))

        assert mock_run.call_args.kwargs["cwd"] == "/tmp/target"

    def test_code_records_cost_on_nonzero_exit_with_json_payload(self) -> None:
        """Regression: non-zero exit paths used to return before parsing
        stdout, so a JSON is_error payload on stdout dropped its
        total_cost_usd silently."""
        import asyncio
        import subprocess
        from unittest.mock import AsyncMock, patch

        provider = ClaudeProvider()
        payload = (
            '{"is_error":true,"result":"auth failed",'
            '"total_cost_usd":0.12,"usage":{"input_tokens":5,"output_tokens":2}}'
        )
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=payload, stderr="",
        )
        with patch(
            "sentinel.providers.claude.run_cli_async",
            new=AsyncMock(return_value=fake_result),
        ):
            response = asyncio.run(provider.code("hi"))

        assert response.is_error is True
        assert response.cost_usd == 0.12
        assert response.input_tokens == 5

    def test_code_records_cost_on_is_error_paths(self) -> None:
        """Sigint run: Claude burned $2.07 on max-turns-out runs but
        sentinel reported $0 because is_error paths dropped total_cost_usd.
        Budget tracking must not silently lose real spend."""
        import asyncio
        import subprocess
        from unittest.mock import AsyncMock, patch

        provider = ClaudeProvider()
        payload = (
            '{"type":"result","is_error":true,"result":"",'
            '"num_turns":20,"total_cost_usd":2.07,'
            '"usage":{"input_tokens":100,"output_tokens":50},'
            '"duration_ms":300000}'
        )
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=payload, stderr="",
        )
        with patch(
            "sentinel.providers.claude.run_cli_async",
            new=AsyncMock(return_value=fake_result),
        ):
            response = asyncio.run(provider.code("hi"))

        assert response.is_error is True
        assert response.cost_usd == 2.07
        assert response.input_tokens == 100
        assert response.output_tokens == 50
