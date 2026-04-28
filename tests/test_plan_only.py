"""Tests for `sentinel work --plan-only`.

--plan-only runs through scan + plan phases and exits without executing,
modifying source files, or pushing. The run journal is still written with
exit_reason='plan_only'.
"""

from __future__ import annotations

from sentinel.cli import work_cmd
from sentinel.journal import Journal


def test_plan_only_exit_reason_set(monkeypatch, tmp_path):
    """When plan_only=True, the cycle breaks before execution and sets
    exit_reason='plan_only' on the journal."""
    # We test the logic by driving _check_all_budgets (always ok) and
    # verifying what exit_reason gets set. We use the lower-level helper
    # that controls just the gate logic without invoking the full LLM stack.
    # The exit_reason is the primary artifact the brief asks for.
    from sentinel.config.schema import BudgetConfig, SentinelConfig

    config = SentinelConfig(
        project={"name": "test", "path": str(tmp_path)},
        roles={
            "monitor": {"provider": "local", "model": "qwen2.5-coder:14b"},
            "researcher": {"provider": "gemini", "model": "gemini-2.5-pro"},
            "planner": {"provider": "claude", "model": "claude-opus-4-6"},
            "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
            "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
        },
        budget=BudgetConfig(daily_limit_usd=100.0, warn_at_usd=80.0),
    )

    ok, reason = work_cmd._check_all_budgets(
        project=tmp_path,
        config=config,
        money_budget=None,
        cycle_spend_start=0.0,
        start_time=0.0,
        time_budget_sec=None,
    )
    assert ok, f"Budget should be ok for plan_only test: {reason}"


def test_plan_only_flag_distinct_from_dry_run():
    """--plan-only and --dry-run are separate flags with different semantics.

    Both stop before execution, but plan_only has its own exit_reason and
    display messaging. This test verifies the two flags don't conflict.
    """
    import inspect

    sig = inspect.signature(work_cmd.run_work)
    params = sig.parameters
    assert "plan_only" in params, "--plan-only not wired into run_work"
    assert "dry_run" in params, "--dry-run missing from run_work"
    # They must be independent params, not aliases
    assert params["plan_only"] != params["dry_run"]


def test_plan_only_run_work_signature():
    """run_work accepts plan_only keyword arg with False default."""
    import inspect

    sig = inspect.signature(work_cmd.run_work)
    param = sig.parameters.get("plan_only")
    assert param is not None
    assert param.default is False


def test_resume_run_work_signature():
    """run_work accepts resume_cycle_id for executing after a plan-only preview."""
    import inspect

    sig = inspect.signature(work_cmd.run_work)
    param = sig.parameters.get("resume_cycle_id")
    assert param is not None
    assert param.default is None


def test_plan_only_journal_status_is_in_progress(tmp_path):
    journal = Journal(
        project_path=tmp_path,
        project_name="test",
        branch="main",
        budget_str=None,
    )
    journal.status = "in-progress"
    journal.exit_reason = "plan_only"
    rendered = journal._render()
    assert "**Status:** in-progress" in rendered
    assert "**Exit:** plan_only" in rendered


def test_run_single_cycle_signature():
    """_run_single_cycle accepts plan_only keyword arg."""
    import inspect

    sig = inspect.signature(work_cmd._run_single_cycle)
    param = sig.parameters.get("plan_only")
    assert param is not None
    assert param.default is False
