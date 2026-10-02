"""One loop asks the human; every other loop writes a Question row — gate it.

`docs/REGISTRY.md` ("Human surfaces — exactly one") says dev-manager is the only
seat that puts a question in front of a person. A fleet loop hands an approved
spec to `/project`, which invokes `solution`, and any `AskUserQuestion` those
skills fire lands on a screen nobody is watching: the run blocks forever with
every test green. `skills/orchestrator/SKILL.md` already carries the contract
that fixes this (an explicit `fleet-context: <agent>` marker, a registry
`## Questions` row with a mandatory `default:`, standalone interactive runs
unchanged). Nothing enforced it — before this module,
`grep -rln AskUserQuestion harness/ skills/_shared/tests/` returned nothing, so
a newly-added unbranched gate, or a deleted branch, cost nothing to ship.

**What this module actually proves, and what it does not.** These skills are
prose programs. No static check can prove prose branches correctly — an author
can write `fleet-context` above a gate and still describe the wrong behaviour.
What IS checkable, and what every assertion here is limited to:

    every occurrence of the literal `AskUserQuestion` in a `skills/*/SKILL.md`
    either sits in an allowlisted human-surface skill, or sits inside a markdown
    section whose body contains the literal marker `fleet-context`.

"Section" is defined mechanically (`_section_of`): from the nearest preceding
markdown heading to the next heading of the same-or-higher level, with headings
inside fenced code blocks ignored (both target files contain `# comment` lines
in bash fences that would otherwise be read as level-1 headings). A site with no
preceding heading at all counts as unbranched — it cannot be shown to be under
anything.

The check is deliberately conservative in one direction: it counts *every*
occurrence of the token, prose mentions included, because "is this line a call
or a description of a call?" is not decidable in prose. An unbranched
description of a gate reads to the next author as a gate, so requiring it to sit
next to the marker is the behaviour we want anyway.
"""

from __future__ import annotations

import re
from pathlib import Path

import sys

import pytest
from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]

# The literal token whose call sites this module gates.
SITE_TOKEN = "AskUserQuestion"

# The machine-checkable marker of a branched gate. Same literal the orchestrator
# and lead-dev already use, so there is one vocabulary, not two.
FLEET_MARKER = "fleet-context"

# Skills allowed to surface a question to a person, each WITH the reason — an
# unexplained allowlist is how an exemption outlives the fact that justified it.
HUMAN_SURFACE_SKILLS: dict[str, str] = {
    "dev-manager": (
        "the human's own seat: the one loop whose entire purpose is asking the "
        "human, per docs/REGISTRY.md 'Human surfaces - exactly one'"
    ),
    "asking-questions": (
        "the protocol document for asking a human at all; its occurrences ARE "
        "the subject matter, and no loop dispatches it as work"
    ),
    "choose-next": (
        "human-seat picker: a person invokes it to choose the next epic, and no "
        "builder loop calls it, so no fleet path can reach its buttons - it "
        "breaks the literal grep without being a fleet leak"
    ),
}

# Skills that MUST be scanned. A rename or a moved gate would otherwise make
# this module silently cover nothing while CI stayed green.
GATED_SKILLS = ("project", "solution", "orchestrator")

# Sections that are unbranched TODAY and are NOT this epic's to fix, recorded
# with their reason so the gate reports new regressions instead of drowning in
# pre-existing ones. This is a ratchet, not an excuse: every entry is asserted
# to still exist, so fixing one fails loudly until the entry is deleted, and
# nothing in the gated pair (project, solution) may be parked here at all.
KNOWN_UNBRANCHED: dict[tuple[str, str], str] = {
    # Step 8 and Step 9.1.5 were recorded here as genuine gaps and are now BRANCHED (issue #12):
    # Step 8 raises a Question row (urgency waiting, default "Pause for spec review"), and
    # Step 9.1.5 is skipped outright on a fleet run. Their entries are deleted rather than
    # reworded, which is what the ratchet above demands -- a stale entry would go on exempting
    # whatever section later occupies that heading.
    #
    # Step 9.2 joined them (gh#48/gh#19). Its exemption read "covered by state rather than by the
    # marker: the merge gate auto-skips when current_state == approved-for-autonomous, so no fleet
    # run reaches these buttons". That stayed true of the BUTTONS and stopped being true of the
    # SECTION: 9.2 now carries the deploy-lock preflight, which a fleet run reaches on every merge
    # and which branches on the marker explicitly. Deleted, not reworded, per the same ratchet --
    # and the deletion is the point, because an exemption that survives its own reason is exactly
    # what goes on exempting the next thing to occupy the heading.
    ("orchestrator", "## Escape hatches for the user"): (
        "not a gate: a user-facing list of the ways a human can answer a question that some "
        "other section raised"
    ),
}


def _skill_text(name: str) -> str:
    path = SKILLS_ROOT / name / "SKILL.md"
    # FAIL, never skip: a renamed skill would silently stop being guarded.
    assert path.is_file(), f"{name}/SKILL.md not found at {path} — renamed, or this gate's list is stale"
    return path.read_text(encoding="utf-8", errors="replace")


_HEADING = re.compile(r"^(#{1,6})\s+\S")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(index, level, text) for every heading OUTSIDE a fenced code block.

    Both gated skills embed bash and markdown fences containing `# comment`
    lines; reading those as level-1 headings would hand a site the wrong
    enclosing section and make the gate's verdict meaningless.
    """
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        opener = _FENCE.match(line)
        if opener:
            token = opener.group(1)
            if fence is None:
                fence = token
            elif line.strip().startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        m = _HEADING.match(line)
        if m:
            found.append((i, len(m.group(1)), line.strip()))
    return found


def _section_of(lines: list[str], headings: list[tuple[int, int, str]], idx: int) -> tuple[str | None, str]:
    """The section containing line *idx*: (heading text, body).

    Body runs from the heading line to the next heading of the same-or-higher
    level. Returns (None, "") when nothing precedes the line — a site with no
    enclosing section cannot be shown to be branched, so it must fail.
    """
    enclosing = None
    for pos, (line_no, level, text) in enumerate(headings):
        if line_no > idx:
            break
        enclosing = (pos, line_no, level, text)
    if enclosing is None:
        return None, ""
    pos, line_no, level, text = enclosing
    end = len(lines)
    for next_line, next_level, _ in headings[pos + 1:]:
        if next_level <= level:
            end = next_line
            break
    return text, "\n".join(lines[line_no:end])


# Each SITE needs its own nearby branch. A section-wide check false-passes the
# regression this file exists to catch: a section holding three gates and one
# branch stays green when you delete the branch of the OTHER two, because the
# surviving marker is still "in the section". Proximity is the cheap proxy for
# "this site's own branch" — generous enough for a branch paragraph written
# above or below the call, tight enough that a deleted branch fails.
PROXIMITY_LINES = 25

# ...but 25 is the DEFAULT, not a universal. A section's right window is a property of how long
# that section is, and `orchestrator`'s Step 3 is 214 lines holding 17 gates -- the furthest sits
# 213 lines from the section's opening branch paragraph. Measured on the tree that introduced this
# override, the whole curve of "window -> new branch paragraphs the skill must grow":
#
#     25 -> 7    40 -> 5    60 -> 3    80 -> 2    213 -> 0
#
# 213 is the trap and the reason this is a dial rather than a bump: a window that spans the whole
# section makes proximity mean exactly what section-scope meant, so the check would LOOK strict
# while re-opening the hole it was added to close. 80 is the knee -- wide enough that only the one
# genuinely over-long section grows prose, tight enough that Step 3 still needs THREE separate
# branch paragraphs and deleting ANY of them reddens (pinned below). Raising this for a skill is a
# visible, reviewable edit here, exactly like moving one into PER_GATE_SKILLS.
PROXIMITY_OVERRIDES: dict[str, int] = {"orchestrator": 80}


def _proximity(skill: str) -> int:
    """Window for *skill* -- the default unless it carries a measured override."""
    return PROXIMITY_OVERRIDES.get(skill, PROXIMITY_LINES)

# Two legitimate ways to carry the contract, and the check differs by mode:
#
#   PER-GATE (strict, site-level) — each gate has its own branch paragraph, so a
#   deleted branch MUST fail. This is how `project` and `solution` are written,
#   and it closes the regression a section-level check misses: a section holding
#   three gates and one surviving marker stays green when you delete the other
#   two branches — precisely the site this file was written to protect.
#
#   GLOBAL (section-level) — the skill states the contract ONCE and says it
#   "applies at every branched site below" (`orchestrator/SKILL.md` does exactly
#   that, and is the canonical description the other two point at). Demanding a
#   marker within PROXIMITY_LINES of each of its 16 Step-3 sites would false-fail
#   a correct design.
#
# Naming the modes is the point: a skill that adopts per-gate branches belongs in
# the strict set, and moving one OUT of it is a visible, reviewable edit here.
#
# `orchestrator` was the GLOBAL example above until issue #28: stating the contract once and
# exempting the whole section means a gate ADDED to that section later inherits the exemption and
# gets no CI signal at all, which is the wedge #12 was filed for. It is per-gate now, carried by a
# wider window plus branch paragraphs restated at the two branch heads that sit furthest from the
# section's opening one -- documentation of an existing contract at the point of use, not new
# behaviour: Step 3's opening paragraph already names both branches.
PER_GATE_SKILLS = frozenset({"project", "solution", "orchestrator"})


def _unbranched_sites(skill: str) -> list[tuple[str, int]]:
    """Sites of *skill* with no marker nearby: (heading, count) per section.

    A site is branched when `fleet-context` appears within this skill's window of it
    AND inside the same section — so the marker must belong to this gate, not to
    a sibling gate that happens to share the heading.
    """
    lines = _skill_text(skill).splitlines()
    headings = _headings(lines)
    offenders: dict[str, int] = {}
    for idx, line in enumerate(lines):
        if SITE_TOKEN not in line:
            continue
        heading, body = _section_of(lines, headings, idx)
        section_start = idx
        while section_start > 0 and lines[section_start] not in (heading,):
            section_start -= 1
            if heading is None:
                section_start = 0
                break
        if skill in PER_GATE_SKILLS:
            window = _proximity(skill)
            lo = max(section_start, idx - window)
            hi = min(len(lines), idx + window + 1)
            scope = "\n".join(lines[lo:hi])
        else:
            scope = body
        if heading is not None and FLEET_MARKER in scope and FLEET_MARKER in body:
            continue
        key = heading or f"<no enclosing heading, line {idx + 1}>"
        offenders[key] = offenders.get(key, 0) + 1
    return sorted(offenders.items())


def _unbranched_sections(skill: str) -> list[tuple[str, int]]:
    """Back-compat alias: the per-SITE check is the real one."""
    return _unbranched_sites(skill)


def _skills_with_sites() -> list[str]:
    """Every skill whose SKILL.md names the token, discovered not hardcoded - FROM THE INDEX.

    Discovery still walks; `only_tracked` is what makes the answer a property of the commit
    rather than of this checkout (gh#175). Note the failure mode moves with it: this runs at
    COLLECTION time (the parametrize below), so a git that cannot answer surfaces as a collection
    error for the whole module rather than as one red test.
    """
    return sorted(
        p.parent.name
        for p in only_tracked(SKILLS_ROOT, SKILLS_ROOT.glob("*/SKILL.md"))
        if SITE_TOKEN in p.read_text(encoding="utf-8", errors="replace")
    )


@pytest.mark.parametrize("skill", _skills_with_sites())
def test_every_ask_user_question_site_is_allowlisted_or_fleet_branched(skill: str) -> None:
    if skill in HUMAN_SURFACE_SKILLS:
        pytest.skip(f"{skill} is an allowlisted human surface: {HUMAN_SURFACE_SKILLS[skill]}")
    offenders = [
        (heading, count)
        for heading, count in _unbranched_sections(skill)
        if (skill, heading) not in KNOWN_UNBRANCHED
    ]
    assert not offenders, (
        f"{skill}/SKILL.md has {sum(c for _, c in offenders)} {SITE_TOKEN} occurrence(s) in "
        f"{len(offenders)} section(s) that never mention `{FLEET_MARKER}`. A fleet loop reaching "
        f"one of these blocks on a button no human sees. Give each section the contract "
        f"(see skills/orchestrator/SKILL.md 'Fleet invocation context'): branch on the marker, "
        f"raise a registry `## Questions` row with a mandatory `default:`, leave standalone "
        f"interactive behaviour unchanged. Sections: {offenders}"
    )


def test_gated_skills_are_actually_scanned() -> None:
    """The discovery glob must reach the skills this epic exists to protect."""
    discovered = set(_skills_with_sites())
    missing = [s for s in GATED_SKILLS if s not in discovered]
    assert not missing, (
        f"{missing} name no {SITE_TOKEN} occurrence — either the gates moved (re-point this "
        "module) or the skill was renamed and is now unguarded"
    )


@pytest.mark.parametrize("skill", sorted(HUMAN_SURFACE_SKILLS))
def test_allowlisted_human_surfaces_exist(skill: str) -> None:
    """An allowlist entry for a skill that no longer exists is a silent hole."""
    path = SKILLS_ROOT / skill / "SKILL.md"
    assert path.is_file(), (
        f"allowlisted human surface `{skill}` has no SKILL.md at {path} — delete the entry or "
        "fix the name; a stale exemption exempts nothing and hides that it stopped applying"
    )


@pytest.mark.parametrize("skill", sorted(HUMAN_SURFACE_SKILLS))
def test_allowlisted_human_surfaces_carry_their_reason(skill: str) -> None:
    """Each exemption states WHY, so it can be re-judged rather than inherited."""
    reason = HUMAN_SURFACE_SKILLS[skill]
    assert len(reason.split()) >= 8, f"{skill}'s exemption reason is too thin to re-judge: {reason!r}"


def test_choose_next_exemption_names_its_actual_reason() -> None:
    """The one exemption a reader will challenge: encode it, do not omit it.

    choose-next breaks the literal grep, and the reason it is still safe is
    specific — a human invokes it, no builder loop does — so the reason has to
    say that rather than being an unexplained name on a list.
    """
    reason = HUMAN_SURFACE_SKILLS["choose-next"].lower()
    assert "builder" in reason and "human" in reason, (
        "choose-next is exempt because it is a human-seat picker with no builder caller; "
        f"the recorded reason must say so: {reason!r}"
    )


def test_the_epics_own_targets_cannot_be_parked_as_known_gaps() -> None:
    """The one way to fake this gate green is to record its targets as gaps.

    `project` and `solution` are precisely the skills this module was written
    red against; an entry for either would convert the fix into a bookkeeping
    edit. Their fix is prose, in their own files, or the gate is theatre.
    """
    parked = sorted(k for k in KNOWN_UNBRANCHED if k[0] in {"project", "solution"})
    assert not parked, (
        f"{parked} parked in KNOWN_UNBRANCHED — those sections must carry the real "
        f"`{FLEET_MARKER}` branch, not an exemption"
    )


@pytest.mark.parametrize(("skill", "reason"), sorted((k[0], v) for k, v in KNOWN_UNBRANCHED.items()))
def test_known_unbranched_entries_carry_their_reason(skill: str, reason: str) -> None:
    """A parked gap without a stated reason becomes permanent by default."""
    assert len(reason.split()) >= 10, f"{skill}'s KNOWN_UNBRANCHED reason is too thin: {reason!r}"


@pytest.mark.parametrize(("skill", "heading"), sorted(KNOWN_UNBRANCHED))
def test_known_unbranched_sections_still_exist(skill: str, heading: str) -> None:
    """A ratchet: fixing a recorded gap must delete its entry, not widen it.

    Left in place, a stale entry would exempt whatever section later occupies
    that heading.
    """
    assert heading in dict(_unbranched_sections(skill)), (
        f"{skill}/SKILL.md section {heading!r} is recorded in KNOWN_UNBRANCHED but is no longer "
        "an unbranched site — delete the entry so the section is gated from now on"
    )


def test_section_parser_finds_the_right_boundaries() -> None:
    """The parser must be able to say NO, and must ignore fenced `#` lines.

    Without this, a bug that returned the whole document as every site's section
    would make every assertion above pass on any input — a gate measuring
    nothing. Case A: the marker one section EARLIER must not count. Case B: a
    `# comment` inside a fence must not be mistaken for a heading (both gated
    skills contain such lines directly above a real site).
    """
    doc = [
        "# Title",
        "## Marked step",
        "handled per fleet-context: <agent>",
        "## Later step",
        "```bash",
        "# Path-shaped?",
        "```",
        "call AskUserQuestion here",
    ]
    headings = _headings(doc)
    assert [h[2] for h in headings] == ["# Title", "## Marked step", "## Later step"], headings
    heading, body = _section_of(doc, headings, doc.index("call AskUserQuestion here"))
    assert heading == "## Later step"
    assert FLEET_MARKER not in body, "the marker leaked in from the preceding section"

    marked_heading, marked_body = _section_of(doc, headings, 2)
    assert marked_heading == "## Marked step"
    assert FLEET_MARKER in marked_body
# ---------------------------------------------------------------------------
# issue #28: orchestrator moved from GLOBAL (section) to PER-GATE scoping.
# ---------------------------------------------------------------------------


STEP_3 = "### Step 3: Early-exit checks"


def _step3_branch_paragraphs() -> list[tuple[int, int]]:
    """(start, end) line spans of Step 3's own `**Fleet-context branch` paragraphs.

    Computed from the file at test time rather than pinned to line numbers, so the
    mutations below survive ordinary edits to `orchestrator/SKILL.md`.
    """
    lines = _skill_text("orchestrator").splitlines()
    headings = _headings(lines)
    spans = []
    for i, line in enumerate(lines):
        if not line.strip().startswith("**Fleet-context branch"):
            continue
        if _section_of(lines, headings, i)[0] != STEP_3:
            continue
        j = i
        while j < len(lines) and lines[j].strip() != "":
            j += 1
        spans.append((i, j))
    return spans


def _offenders_with(text: str, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """`orchestrator`'s unbranched sites, computed against *text* instead of the file."""
    monkeypatch.setattr(
        sys.modules[__name__],
        "_skill_text",
        lambda name: text if name == "orchestrator" else _ORIGINAL_SKILL_TEXT(name),
    )
    return _unbranched_sites("orchestrator")


_ORIGINAL_SKILL_TEXT = _skill_text


def test_orchestrator_is_per_gate_and_the_window_is_a_measured_override() -> None:
    """Both halves of the #28 fix, pinned together because neither works alone.

    Membership without the override fails on 23 sites; the override without
    membership does nothing at all, since only PER_GATE_SKILLS reads a window.
    """
    assert "orchestrator" in PER_GATE_SKILLS
    assert PROXIMITY_OVERRIDES["orchestrator"] == 80
    assert _proximity("orchestrator") == 80


def test_the_override_does_not_loosen_the_other_per_gate_skills() -> None:
    """A dict makes it easy to widen everyone at once. That would silently weaken
    the two skills this file was originally written to protect."""
    for skill in ("project", "solution"):
        assert skill not in PROXIMITY_OVERRIDES
        assert _proximity(skill) == PROXIMITY_LINES == 25


def test_step_3_carries_more_than_one_branch_paragraph() -> None:
    """The premise of the window: one statement cannot serve a 200-line section."""
    assert len(_step3_branch_paragraphs()) >= 3


@pytest.mark.parametrize("nth", range(len(_step3_branch_paragraphs())))
def test_deleting_any_step_3_branch_paragraph_reddens_the_gate(
    nth: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE POINT OF PER-GATE MODE, and the reason the window is 80 rather than 213.

    Section scope goes green when you delete one of three branches because a
    sibling marker survives "in the section". At 80 each Step 3 branch paragraph
    is load-bearing on its own: remove any one and sites go unbranched.
    """
    lines = _skill_text("orchestrator").splitlines()
    spans = _step3_branch_paragraphs()
    start, end = spans[nth]
    mutated = "\n".join(lines[:start] + lines[end:])

    offenders = _offenders_with(mutated, monkeypatch)
    assert any(heading == STEP_3 for heading, _ in offenders), (
        f"deleting Step 3 branch paragraph #{nth} (lines {start + 1}-{end}) left the gate GREEN. "
        "Proximity has stopped distinguishing this site's own branch from any branch in the "
        "section, which is the hole issue #28 was filed to close."
    )


def test_a_window_spanning_the_section_is_section_scope_in_disguise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why 80 and not "whatever makes it pass with no prose changes".

    213 lines separate Step 3's furthest gate from its opening branch paragraph, so
    a 213-line window needs zero new paragraphs -- and is worthless: it re-derives
    section scope while LOOKING strict. Pinned by showing the deletion mutation
    above stops biting at that width.
    """
    lines = _skill_text("orchestrator").splitlines()
    start, end = _step3_branch_paragraphs()[-1]
    mutated = "\n".join(lines[:start] + lines[end:])

    monkeypatch.setitem(PROXIMITY_OVERRIDES, "orchestrator", 250)
    offenders = _offenders_with(mutated, monkeypatch)
    assert not any(heading == STEP_3 for heading, _ in offenders), (
        "expected a section-spanning window to hide the deleted branch -- if this now fails, "
        "the 80 chosen for orchestrator is no longer the meaningful end of the curve and the "
        "comment above PROXIMITY_OVERRIDES needs re-measuring, not this assertion relaxing"
    )
