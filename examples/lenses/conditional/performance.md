# Performance Lens

## When to apply
All projects, but the specific concerns vary:
- APIs: response latency, throughput, database query efficiency
- CLIs: startup time, memory footprint
- UIs: render time, bundle size, perceived responsiveness
- Data pipelines: throughput, memory usage, processing time

## What to look for
- Hot paths: are the most-called functions efficient?
- Database: N+1 queries, missing indexes, full table scans on large tables
- Memory: unbounded growth, large allocations, objects kept alive too long
- I/O: unnecessary network calls, synchronous I/O in async contexts
- Algorithms: appropriate data structures and algorithms for the scale

## Smells
- N+1 query patterns (loop that makes a query per iteration)
- Loading entire datasets into memory when streaming would work
- Synchronous I/O blocking an async event loop
- No pagination on list endpoints
- String concatenation in tight loops
- Missing database indexes on columns used in WHERE/JOIN clauses
- Repeated computation that could be cached
- Large payloads transferred when only a subset is needed

## What good looks like
- Performance-critical paths are measured, not assumed
- Database queries are batched and indexed
- Caching has a clear strategy (what, how long, invalidation)
- Memory usage is bounded — no unbounded growth over time
- Performance is tested — benchmarks or load tests for critical paths

## Questions to ask
- What happens at 10x current load?
- Where is the most time spent in a typical request?
- Are there any database queries that scale linearly with data size?
- Is there monitoring for latency and throughput in production?
