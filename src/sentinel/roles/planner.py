"""
Planner role — strategic decisions, task decomposition, prioritization.

Takes state assessment + research briefs, produces prioritized work items.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sentinel.providers.router import Router
    from sentinel.roles.monitor import StateAssessment
    from sentinel.roles.researcher import ResearchBrief


@dataclass
class WorkItem:
    id: str
    title: str
    description: str
    type: Literal["feature", "bugfix", "refactor", "test", "docs", "chore"]
    priority: Literal["critical", "high", "medium", "low"]
    complexity: int  # 1-5
    files: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    lens: str = ""  # which lens surfaced this
    risk: str = ""


@dataclass
class Plan:
    timestamp: str = ""
    backlog: list[WorkItem] = field(default_factory=list)
    rationale: str = ""


class Planner:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def plan(
        self,
        assessment: StateAssessment,
        research: list[ResearchBrief],
    ) -> Plan:
        """Generate a prioritized backlog from current state and research."""
        raise NotImplementedError
