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


def test_remaining_backlog_items_preserves_unfiltered(tmp_path: Path) -> None:
    """When nothing matches the filters, every refinement survives."""
    _seed_project(tmp_path, cortex_present=False)

    remaining = _remaining_backlog_items(tmp_path)
    titles = [item["title"] for item in remaining]

    # cortex not present → registry doesn't filter the T1.6 item
    assert "Automate Sentinel Cycle Journaling" in titles
    assert "Add retry logic to the Gmail poller" in titles
