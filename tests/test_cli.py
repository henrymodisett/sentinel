"""Tests for CLI commands."""

from click.testing import CliRunner

from sentinel.cli.main import main


class TestCLIBasics:
    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

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

    def test_providers_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["providers", "--help"])
        assert result.exit_code == 0


class TestUnimplementedCommandsFailLoudly:
    """Sentinel's own scan flagged silent stubs as the #1 issue."""

    def test_cycle_exits_with_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["cycle"])
        assert result.exit_code == 1
        assert "Not yet implemented" in result.output

    def test_watch_exits_with_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["watch"])
        assert result.exit_code == 1
        assert "Not yet implemented" in result.output

    def test_research_exits_with_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["research"])
        assert result.exit_code == 1
        assert "Not yet implemented" in result.output

    def test_plan_exits_with_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["plan"])
        assert result.exit_code == 1
        assert "Not yet implemented" in result.output

    def test_status_exits_with_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 1
        assert "Not yet implemented" in result.output

    def test_config_exits_with_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["config"])
        assert result.exit_code == 1
        assert "Not yet implemented" in result.output
