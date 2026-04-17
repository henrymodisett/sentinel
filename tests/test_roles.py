"""Tests for the roles layer (monitor, coder, reviewer).

Mock the Provider interface to test role logic without real LLM calls.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentinel.providers.interface import (
    ChatResponse,
    Provider,
    ProviderCapabilities,
    ProviderName,
)
from sentinel.roles.coder import Coder, _slug
from sentinel.roles.monitor import EXPLORE_SCHEMA, Monitor
from sentinel.roles.planner import WorkItem
from sentinel.roles.reviewer import Reviewer
from sentinel.state import ProjectState

# --- Mock Provider ---

class MockProvider(Provider):
    name = ProviderName.CLAUDE
    cli_command = "mock"
    capabilities = ProviderCapabilities(
        chat=True, agentic_code=True, web_search=True,
    )

    def __init__(self, chat_responses=None, json_responses=None, code_response=None):
        self.chat_responses = chat_responses or []
        self.json_responses = json_responses or []
        self.code_response = code_response
        self.chat_calls = []
        self.chat_json_calls = []
        self.code_calls = []

    async def chat(self, prompt, system_prompt=None):
        self.chat_calls.append((prompt, system_prompt))
        if self.chat_responses:
            return self.chat_responses.pop(0)
        return ChatResponse(content="mock", provider=self.name)

    async def chat_json(self, prompt, schema, system_prompt=None):
        self.chat_json_calls.append((prompt, schema))
        if self.json_responses:
            return self.json_responses.pop(0)
        return None, ChatResponse(content="mock", provider=self.name)

    async def code(self, prompt, options=None, **kwargs):
        self.code_calls.append(prompt)
        return self.code_response or ChatResponse(
            content="mock code run", provider=self.name,
        )

    def detect(self):
        from sentinel.providers.interface import ProviderStatus
        return ProviderStatus(installed=True, authenticated=True)


def _mock_router(provider):
    router = MagicMock()
    router.get_provider.return_value = provider

    async def _chat(role, prompt):
        return await provider.chat(prompt)

    router.chat = _chat
    return router


# --- Monitor Tests ---

class TestMonitorSchema:
    def test_explore_schema_requires_lenses(self) -> None:
        assert "lenses" in EXPLORE_SCHEMA["required"]
        assert "project_summary" in EXPLORE_SCHEMA["required"]

    def test_lens_schema_has_required_fields(self) -> None:
        lens_schema = EXPLORE_SCHEMA["properties"]["lenses"]["items"]
        required = lens_schema["required"]
        assert "name" in required
        assert "description" in required
        assert "what_to_look_for" in required


class TestExplorePromptBuilder:
    """Regression: the locked-lens code path used to format EXPLORE_PROMPT
    with a stale argument set and raise KeyError the moment a new template
    field was added."""

    def test_populates_installed_tools_field(self) -> None:
        from sentinel.roles.monitor import _build_explore_prompt
        state = ProjectState(path="/tmp/fake", name="fake")
        state.installed_tools = "vcs: gh, git"
        rendered = _build_explore_prompt(state)
        assert "Available tools" in rendered
        assert "vcs: gh, git" in rendered

    def test_handles_missing_installed_tools(self) -> None:
        """Empty installed_tools should render the fallback sentinel, not raise."""
        from sentinel.roles.monitor import _build_explore_prompt
        state = ProjectState(path="/tmp/fake", name="fake")
        rendered = _build_explore_prompt(state)
        assert "not probed" in rendered


class TestMonitorFailsLoudly:
    @pytest.mark.asyncio
    async def test_fails_when_no_lenses_generated(self) -> None:
        """If the first LLM call doesn't return structured lenses, scan fails loudly."""
        provider = MockProvider(json_responses=[
            (None, ChatResponse(content="bad output", provider=ProviderName.CLAUDE)),
        ])
        router = _mock_router(provider)
        monitor = Monitor(router)
        state = ProjectState(name="test", path="/tmp/test")

        result = await monitor.assess(state)
        assert not result.ok
        assert result.error is not None
        assert "failed" in result.error.lower() or "lens" in result.error.lower()


# --- Coder Tests ---

def _coder_kwargs(tmpdir: str, branch: str = "test/coder-branch") -> dict:
    """Set up the worktree-managed kwargs Coder.execute requires.

    Mimics what `worktree_for(...)` does for the real call site:
    creates the branch and checks it out, then returns the kwargs to
    pass through. Tests that don't care about a real git repo can
    skip this helper — the few tests that do their own repo setup
    just need the kwargs dict shape.
    """
    import subprocess as _sp
    _sp.run(
        ["git", "checkout", "-b", branch], cwd=tmpdir,
        check=True, capture_output=True,
    )
    return {
        "working_directory": tmpdir,
        "artifacts_directory": tmpdir,
        "branch": branch,
    }


class TestCoderSlug:
    def test_simple_title(self) -> None:
        assert _slug("Fix bug in parser") == "fix-bug-in-parser"

    def test_special_characters(self) -> None:
        assert _slug("Add CI/CD pipeline!") == "add-ci-cd-pipeline"

    def test_caps_to_length(self) -> None:
        s = _slug("A" * 100)
        assert len(s) <= 50

    def test_strips_trailing_dashes(self) -> None:
        assert not _slug("Title with !!!").endswith("-")


class TestCoderRejectsNonAgenticProvider:
    @pytest.mark.asyncio
    async def test_rejects_provider_without_agentic_code(self) -> None:
        provider = MockProvider()
        provider.capabilities = ProviderCapabilities(
            chat=True, agentic_code=False,
        )
        router = _mock_router(provider)
        coder = Coder(router)

        work_item = WorkItem(
            id="1", title="test", description="",
            type="chore", priority="low", complexity=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            # Capability check fires before any git ops — bare kwargs OK
            result = await coder.execute(
                work_item,
                working_directory=tmpdir,
                artifacts_directory=tmpdir,
                branch="test/no-branch-needed",
            )
            assert result.status == "failed"
            assert "agentic_code" in (result.error or "") or "claude" in (
                result.error or ""
            ).lower()


class TestCommitFilesPerPathResilience:
    """Regression: dogfood on portfolio_new (2026-04-16) showed
    `git add a b c d` aborts the entire batch on a single bad path,
    losing all the valid files. _commit_files now stages each path
    individually so one bad file is a warning, not a destroyed commit."""

    def test_one_bad_path_does_not_abort_entire_commit(self) -> None:
        import subprocess as _sp

        from sentinel.roles.coder import _commit_files
        from sentinel.roles.planner import WorkItem

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "config", "user.email", "t@t.io"], cwd=tmpdir,
                check=True,
            )
            _sp.run(["git", "config", "user.name", "t"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=tmpdir, check=True,
            )

            # Two valid files actually exist, one path doesn't
            (Path(tmpdir) / "real-1.py").write_text("a")
            (Path(tmpdir) / "real-2.py").write_text("b")
            files = ["real-1.py", "real-2.py", "imaginary.py"]

            work_item = WorkItem(
                id="t-1", title="resilience test", description="",
                type="fix", priority="low", complexity=1,
            )
            ok, sha = _commit_files(tmpdir, files, work_item)

            assert ok, f"commit should succeed despite one bad path; got error: {sha}"
            # Both real files landed in the commit
            show = _sp.run(
                ["git", "show", "--name-only", "--pretty=format:", sha],
                capture_output=True, text=True, cwd=tmpdir,
            ).stdout
            assert "real-1.py" in show
            assert "real-2.py" in show
            # The bad path is absent
            assert "imaginary.py" not in show

    def test_all_bad_paths_returns_failure(self) -> None:
        """If EVERY path fails, surface the first failure (they
        probably share a root cause) rather than committing nothing
        with a misleading success."""
        import subprocess as _sp

        from sentinel.roles.coder import _commit_files
        from sentinel.roles.planner import WorkItem

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "config", "user.email", "t@t.io"], cwd=tmpdir,
                check=True,
            )
            _sp.run(["git", "config", "user.name", "t"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=tmpdir, check=True,
            )

            work_item = WorkItem(
                id="t-2", title="all bad", description="",
                type="fix", priority="low", complexity=1,
            )
            ok, err = _commit_files(
                tmpdir, ["nope1.py", "nope2.py"], work_item,
            )
            assert ok is False
            assert "nope1.py" in err  # first failure surfaced


class TestGitStatusSnapshotPreservesFullPath:
    """Regression: dogfood on portfolio_new (2026-04-16) showed
    `_git_status_snapshot` mangling paths — `src/foo.tsx` came back
    as `rc/foo.tsx`, `principles/README.md.bak` as `rinciples/...`.
    Every path lost its first character. Cause was a fragile line-
    based parser in a region of git/locale state we couldn't fully
    reproduce. The fix uses `git status --porcelain -z` (NUL-
    terminated) so path boundaries are explicit, not whitespace-
    derived. This test locks the contract in across the file states
    we care about: untracked, modified, and modified+staged.
    """

    def test_full_path_preserved_for_untracked_file(self) -> None:
        import subprocess as _sp

        from sentinel.roles.coder import _git_status_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
            (Path(tmpdir) / "src" / "app").mkdir(parents=True)
            (Path(tmpdir) / "src" / "app" / "MasonryGallery.tsx").write_text("x")
            paths = _git_status_snapshot(tmpdir)
            assert "src/app/MasonryGallery.tsx" in paths, (
                f"untracked path mangled — got {paths}"
            )
            assert not any(p.startswith("rc/") for p in paths)

    def test_full_path_preserved_for_modified_tracked_file(self) -> None:
        import subprocess as _sp

        from sentinel.roles.coder import _git_status_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "config", "user.email", "t@t.io"], cwd=tmpdir,
                check=True,
            )
            _sp.run(["git", "config", "user.name", "t"], cwd=tmpdir, check=True)
            (Path(tmpdir) / "principles").mkdir()
            f = Path(tmpdir) / "principles" / "README.md.bak"
            f.write_text("v1")
            _sp.run(["git", "add", "."], cwd=tmpdir, check=True)
            _sp.run(["git", "commit", "-m", "init", "-q"], cwd=tmpdir, check=True)
            f.write_text("v2")
            paths = _git_status_snapshot(tmpdir)
            assert "principles/README.md.bak" in paths
            assert not any(p.startswith("rinciples/") for p in paths)

    def test_rename_returns_both_old_and_new_paths(self) -> None:
        """We pass `--no-renames` to git status so each side of a
        rename is its own entry: ` D old.py` + `?? new.py` (or
        equivalent). _commit_files needs BOTH so the commit captures
        the deletion AND the new file — without both, codex flagged
        that renames would ship as copies."""
        import subprocess as _sp

        from sentinel.roles.coder import _git_status_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "config", "user.email", "t@t.io"], cwd=tmpdir,
                check=True,
            )
            _sp.run(["git", "config", "user.name", "t"], cwd=tmpdir, check=True)
            (Path(tmpdir) / "old.py").write_text("x")
            _sp.run(["git", "add", "."], cwd=tmpdir, check=True)
            _sp.run(["git", "commit", "-m", "init", "-q"], cwd=tmpdir, check=True)
            _sp.run(["git", "mv", "old.py", "new.py"], cwd=tmpdir, check=True)
            paths = _git_status_snapshot(tmpdir)
            assert "new.py" in paths
            assert "old.py" in paths, (
                "rename's old path must also be in the snapshot so "
                "_commit_files stages the deletion alongside the new file"
            )


class TestFilesChangedIgnoresSentinelArtifacts:
    """Regression: transcripts and other .sentinel/ artifacts must never
    be counted as Coder changes. Otherwise a no-op Coder run shows up as
    successful because the transcript it just wrote looks like a diff."""

    def test_excludes_sentinel_paths_from_change_detection(self) -> None:
        import subprocess as _sp

        from sentinel.roles.coder import _files_changed

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "-c", "user.email=a@b", "-c", "user.name=t",
                 "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=tmpdir, check=True,
            )
            # Write an execution transcript — looks like a change to git
            sentinel_dir = Path(tmpdir) / ".sentinel" / "executions"
            sentinel_dir.mkdir(parents=True)
            (sentinel_dir / "2026-01-01-dummy.md").write_text("# fake")
            # And a real change in project code
            (Path(tmpdir) / "real.py").write_text("print('hi')")

            changed = _files_changed(tmpdir)
            assert "real.py" in changed
            assert not any(f.startswith(".sentinel/") for f in changed), (
                f"_files_changed returned sentinel artifacts: {changed}"
            )


class TestCoderCommitsToFeatureBranch:
    """Before this PR, Coder created a branch, edited files, ran tests,
    and returned status=success — but never committed. The diff lived
    in the working tree only, and the next item's checkout silently
    failed on the dirty tree, commingling edits. Sigint dogfood showed
    4 branches with 0 commits each. Fix verified by this test."""

    @pytest.mark.asyncio
    async def test_successful_execution_commits_to_branch(self) -> None:
        """Claude writes a file, tests pass → Coder commits it. The
        feature branch now has a real commit pointing at the diff."""
        import subprocess as _sp

        # Mock provider that pretends Claude wrote a file to the tree
        # in the caller's cwd. We run the test with cwd=tmpdir so the
        # effect is scoped.
        class FileWritingMock(MockProvider):
            async def code(self, prompt, options=None, **kwargs):
                self.code_calls.append(prompt)
                # Simulate Claude editing a file in the target repo
                wd = kwargs.get("working_directory", ".")
                (Path(wd) / "fixed.py").write_text("print('fixed')\n")
                return ChatResponse(
                    content="done", provider=self.name, cost_usd=0.01,
                )

        provider = FileWritingMock()
        provider.capabilities = ProviderCapabilities(
            chat=True, agentic_code=True,
        )
        router = _mock_router(provider)
        coder = Coder(router)

        work_item = WorkItem(
            id="cycle-1", title="fix the thing", description="it was broken",
            type="fix", priority="high", complexity=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "-c", "user.email=a@b", "-c", "user.name=t",
                 "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=tmpdir, check=True,
            )
            init_sha = _sp.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, cwd=tmpdir,
            ).stdout.strip()

            result = await coder.execute(work_item, **_coder_kwargs(tmpdir))

            assert result.status in ("success", "partial")
            # The fix: an actual commit exists on the feature branch
            assert result.commit_sha, "Coder must record a commit SHA"
            assert result.commit_sha != init_sha, (
                "commit_sha must point at a new commit, not the init commit"
            )
            log = _sp.run(
                ["git", "log", "--oneline", result.branch],
                capture_output=True, text=True, cwd=tmpdir,
            )
            assert "fix the thing" in log.stdout, (
                f"expected commit on feature branch, got: {log.stdout!r}"
            )

    @pytest.mark.asyncio
    async def test_commits_even_when_tests_fail(self) -> None:
        """Partial success (coded but tests fail) must still commit —
        reviewer needs a real diff to give useful changes-requested
        feedback. A vaporized diff is worse than a failing one."""
        import subprocess as _sp

        class FileWritingMock(MockProvider):
            async def code(self, prompt, options=None, **kwargs):
                wd = kwargs.get("working_directory", ".")
                (Path(wd) / "partial.py").write_text("broken = 1\n")
                return ChatResponse(
                    content="done", provider=self.name, cost_usd=0.01,
                )

        provider = FileWritingMock()
        provider.capabilities = ProviderCapabilities(
            chat=True, agentic_code=True,
        )
        router = _mock_router(provider)
        coder = Coder(router)

        work_item = WorkItem(
            id="2", title="attempt fix", description="",
            type="fix", priority="low", complexity=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "-c", "user.email=a@b", "-c", "user.name=t",
                 "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=tmpdir, check=True,
            )
            # Configure a toolkit test_command that always fails
            (Path(tmpdir) / ".toolkit-config").write_text(
                "test_command=false\n",
            )
            result = await coder.execute(work_item, **_coder_kwargs(tmpdir))

            # Partial = code landed but tests failed
            assert result.status == "partial"
            assert result.commit_sha, (
                "must commit even on test-fail so reviewer has real diff"
            )


class TestCoderCommitPathspecIsolation:
    """Codex review caught: _commit_files() ran `git add` + `git commit -m`
    without a pathspec, so anything in the user's staged index got
    swept into the sentinel commit. Now we use `git commit -- files`
    which commits only those paths regardless of index state."""

    @pytest.mark.asyncio
    async def test_does_not_include_pre_staged_unrelated_files(self) -> None:
        """Scenario: Claude writes a fix, but something else is already
        staged in the index (e.g. from a prior partial operation).
        The sentinel commit must NOT include those pre-staged files."""
        import subprocess as _sp

        class FileWritingMock(MockProvider):
            async def code(self, prompt, options=None, **kwargs):
                wd = kwargs.get("working_directory", ".")
                (Path(wd) / "fixed.py").write_text("fixed\n")
                return ChatResponse(
                    content="done", provider=self.name, cost_usd=0.0,
                )

        provider = FileWritingMock()
        provider.capabilities = ProviderCapabilities(
            chat=True, agentic_code=True,
        )
        router = _mock_router(provider)
        coder = Coder(router)
        work_item = WorkItem(
            id="1", title="isolate the commit", description="",
            type="fix", priority="low", complexity=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "-c", "user.email=a@b", "-c", "user.name=t",
                 "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=tmpdir, check=True,
            )
            # Pre-stage an unrelated file — simulates user's in-progress
            # work or stray coder-agent staging outside our filter.
            (Path(tmpdir) / "unrelated_staged.py").write_text("staged\n")
            _sp.run(
                ["git", "add", "unrelated_staged.py"],
                cwd=tmpdir, check=True,
            )

            result = await coder.execute(work_item, **_coder_kwargs(tmpdir))

            # Commit landed
            assert result.commit_sha
            # But only contains fixed.py — the pre-staged file is absent
            show = _sp.run(
                ["git", "show", "--name-only", "--pretty=format:", result.commit_sha],
                capture_output=True, text=True, cwd=tmpdir,
            ).stdout.strip()
            assert "fixed.py" in show
            assert "unrelated_staged.py" not in show, (
                f"sentinel commit must not sweep pre-staged files; got:\n{show}"
            )


class TestWorkingTreeGuard:
    """Codex review caught: work_cmd ran _reset_and_checkout before
    the first item, wiping any user-uncommitted changes. Now we
    refuse to start on a dirty tree."""

    def test_detects_dirty_tree(self, tmp_path) -> None:
        import subprocess as _sp

        from sentinel.cli.work_cmd import _working_tree_clean

        _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        _sp.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=tmp_path, check=True,
        )
        assert _working_tree_clean(tmp_path) is True

        # Add a tracked-file modification
        (tmp_path / "file.py").write_text("x\n")
        _sp.run(["git", "add", "file.py"], cwd=tmp_path, check=True)
        _sp.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t",
             "commit", "-m", "add", "-q"],
            cwd=tmp_path, check=True,
        )
        (tmp_path / "file.py").write_text("x\nmodified\n")
        assert _working_tree_clean(tmp_path) is False

    def test_rejects_user_untracked_files(self, tmp_path) -> None:
        """Codex round 3: untracked files outside .sentinel/ and
        .claude/ must count as dirty, because `git clean -fd` between
        items would wipe them."""
        import subprocess as _sp

        from sentinel.cli.work_cmd import _working_tree_clean

        _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        _sp.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=tmp_path, check=True,
        )
        (tmp_path / "user_wip.md").write_text("user scratch\n")
        assert _working_tree_clean(tmp_path) is False, (
            "user's untracked files must block start — clean -fd would wipe them"
        )

    def test_allows_sentinel_artifacts_as_untracked(self, tmp_path) -> None:
        """Sentinel's own artifacts (.sentinel/, .claude/) don't count
        as dirty — clean -fd excludes them, they're sentinel's own."""
        import subprocess as _sp

        from sentinel.cli.work_cmd import _working_tree_clean

        _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        _sp.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=tmp_path, check=True,
        )
        (tmp_path / ".sentinel").mkdir()
        (tmp_path / ".sentinel" / "config.toml").write_text("x\n")
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "agent.md").write_text("x\n")
        assert _working_tree_clean(tmp_path) is True


class TestResetAndCheckoutReturnCodes:
    """Codex review caught: `_reset_and_checkout()` ignored return
    codes. A failed checkout would silently leave the loop running
    on the wrong branch — exactly the bug this PR is supposed to fix."""

    def test_returns_false_when_checkout_fails(self, tmp_path) -> None:
        from sentinel.cli.work_cmd import _reset_and_checkout

        # Not a git repo — checkout will fail
        assert _reset_and_checkout(str(tmp_path), "nonexistent-branch") is False

    def test_returns_true_on_clean_success(self, tmp_path) -> None:
        import subprocess as _sp

        from sentinel.cli.work_cmd import _reset_and_checkout

        _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        _sp.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=tmp_path, check=True,
        )
        assert _reset_and_checkout(str(tmp_path), "main") is True


class TestCoderPersistsTranscripts:
    """Every execution attempt — success, failure, or exception —
    must leave a debuggable record behind. Before this PR, bare
    `Error: ` failures produced no trace at all."""

    @pytest.mark.asyncio
    async def test_writes_transcript_for_empty_error_response(self) -> None:
        """The exact failure mode we saw on sigint: claude returns
        content='Error: ' with empty stderr. The transcript must
        exist so the user can still see what the provider sent."""
        provider = MockProvider(code_response=ChatResponse(
            content="Error: ",
            provider=ProviderName.CLAUDE,
            is_error=True,
            stderr="max turns reached",
            raw_stdout='{"is_error": true, "result": ""}',
        ))
        provider.capabilities = ProviderCapabilities(
            chat=True, agentic_code=True,
        )
        router = _mock_router(provider)
        coder = Coder(router)

        work_item = WorkItem(
            id="1", title="demo item", description="do a thing",
            type="fix", priority="high", complexity=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            # Init an empty git repo so branch creation succeeds
            import subprocess as _sp
            _sp.run(["git", "init", "-q"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "-c", "user.email=a@b", "-c", "user.name=t",
                 "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=tmpdir, check=True,
            )

            result = await coder.execute(work_item, **_coder_kwargs(tmpdir))
            assert result.status == "failed"
            # The fix: transcript exists on disk
            transcripts = list(
                (Path(tmpdir) / ".sentinel" / "executions").glob("*.md"),
            )
            assert len(transcripts) == 1, "execution must leave a transcript"
            body = transcripts[0].read_text(encoding="utf-8")
            # Stderr surfaces in the transcript even when content was empty
            assert "max turns reached" in body
            # Raw stdout is preserved for post-hoc JSON diffing
            assert "is_error" in body
            # And the error string now mentions what really happened,
            # not just "Error: "
            assert result.error and result.error != "Error: "

    @pytest.mark.asyncio
    async def test_empty_error_surfaces_stderr_in_result(self) -> None:
        """Regression: Coder used to set `result.error = "Error: "` and
        throw away stderr. Now stderr is appended to the error surfaced
        upward so the cycle-level output is informative."""
        provider = MockProvider(code_response=ChatResponse(
            content="Error: ",
            provider=ProviderName.CLAUDE,
            is_error=True,
            stderr="authentication failed — run `claude auth login`",
            raw_stdout="",
        ))
        provider.capabilities = ProviderCapabilities(
            chat=True, agentic_code=True,
        )
        router = _mock_router(provider)
        coder = Coder(router)

        work_item = WorkItem(
            id="2", title="another", description="",
            type="fix", priority="low", complexity=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            import subprocess as _sp
            _sp.run(["git", "init", "-q"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "-c", "user.email=a@b", "-c", "user.name=t",
                 "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=tmpdir, check=True,
            )
            result = await coder.execute(work_item, **_coder_kwargs(tmpdir))
            assert "authentication failed" in (result.error or "")


class TestCoderWorktreeManagedMode:
    """When the worktree layer manages the branch and the artifacts
    directory is separated from the working directory, Coder must
    operate on the supplied branch (no checkout) and write its
    transcript to the artifacts directory (not the worktree, which
    would vanish on cleanup)."""

    @pytest.mark.asyncio
    async def test_uses_supplied_branch_does_not_checkout(self) -> None:
        """When `branch` is passed, Coder trusts the caller — no
        `checkout -b`. The recorded result.branch matches what the
        caller passed."""
        import subprocess as _sp

        class FileWritingMock(MockProvider):
            async def code(self, prompt, options=None, **kwargs):
                wd = kwargs.get("working_directory", ".")
                (Path(wd) / "implemented.py").write_text("x = 1\n")
                return ChatResponse(
                    content="done", provider=self.name, cost_usd=0.01,
                )

        provider = FileWritingMock()
        provider.capabilities = ProviderCapabilities(
            chat=True, agentic_code=True,
        )
        router = _mock_router(provider)
        coder = Coder(router)

        work_item = WorkItem(
            id="wm-1", title="worktree mode", description="",
            type="fix", priority="high", complexity=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "project"
            project.mkdir()
            _sp.run(
                ["git", "init", "-q", "-b", "main"], cwd=project, check=True,
            )
            _sp.run(
                ["git", "-c", "user.email=a@b", "-c", "user.name=t",
                 "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=project, check=True,
            )

            # Caller (worktree layer) creates the branch up-front, then
            # Coder runs against it.
            worktree = Path(tmpdir) / "worktree"
            _sp.run(
                ["git", "worktree", "add", "-b", "sentinel/wm-1", str(worktree)],
                cwd=project, check=True,
            )
            try:
                result = await coder.execute(
                    work_item,
                    working_directory=str(worktree),
                    artifacts_directory=str(project),
                    branch="sentinel/wm-1",
                )
                # Coder records the supplied branch unchanged
                assert result.branch == "sentinel/wm-1"
                # Coder does NOT create a `sentinel/fix/...` branch
                branches = _sp.run(
                    ["git", "branch", "--list"],
                    capture_output=True, text=True, cwd=project,
                )
                assert "sentinel/fix/worktree-mode" not in branches.stdout, (
                    "Coder must not create its own branch in worktree mode"
                )
            finally:
                _sp.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=project, check=False,
                )

    @pytest.mark.asyncio
    async def test_transcripts_go_to_artifacts_dir_not_working_dir(
        self,
    ) -> None:
        """When working_directory != artifacts_directory (the worktree
        case), the transcript must land in artifacts_directory so it
        survives worktree cleanup."""
        import subprocess as _sp

        provider = MockProvider(code_response=ChatResponse(
            content="Error: ",
            provider=ProviderName.CLAUDE,
            is_error=True,
            stderr="diagnostic info",
        ))
        provider.capabilities = ProviderCapabilities(
            chat=True, agentic_code=True,
        )
        router = _mock_router(provider)
        coder = Coder(router)

        work_item = WorkItem(
            id="wm-2", title="separate dirs", description="",
            type="fix", priority="high", complexity=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "project"
            worktree = Path(tmpdir) / "worktree"
            project.mkdir()
            worktree.mkdir()
            _sp.run(
                ["git", "init", "-q", "-b", "main"], cwd=project, check=True,
            )

            await coder.execute(
                work_item,
                working_directory=str(worktree),
                artifacts_directory=str(project),
                branch="sentinel/wm-2",
            )

            # Transcript lands in artifacts_directory, NOT working_directory
            artifact_transcripts = list(
                (project / ".sentinel" / "executions").glob("*.md"),
            )
            worktree_transcripts = list(
                (worktree / ".sentinel" / "executions").glob("*.md"),
            ) if (worktree / ".sentinel" / "executions").exists() else []

            assert len(artifact_transcripts) == 1
            assert len(worktree_transcripts) == 0


# --- Reviewer Tests ---

class TestReviewerHandlesBadResponse:
    @pytest.mark.asyncio
    async def test_rejects_when_provider_returns_invalid_json(self) -> None:
        """Reviewer must not silently approve on malformed output."""
        coder_provider = MockProvider()
        reviewer_provider = MockProvider(json_responses=[
            (None, ChatResponse(content="garbage", provider=ProviderName.GEMINI)),
        ])

        router = MagicMock()
        router.get_provider = lambda role: (
            reviewer_provider if role == "reviewer" else coder_provider
        )
        reviewer = Reviewer(router)

        work_item = WorkItem(
            id="1", title="test", description="",
            type="chore", priority="low", complexity=1,
        )
        from sentinel.roles.coder import ExecutionResult
        execution = ExecutionResult(
            work_item_id="1", status="success",
        )

        with (
            patch("sentinel.roles.reviewer._get_diff", return_value="diff content"),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            result = await reviewer.review(work_item, execution, tmpdir)
            assert result.verdict == "rejected"
            assert len(result.blocking_issues) > 0


class TestReviewerPersistsTranscripts:
    """Sigint dogfood: reviewer said 'changes-requested' on item 1 and
    we had no record of what it actually flagged. Follow-up work was
    impossible without re-running review. Now every review writes a
    Markdown transcript including summary, blocking issues, non-
    blocking observations, and the raw provider response."""

    @pytest.mark.asyncio
    async def test_survives_drifted_criteria_met_type(self) -> None:
        """Regression: if the provider returns criteria_met as a list
        (schema drift), transcript persistence used to crash on
        .items() and mask the real verdict."""
        import json as _json

        coder_provider = MockProvider()
        drifted_payload = {
            "verdict": "approved",
            "summary": "LGTM",
            "blocking_issues": [],
            # Drifted schema: should be dict, came back as list
            "criteria_met": ["first crit", "second crit"],
        }
        reviewer_provider = MockProvider(json_responses=[
            (drifted_payload, ChatResponse(
                content=_json.dumps(drifted_payload),
                provider=ProviderName.GEMINI, cost_usd=0.0,
            )),
        ])
        router = MagicMock()
        router.get_provider = lambda role: (
            reviewer_provider if role == "reviewer" else coder_provider
        )
        reviewer = Reviewer(router)
        work_item = WorkItem(
            id="1", title="drift test", description="", type="fix",
            priority="low", complexity=1,
        )
        from sentinel.roles.coder import ExecutionResult
        execution = ExecutionResult(work_item_id="1", status="success")

        with (
            patch("sentinel.roles.reviewer._get_diff", return_value=""),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            # Must not crash even though criteria_met is a list
            result = await reviewer.review(work_item, execution, tmpdir)
            assert result.verdict == "approved"
            # Normalized to empty dict
            assert result.acceptance_criteria_met == {}

    @pytest.mark.asyncio
    async def test_writes_review_transcript_on_success(self) -> None:
        import json as _json

        coder_provider = MockProvider()
        review_payload = {
            "verdict": "changes-requested",
            "summary": "Good direction but missing tests for the new branch.",
            "blocking_issues": [
                "No regression test for the 20-turn max path",
                "subprocess.run without timeout in retry loop",
            ],
            "non_blocking_observations": [
                "Consider caching the Finnhub API key lookup",
            ],
            "criteria_met": {"minimum change": True, "tests pass": False},
        }
        reviewer_provider = MockProvider(json_responses=[
            (review_payload, ChatResponse(
                content=_json.dumps(review_payload),
                provider=ProviderName.GEMINI,
                cost_usd=0.003,
            )),
        ])
        router = MagicMock()
        router.get_provider = lambda role: (
            reviewer_provider if role == "reviewer" else coder_provider
        )
        reviewer = Reviewer(router)

        work_item = WorkItem(
            id="cycle-1", title="fix the thing",
            description="make it work", type="fix",
            priority="high", complexity=2,
            acceptance_criteria=["minimum change", "tests pass"],
        )
        from sentinel.roles.coder import ExecutionResult
        execution = ExecutionResult(
            work_item_id="cycle-1", status="partial",
            branch="sentinel/fix/fix-the-thing",
            commit_sha="deadbeef",
            files_changed=["src/foo.py"],
        )

        with (
            patch(
                "sentinel.roles.reviewer._get_diff",
                return_value="diff --git a/src/foo.py b/src/foo.py\n+new line",
            ),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            result = await reviewer.review(work_item, execution, tmpdir)

            assert result.verdict == "changes-requested"
            # The fix: transcript exists
            transcripts = list(
                (Path(tmpdir) / ".sentinel" / "reviews").glob("*.md"),
            )
            assert len(transcripts) == 1, "review must leave a transcript"
            body = transcripts[0].read_text(encoding="utf-8")
            assert "CHANGES REQUESTED" in body
            assert "20-turn max path" in body  # blocking issue preserved
            assert "Finnhub API key" in body  # non-blocking observation
            assert "minimum change" in body  # acceptance criteria scorecard
            assert "deadbeef" in body  # commit SHA link
