"""
Reviewer role — verifies completed work independently.

Uses a DIFFERENT provider than the coder by default — two models with
different blind spots catch more issues than one reviewing its own work.
"""

from __future__ import annotations

import datetime as _dt
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sentinel.providers.router import Router
    from sentinel.roles.coder import ExecutionResult
    from sentinel.roles.planner import WorkItem


@dataclass
class ReviewResult:
    work_item_id: str
    verdict: Literal["approved", "changes-requested", "rejected"]
    summary: str = ""
    blocking_issues: list[str] = field(default_factory=list)
    non_blocking_observations: list[str] = field(default_factory=list)
    acceptance_criteria_met: dict[str, bool] = field(default_factory=dict)
    cost_usd: float = 0.0


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approved", "changes-requested", "rejected"],
        },
        "summary": {
            "type": "string",
            "description": "One paragraph: what this change does and your overall verdict",
        },
        "blocking_issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Bugs/risks that must be fixed before merge",
        },
        "non_blocking_observations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Worth noting but not blocking",
        },
        "criteria_met": {
            "type": "object",
            "description": "Map of acceptance criterion → whether it was met",
            "additionalProperties": {"type": "boolean"},
        },
    },
    "required": ["verdict", "summary", "blocking_issues"],
    "additionalProperties": False,
}


REVIEW_PROMPT = """\
You are reviewing a completed work item for the {project_name} project.

## Original Work Item: {title}

{description}

### Acceptance Criteria
{criteria}

## What the Coder Produced

**Status:** {status}
**Files changed:** {files_changed}
**Tests passing:** {tests_passing}
**Branch:** {branch}

### Diff
```diff
{diff}
```

### Coder's notes
{coder_output}

## Your Task

Independently verify this work. You are a senior engineer reviewing a PR:

1. Does the code actually meet each acceptance criterion?
2. Are there bugs, security issues, or silent failures introduced?
3. Are tests present for new behavior?
4. Does the code follow project conventions (check CLAUDE.md patterns)?
5. Is this the minimum change, or did the coder scope-creep?

Be strict. Better to request changes on a marginal PR than to approve bad code.

Verdicts:
- **approved**: ships as-is, acceptance criteria met, no blocking issues
- **changes-requested**: fixable issues, coder should iterate
- **rejected**: fundamental problems, needs to be redone

If there are zero blocking issues, verdict is "approved" and you can say
"LGTM" in the summary.
"""


def _slug(title: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:50]


def _write_review_transcript(
    project_path: str,
    work_item: WorkItem,
    execution: ExecutionResult,
    result: ReviewResult,
    diff: str,
    raw_response: str,
) -> Path | None:
    """Persist the reviewer's full output to `.sentinel/reviews/`.

    The work_cmd log only prints the verdict and first few blocking
    issues. Everything else (non-blocking observations, criteria
    scorecard, full summary, raw provider response) was dying on the
    floor. Sigint dogfood showed this: item 1 got "changes-requested"
    and we had NO idea what the reviewer actually said. Without this
    record, a follow-up Coder run can't address the feedback.

    Returns the transcript path on success, None on write failure —
    we never want transcript persistence to mask the real verdict.
    """
    try:
        reviews_dir = Path(project_path) / ".sentinel" / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        timestamp = _dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
        filename = f"{timestamp}-{_slug(work_item.title)}.md"
        path = reviews_dir / filename

        verdict_badge = {
            "approved": "✅ APPROVED",
            "changes-requested": "✏️  CHANGES REQUESTED",
            "rejected": "❌ REJECTED",
        }.get(result.verdict, result.verdict)

        lines: list[str] = [
            f"# Review — {work_item.title}",
            "",
            f"**Verdict:** {verdict_badge}",
            "",
            f"- **Work item ID:** {work_item.id}",
            f"- **Branch:** {execution.branch or '(none)'}",
            f"- **Commit:** {execution.commit_sha or '(none)'}",
            f"- **Files changed:** {len(execution.files_changed)}",
            f"- **Tests passing:** {execution.tests_passing}",
            f"- **Cost (review):** ${result.cost_usd:.4f}",
        ]

        if result.summary:
            lines += ["", "## Summary", "", result.summary]

        if result.blocking_issues:
            lines += ["", "## Blocking issues", ""]
            lines += [f"- {issue}" for issue in result.blocking_issues]

        if result.non_blocking_observations:
            lines += ["", "## Non-blocking observations", ""]
            lines += [f"- {obs}" for obs in result.non_blocking_observations]

        # Defense in depth — the upstream parse normalizes to dict, but
        # a drifted schema or future contributor could still feed this
        # a non-dict. An isinstance check keeps transcript persistence
        # from crashing on surprise types.
        if isinstance(result.acceptance_criteria_met, dict) and result.acceptance_criteria_met:
            lines += ["", "## Acceptance criteria", ""]
            for crit, met in result.acceptance_criteria_met.items():
                mark = "✅" if met else "❌"
                lines += [f"- {mark} {crit}"]

        if raw_response:
            lines += [
                "", "## Raw reviewer response", "",
                "```",
                raw_response[:15000].rstrip(),
                "```",
            ]

        if diff:
            lines += [
                "", "## Diff under review", "",
                "```diff",
                diff[:15000].rstrip(),
                "```",
            ]

        lines += [""]
        # Explicit utf-8 so emoji verdict badges don't crash on
        # non-UTF-8 default locales (e.g. Windows cp1252). Catch broader
        # than OSError — a UnicodeEncodeError here must never mask the
        # real review verdict returned to the orchestrator.
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except (OSError, UnicodeError):
        logging.getLogger(__name__).exception(
            "Failed to write review transcript (non-fatal)",
        )
        return None


def _get_diff(project_path: str, base_branch: str = "main") -> str:
    """Get the diff between current HEAD and base branch."""
    result = subprocess.run(
        ["git", "diff", f"{base_branch}...HEAD"],
        capture_output=True, text=True, cwd=project_path, timeout=30,
    )
    return result.stdout[:15000]  # cap at 15k chars to fit in context


class Reviewer:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def review(
        self, work_item: WorkItem, execution: ExecutionResult, project_path: str,
    ) -> ReviewResult:
        """Review completed work against acceptance criteria."""
        # Get the diff of what changed
        diff = _get_diff(project_path)

        criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "(none)"
        files_changed = ", ".join(execution.files_changed[:10]) or "(none)"

        from pathlib import Path as _Path

        prompt = REVIEW_PROMPT.format(
            project_name=_Path(project_path).name,
            title=work_item.title,
            description=work_item.description,
            criteria=criteria,
            status=execution.status,
            files_changed=files_changed,
            tests_passing=execution.tests_passing,
            branch=execution.branch,
            diff=diff,
            coder_output=execution.raw_output[:2000],
        )

        provider = self.router.get_provider("reviewer")
        coder_provider = self.router.get_provider("coder")

        # Warn if reviewer is same provider as coder (reduces independence)
        if provider.name == coder_provider.name:
            # Still run but note this in observations
            pass

        parsed, response = await provider.chat_json(prompt, REVIEW_SCHEMA)

        result = ReviewResult(
            work_item_id=work_item.id,
            verdict="rejected",
            cost_usd=response.cost_usd,
        )

        if not parsed:
            result.verdict = "rejected"
            result.summary = f"Review failed: {response.content[:200]}"
            result.blocking_issues = ["Reviewer could not produce a structured verdict"]
            _write_review_transcript(
                project_path, work_item, execution, result,
                diff=diff, raw_response=response.content,
            )
            return result

        result.verdict = parsed["verdict"]
        result.summary = parsed.get("summary", "")
        result.blocking_issues = parsed.get("blocking_issues", [])
        result.non_blocking_observations = parsed.get("non_blocking_observations", [])
        # `criteria_met` is schema-optional and some providers drift to
        # returning a list or a string. Normalize to dict at the parse
        # boundary so downstream code can trust the type.
        raw_criteria = parsed.get("criteria_met", {})
        result.acceptance_criteria_met = (
            raw_criteria if isinstance(raw_criteria, dict) else {}
        )
        _write_review_transcript(
            project_path, work_item, execution, result,
            diff=diff, raw_response=response.content,
        )

        return result
