---
ID: 0004
Title: Avoid premature abstraction
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0004 — Avoid premature abstraction

Three similar lines of code is better than a wrong abstraction. Abstract when you have three concrete examples and can see the pattern clearly — not when you anticipate one.

## What it means in practice

- Two callers of "almost the same thing" stay duplicated.
- Three callers earns the conversation about extracting a helper.
- The abstraction is named for the *concept*, not the *mechanism*. `RetryWithBackoff` not `WrappedFunction`.
- Don't add parameters or hooks for hypothetical future callers. YAGNI; add them when the second caller arrives.

## Why

Wrong abstractions are harder to fix than duplication, because the abstraction's existence becomes load-bearing — every caller assumes it captures the right concept, and changing it requires touching all of them. Duplication, by contrast, is locally fixable.
