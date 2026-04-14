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


def run_cost(project_path: str | None = None) -> None:
    project = Path(project_path or os.getcwd()).resolve()
    config = _load_config(project)
    if not config:
        return

    budget = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )
    history = get_history(project, days=7)

    console.print(f"\n[bold]Sentinel Spend[/bold] — {project.name}\n")

    # Today
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

    # Last 7 days
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
