---
ID: 0002
Title: API breaks require migration notes
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0002 — API breaks require migration notes

When changing a public interface (function signature, CLI flag, file format, network protocol), prefer additive changes. If breaking is necessary, the diff carries a migration note callers can follow.

## What it means in practice

- New behavior: add a new function/flag, leave the old one in place with a deprecation marker.
- Breaking behavior: rename or remove with a migration note in CHANGELOG, README, or the relevant docstring.
- Major version bump for breaking changes; minor for additive.
- Internal-only API (single-caller, no external consumers) — break freely. The discipline is for *boundaries*.

## Why

Breaking changes without migration notes turn upgrade decisions into archaeology — callers have to diff the source to figure out what changed. The migration note is cheaper than the cumulative cost of every caller doing that lookup.
