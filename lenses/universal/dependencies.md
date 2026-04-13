# Dependencies Lens

## What to look for
- Currency: are dependencies reasonably up to date? Any major versions behind?
- Vulnerabilities: any known CVEs in the dependency tree?
- Minimalism: does the project pull in more than it needs?
- License compatibility: are all dependency licenses compatible with the project?
- Lock file: is the lock file committed and up to date?

## Smells
- Dependencies more than 2 major versions behind
- Known vulnerabilities that have patches available
- Large dependencies pulled in for a single function (use the stdlib or write it)
- Pinned to exact versions without a lock file (or lock file not committed)
- Dependencies that are unmaintained (no commits in 12+ months, no response to issues)
- Transitive dependency conflicts or version ranges that could break on install
- Multiple dependencies that solve the same problem (two HTTP clients, two ORMs)

## What good looks like
- Lock file committed, dependencies pinned to specific versions
- Regular dependency updates (weekly or at least monthly)
- Security advisories monitored and patched promptly
- Each dependency has a clear reason — you can explain why it's there
- Minimal transitive dependency tree — fewer indirect dependencies = fewer surprises

## Questions to ask
- When was the last time someone ran a dependency update?
- Are there any dependencies with known security issues?
- Which dependency would be hardest to replace? Is that a risk?
- Is there anything in the dependency tree you've never heard of?
