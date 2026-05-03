---
Status: active
Written: 2026-04-28
Author: claude-code (Henry Modisett)
Goal-hash: sendrop01
Updated-by:
  - 2026-04-28 claude-code (initial plan — product framing + cross-tool delegation structure)
  - 2026-04-28 claude-code (slimmed to cross-cutting only per house rule)
  - 2026-04-28 claude-code (moved here from autumn-garage per stricter rule: even cross-cutting plans live in the primary owner's repo)
  - 2026-04-29 claude-code (comprehensive refactor: stale-against-code claims fixed; differentiator reframed local→deployable; Service Mode + Railway + GitHub-event triggers added as first-class waves; trust-controls extracted to sibling plan)
Cites:
  - autumn-garage/.cortex/doctrine/0001-why-autumn-garage-exists
  - autumn-garage/.cortex/doctrine/0003-llm-providers-compose-by-contract
  - autumn-garage/.cortex/doctrine/0004-conductor-as-fourth-peer
  - autumn-garage/.cortex/doctrine/0006-autumn-garage-is-meta-context
  - .cortex/plans/sentinel-conductor-migration.md (subprocess seam — shipped, validation gates remain)
  - .cortex/plans/sentinel-cortex-t16-integration.md (cycle-end Cortex journal write — shipped)
  - .cortex/plans/sentinel-trust-controls.md (threat model + unattended-mode boundaries)
---

# Sentinel as autonomous engineer — local or deployed

> Position Sentinel as the autonomous engineer for any repo: drop in, sleep, wake up to a queue of small reviewed PRs. Run it on your laptop for active dev, or deploy it on a $5 Railway service that watches GitHub issues and ships PRs while you sleep. Either mode keeps the same file-contract memory layer (Cortex), the same engineering values (seeded Doctrine), the same composability (Conductor + Touchstone). Differentiates from Hermes (conversational generalist, voice, multi-platform) on **focused autonomous coding loop**; from Devin / Codex Cloud (opaque, cloud-only) on **deployable OR local + auditable + composable + bounded**.
>
> **Where this plan lives.** Sentinel's product plan. Per autumn-garage's meta-repo doctrine, even cross-cutting plans live in the primary owner's repo. Cross-cutting decisions are journaled in autumn-garage's `.cortex/journal/`; cross-cutting principles live in autumn-garage doctrine. Per-tool implementation work for the three peer tools is tracked as GitHub issues against their repos.

> **Staleness check — read this first.** Plans go stale; this one's claims must be verified before being acted on. **On load:**
>
> 1. **Tool versions.** Run `touchstone version`, `cortex version`, `sentinel version`, `conductor --version`. Compare to claims here. Brew tap vs source can diverge — version skew has bitten us.
> 2. **Issue status.** Walk the cited GitHub issues. Closed → that slice landed; superseded → updated approach exists; still open → assumptions here still hold for that slice.
> 3. **State files.** Read each tool's `.cortex/state.md` (when present) and recent journal entries. Tool state is ground truth, not this plan.
> 4. **Doctrine.** Doctrine entries numbered after 0006 that touch the cross-cutting decisions below supersede those decisions.
> 5. **Cross-cutting decisions specifically.** Decisions are timestamped 2026-04-28 / 2026-04-29. If you're more than ~3 months past the most recent timestamp and decisions haven't been re-confirmed in the journal, treat as suspect until verified.
> 6. **Code reality.** This plan rewrites earlier "Why now" claims that turned out to be stale against the codebase (the conductor seam migration and Cortex T1.6 write integration were *already shipped* when the prior version listed them as Wave 1 work). Always grep the code before treating a "Why now" bullet as live.
>
> If you find drift, update this plan or supersede the affected section before continuing — don't paper over it.

## Why now (2026-04-29)

Two reframes since the prior version (2026-04-28):

1. **Bar moved.** Hermes ([nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent)) shows what an autonomous agent looks like in 2026: 24/7 reachable, scheduled cron with cross-platform delivery, multi-provider routing, learning loop, run-anywhere deployment (Modal serverless, Daytona, SSH, Docker). Most of those are *generalist* features Sentinel doesn't need — but the deployment shape and event reactivity are non-negotiable for a credible autonomous coding agent. **Sentinel must be deployable and event-driven, not just CLI-loop.**
2. **Cloud is no longer parked.** The 2026-04-28 plan said "Cloud execution. Local + auditable is the differentiator." That framing does not survive contact with "the user wants to deploy this on Railway and have it react to GitHub issues." The differentiator becomes **deployable OR local + auditable + composable + bounded** — single product, two operating modes, same file-contract memory layer underneath.

What hasn't changed:

- The three pillars (Memory / Values / Delegation) and the file-contract composition with Cortex / Touchstone / Conductor are still load-bearing.
- The "drop in, sleep, wake up to small reviewed PRs" promise is the same; cloud mode just makes it true while you're not at your desk.
- Sentinel does not become Hermes. Messaging gateways (Slack/Discord/Telegram), voice, conversational chat, and multi-platform user modeling stay parked. Sentinel is a focused coding loop with a deployment surface, not a personal assistant.

## What's already shipped (verified against HEAD, 2026-04-29)

The prior plan listed these as Wave 1 work; they're in fact done. Validation gates remain (Wave 0–1) but no new implementation is needed for any of:

- **Conductor subprocess seam.** `src/sentinel/providers/` contains `__init__.py`, `conductor_adapter.py`, `interface.py`, `router.py` — no `claude.py` / `openai.py` / `gemini.py` / `local.py`. `pyproject.toml` has no `conductor @ git+...` git-ref pin. Subprocess adapter implements `chat`/`code`/`research`/`detect` via `subprocess.run(['conductor', 'call', ...])` with JSON-mode output parsing. The companion plan `sentinel-conductor-migration.md` covers Slice A–E with current evidence.
- **Cortex T1.6 cycle-end journal write.** `src/sentinel/integrations/cortex.py:572-689` implements the write path; `src/sentinel/cli/work_cmd.py:407-479` calls it; config gate at `src/sentinel/config/schema.py:294-314`; init wizard prompt at `src/sentinel/cli/init_cmd.py:488-508`. Companion plan `sentinel-cortex-t16-integration.md` is shipped.
- **Cortex read-side manifest consumption.** `src/sentinel/integrations/cortex_read.py:52-99` shells out to `cortex manifest --budget N`; `src/sentinel/cli/work_cmd.py:676-678` fetches at cycle start; threaded through scan/execute/review (`work_cmd.py:728`, `:945`, `:1913`). Manifest budget defaults to 6000 tokens, 30s timeout. Policy work (manifest section trust, conflict handling, recency budget) is open — see Wave 2.
- **Default Doctrine pack seeded by `sentinel init`.** `defaults/doctrine/0001..0015` exists; `src/sentinel/cli/init_cmd.py:566-586` seeds when `.cortex/doctrine/` is empty.
- **Trust-control primitives (partial).** Loop guard (`src/sentinel/loop_guard.py:1-22`, 95-150), destructive-change gate (`src/sentinel/gate.py`), per-run / session / daily money + time caps (`work_cmd.py:1277-1325`, 2113-2195), scheduled mode skeleton (`work_cmd.py:2081-2195`). What's missing is a coherent threat model + test matrix + scaling for unattended cloud operation — see `sentinel-trust-controls.md`.
- **Shipped legacy contradiction.** `src/sentinel/loop/cycle.py:79-87` still raises `NotImplementedError` from `_research_phase` while public README claims "core loop shipped." `work_cmd.py` is the de facto loop. Resolution (rename `_legacy_cycle.py` or delete) is Wave 0.

## Differentiator framing — deployable or local

Sentinel is the only autonomous coding agent that:

| Dimension | Sentinel | Hermes | Devin / Codex Cloud |
|---|---|---|---|
| Scope | Focused coding loop (assess→plan→delegate→review) | Generalist personal agent | Coding agent, opaque |
| Where it runs | Local **or** Railway / Modal / Docker / SSH | Local + Modal/Daytona | Vendor cloud only |
| Memory layer | Cortex (file-contract, portable, immutable doctrine) | Hermes-curated SQLite + skills | Vendor-internal |
| Engineering values | Default Doctrine pack, supersedable per project | Skills emerge from cycles (capabilities, not values) | None surfaced |
| Provider | Pluggable via Conductor (any frontier or local model) | Pluggable | Vendor lock-in |
| Auditability | `.sentinel/runs/` + `.cortex/journal/` + Touchstone PR-body anchors | Journal in DB | PR + chat transcript |
| Boundedness | Loop guard + destructive-change gate + budget caps | Skill emergence loop, less explicit bounds | Vendor SLA |
| Reachability surface | GitHub events + HTTP status + journal artifacts | Telegram + Discord + Slack + voice | Web app |
| What it doesn't do | Chat with you. Send Telegram messages. Voice. | Focused, opinionated coding cycles | Run on your hardware |

The durable moat is the **file-contract memory layer plus engineering-values-by-default**. If Hermes adds a Cortex-equivalent and ships values-by-default, the moat narrows; until then, Sentinel is the only agent that ships with project memory + immutable-with-supersede engineering values + an audit trail by default.

The local-or-deployed dimension is **dual-mode, not split-product**: same binary, same config schema, same `.cortex/` and `.sentinel/` artifacts, same Conductor-driven provider routing. Local mode is the dev-loop; deployed mode is the night-shift. Both must work; both share the same trust controls, audit artifacts, and PR-only autonomy boundary.

## Definition of done

A user has two paths, both terminating in small reviewed PRs:

### Local drop-in (existing, hardened)

`brew install autumngarage/garage/garage` then `cd into-any-repo && sentinel init`. Within 90 seconds:

1. All four tools installed and healthy (`sentinel doctor` green, reporting *active* boundaries — not just tool presence).
2. `.cortex/` initialized with default Sentinel Doctrine seeded (when `.cortex/doctrine/` is empty).
3. Existing `CLAUDE.md` / `AGENTS.md` scanned-and-absorbed into Doctrine candidates per cortex flow.
4. `.touchstone-config` written with reviewer cascade defaults.
5. Conductor model resolved (env → keychain → wizard fallback).
6. First-cycle task sources surfaced (open issues with configurable label, `TODO(sentinel)` markers, failing tests, stale dep PRs, lints over threshold).
7. `sentinel work --auto --budget 5` runs unattended and produces:
   - 0–N PRs opened via Touchstone, each with body sourced from the cycle's `.sentinel/runs/<id>.md` PR-body anchors.
   - Each PR linked to its cycle journal entry in `.cortex/journal/`.
   - Rejections journaled to Cortex (single source of truth, no parallel `.sentinel/state/rejections.jsonl`); next cycle's manifest sees them.
   - Destructive changes halted with a `blocked-on-human` journal entry, not silently shipped.
   - Memory usefulness proved: a seeded prior-rejection visibly alters planner output the second cycle.

### Deployed autonomous (new in this revision)

Containerized Sentinel running on Railway, scoped to one or more repos, reacting to GitHub events. Within 5 minutes from the moment a contributor opens a labeled issue:

1. The issue webhook lands at Sentinel's HTTP listener.
2. Sentinel acquires a single-flight lease on that issue (no double-cycling).
3. A scoped cycle runs (assess→plan→delegate→review) with the issue text seeded as the work item.
4. PR opened on a feature branch. Default-branch push is *unreachable* in deployed mode — the autonomy boundary is structural, not configurable.
5. Touchstone's pre-push hook fires; review bots gate the PR.
6. Cycle journal written to `.cortex/journal/`, mirrored to GitHub as a comment on the originating issue.
7. Trust gates respected: destructive-change → block + comment; budget exceeded → graceful halt + journal; loop detected → halt + journal.
8. Health endpoint `/healthz` returns 200; `/status` returns the most recent N cycle summaries.

## Architecture

### Three pillars + service surface (revised)

Three pillars unchanged, plus a service surface that exposes them under a long-lived process.

| Pillar | Owner | Read contract | Write contract |
|---|---|---|---|
| **Memory** | Cortex | `cortex manifest --budget N` (cycle start), `cortex grep` (mid-cycle) | `.cortex/journal/<date>-sentinel-cycle-<id>.md` (T1.6) — shipped |
| **Engineering values** | Cortex (format) + Sentinel (default content) | `.cortex/doctrine/*.md` via the manifest | Doctrine candidates from cycle proposals (rare; via `cortex promote`) |
| **Agent delegation** | Conductor | `conductor list --json` for capability lookup | `conductor call --with <id> --effort <e> --tools <set> --sandbox <mode>` per subagent invocation |

PR-shape integration with Touchstone (Touchstone retains lane ownership):

| Concern | Owner | Contract |
|---|---|---|
| Code review gate | Touchstone | Pre-push hook → `conductor call --tags code-review` |
| PR open + body | Touchstone | `open-pr.sh` reads `.sentinel/runs/<latest>.md` PR-body anchors when on a sentinel-authored branch |

### Service surface (new)

Local-mode Sentinel is unchanged: `sentinel work` is a one-shot CLI. Deployed-mode Sentinel adds a long-lived process subcommand:

```
sentinel serve [--listen 0.0.0.0:8080] [--scope <repo>] [--triggers cron,webhook] [--config /etc/sentinel/serve.toml]
```

`sentinel serve` is a thin orchestration layer over the existing cycle. It:

- Listens on an HTTP port for `/healthz`, `/status`, and `/webhooks/github` (verified via shared secret).
- Maintains a single-flight lease per `(repo, scope-key)` so concurrent triggers don't double-cycle the same work.
- Schedules cron-shaped triggers when configured (e.g., "every 6 hours, scan for stale dep PRs").
- Persists state to a mounted volume: `.cortex/`, `.sentinel/runs/`, `.sentinel/state/leases.json`. No second source of truth.
- Delegates the actual cycle to the same `sentinel work` code path. Cycle = cycle, regardless of trigger.

The only state outside the file-contract layer is the lease file, used to coordinate triggers within a single Sentinel instance. Multi-instance horizontal scaling is out of scope for v0.1 (single-instance is sufficient for the bar).

### Trigger surface

Three trigger sources, prioritized by adoption simplicity:

1. **Cron (default).** `sentinel serve --triggers cron` reads cron-shaped entries from `.sentinel/serve.toml` (e.g., daily-stale-deps, hourly-failing-tests). Already partly shipped in `work_cmd.py:2081-2195`; `serve` wraps it.
2. **GitHub webhook (primary new).** `/webhooks/github` accepts `issues.labeled` (configurable label; default `sentinel:work`), `pull_request.opened` (for review-only flows), and a small allow-list of other event types. HMAC verification using the GitHub webhook secret. Out-of-allow-list events ignored with a 200 (don't fail webhooks GitHub will retry).
3. **HTTP API (manual escape hatch).** `POST /trigger` with auth header → run a one-shot cycle. Used for ops debugging and CI integration. Auth via shared secret; rate-limited.

Polling is **not** the primary path. A poll fallback (`sentinel serve --triggers poll-issues --interval 5m`) exists for users without webhook reachability (private GH instances, network-restricted), but webhooks are the documented happy path. Rationale: webhook-driven gives <30s latency and zero idle cost; polling burns API quota and creates ambiguity about which event triggered which cycle.

### Autonomy boundary (load-bearing)

In deployed mode, Sentinel is **structurally** restricted from default-branch writes:

- `git push` to default branch fails by configuration (`git config --add receive.denyCurrentBranch refuse` on the local clone is insufficient; the GitHub PAT scope must omit `contents:write` on default branch via fine-grained token + `pull-requests:write` only).
- Sentinel only opens PRs; review bots merge.
- The autonomy boundary is documented in `sentinel-trust-controls.md` and asserted by a startup self-check (`sentinel serve --doctor` exits non-zero if the PAT can push to default branch).

Local mode keeps the existing model (the user is at the keyboard; the Coder writes to the working tree; pre-push hooks gate). The structural restriction is deploy-mode-specific.

## Cross-cutting decisions

### 1. Default Doctrine pack — ships in this repo, seeded by `sentinel init`

(2026-04-28; status: shipped — `defaults/doctrine/0001..0015`, `init_cmd.py:566-586`.) Sentinel ships engineering-values Doctrine in `defaults/doctrine/`. `sentinel init` seeds when `.cortex/doctrine/` is empty or absent, with sequential numbering (0001–0015) and a `Sentinel-baseline: true` frontmatter flag. Projects supersede via Cortex's immutable-with-supersede mechanism.

Open follow-up (Wave 2): `sentinel audit --doctrine` cross-checks shipped baseline against local entries — surfaces drift, never auto-rewrites.

### 2. Brew packaging — two taps: à la carte + meta-formula

(2026-04-28.) `autumngarage/tools/` tap = four independent formulas; `autumngarage/garage/` tap = `garage` meta-formula depending on all four. `brew install autumngarage/garage/garage` is the one-command full-ecosystem install for the killer demo.

### 3. Cycle artifact schema — frontmatter version + stable HTML anchors

(2026-04-28; status: design-locked, implementation pending Wave 1 PR-body seam.) `.sentinel/runs/<id>.md` carries:

- **Frontmatter** (machine-readable, evolves by major version): `schema-version`, `sentinel-run-id`, `timestamp`, `cycle-id`, `branch`, `status`.
- **Body anchors** (immutable across schema versions): `<!-- pr-body-start -->...<!-- pr-body-end -->`, `<!-- decisions-start -->...<!-- decisions-end -->`, `<!-- transcript-start -->...<!-- transcript-end -->`.

Touchstone's `open-pr.sh` consumes `pr-body` anchors regardless of schema version; cortex journal promotion reads frontmatter + decisions block.

### 4. Sentinel → Conductor seam — per-call subprocess

(2026-04-28; status: shipped — `conductor_adapter.py`, no native providers, no git-ref pin.) Sentinel calls Conductor via `conductor call ...` subprocess. Independent release cadence, process isolation, Doctrine 0003 compliance, Touchstone uniformity. ~2% wall-time overhead at cycle frequency (verified by Slice B spike).

### 5. Service-mode shape — `sentinel serve` long-lived process (NEW, 2026-04-29)

(Status: design.) Local mode is `sentinel work` (one-shot CLI, unchanged). Deployed mode is `sentinel serve` (long-lived process) — a thin orchestration layer over the same cycle code path. Lease coordination via `.sentinel/state/leases.json`. State persists to a mounted volume; no in-memory state survives restart that affects correctness. Single-instance only for v0.1; horizontal scaling deferred.

**Rationale.** Reusing the cycle code path means there is exactly one autonomous-cycle code path; service mode only adds the trigger plumbing. Avoids the alternative ("`serve` is a different agent shape") which would create two products to maintain.

**Failure modes addressed.** Crash mid-cycle: lease has TTL; expires after `cycle_max_minutes * 2`. Webhook flood: rate-limit at the listener; ignore events without a configured trigger. Lease lock contention with humans: in-process only; humans operate via `sentinel work` in a different repo checkout.

### 6. Deployment target — Railway primary, alternates documented (NEW, 2026-04-29)

(Status: design.) Primary deployment target is Railway:

- Railway is already paid-for in the user's environment (existing vanguard / outrider services).
- Persistent volumes for `.cortex/` and `.sentinel/` survive redeploys.
- Inbound HTTPS for `/webhooks/github` is one-click.
- Always-on pricing is acceptable at the cycle frequency Sentinel runs (vs Modal hibernation savings being load-bearing).

Alternates documented but not packaged: Modal serverless (cold-start latency irrelevant for cycle-frequency work), Docker Compose (self-host), Daytona (dev-loop), SSH-to-VPS (the original "$5 VPS" shape). All share the same containerized image and config surface.

**Rationale.** One blessed path with copy-paste deploy beats five half-supported paths. Railway is documented end-to-end; alternates are listed in `docs/deployment/` with "tested at one point, not actively maintained" labels.

### 7. Event source — GitHub webhook primary, poll fallback (NEW, 2026-04-29)

(Status: design.) Primary event source: GitHub `issues.labeled` webhook with HMAC verification. Fallback: poll mode for environments without webhook reachability. Other event types deferred (PR comments, issue comments, push events) — start small, add when there's a concrete user pull.

**Allow-list** (initial): `issues.labeled` (label match required), `pull_request.opened` (review-only path, no Coder dispatch). Everything else returns 200 + ignored.

### 8. Autonomy boundary — PR-only, structurally enforced (NEW, 2026-04-29)

(Status: design.) Deployed Sentinel never pushes to a default branch. Enforced via:

- Fine-grained GitHub PAT scoped to specific repos with `pull-requests:write` + `contents:write` (needed for branch creation) but **branch protection** on the default branch requires a passing review-bot status.
- `sentinel serve --doctor` self-checks at startup; exits non-zero if PAT scope is too broad.
- Tests assert: a unit-tested attempt to push to `main`/`master` is rejected at the gate, journaled, and the cycle exits.

This is the load-bearing trust control for the deployed mode. See `sentinel-trust-controls.md` for the full threat model.

## Wave plan

Six waves. Wave 0–2 are mostly cleanup + hardening of what's shipped; Wave 3–5 are the new deployed-mode workstream.

### Wave 0 — Plan/state cleanup + foundation verification

**Why first:** the prior plan's "Why now" listed shipped work as TODO; following it sends agents to redo done work. Until the plan tells the truth about the code, every later wave starts on shifting ground.

- Retire / supersede / mark shipped on the older plans (`plans/conductor-migration.md`, `plans/production-ready.md`, `plans/dogfood-2026-04-17.md`); cross-link `sentinel-codex-identifier-rename.md`.
- Convert `sentinel-conductor-migration.md` Slices B–E from "future work" to "shipped + remaining validation gates."
- Mark `sentinel-cortex-t16-integration.md` as shipped; replace unchecked items with code pointers; delete autumn-garage meta section.
- Resolve `src/sentinel/loop/cycle.py` legacy contradiction: rename to `_legacy_cycle.py` or delete; update README "core loop shipped" claim to point at `work_cmd.py`.
- Collapse `plans/` → `.cortex/plans/` (move historical plans to `.cortex/plans/historical/`; leave a one-cycle `plans/README.md` redirect).

**Acceptance:** `cortex doctor` clean; only one active plan owns each workstream; no public doc claims a feature shipped that the code doesn't ship.

### Wave 1 — Trust controls + drop-in PR experience

**Why second:** unattended cloud operation makes trust controls load-bearing. Wave 1 elevates them from scattered primitives to a coherent, threat-modeled, test-asserted layer. Drop-in PR polish ships alongside because the deployed-mode killer demo (Wave 5) needs both.

Trust controls split out to a sibling plan (`sentinel-trust-controls.md`) — see that plan for threat model, controls inventory, test matrix, audit-weak-points coverage.

Drop-in PR scope here:

- Cycle artifact PR-body anchors → Touchstone `open-pr.sh` consumer (cross-tool seam — file as touchstone issue).
- `sentinel doctor` reports *active* boundaries, not just tool presence (active = "loop guard armed, gate armed, autonomy boundary verified").
- 90s drop-in checkpoint: cold install → `sentinel doctor` green ≤90s.
- Conductor contract-drift test: CI fixture asserts `conductor call --json` output schema (provider list, capabilities, JSON shape) so Sentinel fails predictably if Conductor drifts.

**Acceptance:** all gates from `sentinel-trust-controls.md` Wave 1 milestone pass; clean macOS → 90s drop-in works.

### Wave 2 — Project-aware behavior + memory usefulness

**Why third:** the differentiator (project memory + values by default) is unproven without the memory-usefulness gate. This wave makes the differentiator demonstrable.

- **Rejection memory: Cortex-only source of truth.** Migrate `.sentinel/state/rejections.jsonl` to `.cortex/journal/` rejection entries. Rebuild ephemeral fast-index from journal at cycle start. Single source of truth.
- **Cortex manifest read policy.** Define manifest budget defaults, conflict handling for contradictory journal entries, recency window, doctrine immutable-with-supersede traversal. Block on parse failure when `.cortex/` exists; `--no-memory` is the only escape hatch (silent amnesia = differentiator violation).
- **`cortex retrieve` consumption (semantic memory at scale).** Cortex ships `cortex retrieve` — opt-in semantic search over `.cortex/` (see `autumngarage/cortex/.cortex/plans/cortex-retrieve.md`). Sentinel consumes via `cortex retrieve --json --top-k N --filter Type=...` mid-cycle, replacing manifest-stuffing for Planner / Reviewer roles. **Sentinel does not own the index.** No `src/sentinel/index/` module — the index lives in Cortex (`.cortex/.index/`, gitignored, derived). Sentinel calls `cortex retrieve` and receives top-K excerpts. Consumed only when Cortex's retrieve is actually shipped (gates on Cortex's S2 or later); fall-through is grep semantics, no behavior change.
- **Reviewer-side journal awareness.** Reviewer gets manifest including prior rejections + semantically-relevant journal entries via `cortex retrieve`. Independence rule asserted: reviewer provider differs from coder provider when alternative exists.
- **Per-task file-state isolation.** Define task-ID-scoped namespace for generated files; collision behavior; cleanup path. Reference `runtime/file_state.py`.
- **Memory-usefulness gate.** Seeded prior rejection demonstrably alters planner output on the second cycle. This is the proof point for the differentiator. With `cortex retrieve` shipped, the gate also tests that semantically-similar (not just keyword-matched) prior rejections are surfaced.
- **Default Doctrine lifecycle.** `sentinel audit --doctrine` cross-checks shipped baseline against local entries; surfaces drift; never auto-rewrites.

**Acceptance:** memory-usefulness gate passes on a synthetic test repo; rejection memory has one source of truth; if Cortex `retrieve` shipped by Wave 2 entry, Planner/Reviewer use it; otherwise grep fallback verified.

### Wave 3 — Service mode + Railway deployment (NEW)

**Why fourth:** local-mode hardening (Waves 0–2) is the foundation. Service mode adds the trigger plumbing on top of the same cycle code path.

- **`sentinel serve` subcommand.** Long-lived process; HTTP listener for `/healthz`, `/status`, `/webhooks/github`, `/trigger`; cron scheduler; lease coordination. Reuse cycle code path — no second cycle implementation.
- **Containerized image.** Dockerfile, multi-stage build, non-root user, slim runtime image. `garage` meta-formula source-of-truth; `sentinel` Python package as build input.
- **Railway deploy guide.** End-to-end: project setup, persistent volume mount for `.cortex/` + `.sentinel/`, secret env vars (GITHUB_PAT, CONDUCTOR_*), webhook URL exposure, scaling notes (single-instance for v0.1).
- **Startup self-check (`sentinel serve --doctor`).** Verifies PAT scope (no default-branch push), Conductor reachable, `.cortex/` writable, lease directory present.
- **Documented alternates.** `docs/deployment/{modal,docker-compose,ssh,daytona}.md` — tested-at-one-point labels.

**Acceptance:** Railway deploy doc walks a new user through deploy → first cron-triggered cycle in <30 minutes.

### Wave 4 — Event-driven triggers (NEW)

**Why fifth:** event-driven is the killer-demo bar. Cron alone is half the story; reactivity to GitHub issues is what makes "deploy and forget" feel autonomous.

- **`/webhooks/github` listener.** HMAC verification, allow-list `issues.labeled` + `pull_request.opened`, ignore-with-200 for everything else.
- **Issue → work item translator.** Issue body becomes the seeded work item; lens scan still runs (for context) but the planner takes the issue as authoritative scope. Configurable label (default `sentinel:work`).
- **Single-flight lease per `(repo, issue-number)`.** No double-cycling. Lease TTL = `cycle_max_minutes * 2`.
- **Issue-comment mirror.** Cycle outcome (PR link, blocked-on-human reason, etc.) posted as a comment on the originating issue. Body sourced from cycle artifact.
- **Poll fallback.** `--triggers poll-issues --interval 5m` for env without webhook reachability. Documented as fallback, not happy path.
- **Loop / runaway protection at the trigger layer.** Same issue triggering 3+ cycles in `cycle_max_minutes * 5` → halt + journal + comment.

**Acceptance:** open a labeled issue → PR within 5 minutes (assuming cycle completes in budget); issue gets a status comment.

### Wave 5 — Killer demo + v0.1 ship

**Why last:** demo is the public proof point; ship gate signals are downstream of demo success. Demo target chosen now to drive the rest of v0.1 acceptance.

- **Demo target.** Public OSS repo, small-to-medium, real bug with good tests, harmless impact. Not autumn-mail (self-dogfood is dismissible). Not Sentinel itself (CI is the right home for self-dogfood, not the demo).
- **Demo script.** Fresh Mac → `brew install autumngarage/garage/garage` → `sentinel init` (or skip; clone-config) → run `sentinel work` once locally to prove drop-in, then deploy to Railway → open a labeled issue → PR ships → issue comment posts.
- **v0.1 acceptance gates.** See "Acceptance gates" section below.
- **Release cut.** sentinel v0.4.0 (`hatch-vcs`-derived); brew tap bumped via the shared workflow; Cortex / Touchstone / Conductor versions documented.

**Acceptance:** demo runs end-to-end on a clean machine in under 30 minutes; v0.1 acceptance gates all pass.

## Acceptance gates (by wave)

1. **Plan consistency gate (W0).** Only one active plan owns each workstream; no doc claims a feature shipped that the code doesn't ship.
2. **Conductor contract-drift gate (W1).** CI test asserts `conductor call --json` output schema; fails predictably on drift.
3. **90-second drop-in gate (W1).** Clean macOS install → `sentinel doctor` green in ≤90s, reporting active boundaries.
4. **Destructive-change gate (W1).** Large deletion / migration file / secret-shaped addition trips block before push, leaves branch intact, journals reasoning.
5. **Loop / runaway gate (W1).** Same item attempted N+1 times trips loop guard, journals, halts.
6. **Memory-usefulness gate (W2).** Seeded prior-rejection visibly alters planner output the second cycle. The differentiator proof.
7. **Self-cycle gate (W2).** `sentinel work --auto --budget '$5'` on the Sentinel repo three times, all green, all journaled.
8. **Foreign-repo gate (W2).** Two repos (one Python, one non-Python) — small PR or explicit blocked/rejected artifact with next action.
9. **Service-mode startup gate (W3).** `sentinel serve --doctor` rejects an over-broad PAT; accepts a correctly-scoped one; reports active boundaries.
10. **Railway deploy gate (W3).** Doc-driven deploy → first cron cycle in <30 min on a fresh project.
11. **Event-trigger gate (W4).** Labeled issue → PR within 5 min on the demo repo.
12. **Issue-comment gate (W4).** Cycle outcome mirrored to issue comment with budget, PR link or blocked reason, journal reference.
13. **Autonomy-boundary gate (W4).** Unit test: attempt to push to default branch is rejected at gate, journaled, cycle exits with `blocked-on-autonomy-boundary`.
14. **Killer-demo gate (W5).** Public OSS repo, fresh Mac, doc-driven, ≤30 min end-to-end.
15. **Compatibility gate (W5).** Existing `.sentinel/config.toml` with stable provider names continues to parse and route correctly.

## Per-tool work — tracked as GitHub issues

This plan does not track per-tool task lists. Issue families filed against each repo:

- **`autumngarage/sentinel`** — all Wave 0–5 work. Companion plans: `sentinel-trust-controls.md` (threat model + W1 controls), `sentinel-conductor-migration.md` (validation gates only at this point), `sentinel-cortex-t16-integration.md` (shipped follow-ups).
- **`autumngarage/cortex`** — Phase D `cortex journal append` CLI (so Sentinel can stop embedding the cycle template literal). `cortex grep --frontmatter` filter coverage audit. Default-Doctrine seeding flow (`cortex init --seed-from`). **`cortex retrieve` semantic-memory layer** per `cortex/.cortex/plans/cortex-retrieve.md` (council-reviewed design); supersedes Cortex Doctrine 0005 #1's "not a vector store" framing — Sentinel consumes once shipped.
- **`autumngarage/conductor`** — `consumers.md` doc; contract-test fixture for `conductor call --json` output schema; capability-axis stability (Sentinel depends on `--effort`, `--tools`, `--sandbox`, `--exec`, `--max-turns`, `--timeout-sec`).
- **`autumngarage/touchstone`** — `open-pr.sh` reads `.sentinel/runs/<latest>.md` PR-body anchors when on a sentinel-authored branch; reviewer cascade includes the cycle journal entry as context when reviewing sentinel-authored diffs.

## Out of scope (explicitly parked)

- **Multi-platform messaging gateway** (Slack / Discord / Telegram / WhatsApp). Hermes's lane. If reachability matters beyond GitHub events + HTTP `/status`, a thin Telegram bot wrapper that calls Sentinel's HTTP API is a future workstream — not in v0.1.
- **Voice memo transcription, multimodal input.** Hermes's lane.
- **Conversational mode (`sentinel chat`).** Different product shape (general agent vs. focused loop); revisit only if there's a concrete user pull.
- **Cross-project Doctrine sharing across multiple repos served by one instance.** v0.1 is single-instance, single-or-few-repo. Cortex Phase E or later.
- **Skill marketplace (agentskills.io adoption).** Revisit after Wave 5. Skills-from-cycles is interesting in principle (the positive analog to rejection memory) but unproven; not on critical path.
- **Horizontal scaling (multi-instance Sentinel).** Single-instance is sufficient for the bar; lease coordination would have to grow into a real distributed lock, and there's no demand pull.
- **Self-hosted GitHub Enterprise** beyond what the GitHub API surface naturally supports. Webhook + PAT semantics are GH-Cloud-tested first.

## Open risks

1. **Local LLM hardware reality.** "Local + bounded" weakens if a 16–36GB Mac can't reliably run the loop end-to-end. Local-LLM dogfood (autumn-garage `2026-04-24-local-llm-dogfood.md`) found qwen2.5-coder silently fails tool-calls; qwen3.6 MoE works but is slow. Mitigation: explicit RAM-tier guidance in `sentinel init` (16GB → cloud-only; 36GB → slow local viable; 64GB+ → local viable), no silent default to a failing local model, force `--local-model` selection.

2. **Derived-state drift.** If a human hand-edits `.cortex/journal/` or `.sentinel/state/`, Sentinel has no daemon to detect it and invalidate caches. Mitigation: hash + timestamp checks at cycle start; warn-on-divergence; treat hand-edited files as authoritative (don't overwrite).

3. **Conductor CLI flag/JSON drift.** Sentinel depends on `conductor call --effort`, `--tools`, `--sandbox`, `--exec`, `--max-turns`, `--timeout-sec` and the JSON output schema. A Conductor refactor could break Sentinel silently. Mitigation: contract-test fixture in CI (Wave 1).

4. **Webhook reachability and replay.** Railway provides public HTTPS, but mis-configured GH webhook secrets, replayed events (network retries), or out-of-order events can cause double-cycles or missed cycles. Mitigation: HMAC verification, idempotency on `(repo, issue-number, event-id)`, lease coordination.

5. **Autonomy-boundary bypass.** A misconfigured PAT with `contents:write` on default branch defeats the structural restriction. Mitigation: `sentinel serve --doctor` startup check; documented as the load-bearing safety control.

6. **Hermes-parity moving target.** Hermes will keep shipping. If Hermes adds a Cortex-equivalent and ships engineering values by default, the differentiator narrows. Mitigation: invest in the file-contract memory layer's portability and the memory-usefulness proof — those are durable; capability sprawl is not.

7. **Cost spiral on always-on cloud.** Railway always-on pricing + Conductor frontier-model calls per cycle could surprise. Mitigation: per-day / per-week / per-issue money caps enforced at the lease layer; cycle halts with `blocked-on-budget`; daily report mirrors to issue comment.

## Open questions still live

- **Should v0.1 ship a `garage` Helm chart / Kubernetes manifest** alongside the Railway template? Speculative pull from K8s shops; no concrete user yet.
- **Should `sentinel serve` expose a /metrics Prometheus endpoint** in v0.1? Useful for "is it actually working" observability; cheap to add. Lean toward yes.
- **Default cycle budget in deployed mode** — money cap `$1` per issue? Time cap 30 min? Configurable but needs a sensible default.
- **GitHub App vs PAT.** PAT is simpler; GitHub App is the production-correct shape (per-installation auth, finer scopes, better rate limits). v0.1 ships PAT-supported; v0.2 adds App.
