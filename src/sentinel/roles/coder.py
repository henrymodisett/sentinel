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

    Uses `git status --porcelain -z` for NUL-terminated output. The
    line-based parser this replaces stripped the first character off
    every path during dogfood on portfolio_new (`src/...` → `rc/...`,
    `principles/...` → `rinciples/...`). Root cause unclear — likely
    a git/locale interaction where some entries shifted by one column.
    NUL-terminated parsing is bulletproof against that class of bug
    because path delimiters are explicit, not whitespace-derived.

    -z format: each entry is `XY<sp>PATH\\0`. Rename/copy entries
    (R, C status) are followed by an extra `ORIG\\0` entry that we
    skip — we only want the new path.
    """
    # `--untracked-files=all` so newly-created files in newly-created
    # directories show as full paths, not just their containing dir
    # (the default `normal` mode collapses untracked dirs).
    result = _run_git(
        ["status", "--porcelain", "-z", "--untracked-files=all"], project_path,
    )
    if result.returncode != 0:
        # Don't silently swallow — a git failure here means the
        # snapshot is empty and downstream `result.files_changed = []`
        # makes the Coder look like it produced nothing. Log so the
        # underlying git problem (missing repo, locked index, etc.)
        # is visible in operator output rather than masquerading as
        # "Coder produced no file changes".
        import logging
        logging.getLogger(__name__).warning(
            "git status failed in %s (rc=%s): %s",
            project_path, result.returncode,
            (result.stderr or result.stdout or "(no output)").strip(),
        )
        return set()
    paths: set[str] = set()
    entries = result.stdout.split("\0")
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        # Format: "XY PATH" — status is exactly 2 chars, then a single
        # space, then the path. Slice from index 3 to drop the prefix.
        status = entry[:2]
        path = entry[3:]
        if status and status[0] in ("R", "C"):
            skip_next = True  # the next entry is the rename's original
        if path.startswith(".sentinel/") or path.startswith(".claude/"):
            continue
        paths.add(path)
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
        self,
        work_item: WorkItem,
        *,
        working_directory: str,
        artifacts_directory: str,
        branch: str,
    ) -> ExecutionResult:
        """Execute a work item via the coder provider's agentic mode.

        Worktree-managed: the caller (work_cmd._execute_and_review,
        via `worktree_for(...)`) checks out `branch` in
        `working_directory` before invoking Coder. Coder runs there
        and never creates branches itself — that responsibility lives
        in the worktree primitive so concurrent items can't conflict.

        `artifacts_directory` is where transcripts go (typically the
        main project's `.sentinel/`, NOT the worktree, so failure
        evidence survives `git worktree remove --force`).
        """
        from sentinel.journal import set_current_role
        set_current_role("coder")
        start = time.time()
        result = ExecutionResult(
            work_item_id=work_item.id,
            status="failed",
            branch=branch,
        )
        prompt = ""
        response = None

        wd = working_directory
        ad = artifacts_directory

        provider = self.router.get_provider("coder")
        if not provider.capabilities.agentic_code:
            result.error = (
                f"Provider {provider.name} doesn't support agentic code execution. "
                f"Assign coder role to claude or openai in .sentinel/config.toml."
            )
            result.duration_ms = int((time.time() - start) * 1000)
            _write_execution_transcript(
                ad, work_item, prompt, response, result,
            )
            return result

        # Snapshot dirty files BEFORE the call so we can subtract them
        # from the post-call snapshot — anything pre-existing belongs to
        # whatever set up the working directory (typically a clean
        # worktree, but defense-in-depth catches stale state from an
        # interrupted prior run).
        pre_snapshot = _git_status_snapshot(wd)

        # Build the prompt
        criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "(none)"
        files = ", ".join(work_item.files) or "(let coder determine)"
        # Project name comes from the artifacts dir (the main project),
        # NOT the working dir (the worktree, whose path includes the
        # `.sentinel/worktrees/wi-N` suffix).
        prompt = BUILD_PROMPT.format(
            project_name=Path(ad).name,
            title=work_item.title,
            type=work_item.type,
            priority=work_item.priority,
            complexity=work_item.complexity,
            description=work_item.description,
            criteria=criteria,
            files=files,
            risk=work_item.risk or "(none noted)",
        )

        # Execute via provider's agentic code mode — runs in `wd`,
        # which is the worktree path in worktree-managed mode and
        # the project path in legacy mode.
        try:
            response = await provider.code(prompt, working_directory=wd)
        except (OSError, subprocess.SubprocessError) as e:
            result.error = f"Coder execution failed: {e}"
            result.duration_ms = int((time.time() - start) * 1000)
            _write_execution_transcript(
                ad, work_item, prompt, response, result, exception=e,
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
                ad, work_item, prompt, response, result,
            )
            return result

        # Figure out which files Claude actually touched. In worktree
        # mode pre_snapshot is empty (worktree starts clean), so all
        # dirty files belong to the Coder. In legacy mode we subtract
        # the pre-existing dirty files.
        post_snapshot = _git_status_snapshot(wd)
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
                ad, work_item, prompt, response, result,
            )
            return result

        # Run tests to verify. The toolkit-config lives at the project
        # root (artifacts_directory), not the worktree — config is
        # repo-wide. Tests, however, run in the working directory so
        # they see the Coder's diff.
        from sentinel.state import _read_toolkit_command  # type: ignore[attr-defined]

        toolkit_config = Path(ad) / ".toolkit-config"
        test_cmd = _read_toolkit_command(toolkit_config, "test_command")
        if test_cmd:
            try:
                test_result = subprocess.run(
                    test_cmd.split(), capture_output=True, text=True,
                    cwd=wd, timeout=300,
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
            wd, result.files_changed, work_item,
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
                ad, work_item, prompt, response, result,
            )
            return result

        result.status = "success" if result.tests_passing else "partial"
        result.duration_ms = int((time.time() - start) * 1000)
        _write_execution_transcript(
            ad, work_item, prompt, response, result,
        )
        return result
