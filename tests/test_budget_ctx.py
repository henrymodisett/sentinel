"""Tests for cycle-scoped budget clamping of provider subprocess timeouts.

Dogfood on portfolio_new ran 21.6 minutes on a `--budget 10m` flag
because each provider CLI still enforced its own 600s timeout
independent of the cycle budget. These tests lock in the invariant:
when a cycle deadline is set, no subprocess timeout exceeds the
remaining time until that deadline.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from sentinel.budget_ctx import (
    clamp_timeout,
    remaining_seconds,
    set_cycle_deadline,
)
from sentinel.providers.interface import run_cli_async


class TestClampTimeout:
    def test_no_deadline_returns_base(self) -> None:
        set_cycle_deadline(None)
        assert clamp_timeout(600) == 600
        assert clamp_timeout(30) == 30

    def test_deadline_clamps_below_base(self) -> None:
        """Remaining < base → we get the remaining time, not the full 600s."""
        set_cycle_deadline(5)  # 5 seconds from now
        clamped = clamp_timeout(600)
        assert 1 <= clamped <= 5

    def test_deadline_above_base_keeps_base(self) -> None:
        """Remaining > base → still use the provider's configured timeout.
        We never extend a provider timeout past its configured ceiling."""
        set_cycle_deadline(3600)  # 1 hour from now
        assert clamp_timeout(600) == 600

    def test_passed_deadline_floors_at_one(self) -> None:
        """If the deadline is already in the past, clamp to 1s — never 0
        or negative, which would make asyncio.wait_for fire immediately
        without letting the CLI produce even a short error output."""
        set_cycle_deadline(-10)  # 10 seconds ago
        assert clamp_timeout(600) == 1

    def test_remaining_seconds_is_nonnegative(self) -> None:
        set_cycle_deadline(-5)
        assert remaining_seconds() == 0.0
        set_cycle_deadline(None)
        assert remaining_seconds() is None
        set_cycle_deadline(10)
        r = remaining_seconds()
        assert r is not None
        assert 9 <= r <= 10


class TestRunCliAsyncHonorsBudget:
    """`run_cli_async` is the choke point for every provider CLI call.
    Clamping must happen here so all providers inherit the behavior."""

    @pytest.mark.asyncio
    async def test_subprocess_timeout_shortened_by_budget(self) -> None:
        """A provider call with a 60s base timeout must time out at ~2s
        when only 2s of budget remain — not run to the full 60s."""
        set_cycle_deadline(2)
        start = time.time()
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            # /bin/sleep 60 — would run 60s without budget clamping
            await run_cli_async(["/bin/sleep", "60"], timeout=60)
        elapsed = time.time() - start

        # Should fail within ~3s (budget + small slack for process spawn)
        assert elapsed < 5.0, (
            f"subprocess ran {elapsed:.1f}s on a 2s budget — budget "
            f"clamp not applied"
        )
        # And the TimeoutExpired should carry the clamped timeout, not
        # the original 60s base.
        assert exc_info.value.timeout <= 5

    @pytest.mark.asyncio
    async def test_no_budget_uses_base_timeout(self) -> None:
        """Without a cycle deadline, behavior is unchanged — the base
        timeout applies. This is the sentinel scan / dev-test path."""
        set_cycle_deadline(None)
        start = time.time()
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            await run_cli_async(["/bin/sleep", "60"], timeout=1)
        elapsed = time.time() - start

        assert elapsed < 3.0  # should hit the 1s base timeout
        assert exc_info.value.timeout == 1
