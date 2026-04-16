"""sentinel routing show — inspect router decisions from recent journals.

Each `sentinel work` cycle records its routing overrides inline with the
provider calls (`routed_via` field on each call). This command reads
recent journals and surfaces those overrides in one place so the user
can see which rules are firing in practice — useful for tuning the
DEFAULT_RULES set when dogfood reveals a new failure mode.
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console
from rich.table import Table

from sentinel.journal import parse_journal_calls

console = Console()


def _runs_dir(project_path: Path) -> Path:
    return project_path / ".sentinel" / "runs"


def run_routing_show(
    project_path: str | None = None, limit: int = 10,
) -> None:
    """Show recent routing overrides across the last `limit` cycles."""
    project = Path(project_path or os.getcwd()).resolve()
    runs = _runs_dir(project)
    if not runs.exists():
        console.print(
            "[yellow]No run journals found "
            "(.sentinel/runs/ doesn't exist).[/yellow]"
        )
        return

    journals = sorted(runs.glob("*.md"), reverse=True)[:limit]
    if not journals:
        console.print("[yellow]No run journals to inspect.[/yellow]")
        return

    overrides: list[tuple[str, dict]] = []
    for journal_path in journals:
        for call in parse_journal_calls(journal_path):
            if call.get("routed_via"):
                overrides.append((journal_path.stem, call))

    console.print(
        f"\n[bold]Sentinel Routing[/bold] — {project.name} "
        f"(last {len(journals)} cycles)\n"
    )

    if not overrides:
        console.print(
            "[dim]No routing overrides in the last "
            f"{len(journals)} cycles. Calls used the configured "
            "(provider, model) pairs without override.[/dim]\n"
        )
        return

    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Cycle")
    table.add_column("Phase")
    table.add_column("Role")
    table.add_column("Model")
    table.add_column("Rule")
    for cycle_id, call in overrides:
        table.add_row(
            cycle_id,
            call.get("phase", ""),
            call.get("role", ""),
            call.get("model", ""),
            call.get("routed_via", ""),
        )
    console.print(table)
    console.print(
        f"\n  [dim]{len(overrides)} override"
        f"{'s' if len(overrides) != 1 else ''} across "
        f"{len(journals)} cycle"
        f"{'s' if len(journals) != 1 else ''}[/dim]\n"
    )
