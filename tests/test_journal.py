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
import time
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
            cost_usd=0.0021,
        ))
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="gemini-2.5-pro",
            latency_ms=31200, input_tokens=12500, output_tokens=2100,
            cost_usd=0.0022,
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
        assert lines[0]["model"] == "gemini-2.5-flash"
        assert lines[1]["model"] == "gemini-2.5-pro"

    def test_totals_in_header(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="m",
            latency_ms=100, cost_usd=0.01,
        ))
        j.record_provider_call(ProviderCall(
            phase="scan", provider="claude", model="m",
            latency_ms=200, cost_usd=0.02,
        ))
        content = j.write().read_text()
        assert "$0.0300" in content
        assert "Provider calls:** 2" in content
        # No skipped calls in this fixture — header should reflect zero.
        assert "(0 skipped — budget exhausted)" in content

    def test_repeated_write_reuses_same_path(self, tmp_path: Path) -> None:
        """Journal.write() is idempotent — incremental writes during a
        cycle must land on the same file, not generate new collision-
        suffixed names on every call."""
        j = _journal(tmp_path)
        first = j.write()
        second = j.write()
        third = j.write()
        assert first == second == third

    def test_checkpoints_on_phase_start(self, tmp_path: Path) -> None:
        """A phase transition must leave an up-to-date file on disk
        BEFORE the phase body runs. Without this, a cycle that hangs
        inside the phase produces no journal when killed externally."""
        j = _journal(tmp_path)
        j.start_phase("scan")

        runs_dir = tmp_path / ".sentinel" / "runs"
        files = list(runs_dir.glob("*.md"))
        assert len(files) == 1, (
            "start_phase must checkpoint so a killed cycle leaves evidence"
        )
        content = files[0].read_text()
        assert "scan" in content

    def test_checkpoints_on_provider_call_record(self, tmp_path: Path) -> None:
        """Every recorded provider call updates the on-disk journal.
        If the cycle dies mid-phase, the successful calls already
        captured are still visible in the file."""
        j = _journal(tmp_path)
        j.start_phase("scan")
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="flash",
            latency_ms=100, cost_usd=0.001,
        ))

        runs_dir = tmp_path / ".sentinel" / "runs"
        files = list(runs_dir.glob("*.md"))
        content = files[0].read_text()
        assert "gemini" in content
        assert "flash" in content

    def test_checkpoints_on_work_item_record(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.record_work_item(WorkItemRecord(
            work_item_id="wi-1", title="Test", coder_status="succeeded",
        ))

        runs_dir = tmp_path / ".sentinel" / "runs"
        files = list(runs_dir.glob("*.md"))
        content = files[0].read_text()
        assert "wi-1" in content
        assert "succeeded" in content

    def test_checkpoints_do_not_freeze_total_time(
        self, tmp_path: Path,
    ) -> None:
        """Regression: write() used to set ended_at on first call, so
        every checkpoint after that rendered a stale ~0s Total time
        (frozen at the first start_phase). Now write() leaves ended_at
        alone; mark_ended is explicit and only called before the final
        write. A long-running cycle's checkpoints must reflect
        accumulating elapsed time, not a frozen zero."""
        j = _journal(tmp_path)
        # Simulate a cycle that started 5 seconds ago
        j.started_at = time.time() - 5
        j.start_phase("scan")

        runs_dir = tmp_path / ".sentinel" / "runs"
        files = list(runs_dir.glob("*.md"))
        content = files[0].read_text()

        # Parse the "Total time: X.Xs" line — must reflect ~5 seconds,
        # not the ~0s that the frozen ended_at bug produced.
        match = re.search(r"Total time:\*\* ([\d.]+)s", content)
        assert match is not None, f"could not find Total time in:\n{content}"
        total = float(match.group(1))
        assert 4.5 <= total <= 10, (
            f"checkpoint froze Total time at {total}s; expected ~5s "
            f"(cycle started 5s ago, still running)"
        )

    def test_mark_ended_freezes_total_time(self, tmp_path: Path) -> None:
        """After mark_ended, subsequent writes must NOT advance
        Total time — that's the point of the explicit end signal."""
        j = _journal(tmp_path)
        j.started_at = time.time() - 3
        j.mark_ended()
        frozen = j.ended_at
        assert frozen is not None

        # Wait a moment; another write() must still use the frozen ts
        time.sleep(0.1)
        j.write()
        assert j.ended_at == frozen, (
            "mark_ended must be a one-shot freeze; further writes cannot "
            "advance the end timestamp"
        )

    def test_failed_write_preserves_previous_checkpoint(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Atomic-replace guarantee: if a checkpoint write fails after
        the first good checkpoint has landed, the previous file stays
        intact. Rewriting the live file with write_text() would have
        left a truncated/empty journal on a mid-write crash."""
        j = _journal(tmp_path)
        j.start_phase("scan")
        # First checkpoint landed — read it
        runs_dir = tmp_path / ".sentinel" / "runs"
        original_files = list(runs_dir.glob("*.md"))
        assert len(original_files) == 1
        original_content = original_files[0].read_text()
        assert "scan" in original_content

        # Now sabotage the next write to fail mid-way — simulate
        # write_text raising OSError (disk full, permission, etc.)
        original_write_text = Path.write_text

        def failing_write_text(self, *args, **kwargs):  # noqa: ANN001, ANN202
            if self.suffix == ".tmp":
                raise OSError("simulated disk full during checkpoint")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", failing_write_text)

        # Next mutation — checkpoint tries to write, fails, swallows
        j.start_phase("plan")

        # The ORIGINAL journal file must still be intact. A non-atomic
        # write would have left it truncated or empty.
        final_content = original_files[0].read_text()
        assert final_content == original_content, (
            "failed atomic write clobbered the previous checkpoint"
        )

    def test_checkpoint_failure_does_not_crash(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """If the checkpoint write fails (full disk, permission error,
        etc.), the method must not propagate the exception. The cycle
        keeps running; the final finally-block write gets another try."""
        j = _journal(tmp_path)

        def fail_write() -> Path:
            raise OSError("simulated disk full")

        monkeypatch.setattr(j, "write", fail_write)
        # Should not raise
        j.start_phase("scan")
        j.record_provider_call(ProviderCall(
            phase="scan", provider="x", model="y",
            latency_ms=1,
        ))

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

    def test_stderr_renders_in_journal_for_failed_calls(
        self, tmp_path: Path,
    ) -> None:
        """When a provider exits non-zero (or times out), the captured
        stderr must appear in the journal so the next reproduction is
        diagnosable from the file alone — no live re-run required."""
        j = _journal(tmp_path)
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="gemini-2.5-pro",
            latency_ms=222000,
            error="non-zero exit",
            stderr="Error: API quota exceeded for project ABC123",
        ))
        content = j.write().read_text()
        assert "## Provider errors" in content
        assert "API quota exceeded for project ABC123" in content
        assert "scan — gemini/gemini-2.5-pro (non-zero exit)" in content

    def test_stderr_omitted_when_call_succeeded(self, tmp_path: Path) -> None:
        """Successful calls don't get a stderr block — the journal stays
        lean. Stderr is a debugging aid for failures, not a transcript."""
        j = _journal(tmp_path)
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="flash",
            latency_ms=1000,
            stderr="some warning the CLI emitted to stderr",
        ))
        content = j.write().read_text()
        assert "## Provider errors" not in content
        assert "some warning" not in content

    def test_role_attribution_renders_by_role_table(
        self, tmp_path: Path,
    ) -> None:
        """When provider calls carry a role tag, the journal shows a
        per-role breakdown so the user can answer 'where is my money
        going?' without manually grouping the JSONL appendix."""
        j = _journal(tmp_path)
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="flash",
            latency_ms=100, cost_usd=0.01, role="monitor",
            input_tokens=1000, output_tokens=200,
        ))
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="pro",
            latency_ms=300, cost_usd=0.03, role="researcher",
            input_tokens=500, output_tokens=100,
        ))
        j.record_provider_call(ProviderCall(
            phase="execute", provider="claude", model="sonnet",
            latency_ms=2000, cost_usd=0.10, role="coder",
            input_tokens=5000, output_tokens=2000,
        ))
        content = j.write().read_text()
        assert "## By role" in content
        assert "| coder |" in content
        assert "| monitor |" in content
        assert "| researcher |" in content
        # Cost columns formatted to 4 decimal places
        assert "$0.0100" in content  # monitor
        assert "$0.0300" in content  # researcher
        assert "$0.1000" in content  # coder

    def test_role_table_omitted_when_no_calls_have_role(
        self, tmp_path: Path,
    ) -> None:
        """Backwards compatibility: cycles run before the role-tagging
        change (or test/ad-hoc invocations) leave role blank — no table
        should appear in that case rather than rendering a useless
        empty section."""
        j = _journal(tmp_path)
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="flash",
            latency_ms=100, cost_usd=0.01,
        ))
        content = j.write().read_text()
        assert "## By role" not in content

    def test_role_jsonl_includes_role_when_tagged(
        self, tmp_path: Path,
    ) -> None:
        """The JSONL appendix carries role inline so downstream tooling
        can group without re-reading the table. Role is omitted from
        JSONL entries when blank to keep the format clean."""
        j = _journal(tmp_path)
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="flash",
            latency_ms=100, cost_usd=0.01, role="monitor",
        ))
        j.record_provider_call(ProviderCall(
            phase="scan", provider="gemini", model="flash",
            latency_ms=100, cost_usd=0.01,  # no role
        ))
        content = j.write().read_text()
        block = re.search(r"```jsonl\n(.*?)\n```", content, re.DOTALL)
        assert block is not None
        lines = [json.loads(ln) for ln in block.group(1).splitlines() if ln.strip()]
        assert lines[0]["role"] == "monitor"
        assert "role" not in lines[1]

    def test_stderr_truncated_at_render_with_byte_count(
        self, tmp_path: Path,
    ) -> None:
        """Long stderr is truncated in the rendered markdown so the
        on-disk journal stays a sane size, but the truncation message
        names the dropped byte count so a reader knows there's more."""
        huge = "x" * 5000
        j = _journal(tmp_path)
        j.record_provider_call(ProviderCall(
            phase="scan", provider="claude", model="opus",
            latency_ms=300,
            error="non-zero exit",
            stderr=huge,
        ))
        content = j.write().read_text()
        assert "[truncated" in content
        # Original payload is preserved on the in-memory record even
        # though the rendered markdown is truncated.
        assert j.provider_calls[0].stderr == huge


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
            FakeProvider()._journal_call(started, response)
            assert len(j.provider_calls) == 1
            call = j.provider_calls[0]
            assert call.phase == "scan"
            assert call.provider == "gemini"
            assert call.model == "gemini-2.5-flash"
            assert call.input_tokens == 120
            assert call.output_tokens == 30
            assert call.cost_usd == 0.0005
            assert call.latency_ms >= 0
        finally:
            set_current_journal(None)


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
