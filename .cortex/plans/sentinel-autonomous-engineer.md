---
Status: active
Written: 2026-04-28
Author: claude-code (Henry Modisett)
Goal-hash: sendrop01
Updated-by:
  - 2026-04-28 claude-code (initial plan — product framing + cross-tool delegation structure)
  - 2026-04-28 claude-code (slimmed to cross-cutting only per house rule)
  - 2026-04-28 claude-code (moved here from autumn-garage per stricter rule: even cross-cutting plans live in the primary owner's repo. Sentinel is the product owner; autumn-garage holds only doctrine + state + journal + templates + shared infra)
Cites:
  - autumn-garage/.cortex/doctrine/0001-why-autumn-garage-exists
  - autumn-garage/.cortex/doctrine/0003-llm-providers-compose-by-contract
  - autumn-garage/.cortex/doctrine/0004-conductor-as-fourth-peer
  - autumn-garage/.cortex/doctrine/0006-autumn-garage-is-meta-context (when shipped)
  - .cortex/plans/sentinel-conductor-migration.md (this repo, in flight)
---

# Sentinel as drop-in autonomous engineer

> Position Sentinel as the autonomous engineer for any repo: drop in, sleep, wake up to a queue of small reviewed PRs. Conductor + Cortex + Touchstone are Sentinel's load-bearing dependencies via subprocess + file contract — Sentinel materially worse without them, but each peer keeps independent installability for other agents. Differentiates from Hermes (conversational monolith) and Devin/Codex (cloud, opaque) on local + auditable + composable + bounded.
>
> **Where this plan lives.** This is Sentinel's product plan. Per autumn-garage's meta-repo doctrine, even cross-cutting work plans live in the primary owner's repo — Sentinel here. Cross-cutting decisions journaled in autumn-garage's `.cortex/journal/`. Cross-cutting principles live in autumn-garage doctrine. Per-tool implementation work for the three peer tools is tracked as GitHub issues against their repos.

## Why now

Conversation 2026-04-28 surfaced three findings that reshape Sentinel's product story:

1. **Sentinel is a one-way Cortex *contributor*, not a *consumer*.** Every `.cortex/` reference in `~/repos/sentinel/` is detection, config, or write; zero reads. `cortex manifest --budget N` ships in cortex v0.2.3 and Sentinel calls it zero times. Each cycle is amnesiac.
2. **Sentinel's `import conductor.providers`** + git-ref pin in `pyproject.toml` violates Doctrine 0003 in spirit. Touchstone got it right with subprocess (`conductor call`). Sentinel is the outlier.
3. **No shipped engineering values.** Today every project hand-writes Doctrine; Sentinel ships with none. A baseline pack seeded by `sentinel init` is the most product-distinctive move available — Hermes ships skills (capabilities), not values.

Net: **Sentinel can be the only autonomous engineer that ships with project memory, engineering values, and an audit trail by default — and drops into any repo with one command.**

## Definition of done

A user runs `brew install autumngarage/garage/garage` and `cd into-any-repo && sentinel init`. Within 90 seconds:

1. All four tools installed and healthy (`sentinel doctor` green).
2. `.cortex/` initialized with default Sentinel Doctrine seeded.
3. Existing `CLAUDE.md` / `AGENTS.md` scanned-and-absorbed into Doctrine candidates per cortex flow.
4. `.touchstone-config` written with reviewer cascade defaults.
5. Conductor model resolved (env → keychain → wizard fallback).
6. First-cycle task sources surfaced (open issues with configurable label, `TODO(sentinel)` markers, failing tests, stale dep PRs, lints over threshold).
7. `sentinel work --auto --budget 5` runs unattended and produces:
   - 0–N PRs opened via Touchstone, each with body sourced from the cycle's `.sentinel/runs/<id>.md`.
   - Each PR linked to its cycle journal entry in `.cortex/journal/`.
   - Rejections journaled to Cortex (not the parallel `.sentinel/state/rejections.jsonl`); next cycle's manifest sees them.
   - Destructive changes halted with a `blocked-on-human` journal entry, not silently shipped.

## Architecture: three pillars → three subprocess contracts

Each pillar is owned by exactly one peer; Sentinel orchestrates via subprocess + file reads.

| Pillar | Owner | Read contract | Write contract |
|---|---|---|---|
| **Memory** | Cortex | `cortex manifest --budget N` (cycle start), `cortex grep` (mid-cycle) | `.cortex/journal/<date>-sentinel-cycle-<id>.md` (T1.6) — already shipped |
| **Engineering values** | Cortex | `.cortex/doctrine/*.md` via the manifest | Doctrine candidates from cycle proposals (rare; via `cortex promote`) |
| **Agent delegation** | Conductor | `conductor list --json` for capability lookup | `conductor call --with <id> --effort <e> --tools <set> --sandbox <mode>` per subagent invocation |

Plus PR-shape integration with Touchstone (Touchstone retains lane ownership):

| Concern | Owner | Contract |
|---|---|---|
| **Code review gate** | Touchstone | Pre-push hook calls `conductor call --tags code-review` (already shipped) |
| **PR open + body** | Touchstone | `open-pr.sh` reads `.sentinel/runs/<latest>.md` from a sentinel-authored branch as the PR body source (NEW seam — see § Cycle Artifact Schema) |

## Cross-cutting decisions (resolved 2026-04-28)

### 1. Default Doctrine pack — ships in this repo, seeded by `sentinel init`

**Decision:** Sentinel ships engineering-values Doctrine in `defaults/doctrine/`. `sentinel init` copies into the project's `.cortex/doctrine/` if that directory is empty or absent, with sequential numbering preserved (0001–0015) and a `Sentinel-baseline: true` frontmatter flag so projects can distinguish defaults from local entries. Projects supersede via Cortex's existing immutable-with-supersede mechanism.

**Rationale.** Patterns research: ESLint shareable configs (`eslint-config-airbnb`) ship as separate packages; TypeScript `@tsconfig/strictest` lives in its own repo; Renovate ships preset configs (`config:base`); Ruff ships defaults built-in. The mature pattern is "config packs are owned by whoever has the opinions about them, distributed through whatever channel is natural for that ecosystem." Cortex is a *format* (immutable + supersede + tiered triggers); the values are Sentinel's product opinion. Bundling them in cortex would couple the format to one tool's worldview. Bundling them in a separate `cortex-doctrine-defaults` tap adds a moving part for no gain. Sentinel-bundled is cleanest.

**Failure modes addressed.** Reseed-on-init blast: `sentinel init` only seeds when `.cortex/doctrine/` is empty, never overwrites. Drift between shipped baseline and local: `sentinel audit --doctrine` cross-checks (later workstream). Multiple tools wanting their own defaults: each tool can ship its own (e.g., `touchstone/defaults/doctrine/`); Cortex stays format-only, neutral.

### 2. Brew packaging — two taps: à la carte + meta-formula

**Decision:** 
- **`autumngarage/tools/` tap** — four independent formulas (`sentinel`, `conductor`, `cortex`, `touchstone`). Each declares `depends_on` for *true runtime* dependencies only (Sentinel hard-depends on Conductor after the seam fix; nothing else). Cortex/Touchstone documented as recommended companions in Sentinel's caveats text, not via `:recommended`/`:optional` syntax.
- **`autumngarage/garage/` tap** — meta-formula `garage` that depends on all four. `brew install autumngarage/garage/garage` is the one-command full-ecosystem install for the killer demo.

**Rationale.** Patterns research: Homebrew restricted `:optional` and `:recommended` to non-core taps after 2019, and even there they add complexity (users surprised by partial installs). Meta-formula pattern is established (`pre-commit`, others). Single-binary tools (terraform, dbt) prefer one formula per tool. Two taps gives both UX paths — drop-in via `garage`, à la carte via `tools` — without fiddling with optional-dependency syntax. Caveats text is the right place for "for the full experience, also install X" guidance.

### 3. Cycle artifact schema — frontmatter version + stable HTML anchors

**Decision:** `.sentinel/runs/<id>.md` carries both:

- **Frontmatter** (machine-readable, evolves by major version):
  ```yaml
  schema-version: 1.0
  sentinel-run-id: <uuid>
  timestamp: 2026-04-28T14:22:00Z
  cycle-id: <slug>
  branch: <git branch>
  status: completed | in-progress | failed | blocked-on-human
  ```
- **Body anchors** (immutable across schema versions, content evolves freely):
  ```markdown
  <!-- pr-body-start -->
  …Touchstone consumes this as PR body…
  <!-- pr-body-end -->

  <!-- decisions-start -->
  …promotable-to-Doctrine decisions…
  <!-- decisions-end -->

  <!-- transcript-start -->
  …full role-by-role transcript; not for PR…
  <!-- transcript-end -->
  ```

**Rationale.** Patterns research: GitHub Issue Forms version via frontmatter; Conventional Commits versions the spec itself; OpenAPI 3.x uses both schema-version + immutable structural markers. The hybrid handles two audiences: Touchstone reads `<!-- pr-body-start -->` regardless of schema version (consumer-stable); cortex journal promotion reads frontmatter and decisions-block (machine-typed). Schema bumps are additive — v2 adds new anchors, never removes v1 ones; consumers warn on `schema-version >= 2` they don't recognize.

### 4. Sentinel → Conductor seam — per-call subprocess (revises 2026-04-24 migration-plan choice)

**Decision:** Sentinel calls Conductor via `conductor call ...` subprocess, matching Touchstone's pattern. Drop the `conductor @ git+...@v0.3.3` git-ref pin in `pyproject.toml`. The in-repo `sentinel-conductor-migration` plan's Slice B (`ConductorAdapter` shim) keeps its shape — it just wraps subprocess instead of `import`.

**Rationale.** Performance research: a Sentinel cycle is 5–50 LLM calls × 1–60s each = minutes-to-hours wall time. Python subprocess startup on macOS is ~100–400ms cold; even at 50 calls × 200ms = ~10s overhead, that's ~2% of an 8-minute minimum cycle. Daemon patterns (LSP, MCP, mypy `dmypy`, `eslint_d`) emerge when calls are at keystroke frequency or per-call work is fast (10–300ms); they don't apply at cycle frequency. Git's subprocess-everywhere model is the gold-standard precedent.

The architectural wins are independent of perf: independent release cadence (no git-ref pin, Conductor releases without bumping Sentinel), process isolation (Conductor crash → Sentinel detects exit code, doesn't propagate exception), language agnosticism (Conductor could rewrite in Rust without breaking Sentinel), Doctrine 0003 compliance, and uniformity with Touchstone.

The 2026-04-24 migration plan reasoned through this and chose import for "we own both sides." The 2026-04-28 reconsideration overrules on the architecture grounds above. Slice B's adapter shape is unchanged; Slice B's *implementation* shifts from `importlib.import_module(...)` to `subprocess.run(['conductor', 'call', ...])` with JSON-mode output parsing. Wave 1 spike confirms the perf estimate on a real cycle before full commit; failure threshold is >5% overhead, in which case daemon mode (`conductor serve` + JSON-RPC over stdin/stdout) is the Wave 2 hedge.

## Per-tool work — tracked as GitHub issues

Implementation work lives in each tool's repo as GitHub issues (or in-repo plans for larger workstreams). This plan does not track per-tool task lists — see the GitHub project board for status. Issue families filed against each repo:

- **`autumngarage/sentinel`** (this repo) — `init` bootstrap; read-side Cortex consumption; rejection-log fold into Cortex Journal; per-task file-state isolation; cycle-artifact schema implementation; trust controls (loop detection, destructive-change gate); scheduled `sentinel work` mode. Plus continuation of the conductor-seam migration in `.cortex/plans/sentinel-conductor-migration.md` (Slice B revised for subprocess seam per cross-cutting decision 4 above). Wave 1 issues: #89 (subprocess migration), #90 (default Doctrine pack).
- **`autumngarage/cortex`** — default-Doctrine seeding flow on `cortex init` (the *mechanism* — Sentinel ships the *content*); audit `cortex grep --frontmatter` filter coverage for Sentinel's queries; coordinate Phase D `cortex journal append` CLI with Sentinel's later workstream. Wave 1 issues: cortex#60 (grep filter audit), cortex#61 (`--seed-from` flag).
- **`autumngarage/conductor`** — audit subprocess CLI surface for Sentinel's per-role needs; regression test asserting `conductor call`'s flag surface and JSON output schema; `consumers.md` documentation under README. Wave 1 issue: conductor#93.
- **`autumngarage/touchstone`** — `open-pr.sh` reads `.sentinel/runs/<latest>.md` PR-body anchors when on a sentinel-authored branch; reviewer cascade includes the cycle journal entry as context when reviewing sentinel-authored diffs.

## Sequencing

**Wave 1 — foundations.** Parallelizable across tools.
- Conductor seam migration completed (subprocess) — sentinel + conductor.
- Default Doctrine baseline written and shipped — sentinel.
- Cortex grep filter audit — cortex.

**Wave 2 — Sentinel becomes project-aware.**
- Read-side Cortex consumption.
- Rejections fold into Cortex Journal.
- Per-task file-state isolation.

**Wave 3 — drop-in experience.**
- Cycle-artifact schema + Touchstone PR-body seam.
- `sentinel init` end-to-end bootstrap.
- Reviewer-side journal awareness.
- Conductor contract regression test.

**Wave 4 — autonomous mode + ship.**
- Trust controls (loops, destructive-change gate).
- Scheduled `sentinel work`.
- Meta-formula `autumngarage/garage/garage`.
- Killer demo on a real OSS repo.

## Out of scope (parked)

- Multi-platform gateway (Slack/Discord/Telegram bidirectional). Hermes's lane.
- Conversational mode (`sentinel chat`). Different product.
- Cross-project Doctrine sharing. Cortex Phase E or later.
- Skill marketplace (agentskills.io adoption). Revisit after Wave 4.
- Cloud execution. Local + auditable is the differentiator.

## Open questions still live

All four cross-cutting questions resolved above. The placement question for this plan (autumn-garage vs sentinel) also resolved 2026-04-28 — sentinel-side per the meta-repo doctrine.
