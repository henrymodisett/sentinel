---
ID: 0006
Title: One concept per PR
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0006 — One concept per PR

A pull request describes one change at one level of abstraction. Mixing a refactor with a feature, a bug fix with a cleanup, or two unrelated improvements forces reviewers to context-switch and obscures the actual signal.

## What it means in practice

- "Refactor X" and "use refactored X to fix Y" are two PRs, in that order.
- Drive-by cleanup ("fixed a typo while I was here") is acceptable if it's truly drive-by; if it grows past one line, split it.
- Bundling is OK when the bundle is the actual unit of change — a coherent feature touching three files; a security fix that requires updating tests in the same diff.
- "Just one more thing" is the sound of scope creep.

## Why

Reviewers have limited attention. A PR with one clear concept gets reviewed thoroughly; a PR with three gets reviewed superficially in all three. Bisect-ability also depends on this — a good `git bisect` finds a single concept change, not a grab-bag.
