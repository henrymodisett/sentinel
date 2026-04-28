"""
Configuration schema — defines the shape of .sentinel/config.toml

Minimal footprint: just role-to-provider mapping and operational constraints.
Goals are derived from CLAUDE.md/README/GitHub — not stored here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

# Bounds on the Coder's Claude CLI timeout. Floor keeps a misconfigured
# 5s timeout from silently breaking every cycle; ceiling keeps a typo'd
# 99999 from wedging a cycle for a day. Documented here so the same
# bounds apply at config parse, CLI flag, and env-var resolution points.
CODER_TIMEOUT_MIN_SEC = 60
CODER_TIMEOUT_MAX_SEC = 7200
CODER_TIMEOUT_DEFAULT_SEC = 600


class ProviderName(StrEnum):
    CLAUDE = "claude"
    OPENAI = "openai"
    GEMINI = "gemini"
    LOCAL = "local"


class RoleName(StrEnum):
    MONITOR = "monitor"
    RESEARCHER = "researcher"
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"


ROLE_DESCRIPTIONS: dict[RoleName, str] = {
    RoleName.MONITOR: (
        "Scans your codebase continuously. Detects drift, tracks changes, "
        "assesses state. Runs often — should be cheap."
    ),
    RoleName.RESEARCHER: (
        "Deep research, web search, reads docs and papers. Evaluates "
        "alternatives and best practices. Needs web access and long context."
    ),
    RoleName.PLANNER: (
        "Makes strategic decisions. Prioritizes work, decomposes tasks, "
        "sets architecture direction. Needs the best judgment available."
    ),
    RoleName.CODER: (
        "Writes code, runs tests, executes the plan. Needs agentic tool use "
        "(file editing, terminal, self-correction)."
    ),
    RoleName.REVIEWER: (
        "Verifies completed work. Code review, acceptance criteria checks. "
        "Should ideally be a different provider than coder for independence."
    ),
}


class RoleDefault(BaseModel):
    provider: ProviderName
    model: str


ROLE_DEFAULTS: dict[RoleName, RoleDefault] = {
    RoleName.MONITOR: RoleDefault(provider=ProviderName.LOCAL, model="qwen2.5-coder:14b"),
    RoleName.RESEARCHER: RoleDefault(provider=ProviderName.GEMINI, model="gemini-2.5-pro"),
    RoleName.PLANNER: RoleDefault(provider=ProviderName.CLAUDE, model="claude-opus-4-6"),
    RoleName.CODER: RoleDefault(provider=ProviderName.CLAUDE, model="claude-sonnet-4-6"),
    # Reviewer MUST be a different provider than Coder (Doctrine 0002 —
    # cross-provider review is the invariant, not just cross-model).
    # Defaulting to codex/OpenAI gives genuine independence; falls back
    # to gemini → local → same-as-coder-with-warning when unavailable.
    RoleName.REVIEWER: RoleDefault(provider=ProviderName.OPENAI, model="gpt-5.4"),
}


class RoleConfig(BaseModel):
    provider: ProviderName
    model: str


class BudgetConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    daily_limit_usd: float = 15.0
    warn_at_usd: float = 10.0
    # Rolling-window caps — aggregate cycle costs over the last 24h / 7d.
    # None means no cap. These are independent of daily_limit_usd (which
    # resets at calendar-day midnight); these use rolling windows so a
    # burst of spend at 11:59pm doesn't reset at 12:00am.
    per_day_usd: float | None = Field(
        default=None,
        validation_alias=AliasChoices("per_day", "per_day_usd"),
    )
    per_week_usd: float | None = Field(
        default=None,
        validation_alias=AliasChoices("per_week", "per_week_usd"),
    )


class ScheduleConfig(BaseModel):
    max_runs_per_day: int = Field(default=0, ge=0)
    delivery_webhook: str = ""


class LocalConfig(BaseModel):
    ollama_endpoint: str = "http://localhost:11434"


class ScanConfig(BaseModel):
    """Configuration for the scan pipeline."""

    max_lenses: int = 10  # max lenses to generate per scan
    evaluate_per_lens: bool = True  # if False, skip individual evaluation step
    # per-LLM-call timeout (raise for large projects / slow networks)
    provider_timeout_sec: int = 600


DEFAULT_CLI_HELP_ALLOWLIST: list[str] = [
    "gws",
    "swift",
    "swiftc",
    "xcrun",
    "go",
    "cargo",
    "rustc",
    "node",
    "npm",
    "pnpm",
    "uv",
    "pip",
    "pytest",
    "ruff",
    "mypy",
]


class CoderConfig(BaseModel):
    """Configuration for the Coder role's agentic execution."""

    # Max tool-use turns Claude Code / Codex can take per work item.
    # 20 was enough for trivial fixes but chokes on security hardening
    # or multi-file refactors — Claude uses ~4 turns just reading files
    # before it edits anything. 40 is a safer default for real work.
    max_turns: int = 40

    # Per-call timeout (seconds) for the Coder's agentic CLI invocation.
    # Autumn-mail's first real cycle (2026-04-18, Cortex finding C5) hit
    # the old hard-coded 600s cap on its third revision pass — complex
    # revisions can legitimately run longer. This decouples the Coder
    # from `scan.provider_timeout_sec` so the Coder can be given a
    # longer leash without also lengthening monitor/scan timeouts.
    # Precedence at `sentinel work` time: `--coder-timeout` flag >
    # `SENTINEL_CODER_TIMEOUT` env var > this config > default 600s.
    timeout_seconds: int = Field(
        default=CODER_TIMEOUT_DEFAULT_SEC,
        ge=CODER_TIMEOUT_MIN_SEC,
        le=CODER_TIMEOUT_MAX_SEC,
        description=(
            f"Coder CLI timeout in seconds "
            f"(min {CODER_TIMEOUT_MIN_SEC}, max {CODER_TIMEOUT_MAX_SEC})."
        ),
    )

    # Max coder↔reviewer iterations per work item before sentinel bails
    # with a post-mortem (F8). Default 3 preserves the historical
    # hardcoded value. Cap at 10 — past that, runaway-cost risk
    # outweighs any plausible "the coder will get there eventually"
    # signal. Floor at 1 = no revision (initial pass only).
    max_iterations: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Max coder iterations per work item (1-10).",
    )

    # CLI surface awareness (F7): allowlist of CLI tools whose `--help`
    # output should be pre-loaded into the coder's prompt when the work
    # item references them. Allowlist (not freeform) prevents work-item
    # text from injecting `rm -rf` style "tools" into a shell-out.
    # Empty list disables the feature entirely.
    cli_help_allowlist: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CLI_HELP_ALLOWLIST),
        description=(
            "CLI tool names whose --help text is pre-loaded into the "
            "coder's prompt when the work item references them. Empty "
            "list disables the pre-load entirely."
        ),
    )
    # Cap on detected `<cli> <subcommand> --help` calls per work item.
    # We always probe `<cli> --help` (cheap, single call); subcommand
    # probes are bounded so a long acceptance-criteria block can't
    # explode into dozens of subprocess invocations.
    cli_help_max_subcommands: int = Field(
        default=3,
        ge=0,
        le=20,
        description=(
            "Max `<cli> <subcommand> --help` probes per work item, "
            "in addition to the top-level `<cli> --help`."
        ),
    )
    # Per-help-call subprocess timeout. Fail-soft: a slow tool just
    # gets dropped from the prompt — never blocks the cycle.
    cli_help_timeout_sec: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Timeout for each `<cli> --help` subprocess call.",
    )

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, v: int) -> int:
        # Pydantic's ge/le already clamp at parse time, but we re-assert
        # here with a friendlier error — config validation errors surface
        # directly in `sentinel work` output and the default message
        # ("Input should be greater than or equal to 60") is less
        # actionable than naming the key and the valid range.
        if v < CODER_TIMEOUT_MIN_SEC or v > CODER_TIMEOUT_MAX_SEC:
            raise ValueError(
                f"timeout_seconds={v} out of range "
                f"[{CODER_TIMEOUT_MIN_SEC}, {CODER_TIMEOUT_MAX_SEC}]",
            )
        return v

    @field_validator("cli_help_allowlist")
    @classmethod
    def _validate_allowlist(cls, v: list[str]) -> list[str]:
        # Conservative shape: each entry must be a non-empty string of
        # `[A-Za-z0-9._-]+` (no spaces, slashes, shell metas). The
        # allowlist is ultimately spliced into `subprocess.run([cli, ...])`
        # — list-form `subprocess` is already shell-meta-safe, but a
        # malformed entry like "rm -rf /" would still try to invoke a
        # binary literally named "rm -rf /", failing noisily later. Reject
        # at config load time so the failure is in the right place.
        import re

        pattern = re.compile(r"^[A-Za-z0-9._+-]+$")
        cleaned: list[str] = []
        for entry in v:
            if not isinstance(entry, str):
                raise ValueError(
                    f"cli_help_allowlist entries must be strings; got "
                    f"{type(entry).__name__}: {entry!r}",
                )
            stripped = entry.strip()
            if not stripped:
                continue  # silently drop blanks — common toml authoring slip
            if not pattern.match(stripped):
                raise ValueError(
                    f"cli_help_allowlist entry {stripped!r} contains "
                    f"disallowed characters; only [A-Za-z0-9._+-] permitted.",
                )
            cleaned.append(stripped)
        return cleaned


class RetentionConfig(BaseModel):
    """How long sentinel keeps run-scoped artifacts on disk.

    Long-lived artifacts (`.sentinel/scans/`, `.sentinel/verifications.jsonl`,
    `.sentinel/backlog.md`, `.sentinel/proposals/`) are NOT pruned — they
    represent the project's history. Only `.sentinel/runs/` (the per-cycle
    journals introduced in the run-journal mechanism) ages out.

    Default of 30 days fits a `--every 10m` cadence (~4300 cycles/month)
    without bloating the project. Set to 0 to disable pruning entirely.
    """

    runs_days: int = 30


class RolesConfig(BaseModel):
    monitor: RoleConfig
    researcher: RoleConfig
    planner: RoleConfig
    coder: RoleConfig
    reviewer: RoleConfig


class ProjectConfig(BaseModel):
    name: str
    path: str


class CortexIntegrationConfig(BaseModel):
    """Control over the Cortex T1.6 sentinel-cycle journal integration.

    ``auto`` (default) — write entries iff ``.cortex/`` is present at
    the project root. ``on`` — always write (useful when Cortex is
    managed out-of-tree). ``off`` — never write.

    The CLI flags ``--cortex-journal`` / ``--no-cortex-journal`` on
    ``sentinel work`` override this value per-invocation (flag > config
    > auto-detect). See ``sentinel.integrations.cortex.resolve_enabled``.
    """

    enabled: Literal["auto", "on", "off"] = "auto"


class IntegrationsConfig(BaseModel):
    """Composition with Autumn Garage sibling tools (cortex, touchstone).

    Each sibling is opt-in-by-presence: Sentinel detects the sibling's
    file-contract marker at the repo root and composes automatically.
    These settings are the override knobs for the default auto-detect.
    """

    cortex: CortexIntegrationConfig = Field(default_factory=CortexIntegrationConfig)


class SentinelConfig(BaseModel):
    project: ProjectConfig
    roles: RolesConfig
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    local: LocalConfig = Field(default_factory=LocalConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    coder: CoderConfig = Field(default_factory=CoderConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
