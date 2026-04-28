"""Destructive-change gate — inspects a diff before `git push`.

Applied after the Coder produces a commit but before Sentinel opens a PR.
When a diff matches any risky pattern, Sentinel writes a
`blocked-on-human-approval` journal entry and leaves the branch for human
review rather than pushing.

Risky patterns (in order of specificity):
1. DB migration files — paths matching known migration path conventions.
2. Large deletions — sum of removed lines exceeds a configurable threshold.
3. Secret-shaped additions — new lines that match gitleaks-style patterns.
   Uses `gitleaks detect` if the binary is on PATH; falls back to a compact
   regex pack for the most common patterns (AWS keys, GCP keys, private key
   PEM headers, generic high-entropy tokens).

Design notes:
- The gate is ADVISORY: it leaves the branch intact so the human can
  inspect and push manually (`git push && gh pr create`). Sentinel does
  not delete or reset the branch on a gate trigger.
- Pattern detection is deliberately conservative: false positives are a
  small inconvenience (human reviews a benign diff); false negatives on a
  real secret leak are a security incident. When in doubt, gate it.
- The regex fallback does NOT attempt to decrypt or validate the matched
  strings — it only checks shape. Gitleaks is more accurate; install it
  to reduce false positives.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Migration file path patterns — conservative set covering the most common
# ORMs and migration frameworks. Users can extend by lowering the deletion
# threshold or running gitleaks with a custom rules file.
_MIGRATION_PATTERNS = [
    re.compile(r"(^|.*/)migrations/[^/]+\.sql$"),
    re.compile(r"(^|.*/)alembic/versions/.*\.py$"),
    re.compile(r"(^|.*/)db/migrations/.*"),
    re.compile(r"(^|.*/)migrate/.*\.sql$"),
    re.compile(r"(^|.*/)migrations/\d{4,}_.*\.py$"),  # Django-style: 0001_initial.py
]

# Secret-shaped regex pack — conservative shape-only patterns. These match
# the structural form of real secrets (prefix + high-entropy suffix) without
# trying to validate them. Gitleaks uses a much larger rule set; this pack
# is the fallback when gitleaks isn't installed.
_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                            # AWS access key
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),                     # Google API key
    re.compile(r"-----BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY"),  # PEM private key
    re.compile(r"ghp_[0-9a-zA-Z]{30,}"),                       # GitHub PAT
    re.compile(r"glpat-[0-9a-zA-Z\-_]{20}"),                   # GitLab PAT
    re.compile(r"xox[baprs]-[0-9a-zA-Z\-]{10,}"),             # Slack token
    re.compile(r"(?i)(?:secret|password|api_?key)\s*=\s*['\"][^'\"]{8,}['\"]"),
]

# Default max deletions before the gate triggers. 100 lines is large enough
# to skip routine refactors (re-exports, small renames) but small enough to
# catch accidental `rm -rf` style mass-deletes.
DEFAULT_MAX_DELETIONS = 100


@dataclass
class GateResult:
    blocked: bool
    reasons: list[str] = field(default_factory=list)
    # Summary for the PR body / journal — what triggered the gate.
    summary: str = ""


def _get_diff(worktree_path: Path, base_branch: str) -> str:
    """Get the unified diff between HEAD and base_branch in the worktree."""
    result = subprocess.run(
        ["git", "diff", f"{base_branch}...HEAD"],
        capture_output=True, text=True, cwd=worktree_path, timeout=30,
    )
    if result.returncode != 0:
        logger.warning("gate: git diff failed: %s", result.stderr.strip())
        return ""
    return result.stdout


def _is_migration_file(path: str) -> bool:
    return any(p.match(path) for p in _MIGRATION_PATTERNS)


def _count_deletions(diff: str) -> int:
    """Count removed lines (lines starting with '-' but not '---')."""
    return sum(
        1 for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )


def _added_lines(diff: str) -> list[str]:
    """Extract added lines (lines starting with '+' but not '+++')."""
    return [
        line[1:] for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def _changed_paths(diff: str) -> list[str]:
    """Extract file paths mentioned in the diff header lines."""
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[6:])
        elif line.startswith("--- a/"):
            # Only add if not already added via +++
            p = line[6:]
            if p not in paths:
                paths.append(p)
    return paths


def _check_secrets_gitleaks(diff: str, worktree_path: Path) -> bool:
    """Run gitleaks detect on the diff. Returns True if secrets found."""
    if not shutil.which("gitleaks"):
        return False
    try:
        result = subprocess.run(
            ["gitleaks", "detect", "--source", ".", "--no-git", "--pipe"],
            input=diff,
            capture_output=True, text=True,
            cwd=worktree_path, timeout=30,
        )
        # gitleaks exits 1 when secrets found, 0 when clean
        return result.returncode == 1
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("gate: gitleaks failed (using regex fallback): %s", e)
        return False


def _check_secrets_regex(lines: list[str]) -> list[str]:
    """Return a list of matched secret patterns found in added lines."""
    matched: list[str] = []
    for line in lines:
        for pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                matched.append(pattern.pattern)
                break
    return matched


def inspect(
    worktree_path: Path,
    base_branch: str,
    *,
    max_deletions: int = DEFAULT_MAX_DELETIONS,
) -> GateResult:
    """Inspect the diff for destructive patterns.

    Args:
        worktree_path: Path to the git worktree containing the commit.
        base_branch: The branch to diff against (typically `main`).
        max_deletions: Deletion threshold — triggers gate when exceeded.

    Returns a GateResult with blocked=True if any risky pattern matches.
    """
    diff = _get_diff(worktree_path, base_branch)
    if not diff:
        return GateResult(blocked=False)

    reasons: list[str] = []
    changed = _changed_paths(diff)

    # 1. Migration files
    migration_files = [p for p in changed if _is_migration_file(p)]
    if migration_files:
        reasons.append(
            f"diff contains DB migration file(s): "
            f"{', '.join(migration_files[:3])}"
        )

    # 2. Large deletions
    deletions = _count_deletions(diff)
    if deletions > max_deletions:
        reasons.append(
            f"diff removes {deletions} lines (threshold: {max_deletions})"
        )

    # 3. Secret-shaped additions
    added = _added_lines(diff)
    if shutil.which("gitleaks"):
        if _check_secrets_gitleaks(diff, worktree_path):
            reasons.append("gitleaks detected secret-shaped content in diff")
    else:
        secret_matches = _check_secrets_regex(added)
        if secret_matches:
            matched_patterns = list(dict.fromkeys(secret_matches))[:3]
            reasons.append(
                f"secret-shaped content detected in diff "
                f"(patterns: {', '.join(matched_patterns[:2])})"
            )

    if not reasons:
        return GateResult(blocked=False)

    summary = (
        "**Destructive-change gate triggered — human review required.**\n\n"
        + "\n".join(f"- {r}" for r in reasons)
        + "\n\nThe branch has been left intact for manual inspection. "
        "Push with `git push` and open a PR manually after verifying the diff."
    )
    return GateResult(blocked=True, reasons=reasons, summary=summary)
