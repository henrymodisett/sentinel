"""
Reviewer role — verifies completed work independently.

Uses a DIFFERENT provider than the coder by default — two models with
different blind spots catch more issues than one reviewing its own work.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
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
            return result

        result.verdict = parsed["verdict"]
        result.summary = parsed.get("summary", "")
        result.blocking_issues = parsed.get("blocking_issues", [])
        result.non_blocking_observations = parsed.get("non_blocking_observations", [])
        result.acceptance_criteria_met = parsed.get("criteria_met", {})

        return result
