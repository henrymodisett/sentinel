"""sentinel status — one-screen project health check.

Combines what the existing `scan --quick`, `cost`, and `routing show`
commands print individually so the user gets the most-useful slice of
project state without three commands. Read-only and free (no LLM
calls).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from sentinel.budget import check_budget
from sentinel.config.schema import SentinelConfig
from sentinel.state import gather_state

console = Console()


def _load_config(project_path: Path) -> SentinelConfig | None:
    config_file = project_path / ".sentinel" / "config.toml"
    if not config_file.exists():
        return None
    import tomllib

    return SentinelConfig(**tomllib.loads(config_file.read_text()))


def _latest_journal(project: Path) -> Path | None:
    runs = project / ".sentinel" / "runs"
    if not runs.exists():
        return None
    journals = sorted(runs.glob("*.md"), reverse=True)
    return journals[0] if journals else None


def _summarize_journal(path: Path) -> str:
    """Pull the header summary out of a journal markdown file —
    everything between the header and the first ## section. Keeps the
    status command independent of the journal's render format internals."""
    try:
        text = path.read_text()
    except OSError:
        return f"(could not read {path.name})"
    # Header lines: project/branch/budget/exit + total time/cost summary.
    summary_match = re.search(
        r"\*\*Project:.*?\*\*Provider calls:.*?$",
        text, re.DOTALL | re.MULTILINE,
    )
    return summary_match.group(0) if summary_match else f"(no header in {path.name})"


def run_status(project_path: str | None = None) -> None:
    project = Path(project_path or os.getcwd()).resolve()
    config = _load_config(project)

    console.print(f"\n[bold]Sentinel Status[/bold] — {project.name}\n")

    if not config:
        console.print(
            "[yellow]No .sentinel/config.toml found. "
            "Run `sentinel work` to initialize.[/yellow]\n"
        )
        return

    # --- State (free, no LLM) ---
    state = gather_state(project)
    console.print("[bold]State:[/bold]")
    console.print(f"  Branch: {state.branch}")
    console.print(f"  Uncommitted: {state.uncommitted_files} files")
    if state.tests_passed:
        console.print("  Tests: [green]passing[/green]")
    elif state.tests_passed is False:
        console.print("  Tests: [red]failing[/red]")
    else:
        console.print("  Tests: [dim]no test command[/dim]")
    if state.lint_clean:
        console.print("  Lint: [green]clean[/green]")
    elif state.lint_clean is False:
        console.print("  Lint: [red]issues[/red]")
    else:
        console.print("  Lint: [dim]no lint command[/dim]")
    console.print()

    # --- Spend ---
    budget = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )
    if budget.over_limit:
        marker = "[red]OVER LIMIT[/red]"
    elif budget.warning:
        marker = "[yellow]⚠[/yellow]"
    else:
        marker = "[green]✓[/green]"
    console.print("[bold]Spend (today):[/bold]")
    console.print(
        f"  {marker} ${budget.today_spent_usd:.4f} / "
        f"${budget.daily_limit_usd:.2f}"
    )
    console.print()

    # --- Latest cycle ---
    latest = _latest_journal(project)
    if latest:
        console.print("[bold]Latest cycle:[/bold]")
        console.print(
            Panel(
                _summarize_journal(latest),
                title=f"[dim]{latest.name}[/dim]",
                border_style="dim",
            )
        )
        console.print(
            f"  [dim]Inspect: {latest.relative_to(project)}[/dim]"
        )
    else:
        console.print(
            "[dim]No cycles run yet — `sentinel work` to start.[/dim]"
        )
    console.print()
