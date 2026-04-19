"""Tests for the planner pre-flight semantic dedupe (Finding F4).

Background: autumn-mail dogfood cycle 4 surfaced 12 proposals queued
in ``.sentinel/proposals/`` — three of them slightly-different flavors
of "gws CLI wrapper for Gmail I/O". Sentinel's rejection memory only
catches exact-title matches, so the planner kept regenerating
near-duplicates each cycle.

The fix: a Jaccard-similarity check on the title+rationale token set
of every new proposal against the bag of existing proposals (any
status). Above the threshold, skip.

These tests cover:
  - The 3 actual gws-wrapper titles from autumn-mail are caught when a
    fourth similar proposal arrives.
  - Genuinely distinct topics (gws wrapper vs MLX inference) survive.
  - Same-rationale-different-title is also caught (the harder case).
  - Round-trip through ``_load_all_proposals`` parses title+why.
  - The full ``_write_proposals`` integration drops near-dups silently
    and writes only the survivors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sentinel.cli.plan_cmd import (
    _filter_near_duplicate_proposals,
    _jaccard_similarity,
    _load_all_proposals,
    _proposal_keyword_set,
    _write_proposals,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Token + similarity primitives
# ---------------------------------------------------------------------------


def test_keyword_set_drops_short_and_stopwords() -> None:
    """Tokens shorter than 3 chars and English stopwords are noise."""
    tokens = _proposal_keyword_set(
        "Implement the gws CLI wrapper for Gmail I/O on macOS",
    )
    assert "gws" in tokens
    assert "wrapper" in tokens
    assert "gmail" in tokens
    assert "macos" in tokens
    # Stopwords + 2-char tokens dropped:
    assert "the" not in tokens
    assert "for" not in tokens
    assert "on" not in tokens
    # I/O explodes into "i" (dropped, len<3) and "o" (dropped, len<3)
    assert "i" not in tokens
    assert "o" not in tokens


def test_keyword_set_lowercases() -> None:
    """Comparison must be case-insensitive."""
    a = _proposal_keyword_set("GWS Wrapper")
    b = _proposal_keyword_set("gws wrapper")
    assert a == b


def test_jaccard_identical_sets_is_one() -> None:
    s = _proposal_keyword_set("gws wrapper for gmail")
    assert _jaccard_similarity(s, s) == 1.0


def test_jaccard_disjoint_is_zero() -> None:
    a = _proposal_keyword_set("gws wrapper")
    b = _proposal_keyword_set("MLX inference")
    assert _jaccard_similarity(a, b) == 0.0


def test_jaccard_both_empty_is_zero_not_undefined() -> None:
    """Empty/empty must not divide-by-zero — return 0.0 cleanly."""
    empty: frozenset[str] = frozenset()
    assert _jaccard_similarity(empty, empty) == 0.0


# ---------------------------------------------------------------------------
# Filter behavior — the load-bearing tests
# ---------------------------------------------------------------------------


def _make_action(title: str, why: str = "") -> dict:
    return {
        "title": title,
        "why": why,
        "kind": "expand",
    }


def _make_existing(title: str, why: str = "") -> dict:
    return {"title": title, "why": why, "path": "/fake"}


def test_three_gws_wrappers_dedupe_a_fourth() -> None:
    """Reproduces the autumn-mail finding: when 3 flavors of gws-CLI-
    wrapper proposal already exist, a 4th near-duplicate is skipped.

    The actual cycle-4 titles overlap heavily on the noun stems
    (gws, cli, wrapper, gmail, swift) — once stopwords and short
    tokens are dropped, the surviving keyword sets are nearly
    identical, so Jaccard >> threshold.
    """
    existing = [
        _make_existing(
            "Implement core gws CLI wrapper for Gmail",
            "Build a Swift wrapper around the gws CLI to handle Gmail "
            "list, fetch, and send operations from the macOS app.",
        ),
        _make_existing(
            "Build gws wrapper layer for Gmail interaction",
            "Wrap the gws CLI so SwiftUI views call a Swift API to do "
            "Gmail list, fetch, and send operations from macOS.",
        ),
        _make_existing(
            "Add Swift gws CLI integration for Gmail messages",
            "Build Swift integration that calls the gws CLI to handle "
            "Gmail message list, fetch, and send from macOS.",
        ),
    ]
    new = [
        _make_action(
            "Implement gws CLI wrapper for Gmail I/O",
            "Build a Swift wrapper around the gws CLI to handle Gmail "
            "list, fetch, and send operations from the macOS app.",
        ),
    ]

    kept, skipped = _filter_near_duplicate_proposals(new, existing)

    assert kept == [], (
        f"4th gws-wrapper variant should be deduped; survived: {kept}"
    )
    assert len(skipped) == 1
    matched_action, matched_existing, sim = skipped[0]
    assert matched_action == new[0]
    # The match is some existing proposal with substantial overlap.
    assert sim > 0.6, (
        f"expected high similarity; got {sim:.2f} against "
        f"{matched_existing['title']!r}"
    )


def test_distinct_topics_survive() -> None:
    """A genuinely different proposal must NOT be deduped against gws-
    wrapper. This is the false-positive guard."""
    existing = [
        _make_existing(
            "Implement core gws CLI wrapper for Gmail",
            "Need a Swift wrapper around the gws CLI for Gmail I/O.",
        ),
    ]
    new = [
        _make_action(
            "Wire MLX Swift inference for local LLM drafting",
            "Download an Apple MLX-format model and run on-device "
            "inference for draft replies.",
        ),
    ]

    kept, skipped = _filter_near_duplicate_proposals(new, existing)

    assert kept == new, "MLX inference is a distinct topic, must survive"
    assert skipped == []


def test_same_rationale_different_title_caught() -> None:
    """The harder case: titles look unrelated but the rationale text
    overlaps heavily — still a duplicate idea."""
    existing = [
        _make_existing(
            "Add a wrapper around gws",
            "Need a Swift abstraction over gws CLI to keep network IO "
            "off the SwiftUI layer and centralize JSON parsing of "
            "Gmail message bodies.",
        ),
    ]
    new = [
        _make_action(
            "Refactor Gmail integration layer",
            "Need a Swift abstraction over gws CLI to keep network IO "
            "off the SwiftUI layer and centralize JSON parsing of "
            "Gmail message bodies.",
        ),
    ]

    kept, skipped = _filter_near_duplicate_proposals(new, existing)

    assert kept == [], (
        "duplicate rationale should be caught even when titles diverge"
    )
    assert len(skipped) == 1


def test_no_existing_proposals_keeps_all_new() -> None:
    """Sanity: an empty existing set must not skip anything."""
    new = [_make_action("anything"), _make_action("else")]
    kept, skipped = _filter_near_duplicate_proposals(new, [])
    assert kept == new
    assert skipped == []


def test_empty_token_action_is_kept() -> None:
    """An action whose title+why has no usable tokens can't be compared
    meaningfully — let it through (the lack of rationale is a separate
    bug to surface, not a similarity match)."""
    existing = [_make_existing("Implement gws wrapper", "rationale here")]
    new = [_make_action("", "")]
    kept, skipped = _filter_near_duplicate_proposals(new, existing)
    assert new[0] in kept
    assert skipped == []


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _seed_proposal(
    proposals_dir, *, fname: str, title: str, status: str = "pending",
    why: str = "Some rationale.",
) -> None:
    body = (
        f"# Proposal: {title}\n\n"
        f"**Status:** {status}\n\n"
        "**Lens:** integration\n"
        "**Impact:** high\n"
        "**Source scan:** `s.md`\n\n"
        f"## Why\n\n{why}\n\n"
        "## Notes\n\nx\n"
    )
    (proposals_dir / fname).write_text(body)


def test_load_all_proposals_includes_pending(tmp_path: Path) -> None:
    """``_load_all_proposals`` must return every status, not just
    approved — pending is the most-important class to dedupe against."""
    proposals_dir = tmp_path / ".sentinel" / "proposals"
    proposals_dir.mkdir(parents=True)
    _seed_proposal(
        proposals_dir, fname="2026-04-18-pending.md",
        title="Pending one", status="pending", why="Pending why",
    )
    _seed_proposal(
        proposals_dir, fname="2026-04-18-approved.md",
        title="Approved one", status="approved", why="Approved why",
    )
    _seed_proposal(
        proposals_dir, fname="2026-04-18-rejected.md",
        title="Rejected one", status="rejected", why="Rejected why",
    )

    items = _load_all_proposals(tmp_path)
    titles = sorted(it["title"] for it in items)
    assert titles == ["Approved one", "Pending one", "Rejected one"]


def test_load_all_proposals_missing_dir_is_empty(tmp_path: Path) -> None:
    """No ``.sentinel/proposals/`` yet means no prior proposals to
    dedupe against — return [] instead of crashing."""
    assert _load_all_proposals(tmp_path) == []


# ---------------------------------------------------------------------------
# Integration through _write_proposals
# ---------------------------------------------------------------------------


def test_write_proposals_skips_near_duplicate(tmp_path: Path) -> None:
    """End-to-end: when an existing proposal is on disk, a similar new
    expansion must NOT be written."""
    proposals_dir = tmp_path / ".sentinel" / "proposals"
    proposals_dir.mkdir(parents=True)
    _seed_proposal(
        proposals_dir, fname="2026-04-18-existing.md",
        title="Implement core gws CLI wrapper for Gmail",
        why="Wrap the gws CLI for Gmail I/O from Swift.",
    )

    new_actions = [
        {
            "title": "Implement gws CLI wrapper for Gmail I/O",
            "why": "Wrap the gws CLI to do Gmail I/O from Swift.",
            "kind": "expand",
            "lens": "integration",
            "impact": "high",
            "files": [],
        },
    ]
    fake_scan = tmp_path / "scan.md"
    fake_scan.write_text("# scan\n")

    written = _write_proposals(tmp_path, new_actions, fake_scan)

    assert written == [], (
        "near-duplicate expansion should not produce a new proposal file"
    )
    # And the existing proposal must remain untouched.
    existing_files = sorted(
        p.name for p in proposals_dir.glob("*.md")
    )
    assert existing_files == ["2026-04-18-existing.md"]


def test_write_proposals_keeps_distinct_new(tmp_path: Path) -> None:
    """End-to-end: a genuinely distinct new expansion still gets
    written, even when some prior proposal exists."""
    proposals_dir = tmp_path / ".sentinel" / "proposals"
    proposals_dir.mkdir(parents=True)
    _seed_proposal(
        proposals_dir, fname="2026-04-18-existing.md",
        title="Implement core gws CLI wrapper for Gmail",
        why="Wrap the gws CLI for Gmail I/O from Swift.",
    )

    new_actions = [
        {
            "title": "Wire MLX Swift inference for local LLM drafting",
            "why": "Download an Apple MLX-format model and run on-device.",
            "kind": "expand",
            "lens": "performance",
            "impact": "medium",
            "files": [],
        },
    ]
    fake_scan = tmp_path / "scan.md"
    fake_scan.write_text("# scan\n")

    written = _write_proposals(tmp_path, new_actions, fake_scan)

    assert len(written) == 1, "distinct expansion must be written"
    assert "mlx-swift-inference" in written[0].name.lower() or \
           "mlx" in written[0].name.lower()
