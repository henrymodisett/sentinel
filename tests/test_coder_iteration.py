"""Tests for the coder↔reviewer iteration loop.

The point of Sentinel is to ship impactful work, not produce rejected
commits on unmerged branches. Dogfood on portfolio_new (2026-04-17)
exposed that a single reviewer pass wipes partial real work: 2/2 items
were rejected for fixable coder-quality issues (invalid CSS, incomplete
scope). The iteration loop turns those rejections into either merged
PRs (coder addresses feedback) or clean failures (coder can't make
progress) — never silent rot.

These tests cover:
  - Approve on second pass → iterations_used=2, review.verdict='approved'
  - Approve on third pass → iterations_used=3 (covers N-1 revisions)
  - Max iterations exceeded → iterations_used=MAX, final verdict not approved
  - No-progress detection → stop early when two rounds produce identical findings
  - Coder failure mid-loop → break out, surface the error
  - `review_feedback` is threaded into Coder.execute → prompt uses REVISE_PROMPT
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — runtime use in fixtures
from unittest.mock import AsyncMock, MagicMock

import pytest

from sentinel.cli.work_cmd import (
    MAX_CODER_ITERATIONS,
    _issue_set,
    _iterate_coder_reviewer,
)
from sentinel.roles.coder import REVISE_PROMPT, Coder, ExecutionResult
from sentinel.roles.planner import WorkItem
from sentinel.roles.reviewer import ReviewResult


@dataclass
class FakeCtx:
    path: Path
    branch: str
    base: str = "main"


def _make_work_item() -> WorkItem:
    return WorkItem(
        id="t1", title="add accessibility labels",
        description="Work item description.",
        type="fix", priority="high", complexity=2,
        acceptance_criteria=["All interactive elements have labels"],
        files=["src/a.tsx"],
    )


def _make_exec_result(*, status: str = "partial", cost: float = 0.1) -> ExecutionResult:
    r = ExecutionResult(
        work_item_id="t1",
        status=status,
        branch="sentinel/wi-t1",
        files_changed=["src/a.tsx"],
        tests_passing=True,
        commit_sha="abc123",
        cost_usd=cost,
    )
    return r


def _review(
    verdict: str, issues: list[str] | None = None, cost: float = 0.05,
) -> ReviewResult:
    return ReviewResult(
        work_item_id="t1",
        verdict=verdict,  # type: ignore[arg-type]
        summary="",
        blocking_issues=list(issues or []),
        cost_usd=cost,
    )


class TestIssueSetNormalization:
    def test_order_independent(self) -> None:
        assert _issue_set(["a", "b"]) == _issue_set(["b", "a"])

    def test_strips_whitespace(self) -> None:
        assert _issue_set([" a ", "b"]) == _issue_set(["a", "b"])

    def test_drops_empty_entries(self) -> None:
        assert _issue_set(["a", "", "  "]) == _issue_set(["a"])


class TestIterateCoderReviewer:
    @pytest.fixture
    def ctx(self, tmp_path: Path) -> FakeCtx:
        return FakeCtx(path=tmp_path, branch="sentinel/wi-t1")

    @pytest.mark.asyncio
    async def test_approve_on_second_pass(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        """The most valuable case: first pass has a real finding, coder
        addresses it, second pass ships."""
        work_item = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("changes-requested", ["missing aria-label"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        reviewer = MagicMock()
        reviewer.review = AsyncMock(return_value=_review("approved"))

        exec_r, review, iters = await _iterate_coder_reviewer(
            work_item=work_item,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder,
            reviewer=reviewer,
            project=tmp_path,
            ctx=ctx,
        )

        assert review.verdict == "approved"
        assert iters == 2
        # Coder was called once for the revision (initial call happened
        # outside the helper).
        assert coder.execute.call_count == 1
        # review_feedback was threaded through so Coder uses REVISE_PROMPT
        call_kwargs = coder.execute.call_args.kwargs
        assert call_kwargs["review_feedback"] is initial_review

    @pytest.mark.asyncio
    async def test_approve_on_third_pass(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        """Two revisions allowed — first finding gets addressed, second
        pass surfaces a new finding, third pass ships. Each round's
        findings must differ from the last (otherwise no-progress
        guard kicks in)."""
        work_item = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("rejected", ["issue A"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        reviewer = MagicMock()
        reviewer.review = AsyncMock(side_effect=[
            _review("changes-requested", ["issue B"]),  # after 1st revise
            _review("approved"),                         # after 2nd revise
        ])

        exec_r, review, iters = await _iterate_coder_reviewer(
            work_item=work_item,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
        )

        assert review.verdict == "approved"
        assert iters == 3
        assert coder.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_max_iterations_caps_the_loop(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        """If the coder never reaches 'approved', the loop must cap at
        MAX_CODER_ITERATIONS — runaway cost is a deal-breaker."""
        work_item = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("rejected", ["issue A"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        # Reviewer keeps finding NEW issues each round so the no-progress
        # guard doesn't fire before the cap does.
        reviewer = MagicMock()
        reviewer.review = AsyncMock(side_effect=[
            _review("changes-requested", ["issue B"]),
            _review("changes-requested", ["issue C"]),
            # Should never be called — cap hit
        ])

        exec_r, review, iters = await _iterate_coder_reviewer(
            work_item=work_item,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
        )

        assert iters == MAX_CODER_ITERATIONS
        assert review.verdict != "approved"
        assert coder.execute.call_count == MAX_CODER_ITERATIONS - 1

    @pytest.mark.asyncio
    async def test_no_progress_stops_early(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        """If the reviewer returns the same blocking_issues two rounds
        in a row, the coder isn't responding to feedback — stop
        spending budget. Cheaper than waiting for the cap."""
        work_item = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("changes-requested", ["issue A"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        reviewer = MagicMock()
        reviewer.review = AsyncMock(side_effect=[
            _review("changes-requested", ["issue A"]),  # identical — bail
            _review("approved"),  # should never run
        ])

        exec_r, review, iters = await _iterate_coder_reviewer(
            work_item=work_item,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
        )

        # Entered the loop once (iter 2), reviewer said same thing, bail
        # before running coder a third time.
        assert iters == 2
        assert review.verdict != "approved"
        assert coder.execute.call_count == 1  # only the 2nd-iter revise

    @pytest.mark.asyncio
    async def test_no_progress_ignores_ordering_and_whitespace(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        """Same issues in a different order/whitespace should count as
        no progress — the comparison must be set-based, not list."""
        work_item = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("rejected", ["issue A", "issue B"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        reviewer = MagicMock()
        reviewer.review = AsyncMock(return_value=_review(
            "rejected", ["  issue B", "issue A"],  # same findings, reshuffled
        ))

        exec_r, review, iters = await _iterate_coder_reviewer(
            work_item=work_item,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
        )
        assert iters == 2  # initial + one revision, then stuck

    @pytest.mark.asyncio
    async def test_coder_failure_breaks_loop(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        """Coder returning status='failed' mid-loop must break
        iteration. Continuing would just burn more budget on a
        non-working coder."""
        work_item = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("rejected", ["issue A"])

        failed_exec = _make_exec_result(status="failed")
        failed_exec.error = "agentic coder died"

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=failed_exec)

        reviewer = MagicMock()
        reviewer.review = AsyncMock()

        exec_r, review, iters = await _iterate_coder_reviewer(
            work_item=work_item,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
        )
        assert exec_r.status == "failed"
        assert coder.execute.call_count == 1
        assert reviewer.review.call_count == 0  # bailed before re-review


class TestRevisePrompt:
    """Coder.execute with review_feedback must build from REVISE_PROMPT,
    not BUILD_PROMPT — the coder needs to know it's iterating and what
    the blocking issues are."""

    def test_revise_prompt_includes_blocking_issues(self) -> None:
        # Render the template directly — the full Coder.execute path
        # requires a live provider; templating logic is what we care
        # about here.
        rendered = REVISE_PROMPT.format(
            project_name="demo",
            title="fix things",
            type="fix", priority="high", complexity=2,
            description="desc",
            criteria="- must work",
            files="a.py",
            verdict="changes-requested",
            blocking_issues="- fix aria-label\n- correct CSS @media syntax",
            non_blocking="",
        )
        assert "fix aria-label" in rendered
        assert "correct CSS @media syntax" in rendered
        assert "REVISING" in rendered  # signal to the coder it's iterating
        assert "changes-requested" in rendered  # the actual prior verdict
