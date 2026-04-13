"""Tests for the provider router."""

import pytest

from sentinel.config.schema import RoleName, SentinelConfig
from sentinel.providers.claude import ClaudeProvider
from sentinel.providers.gemini import GeminiProvider
from sentinel.providers.local import LocalProvider
from sentinel.providers.router import Router


@pytest.fixture
def config() -> SentinelConfig:
    return SentinelConfig(
        project={"name": "test", "path": "/tmp/test"},
        roles={
            "monitor": {"provider": "local", "model": "qwen2.5-coder:14b"},
            "researcher": {"provider": "gemini", "model": "gemini-2.5-pro"},
            "planner": {"provider": "claude", "model": "claude-opus-4-6"},
            "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
            "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
        },
    )


class TestRouter:
    def test_maps_roles_to_providers(self, config: SentinelConfig) -> None:
        router = Router(config)
        assert isinstance(router.get_provider(RoleName.MONITOR), LocalProvider)
        assert isinstance(router.get_provider(RoleName.RESEARCHER), GeminiProvider)
        assert isinstance(router.get_provider(RoleName.PLANNER), ClaudeProvider)
        assert isinstance(router.get_provider(RoleName.CODER), ClaudeProvider)
        assert isinstance(router.get_provider(RoleName.REVIEWER), GeminiProvider)

    def test_same_provider_different_models_are_separate(self, config: SentinelConfig) -> None:
        """Planner (opus) and coder (sonnet) both use claude but different models."""
        router = Router(config)
        planner_provider = router.get_provider(RoleName.PLANNER)
        coder_provider = router.get_provider(RoleName.CODER)
        assert planner_provider is not coder_provider  # different model = different instance

    def test_same_provider_same_model_reused(self, config: SentinelConfig) -> None:
        """Researcher and reviewer both use gemini-2.5-pro — same instance."""
        router = Router(config)
        researcher = router.get_provider(RoleName.RESEARCHER)
        reviewer = router.get_provider(RoleName.REVIEWER)
        assert researcher is reviewer

    def test_invalid_role_raises(self, config: SentinelConfig) -> None:
        router = Router(config)
        with pytest.raises(ValueError, match="No provider configured"):
            router.get_provider("nonexistent")  # type: ignore[arg-type]


class TestProviderDetection:
    def test_detect_all_returns_four_providers(self) -> None:
        results = Router.detect_all()
        assert "claude" in results
        assert "codex" in results
        assert "gemini" in results
        assert "ollama" in results
