# API Design Lens

## When to apply
Projects that expose an API (REST, GraphQL, gRPC, WebSocket).

## What to look for
- Consistency: do endpoints follow predictable patterns?
- Naming: are resources named as nouns, actions as verbs?
- Error handling: are error responses structured, consistent, and helpful?
- Versioning: is there a strategy for backward compatibility?
- Documentation: is the API documented and is the documentation accurate?

## Smells
- Inconsistent naming conventions across endpoints
- Error responses that return 200 with an error in the body
- No pagination on list endpoints
- Breaking changes without version bumps
- Undocumented endpoints or parameters
- Over-fetching (returning entire objects when clients need one field)
- No rate limiting on public endpoints
- Authentication inconsistently applied across endpoints

## What good looks like
- Predictable URL structure — knowing one endpoint tells you what others look like
- Structured error responses with error code, message, and remediation hint
- Pagination, filtering, and sorting on all list endpoints
- OpenAPI/Swagger spec generated from code (not hand-maintained)
- Backward compatibility — old clients don't break when new fields are added
- Idempotent operations where possible

## Questions to ask
- Can a developer integrate with this API using only the documentation?
- What happens when a client sends unexpected input?
- Is there a deprecation strategy for old endpoints?
- Are there endpoints that return too much data?
