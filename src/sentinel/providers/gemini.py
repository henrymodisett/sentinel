"""
Gemini provider — wraps the Gemini CLI.

Auth: browser OAuth on first run (user handles it).
Chat: `gemini -p "prompt" -o json`
Research: same, but Gemini's built-in Google Search grounding activates automatically.
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
        # Gemini CLI doesn't have a --system-prompt flag, so prepend it
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"

        args = ["gemini", "-p", full_prompt, "-o", "json"]
        # Only pass -m if not the default auto model
        if self.model and self.model != "auto":
            args.extend(["-m", self.model])

        result = run_cli(args, timeout=180)
        if result.returncode != 0:
            return ChatResponse(
                content=f"Error: {result.stderr.strip()}", provider=self.name,
            )

        data = parse_json_safe(result.stdout)
        if not data:
            # Gemini CLI sometimes outputs plain text
            return ChatResponse(
                content=result.stdout.strip(), provider=self.name, model=self.model,
            )

        # Extract token counts from stats
        stats = data.get("stats", {})
        total_input = 0
        total_output = 0
        for model_stats in stats.get("models", {}).values():
            tokens = model_stats.get("tokens", {})
            total_input += tokens.get("input", 0)
            total_output += tokens.get("candidates", 0)

        return ChatResponse(
            content=data.get("response", ""),
            model=self.model,
            provider=self.name,
            input_tokens=total_input,
            output_tokens=total_output,
            session_id=data.get("session_id"),
        )

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
