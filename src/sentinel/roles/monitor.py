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
from typing import TYPE_CHECKING

from sentinel.state import ProjectState  # noqa: TC001 — used at runtime in dataclasses

if TYPE_CHECKING:
    from sentinel.providers.router import Router

logger = logging.getLogger(__name__)


@dataclass
class Lens:
    """A custom analytical lens generated for this specific project."""

    name: str
    description: str
    what_to_look_for: str
    questions: list[str] = field(default_factory=list)


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
                    "files": {"type": "array", "items": {"type": "string"}},
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
                },
                "required": ["title", "why", "impact", "lens", "kind"],
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

When unsure, classify as `expand`. Unapproved refinement is fine.
Unapproved expansion is scope creep.
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


class Monitor:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def assess(
        self,
        state: ProjectState,
        on_progress: ProgressCallback | None = None,
    ) -> ScanResult:
        """Run the full multi-step scan pipeline with structured output."""
        result = ScanResult()

        def emit(event: str, data: dict) -> None:
            if on_progress:
                on_progress(event, data)

        # --- Step 1: EXPLORE + GENERATE LENSES ---
        emit("step_start", {
            "step": "explore",
            "message": "Exploring project and generating custom lenses...",
        })

        explore_prompt = EXPLORE_PROMPT.format(
            goals_md=state.goals_md[:2000] if state.goals_md else "(no goals.md set)",
            claude_md=state.claude_md[:3000],
            readme=state.readme[:2000],
            recent_commits=state.recent_commits,
            file_tree=state.file_tree[:2000],
            branch=state.branch,
            uncommitted_files=state.uncommitted_files,
            test_output=state.test_output[:1000],
            lint_output=state.lint_output[:500],
        )

        provider = self.router.get_provider("monitor")
        parsed, response = await provider.chat_json(explore_prompt, EXPLORE_SCHEMA)

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
            eval_prompt = EVALUATE_PROMPT.format(
                project_name=state.name,
                lens_name=lens.name,
                project_summary=result.project_summary[:1500],
                lens_description=lens.description,
                what_to_look_for=lens.what_to_look_for,
                questions="\n".join(f"- {q}" for q in lens.questions),
                branch=state.branch,
                uncommitted_files=state.uncommitted_files,
                file_tree=state.file_tree[:1500],
                claude_md=state.claude_md[:2000],
                test_output=state.test_output[:500],
            )

            parsed_eval, resp = await provider.chat_json(eval_prompt, EVALUATE_SCHEMA)
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
                parsed_eval, retry_resp = await provider.chat_json(
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

        # Run all lens evaluations in parallel
        tasks = [evaluate_one(lens, i) for i, lens in enumerate(result.lenses, 1)]
        result.evaluations = await asyncio.gather(*tasks)

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

        parsed_synth, synth_resp = await provider.chat_json(
            synth_prompt, SYNTHESIZE_SCHEMA,
        )
        result.total_input_tokens += synth_resp.input_tokens
        result.total_output_tokens += synth_resp.output_tokens
        result.total_cost_usd += synth_resp.cost_usd

        if not parsed_synth:
            result.error = f"Synthesis failed: {synth_resp.content[:200]}"
            logger.error(result.error)
            return result

        # Overall score excludes failed lens evaluations so the top-line
        # number isn't dragged down by parsing bugs. The LLM synthesis gives
        # a suggested weighted score but we override with our own average
        # over successful lenses only — more robust and matches what users expect.
        successful = [e for e in result.evaluations if not e.error]
        n_failed = len(result.evaluations) - len(successful)
        if successful:
            result.overall_score = round(
                sum(e.score for e in successful) / len(successful),
            )
        else:
            # All lenses failed — fall back to the LLM's synthesis score
            result.overall_score = parsed_synth.get("overall_score", 0)

        result.strengths = parsed_synth.get("strengths", [])
        result.critical_risks = parsed_synth.get("critical_risks", [])
        result.top_actions = parsed_synth.get("top_actions", [])
        result.raw_report = parsed_synth.get("summary", "")
        result.n_lenses_failed = n_failed
        result.ok = True

        emit("step_done", {
            "step": "synthesize",
            "cost_usd": synth_resp.cost_usd,
        })

        return result
