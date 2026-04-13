"""
Monitor role — scans the codebase through lenses.

Runs frequently, produces a state assessment. Uses the cheapest
provider available (default: local Ollama).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel.providers.router import Router


@dataclass
class LensResult:
    lens: str
    score: int  # 0-100
    issues: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)


@dataclass
class StateAssessment:
    timestamp: str = ""
    branch: str = ""
    uncommitted_changes: int = 0
    recent_commits: list[str] = field(default_factory=list)
    lens_results: list[LensResult] = field(default_factory=list)
    overall_score: int = 0
    changed_since_last_scan: list[str] = field(default_factory=list)


class Monitor:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def assess(self, project_path: str, lenses: list[str] | None = None) -> StateAssessment:
        """Scan the project through each active lens."""
        # TODO: Implement
        # 1. Run git commands to get repo state
        # 2. For each lens, ask the monitor LLM to evaluate
        # 3. Aggregate scores
        raise NotImplementedError
