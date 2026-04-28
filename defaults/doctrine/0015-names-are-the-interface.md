---
ID: 0015
Title: Names are the interface
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0015 — Names are the interface

Naming is the highest-leverage decision in code. A function's name is its contract; a variable's name is its semantics; a module's name is its boundary. Bad names compound — every reader pays the cost forever.

## What it means in practice

- Names describe *what something is or does*, not *how it's implemented*. `email_dispatcher`, not `email_helper_v2`.
- Booleans read as predicates: `is_ready`, `has_pending_writes`, `should_retry`. Not `flag` or `enabled` (enabled when?).
- Functions are verbs (`open_connection`); types and variables are nouns (`ConnectionPool`, `current_user`).
- Avoid abbreviations except for genuinely well-known ones (`url`, `id`, `db`). `usr_mgr_svc` is a smell.
- When a name doesn't fit, it's usually because the thing it names is doing too many things — fix the thing, then name it.
- Renaming is cheap and refactor-safe in modern tooling. Don't tolerate names you've outgrown.

## Why

Code is read more often than it's written. The cost of a bad name is paid by every reader, every time. Investing in good names is investing in everyone's future reading speed — including your own a month from now.
