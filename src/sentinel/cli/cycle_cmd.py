"""
sentinel cycle — the autonomous loop.

Pipeline: scan → plan → execute top N items → review each → report.

Respects the daily budget limit. Halts early if budget would be exceeded.
Each successful execution lands on its own feature branch for human review.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from sentinel.budget import check_budget, record_spend
from sentinel.cli.plan_cmd import _find_latest_scan, _parse_actions_from_scan, _write_backlog
from sentinel.cli.scan_cmd import _load_config, _persist_scan
from sentinel.providers.router import Router
from sentinel.roles.coder import Coder
from sentinel.roles.monitor import Monitor
from sentinel.roles.planner import WorkItem
from sentinel.roles.reviewer import Reviewer
from sentinel.state import gather_state

console = Console()


def _action_to_work_item(action: dict, index: int) -> WorkItem:
    """Convert a scan action (dict) into a WorkItem."""
    return WorkItem(
        id=f"cycle-{index}",
        title=action["title"],
        description=action.get("why", ""),
        type="chore",  # could infer from action.lens
        priority="high",
        complexity=2,  # default medium
        files=action.get("files", []),
        acceptance_criteria=[
            action.get("impact", ""),
        ],
        risk="",
    )


def _current_branch(project_path: str) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=project_path, timeout=10,
    )
    return result.stdout.strip()


async def run_cycle(
    project_path: str | None = None,
    max_items: int = 3,
    dry_run: bool = False,
) -> None:
    """Run one autonomous cycle."""
    project = Path(project_path or os.getcwd()).resolve()
    config = _load_config(project)
    if not config:
        return

    console.print(f"\n[bold]Sentinel Cycle[/bold] — {project.name}")
    if dry_run:
        console.print("[dim]  (dry run — will plan but not execute)[/dim]")
    console.print()

    # Budget check before starting
    budget = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )
    if budget.over_limit:
        console.print(
            f"[red]Daily budget exceeded: ${budget.today_spent_usd:.2f} / "
            f"${budget.daily_limit_usd:.2f}. Refusing to cycle.[/red]"
        )
        return
    console.print(
        f"  Budget: ${budget.today_spent_usd:.2f} spent / "
        f"${budget.daily_limit_usd:.2f} limit\n"
    )

    # Save current branch so we can return to it
    original_branch = _current_branch(str(project))

    # --- PHASE 1: SCAN ---
    console.print("[bold cyan]Phase 1/4: Scan[/bold cyan]")
    state = gather_state(project)
    router = Router(config)
    monitor = Monitor(router)

    scan_cost_start = budget.today_spent_usd
    scan_result = await monitor.assess(state)

    if scan_result.total_cost_usd > 0:
        record_spend(
            project, scan_result.total_cost_usd, "cycle-scan",
            f"model={scan_result.model}",
        )

    if not scan_result.ok:
        console.print(f"[red]Scan failed: {scan_result.error}[/red]")
        return

    _persist_scan(project, scan_result)
    console.print(
        f"  [green]✓[/green] Health: {scan_result.overall_score}/100 "
        f"(cost: ${scan_result.total_cost_usd:.4f})\n"
    )

    # --- PHASE 2: PLAN ---
    console.print("[bold cyan]Phase 2/4: Plan[/bold cyan]")
    scan_file = _find_latest_scan(project)
    if not scan_file:
        console.print("[red]No scan to plan from[/red]")
        return
    actions = _parse_actions_from_scan(scan_file)
    _write_backlog(project, actions, scan_file)
    console.print(
        f"  [green]✓[/green] {len(actions)} work items in backlog\n"
    )

    if dry_run:
        console.print("[yellow]Dry run — stopping before execution[/yellow]\n")
        console.print("[bold]Would execute:[/bold]")
        for i, action in enumerate(actions[:max_items], 1):
            console.print(f"  {i}. {action['title']}")
        console.print()
        return

    # Confirm before executing
    console.print(
        f"[bold]Phase 3 will execute top {min(max_items, len(actions))} "
        f"items on feature branches.[/bold]"
    )
    for i, action in enumerate(actions[:max_items], 1):
        console.print(f"  {i}. {action['title']}")
    console.print()
    if not click.confirm("  Proceed with autonomous execution?", default=False):
        console.print("[yellow]Stopped before execution.[/yellow]")
        return
    console.print()

    # --- PHASE 3: EXECUTE ---
    console.print("[bold cyan]Phase 3/4: Execute[/bold cyan]")
    coder = Coder(router)
    reviewer = Reviewer(router)

    executions = []
    reviews = []

    for i, action in enumerate(actions[:max_items], 1):
        # Re-check budget before each execution
        budget = check_budget(
            project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
        )
        if budget.over_limit:
            console.print(
                f"[yellow]  Stopped at item {i-1}/{max_items} — "
                f"budget hit.[/yellow]"
            )
            break

        work_item = _action_to_work_item(action, i)
        console.print(f"\n  [bold]({i}/{max_items}) {work_item.title}[/bold]")
        console.print(f"  [dim]lens: {action.get('lens', '')}[/dim]")

        # Return to original branch before each item (parallel-able later)
        subprocess.run(
            ["git", "checkout", original_branch],
            capture_output=True, cwd=project, timeout=30,
        )

        t0 = time.time()
        exec_result = await coder.execute(work_item, str(project))

        if exec_result.cost_usd > 0:
            record_spend(
                project, exec_result.cost_usd, "cycle-execute",
                f"item={work_item.title[:40]}",
            )

        elapsed = time.time() - t0
        if exec_result.status == "failed":
            console.print(
                f"    [red]✗ Execute failed: {exec_result.error}[/red]"
            )
            executions.append(exec_result)
            continue

        console.print(
            f"    [green]✓ Coded in {elapsed:.0f}s — "
            f"{len(exec_result.files_changed)} files changed, "
            f"tests: {'pass' if exec_result.tests_passing else 'FAIL'}[/green]"
        )
        executions.append(exec_result)

        # --- PHASE 4: REVIEW (per item) ---
        console.print("    [dim]reviewing...[/dim]")
        review = await reviewer.review(work_item, exec_result, str(project))
        if review.cost_usd > 0:
            record_spend(
                project, review.cost_usd, "cycle-review",
                f"item={work_item.title[:40]}",
            )
        reviews.append(review)

        verdict_color = {
            "approved": "green",
            "changes-requested": "yellow",
            "rejected": "red",
        }[review.verdict]
        console.print(
            f"    [{verdict_color}]Review: {review.verdict}[/{verdict_color}]"
        )
        if review.blocking_issues:
            for issue in review.blocking_issues[:3]:
                console.print(f"      • {issue}")

    # Return to original branch
    subprocess.run(
        ["git", "checkout", original_branch],
        capture_output=True, cwd=project, timeout=30,
    )

    # --- SUMMARY ---
    console.print()
    console.print(
        Panel(
            _summarize_cycle(executions, reviews, budget, scan_cost_start),
            title="[bold]Cycle Complete[/bold]",
            border_style="cyan",
        )
    )
    console.print()

    # List branches created
    branches = [e.branch for e in executions if e.branch]
    if branches:
        console.print("[bold]Branches created (ready for your review):[/bold]")
        for e in executions:
            if e.branch:
                status_icon = "✓" if e.status == "success" else "!"
                console.print(f"  {status_icon} [cyan]{e.branch}[/cyan]")
        console.print()


def _summarize_cycle(executions, reviews, budget, scan_cost_start) -> str:
    approved = sum(1 for r in reviews if r.verdict == "approved")
    changes = sum(1 for r in reviews if r.verdict == "changes-requested")
    rejected = sum(1 for r in reviews if r.verdict == "rejected")
    failed = sum(1 for e in executions if e.status == "failed")

    # Re-check budget
    total_spent = budget.today_spent_usd

    return (
        f"Executed: {len(executions)} items\n"
        f"  • Approved: {approved}\n"
        f"  • Changes requested: {changes}\n"
        f"  • Rejected: {rejected}\n"
        f"  • Failed to execute: {failed}\n\n"
        f"Total spend today: ${total_spent:.4f} "
        f"(limit: ${budget.daily_limit_usd:.2f})"
    )
