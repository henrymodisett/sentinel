"""PR-shipping primitive — push the worktree's branch, open a PR, arm auto-merge.

Sentinel does the **minimum** git/gh operations needed to ship a PR:
push, `gh pr create`, optionally `gh pr merge --auto`. No reimplementation
of Codex review, branch hygiene, or other Toolkit value-adds — those flow
in via the user's git hooks (pre-push, etc.) when Toolkit is installed.

Codex review of the PR factory plan flagged five real risks this module
addresses up-front:

1. **Idempotency.** If a previous cycle pushed the branch but failed to
   create the PR, the next cycle must discover the existing PR (via
   `gh pr list --head <branch>`) and resume — never recreate.

2. **Auto-merge safety.** `gh pr merge --auto` only waits for required
   checks. On an unprotected branch, the PR merges instantly. We only
   arm auto-merge when the base branch has required checks; otherwise
   the PR is created and left for human review.

3. **Explicit head/base.** Relying on "current branch" is fragile when
   cleanup, retries, and existing PRs are in play. We always pass
   `--head <branch>`, `--base <base>`, and `--match-head-commit <sha>`.

4. **Body via file, not flag.** Large generated PR bodies hit shell
   argument length and quoting issues with `--body`. Always write to a
   tempfile and pass `--body-file`.

5. **Push to a verified remote.** Caller is responsible for confirming
   the push remote exists; this module assumes `origin` and surfaces a
   clear error if push fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sentinel.git_ops import run_git


@dataclass
class ShipResult:
    """Outcome of a `ship_pr` invocation.

    `status` is one of:
      - "merged_armed": PR created/found, auto-merge enabled
      - "created": PR created/found, auto-merge NOT armed (no required
        checks on base branch)
      - "existed": PR already open for this branch from a prior cycle;
        no new PR created (auto-merge state untouched)
      - "failed": push or PR creation failed; see `error`
    """
    status: str
    pr_url: str = ""
    error: str = ""


def _gh(
    args: list[str], cwd: Path, *, check: bool = False, timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command. Non-interactive env so prompts can't hang."""
    env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
    return subprocess.run(
        ["gh", *args],
        capture_output=True, text=True, cwd=str(cwd),
        check=check, timeout=timeout, env=env,
    )


def _existing_pr(
    branch: str, project_path: Path,
) -> dict | None:
    """Return the open PR for `branch`, if any. Used for idempotency:
    a branch left over from a prior cycle's push-but-no-PR failure
    must be discovered, not recreated."""
    result = _gh(
        ["pr", "list", "--head", branch, "--state", "open",
         "--json", "url,number,state"],
        project_path,
    )
    if result.returncode != 0:
        return None
    try:
        prs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return prs[0] if prs else None


def _has_required_checks(base: str, project_path: Path) -> bool:
    """True iff the base branch has any required status checks
    configured. Used to decide whether `gh pr merge --auto` is safe —
    on an unprotected branch, auto-merge fires immediately, defeating
    the "CI gates the merge" guarantee.
    """
    result = _gh(
        ["api", f"repos/{{owner}}/{{repo}}/branches/{base}/protection",
         "--jq", ".required_status_checks.contexts // [] | length"],
        project_path,
    )
    if result.returncode != 0:
        # 404 = no protection configured. Any other error: be safe,
        # treat as no protection rather than risk an unguarded merge.
        return False
    count_str = (result.stdout or "0").strip()
    try:
        return int(count_str) > 0
    except ValueError:
        return False


async def ship_pr(  # noqa: PLR0911 — explicit early-returns are clearer here
    *,
    worktree_path: Path,
    project_path: Path,
    branch: str,
    base: str,
    head_sha: str,
    title: str,
    body_md: str,
) -> ShipResult:
    """Push the worktree's branch and open (or resume) a PR.

    Steps:
      1. Push `branch` to `origin` from the worktree's git context.
         Toolkit's pre-push hook (Codex review) fires here transparently
         if installed.
      2. Check for an existing open PR with this `--head`. If one
         exists, return "existed" — never duplicate.
      3. Create the PR with explicit `--head`, `--base`, `--body-file`,
         `--match-head-commit <sha>`.
      4. If the base branch has required status checks, arm
         `gh pr merge --auto --squash`. Otherwise return "created"
         without arming — letting an auto-merge fire on an unprotected
         branch would skip the CI gate Sentinel relies on.

    `head_sha` ensures the PR title/body and auto-merge attach to the
    exact commit Sentinel just shipped — protects against a race where
    another push lands between steps.
    """
    # 1. Push (use upstream tracking; --force-with-lease so a resumed
    # cycle can update the branch safely without clobbering remote work
    # that wasn't there when we started).
    push_result = run_git(
        ["push", "--force-with-lease", "-u", "origin", branch],
        worktree_path, check=False, timeout=120,
    )
    if push_result.returncode != 0:
        return ShipResult(
            status="failed",
            error=(
                f"git push failed: "
                f"{push_result.stderr.strip() or push_result.stdout.strip()}"
            ),
        )

    # 2. Idempotency: existing PR for this head?
    existing = _existing_pr(branch, project_path)
    if existing:
        return ShipResult(status="existed", pr_url=existing.get("url", ""))

    # 3. Create PR with explicit, race-safe flags
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8",
    ) as body_file:
        body_file.write(body_md)
        body_path = body_file.name
    try:
        create_result = _gh(
            [
                "pr", "create",
                "--head", branch,
                "--base", base,
                "--title", title,
                "--body-file", body_path,
            ],
            project_path,
        )
    finally:
        Path(body_path).unlink(missing_ok=True)

    if create_result.returncode != 0:
        return ShipResult(
            status="failed",
            error=(
                f"gh pr create failed: "
                f"{create_result.stderr.strip() or create_result.stdout.strip()}"
            ),
        )

    # `gh pr create` prints the URL to stdout on success
    pr_url = (create_result.stdout or "").strip().splitlines()[-1].strip()

    # 4. Arm auto-merge ONLY when the base branch is actually protected.
    # An unprotected base + auto-merge means instant merge with no CI
    # gate — exactly the failure mode codex flagged.
    if not _has_required_checks(base, project_path):
        return ShipResult(status="created", pr_url=pr_url)

    merge_result = _gh(
        [
            "pr", "merge", pr_url,
            "--auto", "--squash",
            "--match-head-commit", head_sha,
        ],
        project_path,
    )
    if merge_result.returncode != 0:
        # PR exists; auto-merge couldn't be armed (often: squash
        # disabled at repo level). Surface this as "created" with a
        # note — the user can merge manually.
        return ShipResult(
            status="created",
            pr_url=pr_url,
            error=(
                f"PR created but auto-merge could not be armed: "
                f"{merge_result.stderr.strip() or merge_result.stdout.strip()}"
            ),
        )

    return ShipResult(status="merged_armed", pr_url=pr_url)
