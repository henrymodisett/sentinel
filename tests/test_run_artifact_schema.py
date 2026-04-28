"""Contract tests for the cycle artifact schema (sentinel#97).

These tests pin the stable interface that Touchstone and other consumers
depend on. If any assertion here breaks, a consumer contract has been violated.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path  # noqa: TC003 — runtime use via tmp_path

import pytest

from sentinel.journal import (
    DECISIONS_END,
    DECISIONS_START,
    PR_BODY_END,
    PR_BODY_START,
    SCHEMA_VERSION,
    TRANSCRIPT_END,
    TRANSCRIPT_START,
    Journal,
    ProviderCall,
    WorkItemRecord,
    render_frontmatter,
)


def _journal(tmp_path: Path, **overrides) -> Journal:
    return Journal(
        project_path=tmp_path,
        project_name=overrides.get("project_name", "test-project"),
        branch=overrides.get("branch", "main"),
        budget_str=overrides.get("budget_str"),
        status=overrides.get("status", "completed"),
        cycle_id=overrides.get("cycle_id", "2026-04-28-120000"),
        run_id=overrides.get("run_id", "aaaabbbb-0000-0000-0000-000000000001"),
    )


def _extract_between(content: str, start_marker: str, end_marker: str) -> str:
    """Extract text between two HTML comment markers (mirrors Touchstone's consumer logic)."""
    pattern = re.escape(start_marker) + r"(.*?)" + re.escape(end_marker)
    m = re.search(pattern, content, re.DOTALL)
    assert m is not None, f"markers {start_marker!r} / {end_marker!r} not found in content"
    return m.group(1)


class TestSchemaVersion:
    def test_schema_version_present(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        content = j.write().read_text()
        assert f"schema-version: {SCHEMA_VERSION}" in content

    def test_schema_version_constant_is_1_0(self) -> None:
        assert SCHEMA_VERSION == "1.0"


class TestFrontmatter:
    def test_all_required_frontmatter_fields(self, tmp_path: Path) -> None:
        j = _journal(
            tmp_path,
            branch="feat/test",
            status="completed",
            cycle_id="2026-04-28-093000",
            run_id="deadbeef-0000-0000-0000-000000000001",
        )
        content = j.write().read_text()
        assert "schema-version:" in content
        assert "sentinel-run-id:" in content
        assert "timestamp:" in content
        assert "cycle-id:" in content
        assert "branch:" in content
        assert "status:" in content

    def test_frontmatter_values_match_journal(self, tmp_path: Path) -> None:
        run_id = "cafef00d-1234-5678-abcd-000000000001"
        j = _journal(
            tmp_path,
            branch="feat/schema",
            status="in-progress",
            cycle_id="my-cycle-slug",
            run_id=run_id,
        )
        content = j.write().read_text()
        assert f"sentinel-run-id: {run_id}" in content
        assert "cycle-id: my-cycle-slug" in content
        assert "branch: feat/schema" in content
        assert "status: in-progress" in content

    def test_frontmatter_delimited_by_triple_dash(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        content = j.write().read_text()
        # Standard YAML front-matter: starts with --- on its own line
        assert content.startswith("---\n")
        # Second --- closes it
        assert "\n---\n" in content

    def test_render_frontmatter_function_standalone(self) -> None:
        fm = render_frontmatter(
            run_id="abc-123",
            cycle_id="slug",
            branch="main",
            status="completed",
            timestamp=datetime(2026, 4, 28, 9, 0, 0),
        )
        assert "schema-version: 1.0" in fm
        assert "sentinel-run-id: abc-123" in fm
        assert "timestamp: 2026-04-28T09:00:00" in fm
        assert "cycle-id: slug" in fm
        assert "branch: main" in fm
        assert "status: completed" in fm


class TestStatusValidation:
    @pytest.mark.parametrize("status", ["completed", "in-progress", "failed", "blocked-on-human"])
    def test_valid_statuses_accepted(self, tmp_path: Path, status: str) -> None:
        j = _journal(tmp_path, status=status)
        content = j.write().read_text()
        assert f"status: {status}" in content

    def test_invalid_status_raises_value_error(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.status = "invalid"
        with pytest.raises(ValueError, match="invalid status"):
            j.write()

    def test_render_frontmatter_rejects_unknown_status(self) -> None:
        with pytest.raises(ValueError, match="invalid status"):
            render_frontmatter(run_id="x", cycle_id="y", branch="main", status="unknown")


class TestBodyAnchors:
    def test_pr_body_anchors_present(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        content = j.write().read_text()
        assert PR_BODY_START in content
        assert PR_BODY_END in content

    def test_decisions_anchors_present(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        content = j.write().read_text()
        assert DECISIONS_START in content
        assert DECISIONS_END in content

    def test_transcript_anchors_present(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        content = j.write().read_text()
        assert TRANSCRIPT_START in content
        assert TRANSCRIPT_END in content

    def test_all_six_anchor_markers_present(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        content = j.write().read_text()
        for marker in (
            PR_BODY_START,
            PR_BODY_END,
            DECISIONS_START,
            DECISIONS_END,
            TRANSCRIPT_START,
            TRANSCRIPT_END,
        ):
            assert marker in content, f"missing anchor: {marker!r}"

    def test_anchor_order(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        content = j.write().read_text()
        positions = {
            "pr_start": content.index(PR_BODY_START),
            "pr_end": content.index(PR_BODY_END),
            "dec_start": content.index(DECISIONS_START),
            "dec_end": content.index(DECISIONS_END),
            "tr_start": content.index(TRANSCRIPT_START),
            "tr_end": content.index(TRANSCRIPT_END),
        }
        assert positions["pr_start"] < positions["pr_end"]
        assert positions["pr_end"] < positions["dec_start"]
        assert positions["dec_start"] < positions["dec_end"]
        assert positions["dec_end"] < positions["tr_start"]
        assert positions["tr_start"] < positions["tr_end"]


class TestSectionContent:
    def test_pr_body_content_matches_input(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.pr_body = "Custom PR body content for Touchstone."
        content = j.write().read_text()
        between = _extract_between(content, PR_BODY_START, PR_BODY_END)
        assert "Custom PR body content for Touchstone." in between

    def test_decisions_content_matches_input(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.decisions = "Always validate schema before write."
        content = j.write().read_text()
        between = _extract_between(content, DECISIONS_START, DECISIONS_END)
        assert "Always validate schema before write." in between

    def test_transcript_content_matches_input(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.transcript = "Role-by-role transcript goes here."
        content = j.write().read_text()
        between = _extract_between(content, TRANSCRIPT_START, TRANSCRIPT_END)
        assert "Role-by-role transcript goes here." in between

    def test_empty_decisions_renders_empty_block(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        # decisions defaults to ""
        content = j.write().read_text()
        assert DECISIONS_START in content
        assert DECISIONS_END in content
        between = _extract_between(content, DECISIONS_START, DECISIONS_END)
        assert between.strip() == ""

    def test_assembled_pr_body_contains_project_info(self, tmp_path: Path) -> None:
        j = _journal(tmp_path, project_name="sentinel", branch="feat/x")
        content = j.write().read_text()
        between = _extract_between(content, PR_BODY_START, PR_BODY_END)
        assert "sentinel" in between
        assert "feat/x" in between

    def test_assembled_transcript_contains_provider_calls(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.record_provider_call(
            ProviderCall(
                phase="scan",
                provider="gemini",
                model="flash",
                latency_ms=100,
                cost_usd=0.001,
            )
        )
        content = j.write().read_text()
        between = _extract_between(content, TRANSCRIPT_START, TRANSCRIPT_END)
        assert "gemini" in between
        assert "```jsonl" in between

    def test_assembled_pr_body_contains_work_items(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        j.record_work_item(
            WorkItemRecord(
                work_item_id="wi-1",
                title="Ship the feature",
                coder_status="succeeded",
            )
        )
        content = j.write().read_text()
        between = _extract_between(content, PR_BODY_START, PR_BODY_END)
        assert "wi-1" in between
        assert "Ship the feature" in between


class TestAnchorRoundtrip:
    def test_pr_body_anchor_extraction_roundtrip(self, tmp_path: Path) -> None:
        """Contract-pinning test: write known content, extract via the same
        regex Touchstone uses, assert round-trip fidelity."""
        known_content = "## What shipped\n\n- wi-1: refactor auth\n- wi-2: add tests\n"
        j = _journal(tmp_path)
        j.pr_body = known_content
        content = j.write().read_text()

        extracted = _extract_between(content, PR_BODY_START, PR_BODY_END)
        assert known_content in extracted

    def test_decisions_anchor_extraction_roundtrip(self, tmp_path: Path) -> None:
        known = "Always run integration tests before shipping auth changes."
        j = _journal(tmp_path)
        j.decisions = known
        content = j.write().read_text()

        extracted = _extract_between(content, DECISIONS_START, DECISIONS_END)
        assert known in extracted

    def test_transcript_anchor_extraction_roundtrip(self, tmp_path: Path) -> None:
        known = "monitor: 3 calls, $0.01\ncoder: 5 calls, $0.50\n"
        j = _journal(tmp_path)
        j.transcript = known
        content = j.write().read_text()

        extracted = _extract_between(content, TRANSCRIPT_START, TRANSCRIPT_END)
        assert known in extracted


class TestBackwardCompatibilityPolicy:
    def test_v1_anchors_still_readable_alongside_hypothetical_v2_anchor(
        self,
        tmp_path: Path,
    ) -> None:
        """Additive-evolution policy: a future v2 artifact may add new anchors
        but must never remove v1 ones. This test documents that invariant by
        writing a file that contains both v1 anchors and a hypothetical v2
        anchor, then asserting v1 is still extractable."""
        v2_start = "<!-- review-summary-start -->"
        v2_end = "<!-- review-summary-end -->"

        # Simulate what a v2 writer would produce
        j = _journal(tmp_path)
        j.pr_body = "PR body content"
        j.decisions = ""
        j.transcript = "transcript content"
        raw = j.write().read_text()

        # Inject hypothetical v2 anchor after the transcript block
        raw_with_v2 = raw + f"\n{v2_start}\nv2 review content\n{v2_end}\n"
        j._resolved_path.write_text(raw_with_v2)  # type: ignore[union-attr]

        content = j._resolved_path.read_text()  # type: ignore[union-attr]

        # v1 anchors are still present and extractable
        pr = _extract_between(content, PR_BODY_START, PR_BODY_END)
        assert "PR body content" in pr

        tr = _extract_between(content, TRANSCRIPT_START, TRANSCRIPT_END)
        assert "transcript content" in tr
