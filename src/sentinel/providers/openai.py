"""
OpenAI provider — wraps the Codex CLI.

Auth: user runs `codex login` themselves.
Chat: `codex exec "prompt" --json`
Code: `codex exec "prompt" --json --full-auto`
"""

from __future__ import annotations

import json
import shutil
import subprocess

from sentinel.providers.interface import (
    ChatResponse,
    Provider,
    ProviderCapabilities,
    ProviderName,
    ProviderStatus,
    minimal_provider_env,
    run_cli,
    run_cli_async,
)


class OpenAIProvider(Provider):
    name = ProviderName.OPENAI
    cli_command = "codex"
    capabilities = ProviderCapabilities(
        chat=True,
        web_search=True,
        agentic_code=True,
        long_context=True,
        thinking=True,
    )

    def __init__(self, model: str = "gpt-5.4") -> None:
        self.model = model

    def _parse_ndjson(self, stdout: str) -> tuple[str, int, int]:
        """Parse Codex NDJSON output. Returns (content, input_tokens, output_tokens)."""
        content = ""
        total_input = 0
        total_output = 0
        for line in stdout.strip().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    content = item.get("text", "")
            elif event.get("type") == "turn.completed":
                usage = event.get("usage", {})
                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)
        return content, total_input, total_output

    async def chat(
        self, prompt: str, system_prompt: str | None = None,
    ) -> ChatResponse:
        import time as _time

        if (resp := self._abort_if_budget_exhausted()):
            return resp

        # Codex CLI has no --system-prompt flag; prepend to user prompt
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"
        args = ["codex", "exec", full_prompt, "--json", "--ephemeral"]
        started = _time.perf_counter()
        try:
            result = await run_cli_async(
                args, timeout=self.timeout_sec, env=minimal_provider_env(),
            )
        except subprocess.TimeoutExpired:
            response = ChatResponse(
                content=f"Error: Codex CLI timed out after {self.timeout_sec}s",
                provider=self.name,
                stderr=f"(timeout after {self.timeout_sec}s — no stderr captured)",
            )
            self._journal_call(started, response, error="timeout")
            return response

        stderr = result.stderr or ""
        stdout = result.stdout or ""

        if result.returncode != 0:
            response = ChatResponse(
                content=f"Error: {stderr.strip()}",
                provider=self.name,
                stderr=stderr,
                raw_stdout=stdout,
            )
            self._journal_call(started, response, error="non-zero exit")
            return response

        content, total_input, total_output = self._parse_ndjson(stdout)
        response = ChatResponse(
            content=content,
            model=self.model,
            provider=self.name,
            input_tokens=total_input,
            output_tokens=total_output,
            stderr=stderr,
            raw_stdout=stdout,
        )
        self._journal_call(started, response)
        return response

    async def code(
        self, prompt: str, working_directory: str = ".",
    ) -> ChatResponse:
        import time as _time

        if (resp := self._abort_if_budget_exhausted()):
            return resp

        args = [
            "codex", "exec", prompt,
            "--json", "--full-auto",
            "-C", working_directory,
        ]
        started = _time.perf_counter()
        try:
            result = await run_cli_async(
                args, timeout=self.timeout_sec, env=minimal_provider_env(),
            )
        except subprocess.TimeoutExpired:
            response = ChatResponse(
                content=f"Error: Codex CLI timed out after {self.timeout_sec}s",
                provider=self.name,
                stderr=f"(timeout after {self.timeout_sec}s — no stderr captured)",
            )
            self._journal_call(started, response, error="timeout")
            return response

        stderr = result.stderr or ""
        stdout = result.stdout or ""

        if result.returncode != 0:
            response = ChatResponse(
                content=f"Error: {stderr.strip()}",
                provider=self.name,
                stderr=stderr,
                raw_stdout=stdout,
            )
            self._journal_call(started, response, error="non-zero exit")
            return response

        content, total_input, total_output = self._parse_ndjson(stdout)
        response = ChatResponse(
            content=content,
            model=self.model,
            provider=self.name,
            input_tokens=total_input,
            output_tokens=total_output,
            stderr=stderr,
            raw_stdout=stdout,
        )
        self._journal_call(started, response)
        return response

    def detect(self) -> ProviderStatus:
        path = shutil.which("codex")
        if not path:
            return ProviderStatus(
                installed=False,
                install_hint="npm install -g @openai/codex",
                auth_hint="codex login",
            )
        result = run_cli(["codex", "--version"], timeout=10)
        installed = result.returncode == 0

        return ProviderStatus(
            installed=installed,
            authenticated=installed,
            models=["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "o4-mini"],
            install_hint="npm install -g @openai/codex",
            auth_hint="codex login",
        )
