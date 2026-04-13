# Code Quality Lens

## What to look for
- Readability — could someone unfamiliar with the codebase understand each function?
- Consistency — are similar things done the same way throughout?
- Naming — do names reveal intent? Can you understand the code without comments?
- Simplicity — is the complexity proportional to the problem being solved?
- DRY where it matters — real duplication (same concept), not incidental similarity

## Smells
- Functions longer than ~40 lines (probably doing too much)
- Deep nesting (3+ levels of if/for/try)
- Boolean parameters that change function behavior ("flag arguments")
- Comments explaining *what* instead of *why* — the code should say what
- Premature abstractions — interfaces with one implementation, factories that build one thing
- Dead code — unreachable branches, unused functions, commented-out blocks
- Inconsistent error handling patterns across the codebase

## What good looks like
- Functions do one thing and their name says what
- Control flow is linear — early returns, guard clauses, minimal nesting
- The code reads like prose — you can follow the logic top to bottom
- Patterns are consistent — once you learn how one module works, others follow the same shape
- Complexity budget is spent on the hard problems, not on the glue code

## Questions to ask
- What's the most confusing function in this codebase? Why?
- Are there patterns that are repeated but slightly different each time?
- What would break if you deleted all the comments?
- Where does the codebase fight the language instead of using it idiomatically?
