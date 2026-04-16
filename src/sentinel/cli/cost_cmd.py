"""sentinel cost — show spend history and budget status."""

from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console
from rich.table import Table

from sentinel.budget import check_budget, get_history
from sentinel.config.schema import SentinelConfig

console = Console()


def _load_config(project_path: Path) -> SentinelConfig | None:
    config_file = project_path / ".sentinel" / "config.toml"
    if not config_file.exists():
        console.print("[red]No .sentinel/config.toml found.[/red]")
        return None
    import tomllib

    return SentinelConfig(**tomllib.loads(config_file.read_text()))


def run_cost(
    project_path: str | None = None,
    *,
    by_role: bool = False,
    cycles: int = 20,
) -> None:
    project = Path(project_path or os.getcwd()).resolve()
    config = _load_config(project)
    if not config:
        return

    budget = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )

    console.print(f"\n[bold]Sentinel Spend[/bold] — {project.name}\n")

    # Today (always shown — daily-limit context applies in both views)
    console.print("[bold]Today:[/bold]")
    if budget.over_limit:
        marker = "[red]OVER LIMIT[/red]"
    elif budget.warning:
        marker = "[yellow]⚠[/yellow]"
    else:
        marker = "[green]✓[/green]"
    console.print(
        f"  {marker} ${budget.today_spent_usd:.4f} / "
        f"${budget.daily_limit_usd:.2f} "
        f"(remaining: ${budget.remaining_usd:.2f})"
    )
    console.print()

    if by_role:
        _print_by_role(project, cycles)
        return

    history = get_history(project, days=7)
    console.print("[bold]Last 7 days:[/bold]")
    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Date")
    table.add_column("Spent", justify="right")
    total = 0.0
    for date_str, spent in history.items():
        table.add_row(date_str, f"${spent:.4f}")
        total += spent
    if not history:
        console.print("  [dim]No spend recorded yet.[/dim]")
    else:
        console.print(table)
        console.print(f"\n  [bold]Total 7-day:[/bold] ${total:.4f}")
    console.print()


def _print_by_role(project: Path, cycles: int) -> None:
    """Aggregate cost + token usage by role across the last `cycles`
    journals. Reads `.sentinel/runs/<ts>.md` directly — same source of
    truth the per-cycle "By role" table uses, just rolled up across
    runs."""
    from sentinel.journal import parse_journal_calls

    runs = project / ".sentinel" / "runs"
    if not runs.exists():
        console.print(
            "[dim]No run journals found "
            "(.sentinel/runs/ doesn't exist).[/dim]\n"
        )
        return

    journals = sorted(runs.glob("*.md"), reverse=True)[:cycles]
    if not journals:
        console.print("[dim]No run journals to aggregate.[/dim]\n")
        return

    by_role: dict[str, dict] = {}  # role → {calls, cost, in, out}
    for journal_path in journals:
        for call in parse_journal_calls(journal_path):
            role = call.get("role") or "(untagged)"
            stats = by_role.setdefault(
                role, {"calls": 0, "cost": 0.0, "in": 0, "out": 0},
            )
            stats["calls"] += 1
            stats["cost"] += float(call.get("cost", 0.0))
            stats["in"] += int(call.get("in", 0))
            stats["out"] += int(call.get("out", 0))

    console.print(
        f"[bold]By role[/bold] (last {len(journals)} cycle"
        f"{'s' if len(journals) != 1 else ''}):"
    )
    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Role")
    table.add_column("Calls", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Tokens (in/out)", justify="right")

    total_cost = 0.0
    for role in sorted(by_role):
        s = by_role[role]
        table.add_row(
            role,
            f"{s['calls']:,}",
            f"${s['cost']:.4f}",
            f"{s['in']:,}/{s['out']:,}",
        )
        total_cost += s["cost"]

    console.print(table)
    console.print(f"\n  [bold]Total cost:[/bold] ${total_cost:.4f}\n")
