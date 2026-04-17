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

import logging
import os
import re
from pathlib import Path  # noqa: TC003 — used at runtime for os.walk/read_text

logger = logging.getLogger(__name__)

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
    "vendor", ".touchstone-version",
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

# Substring patterns in filenames that strongly signal secrets or
# auth-adjacent content. We refuse to read these into the LLM prompt
# even if they match a tier keyword — the risk of forwarding a
# `credentials.txt` or `api_keys.md` to an external provider is too
# high vs. the signal they'd provide. Case-insensitive substring match
# on the filename stem.
SECRET_PATTERNS: tuple[str, ...] = (
    "secret", "credential", "cred",
    "api_key", "apikey", "api-key",
    "token", "password", "passwd",
    ".env", "env.local", "env.production",
    "private_key", "privatekey", "private-key",
    "auth.txt", "authorization",
)


def _tier_for_filename(name: str) -> int:
    """Return the highest matching tier (0 = no match)."""
    lower = name.lower()
    for tier in (3, 2, 1):
        if any(kw in lower for kw in TIER_KEYWORDS[tier]):
            return tier
    return 0


def _looks_like_secret(filename: str) -> bool:
    """True if the filename pattern-matches a secret-adjacent name.

    Applied BEFORE tier scoring so a file matching a keyword tier still
    gets rejected when its name is suspicious. The risk of forwarding
    credentials.txt to an external LLM provider is not worth the
    potential signal.
    """
    lower = filename.lower()
    return any(pattern in lower for pattern in SECRET_PATTERNS)


def _iter_doc_candidates(project_path: Path) -> list[Path]:
    """Walk the project for doc-like files, pruning noise dirs.

    Uses os.walk so we can prune skipped dirs IN PLACE (modifying
    `dirs` stops the walk from descending into them). A naive rglob
    still descends into node_modules/.venv/vendor trees before
    filtering — a monorepo with tens of thousands of nested doc files
    would stall state gathering for minutes before we returned. Pruning
    up-front is O(top-level-dirs) not O(all-files).

    Walk errors (permission denied on a dir, etc.) are logged with
    path context. Silently ignoring them violates the "no silent
    failures" principle — operators should see which dir was skipped.

    Depth cap is 3 levels: project-level docs live at the top 1-2
    levels; 3 covers something like `agent/planning/thesis.md`.

    Also skips any dir whose name matches a secret pattern (e.g.
    `secrets/`, `credentials/`) — otherwise `secrets/README.md` would
    read through the filename filter since README is a valid doc name.
    """
    def _on_walk_error(err: OSError) -> None:
        logger.warning(
            "Doc discovery: could not enter %s (%s) — skipping",
            getattr(err, "filename", "?"), err,
        )

    candidates: list[Path] = []
    project_path_str = str(project_path)
    for dirpath, dirs, files in os.walk(project_path, onerror=_on_walk_error):
        # Prune noise dirs + secret-adjacent dirs BEFORE descending
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS and not _looks_like_secret(d)
        ]
        # Enforce depth cap
        relative = os.path.relpath(dirpath, project_path_str)
        depth = 0 if relative == "." else len(relative.split(os.sep))
        if depth >= 3:
            dirs[:] = []
            if depth > 3:
                continue
        for name in files:
            suffix = os.path.splitext(name)[1].lower()
            if suffix not in DOC_EXTENSIONS:
                continue
            lower_name = name.lower()
            if lower_name in DENY_FILENAMES:
                continue
            if _looks_like_secret(lower_name):
                # Secret-adjacent filename — never read, never forward
                # to an LLM, not even if it matches a doc keyword tier.
                continue
            candidates.append(Path(dirpath) / name)
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

    On read failure (permission denied, unicode decode beyond replace
    mode, etc.) logs with path context and returns "" — the caller
    drops the doc from the list. We don't raise because a single
    unreadable strategic doc shouldn't abort the whole scan.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "Could not read strategic doc %s (skipping): %s", path, exc,
        )
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
