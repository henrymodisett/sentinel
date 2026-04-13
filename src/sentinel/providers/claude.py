"""
Claude provider — wraps the Claude Code CLI.

Auth: user runs `claude login` themselves.
Chat: `claude -p "prompt" --output-format json --bare`
Code: `claude -p "prompt" --output-format json` (full agentic mode with tools)
"""

from __future__ import annotations

import shutil

from sentinel.providers.interface import (
    ChatResponse,
    Provider,
    ProviderCapabilities,
    ProviderName,
    ProviderStatus,
    parse_json_safe,
    run_cli,
)


class ClaudeProvider(Provider):
    name = ProviderName.CLAUDE
    cli_command = "claude"
    capabilities = ProviderCapabilities(
        chat=True,
        web_search=True,
        agentic_code=True,
        long_context=True,
        thinking=True,
    )

    def __init__(self, model: str = "sonnet") -> None:
        self.model = model

    async def chat(self, prompt: str, system_prompt: str | None = None) -> ChatResponse:
        args = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--bare",
            "--model", self.model,
            "--no-session-persistence",
        ]
        if system_prompt:
            args.extend(["--system-prompt", system_prompt])

        result = run_cli(args)
        if result.returncode != 0:
            return ChatResponse(content=f"Error: {result.stderr}", provider=self.name)

        data = parse_json_safe(result.stdout)
        if not data:
            return ChatResponse(content=result.stdout, provider=self.name)

        usage = data.get("usage", {})
        return ChatResponse(
            content=data.get("result", ""),
            model=self.model,
            provider=self.name,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=data.get("total_cost_usd", 0.0),
            duration_ms=data.get("duration_ms", 0),
            session_id=data.get("session_id"),
        )

    async def code(self, prompt: str, working_directory: str = ".") -> ChatResponse:
        """Full agentic Claude Code — file editing, terminal, tests."""
        args = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--model", self.model,
            "--max-turns", "20",
            "--no-session-persistence",
        ]
        result = run_cli(args, timeout=600)
        if result.returncode != 0:
            return ChatResponse(content=f"Error: {result.stderr}", provider=self.name)

        data = parse_json_safe(result.stdout)
        if not data:
            return ChatResponse(content=result.stdout, provider=self.name)

        usage = data.get("usage", {})
        return ChatResponse(
            content=data.get("result", ""),
            model=self.model,
            provider=self.name,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=data.get("total_cost_usd", 0.0),
            duration_ms=data.get("duration_ms", 0),
            session_id=data.get("session_id"),
        )

    def detect(self) -> ProviderStatus:
        path = shutil.which("claude")
        if not path:
            return ProviderStatus(
                installed=False,
                install_hint="brew install claude",
                auth_hint="claude login",
            )
        # Check if authenticated by running a minimal command
        result = run_cli(["claude", "-p", "reply with OK", "--bare", "--output-format", "json",
                          "--no-session-persistence", "--max-turns", "1"], timeout=30)
        authenticated = result.returncode == 0

        return ProviderStatus(
            installed=True,
            authenticated=authenticated,
            models=["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
            install_hint="brew install claude",
            auth_hint="claude login",
        )
