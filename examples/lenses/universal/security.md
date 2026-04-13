# Security Lens

## What to look for
- Authentication: how are users/services verified? Token lifecycle? Session management?
- Authorization: are permissions checked at every boundary, not just the UI?
- Input validation: is ALL external input (user input, API params, file uploads, headers) validated?
- Secrets: are API keys, passwords, tokens kept out of code, logs, and error messages?
- Dependencies: any known CVEs? Are security patches current?

## Smells
- SQL or command strings built with string concatenation or f-strings
- Broad exception handlers around auth/security code
- Secrets in environment variables with no rotation mechanism
- CORS configured to allow all origins
- No rate limiting on authentication or payment endpoints
- User input passed directly to shell commands, SQL, or template engines
- Logging that includes request/response bodies (may contain tokens, passwords, PII)
- Default credentials in config files or docker-compose
- JWT tokens that never expire or have excessive claims

## What good looks like
- Defense in depth — multiple layers, not one gate
- Principle of least privilege at every level (DB permissions, API scopes, file access)
- Security-critical code is the *simplest* code in the repo — no cleverness
- Failed auth attempts are logged with context (IP, timestamp) but never with credentials
- Input validation happens at the boundary; internal code trusts validated data
- Secrets are injected at runtime, rotated on a schedule, and never logged

## Questions to ask
- If an attacker had read access to the logs, what would they learn?
- What's the blast radius if a single API key leaks?
- Which endpoint would you attack first?
- Is there any path from user input to a shell command or SQL query without sanitization?
