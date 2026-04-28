---
ID: 0010
Title: Comments explain why, not what
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0010 — Comments explain why, not what

Well-named code shows what it does. Comments explain *why* — hidden constraints, surprising context, the bug the code works around, the spec it implements.

## What it means in practice

- `# increment counter\ncounter += 1` is noise. Delete it.
- `# Slack rate-limits us at 50 req/min; queue beyond 40 req/min to leave headroom for retries` is signal. Keep it.
- A comment that just describes the next line is a sign the next line should be renamed.
- TODOs include enough context to be actionable — a date, an issue link, or a "remove once X" condition.
- Don't reference the current task or PR ("fixed in #123") — that rots; well-named code + commit history capture it.

## Why

What-comments lie because they don't get updated when the code changes. Why-comments don't lie because the why doesn't change with refactoring. Reading code with good why-comments is faster than reading code without them; reading code with stale what-comments is slower than reading uncommented code.
