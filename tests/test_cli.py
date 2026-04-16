"""Tests for CLI commands."""

from click.testing import CliRunner

from sentinel.cli.main import main


class TestCLIBasics:
    def test_version(self) -> None:
        from sentinel import __version__
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Autonomous meta-agent" in result.output

    def test_init_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--help"])
        assert result.exit_code == 0

    def test_scan_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["scan", "--help"])
        assert result.exit_code == 0

    def test_plan_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["plan", "--help"])
        assert result.exit_code == 0

    def test_providers_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["providers", "--help"])
        assert result.exit_code == 0

    def test_cycle_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["cycle", "--help"])
        assert result.exit_code == 0

    def test_cost_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["cost", "--help"])
        assert result.exit_code == 0


class TestStatusCommand:
    """Status produces a one-screen project health summary without
    making any LLM calls. Free + read-only — runnable on any project."""

    def test_status_runs_in_clean_dir_without_config(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A directory without .sentinel/config.toml should print a
        friendly hint, not crash."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "Sentinel Status" in result.output
        assert "config.toml" in result.output

    def test_status_with_config_shows_state_and_spend(
        self, tmp_path, monkeypatch,
    ) -> None:
        """When config exists, status prints state, spend, and a
        latest-cycle hint when no cycles have been run yet."""
        sentinel_dir = tmp_path / ".sentinel"
        sentinel_dir.mkdir()
        (sentinel_dir / "config.toml").write_text(
            f'[project]\nname = "test"\npath = "{tmp_path}"\ntype = "python"\n'
            '[roles.monitor]\nprovider = "gemini"\nmodel = "gemini-2.5-flash"\n'
            '[roles.researcher]\nprovider = "gemini"\nmodel = "gemini-2.5-pro"\n'
            '[roles.planner]\nprovider = "claude"\nmodel = "claude-opus-4-6"\n'
            '[roles.coder]\nprovider = "claude"\nmodel = "claude-sonnet-4-6"\n'
            '[roles.reviewer]\nprovider = "gemini"\nmodel = "gemini-2.5-pro"\n'
            "[budget]\ndaily_limit_usd = 15.0\nwarn_at_usd = 10.0\n",
        )
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        assert "State:" in result.output
        assert "Spend (today):" in result.output


class TestUnimplementedCommandsFailLoudly:
    """Remaining unimplemented commands must fail loudly, not silently."""

    def test_config_exits_with_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["config"])
        assert result.exit_code == 1
        assert "Not yet implemented" in result.output


class TestShippingPreflight:
    """The preflight that blocks `sentinel work` from starting if the
    shipping prerequisites (gh installed + authenticated, origin
    remote) aren't met. Codex flagged that the user otherwise hits a
    cryptic error deep inside the ship step."""

    def test_missing_gh_reports_install_command(
        self, tmp_path, monkeypatch,
    ) -> None:
        """No gh → return error mentioning brew install gh, no further
        checks attempted (early return)."""
        from sentinel.cli.work_cmd import _check_shipping_preflight

        monkeypatch.setattr("shutil.which", lambda name: None)
        errors = _check_shipping_preflight(tmp_path)
        assert errors
        assert any("brew install gh" in e for e in errors)

    def test_missing_origin_reports_git_remote_add(
        self, tmp_path, monkeypatch,
    ) -> None:
        """gh present + authenticated but no origin remote → error
        mentions `git remote add`."""
        import subprocess as _sp

        from sentinel.cli.work_cmd import _check_shipping_preflight

        # Real git repo in tmp_path with no remote
        _sp.run(
            ["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True,
        )

        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/gh")
        # Stub gh auth status to succeed; only origin should fail
        original_run = _sp.run

        def fake_run(args, *a, **kw):  # noqa: ANN001, ANN202
            if args[:3] == ["gh", "auth", "status"]:
                return _sp.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")
            return original_run(args, *a, **kw)

        monkeypatch.setattr(_sp, "run", fake_run)
        errors = _check_shipping_preflight(tmp_path)
        assert any("origin" in e for e in errors)


class TestRemovedCommands:
    """These commands were removed — ensure they're no longer available."""

    def test_watch_removed(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["watch"])
        assert result.exit_code != 0  # Should be unknown command

    def test_research_removed(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["research"])
        assert result.exit_code != 0  # Should be unknown command
