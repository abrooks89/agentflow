"""The Evidence table's numbers must be what the Evidence table's own commands produce.

`README.md`'s `## Evidence` table is a claim of a particular shape: every row prints a
count AND, in the right-hand column, the exact command that produces it, so a sceptical
reader can run the command and get the number. That reproducibility is the only thing
the table is worth. It has already drifted four separate times in one epic's history --
counts move as tests land, nobody re-measures, and a reader who checks one row, finds it
wrong, and stops believing the rest is the whole cost.

A renumber alone is therefore worthless; this module is the deliverable. It re-derives
the table FROM the table: the rows are parsed out of `README.md` rather than carried here
as a list, because a hardcoded list reproduces exactly the same drift one level up, where
nothing checks it.

WHAT THIS GATE CAN AND CANNOT DO -- stated because a gate that overstates itself is worse
than no gate:

* It CAN prove that every row it runs produces the number printed beside it, that a
  minimum number of rows were actually RUN rather than merely parsed, and that the list of
  standing excuses cannot grow without a visible edit.
* It CANNOT prove the number for a row it cannot run here (today: the Node/npm inventory
  row, whose command performs a network install; and, wherever the autodoc-graph
  dependencies or the orchestrator row's `node`/`bash` are absent, those rows too). Those are
  declared with reasons below. The orchestrator row may go unrun on a checkout without Node,
  but never under CI: `test_the_orchestrator_node_row_is_measured_under_ci` fails if it does.
* It CANNOT prove the counts on a platform other than the one the table states them for.
  Collection CAN differ by platform on one commit, so the comparison is enforced on the
  baseline platform only. That limit is in the README beside the table, not just here.
  No number is quoted for the divergence on purpose - see `BASELINE_PLATFORM` below, where a
  quoted pair was found false and removed rather than refreshed.

An earlier version of this file claimed to assert that the not-run set EQUALS the declared
set. It did contain such an assertion, and that assertion was a tautology: both sides
re-evaluated the same predicate over the same objects, so no input could make them differ.
It has been deleted rather than reworded, and the two properties that actually carry the
weight - a floor on rows executed, and a cap on declarations - are asserted instead. A dead
check is worse than a missing one when a public README cites it as a guarantee.

There is deliberately no `pytest.skip` anywhere in this file. A skip is not a kill -- a
skipped case reads as a green file, and "this row could not be checked" is precisely the
state that has to be loud. Unrunnable rows are asserted against a declaration instead.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"

# The table is found by its header cells rather than by position, so a section inserted
# above it changes nothing, and a renamed column fails loudly instead of shifting a parse.
HEADER_CELLS = ("layer", "tests", "counted with")

# The sha-pin scan, lifted out of its test by E-0766 so that BOTH directions can assert the same
# pattern. It lived inline in `test_evidence_prose_does_not_pin_a_commit_sha`, which only ever
# asserted that it does NOT fire on today's README - an assertion a pattern matching nothing at
# all satisfies perfectly. Measured at 23392c0: gutting the regex left this file green.
#
# Deliberately narrow: it wants a sha presented as a MEASUREMENT BASELINE, not any hex string,
# because the README names commits as durable facts (the *Scars* provenance box) and those must
# stay legal. Word-anchored, because the first draft matched inside the word "Shas" and flagged
# such a sentence - an unanchored alternative inside a prose scan finds itself.
_SHA_PIN = re.compile(
    r"\b(?:measured|counted)\b[^.\n]{0,60}`?[0-9a-f]{7,40}`?"
    r"|\bas of\b[^.\n]{0,60}`?[0-9a-f]{7,40}`?"
    r"|`?[0-9a-f]{7,40}`?[^.\n]{0,40}\b(?:the numbers|counts)\s+below\b",
    re.IGNORECASE,
)


def _sha_pin_in(text: str):
    """The match object for a measurement pinned to a commit sha, or None."""
    return _SHA_PIN.search(text)


# Non-vacuity floor. A parser that silently returns [] would make every assertion below
# pass over nothing, which is the classic way a guard like this goes green while the thing
# it guards rots. The table has had five rows for its whole life; four is the floor at
# which the parse is still believable.
MIN_ROWS = 4

# Coverage floor, and it is NOT derived from DECLARED_UNRUNNABLE. MIN_ROWS floors the PARSE;
# this floors what was actually MEASURED, which is a different number and the one that matters.
# Without it, declaring every row unrunnable left all four tests green having verified nothing -
# demonstrated, not imagined. Three is the rows that declare NO prerequisite (`_shared`, install,
# harness), so it holds on any checkout that can run this suite at all. `shared-tests` CI measures
# four (it also has Node, so the orchestrator row runs - asserted separately below, because
# raising this floor would turn a developer checkout without Node red). Deriving
# this from the declaration list would defeat it, because adding a declaration would lower the
# bar it is supposed to hold.
MIN_ROWS_EXECUTED = 3

# The platform the table's counts are stated for. Collection CAN differ by platform on one
# commit, so a single number in a row is not guaranteed true on both and the table therefore
# names a
#
# WHAT USED TO BE HERE, AND WHY IT IS GONE (E-0766). This comment claimed the divergence was
# "DEMONSTRATED, not assumed" and quoted it: the `_shared` collect at 1460 on Windows against
# 1490 on Linux. Measured 2026-09-22 at 23392c0: the `_shared` row does NOT diverge - Windows
# and Linux both collect 3369, matching `README.md`'s cell exactly. The row that DID diverge
# on the box where this was re-measured is autodoc-graph, and by 5 rather than 30.
#
# So the pair was wrong about the row, the magnitude and the numbers, inside the one module
# whose thesis is that an unenforced number rots. Nothing anywhere asserted it, which is how it
# survived three epics. It is REMOVED rather than refreshed, and no replacement pair is quoted:
# a fresh one would rot the same way, and naming the row that happens to diverge today invites
# the next reader to treat that instance as the rule. The PROPERTY is what this guard needs -
# collection can differ, so compare on one platform - and the property does not decay. The
# table therefore names a
# baseline platform and this guard enforces the comparison only there - on any other platform a
# mismatch is expected and says nothing about drift. CI's ubuntu-latest leg is the baseline
# because it is the one every job in the matrix shares with the published numbers.
#
# This is a real limit on the guard, not a tidy-up: on Windows the counts go unchecked. It is
# stated in the README beside the table rather than left for a reader to infer from a green run.
BASELINE_PLATFORM = "linux"

# A declaration is an excuse, so the list of them is capped. The cheapest way to turn this
# module green is to declare the awkward row unrunnable with a plausible reason; a cap makes
# that a visible edit rather than an invisible one.
MAX_DECLARATIONS = 3

# Generous: the `_shared` collect alone walks ~1.5k tests, and CI runners are slower than
# a dev box. A hang here should still end as a failure rather than a stuck job.
SUBPROCESS_TIMEOUT_S = 900


@dataclass(frozen=True)
class Row:
    """One parsed Evidence row: what it claims, and how it says the claim was produced."""

    layer: str
    claim: int
    command: str
    line_no: int


@dataclass(frozen=True)
class Unrunnable:
    """A declared reason a row cannot be executed in some environment.

    `matches` keys on the row's COMMAND, not on its layer name: the reason a row cannot
    run is a property of what the command needs, and prose in the left-hand column gets
    reworded far more often than the command does.

    `available` is a predicate rather than a flag so a declaration can be honest in both
    places this suite runs: where the prerequisite happens to be installed the row is
    executed and checked like any other, and only where it is genuinely absent does the row
    go unrun. A declaration whose prerequisite can never be satisfied here returns a
    constant `False` - the node row does, because a unit test must not perform a network
    install even on a machine that could - and that constant is the exception the reason
    text has to justify, not the norm.
    """

    key: str
    matches: Callable[[str], bool]
    available: Callable[[], bool]
    reason: str


def _module_available(*names: str) -> bool:
    return all(importlib.util.find_spec(name) is not None for name in names)


def _executables_available(*names: str) -> bool:
    return all(shutil.which(name) is not None for name in names)


DECLARED_UNRUNNABLE: tuple[Unrunnable, ...] = (
    Unrunnable(
        key="node-inventory",
        matches=lambda cmd: "npm ci" in cmd,
        available=lambda: False,
        reason=(
            "the command performs a network package install (`npm ci`) and needs a Node "
            "toolchain. The `shared-tests` CI job that runs this file provides neither, and "
            "a unit test must not install packages even where it could. This row is guarded "
            "instead by the `inventory-tests` CI job, which runs this exact command on "
            "ubuntu-latest and windows-latest."
        ),
    ),
    Unrunnable(
        key="autodoc-graph",
        matches=lambda cmd: "references/graph" in cmd,
        available=lambda: _module_available("networkx", "sqlite_vec"),
        reason=(
            "the autodoc-graph package's runtime dependencies (networkx, sqlite-vec) are "
            "installed by the `graph-tests` CI job, not by the `shared-tests` job that runs "
            "this file. Where they ARE importable -- a dev box, or the graph job itself -- "
            "this row is executed and checked like every other row."
        ),
    ),
    Unrunnable(
        key="orchestrator-node",
        matches=lambda cmd: "skills/orchestrator/tests/" in cmd,
        available=lambda: _executables_available("node", "bash"),
        reason=(
            "the command needs a Node runtime, and the tests it runs execute the shell commands "
            "`wave_group.js` emits in a real `bash`. No install and no network: where both are "
            "on PATH -- a dev box, and the `shared-tests` CI job, which sets Node up for this "
            "row -- it is executed and checked like every other row. Under CI a missing "
            "prerequisite is a failure, not an excuse (see "
            "`test_the_orchestrator_node_row_is_measured_under_ci`)."
        ),
    ),
)

ORCHESTRATOR_NODE_KEY = "orchestrator-node"


def _plain(cell: str) -> str:
    """Strip markdown emphasis / code ticks from a cell so its text can be used."""
    return cell.replace("**", "").replace("`", "").strip()


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _claim(cell: str, line_no: int) -> int:
    """The claimed count. Cells carry emphasis and trailing words ('43 wired assertions')."""
    match = re.search(r"\d[\d,]*", cell)
    assert match is not None, (
        f"README.md:{line_no}: Evidence row's Tests cell {cell!r} carries no number. Every row "
        "in this table is a count a reader is invited to reproduce; a row without one is a claim "
        "nobody can check, and it silently drops out of this guard's coverage."
    )
    return int(match.group(0).replace(",", ""))


def _command(cell: str, line_no: int) -> str:
    command = _plain(cell)
    assert command, (
        f"README.md:{line_no}: Evidence row has an empty 'Counted with' cell. The command IS the "
        "row's evidence -- without it the number is an assertion, not a measurement, and this "
        "guard has nothing to run."
    )
    return command


def _parse_evidence_table(text: str) -> list[Row]:
    lines = text.splitlines()

    start = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower().startswith("## evidence")),
        None,
    )
    assert start is not None, (
        f"{README} has no `## Evidence` heading. Either the section was removed -- in which case "
        "delete this module deliberately rather than leaving it to pass over nothing -- or it "
        "was renamed and this guard is now checking an empty parse."
    )

    header = None
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith("|") and [c.lower() for c in _cells(stripped)] == list(HEADER_CELLS):
            header = i
            break
    assert header is not None, (
        f"{README}: no table with header {HEADER_CELLS} was found under `## Evidence`. A renamed "
        "column is not a cosmetic edit here: this guard reads the table by its header, so an "
        "unnoticed rename turns the whole reproducibility check into a no-op."
    )

    # header + 1 must BE the |---|---:|---| separator row - asserted, not assumed. The loop
    # below starts at header + 2 on that assumption, so if the separator is ever deleted the
    # first data row silently becomes the separator and is dropped: the parse then yields four
    # rows, clears MIN_ROWS, and the guard goes green with one README number unverified. A
    # missing separator also stops GitHub rendering the block as a table at all, which is
    # exactly the kind of edit a docs-only review waves through.
    separator = lines[header + 1].strip() if header + 1 < len(lines) else ""
    assert separator and set(separator) <= set("|-: "), (
        f"README.md:{header + 2}: expected the table separator row under the Evidence header, "
        f"found {separator!r}. Without it this parser drops the first data row and still reports "
        "a plausible row count, so a number would go unchecked while everything looked green."
    )

    rows: list[Row] = []
    last_row_index = header + 1
    for j in range(header + 2, len(lines)):
        stripped = lines[j].strip()
        if not stripped.startswith("|"):
            break
        cells = _cells(stripped)
        line_no = j + 1
        assert len(cells) == 3, (
            f"README.md:{line_no}: Evidence row parsed into {len(cells)} cells, expected 3 "
            f"(Layer | Tests | Counted with): {stripped!r}. A row this guard cannot parse is a "
            "row it cannot verify."
        )
        rows.append(
            Row(
                layer=_plain(cells[0]),
                claim=_claim(cells[1], line_no),
                command=_command(cells[2], line_no),
                line_no=line_no,
            )
        )
        last_row_index = j

    # Nothing table-shaped may survive further down the section. The loop above stops at the
    # first non-`|` line, so a row added after a blank line - or a whole second table with the
    # same header - is simply not seen, and an unseen row reads as covered while nobody checks
    # its number. Catching it here keeps "every row in the Evidence section is verified" true
    # rather than "every row in the first contiguous block is".
    for k in range(last_row_index + 1, len(lines)):
        stripped = lines[k].strip()
        if stripped.startswith("## "):
            break
        assert not stripped.startswith("|"), (
            f"README.md:{k + 1}: a table row sits outside the Evidence table's contiguous block "
            f"({stripped[:80]!r}). This parser reads one block, so that row is invisible to it - "
            "it would be published as a checkable claim that nothing checks. Fold it into the "
            "table above, or move it out of the Evidence section."
        )
    return rows


ROWS: list[Row] = _parse_evidence_table(README.read_text(encoding="utf-8"))


def _declaration_for(row: Row) -> Unrunnable | None:
    hits = [d for d in DECLARED_UNRUNNABLE if d.matches(row.command)]
    assert len(hits) <= 1, (
        f"README.md:{row.line_no}: row {row.layer!r} matches more than one unrunnable "
        f"declaration ({[h.key for h in hits]}). Overlapping declarations make it ambiguous "
        "which reason excuses the row, which is how an unrunnable row acquires a reason nobody "
        "checked."
    )
    return hits[0] if hits else None


_CD_PREFIX = re.compile(r"^cd\s+(?P<dir>[^\s&]+)\s*&&\s*(?P<rest>.+)$")

# How each row's command reports its number. Kept as patterns rather than per-row parsers so a
# new row using either shape needs no change here.
_COUNTERS = (
    re.compile(r"(\d+)\s+tests?\s+collected"),
    re.compile(r"(\d+)\s+assertions?\s+wired"),
    # `node --test` summary line: `# tests N` under the TAP reporter, `<info glyph> tests N` under
    # the spec reporter, which recent Node uses even when piped. The glyph is matched as any 1-3
    # non-space chars because a subprocess decoded with a Windows code page receives it as three
    # mojibake chars. Line-anchored with nothing after the number, so a passing test's own line
    # (which ends in a duration) cannot be read as the total.
    re.compile(r"^\s*(?:#|\S{1,3})\s*tests\s+(\d+)\s*$", re.MULTILINE),
)


@dataclass(frozen=True)
class Result:
    row: Row
    declaration: Unrunnable | None
    ran: bool
    cwd: Path
    argv: tuple[str, ...]
    returncode: int | None
    measured: int | None
    output: str


def _resolve(row: Row) -> tuple[Path, str]:
    """Honour a leading `cd <dir> &&`: several rows only reproduce from their own tree."""
    match = _CD_PREFIX.match(row.command)
    if match:
        return REPO_ROOT / match.group("dir"), match.group("rest").strip()
    return REPO_ROOT, row.command


def _measure(row: Row) -> Result:
    cwd, command = _resolve(row)
    argv = shlex.split(command)
    # The table writes `python` because that is what a reader types. Inside the suite the
    # honest equivalent is the interpreter already running this test: on Windows a bare
    # `python` on PATH is routinely not the environment the suite was installed into, and
    # measuring one tree with another tree's interpreter compares nothing. This is an
    # interpreter binding, not a change to what the command does.
    if argv and argv[0] == "python":
        argv = [sys.executable, *argv[1:]]

    declaration = _declaration_for(row)

    if not cwd.is_dir():
        return Result(
            row, declaration, True, cwd, tuple(argv), None, None,
            f"working directory does not exist: {cwd}",
        )

    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(row, declaration, True, cwd, tuple(argv), None, None, repr(exc))

    blob = f"{proc.stdout}\n{proc.stderr}"
    measured: int | None = None
    for pattern in _COUNTERS:
        found = pattern.findall(blob)
        if found:
            # Last match: pytest prints its total on the final line, after any per-file noise.
            measured = int(found[-1])
            break
    return Result(
        row, declaration, True, cwd, tuple(argv), proc.returncode, measured, blob[-4000:]
    )


@functools.lru_cache(maxsize=1)
def _results() -> tuple[Result, ...]:
    """Run every row once. Rows whose declared prerequisite is genuinely absent are not run."""
    out: list[Result] = []
    for row in ROWS:
        declaration = _declaration_for(row)
        if declaration is not None and not declaration.available():
            out.append(Result(row, declaration, False, REPO_ROOT, (), None, None, ""))
            continue
        out.append(_measure(row))
    return tuple(out)


def test_evidence_table_parse_is_not_vacuous() -> None:
    """A parse that finds nothing would make every other assertion here pass over nothing."""
    assert len(ROWS) >= MIN_ROWS, (
        f"parsed only {len(ROWS)} row(s) from README.md's Evidence table, expected at least "
        f"{MIN_ROWS}. This is the guard's own vacancy check: with an empty or truncated parse "
        "every reproducibility assertion below iterates nothing and the suite reports green "
        "while the README's numbers go unverified -- the exact failure this module exists to "
        "prevent. Fix the parser or the table; do not lower the floor."
    )

    layers = [r.layer for r in ROWS]
    commands = [r.command for r in ROWS]
    assert len(set(layers)) == len(layers), (
        f"duplicate Layer names in the Evidence table: {layers}. Rows are addressed by layer in "
        "this guard's failure messages, so duplicates make a failure point at the wrong row."
    )
    assert len(set(commands)) == len(commands), (
        f"two Evidence rows print the same command: {commands}. Two different counts cannot both "
        "come out of one command, so at least one of the numbers is unverifiable by construction."
    )
    for row in ROWS:
        assert row.claim > 0, (
            f"README.md:{row.line_no}: row {row.layer!r} claims {row.claim}. A zero or negative "
            "count is not a measurement, and it makes the row's reproducibility claim empty."
        )
        assert row.command.strip(), (
            f"README.md:{row.line_no}: row {row.layer!r} has no command. The command is the "
            "evidence; without it the number is unfalsifiable."
        )


def test_rows_that_cannot_run_here_are_exactly_the_declared_set() -> None:
    """An unrunnable row must be declared, and a declared reason must still match a row."""
    assert len(ROWS) >= MIN_ROWS, (
        f"only {len(ROWS)} Evidence row(s) parsed; refusing to check the unrunnable set over a "
        "parse this thin, because it would pass vacuously."
    )

    for declaration in DECLARED_UNRUNNABLE:
        matched = [r.layer for r in ROWS if declaration.matches(r.command)]
        assert len(matched) == 1, (
            f"unrunnable declaration {declaration.key!r} matches {len(matched)} Evidence row(s) "
            f"({matched}), expected exactly 1. A declaration matching nothing is a stale excuse "
            "still sitting in the allowlist -- the next row that happens to match it inherits a "
            "reason nobody wrote for it. Delete or re-key it."
        )

    # WHAT USED TO BE HERE, AND WHY IT IS GONE. This test previously asserted that the set of
    # rows not run equalled the set of declared-and-unavailable rows. Both sides evaluated the
    # SAME predicate over the SAME objects - `_results` marks a row unrun under exactly the
    # condition the second comprehension re-tested - so no input could make them differ. It was
    # a tautology wearing the module's headline guarantee, and the README pointed readers at it.
    # A dead assertion that a public document cites as a guarantee is worse than no assertion,
    # which is this module's own thesis applied to itself. The two properties that actually
    # carry that weight are asserted instead: the excuse list cannot grow silently (below), and
    # coverage cannot fall (`test_the_guard_actually_measured_something`).
    results = _results()
    assert len(DECLARED_UNRUNNABLE) <= MAX_DECLARATIONS, (
        f"{len(DECLARED_UNRUNNABLE)} unrunnable declarations, cap is {MAX_DECLARATIONS}. The cap "
        "exists because a declaration is an excuse: the cheapest way to make this module green "
        "is to declare the failing row unrunnable with an honest-sounding reason, and nothing "
        "else here would notice. Raising the cap is a deliberate edit a reviewer sees; adding a "
        "declaration under it is not."
    )
    # Deliberately NOT re-asserting "unrun == declared-and-unavailable" in any spelling. Any
    # version of that comparison re-evaluates the condition `_results` already branched on, so
    # it is the same tautology with different words. What it was reaching for - a row cannot
    # become unchecked without someone saying so - is delivered by the cap above plus the
    # floor in `test_the_guard_actually_measured_something`.

    # A row expected to run that then fails to run is the loud case: it must never be rounded
    # down into "well, it could not be checked".
    for result in results:
        if not result.ran:
            continue
        assert result.returncode == 0 and result.measured is not None, (
            f"README.md:{result.row.line_no}: the command for {result.row.layer!r} was expected "
            f"to run here and did not produce a count (exit={result.returncode}, "
            f"parsed={result.measured}).\n"
            f"    cwd:     {result.cwd}\n"
            f"    command: {' '.join(result.argv) or result.row.command}\n"
            "This is a broken row, not an excusable one: the README invites a reader to run this "
            "exact command, so whatever failed here fails for them too.\n"
            f"--- tail of output ---\n{result.output}"
        )


def test_every_runnable_row_produces_the_number_the_table_claims() -> None:
    """The table's premise: run the right-hand column, get the left-hand number."""
    if not sys.platform.startswith(BASELINE_PLATFORM):
        # NOT a skip: the test still runs and still asserts the parse and the floor via the
        # other cases in this module. Only the count COMPARISON is platform-scoped, because
        # the numbers are stated for one platform and are known to differ on another.
        results = _results()
        assert len(results) >= MIN_ROWS, (
            f"parsed {len(results)} Evidence rows, expected at least {MIN_ROWS}."
        )
        return

    results = _results()
    assert len(results) >= MIN_ROWS, (
        f"only {len(results)} Evidence row(s) to check; a thin parse would let this pass over "
        "nothing while the README's numbers went unverified."
    )

    mismatches = [
        r for r in results if r.ran and r.measured is not None and r.measured != r.row.claim
    ]
    assert not mismatches, "\n".join(
        [
            "README.md's Evidence table prints numbers that its own commands do not produce:",
            *[
                f"  README.md:{r.row.line_no}  {r.row.layer}: claims {r.row.claim}, command "
                f"produces {r.measured}\n"
                f"      cwd:     {r.cwd}\n"
                f"      command: {r.row.command}"
                for r in mismatches
            ],
            "",
            "The whole point of this table is that a sceptical reader can reproduce every number "
            "from the command printed beside it. A wrong number here is worse than no table at "
            "all: the first row a reader checks and finds false discredits every other claim in "
            "the README, including the true ones. Update the number to what the command "
            "produces -- do not adjust the command to fit the number.",
        ]
    )


# ---------------------------------------------------------------------------
# The parser's own cover (E-0766). Every test above drives the REAL README, which is well-formed,
# so every assertion inside `_parse_evidence_table` was unreachable by test. Measured at 23392c0
# with the count comparison FORCED ON so the result could not be a platform artifact: deleting the
# separator assertion outright left this file at 5 passed, and so did deleting the out-of-block
# sweep. Nine mutations survived in total, including zeroing `MIN_ROWS` and gutting the sha-pin
# pattern. A parser whose defensive assertions cannot fail is one whose next tidy-up deletes them
# for free, and the module they protect is what stands between a published number and nobody
# checking it.
#
# These cases feed SYNTHETIC text and assert on the MESSAGE, never on a bare `AssertionError`: a
# test that only asserts "it raised" passes when the parser raises for a different reason, which
# is the weak version this module's own risk note warns about. They are also deliberately NOT
# routed through `_results()` or `_measure()` - `_parse_evidence_table` is a pure string function,
# so these spawn no subprocess, and putting them behind the runner would cost four process spawns
# per case for nothing.
#
# No `pytest.skip` and no platform guard anywhere below, per the module docstring: a skip is not a
# kill, and string parsing is platform-independent by construction.

_GOOD_TABLE = """\
## Evidence

Some intro prose that is not a table.

| Layer | Tests | Counted with |
|---|---|---|
| alpha | **11** | `cd a && run one` |
| beta | **22** | `cd b && run two` |

## Next section
"""


def test_the_synthetic_fixture_parses_so_every_negative_below_discriminates() -> None:
    """The control. Without it, each negative case could be passing on a broken fixture.

    Every test below mutates ONE thing about `_GOOD_TABLE` and expects a named refusal. If the
    fixture were malformed to begin with they would all still raise, for the wrong reason, and the
    whole block would read as cover while asserting nothing about the mutation it names.
    """
    rows = _parse_evidence_table(_GOOD_TABLE)
    assert [r.layer for r in rows] == ["alpha", "beta"]
    assert [r.claim for r in rows] == [11, 22]
    assert [r.command for r in rows] == ["cd a && run one", "cd b && run two"]


def test_a_missing_separator_row_is_refused() -> None:
    """Deleting the separator silently drops the first data row while the count still looks fine.

    The row loop starts two lines below the header on the assumption that the separator is there,
    so without it the first data row IS the separator and is dropped: the parse yields one row
    fewer, still clears its floor, and the guard goes green with a README number unverified. A
    missing separator also stops GitHub rendering the block as a table, which is exactly the edit
    a docs-only review waves through.
    """
    text = _GOOD_TABLE.replace("|---|---|---|\n", "")
    with pytest.raises(AssertionError, match="expected the table separator row"):
        _parse_evidence_table(text)


def test_a_row_outside_the_contiguous_block_is_refused() -> None:
    """A row after a blank line is invisible to a one-block parser, so it reads as covered.

    The sweep that catches this runs after the row loop and looks like dead code, which is what
    makes it cheap to delete. It is the assertion that keeps "every row in the Evidence section is
    verified" true, rather than only "every row in the first contiguous block is".
    """
    text = _GOOD_TABLE.replace(
        "## Next section",
        "| gamma | **33** | `cd c && run three` |\n\n## Next section",
    )
    with pytest.raises(AssertionError, match="sits outside the Evidence table's contiguous block"):
        _parse_evidence_table(text)


@pytest.mark.parametrize(
    "row",
    [
        "| alpha | **11** |",
        "| alpha | **11** | `cd a && run one` | extra |",
    ],
    ids=["two-cells", "four-cells"],
)
def test_a_row_that_is_not_three_cells_is_refused(row: str) -> None:
    """Both directions, because a short row and a long row fail for the same reason.

    A two-cell row has lost its command; a four-cell row means the header this parser matched no
    longer describes the data beneath it. Either way the row cannot be verified, which is the only
    thing this table exists for.
    """
    text = _GOOD_TABLE.replace("| alpha | **11** | `cd a && run one` |", row)
    with pytest.raises(AssertionError, match="expected 3"):
        _parse_evidence_table(text)


def test_a_renamed_column_is_refused_rather_than_shifting_the_parse() -> None:
    """Why this parser reads by header instead of by position, pinned rather than commented.

    "Just take the first table under the heading" is the obvious simplification, and it silently
    disables the rename protection: columns would be read positionally, so a renamed or reordered
    table would parse into plausible nonsense. A rename is not a cosmetic edit here.
    """
    text = _GOOD_TABLE.replace(
        "| Layer | Tests | Counted with |", "| Layer | Count | Counted with |"
    )
    with pytest.raises(AssertionError, match="no table with header"):
        _parse_evidence_table(text)


def test_a_missing_evidence_heading_is_refused_rather_than_parsed_as_empty() -> None:
    """An empty parse is the one failure this module cannot survive quietly.

    If the section is gone, the honest outcomes are "delete this module deliberately" or "fix the
    heading" - never "pass over nothing", which is what a parser returning an empty list would do
    while every count in the README went unchecked.
    """
    text = _GOOD_TABLE.replace("## Evidence", "## Reproducibility")
    with pytest.raises(AssertionError, match="has no `## Evidence` heading"):
        _parse_evidence_table(text)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ("| alpha | many | `cd a && run one` |", "carries no number"),
        ("| alpha | **11** |  |", "empty 'Counted with' cell"),
    ],
    ids=["no-number", "no-command"],
)
def test_a_row_missing_its_number_or_its_command_is_refused(row: str, expected: str) -> None:
    """The two halves of a row's claim, each unreachable by test until now.

    A row with no number is a claim nobody can check and drops out of this guard's coverage
    silently. A row with no command is an assertion rather than a measurement, leaving this guard
    nothing to run. Both are refused where they are parsed rather than somewhere downstream.
    """
    text = _GOOD_TABLE.replace("| alpha | **11** | `cd a && run one` |", row)
    with pytest.raises(AssertionError, match=expected):
        _parse_evidence_table(text)


def test_a_rows_reported_line_number_is_the_line_it_is_actually_on() -> None:
    """`line_no` addresses every failure message in this module, and nothing checked it.

    An off-by-one is the commonest edit in a line-number-carrying parser, and it is invisible: the
    guard still runs the right commands and compares the right numbers, it just sends the reader to
    the wrong line. Derived from the fixture rather than hardcoded, so this survives an edit to the
    prose above the table instead of pinning a position.
    """
    lines = _GOOD_TABLE.splitlines()
    for row in _parse_evidence_table(_GOOD_TABLE):
        on_that_line = lines[row.line_no - 1]
        assert row.layer in on_that_line, (
            f"row {row.layer!r} reports line {row.line_no}, which holds {on_that_line!r}. That "
            "number is what every failure message in this module points a reader at, so an "
            "off-by-one sends them to the wrong row while everything else looks right."
        )


def test_the_sha_pin_scan_matches_a_real_pin_and_not_only_clean_prose() -> None:
    """The positive case for a rule that was only ever asserted one-sidedly.

    The test below proves the pattern does not fire on today's README. It cannot tell a working
    pattern from one that matches nothing at all - measured at 23392c0: gutting the regex entirely
    left this file green. A guard that can only pass is not a guard, and that is the same defect
    class as the parser holes above, sitting in the test beside them.

    The pattern is narrowed twice already, so a third narrowing that overshoots would read as a bug
    fix. These are the shapes it must keep catching, phrased as the drift it exists to stop.
    """
    for prose in (
        "The counts below were measured against master `deadbeef1`.",
        "Test surface as of `1a2b3c4d`, reproduced per row.",
        "`0f1e2d3c` is the commit the counts below were taken on.",
    ):
        assert _sha_pin_in(prose) is not None, (
            f"the sha-pin scan does not match {prose!r}, which pins a measurement to a commit. A "
            "pattern matching no real pin passes on every README and guards nothing, and the "
            "one-sided test below cannot tell that apart from prose that is genuinely clean."
        )
    legal = "Shas are written bare here, and `5d6b0cf` is an `agentflow-dev` commit."
    assert _sha_pin_in(legal) is None, (
        "the scan fired on prose naming a commit as a durable fact rather than as a "
        "measurement baseline. That spelling is legal on purpose, and an over-broad pattern is how "
        "this module gets allowlisted into uselessness."
    )


def test_the_coverage_prose_numbers_are_pinned_to_the_constants_they_describe() -> None:
    """The README states two figures about this guard's own coverage; both were unenforced.

    Beside the table it says three rows declare a prerequisite (two of them excused in
    `shared-tests`), and that the `_shared`, install and harness rows - the floor - are checked
    everywhere. Those are `len(DECLARED_UNRUNNABLE)` and
    `MIN_ROWS_EXECUTED` written out in prose for a reader - and the assertions that existed were
    both one-sided bounds, `<= MAX_DECLARATIONS` and `>= MIN_ROWS_EXECUTED`. Measured at 23392c0:
    raising the cap to 5, or zeroing the floor, left this file green. So the README could say
    "two are excused" while three were, and nothing here would notice.

    Equality rather than a bound, because a bound is what let the prose drift in the first place.
    These are the numbers a reader checks this module against; if the real coverage changes, the
    honest edit is to change the sentence and this assertion together, which is a reviewer-visible
    act instead of a silent one.
    """
    assert len(DECLARED_UNRUNNABLE) == 3, (
        f"{len(DECLARED_UNRUNNABLE)} unrunnable declarations, but the Evidence prose in README.md "
        "tells a reader that THREE rows declare a prerequisite and names them. Changing the set without "
        "changing that sentence publishes a coverage claim this repository does not honour - "
        "update both, in the same commit, or the README is lying about its own guard."
    )
    assert MIN_ROWS_EXECUTED == 3, (
        f"MIN_ROWS_EXECUTED is {MIN_ROWS_EXECUTED}, but the Evidence prose in README.md names "
        "THREE rows as the floor checked everywhere. The floor and the sentence are the "
        "same claim written twice; a floor that drifts below the sentence means the guard can go "
        "green having measured fewer rows than the README promises."
    )


def test_evidence_prose_does_not_pin_a_commit_sha() -> None:
    """A sha pinned in the prose is stale the moment the branch moves.

    The intro used to read "measured on this branch against master `<sha>`". That sha rotted
    while the numbers beside it rotted too, and nothing can keep it current: no step re-writes
    it on merge. The counts are now re-derived by this module on every CI run, which is a live
    claim rather than a snapshot, so the pin is both wrong and redundant.
    """
    # Scanned over the WHOLE README, not just the Evidence intro. The first version windowed
    # from `## Evidence` to the table header, which is where the author happened to be looking -
    # and the pin it was written to kill survived at README.md:13, in the *Scars* preamble,
    # describing itself as "the commit the numbers below were measured against". The paragraph
    # this PR added below the table was outside the window too. A rule enforced only where its
    # author looked is a rule the next reader meets somewhere else.
    #
    # The pattern is deliberately narrow: it wants a sha presented as a MEASUREMENT BASELINE,
    # not any hex string. The *Scars* provenance box names predecessor commits as durable
    # facts rather than rotting snapshots, so bare shas stay legal.
    text = README.read_text(encoding="utf-8")
    # Word-anchored. The first draft of this pattern used a bare `as of` alternative and
    # matched inside the word "Shas", flagging such a sentence (the provenance box naming
    # commits as durable facts). An unanchored alternative inside a prose scan finds itself.
    stale = _sha_pin_in(text)
    found = stale.group(0) if stale is not None else ""
    assert stale is None, (
        f"the Evidence intro pins a commit sha ({found!r}). Nothing updates that sha on merge, "
        "so it starts rotting immediately and re-creates the exact drift this module was written "
        "to end -- a reader who checks the pin finds it resolves to nothing and stops trusting "
        "the counts beside it. The counts are guarded per-run by this test instead; say that, "
        "rather than pinning a snapshot."
    )


def test_the_guard_actually_measured_something() -> None:
    """COVERAGE floor, as distinct from the parse floor.

    The parse-vacuity test proves rows were found; it says nothing about rows being RUN.
    `_results` returns one entry per row including the ones it never executed, so a length
    check over results is satisfied by rows nobody measured. Declaring all five rows
    unrunnable used to leave this module fully green with zero commands executed - the exact
    "gate that overstates itself" this file opens by warning against, in the file itself.
    """
    results = _results()
    measured = [r for r in results if r.ran and r.measured is not None]
    assert len(measured) >= MIN_ROWS_EXECUTED, (
        f"only {len(measured)} Evidence row(s) were actually executed and measured here "
        f"({sorted(r.row.layer for r in measured)}), below the floor of {MIN_ROWS_EXECUTED}. "
        "Every other row's number is currently unverified by anything. This is the failure mode "
        "the module exists to prevent, arriving from inside: rows drift into the excused pile "
        "and the suite keeps reporting green over an unchecked table."
    )


def test_the_orchestrator_node_row_is_measured_under_ci() -> None:
    """The orchestrator row's declaration is an excuse for a checkout without Node, never for CI.

    `MIN_ROWS_EXECUTED` stays at the three prerequisite-free rows so a developer checkout without
    Node is not red, which means the floor alone would let this row drop out of CI unnoticed - the
    `shared-tests` job would still measure three and pass. This pins the README's claim that the
    job hosting this guard checks the row: under GitHub Actions it must have run and produced a
    count. Off CI there is nothing further to assert; the declaration and its cap cover it.
    """
    declared = [d for d in DECLARED_UNRUNNABLE if d.key == ORCHESTRATOR_NODE_KEY]
    assert len(declared) == 1, (
        f"no single {ORCHESTRATOR_NODE_KEY!r} declaration; the README says the orchestrator row "
        "is guarded, and this test is how."
    )
    rows = [r for r in ROWS if declared[0].matches(r.command)]
    assert len(rows) == 1, (
        f"{len(rows)} Evidence row(s) run the orchestrator node tests, expected exactly 1. The "
        "README's Evidence table carries that layer; a removed row is an unpublished test layer."
    )
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    result = next(r for r in _results() if r.row == rows[0])
    assert result.ran and result.measured is not None, (
        f"README.md:{rows[0].line_no}: under CI the orchestrator row was not measured "
        f"(ran={result.ran}, parsed={result.measured}). The job hosting this guard is meant to "
        "provide `node` and `bash`; if it no longer does, the README's claim that this job checks "
        "the row is false. Restore the setup step rather than excusing the row."
    )


@pytest.mark.parametrize(
    "output",
    [
        "TAP version 13\nok 1 - x\n1..9\n# tests 9\n# suites 0\n# pass 9\n",
        "\u2714 tests 3 (1ms)\n\u2139 tests 9\n\u2139 suites 0\n\u2139 pass 9\n",
        # The spec output as a cp1252-decoding subprocess on Windows actually receives it.
        "\u2714 tests 3 (1ms)\n\u2139 tests 9\n".encode("utf-8").decode("cp1252", "replace"),
    ],
    ids=["tap", "spec", "spec-cp1252"],
)
def test_the_node_test_summary_is_counted(output: str) -> None:
    """The orchestrator row is only checkable if its summary line parses, in either reporter."""
    counts = [int(m) for p in _COUNTERS for m in p.findall(output)]
    assert counts == [9], (
        f"the counters read {counts} from a `node --test` summary reporting 9 tests. Without a "
        "working pattern the orchestrator row is 'expected to run and produced no count' - red "
        "for a parser bug, or, if the pattern over-matches, compared against the wrong number."
    )
