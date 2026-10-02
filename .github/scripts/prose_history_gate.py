#!/usr/bin/env python3
"""Instruction-prose history gate: the prose agents read states rules, not the events behind them.

A rule in a skill keeps its reason and loses its incident. An issue number, a registry id, a date
or the name of whoever ruled it points at a record a reader of this repo cannot open, and lanes
add them by habit, so this gate refuses them at PR time. It is repo development tooling, run as a
step in CI's `sanitize-gate` job. It is not part of the `agentflow` package, imports nothing from
it, and runs on no user's project.

Usage, from the repo root (the set is read from the checkout's top level either way):

    python .github/scripts/prose_history_gate.py            # the whole guarded set
    python .github/scripts/prose_history_gate.py PATH ...   # only these files, repo-relative;
                                                            # each must be in the set

THE SET IT GUARDS is derived at run time from `git ls-files`, so a new skill file is guarded from
its first commit: `skills/**/*.md`, `commands/*.md`, `templates/*`, `skills/**/prompts/*.md`,
`skills/**/templates/*` and `install/SKILL.md`, minus `skills/_vendored/` and every `test/` or
`tests/` directory. Code, tests, `docs/`, README.md, CHANGELOG.md, CONTRIBUTING.md, the repo-root
CLAUDE.md, `harness/` and the `.js` prompt builders are outside it: history belongs there.

WHAT IT REFUSES, one class per token:
* NAME  - the copyright holder's surname, read from LICENSE's copyright line (never typed here);
* GH    - `gh#<digits>`, in any case;
* ISSUE - `issue #<digits>`;
* ID    - a `Q-`, `B-` or `E-` id of four or five digits, except the `Q-00NN` and `B-00NN`
          example ranges. `E-00NN` is refused too: queue examples use the `E-NNNN` placeholder;
* DATE  - a `YYYY-MM-DD` date, unless it sits inside a `DATE_OK` literal for its file.

`DATE_OK` is the only exemption. Each entry is an exact dated literal plus the reason it stays (a
config value, a path or a format example). There is no inline marker, because a marker is one
more line an agent reads. An entry whose literal is no longer in its file fails the gate, so the
allowlist cannot outlive its example.

EXIT CODES: 0 clean; 1 on any hit or stale `DATE_OK` entry; 2 when the scan did not run (not in
a git checkout, `git ls-files` failed, the derived set is empty, LICENSE has no copyright line,
a guarded file is unreadable, or a named path is outside the set). It never exits 0 without
having scanned.

WHAT IT CANNOT SEE, so nobody reads more protection into it than is here:
* undated narrative with no token ("the first run of this ..."). The full read of each prose file
  at review time covers that, not this script;
* commit shas and priority handles, because the prose teaches with examples such as `30bc39f`,
  `PR #271` and `p220`;
* the text a `.js` prompt builder sends to an agent;
* a real low id inside the example range: a live board can hold `B-0011`, and so can an example.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SPECS = (
    "skills/**/*.md",
    "commands/*.md",
    "templates/*",
    "skills/**/prompts/*.md",
    "skills/**/templates/*",
    "install/SKILL.md",
)

# The only dated text that stays in the guarded set: path -> ((literal, reason), ...).
DATE_OK: dict[str, tuple[tuple[str, str], ...]] = {
    "skills/_shared/SCHEMA.md": (
        ("2026-04-29T00:00:00+00:00", "config value: mtime_cutoff_iso's documented default"),
        ("scenarioF-carved/2026-08-30-", "fixture path: a recorded harness fixture listed as immutable"),
    ),
    "templates/environment.md": (
        ("2026-01-01T00:00:00+00:00", "config value: the template's mtime_cutoff_iso setting"),
    ),
    "skills/backlog/SKILL.md": (
        ("design/specs/2026-05-10-", "path example: the dated spec filename this skill writes"),
    ),
    "skills/choose-next/SKILL.md": (
        ("design/specs/2026-05-10-", "path example: a dated spec path in a worked example"),
    ),
    "skills/report/SKILL.md": (
        ("fleet report 2026-09-01T14:20Z", "format example: the report's timestamped heading line"),
    ),
    "skills/auditor/references/report-format.md": (
        ("2026-01-01-1.md", "path example: a link into a dated audit report"),
    ),
    "skills/orchestrator/prompts/implementer-with-verifier-loop.md": (
        ('"at": "2026-05-09T10:00:00Z"', "format example: a timestamp in a sample verdict record"),
    ),
    "skills/wiki-save/SKILL.md": (
        ("`2026-13-45`", "format example: a well-shaped impossible date a shape check passes"),
    ),
}

GH = re.compile(r"gh#\d+", re.I)
ISSUE = re.compile(r"\bissue #\d+", re.I)
ID = re.compile(r"\b([QBE])-(\d{4,5})\b")
DATE = re.compile(r"\b20\d\d-\d\d-\d\d")
COPYRIGHT = re.compile(r"^Copyright \(c\) \d{4} (.+?)\s*$", re.M)

WHERE_HISTORY_GOES = (
    "::error::prose history gate: instruction prose states a rule and its reason in the present "
    "tense, not the event behind it. Restate the rule and keep its reason; put the history in the "
    "commit message, the PR body, a code comment or test docstring (which keep their refs), "
    "CHANGELOG.md, the registry, or the wiki/ pillar. A new dated example needs a DATE_OK entry "
    "with its reason in .github/scripts/prose_history_gate.py."
)


class GateDidNotRun(Exception):
    """The scan could not run. Never read as clean."""


def _git(root: Path | None, *args: str) -> str:
    cmd = ["git"] + (["-C", str(root)] if root else []) + list(args)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    except OSError as exc:
        raise GateDidNotRun(f"cannot run git: {exc!r}") from exc
    if res.returncode != 0:
        raise GateDidNotRun(f"`git {' '.join(args)}` exited {res.returncode}: {res.stderr.strip()}")
    return res.stdout


def repo_root(start: Path | None = None) -> Path:
    """The checkout's top level, so the gate reads the same set from any working directory."""
    top = _git(start, "rev-parse", "--show-toplevel").strip()
    if not top:
        raise GateDidNotRun("git rev-parse --show-toplevel printed nothing")
    return Path(top)


def in_set(path: str) -> bool:
    return "skills/_vendored/" not in path and not re.search(r"(^|/)tests?/", path)


def derive_set(root: Path) -> list[str]:
    """The guarded paths, from `git ls-files` over SPECS. Empty is an error, never a clean pass."""
    listed = _git(root, "-c", "core.quotepath=off", "ls-files", "--", *SPECS).splitlines()
    files = sorted({p for p in listed if p and in_set(p)})
    if not files:
        raise GateDidNotRun(f"the derived set is empty: git ls-files matched nothing under {root}")
    return files


def holder_surname(root: Path) -> str:
    """The copyright holder's surname, from LICENSE, so this file never types a copy of it."""
    try:
        text = (root / "LICENSE").read_text(encoding="utf-8")
    except OSError as exc:
        raise GateDidNotRun(f"cannot read LICENSE: {exc!r}") from exc
    m = COPYRIGHT.search(text)
    if not m:
        raise GateDidNotRun("LICENSE has no `Copyright (c) <year> <holder>` line")
    return m.group(1).split()[-1]


def _inside_allowed(line: str, start: int, end: int, literals: tuple[str, ...]) -> bool:
    for lit in literals:
        i = line.find(lit)
        while i != -1:
            if i <= start and end <= i + len(lit):
                return True
            i = line.find(lit, i + 1)
    return False


def scan(root: Path, files: list[str], date_ok: dict, surname: str, *, check_all_entries: bool):
    """Return (hits, stale). hits: (path, line, CLASS, token); stale: (path, literal)."""
    name = re.compile(r"\b" + re.escape(surname) + r"\b", re.I)
    hits: list[tuple[str, int, str, str]] = []
    stale: list[tuple[str, str]] = []
    texts: dict[str, str] = {}
    for f in files:
        try:
            texts[f] = (root / f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise GateDidNotRun(f"cannot read guarded file {f}: {exc!r}") from exc
    for f in files:
        literals = tuple(lit for lit, _ in date_ok.get(f, ()))
        for n, line in enumerate(texts[f].splitlines(), 1):
            found = [("NAME", m.group()) for m in name.finditer(line)]
            found += [("GH", m.group()) for m in GH.finditer(line)]
            found += [("ISSUE", m.group()) for m in ISSUE.finditer(line)]
            found += [
                ("ID", m.group())
                for m in ID.finditer(line)
                if not (m.group(1) in "QB" and len(m.group(2)) == 4 and m.group(2).startswith("00"))
            ]
            found += [
                ("DATE", m.group())
                for m in DATE.finditer(line)
                if not _inside_allowed(line, m.start(), m.end(), literals)
            ]
            hits += [(f, n, kind, tok) for kind, tok in found]
    for path, entries in date_ok.items():
        if path not in texts and not check_all_entries:
            continue
        for lit, _ in entries:
            if lit not in texts.get(path, ""):
                stale.append((path, lit))
    return hits, stale


def run(argv: list[str], *, root: Path | None = None, date_ok: dict | None = None) -> int:
    date_ok = DATE_OK if date_ok is None else date_ok
    try:
        root = repo_root(root)
        guarded = derive_set(root)
        surname = holder_surname(root)
        if argv:
            named = []
            for a in argv:
                p = Path(a.replace("\\", "/"))
                rel = p.resolve().relative_to(root.resolve()) if p.is_absolute() else p
                named.append(rel.as_posix().removeprefix("./"))
            outside = sorted(set(named) - set(guarded))
            if outside:
                raise GateDidNotRun(f"not in the instruction-prose set: {outside}")
            files = [f for f in guarded if f in set(named)]
        else:
            files = guarded
        hits, stale = scan(root, files, date_ok, surname, check_all_entries=not argv)
    except (GateDidNotRun, ValueError) as exc:
        print(f"::error::prose history gate: the scan did not run: {exc}. Failing closed.")
        return 2
    for f, n, kind, tok in hits:
        print(f"{f}:{n}: {kind} {tok}")
    for path, lit in stale:
        print(f"{path}: STALE DATE_OK entry {lit!r} no longer appears in the file")
    by: dict[str, int] = {}
    for _, _, kind, _ in hits:
        by[kind] = by.get(kind, 0) + 1
    lines = len({(f, n) for f, n, _, _ in hits})
    counts = {k: by[k] for k in sorted(by)} or "none"
    print(f"{len(files)} files scanned; {lines} line(s) carry history: {counts}")
    if hits:
        print(WHERE_HISTORY_GOES)
    if stale:
        print(
            "::error::prose history gate: a DATE_OK entry no longer matches its file. Remove the "
            "entry, or fix it to the literal the file now carries."
        )
    return 1 if hits or stale else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
