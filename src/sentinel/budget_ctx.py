"""Cycle-scoped time budget that provider calls can read.

`sentinel work --budget 10m` sets a deadline for the whole cycle. Each
provider checks `is_budget_exhausted()` BEFORE dispatching a call: if the
budget is already gone, the call is skipped (recorded in the journal,
no subprocess spawned). If budget remains, the call runs at its own
configured timeout — we do not shrink subprocess timeouts dynamically.

Why between-call gating instead of mid-call clamping: dogfood on the
sentinel repo (2026-04-16) showed that shrinking subprocess timeouts
to fit the remaining budget killed in-flight Gemini calls and produced
zero output for the latency cost — strictly worse than letting the call
finish slowly. With between-call gating, every dispatched call gets to
complete naturally; only the next call is gated on remaining budget.

Worst case overshoot: a call started just before the deadline can run
for up to its own configured timeout (default 600s) past the deadline.
That's a one-shot overshoot, not a destructive kill — and the next
call won't start. Tighten provider.timeout_sec if a tighter ceiling is
required.

ContextVar is used so asyncio tasks and nested calls inherit the
deadline automatically. When no deadline is set (unit tests, `sentinel
work` without `--budget`), `is_budget_exhausted()` returns False and
behavior is unchanged.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

# Absolute wall-clock timestamp (time.time()) when the current cycle must
# end. None means no cycle budget is in effect — calls run unconstrained.
# Set by run_work at cycle start, cleared on exit.
_cycle_deadline: ContextVar[float | None] = ContextVar(
    "sentinel_cycle_deadline", default=None,
)


def set_cycle_deadline(seconds_from_now: float | None) -> None:
    """Set the cycle deadline to `seconds_from_now` in the future, or
    clear it (None). Called by run_work at the start of each cycle."""
    if seconds_from_now is None:
        _cycle_deadline.set(None)
    else:
        _cycle_deadline.set(time.time() + float(seconds_from_now))


def remaining_seconds() -> float | None:
    """Seconds until the cycle deadline, or None if no deadline is set.
    Never returns negative — a passed deadline returns 0.0 so callers
    can short-circuit cleanly."""
    deadline = _cycle_deadline.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.time())


def is_budget_exhausted() -> bool:
    """True if a cycle deadline is set AND it has already passed.

    Providers call this before dispatching any subprocess or HTTP call.
    A True result means: do not start the call, return a budget-exhausted
    error response, journal the skip. False means: proceed at full
    configured timeout — we do NOT shrink the timeout to fit.

    No deadline set → always False (no constraints).
    """
    remaining = remaining_seconds()
    return remaining is not None and remaining <= 0.0
