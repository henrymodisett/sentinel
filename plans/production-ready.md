# Sentinel — Production-Ready Plan

**Author:** drafted from the 2026-04-16 dogfood session
**Status:** proposed
**Owner:** Henry

## What "production-ready" means here

A user can `brew install sentinel` (or `pip install sentinel`), `cd` into any project of theirs, run `sentinel work`, and get a useful result without crashing, without surprising costs, and without damage to their repo. The full ASSESS → RESEARCH → PLAN → DELEGATE → REVIEW loop completes end-to-end. Failures are loud, diagnosable from the journal alone, and never silently corrupt downstream work. Sentinel can run on itself nightly without intervention.

Three concrete release gates:

1. **Self-cycle gate** — `sentinel work --budget 30m` completes one full cycle on the Sentinel repo itself, end-to-end, with no manual intervention, on three consecutive nightly runs.
2. **Foreign-repo gate** — same command completes on at least two unrelated user repos (one Python, one non-Python) without crashing.
3. **Install gate** — `brew install henrymodisett/sentinel/sentinel && sentinel work` works on a clean macOS machine in under five minutes.

Until all three gates pass, we are not production-ready.

---

## Where we are today (post-dogfood snapshot, 2026-04-16)

**Working:**
- Provider abstraction (claude, codex, gemini, ollama) cleanly wraps CLIs, no API keys touched.
- Monitor's dynamic lens generation produces astonishingly relevant project-specific lenses (it generated `llm-budgeting` and `dogfood-readiness` for the Sentinel repo unprompted).
- Per-cycle journal (#45), checkpoint-on-append (#47), partial-scan rescue (#42), per-lens timeout (#48) all behave as designed.
- `sentinel scan --quick` is a clean, free state baseline.

**Broken or stubbed:**
- `_research_phase` raises `NotImplementedError` (`src/sentinel/loop/cycle.py`).
- `_execute_phase` processes only the top 3 backlog items.
- `sentinel status` is a stub.
- Mid-call budget clamping kills in-flight Gemini calls, producing zero output for the latency cost.
- `gemini-2.5-pro` reproducibly exits non-zero (~222s) on the explore prompt for this repo. Root cause unknown — stderr is not preserved in the journal, so we are flying blind.
- Synthesis on `gemini-2.5-flash` times out at 600s. Wrong model for the task; no router knows that.
- `.sentinel/runs/` is missing from `.gitignore` (the gitignore block that handles `.sentinel/` artifacts wasn't updated when #45 added the directory).

**Decided in this session:**
- The fallback ladder includes a user-installable local LLM (ollama) as a first-class rung, not optional cleanup.
- The routing decision space is multi-dimensional `(provider × model)`. We need a meta-router — start with rules, add an LLM router only when rules become unmaintainable.
- Budget should gate *new* calls, never preempt in-flight ones. Time and money are separate dimensions; for free providers (gemini OAuth, ollama), money is a no-op.

---

## Workstreams

Five workstreams, sequenced by dependency. Each item is small enough to ship in one PR.

### WS-1: Routing & Budget — the core redesign

**Why first:** Every other workstream calls into the router. Until routing is right, we will keep paying latency for nothing and failing on the wrong model for the task.

#### 1.1 Replace mid-call clamping with between-call gating

- Remove `clamp_timeout` shrinking of subprocess timeouts in `src/sentinel/budget_ctx.py`.
- Add a `budget_remaining()` check at the start of every provider call — if ≤ 0, return early with a clear `budget_exhausted` error in the journal, do not start the call.
- Provider calls run at the provider's own configured timeout (no dynamic shrinking).
- **Acceptance:** the same 5m-budget scan that produced two clamped errors and zero useful output now produces *either* one fully-completed call (if budget allows) *or* zero calls (if the prior phase ate the budget) — never half-finished work.

#### 1.2 Decouple money budget from time budget

- `--budget 5m` and `--budget $5` are separate dimensions, both checkable, never conflated.
- For free-providers-only sessions, the money budget is silently inactive (logged, not enforced).
- `daily_limit_usd` continues to apply as a hard global cap.
- **Acceptance:** `sentinel cost` clearly shows time-spent vs money-spent, with each cap reported independently.

#### 1.3 Rules-based router

- New module `src/sentinel/router/rules.py` (or extend `providers/router.py` if it stays under ~400 lines).
- Replaces the current static role→model mapping with a function `select(role, task_kind, prompt_size, session_state) → (provider, model)`.
- Initial rules (encode what dogfooding observed):
  - `evaluate_lens` + prompt > 60k tokens → `gemini-2.5-flash` (pro fails on huge prompts here)
  - `synthesize` → `gemini-2.5-pro` (flash times out on synthesis)
  - any model that has failed twice in this session → skip for the rest of the cycle
  - if no remote provider is healthy → fall through to ollama
- Routing decisions written to the journal as a new `routing` event per call.
- **Acceptance:** the same dogfood scan that crashed at synthesis on flash now picks pro for synthesis and completes. Journal shows the routing decision and rationale.

#### 1.4 ollama as guaranteed bottom rung

- `Router` detects whether the configured ollama model is pulled.
- If not, surface `ollama pull <model>` to the user with a clear message and pause the cycle (do not silently degrade past it). Resume on next invocation.
- A sensible default ollama model is chosen by `sentinel init` (e.g. `qwen2.5-coder:7b`) and written to `config.toml`. User can override.
- **Acceptance:** kill internet, run `sentinel work --dry-run` — the cycle still completes a useful scan on ollama alone, journal shows ollama was selected because remotes were unhealthy.

#### 1.5 Preserve stderr in the journal

- When any provider CLI exits non-zero, capture stderr (truncated to ~2KB) into the journal entry alongside the existing `error` classification.
- **Acceptance:** the next reproducible `gemini-pro` non-zero exit is diagnosable from the journal alone, no live re-run needed.

---

### WS-2: Pipeline completion

**Why second:** Routing has to be right first or these phases will inherit the same problems.

#### 2.1 Implement `_research_phase`

- Researcher role takes the synthesized scan and identifies which work items would benefit from a research brief (e.g., "best practice for X in domain Y").
- Researcher uses Gemini's built-in Google grounding (already wrapped).
- Brief is cached at `.sentinel/research/<work-item-id>.md` (this is derived state, but caching is justified — re-derived on context-hash change, same pattern as `domain_brief.md`).
- **Acceptance:** the research phase produces at least one brief for the dogfood scan and the brief content shows up in the Planner's prompt.

#### 2.2 Lift the top-3 limit in `_execute_phase`

- Current implementation hard-caps at 3 items. Replace with: process all items in priority order until budget exhausted (between-call gating from 1.1 makes this safe).
- Per-item failure does not block the next item — log the failure, continue.
- **Acceptance:** a backlog of 7 items processes all 7 (or as many as budget allows) in one cycle, with per-item pass/fail in the journal.

#### 2.3 Wire Planner output → Coder input properly

- Audit the contract between `plan_cmd.py`'s output (`backlog.md` and `proposals/*.md`) and what the Coder consumes.
- Specifically: the Coder needs structured fields (title, why, lens, files, acceptance). Confirm parser handles all current format edge cases.
- **Acceptance:** a hand-crafted backlog.md with three diverse items is consumed and dispatched without parse errors.

#### 2.4 Replace `goals.md` with derivation from CLAUDE.md/README/issues

- This was flagged by the `state-derivation` lens (75/100). Currently `ProjectState.goals_md` reads a user-maintained file, which violates "derive, don't persist."
- Replace with: a small LLM call that derives current goals from CLAUDE.md + README.md + open GitHub issues. Cache with the same hash-invalidation strategy as `domain_brief.md`.
- Allow `--goal "<one-line override>"` on the CLI for ad-hoc focus.
- **Acceptance:** `goals.md` can be deleted from any repo and `sentinel work` still produces a coherent goal-aware scan.

---

### WS-3: UX & safety

**Why third:** Once the loop completes reliably, the next gate is "would I trust this on a stranger's repo?"

#### 3.1 Coder execution sandbox

- The Coder is the only role that writes to disk. Today it has full repo write access.
- Add: confirmation prompt before the first execution of any cycle (suppressible with `--auto`, which already exists on `work_cmd.py`).
- Add: a per-cycle execution log of every file the Coder touched, with diff summary.
- Optional: `--dry-run-coder` flag that runs the Coder against a worktree copy and shows the diff without applying.
- **Acceptance:** a dry-run-coder execution against the dogfood scan produces a diff in a worktree that the user can review before approving.

#### 3.2 Failure surfacing in CLI output

- Today, when a phase fails (e.g., synthesis timeout), the user sees a single line and has to dig into the journal.
- Add: a structured failure summary at the end of any failed `sentinel work` invocation showing (a) which phase failed, (b) the routing decision that produced the failure, (c) the suggested next action ("the synthesis step needs `gemini-2.5-pro` — your config currently routes it to flash; run `sentinel routing show` to inspect").
- **Acceptance:** the next dogfood failure is diagnosable from terminal output alone, no journal-spelunking required.

#### 3.3 First-run UX

- `sentinel init` should:
  - Detect installed providers and pick sensible defaults (claude > gemini > codex > ollama for paid; ollama always as fallback).
  - Write a config the user can immediately run with no edits.
  - Suggest the right ollama model to pull.
- `sentinel work` on a fresh repo should never show a stack trace — every error path has a friendly message.
- **Acceptance:** `git clone <some random python repo> && cd && sentinel init && sentinel work --dry-run --budget 5m` completes without crash on a clean machine.

---

### WS-4: Observability

**Why fourth:** Once the loop is reliable, the next priority is *understanding* what it did.

#### 4.1 Cost attribution per role

- Today the journal lumps cost by phase. Add per-role attribution so we can see "Researcher cost $X, Coder $Y" in `sentinel cost`.
- Useful for tuning the router — if Researcher is cheap and producing value, push it harder; if Coder is expensive and producing rework, throttle.
- **Acceptance:** `sentinel cost --by-role` shows a 7-day breakdown.

#### 4.2 Routing decision audit

- Every routing decision (from WS-1.3) is logged. Add `sentinel routing show` to dump the recent routing decisions and their outcomes — a feedback loop for improving the rules.
- **Acceptance:** after a week of runs, `sentinel routing show` reveals at least one rule that needs adjusting based on observed outcomes.

#### 4.3 Self-evaluation feedback

- After each cycle, the Reviewer's verdict on each work item (pass/fail/partial) is rolled up into a session summary.
- Over time this becomes the dataset for an LLM-driven router (the future replacement for the rules-based one).
- **Acceptance:** the per-cycle journal includes a "verifier verdict" section summarizing how many work items passed independent verification.

---

### WS-5: Distribution & release

**Why last:** No point shipping until the loop works. But these are mechanical and can be parallelized with WS-3/WS-4 once WS-1 and WS-2 are stable.

#### 5.1 Homebrew formula

- Mirror the toolkit pattern (`henrymodisett/sentinel` tap).
- Formula installs into a managed venv, exposes `sentinel` on PATH.
- **Acceptance:** `brew install henrymodisett/sentinel/sentinel` on a clean Mac and `sentinel --version` works.

#### 5.2 PyPI package

- `pip install sentinel` (claim the name now if not yet claimed).
- Pinned dependencies, lock file shipped.
- **Acceptance:** `pip install sentinel` in a fresh venv and `sentinel --version` works.

#### 5.3 Release script

- Mirrors toolkit: bump `__init__.py`, tag, push, `gh release create`, update Homebrew SHA — one script.
- **Acceptance:** release a v0.1.0-beta following the script with no manual steps.

---

## Sequencing

```
Week 1:  WS-1 (routing & budget) — entire workstream
Week 2:  WS-2.1 (research phase) + WS-2.4 (derive goals)
Week 3:  WS-2.2 + WS-2.3 (full execute, planner→coder contract) + WS-1.5 (stderr capture, can slot anywhere)
Week 4:  WS-3 (UX & safety) — entire workstream
Week 5:  Self-cycle gate — three consecutive nightly runs of `sentinel work --budget 30m` on this repo, fix anything that breaks.
Week 6:  WS-4 (observability) + start WS-5 (distribution)
Week 7:  Foreign-repo gate — run on two unrelated user repos, fix what breaks.
Week 8:  Install gate — clean Mac, `brew install`, ship v0.1.0-beta.
```

Total: ~2 months of focused work to v0.1.0-beta. Add a buffer.

---

## Acceptance criteria for v0.1.0-beta (production-ready)

A bullet list the user can grade against:

- [ ] All three release gates pass (self-cycle ×3, foreign-repo ×2, install).
- [ ] No `NotImplementedError` reachable from `sentinel work`.
- [ ] No silent failures: every error path either re-raises or writes a journal entry with stderr included.
- [ ] Mid-call preemption is gone; in-flight calls always complete or never start.
- [ ] Router selects different models for different tasks based on observed failures.
- [ ] ollama works as the bottom rung when configured, with a clear download prompt when not.
- [ ] Coder execution requires confirmation by default, runs against a worktree by default.
- [ ] `sentinel cost` shows per-role attribution.
- [ ] `sentinel routing show` reveals routing decisions and outcomes.
- [ ] `goals.md` is derivable, not required.
- [ ] One-command install via brew or pip succeeds on a clean machine.

---

## Out of scope (explicitly deferred)

- LLM-driven meta-router (rules + observed degradation are sufficient until they aren't).
- Full schema versioning for lenses (premature).
- Web dashboard for journal visualization (CLI is enough for v0.1).
- Multi-cycle learning / fine-tuning a router model on accumulated data.
- Provider registration CLI (the friction of editing `router.py` is fine for now).
- Audit for magic numbers across the codebase (lens recommendation; not blocking).
- Formalizing a logging standard for the "no silent failures" principle (worthwhile, but the journal carries us for now).

---

## What this plan does *not* fix

Two structural concerns this plan acknowledges but doesn't solve:

1. **The `llm-budgeting` lens scored 92/100 on a system that crashes from its budget design.** The lens evaluator graded engineering quality, not design judgment. This is a deeper problem about how lenses score — the meta-evaluator needs to assess whether the *design* solves the user's actual problem, not just whether the *implementation* is clean. Worth a future conversation.

2. **Custom lens generation is non-deterministic.** Two runs on the same repo will produce different lens names and scopes. For dogfooding-driven improvement we may want a "stable lens set" mode where the lenses are pinned across cycles, or a way to roll up findings across cycles even when lens names drift. Not blocking v0.1, but flag for v0.2.
