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
        web_search=True,  # native Google Search grounding
        agentic_code=False,
        long_context=True,
        thinking=True,
    )

    def __init__(self, model: str = "gemini-2.5-pro") -> None:
        self.model = model

    async def chat(self, prompt: str, system_prompt: str | None = None) -> ChatResponse:
        args = ["gemini", "-p", prompt, "-o", "json", "-m", self.model]
        result = run_cli(args)
        if result.returncode != 0:
            return ChatResponse(content=f"Error: {result.stderr}", provider=self.name)

        data = parse_json_safe(result.stdout)
        if not data:
            return ChatResponse(content=result.stdout.strip(), provider=self.name)

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
        # Gemini CLI's built-in extensions include Google Search.
        # The model decides when to search based on the query.
        return await self.chat(prompt)

    def detect(self) -> ProviderStatus:
        path = shutil.which("gemini")
        if not path:
            return ProviderStatus(
                installed=False,
                install_hint="npm install -g @google/gemini-cli",
                auth_hint="gemini (authenticates via browser on first run)",
            )
        return ProviderStatus(
            installed=True,
            authenticated=True,  # OAuth happens on first use
            models=["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
            install_hint="npm install -g @google/gemini-cli",
            auth_hint="gemini (authenticates via browser on first run)",
        )
