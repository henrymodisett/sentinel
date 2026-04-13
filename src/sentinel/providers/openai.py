"""
OpenAI provider — wraps the Codex CLI.

Auth: user runs `codex login` themselves.
Chat: `codex exec "prompt" --json`
Code: `codex exec "prompt" --json --full-auto`
"""

from __future__ import annotations

import json
import shutil

from sentinel.providers.interface import (
    ChatResponse,
    Provider,
    ProviderCapabilities,
    ProviderName,
    ProviderStatus,
    run_cli,
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

    async def chat(self, prompt: str, system_prompt: str | None = None) -> ChatResponse:
        args = ["codex", "exec", prompt, "--json", "--ephemeral"]
        result = run_cli(args)
        if result.returncode != 0:
            return ChatResponse(content=f"Error: {result.stderr}", provider=self.name)

        # Parse NDJSON — find the last agent_message item
        content = ""
        total_input = 0
        total_output = 0
        for line in result.stdout.strip().splitlines():
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

        return ChatResponse(
            content=content,
            model=self.model,
            provider=self.name,
            input_tokens=total_input,
            output_tokens=total_output,
        )

    async def code(self, prompt: str, working_directory: str = ".") -> ChatResponse:
        args = [
            "codex", "exec", prompt,
            "--json", "--full-auto",
            "-C", working_directory,
        ]
        result = run_cli(args, timeout=600)
        if result.returncode != 0:
            return ChatResponse(content=f"Error: {result.stderr}", provider=self.name)

        content = ""
        total_input = 0
        total_output = 0
        for line in result.stdout.strip().splitlines():
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

        return ChatResponse(
            content=content,
            model=self.model,
            provider=self.name,
            input_tokens=total_input,
            output_tokens=total_output,
        )

    def detect(self) -> ProviderStatus:
        path = shutil.which("codex")
        if not path:
            return ProviderStatus(
                installed=False,
                install_hint="npm install -g @openai/codex",
                auth_hint="codex login",
            )
        return ProviderStatus(
            installed=True,
            authenticated=True,  # codex exec will fail if not authed
            models=["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "o4-mini"],
            install_hint="npm install -g @openai/codex",
            auth_hint="codex login",
        )
