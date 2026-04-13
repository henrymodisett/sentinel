# Technical Debt Lens

## What to look for
- TODO/FIXME/HACK comments — are they accumulating or being addressed?
- Workarounds that were meant to be temporary
- Dead code — unreachable functions, unused imports, commented-out blocks
- Inconsistent patterns — old code does it one way, new code another
- Parts of the codebase that everyone avoids touching

## Smells
- TODOs older than 6 months with no associated issue
- "Temporary" workarounds that have been in place for multiple releases
- Copy-pasted code with minor variations (diverged clones)
- Commented-out code blocks (if it's not needed, delete it; git remembers)
- Configuration through code changes instead of actual config
- Feature flags that are always on/off (never cleaned up)
- Migration code that ran once and is now dead weight
- Layers of abstraction added to work around limitations of earlier layers

## What good looks like
- TODOs have associated issues and target dates
- Technical debt is tracked explicitly, not just in comments
- Regular debt paydown — some capacity dedicated to cleanup every sprint
- Old patterns are migrated when touched, not left as "legacy"
- The team can articulate what debt exists and why it was accepted

## Questions to ask
- What part of the codebase are you most afraid to change?
- How many TODOs are in the code right now? How old are they?
- If you had a week with no feature pressure, what would you fix first?
- Is debt accumulating faster than it's being paid down?
