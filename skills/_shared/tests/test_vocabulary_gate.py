"""Every ``queue_class`` value a SKILL.md teaches must be a value the code can dispatch.

WHY THIS IS AN ALLOW-LIST AND NOT A BAN ON THE RETIRED SPELLING (gh#252). The obvious gate
after a vocabulary retirement is "grep for the dead term". That is a denylist over history:
it closes one instance and leaves the class open. It would not catch a skill teaching a
*typo* that was never a value, a third spelling invented after the gate was written, or a
value that is real but non-dispatchable taught in a context implying it dispatches. Each of
those fails the same silent way: dev-manager assigns the value at promotion, ``dispatchable()``
drops the spec, and the epic sits approved and unclaimable forever with nothing reporting why.

So the expectation is DERIVED from the thing that actually decides dispatch. This module
imports ``DISPATCHABLE_QUEUE_CLASSES`` and ``NON_DISPATCHABLE_QUEUE_CLASSES`` and unions them;
it restates neither, and ``test_this_module_spells_no_vocabulary_value_literally`` enforces
that by reading this file's own source. Edit the frozensets and this gate follows on the next
run — the same move ``ACTOR_CAPABILITY`` made for the registry guards, where the map became
the thing the guards read rather than a parallel description of them.

WHAT COUNTS AS "TEACHING A VALUE", AND WHY IT IS A POSITION TEST RATHER THAN A PROXIMITY ONE.
Most teaching in these skills is PROSE, not a frontmatter line: measured on the tree this was
written against, a sweep for ``queue_class:`` lines found 4 sites where the real teaching
surface is 33 mentions across four skills. A gate reading only assignment lines would stay
green while a skill taught a bogus value in a sentence.

But "a hyphenated word near the word ``queue_class``" is not usable either. The passages that
teach this field are dense with OTHER hyphenated code spans — a ``current_state`` value, three
loop names, a doc title — and a block-scoped proximity rule was measured against this tree at
**14 false positives**, every one of them correct prose. A gate that reds on correct prose gets
disabled, and then it guards nothing at all. Excluding them would mean maintaining a denylist
of other fields' words, which grows every time someone writes a new hyphenated concept noun in
one of these paragraphs — the same maintenance burden, one layer over.

The predicate is therefore about POSITION: a code span is checked only where the prose itself
has put it somewhere a value of this field is what belongs.

  1. ASSIGNMENT   — the field name, then ``:``, then the value. Markdown noise between them is
                    tolerated because these files write the pair inside backticks and bold.
  2. ALTERNATION  — the span sits in a run of code spans joined by nothing but list glue
                    (``/ , ; · |``, "or", "and"), and that run either already contains a known
                    value or is introduced by "unset"/"untagged" — an idiom that only means
                    anything about an optional frontmatter field.
  3. DEFINITION   — the span is immediately followed by ``= gloss`` or ``-> action`` inside a
                    markdown block where another span defined the same way is a known value.
                    This is the shape of the Conventions glossary, which is the single densest
                    teaching site in the tree.

WHICH WAY THIS ERRS, STATED PLAINLY: toward MISSING. Measured against the tree it was written
for, mutating each of the 33 real mentions one at a time to a bogus spelling, the predicate
catches 26 and misses 7 — 79% — at **zero** false positives. Five of the seven misses are the
value written as bare or bold prose rather than as a code span ("the ...-pin set"), which no
span-based rule can see without also claiming every bolded phrase in the paragraph; the other
two are a lone span glossed by a full clause. All seven are narrative mentions, not instructions
to write a value. The three positions that ARE covered are the ones a generator or a promotion
gate reads, which is where gh#252 located the cost.

NO EXEMPTION MECHANISM, DELIBERATELY. The tree needs none — the sweep is at zero violations —
and an unused escape hatch is an attractive nuisance: the next person with a red gate reaches
for it instead of the prose. Historical passages are already safe, because narrative mentions
are not in any of the three positions above. If one ever genuinely needs exempting, add an
explicit ``(file, value, reason)`` entry here rather than a wildcard, so the exemption is as
readable as the rule.

THE TREE WAS ALREADY CLEAN WHEN THIS LANDED, so a green sweep proves nothing on its own. The
self-tests below are the real evidence: they build passages in each of the three positions and
assert the predicate reds on the retired spelling, on a spelling that was never a value, and on
a plausible future spelling, while staying green on every legitimate value and on the
neighbouring prose vocabulary that the rejected proximity rule flagged.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[2]

# Put the tree under test ahead of the editable install for a FRESH import. Stated precisely,
# because the stronger claim is false: this does NOT win when `agentflow` is already in
# `sys.modules`, which is the normal case in a full-suite run - the earlier import keeps the
# binding and only SKILLS_ROOT follows this file. Measured: pre-binding `agentflow` from the
# editable install and then importing this module yields SKILLS_ROOT from the worktree and the
# frozensets from the installed tree. Harmless today (both trees carry identical frozensets, and
# CI has exactly one tree), and worth saying plainly rather than leaving a comment that promises
# an isolation it cannot deliver.
sys.path.insert(0, str(SKILLS_ROOT / "_shared"))

from agentflow.epic_survey import (  # noqa: E402
    DISPATCHABLE_QUEUE_CLASSES,
    KNOWN_QUEUE_CLASSES,
    NON_DISPATCHABLE_QUEUE_CLASSES,
)
from agentflow.vocabulary import RETIRED_QUEUE_CLASS  # noqa: E402
from agentflow.tracked_paths import only_tracked  # noqa: E402

#: The whole vocabulary, derived. Never a literal — see the module docstring.
VOCABULARY = DISPATCHABLE_QUEUE_CLASSES | NON_DISPATCHABLE_QUEUE_CLASSES

FIELD = "queue_class"

# A value of this field is lower-case kebab: at least two segments of [a-z0-9] joined by
# hyphens. Deliberately narrow — it is what makes `bug`/`feature`, which these skills name on
# purpose as counter-examples, fall out of the candidate set instead of needing an exemption.
_KEBAB = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+"

# Position 1. Markdown noise is tolerated on both sides of the colon because these files write
# the pair as `queue_class: value` inside backticks and **bold**.
_ASSIGNMENT = re.compile(FIELD + r"[`*_\s]*[:=][`*\s\"']*(" + _KEBAB + r")")

_SPAN = re.compile(r"`([^`\n]+)`")
_BARE_KEBAB = re.compile(r"^" + _KEBAB + r"$")

# Words that join ALTERNATIVES of one field rather than starting a new clause.
_ALT = r"(?:or|and|AND|nor|unset|Unset|untagged|Untagged)"
_PUNCT = r"[\s/,;·|]"
# Between two spans of one alternation run: list punctuation, joining words, and at most one
# parenthetical gloss — `a` (what a is) and `b` (what b is) is how two of these skills write it.
_GLUE = re.compile(
    r"^" + _PUNCT + r"*(?:\b" + _ALT + r"\b" + _PUNCT + r"*)*"
    r"(?:\([^()`]*\)" + _PUNCT + r"*(?:\b" + _ALT + r"\b" + _PUNCT + r"*)*)?$"
)
# "unset / `x`" and "Untagged or `x`" introduce a run of this field's values even when no known
# value is present — the field-absence idiom is meaningless about anything but a frontmatter
# field. Generic "and"/"or" is NOT enough to anchor a run on its own: it matched `stand-down`
# and `from-refine` in unrelated prose when this was first measured.
_UNSET_IDIOM = re.compile(r"\b(?:unset|Unset|untagged|Untagged)\b")
_UNSET_HEAD = re.compile(
    _UNSET_IDIOM.pattern + _PUNCT + r"*(?:\b" + _ALT + r"\b" + _PUNCT + r"*)*$"
)
_HEAD_WINDOW = 30

# Position 3. `x` = gloss, or `x` -> action. Runs are bounded by the markdown BLOCK, not by the
# blank line: an unbounded run merged a `current_state` glossary with the queue_class one eleven
# lines below it and produced a false positive on a correct sentence.
_DEFINES = re.compile(r"^\s*(?:=[^=]|→|->)")
_BLOCK_BREAK = re.compile(r"\n\s*\n|\n\s*(?:[-*+>|]|#{1,6} |\d+[.)] )")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _spans(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(1)) for m in _SPAN.finditer(text)]


def _assignment_values(text: str) -> list[tuple[int, str]]:
    return [(_line_of(text, m.start()), m.group(1)) for m in _ASSIGNMENT.finditer(text)]


def _alternation_values(text: str) -> list[tuple[int, str]]:
    spans = _spans(text)
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(spans):
        run, j = [spans[i]], i
        while j + 1 < len(spans):
            gap = text[spans[j][1]:spans[j + 1][0]]
            if "\n\n" in gap or not _GLUE.match(gap):
                break
            j += 1
            run.append(spans[j])
        tokens = [(s, t) for s, _e, t in run if _BARE_KEBAB.match(t)]
        head = text[max(0, run[0][0] - _HEAD_WINDOW):run[0][0]]
        # The run is about this field when it names a known value, when the field name itself
        # is one of its spans, or when the field-absence idiom introduces it — either just
        # before the run or between two of its spans ("(`queue_class` unset/`value`)").
        body = text[run[0][0]:run[-1][1]]
        anchored = (
            any(t in VOCABULARY for _s, t in tokens)
            or any(t == FIELD for _s, _e, t in run)
            or _UNSET_HEAD.search(head) is not None
            or _UNSET_IDIOM.search(body) is not None
        )
        if anchored:
            out += [(_line_of(text, s), t) for s, t in tokens]
        i = j + 1
    return out


def _definition_values(text: str) -> list[tuple[int, str]]:
    runs: list[list[tuple[int, int, str]]] = []
    cur: list[tuple[int, int, str]] = []
    for s, e, t in _spans(text):
        if not (_BARE_KEBAB.match(t) and _DEFINES.match(text[e:e + 4])):
            continue
        if cur and _BLOCK_BREAK.search(text[cur[-1][1]:s]):
            runs.append(cur)
            cur = []
        cur.append((s, e, t))
    if cur:
        runs.append(cur)
    out: list[tuple[int, str]] = []
    for run in runs:
        if any(t in VOCABULARY for _s, _e, t in run):
            out += [(_line_of(text, s), t) for s, _e, t in run]
    return out


def taught_values(text: str) -> list[tuple[int, str]]:
    """Every value *taught* by *text*, as (line number, value).

    Union of the three positions described in the module docstring. Order is by position kind
    then by offset; callers sort when they need line order.
    """
    return _assignment_values(text) + _alternation_values(text) + _definition_values(text)


def violations(text: str) -> list[tuple[int, str]]:
    """Taught values that the code cannot dispatch and does not know."""
    return sorted({(ln, v) for ln, v in taught_values(text) if v not in VOCABULARY})


def _skill_files() -> list[Path]:
    return only_tracked(SKILLS_ROOT, SKILLS_ROOT.glob("*/SKILL.md"))


# --------------------------------------------------------------------------------------
# The derivation itself
# --------------------------------------------------------------------------------------


def test_the_vocabulary_is_exactly_the_two_frozensets() -> None:
    """`KNOWN_QUEUE_CLASSES` must stay the union this gate reconstructs.

    If a third category set is ever added to `epic_survey` and folded into the known
    vocabulary, this gate would start redding on a legitimate value while looking correct.
    Fail HERE, naming the drift, instead of there.
    """
    assert KNOWN_QUEUE_CLASSES == VOCABULARY, (
        "epic_survey.KNOWN_QUEUE_CLASSES is no longer the union of the dispatchable and "
        f"non-dispatchable sets ({sorted(KNOWN_QUEUE_CLASSES ^ VOCABULARY)} differ) - this "
        "gate reconstructs that union and would now reject a value the code accepts"
    )


def test_this_module_spells_no_vocabulary_value_literally() -> None:
    """The expectation must DERIVE, not be a copy that drifts.

    A restated literal is the exact defect this module exists to remove, so the ban applies
    to this module first.
    """
    source = inspect.getsource(sys.modules[__name__])
    restated = sorted(v for v in VOCABULARY if v in source)
    assert not restated, (
        f"{Path(__file__).name} spells {restated} literally; import the frozensets instead - "
        "a copy is what this gate exists to stop skills from keeping"
    )


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


def test_every_queue_class_value_taught_in_a_skill_is_in_the_vocabulary() -> None:
    found: list[str] = []
    for path in _skill_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for line, value in violations(text):
            found.append(f"{path.parent.name}/SKILL.md:{line} teaches {value!r}")
    assert not found, (
        f"{len(found)} {FIELD} value(s) taught in a SKILL.md are outside the vocabulary the "
        f"code dispatches on. Allowed: {sorted(VOCABULARY)} (dispatchable: "
        f"{sorted(DISPATCHABLE_QUEUE_CLASSES)}). A spec carrying any other value surveys as "
        "out-of-vocabulary and sits approved and unclaimable forever.\n  " + "\n  ".join(found)
    )


def test_the_gate_covers_a_real_teaching_surface() -> None:
    """A sweep that finds nothing reads exactly like a sweep that found nothing wrong.

    If the skills are reorganised, or the predicate is broken by an edit, this gate goes
    quiet while staying green. Assert it still sees the surface it was built for.
    """
    per_skill = {
        p.parent.name: len(taught_values(p.read_text(encoding="utf-8", errors="replace")))
        for p in _skill_files()
    }
    teaching = {name: n for name, n in per_skill.items() if n}
    assert len(teaching) >= 4, (
        f"only {len(teaching)} skill(s) still teach a {FIELD} value ({teaching}); the gate was "
        "built against four and has probably stopped matching rather than found a cleaner tree"
    )
    assert sum(teaching.values()) >= 24, (
        f"the sweep sees only {sum(teaching.values())} taught values across {teaching}; it saw "
        "34 when written, so the predicate is likely broken rather than the prose thinned"
    )


# --------------------------------------------------------------------------------------
# What the predicate does and does not flag
# --------------------------------------------------------------------------------------


def _assignment_passage(value: str) -> str:
    return f"- Carve the spec with `{FIELD}: {value}`, comment, and label it.\n"


def _alternation_passage(value: str) -> str:
    return f"- Selection keeps unset / `{value}` and drops every other value.\n"


def _definition_passage(value: str) -> str:
    """A glossary block: the value under test defined beside a different known value."""
    other = next(v for v in sorted(VOCABULARY) if v != value)
    return (
        f"- **`{FIELD}`** (optional frontmatter):\n"
        f"  `{other}` = a class the fleet knows · `{value}` = the one under test\n"
    )


POSITIONS = {
    "assignment": _assignment_passage,
    "alternation": _alternation_passage,
    "definition": _definition_passage,
}

#: A spelling that was NEVER a value - the case a denylist over the retired term cannot catch,
#: and the whole argument for deriving from the allow-list (gh#252).
NEVER_A_VALUE = "fleet-buildible"
#: A plausible next spelling, invented after this gate was written.
FUTURE_SPELLING = "fleet-dispatchable"


@pytest.mark.parametrize("position", sorted(POSITIONS))
@pytest.mark.parametrize(
    "bogus",
    [RETIRED_QUEUE_CLASS, NEVER_A_VALUE, FUTURE_SPELLING],
    ids=["retired", "never-a-value", "future-spelling"],
)
def test_the_predicate_flags_a_value_outside_the_vocabulary(position: str, bogus: str) -> None:
    text = POSITIONS[position](bogus)
    assert bogus not in VOCABULARY, "fixture value must be outside the vocabulary"
    assert [v for _ln, v in violations(text)] == [bogus], (
        f"a skill teaching {bogus!r} in {position} position must red the gate; got "
        f"{violations(text)} for:\n{text}"
    )


@pytest.mark.parametrize("value", sorted(KNOWN_QUEUE_CLASSES))
@pytest.mark.parametrize("position", sorted(POSITIONS))
def test_the_predicate_accepts_every_known_value(position: str, value: str) -> None:
    """Including the NON-dispatchable ones.

    A gate that reds on a skill correctly teaching a known-unbuildable class is worse than no
    gate: those classes exist precisely so a human can see "known, deliberately not buildable"
    and the skills must be free to say so.
    """
    text = POSITIONS[position](value)
    assert violations(text) == [], f"{value!r} is in the vocabulary but reddened in {position}"
    assert value in {v for _ln, v in taught_values(text)}, (
        f"{position} position no longer detects {value!r} at all - the predicate broke, and a "
        "gate that sees nothing passes everything"
    )


def test_the_predicate_ignores_the_neighbouring_prose_vocabulary() -> None:
    """The cry-wolf property, pinned.

    These four spans are real prose from the same paragraphs that teach this field, and each
    one was a false positive under the block-proximity rule this predicate replaced. None of
    them is in a value position, so none may be flagged.
    """
    known = sorted(VOCABULARY)[0]
    text = (
        f"- **`{FIELD}`** is an allow-list: selection keeps unset / `{known}` and drops the "
        "rest.\n"
        "  So do NOT promote such a spec to `approved-for-autonomous`; `lead-dev` filters it\n"
        "  out, the same dep math as `choose-next`, and it subsumes the old `polishing-mode`\n"
        "  sweep. A `completion-truth` comment on a done spec says so.\n"
    )
    assert violations(text) == [], (
        "the predicate flagged ordinary prose from a real teaching paragraph; a gate that "
        f"cries wolf gets disabled: {violations(text)}"
    )


def test_a_counter_example_value_is_not_a_candidate() -> None:
    """These skills name `bug`/`feature` on purpose as out-of-vocabulary examples.

    They are not kebab-shaped, so they fall out of the candidate set rather than needing an
    exemption. If the shape rule is ever widened, this fails and says why.
    """
    known = sorted(VOCABULARY)[0]
    text = f"- Selection keeps unset / `{known}`; an unknown class like `bug`/`feature` is not.\n"
    assert violations(text) == []
