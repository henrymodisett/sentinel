"""Tests for the shared state-gathering module."""

import tempfile
from pathlib import Path

from sentinel.state import ProjectState, gather_state


class TestGatherState:
    def test_gathers_git_state_from_current_repo(self) -> None:
        """gather_state reads git info from a real repo."""
        # Use a temp dir with no .toolkit-config to avoid recursive pytest
        state = gather_state(Path.cwd())
        # Git info should work even without running tests/lint
        assert state.name != ""
        assert state.branch != "unknown"
        assert state.recent_commits != ""

    def test_returns_project_state_dataclass(self) -> None:
        state = gather_state(Path.cwd())
        assert isinstance(state, ProjectState)

    def test_reads_claude_md(self) -> None:
        state = gather_state(Path.cwd())
        assert "Sentinel" in state.claude_md

    def test_reads_readme(self) -> None:
        state = gather_state(Path.cwd())
        assert "Sentinel" in state.readme or state.readme == "(no README.md)"

    def test_handles_nonexistent_directory(self) -> None:
        state = gather_state(Path("/tmp/nonexistent-sentinel-test"))
        assert state.name == "nonexistent-sentinel-test"
        assert len(state.errors) > 0

    def test_no_toolkit_config_means_no_tests(self) -> None:
        """Without .toolkit-config, tests_passed should be None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = gather_state(Path(tmpdir))
            assert state.tests_passed is None
            assert state.lint_clean is None

    def test_file_tree_excludes_git(self) -> None:
        state = gather_state(Path.cwd())
        assert ".git/" not in state.file_tree


class TestProjectStateDefaults:
    def test_defaults(self) -> None:
        state = ProjectState()
        assert state.branch == "unknown"
        assert state.uncommitted_files == 0
        assert state.tests_passed is None
        assert state.lint_clean is None
        assert state.errors == []
