"""Tests for the optional ``Lens.scope`` field (Finding F2).

Background: autumn-mail dogfood cycle 4 surfaced that Sentinel's
privacy-compliance lens (read "no cloud LLMs" from CLAUDE.md) was
applied globally — proposing the removal of cloud LLM references from
``.sentinel/config.toml`` and ``setup.sh``, which legitimately use a
cloud reviewer per Garage Doctrine 0002. The fix is an optional
``scope: list[str]`` field on ``Lens``: when set, the evaluator filters
the considered file_tree to matching path globs.

These tests cover:
  - Unscoped (legacy) lens sees the full file_tree.
  - Scoped lens filters file_tree lines that don't match.
  - The Markdown parser round-trips a ``### Scope`` section through
    load + save without dropping data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sentinel.roles.monitor import (
    Lens,
    _filter_file_tree_by_scope,
    _load_locked_lenses,
    _save_locked_lenses,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Filter behavior
# ---------------------------------------------------------------------------

_FILE_TREE = "\n".join([
    "./Sources/AutumnMail/AutumnMailApp.swift",
    "./Sources/AutumnMail/GmailClient.swift",
    "./Tests/AutumnMailTests/SmokeTests.swift",
    "./.sentinel/config.toml",
    "./.sentinel/lenses.md",
    "./setup.sh",
    "./README.md",
])


def test_unscoped_lens_sees_full_file_tree() -> None:
    """Empty ``scope`` is the legacy default — full tree visible."""
    out = _filter_file_tree_by_scope(_FILE_TREE, scope=[])
    assert out == _FILE_TREE


def test_scope_filters_out_toolchain_files() -> None:
    """Sources/** scope must keep app code, drop dev-toolchain config."""
    out = _filter_file_tree_by_scope(_FILE_TREE, scope=["Sources/**"])
    lines = out.splitlines()
    # App sources kept.
    assert "./Sources/AutumnMail/AutumnMailApp.swift" in lines
    assert "./Sources/AutumnMail/GmailClient.swift" in lines
    # Dev-toolchain dropped — that's the whole point of the field.
    assert "./.sentinel/config.toml" not in lines
    assert "./setup.sh" not in lines
    assert "./README.md" not in lines
    # Tests directory dropped because the scope was Sources-only.
    assert "./Tests/AutumnMailTests/SmokeTests.swift" not in lines


def test_scope_supports_multiple_globs() -> None:
    """Two globs combine via OR — both Sources/** and Tests/** kept."""
    out = _filter_file_tree_by_scope(
        _FILE_TREE, scope=["Sources/**", "Tests/**"],
    )
    lines = out.splitlines()
    assert "./Sources/AutumnMail/AutumnMailApp.swift" in lines
    assert "./Tests/AutumnMailTests/SmokeTests.swift" in lines
    assert "./.sentinel/config.toml" not in lines


def test_scope_against_empty_tree_is_safe() -> None:
    """Empty file_tree returns empty — no crash, no spurious matches."""
    assert _filter_file_tree_by_scope("", scope=["Sources/**"]) == ""
    assert _filter_file_tree_by_scope("   \n  ", scope=["Sources/**"]) == "   \n  "


def test_invalid_glob_pattern_does_not_match() -> None:
    """Bad globs simply yield no matches; sane fallback (don't crash)."""
    # fnmatch is permissive — even unusual patterns shouldn't crash.
    out = _filter_file_tree_by_scope(_FILE_TREE, scope=["[invalid"])
    # An empty string vs newline — either is fine; the assertion is
    # "no crash + no spurious match".
    assert "./Sources/AutumnMail/AutumnMailApp.swift" not in out


# ---------------------------------------------------------------------------
# Markdown round-trip
# ---------------------------------------------------------------------------


def test_load_parses_scope_section(tmp_path: Path) -> None:
    """A lens with ``### Scope`` must be parsed into ``Lens.scope``."""
    sentinel_dir = tmp_path / ".sentinel"
    sentinel_dir.mkdir()
    (sentinel_dir / "lenses.md").write_text(
        "# Sentinel Lenses\n\n---\n\n"
        "## privacy-compliance\n\n"
        "Privacy guard.\n\n"
        "### What to look for\n\nLeaks.\n\n"
        "### Questions\n\n- Is data leaving?\n\n"
        "### Scope\n\n- Sources/**\n- Tests/**\n\n"
        "---\n",
    )

    lenses = _load_locked_lenses(tmp_path)
    assert lenses is not None and len(lenses) == 1
    lens = lenses[0]
    assert lens.name == "privacy-compliance"
    assert lens.scope == ["Sources/**", "Tests/**"]


def test_load_unscoped_lens_yields_empty_scope(tmp_path: Path) -> None:
    """A lens without ``### Scope`` parses to ``scope=[]`` — global default."""
    sentinel_dir = tmp_path / ".sentinel"
    sentinel_dir.mkdir()
    (sentinel_dir / "lenses.md").write_text(
        "# Sentinel Lenses\n\n---\n\n"
        "## architecture\n\n"
        "Big-picture design.\n\n"
        "### What to look for\n\nLayering.\n\n"
        "### Questions\n\n- Are layers respected?\n\n"
        "---\n",
    )

    lenses = _load_locked_lenses(tmp_path)
    assert lenses is not None and len(lenses) == 1
    assert lenses[0].scope == []


def test_save_emits_scope_section(tmp_path: Path) -> None:
    """A lens whose ``scope`` is non-empty must surface in the saved file."""
    out_path = _save_locked_lenses(tmp_path, [
        Lens(
            name="privacy-compliance",
            description="Privacy guard.",
            what_to_look_for="Leaks.",
            questions=["Is data leaving?"],
            scope=["Sources/**", "Tests/**"],
        ),
    ])
    body = out_path.read_text()
    assert "### Scope" in body
    assert "- Sources/**" in body
    assert "- Tests/**" in body


def test_save_omits_scope_section_for_global_lens(tmp_path: Path) -> None:
    """An empty ``scope`` is the legacy default — must NOT emit a Scope
    section under the lens body. (The preamble may mention the field
    in its instructions, but no per-lens section is written.)"""
    out_path = _save_locked_lenses(tmp_path, [
        Lens(
            name="architecture",
            description="Design.",
            what_to_look_for="Layering.",
            questions=["Layered?"],
        ),
    ])
    body = out_path.read_text()
    # Find the architecture lens body and verify no `### Scope` appears
    # between its `## architecture` header and the trailing `---`.
    head = body.index("## architecture")
    tail = body.index("\n---\n", head)
    lens_body = body[head:tail]
    assert "### Scope" not in lens_body, (
        f"global lens must not write a Scope section; got: {lens_body!r}"
    )


def test_round_trip_preserves_scope(tmp_path: Path) -> None:
    """Save then load must yield the same ``scope`` list."""
    original = Lens(
        name="privacy-compliance",
        description="Privacy guard.",
        what_to_look_for="Leaks.",
        questions=["Is data leaving?"],
        scope=["Sources/**", "Tests/**"],
    )
    _save_locked_lenses(tmp_path, [original])

    loaded = _load_locked_lenses(tmp_path)
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0].scope == ["Sources/**", "Tests/**"]
