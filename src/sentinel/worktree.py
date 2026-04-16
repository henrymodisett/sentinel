"""Per-work-item git worktree.

Each work item runs in its own worktree so concurrent items can't
clobber each other, and the user's main checkout is never touched
during execution.

**Crucial separation:** the worktree is only for the *code changes*.
Persistent artifacts — execution transcripts, review transcripts,
verification logs, run journals — live under the MAIN project's
`.sentinel/` directory, not the worktree. If they lived inside the
worktree they'd vanish on cleanup, exactly when you need them most
(to inspect what went wrong).

The caller decides branch name and base branch. This module just
manages the worktree directory lifecycle.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sentinel.git_ops import branch_exists, run_git

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class WorktreeContext:
    """Handle for a work-item-scoped worktree.

    Fields:
      path: the worktree directory (where Coder edits, Verifier runs)
      branch: the feature branch checked out in the worktree
      base: the branch we forked from (used as PR --base)
      artifacts_dir: absolute path under MAIN project where transcripts
        go; survives worktree cleanup so failure evidence is preserved
    """
    path: Path
    branch: str
    base: str
    artifacts_dir: Path


def _worktree_dir(project_path: Path, slug: str) -> Path:
    return project_path / ".sentinel" / "worktrees" / slug


@asynccontextmanager
async def worktree_for(
    project_path: Path,
    branch: str,
    base: str,
    slug: str,
) -> AsyncIterator[WorktreeContext]:
    """Create a worktree at `.sentinel/worktrees/<slug>` on `branch`
    forked from `base`. Removes the worktree on exit regardless of
    success — but leaves the branch in place (it may have an open PR).

    If `branch` already exists locally (resume-after-crash path),
    reuse it rather than failing. Artifacts are always written to the
    main project's `.sentinel/` — NEVER the worktree.
    """
    wt_path = _worktree_dir(project_path, slug)
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    if wt_path.exists():
        # Stale worktree from a prior crashed run; force-remove before
        # creating a new one so `git worktree add` doesn't fail on the
        # existing directory.
        _force_remove_worktree(project_path, wt_path)

    if branch_exists(project_path, branch):
        add_result = run_git(
            ["worktree", "add", str(wt_path), branch], project_path,
        )
    else:
        add_result = run_git(
            ["worktree", "add", "-b", branch, str(wt_path), base],
            project_path,
        )
    if add_result.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed: {add_result.stderr.strip() or add_result.stdout.strip()}"
        )

    artifacts_dir = project_path / ".sentinel"

    try:
        yield WorktreeContext(
            path=wt_path,
            branch=branch,
            base=base,
            artifacts_dir=artifacts_dir,
        )
    finally:
        _force_remove_worktree(project_path, wt_path)


def _force_remove_worktree(project_path: Path, wt_path: Path) -> None:
    """Remove a worktree directory without leaving git's bookkeeping
    stale. `git worktree remove --force` handles a dirty tree; if
    git's own tracking is already out of sync (e.g., the directory
    was deleted manually), fall back to `git worktree prune` and
    manual rmtree to keep things consistent."""
    result = run_git(
        ["worktree", "remove", "--force", str(wt_path)],
        project_path, check=False,
    )
    if result.returncode == 0:
        return
    # Fallback: if git didn't find the worktree (already removed or
    # never added), make sure neither the directory nor the metadata
    # survive.
    if wt_path.exists():
        with contextlib.suppress(OSError):
            shutil.rmtree(wt_path)
    run_git(["worktree", "prune"], project_path, check=False)


def cleanup_orphaned_worktrees(project_path: Path) -> int:
    """Remove worktrees left behind by prior crashed runs.

    Called once at cycle start. Only touches worktrees whose paths
    live under `.sentinel/worktrees/` — never the user's own
    worktrees. Returns the count removed so the caller can surface
    it in cycle-start output (helpful for debugging).
    """
    result = run_git(
        ["worktree", "list", "--porcelain"], project_path, check=False,
    )
    if result.returncode != 0:
        return 0

    sentinel_prefix = str(project_path / ".sentinel" / "worktrees")
    removed = 0
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        wt_path_str = line.removeprefix("worktree ").strip()
        if not wt_path_str.startswith(sentinel_prefix):
            continue
        _force_remove_worktree(project_path, Path(wt_path_str))
        removed += 1
    return removed
