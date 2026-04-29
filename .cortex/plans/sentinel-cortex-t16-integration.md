---
Status: shipped
Written: 2026-04-18
Author: human
Goal-hash: e754c76b
Updated-by:
  - 2026-04-18T17:00 human (created; scoping T1.6 after R4 small items shipped)
  - 2026-04-29 claude-code (status flipped to `shipped`; verified: write path in `src/sentinel/integrations/cortex.py:572-689`, hook in `src/sentinel/cli/work_cmd.py:407-479`, config in `src/sentinel/config/schema.py:294-314`, init prompt in `src/sentinel/cli/init_cmd.py:488-508`. Follow-ups recorded below remain open: shared template contract + migration to `cortex journal append` once Cortex Phase D ships.)
Cites: doctrine/0001-why-autumn-garage-exists, plans/autumn-mail-dogfood, https://github.com/autumngarage/cortex/blob/main/.cortex/protocol.md, https://github.com/autumngarage/cortex/blob/main/.cortex/templates/journal/sentinel-cycle.md, https://github.com/autumngarage/sentinel/pull/73
---

# Sentinel writes Cortex journal entries at cycle end (Protocol T1.6)

> **2026-04-29 status — SHIPPED.** Verified against HEAD; code pointers in the frontmatter Updated-by entry. The original "why this plan is in autumn-garage" meta section at the bottom is now obsolete (the plan moved here 2026-04-28); leaving as historical context. Open follow-ups remain in § Follow-ups (deferred): migrate to `cortex journal append` when Cortex Phase D ships, and adopt shared-template-location contract.

> When `sentinel work` finishes a cycle and writes `.sentinel/runs/<timestamp>.md`, it also appends a corresponding `journal/<date>-sentinel-cycle-<id>.md` entry to the project's `.cortex/journal/` — iff `.cortex/` is present. First real exercise of the Cortex Protocol's Tier-1 write-triggers in practice. Cortex Phase E kickoff.

## Why (grounding)

Cortex Protocol v0.2.0 already specifies T1.6 (sentinel-cycle) as a Tier-1 enforceable trigger with a canonical template at `.cortex/templates/journal/sentinel-cycle.md`. What's missing is the *producer* — until today, nothing actually writes those entries. Protocol is advice without an enforcer until code honors it.

Operationalizing T1.6 is also the smallest-possible test of the "compose by file contract" design rule (cortex Doctrine 0002 / autumn-garage Doctrine 0001): two tools, two install paths, two release cadences, composing *only* through a file format. If this works, Touchstone's parallel triggers (T1.7 arch-diff → doctrine/candidate.md; T1.9 pr-merged → journal/pr-merged.md) follow the same pattern. If this doesn't work, the file-contract thesis needs revisiting before we compound the pattern.

The autumn-mail dogfood also gets a real payoff here: the first `sentinel work` cycle becomes an observed, replayable event in autumn-mail's memory instead of transient state in `.sentinel/runs/`.

Grounds-in: `.cortex/doctrine/0001-why-autumn-garage-exists`.

## Approach

**Write mechanism: direct file write, following the Cortex template shape.** Sentinel writes `.cortex/journal/<YYYY-MM-DD>-sentinel-cycle-<cycle-id>.md` directly using the journal-entry shape defined in Cortex's bundled `templates/journal/sentinel-cycle.md`. No shell-out to a `cortex journal append`-style CLI — that's Phase D in Cortex, not shipped.

**Why direct write:** Cortex Phase D authoring helpers (`cortex journal draft`, `cortex plan spawn`) aren't shipped yet. Waiting for them would gate T1.6 on an independent Cortex milestone. Meanwhile the journal entry format IS specified and templated, so Sentinel can produce a conformant entry today by following the template. When Phase D ships and `cortex journal append` exists, Sentinel can switch to shelling out — the file output is the same either way, so consumers of the journal don't notice.

**Detection: presence of `.cortex/` at the repo root.** Same git-root detection Sentinel already uses elsewhere. If `.cortex/` absent, skip silently. If present but `cortex` CLI is absent, still write (the file format is self-describing; cortex is only needed to *validate* the entry later, not to produce it).

**Opt-in / opt-out:** default auto-detect. Explicit flags `--cortex-journal` / `--no-cortex-journal` on `sentinel work` force on/off. Config file `.sentinel/config.toml` gets a `[integrations.cortex] enabled = "auto"` section with values `auto | on | off`.

**Failure mode: non-blocking warning.** If the write fails (permissions, disk, invalid template), print a warning and continue. The cycle itself is unaffected. Log the failure to `.sentinel/state/cortex-write-errors.jsonl` so users can debug without grepping stderr.

**Content shape** (from the `sentinel-cycle.md` template — read the current template at cycle time, don't hardcode):

```markdown
# Sentinel cycle <id> — <short summary>

**Date:** YYYY-MM-DD
**Type:** sentinel-cycle
**Trigger:** T1.6
**Cites:** .sentinel/runs/<timestamp>.md

> One-line summary of the cycle outcome (verdict + headline finding).

## Cycle summary

- **Lenses:** privacy-guardian 70/100 · cli-integrator 0/100 · ...
- **Health:** 31/100
- **Findings count:** 2 refinements + 3 expansion proposals
- **Verdict:** dry_run | approved N of M | failed | budget-exhausted
- **PR:** https://github.com/autumngarage/autumn-mail/pull/N (if any)
- **Spend:** $X.XX
- **Duration:** Ns
- **Providers used:** monitor=gemini-2.5-flash, coder=claude-sonnet-4-6, reviewer=codex-gpt-5.4

## Run journal

[Link to .sentinel/runs/<timestamp>.md with a short excerpt of the key phase outputs.]

## Follow-ups

- [ ] Concrete next-cycle actions if any, else a one-liner noting no follow-up needed.
```

**Content generation from the cycle:** Sentinel's existing `sentinel/runs/<timestamp>.md` writer already collects lenses, scores, findings counts, verdict, PR link, spend, duration, and provider calls. A new function in `sentinel/integrations/cortex.py` (or similar module) reads those same data structures and renders the Cortex template. Single source of truth in the cycle data; two output formats (`.sentinel/runs/` is the full machine-readable record; `.cortex/journal/` is the summary for memory consumption).

## Success Criteria

This plan is done when all of the following hold:

1. `sentinel work` in a fresh project scaffolded via `touchstone new <dir> --with-cortex` produces both `.sentinel/runs/<timestamp>.md` AND `.cortex/journal/<date>-sentinel-cycle-<id>.md` at cycle end.
2. The Cortex journal entry validates clean under `cortex doctor` (no frontmatter errors, no missing sections, `Trigger: T1.6` present).
3. `cortex doctor --audit` on the same repo matches the T1.6 fire (the cycle-end event) to the produced journal entry — the existing audit machinery recognizes the write.
4. Running `sentinel work` in a project *without* `.cortex/` produces only `.sentinel/runs/<timestamp>.md`; no warnings, no errors, no orphan files.
5. Write failures (tested by running against a read-only `.cortex/journal/`) produce a visible warning to stderr, log the failure to `.sentinel/state/cortex-write-errors.jsonl`, and do not fail the cycle — `sentinel work` exits 0 on an otherwise-successful cycle with a cortex-write failure.
6. The produced journal entry includes the fields listed in the "Content shape" section above, populated from the cycle data with no placeholder text.
7. Dedup: re-running `sentinel work` on an unchanged repo that produces the same cycle-id does NOT overwrite an existing journal entry; it skips with a warning ("cortex-journal entry for cycle <id> already exists; skipping write"). (Cycle IDs are timestamp-based, so this only fires on clock anomalies in practice.)
8. Sentinel's explicit flags work: `--cortex-journal` forces write even if detection says off; `--no-cortex-journal` forces skip even if detection says on.
9. Config file: `[integrations.cortex] enabled = "off"` in `.sentinel/config.toml` disables writes project-wide.
10. `sentinel init` wizard adds one prompt: "Write Cortex journal entries at cycle end? [Y/n if .cortex/ detected else N/y]" — default reflects `.cortex/` presence at init time.
11. Regression: all pre-T1.6 Sentinel tests still pass. No behavior change for users without Cortex.
12. Dogfood gate: one real `sentinel work --budget $3` cycle on `autumn-mail` produces a valid Cortex journal entry that `cortex doctor` accepts on the first try.

## Work items

### Detection + config

- [ ] `sentinel/integrations/cortex.py` module: `detect_cortex()` returns `(dir_present, cli_present, version)` using the same file-contract detection Sentinel's R3 `siblings.py` already uses.
- [ ] Config schema addition in `src/sentinel/config/schema.py`: `[integrations.cortex]` section with `enabled = "auto" | "on" | "off"` (default `"auto"`). Pydantic validation.
- [ ] New CLI flags on `sentinel work`: `--cortex-journal` / `--no-cortex-journal`. Flag precedence > config > auto-detection.
- [ ] `sentinel init` wizard prompt (detected-default).

### Content rendering

- [ ] `sentinel/integrations/cortex.py:render_cycle_journal_entry(cycle_data) -> str` — takes the existing cycle data dict, produces a Cortex-conformant markdown file body. Template literal in-code initially; can migrate to reading `.cortex/templates/journal/sentinel-cycle.md` later if the template lives in a stable path.
- [ ] Helper that enumerates providers used from the run's `provider_calls` list, de-duplicating by role.
- [ ] Helper that formats lenses block concisely (`privacy-guardian 70/100 · cli-integrator 0/100`).

### Write path

- [ ] `write_cortex_journal_entry(project_dir, cycle_data)` — computes the filename, checks for dedup, atomic write (write-temp-and-rename), handles permission errors with structured logging.
- [ ] Error log: `.sentinel/state/cortex-write-errors.jsonl` — append one line per failure with `{"timestamp", "cycle_id", "error_class", "error_message"}`.
- [ ] Hook point: call the write path at the same moment `.sentinel/runs/<timestamp>.md` is finalized. Single source of truth for "cycle ended" — no drift between the two writes.

### Tests

- [ ] `tests/test_cortex_integration.py`:
  - Fresh `.cortex/` present + cycle runs → file written, content validates.
  - No `.cortex/` → no write, no warning.
  - `--no-cortex-journal` → no write even when `.cortex/` present.
  - `--cortex-journal` → write even if `enabled = "off"` in config.
  - Read-only `.cortex/journal/` → warning logged, cycle exits 0.
  - Dedup: same cycle-id twice → second call skips with warning.
- [ ] Integration test (slower): real `cortex` CLI installed → produced entry passes `cortex doctor` without editing.
- [ ] Existing Sentinel tests: no regressions (especially `test_loop`, `test_runs`, `test_cli`).

### Distribution

- [ ] PR stacked on the latest Sentinel branch available at dispatch time.
- [ ] Changelog / release-note entry: "Sentinel now writes Cortex journal entries at cycle end when `.cortex/` is detected. Cortex Protocol v0.2.0 T1.6 operationalized."
- [ ] Update Sentinel's README to describe the integration in the composition section.

## Follow-ups (deferred)

Each resolves to a specific future plan or journal entry per SPEC § 4.2.

- **T1.7 (Touchstone pre-merge → doctrine/candidate.md) operationalization.** Same file-contract pattern; different producer (Touchstone's codex-review hook), different template. Resolves to a future `plans/touchstone-cortex-t17-integration.md` once T1.6 lands and proves the pattern.
- **T1.9 (Touchstone post-merge → journal/pr-merged.md) operationalization.** Resolves to the same follow-up plan or a sibling plan.
- **Migration to `cortex journal append` CLI** once Cortex Phase D ships. Today Sentinel writes the file directly; when Phase D ships a proper authoring CLI, Sentinel should migrate to shelling out so Cortex owns the file format at the write point. Resolves to a future plan once Cortex Phase D is in progress.
- **Shared template location contract.** Today Sentinel embeds the cycle-template literal in its own code. Consider a later move where Sentinel reads `.cortex/templates/journal/sentinel-cycle.md` at runtime so Cortex owns the shape. Resolves to the Phase D migration plan.

## Known limitations at exit

- **Sentinel owns the cycle-template shape twice.** The template literal lives in Sentinel's code; the canonical shape lives in Cortex's bundled templates. If Cortex updates its template shape (e.g., adds a required field), Sentinel will silently produce an outdated shape until Sentinel is updated to match. Mitigation: `cortex doctor` validation will catch the drift. Permanent fix is the Phase D migration (see follow-ups).
- **No retroactive writes.** Cycles that ran before this ships don't get journal entries retroactively. Acceptable — the Journal is append-only per Cortex SPEC § 3.5; backfilling would require Cortex's `--backfill` flag which isn't shipped.
- **Auto-detect heuristic is loose.** "`.cortex/` present at git-root" is the only signal. A project with a `.cortex/` that's actually someone else's (e.g., a forked repo where the upstream has `.cortex/` but this project doesn't use it) will silently get entries written. Acceptable because write-failures are non-blocking; user sees journal entries appear and can `--no-cortex-journal` or edit config.
- **No cross-tool timestamp coordination.** Sentinel's cycle timestamp and Cortex's journal filename timestamp may differ by seconds if the write spans a second-boundary. Mitigation: derive filename timestamp from cycle start, not write time.

## Meta — why this plan is in autumn-garage, not sentinel

Integration between two tools is a coordination question, not a single-tool question. The plan lives in the coordination repo; the *implementation* lands in sentinel. When an agent is dispatched to implement, that agent's brief will point here for the scope and success criteria, and execute the changes in the sentinel repo.
