"""Tests for the failure-surfacing helpers in work_cmd.

When a `sentinel work` cycle fails, the user used to see one cryptic
line and had to grep the journal to figure out what to try next.
`_build_failure_summary` extracts the failing phase + last erroring
provider call from the journal and matches the error pattern to a
suggested next action; `_suggest_next_action` is the keyed lookup
table.

These are pure functions over journal state, so they're cheap to
exercise directly without spinning up the whole CLI.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — runtime use via tmp_path

from sentinel.cli.work_cmd import _build_failure_summary, _suggest_next_action
from sentinel.journal import Journal, PhaseRecord, ProviderCall


def _journal(tmp_path: Path) -> Journal:
    return Journal(
        project_path=tmp_path,
        project_name="test",
        branch="main",
        budget_str="5m",
    )


class TestBuildFailureSummary:
    def test_returns_blank_when_no_failure(self, tmp_path: Path) -> None:
        """Successful cycles get the regular Done panel — the failure
        summary returns "" so the caller can branch on that."""
        j = _journal(tmp_path)
        j.start_phase("scan")
        j.end_phase("scan")
        j.exit_reason = "backlog_empty"
        assert _build_failure_summary(j) == ""

    def test_failed_phase_surfaces_phase_and_error(
        self, tmp_path: Path,
    ) -> None:
        """A phase marked as failed should appear in the summary along
        with its error reason — that's the most actionable detail."""
        j = _journal(tmp_path)
        j.start_phase("scan")
        j.end_phase("scan", status="failed", error="Gemini CLI timed out after 600s")
        j.exit_reason = "scan_failed"
        summary = _build_failure_summary(j)
        assert "scan" in summary
        assert "Gemini CLI timed out" in summary

    def test_last_erroring_call_is_surfaced(self, tmp_path: Path) -> None:
        """When a provider call carries an error, surface (role,
        provider, model, error) so the user can see what failed without
        opening the journal."""
        j = _journal(tmp_path)
        j.start_phase("scan")
        j.end_phase("scan", status="failed", error="synthesis timed out")
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="gemini-2.5-flash",
            latency_ms=600_000, role="monitor",
            error="timeout",
        ))
        j.exit_reason = "scan_failed"
        summary = _build_failure_summary(j)
        assert "monitor" in summary
        assert "gemini-2.5-flash" in summary
        assert "timeout" in summary

    def test_routing_rule_attribution_when_present(
        self, tmp_path: Path,
    ) -> None:
        """If the failing call was routed via a rule override, name the
        rule — that closes the loop on 'why was this model chosen?'"""
        j = _journal(tmp_path)
        j.start_phase("scan")
        j.end_phase("scan", status="failed", error="x")
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="gemini-2.5-pro",
            latency_ms=222_000, role="monitor",
            error="non-zero exit",
            routed_via="huge-eval-prefers-flash",
        ))
        summary = _build_failure_summary(j)
        assert "huge-eval-prefers-flash" in summary


class TestSuggestNextAction:
    def test_budget_exhausted_suggestion(self) -> None:
        call = ProviderCall(
            phase="scan", provider="gemini", model="m",
            latency_ms=0, error="budget_exhausted",
        )
        assert "budget" in _suggest_next_action("", None, call).lower()

    def test_timeout_suggestion(self) -> None:
        call = ProviderCall(
            phase="scan", provider="gemini", model="m",
            latency_ms=600_000, error="timeout",
        )
        suggestion = _suggest_next_action("scan_failed", None, call)
        assert "budget" in suggestion.lower() or "model" in suggestion.lower()

    def test_non_zero_exit_suggestion_mentions_routing(self) -> None:
        """When a CLI fails non-zero, the most useful action is to
        consider whether routing should pre-emptively avoid the model.
        Direct the user to `sentinel routing show` and DEFAULT_RULES."""
        call = ProviderCall(
            phase="scan", provider="gemini", model="m",
            latency_ms=200_000, error="non-zero exit",
        )
        suggestion = _suggest_next_action("scan_failed", None, call)
        assert "routing" in suggestion.lower()

    def test_cli_is_error_suggests_provider_auth(self) -> None:
        call = ProviderCall(
            phase="scan", provider="claude", model="m",
            latency_ms=100, error="cli is_error",
        )
        suggestion = _suggest_next_action("scan_failed", None, call)
        assert "providers" in suggestion.lower() or "auth" in suggestion.lower()

    def test_no_pattern_matches_returns_blank(self) -> None:
        """If we can't recognize the failure, return "" rather than a
        wrong suggestion. The summary still shows the raw error."""
        call = ProviderCall(
            phase="scan", provider="x", model="y",
            latency_ms=1, error="some-novel-error",
        )
        assert _suggest_next_action("", None, call) == ""

    def test_scan_phase_failure_without_call_suggests_partial(self) -> None:
        """Some scan failures don't have a clear erroring call — the
        partial-scan rescue is the next thing worth pointing at."""
        phase = PhaseRecord(name="scan", started_at=0, ended_at=1, status="failed")
        suggestion = _suggest_next_action("scan_failed", phase, None)
        assert "partial" in suggestion.lower()
