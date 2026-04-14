"""
sentinel init — detect providers, assign roles, install files.

The zero-to-running setup wizard. Detects what CLIs you have,
recommends role assignments, creates config, installs Claude Code
agents/skills/loop.md, and creates a goals.md template for the user
to describe their project.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from sentinel.config.schema import (
    ROLE_DEFAULTS,
    ProviderName,
    RoleName,
)
from sentinel.providers.router import Router

console = Console()


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
    """Detect the project type for better default role assignment."""
    if (project / "Package.swift").exists() or list(project.glob("*.xcodeproj")):
        return "swift"
    if (project / "Cargo.toml").exists():
        return "rust"
    if (project / "go.mod").exists():
        return "go"
    if (project / "pyproject.toml").exists():
        return "python"
    if (project / "package.json").exists():
        tsconfig = (project / "tsconfig.json").exists()
        return "typescript" if tsconfig else "javascript"
    if list(project.glob("*.cabal")) or (project / "stack.yaml").exists():
        return "haskell"
    return "generic"


def _pick_monitor_provider(
    available: set[ProviderName],
) -> tuple[ProviderName, str]:
    """Pick the best monitor provider — prefer cheap/fast over expensive.

    Based on dogfood findings: Gemini Flash is 6x faster than Claude Sonnet
    for the monitor role with comparable quality. Prefer it when available.
    """
    # Preference order: local > gemini-flash > gpt-5.4-mini > claude-sonnet
    if ProviderName.LOCAL in available:
        return ProviderName.LOCAL, "qwen2.5-coder:14b"
    if ProviderName.GEMINI in available:
        return ProviderName.GEMINI, "gemini-2.5-flash"
    if ProviderName.OPENAI in available:
        return ProviderName.OPENAI, "gpt-5.4-mini"
    return ProviderName.CLAUDE, "claude-sonnet-4-6"


def _write_goals_template(project: Path, project_type: str) -> Path:
    """Write .sentinel/goals.md template if it doesn't exist."""
    goals_path = project / ".sentinel" / "goals.md"
    if goals_path.exists():
        return goals_path

    template = f"""# Project Goals — {project.name}

*Sentinel reads this file during scans to generate project-specific lenses \
and inform its assessments. Keep it short (under 100 lines).*

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

<!-- Things sentinel should know: tech debt we're living with, migrations in \
flight, things we've decided NOT to do -->

## Explicit non-goals

<!-- Things sentinel should NOT recommend — we've considered and rejected \
these. Example: "Don't suggest adding tests for deprecated modules" -->

---

*Detected project type: {project_type}. Edit freely — sentinel re-reads this on every scan.*
"""
    goals_path.parent.mkdir(parents=True, exist_ok=True)
    goals_path.write_text(template)
    return goals_path


def run_init(project_path: str | None = None, auto_yes: bool = False) -> None:
    """Run the interactive init wizard."""
    project = Path(project_path or os.getcwd()).resolve()
    console.print(f"\n[bold]Sentinel Setup[/bold] — {project.name}\n")

    # Detect project type
    project_type = _detect_project_type(project)
    console.print(f"  Detected project type: [bold]{project_type}[/bold]\n")

    # Step 1: Detect providers
    console.print("[bold]Checking for providers...[/bold]\n")
    statuses = Router.detect_all()

    table = Table(show_header=False, padding=(0, 2))
    table.add_column("status", width=3)
    table.add_column("name", width=10)
    table.add_column("detail")

    available_providers: list[str] = []
    for name, status in statuses.items():
        if status.installed and status.authenticated:
            models_str = ", ".join(status.models[:3]) if status.models else "ready"
            table.add_row("[green]✓[/green]", name, f"[green]{models_str}[/green]")
            available_providers.append(name)
        elif status.installed:
            table.add_row(
                "[yellow]![/yellow]", name,
                "[yellow]installed but not ready[/yellow]",
            )
            console.print(f"    [dim]{status.auth_hint}[/dim]")
        else:
            table.add_row("[red]✗[/red]", name, "[red]not found[/red]")

    console.print(table)

    missing = {k: v for k, v in statuses.items() if not v.installed}
    if missing:
        console.print("\n[dim]  Missing providers:[/dim]")
        for name, status in missing.items():
            console.print(f"    [dim]{name}: {status.install_hint}[/dim]")

    if not available_providers:
        console.print("\n[red]No providers available. Install at least one:[/red]")
        for _name, status in statuses.items():
            console.print(f"  {status.install_hint}")
        return

    console.print()

    # Step 2: Assign roles
    console.print("[bold]Assigning roles...[/bold]\n")

    provider_map = {
        "claude": ProviderName.CLAUDE,
        "codex": ProviderName.OPENAI,
        "gemini": ProviderName.GEMINI,
        "ollama": ProviderName.LOCAL,
    }
    available_set = {provider_map[p] for p in available_providers if p in provider_map}

    # Smart defaults based on provider availability
    role_assignments: dict[RoleName, tuple[ProviderName, str]] = {}

    for role in RoleName:
        default = ROLE_DEFAULTS[role]
        # Monitor role — use our smart picker (prefers cheap/fast)
        if role == RoleName.MONITOR:
            role_assignments[role] = _pick_monitor_provider(available_set)
            continue

        if default.provider in available_set:
            role_assignments[role] = (default.provider, default.model)
        else:
            fallback_models = {
                ProviderName.CLAUDE: "claude-sonnet-4-6",
                ProviderName.OPENAI: "gpt-5.4",
                ProviderName.GEMINI: "gemini-2.5-pro",
                ProviderName.LOCAL: "qwen2.5-coder:14b",
            }
            fallback = next(iter(available_set))
            role_assignments[role] = (
                fallback, fallback_models.get(fallback, "default"),
            )

    role_table = Table(show_header=True, padding=(0, 2))
    role_table.add_column("Role", width=12)
    role_table.add_column("Provider", width=10)
    role_table.add_column("Model", width=25)
    role_table.add_column("Note")

    for role in RoleName:
        provider, model = role_assignments[role]
        default = ROLE_DEFAULTS[role]
        note = ""
        if default.provider not in available_set and role != RoleName.MONITOR:
            note = f"[yellow](default was {default.provider}, fallback)[/yellow]"
        elif role == RoleName.MONITOR and provider != default.provider:
            note = "[dim](optimized for speed/cost)[/dim]"
        role_table.add_row(role.value, provider.value, model, note)

    console.print(role_table)
    console.print()

    # Step 3: Preview what will be created
    sentinel_root = _find_sentinel_root()
    templates_src = sentinel_root / "templates" / ".claude"
    claude_dst = project / ".claude"

    config_path = project / ".sentinel" / "config.toml"
    goals_path = project / ".sentinel" / "goals.md"
    config_exists = config_path.exists()
    goals_exists = goals_path.exists()

    preview_items = []
    if not config_exists:
        preview_items.append(".sentinel/config.toml")
    if not goals_exists:
        preview_items.append(".sentinel/goals.md (template)")

    claude_files_to_create = []
    if templates_src.exists():
        for src_file in templates_src.rglob("*"):
            if src_file.is_dir():
                continue
            rel = src_file.relative_to(templates_src)
            dst_file = claude_dst / rel
            if not dst_file.exists():
                claude_files_to_create.append(f".claude/{rel}")

    preview_items.extend(claude_files_to_create)

    if preview_items:
        console.print("[bold]Will create:[/bold]")
        for item in preview_items:
            console.print(f"  [green]+[/green] {item}")
        console.print()

        # Auto-proceed if running non-interactively or auto_yes passed
        import sys as _sys
        is_tty = _sys.stdin.isatty()
        if auto_yes or not is_tty:
            if not is_tty:
                console.print("  [dim](non-interactive — proceeding)[/dim]")
        elif not click.confirm("  Proceed?", default=True):
            console.print("[yellow]Cancelled.[/yellow]")
            return
        console.print()
    else:
        console.print("[dim]  Nothing to install — sentinel is already set up here.[/dim]\n")

    # Step 4: Create .sentinel/config.toml
    sentinel_dir = project / ".sentinel"
    sentinel_dir.mkdir(exist_ok=True)

    if config_exists:
        console.print("[yellow]  .sentinel/config.toml already exists — skipping[/yellow]")
    else:
        import tomli_w

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
            "budget": {"daily_limit_usd": 15.0, "warn_at_usd": 10.0},
            "scan": {"max_lenses": 10, "evaluate_per_lens": True},
        }
        config_path.write_bytes(tomli_w.dumps(config_dict).encode())
        console.print("  [green]✓[/green] Created .sentinel/config.toml")

    # Step 5: Create goals.md template
    if goals_exists:
        console.print("[dim]  .sentinel/goals.md already exists — skipping[/dim]")
    else:
        _write_goals_template(project, project_type)
        console.print("  [green]✓[/green] Created .sentinel/goals.md (fill in for better scans)")

    # Step 6: Install .claude/ templates
    if templates_src.exists():
        created = _copy_tree(templates_src, claude_dst)
        if created:
            n = len(created)
            console.print(f"  [green]✓[/green] Installed {n} Claude Code files to .claude/")
        else:
            console.print("  [dim]  .claude/ templates already up to date[/dim]")
    else:
        console.print(f"  [yellow]  templates/.claude/ not found at {templates_src}[/yellow]")

    console.print(
        "  [dim]  Lenses are generated dynamically per scan, not pre-installed[/dim]"
    )

    # Done
    console.print("\n[bold green]Done![/bold green]\n")
    console.print("  [bold]Next steps:[/bold]")
    console.print(
        "    [dim]1.[/dim] Fill in [cyan].sentinel/goals.md[/cyan] "
        "[dim](project summary, priorities, constraints)[/dim]"
    )
    console.print(
        "    [dim]2.[/dim] Run [cyan]sentinel scan[/cyan] "
        "[dim](generates custom lenses, produces health report)[/dim]"
    )
    console.print(
        "    [dim]3.[/dim] Run [cyan]sentinel plan[/cyan] "
        "[dim](turns scan into a prioritized backlog)[/dim]"
    )
    console.print()
