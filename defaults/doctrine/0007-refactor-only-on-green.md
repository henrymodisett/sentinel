---
ID: 0007
Title: Refactor only on green
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0007 — Refactor only on green

Refactor with a green test suite. If tests are red when you start, fix them first or revert to the last green state. Refactoring on red mixes "did this change behavior?" with "did this fix the existing failure?" and you can't tell which.

## What it means in practice

- Step 0 of any refactor: confirm `make test` (or equivalent) is green.
- If you find tests red mid-refactor, stash, get back to green, then resume.
- Behavior change and shape change in the same commit is forbidden — separate them.
- "I'll fix the tests at the end" is a trap; you'll fix them by adjusting expectations, which is just hiding the regression.

## Why

The whole point of a test suite is to give you a signal. Refactoring on red destroys the signal — green-after means nothing if you don't know what red-before meant.
