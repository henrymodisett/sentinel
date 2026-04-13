"""
Reviewer role — verifies completed work.

Uses a DIFFERENT provider than the coder for independent review.
"""

from __future__ import annotations

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
    issues: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)


class Reviewer:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def review(
        self, work_item: WorkItem, result: ExecutionResult, project_path: str,
    ) -> ReviewResult:
        """Review completed work against acceptance criteria."""
        raise NotImplementedError
