# Sentinel Research
<!-- Installed copy. Canonical source: templates/.claude/skills/sentinel-research/SKILL.md -->

Deep research on a topic to guide the next engineering decision.

## Arguments

$ARGUMENTS — the topic or question to research.

## Instructions

Research the given topic thoroughly:

1. **Understand the context**: Read CLAUDE.md to understand the project, then figure out why this topic matters for this specific project.

2. **Search broadly**: Use web search to find:
   - Best practices and recommendations
   - How similar projects solve this problem
   - Current state of relevant libraries/tools
   - Known pitfalls and anti-patterns

3. **Evaluate for this project**: Filter findings through the project's specific constraints, tech stack, and goals.

4. **Synthesize**: Produce a research brief with:
   - Key findings (with sources)
   - Recommended approach for THIS project
   - Tradeoffs and risks
   - Confidence level (low/medium/high)

Be specific and actionable. "Use library X because Y" is better than "consider several options."
