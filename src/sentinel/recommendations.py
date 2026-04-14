"""
Recommended defaults for sentinel setup.

Maintained as code (not TOML) so presets can react to what's actually
installed on the user's machine. Each recommendation includes a
rationale so the wizard can explain WHY it's picking something.

Last research refresh: 2026-04-13
"""

from __future__ import annotations

from dataclasses import dataclass

from sentinel.config.schema import ProviderName, RoleName


@dataclass
class Recommendation:
    """A recommended (provider, model) pair for a role, with rationale."""

    provider: ProviderName
    model: str
    rationale: str
    monthly_cost_estimate: str  # "free" | "$1-5" | "$5-20" | etc


# Per-role recommendations when every provider is available.
# These are the defaults for --preset recommended.

RECOMMENDED = {
    RoleName.MONITOR: Recommendation(
        provider=ProviderName.GEMINI,
        model="gemini-2.5-flash",
        rationale=(
            "Scans run often, so speed and cost matter more than peak quality. "
            "Gemini Flash is ~6x faster than Claude Sonnet and effectively free "
            "on the free tier. Findings are still specific and useful."
        ),
        monthly_cost_estimate="free",
    ),
    RoleName.RESEARCHER: Recommendation(
        provider=ProviderName.GEMINI,
        model="gemini-2.5-pro",
        rationale=(
            "Research needs web grounding and long context, which Gemini does "
            "natively. Cheaper than Claude Opus for the same quality on "
            "research tasks."
        ),
        monthly_cost_estimate="$1-5",
    ),
    RoleName.PLANNER: Recommendation(
        provider=ProviderName.CLAUDE,
        model="claude-opus-4-6",
        rationale=(
            "Planning is infrequent and high-stakes — picks what sentinel "
            "builds next. Claude Opus has the best judgment for prioritization."
        ),
        monthly_cost_estimate="$1-5",
    ),
    RoleName.CODER: Recommendation(
        provider=ProviderName.CLAUDE,
        model="claude-sonnet-4-6",
        rationale=(
            "Claude Code CLI's agentic loop (file edit, terminal, test running) "
            "is the strongest tool integration available. Sonnet is a good "
            "cost/quality balance for the frequent execution role."
        ),
        monthly_cost_estimate="$5-20",
    ),
    RoleName.REVIEWER: Recommendation(
        provider=ProviderName.GEMINI,
        model="gemini-2.5-pro",
        rationale=(
            "Must be a different provider than the coder for independence — "
            "two models with different blind spots catch more issues. Gemini "
            "Pro is strong at code review."
        ),
        monthly_cost_estimate="$1-5",
    ),
}


# Alternative presets — composed from available providers at init time.

PRESETS = {
    "recommended": "Smart defaults per role (the default)",
    "simple": "Use claude for everything — simplest, one API",
    "cheap": "Prefer local/gemini-flash everywhere possible",
    "local": "Ollama for the cold path; claude/codex for coder (agentic)",
    "hybrid": "Local Monitor (hot path) + cloud everything else — best $/quality",
    "power": "Highest-quality model per role (expensive)",
}


def recommend_for_role(
    role: RoleName, available: set[ProviderName], ollama_models: list[str],
) -> Recommendation:
    """Return a recommendation for a role, filtered by what's actually available.

    Falls back to best-available provider if the recommended one isn't installed.
    """
    rec = RECOMMENDED[role]

    if rec.provider in available:
        return rec

    # Fallback chain — pick the best available provider for this role
    fallback_order = _fallback_order(role, available)
    if not fallback_order:
        # Return the original even if unavailable — init will surface the error
        return rec

    fallback_provider = fallback_order[0]
    fallback_model = _default_model_for(fallback_provider, ollama_models)
    return Recommendation(
        provider=fallback_provider,
        model=fallback_model,
        rationale=(
            f"{rec.provider.value} is recommended but not installed — "
            f"falling back to {fallback_provider.value}."
        ),
        monthly_cost_estimate=rec.monthly_cost_estimate,
    )


def _fallback_order(
    role: RoleName, available: set[ProviderName],
) -> list[ProviderName]:
    """Pick substitute providers based on what this role needs.

    Coder prefers providers with real agentic_code support (claude, openai).
    Other roles just need chat. If the preferred set is empty, every role
    falls back to whichever provider is installed — a degraded coder beats
    an empty config.
    """
    generic = [
        p for p in [
            ProviderName.GEMINI, ProviderName.CLAUDE,
            ProviderName.OPENAI, ProviderName.LOCAL,
        ]
        if p in available
    ]

    if role == RoleName.CODER:
        agentic = [
            p for p in [ProviderName.CLAUDE, ProviderName.OPENAI]
            if p in available
        ]
        return agentic or generic

    return generic


def _default_model_for(
    provider: ProviderName, ollama_models: list[str],
) -> str:
    """Pick a reasonable default model for a provider."""
    if provider == ProviderName.CLAUDE:
        return "claude-sonnet-4-6"
    if provider == ProviderName.OPENAI:
        return "gpt-5.4"
    if provider == ProviderName.GEMINI:
        return "gemini-2.5-pro"
    if provider == ProviderName.LOCAL:
        return _pick_local_model(ollama_models)
    return "default"


def _pick_local_model(available_models: list[str]) -> str:
    """Pick the best pulled Ollama model by family preference."""
    if not available_models:
        return "qwen2.5-coder:14b"  # user needs to pull this

    import re

    def _size(model: str) -> int:
        m = re.search(r":(\d+)b", model)
        return int(m.group(1)) if m else 0

    families = ["qwen2.5-coder", "deepseek-r1", "deepseek-coder", "llama3.3"]
    for family in families:
        candidates = [m for m in available_models if m.startswith(family)]
        if candidates:
            return max(candidates, key=_size)

    return available_models[0]


def apply_preset(
    preset: str, available: set[ProviderName], ollama_models: list[str],
) -> dict[RoleName, tuple[ProviderName, str]]:
    """Apply a named preset and return role → (provider, model) assignments.

    Falls back intelligently when a preset's first choice isn't available.
    """
    if preset == "recommended":
        return {
            role: (rec.provider, rec.model)
            for role, rec in (
                (r, recommend_for_role(r, available, ollama_models))
                for r in RoleName
            )
        }

    if preset == "simple":
        # Use claude for everything if available; else first available
        target = (
            ProviderName.CLAUDE if ProviderName.CLAUDE in available
            else next(iter(available), None)
        )
        if not target:
            return {}
        model = _default_model_for(target, ollama_models)
        return {role: (target, model) for role in RoleName}

    if preset == "cheap":
        # Prefer local > gemini-flash for most roles; claude only for coder.
        # Always fall through to recommend_for_role() so an unavailable
        # provider is never written into config.
        cheap_result: dict[RoleName, tuple[ProviderName, str]] = {}
        for role in RoleName:
            if role == RoleName.CODER:
                if ProviderName.CLAUDE in available:
                    cheap_result[role] = (
                        ProviderName.CLAUDE, "claude-sonnet-4-6",
                    )
                elif ProviderName.OPENAI in available:
                    cheap_result[role] = (ProviderName.OPENAI, "gpt-5.4-mini")
                else:
                    rec = recommend_for_role(role, available, ollama_models)
                    cheap_result[role] = (rec.provider, rec.model)
            elif ProviderName.LOCAL in available:
                cheap_result[role] = (
                    ProviderName.LOCAL, _pick_local_model(ollama_models),
                )
            elif ProviderName.GEMINI in available:
                cheap_result[role] = (
                    ProviderName.GEMINI, "gemini-2.5-flash",
                )
            else:
                rec = recommend_for_role(role, available, ollama_models)
                cheap_result[role] = (rec.provider, rec.model)
        return cheap_result

    if preset == "local":
        # Ollama for every cold-path role (monitor/researcher/planner/
        # reviewer). Coder MUST be an agentic-capable provider since
        # Ollama has no Claude-Code-equivalent loop — fall back to
        # claude or openai for that role. If neither is available, the
        # preset degrades gracefully via recommend_for_role.
        if ProviderName.LOCAL not in available:
            # No Ollama → use recommended defaults
            return apply_preset("recommended", available, ollama_models)
        local_model = _pick_local_model(ollama_models)
        local_result: dict[RoleName, tuple[ProviderName, str]] = {}
        for role in RoleName:
            if role == RoleName.CODER:
                if ProviderName.CLAUDE in available:
                    local_result[role] = (
                        ProviderName.CLAUDE, "claude-sonnet-4-6",
                    )
                elif ProviderName.OPENAI in available:
                    local_result[role] = (
                        ProviderName.OPENAI, "gpt-5.4-mini",
                    )
                else:
                    rec = recommend_for_role(role, available, ollama_models)
                    local_result[role] = (rec.provider, rec.model)
            else:
                local_result[role] = (ProviderName.LOCAL, local_model)
        return local_result

    if preset == "hybrid":
        # Local Monitor (runs every cycle — save cloud spend on the hot
        # path), cloud everything else for quality. Falls back per-role
        # when the ideal provider isn't installed.
        hybrid_result: dict[RoleName, tuple[ProviderName, str]] = {}
        if ProviderName.LOCAL in available:
            hybrid_result[RoleName.MONITOR] = (
                ProviderName.LOCAL, _pick_local_model(ollama_models),
            )
        else:
            rec = recommend_for_role(
                RoleName.MONITOR, available, ollama_models,
            )
            hybrid_result[RoleName.MONITOR] = (rec.provider, rec.model)
        # Researcher/Planner/Coder/Reviewer use recommended defaults
        for role in (
            RoleName.RESEARCHER, RoleName.PLANNER,
            RoleName.CODER, RoleName.REVIEWER,
        ):
            rec = recommend_for_role(role, available, ollama_models)
            hybrid_result[role] = (rec.provider, rec.model)
        return hybrid_result

    if preset == "power":
        # Highest-quality model per role — fall back to best available
        # when the ideal pick isn't installed
        ideal = {
            RoleName.MONITOR: (ProviderName.CLAUDE, "claude-sonnet-4-6"),
            RoleName.RESEARCHER: (ProviderName.GEMINI, "gemini-2.5-pro"),
            RoleName.PLANNER: (ProviderName.CLAUDE, "claude-opus-4-6"),
            RoleName.CODER: (ProviderName.CLAUDE, "claude-opus-4-6"),
            RoleName.REVIEWER: (ProviderName.CLAUDE, "claude-opus-4-6"),
        }
        result: dict[RoleName, tuple[ProviderName, str]] = {}
        for role, (ideal_prov, ideal_model) in ideal.items():
            if ideal_prov in available:
                result[role] = (ideal_prov, ideal_model)
            else:
                rec = recommend_for_role(role, available, ollama_models)
                result[role] = (rec.provider, rec.model)
        return result

    raise ValueError(
        f"Unknown preset '{preset}'. Valid presets: {list(PRESETS.keys())}",
    )
