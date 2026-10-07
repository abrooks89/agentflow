#!/usr/bin/env python3
"""Skill-size ratchet: every SKILL.md has a line ceiling, and a raised ceiling is declared.

Every loop wake re-reads its whole SKILL.md, so a skill that grows costs context on every firing.
Growth is allowed, but only as a visible, explained decision. Two halves enforce that:

* THE CEILING. `skills/_shared/skill_budgets.json` maps each budgeted skill to a line ceiling.
  `skills/_shared/tests/test_skill_size_budget.py` fails when a SKILL.md exceeds its ceiling, when
  a budgeted SKILL.md has no entry, and when an entry names no SKILL.md. That test runs in the
  `_shared pytest` jobs and calls the functions below.
* THE DECLARATION. This script, run as a step in CI's `sanitize-gate` job on a pull request,
  compares the budget file against the base the PR merges onto. Every ceiling the PR raises needs
  one line in the PR body:

      skill-budget: <skill> +<N> because <reason>

  where <N> is exactly the raise. A ceiling counts as raised when it is above its base value, or,
  for a key the base did not have (a new skill, or a renamed one), when it is above its file's
  current line count. So renaming a key cannot carry a free raise. A raise with no line, a line
  whose <N> differs from the raise, a malformed `skill-budget:` line, two lines for one skill,
  and a line naming an unbudgeted skill all fail. A line for a skill whose ceiling did not rise
  is reported and passes. A lowered ceiling needs no line.

  Reading the body: a leading `- `, `* ` or `> ` is stripped first, so a bulleted or quoted
  declaration counts. A line still holding the literal `<skill>` is a quoted template and is
  skipped. Inside a fenced code block a malformed line is not reported, because it is an example.
  PR-body text is never echoed: a bad line is reported by its line number in the body only, so
  author text cannot reach the runner as a workflow command.

THE BUDGETED SET is read from the git index, never the working tree: tracked `skills/<dir>/SKILL.md`
(exactly one level down) plus `install/SKILL.md`, keyed by the directory name (`install` for the
latter). `skills/_vendored/` is excluded: those skills are third-party copies, and their size is
not this repo's decision. Membership comes from the index and content from the working tree. A
line count is the number of newline bytes, which is what `wc -l` prints, so CRLF counts the same
and a last line with no newline is not counted.

Usage, from the repo root:

    PR_BODY=... python .github/scripts/skill_budget_gate.py --base <rev>   # the CI declaration check
    python .github/scripts/skill_budget_gate.py --regenerate              # every ceiling := its count

`--regenerate` resets EVERY ceiling to its file's current count, lowering slack ones too. To raise
one ceiling, edit that entry by hand and add the PR-body line.

EXIT CODES: 0 pass; 1 on any undeclared or mis-declared raise; 2 when the check did not run (not
in a git checkout, the base revision is not fetched, PR_BODY is unset, or a budget file is
malformed). It never exits 0 without having compared.

It is repo development tooling: not part of the `agentflow` package, and run on no user's project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BUDGET_FILE = "skills/_shared/skill_budgets.json"
VENDORED = "_vendored"
ADVISORY_SLACK = 50
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
DECL_START = re.compile(r"skill-budget\s*:", re.I)
DECL = re.compile(r"skill-budget\s*:\s*(?P<skill>[a-z0-9][a-z0-9-]*)\s+\+(?P<n>[1-9][0-9]*)\s+because\s+(?P<reason>\S.*)", re.I)
DECL_FORM = "skill-budget: <skill> +<N> because <reason>"
LIST_MARK = re.compile(r"^(?:[-*>]\s+)+")
FENCE = re.compile(r"^(```|~~~)")


class GateDidNotRun(Exception):
    """The check could not run. Never read as a pass."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", check=False
        )
    except OSError as exc:
        raise GateDidNotRun(f"cannot run git: {exc!r}") from exc


def repo_root(start: Path | None = None) -> Path:
    res = _git(start or Path.cwd(), "rev-parse", "--show-toplevel")
    top = res.stdout.strip()
    if res.returncode != 0 or not top:
        raise GateDidNotRun(f"not in a git checkout: {res.stderr.strip()}")
    return Path(top)


def is_budgeted(rel: str) -> bool:
    """`skills/<dir>/SKILL.md` exactly one level down, not vendored, or `install/SKILL.md`."""
    if rel == "install/SKILL.md":
        return True
    parts = rel.split("/")
    return len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md" and parts[1] != VENDORED


def discover(root: Path) -> dict[str, str]:
    """Budgeted skill name -> repo-relative SKILL.md path, from the git index at `root`."""
    # A pathspec only narrows the listing: git's `*` crosses `/`, so the shape is checked in Python.
    res = _git(root, "-c", "core.quotepath=off", "ls-files", "-z", "--", "skills", "install/SKILL.md")
    if res.returncode != 0:
        raise GateDidNotRun(f"git ls-files failed in {root}: {res.stderr.strip()}")
    found: dict[str, str] = {}
    for rel in sorted(p for p in res.stdout.split("\0") if p and is_budgeted(p)):
        name = "install" if rel == "install/SKILL.md" else rel.split("/")[1]
        if name in found:
            raise GateDidNotRun(f"{found[name]} and {rel} would share the budget key {name!r}")
        found[name] = rel
    if not found:
        raise GateDidNotRun(f"no tracked SKILL.md found under {root}")
    return found


def line_count(path: Path) -> int:
    return path.read_bytes().count(b"\n")


def parse_budgets(text: str, source: str) -> dict[str, int]:
    """The budget file's {skill: ceiling} map. Anything else is malformed, never partly read."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GateDidNotRun(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GateDidNotRun(f"{source} must be a JSON object of skill -> line ceiling")
    for key, value in data.items():
        if not NAME.fullmatch(key):
            raise GateDidNotRun(f"{source}: {key!r} is not a skill name")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GateDidNotRun(f"{source}: the ceiling for {key!r} must be a positive integer, not {value!r}")
    return data


def load_budgets(root: Path) -> dict[str, int]:
    path = root / BUDGET_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateDidNotRun(f"cannot read {BUDGET_FILE}: {exc!r}") from exc
    return parse_budgets(text, BUDGET_FILE)


def current_counts(root: Path) -> dict[str, int]:
    return {name: line_count(root / rel) for name, rel in discover(root).items()}


def check_ceilings(root: Path, budgets: dict[str, int]) -> tuple[list[str], list[str]]:
    """(errors, advisories) for the tree at `root` against `budgets`."""
    skills = discover(root)
    errors: list[str] = []
    advisories: list[str] = []
    for name in sorted(set(skills) - set(budgets)):
        errors.append(f"{skills[name]} has no budget: add \"{name}\": <ceiling> to {BUDGET_FILE}")
    for name in sorted(set(budgets) - set(skills)):
        errors.append(f"{BUDGET_FILE} budgets {name!r}, which names no budgeted SKILL.md: remove the entry")
    for name in sorted(set(skills) & set(budgets)):
        count, ceiling = line_count(root / skills[name]), budgets[name]
        if count > ceiling:
            over = count - ceiling
            errors.append(
                f"{skills[name]} is {count} lines, over its budget of {ceiling} by {over}: cut it, or "
                f"set \"{name}\": {count} in {BUDGET_FILE} and add "
                f"'skill-budget: {name} +{over} because <reason>' to the PR body"
            )
        elif ceiling - count > ADVISORY_SLACK:
            advisories.append(f"budget for {name} can drop to {count} ({skills[name]} is {count} lines, budget {ceiling})")
    return errors, advisories


def parse_declarations(body: str) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """({skill: (N, body line number)}, errors) from the `skill-budget:` lines of a PR body.

    No error carries body text: a line is named by its 1-based number in the body.
    """
    decls: dict[str, tuple[int, int]] = {}
    errors: list[str] = []
    fenced = False
    for lineno, raw in enumerate(body.replace("\r", "").split("\n"), 1):
        line = LIST_MARK.sub("", raw.strip())
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not DECL_START.match(line) or "<skill>" in line:
            continue
        m = DECL.fullmatch(line)
        if not m:
            if not fenced:
                errors.append(f"PR body line {lineno} is a malformed skill-budget line; expected `{DECL_FORM}`")
            continue
        skill = m.group("skill").lower()
        if skill in decls:
            errors.append(f"PR body lines {decls[skill][1]} and {lineno} declare the same skill; keep one")
            continue
        decls[skill] = (int(m.group("n")), lineno)
    return decls, errors


def check_raises(
    base: dict[str, int], head: dict[str, int], counts: dict[str, int], body: str
) -> tuple[list[str], list[str]]:
    """(errors, notes): every ceiling raised in `head` must be declared in `body`.

    Against `base` for a key it has; against the file's current line count (`counts`) for a key it
    has not, so a new or renamed key is budgeted at its size unless the raise is declared.
    """
    decls, errors = parse_declarations(body)
    notes: list[str] = []
    raised: dict[str, tuple[int, str]] = {}
    for k in sorted(head):
        if k in base:
            if head[k] > base[k]:
                raised[k] = (head[k] - base[k], f"{base[k]} -> {head[k]}")
        elif k in counts and head[k] > counts[k]:
            raised[k] = (head[k] - counts[k], f"new key at {head[k]}, its file is {counts[k]} lines")
    for skill, (delta, how) in raised.items():
        if skill not in decls:
            errors.append(
                f"the budget for {skill} rose by +{delta} ({how}) and the PR body declares no raise: "
                f"add the line 'skill-budget: {skill} +{delta} because <reason>'"
            )
        elif decls[skill][0] != delta:
            errors.append(
                f"the budget for {skill} rose by +{delta} ({how}), "
                f"but PR body line {decls[skill][1]} declares +{decls[skill][0]}"
            )
        else:
            notes.append(f"budget for {skill} raised by +{delta}, declared")
    for skill in sorted(set(decls) - set(raised)):
        if skill not in head:
            errors.append(f"PR body line {decls[skill][1]} names a skill with no budget in {BUDGET_FILE}")
        else:
            notes.append(f"a skill-budget line names {skill}, whose budget did not rise; nothing to declare")
    return errors, notes


def base_budgets(root: Path, rev: str) -> dict[str, int] | None:
    """The budget file at `rev`, or None when that commit has no budget file yet."""
    if _git(root, "cat-file", "-e", f"{rev}^{{commit}}").returncode != 0:
        raise GateDidNotRun(f"base revision {rev!r} is not in this checkout; fetch it first")
    listed = _git(root, "ls-tree", "--name-only", rev, "--", BUDGET_FILE)
    if listed.returncode != 0:
        raise GateDidNotRun(f"git ls-tree {rev} failed: {listed.stderr.strip()}")
    if not listed.stdout.strip():
        return None
    shown = _git(root, "show", f"{rev}:{BUDGET_FILE}")
    if shown.returncode != 0:
        raise GateDidNotRun(f"git show {rev}:{BUDGET_FILE} failed: {shown.stderr.strip()}")
    return parse_budgets(shown.stdout, f"{BUDGET_FILE} at {rev}")


def regenerate(root: Path) -> dict[str, int]:
    budgets = dict(sorted(current_counts(root).items()))
    (root / BUDGET_FILE).write_text(json.dumps(budgets, indent=2) + "\n", encoding="utf-8", newline="\n")
    return budgets


def run(argv: list[str], *, root: Path | None = None, env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="Skill-size ratchet: declared budget raises.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--base", metavar="REV", help="check raised ceilings against this base commit")
    mode.add_argument("--regenerate", action="store_true", help="set every ceiling to its file's line count")
    args = parser.parse_args(argv)
    try:
        root = repo_root(root)
        if args.regenerate:
            budgets = regenerate(root)
            print(f"wrote {BUDGET_FILE}: {len(budgets)} ceilings")
            return 0
        if "PR_BODY" not in env:
            raise GateDidNotRun("PR_BODY is not set; the step must pass the pull request body")
        head = load_budgets(root)
        counts = current_counts(root)
        base = base_budgets(root, args.base)
    except GateDidNotRun as exc:
        print(f"::error::skill budget gate: the check did not run: {exc}. Failing closed.")
        return 2
    if base is None:
        print(f"skill budget gate: {BUDGET_FILE} is new at this PR; every ceiling is checked against its file's line count")
        base = {}
    errors, notes = check_raises(base, head, counts, env["PR_BODY"])
    for note in notes:
        print(f"skill budget gate: {note}")
    for err in errors:
        print(f"::error::skill budget gate: {err}")
    if not errors:
        print(f"skill budget gate: {len(head)} budgets compared against {args.base}; every raise is declared")
    return 1 if errors else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
