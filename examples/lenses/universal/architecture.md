# Architecture Lens

## What to look for
- Clear boundaries between modules/packages with non-overlapping responsibilities
- Data flows in predictable directions — dependencies point inward
- Abstractions at the right level — not too thin (wrapper with no value), not too deep (god module)
- Single source of truth for each concept — no duplicated state
- A new engineer could understand the structure in 30 minutes

## Smells
- Circular dependencies between modules
- God objects or modules that do everything
- Leaky abstractions — implementation details exposed in public interfaces
- Shotgun surgery — one conceptual change requires touching 10+ files
- A "utils" or "helpers" directory that has become a junk drawer
- Import graphs that form tangles instead of trees
- Business logic mixed into infrastructure code (HTTP handlers, database queries)

## What good looks like
- Each directory has a clear, stated responsibility
- You can delete a module without breaking unrelated features
- New features have an obvious home — no ambiguity about where code goes
- The dependency graph is a DAG, not a web
- Infrastructure wraps domain logic, never the reverse

## Questions to ask
- If this project 10x'd in size, what breaks first?
- Where would a new team member get confused?
- Can you explain the architecture in three sentences?
- What would make this harder to split into services later?
