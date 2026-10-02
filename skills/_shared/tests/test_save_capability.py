"""`/wiki-save` — the `wiki/` pillar's writer — must keep the four triggers and must not grow a spawn.

WHY THIS FILE EXISTS. E-0204 filled `wiki/pages/` with a backfilled corpus and left the pillar
with no ongoing writer. E-0205 adds one: `skills/wiki-save/SKILL.md` plus its `commands/wiki-save.md`
wrapper. Three properties of that capability are worth a gate, and all three are properties of
PROSE — these are prose programs, and no static check proves prose behaves. What each assertion
here is limited to is stated at the assertion.

1. **THE WRAPPER FORWARDS.** A command wrapper that documents `--project` in its frontmatter and
   forwards nothing enters the skill with empty arguments. That is a known defect class in this
   repo: `commands/report.md` shipped it once, in a file no test read, and the suite stayed green.
   `pages/` is create-only, so a page written into the wrong project's store cannot be moved back
   out — the forwarding failure is unrecoverable here in a way it is not for a report.

2. **ALL FOUR TRIGGERS SURVIVE, SEPARATELY.** The capability fires at end-of-epic AND at park,
   halt and block. Those last three were an explicit human override of a narrower
   completed-epics-only default (Q-10042, 2026-09-04), and they are the ones that decay: a park is
   where the reasoning is freshest and most perishable. Written as a disjunction ("fires at
   end-of-epic or when work stops"), any ONE surviving alternative keeps the sentence true, so
   three could be deleted with nothing reddening. Hence a case per trigger, each depending on its
   own trigger alone, over a table row per trigger rather than over a sentence.

3. **THE SPAWN IS BUILT, AND IT LIVES IN ONE MODULE.** Q-10042 chose "a model summarises";
   Q-10043 answered who starts it on 2026-09-05 — option A, an inline bounded one-shot — and
   `skills/_shared/agentflow/save_summariser.py` is that implementation. So the guards below no
   longer hold anything OPEN. What they now hold is the shape of the answer: a seat CALLS the
   module, and a process start written into a fence in this skill is a second spawn path that
   inherits none of the module's guardrails. The proximity guard's marker moved with the reason —
   from the question id that meant "this is held" to the CONSTRAINT the answer carries, so a
   reader who meets a spawn mention still meets what bounds it.

WHAT THESE CANNOT DO. They cannot prove a lane performs a save, or that a page is any good. They
prove the instructions are present, are worded per-trigger, and prescribe no process start. Which
SEATS carry the duty is a different question on a different corpus — the loop skills — and it is
gated in `test_skill_call_sites.py` off `SAVING_SEATS`, inclusion and exclusion both.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = SKILLS_ROOT.parent
SKILL_PATH = SKILLS_ROOT / "wiki-save" / "SKILL.md"
WRAPPER_PATH = SUITE_ROOT / "commands" / "wiki-save.md"
SCHEMA_PATH = SKILLS_ROOT / "_shared" / "SCHEMA.md"

#: The four trigger points, exactly as decided. Not a superset and not a subset: a fifth row is a
#: widening nobody signed off and a missing row is the silent narrowing this file exists to catch.
TRIGGERS = ("end-of-epic", "park", "halt", "block")

#: The ONE construct that actually forwards the invocation's arguments into the skill. Same shape
#: the loop wrappers use, so there is one vocabulary and not two — and the skill name is captured
#: so `Skill(report, ...)` inside `commands/wiki-save.md` does not count as save forwarding.
_SKILL_ARGS_FORWARD = re.compile(
    r"Skill\(\s*([a-z0-9-]+)\s*,\s*args\s*=\s*[\"']\$ARGUMENTS[\"']\s*\)"
)

#: Sentences that INSTRUCT invoking a skill, in either shape the suite's wrappers use: the
#: executable `Skill(<name>, ...)` line and the prose imperative that precedes it. Both are read
#: as instructions by a seat, so both have to name the same skill; the first draft of
#: `commands/wiki-save.md` was correct on the executable line and stale in the prose.
_SKILL_INVOCATION = re.compile(
    r"Skill\(\s*([a-z0-9_-]+)\s*[,)]"
    r"|(?:invoke|call|run)\s+the\s+`([a-z0-9_-]+)`\s+skill",
    re.I,
)


#: A markdown table row whose FIRST cell is a single bolded token — the shape the trigger table
#: uses and nothing else in the file does (the failure-modes table's first cells are prose).
_BOLD_ROW_FIRST_CELL = re.compile(r"^\s*\|\s*\*\*([^*|]+)\*\*\s*\|")

#: Tokens that name STARTING A PROCESS. The spawn is built now (Q-10043, option A); what these
#: gate is WHERE it may be written, not whether it exists.
#: `claude -p` is spelled with flexible whitespace so `claude  -p` does not slip the guard.
_SPAWN_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("subprocess", re.compile(r"subprocess", re.I)),
    ("Popen", re.compile(r"\bPopen\b")),
    ("os.system", re.compile(r"os\.system")),
    ("claude -p", re.compile(r"\bclaude\s+-p\b", re.I)),
    ("claude --print", re.compile(r"\bclaude\s+--print\b", re.I)),
    ("run_in_background", re.compile(r"run_in_background")),
)

#: The CONSTRAINT a spawn mention must sit beside, and the successor to the `Q-10043` proximity
#: marker this file used while the question was open.
#:
#: WHY IT CHANGED AND WHY TO THIS. The old marker asserted a state — "this is held" — and that
#: state is gone, so keeping it would have gated on an id whose presence no longer means anything
#: a reader can act on. The property worth keeping was never that a particular id appears; it is
#: that a reader who meets a spawn mention in this skill also meets what BOUNDS it. Three
#: candidates were on the table: the answered question id (asserts nothing, and an answered id
#: reads as settled-therefore-unconstrained), the implementing module name (a pointer to the
#: constraint rather than the constraint — satisfied by "see save_summariser.py"), and the scope
#: line's own term. The scope line wins: `bounded one-shot` is the distinction Q-10043 turns on,
#: it is the half of Q-10015 that was SCOPED rather than repealed, and it is the sentence that
#: stops "seats may spawn" being read as general licence. A reader who meets `claude -p` here and
#: those two words within a paragraph has met the whole rule.
_CONSTRAINT_MARKER = "bounded one-shot"

#: Lines either side of a spawn mention within which the constraint must appear. Proximity, not
#: section scope: a section-wide check false-passes the exact regression this guards, because one
#: statement of the constraint at the top of a long section would excuse a prescription written at
#: the bottom of it. 15 is wide enough for a constraint stated a paragraph above or below.
_CONSTRAINT_PROXIMITY_LINES = 15

#: The module the spawn actually lives in. Named here so the guards below can say where a seat is
#: supposed to go instead of the fence it was about to write.
_SPAWN_MODULE = "save_summariser"


def _skill_text() -> str:
    assert SKILL_PATH.is_file(), f"skills/wiki-save/SKILL.md not found at {SKILL_PATH}"
    return SKILL_PATH.read_text(encoding="utf-8", errors="replace")


def _fenced_blocks(text: str) -> list[tuple[int, str]]:
    """Every fenced code block in *text*, as ``(1-based opening-fence line, body)``.

    Fences are matched on a leading ``` at any indentation; the info string (```python) is not
    part of the body. An unclosed final fence yields the remainder, which is the conservative
    reading — an author who opens a fence and forgets to close it has still written code.
    """
    blocks: list[tuple[int, str]] = []
    open_at: int | None = None
    buf: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            if open_at is None:
                open_at, buf = i, []
            else:
                blocks.append((open_at, "\n".join(buf)))
                open_at = None
            continue
        if open_at is not None:
            buf.append(line)
    if open_at is not None:
        blocks.append((open_at, "\n".join(buf)))
    return blocks


def _trigger_rows() -> dict[str, list[str]]:
    """Trigger name -> its table row's cells. Derived from the file, never listed."""
    rows: dict[str, list[str]] = {}
    for line in _skill_text().splitlines():
        m = _BOLD_ROW_FIRST_CELL.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows[m.group(1).strip()] = cells
    return rows


# --------------------------------------------------------------------------- #
# 1. The capability exists, and its wrapper actually forwards
# --------------------------------------------------------------------------- #
def test_the_skill_and_its_wrapper_both_exist() -> None:
    """Half a capability is worse than none: a skill nobody can invoke, or a command that
    resolves to nothing. Both halves ship together or neither does."""
    assert SKILL_PATH.is_file(), (
        f"skills/wiki-save/SKILL.md missing at {SKILL_PATH} — the wiki pillar has a store, a "
        "projection and a digest, and no writer"
    )
    assert WRAPPER_PATH.is_file(), (
        f"commands/wiki-save.md missing at {WRAPPER_PATH} — `/wiki-save` does not resolve as a slash "
        "command, so the human-command half of the capability is unreachable"
    )


def test_the_wrapper_forwards_arguments_on_a_single_line() -> None:
    """The forwarding must be ONE line carrying `Skill(wiki-save, args="$ARGUMENTS")`, and the
    wrapper must also DOCUMENT `--project`.

    LINE-SCOPED, which is weaker than structural and is worth naming rather than overselling: a
    fenced example carrying the same invocation would satisfy it. What it buys is the whole
    distance between "both tokens appear somewhere in a 25-line file" — which every wrapper's
    frontmatter satisfies while forwarding nothing — and "this exact invocation, for THIS skill,
    on one line". That gap is where the report.md defect lived.
    """
    text = WRAPPER_PATH.read_text(encoding="utf-8", errors="replace")
    assert "--project" in text, (
        "commands/wiki-save.md never mentions --project, so nothing tells the reader the selector "
        "exists; a save then lands in whichever project the session default names"
    )
    forwarding = [
        ln.strip()
        for ln in text.splitlines()
        if any(m.group(1) == "wiki-save" for m in _SKILL_ARGS_FORWARD.finditer(ln))
    ]
    assert forwarding, (
        'commands/wiki-save.md carries no `Skill(wiki-save, args="$ARGUMENTS")` invocation on a single '
        "line — the wrapper may still SAY --project and $ARGUMENTS in its prose while forwarding "
        "nothing. The skill is then entered with empty arguments and the page lands in the wrong "
        "project's create-only store, where it cannot be moved back out"
    )


def test_the_wrapper_never_names_a_skill_other_than_its_own() -> None:
    """Every skill the wrapper tells a reader to INVOKE must be the one the wrapper is named for.

    Derived from the wrapper's own filename rather than checked against a banned word. The
    instance that prompted this was `Invoke the `save` skill` surviving E-0205's
    rename — and bare `save` is a DIFFERENT, real skill (the personal vault's), so a seat
    following that sentence files the fleet's page into a human's private vault. But banning
    `save` would be an enumeration of one past mistake: it would miss `wiki_save`, `wikisave`, a
    future second rename, or a copy-paste from any other wrapper in `commands/`. Asking instead
    "does every invocation name THIS wrapper's skill" catches all of those for the same reason.

    Scoped to invocation-shaped sentences on purpose. The file is REQUIRED to mention the
    personal-vault `/save` in prose to distinguish itself from it, and a check that could not
    tell those apart would force the distinction out of the file to stay green.
    """
    expected = WRAPPER_PATH.stem
    text = WRAPPER_PATH.read_text(encoding="utf-8", errors="replace")
    named = {(m.group(1) or m.group(2)).lower() for m in _SKILL_INVOCATION.finditer(text)}
    assert named, (
        f"{WRAPPER_PATH.name} contains no skill invocation at all, so this guard is reading a "
        "file with nothing to guard: a wrapper that names no skill forwards to nothing"
    )
    wrong = sorted(n for n in named if n != expected)
    assert not wrong, (
        f"{WRAPPER_PATH.name} tells the reader to invoke {wrong}, not {expected!r}. A wrapper "
        "that names the wrong skill is not a typo: `save` is the personal vault's skill, so the "
        "fleet's page is filed into a human's private vault and `pages/` is create-only, which "
        "means it cannot be moved back. Renames must reach the prose, not only the Run: line"
    )


def test_the_wrapper_stays_inside_the_suites_forwarding_gate() -> None:
    """`test_skill_call_sites._forwarding_wrappers()` scopes the suite-wide forwarding gate to
    wrappers that MENTION `$ARGUMENTS` at all — deliberately, so that deleting the forwarding
    line does not drop the file out of its own gate.

    That only holds while the wrapper says `$ARGUMENTS` somewhere OTHER than the forwarding line
    itself. This asserts the property directly rather than importing the sibling module, so a
    rename there cannot silently make this vacuous: at least two mentions, so one can be deleted
    and the file still reds instead of disappearing.
    """
    lines = WRAPPER_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    mentions = [ln for ln in lines if "$ARGUMENTS" in ln]
    non_forwarding = [
        ln for ln in mentions if not any(_SKILL_ARGS_FORWARD.finditer(ln))
    ]
    assert len(non_forwarding) >= 2, (
        f"commands/wiki-save.md mentions $ARGUMENTS on {len(non_forwarding)} line(s) outside its "
        "forwarding invocation. The suite-wide forwarding gate is keyed on mentioning the token, "
        "so a wrapper that names it ONLY on the forwarding line drops out of that gate the "
        "moment the line is deleted — the defect hiding its own detector"
    )


# --------------------------------------------------------------------------- #
# 2. All four triggers, each on its own
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("trigger", TRIGGERS)
def test_each_trigger_point_has_its_own_row(trigger: str) -> None:
    """One case per trigger, each depending on ITS OWN trigger alone.

    Parametrised rather than a single "all four are named" assertion for a reason that is not
    style: the requirement is a disjunction, and a disjunction tested through one alternative
    lets the others be deleted silently. Here, deleting the `park` row reds exactly the `park`
    case and nothing else.

    The row must also be SUBSTANTIVE. A row degraded to `| **park** | | |` names the trigger and
    tells a seat nothing about when it fires, which is the same outcome as deleting it.
    """
    rows = _trigger_rows()
    assert trigger in rows, (
        f"skills/wiki-save/SKILL.md has no trigger row for **{trigger}** — the four trigger points "
        f"are {list(TRIGGERS)}, and the last three (park, halt, block) were an explicit human "
        "override of a completed-epics-only default. A park is where the reasoning is freshest "
        "and most perishable; dropping its row is how that override gets quietly reverted"
    )
    cells = rows[trigger]
    assert len(cells) >= 3, (
        f"the **{trigger}** row has {len(cells)} cell(s); it must carry the boundary it fires at "
        "and why that boundary specifically"
    )
    for pos, cell in enumerate(cells[1:], start=2):
        assert len(cell) >= 20, (
            f"cell {pos} of the **{trigger}** row is {len(cell)} chars ({cell!r}) — a row that "
            "names a trigger without saying when it fires is not a trigger point, it is a label"
        )


def test_the_trigger_table_is_exactly_the_four() -> None:
    """NON-VACUITY plus a no-drift check for the derived parametrisation above.

    `_trigger_rows()` reads the file, so it can go empty (the table reformatted out of the shape
    the regex knows) or go wide (a fifth trigger added without a decision) with every per-trigger
    case still green — the derivation failing open. Both directions are named here.
    """
    found = set(_trigger_rows())
    assert found, (
        "no bolded-first-cell table rows found in skills/wiki-save/SKILL.md — the trigger table has "
        "been reformatted out from under this gate, and every per-trigger case above is now "
        "reading an empty dict"
    )
    assert found == set(TRIGGERS), (
        f"the trigger rows are {sorted(found)} but the decided set is {sorted(TRIGGERS)}. "
        "Extra rows are a widening nobody signed off; missing rows are the silent narrowing "
        "this module exists to catch"
    )


# --------------------------------------------------------------------------- #
# 3. The spawn is built — in ONE module, and never in a fence in this skill
# --------------------------------------------------------------------------- #
def test_no_process_spawn_is_prescribed_as_code() -> None:
    """No fenced code block in the skill may start a process. The ban survives; its reason moved.

    IT USED TO SAY the ban held Q-10043 open. That reason is now false — Q-10043 is answered and
    the spawn is built. The surviving reason is stronger and is why the assertion was not
    weakened to pass: the spawn belongs to `agentflow.save_summariser`, which owns the
    non-recursion marker, the timeout, the in-flight guard and the audit record. A seat must CALL
    that module. A shell line copied out of a fence here is a SECOND spawn path that inherits
    none of those, and a fence is precisely what a tired seat at a phase boundary copies.

    Still scoped to fenced blocks, because that is what "prescribed as code" means in a prose
    program. Naming `claude -p` in a sentence that says what the module does is documentation and
    is required; putting it in a fence is a competing implementation.
    """
    offenders: list[str] = []
    for line_no, body in _fenced_blocks(_skill_text()):
        for name, pattern in _SPAWN_TOKENS:
            if pattern.search(body):
                offenders.append(f"line {line_no}: {name}")
    assert not offenders, (
        f"skills/wiki-save/SKILL.md prescribes a process start in a code block ({offenders}). "
        f"The spawn lives in `{_SPAWN_MODULE}`, which carries the non-recursion marker, the "
        "timeout, the in-flight guard and the audit record. A command line in a fence is a "
        "second spawn path that inherits none of them, and a fence is what a seat copies. Call "
        "the module; do not restate its command line here"
    )


def test_every_spawn_mention_sits_beside_its_constraint() -> None:
    """A spawn token may appear in prose only with its CONSTRAINT stated within a few lines.

    THE MARKER CHANGED WITH THE FACTS. It was `Q-10043`, which asserted "this is held"; the hold
    is gone, so gating on that id would gate on a word that no longer constrains anything. What
    is worth keeping is the property, not the id: a reader who meets a spawn mention here also
    meets what bounds it. `bounded one-shot` is that bound — the distinction Q-10043 turns on,
    and the half of Q-10015 that was scoped rather than repealed. See `_CONSTRAINT_MARKER` for
    the two candidates it was chosen over.

    Proximity, not section scope. A section-wide check false-passes the regression: one statement
    of the constraint at the top of the seam section would excuse a prescription written 60 lines
    below it, and the seam section is exactly where a later author would add one.
    """
    lines = _skill_text().splitlines()
    unheld: list[str] = []
    for i, line in enumerate(lines):
        hit = next((name for name, pat in _SPAWN_TOKENS if pat.search(line)), None)
        if hit is None:
            continue
        lo = max(0, i - _CONSTRAINT_PROXIMITY_LINES)
        hi = min(len(lines), i + _CONSTRAINT_PROXIMITY_LINES + 1)
        window = "\n".join(lines[lo:hi]).lower()
        if _CONSTRAINT_MARKER not in window:
            unheld.append(f"line {i + 1} ({hit}): {line.strip()[:80]}")
    assert not unheld, (
        f"spawn mention(s) in skills/wiki-save/SKILL.md with no {_CONSTRAINT_MARKER!r} within "
        f"{_CONSTRAINT_PROXIMITY_LINES} lines: {unheld}. A spawn described without its bound "
        "reads to the next author as general licence to start sessions — which is exactly what "
        "Q-10015 refused for durable seats and exactly what Q-10043 did NOT grant"
    )


def test_the_spawn_guard_is_not_vacuous() -> None:
    """Both guards above pass hardest on a file that never mentions a spawn at all.

    UNCHANGED IN PURPOSE, and it is still needed: a file silent on the subject has dropped the
    seam, not honoured it, and the two guards would report clean over it. The skill states the
    rule rather than its history, so what is asserted is the rule itself: the constraint that
    bounds the spawn and the module that implements it, because those are what a silent file
    would be missing.
    """
    text = _skill_text()
    named = [name for name, pattern in _SPAWN_TOKENS if pattern.search(text)]
    assert named, (
        "skills/wiki-save/SKILL.md names no spawn mechanism anywhere, so both spawn guards above are "
        "reading a file with nothing to guard. The skill must say what the summariser does and "
        "what bounds it — dropping the seam is not the same as documenting its answer"
    )
    assert _CONSTRAINT_MARKER in text.lower(), (
        f"skills/wiki-save/SKILL.md never says {_CONSTRAINT_MARKER!r}, so the proximity guard "
        "above is reading a file whose constraint is absent everywhere — it would pass only "
        "because there is nothing to find near anything"
    )
    assert _SPAWN_MODULE in text, (
        f"skills/wiki-save/SKILL.md never names `{_SPAWN_MODULE}`. A seat told a spawn exists and "
        "not told where it lives writes its own, which is the second spawn path the fence ban "
        "above exists to prevent"
    )


# --------------------------------------------------------------------------- #
# 4. The write path, and the gate that stops unattended writing
# --------------------------------------------------------------------------- #
def test_the_write_path_goes_through_the_module() -> None:
    """Both calls named, in prose, plus the prohibition on hand-editing a projection.

    `write_page` without `refresh_projection` leaves the page invisible to every seat that reads
    the hot tier; `refresh_projection` replaced by a hand edit is erased by the next refresh, and
    anything built on it is unreproducible. Naming one and not the other is the failure worth
    catching, so both are asserted separately.
    """
    text = _skill_text()
    for call in ("write_page", "refresh_projection"):
        assert call in text, (
            f"skills/wiki-save/SKILL.md never names `{call}` — the write path is the two calls "
            "together, and half of it is a page nobody reads or a projection nobody regenerates"
        )
    assert re.search(r"hand[- ]edit", text, re.I), (
        "skills/wiki-save/SKILL.md never forbids hand-editing a hot file. `hot.md` and `hot-<lane>.md` "
        "are regenerated from `pages/`, so an edit there is silently erased by the next refresh"
    )


#: Every sentence shape that says the capability may not write unattended. The gate was lifted on
#: 2026-09-05 (E-0213) and this is the LIVE half of that: a reader who meets one of these believes
#: no lane may save, and the cheapest way for the gate to come back is for one of them to survive
#: a merge in a file nobody re-read. `re.I` throughout because the original said "ATTENDED" in
#: capitals in one place and "attended" in another.
_GATE_STANDS_PATTERNS = (
    r"acceptance gate",
    r"attended seat",
    r"does not write unattended",
    r"may not write unattended",
)


#: The lift note's own heading. Anchored rather than searched for by keyword: the note is where
#: the discharge is recorded, and a citation is only a citation where the claim is made.
_LIFT_HEADING = "## The precondition on writing"


def _lift_section(text: str) -> str:
    at = text.find(_LIFT_HEADING)
    assert at != -1, (
        f"skills/wiki-save/SKILL.md has no {_LIFT_HEADING!r} section. The note that states the "
        "review the lift rests on is what replaced the gate; a file with neither the gate nor "
        "that statement reads as a capability that was never gated at all"
    )
    end = text.find("\n## ", at + len(_LIFT_HEADING))
    return text[at:] if end == -1 else text[at:end]


def test_the_gate_on_unattended_writing_is_gone_and_the_review_is_stated() -> None:
    """The lift, asserted as hard as the gate it replaces.

    The gate was a precondition on WRITING: no lane could discharge a trigger duty until the human
    had read the first generated pages AS PAGE TEXT. He read all five on 2026-09-05, accepted the
    capability, and instructed the wiring. Two properties matter now and they fail in opposite
    directions:

    1. **No sentence still says the capability may not write unattended.** The gate lived in a
       named section, so deleting the section reads like finishing the job — and the same claim was
       ALSO in the frontmatter description, in `commands/wiki-save.md`, in `docs/ARCHITECTURE.md`
       and in the module's own CLI banner. A skill whose body wires five seats while its
       description says they may not write is a file that contradicts itself, and the description
       is the half a seat reads first.
    2. **The lift STATES the review it rests on.** A gate deleted with no reason recorded is
       indistinguishable from a gate someone found inconvenient, and the next reader has no way to
       tell which. The note says unattended writing is enabled because the first pages were judged
       as page text.
    """
    text = _skill_text()
    for pattern in _GATE_STANDS_PATTERNS:
        # The lift note is allowed to say what the precondition WAS, in the past tense, which is
        # why this anchors on the phrases that assert it still binds rather than on the word
        # "gate". Any of them in present tense is the gate back.
        assert not re.search(pattern, text, re.I), (
            f"skills/wiki-save/SKILL.md still carries {pattern!r} — the precondition on unattended "
            "writing was discharged on 2026-09-05 and five seats now discharge the duty on a wake. "
            "A skill that still says otherwise sends a lane to an attended human who is not there"
        )
    # SCOPED TO THE LIFT'S OWN SECTION, not to the file. A statement that any other paragraph can
    # satisfy is not the lift note's own.
    section = _lift_section(text)
    assert re.search(r"unattended writing is enabled", section, re.I), (
        "the lift note does not say unattended writing is enabled. The gate existed because a "
        "memory store nobody has audited is worse than none; a note that no longer says the "
        "precondition was discharged leaves the next reader unable to tell a discharged "
        f"precondition from a skipped one. Section: {section[:300]!r}"
    )
    assert re.search(r"page text|bodies, not a count", section, re.I), (
        "the lift note never says the review was of the PAGE TEXT. 'He accepted it' discharged by "
        "a count of pages written is the gate passing without the review it existed to force — "
        "and the note is now the only place that distinction is recorded"
    )


def test_the_schema_records_the_pages_unit() -> None:
    """The unit is decided, so it belongs in the schema and not only in the skill that applies it.

    Two halves, and the second is the one that carries meaning: WHAT the unit is, and that it
    grows per outcome rather than per wake. A schema that says "one page per lesson" without the
    per-outcome half leaves a reader free to write one page per wake and call it a lesson.
    """
    text = SCHEMA_PATH.read_text(encoding="utf-8", errors="replace")
    assert re.search(r"one page per durable lesson or decision", text, re.I), (
        "skills/_shared/SCHEMA.md does not record the `pages/` unit. The layout is documented "
        "there and the unit is now decided; leaving it only in the skill means the next thing "
        "that reads the pillar has to infer it"
    )
    assert re.search(r"per\s+outcome[^.]*\bper\s+wake\b", text, re.I | re.S), (
        "SCHEMA.md names the unit without saying `pages/` grows per OUTCOME and not per wake. "
        "That is the half the top-N projection depends on: a per-wake unit ranks the hot tier by "
        "how often a lane woke rather than by what was learned"
    )


# --------------------------------------------------------------------------- #
# 5. The live opt-in the skill documents is the one the code reads (gh#350)
# --------------------------------------------------------------------------- #
def test_the_documented_live_opt_in_is_the_variable_the_code_reads() -> None:
    """The skill's *Definition of done* tells a reader to set an environment variable to run the
    live smoke. That prose is the ONLY place the opt-in is announced, so a rename on either side
    orphans it silently: the code reads a name nobody is told, or the skill names one nothing reads.

    The name is IMPORTED from the module, never retyped here. A literal in this file would be a
    third copy that drifts with neither of the other two and keeps this test green over the exact
    rename it exists to catch. The fenced command is checked separately from the prose because it
    is what a reader copies: a fence that sets a stale name runs the file and skips, quietly.
    """
    from agentflow.save_summariser import LIVE_ENV

    assert isinstance(LIVE_ENV, str) and LIVE_ENV.strip(), (
        f"save_summariser.LIVE_ENV is {LIVE_ENV!r}; an empty name makes every check below vacuous"
    )
    text = _skill_text()
    assert re.search(rf"\b{re.escape(LIVE_ENV)}\b", text), (
        f"skills/wiki-save/SKILL.md never names {LIVE_ENV!r}, the variable "
        "`save_summariser.LIVE_ENV` gates the live smoke on. Either the variable was renamed and "
        "the prose was not, or the prose was edited away; either way the opt-in is now announced "
        "nowhere, and an opt-in nobody is told about is an opt-in nobody uses"
    )
    fenced_opt_in = [
        line_no for line_no, body in _fenced_blocks(text) if f"{LIVE_ENV}=1" in body
    ]
    assert fenced_opt_in, (
        f"no fenced command in skills/wiki-save/SKILL.md sets `{LIVE_ENV}=1`. The fence is what a "
        "reader copies; a copied command that sets any other name runs the file, skips the live "
        "test, and reports green"
    )


def _effective_report_chars(addopts: str | list[str] | None) -> str | None:
    """The `-r` value pytest will actually use from *addopts*, or None when none is given.

    LAST occurrence wins: pytest's `-r`/`--report-chars` is a plain store, not an append, so
    `-ra -rs` reports skips only. Concatenating every value would read that as `as` and pass a
    config that drops the FAILED lines. All four spellings count: `-rX`, `-r X`,
    `--report-chars=X`, `--report-chars X`.
    """
    import shlex

    if not addopts:
        return None
    tokens = shlex.split(addopts) if isinstance(addopts, str) else list(addopts)
    chars: str | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-r", "--report-chars"):
            if i + 1 < len(tokens):
                chars = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--report-chars="):
            chars = tok.split("=", 1)[1]
        elif tok.startswith("-r") and not tok.startswith("--"):
            chars = tok[2:]
        i += 1
    return chars


def _reports_skips_and_failures(chars: str | None) -> bool:
    """True when *chars* shows skip reasons AND keeps the default failed/error summary."""
    if chars is None:
        return False
    covered = set(chars)
    if covered & {"a", "A"}:
        covered |= set("sfE")
    return {"s", "f", "E"} <= covered


@pytest.mark.parametrize(
    ("addopts", "ok"),
    [
        ("-ra", True),
        ("-r a", True),
        ("-rA", True),
        ("--report-chars=fEs", True),
        ("-rs", False),
        ("-ra -rs", False),
        ("", False),
        (None, False),
    ],
)
def test_the_report_chars_parser_reads_the_effective_value(
    addopts: str | None, ok: bool
) -> None:
    """The config check below is only as good as this parser, so the parser gets its own cases:
    the passing spellings, the `-rs` that trades failures for skips, the last-wins override
    that a concatenating parser misreads, and the absent setting."""
    assert _reports_skips_and_failures(_effective_report_chars(addopts)) is ok, (
        f"addopts {addopts!r} parsed to report chars {_effective_report_chars(addopts)!r}; "
        f"expected the config check to {'pass' if ok else 'fail'}"
    )


def test_the_shared_pytest_config_surfaces_skip_reasons() -> None:
    """Every run in this repo is `-q`, and `-q` alone prints a skip as one `s` with no reason — so
    the live smoke's deliberately loud skip reason reached nobody (gh#350). `addopts` in this
    package's pyproject is what makes a bare run show it.

    Checked for the default `fE` too: `-r` REPLACES the default report chars, so `-rs` alone
    would buy the skip reasons by silently dropping the failure summary.
    """
    import tomllib

    pyproject = SKILLS_ROOT / "_shared" / "pyproject.toml"
    cfg = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    addopts = cfg["tool"]["pytest"]["ini_options"].get("addopts", "")
    chars = _effective_report_chars(addopts)
    assert _reports_skips_and_failures(chars), (
        f"skills/_shared/pyproject.toml addopts is {addopts!r}, effective `-r` {chars!r}. It must "
        "report skip reasons (under the repo's universal `-q` a skip's reason is otherwise printed "
        "nowhere) AND keep pytest's default `fE`, which any `-r` replaces — `-rs` alone surfaces "
        "skips by hiding the failure and error summary lines"
    )
