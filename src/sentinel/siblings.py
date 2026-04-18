"""Autumn Garage sibling detection — cortex and touchstone.

File-contract + CLI-shell-out only. No Python imports of sibling modules;
per Autumn Garage Doctrine 0001 and Cortex Doctrine 0002 the three tools
compose through file contracts and subprocess calls, never through a
shared library.

Detection is absence-tolerant: missing siblings are normal and never an
error. A 3-second timeout per `<tool> version` shell-out bounds any
regression in `sentinel status` runtime.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — runtime use in detect_* signatures

# Bound the extra latency `sentinel status` adds when siblings are installed.
# Two siblings × 3s worst case = 6s in a failure mode; typical happy path
# is sub-100ms per `<tool> version`.
_VERSION_TIMEOUT_SECONDS = 3.0

# Match the first semver-looking token in CLI version output. Siblings are
# free to format however they like ("cortex 0.1.0", "touchstone version
# 1.1.0", etc.) — we take the first X.Y.Z we see.
_SEMVER_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b")


@dataclass(frozen=True)
class SiblingStatus:
    """Snapshot of one sibling tool's presence in the current environment.

    ``cli_installed`` reflects ``shutil.which`` — whether the tool is on
    ``$PATH``. ``project_marker_present`` reflects whether the project
    directory carries the sibling's file-contract marker (``.cortex/`` or
    ``.touchstone-config``). ``version`` is the parsed semver string from
    ``<tool> version`` stdout, or ``None`` if the CLI is absent, timed
    out, exited non-zero, or produced unparseable output.
    """

    name: str
    cli_installed: bool
    project_marker_present: bool
    version: str | None
    marker_label: str


def _parse_version(output: str) -> str | None:
    match = _SEMVER_RE.search(output)
    return match.group(1) if match else None


def _probe_version(cli_name: str) -> str | None:
    """Shell out to ``<cli_name> version`` with a bounded timeout.

    Returns the parsed semver string or ``None`` on any failure mode
    (missing CLI, timeout, non-zero exit, unparseable output). Failures
    are intentionally silent here because absence is normal for sibling
    detection — the caller surfaces the absence via the ✓/—/! glyph.
    """
    try:
        result = subprocess.run(
            [cli_name, "version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return _parse_version(result.stdout) or _parse_version(result.stderr)


def detect_cortex(project: Path) -> SiblingStatus:
    cli_path = shutil.which("cortex")
    marker = project / ".cortex"
    return SiblingStatus(
        name="cortex",
        cli_installed=cli_path is not None,
        project_marker_present=marker.is_dir(),
        version=_probe_version("cortex") if cli_path else None,
        marker_label=".cortex/",
    )


def detect_touchstone(project: Path) -> SiblingStatus:
    cli_path = shutil.which("touchstone")
    marker = project / ".touchstone-config"
    return SiblingStatus(
        name="touchstone",
        cli_installed=cli_path is not None,
        project_marker_present=marker.is_file(),
        version=_probe_version("touchstone") if cli_path else None,
        marker_label=".touchstone-config",
    )


def detect_siblings(project: Path) -> list[SiblingStatus]:
    """Return sibling statuses in stable display order (cortex, touchstone)."""
    return [detect_cortex(project), detect_touchstone(project)]


def format_sibling_line(status: SiblingStatus) -> str:
    """Render one sibling-status line for ``sentinel status`` output.

    Glyphs:
      ✓ — CLI installed AND project marker present (fully composed)
      — — neither CLI nor marker (absence is normal, not an error)
      ! — partial state (CLI without marker, or marker without CLI) — an
          orientation cue that something is half-wired

    The line always names the sibling and its marker path so a new user
    can see what the file contract is without reading docs.
    """
    if status.cli_installed and status.project_marker_present:
        glyph = "[green]✓[/green]"
        version = status.version or "unknown"
        return (
            f"  {glyph} {status.name} {version} (installed) — "
            f"{status.marker_label} present"
        )
    if not status.cli_installed and not status.project_marker_present:
        glyph = "[dim]—[/dim]"
        return (
            f"  {glyph} {status.name} not installed — "
            f"{status.marker_label} absent"
        )
    # Partial: one side present, the other not. Surface it.
    glyph = "[yellow]![/yellow]"
    if status.cli_installed:
        version = status.version or "unknown"
        return (
            f"  {glyph} {status.name} {version} (installed) — "
            f"{status.marker_label} absent"
        )
    return (
        f"  {glyph} {status.name} not installed — "
        f"{status.marker_label} present"
    )
