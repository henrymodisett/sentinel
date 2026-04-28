"""Cortex read-side integration - fetch session manifest at cycle start.

Shells out to ``cortex manifest --budget N --path DIR`` once per cycle and
returns the markdown string so roles can prepend it to their prompts inside
a ``<cortex-context>`` fence.

Intentionally thin:
- No import of sentinel internals (no circular-import risk).
- All failures return None, never raise - the cycle is not gated on
  Cortex availability.
- First miss per session is logged as a warning; subsequent misses are
  silent to avoid log spam in long-running cycles.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path  # noqa: TC003 — runtime use in fetch_manifest signature

logger = logging.getLogger(__name__)

_cortex_bin: str | None = None
_cortex_bin_resolved: bool = False
_warned: bool = False


def _get_cortex_bin() -> str | None:
    global _cortex_bin, _cortex_bin_resolved  # noqa: PLW0603
    if not _cortex_bin_resolved:
        _cortex_bin = shutil.which("cortex")
        _cortex_bin_resolved = True
    return _cortex_bin


def _warn_once(msg: str) -> None:
    global _warned  # noqa: PLW0603
    if not _warned:
        logger.warning(msg)
        _warned = True


def reset_warned() -> None:
    """Reset module-level caches and flags. Intended for tests only."""
    global _cortex_bin, _cortex_bin_resolved, _warned  # noqa: PLW0603
    _cortex_bin = None
    _cortex_bin_resolved = False
    _warned = False


def fetch_manifest(
    project_dir: Path,
    budget: int = 6000,
    timeout_sec: int = 30,
) -> str | None:
    """Return the Cortex session manifest as markdown, or None if unavailable.

    Shells out to ``cortex manifest --budget <budget> --path <project_dir>``.
    Returns None (not raises) when:
    - the ``cortex`` binary is not on $PATH
    - ``.cortex/`` is absent under project_dir
    - the subprocess returns non-zero exit
    - the subprocess times out

    Logs a warning on the first miss per session; subsequent misses are
    silent to avoid log spam.
    """
    bin_path = _get_cortex_bin()
    if bin_path is None:
        _warn_once("cortex binary not on PATH - manifest fetch skipped")
        return None

    if not (project_dir / ".cortex").is_dir():
        _warn_once(f".cortex/ absent at {project_dir} - manifest fetch skipped")
        return None

    try:
        proc = subprocess.run(
            [bin_path, "manifest", "--budget", str(budget), "--path", str(project_dir)],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        _warn_once(f"cortex manifest timed out after {timeout_sec}s - skipped")
        return None
    except OSError as exc:
        _warn_once(f"cortex manifest failed ({exc}) - skipped")
        return None

    if proc.returncode != 0:
        _warn_once(
            f"cortex manifest exited {proc.returncode} - skipped "
            f"({(proc.stderr or '').strip()[:120]})"
        )
        return None

    return proc.stdout if proc.stdout.strip() else None


def cortex_fence(ctx: str | None) -> str:
    """Wrap a Cortex manifest in a ``<cortex-context>`` fence for prompt injection.

    Returns an empty string when ``ctx`` is None or empty so callers can
    unconditionally prepend the result without an extra None-check.
    """
    if not ctx:
        return ""
    return f"<cortex-context>\n{ctx}\n</cortex-context>\n\n"
