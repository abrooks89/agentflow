"""Every written-out deploy-lock holder list names exactly `none` plus registry.BUILDERS (gh#580).

jr-dev-3 became a live builder lane and took the lock (PR 573), but five holder lists in
docs/REGISTRY.md and the builder skills still read `none|lead-dev|jr-dev-1|jr-dev-2`, and
jr-dev-3's own skill dropped jr-dev-2. A reader checking whether a hold is legitimate read
a real holder as outside the schema. The lists are prose, so nothing tied them to
`registry.BUILDERS`; this test does.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from agentflow import registry
from agentflow.tracked_paths import tracked_files

REPO_ROOT = Path(__file__).resolve().parents[3]

#: `holder: none|a|b` and `holder` is `none | a | b`: the two shapes the docs write it in.
_HOLDER_LIST = re.compile(r"holder(?::|`\s+is)\s+`?(none\s*\|[\w|\s-]+?)`")


def _is_doc(rel: str) -> bool:
    return rel.endswith(".md") and rel.startswith(("docs/", "skills/"))


def _holder_lists() -> list[tuple[str, int, list[str]]]:
    found = []
    # Tracked files only: a corpus walked off the checkout grades whatever is lying in it (gh#175).
    for path in tracked_files(REPO_ROOT, _is_doc):
        text = path.read_text(encoding="utf-8")
        for m in _HOLDER_LIST.finditer(text):
            names = [n.strip() for n in m.group(1).split("|") if n.strip()]
            line = text.count("\n", 0, m.start()) + 1
            found.append((path.relative_to(REPO_ROOT).as_posix(), line, names))
    return found


def test_the_scan_finds_the_known_holder_lists():
    """Positive control: a pattern that matched nothing would pass the real test vacuously."""
    per_file = Counter(path for path, _, _ in _holder_lists())
    # A floor per file, not just presence: lead-dev writes it twice, and a typo that made
    # one of those two unmatchable would otherwise drop out of the check unnoticed.
    floor = {"docs/REGISTRY.md": 1, "skills/jr-dev-1/SKILL.md": 1, "skills/jr-dev-2/SKILL.md": 1,
             "skills/jr-dev-3/SKILL.md": 1, "skills/lead-dev/SKILL.md": 2}
    short = {f: per_file[f] for f, n in floor.items() if per_file[f] < n}
    assert not short, f"holder lists no longer found where expected: {short}"


def test_the_pattern_reads_both_written_shapes():
    assert _HOLDER_LIST.search("(`holder: none|lead-dev|jr-dev-1`)").group(1) == \
        "none|lead-dev|jr-dev-1"
    assert _HOLDER_LIST.search("`holder` is `none | lead-dev | jr-dev-1`.").group(1) == \
        "none | lead-dev | jr-dev-1"


def test_every_holder_list_is_none_plus_the_builders():
    expected = ["none", *registry.BUILDERS]
    wrong = [f"{path}:{line} lists {names}" for path, line, names in _holder_lists()
             if sorted(names) != sorted(expected)]
    assert not wrong, f"holder lists out of step with registry.BUILDERS {expected}: {wrong}"
