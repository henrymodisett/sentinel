"""
sentinel scan — assess the project through lenses.

Uses the Monitor role for the full LLM-powered scan, or --quick
for a free instant state summary without an LLM call.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from sentinel.config.schema import SentinelConfig
from sentinel.providers.router import Router
from sentinel.roles.monitor import Monitor, load_lenses
from sentinel.state import gather_state

console = Console()


def _load_config(project_path: Path) -> SentinelConfig | None:
    config_file = project_path / ".sentinel" / "config.toml"
    if not config_file.exists():
        console.print(
            "[red]No .sentinel/config.toml found. Run `sentinel init` first.[/red]"
        )
        return None
    import tomllib

    data = tomllib.loads(config_file.read_text())
    return SentinelConfig(**data)


def _print_state_summary(state: object) -> None:
    """Print the quick state summary (shared between quick and full mode)."""
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

    if state.errors:
        for err in state.errors:
            console.print(f"  [yellow]Warning: {err}[/yellow]")


async def run_scan(
    project_path: str | None = None, quick: bool = False,
) -> None:
    """Run the scan command."""
    project = Path(project_path or os.getcwd()).resolve()

    console.print(f"\n[bold]Sentinel Scan[/bold] — {project.name}")
    if quick:
        console.print("[dim]  (quick mode — no LLM call)[/dim]")
    console.print()

    # Gather state
    with console.status("[bold blue]Gathering project state..."):
        state = gather_state(project)

    _print_state_summary(state)
    console.print()

    # Quick mode — just show state, no LLM
    if quick:
        if state.recent_commits:
            console.print("[bold]Recent commits:[/bold]")
            for line in state.recent_commits.splitlines()[:5]:
                console.print(f"  {line}")
            console.print()
        return

    # Full mode — need config and lenses
    config = _load_config(project)
    if not config:
        return

    lenses = load_lenses(project, config.lenses.enabled)
    console.print(f"  Active lenses: {', '.join(lenses.keys())}")
    if not lenses:
        console.print(
            "[yellow]  No lenses found. Run `sentinel init` to install.[/yellow]"
        )
        return

    # Use the Monitor role
    router = Router(config)
    monitor = Monitor(router)
    provider = router.get_provider("monitor")

    console.print(f"  Monitor provider: [bold]{provider.name}[/bold]")
    console.print()

    start = time.time()
    n_lenses = len(lenses)
    prov_name = provider.name
    with console.status(
        f"[bold blue]Scanning through {n_lenses} lenses via {prov_name}..."
    ):
        result = await monitor.assess(state, lenses)

    elapsed = time.time() - start

    console.print(
        Panel(
            result.raw_response,
            title="[bold]Scan Results[/bold]",
            border_style="blue",
        )
    )

    console.print()
    console.print(
        f"  [dim]Provider: {result.provider} | Model: {result.model}[/dim]"
    )
    console.print(
        f"  [dim]Tokens: {result.input_tokens} in / {result.output_tokens} out[/dim]"
    )
    if result.cost_usd > 0:
        console.print(f"  [dim]Cost: ${result.cost_usd:.4f}[/dim]")
    console.print(f"  [dim]Time: {elapsed:.1f}s[/dim]")
    console.print()
