# Reliability Lens

## What to look for
- Error handling: is every failure visible? No swallowed exceptions?
- Graceful degradation: when a dependency fails, does the system degrade or crash?
- Retry logic: are transient failures retried with backoff? Are permanent failures failed fast?
- Timeouts: does every external call have a timeout?
- Observability: can you tell from logs/metrics what the system is doing and why it failed?

## Smells
- `except: pass` or `except Exception: continue` without logging
- External calls without timeouts (HTTP, database, file I/O)
- Retry loops without backoff or retry limits (infinite retry storms)
- Error messages that say what happened but not why or what to do about it
- State that can become inconsistent if a process crashes mid-operation
- No health check or liveness endpoint
- Logging at the wrong level — everything is INFO, nothing is structured

## What good looks like
- Every exception is either re-raised or logged with enough context to debug from logs alone
- External calls have timeouts, retries with exponential backoff, and circuit breakers
- The system starts clean, degrades gracefully, and recovers automatically
- Structured logging with correlation IDs — you can trace a request across services
- Crash recovery: the system handles restart without manual intervention
- Operations that must be atomic ARE atomic (transactions, file renames, etc.)

## Questions to ask
- If the database goes down for 30 seconds, what happens?
- Can you diagnose a production issue from logs alone, without reproducing it?
- What's the longest the system has run without human intervention?
- Is there any state that could get corrupted by a crash at the wrong moment?
