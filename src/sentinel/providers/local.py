"""
Local provider — Ollama via HTTP API.

Auth: none needed.
Chat: POST http://localhost:11434/api/chat
Models: whatever the user has pulled.
"""

from __future__ import annotations

import shutil

import httpx

from sentinel.providers.interface import (
    ChatResponse,
    Provider,
    ProviderCapabilities,
    ProviderName,
    ProviderStatus,
)


class LocalProvider(Provider):
    name = ProviderName.LOCAL
    cli_command = "ollama"
    capabilities = ProviderCapabilities(
        chat=True,
        web_search=False,
        agentic_code=False,
        long_context=False,
        thinking=False,
    )

    def __init__(
        self, model: str = "qwen2.5-coder:14b", endpoint: str = "http://localhost:11434",
    ) -> None:
        self.model = model
        self.endpoint = endpoint

    async def chat(self, prompt: str, system_prompt: str | None = None) -> ChatResponse:
        import time as _time

        if (resp := self._abort_if_budget_exhausted()):
            return resp

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # Return an error ChatResponse on timeout/connection error rather
        # than raising. The CLI providers all do this (TimeoutExpired →
        # error ChatResponse) so scan-failure + partial-persist handling
        # works uniformly; without it, an Ollama timeout would raise a
        # traceback out of Monitor.assess and bypass _persist_scan.
        started = _time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                resp = await client.post(
                    f"{self.endpoint}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                    },
                )
        except httpx.TimeoutException:
            response = ChatResponse(
                content=(
                    f"Error: Ollama HTTP call timed out after "
                    f"{self.timeout_sec}s"
                ),
                provider=self.name,
                stderr=f"(timeout after {self.timeout_sec}s — no response body)",
            )
            self._journal_call(started, response, error="timeout")
            return response
        except httpx.RequestError as e:
            response = ChatResponse(
                content=f"Error: Ollama HTTP call failed: {e}",
                provider=self.name,
                stderr=str(e),
            )
            self._journal_call(started, response, error="request error")
            return response

        if resp.status_code != 200:
            response = ChatResponse(
                content=f"Error: Ollama returned {resp.status_code}",
                provider=self.name,
                stderr=resp.text[:4096],
            )
            self._journal_call(started, response, error=f"http {resp.status_code}")
            return response

        data = resp.json()
        message = data.get("message", {})
        response = ChatResponse(
            content=message.get("content", ""),
            model=self.model,
            provider=self.name,
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            duration_ms=data.get("total_duration", 0) // 1_000_000,  # nanoseconds → ms
        )
        self._journal_call(started, response)
        return response

    def detect(self) -> ProviderStatus:
        path = shutil.which("ollama")
        if not path:
            return ProviderStatus(
                installed=False,
                install_hint="brew install ollama",
                auth_hint="(no auth needed — start with: ollama serve)",
            )

        # Check if Ollama is running and list models
        try:
            resp = httpx.get(f"{self.endpoint}/api/tags", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                models = [m["name"] for m in data.get("models", [])]
                return ProviderStatus(
                    installed=True,
                    authenticated=True,
                    models=models,
                    install_hint="brew install ollama",
                    auth_hint="ollama serve && ollama pull qwen2.5-coder:14b",
                )
        except httpx.ConnectError:
            pass

        return ProviderStatus(
            installed=True,
            authenticated=False,
            install_hint="brew install ollama",
            auth_hint="ollama serve && ollama pull qwen2.5-coder:14b",
        )
