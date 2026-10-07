"""Fleet counts in the skills' prose and the shared code's comments match the roster in code.

`registry.AGENTS` is the fleet roster, `registry.BUILDERS` the seats that build, and
`registry.LANES` the Queue lanes they claim from. The skills used to say "six loops" in nine
places after the roster grew to seven, because nothing tied the prose to the tuples. This test
derives every expected number from those tuples and fails, naming file:line, on any spelled-out
count that disagrees.

WHAT IS SCANNED: every tracked `skills/*/SKILL.md`, and every tracked `.py` under
`skills/_shared/agentflow/` and `skills/_shared/tests/` - comments and docstrings describe the
fleet too, and a stale count there misleads the next reader exactly as a skill's does. The first
widening read only SKILL.md, so "all six loops" survived in the registry's own docstrings and
"three builders in one seam" in a test header after the fourth builder landed.

WHAT IS READ: a count (a number word or digits), at most ONE qualifier word, then `loop`,
`builder`, `lane` or `seat`. Joined by whitespace OR a hyphen ("six-loop", "seven-seat fleet"),
through markdown markup around the number ("**six** loops", "`6` lanes"), and across a wrapped
comment line ("six\\n#: lanes"). A SINGULAR noun is read only as a compound whose head names the
units themselves ("seven loop skills", "a seven-seat fleet"); "seven lane states" counts states,
and "a 260-char lane" counts characters.

A QUALIFIER NAMES A SUBSET, AND EACH SUBSET HAS ITS OWN CONSTANT. "six self-waking lanes" is
correct with seven loops, because it counts every loop but dev-manager; it is pinned to
`SELF_WAKING`, not waved through. "three lane-less seats" is pinned to the seats `LANE_OF` does
not map. A qualifier this table does not know is itself a failure, so a new subset phrase has to
be mapped to the constant it counts - or reworded - before it can pass.

A BARE ONE OR TWO IS NEVER READ AS A FLEET SIZE. "two builders ever share a lane", "two seats
deriving different names", "one lane over" are the arity of a collision or a single actor, and so
is a one or two under a qualifier the table does not map ("two different seats"). The guard below
keeps every expected count above two, so neither can collide with a correct total. A one, two or
"both" under a MAPPED qualifier IS read: "the two junior lanes" and "both jr lanes" name a roster
subset, and a two there is exactly the shape the fleet's growth from two juniors left behind.

THE LEDGER (`EXEMPT`) names, per file, the phrases that legitimately carry a number the table
would reject: a dated incident that was true when it happened, a fixture's own row count, a
subset with no roster constant (the save-duty seats), a right count in a bare loose phrase, and
(`FOLLOW_UP`) a stale count the widening found but did not edit - named, so it stays visible
rather than waved through. Every entry must still match something, so the ledger cannot outlive
the text it excuses.

KNOWN BLIND SPOTS: a count written as a phrase the matcher does not parse ("six of the seven
loops" reads only "seven loops"), a noun outside the four ("six skills"), a singular compound
whose head is not in `UNIT_HEADS`, an ordinal ("the fleet's sixth loop"), and a one, two or
"both" that is bare or under an unmapped qualifier ("BOTH parallel builders").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow.registry import AGENTS, BUILDERS, LANE_OF, LANES
from agentflow.tracked_paths import only_tracked

SKILLS = Path(__file__).resolve().parents[2]
REPO = SKILLS.parent
SELF = Path(__file__).resolve()

#: The committed files, not the checkout: an untracked scratch file must not decide the verdict.
SKILL_FILES = only_tracked(SKILLS, SKILLS.glob("*/SKILL.md"))
#: This file is excluded: its docstring and controls quote stale counts on purpose.
PY_FILES = [
    p for p in only_tracked(
        SKILLS,
        [*(SKILLS / "_shared" / "agentflow").glob("*.py"), *(SKILLS / "_shared" / "tests").glob("*.py")],
    )
    if p.resolve() != SELF
]
FILES = [*SKILL_FILES, *PY_FILES]

#: The loops that continue themselves: every seat but dev-manager, whose unattended pump moved
#: to the steward. Grounded below against which skills actually run the self-wake preflight.
SELF_WAKING = tuple(a for a in AGENTS if a != "dev-manager")
PREFLIGHT_CALL = "python -m agentflow.preflight --tools"
#: The builders that are not the primary.
JUNIORS = tuple(b for b in BUILDERS if b != "lead-dev")
#: The seats with no Queue lane.
LANELESS = tuple(a for a in AGENTS if a not in LANE_OF)

WORDS = {
    "one": 1, "two": 2, "both": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

#: noun (plural) -> qualifier -> expected count. `None` is the bare "<n> <noun>".
EXPECTED: dict[str, dict[str | None, int]] = {
    "loops": {
        None: len(AGENTS),
        "known": len(AGENTS),
        "self-waking": len(SELF_WAKING),
        "builder": len(BUILDERS),
    },
    "builders": {
        None: len(BUILDERS),
        "sibling": len(BUILDERS) - 1,
    },
    "lanes": {
        None: len(LANES),
        "builder": len(LANES),
        "sibling": len(LANES) - 1,
        "junior": len(JUNIORS),
        "jr": len(JUNIORS),
        # "lanes" in the loose sense of every self-continuing seat, not Queue lanes.
        "self-waking": len(SELF_WAKING),
    },
    "seats": {
        None: len(AGENTS),
        "lane-less": len(LANELESS),
        "builder": len(BUILDERS),
        "self-waking": len(SELF_WAKING),
    },
}
#: The head words that make a singular noun count the units themselves ("seven loop skills").
UNIT_HEADS = frozenset({"fleet", "skill", "skills", "command", "commands", "roster"})

_MARK = r"[*_`]*"
# A hyphen, or whitespace that may wrap onto a continued `#` / `#:` comment line.
_SEP = r"(?:-|\s+(?:#:?[ \t]*)?)"
# The lookbehind keeps "#241 every loop", "2.6 lane" and "e-3 lanes" from reading as counts.
COUNT_NOUN = re.compile(
    r"(?<![\w.#§-])" + _MARK + r"(?P<n>" + "|".join(WORDS) + r"|\d+)" + _MARK + r"(?P<sep>" + _SEP + ")"
    + r"(?:(?P<qual>[A-Za-z][\w-]*)" + _MARK + _SEP + r")?"
    + r"(?P<noun>loop|builder|lane|seat)(?P<s>s?)\b(?!-)" + _MARK
    + r"(?:" + _SEP + r"(?P<head>[A-Za-z]+))?",
    re.IGNORECASE,
)


# Ledger reasons. Each names WHY a number the table rejects is not a roster claim to fix here.
HISTORICAL = "a dated or 'of the time' incident: the count was true when it happened"
FIXTURE = "counts a scenario's or fixture's own rows, not the fleet"
SAVE_SUBSET = "the save-duty seats (lead-dev, the juniors, dev-manager): a subset with no registry constant"
SUBSET = "counts a named subset with no registry constant, not the fleet"
LOOSE_SELF_WAKING = (
    "'lanes'/'loops' in the loose sense of the self-waking seats, which number six - a right "
    "count in a bare phrase; saying 'self-waking' would let the table check it and drop the entry"
)
FOLLOW_UP = "stale against the live roster, in a file the widening did not edit; fix it and drop the entry"

#: path -> needle -> reason. A hit is excused only when the needle, whitespace-normalised, sits over
#: that hit's own count in the file (`_needle_covers`). Needles are prose, never line numbers.
EXEMPT: dict[str, dict[str, str]] = {
    "skills/_shared/agentflow/atomic_io.py": {
        "the design tells six lanes to do constantly": LOOSE_SELF_WAKING,
        "Observed live on 2026-08-31 with six lanes active": HISTORICAL,
    },
    "skills/_shared/agentflow/backoff.py": {
        "blackouts the six lanes logged 523 scheduled firings": HISTORICAL,
        "and six lanes each backing off": LOOSE_SELF_WAKING,
        "a fleet-shared log, and six #: lanes observe within one wake cycle": LOOSE_SELF_WAKING,
        "~200 bytes a record, six lanes": LOOSE_SELF_WAKING,
        "with six lanes it reaches the ladder's top": LOOSE_SELF_WAKING,
        "safe under six concurrent lanes": LOOSE_SELF_WAKING,
        "wedge all six lanes for a wake interval": LOOSE_SELF_WAKING,
        "make six concurrent lanes safe": LOOSE_SELF_WAKING,
        "six lanes observing within one wake cycle": LOOSE_SELF_WAKING,
        "multiplied by six lanes an hour": LOOSE_SELF_WAKING,
    },
    "skills/_shared/agentflow/preflight.py": {
        "Six loops now instruct a `CronCreate` call": LOOSE_SELF_WAKING,
    },
    "skills/_shared/agentflow/refs.py": {
        "a checkout six lanes share": LOOSE_SELF_WAKING,
        '"six lanes share': LOOSE_SELF_WAKING,
    },
    "skills/_shared/agentflow/registry.py": {
        "in which TWO of six lanes carried": HISTORICAL,
    },
    "skills/_shared/agentflow/stream_report.py": {
        "Five seats repeated it for roughly ten sweeps": HISTORICAL,
    },
    "skills/_shared/agentflow/wake_watch.py": {
        "stopped all six lanes for days at a time": HISTORICAL,
        "against four stalled lanes": FIXTURE,
    },
    "skills/_shared/tests/test_backoff.py": {
        "Six lanes share the log": LOOSE_SELF_WAKING,
        "an instruction six prose loops cannot follow": LOOSE_SELF_WAKING,
        "into a log five other lanes read": FIXTURE,
        "Six lanes observe within one wake cycle": LOOSE_SELF_WAKING,
        "Six lanes each record a STILL": FIXTURE,
        "Five lanes see stillness": FIXTURE,
        "would have five lanes at the floor": FIXTURE,
        "Six lanes, interleaved, no lock": FIXTURE,
        "Five more lanes then observe": FIXTURE,
        "six lanes share the log, so the": LOOSE_SELF_WAKING,
    },
    "skills/_shared/tests/test_backoff_cli.py": {
        "the only form six prose loops can actually ask": LOOSE_SELF_WAKING,
        "Six lanes append to one file": LOOSE_SELF_WAKING,
    },
    "skills/_shared/tests/test_grounding_precondition_prose.py": {
        "THREE SEATS, THREE DUTIES": SUBSET,
    },
    "skills/_shared/tests/test_halt_vocabulary.py": {
        "the five seats that carry the save duty": SAVE_SUBSET,
        "When the five saving seats gained the duty": SAVE_SUBSET,
        "the shapes the five seats actually write": SAVE_SUBSET,
    },
    "skills/_shared/tests/test_harness_tool_prereqs.py": {
        "Four loops open their wake step with a heading": SUBSET,
    },
    "skills/_shared/tests/test_missed_wake_alarm.py": {
        "the five live loops of the time": HISTORICAL,
        "TWO of six lanes carried": HISTORICAL,
    },
    "skills/_shared/tests/test_recovery_guard.py": {
        "Six loops now instruct a `CronCreate` call": LOOSE_SELF_WAKING,
        "learning that six loops call a tool": LOOSE_SELF_WAKING,
    },
    "skills/_shared/tests/test_registry.py": {
        "Hit live on 2026-08-31 with six lanes active": HISTORICAL,
        "steward reaps three dead lanes": FIXTURE,
        "the only channel from seven autonomous loops": "a right total (every loop) under a qualifier the table does not map",
    },
    "skills/_shared/tests/test_save_capability.py": {
        "body wires five seats": SAVE_SUBSET,
        "five seats now discharge the duty": SAVE_SUBSET,
    },
    "skills/_shared/tests/test_save_summariser.py": {
        "gates which five seats hold it": SAVE_SUBSET,
        "Five seats repeated it for about ten sweeps": HISTORICAL,
        "the arrangement that fooled five seats": HISTORICAL,
    },
    "skills/_shared/tests/test_skill_call_sites.py": {
        "Four of five named lanes honoured the 2026-09-01": HISTORICAL,
        "the six loop skills of the time": HISTORICAL,
        "a six-tuple for a seven-lane fleet": "'lane' in the loose sense of seat, which number seven - a right count",
        "in the FIVE seats that carry it": SAVE_SUBSET,
        "five seats gaining the duty": SAVE_SUBSET,
        "three of the five seats deliberately carry no fence": SAVE_SUBSET,
    },
    "skills/_shared/tests/test_steward_residue_wiring.py": {
        "would redden five seats that must never make this call": SAVE_SUBSET,
    },
    "skills/_shared/tests/test_wake_watch.py": {
        "Four stalled lanes must not become": FIXTURE,
    },
    "skills/dev-manager/SKILL.md": {
        "You are one of five seats that write": SAVE_SUBSET,
    },
    "skills/wiki-save/SKILL.md": {
        "A lane duty for five seats": SAVE_SUBSET,
        "for **five seats, named rather than described**": SAVE_SUBSET,
    },
}


def _value(token: str) -> int:
    return int(token) if token.isdigit() else WORDS[token.lower()]


def _norm(s: str) -> str:
    return " ".join(s.split())


def count_hits(text: str):
    """Every count phrase in `text` the pin reads: (match, n, qualifier, plural noun)."""
    for m in COUNT_NOUN.finditer(text):
        n = _value(m["n"])
        qualifier = m["qual"].lower() if m["qual"] else None
        noun = m["noun"].lower() + "s"
        # One and two are arity ("two builders ever share a lane") unless a mapped qualifier
        # names a roster subset ("the two junior lanes"), whose right count is never below three.
        if n <= 2 and qualifier not in EXPECTED[noun].keys() - {None}:
            continue
        # A singular noun counts the units only as "six-loop ..." (hyphen straight onto the
        # number) or before a head that names them ("seven loop skills"); "seven lane states"
        # counts states.
        hyphenated = m["qual"] is None and m["sep"] == "-"
        if not m["s"] and not hyphenated and (m["head"] or "").lower() not in UNIT_HEADS:
            continue
        yield m, n, qualifier, noun


def _phrase(m: re.Match) -> str:
    """The count through the noun, plus the head word of a singular compound."""
    return _norm(m.string[m.start():m.end("head") if not m["s"] and m["head"] else m.end("s")])


def _needle_covers(text: str, m: re.Match, needle: str) -> bool:
    """True when `needle` occurs in `text` (whitespace-normalised) over THIS hit's count.

    Positional, not "somewhere nearby": two ledgered phrases a line apart must each excuse only
    their own hit, or one entry silently covers a neighbour and the other reads as rotted."""
    core = _norm(text[m.start("n"):m.end("s")])
    at = needle.find(core)
    if at < 0:
        return False
    raw_before = text[max(0, m.start("n") - 400):m.start("n")]
    before = _norm(raw_before) + (" " if raw_before[-1:].isspace() else "")
    joined = before + _norm(text[m.start("n"):m.end("s") + 400])
    start = len(before) - at
    return start >= 0 and joined[start:start + len(needle)] == needle


def wrong_counts(text: str, label: str, exempt: dict[str, str] | None = None,
                 used: set[str] | None = None) -> list[str]:
    """Every count phrase in `text` that disagrees with the roster, as `label:line: phrase`.

    `exempt` is one file's ledger; each needle that excuses a hit is added to `used`."""
    out = []
    for m, n, qualifier, noun in count_hits(text):
        table = EXPECTED[noun]
        if qualifier not in table:
            why = (f"qualifier {qualifier!r} is not mapped to a roster constant for {noun!r}; "
                   "add it to EXPECTED or reword")
        elif n != table[qualifier]:
            why = f"expected {table[qualifier]}"
        else:
            continue
        hit = next((k for k in (exempt or {}) if _needle_covers(text, m, k)), None)
        if hit is not None:
            if used is not None:
                used.add(hit)
            continue
        line = text.count("\n", 0, m.start()) + 1
        out.append(f"{label}:{line}: {_phrase(m)!r} ({why})")
    return out


def _label(path: Path) -> str:
    return path.resolve().relative_to(REPO.resolve()).as_posix()


def test_rosters_are_what_the_prose_is_pinned_to():
    # Guard the derivations: an empty or duplicated tuple would make every count "right".
    for roster in (AGENTS, BUILDERS, LANES):
        assert len(roster) == len(set(roster)) > 0
    assert "dev-manager" in AGENTS
    assert set(BUILDERS) <= set(AGENTS)
    assert set(LANE_OF) == set(BUILDERS)
    # One and two are skipped as arity idioms only because no right count can be either.
    assert min(n for t in EXPECTED.values() for n in t.values()) > 2
    # Every number word the table can expect must be spellable, or a right count would be
    # unreadable as one.
    assert max(n for t in EXPECTED.values() for n in t.values()) <= max(WORDS.values())


def test_the_live_roster_is_the_one_this_pin_was_written_against():
    # Not a pin on the numbers - EXPECTED derives them - but on the ledger: the LOOSE and
    # SAVE reasons assert six self-waking seats and a five-seat save set, so a roster change
    # must re-read the ledger rather than inherit it.
    assert (len(AGENTS), len(BUILDERS), len(LANES), len(SELF_WAKING)) == (7, 4, 4, 6), (
        "the roster moved; re-check every EXEMPT entry whose reason quotes a count"
    )


def test_self_waking_is_the_set_that_runs_the_self_wake_preflight():
    wired = tuple(
        a for a in AGENTS
        if PREFLIGHT_CALL in (SKILLS / a / "SKILL.md").read_text(encoding="utf-8")
    )
    assert wired == SELF_WAKING, (
        f"skills running `{PREFLIGHT_CALL}` are {wired}, but SELF_WAKING is {SELF_WAKING}; "
        "the 'self-waking' count is pinned to the wrong set"
    )


def test_the_corpus_reads_every_loop_skill_and_the_shared_code():
    names = {p.parent.name for p in SKILL_FILES}
    assert set(AGENTS) <= names, f"loop skills missing from the scan: {set(AGENTS) - names}"
    labels = {_label(p) for p in PY_FILES}
    for must in ("skills/_shared/agentflow/registry.py", "skills/_shared/agentflow/fleet_report.py",
                 "skills/_shared/tests/test_work_section_seam.py"):
        assert must in labels, f"{must} fell out of the scan"
    assert _label(SELF) not in labels


def test_every_ledger_path_is_in_the_corpus():
    labels = {_label(p) for p in FILES}
    assert set(EXEMPT) <= labels, f"ledger names files the scan never reads: {set(EXEMPT) - labels}"


@pytest.mark.parametrize("path", FILES, ids=_label)
def test_fleet_counts_match_roster(path: Path):
    text = path.read_text(encoding="utf-8")
    label = _label(path)
    bad = wrong_counts(text, label, EXEMPT.get(label))
    assert not bad, (
        f"fleet count disagrees with registry (AGENTS={len(AGENTS)}, BUILDERS={len(BUILDERS)}, "
        f"LANES={len(LANES)}, self-waking={len(SELF_WAKING)}); fix the count, drop the "
        "number, or (only for a dated incident, a fixture, or an unmapped subset) add a "
        "reasoned EXEMPT entry:\n" + "\n".join(bad)
    )


def test_every_ledger_entry_still_excuses_something():
    stale = []
    for label, entries in EXEMPT.items():
        text = (REPO / label).read_text(encoding="utf-8")
        used: set[str] = set()
        wrong_counts(text, label, entries, used)
        stale += [f"{label}: {k!r}" for k in entries if k not in used]
    assert not stale, "EXEMPT entries that excuse nothing any more - delete them:\n" + "\n".join(stale)


def _word(n: int) -> str:
    return next(w for w, v in WORDS.items() if v == n)


def test_negative_control_wrong_counts_are_flagged():
    wrong = len(AGENTS) - 1
    word = _word(wrong)
    sample = (
        f"the single\ncoordination surface all {word} loops share.\n"
        f"{word.capitalize()}\nloops appending to one store. Also {wrong} loops here.\n"
        f"the {word} known loops; all {_word(len(SELF_WAKING) - 1)} self-waking lanes;\n"
        f"{_word(len(BUILDERS) - 1)} builders in one seam; {len(LANES) + 1} lanes; "
        f"{_word(len(LANES))} sibling lanes; six mystery loops.\n"
        f"a {word}-loop fleet; the {_word(len(BUILDERS) - 1)}-builder module; **{word}** loops;\n"
        f"`{wrong}` lanes; the {word} loop skills; ALL {word.upper()} SEATS; "
        f"the {_word(len(LANELESS) + 1)} lane-less seats;\n"
        f"# a comment that wraps: all {word}\n#: loops read it.\n"
        "the two junior lanes; both jr lanes' claims; **two** sibling lanes; one builder loop.\n"
    )
    bad = [b.split(" (")[0] for b in wrong_counts(sample, "synthetic")]
    assert bad == [
        f"synthetic:2: '{word} loops'",
        f"synthetic:3: '{word.capitalize()} loops'",
        f"synthetic:4: '{wrong} loops'",
        f"synthetic:5: '{word} known loops'",
        f"synthetic:5: '{_word(len(SELF_WAKING) - 1)} self-waking lanes'",
        f"synthetic:6: '{_word(len(BUILDERS) - 1)} builders'",
        f"synthetic:6: '{len(LANES) + 1} lanes'",
        f"synthetic:6: '{_word(len(LANES))} sibling lanes'",
        "synthetic:6: 'six mystery loops'",
        f"synthetic:7: '{word}-loop fleet'",
        f"synthetic:7: '{_word(len(BUILDERS) - 1)}-builder module'",
        f"synthetic:7: '**{word}** loops'",
        f"synthetic:8: '`{wrong}` lanes'",
        f"synthetic:8: '{word} loop skills'",
        f"synthetic:8: '{word.upper()} SEATS'",
        f"synthetic:8: '{_word(len(LANELESS) + 1)} lane-less seats'",
        f"synthetic:9: '{word} #: loops'",
        "synthetic:11: 'two junior lanes'",
        "synthetic:11: 'both jr lanes'",
        "synthetic:11: '**two** sibling lanes'",
    ]


def test_positive_control_right_counts_subsets_pairs_and_other_units_pass():
    sample = (
        f"all {_word(len(AGENTS))} loops share it; {len(AGENTS)} loops in all; the "
        f"{_word(len(AGENTS))} known loops. Resets the streak for all "
        f"{_word(len(SELF_WAKING))} self-waking lanes; {_word(len(BUILDERS))} builders share "
        f"one store with {_word(len(LANES) - 1)} sibling lanes; {len(LANES)} lanes; the "
        f"{_word(len(JUNIORS))} junior lanes; all {_word(len(LANELESS))} lane-less seats; a "
        f"{_word(len(AGENTS))}-seat fleet; **{_word(len(BUILDERS))} builders**; the "
        f"{_word(len(AGENTS))} loop skills.\n"
        "No two builders ever share a lane; an epic present in two lanes; two loops; two seats "
        "deriving names; one lane over; the builder loops. Seven lane states, eight classes; "
        "a 260-char lane; since #241 every loop skill; the §2.6 lane publishing; at most 3 per "
        "lane; three loop names. Two different seats; BOTH parallel builders fed; the two "
        "thinnest loops; both lanes; one builder lane.\n"
    )
    assert wrong_counts(sample, "synthetic") == []


def test_ledger_excuses_only_its_own_phrase():
    text = "the design tells six lanes to do constantly; and all six loops read it.\n"
    entries = {"the design tells six lanes to do constantly": FIXTURE}
    used: set[str] = set()
    bad = wrong_counts(text, "synthetic", entries, used)
    assert [b.split(" (")[0] for b in bad] == ["synthetic:1: 'six loops'"]
    assert used == set(entries)
