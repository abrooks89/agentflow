"""The never-halt-on-empty behaviour, given a gate that can actually go red.

WHY THIS FILE EXISTS. `no-terminal-halt-on-empty-input` removed terminal halting cleanly, and
then shipped with nothing protecting it. Its own headline verification - *"a lane whose state
file is hand-set to a former halting condition and then relaunched CLAIMS WORK rather than
halting"* - was never written down as a test, and the one assertion that looked like coverage
(`F2-auditor-blocker-dm`) read a state file the same module had just written, against a literal
that module hardcoded. Both halves would still have passed with the behaviour deleted.

That matters more here than it would elsewhere, because this failure mode is SILENT. A lane that
halts wrongly does not alarm; it simply stops, and the last time that happened it cost roughly
fourteen hours before a human noticed.

WHAT CAN AND CANNOT BE TESTED HERE, stated plainly rather than papered over. A lane's selection
is prose in a `SKILL.md`; there is no `select()` to call, so "run the lane's selection path" is
not literally executable. What IS executable is every mechanism that prose depends on, and those
are what this file pins:

* a state file carrying the FORMER halting keys does not stop the lane claiming a ready row
  (`test_a_lane_carrying_the_old_halt_keys_still_claims`) - the replay, in the part that has code
  underneath it;
* an empty lane cannot even RECORD a halt, because `status --state halted` demands a live `Q-`/
  `B-` referent an empty lane has none of (`test_an_empty_lane_cannot_record_a_halt`);
* the auditor scenario's continuation decision returns "wake, do not halt" for an EMPTY pass
  (`test_the_scenario_decides_to_continue_on_an_empty_pass`);
* no loop `SKILL.md` reintroduces a halt-on-no-work rule, in ANY vocabulary
  (`test_no_loop_reintroduces_a_halt_on_no_work_rule`).

The last one is deliberately not a token search. p190's site table was built by grepping
`empty_wakes` and `consecutive` - the tokens the junior rule happened to use - so lead-dev's halt,
which used neither, returned zero hits and the zero was read as absence. This gate matches the
BEHAVIOUR (halt vocabulary AND-ed with no-work vocabulary) and carries an allowlist of the
kept-on-purpose passages, so a rule written in a fourth vocabulary is caught by construction
rather than by having guessed its words in advance.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path

import pytest

from agentflow import ntfy
from agentflow.registry import main, parse_registry

SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parent
LOOPS = ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor", "steward", "dev-manager")


@pytest.fixture(autouse=True)
def _no_real_pings(monkeypatch):
    """Never POST from a test. NTFY_URL/NTFY_TOPIC are set in real fleet shells, and one of
    these tests deliberately drives a halted transition, which is a ping site."""
    monkeypatch.setattr(ntfy, "notify", lambda trigger, **kw: None)


@pytest.fixture
def board(tmp_path: Path):
    """An initialized registry, plus the data_root a lane state file would sit under."""
    data_root = tmp_path
    reg = data_root / "projects" / "demo" / "status" / "registry.md"
    lock = tmp_path / "locks"
    lock.mkdir()
    base = ["--file", str(reg), "--lock-dir", str(lock), "--project", "demo"]
    assert main(["init", *base]) == 0

    class Board:
        def __init__(self) -> None:
            self.data_root = data_root
            self.project = "demo"
            self.registry_path = reg

        def run(self, *args, timeout: str = "5") -> int:
            return main([args[0], *base, "--timeout", timeout, *args[1:]])

        def reg(self):
            return parse_registry(self.registry_path.read_text(encoding="utf-8"))

    return Board()


# ── The replay: a lane still carrying the former halting keys ────────────────

FORMER_HALT_STATE = """---
loop: jr-dev-1
project: demo
empty_wakes: 3
halted: true
halt_reason: lane empty 3 wakes
starvation_note_written: 2026-09-01T00:03Z
---

# jr-dev-1 loop state

## ON WAKE

Relaunched into unchanged conditions.

## in_progress

## completed

## history
"""


def test_a_lane_carrying_the_old_halt_keys_still_claims(board):
    """The exact overnight failure, replayed: relaunch on a stale halt, then claim.

    The keys are NOT swept from live state files - each lane drops them on its next write - so
    a lane relaunching today genuinely does read `empty_wakes: 3` and `halted: true` off its own
    disk. The old rule read that counter back, found it already at three, and halted again on
    the lane's FIRST empty wake, which is precisely why the human's relaunch did nothing.

    Nothing in the claim path may consult those keys. Asserted here rather than assumed,
    because "no code reads it" is a claim about absence and absence is what nobody checks.
    """
    state = board.data_root / "projects" / board.project / "status" / "jr-dev-1-state.md"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(FORMER_HALT_STATE, encoding="utf-8")

    assert board.run("publish", "--actor", "lead-dev", "--slug", "first-queued-item",
                     "--title", "a ready row", "--lane", "jr-dev-1") == 0
    assert board.run("claim", "--actor", "jr-dev-1", "--id", "E-0001-first-queued-item") == 0

    row = board.reg().queue["E-0001-first-queued-item"]
    assert row.claimed_by == "jr-dev-1"
    assert row.state == "building"
    # And the stale keys are still sitting there, unread by anything that mattered.
    assert "empty_wakes: 3" in state.read_text(encoding="utf-8")


def test_an_empty_lane_cannot_record_a_halt(board):
    """The structural half of never-halt: an empty lane has nothing to wait ON.

    `status --state halted` requires `waiting_on` to name a live `Q-`/`B-` id. A lane with no
    work has no such referent, so the honest halt it would have to declare does not typecheck.
    This is what makes "an empty lane is not a halt condition" a property of the system rather
    than a sentence in a skill file that a future rewrite can quietly drop.
    """
    # Q-99999 does not exist, which is the shape an empty lane is stuck with: there is no
    # question to point at, because "nothing has been published to me" is not a question.
    assert board.run("status", "--actor", "jr-dev-1", "--state", "halted",
                     "--waiting-on", "Q-99999") == 2
    assert board.reg().agents["jr-dev-1"].status != "halted"

    # And omitting the referent entirely is refused too, so there is no way around it.
    assert board.run("status", "--actor", "jr-dev-1", "--state", "halted") == 2
    assert board.reg().agents["jr-dev-1"].status != "halted"


# ── The scenario's continuation decision, for the pass it never runs ─────────

def _load_scenario():
    path = REPO_ROOT / "harness" / "auditor-scenario" / "auditor_scenario.py"
    if not path.is_file():
        pytest.skip(f"auditor scenario not present at {path}")
    spec = importlib.util.spec_from_file_location("_auditor_scenario_for_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_scenario_decides_to_continue_on_an_empty_pass():
    """`decide_continuation([], [])` is the assertion F2 could never make.

    The scenario always audits something, so its state file only ever exercises the full-pass
    case. The empty pass - the one that used to increment a counter and stop - reaches an
    assertion only by asking the decision function directly.
    """
    scenario = _load_scenario()
    scheduled_wake, halt_to_human = scenario.decide_continuation([], [])
    assert scheduled_wake is True
    assert halt_to_human is False


def test_the_scenario_no_longer_hardcodes_the_answer_it_asserts():
    """The defect class, pinned: a writer that cannot record a wrong answer proves nothing.

    `write_auditor_state` used to emit a literal `halt_to_human: false` and take
    `scheduled_wake=True` from its only caller. Both fields must now arrive as arguments, or
    F2 is back to reading its own handwriting.
    """
    scenario = _load_scenario()
    params = inspect.signature(scenario.write_auditor_state).parameters
    assert "halt_to_human" in params, "halt_to_human must be a parameter, not a literal"
    assert "scheduled_wake" in params
    src = inspect.getsource(scenario.write_auditor_state)
    assert "halt_to_human: false" not in src, "the writer hardcodes the value the gate reads back"


# ── The behaviour gate: no halt-on-no-work rule, in any vocabulary ───────────

# Two vocabularies, AND-ed. Neither list is the set of words a past bug used - that is the
# mistake this gate exists to avoid - they are the words the BEHAVIOUR needs in order to be
# expressed at all. A rule that stops a loop because it has nothing to do has to say both
# "stop" and "nothing", in some words.
_HALT_VOCAB = re.compile(
    r"\b(halt|halts|halted|halting|stop|stops|stopped|stopping|terminal|terminate|terminates"
    r"|shut down|shuts down|give up|gives up|abort|aborts|exit(?:s)? for good"
    r"|do(?:es)? not re-?wake)\b",
    re.IGNORECASE,
)
_NO_WORK_VOCAB = re.compile(
    # `nothing` is allowed up to three words of slack before its verb, because English puts
    # them there: "nothing published" and "nothing HAS BEEN published" are the same rule, and
    # the fixed-phrase version of this pattern matched only the first. Found by the
    # parametrized novel-vocabulary test below, which is the whole reason it is parametrized.
    r"\b(empty|no work|dry|zero|starv\w*|exhaust\w*|no input|unfed|idle"
    r"|nothing(?:\s+\w+){0,3}\s+(?:to\s+do|left|selectable|claimable|published|queued|ready))\b",
    re.IGNORECASE,
)

# The kept-on-purpose passages, as DISTINCTIVE SUBSTRINGS rather than whole lines.
#
# Whole-line entries would redden on every reflow of prose nobody meant to change, and a token
# blocklist would be the very failure this gate replaces. A phrase is the middle: it survives
# rewording around it, and a genuinely NEW halt rule contains none of them, so it is caught
# without anyone having predicted its wording.
#
# Adding an entry here is a deliberate act that shows up in a diff. That is the point: the only
# way to make this gate green is to say, in review, that a new halt-adjacent line is on purpose.
_ALLOWED = (
    # ---- the state enum, quoted in every loop's status-write example ----
    "building|waiting|halted|idle",
    "is MANDATORY on every `status` write except",

    # ---- halts that are about a BROKEN CONFIG, not about having no work ----
    # These are the one legitimate halt in the suite and they must stay: a lane that cannot
    # resolve its project has no safe way to continue. They say "halt" beside "empty" only
    # because the sentence around them mentions an empty field.
    "Non-zero exit ABORTS the wake, never a fallback",
    "A non-zero exit is fatal: report the config error and abort the pass",
    "abort this wake and ntfy the error",
    "record it in the state file, say so in the notify, and abort the pass",
    "surface it as a finding and stop this step",

    # ---- the honest halt: blocked on an answer, with a live referent ----
    "AND the Queue has nothing claimable in your lane",
    "set your own Agents row `halted` via",
    "classified by the strict rule (quoted from",
    "lane cannot get*, never *has no work yet*",
    "an empty lane has no such referent",
    "id that an empty backlog cannot supply",

    # ---- the never-halt rule itself, in each loop's words ----
    "An empty queue is NOT a stop condition",
    "There is no stop, no counter, and no",
    "An empty lane is **not** a reason to stop",
    "An empty lane is never a reason to skip it",
    "the only no-continuation exits are the two ABORTS",
    "never stop-to-human",
    "Ntfy and idle-wake; do NOT stop",
    "so `status --state halted` would refuse it",
    "A stopped foreman is not one idle loop",
    "Never stop on an empty pass",
    "An empty pass is NOT a stop",
    "It never stops and keeps no counter",
    "an empty pass never stops the loop and keeps no counter",
    "Never stop for an empty sweep",
    "An empty answered-set is the expected case",

    # ---- the history, kept deliberately so nobody reinstates the rule ----
    "Why there is no terminal stop, so nobody adds one",
    "the counter survives the relaunch",
    "Why even the strongest-looking case for stopping is wrong",
    "`starvation_note_written: <ISO|null>` stays in your state-file frontmatter",

    # ---- reporting an empty queue, which is the OPPOSITE of halting on one ----
    "An empty QUEUE is a finding",
    "an empty sweep is SUCCESS, not starvation",
    "with nothing claimable in the `## Queue`",
    "prolonged starvation is what the",
    '"ran and changed nothing" is the expected case',

    # ---- checklist lines that happen to carry both vocabularies ----
    "auditor state persisted (including",

    # ---- the residual-key drop this epic added ----
    "drop them on your next state-file write",

    # ---- the token-budget rule's API-refusal stop, which is NOT a no-work stop ----
    # "exhaustion" here means a real context-length refusal on an actual call; the same
    # rule tells the seat a budget counter at 0 is never that, and to run its wake.
    "counts as exhaustion: report its exact text and stop",
)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z*`\[(-])")

#: A line that STARTS a new block of prose: a list item, a heading, a table row, a fence.
#: Needed because a checklist is one paragraph with no sentence-ending punctuation in it, so
#: without this the whole of a loop's end-of-firing checklist collapses into a single
#: "sentence" - and one allowlisted bullet would then excuse every other bullet in the list.
_BLOCK_START = re.compile(r"^\s*(?:[-*+]\s|\d+\.\s|#{1,6}\s|\||```|>)")


def _blocks(text: str):
    """Yield (line_number, joined_text) for each block: a paragraph, a bullet, a heading.

    Markdown prose here is hard-wrapped, so a rule routinely spans a line break and a
    line-scoped reader sees neither half of it. Joining a block back together is what makes the
    unit below a rule rather than a fragment.
    """
    nl = chr(10)
    buf: list[str] = []
    buf_line = 1
    for lineno, line in enumerate(text.split(nl), 1):
        starts_block = (not line.strip()) or _BLOCK_START.match(line)
        if starts_block and buf:
            yield buf_line, " ".join(" ".join(buf).split())
            buf = []
        if not line.strip():
            continue
        if not buf:
            buf_line = lineno
        buf.append(line)
    if buf:
        yield buf_line, " ".join(" ".join(buf).split())


def _sentences(text: str):
    """Yield (line_number, sentence) over the document, block by block then sentence by sentence.

    THE UNIT IS A SENTENCE, and the two cheaper units were both tried and both failed here.

    A LINE is too small: the first version of this gate was line-scoped, and its own
    novel-vocabulary test walked straight through it, because the synthetic rule wrapped across
    a line break and matched neither half. A gate a line break defeats has the same shape of
    blind spot as a gate yesterday's tokens defeat.

    A TWO-LINE WINDOW is too big: it staples unrelated sentences together, and this document set
    immediately produced `--state halted` on one line beside "leaves it empty" on the next, from
    two sentences about different subjects. A gate that cries wolf earns an allowlist entry per
    false alarm until the allowlist is the document.
    """
    for line_no, block in _blocks(text):
        if not block:
            continue
        for sentence in _SENTENCE_SPLIT.split(block):
            yield line_no, sentence


def _violations(text: str, source: str) -> list[str]:
    """Sentences that say both "stop" and "no work" and are not on the allowlist.

    A PHRASE IDENTIFIES ITS SENTENCE, and excuses that sentence only. The stricter design was
    tried first - scrub the allowlisted words and re-test the remainder - and it is wrong here
    for a reason worth recording: several kept passages EXIST to describe the deleted rule.
    "The rule used to halt after three consecutive empty wakes" says halt and empty because it
    is quoting the bug, and no short phrase can scrub a paragraph of history down to nothing.

    Sentence granularity is what keeps that from being a loophole. A new rule smuggled in beside
    an allowlisted one is a DIFFERENT sentence, evaluated on its own - which is exactly what
    `test_the_allowlist_does_not_swallow_a_real_rule_on_an_allowed_line` pins.
    """
    out = []
    for line_no, sentence in _sentences(text):
        if not (_HALT_VOCAB.search(sentence) and _NO_WORK_VOCAB.search(sentence)):
            continue
        if any(phrase in sentence for phrase in _ALLOWED):
            continue
        out.append(f"{source}:~{line_no}: {sentence.strip()[:160]}")
    return out


@pytest.mark.parametrize("loop", LOOPS)
def test_no_loop_reintroduces_a_halt_on_no_work_rule(loop):
    """Every loop SKILL.md, checked by behaviour rather than by yesterday's tokens.

    A red here is not automatically a bug: it means a line now says both "stop" and "nothing to
    do", and somebody has to decide which it is. If the line is kept-on-purpose prose, add a
    distinctive phrase from it to `_ALLOWED` above and the reviewer sees that choice in the diff.
    """
    path = SKILLS_ROOT / loop / "SKILL.md"
    assert path.is_file(), f"missing loop skill: {path}"
    found = _violations(path.read_text(encoding="utf-8"), f"skills/{loop}/SKILL.md")
    assert not found, (
        "halt-on-no-work rule reintroduced (or new kept-on-purpose prose needs an allowlist "
        "entry):\n  " + "\n  ".join(found)
    )


#: One phrasing per vocabulary alternative that is NOT how either shipped rule was written.
#: Every entry pairs a halt word with a no-work word, wraps across a line break the way real
#: prose does, and shares no distinctive wording with any allowlist entry.
_NOVEL_RULES = (
    "When the backlog is exhausted for the third consecutive pass, the lane shall\n"
    "terminate and await operator intervention.\n",

    "If the intake produces no input two sweeps running, the worker should shut down\n"
    "until someone restarts it.\n",

    "A lane that finds nothing selectable gives up rather than burning another wake.\n",

    "After a dry cycle the process aborts; do not re-wake it on the usual schedule.\n",

    "Where nothing has been published to this seat, exit for good and let the operator\n"
    "decide when to bring it back.\n",

    "An unfed worker enters a terminal state and stops re-scheduling itself.\n",

    # This one carries NO other halt word on purpose. Mutation found that every earlier entry
    # had a spare - deleting `do not re-wake` from the vocabulary changed nothing, because the
    # sentence that used it also said "aborts". An alternative only counts as tested when some
    # rule depends on it alone.
    "A sweep that finds nothing queued does not re-wake until an operator says so.\n",
)


@pytest.mark.parametrize("rule", _NOVEL_RULES, ids=range(len(_NOVEL_RULES)))
def test_the_gate_catches_a_halt_written_in_a_NOVEL_vocabulary(rule):
    """The gate's own verification, one phrasing per alternative.

    None of these is how either rule this repo actually shipped was written - no
    `empty_wakes`, no `consecutive` counter, and in most of them no "halt" at all. A check
    that only caught the words of a past bug would pass every one of them, which is exactly
    how p190's sweep read lead-dev's halt as absent and concluded the site list was complete.

    PARAMETRIZED FOR A REASON, and it was mutation that found it: with a single synthetic rule,
    deleting an alternative from the halt vocabulary changed nothing and the suite stayed green.
    A disjunction exercised through one branch is a disjunction whose other branches can be
    removed silently.
    """
    assert _violations(rule, "synthetic"), (
        "a halt rule in this vocabulary is invisible to the gate:\n" + rule
    )


def test_the_allowlist_does_not_swallow_a_real_rule_on_an_allowed_line():
    """An allowlisted phrase excuses the line it appears on, and nothing else.

    Worth pinning because the cheap implementation - allowlisting a FILE, or a line NUMBER -
    would let a new rule ride in beside an old one and never be seen.
    """
    smuggled = "An empty lane is **not** a reason to stop.\nAfter three empty wakes, stop for good.\n"
    found = _violations(smuggled, "synthetic")
    assert len(found) == 1
    assert "three empty wakes" in found[0]
