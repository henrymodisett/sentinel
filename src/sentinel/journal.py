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
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path  # noqa: TC003 — runtime use for fs writes

logger = logging.getLogger(__name__)

# Stderr is rendered into the journal markdown only for failed calls. We
# truncate at render time (not at capture) so any future tooling can read
# the full payload from the in-memory ProviderCall, while the on-disk
# markdown stays a sane size.
_STDERR_RENDER_LIMIT = 2048


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
    error: str | None = None
    # Sentinel role that issued the call (monitor / researcher / planner /
    # coder / reviewer) or "" if the call happened outside a role context
    # (test harness, ad-hoc CLI usage). Lets the journal break down spend
    # by role — answers "where is my money going?" without manually
    # tagging each call site.
    role: str = ""
    # Name of the routing rule that overrode the configured (provider, model)
    # for this call. Empty when the configured default was used. Lets the
    # journal answer "why did this call use that model?" — when paired with
    # the rule's reason in the source, the override is fully traceable.
    routed_via: str = ""
    # Raw stderr from the provider CLI (subprocess) or HTTP error body.
    # Populated whenever the provider has it — captures the actual diagnostic
    # text that lets us debug non-zero exits without re-running the call.
    # Truncated at render time, not at capture, so we keep the full payload
    # available in memory for any tooling that wants it.
    stderr: str = ""


@dataclass
class WorkItemRecord:
    work_item_id: str
    title: str
    coder_status: str = "pending"  # pending | succeeded | failed
    coder_error: str | None = None
    reviewer_verdict: str | None = None  # approved | changes_requested | rejected | None
    # Independent post-execute verifier verdict (project's own lint/test
    # commands re-run against the new code). One of:
    # verified | not_verified | no_check_defined | None (not yet run).
    verification: str | None = None


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
    # Resolved on first write() so incremental rewrites land on the same
    # file. Otherwise two cycles started in the same wall-clock second
    # (or two write() calls of the same journal) would race for the
    # collision-suffixed name and produce duplicate or overwritten files.
    _resolved_path: Path | None = field(default=None, repr=False)

    def start_phase(self, name: str) -> PhaseRecord:
        record = PhaseRecord(name=name, started_at=time.time())
        self.phases.append(record)
        self._checkpoint()
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
                self._checkpoint()
                return
        # No matching open phase — record one so the data isn't lost
        record = PhaseRecord(
            name=name, started_at=time.time(), ended_at=time.time(),
            status=status, error=error,
        )
        self.phases.append(record)
        self._checkpoint()

    def record_provider_call(self, call: ProviderCall) -> None:
        self.provider_calls.append(call)
        self._checkpoint()

    def record_work_item(self, item: WorkItemRecord) -> None:
        self.work_items.append(item)
        self._checkpoint()

    def _checkpoint(self) -> None:
        """Rewrite the journal file with current state.

        Called from every mutating method so a cycle that hangs inside
        a phase still leaves an up-to-date file on disk. Without this,
        the original finally-only write meant killing a stuck cycle
        (SIGKILL from outside, pkill, etc.) produced no journal at all
        — exactly the opposite of "partial file on crash."

        Writes are cheap (small markdown file, few KB) and happen at
        most once per provider call, so a realistic cycle writes
        dozens of times per minute. If this ever becomes a perf
        concern, gate it on a dirty-at-most-once-per-N-seconds check;
        for now the frequent write IS the feature.
        """
        try:
            self.write()
        except OSError as e:
            # Filesystem failures during checkpoint must not crash the
            # cycle. Log and continue — the final write() in the
            # caller's finally block gets another chance.
            logger.warning("journal checkpoint failed: %s", e)

    def write(self) -> Path:
        """Write the journal markdown. Idempotent — overwrites on
        subsequent calls so the file always reflects the latest state.
        Callers can write incrementally during the cycle to leave a
        usable file even if the process crashes.

        Does NOT set ended_at. Callers mark the cycle as ended
        explicitly via `mark_ended()` before the final write; otherwise
        the rendered "Total time" advances with each checkpoint rather
        than freezing at the first write's timestamp. An earlier version
        set ended_at here on the first call, which meant every checkpoint
        after the first (and the final finally-block write) reported the
        same stale total time near zero.

        The destination path is resolved once (on first write) and
        reused for every subsequent write of the same Journal. Two
        cycles started in the same second use the seconds-precision
        timestamp PLUS a numeric suffix (-2, -3, ...) to stay unique;
        the first one to call write() takes the un-suffixed name.
        """
        if self._resolved_path is None:
            self._resolved_path = self._resolve_unique_path()
        # Atomic replace: write to a sibling temp file first, then
        # os.replace() into place. Rewriting the live file with
        # write_text() would leave a corrupted/empty journal if the
        # process dies between truncation and write completion — exactly
        # the crash-survival scenario this whole mechanism is meant to
        # serve. os.replace is atomic on POSIX: either the new file is
        # at the target path, or the old one still is, never a partial.
        tmp = self._resolved_path.with_suffix(
            self._resolved_path.suffix + ".tmp",
        )
        tmp.write_text(self._render())
        os.replace(tmp, self._resolved_path)
        return self._resolved_path

    def mark_ended(self) -> None:
        """Freeze the cycle's end time. Call once at the terminal path
        (normal exit, exception, KeyboardInterrupt — typically in a
        finally block) before the final write. Idempotent: only the
        first call takes effect."""
        if self.ended_at is None:
            self.ended_at = time.time()

    def _resolve_unique_path(self) -> Path:
        runs_dir = self.project_path / ".sentinel" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.fromtimestamp(self.started_at).strftime(
            "%Y-%m-%d-%H%M%S",
        )
        path = runs_dir / f"{ts}.md"
        n = 1
        while path.exists():
            n += 1
            path = runs_dir / f"{ts}-{n}.md"
        return path

    def _render(self) -> str:
        total_cost = sum(c.cost_usd for c in self.provider_calls)
        total_duration = (self.ended_at or time.time()) - self.started_at
        skipped_count = sum(
            1 for c in self.provider_calls if c.error == "budget_exhausted"
        )
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
            f"({skipped_count} skipped — budget exhausted)",
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
                if wi.verification:
                    # Visual marker so verified vs not_verified jumps out
                    # when scanning the journal.
                    icon = {
                        "verified": "✅",
                        "not_verified": "❌",
                        "no_check_defined": "—",
                    }.get(wi.verification, "?")
                    lines.append(
                        f"  - Verifier: {icon} {wi.verification}"
                    )
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
                }
                if c.role:
                    payload["role"] = c.role
                if c.routed_via:
                    payload["routed_via"] = c.routed_via
                if c.error:
                    payload["error"] = c.error
                lines.append(json.dumps(payload))
            lines += ["```", ""]

            roles_present = [c for c in self.provider_calls if c.role]
            if roles_present:
                by_role: dict[str, list[ProviderCall]] = {}
                for c in roles_present:
                    by_role.setdefault(c.role, []).append(c)
                lines += [
                    "## By role",
                    "",
                    "| Role | Calls | Cost | Tokens (in/out) |",
                    "|---|---|---|---|",
                ]
                for role in sorted(by_role):
                    calls = by_role[role]
                    cost = sum(c.cost_usd for c in calls)
                    in_tok = sum(c.input_tokens for c in calls)
                    out_tok = sum(c.output_tokens for c in calls)
                    lines.append(
                        f"| {role} | {len(calls)} | ${cost:.4f} | "
                        f"{in_tok:,}/{out_tok:,} |"
                    )
                lines.append("")

            errors_with_stderr = [
                c for c in self.provider_calls if c.error and c.stderr
            ]
            if errors_with_stderr:
                lines += ["## Provider errors", ""]
                for c in errors_with_stderr:
                    truncated = c.stderr[:_STDERR_RENDER_LIMIT]
                    if len(c.stderr) > _STDERR_RENDER_LIMIT:
                        truncated += (
                            f"\n... [truncated {len(c.stderr) - _STDERR_RENDER_LIMIT} bytes]"
                        )
                    lines += [
                        f"### {c.phase} — {c.provider}/{c.model} ({c.error})",
                        "",
                        "```",
                        truncated,
                        "```",
                        "",
                    ]

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
# ContextVar for the active role (monitor/researcher/planner/coder/reviewer).
# Roles set this on entry to their work; providers read it when recording
# calls. Defaults to empty so test harness and ad-hoc usage produce
# blank role rather than a misleading default.
_current_role: ContextVar[str] = ContextVar(
    "sentinel_role", default="",
)
# ContextVar set by the Router when it overrides a configured model via
# a routing rule. The next provider call consumes and clears it (one-shot)
# so the override is recorded against the call it produced — not against
# subsequent calls that may have used a different rule (or no rule).
_pending_routing_reason: ContextVar[str] = ContextVar(
    "sentinel_pending_routing_reason", default="",
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


def set_current_role(role: str) -> None:
    """Set the active role. Each Sentinel role sets this on entry to
    its method (Monitor.assess, Coder.execute, etc.) so provider calls
    issued during that role's work carry the role tag in the journal."""
    _current_role.set(role)


def current_role() -> str:
    """Return the active role, or '' outside any role context."""
    return _current_role.get()


def set_pending_routing_reason(reason: str) -> None:
    """Router sets this when an override fires. Cleared by the next
    record_provider_call so the reason attaches to the right call."""
    _pending_routing_reason.set(reason)


def consume_pending_routing_reason() -> str:
    """Return and clear the pending routing reason. Called once per
    provider call — if no override is in flight, returns ''."""
    reason = _pending_routing_reason.get()
    if reason:
        _pending_routing_reason.set("")
    return reason


def record_provider_call(
    provider: str,
    model: str,
    latency_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    error: str | None = None,
    phase: str | None = None,
    role: str | None = None,
    stderr: str = "",
) -> None:
    """Append a provider call to the active journal. No-op if no
    journal is set (e.g., a `sentinel scan` invocation outside the
    work loop, or a unit test that exercises a provider directly).

    `phase` and `role` are read from the current_phase()/current_role()
    ContextVars by default — callers don't need to pass them. Override
    only when recording from a known-out-of-context position."""
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
        error=error,
        role=role if role is not None else current_role(),
        routed_via=consume_pending_routing_reason(),
        stderr=stderr,
    ))
