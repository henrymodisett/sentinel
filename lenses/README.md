# Sentinel Lenses

Lenses are analytical perspectives that Sentinel uses to evaluate a project. Each lens is a focused framework that tells the LLM what to look for from a specific angle.

## Universal Lenses (always active)

| Lens | Focus |
|------|-------|
| architecture | System structure, boundaries, dependencies, abstractions |
| code-quality | Readability, consistency, simplicity, naming |
| security | Auth, input validation, secrets, vulnerabilities |
| testing | Coverage of critical paths, test quality, failure modes |
| reliability | Error handling, graceful degradation, observability |
| dependencies | Currency, CVEs, minimalism, license compatibility |
| technical-debt | TODOs, workarounds, dead code, inconsistent patterns |
| developer-experience | Onboarding, feedback loops, tooling, error messages |

## Conditional Lenses (activated based on project type)

| Lens | When | Focus |
|------|------|-------|
| performance | All projects | Latency, throughput, memory, database efficiency |
| ui-design | Frontend projects | Consistency, hierarchy, responsiveness, feedback |
| api-design | API projects | Naming, errors, versioning, documentation |
| accessibility | UI projects | Keyboard nav, screen readers, contrast, focus |
| data-integrity | Database projects | Migrations, transactions, validation, backups |
| cost-efficiency | Cloud projects | Resource sizing, waste, caching, scaling |

## Custom Lenses

Drop any `.md` file into `.sentinel/lenses/` in your project. Sentinel discovers and uses it automatically.

## How the Loop Uses Lenses

- **Monitor** scans through each active lens, producing per-lens health scores
- **Researcher** investigates issues surfaced by lenses
- **Planner** balances priorities across lenses
- **Reviewer** checks that completed work doesn't regress any lens
