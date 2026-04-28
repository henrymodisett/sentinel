---
ID: 0012
Title: Match scope to the request
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0012 — Match scope to the request

When fixing a bug, fix the bug. When adding a feature, add the feature. Don't fold in unrelated cleanup, refactors, or "while I was here" improvements without separate authorization.

## What it means in practice

- A one-line fix is a one-line PR. Resist the urge to "improve" the surrounding code.
- A feature PR doesn't also bump dependencies, migrate to a new framework, or rename modules.
- If you spot adjacent issues while working, file them; don't fix them in the same diff.
- Authorization to expand scope ("yeah while you're at it") is fine — but it has to be explicit, not assumed.

## Why

Scope creep destroys reviewability and makes rollback risky. A bug fix bundled with a refactor can't be reverted without losing the fix; a feature bundled with a dependency bump pins both to the same version forever. Keeping scope tight is also a respect contract — the reviewer agreed to look at one thing.
