"""
sentinel scan — multi-step project assessment with live progress.

Pipeline: explore → generate lenses → evaluate each (parallel) → synthesize.
Or --quick for a free instant state summary.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from sentinel.config.schema import SentinelConfig
from sentinel.providers.router import Router
from sentinel.roles.monitor import Monitor, ScanResult
from sentinel.state import ProjectState, gather_state

console = Console()


def _load_config(project_path: Path) -> SentinelConfig | None:
    config_file = project_path / ".sentinel" / "config.toml"
    if not config_file.exists():
        console.print(
            "[red]No .sentinel/config.toml found. Run `sentinel init` first.[/red]"
        )
        return None
    import tomllib

    data = tomllib.loads(config_file.read_text())
    return SentinelConfig(**data)


def scan_progress_printer():
    """Return an on_progress callback that prints scan events to console.

    Shared across scan, work, and cycle commands for consistent streaming UX.
    """
    def on_progress(event: str, data: dict) -> None:
        if event == "step_start":
            console.print(
                f"[bold cyan]▶[/bold cyan] {data.get('message', data['step'])}"
            )
        elif event == "lens_generated":
            lenses = data["lenses"]
            console.print(
                f"  [green]✓[/green] Generated {len(lenses)} custom lenses:"
            )
            for lens in lenses:
                console.print(
                    f"    [bold]{lens.name}[/bold] — [dim]{lens.description}[/dim]"
                )
            console.print()
        elif event == "lens_start":
            idx = data["index"]
            total = data["total"]
            name = data["lens_name"]
            console.print(
                f"  [dim]({idx}/{total})[/dim] [cyan]evaluating[/cyan] {name}..."
            )
        elif event == "lens_done":
            name = data["lens_name"]
            score = data["score"]
            running = data.get("running_cost_usd", 0.0)
            color = "green" if score >= 75 else "yellow" if score >= 50 else "red"
            cost_str = (
                f" [dim](running: ${running:.4f})[/dim]" if running > 0 else ""
            )
            console.print(
                f"  [green]✓[/green] {name}: "
                f"[{color}]{score}/100[/{color}]{cost_str}"
            )
        elif event == "lens_failed":
            console.print(
                f"  [red]✗[/red] {data['lens_name']}: failed"
            )
    return on_progress


def _print_state_summary(state: ProjectState) -> None:
    console.print(f"  Branch: {state.branch}")
    console.print(f"  Uncommitted: {state.uncommitted_files} files")

    if state.tests_passed:
        console.print("  Tests: [green]passing[/green]")
    elif state.tests_passed is False:
        console.print("  Tests: [red]failing[/red]")
    else:
        console.print("  Tests: [dim]no test command[/dim]")

    if state.lint_clean:
        console.print("  Lint: [green]clean[/green]")
    elif state.lint_clean is False:
        console.print("  Lint: [red]issues[/red]")
    else:
        console.print("  Lint: [dim]no lint command[/dim]")

    if state.errors:
        for err in state.errors:
            console.print(f"  [yellow]Warning: {err}[/yellow]")


def _persist_scan(project_path: Path, result: ScanResult) -> Path:
    """Write scan result to disk.

    Complete scans go to `.sentinel/scans/YYYY-MM-DD-HHMM.md`.

    Partial scans (result.ok is False but lens evaluations ran) go to
    `.sentinel/scans/partial/YYYY-MM-DD-HHMM.md`. Keeping them out of
    the main `scans/` glob means `_find_latest_scan` naturally ignores
    them — so the next `sentinel work` treats the project as having no
    recent scan and rescans, rather than planning from a scan with an
    empty Top Actions section and writing an empty backlog. The partial
    file still exists for operators to inspect; it just doesn't masquerade
    as a successful scan.

    Losing successful lens work on every synthesis failure is exactly the
    silent-failure pattern the engineering principles forbid — but so is
    quietly poisoning the next run with a failed scan.
    """
    scans_dir = project_path / ".sentinel" / "scans"
    if not result.ok:
        scans_dir = scans_dir / "partial"
    scans_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    scan_file = scans_dir / f"{timestamp}.md"

    lines = [
        f"# Sentinel Scan — {timestamp}",
        "",
    ]
    if not result.ok:
        lines += [
            "> ⚠️  **Partial scan — synthesis did not complete.** Lens "
            "evaluations below are still useful, but the top-line summary, "
            "strengths, risks, and recommended actions are missing.",
            "",
            f"> **Reason:** {result.error or 'unknown failure'}",
            "",
        ]
    lines += [
        f"**Overall score:** {result.overall_score}/100",
        f"**Provider:** {result.provider} | **Model:** {result.model}",
        f"**Cost:** ${result.total_cost_usd:.4f} | "
        f"**Tokens:** {result.total_input_tokens} in / {result.total_output_tokens} out",
        "",
        "## Project Understanding",
        "",
        result.project_summary,
        "",
        "## Summary",
        "",
        result.raw_report or "_(synthesis did not complete — see lens evaluations below)_",
        "",
        "## Strengths",
        "",
    ]
    for s in result.strengths:
        lines.append(f"- {s}")
    lines += ["", "## Critical Risks", ""]
    for r in result.critical_risks:
        lines.append(f"- {r}")
    lines += ["", "## Top Actions", ""]
    for i, a in enumerate(result.top_actions, 1):
        lines.append(f"### {i}. {a.get('title', '')}")
        lines.append("")
        kind = a.get("kind", "unknown")
        lines.append(f"**Kind:** {kind}")
        lines.append(f"**Lens:** {a.get('lens', '')}")
        lines.append(f"**Why:** {a.get('why', '')}")
        if a.get("files"):
            lines.append(f"**Files:** {', '.join(a['files'])}")
        lines.append(f"**Impact:** {a.get('impact', '')}")
        lines.append("")

    lines += ["## Lens Evaluations", ""]
    for ev in result.evaluations:
        lines.append(f"### {ev.lens_name} — {ev.score}/100")
        lines.append("")
        if ev.error:
            lines.append(f"*Evaluation failed: {ev.error}*")
        else:
            lines.append(f"**Top finding:** {ev.top_finding}")
            lines.append("")
            lines.append(ev.findings)
            lines.append("")
            lines.append("**Recommended tasks:**")
            for t in ev.recommended_tasks:
                lines.append(f"- {t}")
        lines.append("")

    scan_file.write_text("\n".join(lines))
    return scan_file


async def run_scan(
    project_path: str | None = None, quick: bool = False,
) -> None:
    project = Path(project_path or os.getcwd()).resolve()

    console.print(f"\n[bold]Sentinel Scan[/bold] — {project.name}")
    if quick:
        console.print("[dim]  (quick mode — no LLM call)[/dim]")
    console.print()

    with console.status("[bold blue]Gathering project state..."):
        state = gather_state(project)

    _print_state_summary(state)
    console.print()

    if quick:
        if state.recent_commits:
            console.print("[bold]Recent commits:[/bold]")
            for line in state.recent_commits.splitlines()[:5]:
                console.print(f"  {line}")
            console.print()
        return

    config = _load_config(project)
    if not config:
        return

    # Budget check before starting
    from sentinel.budget import check_budget, record_spend

    budget = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )
    if budget.over_limit:
        console.print(
            f"[red]Daily budget exceeded: ${budget.today_spent_usd:.2f} / "
            f"${budget.daily_limit_usd:.2f}[/red]"
        )
        console.print(
            "[red]Refusing to scan. Edit .sentinel/config.toml to raise the limit.[/red]"
        )
        return
    if budget.warning:
        console.print(
            f"[yellow]  Budget warning: "
            f"${budget.today_spent_usd:.2f} spent today "
            f"(limit: ${budget.daily_limit_usd:.2f})[/yellow]"
        )

    router = Router(config)
    monitor = Monitor(router)
    provider = router.get_provider("monitor")
    console.print(f"  Monitor provider: [bold]{provider.name}[/bold]")
    console.print()

    # Streaming progress callback
    start = time.time()
    result = await monitor.assess(state, on_progress=scan_progress_printer())
    elapsed = time.time() - start

    # Record spend regardless of success/failure (we paid for the tokens)
    if result.total_cost_usd > 0:
        record_spend(
            project, result.total_cost_usd, "scan",
            f"model={result.model}",
        )

    if not result.ok:
        console.print()
        console.print(
            Panel(
                f"[red]Scan pipeline failed:[/red]\n\n{result.error}",
                title="[bold red]Error[/bold red]",
                border_style="red",
            )
        )
        # Persist whatever lens work we managed before the failure. Losing
        # 6/6 successful lens evaluations because synthesis timed out is
        # the exact silent-failure pattern we're meant to prevent.
        if result.evaluations:
            try:
                scan_file = _persist_scan(project, result)
                console.print()
                console.print(
                    f"  [dim]Partial scan saved to: "
                    f"{scan_file.relative_to(project)}[/dim]"
                )
            except (OSError, ValueError) as persist_err:
                console.print(
                    f"  [yellow]Could not persist partial scan: "
                    f"{persist_err}[/yellow]"
                )
        console.print()
        console.print(
            f"  [dim]Tokens: {result.total_input_tokens} in / "
            f"{result.total_output_tokens} out[/dim]"
        )
        if result.total_cost_usd > 0:
            console.print(f"  [dim]Cost: ${result.total_cost_usd:.4f}[/dim]")
        console.print(f"  [dim]Time: {elapsed:.1f}s[/dim]")
        raise click.exceptions.Exit(code=1)

    # Show project understanding
    console.print()
    console.print(
        Panel(
            result.project_summary,
            title="[bold]Project Understanding[/bold]",
            border_style="cyan",
        )
    )

    # Show summary
    console.print()
    n_total = len(result.evaluations)
    n_ok = n_total - result.n_lenses_failed
    score_line = f"[bold]Overall health: {result.overall_score}/100[/bold]"
    if result.n_lenses_failed:
        score_line += (
            f" [yellow](based on {n_ok}/{n_total} lenses — "
            f"{result.n_lenses_failed} failed to evaluate)[/yellow]"
        )
    else:
        score_line += f" [dim](across {n_total} lenses)[/dim]"
    console.print(score_line)
    console.print()
    console.print(result.raw_report)

    if result.strengths:
        console.print()
        console.print("[bold green]Strengths[/bold green]")
        for s in result.strengths:
            console.print(f"  • {s}")

    if result.critical_risks:
        console.print()
        console.print("[bold red]Critical Risks[/bold red]")
        for r in result.critical_risks:
            console.print(f"  • {r}")

    if result.top_actions:
        console.print()
        refine_actions = [a for a in result.top_actions if a.get("kind") == "refine"]
        expand_actions = [a for a in result.top_actions if a.get("kind") == "expand"]

        if refine_actions:
            console.print(
                "[bold green]Refinements[/bold green] "
                "[dim](sentinel can execute autonomously)[/dim]"
            )
            for i, a in enumerate(refine_actions, 1):
                console.print(f"  [bold]{i}. {a.get('title', '')}[/bold]")
                console.print(
                    f"     [dim]{a.get('lens', '')} — {a.get('impact', '')}[/dim]"
                )
                console.print(f"     {a.get('why', '')}")

        if expand_actions:
            console.print()
            console.print(
                "[bold yellow]Expansions[/bold yellow] "
                "[dim](require your approval — see .sentinel/proposals/)[/dim]"
            )
            for i, a in enumerate(expand_actions, 1):
                console.print(f"  [bold]{i}. {a.get('title', '')}[/bold]")
                console.print(
                    f"     [dim]{a.get('lens', '')} — {a.get('impact', '')}[/dim]"
                )
                console.print(f"     {a.get('why', '')}")

    # Persist
    try:
        scan_file = _persist_scan(project, result)
        console.print()
        console.print(f"  [dim]Saved to: {scan_file.relative_to(project)}[/dim]")
    except (OSError, ValueError) as e:
        console.print(f"  [yellow]Could not persist scan: {e}[/yellow]")

    # Totals
    console.print()
    console.print(
        f"  [dim]Provider: {result.provider} | Model: {result.model}[/dim]"
    )
    console.print(
        f"  [dim]Tokens: {result.total_input_tokens} in / "
        f"{result.total_output_tokens} out[/dim]"
    )
    if result.total_cost_usd > 0:
        console.print(f"  [dim]Cost: ${result.total_cost_usd:.4f}[/dim]")
    console.print(f"  [dim]Time: {elapsed:.1f}s[/dim]")
    console.print()
