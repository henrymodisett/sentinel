"""Loop detection for the sentinel work cycle.

Tracks a content-derived fingerprint per task so Sentinel can detect when
it's running the same work item repeatedly without making progress. A
repeated fingerprint indicates the planner is regenerating the same item
(because the coder keeps failing or the reviewer keeps rejecting) — the
right response is to halt with a clear signal rather than burning budget.

Design:
- Fingerprint: stable SHA-256 prefix of (title + sorted file paths). Stable
  across runs because it's derived from plan content, not timestamps.
- Ring buffer: last N fingerprints stored in `.sentinel/state/loop-guard.json`.
  File is small (N × ~60 bytes) and rewritten atomically on every update.
- Halt condition: same fingerprint appears >= M times in the buffer.
- Unblock: human deletes `.sentinel/state/loop-guard.json` or marks the
  item done externally. No automatic reset — the loop guard only clears on
  human action so Sentinel can't silently retry itself into oblivion.

Defaults: N=5 (ring buffer size), M=3 (allowed occurrences before halt).
These are conservative — they catch tight loops quickly while allowing
some legitimate retry (e.g., a flaky reviewer that blocks then approves).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_RING_SIZE_DEFAULT = 5
_MAX_OCCURRENCES_DEFAULT = 3


def _guard_file(project_path: Path) -> Path:
    state = project_path / ".sentinel" / "state"
    state.mkdir(parents=True, exist_ok=True)
    return state / "loop-guard.json"


def _load_ring(project_path: Path) -> list[dict]:
    path = _guard_file(project_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("loop-guard: could not read %s: %s", path, e)
        return []


def _save_ring(project_path: Path, ring: list[dict]) -> None:
    path = _guard_file(project_path)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(ring, indent=2))
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("loop-guard: could not write %s: %s", path, e)


def _file_path(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("path", ""))
    return str(value)


def fingerprint(title: str, files: list[object]) -> str:
    """Stable SHA-256 prefix for a task identity (title + sorted file paths)."""
    paths = sorted(p for p in (_file_path(f).strip() for f in files) if p)
    key = title.strip() + "\n" + "\n".join(paths)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class LoopGuardResult:
    looping: bool
    fingerprint: str
    occurrences: int
    max_occurrences: int
    reason: str = ""


def check_and_record(
    project_path: Path,
    title: str,
    files: list[object],
    *,
    ring_size: int = _RING_SIZE_DEFAULT,
    max_occurrences: int = _MAX_OCCURRENCES_DEFAULT,
) -> LoopGuardResult:
    """Check for a loop and record this cycle's fingerprint.

    Called before each work item is executed. The check happens BEFORE
    recording so that the halt fires on the M+1 occurrence: M recent
    attempts are allowed, but the next repeated attempt is blocked.

    Returns a LoopGuardResult with `looping=True` when the same fingerprint
    appears >= max_occurrences times in the last ring_size entries. When
    looping, the fingerprint is NOT recorded (no point appending to a
    full loop — the human needs to clear the state first).

    On any filesystem error the check is silently bypassed (looping=False)
    so a broken state dir can't permanently block work.
    """
    fp = fingerprint(title, files)
    ring = _load_ring(project_path)

    # Count occurrences in the existing ring (before this cycle's entry)
    occurrences = sum(1 for entry in ring if entry.get("fingerprint") == fp)

    if occurrences >= max_occurrences:
        return LoopGuardResult(
            looping=True,
            fingerprint=fp,
            occurrences=occurrences,
            max_occurrences=max_occurrences,
            reason=(
                f"work item appeared {occurrences} times in the last "
                f"{ring_size} cycles — Sentinel is looping. "
                f"Delete .sentinel/state/loop-guard.json to unblock."
            ),
        )

    # Record this cycle's fingerprint and prune to ring_size
    ring.append({
        "fingerprint": fp,
        "title": title[:80],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    })
    ring = ring[-ring_size:]
    _save_ring(project_path, ring)

    return LoopGuardResult(
        looping=False,
        fingerprint=fp,
        occurrences=occurrences + 1,
        max_occurrences=max_occurrences,
    )


def clear(project_path: Path) -> bool:
    """Delete the loop-guard ring buffer. Returns True if the file existed."""
    path = _guard_file(project_path)
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.warning("loop-guard: could not clear %s: %s", path, e)
    return False
