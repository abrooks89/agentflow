"""Every loop that MERGES an epic must carry the completion-truth reconcile duty in prose.

WHY THIS FILE EXISTS. gh#75. ``selectable()``'s predicate reads spec frontmatter only -- it
ignores the status.md-derived ``lifecycle_state`` already on the record it is handed -- so an epic
whose PR merged while its spec still says ``current_state: approved-for-autonomous`` stays in
``selectable()``, where lead-dev can re-select it on a later wake or re-publish it into a junior
lane as if it were unbuilt. (The juniors never call ``selectable()``; they claim only what
lead-dev publishes, so lead-dev is the only re-offer path.) The write that closes that window --
``current_state: done`` plus a ``completion-truth:`` note -- was documented at a merge in no lane
at all.

The delta this pins down, stated no wider than it was checked: the juniors DO inherit lead-dev's
section-2.5 write -- their LOAD-BEARING Inheritance section names "grounding 2.5" verbatim and
binds them to follow it -- but that write fires at pre-build grounding, which for their own merge
is the NEXT wake that happens to re-select the epic. Nothing put the write at the merge itself,
in any lane. gh#75's ``grep -c`` measured the junior FILES, not the junior PROCEDURE.
Compliance has in fact held -- as of
2026-09-03, no recurrence across the 42 PRs merged since the issue was filed -- with steward's
domain-A backlog-truth sweep reconciling frontmatter against merged shas on a timer. That sweep
is a periodic net, not a duty at the point of work, and nothing would announce it if steward
stopped waking.

WHAT THIS GATE CAN AND CANNOT DO -- stated plainly, because a gate that overstates itself is
worse than none:

* It CAN prove each merging lane's SKILL.md still STATES the duty; that it states it in a chunk
  that also carries merge wording AND same-pass timing wording, rather than only as an
  after-the-fact repair; that it carries the duty as a checklist box and not prose alone; and
  that lead-dev's completion-truth prose still names every junior lane nearby.
* It CANNOT prove any lane PERFORMS the write. Nothing in this repo observes a live wake; a pass
  here means the instruction is present and worded at the merge, not that an epic's frontmatter
  is current.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]

# The loops that merge an epic's PR, and therefore each need the reconcile write at their own
# merge point. Deliberately NOT every loop:
#   dev-manager  - the human's seat. It promotes specs to approved-for-autonomous and reviews;
#                  it does not build or merge epics, so it has no merge point to hang this on.
#   auditor      - read-only against the product. It reviews already-done epics and drafts new
#                  ones for approval; it never merges, so the duty would have no trigger.
#   steward      - the periodic net BEHIND this duty, not a lane that ships work. Its domain-A
#                  sweep reconciles frontmatter against merged shas on a timer; requiring the
#                  merge-time wording of it would misdescribe what it does.
#   orchestrator - it DOES run the merge (substep 7d squash-merges the PR and sets
#                  state.status = "done"), and every lane in MERGING_LOOPS reaches its merge THROUGH it
#                  via /project. It is out of this set because 7d already discharges the
#                  status.md half of completion truth, while the spec-frontmatter half belongs to
#                  the DISPATCHING lane - the one that owns the spec and its lane's registry row,
#                  and the one this gate reads.
MERGING_LOOPS = ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3")

# Loops allowed to state the rule without being a merging lane (see the coverage test). Both
# cite the READ half - completion truth comes from status.md, never frontmatter - and neither
# writes at a merge: steward sweeps on a timer, the auditor only filters its done-epic window.
ACKNOWLEDGED_NON_MERGING = ("steward", "auditor")

_CHECKLIST_HEADING = {
    "lead-dev": "## 9. End-of-firing checklist",
    "jr-dev-1": "## End-of-firing checklist",
    "jr-dev-2": "## End-of-firing checklist",
    "jr-dev-3": "## End-of-firing checklist",
}

# Behaviour, not one token: any of the wordings a reader would recognise as this duty counts, so
# a legitimate rewrite does not red the gate. None of these survives in a file that has dropped
# the duty entirely, which is the state this exists to catch.
_DUTY_PATTERNS = (
    re.compile(r"completion[- ]truth", re.I),
    re.compile(r"current_state:\s*done", re.I),
    re.compile(r"reconcil\w*(?:\s+\S+){0,8}\s+frontmatter", re.I),
    re.compile(r"frontmatter(?:\s+\S+){0,8}\s+reconcil\w*", re.I),
)

# The duty has to sit at the merge, not only in a repair branch that fires when some later wake
# re-selects the epic - by then the stale window has already been open for a whole wake. Both
# halves are required together: "merge" alone is ambient in a builder skill (merge conflicts,
# auto-merge to main), and the timing word is what separates "write it when you merge" from
# "something reconciles it eventually".
_MERGE_PATTERN = re.compile(r"\bmerge[ds]?\b", re.I)
_SAME_PASS = re.compile(r"same pass|same firing|same wake", re.I)

_CONSEQUENCE = (
    "a shipped epic stays in selectable(), where lead-dev can re-select it or re-publish it "
    "into a lane"
)


def _skill_text(name: str) -> str:
    path = SKILLS_ROOT / name / "SKILL.md"
    # FAIL, never skip: a renamed or moved loop would silently stop being guarded while CI
    # stayed green - the gate would quietly cover nothing.
    assert path.is_file(), f"{name}/SKILL.md not found at {path} - renamed, or the gate's list is stale"
    return path.read_text(encoding="utf-8", errors="replace")


def _flat(text: str) -> str:
    """Collapse whitespace so a claim that wraps across lines still matches."""
    return re.sub(r"\s+", " ", text)


_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]\s|\d+(?:\.\d+)*\.\s)")


def _blocks(text: str) -> list[str]:
    """One paragraph, or one list ITEM, per entry.

    Blank lines alone are far too coarse for these files: a whole numbered section or an entire
    end-of-firing checklist is one blank-line-free run of hundreds of words, so an unrelated
    "merge conflicts" bullet and an unrelated completion-truth bullet land in the same "block"
    and read as one statement. Splitting at list-item boundaries as well keeps each bullet, each
    numbered step and each checklist box its own chunk; a wrapped continuation line carries no
    list marker, so it stays with the item it belongs to.
    """
    chunks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip() or _LIST_ITEM.match(line):
            if current:
                chunks.append(_flat("\n".join(current)))
            current = []
        if line.strip():
            current.append(line)
    if current:
        chunks.append(_flat("\n".join(current)))
    return chunks


def _states_duty(chunk: str) -> bool:
    return any(pattern.search(chunk) for pattern in _DUTY_PATTERNS)


def _checklist_section(skill: str) -> str:
    return _checklist_section_of(_skill_text(skill), skill)


def _checklist_section_of(text: str, skill: str) -> str:
    head = _CHECKLIST_HEADING[skill]
    start = text.find(head)
    assert start != -1, (
        f'{skill}/SKILL.md has no "{head}" section - this gate anchors on it; renaming the '
        "checklist silently un-gates every box in it"
    )
    end = text.find("\n## ", start + len(head))
    return text[start:] if end == -1 else text[start:end]


# --- gh#75: the duty exists at all, in every lane that merges -------------------------------
# Re-reds if the reconcile paragraph is deleted from any merging lane's SKILL.md.


@pytest.mark.parametrize("skill", MERGING_LOOPS)
def test_every_merging_loop_states_the_completion_truth_reconcile(skill: str) -> None:
    text = _skill_text(skill)
    assert _states_duty(text), (
        f"{skill}/SKILL.md states no completion-truth reconcile duty (no 'completion-truth', no "
        f"'current_state: done', no reconcile-the-frontmatter wording) - this lane merges epics, "
        f"so without it {_CONSEQUENCE}"
    )


# --- gh#75: the duty is at the MERGE, not only in a later repair branch ---------------------
# Re-reds if the duty survives only as lead-dev's 2.5 ALREADY BUILT branch, which fires at
# pre-build grounding on some later wake and never on the lane's own ship. Mutating a lane's
# wording from "in the same pass as the merge" to a deferral ("lead-dev reconciles it on its next
# pass") drops that lane to zero matching chunks and reds this.


#
# It reads only the text outside the end-of-firing checklist: each lane's checklist box carries
# the duty, merge and same-pass words too, so with the box in scope the box alone satisfied this
# and the prose beside the merge could be deleted with the gate still green.
#
# The chunk must also name the WRITE (`current_state: done`, or reconciling the frontmatter), not
# only the phrase "completion truth": lead-dev's selection step names the concept and points at
# the lanes that "write the reconcile in the same pass as the merge", which is a cross-reference,
# not the duty, and on its own it kept lead-dev green with its merge prose deleted.

_WRITE_PATTERNS = _DUTY_PATTERNS[1:]

# The opening words of each lane's merge-time duty prose. The mutation control below anchors its
# deletion on this; if the prose is reworded the control fails loudly rather than deleting nothing.
_MERGE_DUTY_ANCHOR = "**Reconcile completion truth in the SAME pass as the merge"


def _is_merge_duty_chunk(chunk: str) -> bool:
    return bool(
        any(p.search(chunk) for p in _WRITE_PATTERNS)
        and _MERGE_PATTERN.search(chunk)
        and _SAME_PASS.search(chunk)
    )


def _merge_duty_chunks(text: str, skill: str) -> list[str]:
    return [b for b in _blocks(_outside_checklist(text, skill)) if _is_merge_duty_chunk(b)]


def _delete_merge_duty_prose(text: str) -> str:
    """Cut the duty prose from its anchor to the end of the paragraph or list item holding it.

    In the junior lanes the duty is the tail of a longer claim step, so the cut starts at the
    anchor rather than at the item's marker and leaves the claim instructions in place.
    """
    start = text.find(_MERGE_DUTY_ANCHOR)
    assert start != -1, f"anchor {_MERGE_DUTY_ANCHOR!r} not found - the control has nothing to delete"
    assert text.find(_MERGE_DUTY_ANCHOR, start + 1) == -1, "anchor is not unique"
    line_end = text.find("\n", start)
    end = len(text)
    while line_end != -1:
        next_end = text.find("\n", line_end + 1)
        line = text[line_end + 1 : len(text) if next_end == -1 else next_end]
        if not line.strip() or _LIST_ITEM.match(line):
            end = line_end
            break
        line_end = next_end
    return text[:start] + text[end:]


@pytest.mark.parametrize("skill", MERGING_LOOPS)
def test_reconcile_duty_is_stated_beside_the_merge(skill: str) -> None:
    matching = _merge_duty_chunks(_skill_text(skill), skill)
    assert matching, (
        f"{skill}/SKILL.md states the reconcile, but no single bullet or paragraph of it carries "
        f"the duty, a merge and same-pass timing together - so the write is scheduled for some "
        f"later wake, and until it lands {_CONSEQUENCE}"
    )


@pytest.mark.parametrize("skill", MERGING_LOOPS)
def test_beside_the_merge_check_rejects_a_lane_left_with_only_its_checklist_box(
    skill: str,
) -> None:
    text = _skill_text(skill)
    mutated = _delete_merge_duty_prose(text)
    removed = len(text) - len(mutated)
    assert removed > 200, f"the mutation removed only {removed} chars from {skill} - not the duty prose"
    assert _MERGE_DUTY_ANCHOR not in mutated
    # The box must survive and still carry every word the check looks for; otherwise this control
    # would not be testing the box-alone case at all.
    box = _checklist_section_of(mutated, skill)
    assert box == _checklist_section(skill), f"the mutation touched {skill}'s checklist"
    assert any(_is_merge_duty_chunk(b) for b in _blocks(box)), (
        f"{skill}'s checklist box no longer carries the duty words, so this control is vacuous"
    )
    assert _merge_duty_chunks(text, skill), f"the check rejects {skill}'s unmutated SKILL.md"
    assert not _merge_duty_chunks(mutated, skill), (
        f"with {skill}'s merge-time duty prose deleted the check still passes - something other "
        "than that prose satisfies it"
    )


# --- gh#75: checklist box, not prose alone -------------------------------------------------
# Naming the write mid-prose is the half that is easy to fake; the box is what a lane ticks.


@pytest.mark.parametrize("skill", MERGING_LOOPS)
def test_reconcile_duty_is_a_checklist_step_not_only_prose(skill: str) -> None:
    section = _checklist_section(skill)
    boxes = [b for b in _blocks(section) if b.lstrip().startswith("- [ ]") and _states_duty(b)]
    assert boxes, (
        f"{skill}/SKILL.md has no end-of-firing checklist box for the completion-truth reconcile - "
        f"prose alone is the fakeable half of this duty, and a firing that skips the write leaves "
        f"{_CONSEQUENCE}"
    )


# --- the issue-sourced branch of the duty is stated, not only the spec-backed one -----------
# A row lead-dev published from an issue has no spec, so the frontmatter write above cannot
# apply to it; its completion truth is the PR's Source line plus `release --state done`, with
# the phase saying so. The generic patterns above match a lane that states only the spec-backed
# half, so on their own they pass vacuously for an issue-sourced merge. These two tests require
# the issue-origin branch in the same chunk as the duty, in prose beside the merge and as a
# checklist box. Like the rest of this file they read the instruction, not a live wake: they
# cannot show a lane runs the release. Re-reds if any one of the three markers below is dropped
# from a lane's merge prose, or from its checklist box, independently of the other.
#
# The markers are literal on purpose: `Source: gh#N` is the PR-body line the auditor's
# fixed-issue pass reads, and the phase string is what the board shows, so a rewording of either
# is a change to the contract rather than to the prose.

_ISSUE_ORIGIN_PATTERNS = (
    re.compile(r"no spec: issue-published", re.I),
    re.compile(r"release --state done", re.I),
    re.compile(r"Source: gh#N"),
)

# The spec-only wording a lane carried before the issue-sourced branch was added. It states the
# duty beside the merge in the same pass, so every older check accepts it; the origin check must
# not.
_SPEC_ONLY_DUTY = (
    "3. **Completion truth at your merge.** For every epic whose PR you merged this firing, write "
    "`current_state: done` plus a `completion-truth:` note into its spec frontmatter and its "
    "status board with `status_writer mark-done`, in the same pass as the merge."
)


def _states_issue_origin(chunk: str) -> bool:
    return _states_duty(chunk) and all(p.search(chunk) for p in _ISSUE_ORIGIN_PATTERNS)


def _text_outside_checklist(skill: str) -> str:
    # The checklist box also carries merge and same-pass wording, so leaving it in would let the
    # box alone satisfy the prose assertion and the two halves would stop being independent.
    return _outside_checklist(_skill_text(skill), skill)


def _outside_checklist(text: str, skill: str) -> str:
    return text.replace(_checklist_section_of(text, skill), "", 1)


def test_spec_only_wording_passes_the_older_checks_but_not_the_origin_check() -> None:
    chunk = _flat(_SPEC_ONLY_DUTY)
    assert _states_duty(chunk) and _MERGE_PATTERN.search(chunk) and _SAME_PASS.search(chunk), (
        "the spec-only control no longer satisfies the older checks - it has stopped proving "
        "that they pass vacuously for an issue-sourced row"
    )
    assert not _states_issue_origin(chunk), (
        "the issue-origin check accepts spec-only wording, so it cannot tell a lane that dropped "
        "the issue-sourced branch from one that kept it"
    )


@pytest.mark.parametrize("skill", MERGING_LOOPS)
def test_issue_sourced_completion_truth_is_stated_at_the_merge_and_in_the_checklist(
    skill: str,
) -> None:
    prose = [
        b
        for b in _blocks(_text_outside_checklist(skill))
        if _states_issue_origin(b) and _MERGE_PATTERN.search(b) and _SAME_PASS.search(b)
    ]
    assert prose, (
        f"{skill}/SKILL.md states completion truth at the merge for spec-backed rows only - no "
        "chunk carrying the duty, a merge and same-pass timing also names the issue-sourced "
        "branch (Source: gh#N, release --state done, 'no spec: issue-published' in the phase), "
        "so a merged issue-sourced row has no completion-truth record"
    )
    boxes = [
        b
        for b in _blocks(_checklist_section(skill))
        if b.lstrip().startswith("- [ ]") and _states_issue_origin(b)
    ]
    assert boxes, (
        f"{skill}/SKILL.md has no end-of-firing checklist box for the issue-sourced branch of the "
        "completion-truth duty - prose alone is the fakeable half"
    )


# --- gh#75: lead-dev's completion-truth prose keeps the junior lanes in view ----------------
# The falsified reading is that lead-dev's selection-time cross-check covers everything; it
# covers the epics lead-dev selects, and the junior lanes cover their own merges. What the
# assertion below actually checks is narrower than that reading: it requires every junior lane
# in MERGING_LOOPS to occur within +-700 characters of at least one completion-truth match. It
# re-reds if any lane name is dropped from that neighbourhood, so a junior added to
# MERGING_LOOPS but not to the prose fails here; it CANNOT detect a rewrite that keeps the names
# nearby while going back to implying full coverage.


def test_lead_dev_scopes_its_cross_check_and_names_the_junior_lanes() -> None:
    flat = _flat(_skill_text("lead-dev"))
    windows = [
        flat[max(0, m.start() - 700) : m.end() + 700]
        for pattern in _DUTY_PATTERNS
        for m in pattern.finditer(flat)
    ]
    juniors = [loop for loop in MERGING_LOOPS if loop != "lead-dev"]
    assert juniors, "MERGING_LOOPS names no junior lane - this gate would pass vacuously"
    missing = [j for j in juniors if not any(j in w for w in windows)]
    assert any(all(j in w for j in juniors) for w in windows), (
        "lead-dev/SKILL.md discusses completion truth without naming every junior lane "
        f"alongside it (absent from every window: {missing or 'none, but never all in one'}) - "
        "unscoped, the cross-check reads as full coverage when it only sees epics "
        f"lead-dev itself selects, and a junior-shipped epic left approved means {_CONSEQUENCE}"
    )


# --- gh#75: the gate's own coverage is asserted, not hand-kept ------------------------------
# A gate that keeps passing while its coverage falls is indistinguishable from health. If a loop
# grows this duty and is not listed above, this fails and forces the list to be revisited.


def test_gate_lists_every_skill_that_states_the_reconcile_note() -> None:
    listed = set(MERGING_LOOPS) | set(ACKNOWLEDGED_NON_MERGING)
    marker = re.compile(r"completion[- ]truth", re.I)
    stating = {
        path.parent.name
        for path in only_tracked(SKILLS_ROOT, SKILLS_ROOT.rglob("SKILL.md"))
        if marker.search(path.read_text(encoding="utf-8", errors="replace"))
    }
    unlisted = sorted(stating - listed)
    assert not unlisted, (
        f"skills/{{{','.join(unlisted)}}}/SKILL.md state the completion-truth rule but are in "
        "neither MERGING_LOOPS nor ACKNOWLEDGED_NON_MERGING - decide which, or this gate keeps "
        f"passing while a lane that ships work goes unguarded and {_CONSEQUENCE}"
    )
