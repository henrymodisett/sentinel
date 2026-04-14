"""
sentinel work — the one command.

Figures out what the project needs and does it, until:
  - the budget (time or money) is hit
  - the backlog is empty
  - the user interrupts (Ctrl-C)
  - something fails that needs human attention

State machine:
  1. Not initialized? Run init.
  2. No goals.md or goals.md is empty template? Prompt user to fill in and stop.
  3. No recent scan (older than 1 hour, or none)? Run scan.
  4. No backlog or backlog stale (older than the latest scan)? Run plan.
  5. Backlog has items? Execute top item, review, commit to feature branch.
  6. Repeat from step 3 if budget remains.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from sentinel.budget import check_budget, record_spend
from sentinel.cli.cycle_cmd import _action_to_work_item, _current_branch
from sentinel.cli.init_cmd import run_init
from sentinel.cli.plan_cmd import (
    _find_latest_scan,
    _parse_actions_from_scan,
    _write_backlog,
)
from sentinel.cli.scan_cmd import _load_config, _persist_scan
from sentinel.config.schema import SentinelConfig  # noqa: TC001 — runtime type
from sentinel.providers.router import Router
from sentinel.roles.coder import Coder
from sentinel.roles.monitor import Monitor
from sentinel.roles.reviewer import Reviewer
from sentinel.state import gather_state

console = Console()

# Template markers that indicate goals.md hasn't been filled in
TEMPLATE_MARKERS = [
    "<!-- One paragraph: what it does",
    "<!-- 2-5 bullet points",
    "<!-- Things sentinel should know",
]


def _parse_interval(interval: str) -> int:
    """Parse interval like '10m', '1h', '30s' to seconds."""
    m = re.match(r"^(\d+)\s*([smh])$", interval.strip())
    if not m:
        import click as _click
        raise _click.BadParameter(
            f"Invalid interval '{interval}'. Use e.g. '30s', '10m', '1h'.",
        )
    n = int(m.group(1))
    unit = m.group(2)
    return {"s": n, "m": n * 60, "h": n * 3600}[unit]


def _format_duration(seconds: float) -> str:
    """Human-friendly duration."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    return f"{h}h {(s % 3600) // 60}m"


def _parse_budget(budget_str: str | None) -> tuple[float | None, int | None]:
    """Parse --budget string. Returns (money_usd, time_seconds).

    Examples:
      "$5"    -> (5.0, None)
      "5"     -> (5.0, None) — assume money if plain number
      "10m"   -> (None, 600)
      "1h"    -> (None, 3600)
      "30s"   -> (None, 30)
    """
    if not budget_str:
        return None, None

    s = budget_str.strip()

    # Money: $5, 5.50
    money_match = re.match(r"^\$?(\d+(?:\.\d+)?)$", s)
    if money_match:
        return float(money_match.group(1)), None

    # Time: 10m, 1h, 30s, 2h30m (simple suffix parse)
    time_match = re.match(r"^(\d+)\s*([smh])$", s)
    if time_match:
        n = int(time_match.group(1))
        unit = time_match.group(2)
        seconds = {"s": n, "m": n * 60, "h": n * 3600}[unit]
        return None, seconds

    raise click.BadParameter(
        f"Could not parse budget '{budget_str}'. "
        f"Examples: '$5', '10m', '1h', '30s'"
    )


def _goals_filled(project: Path) -> bool:
    """Check if goals.md has been edited beyond the template."""
    goals = project / ".sentinel" / "goals.md"
    if not goals.exists():
        return False
    content = goals.read_text()
    # If any template comment markers are still present, treat as unfilled
    return not any(marker in content for marker in TEMPLATE_MARKERS)


def _latest_scan_age(project: Path) -> timedelta | None:
    """Age of the most recent scan, or None if no scans exist."""
    scan = _find_latest_scan(project)
    if not scan:
        return None
    mtime = datetime.fromtimestamp(scan.stat().st_mtime)
    return datetime.now() - mtime


def _backlog_stale(project: Path) -> bool:
    """True if backlog is missing or older than latest scan."""
    backlog = project / ".sentinel" / "backlog.md"
    scan = _find_latest_scan(project)
    if not backlog.exists() or not scan:
        return True
    return scan.stat().st_mtime > backlog.stat().st_mtime


def _remaining_backlog_items(project: Path) -> list[dict]:
    """Parse backlog and return items still marked todo."""
    backlog = project / ".sentinel" / "backlog.md"
    if not backlog.exists():
        return []
    scan = _find_latest_scan(project)
    if not scan:
        return []
    items = _parse_actions_from_scan(scan)
    # TODO: filter out items that are already executed (have a branch)
    return items


async def run_work(
    project_path: str | None = None,
    budget_str: str | None = None,
    dry_run: bool = False,
    auto: bool = False,
    every: str | None = None,
) -> None:
    """The one command.

    Single mode (default): runs one cycle of work and exits.
    Loop mode (--every): keeps running cycles with sleep between, until
    Ctrl-C, budget hit, or max cycles.
    """
    if every is None:
        # Single cycle — just run it and return
        await _run_single_cycle(project_path, budget_str, dry_run, auto)
        return

    # Loop mode
    await _run_loop(project_path, budget_str, dry_run, auto, every)


async def _run_single_cycle(
    project_path: str | None = None,
    budget_str: str | None = None,
    dry_run: bool = False,
    auto: bool = False,
) -> None:
    """Run exactly one cycle of work and return."""
    project = Path(project_path or os.getcwd()).resolve()
    money_budget, time_budget_sec = _parse_budget(budget_str)
    start_time = time.time()

    console.print(f"\n[bold]Sentinel[/bold] — {project.name}")
    if budget_str:
        console.print(f"  Budget: {budget_str}")
    if dry_run:
        console.print("  [yellow]Dry run — no execution[/yellow]")
    console.print()

    # --- 1. Initialize if needed ---
    if not (project / ".sentinel" / "config.toml").exists():
        console.print("[bold cyan]→ Initializing[/bold cyan]\n")
        run_init(str(project))
        console.print()

    config = _load_config(project)
    if not config:
        return

    # --- 2. Check goals.md ---
    if not _goals_filled(project):
        console.print(
            "[yellow]  Goals not filled in yet.[/yellow]\n"
            "  Edit [cyan].sentinel/goals.md[/cyan] to describe your project, "
            "then run `sentinel work` again.\n"
            "  Sentinel produces much better results with goals.md filled in."
        )
        return

    # --- Main work loop ---
    router = Router(config)
    monitor = Monitor(router)
    coder = Coder(router)
    reviewer = Reviewer(router)
    original_branch = _current_branch(str(project))

    items_executed = 0
    items_approved = 0
    items_failed = 0

    try:
        while True:
            # Budget check
            budget_ok, reason = _check_all_budgets(
                project, config, money_budget, start_time, time_budget_sec,
            )
            if not budget_ok:
                console.print(f"\n[yellow]  Stopping: {reason}[/yellow]")
                break

            # --- 3. Scan if stale or missing ---
            scan_age = _latest_scan_age(project)
            if scan_age is None or scan_age > timedelta(hours=1):
                console.print("[bold cyan]→ Scanning[/bold cyan]")
                if scan_age:
                    mins = int(scan_age.total_seconds() / 60)
                    console.print(f"  [dim]Last scan: {mins} min ago[/dim]")

                state = gather_state(project)
                from sentinel.cli.scan_cmd import scan_progress_printer
                scan_result = await monitor.assess(
                    state, on_progress=scan_progress_printer(),
                )

                if scan_result.total_cost_usd > 0:
                    record_spend(
                        project, scan_result.total_cost_usd, "work-scan",
                        f"model={scan_result.model}",
                    )

                if not scan_result.ok:
                    console.print(f"  [red]Scan failed: {scan_result.error}[/red]")
                    break

                _persist_scan(project, scan_result)
                console.print(
                    f"  [green]✓[/green] Health: {scan_result.overall_score}/100 "
                    f"(${scan_result.total_cost_usd:.4f})\n"
                )

            # --- 4. Plan if backlog stale ---
            if _backlog_stale(project):
                console.print("[bold cyan]→ Planning[/bold cyan]")
                scan_file = _find_latest_scan(project)
                if not scan_file:
                    console.print("  [red]No scan to plan from[/red]")
                    break
                actions = _parse_actions_from_scan(scan_file)
                _write_backlog(project, actions, scan_file)
                console.print(
                    f"  [green]✓[/green] {len(actions)} items in backlog\n"
                )

            # --- 5. Execute next item ---
            items = _remaining_backlog_items(project)
            if not items:
                console.print("[green]  Backlog empty. Done.[/green]")
                break

            # Handle first execution — confirm unless --auto or --dry-run
            if items_executed == 0 and not auto and not dry_run:
                console.print("[bold]Next up:[/bold]")
                for i, a in enumerate(items[:3], 1):
                    console.print(f"  {i}. {a['title']}")
                console.print()
                if not click.confirm(
                    "  Proceed with autonomous execution?", default=False,
                ):
                    console.print("[yellow]  Stopped before execution.[/yellow]")
                    return
                console.print()

            if dry_run:
                console.print("[bold cyan]→ Would execute[/bold cyan]")
                for i, a in enumerate(items[:3], 1):
                    console.print(f"  {i}. {a['title']}")
                console.print()
                console.print("[yellow]  Dry run — stopping[/yellow]")
                break

            next_item = items[items_executed] if items_executed < len(items) else None
            if not next_item:
                console.print("[green]  All items processed.[/green]")
                break

            # Execute + review
            success = await _execute_and_review(
                next_item, items_executed + 1,
                project, original_branch,
                coder, reviewer, config,
            )
            items_executed += 1
            if success == "approved":
                items_approved += 1
            elif success == "failed":
                items_failed += 1

    except KeyboardInterrupt:
        console.print("\n\n[yellow]  Interrupted. Cleaning up...[/yellow]")

    finally:
        # Return to original branch
        subprocess.run(
            ["git", "checkout", original_branch],
            capture_output=True, cwd=project, timeout=30,
        )

    # --- Summary ---
    elapsed = time.time() - start_time
    budget_now = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )
    console.print()
    console.print(
        Panel(
            f"Items executed: {items_executed}\n"
            f"  • Approved: {items_approved}\n"
            f"  • Failed: {items_failed}\n\n"
            f"Time: {int(elapsed)}s\n"
            f"Spend today: ${budget_now.today_spent_usd:.4f} / "
            f"${budget_now.daily_limit_usd:.2f}",
            title="[bold]Done[/bold]",
            border_style="cyan",
        )
    )
    console.print()


def _check_all_budgets(
    project: Path,
    config: SentinelConfig,
    money_budget: float | None,
    start_time: float,
    time_budget_sec: int | None,
) -> tuple[bool, str]:
    """Check all budget constraints. Returns (ok, reason_if_not)."""
    # Daily money budget (from config)
    budget = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )
    if budget.over_limit:
        return False, (
            f"daily budget reached "
            f"(${budget.today_spent_usd:.2f} / ${budget.daily_limit_usd:.2f})"
        )

    # Per-work money budget (from --budget flag)
    if money_budget is not None and budget.today_spent_usd >= money_budget:
        return False, (
            f"session money budget reached "
            f"(${budget.today_spent_usd:.2f} / ${money_budget:.2f})"
        )

    # Per-work time budget
    if time_budget_sec is not None:
        elapsed = time.time() - start_time
        if elapsed >= time_budget_sec:
            mins = int(elapsed / 60)
            return False, f"time budget reached ({mins} min)"

    return True, ""


async def _execute_and_review(
    action: dict,
    index: int,
    project: Path,
    original_branch: str,
    coder: Coder,
    reviewer: Reviewer,
    config: SentinelConfig,
) -> str:
    """Execute one work item and review it.

    Returns 'approved', 'changes', 'rejected', or 'failed'.
    """
    work_item = _action_to_work_item(action, index)
    console.print(f"[bold cyan]→ Executing[/bold cyan] {work_item.title}")
    console.print(f"  [dim]lens: {action.get('lens', '')}[/dim]")

    # Reset to original branch before each item
    subprocess.run(
        ["git", "checkout", original_branch],
        capture_output=True, cwd=project, timeout=30,
    )

    t0 = time.time()
    exec_result = await coder.execute(work_item, str(project))

    if exec_result.cost_usd > 0:
        record_spend(
            project, exec_result.cost_usd, "work-execute",
            f"item={work_item.title[:40]}",
        )

    elapsed = time.time() - t0
    if exec_result.status == "failed":
        console.print(f"  [red]✗ Execute failed:[/red] {exec_result.error}")
        return "failed"

    console.print(
        f"  [green]✓ Coded[/green] in {elapsed:.0f}s — "
        f"{len(exec_result.files_changed)} files, "
        f"tests: {'pass' if exec_result.tests_passing else 'FAIL'}"
    )

    # Review
    console.print("  [dim]reviewing...[/dim]")
    review = await reviewer.review(work_item, exec_result, str(project))
    if review.cost_usd > 0:
        record_spend(
            project, review.cost_usd, "work-review",
            f"item={work_item.title[:40]}",
        )

    verdict_color = {
        "approved": "green",
        "changes-requested": "yellow",
        "rejected": "red",
    }[review.verdict]
    console.print(
        f"  [{verdict_color}]Review: {review.verdict}[/{verdict_color}] "
        f"[dim]→ branch: {exec_result.branch}[/dim]"
    )
    if review.blocking_issues:
        for issue in review.blocking_issues[:2]:
            console.print(f"    • {issue}")
    console.print()

    if review.verdict == "approved":
        return "approved"
    if review.verdict == "changes-requested":
        return "changes"
    return "rejected"


async def _run_loop(
    project_path: str | None,
    budget_str: str | None,
    dry_run: bool,
    auto: bool,
    every: str,
) -> None:
    """Run cycles continuously until stopped."""
    import asyncio

    project = Path(project_path or os.getcwd()).resolve()
    interval_sec = _parse_interval(every)
    money_budget, time_budget_sec = _parse_budget(budget_str)

    # Pre-flight: need config to check session spend
    config = _load_config(project)
    if not config and (project / ".sentinel" / "config.toml").exists():
        return

    session_start = time.time()
    session_spend_start = 0.0
    if config:
        session_spend_start = check_budget(
            project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
        ).today_spent_usd

    cycles = 0
    console.print("\n[bold]Sentinel Work[/bold] — loop mode")
    console.print(f"  Cadence: every {every}")
    if budget_str:
        console.print(f"  Session budget: {budget_str}")
    console.print("  [dim]Ctrl-C to stop[/dim]")

    try:
        while True:
            cycles += 1
            console.print(
                f"\n[bold cyan]─── Cycle {cycles} "
                f"({datetime.now().strftime('%H:%M:%S')}) ───[/bold cyan]"
            )

            await _run_single_cycle(
                project_path=str(project),
                budget_str=None,  # session budget is tracked outside
                dry_run=dry_run,
                auto=True,  # loop mode bypasses confirmation
            )

            # Session bounds check
            elapsed = time.time() - session_start
            if time_budget_sec is not None and elapsed >= time_budget_sec:
                console.print(
                    f"\n[yellow]Stopping: session time budget "
                    f"{_format_duration(elapsed)} reached[/yellow]"
                )
                break

            if money_budget is not None and config:
                current = check_budget(
                    project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
                )
                session_spent = current.today_spent_usd - session_spend_start
                if session_spent >= money_budget:
                    console.print(
                        f"\n[yellow]Stopping: session spend "
                        f"${session_spent:.2f} hit cap "
                        f"${money_budget:.2f}[/yellow]"
                    )
                    break

            console.print(
                f"\n[dim]Next cycle in {every}... (Ctrl-C to stop)[/dim]"
            )
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                raise
    except KeyboardInterrupt:
        console.print("\n\n[yellow]Stopped by user.[/yellow]")

    elapsed = time.time() - session_start
    console.print()
    console.print("[bold]Session summary[/bold]")
    console.print(f"  Cycles: {cycles}")
    console.print(f"  Duration: {_format_duration(elapsed)}")
    if config:
        final = check_budget(
            project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
        )
        session_spent = final.today_spent_usd - session_spend_start
        console.print(f"  Session spend: ${session_spent:.4f}")
    console.print()
