---
title: "Toy calc.py single-task plan (BROKEN - task type out of vocabulary)"
date: 2026-07-17
epic: toy calc
slug: toy-calc-bad-task-type
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
    type: scaffold
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

# Toy calc.py single-task plan (BROKEN - bad task type)

Identical to `plan-single-task.md` (minus `project:`; parse-only fixtures are never
launched) with task 1's `type` set to a value outside
`{standard, tdd}`. `parse_plan` must hard-fail loudly, naming the offending key,
rather than warn-and-continue. Used by Scenario C to prove the loud-fail behavior.

**Why this fixture and not `plan-missing-epic.md`, which used to serve here.**
gh#74 made `epic` OPTIONAL: `solution/` holds human-authored operator documents as
well as orchestrator plans, and requiring orchestrator frontmatter of both dropped
fifty files from every survey, silently. A plan missing only `epic` is therefore no
longer malformed, so it can no longer prove a loud fail. An out-of-vocabulary task
`type` still is malformed - two live plans carry `type: scaffold` - so the PROPERTY
under test is unchanged and only its fixture moved.
