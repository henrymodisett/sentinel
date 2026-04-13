# Developer Experience Lens

## What to look for
- Onboarding: can a new developer go from clone to running in under 10 minutes?
- Feedback loops: is the edit-build-test cycle fast?
- Documentation: is the README accurate? Are there setup instructions that actually work?
- Error messages: when something fails, does the message tell you how to fix it?
- Tooling: are lint, format, test, and build one command each?

## Smells
- README setup instructions that don't work (outdated, missing steps)
- Build that takes more than 30 seconds for incremental changes
- Cryptic error messages from custom tooling
- Multiple manual steps to get a dev environment working
- No `.env.example` or config template — new devs guess at required config
- Platform-specific setup with no documentation for other platforms
- Tests that require external services to be running locally with manual setup

## What good looks like
- One command to set up: `bash setup.sh` or equivalent
- One command to validate: `make test` / `pnpm validate` / `uv run pytest`
- Error messages include what went wrong, why, and how to fix it
- Hot reload / watch mode for development
- CI matches local — if it passes locally, it passes in CI
- Contributing guide that covers the non-obvious parts

## Questions to ask
- How long does it take a new hire to submit their first PR?
- What's the most common "gotcha" that trips up new developers?
- Is there tribal knowledge that isn't written down?
- Can you run the full test suite without an internet connection?
