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
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel.config.schema import CoderConfig
    from sentinel.providers.interface import ChatResponse
    from sentinel.providers.router import Router
    from sentinel.roles.planner import WorkItem
    from sentinel.roles.reviewer import ReviewResult


# Cap on captured output per `<cli> --help` invocation. Some tools dump
# hundreds of lines (e.g. `git --help`); 200 is enough to learn the arg
# shape without ballooning the coder's prompt by tens of KB per probe.
CLI_HELP_MAX_LINES = 200


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


def _file_label(item: object) -> str:
    """Render a single `WorkItem.files` entry as a human-readable string.

    The planner's current scan parser (plan_cmd._parse_actions_from_scan)
    emits each file as `{"path": ..., "rationale": ...}`, but legacy
    callers (hand-authored WorkItems in tests, older scans) pass bare
    strings. Must tolerate both without crashing.

    Returns the best-effort label:
      - str          -> the string itself
      - {"path":...} -> "path — rationale" if rationale present, else "path"
      - anything else -> `str(item)` (visible failure mode, not silent)
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        path = item.get("path") or item.get("file") or ""
        rationale = item.get("rationale") or item.get("note") or ""
        if path and rationale:
            return f"{path} — {rationale}"
        if path:
            return path
        # Dict with no recognized keys — surface the raw shape rather than
        # silently dropping it, so the bug is debuggable from prompt text.
        return str(item)
    return str(item)


def _format_files_for_prompt(files: list) -> str:
    """Render `WorkItem.files` as the prompt's Files section.

    Used to be `", ".join(files)`, which crashes with TypeError when
    planner emits list[dict]. This helper is the single consumer-side
    adapter for the `list[str | dict]` contract declared on WorkItem.files.
    """
    if not files:
        return "(let coder determine)"
    return ", ".join(_file_label(f) for f in files)


def _file_path(item: object) -> str | None:
    """Extract just the path string from a `WorkItem.files` entry.

    Mirror of ``_file_label`` for callers that only need the path
    (e.g. grounding checks). Returns None for entries that don't
    encode a path so the caller can skip them rather than raise on
    malformed planner output.
    """
    if isinstance(item, str):
        return item or None
    if isinstance(item, dict):
        path = item.get("path") or item.get("file")
        if isinstance(path, str) and path:
            return path
    return None


class RefinementGroundingError(Exception):
    """Raised when a refinement cites files absent from HEAD.

    A refinement (kind="refine") is supposed to *improve* existing
    code. If its cited files don't exist on HEAD, the planner has
    hallucinated state and the coder will silently net-create the
    files — same diff, wrong category. Autumn-mail dogfood cycle 4
    (Finding F1) hit exactly this when the planner referenced
    ``Sources/AutumnMail/GmailClient.swift`` (only existed on a
    deleted branch) and the coder happily created it from scratch.

    Expansions (kind="expand") are intentionally allowed to create
    files; this check is refinement-only.
    """


def _check_refinement_grounding(
    work_item: WorkItem, working_directory: str,
) -> None:
    """Verify each refinement's cited files exist on HEAD.

    Run before invoking the coder so a hallucinated planner reference
    fails loudly with a clear message instead of being silently
    absorbed into a category-error diff. Skips when:
      - work_item.kind == "expand" (expansions may net-create files).
      - work_item has no cited files (legacy / let-coder-determine).

    The check uses ``git ls-files <path>`` against ``working_directory``
    (the worktree, not the project root) because the worktree is what
    the coder will see and modify. ``git ls-files`` exits 0 with empty
    stdout when the path isn't tracked — that's the "missing" signal.

    Raises:
        RefinementGroundingError: any cited path is absent from HEAD.
    """
    if work_item.kind != "refine":
        return
    if not work_item.files:
        return

    paths_to_check: list[str] = []
    for entry in work_item.files:
        path = _file_path(entry)
        if path is not None:
            paths_to_check.append(path)
    if not paths_to_check:
        return

    missing: list[str] = []
    for path in paths_to_check:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path],
            capture_output=True, text=True,
            cwd=working_directory, timeout=10,
        )
        # ``--error-unmatch`` exits non-zero when the path is not
        # tracked — that's our "missing on HEAD" signal. ls-files
        # without it would silently return empty for missing paths,
        # which is harder to distinguish from "git failed entirely".
        if result.returncode != 0:
            missing.append(path)

    if missing:
        raise RefinementGroundingError(
            f"Refinement \"{work_item.title}\" cites files not present on "
            f"HEAD: {missing}. Refinements must improve existing code. If "
            f"the file is intentional new work, mark this as an expansion "
            f"proposal instead.",
        )


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
    # `--no-renames` so each side of a rename gets its own entry
    # (`D <old>` + `?? <new>`) — _commit_files stages both, capturing
    # the deletion AND the new content. Without this, git's default
    # rename detection would collapse to `R <old> -> <new>` and we'd
    # ship the new file as a copy (the old path's deletion would not
    # be staged).
    result = _run_git(
        ["status", "--porcelain", "-z",
         "--untracked-files=all", "--no-renames"],
        project_path,
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
    for entry in result.stdout.split("\0"):
        if len(entry) < 4:
            continue
        # Format: "XY PATH" — status is exactly 2 chars, then a single
        # space, then the path. Slice from index 3 to drop the prefix.
        path = entry[3:]
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


def _added_paths_in_diff(working_directory: str) -> list[str]:
    """Return paths the working tree has *added* (untracked or staged-add).

    Drives the post-execution refinement-creates-files guard. Uses
    `git status --porcelain -z` so we can read both index status (X)
    and worktree status (Y) per entry — a path is "added" if either
    column is `A` (staged add) or both are `?` (untracked).

    Sentinel's own artifacts are excluded for the same reason
    ``_git_status_snapshot`` excludes them: they don't represent user
    work and our reset/clean cycle ignores them.

    Returns paths sorted for deterministic test output.
    """
    result = _run_git(
        ["status", "--porcelain", "-z",
         "--untracked-files=all", "--no-renames"],
        working_directory,
    )
    if result.returncode != 0:
        # Match the snapshot helper's policy: log + return empty so the
        # caller sees "no added files" rather than a hidden git failure.
        # The post-exec call site decides whether silence is acceptable.
        import logging
        logging.getLogger(__name__).warning(
            "git status failed in %s (rc=%s): %s",
            working_directory, result.returncode,
            (result.stderr or result.stdout or "(no output)").strip(),
        )
        return []
    added: list[str] = []
    for entry in result.stdout.split("\0"):
        if len(entry) < 4:
            continue
        # Format: "XY PATH" — X is index, Y is worktree.
        x, y = entry[0], entry[1]
        path = entry[3:]
        if path.startswith(".sentinel/") or path.startswith(".claude/"):
            continue
        # `A` in either column = staged add; `??` = untracked = net-new.
        if x == "A" or y == "A" or (x == "?" and y == "?"):
            added.append(path)
    return sorted(added)


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

    # Stage paths INDIVIDUALLY rather than as one batch.
    #
    # `git add -- a b c d` aborts the entire batch if ANY single path
    # fails (e.g. a deleted-from-worktree file in some race state, a
    # path with quoting that confuses git's pathspec resolver, a stale
    # filesystem entry). Dogfood on portfolio_new (2026-04-16) hit
    # exactly this: 22 valid file changes from Coder, 1 path that
    # failed `git add` with "did not match any files", and the entire
    # commit aborted — losing all 22 valid changes.
    #
    # Per-path staging means one bad file logs a warning but the rest
    # still land. The ship is partial, not destroyed.
    import logging
    _log = logging.getLogger(__name__)
    staged: list[str] = []
    add_failures: list[tuple[str, str]] = []
    for path in safe_files:
        r = _run_git(["add", "--", path], project_path)
        if r.returncode == 0:
            staged.append(path)
        else:
            add_failures.append((path, r.stderr.strip() or r.stdout.strip()))

    if not staged:
        # Every path failed — surface the first failure as the error
        # since they probably share a root cause.
        first_path, first_err = add_failures[0]
        return False, (
            f"git add failed for all {len(safe_files)} files; "
            f"first: {first_path!r}: {first_err}"
        )

    if add_failures:
        _log.warning(
            "git add failed for %d/%d files (proceeding with %d staged): %s",
            len(add_failures), len(safe_files), len(staged),
            [f"{p}: {e}" for p, e in add_failures[:3]],
        )

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
    # Commit only what was actually staged (not the failed paths). Use
    # the precommit-recovery wrapper so a globally-installed `pre-commit`
    # doesn't abort the commit in repos with no config (same wrapper
    # used by the push path in `sentinel.pr`).
    from sentinel.git_ops import run_git_with_precommit_recovery
    commit_result = run_git_with_precommit_recovery(
        ["commit", "-m", commit_msg, "--", *staged],
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


# ---------------------------------------------------------------------------
# CLI surface awareness (Finding F7)
# ---------------------------------------------------------------------------
#
# Cycle 5 of the autumn-mail dogfood surfaced a coder that emitted
# `gws gmail +read <id>` and `gws gmail +reply <id>` — the installed
# gws 0.22.5 actually wants `--id <ID>`, `--message-id <ID>`, etc.
# Codex (the reviewer) caught it by *running* `gws gmail +read --dry-run`
# and observing the argument-validation error. The coder shouldn't need
# the reviewer to dry-run binaries to find arg shape — pre-loading the
# tool's `--help` output into the prompt eliminates a whole class of
# guess-the-flag-shape errors.
#
# Constraint: the allowlist is conservative. Work-item text is LLM
# output — we never `subprocess.run` a token from it without filtering
# through the configured allowlist. The schema validator (see
# CoderConfig._validate_allowlist) further restricts entries to
# `[A-Za-z0-9._+-]+`.


# Token shape we accept as a CLI name or subcommand. Mirrors the schema
# validator's restriction (`[A-Za-z0-9._+-]+`) plus the `+` prefix that
# tools like gws use for action verbs (`gws gmail +read`).
_CLI_TOKEN = r"[A-Za-z+][A-Za-z0-9._+-]*"


# Per-tool whitelist of subcommand tokens we consider safe to probe.
#
# SECURITY: Codex review (PR #82, 2026-04-19) flagged that a generic
# `<cli> <token> --help` probe can EXECUTE user code for tools whose
# first positional arg is a script or file:
#   - `node server.js --help`  → runs server.js (Node ignores trailing --help)
#   - `go run main.go --help`  → compiles + runs main.go
#   - `cargo run app --help`   → executes the `app` binary
#   - `python script.py --help`→ runs script.py
# The fix is to be EXPLICIT about which subcommand verbs we know respond
# safely to `--help`. Tools whose entire CLI shape is "verb + flags"
# (like `gws`, `swift`, `cargo`) get an explicit allowlist of known-safe
# verbs. Tools whose first positional is a script (`node`, `pytest`)
# get the `_NO_SUBCOMMAND_PROBES` sentinel — top-level `--help` only.
#
# Conservative defaults: when in doubt, omit. A missing entry costs us
# at worst one extra line in the prompt; including a wrong entry could
# execute arbitrary user code.
_NO_SUBCOMMAND_PROBES: frozenset[str] = frozenset()

CLI_SAFE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    # gws — Henry's gmail/calendar/drive wrapper. Action verbs are
    # prefixed with `+` (like `+list`, `+read`); the leading verb is a
    # plain noun (`gmail`, `calendar`, `drive`, `auth`). All `gws`
    # operations are pure command verbs, no script execution path.
    "gws": frozenset({
        "gmail", "calendar", "drive", "auth", "config", "version",
        "help", "tasks", "people", "docs", "sheets", "slides", "forms",
    }),
    # swift — `swift build/test/run/package` are all safe; we omit
    # `swift run` because the second positional is the executable
    # name, and `swift run myapp --help` runs `myapp` with `--help`.
    "swift": frozenset({"build", "test", "package", "version"}),
    "swiftc": _NO_SUBCOMMAND_PROBES,  # compiler — first arg is .swift file
    "xcrun": _NO_SUBCOMMAND_PROBES,   # toolchain dispatcher; runs binaries
    # go — same caveat as swift: `go run` and `go test ./pkg/...` would
    # potentially execute. Stick to read-only build/dependency verbs.
    "go": frozenset({
        "build", "fmt", "vet", "version", "env", "mod", "tool",
        "doc", "list", "help",
    }),
    # cargo — Rust's package manager. Same hazard as go: `cargo run`
    # builds AND executes. Stick to inspection/build verbs.
    "cargo": frozenset({
        "build", "check", "test", "fmt", "version", "metadata",
        "tree", "search", "help",
    }),
    "rustc": _NO_SUBCOMMAND_PROBES,  # compiler — first arg is source file
    # node — first arg is ALWAYS a script path. Top-level only.
    "node": _NO_SUBCOMMAND_PROBES,
    # npm — `npm install foo` is safe (no execution), `npm run script`
    # is NOT (executes the script). Limit to inspection verbs.
    "npm": frozenset({"install", "list", "view", "version", "help"}),
    "pnpm": frozenset({"install", "list", "view", "version", "help"}),
    # uv — astral's Python toolchain. `uv run` executes; `uv pip` and
    # `uv venv` are pure commands.
    "uv": frozenset({"pip", "venv", "version", "help", "lock", "sync"}),
    "pip": frozenset({"install", "list", "show", "freeze", "help"}),
    "pytest": _NO_SUBCOMMAND_PROBES,  # first arg is a test path → executes
    "ruff": frozenset({"check", "format", "version", "help", "rule"}),
    "mypy": _NO_SUBCOMMAND_PROBES,  # first arg is a path → type-checks code
}


def _detect_cli_invocations(
    text: str,
    allowlist: set[str],
    *,
    max_subcommands: int,
    safe_subcommands: dict[str, frozenset[str]] | None = None,
) -> list[tuple[str, ...]]:
    """Find probe targets in `text`, restricted by `allowlist`.

    Returns a list of tuples — `(cli,)` for top-level probes and
    `(cli, sub1)` or `(cli, sub1, sub2)` for subcommand probes.

    SECURITY: subcommand probes are gated by a per-tool known-safe
    verb whitelist (`safe_subcommands`, default `CLI_SAFE_SUBCOMMANDS`).
    A subcommand token that doesn't appear in its tool's safe set is
    NOT probed — even though the top-level `<cli> --help` still runs.
    Without this gate, `node server.js --help` (and other "first
    positional is a script" tools) would execute user code instead of
    fetching help text. Codex caught this on PR #82.

    Order: top-level CLIs first (deduped, in first-seen order), then
    subcommand probes in first-seen order capped at `max_subcommands`.

    Implementation: build a single pattern that anchors on the
    allowlist tokens. `re.escape` each entry so a config-supplied tool
    name with a literal `.` or `+` is treated literally.
    """
    if not allowlist or not text:
        return []

    safe_subs = safe_subcommands if safe_subcommands is not None else CLI_SAFE_SUBCOMMANDS

    # Escape allowlist entries to keep regex-meta chars literal, sort by
    # length descending so a longer prefix wins over a shorter one
    # (`pnpm` should win over a hypothetical `pn`).
    escaped = sorted((re.escape(a) for a in allowlist), key=len, reverse=True)
    cli_alternation = "|".join(escaped)
    pattern = re.compile(
        rf"\b(?P<cli>{cli_alternation})\b"
        rf"(?:[ \t]+(?P<sub1>{_CLI_TOKEN}))?"
        rf"(?:[ \t]+(?P<sub2>{_CLI_TOKEN}))?",
    )

    # English fillers we routinely see between a CLI mention and the
    # next noun in prose ("use gws to fetch …", "run swift and tests").
    prose_fillers = frozenset({
        "to", "the", "a", "an", "and", "or", "but", "with", "from",
        "into", "onto", "for", "of", "in", "on", "at", "by", "is",
        "are", "be", "as", "via", "then", "when", "if", "so",
    })

    seen_cli: list[str] = []
    seen_cli_set: set[str] = set()
    sub_probes: list[tuple[str, ...]] = []
    sub_probes_seen: set[tuple[str, ...]] = set()

    for match in pattern.finditer(text):
        cli = match.group("cli")
        if cli not in seen_cli_set:
            seen_cli.append(cli)
            seen_cli_set.add(cli)

        sub1 = match.group("sub1")
        sub2 = match.group("sub2")

        # Subcommand probing is GATED by per-tool safe-verb whitelist.
        # A user-supplied CLI in the allowlist with no entry in
        # `safe_subs` gets no subcommand probes (top-level only) —
        # safer to probe one less thing than to execute user code.
        tool_safe_subs = safe_subs.get(cli)
        if tool_safe_subs is None:
            continue  # unknown tool → top-level probe only
        if not tool_safe_subs:
            continue  # explicit "no subcommand probes" sentinel

        # Filter prose fillers, flag-shaped tokens, and tokens absent
        # from the safe set. The `+` prefix is gws-specific action-verb
        # syntax — these are always safe (they're literal arguments to
        # the gws subcommand, not script paths).
        if not sub1 or sub1.startswith("-") or sub1 in prose_fillers:
            continue

        is_action_verb = sub1.startswith("+")
        if not is_action_verb and sub1 not in tool_safe_subs:
            # The first positional after the CLI is not in the safe-verb
            # set. For tools like `go`, this means the work item said
            # `go run main.go` — we skip the probe entirely rather than
            # risk executing main.go.
            continue
        if len(sub_probes) >= max_subcommands:
            continue

        # Second positional handling. Three cases:
        #   - sub2 absent / flag-shaped / filler → probe is (cli, sub1).
        #   - sub2 present and starts with `+` (gws-style action verb) →
        #     safe to probe `(cli, sub1, sub2)`.
        #   - sub2 looks like a target/path (e.g. `swift build MyTarget`)
        #     → drop sub2 and only probe `(cli, sub1)` to avoid triggering
        #     a build step before --help is read.
        sub2_is_safe_action_verb = bool(
            sub2
            and sub2.startswith("+")
            and not sub2.startswith("-")
            and sub2 not in prose_fillers
        )
        probe: tuple[str, ...] = (
            (cli, sub1, sub2) if sub2_is_safe_action_verb else (cli, sub1)
        )

        if probe not in sub_probes_seen:
            sub_probes.append(probe)
            sub_probes_seen.add(probe)

    probes: list[tuple[str, ...]] = [(c,) for c in seen_cli]
    probes.extend(sub_probes)
    return probes


def _capture_cli_help(
    probe: tuple[str, ...], *, timeout_sec: int,
) -> str | None:
    """Run `<probe...> --help` and return up to CLI_HELP_MAX_LINES lines.

    Fail-soft: any subprocess failure (timeout, non-zero exit, missing
    binary, OS error) is logged at DEBUG and returns None so the caller
    can simply omit the section. Never raises — a help-fetch failure
    must not abort the coder cycle.

    The `shutil.which(cli)` check is the caller's responsibility — by
    the time we reach here, the tool is known installed and on the
    allowlist. We still pass the binary by full PATH-resolved name
    (subprocess.run does that automatically with list-form args).
    """
    import logging
    cmd = [*probe, "--help"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout_sec,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logging.getLogger(__name__).debug(
            "CLI help probe %s failed: %s", " ".join(cmd), exc,
        )
        return None
    # Some tools (e.g. `pytest --help`) write to stdout; others (busybox
    # variants, some Go tools) write `--help` to stderr. Prefer stdout
    # but fall back to stderr so we don't return a useless empty section
    # when the tool just chose stderr.
    output = result.stdout or result.stderr or ""
    if not output.strip():
        return None
    lines = output.splitlines()
    if len(lines) > CLI_HELP_MAX_LINES:
        lines = lines[:CLI_HELP_MAX_LINES]
        lines.append(
            f"... [truncated at {CLI_HELP_MAX_LINES} lines — "
            f"run `{' '.join(cmd)}` for the full output]",
        )
    return "\n".join(lines)


def _build_cli_help_section(
    work_item: WorkItem,
    *,
    coder_config: CoderConfig | None,
) -> str:
    """Return a Markdown block of `--help` outputs for the coder prompt.

    Empty string when:
      - coder_config is None (legacy callers, e.g. tests with no config);
      - the allowlist is empty (feature disabled per-project);
      - no detected CLI from the work item is both allowlisted and
        installed;
      - every probed `--help` invocation failed (fail-soft).

    The block is meant to be prepended to the prompt, before the work-
    item details, so the coder sees the surface up-front rather than
    after deciding what to write.
    """
    if coder_config is None:
        return ""
    allowlist = set(coder_config.cli_help_allowlist)
    if not allowlist:
        return ""

    # Build the probe corpus from work-item fields the coder will read
    # anyway. WorkItem doesn't have a `verification` attribute today —
    # the planner emits it as `acceptance_criteria` text. Tests cited
    # both for completeness, so we accept either.
    parts: list[str] = []
    parts.append(work_item.title or "")
    parts.append(work_item.description or "")
    if work_item.acceptance_criteria:
        parts.extend(str(c) for c in work_item.acceptance_criteria)
    if work_item.files:
        for entry in work_item.files:
            label = _file_label(entry)
            if label:
                parts.append(label)
    # Defensive `getattr`: if a future planner adds a `verification`
    # field (tests reference one in the spec), pick it up automatically
    # instead of needing a second code change here.
    extra = getattr(work_item, "verification", None)
    if extra:
        parts.append(str(extra))
    text = "\n".join(parts)

    probes = _detect_cli_invocations(
        text, allowlist,
        max_subcommands=coder_config.cli_help_max_subcommands,
    )
    if not probes:
        return ""

    # Drop probes whose root CLI isn't installed in this environment.
    # `shutil.which` is the simplest "is this on PATH and executable"
    # check — same primitive sentinel uses for provider detection.
    installed_probes: list[tuple[str, ...]] = []
    seen_missing: set[str] = set()
    import logging
    log = logging.getLogger(__name__)
    for probe in probes:
        cli = probe[0]
        if shutil.which(cli) is None:
            if cli not in seen_missing:
                log.debug("CLI %r not installed; skipping help probe", cli)
                seen_missing.add(cli)
            continue
        installed_probes.append(probe)

    if not installed_probes:
        return ""

    sections: list[str] = []
    for probe in installed_probes:
        captured = _capture_cli_help(
            probe, timeout_sec=coder_config.cli_help_timeout_sec,
        )
        if captured is None:
            continue
        header = " ".join(probe) + " --help"
        sections.append(f"### {header}\n\n```\n{captured}\n```")

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return (
        "## Installed CLI surfaces\n\n"
        "The following CLI tools are installed in this environment. "
        "Use the documented argument shapes from `--help` output rather "
        "than guessing flag names or positional shapes:\n\n"
        f"{body}\n\n---\n\n"
    )


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

## Hard scope rules (a reviewer LLM will reject the diff if violated)

- **Touch ONLY files this work item is about.** The "Files likely to
  be touched" list above is not exhaustive but it IS scoped — do not
  add or modify files unrelated to this work item's described change.
- **Do not modify `.claude/` or `.sentinel/` files.** Those are meta-
  configuration / agent scaffolds, not project source. They look like
  legitimate edits because of their proximity but they are out of
  scope for every work item.
- **Do not add tooling, hooks, CI configs, or "improvements" beyond
  the explicit acceptance criteria.** Scope creep is the most common
  reason the reviewer rejects a diff. If you find related work that
  feels worth doing, mention it in your final reply but DO NOT do it.
- **Do not commit or push.** Sentinel stages the files you touched
  and writes the commit message. Leave your work in the working tree.
- **Do not refactor surrounding code** unless the work item explicitly
  requires it.

Report what you changed and whether tests pass.
"""


REVISE_PROMPT = """\
You are REVISING your previous work on this work item for the {project_name} project.

## Work Item: {title}

**Type:** {type} | **Priority:** {priority} | **Complexity:** {complexity}/5

{description}

## Acceptance Criteria
{criteria}

## Files likely to be touched
{files}

## Your previous attempt was reviewed and needs changes

The code reviewer returned verdict: **{verdict}**.

### Blocking issues (each must be addressed)

{blocking_issues}

{non_blocking}

## Your Task

1. Inspect the commits already on this branch to see your prior approach.
2. Address EACH blocking issue explicitly. A finding left unaddressed means another rejection.
3. Refine your prior changes — do not revert wholesale and rewrite from scratch.
4. Run the project's tests to verify nothing is broken.

## Hard scope rules (same as before)

- Address the blocking issues ONLY. Do not expand scope.
- Do not modify `.claude/` or `.sentinel/` files.
- Do not commit or push — sentinel handles staging and commit.
- Do not refactor surrounding code unless a blocking issue requires it.

Report what you changed in response to the findings and whether tests pass.
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
        review_feedback: ReviewResult | None = None,
        coder_config: CoderConfig | None = None,
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

        # Refinement grounding check — refuse to run a "harden existing
        # code" item against files that don't exist on HEAD. Without
        # this, the coder silently absorbs the contradiction by net-
        # creating the files (Finding F1 from autumn-mail dogfood
        # cycle 4: planner cited GmailClient.swift which only existed
        # on a deleted branch, coder created it from scratch, tests
        # passed, category was wrong). On revisions, the cited files
        # may have been removed by the prior attempt — skip the check
        # so the coder can address review feedback without re-tripping
        # the guard on its own intermediate state.
        if review_feedback is None:
            try:
                _check_refinement_grounding(work_item, wd)
            except RefinementGroundingError as exc:
                result.error = str(exc)
                result.duration_ms = int((time.time() - start) * 1000)
                _write_execution_transcript(
                    ad, work_item, prompt, response, result, exception=exc,
                )
                return result

        # Snapshot dirty files BEFORE the call so we can subtract them
        # from the post-call snapshot — anything pre-existing belongs to
        # whatever set up the working directory (typically a clean
        # worktree, but defense-in-depth catches stale state from an
        # interrupted prior run).
        pre_snapshot = _git_status_snapshot(wd)

        # Build the prompt — `review_feedback` present means we're
        # iterating on a previously-committed attempt on this branch,
        # addressing the reviewer's blocking issues. The coder stays
        # stateless across calls: each invocation rebuilds the prompt
        # from the live work item + (for revisions) the review findings.
        criteria = "\n".join(f"- {c}" for c in work_item.acceptance_criteria) or "(none)"
        files = _format_files_for_prompt(work_item.files)
        # Project name comes from the artifacts dir (the main project),
        # NOT the working dir (the worktree, whose path includes the
        # `.sentinel/worktrees/wi-N` suffix).
        if review_feedback is None:
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
        else:
            blocking = (
                "\n".join(f"- {i}" for i in review_feedback.blocking_issues)
                or "(none listed — see summary)"
            )
            non_blocking_section = ""
            if review_feedback.non_blocking_observations:
                items = "\n".join(
                    f"- {o}" for o in review_feedback.non_blocking_observations
                )
                non_blocking_section = (
                    f"### Non-blocking observations (optional)\n\n{items}\n"
                )
            prompt = REVISE_PROMPT.format(
                project_name=Path(ad).name,
                title=work_item.title,
                type=work_item.type,
                priority=work_item.priority,
                complexity=work_item.complexity,
                description=work_item.description,
                criteria=criteria,
                files=files,
                verdict=review_feedback.verdict,
                blocking_issues=blocking,
                non_blocking=non_blocking_section,
            )

        # CLI surface awareness (Finding F7): prepend `--help` text for
        # installed tools the work item references. Fail-soft — any
        # subprocess error (timeout, missing tool, OS error) just omits
        # the section, never aborts the cycle. Disabled when the
        # allowlist is empty (configurable per-project) or no caller
        # threaded `coder_config` (legacy callers).
        try:
            help_section = _build_cli_help_section(
                work_item, coder_config=coder_config,
            )
        except Exception as exc:  # noqa: BLE001 — defense in depth
            # The helper already swallows per-probe errors; a top-level
            # exception means a programming bug in the helper itself.
            # Log loudly but don't block the work — the coder can still
            # do its job without the help section.
            import logging
            logging.getLogger(__name__).warning(
                "CLI help pre-load failed (continuing without): %s", exc,
            )
            help_section = ""
        if help_section:
            prompt = help_section + prompt

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

        # Post-execution category-drift guard: a refinement that
        # net-creates files contradicts its own kind. The pre-execution
        # check (Fix 2) catches the planner-level miscategorization;
        # this post-execution check catches drift introduced by the
        # coder itself (e.g. it decided to "extract a helper module"
        # when the work item said "harden existing code"). Warning,
        # not error — the work may still be salvageable; the reviewer
        # can weigh in.
        if work_item.kind == "refine":
            added = _added_paths_in_diff(wd)
            if added:
                import logging
                logging.getLogger(__name__).warning(
                    "Refinement %r created new files: %s. Refinements "
                    "should improve existing code; consider re-classifying "
                    "as expansion.",
                    work_item.title, added,
                )

        # Run tests to verify. The touchstone-config lives at the project
        # root (artifacts_directory), not the worktree — config is
        # repo-wide. Tests, however, run in the working directory so
        # they see the Coder's diff.
        from sentinel.state import _read_touchstone_command  # type: ignore[attr-defined]

        touchstone_config = Path(ad) / ".touchstone-config"
        test_cmd = _read_touchstone_command(touchstone_config, "test_command")
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
