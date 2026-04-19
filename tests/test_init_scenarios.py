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
    """sentinel init appends .claude/ to the target's .gitignore so every
    open-pr.sh / git status doesn't warn about Claude Code's per-user
    cache. R5.2: `.sentinel/` is NOT blanket-ignored here — the
    per-directory `.sentinel/.gitignore` written by
    `_write_sentinel_gitignore` handles `state/` exclusion so that
    durable artifacts (config.toml, runs/, proposals/, scans/,
    backlog.md, lenses.md, domain_brief.md) stay committable."""

    def test_creates_gitignore_when_absent(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        CliRunner().invoke(main, ["init", "--yes"])
        gitignore = (isolated_home / ".gitignore").read_text()
        # R5.2: `.sentinel/` must NOT be blanket-ignored at the root.
        assert ".sentinel/" not in gitignore.splitlines()
        # `.claude/` is Claude Code's per-user cache — project-external.
        assert ".claude/" in gitignore

    def test_appends_to_existing_gitignore(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True)
        (isolated_home / ".gitignore").write_text("node_modules/\n")
        CliRunner().invoke(main, ["init", "--yes"])
        gitignore = (isolated_home / ".gitignore").read_text()
        # Existing entries preserved
        assert "node_modules/" in gitignore
        # New sentinel artifacts entry added (.claude/ only — R5.2 scope).
        assert ".claude/" in gitignore
        assert ".sentinel/" not in gitignore.splitlines()

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

    def test_reinit_migrates_stale_sentinel_line(
        self, fake_cli_env, isolated_home,
    ):
        """R5.2 upgrade path: projects initialized with an older sentinel
        have a stale `.sentinel/` line inside the generated block. Re-
        running `sentinel init` must strip that line in-place so the bug
        this PR fixes actually gets repaired on existing projects (not
        just freshly-scaffolded ones).

        The marker comment is preserved (so the block stays recognizable
        as sentinel-managed) and unrelated user content is untouched.
        """
        # Simulate a .gitignore produced by an older sentinel version.
        stale = (
            "node_modules/\n"
            "\n"
            "# sentinel artifacts — generated per-run, not source\n"
            ".sentinel/\n"
            ".claude/\n"
        )
        (isolated_home / ".gitignore").write_text(stale)

        fake_cli_env(claude=True)
        result = CliRunner().invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output

        migrated = (isolated_home / ".gitignore").read_text()
        # Stale blanket is gone.
        assert ".sentinel/" not in migrated.splitlines()
        # `.claude/` still ignored (it's Claude Code's per-user cache).
        assert ".claude/" in migrated.splitlines()
        # Marker preserved so the block remains recognizable.
        assert "# sentinel artifacts — generated per-run, not source" in migrated
        # User's unrelated content preserved.
        assert "node_modules/" in migrated.splitlines()

    def test_reinit_leaves_user_sentinel_line_alone(
        self, fake_cli_env, isolated_home,
    ):
        """R5.2 migration must only strip `.sentinel/` from inside the
        sentinel-generated block. A `.sentinel/` line the user wrote
        elsewhere in their own .gitignore is their prerogative and must
        stay put."""
        user_owned = (
            ".sentinel/\n"  # user's own line, outside our block
            "node_modules/\n"
            "\n"
            "# sentinel artifacts — generated per-run, not source\n"
            ".claude/\n"
        )
        (isolated_home / ".gitignore").write_text(user_owned)

        fake_cli_env(claude=True)
        result = CliRunner().invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output

        after = (isolated_home / ".gitignore").read_text()
        # User's own `.sentinel/` line above our block is preserved.
        assert after.splitlines()[0] == ".sentinel/"
        # No duplicate block appended.
        assert after.count("# sentinel artifacts") == 1

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


class TestSentinelDirGitignore:
    """sentinel init writes .sentinel/.gitignore so runtime state (state/)
    never gets staged when a user does `git add .sentinel/`. Durable
    artifacts (config.toml, lenses.md, runs/, etc.) remain trackable."""

    def test_auto_init_via_work_writes_sentinel_gitignore(
        self, fake_cli_env, isolated_home,
    ):
        """Fresh `sentinel work --dry-run` auto-inits and must drop
        .sentinel/.gitignore excluding the ephemeral state/ dir.

        The downstream scan may fail against stub providers — we only
        care that the auto-init step ran and wrote the gitignore.
        """
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["work", "--dry-run"])

        gitignore_path = isolated_home / ".sentinel" / ".gitignore"
        assert gitignore_path.exists(), (
            "auto-init must create .sentinel/.gitignore"
        )
        contents = gitignore_path.read_text()
        assert "state/" in contents, (
            f"ephemeral state/ must be ignored; got:\n{contents}"
        )

    def test_init_writes_sentinel_gitignore(
        self, fake_cli_env, isolated_home,
    ):
        """Explicit `sentinel init` also writes the .sentinel/.gitignore."""
        fake_cli_env(claude=True)
        CliRunner().invoke(main, ["init", "--yes"])
        gitignore_path = isolated_home / ".sentinel" / ".gitignore"
        assert gitignore_path.exists()
        assert "state/" in gitignore_path.read_text()

    def test_does_not_overwrite_user_customized_gitignore(
        self, fake_cli_env, isolated_home,
    ):
        """If .sentinel/.gitignore already exists, init must leave it
        alone — user may have customized it."""
        fake_cli_env(claude=True)
        sentinel_dir = isolated_home / ".sentinel"
        sentinel_dir.mkdir()
        gitignore_path = sentinel_dir / ".gitignore"
        gitignore_path.write_text("# my custom rules\nfoo/\n")

        CliRunner().invoke(main, ["init", "--yes"])
        assert gitignore_path.read_text() == "# my custom rules\nfoo/\n"


class TestNoGoalsTemplate:
    """Sentinel reads whatever project docs exist — no dedicated goals file."""

    def test_init_does_not_create_goals_md(
        self, fake_cli_env, isolated_home,
    ):
        """Fresh init must not write .sentinel/goals.md.

        Project context is discovered from README/CLAUDE.md/docs via the
        LEARN phase. Creating a required-looking template file misleads
        users into thinking it's the source of truth.
        """
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--yes"])

        goals_path = isolated_home / ".sentinel" / "goals.md"
        assert not goals_path.exists(), (
            f"init should not create goals.md, but it exists at {goals_path}"
        )

    def test_work_does_not_nag_about_goals_md(
        self, fake_cli_env, isolated_home,
    ):
        """`sentinel work` must not print a 'fill in goals.md' warning.

        Before the dogfood cleanup, work printed a yellow 'Heads up:
        goals.md still has the default template' line on every run. That
        nag is gone — docs discovery handles project context.
        """
        fake_cli_env(claude=True, gemini=True)
        CliRunner().invoke(main, ["init", "--yes"])

        result = CliRunner().invoke(main, ["work", "--dry-run"])
        combined = (result.output or "") + (result.stderr or "")
        assert "goals.md" not in combined, (
            "work output should not mention goals.md at all; got:\n"
            + combined
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

    def test_reinit_preserves_user_created_goals(self, fake_cli_env, isolated_home):
        """If a user manually created .sentinel/goals.md, re-running init
        must not touch it. Init doesn't create this file itself, but if
        present it's user-owned content we must leave alone."""
        fake_cli_env(claude=True)
        CliRunner().invoke(main, ["init", "--yes"])

        goals_path = isolated_home / ".sentinel" / "goals.md"
        goals_path.write_text("# my custom goals\n")

        CliRunner().invoke(main, ["init", "--yes"])
        assert goals_path.read_text() == "# my custom goals\n"


# ---------- Doctrine 0002 — interactive-by-default wizard ----------

class TestDoctrine0002Defaults:
    """Fresh `sentinel init --yes` produces a config that satisfies
    Doctrine 0002's cross-provider review invariant: reviewer provider
    must differ from coder provider whenever a different provider is
    installed.
    """

    def test_yes_with_claude_and_codex_picks_codex_reviewer(
        self, fake_cli_env, isolated_home,
    ):
        """The doctrine-aligned default — when both claude and codex are
        installed, coder=claude, reviewer=codex (OPENAI provider name in
        the config).
        """
        fake_cli_env(claude=True, codex=True)
        result = CliRunner().invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        config = _read_config(isolated_home)
        assert config["roles"]["coder"]["provider"] == "claude"
        assert config["roles"]["reviewer"]["provider"] == "openai"

    def test_yes_with_claude_only_warns_about_same_provider(
        self, fake_cli_env, isolated_home,
    ):
        """Single-provider install: reviewer falls back to the coder's
        provider (claude). The wizard must print a warning rather than
        block, so the user can still proceed on a minimal setup.
        """
        fake_cli_env(claude=True)
        result = CliRunner().invoke(main, ["init", "--yes"])
        assert result.exit_code == 0
        config = _read_config(isolated_home)
        assert config["roles"]["coder"]["provider"] == "claude"
        assert config["roles"]["reviewer"]["provider"] == "claude"
        # Warning must surface — users need to know about the violation
        assert (
            "Doctrine 0002" in result.output
            or "cross-provider" in result.output
        )

    def test_yes_idempotent_on_existing_config(
        self, fake_cli_env, isolated_home,
    ):
        """`sentinel init --yes` called again on an initialized project
        is a no-op — the existing config is preserved.
        """
        fake_cli_env(claude=True, codex=True)
        CliRunner().invoke(main, ["init", "--yes"])
        config_path = isolated_home / ".sentinel" / "config.toml"
        first = config_path.read_text()

        # Hand-edit so we can detect any overwrite
        edited = first.replace(
            'daily_limit_usd = 15.0', 'daily_limit_usd = 42.0',
        )
        config_path.write_text(edited)

        result = CliRunner().invoke(main, ["init", "--yes"])
        assert result.exit_code == 0
        assert config_path.read_text() == edited, (
            "init --yes on existing config must not overwrite"
        )


class TestImplicitWorkWarning:
    """`sentinel work` on an uninitialized project auto-inits but must
    print a visible warning recommending `sentinel init`.
    """

    def test_work_prints_implicit_init_warning(
        self, fake_cli_env, isolated_home,
    ):
        fake_cli_env(claude=True, gemini=True)
        result = CliRunner().invoke(main, ["work", "--dry-run"])
        combined = (result.output or "") + (result.stderr or "")
        # The visible warning must be there, naming the explicit command
        assert "config.toml not found" in combined
        assert "sentinel init" in combined
        # And init did run — config exists afterwards
        assert (isolated_home / ".sentinel" / "config.toml").exists()


class TestInitFlagOverrides:
    """New flags `--providers` / `--coder` / `--reviewer` / `--budget`
    must take effect when passed explicitly (Doctrine 0002 §3 — flag
    precedence).
    """

    def test_explicit_providers_flag(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True, codex=True, gemini=True)
        result = CliRunner().invoke(
            main, ["init", "--yes", "--providers", "claude,codex"],
        )
        assert result.exit_code == 0, result.output
        config = _read_config(isolated_home)
        # Only claude + codex should be used; gemini/ollama must not
        # appear in any role assignment
        providers_in_use = {r["provider"] for r in config["roles"].values()}
        assert providers_in_use <= {"claude", "openai"}

    def test_explicit_coder_and_reviewer_flags(
        self, fake_cli_env, isolated_home,
    ):
        fake_cli_env(claude=True, codex=True, gemini=True)
        result = CliRunner().invoke(
            main,
            [
                "init", "--yes",
                "--coder", "claude:claude-sonnet-4-6",
                "--reviewer", "codex:gpt-5.4",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _read_config(isolated_home)
        assert config["roles"]["coder"]["provider"] == "claude"
        assert config["roles"]["coder"]["model"] == "claude-sonnet-4-6"
        assert config["roles"]["reviewer"]["provider"] == "openai"
        assert config["roles"]["reviewer"]["model"] == "gpt-5.4"

    def test_explicit_budget_flag(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True, codex=True)
        result = CliRunner().invoke(
            main, ["init", "--yes", "--budget", "42.0"],
        )
        assert result.exit_code == 0, result.output
        config = _read_config(isolated_home)
        assert config["budget"]["daily_limit_usd"] == 42.0

    def test_unknown_provider_name_rejected(
        self, fake_cli_env, isolated_home,
    ):
        fake_cli_env(claude=True)
        result = CliRunner().invoke(
            main, ["init", "--yes", "--providers", "nonexistent"],
        )
        assert result.exit_code != 0, (
            "unknown provider name should fail cleanly with a BadParameter"
        )

    def test_equivalent_flag_form_printed_on_wizard(
        self, fake_cli_env, isolated_home,
    ):
        """After `sentinel init --yes`, the equivalent single-line flag
        command is printed so the user can paste it into CI.
        """
        fake_cli_env(claude=True, codex=True)
        result = CliRunner().invoke(main, ["init", "--yes"])
        assert result.exit_code == 0
        assert "Equivalent to rerun" in result.output
        assert "sentinel init" in result.output
        assert "--providers" in result.output
        assert "--coder" in result.output
        assert "--reviewer" in result.output
        assert "--budget" in result.output


class TestImplicitInitSkipsWizardPrint:
    """Auto-init from `sentinel work` is not a wizard — the equivalent-
    flag-form line would be misleading there. Only explicit `sentinel
    init` prints it.
    """

    def test_implicit_init_does_not_print_rerun_line(
        self, fake_cli_env, isolated_home,
    ):
        fake_cli_env(claude=True, gemini=True)
        result = CliRunner().invoke(main, ["work", "--dry-run"])
        # work triggered auto-init; the "Equivalent to rerun" line is
        # for explicit-init output only.
        assert "Equivalent to rerun" not in (result.output or "")


class TestNonTTYDoesNotHang:
    """Non-TTY runs must not block on prompts — CliRunner simulates
    this by feeding a closed stdin.
    """

    def test_non_tty_init_completes(self, fake_cli_env, isolated_home):
        fake_cli_env(claude=True, codex=True)
        # No --yes, no input provided → wizard must still complete
        result = CliRunner().invoke(main, ["init"], input="")
        assert result.exit_code == 0, result.output
        assert (isolated_home / ".sentinel" / "config.toml").exists()
