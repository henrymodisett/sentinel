---
ID: 0001
Title: Tests accompany behavior changes
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0001 — Tests accompany behavior changes

If a change alters behavior, the diff includes a test exercising the new behavior. If a change is purely structural (rename, extract, inline), existing tests still pass.

## What it means in practice

- A bug fix lands with a test that fails before the fix and passes after.
- A new feature lands with a test for the happy path and at least one edge case.
- A refactor lands with no new tests *and* no test failures — green before, green after.
- A behavior change with "no test possible" is a signal to invest in testability before the change, not a license to skip.

## Why

Behavior change without a test is a regression waiting to happen — the next person who modifies the code has no signal that the behavior matters. Refactoring without green tests means you're changing two variables at once and can't tell which one broke things.
