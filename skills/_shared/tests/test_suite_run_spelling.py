"""No builder lane may document a suite run that `cd`s into the worktree first (gh#240).

THE DEFECT. The Bash tool's working directory PERSISTS between calls. A suite step spelled
`cd <worktree>/skills/_shared && python -m pytest ...` therefore leaves the lane's shell inside
the tree for the rest of the firing, and on Windows a directory that is a live process's CWD
cannot be removed. Cleanup's `git worktree remove` then fails or half-succeeds: git DEREGISTERS
the tree either way, so `worktree list` reads clean and `worktree prune` reports nothing, while
the directory survives with zero files in it. A lane trusting `worktree list` concludes it
cleaned up. The residue is SILENT BY CONSTRUCTION -- which is why prose alone cannot hold the
rule and this module exists. Reproduced twice in one session (worktrees `p232-actor-capability`
and `gh128-ack-order`, both wedged on exactly `skills/_shared`), and hit a third time on E-0755:
`worktree remove` gave "Permission denied", `rm -rf` gave "Device or resource busy", and the
empty directory outlived the pruned metadata.

WHY THE FIX IS AVAILABLE AT ALL, since "run it by path instead" is only a fix if it measures the
same thing. Measured at `85a75a1` (win32, Python 3.11.15 / pytest 9.1.1): `pytest
<worktree>/skills/_shared` from outside the tree and `cd <worktree>/skills/_shared && pytest`
collect THE SAME node ids, byte-identical under `diff`, with the same rootdir
(`.../skills/_shared`) and the same configfile, because `skills/_shared/pyproject.toml` is the
only ini that can apply and `testpaths = ["tests"]` names exactly what the explicit path names.
Run parity too, on the cwd-sensitive subset (`test_registry.py`, `test_save_live_spawn.py`,
`test_scenario_c_checker.py`, `test_evidence_table.py`): 736 passed, 1 skipped both ways. So the
`cd` buys nothing and costs a wedge.

The property is the EQUALITY, never a number. An earlier draft of this paragraph pinned the
count of the day, which goes stale the moment anyone adds a test -- including this module, whose
own cases moved it within the hour -- and a reader landing here beside a README that says
something else has to work out which is lying. `README.md`'s Evidence row is the place a count
belongs, because a gate re-derives it there on every run.

WHAT THIS GATE CHECKS, and where it stops.

* It scans the FOUR BUILDER LANES' `SKILL.md` -- the lanes that create a worktree and run product
  suites in it. `auditor` and `steward` are deliberately absent (lead-dev's seam note excludes
  them; neither runs a product suite in an epic worktree), and so are the `skills/orchestrator/
  prompts/*.md` fallbacks, which still sanction `cd {worktree} && <command>` for runners with no
  path flag. That sanction is real and is tracked separately; widening this module to it without
  the matching prose edit would red text nobody has fixed yet.
* Its corpus comes from the GIT INDEX via `tracked_files`, never from a tree walk. A gate whose
  corpus is whatever is lying in one checkout is a verdict about that checkout, not about the
  commit (`test_corpus_from_index.py`).
* The detector is a TEXT check over command spans. It catches a directory change chained to a
  runner inside ONE span: the verb `cd` or `pushd`, a target, then `&&`, `;` or `||`, then the
  runner. `&&` alone was the first cut of this gate, and an adversarial pass walked straight past
  it twice -- `cd <worktree>/skills/_shared; python -m pytest tests -q` and the `pushd` spelling
  of the same line were each injected into a builder skill and left all 34 tests green, while
  wedging the tree exactly as the `&&` form does. Both are caught now.

  WHAT IT STILL CANNOT SEE, enumerated rather than implied. A limitations note that omits the
  evasions someone has already demonstrated is worse than no note, because it is read as a
  guarantee:
  - a `cd` on ONE line with the runner on the NEXT (a fence written as two steps, a numbered
    list). The scan is per-span on purpose: joining spans would make every prose paragraph a
    candidate command and the gate would be allowlisted into uselessness.
  - a non-POSIX spelling of the same move -- PowerShell's `Set-Location` or `chdir`, cmd's
    `cd /d`, or a shell alias, function or repo script that changes directory on the lane's
    behalf. Nothing in this repo documents a suite step that way today.
  - `pushd <target> && <runner> && popd` is FLAGGED even though the `popd` would restore the
    directory. That is deliberate, not an oversight: `&&` short-circuits, so the `popd` is
    skipped the moment the runner exits non-zero -- which is precisely the firing where the tree
    then has to be removed. Use a subshell if you want the restore to be unconditional.
  - whether a lane READING the file obeys any of it. This gate is a claim about the text.

  What it buys is that none of the spellings it does see can come back into a builder skill
  without a red test.
* A SUBSHELL is allowed on purpose -- `( cd <worktree> && npm ci )` restores the parent shell's
  directory on exit, so it does not wedge anything. Measured while producing the parity numbers
  above: every `cd`-based run in that measurement was subshell-wrapped and `pwd` was unchanged
  after each one. Where a runner genuinely has no path flag, that is the cheap fix, and a gate
  that forbade it would push authors back to the bare form.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow.tracked_paths import tracked_files

SKILLS_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = SKILLS_ROOT.parent

#: The lanes that create an epic worktree AND run product suites inside it. Hand-named rather
#: than derived, because the two obvious derivations are both wrong here: `AGENTS` sweeps in
#: `auditor`/`steward`/`dev-manager`, which this epic's seam excludes, and "every SKILL.md that
#: says `worktree add`" also matches `orchestrator`, `project` and `solution`, whose prose this
#: epic did not edit. A hand list is only as good as its floor, so `test_every_builder_lane_is_
#: a_real_tracked_file` asserts each entry exists in the index: a renamed lane reds here instead
#: of quietly dropping out of coverage.
BUILDER_LANES = ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3")

#: Test runners a suite step would name. `pytest` is the one that wedged; the rest are here so
#: the next author cannot evade the gate by reaching for a different runner.
_RUNNER = re.compile(
    r"\b(?:pytest|vitest|jest|npm\s+(?:run\s+)?test|npx\s+\S*(?:vitest|jest)|go\s+test"
    r"|cargo\s+test)\b"
)

#: `<verb> <target> <sep> <rest>` inside a single command span. `open` captures a preceding `(`,
#: which makes it a subshell and therefore harmless; the target excludes the shell metacharacters
#: so a `cd` with no argument (the bare word in prose like "never a bare `cd`") cannot match.
#:
#: Neither alternation is decoration. `pushd` moves the shell exactly as `cd` does -- the matching
#: `popd` is a promise, not a guarantee, and `&&` drops it on the first non-zero exit -- and a `;`
#: or `||` separator runs the runner from the new directory just as `&&` does. Both spellings were
#: injected into a builder skill against the `&&`/`cd`-only version of this pattern and passed all
#: 34 tests, which is why they are named here and controlled for below.
_CD_CHAIN = re.compile(
    r"(?P<open>\(\s*)?\b(?P<verb>cd|pushd)\s+(?P<target>[^\s&|;`)]+)"
    r"\s*(?:&&|\|\||;)\s*(?P<rest>[^`\n]*)"
)

#: Command spans a reader would run: inline backtick spans, plus the body lines of fenced blocks.
#: Both, because a fence is the obvious place a future suite step would be written and a
#: backtick-only scan would teach the next author which spelling evades this gate.
_BACKTICK_SPAN = re.compile(r"`([^`\n]*)`")
_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})")


def command_spans(text: str) -> list[str]:
    """Every inline backtick span and every line inside a fenced block."""
    spans = [m.group(1) for m in _BACKTICK_SPAN.finditer(text)]
    fenced, in_fence = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            fenced.append(line.strip())
    return spans + fenced


def cd_chained_suite_runs(text: str) -> list[str]:
    """Command spans that `cd` somewhere and then run a test suite, outside a subshell.

    This is the predicate, extracted so the gate below and its positive control run the SAME
    code. A control that restated the logic would be a second definition of the gate, free to
    stay green while the real one rotted -- the failure `test_skill_call_sites.py` documents at
    length and the reason gh#314 was possible.
    """
    offenders = []
    for span in command_spans(text):
        for m in _CD_CHAIN.finditer(span):
            if m.group("open"):
                continue  # `( cd X && ... )` restores the caller's cwd on exit
            if _RUNNER.search(m.group("rest")):
                offenders.append(span.strip())
    return offenders


def _skill_path(lane: str) -> Path:
    """The lane's SKILL.md, resolved through the git index."""
    rel = f"skills/{lane}/SKILL.md"
    found = tracked_files(SUITE_ROOT, lambda p, rel=rel: p == rel)
    # FAIL, never skip: a renamed or untracked lane must break the gate loudly rather than
    # quietly stop being covered.
    assert found, f"{rel} is not in the git index - renamed, deleted, or never added"
    return found[0]


def _skill_text(lane: str) -> str:
    return _skill_path(lane).read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("lane", BUILDER_LANES)
def test_no_builder_skill_documents_a_cd_chained_suite_run(lane: str) -> None:
    offenders = cd_chained_suite_runs(_skill_text(lane))
    assert not offenders, (
        f"{lane}/SKILL.md documents a suite run that `cd`s first: {offenders}. The Bash tool's "
        "cwd persists between calls, so the lane's shell stays inside the worktree and Windows "
        "refuses to remove it; `worktree remove` deregisters the tree anyway, leaving an empty "
        "directory that `worktree list` and `worktree prune` both report as gone (gh#240). Run "
        "the suite by explicit path instead - `<canonical_repo.python> -m pytest "
        "<worktree>/skills/_shared -q`, which collects exactly the same node ids - or, where the "
        "runner has no path flag, wrap the `cd` in a subshell: `( cd <worktree> && <command> )`."
    )


@pytest.mark.parametrize("lane", BUILDER_LANES)
def test_builder_skill_names_the_explicit_path_suite_spelling(lane: str) -> None:
    """Forbidding the bad spelling is half a rule; the lane also has to be told the good one."""
    text = _skill_text(lane)
    assert "-m pytest <worktree>" in text, (
        f"{lane}/SKILL.md never shows a pytest invocation carrying `<worktree>` - a lane told "
        "only what not to do reaches for the `cd` again"
    )


def _section(lane: str, text: str, heading: str) -> str:
    """The body of the `## ` section whose heading line STARTS WITH `heading`, heading included.

    Shared by the point-of-use gate and the checklist gate below so both anchor the same way; a
    second hand-rolled slicer is a second definition free to drift from the first.
    """
    start = text.find(heading)
    assert start != -1, (
        f'{lane}/SKILL.md has no section starting "{heading}" - this gate anchors on that '
        "heading, so a rename empties the slice instead of failing, and the rule it holds stops "
        "being checked while the case still reports green"
    )
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


#: WHERE the prohibition has to sit, per lane - not merely that the file contains it somewhere.
#: The instruction that matters is the one a lane reads AT THE MOMENT it decides how to spell a
#: suite run, and lead-dev has two such moments: §4, where it spells its OWN run, and §7.5, where
#: it writes the prompt that tells a REVIEWER/VERIFIER SUBAGENT how to spell one. The juniors have
#: one, in their worktree delta.
#:
#: This is keyed by section because the whole-file version of this check was not a check. Measured
#: by mutation: deleting BOTH the §4 prohibition and the §7.5 prompt clause outright left all 34
#: tests green, because the §9 end-of-firing checklist line carries `gh#240` and a `cd` mention on
#: its own and satisfied a whole-file scan by itself. A checklist box at the end of a 1500-line
#: file is not the place a lane is standing when it writes the command.
_POINTS_OF_USE: dict[str, tuple[tuple[str, str], ...]] = {
    "lead-dev": (
        ("## 4. Execute via /project", "where the lane spells its own suite run"),
        (
            "## 7.5 Background-task hygiene",
            "where the lane writes the reviewer/verifier subagent's suite-run clause",
        ),
    ),
    "jr-dev-1": (
        ("## Division of labor (deltas from primary)", "where the lane spells its own suite run"),
    ),
    "jr-dev-2": (
        ("## Division of labor (deltas from primary)", "where the lane spells its own suite run"),
    ),
    "jr-dev-3": (
        ("## Division of labor (deltas from primary)", "where the lane spells its own suite run"),
    ),
}


def _point_of_use_cases() -> list[tuple[str, str, str]]:
    return [(lane, head, why) for lane, points in _POINTS_OF_USE.items() for head, why in points]


@pytest.mark.parametrize("lane, heading, why", _point_of_use_cases())
def test_builder_skill_states_why_at_the_point_of_use(lane: str, heading: str, why: str) -> None:
    """The prohibition and its reason sit in ONE PARAGRAPH of the section the lane reads.

    Two independent properties. Co-location in a paragraph, because `cd` appears in ordinary
    prose and "persists" could sit in an unrelated paragraph three sections away, so a whole-file
    pair of `in` checks stays green through a rewrite that deleted the rule and kept the words.
    And SECTION scope, because a single surviving copy anywhere in the file satisfied the
    co-location check for the whole file - so the §9 checklist alone held every lane green while
    the §4 and §7.5 prohibitions were deleted (gh#240).

    The unit is the paragraph, not the line: the section is split on blank lines and each
    paragraph's whitespace flattened, so a reflowed line break cannot defeat it. A per-line pin
    accepting "working directory" or "cwd" anywhere on the forbidding line is satisfied in §7.5 by
    the unrelated "never a bare command that resolves against the working directory" clause, and
    the reason sentence beside it could be deleted with the case still green. "persists" is what
    only the reason carries.
    """
    section = _section(lane, _skill_text(lane), heading)
    paras = [" ".join(p.split()) for p in re.split(r"\n[ \t]*\n", section)]
    forbidding = [
        p for p in paras
        if re.search(r"[Nn]ever (a )?`cd` into", p)
        and re.search(r"(working directory|cwd) persists", p, re.I)
    ]
    assert forbidding, (
        f'{lane}/SKILL.md section "{heading}" ({why}) has no paragraph that both forbids the '
        f"`cd` and gives its reason (the working directory / cwd persists between calls). A copy "
        f"of the rule elsewhere in the file does not cover this: a lane reads the section it is "
        f"acting from. Paragraphs here mentioning `cd`: "
        f"{[p[:90] for p in paras if '`cd`' in p][:3]}"
    )


#: The three claims that make the residue comprehensible rather than merely forbidden. A lane
#: that knows only "do not `cd`" cannot recognise the wedge when it meets one, and the whole
#: hazard is that nothing else reports it.
_WHY_CLAIMS = (
    ("the cwd outlives the call", "PERSISTS between calls"),
    ("removal deregisters regardless", "DEREGISTERS"),
    ("so the registration list lies", "`worktree list` reads clean"),
)


@pytest.mark.parametrize("lane", BUILDER_LANES)
@pytest.mark.parametrize("why, wording", _WHY_CLAIMS)
def test_builder_skill_states_the_mechanism(lane: str, why: str, wording: str) -> None:
    assert wording in _skill_text(lane), (
        f"{lane}/SKILL.md never states {why} ({wording!r}); without it the rule is a taboo and "
        "the lane cannot tell a clean cleanup from a wedged one"
    )


#: Where each lane's end-of-pass checklist starts. The juniors' checklists are deltas, but this
#: box is theirs to tick: the wedge happens in their own worktree.
_CHECKLIST_HEADING = {
    "lead-dev": "## 9. End-of-firing checklist",
    "jr-dev-1": "## End-of-firing checklist",
    "jr-dev-2": "## End-of-firing checklist",
    "jr-dev-3": "## End-of-firing checklist",
}


@pytest.mark.parametrize("lane", BUILDER_LANES)
def test_the_cleanup_obligation_is_a_checklist_box_not_only_prose(lane: str) -> None:
    """Prose mid-file is the half that is easy to fake; the checklist is what a firing runs.

    The converse of the point-of-use gate above, and both directions are now load-bearing:
    deleting the §4 / §7.5 prohibition reds that one while this stays green, and deleting the
    checklist box reds this one while that stays green. Before the split, either deletion left
    both green.
    """
    section = _section(lane, _skill_text(lane), _CHECKLIST_HEADING[lane])
    assert "No shell left inside" in section, (
        f"{lane}/SKILL.md's end-of-pass checklist never asserts the no-`cd` / directory-gone "
        "obligation. A firing that ran its suite from inside the tree and left an empty "
        "directory behind ticks every other box"
    )


def test_every_builder_lane_is_a_real_tracked_file() -> None:
    """Non-vacuity for the hand lists. A renamed lane must red here, not vanish from coverage."""
    for lane in BUILDER_LANES:
        assert _skill_path(lane).is_file()
        # `test_builder_skill_states_why_at_the_point_of_use` is parametrised off `_POINTS_OF_USE`,
        # so a lane dropped from that map loses the check entirely while every remaining case
        # still reports green - the same silent-narrowing failure the hand list itself has.
        assert _POINTS_OF_USE.get(lane), (
            f"{lane} is a builder lane with no `_POINTS_OF_USE` entry, so the sectional "
            "point-of-use gate does not cover it at all"
        )


# --------------------------------------------------------------------------------------------
# The detector, both directions - a gate that cannot fail proves nothing
# --------------------------------------------------------------------------------------------
#: The verbatim spelling gh#240 was filed about.
_WEDGING = "Run it with `cd <worktree>/skills/_shared && python -m pytest tests -q`."

_WEDGING_FENCED = """
Run the suite:

```bash
cd <worktree>/skills/_shared && python -m pytest tests -q
```
"""

_FIXED = (
    "Run it with `<canonical_repo.python> -m pytest <worktree>/skills/_shared -q` - never a "
    "bare `cd` into the tree."
)

_SUBSHELL = "Where the runner has no path flag: `( cd <worktree> && npm ci )`."


def test_the_detector_catches_the_spelling_that_wedged_two_worktrees() -> None:
    """POSITIVE CONTROL. Without it, every green above is indistinguishable from a dead regex.

    This is not hypothetical for this repo: `test_skill_call_sites.py` records a sibling gate
    whose pattern was silently dead for its whole life and whose suite stayed green, and another
    whose corpus went empty when a function was renamed.
    """
    assert cd_chained_suite_runs(_WEDGING) == [
        "cd <worktree>/skills/_shared && python -m pytest tests -q"
    ], "the detector does not see the exact spelling gh#240 was filed about"


def test_the_detector_reads_fenced_blocks_too() -> None:
    """A backtick-only scan would teach the next author to write the step in a fence instead."""
    assert cd_chained_suite_runs(_WEDGING_FENCED) == [
        "cd <worktree>/skills/_shared && python -m pytest tests -q"
    ]


#: NEAR-MISSES. Each was injected into a builder SKILL.md against the `&&`/`cd`-only pattern and
#: passed all 34 tests, and each leaves the shell inside the worktree exactly as `cd X && run`
#: does. They are controls, not hypotheticals: a limitations note that lists what a gate cannot
#: see is only honest if the cases it CAN see are pinned here.
_EVASION_SEMICOLON = "Run it with `cd <worktree>/skills/_shared; python -m pytest tests -q`."
_EVASION_PUSHD = "Run it with `pushd <worktree>/skills/_shared && python -m pytest tests -q`."
_EVASION_OR = "Run it with `cd <worktree>/skills/_shared || exit 1; python -m pytest tests -q`."


def test_the_detector_catches_a_semicolon_separated_cd() -> None:
    """`;` runs the runner from the new directory just as `&&` does - it only drops the guard."""
    assert cd_chained_suite_runs(_EVASION_SEMICOLON) == [
        "cd <worktree>/skills/_shared; python -m pytest tests -q"
    ]


def test_the_detector_catches_pushd() -> None:
    """`pushd` moves the shell too; the matching `popd` is a promise, and often an unkept one."""
    assert cd_chained_suite_runs(_EVASION_PUSHD) == [
        "pushd <worktree>/skills/_shared && python -m pytest tests -q"
    ]


def test_the_detector_catches_an_or_guarded_cd() -> None:
    """`cd X || exit 1; run` is the defensive spelling of the same wedge."""
    assert cd_chained_suite_runs(_EVASION_OR) == [
        "cd <worktree>/skills/_shared || exit 1; python -m pytest tests -q"
    ]


def test_the_detector_passes_the_explicit_path_form() -> None:
    assert cd_chained_suite_runs(_FIXED) == []


def test_the_detector_passes_a_subshell_wrapped_cd() -> None:
    """`( cd X && ... )` returns the caller's cwd on exit, so it wedges nothing."""
    assert cd_chained_suite_runs(_SUBSHELL) == []


def test_the_detector_does_not_fire_on_prose_about_cd() -> None:
    """Precision. Every one of these lines is text this epic deliberately ADDED, so a detector
    that flagged them would red the fix itself - the false-positive shape that gets a gate
    allowlisted into uselessness."""
    for benign in (
        "**Never `cd` into the worktree to run them (gh#240).**",
        "a suite run spelled `cd <worktree>/<suite>` first leaves your shell inside the tree",
        "wrap the `cd` in a SUBSHELL - `( cd <worktree> && <command> )` - never a bare `cd`.",
    ):
        assert cd_chained_suite_runs(benign) == [], benign
