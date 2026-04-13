"""
sentinel scan — multi-step project assessment.

Pipeline: explore → generate lenses → evaluate each → synthesize report.
Or --quick for a free instant state summary.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from sentinel.config.schema import SentinelConfig
from sentinel.providers.router import Router
from sentinel.roles.monitor import Monitor
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

    # Full mode — multi-step pipeline
    config = _load_config(project)
    if not config:
        return

    router = Router(config)
    monitor = Monitor(router)
    provider = router.get_provider("monitor")

    console.print(f"  Monitor provider: [bold]{provider.name}[/bold]")
    console.print()

    start = time.time()

    # Run the full multi-step pipeline
    with console.status(
        "[bold blue]Step 1/3: Exploring project and generating custom lenses..."
    ):
        result = await monitor.assess(state)

    elapsed = time.time() - start

    # Show generated lenses
    if result.lenses:
        console.print(
            f"  [green]Generated {len(result.lenses)} custom lenses:[/green]"
        )
        for lens in result.lenses:
            console.print(f"    - [bold]{lens.name}[/bold]: {lens.description}")
        console.print()

    # Show project understanding
    if result.project_summary:
        console.print(
            Panel(
                result.project_summary,
                title="[bold]Project Understanding[/bold]",
                border_style="cyan",
            )
        )
        console.print()

    # Show final report
    if result.raw_report:
        console.print(
            Panel(
                result.raw_report,
                title="[bold]Assessment Report[/bold]",
                border_style="blue",
            )
        )

    # Show cost/usage
    console.print()
    console.print(
        f"  [dim]Provider: {result.provider} | Model: {result.model}[/dim]"
    )
    console.print(
        f"  [dim]Tokens: {result.total_input_tokens} in / "
        f"{result.total_output_tokens} out[/dim]"
    )
    if result.total_cost_usd > 0:
        console.print(f"  [dim]Cost: ${result.total_cost_usd:.4f}[/dim]")
    console.print(f"  [dim]Time: {elapsed:.1f}s[/dim]")
    n_steps = 1 + len(result.lenses) + (1 if result.raw_report else 0)
    console.print(f"  [dim]LLM calls: {n_steps}[/dim]")
    console.print()
