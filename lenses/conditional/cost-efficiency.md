# Cost Efficiency Lens

## When to apply
Projects that use cloud infrastructure, paid APIs, or metered services.

## What to look for
- Resource sizing: are instances/containers right-sized for actual load?
- Waste: are there resources running that nobody uses?
- API costs: are paid API calls (LLM, search, maps) optimized?
- Caching: is cacheable data being re-fetched unnecessarily?
- Architecture: does the design minimize cost at the expected scale?

## Smells
- Oversized instances running at 5% utilization
- Dev/staging environments left running 24/7 when used only during work hours
- LLM API calls without prompt caching enabled
- No cost alerts or budget limits set
- Polling APIs at high frequency when webhooks are available
- Storing large files in a database instead of object storage
- Full data processing pipelines re-running when incremental updates would work
- No distinction between hot and cold storage tiers

## What good looks like
- Cost is monitored and attributed to teams/features
- Auto-scaling matches actual demand (not just peak provisioning)
- Caching at every layer: CDN, application, database query cache
- LLM calls use the cheapest model that meets quality requirements
- Non-production environments scale down outside business hours
- Cost per unit (per request, per user, per transaction) is tracked over time

## Questions to ask
- What's the monthly cloud bill and is it trending up or down?
- Which service/feature costs the most? Is that justified?
- Are there resources running right now that nobody is using?
- If usage doubled tomorrow, would costs double too or is there a scaling cliff?
