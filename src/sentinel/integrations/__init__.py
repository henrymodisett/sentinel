"""Autumn Garage sibling integrations — file-contract composition only.

Each submodule here implements a single sibling-tool integration via the
file-contract pattern (cortex Doctrine 0002 / autumn-garage Doctrine 0001):
no Python imports of sibling code, no shared libraries, no direct CLI
shell-outs into sibling business logic. The integration reads the
sibling's presence marker (e.g. ``.cortex/``) and writes into the
sibling's file layout (``.cortex/journal/``) using a documented template
shape.
"""
