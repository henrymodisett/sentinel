# Sentinel — Conductor Migration Plan

**Author:** drafted 2026-04-24 during garage-wide post-v0.3.3 audit
**Status:** in progress (Slice A shipped; B-E open)
**Owner:** Henry
**Upstream plan of record:** [`autumn-garage/.cortex/plans/sentinel-conductor-migration.md`](https://github.com/autumngarage/autumn-garage/blob/main/.cortex/plans/sentinel-conductor-migration.md) — cross-tool coordination decisions live there; this file is the sentinel-local actionable view.

## What this does

Collapse `src/sentinel/providers/` onto [conductor](https://github.com/autumngarage/conductor) (the garage's dedicated LLM router). Sentinel keeps its role loops (`monitor`, `researcher`, `planner`, `coder`, `reviewer`), its config schema, and its `Provider` ABC + `ChatResponse` dataclass. Only the **concrete provider implementations** (`claude.py`, `openai.py`, `gemini.py`, `local.py`) and the routing logic inside `router.py` get replaced — by a single `conductor_adapter.py` that delegates every LLM call to conductor.

This completes the "trio → quartet → collapse" arc the garage has been working toward since touchstone v2.0 (PR #54) deleted its own provider adapters onto conductor. Sentinel is the last consumer maintaining a parallel provider layer.

## Why now

Three drivers, ranked by impact:

1. **Conductor v0.3.x gains that sentinel inherits on day one** — graceful 5xx/429/timeout fallback, session resume, per-iteration cost log, context-budget halts, silent-fail diagnostics for local LLMs, capability-aware auto-routing (prefer/effort/tools/sandbox/exclude). None of these exist in `src/sentinel/providers/` today.
2. **Local coder unblocked** — sentinel's `LocalProvider` declares `agentic_code=False`, so the coder role refuses to run on ollama. Conductor v0.3.x shipped the HTTP tool-use loop on ollama; post-migration the coder role can target a local model for the first time.
3. **One provider codebase to maintain** — every new LLM capability (new thinking mode, new provider, new sandbox semantics) today has to be coded twice. Post-migration, sentinel gets it for free when conductor ships it.

## Target-state shape

```
src/sentinel/roles/{monitor,coder,reviewer,…}.py   (unchanged)
         │
         │ (same sentinel.providers.Provider ABC, same ChatResponse)
         ▼
src/sentinel/providers/interface.py                (unchanged — the contract)
         ▲
         │ (new single implementation)
         │
src/sentinel/providers/conductor_adapter.py        (NEW in Slice B)
         │
         │ (Python import, not subprocess)
         ▼
conductor.providers.{Claude,Codex,Gemini,Kimi,Ollama}Provider
```

Key guarantee: **roles don't change**. If you `git diff src/sentinel/roles/` at the end of Slice E, it's empty.

## Slice status

### Slice A — brew hygiene ✅ shipped 2026-04-24

- [homebrew-sentinel#8622241](https://github.com/autumngarage/homebrew-sentinel/commit/8622241) — description trimmed 82 → 74 chars to pass `brew audit --strict`. Formula was already at v0.3.6 with matching sha256.
- Verified: `brew install autumngarage/sentinel/sentinel` gives a working `sentinel 0.3.6`; `/opt/homebrew/bin/sentinel providers` detects all four providers end-to-end alongside the existing `uv tool` install (PATH ordering keeps uv-editable first for dev).
- No sentinel-repo code change in Slice A.

### Slice B — `ConductorAdapter` shim (next)

**Scope:** one new file, no role changes, no behaviour change at runtime (feature-flagged off).

**Files to add:**
- `src/sentinel/providers/conductor_adapter.py` — new
- `tests/test_conductor_adapter.py` — new, ~40 tests

**Files to touch:**
- `pyproject.toml` — add `conductor` to `dependencies`. Use a git spec until conductor is on PyPI: `conductor @ git+https://github.com/autumngarage/conductor.git@v0.3.3`
- `src/sentinel/providers/__init__.py` — export `ConductorAdapter`

**API the adapter must implement** (matches existing `Provider` ABC in `src/sentinel/providers/interface.py`):

```python
class ConductorAdapter(Provider):
    def __init__(
        self,
        *,
        provider_name: Literal["claude", "openai", "gemini", "kimi", "local"],
        model: str,
        timeout_sec: int = 600,
        max_turns: int = 40,
        ollama_endpoint: str | None = None,  # passed through only for provider_name="local"
    ) -> None: ...

    async def chat(self, prompt: str, *, system_prompt: str | None = None) -> ChatResponse: ...
    async def chat_json(self, prompt: str, schema: dict) -> tuple[dict | None, ChatResponse]: ...
    async def research(self, prompt: str) -> ChatResponse: ...
    async def code(self, prompt: str, *, working_directory: str) -> ChatResponse: ...
    def detect(self) -> ProviderStatus: ...
```

**Translation layer** (conductor's surface → sentinel's surface):

| Sentinel method | Conductor call | Notes |
|---|---|---|
| `chat()` | `conductor.providers.get_provider(name).call(prompt, model=model, effort='medium', resume_session_id=None)` | Map `conductor.CallResponse` → `sentinel.ChatResponse` |
| `chat_json()` | `chat()` + JSON-shaped preamble + `json.loads` + `jsonschema.validate` | Schema validation stays in sentinel — conductor has no equivalent |
| `research()` | `chat()` | Web search is implicit on providers that support it (gemini); no conductor flag needed |
| `code()` | `get_provider(name).exec(prompt, tools=frozenset({"Read","Grep","Glob","Edit","Write","Bash"}), sandbox='workspace-write', cwd=working_directory, timeout_sec=timeout_sec)` | For claude/codex, conductor's shell-out `exec()` passes `--max-turns` to the CLI; for kimi/local, conductor drives the HTTP tool-use loop with the v0.3.x iteration cap |
| `detect()` | `get_provider(name).configured()` + capability lookup | Map `configured() -> (bool, reason)` to `ProviderStatus(installed, authenticated, hints)` |

**Feature flag** — construct the adapter only when `SENTINEL_USE_CONDUCTOR=1` is in the environment. When unset or `0`, keep using the native providers. The flag is read in `router.py` (Slice C) — in Slice B the adapter just exists, nothing consumes it yet.

**Known gaps that may surface during Slice B implementation** — if any bite, file a conductor PR rather than monkey-patching in sentinel:

- Conductor's claude adapter may not support `--disallowedTools Bash,Edit,Write,NotebookEdit` (used by sentinel's read-only chat mode). If missing, add a `deny_tools=` kwarg to conductor's claude `call()` — small PR.
- Conductor's ollama adapter reads `OLLAMA_BASE_URL` from env only. If sentinel needs per-instance endpoint override (it does — `config.local.ollama_endpoint`), add a `base_url=` kwarg on `OllamaProvider.__init__`. Already partially there; may just need plumbing through `get_provider()`.
- Conductor's HTTP tool-use loop uses a hardcoded `KIMI_MAX_TOOL_ITERATIONS=10`. If sentinel's `config.coder.max_turns` needs to override this, expose it as a kwarg on `exec()` — small PR.

**Design decisions already locked:**
- Python import, not subprocess. Adding `conductor` as a library dependency is cheaper than shelling out per call.
- `chat_json` stays in sentinel. Conductor's surface stays small; sentinel's adapter does JSON-shaped prompt + parse + validate.
- `DEFAULT_RULES` stays in sentinel. Task-aware model overrides (`synthesize → gemini-2.5-pro`, `evaluate_lens > 60k → gemini-2.5-flash`) are a sentinel concern — router picks the model, adapter routes through conductor with it.

**Tests expected** (in `tests/test_conductor_adapter.py`):
- Construction: each valid `provider_name`; invalid name raises `ValueError`
- `chat()` happy path: mocks `conductor.providers.get_provider(...).call()`, asserts args + response mapping
- `chat()` preserves `stderr`, `raw_stdout`, `cost_usd` on error paths (R6/R7 from the master plan)
- `chat_json()` happy path: valid JSON → `(parsed_dict, response)`; invalid JSON → `(None, response)` with `is_error=True`
- `chat_json()` schema violation: structurally valid JSON that fails jsonschema → `(None, response)`
- `code()` passes tools + sandbox correctly; respects `max_turns`
- `code()` on `local` provider goes through conductor's HTTP tool-use loop (new capability vs native sentinel which refused)
- `detect()` maps `configured()` output to `ProviderStatus`
- `timeout_sec` kwarg propagates to conductor's provider constructor
- `research()` delegates to `chat()` (web search is implicit)

### Slice C — router migration (~0.5 sessions)

Touch `src/sentinel/providers/router.py`:
- Behind `SENTINEL_USE_CONDUCTOR=1`, instantiate `ConductorAdapter` instead of `{Claude,OpenAI,Gemini,Local}Provider`
- `DEFAULT_RULES` keeps working — it runs first, picks the model name, then router constructs adapter with that model
- Per-role timeout isolation preserved: coder's adapter constructed with `timeout_sec=config.coder.timeout_seconds`; others with `config.scan.provider_timeout_sec`
- Flag default stays OFF — this slice is pure plumbing

Existing `tests/test_router.py` should continue to pass with the flag off; add a small parametrized set that enables the flag and verifies adapter construction.

### Slice D — role dogfood (~1-2 sessions)

Run real cycles with `SENTINEL_USE_CONDUCTOR=1` on a target repo (autumn-mail or a scratch dir). Per-role validation order by rising risk:

1. **Monitor** (simplest — `chat()` + `chat_json()`, read-only). If monitor completes one cycle cleanly, the 80%-case path is proven.
2. **Researcher** (chat-only + implicit web search).
3. **Reviewer** (stresses `chat_json()` hardest — structured verdicts).
4. **Planner** (stubbed in `roles/planner.py` today; low risk).
5. **Coder** (agentic `code()`, highest risk — validates `--max-turns` + tool-use flows through conductor's shell-out for claude/codex, and through conductor's HTTP loop for local).

Each role gets a commit. Each is real-workload validation, not a unit test. Expect 1-3 small conductor PRs for gaps surfaced (R1 `--disallowedTools`, R9 per-instance ollama endpoint, R5 `max_turns` kwarg on HTTP loop).

**Flip the default** (`SENTINEL_USE_CONDUCTOR=1` → default `true` in router) once all five roles pass.

### Slice E — delete native providers (~0.5 sessions)

- `rm src/sentinel/providers/{claude,openai,gemini,local}.py` (≈2000 lines)
- Trim `src/sentinel/providers/router.py` to only know about `ConductorAdapter`
- Keep `src/sentinel/providers/interface.py` — the ABC + `ChatResponse` + `ProviderCapabilities` + `ProviderStatus` stay as the contract the adapter satisfies
- Delete `tests/test_providers.py` (242 lines), `tests/test_openai_ndjson.py` (105 lines) — now conductor's responsibility
- Trim `tests/test_router.py` — task-aware rule logic stays, provider-internals tests go
- Net change: **-1800 to -2200 lines**
- Bump sentinel to **v0.4.0** — version landmark for "sentinel now runs exclusively on conductor"

## Risks from the 2026-04-24 recon

From the cross-tool recon done in the autumn-garage plan; restated here with the slice each one lands in:

| # | Risk | Slice | Notes |
|---|---|---|---|
| R1 | Claude's `--disallowedTools` for chat safety | B (gap) → C (conductor PR if needed) | Likely needs `deny_tools=` kwarg on conductor's claude `call()` |
| R2 | Gemini `--approval-mode plan` read-only | D | Conductor's gemini adapter already uses `plan` — verify in Slice D |
| R3 | Per-role timeout isolation | B | Adapter takes `timeout_sec` in ctor; router constructs per-role adapters in C |
| R4 | Task-aware `DEFAULT_RULES` | C | Router keeps them; they run before the adapter call |
| R5 | `max_turns` propagation | B → C | Pipe through to conductor's `exec()`; HTTP loops may need a kwarg |
| R6 | `stderr` + `raw_stdout` on all paths | B | Adapter populates from `conductor.CallResponse.raw` |
| R7 | Cost on non-zero exit | B | Conductor already preserves `usage` on `ProviderHTTPError` |
| R8 | OpenAI NDJSON parsing | E | Conductor's codex adapter already does this — sentinel's parser deletes cleanly |
| R9 | Ollama endpoint config | B (gap) | May need plumbing `base_url=` through `get_provider()` |
| R10 | Tool-use loop ownership | B | Shell-out for claude/codex; HTTP loop for local/kimi |

## Success criteria

1. `sentinel work` completes a full cycle on autumn-mail (or scratch repo) with `SENTINEL_USE_CONDUCTOR=1` — all 5 roles execute, output quality matches pre-migration runs
2. `sentinel providers` output identical pre- and post-migration (same providers detected, same capabilities)
3. Test count within ±50 of pre-migration after Slice E deletion
4. Conductor gains ≤3 small PRs filed as gaps surface — bounded, each <200 lines, none architectural
5. `git grep "from sentinel.providers.claude"` and the other three native-provider imports return zero matches after Slice E
6. **sentinel v0.4.0** cut: tag, GitHub release, brew formula bumped

## Out of scope (explicit)

- **Role logic changes.** `src/sentinel/roles/*.py` is unchanged. Any role improvement tempted during this work → file a separate issue.
- **Budget tracking replacement.** `_abort_if_budget_exhausted` and `_journal_call` stay as they are.
- **Config schema rewrite.** Users' `.sentinel/config.toml` stays unchanged through this entire plan. Provider names (`claude`/`openai`/`gemini`/`local`) in config still work; they get translated to conductor provider IDs inside the adapter.
- **Conductor redesign.** If a conductor gap surfaces, file a small PR. Don't reshape conductor to accommodate sentinel patterns.

## Where to pick up

**If you're back in this repo and want to start Slice B:**
1. Read this file's "Slice B" section
2. Read `src/sentinel/providers/interface.py` to refresh on `Provider` ABC, `ChatResponse`, `ProviderCapabilities`, `ProviderStatus`
3. Read `autumn-garage/.cortex/plans/sentinel-conductor-migration.md` for cross-tool context (same plan, broader framing)
4. Look at `src/sentinel/providers/claude.py` for a reference of how a sentinel provider translates CLI output into `ChatResponse` — the adapter does the same kind of translation but against `conductor.CallResponse` instead of `claude -p`
5. Start with the file skeleton + `__init__` + `chat()` method + its tests. That's enough for a first commit that can be reviewed before going deeper.
