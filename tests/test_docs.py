"""Tests for the strategic-docs discovery module.

Sigint dogfood was the motivating failure: files like
INVESTMENT_THESIS.md, SYSTEM_ARCHITECTURE.md, and ANTI_CHASE_PLAN.md
were never read by Monitor because only CLAUDE.md + README were
hard-coded. These tests assert the discovery heuristic finds and
ranks those correctly without requiring a live LLM.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — runtime use in _make_project

from sentinel.docs import (
    DOC_EXTENSIONS,
    discover_project_docs,
    rank_docs,
)


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    """Drop a dict of {relative_path: content} into tmp_path."""
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return tmp_path


class TestRanking:
    def test_thesis_ranks_above_license(self, tmp_path: Path) -> None:
        _make_project(tmp_path, {
            "INVESTMENT_THESIS.md": "# Thesis\nOur edge is...",
            "LICENSE.md": "MIT License...",
        })
        ranked = rank_docs(tmp_path)
        assert ranked, "should find at least the thesis"
        assert ranked[0][0].name == "INVESTMENT_THESIS.md"
        # LICENSE filtered out entirely by DENY_FILENAMES
        assert not any("LICENSE" in str(p) for p, _ in ranked)

    def test_architecture_doc_ranks_high(self, tmp_path: Path) -> None:
        _make_project(tmp_path, {
            "agent/SYSTEM_ARCHITECTURE.md": "# Architecture\n...",
            "misc/notes.md": "random notes",
        })
        ranked = rank_docs(tmp_path)
        top_name = ranked[0][0].name
        assert "ARCHITECTURE" in top_name.upper()

    def test_denylist_excludes_changelog(self, tmp_path: Path) -> None:
        _make_project(tmp_path, {
            "CHANGELOG.md": "v1.0\n",
            "ROADMAP.md": "# Roadmap\n",
        })
        ranked = rank_docs(tmp_path)
        names = [p.name for p, _ in ranked]
        assert "CHANGELOG.md" not in names
        assert "ROADMAP.md" in names

    def test_respects_max_docs(self, tmp_path: Path) -> None:
        _make_project(tmp_path, {
            f"docs/plan_{i}.md": f"# Plan {i}" for i in range(20)
        })
        ranked = rank_docs(tmp_path, max_docs=5)
        assert len(ranked) == 5

    def test_skips_node_modules_and_venv(self, tmp_path: Path) -> None:
        _make_project(tmp_path, {
            "node_modules/some_pkg/README.md": "junk",
            ".venv/pkg/README.md": "junk",
            "README.md": "# Real README",
        })
        ranked = rank_docs(tmp_path)
        # Check path components relative to tmp_path — the pytest temp
        # dir name itself can contain "node_modules" as a substring.
        rel_parts = [p.relative_to(tmp_path).parts for p, _ in ranked]
        assert not any("node_modules" in parts for parts in rel_parts), rel_parts
        assert not any(".venv" in parts for parts in rel_parts), rel_parts
        assert any(p.name == "README.md" for p, _ in ranked)

    def test_respects_depth_limit(self, tmp_path: Path) -> None:
        """Docs buried 5 levels deep are likely vendored, not strategic."""
        _make_project(tmp_path, {
            "a/b/c/d/e/DEEPLY_NESTED_THESIS.md": "# Thesis\n",
            "THESIS.md": "# Top-level thesis\n",
        })
        ranked = rank_docs(tmp_path)
        names = [p.name for p, _ in ranked]
        assert "THESIS.md" in names
        assert "DEEPLY_NESTED_THESIS.md" not in names


class TestSecretFiltering:
    """Codex review: secret-adjacent filenames at root level (e.g.
    `secrets.txt`, `credentials.md`, `API_KEY.md`) must never be
    read or forwarded to an LLM. Defense-in-depth: match patterns
    BEFORE tier scoring so even doc-keyword-matching names get
    rejected."""

    def test_secret_filenames_are_skipped(self, tmp_path: Path) -> None:
        from sentinel.docs import discover_project_docs
        _make_project(tmp_path, {
            "SECRETS.md": "OPENAI_API_KEY=sk-real\n",
            "credentials.txt": "password=hunter2",
            "api_keys.md": "foo_key=abc",
            ".env": "SECRET=x",
            "README.md": "# real",
        })
        output = discover_project_docs(tmp_path)
        assert "OPENAI_API_KEY" not in output
        assert "hunter2" not in output
        assert "foo_key" not in output
        assert "# real" in output

    def test_keyword_match_does_not_override_secret_filter(
        self, tmp_path: Path,
    ) -> None:
        """A secret filename that ALSO matches a doc keyword
        (e.g. `secrets_api.md`) still gets filtered out."""
        from sentinel.docs import discover_project_docs
        _make_project(tmp_path, {
            "secrets_api.md": "shared api_key=secret123",
            "README.md": "real",
        })
        output = discover_project_docs(tmp_path)
        assert "secret123" not in output

    def test_secret_directory_names_are_pruned(self, tmp_path: Path) -> None:
        """Codex round 2: basename-only check used to let
        `secrets/README.md` slip through because README is a valid
        doc name. Now secret-adjacent DIR names are pruned during
        walk, so README inside `secrets/` is never read."""
        from sentinel.docs import discover_project_docs
        _make_project(tmp_path, {
            "secrets/README.md": "API_TOKEN=abc123",
            "credentials/PLAN.md": "admin_password=xyz",
            "ROADMAP.md": "# real roadmap",
        })
        output = discover_project_docs(tmp_path)
        assert "API_TOKEN" not in output
        assert "admin_password" not in output
        assert "# real roadmap" in output


class TestWalkPruning:
    """Codex review: rglob used to descend into skipped dirs before
    filtering, so a repo with a giant node_modules would stall state
    gathering. os.walk + in-place dirs pruning fixes this — the skip
    dir is never entered."""

    def test_large_node_modules_does_not_slow_walk(
        self, tmp_path: Path,
    ) -> None:
        """This is a perf test by proxy — we create a node_modules
        subtree and verify no files inside it are even considered."""
        from sentinel.docs import _iter_doc_candidates

        # Simulate a moderately-sized node_modules
        for i in range(30):
            pkg = tmp_path / "node_modules" / f"pkg_{i}"
            pkg.mkdir(parents=True)
            (pkg / "README.md").write_text(f"junk {i}")
        (tmp_path / "ROADMAP.md").write_text("# real")

        candidates = _iter_doc_candidates(tmp_path)
        parts_lists = [
            str(p.relative_to(tmp_path)).split("/") for p in candidates
        ]
        assert not any("node_modules" in parts for parts in parts_lists)
        assert any(p.name == "ROADMAP.md" for p in candidates)


class TestExcerptFormatting:
    def test_empty_project_returns_empty_string(self, tmp_path: Path) -> None:
        assert discover_project_docs(tmp_path) == ""

    def test_output_includes_rel_path_headers(self, tmp_path: Path) -> None:
        _make_project(tmp_path, {
            "agent/INVESTMENT_THESIS.md": "# Thesis\nEdge description.\n",
        })
        output = discover_project_docs(tmp_path)
        assert "### agent/INVESTMENT_THESIS.md" in output
        assert "Edge description" in output

    def test_truncates_long_docs(self, tmp_path: Path) -> None:
        long_body = "x" * 5000
        _make_project(tmp_path, {
            "PLAN.md": f"# Plan\n{long_body}",
        })
        output = discover_project_docs(tmp_path, max_chars_per_doc=200)
        # Doc body capped — total output shouldn't include most of long_body
        assert output.count("x") < 300

    def test_strips_code_blocks(self, tmp_path: Path) -> None:
        """Long fenced blocks of code/data shouldn't dominate the
        excerpt — we want prose signal."""
        body = (
            "# Architecture\n"
            "Core principle: no silent failures.\n\n"
            "```python\n"
            + "\n".join(f"def long_func_{i}(): pass" for i in range(50))
            + "\n```\n\n"
            "Second principle: derive, don't persist.\n"
        )
        _make_project(tmp_path, {"ARCHITECTURE.md": body})
        output = discover_project_docs(tmp_path, max_chars_per_doc=800)
        assert "no silent failures" in output
        # Code block replaced with placeholder
        assert "code block omitted" in output
        assert "long_func_0" not in output


class TestIntegrationWithState:
    """End-to-end: gather_state should populate project_docs."""

    def test_gather_state_populates_project_docs(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import subprocess
        _make_project(tmp_path, {
            "AGENTS.md": "# Agents\n",
            "CLAUDE.md": "# Claude\n",  # doubles as a git-init seed
        })
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=tmp_path, check=True,
        )

        from sentinel.state import gather_state
        state = gather_state(tmp_path)
        assert "AGENTS.md" in state.project_docs


def test_doc_extensions_is_tuple() -> None:
    """Guardrail: DOC_EXTENSIONS must stay a tuple so it's immutable
    and hashable for set operations downstream."""
    assert isinstance(DOC_EXTENSIONS, tuple)
