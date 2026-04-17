# Sentinel

**Autonomous meta-agent for software projects.** One command — `sentinel work` — and it figures out what to do next: scans the codebase, generates project-specific analytical lenses, plans work, dispatches coding agents, reviews the output.

Built around the way a technical PM actually operates: understand the project, research the approach, make a plan, delegate, review. Sentinel automates that loop across whichever LLM CLIs you have installed.

## How it works

Every run walks one cycle:

```
ASSESS → PLAN → DELEGATE
  ↑                │
  └────────────────┘
```

1. **Assess.** Read `README.md` / `CLAUDE.md` / `AGENTS.md` and any strategic docs in the repo, scan the code, generate 6–8 project-specific lenses (e.g. `risk-surface`, `cost-awareness`, `adoption`), evaluate the project through each.
2. **Plan.** Produce a ranked backlog with acceptance criteria. Optionally sync to GitHub issues with `sentinel plan --sync-github`.
3. **Delegate.** Hand each item to a coding agent. A different provider reviews.

A dedicated research phase (cross-provider comparison, consensus queries) is on the roadmap but not shipped yet.

Two ideas do most of the work:

- **Goals are derived, not stored.** Read from `README.md`, `CLAUDE.md`, `AGENTS.md`, and strategic docs in the repo each cycle. No dedicated sentinel config file for project context — no second source of truth to drift.
- **Lenses are generated from your project, not shipped as fixed checklists.** For a trading system you'd get `risk-surface` and `reliability`; for a dev tool you'd get `craft` and `adoption`. Sentinel generates the lens set on first scan and persists it to `.sentinel/lenses.md` so subsequent scans use the same lenses (useful for trend tracking). Delete that file to regenerate.

## The five roles

Each phase is powered by a role — an LLM configured for one job. You assign a provider per role.

| Role | Default | Why |
|---|---|---|
| **Monitor** | Ollama (local) | Runs often — should be free |
| **Researcher** | Gemini CLI | Native web search, cheap |
| **Planner** | Claude CLI | Best judgment |
| **Coder** | Claude Code | Full agentic loop |
| **Reviewer** | Gemini CLI | Independent from coder |

The Reviewer must be a different provider than the Coder. A model reviewing its own output has the same blind spots it started with.

## Providers

Sentinel wraps CLIs — **no API keys live inside sentinel**. Each CLI handles its own auth:

- `claude` — Anthropic
- `codex` — OpenAI
- `gemini` — Google (native web search)
- `ollama` — local, free, offline

Any role can use any provider, with one constraint: the **Coder** needs agentic-code capability (Claude or OpenAI today — Gemini and local don't qualify yet).

## Quick start

```bash
brew install autumngarage/sentinel/sentinel
```

(There's an unrelated macOS app cask also named `sentinel`, so the tap-prefixed form disambiguates.)

Or from source:

```bash
git clone https://github.com/autumngarage/sentinel ~/Repos/sentinel
cd ~/Repos/sentinel
uv tool install .
```

Then, in any project:

```bash
cd your-project
sentinel init                    # interactive, or: sentinel init --preset recommended
sentinel work
```

`sentinel init` writes `.sentinel/config.toml` and installs the Claude Code agents. No separate goals file — sentinel derives project context from `README.md`, `CLAUDE.md`, `AGENTS.md`, and any strategic docs it finds (`docs/`, `principles/`, architecture/vision/thesis files). Keep those up to date and lens generation stays sharp. `.sentinel/goals.md` is still read if you create one manually, but it's optional legacy, not the source of truth.

## The one command

```bash
sentinel work                   # one full cycle
sentinel work --budget 10m      # time-bounded
sentinel work --budget '$5'     # money-bounded (quote to stop shell expansion)
sentinel work --every 1h        # loop continuously
sentinel work --dry-run         # plan, don't execute
```

Supporting commands:

```bash
sentinel cost                   # spend history
sentinel providers              # provider detection + health
```

## Configuration

`.sentinel/config.toml` maps roles to providers and sets a budget:

```toml
[project]
name = "my-project"
path = "/Users/you/Repos/my-project"

[roles.monitor]
provider = "local"              # Ollama, runs on your machine
model = "qwen2.5-coder:14b"

[roles.researcher]
provider = "gemini"
model = "gemini-2.5-pro"

[roles.planner]
provider = "claude"
model = "claude-opus-4-6"

[roles.coder]
provider = "claude"
model = "claude-sonnet-4-6"

[roles.reviewer]
provider = "gemini"
model = "gemini-2.5-pro"

[budget]
daily_limit_usd = 15.00
```

Project context — what it is, current stage, what matters most right now — lives in your existing `README.md`, `CLAUDE.md`, `AGENTS.md`, and any strategic docs (architecture, vision, principles). Sentinel reads them every scan.

## Design principles

- **CLI-based providers.** No SDKs, no stored keys. Each CLI handles its own auth.
- **Derive, don't persist.** Goals, state, plans are recomputed from source each cycle. Nothing stored to drift.
- **Dynamic lenses.** Per-project analytical perspectives beat flat universal checklists.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) — fixed research budgets, structured logging, simplicity-weighted decisions, graceful failure.

## Relationship to touchstone

[Touchstone](https://github.com/autumngarage/touchstone) defines *what good looks like* — engineering principles, git workflow, Codex pre-merge review hooks, project starter templates.
Sentinel provides *the autonomous loop that drives improvement* — lenses, planner, coder, reviewer, verifier, PR factory.

**Either installs without the other. Together they're better.**

Sentinel does the minimum `git` and `gh` operations it needs to ship a PR (push, `gh pr create`, `gh pr merge --auto --squash`). It never imports Touchstone modules or calls Touchstone scripts as subprocesses. When Touchstone is installed and its `pre-push` hook is wired, Codex pre-merge review fires automatically on every PR Sentinel ships — same as if a human had pushed. The interface is git itself, not Python; no glue code, no conditional branches.

```bash
# Recommended together
brew install autumngarage/touchstone/touchstone
brew install autumngarage/sentinel/sentinel
```

## Status

v0.2.0. Core loop shipped (scan, plan, execute, review). Autonomous PR factory wired (worktree + ship_pr). Available via Homebrew (`autumngarage/sentinel` tap). PyPI release tracked separately.

## License

MIT
