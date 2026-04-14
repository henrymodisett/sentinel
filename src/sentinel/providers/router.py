"""
Router — selects the right provider for each role.

Reads config to map roles to providers. Detects which CLIs are
installed and authenticated. The user configures roles, not models.
"""

from __future__ import annotations

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
    def __init__(self, config: SentinelConfig) -> None:
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
            key = f"{role_config.provider.value}:{role_config.model}"
            if key not in self._providers:
                provider = _create_provider(
                    role_config.provider.value, role_config.model, config,
                )
                # Apply configured timeout from [scan] section
                provider.timeout_sec = config.scan.provider_timeout_sec
                self._providers[key] = provider
            self._role_map[role_name] = self._providers[key]

    def get_provider(self, role: RoleName) -> Provider:
        provider = self._role_map.get(role)
        if not provider:
            raise ValueError(f"No provider configured for role: {role}")
        return provider

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
