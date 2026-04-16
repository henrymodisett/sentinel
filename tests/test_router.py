"""Tests for the provider router."""

import pytest

from sentinel.config.schema import RoleName, SentinelConfig
from sentinel.providers.claude import ClaudeProvider
from sentinel.providers.gemini import GeminiProvider
from sentinel.providers.local import LocalProvider
from sentinel.providers.router import DEFAULT_RULES, Router, RoutingRule


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


@pytest.fixture
def gemini_config() -> SentinelConfig:
    """Config where the monitor uses Gemini — the provider where dogfood
    revealed task-aware routing matters."""
    return SentinelConfig(
        project={"name": "test", "path": "/tmp/test"},
        roles={
            "monitor": {"provider": "gemini", "model": "gemini-2.5-flash"},
            "researcher": {"provider": "gemini", "model": "gemini-2.5-pro"},
            "planner": {"provider": "claude", "model": "claude-opus-4-6"},
            "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
            "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
        },
    )


class TestTaskAwareRouting:
    """The Router applies DEFAULT_RULES when a caller passes task hints.
    Without hints, behavior is the static-mapping default — fully
    backwards compatible with every existing caller."""

    def test_no_hints_returns_configured_provider(
        self, gemini_config: SentinelConfig,
    ) -> None:
        router = Router(gemini_config)
        provider = router.get_provider(RoleName.MONITOR)
        assert isinstance(provider, GeminiProvider)
        assert provider.model == "gemini-2.5-flash"  # the configured default

    def test_synthesize_overrides_flash_to_pro(
        self, gemini_config: SentinelConfig,
    ) -> None:
        """The synthesize rule pushes gemini calls up to pro because
        flash times out on the cross-lens summary prompt."""
        router = Router(gemini_config)
        provider = router.get_provider(RoleName.MONITOR, task="synthesize")
        assert isinstance(provider, GeminiProvider)
        assert provider.model == "gemini-2.5-pro"

    def test_huge_eval_overrides_below_threshold_no_change(
        self, gemini_config: SentinelConfig,
    ) -> None:
        """The huge-eval rule only fires above the 60k token threshold —
        a small evaluate_lens call uses the configured model."""
        router = Router(gemini_config)
        provider = router.get_provider(
            RoleName.MONITOR, task="evaluate_lens", prompt_size=1000,
        )
        assert provider.model == "gemini-2.5-flash"

    def test_huge_eval_overrides_above_threshold_keeps_flash(
        self, gemini_config: SentinelConfig,
    ) -> None:
        """Above 60k tokens, the huge-eval rule pushes to flash. Since
        the configured model is already flash, the model is unchanged
        but the rule still semantically applies (no-op override)."""
        router = Router(gemini_config)
        provider = router.get_provider(
            RoleName.MONITOR, task="evaluate_lens", prompt_size=120_000,
        )
        assert provider.model == "gemini-2.5-flash"

    def test_huge_eval_pulls_pro_down_to_flash(self) -> None:
        """The most useful direction of the huge-eval rule: when config
        says pro but the prompt is too big for pro to handle reliably,
        flash takes over."""
        cfg = SentinelConfig(
            project={"name": "test", "path": "/tmp/test"},
            roles={
                "monitor": {"provider": "gemini", "model": "gemini-2.5-pro"},
                "researcher": {"provider": "gemini", "model": "gemini-2.5-pro"},
                "planner": {"provider": "claude", "model": "claude-opus-4-6"},
                "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
                "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
            },
        )
        router = Router(cfg)
        provider = router.get_provider(
            RoleName.MONITOR, task="evaluate_lens", prompt_size=120_000,
        )
        assert provider.model == "gemini-2.5-flash"

    def test_unknown_task_falls_through_to_configured(
        self, gemini_config: SentinelConfig,
    ) -> None:
        router = Router(gemini_config)
        provider = router.get_provider(RoleName.MONITOR, task="something-novel")
        assert provider.model == "gemini-2.5-flash"

    def test_rule_only_fires_for_specified_provider(self) -> None:
        """The synthesize rule has only_for_provider='gemini' — a Claude
        monitor doesn't get pushed to gemini-2.5-pro, it stays on its
        configured Claude model."""
        cfg = SentinelConfig(
            project={"name": "test", "path": "/tmp/test"},
            roles={
                "monitor": {"provider": "claude", "model": "claude-sonnet-4-6"},
                "researcher": {"provider": "gemini", "model": "gemini-2.5-pro"},
                "planner": {"provider": "claude", "model": "claude-opus-4-6"},
                "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
                "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
            },
        )
        router = Router(cfg)
        provider = router.get_provider(RoleName.MONITOR, task="synthesize")
        assert isinstance(provider, ClaudeProvider)
        assert provider.model == "claude-sonnet-4-6"

    def test_overridden_provider_is_cached(
        self, gemini_config: SentinelConfig,
    ) -> None:
        """Two synthesize calls in the same cycle should reuse the same
        materialized provider instance — both for memory and for sharing
        the provider's timeout/max_turns state."""
        router = Router(gemini_config)
        a = router.get_provider(RoleName.MONITOR, task="synthesize")
        b = router.get_provider(RoleName.MONITOR, task="synthesize")
        assert a is b

    def test_default_rules_are_immutable(self) -> None:
        """DEFAULT_RULES is a tuple, not a list — accidental mutation
        of the global rule set during a test would corrupt every
        subsequent test in the module."""
        assert isinstance(DEFAULT_RULES, tuple)
        for rule in DEFAULT_RULES:
            assert isinstance(rule, RoutingRule)


class TestCustomRules:
    """Routers can be constructed with a custom rules tuple — useful
    for tests of policy without forking DEFAULT_RULES."""

    def test_custom_rule_overrides_for_specific_task(
        self, gemini_config: SentinelConfig,
    ) -> None:
        rules = (
            RoutingRule(
                name="test-rule",
                task="ad-hoc",
                role=None,
                min_prompt_size=0,
                only_for_provider="gemini",
                override_model="gemini-2.5-flash-lite",
                reason="test fixture",
            ),
        )
        router = Router(gemini_config, rules=rules)
        provider = router.get_provider(RoleName.MONITOR, task="ad-hoc")
        assert provider.model == "gemini-2.5-flash-lite"

    def test_empty_rules_means_no_overrides(
        self, gemini_config: SentinelConfig,
    ) -> None:
        router = Router(gemini_config, rules=())
        provider = router.get_provider(RoleName.MONITOR, task="synthesize")
        # Without rules, configured default wins regardless of task.
        assert provider.model == "gemini-2.5-flash"

    def test_override_sets_pending_routing_reason(
        self, gemini_config: SentinelConfig,
    ) -> None:
        """When a rule fires, the next provider call should pick up the
        rule name via the pending-reason ContextVar — that's how the
        journal records *why* a particular model was used."""
        from sentinel.journal import (
            consume_pending_routing_reason,
            set_pending_routing_reason,
        )

        # Clear any leftover state, then trigger an override.
        set_pending_routing_reason("")
        router = Router(gemini_config)
        router.get_provider(RoleName.MONITOR, task="synthesize")
        # The Router set a pending reason; consume returns it once and clears.
        first = consume_pending_routing_reason()
        second = consume_pending_routing_reason()
        assert first == "synthesize-prefers-pro"
        assert second == "", "consume must clear the pending reason"

    def test_no_override_leaves_pending_reason_blank(
        self, gemini_config: SentinelConfig,
    ) -> None:
        """A call that doesn't trigger any rule must NOT leave a stale
        reason in the ContextVar — otherwise the next call would
        misattribute."""
        from sentinel.journal import (
            consume_pending_routing_reason,
            set_pending_routing_reason,
        )

        set_pending_routing_reason("")
        router = Router(gemini_config)
        router.get_provider(RoleName.MONITOR)  # no task → no rule
        assert consume_pending_routing_reason() == ""

    def test_get_provider_accepts_bare_string_role(
        self, gemini_config: SentinelConfig,
    ) -> None:
        """Regression: dogfood 2026-04-16 crashed with
        `AttributeError: 'str' object has no attribute 'value'` because
        the override-log line used `role.value`. Callers in the roles
        layer pass bare strings ("monitor"), not RoleName enum values.
        Both must work — locks in the str(role) fix."""
        router = Router(gemini_config)
        # Bare string, not enum — must not crash even when an override
        # fires and the log line tries to render the role name.
        provider = router.get_provider("monitor", task="synthesize")
        # Sanity: the routing override actually fired (proves we hit
        # the formatting code path in question)
        assert provider.model == "gemini-2.5-pro"


class TestMissingLocalModels:
    """Pre-flight check: any role configured for local (ollama) needs
    its model already pulled, otherwise sentinel can't start. We surface
    the exact `ollama pull` command rather than letting the first
    provider call die with an obscure error."""

    def test_no_local_roles_returns_empty(
        self, gemini_config: SentinelConfig,
    ) -> None:
        """Configs that don't use ollama at all skip the check entirely."""
        router = Router(gemini_config)
        assert router.missing_local_models() == []

    def test_local_role_with_missing_model_reported(
        self, monkeypatch,
    ) -> None:
        """Monitor configured for local/qwen2.5-coder:14b but only
        llama3.2:3b is pulled — the missing pair is reported with the
        role context so the message can name which role needs which model."""
        from sentinel.providers import local as local_module
        from sentinel.providers.interface import ProviderStatus

        cfg = SentinelConfig(
            project={"name": "test", "path": "/tmp/test"},
            roles={
                "monitor": {"provider": "local", "model": "qwen2.5-coder:14b"},
                "researcher": {"provider": "gemini", "model": "gemini-2.5-pro"},
                "planner": {"provider": "claude", "model": "claude-opus-4-6"},
                "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
                "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
            },
        )

        def fake_detect(self):  # noqa: ANN001, ANN202
            return ProviderStatus(
                installed=True, authenticated=True, models=["llama3.2:3b"],
            )

        monkeypatch.setattr(local_module.LocalProvider, "detect", fake_detect)

        router = Router(cfg)
        missing = router.missing_local_models()
        assert missing == [("monitor", "qwen2.5-coder:14b")]

    def test_local_role_with_pulled_model_passes(
        self, monkeypatch,
    ) -> None:
        from sentinel.providers import local as local_module
        from sentinel.providers.interface import ProviderStatus

        cfg = SentinelConfig(
            project={"name": "test", "path": "/tmp/test"},
            roles={
                "monitor": {"provider": "local", "model": "qwen2.5-coder:14b"},
                "researcher": {"provider": "gemini", "model": "gemini-2.5-pro"},
                "planner": {"provider": "claude", "model": "claude-opus-4-6"},
                "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
                "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
            },
        )

        def fake_detect(self):  # noqa: ANN001, ANN202
            return ProviderStatus(
                installed=True, authenticated=True,
                models=["qwen2.5-coder:14b", "llama3.2:3b"],
            )

        monkeypatch.setattr(local_module.LocalProvider, "detect", fake_detect)
        router = Router(cfg)
        assert router.missing_local_models() == []

    def test_ollama_not_installed_reports_all_local_models(
        self, monkeypatch,
    ) -> None:
        """If ollama itself isn't installed, every required model is
        effectively missing — the message tells the user to install
        ollama first by surfacing the pull commands they'll need next."""
        from sentinel.providers import local as local_module
        from sentinel.providers.interface import ProviderStatus

        cfg = SentinelConfig(
            project={"name": "test", "path": "/tmp/test"},
            roles={
                "monitor": {"provider": "local", "model": "qwen2.5-coder:14b"},
                "researcher": {"provider": "local", "model": "llama3.2:3b"},
                "planner": {"provider": "claude", "model": "claude-opus-4-6"},
                "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
                "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
            },
        )

        def fake_detect(self):  # noqa: ANN001, ANN202
            return ProviderStatus(installed=False, authenticated=False)

        monkeypatch.setattr(local_module.LocalProvider, "detect", fake_detect)
        router = Router(cfg)
        missing = router.missing_local_models()
        assert ("monitor", "qwen2.5-coder:14b") in missing
        assert ("researcher", "llama3.2:3b") in missing
