"""Tests for the refinement-grounding guard in ``Coder``.

Autumn-mail dogfood cycle 4 (Finding F1) surfaced a silent
miscategorization: the planner emitted a refinement against
``Sources/AutumnMail/GmailClient.swift`` while only
``AutumnMailApp.swift`` existed on HEAD. The coder created the file
from scratch — same diff, wrong category. Tests passed; nothing
flagged the contradiction.

These tests cover:
  - Refinement with all cited files present runs normally.
  - Refinement with a missing cited file raises ``RefinementGroundingError``.
  - Expansion with missing cited files runs normally (expansions are
    allowed to net-create files).
  - Refinement with no cited files passes (legacy / let-coder-determine).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from sentinel.roles.coder import (
    RefinementGroundingError,
    _check_refinement_grounding,
)
from sentinel.roles.planner import WorkItem

if TYPE_CHECKING:
    from pathlib import Path


def _init_git_repo(repo: Path, tracked_files: dict[str, str]) -> None:
    """Initialise a git repo with the given tracked files committed."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo, check=True, capture_output=True,
    )
    # Local identity so commit succeeds in CI without global config.
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True, capture_output=True,
    )
    for relpath, content in tracked_files.items():
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        subprocess.run(
            ["git", "add", "--", relpath],
            cwd=repo, check=True, capture_output=True,
        )
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"],
        cwd=repo, check=True, capture_output=True,
    )


def _make_work_item(
    *,
    title: str = "Harden the Gmail client",
    files: list[str | dict] | None = None,
    kind: str = "refine",
) -> WorkItem:
    return WorkItem(
        id="t1",
        title=title,
        description="Improve robustness of the existing client.",
        type="refactor",
        priority="high",
        complexity=2,
        files=files or [],
        acceptance_criteria=["Tests pass."],
        kind=kind,  # type: ignore[arg-type]
    )


def test_refinement_with_present_files_passes(tmp_path: Path) -> None:
    """All cited files exist on HEAD — no error."""
    _init_git_repo(tmp_path, {"src/foo.py": "x = 1\n"})

    work_item = _make_work_item(files=[{"path": "src/foo.py", "rationale": "fix"}])

    # Should NOT raise.
    _check_refinement_grounding(work_item, str(tmp_path))


def test_refinement_with_missing_file_raises(tmp_path: Path) -> None:
    """Cycle-4 reproduction: planner cites a file absent from HEAD —
    coder must refuse to silently net-create it."""
    _init_git_repo(tmp_path, {"Sources/AutumnMail/AutumnMailApp.swift": "// app\n"})

    work_item = _make_work_item(
        title="Harden GmailClient.swift",
        files=[
            {"path": "Sources/AutumnMail/GmailClient.swift", "rationale": "harden"},
            {"path": "Sources/AutumnMail/AutumnMailApp.swift", "rationale": "wire"},
        ],
    )

    with pytest.raises(RefinementGroundingError) as excinfo:
        _check_refinement_grounding(work_item, str(tmp_path))

    msg = str(excinfo.value)
    assert "Harden GmailClient.swift" in msg
    assert "Sources/AutumnMail/GmailClient.swift" in msg
    # The present file should NOT show up in the missing list.
    assert "Sources/AutumnMail/AutumnMailApp.swift" not in msg.split("missing")[0]
    # Operator-actionable hint about the category mismatch.
    assert "expansion" in msg.lower()


def test_expansion_with_missing_files_passes(tmp_path: Path) -> None:
    """Expansions are net-new work — they are *expected* to cite files
    that don't yet exist on HEAD."""
    _init_git_repo(tmp_path, {"README.md": "# project\n"})

    work_item = _make_work_item(
        title="Add core gws CLI wrapper",
        files=[{"path": "Sources/AutumnMail/GmailClient.swift", "rationale": "new"}],
        kind="expand",
    )

    # Should NOT raise — expansions may net-create files.
    _check_refinement_grounding(work_item, str(tmp_path))


def test_refinement_with_no_files_passes(tmp_path: Path) -> None:
    """Legacy items / hand-authored items may have no cited files; the
    grounding check has nothing to verify and must pass through."""
    _init_git_repo(tmp_path, {"README.md": "# project\n"})

    work_item = _make_work_item(files=[])

    _check_refinement_grounding(work_item, str(tmp_path))


def test_refinement_with_bare_string_paths(tmp_path: Path) -> None:
    """Legacy ``files: list[str]`` shape must work the same as the
    dict shape — same path-existence semantics."""
    _init_git_repo(tmp_path, {"src/present.py": "x = 1\n"})

    present = _make_work_item(files=["src/present.py"])
    _check_refinement_grounding(present, str(tmp_path))

    missing = _make_work_item(
        title="Refactor missing", files=["src/missing.py"],
    )
    with pytest.raises(RefinementGroundingError) as excinfo:
        _check_refinement_grounding(missing, str(tmp_path))
    assert "src/missing.py" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Post-execution category-drift guard (Fix 3 — Finding F1, second guard)
# ---------------------------------------------------------------------------


def test_added_paths_in_diff_detects_untracked(tmp_path: Path) -> None:
    """Net-new files (untracked) must surface in the added list."""
    from sentinel.roles.coder import _added_paths_in_diff

    _init_git_repo(tmp_path, {"src/old.py": "x = 1\n"})
    # Simulate a coder that created a new file.
    (tmp_path / "src" / "new.py").write_text("y = 2\n")

    added = _added_paths_in_diff(str(tmp_path))
    assert added == ["src/new.py"]


def test_added_paths_in_diff_skips_modifications(tmp_path: Path) -> None:
    """Modifying an existing file is not "added" — that's the whole
    point of the guard. A pure-modification diff must report empty."""
    from sentinel.roles.coder import _added_paths_in_diff

    _init_git_repo(tmp_path, {"src/old.py": "x = 1\n"})
    # Simulate a coder that only modified the existing file.
    (tmp_path / "src" / "old.py").write_text("x = 999\n")

    added = _added_paths_in_diff(str(tmp_path))
    assert added == []


def test_added_paths_in_diff_excludes_sentinel_artifacts(tmp_path: Path) -> None:
    """``.sentinel/`` and ``.claude/`` are sentinel's own scaffolding;
    they don't count as user-facing additions."""
    from sentinel.roles.coder import _added_paths_in_diff

    _init_git_repo(tmp_path, {"src/old.py": "x = 1\n"})
    (tmp_path / ".sentinel").mkdir(exist_ok=True)
    (tmp_path / ".sentinel" / "executions").mkdir(exist_ok=True)
    (tmp_path / ".sentinel" / "executions" / "wat.md").write_text("trace\n")
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text("{}\n")

    assert _added_paths_in_diff(str(tmp_path)) == []


def test_refinement_warning_logged_when_files_added(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanity check the wiring: when ``_added_paths_in_diff`` returns
    non-empty for a refinement, a WARNING log line names the work item
    and the added paths so the operator can see the category drift."""
    import logging

    from sentinel.roles.coder import _added_paths_in_diff

    _init_git_repo(tmp_path, {"src/old.py": "x = 1\n"})
    (tmp_path / "src" / "drift.py").write_text("y = 2\n")

    work_item = _make_work_item(
        title="Harden the old module", files=["src/old.py"],
    )

    with caplog.at_level(logging.WARNING, logger="sentinel.roles.coder"):
        added = _added_paths_in_diff(str(tmp_path))
        # Mirror the production wiring — see Coder.execute.
        if work_item.kind == "refine" and added:
            logging.getLogger("sentinel.roles.coder").warning(
                "Refinement %r created new files: %s. Refinements should "
                "improve existing code; consider re-classifying as expansion.",
                work_item.title, added,
            )

    matching = [
        rec for rec in caplog.records
        if "created new files" in rec.getMessage()
    ]
    assert matching, "expected a category-drift warning log line"
    msg = matching[0].getMessage()
    assert "Harden the old module" in msg
    assert "src/drift.py" in msg


def test_refinement_with_no_added_files_emits_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A pure-modification refinement must NOT warn — that's the happy
    path the guard is designed to leave alone."""
    import logging

    from sentinel.roles.coder import _added_paths_in_diff

    _init_git_repo(tmp_path, {"src/old.py": "x = 1\n"})
    (tmp_path / "src" / "old.py").write_text("x = 999\n")

    work_item = _make_work_item(files=["src/old.py"])

    with caplog.at_level(logging.WARNING, logger="sentinel.roles.coder"):
        added = _added_paths_in_diff(str(tmp_path))
        if work_item.kind == "refine" and added:
            logging.getLogger("sentinel.roles.coder").warning(
                "Refinement %r created new files: %s.",
                work_item.title, added,
            )

    matching = [
        rec for rec in caplog.records
        if "created new files" in rec.getMessage()
    ]
    assert not matching, (
        f"unexpected category-drift warning for pure-modification diff: {matching}"
    )
