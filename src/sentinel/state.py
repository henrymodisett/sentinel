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
    goals_md: str = ""  # user-provided goals (.sentinel/goals.md)

    # Tests
    test_output: str = ""
    tests_passed: bool | None = None  # None = no test command

    # Lint
    lint_output: str = ""
    lint_clean: bool | None = None  # None = no lint command

    # Ops tooling discovered on PATH (rendered for the lens-gen prompt)
    installed_tools: str = ""

    # Short excerpts from discovered project-level documentation (strategy
    # docs, architecture notes, thesis files) — formatted for the explore
    # prompt. Separate from claude_md/readme which are always included.
    project_docs: str = ""

    # Project type label (python, typescript, swift, rust, etc.) — used
    # by the domain-brief researcher to frame the web query.
    project_type: str = "generic"

    # Domain-expertise brief produced by the Researcher role pre-scan.
    # Populated by Monitor when it calls researcher.domain_brief. Empty
    # string when research failed or was skipped (offline, no provider).
    domain_brief: str = ""

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


def detect_project_type(project_path: Path) -> dict:
    """Detect project type and suggest test/lint commands.

    Returns a dict with 'type', 'test_command', 'lint_command', and
    'conditional_lenses' based on project files found.
    """
    result: dict = {
        "type": "generic",
        "test_command": None,
        "lint_command": None,
        "conditional_lenses": [],
    }

    # Swift
    if (project_path / "Package.swift").exists():
        result["type"] = "swift"
        result["test_command"] = "swift test"
        result["lint_command"] = "swiftlint"
        result["conditional_lenses"].append("ui-design")
        result["conditional_lenses"].append("accessibility")
        result["conditional_lenses"].append("performance")

    # Xcode project (SwiftUI app without Package.swift)
    elif list(project_path.glob("*.xcodeproj")) or list(project_path.glob("*.xcworkspace")):
        result["type"] = "xcode"
        result["test_command"] = "xcodebuild test"
        result["conditional_lenses"].append("ui-design")
        result["conditional_lenses"].append("accessibility")
        result["conditional_lenses"].append("performance")

    # Rust
    elif (project_path / "Cargo.toml").exists():
        result["type"] = "rust"
        result["test_command"] = "cargo test"
        result["lint_command"] = "cargo clippy"
        result["conditional_lenses"].append("performance")

    # Go
    elif (project_path / "go.mod").exists():
        result["type"] = "go"
        result["test_command"] = "go test ./..."
        result["lint_command"] = "golangci-lint run"
        result["conditional_lenses"].append("performance")

    # Python (uv, pip-tools, setuptools, pipenv) — any of these markers
    # flags the project as Python for scans/work commands
    elif any(
        (project_path / marker).exists()
        for marker in (
            "pyproject.toml", "requirements.txt", "setup.py",
            "setup.cfg", "Pipfile",
        )
    ):
        result["type"] = "python"
        if (project_path / "uv.lock").exists():
            result["test_command"] = "uv run pytest"
            result["lint_command"] = "uv run ruff check ."
        else:
            result["test_command"] = "pytest"
            result["lint_command"] = "ruff check ."

    # Node/TypeScript
    elif (project_path / "package.json").exists():
        result["type"] = "node"
        import json
        try:
            pkg = json.loads((project_path / "package.json").read_text())
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                # Detect package manager
                if (project_path / "pnpm-lock.yaml").exists():
                    result["test_command"] = "pnpm test"
                    result["lint_command"] = "pnpm lint" if "lint" in scripts else None
                elif (project_path / "bun.lock").exists() or (project_path / "bun.lockb").exists():
                    result["test_command"] = "bun test"
                    result["lint_command"] = "bun lint" if "lint" in scripts else None
                else:
                    result["test_command"] = "npm test"
                    result["lint_command"] = "npm run lint" if "lint" in scripts else None
        except (json.JSONDecodeError, OSError):
            pass

    # Detect frontend → enable UI lenses
    has_frontend = (
        any(project_path.rglob("*.tsx"))
        or any(project_path.rglob("*.jsx"))
        or any(project_path.rglob("*.vue"))
        or any(project_path.rglob("*.svelte"))
        or (project_path / "next.config.js").exists()
        or (project_path / "next.config.ts").exists()
    )
    if has_frontend:
        if "ui-design" not in result["conditional_lenses"]:
            result["conditional_lenses"].append("ui-design")
        if "accessibility" not in result["conditional_lenses"]:
            result["conditional_lenses"].append("accessibility")

    # Detect API → enable API lens
    has_api = (
        any(project_path.rglob("**/routes*"))
        or any(project_path.rglob("**/api*"))
        or (project_path / "openapi.yaml").exists()
        or (project_path / "openapi.json").exists()
    )
    if has_api:
        result["conditional_lenses"].append("api-design")

    # Detect database → enable data integrity lens
    has_db = (
        any(project_path.rglob("**/migrations*"))
        or any(project_path.rglob("*.sql"))
        or (project_path / "prisma").is_dir()
        or (project_path / "alembic.ini").exists()
    )
    if has_db:
        result["conditional_lenses"].append("data-integrity")

    # Detect cloud infra → enable cost lens
    has_cloud = (
        (project_path / "Dockerfile").exists()
        or (project_path / "docker-compose.yml").exists()
        or (project_path / "terraform").is_dir()
        or any(project_path.rglob("*.tf"))
    )
    if has_cloud:
        result["conditional_lenses"].append("cost-efficiency")

    return result


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

    # File tree (top 2 levels, excluding noise — keep it compact)
    try:
        result = _run(
            ["find", ".", "-maxdepth", "2", "-type", "f",
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

    # goals.md — user-provided project goals (optional but high-signal)
    goals_md = project_path / ".sentinel" / "goals.md"
    if goals_md.exists():
        state.goals_md = goals_md.read_text()[:3000]

    # Test results — try toolkit-config first, then auto-detect
    toolkit_config = project_path / ".toolkit-config"
    test_cmd = _read_toolkit_command(toolkit_config, "test_command")
    if not test_cmd:
        detected = detect_project_type(project_path)
        test_cmd = detected.get("test_command")
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

    # Lint results — try toolkit-config first, then auto-detect
    lint_cmd = _read_toolkit_command(toolkit_config, "lint_command")
    if not lint_cmd:
        detected = detect_project_type(project_path)
        lint_cmd = detected.get("lint_command")
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

    # Ops CLIs available for the Coder to invoke during execution
    from sentinel.tools import discover_installed_tools, format_tools_for_prompt
    state.installed_tools = format_tools_for_prompt(discover_installed_tools())

    # Strategic project docs — INVESTMENT_THESIS, ARCHITECTURE, plans,
    # etc. Goes into EXPLORE_PROMPT so lens generation sees the vision,
    # not just CLAUDE.md + README. See sentinel.docs for the ranking.
    from sentinel.docs import discover_project_docs
    try:
        state.project_docs = discover_project_docs(project_path)
    except OSError as e:
        state.errors.append(f"doc discovery failed: {e}")
        logger.warning("doc discovery failed: %s", e)

    # Project type — needed for the domain brief
    try:
        state.project_type = detect_project_type(project_path)["type"]
    except OSError as e:
        state.errors.append(f"project type detection failed: {e}")
        state.project_type = "generic"

    if state.errors:
        logger.info("State gathering completed with %d errors", len(state.errors))

    return state
