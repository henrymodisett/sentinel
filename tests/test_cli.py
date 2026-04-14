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


class TestUnimplementedCommandsFailLoudly:
    """Unimplemented commands must fail loudly, not silently."""

    def test_cycle_exits_with_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["cycle"])
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
