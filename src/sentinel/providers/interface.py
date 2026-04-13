"""
Provider interface — the unified abstraction across all LLM providers.

Every provider wraps a CLI tool (claude, codex, gemini) or HTTP API (ollama).
Sentinel never touches API keys — each CLI handles its own authentication.

Design decisions:
- chat() is the universal primitive — send a prompt, get a response
- research() adds web search capability (Gemini grounding, Claude web search)
- code() adds agentic code execution (Claude Code, Codex full-auto mode)
- Providers declare their capabilities so the router can warn about mismatches
- All cloud providers use CLI subprocesses; Ollama uses its local HTTP API
"""

from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum


class ProviderName(StrEnum):
    CLAUDE = "claude"
    OPENAI = "openai"
    GEMINI = "gemini"
    LOCAL = "local"


@dataclass
class ChatResponse:
    content: str
    model: str = ""
    provider: ProviderName = ProviderName.CLAUDE
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    session_id: str | None = None


@dataclass
class ProviderCapabilities:
    chat: bool = True
    web_search: bool = False
    agentic_code: bool = False
    long_context: bool = False
    thinking: bool = False


@dataclass
class ProviderStatus:
    installed: bool = False
    authenticated: bool = False
    version: str | None = None
    models: list[str] = field(default_factory=list)
    install_hint: str = ""
    auth_hint: str = ""


def run_cli(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run a CLI command and return the result."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def parse_json_safe(text: str) -> dict | None:
    """Parse JSON from CLI output, handling trailing garbage (Gemini CLI issue)."""
    text = text.strip()
    if not text:
        return None
    # Try full text first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the first complete JSON object (handles trailing hook output)
    depth = 0
    start = text.find("{")
    if start == -1:
        return None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class Provider(ABC):
    """Base class for all LLM providers."""

    name: ProviderName
    capabilities: ProviderCapabilities
    cli_command: str  # the CLI binary name

    @abstractmethod
    async def chat(self, prompt: str, system_prompt: str | None = None) -> ChatResponse:
        """Send a prompt, get a response."""

    async def research(self, prompt: str) -> ChatResponse:
        """Chat with web search grounding. Falls back to regular chat."""
        return await self.chat(prompt)

    async def code(self, prompt: str, working_directory: str = ".") -> ChatResponse:
        """Agentic code execution. Only Claude and Codex support this."""
        raise NotImplementedError(
            f"Provider {self.name} does not support agentic code execution."
        )

    @abstractmethod
    def detect(self) -> ProviderStatus:
        """Check if the CLI is installed and authenticated."""
