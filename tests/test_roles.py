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
            result = await coder.execute(work_item, tmpdir)
            assert result.status == "failed"
            assert "agentic_code" in (result.error or "") or "claude" in (
                result.error or ""
            ).lower()


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

            result = await coder.execute(work_item, tmpdir)

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
            result = await coder.execute(work_item, tmpdir)

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

            result = await coder.execute(work_item, tmpdir)

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

    def test_ignores_untracked_files(self, tmp_path) -> None:
        """Untracked files (like .sentinel/transcripts) must not count
        as dirty — reset --hard doesn't touch them."""
        import subprocess as _sp

        from sentinel.cli.work_cmd import _working_tree_clean

        _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        _sp.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=tmp_path, check=True,
        )
        (tmp_path / "untracked.md").write_text("x\n")
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


class TestCoderExistingBranchCheckout:
    """Codex review caught: when the feature branch already exists
    (e.g. from a prior partial run), `git checkout -b` failed with
    'already exists' and we skipped the error path — BUT we didn't
    actually check the branch out. Claude ran + the commit landed on
    the original branch instead of the feature branch."""

    @pytest.mark.asyncio
    async def test_checks_out_existing_branch_instead_of_original(self) -> None:
        import subprocess as _sp

        class FileWritingMock(MockProvider):
            async def code(self, prompt, options=None, **kwargs):
                wd = kwargs.get("working_directory", ".")
                (Path(wd) / "fixed.py").write_text("ok\n")
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
            id="1", title="reuse branch", description="",
            type="fix", priority="low", complexity=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmpdir, check=True)
            _sp.run(
                ["git", "-c", "user.email=a@b", "-c", "user.name=t",
                 "commit", "--allow-empty", "-m", "init", "-q"],
                cwd=tmpdir, check=True,
            )
            # Pre-create the branch the Coder will try to create
            branch = f"sentinel/{work_item.type}/{_slug(work_item.title)}"
            _sp.run(["git", "branch", branch], cwd=tmpdir, check=True)

            await coder.execute(work_item, tmpdir)

            # The commit must land on the feature branch, not main
            current = _sp.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, cwd=tmpdir,
            ).stdout.strip()
            assert current == branch, (
                f"coder must switch to existing feature branch, got {current!r}"
            )
            main_log = _sp.run(
                ["git", "log", "--oneline", "main"],
                capture_output=True, text=True, cwd=tmpdir,
            )
            assert "reuse branch" not in main_log.stdout, (
                "commit must NOT land on main when feature branch exists"
            )


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

            result = await coder.execute(work_item, tmpdir)
            assert result.status == "failed"
            # The fix: transcript exists on disk
            transcripts = list(
                (Path(tmpdir) / ".sentinel" / "executions").glob("*.md"),
            )
            assert len(transcripts) == 1, "execution must leave a transcript"
            body = transcripts[0].read_text()
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
            result = await coder.execute(work_item, tmpdir)
            assert "authentication failed" in (result.error or "")


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
