"""Tests for rejection memory — the planner's cross-cycle veto cache.

Finding C4 on autumn-mail (2026-04-18): Sentinel had no memory of past
reviewer rejections, so the next scan regenerated them and the reviewer
had to re-reject them. This module closes the loop.

Invariants the tests enforce:

1. A rejected item writes a record to ``.sentinel/state/rejections.jsonl``.
2. A subsequent plan invocation filters the matching item via the
   fingerprint index.
3. TTL expiry is honored — rejections older than the window are ignored
   so veto debt doesn't accumulate forever.
4. A DIFFERENT proposal with no matching fingerprint is untouched
   (no spurious filtering).
5. Atomic-write invariant: a torn write can't poison the index — tested
   by injecting a malformed tail line and showing the good line still
   matches.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sentinel.integrations.rejections import (
    compute_fingerprint,
    filter_rejected,
    load_index,
    record_rejection,
)

if TYPE_CHECKING:
    from pathlib import Path


def _work_item(
    *,
    title: str = "Automate Sentinel Cycle Journaling",
    lens: str = "toolchain-dogfood",
    why: str = "cortex journal entry for each sentinel run",
    files: list[dict] | list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "lens": lens,
        "why": why,
        "impact": "high",
        "files": files or [],
    }


# ---------------------------------------------------------------------------
# Record + filter round-trip.
# ---------------------------------------------------------------------------


class TestPersistenceAndFilter:
    def test_rejection_persists_to_disk(self, tmp_path: Path) -> None:
        record_rejection(
            tmp_path,
            cycle_id="2026-04-18-181350",
            work_item=_work_item(),
            review_verdict="changes_requested",
            reviewer_reason=(
                "This PR only adds a manual Touchstone action. Nothing in "
                "the existing Sentinel code changes, so the integration "
                "is still missing."
            ),
        )

        log_file = tmp_path / ".sentinel" / "state" / "rejections.jsonl"
        assert log_file.exists()
        lines = [
            line for line in log_file.read_text().splitlines() if line.strip()
        ]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["cycle_id"] == "2026-04-18-181350"
        assert payload["review_verdict"] == "changes_requested"
        assert payload["rejection_fingerprint"]

    def test_filter_drops_matching_proposal(self, tmp_path: Path) -> None:
        record_rejection(
            tmp_path,
            cycle_id="2026-04-18-181350",
            work_item=_work_item(),
            review_verdict="changes_requested",
            reviewer_reason="tautology loop",
        )

        outcome = filter_rejected([_work_item()], tmp_path)

        assert outcome.kept == []
        assert len(outcome.skipped) == 1
        _, match = outcome.skipped[0]
        assert match.record.cycle_id == "2026-04-18-181350"
        assert "tautology" in match.record.reviewer_reason

    def test_filter_passes_through_when_no_log(self, tmp_path: Path) -> None:
        outcome = filter_rejected([_work_item()], tmp_path)
        assert outcome.kept == [_work_item()]
        assert outcome.skipped == []


# ---------------------------------------------------------------------------
# Negative test — unrelated proposal untouched.
# ---------------------------------------------------------------------------


class TestNegativeMatching:
    def test_different_proposal_not_filtered(self, tmp_path: Path) -> None:
        record_rejection(
            tmp_path,
            cycle_id="2026-04-18-181350",
            work_item=_work_item(),  # the rejected item
            review_verdict="rejected",
            reviewer_reason="already shipped",
        )

        unrelated = _work_item(
            title="Add retry logic to Gmail poller",
            lens="reliability",
            why="Transient 503s currently fail the whole poll.",
            files=[{"path": "gws/poller.swift", "rationale": "poll loop"}],
        )

        outcome = filter_rejected([unrelated], tmp_path)
        assert outcome.kept == [unrelated]
        assert outcome.skipped == []

    def test_same_title_different_lens_fingerprints_differently(
        self, tmp_path: Path,
    ) -> None:
        """Two items with identical titles but different lenses are
        different proposals — rejecting one must not silently veto the
        other. The fingerprint is title + lens + content."""
        fp_a = compute_fingerprint(_work_item(lens="toolchain-dogfood"))
        fp_b = compute_fingerprint(_work_item(lens="architecture"))
        assert fp_a != fp_b


# ---------------------------------------------------------------------------
# TTL expiry.
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    def test_expired_rejection_does_not_filter(self, tmp_path: Path) -> None:
        # Write a rejection dated 31 days ago by hand-crafting the
        # JSONL — the public ``record_rejection`` stamps ``now``, which
        # is what we want in prod but makes TTL tests untestable
        # without a clock injection or direct writes.
        log_file = tmp_path / ".sentinel" / "state" / "rejections.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        old_ts = (
            datetime.now(UTC) - timedelta(days=31)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        fp = compute_fingerprint(_work_item())
        payload = {
            "rejected_at": old_ts,
            "cycle_id": "2026-03-17-181350",
            "work_item": _work_item(),
            "review_verdict": "changes_requested",
            "reviewer_reason": "old rejection",
            "rejection_fingerprint": fp,
        }
        log_file.write_text(json.dumps(payload) + "\n")

        outcome = filter_rejected([_work_item()], tmp_path)
        # Expired — planner reconsiders the item.
        assert outcome.kept == [_work_item()]
        assert outcome.skipped == []

    def test_within_ttl_window_filters(self, tmp_path: Path) -> None:
        # Freshly recorded — well inside the 30-day window.
        record_rejection(
            tmp_path,
            cycle_id="2026-04-18-181350",
            work_item=_work_item(),
            review_verdict="changes_requested",
            reviewer_reason="still relevant",
        )
        outcome = filter_rejected([_work_item()], tmp_path)
        assert outcome.kept == []
        assert len(outcome.skipped) == 1

    def test_custom_ttl_respected(self, tmp_path: Path) -> None:
        record_rejection(
            tmp_path,
            cycle_id="2026-04-18-181350",
            work_item=_work_item(),
            review_verdict="changes_requested",
            reviewer_reason="r",
        )
        # TTL of 0 days means: treat nothing as still-active.
        future = datetime.now(UTC) + timedelta(seconds=1)
        outcome = filter_rejected(
            [_work_item()], tmp_path, now=future, ttl_days=0,
        )
        assert outcome.kept == [_work_item()]
        assert outcome.skipped == []


# ---------------------------------------------------------------------------
# Resilience — malformed / partial state.
# ---------------------------------------------------------------------------


class TestResilience:
    def test_malformed_line_does_not_poison_index(self, tmp_path: Path) -> None:
        # One good rejection, then a corrupted trailing line (what a
        # torn write would leave behind).
        record_rejection(
            tmp_path,
            cycle_id="2026-04-18-181350",
            work_item=_work_item(),
            review_verdict="changes_requested",
            reviewer_reason="good line",
        )
        log_file = tmp_path / ".sentinel" / "state" / "rejections.jsonl"
        # Simulate a torn append — junk on a new line.
        with log_file.open("a", encoding="utf-8") as f:
            f.write("{not-json, this is garbage\n")

        index = load_index(tmp_path)
        # The good line still indexes.
        assert len(index.by_fingerprint) == 1

        # And the filter still catches the item.
        outcome = filter_rejected([_work_item()], tmp_path)
        assert outcome.kept == []
        assert len(outcome.skipped) == 1

    def test_most_recent_record_wins_on_duplicate_fingerprint(
        self, tmp_path: Path,
    ) -> None:
        """If the same item is rejected in two cycles, the index should
        surface the most recent rejection so the user sees the latest
        reviewer reasoning."""
        record_rejection(
            tmp_path,
            cycle_id="2026-04-10-120000",
            work_item=_work_item(),
            review_verdict="changes_requested",
            reviewer_reason="first rejection",
        )
        record_rejection(
            tmp_path,
            cycle_id="2026-04-18-181350",
            work_item=_work_item(),
            review_verdict="rejected",
            reviewer_reason="still wrong",
        )

        index = load_index(tmp_path)
        assert len(index.by_fingerprint) == 1
        record = next(iter(index.by_fingerprint.values()))
        # Both lines are on disk (append-only); lookup returns the
        # newer one.
        assert record.cycle_id == "2026-04-18-181350"
        assert record.reviewer_reason == "still wrong"

        log_file = tmp_path / ".sentinel" / "state" / "rejections.jsonl"
        lines = [
            ln for ln in log_file.read_text().splitlines() if ln.strip()
        ]
        # Both records preserved on disk — append-only invariant.
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# Fingerprint stability under representation drift.
# ---------------------------------------------------------------------------


class TestFingerprintStability:
    def test_title_case_insensitive(self) -> None:
        a = compute_fingerprint(_work_item(title="Automate Sentinel Cycle Journaling"))
        b = compute_fingerprint(_work_item(title="automate sentinel cycle journaling"))
        assert a == b

    def test_file_list_order_doesnt_matter(self) -> None:
        a = compute_fingerprint(
            _work_item(
                files=[
                    {"path": "a.py", "rationale": "first"},
                    {"path": "b.py", "rationale": "second"},
                ],
            ),
        )
        b = compute_fingerprint(
            _work_item(
                files=[
                    {"path": "b.py", "rationale": "different rationale but same path"},
                    {"path": "a.py", "rationale": "also different"},
                ],
            ),
        )
        # Rationales are ignored for fingerprinting (they're advisory
        # metadata); only paths count, and paths are sorted.
        assert a == b

    def test_legacy_list_of_strings_files_shape(self) -> None:
        """The WorkItem.files contract accepts both list[str] and
        list[dict]. Both shapes must fingerprint the same way when they
        name the same files."""
        a = compute_fingerprint(_work_item(files=["a.py", "b.py"]))
        b = compute_fingerprint(
            _work_item(files=[{"path": "a.py"}, {"path": "b.py"}]),
        )
        assert a == b
