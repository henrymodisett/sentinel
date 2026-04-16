"""Cycle-scoped budget — time and money — that provider calls read.

`sentinel work --budget 10m` sets a time deadline. `--budget $5` sets a
money cap. `--budget 10m,$5` sets both. Each provider checks
`is_budget_exhausted()` BEFORE dispatching a call: if either dimension
is gone, the call is skipped (recorded in the journal, no subprocess
spawned). If budget remains, the call runs at its own configured
timeout — we do not shrink subprocess timeouts dynamically.

Why between-call gating instead of mid-call clamping: dogfood on the
sentinel repo (2026-04-16) showed that shrinking subprocess timeouts
to fit the remaining time budget killed in-flight Gemini calls and
produced zero output for the latency cost — strictly worse than letting
the call finish slowly. With between-call gating, every dispatched call
gets to complete naturally; only the next call is gated on remaining
budget.

Worst case time overshoot: a call started just before the deadline can
run for up to its own configured timeout (default 600s) past the
deadline. That's a one-shot overshoot, not a destructive kill — and
the next call won't start. Tighten provider.timeout_sec if a tighter
ceiling is required.

Money tracking reads the live journal's accumulated cost — no separate
spend file is consulted, so the cap reflects exactly the cost incurred
within this cycle. Free providers (Gemini OAuth, Ollama local) report
$0 per call, so a money cap on a free-only run is naturally a no-op.

ContextVar is used so asyncio tasks and nested calls inherit budget
state automatically. When nothing is set (unit tests, `sentinel work`
without `--budget`), `is_budget_exhausted()` returns False and behavior
is unchanged.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

# Absolute wall-clock timestamp (time.time()) when the current cycle must
# end. None means no time budget — calls are not time-gated. Set by
# run_work at cycle start, cleared on exit.
_cycle_deadline: ContextVar[float | None] = ContextVar(
    "sentinel_cycle_deadline", default=None,
)

# Money cap in USD for the current cycle. None means no money budget —
# only the daily limit (enforced separately by sentinel.budget) applies.
# Compared against the live journal's total cost on each check.
_cycle_money_cap: ContextVar[float | None] = ContextVar(
    "sentinel_cycle_money_cap", default=None,
)


def set_cycle_deadline(seconds_from_now: float | None) -> None:
    """Set the cycle deadline to `seconds_from_now` in the future, or
    clear it (None). Called by run_work at the start of each cycle."""
    if seconds_from_now is None:
        _cycle_deadline.set(None)
    else:
        _cycle_deadline.set(time.time() + float(seconds_from_now))


def set_cycle_money_cap(usd: float | None) -> None:
    """Set the cycle money cap in USD, or clear it (None). The cap is
    compared against the running journal's total cost; once total >= cap,
    `is_budget_exhausted()` returns True and the next provider call is
    skipped."""
    _cycle_money_cap.set(usd)


def remaining_seconds() -> float | None:
    """Seconds until the cycle deadline, or None if no deadline is set.
    Never returns negative — a passed deadline returns 0.0 so callers
    can short-circuit cleanly."""
    deadline = _cycle_deadline.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.time())


def remaining_usd() -> float | None:
    """USD remaining against the cycle money cap, or None if no cap is
    set. Reads the live journal's accumulated cost — costs accrue only
    from calls that actually happened in this cycle."""
    cap = _cycle_money_cap.get()
    if cap is None:
        return None
    from sentinel.journal import current_journal

    journal = current_journal()
    spent = sum(c.cost_usd for c in journal.provider_calls) if journal else 0.0
    return max(0.0, cap - spent)


def is_budget_exhausted() -> bool:
    """True if EITHER the time deadline has passed OR the money cap is
    reached. Providers call this before dispatching any subprocess or
    HTTP call. A True result means: do not start the call, return a
    budget-exhausted error response, journal the skip.

    No constraints set (the test/dev path) → always False.
    """
    time_remaining = remaining_seconds()
    if time_remaining is not None and time_remaining <= 0.0:
        return True
    money_remaining = remaining_usd()
    return money_remaining is not None and money_remaining <= 0.0
