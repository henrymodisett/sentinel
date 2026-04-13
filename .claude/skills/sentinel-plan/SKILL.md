# Sentinel Plan
<!-- Installed copy. Canonical source: templates/.claude/skills/sentinel-plan/SKILL.md -->

Generate a prioritized backlog of work items from the current project state.

## Instructions

1. **Assess**: Run `/sentinel-scan` first (or read a recent scan if available in conversation).

2. **Identify gaps**: Compare the current state against:
   - The project's stated goals (from CLAUDE.md / README.md)
   - Open GitHub issues
   - Lens scores — which lenses scored lowest?

3. **Generate work items**: For each identified gap, create a work item:
   - Title (imperative: "Add input validation to /api/users endpoint")
   - Type: feature / bugfix / refactor / test / docs / chore
   - Priority: critical / high / medium / low
   - Complexity: 1-5 (1=trivial, 5=major)
   - Which lens surfaced it
   - Files likely to be touched
   - Acceptance criteria (how to know it's done)
   - Risk (what could go wrong)

4. **Prioritize**: Rank by impact x effort x risk. Critical security issues first. Then high-impact, low-effort wins. Then larger improvements.

5. **Output**: A numbered backlog, top item first, with rationale for the ordering.

Keep each work item atomic — one clear change that can be completed, tested, and reviewed independently.
