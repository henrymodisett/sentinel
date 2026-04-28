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
