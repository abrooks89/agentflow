"""`halt` names exactly one thing in the loop skills: the ROW STATE. Nothing else may use it.

WHAT WAS WRONG (gh#148). The loop skills spelled three different states `halt`, and the three
have opposite behaviours:

* ``status --state halted`` - a ROW STATE, a lane blocked on an answer it cannot get. The lane
  **keeps firing on schedule**, because the next wake is the only way the answer can reach it.
* §8's stop-the-loop conditions - **scheduling ends**, no continuation.
* Step 0's config-resolution failure - the wake **cannot start**, no continuation.

Nothing in the text separated them, and the failure is the silent, expensive direction: a lane
that resolves `halt` toward §8 stops scheduling *while halted on a question*, so the answer
arrives and nothing ever consumes it. An indefinite stall, no error anywhere, on the exact path a
blocked lane takes. Both halves were observed live - jr-dev-1 read the contradiction correctly and
still had to stop and ask; jr-dev-2, as a fresh instance, held the false belief outright ("a halt
would need someone to relaunch me"), contradicted by a sentence in the file it reads every wake.
That second one is why this is a rename and not a clarifying clause: **a reader resolves an
ambiguous word before reaching the sentence that would have corrected it.**

The rename kept `halted` for the row state (it is a CLI value, and correct English for what it
is) and gave the other two senses their own words: **stop** = scheduling ends, **abort** = this
wake could not start.

WHY THIS IS NOT A GREP FOR THE NEW WORDS. `stop` and `abort` are ordinary English and appear all
over these files for unrelated reasons ("do NOT stop the loop", "`TaskStop`", "an alert that means
a loop has stopped"). A gate that greps for them proves nothing about the sites it did not visit,
and would be swamped besides. So this gate is scoped to the word that must now mean ONE thing, and
it checks EVERY occurrence of that word's family rather than sampling: the surface it covers is
enumerated by `test_the_gate_covers_the_surface_it_was_built_for`, which reds if the sweep goes
quiet.

THE RULE, IN TWO CLAUSES, AND WHY EACH IS SHAPED THAT WAY.

**(a) Only the adjective survives.** The permitted spellings are `halted` and the two compounds
built on it that name the same state (`halted-detection`, `halted-by-design`). Every VERB form -
`halt`, `halts`, `halting`, `HALT`, and the compounds built on the verb (`halt-to-human`,
`halt_reason`, `ScheduleWakeup-halt`) - is banned outright. This is not a stylistic ban: **a row
state is something a row IS, never something a loop DOES**, so a verb form is by construction one
of the two renamed senses. The grammar carries the semantics, which is what lets the check be
mechanical without being arbitrary.

**(a's one carve-out, added by E-0213: a CHECKED VALUE spelled `halt`.)** The save capability's
trigger vocabulary (`agentflow.save_summariser.TRIGGERS`) contains the value `halt`, and
`compose_and_save` refuses anything outside that set — so the five seats that carry the save duty
must write the banned token to prescribe a call that works. The token is therefore excused in a
save-trigger VALUE POSITION only: beside `trigger=`, or inside an enumeration carrying two or more
of its siblings. It is excused nowhere else, and `test_a_scheduling_halt_is_not_excused_by_the_
trigger_position` proves a stop-sense `halt` standing next to one piece of trigger vocabulary is
still reported. A per-skill exemption was the alternative and is worse: it would blind this gate
to every verb form in five of the seven files.

**(b) The adjective must arrive with its carrier.** Bare-prose `halted`, floating with no noun
and no field marker beside it, is exactly where the sense slipped in the real files: *"a lane that
halted at 3/3"*, *"a halted foreman"*, *"when halted on a blocker"* were all stop-scheduling sites
wearing the row state's word. So `halted` must sit in a value position - after `--state` /
`--urgency`, beside another value of those fields, or immediately followed by `:` - or attach to
one of an enumerated set of CARRIER nouns (a `halted` row, a `halted` question, a `halted` lane).

**The carrier's SIDE matters (E-0953, gh#264).** A carrier on the RIGHT ("a `halted` row") is the
adjective attached to its noun. A carrier on the LEFT with nothing between ("the lane halted at
3/3", "the row halted the loop", "a question halted the fleet") is a subject and its past-tense
VERB - the stop sense again - so it reds. A left carrier still anchors in three shapes only: a
copula stands directly between them ("the row is halted", `COPULAS`; an adverb or relative clause
in between breaks it), `halted` sits inside an inline code span on the same line ("the Agents row
`halted`"), or it is in a value position (`--state halted`, `--state=halted`, `halted:`). A left
SIBLING state ("advisory/waiting/halted") anchors on either side, as it always has.

THE LIMIT, STATED PLAINLY: **the gate checks grammar, never meaning.** It reads word order, a short
copula list, backticks and field markers. It cannot tell "the lane was halted" (the row state)
from "the lane was halted by a crash" (an event); both are green because both are grammatical
adjective positions. A writer who wants to say the stop sense in carrier-adjacent grammar can, and
only review catches that.

WHICH WAY THIS ERRS, STATED PLAINLY: toward FALSE POSITIVES, the opposite of the queue_class
vocabulary gate that shipped alongside it. That gate reads 33 real prose mentions and must not cry
wolf, so it errs toward missing. This one governs ONE word across seven files whose whole content
is instructions, and the remedy for a red is a five-word rephrase - write "a `halted` row" instead
of "halted" - which makes the prose better rather than worse. A rule that costs a rephrase and
buys an unambiguous word is the right trade here; a rule that lets bare `halted` back into prose
is how the ambiguity returns.

ONE EXEMPTION, NAMED AND JUSTIFIED, not a wildcard: a residual state-file key a lane greps its
own file for, in the three junior lanes' residual-keys paragraph. It must stay VERBATIM, because
a renamed key is one the reader cannot find in their own file. `test_every_exemption_is_still_load_bearing`
reds if one stops matching, so a dead exemption cannot linger as an unguarded hole.

WHAT SURFACE THIS READS, AND WHY IT IS A GLOB WITH DECLARED EXCLUSIONS.

The first cut named the seven loop skills in a tuple here. That is the defect this epic is ABOUT,
one layer up: a derived set restating a hand-written list inherits the list's blind spots as
green. Proved on this very file - an eighth loop skill dropped into the tree carrying six
pre-rename lines returned 39 passed, because the gate never opened it. Deriving the list from
`registry.AGENTS` instead would not have helped: a skill directory on disk that no code has heard
of is exactly the case both lists miss.

So the file set is a GLOB of the skills tree, and the failure direction is inverted. Every
`*/SKILL.md` is gated unless it is named in `NOT_A_LOOP` below, which means a new directory
defaults IN and an omission reds instead of passing silently. The exclusions are not a quiet
allowance: `test_no_excluded_skill_is_a_self_waking_loop` reds if an excluded skill writes its own
`## Agents` row (which is what a fleet loop does and nothing else does), and
`test_every_registry_agent_is_gated` reds if a name the code dispatches on drops out of the set.
Silencing a real loop by adding it here is therefore not available.

It also reads each gated skill's `references/*.md`. That was found by a reviewer, not by design:
`auditor/references/notify-format.md` carried a pre-rename sentence through the whole rename
because the gate read `SKILL.md` only - while `auditor/SKILL.md` sends the reader to that file for
the exact format it describes. A rule that stops one file short of where the reader is sent is not
enforced, so the surface is the file the loop IS plus the files it hands the reader.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow import ntfy
from agentflow.registry import AGENT_STATES, AGENTS, URGENCY_ORDER, main
from agentflow.save_summariser import TRIGGERS as SAVE_TRIGGERS
from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]

#: (skill, why it is not a loop). Skills that live in this tree but are not self-waking fleet
#: loops, so they have no three-way scheduling sense to confuse and were outside this rename.
#: Every entry is checked - it must exist, it must not be a `registry.AGENTS` name, and it must
#: not write its own `## Agents` row. An omission from this list is a RED, not a silent pass.
NOT_A_LOOP: tuple[tuple[str, str], ...] = (
    ("orchestrator", "an epic-execution skill a loop INVOKES; its `halt_reason` is a state-file "
                     "key on disk, so renaming its prose is a separate change with its own risk"),
    ("polish-mode", "a single pass a loop invokes when the backlog runs dry; it schedules nothing"),
    ("project", "the epic-launch dispatcher; it runs once per invocation and never wakes itself"),
    ("report", "a pure read rendered three ways; it quotes registry field names verbatim"),
    ("wiki-save", "a one-page writer invoked at a phase boundary; it owns no lane and no cadence"),
)

#: A loop writes its own `## Agents` row. Nothing else in this tree does, which is what lets an
#: exclusion be checked rather than trusted.
_SELF_ACTOR = "--actor {}"

#: The one permitted spelling. It is a linguistic constant, not a value that drifts, so it is
#: written here rather than derived - but its STANDING as a field value is asserted against the
#: code in `test_the_row_state_this_gate_protects_is_still_a_field_value`, so if the enum ever
#: drops it this file reds instead of silently guarding a word the CLI no longer accepts.
ROW_STATE = "halted"

#: The per-loop coverage floor. Held below the smallest real count (7) so ordinary rewording of
#: the two thinnest loops does not red, and far enough above zero that a loop dropping out of the
#: sweep does. See `test_the_gate_covers_the_surface_it_was_built_for` for why it is not a total.
PER_LOOP_FLOOR = 5

#: The two replacement names this epic chose. Asserted NOT to be row states, and never grepped
#: for - see the module docstring on why a grep for them would prove nothing.
STOP_WORD = "stop"
ABORT_WORD = "abort"

#: Sibling values of the two fields `halted` belongs to, DERIVED. `halted` standing next to
#: `waiting` or `idle` is unambiguously a field value, whatever the surrounding prose does.
SIBLINGS = (set(AGENT_STATES) | set(URGENCY_ORDER)) - {ROW_STATE}

#: The nouns that can CARRY this state, enumerated. A row has it, a question is raised at it, a
#: lane is in it. Deliberately short: every word added here is a word bare `halted` may hide
#: behind, and the list is the whole reason clause (b) has teeth.
CARRIERS = frozenset({
    "state", "states", "status", "urgency",
    "question", "questions", "row", "rows", "lane", "lanes",
})

#: Compounds built on the ADJECTIVE, naming the same state. Both predate this epic and both are
#: correct: detecting that a lane is `halted`, and a row that is `halted` on purpose.
ALLOWED_COMPOUNDS = frozenset({"halted-detection", "halted-by-design"})

#: THE SAVE-TRIGGER COLLISION (E-0213), and why clause (a) needed a value position rather than an
#: exemption. `agentflow.save_summariser.TRIGGERS` is a CHECKED vocabulary — `compose_and_save`
#: refuses any string outside it and writes nothing — and one of its four values is spelled
#: `halt`. When the five saving seats gained the duty, each had to name that value in its own
#: procedure, and clause (a) reds on the bare token.
#:
#: Renaming it in the skills is not available: the seats would then prescribe a call the module
#: refuses, which is the "instruction the reader cannot follow" the `halt_reason` exemption
#: already exists for. A per-skill token exemption is worse — it would blind this gate to every
#: genuine verb-form `halt` in five of the seven files, which is most of the surface gh#148 was
#: about. So the refinement is narrow and mirrors clause (b): the token is excused ONLY where it
#: sits in a save-trigger VALUE position, and nowhere else. `halt` in a scheduling sentence is
#: still a violation, which `test_a_scheduling_halt_is_not_excused_by_the_trigger_position` pins.
#:
#: DERIVED from the module, never restated: if the trigger vocabulary is ever renamed, this
#: excuse narrows or disappears with it rather than lingering as an unguarded hole.
SAVE_TRIGGER_SIBLINGS = frozenset(t for t in SAVE_TRIGGERS if t != "halt")

#: How much text either side of the token is read for evidence that it is a trigger value. The
#: skills hard-wrap, so the four values routinely straddle a line break and a line-scoped check
#: would miss half the real sites.
_SAVE_TRIGGER_WINDOW = 200

#: TWO distinct siblings, not one. `park` alone is ordinary fleet vocabulary and would excuse
#: "park the epic and halt"; two of {end-of-epic, park, block} inside 200 characters is an
#: enumeration of the trigger set and essentially nothing else.
_MIN_SIBLINGS = 2

#: Words that join alternatives rather than starting a new clause, skipped when looking for the
#: adjacent carrier or sibling. Articles and relative pronouns are deliberately NOT here: "a" and
#: "that" are what let "a lane that halted at 3/3" and "a halted foreman" look attached to a noun
#: they were never attached to.
GLUE = frozenset({"or", "and", "nor", "never", "only", "not", "no", "either", "neither"})

#: Linking verbs that turn a LEFT carrier + `halted` from subject-and-verb ("the lane halted")
#: into subject-and-adjective ("the lane is halted"). Explicit and short on purpose: every word
#: added here is another way to put a left carrier back beside a bare `halted`.
COPULAS = frozenset({"is", "was", "are", "were", "stays", "remains"})

#: Raw field flags whose next token is a VALUE. Checked on the raw text, because `_neighbour`
#: strips the dashes and `state` / `urgency` are carriers in their own right.
VALUE_FLAGS = ("--state", "--urgency")
#: A value flag ending right where `halted` begins: `--state halted`, `--state=halted`, with
#: backticks or bold between. The lookbehind keeps `x--state` and `--states` from counting.
_VALUE_FLAG_BEFORE = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(f) for f in VALUE_FLAGS) + r")[\s`*=]*$"
)

#: (skill, token, reason). See the module docstring. Every entry must still match something.
EXEMPTIONS: tuple[tuple[str, str, str], ...] = tuple(
    (jr, token, reason)
    for jr in ("jr-dev-1", "jr-dev-2", "jr-dev-3")
    for token, reason in (
        ("halt_reason",
         "a residual state-file key that is still on disk; the instruction is to find and drop "
         "it, and a renamed key is one the reader cannot find in their own file"),
    )
)

# A word, hyphens and underscores included, so `halt-to-human` and `halt_reason` are seen whole
# rather than as a bare `halt` beside unrelated text.
_WORD = re.compile(r"[A-Za-z]+(?:[-_][A-Za-z]+)*")
# Markdown and punctuation that sits between a word and its neighbour without separating them.
_NOISE = "`*_\"'()[]<>,.;|/—–:= \t\n"


def _neighbour_at(words: list[str], start: int, step: int) -> tuple[int, str | None]:
    """(index, word) of the nearest meaningful word from *start* in direction *step*."""
    i = start + step
    while 0 <= i < len(words):
        w = words[i].strip(_NOISE).lstrip("-").lower()
        if not w or w in GLUE:
            i += step
            continue
        return i, w
    return i, None


def _neighbour(words: list[str], start: int, step: int) -> str | None:
    """The nearest meaningful word from *start* in direction *step*, skipping GLUE."""
    return _neighbour_at(words, start, step)[1]


def _is_anchored(text: str, match: re.Match, words: list[str], idx: int) -> bool:
    """True when this `halted` sits where a value of its field belongs.

    Grammar, never meaning - see the module docstring. A left carrier is a subject unless a
    copula, a code span or a value flag says otherwise.
    """
    # ASSIGNMENT: `halted: true`, `halted: Q-0007`. The colon makes it a key/value outright.
    tail = text[match.end():match.end() + 2].lstrip("`*")
    if tail.startswith(":"):
        return True
    # VALUE POSITION: `--state halted`, `--urgency halted`, read raw before dashes are stripped.
    if _VALUE_FLAG_BEFORE.search(text, 0, match.start()):
        return True
    right = _neighbour(words, idx, 1)
    if right is not None and (right in CARRIERS or right in SIBLINGS):
        return True
    at, left = _neighbour_at(words, idx, -1)
    if left is None:
        return False
    if left in SIBLINGS:
        return True
    if left in COPULAS:
        return _neighbour(words, at, -1) in CARRIERS
    if left in CARRIERS:
        line_start = text.rfind("\n", 0, match.start()) + 1
        return text.count("`", line_start, match.start()) % 2 == 1
    return False


def _is_save_trigger_value(text: str, match: re.Match) -> bool:
    """True when this `halt` is the save-trigger VALUE rather than a verb.

    Two shapes count, and both are structural: the token sits beside the parameter that takes it
    (`trigger=`), or inside an enumeration of the trigger set (two or more of its siblings).
    """
    lo = max(0, match.start() - _SAVE_TRIGGER_WINDOW)
    window = text[lo:match.end() + _SAVE_TRIGGER_WINDOW]
    if "trigger=" in window or "trigger =" in window:
        return True
    # WORD BOUNDARIES, not substrings, and the difference is the whole carve-out.
    # `park` is inside `parked`; `block` is inside `blocked` and `blocker`. Those are
    # ordinary fleet vocabulary, not trigger names, so a substring test excuses precisely
    # the prose this gate exists to catch - "a blocker you own is unclearable and the item
    # is parked, halt the loop" carries two APPARENT siblings and no enumeration at all.
    return sum(
        1
        for sibling in SAVE_TRIGGER_SIBLINGS
        if re.search(rf"\b{re.escape(sibling)}\b", window)
    ) >= _MIN_SIBLINGS


def violations(text: str, *, exempt: frozenset[str] = frozenset()) -> list[tuple[int, str, str]]:
    """Every `halt`-family token that breaks clause (a) or (b), as (line, token, why)."""
    words = [m.group(0) for m in _WORD.finditer(text)]
    out: list[tuple[int, str, str]] = []
    for idx, match in enumerate(_WORD.finditer(text)):
        token = match.group(0)
        if "halt" not in token.lower():
            continue
        line = text.count("\n", 0, match.start()) + 1
        if token in exempt:
            continue
        if token in ALLOWED_COMPOUNDS:
            continue
        if token.lower() == "halt" and _is_save_trigger_value(text, match):
            continue
        if token != ROW_STATE:
            out.append((line, token, (
                f"a verb form of halt - a row state is something a row IS, so {token!r} is a "
                f"stop-scheduling or abort sense wearing the row state's word; spell it "
                f"{STOP_WORD!r} or {ABORT_WORD!r}"
            )))
            continue
        if not _is_anchored(text, match, words, idx):
            out.append((line, token, (
                "bare-prose `halted` with no carrier beside it - write the noun it belongs to "
                f"(a `{ROW_STATE}` row / question / lane) or put it in a value position "
                f"(`--state {ROW_STATE}`, beside {sorted(SIBLINGS)})"
            )))
    return out


def _exempt_for(loop: str) -> frozenset[str]:
    return frozenset(token for skill, token, _r in EXEMPTIONS if skill == loop)


def gated_loops() -> list[str]:
    """Every skill directory in the tree except the declared non-loops. Globbed, never listed."""
    excluded = {name for name, _why in NOT_A_LOOP}
    return sorted(
        p.parent.name
        for p in only_tracked(SKILLS_ROOT, SKILLS_ROOT.glob("*/SKILL.md"))
        if p.parent.name not in excluded
    )


def gated_docs(loop: str) -> list[Path]:
    """The loop's own file plus the reference files it hands the reader. See the docstring."""
    home = SKILLS_ROOT / loop
    return [home / "SKILL.md", *only_tracked(SKILLS_ROOT, home.glob("references/*.md"))]


#: Relative posix paths, so a parametrized failure names the file rather than an absolute path.
GATED = [
    doc.relative_to(SKILLS_ROOT).as_posix() for loop in gated_loops() for doc in gated_docs(loop)
]


def _text(rel: str) -> str:
    path = SKILLS_ROOT / rel
    assert path.is_file(), f"missing gated doc: {path}"
    return path.read_text(encoding="utf-8")


def _halt_tokens(text: str) -> int:
    return sum(1 for m in _WORD.finditer(text) if "halt" in m.group(0).lower())


# --------------------------------------------------------------------------------------
# The word is pinned to the code, and the new words are not row states
# --------------------------------------------------------------------------------------


def test_the_row_state_this_gate_protects_is_still_a_field_value() -> None:
    """`halted` is kept BECAUSE it is a live CLI value. Assert that, do not assume it."""
    assert ROW_STATE in AGENT_STATES, (
        f"{ROW_STATE!r} is no longer an `## Agents` status; this gate protects a word the code "
        f"has stopped accepting. States are {list(AGENT_STATES)}"
    )
    assert ROW_STATE in URGENCY_ORDER, (
        f"{ROW_STATE!r} is no longer a `## Questions` urgency; urgencies are {list(URGENCY_ORDER)}"
    )


@pytest.mark.parametrize("word", [STOP_WORD, ABORT_WORD])
def test_the_replacement_words_are_not_row_states(word: str) -> None:
    """The rename must not have created a second spelling of the row state.

    The whole point is that `halted` names ONE thing. If `stop` were also accepted by
    `--state`, a lane could write either and the ambiguity would be back with new words.
    """
    assert word not in AGENT_STATES, f"{word!r} became an `## Agents` status; it names a BEHAVIOUR"
    assert word not in URGENCY_ORDER, f"{word!r} became a `## Questions` urgency"


@pytest.fixture(autouse=True)
def _no_real_pings(monkeypatch):
    """A halted transition is a ping site, and NTFY_* are set in real fleet shells."""
    monkeypatch.setattr(ntfy, "notify", lambda trigger, **kw: None)


def test_the_cli_still_accepts_the_row_state_and_rejects_the_new_words(tmp_path: Path) -> None:
    """The behavioural half: `status --state halted` works, `--state stop` does not.

    Asserted through the CLI rather than against the enum alone, because the enum is what the
    epic promised NOT to migrate. A green here is the evidence that this stayed a text change.
    """
    reg = tmp_path / "projects" / "demo" / "status" / "registry.md"
    lock = tmp_path / "locks"
    lock.mkdir()
    base = ["--file", str(reg), "--lock-dir", str(lock), "--project", "demo", "--timeout", "5"]
    assert main(["init", *base]) == 0
    assert main(["ask", *base, "--actor", "jr-dev-1", "--urgency", ROW_STATE,
                 "--question", "which way?", "--default", "the first one"]) == 0

    assert main(["status", *base, "--actor", "jr-dev-1", "--state", ROW_STATE,
                 "--waiting-on", "Q-0001"]) == 0, (
        f"`status --state {ROW_STATE}` was refused; the row state is the one thing this epic "
        "promised to leave alone"
    )
    for word in (STOP_WORD, ABORT_WORD):
        # argparse builds `--state`'s choices from AGENT_STATES, so an unknown value exits 2
        # before any registry code runs. That exit IS the rejection.
        with pytest.raises(SystemExit) as exc:
            main(["status", *base, "--actor", "jr-dev-1", "--state", word,
                  "--eta", "2026-09-05T12:00Z"])
        assert exc.value.code == 2, (
            f"`status --state {word}` was ACCEPTED; {word!r} names a scheduling behaviour, not a "
            "row state, and a second spelling of the row state re-creates gh#148"
        )
        assert f"| {ROW_STATE} " in reg.read_text(encoding="utf-8"), (
            f"the refused `--state {word}` write disturbed the row it could not set"
        )



# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("rel", GATED)
def test_no_loop_skill_spells_a_stop_or_an_abort_as_halt(rel: str) -> None:
    found = violations(_text(rel), exempt=_exempt_for(rel.split("/")[0]))
    assert not found, (
        f"skills/{rel} uses the halt family outside the row state:\n  "
        + "\n  ".join(f"line {ln}: {tok!r} - {why}" for ln, tok, why in found)
    )


def test_every_registry_agent_is_gated() -> None:
    """A name the code dispatches on must be a file this gate reads.

    The exclusion list is the one way a skill leaves the sweep, so it is also the one way this
    epic's defect could come back. Fail HERE, naming the agent, rather than by going quiet.
    """
    missing = sorted(set(AGENTS) - set(gated_loops()))
    assert not missing, (
        f"{missing} are `registry.AGENTS` lanes with no gated SKILL.md - either the skill was "
        "renamed or it was excluded, and either way the fleet's own lanes are unguarded"
    )


def test_no_excluded_skill_is_a_self_waking_loop() -> None:
    """Exclusions are checked against the tree, not taken on trust.

    A loop writes its own `## Agents` row and nothing else here does. So an exclusion that
    starts naming itself as `--actor` has become a loop and must leave this list - which is
    what stops the list being a place to hide a skill the gate would red.
    """
    problems = []
    for name, _why in NOT_A_LOOP:
        path = SKILLS_ROOT / name / "SKILL.md"
        if not path.is_file():
            problems.append(f"{name}: excluded but has no SKILL.md; delete the dead entry")
            continue
        if name in AGENTS:
            problems.append(f"{name}: excluded but is a `registry.AGENTS` lane")
        if _SELF_ACTOR.format(name) in path.read_text(encoding="utf-8"):
            problems.append(f"{name}: excluded but writes its own `## Agents` row - it is a loop")
    assert not problems, "\n  ".join(["the NOT_A_LOOP list no longer matches the tree:", *problems])


def test_the_gate_covers_the_surface_it_was_built_for() -> None:
    """A sweep that finds nothing reads exactly like a sweep that found nothing wrong.

    If the prose is reorganised or `_WORD` is broken by an edit, the gate goes quiet while
    staying green. Assert it still SEES the occurrences it was built against.

    PER LOOP, NOT IN TOTAL. The first cut summed 129 across the whole set, which is the one
    shape a floor exists to catch and cannot: a single loop falling to zero is invisible under a
    total the other six carry. Measured when this landed, over `SKILL.md` plus `references/*.md`:
    27 lead-dev, 26/26/26 juniors, 11 auditor, 7 steward, 7 dev-manager. The floor is checked
    against the `registry.AGENTS` lanes only, because those are the files it was measured
    against; a newly globbed skill is still GATED, it just cannot be held to a count nobody took.
    """
    seen = {loop: sum(_halt_tokens(_text(d.relative_to(SKILLS_ROOT).as_posix()))
                      for d in gated_docs(loop))
            for loop in gated_loops()}
    thin = {loop: n for loop, n in seen.items() if loop in AGENTS and n < PER_LOOP_FLOOR}
    assert not thin, (
        f"the sweep sees fewer than {PER_LOOP_FLOOR} halt-family tokens in {thin}; the lowest "
        f"real count when this was written was 7, so the scanner is likelier broken there than "
        f"the prose thinned. Full picture: {seen}"
    )


def test_every_exemption_is_still_load_bearing() -> None:
    """A dead exemption is an unguarded hole that looks like a considered decision."""
    stale = [
        f"{skill}: {token!r} ({reason[:60]}...)"
        for skill, token, reason in EXEMPTIONS
        if not any(token in _text(d.relative_to(SKILLS_ROOT).as_posix())
                   for d in gated_docs(skill))
    ]
    assert not stale, (
        "these exemptions no longer match anything and must be deleted rather than left as "
        "standing permission:\n  " + "\n  ".join(stale)
    )


# --------------------------------------------------------------------------------------
# What the predicate does and does not flag
# --------------------------------------------------------------------------------------

#: Real stop-scheduling and abort sentences from the tree BEFORE the rename. Each is a sense the
#: rename removed, and each must red - this is the regression the gate exists for.
_PRE_RENAME = (
    "**Genuinely halt the loop (do NOT ScheduleWakeup a continuation)** only when:\n",
    "STDERR: **halt this wake and ntfy the error** - do not fall back to the injected block.\n",
    "- **Never halt on an empty pass** (builder-loop convention).\n",
    "Then continue (next claim / idle) - **never halt-to-human**; lead-dev owns resolution.\n",
    "**An empty queue is NOT a halt condition - not even after polish mode comes back dry.**\n",
    "4. End the turn. The loop never halts - it advances across context resets.\n",
    "During an epic, prefer parking over halting.\n",
    "**Why terminal halting was removed, so nobody reinstates it.**\n",
)

#: Bare-prose `halted` in a stop sense - clause (b)'s reason for existing. Every one of these was
#: a real line, and every one of them would slip past a check that only banned the verb forms.
_BARE_PROSE = (
    "so a lane that halted at 3/3 came back still at 3/3 and halted again on its first wake.\n",
    "A halted foreman is not one idle loop, it is four.\n",
    "skipped only for `--once`, or when halted on a blocker naming a live `Q-`/`B-` id\n",
)


@pytest.mark.parametrize("passage", _PRE_RENAME, ids=range(len(_PRE_RENAME)))
def test_the_predicate_reds_on_a_pre_rename_stop_or_abort(passage: str) -> None:
    assert violations(passage), f"a stop/abort still spelled `halt` slipped through:\n{passage}"


@pytest.mark.parametrize("passage", _BARE_PROSE, ids=range(len(_BARE_PROSE)))
def test_the_predicate_reds_on_bare_prose_halted(passage: str) -> None:
    found = violations(passage)
    assert found, f"bare-prose `halted` slipped through:\n{passage}"
    assert all(tok == ROW_STATE for _ln, tok, _w in found), (
        "these passages test clause (b); flagging them as verb forms means the token scanner, "
        f"not the anchor rule, caught them: {found}"
    )


#: Every position the surviving prose actually uses. If one of these ever reds, the gate has
#: started rejecting correct writing and would be disabled rather than obeyed.
_LEGITIMATE = (
    "`--eta` is MANDATORY on every `status` write except `--state halted`.",
    "--state building|waiting|halted|idle [--work E-NNNN-<name>]",
    "raise it via `ask --urgency halted --question \"...\" --default \"...\"`",
    "reports every `## Agents` row observed in the `halted` row state",
    "a `halted` row it could NOT judge is reported too, and is MORE stuck than a stranded one",
    "send ONE nag (\"open dev-manager - questions pending\") for the `halted` rows it printed.",
    "a `halted` row is exempt from `eta` and excluded from the missed-wake alarm",
    "proceed per the question's advisory/waiting/halted class",
    "Sort **halted > waiting > advisory**, oldest `raised_at` first.",
    "if your state file still carries `empty_wakes` or `halted: true`, drop them",
    "This is what makes halted-detection trustworthy.",
    "every cadence figure sourced, halted-by-design rows excluded",
    "every builder `halted`, or `idle` with nothing claimable in the `## Queue`",
    "`waiting`/`advisory` ONLY - never `halted` urgency.",
    "set your own Agents row `halted` via `status --state halted --waiting-on Q-NNNN`",
)


#: A LEFT carrier with nothing between it and bare `halted` is subject + past-tense verb - the
#: stop sense (E-0953, gh#264). These three are the issue's own sentences; the first symmetric
#: carrier check read each as anchored because the noun sat beside the word, on the wrong side.
_LEFT_CARRIER_VERB = (
    "the lane halted at 3/3 and never woke",
    "the row halted the loop entirely",
    "a question halted the fleet",
    # A balanced code span earlier on the line must not make the bare word look quoted.
    "the `row` halted the loop",
    # A flag-like prefix that is not the flag itself is not a value position.
    "the x--state halted the loop",
)

#: The shapes in which a left carrier still anchors, plus the unchanged right-carrier case. Each
#: must stay green, or the left-side rule has started rejecting correct writing.
_LEFT_CARRIER_ANCHORED = (
    "the row is halted on Q-0001",
    "the lane stays halted until the answer lands",
    "set your own Agents row `halted` and keep firing",
    "write `status --state halted` before ending the turn",
    "raise it via `ask --urgency halted`",
    "status --state halted --waiting-on Q-0001",
    "status --state=halted --waiting-on Q-0001",
    "the row **`halted`** today",
    "a `halted` row is exempt from the missed-wake alarm",
    "a halted row is exempt from the missed-wake alarm",
)


@pytest.mark.parametrize("passage", _LEFT_CARRIER_VERB, ids=range(len(_LEFT_CARRIER_VERB)))
def test_the_predicate_reds_on_a_left_carrier_past_tense_verb(passage: str) -> None:
    found = violations(passage)
    assert found, f"a left carrier excused a past-tense `halted` (the stop sense):\n{passage}"
    assert all(tok == ROW_STATE for _ln, tok, _w in found), (
        f"these test the left-carrier rule; anything other than `halted` flagged is noise: {found}"
    )


@pytest.mark.parametrize("passage", _LEFT_CARRIER_ANCHORED, ids=range(len(_LEFT_CARRIER_ANCHORED)))
def test_a_left_carrier_still_anchors_through_copula_code_span_or_value_flag(passage: str) -> None:
    assert violations(passage) == [], (
        f"the left-carrier rule flagged a correct row-state use:\n{passage}\n{violations(passage)}"
    )


@pytest.mark.parametrize("passage", _LEGITIMATE, ids=range(len(_LEGITIMATE)))
def test_the_predicate_accepts_the_row_state_in_every_position_the_prose_uses(passage: str) -> None:
    assert violations(passage) == [], (
        f"the gate flagged a correct row-state use; a gate that cries wolf gets disabled:\n"
        f"{passage}\n{violations(passage)}"
    )


def test_an_exemption_covers_only_its_own_token() -> None:
    """An exemption excuses one spelling, never the paragraph it sits in.

    Worth pinning because the cheap implementation - exempting a FILE, or a line - would let a
    genuine stop-spelled-halt ride in beside the quotation nobody may edit.
    """
    passage = (
        'the registry recorded "RE-HALTED after a manual relaunch into UNCHANGED conditions".\n'
        "After three empty wakes the lane will halt and wait for a human.\n"
    )
    found = violations(passage, exempt=frozenset({"RE-HALTED"}))
    assert [tok for _ln, tok, _w in found] == ["halt"], (
        f"the exemption swallowed a real stop rule beside it: {found}"
    )


# --------------------------------------------------------------------------------------
# The save-trigger value position (E-0213) — narrow, and proved narrow
# --------------------------------------------------------------------------------------
def test_the_save_trigger_vocabulary_still_contains_the_colliding_value() -> None:
    """The excuse exists because a CHECKED value is spelled `halt`. If that stops being true,
    this refinement must narrow with it rather than linger as an unguarded hole."""
    assert "halt" in SAVE_TRIGGERS, (
        "`agentflow.save_summariser.TRIGGERS` no longer contains `halt`, so nothing forces the "
        "loop skills to write the banned token and the value-position excuse in `violations()` "
        "guards nothing. Delete it and let clause (a) apply unmodified"
    )
    assert len(SAVE_TRIGGER_SIBLINGS) >= _MIN_SIBLINGS, (
        f"the trigger set shrank to {sorted(SAVE_TRIGGERS)}: fewer than {_MIN_SIBLINGS} siblings "
        "exist, so the enumeration half of the excuse can never fire and every real call site "
        "would have to carry `trigger=` verbatim"
    )


SUBSTRING_TRAPS = (
    "When a blocker you own is unclearable and the item is parked, halt the loop and wait.",
    "If the queue is empty, park the epic; if the lane is blocked, halt until relaunched.",
    "A parked row and an open blocker both mean the same thing: halt rather than invent work.",
)


@pytest.mark.parametrize("prose", SUBSTRING_TRAPS)
def test_inflected_siblings_do_not_excuse_a_stop_sense_halt(prose: str) -> None:
    """The carve-out matches siblings on WORD BOUNDARIES, and these prove why it must.

    `park` is inside `parked`; `block` is inside `blocked` and `blocker`. Both inflections are
    ordinary fleet vocabulary rather than trigger names, so a substring test counts two
    "siblings" in prose that enumerates nothing - and excuses exactly the stop-sense `halt`
    this gate exists to catch. Every string below is that shape: two apparent siblings, no
    enumeration, and a `halt` that means stop.

    MEASURED when this landed: with substring matching all three were excused, and the whole
    file was still green - so the boundary fix was real and completely unguarded. In the live
    corpus the hole was bounded but not theoretical: 17 windows across the gated skills
    already pair `park*` with `block*`, 8 of them in lead-dev, which is precisely the file
    where park/block/halt decision prose lives.
    """
    assert violations(prose), (
        "a stop-sense `halt` was excused because `parked`/`blocked`/`blocker` were read as "
        "trigger siblings. The carve-out must match its siblings on word boundaries: an "
        "inflection is not an enumeration, and this is the prose gh#148 was written for"
    )


def test_the_real_enumeration_shapes_survive_the_boundary_rule(): 
    """The companion half: tightening the carve-out must not red the shapes the skills use.

    A guard that closes a hole by refusing the legitimate case just moves the cost onto every
    author who writes the duty correctly, and gets itself exempted a week later.
    """
    for prose in (
        "at whichever boundary this firing reached - end-of-epic, park, halt, block - with lane",
        "the trigger this firing reached (`end-of-epic` / `park` / `halt` / `block`), never guessed",
        "**end-of-epic**, **park**, **halt**, **block**",
    ):
        assert not violations(prose), (
            f"a real trigger enumeration was reported: {prose!r}. The carve-out has to excuse "
            "the shapes the five seats actually write, or the duty cannot be documented at all"
        )

def test_a_scheduling_halt_is_not_excused_by_the_trigger_position() -> None:
    """The false-positive channel the excuse opens, closed as a test rather than by assertion.

    The whole cost of gh#148 was `halt` used for the STOP sense. If the excuse were written as
    "any `halt` near fleet vocabulary", it would readmit exactly that: a stop-the-loop sentence
    in a skill that also happens to mention parking an item. So the fixture below is a genuine
    violation wearing the neighbourhood, and it must still be reported.
    """
    stop_sense = (
        "When the queue is empty, park the item and halt the loop until a human relaunches you.\n"
    )
    assert "park" in stop_sense, "the fixture must carry ONE sibling, or it proves nothing"
    found = violations(stop_sense)
    assert [tok for _ln, tok, _why in found] == ["halt"], (
        "a stop-scheduling `halt` beside one piece of trigger vocabulary was excused as a trigger "
        f"value. The excuse must need TWO siblings or an explicit `trigger=`: {found}"
    )


def test_the_trigger_enumeration_is_excused_in_every_shape_the_skills_use() -> None:
    """Both real shapes pass — the bolded prose enumeration and the slashed checklist form."""
    for shape in (
        "the boundary this firing reached - **end-of-epic**, **park**, **halt**, **block** - with\n",
        "the trigger this firing reached (`end-of-epic` / `park` / `halt` / `block`), after the\n",
        '    trigger="halt",                      # the row above that this page is about\n',
    ):
        assert not violations(shape), (
            f"a real save-trigger enumeration was reported as a verb form: {shape!r}. The five "
            "saving seats must be able to name the value the module checks"
        )
