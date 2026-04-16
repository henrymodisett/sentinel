"""Core loop cycle — the four-step cycle that drives Sentinel."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sentinel.roles.coder import Coder, ExecutionResult
from sentinel.roles.monitor import Monitor, ScanResult
from sentinel.roles.planner import Plan, Planner
from sentinel.roles.researcher import ResearchBrief, Researcher
from sentinel.roles.reviewer import Reviewer, ReviewResult
from sentinel.state import gather_state

if TYPE_CHECKING:

    from sentinel.config.schema import SentinelConfig
    from sentinel.providers.router import Router


@dataclass
class CycleResult:
    timestamp: str = ""
    assessment: ScanResult | None = None
    research_briefs: list[ResearchBrief] = field(default_factory=list)
    plan: Plan | None = None
    executions: list[ExecutionResult] = field(default_factory=list)
    reviews: list[ReviewResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    duration_ms: int = 0


class Loop:
    def __init__(self, config: SentinelConfig, router: Router) -> None:
        self.config = config
        self.router = router
        self.monitor = Monitor(router)
        self.researcher = Researcher(router)
        self.planner = Planner(router)
        self.coder = Coder(router)
        self.reviewer = Reviewer(router)

    async def cycle(self) -> CycleResult:
        """Run one full cycle: assess -> research -> plan -> execute -> review."""
        start = time.time()
        project_path = Path(self.config.project.path)

        # Step 1: ASSESS — multi-step scan (explore → generate lenses → evaluate)
        state = gather_state(project_path)
        assessment = await self.monitor.assess(state)

        # Step 2: RESEARCH — investigate issues found
        research_briefs = await self._research_phase(assessment)

        # Step 3: PLAN — create prioritized work items
        plan = await self.planner.plan(assessment, research_briefs)

        # Step 4: DELEGATE — execute and review
        executions, reviews = await self._execute_phase(plan, str(project_path))

        return CycleResult(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            assessment=assessment,
            research_briefs=research_briefs,
            plan=plan,
            executions=executions,
            reviews=reviews,
            total_cost_usd=sum(e.cost_usd for e in executions),
            duration_ms=int((time.time() - start) * 1000),
        )

    async def _research_phase(
        self, assessment: ScanResult,
    ) -> list[ResearchBrief]:
        # TODO(cycle): implement research phase — identify what needs
        # investigating based on assessment, call self.researcher
        raise NotImplementedError(
            "Research phase not yet implemented. Use `sentinel scan` for assessment-only."
        )

    async def _execute_phase(
        self, plan: Plan, project_path: str,  # noqa: ARG002
    ) -> tuple[list[ExecutionResult], list[ReviewResult]]:
        # The Loop class predates the worktree-managed Coder API and
        # the autonomous PR factory in work_cmd._execute_and_review.
        # That module is the live execution path; this one is unreached
        # (Loop.cycle raises in _research_phase before _execute_phase
        # is called). Keep the signature for type-checking and any
        # legacy importers, but be loud about the path being dead.
        raise NotImplementedError(
            "Loop._execute_phase is superseded by "
            "sentinel.cli.work_cmd._execute_and_review. Use `sentinel work`.",
        )
