"""Scenario-matrix tests for sentinel init.

Covers every combination of installed provider CLIs to make sure the
setup wizard produces sane configs on any user's machine.

All tests use fake CLI stubs (via the `fake_cli_env` fixture in
conftest.py) and an isolated temp project directory. No real LLM
calls are made.
"""

from __future__ import annotations

import tomllib
from pathlib import Path  # noqa: TC003 — runtime use in _read_config

from click.testing import CliRunner

from sentinel.cli.main import main


def _read_config(project_dir: Path) -> dict:
    """Load the config sentinel init just wrote."""
    return tomllib.loads((project_dir / ".sentinel" / "config.toml").read_text())


# ---------- No CLIs available ----------

class TestNoProviders:
    """User has no LLM provider CLI installed."""

    def test_init_bails_with_actionable_message(
        self, fake_cli_env, isolated_home,
    ):
        fake_cli_env()  # empty — no stubs
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0  # exits cleanly, just prints hints
        assert "No providers available" in result.output
        assert "brew install" in result.output or "install" in result.output.lower()
        # No config should have been written
        assert not (isolated_home / ".sentinel" / "config.toml").exists()


# ---------- Single CLI available ----------

class TestSingleProvider:
    """Only one provider CLI installed — config must still be usable."""

    def test_only_claude(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)
        # Every role must be assigned — no empty config
        for role in ("monitor", "researcher", "planner", "coder", "reviewer"):
            assert config["roles"][role]["provider"] == "claude", (
                f"{role} should fall back to claude when it's the only provider"
            )

    def test_only_gemini(self, fake_cli_env, isolated_home):
        fake_cli_env(gemini=True)
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)
        for role in ("monitor", "researcher", "planner", "coder", "reviewer"):
            assert config["roles"][role]["provider"] == "gemini"

    def test_only_codex(self, fake_cli_env, isolated_home):
        fake_cli_env(codex=True)
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)
        for role in ("monitor", "researcher", "planner", "coder", "reviewer"):
            assert config["roles"][role]["provider"] == "openai"


# ---------- Combinations ----------

class TestCombinations:
    """Multiple providers — defaults should pick smart per-role."""

    def test_claude_plus_gemini(self, fake_cli_env, isolated_home):
        """The common case — Claude Code + Gemini CLI, no Ollama, no Codex."""
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)

        # Monitor should prefer Gemini Flash (cheap/fast)
        assert config["roles"]["monitor"]["provider"] == "gemini"
        assert "flash" in config["roles"]["monitor"]["model"].lower()

        # Planner/coder should default to claude (best for judgment + agentic)
        assert config["roles"]["planner"]["provider"] == "claude"
        assert config["roles"]["coder"]["provider"] == "claude"

        # Reviewer must not equal coder provider (independence)
        assert (
            config["roles"]["reviewer"]["provider"]
            != config["roles"]["coder"]["provider"]
        )

    def test_all_four_providers(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True, codex=True, gemini=True, ollama=True)
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)

        # Recommended defaults: monitor → gemini-flash (fastest + free tier)
        assert config["roles"]["monitor"]["provider"] == "gemini"
        assert "flash" in config["roles"]["monitor"]["model"].lower()

        # Reviewer ≠ coder (independence check)
        assert (
            config["roles"]["reviewer"]["provider"]
            != config["roles"]["coder"]["provider"]
        )


# ---------- Preserves invariants ----------

class TestInvariants:
    """Things that must be true of any generated config."""

    def test_all_roles_assigned(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)

        required_roles = {"monitor", "researcher", "planner", "coder", "reviewer"}
        assert set(config["roles"].keys()) == required_roles

        for role, role_config in config["roles"].items():
            assert role_config.get("provider"), f"{role} missing provider"
            assert role_config.get("model"), f"{role} missing model"

    def test_budget_has_sensible_defaults(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)

        assert config["budget"]["daily_limit_usd"] > 0
        assert config["budget"]["warn_at_usd"] > 0
        assert config["budget"]["warn_at_usd"] <= config["budget"]["daily_limit_usd"]

    def test_project_type_detected(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        # Pretend this temp dir is a Python project
        (isolated_home / "pyproject.toml").write_text(
            '[project]\nname = "fake"\n',
        )
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)
        assert config["project"]["type"] == "python"

    def test_python_detected_from_requirements_txt(
        self, fake_cli_env, isolated_home,
    ):
        """Regression: sigint has requirements.txt but no top-level
        pyproject.toml; previously landed as 'generic'."""
        fake_cli_env(claude=True)
        (isolated_home / "requirements.txt").write_text("requests\n")
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)
        assert config["project"]["type"] == "python"

    def test_python_detected_from_setup_py(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        (isolated_home / "setup.py").write_text("from setuptools import setup\n")
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)
        assert config["project"]["type"] == "python"

    def test_primary_language_beats_side_package_json(
        self, fake_cli_env, isolated_home,
    ):
        """Regression: a Rust project that ships a docs site with
        package.json should NOT land as JavaScript. State-level
        detection wins over the init-only node label."""
        fake_cli_env(claude=True)
        (isolated_home / "Cargo.toml").write_text(
            '[package]\nname = "demo"\n',
        )
        (isolated_home / "package.json").write_text(
            '{"name": "docs-site"}',
        )
        CliRunner().invoke(main, ["init", "--yes"])
        config = _read_config(isolated_home)
        assert config["project"]["type"] == "rust"

    def test_non_interactive_mode_auto_proceeds(self, fake_cli_env, isolated_home):
        """Running in a pipe (no TTY) must not hang on confirmation."""
        fake_cli_env(claude=True)
        runner = CliRunner()
        result = runner.invoke(main, ["init"])  # no --yes
        # CliRunner simulates non-TTY stdin → init should auto-proceed
        assert result.exit_code == 0
        assert (isolated_home / ".sentinel" / "config.toml").exists()


# ---------- Presets ----------

class TestPresets:
    """--preset X should skip interactive questions and use the named preset."""

    def test_preset_simple_uses_one_provider(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--preset", "simple"])
        config = _read_config(isolated_home)
        # simple = use one provider for everything (claude preferred)
        providers = {r["provider"] for r in config["roles"].values()}
        assert providers == {"claude"}, (
            f"simple preset should collapse to one provider, got {providers}"
        )

    def test_preset_cheap_prefers_free_tiers(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--preset", "cheap"])
        config = _read_config(isolated_home)
        # Coder still needs agentic — stays on claude
        assert config["roles"]["coder"]["provider"] == "claude"
        # Everything else should prefer gemini-flash (free tier) over claude
        for role in ("monitor", "researcher", "planner", "reviewer"):
            assert config["roles"][role]["provider"] == "gemini", (
                f"cheap preset should push {role} off claude onto gemini"
            )

    def test_preset_power_uses_top_models(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--preset", "power"])
        config = _read_config(isolated_home)
        # Planner, coder, reviewer should get opus
        assert "opus" in config["roles"]["planner"]["model"]
        assert "opus" in config["roles"]["coder"]["model"]

    def test_preset_local_falls_back_if_ollama_missing(
        self, fake_cli_env, isolated_home,
    ):
        # No ollama — should fall back to recommended silently
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--preset", "local"])
        config = _read_config(isolated_home)
        # Nothing should be 'local' since ollama isn't installed
        providers = {r["provider"] for r in config["roles"].values()}
        assert "local" not in providers

    def test_preset_cheap_never_writes_unavailable_provider(self):
        """Regression: cheap preset used to hard-code gemini as the coder
        fallback even when gemini wasn't installed."""
        from sentinel.config.schema import ProviderName
        from sentinel.recommendations import apply_preset

        # Ollama-only install — every role must land on an installed provider
        assignments = apply_preset(
            "cheap",
            available={ProviderName.LOCAL},
            ollama_models=["qwen2.5-coder:14b"],
        )
        providers = {prov for prov, _ in assignments.values()}
        assert providers <= {ProviderName.LOCAL}, (
            f"cheap preset wrote unavailable providers: {providers}"
        )

    def test_preset_local_keeps_coder_on_claude(self):
        """Regression: `local` used to assign Ollama to every role
        including Coder, but Ollama has no Claude-Code-equivalent
        agentic loop, so Coder would never actually execute. Now
        Coder falls back to claude/openai while other roles stay local."""
        from sentinel.config.schema import ProviderName, RoleName
        from sentinel.recommendations import apply_preset

        available = {
            ProviderName.CLAUDE, ProviderName.GEMINI, ProviderName.LOCAL,
        }
        assignments = apply_preset(
            "local", available, ollama_models=["qwen2.5-coder:14b"],
        )
        # Coder must be claude (agentic-capable)
        assert assignments[RoleName.CODER][0] == ProviderName.CLAUDE
        # All other roles stay local
        for role in (
            RoleName.MONITOR, RoleName.RESEARCHER,
            RoleName.PLANNER, RoleName.REVIEWER,
        ):
            assert assignments[role][0] == ProviderName.LOCAL, (
                f"{role} should be local in `local` preset"
            )

    def test_preset_local_falls_back_to_openai_when_no_claude(self):
        """If neither claude nor ollama gives agentic, coder picks
        openai. This keeps the preset usable on openai-only installs."""
        from sentinel.config.schema import ProviderName, RoleName
        from sentinel.recommendations import apply_preset

        available = {ProviderName.OPENAI, ProviderName.LOCAL}
        assignments = apply_preset(
            "local", available, ollama_models=["qwen2.5-coder:14b"],
        )
        assert assignments[RoleName.CODER][0] == ProviderName.OPENAI

    def test_preset_hybrid_local_monitor_cloud_elsewhere(self):
        """New `hybrid` preset: Ollama for Monitor only (runs on every
        cycle — save cloud $ on the hot path), cloud for the other
        four roles for quality."""
        from sentinel.config.schema import ProviderName, RoleName
        from sentinel.recommendations import apply_preset

        available = {
            ProviderName.CLAUDE, ProviderName.GEMINI, ProviderName.LOCAL,
        }
        assignments = apply_preset(
            "hybrid", available, ollama_models=["qwen2.5-coder:14b"],
        )
        assert assignments[RoleName.MONITOR][0] == ProviderName.LOCAL
        # All other roles must NOT be local
        for role in (
            RoleName.RESEARCHER, RoleName.PLANNER,
            RoleName.CODER, RoleName.REVIEWER,
        ):
            assert assignments[role][0] != ProviderName.LOCAL

    def test_preset_hybrid_falls_back_when_no_ollama(self):
        """Without Ollama, hybrid degrades to recommended defaults
        for all roles including Monitor."""
        from sentinel.config.schema import ProviderName, RoleName
        from sentinel.recommendations import apply_preset

        available = {ProviderName.CLAUDE, ProviderName.GEMINI}
        assignments = apply_preset(
            "hybrid", available, ollama_models=[],
        )
        # Monitor should be recommended (gemini-flash) — no local available
        assert assignments[RoleName.MONITOR][0] == ProviderName.GEMINI
        # Coder still claude
        assert assignments[RoleName.CODER][0] == ProviderName.CLAUDE

    def test_unknown_preset_fails_cleanly(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        result = CliRunner().invoke(main, ["init", "--preset", "bogus"])
        # click should reject before run_init is even called
        assert result.exit_code != 0
        assert not (isolated_home / ".sentinel" / "config.toml").exists()


# ---------- Goals.md nudge (not a blocker) ----------

class TestAutoGitignore:
    """sentinel init appends .sentinel/ and .claude/ to the target's
    .gitignore so every open-pr.sh / git status doesn't warn about
    untracked sentinel artifacts."""

    def test_creates_gitignore_when_absent(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        CliRunner().invoke(main, ["init", "--yes"])
        gitignore = (isolated_home / ".gitignore").read_text()
        assert ".sentinel/" in gitignore
        assert ".claude/" in gitignore

    def test_appends_to_existing_gitignore(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        (isolated_home / ".gitignore").write_text("node_modules/\n")
        CliRunner().invoke(main, ["init", "--yes"])
        gitignore = (isolated_home / ".gitignore").read_text()
        # Existing entries preserved
        assert "node_modules/" in gitignore
        # New sentinel entries added
        assert ".sentinel/" in gitignore

    def test_idempotent_on_reinit(self, fake_cli_env, isolated_home):
        """Running init twice must not duplicate the sentinel block."""
        fake_cli_env(claude=True)
        CliRunner().invoke(main, ["init", "--yes"])
        first = (isolated_home / ".gitignore").read_text()
        CliRunner().invoke(main, ["init", "--yes"])
        second = (isolated_home / ".gitignore").read_text()
        assert first == second, "gitignore must not grow on re-init"
        # Marker appears exactly once
        assert second.count("# sentinel artifacts") == 1

    def test_gitignore_change_is_committed_in_git_repo(
        self, fake_cli_env, isolated_home,
    ):
        """Regression: .gitignore was being reset-away by
        _reset_and_checkout between items because init wrote it but
        never committed. In a git repo, init must commit the change
        so it survives sentinel work's between-item resets."""
        import subprocess as _sp

        fake_cli_env(claude=True)
        _sp.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=isolated_home, check=True,
        )
        _sp.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=isolated_home, check=True,
        )

        CliRunner().invoke(main, ["init", "--yes"])

        log = _sp.run(
            ["git", "log", "--oneline"],
            capture_output=True, text=True, cwd=isolated_home,
        ).stdout
        assert "gitignore sentinel artifacts" in log, (
            f"init must commit .gitignore in a git repo; log was:\n{log}"
        )
        # Working tree must be clean on .gitignore
        status = _sp.run(
            ["git", "status", "--porcelain", ".gitignore"],
            capture_output=True, text=True, cwd=isolated_home,
        ).stdout
        assert not status, f"expected clean .gitignore, got: {status!r}"

    def test_no_commit_when_not_in_git_repo(
        self, fake_cli_env, isolated_home,
    ):
        """Outside a git repo, init still writes .gitignore but must
        not explode trying to commit it."""
        fake_cli_env(claude=True)
        result = CliRunner().invoke(main, ["init", "--yes"])
        assert result.exit_code == 0
        assert (isolated_home / ".gitignore").exists()

    def test_gitignore_commit_ignores_prestaged_files(
        self, fake_cli_env, isolated_home,
    ):
        """Regression: init's `git commit -m ...` without a pathspec
        used to sweep in anything pre-staged in the user's index. Now
        uses `git commit -- .gitignore` so only .gitignore lands."""
        import subprocess as _sp

        fake_cli_env(claude=True)
        _sp.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=isolated_home, check=True,
        )
        _sp.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=isolated_home, check=True,
        )
        # User has a file staged BEFORE running sentinel init
        (isolated_home / "my_work.py").write_text("print('work in progress')\n")
        _sp.run(["git", "add", "my_work.py"], cwd=isolated_home, check=True)

        CliRunner().invoke(main, ["init", "--yes"])

        # User's staged file must still be staged (not committed)
        staged = _sp.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, cwd=isolated_home,
        ).stdout.strip()
        assert "my_work.py" in staged, (
            "init must not commit user's pre-staged files; "
            f"staged after init: {staged!r}"
        )
        # And the gitignore commit must NOT include my_work.py
        latest_files = _sp.run(
            ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
            capture_output=True, text=True, cwd=isolated_home,
        ).stdout.strip()
        assert "my_work.py" not in latest_files
        assert ".gitignore" in latest_files


class TestGoalsNudge:
    """Goals.md template should warn, not block — work still proceeds."""

    def test_work_proceeds_when_goals_is_template(
        self, fake_cli_env, isolated_home,
    ):
        """A fresh init leaves goals.md as a template — work should still run."""
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--yes"])

        # Goals.md is still the default template at this point
        from sentinel.cli.work_cmd import _goals_filled
        assert not _goals_filled(isolated_home), (
            "test setup: goals.md should still be a template after fresh init"
        )


# ---------- Re-running init ----------

class TestReinit:
    """Running init twice should not clobber existing state."""

    def test_skips_existing_config(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        CliRunner().invoke(main, ["init", "--yes"])

        # Manually edit the config to verify we don't overwrite
        config_path = isolated_home / ".sentinel" / "config.toml"
        edited = config_path.read_text().replace(
            'provider = "claude"', 'provider = "claude"  # user-edited',
        )
        config_path.write_text(edited)

        # Re-run
        CliRunner().invoke(main, ["init", "--yes"])
        # Marker should still be there
        assert "# user-edited" in config_path.read_text()

    def test_skips_existing_goals(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        CliRunner().invoke(main, ["init", "--yes"])

        goals_path = isolated_home / ".sentinel" / "goals.md"
        goals_path.write_text("# my custom goals\n")

        CliRunner().invoke(main, ["init", "--yes"])
        assert goals_path.read_text() == "# my custom goals\n"
