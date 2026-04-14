"""Tests for the ops-CLI discovery helper.

Uses the fake_cli_env fixture from conftest.py — same PATH-isolation
mechanism the init scenario tests use.
"""

from __future__ import annotations

import stat
import tempfile
from pathlib import Path

from sentinel.tools import (
    TOOL_GROUPS,
    discover_installed_tools,
    format_tools_for_prompt,
)


def _path_with_only(monkeypatch, tool_names: list[str]) -> Path:
    """Replace PATH with a temp dir containing exactly these stubs.

    Returns the dir so individual tests can add more stubs if they want.
    Uses tempfile.mkdtemp (not a fixture) so we control cleanup via
    monkeypatch's teardown indirectly — the temp dir leaks but pytest
    clears /tmp between runs.
    """
    bin_dir = Path(tempfile.mkdtemp(prefix="sentinel-tools-test-"))
    for name in tool_names:
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    # Replace PATH with ONLY the temp dir — stripping /usr/bin keeps
    # system-installed tools (like `git`) from leaking into discovery.
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


class TestDiscovery:
    def test_empty_path_returns_empty_dict(self, monkeypatch):
        _path_with_only(monkeypatch, [])
        assert discover_installed_tools() == {}

    def test_discovers_only_tools_on_path(self, monkeypatch):
        _path_with_only(monkeypatch, ["gh", "railway", "docker"])
        tools = discover_installed_tools()
        assert tools["vcs"] == ["gh"]
        assert tools["deploy"] == ["railway"]
        assert tools["infra"] == ["docker"]

    def test_drops_empty_groups(self, monkeypatch):
        _path_with_only(monkeypatch, ["railway"])
        tools = discover_installed_tools()
        # Only the 'deploy' group should be present
        assert set(tools.keys()) == {"deploy"}

    def test_preserves_alphabetical_order_within_group(self, monkeypatch):
        _path_with_only(monkeypatch, ["vercel", "fly", "netlify"])
        tools = discover_installed_tools()
        assert tools["deploy"] == ["fly", "netlify", "vercel"]

    def test_every_tool_in_groups_is_alphabetized(self):
        """Guardrail — prevents the next contributor from appending to the
        list and letting the prompt output drift out of sorted order."""
        for group, names in TOOL_GROUPS.items():
            assert names == sorted(names), (
                f"TOOL_GROUPS['{group}'] must stay alphabetized"
            )


class TestFormatting:
    def test_empty_input_returns_sentinel_string(self):
        assert "none detected" in format_tools_for_prompt({})

    def test_renders_groups_on_separate_lines(self):
        output = format_tools_for_prompt({
            "vcs": ["git", "gh"],
            "deploy": ["railway"],
        })
        lines = output.splitlines()
        assert lines[0] == "vcs: git, gh"
        assert lines[1] == "deploy: railway"
