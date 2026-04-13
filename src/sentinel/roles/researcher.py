"""
Researcher role — deep research to guide the next step.

Uses web search (Gemini grounding) to investigate best practices,
evaluate alternatives, and build research briefs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sentinel.providers.router import Router


@dataclass
class ResearchBrief:
    question: str
    mode: Literal["targeted", "exploratory", "comparative", "consensus"]
    findings: list[str] = field(default_factory=list)
    synthesis: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"
    sources: list[str] = field(default_factory=list)


class Researcher:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def targeted(self, question: str, context: str | None = None) -> ResearchBrief:
        """Research a specific topic before acting."""
        raise NotImplementedError

    async def exploratory(self, project_path: str) -> ResearchBrief:
        """Discover what we should be thinking about."""
        raise NotImplementedError

    async def comparative(self, topic: str, alternatives: list[str]) -> ResearchBrief:
        """Evaluate alternatives."""
        raise NotImplementedError

    async def consensus(self, question: str) -> ResearchBrief:
        """Ask multiple providers independently, synthesize."""
        raise NotImplementedError
