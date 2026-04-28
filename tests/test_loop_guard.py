"""Tests for loop detection (sentinel.loop_guard).

The loop guard tracks a content-derived fingerprint per work item and halts
when the same fingerprint appears more than M times in the last N cycles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sentinel.loop_guard import (
    check_and_record,
    clear,
    fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestFingerprint:
    def test_same_input_same_hash(self) -> None:
        fp1 = fingerprint("fix auth bug", ["src/auth.py", "tests/test_auth.py"])
        fp2 = fingerprint("fix auth bug", ["src/auth.py", "tests/test_auth.py"])
        assert fp1 == fp2

    def test_different_title_different_hash(self) -> None:
        fp1 = fingerprint("fix auth bug", [])
        fp2 = fingerprint("fix login bug", [])
        assert fp1 != fp2

    def test_file_order_independent(self) -> None:
        """Fingerprint is stable regardless of file list ordering."""
        fp1 = fingerprint("task", ["a.py", "b.py"])
        fp2 = fingerprint("task", ["b.py", "a.py"])
        assert fp1 == fp2

    def test_no_files_ok(self) -> None:
        fp = fingerprint("task with no files", [])
        assert isinstance(fp, str)
        assert len(fp) == 16  # 16-char hex prefix


class TestCheckAndRecord:
    def test_first_occurrence_not_looping(self, tmp_path: Path) -> None:
        result = check_and_record(tmp_path, title="fix bug", files=["f.py"])
        assert not result.looping
        assert result.occurrences == 1

    def test_second_occurrence_not_looping(self, tmp_path: Path) -> None:
        check_and_record(tmp_path, title="fix bug", files=["f.py"])
        result = check_and_record(tmp_path, title="fix bug", files=["f.py"])
        assert not result.looping
        assert result.occurrences == 2

    def test_third_occurrence_does_not_halt(self, tmp_path: Path) -> None:
        """Default max_occurrences=3: 3rd call is still allowed."""
        check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        result = check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        assert not result.looping
        assert result.occurrences == 3

    def test_fourth_occurrence_halts(self, tmp_path: Path) -> None:
        """Default max_occurrences=3: 4th call triggers loop detection."""
        for _ in range(3):
            check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        result = check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        assert result.looping
        assert result.occurrences >= 3
        assert "looping" in result.reason.lower()

    def test_different_items_dont_interfere(self, tmp_path: Path) -> None:
        """Loop detection is per-item fingerprint — different items don't count
        toward each other's threshold."""
        check_and_record(tmp_path, title="item A", files=[], max_occurrences=3)
        check_and_record(tmp_path, title="item A", files=[], max_occurrences=3)
        # item A appeared twice — item B's first appearance should be fine
        result = check_and_record(tmp_path, title="item B", files=[], max_occurrences=3)
        assert not result.looping

    def test_ring_buffer_prunes_old_entries(self, tmp_path: Path) -> None:
        """With ring_size=3, only the last 3 entries are kept. A fingerprint
        that appeared twice then fell off the ring should not trigger halt."""
        # Record item A twice, then fill the ring with 3 different items
        check_and_record(tmp_path, title="A", files=[], ring_size=3, max_occurrences=3)
        check_and_record(tmp_path, title="A", files=[], ring_size=3, max_occurrences=3)
        check_and_record(tmp_path, title="B", files=[], ring_size=3, max_occurrences=3)
        check_and_record(tmp_path, title="C", files=[], ring_size=3, max_occurrences=3)
        check_and_record(tmp_path, title="D", files=[], ring_size=3, max_occurrences=3)
        # A's two earlier entries have been pushed out of the ring_size=3 buffer
        result = check_and_record(
            tmp_path, title="A", files=[], ring_size=3, max_occurrences=3,
        )
        assert not result.looping

    def test_guard_file_created(self, tmp_path: Path) -> None:
        check_and_record(tmp_path, title="task", files=[])
        guard = tmp_path / ".sentinel" / "state" / "loop-guard.json"
        assert guard.exists()

    def test_looping_does_not_record_new_entry(self, tmp_path: Path) -> None:
        """When looping=True, the fingerprint is NOT appended to the buffer."""
        for _ in range(3):
            check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        # Now at threshold — triggering call should not add to the ring
        import json
        guard = tmp_path / ".sentinel" / "state" / "loop-guard.json"
        size_before = len(json.loads(guard.read_text()))
        check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        size_after = len(json.loads(guard.read_text()))
        assert size_after == size_before, (
            "A looping-detected call must not grow the ring buffer"
        )

    def test_result_has_reason_when_looping(self, tmp_path: Path) -> None:
        for _ in range(3):
            check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        result = check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        assert result.looping
        assert result.reason  # non-empty human-readable message
        assert "loop-guard.json" in result.reason  # tells user how to unblock


class TestClear:
    def test_clear_removes_file(self, tmp_path: Path) -> None:
        check_and_record(tmp_path, title="x", files=[])
        guard = tmp_path / ".sentinel" / "state" / "loop-guard.json"
        assert guard.exists()
        removed = clear(tmp_path)
        assert removed
        assert not guard.exists()

    def test_clear_returns_false_when_no_file(self, tmp_path: Path) -> None:
        removed = clear(tmp_path)
        assert not removed

    def test_clear_allows_fresh_start(self, tmp_path: Path) -> None:
        """After clear(), the loop counter resets — same item can run again."""
        for _ in range(3):
            check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        loop_result = check_and_record(
            tmp_path, title="x", files=[], max_occurrences=3,
        )
        assert loop_result.looping

        clear(tmp_path)
        # Now a fresh start — should not be looping
        fresh = check_and_record(tmp_path, title="x", files=[], max_occurrences=3)
        assert not fresh.looping
