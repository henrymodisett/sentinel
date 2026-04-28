---
Status: superseded
Written: 2026-04-20
Author: human
Goal-hash: f7a23901
Updated-by:
  - 2026-04-20T00:00 human (created; extracted from plans/llm-provider-additions follow-ups)
  - 2026-04-20T00:00 human (superseded by plans/conductor-bootstrap; rename happens by Sentinel adopting Conductor's canonical identifiers via shell-out, not by an in-place file rename)
Cites: doctrine/0003-llm-providers-compose-by-contract, doctrine/0004-conductor-as-fourth-peer, plans/conductor-bootstrap, journal/2026-04-20-conductor-decision, integration/providers.md
---

> **SUPERSEDED 2026-04-20 by [`plans/conductor-bootstrap.md`](conductor-bootstrap.md).** This plan proposed an in-place rename of `src/sentinel/providers/openai.py` → `codex.py` plus a backward-compat alias. Same-day refinement extracted Conductor as the canonical owner of provider identifiers — Sentinel migrates to `conductor call` shell-outs, at which point the local `openai.py` / `OpenAIProvider` / `ProviderName.OPENAI` symbols are deleted entirely (not aliased). The user-facing `provider = "openai"` config value still needs the deprecation alias when Sentinel migrates; that alias logic moves into the future Sentinel→Conductor migration plan. The Pydantic validator pattern, `_warned` sentinel idiom, and tests outlined here remain useful — read this plan for the migration *technique*, not as the active workstream.

# Rename Sentinel's `openai` provider identifier to `codex` for cross-tool consistency

> Sentinel labels the Codex CLI provider as `openai` (`src/sentinel/providers/openai.py`, class `OpenAIProvider`, enum `ProviderName.OPENAI`). Touchstone calls the same thing `codex`. Doctrine 0003 §1 mandates identifiers match across tools. Rename Sentinel's identifier to `codex` with a one-version backward-compat alias so existing user configs continue to work, then deprecate `openai` in the next minor.

## Why (grounding)

Doctrine 0003 (shipped 2026-04-20) names a contract that every garage tool implementing an LLM provider must satisfy. Field 1 of the contract: *"A short, lowercase, single-word identifier... The same identifier is used in every tool's config."*

The drift exists because Sentinel was built when "OpenAI" felt like the more general label — Codex is a CLI from OpenAI, after all. But:

- The CLI is named `codex`, not `openai`. Touchstone's reviewer cascade uses `codex` as the identifier (`hooks/codex-review.config.example.toml` line 64: *"Valid reviewer IDs: 'codex', 'claude', 'gemini', 'local'"*).
- A user reading both tools' configs sees `provider = "openai"` in `.sentinel/config.toml` and `reviewers = ["codex"]` in `.codex-review.toml` and reasonably asks: are these the same provider or different ones? They're the same. That confusion is exactly what Doctrine 0003 prevents going forward.
- If a real OpenAI-API HTTP provider is added later (e.g., to call GPT models without the Codex CLI), `openai` is the natural identifier for it. Holding the slot for the CLI wrapper blocks the better name for the future HTTP wrapper.

Pre-existing drift, not blocking the Kimi rollout. But the doctrine ships with a known violation if we don't fix it. Better to clear it on the same day the doctrine lands.

Grounds-in: `.cortex/doctrine/0003-llm-providers-compose-by-contract`.

## Approach

**Single Sentinel PR with backward-compat alias.** The change is mechanical (rename file, class, enum value) plus a config-parsing alias so users with `provider = "openai"` in `.sentinel/config.toml` see no breakage on the upgrade.

**Migration path:**

1. **Sentinel vNext (this PR):** add `CODEX = "codex"` to `ProviderName`, register `CodexProvider` (renamed from `OpenAIProvider`), accept `provider = "codex"` everywhere. Keep `OPENAI = "openai"` in the enum as an alias that resolves to the same provider class. Config parser maps `"openai"` → `"codex"` with a deprecation warning printed to stderr on load.
2. **Sentinel vNext+1:** drop the alias. `provider = "openai"` raises a clear error directing users to rename to `"codex"`.

**Why two-step migration vs immediate break:**

- Sentinel users have `.sentinel/config.toml` files in their projects. A breaking rename forces a coordinated update across every project. A one-version alias gives users a clean upgrade path with a visible warning.
- Touchstone has done multi-step renames before (R5 ordering work) — the precedent exists.

**File-level changes** (Sentinel repo):

- `src/sentinel/providers/openai.py` → `src/sentinel/providers/codex.py` (git mv).
- Inside that file: class `OpenAIProvider` → `CodexProvider`. Class attribute `name = ProviderName.OPENAI` → `name = ProviderName.CODEX`. Module docstring updated.
- `src/sentinel/providers/interface.py`: add `CODEX = "codex"` to `ProviderName` enum. Keep `OPENAI = "openai"` as an alias; document with a comment that it's deprecated.
- `src/sentinel/providers/router.py` (and wherever providers are instantiated): register `CodexProvider` for both `ProviderName.CODEX` and `ProviderName.OPENAI` so old configs keep working.
- `src/sentinel/config/schema.py`: validator that normalizes `"openai"` → `"codex"` at parse time and emits a `DeprecationWarning` (or stderr line — match existing Sentinel deprecation style if any).
- `sentinel init` wizard: offer `codex` as the choice; do not show `openai` as an option to new users.
- `tests/`: rename `test_openai_provider.py` → `test_codex_provider.py`. Add a test that loading a config with `provider = "openai"` succeeds and produces the same behavior as `provider = "codex"`. Add a test that the deprecation warning fires.
- `README.md` and any provider docs: rename in user-facing docs.

**Update `integration/providers.md`** in autumn-garage to remove the "pre-existing drift" note from the `codex` row once Sentinel ships the rename. The row stays the same (identifier already documented as `codex` because that's what the doctrine says); only the parenthetical "(Sentinel currently labels this `openai`...)" comes out.

## Success Criteria

This plan is done when all of the following hold:

1. Sentinel PR shipped: `src/sentinel/providers/codex.py` exists; `OpenAIProvider` is gone (or aliased — see below); `ProviderName.CODEX` is the canonical enum value.
2. A user with an existing `.sentinel/config.toml` containing `provider = "openai"` runs `sentinel work` and sees: (a) the cycle runs successfully, (b) a single deprecation warning to stderr saying "provider 'openai' is deprecated; use 'codex' (will be removed in v0.X)".
3. A user with the new `provider = "codex"` runs without any deprecation warning.
4. `sentinel init` wizard prompts offer `codex` and do not list `openai`.
5. All Sentinel tests pass, including the new tests for the alias + deprecation warning.
6. Sentinel README and any user-facing provider docs use `codex` as the identifier.
7. `autumn-garage/integration/providers.md` is updated to remove the drift note from the `codex` row, citing the Sentinel PR that closed it.
8. A journal entry in autumn-garage records the rename shipping (`2026-MM-DD-sentinel-codex-rename-shipped.md`), citing this plan and the Sentinel PR URL.
9. Doctrine 0003 §1 is now satisfied for the `codex` identifier across Touchstone, Sentinel, and `integration/providers.md`.

## Work items

### Sentinel repo

- [ ] `git mv src/sentinel/providers/openai.py src/sentinel/providers/codex.py`.
- [ ] In `codex.py`: rename `OpenAIProvider` → `CodexProvider`, update `name` attribute, update docstring, update any `cli_command` or default-model references that should reflect the new identity (the CLI command `codex` doesn't change; only the Python identifier does).
- [ ] In `src/sentinel/providers/interface.py`: add `CODEX = "codex"` to `ProviderName` enum. Mark `OPENAI = "openai"` with a comment `# Deprecated alias for CODEX; remove in v0.X.`.
- [ ] Update `src/sentinel/providers/router.py` (or wherever the registry is) to map both `ProviderName.CODEX` and `ProviderName.OPENAI` to `CodexProvider`.
- [ ] Add a config validator that normalizes `provider = "openai"` → `provider = "codex"` and emits one deprecation warning per cycle (not per call).
- [ ] Update `sentinel init` wizard to offer `codex` (not `openai`) for any role that prompts.
- [ ] Update `src/sentinel/__init__.py`, `src/sentinel/__init__.pyi`, or any package exports that name `OpenAIProvider`.

### Tests (Sentinel repo)

- [ ] Rename `tests/test_openai_provider.py` → `tests/test_codex_provider.py` (if it exists; confirm by `ls tests/`).
- [ ] Update all test imports from `sentinel.providers.openai` → `sentinel.providers.codex`.
- [ ] New test: config with `provider = "openai"` loads successfully and produces a `CodexProvider` instance.
- [ ] New test: deprecation warning fires once when loading a config with `provider = "openai"`.
- [ ] New test: config with `provider = "codex"` loads with no warnings.
- [ ] All existing Sentinel tests pass without modification (the rename is internal).

### Docs (Sentinel repo)

- [ ] `README.md`: search for `openai` / `OpenAIProvider` / `OPENAI` and update user-facing mentions to `codex` / `CodexProvider` / `CODEX`.
- [ ] If Sentinel has a `docs/providers.md` or similar, update.
- [ ] Changelog / release-note entry: "Provider identifier `openai` renamed to `codex` for consistency with Touchstone and the autumn-garage provider contract (Doctrine 0003). Existing configs with `provider = 'openai'` continue to work with a deprecation warning until vX.Y.Z."

### Coordination repo (autumn-garage)

- [ ] Update `integration/providers.md`: remove the "pre-existing drift" note from the `codex` row once Sentinel PR ships. Bump "Last update" date.
- [ ] Journal the rename shipping.
- [ ] Update `state.md` to reflect this workstream's status.

### Future PR (Sentinel vNext+1)

- [ ] Drop the `OPENAI` alias. `provider = "openai"` raises a clear error: *"provider 'openai' is no longer supported; rename to 'codex' in your .sentinel/config.toml. See <link to autumn-garage providers.md>."*
- [ ] Drop the validator that normalizes the old name.
- [ ] Drop the alias-related tests; keep the test that proves the error message is helpful.

## Follow-ups (deferred)

- **Audit other identifiers for similar drift.** Doctrine 0003 was extracted from a single observed drift. There may be others — e.g., does Sentinel call Gemini `google` anywhere? Does the `local` identifier mean different things across tools (yes — see `plans/local-llm-provider-alignment.md`)? Resolves to a periodic audit, not a separate plan.
- **Consider a real `openai` HTTP provider.** Once `openai` is freed up as an identifier (after the deprecation completes), a future provider that calls OpenAI's HTTP API directly (without going through Codex CLI) can claim the name. Resolves to a future plan if/when needed.

## Known limitations at exit

- **Deprecation window is one minor version.** Users who skip versions (e.g., upgrade from v0.3.x to v0.5.x) see the hard error directly without ever seeing the warning. Acceptable because the error message is explicit about the rename.
- **Config files in `templates/` directories or example configs in docs** outside Sentinel's own repo (e.g., in autumn-mail's `.sentinel/config.toml`) need a manual update. Not blocking — those are project-owned files; the deprecation warning will surface them.

## Meta — why this plan is in autumn-garage, not in sentinel

The rename is a single-tool change in implementation, but the *reason* for the rename (cross-tool identifier consistency) is a coordination decision. Per autumn-garage Doctrine 0001, decisions that span two or more tools live here even if the implementation is single-tool. When the Sentinel agent picks up the work, this plan + Doctrine 0003 + `integration/providers.md` is the brief.
