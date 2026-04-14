"""
Discover project-level documentation for the LEARN phase.

Monitor's existing explore prompt pulls CLAUDE.md + README.md only. That
misses the strategic context projects like sigint carry in files like
INVESTMENT_THESIS.md, SYSTEM_ARCHITECTURE.md, ANTI_CHASE_PLAN.md — exactly
the docs a new hire would read first to understand the vision before
proposing work.

This module walks the repo for doc-like files, ranks them by likely
strategic value, and returns short excerpts of the top N for the explore
prompt. The ranking is heuristic-first — we prioritize files whose names
signal "this is a strategy/architecture doc" over files whose names signal
"this is a changelog/license/boilerplate."

Deliberately keeps the LLM out of triage. A heuristic ranker is cheap,
deterministic, and transparent; a round-trip to pick docs would add cost
to every scan. If heuristics turn out to be wrong on a real project,
extend TIER_KEYWORDS rather than delegating to an LLM.
"""

from __future__ import annotations

import re
from pathlib import Path  # noqa: TC003 — used at runtime for rglob/read_text

# File extensions that carry documentation. Kept narrow — we don't want
# to sweep source code or data files. `.org` for Emacs users, `.adoc`
# for Asciidoc projects.
DOC_EXTENSIONS: tuple[str, ...] = (
    ".md", ".rst", ".adoc", ".txt", ".org",
)

# Directories that commonly hold project documentation. `.claude/` and
# `.sentinel/` are excluded upstream — those are agent config, not
# project docs.
DOC_DIRS: tuple[str, ...] = (
    "docs", "documentation", "doc", "wiki", "notes",
    "planning", "architecture", "design", "principles",
)

# Noise directories that may contain doc-like files but aren't project
# docs — third-party code, build output, local envs.
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "node_modules", ".next", "dist", "build",
    "__pycache__", ".pytest_cache", ".sentinel", ".claude",
    "vendor", ".toolkit-version",
})

# Tiered keywords that bias ranking. Higher tier = higher priority. Used
# as case-insensitive substring matches on the filename. Kept short and
# generic — project-specific terms shouldn't go here, the heuristic
# should work across domains.
TIER_KEYWORDS: dict[int, tuple[str, ...]] = {
    # Tier 3 — strongest strategic signal
    3: (
        "thesis", "vision", "roadmap", "charter", "principles",
        "architecture", "system_architecture", "design",
    ),
    # Tier 2 — project-specific planning and context
    2: (
        "plan", "phase", "goals", "strategy", "spec", "agents",
        "setup", "investment", "readme",
    ),
    # Tier 1 — operational or reference
    1: (
        "api", "integration", "deploy", "migration", "audit",
        "guide", "notes", "how-to", "howto",
    ),
}

# Filenames that are almost always noise for strategic context — high
# false-positive rate on substring matching otherwise.
DENY_FILENAMES: frozenset[str] = frozenset({
    "license", "license.md", "license.txt", "license.rst",
    "copying", "copying.md",
    "changelog", "changelog.md",
    "history", "history.md",
    "contributors", "contributors.md",
    "code_of_conduct", "code_of_conduct.md",
    "security", "security.md",  # mostly vulnerability-disclosure boilerplate
})


def _tier_for_filename(name: str) -> int:
    """Return the highest matching tier (0 = no match)."""
    lower = name.lower()
    for tier in (3, 2, 1):
        if any(kw in lower for kw in TIER_KEYWORDS[tier]):
            return tier
    return 0


def _iter_doc_candidates(project_path: Path) -> list[Path]:
    """Walk the project for doc-like files, skipping noise dirs.

    Limits walk depth to 3 levels so docs inside deeply nested vendored
    libraries don't sneak in. Project-level docs live at the top 1-2
    levels; 3 levels covers something like `agent/planning/thesis.md`.
    """
    candidates: list[Path] = []
    root_depth = len(project_path.resolve().parts)
    for path in project_path.rglob("*"):
        try:
            relative_parts = path.relative_to(project_path).parts
        except ValueError:
            continue
        if any(p in SKIP_DIRS for p in relative_parts):
            continue
        depth = len(path.resolve().parts) - root_depth
        if depth > 3:
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in DOC_EXTENSIONS:
            continue
        if path.name.lower() in DENY_FILENAMES:
            continue
        candidates.append(path)
    return candidates


def rank_docs(
    project_path: Path,
    max_docs: int = 8,
) -> list[tuple[Path, int]]:
    """Return the top `max_docs` doc paths ranked by strategic-signal tier.

    Ranking signals (in order):
    1. Filename keyword tier (strongest signal)
    2. Inside a doc directory (docs/, architecture/, planning/, ...)
    3. Root-level filename (README-style docs)
    4. Alphabetical (stable sort for same-tier ties)
    """
    candidates = _iter_doc_candidates(project_path)
    scored: list[tuple[Path, int]] = []
    for path in candidates:
        tier = _tier_for_filename(path.stem)
        # Boost for being in a recognized doc dir
        if any(part.lower() in DOC_DIRS for part in path.relative_to(project_path).parts):
            tier += 1
        # Boost for root-level files (README, AGENTS, etc.)
        if len(path.relative_to(project_path).parts) == 1:
            tier += 1
        if tier <= 0:
            continue
        scored.append((path, tier))
    # Sort: higher tier first, then alphabetical for stability
    scored.sort(key=lambda x: (-x[1], str(x[0])))
    return scored[:max_docs]


def _summarize_doc(path: Path, max_chars: int = 800) -> str:
    """Return the leading lines of a doc, capped at max_chars.

    Strips long code/data blocks (≥10 consecutive lines starting with
    whitespace or inside ``` fences) to keep the prompt dense with
    descriptive prose. Falls back to the first max_chars if stripping
    produces nothing useful.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Trim long fenced code blocks
    stripped = re.sub(
        r"```.*?```", "```(code block omitted)```", raw,
        flags=re.DOTALL,
    )
    stripped = stripped.strip()
    if not stripped:
        stripped = raw.strip()
    return stripped[:max_chars]


def discover_project_docs(
    project_path: Path,
    max_docs: int = 8,
    max_chars_per_doc: int = 800,
) -> str:
    """Return a prompt-ready string of the top strategic project docs.

    Shape:
        ### agent/INVESTMENT_THESIS.md
        <first 800 chars, code blocks stripped>

        ### docs/architecture.md
        <first 800 chars>

    Empty string when nothing relevant is found — callers must handle
    that case (the explore prompt renders a "(no strategic docs found)"
    fallback).
    """
    ranked = rank_docs(project_path, max_docs=max_docs)
    if not ranked:
        return ""
    sections: list[str] = []
    for path, _tier in ranked:
        excerpt = _summarize_doc(path, max_chars=max_chars_per_doc)
        if not excerpt:
            continue
        rel = path.relative_to(project_path)
        sections.append(f"### {rel}\n{excerpt}")
    return "\n\n".join(sections)
