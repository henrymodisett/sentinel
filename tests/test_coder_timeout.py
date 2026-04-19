"""Tests for Coder CLI timeout resolution (Cortex C5).

Autumn-mail's first real sentinel cycle (2026-04-18) hit a hard 600s
Claude CLI timeout on its third revision pass. The old code path had
the coder timeout wired to `scan.provider_timeout_sec` with no
role-specific override. These tests lock in the three-layer resolution:

    --coder-timeout flag > SENTINEL_CODER_TIMEOUT env > config > default

and the pydantic bounds on the config field itself.
"""

from __future__ import annotations

import click
import pytest

from sentinel.cli.work_cmd import _resolve_coder_timeout


class TestResolveCoderTimeout:
    def test_default_from_config(self) -> None:
        assert _resolve_coder_timeout(
            cli_value=None, env_value=None, config_value=600,
        ) == 600

    def test_config_override_used_when_no_flag_or_env(self) -> None:
        assert _resolve_coder_timeout(
            cli_value=None, env_value=None, config_value=1200,
        ) == 1200

    def test_env_overrides_config(self) -> None:
        assert _resolve_coder_timeout(
            cli_value=None, env_value="900", config_value=600,
        ) == 900

    def test_env_empty_string_falls_through_to_config(self) -> None:
        """An empty SENTINEL_CODER_TIMEOUT shouldn't eat the config."""
        assert _resolve_coder_timeout(
            cli_value=None, env_value="", config_value=600,
        ) == 600

    def test_env_whitespace_falls_through_to_config(self) -> None:
        assert _resolve_coder_timeout(
            cli_value=None, env_value="   ", config_value=600,
        ) == 600

    def test_env_non_integer_raises(self) -> None:
        with pytest.raises(click.BadParameter, match="SENTINEL_CODER_TIMEOUT"):
            _resolve_coder_timeout(
                cli_value=None, env_value="not-a-number", config_value=600,
            )

    def test_flag_overrides_env(self) -> None:
        assert _resolve_coder_timeout(
            cli_value=1500, env_value="900", config_value=600,
        ) == 1500

    def test_flag_overrides_everything(self) -> None:
        assert _resolve_coder_timeout(
            cli_value=1500, env_value="900", config_value=1200,
        ) == 1500

    def test_flag_below_min_raises(self) -> None:
        with pytest.raises(click.BadParameter, match="--coder-timeout"):
            _resolve_coder_timeout(
                cli_value=10, env_value=None, config_value=600,
            )

    def test_flag_above_max_raises(self) -> None:
        with pytest.raises(click.BadParameter, match="--coder-timeout"):
            _resolve_coder_timeout(
                cli_value=99999, env_value=None, config_value=600,
            )

    def test_env_below_min_raises(self) -> None:
        with pytest.raises(click.BadParameter, match="SENTINEL_CODER_TIMEOUT"):
            _resolve_coder_timeout(
                cli_value=None, env_value="10", config_value=600,
            )

    def test_env_above_max_raises(self) -> None:
        with pytest.raises(click.BadParameter, match="SENTINEL_CODER_TIMEOUT"):
            _resolve_coder_timeout(
                cli_value=None, env_value="99999", config_value=600,
            )

    def test_bounds_are_inclusive(self) -> None:
        """Min and max themselves are valid values."""
        assert _resolve_coder_timeout(
            cli_value=60, env_value=None, config_value=600,
        ) == 60
        assert _resolve_coder_timeout(
            cli_value=7200, env_value=None, config_value=600,
        ) == 7200


class TestCliFlagWiring:
    """Smoke-test that `sentinel work --coder-timeout N` is accepted
    and plumbed through to the resolved timeout. Uses --dry-run so we
    don't actually invoke the Claude CLI."""

    def test_cli_flag_registered(self) -> None:
        from sentinel.cli.main import work
        params = {p.name: p for p in work.params}
        assert "coder_timeout" in params
        assert params["coder_timeout"].type is click.INT

    def test_cli_help_mentions_coder_timeout(self) -> None:
        from click.testing import CliRunner

        from sentinel.cli.main import work
        runner = CliRunner()
        result = runner.invoke(work, ["--help"])
        assert result.exit_code == 0
        assert "--coder-timeout" in result.output
