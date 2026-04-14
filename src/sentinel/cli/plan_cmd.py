"""
sentinel plan — turn the most recent scan into a prioritized backlog.

Reads the most recent scan from .sentinel/scans/, extracts top actions,
writes them to .sentinel/backlog.md (source of truth), and optionally
syncs to GitHub issues via `gh` CLI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()


def _find_latest_scan(project_path: Path) -> Path | None:
    """Find the most recent scan file in .sentinel/scans/."""
    scans_dir = project_path / ".sentinel" / "scans"
    if not scans_dir.exists():
        return None
    scans = sorted(scans_dir.glob("*.md"), reverse=True)
    return scans[0] if scans else None


def _parse_actions_from_scan(scan_file: Path) -> list[dict]:
    """Extract 'Top Actions' from a scan markdown file."""
    content = scan_file.read_text()
    actions = []

    in_actions = False
    current: dict = {}
    for line in content.splitlines():
        if line.strip() == "## Top Actions":
            in_actions = True
            continue
        if in_actions and line.startswith("## "):
            # End of Top Actions section
            if current:
                actions.append(current)
                current = {}
            break
        if not in_actions:
            continue

        # Parse "### 1. Title"
        if line.startswith("### ") and ". " in line:
            if current:
                actions.append(current)
            title = line.split(". ", 1)[1].strip()
            current = {
                "title": title, "why": "", "impact": "",
                "lens": "", "files": [], "kind": "refine",
            }
        elif line.startswith("**Kind:**"):
            current["kind"] = line.replace("**Kind:**", "").strip()
        elif line.startswith("**Lens:**"):
            current["lens"] = line.replace("**Lens:**", "").strip()
        elif line.startswith("**Why:**"):
            current["why"] = line.replace("**Why:**", "").strip()
        elif line.startswith("**Impact:**"):
            current["impact"] = line.replace("**Impact:**", "").strip()
        elif line.startswith("**Files:**"):
            files_str = line.replace("**Files:**", "").strip()
            current["files"] = [f.strip() for f in files_str.split(",") if f.strip()]

    if current and in_actions:
        actions.append(current)

    return actions


def _write_backlog(project_path: Path, actions: list[dict], scan_file: Path) -> Path:
    """Write refinement items to .sentinel/backlog.md (the autonomous work queue)."""
    backlog_path = project_path / ".sentinel" / "backlog.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    scan_name = scan_file.name

    refinements = [a for a in actions if a.get("kind", "refine") == "refine"]

    lines = [
        "# Sentinel Backlog — Refinements",
        "",
        f"*Generated {timestamp} from {scan_name}*",
        "",
        "Items sentinel can execute autonomously. Edit freely — sentinel will",
        "preserve your changes on next `plan`.",
        "",
        "---",
        "",
    ]

    for i, action in enumerate(refinements, 1):
        lines.append(f"## [{i}] {action['title']}")
        lines.append("")
        lines.append("**Status:** todo")
        lines.append(f"**Lens:** {action.get('lens', '')}")
        lines.append(f"**Impact:** {action.get('impact', '')}")
        lines.append("")
        lines.append(f"{action.get('why', '')}")
        lines.append("")
        if action.get("files"):
            lines.append(f"**Files:** {', '.join(action['files'])}")
            lines.append("")
        lines.append("---")
        lines.append("")

    backlog_path.write_text("\n".join(lines))
    return backlog_path


def _slug(title: str) -> str:
    """Short slug from a title for filenames."""
    import re
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40]


def _write_proposals(
    project_path: Path, actions: list[dict], scan_file: Path,
) -> list[Path]:
    """Write expansion items as individual proposals awaiting user approval."""
    proposals_dir = project_path / ".sentinel" / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d")

    expansions = [a for a in actions if a.get("kind") == "expand"]
    written = []

    for action in expansions:
        slug = _slug(action["title"])
        fname = f"{timestamp}-{slug}.md"
        path = proposals_dir / fname

        # Don't overwrite existing proposals (preserves user edits to status)
        if path.exists():
            continue

        body = [
            f"# Proposal: {action['title']}",
            "",
            "**Status:** pending",
            "",
            "> Change **Status** to `approved` for sentinel to execute this in the next cycle.",
            "> Change to `rejected` to mark it handled without executing.",
            "> Leave as `pending` to think about it later — sentinel won't re-propose it.",
            "",
            f"**Lens:** {action.get('lens', '')}",
            f"**Impact:** {action.get('impact', '')}",
            f"**Source scan:** `{scan_file.name}`",
            "",
            "## Why",
            "",
            action.get("why", ""),
            "",
        ]
        if action.get("files"):
            body += [
                "## Files likely to be touched",
                "",
                "\n".join(f"- {f}" for f in action["files"]),
                "",
            ]
        body += [
            "## Notes",
            "",
            "*Add your thoughts, questions, or constraints here. Sentinel will",
            "read this file before executing.*",
            "",
        ]

        path.write_text("\n".join(body))
        written.append(path)

    return written


def _sync_github(project_path: Path, actions: list[dict]) -> int:
    """Create GitHub issues for each action via gh CLI. Returns count created."""
    if not shutil.which("gh"):
        console.print("[yellow]  gh CLI not found — skipping GitHub sync[/yellow]")
        return 0

    created = 0
    for action in actions:
        title = action["title"]
        body_lines = [
            f"**Lens:** {action.get('lens', '')}",
            f"**Impact:** {action.get('impact', '')}",
            "",
            action.get("why", ""),
        ]
        if action.get("files"):
            body_lines.append("")
            body_lines.append(f"**Files:** {', '.join(action['files'])}")
        body_lines.append("")
        body_lines.append("_Created by sentinel plan_")
        body = "\n".join(body_lines)

        try:
            subprocess.run(
                ["gh", "issue", "create", "--title", title, "--body", body,
                 "--label", "sentinel"],
                capture_output=True, text=True, cwd=project_path, timeout=30, check=True,
            )
            created += 1
            console.print(f"  [green]✓[/green] Created issue: {title}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            console.print(f"  [red]✗[/red] Failed to create issue '{title}': {e}")

    return created


async def run_plan(project_path: str | None = None, sync_github: bool = False) -> None:
    """Run the plan command."""
    project = Path(project_path or os.getcwd()).resolve()

    console.print(f"\n[bold]Sentinel Plan[/bold] — {project.name}\n")

    scan_file = _find_latest_scan(project)
    if not scan_file:
        console.print(
            "[red]No scans found in .sentinel/scans/. Run `sentinel scan` first.[/red]"
        )
        return

    console.print(f"  Reading {scan_file.relative_to(project)}...")
    actions = _parse_actions_from_scan(scan_file)

    if not actions:
        console.print(
            "[yellow]No Top Actions section found in scan. "
            "Scan may have failed — check the scan file.[/yellow]"
        )
        return

    refinements = [a for a in actions if a.get("kind", "refine") == "refine"]
    expansions = [a for a in actions if a.get("kind") == "expand"]
    console.print(
        f"  [green]✓[/green] Extracted {len(actions)} work items "
        f"[dim]({len(refinements)} refine, {len(expansions)} expand)[/dim]"
    )

    # Write backlog.md (refinements only — autonomously executable)
    backlog_path = _write_backlog(project, actions, scan_file)
    console.print(
        f"  [green]✓[/green] Wrote {backlog_path.relative_to(project)} "
        f"[dim]({len(refinements)} refinements)[/dim]"
    )

    # Write proposals (expansions — require user approval)
    proposal_paths = _write_proposals(project, actions, scan_file)
    if proposal_paths:
        console.print(
            f"  [yellow]✓[/yellow] Wrote {len(proposal_paths)} expansion proposals "
            f"[dim](.sentinel/proposals/)[/dim]"
        )

    # Optionally sync to GitHub (refinements only — proposals are private to you)
    if sync_github and refinements:
        console.print()
        console.print("[bold]Syncing refinements to GitHub Issues...[/bold]")
        created = _sync_github(project, refinements)
        console.print(f"  Created {created}/{len(refinements)} issues")

    console.print()
    if refinements:
        console.print("[bold green]Refinements[/bold green] [dim](auto-executable)[/dim]")
        for i, a in enumerate(refinements, 1):
            console.print(
                f"  [bold]{i}.[/bold] {a['title']} "
                f"[dim]({a.get('lens', '')})[/dim]"
            )
    if expansions:
        console.print()
        console.print(
            "[bold yellow]Expansions[/bold yellow] "
            "[dim](review .sentinel/proposals/ and flip status to approved)[/dim]"
        )
        for i, a in enumerate(expansions, 1):
            console.print(
                f"  [bold]{i}.[/bold] {a['title']} "
                f"[dim]({a.get('lens', '')})[/dim]"
            )
    console.print()
