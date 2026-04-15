"""Tests for the runs/ prune mechanism.

The cycle journal isn't shipped yet (PR B), so prune currently runs
against an empty / nonexistent runs/ dir on every real cycle. These
tests cover the contract regardless: no-op when there's nothing to
prune, removes only items older than retention, never touches anything
outside .sentinel/runs/, fails gracefully on filesystem errors.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from sentinel.prune import prune_runs


def _touch(path: Path, age_days: float) -> None:
    """Create a file (or dir) with mtime set N days in the past."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == "" and not path.exists():
        path.mkdir(parents=True)
    else:
        path.write_text("x")
    past = time.time() - age_days * 86400
    os.utime(path, (past, past))


class TestPruneRuns:
    def test_returns_zero_when_no_dot_sentinel(self, tmp_path: Path) -> None:
        assert prune_runs(tmp_path, retention_days=30) == 0

    def test_returns_zero_when_no_runs_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".sentinel").mkdir()
        assert prune_runs(tmp_path, retention_days=30) == 0

    def test_returns_zero_when_disabled(self, tmp_path: Path) -> None:
        """retention_days <= 0 disables pruning entirely."""
        runs = tmp_path / ".sentinel" / "runs"
        _touch(runs / "old.md", age_days=999)
        assert prune_runs(tmp_path, retention_days=0) == 0
        assert (runs / "old.md").exists(), "disabled prune must not touch files"

    def test_removes_older_than_retention(self, tmp_path: Path) -> None:
        runs = tmp_path / ".sentinel" / "runs"
        _touch(runs / "old.md", age_days=45)
        _touch(runs / "older.md", age_days=100)
        _touch(runs / "fresh.md", age_days=5)

        removed = prune_runs(tmp_path, retention_days=30)

        assert removed == 2
        assert not (runs / "old.md").exists()
        assert not (runs / "older.md").exists()
        assert (runs / "fresh.md").exists(), (
            "fresh file (5 days old) must survive 30-day retention"
        )

    def test_does_not_touch_artifacts_outside_runs(self, tmp_path: Path) -> None:
        """Long-lived artifacts (scans/, verifications.jsonl, etc.) live
        outside runs/ specifically so prune can never touch them."""
        runs = tmp_path / ".sentinel" / "runs"
        _touch(runs / "old.md", age_days=999)
        _touch(tmp_path / ".sentinel" / "scans" / "old-scan.md", age_days=999)
        _touch(tmp_path / ".sentinel" / "verifications.jsonl", age_days=999)
        _touch(tmp_path / ".sentinel" / "backlog.md", age_days=999)

        prune_runs(tmp_path, retention_days=30)

        # runs/ entry gone, everything else preserved
        assert not (runs / "old.md").exists()
        assert (tmp_path / ".sentinel" / "scans" / "old-scan.md").exists()
        assert (tmp_path / ".sentinel" / "verifications.jsonl").exists()
        assert (tmp_path / ".sentinel" / "backlog.md").exists()

    def test_handles_directory_entries(self, tmp_path: Path) -> None:
        """Future variants of the journal may write a per-cycle dir
        instead of a file. Prune must handle both shapes."""
        runs = tmp_path / ".sentinel" / "runs"
        old_dir = runs / "2026-01-01-1200"
        old_dir.mkdir(parents=True)
        (old_dir / "events.jsonl").write_text("{}")
        (old_dir / "manifest.md").write_text("x")
        past = time.time() - 100 * 86400
        os.utime(old_dir / "events.jsonl", (past, past))
        os.utime(old_dir / "manifest.md", (past, past))
        os.utime(old_dir, (past, past))

        removed = prune_runs(tmp_path, retention_days=30)

        assert removed == 1
        assert not old_dir.exists()

    def test_no_removal_when_everything_fresh(self, tmp_path: Path) -> None:
        runs = tmp_path / ".sentinel" / "runs"
        _touch(runs / "a.md", age_days=1)
        _touch(runs / "b.md", age_days=10)
        _touch(runs / "c.md", age_days=29)

        assert prune_runs(tmp_path, retention_days=30) == 0
        assert (runs / "a.md").exists()
        assert (runs / "b.md").exists()
        assert (runs / "c.md").exists()

    def test_runs_root_as_symlink_is_refused(self, tmp_path: Path) -> None:
        """If `.sentinel/runs/` itself is a symlink to somewhere else,
        prune must refuse to walk it. Otherwise an old external target
        tree gets recursively pruned — same outside-scope deletion class
        as the per-entry symlink case, just at the directory boundary."""
        # Outside-scope dir with files we must not touch
        outside = tmp_path / "external-runs"
        outside.mkdir()
        (outside / "important.md").write_text("must not be deleted")
        past = time.time() - 100 * 86400
        os.utime(outside / "important.md", (past, past))

        # .sentinel/runs/ is a symlink to that dir
        sentinel_dir = tmp_path / ".sentinel"
        sentinel_dir.mkdir()
        (sentinel_dir / "runs").symlink_to(outside)

        removed = prune_runs(tmp_path, retention_days=30)

        assert removed == 0, (
            "prune must skip a symlinked runs/ root entirely"
        )
        assert (outside / "important.md").exists(), (
            "files inside the symlink target must NOT be touched"
        )
        # The symlink itself stays — we don't replace user choices
        assert (sentinel_dir / "runs").is_symlink()

    def test_symlinked_entry_is_unlinked_not_followed(
        self, tmp_path: Path,
    ) -> None:
        """If runs/ contains a symlink (top-level or nested) to data
        outside the prune scope, prune must remove the symlink itself
        and never touch the target. A bug here would let prune destroy
        arbitrary files anywhere a symlink points — catastrophic."""
        runs = tmp_path / ".sentinel" / "runs"
        runs.mkdir(parents=True)

        # Outside-prune-scope target the symlink will point to
        outside = tmp_path / "important"
        outside.mkdir()
        precious = outside / "precious.txt"
        precious.write_text("must not be deleted")

        # Top-level symlink in runs/ pointing at the outside dir
        link = runs / "old-link"
        link.symlink_to(outside)
        # Age the symlink so it's older than retention
        past = time.time() - 100 * 86400
        os.utime(link, (past, past), follow_symlinks=False)

        prune_runs(tmp_path, retention_days=30)

        assert not link.exists(), "symlink itself should be removed"
        assert outside.exists(), (
            "symlink target directory must NOT be deleted by prune"
        )
        assert precious.exists(), (
            "files inside the symlink target must NOT be touched by prune"
        )
        assert precious.read_text() == "must not be deleted"

    def test_nested_symlink_inside_real_dir_not_followed(
        self, tmp_path: Path,
    ) -> None:
        """Even when prune recurses into a real expired directory, any
        symlink it encounters during the walk must be unlinked, not
        traversed. Defense in depth at every level of the recursion."""
        runs = tmp_path / ".sentinel" / "runs"
        old_dir = runs / "2026-01-01-1200"
        old_dir.mkdir(parents=True)
        (old_dir / "events.jsonl").write_text("{}")

        # Nested symlink inside the to-be-pruned directory
        outside = tmp_path / "outside-data"
        outside.mkdir()
        (outside / "secret.txt").write_text("untouchable")
        nested_link = old_dir / "linked-data"
        nested_link.symlink_to(outside)

        # Age every entry past retention
        past = time.time() - 100 * 86400
        for p in [old_dir, old_dir / "events.jsonl"]:
            os.utime(p, (past, past))
        os.utime(nested_link, (past, past), follow_symlinks=False)

        prune_runs(tmp_path, retention_days=30)

        assert not old_dir.exists(), "expired dir should be gone"
        assert outside.exists(), "symlink target must survive"
        assert (outside / "secret.txt").exists(), (
            "files inside the symlink target must NOT be touched"
        )

    def test_disappearing_entry_does_not_crash(self, tmp_path: Path) -> None:
        """A file that vanishes between iterdir and stat (concurrent
        prune from another process, external deletion) must not crash."""
        runs = tmp_path / ".sentinel" / "runs"
        _touch(runs / "ghost.md", age_days=999)

        # Race: delete the file before prune sees it
        original_iterdir = Path.iterdir

        def racy_iterdir(self):  # noqa: ANN001, ANN202
            for entry in original_iterdir(self):
                if entry.name == "ghost.md":
                    entry.unlink()  # vanish between iterdir and stat
                yield entry

        Path.iterdir = racy_iterdir
        try:
            # Should not raise, just skip the missing file
            removed = prune_runs(tmp_path, retention_days=30)
        finally:
            Path.iterdir = original_iterdir

        # We don't assert the count — what matters is that no exception
        # propagated. The ghost was already gone by the time stat ran.
        assert isinstance(removed, int)
