"""Tests for sentinel.integrations.cortex_read - fetch_manifest and cortex_fence.

Covers:
- Happy path: subprocess exits 0, stdout returned.
- Missing binary: shutil.which returns None, returns None, warns once.
- Missing .cortex/ dir: pre-check returns None before subprocess is called.
- Timeout: subprocess.TimeoutExpired raised, returns None.
- Non-zero exit: returncode != 0, returns None.
- Budget flag: --budget value passed through to subprocess argv.
- Warning logged exactly once across multiple misses.
- cortex_fence helper: wraps non-empty context; returns "" for None/empty.
- Cycle integration: monitor.assess system prompt contains fence when
  cortex_context is provided; omits fence when None.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sentinel.integrations.cortex_read as cortex_read_mod
from sentinel.integrations.cortex_read import cortex_fence, fetch_manifest, reset_warned


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset module-level caches + warned flag before each test."""
    reset_warned()
    yield
    reset_warned()


# ---------- cortex_fence ----------


def test_cortex_fence_wraps_content():
    result = cortex_fence("hello world")
    assert result.startswith("<cortex-context>\n")
    assert "hello world" in result
    assert result.endswith("</cortex-context>\n\n")


def test_cortex_fence_none_returns_empty():
    assert cortex_fence(None) == ""


def test_cortex_fence_empty_string_returns_empty():
    assert cortex_fence("") == ""


# ---------- fetch_manifest - happy path ----------


def test_fetch_manifest_happy_path(tmp_path):
    (tmp_path / ".cortex").mkdir()
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "# Cortex Manifest\n\nsome state"
    with (
        patch.object(cortex_read_mod, "_cortex_bin", "/usr/bin/cortex"),
        patch.object(cortex_read_mod, "_cortex_bin_resolved", True),
        patch("subprocess.run", return_value=proc) as mock_run,
    ):
        result = fetch_manifest(tmp_path)

    assert result == "# Cortex Manifest\n\nsome state"
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert "--budget" in cmd
    assert "--path" in cmd


def test_fetch_manifest_happy_path_preserves_stdout(tmp_path):
    (tmp_path / ".cortex").mkdir()
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "\n\n# Manifest\n\n"
    with (
        patch.object(cortex_read_mod, "_cortex_bin", "/usr/bin/cortex"),
        patch.object(cortex_read_mod, "_cortex_bin_resolved", True),
        patch("subprocess.run", return_value=proc),
    ):
        result = fetch_manifest(tmp_path)

    assert result == "\n\n# Manifest\n\n"


# ---------- fetch_manifest - missing binary ----------


def test_fetch_manifest_missing_binary(tmp_path, caplog):
    (tmp_path / ".cortex").mkdir()
    with (
        patch("shutil.which", return_value=None),
        caplog.at_level(logging.WARNING, logger="sentinel.integrations.cortex_read"),
    ):
        result = fetch_manifest(tmp_path)

    assert result is None
    assert any("not on PATH" in r.message for r in caplog.records)


# ---------- fetch_manifest - missing .cortex ----------


def test_fetch_manifest_missing_dotcortex(tmp_path, caplog):
    # tmp_path has no .cortex/ subdirectory
    with (
        patch.object(cortex_read_mod, "_cortex_bin", "/usr/bin/cortex"),
        patch.object(cortex_read_mod, "_cortex_bin_resolved", True),
        patch("subprocess.run") as mock_run,
        caplog.at_level(logging.WARNING, logger="sentinel.integrations.cortex_read"),
    ):
        result = fetch_manifest(tmp_path)

    assert result is None
    mock_run.assert_not_called()
    assert any(".cortex/" in r.message for r in caplog.records)


# ---------- fetch_manifest - timeout ----------


def test_fetch_manifest_timeout(tmp_path, caplog):
    (tmp_path / ".cortex").mkdir()
    with (
        patch.object(cortex_read_mod, "_cortex_bin", "/usr/bin/cortex"),
        patch.object(cortex_read_mod, "_cortex_bin_resolved", True),
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="cortex", timeout=30)),
        caplog.at_level(logging.WARNING, logger="sentinel.integrations.cortex_read"),
    ):
        result = fetch_manifest(tmp_path)

    assert result is None
    assert any("timed out" in r.message for r in caplog.records)


# ---------- fetch_manifest - non-zero exit ----------


def test_fetch_manifest_non_zero_exit(tmp_path, caplog):
    (tmp_path / ".cortex").mkdir()
    proc = MagicMock()
    proc.returncode = 1
    proc.stderr = "some error from cortex"
    with (
        patch.object(cortex_read_mod, "_cortex_bin", "/usr/bin/cortex"),
        patch.object(cortex_read_mod, "_cortex_bin_resolved", True),
        patch("subprocess.run", return_value=proc),
        caplog.at_level(logging.WARNING, logger="sentinel.integrations.cortex_read"),
    ):
        result = fetch_manifest(tmp_path)

    assert result is None
    assert any("exited 1" in r.message for r in caplog.records)


# ---------- fetch_manifest - budget passed through ----------


def test_fetch_manifest_budget_passed_through(tmp_path):
    (tmp_path / ".cortex").mkdir()
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "manifest content"
    with (
        patch.object(cortex_read_mod, "_cortex_bin", "/usr/bin/cortex"),
        patch.object(cortex_read_mod, "_cortex_bin_resolved", True),
        patch("subprocess.run", return_value=proc) as mock_run,
    ):
        fetch_manifest(tmp_path, budget=4000)

    cmd = mock_run.call_args[0][0]
    budget_idx = cmd.index("--budget")
    assert cmd[budget_idx + 1] == "4000"


def test_fetch_manifest_default_budget_is_6000(tmp_path):
    (tmp_path / ".cortex").mkdir()
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "manifest content"
    with (
        patch.object(cortex_read_mod, "_cortex_bin", "/usr/bin/cortex"),
        patch.object(cortex_read_mod, "_cortex_bin_resolved", True),
        patch("subprocess.run", return_value=proc) as mock_run,
    ):
        fetch_manifest(tmp_path)

    cmd = mock_run.call_args[0][0]
    budget_idx = cmd.index("--budget")
    assert cmd[budget_idx + 1] == "6000"


# ---------- fetch_manifest - warning once ----------


def test_fetch_manifest_warning_logged_once(tmp_path, caplog):
    """Three consecutive misses produce exactly one warning."""
    with (
        patch("shutil.which", return_value=None),
        caplog.at_level(logging.WARNING, logger="sentinel.integrations.cortex_read"),
    ):
        fetch_manifest(tmp_path)
        fetch_manifest(tmp_path)
        fetch_manifest(tmp_path)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1


# ---------- cortex_fence integration with monitor ----------


@pytest.mark.asyncio
async def test_monitor_assess_includes_cortex_fence(tmp_path):
    """When cortex_context is provided, it appears in the prompt the
    provider receives for the explore step."""
    from sentinel.roles.monitor import Monitor

    captured_prompts: list[str] = []

    # Build a minimal fake provider
    fake_response = MagicMock()
    fake_response.model = "test"
    fake_response.provider = "test"
    fake_response.input_tokens = 0
    fake_response.output_tokens = 0
    fake_response.cost_usd = 0.0
    fake_response.content = "summary text"

    fake_parsed = {
        "project_summary": "test project",
        "lenses": [
            {
                "name": "test-lens",
                "description": "a test lens",
                "what_to_look_for": "nothing",
                "questions": ["q1"],
            }
        ],
    }

    class FakeProvider:
        name = "fake"
        timeout_sec = 60
        capabilities = MagicMock(agentic_code=False)

        async def chat(self, prompt, **kwargs):
            captured_prompts.append(prompt)
            return fake_response

        async def chat_json(self, prompt, schema, **kwargs):
            captured_prompts.append(prompt)
            return fake_parsed, fake_response

    fake_router = MagicMock()
    fake_router.get_provider.return_value = FakeProvider()

    from sentinel.state import ProjectState

    state = ProjectState(
        name="test",
        path=str(tmp_path),
        claude_md="",
        readme="",
        recent_commits="",
        file_tree="",
        branch="main",
        uncommitted_files="",
        test_output="",
        lint_output="",
    )

    monitor = Monitor(fake_router)

    # Patch researcher so domain_brief doesn't fail
    with patch("sentinel.roles.researcher.Researcher") as mock_researcher_cls:
        mock_researcher = MagicMock()
        mock_brief = MagicMock()
        mock_brief.synthesis = ""
        mock_brief.cost_usd = 0.0

        mock_researcher.domain_brief = AsyncMock(return_value=mock_brief)
        mock_researcher_cls.return_value = mock_researcher

        # Also patch the synthesize call to avoid a second chat_json
        with patch.object(FakeProvider, "chat_json") as mock_cj:
            synth_parsed = {
                "overall_score": 70,
                "summary": "ok",
                "strengths": [],
                "critical_risks": [],
                "top_actions": [
                    {
                        "title": "Fix it",
                        "why": "because",
                        "impact": "high",
                        "lens": "test-lens",
                        "kind": "refine",
                        "acceptance_criteria": ["tests pass"],
                        "verification": ["pytest"],
                        "out_of_scope": [],
                    }
                ],
            }
            synth_resp = MagicMock()
            synth_resp.model = "test"
            synth_resp.provider = "test"
            synth_resp.input_tokens = 0
            synth_resp.output_tokens = 0
            synth_resp.cost_usd = 0.0

            call_count = 0

            async def side_effect(prompt, schema, **kwargs):
                nonlocal call_count
                captured_prompts.append(prompt)
                call_count += 1
                if call_count == 1:
                    return fake_parsed, fake_response
                return synth_parsed, synth_resp

            mock_cj.side_effect = side_effect

            await monitor.assess(state, cortex_context="project-context-here")

    mock_researcher.domain_brief.assert_awaited_once()
    assert (
        mock_researcher.domain_brief.await_args.kwargs["cortex_context"] == "project-context-here"
    )
    assert any("<cortex-context>" in p for p in captured_prompts), (
        f"Expected <cortex-context> in at least one prompt. Got: {captured_prompts[:1]!r}"
    )
    assert any("project-context-here" in p for p in captured_prompts)


@pytest.mark.asyncio
async def test_monitor_assess_no_cortex_fence_when_none(tmp_path):
    """When cortex_context is None, no <cortex-context> fence appears."""
    from sentinel.roles.monitor import Monitor

    captured_prompts: list[str] = []

    fake_response = MagicMock()
    fake_response.model = "test"
    fake_response.provider = "test"
    fake_response.input_tokens = 0
    fake_response.output_tokens = 0
    fake_response.cost_usd = 0.0
    fake_response.content = "summary"

    fake_parsed = {
        "project_summary": "test",
        "lenses": [
            {
                "name": "lens1",
                "description": "desc",
                "what_to_look_for": "stuff",
                "questions": ["q"],
            }
        ],
    }

    class FakeProvider:
        name = "fake"
        timeout_sec = 60
        capabilities = MagicMock(agentic_code=False)

        async def chat(self, prompt, **kwargs):
            captured_prompts.append(prompt)
            return fake_response

        async def chat_json(self, prompt, schema, **kwargs):
            captured_prompts.append(prompt)
            return fake_parsed, fake_response

    fake_router = MagicMock()
    fake_router.get_provider.return_value = FakeProvider()

    from sentinel.state import ProjectState

    state = ProjectState(
        name="test",
        path=str(tmp_path),
        claude_md="",
        readme="",
        recent_commits="",
        file_tree="",
        branch="main",
        uncommitted_files="",
        test_output="",
        lint_output="",
    )

    monitor = Monitor(fake_router)

    with patch("sentinel.roles.researcher.Researcher") as mock_researcher_cls:
        mock_researcher = MagicMock()
        mock_brief = MagicMock()
        mock_brief.synthesis = ""
        mock_brief.cost_usd = 0.0
        mock_researcher.domain_brief = AsyncMock(return_value=mock_brief)
        mock_researcher_cls.return_value = mock_researcher

        with patch.object(FakeProvider, "chat_json") as mock_cj:
            synth_parsed = {
                "overall_score": 70,
                "summary": "ok",
                "strengths": [],
                "critical_risks": [],
                "top_actions": [
                    {
                        "title": "Fix it",
                        "why": "because",
                        "impact": "high",
                        "lens": "lens1",
                        "kind": "refine",
                        "acceptance_criteria": ["tests pass"],
                        "verification": ["pytest"],
                        "out_of_scope": [],
                    }
                ],
            }
            synth_resp = MagicMock()
            synth_resp.model = "test"
            synth_resp.provider = "test"
            synth_resp.input_tokens = 0
            synth_resp.output_tokens = 0
            synth_resp.cost_usd = 0.0

            call_count = 0

            async def side_effect(prompt, schema, **kwargs):
                nonlocal call_count
                captured_prompts.append(prompt)
                call_count += 1
                if call_count == 1:
                    return fake_parsed, fake_response
                return synth_parsed, synth_resp

            mock_cj.side_effect = side_effect

            await monitor.assess(state, cortex_context=None)

    mock_researcher.domain_brief.assert_awaited_once()
    assert mock_researcher.domain_brief.await_args.kwargs["cortex_context"] is None
    assert not any("<cortex-context>" in p for p in captured_prompts)


# ---------- CLI smoke test (skipped when cortex absent) ----------


@pytest.mark.skipif(
    shutil.which("cortex") is None,
    reason="cortex not installed",
)
def test_fetch_manifest_real_cortex_missing_dotcortex(tmp_path):
    """Real cortex binary, no .cortex/ - returns None, no exception."""
    result = fetch_manifest(tmp_path)
    assert result is None


# ---------- cycle wiring ----------


@pytest.mark.asyncio
async def test_cycle_with_cortex_context(tmp_path):
    """Loop.cycle fetches once and forwards the manifest into role calls."""
    from sentinel.loop.cycle import Loop
    from sentinel.roles.planner import Plan

    config = SimpleNamespace(project=SimpleNamespace(path=str(tmp_path)))
    loop = Loop(config, MagicMock())
    state = MagicMock()
    assessment = MagicMock()

    loop.monitor.assess = AsyncMock(return_value=assessment)
    loop._research_phase = AsyncMock(return_value=[])
    loop.planner.plan = AsyncMock(return_value=Plan())
    loop._execute_phase = AsyncMock(return_value=([], []))

    with (
        patch("sentinel.loop.cycle.fetch_manifest", return_value="fake context") as mock_fetch,
        patch("sentinel.loop.cycle.gather_state", return_value=state),
    ):
        await loop.cycle()

    mock_fetch.assert_called_once_with(tmp_path)
    loop.monitor.assess.assert_awaited_once_with(state, cortex_context="fake context")
    loop.planner.plan.assert_awaited_once_with(
        assessment,
        [],
        cortex_context="fake context",
    )


@pytest.mark.asyncio
async def test_cycle_without_cortex(tmp_path):
    """Loop.cycle preserves the existing prompt path when no manifest is available."""
    from sentinel.loop.cycle import Loop
    from sentinel.roles.planner import Plan

    config = SimpleNamespace(project=SimpleNamespace(path=str(tmp_path)))
    loop = Loop(config, MagicMock())
    state = MagicMock()
    assessment = MagicMock()

    loop.monitor.assess = AsyncMock(return_value=assessment)
    loop._research_phase = AsyncMock(return_value=[])
    loop.planner.plan = AsyncMock(return_value=Plan())
    loop._execute_phase = AsyncMock(return_value=([], []))

    with (
        patch("sentinel.loop.cycle.fetch_manifest", return_value=None),
        patch("sentinel.loop.cycle.gather_state", return_value=state),
    ):
        await loop.cycle()

    loop.monitor.assess.assert_awaited_once_with(state, cortex_context=None)
    loop.planner.plan.assert_awaited_once_with(assessment, [], cortex_context=None)
