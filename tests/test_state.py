"""Tests for the shared state-gathering module."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from sentinel.state import ProjectState, gather_state


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


class TestProjectStateDefaults:
    def test_defaults(self) -> None:
        state = ProjectState()
        assert state.branch == "unknown"
        assert state.uncommitted_files == 0
        assert state.tests_passed is None
        assert state.lint_clean is None
        assert state.errors == []
