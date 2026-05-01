"""
sentinel init — guided setup wizard.

Modes:
  - Interactive (TTY + no --preset, --yes, or wizard-overriding flags):
    walks the Doctrine 0002 wizard (providers multi-select, coder,
    reviewer, models, budget, optional scan).
  - Non-interactive (no TTY or --yes): uses recommended defaults silently.
  - Preset (--preset X): uses the named preset, no questions.
  - Flag overrides (--providers / --coder / --reviewer / --budget):
    flags answer the prompts they correspond to. Any unanswered prompts
    still run interactively on TTY; non-TTY uses recommended defaults.

The wizard prints its equivalent flag-form at the end so scripters
learn the flags by using the tool (Doctrine 0002).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sentinel.banner import SUBTITLE_INIT, print_banner, sentinel_version
from sentinel.config.schema import (
    ProviderName,
    RoleName,
)
from sentinel.providers.router import Router
from sentinel.recommendations import (
    PRESETS,
    RECOMMENDED,
    apply_preset,
    pick_reviewer_provider,
)

console = Console()


# CLI-name ↔ ProviderName mapping — kept here as the single source of
# truth for flag parsing (e.g. --providers claude,codex,gemini,ollama).
# The detection layer uses the CLI names; the config schema uses the
# enum; the wizard needs both.
_CLI_TO_PROVIDER: dict[str, ProviderName] = {
    "claude": ProviderName.CLAUDE,
    "codex": ProviderName.OPENAI,
    "gemini": ProviderName.GEMINI,
    "ollama": ProviderName.LOCAL,
}
_PROVIDER_TO_CLI: dict[ProviderName, str] = {
    v: k for k, v in _CLI_TO_PROVIDER.items()
}


# ---------- Helpers ----------


def _find_sentinel_root() -> Path:
    """Find the sentinel package installation root (for templates)."""
    source_root = Path(__file__).parent.parent.parent.parent
    if (source_root / "templates").exists():
        return source_root
    brew_root = Path("/opt/homebrew/opt/sentinel/libexec")
    if brew_root.exists():
        return brew_root
    return source_root


def _copy_tree(src: Path, dst: Path) -> list[str]:
    """Copy a directory tree, skipping existing files."""
    created = []
    for src_file in src.rglob("*"):
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(src)
        dst_file = dst / rel
        if dst_file.exists():
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        created.append(str(rel))
    return created


def _detect_project_type(project: Path) -> str:
    """Return a short project-type label for config.toml.

    Delegates the Swift/Rust/Go/Python/Node/generic decision to the
    single source of truth in sentinel.state.detect_project_type() so
    init and gather_state() can't drift apart. Only falls through to
    init-only labels (typescript, haskell) when the state detector
    returns "generic" — otherwise a Rust or Swift project that happens
    to ship a docs site with package.json would get mis-labelled.
    """
    from sentinel.state import detect_project_type as _state_detect

    detected = _state_detect(project)["type"]

    if detected != "generic":
        # Node/TS distinction: state calls both "node"; init prefers
        # "typescript" when a tsconfig is present
        if detected == "node":
            return "typescript" if (project / "tsconfig.json").exists() else "javascript"
        return detected

    # Haskell — state-level test/lint commands aren't wired yet
    if list(project.glob("*.cabal")) or (project / "stack.yaml").exists():
        return "haskell"

    return "generic"


# ---------- Interactive Questions ----------


def _ask_numbered(
    prompt: str, options: list[tuple[str, str]], default: int = 1,
) -> int:
    """Prompt user with a numbered menu. Returns the selected index (1-based).

    options: list of (value, description) tuples — display order matters.
    Returns the 1-based index of the chosen option.
    """
    console.print(f"\n[bold]{prompt}[/bold]")
    for i, (_value, desc) in enumerate(options, 1):
        marker = "[dim]→[/dim] " if i == default else "    "
        console.print(f"  {marker}[{i}] {desc}")

    while True:
        choice = click.prompt(
            f"  Your choice [{default}]", default=str(default), show_default=False,
        ).strip()
        if not choice:
            return default
        try:
            n = int(choice)
            if 1 <= n <= len(options):
                return n
        except ValueError:
            pass
        console.print(f"  [red]Please enter 1-{len(options)}[/red]")


def _interactive_questions(
    available: set[ProviderName], ollama_models: list[str],
) -> tuple[dict[RoleName, tuple[ProviderName, str]], float]:
    """Walk the user through the setup questions. Returns (role_assignments, daily_budget_usd)."""
    # Question 1: Goal — informs which preset we pick
    goal_options = [
        ("recommended", "Keep it moving — balanced refinement + proposals"),
        ("cheap", "Keep it polished — refinement focus, tight budget"),
        ("power", "Build things out — accept expansion, use best models"),
    ]
    goal_idx = _ask_numbered(
        "What do you want sentinel to focus on for this project?",
        goal_options, default=1,
    )
    preset = goal_options[goal_idx - 1][0]

    role_assignments = apply_preset(preset, available, ollama_models)

    # Question 2: Budget
    budget_options = [
        ("15", "$15/day (recommended — work + occasional expansion)"),
        ("5", "$5/day (light use, mostly scans)"),
        ("50", "$50/day (heavy use)"),
        ("1000", "No practical cap ($1000/day)"),
    ]
    budget_idx = _ask_numbered(
        "Daily budget cap?", budget_options, default=1,
    )
    daily_budget = float(budget_options[budget_idx - 1][0])

    return role_assignments, daily_budget


# ---------- Wizard (Doctrine 0002 — interactive-by-default) ----------


def _parse_providers_flag(
    raw: str | None, detected_ready: set[ProviderName],
) -> set[ProviderName] | None:
    """Parse --providers claude,codex,gemini into a set. None = not passed.

    Unknown CLI names raise BadParameter. Names the user passed that
    aren't actually installed-and-ready are silently dropped with a
    warning — we don't block init just because a user pasted an ambitious
    flag line; we want the tool to degrade gracefully.
    """
    if not raw:
        return None
    requested: set[ProviderName] = set()
    unknown: list[str] = []
    missing: list[str] = []
    for raw_name in raw.split(","):
        name = raw_name.strip().lower()
        if not name:
            continue
        if name not in _CLI_TO_PROVIDER:
            unknown.append(name)
            continue
        provider = _CLI_TO_PROVIDER[name]
        if provider not in detected_ready:
            missing.append(name)
            continue
        requested.add(provider)
    if unknown:
        raise click.BadParameter(
            f"Unknown provider(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(_CLI_TO_PROVIDER)}",
        )
    if missing:
        console.print(
            f"  [yellow]! Requested providers not installed/ready, "
            f"skipping: {', '.join(missing)}[/yellow]",
        )
    return requested


def _parse_role_flag(
    raw: str | None, flag_name: str,
) -> tuple[ProviderName, str | None] | None:
    """Parse --coder claude or --coder claude:claude-sonnet-4-6.

    Returns (provider, model_or_None) or None if flag wasn't passed.
    Unknown providers raise BadParameter so the user gets a readable
    error, not a silent fallback.
    """
    if not raw:
        return None
    if ":" in raw:
        prov_str, model = raw.split(":", 1)
    else:
        prov_str, model = raw, None
    prov_str = prov_str.strip().lower()
    if prov_str not in _CLI_TO_PROVIDER:
        raise click.BadParameter(
            f"{flag_name}: unknown provider {prov_str!r}. "
            f"Valid: {', '.join(_CLI_TO_PROVIDER)}",
        )
    return _CLI_TO_PROVIDER[prov_str], (model.strip() if model else None)


def _prompt_multiselect_providers(
    detected_ready: set[ProviderName],
    all_detected: dict[str, object],
) -> set[ProviderName]:
    """Multi-select from detected providers. Ready ones pre-checked.

    Input: space- or comma-separated indices, or empty to accept
    the pre-checked set. Returns the chosen set (always non-empty —
    caller has already handled the zero-providers case).
    """
    cli_names = list(_CLI_TO_PROVIDER.keys())
    console.print("\n[bold]Providers to enable for this project[/bold]")
    console.print(
        "  [dim]Pre-checked = installed and ready. Enter indices "
        "(e.g. '1 2 3'), or press Enter to keep defaults.[/dim]",
    )

    for i, cli_name in enumerate(cli_names, 1):
        provider = _CLI_TO_PROVIDER[cli_name]
        ready = provider in detected_ready
        mark = "[green][x][/green]" if ready else "[dim][ ][/dim]"
        label = f"[bold]{cli_name}[/bold]" if ready else f"[dim]{cli_name}[/dim]"
        console.print(f"  {mark} [{i}] {label}")

    while True:
        raw = click.prompt(
            "  Your choice (Enter = keep pre-checked)",
            default="", show_default=False,
        ).strip()
        if not raw:
            return set(detected_ready) or set()
        try:
            chosen: set[ProviderName] = set()
            for tok in raw.replace(",", " ").split():
                idx = int(tok)
                if not (1 <= idx <= len(cli_names)):
                    raise ValueError
                chosen.add(_CLI_TO_PROVIDER[cli_names[idx - 1]])
            if not chosen:
                console.print("  [red]Select at least one provider[/red]")
                continue
            return chosen
        except ValueError:
            console.print(
                f"  [red]Please enter 1-{len(cli_names)} separated by "
                f"spaces or commas[/red]",
            )


def _prompt_single_provider(
    prompt: str, choices: list[ProviderName], default: ProviderName,
) -> ProviderName:
    """Pick one provider from a small list, default pre-highlighted."""
    options = [(_PROVIDER_TO_CLI[p], _PROVIDER_TO_CLI[p]) for p in choices]
    default_idx = choices.index(default) + 1 if default in choices else 1
    idx = _ask_numbered(prompt, options, default=default_idx)
    return choices[idx - 1]


def _default_model_for_provider(
    provider: ProviderName, ollama_models: list[str],
) -> str:
    """Provider-appropriate default model — pulled from RECOMMENDED
    where it exists so the wizard, presets, and fallbacks all suggest
    the same thing."""
    from sentinel.recommendations import _default_model_for
    # Prefer the RECOMMENDED coder/reviewer model for that provider,
    # otherwise fall back to the generic default.
    for rec in RECOMMENDED.values():
        if rec.provider == provider:
            return rec.model
    return _default_model_for(provider, ollama_models)


def _run_wizard(
    available_set: set[ProviderName],
    all_detected: dict[str, object],
    ollama_models: list[str],
    flags: dict,
) -> tuple[
    set[ProviderName],
    dict[RoleName, tuple[ProviderName, str]],
    float,
    bool,
    str,
]:
    """Doctrine 0002 wizard. Flags skip their corresponding prompts.

    Returns (active_providers, role_assignments, daily_budget,
    run_scan, cortex_enabled). ``cortex_enabled`` is one of
    ``auto|on|off`` and controls
    ``[integrations.cortex] enabled`` in the generated config — see the
    T1.6 integration plan, Success Criterion #10.
    """
    # 1. Providers multi-select (skipped if --providers passed)
    if flags.get("providers_set") is not None:
        active = flags["providers_set"]
        if not active:
            # User passed --providers but every name dropped — fall back
            # to detected-ready to avoid an empty config.
            active = set(available_set)
    elif flags["interactive"]:
        active = _prompt_multiselect_providers(available_set, all_detected)
    else:
        active = set(available_set)

    if not active:
        # Caller checked earlier; defensive.
        active = set(available_set)

    # Start from recommended assignments, then override per flags/prompts
    assignments = apply_preset("recommended", active, ollama_models)

    # 2. Coder provider + 4. Coder model
    coder_flag = flags.get("coder")
    if coder_flag is not None:
        coder_prov, coder_model = coder_flag
        if coder_prov not in active:
            # User asked for a provider that isn't in the active set —
            # add it if it's installed, else hard error with a message
            # the user can act on.
            if coder_prov in available_set:
                active.add(coder_prov)
            else:
                raise click.BadParameter(
                    f"--coder {_PROVIDER_TO_CLI[coder_prov]}: that "
                    "provider isn't installed/ready. Install it first or "
                    "pick one of: "
                    f"{', '.join(_PROVIDER_TO_CLI[p] for p in active)}",
                )
        coder_model = coder_model or _default_model_for_provider(
            coder_prov, ollama_models,
        )
    elif flags["interactive"]:
        coder_default = (
            ProviderName.CLAUDE if ProviderName.CLAUDE in active
            else next(iter(active))
        )
        coder_prov = _prompt_single_provider(
            "Coder provider?", sorted(active, key=lambda p: p.value),
            coder_default,
        )
        suggested = _default_model_for_provider(coder_prov, ollama_models)
        coder_model = click.prompt(
            f"  Coder model [{suggested}]", default=suggested,
            show_default=False,
        ).strip() or suggested
    else:
        coder_prov, coder_model = assignments[RoleName.CODER]

    assignments[RoleName.CODER] = (coder_prov, coder_model)

    # 3. Reviewer provider + 5. Reviewer model
    reviewer_flag = flags.get("reviewer")
    if reviewer_flag is not None:
        reviewer_prov, reviewer_model = reviewer_flag
        if reviewer_prov not in active:
            if reviewer_prov in available_set:
                active.add(reviewer_prov)
            else:
                raise click.BadParameter(
                    f"--reviewer {_PROVIDER_TO_CLI[reviewer_prov]}: "
                    "that provider isn't installed/ready.",
                )
        if reviewer_prov == coder_prov:
            console.print(
                "  [yellow]! --reviewer matches --coder provider — "
                "violates Doctrine 0002 cross-provider review.[/yellow]",
            )
        reviewer_model = reviewer_model or _default_model_for_provider(
            reviewer_prov, ollama_models,
        )
    elif flags["interactive"]:
        # Default: first entry that is NOT the coder provider.
        default_reviewer, violates = pick_reviewer_provider(
            coder_prov, active,
        )
        reviewer_prov = _prompt_single_provider(
            "Reviewer provider? (should differ from coder)",
            sorted(active, key=lambda p: p.value),
            default_reviewer,
        )
        if reviewer_prov == coder_prov:
            console.print(
                "  [yellow]! Reviewer provider matches coder — "
                "violates Doctrine 0002 cross-provider review. "
                "Continuing anyway.[/yellow]",
            )
        suggested = _default_model_for_provider(reviewer_prov, ollama_models)
        reviewer_model = click.prompt(
            f"  Reviewer model [{suggested}]", default=suggested,
            show_default=False,
        ).strip() or suggested
    else:
        # Non-interactive: let apply_preset's pair-independence enforcement
        # stand, but re-resolve against the current coder if that changed.
        rp, violates = pick_reviewer_provider(coder_prov, active)
        if violates:
            console.print(
                "  [yellow]! Only one provider installed — reviewer "
                "falls back to the coder's provider, which violates "
                "Doctrine 0002 cross-provider review. Install a second "
                "provider (brew install codex) to restore independence."
                "[/yellow]",
            )
        reviewer_prov = rp
        reviewer_model = _default_model_for_provider(rp, ollama_models)

    assignments[RoleName.REVIEWER] = (reviewer_prov, reviewer_model)

    # 6. Budget
    budget_flag = flags.get("budget")
    if budget_flag is not None:
        daily_budget = budget_flag
    elif flags["interactive"]:
        raw = click.prompt(
            "  Daily budget cap USD [15.0]", default="15.0",
            show_default=False,
        ).strip()
        try:
            daily_budget = float(raw) if raw else 15.0
        except ValueError:
            console.print(
                "  [yellow]! Invalid number — defaulting to $15.0[/yellow]",
            )
            daily_budget = 15.0
    else:
        daily_budget = 15.0

    # 7. Run a scan now?
    run_scan = False
    if flags.get("run_scan") is not None:
        run_scan = flags["run_scan"]
    elif flags["interactive"]:
        run_scan = click.confirm(
            "  Run a scan now?", default=False,
        )

    # 8. Cortex T1.6 integration — write Cortex journal entries at
    # cycle end? Default reflects `.cortex/` presence at init time per
    # autumn-garage/.cortex/plans/sentinel-cortex-t16-integration.md
    # Success Criterion #10. The flag/config lands in
    # `.sentinel/config.toml` as `[integrations.cortex] enabled`
    # (`auto`/`on`/`off`). `project_path` comes from the caller (via
    # flags) so the wizard stays independent of the surrounding init
    # flow's filesystem probing.
    project_path = flags.get("project_path")
    cortex_dir_present = False
    if project_path is not None:
        cortex_dir_present = (project_path / ".cortex").is_dir()
    cortex_integration_choice = flags.get("cortex_integration")  # None | "auto" | "on" | "off"
    if cortex_integration_choice is None and flags["interactive"]:
        default = cortex_dir_present
        answer = click.confirm(
            "  Write Cortex journal entries at cycle end?",
            default=default,
        )
        cortex_integration_choice = "on" if answer else "off"
    flags["cortex_integration"] = cortex_integration_choice or "auto"

    # Re-run the pair-independence check one last time for any case where
    # a flag combo landed coder == reviewer without an explicit warning
    # firing above (e.g. --coder claude --yes with only claude available).
    final_coder = assignments[RoleName.CODER][0]
    final_reviewer = assignments[RoleName.REVIEWER][0]
    # Already warned for the single-provider path above; re-warn only
    # when the user had a real choice (len(active) > 1) but ended up
    # with coder == reviewer anyway (e.g. an explicit --reviewer flag).
    if (
        final_coder == final_reviewer
        and not flags["interactive"]
        and len(active) > 1
    ):
        console.print(
            "  [yellow]! Reviewer provider matches coder — "
            "violates Doctrine 0002 cross-provider review.[/yellow]",
        )

    return (
        active, assignments, daily_budget, run_scan,
        flags.get("cortex_integration") or "auto",
    )


def _print_equivalent_flag_form(
    active: set[ProviderName],
    assignments: dict[RoleName, tuple[ProviderName, str]],
    daily_budget: float,
    run_scan: bool,
) -> None:
    """Print the single-line flag command that reproduces this wizard.

    Per Doctrine 0002 — teach-by-doing beats documentation for scripting
    onboarding.
    """
    providers_arg = ",".join(
        _PROVIDER_TO_CLI[p] for p in sorted(active, key=lambda p: p.value)
    )
    coder_prov, coder_model = assignments[RoleName.CODER]
    reviewer_prov, reviewer_model = assignments[RoleName.REVIEWER]
    scan_arg = "--scan" if run_scan else "--no-scan"

    line = (
        f"sentinel init --providers {providers_arg} "
        f"--coder {_PROVIDER_TO_CLI[coder_prov]}:{coder_model} "
        f"--reviewer {_PROVIDER_TO_CLI[reviewer_prov]}:{reviewer_model} "
        f"--budget {daily_budget} {scan_arg} --yes"
    )
    console.print()
    console.print("[bold]==> Equivalent to rerun:[/bold]")
    console.print(f"    [cyan]{line}[/cyan]")


# ---------- Main flow ----------


def _seed_doctrine_defaults(project: Path) -> None:
    """Seed the Sentinel baseline Doctrine pack into project's .cortex/.

    Gracefully skips when cortex is missing or the defaults pack is not
    found — init's value is not gated on Doctrine seeding. Logs a
    one-line result so the user sees what happened without noise.
    """
    from sentinel.integrations.cortex import seed_default_doctrine

    result = seed_default_doctrine(project, merge="skip-existing")
    if result is None:
        console.print(
            "  [dim]→ Doctrine seeding skipped "
            "(cortex not installed or defaults pack not found)[/dim]",
        )
    else:
        console.print(
            f"  [green]✓[/green] Seeded {result.seeded} default "
            "Doctrine entries into .cortex/doctrine/ "
            "(--no-seed-defaults to skip)",
        )


def run_init(
    project_path: str | None = None,
    auto_yes: bool = False,
    preset: str | None = None,
    *,
    providers: str | None = None,
    coder: str | None = None,
    reviewer: str | None = None,
    budget: float | None = None,
    run_scan: bool | None = None,
    implicit: bool = False,
    seed_defaults: bool = True,
) -> None:
    """Run the setup wizard — interactive if TTY, else use defaults.

    Flags are Doctrine 0002 overrides: any flag passed skips its
    corresponding prompt; remaining prompts still run on TTY. `--yes`
    skips all prompts and uses defaults.

    When called from `sentinel work` as an implicit auto-init (config
    missing), `implicit=True` suppresses the "reconfigure?" prompt
    (there's nothing to reconfigure) and the wizard's trailing
    equivalent-flag-form (the user didn't ask for a wizard).

    ``seed_defaults=False`` skips seeding the Sentinel baseline Doctrine
    pack into ``.cortex/doctrine/`` (``--no-seed-defaults`` flag).
    """
    # Splash on entry except when called as an implicit auto-init from
    # `sentinel work` (which already printed its own banner).
    if not implicit:
        print_banner(SUBTITLE_INIT, sentinel_version())

    project = Path(project_path or os.getcwd()).resolve()
    console.print(f"\n[bold]Sentinel Setup[/bold] — {project.name}\n")

    config_exists = (project / ".sentinel" / "config.toml").exists()

    # Re-init prompt: config already written, user is re-running init.
    # Doctrine 0002 § 7 — re-running is idempotent; never re-ask if a
    # valid config exists unless the user consents to overwrite.
    # (Implicit auto-inits from `sentinel work` can't hit this because
    # work only calls run_init when config is missing.)
    is_tty = sys.stdin.isatty()
    if config_exists and not implicit:
        if auto_yes or not is_tty:
            # Non-interactive re-init with an existing config: honor
            # the existing file (idempotent) and only refresh adjacent
            # files (.sentinel/.gitignore, .claude/ templates, target
            # .gitignore).
            console.print(
                "[dim]Config exists; refreshing adjacent files only "
                "(use prompts on a TTY to reconfigure).[/dim]\n",
            )
            _write_sentinel_gitignore(project)
            _install_claude_templates(project)
            _ensure_gitignore_entries(project)
            if seed_defaults:
                _seed_doctrine_defaults(project)
            return
        if not click.confirm(
            "Config already exists — reconfigure?", default=False,
        ):
            console.print(
                "[dim]Leaving existing config in place.[/dim]",
            )
            # Still refresh adjacent files — cheap idempotent upgrades.
            _write_sentinel_gitignore(project)
            _install_claude_templates(project)
            _ensure_gitignore_entries(project)
            if seed_defaults:
                _seed_doctrine_defaults(project)
            return
        # User confirmed reconfigure — wipe the old config so the
        # downstream write path doesn't skip it.
        (project / ".sentinel" / "config.toml").unlink()
        console.print("[dim]Removed existing config; running wizard.[/dim]\n")

    # Detect project type
    project_type = _detect_project_type(project)

    # Detect providers
    console.print("[bold]Detecting providers...[/bold]\n")
    statuses = Router.detect_all()
    _render_provider_table(statuses)

    available_providers = [
        n for n, s in statuses.items() if s.installed and s.authenticated
    ]
    if not available_providers:
        _show_install_hints(statuses)
        return

    available_set = {
        _CLI_TO_PROVIDER[p] for p in available_providers
        if p in _CLI_TO_PROVIDER
    }
    ollama_models = (
        statuses["ollama"].models if statuses["ollama"].installed else []
    )

    # Decide role assignments + budget
    # Interactive = TTY, not --yes, no --preset, and no full flag override.
    # Any wizard-answering flag (providers/coder/reviewer/budget) still
    # lets the remaining prompts run on TTY — per Doctrine 0002 §3.
    is_interactive = is_tty and not auto_yes and preset is None

    # --- Preset path — unchanged shape; kept for scripted installs ---
    if preset:
        if preset not in PRESETS:
            console.print(
                f"[red]Unknown preset '{preset}'. "
                f"Options: {', '.join(PRESETS.keys())}[/red]"
            )
            return
        role_assignments = apply_preset(preset, available_set, ollama_models)
        daily_budget = budget if budget is not None else 15.0
        active = {prov for prov, _ in role_assignments.values()}
        console.print(f"Using preset: [bold]{preset}[/bold]\n")
        run_scan_final = run_scan if run_scan is not None else False
        # Presets default cortex integration to `auto` — the plan's
        # Success Criterion #10 only requires a wizard prompt. Presets
        # are explicitly scripted, so honoring auto-detection without a
        # prompt is the least-surprising behavior here.
        cortex_enabled = "auto"
    else:
        # --- Wizard path (interactive or flag-driven) ---
        providers_set = _parse_providers_flag(providers, available_set)
        coder_parsed = _parse_role_flag(coder, "--coder")
        reviewer_parsed = _parse_role_flag(reviewer, "--reviewer")

        flags = {
            "interactive": is_interactive,
            "providers_set": providers_set,
            "coder": coder_parsed,
            "reviewer": reviewer_parsed,
            "budget": budget,
            "run_scan": run_scan,
            "project_path": project,
        }
        (
            active, role_assignments, daily_budget,
            run_scan_final, cortex_enabled,
        ) = _run_wizard(
            available_set, dict(statuses), ollama_models, flags,
        )

    # Show the final role assignments
    console.print()
    _render_role_assignments(role_assignments)
    console.print()

    # Write files
    _write_config(
        project, project_type, role_assignments, daily_budget,
        cortex_enabled=cortex_enabled,
    )
    _write_sentinel_gitignore(project)
    _install_claude_templates(project)
    _ensure_gitignore_entries(project)
    if seed_defaults:
        _seed_doctrine_defaults(project)

    # Doctrine 0002 §5 — print the equivalent flag-form at the end of a
    # successful wizard so scripters learn flags by using the tool.
    # Skipped for preset invocations (the user already knows the flag
    # form) and implicit auto-inits (they didn't ask for a wizard).
    if not preset and not implicit:
        _print_equivalent_flag_form(
            active, role_assignments, daily_budget, run_scan_final,
        )

    # Done
    console.print("\n[bold green]Done![/bold green]\n")
    console.print("  [bold]Next steps:[/bold]")
    console.print(
        "    [dim]1.[/dim] Run [cyan]sentinel work[/cyan] "
        "[dim](scans, plans, executes refinements; proposes expansions)[/dim]"
    )
    console.print(
        "    [dim]2.[/dim] For continuous mode: [cyan]sentinel work --every 10m[/cyan]"
    )
    console.print()

    # Optionally kick off a scan immediately. Only runs when the user
    # explicitly opted in — either via --scan or the wizard's yes.
    if run_scan_final:
        console.print("\n[bold cyan]→ Running initial scan[/bold cyan]\n")
        import asyncio

        from sentinel.cli.scan_cmd import run_scan as _run_scan_cmd
        try:
            asyncio.run(_run_scan_cmd(quick=False))
        except Exception as exc:  # noqa: BLE001
            # Scan failures must not mask init success — init already
            # wrote config. Surface the error loudly per the no-silent-
            # failures principle.
            console.print(
                f"  [yellow]Initial scan failed: {exc}. Run "
                "`sentinel scan` manually to retry.[/yellow]",
            )


# ---------- Rendering helpers ----------


def _render_provider_table(statuses: dict) -> None:
    table = Table(show_header=False, padding=(0, 2))
    table.add_column("status", width=3)
    table.add_column("name", width=10)
    table.add_column("detail")

    for name, status in statuses.items():
        if status.installed and status.authenticated:
            models_str = ", ".join(status.models[:3]) if status.models else "ready"
            table.add_row("[green]✓[/green]", name, f"[green]{models_str}[/green]")
        elif status.installed:
            table.add_row(
                "[yellow]![/yellow]", name,
                "[yellow]installed but not ready[/yellow]",
            )
            console.print(f"    [dim]{status.auth_hint}[/dim]")
        else:
            table.add_row("[red]✗[/red]", name, "[red]not found[/red]")

    console.print(table)


def _show_install_hints(statuses: dict) -> None:
    console.print()
    console.print(
        Panel(
            "[yellow]No providers available.[/yellow]\n\n"
            "Sentinel needs at least one LLM provider CLI installed. Options:\n\n"
            + "\n".join(
                f"  • {name}: [cyan]{s.install_hint}[/cyan]"
                for name, s in statuses.items() if not s.installed
            ),
            title="[bold red]Setup Incomplete[/bold red]",
            border_style="yellow",
        )
    )
    console.print()


def _render_role_assignments(
    role_assignments: dict[RoleName, tuple[ProviderName, str]],
) -> None:
    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Role", width=12)
    table.add_column("Provider", width=10)
    table.add_column("Model", width=25)
    table.add_column("Why")

    for role in RoleName:
        provider, model = role_assignments[role]
        rec = RECOMMENDED[role]
        why = ""
        if provider != rec.provider:
            why = f"[dim](fallback from {rec.provider.value})[/dim]"
        table.add_row(role.value, provider.value, model, why)

    console.print("[bold]Role assignments:[/bold]")
    console.print(table)


def _write_config(
    project: Path,
    project_type: str,
    role_assignments: dict[RoleName, tuple[ProviderName, str]],
    daily_budget: float,
    *,
    cortex_enabled: str = "auto",
) -> None:
    sentinel_dir = project / ".sentinel"
    sentinel_dir.mkdir(exist_ok=True)
    config_path = sentinel_dir / "config.toml"

    if config_path.exists():
        console.print(
            "  [yellow]![/yellow] .sentinel/config.toml already exists — "
            "skipping"
        )
        return

    import tomli_w

    warn_at = max(1.0, daily_budget * 0.7)
    config_dict = {
        "project": {
            "name": project.name,
            "path": str(project),
            "type": project_type,
        },
        "roles": {
            role.value: {"provider": prov.value, "model": model}
            for role, (prov, model) in role_assignments.items()
        },
        "budget": {
            "daily_limit_usd": daily_budget,
            "warn_at_usd": round(warn_at, 2),
        },
        "scan": {"max_lenses": 10, "evaluate_per_lens": True},
        # Coder role tuning. `timeout_seconds` caps each agentic Claude
        # CLI call — raise for complex refactors, slow networks, or
        # large projects. Range: 60-7200. Overridable per-invocation
        # via `--coder-timeout` / SENTINEL_CODER_TIMEOUT. (Cortex C5.)
        "coder": {"timeout_seconds": 600},
        # Cortex T1.6 integration — write Sentinel-cycle journal
        # entries at cycle end. `auto` honors `.cortex/` presence at
        # runtime (default); `on`/`off` force the behavior. Runtime
        # override via `--cortex-journal` / `--no-cortex-journal`.
        "integrations": {"cortex": {"enabled": cortex_enabled}},
    }
    config_path.write_bytes(tomli_w.dumps(config_dict).encode())
    console.print("  [green]✓[/green] Created .sentinel/config.toml")


_SENTINEL_DIR_GITIGNORE = """\
# Sentinel runtime state — ephemeral, do not commit.
state/

# Durable artifacts — commit these.
# (listed explicitly for clarity; not actually negated here because
# git treats untracked files as not-ignored by default)
#   config.toml
#   backlog.md
#   lenses.md
#   domain_brief.md
#   runs/
#   proposals/
#   scans/
"""


def _write_sentinel_gitignore(project: Path) -> None:
    """Write .sentinel/.gitignore so ephemeral state/ never gets committed.

    Without this, a user's first `git add .sentinel/` after auto-init
    stages runtime state (state/) alongside durable artifacts (config,
    lenses, backlog, runs). Shipping the gitignore at init time keeps
    the user from having to learn which subpaths are durable.

    Never overwrite — the user may have customized this file.
    """
    sentinel_dir = project / ".sentinel"
    sentinel_dir.mkdir(exist_ok=True)
    gitignore_path = sentinel_dir / ".gitignore"
    if gitignore_path.exists():
        return
    gitignore_path.write_text(_SENTINEL_DIR_GITIGNORE)
    console.print("  [green]✓[/green] Created .sentinel/.gitignore")


def _install_claude_templates(project: Path) -> None:
    sentinel_root = _find_sentinel_root()
    templates_src = sentinel_root / "templates" / ".claude"
    claude_dst = project / ".claude"

    if not templates_src.exists():
        return

    created = _copy_tree(templates_src, claude_dst)
    if created:
        console.print(
            f"  [green]✓[/green] Installed {len(created)} Claude Code files to .claude/"
        )


_SENTINEL_GITIGNORE_MARKER = "# sentinel artifacts"
# The exact full-line marker the legacy sentinel-init generator wrote.
# `_migrate_stale_sentinel_gitignore_line` requires this exact string
# (not just the substring) before stripping, so a user's custom comment
# that happens to start with "# sentinel artifacts" can never be matched
# as a migration target.
_SENTINEL_GITIGNORE_MARKER_LINE = (
    "# sentinel artifacts — generated per-run, not source"
)
# R5.2 (autumn-garage journal 2026-04-18-r5-findings-from-fresh-scaffold):
# we deliberately do NOT blanket-ignore ``.sentinel/`` at the project root.
# ``.sentinel/`` holds durable artifacts meant to be committed (config.toml,
# runs/, proposals/, scans/, backlog.md, lenses.md, domain_brief.md). The
# ephemeral subtree (``state/``) is excluded by the per-directory
# ``.sentinel/.gitignore`` that ``_write_sentinel_gitignore`` installs
# alongside this call. Blanket-ignoring ``.sentinel/`` at the root overrides
# that design and silently hides every durable artifact from git.
# ``.claude/`` stays here because it's Claude Code's per-user cache,
# correctly project-external.
_SENTINEL_GITIGNORE_BLOCK = """\

# sentinel artifacts — generated per-run, not source
.claude/
"""


def _ensure_gitignore_entries(project: Path) -> None:
    """Append sentinel artifact paths to the target project's .gitignore.

    Without this, every `git status` / `open-pr.sh` run warns about
    uncommitted .claude/ files — friction that makes users either
    ignore warnings (bad) or commit the artifacts (worse).
    Idempotent: checks for our marker comment before appending.

    R5.2 migration: earlier sentinel versions wrote a `.sentinel/` line
    into the generated block, which silently hid every durable artifact
    (config.toml, runs/, proposals/, scans/, backlog.md, lenses.md,
    domain_brief.md). When we detect an existing generated block that
    still contains the stale `.sentinel/` line, we remove just that
    line — leaving the rest of the user's .gitignore untouched — so
    re-running `sentinel init` on an already-initialized project
    actually repairs the bug this PR was written to fix.

    Commits the .gitignore change if we're inside a git repo, because
    otherwise `sentinel work`'s `_reset_and_checkout` would wipe the
    change on its first reset --hard between items.
    """
    gitignore = project / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""

    # Whole-line equality on the marker line — NEVER a substring match.
    # A user comment like "# sentinel artifacts I added myself" must not
    # be mistaken for our generated marker (otherwise init would skip
    # appending its own block, leaving .claude/ unmanaged).
    has_managed_block = any(
        line == _SENTINEL_GITIGNORE_MARKER_LINE
        for line in existing.splitlines()
    )
    if has_managed_block:
        migrated = _migrate_stale_sentinel_gitignore_line(existing)
        if migrated is None:
            return
        gitignore.write_text(migrated)
        console.print(
            "  [green]✓[/green] Migrated .gitignore: removed stale "
            "`.sentinel/` blanket entry (R5.2)",
        )
        _commit_gitignore_if_in_repo(project)
        return

    # Preserve the existing file's trailing newline situation; append
    # with a leading newline so our block doesn't glue onto the last
    # existing entry.
    separator = "" if existing.endswith("\n") or not existing else "\n"
    gitignore.write_text(existing + separator + _SENTINEL_GITIGNORE_BLOCK)
    action = "Created" if not existing else "Updated"
    console.print(
        f"  [green]✓[/green] {action} .gitignore with sentinel/claude entries",
    )

    _commit_gitignore_if_in_repo(project)


def _migrate_stale_sentinel_gitignore_line(existing: str) -> str | None:
    """Remove the stale ``.sentinel/`` line from the legacy sentinel-
    generated block of an existing .gitignore.

    Returns the migrated text if a change is needed, ``None`` if the
    file is already compliant.

    The legacy generated block had exactly this shape::

        # sentinel artifacts — generated per-run, not source
        .sentinel/
        .claude/

    with no trailing blank delimiter, which means we can't use a blank
    line as the lower bound of the block — users who appended their own
    ``.sentinel/`` below ``.claude/`` without an intervening blank would
    have that line eaten by a naive scan. Matching must also be exact
    on the marker line itself (not a substring match): a user's own
    comment that starts with "# sentinel artifacts" (e.g. "# sentinel
    artifacts I added myself") must never be mistaken for the legacy
    generator's marker.

    So we strip ``.sentinel/`` only when the following three-line
    signature matches exactly::

        <full legacy marker line>
        .sentinel/
        .claude/

    That signature uniquely identifies the legacy generated block and
    leaves every user-authored entry alone.
    """
    lines = existing.splitlines(keepends=True)
    try:
        marker_idx = next(
            i for i, line in enumerate(lines)
            if line.rstrip("\n") == _SENTINEL_GITIGNORE_MARKER_LINE
        )
    except StopIteration:
        return None

    stale_idx = marker_idx + 1
    claude_idx = marker_idx + 2
    if claude_idx >= len(lines):
        return None
    if lines[stale_idx].strip() != ".sentinel/":
        return None
    if lines[claude_idx].strip() != ".claude/":
        return None

    new_lines = lines[:stale_idx] + lines[stale_idx + 1:]
    return "".join(new_lines)


def _commit_gitignore_if_in_repo(project: Path) -> None:
    """Commit .gitignore change when the project is a git repo.

    We do this explicitly so `sentinel work`'s between-item reset
    doesn't discard the gitignore edit. The commit is a single
    .gitignore change with a descriptive message the user can revert
    with `git revert` if they don't want it.

    Silent noop when: not a git repo, no changes staged, pre-commit
    hook rejects — sentinel init should never hard-fail on gitignore
    housekeeping.
    """
    try:
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=project, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if check.returncode != 0 or check.stdout.strip() != "true":
        return

    # Check if there's anything to commit (tracked diff or untracked
    # new file). `git diff` only sees tracked files so we also check
    # git status for the untracked case.
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".gitignore"],
        capture_output=True, text=True, cwd=project, timeout=10,
    )
    if not status.stdout.strip():
        # .gitignore unchanged (already matched ignore rules or no diff)
        return

    # Stage .gitignore specifically so a new (untracked) file ends up
    # in the index. `git commit -- pathspec` only commits tracked paths.
    subprocess.run(
        ["git", "add", "--", ".gitignore"],
        capture_output=True, cwd=project, timeout=10,
    )

    # `git commit <pathspec>` scopes the commit to ONLY .gitignore,
    # ignoring anything else the user may have staged in their index.
    commit = subprocess.run(
        ["git", "commit", "-m",
         "chore: gitignore sentinel artifacts (.sentinel/, .claude/)",
         "--", ".gitignore"],
        capture_output=True, text=True, cwd=project, timeout=30,
    )
    if commit.returncode == 0:
        console.print(
            "  [dim]→ Committed .gitignore change so sentinel work "
            "resets don't discard it.[/dim]",
        )
    else:
        # Don't fail init — just warn. User can commit manually.
        console.print(
            "  [yellow]! Could not commit .gitignore change:[/yellow] "
            f"{commit.stderr.strip() or commit.stdout.strip()}"
        )
        console.print(
            "  [dim]  Commit it manually before `sentinel work` or "
            "the between-item reset will revert it.[/dim]",
        )
