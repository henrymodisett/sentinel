"""
Coder role — executes work items by writing code.

Dispatches to Claude Code or Codex CLI's agentic mode. The coder
provider runs with full tool access (file edit, terminal, tests)
inside the target project directory.

Given: a WorkItem with title, description, files, acceptance criteria.
Produces: file changes + a commit on a feature branch.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel.providers.interface import ChatResponse
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
    """List files changed in the working tree, excluding Sentinel artifacts.

    Files under `.sentinel/` are never counted as Coder changes — they
    belong to sentinel itself (transcripts, scans, proposals). Without
    this filter, a transcript written during THIS run would be picked
    up as a "changed file" and turn a no-op Coder run into a false
    success on projects that haven't gitignored .sentinel/.
    """
    result = _run_git(["status", "--porcelain"], project_path)
    files = []
    for line in result.stdout.strip().splitlines():
        # Format: "XY filename" where XY is status code
        if len(line) > 3:
            filename = line[3:]
            if filename.startswith(".sentinel/") or filename == ".sentinel":
                continue
            files.append(filename)
    return files


def _write_execution_transcript(
    project_path: str,
    work_item: WorkItem,
    prompt: str,
    response: ChatResponse | None,
    result: ExecutionResult,
    exception: BaseException | None = None,
) -> Path | None:
    """Persist a debuggable record of an execution attempt.

    Writes a Markdown file under .sentinel/executions/ with the prompt,
    the raw provider output (both content and stdout), stderr, duration,
    status, and any exception trace. This is the difference between a
    silent failure and one we can actually investigate.

    Returns the transcript path on success, None if the write itself
    failed (we don't want the transcript write to mask the real error).
    """
    try:
        executions_dir = Path(project_path) / ".sentinel" / "executions"
        executions_dir.mkdir(parents=True, exist_ok=True)
        timestamp = _dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
        filename = f"{timestamp}-{_slug(work_item.title)}.md"
        path = executions_dir / filename

        lines: list[str] = [
            f"# Execution Transcript — {work_item.title}",
            "",
            f"- **Work item ID:** {work_item.id}",
            f"- **Type:** {work_item.type}",
            f"- **Branch:** {result.branch or '(none — failed before branch)'}",
            f"- **Status:** {result.status}",
            f"- **Duration:** {result.duration_ms}ms",
            f"- **Cost:** ${result.cost_usd:.4f}",
            f"- **Files changed:** {len(result.files_changed)}",
        ]
        if result.error:
            lines += ["", "## Error", "", "```", result.error, "```"]
        if exception is not None:
            import traceback
            lines += [
                "", "## Exception", "",
                "```",
                "".join(traceback.format_exception(exception)).rstrip(),
                "```",
            ]
        if response is not None:
            lines += [
                "",
                "## Provider diagnostics",
                "",
                f"- is_error: {response.is_error}",
                f"- model: {response.model or '(unset)'}",
                f"- input_tokens: {response.input_tokens}",
                f"- output_tokens: {response.output_tokens}",
            ]
            if response.stderr:
                lines += [
                    "", "### stderr", "",
                    "```", response.stderr.rstrip(), "```",
                ]
            if response.raw_stdout and response.raw_stdout != response.content:
                lines += [
                    "", "### Raw stdout", "",
                    "```",
                    response.raw_stdout[:20000].rstrip(),
                    "```",
                ]
            lines += [
                "", "## Content (parsed)", "",
                "```",
                (response.content or "(empty)")[:20000].rstrip(),
                "```",
            ]
        if result.files_changed:
            lines += [
                "", "## Files changed", "",
                *(f"- `{f}`" for f in result.files_changed),
            ]
        lines += ["", "## Prompt", "", "```", prompt[:20000].rstrip(), "```", ""]

        path.write_text("\n".join(lines))
        return path
    except OSError:
        # Persistence failure must not mask the execution result — log
        # to stderr in the calling process but continue normally.
        import logging
        logging.getLogger(__name__).exception(
            "Failed to write execution transcript (non-fatal)",
        )
        return None


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
        """Execute a work item via the coder provider's agentic mode.

        Every attempt writes a Markdown transcript to
        `.sentinel/executions/` — on success, failure, or exception —
        so no failure is silent. The transcript captures the prompt,
        the provider's stderr + stdout, and the final status.
        """
        start = time.time()
        result = ExecutionResult(
            work_item_id=work_item.id,
            status="failed",
        )
        prompt = ""
        response = None

        provider = self.router.get_provider("coder")
        if not provider.capabilities.agentic_code:
            result.error = (
                f"Provider {provider.name} doesn't support agentic code execution. "
                f"Assign coder role to claude or openai in .sentinel/config.toml."
            )
            result.duration_ms = int((time.time() - start) * 1000)
            _write_execution_transcript(
                project_path, work_item, prompt, response, result,
            )
            return result

        # Create a feature branch before starting
        branch_name = f"sentinel/{work_item.type}/{_slug(work_item.title)}"
        result.branch = branch_name

        co_result = _run_git(["checkout", "-b", branch_name], project_path)
        if co_result.returncode != 0 and "already exists" not in co_result.stderr:
            result.error = (
                f"Could not create branch {branch_name}: "
                f"{co_result.stderr.strip()}"
            )
            result.duration_ms = int((time.time() - start) * 1000)
            _write_execution_transcript(
                project_path, work_item, prompt, response, result,
            )
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
            _write_execution_transcript(
                project_path, work_item, prompt, response, result, exception=e,
            )
            return result

        result.raw_output = response.content
        result.cost_usd = response.cost_usd

        if response.is_error or response.content.startswith("Error:"):
            # Surface stderr + stdout when the CLI's `content` is uninformative
            # (e.g. bare "Error: ") — silent failures are the worst kind.
            detail = response.content or "(empty content)"
            if response.stderr and response.stderr.strip() not in detail:
                detail += f"\n--- stderr ---\n{response.stderr.strip()}"
            if not detail.strip() or detail == "Error: ":
                detail = "Coder returned empty error — see transcript for stdout/stderr"
            result.error = detail
            result.duration_ms = int((time.time() - start) * 1000)
            _write_execution_transcript(
                project_path, work_item, prompt, response, result,
            )
            return result

        # Check what files actually changed
        changed = _files_changed(project_path)
        result.files_changed = changed

        if not changed:
            result.status = "failed"
            result.error = (
                "Coder produced no file changes. See transcript for the "
                "full provider response."
            )
            result.duration_ms = int((time.time() - start) * 1000)
            _write_execution_transcript(
                project_path, work_item, prompt, response, result,
            )
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
        _write_execution_transcript(
            project_path, work_item, prompt, response, result,
        )
        return result
