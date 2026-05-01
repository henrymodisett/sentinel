```text
 ____             _   _            _
/ ___|  ___ _ __ | |_(_)_ __   ___| |
\___ \ / _ \ '_ \| __| | '_ \ / _ \ |
 ___) |  __/ | | | |_| | | | |  __/ |
|____/ \___|_| |_|\__|_|_| |_|\___|_|
```

> *Autonomous meta-agent for software projects — assess, plan, delegate, review.*
>
> by **[Autumn Garage](https://github.com/autumngarage/autumn-garage)** · alongside [Touchstone](https://github.com/autumngarage/touchstone) · [Cortex](https://github.com/autumngarage/cortex) · [Conductor](https://github.com/autumngarage/conductor)

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
| **Monitor** | Gemini Flash | Runs often — Flash is fast and free on the free tier |
| **Researcher** | Gemini via Conductor | Native web search, cheap |
| **Planner** | Claude via Conductor | Best judgment |
| **Coder** | Claude Code via Conductor | Full agentic loop |
| **Reviewer** | Gemini via Conductor | Independent from coder |

The Reviewer must be a different provider than the Coder. A model reviewing its own output has the same blind spots it started with.

## Providers

Sentinel delegates AI calls to [Conductor](https://github.com/autumngarage/conductor) — **no API keys live inside sentinel**. Conductor wraps each CLI or local endpoint and owns provider-specific behavior:

- `claude` — Anthropic
- `codex` — OpenAI
- `gemini` — Google (native web search)
- `ollama` — local, free, offline

Any role can use any provider, with one constraint: the **Coder** needs Conductor's agentic-code capability (`workspace-write` tool execution).

Sentinel routes by task intent, not just by static provider. Quick monitor work asks Conductor for local/offline when available; research asks for web-search + long context; code asks for tool-use + `workspace-write`; review asks for a code-review provider independent from the coder when possible.

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
sentinel cost --by-role         # spend broken down by role
sentinel cost --by-role -n 10   # by-role view, last N cycles only
sentinel providers              # provider detection + health
```

## Configuration

`.sentinel/config.toml` maps roles to providers and sets a budget:

```toml
[project]
name = "my-project"
path = "/Users/you/Repos/my-project"

[roles.monitor]
provider = "gemini"
model = "gemini-2.5-flash"      # fast + free-tier eligible; runs every cycle

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
per_day = 50.00    # optional rolling 24h cap
per_week = 200.00  # optional rolling 7d cap
```

Optional sections — see `src/sentinel/config/schema.py` for the full list:

- `[scan]` — `max_lenses`, `evaluate_per_lens`, `provider_timeout_sec`
- `[coder]` — `max_turns`, `timeout_seconds` (Coder Claude CLI timeout, 60-7200, default 600; override with `--coder-timeout` or `SENTINEL_CODER_TIMEOUT`), `max_iterations` (coder↔reviewer cap per work item, 1-10, default 3), `cli_help_allowlist` / `cli_help_max_subcommands` / `cli_help_timeout_sec` (pre-load `<tool> --help` text into the coder's prompt when the work item references an allowlisted CLI — defaults cover `gws`, `swift`, `go`, `cargo`, `pytest`, etc.; empty list disables the feature for that project)
- `[local]` — `ollama_endpoint` for non-default Ollama hosts
- `[retention]` — `runs_days` for how long cycle logs stick around

`sentinel work --plan-only` runs scan and plan, writes the run journal with
`Status: in-progress`, and stops before Coder/Reviewer execution, source edits,
or PR pushes. Rolling budget caps halt before starting another provider call and
record `Status: blocked-on-budget` in the run journal.

When the coder↔reviewer loop hits `max_iterations` without an approved verdict (or when two rounds produce identical findings), sentinel prints a post-mortem and writes a Markdown copy to `.sentinel/exhaustions/<timestamp>-<slug>.md` so you don't have to dig through reviewer transcripts. The block names the branch, the last verdict, the unaddressed findings, and concrete next steps:

```
### Coder iterations exhausted

  Work item: harden Gmail client retries
  Branch: sentinel/wi-42-harden-gmail-client-retries
  Iterations: 3/3
  Last reviewer verdict: changes-requested

  Last reviewer findings:
    - retry policy still misses 429 responses
    - test_retries.py asserts on stale fixture

Suggested next step:
  1. Inspect the branch: git checkout sentinel/wi-42-...
  2. Apply the findings manually
  3. Push as a regular PR via scripts/open-pr.sh
Or:
  1. Reduce work item scope (split into smaller items)
  2. Reject this proposal: edit
     `.sentinel/proposals/<file>` → Status: rejected
```

Project context — what it is, current stage, what matters most right now — lives in your existing `README.md`, `CLAUDE.md`, `AGENTS.md`, and any strategic docs (architecture, vision, principles). Sentinel reads them every scan.

## Design principles

- **Conductor-backed providers.** Sentinel keeps role routing and budgets; Conductor owns provider-specific AI execution. Sentinel stores no provider API keys.
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

Core loop shipped (scan, plan, execute, review). Autonomous PR factory wired (worktree + ship_pr). Available via Homebrew (`autumngarage/sentinel` tap). PyPI release tracked separately. Version is git-tag-derived (`hatch-vcs`) — see `pyproject.toml` for the dynamic-version setup; the latest release is published in the GitHub Releases tab.

## License

MIT
