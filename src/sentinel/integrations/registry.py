"""Built-in integrations registry — tell the planner what Sentinel already ships.

The planner lives inside Sentinel but does not know what Sentinel's own
released binary provides. On the first real autumn-mail cycle
(2026-04-18, see autumn-garage/.cortex/journal/2026-04-18-first-cycle-
attempt-findings.md finding C2) the planner kept proposing *"Automate
Sentinel Cycle Journaling"* — a feature shipped by sentinel v0.3.0 itself
via ``[integrations.cortex]`` + T1.6 — because the scan saw no
project-local trigger scripts and concluded the integration was missing.

This module enumerates the integrations Sentinel already ships, each
with a set of fingerprint keywords the planner can match proposed work
items against. A refinement whose title + why + file-paths fingerprint-
match an *active* built-in integration is dropped from the backlog with
a visible audit comment. Users can still force the item by editing the
scan manually; this layer is a guard, not a gag.

Keep this file in sync with the sentinel CHANGELOG: when a new built-in
integration lands (or an existing one changes its activation rule),
update the registry. The canonical list of active integrations is this
module — not a doc, not a wiki.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — runtime use in signatures
from typing import Protocol


@dataclass(frozen=True)
class BuiltinIntegration:
    """One built-in feature the planner must not propose re-implementing.

    ``slug`` is a stable identifier surfaced in logs and skip-audit
    comments. ``description`` is the one-line human summary (also shown
    in the backlog comment when a match fires). ``fingerprints`` is the
    case-insensitive keyword set matched against the concatenation of
    (title + why + file paths + rationales) of a proposed work item.

    ``is_active`` is a callable taking the project path + the loaded
    config and returning True iff this integration is currently live in
    this project. A matched but *inactive* integration does NOT filter —
    the user may legitimately want to build the feature in that case
    (e.g. cortex integration with ``enabled = "off"`` means Sentinel is
    deliberately not writing journal entries and a local replacement is
    fair game).
    """

    slug: str
    description: str
    fingerprints: frozenset[str]
    is_active: ActivationFn
    # Keep a short pointer so the skip comment can tell the user where
    # to look. Version is informational only; the registry tracks the
    # current state of the binary, not historical feature-flag deltas.
    shipped_in: str = ""
    docs_hint: str = ""


class ActivationFn(Protocol):
    def __call__(self, project_dir: Path, config: object | None) -> bool: ...


def _cortex_cycle_journal_active(project_dir: Path, config: object | None) -> bool:
    """Cortex T1.6 is active when ``integrations.cortex.enabled`` resolves
    to ``on`` or (``auto`` with ``.cortex/`` present).

    Mirrors ``sentinel.integrations.cortex.resolve_enabled`` semantics
    but without the CLI-flag axis — the registry checks the *configured*
    activation, not the per-invocation override. A user who passes
    ``--no-cortex-journal`` for a single run still has the feature
    installed; the planner should treat it as shipping.
    """
    cortex_dir = project_dir / ".cortex"
    enabled = "auto"
    if config is not None:
        integrations = getattr(config, "integrations", None)
        cortex_cfg = getattr(integrations, "cortex", None) if integrations else None
        configured = getattr(cortex_cfg, "enabled", None)
        if configured in ("on", "off", "auto"):
            enabled = configured
    if enabled == "off":
        return False
    if enabled == "on":
        return True
    # auto
    return cortex_dir.is_dir()


def _sibling_detection_active(project_dir: Path, config: object | None) -> bool:
    """R3 sibling detection is unconditionally active — every ``sentinel
    status`` call runs it. No configuration switch exists."""
    del project_dir, config  # unused — always active
    return True


# The canonical list of built-in integrations shipping in the current
# sentinel binary. Ordered for readability; the planner iterates all of
# them, so order is not load-bearing.
BUILTIN_INTEGRATIONS: tuple[BuiltinIntegration, ...] = (
    BuiltinIntegration(
        slug="cortex_cycle_journal_t16",
        description=(
            "Cortex T1.6 sentinel-cycle journal entries are written "
            "automatically at the end of every `sentinel work` cycle "
            "when `[integrations.cortex].enabled` is `on` or `auto` "
            "(auto-detects `.cortex/`)."
        ),
        fingerprints=frozenset(
            k.lower() for k in {
                "sentinel cycle journal",
                "sentinel-cycle journal",
                "sentinel-cycle.md",
                "automate cycle journaling",
                "automate sentinel cycle journaling",
                "automate sentinel journaling",
                "cortex journal entry for each sentinel run",
                "cortex journal entry for each cycle",
                "record sentinel cycle",
                "record-sentinel-cycle",
                "t1.6",
                "sentinel cycle cortex",
                "journal each sentinel run",
            }
        ),
        is_active=_cortex_cycle_journal_active,
        shipped_in="v0.3.0",
        docs_hint="sentinel.integrations.cortex + config.integrations.cortex",
    ),
    BuiltinIntegration(
        slug="sibling_detection_r3",
        description=(
            "`sentinel status` detects sibling tools (cortex, touchstone) "
            "by CLI presence + project file-contract markers (R3, "
            "file-contract composition per Doctrine 0002)."
        ),
        fingerprints=frozenset(
            k.lower() for k in {
                "detect sibling tools",
                "sibling detection",
                "detect cortex installation",
                "detect touchstone installation",
                "sentinel status siblings",
                "siblings block in sentinel status",
                "r3 sibling",
            }
        ),
        is_active=_sibling_detection_active,
        shipped_in="v0.3.0",
        docs_hint="sentinel.siblings",
    ),
)


@dataclass(frozen=True)
class RegistryMatch:
    """One integration matched a proposed work item.

    The planner attaches this to the dropped action so the backlog
    audit comment can explain exactly what happened and where to look
    for the already-shipped feature.
    """

    integration: BuiltinIntegration
    matched_keywords: tuple[str, ...]


def _tokenize_action(action: dict) -> str:
    """Flatten an action dict to a single lowercased string for
    fingerprint matching.

    Includes title, why, impact, lens, each file's path + rationale, and
    each acceptance criterion / verification line. We match against the
    union because scans vary in where they put the signal — sometimes
    the title is generic (``"improve integration"``) and the why carries
    the specific feature name.
    """
    parts: list[str] = []

    def _append(value: object) -> None:
        if value is None:
            return
        if isinstance(value, str):
            if value.strip():
                parts.append(value)
            return
        if isinstance(value, list):
            for item in value:
                _append(item)
            return
        if isinstance(value, dict):
            for v in value.values():
                _append(v)
            return
        # Fallback: stringify anything else so we never silently drop
        # planner-provided content that happens to be a non-string.
        parts.append(str(value))

    for key in (
        "title", "why", "impact", "lens",
        "acceptance_criteria", "verification",
    ):
        _append(action.get(key))

    for f in action.get("files", []) or []:
        if isinstance(f, dict):
            _append(f.get("path"))
            _append(f.get("rationale"))
        else:
            _append(f)

    return " \n ".join(parts).lower()


def match_builtin(
    action: dict,
    project_dir: Path,
    config: object | None,
) -> RegistryMatch | None:
    """Return the first active built-in integration that fingerprint-matches
    this proposed action, or None.

    "Fingerprint match" is currently a conservative substring test: at
    least one keyword from the integration's ``fingerprints`` appears
    verbatim inside the tokenized action. The threshold is deliberately
    one keyword — scan proposals are typically specific enough that a
    single hit is strong signal. If false positives show up in practice
    we can raise the threshold; false negatives (missed filtering) are
    the failure mode we're trying to close right now.

    Only *active* integrations filter — an inactive one returning True
    would prevent users from legitimately building a local replacement
    for an opted-out feature.
    """
    haystack = _tokenize_action(action)
    if not haystack:
        return None
    for integration in BUILTIN_INTEGRATIONS:
        if not integration.is_active(project_dir, config):
            continue
        matched = tuple(
            kw for kw in integration.fingerprints if kw in haystack
        )
        if matched:
            return RegistryMatch(
                integration=integration,
                matched_keywords=matched,
            )
    return None


@dataclass
class FilterOutcome:
    """Planner-facing result of running actions through the registry.

    ``kept`` carries the actions that survived filtering, in the
    original order. ``skipped`` carries ``(action, RegistryMatch)``
    pairs so the planner can emit a human-readable audit line per drop.
    """

    kept: list[dict] = field(default_factory=list)
    skipped: list[tuple[dict, RegistryMatch]] = field(default_factory=list)


def filter_actions(
    actions: list[dict],
    project_dir: Path,
    config: object | None,
) -> FilterOutcome:
    """Split a list of scan actions into (kept, skipped_with_reason).

    The planner calls this before writing the backlog. Skipped items
    are logged via the caller (so the skip surfaces in normal CLI
    output) and also memorialized in the backlog markdown as a footer
    comment — the user can read why a proposal disappeared without
    digging through stderr.
    """
    outcome = FilterOutcome()
    for action in actions:
        match = match_builtin(action, project_dir, config)
        if match is None:
            outcome.kept.append(action)
        else:
            outcome.skipped.append((action, match))
    return outcome


__all__ = [
    "BUILTIN_INTEGRATIONS",
    "BuiltinIntegration",
    "FilterOutcome",
    "RegistryMatch",
    "filter_actions",
    "match_builtin",
]
