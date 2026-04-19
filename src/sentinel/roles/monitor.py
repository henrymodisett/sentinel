"""
Monitor role — multi-step project assessment.

Pipeline:
  1. EXPLORE — read the project, generate custom lenses (JSON-validated)
  2. EVALUATE — run each lens evaluation in parallel (JSON-validated)
  3. SYNTHESIZE — produce the final multidisciplinary report

Fails loudly when any step breaks — no silent fallback to a degraded result.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — runtime use in _load/_save_locked_lenses
from typing import TYPE_CHECKING

from sentinel.state import ProjectState  # noqa: TC001 — used at runtime in dataclasses

if TYPE_CHECKING:
    from sentinel.providers.router import Router

logger = logging.getLogger(__name__)


@dataclass
class Lens:
    """A custom analytical lens generated for this specific project.

    ``scope`` is an optional list of path globs. When non-empty, the
    lens only "sees" files matching one of the globs during evaluation
    — used to keep app-runtime constraints (e.g. "no cloud LLMs") from
    leaking into dev-toolchain config (e.g. ``.sentinel/config.toml``,
    ``setup.sh``). Empty (default) means global, matching pre-2026-04-19
    behavior. Autumn-mail dogfood cycle 4 (Finding F2) surfaced the
    conflation that motivated this field.
    """

    name: str
    description: str
    what_to_look_for: str
    questions: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)


@dataclass
class LensEvaluation:
    """Evaluation of a project through one lens."""

    lens_name: str
    score: int = 0  # 0-100
    top_finding: str = ""
    findings: str = ""
    recommended_tasks: list[str] = field(default_factory=list)
    error: str | None = None  # set if evaluation failed


@dataclass
class ScanResult:
    """Full scan result — output of the multi-step pipeline."""

    project_summary: str = ""
    lenses: list[Lens] = field(default_factory=list)
    evaluations: list[LensEvaluation] = field(default_factory=list)
    overall_score: int = 0
    strengths: list[str] = field(default_factory=list)
    critical_risks: list[str] = field(default_factory=list)
    top_actions: list[dict] = field(default_factory=list)
    raw_report: str = ""
    # Status
    ok: bool = False
    error: str | None = None
    n_lenses_failed: int = 0  # lenses that errored during evaluation
    # Usage tracking
    model: str = ""
    provider: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0


# --- Schemas for structured output ---

EXPLORE_SCHEMA = {
    "type": "object",
    "properties": {
        "project_summary": {
            "type": "string",
            "description": (
                "2-3 paragraph summary demonstrating deep understanding of "
                "what this project is, what it's trying to become, what "
                "makes it unique, and what matters most for its success."
            ),
        },
        "lenses": {
            "type": "array",
            "description": (
                "Custom lenses for evaluating THIS specific project. Each "
                "lens represents one advisor's perspective in the team you "
                "would assemble for this project."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Short kebab-case lens name, ideally 1-3 words, "
                            "verb-ish or role-ish. Good: ships-cleanly, "
                            "terminal-correctness, cost-awareness, adoption. "
                            "Bad (too long): developer-experience-and-"
                            "adoption-specialist-engineer."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "One sentence: what this lens evaluates "
                            "and why it matters here"
                        ),
                    },
                    "what_to_look_for": {
                        "type": "string",
                        "description": (
                            "2-3 sentences of specific things to examine, "
                            "grounded in this project"
                        ),
                    },
                    "questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 key questions this lens should answer",
                    },
                },
                "required": ["name", "description", "what_to_look_for", "questions"],
                "additionalProperties": False,
            },
            "minItems": 5,
            "maxItems": 10,
        },
    },
    "required": ["project_summary", "lenses"],
    "additionalProperties": False,
}

EVALUATE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Score from 0 to 100",
        },
        "top_finding": {
            "type": "string",
            "description": "One-sentence top finding for this lens",
        },
        "findings": {
            "type": "string",
            "description": (
                "Detailed findings through this lens — what you found, good "
                "and bad, with specific file references and evidence."
            ),
        },
        "recommended_tasks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 specific, actionable work items for this lens",
            "minItems": 1,
            "maxItems": 6,
        },
    },
    "required": ["score", "top_finding", "findings", "recommended_tasks"],
    "additionalProperties": False,
}

SYNTHESIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Overall health score, weighted by importance to this project",
        },
        "summary": {
            "type": "string",
            "description": "One-paragraph executive summary",
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-3 things this project does exceptionally well",
        },
        "critical_risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-3 things that could seriously hurt this project",
        },
        "top_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "rationale": {
                                    "type": "string",
                                    "description": "One-line reason why this file is touched",
                                },
                            },
                            "required": ["path", "rationale"],
                            "additionalProperties": False,
                        },
                        "description": "Files to be touched, each with a one-line rationale",
                    },
                    "impact": {"type": "string"},
                    "lens": {"type": "string", "description": "Which lens surfaced this"},
                    "kind": {
                        "type": "string",
                        "enum": ["refine", "expand"],
                        "description": (
                            "'refine' = improves what exists (bug fix, test, "
                            "performance, refactor, docs, security hardening). "
                            "ALWAYS safe to execute autonomously. "
                            "'expand' = adds new capability/scope (new feature, "
                            "new endpoint, new config surface, new integration). "
                            "Requires user approval before execution."
                        ),
                    },
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Testable conditions that must hold after the work is done. "
                            "Each item should be concrete and verifiable "
                            "(e.g. 'uv run pytest exits 0', 'output contains X')."
                        ),
                        "minItems": 1,
                    },
                    "verification": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Shell commands a coder can run to verify the work is correct "
                            "(e.g. 'uv run pytest', 'uv run ruff check src/')."
                        ),
                        "minItems": 1,
                    },
                    "out_of_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Things explicitly NOT part of this work item, "
                            "to prevent scope creep."
                        ),
                    },
                },
                "required": [
                    "title", "why", "impact", "lens", "kind",
                    "acceptance_criteria", "verification", "out_of_scope",
                ],
                "additionalProperties": False,
            },
            "description": "Top 5 recommended actions, prioritized by impact",
            "minItems": 1,
            "maxItems": 8,
        },
    },
    "required": ["overall_score", "summary", "strengths", "critical_risks", "top_actions"],
    "additionalProperties": False,
}


# --- Prompts ---

EXPLORE_PROMPT = """\
You are taking over this project. You are now responsible for making it successful.

Build a deep, first-principles understanding of:
- What this project IS (domain, purpose, users, context)
- What it's trying to BECOME (trajectory, goals, ambitions)
- What MATTERS MOST for this specific project to succeed
- What makes this project UNIQUE

Then ask yourself: "What team would I assemble to advise me on this project?"

For a high-frequency trading system: quant, reliability engineer, risk officer.
For a VC-backed consumer app: product strategist, designer, growth marketer.
For a passion-project developer tool: senior engineers focused on craft.

Generate lenses ONLY for the advisors this project actually needs.
Don't pad with irrelevant perspectives. Don't skip what matters.

Lens rules:
- Aim for 6-8 lenses for substantive projects. Never fewer than 5.
- Use SHORT names (1-3 words, kebab-case, verb-ish or role-ish).
  Good: "ships-cleanly", "terminal-correctness", "cost-awareness",
  "adoption", "risk-surface", "craft". Bad: long phrases with "and",
  "specialist", "lead", "engineer", "architect" tacked on.
- No two lenses should cover the same scope. If two would evaluate the
  same files, consolidate them.
- REQUIRED: at least one lens should be non-engineering — product,
  operations, distribution, adoption, DX, strategy. Not every lens can
  be "senior engineer looking at code."

## Project Context

### Goals (from user)
{goals_md}

### CLAUDE.md
```
{claude_md}
```

### README
```
{readme}
```

### Strategic project documentation
These are the project's own planning, architecture, and vision docs
(INVESTMENT_THESIS, SYSTEM_ARCHITECTURE, *_PLAN.md, etc.). Read them
to understand what the project IS and what it's trying to BECOME —
they usually encode more signal about priorities than the code does.
{project_docs}

### Domain expertise brief
Pre-scan research on the subject-matter domain this project operates
in. Use this to generate lenses that care about what MATTERS in this
domain — e.g. for a trading system, lenses about calibration +
adverse selection > generic "testing" lenses. If the brief is empty,
lens generation should lean harder on the strategic docs above.
{domain_brief}

### Recent commits
```
{recent_commits}
```

### File structure
```
{file_tree}
```

### Git state
Branch: {branch} | Uncommitted: {uncommitted_files}

### Available tools on this machine
These CLIs are installed and on PATH — the Coder role can invoke them
during execution. Factor this into which lenses you generate (e.g.
generate a deploy-ops lens if deploy CLIs are present):
{installed_tools}

### Test results
{test_output}

### Lint results
{lint_output}
"""


EVALUATE_PROMPT = """\
You are evaluating "{project_name}" through the lens of **{lens_name}**.

## About this project
{project_summary}

## This Lens: {lens_name}
{lens_description}

### What to look for
{what_to_look_for}

### Key questions
{questions}

## Project State
Branch: {branch} | Uncommitted: {uncommitted_files}

### File structure
```
{file_tree}
```

### CLAUDE.md context
```
{claude_md}
```

### Test results
{test_output}

Be a domain expert, not a generic auditor. Reference specific files.
Strong takes backed by evidence.
"""


SYNTHESIZE_PROMPT = """\
You are producing the final assessment for "{project_name}".

## Project Summary
{project_summary}

## Lens Evaluations
{lens_evaluations}

Synthesize into a final report. Prioritize by business impact to THIS project,
not by code neatness. A silent failure in a trading loop matters more than a
long function. A missing test for a revenue-critical path matters more than
test coverage percentage.

## Classifying actions: refine vs expand

CRITICAL: Every recommended action must be classified as `refine` or `expand`.

**refine** (safe to execute autonomously):
- Fixing bugs, silent failures, race conditions
- Adding tests, improving coverage, docs
- Refactoring for clarity without changing behavior
- Performance improvements to existing code paths
- Security hardening of existing surfaces
- Making timeouts/limits configurable without changing defaults
- Removing dead code, unused deps, debug prints

**expand** (requires user approval — sentinel will NOT auto-execute these):
- Adding new features, endpoints, or user-facing capabilities
- Adding new CLI commands or flags that weren't requested
- New integrations with external services (Linear, Slack, etc.)
- New modules/packages that change the project's scope
- New configuration surfaces that expose new behavior

Rule of thumb: does this change what the system CAN do, or does it improve
what it ALREADY does? If "can do" → expand. If "already does" → refine.

**Scope check — framework-level work is `expand` regardless of intent.**
The Coder role executes each action in a single Claude Code session with
a ~20-turn cap. Anything that looks like "implement and enforce X across
all components", "develop a centralized framework for Y", "design a
comprehensive Z system", or touches more than ~5 unrelated files MUST be
classified as `expand` — not because of what it does, but because the
Coder cannot finish it in one session. Users should split these into
smaller refinements themselves, or approve the expansion as a research
task that produces a plan, not an implementation. A "refine" that the
Coder quits halfway is worse than an "expand" the user rejects.

When unsure, classify as `expand`. Unapproved refinement is fine.
Unapproved expansion is scope creep. Half-finished refinement is the worst
of both — it leaves commits nobody asked for on branches nobody reviews.

## Sharp work items — required fields per top_action

Every top_action MUST include these fields with high-signal content:

**files** — list of {{path, rationale}} objects. Each entry is a specific file
path with a one-line reason it must be touched. Example:
  [{{"path": "src/sentinel/roles/monitor.py",
    "rationale": "owns SYNTHESIZE_SCHEMA — schema change goes here"}}]

**acceptance_criteria** — list of testable conditions that must hold after the
work is done. Use concrete, verifiable wording:
  - "uv run pytest exits 0"
  - "uv run ruff check src/ tests/ exits 0"
  - "scan output contains **Acceptance criteria:** section"
NOT vague wording like "the code is better" or "tests are improved".

**verification** — list of shell commands the coder can run to verify correctness.
At minimum include the test and lint commands for this project:
  - "uv run pytest"
  - "uv run ruff check src/ tests/"

**out_of_scope** — list of things explicitly NOT part of this work item to
prevent scope creep. May be empty list [] if nothing needs bounding, but
should list any tempting-but-out-of-scope areas when the item is near other
concerns.
"""


# --- Progress callback type ---

ProgressCallback = Callable[[str, dict], None]
"""Callback signature: (event_type, event_data) -> None.

Event types:
  - 'step_start': {'step': 'explore' | 'evaluate' | 'synthesize', 'message': str}
  - 'lens_generated': {'lenses': list[Lens]}
  - 'lens_start': {'lens_name': str, 'index': int, 'total': int}
  - 'lens_done': {'lens_name': str, 'score': int}
  - 'lens_failed': {'lens_name': str, 'error': str}
  - 'step_done': {'step': str, 'cost_usd': float}
"""


def _build_explore_prompt(state: ProjectState) -> str:
    """Render EXPLORE_PROMPT from a ProjectState.

    Single source of truth — both the fresh-scan and locked-lens paths
    call this so no caller can drift out of sync when the template gains
    a new field.
    """
    return EXPLORE_PROMPT.format(
        goals_md=state.goals_md[:2000] if state.goals_md else "(no goals.md set)",
        claude_md=state.claude_md[:3000],
        readme=state.readme[:2000],
        project_docs=state.project_docs or "(no strategic docs discovered)",
        domain_brief=state.domain_brief or "(no domain brief — research step skipped or failed)",
        recent_commits=state.recent_commits,
        file_tree=state.file_tree[:2000],
        branch=state.branch,
        uncommitted_files=state.uncommitted_files,
        installed_tools=state.installed_tools or "(not probed)",
        test_output=state.test_output[:1000],
        lint_output=state.lint_output[:500],
    )


def _load_locked_lenses(project_path: Path) -> list[Lens] | None:
    """Read .sentinel/lenses.md if it exists — user-approved lens set.

    Locked lenses enable trend tracking (same lens scored across scans)
    and avoid re-generating the same lenses every run.
    """
    import re

    lenses_file = project_path / ".sentinel" / "lenses.md"
    if not lenses_file.exists():
        return None

    content = lenses_file.read_text()
    lenses: list[Lens] = []

    # Parse markdown — each lens is an H2 section with description,
    # "### What to look for", and "### Questions" subsections.
    sections = re.split(r"^## ", content, flags=re.MULTILINE)[1:]  # skip preamble
    for section in sections:
        lines = section.strip().splitlines()
        if not lines:
            continue
        name = lines[0].strip()
        # Skip non-lens H2 sections (e.g. "How to edit")
        if not re.match(r"^[a-z][a-z0-9-]*$", name):
            continue

        description = ""
        what_to_look_for = ""
        questions: list[str] = []
        scope: list[str] = []

        mode = "description"
        for line in lines[1:]:
            s = line.strip()
            if s.startswith("### What to look for"):
                mode = "look"
                continue
            if s.startswith("### Questions"):
                mode = "q"
                continue
            if s.startswith("### Scope"):
                # Optional path-glob list — when non-empty, restricts
                # which files the evaluator considers for this lens.
                # Absent section = global (matches pre-scope behavior).
                mode = "scope"
                continue
            if s.startswith("###"):
                mode = "other"
                continue
            if mode == "description" and s:
                description += (" " if description else "") + s
            elif mode == "look" and s:
                what_to_look_for += (" " if what_to_look_for else "") + s
            elif mode == "q" and s.startswith("- "):
                questions.append(s[2:].strip())
            elif mode == "scope" and s.startswith("- "):
                scope.append(s[2:].strip())

        if name and description:
            lenses.append(Lens(
                name=name,
                description=description,
                what_to_look_for=what_to_look_for or description,
                questions=questions,
                scope=scope,
            ))

    return lenses if lenses else None


def _filter_file_tree_by_scope(file_tree: str, scope: list[str]) -> str:
    """Return only the lines of ``file_tree`` matching one of ``scope``.

    ``file_tree`` is the find-style newline-separated listing built by
    ``state.gather_state`` (paths typically prefixed with ``./``).
    ``scope`` is a list of path globs (e.g. ``Sources/**``).
    Empty ``scope`` returns ``file_tree`` unchanged — that's how a lens
    declares "I'm global."

    Glob semantics use ``fnmatch.fnmatchcase`` over the leading-``./``-
    stripped path. This treats ``*`` as matching across path separators
    (the same loose semantics ``fnmatch`` always has), so a scope of
    ``Sources/**`` matches every file under ``Sources/`` regardless of
    nesting depth — which is the intuitive operator expectation for
    keeping app-runtime lenses out of dev-toolchain config files.

    Used by the monitor's per-lens evaluator to keep app-runtime lenses
    (e.g. privacy-compliance "no cloud LLMs") from seeing dev-toolchain
    files (e.g. ``.sentinel/config.toml``) that legitimately reference
    a cloud LLM for review purposes.
    """
    if not scope:
        return file_tree
    if not file_tree.strip():
        return file_tree

    import fnmatch

    def _matches_any(path_str: str) -> bool:
        # Strip leading "./" since find emits it but globs typically don't.
        cleaned = path_str.removeprefix("./")
        if not cleaned:
            return False
        for glob in scope:
            if fnmatch.fnmatchcase(cleaned, glob):
                return True
        return False

    kept = [line for line in file_tree.splitlines() if _matches_any(line.strip())]
    return "\n".join(kept)


def _save_locked_lenses(
    project_path: Path, lenses: list[Lens],
) -> Path:
    """Persist lenses to .sentinel/lenses.md for reuse on future scans."""
    from datetime import datetime

    lenses_dir = project_path / ".sentinel"
    lenses_dir.mkdir(exist_ok=True)
    path = lenses_dir / "lenses.md"

    # Don't overwrite existing — respects user edits
    if path.exists():
        return path

    timestamp = datetime.now().strftime("%Y-%m-%d")
    lines = [
        "# Sentinel Lenses",
        "",
        f"*Generated {timestamp}. Edit freely — sentinel reuses these on every scan.*",
        "",
        "Delete this file to regenerate from scratch on the next scan.",
        "Add/remove/rename lenses as you like — sentinel will use what you write here.",
        "",
        "Each lens may declare an optional `### Scope` section listing path",
        "globs (e.g. `Sources/**`). When present, the lens only evaluates",
        "files matching those globs — useful for keeping app-runtime",
        "constraints (e.g. \"no cloud LLMs\") from leaking into dev-toolchain",
        "config (e.g. `.sentinel/config.toml`, `setup.sh`). Omit the section",
        "for global lenses (the default).",
        "",
        "---",
        "",
    ]

    for lens in lenses:
        lines.append(f"## {lens.name}")
        lines.append("")
        lines.append(lens.description)
        lines.append("")
        lines.append("### What to look for")
        lines.append("")
        lines.append(lens.what_to_look_for)
        lines.append("")
        if lens.questions:
            lines.append("### Questions")
            lines.append("")
            for q in lens.questions:
                lines.append(f"- {q}")
            lines.append("")
        if lens.scope:
            # Path globs constraining which files the evaluator
            # considers for this lens — empty/absent means global.
            lines.append("### Scope")
            lines.append("")
            for glob in lens.scope:
                lines.append(f"- {glob}")
            lines.append("")
        lines.append("---")
        lines.append("")

    path.write_text("\n".join(lines))
    return path


class Monitor:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def assess(
        self,
        state: ProjectState,
        on_progress: ProgressCallback | None = None,
    ) -> ScanResult:
        """Run the full multi-step scan pipeline with structured output."""
        from sentinel.journal import set_current_role
        set_current_role("monitor")
        result = ScanResult()

        def emit(event: str, data: dict) -> None:
            if on_progress:
                on_progress(event, data)

        # --- Step 0: DOMAIN BRIEF (LEARN phase) ---
        # Before lens generation, ask the Researcher to build a
        # subject-matter brief so the evaluator knows what "good"
        # looks like in this domain. Cached to .sentinel/ with a
        # 7-day TTL so most scans skip the research call. Failure
        # is non-fatal — lens generation still runs with empty
        # domain context, same as pre-LEARN-phase behavior.
        from sentinel.roles.researcher import Researcher
        try:
            researcher = Researcher(self.router)
            brief = await researcher.domain_brief(
                project_path=state.path,
                project_type=state.project_type,
                project_name=state.name or Path(state.path or ".").name,
                readme_excerpt=state.readme,
                docs_excerpt=state.project_docs,
            )
            # Researcher.domain_brief sets the role ContextVar to
            # "researcher" — restore it to "monitor" so the lens evals
            # that follow are attributed correctly in the journal.
            set_current_role("monitor")
            state.domain_brief = brief.synthesis
            if brief.cost_usd > 0:
                result.total_cost_usd += brief.cost_usd
                emit("domain_brief", {
                    "synthesis_len": len(brief.synthesis),
                    "cost_usd": brief.cost_usd,
                })
        except (RuntimeError, OSError) as exc:
            logger.warning("Domain brief step skipped: %s", exc)

        # --- Step 1: EXPLORE + GENERATE LENSES ---
        # Per-call routing: each step (explore / evaluate_lens / synthesize)
        # passes a task hint so the router can override the configured
        # model when a rule applies (e.g. synthesize → gemini-2.5-pro).
        # Without hints the configured default is used unchanged.
        provider = self.router.get_provider("monitor")

        # Check for locked lenses first (reuse across scans for trend tracking)
        project_path_obj = Path(state.path) if state.path else None
        locked = (
            _load_locked_lenses(project_path_obj)
            if project_path_obj else None
        )

        if locked:
            emit("step_start", {
                "step": "explore",
                "message": (
                    f"Using {len(locked)} locked lenses from "
                    f".sentinel/lenses.md..."
                ),
            })
            # Still need the project summary — prompt just for that
            summary_prompt = _build_explore_prompt(state)
            # Simple summary request when lenses are already locked
            summary_response = await provider.chat(
                summary_prompt
                + "\n\nJust give me the project_summary paragraph — "
                "2-3 paragraphs of what this project is, what matters, what makes it unique. "
                "No JSON, no lens list.",
            )
            result.model = summary_response.model
            result.provider = summary_response.provider
            result.total_input_tokens += summary_response.input_tokens
            result.total_output_tokens += summary_response.output_tokens
            result.total_cost_usd += summary_response.cost_usd
            result.project_summary = summary_response.content
            result.lenses = locked
            emit("lens_generated", {"lenses": result.lenses})
            emit("step_done", {"step": "explore", "cost_usd": summary_response.cost_usd})
        else:
            emit("step_start", {
                "step": "explore",
                "message": "Exploring project and generating custom lenses...",
            })

            explore_prompt = _build_explore_prompt(state)

            explore_provider = self.router.get_provider(
                "monitor", task="explore", prompt_size=len(explore_prompt),
            )
            parsed, response = await explore_provider.chat_json(
                explore_prompt, EXPLORE_SCHEMA,
            )

            result.model = response.model
            result.provider = response.provider
            result.total_input_tokens += response.input_tokens
            result.total_output_tokens += response.output_tokens
            result.total_cost_usd += response.cost_usd

            if not parsed or "lenses" not in parsed:
                result.error = (
                    f"Lens generation failed. Provider returned non-schema response. "
                    f"First 200 chars: {response.content[:200]}"
                )
                logger.error(result.error)
                return result

            result.project_summary = parsed.get("project_summary", "")
            for lens_data in parsed["lenses"]:
                result.lenses.append(Lens(
                    name=lens_data["name"],
                    description=lens_data["description"],
                    what_to_look_for=lens_data["what_to_look_for"],
                    questions=lens_data.get("questions", []),
                ))

            # Save the generated lenses so future scans reuse them
            if project_path_obj:
                _save_locked_lenses(project_path_obj, result.lenses)

            emit("lens_generated", {"lenses": result.lenses})
            emit("step_done", {"step": "explore", "cost_usd": response.cost_usd})

        # --- Step 2: EVALUATE (parallel) ---
        emit("step_start", {
            "step": "evaluate",
            "message": f"Evaluating through {len(result.lenses)} lenses in parallel...",
        })

        async def evaluate_one(lens: Lens, index: int) -> LensEvaluation:
            emit("lens_start", {
                "lens_name": lens.name,
                "index": index,
                "total": len(result.lenses),
            })
            # Scope filter: when the lens declares an explicit scope,
            # narrow the file_tree to matching paths so the evaluator
            # doesn't consider out-of-scope files. Unscoped lenses see
            # the full tree (legacy behavior). Cycle-4 finding F2
            # showed why this matters: an app-runtime "no cloud LLMs"
            # constraint was applied to dev-toolchain config that
            # legitimately uses a cloud reviewer.
            scoped_tree = _filter_file_tree_by_scope(
                state.file_tree, lens.scope,
            )
            eval_prompt = EVALUATE_PROMPT.format(
                project_name=state.name,
                lens_name=lens.name,
                project_summary=result.project_summary[:1500],
                lens_description=lens.description,
                what_to_look_for=lens.what_to_look_for,
                questions="\n".join(f"- {q}" for q in lens.questions),
                branch=state.branch,
                uncommitted_files=state.uncommitted_files,
                file_tree=scoped_tree[:1500],
                claude_md=state.claude_md[:2000],
                test_output=state.test_output[:500],
            )

            eval_provider = self.router.get_provider(
                "monitor", task="evaluate_lens", prompt_size=len(eval_prompt),
            )
            parsed_eval, resp = await eval_provider.chat_json(
                eval_prompt, EVALUATE_SCHEMA,
            )
            result.total_input_tokens += resp.input_tokens
            result.total_output_tokens += resp.output_tokens
            result.total_cost_usd += resp.cost_usd

            # Retry once with a sharper prompt if the first attempt didn't
            # produce valid JSON. Providers like Gemini in read-only mode
            # sometimes emit prose instead of schema output.
            if not parsed_eval:
                retry_prompt = (
                    eval_prompt
                    + "\n\nOUTPUT REQUIREMENT: Return ONLY the JSON object. "
                    "No prose, no markdown, no explanation. Start with { and end with }."
                )
                parsed_eval, retry_resp = await eval_provider.chat_json(
                    retry_prompt, EVALUATE_SCHEMA,
                )
                result.total_input_tokens += retry_resp.input_tokens
                result.total_output_tokens += retry_resp.output_tokens
                result.total_cost_usd += retry_resp.cost_usd
                # Use the retry response for error reporting if it also failed
                if not parsed_eval:
                    resp = retry_resp

            if not parsed_eval:
                emit("lens_failed", {
                    "lens_name": lens.name,
                    "error": resp.content[:200],
                })
                return LensEvaluation(
                    lens_name=lens.name,
                    error=f"Evaluation failed: {resp.content[:200]}",
                )

            evaluation = LensEvaluation(
                lens_name=lens.name,
                score=parsed_eval.get("score", 0),
                top_finding=parsed_eval.get("top_finding", ""),
                findings=parsed_eval.get("findings", ""),
                recommended_tasks=parsed_eval.get("recommended_tasks", []),
            )
            emit("lens_done", {
                "lens_name": lens.name,
                "score": evaluation.score,
                "running_cost_usd": result.total_cost_usd,
            })
            return evaluation

        # Run all lens evaluations in parallel with a per-lens timeout
        # and exception isolation — one hung lens must not block the
        # pipeline or erase the six sibling lenses that already succeeded.
        # Touchstone dogfood hit this: a Gemini sub-call ran 13+ minutes
        # past its budget, and gather() kept waiting for it, so the
        # cycle died with zero persisted lens evaluations.
        #
        # The per-lens timeout is belt-and-suspenders relative to the
        # provider's own subprocess timeout: if subprocess-level cancellation
        # somehow fails to fire (Gemini CLI quirks, OS-level wedges), this
        # outer asyncio.wait_for still guarantees the task ends. Use 2x the
        # provider timeout to leave room for evaluate_one's own retry path.
        per_lens_timeout = provider.timeout_sec * 2

        async def evaluate_with_timeout(lens: Lens, index: int) -> LensEvaluation:
            try:
                return await asyncio.wait_for(
                    evaluate_one(lens, index), timeout=per_lens_timeout,
                )
            except TimeoutError:
                emit("lens_failed", {
                    "lens_name": lens.name,
                    "error": f"timed out after {per_lens_timeout}s",
                })
                return LensEvaluation(
                    lens_name=lens.name,
                    error=f"Evaluation timed out after {per_lens_timeout}s",
                )

        tasks = [
            evaluate_with_timeout(lens, i)
            for i, lens in enumerate(result.lenses, 1)
        ]
        # return_exceptions=True so one bad apple (unexpected exception,
        # not just timeout) doesn't cancel the others. Exceptions that
        # slip past evaluate_with_timeout's try/except become
        # LensEvaluation(error=...) entries; scan proceeds.
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        result.evaluations = [
            ev if isinstance(ev, LensEvaluation) else LensEvaluation(
                lens_name=result.lenses[i].name,
                error=f"Evaluation raised {type(ev).__name__}: {ev}",
            )
            for i, ev in enumerate(gathered)
        ]

        emit("step_done", {
            "step": "evaluate",
            "cost_usd": sum(
                e.score for e in result.evaluations  # placeholder
            ),
        })

        # --- Step 3: SYNTHESIZE ---
        emit("step_start", {
            "step": "synthesize",
            "message": "Synthesizing final report...",
        })

        evals_text = ""
        for ev in result.evaluations:
            if ev.error:
                evals_text += f"\n### {ev.lens_name}\n[evaluation failed: {ev.error}]\n"
            else:
                evals_text += (
                    f"\n### {ev.lens_name} (score: {ev.score}/100)\n"
                    f"**Top finding:** {ev.top_finding}\n\n"
                    f"{ev.findings}\n\n"
                    f"**Tasks:**\n"
                    + "\n".join(f"- {t}" for t in ev.recommended_tasks)
                    + "\n"
                )

        synth_prompt = SYNTHESIZE_PROMPT.format(
            project_name=state.name,
            project_summary=result.project_summary[:1500],
            lens_evaluations=evals_text[:10000],
        )

        # Compute overall_score from successful lens evaluations BEFORE
        # the synthesis call, so partial scans (where synthesis fails but
        # lenses succeeded) still carry a meaningful top-line number. The
        # LLM synthesis also returns a score but we've always overridden
        # it with this average — more robust and matches user expectations.
        successful = [e for e in result.evaluations if not e.error]
        n_failed = len(result.evaluations) - len(successful)
        if successful:
            result.overall_score = round(
                sum(e.score for e in successful) / len(successful),
            )
        result.n_lenses_failed = n_failed

        synth_provider = self.router.get_provider(
            "monitor", task="synthesize", prompt_size=len(synth_prompt),
        )
        parsed_synth, synth_resp = await synth_provider.chat_json(
            synth_prompt, SYNTHESIZE_SCHEMA,
        )
        result.total_input_tokens += synth_resp.input_tokens
        result.total_output_tokens += synth_resp.output_tokens
        result.total_cost_usd += synth_resp.cost_usd

        if not parsed_synth:
            # Synthesis failed but lens evaluations are already populated
            # in result.evaluations. Leave result.ok False so callers know
            # the scan is partial, but preserve everything we computed so
            # _persist_scan can still write a useful file (overall_score,
            # all lens findings, recommended tasks per lens). The caller
            # decides whether to persist and whether to exit non-zero.
            result.error = f"Synthesis failed: {synth_resp.content[:200]}"
            logger.error(result.error)
            return result

        # Synthesis-only field for truly-all-failed scans: if every lens
        # errored, fall back to the LLM's synthesis score so the top-line
        # isn't a flat 0.
        if not successful:
            result.overall_score = parsed_synth.get("overall_score", 0)

        result.strengths = parsed_synth.get("strengths", [])
        result.critical_risks = parsed_synth.get("critical_risks", [])
        result.top_actions = parsed_synth.get("top_actions", [])
        result.raw_report = parsed_synth.get("summary", "")
        result.ok = True

        emit("step_done", {
            "step": "synthesize",
            "cost_usd": synth_resp.cost_usd,
        })

        return result
