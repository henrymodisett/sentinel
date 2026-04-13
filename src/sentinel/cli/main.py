"""
Sentinel CLI — the command-line interface for the meta-agent.
"""

from __future__ import annotations

import asyncio

import click

from sentinel import __version__


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
def scan() -> None:
    """Assess current project state through lenses."""
    from sentinel.cli.scan_cmd import run_scan

    asyncio.run(run_scan())


@main.command()
def providers() -> None:
    """Show provider health and capabilities."""
    from sentinel.cli.providers_cmd import run_providers

    run_providers()


@main.command()
def cycle() -> None:
    """Run one full loop cycle: assess -> research -> plan -> execute -> review."""
    click.echo("sentinel cycle — not yet implemented")


@main.command()
def watch() -> None:
    """Continuous mode — run the loop on a schedule."""
    click.echo("sentinel watch — not yet implemented")


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
    click.echo(f"sentinel research — not yet implemented (topic: {topic}, mode: {mode})")


@main.command()
def plan() -> None:
    """Run monitor + researcher + planner to generate a backlog."""
    click.echo("sentinel plan — not yet implemented")


@main.command()
def status() -> None:
    """Show current project health and backlog."""
    click.echo("sentinel status — not yet implemented")


@main.command("config")
def config_cmd() -> None:
    """View or update role configuration."""
    click.echo("sentinel config — not yet implemented")
