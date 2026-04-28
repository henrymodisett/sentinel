---
ID: 0008
Title: Don't bypass review gates
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0008 — Don't bypass review gates

`--no-verify`, `--force`, `--skip-checks`, `i` (interactive override), or any flag whose purpose is "make the gate go away" is a yellow flag at minimum. If a gate is wrong, fix the gate; don't tunnel under it.

## What it means in practice

- A failing pre-commit hook means the change isn't ready or the hook is wrong. Fix one or the other.
- A failing CI test is blocking by default. "Flaky test, retrying" is a hypothesis to verify, not a remediation.
- Force-push to a published branch needs a reason that survives review.
- Emergency overrides exist (production fire, broken main blocking everyone). Document why in the commit/PR; the absence of an explanation is the bug.

## Why

Gates exist because someone learned the hard way. Bypassing them imports the lesson back into your codebase. If a gate genuinely doesn't apply to this change, the gate has a missing escape hatch — file an issue against the gate, don't ad-hoc bypass.
