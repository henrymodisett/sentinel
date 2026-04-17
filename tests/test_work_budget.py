"""Per-run budget semantics for `sentinel work --budget $X`.

Dogfood on portfolio_new (2026-04-17) surfaced that `--budget $X` was
behaving as a second daily cap rather than a per-run cap: a fresh cycle
with `--budget $2` was blocked at start because the day had already
spent $2.12 across earlier runs. Loop mode handled this correctly via
`session_spend_start`; single-cycle mode did not. These tests pin the
fix: snapshot `today_spent_usd` at cycle start, compare the delta.
"""

from __future__ import annotations

from sentinel.budget import BudgetStatus
from sentinel.cli import work_cmd
from sentinel.config.schema import BudgetConfig, SentinelConfig


def _config(daily_limit: float = 100.0, warn_at: float = 80.0) -> SentinelConfig:
    return SentinelConfig(
        project={"name": "test", "path": "/tmp/test"},
        roles={
            "monitor": {"provider": "local", "model": "qwen2.5-coder:14b"},
            "researcher": {"provider": "gemini", "model": "gemini-2.5-pro"},
            "planner": {"provider": "claude", "model": "claude-opus-4-6"},
            "coder": {"provider": "claude", "model": "claude-sonnet-4-6"},
            "reviewer": {"provider": "gemini", "model": "gemini-2.5-pro"},
        },
        budget=BudgetConfig(
            daily_limit_usd=daily_limit, warn_at_usd=warn_at,
        ),
    )


def _budget_status(spent: float, daily_limit: float = 100.0) -> BudgetStatus:
    return BudgetStatus(
        today_spent_usd=spent,
        daily_limit_usd=daily_limit,
        warn_at_usd=daily_limit * 0.8,
        over_limit=spent >= daily_limit,
        warning=spent >= daily_limit * 0.8,
        remaining_usd=max(0.0, daily_limit - spent),
    )


def test_per_run_budget_allows_when_daily_already_exceeded_cap(monkeypatch, tmp_path):
    """Cycle starts after $2.12 daily spend; --budget $2 should NOT block.

    The cycle's own spend is $0 — well under its $2 cap. The daily total
    is irrelevant to the per-run gate.
    """
    monkeypatch.setattr(
        work_cmd, "check_budget",
        lambda *_args, **_kw: _budget_status(spent=2.12),
    )
    ok, reason = work_cmd._check_all_budgets(
        project=tmp_path,
        config=_config(),
        money_budget=2.00,
        cycle_spend_start=2.12,
        start_time=0.0,
        time_budget_sec=None,
    )
    assert ok, f"Per-run cap blocked unexpectedly: {reason}"


def test_per_run_budget_blocks_when_cycle_spend_exceeds_cap(monkeypatch, tmp_path):
    """Cycle started at $2.12, now at $4.50 — cycle spent $2.38 of its $2 cap."""
    monkeypatch.setattr(
        work_cmd, "check_budget",
        lambda *_args, **_kw: _budget_status(spent=4.50),
    )
    ok, reason = work_cmd._check_all_budgets(
        project=tmp_path,
        config=_config(),
        money_budget=2.00,
        cycle_spend_start=2.12,
        start_time=0.0,
        time_budget_sec=None,
    )
    assert not ok
    assert "per-run budget reached" in reason
    # Reports cycle delta, not daily total
    assert "$2.38" in reason
    assert "$2.00" in reason


def test_daily_limit_still_blocks_independent_of_per_run(monkeypatch, tmp_path):
    """If the daily limit is hit, cycle blocks even with per-run room left."""
    monkeypatch.setattr(
        work_cmd, "check_budget",
        lambda *_args, **_kw: _budget_status(spent=10.00, daily_limit=10.00),
    )
    ok, reason = work_cmd._check_all_budgets(
        project=tmp_path,
        config=_config(daily_limit=10.00),
        money_budget=100.00,
        cycle_spend_start=10.00,
        start_time=0.0,
        time_budget_sec=None,
    )
    assert not ok
    assert "daily budget reached" in reason


def test_per_run_budget_message_says_per_run_not_session(monkeypatch, tmp_path):
    """The pre-fix message said 'session money budget reached' which doubled
    the confusion (it's neither session nor reflective of cycle spend)."""
    monkeypatch.setattr(
        work_cmd, "check_budget",
        lambda *_args, **_kw: _budget_status(spent=5.00),
    )
    _, reason = work_cmd._check_all_budgets(
        project=tmp_path,
        config=_config(),
        money_budget=1.00,
        cycle_spend_start=0.00,
        start_time=0.0,
        time_budget_sec=None,
    )
    assert "session money budget" not in reason
    assert "per-run budget" in reason


def test_no_money_budget_means_no_per_run_gate(monkeypatch, tmp_path):
    """Without --budget $X, only the daily limit applies."""
    monkeypatch.setattr(
        work_cmd, "check_budget",
        lambda *_args, **_kw: _budget_status(spent=50.00),
    )
    ok, _ = work_cmd._check_all_budgets(
        project=tmp_path,
        config=_config(daily_limit=100.00),
        money_budget=None,
        cycle_spend_start=0.00,
        start_time=0.0,
        time_budget_sec=None,
    )
    assert ok
