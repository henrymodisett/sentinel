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
    """Autonomous meta-agent for managing software projects."""


@main.command()
def init() -> None:
    """Initialize Sentinel in the current project."""
    from sentinel.cli.init_cmd import run_init

    run_init()


@main.command()
@click.option(
    "--quick", "-q", is_flag=True,
    help="Quick mode — state summary only, no LLM call",
)
def scan(quick: bool) -> None:
    """Assess project health through auto-generated lenses."""
    from sentinel.cli.scan_cmd import run_scan

    asyncio.run(run_scan(quick=quick))


@main.command()
@click.option(
    "--sync-github", is_flag=True,
    help="Also create GitHub issues via gh CLI",
)
def plan(sync_github: bool) -> None:
    """Turn the most recent scan into a prioritized backlog."""
    from sentinel.cli.plan_cmd import run_plan

    asyncio.run(run_plan(sync_github=sync_github))


@main.command()
@click.option(
    "--max-items", "-n", type=int, default=3,
    help="Max work items to execute in this cycle",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Scan and plan but stop before execution",
)
def cycle(max_items: int, dry_run: bool) -> None:
    """Autonomous loop: scan → plan → execute → review."""
    from sentinel.cli.cycle_cmd import run_cycle

    asyncio.run(run_cycle(max_items=max_items, dry_run=dry_run))


@main.command()
def cost() -> None:
    """Show spend history and budget status."""
    from sentinel.cli.cost_cmd import run_cost

    run_cost()


@main.command()
def providers() -> None:
    """Show provider health and capabilities."""
    from sentinel.cli.providers_cmd import run_providers

    run_providers()


@main.command()
def status() -> None:
    """Show project health dashboard from recent scans."""
    click.echo(f"{NOT_YET}")
    click.echo("  Run `sentinel scan --quick` for quick state, `sentinel scan` for full.")
    sys.exit(1)


@main.command("config")
def config_cmd() -> None:
    """View or update role configuration."""
    click.echo(f"{NOT_YET}")
    click.echo("  Edit .sentinel/config.toml directly.")
    sys.exit(1)
