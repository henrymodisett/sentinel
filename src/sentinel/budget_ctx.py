"""Cycle-scoped time budget that provider CLI calls can read.

`sentinel work --budget 10m` sets a deadline for the whole cycle. Without
this module, each provider sub-call still honored its own `provider_timeout_sec`
(default 600s) — so a single slow synthesis call could blow past a 10-minute
cycle budget by another 10 minutes. Observed live on portfolio_new: 10m
budget, 21.6m actual runtime, hardcoded Gemini timeout ignored the budget
completely.

The fix: carry a deadline in a `ContextVar` set by `run_work` at cycle
start. `run_cli_async` clamps each subprocess timeout to the minimum of
the provider's configured timeout and the time remaining until the cycle
deadline. Nothing runs longer than the cycle is allowed to live.

ContextVar was chosen over threading a `deadline=` argument through every
layer because asyncio tasks and nested subprocess calls inherit it
automatically — no plumbing through Router, Monitor, each Role, each
Provider method signature. When no deadline is set (unit tests, `sentinel
work` without `--budget`), behavior is unchanged.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

# Absolute wall-clock timestamp (time.time()) when the current cycle must
# end. None means no cycle budget is in effect — provider timeouts apply
# as configured. Set by run_work at cycle start, cleared on exit.
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
    can short-circuit rather than compute a negative timeout."""
    deadline = _cycle_deadline.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.time())


def clamp_timeout(base_timeout: int) -> int:
    """Return the effective timeout for a provider CLI call.

    With no cycle deadline set: returns base_timeout unchanged.
    With a deadline: returns min(base_timeout, remaining_seconds),
    floored at 1 second so we never ask asyncio.wait_for for a zero
    or negative timeout (which would raise immediately without letting
    the CLI produce even a quick error).
    """
    remaining = remaining_seconds()
    if remaining is None:
        return base_timeout
    return max(1, min(base_timeout, int(remaining)))
