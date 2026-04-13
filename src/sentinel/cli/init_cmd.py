"""
sentinel init — detect providers, assign roles, install files.

The zero-to-running setup wizard. Detects what CLIs you have,
recommends role assignments, creates config, installs Claude Code
agents/skills/loop.md and lenses into the project.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table

from sentinel.config.schema import (
    ROLE_DEFAULTS,
    LensConfig,
    ProviderName,
    RoleName,
)
from sentinel.providers.router import Router

console = Console()

# Lens references to append to CLAUDE.md
LENS_REFERENCES = """
## Sentinel Lenses

@lenses/universal/architecture.md
@lenses/universal/code-quality.md
@lenses/universal/security.md
@lenses/universal/testing.md
@lenses/universal/reliability.md
@lenses/universal/dependencies.md
@lenses/universal/technical-debt.md
@lenses/universal/developer-experience.md
"""


def _find_sentinel_root() -> Path:
    """Find the sentinel package installation root (for templates/lenses)."""
    # When installed via pip/uv, resources are alongside the package
    # When running from source, they're at the repo root
    source_root = Path(__file__).parent.parent.parent.parent
    if (source_root / "lenses").exists():
        return source_root
    # Fallback: check brew libexec
    brew_root = Path("/opt/homebrew/opt/sentinel/libexec")
    if brew_root.exists():
        return brew_root
    return source_root


def _copy_tree(src: Path, dst: Path) -> list[str]:
    """Copy a directory tree, skipping existing files. Returns list of created files."""
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


def run_init(project_path: str | None = None) -> None:
    """Run the interactive init wizard."""
    project = Path(project_path or os.getcwd()).resolve()
    console.print(f"\n[bold]Sentinel Setup[/bold] — {project.name}\n")

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
            table.add_row("[yellow]![/yellow]", name, "[yellow]installed but not ready[/yellow]")
            console.print(f"    [dim]{status.auth_hint}[/dim]")
        else:
            table.add_row("[red]✗[/red]", name, "[red]not found[/red]")

    console.print(table)

    # Show install hints for missing providers
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

    # Step 2: Assign roles based on available providers
    console.print("[bold]Assigning roles...[/bold]\n")

    # Map provider detection names to ProviderName enum
    provider_map = {
        "claude": ProviderName.CLAUDE,
        "codex": ProviderName.OPENAI,
        "gemini": ProviderName.GEMINI,
        "ollama": ProviderName.LOCAL,
    }
    available_set = {provider_map[p] for p in available_providers if p in provider_map}

    # Build role assignments — use defaults where available, fallback otherwise
    role_assignments: dict[RoleName, tuple[ProviderName, str]] = {}
    for role in RoleName:
        default = ROLE_DEFAULTS[role]
        if default.provider in available_set:
            role_assignments[role] = (default.provider, default.model)
        else:
            # Fallback: use the first available provider
            fallback = next(iter(available_set))
            # Pick a sensible model for the fallback
            fallback_models = {
                ProviderName.CLAUDE: "claude-sonnet-4-6",
                ProviderName.OPENAI: "gpt-5.4",
                ProviderName.GEMINI: "gemini-2.5-flash",
                ProviderName.LOCAL: "qwen2.5-coder:14b",
            }
            role_assignments[role] = (fallback, fallback_models.get(fallback, "default"))

    role_table = Table(show_header=True, padding=(0, 2))
    role_table.add_column("Role", width=12)
    role_table.add_column("Provider", width=10)
    role_table.add_column("Model", width=25)
    role_table.add_column("Note")

    for role in RoleName:
        provider, model = role_assignments[role]
        default = ROLE_DEFAULTS[role]
        note = ""
        if default.provider not in available_set:
            note = f"[yellow](default was {default.provider}, not available)[/yellow]"
        role_table.add_row(role.value, provider.value, model, note)

    console.print(role_table)
    console.print()

    # Step 3: Create .sentinel/config.toml
    sentinel_dir = project / ".sentinel"
    sentinel_dir.mkdir(exist_ok=True)
    config_path = sentinel_dir / "config.toml"

    if config_path.exists():
        console.print("[yellow]  .sentinel/config.toml already exists — skipping[/yellow]")
    else:
        import tomli_w

        config_dict = {
            "project": {"name": project.name, "path": str(project)},
            "roles": {
                role.value: {"provider": prov.value, "model": model}
                for role, (prov, model) in role_assignments.items()
            },
            "budget": {"daily_limit_usd": 15.0, "warn_at_usd": 10.0},
            "lenses": {"enabled": LensConfig().enabled},
        }
        config_path.write_bytes(tomli_w.dumps(config_dict).encode())
        console.print("  [green]✓[/green] Created .sentinel/config.toml")

    # Step 4: Install lenses
    sentinel_root = _find_sentinel_root()
    lenses_src = sentinel_root / "lenses"
    lenses_dst = project / "lenses"

    if lenses_src.exists():
        created = _copy_tree(lenses_src, lenses_dst)
        if created:
            console.print(f"  [green]✓[/green] Installed {len(created)} lens files to lenses/")
        else:
            console.print("  [dim]  lenses/ already up to date[/dim]")
    else:
        console.print(f"  [yellow]  lenses/ source not found at {lenses_src}[/yellow]")

    # Step 5: Install .claude/ templates (agents, skills, loop.md)
    templates_src = sentinel_root / "templates" / ".claude"
    claude_dst = project / ".claude"

    if templates_src.exists():
        created = _copy_tree(templates_src, claude_dst)
        if created:
            n = len(created)
            console.print(f"  [green]✓[/green] Installed {n} Claude Code files to .claude/")
            for f in created:
                console.print(f"      + .claude/{f}")
        else:
            console.print("  [dim]  .claude/ templates already up to date[/dim]")
    else:
        console.print(f"  [yellow]  templates/.claude/ not found at {templates_src}[/yellow]")

    # Step 6: Append lens references to CLAUDE.md (if not already there)
    claude_md = project / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text()
        if "@lenses/universal/architecture.md" not in content:
            claude_md.write_text(content.rstrip() + "\n" + LENS_REFERENCES)
            console.print("  [green]✓[/green] Appended lens references to CLAUDE.md")
        else:
            console.print("  [dim]  CLAUDE.md already has lens references[/dim]")
    else:
        console.print("  [dim]  No CLAUDE.md found (create one or run toolkit init)[/dim]")

    # Done
    console.print("\n[bold green]Done![/bold green]\n")
    console.print("  Next steps:")
    console.print("    sentinel scan       — assess project health")
    console.print("    sentinel providers   — check provider status")
    console.print("    /loop               — continuous mode in Claude Code")
    console.print()
