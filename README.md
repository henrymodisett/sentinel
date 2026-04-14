# Sentinel

**Autonomous meta-agent for software projects.** One command — `sentinel work` — and it figures out what to do next: scans the codebase, generates project-specific analytical lenses, plans work, dispatches coding agents, reviews the output.

Built around the way a technical PM actually operates: understand the project, research the approach, make a plan, delegate, review. Sentinel automates that loop across whichever LLM CLIs you have installed.

## How it works

Every run walks one cycle:

```
ASSESS → RESEARCH → PLAN → DELEGATE
  ↑                          │
  └──────────────────────────┘
```

1. **Assess.** Read `goals.md`, scan the code, generate 6–8 project-specific lenses (e.g. `risk-surface`, `cost-awareness`, `adoption`), evaluate the project through each.
2. **Research.** Investigate the best approach for the highest-priority findings — web search, docs, cross-provider comparison.
3. **Plan.** Produce a ranked backlog with acceptance criteria.
4. **Delegate.** Hand each item to a coding agent. A different provider reviews.

Two ideas do most of the work:

- **Goals are derived, not stored.** Pulled from `goals.md`, `CLAUDE.md`, `README.md`, and GitHub issues each cycle. No second source of truth to drift.
- **Lenses are generated per scan, not shipped as fixed checklists.** For a trading system you'd get `risk-surface` and `reliability`; for a dev tool you'd get `craft` and `adoption`. Sentinel decides the advisory team based on what the project actually is.

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

Any role can use any provider.

## Quick start

```bash
uv tool install sentinel        # or: pip install sentinel
cd your-project
sentinel work
```

On first run, sentinel asks a few setup questions (or use `--preset recommended`) and writes `.sentinel/config.toml` + `.sentinel/goals.md`.

**Fill in `goals.md` before the first real run.** It's the single biggest lever on output quality — lens generation is only as good as the project context you give it.

## The one command

```bash
sentinel work                   # one full cycle
sentinel work --budget 10m      # time-bounded
sentinel work --budget $5       # money-bounded
sentinel work --every 1h        # loop continuously
sentinel work --dry-run         # plan, don't execute
```

Supporting commands:

```bash
sentinel status                 # quick health check
sentinel cost                   # spend history
sentinel providers              # provider detection + health
```

## Configuration

`.sentinel/config.toml` maps roles to providers and sets a budget:

```toml
[project]
name = "my-project"

[roles.monitor]
provider = "ollama"
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

`.sentinel/goals.md` holds project context — what it is, its current stage, what matters most right now. Sentinel reads it every scan.

## Design principles

- **CLI-based providers.** No SDKs, no stored keys. Each CLI handles its own auth.
- **Derive, don't persist.** Goals, state, plans are recomputed from source each cycle. Nothing stored to drift.
- **Dynamic lenses.** Per-project analytical perspectives beat flat universal checklists.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) — fixed research budgets, structured logging, simplicity-weighted decisions, graceful failure.

## Relationship to toolkit

[Toolkit](https://github.com/henrymodisett/toolkit) defines *what good looks like* — engineering principles, git workflow, review hooks.
Sentinel provides *the autonomous loop that drives improvement*.

Either installs without the other. Sentinel follows toolkit's principles when both are present.

## Status

v0.1.0. Core loop shipped (scan, plan, execute, review). Dogfooding on sentinel and other projects. Not yet on PyPI or Homebrew — install from source.

## License

MIT
