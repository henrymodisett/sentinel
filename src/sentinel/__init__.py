"""
Sentinel — Autonomous Meta-Agent for Software Projects

Manages software projects through a continuous loop:
  1. Assess State  — Monitor scans the codebase through multiple lenses
  2. Research      — Researcher investigates best approaches
  3. Plan          — Planner creates prioritized work items
  4. Delegate      — Coder executes, Reviewer verifies

Each step is powered by a configurable LLM provider (CLI-based, no API keys stored):
  - Monitor    → default: Local/Ollama (free, runs often)
  - Researcher → default: Gemini CLI (web search, cheap)
  - Planner    → default: Claude CLI (best judgment)
  - Coder      → default: Claude Code (agentic coding)
  - Reviewer   → default: Gemini CLI (independent from coder)

Goals are derived from CLAUDE.md, README, and GitHub issues — not stored separately.
"""

__version__ = "0.1.0"

# Silence the sentinel logger by default — errors are captured in
# ProjectState.errors / ScanResult.error and surfaced via the CLI.
# Users who want verbose output can configure logging themselves.
import logging as _logging

_logging.getLogger("sentinel").addHandler(_logging.NullHandler())
