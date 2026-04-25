"""Tests for Sentinel's Conductor-backed provider adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sentinel.providers.conductor_adapter import ConductorAdapter
from sentinel.providers.interface import ProviderName, ProviderStatus


class FakeConductorProvider:
    name = "codex"
    default_model = "gpt-5.4"
    tags = ["web-search", "tool-use", "long-context"]
    supported_tools = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})
    supported_sandboxes = frozenset({"read-only", "workspace-write", "none"})
    supports_effort = True

    def __init__(self) -> None:
        self.call_args = None
        self.exec_args = None
        self.call_response = SimpleNamespace(
            text="ok",
            provider="codex",
            model="gpt-5.4",
            duration_ms=25,
            usage={"input_tokens": 10, "output_tokens": 5},
            cost_usd=0.01,
            session_id="session-1",
            raw={"stdout": "raw out", "stderr": "raw err"},
        )
        self.exec_response = self.call_response
        self.call_error: Exception | None = None
        self.exec_error: Exception | None = None
        self.configured_result = (True, None)

    def configured(self):  # noqa: ANN201
        return self.configured_result

    def call(self, task, model=None, **kwargs):  # noqa: ANN001, ANN201
        self.call_args = (task, model, kwargs)
        if self.call_error:
            raise self.call_error
        return self.call_response

    def exec(self, task, model=None, *, max_turns=None, **kwargs):  # noqa: A003, ANN001, ANN201
        self.exec_args = (task, model, kwargs | {"max_turns": max_turns})
        if self.exec_error:
            raise self.exec_error
        return self.exec_response


def _adapter(monkeypatch, fake=None, **kwargs) -> tuple[ConductorAdapter, FakeConductorProvider]:
    provider = fake or FakeConductorProvider()
    monkeypatch.setattr(
        "sentinel.providers.conductor_adapter._get_conductor_provider",
        lambda *args, **kwargs: provider,
    )
    adapter = ConductorAdapter(
        provider_name=kwargs.pop("provider_name", "openai"),
        model=kwargs.pop("model", "gpt-5.4"),
        **kwargs,
    )
    return adapter, provider


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
    monkeypatch,
    provider_name: str,
    conductor_name: str,
    sentinel_name: ProviderName,
) -> None:
    adapter, _ = _adapter(monkeypatch, provider_name=provider_name)

    assert adapter.provider_name == provider_name
    assert adapter.conductor_name == conductor_name
    assert adapter.name == sentinel_name


def test_invalid_provider_name_raises() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        ConductorAdapter(provider_name="mistral", model="x")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_chat_calls_conductor_and_maps_response(monkeypatch) -> None:
    adapter, provider = _adapter(monkeypatch)

    response = await adapter.chat("user prompt", system_prompt="system prompt")

    assert provider.call_args == (
        "system prompt\n\nuser prompt",
        "gpt-5.4",
        {"effort": "medium", "resume_session_id": None},
    )
    assert response.content == "ok"
    assert response.provider == ProviderName.OPENAI
    assert response.model == "gpt-5.4"
    assert response.input_tokens == 10
    assert response.output_tokens == 5
    assert response.cost_usd == 0.01
    assert response.session_id == "session-1"
    assert response.stderr == "raw err"
    assert response.raw_stdout == "raw out"


@pytest.mark.asyncio
async def test_chat_returns_error_response_on_conductor_failure(monkeypatch) -> None:
    fake = FakeConductorProvider()
    fake.call_error = RuntimeError("upstream failed")
    adapter, _ = _adapter(monkeypatch, fake=fake)

    response = await adapter.chat("prompt")

    assert response.is_error is True
    assert "upstream failed" in response.content
    assert response.stderr == "upstream failed"


@pytest.mark.asyncio
async def test_chat_json_parses_and_validates(monkeypatch) -> None:
    fake = FakeConductorProvider()
    fake.call_response = SimpleNamespace(
        text='{"ok": true}',
        provider="codex",
        model="gpt-5.4",
        duration_ms=1,
        usage={},
        cost_usd=None,
        session_id=None,
        raw={},
    )
    adapter, _ = _adapter(monkeypatch, fake=fake)

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
async def test_chat_json_marks_invalid_json_as_error(monkeypatch) -> None:
    fake = FakeConductorProvider()
    fake.call_response = SimpleNamespace(
        text="not JSON",
        provider="codex",
        model="gpt-5.4",
        duration_ms=1,
        usage={},
        cost_usd=None,
        session_id=None,
        raw={},
    )
    adapter, _ = _adapter(monkeypatch, fake=fake)

    parsed, response = await adapter.chat_json("return JSON", {"type": "object"})

    assert parsed is None
    assert response.is_error is True
    assert "not valid JSON" in response.stderr


@pytest.mark.asyncio
async def test_chat_json_marks_schema_violation_as_error(monkeypatch) -> None:
    fake = FakeConductorProvider()
    fake.call_response = SimpleNamespace(
        text='{"wrong": true}',
        provider="codex",
        model="gpt-5.4",
        duration_ms=1,
        usage={},
        cost_usd=None,
        session_id=None,
        raw={},
    )
    adapter, _ = _adapter(monkeypatch, fake=fake)

    parsed, response = await adapter.chat_json(
        "return JSON",
        {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )

    assert parsed is None
    assert response.is_error is True
    assert "ok" in response.stderr


@pytest.mark.asyncio
async def test_code_passes_agentic_options_to_conductor(monkeypatch) -> None:
    adapter, provider = _adapter(monkeypatch, timeout_sec=123, max_turns=7)

    response = await adapter.code("fix it", working_directory="/tmp/target")

    assert response.content == "ok"
    assert provider.exec_args is not None
    task, model, kwargs = provider.exec_args
    assert task == "fix it"
    assert model == "gpt-5.4"
    assert kwargs["sandbox"] == "workspace-write"
    assert kwargs["cwd"] == "/tmp/target"
    assert kwargs["timeout_sec"] == 123
    assert kwargs["max_turns"] == 7
    assert set(kwargs["tools"]) == {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}


@pytest.mark.asyncio
async def test_research_delegates_to_chat(monkeypatch) -> None:
    adapter, provider = _adapter(monkeypatch)

    await adapter.research("research this")

    assert provider.call_args[0] == "research this"


def test_detect_maps_configured_result(monkeypatch) -> None:
    fake = FakeConductorProvider()
    fake.configured_result = (False, "missing credentials")
    adapter, _ = _adapter(monkeypatch, fake=fake, provider_name="kimi")

    status = adapter.detect()

    assert isinstance(status, ProviderStatus)
    assert status.installed is True
    assert status.authenticated is False
    assert status.auth_hint == "missing credentials"


def test_constructor_passes_timeout_and_ollama_endpoint(monkeypatch) -> None:
    fake = FakeConductorProvider()
    calls = []

    def fake_get(name, *, timeout_sec, ollama_endpoint):  # noqa: ANN001, ANN202
        calls.append((name, timeout_sec, ollama_endpoint))
        return fake

    monkeypatch.setattr(
        "sentinel.providers.conductor_adapter._get_conductor_provider",
        fake_get,
    )

    ConductorAdapter(
        provider_name="local",
        model="qwen3.6:35b-a3b",
        timeout_sec=77,
        ollama_endpoint="http://ollama.local",
    )

    assert calls == [("ollama", 77, "http://ollama.local")]
