"""Tests for `src/sentinel/git_ops.py`.

Focus: `run_git_with_precommit_recovery` — the shared wrapper that
auto-retries when a globally-installed `pre-commit` hook aborts a git
operation in a repo with no `.pre-commit-config.yaml`. Dogfood on
portfolio_new surfaced this pattern on both commit (fixed in #61) and
push (this wrapper's reason for existing).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from sentinel.git_ops import (
    _is_missing_precommit_config_error,
    _precommit_config_absent_from_repo,
    run_git_with_precommit_recovery,
)


def _init_bare_remote(tmpdir: str) -> str:
    """Create a bare repo to act as `origin` for push tests."""
    remote = Path(tmpdir) / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main"],
        cwd=remote, check=True,
    )
    return str(remote)


def _init_work_repo_with_fake_hook(
    tmpdir: str, hook_name: str,
) -> None:
    """Set up a repo whose `.git/hooks/<hook_name>` mirrors the real
    pre-commit tool's missing-config behavior: exit 1 with the
    signature message unless PRE_COMMIT_ALLOW_NO_CONFIG=1."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.io"], cwd=tmpdir, check=True,
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmpdir, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=tmpdir, check=True,
    )
    hook = Path(tmpdir) / ".git" / "hooks" / hook_name
    hook.write_text(
        "#!/bin/sh\n"
        'if [ -z "$PRE_COMMIT_ALLOW_NO_CONFIG" ]; then\n'
        '  echo "No .pre-commit-config.yaml file was found" >&2\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )
    hook.chmod(0o755)


class TestDetectMissingPreCommitConfig:
    def test_matches_real_signature(self) -> None:
        stderr = (
            "No .pre-commit-config.yaml file was found\n"
            "- To temporarily silence this, run `PRE_COMMIT_ALLOW_NO_CONFIG=1`"
        )
        assert _is_missing_precommit_config_error(stderr, "") is True

    def test_ignores_unrelated_commit_failures(self) -> None:
        stderr = "Your branch is ahead of 'origin/main' by 3 commits."
        assert _is_missing_precommit_config_error(stderr, "") is False


class TestPreCommitConfigAbsenceCheck:
    def test_absent_when_neither_tree_nor_head_has_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _init_work_repo_with_fake_hook(tmpdir, "pre-commit")
            assert _precommit_config_absent_from_repo(tmpdir) is True

    def test_present_when_in_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _init_work_repo_with_fake_hook(tmpdir, "pre-commit")
            (Path(tmpdir) / ".pre-commit-config.yaml").write_text("")
            assert _precommit_config_absent_from_repo(tmpdir) is False

    def test_present_when_tracked_in_head_even_if_deleted_in_tree(
        self,
    ) -> None:
        """Prevents the bypass from firing when a coder deletes the
        tracked config as part of its change — that's a real hook
        failure we must surface, not an environment mismatch."""
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            _init_work_repo_with_fake_hook(tmpdir, "pre-commit")
            config = Path(tmpdir) / ".pre-commit-config.yaml"
            config.write_text("# real\n")
            subprocess.run(
                ["git", "add", ".pre-commit-config.yaml"],
                cwd=tmpdir, check=True,
            )
            env = {**os.environ, "PRE_COMMIT_ALLOW_NO_CONFIG": "1"}
            subprocess.run(
                ["git", "commit", "-m", "add config"],
                cwd=tmpdir, check=True, env=env,
            )
            config.unlink()
            # Absent from tree, but HEAD still has it — must return False
            assert _precommit_config_absent_from_repo(tmpdir) is False


class TestRunGitWithPreCommitRecoveryCommit:
    def test_retries_commit_when_config_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _init_work_repo_with_fake_hook(tmpdir, "pre-commit")
            (Path(tmpdir) / "f.py").write_text("x\n")
            subprocess.run(["git", "add", "f.py"], cwd=tmpdir, check=True)

            result = run_git_with_precommit_recovery(
                ["commit", "-m", "real"], tmpdir,
            )
            assert result.returncode == 0, result.stderr


class TestRunGitWithPreCommitRecoveryPush:
    """Regression for dogfood 2026-04-17 PM: pre-commit also installs a
    pre-push hook. After fixing the commit path, push still aborted
    with the same signature. The shared wrapper handles both."""

    def test_retries_push_when_config_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            remote = _init_bare_remote(tmpdir)
            work = Path(tmpdir) / "work"
            work.mkdir()
            _init_work_repo_with_fake_hook(str(work), "pre-push")
            subprocess.run(
                ["git", "remote", "add", "origin", remote],
                cwd=work, check=True,
            )

            result = run_git_with_precommit_recovery(
                ["push", "-u", "origin", "main"], str(work),
            )
            assert result.returncode == 0, (
                f"push should recover from missing pre-commit config; "
                f"stderr={result.stderr!r}"
            )

            # Verify the ref actually landed on the remote
            show = subprocess.run(
                ["git", "-C", remote, "show-ref", "refs/heads/main"],
                capture_output=True, text=True,
            )
            assert show.returncode == 0


class TestRunGitWithPreCommitRecoveryPassthrough:
    def test_real_hook_rejection_is_surfaced(self) -> None:
        """If the repo HAS a `.pre-commit-config.yaml`, a hook failure
        is real signal and must pass through unmodified — the wrapper
        must NOT bypass the hook just because the error string matches."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _init_work_repo_with_fake_hook(tmpdir, "pre-commit")
            (Path(tmpdir) / ".pre-commit-config.yaml").write_text("# real\n")
            (Path(tmpdir) / "f.py").write_text("x\n")
            subprocess.run(
                ["git", "add", "f.py", ".pre-commit-config.yaml"],
                cwd=tmpdir, check=True,
            )

            result = run_git_with_precommit_recovery(
                ["commit", "-m", "real"], tmpdir,
            )
            # Hook still fails (config present means the guard skips
            # retry) and the caller receives the non-zero result.
            assert result.returncode != 0

    def test_success_on_first_try_is_not_retried(self) -> None:
        """No recovery path should fire when git already succeeded —
        wrapper must be a no-op on the happy path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                ["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "t@t.io"],
                cwd=tmpdir, check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "t"], cwd=tmpdir, check=True,
            )
            result = run_git_with_precommit_recovery(
                ["commit", "--allow-empty", "-m", "init"], tmpdir,
            )
            assert result.returncode == 0
