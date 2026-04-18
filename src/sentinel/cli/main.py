"""Sentinel CLI — the command-line interface for the meta-agent."""

from __future__ import annotations

import asyncio
import sys

import click

from sentinel import __version__

NOT_YET = "[sentinel] Not yet implemented. Coming soon."


@click.group()
@click.version_option(version=__version__)
def main() -> None:
    """Autonomous meta-agent for managing software projects.

    The one command you need:

        sentinel work             — do whatever the project needs
        sentinel work --budget 10m   — time-bounded
        sentinel work --budget $5    — money-bounded
        sentinel work --budget 10m,$5  — time AND money bounded
        sentinel work --dry-run      — plan without executing

    Advanced (for when you want granular control):

        sentinel status           — quick health check
        sentinel scan             — full scan only
        sentinel plan             — turn scan into backlog
        sentinel cycle            — alias for `work`
        sentinel cost             — spend history
        sentinel providers        — LLM provider status
        sentinel init             — first-run interactive setup
    """


# --- PRIMARY COMMAND ---

@main.command()
@click.option(
    "--budget", "-b",
    default=None,
    help=(
        "Per-run cap on this cycle's spend (single mode) or session spend "
        "(loop mode). Money ($5, 10.50), time (10m, 1h, 30s), or both "
        "comma-separated (10m,$5). Independent of daily_limit_usd in "
        "config.toml — daily cap still applies. For free providers "
        "(Gemini OAuth, Ollama) the money cap is naturally a no-op."
    ),
)
@click.option(
    "--every", "-e",
    default=None,
    help="Loop mode — run continuously, sleep between cycles (e.g. 10m, 1h).",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Scan and plan but stop before execution.",
)
@click.option(
    "--auto", is_flag=True,
    help="Skip the confirmation prompt before executing.",
)
@click.option(
    "--cortex-journal/--no-cortex-journal",
    "cortex_journal",
    default=None,
    help=(
        "Force on/off the Cortex T1.6 integration for this cycle — "
        "write (or skip) a `.cortex/journal/<date>-sentinel-cycle-<id>.md` "
        "entry at cycle end. Overrides `.sentinel/config.toml`'s "
        "`[integrations.cortex] enabled`. Default (no flag): auto-detect "
        "by `.cortex/` presence."
    ),
)
def work(
    budget: str | None,
    every: str | None,
    dry_run: bool,
    auto: bool,
    cortex_journal: bool | None,
) -> None:
    """Work on this project. One cycle, or loop with --every.

    Examples:
      sentinel work                      one cycle and exit
      sentinel work --every 10m          loop, 10 min between cycles
      sentinel work --every 1h -b $20    loop with $20 session cap
    """
    from sentinel.cli.work_cmd import run_work

    asyncio.run(
        run_work(
            budget_str=budget, dry_run=dry_run, auto=auto, every=every,
            cortex_journal=cortex_journal,
        ),
    )


@main.command()
def status() -> None:
    """Quick project health check (state + spend + latest cycle)."""
    from sentinel.cli.status_cmd import run_status

    run_status()


# --- ADVANCED / GRANULAR ---

@main.command()
@click.option("--yes", "-y", is_flag=True, help="Skip all prompts, use defaults.")
@click.option(
    "--preset",
    type=click.Choice(
        ["recommended", "simple", "cheap", "local", "hybrid", "power"],
        case_sensitive=False,
    ),
    default=None,
    help="Use a named preset instead of interactive questions.",
)
@click.option(
    "--providers",
    default=None,
    help=(
        "Comma-separated list of provider CLIs to enable "
        "(e.g. claude,codex,gemini,ollama). Skips the wizard's providers "
        "multi-select step."
    ),
)
@click.option(
    "--coder",
    default=None,
    help=(
        "Coder provider[:model], e.g. `claude` or `claude:claude-sonnet-4-6`. "
        "Skips the wizard's coder prompts."
    ),
)
@click.option(
    "--reviewer",
    default=None,
    help=(
        "Reviewer provider[:model], e.g. `codex` or `codex:gpt-5.4`. "
        "Skips the wizard's reviewer prompts."
    ),
)
@click.option(
    "--budget",
    type=float,
    default=None,
    help="Daily budget cap in USD. Default 15.0.",
)
@click.option(
    "--scan/--no-scan",
    "run_scan",
    default=None,
    help="Run an initial scan after init. Default off.",
)
def init(
    yes: bool,
    preset: str | None,
    providers: str | None,
    coder: str | None,
    reviewer: str | None,
    budget: float | None,
    run_scan: bool | None,
) -> None:
    """Initialize Sentinel in the current project (first-run entry point).

    Interactive on a TTY — walks you through providers, coder/reviewer
    selection, budget, and an optional first scan (Doctrine 0002).

    Flags override individual prompts; --yes skips the whole wizard with
    defaults. The wizard prints its equivalent flag-form at the end so
    the same config can be reproduced in CI.

    Presets (skip all prompts):
      recommended  — smart defaults per role (the default)
      simple       — use claude for everything
      cheap        — prefer local / gemini-flash where possible
      local        — Ollama for cold path, claude/codex for agentic coder
      hybrid       — local Monitor + cloud everything else (best $/quality)
      power        — highest-quality model per role (expensive)
    """
    from sentinel.cli.init_cmd import run_init

    run_init(
        auto_yes=yes, preset=preset,
        providers=providers, coder=coder, reviewer=reviewer,
        budget=budget, run_scan=run_scan,
    )


@main.command(hidden=True)
@click.option(
    "--quick", "-q", is_flag=True,
    help="Quick mode — state summary only, no LLM call",
)
def scan(quick: bool) -> None:
    """Run just the scan phase (explore → lenses → evaluate → synthesize)."""
    from sentinel.cli.scan_cmd import run_scan

    asyncio.run(run_scan(quick=quick))


@main.command(hidden=True)
@click.option(
    "--sync-github", is_flag=True,
    help="Also create GitHub issues via gh CLI",
)
def plan(sync_github: bool) -> None:
    """Turn the most recent scan into a prioritized backlog."""
    from sentinel.cli.plan_cmd import run_plan

    asyncio.run(run_plan(sync_github=sync_github))


@main.command(hidden=True)
@click.option(
    "--max-items", "-n", type=int, default=3,
    help="Max work items to execute in this cycle",
)
@click.option("--dry-run", is_flag=True, help="Scan and plan but stop before execution")
def cycle(max_items: int, dry_run: bool) -> None:
    """Alias for `work --max-items N`. Kept for compatibility."""
    from sentinel.cli.cycle_cmd import run_cycle

    asyncio.run(run_cycle(max_items=max_items, dry_run=dry_run))


@main.command()
@click.option(
    "--by-role", is_flag=True,
    help="Aggregate spend by role (monitor/researcher/coder/reviewer) "
         "across recent cycles instead of by date.",
)
@click.option(
    "--cycles", "-n", type=click.IntRange(min=1), default=20,
    help="How many recent cycles to aggregate when --by-role is set "
         "(default 20). Must be >= 1.",
)
def cost(by_role: bool, cycles: int) -> None:
    """Show spend history and budget status."""
    from sentinel.cli.cost_cmd import run_cost

    run_cost(by_role=by_role, cycles=cycles)


@main.group(invoke_without_command=False)
def routing() -> None:
    """Inspect and tune router decisions."""


@routing.command("show")
@click.option(
    "--limit", "-n", type=click.IntRange(min=1), default=10,
    help="How many recent cycles to scan (default 10). Must be >= 1.",
)
def routing_show(limit: int) -> None:
    """Show routing overrides from recent run journals."""
    from sentinel.cli.routing_cmd import run_routing_show

    run_routing_show(limit=limit)


@main.command()
def providers() -> None:
    """Show LLM provider detection and health."""
    from sentinel.cli.providers_cmd import run_providers

    run_providers()


@main.command("config", hidden=True)
def config_cmd() -> None:
    """View or update role configuration."""
    click.echo(f"{NOT_YET}")
    click.echo("  Edit .sentinel/config.toml directly.")
    sys.exit(1)
