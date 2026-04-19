"""Tests for the built-in integrations registry.

The registry exists to solve a concrete dogfood bug (finding C2 on the
first autumn-mail cycle, 2026-04-18): the planner hallucinated a missing
feature because it didn't know what Sentinel's own binary already ships.

These tests enforce the three invariants:

1. An "automate sentinel cycle journaling" proposal is filtered out
   when cortex T1.6 is active (the motivating case).
2. The filter surfaces the drop via ``FilterOutcome.skipped`` so the
   planner can emit an audit line — silent filtering would be worse
   than the bug.
3. An unrelated proposal that doesn't fingerprint-match is preserved
   untouched — the filter must not regress into a false-positive
   machine.

Plus activation rule tests so a future change to
``CortexIntegrationConfig`` can't silently break the gating.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from sentinel.integrations.registry import (
    BUILTIN_INTEGRATIONS,
    filter_actions,
    match_builtin,
)

if TYPE_CHECKING:
    from pathlib import Path


def _config(enabled: str) -> object:
    """Build a minimal duck-typed config exposing integrations.cortex.enabled."""
    return SimpleNamespace(
        integrations=SimpleNamespace(cortex=SimpleNamespace(enabled=enabled)),
    )


# ---------------------------------------------------------------------------
# Motivating case: the autumn-mail proposal gets filtered.
# ---------------------------------------------------------------------------


class TestAutumnMailMotivatingCase:
    def test_automate_sentinel_cycle_journaling_filtered_with_cortex_active(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / ".cortex").mkdir()
        action = {
            "title": "Automate Sentinel Cycle Journaling",
            "lens": "toolchain-dogfood",
            "why": (
                "Sentinel runs are occurring but not being journaled, "
                "indicating a silent failure in the toolchain integration."
            ),
            "impact": "high",
            "files": [
                {"path": "scripts/touchstone-run.sh",
                 "rationale": "Hook point for recording cycle output"},
                {"path": ".cortex/procedures/record-sentinel-cycle.sh",
                 "rationale": "New helper script"},
            ],
            "acceptance_criteria": [
                "Every sentinel work writes a cortex journal entry for each sentinel run",
            ],
            "verification": [],
            "out_of_scope": [],
        }

        match = match_builtin(action, tmp_path, _config("auto"))

        assert match is not None
        assert match.integration.slug == "cortex_cycle_journal_t16"
        assert match.matched_keywords  # at least one keyword hit

    def test_filter_actions_puts_motivating_case_in_skipped(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / ".cortex").mkdir()
        actions = [
            {
                "title": "Automate Sentinel Cycle Journaling",
                "lens": "toolchain-dogfood",
                "why": "No cortex journal entry for each sentinel run",
                "impact": "high",
                "files": [],
            },
        ]

        outcome = filter_actions(actions, tmp_path, _config("auto"))

        assert outcome.kept == []
        assert len(outcome.skipped) == 1
        assert outcome.skipped[0][1].integration.slug == "cortex_cycle_journal_t16"


# ---------------------------------------------------------------------------
# Activation rule.
# ---------------------------------------------------------------------------


class TestActivationRules:
    def test_cortex_integration_inactive_when_enabled_off(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / ".cortex").mkdir()
        action = {
            "title": "Automate Sentinel Cycle Journaling",
            "lens": "toolchain-dogfood",
            "why": "cortex journal entry for each sentinel run",
        }

        # enabled=off means the user opted out — local replacement is
        # fair game, so we must NOT filter.
        assert match_builtin(action, tmp_path, _config("off")) is None

    def test_cortex_integration_active_when_enabled_on_without_dir(
        self, tmp_path: Path,
    ) -> None:
        # No .cortex/ directory present, but config says "on" — the
        # feature is live regardless.
        action = {
            "title": "Automate Sentinel Cycle Journaling",
            "why": "t1.6",
        }
        assert match_builtin(action, tmp_path, _config("on")) is not None

    def test_cortex_integration_inactive_when_auto_without_dir(
        self, tmp_path: Path,
    ) -> None:
        # auto + no .cortex/ means the integration isn't writing
        # anything — user may legitimately want a local version.
        action = {"title": "Automate Sentinel Cycle Journaling", "why": "t1.6"}
        assert match_builtin(action, tmp_path, _config("auto")) is None

    def test_cortex_integration_active_when_auto_with_dir(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / ".cortex").mkdir()
        action = {"title": "Automate Sentinel Cycle Journaling", "why": "t1.6"}
        assert match_builtin(action, tmp_path, _config("auto")) is not None

    def test_sibling_detection_always_active(self, tmp_path: Path) -> None:
        action = {
            "title": "Add sibling detection",
            "why": "sentinel status should show sibling tools",
            "lens": "developer-experience",
        }
        match = match_builtin(action, tmp_path, None)
        # sibling_detection_r3 is unconditional, and the keyword
        # "sibling detection" is in its fingerprints.
        assert match is not None
        assert match.integration.slug == "sibling_detection_r3"


# ---------------------------------------------------------------------------
# Negative test — unrelated proposals pass through untouched.
# ---------------------------------------------------------------------------


class TestNegativeCases:
    def test_unrelated_proposal_not_filtered(self, tmp_path: Path) -> None:
        (tmp_path / ".cortex").mkdir()
        action = {
            "title": "Add retry logic to the Gmail poller",
            "lens": "reliability",
            "why": "Transient 503s from the Gmail API currently fail the whole poll.",
            "impact": "medium",
            "files": [{"path": "gws/poller.swift", "rationale": "the poll loop"}],
            "acceptance_criteria": ["503s retry with exponential backoff"],
            "verification": ["swift test"],
            "out_of_scope": [],
        }
        assert match_builtin(action, tmp_path, _config("auto")) is None

        outcome = filter_actions([action], tmp_path, _config("auto"))
        assert outcome.kept == [action]
        assert outcome.skipped == []

    def test_title_collision_without_body_context_still_matches(
        self, tmp_path: Path,
    ) -> None:
        """Sanity: if a proposal's title alone names the feature, the
        filter still catches it even with an empty body. The motivating
        case has a full body; this enforces the shorter-form fallback."""
        (tmp_path / ".cortex").mkdir()
        action = {"title": "Automate Sentinel Cycle Journaling"}
        assert match_builtin(action, tmp_path, _config("auto")) is not None

    def test_empty_action_does_not_match(self, tmp_path: Path) -> None:
        (tmp_path / ".cortex").mkdir()
        assert match_builtin({}, tmp_path, _config("auto")) is None


# ---------------------------------------------------------------------------
# Registry structural invariants.
# ---------------------------------------------------------------------------


class TestRegistryStructuralInvariants:
    def test_at_least_the_two_documented_integrations_shipped(self) -> None:
        """The PR description commits us to shipping these two. Drift
        in either direction (removing one, or shipping a duplicate) is
        something we want to see in code review."""
        slugs = {bi.slug for bi in BUILTIN_INTEGRATIONS}
        assert "cortex_cycle_journal_t16" in slugs
        assert "sibling_detection_r3" in slugs

    def test_slugs_unique(self) -> None:
        slugs = [bi.slug for bi in BUILTIN_INTEGRATIONS]
        assert len(slugs) == len(set(slugs))

    def test_fingerprints_are_lowercased(self) -> None:
        """Matcher lowercases the haystack. Keywords must match that
        convention or they silently never fire."""
        for bi in BUILTIN_INTEGRATIONS:
            for kw in bi.fingerprints:
                assert kw == kw.lower(), (
                    f"{bi.slug}: fingerprint {kw!r} must be lowercase"
                )
