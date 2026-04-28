# Cycle Artifact Schema

Each `sentinel work` invocation writes a run artifact at `.sentinel/runs/<timestamp>.md`. This document defines the stable contract surface that consumers (Touchstone, downstream tooling) rely on.

## File Layout

```
---
schema-version: 1.0
sentinel-run-id: <uuid>
timestamp: <ISO 8601>
cycle-id: <slug>
branch: <git branch>
status: completed | in-progress | failed | blocked-on-human
---

# Cycle <cycle-id>

<!-- pr-body-start -->
{curated summary: what shipped, what was tried, what's queued, what's blocked}
<!-- pr-body-end -->

<!-- decisions-start -->
{decisions promotable to Doctrine — empty for most cycles}
<!-- decisions-end -->

<!-- transcript-start -->
{full role-by-role transcript with phase timings and provider call log}
<!-- transcript-end -->
```

## Frontmatter Fields

| Field | Type | Description |
|---|---|---|
| `schema-version` | string | Schema version (`1.0`). Consumers warn on `>= 2.0` but can still extract v1 anchors. |
| `sentinel-run-id` | UUID string | Unique identifier for this run instance, auto-generated. |
| `timestamp` | ISO 8601 datetime | Wall-clock time when the cycle started (`YYYY-MM-DDTHH:MM:SS`). |
| `cycle-id` | string | Human-readable slug identifying the cycle; matches the filename timestamp. |
| `branch` | string | Git branch active when the cycle ran. |
| `status` | string | Terminal state of the cycle (see below). |

## Status Values

| Value | When it applies |
|---|---|
| `completed` | Cycle ran to its natural end — all planned work attempted and exit was clean. |
| `in-progress` | Cycle is still running (checkpointed mid-cycle) or was killed without a clean exit. |
| `failed` | Cycle hit an unrecoverable error (provider failure, configuration error, etc.). |
| `blocked-on-human` | Cycle stopped because it reached a decision point requiring human input. |

## Body Anchors

The three anchor pairs are **immutable** across schema versions — consumers can always find and extract them regardless of the `schema-version` value. Future versions may add new anchor pairs; they never remove existing ones.

### `<!-- pr-body-start -->` / `<!-- pr-body-end -->`

The curated summary Touchstone uses to populate PR descriptions. Contains:
- Run metadata (project, branch, budget, exit reason, totals)
- Work item outcomes (what shipped, reviewer verdicts, PR URLs)

Not suitable for automated parsing of raw LLM calls — that data belongs in the transcript.

### `<!-- decisions-start -->` / `<!-- decisions-end -->`

Decisions surfaced during the cycle that are candidates for promotion to project Doctrine. Empty for most cycles. When populated, entries are freeform markdown.

### `<!-- transcript-start -->` / `<!-- transcript-end -->`

The verbose log: phase timings, provider call JSONL, per-role cost breakdown, provider errors. Not for PR descriptions — too noisy for human review.

## Schema Evolution Policy

Schema versions are additive-only:

- **v1 anchors are permanent.** A consumer that knows v1 can always extract `pr-body`, `decisions`, and `transcript` from any future artifact.
- **v2+ anchors are additive.** New sections get new anchor pairs; existing pairs are never repurposed or removed.
- **Consumer behavior on unknown versions:** warn once, then proceed with v1 extraction. Don't abort.

## Extracting Sections (Python)

```python
import re

def extract_section(content: str, start: str, end: str) -> str:
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), content, re.DOTALL)
    return m.group(1).strip() if m else ""

pr_body = extract_section(content, "<!-- pr-body-start -->", "<!-- pr-body-end -->")
```

## Example Artifact

```markdown
---
schema-version: 1.0
sentinel-run-id: cafef00d-1234-5678-abcd-000000000001
timestamp: 2026-04-28T09:30:00
cycle-id: 2026-04-28-093000
branch: feat/auth-refactor
status: completed
---

# Cycle 2026-04-28-093000

<!-- pr-body-start -->
**Project:** my-app  **Branch:** feat/auth-refactor  **Budget:** $2.00  **Exit:** all_done

**Total time:** 142.3s  **Total cost:** $0.8421  **Provider calls:** 12 (0 skipped — budget exhausted)

## Work items

- **wi-1** Refactor auth middleware
  - Coder: succeeded
  - Reviewer: approved
  - Verifier: ✅ verified
  - PR: [merged_armed] https://github.com/org/repo/pull/42
<!-- pr-body-end -->

<!-- decisions-start -->
<!-- decisions-end -->

<!-- transcript-start -->
## Phases

| Phase | Duration | Status |
|---|---|---|
| scan | 5.42s | done |
| plan | 2.15s | done |
| execute:wi-1 | 134.63s | done |

## Provider calls

```jsonl
{"phase":"scan","provider":"gemini","model":"gemini-2.5-flash","latency_ms":2104,"in":1820,"out":540,"cost":0.0021,"role":"monitor"}
{"phase":"execute:wi-1","provider":"claude","model":"claude-sonnet-4-6","latency_ms":98000,"in":8000,"out":2000,"cost":0.8400,"role":"coder"}
` `` `
<!-- transcript-end -->
```

*(The closing triple-backtick in the example above has a space to avoid rendering issues in this doc.)*
