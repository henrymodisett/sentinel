"""Tests for Autumn Garage sibling detection in `sentinel status`.

Covers two core scenarios:
  1. Siblings absent — no CLI on PATH, no project markers — status
     reports "not installed" for both and exits 0 (absence is normal).
  2. Siblings present — CLI stubs on PATH + markers on disk — status
     reports parsed version numbers for both.

Plus a handful of unit-level cases for the glyph logic (partial
states) and version parsing, since those are the failure modes most
likely to silently regress.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path  # noqa: TC003 — runtime use via tmp_path
from unittest.mock import patch

from click.testing import CliRunner

from sentinel.cli.main import main
from sentinel.siblings import (
    SiblingStatus,
    _parse_version,
    detect_cortex,
    detect_siblings,
    detect_touchstone,
    format_sibling_line,
)

_CORTEX_STUB = """#!/usr/bin/env bash
case "$1" in
  version) echo "cortex 0.1.0"; exit 0 ;;
  *) exit 0 ;;
esac
"""

_TOUCHSTONE_STUB = """#!/usr/bin/env bash
case "$1" in
  version) echo "touchstone 1.1.0"; exit 0 ;;
  *) exit 0 ;;
esac
"""


def _install_sibling_stubs(
    bin_dir: Path,
    *,
    cortex: bool = False,
    touchstone: bool = False,
) -> None:
    """Drop executable stubs for the requested sibling CLIs into bin_dir."""
    if cortex:
        p = bin_dir / "cortex"
        p.write_text(_CORTEX_STUB)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    if touchstone:
        p = bin_dir / "touchstone"
        p.write_text(_TOUCHSTONE_STUB)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _write_sentinel_config(project: Path) -> None:
    """Minimal .sentinel/config.toml so status proceeds past its early exit."""
    sentinel_dir = project / ".sentinel"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    (sentinel_dir / "config.toml").write_text(
        f"""
[project]
name = "{project.name}"
path = "{project}"

[budget]
daily_limit_usd = 15.0
warn_at_usd = 12.0

[roles.monitor]
provider = "local"
model = "qwen2.5-coder:14b"

[roles.researcher]
provider = "gemini"
model = "gemini-2.5-flash"

[roles.planner]
provider = "claude"
model = "sonnet"

[roles.coder]
provider = "claude"
model = "sonnet"

[roles.reviewer]
provider = "openai"
model = "gpt-5"
""".strip()
    )


class TestVersionParsing:
    def test_parses_plain_semver(self) -> None:
        assert _parse_version("cortex 0.1.0") == "0.1.0"

    def test_parses_version_with_prerelease(self) -> None:
        assert _parse_version("cortex 0.3.1-dev") == "0.3.1-dev"

    def test_returns_none_on_no_match(self) -> None:
        assert _parse_version("no version here") is None

    def test_picks_first_semver_token(self) -> None:
        # If tool prints multiple versions (e.g. SPEC + CLI), we take the first.
        assert _parse_version("SPEC 0.3.1-dev, CLI 0.1.0") == "0.3.1-dev"


class TestGlyphFormatting:
    def test_installed_and_marker_uses_check_glyph(self) -> None:
        status = SiblingStatus(
            name="cortex",
            cli_installed=True,
            project_marker_present=True,
            version="0.1.0",
            marker_label=".cortex/",
        )
        line = format_sibling_line(status)
        assert "✓" in line
        assert "0.1.0" in line
        assert ".cortex/ present" in line

    def test_neither_uses_dash_glyph(self) -> None:
        status = SiblingStatus(
            name="touchstone",
            cli_installed=False,
            project_marker_present=False,
            version=None,
            marker_label=".touchstone-config",
        )
        line = format_sibling_line(status)
        assert "—" in line
        assert "not installed" in line

    def test_partial_cli_only_uses_bang_glyph(self) -> None:
        status = SiblingStatus(
            name="cortex",
            cli_installed=True,
            project_marker_present=False,
            version="0.1.0",
            marker_label=".cortex/",
        )
        line = format_sibling_line(status)
        assert "!" in line
        assert ".cortex/ absent" in line

    def test_partial_marker_only_uses_bang_glyph(self) -> None:
        status = SiblingStatus(
            name="touchstone",
            cli_installed=False,
            project_marker_present=True,
            version=None,
            marker_label=".touchstone-config",
        )
        line = format_sibling_line(status)
        assert "!" in line
        assert "not installed" in line
        assert ".touchstone-config present" in line


class TestDetectSiblings:
    def test_absent_when_no_cli_no_marker(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        # Empty PATH (well, no LLM tools) + empty project.
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", f"{empty_bin}:/usr/bin:/bin")

        cortex = detect_cortex(tmp_path)
        touchstone = detect_touchstone(tmp_path)

        assert cortex.cli_installed is False
        assert cortex.project_marker_present is False
        assert cortex.version is None
        assert touchstone.cli_installed is False
        assert touchstone.project_marker_present is False
        assert touchstone.version is None

    def test_present_when_stubs_and_markers(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _install_sibling_stubs(bin_dir, cortex=True, touchstone=True)
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

        (tmp_path / ".cortex").mkdir()
        (tmp_path / ".touchstone-config").write_text("# marker\n")

        siblings = detect_siblings(tmp_path)
        by_name = {s.name: s for s in siblings}

        assert by_name["cortex"].cli_installed is True
        assert by_name["cortex"].project_marker_present is True
        assert by_name["cortex"].version == "0.1.0"

        assert by_name["touchstone"].cli_installed is True
        assert by_name["touchstone"].project_marker_present is True
        assert by_name["touchstone"].version == "1.1.0"

    def test_timeout_yields_none_version(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        # If `<tool> version` hangs past the 3s cap we surface None, not raise.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _install_sibling_stubs(bin_dir, cortex=True)
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

        real_run = subprocess.run

        def _timeout_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            if args and isinstance(args[0], list) and args[0][:1] == ["cortex"]:
                raise subprocess.TimeoutExpired(cmd=args[0], timeout=3.0)
            return real_run(*args, **kwargs)

        with patch("sentinel.siblings.subprocess.run", side_effect=_timeout_run):
            status = detect_cortex(tmp_path)
        assert status.cli_installed is True
        assert status.version is None


class TestStatusCommandOutput:
    def test_siblings_absent_prints_not_installed_and_exits_zero(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        _write_sentinel_config(tmp_path)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", f"{empty_bin}:/usr/bin:/bin")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        assert "Autumn Garage siblings:" in result.output
        # Both siblings reported as not-installed; absence is normal.
        assert result.output.count("not installed") == 2
        assert "cortex" in result.output
        assert "touchstone" in result.output

    def test_siblings_present_prints_version_numbers(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        _write_sentinel_config(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _install_sibling_stubs(bin_dir, cortex=True, touchstone=True)
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        monkeypatch.chdir(tmp_path)

        (tmp_path / ".cortex").mkdir()
        (tmp_path / ".touchstone-config").write_text("# marker\n")

        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        assert "Autumn Garage siblings:" in result.output
        assert "cortex 0.1.0" in result.output
        assert "touchstone 1.1.0" in result.output
        assert ".cortex/ present" in result.output
        assert ".touchstone-config present" in result.output
