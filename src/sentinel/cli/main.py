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
    help="Budget cap. Money ($5, 10.50) or time (10m, 1h, 30s).",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Scan and plan but stop before execution.",
)
@click.option(
    "--auto", is_flag=True,
    help="Skip the confirmation prompt before executing.",
)
def work(budget: str | None, dry_run: bool, auto: bool) -> None:
    """Work on this project. Init/scan/plan/execute as needed."""
    from sentinel.cli.work_cmd import run_work

    asyncio.run(run_work(budget_str=budget, dry_run=dry_run, auto=auto))


@main.command()
def status() -> None:
    """Quick project health check (state + latest scan summary)."""
    click.echo(f"{NOT_YET}")
    click.echo("  Use `sentinel scan --quick` for state, `sentinel cost` for spend.")
    sys.exit(1)


# --- ADVANCED / GRANULAR ---

@main.command(hidden=True)
def init() -> None:
    """Initialize Sentinel in the current project (usually auto-run by work)."""
    from sentinel.cli.init_cmd import run_init

    run_init()


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
