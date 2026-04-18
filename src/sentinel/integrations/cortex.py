"""Cortex T1.6 integration — write Sentinel-cycle journal entries.

Operationalizes Cortex Protocol v0.2.0 T1.6 (sentinel-cycle):
when ``sentinel work`` finishes a cycle and writes
``.sentinel/runs/<timestamp>.md``, we also append a conformant
``journal/<date>-sentinel-cycle-<cycle-id>.md`` entry to the project's
``.cortex/`` — iff ``.cortex/`` is detected at the repo root.

**File-contract composition only** (cortex Doctrine 0002 /
autumn-garage Doctrine 0001). Sentinel does not import Cortex. The
integration detects ``.cortex/`` by presence, writes a markdown file
conforming to the template shape in ``.cortex/templates/journal/
sentinel-cycle.md``, and surfaces any write failure as a warning + a
structured error-log entry — without failing the cycle.

**Why direct file write, not shell-out:** Cortex's Phase D authoring
CLI (``cortex journal append``) is not shipped yet. Waiting for it
would gate T1.6 on an independent milestone. The journal-entry format
is specified today via the bundled template, so Sentinel can produce a
conformant entry by rendering that shape. When Phase D ships, Sentinel
migrates to shelling out — the file output is identical either way, so
consumers of the journal do not notice.

**Known template-drift risk.** The cycle-template literal lives here
(``_CYCLE_TEMPLATE``); the canonical shape lives in Cortex's bundled
templates. If Cortex updates its template shape and Sentinel does not
follow, ``cortex doctor`` will flag the drift — but Sentinel will keep
producing the old shape until patched. Mitigation is the Phase D
migration noted above; until then we rely on the validator, not the
compiler, to catch drift.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — runtime use in signatures

logger = logging.getLogger(__name__)


# Bound the number of characters we include in the run-journal excerpt
# so a chatty run (thousands of provider calls) does not produce a
# multi-megabyte cortex journal entry. The full data lives in
# ``.sentinel/runs/<timestamp>.md``; the cortex entry is the summary.
_RUN_JOURNAL_EXCERPT_CHARS = 800


@dataclass(frozen=True)
class CortexPresence:
    """Snapshot of whether a project is Cortex-enabled.

    ``dir_present`` — ``.cortex/`` directory exists at ``project_dir``.
    ``journal_dir_writable`` — ``.cortex/journal/`` exists and is writable
    (or can be created). When ``dir_present`` is True but this is False,
    a write will fail at the OS layer; we surface it as a warning.
    """

    project_dir: Path
    dir_present: bool
    journal_dir_writable: bool


def detect_cortex(project_dir: Path) -> CortexPresence:
    """Detect whether the project composes with Cortex.

    Mirrors the file-contract detection pattern from ``siblings.py``:
    presence of ``.cortex/`` at the repo root is the only signal, by
    design. A project that happens to have ``.cortex/`` from an
    upstream fork will write entries it does not consume — acceptable
    per the plan's "Known limitations" (users see entries appear and
    can disable via ``--no-cortex-journal`` / config).

    ``journal_dir_writable`` best-effort probes whether the journal
    directory is usable without actually writing. A race between this
    check and the real write is handled by the write path, which treats
    any ``OSError`` as a warning, not a cycle failure.
    """
    cortex_dir = project_dir / ".cortex"
    dir_present = cortex_dir.is_dir()

    journal_dir = cortex_dir / "journal"
    if not dir_present:
        return CortexPresence(
            project_dir=project_dir,
            dir_present=False,
            journal_dir_writable=False,
        )

    # Probe writability: prefer os.access on an existing dir, else the
    # parent must be writable so we can create it. This is best-effort;
    # the authoritative answer comes from the write attempt itself.
    if journal_dir.exists():
        writable = journal_dir.is_dir() and os.access(journal_dir, os.W_OK)
    else:
        writable = os.access(cortex_dir, os.W_OK)

    return CortexPresence(
        project_dir=project_dir,
        dir_present=True,
        journal_dir_writable=writable,
    )


# ---------- Cycle-data contract ----------


@dataclass
class CortexCycleData:
    """All the fields the cortex-journal renderer needs from a cycle.

    Populated by the Sentinel hook point from the live ``Journal`` and
    the final ``ScanResult`` (when available). Kept as a plain
    dataclass — not coupled to ``sentinel.journal.Journal`` — so the
    renderer can be unit-tested without spinning up a full cycle.
    """

    cycle_id: str
    """Timestamp-based id, e.g. ``2026-04-17-1430`` — matches the
    ``.sentinel/runs/<id>.md`` filename stem. Used verbatim in the
    cortex entry's filename and title so a reader can ``grep`` across
    both stores for the same cycle."""

    started_at: float
    """Cycle start, unix seconds. Used to derive the ``<YYYY-MM-DD>``
    filename prefix per cortex's filename convention. Derived from
    cycle start (not write time) so a cycle that spans a day-boundary
    still files under its start date."""

    ended_at: float
    """Cycle end, unix seconds."""

    project_name: str
    branch: str
    exit_reason: str
    """Sentinel's own ``exit_reason`` string — ``dry_run``,
    ``backlog_empty``, ``interrupted``, ``budget: ...``, etc. Mapped to
    a cortex-friendly verdict in the rendered entry."""

    total_cost_usd: float
    total_provider_calls: int
    lens_scores: list[tuple[str, int]] = field(default_factory=list)
    """Ordered list of ``(lens_name, score)`` from the latest scan.
    Empty when no scan ran during the cycle."""
    overall_score: int | None = None
    refinement_count: int = 0
    expansion_count: int = 0
    work_item_outcomes: list[tuple[str, str, str]] = field(default_factory=list)
    """``(work_item_id, title, status)`` per executed work item."""
    pr_url: str = ""
    providers_by_role: list[tuple[str, str, str]] = field(default_factory=list)
    """Deduplicated ``(role, provider, model)`` from the run's provider
    calls."""
    run_journal_relpath: str = ""
    """``.sentinel/runs/<id>.md`` relative to project_dir. Cited
    verbatim in the ``Cites:`` field so a reader can jump to the full
    record."""


# ---------- Rendering ----------


_CYCLE_TEMPLATE = """\
# Sentinel cycle {cycle_id} — {summary}

**Date:** {date}
**Type:** sentinel-cycle
**Trigger:** T1.6
**Cites:** {cites}

> {one_line_summary}

## Cycle summary

- **Lenses:** {lenses_line}
- **Health:** {health_line}
- **Findings count:** {findings_line}
- **Verdict:** {verdict}
- **PR:** {pr_line}
- **Spend:** ${spend:.4f}
- **Duration:** {duration}
- **Providers used:** {providers_line}

## Run journal

[Full run record]({run_journal_link})

{run_journal_excerpt}

## Follow-ups

{follow_ups}
"""


def _format_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _format_verdict(
    exit_reason: str,
    approved: int,
    total_items: int,
    n_failed: int,
) -> str:
    """Map Sentinel's exit_reason + item counts to the cortex vocabulary
    suggested in the plan (``dry_run | approved N of M | failed |
    budget-exhausted``).

    Keeps the mapping explicit so a ``cortex doctor`` grep over
    ``Verdict:`` lines reveals the same values regardless of how
    Sentinel names its exits internally.
    """
    if exit_reason == "dry_run":
        return "dry_run"
    if exit_reason.startswith("budget"):
        return "budget-exhausted"
    if exit_reason == "interrupted":
        return "interrupted"
    if n_failed and not approved:
        return "failed"
    if total_items:
        return f"approved {approved} of {total_items}"
    return exit_reason or "no-work"


def _format_lenses(lens_scores: list[tuple[str, int]]) -> str:
    if not lens_scores:
        return "(no scan this cycle)"
    return " · ".join(f"{name} {score}/100" for name, score in lens_scores)


def _format_providers(
    providers_by_role: list[tuple[str, str, str]],
) -> str:
    if not providers_by_role:
        return "(none recorded)"
    # Dedupe by role — first entry wins. The plan example shows one
    # model per role, not N.
    seen: dict[str, str] = {}
    for role, provider, model in providers_by_role:
        if not role:
            continue
        seen.setdefault(role, f"{role}={provider}/{model}" if provider else f"{role}={model}")
    if not seen:
        return "(none recorded)"
    return ", ".join(seen[role] for role in sorted(seen))


def render_cycle_journal_entry(cycle_data: CortexCycleData) -> str:
    """Render the cortex-journal markdown body for one Sentinel cycle.

    Follows the template at ``.cortex/templates/journal/sentinel-cycle.md``
    (content shape also spelled out in autumn-garage's T1.6 plan).
    Populates every field from cycle_data — no placeholder text left in
    the output. Callers pass the result to ``write_cortex_journal_entry``.
    """
    started = datetime.fromtimestamp(cycle_data.started_at)
    date = started.strftime("%Y-%m-%d")

    # Cites: the canonical source-of-truth for the cycle is the run
    # journal in .sentinel/runs/. Cortex doctor uses this field to trace
    # the cortex entry back to the machine-readable record.
    cites = cycle_data.run_journal_relpath or f".sentinel/runs/{cycle_data.cycle_id}.md"

    approved = sum(
        1 for _, _, status in cycle_data.work_item_outcomes
        if status in {"succeeded-approved", "approved"}
    )
    total_items = len(cycle_data.work_item_outcomes)
    n_failed = sum(
        1 for _, _, status in cycle_data.work_item_outcomes
        if "failed" in status
    )
    verdict = _format_verdict(
        cycle_data.exit_reason, approved, total_items, n_failed,
    )

    summary = _short_summary(cycle_data, verdict)
    one_line = _one_line_summary(cycle_data, verdict, approved, total_items)

    if cycle_data.overall_score is not None:
        # Rendered the same whether lens-by-lens detail is also
        # available — the lens block carries the per-lens scores
        # separately. Kept as a single branch for the reader's sake.
        health_line = f"{cycle_data.overall_score}/100"
    else:
        health_line = "(not computed this cycle)"

    findings_line = (
        f"{cycle_data.refinement_count} refinements + "
        f"{cycle_data.expansion_count} expansion proposals"
    )

    pr_line = cycle_data.pr_url or "(none opened)"
    duration = _format_duration(cycle_data.ended_at - cycle_data.started_at)
    providers_line = _format_providers(cycle_data.providers_by_role)

    run_journal_link = cites
    run_excerpt = _build_run_journal_excerpt(cycle_data)

    follow_ups = _build_follow_ups(cycle_data, verdict)

    return _CYCLE_TEMPLATE.format(
        cycle_id=cycle_data.cycle_id,
        summary=summary,
        date=date,
        cites=cites,
        one_line_summary=one_line,
        lenses_line=_format_lenses(cycle_data.lens_scores),
        health_line=health_line,
        findings_line=findings_line,
        verdict=verdict,
        pr_line=pr_line,
        spend=cycle_data.total_cost_usd,
        duration=duration,
        providers_line=providers_line,
        run_journal_link=run_journal_link,
        run_journal_excerpt=run_excerpt,
        follow_ups=follow_ups,
    )


def _short_summary(data: CortexCycleData, verdict: str) -> str:
    """One phrase for the title line."""
    if verdict == "dry_run":
        return "dry run (no execution)"
    if verdict == "budget-exhausted":
        return "stopped at budget cap"
    if verdict == "interrupted":
        return "interrupted"
    if verdict.startswith("approved"):
        return verdict
    if verdict == "failed":
        return "work items failed"
    return verdict


def _one_line_summary(
    data: CortexCycleData, verdict: str, approved: int, total: int,
) -> str:
    if data.overall_score is not None:
        score_frag = f"health {data.overall_score}/100"
    else:
        score_frag = "no scan this cycle"
    item_frag = (
        f"{approved}/{total} work items approved" if total
        else "no work items executed"
    )
    return f"{score_frag}; {item_frag}; verdict: {verdict}."


def _build_run_journal_excerpt(data: CortexCycleData) -> str:
    """Render the short excerpt block cited in the ``## Run journal``
    section. Kept small by design — the canonical full record is the
    linked ``.sentinel/runs/<id>.md``."""
    lines: list[str] = []
    if data.work_item_outcomes:
        lines.append("Work items:")
        for wi_id, title, status in data.work_item_outcomes:
            lines.append(f"- **{wi_id}** {title} — {status}")
    if data.total_provider_calls:
        lines.append("")
        lines.append(
            f"Provider calls: {data.total_provider_calls} "
            f"(${data.total_cost_usd:.4f})",
        )
    text = "\n".join(lines).strip()
    if not text:
        return ""
    # Truncate defensively; the full data is one link-click away.
    if len(text) > _RUN_JOURNAL_EXCERPT_CHARS:
        text = text[:_RUN_JOURNAL_EXCERPT_CHARS].rstrip() + " ..."
    return text + "\n"


def _build_follow_ups(data: CortexCycleData, verdict: str) -> str:
    """Render the ``## Follow-ups`` section.

    The plan requires a concrete action list or an explicit "no
    follow-up needed" one-liner. We enumerate failed work items as
    natural follow-ups; when the cycle shipped cleanly we still emit
    the explicit line so the section never reads as accidentally blank.
    """
    failed = [
        (wi_id, title) for wi_id, title, status in data.work_item_outcomes
        if "failed" in status or "rejected" in status
    ]
    if failed:
        return "\n".join(
            f"- [ ] Retry or investigate **{wi_id}**: {title}"
            for wi_id, title in failed
        ) + "\n"
    if verdict == "dry_run":
        return "- [ ] Re-run without `--dry-run` to execute the plan.\n"
    if verdict == "budget-exhausted":
        return (
            "- [ ] Raise budget or resume cycle to finish remaining backlog.\n"
        )
    return "- No follow-up needed — cycle closed cleanly.\n"


# ---------- Write path ----------


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a cortex-journal write attempt.

    ``status`` is one of:
      ``written`` — file created
      ``skipped_no_cortex`` — ``.cortex/`` absent (auto-detect miss)
      ``skipped_disabled`` — explicit opt-out via flag or config
      ``skipped_existing`` — dedup: file for this cycle_id already exists
      ``failed`` — write raised, structured error logged, cycle unaffected

    ``path`` is the absolute path on ``written`` / ``skipped_existing``;
    ``None`` otherwise. ``warning`` is the operator-visible message when
    non-empty — the caller prints it to stderr.
    """

    status: str
    path: Path | None = None
    warning: str = ""


def cycle_journal_filename(cycle_id: str, started_at: float) -> str:
    """Compute the cortex-journal filename for a cycle.

    Format: ``<YYYY-MM-DD>-sentinel-cycle-<cycle-id>.md``. The date
    prefix comes from ``started_at`` (not write time) so a cycle that
    spans a day boundary files under its start date and a reader
    grepping by date finds the right entry."""
    date = datetime.fromtimestamp(started_at).strftime("%Y-%m-%d")
    return f"{date}-sentinel-cycle-{cycle_id}.md"


def write_cortex_journal_entry(
    project_dir: Path,
    cycle_data: CortexCycleData,
    *,
    force: bool = False,
) -> WriteResult:
    """Atomically write the cortex-journal entry for this cycle.

    **Non-blocking failure mode.** Any OSError is caught, logged to
    ``.sentinel/state/cortex-write-errors.jsonl`` as one structured
    JSON line, and surfaced via ``WriteResult.warning``. The caller
    prints the warning and continues — cycle exit code is unaffected.

    **Dedup.** If the target filename already exists, we do NOT
    overwrite (Cortex Protocol § 4.1 — the Journal is append-only).
    The result is ``skipped_existing`` with a warning. Cycle IDs are
    timestamp-based so this only fires on clock anomalies; the write
    path treats dedup as a correctness invariant, not a heuristic.

    **Atomic.** Write-to-tmp + ``os.replace`` so a partial write never
    lands under the target path.

    ``force=True`` bypasses detection (already done upstream) — the
    caller has resolved the enabled-vs-disabled precedence and is only
    asking us to attempt the write.
    """
    presence = detect_cortex(project_dir)
    if not presence.dir_present and not force:
        return WriteResult(status="skipped_no_cortex")

    body = render_cycle_journal_entry(cycle_data)

    journal_dir = project_dir / ".cortex" / "journal"
    filename = cycle_journal_filename(cycle_data.cycle_id, cycle_data.started_at)
    target = journal_dir / filename

    try:
        journal_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _record_write_failure(
            project_dir, cycle_data.cycle_id, exc,
            "could not create .cortex/journal/",
        )

    if target.exists():
        # Append-only invariant — do NOT overwrite. Surface the skip so
        # the operator sees the (rare) clock-collision and can
        # investigate if it happens.
        return WriteResult(
            status="skipped_existing",
            path=target,
            warning=(
                f"cortex-journal entry for cycle {cycle_data.cycle_id} "
                "already exists; skipping write"
            ),
        )

    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(body)
        os.replace(tmp, target)
    except OSError as exc:
        # Clean up the tmp if it exists so we don't litter the journal
        # dir with half-written .tmp files on retry.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass  # best-effort cleanup; real error already captured below
        return _record_write_failure(
            project_dir, cycle_data.cycle_id, exc,
            f"could not write {target.name}",
        )

    return WriteResult(status="written", path=target)


def _record_write_failure(
    project_dir: Path,
    cycle_id: str,
    exc: Exception,
    message: str,
) -> WriteResult:
    """Append one line to ``.sentinel/state/cortex-write-errors.jsonl``.

    Structured so operators can grep the log without re-parsing the
    human warning. Fields: timestamp, cycle_id, error_class,
    error_message. Appending is append-only for the same
    auditability reason cortex's own Journal is — a write failure we
    can't reconstruct later is a silent failure.

    If even the log-write fails, we log via ``logger.warning`` and
    continue. The whole point of this function is to keep the cycle
    alive; a failure in the failure-log path cannot crash it.
    """
    log_dir = project_dir / ".sentinel" / "state"
    log_path = log_dir / "cortex-write-errors.jsonl"
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "cycle_id": cycle_id,
        "error_class": type(exc).__name__,
        "error_message": str(exc),
    }
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as fp:
            fp.write(json.dumps(record) + "\n")
    except OSError as log_exc:
        # Log-of-last-resort. We still return a structured failure so
        # the caller can surface the original error to the operator.
        logger.warning(
            "cortex-write-errors.jsonl append failed: %s (original: %s)",
            log_exc, exc,
        )

    return WriteResult(
        status="failed",
        warning=f"cortex-journal write failed ({type(exc).__name__}): {message}",
    )


# ---------- Enablement resolution ----------


def resolve_enabled(
    *,
    cli_flag: bool | None,
    config_value: str | None,
    cortex_present: bool,
) -> bool:
    """Resolve whether to write a cortex-journal entry for this cycle.

    Precedence: ``cli_flag`` > ``config_value`` > auto-detect.

    - ``cli_flag=True`` — force on (``--cortex-journal``)
    - ``cli_flag=False`` — force off (``--no-cortex-journal``)
    - ``cli_flag=None`` — fall through to config
    - ``config_value='on'`` — force on
    - ``config_value='off'`` — force off
    - ``config_value='auto'`` or ``None`` — write iff ``.cortex/`` present

    An unknown config_value falls back to auto, so a typo does not
    silently disable the integration across a whole project.
    """
    if cli_flag is True:
        return True
    if cli_flag is False:
        return False
    if config_value == "on":
        return True
    if config_value == "off":
        return False
    # 'auto', None, or unknown → detect
    return cortex_present


# ---------- Cycle-data extraction ----------


def build_cycle_data_from_journal(
    journal: object,
    *,
    cycle_id: str,
    project_dir: Path,
    overall_score: int | None = None,
    lens_scores: list[tuple[str, int]] | None = None,
    refinement_count: int = 0,
    expansion_count: int = 0,
) -> CortexCycleData:
    """Extract the cortex-render fields from a live Sentinel ``Journal``.

    Kept separate from ``render_cycle_journal_entry`` so both the
    extraction and the rendering can be tested independently. Takes
    ``journal`` as ``object`` because ``sentinel.journal.Journal`` is
    a rich dataclass we do not want to import at module top-level
    (circular-import hazard inside ``src/sentinel/``).
    """
    # Work-item outcomes: map sentinel-vocabulary to a stable
    # cortex-side status string. "succeeded-approved" mirrors the
    # (coder_status, reviewer_verdict) pair so the cortex reader can
    # see both pieces without loading the sentinel dataclass shape.
    outcomes: list[tuple[str, str, str]] = []
    for wi in getattr(journal, "work_items", []):
        reviewer = getattr(wi, "reviewer_verdict", None) or "no-review"
        coder = getattr(wi, "coder_status", "unknown")
        status = f"{coder}-{reviewer}" if coder != "failed" else "failed"
        outcomes.append(
            (
                getattr(wi, "work_item_id", "?"),
                getattr(wi, "title", "(untitled)"),
                status,
            ),
        )

    # Providers-by-role: dedupe by (role, provider, model). First
    # appearance wins so the journal reads the same way humans talk
    # about provider usage ("coder ran claude-sonnet-4-6", not "claude
    # N times").
    seen: set[tuple[str, str, str]] = set()
    providers_by_role: list[tuple[str, str, str]] = []
    for call in getattr(journal, "provider_calls", []):
        role = getattr(call, "role", "") or ""
        provider = getattr(call, "provider", "") or ""
        model = getattr(call, "model", "") or ""
        key = (role, provider, model)
        if key in seen:
            continue
        seen.add(key)
        providers_by_role.append(key)

    total_cost = sum(
        getattr(c, "cost_usd", 0.0) for c in getattr(journal, "provider_calls", [])
    )
    total_calls = len(getattr(journal, "provider_calls", []))

    started_at = float(getattr(journal, "started_at", time.time()))
    ended_at_val = getattr(journal, "ended_at", None)
    ended_at = float(ended_at_val) if ended_at_val is not None else time.time()

    # Try to derive the run-journal relpath from the journal's resolved
    # path (set after its first write()). Fall back to the conventional
    # path so the cortex entry still cites somewhere useful.
    resolved_path = getattr(journal, "_resolved_path", None)
    if isinstance(resolved_path, Path):
        try:
            run_relpath = str(resolved_path.relative_to(project_dir))
        except ValueError:
            run_relpath = f".sentinel/runs/{cycle_id}.md"
    else:
        run_relpath = f".sentinel/runs/{cycle_id}.md"

    return CortexCycleData(
        cycle_id=cycle_id,
        started_at=started_at,
        ended_at=ended_at,
        project_name=getattr(journal, "project_name", project_dir.name),
        branch=getattr(journal, "branch", "(unknown)"),
        exit_reason=getattr(journal, "exit_reason", "in_progress"),
        total_cost_usd=total_cost,
        total_provider_calls=total_calls,
        lens_scores=lens_scores or [],
        overall_score=overall_score,
        refinement_count=refinement_count,
        expansion_count=expansion_count,
        work_item_outcomes=outcomes,
        pr_url=_first_pr_url(journal),
        providers_by_role=providers_by_role,
        run_journal_relpath=run_relpath,
    )


def _first_pr_url(journal: object) -> str:
    """Return the first non-empty work-item PR URL, else ``""``."""
    for wi in getattr(journal, "work_items", []):
        url = getattr(wi, "pr_url", "")
        if url:
            return url
    return ""


def cycle_id_from_run_path(run_path: Path) -> str:
    """Extract the cycle-id token from a ``.sentinel/runs/<id>.md`` path.

    Sentinel names run files ``<YYYY-MM-DD-HHMMSS>.md`` (with an
    optional ``-N`` collision suffix); we return the stem verbatim so
    a cortex reader can join ``cortex/journal/*.md`` to
    ``sentinel/runs/*.md`` by cycle_id substring.
    """
    return run_path.stem
