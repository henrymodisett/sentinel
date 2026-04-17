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
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in `cwd`. `check=True` raises on non-zero exit.

    `env_overrides` merges onto the current process environment — use
    when a hook needs a specific env var (e.g. pre-commit's
    ``PRE_COMMIT_ALLOW_NO_CONFIG=1``).
    """
    env = None
    if env_overrides:
        import os
        env = {**os.environ, **env_overrides}
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=str(cwd), timeout=timeout,
        check=check, env=env,
    )


def _is_missing_precommit_config_error(stderr: str, stdout: str) -> bool:
    """True iff the git failure is the `pre-commit` tool complaining
    about a missing ``.pre-commit-config.yaml``.

    A globally-installed `pre-commit` hook fires in every repo under
    the user's account. Target repos that don't use pre-commit have no
    config file, which makes hook invocation (commit, push, merge,
    etc.) abort with this error. The signature is stable across
    pre-commit versions.
    """
    blob = f"{stderr}\n{stdout}"
    return "No .pre-commit-config.yaml file was found" in blob


def _precommit_config_absent_from_repo(cwd: str | Path) -> bool:
    """True iff the repo genuinely has no `.pre-commit-config.yaml` —
    neither in the working tree nor in HEAD. Both checks are needed:
    working-tree alone misses the case where the change under consideration
    deletes a tracked config (that's a real hook failure, not an
    environment mismatch); HEAD alone misses the pre-initial-commit case.
    """
    if (Path(cwd) / ".pre-commit-config.yaml").exists():
        return False
    head_check = run_git(
        ["cat-file", "-e", "HEAD:.pre-commit-config.yaml"], cwd,
    )
    return head_check.returncode != 0


def run_git_with_precommit_recovery(
    args: list[str], cwd: str | Path, *, timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a git command that may trigger a git hook; auto-recover
    from the "missing .pre-commit-config.yaml" failure mode.

    Globally-installed `pre-commit` registers hooks for commit, push,
    merge, rebase, etc. — all of them abort with the same error when
    the target repo has no config file. This wrapper detects that
    specific signature and retries once with
    ``PRE_COMMIT_ALLOW_NO_CONFIG=1`` when the repo has never had a
    config. Real hook rejections (lint, tests, etc.) pass through
    unmodified so callers can surface them as review findings.

    Dogfood on portfolio_new surfaced this pattern on both `git commit`
    (2026-04-17 AM, fixed in #61) and `git push` (2026-04-17 PM, this
    change). Use this wrapper for any git operation whose failure
    could be caused by a hook the user didn't opt into.
    """
    import logging

    result = run_git(args, cwd, timeout=timeout)
    if result.returncode == 0:
        return result
    if not _is_missing_precommit_config_error(result.stderr, result.stdout):
        return result
    if not _precommit_config_absent_from_repo(cwd):
        return result

    logging.getLogger(__name__).info(
        "pre-commit has no config in %s; retrying `git %s` with "
        "PRE_COMMIT_ALLOW_NO_CONFIG=1", cwd, args[0] if args else "?",
    )
    return run_git(
        args, cwd, timeout=timeout,
        env_overrides={"PRE_COMMIT_ALLOW_NO_CONFIG": "1"},
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
