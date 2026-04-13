"""
sentinel scan — assess the project through lenses.

Gathers project state (git, tests, lint), then asks the monitor
provider to evaluate the codebase through each active lens.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from sentinel.config.schema import SentinelConfig
from sentinel.providers.router import Router

console = Console()


def _load_config(project_path: Path) -> SentinelConfig | None:
    """Load .sentinel/config.toml."""
    config_file = project_path / ".sentinel" / "config.toml"
    if not config_file.exists():
        console.print("[red]No .sentinel/config.toml found. Run `sentinel init` first.[/red]")
        return None

    import tomllib

    data = tomllib.loads(config_file.read_text())
    return SentinelConfig(**data)


def _gather_state(project_path: Path) -> dict:
    """Gather project state from git, tests, lint."""
    state: dict = {"path": str(project_path), "name": project_path.name}

    # Git status
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True,
            cwd=project_path, timeout=10,
        )
        lines = result.stdout.strip().splitlines()
        state["uncommitted_files"] = len(lines) if lines else 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        state["uncommitted_files"] = -1

    # Current branch
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True,
            cwd=project_path, timeout=5,
        )
        state["branch"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        state["branch"] = "unknown"

    # Recent commits
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10"], capture_output=True, text=True,
            cwd=project_path, timeout=10,
        )
        state["recent_commits"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        state["recent_commits"] = ""

    # File tree (top level + src)
    try:
        result = subprocess.run(
            ["find", ".", "-maxdepth", "3", "-type", "f",
             "-not", "-path", "./.git/*",
             "-not", "-path", "./.venv/*",
             "-not", "-path", "./node_modules/*",
             "-not", "-path", "./.pytest_cache/*"],
            capture_output=True, text=True, cwd=project_path, timeout=10,
        )
        state["file_tree"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        state["file_tree"] = ""

    # CLAUDE.md content
    claude_md = project_path / "CLAUDE.md"
    if claude_md.exists():
        state["claude_md"] = claude_md.read_text()[:3000]  # first 3k chars
    else:
        state["claude_md"] = "(no CLAUDE.md)"

    # README content
    readme = project_path / "README.md"
    if readme.exists():
        state["readme"] = readme.read_text()[:2000]
    else:
        state["readme"] = "(no README.md)"

    # Test results
    toolkit_config = project_path / ".toolkit-config"
    test_cmd = None
    if toolkit_config.exists():
        for line in toolkit_config.read_text().splitlines():
            if line.startswith("test_command=") and line.split("=", 1)[1].strip():
                test_cmd = line.split("=", 1)[1].strip()
                break

    if test_cmd:
        try:
            result = subprocess.run(
                test_cmd.split(), capture_output=True, text=True,
                cwd=project_path, timeout=120,
            )
            state["test_output"] = result.stdout[-2000:] + result.stderr[-1000:]
            state["tests_passed"] = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            state["test_output"] = "(tests timed out)"
            state["tests_passed"] = False
    else:
        state["test_output"] = "(no test command configured)"
        state["tests_passed"] = None

    # Lint results
    lint_cmd = None
    if toolkit_config.exists():
        for line in toolkit_config.read_text().splitlines():
            if line.startswith("lint_command=") and line.split("=", 1)[1].strip():
                lint_cmd = line.split("=", 1)[1].strip()
                break

    if lint_cmd:
        try:
            result = subprocess.run(
                lint_cmd.split(), capture_output=True, text=True,
                cwd=project_path, timeout=60,
            )
            state["lint_output"] = result.stdout[-1000:] + result.stderr[-500:]
            state["lint_clean"] = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            state["lint_output"] = "(lint timed out)"
            state["lint_clean"] = False
    else:
        state["lint_output"] = "(no lint command configured)"
        state["lint_clean"] = None

    return state


def _load_lenses(project_path: Path, enabled: list[str]) -> dict[str, str]:
    """Load lens content from the project's lenses/ directory."""
    lenses = {}
    for lens_name in enabled:
        # Check universal first, then conditional
        for subdir in ["universal", "conditional"]:
            lens_file = project_path / "lenses" / subdir / f"{lens_name}.md"
            if lens_file.exists():
                lenses[lens_name] = lens_file.read_text()
                break
    return lenses


def _build_scan_prompt(state: dict, lenses: dict[str, str]) -> str:
    """Build the prompt for the monitor LLM."""
    lens_section = ""
    for _name, content in lenses.items():
        lens_section += f"\n---\n{content}\n"

    return f"""You are Sentinel's Monitor. Assess this project's health \
through the analytical lenses provided.

## Project State

**Project**: {state['name']}
**Branch**: {state['branch']}
**Uncommitted changes**: {state['uncommitted_files']}

### Recent commits
```
{state['recent_commits']}
```

### File structure
```
{state['file_tree'][:2000]}
```

### CLAUDE.md (project context)
```
{state['claude_md'][:2000]}
```

### Test results
Tests passed: {state['tests_passed']}
```
{state['test_output'][:1500]}
```

### Lint results
Lint clean: {state['lint_clean']}
```
{state['lint_output'][:500]}
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

    # Gather state
    with console.status("[bold blue]Gathering project state..."):
        state = _gather_state(project)

    # Show quick summary
    console.print(f"  Branch: {state['branch']}")
    console.print(f"  Uncommitted: {state['uncommitted_files']} files")
    if state["tests_passed"]:
        test_str = "[green]passing[/green]"
    elif state["tests_passed"] is False:
        test_str = "[red]failing[/red]"
    else:
        test_str = "[dim]no test command[/dim]"
    console.print(f"  Tests: {test_str}")

    if state["lint_clean"]:
        lint_str = "[green]clean[/green]"
    elif state["lint_clean"] is False:
        lint_str = "[red]issues[/red]"
    else:
        lint_str = "[dim]no lint command[/dim]"
    console.print(f"  Lint: {lint_str}")
    console.print()

    # Load lenses
    lenses = _load_lenses(project, config.lenses.enabled)
    console.print(f"  Active lenses: {', '.join(lenses.keys())}")
    if not lenses:
        console.print("[yellow]  No lenses found. Run `sentinel init` to install lenses.[/yellow]")
        return

    # Build prompt and call monitor provider
    prompt = _build_scan_prompt(state, lenses)
    router = Router(config)
    monitor_provider = router.get_provider("monitor")

    console.print(f"  Monitor provider: [bold]{monitor_provider.name}[/bold]")
    console.print()

    start = time.time()
    n_lenses = len(lenses)
    prov_name = monitor_provider.name
    with console.status(f"[bold blue]Scanning through {n_lenses} lenses via {prov_name}..."):
        response = await monitor_provider.chat(prompt)

    elapsed = time.time() - start

    # Display results
    console.print(Panel(response.content, title="[bold]Scan Results[/bold]", border_style="blue"))

    # Show cost/usage
    console.print()
    console.print(f"  [dim]Provider: {response.provider} | Model: {response.model}[/dim]")
    console.print(f"  [dim]Tokens: {response.input_tokens} in / {response.output_tokens} out[/dim]")
    if response.cost_usd > 0:
        console.print(f"  [dim]Cost: ${response.cost_usd:.4f}[/dim]")
    console.print(f"  [dim]Time: {elapsed:.1f}s[/dim]")
    console.print()
