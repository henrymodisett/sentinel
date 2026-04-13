"""
Monitor role — scans the codebase through lenses.

Owns the prompt logic for how to evaluate a project. Both `sentinel scan`
(CLI) and `Loop.cycle()` call monitor.assess() — single source of truth
for how project health is evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — used at runtime in load_lenses()
from typing import TYPE_CHECKING

from sentinel.state import ProjectState

if TYPE_CHECKING:
    from sentinel.providers.router import Router


@dataclass
class ScanResult:
    state: ProjectState = field(default_factory=ProjectState)
    raw_response: str = ""
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def load_lenses(project_path: Path, enabled: list[str]) -> dict[str, str]:
    """Load lens content from the project's lenses/ directory."""
    lenses: dict[str, str] = {}
    for lens_name in enabled:
        for subdir in ["universal", "conditional"]:
            lens_file = project_path / "lenses" / subdir / f"{lens_name}.md"
            if lens_file.exists():
                lenses[lens_name] = lens_file.read_text()
                break
    return lenses


def build_scan_prompt(state: ProjectState, lenses: dict[str, str]) -> str:
    """Build the prompt for the monitor LLM.

    Single source of truth for how project health is evaluated.
    """
    lens_section = ""
    for _name, content in lenses.items():
        lens_section += f"\n---\n{content}\n"

    return f"""\
You are Sentinel's Monitor. Assess this project's health \
through the analytical lenses provided.

## Project State

**Project**: {state.name}
**Branch**: {state.branch}
**Uncommitted changes**: {state.uncommitted_files}

### Recent commits
```
{state.recent_commits}
```

### File structure
```
{state.file_tree[:2000]}
```

### CLAUDE.md (project context)
```
{state.claude_md[:2000]}
```

### Test results
Tests passed: {state.tests_passed}
```
{state.test_output[:1500]}
```

### Lint results
Lint clean: {state.lint_clean}
```
{state.lint_output[:500]}
```

## Lenses
{lens_section}

## Your Task

Evaluate this project through EACH active lens. For each lens, provide:
1. A score (0-100)
2. Top issues found (if any)
3. Highlights (things done well)

Then provide:
- **Overall health score** (0-100, weighted average)
- **Top 3 recommended actions** ranked by impact

Be specific. Reference actual files and patterns, not generic advice.
Format your response clearly with markdown headers for each lens.
"""


class Monitor:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def assess(
        self, state: ProjectState, lenses: dict[str, str],
    ) -> ScanResult:
        """Scan the project through active lenses via the monitor provider."""
        prompt = build_scan_prompt(state, lenses)
        response = await self.router.chat("monitor", prompt)
        return ScanResult(
            state=state,
            raw_response=response.content,
            model=response.model,
            provider=response.provider,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
        )
