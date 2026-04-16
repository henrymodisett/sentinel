"""Shared pytest fixtures — fake CLI environments for scenario testing.

The `fake_cli_env` fixture creates a temp directory with stub executables
for whichever providers you want to simulate. Prepends that dir to PATH
for the duration of the test. The stubs just echo a version string when
called with `--version`, making `shutil.which(...)` and detection pass.

Usage:
    def test_init_with_claude_only(fake_cli_env):
        fake_cli_env(claude=True)
        result = runner.invoke(main, ["init", "--yes"])
        assert ...

For Ollama (HTTP-based, not a CLI), use `fake_ollama_server` to spin up
an actual local HTTP server that mimics /api/tags and /api/chat.
"""

from __future__ import annotations

import stat
import tempfile
from pathlib import Path

import pytest

# Stub scripts — each just echoes something reasonable for the CLI's
# expected output so detection passes. Sentinel providers only check
# `<cli> --version`, so all we need is a successful exit.

_CLAUDE_STUB = """#!/usr/bin/env bash
case "$1" in
  --version) echo "claude 4.6.0"; exit 0 ;;
  -p|--print)
    # Fake a successful JSON response for scan prompts
    cat <<'JSON'
{"type":"result","subtype":"success","is_error":false,"duration_ms":100,"num_turns":1,"result":"{\\"score\\":80,\\"top_finding\\":\\"fake\\",\\"findings\\":\\"fake\\",\\"recommended_tasks\\":[\\"fake\\"]}","stop_reason":"end_turn","session_id":"fake","total_cost_usd":0.01,"usage":{"input_tokens":10,"output_tokens":5}}
JSON
    ;;
  *) exit 0 ;;
esac
"""

_CODEX_STUB = """#!/usr/bin/env bash
case "$1" in
  --version) echo "codex 1.0.0"; exit 0 ;;
  exec)
    # Emit minimal NDJSON that our parser accepts
    echo '{"type":"item.completed","item":{"type":"agent_message","text":"fake"}}'
    echo '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}'
    ;;
  *) exit 0 ;;
esac
"""

_GEMINI_STUB = """#!/usr/bin/env bash
case "$1" in
  --version|-v) echo "0.36.0"; exit 0 ;;
  -p|--prompt)
    # Emit Gemini's JSON output format
    cat <<'JSON'
{"session_id":"fake","response":"fake","stats":{"models":{"gemini-2.5-flash":{"api":{"totalRequests":1},"tokens":{"input":10,"candidates":5,"total":15}}}}}
JSON
    ;;
  *) exit 0 ;;
esac
"""

_OLLAMA_STUB = """#!/usr/bin/env bash
case "$1" in
  --version) echo "ollama version 0.20.0"; exit 0 ;;
  *) exit 0 ;;
esac
"""


STUBS = {
    "claude": _CLAUDE_STUB,
    "codex": _CODEX_STUB,
    "gemini": _GEMINI_STUB,
    "ollama": _OLLAMA_STUB,
}


@pytest.fixture
def fake_cli_env(monkeypatch):
    """Factory for simulated CLI environments.

    Returns a function that installs stubs on PATH. Call it with keyword
    args for each CLI you want available:

        fake_cli_env(claude=True, gemini=True)

    The PATH is scoped to the test — cleaned up automatically.
    """
    tmpdirs: list[tempfile.TemporaryDirectory] = []

    def _install(**flags: bool) -> Path:
        tmp = tempfile.TemporaryDirectory()
        tmpdirs.append(tmp)
        bin_dir = Path(tmp.name)

        for name, available in flags.items():
            if not available:
                continue
            if name not in STUBS:
                raise ValueError(f"Unknown CLI stub: {name}")
            stub_path = bin_dir / name
            stub_path.write_text(STUBS[name])
            stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC)

        # Replace PATH entirely so real CLIs don't leak through.
        # Keep only system basics (sh, env, etc.) — these don't include
        # any LLM CLIs and are needed for stubs to run.
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        return bin_dir

    yield _install

    # Cleanup temp dirs after test
    for tmp in tmpdirs:
        tmp.cleanup()


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Point HOME and cwd at a clean temp dir for init/scan tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_budget_state():
    """Reset both budget ContextVars (time deadline + money cap) around
    every test.

    set_cycle_deadline / set_cycle_money_cap write to module-level
    ContextVars. Without an autouse reset, a test that exhausts either
    dimension leaks that state into the next test in collection order —
    so a provider's _abort_if_budget_exhausted short-circuits a call
    that the next test expected to dispatch. Cheap to reset; impossible
    to debug if you don't.
    """
    from sentinel.budget_ctx import set_cycle_deadline, set_cycle_money_cap
    set_cycle_deadline(None)
    set_cycle_money_cap(None)
    yield
    set_cycle_deadline(None)
    set_cycle_money_cap(None)
