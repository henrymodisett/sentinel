"""Tests for plan_cmd: parser, backlog renderer, and round-trip correctness.

These tests exercise _parse_actions_from_scan and _write_backlog without
making any LLM calls. All inputs are synthetic markdown strings written to
temp files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sentinel.cli.plan_cmd import _parse_actions_from_scan, _write_backlog

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_scan(tmp_path: Path, body: str) -> Path:
    """Write a minimal scan file with a Top Actions section containing body."""
    content = (
        "# Sentinel Scan — 2026-01-01-1200\n\n"
        "**Overall score:** 80/100\n\n"
        "## Top Actions\n\n"
        + body
        + "\n## Lens Evaluations\n\n"
    )
    p = tmp_path / "scan.md"
    p.write_text(content)
    return p


def _new_action_md(
    n: int = 1,
    title: str = "Fix something",
    kind: str = "refine",
    lens: str = "code-quality",
    why: str = "It is broken",
    impact: str = "High",
    files: list[tuple[str, str]] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification: list[str] | None = None,
    out_of_scope: list[str] | None = None,
) -> str:
    """Build a new-format top_action markdown block."""
    lines = [
        f"### {n}. {title}",
        "",
        f"**Kind:** {kind}",
        f"**Lens:** {lens}",
        f"**Why:** {why}",
        f"**Impact:** {impact}",
    ]
    if files is not None:
        lines.append("**Files:**")
        for path, rationale in files:
            if rationale:
                lines.append(f"- `{path}` — {rationale}")
            else:
                lines.append(f"- `{path}`")
    if acceptance_criteria is not None:
        lines.append("**Acceptance criteria:**")
        for i, criterion in enumerate(acceptance_criteria, 1):
            lines.append(f"{i}. {criterion}")
    if verification is not None:
        lines.append("**Verification:**")
        for cmd in verification:
            lines.append(f"- `{cmd}`")
    if out_of_scope is not None:
        lines.append("**Out of scope:**")
        for item in out_of_scope:
            lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

class TestParseActionsFromScan:
    def test_parses_new_shape_files(self, tmp_path):
        body = _new_action_md(
            files=[
                ("src/sentinel/roles/monitor.py", "owns the schema"),
                ("tests/test_plan_cmd.py", "new test file"),
            ],
            acceptance_criteria=["uv run pytest exits 0"],
            verification=["uv run pytest"],
            out_of_scope=["src/sentinel/roles/coder.py"],
        )
        actions = _parse_actions_from_scan(_write_scan(tmp_path, body))

        assert len(actions) == 1
        a = actions[0]
        assert a["title"] == "Fix something"
        assert a["kind"] == "refine"
        assert a["lens"] == "code-quality"
        assert a["why"] == "It is broken"
        assert a["impact"] == "High"

        # files — new dict shape
        assert len(a["files"]) == 2
        assert a["files"][0] == {
            "path": "src/sentinel/roles/monitor.py",
            "rationale": "owns the schema",
        }
        assert a["files"][1] == {
            "path": "tests/test_plan_cmd.py",
            "rationale": "new test file",
        }

        # acceptance_criteria
        assert a["acceptance_criteria"] == ["uv run pytest exits 0"]

        # verification
        assert a["verification"] == ["uv run pytest"]

        # out_of_scope
        assert a["out_of_scope"] == ["src/sentinel/roles/coder.py"]

    def test_parses_multiple_acceptance_criteria(self, tmp_path):
        body = _new_action_md(
            acceptance_criteria=[
                "uv run pytest exits 0",
                "uv run ruff check src/ tests/ exits 0",
                "scan output contains Acceptance criteria section",
            ],
            verification=["uv run pytest", "uv run ruff check src/ tests/"],
            out_of_scope=[],
        )
        actions = _parse_actions_from_scan(_write_scan(tmp_path, body))
        a = actions[0]
        assert len(a["acceptance_criteria"]) == 3
        assert a["acceptance_criteria"][1] == "uv run ruff check src/ tests/ exits 0"
        assert len(a["verification"]) == 2

    def test_parses_empty_out_of_scope(self, tmp_path):
        body = _new_action_md(
            acceptance_criteria=["pytest exits 0"],
            verification=["uv run pytest"],
            out_of_scope=[],
        )
        actions = _parse_actions_from_scan(_write_scan(tmp_path, body))
        assert actions[0]["out_of_scope"] == []

    def test_parses_multiple_actions(self, tmp_path):
        body = (
            _new_action_md(
                n=1, title="First action",
                acceptance_criteria=["pytest exits 0"],
                verification=["uv run pytest"],
                out_of_scope=[],
            )
            + _new_action_md(
                n=2, title="Second action", kind="expand",
                acceptance_criteria=["feature works"],
                verification=["uv run pytest"],
                out_of_scope=["legacy code"],
            )
        )
        actions = _parse_actions_from_scan(_write_scan(tmp_path, body))
        assert len(actions) == 2
        assert actions[0]["title"] == "First action"
        assert actions[0]["kind"] == "refine"
        assert actions[1]["title"] == "Second action"
        assert actions[1]["kind"] == "expand"

    def test_backwards_compat_flat_files(self, tmp_path):
        """Legacy **Files:** a.py, b.py format produces list[dict] with empty rationale."""
        body = (
            "### 1. Old-style action\n\n"
            "**Kind:** refine\n"
            "**Lens:** testing\n"
            "**Why:** legacy\n"
            "**Impact:** medium\n"
            "**Files:** src/a.py, src/b.py, src/c.py\n\n"
        )
        actions = _parse_actions_from_scan(_write_scan(tmp_path, body))
        assert len(actions) == 1
        a = actions[0]
        assert len(a["files"]) == 3
        assert a["files"][0] == {"path": "src/a.py", "rationale": ""}
        assert a["files"][1] == {"path": "src/b.py", "rationale": ""}
        assert a["files"][2] == {"path": "src/c.py", "rationale": ""}
        # New fields default to empty lists when absent
        assert a["acceptance_criteria"] == []
        assert a["verification"] == []
        assert a["out_of_scope"] == []

    def test_files_without_rationale_in_new_format(self, tmp_path):
        """New-format file bullets without a rationale still parse."""
        body = (
            "### 1. Minimal files\n\n"
            "**Kind:** refine\n"
            "**Lens:** testing\n"
            "**Why:** x\n"
            "**Impact:** low\n"
            "**Files:**\n"
            "- `src/a.py`\n\n"
        )
        actions = _parse_actions_from_scan(_write_scan(tmp_path, body))
        a = actions[0]
        assert len(a["files"]) == 1
        assert a["files"][0]["path"] == "src/a.py"
        assert a["files"][0]["rationale"] == ""

    def test_empty_top_actions_section(self, tmp_path):
        p = _write_scan(tmp_path, "")
        assert _parse_actions_from_scan(p) == []

    def test_new_fields_default_when_missing(self, tmp_path):
        """An action parsed from old scan without new sections gets empty defaults."""
        body = (
            "### 1. Old action\n\n"
            "**Kind:** refine\n"
            "**Lens:** testing\n"
            "**Why:** old\n"
            "**Impact:** low\n\n"
        )
        actions = _parse_actions_from_scan(_write_scan(tmp_path, body))
        a = actions[0]
        assert a["acceptance_criteria"] == []
        assert a["verification"] == []
        assert a["out_of_scope"] == []
        assert a["files"] == []


# ---------------------------------------------------------------------------
# Backlog renderer tests
# ---------------------------------------------------------------------------

class TestWriteBacklog:
    def _make_action(self, **overrides) -> dict:
        base = {
            "title": "Fix the thing",
            "kind": "refine",
            "lens": "code-quality",
            "why": "It is broken",
            "impact": "High",
            "files": [
                {"path": "src/a.py", "rationale": "has the bug"},
                {"path": "tests/test_a.py", "rationale": "needs the test"},
            ],
            "acceptance_criteria": [
                "uv run pytest exits 0",
                "uv run ruff check src/ tests/ exits 0",
            ],
            "verification": ["uv run pytest", "uv run ruff check src/ tests/"],
            "out_of_scope": ["src/sentinel/roles/coder.py"],
        }
        base.update(overrides)
        return base

    def _setup(self, tmp_path):
        """Create .sentinel dir so _write_backlog can write backlog.md."""
        (tmp_path / ".sentinel").mkdir()
        scan_file = tmp_path / "scan.md"
        scan_file.write_text("# scan\n")
        return scan_file

    def test_renders_files_as_bullets(self, tmp_path):
        action = self._make_action()
        scan_file = self._setup(tmp_path)
        backlog = _write_backlog(tmp_path, [action], scan_file)
        content = backlog.read_text()
        assert "- `src/a.py` — has the bug" in content
        assert "- `tests/test_a.py` — needs the test" in content

    def test_renders_acceptance_criteria(self, tmp_path):
        action = self._make_action()
        scan_file = self._setup(tmp_path)
        backlog = _write_backlog(tmp_path, [action], scan_file)
        content = backlog.read_text()
        assert "**Acceptance criteria:**" in content
        assert "1. uv run pytest exits 0" in content
        assert "2. uv run ruff check src/ tests/ exits 0" in content

    def test_renders_verification(self, tmp_path):
        action = self._make_action()
        scan_file = self._setup(tmp_path)
        backlog = _write_backlog(tmp_path, [action], scan_file)
        content = backlog.read_text()
        assert "**Verification:**" in content
        assert "- `uv run pytest`" in content
        assert "- `uv run ruff check src/ tests/`" in content

    def test_renders_out_of_scope(self, tmp_path):
        action = self._make_action()
        scan_file = self._setup(tmp_path)
        backlog = _write_backlog(tmp_path, [action], scan_file)
        content = backlog.read_text()
        assert "**Out of scope:**" in content
        assert "- src/sentinel/roles/coder.py" in content

    def test_omits_out_of_scope_section_when_empty(self, tmp_path):
        action = self._make_action(out_of_scope=[])
        scan_file = self._setup(tmp_path)
        backlog = _write_backlog(tmp_path, [action], scan_file)
        content = backlog.read_text()
        assert "**Out of scope:**" not in content

    def test_expand_actions_excluded_from_backlog(self, tmp_path):
        refine = self._make_action(title="Refine action", kind="refine")
        expand = self._make_action(title="Expand action", kind="expand")
        scan_file = self._setup(tmp_path)
        backlog = _write_backlog(tmp_path, [refine, expand], scan_file)
        content = backlog.read_text()
        assert "Refine action" in content
        assert "Expand action" not in content

    def test_files_without_rationale_render_without_dash(self, tmp_path):
        action = self._make_action(files=[{"path": "src/x.py", "rationale": ""}])
        scan_file = self._setup(tmp_path)
        backlog = _write_backlog(tmp_path, [action], scan_file)
        content = backlog.read_text()
        assert "- `src/x.py`\n" in content
        # The file bullet itself should NOT include the em-dash separator
        assert "- `src/x.py` —" not in content


# ---------------------------------------------------------------------------
# End-to-end round-trip test
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """Verify: action dict → scan markdown → parsed dict → backlog markdown
    all preserve every field without data loss."""

    def test_full_round_trip(self, tmp_path):
        # 1. Build synthetic action dict (as Monitor would produce)
        original_action = {
            "title": "Round-trip action",
            "kind": "refine",
            "lens": "testing",
            "why": "Tests are missing",
            "impact": "High — no coverage",
            "files": [
                {"path": "src/sentinel/cli/plan_cmd.py", "rationale": "owns parser"},
                {"path": "tests/test_plan_cmd.py", "rationale": "new tests go here"},
            ],
            "acceptance_criteria": [
                "uv run pytest exits 0",
                "parser returns files as list of dicts",
            ],
            "verification": [
                "uv run pytest",
                "uv run ruff check src/ tests/",
            ],
            "out_of_scope": [
                "src/sentinel/roles/coder.py",
                "src/sentinel/roles/reviewer.py",
            ],
        }

        # 2. Write scan markdown (mimic _persist_scan output)
        from sentinel.cli.scan_cmd import _persist_scan
        from sentinel.roles.monitor import ScanResult

        result = ScanResult(
            ok=True,
            top_actions=[original_action],
            overall_score=75,
            project_summary="Test project",
            raw_report="Test summary",
            strengths=["good"],
            critical_risks=["bad"],
        )
        scan_file = _persist_scan(tmp_path, result)

        # Verify the scan file was written into the scans/ subdirectory
        assert scan_file.exists()
        scan_text = scan_file.read_text()
        assert "**Files:**" in scan_text
        assert "- `src/sentinel/cli/plan_cmd.py` — owns parser" in scan_text
        assert "**Acceptance criteria:**" in scan_text
        assert "1. uv run pytest exits 0" in scan_text
        assert "**Verification:**" in scan_text
        assert "- `uv run pytest`" in scan_text
        assert "**Out of scope:**" in scan_text
        assert "- src/sentinel/roles/coder.py" in scan_text

        # 3. Parse scan back into dicts
        parsed = _parse_actions_from_scan(scan_file)
        assert len(parsed) == 1
        a = parsed[0]

        assert a["title"] == "Round-trip action"
        assert a["kind"] == "refine"
        assert a["lens"] == "testing"
        assert a["impact"] == "High — no coverage"

        assert len(a["files"]) == 2
        assert a["files"][0] == {
            "path": "src/sentinel/cli/plan_cmd.py",
            "rationale": "owns parser",
        }
        assert a["files"][1] == {
            "path": "tests/test_plan_cmd.py",
            "rationale": "new tests go here",
        }

        assert a["acceptance_criteria"] == [
            "uv run pytest exits 0",
            "parser returns files as list of dicts",
        ]
        assert a["verification"] == [
            "uv run pytest",
            "uv run ruff check src/ tests/",
        ]
        assert a["out_of_scope"] == [
            "src/sentinel/roles/coder.py",
            "src/sentinel/roles/reviewer.py",
        ]

        # 4. Write backlog and verify new fields are present
        backlog = _write_backlog(tmp_path, parsed, scan_file)
        backlog_text = backlog.read_text()

        assert "Round-trip action" in backlog_text
        assert "- `src/sentinel/cli/plan_cmd.py` — owns parser" in backlog_text
        assert "**Acceptance criteria:**" in backlog_text
        assert "1. uv run pytest exits 0" in backlog_text
        assert "2. parser returns files as list of dicts" in backlog_text
        assert "**Verification:**" in backlog_text
        assert "- `uv run pytest`" in backlog_text
        assert "- `uv run ruff check src/ tests/`" in backlog_text
        assert "**Out of scope:**" in backlog_text
        assert "- src/sentinel/roles/coder.py" in backlog_text
