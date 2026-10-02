---
slug: toy-calc-feature
title: "Toy calc.py arithmetic feature"
depends_on: []
current_state: speccing
last_modified: 2026-07-17
---

# Toy calc.py arithmetic feature

A tiny frozen spec doc used as canned input for Scenario A (refine -> solution
emits a parseable plan). It describes a 2-3 task feature against `calc.py` in
the toy-repo, small enough that the solution stage produces a plan matching
`plan-single-task.md`'s shape.

## Goal

Give the toy-repo `calc.py` a minimal arithmetic surface with tests, so the
harness can exercise the full refine -> solution -> orchestrator path against a
trivial, deterministic feature.

## Proposed tasks

1. **add(a, b)** — implement addition in `calc.py`; add a passing test in
   `tests/test_calc.py`.
2. **subtract(a, b)** — implement subtraction alongside `add`; extend
   `tests/test_calc.py` with a subtraction assertion.
3. **multiply(a, b)** — implement multiplication; extend the test module with a
   multiplication assertion.

## Boundary

- Only `calc.py` and `tests/test_calc.py` are touched.
- Pure functions, no I/O, no external dependencies.
- Each task keeps the pytest baseline green.
