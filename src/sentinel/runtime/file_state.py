"""Per-task file-state isolation tracker.

Detects when one subagent's writes invalidate another subagent's prior
reads within the same cycle.  Borrowed conceptually from Hermes Agent's
sibling-write detection pattern.

Phase 1: conflict detection + structured logging only.  Auto-recovery
(retry, escalate, halt) is a Phase 2 concern.

Usage pattern:
    task_id = generate_task_id()
    tracker.snapshot_reads(task_id, paths)   # before subagent runs
    # ... subagent runs and writes files ...
    tracker.record_writes(task_id, written_paths)
    conflicts = tracker.detect_conflicts(task_id)
    for c in conflicts:
        write_conflict_entry(c, dest_dir)
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
from collections.abc import Iterable  # noqa: TC003 — used at runtime in function signatures
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — used at runtime via path.resolve(), path.stat()
from uuid import uuid4

logger = logging.getLogger(__name__)

_HASH_WINDOW = 4096  # bytes — first 4 KB only; cheap, catches typical edits


def generate_task_id() -> str:
    """Return an 8-hex-char task identifier (UUIDv4-derived)."""
    return uuid4().hex[:8]


def _snapshot_file(path: Path) -> FileSnapshot | None:
    """Snapshot a file's mtime_ns, size, and first-4 KB SHA-256.

    Returns None when the file cannot be read (missing, permission
    error, directory) — callers treat an unsnapshotable path as
    unchecked rather than conflicted to avoid false positives on
    generated files or race conditions.
    """
    try:
        stat = path.stat()
        with path.open("rb") as f:
            chunk = f.read(_HASH_WINDOW)
        digest = hashlib.sha256(chunk).hexdigest()[:16]
        return FileSnapshot(
            path=path,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            sha256_first_4k=digest,
        )
    except OSError:
        return None


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    mtime_ns: int
    size: int
    sha256_first_4k: str  # first 4 KB hash — cheap, sufficient for typical edits


@dataclass(frozen=True)
class Conflict:
    path: Path
    reader_task_id: str
    writer_task_id: str
    reader_snapshot: FileSnapshot
    current_state: FileSnapshot


class FileStateTracker:
    """Tracks file reads/writes per task_id within a cycle.

    Conflict condition: task A snapshotted path P before running;
    task B (different task_id) subsequently wrote to P; the current
    hash of P differs from A's snapshot.  A mtime-only change (touch)
    with identical content is NOT flagged.
    """

    def __init__(self) -> None:
        # {task_id: {resolved_path: FileSnapshot}} — state at time of read
        self._reads: dict[str, dict[Path, FileSnapshot]] = {}
        # {task_id: set[resolved_path]} — paths written by each task
        self._writes: dict[str, set[Path]] = {}

    def snapshot_reads(self, task_id: str, paths: Iterable[Path]) -> None:
        """Snapshot the current state of `paths` for `task_id`.

        Call immediately before the subagent runs.  Paths that cannot
        be read are silently skipped — a missing file should not block
        the coder cycle.
        """
        snapshots: dict[Path, FileSnapshot] = {}
        for path in paths:
            resolved = path.resolve()
            snap = _snapshot_file(resolved)
            if snap is not None:
                snapshots[resolved] = snap
        self._reads.setdefault(task_id, {}).update(snapshots)

    def record_writes(self, task_id: str, paths: Iterable[Path]) -> None:
        """Record that `task_id` wrote to `paths`."""
        resolved_set = {p.resolve() for p in paths}
        self._writes.setdefault(task_id, set()).update(resolved_set)

    def detect_conflicts(self, task_id: str) -> list[Conflict]:
        """Return conflicts for `task_id`.

        A conflict exists when:
        1. `task_id` has a snapshot for path P.
        2. A *different* task recorded a write to P.
        3. The current first-4 KB hash of P differs from the snapshot.

        mtime-only changes (touch without content modification) are not
        flagged — we compare hashes, not timestamps.
        """
        read_snapshots = self._reads.get(task_id, {})
        if not read_snapshots:
            return []

        # Build a map of path → first sibling writer_task_id
        sibling_written: dict[Path, str] = {}
        for other_id, written_paths in self._writes.items():
            if other_id == task_id:
                continue
            for path in written_paths:
                if path not in sibling_written:
                    sibling_written[path] = other_id

        conflicts: list[Conflict] = []
        for path, snap in read_snapshots.items():
            writer_task_id = sibling_written.get(path)
            if writer_task_id is None:
                continue
            current = _snapshot_file(path)
            if current is None:
                # File deleted after our snapshot — log but don't raise;
                # absence doesn't produce a Conflict (no current_state).
                logger.warning(
                    "file-state: %s was read by %s but deleted by %s",
                    path,
                    task_id,
                    writer_task_id,
                )
                continue
            # Compare by content hash, not mtime — a touch must not flag.
            if current.sha256_first_4k != snap.sha256_first_4k:
                conflicts.append(
                    Conflict(
                        path=path,
                        reader_task_id=task_id,
                        writer_task_id=writer_task_id,
                        reader_snapshot=snap,
                        current_state=current,
                    )
                )
        return conflicts

    def reset(self) -> None:
        """Clear all task state.  Call between cycles."""
        self._reads.clear()
        self._writes.clear()


def write_conflict_entry(conflict: Conflict, dest_dir: Path) -> Path | None:
    """Write a structured Markdown conflict entry to `dest_dir`.

    Returns the path of the written file, or None if the write failed.
    Failure is logged as a warning and does not propagate — a conflict-
    write failure must not abort the coder cycle.
    """
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
        filename = f"{ts}-conflict-{conflict.reader_task_id[:4]}-{conflict.writer_task_id[:4]}.md"
        entry_path = dest_dir / filename

        lines = [
            f"# File-state conflict: {conflict.path.name}",
            "",
            "<!-- type: incident  file_state_conflict: true -->",
            "",
            f"- **path:** `{conflict.path}`",
            f"- **reader_task_id:** `{conflict.reader_task_id}`",
            f"- **writer_task_id:** `{conflict.writer_task_id}`",
            f"- **reader_snapshot:** hash `{conflict.reader_snapshot.sha256_first_4k}`"
            f"  size {conflict.reader_snapshot.size}",
            f"- **current_state:** hash `{conflict.current_state.sha256_first_4k}`"
            f"  size {conflict.current_state.size}",
            "",
            "## Summary",
            "",
            f"Task `{conflict.reader_task_id}` read `{conflict.path}` "
            f"(snapshot hash `{conflict.reader_snapshot.sha256_first_4k}`) "
            f"but task `{conflict.writer_task_id}` subsequently wrote to it, "
            f"leaving current state hash `{conflict.current_state.sha256_first_4k}`.",
            "",
            "The reader's reasoning may be stale.  Phase 2 will add auto-recovery;",
            "this entry is Phase 1 visibility only.",
            "",
        ]
        entry_path.write_text("\n".join(lines), encoding="utf-8")
        return entry_path
    except OSError as exc:
        logger.warning("file-state: failed to write conflict entry: %s", exc)
        return None
