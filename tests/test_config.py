"""Tests for configuration schema."""

import pytest
from pydantic import ValidationError

from sentinel.config.schema import (
    ROLE_DEFAULTS,
    ROLE_DESCRIPTIONS,
    ProviderName,
    RoleConfig,
    RoleName,
    SentinelConfig,
)


class TestRoleNames:
    def test_all_five_roles_exist(self) -> None:
        assert len(RoleName) == 5

    def test_values(self) -> None:
        assert RoleName.MONITOR == "monitor"
        assert RoleName.RESEARCHER == "researcher"
        assert RoleName.PLANNER == "planner"
        assert RoleName.CODER == "coder"
        assert RoleName.REVIEWER == "reviewer"


class TestProviderNames:
    def test_all_four_providers_exist(self) -> None:
        assert len(ProviderName) == 4

    def test_values(self) -> None:
        assert ProviderName.CLAUDE == "claude"
        assert ProviderName.OPENAI == "openai"
        assert ProviderName.GEMINI == "gemini"
        assert ProviderName.LOCAL == "local"


class TestRoleDefaults:
    def test_defaults_for_all_roles(self) -> None:
        assert len(ROLE_DEFAULTS) == 5
        assert ROLE_DEFAULTS[RoleName.MONITOR].provider == ProviderName.LOCAL
        assert ROLE_DEFAULTS[RoleName.RESEARCHER].provider == ProviderName.GEMINI
        assert ROLE_DEFAULTS[RoleName.PLANNER].provider == ProviderName.CLAUDE
        assert ROLE_DEFAULTS[RoleName.CODER].provider == ProviderName.CLAUDE
        assert ROLE_DEFAULTS[RoleName.REVIEWER].provider == ProviderName.OPENAI

    def test_reviewer_differs_from_coder(self) -> None:
        assert ROLE_DEFAULTS[RoleName.REVIEWER].provider != ROLE_DEFAULTS[RoleName.CODER].provider


class TestRoleDescriptions:
    def test_descriptions_for_all_roles(self) -> None:
        assert len(ROLE_DESCRIPTIONS) == 5
        for role in RoleName:
            assert role in ROLE_DESCRIPTIONS
            assert len(ROLE_DESCRIPTIONS[role]) > 0


class TestSentinelConfig:
    def test_valid_config(self) -> None:
        config = SentinelConfig(
            project={"name": "test-project", "path": "/tmp/test"},
            roles={
                "monitor": {"provider": "local", "model": "qwen2.5-coder:14b"},
                "researcher": {"provider": "gemini", "model": "gemini-2.5-pro"},
                "planner": {"provider": "claude", "model": "claude-opus-4-6"},
                "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
                "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
            },
        )
        assert config.project.name == "test-project"
        assert config.budget.daily_limit_usd == 15.0

    def test_no_goals_in_config(self) -> None:
        """Goals are derived from CLAUDE.md/README, not stored in config."""
        config = SentinelConfig(
            project={"name": "test", "path": "/tmp"},
            roles={
                "monitor": {"provider": "local", "model": "test"},
                "researcher": {"provider": "gemini", "model": "test"},
                "planner": {"provider": "claude", "model": "test"},
                "coder": {"provider": "claude", "model": "test"},
                "reviewer": {"provider": "gemini", "model": "test"},
            },
        )
        assert not hasattr(config, "goals")

    def test_invalid_provider_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SentinelConfig(
                project={"name": "test", "path": "/tmp"},
                roles={
                    "monitor": {"provider": "invalid", "model": "test"},
                    "researcher": {"provider": "gemini", "model": "test"},
                    "planner": {"provider": "claude", "model": "test"},
                    "coder": {"provider": "claude", "model": "test"},
                    "reviewer": {"provider": "gemini", "model": "test"},
                },
            )

    def test_default_scan_config(self) -> None:
        config = SentinelConfig(
            project={"name": "test", "path": "/tmp"},
            roles={
                "monitor": {"provider": "local", "model": "test"},
                "researcher": {"provider": "gemini", "model": "test"},
                "planner": {"provider": "claude", "model": "test"},
                "coder": {"provider": "claude", "model": "test"},
                "reviewer": {"provider": "gemini", "model": "test"},
            },
        )
        assert config.scan.max_lenses == 10
        assert config.scan.evaluate_per_lens is True


class TestRoleConfig:
    def test_minimal_role_config(self) -> None:
        role = RoleConfig(provider=ProviderName.CLAUDE, model="claude-opus-4-6")
        assert role.provider == ProviderName.CLAUDE
