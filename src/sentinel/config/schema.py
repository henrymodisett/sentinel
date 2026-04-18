"""
Configuration schema — defines the shape of .sentinel/config.toml

Minimal footprint: just role-to-provider mapping and operational constraints.
Goals are derived from CLAUDE.md/README/GitHub — not stored here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


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
    daily_limit_usd: float = 15.0
    warn_at_usd: float = 10.0


class LocalConfig(BaseModel):
    ollama_endpoint: str = "http://localhost:11434"


class ScanConfig(BaseModel):
    """Configuration for the scan pipeline."""
    max_lenses: int = 10  # max lenses to generate per scan
    evaluate_per_lens: bool = True  # if False, skip individual evaluation step
    # per-LLM-call timeout (raise for large projects / slow networks)
    provider_timeout_sec: int = 600


class CoderConfig(BaseModel):
    """Configuration for the Coder role's agentic execution."""
    # Max tool-use turns Claude Code / Codex can take per work item.
    # 20 was enough for trivial fixes but chokes on security hardening
    # or multi-file refactors — Claude uses ~4 turns just reading files
    # before it edits anything. 40 is a safer default for real work.
    max_turns: int = 40


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
    local: LocalConfig = Field(default_factory=LocalConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    coder: CoderConfig = Field(default_factory=CoderConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
