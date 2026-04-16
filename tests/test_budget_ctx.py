"""Tests for the cycle-scoped budget gating model.

Dogfood on portfolio_new ran 21.6 minutes on a `--budget 10m` flag
because each provider CLI still enforced its own 600s timeout
independent of the cycle budget. The first fix shrank subprocess
timeouts dynamically — but a follow-up dogfood on the sentinel repo
showed that cost zero output: half-finished Gemini calls returned
nothing for the latency we paid.

The current contract:
- `set_cycle_deadline(seconds)` sets when the cycle should end.
- `is_budget_exhausted()` returns True when that deadline has passed.
- Providers consult `is_budget_exhausted()` BEFORE dispatch and skip
  the call if True. Subprocess timeouts are NOT clamped — once a call
  starts, it runs at its full configured timeout.

Worst case: a call started just before the deadline can overshoot by
up to its provider timeout (~10 min for Gemini). That's a one-shot
overshoot, not a destructive kill — the next call won't start.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from sentinel.budget_ctx import (
    is_budget_exhausted,
    remaining_seconds,
    remaining_usd,
    set_cycle_deadline,
    set_cycle_money_cap,
)
from sentinel.cli.work_cmd import _parse_budget
from sentinel.providers.interface import (
    ChatResponse,
    Provider,
    ProviderCapabilities,
    ProviderName,
    run_cli_async,
)


class TestIsBudgetExhausted:
    def test_no_deadline_never_exhausted(self) -> None:
        set_cycle_deadline(None)
        assert is_budget_exhausted() is False

    def test_deadline_in_future_not_exhausted(self) -> None:
        set_cycle_deadline(60)  # 60 seconds from now
        assert is_budget_exhausted() is False

    def test_deadline_in_past_is_exhausted(self) -> None:
        set_cycle_deadline(-1)  # 1 second ago
        assert is_budget_exhausted() is True

    def test_deadline_at_exactly_zero_is_exhausted(self) -> None:
        """A deadline that has just hit (0 remaining) counts as exhausted —
        the next call would start with no budget. Treat 0.0 the same as
        a passed deadline so we never spawn a doomed subprocess."""
        set_cycle_deadline(0)
        assert is_budget_exhausted() is True

    def test_remaining_seconds_is_nonnegative(self) -> None:
        set_cycle_deadline(-5)
        assert remaining_seconds() == 0.0
        set_cycle_deadline(None)
        assert remaining_seconds() is None
        set_cycle_deadline(10)
        r = remaining_seconds()
        assert r is not None
        assert 9 <= r <= 10


class TestRunCliAsyncDoesNotClamp:
    """Subprocess timeouts are now passed through unchanged. The cycle
    budget is enforced between calls, not by shrinking each subprocess
    timeout to fit. This guarantees that any call that starts gets to
    finish — no more zero-output partials."""

    @pytest.mark.asyncio
    async def test_subprocess_runs_to_full_timeout_regardless_of_budget(
        self,
    ) -> None:
        """Even with the cycle deadline already passed, a call that's
        started uses its own timeout. The protection is to not start
        the call in the first place (provider-level), not to kill it
        mid-flight (run_cli_async-level)."""
        set_cycle_deadline(-10)  # cycle is "over"
        start = time.time()
        # /bin/sleep 2 with a 1s timeout — should hit the 1s timeout,
        # NOT a clamped 0s timeout, NOT the cycle deadline.
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            await run_cli_async(["/bin/sleep", "2"], timeout=1)
        elapsed = time.time() - start

        # The call ran for ~1 second (its own timeout), proving the
        # subprocess timeout was respected as-passed.
        assert 0.5 <= elapsed <= 2.5
        assert exc_info.value.timeout == 1

    @pytest.mark.asyncio
    async def test_no_budget_set_uses_passed_timeout(self) -> None:
        """The dev-test path with no cycle deadline behaves identically
        — the timeout argument is honored as given."""
        set_cycle_deadline(None)
        start = time.time()
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            await run_cli_async(["/bin/sleep", "60"], timeout=1)
        elapsed = time.time() - start

        assert elapsed < 3.0
        assert exc_info.value.timeout == 1


class TestProviderShortCircuitsWhenBudgetExhausted:
    """Every provider's chat()/code() entrypoint must check
    is_budget_exhausted() before dispatching the call. A short-circuited
    call appears in the journal as error="budget_exhausted" so it's
    visible — silently skipping would hide the budget overrun."""

    @pytest.mark.asyncio
    async def test_provider_returns_error_response_when_budget_gone(
        self,
    ) -> None:
        """The shared `_abort_if_budget_exhausted` helper produces a
        clearly-labeled ChatResponse without spawning a subprocess.
        Any concrete provider's chat() should use it as the first
        line of the method."""

        class FakeProvider(Provider):
            name = ProviderName.GEMINI
            cli_command = "fake"
            capabilities = ProviderCapabilities(chat=True)
            calls_made = 0

            async def chat(self, prompt, system_prompt=None):  # noqa: ANN001, ANN201
                if (resp := self._abort_if_budget_exhausted()):
                    return resp
                self.calls_made += 1
                return ChatResponse(content="ok", provider=self.name)

            def detect(self):  # noqa: ANN201
                from sentinel.providers.interface import ProviderStatus
                return ProviderStatus(installed=True, authenticated=True)

        set_cycle_deadline(-1)  # already exhausted
        try:
            provider = FakeProvider()
            response = await provider.chat("anything")
            assert provider.calls_made == 0, (
                "exhausted budget must short-circuit before any work is done"
            )
            assert response.is_error is True
            assert "budget exhausted" in response.content.lower()
        finally:
            set_cycle_deadline(None)

    @pytest.mark.asyncio
    async def test_provider_proceeds_when_budget_remains(self) -> None:
        """Inverse of the above: with budget remaining, the provider
        runs its real path — the helper returns None and the caller
        falls through to the actual dispatch."""

        class FakeProvider(Provider):
            name = ProviderName.GEMINI
            cli_command = "fake"
            capabilities = ProviderCapabilities(chat=True)
            calls_made = 0

            async def chat(self, prompt, system_prompt=None):  # noqa: ANN001, ANN201
                if (resp := self._abort_if_budget_exhausted()):
                    return resp
                self.calls_made += 1
                return ChatResponse(content="ok", provider=self.name)

            def detect(self):  # noqa: ANN201
                from sentinel.providers.interface import ProviderStatus
                return ProviderStatus(installed=True, authenticated=True)

        set_cycle_deadline(60)  # plenty of budget
        try:
            provider = FakeProvider()
            response = await provider.chat("anything")
            assert provider.calls_made == 1
            assert response.content == "ok"
            assert response.is_error is False
        finally:
            set_cycle_deadline(None)


class TestParseBudget:
    """Money and time are independent dimensions. The user can specify
    either, or both via comma-separation. Order doesn't matter."""

    def test_money_only(self) -> None:
        assert _parse_budget("$5") == (5.0, None)
        assert _parse_budget("5") == (5.0, None)
        assert _parse_budget("10.50") == (10.5, None)

    def test_time_only(self) -> None:
        assert _parse_budget("10m") == (None, 600)
        assert _parse_budget("1h") == (None, 3600)
        assert _parse_budget("30s") == (None, 30)

    def test_combined_money_first(self) -> None:
        assert _parse_budget("$5,10m") == (5.0, 600)

    def test_combined_time_first(self) -> None:
        assert _parse_budget("10m,$5") == (5.0, 600)

    def test_combined_with_whitespace(self) -> None:
        """Whitespace around the comma should be tolerated — common copy-paste."""
        assert _parse_budget("10m , $5") == (5.0, 600)

    def test_empty_returns_none(self) -> None:
        assert _parse_budget(None) == (None, None)
        assert _parse_budget("") == (None, None)

    def test_garbage_raises(self) -> None:
        import click as _click
        with pytest.raises(_click.BadParameter):
            _parse_budget("nonsense")
        with pytest.raises(_click.BadParameter):
            _parse_budget("10x")  # invalid time unit


class TestMoneyCapExhaustion:
    """Money cap is checked against the live journal's accumulated cost.
    A run with no cost spent reports False; once cost ≥ cap, True."""

    def test_no_cap_never_exhausted(self) -> None:
        set_cycle_money_cap(None)
        assert is_budget_exhausted() is False
        assert remaining_usd() is None

    def test_cap_with_no_journal_reports_full_remaining(self) -> None:
        """Outside a cycle (no journal), cap is set but nothing is
        spent — exhausted is False, remaining is the full cap."""
        set_cycle_money_cap(10.0)
        try:
            assert is_budget_exhausted() is False
            assert remaining_usd() == 10.0
        finally:
            set_cycle_money_cap(None)

    def test_cap_exhausted_when_journal_total_meets_cap(
        self, tmp_path,
    ) -> None:
        """With a live journal whose total_cost reaches the cap,
        is_budget_exhausted returns True so the next provider call
        short-circuits."""
        from sentinel.journal import (
            Journal,
            ProviderCall,
            set_current_journal,
        )

        j = Journal(
            project_path=tmp_path, project_name="t", branch="main",
            budget_str="$1",
        )
        j.record_provider_call(ProviderCall(
            phase="scan", provider="claude", model="opus",
            latency_ms=100, cost_usd=0.50,
        ))
        j.record_provider_call(ProviderCall(
            phase="scan", provider="claude", model="opus",
            latency_ms=100, cost_usd=0.55,
        ))

        set_current_journal(j)
        set_cycle_money_cap(1.0)
        try:
            assert is_budget_exhausted() is True
            assert remaining_usd() == 0.0
        finally:
            set_current_journal(None)
            set_cycle_money_cap(None)

    def test_time_and_money_caps_are_independent(self) -> None:
        """Setting one dimension does not implicitly set the other.
        A user who specifies only `--budget 5m` should not get any
        money enforcement (only the daily limit applies, separately)."""
        set_cycle_deadline(60)
        set_cycle_money_cap(None)
        try:
            assert is_budget_exhausted() is False
            assert remaining_seconds() is not None
            assert remaining_usd() is None
        finally:
            set_cycle_deadline(None)
