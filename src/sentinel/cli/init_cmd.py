"""
sentinel init — guided setup wizard.

Three modes:
  - Interactive (TTY + no --preset): walks through questions
  - Non-interactive (no TTY or --yes): uses 'recommended' preset silently
  - Preset (--preset X): uses the named preset, no questions
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sentinel.config.schema import (
    ProviderName,
    RoleName,
)
from sentinel.providers.router import Router
from sentinel.recommendations import PRESETS, RECOMMENDED, apply_preset

console = Console()


# ---------- Helpers ----------


def _find_sentinel_root() -> Path:
    """Find the sentinel package installation root (for templates)."""
    source_root = Path(__file__).parent.parent.parent.parent
    if (source_root / "templates").exists():
        return source_root
    brew_root = Path("/opt/homebrew/opt/sentinel/libexec")
    if brew_root.exists():
        return brew_root
    return source_root


def _copy_tree(src: Path, dst: Path) -> list[str]:
    """Copy a directory tree, skipping existing files."""
    created = []
    for src_file in src.rglob("*"):
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(src)
        dst_file = dst / rel
        if dst_file.exists():
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        created.append(str(rel))
    return created


def _detect_project_type(project: Path) -> str:
    if (project / "Package.swift").exists() or list(project.glob("*.xcodeproj")):
        return "swift"
    if (project / "Cargo.toml").exists():
        return "rust"
    if (project / "go.mod").exists():
        return "go"
    python_markers = (
        "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile",
    )
    if any((project / marker).exists() for marker in python_markers):
        return "python"
    if (project / "package.json").exists():
        tsconfig = (project / "tsconfig.json").exists()
        return "typescript" if tsconfig else "javascript"
    if list(project.glob("*.cabal")) or (project / "stack.yaml").exists():
        return "haskell"
    return "generic"


def _write_goals_template(project: Path, project_type: str) -> Path:
    goals_path = project / ".sentinel" / "goals.md"
    if goals_path.exists():
        return goals_path

    template = f"""# Project Goals — {project.name}

*Sentinel reads this file during scans to generate project-specific lenses. \
Keep it short (under 100 lines).*

## What is this project?

<!-- One paragraph: what it does, who it's for, why it exists -->

## Current stage

<!-- One of: prototype, pre-launch, v1, growing, mature, maintenance -->

## What matters most right now?

<!-- 2-5 bullet points on the priorities sentinel should weight highest -->

-
-
-

## Constraints

<!-- Tech debt we're living with, migrations in flight, decisions already made -->

## Explicit non-goals

<!-- Things sentinel should NOT recommend — we've considered and rejected these -->

---

*Detected project type: {project_type}. Edit freely — sentinel re-reads on every scan.*
"""
    goals_path.parent.mkdir(parents=True, exist_ok=True)
    goals_path.write_text(template)
    return goals_path


# ---------- Interactive Questions ----------


def _ask_numbered(
    prompt: str, options: list[tuple[str, str]], default: int = 1,
) -> int:
    """Prompt user with a numbered menu. Returns the selected index (1-based).

    options: list of (value, description) tuples — display order matters.
    Returns the 1-based index of the chosen option.
    """
    console.print(f"\n[bold]{prompt}[/bold]")
    for i, (_value, desc) in enumerate(options, 1):
        marker = "[dim]→[/dim] " if i == default else "    "
        console.print(f"  {marker}[{i}] {desc}")

    while True:
        choice = click.prompt(
            f"  Your choice [{default}]", default=str(default), show_default=False,
        ).strip()
        if not choice:
            return default
        try:
            n = int(choice)
            if 1 <= n <= len(options):
                return n
        except ValueError:
            pass
        console.print(f"  [red]Please enter 1-{len(options)}[/red]")


def _interactive_questions(
    available: set[ProviderName], ollama_models: list[str],
) -> tuple[dict[RoleName, tuple[ProviderName, str]], float]:
    """Walk the user through the setup questions. Returns (role_assignments, daily_budget_usd)."""
    # Question 1: Goal — informs which preset we pick
    goal_options = [
        ("recommended", "Keep it moving — balanced refinement + proposals"),
        ("cheap", "Keep it polished — refinement focus, tight budget"),
        ("power", "Build things out — accept expansion, use best models"),
    ]
    goal_idx = _ask_numbered(
        "What do you want sentinel to focus on for this project?",
        goal_options, default=1,
    )
    preset = goal_options[goal_idx - 1][0]

    role_assignments = apply_preset(preset, available, ollama_models)

    # Question 2: Budget
    budget_options = [
        ("15", "$15/day (recommended — work + occasional expansion)"),
        ("5", "$5/day (light use, mostly scans)"),
        ("50", "$50/day (heavy use)"),
        ("1000", "No practical cap ($1000/day)"),
    ]
    budget_idx = _ask_numbered(
        "Daily budget cap?", budget_options, default=1,
    )
    daily_budget = float(budget_options[budget_idx - 1][0])

    return role_assignments, daily_budget


# ---------- Main flow ----------


def run_init(
    project_path: str | None = None,
    auto_yes: bool = False,
    preset: str | None = None,
) -> None:
    """Run the setup wizard — interactive if TTY, else use preset."""
    project = Path(project_path or os.getcwd()).resolve()
    console.print(f"\n[bold]Sentinel Setup[/bold] — {project.name}\n")

    # Detect project type
    project_type = _detect_project_type(project)

    # Detect providers
    console.print("[bold]Detecting providers...[/bold]\n")
    statuses = Router.detect_all()
    _render_provider_table(statuses)

    available_providers = [
        n for n, s in statuses.items() if s.installed and s.authenticated
    ]
    if not available_providers:
        _show_install_hints(statuses)
        return

    provider_map = {
        "claude": ProviderName.CLAUDE,
        "codex": ProviderName.OPENAI,
        "gemini": ProviderName.GEMINI,
        "ollama": ProviderName.LOCAL,
    }
    available_set = {
        provider_map[p] for p in available_providers if p in provider_map
    }
    ollama_models = (
        statuses["ollama"].models if statuses["ollama"].installed else []
    )

    # Decide role assignments + budget
    is_interactive = sys.stdin.isatty() and not auto_yes and preset is None

    if preset:
        if preset not in PRESETS:
            console.print(
                f"[red]Unknown preset '{preset}'. "
                f"Options: {', '.join(PRESETS.keys())}[/red]"
            )
            return
        role_assignments = apply_preset(preset, available_set, ollama_models)
        daily_budget = 15.0
        console.print(f"Using preset: [bold]{preset}[/bold]\n")
    elif is_interactive:
        role_assignments, daily_budget = _interactive_questions(
            available_set, ollama_models,
        )
    else:
        # Non-interactive + no preset → default to recommended silently
        role_assignments = apply_preset(
            "recommended", available_set, ollama_models,
        )
        daily_budget = 15.0

    # Show the final role assignments
    console.print()
    _render_role_assignments(role_assignments)
    console.print()

    # Write files
    _write_config(project, project_type, role_assignments, daily_budget)
    _write_goals_template(project, project_type)
    _install_claude_templates(project)

    # Done
    console.print("\n[bold green]Done![/bold green]\n")
    console.print("  [bold]Next steps:[/bold]")
    console.print(
        "    [dim]1.[/dim] Fill in [cyan].sentinel/goals.md[/cyan] "
        "[dim](describe your project — sharpens lens generation)[/dim]"
    )
    console.print(
        "    [dim]2.[/dim] Run [cyan]sentinel work[/cyan] "
        "[dim](scans, plans, executes refinements; proposes expansions)[/dim]"
    )
    console.print(
        "    [dim]3.[/dim] For continuous mode: [cyan]sentinel work --every 10m[/cyan]"
    )
    console.print()


# ---------- Rendering helpers ----------


def _render_provider_table(statuses: dict) -> None:
    table = Table(show_header=False, padding=(0, 2))
    table.add_column("status", width=3)
    table.add_column("name", width=10)
    table.add_column("detail")

    for name, status in statuses.items():
        if status.installed and status.authenticated:
            models_str = ", ".join(status.models[:3]) if status.models else "ready"
            table.add_row("[green]✓[/green]", name, f"[green]{models_str}[/green]")
        elif status.installed:
            table.add_row(
                "[yellow]![/yellow]", name,
                "[yellow]installed but not ready[/yellow]",
            )
            console.print(f"    [dim]{status.auth_hint}[/dim]")
        else:
            table.add_row("[red]✗[/red]", name, "[red]not found[/red]")

    console.print(table)


def _show_install_hints(statuses: dict) -> None:
    console.print()
    console.print(
        Panel(
            "[yellow]No providers available.[/yellow]\n\n"
            "Sentinel needs at least one LLM provider CLI installed. Options:\n\n"
            + "\n".join(
                f"  • {name}: [cyan]{s.install_hint}[/cyan]"
                for name, s in statuses.items() if not s.installed
            ),
            title="[bold red]Setup Incomplete[/bold red]",
            border_style="yellow",
        )
    )
    console.print()


def _render_role_assignments(
    role_assignments: dict[RoleName, tuple[ProviderName, str]],
) -> None:
    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Role", width=12)
    table.add_column("Provider", width=10)
    table.add_column("Model", width=25)
    table.add_column("Why")

    for role in RoleName:
        provider, model = role_assignments[role]
        rec = RECOMMENDED[role]
        why = ""
        if provider != rec.provider:
            why = f"[dim](fallback from {rec.provider.value})[/dim]"
        table.add_row(role.value, provider.value, model, why)

    console.print("[bold]Role assignments:[/bold]")
    console.print(table)


def _write_config(
    project: Path,
    project_type: str,
    role_assignments: dict[RoleName, tuple[ProviderName, str]],
    daily_budget: float,
) -> None:
    sentinel_dir = project / ".sentinel"
    sentinel_dir.mkdir(exist_ok=True)
    config_path = sentinel_dir / "config.toml"

    if config_path.exists():
        console.print(
            "  [yellow]![/yellow] .sentinel/config.toml already exists — "
            "skipping"
        )
        return

    import tomli_w

    warn_at = max(1.0, daily_budget * 0.7)
    config_dict = {
        "project": {
            "name": project.name,
            "path": str(project),
            "type": project_type,
        },
        "roles": {
            role.value: {"provider": prov.value, "model": model}
            for role, (prov, model) in role_assignments.items()
        },
        "budget": {
            "daily_limit_usd": daily_budget,
            "warn_at_usd": round(warn_at, 2),
        },
        "scan": {"max_lenses": 10, "evaluate_per_lens": True},
    }
    config_path.write_bytes(tomli_w.dumps(config_dict).encode())
    console.print("  [green]✓[/green] Created .sentinel/config.toml")


def _install_claude_templates(project: Path) -> None:
    sentinel_root = _find_sentinel_root()
    templates_src = sentinel_root / "templates" / ".claude"
    claude_dst = project / ".claude"

    if not templates_src.exists():
        return

    created = _copy_tree(templates_src, claude_dst)
    if created:
        console.print(
            f"  [green]✓[/green] Installed {len(created)} Claude Code files to .claude/"
        )
