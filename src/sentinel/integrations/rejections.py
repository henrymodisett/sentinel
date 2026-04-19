"""Rejection memory — persist reviewer rejections so the planner stops
regenerating items a human already rejected.

Problem (autumn-garage journal 2026-04-18 finding C4): ``sentinel work``
does scan → plan → execute in one atom. Rejected work items vanish from
``.sentinel/backlog.md`` but the next cycle re-runs the scan, the
planner regenerates the same item, and the reviewer has to reject it
again. Same LLM spend, same wasted cycle, every time.

Fix: an append-only log at ``.sentinel/state/rejections.jsonl``. One
JSON object per rejection, written atomically (temp + rename) at the
moment the reviewer rejects. The planner consults this log before
writing a refinement to the backlog; any proposal whose fingerprint
matches a rejection from the last ``_TTL_DAYS`` days is dropped with
an audit comment.

Escape hatches:
  - ``--force-retry`` (future CLI flag) — not implemented here; the
    escape hatch today is ``rm .sentinel/state/rejections.jsonl``, or
    deleting the one line that blocks the item. The file is plain
    JSONL on purpose so this is one ``sed -i '/pattern/d'`` away.
  - TTL window — 30 days. An item rejected 31 days ago will be
    reconsidered. This is deliberate: a proposal that was wrong a
    month ago may be right now (the codebase changed, the priority
    changed). Without a TTL we'd accumulate veto debt forever.

Non-goals:
  - No deduplication. Two rejections of the same item produce two log
    lines; the matcher OR-s across all unexpired lines.
  - No schema versioning for now. If the record shape changes, we
    either keep backward compat or bump a schema version field. Until
    then, unknown fields are ignored on read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 — runtime use in signatures

logger = logging.getLogger(__name__)


# How long a rejection stays "sticky." 30 days matches the default
# run-journal retention — rejections and the journals that motivated
# them age out together, so operators don't have a rejection line
# referring to a journal they can't read anymore.
_TTL_DAYS = 30

# Path to the rejection log, relative to project root. Under
# ``.sentinel/state/`` so it sits next to the cortex-write-errors log
# and the other append-only "why did sentinel do that" diagnostics.
_REJECTIONS_RELPATH = Path(".sentinel/state/rejections.jsonl")


@dataclass(frozen=True)
class RejectionRecord:
    """One persisted rejection.

    Minimum fields required to (a) re-identify a matching proposal on
    the next cycle, (b) explain to the user what was rejected, and (c)
    let the user audit or purge specific entries by reading the file.
    """

    rejected_at: str
    """ISO-8601 UTC timestamp. String form so the file is legible
    without tooling."""

    cycle_id: str
    """The cycle-id that produced the rejection. Joins to the
    ``.sentinel/runs/<cycle_id>.md`` journal and (if cortex is enabled)
    to ``.cortex/journal/<date>-sentinel-cycle-<cycle_id>.md``."""

    work_item: dict
    """The rejected work item. Stored verbatim so a future planner can
    compute new fingerprints without us pre-committing to a hashing
    scheme that's impossible to change later."""

    review_verdict: str
    """The reviewer's verdict string (``"changes_requested"`` or
    ``"rejected"``). Kept so the planner can distinguish "bad idea" from
    "fixable-but-the-coder-failed" if it wants to in the future."""

    reviewer_reason: str
    """Human-readable summary from the reviewer. Truncated by the
    writer, not here — we trust whatever ``record_rejection`` hands us."""

    rejection_fingerprint: str
    """sha256 of title + lens + content-hash. Stable identifier the
    planner compares against on subsequent cycles."""


# ---------- Fingerprinting ----------


def _content_hash(work_item: dict) -> str:
    """Hash the semantic content of a work item.

    Tolerant to field ordering and to the dual list[str] / list[dict]
    shape for ``files``. Excludes id/timestamp/index because those are
    cycle-scoped noise — the same *idea* generated on two different
    cycles must hash identically.
    """
    parts: list[str] = []
    for key in ("why", "impact", "lens"):
        value = work_item.get(key)
        if isinstance(value, str):
            parts.append(f"{key}:{value.strip()}")

    file_tokens: list[str] = []
    for f in work_item.get("files", []) or []:
        if isinstance(f, dict):
            path = str(f.get("path", "")).strip()
            if path:
                file_tokens.append(path)
        elif isinstance(f, str):
            stripped = f.strip()
            if stripped:
                file_tokens.append(stripped)
    # Sort so reorderings of the file list still fingerprint-match.
    file_tokens.sort()
    parts.append("files:" + "|".join(file_tokens))

    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def compute_fingerprint(work_item: dict) -> str:
    """Public fingerprint: sha256(title || lens || content_hash).

    Title is normalized (lowercased, whitespace collapsed) so "Automate
    Sentinel Cycle Journaling" and "automate sentinel cycle journaling"
    fingerprint to the same string.
    """
    title = str(work_item.get("title", "")).lower()
    title = " ".join(title.split())
    lens = str(work_item.get("lens", "")).lower().strip()
    content = _content_hash(work_item)
    raw = f"title:{title}\nlens:{lens}\ncontent:{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- I/O ----------


def _log_path(project_dir: Path) -> Path:
    return project_dir / _REJECTIONS_RELPATH


def _atomic_append(path: Path, line: str) -> None:
    """Append one line atomically.

    ``append + rename`` is not a thing for a single POSIX fs op, but we
    can achieve durability by: read existing content (if any), write
    content + new line to a temp file in the same dir, rename over.
    Costs one extra read per rejection; rejections are rare and the log
    is short, so the cost is negligible vs. the consistency guarantee.

    Why atomic matters: ``rejections.jsonl`` is consulted at cycle
    start. A torn write (process killed mid-append) on a naive
    ``open(..., "a")`` path would leave half a JSON object on disk, the
    JSONL parser would choke on it, and either every subsequent planner
    call silently loses the log (if we swallow) or crashes (if we
    don't). Temp + rename sidesteps the whole class.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"

    # Use NamedTemporaryFile in the target dir so rename is on the same
    # filesystem (required for atomicity on POSIX).
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=".rejections-",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(existing)
        tmp.write(line)
        if not line.endswith("\n"):
            tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)

    os.replace(tmp_path, path)


def record_rejection(
    project_dir: Path,
    *,
    cycle_id: str,
    work_item: dict,
    review_verdict: str,
    reviewer_reason: str,
) -> RejectionRecord:
    """Persist one rejection. Returns the record written.

    Failure mode is loud-but-nonblocking: we try to write; if the OS
    refuses (ro-fs, permissions), we log + warn + continue. Missing a
    rejection log line is not worse than the pre-fix world — it's the
    same failure mode we had before this feature.
    """
    record = RejectionRecord(
        rejected_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        cycle_id=cycle_id,
        work_item=work_item,
        review_verdict=review_verdict,
        reviewer_reason=reviewer_reason,
        rejection_fingerprint=compute_fingerprint(work_item),
    )
    line = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
    path = _log_path(project_dir)
    try:
        _atomic_append(path, line)
    except OSError as exc:
        # Surface the failure — principle: no silent failures. We
        # explicitly do NOT re-raise, because the review already
        # happened and the work-item outcome must not be contingent on
        # the memory write succeeding. But the operator needs to know.
        logger.warning(
            "Could not persist rejection to %s: %s. "
            "The planner will not filter this item next cycle.",
            path, exc,
        )
    return record


@dataclass
class RejectionIndex:
    """In-memory view of the rejections log for fast fingerprint lookup.

    ``by_fingerprint`` maps fingerprint → most recent matching record.
    Older duplicates are discarded for the lookup path but remain on
    disk (append-only invariant).
    """

    by_fingerprint: dict[str, RejectionRecord] = field(default_factory=dict)

    def matches(self, work_item: dict) -> RejectionRecord | None:
        """Return the most recent unexpired rejection matching this item."""
        fp = compute_fingerprint(work_item)
        return self.by_fingerprint.get(fp)


def _parse_iso_z(value: str) -> datetime | None:
    """Parse an ISO-8601 ``Z``-suffixed UTC timestamp defensively.

    Accepts a few common variants; returns None on any failure so a
    single corrupt line doesn't blind the index.
    """
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def load_index(
    project_dir: Path,
    *,
    now: datetime | None = None,
    ttl_days: int = _TTL_DAYS,
) -> RejectionIndex:
    """Read the rejection log, prune expired entries, return the index.

    ``now`` is injectable for tests so TTL expiry is deterministic.
    Malformed JSON lines are skipped with a warning — the file is
    append-only from our code, but a human editing it by hand may
    introduce a stray comma. Never crash the planner because of it.
    """
    path = _log_path(project_dir)
    index = RejectionIndex()
    if not path.exists():
        return index

    reference = now or datetime.now(UTC)
    cutoff = reference - timedelta(days=ttl_days)

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return index

    for lineno, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Skipping malformed rejection line %s:%d (%s)",
                path, lineno, exc,
            )
            continue

        rejected_at = _parse_iso_z(str(payload.get("rejected_at", "")))
        if rejected_at is None:
            continue
        if rejected_at < cutoff:
            # Expired — the line stays on disk (append-only invariant)
            # but the planner treats it as absent.
            continue

        fingerprint = str(payload.get("rejection_fingerprint", ""))
        if not fingerprint:
            continue

        record = RejectionRecord(
            rejected_at=str(payload.get("rejected_at", "")),
            cycle_id=str(payload.get("cycle_id", "")),
            work_item=payload.get("work_item", {}) or {},
            review_verdict=str(payload.get("review_verdict", "")),
            reviewer_reason=str(payload.get("reviewer_reason", "")),
            rejection_fingerprint=fingerprint,
        )

        # Keep the most recent per fingerprint for the lookup.
        existing = index.by_fingerprint.get(fingerprint)
        if existing is None:
            index.by_fingerprint[fingerprint] = record
        else:
            existing_at = _parse_iso_z(existing.rejected_at)
            if existing_at is None or rejected_at >= existing_at:
                index.by_fingerprint[fingerprint] = record

    return index


# ---------- Planner-facing filter ----------


@dataclass(frozen=True)
class RejectionMatch:
    """Result of matching a proposed action against the rejection log."""

    record: RejectionRecord


@dataclass
class RejectionFilterOutcome:
    kept: list[dict] = field(default_factory=list)
    skipped: list[tuple[dict, RejectionMatch]] = field(default_factory=list)


def filter_rejected(
    actions: list[dict],
    project_dir: Path,
    *,
    now: datetime | None = None,
    ttl_days: int = _TTL_DAYS,
) -> RejectionFilterOutcome:
    """Drop actions whose fingerprint matches an unexpired rejection."""
    index = load_index(project_dir, now=now, ttl_days=ttl_days)
    outcome = RejectionFilterOutcome()
    if not index.by_fingerprint:
        outcome.kept = list(actions)
        return outcome
    for action in actions:
        record = index.matches(action)
        if record is None:
            outcome.kept.append(action)
        else:
            outcome.skipped.append((action, RejectionMatch(record=record)))
    return outcome


# Clock helper kept module-private so callers can override in tests by
# passing ``now=``; we intentionally do not expose a module-level clock
# patch hook because the function signatures already carry the knob.
_ = time  # keep import for future drift-telemetry hooks


__all__ = [
    "RejectionFilterOutcome",
    "RejectionIndex",
    "RejectionMatch",
    "RejectionRecord",
    "compute_fingerprint",
    "filter_rejected",
    "load_index",
    "record_rejection",
]
