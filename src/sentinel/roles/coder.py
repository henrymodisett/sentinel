"""
Coder role — executes work items by writing code.

Dispatches to Claude Code or Codex CLI's agentic mode. The coder
provider runs with full tool access (file edit, terminal, tests)
inside the target project directory.

Given: a WorkItem with title, description, files, acceptance criteria.
Produces: file changes + a commit on a feature branch.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel.providers.router import Router
    from sentinel.roles.planner import WorkItem


@dataclass
class ExecutionResult:
    work_item_id: str
    status: str  # "success", "partial", "failed"
    branch: str = ""
    files_changed: list[str] = field(default_factory=list)
    tests_passing: bool = False
    commit_sha: str | None = None
    error: str | None = None
    raw_output: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0


def _slug(title: str) -> str:
    """Turn a work item title into a git-safe branch slug."""
    import re
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:50]  # cap at 50 chars


def _run_git(
    args: list[str], cwd: str,
) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=cwd, timeout=30,
    )


def _files_changed(project_path: str) -> list[str]:
    """List files changed in the working tree."""
    result = _run_git(["status", "--porcelain"], project_path)
    files = []
    for line in result.stdout.strip().splitlines():
        # Format: "XY filename" where XY is status code
        if len(line) > 3:
            files.append(line[3:])
    return files


BUILD_PROMPT = """\
You are executing this work item for the {project_name} project.

## Work Item: {title}

**Type:** {type} | **Priority:** {priority} | **Complexity:** {complexity}/5

{description}

## Acceptance Criteria
{criteria}

## Files likely to be touched
{files}

## Risk
{risk}

## Your Task

1. Read the relevant files to understand the current state.
2. Make the minimum change that satisfies all acceptance criteria.
3. Run the project's tests to verify nothing is broken.
4. If tests fail, fix them before moving on.

Do NOT commit or push — the orchestrator will do that after review.
Do NOT refactor surrounding code unless the work item requires it.

Report what you changed and whether tests pass.
"""


class Coder:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def execute(
        self, work_item: WorkItem, project_path: str,
    ) -> ExecutionResult:
        """Execute a work item via the coder provider's agentic mode."""
        start = time.time()
        result = ExecutionResult(
            work_item_id=work_item.id,
            status="failed",
        )

        provider = self.router.get_provider("coder")
        if not provider.capabilities.agentic_code:
            result.error = (
                f"Provider {provider.name} doesn't support agentic code execution. "
                f"Assign coder role to claude or openai in .sentinel/config.toml."
            )
            return result

        # Create a feature branch before starting
        branch_name = f"sentinel/{work_item.type}/{_slug(work_item.title)}"
        result.branch = branch_name

        co_result = _run_git(["checkout", "-b", branch_name], project_path)
        if co_result.returncode != 0 and "already exists" not in co_result.stderr:
            result.error = f"Could not create branch {branch_name}: {co_result.stderr.strip()}"
            return result

        # Build the prompt
        criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "(none)"
        files = ", ".join(work_item.files) or "(let coder determine)"
        prompt = BUILD_PROMPT.format(
            project_name=Path(project_path).name,
            title=work_item.title,
            type=work_item.type,
            priority=work_item.priority,
            complexity=work_item.complexity,
            description=work_item.description,
            criteria=criteria,
            files=files,
            risk=work_item.risk or "(none noted)",
        )

        # Execute via provider's agentic code mode
        try:
            response = await provider.code(prompt, working_directory=project_path)
        except (OSError, subprocess.SubprocessError) as e:
            result.error = f"Coder execution failed: {e}"
            result.duration_ms = int((time.time() - start) * 1000)
            return result

        result.raw_output = response.content
        result.cost_usd = response.cost_usd

        if response.content.startswith("Error:"):
            result.error = response.content
            result.duration_ms = int((time.time() - start) * 1000)
            return result

        # Check what files actually changed
        changed = _files_changed(project_path)
        result.files_changed = changed

        if not changed:
            result.status = "failed"
            result.error = "Coder produced no file changes"
            result.duration_ms = int((time.time() - start) * 1000)
            return result

        # Run tests to verify
        from sentinel.state import _read_toolkit_command  # type: ignore[attr-defined]

        toolkit_config = Path(project_path) / ".toolkit-config"
        test_cmd = _read_toolkit_command(toolkit_config, "test_command")
        if test_cmd:
            try:
                test_result = subprocess.run(
                    test_cmd.split(), capture_output=True, text=True,
                    cwd=project_path, timeout=300,
                )
                result.tests_passing = test_result.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError):
                result.tests_passing = False
        else:
            result.tests_passing = True  # can't verify — assume ok

        result.status = "success" if result.tests_passing else "partial"
        result.duration_ms = int((time.time() - start) * 1000)
        return result
