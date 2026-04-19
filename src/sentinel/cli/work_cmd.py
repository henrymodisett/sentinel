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
from sentinel.integrations.cortex import (
    build_cycle_data_from_journal,
    cycle_id_from_run_path,
    detect_cortex,
    resolve_enabled,
    write_cortex_journal_entry,
)
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

    The two dimensions are independent — either can be set, both can be
    set together (comma-separated), or neither.

    Examples:
      "$5"        -> (5.0, None)
      "5"         -> (5.0, None) — assume money if plain number
      "10m"       -> (None, 600)
      "1h"        -> (None, 3600)
      "30s"       -> (None, 30)
      "10m,$5"    -> (5.0, 600) — both
      "$5,10m"    -> (5.0, 600) — order doesn't matter
    """
    if not budget_str:
        return None, None

    money_usd: float | None = None
    time_seconds: int | None = None

    # Split on comma to allow combined "10m,$5" — each part is parsed
    # independently, and either dimension can appear in either order.
    for raw_part in budget_str.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if (m := re.match(r"^\$?(\d+(?:\.\d+)?)$", part)):
            money_usd = float(m.group(1))
            continue
        if (t := re.match(r"^(\d+)\s*([smh])$", part)):
            n = int(t.group(1))
            unit = t.group(2)
            time_seconds = {"s": n, "m": n * 60, "h": n * 3600}[unit]
            continue
        import click as _click
        raise _click.BadParameter(
            f"Invalid budget component {part!r}. "
            f"Use $5 (money), 10m/1h/30s (time), or '10m,$5' (both).",
        )
    return money_usd, time_seconds


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


def _resolve_coder_timeout(
    *,
    cli_value: int | None,
    env_value: str | None,
    config_value: int,
) -> int:
    """Resolve the Coder CLI timeout from the three precedence levels.

    Precedence: CLI flag > env var (SENTINEL_CODER_TIMEOUT) > config
    (`[coder] timeout_seconds`) > default. Each layer is independently
    validated against `[CODER_TIMEOUT_MIN_SEC, CODER_TIMEOUT_MAX_SEC]`.

    Raising `click.BadParameter` on out-of-range / unparseable values
    at the CLI boundary keeps the misconfiguration visible to the
    operator rather than silently falling back to the next layer — a
    typo in --coder-timeout should fail loudly, not quietly ignore.
    """
    from sentinel.config.schema import (
        CODER_TIMEOUT_MAX_SEC,
        CODER_TIMEOUT_MIN_SEC,
    )

    def _check(label: str, v: int) -> int:
        if v < CODER_TIMEOUT_MIN_SEC or v > CODER_TIMEOUT_MAX_SEC:
            raise click.BadParameter(
                f"{label}={v} out of range "
                f"[{CODER_TIMEOUT_MIN_SEC}, {CODER_TIMEOUT_MAX_SEC}]",
            )
        return v

    if cli_value is not None:
        return _check("--coder-timeout", cli_value)
    if env_value is not None and env_value.strip():
        try:
            parsed = int(env_value.strip())
        except ValueError as e:
            raise click.BadParameter(
                f"SENTINEL_CODER_TIMEOUT={env_value!r} is not a valid integer",
            ) from e
        return _check("SENTINEL_CODER_TIMEOUT", parsed)
    # Config value has already been validated by pydantic at load time.
    return config_value


async def run_work(
    project_path: str | None = None,
    budget_str: str | None = None,
    dry_run: bool = False,
    auto: bool = False,
    every: str | None = None,
    cortex_journal: bool | None = None,
    coder_timeout: int | None = None,
) -> None:
    """The one command.

    Single mode (default): runs one cycle of work and exits.
    Loop mode (--every): keeps running cycles with sleep between, until
    Ctrl-C, budget hit, or max cycles.

    ``cortex_journal`` maps to ``--cortex-journal`` / ``--no-cortex-journal``.
    Forwarded to each cycle so loop-mode honors the flag every tick; the
    per-cycle resolution consults config and auto-detect when None.

    ``coder_timeout`` maps to ``--coder-timeout <seconds>`` and overrides
    both `SENTINEL_CODER_TIMEOUT` and `[coder] timeout_seconds`. When
    None we fall through to env → config → default.
    """
    if every is None:
        # Single cycle — just run it and return
        await _run_single_cycle(
            project_path, budget_str, dry_run, auto,
            cortex_journal=cortex_journal,
            coder_timeout=coder_timeout,
        )
        return

    # Loop mode
    await _run_loop(
        project_path, budget_str, dry_run, auto, every,
        cortex_journal=cortex_journal,
        coder_timeout=coder_timeout,
    )


def _emit_cortex_t16_entry(
    project: Path,
    journal,  # type: ignore[no-untyped-def]  # sentinel.journal.Journal at runtime
    journal_path: Path | None,
    *,
    cli_flag: bool | None,
    config: SentinelConfig | None,
    overall_score: int | None,
    lens_scores: list[tuple[str, int]],
    refinement_count: int,
    expansion_count: int,
) -> None:
    """Operationalize Cortex Protocol T1.6 at the cycle-end hook.

    Resolution precedence (cli_flag > config > auto-detect) is handled
    inside `resolve_enabled`. When the write is attempted, we honor the
    plan's non-blocking failure mode: a warning on stderr, a structured
    line in `.sentinel/state/cortex-write-errors.jsonl`, and the cycle
    exits 0 on an otherwise-successful cycle regardless.
    """
    presence = detect_cortex(project)

    config_value: str | None = None
    if config is not None:
        # `integrations.cortex.enabled` is always set because
        # IntegrationsConfig has a default factory; reading it here
        # defensively guards against a partially-constructed config in
        # tests that mint SentinelConfig by hand.
        integrations = getattr(config, "integrations", None)
        cortex_cfg = getattr(integrations, "cortex", None) if integrations else None
        config_value = getattr(cortex_cfg, "enabled", None)

    enabled = resolve_enabled(
        cli_flag=cli_flag,
        config_value=config_value,
        cortex_present=presence.dir_present,
    )
    if not enabled:
        # `--no-cortex-journal` or config `off` or auto-detect miss —
        # all three are deliberate silent skips. Only speak up when the
        # user explicitly asked for a write and we can't do it.
        if cli_flag is True and not presence.dir_present:
            console.print(
                "  [yellow]--cortex-journal set but .cortex/ not found — "
                "writing anyway (forced); first run will scaffold the "
                "journal directory.[/yellow]"
            )
            # Forced write continues below — fall through.
        else:
            return

    # Derive cycle_id from the run-journal filename so the sentinel run
    # and cortex entry are joinable by substring. Fallback to the
    # journal's timestamp when the run journal couldn't be written.
    if journal_path is not None:
        cycle_id = cycle_id_from_run_path(journal_path)
    else:
        from datetime import datetime as _dt
        cycle_id = _dt.fromtimestamp(journal.started_at).strftime("%Y-%m-%d-%H%M%S")

    cycle_data = build_cycle_data_from_journal(
        journal,
        cycle_id=cycle_id,
        project_dir=project,
        overall_score=overall_score,
        lens_scores=lens_scores,
        refinement_count=refinement_count,
        expansion_count=expansion_count,
    )

    force = cli_flag is True and not presence.dir_present
    result = write_cortex_journal_entry(project, cycle_data, force=force)

    if result.status == "written" and result.path is not None:
        try:
            rel = result.path.relative_to(project)
            console.print(f"  [dim]Cortex journal: {rel}[/dim]")
        except ValueError:
            console.print(f"  [dim]Cortex journal: {result.path}[/dim]")
    elif result.status == "skipped_existing":
        # Rare (timestamp-based cycle IDs); surface per the plan so the
        # operator notices clock anomalies.
        console.print(f"  [yellow]{result.warning}[/yellow]")
    elif result.status == "failed":
        # Non-blocking — warn loudly, keep the cycle exit code clean.
        console.print(f"  [yellow]{result.warning}[/yellow]")


async def _run_single_cycle(
    project_path: str | None = None,
    budget_str: str | None = None,
    dry_run: bool = False,
    auto: bool = False,
    *,
    cortex_journal: bool | None = None,
    coder_timeout: int | None = None,
) -> None:
    """Run exactly one cycle of work and return."""
    project = Path(project_path or os.getcwd()).resolve()
    money_budget, time_budget_sec = _parse_budget(budget_str)
    start_time = time.time()

    # Both budget dimensions are independent — set whichever the user
    # provided. Providers consult is_budget_exhausted() before each call
    # and short-circuit when either dimension hits its cap. Time gates
    # via wall-clock deadline; money gates via the live journal's
    # accumulated cost (so the cap reflects exactly this cycle's spend,
    # not the daily total).
    from sentinel.budget_ctx import set_cycle_deadline, set_cycle_money_cap
    set_cycle_deadline(time_budget_sec)
    set_cycle_money_cap(money_budget)

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
        # Doctrine 0002 — `sentinel init` is the canonical first-run
        # entry. `sentinel work` still auto-inits for backward compat,
        # but prints a visible warning so users discover the explicit
        # command. No hard-fail: preserving the running flow matters
        # more than enforcing discoverability at this exact moment.
        console.print(
            "[yellow]WARNING: .sentinel/config.toml not found; running "
            "implicit init with defaults. Run [bold]sentinel init[/bold] "
            "next time for interactive setup.[/yellow]\n",
        )
        console.print("[bold cyan]→ Initializing[/bold cyan]\n")
        run_init(str(project), implicit=True)
        console.print()

    config = _load_config(project)
    if not config:
        return

    # Resolve the Coder CLI timeout (Cortex C5). Precedence:
    # --coder-timeout flag > SENTINEL_CODER_TIMEOUT env > config > default.
    # Applied in-place on config.coder so the downstream Router picks it
    # up without extra plumbing — single code path, no second source.
    resolved_coder_timeout = _resolve_coder_timeout(
        cli_value=coder_timeout,
        env_value=os.environ.get("SENTINEL_CODER_TIMEOUT"),
        config_value=config.coder.timeout_seconds,
    )
    if resolved_coder_timeout != config.coder.timeout_seconds:
        console.print(
            f"  [dim]Coder CLI timeout: {resolved_coder_timeout}s "
            f"(was {config.coder.timeout_seconds}s in config)[/dim]"
        )
    config.coder.timeout_seconds = resolved_coder_timeout

    # Snapshot daily spend at cycle start so --budget enforces this run's
    # spend, not the daily running total. Without this, passing
    # `--budget $5` on a day already at $4 of spend blocks the cycle
    # before any work runs. Mirrors loop mode's session_spend_start.
    cycle_spend_start = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    ).today_spent_usd

    # Pre-flight: any role configured for the local (ollama) provider
    # needs its model already pulled. Surfacing this as an actionable
    # `ollama pull X` message is much friendlier than letting the first
    # provider call fail with a less obvious error from inside a phase.
    router_for_check = Router(config)
    missing = router_for_check.missing_local_models()
    if missing:
        console.print(
            "[red]  Missing local models — sentinel can't start until "
            "they're pulled:[/red]"
        )
        for role, model in missing:
            console.print(
                f"    {role}: [bold]ollama pull {model}[/bold]"
            )
        return

    # Pre-flight: clean up orphaned worktrees from prior crashed runs.
    # `git worktree add` fails if the target path exists, so a SIGKILL
    # partway through the prior cycle would block the next. One-line
    # note when anything was actually pruned; silent otherwise.
    from sentinel.worktree import cleanup_orphaned_worktrees
    orphans = cleanup_orphaned_worktrees(project)
    if orphans:
        console.print(
            f"  [dim]Pruned {orphans} orphaned worktree"
            f"{'s' if orphans != 1 else ''} from prior crashed runs[/dim]"
        )

    # Pre-flight: shipping requirements. Skipped in dry-run mode since
    # we never reach ship_pr. Checks gh auth, origin remote.
    # Codex flagged that `which gh` alone isn't enough — an installed-
    # but-unauthenticated gh surfaces as a cryptic error deep inside
    # the ship step. Better to fail fast with an actionable message.
    if not dry_run:
        ship_errors = _check_shipping_preflight(project)
        if ship_errors:
            console.print(
                "[red]  Shipping preflight failed — sentinel can't open "
                "PRs until these are resolved:[/red]"
            )
            for err in ship_errors:
                console.print(f"    {err}")
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
    # Reuse the router built for the pre-flight model check so we don't
    # initialize providers twice.
    router = router_for_check
    monitor = Monitor(router)
    coder = Coder(router)
    reviewer = Reviewer(router)
    original_branch = _current_branch(str(project))

    items_executed = 0
    items_approved = 0
    items_rejected = 0
    items_failed = 0

    # Cycle-scope accumulators for the cortex T1.6 hook. Populated as
    # phases complete so the finally-block write has the full picture
    # even if the cycle aborts mid-loop. (The cortex entry summarizes a
    # cycle regardless of whether it shipped work — per the plan, the
    # entry is the cycle's end-of-life record.)
    cortex_overall_score: int | None = None
    cortex_lens_scores: list[tuple[str, int]] = []
    cortex_refinement_count = 0
    cortex_expansion_count = 0

    try:
        while True:
            # Budget check
            budget_ok, reason = _check_all_budgets(
                project, config, money_budget, cycle_spend_start,
                start_time, time_budget_sec,
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
                # Capture for the cortex T1.6 entry. Prefer the live
                # scan over the on-disk one because the persisted file
                # stores lens scores in a form we'd have to reparse.
                cortex_overall_score = scan_result.overall_score
                cortex_lens_scores = [
                    (ev.lens_name, ev.score)
                    for ev in scan_result.evaluations
                    if not ev.error
                ]
                console.print(
                    f"  [green]✓[/green] Health: {scan_result.overall_score}/100 "
                    f"(${scan_result.total_cost_usd:.4f})\n"
                )
                _print_cycle_spend(
                    project, config, cycle_spend_start, money_budget,
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
                _write_backlog(project, actions, scan_file, config=config)
                # Write expansion proposals so user can approve later
                from sentinel.cli.plan_cmd import _write_proposals
                proposals = _write_proposals(project, actions, scan_file)

                refinements = [
                    a for a in actions
                    if a.get("kind", "refine") == "refine"
                ]
                expansions = [a for a in actions if a.get("kind") == "expand"]
                cortex_refinement_count = len(refinements)
                cortex_expansion_count = len(expansions)
                console.print(
                    f"  [green]✓[/green] {len(refinements)} refinements, "
                    f"{len(expansions)} expansion proposals"
                )
                journal.end_phase("plan")
                _print_cycle_spend(
                    project, config, cycle_spend_start, money_budget,
                )
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
                # Surface the resolved Coder timeout so --dry-run is a
                # useful configuration-probe: users (and tests) can
                # verify the flag/env/config resolution without actually
                # invoking the Claude CLI.
                console.print(
                    f"  [dim]Resolved coder timeout: "
                    f"{config.coder.timeout_seconds}s[/dim]"
                )
                console.print("[yellow]  Dry run — stopping[/yellow]")
                journal.exit_reason = "dry_run"
                break

            next_item = items[items_executed] if items_executed < len(items) else None
            if not next_item:
                console.print("[green]  All items processed.[/green]")
                journal.exit_reason = "all_items_processed"
                break

            # Execute + review + verify + ship
            from sentinel.journal import WorkItemRecord
            wi_id = str(next_item.get("id", items_executed + 1))
            wi_title = next_item.get("title", "(untitled)")
            phase_label = f"execute:{wi_id}"
            set_current_phase("execute")
            journal.start_phase(phase_label)
            (
                success, verification_verdict, ship_status, pr_url,
            ) = await _execute_and_review(
                next_item, items_executed + 1,
                project, original_branch,
                coder, reviewer, config,
                cycle_id=datetime.fromtimestamp(
                    journal.started_at,
                ).strftime("%Y-%m-%d-%H%M%S"),
            )
            journal.end_phase(phase_label, status=success or "unknown")
            _print_cycle_spend(
                project, config, cycle_spend_start, money_budget,
            )

            # Mirror the outcome into the work-items table so the journal
            # shows what we ran, not just timings.
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
                pr_url=pr_url,
                ship_status=ship_status,
            ))

            items_executed += 1
            bucket = _bucket_outcome(success)
            if bucket == "approved":
                items_approved += 1
            elif bucket == "rejected":
                items_rejected += 1
            else:
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
        # Freeze the end timestamp then write the journal once more.
        # mark_ended is idempotent and must happen BEFORE the final
        # write so the rendered Total time captures the actual cycle
        # duration rather than "right now."
        journal.mark_ended()
        try:
            journal_path = journal.write()
            console.print(
                f"  [dim]Run journal: "
                f"{journal_path.relative_to(project)}[/dim]"
            )
        except OSError as e:
            console.print(f"  [yellow]Could not write run journal: {e}[/yellow]")
            journal_path = None

        # --- Cortex T1.6 hook ---
        # Fired at the same moment `.sentinel/runs/<ts>.md` is
        # finalized (single source of truth for "cycle ended"). Writes
        # a conformant `.cortex/journal/<date>-sentinel-cycle-<id>.md`
        # entry when resolve_enabled() says yes. Failure is non-blocking
        # per the plan: a bad write logs to
        # `.sentinel/state/cortex-write-errors.jsonl`, prints a warning,
        # and the cycle exit code is unaffected.
        _emit_cortex_t16_entry(
            project, journal, journal_path,
            cli_flag=cortex_journal,
            config=config,
            overall_score=cortex_overall_score,
            lens_scores=cortex_lens_scores,
            refinement_count=cortex_refinement_count,
            expansion_count=cortex_expansion_count,
        )
        set_current_journal(None)

    # --- Summary ---
    elapsed = time.time() - start_time
    budget_now = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )
    console.print()

    failure_summary = _build_failure_summary(journal)
    if failure_summary:
        console.print(
            Panel(
                failure_summary,
                title="[bold red]Cycle failed[/bold red]",
                border_style="red",
            )
        )
    else:
        cycle_spend_line = _format_cycle_spend_line(
            budget_now.today_spent_usd, cycle_spend_start, money_budget,
        )
        panel_body = (
            f"Items executed: {items_executed}\n"
            f"  • Approved: {items_approved}\n"
            f"  • Rejected: {items_rejected}\n"
            f"  • Failed: {items_failed}\n\n"
            f"Time: {int(elapsed)}s\n"
            f"Spend today: ${budget_now.today_spent_usd:.4f} / "
            f"${budget_now.daily_limit_usd:.2f}"
        )
        if cycle_spend_line:
            panel_body += f"\n{cycle_spend_line}"
        console.print(
            Panel(
                panel_body,
                title="[bold]Done[/bold]",
                border_style="cyan",
            )
        )
    console.print()


def _build_failure_summary(journal) -> str:  # noqa: ANN001
    """Return a user-facing failure summary, or "" if the cycle did
    not fail. Pulls the failing phase + last erroring provider call
    from the journal and matches the error pattern to a suggested next
    action so the user doesn't have to grep the journal to know what
    to try next."""
    failed_phase = next(
        (p for p in reversed(journal.phases) if p.status == "failed"),
        None,
    )
    erroring_call = next(
        (c for c in reversed(journal.provider_calls) if c.error),
        None,
    )
    exit_reason = journal.exit_reason or ""

    is_failure = (
        failed_phase is not None
        or exit_reason.startswith(("scan_failed", "error:", "budget:"))
        or exit_reason in ("loop_ended_unexpectedly", "interrupted")
    )
    if not is_failure:
        return ""

    lines: list[str] = []
    if failed_phase:
        lines.append(f"Phase: [bold]{failed_phase.name}[/bold]")
        if failed_phase.error:
            lines.append(f"Reason: {failed_phase.error}")
    elif exit_reason:
        lines.append(f"Exit: {exit_reason}")

    if erroring_call:
        lines.append(
            f"Last call: {erroring_call.role or '?'} via "
            f"{erroring_call.provider}/{erroring_call.model} "
            f"({erroring_call.error})"
        )
        if erroring_call.routed_via:
            lines.append(
                f"Routing rule: [dim]{erroring_call.routed_via}[/dim]"
            )

    suggestion = _suggest_next_action(
        exit_reason, failed_phase, erroring_call,
    )
    if suggestion:
        lines += ["", f"Try: [bold]{suggestion}[/bold]"]

    return "\n".join(lines)


def _suggest_next_action(  # noqa: ANN001
    exit_reason: str, failed_phase, erroring_call,
) -> str:
    """Map a failure pattern to one suggested next action. Patterns
    are keyed off (a) journal.exit_reason and (b) the last erroring
    provider call's error classification — both are stable strings
    set by code we control."""
    if exit_reason.startswith("budget:"):
        return "increase --budget or check `sentinel cost`"
    if erroring_call and erroring_call.error == "budget_exhausted":
        return "increase --budget or set a money cap with --budget $N"
    if erroring_call and erroring_call.error == "timeout":
        return (
            "increase --budget for more time, or pin the model to a "
            "faster one in .sentinel/config.toml"
        )
    if erroring_call and erroring_call.error == "non-zero exit":
        return (
            "check `sentinel routing show` — the model that failed may "
            "warrant a new routing rule (DEFAULT_RULES in providers/router.py)"
        )
    if erroring_call and erroring_call.error == "cli is_error":
        return "check provider auth with `sentinel providers`"
    if failed_phase and failed_phase.name == "scan":
        return (
            "scan failure: check `.sentinel/scans/partial/` for any "
            "rescued lens evaluations"
        )
    return ""


def _format_cycle_spend_line(
    today_spent_usd: float,
    cycle_spend_start: float,
    money_budget: float | None,
) -> str | None:
    """Return a dim per-phase spend hint, or None if no money cap is set.

    Format: "Cycle spend: $X.XXXX / $Y.YY"

    Only meaningful (and only shown) when the user passed a money cap via
    --budget $X. Without a cap there's no reference point and the line
    would just add noise.
    """
    if money_budget is None:
        return None
    cycle_spent = today_spent_usd - cycle_spend_start
    return f"Cycle spend: ${cycle_spent:.4f} / ${money_budget:.2f}"


def _print_cycle_spend(
    project: Path,
    config: SentinelConfig,
    cycle_spend_start: float,
    money_budget: float | None,
) -> None:
    """Print a dim cycle spend hint if a money cap is active.

    Reads live spend from the budget journal so the number reflects any
    spend that occurred during the phase that just completed. No-ops when
    no money cap is set — avoids clutter for time-only or uncapped runs.
    """
    if money_budget is None:
        return
    status = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )
    line = _format_cycle_spend_line(
        status.today_spent_usd, cycle_spend_start, money_budget,
    )
    if line:
        console.print(f"  [dim]{line}[/dim]")


def _bucket_outcome(success: str | None) -> str:
    """Map _execute_and_review's `success` token to a summary bucket.

    Three buckets, exhaustive: an executed item lands in exactly one.
    - `approved` — reviewer approved, work shipped (or attempted to)
    - `rejected` — reviewer surfaced fixable issues; iteration loop ran
      to completion without earning approval (`changes` or `rejected`)
    - `failed`  — tooling broke (`failed`), or any unexpected token

    Distinguishing rejected from failed matters for operator signal: a
    rejection means coder quality / scope was the gate, a failure means
    the harness itself broke. Pre-fix the cycle summary lumped them so
    rejections silently disappeared from the totals.
    """
    if success == "approved":
        return "approved"
    if success in ("changes", "rejected"):
        return "rejected"
    return "failed"


def _check_all_budgets(
    project: Path,
    config: SentinelConfig,
    money_budget: float | None,
    cycle_spend_start: float,
    start_time: float,
    time_budget_sec: int | None,
) -> tuple[bool, str]:
    """Check all budget constraints. Returns (ok, reason_if_not).

    `cycle_spend_start` is the daily spend snapshotted at cycle start, so
    `--budget $X` enforces a per-run cap (this cycle's delta) rather than
    a second daily cap. Daily limit from config still applies separately.
    """
    # Daily money budget (from config)
    budget = check_budget(
        project, config.budget.daily_limit_usd, config.budget.warn_at_usd,
    )
    if budget.over_limit:
        return False, (
            f"daily budget reached "
            f"(${budget.today_spent_usd:.2f} / ${budget.daily_limit_usd:.2f})"
        )

    # Per-run money budget (from --budget flag) — compares this cycle's
    # spend, not the daily total. Mirrors loop mode's session_spend_start.
    if money_budget is not None:
        cycle_spent = budget.today_spent_usd - cycle_spend_start
        if cycle_spent >= money_budget:
            return False, (
                f"per-run budget reached "
                f"(${cycle_spent:.2f} / ${money_budget:.2f})"
            )

    # Per-work time budget
    if time_budget_sec is not None:
        elapsed = time.time() - start_time
        if elapsed >= time_budget_sec:
            mins = int(elapsed / 60)
            return False, f"time budget reached ({mins} min)"

    return True, ""


def _check_shipping_preflight(project: Path) -> list[str]:
    """Return actionable error messages for anything that would block
    PR shipping. Empty list means all preconditions are met.

    Only checks things the user can fix from the CLI: gh installation,
    gh authentication, and the existence of an `origin` remote. Branch
    protection is NOT a precondition — ship_pr degrades gracefully
    (creates the PR without arming auto-merge) when the base branch
    is unprotected.
    """
    import shutil

    errors: list[str] = []

    if not shutil.which("gh"):
        errors.append(
            "[bold]gh[/bold] not found on PATH. Install with: "
            "[bold]brew install gh[/bold]"
        )
        # If gh is missing, the auth check can't run — return early.
        return errors

    auth_result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if auth_result.returncode != 0:
        errors.append(
            "gh is not authenticated. Run: [bold]gh auth login[/bold]"
        )

    # Check origin remote — sentinel pushes to `origin` by convention.
    remote_result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, cwd=str(project),
        timeout=10, check=False,
    )
    if remote_result.returncode != 0:
        errors.append(
            "No [bold]origin[/bold] remote configured. Add one with: "
            "[bold]git remote add origin <url>[/bold]"
        )

    return errors


def _should_ship(review_verdict: str, verification_overall: str | None) -> bool:
    """Pure ship-gate predicate: True iff Sentinel should attempt to
    open a PR for this work item.

    - `approved` + `verified` → ship (the happy path)
    - `approved` + `no_check_defined` → ship (project has no test
      config; auto-merge still gated by branch protection in ship_pr)
    - `approved` + `not_verified` → block (a configured check
      actually FAILED)
    - any non-approved verdict → block
    """
    return (
        review_verdict == "approved"
        and verification_overall in ("verified", "no_check_defined")
    )


def _build_pr_body(
    work_item, action: dict, review, verification,  # noqa: ANN001
) -> str:
    """Compose the PR body — work item context, lens that flagged it,
    reviewer verdict, verification results. Goes through --body-file so
    length is unconstrained.
    """
    lines = [
        f"## What\n\n{work_item.description or work_item.title}\n",
        f"## Why\n\n{action.get('why', '(no rationale recorded)')}\n",
        f"**Lens:** `{action.get('lens', '(none)')}`",
        f"**Type:** {work_item.type} | **Priority:** {work_item.priority}",
        "",
        "## Reviewer verdict",
        "",
        f"- **Verdict:** {review.verdict}",
        f"- **Summary:** {review.summary or '(no summary)'}",
    ]
    if review.blocking_issues:
        lines += ["", "**Blocking issues:**"]
        lines += [f"- {issue}" for issue in review.blocking_issues]
    lines += [
        "",
        "## Verification (project's own checks)",
        "",
        f"- **Overall:** {verification.overall}",
    ]
    for c in verification.checks:
        lines.append(f"- `{c.name}`: {c.verdict} ({c.duration_s:.1f}s)")

    if verification.overall == "no_check_defined":
        lines += [
            "",
            "> ⚠️ **No automated verification was run.** This project "
            "has no `lint_command` or `test_command` configured in "
            "`.touchstone-config`, so sentinel could not independently "
            "verify the diff. The reviewer LLM still approved the "
            "change; please review carefully before merging.",
        ]

    lines += [
        "",
        "---",
        "",
        f"_Shipped by sentinel for work item `{work_item.id}`._",
    ]
    return "\n".join(lines)


MAX_CODER_ITERATIONS = 3


def _issue_set(issues: object) -> frozenset[str]:
    """Normalize reviewer blocking issues for no-progress comparison.

    Two reviews reporting the same findings in different order — or
    with incidental whitespace drift — are still the same findings for
    the purposes of deciding whether to keep iterating.

    Defensive to malformed reviewer output: provider JSON can drift
    (None instead of a list, non-string entries, nested nulls). Codex
    review of PR #63 flagged that strict typing here would crash
    iteration on untrusted LLM output. Accept anything iterable, keep
    only stripped non-empty strings.
    """
    if not issues:
        return frozenset()
    try:
        iterator = iter(issues)  # type: ignore[call-overload]
    except TypeError:
        return frozenset()
    out: set[str] = set()
    for item in iterator:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                out.add(stripped)
    return frozenset(out)


async def _iterate_coder_reviewer(
    *,
    work_item,
    exec_result,
    review,
    coder: Coder,
    reviewer: Reviewer,
    project: Path,
    ctx,
) -> tuple[object, object, int]:
    """Loop coder.execute → reviewer.review until approved, stuck, or capped.

    The initial (exec_result, review) pair is passed in — the first
    coder+reviewer call happens at the top of `_execute_and_review`.
    This helper picks up from there and only iterates when the verdict
    is not `approved`.

    Termination conditions (in priority order):
      1. `review.verdict == 'approved'` → ship gate takes over
      2. `iterations >= MAX_CODER_ITERATIONS` → cap hit, surface final verdict
      3. No progress: reviewer returns the same blocking_issues set two
         rounds in a row → stop burning budget on a coder that isn't
         responding to feedback
      4. `exec_result.status == 'failed'` mid-loop → surface the error,
         bail out of iteration (the outer ship gate will handle it)

    Returns `(exec_result, review, iterations_used)`. `iterations_used`
    starts at 1 (counting the initial pass) so the caller can report
    "approved on iteration N of M".
    """
    iterations = 1
    prior_issues: frozenset[str] | None = None

    # If the initial review was a reviewer-infrastructure failure (not
    # a real verdict on the code), do not iterate — there are no
    # findings for the coder to address. Codex review of PR #63 caught
    # this: otherwise a "reviewer crashed" outcome would trigger
    # expensive coder passes against nonexistent feedback.
    if getattr(review, "infrastructure_failure", False):
        console.print(
            "  [yellow]Reviewer infrastructure failure — not "
            "iterating[/yellow]"
        )
        return exec_result, review, iterations

    while (
        review.verdict != "approved"
        and iterations < MAX_CODER_ITERATIONS
    ):
        current = _issue_set(review.blocking_issues)
        # Stop if the coder couldn't shift the findings since last round.
        # Check only once we've completed a revise→review cycle (i.e.
        # we have a prior set to compare against).
        if prior_issues is not None and current == prior_issues:
            console.print(
                "  [yellow]No progress on findings — stopping "
                "iteration[/yellow]"
            )
            break
        prior_issues = current
        iterations += 1

        console.print(
            f"  [dim]revising (iteration {iterations}/"
            f"{MAX_CODER_ITERATIONS})...[/dim]"
        )
        exec_result = await coder.execute(
            work_item,
            working_directory=str(ctx.path),
            artifacts_directory=str(project),
            branch=ctx.branch,
            review_feedback=review,
        )
        if exec_result.cost_usd > 0:
            record_spend(
                project, exec_result.cost_usd, "work-execute",
                f"revise={work_item.title[:40]}",
            )
        if exec_result.status == "failed":
            console.print(
                f"  [red]✗ Revise failed:[/red] {exec_result.error}"
            )
            break
        console.print(
            f"  [green]✓ Revised[/green] — "
            f"{len(exec_result.files_changed)} files, "
            f"tests: {'pass' if exec_result.tests_passing else 'FAIL'}"
        )

        console.print("  [dim]re-reviewing...[/dim]")
        review = await reviewer.review(
            work_item, exec_result, str(project),
            working_directory=str(ctx.path),
        )
        if review.cost_usd > 0:
            record_spend(
                project, review.cost_usd, "work-review",
                f"revise={work_item.title[:40]}",
            )
        verdict_color = {
            "approved": "green",
            "changes-requested": "yellow",
            "rejected": "red",
        }[review.verdict]
        console.print(
            f"  [{verdict_color}]Review: {review.verdict}"
            f"[/{verdict_color}] [dim](iteration {iterations})[/dim]"
        )
        if review.verdict != "approved" and review.blocking_issues:
            for issue in review.blocking_issues[:2]:
                console.print(f"    • {issue}")

        # Mid-loop reviewer infrastructure failure — same rule as at
        # entry: stop iterating, don't burn another coder pass against
        # a non-verdict.
        if getattr(review, "infrastructure_failure", False):
            console.print(
                "  [yellow]Reviewer infrastructure failure mid-loop — "
                "stopping iteration[/yellow]"
            )
            break

    return exec_result, review, iterations


async def _execute_and_review(
    action: dict,
    index: int,
    project: Path,
    original_branch: str,
    coder: Coder,
    reviewer: Reviewer,
    config: SentinelConfig,
    *,
    cycle_id: str = "",
) -> tuple[str, str | None, str, str]:
    """Execute one work item in its own worktree, review it, verify
    against project checks, and ship a PR if both gates pass.

    Returns (verdict, verification_overall, ship_status, pr_url) where:
      - verdict: 'approved' | 'changes' | 'rejected' | 'failed'
      - verification_overall: 'verified' | 'not_verified' |
        'no_check_defined' | None
      - ship_status: '' (not attempted), 'merged_armed', 'created',
        'existed', 'failed'
      - pr_url: GitHub URL when shipped, '' otherwise

    The user's main checkout is NEVER touched — all work happens in a
    `.sentinel/worktrees/wi-<id>` worktree that's cleaned up on exit
    (success, exception, or interrupt).
    """
    from sentinel.git_ops import slug
    from sentinel.pr import ship_pr
    from sentinel.verify import persist_verification, verify_work_item
    from sentinel.worktree import worktree_for

    work_item = _action_to_work_item(action, index)
    console.print(f"[bold cyan]→ Executing[/bold cyan] {work_item.title}")
    console.print(f"  [dim]lens: {action.get('lens', '')}[/dim]")

    branch = f"sentinel/wi-{work_item.id}-{slug(work_item.title)}"
    wi_slug = f"wi-{work_item.id}"

    async with worktree_for(
        project, branch=branch, base=original_branch, slug=wi_slug,
    ) as ctx:
        t0 = time.time()
        exec_result = await coder.execute(
            work_item,
            working_directory=str(ctx.path),
            artifacts_directory=str(project),
            branch=ctx.branch,
        )

        if exec_result.cost_usd > 0:
            record_spend(
                project, exec_result.cost_usd, "work-execute",
                f"item={work_item.title[:40]}",
            )

        elapsed = time.time() - t0
        if exec_result.status == "failed":
            console.print(f"  [red]✗ Execute failed:[/red] {exec_result.error}")
            return "failed", None, "", ""

        console.print(
            f"  [green]✓ Coded[/green] in {elapsed:.0f}s — "
            f"{len(exec_result.files_changed)} files, "
            f"tests: {'pass' if exec_result.tests_passing else 'FAIL'}"
        )

        # Review against the worktree's diff
        console.print("  [dim]reviewing...[/dim]")
        review = await reviewer.review(
            work_item, exec_result, str(project),
            working_directory=str(ctx.path),
        )
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
            f"[dim]→ branch: {ctx.branch}[/dim]"
        )
        if review.blocking_issues:
            for issue in review.blocking_issues[:2]:
                console.print(f"    • {issue}")

        # Iteration loop: if the reviewer rejected or requested
        # changes, feed the blocking issues back to the coder and try
        # again. Without this the coder's partial work rots on an
        # unmerged branch — sentinel was identifying impactful work
        # but never shipping it. Dogfood on portfolio_new (2026-04-17)
        # showed 2/2 items rejected for fixable coder-quality issues
        # (invalid CSS syntax, incomplete scope); both were exactly
        # what an iteration loop can resolve.
        if review.verdict != "approved":
            exec_result, review, iterations_used = await _iterate_coder_reviewer(
                work_item=work_item,
                exec_result=exec_result,
                review=review,
                coder=coder,
                reviewer=reviewer,
                project=project,
                ctx=ctx,
            )
            if iterations_used > 1:
                console.print(
                    f"  [dim]coder iterations: {iterations_used}/"
                    f"{MAX_CODER_ITERATIONS}[/dim]"
                )
        console.print()

        # Verify in the worktree (where the diff lives), persist to
        # main project (where artifacts survive).
        verification = verify_work_item(
            project_path=project,
            work_item_id=str(work_item.id),
            work_item_title=work_item.title,
            branch=ctx.branch,
            working_directory=ctx.path,
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

        # Ship gate: reviewer.approved AND verifier ∈ {verified,
        # no_check_defined}. The original codex-flagged risk was
        # "no_check_defined ≠ tested PR quality" — true, but the
        # alternative is refusing to ship on any project that hasn't
        # configured tests, which is most projects on first-touch
        # (portfolio_new dogfood 2026-04-16 hit exactly this and
        # blocked all 5 candidate items). Reconciliation:
        #   - `verified` and `no_check_defined` both ship the PR
        #   - `not_verified` (a check failed) blocks
        #   - The PR body explicitly notes when verification was
        #     undefined so reviewers see the gap, and ship_pr only
        #     arms auto-merge when the base branch has required
        #     checks (so unprotected repos won't autoship anyway —
        #     the PR sits open for human review)
        ship_status = ""
        pr_url = ""
        ship_ready = _should_ship(review.verdict, verification.overall)
        if ship_ready and exec_result.commit_sha:
            ship = await ship_pr(
                worktree_path=ctx.path,
                project_path=project,
                branch=ctx.branch,
                base=ctx.base,
                head_sha=exec_result.commit_sha,
                title=f"sentinel: {work_item.title[:72]}",
                body_md=_build_pr_body(
                    work_item, action, review, verification,
                ),
            )
            ship_status = ship.status
            pr_url = ship.pr_url
            if ship.status in ("merged_armed", "created", "existed"):
                console.print(
                    f"  [green]→ PR ({ship.status}):[/green] {ship.pr_url}"
                )
                if ship.error:
                    console.print(f"    [yellow]{ship.error}[/yellow]")
            else:
                console.print(
                    f"  [red]✗ Ship failed:[/red] {ship.error}"
                )
        elif (
            review.verdict == "approved"
            and verification.overall == "not_verified"
        ):
            # Reviewer approved but a configured check actually
            # FAILED — that's a real gate failure (different from
            # no_check_defined which means we just couldn't tell).
            # Branch stays for human inspection; no PR opened.
            console.print(
                f"  [yellow]Approved by reviewer but verification "
                f"FAILED — branch left at {ctx.branch}, no PR "
                f"opened. Check `.sentinel/verifications.jsonl` for "
                f"which check failed.[/yellow]"
            )
        console.print()

    if review.verdict == "approved":
        return "approved", verification.overall, ship_status, pr_url

    # Non-approved verdict — memorialize it so the next cycle's
    # planner doesn't regenerate the same item. See
    # ``sentinel.integrations.rejections`` for the 30-day TTL behavior
    # and escape hatch. Suppressed on infrastructure failures (the
    # verdict isn't a verdict on the code, it's "reviewer crashed")
    # because persisting those would cost us a real item forever.
    if not getattr(review, "infrastructure_failure", False):
        verdict_token = (
            "changes_requested"
            if review.verdict == "changes-requested"
            else "rejected"
        )
        try:
            from sentinel.integrations.rejections import record_rejection
            record_rejection(
                project,
                cycle_id=cycle_id,
                work_item={
                    "title": action.get("title", work_item.title),
                    "lens": action.get("lens", ""),
                    "why": action.get("why", ""),
                    "impact": action.get("impact", ""),
                    "files": action.get("files", []),
                },
                review_verdict=verdict_token,
                reviewer_reason=review.summary or (
                    "; ".join(review.blocking_issues[:3])
                    if review.blocking_issues else ""
                ),
            )
        except Exception as exc:  # noqa: BLE001 — never block on memory
            # Loud but nonblocking; the reviewer verdict already
            # stands, and the rejection log is a best-effort guard.
            console.print(
                f"  [yellow]Could not record rejection for "
                f"planner memory: {exc}[/yellow]"
            )

    if review.verdict == "changes-requested":
        return "changes", verification.overall, ship_status, pr_url
    return "rejected", verification.overall, ship_status, pr_url


async def _run_loop(
    project_path: str | None,
    budget_str: str | None,
    dry_run: bool,
    auto: bool,
    every: str,
    *,
    cortex_journal: bool | None = None,
    coder_timeout: int | None = None,
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
                cortex_journal=cortex_journal,
                coder_timeout=coder_timeout,
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
