"""
Coder role — executes work items by writing code.

Delegates to Claude Code or Codex CLI for full agentic execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel.providers.router import Router
    from sentinel.roles.planner import WorkItem


@dataclass
class ExecutionResult:
    work_item_id: str
    status: str  # "success", "partial", "failed"
    files_changed: list[str] = field(default_factory=list)
    tests_passing: bool = False
    error: str | None = None
    cost_usd: float = 0.0
    duration_ms: int = 0


class Coder:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def execute(self, work_item: WorkItem, project_path: str) -> ExecutionResult:
        """Execute a work item via the coder provider's agentic mode."""
        raise NotImplementedError
