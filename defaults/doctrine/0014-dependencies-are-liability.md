---
ID: 0014
Title: Dependencies are liability
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0014 — Dependencies are liability

Every dependency is potential supply-chain risk + maintenance burden + version skew. Adding one is a decision that should clear a bar; "it would save 20 lines" usually doesn't.

## What it means in practice

- Before adding a new dependency, look at the standard library, existing project deps, and "could I write this in 50 lines."
- Prefer narrow, focused libraries with one author over bloated frameworks with many maintainers — fewer surfaces for surprise.
- Pin versions in lockfiles; don't trust upper-bound constraints alone.
- Audit transitive deps occasionally — `npm audit`, `pip-audit`, `cargo audit`. New transitive deps slip in via minor releases.
- Removing a dependency is a feature; celebrate it.

## Why

The libraries you use determine your security posture, your build time, your bus factor, and your upgrade path. A 50-line vendor copy you understand is often safer than a 50KLOC dependency you don't.
