"""
Provider interface — the unified abstraction used by Sentinel roles.

The concrete implementation delegates to Conductor, which wraps CLI tools
(claude, codex, gemini) and HTTP/local providers (ollama, kimi). Sentinel
never touches provider API keys.

Design decisions:
- chat() is the universal primitive — send a prompt, get a response
- research() adds web search capability (Gemini grounding, Claude web search)
- code() adds agentic code execution (Claude Code, Codex full-auto mode)
- Providers declare their capabilities so the router can warn about mismatches
- Conductor owns provider-specific subprocess / HTTP execution
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
    KIMI = "kimi"


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
    args: list[str],
    timeout: int = 300,
    env: dict | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a CLI command asynchronously.

    env: optional environment dict. If None, inherits parent env (most CLIs
    need this for auth tokens in macOS keychain/config dirs). Providers
    should pass a minimal env to reduce secret leakage into prompts.
    cwd: optional working directory. If None, inherits the caller's cwd.
    The Coder path MUST pass the target project path here so Claude Code
    edits land in the target, not in the sentinel process cwd.

    timeout is used as-is. The cycle budget is enforced between calls
    (see sentinel.budget_ctx.is_budget_exhausted) — providers skip the
    call entirely when budget is out, rather than shrinking this
    subprocess timeout to fit. Shrinking mid-call kills in-flight work
    and returns zero output for the latency cost; between-call gating
    lets every started call finish naturally.
    """
    import asyncio as _asyncio

    process = await _asyncio.create_subprocess_exec(
        *args,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
        env=env,  # None means inherit
        cwd=cwd,  # None means inherit
    )

    try:
        stdout_bytes, stderr_bytes = await _asyncio.wait_for(
            process.communicate(), timeout=timeout,
        )
    except TimeoutError as e:
        process.kill()
        await process.wait()
        raise subprocess.TimeoutExpired(args, timeout) from e
    except _asyncio.CancelledError:
        # Outer cancellation (e.g., a parent asyncio.wait_for timed out,
        # or the task was cancelled by KeyboardInterrupt handling). The
        # subprocess does NOT die automatically — touchstone dogfood caught
        # this as a process that kept running for 13+ minutes after its
        # asyncio parent gave up. Kill it explicitly, then propagate the
        # cancellation so the caller sees the expected CancelledError.
        import contextlib
        process.kill()
        # wait() itself can be re-cancelled during shutdown; safe to
        # suppress since we already sent SIGKILL above.
        with contextlib.suppress(_asyncio.CancelledError):
            await process.wait()
        raise

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

    # Max tool-use turns the Coder-role provider can run before bailing.
    # Only the agentic code() path reads this today. Set by Router from
    # config.coder.max_turns when present.
    max_turns: int = 40

    @abstractmethod
    async def chat(self, prompt: str, system_prompt: str | None = None) -> ChatResponse:
        """Send a prompt, get a response."""

    def _abort_if_budget_exhausted(self) -> ChatResponse | None:
        """Return a budget-exhausted error response if the cycle deadline
        has passed, else None. Every chat() / code() entry point should
        call this first:

            if (resp := self._abort_if_budget_exhausted()):
                return resp

        The skip is recorded in the journal as a 0-latency call with
        error="budget_exhausted" so the journal naturally shows which
        calls were dropped (not just which ones ran).
        """
        from sentinel.budget_ctx import is_budget_exhausted

        if not is_budget_exhausted():
            return None
        import time as _time
        started = _time.perf_counter()
        response = ChatResponse(
            content="Error: cycle budget exhausted before this call could start",
            provider=self.name,
            is_error=True,
        )
        self._journal_call(started, response, error="budget_exhausted")
        return response

    def _journal_call(
        self,
        started_at: float,
        response: ChatResponse,
        error: str | None = None,
    ) -> None:
        """Append this call to the cycle's run journal (no-op outside a
        cycle). Subclasses call this once at the end of every chat() /
        code() path so the journal sees latency, tokens, cost, and any
        captured stderr/error context.
        """
        import time as _time

        from sentinel.journal import record_provider_call

        latency_ms = int((_time.perf_counter() - started_at) * 1000)
        record_provider_call(
            provider=str(self.name),
            model=response.model or getattr(self, "model", "") or "",
            latency_ms=latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            error=error,
            stderr=response.stderr,
        )

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
