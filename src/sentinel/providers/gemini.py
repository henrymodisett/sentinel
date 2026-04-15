"""
Gemini provider — wraps the Gemini CLI.

Auth: browser OAuth on first run (user handles it).
Chat: `gemini -p "prompt" -o json`
Research: same, but Gemini's built-in Google Search grounding activates automatically.
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


class GeminiProvider(Provider):
    name = ProviderName.GEMINI
    cli_command = "gemini"
    capabilities = ProviderCapabilities(
        chat=True,
        web_search=True,
        agentic_code=False,
        long_context=True,
        thinking=True,
    )

    def __init__(self, model: str = "gemini-2.5-pro") -> None:
        self.model = model

    async def chat(
        self, prompt: str, system_prompt: str | None = None,
    ) -> ChatResponse:
        import time as _time

        from sentinel.budget_ctx import clamp_timeout

        # Gemini CLI doesn't have a --system-prompt flag, so prepend it
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"

        # --approval-mode plan = read-only (no file writes, no edits).
        # Critical for scan safety — without this, Gemini CLI will write
        # files into the target project during lens evaluation.
        args = [
            "gemini", "-p", full_prompt, "-o", "json",
            "--approval-mode", "plan",
        ]
        # Only pass -m if not the default auto model
        if self.model and self.model != "auto":
            args.extend(["-m", self.model])

        # Snapshot clamp state BEFORE the call so elapsed time inside
        # the call doesn't change whether we report it as clamped.
        was_clamped = clamp_timeout(self.timeout_sec) < self.timeout_sec
        started = _time.perf_counter()
        try:
            result = await run_cli_async(
                args, timeout=self.timeout_sec, env=minimal_provider_env(),
            )
        except subprocess.TimeoutExpired:
            response = ChatResponse(
                content=f"Error: Gemini CLI timed out after {self.timeout_sec}s",
                provider=self.name,
            )
            self._journal_call(started, response, was_clamped, error="timeout")
            return response
        if result.returncode != 0:
            response = ChatResponse(
                content=f"Error: {result.stderr.strip()}", provider=self.name,
            )
            self._journal_call(started, response, was_clamped, error="non-zero exit")
            return response

        data = parse_json_safe(result.stdout)
        if not data:
            # Gemini CLI sometimes outputs plain text
            response = ChatResponse(
                content=result.stdout.strip(), provider=self.name, model=self.model,
            )
            self._journal_call(started, response, was_clamped)
            return response

        # Extract token counts from stats
        stats = data.get("stats", {})
        total_input = 0
        total_output = 0
        for model_stats in stats.get("models", {}).values():
            tokens = model_stats.get("tokens", {})
            total_input += tokens.get("input", 0)
            total_output += tokens.get("candidates", 0)

        response = ChatResponse(
            content=data.get("response", ""),
            model=self.model,
            provider=self.name,
            input_tokens=total_input,
            output_tokens=total_output,
            session_id=data.get("session_id"),
        )
        self._journal_call(started, response, was_clamped)
        return response

    async def research(self, prompt: str) -> ChatResponse:
        """Gemini with Google Search grounding — activates automatically."""
        return await self.chat(prompt)

    def detect(self) -> ProviderStatus:
        path = shutil.which("gemini")
        if not path:
            return ProviderStatus(
                installed=False,
                install_hint="npm install -g @google/gemini-cli",
                auth_hint="gemini (authenticates via browser on first run)",
            )
        # Check version to confirm it runs
        result = run_cli(["gemini", "--version"], timeout=10)
        installed = result.returncode == 0

        return ProviderStatus(
            installed=installed,
            authenticated=installed,
            models=["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
            install_hint="npm install -g @google/gemini-cli",
            auth_hint="gemini (authenticates via browser on first run)",
        )
