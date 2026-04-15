"""Tests for the per-cycle run journal.

The journal is the single artifact answering "what happened on that
cycle?" — phase timings, every provider call's metadata, exit reason.
These tests cover the contract independently of where it's wired in,
so any future caller (work, scan, future commands) can rely on the
same shape.
"""

from __future__ import annotations

import json
import re
from pathlib import Path  # noqa: TC003 — runtime use via tmp_path

from sentinel.journal import (
    Journal,
    PhaseRecord,
    ProviderCall,
    WorkItemRecord,
    current_journal,
    current_phase,
    record_provider_call,
    set_current_journal,
    set_current_phase,
)


def _journal(tmp_path: Path, **overrides) -> Journal:
    return Journal(
        project_path=tmp_path,
        project_name=overrides.get("project_name", "test-project"),
        branch=overrides.get("branch", "main"),
        budget_str=overrides.get("budget_str"),
    )


class TestJournalShape:
    def test_writes_file_at_expected_path(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        path = j.write()
        assert path.parent == tmp_path / ".sentinel" / "runs"
        assert path.exists()
        assert path.suffix == ".md"

    def test_renders_header_with_project_branch_budget(self, tmp_path: Path) -> None:
        j = _journal(tmp_path, project_name="vesper", branch="feat/foo", budget_str="10m")
        content = j.write().read_text()
        assert "vesper" in content
        assert "feat/foo" in content
        assert "10m" in content
        assert "# Sentinel Run" in content

    def test_phase_timings_in_table(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.start_phase("scan")
        j.end_phase("scan")
        j.start_phase("plan")
        j.end_phase("plan")
        content = j.write().read_text()
        assert "## Phases" in content
        assert "| scan |" in content
        assert "| plan |" in content

    def test_phase_failed_status_carries_error(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.start_phase("scan")
        j.end_phase("scan", status="failed", error="Gemini timed out")
        content = j.write().read_text()
        assert "failed" in content
        assert "Gemini timed out" in content

    def test_provider_calls_appendix_is_jsonl(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="gemini-2.5-flash",
            latency_ms=2104, input_tokens=1820, output_tokens=540,
            cost_usd=0.0021, was_clamped=False,
        ))
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="gemini-2.5-pro",
            latency_ms=31200, input_tokens=12500, output_tokens=2100,
            cost_usd=0.0022, was_clamped=True,
        ))
        content = j.write().read_text()
        assert "## Provider calls" in content
        assert "```jsonl" in content

        # Extract the jsonl block and parse each line
        block = re.search(r"```jsonl\n(.*?)\n```", content, re.DOTALL)
        assert block is not None
        lines = [json.loads(ln) for ln in block.group(1).splitlines() if ln.strip()]
        assert len(lines) == 2
        assert lines[0]["provider"] == "gemini"
        assert lines[0]["clamped"] is False
        assert lines[1]["clamped"] is True

    def test_totals_in_header(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="m",
            latency_ms=100, cost_usd=0.01, was_clamped=False,
        ))
        j.record_provider_call(ProviderCall(
            phase="scan", provider="claude", model="m",
            latency_ms=200, cost_usd=0.02, was_clamped=True,
        ))
        content = j.write().read_text()
        assert "$0.0300" in content
        assert "Provider calls:** 2" in content
        assert "(1 clamped)" in content

    def test_repeated_write_reuses_same_path(self, tmp_path: Path) -> None:
        """Journal.write() is idempotent — incremental writes during a
        cycle must land on the same file, not generate new collision-
        suffixed names on every call."""
        j = _journal(tmp_path)
        first = j.write()
        second = j.write()
        third = j.write()
        assert first == second == third

    def test_two_journals_in_same_second_get_unique_paths(
        self, tmp_path: Path,
    ) -> None:
        """Two cycles started in the same wall-clock second (or even
        the same wall-clock instant — same `started_at`) must NOT
        overwrite each other's journals. The second writer gets a
        numeric suffix; whoever calls write() first takes the clean
        name."""
        same_ts = 1_700_000_000.0  # arbitrary fixed timestamp
        j1 = _journal(tmp_path)
        j1.started_at = same_ts
        j2 = _journal(tmp_path)
        j2.started_at = same_ts

        path1 = j1.write()
        path2 = j2.write()

        assert path1 != path2, (
            "two journals started at the same second must not share a path"
        )
        assert path1.exists()
        assert path2.exists()

    def test_partial_journal_writes_what_we_have(self, tmp_path: Path) -> None:
        """A journal that wrote during a still-running phase (cycle
        crashed mid-way) should still produce a usable file rather than
        crashing on the un-ended phase."""
        j = _journal(tmp_path)
        j.start_phase("scan")  # never ended — process crashed mid-scan
        content = j.write().read_text()
        assert "scan" in content
        # Duration should render as "—" for the still-running phase
        assert "—" in content


class TestContextVarHooks:
    def test_record_provider_call_no_op_outside_cycle(self) -> None:
        """When no journal is set (outside `sentinel work`), recording
        a provider call must be a silent no-op rather than raising —
        otherwise unit tests on providers would crash."""
        set_current_journal(None)
        # Should not raise
        record_provider_call(provider="x", model="y", latency_ms=10)

    def test_record_provider_call_appends_to_current(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        set_current_journal(j)
        try:
            set_current_phase("scan")
            record_provider_call(
                provider="gemini", model="g", latency_ms=42,
                input_tokens=100, output_tokens=20, cost_usd=0.001,
            )
            assert len(j.provider_calls) == 1
            assert j.provider_calls[0].phase == "scan"
            assert j.provider_calls[0].latency_ms == 42
            assert j.provider_calls[0].cost_usd == 0.001
        finally:
            set_current_journal(None)

    def test_phase_contextvar_isolated_from_journal(self) -> None:
        """current_phase() returns the current phase regardless of
        whether a journal is set — useful for any sub-call that wants
        the phase as context."""
        set_current_phase("explore")
        assert current_phase() == "explore"
        set_current_phase("synthesize")
        assert current_phase() == "synthesize"

    def test_set_current_journal_to_none_clears(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        set_current_journal(j)
        assert current_journal() is j
        set_current_journal(None)
        assert current_journal() is None


class TestProviderHelperWiring:
    """The Provider._journal_call helper must record into the journal
    using the active phase. This is the integration point — if it
    breaks, all four providers stop appearing in the journal."""

    def test_provider_helper_records_with_active_phase(
        self, tmp_path: Path,
    ) -> None:
        from sentinel.providers.interface import (
            ChatResponse,
            Provider,
            ProviderCapabilities,
            ProviderName,
        )

        class FakeProvider(Provider):
            name = ProviderName.GEMINI
            cli_command = "fake"
            capabilities = ProviderCapabilities(chat=True)
            model = "fake-model"

            async def chat(self, prompt, system_prompt=None):  # noqa: ANN001, ANN201
                return ChatResponse(content="ok", provider=self.name)

            def detect(self):  # noqa: ANN201
                from sentinel.providers.interface import ProviderStatus
                return ProviderStatus(installed=True, authenticated=True)

        j = _journal(tmp_path)
        set_current_journal(j)
        try:
            set_current_phase("scan")
            import time
            started = time.perf_counter()
            response = ChatResponse(
                content="ok",
                model="gemini-2.5-flash",
                provider=ProviderName.GEMINI,
                input_tokens=120,
                output_tokens=30,
                cost_usd=0.0005,
            )
            FakeProvider()._journal_call(started, response, was_clamped=False)
            assert len(j.provider_calls) == 1
            call = j.provider_calls[0]
            assert call.phase == "scan"
            assert call.provider == "gemini"
            assert call.model == "gemini-2.5-flash"
            assert call.input_tokens == 120
            assert call.output_tokens == 30
            assert call.cost_usd == 0.0005
            assert call.latency_ms >= 0
            assert call.was_clamped is False
        finally:
            set_current_journal(None)

    def test_provider_helper_records_clamped_state_as_passed(
        self, tmp_path: Path,
    ) -> None:
        """was_clamped is captured by the provider BEFORE the call to
        avoid the race where elapsed time during the call shrinks the
        remaining budget. Verify the helper records exactly what was
        passed, not what it would compute now."""
        from sentinel.budget_ctx import set_cycle_deadline
        from sentinel.providers.interface import (
            ChatResponse,
            Provider,
            ProviderCapabilities,
            ProviderName,
        )

        class FakeProvider(Provider):
            name = ProviderName.GEMINI
            cli_command = "fake"
            capabilities = ProviderCapabilities(chat=True)
            timeout_sec = 600

            async def chat(self, prompt, system_prompt=None):  # noqa: ANN001, ANN201
                return ChatResponse(content="ok", provider=self.name)

            def detect(self):  # noqa: ANN201
                from sentinel.providers.interface import ProviderStatus
                return ProviderStatus(installed=True, authenticated=True)

        j = _journal(tmp_path)
        set_current_journal(j)
        # Simulate: budget is wide open, so the call wasn't clamped at
        # entry. If we computed `was_clamped` after the call returned,
        # a brief delay could shrink the remaining budget below 600s
        # and we'd misreport. Helper must use the value the caller
        # captured at the start.
        set_cycle_deadline(None)
        try:
            import time
            FakeProvider()._journal_call(
                time.perf_counter(),
                ChatResponse(content="ok", provider=ProviderName.GEMINI),
                was_clamped=False,
            )
            assert j.provider_calls[0].was_clamped is False
        finally:
            set_current_journal(None)
            set_cycle_deadline(None)


class TestUnusedSymbolsExist:
    """Lock in the exported names so future refactors don't silently
    break callers (Reviewer pass that uses WorkItemRecord, etc.)."""

    def test_work_item_record_constructs(self) -> None:
        wi = WorkItemRecord(work_item_id="wi-1", title="Test")
        assert wi.coder_status == "pending"
        assert wi.reviewer_verdict is None

    def test_phase_record_duration_handles_open_phase(self) -> None:
        p = PhaseRecord(name="x", started_at=100.0)
        assert p.duration_s is None
        p.ended_at = 105.0
        assert p.duration_s == 5.0
