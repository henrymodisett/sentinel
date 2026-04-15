"""Per-cycle run journal — `.sentinel/runs/<ts>.md`.

Yesterday's dogfood found 7 bugs in 2 hours by eyeballing stdout. With a
structured run journal, the same kind of investigation reads from one
file after the fact instead of polling a live process. The journal is
the single artifact that answers "what happened on that run?"

Shape:
- Header: project, branch, budget, start time, exit reason
- Phase summary table (name, duration, cost)
- Provider-call appendix (one JSONL line per LLM/HTTP call)
- Per-work-item summary

The journal is written incrementally via a ContextVar-scoped accumulator,
so a crashed cycle still leaves a partial file behind. ContextVar matches
the budget_ctx pattern — providers and phase code read the current
journal without a plumbed argument.

What this module is NOT:
- It does not capture raw prompts / responses by default. Doing so would
  require a redaction layer to avoid leaking secrets from project source
  into a logged file. Opt-in raw capture is a separate future concern.
- It does not synthesize trends across runs. Each journal stands alone.
  Trend analysis is a downstream tool problem.
"""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path  # noqa: TC003 — runtime use for fs writes

logger = logging.getLogger(__name__)


@dataclass
class PhaseRecord:
    name: str
    started_at: float
    ended_at: float | None = None
    status: str = "running"  # running | done | failed | aborted
    error: str | None = None

    @property
    def duration_s(self) -> float | None:
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at


@dataclass
class ProviderCall:
    """One LLM/HTTP call's metadata. NOT the prompt or response."""
    phase: str
    provider: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    was_clamped: bool = False
    error: str | None = None


@dataclass
class WorkItemRecord:
    work_item_id: str
    title: str
    coder_status: str = "pending"  # pending | succeeded | failed
    coder_error: str | None = None
    reviewer_verdict: str | None = None  # approved | changes_requested | None


@dataclass
class Journal:
    """Per-cycle run journal. One per `sentinel work` invocation."""
    project_path: Path
    project_name: str
    branch: str
    budget_str: str | None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    exit_reason: str = "in_progress"
    phases: list[PhaseRecord] = field(default_factory=list)
    provider_calls: list[ProviderCall] = field(default_factory=list)
    work_items: list[WorkItemRecord] = field(default_factory=list)

    def start_phase(self, name: str) -> PhaseRecord:
        record = PhaseRecord(name=name, started_at=time.time())
        self.phases.append(record)
        return record

    def end_phase(
        self,
        name: str,
        status: str = "done",
        error: str | None = None,
    ) -> None:
        # Match by name from the back so re-entered phases (rare but
        # possible in loop mode) update the right record.
        for record in reversed(self.phases):
            if record.name == name and record.ended_at is None:
                record.ended_at = time.time()
                record.status = status
                record.error = error
                return
        # No matching open phase — record one so the data isn't lost
        record = PhaseRecord(
            name=name, started_at=time.time(), ended_at=time.time(),
            status=status, error=error,
        )
        self.phases.append(record)

    def record_provider_call(self, call: ProviderCall) -> None:
        self.provider_calls.append(call)

    def record_work_item(self, item: WorkItemRecord) -> None:
        self.work_items.append(item)

    def write(self) -> Path:
        """Write the journal markdown. Idempotent — overwrites on
        subsequent calls so the file always reflects the latest state.
        Callers can write incrementally during the cycle to leave a
        usable file even if the process crashes."""
        if self.ended_at is None:
            self.ended_at = time.time()
        runs_dir = self.project_path / ".sentinel" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.fromtimestamp(self.started_at).strftime(
            "%Y-%m-%d-%H%M%S",
        )
        path = runs_dir / f"{ts}.md"
        path.write_text(self._render())
        return path

    def _render(self) -> str:
        total_cost = sum(c.cost_usd for c in self.provider_calls)
        total_duration = (self.ended_at or time.time()) - self.started_at
        clamped_count = sum(1 for c in self.provider_calls if c.was_clamped)
        started = datetime.fromtimestamp(self.started_at).strftime(
            "%Y-%m-%d %H:%M:%S",
        )

        lines: list[str] = [
            f"# Sentinel Run — {started}",
            "",
            f"**Project:** {self.project_name}  "
            f"**Branch:** {self.branch}  "
            f"**Budget:** {self.budget_str or '(none)'}  "
            f"**Exit:** {self.exit_reason}",
            "",
            f"**Total time:** {total_duration:.1f}s  "
            f"**Total cost:** ${total_cost:.4f}  "
            f"**Provider calls:** {len(self.provider_calls)} "
            f"({clamped_count} clamped)",
            "",
        ]

        if self.phases:
            lines += ["## Phases", "", "| Phase | Duration | Status |", "|---|---|---|"]
            for p in self.phases:
                duration = (
                    f"{p.duration_s:.2f}s" if p.duration_s is not None
                    else "—"
                )
                status = p.status if not p.error else f"{p.status} ({p.error})"
                lines.append(f"| {p.name} | {duration} | {status} |")
            lines.append("")

        if self.work_items:
            lines += ["## Work items", ""]
            for wi in self.work_items:
                bullet = f"- **{wi.work_item_id}** {wi.title}"
                lines.append(bullet)
                lines.append(
                    f"  - Coder: {wi.coder_status}"
                    + (f" — {wi.coder_error}" if wi.coder_error else "")
                )
                if wi.reviewer_verdict:
                    lines.append(f"  - Reviewer: {wi.reviewer_verdict}")
            lines.append("")

        if self.provider_calls:
            lines += [
                "## Provider calls",
                "",
                "```jsonl",
            ]
            for c in self.provider_calls:
                payload = {
                    "phase": c.phase,
                    "provider": c.provider,
                    "model": c.model,
                    "latency_ms": c.latency_ms,
                    "in": c.input_tokens,
                    "out": c.output_tokens,
                    "cost": round(c.cost_usd, 6),
                    "clamped": c.was_clamped,
                }
                if c.error:
                    payload["error"] = c.error
                lines.append(json.dumps(payload))
            lines += ["```", ""]

        return "\n".join(lines)


# ContextVar-scoped current journal. None when not in a sentinel work
# cycle (unit tests, ad-hoc scan, etc.) so calling code can no-op
# rather than raising.
_current_journal: ContextVar[Journal | None] = ContextVar(
    "sentinel_journal", default=None,
)
# ContextVar for the active phase name. Providers read this when
# recording calls so each provider call carries the phase context
# without the provider needing the journal API itself.
_current_phase: ContextVar[str] = ContextVar(
    "sentinel_phase", default="(unknown)",
)


def set_current_journal(journal: Journal | None) -> None:
    """Set the active journal for the current cycle, or clear it
    (None). Called from _run_single_cycle at start and end."""
    _current_journal.set(journal)


def current_journal() -> Journal | None:
    """Return the active journal, if any. None outside a cycle."""
    return _current_journal.get()


def set_current_phase(phase: str) -> None:
    """Set the active phase name. Phase wrapper code calls this when
    entering a phase; provider code reads via current_phase()."""
    _current_phase.set(phase)


def current_phase() -> str:
    """Return the active phase name, or '(unknown)' outside a cycle."""
    return _current_phase.get()


def record_provider_call(
    provider: str,
    model: str,
    latency_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    was_clamped: bool = False,
    error: str | None = None,
    phase: str | None = None,
) -> None:
    """Append a provider call to the active journal. No-op if no
    journal is set (e.g., a `sentinel scan` invocation outside the
    work loop, or a unit test that exercises a provider directly).

    `phase` is read from the current_phase() ContextVar by default —
    callers don't need to pass it. Override only when recording from
    a known-out-of-context position."""
    journal = current_journal()
    if journal is None:
        return
    journal.record_provider_call(ProviderCall(
        phase=phase if phase is not None else current_phase(),
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        was_clamped=was_clamped,
        error=error,
    ))
