"""
Router — translates Sentinel role calls into Conductor routing requests.

For normal runtime calls, Sentinel describes the task intent (`quick`,
`research`, `plan`, `code`, `review`, or `chat`) and Conductor chooses the
right provider/model from configured backends. This keeps provider-specific
selection policy in Conductor rather than duplicating it in Sentinel.

The legacy `.sentinel/config.toml` role defaults are still materialized for
backward compatibility and for callers that do not pass an intent. The
`DEFAULT_RULES` table remains as a compatibility layer for those static
calls, but intent-based paths intentionally bypass Sentinel-side model
overrides and delegate selection to Conductor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.console import Console

from sentinel.config.schema import RoleName, SentinelConfig
from sentinel.providers.conductor_adapter import ConductorAdapter
from sentinel.providers.interface import (  # noqa: TCH001
    ChatResponse,
    Provider,
    ProviderStatus,
)

_console = Console()

TaskIntent = Literal["quick", "research", "plan", "code", "review", "chat"]

_AGENTIC_TOOLS = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})


@dataclass(frozen=True)
class IntentSpec:
    """Conductor routing constraints for a Sentinel task intent."""

    tags: tuple[str, ...]
    prefer: str
    effort: str | int
    tools: frozenset[str]
    sandbox: str


INTENT_SPECS: dict[str, IntentSpec] = {
    # "Quick" means local/offline if available. Balanced preserves tag
    # dominance so Ollama beats faster cloud models when it is configured.
    "quick": IntentSpec(
        tags=("offline", "local"),
        prefer="balanced",
        effort="minimal",
        tools=frozenset(),
        sandbox="none",
    ),
    # Research must prefer actual web-search capability over raw tier.
    "research": IntentSpec(
        tags=("web-search", "long-context"),
        prefer="balanced",
        effort="medium",
        tools=frozenset(),
        sandbox="none",
    ),
    "plan": IntentSpec(
        tags=("strong-reasoning", "long-context"),
        prefer="best",
        effort="high",
        tools=frozenset(),
        sandbox="read-only",
    ),
    "code": IntentSpec(
        tags=("tool-use", "strong-reasoning"),
        prefer="best",
        effort="medium",
        tools=_AGENTIC_TOOLS,
        sandbox="workspace-write",
    ),
    "review": IntentSpec(
        tags=("code-review", "strong-reasoning"),
        prefer="best",
        effort="medium",
        tools=frozenset(),
        sandbox="read-only",
    ),
    "chat": IntentSpec(
        tags=(),
        prefer="balanced",
        effort="medium",
        tools=frozenset(),
        sandbox="none",
    ),
}


def _conductor_pick(
    spec: IntentSpec,
    *,
    exclude: frozenset[str] = frozenset(),
):
    from conductor.router import pick

    return pick(
        list(spec.tags),
        prefer=spec.prefer,
        effort=spec.effort,
        tools=spec.tools,
        sandbox=spec.sandbox,
        exclude=exclude,
    )


@dataclass(frozen=True)
class RoutingRule:
    """A static routing rule. Matches certain (role, task, prompt_size,
    configured_provider) contexts and overrides the model. Encoded as
    data so additions don't require new code paths."""
    name: str
    task: str | None  # None = match any task
    role: RoleName | None  # None = match any role
    min_prompt_size: int  # 0 = no minimum
    only_for_provider: str | None  # None = any provider
    override_model: str
    reason: str

    def matches(
        self,
        role: RoleName,
        task: str | None,
        prompt_size: int,
        configured_provider: str,
    ) -> bool:
        if self.task is not None and self.task != task:
            return False
        if self.role is not None and self.role != role:
            return False
        if prompt_size < self.min_prompt_size:
            return False
        return not (
            self.only_for_provider is not None
            and self.only_for_provider != configured_provider
        )


# Each rule encodes a real failure mode observed in dogfood. Add new
# rules here when a future dogfood reveals another (provider, task)
# pair that warrants override. Order matters — first match wins, so
# put more specific rules first.
DEFAULT_RULES: tuple[RoutingRule, ...] = (
    RoutingRule(
        name="huge-eval-prefers-flash",
        task="evaluate_lens",
        role=None,
        min_prompt_size=60_000,
        only_for_provider="gemini",
        override_model="gemini-2.5-flash",
        reason="gemini-2.5-pro fails non-zero on prompts > 60k tokens (dogfood 2026-04-16)",
    ),
    RoutingRule(
        name="synthesize-prefers-pro",
        task="synthesize",
        role=None,
        min_prompt_size=0,
        only_for_provider="gemini",
        override_model="gemini-2.5-pro",
        reason="gemini-2.5-flash times out on cross-lens synthesis (dogfood 2026-04-16)",
    ),
)


def _create_provider(
    provider_name: str,
    model: str,
    config: SentinelConfig,
    *,
    timeout_sec: int,
    max_turns: int,
) -> Provider:
    return ConductorAdapter(
        provider_name=provider_name,  # type: ignore[arg-type]
        model=model,
        timeout_sec=timeout_sec,
        max_turns=max_turns,
        ollama_endpoint=config.local.ollama_endpoint,
    )


class Router:
    def __init__(
        self,
        config: SentinelConfig,
        rules: tuple[RoutingRule, ...] = DEFAULT_RULES,
    ) -> None:
        self._config = config
        self._rules = rules
        self._providers: dict[str, Provider] = {}
        self._role_map: dict[RoleName, Provider] = {}
        self._init_from_config(config)

    def _init_from_config(self, config: SentinelConfig) -> None:
        roles = {
            RoleName.MONITOR: config.roles.monitor,
            RoleName.RESEARCHER: config.roles.researcher,
            RoleName.PLANNER: config.roles.planner,
            RoleName.CODER: config.roles.coder,
            RoleName.REVIEWER: config.roles.reviewer,
        }
        for role_name, role_config in roles.items():
            provider = self._materialize(
                role_config.provider.value, role_config.model, role=role_name,
            )
            self._role_map[role_name] = provider

    def _materialize(
        self,
        provider_name: str,
        model: str,
        *,
        role: RoleName | None = None,
    ) -> Provider:
        """Return a Provider instance for (provider_name, model), cached
        across the router's lifetime.

        Normally the same (provider, model) pair is a single shared
        instance — memory-efficient and lets us set max_turns / timeout
        once on the instance. Exception: the Coder role owns its own
        timeout (`config.coder.timeout_seconds`, per Cortex C5) which
        can legitimately differ from the scan timeout. If we shared the
        instance with, say, the reviewer, setting the coder timeout on
        it would silently stretch the reviewer's timeout too. We key
        the coder's instance separately so timeouts stay role-scoped.
        """
        coder_scoped = role == RoleName.CODER
        key = f"coder:{provider_name}:{model}" if coder_scoped else f"{provider_name}:{model}"
        if key in self._providers:
            return self._providers[key]
        timeout_sec = (
            self._config.coder.timeout_seconds
            if coder_scoped
            else self._config.scan.provider_timeout_sec
        )
        provider = _create_provider(
            provider_name,
            model,
            self._config,
            timeout_sec=timeout_sec,
            max_turns=self._config.coder.max_turns,
        )
        self._providers[key] = provider
        return provider

    def _materialize_intent(
        self,
        intent: TaskIntent,
        *,
        role: RoleName,
        exclude_providers: frozenset[str] = frozenset(),
    ) -> Provider:
        spec = INTENT_SPECS[intent]
        timeout_sec = (
            self._config.coder.timeout_seconds
            if role == RoleName.CODER
            else self._config.scan.provider_timeout_sec
        )

        try:
            provider, decision = _conductor_pick(spec, exclude=exclude_providers)
        except Exception as exc:
            # Reviewer independence is preferred, not a hard availability
            # blocker. If every alternative is unavailable, retry without
            # the exclude set so the review can still run and surface its
            # reduced independence through provider comparison.
            if (
                exc.__class__.__name__ != "NoConfiguredProvider"
                or intent != "review"
                or not exclude_providers
            ):
                raise
            provider, decision = _conductor_pick(spec)

        reason = (
            f"conductor:{intent}:{decision.provider}"
            f":prefer={decision.prefer}:sandbox={decision.sandbox}"
        )
        return ConductorAdapter.from_conductor_provider(
            provider,
            timeout_sec=timeout_sec,
            max_turns=self._config.coder.max_turns,
            ollama_endpoint=self._config.local.ollama_endpoint,
            effort=decision.effort,
            routing_reason=reason,
        )

    def get_provider(
        self,
        role: RoleName,
        *,
        task: str | None = None,
        prompt_size: int = 0,
        intent: TaskIntent | None = None,
        exclude_providers: frozenset[str] | set[str] | list[str] | None = None,
    ) -> Provider:
        """Return the provider for `role`.

        Intent-aware callers route through Conductor. Legacy callers can
        omit `intent` and keep the configured role default, with optional
        `task`/`prompt_size` compatibility rules.
        """
        configured = self._role_map.get(role)
        if not configured:
            raise ValueError(f"No provider configured for role: {role}")

        if intent is not None:
            if intent not in INTENT_SPECS:
                raise ValueError(f"Unknown task intent: {intent}")
            return self._materialize_intent(
                intent,
                role=RoleName(str(role)),
                exclude_providers=frozenset(exclude_providers or ()),
            )

        if task is None and prompt_size == 0:
            return configured

        configured_provider = str(configured.name)
        for rule in self._rules:
            if rule.matches(role, task, prompt_size, configured_provider):
                if rule.override_model == configured.model:
                    # Rule matches but the configured model is already
                    # the chosen one — no override, no log line.
                    return configured
                _console.print(
                    f"[dim][router] {str(role)}/{task or '(no task)'}: "
                    f"{configured.model} → {rule.override_model} "
                    f"({rule.name})[/dim]"
                )
                # Record the override on the next provider call so the
                # journal shows why this model was chosen — paired with
                # the rule's static reason in the source, the override
                # is fully traceable from the journal alone.
                from sentinel.journal import set_pending_routing_reason
                set_pending_routing_reason(rule.name)
                return self._materialize(configured_provider, rule.override_model)

        return configured

    async def chat(self, role: RoleName, prompt: str) -> ChatResponse:
        intent = "plan" if role == RoleName.PLANNER else "chat"
        return await self.get_provider(role, intent=intent).chat(prompt)

    async def research(self, prompt: str) -> ChatResponse:
        return await self.get_provider(
            RoleName.RESEARCHER, intent="research",
        ).research(prompt)

    async def code(self, prompt: str, working_directory: str = ".") -> ChatResponse:
        return await self.get_provider(
            RoleName.CODER, intent="code",
        ).code(prompt, working_directory)

    @staticmethod
    def detect_all() -> dict[str, ProviderStatus]:
        """Detect provider readiness via the Conductor-backed adapter."""
        return {
            "claude": ConductorAdapter(
                provider_name="claude", model="claude-sonnet-4-6",
            ).detect(),
            "codex": ConductorAdapter(
                provider_name="openai", model="gpt-5.4",
            ).detect(),
            "gemini": ConductorAdapter(
                provider_name="gemini", model="gemini-2.5-pro",
            ).detect(),
            "ollama": ConductorAdapter(
                provider_name="local", model="qwen2.5-coder:14b",
            ).detect(),
        }

    def missing_local_models(self) -> list[tuple[str, str]]:
        """For each role configured to use the local (ollama) provider,
        check whether the configured model is actually pulled on this
        machine. Returns a list of `(role, missing_model)` pairs — empty
        means every required model is available.

        Ollama is unique among providers in that the user can fix a
        missing-model failure with one CLI command (`ollama pull X`),
        so we surface this as a pre-flight check rather than letting
        the first call fail with a less actionable error.
        """
        local_roles: list[tuple[str, str]] = []
        for role_name, role_config in (
            ("monitor", self._config.roles.monitor),
            ("researcher", self._config.roles.researcher),
            ("planner", self._config.roles.planner),
            ("coder", self._config.roles.coder),
            ("reviewer", self._config.roles.reviewer),
        ):
            if role_config.provider.value == "local":
                local_roles.append((role_name, role_config.model))

        if not local_roles:
            return []

        # One detection call returns all pulled models — avoids hitting
        # ollama's HTTP endpoint once per local-role.
        status = ConductorAdapter(
            provider_name="local",
            model="qwen2.5-coder:14b",
            ollama_endpoint=self._config.local.ollama_endpoint,
        ).detect()
        if not status.installed:
            # Treat every required model as missing if ollama itself
            # isn't installed — the message naturally tells the user
            # to install ollama first.
            return local_roles
        pulled = set(status.models)
        return [(role, model) for role, model in local_roles if model not in pulled]
