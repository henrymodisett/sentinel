"""ASCII hero banner for `sentinel work` and other splash moments.

Embedded standard-font figlet glyphs with ANSI color. No runtime figlet
dependency — we ship the wordmark as a string literal so it always
renders, then ANSI-color it when stderr is a TTY. Call ``render_banner()``
to get the lines as a list (testable) or ``print_banner()`` to write to
stderr (doesn't interfere with stdout capture in scripts).

Per Autumn Garage Doctrine 0007, Sentinel's pastel tone-on-tone palette
is sage primary (ANSI 256 = 151) for the wordmark, pale mint (157) for
subtitle/version/attribution.
"""

from __future__ import annotations

import os
import sys

# Rendered once from `figlet -f standard "Sentinel"` and embedded here so
# we don't take a dependency on figlet at runtime. Keep the glyphs
# aligned — editors that strip trailing whitespace may break the art.
_SENTINEL_GLYPHS: tuple[str, ...] = (
    " ____             _   _            _ ",
    "/ ___|  ___ _ __ | |_(_)_ __   ___| |",
    "\\___ \\ / _ \\ '_ \\| __| | '_ \\ / _ \\ |",
    " ___) |  __/ | | | |_| | | | |  __/ |",
    "|____/ \\___|_| |_|\\__|_|_| |_|\\___|_|",
)

# Sage — distinguishes sentinel from touchstone's peach,
# cortex's aqua, and conductor's lavender. Pastel tone-on-tone with the
# subtitle / attribution at the slightly lighter mint.
_ANSI_SAGE = "\033[38;5;151m"
_ANSI_MINT = "\033[38;5;157m"
_ANSI_RESET = "\033[0m"


def _color_enabled(stream) -> bool:  # type: ignore[no-untyped-def]
    """True when the stream is a TTY and NO_COLOR / CLICOLOR=0 are unset."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR") == "0":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def render_banner(
    subtitle: str | None = None,
    version: str | None = None,
    *,
    use_color: bool = True,
) -> list[str]:
    """Return the banner as a list of lines (no trailing newline per line).

    When ``use_color`` is True, glyph lines are wrapped in the sage ANSI
    code and the subtitle/version/attribution lines in pale mint.
    Callers writing to a non-TTY should pass ``use_color=False``.
    """
    lines: list[str] = [""]
    for glyph in _SENTINEL_GLYPHS:
        if use_color:
            lines.append(f"  {_ANSI_SAGE}{glyph}{_ANSI_RESET}")
        else:
            lines.append(f"  {glyph}")

    sub_parts: list[str] = []
    if subtitle:
        sub_parts.append(subtitle)
    if version:
        sub_parts.append(f"v{version}")
    if sub_parts:
        sub_text = "  ·  ".join(sub_parts)
        if use_color:
            lines.append(f"  {_ANSI_MINT}{sub_text}{_ANSI_RESET}")
        else:
            lines.append(f"  {sub_text}")

    if use_color:
        lines.append(f"  {_ANSI_MINT}by Autumn Garage{_ANSI_RESET}")
    else:
        lines.append("  by Autumn Garage")
    lines.append("")
    return lines


def print_banner(
    subtitle: str | None = None,
    version: str | None = None,
    *,
    stream=None,  # type: ignore[no-untyped-def]
) -> None:
    """Write the banner to ``stream`` (default: stderr).

    Picks color based on whether ``stream`` is a TTY and NO_COLOR /
    CLICOLOR=0 are unset. Scripts that capture sentinel's stdout are not
    disturbed because the banner lives on stderr.
    """
    target = stream if stream is not None else sys.stderr
    use_color = _color_enabled(target)
    for line in render_banner(subtitle, version, use_color=use_color):
        print(line, file=target)


def sentinel_version() -> str | None:
    """Return the resolved sentinel version for banner display."""
    from sentinel import __version__

    return str(__version__) if __version__ else None


SUBTITLE_WORK = "assess · plan · delegate · review"
SUBTITLE_INIT = "set up the autonomous loop"
