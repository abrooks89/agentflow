"""The auditor's prose must enumerate every body section its own template requires.

`templates/gap-epic.md` is the source of truth for the shape a drafted gap epic takes. The
skeleton the auditor copies lives inside the file's single ```markdown fence; everything below
that fence is documentation ABOUT the skeleton and never reaches a written spec.

That list is enumerated a second time in `skills/auditor/SKILL.md` section 4.3 -- and the second
copy is the one the auditor actually follows. It has now drifted twice in the same direction: a
section was added to the template, the prose was not updated, and gap epics shipped without it.
`## Files` was the second (gh#310), and it is the expensive one, because a standing Directive
refuses promotion to a spec that names no files -- so every draft written from the stale prose
was unpromotable by construction and spent an attended promotion pass to discover it.

A renumbering of the prose alone would fix this instance and nothing else. This module is the
deliverable: it re-derives the required list FROM the template and requires the prose to name
every member, so the NEXT section added to the template fails here until the prose catches up.

WHAT THIS GATE CAN AND CANNOT DO -- stated because a gate that overstates itself is worse than
no gate:

* It CAN prove the prose enumeration names every REQUIRED skeleton section, and that the
  enumeration is parseable at all -- an unparseable one FAILS rather than passing vacuously,
  because a rewording this parser cannot read is drift nobody can see.
* It CANNOT prove the auditor obeys the prose. That is the live agent's behaviour; the
  deterministic half is gated by `harness/auditor-scenario/auditor_scenario.py`, which raises
  when the spec it assembles is missing a required section.
* It CANNOT judge the CONTENT of a section, only that it is named in both places.

There is deliberately no `pytest.skip` here. Every input this module needs is a tracked file in
this repository, so an unreadable one is a failure, not an excuse.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GAP_TEMPLATE = REPO_ROOT / "templates" / "gap-epic.md"
AUDITOR_SKILL = REPO_ROOT / "skills" / "auditor" / "SKILL.md"

# Only the fenced skeleton reaches a written spec. A whole-file `## ` sweep would also collect
# the template's own documentation headings (`## Invariants`, `## Provenance block (required)`,
# `## UC continuity (the auditor NEVER allocates)`, ...) -- about a dozen -- and would then
# demand the prose enumerate sections no spec ever carries.
_FENCE_RE = re.compile(r"^```markdown\n(.*?)^```$", re.DOTALL | re.MULTILINE)

# `## Hand-offs` is the one body section the template declares optional in its own words, so it
# is excluded from the REQUIRED set. `test_the_template_still_declares_hand_offs_optional` holds
# the other end: if the template stops saying so, this exclusion has to be reconsidered rather
# than silently kept.
OPTIONAL_SECTIONS = ("Hand-offs",)
_OPTIONAL_MARKER = "OMIT this section entirely"

# Non-vacuity floor. A parser that returned [] would make every assertion below trivially true.
_MIN_TEMPLATE_SECTIONS = 4
_MIN_ENUMERATED = 3

# The prose site: section 4.3's `- **Body:**` bullet, the sentence that tells the auditor which
# sections to fill. Its enumeration wraps across lines mid-backtick (`## Use case\n  continuity`),
# so the bullet is re-joined before the list is read out of it.
_BODY_BULLET_PREFIX = "- **Body:**"

# ...and only the ENUMERATION is read, not the whole bullet. The bullet goes on to gloss two of the
# sections by name after the list ends; a whole-bullet parse would count those mentions, so deleting
# a section FROM THE LIST would leave this gate green while the auditor read a shorter list. That is
# the precise shape of the bug being fixed here, so the parser must not be blind to it.
_ENUMERATION_RE = re.compile(r"fill the template's\s+(.*?)\s+sections\b")
_SECTION_TOKEN_RE = re.compile(r"`##\s+([^`]+)`")


def _norm(heading: str) -> str:
    """Compare headings by their name, not their gloss.

    The template writes `## Gap (one sentence)` where the prose writes `## Gap`: the parenthetical
    is authoring guidance, not part of the heading's identity. Whitespace is collapsed because the
    prose enumeration wraps across lines, and case is folded so a capitalisation change reads as
    the same section rather than as a missing one.
    """
    return re.sub(r"\s+", " ", heading.split("(")[0]).strip().casefold()


def _skeleton() -> str:
    """The fenced skeleton only -- what a spec written from the template actually contains."""
    text = GAP_TEMPLATE.read_text(encoding="utf-8")
    m = _FENCE_RE.search(text)
    assert m is not None, (
        f"{GAP_TEMPLATE}: no ```markdown skeleton fence found. The template's body sections are "
        f"read out of that fence; without it this gate cannot tell a spec section from a "
        f"documentation heading. Restore the fence, or teach this parser the new shape -- do not "
        f"delete the gate."
    )
    return m.group(1)


def _template_sections() -> list[str]:
    """Every `## ` body heading in the skeleton, in order."""
    heads = [ln[3:].strip() for ln in _skeleton().split("\n") if ln.startswith("## ")]
    assert len(heads) >= _MIN_TEMPLATE_SECTIONS, (
        f"{GAP_TEMPLATE}: parsed only {heads} from the skeleton fence, fewer than the "
        f"{_MIN_TEMPLATE_SECTIONS} a gap epic has always carried. That is a parse failure, not a "
        f"smaller template -- fix the parser before trusting a green run."
    )
    return heads


def _required_sections() -> list[str]:
    return [h for h in _template_sections() if h not in OPTIONAL_SECTIONS]


def _body_bullet() -> str:
    """SKILL.md section 4.3's `- **Body:**` bullet, re-joined into one line."""
    lines = AUDITOR_SKILL.read_text(encoding="utf-8").split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.startswith(_BODY_BULLET_PREFIX)), None)
    assert start is not None, (
        f"{AUDITOR_SKILL}: no `{_BODY_BULLET_PREFIX}` bullet found. That bullet is where the "
        f"auditor is told which template sections to fill; if it moved or was renamed, point this "
        f"gate at its new home. An enumeration this parser cannot find is drift nobody can see, so "
        f"this FAILS rather than passing vacuously."
    )
    out = [lines[start]]
    for ln in lines[start + 1:]:                 # continuation lines are indented and not bullets
        if not ln.strip() or not ln.startswith("  ") or ln.lstrip().startswith("- "):
            break
        out.append(ln)
    return re.sub(r"\s+", " ", " ".join(out))


def _enumeration_span() -> str:
    """Just the list -- "fill the template's <...> sections" -- not the rest of the bullet."""
    m = _ENUMERATION_RE.search(_body_bullet())
    assert m is not None, (
        f"{AUDITOR_SKILL}: the `{_BODY_BULLET_PREFIX}` bullet no longer reads \"fill the "
        f"template's <sections> sections\", so this gate cannot tell which sections it enumerates. "
        f"Reword it back, or teach this parser the new shape. An enumeration this parser cannot "
        f"read is drift nobody can see, so it FAILS here rather than passing vacuously."
    )
    return m.group(1)


def _enumerated_sections() -> set[str]:
    """The `## X` sections the prose enumeration names, normalised."""
    names = {_norm(t) for t in _SECTION_TOKEN_RE.findall(_enumeration_span())}
    assert len(names) >= _MIN_ENUMERATED, (
        f"{AUDITOR_SKILL}: the `{_BODY_BULLET_PREFIX}` enumeration yielded {sorted(names)} -- "
        f"fewer than the {_MIN_ENUMERATED} backticked `## Section` tokens it has always carried. "
        f"Either it was reworded into a form this parser cannot read, or it really did shrink. "
        f"Both are failures: an unreadable enumeration cannot be checked against the template, "
        f"which is the whole point of this gate."
    )
    return names


def test_the_skill_prose_enumerates_every_required_template_section():
    """The pin. Add a section to the template and this fails until SKILL.md names it too."""
    required = _required_sections()
    named = _enumerated_sections()
    missing = [h for h in required if _norm(h) not in named]
    assert not missing, (
        f"{AUDITOR_SKILL} section 4.3's `{_BODY_BULLET_PREFIX}` bullet does not name {missing}, which "
        f"{GAP_TEMPLATE.name}'s skeleton requires of every gap spec. The prose is what the auditor "
        f"follows, so a section missing here is a section missing from every draft it writes. Add "
        f"it to that bullet's enumeration (template order: {required})."
    )


def test_the_enumeration_is_parseable_and_the_skeleton_is_findable():
    """Non-vacuity. Both halves must be READ, or the pin above passes on nothing."""
    assert _template_sections(), "skeleton parse returned no sections"
    assert _enumerated_sections(), "prose enumeration parse returned no sections"


def test_the_template_still_declares_hand_offs_optional():
    """The exclusion above is the template's own words, not this module's opinion.

    `## Hand-offs` is dropped from the required set because the template says to omit it when the
    whole scope is the builder's own. If that instruction goes away, the section is no longer
    optional and the exclusion is wrong -- so it must fail here rather than quietly keep letting a
    now-mandatory section go unenumerated.
    """
    skeleton = _skeleton()
    for name in OPTIONAL_SECTIONS:
        assert f"## {name}" in skeleton, (
            f"{GAP_TEMPLATE.name}: `## {name}` is excluded from the required sections but is no "
            f"longer in the skeleton at all. Drop it from OPTIONAL_SECTIONS."
        )
        after = skeleton.split(f"## {name}", 1)[1].split("\n## ", 1)[0]
        assert _OPTIONAL_MARKER in after, (
            f"{GAP_TEMPLATE.name}: `## {name}` no longer says {_OPTIONAL_MARKER!r}, so it is not "
            f"declared optional any more. Either restore that instruction or remove {name!r} from "
            f"OPTIONAL_SECTIONS so the prose is required to enumerate it."
        )
