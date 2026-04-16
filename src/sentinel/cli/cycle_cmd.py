"""
sentinel cycle — legacy alias for `sentinel work`.

The original `cycle` command had its own scan/plan/execute loop using
the legacy in-place Coder mode. That mode is gone now (the worktree-
managed PR factory is the only execution path), so `cycle` is a thin
shim that delegates to `sentinel.cli.work_cmd.run_work`.

A few helper functions in this module — `_action_to_work_item`,
`_current_branch`, `_load_approved_proposals` — are still imported
by `work_cmd` and stay; the rest of the original module is gone.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from rich.console import Console

from sentinel.roles.planner import WorkItem

if TYPE_CHECKING:
    from pathlib import Path

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


def _load_approved_proposals(project_path: Path) -> list[dict]:
    """Read .sentinel/proposals/*.md and return approved ones as actions.

    Proposals are approved by the user editing the file and changing
    'Status: pending' to 'Status: approved'.
    """
    import re

    proposals_dir = project_path / ".sentinel" / "proposals"
    if not proposals_dir.exists():
        return []

    approved: list[dict] = []
    for path in sorted(proposals_dir.glob("*.md")):
        content = path.read_text()
        status_match = re.search(
            r"^\*\*Status:\*\*\s*(\w+)", content, re.MULTILINE,
        )
        if not status_match or status_match.group(1).lower() != "approved":
            continue

        # Parse the rest
        title_match = re.search(r"^# Proposal:\s*(.+)$", content, re.MULTILINE)
        lens_match = re.search(r"^\*\*Lens:\*\*\s*(.+)$", content, re.MULTILINE)
        impact_match = re.search(r"^\*\*Impact:\*\*\s*(.+)$", content, re.MULTILINE)
        why_match = re.search(
            r"## Why\s*\n\s*\n(.+?)(?=\n##|\Z)", content, re.DOTALL,
        )
        files_match = re.search(
            r"## Files likely to be touched\s*\n\s*\n((?:- .+\n?)+)", content,
        )
        files = []
        if files_match:
            files = [
                line[2:].strip() for line in files_match.group(1).splitlines()
                if line.startswith("- ")
            ]

        approved.append({
            "title": title_match.group(1).strip() if title_match else path.stem,
            "lens": lens_match.group(1).strip() if lens_match else "",
            "impact": impact_match.group(1).strip() if impact_match else "",
            "why": why_match.group(1).strip() if why_match else "",
            "files": files,
            "kind": "expand",
            "proposal_path": str(path),
        })

    return approved


async def run_cycle(
    project_path: str | None = None,
    max_items: int = 3,
    dry_run: bool = False,
) -> None:
    """Deprecated alias — delegates to `sentinel work`.

    The original `cycle_cmd` had its own scan/plan/execute loop using
    the legacy in-place Coder mode. That mode is gone (worktree-managed
    is the only path). Rather than re-implement the whole loop here,
    `cycle` now redirects through `work_cmd.run_work` — single code
    path, no drift.

    `work` iterates the full backlog until budget exhaustion or
    completion; it has no equivalent of `--max-items`. If the caller
    passed a non-default `max_items`, refuse explicitly rather than
    silently ignoring the cap — pretending to cap at N items while
    actually executing the whole backlog is the worst kind of API
    surprise.
    """
    if max_items != 3:
        import click as _click
        console.print(
            f"[red]  `sentinel cycle --max-items {max_items}` is no longer "
            f"supported.[/red]\n"
            f"  The legacy item-cap was tied to the in-place Coder mode that "
            f"has been removed.\n"
            f"  Use `[bold]sentinel work --budget <time-or-money>[/bold]` "
            f"to bound the cycle instead — `work` iterates the full "
            f"backlog until the budget is hit.\n"
        )
        # Non-zero exit so CI/scripts notice the rejection. Just
        # printing and returning would silently succeed (exit 0)
        # despite doing zero work — codex-flagged failure mode.
        raise _click.exceptions.Exit(code=1)

    from sentinel.cli.work_cmd import run_work

    console.print(
        "[dim]  `sentinel cycle` is a legacy alias — delegating to "
        "`sentinel work`.[/dim]\n"
    )
    await run_work(
        project_path=project_path, dry_run=dry_run, auto=True,
    )
