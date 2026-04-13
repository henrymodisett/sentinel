# Sentinel Scan

Assess the current project state through Sentinel's analytical lenses.

## What this does

Runs a comprehensive health check of the project:
1. Git status and recent changes
2. Test suite results
3. Lint results
4. Per-lens evaluation (architecture, code quality, security, testing, reliability, dependencies, tech debt, developer experience)

## Instructions

Read CLAUDE.md and README.md first to understand the project context.

Then assess the project through each lens:

**Architecture**: Are module boundaries clear? Do dependencies flow inward? Are abstractions at the right level?

**Code Quality**: Is the code readable and consistent? Are there complexity hotspots?

**Security**: Any input validation gaps? Secrets in code? Auth boundary issues?

**Testing**: Are critical paths tested? Any obvious coverage gaps? Are tests high quality?

**Reliability**: Is error handling comprehensive? Are external calls protected with timeouts?

**Dependencies**: Anything outdated? Known vulnerabilities? Lock file committed?

**Technical Debt**: How many TODOs? Dead code? Inconsistent patterns?

**Developer Experience**: Can a new developer get started easily? Is tooling documented?

Produce a health report with scores (0-100) per lens and the top 3 recommended actions.
