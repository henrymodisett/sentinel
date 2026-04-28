"""Tests for Sentinel's Conductor subprocess provider adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable

import pytest

from sentinel.providers.conductor_adapter import ConductorAdapter, ProviderError
from sentinel.providers.interface import ProviderName, ProviderStatus

RunFactory = Callable[
    [int, str, str],
    Callable[..., subprocess.CompletedProcess[str]],
]


@pytest.fixture
def conductor_path(monkeypatch) -> str:
    path = "/usr/local/bin/conductor"
    monkeypatch.setattr(shutil, "which", lambda command: path if command == "conductor" else None)
    return path


@pytest.fixture
def run_factory(monkeypatch) -> RunFactory:
    def install(
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> Callable[..., subprocess.CompletedProcess[str]]:
        calls = []

        def fake_run(args, **kwargs):  # noqa: ANN001, ANN202
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(
                args=args,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )

        fake_run.calls = calls  # type: ignore[attr-defined]
        monkeypatch.setattr(subprocess, "run", fake_run)
        return fake_run

    return install


def _payload(**overrides) -> str:  # noqa: ANN003, ANN202
    payload = {
        "text": "ok",
        "model": "gpt-5.4",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "cost_usd": 0.01,
        "duration_ms": 25,
        "session_id": "session-1",
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.mark.parametrize(
    ("provider_name", "conductor_name", "sentinel_name"),
    [
        ("claude", "claude", ProviderName.CLAUDE),
        ("openai", "codex", ProviderName.OPENAI),
        ("gemini", "gemini", ProviderName.GEMINI),
        ("local", "ollama", ProviderName.LOCAL),
        ("kimi", "kimi", ProviderName.KIMI),
    ],
)
def test_construction_maps_names(
    conductor_path: str,
    provider_name: str,
    conductor_name: str,
    sentinel_name: ProviderName,
) -> None:
    adapter = ConductorAdapter(provider_name=provider_name, model="model")  # type: ignore[arg-type]

    assert adapter.conductor_path == conductor_path
    assert adapter.provider_name == provider_name
    assert adapter.conductor_name == conductor_name
    assert adapter.name == sentinel_name


def test_missing_conductor_binary_raises(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda command: None)

    with pytest.raises(RuntimeError, match="brew install autumngarage/conductor/conductor"):
        ConductorAdapter(provider_name="openai", model="gpt-5.4")


def test_invalid_provider_name_raises(conductor_path: str) -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        ConductorAdapter(provider_name="mistral", model="x")  # type: ignore[arg-type]


def test_from_conductor_provider_maps_router_object(conductor_path: str) -> None:
    provider = type("Provider", (), {"name": "codex", "default_model": "gpt-5.4"})()

    adapter = ConductorAdapter.from_conductor_provider(
        provider,
        timeout_sec=77,
        max_turns=9,
        effort="low",
    )

    assert adapter.provider_name == "openai"
    assert adapter.model == "gpt-5.4"
    assert adapter.timeout_sec == 77
    assert adapter.max_turns == 9
    assert adapter.effort == "low"


def test_from_conductor_provider_rejects_unknown_provider(conductor_path: str) -> None:
    provider = type("Provider", (), {"name": "unknown", "default_model": "model"})()

    with pytest.raises(ValueError, match="Unknown Conductor provider"):
        ConductorAdapter.from_conductor_provider(provider, timeout_sec=1, max_turns=1)


def test_capabilities_are_derived_from_provider_name(conductor_path: str) -> None:
    adapter = ConductorAdapter(provider_name="gemini", model="gemini-2.5-pro")

    assert adapter.capabilities.chat is True
    assert adapter.capabilities.web_search is True
    assert adapter.capabilities.agentic_code is True
    assert adapter.capabilities.long_context is True
    assert adapter.capabilities.thinking is True


@pytest.mark.asyncio
async def test_chat_invokes_conductor_call_and_maps_response(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    fake_run = run_factory(stdout=_payload(), stderr="debug")
    adapter = ConductorAdapter(
        provider_name="openai",
        model="gpt-5.4",
        timeout_sec=123,
        effort="high",
    )

    response = await adapter.chat("user prompt", system_prompt="system prompt")

    args, kwargs = fake_run.calls[0]  # type: ignore[attr-defined]
    assert args == [
        conductor_path,
        "call",
        "--with",
        "codex",
        "--model",
        "gpt-5.4",
        "--effort",
        "high",
        "--json",
        "--silent-route",
    ]
    assert kwargs["input"] == "system prompt\n\nuser prompt"
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 123
    assert kwargs["text"] is True
    assert response.content == "ok"
    assert response.provider == ProviderName.OPENAI
    assert response.model == "gpt-5.4"
    assert response.input_tokens == 10
    assert response.output_tokens == 5
    assert response.cost_usd == 0.01
    assert response.duration_ms == 25
    assert response.session_id == "session-1"
    assert response.stderr == "debug"
    assert response.raw_stdout == _payload()


@pytest.mark.asyncio
async def test_chat_accepts_content_field_alias(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(stdout=json.dumps({"content": "alias", "model": "gpt-5.4"}))
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    response = await adapter.chat("prompt")

    assert response.content == "alias"


@pytest.mark.asyncio
async def test_chat_ignores_non_mapping_usage(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(stdout=_payload(usage="bad"))
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    response = await adapter.chat("prompt")

    assert response.input_tokens == 0
    assert response.output_tokens == 0


@pytest.mark.asyncio
async def test_research_delegates_to_chat(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    fake_run = run_factory(stdout=_payload(text="researched"))
    adapter = ConductorAdapter(provider_name="gemini", model="gemini-2.5-pro")

    response = await adapter.research("research this")

    assert response.content == "researched"
    assert fake_run.calls[0][1]["input"] == "research this"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_code_invokes_conductor_exec_with_agentic_flags(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    fake_run = run_factory(stdout=_payload(text="patched"))
    adapter = ConductorAdapter(
        provider_name="claude",
        model="claude-sonnet-4-6",
        timeout_sec=900,
        effort="medium",
        max_turns=7,
    )

    response = await adapter.code("fix it", working_directory="/tmp/target")

    args, kwargs = fake_run.calls[0]  # type: ignore[attr-defined]
    assert args == [
        conductor_path,
        "exec",
        "--with",
        "claude",
        "--model",
        "claude-sonnet-4-6",
        "--effort",
        "medium",
        "--tools",
        "Bash,Edit,Glob,Grep,Read,Write",
        "--sandbox",
        "workspace-write",
        "--cwd",
        "/tmp/target",
        "--timeout",
        "900",
        "--json",
        "--silent-route",
    ]
    assert kwargs["input"] == "fix it"
    assert kwargs["timeout"] == 930
    assert response.content == "patched"


@pytest.mark.asyncio
async def test_chat_json_parses_and_validates(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(stdout=_payload(text='{"ok": true}'))
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    parsed, response = await adapter.chat_json(
        "return JSON",
        {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )

    assert parsed == {"ok": True}
    assert response.is_error is False


@pytest.mark.asyncio
async def test_chat_json_marks_invalid_json_as_error(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(stdout=_payload(text="not JSON"))
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    parsed, response = await adapter.chat_json("return JSON", {"type": "object"})

    assert parsed is None
    assert response.is_error is True
    assert "not valid JSON" in response.stderr


@pytest.mark.asyncio
async def test_chat_json_marks_schema_violation_as_error(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(stdout=_payload(text='{"wrong": true}'))
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    parsed, response = await adapter.chat_json(
        "return JSON",
        {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
    )

    assert parsed is None
    assert response.is_error is True
    assert "ok" in response.stderr


@pytest.mark.asyncio
async def test_nonzero_exit_raises_provider_error_with_stderr_and_partial_cost(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(returncode=2, stdout=_payload(text="partial", cost_usd=0.03), stderr="failed")
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    with pytest.raises(ProviderError, match="failed") as exc_info:
        await adapter.chat("prompt")

    assert exc_info.value.response is not None
    assert exc_info.value.response.is_error is True
    assert exc_info.value.response.content == "partial"
    assert exc_info.value.response.cost_usd == 0.03
    assert exc_info.value.response.stderr == "failed"


@pytest.mark.asyncio
async def test_nonzero_exit_without_json_raises_with_raw_stdout(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(returncode=1, stdout="not-json", stderr="boom")
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    with pytest.raises(ProviderError) as exc_info:
        await adapter.chat("prompt")

    assert exc_info.value.response is not None
    assert exc_info.value.response.raw_stdout == "not-json"
    assert exc_info.value.response.stderr == "boom"


@pytest.mark.asyncio
async def test_timeout_raises_provider_error(
    conductor_path: str,
    monkeypatch,
) -> None:
    def fake_run(args, **kwargs):  # noqa: ANN001, ANN202
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4", timeout_sec=1)

    with pytest.raises(ProviderError, match="timed out"):
        await adapter.chat("prompt")


@pytest.mark.asyncio
async def test_malformed_json_output_raises_provider_error(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(stdout="not-json")
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    with pytest.raises(ProviderError, match="malformed JSON"):
        await adapter.chat("prompt")


@pytest.mark.asyncio
async def test_missing_text_field_raises_provider_error(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(stdout=json.dumps({"usage": {"input_tokens": 1}, "cost_usd": 0.01}))
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    with pytest.raises(ProviderError, match="missing required field `text`"):
        await adapter.chat("prompt")


def test_detect_maps_configured_provider(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(
        stdout=json.dumps(
            [
                {
                    "provider": "codex",
                    "configured": True,
                    "reason": None,
                    "fix_command": None,
                    "default_model": "gpt-5.4",
                }
            ]
        )
    )
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    status = adapter.detect()

    assert isinstance(status, ProviderStatus)
    assert status.installed is True
    assert status.authenticated is True
    assert "gpt-5.4" in status.models


def test_detect_maps_unconfigured_provider_reason(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(
        stdout=json.dumps(
            [
                {
                    "provider": "gemini",
                    "configured": False,
                    "reason": "missing credentials",
                    "fix_command": "conductor init --only gemini",
                    "default_model": "gemini-2.5-pro",
                }
            ]
        )
    )
    adapter = ConductorAdapter(provider_name="gemini", model="gemini-2.5-pro")

    status = adapter.detect()

    assert status.installed is True
    assert status.authenticated is False
    assert status.auth_hint == "missing credentials"
    assert status.install_hint == "conductor init --only gemini"


def test_detect_handles_malformed_json(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(stdout="nope")
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    status = adapter.detect()

    assert status.installed is True
    assert status.authenticated is False
    assert "malformed JSON" in status.auth_hint


def test_detect_handles_nonzero_exit(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(returncode=1, stderr="list failed")
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    status = adapter.detect()

    assert status.installed is True
    assert status.authenticated is False
    assert status.auth_hint == "list failed"


def test_detect_handles_missing_provider_in_list(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(stdout=json.dumps([{"provider": "gemini", "configured": True}]))
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    status = adapter.detect()

    assert status.installed is True
    assert status.authenticated is False
    assert "did not list provider codex" in status.auth_hint


def test_detect_appends_default_model_from_conductor_list(
    conductor_path: str,
    run_factory: RunFactory,
) -> None:
    run_factory(
        stdout=json.dumps(
            [
                {
                    "provider": "ollama",
                    "configured": True,
                    "default_model": "qwen3.6:35b-a3b",
                }
            ]
        )
    )
    adapter = ConductorAdapter(provider_name="local", model="qwen3.6:35b-a3b")

    status = adapter.detect()

    assert "qwen3.6:35b-a3b" in status.models


@pytest.mark.skipif(shutil.which("conductor") is None, reason="conductor CLI not installed")
def test_real_conductor_list_smoke() -> None:
    adapter = ConductorAdapter(provider_name="openai", model="gpt-5.4")

    status = adapter.detect()

    assert isinstance(status, ProviderStatus)
