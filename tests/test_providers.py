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
