"""A surface a human reads names an epic by its SLUG - gate the rule, not the word.

WHAT WAS WRONG (gh#163). `skills/report/SKILL.md` Step 3 carried the rule and nothing else did.
`skills/dev-manager/SKILL.md` - the fleet's SOLE human surface, the seat that writes chat prose,
routed registry Notes, Ntfy digests and answers to `## Questions` - carried no naming rule at
all. Measured 2026-09-03, that seat wrote `gh#44`, `Q-10033`, `E-0200` and `p220` bare into a
decision prose turn while a memory saying "epics by name not number" was loaded. A memory is
context-only behaviour; the skill file is the reader path, so the rule had to land in the file.
The human has asked three times. The numbers do not even discriminate: four separate specs shared
`priority: 220` that same day, so `p220` named none of them.

WHAT THIS MODULE PROVES. For each gated surface, that the file contains ONE markdown section
whose body states the rule - checked clause by clause, each clause a conjunction of whole-token
and shaped-id patterns that a rewrite can satisfy in its own words but a deletion cannot.

WHY IT IS NOT A GREP FOR "slug" OR "number". Both are ordinary English throughout these files:
`dev-manager/SKILL.md` said "an epic slug" three times on master while carrying no rule
whatsoever, so a word-presence check would have been green against the exact defect this epic was
filed for. Every clause below therefore requires a CO-OCCURRENCE that only a statement of the
rule produces - the word `slug` beside a rejected epic-number form beside a negation; the word
`identifier` beside `commits` and `PRs`; an issue reference beside the word `title`.

WHICH SURFACES ARE GATED, AND THE BOUNDARY DEFENDED RATHER THAN ASSUMED.

Two: `report` and `dev-manager`. They are the only skills whose output is a document a person
reads end to end and composes in free prose, which is the exact condition the rule addresses -
free prose is where a bare `E-0200` can appear, because nothing about the surface forces the name
in. That is the honest line, and it is drawn narrower than "every loop that touches a human" on
purpose:

* The builder loops (`lead-dev`, the juniors, `auditor`, `steward`) reach a person through one
  line of Ntfy and through registry rows. Ntfy is already governed by lead-dev's own standing
  format rule, which mandates the epic slug; the rows are machine-consumed and reach a human
  THROUGH the report, which is gated here. Requiring the paragraph in six more files would gate
  five surfaces that do not render epic identity in prose - the shape of over-reach that got an
  earlier gate in this tree refused.
* `choose-next` presents epics to a person and was the one genuinely close call. It is excluded
  because it complies BY CONSTRUCTION rather than by instruction: its option `label` IS the slug,
  which leaves no free-prose site to get wrong. That exclusion is not taken on trust -
  `test_the_choose_next_exclusion_is_still_true_of_the_file` reds if that line stops saying so.
* `asking-questions` is the protocol document for HOW to ask, and renders no epic identity at
  all. Also guarded, by asserting it still contains no epic-number token.

WHAT THE CORPUS IS, AND WHERE IT IS BLIND - SAY IT PLAINLY.

The two gated names are a JUDGEMENT, made above and written down, not a set derived from anything.
The glob is real and does two jobs. Every name this module mentions is asserted to exist on disk
and to be reachable by it, so a rename or a re-root reds instead of silently gating nothing; and
every skill it finds must be accounted for here (gh#273, below).

There is also a cross-check, and it must not be read as coverage. The exclusion boundary is
reconciled against an INDEPENDENT in-tree source - `test_human_surface_gates.HUMAN_SURFACE_SKILLS`
- and every name in that dict must be gated here or excluded here with a reason. But that dict
answers a DIFFERENT question: which skills may raise an `AskUserQuestion`. The two sets are
provably not the same set. `report` is gated here and is not in that dict at all; `asking-questions`
is in that dict and renders no epic identity. So membership there is neither necessary for nor
implied by being a naming surface, and the reconciliation buys exactly one direction: a skill added
to THAT allowlist must be classified here.

THAT USED TO BE THE WHOLE STORY, AND IT WAS THE BLIND SPOT. Adding a new human-facing seat did
NOT red this file. A review proved it - `skills/briefing/SKILL.md` was dropped in as a second seat
composing prose and carrying four bare ids, the exact 2026-09-03 failure, and this gate returned
green. It redded only once that same skill was ALSO hand-added to the AskUserQuestion allowlist,
which is an unrelated property nobody adding a naming surface has a reason to touch.

WHAT CHANGED, AND EXACTLY HOW FAR IT GOES (gh#273). The glob is now reconciled against this gate
in BOTH directions: every skill on disk must be gated here, excluded here with a reason, or
recorded in `NOT_YET_JUDGED`, and that set is ratcheted so a name cannot outlive its directory.
So a new seat can no longer arrive unnoticed - `skills/briefing/SKILL.md` would red this file
today, on arrival, with no allowlist edit anywhere.

BE PRECISE ABOUT WHAT THAT IS NOT. It does not decide whether the new seat owes the rule, and it
does not apply the rule to it. It forces a person to say which of the three it is, and no more.
The predicate that SHOULD decide membership is still an open question - gh#273's last paragraph
weighs a declared marker in the skill's own frontmatter, a derived check on whether output reaches
a person unmediated, and a curated list with a documented review trigger. This is the third of
those, chosen because it is the only one that changes no file format and invents no predicate.
Widening instead to every skill that writes prose was tried on an earlier epic and correctly
refused: it gates non-loop skills and manufactures false violations.

The classification itself is still a person's call. WHOEVER ADDS A HUMAN-FACING SEAT ADDS IT HERE -
to `HUMAN_NAMING_SURFACES`, and the rule into its file, or to `NOT_A_NAMING_SURFACE` with a reason
and a test guarding that reason. What is no longer true is that forgetting costs nothing. This gate
defends the two surfaces it names, refuses to stay silent about a third one nobody told it about,
and still does not pretend to know which kind it is. A gate that names its own blind spot is
honest; one that claims the coverage this docstring once claimed is the failure gh#163 is about.

WHOLE TOKENS, NEVER SUBSTRINGS. An earlier gate in this tree asserted a token was `in` a file and
a one-character truncation satisfied it. Word clauses here are checked against a tokenized set
(`_tokens`), and id shapes against patterns with explicit non-alphanumeric boundaries, so
`identifie` does not satisfy `identifier` and `E-020` does not satisfy `E-0200`.
`test_a_one_character_truncation_does_not_satisfy_a_clause` pins that on the real files.

AND THE GATE IS PROVED TO BITE. `test_deleting_the_lines_that_carry_a_clause_part_reddens_it`
deletes, from the real file, every line carrying each part of each clause and asserts the clause
then fails - so a green here means the sections were opened and read, not that a predicate went
quiet.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]

#: The phrase that locates the rule's own section in a gated file. Both surfaces state it in
#: their opening sentence; it is specific enough that "the human-facing half of the question"
#: and "this is the human-facing report" (both real lines in these files) do not match it.
ANCHOR = "human-facing surface"

#: Gated surfaces, each WITH the reason it is gated. See the module docstring on the boundary.
HUMAN_NAMING_SURFACES: dict[str, str] = {
    "dev-manager": (
        "the fleet's sole human surface: it composes chat prose, routed registry Notes, Ntfy "
        "digests and answers to Questions, all of them free prose a person reads"
    ),
    "report": (
        "the artefact a person reads end to end; it renders every epic, issue and registry row "
        "the fleet produced, so a bare number here is unreadable at scale"
    ),
}

#: Skills that reach a human but are deliberately NOT required to state the rule, each with the
#: reason and each reason guarded by its own test below. This is the boundary, written down.
NOT_A_NAMING_SURFACE: dict[str, str] = {
    "choose-next": (
        "complies by construction rather than by instruction: the option `label` it puts in "
        "front of the human IS the slug, so it has no free-prose site to get wrong"
    ),
    "asking-questions": (
        "the protocol document for HOW to ask a human, not a surface that names epics; it "
        "renders no epic identity anywhere, which is asserted rather than assumed"
    ),
}

#: Skills on disk this gate has NOT judged - and this is an accounting record, not a verdict.
#:
#: Read the name literally. Every entry above carries a reason because somebody decided it; this
#: set carries no reason because nobody has, and generating seventeen per-skill rationales would be
#: the "unexplained allowlist" failure wearing a better coat. Do NOT read membership as "not a
#: naming surface". Some members plainly talk to a person in free prose - `refine`, `project` and
#: `backlog` converse in chat, and `graph-query` answers a person directly - and are the first
#: candidates for a real decision. Nobody has decided which side any member is on; that is the
#: whole meaning of the name.
#:
#: ITS JOB IS THE RATCHET, NOT THE CONTENT. `test_every_skill_on_disk_is_accounted_for` compares
#: this set against the glob in BOTH directions, so a skill that appears, disappears or is renamed
#: reds this file until a person puts it in one of the three places. That is the whole of what
#: gh#273 asked for and the limit of what it can be given without deciding the open question in
#: its last paragraph: WHICH PROPERTY should decide membership. This does not decide it. It makes
#: the next seat's arrival impossible to miss, which is the cost gh#273 actually named - "a future
#: human-facing seat ships without the naming rule and nothing reports it".
NOT_YET_JUDGED: frozenset[str] = frozenset({
    "auditor",
    "autodoc-init",
    "autodoc-update",
    "backlog",
    "graph-query",
    "jr-dev-1",
    "jr-dev-2",
    "jr-dev-3",
    "lead-dev",
    "orchestrator",
    "polish-mode",
    "project",
    "refine",
    "solution",
    "steward",
    "tdd-loop",
    "wiki-save",
})


# ---------------------------------------------------------------------------
# Tokenizing and section parsing
# ---------------------------------------------------------------------------

#: A word, hyphens and underscores included, so `human-facing` is seen whole. Digits are in the
#: class so `E-0200` tokenizes as one thing rather than as `E` beside `0200`.
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")

_HEADING = re.compile(r"^(#{1,6})\s+\S")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def _tokens(text: str) -> set[str]:
    """Lower-cased whole tokens of *text*. Membership, never substring containment."""
    return {m.group(0).lower() for m in _TOKEN.finditer(text)}


def _has_token(text: str, token: str) -> bool:
    return token.lower() in _tokens(text)


def _headings(lines: list[str]) -> list[tuple[int, int]]:
    """(line index, level) for every heading OUTSIDE a fenced code block.

    Both gated files embed bash and markdown fences whose `# comment` lines would otherwise be
    read as level-1 headings, which would hand the anchor the wrong enclosing section.
    """
    found: list[tuple[int, int]] = []
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
            found.append((i, len(m.group(1))))
    return found


def _sections(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end) line spans of every section: heading line to the next same-or-higher one."""
    heads = _headings(lines)
    spans = []
    for pos, (line_no, level) in enumerate(heads):
        end = len(lines)
        for next_line, next_level in heads[pos + 1:]:
            if next_level <= level:
                end = next_line
                break
        spans.append((line_no, end))
    return spans


def _normalized(text: str) -> str:
    """Whitespace collapsed and lower-cased, so a phrase wrapped across lines still matches."""
    return " ".join(text.split()).lower()


def _skill_lines(skill: str) -> list[str]:
    path = SKILLS_ROOT / skill / "SKILL.md"
    # FAIL, never skip: a renamed skill would silently stop being gated.
    assert path.is_file(), f"{skill}/SKILL.md not found at {path} - renamed, or this list is stale"
    return path.read_text(encoding="utf-8").splitlines()


def naming_section(lines: list[str]) -> list[str]:
    """The innermost section whose body states the rule, as its own lines.

    Returns [] when no section carries the anchor - which is exactly the state
    `dev-manager/SKILL.md` was in on master, and the state this gate must red on.
    """
    matches = [
        (start, end)
        for start, end in _sections(lines)
        if ANCHOR in _normalized("\n".join(lines[start:end]))
    ]
    if not matches:
        return []
    innermost = min(matches, key=lambda span: span[1] - span[0])
    # Every other match must CONTAIN the innermost one. Two disjoint anchored sections would mean
    # the clauses below could be satisfied by assembling halves of the rule from opposite ends of
    # the file, which is not a statement of the rule at all.
    stray = [s for s in matches if not (s[0] <= innermost[0] and s[1] >= innermost[1])]
    assert not stray, (
        f"the phrase {ANCHOR!r} appears in {len(matches)} sections that are not nested "
        f"({matches}); the rule must live in ONE place so a reader finds all of it at once"
    )
    return lines[innermost[0]:innermost[1]]


# ---------------------------------------------------------------------------
# The rule, as clauses
# ---------------------------------------------------------------------------

#: An epic number in either the concrete or the placeholder spelling. Boundaries are explicit
#: rather than `\b`, so `E-020` (a truncation) and `pre-0200` do not match.
_EPIC_NUMBER = re.compile(r"(?<![A-Za-z0-9])(?:E-(?:\d{4}|NNNN)|p(?:\d{3}|NNN))(?![A-Za-z0-9])")
#: A tracker reference: `#163`, `#N`, `gh#44`.
_ISSUE_REF = re.compile(r"#(?:N|\d+)(?![A-Za-z0-9])")
_Q_ID = re.compile(r"(?<![A-Za-z0-9])Q-(?:\d{4,}|NNNN)(?![A-Za-z0-9])")
_B_ID = re.compile(r"(?<![A-Za-z0-9])B-(?:\d{4,}|NNNN)(?![A-Za-z0-9])")


def _pattern_part(rx: re.Pattern[str]):
    return lambda text: rx.search(text) is not None


def _token_part(*alternatives: str):
    """Satisfied when ANY of *alternatives* is present as a whole token."""
    return lambda text: any(_has_token(text, alt) for alt in alternatives)


#: Each clause is a CONJUNCTION of named parts. Every part is separately load-bearing - the
#: mutation test below deletes the lines carrying each one and requires the clause to red.
CLAUSES: dict[str, dict[str, object]] = {
    "epics-are-named-by-slug": {
        "why": "the rule's kernel: an epic is named by its slug, and a bare epic number is not a name",
        "parts": {
            "the word slug": _token_part("slug", "slugs"),
            "a rejected epic number": _pattern_part(_EPIC_NUMBER),
            "a negation": _token_part("never", "not"),
        },
    },
    "numbers-belong-where-they-are-the-identifier": {
        "why": "the other half: the exemption is stated, so the rule is not read as a ban on numbers",
        "parts": {
            "the word identifier": _token_part("identifier"),
            "commits": _token_part("commit", "commits"),
            "PRs": _token_part("pr", "prs"),
        },
    },
    "tracker-issues-carry-their-title": {
        "why": "issues have no slug, so the readable half has to be the title beside the number",
        "parts": {
            "an issue reference": _pattern_part(_ISSUE_REF),
            "the word title": _token_part("title"),
            "the word issue": _token_part("issue", "issues"),
        },
    },
    "registry-ids-carry-a-subject": {
        "why": "a bare Q-/B- id is unreadable on any surface; the id needs its subject beside it",
        "parts": {
            "a question id": _pattern_part(_Q_ID),
            "a blocker id": _pattern_part(_B_ID),
            "the word subject": _token_part("subject"),
        },
    },
}

#: Clauses required of the SEAT only. Both surfaces print registry ids, so both owe the subject
#: clause above: the report's CHANGED SINCE and LANES once printed `Q-` ids bare on the theory
#: that the collector's row types made them safe, and they did not. What stays seat-only is the
#: sha-level breakdown of commits and PRs - the seat writes those into free prose itself, while
#: the report prints them from rows that carry no name to pair with them.
SEAT_CLAUSES: dict[str, dict[str, object]] = {
    "prs-and-commits-are-fine-as-they-are": {
        "why": "the surface where the number IS the identifier, named so the rule is checkable",
        "parts": {
            "the word sha": _token_part("sha"),
            "commits": _token_part("commit", "commits"),
            "PRs": _token_part("pr", "prs"),
        },
    },
}

SEAT = "dev-manager"


def clauses_for(skill: str) -> dict[str, dict[str, object]]:
    return {**CLAUSES, **(SEAT_CLAUSES if skill == SEAT else {})}


def unmet_parts(section: str, clause: dict[str, object]) -> list[str]:
    parts: dict = clause["parts"]  # type: ignore[assignment]
    return [name for name, ok in parts.items() if not ok(section)]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill", sorted(HUMAN_NAMING_SURFACES))
def test_the_surface_carries_a_naming_section(skill: str) -> None:
    """The section must exist at all. This is the assertion that reds on master's dev-manager."""
    assert naming_section(_skill_lines(skill)), (
        f"{skill}/SKILL.md has no section stating the naming rule (no body contains "
        f"{ANCHOR!r}). A surface a human reads must say, in its own file, that epics are named "
        f"by slug - see skills/report/SKILL.md Step 3 for the text to mirror. Gated because: "
        f"{HUMAN_NAMING_SURFACES[skill]}"
    )


@pytest.mark.parametrize(
    ("skill", "clause"),
    [(s, c) for s in sorted(HUMAN_NAMING_SURFACES) for c in clauses_for(s)],
)
def test_the_naming_section_states_every_clause_the_surface_owes(skill: str, clause: str) -> None:
    section = "\n".join(naming_section(_skill_lines(skill)))
    assert section, f"{skill}/SKILL.md has no naming section at all"
    spec = clauses_for(skill)[clause]
    missing = unmet_parts(section, spec)
    assert not missing, (
        f"{skill}/SKILL.md's naming section does not state `{clause}` - missing {missing}. "
        f"Why the clause exists: {spec['why']}"
    )


def test_the_seat_owes_more_than_the_report_and_the_split_is_real() -> None:
    """The two-tier split must stay a deliberate design, not an accident of drafting.

    If SEAT_CLAUSES ever empties, this file silently becomes a kernel-only check and the seat's
    per-surface breakdown - the actual deliverable of gh#163 - stops being enforced.
    """
    assert SEAT_CLAUSES, "SEAT_CLAUSES is empty; the seat's per-surface breakdown is unguarded"
    assert set(clauses_for(SEAT)) > set(CLAUSES)
    assert set(clauses_for("report")) == set(CLAUSES)


# ---------------------------------------------------------------------------
# The gate is proved to bite
# ---------------------------------------------------------------------------


def _drop_lines(lines: list[str], matches) -> list[str]:
    return [line for line in lines if not matches(line)]


@pytest.mark.parametrize(
    ("skill", "clause", "part"),
    [
        (s, c, p)
        for s in sorted(HUMAN_NAMING_SURFACES)
        for c, spec in clauses_for(s).items()
        for p in spec["parts"]  # type: ignore[index]
    ],
)
def test_deleting_the_lines_that_carry_a_clause_part_reddens_it(
    skill: str, clause: str, part: str
) -> None:
    """Every part is load-bearing ON THE REAL FILE, so a green above is a read, not a shrug.

    A clause whose part is satisfied by nothing in the file - a predicate that went quiet after a
    rewrite, or one written against text that never existed - passes the check above and fails
    here, because deleting "every line that carries it" deletes nothing and the clause survives.
    """
    spec = clauses_for(skill)[clause]
    ok = spec["parts"][part]  # type: ignore[index]
    section = naming_section(_skill_lines(skill))
    assert section, f"{skill}/SKILL.md has no naming section at all"

    mutated = "\n".join(_drop_lines(section, ok))
    assert part in unmet_parts(mutated, spec), (
        f"deleting every line of {skill}'s naming section that carries `{part}` left "
        f"`{clause}` satisfied. Either the part matches no real line (the predicate has gone "
        f"quiet and this clause is checking nothing) or it is satisfied somewhere the deletion "
        f"cannot reach."
    )


def test_removing_the_whole_section_reddens_the_seat() -> None:
    """The literal regression: dev-manager as it stood on master, with no rule anywhere."""
    lines = _skill_lines(SEAT)
    section = naming_section(lines)
    assert section
    start = lines.index(section[0])
    stripped = lines[:start] + lines[start + len(section):]
    assert not naming_section(stripped), (
        "with its naming section deleted, dev-manager/SKILL.md still looks gated - the anchor "
        "is matching something else in the file and the gate would pass on master's text"
    )


@pytest.mark.parametrize("skill", sorted(HUMAN_NAMING_SURFACES))
def test_a_one_character_truncation_does_not_satisfy_a_clause(skill: str) -> None:
    """Whole tokens, never substrings - the failure an earlier gate in this tree shipped.

    Truncating the last character of `identifier` everywhere in the real section must break the
    clause that requires it. A substring check ("is `identifie` in the text?") would not.
    """
    section = "\n".join(naming_section(_skill_lines(skill)))
    assert _has_token(section, "identifier"), "precondition: the real section says `identifier`"
    truncated = section.replace("identifier", "identifie")
    assert not _has_token(truncated, "identifier"), (
        "a truncated `identifie` still satisfied the whole-token check for `identifier`"
    )
    spec = CLAUSES["numbers-belong-where-they-are-the-identifier"]
    assert "the word identifier" in unmet_parts(truncated, spec)


def test_the_section_parser_can_say_no_and_ignores_fenced_hashes() -> None:
    """Without this, a parser bug returning the whole file as every section would make every
    assertion above pass on any input - a gate measuring nothing."""
    doc = [
        "# Title",
        "## Elsewhere",
        "unrelated prose",
        "## The rule",
        "```bash",
        "# not a heading",
        "```",
        "Every human-facing surface names epics by slug.",
        "## After",
        "more prose",
    ]
    assert [lvl for _i, lvl in _headings(doc)] == [1, 2, 2, 2], _headings(doc)
    section = naming_section(doc)
    assert section[0] == "## The rule"
    assert "## After" not in section
    assert "unrelated prose" not in section
    assert naming_section(["# Title", "nothing here"]) == []


# ---------------------------------------------------------------------------
# The corpus is derived, and the boundary is guarded
# ---------------------------------------------------------------------------


def _all_skills() -> list[str]:
    """Every skill in the tree, GLOBBED from disk - never a roster restated here."""
    return sorted(
        p.parent.name
        for p in only_tracked(SKILLS_ROOT, SKILLS_ROOT.glob("*/SKILL.md"))
    )


def test_the_glob_sees_the_whole_tree_and_reaches_every_gated_name() -> None:
    """A discovery glob that has gone quiet reads exactly like a tree with nothing to gate."""
    discovered = _all_skills()
    assert len(discovered) >= 15, (
        f"the skills glob found only {len(discovered)} skills ({discovered}); it is likelier "
        "broken or re-rooted than the tree is empty"
    )
    named = set(HUMAN_NAMING_SURFACES) | set(NOT_A_NAMING_SURFACE)
    missing = sorted(named - set(discovered))
    assert not missing, f"{missing} are named in this gate but not discovered on disk"


def _human_surface_allowlist() -> dict[str, str]:
    """`HUMAN_SURFACE_SKILLS` from the sibling gate - an INDEPENDENT in-tree source of truth.

    Loaded by path rather than by bare import so this does not depend on pytest's sys.path
    insertion mode.
    """
    path = Path(__file__).with_name("test_human_surface_gates.py")
    assert path.is_file(), (
        f"{path.name} is gone; this gate's boundary was reconciled against its allowlist and "
        "now reconciles against nothing - re-point it or replace the check"
    )
    spec = importlib.util.spec_from_file_location("_sibling_human_surface_gates", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.HUMAN_SURFACE_SKILLS


def test_every_declared_human_surface_is_classified_here() -> None:
    """ONE DIRECTION OF RECONCILIATION - and not the one it is easy to assume.

    The one place in this tree that already decides "may this skill raise an `AskUserQuestion`" is
    read back, and every name it holds must be either gated here or excluded here WITH a reason.
    Add a skill to THAT allowlist and this reds until someone classifies it.

    WHAT IT DOES NOT DO: catch a new naming surface. That dict is not the set of naming surfaces -
    `report` is gated by this module and is absent from it - so on its own this test stays green
    when a new human-facing seat is added to the tree. The test below is what reds on that arrival
    (gh#273); this one only reconciles the allowlist.
    """
    classified = set(HUMAN_NAMING_SURFACES) | set(NOT_A_NAMING_SURFACE)
    unclassified = sorted(set(_human_surface_allowlist()) - classified)
    assert not unclassified, (
        f"{unclassified} are allowlisted human surfaces in test_human_surface_gates.py but this "
        "gate says nothing about them. Either they compose free prose naming epics (add them to "
        "HUMAN_NAMING_SURFACES and give them the rule) or they do not (add them to "
        "NOT_A_NAMING_SURFACE with the reason, and guard it)."
    )


def _unaccounted(discovered: set[str]) -> list[str]:
    """Skills on disk that are gated nowhere, excluded nowhere and not recorded as unjudged."""
    classified = set(HUMAN_NAMING_SURFACES) | set(NOT_A_NAMING_SURFACE)
    return sorted(discovered - classified - NOT_YET_JUDGED)


def _stale_unjudged(discovered: set[str]) -> list[str]:
    """Names recorded in NOT_YET_JUDGED whose directory is gone."""
    return sorted(NOT_YET_JUDGED - discovered)


def test_every_skill_on_disk_is_accounted_for() -> None:
    """THE OTHER DIRECTION - disk against the gate, which is the one gh#273 is about.

    Its neighbour above subtracts the AskUserQuestion allowlist from the classified set. A skill
    sitting on disk and in neither dict is in neither operand of that subtraction, so it cannot
    make anything fail: it defaults out in silence. That is the behavioural half of gh#273, and
    the module docstring has said so in its own words since 8106263.

    This closes it by accounting rather than by judging. Three places a skill may be - gated,
    excluded with a reason, or recorded as unjudged - and the glob must land in one of them.
    Deliberately NOT a wider predicate: gating every skill that writes prose was tried on an
    earlier epic and correctly refused because it manufactures violations against non-loop skills.
    """
    unaccounted = _unaccounted(set(_all_skills()))
    assert not unaccounted, (
        f"{unaccounted} exist on disk and this gate says nothing about them. If one composes free "
        "prose naming epics, add it to HUMAN_NAMING_SURFACES and put the rule in its file; if it "
        "does not, add it to NOT_A_NAMING_SURFACE with the reason and guard that reason; if "
        "nobody is deciding today, add it to NOT_YET_JUDGED so the next reader can see it was "
        "seen. What must not happen is the fourth option, which is what this test removes: it "
        "ships gated by nothing and nothing reports it (gh#273)."
    )


def test_the_unjudged_set_cannot_outlive_the_skills_it_names() -> None:
    """The ratchet half, and the reason the set above is not just a mute exemption list.

    Same shape as KNOWN_UNBRANCHED in the sibling gate: a recorded gap that is fixed must have
    its entry DELETED, never left behind. A stale name here would silently pre-excuse whatever
    skill later takes that directory name - the exemption outliving the fact that justified it,
    which is the failure the comment on HUMAN_SURFACE_SKILLS warns about one file over.
    """
    stale = _stale_unjudged(set(_all_skills()))
    assert not stale, (
        f"{stale} are recorded in NOT_YET_JUDGED but no longer exist on disk - delete them. Left "
        "in place, the name pre-excuses any future skill that takes that directory."
    )


def test_both_reconciliation_directions_can_fire() -> None:
    """The two tests above pass on today's tree, which a subtraction that returns nothing also does.

    Measured on review: replacing either subtraction with an empty list left the whole file green,
    because a gate over real data cannot tell "nothing to report" from "cannot report". So each
    direction is driven here with a synthetic tree it must object to, plus the real tree as the
    control it must accept.
    """
    real = set(_all_skills())
    assert _unaccounted(real) == [] and _stale_unjudged(real) == []

    arrived = real | {"a-new-human-seat"}
    assert _unaccounted(arrived) == ["a-new-human-seat"]

    gone = next(iter(sorted(NOT_YET_JUDGED)))
    assert _stale_unjudged(real - {gone}) == [gone]


def test_no_skill_is_both_judged_and_unjudged() -> None:
    """The three destinations must be disjoint, or "accounted for" stops meaning anything.

    Without this, classifying a skill while leaving it in NOT_YET_JUDGED would read as done in
    one place and undecided in the other, and the subtraction above would pass either way.
    """
    classified = set(HUMAN_NAMING_SURFACES) | set(NOT_A_NAMING_SURFACE)
    both = sorted(classified & NOT_YET_JUDGED)
    assert not both, (
        f"{both} are classified by this gate AND recorded as unjudged. Pick one: a decision was "
        "made, so delete the NOT_YET_JUDGED entry."
    )


@pytest.mark.parametrize(
    ("skill", "reason"),
    sorted({**HUMAN_NAMING_SURFACES, **NOT_A_NAMING_SURFACE}.items()),
)
def test_every_entry_states_a_reason_thick_enough_to_re_judge(skill: str, reason: str) -> None:
    """An unexplained name on a list is how an exemption outlives the fact that justified it."""
    assert len(reason.split()) >= 12, f"{skill}'s recorded reason is too thin to re-judge: {reason!r}"


def test_the_choose_next_exclusion_is_still_true_of_the_file() -> None:
    """The one substantive exclusion, checked against the tree rather than trusted.

    choose-next is excluded because the epic name it puts in front of a person IS the slug, by
    construction. The moment that stops being what the file says, the exclusion is a hole and
    this reds - which is what stops NOT_A_NAMING_SURFACE being a place to park a real surface.
    """
    lines = _skill_lines("choose-next")
    carriers = [
        line for line in lines
        if _has_token(line, "label") and _has_token(line, "slug")
    ]
    assert carriers, (
        "choose-next/SKILL.md no longer says that the option label it shows the human is the "
        "slug. Its compliance was BY CONSTRUCTION; without that line it composes a name in "
        "prose like any other surface and must be gated - move it into HUMAN_NAMING_SURFACES "
        "and give it the rule."
    )


def test_the_asking_questions_exclusion_is_still_true_of_the_file() -> None:
    """Excluded for rendering no epic identity at all. Assert that, do not assume it."""
    text = "\n".join(_skill_lines("asking-questions"))
    found = sorted({m.group(0) for m in _EPIC_NUMBER.finditer(text)})
    assert not found, (
        f"asking-questions/SKILL.md now renders epic numbers ({found}); it was excluded from "
        "this gate for naming no epics, so either the mention is wrong or the skill has become "
        "a naming surface and owes the rule"
    )


@pytest.mark.parametrize("skill", sorted(HUMAN_NAMING_SURFACES))
def test_no_gated_surface_is_also_excluded(skill: str) -> None:
    """The one way to fake this green: park a gated surface in the exclusion list."""
    assert skill not in NOT_A_NAMING_SURFACE, (
        f"{skill} is both gated and excluded; the exclusion would win and the rule would go "
        "unenforced on the surface this epic was filed about"
    )
