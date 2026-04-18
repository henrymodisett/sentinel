"""Tests for the Cortex T1.6 integration (sentinel -> .cortex/journal/).

Each test corresponds to one or more of the 12 Success Criteria in
``autumn-garage/.cortex/plans/sentinel-cortex-t16-integration.md``.
Comments below flag the criterion each test covers so a reader can
cross-reference the plan.

The tests exercise the integration at three layers:

1. Pure unit tests on ``detect_cortex`` / ``render_cycle_journal_entry``
   / ``write_cortex_journal_entry`` — fastest, no CLI surface.
2. ``resolve_enabled`` precedence — the flag > config > auto-detect
   contract from the plan.
3. End-to-end ``sentinel work`` invocation with a mocked cycle,
   verifying the hook point wires everything through correctly
   (including the ``--cortex-journal`` / ``--no-cortex-journal`` flags
   and the ``.sentinel/state/cortex-write-errors.jsonl`` failure log).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path  # noqa: TC003 — runtime use for fs operations

import pytest

from sentinel.integrations.cortex import (
    CortexCycleData,
    cycle_id_from_run_path,
    cycle_journal_filename,
    detect_cortex,
    render_cycle_journal_entry,
    resolve_enabled,
    write_cortex_journal_entry,
)

# ---------- Helpers ----------


def _make_cycle_data(
    *,
    cycle_id: str = "2026-04-17-143000",
    exit_reason: str = "backlog_empty",
    lens_scores: list[tuple[str, int]] | None = None,
    overall_score: int | None = 72,
    work_items: list[tuple[str, str, str]] | None = None,
    pr_url: str = "",
    providers: list[tuple[str, str, str]] | None = None,
    project_dir: Path | None = None,
) -> CortexCycleData:
    """Build a CortexCycleData with sensible defaults for the renderer."""
    started = time.time()
    return CortexCycleData(
        cycle_id=cycle_id,
        started_at=started,
        ended_at=started + 42.0,
        project_name="test-project",
        branch="main",
        exit_reason=exit_reason,
        total_cost_usd=0.1234,
        total_provider_calls=7,
        lens_scores=lens_scores if lens_scores is not None else [
            ("privacy-guardian", 70),
            ("cli-integrator", 85),
        ],
        overall_score=overall_score,
        refinement_count=2,
        expansion_count=3,
        work_item_outcomes=work_items if work_items is not None else [],
        pr_url=pr_url,
        providers_by_role=providers if providers is not None else [
            ("monitor", "gemini", "gemini-2.5-flash"),
            ("coder", "claude", "claude-sonnet-4-6"),
            ("reviewer", "openai", "gpt-5.4"),
        ],
        run_journal_relpath=f".sentinel/runs/{cycle_id}.md",
    )


# ---------- detect_cortex ----------


class TestDetectCortex:
    def test_absent_when_no_cortex_dir(self, tmp_path: Path) -> None:
        presence = detect_cortex(tmp_path)
        assert presence.dir_present is False
        assert presence.journal_dir_writable is False

    def test_present_when_cortex_dir_exists(self, tmp_path: Path) -> None:
        (tmp_path / ".cortex").mkdir()
        presence = detect_cortex(tmp_path)
        assert presence.dir_present is True
        # .cortex/ exists and is writable by our process → journal_dir
        # creation will succeed, so we call it writable.
        assert presence.journal_dir_writable is True

    def test_read_only_journal_dir(self, tmp_path: Path) -> None:
        """Covers Criterion #5 partially — the detection layer flags a
        non-writable journal dir so the caller can warn without having
        to attempt the write first."""
        cortex = tmp_path / ".cortex"
        cortex.mkdir()
        journal = cortex / "journal"
        journal.mkdir()
        original = journal.stat().st_mode
        try:
            journal.chmod(0o500)  # r-x, no write
            presence = detect_cortex(tmp_path)
            assert presence.dir_present is True
            assert presence.journal_dir_writable is False
        finally:
            journal.chmod(original)


# ---------- resolve_enabled ----------


class TestResolveEnabled:
    """Covers precedence rules from the plan's Approach section:
    flag > config > auto-detect. Also Success Criteria #8 and #9."""

    def test_flag_true_wins_over_config_off(self) -> None:
        assert resolve_enabled(
            cli_flag=True, config_value="off", cortex_present=False,
        ) is True

    def test_flag_false_wins_over_config_on(self) -> None:
        assert resolve_enabled(
            cli_flag=False, config_value="on", cortex_present=True,
        ) is False

    def test_config_on_overrides_absent_dir(self) -> None:
        assert resolve_enabled(
            cli_flag=None, config_value="on", cortex_present=False,
        ) is True

    def test_config_off_overrides_present_dir(self) -> None:
        assert resolve_enabled(
            cli_flag=None, config_value="off", cortex_present=True,
        ) is False

    def test_config_auto_follows_presence_true(self) -> None:
        assert resolve_enabled(
            cli_flag=None, config_value="auto", cortex_present=True,
        ) is True

    def test_config_auto_follows_presence_false(self) -> None:
        assert resolve_enabled(
            cli_flag=None, config_value="auto", cortex_present=False,
        ) is False

    def test_missing_config_defaults_to_auto(self) -> None:
        # No config_value + present dir → write. Mirrors the "default
        # reflects .cortex/ presence" rule from the plan.
        assert resolve_enabled(
            cli_flag=None, config_value=None, cortex_present=True,
        ) is True

    def test_unknown_config_value_falls_back_to_auto(self) -> None:
        # Typos must not silently disable the integration project-wide
        # — a principle violation surfaced in sentinel's
        # engineering-principles ("no silent failures").
        assert resolve_enabled(
            cli_flag=None, config_value="mabye", cortex_present=True,
        ) is True


# ---------- cycle_journal_filename ----------


class TestFilenameConvention:
    def test_filename_uses_start_date_not_write_time(self) -> None:
        """Covers the plan's "No cross-tool timestamp coordination"
        known-limitation — filename prefix is derived from cycle start.
        """
        # 2026-04-17 23:59:50 local — one cycle that spans a day boundary
        ts = time.mktime(time.strptime("2026-04-17 23:59:50", "%Y-%m-%d %H:%M:%S"))
        fn = cycle_journal_filename("2026-04-17-235950", ts)
        assert fn == "2026-04-17-sentinel-cycle-2026-04-17-235950.md"

    def test_cycle_id_extracted_from_run_path(self, tmp_path: Path) -> None:
        """The cortex entry must cite the sentinel run by cycle-id so
        readers can join the two stores."""
        run = tmp_path / ".sentinel" / "runs" / "2026-04-17-143022.md"
        run.parent.mkdir(parents=True)
        run.write_text("# run")
        assert cycle_id_from_run_path(run) == "2026-04-17-143022"


# ---------- render_cycle_journal_entry ----------


class TestRender:
    """Covers Success Criterion #6 — all template fields populated
    from cycle data with no placeholder text.
    """

    def test_full_shape_has_required_fields(self) -> None:
        data = _make_cycle_data(
            work_items=[("1", "Refine docs", "succeeded-approved")],
            pr_url="https://github.com/org/repo/pull/42",
        )
        body = render_cycle_journal_entry(data)

        # Header per the plan's content shape
        assert body.startswith("# Sentinel cycle ")
        assert "**Trigger:** T1.6" in body
        assert "**Type:** sentinel-cycle" in body
        assert "**Date:**" in body
        assert "**Cites:** .sentinel/runs/" in body

        # Cycle summary block
        assert "privacy-guardian 70/100" in body
        assert "cli-integrator 85/100" in body
        assert "Health: 72/100" not in body  # distinct from ## Cycle summary
        assert "**Health:** 72/100" in body
        assert "2 refinements + 3 expansion proposals" in body
        assert "approved 1 of 1" in body
        assert "https://github.com/org/repo/pull/42" in body
        assert "$0.1234" in body

        # Providers de-duped by role
        assert "monitor=gemini/gemini-2.5-flash" in body
        assert "coder=claude/claude-sonnet-4-6" in body
        assert "reviewer=openai/gpt-5.4" in body

        # No unsubstituted template tokens
        assert "{" not in body or "}" not in body

    def test_dry_run_verdict(self) -> None:
        body = render_cycle_journal_entry(_make_cycle_data(
            exit_reason="dry_run", work_items=[],
        ))
        assert "**Verdict:** dry_run" in body
        assert "Re-run without `--dry-run`" in body

    def test_budget_exhausted_verdict(self) -> None:
        body = render_cycle_journal_entry(_make_cycle_data(
            exit_reason="budget: daily cap hit", work_items=[],
        ))
        assert "**Verdict:** budget-exhausted" in body
        assert "Raise budget" in body

    def test_failed_items_become_follow_ups(self) -> None:
        body = render_cycle_journal_entry(_make_cycle_data(
            work_items=[
                ("1", "Refactor loop", "failed"),
                ("2", "Ship docs", "succeeded-approved"),
            ],
        ))
        assert "Retry or investigate **1**: Refactor loop" in body
        # Succeeded item doesn't appear as a follow-up
        assert "Retry or investigate **2**" not in body

    def test_no_scan_this_cycle_renders_gracefully(self) -> None:
        body = render_cycle_journal_entry(_make_cycle_data(
            lens_scores=[], overall_score=None,
        ))
        assert "(no scan this cycle)" in body


# ---------- write_cortex_journal_entry ----------


class TestWrite:
    def test_writes_file_when_cortex_present(self, tmp_path: Path) -> None:
        """Covers Success Criterion #1 — write happens when .cortex/
        is present at the repo root."""
        (tmp_path / ".cortex").mkdir()
        data = _make_cycle_data()

        result = write_cortex_journal_entry(tmp_path, data)

        assert result.status == "written"
        assert result.path is not None
        assert result.path.exists()
        content = result.path.read_text()
        assert "**Trigger:** T1.6" in content

    def test_skip_silently_when_no_cortex(self, tmp_path: Path) -> None:
        """Covers Success Criterion #4 — no write, no warning, no
        orphan files when .cortex/ is absent."""
        data = _make_cycle_data()
        result = write_cortex_journal_entry(tmp_path, data)

        assert result.status == "skipped_no_cortex"
        assert result.path is None
        assert result.warning == ""
        # No .cortex/ scaffolded as a side-effect
        assert not (tmp_path / ".cortex").exists()

    def test_dedup_skips_existing_entry(self, tmp_path: Path) -> None:
        """Covers Success Criterion #7 — same cycle-id twice → second
        call skips with warning, original file untouched."""
        (tmp_path / ".cortex").mkdir()
        data = _make_cycle_data()

        first = write_cortex_journal_entry(tmp_path, data)
        assert first.status == "written"
        assert first.path is not None
        original_mtime = first.path.stat().st_mtime
        original_content = first.path.read_text()

        # Sleep a hair so mtime would differ if we did overwrite
        time.sleep(0.01)
        second = write_cortex_journal_entry(tmp_path, data)

        assert second.status == "skipped_existing"
        assert "already exists" in second.warning
        # Append-only invariant: file unchanged
        assert first.path.read_text() == original_content
        assert first.path.stat().st_mtime == original_mtime

    def test_read_only_journal_dir_logs_and_warns(
        self, tmp_path: Path,
    ) -> None:
        """Covers Success Criterion #5 — read-only .cortex/journal/:
        warning surfaced, failure logged to
        .sentinel/state/cortex-write-errors.jsonl, cycle NOT failed.
        """
        journal = tmp_path / ".cortex" / "journal"
        journal.mkdir(parents=True)
        original_mode = journal.stat().st_mode

        if os.geteuid() == 0:
            pytest.skip("root bypasses rwx permissions; test n/a")

        data = _make_cycle_data()
        try:
            journal.chmod(0o500)  # no write
            result = write_cortex_journal_entry(tmp_path, data)
        finally:
            journal.chmod(original_mode)

        assert result.status == "failed"
        assert result.warning
        assert "write failed" in result.warning.lower()

        log_path = tmp_path / ".sentinel" / "state" / "cortex-write-errors.jsonl"
        assert log_path.exists(), "Failure log must be appended"
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["cycle_id"] == data.cycle_id
        assert record["error_class"]
        assert record["error_message"]
        # ISO-8601 UTC (either trailing Z or +00:00 form — both valid)
        assert "T" in record["timestamp"]
        assert record["timestamp"].endswith(("Z", "+00:00"))

    def test_failure_log_appends_on_repeat(self, tmp_path: Path) -> None:
        """Invariant: the failure log is append-only (like Cortex's own
        Journal). Two failures → two lines, not overwrite."""
        journal = tmp_path / ".cortex" / "journal"
        journal.mkdir(parents=True)
        original_mode = journal.stat().st_mode

        if os.geteuid() == 0:
            pytest.skip("root bypasses rwx permissions; test n/a")

        try:
            journal.chmod(0o500)
            write_cortex_journal_entry(
                tmp_path, _make_cycle_data(cycle_id="cycle-a"),
            )
            write_cortex_journal_entry(
                tmp_path, _make_cycle_data(cycle_id="cycle-b"),
            )
        finally:
            journal.chmod(original_mode)

        log_path = tmp_path / ".sentinel" / "state" / "cortex-write-errors.jsonl"
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 2
        ids = {json.loads(ln)["cycle_id"] for ln in lines}
        assert ids == {"cycle-a", "cycle-b"}

    def test_force_writes_even_without_cortex_dir(self, tmp_path: Path) -> None:
        """The CLI uses `force=True` when --cortex-journal is passed
        and .cortex/ is absent — scaffolds journal dir and writes.
        Covers Success Criterion #8 (flag overrides detection)."""
        data = _make_cycle_data()
        result = write_cortex_journal_entry(tmp_path, data, force=True)

        assert result.status == "written"
        assert result.path is not None
        assert result.path.exists()
        assert (tmp_path / ".cortex" / "journal").is_dir()

    def test_atomic_write_leaves_no_tmp_on_success(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / ".cortex").mkdir()
        write_cortex_journal_entry(tmp_path, _make_cycle_data())
        tmps = list((tmp_path / ".cortex" / "journal").glob("*.tmp"))
        assert tmps == []


# ---------- Hook-point integration (direct, no CLI subprocess) ----------


class TestHookPointIntegration:
    """Drive the ``_emit_cortex_t16_entry`` hook directly with a
    minimal fake Journal, so the flag/config/auto-detect precedence
    tests don't hang on real provider-CLI detection inside
    ``sentinel work``. Still exercises the real config-loading path
    and real write_cortex_journal_entry.

    Covers the same Success Criteria as a full end-to-end CliRunner
    invocation would (#1, #4, #5, #8, #9), at a fraction of the cost.
    The full ``sentinel work`` path is exercised by the dogfood gate
    (Success Criterion #12) which is manual-verification.
    """

    def _minimal_config(
        self, project: Path, cortex_enabled: str = "auto",
    ):
        """Build a SentinelConfig via the same loader sentinel work uses."""
        (project / ".sentinel").mkdir(exist_ok=True)
        (project / ".sentinel" / "config.toml").write_text(
            f"""
[project]
name = "t"
path = "{project}"

[budget]
daily_limit_usd = 15.0
warn_at_usd = 12.0

[roles.monitor]
provider = "claude"
model = "claude-sonnet-4-6"

[roles.researcher]
provider = "claude"
model = "claude-sonnet-4-6"

[roles.planner]
provider = "claude"
model = "claude-sonnet-4-6"

[roles.coder]
provider = "claude"
model = "claude-sonnet-4-6"

[roles.reviewer]
provider = "openai"
model = "gpt-5.4"

[integrations.cortex]
enabled = "{cortex_enabled}"
""".strip()
        )
        # Load via the same code path sentinel work uses so a schema
        # regression on the integrations section would surface here.
        import tomllib

        from sentinel.config.schema import SentinelConfig
        data = tomllib.loads(
            (project / ".sentinel" / "config.toml").read_text(),
        )
        return SentinelConfig(**data)

    def _fake_journal(self, project: Path):
        """Minimal duck-typed journal. The hook only reads attributes
        exposed by the real Journal dataclass, so a simple namespace
        with the right fields is sufficient."""
        from types import SimpleNamespace

        run_path = project / ".sentinel" / "runs" / "2026-04-17-143022.md"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text("# fake run")

        started = time.time()
        return SimpleNamespace(
            project_name=project.name,
            branch="main",
            started_at=started,
            ended_at=started + 10.0,
            exit_reason="backlog_empty",
            provider_calls=[],
            work_items=[],
            _resolved_path=run_path,
        ), run_path

    def _latest_cortex_entry(self, project: Path):
        journal_dir = project / ".cortex" / "journal"
        if not journal_dir.is_dir():
            return None
        entries = sorted(journal_dir.glob("*-sentinel-cycle-*.md"))
        return entries[-1] if entries else None

    def _emit(
        self,
        project: Path,
        *,
        cli_flag: bool | None,
        config,
    ) -> None:
        from sentinel.cli.work_cmd import _emit_cortex_t16_entry
        journal, run_path = self._fake_journal(project)
        _emit_cortex_t16_entry(
            project, journal, run_path,
            cli_flag=cli_flag,
            config=config,
            overall_score=72,
            lens_scores=[("privacy-guardian", 70)],
            refinement_count=2,
            expansion_count=3,
        )

    def test_cortex_present_and_default_writes(self, tmp_path: Path) -> None:
        """Success Criterion #1."""
        config = self._minimal_config(tmp_path)
        (tmp_path / ".cortex").mkdir()

        self._emit(tmp_path, cli_flag=None, config=config)

        entry = self._latest_cortex_entry(tmp_path)
        assert entry is not None
        body = entry.read_text()
        assert "**Trigger:** T1.6" in body

    def test_no_cortex_dir_skips_silently(self, tmp_path: Path) -> None:
        """Success Criterion #4 — no write, no orphan files."""
        config = self._minimal_config(tmp_path)

        self._emit(tmp_path, cli_flag=None, config=config)

        assert not (tmp_path / ".cortex").exists()

    def test_no_cortex_journal_flag_disables_write(
        self, tmp_path: Path,
    ) -> None:
        """Success Criterion #8 — `--no-cortex-journal` overrides
        detection even when `.cortex/` is present."""
        config = self._minimal_config(tmp_path)
        (tmp_path / ".cortex").mkdir()

        self._emit(tmp_path, cli_flag=False, config=config)

        assert self._latest_cortex_entry(tmp_path) is None

    def test_cortex_journal_flag_forces_write_with_config_off(
        self, tmp_path: Path,
    ) -> None:
        """Success Criterion #8 + #9 — flag beats config."""
        config = self._minimal_config(tmp_path, cortex_enabled="off")
        (tmp_path / ".cortex").mkdir()

        self._emit(tmp_path, cli_flag=True, config=config)

        assert self._latest_cortex_entry(tmp_path) is not None

    def test_config_off_disables_write(self, tmp_path: Path) -> None:
        """Success Criterion #9."""
        config = self._minimal_config(tmp_path, cortex_enabled="off")
        (tmp_path / ".cortex").mkdir()

        self._emit(tmp_path, cli_flag=None, config=config)

        assert self._latest_cortex_entry(tmp_path) is None

    def test_read_only_journal_dir_logs_and_cycle_survives(
        self, tmp_path: Path,
    ) -> None:
        """Success Criterion #5 — read-only dir: warning surfaced via
        stderr (captured by the hook's console), error logged,
        no exception raised out of the hook."""
        if os.geteuid() == 0:
            pytest.skip("root bypasses rwx permissions; test n/a")

        config = self._minimal_config(tmp_path)
        journal_dir = tmp_path / ".cortex" / "journal"
        journal_dir.mkdir(parents=True)
        original_mode = journal_dir.stat().st_mode
        try:
            journal_dir.chmod(0o500)
            # Must NOT raise — non-blocking-failure contract.
            self._emit(tmp_path, cli_flag=None, config=config)
        finally:
            journal_dir.chmod(original_mode)

        log_path = tmp_path / ".sentinel" / "state" / "cortex-write-errors.jsonl"
        assert log_path.exists()
        assert log_path.read_text().strip()


# ---------- Schema regression guard ----------


class TestConfigSchema:
    """Success Criterion #9 + #11 — config accepts the new integrations
    section without breaking other tests."""

    def test_integrations_cortex_enabled_accepts_auto_on_off(self) -> None:
        import tomllib

        from sentinel.config.schema import SentinelConfig

        base = """
[project]
name = "t"
path = "/tmp"

[roles.monitor]
provider = "claude"
model = "m"

[roles.researcher]
provider = "claude"
model = "m"

[roles.planner]
provider = "claude"
model = "m"

[roles.coder]
provider = "claude"
model = "m"

[roles.reviewer]
provider = "openai"
model = "m"
"""
        for value in ("auto", "on", "off"):
            data = tomllib.loads(
                base + f'\n[integrations.cortex]\nenabled = "{value}"\n',
            )
            config = SentinelConfig(**data)
            assert config.integrations.cortex.enabled == value

    def test_config_without_integrations_section_defaults_to_auto(
        self,
    ) -> None:
        import tomllib

        from sentinel.config.schema import SentinelConfig

        base = """
[project]
name = "t"
path = "/tmp"

[roles.monitor]
provider = "claude"
model = "m"
[roles.researcher]
provider = "claude"
model = "m"
[roles.planner]
provider = "claude"
model = "m"
[roles.coder]
provider = "claude"
model = "m"
[roles.reviewer]
provider = "openai"
model = "m"
"""
        config = SentinelConfig(**tomllib.loads(base))
        # Regression: old configs (no integrations section) load cleanly.
        assert config.integrations.cortex.enabled == "auto"


# ---------- Integration test (gated on real cortex CLI) ----------


@pytest.mark.skipif(
    not any(
        Path(p).is_file() and os.access(p, os.X_OK)
        for p in (
            Path("/opt/homebrew/bin/cortex"),
            Path("/usr/local/bin/cortex"),
            *(Path(d) / "cortex" for d in os.environ.get("PATH", "").split(os.pathsep)),
        )
    ),
    reason="real `cortex` CLI not installed; integration test deferred",
)
class TestCortexDoctorIntegration:
    """Slower integration test — requires the real `cortex` CLI on
    PATH. Gated skip per the plan: when the CLI is installed, run
    `cortex doctor` against a produced entry and assert it exits 0.
    Covers Success Criterion #2.
    """

    def test_produced_entry_passes_cortex_doctor(
        self, tmp_path: Path,
    ) -> None:
        import subprocess

        # Scaffold a minimal .cortex/ via the real CLI.
        init = subprocess.run(
            ["cortex", "init"], cwd=tmp_path, capture_output=True, text=True,
        )
        if init.returncode != 0:
            pytest.skip(f"cortex init failed: {init.stderr}")

        # Write a conformant cycle entry via our renderer.
        data = _make_cycle_data(
            work_items=[("1", "Test work item", "succeeded-approved")],
            project_dir=tmp_path,
        )
        result = write_cortex_journal_entry(tmp_path, data)
        assert result.status == "written"

        # Run cortex doctor — should accept the entry.
        doctor = subprocess.run(
            ["cortex", "doctor"], cwd=tmp_path, capture_output=True, text=True,
        )
        assert doctor.returncode == 0, (
            f"cortex doctor rejected our entry.\n"
            f"stdout: {doctor.stdout}\n"
            f"stderr: {doctor.stderr}\n"
            f"entry: {result.path.read_text() if result.path else '(no path)'}"
        )
