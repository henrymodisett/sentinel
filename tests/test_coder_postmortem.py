"""Tests for the configurable iteration limit + exhaustion post-mortem.

Cycle 5 of the autumn-mail dogfood (Finding F8) hit `coder iterations:
3/3` with the reviewer still emitting `changes-requested`, and sentinel
moved on silently. The user had to dig into `.sentinel/reviews/` to
reconstruct what went wrong.

These tests cover:
  - Configurable iteration cap: `max_iterations` from config is honored
    (default 3, configurable up to 10).
  - Exhaustion post-mortem: prints to stdout AND persists to
    `.sentinel/exhaustions/<timestamp>-<slug>.md` with the right body.
  - No-progress post-mortem: identical findings two rounds in a row
    triggers the no-progress variant of the post-mortem.
  - Backward compat: callers that don't pass `max_iterations` get the
    legacy MAX_CODER_ITERATIONS default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock

import pytest

from sentinel.cli.work_cmd import (
    MAX_CODER_ITERATIONS,
    _format_exhaustion_postmortem,
    _iterate_coder_reviewer,
    _persist_exhaustion,
)
from sentinel.roles.coder import Coder, ExecutionResult
from sentinel.roles.planner import WorkItem
from sentinel.roles.reviewer import ReviewResult


@dataclass
class FakeCtx:
    path: Path
    branch: str
    base: str = "main"


def _make_work_item() -> WorkItem:
    return WorkItem(
        id="t1",
        title="add accessibility labels",
        description="Work item description.",
        type="fix",
        priority="high",
        complexity=2,
        acceptance_criteria=["All interactive elements have labels"],
        files=["src/a.tsx"],
    )


def _make_exec_result(*, status: str = "partial", cost: float = 0.1) -> ExecutionResult:
    return ExecutionResult(
        work_item_id="t1",
        status=status,
        branch="sentinel/wi-t1",
        files_changed=["src/a.tsx"],
        tests_passing=True,
        commit_sha="abc123",
        cost_usd=cost,
    )


def _review(
    verdict: str,
    issues: list[str] | None = None,
    cost: float = 0.05,
    *,
    summary: str = "",
    infrastructure_failure: bool = False,
) -> ReviewResult:
    return ReviewResult(
        work_item_id="t1",
        verdict=verdict,  # type: ignore[arg-type]
        summary=summary,
        blocking_issues=list(issues or []),
        cost_usd=cost,
        infrastructure_failure=infrastructure_failure,
    )


# ---------------------------------------------------------------------------
# Post-mortem formatting
# ---------------------------------------------------------------------------


class TestFormatExhaustionPostmortem:
    def test_exhausted_includes_iteration_counts_and_branch(self) -> None:
        wi = _make_work_item()
        review = _review("changes-requested", ["fix the thing", "another"])
        body = _format_exhaustion_postmortem(
            work_item=wi,
            branch="sentinel/wi-t1-add-accessibility-labels",
            iterations=3,
            max_iterations=3,
            review=review,
            reason="exhausted",
        )
        # Banner + structured fields.
        assert "Coder iterations exhausted" in body
        assert "add accessibility labels" in body
        assert "sentinel/wi-t1-add-accessibility-labels" in body
        assert "3/3" in body
        assert "changes-requested" in body
        # Findings rendered as a bulleted block.
        assert "fix the thing" in body
        assert "another" in body
        # Suggested next steps point the user to actionable recovery
        # paths — both manual fix-up and rejection.
        assert "git checkout" in body
        assert "scripts/open-pr.sh" in body
        assert "Reject this proposal" in body

    def test_no_progress_uses_distinct_banner(self) -> None:
        wi = _make_work_item()
        review = _review("rejected", ["same finding"])
        body = _format_exhaustion_postmortem(
            work_item=wi, branch="b", iterations=2, max_iterations=3,
            review=review, reason="no_progress",
        )
        assert "No progress" in body
        # The no-progress variant still gives actionable next steps.
        assert "git checkout" in body
        assert "split" in body.lower()

    def test_summary_included_when_present(self) -> None:
        wi = _make_work_item()
        review = _review(
            "changes-requested", ["x"], summary="Tests fail; aria-labels missing.",
        )
        body = _format_exhaustion_postmortem(
            work_item=wi, branch="b", iterations=3, max_iterations=3,
            review=review,
        )
        assert "aria-labels missing" in body

    def test_no_findings_renders_placeholder(self) -> None:
        """When `blocking_issues` is empty, the post-mortem still has
        to be informative — fall back to a placeholder so the section
        doesn't render as a blank ``Last reviewer findings:`` line."""
        wi = _make_work_item()
        review = _review("changes-requested", [])
        body = _format_exhaustion_postmortem(
            work_item=wi, branch="b", iterations=3, max_iterations=3,
            review=review,
        )
        assert "no blocking_issues recorded" in body


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistExhaustion:
    def test_writes_to_sentinel_exhaustions_dir(self, tmp_path: Path) -> None:
        wi = _make_work_item()
        body = "## Sample post-mortem body\n\n  - finding 1\n  - finding 2\n"
        path = _persist_exhaustion(tmp_path, work_item=wi, body=body)

        assert path is not None
        assert path.exists()
        # Must live under .sentinel/exhaustions/
        assert path.parent == tmp_path / ".sentinel" / "exhaustions"
        # Filename contains a timestamp + work-item slug.
        assert path.suffix == ".md"
        assert "add-accessibility-labels" in path.name
        # Body is preserved verbatim.
        contents = path.read_text(encoding="utf-8")
        assert "Sample post-mortem body" in contents
        assert "finding 1" in contents
        # Header includes the work-item ID for grep-from-jsonl pivots.
        assert "t1" in contents


# ---------------------------------------------------------------------------
# Loop integration: iteration cap + post-mortem firing
# ---------------------------------------------------------------------------


class TestIterationCapWithConfig:
    @pytest.fixture
    def ctx(self, tmp_path: Path) -> FakeCtx:
        return FakeCtx(path=tmp_path, branch="sentinel/wi-t1")

    @pytest.mark.asyncio
    async def test_max_iterations_2_caps_at_2(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        """Spec acceptance: cycle with `max_iterations=2` and a
        reviewer mock that always says `changes-requested` runs exactly
        2 iterations and fires the exhaustion post-mortem."""
        wi = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("changes-requested", ["finding 0"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        reviewer = MagicMock()
        # Use NEW findings each round to avoid the no-progress short-circuit
        # (so we test the cap path, not no-progress).
        reviewer.review = AsyncMock(side_effect=[
            _review("changes-requested", ["finding 1"]),
            _review("changes-requested", ["finding 2"]),
            # Should never be called — capped at 2
        ])

        exec_r, review, iters = await _iterate_coder_reviewer(
            work_item=wi,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
            max_iterations=2,
        )

        # Initial pass + 1 revise = 2 total iterations
        assert iters == 2
        assert review.verdict != "approved"
        assert coder.execute.call_count == 1
        # Persisted post-mortem under .sentinel/exhaustions/
        exhaustions = tmp_path / ".sentinel" / "exhaustions"
        assert exhaustions.exists()
        files = list(exhaustions.glob("*.md"))
        assert len(files) == 1
        body = files[0].read_text(encoding="utf-8")
        assert "Coder iterations exhausted" in body
        assert "2/2" in body

    @pytest.mark.asyncio
    async def test_default_uses_legacy_constant(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        """Backward compat: callers that don't pass `max_iterations`
        get MAX_CODER_ITERATIONS (3) — preserves the historical contract
        for tests and any out-of-tree callers."""
        wi = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("changes-requested", ["a"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        reviewer = MagicMock()
        reviewer.review = AsyncMock(side_effect=[
            _review("changes-requested", ["b"]),
            _review("changes-requested", ["c"]),
        ])

        exec_r, review, iters = await _iterate_coder_reviewer(
            work_item=wi,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
        )
        # The legacy default is 3 — the helper caps at exactly that
        # without any new kwarg passed.
        assert iters == MAX_CODER_ITERATIONS == 3

    @pytest.mark.asyncio
    async def test_max_iterations_higher_than_default(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        """Allows projects that need more iterations to opt in (capped
        in the schema at 10)."""
        wi = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("changes-requested", ["finding 0"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        # Distinct findings each round; reviewer side_effect provides 4.
        reviewer = MagicMock()
        reviewer.review = AsyncMock(side_effect=[
            _review("changes-requested", [f"f{i}"]) for i in range(1, 5)
        ])

        _, _, iters = await _iterate_coder_reviewer(
            work_item=wi,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
            max_iterations=5,
        )
        assert iters == 5


class TestNoProgressPostmortem:
    """No-progress detection: if findings don't change between iterations,
    bail early with the no-progress variant of the post-mortem.

    Spec acceptance: 'mock 3 iterations of identical findings → bail at
    iteration 2 with the no-progress message.'
    """

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> FakeCtx:
        return FakeCtx(path=tmp_path, branch="sentinel/wi-t1")

    @pytest.mark.asyncio
    async def test_identical_findings_triggers_no_progress(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        wi = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("changes-requested", ["IDENTICAL"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        # Three iterations of identical findings — we must bail at
        # iteration 2 (where the duplicate is detected).
        reviewer = MagicMock()
        reviewer.review = AsyncMock(side_effect=[
            _review("changes-requested", ["IDENTICAL"]),
            # Should never be called — no_progress fires first
            _review("changes-requested", ["IDENTICAL"]),
        ])

        _, review, iters = await _iterate_coder_reviewer(
            work_item=wi,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
            max_iterations=5,  # cap is high; no-progress should fire first
        )

        assert iters == 2
        assert review.verdict != "approved"
        # Post-mortem fired with the no-progress banner.
        exhaustions = tmp_path / ".sentinel" / "exhaustions"
        files = list(exhaustions.glob("*.md")) if exhaustions.exists() else []
        assert len(files) == 1, f"expected 1 post-mortem, got {files}"
        body = files[0].read_text(encoding="utf-8")
        assert "No progress" in body
        # The shared findings appear in the post-mortem so the operator
        # can see what the coder couldn't address.
        assert "IDENTICAL" in body


class TestPostmortemDoesNotFireOnApproval:
    """Sanity check: the post-mortem MUST NOT fire when the loop ends
    with an approval — that would be misleading noise."""

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> FakeCtx:
        return FakeCtx(path=tmp_path, branch="sentinel/wi-t1")

    @pytest.mark.asyncio
    async def test_approval_skips_postmortem(
        self, ctx: FakeCtx, tmp_path: Path,
    ) -> None:
        wi = _make_work_item()
        initial_exec = _make_exec_result()
        initial_review = _review("changes-requested", ["fix me"])

        coder = MagicMock(spec=Coder)
        coder.execute = AsyncMock(return_value=_make_exec_result())

        reviewer = MagicMock()
        reviewer.review = AsyncMock(return_value=_review("approved"))

        _, review, _ = await _iterate_coder_reviewer(
            work_item=wi,
            exec_result=initial_exec,
            review=initial_review,
            coder=coder, reviewer=reviewer,
            project=tmp_path, ctx=ctx,
            max_iterations=3,
        )

        assert review.verdict == "approved"
        # No exhaustions directory should have been created.
        exhaustions = tmp_path / ".sentinel" / "exhaustions"
        assert not exhaustions.exists() or not list(exhaustions.glob("*.md"))


# ---------------------------------------------------------------------------
# Config schema validation
# ---------------------------------------------------------------------------


class TestCoderMaxIterationsConfig:
    def test_default_is_3(self) -> None:
        from sentinel.config.schema import CoderConfig
        assert CoderConfig().max_iterations == 3

    def test_accepts_higher_values(self) -> None:
        from sentinel.config.schema import CoderConfig
        cfg = CoderConfig(max_iterations=5)
        assert cfg.max_iterations == 5

    def test_rejects_zero(self) -> None:
        """Zero iterations would mean "don't even run the initial pass"
        — meaningless for a work item we already chose to execute."""
        from pydantic import ValidationError

        from sentinel.config.schema import CoderConfig
        with pytest.raises(ValidationError):
            CoderConfig(max_iterations=0)

    def test_rejects_above_cap(self) -> None:
        """Schema enforces max=10 to prevent runaway-cost misconfig."""
        from pydantic import ValidationError

        from sentinel.config.schema import CoderConfig
        with pytest.raises(ValidationError):
            CoderConfig(max_iterations=999)


class TestCliHelpAllowlistValidation:
    """Schema-level checks for the F7 allowlist (security-relevant)."""

    def test_default_includes_documented_tools(self) -> None:
        from sentinel.config.schema import CoderConfig
        cfg = CoderConfig()
        assert "gws" in cfg.cli_help_allowlist
        assert "swift" in cfg.cli_help_allowlist
        assert "pytest" in cfg.cli_help_allowlist

    def test_empty_allowlist_disables_feature(self) -> None:
        from sentinel.config.schema import CoderConfig
        cfg = CoderConfig(cli_help_allowlist=[])
        assert cfg.cli_help_allowlist == []

    def test_rejects_shell_meta_chars(self) -> None:
        """A user-supplied allowlist entry with shell metas (`;`, `&`,
        space, `/`, `..`) must be rejected. Defense in depth — even
        though `subprocess.run([...])` is shell-meta-safe, a weird
        entry like 'rm -rf /' would still try to invoke a binary
        literally named that, failing later in the pipeline at a
        confusing place."""
        from pydantic import ValidationError

        from sentinel.config.schema import CoderConfig
        with pytest.raises(ValidationError):
            CoderConfig(cli_help_allowlist=["rm -rf /"])
        with pytest.raises(ValidationError):
            CoderConfig(cli_help_allowlist=["foo; bar"])
        with pytest.raises(ValidationError):
            CoderConfig(cli_help_allowlist=["../bin/foo"])

    def test_drops_blank_entries(self) -> None:
        """Toml authoring often produces stray blank list entries —
        treat them as absent rather than failing the whole config."""
        from sentinel.config.schema import CoderConfig
        cfg = CoderConfig(cli_help_allowlist=["gws", "", "  ", "swift"])
        assert cfg.cli_help_allowlist == ["gws", "swift"]
