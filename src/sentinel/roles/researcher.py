"""
Researcher role — deep research to guide the next step.

Uses web search (Gemini grounding) to investigate best practices,
evaluate alternatives, and build research briefs.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sentinel.providers.router import Router

logger = logging.getLogger(__name__)


@dataclass
class ResearchBrief:
    question: str
    mode: Literal["targeted", "exploratory", "comparative", "consensus", "domain"]
    findings: list[str] = field(default_factory=list)
    synthesis: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"
    sources: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


# Domain brief lives in .sentinel/domain_brief.md alongside the other
# state files. Caching is keyed on a hash of (project_type, readme
# prefix, project_docs prefix) so the brief auto-refreshes when the
# project's shape meaningfully changes.
DOMAIN_BRIEF_FILENAME = "domain_brief.md"
DOMAIN_BRIEF_TTL_DAYS = 7


DOMAIN_RESEARCH_PROMPT = """\
You are researching the subject-matter domain of a software project
before sentinel evaluates it. Your output will be read by an LLM that
generates analytical lenses — so it needs to carry concrete domain
knowledge, not generic "consider testing and security" filler.

## Project snapshot

- Type: {project_type}
- Name: {project_name}

### README (truncated)
{readme_excerpt}

### Strategic docs (truncated)
{docs_excerpt}

## Your task

Identify the domain and produce a sharp, specific brief that an
evaluator could use to assess this project competently. Use web
search to ground your output in current best practices.

Output plain markdown with these sections:

## Domain
<one sentence naming the specific field — "event-driven quant trading
on prediction markets", not "finance">

## What "good" looks like in this domain
<3-5 concrete properties that separate winning projects from losing
ones in this domain. Specific metrics, measurable behaviors, or
architecture patterns — not platitudes.>

## Common failure modes
<3-5 specific ways projects in this domain fail. Ideally with the
mechanism, not just the symptom. "Mean-reversion strategies that
ignore regime changes blow up in trending markets" beats "risk
management is important".>

## Key benchmarks or references
<2-4 links/names that the evaluator should check against. Industry
benchmarks, authoritative specs, widely-cited papers, major open-source
examples. Real URLs.>

Keep the whole brief under 800 words. Density over length.
"""


class Researcher:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def domain_brief(
        self,
        project_path: str,
        project_type: str,
        project_name: str,
        readme_excerpt: str,
        docs_excerpt: str,
    ) -> ResearchBrief:
        """Build a domain-expertise brief before lens generation.

        Cached to .sentinel/domain_brief.md so we don't pay the research
        round-trip on every scan. Cache is invalidated by:
        - age (> DOMAIN_BRIEF_TTL_DAYS)
        - context hash change (readme or strategic docs shifted)

        On research failure returns an empty brief — lens generation
        still works, just without the domain context (same as today).
        """
        from sentinel.journal import set_current_role
        set_current_role("researcher")
        cache_path = Path(project_path) / ".sentinel" / DOMAIN_BRIEF_FILENAME
        context_hash = _hash_context(
            project_type, readme_excerpt, docs_excerpt,
        )
        cached = _load_cached_brief(cache_path, context_hash)
        if cached is not None:
            return cached

        provider = self.router.get_provider("researcher", intent="research")
        prompt = DOMAIN_RESEARCH_PROMPT.format(
            project_type=project_type or "generic",
            project_name=project_name,
            readme_excerpt=readme_excerpt[:2000] or "(no README)",
            docs_excerpt=docs_excerpt[:4000] or "(no strategic docs found)",
        )
        try:
            response = await provider.chat(prompt)
        except (OSError, RuntimeError) as exc:
            logger.warning(
                "Domain research failed (%s) — proceeding without brief", exc,
            )
            return ResearchBrief(
                question="domain identification",
                mode="domain",
                synthesis="",
                confidence="low",
            )

        # Treat Error-prefixed content as failure too — not every
        # provider sets is_error=True on CLI failure; some return
        # `content="Error: ..."` with is_error=False, which would
        # otherwise get cached as a legit brief for 7 days.
        content = response.content.strip()
        if (
            response.is_error
            or not content
            or content.startswith("Error:")
        ):
            logger.warning(
                "Domain research returned empty/error — proceeding without brief",
            )
            return ResearchBrief(
                question="domain identification",
                mode="domain",
                synthesis="",
                confidence="low",
                cost_usd=response.cost_usd,
            )

        brief = ResearchBrief(
            question="domain identification",
            mode="domain",
            synthesis=content,
            confidence="medium",
            cost_usd=response.cost_usd,
        )
        _save_cached_brief(cache_path, brief, context_hash)
        return brief

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


# --- Brief caching ---

_BRIEF_HEADER = "<!-- sentinel-domain-brief"


def _hash_context(project_type: str, readme: str, docs: str) -> str:
    """Stable hash of the inputs that would materially change the brief."""
    payload = f"{project_type}|||{readme[:1500]}|||{docs[:3000]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_cached_brief(
    cache_path: Path, context_hash: str,
) -> ResearchBrief | None:
    """Return the cached brief if fresh AND context hash matches, else None."""
    import datetime as _dt

    if not cache_path.exists():
        return None
    try:
        raw = cache_path.read_text(encoding="utf-8")
    except OSError:
        return None

    lines = raw.splitlines()
    if not lines or not lines[0].startswith(_BRIEF_HEADER):
        return None
    # Header format: <!-- sentinel-domain-brief hash=<h> generated=<iso> -->
    header = lines[0]
    if f"hash={context_hash}" not in header:
        return None
    try:
        generated = header.split("generated=", 1)[1].split()[0]
        ts = _dt.datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except (IndexError, ValueError):
        return None

    age = _dt.datetime.now(_dt.UTC) - ts
    if age > _dt.timedelta(days=DOMAIN_BRIEF_TTL_DAYS):
        return None

    body = "\n".join(lines[1:]).strip()
    return ResearchBrief(
        question="domain identification",
        mode="domain",
        synthesis=body,
        confidence="medium",
    )


def _save_cached_brief(
    cache_path: Path, brief: ResearchBrief, context_hash: str,
) -> None:
    """Write the brief to disk with a metadata header for cache lookup."""
    import datetime as _dt

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        generated = _dt.datetime.now(_dt.UTC).isoformat()
        header = (
            f"{_BRIEF_HEADER} hash={context_hash} generated={generated} -->"
        )
        cache_path.write_text(
            f"{header}\n{brief.synthesis}\n", encoding="utf-8",
        )
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not cache domain brief: %s", exc)
