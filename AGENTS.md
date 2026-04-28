# AGENTS.md — AI Reviewer Guide for Sentinel

<!-- touchstone:shared-principles:start -->
## Shared Engineering Principles (apply these first)

These principles are touchstone-owned and shared across every project. Apply them as the **primary coding and review criteria** before any project-specific rule below — an agent that lets a band-aid or a silent failure through has missed the point of this gate.

- **No band-aids** — fix the root cause; if patching a symptom, say so explicitly and name the root cause.
- **Keep interfaces narrow** — expose the smallest stable contract; don't leak storage shape, vendor SDKs, or workflow sequencing.
- **Derive limits from domain** — thresholds and sizes come from input/config/named constants; test at small, typical, and large scales.
- **Derive, don't persist** — compute from the source of truth; persist derived state only with a documented invalidation + rebuild path.
- **No silent failures** — every exception is re-raised or logged with debug context. No `except: pass`, no swallowed errors.
- **Every fix gets a test** — bug fix includes a regression test that runs in CI and fails on the old code.
- **Think in invariants** — name and assert at least one invariant for nontrivial logic.
- **One code path** — share business logic across modes; confine mode-specific differences to adapters, config, or the I/O boundary.
- **Version your data boundaries** — when a model/algorithm/source change affects decisions, version the boundary; don't aggregate across.
- **Separate behavior changes from tidying** — never mix functional changes with broad renames, formatting sweeps, or unrelated refactors.
- **Make irreversible actions recoverable** — destructive operations need a dry-run, backup, idempotency, rollback, or forward-fix plan before they run.
- **Preserve compatibility at boundaries** — public API/config/schema/CLI/hook/template changes need a compatibility or migration plan.
- **Audit weak-point classes** — when a structural bug is found, audit the class and add a guardrail; don't fix only the one instance.

Full rationale, worked examples, and the *why* behind each rule:

- `principles/engineering-principles.md`
- `principles/pre-implementation-checklist.md`
- `principles/documentation-ownership.md`
- `principles/git-workflow.md`

This block is managed by `touchstone` and refreshes on `touchstone update` / `touchstone init`. Edit content **outside** the markers to add project-specific agent guidance — touchstone will not touch it.
<!-- touchstone:shared-principles:end -->


You are reviewing pull requests for **Sentinel**, an autonomous meta-agent that manages software projects across multiple LLM providers. Optimize your review for catching the things that bite this repo, not generic style polish.

This file is the source of truth for how AI reviewers (Codex, Claude, etc.) should think about a PR. The companion file `CLAUDE.md` is for the *author* writing the code; this file is for the *reviewer*.

---

## What to prioritize (in order)

1. **Provider abstraction integrity.** Any change that breaks the Provider interface or makes one provider behave differently than others in ways the router doesn't account for. The abstraction must hold — user config says "use gemini for researcher" and it works identically to "use claude for researcher."
2. **Config schema safety.** Changes to Zod schemas that could break existing `.sentinel/config.toml` files. Backward compatibility matters — users have configured projects.
3. **Role boundary violations.** Code in one role that reaches into another role's concerns. The monitor should not plan. The planner should not write code. The coder should not review its own output.
4. **Cost control.** Any code path that could make unbounded API calls or fail to respect budget limits. A runaway loop against Claude Opus is expensive.
5. **Secret handling.** API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY) must never be logged, committed, or sent to a provider other than their owner.
6. **Silent failures in the loop.** The five-step cycle must be observable. If a step fails, the failure must be visible — not swallowed so the next step runs on garbage input.

Style nits, formatting, and theoretical refactors are **out of scope** unless they hide a bug. Do not flag them.

---

## Specific review rules

### High-scrutiny paths

Files: `src/providers/`, `src/loop/`, `src/config/schema.ts`

Flag any of the following:
- Provider implementations that don't match the interface contract
- Router logic that could send a request to a provider that doesn't support the required capability
- Config schema changes without migration logic for existing configs
- Loop steps that proceed without checking the previous step's output
- Any hardcoded model IDs outside of the defaults in `config/schema.ts`

### Silent failures

Flag any of the following:

- New `catch` blocks that swallow errors without logging.
- API calls without timeout or budget checks.
- Provider health checks that return `true` without actually verifying connectivity.
- Research results used without checking if the research actually succeeded.
- Default values returned on error without a log line.

The rule: every exception is either re-raised or logged with enough context to debug from production logs alone.

### Tests

- Bug fixes must include a test that reproduces the original failure mode.
- Provider implementations should have integration tests (mocked API responses).
- The loop should have tests for partial failure scenarios (what if the researcher fails but everything else works?).
- Config schema changes need tests for backward compatibility.

---

## What NOT to flag

- Formatting, whitespace, import order — pre-commit hooks handle these.
- Type annotations on existing untyped code.
- "You could refactor this for clarity" — only if the unclarity hides a bug.
- Missing docstrings on small private functions.
- Speculative future-proofing — don't suggest abstractions for hypothetical future requirements.
- Naming preferences absent a clear convention violation.

If you find yourself writing "consider" or "you might want to" without a concrete bug or risk attached, delete the comment.

---

## Output format

1. **Summary** — one paragraph: what this PR does and your overall verdict (approve / request changes / comment).
2. **Blocking issues** — bugs or risks that must be fixed before merge. Each item: file:line, what's wrong, why it matters, suggested fix.
3. **Non-blocking observations** — things worth noting but not blocking. Keep this section short.
4. **Tests** — does this PR add tests for the changed behavior? If not, is that OK?

If there are zero blocking issues, the review is just: "LGTM."

<!-- conductor:begin v0.8.2 -->
## Conductor delegation

This project has [conductor](https://github.com/autumngarage/conductor)
available for delegating tasks to other LLMs from inside an agent loop.
You can shell out to it instead of trying to do everything yourself.

Quick reference:

- Quick factual/background ask:
  `conductor ask --kind research --effort minimal --brief-file /tmp/brief.md`.
- Deeper synthesis/research:
  `conductor ask --kind research --effort medium --brief-file /tmp/brief.md`.
- Code explanation or small coding judgment:
  `conductor ask --kind code --effort low --brief-file /tmp/brief.md`.
- Repo-changing implementation/debugging:
  `conductor ask --kind code --effort high --brief-file /tmp/brief.md`.
- Merge/PR/diff review:
  `conductor ask --kind review --base <ref> --brief-file /tmp/review.md`.
- Architecture/product judgment needing multiple views:
  `conductor ask --kind council --effort medium --brief-file /tmp/brief.md`.
- `conductor list` — show configured providers and their tags.

Conductor does not inherit your conversation context. For delegation,
write a complete brief with goal, context, scope, constraints, expected
output, and validation; use `--brief-file` for nontrivial `exec` tasks.
Default to `conductor ask`; use provider-specific `call` / `exec` only
when the user explicitly asks for a provider or the semantic API does not
fit.

Providers commonly worth delegating to:

- `kimi` — long-context summarization, cheap second opinions.
- `gemini` — web search, multimodal.
- `claude` / `codex` — strongest reasoning / coding agent loops.
- `ollama` — local, offline, privacy-sensitive.
- `council` kind — OpenRouter-only multi-model deliberation and synthesis.

Full delegation guidance (when to delegate, when not to, error handling):

    ~/.conductor/delegation-guidance.md
<!-- conductor:end -->
