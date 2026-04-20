"""Tests for the CLI surface awareness pre-load (Finding F7).

Cycle 5 of the autumn-mail dogfood produced a coder that emitted
``gws gmail +read <id>`` and ``gws gmail +reply <id>`` — the installed
gws 0.22.5 actually wants ``--id <ID>`` / ``--message-id <ID>``.
The reviewer caught it by dry-running the binary; sentinel should
have grounded the coder up-front by pre-loading ``--help`` text.

These tests cover:
  - Detection: an allowlisted CLI in the work item triggers ``--help``;
    a subcommand reference triggers an additional probe.
  - Prompt insertion: the captured output lands in the coder's prompt
    under the documented marker.
  - Allowlist enforcement: an unknown CLI is NOT shelled out to.
  - Fail-soft on missing CLIs: an allowlisted-but-uninstalled tool is
    silently skipped.
  - Fail-soft on subprocess failure: timeouts and other exceptions
    don't crash the cycle.
  - Subcommand probe limit: configured ``cli_help_max_subcommands``
    is honored.
"""

from __future__ import annotations

import subprocess

import pytest

from sentinel.config.schema import DEFAULT_CLI_HELP_ALLOWLIST, CoderConfig
from sentinel.roles.coder import (
    _build_cli_help_section,
    _capture_cli_help,
    _detect_cli_invocations,
)
from sentinel.roles.planner import WorkItem


def _make_work_item(
    *,
    title: str = "Wire gws into the inbox view",
    description: str = "",
    files: list | None = None,
    acceptance_criteria: list[str] | None = None,
    kind: str = "refine",
) -> WorkItem:
    return WorkItem(
        id="t1",
        title=title,
        description=description or "Default description.",
        type="feature",
        priority="high",
        complexity=2,
        files=files or [],
        acceptance_criteria=acceptance_criteria or [],
        kind=kind,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetectCliInvocations:
    def test_top_level_cli_in_text(self) -> None:
        probes = _detect_cli_invocations(
            "Use gws to fetch the message.",
            allowlist={"gws"},
            max_subcommands=3,
        )
        assert probes == [("gws",)]

    def test_subcommand_pair_recorded(self) -> None:
        """``gws gmail +read`` should produce both ``gws --help`` AND
        ``gws gmail +read --help`` so the coder learns the global flag
        set plus the subcommand-specific shape."""
        probes = _detect_cli_invocations(
            "shell out to gws gmail +read",
            allowlist={"gws"},
            max_subcommands=3,
        )
        # Top-level probe is always first.
        assert probes[0] == ("gws",)
        # Subcommand probe captured both tokens after `gws`.
        assert ("gws", "gmail", "+read") in probes

    def test_unknown_cli_dropped(self) -> None:
        """Allowlist enforcement — security: prevents work-item text
        from injecting ``rm -rf`` style "tools" into a shell-out."""
        probes = _detect_cli_invocations(
            "Run rm -rf the universe",
            allowlist={"gws"},
            max_subcommands=3,
        )
        assert probes == []

    def test_dedup_top_level(self) -> None:
        probes = _detect_cli_invocations(
            "swift build, swift test, then swift run",
            allowlist={"swift"},
            max_subcommands=3,
        )
        # `swift` appears exactly once at the top of the probe list.
        assert probes[0] == ("swift",)
        assert sum(1 for p in probes if p == ("swift",)) == 1

    def test_subcommand_cap_honored(self) -> None:
        text = (
            "swift build, swift test, swift package update, "
            "swift run app, swift package init"
        )
        probes = _detect_cli_invocations(
            text, allowlist={"swift"}, max_subcommands=2,
        )
        sub_probes = [p for p in probes if len(p) > 1]
        assert len(sub_probes) == 2

    def test_empty_allowlist_returns_empty(self) -> None:
        """Empty allowlist disables the feature for that project."""
        probes = _detect_cli_invocations(
            "use gws and swift", allowlist=set(), max_subcommands=3,
        )
        assert probes == []


# ---------------------------------------------------------------------------
# Subprocess capture (fail-soft contract)
# ---------------------------------------------------------------------------


class TestCaptureCliHelp:
    def test_captures_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="usage: gws [opts]\n  --id ID\n",
                stderr="",
            )
        monkeypatch.setattr(subprocess, "run", fake_run)
        out = _capture_cli_help(("gws",), timeout_sec=5)
        assert out is not None
        assert "--id ID" in out

    def test_falls_back_to_stderr_when_stdout_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Some tools (busybox-style) emit `--help` to stderr."""
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="",
                stderr="usage info on stderr\n",
            )
        monkeypatch.setattr(subprocess, "run", fake_run)
        out = _capture_cli_help(("foo",), timeout_sec=5)
        assert out is not None
        assert "usage info on stderr" in out

    def test_timeout_returns_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hanging help fetch must be caught — never block the cycle."""
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
        monkeypatch.setattr(subprocess, "run", fake_run)
        out = _capture_cli_help(("gws",), timeout_sec=5)
        assert out is None

    def test_oserror_returns_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OSError (e.g. binary disappeared between which() and run())
        must not propagate."""
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            raise OSError("binary went away")
        monkeypatch.setattr(subprocess, "run", fake_run)
        out = _capture_cli_help(("gws",), timeout_sec=5)
        assert out is None

    def test_truncates_long_output(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A multi-thousand-line help block must not balloon the prompt."""
        from sentinel.roles.coder import CLI_HELP_MAX_LINES
        big = "\n".join(f"line {i}" for i in range(CLI_HELP_MAX_LINES * 3))
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=big, stderr="",
            )
        monkeypatch.setattr(subprocess, "run", fake_run)
        out = _capture_cli_help(("gws",), timeout_sec=5)
        assert out is not None
        # Should include exactly CLI_HELP_MAX_LINES + 1 truncation marker.
        assert out.count("\n") == CLI_HELP_MAX_LINES
        assert "truncated" in out


# ---------------------------------------------------------------------------
# Prompt assembly (the integration point)
# ---------------------------------------------------------------------------


class TestBuildCliHelpSection:
    def test_disabled_when_no_config(self) -> None:
        """Legacy callers (no coder_config kwarg) get an empty section."""
        wi = _make_work_item(acceptance_criteria=["use gws"])
        assert _build_cli_help_section(wi, coder_config=None) == ""

    def test_disabled_when_allowlist_empty(self) -> None:
        wi = _make_work_item(acceptance_criteria=["use gws"])
        cfg = CoderConfig(cli_help_allowlist=[])
        assert _build_cli_help_section(wi, coder_config=cfg) == ""

    def test_help_section_emitted_for_allowlisted_installed_cli(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Spec acceptance: work item with `files: [...swift]` and
        acceptance_criteria mentioning `gws gmail +list` must trigger
        BOTH `gws --help` AND `gws gmail +list --help`, and both
        outputs must appear in the prompt."""
        wi = _make_work_item(
            files=["Sources/X.swift"],
            acceptance_criteria=["shell out to gws gmail +list"],
        )

        # Pretend `gws` is installed (Sources/X.swift triggers `swift`
        # detection, but we keep `swift` un-installed to scope the test).
        def fake_which(cmd: str) -> str | None:
            return "/usr/local/bin/gws" if cmd == "gws" else None
        monkeypatch.setattr("sentinel.roles.coder.shutil.which", fake_which)

        captured_calls: list[list[str]] = []
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            captured_calls.append(list(cmd))
            joined = " ".join(cmd[:-1])  # drop --help for the marker
            return subprocess.CompletedProcess(
                cmd, returncode=0,
                stdout=f"USAGE: {joined} [OPTIONS]\n  --id ID\n",
                stderr="",
            )
        monkeypatch.setattr(subprocess, "run", fake_run)

        cfg = CoderConfig()  # default allowlist includes `gws`
        section = _build_cli_help_section(wi, coder_config=cfg)

        # Both probes appear in the prompt section.
        assert "## Installed CLI surfaces" in section
        assert "### gws --help" in section
        assert "### gws gmail +list --help" in section
        # Captured help text is in there.
        assert "USAGE: gws" in section
        assert "USAGE: gws gmail +list" in section

        # Subprocess was called for both probes.
        invoked = [tuple(c) for c in captured_calls]
        assert ("gws", "--help") in invoked
        assert ("gws", "gmail", "+list", "--help") in invoked
        # `swift --help` was NOT invoked (swift not "installed").
        assert not any(c[0] == "swift" for c in invoked)

    def test_unknown_cli_not_shelled_out(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`fake-tool-xyz` is not in the allowlist — even if installed,
        it must not be shelled out to. Defense against work-item text
        injecting tool names."""
        wi = _make_work_item(
            title="Add a button",  # no allowlisted CLI in title
            description="A new feature.",
            acceptance_criteria=["use fake-tool-xyz to do the thing"],
        )
        # Pretend everything is installed.
        monkeypatch.setattr(
            "sentinel.roles.coder.shutil.which", lambda _c: "/bin/true",
        )
        called: list = []
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            called.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        monkeypatch.setattr(subprocess, "run", fake_run)

        cfg = CoderConfig()  # default allowlist has no `fake-tool-xyz`
        section = _build_cli_help_section(wi, coder_config=cfg)

        assert section == ""
        assert called == []

    def test_uninstalled_cli_silently_skipped(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Allowlisted but `shutil.which` returns None → no subprocess
        call, empty section."""
        wi = _make_work_item(acceptance_criteria=["use gws to call gmail"])
        monkeypatch.setattr(
            "sentinel.roles.coder.shutil.which", lambda _c: None,
        )
        called: list = []
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            called.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        monkeypatch.setattr(subprocess, "run", fake_run)

        cfg = CoderConfig()
        assert _build_cli_help_section(wi, coder_config=cfg) == ""
        assert called == []

    def test_timeout_does_not_block_cycle(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mock subprocess that hangs (TimeoutExpired) → returns empty
        section, no exception escapes."""
        wi = _make_work_item(acceptance_criteria=["use gws"])
        monkeypatch.setattr(
            "sentinel.roles.coder.shutil.which",
            lambda _c: "/usr/local/bin/gws",
        )
        def fake_run(cmd, **kwargs):  # noqa: ARG001
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
        monkeypatch.setattr(subprocess, "run", fake_run)

        cfg = CoderConfig()
        # Must not raise.
        section = _build_cli_help_section(wi, coder_config=cfg)
        assert section == ""

    def test_default_allowlist_matches_spec(self) -> None:
        """Lock the documented default allowlist against silent drift."""
        for tool in ("gws", "swift", "go", "cargo", "node", "npm",
                     "uv", "pip", "pytest", "ruff", "mypy"):
            assert tool in DEFAULT_CLI_HELP_ALLOWLIST


# ---------------------------------------------------------------------------
# End-to-end via Coder.execute (verifies prompt prepend wiring)
# ---------------------------------------------------------------------------


class TestCoderPromptIncludesHelpSection:
    """The piece the coder actually sees: when `coder_config` is threaded
    in, the prompt sent to the provider starts with the help section."""

    @pytest.mark.asyncio
    async def test_prompt_prepends_help_section(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ) -> None:
        from unittest.mock import MagicMock

        from sentinel.providers.interface import ChatResponse
        from sentinel.roles.coder import Coder

        wi = _make_work_item(
            files=["Sources/X.swift"],
            acceptance_criteria=["shell out to gws gmail +list"],
            # Expand kind so the refinement-grounding check (which
            # would reject a non-existent Sources/X.swift on HEAD)
            # doesn't short-circuit before the prompt is built.
            kind="expand",
        )

        # Stub the workspace as a clean git repo so pre/post snapshots
        # don't blow up. The Coder doesn't need to commit anything for
        # this test — we assert on the captured prompt before that.
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "t@e.com"],
            cwd=tmp_path, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=tmp_path, check=True,
        )
        (tmp_path / "README.md").write_text("seed\n")
        subprocess.run(
            ["git", "add", "README.md"], cwd=tmp_path, check=True,
        )
        subprocess.run(
            ["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True,
        )

        # Stub the help subprocess (gws installed; subprocess returns
        # something distinctive we can grep for in the prompt). We must
        # patch shutil.which *before* the help builder runs.
        def fake_which(cmd: str) -> str | None:
            return "/usr/local/bin/gws" if cmd == "gws" else None
        monkeypatch.setattr("sentinel.roles.coder.shutil.which", fake_which)

        original_run = subprocess.run
        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[-1] == "--help":
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout="GWS_HELP_MARKER\n  --id ID\n  --format json\n",
                    stderr="",
                )
            return original_run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", fake_run)

        # Mock the provider so we capture the prompt and don't actually
        # spawn an agentic CLI. The Coder will still hit the post-call
        # snapshot path; that's fine — we assert before asserting any
        # of that.
        captured_prompts: list[str] = []
        class _StubProvider:
            name = "stub"
            class _Caps:
                agentic_code = True
            capabilities = _Caps()

            async def code(self, prompt: str, *, working_directory: str):  # noqa: ARG002
                captured_prompts.append(prompt)
                return ChatResponse(
                    content="ok", is_error=False,
                    cost_usd=0.0, model="stub",
                    input_tokens=0, output_tokens=0,
                )

        router = MagicMock()
        router.get_provider = MagicMock(return_value=_StubProvider())

        coder = Coder(router)
        cfg = CoderConfig()
        await coder.execute(
            wi,
            working_directory=str(tmp_path),
            artifacts_directory=str(tmp_path),
            branch="main",
            coder_config=cfg,
        )

        # The prompt the provider received must start with the
        # CLI-surface section, before the standard BUILD_PROMPT body.
        assert captured_prompts, "provider was never called"
        prompt = captured_prompts[0]
        assert "## Installed CLI surfaces" in prompt
        assert "GWS_HELP_MARKER" in prompt
        # And the standard work-item header still appears AFTER the help.
        assert prompt.index("## Installed CLI surfaces") < prompt.index(
            "## Work Item",
        )
