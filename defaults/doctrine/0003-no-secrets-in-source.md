---
ID: 0003
Title: No secrets in source
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0003 — No secrets in source

API keys, tokens, credentials, signing keys, and connection strings with embedded passwords never land in tracked files. Use environment variables, a secret store (system keychain, vault, secrets manager), or a runtime injection mechanism.

## What it means in practice

- `.env` files with real values are gitignored; commit `.env.example` with placeholders only.
- Connection strings in config use `${VAR}` interpolation, not literal credentials.
- If you find a secret already committed, treat it as compromised — rotate first, then scrub history.
- gitleaks (or equivalent) runs at commit/push time; failures are blocking.

## Why

A secret in git history is a secret leaked to anyone with read access, forever. Public repos amplify this — bots scan GitHub for credentials within minutes of push. The cost of treating "rotate the credential" as the only remediation is small compared to assuming history-scrubbing made it safe.
