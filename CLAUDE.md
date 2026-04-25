# Sentinel — Claude Code Instructions

## Who You Are on This Project

Sentinel is an autonomous meta-agent that manages software projects through a continuous loop of state assessment, research, planning, and delegated execution across multiple LLM providers. It's an AI technical PM — it understands a project holistically through analytical lenses, identifies what needs doing, researches the best approach, and dispatches coding agents.

You are building a tool that automates what Henry does every day: investigate the project, research best approaches, make a plan, and ask AI coding agents to execute. The goal is to take this into any project and hand it off to the meta-agent.

"Good" looks like: clean Python, CLI-based provider abstraction (no API keys stored), the 5-role architecture working end-to-end, analytical lenses that provide structured expertise, and eventually Sentinel managing its own development (dogfooding).

## Engineering Principles

@principles/engineering-principles.md
@principles/pre-implementation-checklist.md
@principles/audit-weak-points.md
@principles/documentation-ownership.md

## Git Workflow

@principles/git-workflow.md

### The lifecycle (drive this automatically, do not ask the user for permission at each step)

1. **Pull.** `git pull --rebase` on the default branch before starting work.
2. **Branch.** `git checkout -b <type>/<short-description>` where `<type>` is one of `feat`, `fix`, `chore`, `refactor`, `docs`.
3. **Change + commit.** Make the code change, stage explicit file paths, commit with a concise message.
4. **Ship.** `bash scripts/open-pr.sh --auto-merge` — pushes, creates the PR, runs Codex review, squash-merges, and syncs the default branch in one step.
5. **Clean up.** `git branch -D <feature-branch>` if it still exists locally.

### Housekeeping

- Concise commit messages. Logically grouped changes.
- Run `/compact` at ~50% context. Start fresh sessions for unrelated work.

## Testing

```bash
bash setup.sh --deps-only          # reinstall deps
bash scripts/touchstone-run.sh validate  # full validation
uv run pytest                      # tests
uv run ruff check src/ tests/      # lint
uv run ruff check --fix src/ tests/   # auto-fix
```

Fix failing tests before pushing.

## Release & Distribution

Homebrew formula (`brew install sentinel` via `autumngarage/sentinel` tap) + PyPI (`pip install sentinel`). Version is derived from the git tag via `hatch-vcs` — no manual bump. Release process: tag on main (`git tag v0.X.Y`), push tag (`git push --tags`), `gh release create v0.X.Y --generate-notes`. The release-published event triggers `.github/workflows/release.yml`, which calls the shared `homebrew-bump.yml` reusable workflow in `autumngarage/autumn-garage` (pinned `@v1`) to rewrite the tap formula's `url` + `sha256` and commit directly to the tap's `main` — no hand-editing. Manual escape hatch: `gh workflow run release.yml -f tag_name=v0.X.Y` re-bumps for an existing tag. Required repo secret: `HOMEBREW_TAP_PAT` (classic PAT with `repo` scope on the tap, or fine-grained with `contents:write` on `autumngarage/homebrew-sentinel`). `.git_archival.txt` (populated by git-archive's `export-subst`) lets tag tarballs resolve the version without `.git`, so Homebrew's source archive build works.

## Architecture

### Core Design Principles

- **Conductor-backed providers**: Sentinel routes roles and budgets; Conductor owns provider-specific AI execution. Sentinel never stores provider API keys.
- **Derive, don't persist**: Goals come from CLAUDE.md/README/GitHub, not a separate config. No memory module.
- **Lenses as structured expertise**: Analytical perspectives (architecture, security, testing, etc.) that guide every step
- **Hybrid distribution**: Works as a standalone CLI AND as Claude Code agents/skills/loop.md

### The Loop

```
1. ASSESS STATE  → Monitor scans through lenses (cheap provider)
2. RESEARCH      → Researcher investigates best approaches (web search)
3. PLAN          → Planner creates prioritized work items (best judgment)
4. DELEGATE      → Coder executes, Reviewer verifies (agentic + independent)
```

Goals are derived from CLAUDE.md, README.md, and GitHub issues — not stored separately.

### The Five Roles

| Role | Default Provider | Why |
|------|------------|-----|
| Monitor | `ollama` (local) | Runs often, should be free |
| Researcher | `gemini` via Conductor | Built-in Google Search grounding |
| Planner | `claude` via Conductor | Best judgment and reasoning |
| Coder | `claude` via Conductor | Full agentic coding loop |
| Reviewer | `gemini` via Conductor | Independent from coder |

### Provider Layer

Sentinel keeps the small `Provider` contract used by roles and journals, but the single concrete implementation is `ConductorAdapter`. Config still uses Sentinel's stable names (`claude`, `openai`, `gemini`, `local`); the adapter translates them to Conductor IDs (`claude`, `codex`, `gemini`, `ollama`) and maps `CallResponse` back to `ChatResponse`.

Runtime calls route by Sentinel intent before execution:
- `quick` → local/offline if configured, used for cheap monitor subcalls
- `research` → web-search + long-context
- `plan` → high-quality reasoning, read-only
- `code` → tool-use + `workspace-write`
- `review` → code-review provider, excluding the coder provider when an alternative exists

### Package Structure

```
src/sentinel/
├── cli/          CLI entrypoint (click)
├── config/       Pydantic schemas for .sentinel/config.toml
├── providers/    Provider contract + Conductor adapter
├── roles/        The five roles (monitor, researcher, planner, coder, reviewer)
├── loop/         The core cycle orchestrator
└── research/     Extended research engine
lenses/
├── universal/    Always active (architecture, security, testing, etc.)
└── conditional/  Activated per project type (ui-design, api-design, etc.)
templates/
└── .claude/      Agents, skills, and loop.md installed by `sentinel init`
```

## Key Files

| File | Purpose |
|------|---------|
| `src/sentinel/providers/interface.py` | Provider base class and response/status types |
| `src/sentinel/providers/conductor_adapter.py` | Translates Sentinel provider calls to Conductor |
| `src/sentinel/providers/router.py` | Maps roles to provider/model pairs |
| `src/sentinel/config/schema.py` | Pydantic config with role/lens definitions |
| `src/sentinel/loop/cycle.py` | The four-step cycle orchestrator |
| `lenses/universal/*.md` | Analytical perspectives for project evaluation |
| `templates/.claude/` | Claude Code agents/skills installed into target projects |

## Sentinel and Touchstone — Two Layers, No Coupling

Sentinel and the [`autumngarage/touchstone`](https://github.com/autumngarage/touchstone) Homebrew package are deliberately separate layers. They compose, they do not depend on each other. **Do not duplicate Touchstone's value-adds inside Sentinel; do not import Touchstone modules; do not call Touchstone scripts as subprocesses.**

### Boundary

| Concern | Owned by |
|---|---|
| Lens scanning, planning, coding dispatch, LLM review, verification | Sentinel |
| Provider abstraction + routing + budget + journal | Sentinel |
| Minimum `git`/`gh` operations needed to ship a PR (push, `gh pr create`, `gh pr merge --auto --squash`) | Sentinel |
| Worktree management for parallel coders | Sentinel |
| Codex pre-merge review (fires via `pre-push` hook) | Touchstone |
| Pre-commit / pre-push validation gates | Touchstone |
| Branch cleanup helpers (`cleanup-branches.sh`) | Touchstone |
| Project starter templates | Touchstone |
| The `touchstone` CLI for humans | Touchstone |

### How they compose

The interface is **git itself**, not Python. Sentinel does `git commit` / `git push`; whatever pre-commit / pre-push hooks the user has installed fire automatically. If Touchstone is installed and its hooks are wired, Codex review runs on every push Sentinel makes — same way it would for a human-driven push. **Sentinel does not branch on whether Touchstone is installed.** Single code path; hooks decide what to do.

### Recommend together, operate independently

Sentinel must run cleanly in any repo, with or without Touchstone. `sentinel init` and `sentinel status` may *detect* Touchstone (`shutil.which("touchstone")`) and print a one-line recommendation when absent (`[recommendation] install autumngarage/touchstone for Codex pre-merge review on every PR Sentinel ships`), but never block on it.

### When in doubt

If a feature seems to need new git/PR/CI logic:
1. Check if Touchstone already does it (almost always yes).
2. If not, add it to Touchstone so other projects benefit, then call it from a git hook.
3. Build it inside Sentinel only if the logic is genuinely LLM-flavored and project-specific.

If you find yourself reaching for `subprocess.run(["bash", "scripts/open-pr.sh"…])` from Sentinel, **stop** — the boundary has been violated. Either the feature belongs in a git hook (Touchstone's domain) or in Sentinel's own minimum-`gh` layer (`src/sentinel/pr.py`).

## State & Config

- **Config**: `.sentinel/config.toml` — role-to-provider mapping, budget, active lenses
- **No memory module** — derive, don't persist
- **No goals in config** — derived from CLAUDE.md, README, GitHub issues
- **Lenses**: `lenses/` directory, copied to target projects. Users add custom lenses.

## Hard-Won Lessons

1. **Use CLIs, not SDKs.** Each provider CLI handles its own auth. Sentinel never touches API keys. This eliminates an entire class of security concerns and simplifies the install path.
2. **Derive, don't persist.** State assessments, goals, and plans are derived each cycle from current sources of truth. Persisting them creates a second source of truth that drifts silently.
3. **Lenses > checklists.** Structured analytical perspectives produce better evaluations than flat checklists because they teach the LLM how to think about a dimension, not just what to check.

## Sentinel Lenses

@lenses/universal/architecture.md
@lenses/universal/code-quality.md
@lenses/universal/security.md
@lenses/universal/testing.md
@lenses/universal/reliability.md
@lenses/universal/dependencies.md
@lenses/universal/technical-debt.md
@lenses/universal/developer-experience.md
