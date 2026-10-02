---
title: "Toy calc.py single-task plan"
date: 2026-07-17
epic: "Toy calc single task"
slug: toy-calc-single-task
spec: "harness/fixtures/refine-toy.md"
current_state: approved-for-autonomous
per_wave_worktrees: true

phases:
  - phase-1

test_commands:
  regression: "python -m pytest tests/ -q"
  fast: "python -m pytest tests/test_calc.py -q"

tasks:
  1:
    phase: phase-1
    deps: []
    complexity: low
    type: standard
    verification:
      acceptance_criteria:
        - "calc.py exposes add(a, b) returning the arithmetic sum."
        - "tests/test_calc.py covers add() with a passing assertion."
      file_allowlist:
        - "calc.py"
        - "tests/test_calc.py"
      spec_format: "plain acceptance-criteria list"
      test_commands:
        - "python -m pytest tests/test_calc.py -q"
---

# Toy calc.py single-task plan

A minimal, behavior-frozen fixture plan that parses clean via the current
`lib.plan_parser.parse_plan`. Exactly one task, one phase, valid acyclic DAG.

## Task 1 — implement add()

Add an `add(a, b)` function to `calc.py` and a passing test in
`tests/test_calc.py`. No other files touched.
