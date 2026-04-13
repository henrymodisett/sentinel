"""
Project state gathering — shared module for scan and cycle.

Gathers the current state of a project from git, tests, lint, and
file system. Used by both `sentinel scan` (CLI) and `Loop.cycle()`
(programmatic). Single source of truth for how project state is read.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ProjectState:
    """Current state of a project, gathered from git/tests/lint/filesystem."""

    path: str = ""
    name: str = ""

    # Git
    branch: str = "unknown"
    uncommitted_files: int = 0
    recent_commits: str = ""

    # File system
    file_tree: str = ""
    claude_md: str = ""
    readme: str = ""

    # Tests
    test_output: str = ""
    tests_passed: bool | None = None  # None = no test command

    # Lint
    lint_output: str = ""
    lint_clean: bool | None = None  # None = no lint command

    # Errors encountered during gathering
    errors: list[str] = field(default_factory=list)


def _run(args: list[str], cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with error handling."""
    return subprocess.run(
        args, capture_output=True, text=True, cwd=cwd, timeout=timeout,
    )


def _read_toolkit_command(config_path: Path, key: str) -> str | None:
    """Read a command from .toolkit-config."""
    if not config_path.exists():
        return None
    for line in config_path.read_text().splitlines():
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip()
            return value if value else None
    return None


def gather_state(project_path: Path) -> ProjectState:
    """Gather the current state of a project.

    This is the single source of truth for how project state is read.
    Both `sentinel scan` and `Loop.cycle()` use this function.
    """
    state = ProjectState(path=str(project_path), name=project_path.name)

    # Git status
    try:
        result = _run(["git", "status", "--porcelain"], project_path)
        lines = result.stdout.strip().splitlines()
        state.uncommitted_files = len(lines) if lines else 0
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        state.errors.append(f"git status failed: {e}")
        logger.warning("git status failed: %s", e)

    # Current branch
    try:
        result = _run(["git", "branch", "--show-current"], project_path, timeout=5)
        state.branch = result.stdout.strip() or "unknown"
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        state.errors.append(f"git branch failed: {e}")
        logger.warning("git branch failed: %s", e)

    # Recent commits
    try:
        result = _run(["git", "log", "--oneline", "-10"], project_path)
        state.recent_commits = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        state.errors.append(f"git log failed: {e}")
        logger.warning("git log failed: %s", e)

    # File tree (top 3 levels, excluding noise)
    try:
        result = _run(
            ["find", ".", "-maxdepth", "3", "-type", "f",
             "-not", "-path", "./.git/*",
             "-not", "-path", "./.venv/*",
             "-not", "-path", "./node_modules/*",
             "-not", "-path", "./.pytest_cache/*"],
            project_path,
        )
        state.file_tree = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        state.errors.append(f"file tree scan failed: {e}")
        logger.warning("file tree scan failed: %s", e)

    # CLAUDE.md
    claude_md = project_path / "CLAUDE.md"
    if claude_md.exists():
        state.claude_md = claude_md.read_text()[:3000]
    else:
        state.claude_md = "(no CLAUDE.md)"

    # README
    readme = project_path / "README.md"
    if readme.exists():
        state.readme = readme.read_text()[:2000]
    else:
        state.readme = "(no README.md)"

    # Test results
    toolkit_config = project_path / ".toolkit-config"
    test_cmd = _read_toolkit_command(toolkit_config, "test_command")
    if test_cmd:
        try:
            result = _run(test_cmd.split(), project_path, timeout=120)
            state.test_output = result.stdout[-2000:] + result.stderr[-1000:]
            state.tests_passed = result.returncode == 0
        except subprocess.TimeoutExpired:
            state.test_output = "(tests timed out after 120s)"
            state.tests_passed = False
            state.errors.append("test command timed out")
            logger.warning("test command timed out")
        except FileNotFoundError as e:
            state.test_output = f"(test command not found: {e})"
            state.tests_passed = False
            state.errors.append(f"test command not found: {e}")
            logger.warning("test command not found: %s", e)
    else:
        state.test_output = "(no test command configured)"

    # Lint results
    lint_cmd = _read_toolkit_command(toolkit_config, "lint_command")
    if lint_cmd:
        try:
            result = _run(lint_cmd.split(), project_path, timeout=60)
            state.lint_output = result.stdout[-1000:] + result.stderr[-500:]
            state.lint_clean = result.returncode == 0
        except subprocess.TimeoutExpired:
            state.lint_output = "(lint timed out after 60s)"
            state.lint_clean = False
            state.errors.append("lint command timed out")
            logger.warning("lint command timed out")
        except FileNotFoundError as e:
            state.lint_output = f"(lint command not found: {e})"
            state.lint_clean = False
            state.errors.append(f"lint command not found: {e}")
            logger.warning("lint command not found: %s", e)
    else:
        state.lint_output = "(no lint command configured)"

    if state.errors:
        logger.info("State gathering completed with %d errors", len(state.errors))

    return state
