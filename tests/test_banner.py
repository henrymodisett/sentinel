"""Tests for the sentinel banner module.

Covers the public surface of `sentinel.banner`:
  - render_banner returns the canonical 5 glyph lines plus attribution
  - use_color=False produces no ANSI escapes
  - subtitle and version both appear when supplied
  - the embedded glyphs match the canonical figlet -f standard "Sentinel"
"""

from __future__ import annotations

from sentinel.banner import (
    _SENTINEL_GLYPHS,
    SUBTITLE_INIT,
    SUBTITLE_WORK,
    render_banner,
)

CANONICAL_GLYPHS: tuple[str, ...] = (
    " ____             _   _            _ ",
    "/ ___|  ___ _ __ | |_(_)_ __   ___| |",
    "\\___ \\ / _ \\ '_ \\| __| | '_ \\ / _ \\ |",
    " ___) |  __/ | | | |_| | | | |  __/ |",
    "|____/ \\___|_| |_|\\__|_|_| |_|\\___|_|",
)


def test_glyphs_match_canonical_figlet_standard() -> None:
    """Embedded glyphs are the canonical `figlet -f standard "Sentinel"`."""
    assert _SENTINEL_GLYPHS == CANONICAL_GLYPHS


def test_render_banner_no_color_contains_all_glyph_lines() -> None:
    lines = render_banner(use_color=False)
    # Each glyph line should appear (with a leading "  " indent).
    for glyph in _SENTINEL_GLYPHS:
        assert any(glyph in line for line in lines), (
            f"glyph row not present in output: {glyph!r}"
        )


def test_render_banner_no_color_emits_no_ansi_escapes() -> None:
    lines = render_banner(
        subtitle="x",
        version="0.1.0",
        use_color=False,
    )
    joined = "\n".join(lines)
    assert "\033[" not in joined, "ANSI escape leaked into use_color=False output"


def test_render_banner_color_wraps_glyphs_in_sage_ansi() -> None:
    lines = render_banner(use_color=True)
    glyph_lines = [line for line in lines if any(g in line for g in _SENTINEL_GLYPHS)]
    assert glyph_lines, "no glyph lines rendered"
    for line in glyph_lines:
        # Sage primary = ANSI 256 color 151. Pale mint = 157 (subtitle).
        assert "\033[38;5;151m" in line, f"sage color missing: {line!r}"
        assert "\033[0m" in line, f"reset missing: {line!r}"


def test_attribution_line_present() -> None:
    lines = render_banner(use_color=False)
    assert any("by Autumn Garage" in line for line in lines), (
        "Autumn Garage attribution missing from banner"
    )


def test_attribution_uses_mint_when_colored() -> None:
    lines = render_banner(use_color=True)
    attribution = next((line for line in lines if "by Autumn Garage" in line), None)
    assert attribution is not None
    assert "\033[38;5;157m" in attribution, "mint color missing on attribution"


def test_render_banner_includes_subtitle_and_version() -> None:
    lines = render_banner(
        subtitle="my subtitle",
        version="0.1.0",
        use_color=False,
    )
    sub_lines = [line for line in lines if "my subtitle" in line]
    assert sub_lines, "subtitle missing from output"
    assert "v0.1.0" in sub_lines[0], (
        "version not joined to subtitle on the same line"
    )


def test_render_banner_omits_subtitle_when_none() -> None:
    lines = render_banner(use_color=False)
    # No spurious subtitle separator when neither subtitle nor version supplied.
    assert not any("·" in line for line in lines)


def test_render_banner_includes_version_only() -> None:
    lines = render_banner(version="1.2.3", use_color=False)
    assert any("v1.2.3" in line for line in lines)


def test_subtitles_are_strings() -> None:
    assert isinstance(SUBTITLE_WORK, str) and SUBTITLE_WORK
    assert isinstance(SUBTITLE_INIT, str) and SUBTITLE_INIT
