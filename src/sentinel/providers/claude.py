"""
Claude provider — wraps the Claude Code CLI.

Auth: user runs `claude login` themselves.
Chat: `claude -p "prompt" --output-format json --bare`
Code: `claude -p "prompt" --output-format json` (full agentic mode with tools)
"""

from __future__ import annotations

import shutil
import subprocess

from sentinel.providers.interface import (
    ChatResponse,
    Provider,
    ProviderCapabilities,
    ProviderName,
    ProviderStatus,
    minimal_provider_env,
    parse_json_safe,
    run_cli,
    run_cli_async,
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
        # chat() is read-only — no tools, no file writes, no terminal.
        # For agentic code execution with full tools, use code() instead.
        args = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--model", self.model,
            "--no-session-persistence",
            "--disallowedTools", "Bash,Edit,Write,NotebookEdit",
        ]
        if system_prompt:
            args.extend(["--system-prompt", system_prompt])

        try:
            result = await run_cli_async(
                args, timeout=self.timeout_sec, env=minimal_provider_env(),
            )
        except subprocess.TimeoutExpired:
            return ChatResponse(
                content=f"Error: Claude CLI timed out after {self.timeout_sec}s",
                provider=self.name,
            )
        if result.returncode != 0:
            return ChatResponse(
                content=f"Error: {result.stderr.strip()}", provider=self.name,
            )

        data = parse_json_safe(result.stdout)
        if not data:
            return ChatResponse(content=result.stdout, provider=self.name)

        # Claude CLI returns is_error=true for auth failures etc.
        if data.get("is_error"):
            return ChatResponse(
                content=f"Error: {data.get('result', 'unknown error')}",
                provider=self.name,
            )

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

    # Note: we tried Claude CLI's --json-schema flag but it hangs indefinitely
    # at 0% CPU. Falling back to base Provider.chat_json() which uses prompt-
    # based enforcement + parse_json_safe (works reliably).

    async def code(self, prompt: str, working_directory: str = ".") -> ChatResponse:
        """Full agentic Claude Code — file editing, terminal, tests."""
        args = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--model", self.model,
            "--max-turns", "20",
            "--no-session-persistence",
        ]
        try:
            result = await run_cli_async(
                args, timeout=self.timeout_sec, env=minimal_provider_env(),
            )
        except subprocess.TimeoutExpired:
            return ChatResponse(
                content=f"Error: Claude CLI timed out after {self.timeout_sec}s",
                provider=self.name,
            )
        if result.returncode != 0:
            return ChatResponse(
                content=f"Error: {result.stderr.strip()}", provider=self.name,
            )

        data = parse_json_safe(result.stdout)
        if not data:
            return ChatResponse(content=result.stdout, provider=self.name)

        if data.get("is_error"):
            return ChatResponse(
                content=f"Error: {data.get('result', 'unknown error')}",
                provider=self.name,
            )

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
        # Just check if the binary runs — don't make an API call during detection
        result = run_cli(["claude", "--version"], timeout=10)
        installed = result.returncode == 0

        return ProviderStatus(
            installed=installed,
            authenticated=installed,  # trust that if the CLI works, user has authed
            models=["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
            install_hint="brew install claude",
            auth_hint="claude login",
        )
