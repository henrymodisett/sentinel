"""Tests for the destructive-change gate (sentinel.gate).

The gate inspects a diff for risky patterns before git push. Tests
synthesize diffs matching each risk pattern and assert the gate triggers
with the expected status. Non-risky diffs must pass cleanly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sentinel.gate import (
    _added_lines,
    _changed_paths,
    _count_deletions,
    _is_migration_file,
    inspect,
)

if TYPE_CHECKING:
    from pathlib import Path


# ─── Unit helpers ─────────────────────────────────────────────────────────────

class TestIsMigrationFile:
    def test_alembic_versions(self) -> None:
        assert _is_migration_file("migrations/alembic/versions/001_add_users.py")

    def test_sql_migrations(self) -> None:
        assert _is_migration_file("db/migrations/0023_rename_table.sql")

    def test_top_level_sql_migrations(self) -> None:
        assert _is_migration_file("migrations/0023_rename_table.sql")

    def test_django_style(self) -> None:
        assert _is_migration_file("app/migrations/0001_initial.py")

    def test_regular_python_not_migration(self) -> None:
        assert not _is_migration_file("src/auth.py")

    def test_regular_sql_not_migration(self) -> None:
        assert not _is_migration_file("fixtures/seed.sql")


class TestCountDeletions:
    def test_counts_minus_lines(self) -> None:
        diff = "--- a/foo.py\n+++ b/foo.py\n-old line\n-another old\n+new line\n"
        assert _count_deletions(diff) == 2

    def test_excludes_diff_header(self) -> None:
        diff = "--- a/foo.py\n+++ b/foo.py\n-removed\n"
        assert _count_deletions(diff) == 1  # header not counted

    def test_zero_for_additions_only(self) -> None:
        diff = "+++ b/foo.py\n+added line\n"
        assert _count_deletions(diff) == 0


class TestAddedLines:
    def test_extracts_added_content(self) -> None:
        diff = "+++ b/foo.py\n+new content\n+another line\n-old line\n"
        lines = _added_lines(diff)
        assert "new content" in lines
        assert "another line" in lines
        assert "old line" not in lines

    def test_excludes_diff_header(self) -> None:
        diff = "+++ b/foo.py\n+real addition\n"
        lines = _added_lines(diff)
        assert lines == ["real addition"]


class TestChangedPaths:
    def test_extracts_b_paths(self) -> None:
        diff = "--- a/old.py\n+++ b/new.py\n"
        paths = _changed_paths(diff)
        assert "new.py" in paths

    def test_multiple_files(self) -> None:
        diff = "--- a/foo.py\n+++ b/foo.py\n--- a/bar.py\n+++ b/bar.py\n"
        paths = _changed_paths(diff)
        assert "foo.py" in paths
        assert "bar.py" in paths


# ─── inspect() integration ────────────────────────────────────────────────────

def _make_diff(
    path: str = "src/app.py",
    added: list[str] | None = None,
    removed: list[str] | None = None,
) -> str:
    """Construct a minimal unified diff for testing."""
    lines = [f"--- a/{path}", f"+++ b/{path}"]
    for line in (removed or []):
        lines.append(f"-{line}")
    for line in (added or []):
        lines.append(f"+{line}")
    return "\n".join(lines) + "\n"


class TestInspect:
    def _patch_diff(self, monkeypatch, diff: str) -> None:
        """Patch _get_diff to return a synthetic diff."""
        monkeypatch.setattr("sentinel.gate._get_diff", lambda *_a, **_kw: diff)

    def test_clean_diff_passes(self, monkeypatch, tmp_path: Path) -> None:
        self._patch_diff(monkeypatch, _make_diff(added=["x = 1"], removed=["x = 0"]))
        result = inspect(tmp_path, base_branch="main")
        assert not result.blocked

    def test_migration_file_triggers_gate(self, monkeypatch, tmp_path: Path) -> None:
        diff = _make_diff(
            path="db/migrations/0042_rename_column.sql",
            added=["ALTER TABLE users RENAME COLUMN name TO full_name;"],
        )
        self._patch_diff(monkeypatch, diff)
        result = inspect(tmp_path, base_branch="main")
        assert result.blocked
        assert any("migration" in r.lower() for r in result.reasons)

    def test_large_deletion_triggers_gate(self, monkeypatch, tmp_path: Path) -> None:
        removed = [f"old line {i}" for i in range(150)]
        diff = _make_diff(removed=removed)
        self._patch_diff(monkeypatch, diff)
        result = inspect(tmp_path, base_branch="main", max_deletions=100)
        assert result.blocked
        assert any("150" in r or "removes" in r.lower() for r in result.reasons)

    def test_deletion_under_threshold_passes(self, monkeypatch, tmp_path: Path) -> None:
        removed = [f"line {i}" for i in range(50)]
        diff = _make_diff(removed=removed)
        self._patch_diff(monkeypatch, diff)
        result = inspect(tmp_path, base_branch="main", max_deletions=100)
        assert not result.blocked

    def test_aws_key_pattern_triggers_secret_gate(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        diff = _make_diff(
            path="config/aws.py",
            added=["AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'"],
        )
        self._patch_diff(monkeypatch, diff)
        # Ensure gitleaks is not on PATH for this test (use regex fallback)
        monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "gitleaks" else cmd)
        result = inspect(tmp_path, base_branch="main")
        assert result.blocked
        assert any("secret" in r.lower() for r in result.reasons)

    def test_pem_key_triggers_secret_gate(self, monkeypatch, tmp_path: Path) -> None:
        diff = _make_diff(
            path="keys/id_rsa",
            added=["-----BEGIN RSA PRIVATE KEY-----"],
        )
        self._patch_diff(monkeypatch, diff)
        monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "gitleaks" else cmd)
        result = inspect(tmp_path, base_branch="main")
        assert result.blocked

    def test_github_token_triggers_secret_gate(self, monkeypatch, tmp_path: Path) -> None:
        diff = _make_diff(
            path="ci/deploy.sh",
            added=["TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"],
        )
        self._patch_diff(monkeypatch, diff)
        monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "gitleaks" else cmd)
        result = inspect(tmp_path, base_branch="main")
        assert result.blocked

    def test_empty_diff_passes(self, monkeypatch, tmp_path: Path) -> None:
        self._patch_diff(monkeypatch, "")
        result = inspect(tmp_path, base_branch="main")
        assert not result.blocked

    def test_blocked_result_has_summary(self, monkeypatch, tmp_path: Path) -> None:
        """Blocked results include a human-readable summary for the journal."""
        diff = _make_diff(
            path="migrations/alembic/versions/001.py",
            added=["op.drop_table('users')"],
        )
        self._patch_diff(monkeypatch, diff)
        result = inspect(tmp_path, base_branch="main")
        assert result.blocked
        assert result.summary
        assert "human review" in result.summary.lower()

    def test_blocked_reason_can_be_journaled(self, monkeypatch, tmp_path: Path) -> None:
        diff = _make_diff(
            path="db/migrations/0042_rename_column.sql",
            added=["ALTER TABLE users RENAME COLUMN name TO full_name;"],
        )
        self._patch_diff(monkeypatch, diff)
        result = inspect(tmp_path, base_branch="main")
        assert "; ".join(result.reasons)

    def test_multiple_patterns_all_reported(self, monkeypatch, tmp_path: Path) -> None:
        """When multiple risky patterns match, all are reported in reasons."""
        # Migration file + secrets + big deletion
        removed = [f"r{i}" for i in range(150)]
        diff = (
            "--- a/db/migrations/001.sql\n"
            "+++ b/db/migrations/001.sql\n"
            + "".join(f"-{r}\n" for r in removed)
            + "+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
        )
        self._patch_diff(monkeypatch, diff)
        monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "gitleaks" else cmd)
        result = inspect(tmp_path, base_branch="main", max_deletions=100)
        assert result.blocked
        assert len(result.reasons) >= 2
