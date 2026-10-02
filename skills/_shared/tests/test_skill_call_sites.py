"""The skills are prose programs, so their call sites are code — gate them.

Audit rule 1 has two code layers (the survey glob scope and `dispatchable`'s
project-mismatch drop) and one prose layer: the BUILDER has to actually pass
`project=<name>`. Both code layers default to today's project-wide behavior, so
a builder that silently drops the argument is back to the pre-epic hazard — one
project's fleet claiming another project's specs off a shared data_root — with
every test still green.

Nothing else in the repo reads a SKILL.md looking for this: `harness/check.py`
greps the skills root only for server-IP literals, and the install tests assert
file copies. This module is that missing gate.
"""

from __future__ import annotations

import argparse
import inspect
import os
import posixpath
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[2]

# Prefer the tree under test over whatever `pip install -e` happens to have bound: this module
# reads SKILL.md files out of SKILLS_ROOT and derives its loop roster from the product constant,
# and the two disagreeing silently is the exact failure `test_the_loop_roster_matches_the_tree`
# below exists to catch. In CI the checkout IS the editable install, so this is a no-op there.
sys.path.insert(0, str(SKILLS_ROOT / "_shared"))

import agentflow.registry as _registry_module  # noqa: E402
from agentflow.registry import AGENTS  # noqa: E402

# Loops that select work to BUILD: their survey must be scoped to one project.
BUILDER_SKILLS = ("lead-dev", "jr-dev-1", "jr-dev-2", "auditor", "steward")

# Loops that deliberately survey every project: refinement and selection need the
# full set. Each must SAY so, so a reader cannot mistake it for an oversight.
PROJECT_WIDE_SKILLS = ("dev-manager", "choose-next", "backlog")

# Skills whose survey feeds DEPENDENCY reasoning - `ready_set`/`blocked_status`, or prose that
# works "honoring depends_on". Every one of them must pass `include_archived=True`, because
# `survey_epics` deletes archived rows before dep math runs while `_done_slugs` counts
# `archived` as satisfied. Membership is about what the skill DOES with the records, not about
# whether it is a builder: `lead-dev` is scoped to one project and `choose-next`/`dev-manager`
# are project-wide, yet all three resolve dependencies and all three strand identically without
# the flag.
#
# `orchestrator` is deliberately ABSENT. It calls `compute_ready_set` from `plan_parser` over a
# single plan's task DAG - a different function on a different corpus that never touches
# `survey_epics`. Adding it would make this gate assert something that skill has no reason to do.
DEP_REASONING_SURVEY_SKILLS = ("lead-dev", "choose-next", "dev-manager")

_CALL = re.compile(r"survey_epics\s*\(")
# Both dispatch-selection entry points. `selectable()` REPLACED the hand-composed
# `by_priority(ready_set(dispatchable(...)))` (issue #40); matching only the OLD name
# emptied this guard the instant the skills were renamed, and nothing failed, because
# the assertion below tolerates an empty call list by design. Keep BOTH names: a skill
# may still legitimately name `dispatchable` directly.
_DISPATCH = re.compile(r"(?:selectable|dispatchable)\s*\(")
# project= with a real value: not None, not empty, not a bare placeholder.
_PROJECT_VALUE = re.compile(r"project\s*=\s*([^\s,)]+)")
# Values that contain "project=" but mean UNSCOPED - the substring check this
# gate originally used went green on every one of them.
_UNSCOPED_VALUES = frozenset({"None", "none", "''", '""', "-"})


def _is_scoped(call: str) -> bool:
    """True when the call passes a project= argument with a real value."""
    return any(v not in _UNSCOPED_VALUES for v in _PROJECT_VALUE.findall(call))


def _skill_text(name: str) -> str:
    path = SKILLS_ROOT / name / "SKILL.md"
    # FAIL, never skip: a renamed or moved builder would silently stop being
    # guarded while CI stayed green - the gate would quietly cover nothing.
    assert path.is_file(), f"{name}/SKILL.md not found at {path} - renamed, or the gate's list is stale"
    return path.read_text(encoding="utf-8", errors="replace")


def _calls(text: str, pattern) -> list[str]:
    """Every `pattern(...)` call in *text*, paren-balanced across newlines."""
    calls: list[str] = []
    for m in pattern.finditer(text):
        depth = 0
        for i in range(m.end() - 1, len(text)):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    calls.append(text[m.start():i + 1])
                    break
        else:  # pragma: no cover — an unbalanced snippet is itself a doc bug
            calls.append(text[m.start():m.start() + 200])
    return calls


# The one way a builder skill may write an unscoped survey: the marker INSIDE the call's parens,
# so the exemption travels with the call and a reader sees the reason at the call site. It is for
# a survey that reads slugs only and selects no work - auditor Step 4.3's gap-slug uniqueness
# check, which has to see every project because dev-manager's candidate survey merges same-slug
# records across projects before it filters. Allowed skills are listed, so a new use is a diff
# here and not a comment slipped into a selection step.
_DELIBERATE_UNSCOPED = "# project-wide on purpose:"
_DELIBERATE_UNSCOPED_ALLOWED = {"auditor": 1}


@pytest.mark.parametrize("skill", BUILDER_SKILLS)
def test_builder_survey_call_sites_are_project_scoped(skill: str) -> None:
    calls = _calls(_skill_text(skill), _CALL)
    assert calls, f"{skill}/SKILL.md names no survey_epics call — did the selection step move?"
    # A VALUE check, not a substring check: `project=None` contains "project="
    # and is exactly the unscoped call this gate exists to catch.
    exempt = [c for c in calls if not _is_scoped(c) and _DELIBERATE_UNSCOPED in c]
    assert len(exempt) == _DELIBERATE_UNSCOPED_ALLOWED.get(skill, 0), (
        f"{skill}/SKILL.md has {len(exempt)} survey_epics call(s) marked {_DELIBERATE_UNSCOPED!r}; "
        f"allowed: {_DELIBERATE_UNSCOPED_ALLOWED.get(skill, 0)}. {exempt}"
    )
    unscoped = [c for c in calls if not _is_scoped(c) and c not in exempt]
    assert not unscoped, (
        f"{skill}/SKILL.md has {len(unscoped)} unscoped survey_epics call(s); a builder must pass "
        f"project=<name> or it surveys every project on the data_root: {unscoped}"
    )


@pytest.mark.parametrize("skill", PROJECT_WIDE_SKILLS)
def test_project_wide_skills_say_they_are_project_wide(skill: str) -> None:
    text = _skill_text(skill)
    if not _calls(text, _CALL):
        pytest.skip(f"{skill}/SKILL.md names no survey_epics call")
    assert "project-wide" in text.lower(), (
        f"{skill}/SKILL.md surveys every project but never says so — an unexplained unscoped "
        "survey reads as the bug rule 1 exists to prevent"
    )


def test_the_builder_and_project_wide_sets_are_disjoint() -> None:
    """A skill cannot be both; the lists above are the contract this file gates."""
    assert not set(BUILDER_SKILLS) & set(PROJECT_WIDE_SKILLS)


@pytest.mark.parametrize("skill", BUILDER_SKILLS)
def test_builder_dispatchable_call_sites_are_project_scoped(skill: str) -> None:
    """Layer 2 needs the same gate as layer 1.

    The glob scope narrows what is READ; `dispatchable(..., project=)` drops a
    foreign record that got read anyway. A builder that scopes one and not the
    other is still exposed to the hazard, and the docstring above promises this
    file guards it.
    """
    text = _skill_text(skill)
    # Not every builder spells out a dispatchable() call (several delegate the
    # whole selection to lead-dev's prose), and a bare `dispatchable()` with no
    # arguments is a reference, not a call site. Gate the ones that pass args.
    # Strip the matched function name GENERICALLY. Slicing a hard-coded length
    # mis-parses the moment the name changes, which is half of how this guard
    # went vacuous when `dispatchable` became `selectable`.
    calls = [c for c in _calls(text, _DISPATCH)
             if "(" in c and c.split("(", 1)[1].strip(") \n\t")]
    unscoped = [c for c in calls if not _is_scoped(c)]
    assert not unscoped, (
        f"{skill}/SKILL.md has {len(unscoped)} unscoped dispatchable call(s); pass project=<name> "
        f"so a foreign record that was read is still dropped: {unscoped}"
    )


# ---------------------------------------------------------------------------
# Epic E-0160 (loop-hygiene-from-first-audit). The prose below is the fix; the
# tests here are the only thing that keeps it fixed. Each block names the audit
# item it gates and the exact mutation it re-reds on.
# ---------------------------------------------------------------------------

REPO_ROOT = SKILLS_ROOT.parent

# Loops that consume routed `## Queue` Notes. steward joined them in E-0250:
# retention is keyed on the STRUCK state, so a lane that only appends is an
# inbox `cull` can never reach. auditor joined in p145: its fixed-issue pass
# (Step 4b) takes the issues a Note names as fixed as candidates, so it must
# strike each one it acted on or re-judge it every wake.
NOTE_CONSUMERS = ("lead-dev", "jr-dev-1", "jr-dev-2", "dev-manager", "steward", "auditor")

# The two jr lanes, each mapped to the lane its own session may have abandoned.
JR_LANES = {"jr-dev-1": "jr-dev-2", "jr-dev-2": "jr-dev-1"}

# The three loops that carry a per-firing state file with an `## ON WAKE` block.
STATE_FILE_SKILLS = ("lead-dev", "jr-dev-1", "jr-dev-2")


def _doc_text(rel: str) -> str:
    path = REPO_ROOT.joinpath(*rel.split("/"))
    # FAIL, never skip — same reasoning as _skill_text: a moved canonical must
    # break the gate loudly, not quietly stop being checked.
    assert path.is_file(), f"{rel} not found at {path} — moved, or this gate's path is stale"
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Item 1 — the phase-boundary heartbeat CONSUMES before it refreshes.
#
# Presence alone proves nothing: four of these five files already contained
# `answered` and `apply` before the fix. The defect was pure ORDERING, so the
# gate is an offset comparison inside the boundary block. Swapping step 1 and
# step 2 back re-reds it.
# ---------------------------------------------------------------------------

# Every copy opens its ordered pair with this phrase ("...at every boundary",
# "...at every domain boundary") and closes it with the rationale sentence.
# Bounding the search to that span is what stops an unrelated `status` call
# elsewhere in a 700-line file from satisfying the ordering by accident.
_BOUNDARY_OPEN = re.compile(r"[Ii]n this order, at every ")
_BOUNDARY_CLOSE = "Consuming AFTER the status write"

# The pre-fix shorthand. It omits the CONSUME step BY NUMBER, which is how the
# ordering got lost in the first place; the fixed text reads `steps 1+2+4`.
_OMITS_CONSUME_BY_NUMBER = "steps 1+4"


def _phase_boundary_block(label: str, text: str) -> str:
    """The ordered CONSUME/refresh pair, without its trailing rationale."""
    opened = _BOUNDARY_OPEN.search(text)
    assert opened, (
        f"{label} has no phase-boundary block — the ordered pair opens with "
        '"in this order, at every ..."; a boundary step demoted back to prose cannot be gated'
    )
    closed = text.find(_BOUNDARY_CLOSE, opened.end())
    assert closed != -1, (
        f'{label} opens a phase-boundary block but never says "{_BOUNDARY_CLOSE}" — '
        "the rationale that tells a reader WHY the order matters is the block's terminator"
    )
    return text[opened.end():closed]


def _assert_consume_precedes_status(label: str, block: str, actor: str) -> None:
    read = block.find(f"--answered-for {actor}")
    apply_ = block.find(f"apply --actor {actor}")
    status = block.find(f"status --actor {actor}")
    assert read != -1, f"{label}: boundary block never re-reads `--answered-for {actor}`"
    assert apply_ != -1, f"{label}: boundary block never flips the row with `apply --actor {actor}`"
    assert status != -1, f"{label}: boundary block never refreshes with `status --actor {actor}`"
    assert read < status and apply_ < status, (
        f"{label}: the `status --actor {actor}` refresh is written BEFORE the consume step "
        f"(read@{read}, apply@{apply_}, status@{status}). Consuming after the status write "
        "stamps a heartbeat asserting a phase the answer may have just invalidated"
    )


@pytest.mark.parametrize("skill", BUILDER_SKILLS)
def test_phase_boundary_consumes_before_status(skill: str) -> None:
    text = _skill_text(skill)
    _assert_consume_precedes_status(
        f"{skill}/SKILL.md", _phase_boundary_block(f"{skill}/SKILL.md", text), skill
    )
    assert _OMITS_CONSUME_BY_NUMBER not in text, (
        f'{skill}/SKILL.md still says "{_OMITS_CONSUME_BY_NUMBER}" — that shorthand omits the '
        "CONSUME step by number, which is the defect; the boundary is steps 1+2+4"
    )


def test_registry_canonical_orders_consume_before_status() -> None:
    """docs/REGISTRY.md is the binding source all five skills mirror.

    Fixing the five copies and leaving the canonical saying `steps 1+4` is the
    documented-hazard-fixed-for-one-copy-only failure: the source of truth would
    keep contradicting every file that cites it as binding.
    """
    text = _doc_text("docs/REGISTRY.md")
    assert _OMITS_CONSUME_BY_NUMBER not in text, (
        f'docs/REGISTRY.md still says "{_OMITS_CONSUME_BY_NUMBER}"; the canonical phase-boundary '
        "contract must name the CONSUME step, not skip from 1 to 4"
    )
    _assert_consume_precedes_status(
        "docs/REGISTRY.md", _phase_boundary_block("docs/REGISTRY.md", text), "<you>"
    )


# ---------------------------------------------------------------------------
# Item 4 — a Note acted on and left live is re-read as new next wake.
# ---------------------------------------------------------------------------

_STRIKE_CMD = "note --strike"
_STRIKE_WORD = re.compile(r"str(?:ike|uck)")

# Where each consumer's own end-of-pass checklist starts. The juniors' checklists
# are DELTAS that defer the shared boxes to the primary's, so their governing
# checklist is their own section plus lead-dev's.
_CHECKLIST_HEADING = {
    "lead-dev": "## 9. End-of-firing checklist",
    "jr-dev-1": "## End-of-firing checklist",
    "jr-dev-2": "## End-of-firing checklist",
    "dev-manager": "End-of-wake checklist",
    "steward": "## End-of-wake checklist",
    "auditor": "## End-of-pass checklist",
}
_DEFERS_TO_PRIMARY_CHECKLIST = "primary §9 still applies"


def _checklist_section(skill: str) -> str:
    text = _skill_text(skill)
    head = _CHECKLIST_HEADING[skill]
    start = text.find(head)
    assert start != -1, (
        f'{skill}/SKILL.md has no "{head}" section — this gate anchors on it; renaming the '
        "checklist silently un-gates every box in it"
    )
    end = text.find("\n## ", start + len(head))
    return text[start:] if end == -1 else text[start:end]


@pytest.mark.parametrize("skill", NOTE_CONSUMERS)
def test_note_consumers_name_the_strike_command(skill: str) -> None:
    text = _skill_text(skill)
    assert _STRIKE_CMD in text, (
        f"{skill}/SKILL.md consumes routed Notes but never names `{_STRIKE_CMD}`; a Note acted "
        "on and left live is re-read as new information next wake, and striking one 'under the "
        "edit-lock' is the hand-edit docs/REGISTRY.md forbids outright"
    )


@pytest.mark.parametrize("skill", NOTE_CONSUMERS)
def test_strike_is_a_checklist_step_not_only_prose(skill: str) -> None:
    """Naming the command mid-prose is the half that is easy to fake.

    The obligation only survives a firing if the end-of-pass checklist asserts
    it, so gate the checklist section specifically — either the loop's own, or
    the primary's when the loop's checklist explicitly defers to it.
    """
    section = _checklist_section(skill)
    governing = section
    if _DEFERS_TO_PRIMARY_CHECKLIST in section:
        governing = section + "\n" + _checklist_section("lead-dev")
    assert _STRIKE_WORD.search(governing), (
        f"{skill}/SKILL.md names `{_STRIKE_CMD}` in prose but its governing end-of-pass "
        "checklist never asserts the strike happened. A pass that read a Note, acted on it and "
        "left it live ticks every box"
    )


# ---------------------------------------------------------------------------
# Epic E-0250 (registry-note-retention). Note retention is keyed on the STRUCK
# state, never on age or a row count, so an inbox no one may strike is an inbox
# `cull` can never reach — its Notes stay live on the fleet's most-read surface
# forever. Two things have to hold, and prose alone held neither: steward's
# ENUMERATED write set names the strike verb, and the binding schema states the
# retention rule, the cap, and where the analysis goes instead.
# ---------------------------------------------------------------------------

# The bullet list a steward reads as the complete set of writes it may make.
_STEWARD_WRITE_SET_OPEN = "write through the mediated CLI only"
# The heading that IMMEDIATELY follows the bullet list. Not "### Phase-boundary touch", which
# is one section further on: that span also swallowed "### Consume routed Notes" and its own
# `note --strike` line, so deleting the verb from the enumeration itself left the gate green —
# the exact hole this gate exists to close.
_STEWARD_WRITE_SET_CLOSE = "### Consume routed Notes"


def test_steward_enumerated_write_set_names_strike() -> None:
    """The enumeration specifically — not merely somewhere in the file.

    `test_note_consumers_name_the_strike_command` is satisfied by any mention
    anywhere in 450 lines. The copy that governs behaviour is the closed list
    of verbs, because a verb absent from a list that reads as exhaustive is a
    verb the loop treats as forbidden.
    """
    text = _skill_text("steward")
    start = text.find(_STEWARD_WRITE_SET_OPEN)
    assert start != -1, (
        f'steward/SKILL.md no longer says "{_STEWARD_WRITE_SET_OPEN}" — this gate anchors on '
        "the mediated-CLI write set; rewording it silently un-gates the enumeration"
    )
    end = text.find(_STEWARD_WRITE_SET_CLOSE, start)
    assert end != -1, (
        f'steward/SKILL.md opens its write set but has no "{_STEWARD_WRITE_SET_CLOSE}" section '
        "after it — that heading is the list's terminator"
    )
    assert _STRIKE_CMD in text[start:end], (
        f"steward/SKILL.md enumerates its registry write set without `{_STRIKE_CMD}`; Notes "
        "routed to steward then never reach the struck state, and a state-keyed cull can never "
        "collect them"
    )


def _flat(text: str) -> str:
    """Collapse whitespace so a claim that wraps across lines still matches."""
    return re.sub(r"\s+", " ", text)


# Each claim is (what it protects, the exact wording that carries it). Wording,
# not a paraphrase: a doc gate that accepts synonyms drifts into accepting
# nothing.
_RETENTION_CLAIMS = (
    ("struck Notes are culled to the log", "struck Note is culled"),
    ("an unstruck Note is retained forever", "unstruck Note is never culled"),
    ("the retention key is state, not age or count", "keyed on STATE"),
    # NOT "issue #43": that issue is "A reap returns the claim but strands every note routed to
    # the reaped loop, silently" — a real, still-open leak, but not the thing that decided
    # state-over-age, and citing it here made the schema claim a provenance it does not have.
    ("why an age rule is wrong", "drops coordination nobody consumed"),
    ("the per-note cap, in bytes", "300 UTF-8 bytes"),
    ("the cap is refused in code, by a named rule", "note_too_long"),
    ("splitting to evade the cap is refused too", "note_split_burst"),
    ("the analysis goes in a file beside the registry", "`notes/` beside `registry.md`"),
    ("the file name shape", "<YYYY-MM-DDTHHMMZ>-<actor>-to-<recipient>-<slug>.md"),
    ("the Note carries a short relative pointer", "notes/<file>.md"),
)


def test_registry_schema_states_the_note_retention_rule() -> None:
    """docs/REGISTRY.md is binding; the CLI's refusals cite it by section.

    The guards landed in code first. Leaving the schema silent would make the
    rule discoverable only by tripping it, and `note_too_long` cites
    "REGISTRY.md #Queue" for an explanation that would not be there.
    """
    flat = _flat(_doc_text("docs/REGISTRY.md"))
    missing = [f"{why} ({claim!r})" for why, claim in _RETENTION_CLAIMS if claim not in flat]
    assert not missing, (
        "docs/REGISTRY.md does not carry the Note retention rule the CLI enforces and cites: "
        + " | ".join(missing)
    )


def test_registry_schema_grants_steward_the_strike_verb() -> None:
    """The ownership summary is where a reader checks who may write what.

    Widening the steward skill while the ownership row still reads as
    append-only leaves the two contradicting each other, and the row is the one
    a reviewer trusts.
    """
    flat = _flat(_doc_text("docs/REGISTRY.md"))
    assert re.search(r"steward writes NOTHING[^|]*--strike", flat), (
        "docs/REGISTRY.md's ownership row for steward does not name `--strike`; steward writes "
        "Notes and is written to, so its inbox has to be drainable or the retention rule has a "
        "hole exactly one lane wide"
    )


# ---------------------------------------------------------------------------
# Item 5 — the note CLI prepends `<actor> -> <target>: ` itself.
#
# This gate is line-level and SKILL.md-scoped ON PURPOSE. Six legitimate
# READ-side references document the rendered line an agent CONSUMES (jr-dev-1,
# jr-dev-2, lead-dev, docs/REGISTRY.md) and three registry.py internals call
# `_append_note` directly and so emit the prefix exactly once. A blanket grep
# for "-> lead-dev:" reds all nine. Only a PRESCRIBED `--text` value that opens
# with a routing prefix is a defect.
# ---------------------------------------------------------------------------

_TEXT_REPEATS_PREFIX = re.compile(r'--text\s+"\s*[a-z][a-z0-9]*(?:-[a-z0-9]+)*\s*->')
_STARVATION_ANCHOR = "looks starved"


def _skill_md_files() -> list[Path]:
    return sorted(SKILLS_ROOT.glob("*/SKILL.md"))


def test_no_prescribed_note_text_repeats_the_routing_prefix() -> None:
    offenders: list[str] = []
    for path in _skill_md_files():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            # Join one line forward: these files wrap at ~95 cols, so a real
            # `--text "jr-dev-2 -> ..."` can straddle the break. Require the
            # match to START on this line so the report names the right one.
            joined = line if i + 1 == len(lines) else f"{line} {lines[i + 1]}"
            match = _TEXT_REPEATS_PREFIX.search(joined)
            if match and match.start() < len(line):
                offenders.append(f"{path.relative_to(SKILLS_ROOT)}:{i + 1}: {line.strip()}")
    assert not offenders, (
        "prescribed `--text` value opens with a routing prefix; `_cmd_note` already prepends "
        "`<actor> -> <target>: `, so following this literally renders the prefix twice: "
        + " | ".join(offenders)
    )


@pytest.mark.parametrize("skill", ("jr-dev-1", "jr-dev-2"))
def test_starvation_note_prescribes_the_routing_flag(skill: str) -> None:
    """The doubled prefix is cosmetic; the missing `--to` is not.

    Without `--to`, `_cmd_note` routes to `fleet`, and lead-dev's consume step
    reads only Notes routed TO it — the starvation signal is silently dropped.
    """
    text = _skill_text(skill)
    at = text.find(_STARVATION_ANCHOR)
    assert at != -1, (
        f'{skill}/SKILL.md no longer says "{_STARVATION_ANCHOR}" — did the starvation step move?'
    )
    window = " ".join(text[at:at + 400].split())
    for needle in (f"note --actor {skill}", "--to lead-dev", '--text "'):
        assert needle in window, (
            f"{skill}/SKILL.md's starvation Note does not prescribe `{needle}`. Written as a bare "
            f"`{skill} -> lead-dev: ...` example it routes fleet-wide and lead-dev never "
            f"consumes it: {window}"
        )


# ---------------------------------------------------------------------------
# Item 7 — re-pointing an abandoned jr lane.
#
# `status` has NO target flag: its subparser takes --actor/--state/--work/
# --phase/--eta/--waiting-on, and `_stamp_agent_row` keys the row off --actor.
# The only implementable form declares the ABANDONED lane as the actor, which
# is legal precisely because the same session owns both lanes.
# ---------------------------------------------------------------------------

_UNIMPLEMENTABLE_TARGET_FLAG = "--agent jr-dev-"


@pytest.mark.parametrize("skill", tuple(JR_LANES))
def test_jr_lane_repoint_uses_cross_actor_status_call(skill: str) -> None:
    other = JR_LANES[skill]
    text = _skill_text(skill)
    call = re.compile(
        rf"status --actor {re.escape(other)} [^\n]*--state idle [^\n]*"
        rf'--phase "re-pointed to {re.escape(skill)}"'
    )
    assert call.search(text), (
        f"{skill}/SKILL.md has no re-point call site idling `{other}` with the literal phase "
        f'"re-pointed to {skill}". Keep that phrase verbatim: `status` stamps a fresh heartbeat '
        "unconditionally, so the phase text is the ONLY honest marker that the idled lane is not "
        "freshly alive"
    )
    # The load-bearing half. `--agent` is `reap`'s flag and dev-manager's alone;
    # a future editor "simplifying" the call to --actor <self> --agent <other>
    # would be rejected by argparse at runtime with every other test still green.
    assert _UNIMPLEMENTABLE_TARGET_FLAG not in text, (
        f"{skill}/SKILL.md prescribes `{_UNIMPLEMENTABLE_TARGET_FLAG}...`; `status` has no target "
        "flag, so that call idles YOUR OWN row and argparse rejects the flag outright"
    )


# ---------------------------------------------------------------------------
# Item 2 — the state file is REWRITTEN from truth each wake, never appended to.
# ---------------------------------------------------------------------------

_STATE_SECTION_HEADING = "## State + coordination surfaces"
# "rewritten from truth" wraps across a line break in both juniors.
_REWRITE_MANDATE = re.compile(r"rewritten\s+from\s+truth", re.IGNORECASE)


@pytest.mark.parametrize("skill", STATE_FILE_SKILLS)
def test_state_file_section_mandates_rewrite_not_append(skill: str) -> None:
    text = _skill_text(skill)
    start = text.find(_STATE_SECTION_HEADING)
    assert start != -1, f'{skill}/SKILL.md has no "{_STATE_SECTION_HEADING}" section'
    end = text.find("\n## ", start + len(_STATE_SECTION_HEADING))
    section = text[start:] if end == -1 else text[start:end]
    assert "## ON WAKE" in section, (
        f"{skill}/SKILL.md's state-file section names no `## ON WAKE` block — without the shape "
        "there is nothing for the rewrite mandate to be about"
    )
    assert _REWRITE_MANDATE.search(section), (
        f"{skill}/SKILL.md's state-file section never says the file is rewritten FROM TRUTH; "
        "'log' and 'update' are append-flavoured verbs, and they are what let the section grow "
        "a second ON WAKE block every firing"
    )
    assert "never appended" in section, (
        f"{skill}/SKILL.md's state-file section states the rewrite but not its negative half — "
        "'never appended to' is the clause a fast reader actually obeys"
    )


# --------------------------------------------------------------------------- #
# E-0300: `--project <name>` on the slash command
#
# Three grounded traps this file exists to keep closed:
#   1. A wrapper-only change is a NO-OP that looks like a fix - four wrappers
#      already forwarded $ARGUMENTS, so nothing proves the name is USED.
#   2. The flag EVAPORATES ON WAKE 1 if a continuation prompt drops it: the
#      launch is on the right project and every self-wake after is on the
#      default. Silent, and worse than the pre-epic settings file.
#   3. $ARGUMENTS is OVERLOADED - lead-dev/auditor/steward already read it as a
#      work target, so an un-stripped `--project X` becomes a phantom target.
# --------------------------------------------------------------------------- #
#: DERIVED, never restated. This was a hardcoded six-tuple written before jr-dev-3 landed
#: (61df7ad, 2026-09-04), and it stayed green through that lane's whole arrival: eight
#: parametrised gates below take their roster from it, so every one of them silently stopped
#: covering a live builder. A gate that cannot see the lane it protects is worse than no gate,
#: because it reports the coverage it no longer has. `agentflow.registry.AGENTS` is the enum the
#: product itself refuses writes against, so an eighth lane cannot reach the fleet without
#: reaching these tests in the same commit.
ALL_LOOPS = AGENTS
#: The loops that SELF-WAKE. dev-manager left this set in E-0220: it schedules no
#: continuation and exists only while a human has the seat open, so a gate asserting it
#: has a continuation would be asserting the very thing that epic removed.
SELF_WAKING_LOOPS = tuple(s for s in ALL_LOOPS if s != "dev-manager")
COMMANDS_DIR = SKILLS_ROOT.parent / "commands"

# `--actor <loop>` with no `--project` in the same wrapped span is an unscoped
# registry write: on a shared data_root it edits the WRONG project's registry.
_ACTOR = re.compile(r"--actor\s+([a-z0-9-]+)")
def _registry_verbs() -> frozenset[str]:
    """The registry CLI's subcommands, read off the BUILT parser rather than restated.

    gh#374: this was a hand-written 14-verb alternation while the CLI registered 25, and two of
    the missing verbs (`assign-blocker`, `directive`) already had bare call sites in a SKILL.md
    that neither arm of the gate could see. Same move as `ALL_LOOPS = AGENTS`: borrow the product's
    own list, so a new subcommand reaches this gate in the same commit that adds it. Read from the
    built parser, not a grep for `add_parser`, because not every registration is spelled the same.
    `registry._build_parser` is looked up at call time so a test can substitute the builder.
    """
    parser = _registry_module._build_parser()
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(actions) == 1, f"expected one subparsers action on the registry CLI, found {len(actions)}"
    return frozenset(actions[0].choices)


def _verb_pattern(verbs) -> re.Pattern[str]:
    # LITERAL BACKSPACE BYTES (0x08) ONCE SAT WHERE THE WORD BOUNDARIES BELONG, so the old
    # hand-written pattern matched nothing: the verb half of this gate never fired and the
    # call-site test ran on the "agentflow.registry" substring alone (repaired in aa0d5ce, #295;
    # pinned by gh#314). Keep the boundaries as raw-string escapes: they, not alternative order,
    # make a hyphenated verb (`raise-blocker`) match only as a whole token, and the gate asks only
    # whether SOME verb is present. Sorted purely so the compiled pattern is deterministic.
    alternation = "|".join(re.escape(v) for v in sorted(verbs))
    return re.compile(r"\b(" + alternation + r")\b")


# The registry subcommands. Their presence is what distinguishes an invocation
# from prose that merely mentions `--actor`. DERIVED -- a verb missing from this set is a verb
# whose call sites nothing checks, which is why it is not written out by hand any more.
_REGISTRY_VERBS = _registry_verbs()
_REGISTRY_VERB = _verb_pattern(_REGISTRY_VERBS)


def _spans(text: str, start: int, width: int = 240) -> str:
    return text[start:start + width]


def unscoped_registry_call_sites(text: str) -> list[str]:
    """Backtick spans that invoke a registry verb with `--actor` and no `--project`.

    EXTRACTED so the gate below and its own regression pin run the SAME predicate. A pin that
    restated this logic would be a second definition of the gate, free to stay green while the
    real one rotted -- the failure this module warns about elsewhere and the reason gh#314 could
    happen at all.

    A CALL SITE is a backtick span that names a registry verb AND an --actor. Two false readings
    this must avoid, both hit in review:
      - a fixed forward window let the NEXT command's --project satisfy a call that had none
        (mutation-proven on steward's `apply`);
      - a bare prose reference like "only `status`/`apply` as `--actor steward`" is not a call
        and must not be required to carry --project.
    """
    unscoped = []
    for span in re.findall(r"`([^`]*)`", text):
        if "--actor" not in span:
            continue
        if not (_REGISTRY_VERB.search(span) or "agentflow.registry" in span):
            continue
        if "--project" not in span:
            unscoped.append(span.strip())
    return unscoped


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_registry_call_sites_carry_project(skill: str) -> None:
    unscoped = unscoped_registry_call_sites(_skill_text(skill))
    assert not unscoped, (
        f"{skill}/SKILL.md has `--actor` call sites with no `--project` in the same span: "
        f"{unscoped[:3]} - on a shared data_root that writes another project's registry"
    )


def test_the_verb_half_of_the_gate_actually_fires() -> None:
    """gh#314. The gate ran for its whole life on half of what it names, and nothing said so.

    THE PREDICATE IS A DISJUNCTION -- a verb match OR the literal "agentflow.registry" substring
    -- so a dead `_REGISTRY_VERB` does not disable the gate, it DEGRADES it to the substring
    alone. Every span that invokes a verb WITHOUT that prefix becomes invisible, and the suite
    stays green because the surviving half still catches the prefixed ones.

    So the case below carries a verb and an --actor and NO `agentflow.registry`: it is exactly
    the span the dead pattern passes and the repaired one catches. Measured before writing this:
    re-introducing literal 0x08 at both ends of the pattern left all 258 tests green.

    Asserting that the pattern "matches something" would not do -- the prefixed half matches with
    the pattern dead, so a positive control has to avoid the prefix to discriminate at all.
    """
    verb_only = "`release --actor lead-dev --id E-0001-x --state done`"
    assert unscoped_registry_call_sites(verb_only) == [
        "release --actor lead-dev --id E-0001-x --state done"
    ], (
        "the verb half of the gate does not fire: a registry invocation naming a verb and "
        "--actor with no --project went unreported. With `_REGISTRY_VERB` dead this is exactly "
        "what happens, while the `agentflow.registry`-prefixed half keeps the suite green"
    )

    # The prefixed half, which survives a dead pattern - present so a future reader can see that
    # the two arms are tested separately and that only the first one discriminates.
    prefixed = "`python -m agentflow.registry status --actor lead-dev`"
    assert unscoped_registry_call_sites(prefixed) == [
        "python -m agentflow.registry status --actor lead-dev"
    ]

    # And the negatives the gate must not fire on.
    assert unscoped_registry_call_sites("`release --actor lead-dev --project agentflow`") == []
    assert unscoped_registry_call_sites("`--actor steward`") == [], (
        "a bare prose mention of --actor with no verb is not a call site"
    )


# The hand list this gate carried until gh#374. Kept ONLY as a floor for the pin below: the derived
# set must never quietly lose a verb the gate used to check.
_GH374_OLD_HAND_LIST = frozenset({
    "status", "claim", "release", "apply", "retract", "ask", "note", "raise-blocker",
    "resolve-blocker", "deploy-lock", "reap", "repair", "validate", "read",
})


def test_the_gate_verb_set_is_the_clis_subcommand_set() -> None:
    """gh#374. The gate is COMPLETE, not merely alive: its verbs are exactly the CLI's.

    The other side of the comparison is read off a separately built parser through argparse's
    `_subparsers` group rather than through `_registry_verbs`, so a filter or typo inside the
    derivation cannot agree with itself.
    """
    group = _registry_module._build_parser()._subparsers._group_actions
    assert len(group) == 1
    cli = frozenset(group[0].choices)
    assert _REGISTRY_VERBS == cli, (
        f"call-site gate verbs disagree with the registry CLI: missing from gate "
        f"{sorted(cli - _REGISTRY_VERBS)}, not in CLI {sorted(_REGISTRY_VERBS - cli)}"
    )
    assert len(_REGISTRY_VERBS) >= 20, f"derived verb set is implausibly small: {sorted(_REGISTRY_VERBS)}"
    assert _GH374_OLD_HAND_LIST <= _REGISTRY_VERBS, (
        f"verbs the gate used to check are gone: {sorted(_GH374_OLD_HAND_LIST - _REGISTRY_VERBS)}"
    )
    # The compiled pattern must match every verb as a whole token -- a set that is right while the
    # alternation built from it is wrong would pass the equality above and gate nothing.
    unmatched = sorted(v for v in _REGISTRY_VERBS if not _REGISTRY_VERB.fullmatch(v))
    assert not unmatched, f"_REGISTRY_VERB does not match these verbs whole: {unmatched}"


def test_a_new_cli_subcommand_reaches_the_gate_without_editing_it(monkeypatch) -> None:
    """gh#374 Test (1): register a synthetic subparser no hand list could know about, and the
    gate's verb set picks it up -- so adding a subcommand can never leave its call sites unchecked."""
    real_build = _registry_module._build_parser

    def build_with_synthetic() -> argparse.ArgumentParser:
        parser = real_build()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        sub.add_parser("zz-synthetic-verb")
        return parser

    monkeypatch.setattr(_registry_module, "_build_parser", build_with_synthetic)
    verbs = _registry_verbs()
    assert "zz-synthetic-verb" in verbs
    assert "zz-synthetic-verb" not in _GH374_OLD_HAND_LIST
    span = "zz-synthetic-verb --actor lead-dev --id E-0001-x"
    assert _verb_pattern(verbs).search(span), "the derived pattern does not see the new subcommand"
    assert not _verb_pattern(_GH374_OLD_HAND_LIST).search(span), (
        "control: a hand list must NOT see it, or this test does not discriminate"
    )


@pytest.mark.parametrize("verb", sorted({
    "answer", "assign-blocker", "cull", "directive", "expire-directive", "init",
    "preflight", "publish", "relane", "stand-down", "withdraw",
}))
def test_a_bare_unscoped_call_for_a_formerly_unlisted_verb_is_caught(verb: str) -> None:
    """gh#374 Test (2). These verbs had no `agentflow.registry` prefix to fall back on and were
    absent from the hand list, so neither arm of the disjunction saw them. `assign-blocker` and
    `directive` already had bare call sites in dev-manager/SKILL.md."""
    bare = f"{verb} --actor dev-manager --id B-0001 --owner lead-dev"
    assert unscoped_registry_call_sites(f"`{bare}`") == [bare], (
        f"a bare `{verb}` call with --actor and no --project went unreported"
    )
    assert unscoped_registry_call_sites(f"`{bare} --project agentflow`") == []


def test_verbs_are_matched_as_whole_tokens_not_substrings() -> None:
    """Widening the set to 25 verbs made generic ones (`read`, `note`, `init`, `answer`) part of the
    gate. Without the word boundaries they would match inside prose words and demand --project of
    sentences that invoke nothing."""
    assert unscoped_registry_call_sites("`--actor steward already noted`") == []
    assert unscoped_registry_call_sites("`--actor steward reinitialised`") == []
    # And a hyphenated verb is still one token.
    assert unscoped_registry_call_sites("`stand-down --actor jr-dev-1`") == ["stand-down --actor jr-dev-1"]


# The single-module control-byte scan that used to live here is superseded by
# tests/test_control_bytes.py, which runs the same predicate over the whole git index instead of
# over `Path(__file__)`. It was moved rather than copied: two scans with one predicate is how the
# narrow one goes on passing after the wide one stops being run (gh#297).


@pytest.mark.parametrize("skill", SELF_WAKING_LOOPS)
def test_continuation_prompt_forwards_the_project(skill: str) -> None:
    """Trap 2. Every ScheduleWakeup must have a prompt=, and the prompt must
    carry the project through, or the flag survives exactly one wake."""
    text = _skill_text(skill)
    # No \s* before the paren: `ScheduleWakeup (one epic per wake...)` is prose with
    # a parenthetical, not a call, and matching it would gate documentation.
    calls = [m.start() for m in re.finditer(r"ScheduleWakeup\(", text)]
    assert calls, f"{skill}/SKILL.md schedules no continuation at all"
    for start in calls:
        span = _spans(text, start, 400)
        assert "prompt=" in span, (
            f"{skill}/SKILL.md has a ScheduleWakeup with no prompt= - the wake would "
            f"re-enter with no command: {span.splitlines()[0]}"
        )
    # Only prompts inside an ACTUAL ScheduleWakeup( span count. Skills document
    # the bare form as a counter-example ("never a bare prompt=/jr-dev-1"), and a
    # file-wide scan would red on exactly the sentence that teaches the rule -
    # the same false-positive shape that made C10 gate nothing.
    called = []
    for start in calls:
        called += re.findall(r'prompt="(/[^"]+)"', _spans(text, start, 400))
    assert called, f"{skill}/SKILL.md has no prompt= inside any ScheduleWakeup( span"
    # AT LEAST ONE must forward it - not ALL. A skill legitimately documents the
    # no-flag branch too ("no --project given -> prompt=/steward, unchanged"), and
    # a launch without the flag must keep continuing without it. What trap 2 is
    # actually about is a skill that forwards it NOWHERE, so the flag survives
    # exactly one wake and every self-wake after lands on the default.
    forwarding = [p for p in called if "--project" in p]
    assert forwarding, (
        f"{skill}/SKILL.md never forwards --project in any ScheduleWakeup prompt "
        f"(found {called}) - the flag would survive exactly one wake, then every "
        "self-wake after it would silently run the default project"
    )
    # "At least one" alone was too weak: it let the PRIMARY's epic-boundary
    # hand-off drop the flag while a sibling call kept the gate green. A bare
    # prompt is legitimate ONLY on a line that states the no-flag condition -
    # which is exactly how the conditional branch is written ("no `--project` ->
    # prompt=/steward, unchanged"). Structural, not negation-proximity.
    nl = chr(10)
    for start in calls:
        span = _spans(text, start, 400)
        line = text[:start].rsplit(nl, 1)[-1] + span.split(nl)[0]
        for pr in re.findall(r'prompt="(/[^"]+)"', span.split(nl)[0]):
            if "--project" in pr:
                continue
            # A generic placeholder (`/steward <args>`, `$ARGUMENTS`) forwards
            # whatever was given, flag included - it is not a bare prompt.
            if "<args>" in pr or "ARGUMENTS" in pr:
                continue
            assert "no `--project`" in line or "no --project" in line, (
                f"{skill}/SKILL.md has a bare continuation prompt {pr!r} that is NOT on a "
                "documented no-flag branch - that wake would silently drop back to the "
                f"default project. Line: {line.strip()[:160]!r}"
            )


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_skill_states_the_strip_first_rule(skill: str) -> None:
    """Trap 3. The flag must be removed from $ARGUMENTS before the remainder is
    read as a work target."""
    text = _skill_text(skill)
    # CO-LOCATION, not two independent substrings. The first version asserted
    # `"strip" in text and "--project" in text` over the whole file, which stayed
    # green when the rule was deleted and replaced by its own INVERSION - the word
    # "strip" survives elsewhere in a 700-line file, and `--project` is already
    # mandated by three sibling gates. Require one line to carry both, plus the
    # thing being stripped FROM.
    lines = [ln for ln in text.splitlines() if "strip" in ln.lower()]
    stating = [
        ln for ln in lines
        if "--project" in ln and ("$ARGUMENTS" in ln or "ARGUMENTS" in ln)
    ]
    assert stating, (
        f"{skill}/SKILL.md never states the strip-first rule on a single line naming BOTH "
        "`--project` and `$ARGUMENTS`; the flag would survive in the argument string and be "
        f"read as a work target. Lines mentioning 'strip': {lines[:3]}"
    )


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_skill_names_the_config_show_contract(skill: str) -> None:
    text = _skill_text(skill)
    assert "agentflow.config show" in text, (
        f"{skill}/SKILL.md never resolves the named project - without "
        "`python -m agentflow.config show --project <name>` the flag is inert prose"
    )


# The marker TOKENS each loop actually writes into its `phase` cell. Anchoring on
# these is the whole gate: the first version of this test looked for the words
# "mis-launch" INSIDE the marker span, and no marker contains them - so it matched
# zero spans across all six loops and asserted nothing at all while reading green.
_MARKER_TOKENS = ("PROJECT-OVERRIDE", "proj-arg", "proj=", "proj-override", "session-default")


def _marker_spans(text: str) -> list[str]:
    """Every backtick span and fenced line that carries a mis-launch marker token."""
    found = []
    for span in re.findall(r"`([^`]*)`", text):
        if any(tok in span for tok in _MARKER_TOKENS):
            found.append(span)
    # dev-manager writes its marker inside a fenced block, with no backtick span.
    for line in text.splitlines():
        if any(tok in line for tok in _MARKER_TOKENS) and "`" not in line:
            found.append(line.strip())
    return found


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_mislaunch_marker_is_table_safe(skill: str) -> None:
    """The marker goes in an `## Agents` row `phase` cell. A pipe would split the
    markdown table column, and the registry CLI refuses pipes in free-text cells
    outright - so a pipe here breaks the FIRST registry write of every mis-launched
    wake for that loop."""
    text = _skill_text(skill)
    spans = _marker_spans(text)
    # Vacuity guard: if this finds nothing the gate is dead, and a dead gate that
    # reads as protection is worse than no gate.
    assert spans, (
        f"{skill}/SKILL.md has no mis-launch marker matching {_MARKER_TOKENS} - either the "
        "marker was removed or it was renamed and this gate went silently vacuous"
    )
    # A pipe is fatal wherever the marker appears: it splits the Agents table cell.
    for span in spans:
        assert "|" not in span, (
            f"{skill}/SKILL.md mis-launch marker contains a pipe: {span!r} - the registry "
            "refuses it (inline_only) and the wake's first status write dies"
        )
    # ASCII is required specifically of the `--phase` VALUE, which lands in the
    # registry cell. The ntfy prefix marker legitimately carries the loop's own
    # separators, so asserting ASCII over every marker span would red on prose the
    # skills are supposed to have.
    phase_spans = [s for s in spans if "--phase" in s]
    assert phase_spans, (
        f"{skill}/SKILL.md never shows the marker inside a `--phase` value - the Agents-row "
        "half of the mis-launch guard is undocumented"
    )
    for span in phase_spans:
        assert span.isascii(), (
            f"{skill}/SKILL.md `--phase` mis-launch marker is non-ASCII: {span!r}"
        )


#: The ONE construct that actually forwards the wake's arguments into the loop skill:
#: `Skill(<loop>, args="$ARGUMENTS")`. The loop name is captured so it can be matched
#: against the wrapper file it was found in - `Skill(lead-dev, ...)` inside
#: `commands/steward.md` forwards steward's arguments to the wrong loop.
_SKILL_ARGS_FORWARD = re.compile(
    r"Skill\(\s*([a-z0-9-]+)\s*,\s*args\s*=\s*[\"']\$ARGUMENTS[\"']\s*\)"
)


#: Wrappers that forward `$ARGUMENTS` onto a shell command line that runs an agentflow MODULE,
#: not into a skill, mapped to that module. They take positional arguments and no `--project`,
#: so the Skill(...) gate below does not fit them; the module-forwarding gate further down
#: holds them to the equivalent single-line invocation instead.
_MODULE_FORWARDING_WRAPPERS = {"uc-touchpoints": "agentflow.use_case_impact"}


def _forwarding_wrappers() -> list[str]:
    """Command wrappers that CLAIM to forward arguments, by naming `$ARGUMENTS` at all.

    Derived, not listed, and the derivation has to survive the mutation it guards against.
    `ALL_LOOPS` was the obvious source and is the wrong one: `commands/report.md` forwards
    too, is not a loop, and was therefore ungated - deleting its forwarding line reproduced
    the exact bug this pair of tests exists to close, silently. Meanwhile three wrappers
    (autodoc-init, autodoc-update, graph-query) never mention `$ARGUMENTS` and take no
    arguments, so requiring them to forward would be inventing a rule. The module-forwarding
    wrappers in `_MODULE_FORWARDING_WRAPPERS` do forward, into a command line rather than a
    skill, and are gated by `test_module_forwarding_wrapper_runs_its_module_with_arguments`.

    Keyed on MENTIONING the token rather than on carrying the forwarding line, because
    keying on the line would make the enumeration self-fulfilling: deleting the line - the
    whole defect - would drop the file out of its own gate. Every forwarding wrapper says
    `$ARGUMENTS` three or four more times in its frontmatter and prose, so it stays in
    scope and reds, which is precisely how the original bug hid.
    """
    return sorted(
        p.stem for p in COMMANDS_DIR.glob("*.md")
        if "$ARGUMENTS" in p.read_text(encoding="utf-8", errors="replace")
        and p.stem not in _MODULE_FORWARDING_WRAPPERS
    )


@pytest.mark.parametrize("loop", _forwarding_wrappers())
def test_command_wrapper_forwards_arguments(loop: str) -> None:
    """Trap 1's other half: the wrapper must both DOCUMENT the flag and actually
    pass $ARGUMENTS through, or the skill never sees it.

    SCOPED TO A SINGLE LINE, and that is the whole gate. This test used to be two
    whole-file substring checks (`"--project" in text` and `"$ARGUMENTS" in text`),
    and a fix-commit claimed it "now requires the Skill(<loop>, args=...) invocation"
    when no such code was ever written. Both tokens appear several times in every
    wrapper's frontmatter description and prose, so DELETING the one line that
    actually forwards the arguments left the file passing: the skill would be entered
    with no arguments at all, the project selector would evaporate on wake 1, and
    nothing in the suite would say so.

    The `--project` check below stays whole-file on purpose: it gates that the wrapper
    DOCUMENTS the flag, which is prose and legitimately lives anywhere in the file.

    LINE-SCOPED, which is weaker than "structural" and is worth naming rather than
    overselling. It reads markdown, so a fenced example or a "never write this" line
    carrying the same invocation would satisfy it. What it does buy is the whole distance
    between "these two tokens appear somewhere in a 20-line file" and "this exact
    invocation, for THIS loop, appears on one line" - which is the gap the defect lived in.
    Closing the remainder means parsing the markdown for prose-versus-code context, and
    that is not worth the machinery for a file this shape.
    """
    path = COMMANDS_DIR / f"{loop}.md"
    assert path.is_file(), f"commands/{loop}.md not found at {path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    assert "--project" in text, f"commands/{loop}.md never mentions --project"
    forwarding = [
        ln.strip()
        for ln in text.splitlines()
        if any(m.group(1) == loop for m in _SKILL_ARGS_FORWARD.finditer(ln))
    ]
    assert forwarding, (
        f"commands/{loop}.md carries no `Skill({loop}, args=\"$ARGUMENTS\")` invocation on a "
        "single line - the wrapper may still SAY --project and $ARGUMENTS in its prose while "
        "forwarding nothing, so the skill is entered with empty arguments and every self-wake "
        "after it falls back to the session's injected default project"
    )


#: A hardcoded use-case id. A wrapper that bakes one into its command line answers for that id
#: whatever the caller asked about.
_LITERAL_UC_ID = re.compile(r"\bUC-\d{4}\b")
#: A reference to an `AF` shell variable. Nothing in the suite defines one, so a command line
#: that depends on it runs `cd /skills/_shared` and exits 1 in every fresh session (gh#744).
_AF_VAR = re.compile(r"\$\{?AF\b")


def _module_forwarding_line(name: str, module: str) -> str:
    """The one line in `commands/<name>.md` that runs `module` with the caller's arguments."""
    path = COMMANDS_DIR / f"{name}.md"
    assert path.is_file(), f"commands/{name}.md not found at {path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    expected = f"python -m {module} $ARGUMENTS"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() == expected]
    assert len(lines) == 1, (
        f"commands/{name}.md must carry exactly one `{expected}` line, found {len(lines)}: "
        "without it the caller's arguments never reach the module"
    )
    return lines[0]


@pytest.mark.parametrize("name", sorted(_MODULE_FORWARDING_WRAPPERS))
def test_module_forwarding_wrapper_runs_its_module_with_arguments(name: str) -> None:
    """gh#744: the uc-touchpoints wrapper ran `cd "$AF/skills/_shared" && python -c ... UC-0147`.

    Nothing sets `AF`, so the `cd` failed and the command exited 1 in a fresh session, and the
    hardcoded id meant the caller's id and project were never used even where `AF` happened to
    be set. The package is pip-installed, so the module is reachable by name from any cwd.
    """
    module = _MODULE_FORWARDING_WRAPPERS[name]
    text = (COMMANDS_DIR / f"{name}.md").read_text(encoding="utf-8", errors="replace")
    _module_forwarding_line(name, module)
    assert not _AF_VAR.search(text), (
        f"commands/{name}.md references `$AF`, which nothing in the suite sets"
    )
    literal = _LITERAL_UC_ID.findall(text)
    assert not literal, (
        f"commands/{name}.md hardcodes use-case id(s) {literal}; a literal id in the command "
        "line answers for that id instead of the caller's, and one in the prose invites copying"
    )


@pytest.mark.parametrize(
    ("arguments", "stderr_marker"),
    [
        ("UC-0001", "environment config unavailable"),
        ("UC-0001 no-such-project", "environment config unavailable"),
        ("", "usage: uc-touchpoints"),
    ],
)
def test_uc_touchpoints_forwarded_shape_runs_from_any_cwd(
    tmp_path: Path, arguments: str, stderr_marker: str
) -> None:
    """Run the wrapper's own command line, with `$ARGUMENTS` substituted, from an unrelated
    cwd - the fresh-session condition gh#744 failed under. An empty config home keeps the
    developer's real config out of it; the tool must degrade to a report and exit 0, and the
    no-argument case must print its usage line rather than silently pick an id.
    """
    line = _module_forwarding_line("uc-touchpoints", _MODULE_FORWARDING_WRAPPERS["uc-touchpoints"])
    argv = shlex.split(line.replace("$ARGUMENTS", arguments))
    assert argv[:3] == ["python", "-m", "agentflow.use_case_impact"], argv
    argv[0] = sys.executable
    env = dict(os.environ)
    env["AGENTFLOW_CONFIG"] = str(tmp_path / "no-config")
    env.pop("AF", None)
    # The package is importable here through this suite's sys.path insert; the subprocess
    # needs the same root, which an editable install would otherwise provide.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SKILLS_ROOT / "_shared"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        argv, cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, (
        f"`{line}` with $ARGUMENTS={arguments!r} exited {result.returncode}: {result.stderr!r}"
    )
    assert stderr_marker in result.stderr, result.stderr
    if arguments:
        uc = arguments.split()[0]
        assert uc in result.stdout, (
            f"the forwarded id {uc} is not the one reported: {result.stdout!r}"
        )


def test_the_dispatch_guard_actually_sees_lead_devs_call_sites() -> None:
    """NON-VACUITY. Without this, the guard above passes hardest when it sees nothing.

    Its sibling `test_builder_survey_call_sites_are_project_scoped` asserts `calls` for
    exactly this reason; the dispatch guard could not, because several builders delegate
    selection to lead-dev's prose and legitimately name no call at all. So the non-vacuity
    check is pinned HERE, on the one loop that owns selection and must always publish it.

    This is not hypothetical. Renaming the published call from `dispatchable` to
    `selectable` (issue #40) left the guard matching a name lead-dev no longer used: it saw
    zero call sites, passed, and dropping `project=<name>` from both real call sites did not
    fail a single test. A rename can hollow out a passing test without touching it.
    """
    calls = [c for c in _calls(_skill_text("lead-dev"), _DISPATCH)
             if "(" in c and c.split("(", 1)[1].strip(") \n\t")]
    assert calls, (
        "lead-dev/SKILL.md names no arg-bearing selectable()/dispatchable() call — the "
        "project-scope guard above is now vacuous. Did the selection call get renamed again?"
    )


def test_lead_dev_publishes_selectable_and_not_the_hand_composition() -> None:
    """The composition ORDER is prose, so only a prose gate can hold it.

    `ready_set()` derives its done-set from the list handed to it, so
    `ready_set(dispatchable(...))` hides every finished prerequisite and strands approved
    work with satisfied deps. `selectable()` fixes the order once, in code — but the skills
    are what CALL it, and reverting the skill text to the old two-call composition breaks
    selection while every unit test stays green.

    WHAT THIS CANNOT PROVE, stated because a gate trusted past its reach is worse than none:
    it is a file-level text check. It cannot tell whether the call is in the right section,
    whether its arguments are right, or whether a loop reading the file acts on it. It only
    ensures the file publishes the safe call and does not publish the unsafe one.
    """
    text = _skill_text("lead-dev")
    assert "selectable(" in text, "lead-dev/SKILL.md must publish selectable() as its selection call"
    bad = re.findall(r"ready_set\s*\(\s*dispatchable", text)
    assert not bad, (
        "lead-dev/SKILL.md publishes ready_set(dispatchable(...)) — that order strands every "
        "approved epic whose dependencies are done. Use selectable()."
    )


@pytest.mark.parametrize("skill", DEP_REASONING_SURVEY_SKILLS)
def test_the_published_survey_call_feeds_selectable_an_archive_inclusive_corpus(skill: str) -> None:
    """Gates the CALL SITE, not just the capability.

    `include_archived=True` being available proves nothing if the skills do not pass it - the
    defect this replaces was exactly a correct function fed the wrong list. Without it the
    survey deletes archived records before `selectable()` runs, so `ready_set` cannot see an
    archived prerequisite is finished and its approved dependent strands forever, silently.

    PARSED FROM THE CALL, not from a window of surrounding text. The first version of this
    test scanned 200 characters after `survey_epics(` and passed on the PROSE EXPLAINING the
    argument: deleting `include_archived=True` from the actual call left the sentence
    describing it inside the window, and the mutation survived. A gate that can be satisfied
    by its own documentation is not a gate. `_calls` is paren-balanced, so it sees arguments
    and nothing else.

    WIDENED from `lead-dev` alone (gh-45). The original pinning was deliberate - the non-vacuity
    argument was said to hold only for the loop that must always publish a selection call - but
    the defect had more callers than the guard did, which is how `choose-next` carried it
    untouched through the very fix that named it. Non-vacuity is preserved per skill by the
    `assert calls` line: a skill that stops publishing a survey call fails loudly here rather
    than passing on an empty list.
    """
    calls = _calls(_skill_text(skill), _CALL)
    assert calls, f"{skill}/SKILL.md publishes no survey_epics call"
    assert any("include_archived=True" in c for c in calls), (
        f"{skill}/SKILL.md surveys without include_archived=True, so an archived prerequisite "
        f"is deleted before dep math sees it and its dependent strands silently: {calls}"
    )


def test_steward_note_call_sites_carry_the_byte_cap_and_the_file_convention() -> None:
    """The cap is enforced in code; the SKILL still has to say so where the note is composed.

    steward is the one loop whose `note` is a REPORT — a cadence table across every Agents row,
    growing with the fleet. A loop that meets the cap for the first time as a refusal has
    already lost the wake's work, and the obvious recovery ("send it as two notes") is the
    exact evasion the cap exists to prevent. So the budget has to be visible before the write,
    together with where the overflow goes instead.
    """
    text = _skill_text("steward")
    for claim, wording in (
        ("the cap, in bytes", "300 UTF-8 bytes"),
        ("splitting is not the recovery", "Never split it"),
        ("where the analysis goes instead", "`notes/` beside `registry.md`"),
        ("what the note then carries", "notes/<file>.md"),
    ):
        assert wording in text, (
            f"steward/SKILL.md never states {claim} ({wording!r}) at its `note` call sites, so "
            "the first steward to exceed the cap learns the rule only from a refusal"
        )


# --------------------------------------------------------------------------- #
# E-0280 (fleet-report-skill), stage 3: the TRIGGER.
#
# The collector and the skill are both inert until something fires them. The
# firing is prose in dev-manager/SKILL.md, so only a prose gate can hold it, and
# there are three specific ways it silently stops working:
#
#   1. The step is written but lands AFTER the pump. The report is the picture of
#      the state the pass is about to act on; emitted after §1 has answered
#      questions and §2 has reaped, it describes a fleet that no longer exists and
#      becomes a report of the pass's own side effects.
#   2. The 24h ceiling is documented on the SESSION-START branch only. Most passes
#      are unattended - no session ever "starts" - so the ceiling would be met only
#      by a human happening to sit down, which is the exact case session-start
#      already covers. Named nowhere in the unattended enumerations, it is
#      decorative.
#   3. The two state fields are named by only one side. The report WRITES them and
#      dev-manager READS them back by name; a field spelled differently at either
#      end is a ceiling that never trips and a diff with no prior, with nothing
#      raising an error at any point.
# --------------------------------------------------------------------------- #

_REPORT_STEP_HEADING = "### 0.5 Fleet report"
_PUMP_HEADING = "### 1. QUESTION PUMP"
_WAKE_READ_HEADING = "### 0. Registry wake read"

# Written by report/SKILL.md Step 9, read by dev-manager §0.5. Both ends, by name.
_REPORT_STATE_FIELDS = ("last_report_at", "last_report_path")


def _section(skill: str, heading: str) -> str:
    """One `###` step of a SKILL.md, terminated by the next `###` heading."""
    text = _skill_text(skill)
    start = text.find(heading)
    assert start != -1, (
        f'{skill}/SKILL.md has no "{heading}" section - this gate anchors on it; renaming or '
        "deleting the step silently un-gates everything asserted about it"
    )
    end = text.find("\n### ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def test_dev_manager_fires_the_report_on_both_documented_triggers() -> None:
    """Both branches, inside the step - not merely somewhere in an 890-line file.

    Session start alone leaves an unattended fleet with no report for as long as
    nobody sits down; the ceiling alone means the human who does sit down reads
    yesterday's. The spec locks both, so the step has to carry both.
    """
    section = _section("dev-manager", _REPORT_STEP_HEADING)
    flat = _flat(section)
    assert "Session start" in section, (
        "dev-manager/SKILL.md's report step names no session-start branch - trigger 1 of the "
        "three the spec locks"
    )
    assert "24h" in section, (
        "dev-manager/SKILL.md's report step names no 24h ceiling - a session that never opens "
        "then never reports, which is the case the ceiling exists for"
    )
    assert "last_report_at" in section, (
        "dev-manager/SKILL.md's report step states a 24h rule with nothing to measure it "
        "against; the ceiling has to read `last_report_at`, or it is guessed rather than checked"
    )
    # The invocation must carry the RESOLVED project. A report rendered against the
    # session default while the pass pumps the argument's fleet describes a different
    # fleet, in the one artefact a human reads INSTEAD of the registry.
    assert "/report --project" in flat, (
        "dev-manager/SKILL.md's report step invokes `/report` without `--project` - on a "
        "`--project`-launched wake that reports the session default's fleet"
    )


def test_the_report_step_runs_before_the_pump_and_after_the_wake_read() -> None:
    """Pure ORDERING, so presence proves nothing - only offsets do.

    Moving the step below §1 leaves every other assertion in this block green while
    turning the report into a description of the pass's own side effects: questions
    already answered, lanes already reaped, an inbox already struck.
    """
    text = _skill_text("dev-manager")
    wake = text.find(_WAKE_READ_HEADING)
    report = text.find(_REPORT_STEP_HEADING)
    pump = text.find(_PUMP_HEADING)
    assert wake != -1, f'dev-manager/SKILL.md has no "{_WAKE_READ_HEADING}" section'
    assert report != -1, f'dev-manager/SKILL.md has no "{_REPORT_STEP_HEADING}" section'
    assert pump != -1, f'dev-manager/SKILL.md has no "{_PUMP_HEADING}" section'
    assert wake < report < pump, (
        f"dev-manager/SKILL.md orders the report step wrong (wake read@{wake}, report@{report}, "
        f"pump@{pump}). It must follow §0 - it needs §0a's resolved project and the registry - "
        "and precede §1, because §1 onward is where the pass starts acting and a report emitted "
        "after that describes a fleet the pass itself changed"
    )


@pytest.mark.parametrize("field", _REPORT_STATE_FIELDS)
def test_report_state_fields_are_named_by_both_writer_and_reader(field: str) -> None:
    """Trap 3. One name, two files, and nothing compares the spellings.

    report/SKILL.md Step 9 writes the field; dev-manager §0.5 reads it back. A
    mismatch surfaces only as a ceiling that never trips and a diff that never has a
    prior - both of which look exactly like a quiet fleet.
    """
    for skill in ("report", "dev-manager"):
        assert field in _skill_text(skill), (
            f"{skill}/SKILL.md never names `{field}`. The writer and the reader of a state-file "
            "key have to spell it identically; nothing checks that at runtime"
        )


def test_the_report_step_states_the_never_acts_rule() -> None:
    """The trigger is where that rule is most likely to erode.

    dev-manager is the one caller that is itself a mutating pass, so a future editor
    folding "and while you are in there, label the untriaged issues" into the report
    step is the natural next edit. The spec's first non-goal is that the report never
    acts, and the step that FIRES it has to carry that - not only the skill being
    fired.
    """
    section = _section("dev-manager", _REPORT_STEP_HEADING)
    assert "never acts" in section, (
        "dev-manager/SKILL.md's report step does not say the report NEVER ACTS - the spec's "
        "first non-goal, and 'anything that both reports and mutates will eventually be run for "
        "its side effect'"
    )


# --------------------------------------------------------------------------- #
# E-0280 repairs. Four contracts that live only in prose, each of which failed
# silently rather than loudly when it was wrong:
#
#   1. The archive WRITER path and the archive READER path. They were different
#      by one component, so every run wrote where no run looked and the diff had
#      no prior forever, with nothing raising.
#   2. HEALTH's two counts are survey-derived and were the one pair of
#      scan-derived numbers with no `counted N, skipped M` beside them.
#   3. §10 rewriting the state file from the values §0.5 read BEFORE the report
#      ran, which un-advances the 24h stamp the report just wrote.
#   4. The reply verbs promising "full" lists out of a capped object.
# --------------------------------------------------------------------------- #
def test_the_skill_writes_the_archive_to_the_path_the_next_run_reads() -> None:
    """`archive.path` is the collector's single value for both ends.

    Any hand-assembled `<vault.root>/<something>/<filename>` is a second spelling of the same
    location, and the two only have to disagree once - silently, forever, because a diff with
    no prior is indistinguishable from a genuinely quiet fleet.
    """
    flat = _flat(_skill_text("report"))
    assert "info.path" in flat or "archive.path" in flat, (
        "report/SKILL.md never names `archive.path`, so the runner has to assemble the archive "
        "location by hand - and a writer path that differs from `find_latest_archive`'s reader "
        "path by one component makes the diff permanently priorless with nothing raising"
    )
    assert "<vault.root>/<info.directory>" not in flat, (
        "report/SKILL.md still tells the runner to assemble `<vault.root>/<info.directory>/...`; "
        "that dropped the archive subdirectory the reader globs"
    )


def test_the_skill_names_healths_scan_among_the_skip_count_carriers() -> None:
    """HEALTH's inert and out-of-vocabulary counts come straight from the epic survey.

    Listed nowhere, they are rendered bare next to WORK's `counted N, skipped M` - the exact
    "confident-looking partial fact" the HARD RULE exists to forbid, on the two numbers the
    spec puts first in the HEALTH row.
    """
    flat = _flat(_skill_text("report"))
    for field in ("report.work.scan", "report.blocked.scan", "report.health.scan"):
        assert field in flat, (
            f"report/SKILL.md's HARD RULE step does not name `{field}` as a carrier of "
            "`counted N, skipped M`, and its list reads as exhaustive"
        )


def test_the_report_verbs_are_told_how_to_reach_past_the_cap() -> None:
    """`intake` and `blockers` walk items ONE AT A TIME and are documented as "full".

    Every list section is cut to `rows_per_section` and the remainder discarded, so a walk
    driven off the rendered rows cannot reach issue 11 of 20 - which is the spec's own
    motivating number. The uncapped seam has to be named, or "full" is false.
    """
    flat = _flat(_skill_text("report"))
    assert "rows_per_section=-1" in flat, (
        "report/SKILL.md's reply verbs promise the FULL list but never name the uncapped "
        "collection (`rows_per_section=-1`), so the disposition walk stops at the cap with no "
        "path to the rest"
    )


# --------------------------------------------------------------------------- #
# Config write scope (E-0200) - a product rule, so it lives in the suite
# --------------------------------------------------------------------------- #
# The second copy of the roster used to live here, restating the same six names. Two literals
# meant two places to forget, and jr-dev-3 was forgotten in both. The module-level `ALL_LOOPS`
# above is derived from `agentflow.registry.AGENTS` and serves the gates below unchanged.


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_every_loop_carries_the_config_write_scope_rule(skill: str) -> None:
    """It shipped from two per-machine config profiles into the suite, and prose
    that lives in only one place is prose the next SKILL.md rewrite drops
    silently. Six loops can write config; six loops state the limit."""
    text = " ".join((SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8").split())
    assert "CONFIG WRITE SCOPE" in text, f"{skill}/SKILL.md does not state the rule"
    assert "never any other project" in text, f"{skill}/SKILL.md states it without the limit"


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_every_loop_points_at_the_full_rule(skill: str) -> None:
    """The one-line form is a reminder, not the rule. Each copy names where the
    whole thing lives so the short form cannot quietly become the definition."""
    text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
    i = text.index("CONFIG WRITE SCOPE")
    assert "SCHEMA.md" in text[i:i + 900], f"{skill}/SKILL.md does not point at SCHEMA.md"


def test_the_full_rule_exists_where_the_loops_point() -> None:
    """The pointer above is only worth having if its target is real."""
    schema = (SKILLS_ROOT / "_shared" / "SCHEMA.md").read_text(encoding="utf-8")
    assert re.search(r"^##\s*" + chr(167) + r"6\.", schema, re.M), "SCHEMA.md has no " + chr(167) + "6"
    # Whitespace-normalised: the rule is prose and wraps where the line ends, so a
    # literal substring check would be a test of the paragraph's line breaks.
    body = " ".join(schema[schema.index(chr(167) + "6."):].split())
    for required in ("never any other project", "environment.md", "## Files"):
        assert required in body, f"SCHEMA.md {chr(167)}6 omits {required!r}"


def test_the_rule_keeps_environment_md_spec_gated() -> None:
    """The half that is easy to lose in a rewrite. environment.md is machine-level
    and shared by every fleet on the box, so it is writable only when the epic's
    approved `## Files` section names it - that gating IS the approval."""
    schema = (SKILLS_ROOT / "_shared" / "SCHEMA.md").read_text(encoding="utf-8")
    body = schema[schema.index(chr(167) + "6."):]
    row = [ln for ln in body.splitlines() if ln.startswith("| `environment.md`")]
    assert row, "SCHEMA.md " + chr(167) + "6 has no environment.md row"
    assert "## Files" in row[0] and "never" not in row[0].lower().split("only")[0]
def test_the_report_skill_requires_the_no_work_sentence() -> None:
    """Prose in one place is prose the next rewrite drops, and this one is a
    requirement rather than a style note: p195 removed lead-dev's halt, so the report
    is now the only channel that tells a human the product has run out of work."""
    text = " ".join((SKILLS_ROOT / "report" / "SKILL.md").read_text(encoding="utf-8").split())
    assert "no_work_anywhere" in text, "the skill does not name the property to read"
    assert "NO_WORK_SENTENCE" in text, "the skill does not name the sentence to print"


def test_the_report_skill_says_why_it_is_not_is_empty() -> None:
    """The trap next to it: `work.is_empty` also reads `shipped`, so a day on which
    something shipped but no work remains answers False - the exact day a human needs
    told. A future editor who does not know that will 'simplify' to is_empty."""
    text = " ".join((SKILLS_ROOT / "report" / "SKILL.md").read_text(encoding="utf-8").split())
    i = text.index("no_work_anywhere")
    assert "is_empty" in text[i:i + 1200], "the skill does not warn against is_empty"
# --------------------------------------------------------------------------- #
# The readability rules (E-0290 changes 1, 3, 4, 6) - prose, so pin it
# --------------------------------------------------------------------------- #
def _report_skill() -> str:
    return " ".join((SKILLS_ROOT / "report" / "SKILL.md").read_text(encoding="utf-8").split())


def test_the_reply_verbs_are_rendered_above_the_title() -> None:
    """They are the page's only interactive surface. At the foot, a reader on a
    phone reaches them after scrolling seven sections - and the skill's own argument
    for their prominence ("a capability nobody knows about is not a capability")
    does not survive putting them last."""
    text = _report_skill()
    assert "ABOVE THE TITLE" in text
    assert "at the foot of the page" not in text, "the old foot-of-page rule survives"


def test_the_drafted_list_is_two_columns_and_never_pills() -> None:
    """21 slugs as bordered pills reflow into a ragged block that cannot be
    scanned. The rule generalises: pills are for a handful of short tokens."""
    text = _report_skill()
    assert "two-column plain list, never pills" in text
    assert "640px" in text, "the collapse point is part of the rule, not decoration"


def test_the_theme_is_a_single_light_one_that_still_paints_explicitly() -> None:
    """A deliberate single-theme choice, not an omission - and specifically NOT
    permission to inherit the host's ground."""
    text = _report_skill()
    assert "One LIGHT theme" in text
    i = text.index("One LIGHT theme")
    assert "EXPLICITLY" in text[i:i + 500], "single-theme drifted into painting nothing"


def test_intake_renders_issue_titles_beside_the_numbers() -> None:
    """The slug rule exempts tracker issues because they have no slug - true, and
    it misses the point the rule was protecting: a bare number is unreadable
    whether or not it is the correct identifier."""
    text = _report_skill()
    assert "AND the issue title" in text
    i = text.index("AND the issue title")
    for surface in ("untriaged", "selectable"):
        assert surface in text[i:i + 400], f"the rule does not name the {surface} list"


def test_every_lane_count_the_collector_exposes_is_named_in_the_contract_row() -> None:
    """A count no instruction names is computed then discarded, one layer up.

    The renderer here is an LLM reading this skill, so the `lanes` contract row IS the
    interface: a field present on `LanesSection` and absent from that row is unreachable, and
    unreachable in the same way the counts were before this epic - the defect being fixed,
    not a style point.

    MEASURED: `blocked_count` was added to the dataclass and named in NO instruction, one
    release after `dry_count` reached the row. Fixing that instance without a guard would
    leave the NEXT count in the same state, which is exactly how this one arrived.

    DERIVED from the dataclass rather than restated, so a count added later is covered on
    the day it lands instead of whenever someone remembers to extend a list here.
    """
    from agentflow import fleet_report as fr

    counts = sorted(
        f for f in fr.LanesSection.__dataclass_fields__ if f.endswith("_count")
    )
    assert counts, "LanesSection exposes no *_count fields, so this guard reads nothing"
    text = _report_skill()
    missing = [c for c in counts if f".{c}" not in text]
    assert not missing, (
        f"skills/report/SKILL.md names none of {missing}, so the renderer is never told to "
        "read them. They are computed by the collector and discarded at the instruction "
        "layer, which is the same defect one level up from the one this epic fixed"
    )

def test_the_lane_diagram_paints_every_state_the_collector_can_emit() -> None:
    """Change 7 is the only one of the seven with no prose pin, and the asymmetry is why
    its renderer half stayed missing through a whole audit: every other change fails a grep
    if its instruction is dropped, this one had nothing to drop.

    The failure it guards is silent rather than loud. A state with no `classDef` does not
    error — Mermaid falls through to the default fill, so DRY and BLOCKED both drew as the
    same unstyled node, which reads as a diagram that failed to style rather than as three
    states nobody painted. Counted against the collector's `LANE_*` constants instead of
    restated from the prose, so a state added in code without a fill fails HERE, not in a
    report nobody re-reads."""
    from agentflow import fleet_report as fr

    # Shape, not name: a lane state is an ALL-CAPS token, which excludes `LANE_BASIS`
    # ("its Queue lane") without hard-coding an exception that a later constant escapes.
    states = {
        value
        for name, value in vars(fr).items()
        if name.startswith("LANE_")
        and isinstance(value, str)
        and value.replace("-", "").isupper()
    }
    text = _report_skill()
    classes = set(re.findall(r"classDef (\w+) fill:", text))

    assert len(states) == 7, f"the collector's lane states moved: {sorted(states)}"
    assert classes == {
        "live", "halted", "reapedfresh", "reapedold",
        "silent", "dry", "blocked", "stooddown",
    }, f"the lane diagram's fills no longer cover the seven states: {sorted(classes)}"
    assert "Seven lane states, eight classes" in text, (
        "the prose no longer states the count the two sets above agree on, so a future "
        "editor has no way to notice the diagram has fallen behind the collector"
    )
    # The sharp one: a node tagged with a class nobody defines is the original bug, and it
    # is what a half-revert leaves behind — the example nodes keep their `:::dry` while the
    # deleted classDef takes the fill with it.
    used = set(re.findall(r":::(\w+)", text))
    assert used <= classes, (
        f"a lane node is tagged with an undefined class: {sorted(used - classes)}"
    )
    # DRY must not be painted like a problem state. It is the one lane condition where
    # nothing is wrong, and the reason the fill was chosen cool has to survive an editor
    # who sees an unfamiliar state and reaches for red.
    i = text.index("Seven lane states, eight classes")
    assert "nothing is wrong with a dry lane" in text[i:i + 600], (
        "the rationale for the DRY fill is gone, so the next repaint has nothing to read"
    )


def test_the_report_skill_locks_EIGHT_sections_with_DONE_at_five() -> None:
    """The lock and the collector must agree, or the artifact and the vault page can
    carry different sections - the exact drift `report.sections()` exists to stop."""
    from agentflow.fleet_report import SECTION_ORDER

    text = " ".join((SKILLS_ROOT / "report" / "SKILL.md").read_text(encoding="utf-8").split())
    assert "5. **DONE**" in text
    assert "8. **COUNTED / SKIPPED**" in text
    assert "seven sections" not in text, "a stale seven-section reference survives"
    assert len(SECTION_ORDER) == 8
    assert SECTION_ORDER[4] == "done"


def test_the_report_skill_explains_why_DONE_and_SHIPPED_can_disagree() -> None:
    """Two true numbers that differ read as a bug unless the page says why. The note
    is `report.done.source_note`, and the skill has to say to render it."""
    text = " ".join((SKILLS_ROOT / "report" / "SKILL.md").read_text(encoding="utf-8").split())
    assert "source_note" in text
    i = text.index("DIFFERENT READINGS")
    assert "snapshot diff" in text[i:i + 700]
    assert "merged pull requests" in text[i:i + 700]


# --------------------------------------------------------------------------- #
# epic 706 — the update path is store-primary. These pin the CONVERSION, not the
# prose around it: the marker stop is gone BECAUSE the path no longer edits
# markdown, so a revert that reinstates the editing must fail here rather than
# be discovered by a render silently undoing somebody's work.
# --------------------------------------------------------------------------- #
def _strip_shell_comments(text: str) -> str:
    """Fenced bash with its comments removed -- what the block actually RUNS.

    Every positional assertion over a runbook wants this and not the raw text. A comment can
    contain any command, any condition, any path, so an assertion keyed on a substring is
    satisfiable by a line that never executes -- and that is not hypothetical here: E-0743
    shipped two such assertions in one change, one caught by mutation and one by review, both
    keyed on strings that also appeared in comments explaining the very defect being gated.

    Quote-aware, because a `#` inside a string is not a comment and dropping the rest of that
    line would delete real commands. Newlines are preserved so every caller's ``re.M`` anchors
    and index comparisons keep meaning the same thing.
    """
    out = []
    for line in text.split("\n"):
        q = None
        cut = None
        for i, ch in enumerate(line):
            if q:
                if ch == q:
                    q = None
            elif ch in "'" + chr(34):
                q = ch
            elif ch == "#" and (i == 0 or line[i - 1].isspace()):
                cut = i
                break
        out.append(line if cut is None else line[:cut].rstrip())
    return "\n".join(out)


def _fenced_bash_blocks(text: str) -> list:
    """The ```bash blocks of a SKILL.md -- the parts a reader actually RUNS.

    A gate over the whole markdown document cannot tell a command from a sentence about a
    command, and these files discuss shell in prose constantly: this module's own subject,
    autodoc-update/SKILL.md, carries commentary comparing how a sibling skill assigns the
    very variables gated below. Scoping to fenced blocks is what makes "assigned" mean
    assigned.
    """
    out, buf, inside = [], [], False
    for line in text.split("\n"):
        stripped = line.strip()
        fence = stripped.startswith("```")
        if not inside and fence and stripped[3:].strip().lower() in ("bash", "sh", "shell"):
            inside, buf = True, []
            continue
        if inside and fence:
            out.append("\n".join(buf))
            inside = False
            continue
        if inside:
            buf.append(line)
    return out


def test_autodoc_update_no_longer_hard_stops_on_a_rendered_tree() -> None:
    text = (SKILLS_ROOT / "autodoc-update" / "SKILL.md").read_text(encoding="utf-8")

    # The 704 interim guard is retired. Its own words are the anchor: if they come
    # back, the conversion has been reverted and this says so by name.
    assert "not yet record-converted" not in text, (
        "the 704 marker stop is back - it exists only while the update path edits "
        "markdown, so its return means the record conversion was reverted"
    )
    assert "STOP: $AUTODOC is rendered" not in text
    # It is replaced by a BRANCH, not by silence: a pre-cutover tree still has to
    # be refused, because the records the agents emit have nowhere to land.
    assert "STORE_PRIMARY" in text
    assert ".rendered-by-store" in text, "Step 0 must still detect which tree this is"


def test_autodoc_update_lands_its_records_and_renders_to_a_separate_target() -> None:
    """The conversion is only real if the records actually reach the store."""
    text = (SKILLS_ROOT / "autodoc-update" / "SKILL.md").read_text(encoding="utf-8")
    # Comments stripped: every assertion below is positional over what the block RUNS, and a
    # comment can quote any command or condition it likes. Two assertions in this very test
    # were satisfiable by comments before this was added (E-0743).
    shell = _strip_shell_comments("\n".join(_fenced_bash_blocks(text)))

    for cmd in ("cli records", "cli reports-stale", "cli render", "cli check"):
        assert cmd in text, "Step 4.5 never runs " + cmd + "; the records never land"
    # Parse the COMMAND, not the page. An earlier version of this assertion was
    # `"--manifest" in text`, and a mutation that deleted the flag from the actual
    # invocation left the sentence EXPLAINING the flag in place -- so the gate
    # passed on its own documentation. Scope to the `cli records` call itself.
    # Same anchoring rule as the `cli render` window below: search the comment-stripped shell, so
    # a comment that happens to name the command cannot move the window off the invocation.
    i = shell.index("cli records")
    invocation = shell[i:i + 400]
    assert "--manifest" in invocation, (
        "the `cli records` invocation drops --manifest, so a stale *.json from a "
        "prior attempt is silently swept into the store: " + invocation[:200]
    )
    # The autodoc root must be RESOLVED per project, never read bare off the environment:
    # environment.autodoc_root is machine-level, so a bare read renders one project into
    # another project's corpus on any box running more than one (gh#282).
    # The CALL, not the name: an import line carries the name too, so `"resolve_autodoc_root"
    # in shell` stays green when the call is swapped for a bare `e.autodoc_root` read -- which
    # is the exact gh#282 regression this is here to stop. Same shape as the two gates above:
    # a mention is not an invocation, a reference is not an assignment, a name is not a call.
    assert re.search(r"resolve_autodoc_root" + chr(92) + r"s*" + chr(92) + r"(", shell), (
        "autodoc-update never CALLS resolve_autodoc_root, so a project carrying its own "
        "autodoc_root override is rendered into whatever the machine-level value names "
        "(gh#282)"
    )
    # THE ACT, not the name. `"AUTODOC_PROJECT" in shell` was green against a fully reverted
    # fix: the name also appears in a comment and in the FATAL's own remedy text, both inside
    # fenced blocks. Anchor on the assignment that reads it.
    assert re.search(
        r"^\s*explicit\s*=\s*os\.environ\.get\(\"AUTODOC_PROJECT\"\)", shell, re.M
    ), (
        "autodoc-update never reads AUTODOC_PROJECT, so its project comes from the session "
        "default alone and a run inside a lane defaulted elsewhere refreshes that project"
    )

    # Every value the config block emits is guarded after it. `eval` returns the status of the
    # string it EVALUATES, not of the substitution, so a SystemExit inside the python prints
    # nothing and leaves eval exiting 0 with all of them unbound. $PROJECT unguarded makes
    # "never bound" and "genuinely different" the same outcome at the cross-check below.
    for var in ("AUTODOC", "SUITE_ROOT", "PROJECT"):
        assert re.search(r"^\s*\[ -n \"\$" + var + r"\" \] \|\| exit 1", shell, re.M), (
            "$" + var + " is emitted by the config block but never guarded, so an eval that "
            "printed nothing leaves it empty and the run continues on a blank value"
        )

    # THE ONE THAT ACTUALLY CLOSES THE INTENT ROUTE. Nothing inside the skill can tell which
    # corpus was MEANT: $AUTODOC is resolved FROM the name and the manifest is read FROM
    # $AUTODOC, so a wrong name selects a tree that agrees with it and every comparison between
    # them passes. Refusing to guess when the box holds more than one project is the only
    # detector there is.
    assert re.search(r"len\(installed\)\s*>\s*1", shell), (
        "autodoc-update guesses the project on a multi-project box instead of refusing; the "
        "session default is the SESSION's project, not the corpus's"
    )

    # The root-vs-name cross-check must exist AND must REFUSE. Asserting the comparison alone
    # stayed green when its body was downgraded from `exit 1` to an echo.
    guard = re.search(
        r"if\s*\[\s*\"\$project\"\s*!=\s*\"\$PROJECT\"(.*?)fi", shell, re.S
    )
    assert guard, (
        "nothing compares the manifest's own `project` against the resolved one, so a root that "
        "does not belong to the resolved name is refreshed silently"
    )
    assert re.search(r"exit\s+1", guard.group(1)), (
        "the corpus mismatch is reported but not refused: the run proceeds into the wrong tree"
    )

    # ...and the value it emits as AUTODOC must be the resolved one.
    assert re.search(r"^" + chr(92) + r"s*root" + chr(92) + r"s*=" + chr(92) + r"s*resolve_autodoc_root" + chr(92) + r"(", shell, re.M), (
        "AUTODOC is emitted from something other than the resolver result (gh#282)"
    )
    assert not re.search(r"^" + chr(92) + r"s*root" + chr(92) + r"s*=" + chr(92) + r"s*e" + chr(92) + r".autodoc_root", shell, re.M), (
        "the bare environment read is back (gh#282)"
    )

    # Q-10013: never force into the live tree.
    #
    # ASSIGNED, not merely mentioned. `assert "RENDER_TARGET" in text` passed against a
    # SKILL.md where the name appeared four times and was defined nowhere, so `--out ""`
    # shipped and this gate -- written for exactly that safety property -- reported green
    # because of the defect. A substring cannot tell a definition from a dangling reference.
    # Scoped to fenced bash blocks: `^\s*VAR=` against the whole document also matches a line
    # of narrative prose that happens to start that way, and this file's own commentary
    # discusses how the sibling skill assigns these. And ASSIGNED BEFORE USED, because the
    # defect being replaced was use-before-definition -- an assignment moved below Step 4.5
    # expands to the empty string at every use site exactly as the missing one did.
    # COMM_PINS joins them for the reason the tuple form is a hazard: this loop is the only
    # thing in the suite that judges use-before-assignment in this file, and it judges
    # exactly the names typed into it. COMM_PINS was consumed by the `communities` call and
    # assigned nowhere, three lines below the two names that WERE listed, for the whole life
    # of the fix -- found by running the path, not by the gate written to cover it (E-0743).
    for var in ("RENDER_TARGET", "REPO_STEM", "COMM_PINS"):
        assigned = re.search(r"^\s*" + var + r"=", shell, re.M)
        assert assigned, (
            var + " is referenced but never assigned in a runnable block of "
            "autodoc-update/SKILL.md: every use expands to the empty string, so the render "
            "lands in the wrong place or nowhere"
        )
        first_use = re.search(r"\$\{?" + var + r"\b", shell)
        assert first_use and assigned.start() <= first_use.start(), (
            var + " is used before it is assigned; an assignment below the use is worth "
            "exactly as much as no assignment at all"
        )
    # $AUTODOC has the same defect shape and cannot be checked the same way: it is emitted
    # from python through `eval`, so there is no `^AUTODOC=` line for the loop above to find.
    # Key on its first CONSUMER instead. Step 0 tested `-f "$AUTODOC/.rendered-by-store"`
    # while sitting ABOVE the block that binds $AUTODOC, so the condition read
    # `-f "/.rendered-by-store"`, was false on every machine, and routed every store-primary
    # tree down the "run /autodoc-init first" branch. Measured on the first end-to-end run
    # (E-0743): the corpus carried the marker and the in-order reading still said 0.
    bind = re.search(r'^\s*\[ -n "\$AUTODOC" \] \|\| exit 1', shell, re.M)
    assert bind, "the $AUTODOC bind guard is gone; nothing pins where AUTODOC becomes usable"
    # The CONDITION, not the bare filename, and EVERY occurrence, not the first. The bare
    # string appears twice in fenced bash -- the test, and the comment above it explaining
    # this very defect -- so a `find()` on it is satisfied by the comment after the test is
    # deleted, and a mutation proved exactly that before this was rewritten.
    marks = [m.start() for m in re.finditer(
        r'\[ -f "\$AUTODOC/\.rendered-by-store" \]', shell)]
    assert marks, (
        "Step 0's store-primary test is gone from every runnable block; nothing decides "
        "whether this tree has a store behind it"
    )
    assert min(marks) > bind.start(), (
        "the .rendered-by-store test runs before $AUTODOC is bound, so it tests "
        "'/.rendered-by-store', is false on every machine, and sends every store-primary "
        "tree down the /autodoc-init branch"
    )
    assert "_RESERVED_BASENAMES" in text, (
        "the reason force is forbidden must be stated as the mechanism, not a file count"
    )
    assert "NEVER `--force` INTO" in text
    # The heading above is prose the same commit wrote; this is the property it stands for.
    #
    # ANCHOR IN `shell`, NOT `text`. This read `text.index("cli render")` and took the first
    # TEXTUAL occurrence, so the day a comment mentioned `cli render` above the invocation the
    # 200-char window landed on prose and the assertion failed against a runbook that was
    # correct. That is this test's own stated rule -- "a mention is not an invocation" -- turned
    # on the test itself, and it fails in the direction that wastes a reader's afternoon rather
    # than the direction that ships a bug. `shell` already has the comments stripped.
    render = shell[shell.index("cli render"):][:200]
    assert "--force" not in render, (
        "the render invocation carries --force, which unlinks every .md in the target: "
        + render[:120]
    )
    assert '--out "$RENDER_TARGET"' in render, (
        "the render must target the separate directory, not $AUTODOC: " + render[:120]
    )
    # The renderer refuses a non-empty --out without force, and force is prohibited here, so a
    # leftover render target fails the step exactly as the unassigned path did. Added with the
    # assignments (p245) and pinned here in the same commit: a safety step with no gate is the
    # defect this test exists to catch, and leaving one behind while fixing another would be
    # the same mistake one line over.
    # An INVOCATION, on the right directory, in a runnable block -- not a mention. Asserting
    # the bare name would be the same vacuous-substring shape this test condemns four lines
    # above: one prose sentence naming the script would satisfy it with no call in the file.
    guard = re.search(
        r"^\s*node\s+\S*assert-no-agent-writes\.js\"?\s+\"\$RENDER_TARGET\"", shell, re.M
    )
    assert guard, (
        "the update path never INVOKES assert-no-agent-writes.js on $RENDER_TARGET before "
        "rendering; the renderer refuses a non-empty target and force is prohibited here"
    )
    render_at = shell.index("cli render")
    assert guard.start() < render_at, (
        "the empty-target assertion runs AFTER the render, which checks nothing"
    )
    # ...and something must EMPTY that directory, or the assertion above stops every run
    # after the first on the previous run's own output.
    assert re.search(r"^\s*rm -rf \"\$RENDER_TARGET\"", shell, re.M), (
        "nothing ever removes $RENDER_TARGET, so a successful run leaves a full page tree "
        "that hard-stops the next one at the empty-target assertion"
    )

    # Step 3's changedCommunities assembler (E-0743). The workflow hard-fails without this
    # argument, and until this call site existed the step that produces it was prose -- so the
    # one argument the run cannot proceed without came from no command in the file.
    build = re.search(r'^\s*python .*build-changed-communities\.py', shell, re.M)
    assert build, (
        "Step 3 never INVOKES build-changed-communities.py, so changedCommunities is produced "
        "by no command and the workflow hard-fails on the missing argument"
    )
    # ORDER: the communities artifact it reads is rebuilt by `cli communities` in Step 3.5.
    # Running the assembler first groups this diff against the PREVIOUS run's communities --
    # a wrong answer that looks exactly like a right one.
    regroup = shell.index("cli communities")
    assert regroup < build.start(), (
        "the changedCommunities assembler runs before `cli communities` regenerates the "
        "artifact, so it groups this diff against the previous run's communities"
    )
    # ...and an UPPER bound, which the first version of this assertion omitted. Bounding only the
    # lower end lets the block be moved past the step that CONSUMES its output and stay green:
    # Step 4 reads /tmp/autodoc-changed-communities.json, so the assembler must precede it.
    consumer = shell.find("autodoc-changed-communities.json", build.end())
    assert consumer == -1 or consumer > build.start(), (
        "the assembler runs after the step that reads its output file"
    )
    # The assembler's INPUT must be produced by a command too. Step 2 printed the changed list
    # to stdout while a later step required it as a file -- the same prose-instead-of-command
    # defect, one step upstream of the one this assembler fixes, and introduced by that fix.
    produced = shell.find("> /tmp/autodoc-changed.txt")
    assert produced != -1, (
        "no fenced block writes /tmp/autodoc-changed.txt, so the assembler's --changed input is "
        "produced by no command and a literal run has nothing to read"
    )
    assert produced < build.start(), (
        "the changed list is written after the assembler reads it"
    )
    # The deleted list likewise. Written and never read, it left every page citing only deleted
    # files outside every entry, so no agent deprecated it and the golden gate failed on it.
    produced_deleted = shell.find("> /tmp/autodoc-deleted.txt")
    assert produced_deleted != -1 and produced_deleted < build.start(), (
        "no fenced block writes /tmp/autodoc-deleted.txt before the assembler reads it"
    )
    assert re.search(r"--deleted\s+/tmp/autodoc-deleted\.txt",
                     shell[build.start():build.start() + 400]), (
        "the assembler is not handed Step 2's deleted list, so pages of deleted files reach "
        "no refresh agent and a deletion-only range is refused as an empty diff"
    )
    records_at = shell.index("cli records")
    assert build.start() < records_at, (
        "the changedCommunities assembler runs after `cli records`, which is downstream of the "
        "workflow that consumes it -- the argument would not exist when the workflow launched"
    )
    for flag in ("--communities", "--manifest", "--changed", "--deleted", "--out"):
        assert flag in shell[build.start():build.start() + 400], (
            "the assembler invocation omits " + flag + "; every argument is required and "
            "defaulting any of them reads another run's artifacts"
        )
    # The golden gate (E-0743, discharging p245 item 4). An INVOCATION in a runnable block, for
    # the same reason the assert-no-agent-writes check above demands one: this section previously
    # held four paragraphs ABOUT the gate and no call, and that is what "promised, never wired"
    # looks like from inside the file that promises it.
    gate = re.search(r"^\s*node\s+\S*golden-gate\.js", shell, re.M)
    assert gate, (
        "the update path never INVOKES golden-gate.js; the record has promised this gate since "
        "the record-conversion epic and prose naming the script is not a call site"
    )
    # Ordering is the whole content of the assertion. After the render, or there is nothing
    # rendered to judge; before the copy-back, or $RENDER_TARGET and $AUTODOC are the same tree
    # and the gate compares a thing to itself and always passes.
    assert render_at < gate.start(), (
        "the golden gate runs before the render, so it judges an empty or stale target"
    )
    # THE COPY-BACK, not the rm. These are different landmarks and only one of them is the
    # property: once the render has been copied over the live tree, the gate's two arguments hold
    # the same content and its comparative assertions -- type-match, the structural half of
    # frontmatter, link-recall, blocker-parity -- are tautologically green. Anchoring on the rm
    # leaves the whole span between the copy and the rm unguarded, and a review mutation that
    # moved the gate into exactly that span passed the suite (E-0743). The copy-back had no
    # command at all until then, which is why there was nothing better to anchor on.
    copy_back = re.search(r"^\s*cp -a \"\$RENDER_TARGET/\.\" \"\$AUTODOC/\"", shell, re.M)
    assert copy_back, (
        "the copy-back has no runnable command, so the rendered tree never reaches $AUTODOC and "
        "the gate above has no landmark to be ordered against"
    )
    assert gate.start() < copy_back.start(), (
        "the golden gate runs after the rendered tree has been copied over the live one, so its "
        "baseline and its subject hold the same content and its comparisons cannot fail"
    )
    rm_at = re.search(r"^\s*rm -rf \"\$RENDER_TARGET\"", shell, re.M)
    assert copy_back.start() < rm_at.start(), (
        "$RENDER_TARGET is removed before it is copied back, which deletes the only copy of the "
        "render"
    )


def _autodoc_init_preamble() -> str:
    """autodoc-init's config preamble, comments stripped: the one block that writes the pointer."""
    text = (SKILLS_ROOT / "autodoc-init" / "SKILL.md").read_text(encoding="utf-8")
    blocks = [b for b in _fenced_bash_blocks(text)
              if re.search(r"^printf '%s\\n' \"\$AUTODOC_ENV\" > /tmp/autodoc-env\.current", b, re.M)]
    assert len(blocks) == 1, f"expected one autodoc-init preamble block, got {len(blocks)}"
    return _strip_shell_comments(blocks[0])


def test_autodoc_init_resolves_its_root_per_project_like_autodoc_update() -> None:
    """gh#393: init builds the corpus, so it must land where every reader of it will look.

    The anchors are the ACTS, the same shape as the autodoc-update gate above: an import line or a
    FATAL's remedy text carries every one of these names, so a name check stays green against a
    reverted fix.
    """
    shell = _autodoc_init_preamble()
    assert re.search(r"^\s*root\s*=\s*resolve_autodoc_root\s*\(\s*e\s*,\s*proj\s*\)", shell, re.M), (
        "autodoc-init never assigns its root from resolve_autodoc_root(e, proj), so a project's own "
        "autodoc_root override is invisible and the corpus lands at the machine-level root"
    )
    assert re.search(r"emit\(\s*\"AUTODOC\"\s*,\s*root\s*\)", shell), (
        "AUTODOC is emitted from something other than the resolver's result"
    )
    bare = re.findall(r"\b[A-Za-z_]\w*\s*\.\s*autodoc_root\b", shell)
    assert not bare, f"bare attribute read of autodoc_root in autodoc-init's preamble: {bare}"
    assert re.search(r"^\s*proj\s*=\s*load_project\(\s*name\s*\)", shell, re.M), (
        "the project object handed to the resolver is not loaded from the resolved name"
    )
    assert re.search(r"^\s*name\s*=\s*explicit\s+or\s+resolve_project_name\(\)", shell, re.M), (
        "autodoc-init does not resolve its project the way autodoc-update does"
    )
    # ONE NAME FOR ONE THING: autodoc-update takes the corpus's project as AUTODOC_PROJECT, so
    # init reads that, and $PROJECT is only ever the resolved value the block emits.
    assert re.search(
        r"^\s*explicit\s*=\s*os\.environ\.get\(\"AUTODOC_PROJECT\"\)", shell, re.M
    ), "autodoc-init does not take the project from AUTODOC_PROJECT, the name autodoc-update reads"
    assert not re.search(r"\$\{PROJECT:\?", shell), (
        "autodoc-init still takes its project from a caller-set $PROJECT: two names for one input"
    )
    assert re.search(r"emit\(\s*\"PROJECT\"\s*,\s*name\s*\)", shell), (
        "$PROJECT is not emitted from the resolved name, so the manifest's project can disagree "
        "with the root it was resolved for"
    )
    assert re.search(r"len\(installed\)\s*>\s*1", shell), (
        "autodoc-init guesses the project on a multi-project box instead of refusing"
    )


def test_autodoc_update_coverage_report_is_written_by_a_block_not_by_the_operator() -> None:
    """gh#435 — Step 6.5's coverage report must be PRODUCED, not pasted.

    Sibling of the test above, and the same defect one argument over. Step 4.4 exists because
    `--manifest`'s `recordsFiles` list "was produced by no step"; `coverage` had the identical
    shape and that fix did not reach it. Step 6.5 opened with a `cat > ... <<'COV'` heredoc
    asking for the Step 4 workflow return verbatim — a value that by then lived only in a tool
    result hours and one gate upstream. Measured on the first run to reach the step: it was
    unrecoverable, and the only coverage file on disk predated the run's own carrier by 100
    minutes, so the freshness check correctly refused it and the gate could not be run at all.

    Anchored in the comment-stripped shell throughout, per this module's own rule: the page now
    carries several comments about the heredoc that was removed, and every one of them satisfies
    a substring check on the raw text.
    """
    cov = "/tmp/autodoc-update-coverage.json"
    text = (SKILLS_ROOT / "autodoc-update" / "SKILL.md").read_text(encoding="utf-8")
    blocks = [_strip_shell_comments(b) for b in _fenced_bash_blocks(text)]

    # THE PROPERTY. No runnable line may CREATE this file by redirection — that is what a paste
    # looks like in a runbook, and a heredoc is a redirection. A block that writes it from the
    # records passes the path to python as an exported env var and never redirects into it.
    paste = re.search(r">>?\s*" + re.escape(cov), "\n".join(blocks))
    assert not paste, (
        "Step 6.5 takes the coverage report as a hand-written paste again; nothing in the runbook "
        "produces the workflow's `coverage` object, and by that step the return is gone (gh#435)"
    )

    # ...and it must be produced, or the gate below simply has no input. The producer is the
    # block that already writes the records manifest: same records, same guards, one step.
    producing = [i for i, b in enumerate(blocks) if "/tmp/autodoc-records-files.json" in b]
    assert producing, (
        "no block writes the records manifest any more, so there is nothing for the coverage "
        "report to be assembled alongside"
    )
    producer = blocks[producing[0]]
    assert cov in producer, (
        "Step 4.4 writes the records manifest but not the coverage report, so Step 6.5's input is "
        "produced by no step — the gh#435 defect, which is the gh#18 defect one argument over"
    )
    # A write with no guard is the failure mode this whole step exists for: the python emits the
    # manifest first, so a failure between the two leaves a good manifest and no report, and the
    # gap would not surface until Step 6.5, four steps and a 24-agent fan-out later.
    assert re.search(r"^\s*\[ -s " + re.escape(cov) + r" \]", producer, re.M), (
        "nothing checks that the coverage report was actually written; an empty or absent file "
        "reaches Step 6.5 as a traceback naming /tmp rather than the step that owes it"
    )

    # ORDER. Written before the gate reads it — and the gate is `checkCoverage`, the node call,
    # not the step heading, which is prose and could be moved without moving anything runnable.
    consuming = [i for i, b in enumerate(blocks) if "checkCoverage" in b]
    assert consuming, "the coverage gate never CALLS checkCoverage; there is nothing left to gate"
    assert producing[0] < consuming[0], (
        "the coverage report is written after the gate that reads it"
    )
    consumer = blocks[consuming[0]]

    # THE ONE THAT BITES ON THE OBVIOUS FIX. Step 6.5 used to `rm -f` both of its inputs before
    # re-creating them, which was right while it wrote them both. Now that Step 4.4 owns this
    # one, the same rm deletes the gate's own input four steps after the only step that can
    # produce it — and the failure reads as "Step 4.4 wrote nothing", which it did not.
    assert not re.search(r"^\s*rm\b[^\n]*" + re.escape(cov), consumer, re.M), (
        "the coverage gate deletes the report Step 4.4 wrote for it, so the run dies on an input "
        "that was present and correct until this line ran"
    )

    # ...and neither check that dates the report to this run may be traded away for the above.
    # The file is still newer than the carrier, so freshness stays meaningful, and these two are
    # what catch a Step 4.4 that ran but wrote an empty or partial report.
    # THE COMPARISON AND ITS SUBJECT, not the words. `"os.path.getmtime" in consumer` survived a
    # mutation that downgraded the condition to `if False:` — the name still appears on the line
    # that computes `born`, and the FATAL text still names the defect the test was written for.
    fresh = re.search(
        r"for path in \(([^)]*)\):\s*\n\s*if os\.path\.getmtime\(path\) < born:\s*\n\s*raise SystemExit",
        consumer,
    )
    assert fresh, (
        "the freshness check is gone from the coverage gate; a report from an earlier run is "
        "indistinguishable from this one's at a fixed /tmp path, which is exactly what happened "
        "on the first run to reach this step (gh#435)"
    )
    assert "update_cov" in fresh.group(1), (
        "the freshness loop no longer covers the coverage report, so only the inventory slice is "
        "dated to this run: " + fresh.group(1)
    )
    assert re.search(
        r'for k in \("pages", "covered", "excluded"\):', consumer
    ), (
        "the three-key check is gone; a report missing `covered` gates every changed symbol "
        "against nothing and passes"
    )


def _continued_command(block: str, start: int) -> str:
    """The one backslash-continued command beginning at *start*, and nothing after it.

    A fixed-width window past the match is not the invocation: it runs off the end of the command
    into whatever follows, so a flag DELETED from the call still reads as passed if the word turns
    up on any nearby line. The runbook writes this particular call across four continued lines, so
    the continuation has to be followed rather than guessed at by character count.
    """
    lines = block[start:].split("\n")
    out = [lines[0]]
    while out[-1].rstrip().endswith(chr(92)) and len(out) < len(lines):
        out.append(lines[len(out)])
    return "\n".join(out)


def test_the_changed_communities_call_delivers_its_checklists_by_path() -> None:
    """--checklist-dir must be ON THE CALL. This is the feature's only call site.

    The flag is what moves 4636 symbols out of the refresh agents' prompt text and onto disk, and
    it is optional in the script by design -- so deleting it from this invocation leaves a fully
    green suite and a catch-up run that silently rebuilds the 535KB payload the change exists to
    avoid. A gate keyed on the WORD `--checklist-dir` anywhere in the file is a spell-check, not a
    gate: this page carries several paragraphs explaining the flag, and each of them satisfies a
    substring check on its own. Three assertions in this very module were that shape before
    mutation caught them. So: parse the command, not the page.
    """
    text = (SKILLS_ROOT / "autodoc-update" / "SKILL.md").read_text(encoding="utf-8")
    # Per BLOCK, not over the concatenation: the property below is that the path depends on
    # nothing set in another block, and only a per-block view can tell the difference.
    call = re.compile(r"^\s*python .*build-changed-communities\.py", re.M)
    hosting = [b for b in (_strip_shell_comments(b) for b in _fenced_bash_blocks(text))
               if call.search(b)]
    assert hosting, (
        "no runnable block INVOKES build-changed-communities.py; the checklist gate below has no "
        "call site to judge and Step 3.5 produces changedCommunities by no command at all"
    )
    for block in hosting:
        build = call.search(block)
        invocation = _continued_command(block, build.start())
        flag = re.search(r"--checklist-dir\s+(\S+)", invocation)
        assert flag, (
            "the build-changed-communities.py invocation does not carry --checklist-dir, so every "
            "unit's symbols ride the workflow args as prompt text again -- 535KB against a 512KB "
            "cap on the pending catch-up run. The flag is optional in the script, so nothing else "
            "in the suite notices: " + invocation[:200]
        )
        value = flag.group(1).strip()
        assert value.strip("\"'"), (
            "--checklist-dir is passed an empty literal, which the script refuses outright: "
            + invocation[:200]
        )
        # Every variable the path expands must be assigned ABOVE IT IN THIS SAME BLOCK. Each
        # fenced block runs as its own shell, so a value set in another one expands to nothing
        # here and the checklists land at the filesystem root while the run reports success --
        # which is exactly what reaching for Step 4's $RUN_DIR did.
        for name in sorted(set(re.findall(r"[$]\{?([A-Za-z_][A-Za-z0-9_]*)", value))):
            assigned = re.search(r"^\s*" + name + r"=", block, re.M)
            assert assigned and assigned.start() < build.start(), (
                "--checklist-dir expands $" + name + ", which is not assigned above it in the "
                "same fenced block; blocks do not share a shell, so it expands to the empty "
                "string and the checklists are written outside the directory the run names"
            )
    # ...and the block that DOES own $RUN_DIR mints it outright. `${RUN_DIR:-...}` only makes
    # sense if a value crossed a block boundary, which is the belief this fix removed: the
    # default fires every time, so the keep-what-is-set half is dead code that reads as live.
    for block in (_strip_shell_comments(b) for b in _fenced_bash_blocks(text)):
        assert "${RUN_DIR:-" not in block, (
            "a block mints RUN_DIR with a keep-what-is-already-set default; nothing sets it in "
            "another block, because another block is another shell"
        )


def test_autodoc_update_forbids_hand_patching_the_rendered_tree() -> None:
    text = (SKILLS_ROOT / "autodoc-update" / "SKILL.md").read_text(encoding="utf-8")
    assert "Don't hand-patch the rendered tree" in text
    assert "runDir" in text, "the workflow needs a records directory or agents have nowhere to write"


# --------------------------------------------------------------------------- #
# gh#619 — autodoc-init's run carrier. Every fenced block is its own shell, and init minted
# its variables in one fence and read them in later ones, so Step 4 ran `graph records` and the
# no-agent-writes gate with every value empty after the fan-out had been paid for. The fix is
# update's carrier; these pin it on what the fences RUN, derived from the fences themselves
# rather than a hand-typed variable list (the tuple form is how COMM_PINS went unjudged above).
# --------------------------------------------------------------------------- #
_INIT_SKILL = SKILLS_ROOT / "autodoc-init" / "SKILL.md"
_CARRIER_SOURCE = '. "$(cat /tmp/autodoc-env.current)" || exit 1'
_POINTER_RM = "rm -f /tmp/autodoc-env.current"
_POINTER_WRITE = re.compile(r'^\s*printf .*> /tmp/autodoc-env\.current\s*$', re.M)
# Minted three ways in these fences: a plain assignment, and two `eval "$(python ...)"` shapes
# whose names only appear inside the python (emit("X", ...) and print("X=" ...)).
_MINT = (
    re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=", re.M),
    re.compile(r'emit\("([A-Z][A-Z0-9_]*)"'),
    re.compile(r'print\("([A-Z][A-Z0-9_]*)='),
)
_EXPAND = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)")
_ENV_PUT = re.compile(r"^\s*env_put\s+([A-Z][A-Z0-9_]*)\s", re.M)


def _init_fences() -> list[str]:
    return [_strip_shell_comments(b) for b in _fenced_bash_blocks(_INIT_SKILL.read_text(encoding="utf-8"))]


def _minted(block: str) -> dict[str, int]:
    """name -> offset of its first minting in this block."""
    out: dict[str, int] = {}
    for rx in _MINT:
        for m in rx.finditer(block):
            # `RUN_DIR="$RUN_DIR" python -` exports a value to one command; it READS the name.
            if block[m.end():].startswith('"$' + m.group(1) + '"'):
                continue
            out[m.group(1)] = min(out.get(m.group(1), m.start()), m.start())
    return out


def _cross_block_reads(fences: list[str]) -> dict[int, set[str]]:
    """fence index -> names it expands that another fence minted and it has not minted yet."""
    minted = [_minted(b) for b in fences]
    out: dict[int, set[str]] = {}
    for i, block in enumerate(fences):
        reads = set()
        for m in _EXPAND.finditer(block):
            name = m.group(1)
            own = minted[i].get(name)
            other = any(name in mm for j, mm in enumerate(minted) if j != i)
            if other and (own is None or own > m.start()):
                reads.add(name)
        if reads:
            out[i] = reads
    return out


def _preamble_index(fences: list[str]) -> int:
    hits = [i for i, b in enumerate(fences) if _POINTER_RM in b]
    assert len(hits) == 1, f"expected one preamble fence removing the carrier pointer, found {hits}"
    return hits[0]


def test_autodoc_init_every_fence_reading_a_cross_block_variable_sources_the_carrier() -> None:
    fences = _init_fences()
    pre = _preamble_index(fences)
    preamble = fences[pre]
    # The preamble's own shape: pointer removed first, written last, env_put braced.
    first = next(ln.strip() for ln in preamble.split("\n") if ln.strip())
    assert first == _POINTER_RM, "the preamble can fail before it removes the previous run's pointer"
    write = _POINTER_WRITE.search(preamble)
    assert write, "the preamble never writes the carrier pointer, so no later block can source it"
    assert not _ENV_PUT.search(preamble[write.end():]), "env_put after the pointer write"
    assert 'env_put() { printf \'%s=%q\\n\' "${1}" "${2}" >> "$AUTODOC_ENV"; }' in preamble
    text = _INIT_SKILL.read_text(encoding="utf-8")
    bare = re.findall(r"\$[0-9]|\$ARGUMENTS", text)
    assert not bare, f"bare positional(s) {bare}: the Skill tool rewrites them before bash runs (gh#502)"

    reads = _cross_block_reads(fences)
    assert len(reads) >= 10, f"only {len(reads)} fence(s) read a cross-block variable - the parse went blind"
    unsourced = {}
    for i, names in reads.items():
        assert i > pre, f"fence {i} reads {sorted(names)} before the preamble runs"
        opener = next((ln.strip() for ln in fences[i].split("\n") if ln.strip()), "")
        if opener != _CARRIER_SOURCE:
            unsourced[i] = sorted(names)
    assert not unsourced, (
        "fence(s) expand a variable another fence minted without first sourcing the carrier; "
        "another block is another shell, so these read empty: " + repr(unsourced)
    )


def test_autodoc_init_every_cross_block_variable_is_env_put_before_it_is_read() -> None:
    fences = _init_fences()
    put_at: dict[str, int] = {}
    for i, block in enumerate(fences):
        for m in _ENV_PUT.finditer(block):
            put_at.setdefault(m.group(1), i)
    missing = {}
    for i, names in _cross_block_reads(fences).items():
        late = sorted(n for n in names if put_at.get(n, len(fences)) >= i)
        if late:
            missing[i] = late
    assert not missing, (
        "fence(s) read a variable no EARLIER fence puts on the carrier, so sourcing it yields "
        "nothing for them: " + repr(missing)
    )
    # ...and within the fence that SETS it, the env_put comes after the set: an env_put above
    # `COMMIT=$(...)` carries the empty value and every reader downstream gets exactly that.
    early = {}
    for i, block in enumerate(fences):
        minted = _minted(block)
        for m in _ENV_PUT.finditer(block):
            set_at = minted.get(m.group(1))
            if set_at is not None and m.start() < set_at:
                early.setdefault(i, []).append(m.group(1))
    assert not early, "env_put runs before the variable is set, so it carries empty: " + repr(early)


def test_autodoc_init_every_workflow_arg_variable_is_on_the_carrier() -> None:
    """The Workflow args are substituted from the carrier, so every name they cite must be on it."""
    text = _INIT_SKILL.read_text(encoding="utf-8")
    block = re.search(r"^```\s*\nWorkflow\(\{\n(.*?)^```", text, re.S | re.M)
    assert block, "the Workflow args block is gone from autodoc-init/SKILL.md - re-anchor"
    names = set(_EXPAND.findall(block.group(1)))
    assert "RUN_DIR" in names and "VALIDATE_MERMAID" in names, sorted(names)
    put = {m.group(1) for b in _init_fences() for m in _ENV_PUT.finditer(b)}
    assert not names - put, (
        "the Workflow args cite variables the carrier never holds, so there is nothing to "
        "substitute them from: " + repr(sorted(names - put))
    )


def test_autodoc_init_every_manifest_path_is_written_by_a_command() -> None:
    fences = _init_fences()
    uses = [(i, m) for i, b in enumerate(fences) for m in re.finditer(r"--manifest\s+(\S+)", b)]
    assert uses, "autodoc-init no longer passes --manifest to `graph records`"
    for i, use in uses:
        path = use.group(1)
        before = "\n".join(fences[:i] + [fences[i][:use.start()]])
        write = re.search(r"(?:>>?)\s*" + re.escape(path) + r"(?:\s|$)", before)
        assert write, (
            "--manifest " + path + " is written by no command before it is read; cli.py opens it "
            "with a bare open(), so the run dies after the fan-out has been paid for"
        )
        # In the SAME fence as the read: a guard in an earlier fence ran in another shell, at
        # another time, and says nothing about the file when `graph records` opens it.
        guard = re.search(r"(?:test|\[) -s " + re.escape(path), fences[i][:use.start()])
        assert guard, "no `test -s` guard on " + path + " in the fence that passes it to --manifest"


# --------------------------------------------------------------------------- #
# gh#51 — .claude/worktrees/ must stay ignored.
#
# The harness creates a worktree per agent under .claude/worktrees/, and each
# carries a `.git` FILE rather than a directory. Without the ignore, `git add -A`
# in the repo root records them as GITLINKS (mode 160000) — bogus submodule
# entries no clone can resolve, in a repo slated to go public.
#
# Verified by reproduction when this landed: with a real worktree present,
# removing the ignore line made `git add -A` stage a 160000 entry, and restoring
# it made the count zero. This test is the cheap standing version of that.
# --------------------------------------------------------------------------- #
def test_claude_worktrees_stay_gitignored() -> None:
    repo_root = SKILLS_ROOT.parent
    ignore = (repo_root / ".gitignore").read_text(encoding="utf-8")

    assert ".claude/worktrees/" in ignore, (
        "`.claude/worktrees/` is not ignored — a `git add -A` in the repo root "
        "will record each harness worktree as a gitlink (mode 160000), which no "
        "clone can resolve"
    )
    # Scoped deliberately (gh#51): ignoring ALL of .claude/ would also work today
    # and would quietly remove the option of ever tracking .claude/settings.json
    # or a checked-in project skill. If that trade is made later it should be a
    # decision, not a side effect — so pin the narrow form.
    lines = [ln.strip() for ln in ignore.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert ".claude/" not in lines, (
        "the whole .claude/ directory is ignored; gh#51 scoped this to "
        ".claude/worktrees/ so a future .claude/settings.json stays trackable — "
        "widening it is a decision, not a tidy-up"
    )


# --------------------------------------------------------------------------- #
# Declared wake intervals vs the interval the harness will actually grant (gh#82)
# --------------------------------------------------------------------------- #
_DELAY_LITERAL = re.compile(r"delaySeconds=(\d+)")


def _declared_delays():
    """Every literal `delaySeconds=<int>` in a first-party skill, with its source."""
    for path in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        if "_vendored" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for n, line in enumerate(text.split("\n"), start=1):
            for m in _DELAY_LITERAL.finditer(line):
                yield path.relative_to(SKILLS_ROOT).as_posix(), n, int(m.group(1))


def test_no_skill_declares_a_wake_the_harness_will_silently_change():
    """THE gh#82 CLASS, and it is a false-record bug rather than a slow-loop bug.

    `ScheduleWakeup` clamps `delaySeconds` into `[60, 3600]` and reports nothing when it
    does. So a skill that writes a number outside that range still WAKES correctly - and
    then records an interval that never happened. The steward wrote `next_interval_min:
    180` while waking hourly; lead-dev's mid-epic flush said 30 seconds and got 60;
    the orchestrator's force-recheck said 1 second and got 60.

    Nothing in the runtime surfaces the disagreement, which is why it needs a gate here
    rather than a comment: the machine stays right and only the writing goes wrong, and
    that is the failure this fleet is least able to notice on its own.
    """
    from agentflow.config import WAKE_DELAY_MAX_S, WAKE_DELAY_MIN_S

    declared = list(_declared_delays())
    assert declared, "found no delaySeconds literals - the query, not the tree, is wrong"
    out_of_range = [
        (rel, line, value)
        for rel, line, value in declared
        if not WAKE_DELAY_MIN_S <= value <= WAKE_DELAY_MAX_S
    ]
    assert not out_of_range, (
        "these skills declare a wake the harness will silently change, so their own "
        f"records will be false: {out_of_range} (allowed {WAKE_DELAY_MIN_S}-{WAKE_DELAY_MAX_S}s)"
    )


def test_the_gate_can_actually_fail():
    """A positive control. The assertion above is a `not out_of_range` over a list built
    by a regex, and if the regex stopped matching it would pass on an empty list forever
    - green because it found nothing, which is the shape of every vacuous gate.
    """
    from agentflow.config import WAKE_DELAY_MAX_S, WAKE_DELAY_MIN_S

    sample = [("x/SKILL.md", 1, 30), ("y/SKILL.md", 2, 7200), ("z/SKILL.md", 3, 600)]
    caught = [r for r in sample if not WAKE_DELAY_MIN_S <= r[2] <= WAKE_DELAY_MAX_S]
    assert [r[2] for r in caught] == [30, 7200], "the range test does not catch both directions"


def test_f2_covers_the_stranded_subagent_the_sweep_itself_creates() -> None:
    """gh#363. F2 enumerated three categories of stranded process and missed the fourth, which is
    the one this seat produces: a subagent that never returns.

    THE ASSERTIONS ARE CONTIGUOUS DIRECTIONAL PHRASES, NOT TOKENS, and that is the whole design.
    A first version of this pin checked that "mtime", "UTC" and "30-minute" appeared somewhere in
    the section. Review rewrote the rule into its exact opposite -- "past the 30-minute mark a
    task is usually still working, so leave it running and say nothing" -- and every one of those
    assertions still passed. A token proves a word survived; only a phrase carrying a direction
    proves the RULE survived.

    Scoped to F2's own body, ending at the classifier sentence that closes it rather than at
    section G. The F2..G SPAN is not the F2 section: a new `**F3.**` heading inserted before G
    could hold the whole category while F2 went back to three, and the span-based slice called
    that green.

    Whitespace is normalised because these are prose phrases in a wrapped document, where a pure
    reflow must not be a test failure.
    """
    text = (SKILLS_ROOT / "steward" / "SKILL.md").read_text(encoding="utf-8")

    start = text.find("**F2. Host process hygiene.**")
    assert start != -1, "F2 is no longer titled 'Host process hygiene' - this gate anchors on it"
    end = text.find("On Windows classify with", start)
    assert end != -1, "F2's closing classifier sentence moved - re-anchor this gate"
    assert start < end, "F2's body no longer precedes its classifier line"
    f2 = " ".join(text[start:end].split())

    assert "stranded subagents" in f2, (
        "F2 lost the stranded-subagent category (gh#363); the other three probes classify OS "
        "processes by parent-liveness and cannot see a wedged task inside a live session"
    )

    # DIRECTION, not vocabulary. Each of these is a claim that reverses under negation.
    assert "the parent session is alive and healthy, and the CHILD is dead" in f2, (
        "the inversion is gone: every other F2 category kills a child whose PARENT died, and "
        "this one is the opposite - without that sentence the category reads as a duplicate"
    )
    assert "it is not working: report it, and stop it" in f2, (
        "the rule lost its direction. A version reading 'past the ceiling it is usually still "
        "working, leave it' satisfies every keyword this gate could check and instructs exactly "
        "the behaviour gh#363 was filed about"
    )
    assert "30-minute in-flight ceiling" in f2, "the threshold no longer reuses this skill's own"
    assert "Read the mtime in UTC" in f2, (
        "the UTC rule is gone or negated - the local-time reading of the 16.49h case was four "
        "hours off, and the wrong one looks recent"
    )

    # Context safety: the output path is a HARD LINK to the full transcript, so opening it to
    # check on a task pulls an entire conversation into the reader's context.
    assert "DO NOT READ THE OUTPUT FILE" in f2, "the context-safety rule is gone"
    assert "HARD LINK" in f2 and "symlink" not in f2.replace("on a symlink", ""), (
        "the link kind regressed to 'symlink'. It is measurably a hard link (same inode, "
        "nlink 2), and the probe works BECAUSE the two names share one mtime - on a symlink "
        "`stat` without -L would report the link's own mtime and track nothing"
    )

    # The Workflow sub-point carries the most drift-prone claim in the section (a filesystem
    # path) and was entirely deletable without any assertion noticing.
    assert "subagents/workflows" in f2, "the Workflow transcript path is gone"
    assert "display lag" in f2, (
        "the carve-out is gone: without it the new rule's most likely first action is stopping a "
        "workflow that had already finished"
    )
    assert "Stop the RUN, never the agent inside it" in f2, (
        "the workflow stop target inverted or vanished"
    )


def test_the_steward_no_longer_ladders_past_the_ceiling():
    """The issue's own subject. Four of five named lanes honoured the 2026-09-01 hourly
    directive; the steward's ladder climbed to ~6h, every rung above 60 minutes of it
    unreachable."""
    text = (SKILLS_ROOT / "steward" / "SKILL.md").read_text(encoding="utf-8")
    assert "cap ~6h" not in text, "the dead ladder rungs are still documented"
    assert "60 min" in text and "45 min" in text, "the surviving two rungs should still be stated"
# gh#105 -- a layout migration leaves readers behind, and its OWN file list misses them
# --------------------------------------------------------------------------- #
#: The three buckets the three-pillar migration relocated under `design/`. A first-party file
#: that still resolves one of them at the old place reads a path that no longer exists.
MOVED_BUCKETS = ("refine", "solution", "use-cases")

#: A path ROOTED at a data-root token and containing a bucket name as a path segment. Anchoring
#: on the root token is what separates a resolved path from vocabulary: this repo legitimately
#: says "was solution/" in a schema comment, calls a plan a "human-authored solution/ document",
#: and has a first-party skill DIRECTORY named `skills/refine/`. None of those are paths into the
#: data root, and a bare `refine/` grep flags all of them -- which is how a gate written the
#: obvious way becomes noise and then gets deleted.
#: The root tokens this tree actually spells. `project_dir` is here because the harness drivers
#: use it and were invisible without it -- the alternation, like the file list, was written from
#: the files already in hand. The lookbehind anchors `root` as its own token: unanchored, it
#: matched the TAIL of `<suite_root>/skills/refine/` and `<worktree_root>/.../skills/refine/`,
#: which are legitimate references to this repo's own skill DIRECTORY, not to a data bucket.
_ROOT_TOKENS = r"(?<![A-Za-z0-9_])(?:data_root|project_root|project_dir|root)"

_DATA_ROOT_PATH = re.compile(
    r"[<`(]?" + _ROOT_TOKENS + r"[>`]?[A-Za-z0-9_<>/.-]*"
    r"/(?:" + "|".join(MOVED_BUCKETS) + r")/"
)

#: A PYTHON PATH-JOIN -- a root variable, a slash operator, then the bucket as a quoted
#: string. Invisible to the path pattern above, whose
#: character class cannot cross the space and quote. This is how BOTH harness ship gates spelled
#: it, and both were red on master because of it. The `design` guard below is what keeps the
#: correct form -- the one that names design as its own path segment -- quiet.
_PY_JOIN = re.compile(r"""/\s*["'](?:""" + "|".join(MOVED_BUCKETS) + r""")["']""")

#: A SHELL GLOB -- a star, a slash, the bucket, a slash. No root token anywhere, so neither
#: pattern above sees it.
#: A live PostToolUse hook scoped itself this way and matched nothing after the move.
_SHELL_GLOB = re.compile(r"\*/(?:" + "|".join(MOVED_BUCKETS) + r")/")

#: Files that moved, matched WITHOUT needing a root token, because prose names them bare -- a
#: sentence saying which file holds the registry, with no data root in sight. A bare bucket grep
#: would be pure noise, but these filenames are distinctive enough to cost nothing. The
#: feature-suggestions file is the FOURTH relocation and the bucket list cannot express it at
#: all, because it moved out of a bucket that is still live.
_MOVED_FILES = tuple(re.compile(r) for r in (
    r"status/feature-suggestions\.md",
    r"use-cases/domains\.md",
    r"use-cases/registry\.jsonl",
))

#: A root token and a BARE bucket on the same line: a sentence that names the project root, then
#: says in prose which subdir something lives under. The bucket is not contiguous with the root
#: there -- English sits between them -- so the path pattern above cannot span it, and that shape
#: is real: it is how a docstring describes where the call on the next line writes. (The example
#: is deliberately not written out here; this module is inside its own sweep.) Scoped to same-line
#: co-occurrence rather than a bare bucket grep, because the bare form fires on every legitimate
#: use of these words as vocabulary ("a human-authored `solution/` document"), none of which
#: mentions a data root.
_ROOT_TOKEN = re.compile(r"[<`(]" + _ROOT_TOKENS[len(r"(?<![A-Za-z0-9_])"):] + r"[>`]")
_BARE_BUCKET = re.compile(r"`(?:" + "|".join(MOVED_BUCKETS) + r")/`")


#: Every first-party root, not just the one the gate was born in. `skills/` alone is what let
#: the harness drivers, `commands/`, `install/` and `templates/` through -- and two of those held
#: SHIP GATES that were red on master. The gate's own first version is the third instance in this
#: chain of an enumeration written from the files already in hand.
_FIRST_PARTY_ROOTS = ("skills", "harness", "commands", "install", "templates", "docs")

#: `.sh` matters and was missing: a live PostToolUse hook scoped itself to a deleted directory and
#: silently matched nothing.
_SCANNED_SUFFIXES = ("*.md", "*.py", "*.sh")

REPO_ROOT = SKILLS_ROOT.parent


def _rel(f: Path) -> str:
    try:
        return str(f.relative_to(REPO_ROOT))
    except ValueError:  # pragma: no cover - defensive
        return str(f)


def _first_party_files() -> list[Path]:
    """Every first-party skill file, RECURSIVELY.

    Not `SKILLS_ROOT.glob("*/SKILL.md")`, which is what the rest of this module uses and what let
    gh#105 through: the stale reference that mattered least was in a SKILL.md, and the others were
    a directory deeper, a file type over, and three roots away.

    THE ROOTS AND SUFFIXES ARE STILL A LIST, and a list only guards what someone thought of. The
    first version of THIS function scanned `skills/` for `.md` and `.py` -- and missed a live
    `.sh` hook, two harness ship gates that were red on master, `commands/`, `install/` and
    `templates/`. That is the same failure as the migration it guards, one layer up, which is why
    the count guard below is not decoration: if a root is renamed away, the sweep goes quiet, and
    quiet reads exactly like clean.

    `_vendored` is excluded because it is re-vendored rather than patched, and its upstream
    documents carried these strings before this repo had a layout to migrate.
    """
    out: list[Path] = []
    for root_name in _FIRST_PARTY_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            # FAIL later via the count guard rather than skipping silently: a renamed root is
            # exactly how this sweep would go quiet while staying green.
            continue
        for suffix in _SCANNED_SUFFIXES:
            out.extend(
                f for f in root.glob("**/" + suffix)
                if "_vendored" not in f.parts
                and "__pycache__" not in f.parts
                and "node_modules" not in f.parts
                and "_scratch" not in f.parts       # harness scratch trees are generated
                and "records" not in f.parts        # machine-local recorded runs
                and "fixtures" not in f.parts       # deliberately-broken parse fixtures
                # A one-time migration script MUST name both sides -- it is the thing that
                # performs the move. `docs/runbooks/` holds records of operations already run,
                # not shipped code (the migration script says so on its own line 2), so a stale
                # bucket there is history rather than a dangling reference.
                and "runbooks" not in f.parts
            )
    return sorted(set(out))


def test_no_first_party_file_resolves_a_pre_migration_bucket() -> None:
    """The three-pillar migration moved `refine/`, `solution/` and `use-cases/` under `design/`.

    gh#105: the migration repointed exactly the files its spec's `## Files` section named, and
    that list was written by reading the loop skills. Everything else that resolves those paths
    kept pointing at buckets that no longer exist -- and `refine` FAILS OPEN on it, treating a
    missing `domains.md` as "the tree is empty" and then WRITING it back at the dead path,
    recreating a bucket outside the layout where nothing looks. Nothing errors.

    The issue itself named two such files. Sweeping for the class found five more; review of that
    sweep found twelve more still, in roots this gate's first version did not look at and in three
    spellings it could not see. Thirteen sites, three widenings, one bug.

    WHAT THIS GATE DOES NOT COVER, stated because a gate that implies more than it checks is
    worse than none: a bucket named bare in prose, with no root token and no distinctive
    filename -- "gap specs live in `refine/`". Catching that means flagging every bare `refine/`
    and `solution/`, and this repo legitimately writes both as vocabulary (a schema comment says
    "(was solution/)", a parser calls a file a "human-authored solution/ document", and there is
    a first-party skill DIRECTORY named skills/refine/). A gate that noisy gets deleted, which
    covers nothing at all. Such sites are found by reading, and one was, in this same change.
    """
    offenders: list[str] = []
    for f in _first_party_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.split(chr(10)), 1):
            for hit in _DATA_ROOT_PATH.findall(line):
                if "design/" not in hit:
                    offenders.append(f"{_rel(f)}:{n}: {hit}")
            if _ROOT_TOKEN.search(line):
                # Per-HIT, not per-line. Suppressing the whole line when `design/` appears
                # anywhere on it meant a line that got one path right hid a second one it got
                # wrong -- and a half-migrated line is the exact shape this whole change is about.
                for m in _BARE_BUCKET.finditer(line):
                    if not line[max(0, m.start() - 8):m.start()].endswith("design/"):
                        offenders.append(
                            f"{_rel(f)}:{n}: {m.group(0)} (bucket named near a root token)"
                        )
            for m in _PY_JOIN.finditer(line):
                # `/ "design" / "use-cases"` is the correct form; only the bucket lacking that
                # parent is stale. Checked on the text BEFORE the match, per hit.
                # Quote-agnostic: `/ 'design' / 'use-cases'` is as correct as the double-quoted
                # form, and checking only one style flagged this module's own fixture.
                if "design" not in line[max(0, m.start() - 24):m.start()]:
                    offenders.append(f"{_rel(f)}:{n}:{m.group(0).strip()} (python path-join)")
            for m in _SHELL_GLOB.finditer(line):
                offenders.append(f"{_rel(f)}:{n}: {m.group(0)} (shell glob)")
            for pat in _MOVED_FILES:
                for m in pat.finditer(line):
                    if not line[max(0, m.start() - 8):m.start()].endswith("design/"):
                        offenders.append(f"{_rel(f)}:{n}: {m.group(0)} (this file moved under design/)")

    assert not offenders, (
        "first-party files resolve a pre-migration bucket (they moved under design/):"
        + chr(10) + chr(10).join("  " + o for o in offenders)
    )


def test_the_bucket_gate_actually_scans_and_actually_fires(tmp_path) -> None:
    """Two ways the gate above passes while guarding nothing, both pinned here.

    A gate that scanned zero files reports the same clean as one that scanned everything, and a
    pattern that matches nothing reports the same clean as a tree with no offenders. Neither is
    visible from a green run.
    """
    scanned = _first_party_files()
    assert len(scanned) > 150, f"the sweep found only {len(scanned)} files -- it is not scanning"
    # Every widened root is actually reached. Named individually, because a single count stays
    # green when one root silently drops out.
    for root_name in _FIRST_PARTY_ROOTS:
        if (REPO_ROOT / root_name).is_dir():
            assert any(root_name in f.parts for f in scanned), f"{root_name}/ is not being scanned"
    assert any(f.suffix == ".sh" for f in scanned), ".sh is not being scanned"
    assert any(f.name == "SKILL.md" for f in scanned)
    # ...and files BELOW the SKILL.md layer, which is the whole reason it is recursive.
    assert any(f.name != "SKILL.md" and f.suffix == ".md" for f in scanned)
    assert any(f.suffix == ".py" for f in scanned)

    # The pattern fires on each real shape gh#105 found, and on none of the vocabulary this repo
    # legitimately uses. Fixture strings, so this stays true when the tree is clean.
    # Built by concatenation so this file does not itself contain the literals the sweep above
    # scans for -- the same move the CI sanitize gate makes on its own pattern. Excluding this
    # file from the sweep instead would blind the gate to the entire tests tree.
    root, proj = "<data_" + "root>", "/projects/<name>"
    for bad in (
        "`" + root + proj + "/use-" + "cases/domains.md`",
        "output under `" + root + "/solu" + "tion/`",
        "written to " + root + proj + "/ref" + "ine/",
        "ids live under ``<ro" + "ot>/use-" + "cases/``",
    ):
        hits = [h for h in _DATA_ROOT_PATH.findall(bad) if "design/" not in h]
        assert hits, f"the gate would not have caught: {bad}"

    # The adjacency rule fires on the prose shape the path pattern cannot span...
    adjacent = "`<data_" + "root>/projects/<name>` - ids live under its `use-" + "cases/` subdir"
    assert _ROOT_TOKEN.search(adjacent) and _BARE_BUCKET.findall(adjacent)
    # ...and stays silent when the same line already names the new layout.
    fixed = adjacent.replace("`use-" + "cases/`", "`design/use-cases/`")
    assert "design/" in fixed

    # Each NEW shape fires on the form that actually appeared in this tree...
    py_bad = '(project_dir / "ref' + 'ine").mkdir(parents=True)'
    assert _PY_JOIN.search(py_bad), "python path-join shape not caught"
    py_good = 'Path(project_root) / "design" / "use-' + 'cases"'
    m = _PY_JOIN.search(py_good)
    assert m and '"design"' in py_good[max(0, m.start() - 24):m.start()], "the design/ form must be quiet"

    sh_bad = "  */solu" + "tion/*.md) ;;"
    assert _SHELL_GLOB.search(sh_bad), "shell glob shape not caught"

    for moved in ("status/feature-sugg" + "estions.md", "use-" + "cases/domains.md",
                  "use-" + "cases/registry.jsonl"):
        assert any(pat.search(moved) for pat in _MOVED_FILES), f"moved file not caught: {moved}"
    # ...and the corrected forms are silent, checked the way the sweep checks them.
    for fixed in ("`design/use-cases/domains.md`", "`design/use-cases/registry.jsonl`"):
        hits = [m for pat in _MOVED_FILES for m in pat.finditer(fixed)
                if not fixed[max(0, m.start() - 8):m.start()].endswith("design/")]
        assert not hits, f"the gate is noisy on the corrected form: {fixed}"

    for good in (
        "plans/       # technical plan docs  (was solu" + "tion/)",
        "a human-authored `solu" + "tion/` document - no dispatchable work",
        "Invoke `../ref" + "ine/SKILL.md` with `<topic>`",
        "`" + root + proj + "/design/use-cases/domains.md`",
        "return Path(project_root) / 'design' / 'use-cases'",
    ):
        hits = [h for h in _DATA_ROOT_PATH.findall(good) if "design/" not in h]
        assert not hits, f"the gate is noisy -- it flagged legitimate prose: {good} -> {hits}"


# --------------------------------------------------------------------------- #
# gh#111 -- a relative pointer is wrong only RELATIVE to where its file now sits
# --------------------------------------------------------------------------- #
#: The three statements in `auditor/SKILL.md` that have to agree with each other: where a gap spec
#: is written, the relative pointer stamped into it, and where the report it points at lives.
_SPEC_HOME = re.compile(r"<data_root>/projects/<name>/(?P<dir>[A-Za-z0-9_/-]+)/YYYY-MM-DD-<slug>\.md")
_EVIDENCE = re.compile(r"evidence:\s*(?P<rel>\S+?)#")
#: Step 5.2's declaration of where the report is WRITTEN. The first version of this anchored on
#: the parenthetical restatement three lines below the pointer -- so the two "independent" legs of
#: the arithmetic were one sentence apart, edited together, and a move of the REPORT bucket left
#: the test green. Review reproduced that: changing only the Step 5.2 line kept both tests passing.
#: Anchored the same way `_SPEC_HOME` is, on the `<data_root>/projects/<name>/` write declaration.
_REPORT_HOME = re.compile(
    r"`<data_root>/projects/<name>/(?P<path>[A-Za-z0-9_/-]+/YYYY-MM-DD-<pass>\.md)`"
)

#: The restatement beside the pointer. Not the authority -- cross-checked against it, so the two
#: cannot drift apart silently either.
_REPORT_ASIDE = re.compile(r"\(`(?P<path>[A-Za-z0-9_/-]+/YYYY-MM-DD-<pass>\.md)`\)")

#: Every file that PRESCRIBES the pointer. `_skill_text("auditor")` reads SKILL.md and nothing
#: else, which is how three more copies of the identical stale depth stayed live in this same
#: change: a reference doc, the template the skill copies from, and the harness driver that emits
#: one into a real spec and exits PASS. Third time in this session that an enumeration stopped at
#: SKILL.md.
_PRESCRIPTION_FILES = (
    "skills/auditor/SKILL.md",
    "skills/auditor/references/report-format.md",
    "templates/gap-epic.md",
    "harness/auditor-scenario/auditor_scenario.py",
)

#: Any relative pointer at the audits bucket, in any of the prescription files.
_ANY_EVIDENCE_REL = re.compile(r'''(?P<rel>(?:\.\./)+status/audits/[^\s`"'#)]+)''')


def test_auditor_evidence_pointer_resolves_to_the_report_location() -> None:
    """gh#111: the migration corrected the bucket NAME in this file and left the DEPTH behind.

    Gap specs stamp a provenance pointer back to the audit report that justified them. The pointer
    is relative, so it is correct only with respect to where the spec sits -- and the spec bucket
    moved one level deeper (`refine/` -> `design/specs/`) while the pointer did not. All 28
    pointers written to that point resolved to a `design/status/audits/` that does not exist.

    **This is invisible to a text sweep, and widening one would not have helped.** The stale string
    `../status/audits` contains no renamed bucket -- `status/` never moved -- and no root token.
    Nothing about it is textually wrong. It is wrong only in relation to the file's location, which
    a scan for renamed buckets cannot know. So the check has to be arithmetic instead: take the
    three locations the skill states about ITSELF, resolve one against the other, and require them
    to agree.

    Pure path arithmetic on the documented strings -- no filesystem, so it holds on a fresh clone
    and on a machine where no audit has ever run.

    The three legs are genuinely independent: the spec-write path from step 4.3, the stamped
    pointer, and STEP 5.2'S WRITE DECLARATION -- not the parenthetical restatement beside the
    pointer, which the first version of this test used. Those two sit three lines apart in one
    paragraph and get edited together, so a move of the report bucket left the test green; review
    reproduced it by changing only the Step 5.2 line. The restatement is now cross-checked against
    the authority instead of standing in for it.
    """
    text = _skill_text("auditor")

    m_spec = _SPEC_HOME.search(text)
    assert m_spec, "auditor/SKILL.md no longer states where a gap spec is written"
    spec_dir = "projects/<name>/" + m_spec.group("dir")

    m_ev = _EVIDENCE.search(text)
    assert m_ev, "auditor/SKILL.md no longer stamps an `evidence:` pointer"
    rel = m_ev.group("rel")

    m_rep = _REPORT_HOME.search(text)
    assert m_rep, "auditor/SKILL.md no longer declares where Step 5 WRITES the pass report"
    report = "projects/<name>/" + m_rep.group("path")

    # The prose beside the pointer restates that path. It is not the authority, but if it drifts
    # from the authority the next author copies the wrong one -- so pin them equal.
    m_aside = _REPORT_ASIDE.search(text)
    assert m_aside, "auditor/SKILL.md no longer restates the report path beside the pointer"
    assert m_aside.group("path") == m_rep.group("path"), (
        "the parenthetical beside the evidence pointer disagrees with where Step 5 writes the "
        f"report: aside says {m_aside.group('path')}, Step 5 writes {m_rep.group('path')}"
    )

    resolved = posixpath.normpath(posixpath.join(spec_dir, rel))

    assert resolved == report, (
        "the evidence pointer does not resolve to the report:" + chr(10)
        + f"  spec is written at   {spec_dir}/" + chr(10)
        + f"  pointer stamped      {rel}" + chr(10)
        + f"  resolves to          {resolved}" + chr(10)
        + f"  report is written at {report}" + chr(10)
        + "The relative depth must match how deep the spec bucket sits."
    )
    # And the resolution must actually LEAVE the spec bucket -- a pointer that stayed inside
    # `design/` would satisfy a weaker equality if someone also moved the report under design/.
    assert not resolved.startswith(spec_dir), "the pointer must climb out of the spec bucket"


def test_every_file_that_PRESCRIBES_the_pointer_uses_the_same_depth() -> None:
    """The arithmetic above proves ONE file self-consistent. Four files prescribe this pointer.

    When the depth was fixed in SKILL.md, the identical stale `../` survived in a reference doc,
    in the template the skill copies from, and in the harness driver -- which emits it into a real
    gap spec and exits PASS. A test anchored to `_skill_text("auditor")` cannot see any of them.

    Resolved from the spec home the skill states, so this stays correct if the bucket moves again
    rather than pinning a literal `../../`.
    """
    text = _skill_text("auditor")
    m_spec = _SPEC_HOME.search(text)
    assert m_spec, "auditor/SKILL.md no longer states where a gap spec is written"
    spec_dir = "projects/<name>/" + m_spec.group("dir")
    m_rep = _REPORT_HOME.search(text)
    assert m_rep, "auditor/SKILL.md no longer declares where Step 5 writes the report"
    audits_dir = posixpath.dirname("projects/<name>/" + m_rep.group("path"))

    seen, offenders = 0, []
    for rel_path in _PRESCRIPTION_FILES:
        f = REPO_ROOT / rel_path
        assert f.is_file(), f"{rel_path} not found -- renamed, or this list is stale"
        found_here = 0
        for n, line in enumerate(f.read_text(encoding="utf-8").split(chr(10)), 1):
            for m in _ANY_EVIDENCE_REL.finditer(line):
                found_here += 1
                seen += 1
                resolved = posixpath.dirname(
                    posixpath.normpath(posixpath.join(spec_dir, m.group("rel")))
                )
                if resolved != audits_dir:
                    offenders.append(
                        f"{rel_path}:{n}: {m.group('rel')} -> {resolved} (want {audits_dir})"
                    )
        # A file that stopped prescribing anything is a file this gate silently stopped covering.
        assert found_here, f"{rel_path} no longer prescribes an evidence pointer -- gate went quiet"

    assert seen >= len(_PRESCRIPTION_FILES), f"only {seen} pointers found across the surface"
    assert not offenders, (
        "prescribed evidence pointers do not resolve to the audits directory:" + chr(10)
        + chr(10).join("  " + o for o in offenders)
    )


def test_the_evidence_arithmetic_would_have_caught_the_shipped_bug() -> None:
    """The control. Without it, the assertion above passes on any tree where the two happen to
    line up, and nobody learns whether the arithmetic can fail at all."""
    spec_dir = "projects/<name>/design/specs"
    report = "projects/<name>/status/audits/YYYY-MM-DD-<pass>.md"

    shipped_bug = "../status/audits/YYYY-MM-DD-<pass>.md"
    assert posixpath.normpath(posixpath.join(spec_dir, shipped_bug)) != report

    fixed = "../../status/audits/YYYY-MM-DD-<pass>.md"
    assert posixpath.normpath(posixpath.join(spec_dir, fixed)) == report

    # ...and the same pointer was CORRECT before the bucket moved, which is the whole shape of it:
    # the string never changed and stopped being right anyway.
    assert posixpath.normpath(posixpath.join("projects/<name>/refine", shipped_bug)) == report


# --------------------------------------------------------------------------- #
# The atomic resume: unparking is ONE command (gh#36)
# --------------------------------------------------------------------------- #
#: The seats that can hold a Queue claim, and therefore the only ones that can unpark one.
#: `auditor` and `steward` also say "unpark" - of their own work item, not a Queue row - and
#: `LANE_OF` maps neither, so prescribing a release verb to them would be prescribing a call
#: the CLI refuses.
CLAIMING_SKILLS = ("lead-dev", "jr-dev-1", "jr-dev-2")

#: `--state` belongs to TWO subcommands with DIFFERENT enums: `status` sets the Agents-row
#: state (AGENT_STATES) and `release` sets the Queue-row state (RELEASE_STATES). Scraping the
#: flag without its verb conflates them - `status --state building` is legal for every seat,
#: including the two that hold no lane at all.
_RELEASE_STATE = re.compile(r"release[^`\n]*?--state ([a-z|]+)")
_STATUS_STATE = re.compile(r"status[^`\n]*?--state ([a-z|]+)")


@pytest.mark.parametrize("skill", CLAIMING_SKILLS)
def test_every_claiming_skill_names_the_ATOMIC_resume(skill):
    """Before gh#36 the only route was `release --state ready` then `claim`, and the skills
    said "unpark" without naming a verb - so every lane inferred the two-step. Between those
    two calls the row is genuinely unclaimed and `ready` ON DISK, which is the exact shape
    lead-dev's `relane` and `withdraw` are allowed to act on."""
    text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
    # On the RELEASE verb specifically. A bare `"--state building" in text` is satisfied by the
    # unrelated `status --state building|waiting|halted|idle` line every builder already carries,
    # so it passed with the resume prose mutated back to `--state ready` - a gate green on a
    # sentence about a different subcommand.
    release_states = {st for raw in _RELEASE_STATE.findall(text) for st in raw.split("|")}
    assert "building" in release_states, (
        f"{skill} does not name the atomic resume on `release` (found: {sorted(release_states)})"
    )


@pytest.mark.parametrize("skill", CLAIMING_SKILLS)
def test_no_claiming_skill_still_PRESCRIBES_the_two_step(skill):
    """The dangerous half is not the missing verb, it is the old one still reading as advice.
    Each skill must say plainly not to use it - a file that names the atomic form while an
    older paragraph elsewhere still tells you to release-then-claim has two instructions and
    no rule for which wins."""
    text = " ".join((SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8").split())
    assert "Do NOT reach for the old two-step" in text or "never `--state ready` then `claim`" in text, (
        f"{skill} names the atomic form but never warns off the two-step"
    )




@pytest.mark.parametrize("skill", ("auditor", "steward"))
def test_a_seat_with_no_lane_is_never_told_to_RELEASE_a_row(skill):
    """The narrowness control, and it nearly went the other way twice. Both files say "unpark",
    so a sweep on that word would have added a release verb to two seats that hold no Queue
    claim. The auditor also legitimately says `status --state building` for its OWN Agents
    row, so a check on the bare flag would have failed on correct prose - which it did, and is
    why this asserts on `release` specifically rather than on the flag."""
    text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
    assert not _RELEASE_STATE.findall(text), f"{skill} has no lane and cannot release a row"


def test_every_state_the_skills_name_is_one_ITS_OWN_subcommand_accepts():
    """The gate that would have caught last wake's near-miss: the prose was ready to be written
    while `building` existed only on a branch. Asserting against the enums rather than a literal
    means the docs cannot describe a state the code does not have - and asserting per SUBCOMMAND
    means a legal `status` state cannot be mistaken for an illegal `release` one."""
    from agentflow.registry import AGENT_STATES, RELEASE_STATES

    assert "building" in RELEASE_STATES, "gh#36 is not in the installed module"
    checked = 0
    for skill in (*CLAIMING_SKILLS, "auditor", "steward"):
        text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
        for raw in _RELEASE_STATE.findall(text):
            for state in raw.split("|"):
                assert state in RELEASE_STATES, f"{skill}: release --state {state}"
                checked += 1
        for raw in _STATUS_STATE.findall(text):
            for state in raw.split("|"):
                assert state in AGENT_STATES, f"{skill}: status --state {state}"
                checked += 1
    assert checked > 10, f"the regexes matched almost nothing ({checked}) - the query is wrong"


# --------------------------------------------------------------------------- #
# The mirror call site must PASS the resolved vault root (E-0200 items 3 + 6)
# --------------------------------------------------------------------------- #
def test_every_mirror_invocation_passes_ALL_THREE_resolved_values():
    """`vault.root` was decorative until the caller passed it, and so are the other two.
    The helper falls back to its own literal for each, so omitting any is not a no-op: the
    vault path decides where documents land, the project list decides which projects route
    by name rather than to `_unsorted/`, and the lock dir keeps the lock out of a synced
    folder where two machines can both believe they hold it.

    REPLACES a narrower gate that checked only `WIKI_VAULT_PATH` on the single line holding
    `bash`. Wrapping the invocation across a continuation put the variable on the line above
    and broke it - correctly. Two gates for one property, one of them now wrong, is worse
    than one that reads the whole invocation.

    Asserted per invocation, because one call site passing two of three is the missing one's
    bug back again on that path only."""
    required = ("WIKI_VAULT_PATH", "AGENTFLOW_PROJECTS", "AGENTFLOW_LOCK_DIR")
    invocations = []
    for path in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        if "_vendored" in path.parts:
            continue
        rows = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(rows):
            if "$MIRROR_SCRIPT" in line and "bash " in line:
                # the invocation wraps across a continuation, so a value on either half
                # of the window counts as passed
                window = " ".join(rows[max(0, n - 1):n + 2])
                invocations.append((path.parent.name, window))
    assert invocations, "found no mirror invocations - the query, not the tree, is wrong"
    for skill, window in invocations:
        missing = [v for v in required if v not in window]
        assert not missing, f"{skill} invokes the mirror without {missing}"


def test_the_vault_root_placeholder_matches_what_the_resolved_block_prints():
    """The skills interpolate `<vault_root>`; the block prints `vault_root`. A placeholder
    naming a key the block does not emit is a instruction nobody can follow, and nothing
    else would catch the two drifting apart."""
    from agentflow.config import render_resolved_block

    src = "".join(
        p.read_text(encoding="utf-8")
        for p in SKILLS_ROOT.glob("*/SKILL.md")
        if "_vendored" not in p.parts
    )
    if "<vault_root>" not in src:
        pytest.skip("no skill interpolates the vault root")
    assert "vault_root" in render_resolved_block.__doc__ or True  # doc is not the contract
    import inspect

    body = inspect.getsource(render_resolved_block)
    assert "vault_root" in body, "skills interpolate <vault_root> but the block never emits it"


# --------------------------------------------------------------------------- #
# The mirror gets its config passed, at every call site (E-0200 item 8)
# --------------------------------------------------------------------------- #
#: Env values the mirror helper reads from its caller. Each has a fallback literal
#: inside the script, so an omitted value is not a no-op - it is the script quietly
#: deciding where documents land, which projects route by name, and where the lock
#: lives.
MIRROR_ENV = ("WIKI_VAULT_PATH", "AGENTFLOW_PROJECTS", "AGENTFLOW_LOCK_DIR")


def _mirror_call_sites() -> list:
    """Every SKILL.md that invokes the mirror helper, DERIVED rather than listed.

    A hardcoded touchpoint list is a second copy of the truth and the copy is what
    goes stale: two of the three call sites were passing nothing at all while the
    config-driven values shipped and were documented as in use.

    The rule is "names the helper", not "names it on a line that also says bash".
    The narrower version missed `solution`, which assigns the helper to a variable
    on one line and invokes the variable on the next - so the gate would have swept
    clean over the one call site that was already right, and stayed silent if it
    regressed. A file that only DOCUMENTS the invocation is a false positive that
    costs nothing: it should name the same three values anyway.
    """
    return [
        p
        for p in sorted(SKILLS_ROOT.glob("*/SKILL.md"))
        if "vault.mirror_helper" in p.read_text(encoding="utf-8")
    ]


def test_some_skill_actually_invokes_the_mirror() -> None:
    """Guards the gate itself. A deriving matcher that matches nothing passes every
    assertion below it and reports a clean sweep of an empty set."""
    assert _mirror_call_sites(), "no SKILL.md matched as a mirror call site - matcher broken"


def test_every_mirror_call_site_passes_the_resolved_config() -> None:
    """The helper falls back to a hardcoded vault path, a hardcoded project list and a
    lock inside the vault. A caller passing none of them gets all three fallbacks,
    silently: documents land somewhere else, a newly configured project routes to
    _unsorted/, and the lock goes into a synced folder two machines can both believe
    they hold."""
    missing = {
        p.parent.name: [v for v in MIRROR_ENV if v not in p.read_text(encoding="utf-8")]
        for p in _mirror_call_sites()
    }
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, f"mirror call sites not passing their resolved config: {missing}"


def test_each_mirror_call_site_states_the_derived_touchpoint_count() -> None:
    """Each call site names how many mirror-write touchpoints exist. That number is a
    claim about the set `_mirror_call_sites` derives, so it is checked against that set
    rather than typed here, and a status kind is refused because no site passes one."""
    sites = _mirror_call_sites()
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
    phrase = f"one of {words[len(sites)]} mirror-write touchpoints"
    wrong = [p.parent.name for p in sites if phrase not in p.read_text(encoding="utf-8")]
    assert not wrong, f"call sites not stating {phrase!r}: {wrong}"
    status = [
        p.parent.name
        for p in sites
        if re.search(r'\bbash\s+"?\S+"?\s+"?\S+"?\s+status\b', p.read_text(encoding="utf-8"))
    ]
    assert not status, f"call sites passing a status kind to the mirror: {status}"


# --------------------------------------------------------------------------- #
# E-0380 -- the CI sanitize gate cannot fire on a string it EXEMPTS.
#
# `.github/workflows/ci.yml` greps the whole tree for personal/product literals and
# fails the build on a hit. Its pattern can carry lookaround exemptions so legitimate
# vocabulary survives: a negative lookbehind keeps a longer word legal while the bare
# stem at its end is not, and a negative lookahead does the same at the other end.
#
# WHERE THE EXEMPTIONS COME FROM (gh#821). The shipped ci.yml pattern carries generic
# shapes only, and none of them exempts a word. The exempting alternatives are
# install-specific, so they live in the extra pattern: the repository variable
# SANITIZE_EXTRA_PATTERN, which CI's `_shared pytest` step hands this suite as an
# environment variable of the same name. These gates derive from ci.yml's pattern
# UNIONED with it, and SKIP - saying why - where it is empty (a public copy, or a
# local run that did not export it). A skip there is honest; a pass over an empty
# exemption set would not be.
#
# That exemption is a blind spot with teeth, and it has already been paid for once. A
# bulk `str.replace` derived from this pattern's MATCHES while ignoring its EXEMPTIONS
# corrupted the lookbehind-exempt token across six skill files, 18 occurrences, and CI
# stayed 12/12 green through the whole thing -- the gate cannot fire on a string it
# exempts. The strings were repaired by hand afterwards and nothing guarded the CLASS.
#
# State that precisely, because an earlier draft of this comment did not and the
# imprecision is the same species as the bug. It said a grep for an assertion naming the
# token "returned nothing at all". That grep was case-sensitive; case-insensitively there
# IS one elsewhere in the suite, on an unrelated subject that never reads a skill file and
# so could not have seen the corruption. So the premise of this block stands and the
# sentence describing it did not. A stated grep result is a claim that you ran that grep
# and it answered; a narrower probe returning empty reads exactly like absence.
#
# THE EXEMPTION LIST IS PARSED OUT OF THE PATTERN, never restated here, and this file
# does not spell any exempt token anywhere. A test that hardcodes today's exempt word
# while the gate exempts a different one next month is the same blind spot one layer
# up, and this module already carries that scar three times over (a renamed call, a
# moved bucket, a marker token nothing matched).
#
# WHAT THIS DELIBERATELY DOES NOT DO, written down so the next reader does not reach
# for it. It does not scan for corrupted SIBLINGS of an exempt token, and it must not
# be rewritten as "every occurrence of the lookbehind prefix must be followed by the
# stem": the tree legitimately contains other ordinary words built on that prefix, so
# that form reds on correct prose. Nor can it scan for the
# corruption's replacement string, which is unknowable in general -- a replace can put
# anything there. PRESENCE of the exact exempt token is the only property that is both
# mechanically checkable and true.
# --------------------------------------------------------------------------- #

#: The workflow that owns the gate. Read, never written, by this module.
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The `- name:` of the step that runs the sanitize grep. The parser anchors on the STEP,
#: not on the first `pattern=` it finds: ci.yml holds more than one `pattern=` assignment
#: (the worktree-tool-gate declares its own, double-quoted and with no lookarounds), and a
#: parser that grabbed the wrong one would derive an empty exemption set and report it as
#: a clean parse.
SANITIZE_STEP_NAME = "Grep for personal/product literals"

#: `(?<!PREFIX)STEM` -- the exempt token is PREFIX + STEM.
_NEG_LOOKBEHIND = re.compile(r"\(\?<!([A-Za-z]+)\)([A-Za-z]+)")
#: `STEM(?!SUFFIX)` -- the exempt token is STEM + SUFFIX.
_NEG_LOOKAHEAD = re.compile(r"([A-Za-z]+)\(\?!([A-Za-z]+)\)")


def _sanitize_step_lines(path: Path | None = None) -> list[str]:
    """The sanitize STEP's own lines, `- name:` through the line before the next step.

    Split out of `_sanitize_pattern_source` rather than duplicated because the gh#247
    block below needs the same slice for a different question -- what the step's `env:`
    declares, and what its `run:` script does with it. Two slicers would drift, and a
    slicer that drifts WIDE is the failure this one already had once: an earlier version
    ended at the next `- name:` at any indent, ran 58 lines past the end of the job, and
    the one-pattern assertion below held by luck.

    Fails loudly on a moved file or a renamed step instead of returning []: a derived gate
    that cannot find its source guards nothing, and would report that as clean.
    """
    path = path or CI_WORKFLOW
    assert path.is_file(), (
        f"the sanitize workflow is not at {path} - it moved, and every exemption this "
        "block derives from it would silently become the empty set"
    )
    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [
        i for i, ln in enumerate(raw)
        if ln.lstrip().startswith("- name:") and SANITIZE_STEP_NAME in ln
    ]
    assert len(starts) == 1, (
        f"{path.name} has {len(starts)} step(s) named {SANITIZE_STEP_NAME!r}, expected 1 - "
        "the sanitize step was renamed or removed, and this whole block is now deriving its "
        "exemptions from a gate that is not the one CI runs"
    )
    # The step body ends at the next `- name:` AT THE SAME INDENT, or at the first line
    # indented less than the step itself (which is the enclosing job ending), or EOF.
    # "The next `- name:` at any indent" was wrong and not merely imprecise: on the real
    # file it ran 58 lines, past the end of this job and into the worktree-tool-gate's
    # header, so the one-pattern assertion below was holding by luck rather than by
    # scope. A job-level `env:` carrying a `pattern=`, or this step becoming a call to a
    # script, and the parser would have read a different gate's pattern.
    step_indent = len(raw[starts[0]]) - len(raw[starts[0]].lstrip())
    end = len(raw)
    for j in range(starts[0] + 1, len(raw)):
        line = raw[j]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent < step_indent or (indent == step_indent and line.lstrip().startswith("- ")):
            end = j
            break
    return raw[starts[0]:end]


def _sanitize_pattern_source(path: Path | None = None) -> str:
    """The sanitize gate's regex, with the shell `''` concatenation undone.

    A literal term in ci.yml has to be split with juxtaposed single-quoted shell segments
    so the workflow file never matches its own pattern. Undoing that is step one of
    parsing rather than cosmetics: a lookbehind on a SPLIT stem leaves the exemption
    unreadable -- and derived wrong, silently -- until the halves are rejoined. (The
    shipped pattern is generic shapes with no split; the walk stays for the day one is.)

    Scoped to the sanitize STEP's own body. The workflow declares more than one
    `pattern=` -- the worktree-tool-gate has its own, double-quoted and lookaround-free --
    so "the first pattern= line in ci.yml" is not the gate's pattern, and a parser that
    took it would derive zero exemptions and call that a successful parse.

    Fails loudly on a moved file or an ambiguous line instead of returning "": a derived
    gate that cannot find its source guards nothing, and would report that as clean.
    """
    candidates = [
        ln.strip() for ln in _sanitize_step_lines(path) if ln.strip().startswith("pattern=")
    ]
    assert len(candidates) == 1, (
        f"the {SANITIZE_STEP_NAME!r} step has {len(candidates)} `pattern=` line(s), expected "
        "exactly 1 - the parser cannot tell which one the gate runs, and picking wrong is "
        "invisible from a green run"
    )
    rhs = candidates[0].split("=", 1)[1].strip()
    # Shell single-quote concatenation: adjacent quoted segments join, and the empty `''`
    # between two halves of a split stem is just an empty segment. Joining the segments IS
    # the un-splitting, so this needs no knowledge of which terms split.
    #
    # WALKED, not `re.findall("'([^']*)'")`. That shortcut is not shell semantics and its
    # wrong answers are SILENT, which is the one failure mode this whole block exists to
    # remove. Given `pattern='(?<!super)acme'product'|...'` -- an unquoted middle segment --
    # it drops `product` and derives a TRUNCATED token. Truncated is the dangerous shape: it is
    # ASCII, alphabetic, longer than the shape floor, and a PREFIX of the real word, so a
    # substring-based presence check finds it everywhere the real word lives and every
    # gate below stays green while guarding a word that does not exist. A trailing
    # `# comment 'with quotes'` likewise appends junk. So: require the value to be
    # entirely quoted segments with nothing between them, and fail loudly otherwise.
    assert rhs.startswith("'"), (
        f"the `pattern=` value is not shell-single-quoted, cannot parse safely: {rhs!r}"
    )
    segments: list[str] = []
    i = 0
    while i < len(rhs):
        if rhs[i] != "'":
            raise AssertionError(
                f"unquoted text at offset {i} of the `pattern=` value ({rhs[i:][:40]!r}) - "
                "this parser only accepts a value built entirely from adjacent shell "
                "single-quoted segments. Anything else (an escaped quote, a trailing "
                "comment, a bare segment) would join into a SILENTLY wrong pattern and "
                f"derive a truncated exempt token. Full value: {rhs!r}"
            )
        close = rhs.find("'", i + 1)
        assert close != -1, f"unterminated single-quoted segment in `pattern=`: {rhs!r}"
        segments.append(rhs[i + 1:close])
        i = close + 1
    assert segments, f"the `pattern=` value parsed to no segments at all: {rhs!r}"
    return "".join(segments)


def _word_present(token: str, blob: str) -> bool:
    """Does a word in `blob` START with `token`? (both already lower-cased)

    Anchored at a word start, but deliberately NOT `\\b...\\b`. A derived exempt token is
    the MINIMAL exempt form, not a complete word: the word it protects is written inflected
    as readily as bare - a plural, a third person, a participle. A whole-word test
    therefore reds on a tree that says only the inflected forms, which is the strictness
    failing in the direction that reports absence where the vocabulary is plainly alive.

    Anchoring the START still rejects a token that survives only as an internal fragment.
    What it does NOT do is catch a truncated derivation, because a truncation is a prefix
    and prefixes match here by design. That defence belongs where truncation can arise:
    `_sanitize_pattern_source` refuses any value it cannot parse as whole quoted segments,
    rather than joining them into a plausible-looking wrong answer.
    """
    return re.search(rf"\b{re.escape(token)}", blob) is not None


def _derived_exemptions(pattern_src: str) -> dict[str, list[str]]:
    """Exempt tokens the gate carves out, keyed by the lookaround shape that carved them.

    Keyed rather than flattened because the two are not interchangeable evidence: the six
    loop skills are guarded on the LOOKBEHIND token specifically, that being the token the
    historical corruption actually hit.
    """
    return {
        "lookbehind": [p + s for p, s in _NEG_LOOKBEHIND.findall(pattern_src)],
        "lookahead": [s + x for s, x in _NEG_LOOKAHEAD.findall(pattern_src)],
    }


#: The extra pattern, in the repository variable's own `|`-joined form, under the variable's
#: own name. CI's `_shared pytest` step passes it through; see the block comment above.
_EXTRA_PATTERN_ENV = "SANITIZE_EXTRA_PATTERN"


def _exemption_pattern_source() -> str:
    """The pattern the exemptions are derived from: ci.yml's, UNIONED with the extra pattern
    when this run has one (gh#821).

    SKIPS the calling test when the union carries no word exemption and no extra pattern was
    supplied: the shipped pattern exempts no word, so there is nothing to guard, and saying
    so is the honest result - a pass would read exactly like a guarded tree. Fails, rather
    than skips, on an extra pattern that does not compile, because that is a broken input,
    not an absent one. Neither message prints the extra pattern: it is the private half.
    """
    src = _sanitize_pattern_source()
    extra = os.environ.get(_EXTRA_PATTERN_ENV, "").strip()
    if extra:
        try:
            re.compile(extra)
        except re.error as exc:
            raise AssertionError(
                f"${_EXTRA_PATTERN_ENV} is set but does not compile (error at offset {exc.pos})"
            ) from None
        return src + "|" + extra
    ex = _derived_exemptions(src)
    if not (ex["lookbehind"] or ex["lookahead"]):
        pytest.skip(
            f"${_EXTRA_PATTERN_ENV} is empty and the shipped ci.yml pattern exempts no word, so "
            "there is no exempt token to guard (a public copy, or a local run that did not "
            "export the variable)"
        )
    return src


def test_the_workflow_rescans_published_text_when_a_description_is_edited() -> None:
    """gh#331. The default pull_request types EXCLUDE `edited`, so a title or body changed after
    the last commit was never re-scanned - the published-text half of the gate saw only the text
    that existed when a commit last landed.

    Asserts the CONSEQUENCE rather than the presence of a line: the workflow that CONTAINS the
    published-text scan must fire on `edited`. A test that merely grepped ci.yml for the word
    would pass on a file where `types` had been added to some other trigger.
    """
    import yaml

    parsed = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1), so accept either.
    triggers = parsed[True] if True in parsed else parsed["on"]
    types = triggers["pull_request"]["types"]

    assert "edited" in types, (
        f"pull_request types {types!r} excludes 'edited', so a description edited after the last "
        "run is never re-scanned - gh#331 reopened"
    )
    for default in ("opened", "synchronize", "reopened"):
        assert default in types, (
            f"spelling out `types` DROPPED the default event {default!r}; naming the list means "
            "owning all of it"
        )

    # The scan must still live in THIS workflow. Moving it to a sibling was built and rejected:
    # branch protection requires the context by job name, so a separate job is not a required
    # check and the gate stops blocking merges while still going red.
    assert any(
        SANITIZE_STEP_NAME in ln for ln in CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    ), "the published-text scan left ci.yml; the required status context no longer covers it"


def test_the_sanitize_job_keeps_the_name_branch_protection_requires() -> None:
    """The required status context is matched by JOB NAME, and nothing in this repo records that
    list - so a rename is invisible here and silently demotes the gate to advisory.

    This pins the one half the repo CAN hold. The required-contexts list itself is deployed
    config; that gap is filed separately.
    """
    import yaml

    parsed = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert parsed["jobs"]["sanitize-gate"]["name"] == "sanitize grep gate", (
        "renaming this job silently drops it out of branch protection's required contexts, "
        "which are matched by name and are not recorded anywhere in this repo"
    )


def test_the_sanitize_exemptions_actually_parse_out_of_the_workflow() -> None:
    """MANDATORY VACUITY GUARD for the two gates below, which both iterate the derived set.

    A parser that quietly returns `[]` makes "every exempt token is still present" and
    "every loop skill still names it" pass over nothing at all, and an empty sweep reports
    the identical green as a clean tree. Both shapes are pinned here, separately, because a
    pattern that kept only its lookahead exemption would leave the six-file gate vacuous
    while the presence gate still looked busy.

    Derived from ci.yml's pattern UNIONED with the extra pattern (gh#821), and skipped -
    saying why - when there is no extra pattern and the shipped one exempts no word. Only
    the ci.yml half is ever printed below; the extra half is the private one.
    """
    shipped = _sanitize_pattern_source()
    src = _exemption_pattern_source()
    assert "''" not in shipped, (
        "the shell concatenation was not undone - the stems are still split, so a "
        f"lookbehind exemption cannot be read correctly: {shipped!r}"
    )
    ex = _derived_exemptions(src)
    every = ex["lookbehind"] + ex["lookahead"]
    assert every, (
        "parsed ZERO exemptions out of the sanitize pattern - the gates below now sweep an "
        f"empty set and pass on any tree whatsoever. ci.yml half read as: {shipped!r}"
    )
    assert ex["lookbehind"], (
        "parsed no NEGATIVE-LOOKBEHIND exemption - the six-loop gate below is now vacuous "
        f"and the corruption it exists to catch would pass unseen. ci.yml half: {shipped!r}"
    )
    assert ex["lookahead"], (
        "parsed no NEGATIVE-LOOKAHEAD exemption - either the pattern stopped exempting the "
        f"word it used to, or the parser stopped seeing it. ci.yml half: {shipped!r}"
    )
    # By index, through pytest.fail: a token may come from the extra half, and a failing
    # assert's rewritten explanation would print it.
    implausible = [
        n for n, tok in enumerate(every)
        if not (tok.isascii() and tok.isalpha() and len(tok) > 3)
    ]
    if implausible:
        pytest.fail(
            f"derived exempt token(s) #{implausible} are not plausible words - the lookaround "
            "shapes changed and the parser is reading fragments, which would make both gates "
            "below assert something arbitrary"
        )


def test_every_derived_exempt_token_still_occurs_in_the_first_party_tree() -> None:
    """A total wipe of an exempt word is invisible to the gate that exempts it.

    This is the tree-wide half: whatever the pattern exempts must still be SAID somewhere the
    gate itself is not, or the exemption and the tree have silently stopped agreeing.
    Case-insensitive because a word is capitalised in prose and lowercase inside
    the pattern, and the corruption did not care about case either.
    """
    files = _first_party_files()
    assert len(files) > 150, f"the sweep found only {len(files)} files -- it is not scanning"
    # The workflow the exemptions were parsed FROM must not be in the corpus, or each token
    # would prove its own existence and this gate would be perfectly circular. What actually
    # holds it today is `_SCANNED_SUFFIXES`, which has no `*.yml` - NOT `_FIRST_PARTY_ROOTS`
    # excluding `.github/`, which an earlier version of this comment credited. The difference
    # is operational: widening the roots would not trip this assert, while adding `*.yml` to
    # the suffixes would. Asserted rather than assumed so either change is caught.
    assert not any(f.name == "ci.yml" for f in files), (
        "the first-party sweep now includes the workflow that DEFINES the exemptions - this "
        "gate would pass on a tree where the exempt word survives only inside the gate"
    )
    ex = _derived_exemptions(_exemption_pattern_source())
    every = ex["lookbehind"] + ex["lookahead"]
    assert every, "no exemptions parsed - see the vacuity guard above; this gate is vacuous"
    texts = [f.read_text(encoding="utf-8", errors="replace").lower() for f in files]
    missing = [t for t in every if not any(_word_present(t, blob) for blob in texts)]
    assert not missing, (
        f"the sanitize gate exempts {missing}, but no first-party file outside the workflow "
        "contains it any more, as a word. Three things can put you here and they have "
        "different remedies. (a) A bulk replacement derived from the gate's MATCHES wiped the "
        "vocabulary its EXEMPTIONS exist to protect - CI stays green through exactly that, "
        "because the gate cannot fire on a string it exempts; restore the word. (b) The word "
        "was retired on purpose - drop its exemption from the pattern in the same change, so the "
        "gate and the tree stop disagreeing. (c) An exemption was ADDED to the pattern for a word "
        "this tree does not yet use - that is legitimate and this gate is the wrong place to "
        "learn it; add the word or hold the exemption until you do."
    )


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_every_loop_skill_still_names_the_lookbehind_exempt_token(skill: str) -> None:
    """The sharp one: the six files the historical corruption actually hit.

    Tree-wide presence is not enough on its own - one surviving mention in a doc would
    satisfy the gate above while five of the six loops had been corrupted back.

    SCOPED TO THE LOOP-SKILL EXEMPT TOKEN, which is identified rather than named: of the
    tokens the pattern exempts, the ones this gate holds are those a healthy tree carries in
    ALL SIX loop skills. That indirection is the whole design. Coding it over *every*
    lookbehind exemption looked equivalent and was not: the sanitize pattern is an openly
    growing list, so the next commit that exempts a new literal would have reddened this
    test six times over a corruption that never happened, and the remedy the message
    offered - "update the expectation" - names an expectation that does not exist, because
    the set is derived. A newly exempted word simply has no loop-skill coverage yet and is
    not this gate's business; the tree-wide gate above is where it is accounted for.

    Compared case-insensitively and as WHOLE WORDS, by PRESENCE only. Do not "improve"
    this into a scan for the prefix followed by something other than the stem - see the
    block comment at the top of this section for why that shape reds on correct English
    prose.
    """
    behind = _derived_exemptions(_exemption_pattern_source())["lookbehind"]
    assert behind, (
        "no lookbehind exemption parsed out of the pattern - this gate just went vacuous and "
        "would pass on all six loop skills without reading a word of them"
    )
    texts = {loop: _skill_text(loop).lower() for loop in ALL_LOOPS}
    # A token is this skill's business when ANOTHER loop skill still carries it. That
    # predicate is what makes the parametrized case real work rather than six copies of one
    # global check: "present in all six" would be true of the token by construction, so a
    # per-skill assertion derived from it could never fire. Sibling-carried is different for
    # each skill and is exactly the single-file-corruption shape.
    missing = [
        t for t in behind
        if not _word_present(t.lower(), texts[skill])
        and any(_word_present(t.lower(), b) for loop, b in texts.items() if loop != skill)
    ]
    assert not missing, (
        f"{skill}/SKILL.md no longer names {missing}, which other loop skills still carry - "
        "so this is one file losing the exempted word, not the project retiring it. That is "
        "exactly the corruption the CI sanitize gate structurally cannot see: a bulk replace "
        "derived from the gate's MATCHES rather than its EXEMPTIONS mangled this word across "
        "these files in 18 places while CI reported 12/12 green."
    )


def test_the_six_file_exempt_token_gate_covers_all_six_loops() -> None:
    """NON-VACUITY for the parametrization, plus the one case it structurally cannot see.

    Two jobs, both about the gate above being narrower than it looks.

    1. That gate is only as wide as `ALL_LOOPS`. A loop dropped from that tuple stops being
       checked with nothing turning red - the same hollowing that let a renamed selection
       call empty a guard earlier in this file. The tuple is cross-checked against the loop
       skills actually on disk rather than pinned to a literal 6, because this module
       defines `ALL_LOOPS` TWICE with identical values and a contributor editing the other
       copy would move nothing here.
    2. The gate above asks "do OTHER loop skills still carry this word", which is precisely
       blind to the historical incident's real shape: all six corrupted AT ONCE, so no
       sibling carries it and every per-skill check passes. The tree-wide gate does not
       catch that either, because the word survives in docs. So the all-six case is pinned
       HERE: at least one lookbehind-exempt token must be carried by every loop skill.
    """
    on_disk = sorted(p.parent.name for p in SKILLS_ROOT.glob("*/SKILL.md")
                     if p.parent.name in set(ALL_LOOPS))
    assert sorted(ALL_LOOPS) == on_disk, (
        f"ALL_LOOPS is {sorted(ALL_LOOPS)} but the loop skills on disk are {on_disk} - the "
        "exempt-token gate is scoped to that tuple, so a loop missing from it silently stops "
        "being guarded against the corruption that hit all six at once"
    )
    behind = _derived_exemptions(_exemption_pattern_source())["lookbehind"]
    texts = [_skill_text(loop).lower() for loop in ALL_LOOPS]
    universal = [t for t in behind if all(_word_present(t.lower(), b) for b in texts)]
    assert universal, (
        f"NO lookbehind-exempt token ({behind}) is carried by every loop skill any more. "
        "Either all of them lost it together - which is the 18-occurrence incident's exact "
        "shape, invisible to both the CI gate and the per-skill check above - or the word was "
        "retired everywhere on purpose, in which case its exemption leaves the pattern in the "
        "same change and this gate goes with it."
    )


def test_the_forwarding_gate_covers_every_wrapper_that_claims_to_forward() -> None:
    """NON-VACUITY for `_forwarding_wrappers()`, which is a derived parametrization.

    A derived list can go empty or go narrow without any test reddening - the failure this
    module has been bitten by repeatedly. Three properties, each catching a different way
    the derivation could hollow out.
    """
    wrappers = _forwarding_wrappers()
    assert wrappers, (
        "no command wrapper mentions $ARGUMENTS at all - the forwarding gate is now "
        "parametrized over an empty list and passes without reading a file"
    )
    uncovered = sorted(set(ALL_LOOPS) - set(wrappers))
    assert not uncovered, (
        f"loop wrapper(s) {uncovered} no longer mention $ARGUMENTS anywhere, so they dropped "
        "out of the forwarding gate entirely. Every self-waking loop re-enters through its "
        "wrapper and needs its --project selector carried, so this is the gate going quiet, "
        "not the requirement going away"
    )
    beyond_the_loops = sorted(set(wrappers) - set(ALL_LOOPS))
    assert beyond_the_loops, (
        "the forwarding gate now covers exactly the six loops, which is how it was first "
        "written and was wrong: commands/report.md forwards too and was ungated, so deleting "
        "its forwarding line reproduced this epic's own defect in a file no test read. If a "
        "non-loop wrapper genuinely stopped forwarding, this line is the deliberate decision"
    )


# --------------------------------------------------------------------------- #
# The ci.yml parser, against synthetic input
# --------------------------------------------------------------------------- #
# The tree cannot exercise the parser's failure modes: it has exactly one shape, the
# correct one. Every gate above rests on this function, so it is tested the way this
# module tests its other matchers - by feeding it hand-built content and requiring it to
# FIRE. A parser proven only against the real file is a parser proven against one input.
# The terms are placeholders that name nothing, split the way a literal in ci.yml would be.
_GOOD_STEP = """jobs:
  lint:
    steps:
      - name: Grep for personal/product literals
        run: |
          set -euo pipefail
          pattern='(?<!super)acme''product|zephyr''host(?!ed)'
          echo done
      - name: Something else
        run: |
          pattern="SomeOtherGate|unrelated"
"""


def _write_ci(tmp_path, body: str):
    p = tmp_path / "ci.yml"
    p.write_text(body, encoding="utf-8")
    return p


def test_the_parser_undoes_the_shell_split_on_well_formed_input(tmp_path) -> None:
    """The control. Every refusal below is only meaningful against a passing case."""
    src = _sanitize_pattern_source(_write_ci(tmp_path, _GOOD_STEP))
    assert "''" not in src
    ex = _derived_exemptions(src)
    assert ex["lookbehind"] == ["superacmeproduct"] and ex["lookahead"] == ["zephyrhosted"]


def test_the_parser_REFUSES_an_unquoted_segment_instead_of_truncating(tmp_path) -> None:
    """THE dangerous input, and the reason this parser walks rather than findall-ing.

    `acme'product'` leaves `product` outside any quoted segment. A naive
    `re.findall("'([^']*)'")` drops it and yields `superacme` - ASCII, alphabetic, past the
    length floor, and a PREFIX of the real token, so every downstream presence check still
    finds it and every gate stays green while guarding a word that does not exist.
    """
    bad = _GOOD_STEP.replace("(?<!super)acme''product", "(?<!super)acme'product'")
    with pytest.raises(AssertionError, match="unquoted text"):
        _sanitize_pattern_source(_write_ci(tmp_path, bad))


def test_the_parser_REFUSES_a_trailing_comment_with_quotes(tmp_path) -> None:
    """A comment's paired quotes would otherwise be joined into the pattern as junk."""
    bad = _GOOD_STEP.replace(
        "pattern='(?<!super)acme''product|zephyr''host(?!ed)'",
        "pattern='(?<!super)acme''product|zephyr''host(?!ed)'  # see 'notes'",
    )
    with pytest.raises(AssertionError, match="unquoted text"):
        _sanitize_pattern_source(_write_ci(tmp_path, bad))


def test_the_parser_REFUSES_a_double_quoted_pattern(tmp_path) -> None:
    """Not shell single-quote concatenation, so the un-splitting premise does not hold."""
    bad = _GOOD_STEP.replace(
        "pattern='(?<!super)acme''product|zephyr''host(?!ed)'", 'pattern="acmeproduct"')
    with pytest.raises(AssertionError, match="not shell-single-quoted"):
        _sanitize_pattern_source(_write_ci(tmp_path, bad))


def test_the_parser_REFUSES_a_renamed_or_missing_sanitize_step(tmp_path) -> None:
    """Fails loudly rather than deriving an empty exemption set and calling it clean."""
    bad = _GOOD_STEP.replace("Grep for personal/product literals", "Grep for literals")
    with pytest.raises(AssertionError, match="expected 1"):
        _sanitize_pattern_source(_write_ci(tmp_path, bad))


def test_the_parser_REFUSES_two_patterns_inside_the_sanitize_step(tmp_path) -> None:
    """Ambiguity is a refusal: picking wrong is invisible from a green run."""
    bad = _GOOD_STEP.replace(
        "          echo done", "          pattern='second'\n          echo done")
    with pytest.raises(AssertionError, match="expected exactly 1"):
        _sanitize_pattern_source(_write_ci(tmp_path, bad))


def test_the_parser_does_NOT_read_past_the_end_of_the_sanitize_step(tmp_path) -> None:
    """The step slice must respect the step, or a neighbouring job's pattern= is in range.

    The first version ended the slice at the next `- name:` AT ANY INDENT, which on the
    real workflow ran 58 lines and swallowed the following job's header. It held only
    because that job's own pattern= happened to sit after its own `- name:`.
    """
    neighbour = _GOOD_STEP.replace(
        '          pattern="SomeOtherGate|unrelated"',
        "    env:\n      pattern='decoy'\n",
    )
    src = _sanitize_pattern_source(_write_ci(tmp_path, neighbour))
    assert "decoy" not in src, "the parser read a pattern= belonging to another job"


# --------------------------------------------------------------------------- #
# gh#247 -- the sanitize gate could not see a PLAIN absolute home path, and never
# read the PR body at all.
#
# Its only personal-path term was the MANGLED session-directory shape. An ordinary
# absolute home path -- drive letter, MSYS mount, /home, /Users -- read clean, and that
# is not an exotic input. Every lane is required to prove which tree its suite imported
# by printing `agentflow.__file__`, and that output is an absolute home path by
# construction, so the discipline that makes a run trustworthy is the discipline that
# produces the literal. The measured live instance sat in a PR BODY for about twenty
# minutes, and a PR body is not a tracked file, so the tree half could not have caught
# it even after the widening. Both halves are pinned here.
#
# THE PLACEHOLDER WORD SET IS PARSED OUT OF ci.yml, never restated here. That is the
# same rule the E-0380 block states above and it is the same hazard: the widened term
# carries an EXEMPTION -- prose is allowed to write a placeholder home path -- and a gate
# cannot fire on a string it exempts. A test that hardcoded today's placeholder list
# while the gate exempted a different one next month would be that blind spot one layer
# up, which is exactly the shape this module has already been bitten by three times.
#
# EVERY POSITIVE PATH LITERAL BELOW IS BUILT BY CONCATENATION at the account boundary,
# the same trick ci.yml uses on its own terms. A spelled-out home path in this file would
# be a finding in the whole-tree grep, and it would be right: this is a test file, not an
# exemption. The NEGATIVE cases are spelled out in full on purpose -- that they do not
# match is the assertion.
#
# WHAT THIS DELIBERATELY DOES NOT DO, so nobody reaches for it. It does not claim the
# gate catches every home-path shape: UNC paths, a home outside Users/home, and an
# account named exactly like a placeholder word are out by design and are named in
# ci.yml's own comment. Nor does it re-derive the account shape. The five negatives are
# the placeholders MEASURED in this tree the day the widening was written, kept as
# regression cases so a future narrowing reds here rather than in a contributor's PR.
# --------------------------------------------------------------------------- #

#: `(?!(?:you|user|...)(?![...]))` -- the placeholder words the home-path term exempts.
#: Anchored on the trailing `(?![` so this cannot accidentally read some other lookahead.
_PLACEHOLDER_GROUP = re.compile(r"\(\?!\(\?:([a-z|]+)\)\(\?!\[")

#: Not a real account and not anyone's name: two throwaway syllables that satisfy the
#: account shape (alphanumeric first character, two or more characters, not a placeholder).
_SYNTHETIC_ACCOUNT = "qzacct7"

#: Home-path prefixes the widened term must see, keyed by what each one is. Held apart
#: from the account so no line in this file spells a whole home path.
_PLAIN_HOME_PREFIXES = {
    "windows drive letter, backslash": "C:\\Users\\",
    "windows drive letter, forward slash": "C:/Users/",
    "MSYS / Git-Bash mount": "/c/Users/",
    "linux": "/home/",
    "macos, standing alone rather than as the tail of a windows path": "/Users/",
}

#: The five placeholders MEASURED in this tree while widening the pattern. Every one is
#: legitimate prose -- an elision, a fixture, a doc placeholder -- and none of the five
#: files may be edited to suit the gate; the gate accommodates them.
_MEASURED_PLACEHOLDERS = (
    (r"C:\Users\...\stabilization-tests\...", "status_writer.py -- literal ellipsis"),
    (r"C:/Users/...", "orchestrator/SKILL.md -- literal ellipsis"),
    (r"C:/Users/x/.claude/...", "test_deployed_skill_paths.py -- single-letter fixture"),
    (r"/home/x/.claude/...", "test_deployed_skill_paths.py -- single-letter fixture"),
    ("/home/you/agentflow-data", "docs/INSTALL.md -- placeholder word"),
)


def _compiled_sanitize() -> re.Pattern[str]:
    """The gate's own regex, compiled the way CI runs it.

    IGNORECASE because the gate runs `grep -riP`; a case-sensitive probe here would be
    testing a different regex from the one that fails the build. Python's `re` accepts
    this pattern's fixed-width lookbehinds, and if a future term uses a variable-width
    one `re.compile` raises here rather than quietly testing something else.
    """
    return re.compile(_sanitize_pattern_source(), re.IGNORECASE)


def _placeholder_words() -> list[str]:
    """Placeholder accounts the gate exempts, read out of ci.yml."""
    found = _PLACEHOLDER_GROUP.findall(_sanitize_pattern_source())
    assert len(found) == 1, (
        f"parsed {len(found)} placeholder groups out of the sanitize pattern, expected 1 - "
        "either the exemption changed shape or this parser stopped seeing it, and every "
        "gate below would then sweep the empty set and report that as green"
    )
    return [w for w in found[0].split("|") if w]


def test_the_placeholder_exemption_parses_out_of_the_workflow() -> None:
    """MANDATORY VACUITY GUARD for the per-word gate below, which iterates a derived set.

    A parser that quietly returned `[]` would make "no placeholder is a finding" and "a
    real account beginning with a placeholder still is" both pass over nothing, and an
    empty sweep reports the identical green as a working exemption.
    """
    words = _placeholder_words()
    assert len(words) >= 3, (
        f"parsed only {words} as the placeholder exemption - too few to be the real set, "
        "so the per-word gate below is close to vacuous"
    )
    for w in words:
        assert w.isascii() and w.isalpha() and len(w) >= 2, (
            f"derived placeholder {w!r} is not a plausible word - the exemption changed "
            "shape and this parser is reading fragments"
        )


def test_the_sanitize_pattern_matches_every_plain_absolute_home_form() -> None:
    """gh#247's actual bug: five plain home shapes the gate used to read as clean.

    Each probe is checked to match AT OFFSET ZERO, so a pass means the home-path term
    fired on the prefix under test rather than some unrelated term matching later in the
    same string. That is what makes the macOS case real work: `/Users/<acct>` with no
    drive letter and no mount prefix in front of it can only be the macOS alternative.
    """
    rx = _compiled_sanitize()
    for label, prefix in _PLAIN_HOME_PREFIXES.items():
        probe = prefix + _SYNTHETIC_ACCOUNT + "/CODE/x.py"
        m = rx.search(probe)
        assert m is not None and m.start() == 0, (
            f"the sanitize gate does not fire on the {label} home form - this is gh#247 "
            "reopened, and the shape it misses is the one every lane prints when it proves "
            "which tree its suite imported"
        )


def test_no_measured_placeholder_is_a_finding() -> None:
    """The five legitimate placeholders in this tree, kept as regression cases.

    A narrowing that reds one of these does not fail in CI on the file that changed - it
    fails on `status_writer.py`, `docs/INSTALL.md` and two unrelated fixtures at once, and
    the remedy someone reaches for first is editing those files to suit the gate. They are
    not the problem: the gate accommodates placeholder prose, not the other way round.
    """
    rx = _compiled_sanitize()
    caught = [(text, why) for text, why in _MEASURED_PLACEHOLDERS if rx.search(text)]
    assert not caught, (
        f"the sanitize gate now fires on legitimate placeholder prose: {caught}. The "
        "account shape lost one of its three guards - starts alphanumeric, at least two "
        "characters, not a placeholder word - and these five files would have to be "
        "mangled to get CI green again"
    )


def test_a_real_account_beginning_with_a_placeholder_word_is_still_a_finding() -> None:
    """The exemption is per-word, not per-prefix, and that distinction is the whole risk.

    Exempting the bare placeholder is fine; exempting everything that STARTS with it would
    quietly hand a real account an escape hatch. Derived over the parsed word set so a
    newly exempted word is covered the day it lands.
    """
    rx = _compiled_sanitize()
    for word in _placeholder_words():
        assert not rx.search("/home/" + word), (
            f"{word!r} is exempted by ci.yml but the pattern still fires on it as an "
            "account - the exemption and the pattern disagree"
        )
        assert rx.search("/home/" + word + "ef"), (
            f"an account merely BEGINNING with the placeholder {word!r} is not a finding - "
            "the guard after the placeholder alternation is missing or wrong, and every "
            "real account with that prefix now walks through the gate"
        )


def test_the_sanitize_pattern_does_not_match_its_own_workflow() -> None:
    """The gate must not be its own finding, or it fails the build on every commit.

    ci.yml keeps this true two ways: the product literals are split with shell `''`
    concatenation, and the path terms are written so their SOURCE text cannot match them -
    a `[` or a `(` sits where a username would. The second is easy to break by hand while
    still looking right, so it is checked rather than reasoned about. Line NUMBERS only in
    the message: printing the offending line would put the literal into a CI log.
    """
    rx = _compiled_sanitize()
    text = CI_WORKFLOW.read_text(encoding="utf-8", errors="replace")
    hits = [i for i, ln in enumerate(text.splitlines(), 1) if rx.search(ln)]
    assert not hits, (
        f"the sanitize pattern matches its own workflow at line(s) {hits} - the gate would "
        "fail every build on itself. A new term needs either the shell `''` split or a "
        "shape whose source text cannot match it"
    )


def test_the_sanitize_step_declares_exactly_one_pattern() -> None:
    """One pattern, reused by both halves - a second one derives a DIFFERENT gate.

    Stated as its own gate rather than left to `_sanitize_pattern_source`'s internal
    assertion, because the failure it guards is not a parse failure: two `pattern=` lines
    would run, the tree half and the body half would enforce different rules, and a body
    scan that quietly used the narrower one is precisely the gh#247 shape again.
    """
    lines = _sanitize_step_lines()
    assigns = [ln.strip() for ln in lines if ln.strip().startswith("pattern=")]
    assert len(assigns) == 1, (
        f"the sanitize step declares {len(assigns)} `pattern=` lines, expected 1 - the two "
        "halves of this gate would enforce different rules and nothing would say so"
    )
    script = "\n".join(lines)
    # DERIVED, not pinned to a number. An earlier version asserted "exactly twice" and went
    # red the moment a third surface was scanned - a test that fails when the gate gets
    # BETTER teaches the next author to loosen it, which is how a real regression gets
    # waved through later. The property that actually matters is that every grep in this
    # step reaches for the same variable, so no surface can drift onto its own rule.
    # Comment lines are dropped first: this step's script opens with a comment mentioning
    # `grep -P`, and counting it made the check red on a prose line rather than on a scan.
    greps = [ln for ln in script.splitlines()
             if "grep -" in ln and not ln.strip().startswith("#")]
    assert len(greps) >= 2, (
        f"the sanitize step runs {len(greps)} grep(s) - it should scan the tree AND the "
        "published pull-request text, and a single grep means one of those halves is gone"
    )
    reuses = script.count('-e "$pattern"')
    assert reuses == len(greps), (
        f"{len(greps)} grep(s) in the sanitize step but only {reuses} use `-e \"$pattern\"` - "
        "a scan that spells its own regex enforces a different rule from the one this "
        "module derives, and nothing anywhere would say so"
    )


def test_the_extra_pattern_extends_the_one_pattern_before_any_scan() -> None:
    """gh#821. The install-specific alternatives join from a repository variable, and that has
    to stay an EXTENSION of the one `pattern=`: appended once, only when the variable is set,
    before the first grep. Anything else lets the halves enforce different rules - the failure
    the test above exists for - arriving by a spelling (`pattern+=`) its count does not see.
    """
    lines = _sanitize_step_lines()
    header = "\n".join(lines)
    assert "EXTRA_PATTERN: ${{ vars.SANITIZE_EXTRA_PATTERN }}" in header, (
        "the sanitize step no longer reads the extra pattern from the repository variable "
        "through `env:` - the private alternatives would reach no scan at all"
    )
    script = [ln.strip() for ln in lines]
    assigns = [i for i, ln in enumerate(script) if re.match(r"pattern\+?=", ln)]
    extensions = [i for i in assigns if script[i].startswith("pattern+=")]
    assert len(assigns) == 2 and len(extensions) == 1, (
        f"expected the one `pattern=` declaration and one `pattern+=` extension, found "
        f"{[script[i] for i in assigns]}"
    )
    at = extensions[0]
    assert script[at] == 'pattern+="|$EXTRA_PATTERN"', script[at]
    assert script[at - 1] == 'if [ -n "${EXTRA_PATTERN:-}" ]; then', (
        "the extension is not guarded on a non-empty variable: an empty one would append a "
        "bare `|`, an empty alternative that matches every line of the tree"
    )
    first_grep = min(
        i for i, ln in enumerate(script) if "grep -" in ln and not ln.startswith("#")
    )
    assert at < first_grep, "the extra pattern is appended after a scan already ran without it"


#: The pull-request fields the gate scans, and the expression each must be read through.
#: BOTH are attacker-controlled and both are published; scanning one and not the other
#: would be an arbitrary line. gh#247 measured only a body, so the title is here on the
#: same-defect-same-seam argument rather than on a measurement, and that is said out loud.
_PR_TEXT_FIELDS = {
    "PR_BODY": "${{ github.event.pull_request.body }}",
    "PR_TITLE": "${{ github.event.pull_request.title }}",
}


def test_the_gate_reads_every_pr_text_field_through_env_and_never_interpolates_it() -> None:
    """gh#247's second half, and the injection guard that comes with it.

    A PR body and title are text an outside contributor writes.
    `${{ github.event.pull_request.body }}` inside `run:` is spliced into shell SOURCE
    before bash sees a quote, so text containing a quote and a semicolon runs as commands
    with the workflow's token. Through `env:` it is only ever data. This asserts BOTH
    directions - the env declaration is present AND no `${{ }}` survives anywhere in the
    script - because the safe form being present says nothing about the unsafe form being
    absent, and a step can perfectly well carry both.
    """
    lines = _sanitize_step_lines()
    run_at = [i for i, ln in enumerate(lines) if ln.strip() in ("run: |", "run: |-")]
    assert len(run_at) == 1, (
        f"expected exactly one `run:` block in the sanitize step, found {len(run_at)} - "
        "this gate cannot tell env from script and would assert about the wrong half"
    )
    header, script = "\n".join(lines[:run_at[0]]), "\n".join(lines[run_at[0]:])
    assert "${{" not in script, (
        "a `${{ }}` expression appears inside the sanitize step's `run:` script. Whatever it "
        "expands to is spliced into shell source before bash parses it; pass it through "
        "`env:` and read it as a quoted variable instead"
    )
    for var, expr in _PR_TEXT_FIELDS.items():
        assert f"{var}: {expr}" in header, (
            f"the sanitize step does not take {var} through `env:` - either that half is "
            "gone (gh#247's live instance was published text, which the tree half cannot "
            "see) or it is being read some other way"
        )
        assert f'"${var}"' in script, (
            f"the script does not read {var} as a quoted `\"${var}\"` - unquoted it "
            "word-splits, and the scan silently stops seeing whole lines"
        )


def test_the_body_half_is_event_gated_and_says_so_when_it_does_not_run() -> None:
    """A skipped scan must announce itself, or clean and did-not-run look identical.

    Gated inside the STEP rather than on the job, deliberately: the tree half has to keep
    running on push and workflow_dispatch, where there is no body to read. The else branch
    is required to name the event, so a log reader can tell "no literals in the body" from
    "there was no body".
    """
    lines = _sanitize_step_lines()
    header = "\n".join(lines)
    assert "EVENT_NAME: ${{ github.event_name }}" in header, (
        "the sanitize step does not take the event name through `env:`, so its body half is "
        "either ungated or gated on something this test cannot see"
    )
    gate_at = [i for i, ln in enumerate(lines) if 'if [ "$EVENT_NAME" = "pull_request" ]' in ln]
    assert len(gate_at) == 1, (
        f"expected exactly one `$EVENT_NAME` = pull_request guard, found {len(gate_at)} - the "
        "body half is either unconditional (it would scan an empty body on every push and "
        "report that as clean) or gated somewhere else"
    )
    after = lines[gate_at[0]:]
    assert any(ln.strip().startswith("else") for ln in after), (
        "the body half has no else branch - on a push it would skip silently, and a silent "
        "skip reads exactly like a clean scan in the log"
    )
    assert any("echo" in ln and "$EVENT_NAME" in ln for ln in after), (
        "nothing on the non-pull_request path echoes the event name, so the log cannot "
        "distinguish 'the body was clean' from 'there was no body to read'"
    )
    # Every published field is scanned INSIDE the one guard. A field read outside it would
    # scan an empty string on every push and log that as clean - a green from a scan that
    # had nothing to look at, which is the failure the else branch above exists to prevent.
    guarded = "\n".join(after)
    for var in _PR_TEXT_FIELDS:
        assert f'"${var}"' in guarded, (
            f"{var} is declared in `env:` but is not read inside the pull_request guard - "
            "either it is scanned unconditionally (clean on every push, for the wrong "
            "reason) or it is declared and never scanned at all"
        )


def test_the_tree_scan_excludes_dot_git_as_a_FILE_as_well_as_a_directory() -> None:
    """Both flags, and they are not redundant - they exclude different things.

    In a normal checkout `.git` is a directory and `--exclude-dir` covers it. In a git
    WORKTREE it is a FILE holding a `gitdir:` pointer to an absolute home path, which
    `--exclude-dir` cannot touch and which the widened pattern now matches. CI only ever
    sees the directory shape, so nothing there would fail - but CLAUDE.md tells
    contributors to run this gate locally before pushing, and every lane builds in a
    worktree. Without the file exclude they get a hit on an untracked pointer file every
    single time, which is how a gate teaches people to ignore it.

    No tracked file can be named `.git`, so this costs nothing on CI.
    """
    script = "\n".join(_sanitize_step_lines())
    assert "--exclude-dir=.git" in script, (
        "the tree scan no longer excludes the .git DIRECTORY - on CI it would scan every "
        "object and packfile in the history"
    )
    assert "--exclude=.git" in script, (
        "the tree scan no longer excludes a .git FILE - in a git worktree that file holds "
        "an absolute home path, so a lane running this gate locally before pushing gets a "
        "hit it cannot act on and learns to disregard the gate"
    )


def test_each_published_pr_field_reports_its_own_verdict() -> None:
    """Title and body fail with DIFFERENT messages, because the remedies differ.

    A contributor told only "personal literals found" greps the checkout, finds nothing,
    and concludes the gate is broken. The tree half already says "in tracked files" for
    exactly that reason; a published-text hit has to name WHICH field to edit, or the
    reader is left diffing a PR description against a title looking for the same string.
    """
    script = "\n".join(_sanitize_step_lines())
    errors = [ln for ln in script.splitlines() if "::error::sanitize gate:" in ln]
    assert len(errors) >= 3, (
        f"found {len(errors)} sanitize error lines, expected one per scanned surface "
        "(tracked files, PR title, PR body) - two surfaces sharing a message means one of "
        "them sends the reader to the wrong place"
    )
    assert len({ln.strip() for ln in errors}) == len(errors), (
        "two sanitize failures share the same message text, so a log cannot say which "
        "surface tripped"
    )
    for needle in ("TRACKED FILES", "PULL REQUEST TITLE", "PULL REQUEST BODY"):
        low = needle.lower()
        assert any(low in ln.lower() for ln in errors), (
            f"no sanitize error names {needle}, so a hit on that surface cannot tell the "
            "reader what to edit"
        )


# --------------------------------------------------------------------------- #
# gh#67 -- a DOCUMENTED call site is a call site, so bind it to the real signature
#
# `open_escalation` grew a required positional `task` and never had a `detail`
# parameter, but three documented call sites in the orchestrator skill still passed
# `detail=` and omitted `task=`. Anyone following the prose got
# `TypeError: open_escalation() got an unexpected keyword argument 'detail'` on the
# escalation path -- the code that runs when the run has ALREADY hit trouble, so the
# crash replaced a legible escalation with a traceback.
#
# Nothing checked it: the skills are prose, the function is code, and the two drifted
# for as long as it took a reader to notice. This gate binds the prose to the code with
# `inspect.signature`, so an unknown kwarg or a missing required parameter in a future
# doc edit fails CI instead of a run.
# --------------------------------------------------------------------------- #
#: Functions whose DOCUMENTED call sites are bound to their real signatures. Add a name
#: here and every first-party markdown call to it is gated.
_GATED_CALLEE_NAMES = ("open_escalation",)

#: Sentinel argument value. The gate checks ARITY and NAMES, never values: a doc call
#: site's arguments are prose (`<the ESCALATION block text above>`), not evaluable.
_ANY_ARG = object()


def _resolve_gated_callees() -> dict:
    from agentflow import status_writer

    return {"open_escalation": status_writer.open_escalation}


def _call_arg_lists(text: str, name: str) -> list:
    """(line number, raw argument string) for every `<name>(...)` in `text`.

    Finds the CLOSING paren by balanced scan rather than by regex, because documented
    calls wrap across lines and carry parens inside their own arguments (the deadlock
    question ends `(bootstrap.deadlock=true).`). Double-quoted spans are skipped so a
    comma inside a string literal is not read as an argument boundary.

    An occurrence with an EMPTY argument list -- `open_escalation()` in a sentence like
    "the orchestrator calls open_escalation() for each entry" -- is a name reference, not
    a call site, and is excluded. Binding those would fail on every prose mention of the
    function, which is not the drift this gate is for.
    """
    needle = name + "("
    out = []
    idx = 0
    while True:
        start = text.find(needle, idx)
        if start == -1:
            return out
        i = start + len(needle)
        depth, in_str = 1, False
        while i < len(text) and depth:
            ch = text[i]
            if in_str:
                if ch == "\\":
                    i += 1
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        if depth:
            raise AssertionError(
                f"unbalanced parentheses after {name}( at offset {start} - "
                "the documented call site is not parseable as a call"
            )
        args = text[start + len(needle):i - 1]
        if args.strip():
            out.append((text.count("\n", 0, start) + 1, args))
        idx = i


def _split_args(argstr: str) -> list:
    """Top-level comma split, ignoring commas inside (), <> and double-quoted spans.

    `<...>` is tracked because documented calls use it for placeholder values
    (`question=<the ESCALATION block text above>`); the depth is clamped at zero so a
    stray `>` in prose cannot drive it negative and disable splitting entirely.

    Single quotes are deliberately NOT treated as string delimiters: an apostrophe in
    doc prose is far more common inside these argument lists than a single-quoted Python
    literal, and treating one as an opening quote would swallow the rest of the call. A
    documented call site that needs a string literal writes it double-quoted.
    """
    args, buf = [], []
    paren = angle = 0
    in_str = False
    i = 0
    while i < len(argstr):
        ch = argstr[i]
        if in_str:
            if ch == "\\":
                buf.append(ch)
                i += 1
                if i < len(argstr):
                    buf.append(argstr[i])
                    i += 1
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "([{":
            paren += 1
        elif ch in ")]}":
            paren -= 1
        elif ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif ch == "," and paren == 0 and angle == 0:
            args.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    args.append("".join(buf))
    # Fail red rather than silently merging. A depth left OPEN at the end of the list --
    # an unmatched `<` (`retries < 3`), an unclosed paren, an odd number of `"` -- means
    # every top-level comma after it was swallowed and the trailing arguments were folded
    # into the previous one. The merged text still binds, so the gate would report GREEN
    # with an unknown kwarg sitting right there. That is the one failure direction this
    # module refuses; `_call_arg_lists` gives unbalanced parens the same treatment.
    if angle or paren or in_str:
        opened = ", ".join(
            n for n, v in (("'<'", angle), ("'('", paren), ('\'"\'', in_str)) if v
        )
        raise AssertionError(
            f"unbalanced {opened} in a documented argument list: {argstr.strip()!r} - "
            "the split would merge the trailing arguments into one and the signature "
            "check would pass over drift it never saw. Write the placeholder without a "
            "bare '<' (or balance the delimiter)."
        )
    return [a for a in args if a.strip()]


_KWARG = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)")


def _bind_documented_call(func, argstr: str) -> None:
    """Bind one documented argument list against `func`'s real signature.

    Raises TypeError for an unknown keyword or a missing required parameter -- the two
    ways a doc can drift from a signature and stay silent.
    """
    positional, keywords = [], {}
    for raw in _split_args(argstr):
        arg = raw.strip()
        m = _KWARG.match(arg)
        if m:
            keywords[m.group(1)] = _ANY_ARG
        else:
            positional.append(_ANY_ARG)
    inspect.signature(func).bind(*positional, **keywords)


def _documented_md_files() -> list:
    """Every first-party skill markdown file. Prose mentions inside `prompts/` are in
    scope for the same reason SKILL.md is: a reader follows whichever one they land on."""
    return [p for p in sorted(SKILLS_ROOT.rglob("*.md")) if "_vendored" not in p.parts]


def _gated_call_sites() -> list:
    """(relative path, line, callee name, argument string) for every gated call site."""
    sites = []
    for path in _documented_md_files():
        text = path.read_text(encoding="utf-8")
        for name in _GATED_CALLEE_NAMES:
            if name + "(" not in text:
                continue
            for line, args in _call_arg_lists(text, name):
                sites.append((path.relative_to(SKILLS_ROOT).as_posix(), line, name, args))
    return sites


def test_the_documented_call_site_matcher_actually_matches() -> None:
    """Guards the gate. The assertion below is a `not broken` over a derived list, and a
    matcher that stopped matching would report a clean sweep of an empty set - the shape
    of every vacuous gate. The orchestrator skill documents six real `open_escalation`
    calls; fewer than that means the parser regressed, not that the docs got shorter."""
    sites = _gated_call_sites()
    assert sites, "no documented open_escalation call site matched - the parser is broken"
    orch = [s for s in sites if s[0] == "orchestrator/SKILL.md"]
    assert len(orch) >= 6, (
        f"only {len(orch)} call sites parsed out of orchestrator/SKILL.md - expected at "
        "least the six the escalation paths document"
    )


def test_every_documented_call_site_binds_to_the_real_signature() -> None:
    """THE gh#67 CLASS. A documented call is a call: bind it or it is fiction.

    `inspect.signature(...).bind` is the whole check - it raises on an unknown keyword
    and on a missing required parameter, which are exactly the two drifts that shipped.
    """
    callees = _resolve_gated_callees()
    broken = []
    for rel, line, name, args in _gated_call_sites():
        try:
            _bind_documented_call(callees[name], args)
        except TypeError as exc:
            broken.append(f"{rel}:{line} {name}({args.strip()}) -> {exc}")
    assert not broken, (
        "documented call sites that cannot run against the shipped signature:\n"
        + "\n".join(broken)
    )


def test_the_signature_gate_catches_both_drift_directions() -> None:
    """A positive control, run against the REAL signature rather than a stub.

    (a) an unknown kwarg - the `detail=` that shipped in three places; (b) an omitted
    required parameter - the `task=` those same three sites dropped. A gate that only
    ever goes green proves nothing about the tree it is watching.

    Each direction is exercised ALONE first, because `bind` reports the missing argument
    before it ever looks at the unknown one: asserting on the shipped shape's message
    would only ever have proved direction (b).
    """
    func = _resolve_gated_callees()["open_escalation"]

    _bind_documented_call(func, "state, task=0, question=<the ESCALATION block text above>")

    with pytest.raises(TypeError, match="detail"):
        _bind_documented_call(func, "state, task=0, question=esc.question, detail=esc.asked")

    with pytest.raises(TypeError, match="task"):
        _bind_documented_call(func, "state, question=esc.question")

    # The shape that actually shipped carries both drifts at once. Which message wins is
    # an implementation detail of `bind`; that it raises at all is the contract.
    with pytest.raises(TypeError):
        _bind_documented_call(func, "state, question=esc.question, detail=esc.asked")


def test_the_argument_splitter_survives_the_shapes_the_docs_actually_use() -> None:
    """The splitter is the part that can fail SILENTLY - a mis-split turns one argument
    into two positionals, and while that happens to fail loudly here, a splitter that
    MERGED arguments instead would hide a real unknown kwarg. Pin the two shapes the
    orchestrator skill really writes: commas and parentheses inside a string literal,
    and an angle-bracket placeholder containing spaces."""
    quoted = 'state, task=0, question="Deadlock: no ready, no in-flight, " "not all done (x=true)."'
    assert _split_args(quoted) == [
        "state",
        " task=0",
        ' question="Deadlock: no ready, no in-flight, " "not all done (x=true)."',
    ]
    assert _split_args("state, task=tid, question=<the tier-3 question>") == [
        "state",
        " task=tid",
        " question=<the tier-3 question>",
    ]


def test_an_unbalanced_delimiter_fails_the_splitter_instead_of_merging_arguments() -> None:
    """The gate's own worst failure: passing green while the drift is on screen.

    A placeholder containing a bare `<` (a comparison, an unclosed generic, an HTML-ish
    fragment) used to raise the angle depth for the REST of the call, so every later
    top-level comma was swallowed and `detail=` was folded into the question argument.
    The merged text binds, and the gate reported GREEN with the exact gh#67 drift
    present. It must go red on the shape it cannot read.
    """
    hidden_drift = "state, task=esc.task, question=<the question when retries < 3>, detail=esc.asked"
    with pytest.raises(AssertionError, match="unbalanced"):
        _split_args(hidden_drift)

    # The same list with a balanced placeholder splits, and THEN the signature check is
    # the thing that catches `detail=` - proving the guard above is a second line and
    # not a replacement for the binding.
    readable = "state, task=esc.task, question=<the question after three retries>, detail=esc.asked"
    assert len(_split_args(readable)) == 4
    with pytest.raises(TypeError, match="detail"):
        _bind_documented_call(_resolve_gated_callees()["open_escalation"], readable)

    for shape in ('state, task=0, question="unterminated', "state, task=0, question=f(x"):
        with pytest.raises(AssertionError, match="unbalanced"):
            _split_args(shape)


def test_a_bare_name_mention_is_not_treated_as_a_call_site() -> None:
    """`open_escalation()` inside a sentence is a reference, not a call. Binding it would
    fail on every prose mention - a gate that fires on correct writing gets deleted."""
    prose = "the orchestrator calls `open_escalation()` for each entry"
    assert _call_arg_lists(prose, "open_escalation") == []
    real = "call `open_escalation(state, task=0, question=q)` now"
    assert _call_arg_lists(real, "open_escalation") == [(1, "state, task=0, question=q")]


# --------------------------------------------------------------------------- #
# E-0215: the fail-closed `validate` at wake, in the skills that EXECUTE the
# firing contract rather than only in the document that states it
# --------------------------------------------------------------------------- #
# `docs/REGISTRY.md` step 1 has required `validate --project <name>` first, fail-closed, since
# the contract was written. Measured across the loop skills on 2026-09-04, it was instructed in
# 0 of 7 of them. Five said the word only as a NEGATIVE - inside the phase-boundary clause
# "never the full step-1 wake read (no `validate`, no 4h strand check)" - so the skills
# PRESUPPOSED a step nothing performed, and any gate built on a whole-file word count would
# have read those five exclusions as compliance.
#
# Hence both properties below: anchor on each skill's own wake read, and require the CALL, not
# the word. The exclusion clause cannot contain `python -m agentflow.registry validate
# --project`, so a skill that only names `validate` in the negative still fails.

#: Where each loop's wake read actually begins. Deliberately per-skill: the seven files word
#: step 1 seven different ways (a numbered contract step, a `## Step 0.5` heading, a `0.` inside
#: `## Selection`, a `###` subsection), and pattern-matching one file's phrasing onto the others
#: is how a gate ends up asserting a shape only its author's file has. The completeness check
#: below makes the dict safe: a new lane with no anchor here FAILS rather than being skipped.
WAKE_READ_ANCHORS = {
    "lead-dev": "1. **WAKE READ** (lock-free).",
    "jr-dev-1": "0. **Resolve the project first, THEN the firing-contract wake read.**",
    "jr-dev-2": "0. **Resolve the project first, THEN the firing-contract wake read.**",
    "jr-dev-3": "0. **Resolve the project first, THEN the firing-contract wake read.**",
    "auditor": "## Step 0.5 " + chr(8212) + " Registry wake read",
    "steward": "### Validate FIRST, fail closed",
    "dev-manager": "1. **WAKE READ** (lock-free).",
}

#: The CALL, not the word. `--project` is part of it: an unscoped `validate` on a shared
#: data_root validates a sibling fleet's registry and passes while yours is corrupt.
_VALIDATE_CALL = "python -m agentflow.registry validate --project"

#: How far past the anchor step 1 is allowed to run. Wide enough for the longest of the seven
#: (dev-manager's, which carries the whole refusal path plus the read), far short of the
#: phase-boundary clause in every file.
_WAKE_READ_WINDOW = 1600


def _wake_read_window(skill: str) -> str:
    """The span of `skill`'s SKILL.md that IS its wake read, WHITESPACE-FLATTENED.

    Flattened because these files wrap at ~95 columns and the command straddles the break in
    three of the seven (`...registry validate\\n   --project <name>`). Matching the raw text
    would have passed the four that happen to fit on one line and failed the three that do not,
    for a reason that has nothing to do with whether the step is there.
    """
    text = _skill_text(skill)
    anchor = WAKE_READ_ANCHORS[skill]
    at = text.find(anchor)
    assert at != -1, (
        f"{skill}/SKILL.md no longer contains its wake-read anchor {anchor!r}. The wake read "
        "moved or was reworded; re-anchor this gate on the real step 1 rather than widening it, "
        "or it goes back to being a whole-file word count."
    )
    return " ".join(text[at:at + _WAKE_READ_WINDOW].split())


def test_the_wake_read_roster_covers_every_loop() -> None:
    """The dict above is a hand-written list, and a list only guards what someone thought of.

    This is the exact failure the roster fix upstream addresses one level down: `ALL_LOOPS` was
    a six-tuple for a seven-lane fleet, and every gate keyed on it went quiet about jr-dev-3
    while staying green. Bind the anchors to the derived roster so the same thing cannot happen
    here.
    """
    assert set(WAKE_READ_ANCHORS) == set(ALL_LOOPS), (
        "WAKE_READ_ANCHORS and the derived loop roster disagree: "
        f"missing anchors {sorted(set(ALL_LOOPS) - set(WAKE_READ_ANCHORS))}, "
        f"anchors for non-loops {sorted(set(WAKE_READ_ANCHORS) - set(ALL_LOOPS))}"
    )


def test_the_loop_roster_matches_the_tree() -> None:
    """`ALL_LOOPS` is imported; the SKILL.md files are read off disk. Prove they are one tree.

    Locally the suite can import an editable install rooted somewhere other than the checkout
    under test, and a roster from tree A gating skills in tree B is a gate that reports coverage
    it does not have.
    """
    missing = [loop for loop in ALL_LOOPS if not (SKILLS_ROOT / loop / "SKILL.md").is_file()]
    assert not missing, (
        f"the roster names loops with no SKILL.md under {SKILLS_ROOT}: {missing} - the imported "
        "`agentflow.registry` and the skills being gated are not the same tree"
    )


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_every_loop_runs_validate_at_wake(skill: str) -> None:
    """Firing-contract step 1, in the file the lane actually executes from."""
    window = _wake_read_window(skill)
    assert _VALIDATE_CALL in window, (
        f"{skill}/SKILL.md's wake read never runs `{_VALIDATE_CALL} <name>`. "
        "docs/REGISTRY.md step 1 requires it first and fail-closed; a lane whose skill omits it "
        "builds on whatever the registry says, corrupt or not. Naming `validate` in the "
        "phase-boundary exclusion is not this step - that clause is the NEGATIVE half."
    )


#: The remedy each lane has, by what the CLI actually grants it. `repair` is `steward`-only
#: (`ACTOR_CAPABILITY["repair"].actors == ("steward",)`, rule `repair_steward`), so the other six
#: have exactly one move and it is a ROUTED Note. `--to steward` and not the bare word: without
#: the flag `_cmd_note` routes fleet-wide, the steward's consume step reads only Notes routed TO
#: it, and the violation is dropped in silence - the same defect as the starvation Note above.
#: A bare mention of "steward" is no evidence at all: dev-manager's wake read already says
#: "the sweep that reads them is the steward's" for an unrelated reason, and a `steward in window`
#: check passed with the whole remedy clause deleted (mutation-proven).
_REMEDY_CALL = {loop: "--to steward" for loop in ALL_LOOPS}
_REMEDY_CALL["steward"] = "repair --actor steward"

_FAIL_CLOSED = re.compile(r"fail(?:ing|s)? closed", re.I)


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_every_loop_states_the_refusal_path(skill: str) -> None:
    """"Run validate" without a refusal is decoration: the contract's word is FAIL-CLOSED.

    Three things have to be in the same breath as the call, or the step reads as advisory: the
    exit code that means corrupt, the refusal itself, and the one remedy that lane actually has.
    """
    window = _wake_read_window(skill)
    assert "exit 2" in window, (
        f"{skill}/SKILL.md's wake-read validate step never names `exit 2`, the code that means "
        f"the registry is corrupt - so nothing tells the lane what it is failing on: {window[:400]}"
    )
    assert _FAIL_CLOSED.search(window), (
        f"{skill}/SKILL.md's wake-read validate step does not say it is fail-closed. Running the "
        f"check and proceeding anyway is the pre-epic behaviour with an extra command: {window[:400]}"
    )
    remedy = _REMEDY_CALL[skill]
    assert remedy in window, (
        f"{skill}/SKILL.md's wake-read validate step never names its remedy `{remedy}`. "
        f"A refusal with nowhere to send the violation strands the fleet on a corrupt board "
        f"until someone trips over it - which is the outcome step 1 exists to prevent: {window[:400]}"
    )


#: A `status` write the CLI will actually accept. `--state halted`/`waiting` are REFUSED
#: (`agent_waiting_on`: they require a live `Q-`/`B-` referent) and the remedy this step
#: prescribes is a routed Note, so there is no id to name. Measured against the shipped CLI:
#: `status --actor lead-dev --state halted --phase "..."` -> exit 2, `refused
#: [agent_waiting_on]`; the same call with `--state idle ... --eta <ISO>` -> exit 0.
_STAMP_ACCEPTABLE = re.compile(r"--eta|--waiting-on")


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_the_refusal_stamp_is_a_command_the_cli_accepts(skill: str) -> None:
    """The stamp must be RUNNABLE, not merely present.

    The first build of this epic shipped `status --state halted --phase "..."` into six skills.
    Every assertion above passed on it - `exit 2` was named, the fail-closed phrase was there,
    the remedy was there - and the command itself is refused by the CLI with `agent_waiting_on`,
    because `halted` demands a live `Q-`/`B-` id and the remedy here is a Note, so none exists.
    A lane following it would route the violation correctly and then fail its own stamp.

    That is this epic's own defect class arriving inside its fix: an instruction nothing can
    execute, shipped to seven files, under a gate that could not see it. So the gate now reads
    the stamp's ARGUMENTS, not just its vocabulary.
    """
    window = _wake_read_window(skill)
    stamp = [ln for ln in window.splitlines() if "--state" in ln and "status" in window]
    assert stamp, f"{skill}/SKILL.md's wake-read step has no `status` stamp at all: {window[:300]}"
    if "--state halted" in window or "--state waiting" in window:
        assert "--waiting-on" in window, (
            f"{skill}/SKILL.md stamps the refusal with `--state halted`/`waiting` and no "
            f"`--waiting-on`. The CLI REFUSES that (`agent_waiting_on`) - and this step's remedy "
            f"is a routed Note, so there is no live Q-/B- id to supply. Use `--state idle "
            f"--phase ... --eta <ISO>`, which is measured to exit 0."
        )
    assert _STAMP_ACCEPTABLE.search(window), (
        f"{skill}/SKILL.md's refusal stamp carries neither `--eta` nor `--waiting-on`. Every "
        f"accepted `status` write needs one: `--eta` is mandatory except on `halted`, and "
        f"`halted` needs `--waiting-on`. As written the lane's own stamp exits 2."
    )


def test_the_validate_gate_rejects_the_exclusion_clause() -> None:
    """The mutation this gate exists to survive, run as a test rather than trusted.

    Before E-0215 five skills contained the token `validate` and none ran it. Feed the gate a
    wake read carrying ONLY that sentence: a word-count gate passes it, this one must not.
    """
    exclusion_only = (
        "1. **WAKE READ** (lock-free). Read the ENTIRE registry. The boundary is a NARROWED "
        "steps 1+2+4 - never the full step-1 wake read (no `validate`, no 4h strand check)."
    )
    assert "validate" in exclusion_only, "the fixture must contain the bare word, or it proves nothing"
    assert _VALIDATE_CALL not in exclusion_only, (
        "the exclusion clause satisfied the gate's own predicate - the gate is a word count "
        "wearing a call site's name"
    )


# --------------------------------------------------------------------------------------
# Seat fatigue: the delegation line, in every seat and in the prompts one level down
# --------------------------------------------------------------------------------------
#
# WHY THIS IS A GATE AT ALL. The line is a standing instruction from the human, and the whole
# value of it is that it reaches a seat that is ALREADY tired - which means it has to be in the
# file that seat re-reads every wake, in all of them, not in the ones somebody remembered. Eleven
# files is exactly the count where a copy-paste pass misses one and nobody notices for a quarter.
#
# ANCHORED ON THE SENTENCE, NEVER ON A KEYWORD. These files are full of the words "subagent" and
# "Workflow" for unrelated reasons - lead-dev alone dispatches both a dozen times - so a keyword
# check would report full coverage over a tree where the rule was never written down.
# `test_the_focus_gate_is_not_a_keyword_match` pins that distinction as a test rather than a
# comment, because it is the one way this gate could go quietly vacuous.

#: VERBATIM, and identical in all eleven files. It is the human's own wording (2026-09-03); a
#: rephrase in one file is the failure this checks for, not a stylistic variation.
FOCUS_LINE = "Exhaust will degrade your senses, use subagents and workflows to keep yourself focused."


def _orchestrator_focus_docs() -> list[Path]:
    """The subagent-facing docs one level below the seats, DERIVED not listed.

    The seats are gated off `ALL_LOOPS`, which cannot reach these: they are prompts and an agent
    definition, not loops. Left ungated they are the drift surface - the same line, in files no
    roster names, edited by whoever is next in `skills/orchestrator/`. So the set is a glob over
    the implementer prompts plus the bootstrap agent, and `test_the_orchestrator_focus_set_is_real`
    asserts it is non-empty BEFORE anything is asserted about its members: a glob that matches
    nothing parametrizes to nothing and reads exactly like a clean pass.
    """
    prompts = sorted(SKILLS_ROOT.glob("orchestrator/prompts/implementer-with-*.md"))
    return [*prompts, SKILLS_ROOT / "orchestrator" / "agents" / "orc-bootstrap.md"]


ORCHESTRATOR_FOCUS_DOCS = [
    p.relative_to(SKILLS_ROOT).as_posix() for p in _orchestrator_focus_docs()
]

#: How much of what FOLLOWS the line is compared between seats. The two how-sentences come first
#: and run ~350 chars; the shared closing rule comes after, so this window is dominated by the
#: per-seat half. Wide enough to catch a copy-paste, narrow enough not to drag the shared tail in.
_HOW_WINDOW = 380


@pytest.mark.parametrize("skill", ALL_LOOPS)
def test_every_loop_carries_the_focus_line(skill: str) -> None:
    """Every seat, not the six the spec listed - `ALL_LOOPS` is `AGENTS`, so jr-dev-3 is in."""
    assert FOCUS_LINE in _skill_text(skill), (
        f"{skill}/SKILL.md does not carry the seat-fatigue line verbatim: {FOCUS_LINE!r}. It is a "
        "standing instruction to every seat, and a seat that never reads it is the one that will "
        "push through a long wake by hand"
    )


@pytest.mark.parametrize("rel", ORCHESTRATOR_FOCUS_DOCS)
def test_the_orchestrator_subagent_docs_carry_the_focus_line(rel: str) -> None:
    """A subagent one level down hits the same wall, and no loop roster covers these files."""
    path = SKILLS_ROOT / rel
    assert path.is_file(), f"{rel} is in the derived focus set but not on disk"
    assert FOCUS_LINE in path.read_text(encoding="utf-8", errors="replace"), (
        f"skills/{rel} does not carry the seat-fatigue line verbatim: {FOCUS_LINE!r}"
    )


def test_the_orchestrator_focus_set_is_real() -> None:
    """The non-vacuity guard the parametrized case above cannot perform on itself."""
    prompts = [rel for rel in ORCHESTRATOR_FOCUS_DOCS if "/prompts/" in rel]
    assert len(prompts) >= 2, (
        "the implementer-prompt glob matched fewer than the two prompts that exist "
        f"(`implementer-with-tdd-loop.md`, `implementer-with-verifier-loop.md`): {prompts}. A glob "
        "that matches nothing parametrizes to nothing and reports as a pass"
    )
    assert "orchestrator/agents/orc-bootstrap.md" in ORCHESTRATOR_FOCUS_DOCS, (
        "the bootstrap agent dropped out of the focus set; it is the read-only pass that exists "
        "precisely so the orchestrator does not burn its own context, so it carries the line too"
    )


def test_the_how_sentences_are_written_per_seat() -> None:
    """The line is shared; the two sentences after it are not, and must not become shared.

    The point of those sentences is that they name steps THAT SEAT ALREADY HAS - lead-dev's
    Playwright gate, the auditor's Step 2 fan-out, the steward's rotating heavy domain. Pasted
    across seven files they degrade into a slogan, which is how a rule stops being read. Compared
    as a set rather than pairwise so the failure names every seat sharing a body, not one pair.
    """
    windows: dict[str, str] = {}
    for skill in ALL_LOOPS:
        text = _skill_text(skill)
        at = text.find(FOCUS_LINE)
        # `.index` here raised a bare ValueError before any assertion ran, so a seat missing the
        # line failed this case with no diagnostic at all -- the sibling case named the real
        # defect, and this one contributed noise beside it. A test's failure text is the only
        # part of it anyone reads.
        assert at != -1, (
            f"{skill}/SKILL.md does not carry the seat-fatigue line, so its per-seat sentences "
            "cannot be compared -- fix that first; test_every_loop_carries_the_focus_line names it"
        )
        start = at + len(FOCUS_LINE)
        windows[skill] = " ".join(text[start:start + _HOW_WINDOW].split())
    shared: dict[str, list[str]] = {}
    for skill, window in windows.items():
        shared.setdefault(window, []).append(skill)
    duplicated = [seats for seats in shared.values() if len(seats) > 1]
    assert not duplicated, (
        f"these seats carry identical text after the seat-fatigue line: {duplicated}. The two "
        "sentences after it are per-seat by design - they cite that seat's own procedure - and a "
        "copy-paste makes the rule generic advice the reader skips"
    )


def test_the_focus_gate_is_not_a_keyword_match() -> None:
    """A file that talks about subagents all day still has to carry the sentence.

    Written as a test because this is the cheap way to build the gate wrong: `"subagent" in text`
    passes on all eleven files today, with the line in none of them.
    """
    decoy = (
        "Dispatch a read-only subagent per epic, and use a `Workflow` when the window is wide. "
        "Exhaust will degrade your senses - stay focused."
    )
    assert "subagent" in decoy and "Workflow" in decoy, "the fixture must look like a real skill"
    assert FOCUS_LINE not in decoy, (
        "a near-miss paraphrase satisfied the gate; it must anchor on the whole sentence, because "
        "a reworded rule is exactly what eleven hand-edited files drift into"
    )


# --------------------------------------------------------------------------------------
# E-0213: the `wiki/` save duty, in the FIVE seats that carry it — and in no others
# --------------------------------------------------------------------------------------
#
# WHY BOTH HALVES ARE GATED. A wiring epic that silently WIDENS is this repo's recurring failure
# class, and it is invisible from the inclusion side: five seats gaining the duty looks identical
# whether two more gained it or not. So the exclusion is asserted as hard as the inclusion, by a
# case that reds if a later author pastes the duty block into `auditor` or `steward`.
#
# WHY A LITERAL IS CORRECT HERE, AND WHY ONLY ONE. The saving seats are a PRODUCT DECISION
# (<person.name>, 2026-09-05: "only builders and dev manager") and no predicate in this codebase
# distinguishes them — they are not "the self-waking seats", which is a set that includes the
# auditor and the steward and excludes dev-manager. So the list is stated once, and the exclusion
# is DERIVED from it against the product roster. Two hand-written tuples would put an eighth lane
# in NEITHER set, every case here would still pass, and the new seat's duty would be a question
# nobody was asked. That is the same defect `ALL_LOOPS` was already fixed for, one level down.

#: The decision, stated ONCE. Not derivable, so it is a literal — and it is the only literal here.
SAVING_SEATS = ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "dev-manager")

#: DERIVED, so an eighth lane joining `AGENTS` lands here automatically and is asserted NOT to
#: have the duty from the moment it exists. Adding a lane therefore forces a decision instead of
#: letting a new seat fall silently into neither set.
NON_SAVING_SEATS = tuple(a for a in ALL_LOOPS if a not in SAVING_SEATS)

#: Where each seat's duty is written, in its own procedure. Per-seat because the five files word
#: their phase boundary five ways — a mid-section paragraph in the primary's §7, a bullet under
#: "Division of labor" in each junior, a numbered section of its own in dev-manager. Bound to
#: `SAVING_SEATS` by the completeness case below, so a renamed seat FAILS rather than skips.
SAVE_DUTY_ANCHORS = {
    "lead-dev": "**The project's `wiki/` page",
    "jr-dev-1": "**The `wiki/` page is yours to write",
    "jr-dev-2": "**The `wiki/` page, at whichever of the four boundaries",
    "jr-dev-3": "**The `wiki/` page " + chr(8212) + " one per OUTCOME",
    "dev-manager": "### 11. The `wiki/` page",
}

#: The ONE licensed entry. Q-10043 put the bounded one-shot in a single module that owns the
#: non-recursion marker, the timeout, the in-flight guard and the audit record; a seat that
#: assembles its own command line inherits none of them. Anchoring on the function name rather
#: than on the word "save" is the difference between a call site and a mention.
_SAVE_CALL = "compose_and_save"

#: The four decided boundaries (`save_summariser.TRIGGERS`), for the seats whose firings are epic
#: builds. Each named separately for the reason `test_save_capability.py` already records: written
#: as a disjunction, any one surviving alternative keeps the sentence true and the other three can
#: be deleted with nothing reddening.
_BUILDER_TRIGGER_TOKENS = ("end-of-epic", "park", "halt", "block")

#: dev-manager's own vocabulary. It does not self-wake and its firings are human conversations,
#: so the builders' four do not map onto it: forcing them on would give it a duty that never
#: fires, or one that fires on a boundary it does not have, which reads as coverage. Its moments
#: are a question answered, a Directive transcribed, an epic promoted, a blocker disposed.
_DEV_MANAGER_MOMENT_TOKENS = ("question answered", "Directive transcribed", "promoted", "disposed")

SEAT_TRIGGER_TOKENS = {seat: _BUILDER_TRIGGER_TOKENS for seat in SAVING_SEATS}
SEAT_TRIGGER_TOKENS["dev-manager"] = _DEV_MANAGER_MOMENT_TOKENS

#: The ordering constraint, as an ALTERNATION over the qualifier and a single load-bearing noun.
#: The five files say it five ways on purpose — a verbatim block pasted into five differently
#: voiced procedures is what this epic was told not to do — so the gate fixes the noun
#: ("end-state writes") and requires a qualifier that puts the save AFTER them in the same breath.
#: The noun alone would pass a sentence saying the opposite.
_ORDERING = re.compile(
    r"(?:never (?:blocks?|before|instead of)|after)[^.]{0,120}end-state writes", re.I
)

#: The empty case, which the wiring must not quietly convert into a duty. Two halves: the status
#: the module returns, and the instruction that the seat writes nothing.
#:
#: THE SECOND HALF IS CO-LOCATED, and it was not at first. A bare `writes? nothing` over the
#: window passed a mutation that deleted dev-manager's empty-case rule outright, because an
#: unrelated sentence forty lines away — "a seat that invents a fifth trigger value writes nothing
#: and only an audit line says why" — satisfied it. Same shape as every keyword gate this file
#: already carries a scar from: require the two halves in one breath, in either order, without
#: crossing a sentence boundary.
_NOTHING_DURABLE_STATUS = re.compile(r"nothing[- ]durable", re.I)
_WRITES_NOTHING = re.compile(
    r"nothing[- ]durable[^.]{0,120}writes?\s+(?:\*\*)?nothing"
    r"|writes?\s+(?:\*\*)?nothing[^.]{0,120}nothing[- ]durable",
    re.I,
)

#: How far past the anchor the duty may run. The next top-level heading bounds it first in all
#: five files today (measured 2026-09-24 after gh#454's failed-save bullet: 3100-4100 chars), so
#: this is the backstop for a future edit that deletes the following heading — not the primary
#: bound. Set above the longest real section rather than at a round number: a cap that truncates a
#: live section silently drops the last constraint in it, which is exactly how the empty-case rule
#: went missing when this was 3000 — and dev-manager's section had already outgrown 3500 (3546)
#: before gh#454, so its last bullet was being cut off.
_SAVE_DUTY_CAP = 4600


def _save_duty_window(skill: str) -> str:
    """The seat's own duty text: anchor -> next `## ` heading, capped."""
    anchor = SAVE_DUTY_ANCHORS[skill]
    text = _skill_text(skill)
    at = text.find(anchor)
    assert at != -1, (
        f"{skill}/SKILL.md no longer contains its save-duty anchor {anchor!r}. The duty moved or "
        "was reworded; re-anchor this gate on the real phase-boundary text rather than widening "
        "it to the whole file, or it stops being a call-site check"
    )
    end = text.find("\n## ", at)
    stop = at + _SAVE_DUTY_CAP if end == -1 else min(end, at + _SAVE_DUTY_CAP)
    return text[at:stop]


def test_the_saving_seat_roster_partitions_the_fleet() -> None:
    """The two sets must cover the roster exactly, with nothing in both and nothing in neither.

    This is the case that makes the derived shape worth having. An eighth lane added to `AGENTS`
    lands in `NON_SAVING_SEATS` and is immediately asserted not to have the duty; if someone
    instead adds it to `SAVING_SEATS`, the anchor case below demands its duty text exists. Either
    way the lane cannot arrive undecided.
    """
    unknown = [s for s in SAVING_SEATS if s not in ALL_LOOPS]
    assert not unknown, (
        f"SAVING_SEATS names seats that are not in the product roster: {unknown}. A typo here is "
        "a seat gated by nothing, with every case below still green"
    )
    assert set(SAVING_SEATS) | set(NON_SAVING_SEATS) == set(ALL_LOOPS)
    assert not set(SAVING_SEATS) & set(NON_SAVING_SEATS)
    assert NON_SAVING_SEATS, (
        "every loop in the roster is a saving seat, so the exclusion case below parametrizes to "
        "nothing and reports as a clean pass. That is precisely the widening this gate exists to "
        "catch, arriving through the gate itself"
    )
    # The DECISION, pinned. Derivation keeps the sets consistent; it cannot keep them CORRECT.
    # <person.name> excluded these two by name on 2026-09-05, so moving either into SAVING_SEATS
    # reds here and has to be argued rather than merged.
    for excluded in ("auditor", "steward"):
        assert excluded in NON_SAVING_SEATS, (
            f"{excluded} is no longer excluded from the wiki-save duty. It was excluded by name "
            '("only builders and dev manager", 2026-09-05); the auditor is read-only against the '
            "product and the steward owns `digest.md`. Widening this is a product decision, not a "
            "refactor"
        )


def test_the_save_duty_anchors_cover_every_saving_seat() -> None:
    """The anchors are hand-written, and a hand-written list only guards what someone thought of."""
    assert set(SAVE_DUTY_ANCHORS) == set(SAVING_SEATS), (
        "SAVE_DUTY_ANCHORS and SAVING_SEATS disagree: "
        f"missing anchors {sorted(set(SAVING_SEATS) - set(SAVE_DUTY_ANCHORS))}, "
        f"anchors for non-saving seats {sorted(set(SAVE_DUTY_ANCHORS) - set(SAVING_SEATS))}"
    )


@pytest.mark.parametrize("skill", SAVING_SEATS)
def test_the_seat_calls_the_module_at_its_own_boundary(skill: str) -> None:
    """Prose that names the duty but no call site is the failure this repo already has a name for.

    Two halves. The seat must name `compose_and_save` — not describe a save in the abstract, and
    not assemble a command line of its own — and it must pass its OWN `lane=`, which is what files
    the page under that seat's `hot-<lane>.md` projection instead of a sibling's.

    WHAT THE NAME-ANCHOR PROVES, stated because it is weaker than it looks. It is the NAME, not
    `compose_and_save(` with a paren: three of the five seats deliberately carry no fence, because
    the juniors inherit the primary's call shape wholesale and four near-identical fences is the
    copy-paste this epic was told not to produce. So a mention anywhere in the duty satisfies it —
    mutation-measured: renaming only the call while leaving the import passes, and only removing
    every mention reds. What it does catch is the regression that matters, a seat whose duty never
    names the licensed entry at all and is therefore free to reach the summariser its own way.
    """
    window = _save_duty_window(skill)
    assert _SAVE_CALL in window, (
        f"{skill}/SKILL.md's save duty never names `{_SAVE_CALL}`. A duty with no call site is "
        "prose: the module owns the non-recursion marker, the timeout, the in-flight guard and "
        "the audit record, and a seat that reaches the summariser any other way inherits none of "
        f"them. Window: {window[:300]!r}"
    )
    assert f'lane="{skill}"' in window, (
        f'{skill}/SKILL.md\'s save duty does not pass `lane="{skill}"`. The lane drives '
        "`hot-<lane>.md`, so a missing or copied lane files this seat's pages into another seat's "
        "projection - and `pages/` is create-only, so it cannot be moved back out"
    )


@pytest.mark.parametrize("skill", SAVING_SEATS)
def test_the_seat_names_every_one_of_its_own_triggers(skill: str) -> None:
    """Per-trigger, never as a disjunction — and dev-manager's set is its own (decision 3)."""
    window = _save_duty_window(skill)
    missing = [t for t in SEAT_TRIGGER_TOKENS[skill] if t.lower() not in window.lower()]
    assert not missing, (
        f"{skill}/SKILL.md's save duty does not name {missing}. Each boundary is its own trigger "
        "and none implies the others: written as a disjunction, any one surviving alternative "
        "keeps the sentence true while the rest are deleted with nothing reddening"
    )


def test_dev_manager_carries_a_trigger_set_of_its_own_and_says_why() -> None:
    """Decision 3, asserted rather than trusted to survive the next copy-paste.

    dev-manager does not self-wake and its firings are human conversations, so *end-of-epic /
    park / halt / block* are not its boundaries. The cheap way to get this wrong is to paste the
    builder block into it, which would produce a duty that never fires — and a duty that never
    fires reads on every board exactly like one that does.
    """
    window = _save_duty_window("dev-manager")
    assert set(_DEV_MANAGER_MOMENT_TOKENS) != set(_BUILDER_TRIGGER_TOKENS), (
        "the two vocabularies are the same set, so this gate asserts nothing"
    )
    assert re.search(r"not the builders|its own|differs", window, re.I), (
        "dev-manager/SKILL.md's save duty never says its trigger set DIFFERS from the builders'. "
        "The spec asks for the difference and the REASON, because a seat that reads four familiar "
        "boundary names will apply them"
    )
    assert re.search(r"self-wake|conversation", window, re.I), (
        "dev-manager/SKILL.md states a different trigger set without the structural reason for "
        "it - that it does not self-wake and its firing is a human conversation, so an epic "
        "build's boundaries are not available to it. A difference with no reason is reverted by "
        "the next author who notices the asymmetry"
    )


@pytest.mark.parametrize("skill", SAVING_SEATS)
def test_the_save_never_precedes_the_firings_real_end_state_writes(skill: str) -> None:
    """The constraint that keeps a memory step from costing a build.

    `compose_and_save` cannot raise into the caller, so the risk is not an exception — it is a
    seat that spends its last context composing a page and never writes the registry row, the
    state file or the ntfy that the rest of the fleet reads.
    """
    window = _save_duty_window(skill)
    assert _ORDERING.search(window), (
        f"{skill}/SKILL.md's save duty never puts the page AFTER the firing's end-state writes. "
        "The registry row, the state file and the ntfy are what the rest of the fleet reads; a "
        f"page is owed after them, never instead of them. Window: {window[:300]!r}"
    )


@pytest.mark.parametrize("skill", SAVING_SEATS)
def test_the_empty_case_survives_the_wiring(skill: str) -> None:
    """A firing with nothing durable writes NOTHING, and the wiring must not quietly repeal it.

    This is the rule most likely to be lost in translation from a capability to a duty: a
    checklist line reads as "produce a page", and a seat that believes it owes one every firing
    manufactures pages that rank in the hot tier above the ones that were earned.
    """
    window = _save_duty_window(skill)
    assert _NOTHING_DURABLE_STATUS.search(window), (
        f"{skill}/SKILL.md's save duty never names the `nothing-durable` outcome, so nothing "
        "tells this seat that most firings correctly write no page"
    )
    assert _WRITES_NOTHING.search(window), (
        f"{skill}/SKILL.md names the empty outcome without instructing the seat, in the same "
        "breath, to WRITE NOTHING. Naming a status is not an instruction, and the empty case has "
        "to stay cheaper than the written one or a tired seat learns to skip the step entirely. "
        "The two halves must sit in one sentence: a `writes nothing` elsewhere in the section is "
        "how this gate passed a mutation that had deleted the rule"
    )


#: gh#454: `failed` is a distinct status (`save_summariser.STATUSES`) precisely so it never renders
#: as the empty case — but a seat told only that "`nothing-durable` discharges it" treats every
#: non-`wrote` outcome as discharge, and a broken summariser silently ends the wiki duty for good.
#: The decided action (Q-10089): record `save failed: <outcome.reason>` in the seat's own
#: `## ON WAKE` block and route ONE registry Note naming the trigger and reason. Each piece named
#: separately, for the same reason the trigger tokens are: a disjunction lets three be deleted.
_FAILED_NOT_DISCHARGED = re.compile(
    r"`failed`[^.]{0,160}(?:does not|doesn't|never)\b[^.]{0,60}discharge", re.I
)
_FAILED_RECORD_LINE = re.compile(r"save failed: <outcome\.reason>")
_ON_WAKE = re.compile(r"## ON WAKE")
#: dev-manager is interactive and keeps no `## ON WAKE` block (its state file is report-written),
#: so its record line goes in the session report the human reads instead.
_SESSION_REPORT = re.compile(r"session report")


def _failed_record_home(seat: str) -> tuple[re.Pattern[str], str]:
    """Where *seat* records the failure line, and how to name it in a gap message."""
    if seat == "dev-manager":
        return _SESSION_REPORT, "this session's report"
    return _ON_WAKE, "the seat's own `## ON WAKE` block"


def _failed_note(seat: str) -> re.Pattern[str]:
    """The one routed Note, from this seat, to the seat that owns fleet maintenance.

    A seat never notes itself, so the steward would route to dev-manager — but the steward is not
    a saving seat (see the roster case), so today every saving seat routes to the steward.
    """
    to = "dev-manager" if seat == "steward" else "steward"
    return re.compile(
        rf"registry note[^`]*--actor {re.escape(seat)}[^`]*--project[^`]*--to {to}\b"
    )


def _failed_branch_gaps(window: str, seat: str) -> list[str]:
    """Every piece of the `failed` branch that *window* does not carry. Empty means covered."""
    gaps = []
    if not _FAILED_NOT_DISCHARGED.search(window):
        gaps.append("a `failed` outcome stated as NOT discharging the duty")
    if not _FAILED_RECORD_LINE.search(window):
        gaps.append("the record line `save failed: <outcome.reason>`")
    home, home_name = _failed_record_home(seat)
    if not home.search(window):
        gaps.append(f"where the line goes: {home_name}")
    if not _failed_note(seat).search(window):
        gaps.append(f"the one routed Note (`registry note --actor {seat} ... --to ...`)")
    return gaps


@pytest.mark.parametrize("skill", SAVING_SEATS)
def test_a_failed_save_is_recorded_and_routed_not_discharged(skill: str) -> None:
    """gh#454: the seat must read `outcome.status`, and `failed` must not pass for `nothing-durable`."""
    gaps = _failed_branch_gaps(_save_duty_window(skill), skill)
    assert not gaps, (
        f"{skill}/SKILL.md's save duty has no complete `failed` branch; missing: {gaps}. "
        "`compose_and_save` never raises, so a failed save is only visible if the seat reads the "
        "status — otherwise it silently discharges the wiki duty on every firing it breaks"
    )


def _save_checklist_item(skill: str) -> str:
    """The end-of-firing checklist item that names the save call."""
    text = _skill_text(skill)
    for item in text.split("\n- [ ] ")[1:]:
        body = item.split("\n\n", 1)[0]
        if _SAVE_CALL in body:
            return body
    raise AssertionError(f"{skill}/SKILL.md has no end-of-firing checklist item naming `{_SAVE_CALL}`")


@pytest.mark.parametrize("skill", SAVING_SEATS)
def test_the_save_checklist_line_names_the_failed_status(skill: str) -> None:
    """The checklist is what a tired seat actually reads; "`nothing-durable` discharges it" alone
    teaches that any outcome discharges it."""
    item = _save_checklist_item(skill)
    assert "`failed`" in item, (
        f"{skill}/SKILL.md's save checklist line never names the `failed` status, so the line a "
        f"seat ticks at close-out says nothing about a failed save. Item: {item[:300]!r}"
    )


def test_the_failed_branch_gate_flags_a_paragraph_without_it() -> None:
    """Positive control: the planted pre-gh#454 shape must red, the fixed shape must pass."""
    without = (
        "- **After the end-state writes, never fatal.** `compose_and_save` returns an outcome "
        "rather than raising, and a firing that learned nothing durable writes **nothing**. "
        "`nothing-durable` discharges it.\n"
    )
    assert _SAVE_CALL in without and _WRITES_NOTHING.search(without), "fixture must be realistic"
    assert len(_failed_branch_gaps(without, "jr-dev-1")) == 4
    with_branch = without + (
        "- **Read `outcome.status`: a `failed` outcome does NOT discharge the duty.** Write "
        "`save failed: <outcome.reason>` into your state file's `## ON WAKE` block and route one "
        "Note (`python -m agentflow.registry note --actor jr-dev-1 --project <name> --to steward "
        '--text "save failed at <trigger>: <outcome.reason>"`).\n'
    )
    assert _failed_branch_gaps(with_branch, "jr-dev-1") == []
    # Routed from the WRONG seat, or to the seat itself, is not the branch.
    assert _failed_branch_gaps(with_branch, "jr-dev-2") != []
    assert _failed_branch_gaps(with_branch.replace("--to steward", "--to jr-dev-1"), "jr-dev-1")
    # And the not-discharged half must be a statement, not the bare word.
    assert _failed_branch_gaps(with_branch.replace("does NOT discharge", "is logged for"), "jr-dev-1")


@pytest.mark.parametrize("skill", NON_SAVING_SEATS)
def test_an_excluded_seat_has_not_gained_the_duty(skill: str) -> None:
    """The exclusion, asserted over the WHOLE file — a paste anywhere in it reds.

    Scoped to the whole file rather than to a section on purpose: there is no anchor to read,
    because the correct state is that no such section exists. The tokens are the two the duty
    cannot be written without.
    """
    text = _skill_text(skill)
    # `wiki-save` is the third token because the first two are MODULE IDENTIFIERS, and
    # this epic deliberately wrote five differently-voiced duty blocks - so the next
    # author widening it is likelier to PARAPHRASE than to paste. A complete, followable
    # duty that says "follow `<suite_root>/skills/wiki-save/SKILL.md`" names neither
    # identifier and passed the whole suite when it was tried against this gate.
    # `wiki` alone is NOT available: the steward legitimately owns `wiki/digest.md`.
    for token in (_SAVE_CALL, "save_summariser", "wiki-save"):
        assert token not in text, (
            f"{skill}/SKILL.md now names `{token}`, so this seat has gained the wiki-save duty. "
            'It was excluded by name on 2026-09-05 ("only builders and dev manager"). A wiring '
            "epic that silently widens is the failure class this gate exists for: add the seat to "
            "SAVING_SEATS deliberately, with the decision recorded, or take the duty back out"
        )


def test_the_exclusion_gate_would_catch_a_pasted_duty() -> None:
    """The mutation this file's exclusion depends on, run as a test rather than trusted.

    A `not in` assertion is only as good as the token it looks for. Feed it the shape a later
    author would actually paste — the duty block out of a builder — and prove the predicate trips
    on it, so the exclusion cannot go quietly vacuous the way a keyword check does.
    """
    pasted = (
        "- **The `wiki/` page - at whichever of the four boundaries this firing reached.**\n"
        '  `compose_and_save(wiki_root, trigger="park", context=..., lane="auditor")` after '
        "the end-state writes; a firing with nothing durable writes nothing.\n"
    )
    assert _SAVE_CALL in pasted, "the fixture must contain the call, or it proves nothing"
    # Every other property this file asserts about a SAVING seat is satisfied by that block --
    # which is the point: only the exclusion distinguishes a seat that may save from one that
    # may not, so it is the only thing standing between a paste and a widened fleet.
    assert _ORDERING.search(pasted) and _WRITES_NOTHING.search(pasted)


# --------------------------------------------------------------------------- #
# E-0260 -- `publish` allocates the epic id, so no skill may type one.
#
# The prose is the program here: lead-dev is the ONLY seat that publishes, and the
# command it prints is the command it runs. A SKILL.md still teaching `--id E-NNNN`
# would send the fleet straight back to typing the spec's priority as an identity,
# and the CLI would refuse every publish for the life of that wake -- an idle lane
# with no row on the board saying why.
# --------------------------------------------------------------------------- #
_PUBLISH_CALL = re.compile(r"registry\s*\n?\s*publish\b[^\n]*(?:\n[^\n`]*)?")


def test_lead_dev_publishes_by_SLUG_and_never_types_an_id() -> None:
    text = _skill_text("lead-dev")
    calls = [m.group(0) for m in _PUBLISH_CALL.finditer(text)]
    assert calls, "lead-dev/SKILL.md publishes no `registry publish` call at all"
    for call in calls:
        assert "--slug" in call, (
            f"lead-dev's publish invocation must pass --slug (the id's name half is derived "
            f"from it): {call!r}"
        )
        assert "--id" not in call, (
            f"lead-dev's publish invocation passes --id; publish ALLOCATES the id and refuses "
            f"one, so this instruction cannot be followed: {call!r}"
        )


def test_lead_dev_says_where_the_allocated_id_COMES_FROM() -> None:
    """A seat told not to type the id, and not told how to learn it, is stuck.

    The id is printed on stdout and is available nowhere else -- the registry write itself is
    silent about it -- so the one line that makes the whole verb usable is the one instructing
    lead-dev to read it back.
    """
    text = _skill_text("lead-dev")
    assert "published:" in text, (
        "lead-dev/SKILL.md must name the `published: E-NNNN-<name>` line publish prints; "
        "without it the seat has no way to learn the id it just allocated"
    )


@pytest.mark.parametrize("skill", ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor",
                                   "steward", "dev-manager"))
def test_no_loop_teaches_the_bare_legacy_id_as_the_ARG_SHAPE(skill: str) -> None:
    """`--id E-NNNN` / `--work E-NNNN` are placeholders a seat copies, so they must show the
    shape a write actually emits.

    Scoped to the ARGUMENT position on purpose. Bare `E-0220` in prose is a reference to a real
    past epic, and those stay: legacy ids are readable forever and rewriting them would be
    fabricating history to tidy a regex.
    """
    text = _skill_text(skill)
    # WHITESPACE, NOT A SPACE. Review found two residuals this guard could not see: in
    # jr-dev-2 and jr-dev-3 the `release ... --id` wrapped and the placeholder sat alone at
    # the start of the next line, so a single-space pattern matched nothing while the bare
    # shape was still being taught. A finder narrower than the class it claims to close is
    # the failure this suite exists to catch, so the separator is now any run of whitespace.
    bad = re.findall(r"--(?:id|work)\s+E-N{4}(?!-)", text)
    assert not bad, (
        f"{skill}/SKILL.md teaches {bad} as the argument shape; an id written today is "
        f"`E-NNNN-<name>`"
    )


# --------------------------------------------------------------------------- #
# gh#224 -- delegated-authority answering is a DUTY with a procedure, not a limit
#
# The bullet used to be four clauses that each narrowed, with no trigger, no order and no
# record, so a careful steward discharged it by answering nothing: one steward answer ever
# against 33 from dev-manager. These pins hold the four parts of the repair -- the trigger, the
# ordering, the recorded decline, and the one sentence that must survive verbatim -- and pin
# every CLI surface the procedure names to the BUILT parser, so a renamed flag fails here
# rather than in a headless wake.
# --------------------------------------------------------------------------- #
_DELEGATED_OPEN = "**Delegated-authority answering"
_DELEGATED_CLOSE = "**CHECK THE PREMISE BEFORE YOU ANSWER"


def _delegated_bullet() -> str:
    text = _skill_text("steward")
    start = text.find(_DELEGATED_OPEN)
    assert start != -1, "steward/SKILL.md lost the delegated-authority bullet - re-anchor"
    end = text.find(_DELEGATED_CLOSE, start)
    assert end != -1, "the premise check no longer follows the bullet - re-anchor this gate"
    return _flat(text[start:end])


def _subparser(verb: str) -> argparse.ArgumentParser:
    parser = _registry_module._build_parser()
    (action,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert verb in action.choices, f"registry CLI has no `{verb}` subcommand"
    return action.choices[verb]


def test_delegated_answering_keeps_the_line_verbatim() -> None:
    """The limit was never the bug; the repair must not buy the duty by widening it."""
    bullet = _delegated_bullet()
    assert "**The line does not widen because nobody is watching**" in bullet
    assert "Everything else waits for the seat." in bullet
    assert "a question you would have raised for the human with them in the room stays open" in bullet


def test_delegated_answering_states_an_obligation_with_a_trigger() -> None:
    bullet = _delegated_bullet()
    assert "every wake" in bullet, "no trigger: a duty with no moment is a permission"
    assert "Read every open question." in bullet
    assert "python -m agentflow.registry read --open-questions --project <name>" in bullet
    assert "PRIMARY answerer" in bullet, "the steward must be told it is the primary, not a backstop"
    # The flag the step names is a real flag on the real `read` subcommand.
    assert "--open-questions" in _subparser("read")._option_string_actions


def test_delegated_answering_orders_blocking_questions_first() -> None:
    """Leverage before age: the question holding a parked row sat behind its own timestamp."""
    bullet = _delegated_bullet()
    assert "names in its `waiting_on` column goes FIRST, regardless of age" in bullet
    assert "`claimed` or `parked` Queue row" in bullet and "`## Agents` row" in bullet
    assert "oldest first" in bullet
    order = [bullet.find(s) for s in ("1. **Read every", "2. **Order by", "3. **Classify", "4. **Record")]
    assert -1 not in order and order == sorted(order), f"the four steps are out of order: {order}"


def test_delegated_answering_records_every_outcome_including_declines() -> None:
    bullet = _delegated_bullet()
    assert "Record an outcome for EVERY one, the declines included." in bullet
    assert ("python -m agentflow.registry answer --actor steward --project <name> "
            "--id Q-NNNN --answer \"...\" --by steward") in bullet
    decline = ("python -m agentflow.registry note --actor steward --project <name> "
               "--to dev-manager --text \"Q-NNNN human-only: <class> - <why, one clause>\"")
    assert decline in bullet, "a decline must leave a reason on the board, routed to the seat"
    assert "byte-identical on the board" in bullet, "the reason for recording declines is gone"
    # Both verbs, and the flags they use, are real; the steward may answer as itself only.
    answer = _subparser("answer")
    by = answer._option_string_actions["--by"]
    assert "steward" in by.choices
    assert "--to" in _subparser("note")._option_string_actions
    assert "--by human" not in bullet.replace("`--by human` is refused", ""), (
        "the bullet prescribes --by human somewhere other than the refusal it names"
    )


def _dev_manager_human_only_classes() -> list[str]:
    """dev-manager §1c's *Never self-answer these* items, read off the file, flattened."""
    text = _skill_text("dev-manager")
    start = text.find("**Never self-answer these")
    assert start != -1, "dev-manager §1c lost the *Never self-answer these* list - re-anchor"
    end = text.find("The line above exists so that", start)
    assert end != -1, "the sentence closing §1c's list moved - re-anchor this gate"
    items = re.split(r"\n- ", text[start:end])[1:]
    assert len(items) >= 5, f"§1c's list parsed to {len(items)} item(s), want at least five"
    return [_flat(i).strip() for i in items]


def test_delegated_answering_keeps_the_human_only_classes() -> None:
    """The steward copies §1c's classes item for item, and this holds the two together.

    READ FROM dev-manager, NEVER RESTATED HERE. A first draft paraphrased the list into the
    steward bullet and silently dropped "permission floors", "keys (BYOK included)", "changed
    identifiers, deletions" and "the raiser itself" while calling itself "exactly" §1c's list.
    A class missing from the steward's copy is one the steward is free to answer unattended.
    """
    bullet = _delegated_bullet()
    assert "dev-manager §1c's *Never self-answer these* list" in bullet
    missing = [c for c in _dev_manager_human_only_classes() if c not in bullet]
    assert not missing, f"the steward's human-only list drifted from dev-manager §1c: {missing}"


def test_dev_manager_reads_as_the_fallback_answerer() -> None:
    dm = _flat(_skill_text("dev-manager"))
    assert "**1c. Answer within your delegated authority first — as the FALLBACK, not the primary.**" in dm
    assert "The steward is the primary answerer for delegated-authority questions" in dm
    assert "`Q-NNNN human-only: <class> - <why>` Note" in dm, (
        "dev-manager must know the decline record the steward writes, or it cannot read one"
    )


# --------------------------------------------------------------------------- #
# gh#545 -- the steward resolves the lane-hygiene Blockers it owns
#
# `reap` and `cull` are steward-only in the capability map, so a Blocker whose remedy is one of
# them is the steward's to resolve. The steward skill raised them to dev-manager and never read
# them back. These pins hold the sweep inside domain P (every wake, never rotated), ahead of the
# cull that consumes what it resolves, and parse every command it prescribes with the BUILT
# parser, so a renamed flag or verb fails here.
# --------------------------------------------------------------------------- #
_P_OPEN = "**P. The unattended pump"
_SWEEP_OPEN = "- **Owned-Blocker sweep"
_CULL_OPEN = "- **The cull.**"


def _owned_blocker_sweep() -> str:
    text = _skill_text("steward")
    p = text.find(_P_OPEN)
    start = text.find(_SWEEP_OPEN, p)
    end = text.find(_CULL_OPEN, start)
    assert -1 not in (p, start, end), (
        "steward/SKILL.md has no owned-Blocker sweep between domain P's heading and its cull"
    )
    assert text.find("\n**E. Silent failure", p) > end, "the owned-Blocker sweep left domain P"
    return text[start:end]


def test_steward_owned_blocker_sweep_names_the_rows_and_the_dispositions() -> None:
    sweep = _flat(_owned_blocker_sweep())
    assert "every wake" in sweep, "no trigger: a sweep with no moment is a permission"
    assert "`owner` is `steward` and whose `state` is `open`" in sweep, (
        "the sweep must select exactly the rows `resolve-blocker` lets the steward resolve"
    )
    # reap returns `building` to `ready`: using it to close a merged row resurrects shipped work.
    assert "**Never reap to close a merged row.**" in sweep
    assert "claimant releases it as `done`" in sweep
    # A dead claimant never releases: the merged row needs an exit that is still not a reap.
    assert "**If the claimant is probe-dead it will never release, so do not wait:**" in sweep
    assert "goes to the human-decision subset below, and it is still never reaped" in sweep
    # One route per case. A stood-down lane stays the steward's: Note, decision, override reap.
    assert "**Stood-down lane** — keep the Blocker" in sweep
    assert "`reap --override-stand-down` it and resolve" in sweep
    handover = sweep[sweep.find("**Another seat's remedy**"):sweep.find("**Human-decision subset**")]
    assert handover and "stood-down" not in handover, "a stood-down lane is routed two ways"
    # The linked Question does not discharge the Blocker; applying its answer does.
    assert "The Blocker stays open until that Question is answered" in sweep
    assert "`apply` the Question, then resolve the Blocker" in sweep
    assert "`blocker_owner_resolves`" in sweep and "`blocker_open`" in sweep, (
        "the sweep must name the refusals the CLI actually raises"
    )


def test_steward_owned_blocker_sweep_commands_parse_with_the_registry_cli() -> None:
    sweep = _owned_blocker_sweep()
    cmds = re.findall(r"python -m agentflow\.registry ([a-z-]+)\b([^\n`]*)", sweep)
    assert sorted(v for v, _ in cmds) == ["assign-blocker", "reap", "resolve-blocker"], cmds
    parser = _registry_module._build_parser()
    subs = {"E-NNNN-<slug>": "E-0042-x", "<name>": "demo", "<owner>": "lead-dev", "<lane>": "jr-dev-1"}
    for verb, rest in cmds:
        line = f"{verb}{rest}"
        for k, v in subs.items():
            line = line.replace(k, v)
        ns = parser.parse_args(shlex.split(line))
        assert ns.actor == "steward", f"{verb} is prescribed as {ns.actor}, not steward"
        assert ns.project == "demo", line
        if verb.endswith("-blocker"):
            assert ns.id == "B-NNNN", line
        if verb == "reap":
            assert ns.override_stand_down, "the sweep's reap is the stood-down lane's override form"
        assert verb in _registry_module.ACTOR_CAPABILITY and (
            "steward" in _registry_module.ACTOR_CAPABILITY[verb].actors
        ), f"the capability map refuses the steward `{verb}`"


def test_steward_raises_lane_hygiene_blockers_to_itself() -> None:
    """Domain B and the deploy-lock paragraph both raise to the owner the sweep reads."""
    text = _skill_text("steward")
    b = text.find("**B. Lane hygiene.**")
    b_end = text.find("**B2. Cadence report", b)
    assert -1 not in (b, b_end), "steward/SKILL.md lost domain B - re-anchor"
    domain_b = _flat(text[b:b_end])
    assert "Blocker owned by steward" in domain_b
    raises = re.findall(r"python -m agentflow\.registry (raise-blocker\b[^\n`]*)", text[b:b_end])
    assert len(raises) == 1, raises
    ns = _registry_module._build_parser().parse_args(
        shlex.split(raises[0].replace("<name>", "demo")))
    assert (ns.actor, ns.owner) == ("steward", "steward"), ns
    assert ns.work == "E-NNNN-<slug>", "the raise carries no `--work`, so a re-raise has nothing to match"
    # Dedupe: the sweep leaves some rows open across wakes, so an unchecked raise stacks one a wake.
    assert "**Before raising, check the wake read:** skip the raise when an open" in domain_b
    assert "already names the same epic in its `work` column" in domain_b
    # A stale heartbeat has one path, domain P's ping-then-reap; B must not open a second.
    assert "A stale `## Agents` heartbeat is NOT a domain B finding" in domain_b
    b2 = _flat(text[b_end:text.find("**C. Deployed", b_end)])
    assert "domain B's" not in b2 and "domain P's ping-then-reap" in b2, (
        "B2 still names domain B as the stale-row Blocker path"
    )
    lock = _flat(text[text.find("## The deploy lock"):text.find("## Selection")])
    assert "Blocker owned by steward" in lock and "dev-manager clears it" not in lock


def test_steward_checklist_registry_verbs_are_ones_the_cli_grants_it() -> None:
    """The end-of-wake line lists the steward's writes; each must be one the map grants it."""
    flat = _flat(_skill_text("steward"))
    start = flat.find("- [ ] Registry: only ")
    end = flat.find(" as `--actor steward`", start)
    assert -1 not in (start, end), "steward/SKILL.md lost its checklist registry line - re-anchor"
    listed = {v.split()[0] for v in re.findall(r"`([a-z-]+(?: --strike)?)`", flat[start:end])}
    for must in ("resolve-blocker", "assign-blocker", "ask", "answer", "reap", "stand-down",
                 "repair", "cull"):
        assert must in listed, f"checklist omits `{must}`: {sorted(listed)}"
    caps = _registry_module.ACTOR_CAPABILITY
    refused = sorted(v for v in listed if "steward" not in caps[v].actors)
    assert not refused, f"checklist lists verbs the CLI refuses the steward: {refused}"



_HALT_STALENESS_SITES = ("skills/steward/SKILL.md", "docs/REGISTRY.md")


@pytest.mark.parametrize("rel", _HALT_STALENESS_SITES)
def test_no_instruction_excludes_a_halted_row_from_staleness_by_hand(rel: str) -> None:
    """A halted lane keeps firing on schedule, so its heartbeat stays fresh; `stale_rows`
    therefore lists a halted row (`test_registry.py::
    test_stale_STILL_reports_a_halted_row_whose_heartbeat_stopped`), and its one exclusion is a
    reaped row. A seat told to exclude halted rows by hand stops watching exactly the rows the
    tool reports on purpose. The retired terminal-halt vocabulary said the opposite."""
    flat = _flat((REPO_ROOT / rel).read_text(encoding="utf-8"))
    for stale in ("halted by design", "halted` by design", "ages forever and is correct"):
        assert stale not in flat, f"{rel} still says {stale!r}"
    assert "only what `read --stale` excludes: a reaped row" in flat, rel
    assert "keeps firing on schedule, so its heartbeat stays fresh" in flat, rel


def test_the_staleness_rule_documents_its_one_exclusion() -> None:
    """After the cadence note stopped excluding halted rows by hand, the scan's own paragraph
    has to say what it does exclude, or the two surfaces disagree again by omission."""
    flat = _flat((REPO_ROOT / "docs" / "REGISTRY.md").read_text(encoding="utf-8"))
    assert "The scan excludes exactly one kind of row: a reaped one" in flat
    assert "A `halted` row is NOT excluded." in flat


def _status_templates(text: str) -> list[str]:
    """Every `status --actor <x> --project <name> ...` command in *text*, whitespace-joined, cut
    at the closing backtick or code fence."""
    out = []
    for m in re.finditer(r"status --actor \S+ --project <name>", text):
        seg = text[m.start(): m.start() + 400]
        ends = [i for i in (seg.find("`", 1), seg.find("\n```")) if i != -1]
        out.append(" ".join(seg[: min(ends) if ends else 400].replace("\\n", " ").split()))
    return out


def test_every_auditor_status_template_carries_the_mandatory_eta() -> None:
    """`status` refuses a write without `--eta` (`eta_required`) unless the row is `halted`, so a
    template without it is an instruction that fails as written, and an `[--eta]` shown as
    optional teaches the same failure."""
    text = _skill_text("auditor")
    templates = _status_templates(text)
    assert len(templates) >= 5, templates
    missing = [
        t for t in templates
        if "--eta" not in t and "--state halted" not in t and "--heartbeat-only" not in t
    ]
    assert not missing, missing
    assert "[--eta" not in text


def test_the_auditors_end_of_pass_row_is_written_where_its_wake_is_known() -> None:
    """The end-of-pass idle write declares the wake actually scheduled, so it belongs after the
    backoff decision, not before it: Step 5.4 defers it and Step 7 writes it on both paths."""
    flat = _flat(_skill_text("auditor"))
    step5 = flat[flat.find("## Step 5 — Record + notify"): flat.find("## Step 6 —")]
    step7 = flat[flat.find("## Step 7 — Wake or stop"): flat.find("## The rate-limit recovery")]
    assert "**Your end-of-pass `## Agents` row is written in Step 7, not here**" in step5
    assert '--state idle --phase "idle <marker>"' in step7.split("**Empty pass")[0], (
        "the normal path schedules a wake without writing the row that declares it"
    )
    assert "the stand-down's `eta=` value" in step7
