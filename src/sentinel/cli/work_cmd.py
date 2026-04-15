"""
sentinel work — the one command.

Figures out what the project needs and does it, until:
  - the budget (time or money) is hit
  - the backlog is empty
  - the user interrupts (Ctrl-C)
  - something fails that needs human attention

State machine:
  1. Not initialized? Run init.
  2. No recent scan (older than 1 hour, or none)? Run scan.
  3. No backlog or backlog stale (older than the latest scan)? Run plan.
  4. Backlog has items? Execute top item, review, commit to feature branch.
  5. Repeat from step 2 if budget remains.
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


def _working_tree_clean(project: Path | str) -> bool:
    """True iff the project has no user-owned dirty state.

    Used at cycle start so we never wipe user work. Covers:
    - tracked modifications + staged changes (reset --hard would wipe)
    - untracked files OUTSIDE sentinel's own directories (git clean -fd
      between items would wipe)

    Sentinel's own artifacts (.sentinel/, .claude/) don't count — the
    between-item clean excludes them explicitly, and init commits the
    .gitignore entries immediately.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=project, timeout=10,
    )
    if result.returncode != 0:
        # Not a git repo or git missing — let the caller proceed; other
        # git calls will surface the real error downstream.
        return True
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        filename = line[3:]
        # Skip sentinel's own artifacts — excluded from clean anyway
        if filename.startswith(".sentinel/") or filename.startswith(".claude/"):
            continue
        # Anything else is user state we can't risk clobbering
        return False
    return True


def _reset_and_checkout(project: str, branch: str) -> bool:
    """Reset the working tree and checkout a branch.

    `git checkout` fails silently on dirty trees, which causes each
    work item's edits to stack on the previous. After Coder commits
    its real work to its own feature branch, anything lingering in the
    tree here is a failed attempt (pre-commit hook rejection, Claude
    error mid-edit, etc.) and should be discarded before we move on.

    We preserve .sentinel/ and .claude/ from untracked-cleanup scope —
    those are sentinel's own artifacts, never part of an item.

    Returns True if the sequence landed us on `branch` with a clean
    tree. Callers must abort the loop on False — silently proceeding
    on the wrong branch is how we got the sigint commingling bug.
    """
    reset = subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        capture_output=True, text=True, cwd=project, timeout=30,
    )
    if reset.returncode != 0:
        console.print(
            f"  [red]git reset --hard failed:[/red] {reset.stderr.strip()}"
        )
        return False

    clean = subprocess.run(
        ["git", "clean", "-fd",
         "--exclude=.sentinel/", "--exclude=.claude/"],
        capture_output=True, text=True, cwd=project, timeout=30,
    )
    if clean.returncode != 0:
        console.print(
            f"  [red]git clean failed:[/red] {clean.stderr.strip()}"
        )
        return False

    co = subprocess.run(
        ["git", "checkout", branch],
        capture_output=True, text=True, cwd=project, timeout=30,
    )
    if co.returncode != 0:
        console.print(
            f"  [red]git checkout {branch} failed:[/red] {co.stderr.strip()}"
        )
        return False

    return True


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
    """Parse backlog + approved proposals and return executable items.

    Order: refinements first (from scan), then approved expansions
    (from proposals). Skips pending/rejected proposals.
    """
    from sentinel.cli.cycle_cmd import _load_approved_proposals

    backlog = project / ".sentinel" / "backlog.md"
    if not backlog.exists():
        return []
    scan = _find_latest_scan(project)
    if not scan:
        return []

    actions = _parse_actions_from_scan(scan)
    refinements = [a for a in actions if a.get("kind", "refine") == "refine"]
    approved = _load_approved_proposals(project)

    return refinements + approved


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

    # Set the cycle deadline so provider subprocess timeouts
    # (run_cli_async) are clamped to the remaining budget. Without this a
    # single slow LLM call can blow past `--budget 10m` by another 10
    # minutes because each provider CLI still uses its own 600s timeout.
    from sentinel.budget_ctx import set_cycle_deadline
    set_cycle_deadline(time_budget_sec)

    console.print(f"\n[bold]Sentinel[/bold] — {project.name}")
    if budget_str:
        console.print(f"  Budget: {budget_str}")
    if dry_run:
        console.print("  [yellow]Dry run — no execution[/yellow]")
    console.print()

    # Refuse to start if the user has pending uncommitted work. Between
    # items we own the working tree and reset freely; at cycle start
    # that state is the user's, and silently wiping it would destroy
    # hours of someone's work.
    if not _working_tree_clean(project):
        console.print(
            "[red]  Working tree has uncommitted changes.[/red]\n"
            "  sentinel resets the tree between work items; running on a "
            "dirty tree would destroy your changes.\n"
            "  Commit, stash, or discard your changes, then run again."
        )
        return

    # --- 1. Initialize if needed ---
    if not (project / ".sentinel" / "config.toml").exists():
        console.print("[bold cyan]→ Initializing[/bold cyan]\n")
        run_init(str(project))
        console.print()

    config = _load_config(project)
    if not config:
        return

    # Prune aged-out run journals before the cycle starts. Silent on
    # the common case (nothing expired), one-line note when something
    # was actually removed. Failing prune doesn't block work.
    from sentinel.prune import prune_runs
    try:
        removed = prune_runs(project, config.retention.runs_days)
        if removed:
            console.print(
                f"  [dim]Pruned {removed} run journal"
                f"{'s' if removed != 1 else ''} older than "
                f"{config.retention.runs_days} days[/dim]\n"
            )
    except OSError as e:
        console.print(f"  [yellow]Prune skipped: {e}[/yellow]\n")

    # Open the run journal. Providers and phase wrappers will record into
    # this via ContextVar; the finally block writes it to disk regardless
    # of how the cycle ends (success, exception, KeyboardInterrupt).
    from sentinel.journal import Journal, set_current_journal, set_current_phase
    journal = Journal(
        project_path=project,
        project_name=project.name,
        branch=_current_branch(str(project)),
        budget_str=budget_str,
    )
    set_current_journal(journal)

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
                journal.exit_reason = f"budget: {reason}"
                break

            # --- 3. Scan if stale or missing ---
            scan_age = _latest_scan_age(project)
            if scan_age is None or scan_age > timedelta(hours=1):
                console.print("[bold cyan]→ Scanning[/bold cyan]")
                if scan_age:
                    mins = int(scan_age.total_seconds() / 60)
                    console.print(f"  [dim]Last scan: {mins} min ago[/dim]")

                set_current_phase("scan")
                journal.start_phase("scan")
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
                    journal.end_phase("scan", status="failed", error=scan_result.error)
                    console.print(f"  [red]Scan failed: {scan_result.error}[/red]")
                    # Persist whatever lens work completed before the failure.
                    # Silently dropping successful lens evaluations on a
                    # synthesis timeout is exactly the "no silent failures"
                    # violation the engineering principles call out.
                    if scan_result.evaluations:
                        try:
                            scan_file = _persist_scan(project, scan_result)
                            console.print(
                                f"  [dim]Partial scan saved to: "
                                f"{scan_file.relative_to(project)}[/dim]"
                            )
                        except (OSError, ValueError) as persist_err:
                            console.print(
                                f"  [yellow]Could not persist partial scan: "
                                f"{persist_err}[/yellow]"
                            )
                    raise click.exceptions.Exit(code=1)

                _persist_scan(project, scan_result)
                journal.end_phase("scan")
                console.print(
                    f"  [green]✓[/green] Health: {scan_result.overall_score}/100 "
                    f"(${scan_result.total_cost_usd:.4f})\n"
                )

            # --- 4. Plan if backlog stale ---
            if _backlog_stale(project):
                console.print("[bold cyan]→ Planning[/bold cyan]")
                set_current_phase("plan")
                journal.start_phase("plan")
                scan_file = _find_latest_scan(project)
                if not scan_file:
                    journal.end_phase("plan", status="failed", error="no scan")
                    journal.exit_reason = "no_scan_to_plan_from"
                    console.print("  [red]No scan to plan from[/red]")
                    break
                actions = _parse_actions_from_scan(scan_file)
                _write_backlog(project, actions, scan_file)
                # Write expansion proposals so user can approve later
                from sentinel.cli.plan_cmd import _write_proposals
                proposals = _write_proposals(project, actions, scan_file)

                refinements = [
                    a for a in actions
                    if a.get("kind", "refine") == "refine"
                ]
                expansions = [a for a in actions if a.get("kind") == "expand"]
                console.print(
                    f"  [green]✓[/green] {len(refinements)} refinements, "
                    f"{len(expansions)} expansion proposals"
                )
                journal.end_phase("plan")
                if proposals:
                    console.print(
                        "  [dim]  New proposals in .sentinel/proposals/ — "
                        "review and flip Status to 'approved' to queue[/dim]\n"
                    )
                else:
                    console.print()

            # --- 5. Execute next item ---
            items = _remaining_backlog_items(project)
            if not items:
                console.print("[green]  Backlog empty. Done.[/green]")
                journal.exit_reason = "backlog_empty"
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
                    journal.exit_reason = "user_declined"
                    return
                console.print()

            if dry_run:
                console.print("[bold cyan]→ Would execute[/bold cyan]")
                for i, a in enumerate(items[:3], 1):
                    kind = a.get("kind", "refine")
                    color = "green" if kind == "refine" else "yellow"
                    console.print(
                        f"  {i}. [{color}][{kind}][/{color}] {a['title']}"
                    )
                console.print()
                console.print("[yellow]  Dry run — stopping[/yellow]")
                journal.exit_reason = "dry_run"
                break

            next_item = items[items_executed] if items_executed < len(items) else None
            if not next_item:
                console.print("[green]  All items processed.[/green]")
                journal.exit_reason = "all_items_processed"
                break

            # Execute + review + verify
            from sentinel.journal import WorkItemRecord
            wi_id = str(next_item.get("id", items_executed + 1))
            wi_title = next_item.get("title", "(untitled)")
            phase_label = f"execute:{wi_id}"
            set_current_phase("execute")
            journal.start_phase(phase_label)
            success, verification_verdict = await _execute_and_review(
                next_item, items_executed + 1,
                project, original_branch,
                coder, reviewer, config,
            )
            journal.end_phase(phase_label, status=success or "unknown")

            # Mirror the outcome into the work-items table so the journal
            # shows what we ran, not just timings. _execute_and_review
            # returns one of: "failed" (coder errored before review),
            # "approved", "changes", "rejected". Map all four explicitly:
            # the previous mapping collapsed changes/rejected into
            # in_progress with no reviewer verdict, hiding real outcomes.
            wi_status, reviewer_verdict = {
                "approved": ("succeeded", "approved"),
                "changes": ("succeeded", "changes_requested"),
                "rejected": ("succeeded", "rejected"),
                "failed": ("failed", None),
            }.get(success or "", ("unknown", None))
            journal.record_work_item(WorkItemRecord(
                work_item_id=wi_id,
                title=wi_title,
                coder_status=wi_status,
                reviewer_verdict=reviewer_verdict,
                verification=verification_verdict,
            ))

            items_executed += 1
            if success == "approved":
                items_approved += 1
            elif success == "failed":
                items_failed += 1
        # Loop exits only via break paths above (each sets a specific
        # exit_reason). Falling out the bottom of `while True` would mean
        # we hit an unforeseen path — mark it as such rather than calling
        # it "complete" and hiding the surprise.
        if journal.exit_reason == "in_progress":
            journal.exit_reason = "loop_ended_unexpectedly"

    except KeyboardInterrupt:
        journal.exit_reason = "interrupted"
        console.print("\n\n[yellow]  Interrupted. Cleaning up...[/yellow]")
    except click.exceptions.Exit:
        journal.exit_reason = "scan_failed"
        raise
    except Exception as exc:
        journal.exit_reason = f"error: {exc}"
        raise

    finally:
        # Return to original branch — reset first so a failed final
        # item doesn't leave the user stranded on a dirty feature branch
        _reset_and_checkout(str(project), original_branch)
        # Write the journal regardless of how the cycle ended. Best-
        # effort: a write failure here is not worth crashing the cycle.
        try:
            journal_path = journal.write()
            console.print(
                f"  [dim]Run journal: "
                f"{journal_path.relative_to(project)}[/dim]"
            )
        except OSError as e:
            console.print(f"  [yellow]Could not write run journal: {e}[/yellow]")
        set_current_journal(None)

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
) -> tuple[str, str | None]:
    """Execute one work item, review it, and verify against project checks.

    Returns (verdict, verification_overall) where:
      - verdict is one of 'approved', 'changes', 'rejected', 'failed'
      - verification_overall is one of 'verified', 'not_verified',
        'no_check_defined', or None when verification was skipped (e.g.
        coder failed before producing a diff)
    """
    work_item = _action_to_work_item(action, index)
    console.print(f"[bold cyan]→ Executing[/bold cyan] {work_item.title}")
    console.print(f"  [dim]lens: {action.get('lens', '')}[/dim]")

    # Reset to original branch before each item. `git checkout` fails
    # silently when the working tree is dirty, which used to cause each
    # item's edits to stack on the previous — sigint dogfood showed 3
    # "successful" runs commingling into one diff. Reset first, then
    # checkout — the Coder commits its own work, so anything still in
    # the tree here is a failed attempt we shouldn't carry forward.
    if not _reset_and_checkout(str(project), original_branch):
        console.print(
            "  [red]✗ Cannot return to original branch — aborting item.[/red]"
        )
        return "failed", None

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
        return "failed", None

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

    # Verify against the project's own checks. Independent signal from
    # the reviewer LLM — proves (or disproves) that the project's
    # invariants still hold after the diff. Verdict is recorded
    # regardless of reviewer outcome; downstream tooling decides what
    # to do with not_verified items.
    from sentinel.verify import persist_verification, verify_work_item
    verification = verify_work_item(
        project_path=project,
        work_item_id=str(work_item.id),
        work_item_title=work_item.title,
        branch=exec_result.branch,
    )
    try:
        persist_verification(project, verification)
    except OSError as e:
        console.print(
            f"  [yellow]Could not persist verification: {e}[/yellow]"
        )
    verifier_icon = {
        "verified": "[green]✅[/green]",
        "not_verified": "[red]❌[/red]",
        "no_check_defined": "[dim]—[/dim]",
    }.get(verification.overall, "?")
    console.print(
        f"  Verifier: {verifier_icon} {verification.overall} "
        f"[dim]({len([c for c in verification.checks if c.verdict == 'pass'])}"
        f"/{len(verification.checks)} checks passed)[/dim]"
    )
    console.print()

    if review.verdict == "approved":
        return "approved", verification.overall
    if review.verdict == "changes-requested":
        return "changes", verification.overall
    return "rejected", verification.overall


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
