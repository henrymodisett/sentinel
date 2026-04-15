"""Post-execute verification — does the project still pass its own checks?

When Coder commits a feature branch and Reviewer approves it, that's
two LLMs agreeing on a diff. Verification adds a third, deterministic
signal: the project's own lint and test commands, re-run against the
new code. If they pass, the work item is `verified`. If not, it's
`not_verified` regardless of what the reviewer said. Two opinions plus
one objective check.

This module is deliberately narrow — it only runs commands the project
ITSELF has configured (via `.toolkit-config` or auto-detected from
project type). Sentinel never invents a check command. If a project
has no configured lint/test command, the verdict is `no_check_defined`
rather than silently passing.

What this module does NOT do:
- It does not parse Coder's claims or commit messages. The project's
  existing checks are the contract; if they pass, the project's
  invariants still hold. That's a stronger guarantee than parsing
  English from a commit message.
- It does not gate merging. The verifier produces a verdict that the
  journal records; downstream automation (or a future PR) can decide
  what to do with not_verified items. Today, it's an honest signal.
- It does not isolate the project's working tree from check side
  effects (e.g., `.ruff_cache/`, `__pycache__/`). Those are exactly
  what the project's own CI produces; sentinel running the same
  commands inherits the same write footprint, intentionally.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — runtime use for fs reads/writes

logger = logging.getLogger(__name__)

# Per-check wall-clock cap so a hung lint/test doesn't eat the cycle.
# Configurable later if needed; 60s is generous for lint, tight for
# big test suites — projects with long suites can configure a faster
# subset as their test_command (CI-grade, not full).
DEFAULT_CHECK_TIMEOUT_S = 60

# Maximum chars of evidence to keep per check. Trim from the END so the
# important "FAILED:" / "errors found:" lines stay in the persisted log.
MAX_EVIDENCE_CHARS = 1000


@dataclass
class CheckResult:
    name: str  # "lint" | "test"
    command: str | None  # None if no command available
    verdict: str  # "pass" | "fail" | "no_check_defined"
    duration_s: float = 0.0
    evidence: str = ""  # short tail of stdout+stderr for postmortem


@dataclass
class WorkItemVerification:
    work_item_id: str
    work_item_title: str
    overall: str  # "verified" | "not_verified" | "no_check_defined"
    checks: list[CheckResult] = field(default_factory=list)
    branch: str | None = None
    timestamp: str = ""


def discover_checks(project_path: Path) -> dict[str, str | None]:
    """Return {check_name: command_or_None} from toolkit-config or
    auto-detect. Reuses state.py's machinery so verification and scan
    state-gathering can never disagree about what a project's lint /
    test commands are."""
    from sentinel.state import _read_toolkit_command, detect_project_type

    toolkit_config = project_path / ".toolkit-config"
    detected = detect_project_type(project_path)
    return {
        "lint": (
            _read_toolkit_command(toolkit_config, "lint_command")
            or detected.get("lint_command")
        ),
        "test": (
            _read_toolkit_command(toolkit_config, "test_command")
            or detected.get("test_command")
        ),
    }


def run_check(
    name: str,
    command: str | None,
    project_path: Path,
    timeout_s: int = DEFAULT_CHECK_TIMEOUT_S,
) -> CheckResult:
    """Run one check. No-command → no_check_defined; success → pass;
    non-zero exit → fail; subprocess error / timeout → fail with
    explanatory evidence."""
    if not command:
        return CheckResult(
            name=name, command=None, verdict="no_check_defined",
            evidence="(no command configured)",
        )

    started = time.perf_counter()
    try:
        result = subprocess.run(  # noqa: S603 — command is project-owned
            shlex.split(command),
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=name, command=command, verdict="fail",
            duration_s=time.perf_counter() - started,
            evidence=f"(timed out after {timeout_s}s)",
        )
    except (OSError, FileNotFoundError) as e:
        # Command was configured but couldn't start (binary missing,
        # permission denied, etc.). This is a misconfiguration, NOT a
        # "no check defined" — the project asked us to run something
        # and we couldn't. Treat it as fail so it rolls up to
        # not_verified and the broken config surfaces in the journal.
        # The "no_check_defined" verdict is reserved for the case
        # where the project never configured a command at all
        # (handled at the top of this function).
        return CheckResult(
            name=name, command=command, verdict="fail",
            duration_s=time.perf_counter() - started,
            evidence=f"command not runnable: {e}",
        )

    duration = time.perf_counter() - started
    output = (result.stdout + result.stderr).strip()
    evidence = output[-MAX_EVIDENCE_CHARS:] if output else "(no output)"
    verdict = "pass" if result.returncode == 0 else "fail"
    return CheckResult(
        name=name, command=command, verdict=verdict,
        duration_s=duration, evidence=evidence,
    )


def verify_work_item(
    project_path: Path,
    work_item_id: str,
    work_item_title: str,
    branch: str | None = None,
) -> WorkItemVerification:
    """Run all configured checks against the project, return a verdict.

    Overall verdict logic:
    - All checks have no command → no_check_defined (we couldn't tell)
    - Any check failed → not_verified (claim contradicted by reality)
    - Otherwise (all pass, or mix of pass + no_check_defined) → verified
    """
    checks_config = discover_checks(project_path)
    results = [
        run_check(name, command, project_path)
        for name, command in checks_config.items()
    ]

    if all(r.verdict == "no_check_defined" for r in results):
        overall = "no_check_defined"
    elif any(r.verdict == "fail" for r in results):
        overall = "not_verified"
    else:
        overall = "verified"

    return WorkItemVerification(
        work_item_id=work_item_id,
        work_item_title=work_item_title,
        overall=overall,
        checks=results,
        branch=branch,
        timestamp=datetime.now(UTC).isoformat(),
    )


def persist_verification(
    project_path: Path,
    verification: WorkItemVerification,
) -> Path:
    """Append the verification to .sentinel/verifications.jsonl.

    Append-only, one line per verification, JSON-per-line so trend
    tooling can stream it. Each line is self-describing — never relies
    on prior lines for context."""
    sentinel_dir = project_path / ".sentinel"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    log_path = sentinel_dir / "verifications.jsonl"

    payload = {
        "ts": verification.timestamp,
        "work_item_id": verification.work_item_id,
        "title": verification.work_item_title,
        "branch": verification.branch,
        "overall": verification.overall,
        "checks": [
            {
                "name": c.name,
                "command": c.command,
                "verdict": c.verdict,
                "duration_s": round(c.duration_s, 3),
                "evidence": c.evidence,
            }
            for c in verification.checks
        ],
    }
    with log_path.open("a") as f:
        f.write(json.dumps(payload) + "\n")
    return log_path
