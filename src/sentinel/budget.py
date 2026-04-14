"""
Budget tracking and enforcement.

Tracks cumulative spend per day in .sentinel/state/spend.json.
Enforces daily_limit_usd from config — refuses new scans/cycles
when the limit is hit, warns when approaching it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path  # noqa: TC003 — used at runtime


@dataclass
class BudgetStatus:
    today_spent_usd: float
    daily_limit_usd: float
    warn_at_usd: float
    over_limit: bool
    warning: bool
    remaining_usd: float


def _state_dir(project_path: Path) -> Path:
    d = project_path / ".sentinel" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _spend_file(project_path: Path) -> Path:
    return _state_dir(project_path) / "spend.json"


def _load_spend(project_path: Path) -> dict:
    """Load the spend log. Format: {"YYYY-MM-DD": {"total_usd": N, "entries": [...]}}"""
    path = _spend_file(project_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_spend(project_path: Path, data: dict) -> None:
    _spend_file(project_path).write_text(json.dumps(data, indent=2))


def today_key() -> str:
    return date.today().isoformat()


def record_spend(
    project_path: Path, amount_usd: float, category: str, details: str = "",
) -> None:
    """Record a spend event. Categories: 'scan', 'plan', 'cycle', 'research'."""
    if amount_usd <= 0:
        return

    data = _load_spend(project_path)
    today = today_key()
    if today not in data:
        data[today] = {"total_usd": 0.0, "entries": []}

    data[today]["total_usd"] += amount_usd
    data[today]["entries"].append({
        "timestamp": datetime.now().isoformat(),
        "amount_usd": amount_usd,
        "category": category,
        "details": details,
    })
    _save_spend(project_path, data)


def check_budget(
    project_path: Path, daily_limit_usd: float, warn_at_usd: float,
) -> BudgetStatus:
    """Check current budget status for today."""
    data = _load_spend(project_path)
    today = today_key()
    spent = data.get(today, {}).get("total_usd", 0.0)

    return BudgetStatus(
        today_spent_usd=spent,
        daily_limit_usd=daily_limit_usd,
        warn_at_usd=warn_at_usd,
        over_limit=spent >= daily_limit_usd,
        warning=spent >= warn_at_usd,
        remaining_usd=max(0.0, daily_limit_usd - spent),
    )


def get_history(project_path: Path, days: int = 7) -> dict:
    """Get spend history for the last N days. Returns {date: total_usd}."""
    data = _load_spend(project_path)
    return {k: v["total_usd"] for k, v in sorted(data.items(), reverse=True)[:days]}
