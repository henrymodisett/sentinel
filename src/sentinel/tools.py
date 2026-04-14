"""
Discover common ops CLIs on the user's PATH.

Surfaced to Monitor's exploration prompt so lens generation knows what
tools the Coder can actually use during execution, and to the Planner
so refinements can be scoped to tools that exist on this machine.

Deliberately narrow — we only look for tools that are commonly used in
real-project operations (deploy, infra, package, VCS). Expanding this
list should be a conscious choice: each tool we advertise is one the
Coder may decide to invoke.
"""

from __future__ import annotations

import shutil

# Grouped so the prompt can render them with labels instead of one flat list.
# Order within a group is alphabetical; order of groups roughly mirrors the
# typical stack (VCS → package → deploy → infra).
TOOL_GROUPS: dict[str, list[str]] = {
    "vcs": ["gh", "git", "glab"],
    "package": [
        "bun", "cargo", "go", "npm", "pip", "pnpm", "poetry", "uv", "yarn",
    ],
    "deploy": [
        "fly", "heroku", "netlify", "railway", "render", "vercel",
    ],
    "infra": [
        "aws", "docker", "doctl", "gcloud", "kubectl", "terraform",
    ],
}


def discover_installed_tools() -> dict[str, list[str]]:
    """Return the subset of TOOL_GROUPS that is actually on PATH.

    Returns a dict with the same keys as TOOL_GROUPS but only the tools
    present on the current PATH. Empty groups are dropped.
    """
    present: dict[str, list[str]] = {}
    for group, tools in TOOL_GROUPS.items():
        found = [t for t in tools if shutil.which(t)]
        if found:
            present[group] = found
    return present


def format_tools_for_prompt(tools: dict[str, list[str]]) -> str:
    """Format discovered tools as a prompt-ready multi-line string.

    Shape:
        vcs: git, gh
        deploy: railway, vercel
        infra: docker

    Empty input returns a sentinel string — the prompt template will
    still render cleanly.
    """
    if not tools:
        return "(none detected — plain `git` environment only)"
    return "\n".join(f"{group}: {', '.join(names)}" for group, names in tools.items())
