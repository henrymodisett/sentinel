# Testing Lens

## What to look for
- Coverage of critical paths — not line coverage percentage, but "are the things that matter tested?"
- Test quality — do tests verify behavior or just exercise code?
- Failure mode tests — are edge cases, error paths, and boundary conditions covered?
- Test speed — is the test suite fast enough to run on every commit?
- Test isolation — can any test run independently? No order dependence?

## Smells
- Tests that pass when the implementation is wrong (testing the mock, not the code)
- No tests for error paths — only happy paths covered
- Tests that are brittle — break when implementation changes but behavior doesn't
- Slow tests blocking the dev loop (test suite >60s for a small project)
- Tests that depend on external services without mocking the boundary
- Snapshot tests for logic (snapshots are for output stability, not correctness)
- Test setup that's longer than the test itself
- Bug fixes without a regression test

## What good looks like
- Every bug fix has a test that would have caught it
- Tests document intended behavior — reading tests tells you how the code should work
- Fast tests run on every save; slow tests run on push
- Tests use the public API, not internal implementation details
- Failure messages tell you what went wrong without reading the test code
- Test data is minimal — only what's needed to demonstrate the scenario

## Questions to ask
- If you introduced a subtle bug in the core logic, which test would catch it?
- What's the most critical code path that has zero tests?
- How long does the test suite take? Is anyone skipping it because it's slow?
- Are there tests that nobody understands but nobody dares delete?
