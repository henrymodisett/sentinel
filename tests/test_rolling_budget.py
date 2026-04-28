"""Tests for rolling 24h and 7d budget caps.

Per-day and per-week caps aggregate cycle costs over rolling windows.
On cap breach: _check_all_budgets returns (False, reason) with a clear
message naming the window and the amounts.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sentinel.budget import check_rolling_budgets, record_spend, rolling_spend_usd
from sentinel.cli import work_cmd
from sentinel.config.schema import BudgetConfig, SentinelConfig

if TYPE_CHECKING:
    from pathlib import Path


def _config(
    per_day_usd: float | None = None,
    per_week_usd: float | None = None,
    daily_limit: float = 1000.0,
) -> SentinelConfig:
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
            daily_limit_usd=daily_limit,
            warn_at_usd=daily_limit * 0.8,
            per_day_usd=per_day_usd,
            per_week_usd=per_week_usd,
        ),
    )


# ─── rolling_spend_usd ────────────────────────────────────────────────────────

class TestRollingSpendUsd:
    def test_empty_spend_file_returns_zero(self, tmp_path: Path) -> None:
        assert rolling_spend_usd(tmp_path, hours=24) == 0.0

    def test_recent_entry_counted(self, tmp_path: Path) -> None:
        record_spend(tmp_path, 5.0, "work-execute", "item=foo")
        result = rolling_spend_usd(tmp_path, hours=24)
        assert result == 5.0

    def test_multiple_recent_entries_summed(self, tmp_path: Path) -> None:
        record_spend(tmp_path, 2.0, "scan", "")
        record_spend(tmp_path, 3.5, "work-execute", "")
        result = rolling_spend_usd(tmp_path, hours=24)
        assert abs(result - 5.5) < 0.001

    def test_old_entry_excluded(self, tmp_path: Path) -> None:
        """Entries older than the window should not be counted."""
        from sentinel.budget import today_key

        # Inject a spend entry with an old timestamp directly into the file
        old_ts = (datetime.now() - timedelta(hours=25)).isoformat()
        data = {
            today_key(): {
                "total_usd": 10.0,
                "entries": [
                    {"timestamp": old_ts, "amount_usd": 10.0,
                     "category": "scan", "details": ""},
                ],
            }
        }
        from sentinel.budget import _spend_file
        _spend_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _spend_file(tmp_path).write_text(__import__("json").dumps(data))

        result = rolling_spend_usd(tmp_path, hours=24)
        assert result == 0.0, f"Old entry should be excluded but got {result}"

    def test_7d_window_includes_recent_5_days(self, tmp_path: Path) -> None:
        """A spend 5 days ago is within the 7d window."""
        from sentinel.budget import _spend_file

        ts_5d_ago = (datetime.now() - timedelta(days=5)).isoformat()
        date_5d_ago = (datetime.now() - timedelta(days=5)).date().isoformat()
        data = {
            date_5d_ago: {
                "total_usd": 20.0,
                "entries": [
                    {"timestamp": ts_5d_ago, "amount_usd": 20.0,
                     "category": "cycle", "details": ""},
                ],
            }
        }
        _spend_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _spend_file(tmp_path).write_text(json.dumps(data))

        result = rolling_spend_usd(tmp_path, hours=24 * 7)
        assert abs(result - 20.0) < 0.001

    def test_8d_old_entry_excluded_from_7d_window(self, tmp_path: Path) -> None:
        from sentinel.budget import _spend_file

        ts_8d_ago = (datetime.now() - timedelta(days=8)).isoformat()
        date_8d_ago = (datetime.now() - timedelta(days=8)).date().isoformat()
        data = {
            date_8d_ago: {
                "total_usd": 100.0,
                "entries": [
                    {"timestamp": ts_8d_ago, "amount_usd": 100.0,
                     "category": "cycle", "details": ""},
                ],
            }
        }
        _spend_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _spend_file(tmp_path).write_text(json.dumps(data))

        result = rolling_spend_usd(tmp_path, hours=24 * 7)
        assert result == 0.0


# ─── check_rolling_budgets ────────────────────────────────────────────────────

class TestCheckRollingBudgets:
    def test_no_caps_always_ok(self, tmp_path: Path) -> None:
        ok, reason = check_rolling_budgets(tmp_path, per_day_usd=None, per_week_usd=None)
        assert ok
        assert reason == ""

    def test_per_day_blocks_when_exceeded(self, tmp_path: Path) -> None:
        record_spend(tmp_path, 55.0, "cycle", "")
        ok, reason = check_rolling_budgets(
            tmp_path, per_day_usd=50.0, per_week_usd=None,
        )
        assert not ok
        assert "per-day budget reached" in reason
        assert "$55.00" in reason
        assert "$50.00" in reason

    def test_per_day_allows_when_under(self, tmp_path: Path) -> None:
        record_spend(tmp_path, 30.0, "cycle", "")
        ok, _ = check_rolling_budgets(
            tmp_path, per_day_usd=50.0, per_week_usd=None,
        )
        assert ok

    def test_per_week_blocks_when_exceeded(self, tmp_path: Path) -> None:
        record_spend(tmp_path, 210.0, "cycle", "")
        ok, reason = check_rolling_budgets(
            tmp_path, per_day_usd=None, per_week_usd=200.0,
        )
        assert not ok
        assert "per-week budget reached" in reason
        assert "$200.00" in reason

    def test_per_day_checked_before_per_week(self, tmp_path: Path) -> None:
        """When both caps are exceeded, per-day message surfaces first."""
        record_spend(tmp_path, 300.0, "cycle", "")
        ok, reason = check_rolling_budgets(
            tmp_path, per_day_usd=50.0, per_week_usd=200.0,
        )
        assert not ok
        assert "per-day" in reason  # day checked first


# ─── _check_all_budgets integration ──────────────────────────────────────────

class TestCheckAllBudgetsWithRolling:
    def test_per_day_cap_halts_cycle(self, monkeypatch, tmp_path: Path) -> None:
        """_check_all_budgets returns False when per_day_usd is exceeded."""
        from sentinel.budget import BudgetStatus

        monkeypatch.setattr(
            work_cmd, "check_budget",
            lambda *_a, **_kw: BudgetStatus(
                today_spent_usd=60.0, daily_limit_usd=1000.0,
                warn_at_usd=800.0, over_limit=False,
                warning=False, remaining_usd=940.0,
            ),
        )
        # Inject actual spend so rolling_spend_usd picks it up
        record_spend(tmp_path, 60.0, "cycle", "")

        config = _config(per_day_usd=50.0)
        ok, reason = work_cmd._check_all_budgets(
            project=tmp_path,
            config=config,
            money_budget=None,
            cycle_spend_start=0.0,
            start_time=0.0,
            time_budget_sec=None,
        )
        assert not ok
        assert "per-day" in reason

    def test_per_week_cap_halts_cycle(self, monkeypatch, tmp_path: Path) -> None:
        from sentinel.budget import BudgetStatus

        monkeypatch.setattr(
            work_cmd, "check_budget",
            lambda *_a, **_kw: BudgetStatus(
                today_spent_usd=210.0, daily_limit_usd=1000.0,
                warn_at_usd=800.0, over_limit=False,
                warning=False, remaining_usd=790.0,
            ),
        )
        record_spend(tmp_path, 210.0, "cycle", "")

        config = _config(per_week_usd=200.0)
        ok, reason = work_cmd._check_all_budgets(
            project=tmp_path,
            config=config,
            money_budget=None,
            cycle_spend_start=0.0,
            start_time=0.0,
            time_budget_sec=None,
        )
        assert not ok
        assert "per-week" in reason

    def test_no_rolling_cap_cycles_proceed(self, monkeypatch, tmp_path: Path) -> None:
        from sentinel.budget import BudgetStatus

        monkeypatch.setattr(
            work_cmd, "check_budget",
            lambda *_a, **_kw: BudgetStatus(
                today_spent_usd=5.0, daily_limit_usd=100.0,
                warn_at_usd=80.0, over_limit=False,
                warning=False, remaining_usd=95.0,
            ),
        )
        config = _config()  # no rolling caps
        ok, _ = work_cmd._check_all_budgets(
            project=tmp_path,
            config=config,
            money_budget=None,
            cycle_spend_start=0.0,
            start_time=0.0,
            time_budget_sec=None,
        )
        assert ok


# ─── BudgetConfig schema ─────────────────────────────────────────────────────

class TestBudgetConfigSchema:
    def test_per_day_per_week_default_to_none(self) -> None:
        b = BudgetConfig()
        assert b.per_day_usd is None
        assert b.per_week_usd is None

    def test_per_day_per_week_roundtrip(self) -> None:
        b = BudgetConfig(daily_limit_usd=15.0, per_day_usd=50.0, per_week_usd=200.0)
        assert b.per_day_usd == 50.0
        assert b.per_week_usd == 200.0

    def test_per_day_per_week_toml_aliases(self) -> None:
        b = BudgetConfig(daily_limit_usd=15.0, per_day=50.0, per_week=200.0)
        assert b.per_day_usd == 50.0
        assert b.per_week_usd == 200.0
