# CLAUDE.md — toy-repo

Minimal project instructions for the agentflow acceptance-harness fixture.

## Project

`toy-repo` is a tiny Python calculator used as a controlled target repo for the
agentflow Phase-0 acceptance harness. It exists so an orchestrator's Step-1
init-check (which requires a project instructions file to be present) passes
against a clean, deterministic repo.

## Layout

- `calc.py` — calculator module: `add`, `sub`, `mul`.
- `tests/test_calc.py` — pytest suite covering `calc.py`.

## Conventions

- Python 3, standard library only.
- Run tests with `python -m pytest tests/ -q`.
- Keep functions pure and side-effect free.
