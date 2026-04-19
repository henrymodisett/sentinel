"""Regression test for the filter-mirror invariant in ``_remaining_backlog_items``.

Codex review of the registry+rejections PR flagged: if ``_write_backlog``
filters refinements but ``_remaining_backlog_items`` re-parses the scan
without the same filter, ``sentinel work`` still executes items the
backlog markdown correctly omitted. That's worse than the pre-fix world
because the user sees one thing and the executor does another.

This test enforces that the executor's backlog equals the
post-filtering set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sentinel.cli.work_cmd import _remaining_backlog_items
from sentinel.integrations.rejections import record_rejection

if TYPE_CHECKING:
    from pathlib import Path


_SCAN_WITH_TWO_REFINEMENTS = """\
# Sentinel Scan — 2026-04-18-180000

**Overall score:** 45/100

## Top Actions

### 1. Automate Sentinel Cycle Journaling

**Kind:** refine
**Lens:** toolchain-dogfood
**Why:** Runs not being journaled — need a cortex journal entry for each sentinel run (t1.6).
**Impact:** high

### 2. Add retry logic to the Gmail poller

**Kind:** refine
**Lens:** reliability
**Why:** Transient 503s currently fail the whole poll loop.
**Impact:** medium

## Lens Evaluations

"""


def _seed_project(tmp_path: Path, *, cortex_present: bool) -> None:
    (tmp_path / ".sentinel").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".sentinel" / "scans").mkdir(parents=True, exist_ok=True)
    # backlog.md must exist for _remaining_backlog_items to proceed.
    (tmp_path / ".sentinel" / "backlog.md").write_text("# Sentinel Backlog\n")
    (tmp_path / ".sentinel" / "scans" / "2026-04-18-180000.md").write_text(
        _SCAN_WITH_TWO_REFINEMENTS,
    )
    if cortex_present:
        (tmp_path / ".cortex").mkdir()


def test_remaining_backlog_items_filters_builtin_integrations(
    tmp_path: Path,
) -> None:
    """When cortex T1.6 is active, the tautology item must be absent
    from the execution list even though the scan still contains it."""
    _seed_project(tmp_path, cortex_present=True)

    remaining = _remaining_backlog_items(tmp_path)

    titles = [item["title"] for item in remaining]
    assert "Automate Sentinel Cycle Journaling" not in titles
    assert "Add retry logic to the Gmail poller" in titles


def test_remaining_backlog_items_filters_past_rejections(
    tmp_path: Path,
) -> None:
    """An item fingerprinted into rejections.jsonl must be dropped
    from the executable list — same semantics as the backlog write."""
    _seed_project(tmp_path, cortex_present=False)

    record_rejection(
        tmp_path,
        cycle_id="2026-04-18-181350",
        work_item={
            "title": "Add retry logic to the Gmail poller",
            "lens": "reliability",
            "why": "Transient 503s currently fail the whole poll loop.",
            "impact": "medium",
            "files": [],
        },
        review_verdict="rejected",
        reviewer_reason="Out of scope for this cycle",
    )

    remaining = _remaining_backlog_items(tmp_path)
    titles = [item["title"] for item in remaining]

    assert "Add retry logic to the Gmail poller" not in titles


def test_same_cycle_rejection_does_not_shrink_live_list(
    tmp_path: Path,
) -> None:
    """Regression for codex review finding on PR #78.

    Scenario: the executor loop calls ``_remaining_backlog_items`` on
    every iteration. When item A is rejected mid-cycle, the hook
    writes to ``rejections.jsonl``; if the next iteration picks up
    that rejection, the list shrinks from ``[A, B]`` to ``[B]`` and
    ``items[items_executed]`` (where ``items_executed == 1``) now
    points to index 1 — past the end — skipping item B entirely.

    The cycle-id exclusion keeps same-cycle rejections from affecting
    the current run; they apply from the next cycle onward.
    """
    _seed_project(tmp_path, cortex_present=False)

    # Simulate item A rejected during cycle "2026-04-18-181350".
    record_rejection(
        tmp_path,
        cycle_id="2026-04-18-181350",
        work_item={
            "title": "Automate Sentinel Cycle Journaling",
            "lens": "toolchain-dogfood",
            "why": "Runs not being journaled — need a cortex journal entry"
                   " for each sentinel run (t1.6).",
            "impact": "high",
            "files": [],
        },
        review_verdict="rejected",
        reviewer_reason="tautology loop",
    )

    # Same cycle id — filter must not drop the item.
    with_same_cycle = _remaining_backlog_items(
        tmp_path, current_cycle_id="2026-04-18-181350",
    )
    titles_same = [i["title"] for i in with_same_cycle]
    assert "Automate Sentinel Cycle Journaling" in titles_same

    # Different cycle id — filter drops the item.
    with_next_cycle = _remaining_backlog_items(
        tmp_path, current_cycle_id="2026-04-18-200000",
    )
    titles_next = [i["title"] for i in with_next_cycle]
    assert "Automate Sentinel Cycle Journaling" not in titles_next


def test_remaining_backlog_items_preserves_unfiltered(tmp_path: Path) -> None:
    """When nothing matches the filters, every refinement survives."""
    _seed_project(tmp_path, cortex_present=False)

    remaining = _remaining_backlog_items(tmp_path)
    titles = [item["title"] for item in remaining]

    # cortex not present → registry doesn't filter the T1.6 item
    assert "Automate Sentinel Cycle Journaling" in titles
    assert "Add retry logic to the Gmail poller" in titles


_APPROVED_PROPOSAL_BODY = """\
# Proposal: Implement core gws CLI wrapper for Gmail

**Status:** approved

> Change **Status** to `approved` for sentinel to execute this in the next cycle.

**Lens:** integration
**Impact:** high
**Source scan:** `2026-04-18-180000.md`

## Why

Need a Swift wrapper around the gws CLI to do all Gmail I/O.

## Files likely to be touched

- Sources/AutumnMail/GmailClient.swift

## Notes

*Add your thoughts here.*
"""


def test_approved_proposals_run_before_refinements(tmp_path: Path) -> None:
    """Finding F3 regression test: ``Status: approved`` must jump the queue.

    Reproduces autumn-mail dogfood cycle 4: an approved expansion
    proposal sat in ``.sentinel/proposals/`` while two refinements were
    regenerated by the latest scan. Auto-mode previously ran the
    refinements first, silently bypassing the user-approved signal.
    The fix returns approved expansions before refinements.
    """
    _seed_project(tmp_path, cortex_present=False)

    # Drop an approved proposal alongside the two refinements seeded
    # by ``_seed_project``.
    proposals_dir = tmp_path / ".sentinel" / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    (proposals_dir / "2026-04-18-implement-core-gws-cli-wrapper-for-gmail.md").write_text(
        _APPROVED_PROPOSAL_BODY,
    )

    remaining = _remaining_backlog_items(tmp_path)
    titles = [item["title"] for item in remaining]

    # Order matters: approved proposal first, then the two refinements.
    assert titles == [
        "Implement core gws CLI wrapper for Gmail",
        "Automate Sentinel Cycle Journaling",
        "Add retry logic to the Gmail poller",
    ], (
        f"Expected approved expansion to lead the queue, got: {titles}"
    )
