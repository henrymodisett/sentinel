"""Git helpers shared by Coder, Worktree, and PR shipping.

Kept in one module so there's one name for `run_git`, one for `slug`,
one for branch/sha queries — scattering these across roles/ led to
drift between the Coder's branch-naming and what the PR-shipping code
expected. Everything here is thin wrappers over `subprocess.run` with
`git` as the binary; no async (git operations are fast enough that
subprocess blocking is fine).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path  # noqa: TC003 — runtime use


def run_git(
    args: list[str], cwd: str | Path, *, check: bool = False, timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in `cwd`. `check=True` raises on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=str(cwd), timeout=timeout,
        check=check,
    )


def slug(title: str, max_len: int = 50) -> str:
    """Turn a work-item title into a git-safe branch slug."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:max_len]


def current_branch(cwd: str | Path) -> str:
    """Return the name of the currently checked-out branch.

    Returns an empty string if we're in a detached HEAD state (no
    symbolic ref) or the cwd isn't a git repo — callers can decide
    what to do.
    """
    result = run_git(["symbolic-ref", "--short", "HEAD"], cwd)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def current_sha(cwd: str | Path) -> str:
    """Return the full SHA of the current HEAD."""
    result = run_git(["rev-parse", "HEAD"], cwd)
    return result.stdout.strip() if result.returncode == 0 else ""


def branch_exists(cwd: str | Path, branch: str) -> bool:
    """True iff `branch` is a known local branch."""
    result = run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd,
    )
    return result.returncode == 0


def remote_url(cwd: str | Path, remote: str = "origin") -> str:
    """Return the URL of the named remote, or '' if it doesn't exist."""
    result = run_git(["remote", "get-url", remote], cwd)
    return result.stdout.strip() if result.returncode == 0 else ""
