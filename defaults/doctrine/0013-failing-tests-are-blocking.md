---
ID: 0013
Title: Failing tests are blocking, not advisory
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0013 — Failing tests are blocking, not advisory

A red test is a defect, not a suggestion. Don't ship past it. "Flaky" is a hypothesis to verify, not a remediation. Quarantining a test ("skip for now, fix later") is debt that compounds.

## What it means in practice

- Red CI blocks merge. Period. Override only for documented emergencies (production fire, infrastructure outage), and with a journal entry.
- "Retry the job" is acceptable diagnosis (network blip, runner issue), but if a test fails twice in a row, treat it as a real failure.
- A `@pytest.mark.skip(reason="flaky")` is a TODO with a deadline; if it sits for >2 weeks, fix it or delete it.
- If a test is genuinely non-deterministic, the test is broken — not the code. Fix the test.

## Why

Once a team learns to ignore red tests, the test suite stops being a signal — every red is presumed flaky, including real regressions. Restoring trust in the suite costs more than fixing the original flake would have.
