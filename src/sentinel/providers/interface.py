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
    # Raw stderr captured from the underlying CLI. Always populated when
    # the provider has it, regardless of success or failure — this is
    # what makes empty-error Coder runs debuggable post-hoc.
    stderr: str = ""
    # Raw stdout — useful when the provider's error-path blanks out
    # `content` but the CLI actually printed something meaningful.
    raw_stdout: str = ""
    # True iff the underlying CLI reported an error (non-zero exit,
    # is_error JSON field, or timeout).
    is_error: bool = False


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
    """Run a CLI command and return the result (blocking)."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def run_cli_async(
    args: list[str], timeout: int = 300, env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a CLI command asynchronously.

    env: optional environment dict. If None, inherits parent env (most CLIs
    need this for auth tokens in macOS keychain/config dirs). Providers
    should pass a minimal env to reduce secret leakage into prompts.
    """
    import asyncio as _asyncio

    process = await _asyncio.create_subprocess_exec(
        *args,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
        env=env,  # None means inherit
    )

    try:
        stdout_bytes, stderr_bytes = await _asyncio.wait_for(
            process.communicate(), timeout=timeout,
        )
    except TimeoutError as e:
        process.kill()
        await process.wait()
        raise subprocess.TimeoutExpired(args, timeout) from e

    return subprocess.CompletedProcess(
        args=args,
        returncode=process.returncode or 0,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
    )


def minimal_provider_env(preserve: list[str] | None = None) -> dict:
    """Build a minimal env dict for provider subprocesses.

    Includes only what's needed for CLI auth + process execution, not
    the full user env (which could contain unrelated secrets that end
    up in logs or prompts).

    preserve: extra env var names to pass through (e.g. ['ANTHROPIC_API_KEY'])
    """
    import os as _os

    # Base: things CLIs genuinely need to run
    safe_keys = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR",
        "LANG", "LC_ALL", "LC_CTYPE",
        # macOS keychain access
        "SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
        # Homebrew paths (for CLIs installed via brew)
        "HOMEBREW_PREFIX", "HOMEBREW_CELLAR", "HOMEBREW_REPOSITORY",
        # Node/npm (Gemini CLI is Node-based)
        "NODE_PATH", "NPM_CONFIG_PREFIX",
    }
    if preserve:
        safe_keys.update(preserve)

    return {k: v for k, v in _os.environ.items() if k in safe_keys}


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

    # Per-call timeout (seconds). Set by Router from config.scan.provider_timeout_sec.
    # Can be overridden per provider-instance. Default kept high for safety.
    timeout_sec: int = 600

    @abstractmethod
    async def chat(self, prompt: str, system_prompt: str | None = None) -> ChatResponse:
        """Send a prompt, get a response."""

    async def chat_json(
        self, prompt: str, schema: dict, system_prompt: str | None = None,
    ) -> tuple[dict | None, ChatResponse]:
        """Send a prompt, get a structured JSON response validated against schema.

        Default implementation: prompt-based enforcement + safe parsing.
        Providers should override with native schema support where available
        (Claude --json-schema, Codex --output-schema).

        Returns (parsed_data_or_None, full_response). Caller can inspect
        the response for cost/tokens even if parsing failed.
        """
        import json as _json
        schema_str = _json.dumps(schema, indent=2)
        strict_prompt = (
            f"{prompt}\n\n"
            f"CRITICAL: Respond with ONLY a JSON object matching this schema. "
            f"No prose outside the JSON, no markdown fences, no explanation. "
            f"Just valid JSON starting with {{ and ending with }}.\n\n"
            f"Schema:\n{schema_str}"
        )
        response = await self.chat(strict_prompt, system_prompt)
        parsed = parse_json_safe(response.content)
        return parsed, response

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
