"""
sentinel providers — show provider health and capabilities.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from sentinel.providers.router import Router

console = Console()


def run_providers() -> None:
    """Show all provider statuses."""
    console.print("\n[bold]Provider Status[/bold]\n")

    statuses = Router.detect_all()

    table = Table(padding=(0, 2))
    table.add_column("Provider", width=10)
    table.add_column("Installed", width=10)
    table.add_column("Ready", width=8)
    table.add_column("Models")
    table.add_column("Install / Auth")

    for name, status in statuses.items():
        installed = "[green]yes[/green]" if status.installed else "[red]no[/red]"
        ready = "[green]yes[/green]" if status.authenticated else "[yellow]no[/yellow]"
        models = ", ".join(status.models[:3]) if status.models else "-"

        hint = ""
        if not status.installed:
            hint = status.install_hint
        elif not status.authenticated:
            hint = status.auth_hint

        table.add_row(name, installed, ready, models, hint)

    console.print(table)
    console.print()
