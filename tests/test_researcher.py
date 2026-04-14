"""Tests for Researcher.domain_brief and its caching layer.

Sigint motivated this: quant-edge lens scored 40→95 run-to-run on
the same codebase partly because the evaluator didn't know what
"good" looks like in prediction-market quant. The domain brief
preloads that context before lens gen so scoring has a reference
frame.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path  # noqa: TC003 — runtime use
from unittest.mock import AsyncMock, MagicMock

import pytest

from sentinel.providers.interface import ChatResponse, ProviderName
from sentinel.roles.researcher import (
    DOMAIN_BRIEF_FILENAME,
    ResearchBrief,
    Researcher,
    _hash_context,
    _load_cached_brief,
    _save_cached_brief,
)


def _mock_router(content: str = "## Domain\nexample\n", cost: float = 0.01):
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=ChatResponse(
        content=content, provider=ProviderName.GEMINI, cost_usd=cost,
    ))
    router = MagicMock()
    router.get_provider = MagicMock(return_value=provider)
    return router, provider


class TestDomainBriefCaching:
    def test_hash_is_stable_for_same_inputs(self) -> None:
        h1 = _hash_context("python", "readme", "docs")
        h2 = _hash_context("python", "readme", "docs")
        assert h1 == h2

    def test_hash_changes_when_type_changes(self) -> None:
        h1 = _hash_context("python", "readme", "docs")
        h2 = _hash_context("rust", "readme", "docs")
        assert h1 != h2

    def test_hash_changes_when_docs_change(self) -> None:
        h1 = _hash_context("python", "readme", "docs version a")
        h2 = _hash_context("python", "readme", "docs version b")
        assert h1 != h2

    def test_cache_hit_returns_brief(self, tmp_path: Path) -> None:
        cache = tmp_path / DOMAIN_BRIEF_FILENAME
        brief = ResearchBrief(
            question="domain", mode="domain",
            synthesis="## Domain\nreal brief",
        )
        _save_cached_brief(cache, brief, "hashA")

        loaded = _load_cached_brief(cache, "hashA")
        assert loaded is not None
        assert "real brief" in loaded.synthesis

    def test_cache_miss_on_hash_mismatch(self, tmp_path: Path) -> None:
        cache = tmp_path / DOMAIN_BRIEF_FILENAME
        brief = ResearchBrief(
            question="domain", mode="domain", synthesis="old",
        )
        _save_cached_brief(cache, brief, "hashA")

        # Request with a different hash (e.g. docs changed)
        assert _load_cached_brief(cache, "hashB") is None

    def test_cache_miss_on_stale_timestamp(self, tmp_path: Path) -> None:
        """Manually forge a stale header so we don't have to wait
        7 days for the test."""
        cache = tmp_path / DOMAIN_BRIEF_FILENAME
        old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=30)).isoformat()
        cache.write_text(
            f"<!-- sentinel-domain-brief hash=hashA generated={old} -->\n"
            "old brief\n",
            encoding="utf-8",
        )
        assert _load_cached_brief(cache, "hashA") is None


class TestDomainBriefResearch:
    @pytest.mark.asyncio
    async def test_calls_provider_on_cold_cache(self, tmp_path: Path) -> None:
        router, provider = _mock_router(content="## Domain\nfresh research")
        researcher = Researcher(router)

        # Ensure .sentinel/ dir exists for the cache write
        (tmp_path / ".sentinel").mkdir()
        result = await researcher.domain_brief(
            project_path=str(tmp_path),
            project_type="python",
            project_name="demo",
            readme_excerpt="# Demo project",
            docs_excerpt="# Plan",
        )
        assert "fresh research" in result.synthesis
        assert provider.chat.await_count == 1
        # Next call with same inputs should hit cache (no provider call)
        result2 = await researcher.domain_brief(
            project_path=str(tmp_path),
            project_type="python",
            project_name="demo",
            readme_excerpt="# Demo project",
            docs_excerpt="# Plan",
        )
        assert provider.chat.await_count == 1, "cached brief must not re-call provider"
        assert "fresh research" in result2.synthesis

    @pytest.mark.asyncio
    async def test_refetches_when_docs_change(self, tmp_path: Path) -> None:
        router, provider = _mock_router(content="## Domain\nfirst")
        researcher = Researcher(router)
        (tmp_path / ".sentinel").mkdir()

        await researcher.domain_brief(
            project_path=str(tmp_path), project_type="python",
            project_name="demo", readme_excerpt="readme",
            docs_excerpt="first docs",
        )
        # Swap the response for a second call
        provider.chat = AsyncMock(return_value=ChatResponse(
            content="## Domain\nsecond", provider=ProviderName.GEMINI,
            cost_usd=0.01,
        ))
        result = await researcher.domain_brief(
            project_path=str(tmp_path), project_type="python",
            project_name="demo", readme_excerpt="readme",
            docs_excerpt="SECOND docs — something meaningfully changed",
        )
        assert "second" in result.synthesis

    @pytest.mark.asyncio
    async def test_error_prefixed_content_does_not_get_cached(
        self, tmp_path: Path,
    ) -> None:
        """Regression (Codex): some providers return
        content='Error: foo' with is_error=False on CLI failure.
        Without treating that as a failure, the error string would
        be cached as a legit brief for 7 days and injected into
        every explore prompt."""
        router, provider = _mock_router()
        provider.chat = AsyncMock(return_value=ChatResponse(
            content="Error: gemini CLI timed out after 600s",
            provider=ProviderName.GEMINI,
            is_error=False,  # <-- the dangerous combination
            cost_usd=0.0,
        ))
        researcher = Researcher(router)
        (tmp_path / ".sentinel").mkdir()

        result = await researcher.domain_brief(
            project_path=str(tmp_path), project_type="python",
            project_name="demo", readme_excerpt="", docs_excerpt="",
        )
        # Must treat as failure, not cache the error string
        assert result.synthesis == ""
        assert result.confidence == "low"
        # Cache file must NOT contain the error message
        cache = tmp_path / DOMAIN_BRIEF_FILENAME
        if cache.exists():
            body = cache.read_text(encoding="utf-8")
            assert "Error: gemini CLI timed out" not in body

    @pytest.mark.asyncio
    async def test_research_failure_returns_empty_brief(
        self, tmp_path: Path,
    ) -> None:
        """If the provider errors or returns empty, the brief is empty
        but non-fatal — lens generation still runs without domain ctx."""
        router, _ = _mock_router(content="")
        provider = router.get_provider("researcher")
        provider.chat = AsyncMock(return_value=ChatResponse(
            content="", provider=ProviderName.GEMINI,
            is_error=True, cost_usd=0.0,
        ))
        researcher = Researcher(router)
        (tmp_path / ".sentinel").mkdir()

        result = await researcher.domain_brief(
            project_path=str(tmp_path), project_type="generic",
            project_name="demo", readme_excerpt="", docs_excerpt="",
        )
        assert result.synthesis == ""
        assert result.confidence == "low"
