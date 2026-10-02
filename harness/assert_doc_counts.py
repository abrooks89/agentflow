#!/usr/bin/env python3
"""Assert the docs state the assertion count and ID ranges that check.py wires.

Why this exists: the count was stated as 39 in four files while the registry
had grown to 42, and `README.md` printed the stale number in a table whose
adjacent cell was the exact command that disproves it (gh-55). C11-C13 landed
without a doc being touched. A fact repeated in five places with nothing tying
it to its source rots silently -- so tie it.

The source of truth is `check.EXPECTED_IDS`, the same tuple `--self-test`
counts. Nothing here hardcodes 42: change the registry and the expected prose
changes with it.

Two layers, because either alone leaves the class open:
  KNOWN   every location we know states the count must state the current one.
  SCAN    any OTHER line that talks about wired assertions must not state a
          stale number -- that catches a sixth location added later, which a
          fixed list never would.

Run: python harness/assert_doc_counts.py   (exit 0 clean, 1 with findings)
"""

from __future__ import annotations

import re
import subprocess
import sys
from itertools import groupby
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "harness"))

import check  # noqa: E402  - path set above


def expected_count() -> int:
    return len(check.EXPECTED_IDS)


def expected_ranges() -> str:
    """'A1-A9 / B1-B9 / C1-C13 / ...' derived from the registry, not typed."""
    parts = []
    for _letter, group in groupby(check.EXPECTED_IDS, key=lambda i: i[0]):
        ids = list(group)
        parts.append(ids[0] if len(ids) == 1 else "%s-%s" % (ids[0], ids[-1]))
    return " / ".join(parts)


#: Locations that state the count, and the regex that finds the number in each.
#: A location whose pattern stops matching is a FINDING, not a pass -- prose
#: gets rewritten, and a gate that silently matches nothing is not a gate.
KNOWN_COUNT = [
    ("README.md", r"\*\*(\d+) wired assertions\*\*"),
    ("harness/README.md", r"acceptance checker \((\d+) wired assertions"),
    ("docs/ARCHITECTURE.md", r"wires \*\*(\d+) deterministic assertions\*\*"),
    ("harness/auditor-scenario/README.md", r"\*\*(\d+)\*\* assertions wired"),
]

#: Locations that state the per-scenario ID ranges.
KNOWN_RANGES = [
    ("harness/README.md", r"(A1-A9 / B1-B9 / C1-C\d+ / D1-D4 / E1-E2 / F1-F4 / G1)"),
]

#: Lines mentioning wired assertions anywhere else in the tracked docs.
#: Scope is what `git ls-files` reports, NOT a filesystem walk. A walk reaches
#: into `.claude/worktrees/wf_*`, `.venv` and any other scratch tree a real
#: checkout accumulates, each holding an OLD copy of these same docs -- so the
#: gate passed in CI (fresh clone, none of that present) and failed on every
#: developer machine that had ever run a workflow. Tracked-files scope fixes
#: that by construction rather than by a skip list that has to guess the next
#: scratch directory. The fallback walk keeps the gate usable outside a git
#: checkout, and carries the skip list for exactly that case.
SCAN_GLOBS = ("*.md",)
SCAN_SKIP = ("_vendored", "node_modules", ".git", ".claude", ".venv", "records", "toy-repo")


def scan_candidates():
    """Tracked *.md paths, or a filtered walk when git is unavailable."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "*.md"],
                             capture_output=True, check=True, text=True).stdout
        rels = [r for r in out.split(chr(0)) if r]
        if rels:
            return [(r, REPO / r) for r in rels]
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for glob in SCAN_GLOBS:
        for path in REPO.rglob(glob):
            rel = path.relative_to(REPO).as_posix()
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            if any(skip in rel for skip in SCAN_SKIP):
                continue
            found.append((rel, path))
    return found


#: Count-stating shapes as they appear in the docs today. Emphasis-agnostic where
#: the phrase itself is the claim -- "N wired assertions" is never innocent prose --
#: and literal where a bare number needs its surrounding words to mean a count.
#: The README row shape `**43 wired assertions**` (README.md) was covered by no
#: alternative before gh#139: the third one required a literal `(` because it was
#: written verbatim against harness/README.md's parenthesised shape. KNOWN pins
#: README itself and main() skips KNOWN files, so an unlisted sixth file copying
#: that row stated a stale count and SCAN -- the layer that exists for exactly the
#: unlisted case -- saw nothing. gh#139 worded this as KNOWN being unable to see
#: README's row; that was checked and is false, KNOWN_COUNT[0] matches that line
#: and the gate reports 4 locations. Dropping the `(` subsumes the old alternative
#: rather than adding a fifth.
#: Deliberately NOT loosened to a bare `(\d+) assertions`: that reads
#: harness/uc-scenario/README.md, "all 13 assertions PASS" -- a different registry's
#: true count -- as a stale statement of this one, turning the gate red on correct
#: prose.
SCAN_PATTERN = re.compile(r"(\d+)\s*(?:wired |deterministic )?assertions? wired|"
                          r"\*\*(\d+)\*\* assertions wired|"
                          r"(\d+) wired assertions|"
                          r"wires \*\*(\d+) deterministic assertions")


def main() -> int:
    count, ranges = expected_count(), expected_ranges()
    findings = []

    for rel, pattern in KNOWN_COUNT:
        path = REPO / rel
        if not path.exists():
            findings.append("%s: MISSING (known count location)" % rel)
            continue
        found = re.findall(pattern, path.read_text(encoding="utf-8"))
        if not found:
            findings.append(
                "%s: pattern %r matched nothing -- prose changed shape; "
                "update this gate deliberately rather than deleting it" % (rel, pattern))
        for got in found:
            if int(got) != count:
                findings.append("%s: states %s wired assertions, check.py wires %d"
                                % (rel, got, count))

    for rel, pattern in KNOWN_RANGES:
        path = REPO / rel
        found = re.findall(pattern, path.read_text(encoding="utf-8"))
        if not found:
            findings.append("%s: range pattern %r matched nothing" % (rel, pattern))
        for got in found:
            if got != ranges:
                findings.append("%s: states ranges %r, check.py wires %r" % (rel, got, ranges))

    known = {rel for rel, _ in KNOWN_COUNT}
    for rel, path in scan_candidates():
        if rel in known or not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8",
                                                     errors="replace").splitlines(), 1):
            for match in SCAN_PATTERN.finditer(line):
                got = next(g for g in match.groups() if g)
                if int(got) != count:
                    findings.append(
                        "%s:%d: states %s wired assertions, check.py wires %d "
                        "(unlisted location -- add it to KNOWN_COUNT)"
                        % (rel, lineno, got, count))

    if findings:
        print("FAIL doc-counts: %d finding(s)" % len(findings))
        for f in findings:
            print("  - %s" % f)
        print("Source of truth: check.EXPECTED_IDS (%d ids, %s)" % (count, ranges))
        return 1

    print("PASS doc-counts: %d wired assertions (%s) stated consistently in %d locations"
          % (count, ranges, len(KNOWN_COUNT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
