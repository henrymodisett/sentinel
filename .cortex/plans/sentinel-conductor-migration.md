---
Status: active
Written: 2026-04-24
Author: claude-code (Henry Modisett, @henry@perplexity.ai)
Goal-hash: sen2cdr01
Updated-by:
  - 2026-04-24T01:30 claude-code (initial plan — Slice A brew hygiene shipped, Slices B-E scoped, seam=Python-import)
  - 2026-04-28 claude-code (moved here from autumn-garage per house rule; Slice B + Key design decisions revised: seam = subprocess, not import)
Cites:
  - autumn-garage/.cortex/plans/sentinel-autonomous-engineer.md (cross-cutting decision 4 — subprocess seam)
  - autumn-garage/.cortex/plans/touchstone-conductor-integration
  - autumn-garage/.cortex/plans/conductor-http-tool-use
  - autumn-garage/.cortex/journal/2026-04-24-local-llm-dogfood
  - autumn-garage/.cortex/doctrine/0003-llm-providers-compose-by-contract
  - autumn-garage/.cortex/doctrine/0004-conductor-as-fourth-peer
  - GitHub issue: autumngarage/sentinel#89 (ConductorAdapter subprocess migration tracking)
---

# Sentinel → Conductor migration

> Collapse sentinel's own `src/sentinel/providers/` onto conductor. Sentinel keeps its role loops and high-level contracts; every LLM call underneath becomes a `conductor call` subprocess invocation. Completes the trio→quartet→collapse arc started with touchstone v2.0.

> **2026-04-28 update.** Plan moved from `autumn-garage/.cortex/plans/` to this repo (per "tool-specific plans live in tool repos" house rule). The seam decision flipped from Python import → per-call subprocess; rationale captured in autumn-garage's `sentinel-autonomous-engineer.md` § Cross-cutting decision 4 (research-backed: ~2% overhead at cycle frequency; daemon patterns like LSP/MCP apply at keystroke frequency, not ours). Slice B's adapter shape is unchanged; its *implementation* shifts to `subprocess.run(['conductor', 'call', ...])` from `importlib.import_module`. The git-ref pin in `pyproject.toml:13` is dropped as part of Slice B. Slices C/D/E unchanged in spirit. Tracking issue: autumngarage/sentinel#89.

## Why

Three drivers:

1. **Doctrine 0003 / 0004**: "every tool in the garage composes over one provider contract." Touchstone collapsed onto conductor in v2.0 (-448 lines, one adapter, subprocess seam). Sentinel is the last consumer maintaining a parallel provider layer. Every bug fix, every new provider, every capability axis (effort, sandbox, tool-use, context budget) has to be implemented twice today.
2. **Sentinel dogfood gains**: conductor's v0.3.3 shipped graceful fallback, capability filters, session resume, per-iteration cost tracking, context-budget halts, silent-fail diagnostics for local LLMs. Sentinel inherits none of these without the migration.
3. **Local LLM story**: sentinel's `LocalProvider` says `agentic_code=False` — sentinel's coder can't use ollama today. Conductor's v0.3.x HTTP tool-use loop gives sentinel's coder a local path for the first time.

## Current-state audit

Captured in full during 2026-04-24 reconnaissance (see autumn-garage journal for findings). Summary:

**Sentinel's provider layer** (5 files, ~2000 lines + ~2100 lines of tests):
- `interface.py`: `Provider` ABC with `chat` / `code` / `research` / `chat_json` / `detect` + `ChatResponse` dataclass + `ProviderCapabilities` + shared subprocess + budget helpers
- `claude.py` / `openai.py` / `gemini.py` / `local.py`: concrete impls
- `router.py`: picks provider for each role, applies `DEFAULT_RULES` task-aware overrides, scopes coder's timeout separately

**Features sentinel has that conductor doesn't** (need to port or keep sentinel-side):
- `chat_json(prompt, schema)` — structured-output with schema validation (stays sentinel-side; validation logic doesn't depend on seam shape)
- `DEFAULT_RULES` — per-task/prompt-size model overrides (stays sentinel-side; router pre-resolves model, then calls subprocess)
- Per-role timeout isolation (coder's timeout is separate from monitor/planner/researcher) — passed to subprocess via `--timeout-sec` or wall-clock cap on `subprocess.run(timeout=...)`
- `max_turns` config — passed via `--max-turns`
- `stderr` + `raw_stdout` on every `ChatResponse` path — preserved from `subprocess.run` `stderr`/`stdout`
- `session_id` captured from CLI output — parsed from JSON stdout

**Features conductor has that sentinel doesn't** (inherited post-migration):
- `supported_tools` / `supported_sandboxes` capability declarations → router-enforced before call
- `--prefer best / cheapest / fastest / balanced`
- `--effort minimal..max` with per-provider token translation
- `--exclude <name>` to skip a provider
- `call.usage.iterations` cost-per-turn log
- `hit_context_budget` + `hit_iteration_cap` signals on HTTP loops
- Graceful one-hop fallback on 5xx / 429 / timeout
- Silent-fail stealth-tool-call guard (v0.3.3)
- `--resume <session_id>` for multi-turn sessions

## Target-state design

**Layer the sentinel API onto conductor, don't rewrite the roles.** The role loops (`roles/monitor.py`, `roles/coder.py`, etc.) should not need to change. Keep sentinel's `Provider` interface as the contract the roles consume; implement that contract by shelling out to `conductor call`.

### Architecture after migration

```
sentinel.roles.{monitor,coder,reviewer,…}
         │
         │ (unchanged API)
         ▼
sentinel.providers.Provider   ◄── kept: the ABC + ChatResponse dataclass
         │
         │ (new single implementation)
         ▼
sentinel.providers.conductor_adapter.ConductorAdapter
         │
         │ subprocess.run(['conductor', 'call', ...])
         ▼
conductor (binary on $PATH; independent release cadence)
```

The adapter is the only place that knows about conductor. The roles keep their current imports. Sentinel keeps its config schema (users don't retype anything). Router keeps its `DEFAULT_RULES` — it picks the model name, then the adapter routes through `conductor call` with that model.

### Key design decisions

- **Subprocess, not Python import** (revised 2026-04-28). The 2026-04-24 design chose import for "we own both sides; subprocess overhead per call is unnecessary." The 2026-04-28 reconsideration overrules: at cycle frequency, subprocess overhead is ~2% of wall time (50 calls × 200ms ≈ 10s vs minimum 8 min cycle); the architectural wins (independent release cadence, process isolation, Doctrine 0003 compliance, Touchstone uniformity, language agnosticism) outweigh that. Daemon patterns (LSP / MCP / mypy `dmypy` / `eslint_d`) apply at keystroke frequency, not ours; git's subprocess-everywhere model is the gold-standard precedent. Conductor binary is found via `which conductor` at adapter init; clear error if missing.
- **Drop the git-ref pin** in `pyproject.toml:13`. Conductor is no longer a Python dependency; it's a CLI on `$PATH` (declared via brew formula `depends_on "autumngarage/tools/conductor"`).
- **`chat_json` stays in sentinel.** Conductor doesn't expose a schema-validating call. Sentinel's adapter implements `chat_json` on top of `conductor call` with a prompt-engineering preamble + `json.loads` + pydantic/jsonschema validate. This keeps conductor's surface small.
- **`DEFAULT_RULES` stays in sentinel.** Task-aware model overrides are a sentinel concern (it's about which model sentinel's workload wants, not a router-universal rule). Sentinel's router pre-resolves the model, then calls `conductor call --with <provider> --model <model>`.
- **Per-role timeout stays in sentinel.** Sentinel's `Router` constructs per-role `ConductorAdapter` instances with role-specific timeouts. The adapter passes `timeout_sec` both to `subprocess.run(timeout=...)` (wall-clock cap) and to `conductor call --timeout-sec <n>` (conductor-internal cap).
- **`code()` shells `conductor call` with exec-mode flags** (sentinel's current coder path); for local/kimi it shells the same `conductor call` with HTTP tool-use loop flags. Sentinel's `CoderConfig.max_turns` maps to `--max-turns` on the conductor CLI. Verify the exec-mode flag surface during Slice B; file conductor PRs if any flag is missing.
- **`ChatResponse.stderr / raw_stdout / session_id` preserved.** Sentinel's adapter reads these from `subprocess.run`'s `stderr` / `stdout` and from conductor's structured JSON output (which includes `session_id`, usage, etc.).

## Slice plan

### Slice A — brew hygiene ✅ shipped 2026-04-24

- Sentinel brew formula already at v0.3.6 with matching sha256
- Description trimmed from 82 → 74 chars to pass `brew audit --strict`
- Verified `brew install autumngarage/sentinel/sentinel` gives a working sentinel; `sentinel providers` detects all four providers end-to-end
- No code change; one-line formula fix committed to homebrew-sentinel

### Slice B — `ConductorAdapter` shim (~1 session) — REVISED 2026-04-28

Write `src/sentinel/providers/conductor_adapter.py` implementing sentinel's `Provider` ABC by shelling out to `conductor call`:

- **Init**: resolve `which conductor`; cache the path; fail fast with a clear error if missing. Stash provider_name, model, timeout_sec, max_turns, ollama_endpoint.
- **`chat()`** → `subprocess.run(['conductor', 'call', '--with', provider_name, '--model', model, '--effort', 'medium', '--json', ...], input=prompt, capture_output=True, timeout=timeout_sec, text=True)`. Parse JSON stdout into ChatResponse (preserving stderr, raw_stdout, session_id, usage).
- **`chat_json()`** → `chat()` with JSON-shaped prompt + parse + `jsonschema` validate.
- **`research()`** → `chat()` (for providers with `web_search` capability).
- **`code()`** → `subprocess.run(['conductor', 'call', '--exec', '--with', provider_name, '--tools', tools_csv, '--sandbox', 'workspace-write', '--cwd', working_directory, '--timeout-sec', coder_timeout, '--max-turns', max_turns, '--json', ...], ...)`. Verify the exact exec-mode flag surface during this slice; file conductor PRs if any flag is missing or named differently (see autumngarage/conductor#93).
- **`detect()`** → `subprocess.run(['conductor', 'doctor', '--with', provider_name, '--json'])` and parse `configured: bool`.

Drop `conductor @ git+...@v0.3.3` from `pyproject.toml`. Add brew formula declaration `depends_on "autumngarage/tools/conductor"` (handled in homebrew-sentinel formula update; not in this repo's PR).

Feature-flag at construction so the migration is reversible during the transition window (`SENTINEL_USE_CONDUCTOR=0` reverts to native providers). Tests: ~40 new tests in `tests/test_conductor_adapter.py` covering every method + error paths, with `subprocess.run` mocked at the boundary.

**Spike: measure subprocess overhead before committing.** Before flipping the default in Slice C, run a real sentinel cycle on a scratch repo with `SENTINEL_USE_CONDUCTOR=1`. Pass criterion: subprocess overhead ≤5% of total cycle wall time. If failed, the autumn-garage decision-doc explicitly contemplated `conductor serve` daemon mode as Wave 2 hedge; bring that back as a follow-up issue.

**Risks addressed in Slice B:**
- R6 (stderr/raw_stdout/cost on every path) — adapter populates from `subprocess.run` `stderr`/`stdout` and conductor's JSON output
- R7 (cost on non-zero exit) — adapter parses any usage/cost the JSON includes even on non-zero exit; conductor preserves usage on `ProviderHTTPError` already
- R10 (tool-use loop ownership) — conductor's CLI handles the loop internally; adapter just passes `--max-turns`

**Risks deferred to Slice C/D:**
- R1 `--disallowedTools` for claude chat mode — needs a conductor change to accept a deny-list flag (file conductor PR)
- R9 ollama endpoint — need a way to pass `OLLAMA_BASE_URL` per-call (currently env-only in conductor; may need flag)

### Slice C — router migration (~0.5 sessions)

- Update `src/sentinel/providers/router.py` to instantiate `ConductorAdapter` instead of `ClaudeProvider`/`OpenAIProvider`/etc.
- `DEFAULT_RULES` keep working — they pick the model, router passes it to the adapter
- Add `SENTINEL_USE_CONDUCTOR` env flag: when unset or falsy, fall back to native providers; when set, use adapter
- Feature-flag lets us merge without flipping default; flip default after Slice D validates end-to-end
- Tests: existing `test_router.py` stays mostly intact — now runs against the adapter with `subprocess.run` mocked

### Slice D — role dogfood (~1-2 sessions)

Run real sentinel cycles against autumn-mail or a scratch repo with `SENTINEL_USE_CONDUCTOR=1`:
1. **Monitor first** — simplest role, all `chat()` / `chat_json()`, no tool-use. If monitor works end-to-end, the common path is validated.
2. **Researcher** — chat-only + web_search implicit.
3. **Reviewer** — `chat_json()` with structured verdict schema. Stresses the JSON path hardest.
4. **Planner** — currently stubbed in sentinel, low risk.
5. **Coder** — agentic `code()`, highest risk. Validates `--max-turns`, `--dangerously-skip-permissions` flow through conductor's exec mode.

Each role is a separate commit; each is a real-workload validation, not a unit test. Expect to file small conductor PRs for gaps surfaced here (R1, R9).

Flip the default (`SENTINEL_USE_CONDUCTOR=1` → default true) once all five roles pass a real cycle.

### Slice E — delete native providers (~0.5 sessions)

- Delete `src/sentinel/providers/claude.py`, `openai.py`, `gemini.py`, `local.py` (not `interface.py` — the ABC + `ChatResponse` stay as the contract the adapter satisfies).
- Router loses its `ProviderName → ProviderClass` map; constructs only `ConductorAdapter`.
- Delete `tests/test_providers.py` (242 lines) and `tests/test_openai_ndjson.py` (105 lines) — now conductor's responsibility.
- Trim `tests/test_router.py` to test adapter-instantiation + `DEFAULT_RULES`, not provider internals.
- Net: -1800 to -2200 lines depending on what gets trimmed from integration tests.

Bump sentinel to v0.4.0 for the version landmark ("sentinel now runs on conductor exclusively, via subprocess").

## Risks (from the recon, restated)

| # | Risk | Addressed in slice | Notes |
|---|---|---|---|
| R1 | Claude's `--disallowedTools` for chat safety | B (open gap) → C (conductor change) | Likely needs a new flag on `conductor call` |
| R2 | Gemini `--approval-mode plan` read-only | — | Conductor's gemini adapter already does this — verify in Slice D |
| R3 | Per-role timeout isolation | B | Adapter takes `timeout_sec` in ctor; passed to subprocess `timeout=` and `--timeout-sec` |
| R4 | Task-aware `DEFAULT_RULES` | C | Router keeps them; they run before the adapter call and just pick `model=` |
| R5 | `max_turns` propagation | B → C | Pass via `--max-turns` flag on `conductor call --exec` |
| R6 | `stderr` + `raw_stdout` on all paths | B | Adapter populates from `subprocess.run` and conductor JSON |
| R7 | Cost on non-zero exit | B | Conductor preserves usage on ProviderHTTPError; JSON output includes it |
| R8 | OpenAI NDJSON parsing | — | Conductor's codex adapter already does this — delete sentinel's parser in Slice E |
| R9 | Ollama endpoint config | B (open gap) | Conductor today reads `OLLAMA_BASE_URL` env only; may need flag for per-call override |
| R10 | Tool-use loop ownership | B | Conductor's CLI handles the loop; adapter passes `--max-turns` |

## Success criteria

1. **A real `sentinel work` cycle on autumn-mail completes with `SENTINEL_USE_CONDUCTOR=1`** — all 5 roles executed, no regression in output quality vs the pre-migration run
2. **`sentinel providers` shows identical output** pre- and post-migration (same providers detected, same capabilities reported)
3. **Test count within ±50 of pre-migration** — we delete ~2000 lines of provider tests but add ~500 lines of adapter tests; net reduction is the win
4. **Conductor gets 1-3 small PRs** filed as gaps surface (R1, R9, exec-mode flag confirmation) — all bounded, none requiring architectural change
5. **Slice E deletion is clean** — no remaining imports of `sentinel.providers.claude` etc. in the tree, no `import conductor` left in sentinel src
6. **Subprocess overhead spike passes** — ≤5% of cycle wall time on a real sentinel run
7. **sentinel v0.4.0 cut**: brew formula bumped (with `depends_on "autumngarage/tools/conductor"`), release published

## Out of scope explicitly

- **Changing sentinel's role logic.** Not touching `roles/*.py`. The migration is under the provider interface.
- **Replacing sentinel's budget tracking.** `_abort_if_budget_exhausted` and `_journal_call` stay.
- **Rewriting sentinel's config schema.** Users' `.sentinel/config.toml` stays unchanged.
- **Adding conductor features beyond the surfaced gaps.** If Slice D uncovers an R1/R9-class gap, that's one small PR. Not "rewrite conductor to suit sentinel."
- **Conductor daemon mode (`conductor serve`).** Reserved as Wave 2 hedge if subprocess-overhead spike fails. Not on critical path.

## Relationship to the master integration plan

Finishes Stage 5 of `autumn-garage/.cortex/plans/touchstone-conductor-integration` (explicitly blocked on conductor v0.3 HTTP tool-use — shipped 2026-04-23). Closes the "trio→quartet→collapse" transformation from a code-architecture perspective; the trio comment in autumn-garage state.md becomes literally true.
