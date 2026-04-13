"""
Monitor role — multi-step project assessment.

The monitor doesn't just check code quality. It understands the project,
generates custom analytical lenses, researches domain expertise, then
evaluates the project as a multidisciplinary PM would.

Pipeline:
  1. EXPLORE — read everything, understand what this project is
  2. GENERATE LENSES — create custom lenses tailored to this project
  3. RESEARCH — become a domain expert for each lens
  4. EVALUATE — scan through each lens with deep knowledge
  5. REPORT — synthesize into a multidisciplinary assessment
"""

from __future__ import annotations

import json
import logging
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
    findings: str = ""
    tasks: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    """Full scan result — the output of the multi-step pipeline."""

    project_summary: str = ""
    lenses: list[Lens] = field(default_factory=list)
    evaluations: list[LensEvaluation] = field(default_factory=list)
    overall_score: int = 0
    top_actions: list[str] = field(default_factory=list)
    raw_report: str = ""
    # Usage tracking
    model: str = ""
    provider: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0


# --- Step 1: EXPLORE ---

EXPLORE_PROMPT = """\
You are taking over this project. You are now responsible for making it successful.

First, read everything below and build a deep, first-principles understanding of:

- What this project IS (domain, purpose, users, context)
- What it's trying to BECOME (trajectory, goals, ambitions)
- What MATTERS MOST for this specific project to succeed
- What the team has been WORKING ON recently (commit patterns, momentum)
- What's UNIQUE about this project that generic advice would miss

Then ask yourself: **"What team would I assemble to advise me on this project?"**

For a high-frequency trading system, you'd want a quant, a reliability engineer,
a risk officer, and a compliance lawyer. You would NOT want a UX designer or a
GTM strategist — they're irrelevant.

For a VC-backed consumer app, you'd want a product strategist, a designer, a
growth marketer, an engineering lead, and someone who thinks about retention.
You would NOT want a formal verification expert or a protocol compliance
specialist.

For a passion-project developer tool with no business model, you'd want senior
engineers focused on code quality, craft, and user experience for developers.
You would NOT want a GTM strategist or a pricing expert.

The lenses you generate ARE that team. Each lens represents one advisor giving
you their perspective. Generate only the advisors this project actually needs.

## Project Context

### CLAUDE.md
```
{claude_md}
```

### README
```
{readme}
```

### Recent commits (last 10)
```
{recent_commits}
```

### File structure
```
{file_tree}
```

### Git state
Branch: {branch}
Uncommitted files: {uncommitted_files}

### Test results
{test_output}

### Lint results
{lint_output}

## Your Task

Write a 2-3 paragraph project summary that demonstrates deep understanding —
what it is, what it's trying to be, what makes it unique, what matters.

Then decide: what team of advisors does THIS project need to succeed?
Each lens = one advisor's perspective.

Output a JSON block with the lenses. Generate as many or as few as this project
actually needs — don't pad with irrelevant perspectives, don't skip things that
matter. A 3-lens scan for a simple library is fine. A 12-lens scan for a complex
production system is fine. Quality of judgment matters more than quantity.

Output format — write the summary as prose, then a JSON block:

```json
{{
  "lenses": [
    {{
      "name": "lens-name",
      "description": "One sentence: what this advisor evaluates and why they matter here",
      "what_to_look_for": "2-3 sentences: specific things to examine, grounded in this project",
      "questions": ["Question 1?", "Question 2?", "Question 3?"]
    }}
  ]
}}
```
"""


# --- Step 3: EVALUATE ---

EVALUATE_PROMPT = """\
You are evaluating the project "{project_name}" through the lens of **{lens_name}**.

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

## Your Task

Evaluate this project SPECIFICALLY through the {lens_name} lens.
Be a domain expert, not a generic auditor. Reference specific files,
patterns, and decisions.

Provide:
1. **Score** (0-100)
2. **Key findings** — what you found, good and bad, with specific file references
3. **Recommended tasks** — 2-4 specific, actionable work items for this lens

Be direct and opinionated. Strong takes backed by evidence.
"""


# --- Step 5: SYNTHESIZE ---

SYNTHESIZE_PROMPT = """\
You are a senior technical PM producing a final assessment of "{project_name}".

## Project Summary
{project_summary}

## Lens Evaluations
{lens_evaluations}

## Your Task

Synthesize all lens evaluations into a final report:

1. **Overall health score** (0-100, weighted by importance to this specific project)
2. **Strengths** — what this project does exceptionally well (2-3 points)
3. **Critical risks** — what could seriously hurt this project (2-3 points)
4. **Top 5 recommended actions** — prioritized by impact, mixing across lenses.
   Each action: what to do, why, which files, expected impact.

Think like a PM, not an auditor. Prioritize by business impact, not code neatness.
A silent failure in a trading loop matters more than a long function.
A missing test for a revenue-critical path matters more than test coverage percentage.
"""


def _extract_json(text: str) -> dict | None:
    """Extract a JSON block from LLM output."""
    # Find ```json ... ``` block
    start = text.find("```json")
    if start != -1:
        start = text.index("\n", start) + 1
        end = text.find("```", start)
        if end != -1:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass

    # Try finding raw JSON object
    start = text.find('{"lenses"')
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return None


class Monitor:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def assess(self, state: ProjectState) -> ScanResult:
        """Run the full multi-step scan pipeline."""
        result = ScanResult()

        # Step 1: EXPLORE + GENERATE LENSES
        logger.info("Step 1/3: Exploring project and generating lenses...")
        explore_prompt = EXPLORE_PROMPT.format(
            claude_md=state.claude_md[:3000],
            readme=state.readme[:2000],
            recent_commits=state.recent_commits,
            file_tree=state.file_tree[:2000],
            branch=state.branch,
            uncommitted_files=state.uncommitted_files,
            test_output=state.test_output[:1000],
            lint_output=state.lint_output[:500],
        )

        explore_response = await self.router.chat("monitor", explore_prompt)
        result.total_input_tokens += explore_response.input_tokens
        result.total_output_tokens += explore_response.output_tokens
        result.total_cost_usd += explore_response.cost_usd
        result.model = explore_response.model
        result.provider = explore_response.provider

        # Parse the response — summary is the prose, lenses are in JSON
        explore_text = explore_response.content
        json_data = _extract_json(explore_text)

        if json_data and "lenses" in json_data:
            # Extract summary (everything before the JSON block)
            json_start = explore_text.find("```json")
            if json_start == -1:
                json_start = explore_text.find('{"lenses"')
            result.project_summary = explore_text[:json_start].strip() if json_start > 0 else ""

            for lens_data in json_data["lenses"]:
                result.lenses.append(Lens(
                    name=lens_data.get("name", ""),
                    description=lens_data.get("description", ""),
                    what_to_look_for=lens_data.get("what_to_look_for", ""),
                    questions=lens_data.get("questions", []),
                ))
        else:
            # Fallback: use the whole response as summary, no structured lenses
            result.project_summary = explore_text
            result.raw_report = explore_text
            return result

        # Step 2: EVALUATE through each lens
        logger.info("Step 2/3: Evaluating through %d lenses...", len(result.lenses))
        for lens in result.lenses:
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

            eval_response = await self.router.chat("monitor", eval_prompt)
            result.total_input_tokens += eval_response.input_tokens
            result.total_output_tokens += eval_response.output_tokens
            result.total_cost_usd += eval_response.cost_usd

            result.evaluations.append(LensEvaluation(
                lens_name=lens.name,
                findings=eval_response.content,
            ))

        # Step 3: SYNTHESIZE
        logger.info("Step 3/3: Synthesizing final report...")
        evals_text = ""
        for ev in result.evaluations:
            evals_text += f"\n### {ev.lens_name}\n{ev.findings}\n"

        synth_prompt = SYNTHESIZE_PROMPT.format(
            project_name=state.name,
            project_summary=result.project_summary[:1500],
            lens_evaluations=evals_text[:8000],
        )

        synth_response = await self.router.chat("monitor", synth_prompt)
        result.total_input_tokens += synth_response.input_tokens
        result.total_output_tokens += synth_response.output_tokens
        result.total_cost_usd += synth_response.cost_usd

        result.raw_report = synth_response.content
        return result
