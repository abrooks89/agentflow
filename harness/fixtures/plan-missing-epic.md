---
title: "Toy calc.py single-task plan (VALID since gh#74 - no epic)"
date: 2026-07-17
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

# Toy calc.py single-task plan (no epic - VALID since gh#74)

Identical to `plan-single-task.md` with the `epic:` frontmatter key deleted. It
used to prove the loud-fail behaviour; gh#74 made `epic` optional, because
`solution/` holds human-authored operator documents as well as orchestrator plans
and requiring orchestrator frontmatter of both dropped fifty files from every
survey, silently.

**It is kept, with its assertion INVERTED rather than deleted**: C14 now proves this
parses. A gate that simply disappeared when the rule changed would leave nothing
recording that the old behaviour was retired on purpose.
