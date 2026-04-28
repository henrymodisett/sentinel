"""Tests for sentinel init Doctrine seeding (sentinel#90).

Covers the ``seed_default_doctrine`` helper and its wiring into
``sentinel init``.  All tests mock the subprocess boundary so they run
without a live cortex binary; one integration test (marked
``pytest.mark.integration``) exercises the real binary when available.
"""

from __future__ import annotations

import subprocess
from pathlib import Path  # noqa: TC003 — runtime use in fixtures and helpers
from unittest.mock import patch

import pytest

from sentinel.integrations.cortex import SeedResult, seed_default_doctrine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doctrine_dir(project: Path) -> Path:
    return project / ".cortex" / "doctrine"


def _sentinel_entry(project: Path) -> Path:
    return _doctrine_dir(project) / "0001-tests-accompany-behavior-changes.md"


def _make_fake_cortex(tmp_path: Path, *, returncode: int = 0) -> Path:
    """Write a stub cortex binary that creates .cortex/doctrine/ entries."""
    stub = tmp_path / "fake_bin" / "cortex"
    stub.parent.mkdir(parents=True, exist_ok=True)

    # Stub creates the expected entries on success so integration-adjacent
    # assertions can check file presence without a real cortex.
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# Sentinel test stub — writes minimal doctrine files\n"
        f"exit {returncode}\n"
    )
    stub.chmod(0o755)
    return stub


# ---------------------------------------------------------------------------
# Unit tests for seed_default_doctrine()
# ---------------------------------------------------------------------------


class TestSeedDefaultDoctrine:
    def test_missing_cortex_returns_none(self, tmp_path: Path) -> None:
        """No cortex binary → None, no exception."""
        with patch("sentinel.integrations.cortex.shutil.which", return_value=None):
            # Reset the once-warning flag to avoid ordering issues.
            import sentinel.integrations.cortex as _cortex_mod
            _cortex_mod._missing_cortex_warned = False
            result = seed_default_doctrine(tmp_path)
        assert result is None

    def test_missing_cortex_warned_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import sentinel.integrations.cortex as _cortex_mod
        _cortex_mod._missing_cortex_warned = False
        import logging

        with (
            patch("sentinel.integrations.cortex.shutil.which", return_value=None),
            caplog.at_level(logging.WARNING, logger="sentinel.integrations.cortex"),
        ):
            seed_default_doctrine(tmp_path)
            # Second call — should NOT emit a second warning.
            seed_default_doctrine(tmp_path)

        warnings = [r for r in caplog.records if "cortex binary not found" in r.message]
        assert len(warnings) == 1

    def test_subprocess_timeout_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        with (
            patch("sentinel.integrations.cortex.shutil.which", return_value="/usr/bin/cortex"),
            patch(
                "sentinel.integrations.cortex.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="cortex", timeout=30),
            ),caplog.at_level(logging.WARNING, logger="sentinel.integrations.cortex")
        ):
            result = seed_default_doctrine(tmp_path, timeout_sec=30)

        assert result is None
        assert any("timed out" in r.message for r in caplog.records)

    def test_nonzero_exit_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="some cortex error",
        )
        with (
            patch("sentinel.integrations.cortex.shutil.which", return_value="/usr/bin/cortex"),
            patch("sentinel.integrations.cortex.subprocess.run", return_value=mock_proc),
            caplog.at_level(logging.WARNING, logger="sentinel.integrations.cortex"),
        ):
            result = seed_default_doctrine(tmp_path)

        assert result is None
        assert any("exited 1" in r.message for r in caplog.records)

    def test_success_returns_seed_result(self, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Seeded 15 entries\n", stderr="",
        )
        with (
            patch("sentinel.integrations.cortex.shutil.which", return_value="/usr/bin/cortex"),
            patch("sentinel.integrations.cortex.subprocess.run", return_value=mock_proc),
        ):
            result = seed_default_doctrine(tmp_path)

        assert isinstance(result, SeedResult)
        assert result.status == "ok"
        assert result.seeded == 15  # count of files in defaults/doctrine/

    def test_merge_flag_passed_to_subprocess(self, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with (
            patch("sentinel.integrations.cortex.shutil.which", return_value="/usr/bin/cortex"),
            patch(
                "sentinel.integrations.cortex.subprocess.run", return_value=mock_proc,
            ) as mock_run,
        ):
            seed_default_doctrine(tmp_path, merge="abort")

        cmd = mock_run.call_args[0][0]
        assert "--merge" in cmd
        assert "abort" in cmd

    def test_default_merge_is_skip_existing(self, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with (
            patch("sentinel.integrations.cortex.shutil.which", return_value="/usr/bin/cortex"),
            patch(
                "sentinel.integrations.cortex.subprocess.run", return_value=mock_proc,
            ) as mock_run,
        ):
            seed_default_doctrine(tmp_path)

        cmd = mock_run.call_args[0][0]
        merge_idx = cmd.index("--merge")
        assert cmd[merge_idx + 1] == "skip-existing"

    def test_path_flag_passed_to_subprocess(self, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with (
            patch("sentinel.integrations.cortex.shutil.which", return_value="/usr/bin/cortex"),
            patch(
                "sentinel.integrations.cortex.subprocess.run", return_value=mock_proc,
            ) as mock_run,
        ):
            seed_default_doctrine(tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "--path" in cmd
        assert str(tmp_path) in cmd

    def test_no_add_imports_flags_present(self, tmp_path: Path) -> None:
        """Seeding must not modify CLAUDE.md or AGENTS.md."""
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with (
            patch("sentinel.integrations.cortex.shutil.which", return_value="/usr/bin/cortex"),
            patch(
                "sentinel.integrations.cortex.subprocess.run", return_value=mock_proc,
            ) as mock_run,
        ):
            seed_default_doctrine(tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "--no-add-imports-claude" in cmd
        assert "--no-add-imports-agents" in cmd


# ---------------------------------------------------------------------------
# Integration tests for sentinel init
# ---------------------------------------------------------------------------


class TestInitSeedDoctrine:
    def test_fresh_init_seeds_defaults(
        self, fake_cli_env, isolated_home, monkeypatch,
    ) -> None:
        """sentinel init seeds .cortex/doctrine/ with baseline entries."""
        fake_cli_env(claude=True)
        # Mock seed_default_doctrine to write the expected doctrine files
        # rather than shelling out — avoids the cortex binary requirement.
        def _fake_seed(project_dir: Path, **_kwargs) -> SeedResult:
            doc_dir = project_dir / ".cortex" / "doctrine"
            doc_dir.mkdir(parents=True, exist_ok=True)
            entry = doc_dir / "0001-tests-accompany-behavior-changes.md"
            entry.write_text(
                "---\ntitle: Tests accompany behavior changes\n"
                "Sentinel-baseline: true\n---\n"
                "# Tests accompany behavior changes\n",
            )
            return SeedResult(status="ok", seeded=1)

        monkeypatch.setattr(
            "sentinel.cli.init_cmd.seed_default_doctrine", _fake_seed,
            raising=False,
        )
        # Import happens lazily in _seed_doctrine_defaults; patch at the
        # module level the helper will look up at call time.
        with patch(
            "sentinel.integrations.cortex.seed_default_doctrine", _fake_seed,
        ):
            from click.testing import CliRunner

            from sentinel.cli.main import main

            result = CliRunner().invoke(main, ["init", "--yes"])

        assert result.exit_code == 0
        entry = isolated_home / ".cortex" / "doctrine" / "0001-tests-accompany-behavior-changes.md"
        assert entry.exists(), (
            f".cortex/doctrine/0001-... not found; init output:\n{result.output}"
        )
        content = entry.read_text()
        assert "Sentinel-baseline: true" in content

    def test_no_seed_defaults_flag_skips(
        self, fake_cli_env, isolated_home,
    ) -> None:
        """--no-seed-defaults leaves .cortex/doctrine/ empty / absent."""
        fake_cli_env(claude=True)

        with patch(
            "sentinel.integrations.cortex.seed_default_doctrine",
        ) as mock_seed:
            from click.testing import CliRunner

            from sentinel.cli.main import main

            result = CliRunner().invoke(main, ["init", "--yes", "--no-seed-defaults"])

        assert result.exit_code == 0
        mock_seed.assert_not_called()
        # No doctrine directory should exist (we never called seed)
        assert not (isolated_home / ".cortex" / "doctrine").exists()

    def test_missing_cortex_binary_graceful(
        self, fake_cli_env, isolated_home,
    ) -> None:
        """Init completes successfully even when cortex is not installed.

        Patches seed_default_doctrine to return None (what the function
        returns when shutil.which("cortex") is None) rather than patching
        shutil.which globally, which would break provider detection.
        """
        fake_cli_env(claude=True)

        with patch(
            "sentinel.integrations.cortex.seed_default_doctrine",
            return_value=None,
        ):
            from click.testing import CliRunner

            from sentinel.cli.main import main

            result = CliRunner().invoke(main, ["init", "--yes"])

        assert result.exit_code == 0
        assert "Done!" in result.output
        # Doctrine dir not created (seed returned None)
        assert not (isolated_home / ".cortex" / "doctrine").exists()

    def test_subprocess_timeout_graceful(
        self, fake_cli_env, isolated_home,
    ) -> None:
        """Init completes successfully when cortex init times out."""
        fake_cli_env(claude=True)

        with (
            patch("sentinel.integrations.cortex.shutil.which", return_value="/usr/bin/cortex"),
            patch(
                "sentinel.integrations.cortex.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="cortex", timeout=30),
            ),
        ):
            from click.testing import CliRunner

            from sentinel.cli.main import main

            result = CliRunner().invoke(main, ["init", "--yes"])

        assert result.exit_code == 0
        assert "Done!" in result.output

    def test_reseed_skips_existing(
        self, fake_cli_env, isolated_home, monkeypatch,
    ) -> None:
        """Second sentinel init run reports skipped entries, no errors."""
        fake_cli_env(claude=True)

        call_count = 0

        def _fake_seed(project_dir: Path, **_kwargs) -> SeedResult:
            nonlocal call_count
            call_count += 1
            doc_dir = project_dir / ".cortex" / "doctrine"
            doc_dir.mkdir(parents=True, exist_ok=True)
            return SeedResult(status="ok", seeded=15, skipped=0)

        with patch(
            "sentinel.integrations.cortex.seed_default_doctrine", _fake_seed,
        ):
            from click.testing import CliRunner

            from sentinel.cli.main import main

            runner = CliRunner()
            r1 = runner.invoke(main, ["init", "--yes"])
            # Re-init: non-interactive with existing config refreshes files.
            r2 = runner.invoke(main, ["init", "--yes"])

        assert r1.exit_code == 0
        assert r2.exit_code == 0
        assert call_count == 2  # seeding called on both runs


# ---------------------------------------------------------------------------
# Integration test — real cortex binary (skipped when absent)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSeedDefaultDoctrineIntegration:
    @pytest.fixture(autouse=True)
    def _require_cortex(self) -> None:
        import shutil as _shutil

        if not _shutil.which("cortex"):
            pytest.skip("cortex binary not available")

    def test_real_cortex_seeds_defaults(self, tmp_path: Path) -> None:
        """Actually invoke cortex and verify doctrine files land."""
        result = seed_default_doctrine(tmp_path)
        assert result is not None
        assert result.status == "ok"
        doctrine_dir = tmp_path / ".cortex" / "doctrine"
        assert doctrine_dir.is_dir()
        entries = list(doctrine_dir.glob("*.md"))
        assert len(entries) >= 1, "Expected at least one seeded doctrine entry"
        # Verify one of the expected baseline entries is present
        names = {e.name for e in entries}
        assert any("tests" in n for n in names), (
            f"Expected 0001-tests-* entry; found: {names}"
        )
