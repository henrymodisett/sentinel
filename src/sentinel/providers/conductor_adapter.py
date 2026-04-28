"""Conductor subprocess provider adapter.

Sentinel keeps its own Provider ABC because roles and journals depend on
that small surface. This adapter satisfies that contract by invoking the
`conductor` CLI per call and mapping Conductor's JSON response back to
ChatResponse.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from dataclasses import replace
from typing import Any, Literal

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

_MODEL_LISTS: dict[str, list[str]] = {
    "claude": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "openai": ["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "o4-mini"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    "local": [],
    "kimi": ["moonshotai/kimi-k2.6", "@cf/moonshotai/kimi-k2.6"],
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
    "kimi": "set OPENROUTER_API_KEY via conductor init",
}

_AGENTIC_TOOLS = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})


class ProviderError(RuntimeError):
    """Raised when Conductor exits non-zero or emits an invalid response."""

    def __init__(self, message: str, *, response: ChatResponse | None = None) -> None:
        super().__init__(message)
        self.response = response


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
        for index, item in enumerate(value):
            _validate_schema_basic(item, schema["items"], f"{path}[{index}]")


def _validate_json_schema(value: Any, schema: dict) -> None:
    try:
        from jsonschema import validate  # type: ignore[import-untyped]

        validate(instance=value, schema=schema)
    except ModuleNotFoundError:
        _validate_schema_basic(value, schema)


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


class ConductorAdapter(Provider):
    """Sentinel Provider implementation backed by the Conductor CLI."""

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
        routing_reason: str = "",
    ) -> None:
        if provider_name not in _SENTINEL_TO_CONDUCTOR:
            raise ValueError(f"Unknown provider: {provider_name}")
        conductor_path = shutil.which("conductor")
        if conductor_path is None:
            raise RuntimeError(
                "Conductor CLI not found on PATH. Install it with "
                "`brew install autumngarage/conductor/conductor`."
            )

        self.provider_name = provider_name
        self.conductor_name = _SENTINEL_TO_CONDUCTOR[provider_name]
        self.name = _SENTINEL_ENUM[provider_name]
        self.cli_command = "conductor"
        self.model = model
        self.timeout_sec = timeout_sec
        self.max_turns = max_turns
        self.ollama_endpoint = ollama_endpoint
        self.effort = effort
        self.routing_reason = routing_reason
        self.conductor_path = conductor_path
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
            routing_reason=routing_reason,
        )

    def _capabilities_from_provider(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            chat=True,
            web_search=self.provider_name in {"claude", "openai", "gemini"},
            agentic_code=True,
            long_context=self.provider_name in {"claude", "gemini", "kimi"},
            thinking=self.provider_name in {"claude", "openai", "gemini", "kimi"},
        )

    def _run(
        self,
        args: list[str],
        *,
        prompt: str | None = None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                timeout=timeout,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            response = ChatResponse(
                content=f"Error: conductor timed out after {timeout}s",
                model=self.model,
                provider=self.name,
                stderr=str(exc),
                is_error=True,
            )
            raise ProviderError(str(exc), response=response) from exc
        except OSError as exc:
            response = ChatResponse(
                content=f"Error: {exc}",
                model=self.model,
                provider=self.name,
                stderr=str(exc),
                is_error=True,
            )
            raise ProviderError(str(exc), response=response) from exc

    def _parse_stdout(self, stdout: str) -> dict[str, Any]:
        parsed = parse_json_safe(stdout)
        if not isinstance(parsed, dict):
            raise ProviderError("Conductor emitted malformed JSON.")
        return parsed

    def _response_from_payload(
        self,
        payload: dict[str, Any],
        *,
        stderr: str,
        raw_stdout: str,
        is_error: bool = False,
    ) -> ChatResponse:
        text = payload.get("text", payload.get("content"))
        if text is None:
            raise ProviderError("Conductor JSON response is missing required field `text`.")

        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        return ChatResponse(
            content=str(text),
            model=str(payload.get("model") or self.model),
            provider=self.name,
            input_tokens=_as_int(usage.get("input_tokens")),
            output_tokens=_as_int(usage.get("output_tokens")),
            cost_usd=_as_float(payload.get("cost_usd")),
            duration_ms=_as_int(payload.get("duration_ms")),
            session_id=payload.get("session_id"),
            stderr=stderr,
            raw_stdout=raw_stdout,
            is_error=is_error or bool(payload.get("is_error", False)),
        )

    def _partial_error_response(
        self,
        stdout: str,
        *,
        stderr: str,
    ) -> ChatResponse:
        try:
            payload = self._parse_stdout(stdout)
            return self._response_from_payload(
                payload,
                stderr=stderr,
                raw_stdout=stdout,
                is_error=True,
            )
        except ProviderError:
            return ChatResponse(
                content=f"Error: {stderr or 'conductor failed'}",
                model=self.model,
                provider=self.name,
                stderr=stderr,
                raw_stdout=stdout,
                is_error=True,
            )

    def _map_completed_process(self, result: subprocess.CompletedProcess[str]) -> ChatResponse:
        if result.returncode != 0:
            response = self._partial_error_response(result.stdout, stderr=result.stderr)
            raise ProviderError(
                result.stderr or f"conductor exited {result.returncode}",
                response=response,
            )
        payload = self._parse_stdout(result.stdout)
        return self._response_from_payload(
            payload,
            stderr=result.stderr,
            raw_stdout=result.stdout,
            is_error=False,
        )

    async def chat(
        self,
        prompt: str,
        system_prompt: str | None = None,
    ) -> ChatResponse:
        if resp := self._abort_if_budget_exhausted():
            return resp

        full_prompt = prompt if not system_prompt else f"{system_prompt}\n\n{prompt}"
        started = time.perf_counter()
        args = [
            self.conductor_path,
            "call",
            "--with",
            self.conductor_name,
            "--model",
            self.model,
            "--effort",
            str(self.effort),
            "--json",
            "--silent-route",
        ]
        try:
            result = await asyncio.to_thread(
                self._run,
                args,
                prompt=full_prompt,
                timeout=self.timeout_sec,
            )
            response = self._map_completed_process(result)
            self._journal_call(started, response)
            return response
        except ProviderError as exc:
            response = exc.response or ChatResponse(
                content=f"Error: {exc}",
                model=self.model,
                provider=self.name,
                stderr=str(exc),
                is_error=True,
            )
            self._journal_call(started, response, error="provider error")
            raise

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
        parsed = parse_json_safe(response.content)
        if parsed is None:
            return None, replace(
                response,
                is_error=True,
                stderr=(response.stderr or "Conductor response was not valid JSON for chat_json"),
            )

        try:
            _validate_json_schema(parsed, schema)
        except Exception as exc:
            return None, replace(
                response,
                is_error=True,
                stderr=(f"{response.stderr}\n{exc}".strip() if response.stderr else str(exc)),
            )
        return parsed, response

    async def research(self, prompt: str) -> ChatResponse:
        return await self.chat(prompt)

    async def code(
        self,
        prompt: str,
        working_directory: str = ".",
    ) -> ChatResponse:
        if resp := self._abort_if_budget_exhausted():
            return resp

        started = time.perf_counter()
        timeout = self.timeout_sec + 30
        args = [
            self.conductor_path,
            "exec",
            "--with",
            self.conductor_name,
            "--model",
            self.model,
            "--effort",
            str(self.effort),
            "--tools",
            ",".join(sorted(_AGENTIC_TOOLS)),
            "--sandbox",
            "workspace-write",
            "--cwd",
            working_directory,
            "--timeout",
            str(self.timeout_sec),
            "--json",
            "--silent-route",
        ]
        try:
            result = await asyncio.to_thread(
                self._run,
                args,
                prompt=prompt,
                timeout=timeout,
            )
            response = self._map_completed_process(result)
            self._journal_call(started, response)
            return response
        except ProviderError as exc:
            response = exc.response or ChatResponse(
                content=f"Error: {exc}",
                model=self.model,
                provider=self.name,
                stderr=str(exc),
                is_error=True,
            )
            self._journal_call(started, response, error="provider error")
            raise

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

    def detect(self) -> ProviderStatus:
        try:
            result = self._run(
                [self.conductor_path, "list", "--json"],
                timeout=30,
            )
        except ProviderError as exc:
            return ProviderStatus(
                installed=True,
                authenticated=False,
                install_hint=_INSTALL_HINTS[self.provider_name],
                auth_hint=str(exc),
            )

        if result.returncode != 0:
            return ProviderStatus(
                installed=True,
                authenticated=False,
                install_hint=_INSTALL_HINTS[self.provider_name],
                auth_hint=result.stderr or f"conductor list exited {result.returncode}",
            )

        parsed = parse_json_safe(result.stdout)
        if not isinstance(parsed, list):
            return ProviderStatus(
                installed=True,
                authenticated=False,
                install_hint=_INSTALL_HINTS[self.provider_name],
                auth_hint="conductor list --json emitted malformed JSON",
            )

        provider_data = next(
            (
                item
                for item in parsed
                if isinstance(item, dict) and item.get("provider") == self.conductor_name
            ),
            None,
        )
        if provider_data is None:
            return ProviderStatus(
                installed=True,
                authenticated=False,
                models=_MODEL_LISTS[self.provider_name],
                install_hint=_INSTALL_HINTS[self.provider_name],
                auth_hint=f"conductor did not list provider {self.conductor_name}",
            )

        default_model = provider_data.get("default_model")
        models = list(_MODEL_LISTS[self.provider_name])
        if isinstance(default_model, str) and default_model and default_model not in models:
            models.append(default_model)
        reason = provider_data.get("reason")
        fix_command = provider_data.get("fix_command")
        return ProviderStatus(
            installed=True,
            authenticated=bool(provider_data.get("configured")),
            models=models,
            install_hint=str(fix_command or _INSTALL_HINTS[self.provider_name]),
            auth_hint=str(reason or _AUTH_HINTS[self.provider_name]),
        )
