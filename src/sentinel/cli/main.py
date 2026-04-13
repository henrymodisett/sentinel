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
@click.option("--quick", "-q", is_flag=True, help="Quick mode — state summary only, no LLM call")
def scan(quick: bool) -> None:
    """Assess current project state through lenses."""
    from sentinel.cli.scan_cmd import run_scan

    asyncio.run(run_scan(quick=quick))


@main.command()
def providers() -> None:
    """Show provider health and capabilities."""
    from sentinel.cli.providers_cmd import run_providers

    run_providers()


@main.command()
def cycle() -> None:
    """Run one full loop: assess -> research -> plan -> execute -> review."""
    click.echo(f"{NOT_YET}")
    click.echo("  Use `sentinel scan` for assessment-only mode.")
    sys.exit(1)


@main.command()
def watch() -> None:
    """Continuous mode — run the loop on a schedule."""
    click.echo(f"{NOT_YET}")
    click.echo("  Use `/loop` in Claude Code for continuous mode.")
    sys.exit(1)


@main.command()
@click.argument("topic", required=False)
@click.option(
    "--mode",
    type=click.Choice(["targeted", "exploratory", "comparative", "consensus"]),
    default="targeted",
    help="Research mode",
)
def research(topic: str | None, mode: str) -> None:
    """Run deep research on a topic."""
    click.echo(f"{NOT_YET}")
    click.echo("  Use `/sentinel-research` in Claude Code.")
    sys.exit(1)


@main.command()
def plan() -> None:
    """Generate a prioritized backlog from current state."""
    click.echo(f"{NOT_YET}")
    click.echo("  Use `/sentinel-plan` in Claude Code.")
    sys.exit(1)


@main.command()
def status() -> None:
    """Show current project health and backlog."""
    click.echo(f"{NOT_YET}")
    click.echo("  Use `sentinel scan` for a health report.")
    sys.exit(1)


@main.command("config")
def config_cmd() -> None:
    """View or update role configuration."""
    click.echo(f"{NOT_YET}")
    click.echo("  Edit .sentinel/config.toml directly.")
    sys.exit(1)
