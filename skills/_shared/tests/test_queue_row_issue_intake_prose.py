"""An issue-sourced Queue row passes the same intake filter a built bug does: the prose pins.

gh#487. `registry publish` does not query the tracker, by design - the registry has no GitHub
call and must not grow one. So the only thing standing between a raw issue list and a junior
lane is lead-dev's §2.6, and before this pin §2.6 said nothing about issues at all: the intake
rule lived in §2 (selection, for the item lead-dev builds itself) and the end-of-firing check
covered only "any bug built". A row published from an untriaged issue skipped both.

WHY SECTION-SCOPED. `issue_intake selectable` is already named in Fixed coordinates and in §2,
so a file-wide substring passes with §2.6 untouched. Each predicate slices its own section first.

POSITIVE CONTROLS. Each predicate is run on the pre-fix text, embedded below as quoted lines
(not read from git), and must fail there. A predicate that passes on the unwired text pins
nothing.

WHAT IT CANNOT PROVE. That a wake follows the prose. `issue_intake selectable` itself is covered
by its own tests.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "skills"

SECTION_26 = "## 2.6 Publish the Queue"
SECTION_9 = "## 9. End-of-firing checklist"
SELECTABLE_CMD = "python -m agentflow.issue_intake selectable --project <name>"


def _read(rel: str) -> str:
    path = SKILLS / rel
    assert path.is_file(), f"{rel} not found - moved, or this pin's path is stale"
    return path.read_text(encoding="utf-8")


def _slice(text: str, start: str, end: str = "\n## ") -> str:
    """From `start` to the next `end` after it; empty when `start` is absent."""
    i = text.find(start)
    if i == -1:
        return ""
    j = text.find(end, i + len(start))
    return text[i:] if j == -1 else text[i:j]


def _section_26_failures(text: str) -> list[str]:
    sec = _slice(text, SECTION_26)
    if not sec:
        return [f"{SECTION_26!r} heading not found"]
    bad = []
    if "issue-sourced" not in sec:
        bad.append("§2.6 does not address issue-sourced rows")
    if SELECTABLE_CMD not in sec:
        bad.append(f"§2.6 does not name `{SELECTABLE_CMD}` for issue-sourced rows")
    if "THIS firing" not in sec:
        bad.append("§2.6 does not require the selectable read in the same firing as the publish")
    if "label annotation" not in sec:
        bad.append("§2.6 does not forbid a triage-label annotation on the row")
    return bad


def _section_9_failures(text: str) -> list[str]:
    sec = _slice(text, SECTION_9)
    if not sec:
        return [f"{SECTION_9!r} heading not found"]
    if "Any bug built or published this firing" not in sec:
        return ["the end-of-firing check does not cover a bug PUBLISHED into a lane"]
    return []


def test_section_26_gates_issue_sourced_rows_on_issue_intake_selectable():
    bad = _section_26_failures(_read("lead-dev/SKILL.md"))
    assert not bad, bad


def test_end_of_firing_check_covers_built_or_published():
    bad = _section_9_failures(_read("lead-dev/SKILL.md"))
    assert not bad, bad


# Pre-fix text, quoted. §2.6 opened like this and never mentioned issues; the checklist line
# named only "built".
_PRE_FIX = """\
## 2.6 Publish the Queue — disjoint jr-dev lanes (every firing, after selection + grounding)

Keep BOTH parallel builders fed. After your item is selected and grounded, sweep the remaining
READY dispatchable epics (`selectable(records, proj.excluded_domains, project=<name>)` minus
your pick

## 3. Frame the goal (the goal step)

## 9. End-of-firing checklist

- [ ] **Any bug built this firing was `triaged` and not `epic`** — it came from
      `agentflow.issue_intake selectable` (§2 steps 3.5/4), never from a raw issue list

## 10. Next
"""


def test_positive_control_section_26_predicate_fails_on_pre_fix_text():
    assert _section_26_failures(_PRE_FIX), "the §2.6 predicate passes on text with no issue rule"


def test_positive_control_section_9_predicate_fails_on_pre_fix_text():
    assert _section_9_failures(_PRE_FIX), "the §9 predicate passes on the built-only line"


def test_section_scoping_is_load_bearing():
    """The command is named elsewhere in the file; a file-wide check could not fail."""
    text = _read("lead-dev/SKILL.md")
    outside = text.replace(_slice(text, SECTION_26), "")
    assert "issue_intake selectable" in outside, (
        "if no other section names the command, this pin's scoping argument is stale"
    )
