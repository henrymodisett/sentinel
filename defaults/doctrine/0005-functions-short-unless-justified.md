---
ID: 0005
Title: Functions short unless justified
Date: 2026-04-28
Status: Active
Load-priority: always
Sentinel-baseline: true
Schema-version: 1.0
---

# Doctrine 0005 — Functions short unless justified

A function over ~50 lines is a code smell — usually a sign that two concerns got entangled. Default to short, single-purpose functions. Long functions are allowed when the body is genuinely a single linear procedure and breaking it up would obscure the flow.

## What it means in practice

- Most functions fit in one screen.
- A function with multiple `if/else` branches at the same indentation level usually wants to be split.
- Code that says "step 1: ... ; step 2: ... ; step 3: ..." in comments is asking to be three functions.
- Long-but-linear is fine — a setup-heavy test, a state machine's main loop, a switch-style dispatcher.

## Why

Short functions are easier to test, easier to name (the name is the contract), and easier to change. Long functions accumulate special cases until nobody can hold the full behavior in their head — at which point the next change introduces a regression.
