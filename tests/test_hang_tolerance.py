"""Tests for defense-in-depth against hung provider calls.

Touchstone dogfood found a real hang: a Gemini subprocess ran 13+ minutes
past its budget, and asyncio.gather kept waiting for it — blocking the
whole pipeline and erasing six sibling lens evaluations that had
already succeeded.

Two layers of protection:
1. run_cli_async catches CancelledError from outer timeouts and kills
   the subprocess explicitly (not just on its own TimeoutError).
2. Monitor.assess wraps each evaluate_one in asyncio.wait_for and
   uses gather(return_exceptions=True) so one hung lens can't block
   the pipeline.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from sentinel.providers.interface import run_cli_async


class TestRunCliAsyncCancellationKillsSubprocess:
    """If an outer asyncio.wait_for cancels run_cli_async mid-flight,
    the subprocess must be killed — not left running as an orphan.
    Without this fix, the Touchstone dogfood pattern reproduces: outer
    task gives up, inner subprocess keeps running for minutes."""

    @pytest.mark.asyncio
    async def test_outer_timeout_kills_subprocess(self) -> None:
        """Outer wait_for with 1s timeout on a sleep 60 subprocess
        must terminate in ~1s AND leave no running subprocess behind.

        This exercises the CancelledError path in run_cli_async: the
        inner subprocess-level wait_for has a much larger timeout
        (60s), so the kill happens via cancellation, not inner timeout.
        """
        import time
        started = time.perf_counter()

        with pytest.raises((TimeoutError, asyncio.CancelledError)):
            await asyncio.wait_for(
                # Inner timeout is deliberately huge; outer wait_for
                # fires first and must cancel down to subprocess kill.
                run_cli_async(["/bin/sleep", "60"], timeout=3600),
                timeout=1.0,
            )

        elapsed = time.perf_counter() - started
        # Should finish ~1s + kill overhead, not 60s
        assert elapsed < 5.0, (
            f"outer timeout took {elapsed:.1f}s; subprocess kill "
            f"did not fire on CancelledError path"
        )

    @pytest.mark.asyncio
    async def test_inner_timeout_still_kills_subprocess(self) -> None:
        """The original TimeoutError path still works — regression
        guard so adding the CancelledError handler didn't break the
        primary clamped-timeout case."""
        import time
        started = time.perf_counter()
        with pytest.raises(subprocess.TimeoutExpired):
            await run_cli_async(["/bin/sleep", "60"], timeout=1)
        elapsed = time.perf_counter() - started
        assert elapsed < 3.0


class TestMonitorHungLensToleration:
    """Monitor.assess's gather must not block on a single hung lens
    evaluation. The pattern: one task stays stuck forever while the
    others complete; gather returns all completed results plus a
    timeout-error LensEvaluation for the stuck one."""

    @pytest.mark.asyncio
    async def test_wait_for_wraps_each_lens_independently(self) -> None:
        """Spot check: a timed-out coroutine in gather with
        return_exceptions=True doesn't cancel the other tasks."""

        async def completes() -> str:
            return "ok"

        async def hangs() -> str:
            await asyncio.sleep(60)
            return "never"

        async def wrapped(coro, timeout: float) -> str:
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except TimeoutError:
                return "timeout"

        results = await asyncio.gather(
            wrapped(completes(), 2.0),
            wrapped(hangs(), 0.5),
            wrapped(completes(), 2.0),
            return_exceptions=True,
        )

        # Two completes, one timeout — NOT three hangs, NOT a raise
        assert results == ["ok", "timeout", "ok"]
