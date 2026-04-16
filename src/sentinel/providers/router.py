"""
Router — selects the right provider+model for each call.

The user configures a default `(provider, model)` per role in
`.sentinel/config.toml`. That default works for most calls. But dogfood
on the sentinel repo (2026-04-16) showed two real cases where the
*task* dictates the model, not the role:

- The `synthesize` step times out on `gemini-2.5-flash` but completes
  on `gemini-2.5-pro` — flash isn't large enough for the cross-lens
  summary prompt.
- `evaluate_lens` calls with very large prompts fail non-zero on
  `gemini-2.5-pro` (observed 222s exit) — flash handles them fine.

Encoded as data in `DEFAULT_RULES` so adding a new override doesn't
require touching control flow. Callers that pass `task=` hints to
`get_provider()` get the rule-based override; callers that don't (the
test path, ad-hoc usage) get the configured default — fully backwards
compatible.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console

from sentinel.config.schema import RoleName, SentinelConfig
from sentinel.providers.claude import ClaudeProvider
from sentinel.providers.gemini import GeminiProvider
from sentinel.providers.interface import (  # noqa: TCH001
    ChatResponse,
    Provider,
    ProviderStatus,
)
from sentinel.providers.local import LocalProvider
from sentinel.providers.openai import OpenAIProvider

_console = Console()


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


def _create_provider(provider_name: str, model: str, config: SentinelConfig) -> Provider:
    if provider_name == "claude":
        return ClaudeProvider(model=model)
    if provider_name == "openai":
        return OpenAIProvider(model=model)
    if provider_name == "gemini":
        return GeminiProvider(model=model)
    if provider_name == "local":
        return LocalProvider(model=model, endpoint=config.local.ollama_endpoint)
    raise ValueError(f"Unknown provider: {provider_name}")


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
            provider = self._materialize(role_config.provider.value, role_config.model)
            self._role_map[role_name] = provider

    def _materialize(self, provider_name: str, model: str) -> Provider:
        """Return a Provider instance for (provider_name, model), cached
        across the router's lifetime. Same (provider, model) pair is
        always the same instance — both for memory and for sharing the
        Coder's max_turns / scan timeout that we set on instances."""
        key = f"{provider_name}:{model}"
        if key in self._providers:
            return self._providers[key]
        provider = _create_provider(provider_name, model, self._config)
        provider.timeout_sec = self._config.scan.provider_timeout_sec
        provider.max_turns = self._config.coder.max_turns
        self._providers[key] = provider
        return provider

    def get_provider(
        self,
        role: RoleName,
        *,
        task: str | None = None,
        prompt_size: int = 0,
    ) -> Provider:
        """Return the provider for `role`, possibly overridden by a
        routing rule when `task` and/or `prompt_size` hint at a context
        the rules were written for.

        Without hints (`task=None`), this is identical to the original
        static-mapping behavior — every existing caller keeps working
        unchanged. Pass `task=` from any call site that knows it (the
        Monitor pipeline does: explore / evaluate_lens / synthesize).
        """
        configured = self._role_map.get(role)
        if not configured:
            raise ValueError(f"No provider configured for role: {role}")

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
        return await self.get_provider(role).chat(prompt)

    async def research(self, prompt: str) -> ChatResponse:
        return await self.get_provider(RoleName.RESEARCHER).research(prompt)

    async def code(self, prompt: str, working_directory: str = ".") -> ChatResponse:
        return await self.get_provider(RoleName.CODER).code(prompt, working_directory)

    @staticmethod
    def detect_all() -> dict[str, ProviderStatus]:
        """Detect all provider CLIs — used by `sentinel init` and `sentinel providers`."""
        return {
            "claude": ClaudeProvider().detect(),
            "codex": OpenAIProvider().detect(),
            "gemini": GeminiProvider().detect(),
            "ollama": LocalProvider().detect(),
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
        status = LocalProvider(
            endpoint=self._config.local.ollama_endpoint,
        ).detect()
        if not status.installed:
            # Treat every required model as missing if ollama itself
            # isn't installed — the message naturally tells the user
            # to install ollama first.
            return local_roles
        pulled = set(status.models)
        return [(role, model) for role, model in local_roles if model not in pulled]
