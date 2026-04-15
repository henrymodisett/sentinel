"""Regression tests for scan failure handling.

Dogfood run on portfolio_new showed the failure mode: 6 lenses evaluated
successfully, synthesis timed out after 600s, and every successful lens
evaluation was dropped on the floor. The CLI still exited 0. Both of
those behaviors violate the "no silent failures" engineering principle.

These tests lock in:
- `_persist_scan` writes a partial-scan file when `result.ok=False`
- Partial file includes the ⚠️ Partial banner, the failure reason, and
  every completed lens evaluation (nothing lost)
- `sentinel work` and `sentinel scan` exit non-zero when the scan fails
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — runtime use via tmp_path type
from unittest.mock import patch

from click.testing import CliRunner

from sentinel.cli.main import main
from sentinel.cli.scan_cmd import _persist_scan
from sentinel.roles.monitor import LensEvaluation, ScanResult


def _failed_result_with_partial_lenses() -> ScanResult:
    """A ScanResult shaped like a real synthesis-timeout: lenses ran
    successfully, synthesis never produced parsed output, overall_score
    is averaged from the successful lenses."""
    result = ScanResult(
        project_summary="This is a project summary from the explore step.",
        evaluations=[
            LensEvaluation(
                lens_name="visual-craft",
                score=98,
                top_finding="Animations feel crisp.",
                findings="Detailed findings for visual-craft.",
                recommended_tasks=["audit motion tokens"],
            ),
            LensEvaluation(
                lens_name="performance-eng",
                score=70,
                top_finding="3D assets dominate LCP.",
                findings="Detailed findings for performance-eng.",
                recommended_tasks=["defer WebGL init", "split bundle"],
            ),
            LensEvaluation(
                lens_name="deployment-ops",
                score=60,
                top_finding="No monitoring configured.",
                findings="Detailed findings for deployment-ops.",
                recommended_tasks=["add Vercel analytics"],
            ),
        ],
        overall_score=76,  # avg of 98, 70, 60 rounded
        ok=False,
        error="Synthesis failed: Error: Gemini CLI timed out after 600s",
        provider="gemini",
        model="gemini-2.5-flash",
        total_input_tokens=5000,
        total_output_tokens=1200,
        total_cost_usd=0.0043,
    )
    return result


class TestPersistPartialScan:
    def test_writes_partial_banner_when_not_ok(self, tmp_path: Path) -> None:
        result = _failed_result_with_partial_lenses()
        scan_file = _persist_scan(tmp_path, result)
        content = scan_file.read_text()
        assert "Partial scan" in content
        assert "synthesis did not complete" in content.lower()
        assert "Gemini CLI timed out after 600s" in content

    def test_no_banner_on_successful_scan(self, tmp_path: Path) -> None:
        """Regression guard: the banner must not appear on complete scans."""
        result = _failed_result_with_partial_lenses()
        result.ok = True
        result.error = None
        result.raw_report = "A normal summary."
        result.strengths = ["great design"]
        scan_file = _persist_scan(tmp_path, result)
        content = scan_file.read_text()
        assert "Partial scan" not in content

    def test_preserves_every_lens_finding_on_failure(
        self, tmp_path: Path,
    ) -> None:
        """The point of the fix: all successful lens evaluations must
        survive a synthesis failure. Losing 6/6 successful lenses on
        a single flaky Gemini call is what we're preventing."""
        result = _failed_result_with_partial_lenses()
        scan_file = _persist_scan(tmp_path, result)
        content = scan_file.read_text()

        for ev in result.evaluations:
            assert ev.lens_name in content, (
                f"lens {ev.lens_name} missing from persisted partial scan"
            )
            assert ev.top_finding in content, (
                f"top_finding for {ev.lens_name} missing"
            )
            for task in ev.recommended_tasks:
                assert task in content, (
                    f"recommended task '{task}' for {ev.lens_name} missing"
                )

    def test_partial_scan_shows_computed_overall_score(
        self, tmp_path: Path,
    ) -> None:
        """Overall score computed from successful lens averages must
        still appear, so the file carries a real top-line number rather
        than a misleading 0."""
        result = _failed_result_with_partial_lenses()
        scan_file = _persist_scan(tmp_path, result)
        content = scan_file.read_text()
        assert "76/100" in content

    def test_partial_scan_fallback_summary_text(self, tmp_path: Path) -> None:
        """If synthesis never produced a summary, the file should say so
        explicitly rather than leave the Summary section blank."""
        result = _failed_result_with_partial_lenses()
        scan_file = _persist_scan(tmp_path, result)
        content = scan_file.read_text()
        assert "synthesis did not complete" in content.lower()


class TestWorkExitsNonZeroOnScanFailure:
    """When the scan fails, `sentinel work` must exit non-zero so users
    and CI can detect failures. Previously returned 0 because the failure
    path just printed and returned."""

    def test_work_exits_nonzero_on_scan_failure(
        self, fake_cli_env, isolated_home,
    ) -> None:
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--yes"])

        # Init the project as a git repo with a commit so the clean-tree
        # gate lets work proceed.
        import subprocess
        subprocess.run(
            ["git", "init"], cwd=isolated_home, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "t@t.io"],
            cwd=isolated_home, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "t"],
            cwd=isolated_home, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=isolated_home,
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=isolated_home,
            check=True, capture_output=True,
        )

        # Patch Monitor.assess to return a simulated synthesis failure.
        # This is the exact shape we hit in the live dogfood run.
        async def fake_assess(self, state, on_progress=None):
            return _failed_result_with_partial_lenses()

        with patch(
            "sentinel.roles.monitor.Monitor.assess", fake_assess,
        ):
            result = CliRunner().invoke(main, ["work"])

        # Proves we actually exercised the scan-failure path and didn't
        # exit non-zero for some unrelated reason (bad fixture, import
        # error, etc.).
        assert "Scan failed" in (result.output or ""), (
            "fixture did not reach the scan-failure code path; "
            f"output was:\n{result.output}"
        )
        assert result.exit_code != 0, (
            "work must exit non-zero on scan failure; "
            f"got exit={result.exit_code}, output:\n{result.output}"
        )
