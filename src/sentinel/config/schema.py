"""
Configuration schema — defines the shape of .sentinel/config.toml

Minimal footprint: just role-to-provider mapping and operational constraints.
Goals are derived from CLAUDE.md/README/GitHub — not stored here.
"""

from __future__ import annotations

from enum import StrEnum

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
    RoleName.REVIEWER: RoleDefault(provider=ProviderName.GEMINI, model="gemini-2.5-pro"),
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


class RolesConfig(BaseModel):
    monitor: RoleConfig
    researcher: RoleConfig
    planner: RoleConfig
    coder: RoleConfig
    reviewer: RoleConfig


class ProjectConfig(BaseModel):
    name: str
    path: str


class SentinelConfig(BaseModel):
    project: ProjectConfig
    roles: RolesConfig
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    local: LocalConfig = Field(default_factory=LocalConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
