---
ID: 0011
Title: Validate at boundaries, not internally
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0011 — Validate at boundaries, not internally

Validate inputs where they enter the system: HTTP handlers, CLI argument parsing, file load, message queue consumer. Once data is past the boundary and typed, internal callers trust it.

## What it means in practice

- A REST handler validates query params; the service function it calls assumes valid input.
- A config loader validates the YAML schema once; modules consuming the loaded config trust the types.
- Don't sprinkle `assert isinstance(x, int)` through internal functions — the type system is the contract.
- Defensive programming inside trusted internals is noise that hides real defects (the "this can't happen" branch usually can't, until something far away changes).

## Why

Defense-in-depth is for adversarial inputs at trust boundaries, not for paranoia between cooperating internal modules. Internal validation creates the illusion of safety while obscuring where the *actual* boundary is — and the real boundary, the one that meets untrusted data, often gets neglected because "we validate everywhere."
