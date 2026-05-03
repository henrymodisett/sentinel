---
Status: active
Written: 2026-04-29
Author: claude-code (Henry Modisett)
Goal-hash: senTrust01
Updated-by:
  - 2026-04-29 claude-code (initial; extracted from sentinel-autonomous-engineer.md as scope grew with deployed-mode + event-driven autonomy)
Cites:
  - .cortex/plans/sentinel-autonomous-engineer.md (parent product plan; Wave 1 trust-controls workstream)
  - principles/engineering-principles.md (no silent failures, recoverability, derive-don't-persist)
  - principles/audit-weak-points.md (audit one weak-point class at a time)
---

# Sentinel trust controls — threat model + unattended-mode boundaries

> Sentinel running unattended on Railway with PR-write access to public-or-customer repos has a meaningfully different blast radius than Sentinel running on a developer's laptop. This plan crystallizes the threat model, inventories the controls (shipped vs partial vs missing), specifies the test matrix, and applies the `audit-weak-points` methodology so every control is paired with a guardrail that catches the next instance of the same class. Owns Wave 1 of the parent product plan.

## Why this is its own plan

The parent product plan (`sentinel-autonomous-engineer.md`) listed trust controls as a Wave 1 line item alongside drop-in PR polish. Once cloud deployment + GitHub-event reactivity went un-parked (2026-04-29), the trust surface grew enough that scattering the threat model across Wave 1/2/4 sections of the master plan would violate documentation ownership (`principles/documentation-ownership.md`). One canonical owner per volatile fact: this plan owns the threat model.

## Operating modes — different blast radii

| Mode | Operator presence | Write surface | Failure visibility | Threat amplification |
|---|---|---|---|---|
| **Local dev (`sentinel work`)** | Human at keyboard | Working tree of one repo | Terminal + journal | Low — human reviews each cycle |
| **Local scheduled (`sentinel work --every 1h`)** | Operator nearby; logs visible | Working tree | Terminal + journal | Low–moderate — operator notices anomalies within hours |
| **Deployed (`sentinel serve`)** | None for hours/days | PRs across N repos via PAT/App | HTTP `/status` + cycle journal + issue comment | High — anomaly window measured in days; cumulative blast |

Trust controls must be sized for the deployed mode. Local-mode doesn't relax them; deployed-mode raises the bar.

## Threat model

Six classes. For each: the threat, the current controls (with code pointers and shipped status), the gap, and the wave that closes it.

### T1. Runaway spend / time loop

**Threat.** Planner regenerates the same rejected item across cycles; loop-mode keeps running; local model loops slowly; budget cap not honored at trigger boundary.

**Shipped controls.**
- Loop fingerprint ring buffer (`src/sentinel/loop_guard.py:1-22`, 95-150). Detects identical work item proposals across cycles.
- Coder ↔ reviewer max iterations + no-progress halt (`work_cmd.py:1639-1651`, 1673-1681). Stops when two reviewer rounds produce identical findings.
- Per-run / session / daily money + time caps (`work_cmd.py:1277-1325`, 2113-2195).
- `sentinel work --plan-only` halts before Coder execution; budget halts emit `Status: blocked-on-budget` to journal.

**Gaps.**
- Loop guard is per-process. Deployed mode across N issues can loop on issue-N while issue-M loops on issue-M. Need cross-issue cycle accounting.
- Money caps are configurable but no out-of-the-box safe default for deployed mode (must not default to "unlimited").
- Loop fingerprint hashes are not journaled — loop hits aren't queryable from `cortex grep`.

**Closes:** Wave 1.
- Default deployed-mode budget: `$1/issue`, `$10/day`, `$50/week`. Override via env / config.
- Lease layer enforces session-spend before invoking cycle code path.
- Loop hits journaled to Cortex with `Trigger: loop-guard-fired` so they show up in next cycle's manifest.
- Test: seeded repeating failing item trips loop guard within 3 cycles; never burns full daily budget.

### T2. Destructive repo changes

**Threat.** Coder deletes a large code surface, commits a migration that drops production data, leaks a secret, or otherwise ships an irreversible change before human review.

**Shipped controls.**
- Diff inspection for migration files (`src/sentinel/gate.py:1-25`, 45-70). Pattern match on `migrations/`, `*.sql` schema-change shapes, etc.
- Deletion threshold (`gate.py:127-153`). Configurable; default 100 lines.
- Secret-shaped diff detection (`gate.py:178-216`, secret regex match before commit).
- Block before push, leave branch intact (`work_cmd.py:1984-2003`).

**Gaps.**
- No autonomy boundary: a misconfigured PAT with `contents:write` on default branch defeats the gate (the gate runs on local diff; the push happens regardless if remote allows it).
- Secret detection regex is conservative; gitleaks integration would be stronger. Touchstone has a gitleaks rule (`autumn-garage/.cortex/doctrine/0005-db-uri-gitleaks-rule.md`) — Sentinel should mirror.
- Recovery path not journaled — if the gate fires, the branch survives but the next operator doesn't see the recovery instructions in `/status`.

**Closes:** Wave 1 + Wave 4.
- (W1) Mirror Touchstone's gitleaks rules in Sentinel's gate.
- (W1) Gate-fired events get a structured journal entry with `recovery-steps` block (where the branch is, what was rejected, how to manually push if intentional).
- (W4) `sentinel serve --doctor` startup check: PAT scope must NOT include `contents:write` on the default branch. Test: misconfigured PAT → startup exits non-zero with clear error.
- (W4) Issue comment mirrors the recovery-steps block when the gate fires.

### T3. Silent dependency failure

**Threat.** Cortex / Conductor / Touchstone missing or broken at runtime; Sentinel degrades silently into a less-safe mode without operator visibility. Engineering principle violation: "every exception is either re-raised or logged with enough context to debug from production logs alone" (`engineering-principles.md:17-21`).

**Shipped controls.**
- Cortex manifest miss → one-time warning to stderr (`cortex_read.py:60-99`).
- Conductor missing at adapter init → clear error (`conductor_adapter.py:172-179`).
- `detect_all` reports not-installed status (`router.py:427-457`).

**Gaps.**
- Cortex manifest *parse failure* (file present but unparseable) currently warns and continues. Master plan cross-cutting decision (open question §3 → council answer): block with `--no-memory` escape hatch when `.cortex/` exists. Silent amnesia violates the differentiator.
- `sentinel doctor` reports presence, not active boundaries. Need: "loop guard armed: yes / gate armed: yes / autonomy boundary verified: yes" instead of just "tool installed: yes."
- No periodic re-check in service mode. If Conductor binary is upgraded mid-deploy and breaks, Sentinel finds out at the next cycle, not at the upgrade.

**Closes:** Wave 1 + Wave 3.
- (W1) `sentinel doctor` reports active boundaries; CI test asserts the output shape.
- (W1) Cortex manifest parse failure → `blocked-on-memory-error` journal entry; cycle exits unless `--no-memory`.
- (W3) `sentinel serve` runs `sentinel doctor` at startup and on `SIGUSR1`; `/healthz` returns 503 if any boundary fails.

### T4. Reviewer self-confirmation

**Threat.** Coder and Reviewer use the same provider (or near-identical prompt context), share blind spots, approve flawed work because they reasoned the same way the first time.

**Shipped controls.**
- README states reviewer must differ from coder (`README.md:40`).
- `sentinel init` warns when coder == reviewer (`init_cmd.py:510-526`).
- Router excludes coder provider for review intent when alternative exists (`router.py:331-355`).

**Gaps.**
- No assertion at *cycle time*. If config drift (or upgrade path) leaves coder == reviewer, the warning fires once at init and is forgotten.
- No fallback semantics when coder == reviewer is unavoidable (single-provider deployment): currently router falls through silently.

**Closes:** Wave 2.
- (W2) Per-cycle assertion: log structured event when coder provider == reviewer provider; emit warning in cycle journal; configurable to error.
- (W2) Documented fallback policy: when coder == reviewer is unavoidable, run review under `--effort high` + use a different model from the same provider; journal the degradation explicitly.
- Test: synthetic config with coder == reviewer → cycle journal contains `degraded-review-independence: true` and a recommendation.

### T5. Stale or poisoned memory

**Threat.** Cortex manifest injects outdated, contradictory, or hand-edited (possibly malicious) doctrine / journal context into every role; Sentinel acts on stale assumptions.

**Shipped controls.**
- Cortex Doctrine is immutable-with-supersede (Cortex Protocol §4.2).
- `fetch_manifest` shells out to `cortex manifest --budget N` (`cortex_read.py:52-99`); doesn't merge in-process.

**Gaps.**
- No trust ranking on manifest sections. A human-edited journal entry from yesterday weighs the same as a machine-written sentinel-cycle entry from a week ago.
- No conflict-handling spec when journal entries contradict (e.g., one entry says "X is broken, blocked," another says "X is fixed, shipped").
- No recency budget. Manifest pulls last 72h by default; deployed mode running months may want a different recency window.
- Doctrine traversal doesn't honor `supersedes:` chains explicitly — relies on Cortex's manifest layer.

**Closes:** Wave 2.
- (W2) Define manifest section trust ranking: Doctrine (highest, immutable) > sentinel-cycle journal entries > human journal entries (newest wins on conflict). Document in this plan + cortex docs.
- (W2) Conflict resolution: if two journal entries on the same `Goal-hash` contradict, surface both to the role with `[CONFLICT]` markers; let the role decide.
- (W2) Recency window configurable per role: monitor=72h (current), planner=14d, reviewer=30d.
- (W2) Test: manifest with contradictory entries surfaces conflict markers; doesn't silently pick one.

### T6. Derived-state drift

**Threat.** Persisted lenses, rejections, file-state, and run artifacts diverge from source of truth. Engineering principle: "compute from the source of truth by default. Persist derived state only when … document … the source of truth, the invalidation trigger, the rebuild path, and a reconciliation check" (`engineering-principles.md:14-15`).

**Shipped controls.**
- Lenses cached at `.sentinel/lenses.md`; delete to regenerate (README:25).
- Domain brief cached with hash-invalidation (cycle.py docstring).

**Gaps.**
- No derived-state inventory. A human reading the codebase doesn't know what's persistent-derived vs source-of-truth.
- No reconciliation check. If `.sentinel/state/rejections.jsonl` (legacy) and `.cortex/journal/` (planned source of truth) disagree post-Wave-2 migration, no daemon detects it.
- No invalidation trigger spec. If a human edits `.cortex/journal/`, lens cache should invalidate; currently doesn't.

**Closes:** Wave 2.
- (W2) Inventory all derived state in `docs/derived-state.md`. For each: source of truth, invalidation trigger, rebuild path, reconciliation.
- (W2) Hash + timestamp checks at cycle start; warn on divergence; treat hand-edited files as authoritative.
- (W2) Rejection memory migration (parent plan W2 item) is the first reconciliation: rebuild ephemeral fast-index from journal at cycle start.
- Test: hand-edit a journal entry → next cycle warns "external edit detected; treating as source of truth."

## Audit-weak-points coverage

Per `principles/audit-weak-points.md`, every fix in this plan must:

1. **Identify the pattern.** Each threat class above names the pattern.
2. **Search the surface.** Grep + AST tools to find sibling instances. Document scope: searched directories, queries, what's intentionally out of scope.
3. **Rank by impact.** Production blast > test-only.
4. **Fix in tiers.** Land guardrail in first PR; track tail in issues.
5. **Reset contaminated state.** Rejection memory migration (T6) requires rebuilding the ephemeral fast-index; old `.sentinel/state/rejections.jsonl` is contaminated and must not be merged in.
6. **Add a guardrail.** Each threat closes with a CI test or runtime invariant that catches the next instance.

The audit-weak-points methodology is treated as load-bearing for this plan: every control here ships with its guardrail in the same PR, not deferred.

## Test matrix

| Threat | Wave | Unit test | Integration test | E2E (deployed mode) |
|---|---|---|---|---|
| T1 runaway loop | W1 | loop-guard fingerprint dedup | seeded-loop-on-Sentinel-repo trips guard in ≤3 cycles | Railway deploy + repeated failing issue → daily budget never exceeded |
| T2 destructive | W1 / W4 | gate.py rules per pattern | gate fires on synthetic large-deletion / migration / secret diff | misconfigured PAT → `serve --doctor` exits non-zero |
| T3 silent dep | W1 / W3 | doctor active-boundaries shape | manifest parse failure → blocked-on-memory-error | conductor binary missing at runtime → /healthz 503 |
| T4 reviewer self-confirm | W2 | router excludes coder provider | coder==reviewer config → cycle journals degradation | (covered by W2 unit/integration; no E2E unique surface) |
| T5 stale memory | W2 | manifest section trust ranking | contradictory entries surface conflict markers | (covered by W2 unit/integration) |
| T6 derived-state drift | W2 | hash divergence detection | hand-edited journal → next-cycle warning | (covered by W2 unit/integration) |

## Operational posture for deployed mode

The deployed-mode bar:

- **Default budgets cannot be unset.** Even if config omits caps, hard-coded `$1/issue`, `$10/day`, `$50/week` floor applies. Operator can raise by config; cannot remove.
- **Default safety controls cannot be disabled by config.** `--no-loop-guard`, `--no-destructive-gate` are not accepted in `sentinel serve` mode (only in `sentinel work` interactive). Service-mode is strict; CLI-mode is configurable.
- **Startup self-check is mandatory.** `sentinel serve` runs `sentinel doctor` before listening on the port. Fails closed: any boundary unverified → exit non-zero, do not start.
- **All gate fires journal AND mirror to issue.** Operator visibility is the bedrock of "no silent failures." Mirroring to the GitHub issue is the visibility-amplification step required by deployed mode.
- **Audit log is append-only.** `.cortex/journal/` is the audit log. Sentinel must never overwrite or rewrite an entry. Cortex Protocol enforces this; Sentinel respects it.

## Out of scope (parked)

- **Behavioral anomaly detection** (e.g., "this issue's cycle behaves unusually compared to historical"). Useful, but speculative; needs data that v0.1 won't have collected.
- **Multi-tenant trust boundaries** (one Sentinel instance serving multiple unrelated organizations). Single-instance / single-org for v0.1; multi-tenant is a v0.2+ workstream with its own threat model.
- **Audit log signing / tamper-evidence.** `.cortex/journal/` is git-tracked, which gives append-only-via-commit semantics in practice. Cryptographic signing is overkill for v0.1.
- **Adversarial input testing** (malicious issue body designed to jailbreak the planner / coder). Important if Sentinel is exposed to untrusted issuers; defer to v0.2 with a dedicated threat model.

## Open questions

1. **`--no-memory` escape hatch behavior.** When invoked, does Sentinel run a degraded cycle (lens scan only, no manifest) or refuse? Current proposal: degraded cycle with `Status: degraded-no-memory` journal entry. Council's recommendation aligned.
2. **Default-branch identifier portability.** "Don't push to default" requires knowing the default. GitHub API gives it, but for forked / multi-default-branch projects (rare but real), the check needs a fallback. Currently TODO in W4 design.
3. **Gate failure severity ordering.** If T2 (destructive) and T1 (loop) both fire on the same cycle, which gets reported first? Proposed: severity order T2 > T1 > T6 > T5 > T3 > T4. Worth confirming during W1 implementation.
4. **Webhook secret rotation.** GitHub webhook secret rotation breaks Sentinel until env var update. Should `sentinel serve` accept a list of secrets and try each? Marginal; defer to v0.2 unless ops surfaces it.
