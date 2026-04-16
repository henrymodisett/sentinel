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


def _git_status_snapshot(project_path: str) -> set[str]:
    """Return the set of paths currently tracked by `git status`.

    Used to snapshot the tree before and after Claude runs so we can
    isolate the Coder's actual output from anything that was already
    dirty. Sentinel's own artifacts (.sentinel/, .claude/) are dropped
    from the snapshot — they don't threaten user work and reset/clean
    excludes them anyway.
    """
    result = _run_git(["status", "--porcelain"], project_path)
    paths: set[str] = set()
    for line in result.stdout.strip().splitlines():
        if len(line) > 3:
            filename = line[3:]
            if filename.startswith(".sentinel/") or filename.startswith(".claude/"):
                continue
            paths.add(filename)
    return paths


def _files_changed(project_path: str) -> list[str]:
    """List files changed in the working tree, excluding Sentinel artifacts.

    Returns every non-sentinel path in `git status --porcelain`. Used
    in pure "what's dirty right now" contexts; when you need "what did
    the Coder add beyond what was already dirty," snapshot before and
    after and diff the sets.
    """
    return sorted(_git_status_snapshot(project_path))


def _commit_files(
    project_path: str,
    files: list[str],
    work_item: WorkItem,
) -> tuple[bool, str]:
    """Commit the coder's changes on the current feature branch.

    Stages exactly the files in `files` (never `git add -A` — we don't
    want to sweep in .sentinel/ artifacts or unrelated working-tree
    state). Returns (True, sha) on success or (False, stderr) on
    failure. If a pre-commit hook rejects the commit, we surface the
    stderr so the transcript has enough detail to debug.
    """
    if not files:
        return False, "no files to commit"

    # Stage explicit paths — never `-A`. Guard against sentinel
    # artifacts that may have sneaked past _files_changed filtering.
    safe_files = [
        f for f in files
        if not f.startswith(".sentinel/") and not f.startswith(".claude/")
    ]
    if not safe_files:
        return False, "all files were sentinel/claude artifacts"

    # Stage the files we care about — needed because untracked/new
    # files aren't in the index yet, and `git commit -- pathspec`
    # requires the paths to be known to git.
    add_result = _run_git(["add", "--", *safe_files], project_path)
    if add_result.returncode != 0:
        return False, f"git add failed: {add_result.stderr.strip()}"

    title = work_item.title[:72]
    body = (
        f"\n\nExecuted by sentinel on feature branch for work item "
        f"'{work_item.id}' ({work_item.type}, priority={work_item.priority}).\n\n"
        f"{work_item.description[:500]}"
    )
    commit_msg = f"sentinel: {title}{body}"

    # `git commit -m MSG -- pathspec…` scopes the commit to ONLY the
    # given paths even if other files are staged in the index. Without
    # this constraint, anything the coding agent had staged outside
    # our filter (.sentinel/ artifacts, stray binaries, user's
    # pre-existing in-progress work) would sweep into the sentinel
    # commit.
    commit_result = _run_git(
        ["commit", "-m", commit_msg, "--", *safe_files],
        project_path,
    )
    if commit_result.returncode != 0:
        # Target project's pre-commit hook rejected the work. This is
        # signal worth preserving — a lint failure, a test-suite block,
        # anything the project enforces is a real review finding.
        return False, (
            f"commit blocked (exit {commit_result.returncode}): "
            f"{commit_result.stderr.strip() or commit_result.stdout.strip()}"
        )

    # Capture the commit SHA for downstream review + transcript
    sha_result = _run_git(["rev-parse", "HEAD"], project_path)
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else "unknown"
    return True, sha


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

        # Explicit utf-8 — same reasoning as reviewer transcripts:
        # execution records contain provider JSON (Claude's output
        # routinely has smart quotes, em dashes, emoji) and a non-UTF-8
        # default locale must not mask the real execution result.
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except (OSError, UnicodeError):
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

Do NOT commit or push — sentinel commits the diff for you once tests
have run. Leave your work in the working tree; sentinel stages the
files it knows you touched and writes the commit message itself.
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
        from sentinel.journal import set_current_role
        set_current_role("coder")
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

        # Snapshot pre-existing dirty files so we can subtract them
        # later. Anything already in `git status` is NOT the Coder's
        # output and must never land in the sentinel commit — even if
        # _working_tree_clean at cycle start prevents this in
        # practice, keep the subtraction as defense in depth.
        pre_snapshot = _git_status_snapshot(project_path)

        co_result = _run_git(["checkout", "-b", branch_name], project_path)
        if co_result.returncode != 0:
            if "already exists" in co_result.stderr:
                # Branch already exists — check it out explicitly so
                # Claude and the commit both land there, not on the
                # original branch we stayed on when -b failed.
                switch_result = _run_git(
                    ["checkout", branch_name], project_path,
                )
                if switch_result.returncode != 0:
                    result.error = (
                        f"Branch {branch_name} exists but checkout failed: "
                        f"{switch_result.stderr.strip()}"
                    )
                    result.duration_ms = int((time.time() - start) * 1000)
                    _write_execution_transcript(
                        project_path, work_item, prompt, response, result,
                    )
                    return result
            else:
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

        # Figure out which files Claude actually touched by subtracting
        # the pre-run snapshot — anything that was already dirty belongs
        # to the user (or a prior failed item), not this execution.
        post_snapshot = _git_status_snapshot(project_path)
        changed = sorted(post_snapshot - pre_snapshot)
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

        # Commit the Coder's work to the feature branch. Without this,
        # diffs vanish between items: the next iteration's checkout
        # either discards them (reset --hard) or fails silently (dirty
        # tree blocks checkout). Commit even on test-fail — a reviewer
        # "changes-requested" is more useful with real commits to look
        # at than with vaporized work.
        commit_ok, commit_info = _commit_files(
            project_path, result.files_changed, work_item,
        )
        if commit_ok:
            result.commit_sha = commit_info
        else:
            result.error = (
                f"Files changed but commit failed: {commit_info}. "
                "Working tree left as-is for debugging; the orchestrator "
                "will reset before the next item."
            )
            result.status = "failed"
            result.duration_ms = int((time.time() - start) * 1000)
            _write_execution_transcript(
                project_path, work_item, prompt, response, result,
            )
            return result

        result.status = "success" if result.tests_passing else "partial"
        result.duration_ms = int((time.time() - start) * 1000)
        _write_execution_transcript(
            project_path, work_item, prompt, response, result,
        )
        return result
