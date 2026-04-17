"""Tests for the PR-shipping primitive.

ship_pr is a subprocess wrapper — gh and git are mocked. The behaviors
that matter are:
  - idempotency on already-open PR
  - explicit --head / --base / --body-file / --match-head-commit
  - auto-merge armed only when base branch has required checks
  - failed push surfaces as "failed" with stderr
  - PR body goes through a file (never --body)
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path  # noqa: TC003 — runtime via tmp_path
from unittest.mock import patch

from sentinel.pr import ShipResult, ship_pr


def _push_ok(*args, **kwargs):  # noqa: ANN001, ANN202
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _push_fail(*args, **kwargs):  # noqa: ANN001, ANN202
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="permission denied",
    )


def _gh_factory(behaviors: list):  # noqa: ANN001, ANN202
    """Build a fake _gh that returns the next CompletedProcess in `behaviors`,
    one per call. The list is the script."""
    iterator = iter(behaviors)

    def _fake_gh(args, cwd, *, check=False, timeout=60):  # noqa: ANN001, ANN202
        try:
            return next(iterator)
        except StopIteration as e:
            raise AssertionError(
                f"unexpected gh call beyond scripted behaviors: {args}",
            ) from e

    return _fake_gh


def _gh_result(stdout: str = "", stderr: str = "", returncode: int = 0):  # noqa: ANN001, ANN202
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class TestShipPRSuccessPaths:
    def test_creates_pr_and_arms_auto_merge_when_protected(
        self, tmp_path: Path,
    ) -> None:
        """The happy path: protected base → PR created → auto-merge armed."""
        gh_calls = []

        def fake_gh(args, cwd, *, check=False, timeout=60):  # noqa: ANN001, ANN202
            gh_calls.append(args)
            if args[0] == "pr" and args[1] == "list":
                return _gh_result(stdout="[]")
            if args[0] == "pr" and args[1] == "create":
                return _gh_result(stdout="https://github.com/x/y/pull/123\n")
            if args[0] == "api":
                # required_status_checks count = 2 → protected
                return _gh_result(stdout="2\n")
            if args[0] == "pr" and args[1] == "merge":
                return _gh_result(stdout="auto-merge enabled")
            raise AssertionError(f"unexpected gh args: {args}")

        with patch("sentinel.pr.run_git_with_precommit_recovery", side_effect=_push_ok), \
             patch("sentinel.pr._gh", side_effect=fake_gh):
            result = asyncio.run(ship_pr(
                worktree_path=tmp_path / "wt",
                project_path=tmp_path,
                branch="sentinel/wi-1",
                base="main",
                head_sha="abc123",
                title="fix: thing",
                body_md="## body\n",
            ))

        assert isinstance(result, ShipResult)
        assert result.status == "merged_armed"
        assert result.pr_url == "https://github.com/x/y/pull/123"

        # Verify create call has explicit flags
        create_args = next(a for a in gh_calls if a[:2] == ["pr", "create"])
        assert "--head" in create_args
        assert "sentinel/wi-1" in create_args
        assert "--base" in create_args
        assert "main" in create_args
        assert "--body-file" in create_args
        # No raw --body flag (codex-flagged risk: large bodies hit
        # shell argument limits and quoting issues)
        if "--body" in create_args:
            body_arg = create_args[create_args.index("--body") + 1]
            assert body_arg.endswith(".md")

        # Verify merge call uses --match-head-commit
        merge_args = next(a for a in gh_calls if a[:2] == ["pr", "merge"])
        assert "--match-head-commit" in merge_args
        assert "abc123" in merge_args
        assert "--auto" in merge_args
        assert "--squash" in merge_args

    def test_creates_pr_but_does_not_arm_when_unprotected(
        self, tmp_path: Path,
    ) -> None:
        """Unprotected base → PR created, auto-merge NOT armed.
        Otherwise the PR would merge instantly with no CI gate — exactly
        the failure mode codex flagged."""
        gh_calls = []

        def fake_gh(args, cwd, *, check=False, timeout=60):  # noqa: ANN001, ANN202
            gh_calls.append(args)
            if args[0] == "pr" and args[1] == "list":
                return _gh_result(stdout="[]")
            if args[0] == "pr" and args[1] == "create":
                return _gh_result(stdout="https://github.com/x/y/pull/77\n")
            if args[0] == "api":
                # No protection (404 surfaces as non-zero)
                return _gh_result(returncode=1, stderr="Not Found")
            raise AssertionError(f"unexpected gh args: {args}")

        with patch("sentinel.pr.run_git_with_precommit_recovery", side_effect=_push_ok), \
             patch("sentinel.pr._gh", side_effect=fake_gh):
            result = asyncio.run(ship_pr(
                worktree_path=tmp_path / "wt",
                project_path=tmp_path,
                branch="sentinel/wi-2",
                base="main",
                head_sha="def456",
                title="fix: another",
                body_md="## body\n",
            ))

        assert result.status == "created"
        assert result.pr_url == "https://github.com/x/y/pull/77"
        # `pr merge` must NOT have been called on an unprotected base
        merge_calls = [a for a in gh_calls if a[:2] == ["pr", "merge"]]
        assert merge_calls == []


class TestShipPRIdempotency:
    def test_existing_open_pr_returns_existed(self, tmp_path: Path) -> None:
        """If a prior cycle pushed the branch and created the PR but
        auto-merge couldn't be armed (or the cycle crashed before
        recording the URL), the next cycle's ship_pr must DISCOVER the
        existing PR via gh pr list and return "existed" — never
        create a duplicate."""
        gh_calls = []

        def fake_gh(args, cwd, *, check=False, timeout=60):  # noqa: ANN001, ANN202
            gh_calls.append(args)
            if args[0] == "pr" and args[1] == "list":
                return _gh_result(stdout=(
                    '[{"url": "https://github.com/x/y/pull/9", '
                    '"number": 9, "state": "OPEN"}]'
                ))
            raise AssertionError(
                f"ship_pr made gh calls beyond `pr list` after finding existing PR: {args}",
            )

        with patch("sentinel.pr.run_git_with_precommit_recovery", side_effect=_push_ok), \
             patch("sentinel.pr._gh", side_effect=fake_gh):
            result = asyncio.run(ship_pr(
                worktree_path=tmp_path / "wt",
                project_path=tmp_path,
                branch="sentinel/wi-resume",
                base="main",
                head_sha="aaa",
                title="resumed",
                body_md="body",
            ))

        assert result.status == "existed"
        assert result.pr_url == "https://github.com/x/y/pull/9"
        # No `pr create` should happen — that's the whole point
        create_calls = [a for a in gh_calls if a[:2] == ["pr", "create"]]
        assert create_calls == []


class TestShipPRFailurePaths:
    def test_push_failure_surfaces_stderr(self, tmp_path: Path) -> None:
        """A failed push must return status='failed' with the git
        stderr in `error` — silent failure is the worst kind."""
        with patch("sentinel.pr.run_git_with_precommit_recovery", side_effect=_push_fail):
            result = asyncio.run(ship_pr(
                worktree_path=tmp_path / "wt",
                project_path=tmp_path,
                branch="b",
                base="main",
                head_sha="x",
                title="t",
                body_md="b",
            ))

        assert result.status == "failed"
        assert "permission denied" in result.error

    def test_create_failure_surfaces_stderr(self, tmp_path: Path) -> None:
        def fake_gh(args, cwd, *, check=False, timeout=60):  # noqa: ANN001, ANN202
            if args[0] == "pr" and args[1] == "list":
                return _gh_result(stdout="[]")
            if args[0] == "pr" and args[1] == "create":
                return _gh_result(returncode=1, stderr="API rate limit exceeded")
            raise AssertionError(f"unexpected gh args: {args}")

        with patch("sentinel.pr.run_git_with_precommit_recovery", side_effect=_push_ok), \
             patch("sentinel.pr._gh", side_effect=fake_gh):
            result = asyncio.run(ship_pr(
                worktree_path=tmp_path / "wt",
                project_path=tmp_path,
                branch="b",
                base="main",
                head_sha="x",
                title="t",
                body_md="b",
            ))

        assert result.status == "failed"
        assert "API rate limit" in result.error


class TestShipPRSafety:
    def test_body_passed_via_body_file_not_body(self, tmp_path: Path) -> None:
        """Codex flagged: large generated PR bodies hit shell argument
        and quoting limits with --body. Always use --body-file."""
        captured_args: list[list[str]] = []

        def fake_gh(args, cwd, *, check=False, timeout=60):  # noqa: ANN001, ANN202
            captured_args.append(list(args))
            if args[0] == "pr" and args[1] == "list":
                return _gh_result(stdout="[]")
            if args[0] == "pr" and args[1] == "create":
                return _gh_result(stdout="https://github.com/x/y/pull/1\n")
            if args[0] == "api":
                return _gh_result(returncode=1)  # unprotected, no merge
            raise AssertionError(f"unexpected gh args: {args}")

        huge_body = "# big body\n" + ("x" * 100_000)

        with patch("sentinel.pr.run_git_with_precommit_recovery", side_effect=_push_ok), \
             patch("sentinel.pr._gh", side_effect=fake_gh):
            asyncio.run(ship_pr(
                worktree_path=tmp_path / "wt",
                project_path=tmp_path,
                branch="b",
                base="main",
                head_sha="x",
                title="t",
                body_md=huge_body,
            ))

        create = next(a for a in captured_args if a[:2] == ["pr", "create"])
        assert "--body-file" in create
        # The body itself is never on the command line
        assert huge_body not in create

    def test_force_with_lease_used_for_push(self, tmp_path: Path) -> None:
        """A resumed cycle may need to update a previously-pushed
        branch. --force-with-lease lets us do that without overwriting
        remote work that wasn't there when we started."""
        captured: list[list[str]] = []

        def fake_run_git(args, cwd, *, check=False, timeout=30):  # noqa: ANN001, ANN202
            captured.append(list(args))
            return _push_ok()

        def fake_gh(args, cwd, *, check=False, timeout=60):  # noqa: ANN001, ANN202
            if args[0] == "pr" and args[1] == "list":
                return _gh_result(stdout="[]")
            if args[0] == "pr" and args[1] == "create":
                return _gh_result(stdout="https://x.com/p/1\n")
            if args[0] == "api":
                return _gh_result(returncode=1)
            raise AssertionError(f"unexpected gh args: {args}")

        with patch("sentinel.pr.run_git_with_precommit_recovery", side_effect=fake_run_git), \
             patch("sentinel.pr._gh", side_effect=fake_gh):
            asyncio.run(ship_pr(
                worktree_path=tmp_path / "wt",
                project_path=tmp_path,
                branch="b",
                base="main",
                head_sha="x",
                title="t",
                body_md="b",
            ))

        push_call = next(a for a in captured if a[0] == "push")
        assert "--force-with-lease" in push_call
