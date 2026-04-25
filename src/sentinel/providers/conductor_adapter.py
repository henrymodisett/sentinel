"""Conductor-backed provider adapter.

Sentinel keeps its own Provider ABC because roles and journals depend on
that small surface. This adapter is the only concrete implementation: it
translates Sentinel's stable config names to Conductor's provider IDs, calls
Conductor, then maps the normalized response back to ChatResponse.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import shutil
import subprocess
import time
from dataclasses import replace
from typing import Any, Literal

import httpx

from sentinel.providers.interface import (
    ChatResponse,
    Provider,
    ProviderCapabilities,
    ProviderName,
    ProviderStatus,
    parse_json_safe,
)

SentinelProviderName = Literal["claude", "openai", "gemini", "local", "kimi"]

_SENTINEL_TO_CONDUCTOR: dict[str, str] = {
    "claude": "claude",
    "openai": "codex",
    "gemini": "gemini",
    "local": "ollama",
    "kimi": "kimi",
}

_CONDUCTOR_TO_SENTINEL: dict[str, str] = {
    value: key for key, value in _SENTINEL_TO_CONDUCTOR.items()
}

_SENTINEL_ENUM: dict[str, ProviderName] = {
    "claude": ProviderName.CLAUDE,
    "openai": ProviderName.OPENAI,
    "gemini": ProviderName.GEMINI,
    "local": ProviderName.LOCAL,
    "kimi": ProviderName.KIMI,
}

_CLI_COMMAND: dict[str, str | None] = {
    "claude": "claude",
    "openai": "codex",
    "gemini": "gemini",
    "local": "ollama",
    "kimi": None,
}

_MODEL_LISTS: dict[str, list[str]] = {
    "claude": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "openai": ["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "o4-mini"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    "local": [],
    "kimi": ["@cf/moonshotai/kimi-k2.6"],
}

_INSTALL_HINTS: dict[str, str] = {
    "claude": "brew install claude",
    "openai": "npm install -g @openai/codex",
    "gemini": "npm install -g @google/gemini-cli",
    "local": "brew install ollama",
    "kimi": "conductor init",
}

_AUTH_HINTS: dict[str, str] = {
    "claude": "claude login",
    "openai": "codex login",
    "gemini": "gemini (authenticates via browser on first run)",
    "local": "ollama serve && ollama pull qwen2.5-coder:14b",
    "kimi": "set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID via conductor init",
}

_CONDUCTOR_CLASS_NAMES: dict[str, str] = {
    "claude": "ClaudeProvider",
    "codex": "CodexProvider",
    "gemini": "GeminiProvider",
    "ollama": "OllamaProvider",
    "kimi": "KimiProvider",
}

_AGENTIC_TOOLS = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})


def _get_conductor_provider(
    conductor_name: str,
    *,
    timeout_sec: int,
    ollama_endpoint: str | None,
) -> Any:
    """Instantiate a Conductor provider with Sentinel's per-role settings."""
    providers = importlib.import_module("conductor.providers")
    cls = getattr(providers, _CONDUCTOR_CLASS_NAMES.get(conductor_name, ""), None)
    if cls is not None:
        kwargs: dict[str, Any] = {"timeout_sec": timeout_sec}
        if conductor_name == "ollama" and ollama_endpoint:
            kwargs["base_url"] = ollama_endpoint
        try:
            return cls(**kwargs)
        except TypeError:
            # Older Conductor releases accepted fewer constructor kwargs.
            # Fall back to the registry path rather than failing detection.
            pass
    return providers.get_provider(conductor_name)


def _accepts_keyword(callable_obj: Any, keyword: str) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return keyword in signature.parameters


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _raw_streams(raw: Any) -> tuple[str, str]:
    """Extract stderr/stdout-ish diagnostics from Conductor raw payloads."""
    if not raw:
        return "", ""
    if isinstance(raw, dict):
        stderr = str(raw.get("stderr") or "")
        stdout = raw.get("stdout")
        if stdout is None and raw:
            stdout = json.dumps(raw, separators=(",", ":"), sort_keys=True)
        return stderr, str(stdout or "")
    return "", str(raw)


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_schema_basic(value: Any, schema: dict, path: str = "$") -> None:
    """Small JSON Schema subset used when jsonschema is not installed."""
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_schema_type_matches(value, item) for item in expected):
            raise ValueError(f"{path} must be one of {expected}")
    elif isinstance(expected, str) and not _schema_type_matches(value, expected):
        raise ValueError(f"{path} must be {expected}")

    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']!r}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        properties = schema.get("properties") or {}
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                _validate_schema_basic(value[key], child_schema, f"{path}.{key}")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        child_schema = schema["items"]
        for index, item in enumerate(value):
            _validate_schema_basic(item, child_schema, f"{path}[{index}]")


def _validate_json_schema(value: Any, schema: dict) -> None:
    try:
        from jsonschema import validate
        validate(instance=value, schema=schema)
    except ModuleNotFoundError:
        _validate_schema_basic(value, schema)


class ConductorAdapter(Provider):
    """Sentinel Provider implementation backed by Conductor."""

    capabilities = ProviderCapabilities()

    def __init__(
        self,
        *,
        provider_name: SentinelProviderName,
        model: str,
        timeout_sec: int = 600,
        max_turns: int = 40,
        ollama_endpoint: str | None = None,
        effort: str | int = "medium",
        conductor_client: Any | None = None,
        routing_reason: str = "",
    ) -> None:
        if provider_name not in _SENTINEL_TO_CONDUCTOR:
            raise ValueError(f"Unknown provider: {provider_name}")
        self.provider_name = provider_name
        self.conductor_name = _SENTINEL_TO_CONDUCTOR[provider_name]
        self.name = _SENTINEL_ENUM[provider_name]
        self.cli_command = _CLI_COMMAND[provider_name] or "conductor"
        self.model = model
        self.timeout_sec = timeout_sec
        self.max_turns = max_turns
        self.ollama_endpoint = ollama_endpoint
        self.effort = effort
        self.routing_reason = routing_reason
        self._client: Any | None = conductor_client
        self.capabilities = self._capabilities_from_provider()

    @classmethod
    def from_conductor_provider(
        cls,
        conductor_provider: Any,
        *,
        timeout_sec: int,
        max_turns: int,
        ollama_endpoint: str | None = None,
        effort: str | int = "medium",
        routing_reason: str = "",
    ) -> ConductorAdapter:
        conductor_name = getattr(conductor_provider, "name", "")
        provider_name = _CONDUCTOR_TO_SENTINEL.get(conductor_name)
        if provider_name is None:
            raise ValueError(f"Unknown Conductor provider: {conductor_name}")
        model = getattr(conductor_provider, "default_model", "") or ""
        return cls(
            provider_name=provider_name,  # type: ignore[arg-type]
            model=model,
            timeout_sec=timeout_sec,
            max_turns=max_turns,
            ollama_endpoint=ollama_endpoint,
            effort=effort,
            conductor_client=conductor_provider,
            routing_reason=routing_reason,
        )

    def _conductor_provider(self) -> Any:
        if self._client is None:
            self._client = _get_conductor_provider(
                self.conductor_name,
                timeout_sec=self.timeout_sec,
                ollama_endpoint=self.ollama_endpoint,
            )
        return self._client

    def _capabilities_from_provider(self) -> ProviderCapabilities:
        try:
            client = self._conductor_provider()
        except Exception:
            client = None

        tools = getattr(client, "supported_tools", frozenset()) if client else frozenset()
        sandboxes = getattr(client, "supported_sandboxes", frozenset()) if client else frozenset()
        tags = set(getattr(client, "tags", [])) if client else set()

        agentic_code = (
            (
                bool(_AGENTIC_TOOLS & set(tools))
                and "workspace-write" in set(sandboxes)
            )
            if client is not None
            else self.provider_name in {"claude", "openai", "gemini", "local", "kimi"}
        )
        web_search = (
            self.provider_name in {"claude", "openai", "gemini"}
            or "web-search" in tags
        )
        long_context = self.provider_name in {"claude", "gemini", "kimi"}
        thinking = bool(getattr(client, "supports_effort", False)) if client else (
            self.provider_name in {"claude", "openai", "gemini", "kimi"}
        )
        return ProviderCapabilities(
            chat=True,
            web_search=web_search,
            agentic_code=agentic_code,
            long_context=long_context,
            thinking=thinking,
        )

    def _map_response(self, response: Any) -> ChatResponse:
        usage = getattr(response, "usage", {}) or {}
        raw = getattr(response, "raw", {}) or {}
        stderr, raw_stdout = _raw_streams(raw)
        return ChatResponse(
            content=getattr(response, "text", "") or "",
            model=getattr(response, "model", None) or self.model,
            provider=self.name,
            input_tokens=_as_int(usage.get("input_tokens")),
            output_tokens=_as_int(usage.get("output_tokens")),
            cost_usd=_as_float(getattr(response, "cost_usd", None)),
            duration_ms=_as_int(getattr(response, "duration_ms", 0)),
            session_id=getattr(response, "session_id", None),
            stderr=stderr,
            raw_stdout=raw_stdout,
        )

    def _error_response(
        self,
        exc: Exception,
        *,
        started_at: float,
    ) -> tuple[ChatResponse, str]:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        error_name = exc.__class__.__name__
        label = {
            "ProviderConfigError": "provider config error",
            "ProviderHTTPError": "provider error",
            "UnsupportedCapability": "unsupported capability",
            "ProviderError": "provider error",
            "TimeoutError": "timeout",
        }.get(error_name, "provider error")
        detail = str(exc) or error_name
        return (
            ChatResponse(
                content=f"Error: {detail}",
                model=self.model,
                provider=self.name,
                duration_ms=elapsed_ms,
                stderr=detail,
                is_error=True,
            ),
            label,
        )

    async def chat(
        self,
        prompt: str,
        system_prompt: str | None = None,
    ) -> ChatResponse:
        if (resp := self._abort_if_budget_exhausted()):
            return resp

        full_prompt = prompt if not system_prompt else f"{system_prompt}\n\n{prompt}"
        started = time.perf_counter()
        try:
            client = self._conductor_provider()
            response = await asyncio.to_thread(
                client.call,
                full_prompt,
                self.model,
                effort=self.effort,
                resume_session_id=None,
            )
            mapped = self._map_response(response)
            self._journal_call(started, mapped)
            return mapped
        except Exception as exc:
            mapped, label = self._error_response(exc, started_at=started)
            self._journal_call(started, mapped, error=label)
            return mapped

    async def chat_json(
        self,
        prompt: str,
        schema: dict,
        system_prompt: str | None = None,
    ) -> tuple[dict | None, ChatResponse]:
        schema_str = json.dumps(schema, indent=2)
        strict_prompt = (
            f"{prompt}\n\n"
            "CRITICAL: Respond with ONLY a JSON object matching this schema. "
            "No prose outside the JSON, no markdown fences, no explanation. "
            "Just valid JSON starting with { and ending with }.\n\n"
            f"Schema:\n{schema_str}"
        )
        response = await self.chat(strict_prompt, system_prompt)
        if response.is_error:
            return None, response

        parsed = parse_json_safe(response.content)
        if parsed is None:
            return None, replace(
                response,
                is_error=True,
                stderr=(
                    response.stderr
                    or "Conductor response was not valid JSON for chat_json"
                ),
            )

        try:
            _validate_json_schema(parsed, schema)
        except Exception as exc:
            return None, replace(
                response,
                is_error=True,
                stderr=(
                    f"{response.stderr}\n{exc}".strip()
                    if response.stderr else str(exc)
                ),
            )
        return parsed, response

    async def research(self, prompt: str) -> ChatResponse:
        return await self.chat(prompt)

    async def code(
        self,
        prompt: str,
        working_directory: str = ".",
    ) -> ChatResponse:
        if (resp := self._abort_if_budget_exhausted()):
            return resp

        started = time.perf_counter()
        try:
            client = self._conductor_provider()
            kwargs: dict[str, Any] = {
                "effort": self.effort,
                "tools": _AGENTIC_TOOLS,
                "sandbox": "workspace-write",
                "cwd": working_directory,
                "timeout_sec": self.timeout_sec,
                "resume_session_id": None,
            }
            if _accepts_keyword(client.exec, "max_turns"):
                kwargs["max_turns"] = self.max_turns
            response = await asyncio.to_thread(
                client.exec,
                prompt,
                self.model,
                **kwargs,
            )
            mapped = self._map_response(response)
            self._journal_call(started, mapped)
            return mapped
        except Exception as exc:
            mapped, label = self._error_response(exc, started_at=started)
            self._journal_call(started, mapped, error=label)
            return mapped

    def _journal_call(
        self,
        started_at: float,
        response: ChatResponse,
        error: str | None = None,
    ) -> None:
        if self.routing_reason:
            from sentinel.journal import current_journal, set_pending_routing_reason
            if current_journal() is not None:
                set_pending_routing_reason(self.routing_reason)
        super()._journal_call(started_at, response, error=error)

    def _detect_local(self) -> ProviderStatus:
        path = shutil.which("ollama")
        if not path:
            return ProviderStatus(
                installed=False,
                install_hint=_INSTALL_HINTS["local"],
                auth_hint=_AUTH_HINTS["local"],
            )

        endpoint = (self.ollama_endpoint or "http://localhost:11434").rstrip("/")
        try:
            resp = httpx.get(f"{endpoint}/api/tags", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                models = [m["name"] for m in data.get("models", []) if "name" in m]
                return ProviderStatus(
                    installed=True,
                    authenticated=True,
                    models=models,
                    install_hint=_INSTALL_HINTS["local"],
                    auth_hint=_AUTH_HINTS["local"],
                )
        except (httpx.HTTPError, ValueError):
            pass

        return ProviderStatus(
            installed=True,
            authenticated=False,
            install_hint=_INSTALL_HINTS["local"],
            auth_hint=_AUTH_HINTS["local"],
        )

    def _detect_cli_fallback(self) -> ProviderStatus:
        command = _CLI_COMMAND[self.provider_name]
        if command is None:
            return ProviderStatus(
                installed=False,
                install_hint=_INSTALL_HINTS[self.provider_name],
                auth_hint=_AUTH_HINTS[self.provider_name],
            )
        path = shutil.which(command)
        installed = path is not None
        version = None
        if installed:
            try:
                result = subprocess.run(
                    [command, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    version = (result.stdout or result.stderr).strip() or None
            except (OSError, subprocess.TimeoutExpired):
                installed = False
        return ProviderStatus(
            installed=installed,
            authenticated=installed,
            version=version,
            models=_MODEL_LISTS[self.provider_name],
            install_hint=_INSTALL_HINTS[self.provider_name],
            auth_hint=_AUTH_HINTS[self.provider_name],
        )

    def detect(self) -> ProviderStatus:
        if self.provider_name == "local":
            return self._detect_local()

        try:
            client = self._conductor_provider()
        except ModuleNotFoundError:
            return self._detect_cli_fallback()
        except Exception:
            return self._detect_cli_fallback()

        try:
            configured, reason = client.configured()
        except Exception as exc:
            configured = False
            reason = str(exc)

        if self.provider_name == "kimi":
            installed = True
        else:
            installed = shutil.which(self.cli_command) is not None

        return ProviderStatus(
            installed=installed,
            authenticated=bool(configured),
            models=_MODEL_LISTS[self.provider_name],
            install_hint=_INSTALL_HINTS[self.provider_name],
            auth_hint=reason or _AUTH_HINTS[self.provider_name],
        )
