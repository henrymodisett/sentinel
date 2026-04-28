---
ID: 0009
Title: Prefer reversible changes
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0009 — Prefer reversible changes

Soft-delete over hard-delete. Feature flag over immediate rollout. Canary over big-bang. Database migrations that can be rolled back. Configuration changes that can be reverted by reverting the commit.

## What it means in practice

- Deleting a function: deprecation marker first, removal in a follow-up release.
- Deleting data: soft-delete column or archive table, hard-delete only after a quiet period.
- New features behind a flag, even if you plan to roll out 100% immediately — the flag is your kill-switch when the feature breaks.
- Schema changes: additive first (add column, dual-write), then cutover, then drop the old.
- Avoid the irreversible action when the reversible one accomplishes the same thing.

## Why

The cost of an extra step (the flag, the dual-write, the soft-delete) is small. The cost of an irreversible mistake at scale (deleted production data, broken on-call rollback path) is enormous. Reversibility is the option to be wrong cheaply.
