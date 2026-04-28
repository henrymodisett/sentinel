#!/usr/bin/env bash
#
# scripts/open-pr.sh — push the current branch and open a PR via gh.
#
# Refuses to run on the default branch. Sets upstream on first push.
# Idempotent: if a PR already exists for this branch it just prints the URL.
# Always uses the project's PR template if one exists.
#
# Usage:
#   bash scripts/open-pr.sh                # title from last commit; base = default branch
#   bash scripts/open-pr.sh --auto-merge   # open + Codex review + squash-merge
#   bash scripts/open-pr.sh --draft        # same, opened as draft
#   bash scripts/open-pr.sh --base feat/X  # stacked PR: base this PR on feat/X, not main
#   bash scripts/open-pr.sh "Custom title" # explicit title
#
# Exit contract (--auto-merge):
#   exit 0 ⇔ `gh pr view <n> --json mergedAt --jq .mergedAt` is non-empty.
#   Any other terminal state exits nonzero AND prints the PR URL with recovery
#   commands as the last lines of output. This prevents the "swarm-agent orphan
#   PR" failure mode where an agent's session ends mid-merge and leaves a
#   reviewed-but-unmerged PR open indefinitely.
#
#   Why local polling instead of `gh pr merge --auto`: native auto-merge fires
#   when GitHub's required-checks gate flips green. Touchstone's review gate is
#   the local Conductor review (run from merge-pr.sh), not a GitHub Action, so
#   a queued native auto-merge would never fire. Keeping the merge in-band lets
#   us positively confirm merge before reporting success.
#
# ⚠ Stacked PRs — read this before using --base:
#   Stacking a PR on another PR's branch is useful when work naturally
#   splits into a chain (parent PR ships primitive, child PR ships the
#   consumer that depends on it). GitHub does NOT auto-rebase the child
#   onto main when the parent squash-merges; it closes the child's branch
#   instead. So stacked PRs work well with a merge commit or rebase merge,
#   but the `--auto-merge` default (squash) will orphan the child.
#
#   For simpler review, prefer bundling related work into one PR over
#   stacks when you can. See principles/git-workflow.md.
#
set -euo pipefail

# orphan_warning is set to a PR URL once we know one — any nonzero exit after
# that point prints recovery instructions as the script's last output, so the
# user (or future agent) can see exactly which PR is stuck.
ORPHAN_PR_URL=""
ORPHAN_PR_NUMBER=""
BODY_FILE=""

on_exit() {
  local rc="$?"
  # Always clean up the temp body file, no matter how we exit.
  if [ -n "$BODY_FILE" ] && [ -f "$BODY_FILE" ]; then
    rm -f "$BODY_FILE"
  fi
  print_orphan_warning "$rc"
  return "$rc"
}

print_orphan_warning() {
  local rc="$1"
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  if [ -z "$ORPHAN_PR_URL" ]; then
    return 0
  fi
  # Re-check merge state on exit — if the PR actually merged in flight (e.g.
  # we ran past the merge step but tripped on a follow-up like the local pull)
  # then this isn't an orphan. The exit code stays nonzero; we just suppress
  # the misleading orphan banner.
  if [ -n "$ORPHAN_PR_NUMBER" ] \
    && command -v gh >/dev/null 2>&1 \
    && [ -n "$(gh pr view "$ORPHAN_PR_NUMBER" --json mergedAt --jq '.mergedAt // empty' 2>/dev/null || true)" ]; then
    return 0
  fi
  {
    echo ""
    echo "==> ORPHAN RISK: PR opened but not merged. Resolve manually:"
    echo "==>   $ORPHAN_PR_URL"
    if [ -n "$ORPHAN_PR_NUMBER" ]; then
      echo "==>   gh pr merge $ORPHAN_PR_NUMBER --squash --delete-branch    (if review passed)"
      echo "==>   gh pr close $ORPHAN_PR_NUMBER                              (if abandoning)"
    fi
  } >&2
}

# Verify the PR actually merged. Returns 0 if mergedAt is non-empty, 1 otherwise.
# Used as the post-merge sanity check that turns the script's exit contract from
# "merge-pr.sh exited 0" (proxy) into "GitHub says it's merged" (truth).
verify_pr_merged() {
  local pr_number="$1"
  local merged_at
  merged_at="$(gh pr view "$pr_number" --json mergedAt --jq '.mergedAt // empty' 2>/dev/null || echo "")"
  if [ -n "$merged_at" ]; then
    echo "==> Verified: PR #$pr_number merged at $merged_at"
    return 0
  fi
  return 1
}

REPO_ROOT="$(git rev-parse --show-toplevel)"
TEMPLATE_PATH="$REPO_ROOT/.github/pull_request_template.md"

# Fail fast if gh is missing or unauthenticated.
if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: 'gh' (GitHub CLI) is not installed. Install it before opening PRs." >&2
  exit 1
fi
if ! DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null)"; then
  echo "ERROR: Failed to resolve default branch via 'gh'. Is gh authenticated?" >&2
  echo "       Run: gh auth status" >&2
  exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [ "$CURRENT_BRANCH" = "$DEFAULT_BRANCH" ] || [ "$CURRENT_BRANCH" = "master" ]; then
  echo "ERROR: You are on '$CURRENT_BRANCH'. Code changes must go through a feature branch + PR." >&2
  echo "  git checkout -b feat/short-description   # or fix/, chore/, refactor/, docs/" >&2
  exit 1
fi

# Warn on uncommitted changes.
UNTRACKED="$(git ls-files --others --exclude-standard)"
if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$UNTRACKED" ]; then
  echo "WARNING: working tree has uncommitted changes — they will NOT be included in this PR." >&2
  if [ -n "$UNTRACKED" ]; then
    echo "         Untracked files detected:" >&2
    while IFS= read -r untracked_file; do
      printf '           %s\n' "$untracked_file" >&2
    done <<< "$UNTRACKED"
  fi
  echo "         Commit them first if they should be part of the PR." >&2
  read -r -p "         Continue anyway? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

# Parse flags early (needed before the existing-PR check).
DRAFT_FLAG=""
AUTO_MERGE=false
BASE_OVERRIDE=""
POSITIONAL=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --draft) DRAFT_FLAG="--draft"; shift ;;
    --auto-merge) AUTO_MERGE=true; shift ;;
    --base)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --base requires a branch name." >&2
        exit 1
      fi
      BASE_OVERRIDE="$2"
      shift 2
      ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"

# Resolve the actual base branch: --base overrides the repo default.
BASE_BRANCH="${BASE_OVERRIDE:-$DEFAULT_BRANCH}"
if [ "$BASE_BRANCH" = "$CURRENT_BRANCH" ]; then
  echo "ERROR: --base $BASE_BRANCH cannot equal the current branch." >&2
  exit 1
fi

# Warn when stacking + auto-merge combine — the user is likely about to
# orphan their stack. --auto-merge squashes the parent, which closes (not
# rebases) stacked children.
if [ -n "$BASE_OVERRIDE" ] && [ "$AUTO_MERGE" = true ]; then
  echo "WARNING: --base $BASE_OVERRIDE with --auto-merge stacks this PR on another branch" >&2
  echo "         AND will squash-merge it, which orphans any later stacked children." >&2
  echo "         Either drop --auto-merge (open stack, merge manually in order)" >&2
  echo "         or drop --base (bundle into one PR on $DEFAULT_BRANCH)." >&2
fi

# Push (set upstream on first push, plain push afterwards).
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  echo "==> Pushing $CURRENT_BRANCH ..."
  git push
else
  echo "==> Pushing $CURRENT_BRANCH (setting upstream) ..."
  git push -u origin "$CURRENT_BRANCH"
fi

# Install the cleanup/orphan-warning trap now — every later exit path may
# already have a PR URL we need to surface to the user, and the trap also
# handles temp-file cleanup once BODY_FILE is set further down.
trap on_exit EXIT

# If a PR already exists for this branch, just print the URL (and auto-merge if requested).
EXISTING_PR_URL="$(gh pr list --head "$CURRENT_BRANCH" --author "@me" --state open --json url --jq '.[0].url // empty' 2>/dev/null || echo "")"
if [ -n "$EXISTING_PR_URL" ]; then
  echo "==> PR already open for $CURRENT_BRANCH: $EXISTING_PR_URL"
  if [ "$AUTO_MERGE" = true ]; then
    PR_NUMBER="$(basename "$EXISTING_PR_URL")"
    ORPHAN_PR_URL="$EXISTING_PR_URL"
    ORPHAN_PR_NUMBER="$PR_NUMBER"
    MERGE_SCRIPT="$(dirname "$0")/merge-pr.sh"
    if [ ! -f "$MERGE_SCRIPT" ]; then
      echo "ERROR: merge-pr.sh not found at $MERGE_SCRIPT — cannot auto-merge." >&2
      exit 1
    fi
    echo ""
    echo "==> Auto-merging PR #$PR_NUMBER ..."
    # Don't exec — we need to verify mergedAt after merge-pr.sh returns.
    if ! bash "$MERGE_SCRIPT" "$PR_NUMBER"; then
      echo "ERROR: merge-pr.sh failed for PR #$PR_NUMBER." >&2
      exit 1
    fi
    if ! verify_pr_merged "$PR_NUMBER"; then
      echo "ERROR: merge-pr.sh exited 0 but PR #$PR_NUMBER is not merged on GitHub." >&2
      exit 1
    fi
    exit 0
  fi
  exit 0
fi

if [ "$#" -gt 0 ]; then
  TITLE="$1"
else
  TITLE="$(git log -1 --format=%s)"
fi

COMMIT_BODY="$(git log -1 --format=%b)"

# Build body from commit body + PR template (if present). The unified EXIT
# trap installed above (`on_exit`) will rm the file regardless of how we exit.
BODY_FILE="$(mktemp -t touchstone-pr-body.XXXXXX.md)"

{
  if [ -n "$COMMIT_BODY" ]; then
    printf '%s\n\n---\n\n' "$COMMIT_BODY"
  fi
  if [ -f "$TEMPLATE_PATH" ]; then
    cat "$TEMPLATE_PATH"
  fi
} > "$BODY_FILE"

echo "==> Opening PR against $BASE_BRANCH ..."
if [ -n "$DRAFT_FLAG" ]; then
  PR_URL="$(gh pr create --base "$BASE_BRANCH" --title "$TITLE" --body-file "$BODY_FILE" --draft)"
else
  PR_URL="$(gh pr create --base "$BASE_BRANCH" --title "$TITLE" --body-file "$BODY_FILE")"
fi

echo "$PR_URL"

# Capture the PR for the orphan-warning trap — anything that exits nonzero
# from here on is a stuck-PR risk.
ORPHAN_PR_URL="$PR_URL"
ORPHAN_PR_NUMBER="$(basename "$PR_URL")"

if [ -n "$DRAFT_FLAG" ]; then
  echo "    Opened as draft. Mark ready on github.com when ready to merge."
  if [ "$AUTO_MERGE" = true ]; then
    # --auto-merge + --draft is a contradiction (drafts can't merge). Don't
    # claim success silently — the user explicitly asked for a merge.
    echo "WARNING: --auto-merge ignored because --draft was passed; PR opened as draft only." >&2
  fi
  # Draft path: PR is intentionally open and not merged. That's not an orphan.
  ORPHAN_PR_URL=""
  ORPHAN_PR_NUMBER=""
  exit 0
fi

# Auto-merge: extract PR number and run merge-pr.sh, then positively verify
# the PR actually reached MERGED state on GitHub before claiming success.
if [ "$AUTO_MERGE" = true ]; then
  PR_NUMBER="$(basename "$PR_URL")"
  MERGE_SCRIPT="$(dirname "$0")/merge-pr.sh"
  if [ ! -f "$MERGE_SCRIPT" ]; then
    echo "ERROR: merge-pr.sh not found at $MERGE_SCRIPT — cannot auto-merge." >&2
    exit 1
  fi
  echo ""
  echo "==> Auto-merging PR #$PR_NUMBER ..."
  # Don't exec — we need to verify mergedAt after merge-pr.sh returns. The
  # earlier `exec bash "$MERGE_SCRIPT"` form propagated merge-pr.sh's exit
  # code but never positively confirmed merge happened, so any silent failure
  # post-review (network blip on `gh pr merge`, etc.) could end with exit 0
  # and a still-open PR. The new flow always asks GitHub.
  if ! bash "$MERGE_SCRIPT" "$PR_NUMBER"; then
    echo "ERROR: merge-pr.sh failed for PR #$PR_NUMBER." >&2
    exit 1
  fi
  if ! verify_pr_merged "$PR_NUMBER"; then
    echo "ERROR: merge-pr.sh exited 0 but PR #$PR_NUMBER is not merged on GitHub." >&2
    exit 1
  fi
fi

# Reached the natural end with no failures — clear the orphan markers so the
# EXIT trap stays quiet on a clean exit 0.
ORPHAN_PR_URL=""
ORPHAN_PR_NUMBER=""
