# Sentinel Loop

You are running Sentinel's continuous improvement cycle. Each iteration:

1. **Scan**: Assess the project through all active lenses. Note what changed since last cycle.
2. **Research**: If the scan surfaces issues that need investigation, research the best approach.
3. **Plan**: Prioritize the top 1-3 work items based on scan results and research.
4. **Execute**: Pick the highest-priority item and implement it. Run tests.
5. **Review**: Self-check the changes against the acceptance criteria and relevant lenses.
6. **Commit**: If the change passes review, commit with a clear message.

After each cycle, report what you did and what's next. Then continue to the next iteration.

## Rules

- Focus on the highest-impact change each cycle
- Keep changes small and atomic — one concern per cycle
- Always run tests before and after changes
- If something is broken, fix it before doing anything else
- If you're unsure about an approach, research it before coding
- Never skip the review step
