"""Sentinel CLI — the command-line interface for the meta-agent."""

from __future__ import annotations

import asyncio
import sys

import click

from sentinel import __version__

NOT_YET = "[sentinel] Not yet implemented. Coming soon."


@click.group()
@click.version_option(version=__version__)
def main() -> None:
    """Autonomous meta-agent for managing software projects.

    The one command you need:

        sentinel work             — do whatever the project needs
        sentinel work --budget 10m   — time-bounded
        sentinel work --budget $5    — money-bounded
        sentinel work --budget 10m,$5  — time AND money bounded
        sentinel work --dry-run      — plan without executing

    Advanced (for when you want granular control):

        sentinel status           — quick health check
        sentinel scan             — full scan only
        sentinel plan             — turn scan into backlog
        sentinel cycle            — alias for `work`
        sentinel cost             — spend history
        sentinel providers        — LLM provider status
        sentinel init             — manual initialization
    """


# --- PRIMARY COMMAND ---

@main.command()
@click.option(
    "--budget", "-b",
    default=None,
    help=(
        "Budget cap. Money ($5, 10.50), time (10m, 1h, 30s), or both "
        "comma-separated (10m,$5). For free providers (Gemini OAuth, "
        "Ollama) the money cap is naturally a no-op since calls cost $0."
    ),
)
@click.option(
    "--every", "-e",
    default=None,
    help="Loop mode — run continuously, sleep between cycles (e.g. 10m, 1h).",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Scan and plan but stop before execution.",
)
@click.option(
    "--auto", is_flag=True,
    help="Skip the confirmation prompt before executing.",
)
def work(budget: str | None, every: str | None, dry_run: bool, auto: bool) -> None:
    """Work on this project. One cycle, or loop with --every.

    Examples:
      sentinel work                      one cycle and exit
      sentinel work --every 10m          loop, 10 min between cycles
      sentinel work --every 1h -b $20    loop with $20 session cap
    """
    from sentinel.cli.work_cmd import run_work

    asyncio.run(
        run_work(budget_str=budget, dry_run=dry_run, auto=auto, every=every),
    )


@main.command()
def status() -> None:
    """Quick project health check (state + spend + latest cycle)."""
    from sentinel.cli.status_cmd import run_status

    run_status()


# --- ADVANCED / GRANULAR ---

@main.command(hidden=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--preset",
    type=click.Choice(
        ["recommended", "simple", "cheap", "local", "hybrid", "power"],
        case_sensitive=False,
    ),
    default=None,
    help="Use a named preset instead of interactive questions.",
)
def init(yes: bool, preset: str | None) -> None:
    """Initialize Sentinel in the current project (usually auto-run by work).

    Presets (skip interactive prompts):
      recommended  — smart defaults per role (the default)
      simple       — use claude for everything
      cheap        — prefer local / gemini-flash where possible
      local        — Ollama for cold path, claude/codex for agentic coder
      hybrid       — local Monitor + cloud everything else (best $/quality)
      power        — highest-quality model per role (expensive)
    """
    from sentinel.cli.init_cmd import run_init

    run_init(auto_yes=yes, preset=preset)


@main.command(hidden=True)
@click.option(
    "--quick", "-q", is_flag=True,
    help="Quick mode — state summary only, no LLM call",
)
def scan(quick: bool) -> None:
    """Run just the scan phase (explore → lenses → evaluate → synthesize)."""
    from sentinel.cli.scan_cmd import run_scan

    asyncio.run(run_scan(quick=quick))


@main.command(hidden=True)
@click.option(
    "--sync-github", is_flag=True,
    help="Also create GitHub issues via gh CLI",
)
def plan(sync_github: bool) -> None:
    """Turn the most recent scan into a prioritized backlog."""
    from sentinel.cli.plan_cmd import run_plan

    asyncio.run(run_plan(sync_github=sync_github))


@main.command(hidden=True)
@click.option(
    "--max-items", "-n", type=int, default=3,
    help="Max work items to execute in this cycle",
)
@click.option("--dry-run", is_flag=True, help="Scan and plan but stop before execution")
def cycle(max_items: int, dry_run: bool) -> None:
    """Alias for `work --max-items N`. Kept for compatibility."""
    from sentinel.cli.cycle_cmd import run_cycle

    asyncio.run(run_cycle(max_items=max_items, dry_run=dry_run))


@main.command()
def cost() -> None:
    """Show spend history and budget status."""
    from sentinel.cli.cost_cmd import run_cost

    run_cost()


@main.group(invoke_without_command=False)
def routing() -> None:
    """Inspect and tune router decisions."""


@routing.command("show")
@click.option(
    "--limit", "-n", type=int, default=10,
    help="How many recent cycles to scan (default 10).",
)
def routing_show(limit: int) -> None:
    """Show routing overrides from recent run journals."""
    from sentinel.cli.routing_cmd import run_routing_show

    run_routing_show(limit=limit)


@main.command()
def providers() -> None:
    """Show LLM provider detection and health."""
    from sentinel.cli.providers_cmd import run_providers

    run_providers()


@main.command("config", hidden=True)
def config_cmd() -> None:
    """View or update role configuration."""
    click.echo(f"{NOT_YET}")
    click.echo("  Edit .sentinel/config.toml directly.")
    sys.exit(1)
