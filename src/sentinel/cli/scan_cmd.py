"""
sentinel scan — assess the project through lenses.

Gathers project state via the shared state module, then asks the
monitor provider to evaluate the codebase through each active lens.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from sentinel.config.schema import SentinelConfig
from sentinel.providers.router import Router
from sentinel.state import ProjectState, gather_state

console = Console()


def _load_config(project_path: Path) -> SentinelConfig | None:
    """Load .sentinel/config.toml."""
    config_file = project_path / ".sentinel" / "config.toml"
    if not config_file.exists():
        console.print(
            "[red]No .sentinel/config.toml found. Run `sentinel init` first.[/red]"
        )
        return None

    import tomllib

    data = tomllib.loads(config_file.read_text())
    return SentinelConfig(**data)


def _load_lenses(project_path: Path, enabled: list[str]) -> dict[str, str]:
    """Load lens content from the project's lenses/ directory."""
    lenses = {}
    for lens_name in enabled:
        for subdir in ["universal", "conditional"]:
            lens_file = project_path / "lenses" / subdir / f"{lens_name}.md"
            if lens_file.exists():
                lenses[lens_name] = lens_file.read_text()
                break
    return lenses


def _build_scan_prompt(state: ProjectState, lenses: dict[str, str]) -> str:
    """Build the prompt for the monitor LLM."""
    lens_section = ""
    for _name, content in lenses.items():
        lens_section += f"\n---\n{content}\n"

    return f"""\
You are Sentinel's Monitor. Assess this project's health \
through the analytical lenses provided.

## Project State

**Project**: {state.name}
**Branch**: {state.branch}
**Uncommitted changes**: {state.uncommitted_files}

### Recent commits
```
{state.recent_commits}
```

### File structure
```
{state.file_tree[:2000]}
```

### CLAUDE.md (project context)
```
{state.claude_md[:2000]}
```

### Test results
Tests passed: {state.tests_passed}
```
{state.test_output[:1500]}
```

### Lint results
Lint clean: {state.lint_clean}
```
{state.lint_output[:500]}
```

## Lenses
{lens_section}

## Your Task

Evaluate this project through EACH active lens. For each lens, provide:
1. A score (0-100)
2. Top issues found (if any)
3. Highlights (things done well)

Then provide:
- **Overall health score** (0-100, weighted average)
- **Top 3 recommended actions** ranked by impact

Be specific. Reference actual files and patterns, not generic advice.
Format your response clearly with markdown headers for each lens.
"""


async def run_scan(project_path: str | None = None) -> None:
    """Run the scan command."""
    project = Path(project_path or os.getcwd()).resolve()
    config = _load_config(project)
    if not config:
        return

    console.print(f"\n[bold]Sentinel Scan[/bold] — {project.name}\n")

    # Gather state via shared module
    with console.status("[bold blue]Gathering project state..."):
        state = gather_state(project)

    # Show quick summary
    console.print(f"  Branch: {state.branch}")
    console.print(f"  Uncommitted: {state.uncommitted_files} files")

    if state.tests_passed:
        test_str = "[green]passing[/green]"
    elif state.tests_passed is False:
        test_str = "[red]failing[/red]"
    else:
        test_str = "[dim]no test command[/dim]"
    console.print(f"  Tests: {test_str}")

    if state.lint_clean:
        lint_str = "[green]clean[/green]"
    elif state.lint_clean is False:
        lint_str = "[red]issues[/red]"
    else:
        lint_str = "[dim]no lint command[/dim]"
    console.print(f"  Lint: {lint_str}")

    if state.errors:
        for err in state.errors:
            console.print(f"  [yellow]Warning: {err}[/yellow]")

    console.print()

    # Load lenses
    lenses = _load_lenses(project, config.lenses.enabled)
    console.print(f"  Active lenses: {', '.join(lenses.keys())}")
    if not lenses:
        console.print(
            "[yellow]  No lenses found. Run `sentinel init` to install.[/yellow]"
        )
        return

    # Build prompt and call monitor provider
    prompt = _build_scan_prompt(state, lenses)
    router = Router(config)
    monitor_provider = router.get_provider("monitor")

    prov_name = monitor_provider.name
    console.print(f"  Monitor provider: [bold]{prov_name}[/bold]")
    console.print()

    start = time.time()
    n_lenses = len(lenses)
    with console.status(
        f"[bold blue]Scanning through {n_lenses} lenses via {prov_name}..."
    ):
        response = await monitor_provider.chat(prompt)

    elapsed = time.time() - start

    # Display results
    console.print(
        Panel(response.content, title="[bold]Scan Results[/bold]", border_style="blue")
    )

    # Show cost/usage
    console.print()
    console.print(f"  [dim]Provider: {response.provider} | Model: {response.model}[/dim]")
    console.print(
        f"  [dim]Tokens: {response.input_tokens} in / {response.output_tokens} out[/dim]"
    )
    if response.cost_usd > 0:
        console.print(f"  [dim]Cost: ${response.cost_usd:.4f}[/dim]")
    console.print(f"  [dim]Time: {elapsed:.1f}s[/dim]")
    console.print()
