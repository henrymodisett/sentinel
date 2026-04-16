"""Tests for the per-work-item worktree primitive.

The primitive must guarantee: (a) Coder edits happen in an isolated
directory; (b) cleanup runs on success, exception, AND cancellation;
(c) artifacts persisted under the MAIN project's .sentinel/ survive
worktree cleanup; (d) orphan worktrees from crashed prior runs get
pruned at cycle start.
"""

from __future__ import annotations

import subprocess
from pathlib import Path  # noqa: TC003 — runtime use via tmp_path

import pytest

from sentinel.git_ops import branch_exists, current_branch, run_git
from sentinel.worktree import (
    WorktreeContext,
    cleanup_orphaned_worktrees,
    worktree_for,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with a single commit on `main`. Sentinel's
    worktree primitive needs a real repo to operate against; mocking
    git doesn't exercise the actual behaviors we care about."""
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path,
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path,
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path,
        check=True, capture_output=True,
    )
    (tmp_path / "README.md").write_text("# test repo\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path,
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=tmp_path,
        check=True, capture_output=True,
    )
    return tmp_path


class TestWorktreeLifecycle:
    @pytest.mark.asyncio
    async def test_creates_and_cleans_up(self, repo: Path) -> None:
        wt_path = repo / ".sentinel" / "worktrees" / "wi-1"
        async with worktree_for(
            repo, branch="sentinel/wi-1-test", base="main", slug="wi-1",
        ) as ctx:
            assert isinstance(ctx, WorktreeContext)
            assert ctx.path == wt_path
            assert ctx.path.exists()
            assert (ctx.path / "README.md").exists()
            assert ctx.branch == "sentinel/wi-1-test"
            assert ctx.base == "main"
            # Branch is checked out in the worktree, not the main repo
            assert current_branch(ctx.path) == "sentinel/wi-1-test"
            assert current_branch(repo) == "main"
        # After exit: worktree dir is gone
        assert not wt_path.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_exception(self, repo: Path) -> None:
        """Exception inside the context must not leak a worktree."""
        wt_path = repo / ".sentinel" / "worktrees" / "wi-2"
        with pytest.raises(RuntimeError, match="simulated"):
            async with worktree_for(
                repo, branch="sentinel/wi-2-test", base="main", slug="wi-2",
            ):
                assert wt_path.exists()
                raise RuntimeError("simulated failure")
        assert not wt_path.exists()

    @pytest.mark.asyncio
    async def test_cleanup_when_worktree_is_dirty(self, repo: Path) -> None:
        """If Coder leaves uncommitted changes (failure mid-execution),
        cleanup must still succeed via --force."""
        async with worktree_for(
            repo, branch="sentinel/wi-3-test", base="main", slug="wi-3",
        ) as ctx:
            (ctx.path / "new-file.txt").write_text("dirty\n")
        assert not (repo / ".sentinel" / "worktrees" / "wi-3").exists()

    @pytest.mark.asyncio
    async def test_artifacts_dir_is_main_project_not_worktree(
        self, repo: Path,
    ) -> None:
        """The crucial separation: artifacts_dir is absolute under
        MAIN project, not inside the worktree, so transcripts written
        there survive worktree cleanup."""
        async with worktree_for(
            repo, branch="sentinel/wi-4-test", base="main", slug="wi-4",
        ) as ctx:
            assert ctx.artifacts_dir == repo / ".sentinel"
            assert ctx.artifacts_dir != ctx.path
            # Write a fake artifact (simulating Coder transcript)
            (ctx.artifacts_dir / "executions").mkdir(parents=True, exist_ok=True)
            (ctx.artifacts_dir / "executions" / "wi-4.md").write_text("transcript\n")
        # After cleanup: artifact still exists
        assert (repo / ".sentinel" / "executions" / "wi-4.md").exists()

    @pytest.mark.asyncio
    async def test_branch_persists_after_cleanup(self, repo: Path) -> None:
        """Cleanup removes the worktree dir but leaves the branch —
        a just-shipped PR may point to it."""
        async with worktree_for(
            repo, branch="sentinel/wi-5-test", base="main", slug="wi-5",
        ):
            pass
        assert branch_exists(repo, "sentinel/wi-5-test")

    @pytest.mark.asyncio
    async def test_reuses_existing_branch(self, repo: Path) -> None:
        """When a prior cycle left a branch (e.g., push succeeded but
        PR creation failed), the next worktree_for on that branch
        should check it out, not fail."""
        # First cycle creates the branch
        async with worktree_for(
            repo, branch="sentinel/wi-6-test", base="main", slug="wi-6",
        ) as ctx:
            (ctx.path / "work.txt").write_text("progress\n")
            run_git(["add", "work.txt"], ctx.path, check=True)
            run_git(["commit", "-m", "wip"], ctx.path, check=True)
        assert branch_exists(repo, "sentinel/wi-6-test")

        # Second cycle resumes it
        async with worktree_for(
            repo, branch="sentinel/wi-6-test", base="main", slug="wi-6",
        ) as ctx:
            # The prior commit should be there (branch reuse, not re-create)
            assert (ctx.path / "work.txt").exists()


class TestOrphanCleanup:
    def test_removes_leftover_sentinel_worktrees(self, repo: Path) -> None:
        """A worktree created by a prior crashed run must be pruned at
        cycle start so `git worktree add` on the same path works next
        time."""
        wt_path = repo / ".sentinel" / "worktrees" / "wi-orphan"
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        run_git(
            ["worktree", "add", "-b", "sentinel/orphan", str(wt_path), "main"],
            repo, check=True,
        )
        assert wt_path.exists()

        removed = cleanup_orphaned_worktrees(repo)
        assert removed == 1
        assert not wt_path.exists()

    def test_leaves_user_worktrees_alone(self, repo: Path, tmp_path: Path) -> None:
        """Cleanup must only touch worktrees under
        .sentinel/worktrees/ — a user's own worktree elsewhere is
        their business."""
        user_wt = tmp_path / "user-worktree"
        run_git(
            ["worktree", "add", "-b", "user/feature", str(user_wt), "main"],
            repo, check=True,
        )
        try:
            removed = cleanup_orphaned_worktrees(repo)
            assert removed == 0
            assert user_wt.exists()
        finally:
            run_git(
                ["worktree", "remove", "--force", str(user_wt)], repo,
                check=False,
            )

    def test_no_worktrees_no_crash(self, repo: Path) -> None:
        """Fresh project: cleanup is a no-op, returns 0."""
        assert cleanup_orphaned_worktrees(repo) == 0
