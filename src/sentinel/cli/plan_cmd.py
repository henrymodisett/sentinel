"""
sentinel plan — turn the most recent scan into a prioritized backlog.

Reads the most recent scan from .sentinel/scans/, extracts top actions,
writes them to .sentinel/backlog.md (source of truth), and optionally
syncs to GitHub issues via `gh` CLI.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()

# Compiled regexes used by _parse_actions_from_scan.
# Placed at module level so they are compiled once (not per call) and satisfy
# the N806 "variable in function should be lowercase" lint rule.
_FILE_BULLET_RE = re.compile(r"^-\s*`([^`]+)`\s*(?:—\s*(.*))?$")
_NUMBERED_ITEM_RE = re.compile(r"^\d+\.\s+(.+)$")
_BULLET_ITEM_RE = re.compile(r"^-\s+`?(.+?)`?$")


def _find_latest_scan(project_path: Path) -> Path | None:
    """Find the most recent scan file in .sentinel/scans/."""
    scans_dir = project_path / ".sentinel" / "scans"
    if not scans_dir.exists():
        return None
    scans = sorted(scans_dir.glob("*.md"), reverse=True)
    return scans[0] if scans else None


def _parse_actions_from_scan(scan_file: Path) -> list[dict]:
    """Extract 'Top Actions' from a scan markdown file.

    Handles both the new shape (files as list of {path, rationale} dicts,
    plus acceptance_criteria / verification / out_of_scope sections) and the
    legacy flat shape (**Files:** a, b, c on a single line) so older scans on
    disk still load without error.
    """
    content = scan_file.read_text()
    actions = []

    in_actions = False
    current: dict = {}
    # Track which multi-line list section we're currently accumulating into.
    # Values: "files" | "acceptance_criteria" | "verification" | "out_of_scope" | None
    list_mode: str | None = None

    def _flush_current() -> None:
        """Append current action to actions if it has content."""
        if current:
            actions.append(dict(current))

    for line in content.splitlines():
        if line.strip() == "## Top Actions":
            in_actions = True
            continue
        if in_actions and line.startswith("## "):
            # End of Top Actions section
            _flush_current()
            current = {}
            list_mode = None
            break
        if not in_actions:
            continue

        # Parse "### 1. Title"
        if line.startswith("### ") and ". " in line:
            _flush_current()
            title = line.split(". ", 1)[1].strip()
            current = {
                "title": title,
                "why": "",
                "impact": "",
                "lens": "",
                "files": [],
                "kind": "refine",
                "acceptance_criteria": [],
                "verification": [],
                "out_of_scope": [],
            }
            list_mode = None
            continue

        if not current:
            continue

        # --- Single-line header fields ---
        if line.startswith("**Kind:**"):
            current["kind"] = line.replace("**Kind:**", "").strip()
            list_mode = None
            continue
        if line.startswith("**Lens:**"):
            current["lens"] = line.replace("**Lens:**", "").strip()
            list_mode = None
            continue
        if line.startswith("**Why:**"):
            current["why"] = line.replace("**Why:**", "").strip()
            list_mode = None
            continue
        if line.startswith("**Impact:**"):
            current["impact"] = line.replace("**Impact:**", "").strip()
            list_mode = None
            continue

        # --- Files: new multi-line or legacy flat ---
        if line.startswith("**Files:**"):
            remainder = line.replace("**Files:**", "").strip()
            if remainder:
                # Legacy flat format: "**Files:** a.py, b.py, c.py"
                current["files"] = [
                    {"path": f.strip(), "rationale": ""}
                    for f in remainder.split(",")
                    if f.strip()
                ]
                list_mode = None
            else:
                # New multi-line format: bullet list follows
                list_mode = "files"
            continue

        # --- New multi-line list section headers ---
        if line.startswith("**Acceptance criteria:**"):
            list_mode = "acceptance_criteria"
            continue
        if line.startswith("**Verification:**"):
            list_mode = "verification"
            continue
        if line.startswith("**Out of scope:**"):
            list_mode = "out_of_scope"
            continue

        # --- Any other bold header ends list accumulation ---
        if line.startswith("**") and line.rstrip().endswith("**"):
            list_mode = None
            continue

        # --- Accumulate list items into the active list_mode ---
        if list_mode == "files":
            m = _FILE_BULLET_RE.match(line.strip())
            if m:
                current["files"].append({
                    "path": m.group(1),
                    "rationale": (m.group(2) or "").strip(),
                })
            elif not line.strip():
                list_mode = None
            continue

        if list_mode == "acceptance_criteria":
            m = _NUMBERED_ITEM_RE.match(line.strip())
            if m:
                current["acceptance_criteria"].append(m.group(1).strip())
            elif not line.strip():
                list_mode = None
            continue

        if list_mode == "verification":
            m = _BULLET_ITEM_RE.match(line.strip())
            if m:
                current["verification"].append(m.group(1).strip())
            elif not line.strip():
                list_mode = None
            continue

        if list_mode == "out_of_scope":
            m = _BULLET_ITEM_RE.match(line.strip())
            if m:
                current["out_of_scope"].append(m.group(1).strip())
            elif not line.strip():
                list_mode = None
            continue

    if current and in_actions:
        actions.append(current)

    return actions


def _write_backlog(
    project_path: Path,
    actions: list[dict],
    scan_file: Path,
    *,
    config: object | None = None,
) -> Path:
    """Write refinement items to .sentinel/backlog.md (the autonomous work queue).

    Before writing, proposed refinements are run through two filters:

    1. The built-in integrations registry
       (``sentinel.integrations.registry``) drops proposals that
       re-implement features Sentinel already ships — e.g.
       "Automate Sentinel Cycle Journaling" when Cortex T1.6 is active.
    2. The rejection-memory log (``sentinel.integrations.rejections``)
       drops proposals that match a reviewer rejection from the last
       30 days.

    Filtered items do not vanish — they're recorded as skip-audit
    comments at the foot of the backlog so the user can see what was
    suppressed and why. The user can still force an item in by
    deleting the relevant rejection line or editing the scan.
    """
    from sentinel.integrations.registry import filter_actions
    from sentinel.integrations.rejections import filter_rejected

    backlog_path = project_path / ".sentinel" / "backlog.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    scan_name = scan_file.name

    all_refinements = [a for a in actions if a.get("kind", "refine") == "refine"]

    # Filter 1: built-in integrations registry.
    registry_outcome = filter_actions(all_refinements, project_path, config)
    # Filter 2: rejection memory (runs on the post-registry set so we
    # don't double-report a drop that the registry already caught).
    rejection_outcome = filter_rejected(registry_outcome.kept, project_path)

    refinements = rejection_outcome.kept

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
        lines.append(f"**Why:** {action.get('why', '')}")
        lines.append(f"**Impact:** {action.get('impact', '')}")
        lines.append("")
        if action.get("files"):
            lines.append("**Files:**")
            for f in action["files"]:
                if isinstance(f, dict):
                    path = f.get("path", "")
                    rationale = f.get("rationale", "")
                    if rationale:
                        lines.append(f"- `{path}` — {rationale}")
                    else:
                        lines.append(f"- `{path}`")
                else:
                    lines.append(f"- `{f}`")
            lines.append("")
        if action.get("acceptance_criteria"):
            lines.append("**Acceptance criteria:**")
            for j, criterion in enumerate(action["acceptance_criteria"], 1):
                lines.append(f"{j}. {criterion}")
            lines.append("")
        if action.get("verification"):
            lines.append("**Verification:**")
            for cmd in action["verification"]:
                lines.append(f"- `{cmd}`")
            lines.append("")
        if action.get("out_of_scope"):
            lines.append("**Out of scope:**")
            for item in action["out_of_scope"]:
                lines.append(f"- {item}")
            lines.append("")
        lines.append("---")
        lines.append("")

    # Skip audit — show the user what the filters dropped and why.
    # This is informational; the items are *not* re-queued. If a skip
    # is wrong (integration mis-matched, rejection is stale), the user
    # deletes the relevant `.sentinel/state/rejections.jsonl` line or
    # files a registry false-positive and re-runs the scan.
    total_skipped = len(registry_outcome.skipped) + len(rejection_outcome.skipped)
    if total_skipped:
        lines.append("## Skipped proposals")
        lines.append("")
        lines.append(
            "*The following items were generated by the scan but "
            "filtered before reaching the backlog.*"
        )
        lines.append("")
        for action, match in registry_outcome.skipped:
            lines.append(
                f"- **{action.get('title', '(untitled)')}** — "
                f"already shipped as built-in integration "
                f"`{match.integration.slug}` "
                f"({match.integration.shipped_in or 'current binary'}). "
                f"{match.integration.description}"
            )
        for action, match in rejection_outcome.skipped:
            reason = (match.record.reviewer_reason or "").strip()
            if len(reason) > 140:
                reason = reason[:137].rstrip() + "..."
            lines.append(
                f"- **{action.get('title', '(untitled)')}** — matches "
                f"a rejected item from cycle `{match.record.cycle_id}` "
                f"({match.record.review_verdict}). Skipping. "
                f"Reviewer reason: {reason or '(none recorded)'}"
            )
        lines.append("")
        lines.append(
            "*To force a skipped item back into the backlog, delete "
            "the matching line from `.sentinel/state/rejections.jsonl` "
            "(for rejection filters) or open a sentinel bug (for "
            "registry false positives), then re-run `sentinel plan`.*"
        )
        lines.append("")

    backlog_path.write_text("\n".join(lines))
    return backlog_path


def _slug(title: str) -> str:
    """Short slug from a title for filenames."""
    import re
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40]


# Tunable threshold for the near-duplicate guard. Computed as Jaccard
# similarity over keyword sets drawn from each proposal's title +
# rationale. Picked empirically: 0.6 catches the autumn-mail "three
# flavors of gws CLI wrapper" cluster (Finding F4) while sparing
# genuinely distinct proposals (e.g., "gws wrapper" vs "MLX inference"
# share at most 0.1 — well under the threshold). Tunable by editing
# the constant; not a config knob until we have a second project's
# data point arguing for a different default.
_PROPOSAL_DEDUP_THRESHOLD = 0.6

# A small, language-agnostic set of high-frequency tokens that add
# noise rather than signal to the similarity score. Kept tiny on
# purpose — a heavy stopword list would be tuned to English prose,
# but proposal titles are usually short technical phrases.
_PROPOSAL_DEDUP_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "in", "with", "to", "and", "or",
    "is", "be", "as", "by", "on", "at", "from", "into", "via",
    "this", "that", "these", "those", "it", "its",
})


def _proposal_keyword_set(text: str) -> frozenset[str]:
    """Tokenize ``text`` into a comparable bag-of-words set.

    Lowercases, splits on non-alphanumerics, drops short tokens
    (length < 3 — too noisy for similarity) and stopwords. Returns a
    frozenset so callers can compute Jaccard cheaply via set algebra.

    Used by the planner pre-flight dedupe (Finding F4): proposals
    whose ``title + why`` token set overlaps strongly with an existing
    proposal are skipped before write to prevent the autumn-mail
    "three flavors of gws CLI wrapper" accumulation pattern.
    """
    import re
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(
        t for t in tokens
        if len(t) >= 3 and t not in _PROPOSAL_DEDUP_STOPWORDS
    )


def _jaccard_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Standard Jaccard: |intersection| / |union|.

    Returns 0.0 when both sets are empty rather than raising — an
    empty proposal vs. an empty proposal isn't a "duplicate" worth
    skipping, it's a different bug (planner emitted no rationale).
    """
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _load_all_proposals(project_path: Path) -> list[dict]:
    """Load every proposal under .sentinel/proposals/, regardless of Status.

    The dedupe pre-flight needs *all* prior proposals to compare
    against, not just approved ones. Pending proposals are the most
    important class to dedupe against — they're the ones the planner
    keeps regenerating in slightly different shapes.

    Returns a list of dicts with at least ``title`` and ``why`` (used
    for similarity); other fields included opportunistically. Failures
    on individual files are logged and skipped — one malformed
    proposal must not silently disable the whole guard.
    """
    import logging
    import re

    proposals_dir = project_path / ".sentinel" / "proposals"
    if not proposals_dir.exists():
        return []

    log = logging.getLogger(__name__)
    items: list[dict] = []
    for path in sorted(proposals_dir.glob("*.md")):
        try:
            content = path.read_text()
        except OSError as exc:
            log.warning("Skipping proposal %s during dedupe load: %s", path, exc)
            continue
        title_match = re.search(r"^# Proposal:\s*(.+)$", content, re.MULTILINE)
        why_match = re.search(
            r"## Why\s*\n\s*\n(.+?)(?=\n##|\Z)", content, re.DOTALL,
        )
        items.append({
            "title": title_match.group(1).strip() if title_match else path.stem,
            "why": why_match.group(1).strip() if why_match else "",
            "path": str(path),
        })
    return items


def _filter_near_duplicate_proposals(
    new_actions: list[dict],
    existing_proposals: list[dict],
    *,
    threshold: float = _PROPOSAL_DEDUP_THRESHOLD,
) -> tuple[list[dict], list[tuple[dict, dict, float]]]:
    """Drop new_actions whose title+rationale resembles an existing proposal.

    Returns ``(kept, skipped)`` where ``skipped`` is a list of
    ``(new_action, matching_existing, similarity)`` tuples — handed
    back so the caller can log per-skip rationale rather than the
    helper printing directly.

    Picks the *single best* existing match per new action (highest
    similarity) so the skip log names a concrete prior proposal.
    """
    if not existing_proposals:
        return list(new_actions), []

    existing_token_sets = [
        (item, _proposal_keyword_set(
            f"{item.get('title', '')} {item.get('why', '')}",
        ))
        for item in existing_proposals
    ]

    kept: list[dict] = []
    skipped: list[tuple[dict, dict, float]] = []
    for action in new_actions:
        action_tokens = _proposal_keyword_set(
            f"{action.get('title', '')} {action.get('why', '')}",
        )
        # Empty token bags can't be meaningfully compared — let them
        # through and surface the lack of rationale separately.
        if not action_tokens:
            kept.append(action)
            continue

        best: tuple[dict, float] | None = None
        for existing_item, existing_tokens in existing_token_sets:
            sim = _jaccard_similarity(action_tokens, existing_tokens)
            if best is None or sim > best[1]:
                best = (existing_item, sim)

        if best is not None and best[1] > threshold:
            skipped.append((action, best[0], best[1]))
        else:
            kept.append(action)
    return kept, skipped


def _write_proposals(
    project_path: Path, actions: list[dict], scan_file: Path,
) -> list[Path]:
    """Write expansion items as individual proposals awaiting user approval."""
    import logging
    log = logging.getLogger(__name__)

    proposals_dir = project_path / ".sentinel" / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d")

    expansions = [a for a in actions if a.get("kind") == "expand"]

    # Pre-flight semantic dedupe — Finding F4 from autumn-mail dogfood
    # cycle 4. Before, the planner accumulated ~3 flavors of "gws CLI
    # wrapper for Gmail" because rejection memory only catches exact
    # title matches. Now: skip new proposals whose title+rationale
    # token set overlaps an existing proposal (any status) above the
    # tuned threshold.
    existing = _load_all_proposals(project_path)
    expansions, dedup_skipped = _filter_near_duplicate_proposals(
        expansions, existing,
    )
    for new_action, match, sim in dedup_skipped:
        log.debug(
            "Skipping near-duplicate proposal: %s (similar to %s, "
            "similarity=%.2f)",
            new_action.get("title", "(untitled)"),
            match.get("title", "(untitled)"),
            sim,
        )

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

    all_refinements = [a for a in actions if a.get("kind", "refine") == "refine"]
    expansions = [a for a in actions if a.get("kind") == "expand"]
    console.print(
        f"  [green]✓[/green] Extracted {len(actions)} work items "
        f"[dim]({len(all_refinements)} refine, {len(expansions)} expand)[/dim]"
    )

    # Load config so the registry filter can inspect integration
    # settings (`integrations.cortex.enabled`, etc.). Config is
    # optional — planner runs fine without it, the registry just
    # loses signal on the opt-out axis.
    from sentinel.cli.scan_cmd import _load_config
    from sentinel.integrations.registry import filter_actions
    from sentinel.integrations.rejections import filter_rejected
    config = _load_config(project)

    # Apply the same filter stack _write_backlog uses, so the CLI
    # summary, --sync-github, and the on-disk backlog all agree on
    # what made the cut. Without this mirror the summary advertises
    # items that never reached the backlog, and --sync-github opens
    # GitHub issues for built-in or previously-rejected work.
    registry_outcome = filter_actions(all_refinements, project, config)
    rejection_outcome = filter_rejected(registry_outcome.kept, project)
    refinements = rejection_outcome.kept
    skipped_total = (
        len(registry_outcome.skipped) + len(rejection_outcome.skipped)
    )

    # Write backlog.md (refinements only — autonomously executable)
    backlog_path = _write_backlog(project, actions, scan_file, config=config)
    extra = f", {skipped_total} skipped by filters" if skipped_total else ""
    console.print(
        f"  [green]✓[/green] Wrote {backlog_path.relative_to(project)} "
        f"[dim]({len(refinements)} refinements{extra})[/dim]"
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
