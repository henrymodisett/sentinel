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
