# sentinel — Gemini CLI Instructions

Gemini CLI should follow the same project contract as Claude and Codex.

Read `AGENTS.md` before coding. Follow its Authoring Guide for implementation work and its Review Guide when explicitly reviewing a PR or running the AI review hook. Claude-specific context may live in `CLAUDE.md`, but `AGENTS.md` is the shared source for agent workflow and review priorities.

## Delivery Lifecycle

Drive this automatically unless the user asks for a different flow:

1. Pull/rebase the default branch.
2. Create a feature branch before editing tracked files.
3. Make the change, stage explicit file paths, and commit with a concise message.
4. From a clean worktree, run `CODEX_REVIEW_FORCE=1 bash scripts/codex-review.sh` so Conductor can review and safely auto-fix before merge.
5. If Conductor creates fix commits, let the loop finish. If it blocks, address findings, commit, and rerun until clean.
6. Ship with `bash scripts/open-pr.sh --auto-merge`; this creates the PR, runs the final read-only Conductor merge review, squash-merges, and syncs the default branch.
7. Clean up the feature branch if it still exists locally.

<!-- conductor:begin v0.8.2 -->
## Conductor delegation

This project has [conductor](https://github.com/autumngarage/conductor)
available for delegating tasks to other LLMs from inside an agent loop.
You can shell out to it instead of trying to do everything yourself.

Quick reference:

- Quick factual/background ask:
  `conductor ask --kind research --effort minimal --brief-file /tmp/brief.md`.
- Deeper synthesis/research:
  `conductor ask --kind research --effort medium --brief-file /tmp/brief.md`.
- Code explanation or small coding judgment:
  `conductor ask --kind code --effort low --brief-file /tmp/brief.md`.
- Repo-changing implementation/debugging:
  `conductor ask --kind code --effort high --brief-file /tmp/brief.md`.
- Merge/PR/diff review:
  `conductor ask --kind review --base <ref> --brief-file /tmp/review.md`.
- Architecture/product judgment needing multiple views:
  `conductor ask --kind council --effort medium --brief-file /tmp/brief.md`.
- `conductor list` — show configured providers and their tags.

Conductor does not inherit your conversation context. For delegation,
write a complete brief with goal, context, scope, constraints, expected
output, and validation; use `--brief-file` for nontrivial `exec` tasks.
Default to `conductor ask`; use provider-specific `call` / `exec` only
when the user explicitly asks for a provider or the semantic API does not
fit.

Providers commonly worth delegating to:

- `kimi` — long-context summarization, cheap second opinions.
- `gemini` — web search, multimodal.
- `claude` / `codex` — strongest reasoning / coding agent loops.
- `ollama` — local, offline, privacy-sensitive.
- `council` kind — OpenRouter-only multi-model deliberation and synthesis.

Full delegation guidance (when to delegate, when not to, error handling):

    ~/.conductor/delegation-guidance.md
<!-- conductor:end -->
