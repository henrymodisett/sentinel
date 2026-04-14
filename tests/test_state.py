"""Tests for the shared state-gathering module."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from sentinel.state import ProjectState, detect_project_type, gather_state


def _mock_run(args, **kwargs):
    """Mock subprocess.run for fast, deterministic state tests."""
    cmd = args[0] if args else ""
    if cmd == "git" and "status" in args:
        return subprocess.CompletedProcess(args, 0, stdout="M file1.py\nM file2.py\n", stderr="")
    if cmd == "git" and "branch" in args:
        return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
    if cmd == "git" and "log" in args:
        return subprocess.CompletedProcess(args, 0, stdout="abc1234 initial commit\n", stderr="")
    if cmd == "find":
        return subprocess.CompletedProcess(
            args, 0, stdout="./src/main.py\n./README.md\n", stderr="",
        )
    # Default: command not found
    raise FileNotFoundError(f"mock: {cmd} not found")


class TestGatherState:
    @patch("sentinel.state.subprocess.run", side_effect=_mock_run)
    def test_gathers_git_state(self, mock_sub) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = gather_state(Path(tmpdir))
            assert state.branch == "main"
            assert state.uncommitted_files == 2
            assert "initial commit" in state.recent_commits

    @patch("sentinel.state.subprocess.run", side_effect=_mock_run)
    def test_returns_project_state_dataclass(self, mock_sub) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = gather_state(Path(tmpdir))
            assert isinstance(state, ProjectState)

    @patch("sentinel.state.subprocess.run", side_effect=_mock_run)
    def test_no_toolkit_config_means_no_tests(self, mock_sub) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = gather_state(Path(tmpdir))
            assert state.tests_passed is None
            assert state.lint_clean is None

    @patch("sentinel.state.subprocess.run", side_effect=_mock_run)
    def test_reads_claude_md(self, mock_sub) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "CLAUDE.md").write_text("# My Project\nSentinel test")
            state = gather_state(Path(tmpdir))
            assert "Sentinel test" in state.claude_md

    @patch("sentinel.state.subprocess.run", side_effect=_mock_run)
    def test_reads_readme(self, mock_sub) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "README.md").write_text("# My Project")
            state = gather_state(Path(tmpdir))
            assert "My Project" in state.readme

    @patch("sentinel.state.subprocess.run", side_effect=_mock_run)
    def test_missing_claude_md(self, mock_sub) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = gather_state(Path(tmpdir))
            assert state.claude_md == "(no CLAUDE.md)"

    def test_handles_nonexistent_directory(self) -> None:
        state = gather_state(Path("/tmp/nonexistent-sentinel-test-xyz"))
        assert state.name == "nonexistent-sentinel-test-xyz"
        assert len(state.errors) > 0

    @patch("sentinel.state.subprocess.run", side_effect=_mock_run)
    def test_file_tree_populated(self, mock_sub) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = gather_state(Path(tmpdir))
            assert "main.py" in state.file_tree

    @patch("sentinel.state.subprocess.run", side_effect=FileNotFoundError("git"))
    def test_git_not_found_records_error(self, mock_sub) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = gather_state(Path(tmpdir))
            assert len(state.errors) > 0
            assert state.branch == "unknown"


class TestDetectProjectType:
    """Regression: gather_state uses this to pick test/lint commands.
    A project with requirements.txt but no pyproject.toml used to land
    as 'generic' and get no pytest/ruff invocation at all."""

    def test_requirements_txt_is_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "requirements.txt").write_text("requests\n")
            result = detect_project_type(Path(tmpdir))
            assert result["type"] == "python"
            assert result["test_command"] == "pytest"

    def test_setup_py_is_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "setup.py").write_text("from setuptools import setup\n")
            result = detect_project_type(Path(tmpdir))
            assert result["type"] == "python"

    def test_pipfile_is_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "Pipfile").write_text("[packages]\n")
            result = detect_project_type(Path(tmpdir))
            assert result["type"] == "python"

    def test_pyproject_with_uv_lock_uses_uv_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "pyproject.toml").write_text('[project]\nname="x"\n')
            (Path(tmpdir) / "uv.lock").write_text("")
            result = detect_project_type(Path(tmpdir))
            assert result["test_command"] == "uv run pytest"


class TestProjectStateDefaults:
    def test_defaults(self) -> None:
        state = ProjectState()
        assert state.branch == "unknown"
        assert state.uncommitted_files == 0
        assert state.tests_passed is None
        assert state.lint_clean is None
        assert state.errors == []
