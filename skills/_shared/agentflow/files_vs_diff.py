"""What an epic's spec DECLARED it would touch, measured against what its merged range touched.

WHY THIS EXISTS. Lane disjointness - the property that stops two builders editing the same
file - is computed by comparing spec `## Files` sections, and that comparison is the entire
instrument. A section that is SHORT therefore produces a disjointness proof that is
confidently wrong, and nothing anywhere reported it (gh#161). The standing Directive already
requires the section to EXIST and to be specific enough to seam-check; both constrain the
SHAPE of the list, and neither touches its COMPLETENESS, which is the property disjointness
actually depends on. This module is the missing half: it reads the declaration and the merged
diff and states where they disagree.

TWO DELTAS, AND THEY ARE NOT THE SAME FINDING.

* UNDECLARED - touched by the diff, covered by nothing in `## Files`. This is the
  disjointness HAZARD: the seam check could not see the path, so a publish that relied on the
  declaration was wrong and nobody learned. E-0220 is the recorded instance - it declared six
  entries, touched nine files, and the builder hit `skills/_shared/tests/test_missed_wake_alarm.py`
  mid-build, inside a directory another epic held at directory level.
* OVER-DECLARED - declared, never touched. Not a defect on its own. It PARKS other lanes
  behind a seam nothing was ever going to use, which is the opposite failure and is currently
  just as invisible. Reported separately and never folded into the first count.

THE THIRD CLASS IS THE INTERESTING ONE, AND IT IS NEITHER DELTA. A declared entry is not
always a resolvable path. Real specs declare exact paths, DIRECTORIES
(`skills/_shared/tests/`), globs, `<data_root>/...` placeholders that resolve outside this
repository, and - because the borrowed parser reads every backticked pathlike in the section,
prose included - bare mentions like `registry.py:1267`. Silently counting an entry like that
as a match HIDES misses; counting it as a miss cries wolf. So each entry is classified and
the unresolvable ones are reported in their own list, per entry, with the reason.

ONE UNRESOLVABLE BUCKET WAS FOUR BUCKETS WEARING ONE REASON STRING. Measured across the
corpus, 32 of 52 unresolvable entries carried the single generic "names no path in the tree"
reason, and a reader could not tell a typo from a footnote. The reasons are the product here -
`render` quotes them per entry and a spec author fixes what the reason names - so the entries
now carry a CODE as well, and the report groups by what the reader can act on:

* PARSER NOISE (`empty`, `separator`, `prose`, `bare-mention`) - not a declaration anybody
  wrote as a path. The borrowed regex promotes any backticked token carrying a `/` or a `.`,
  which sweeps up the `/` between two adjacent inline-code spans, a bare module name, and in
  one observed case an entire prose sentence. Nothing to repair in the spec.
* SPEC DEFECT (`misplaced`, `absolute`, `parent-relative`) - a declaration that meant a path
  and named one this repository does not have. This is the actionable class.
* OUT OF REPO BY DESIGN (`placeholder`) - a `<data_root>/...` entry. Correct as written.

A DIRECTORY OR GLOB CANNOT BE CHECKED FOR COMPLETENESS EVEN IN PRINCIPLE, and this module
says so per entry rather than in a footnote. Such an entry does bound a region of the tree, so
it legitimately covers what it absorbs and the paths under it are not reported undeclared -
but every one of those absorbed paths could equally have been a surprise, and an UNDECLARED
count computed under a directory entry is a weaker claim than one computed under exact paths.
`patterns_used` carries that caveat as data.

GLOB MATCHING IS DEFINED HERE, NOT INHERITED. `fnmatch.fnmatch` was the obvious call and it is
the wrong one twice over: it is CASE-INSENSITIVE on Windows and case-sensitive elsewhere, so
the same spec and the same range gave a different `undeclared` set on a daily driver than in
CI - a detector whose answer depends on which machine ran it is not a detector; and its `*`
crosses `/`, so `skills/*/SKILL.md` silently absorbed `skills/a/b/SKILL.md` and biased the
whole measurement toward UNDER-reporting. `_glob_re` translates the pattern to an explicit
regex instead: always case-sensitive, `*` and `?` never cross a path separator, `**` does
(and `**/` also matches zero directories), `[...]` is a character class, and `{a,b}` is an
alternation. Brace support is deliberate rather than a refusal: a brace entry names an exact
set of paths, so treating it as a pattern reports the paths it names as declared. It is still
counted NOT DECIDABLE, because the entry bounds a set rather than enumerating one.

ONE TYPO IS ONE FINDING. A spec that writes `skills/_shared/agentflow/tests/test_registry.py`
for the real `skills/_shared/tests/test_registry.py` used to produce TWO findings - the entry
unresolvable AND the real file undeclared - and 8 of 61 undeclared paths in the corpus were
paths the spec had in fact declared, one path segment off. Where an undeclared path's basename
uniquely matches one `misplaced` entry in the same spec, the pair is reported once as
MIS-PATHED, naming both the string the spec wrote and the path the range touched, and is
removed from both lists. `render` and `as_dict` both say the count, so the smaller UNDECLARED
number is never a quiet one.

ENTRIES ARE DEDUPED BEFORE ANYTHING IS COUNTED. 8 duplicate raw strings across 6 epics meant
"declared N entries" was not a count of declarations. The dropped strings are kept in
`duplicate_entries` rather than discarded, because a spec listing the same path twice is worth
seeing.

IT REPORTS. IT DOES NOT FAIL A BUILD - YET. Nothing today measures this gap at all, so the
first deliverable is a measurement, not a gate: exit is non-zero ONLY for a malformed
invocation, never for what the comparison found. This is the same distinction the merge
preflight draws between a check that returns EVIDENCE and a check that returns a VERDICT, and
getting it backwards is a recorded incident here - a report-only signal wired to redden a
build teaches the fleet to route around it before anyone has learned whether short lists are
rare or routine, which is gh#161's actual open question.

THE DECLARATION PARSER IS BORROWED, NEVER RESTATED. `plan_parser.files_section` is what the
promotion gate and `fleet_report`'s seam signal already read; a second definition here of
"what the spec declared" would be a second answer to that question, and the two would drift
in exactly the way `reconcile`'s docstring warns about. What is NOT borrowed is
`fleet_report._seam_paths`: it collapses the entries into a frozenset and drops the raw text,
so per-entry classification - the whole third class above - cannot be recovered through it.
The classifier below is a PRE-PASS over what that parser returns, for the same reason: the
regex feeds the live promotion gate, so tightening it there would change a gate while this
module is still only measuring.

NO PATH IS EXEMPT FROM THE DIFF SIDE. There is no ignore-list for CHANGELOG-ish or incidental
files, deliberately: an exemption list is a hand-written list, and a check derived from a
hand-written list inheriting that list's blind spots is the defect this module exists to
measure (the family in #144, #146, #156, #157).

A SPEC THAT DECLARED NOTHING READABLE IS A THIRD OUTCOME, not an epic that under-declared
everything. `declaration_usable` is False and both deltas are empty, because "told you
nothing" and "told you it touches nothing" are different facts and reporting the first as a
maximal miss would bury the real ones.

A RANGE THAT CANNOT ANSWER IS THE SAME OUTCOME ON THE OTHER SIDE (gh#316). Where `head` is an
ANCESTOR of `base`, `merge-base(base, head)` is `head` itself, so the three-dot diff is empty
and git exits 0 - and the empty touched set that follows is not a finding, it is the absence of
one. Reported as a comparison, it is the sentence `_git`'s docstring already warns about: every
declared entry over-declared, nothing undeclared, the most confident wrong answer this module
can give. `_git`'s guard cannot see it, because that guard fires on a git FAILURE and this
reaches the same answer through a successful invocation. `range_usable` is the range-side
`declaration_usable`: False, every range-derived list empty, and the report says which of the
two shapes it was, because "the refs are the wrong way round" and "base and head are one commit"
are different repairs. The declaration side is still reported (gh#347): the distinct-entry
count, the duplicates and the absolute, parent-relative, placeholder and noise classes are
parses of the spec that no range changes. `misplaced` is withheld with the deltas, because its
reason is half a claim about the range.

THE PREDICATE IS "THE RANGE CONTAINS NO COMMITS", NEVER "THE DIFF CAME BACK EMPTY". A head that
genuinely descends from base and honestly touched nothing - an empty commit, a commit reverted
within the range - produces the identical empty diff, and flagging it would trade a silent wrong
answer for a noisy false alarm on legitimate input, which is worse for a report-only instrument
the fleet is meant to trust. `range_commits` asks git for the commit counts on each side
instead, so the flag fires on "there is no work in this range to measure" and stays silent on
"the work in this range changed no files".

THE JOIN IS THE WEAKEST LINK, AND A CORPUS-WIDE NUMBER INHERITS ITS SHAPE.
--------------------------------------------------------------------------------------------
Everything above is per-epic and per-range: given a spec and a range, the answer is
reproducible. A number quoted ACROSS a fleet is not, because nothing in this module - or
anywhere else - defines which merged commits belong to which spec. That join is reconstructed
by hand from `completion-truth` frontmatter, and three independent derivations of "the merged
agentflow epics" produced three different epic sets.

This module's own first measurement is the worked example. Its join keyed on `completion-truth`
naming a sha in the VERB form (`squash <sha>`, `merged as <sha>`), and it therefore missed an
11-commit epic whose completion truth records `PR #125 f10226f` instead - a sha in a form the
pattern did not read. That epic turned out to be the corpus's worst under-declarer. The
derived set inherited its source's shape assumption and reported the gap as absence, which is
precisely the defect family this module was built to measure, committed by the tool measuring
it.

So: a corpus-wide count from this module is a claim about a JOIN METHOD, not a fixed fact
about a fleet. Report it with the method that produced it and with the epic count that method
found ("N epics joined by <rule>"), never as "the fleet's undeclared count". Widen the rule
and the number moves; that is the honest reading, not an error in either run.

CLI
---
    python -m agentflow.files_vs_diff --repo <checkout> --spec <spec.md> --commit <sha>
    python -m agentflow.files_vs_diff --repo <checkout> --spec <spec.md> --base A --head B
    ... --json      machine-readable, for sweeping a fleet's worth of merged epics

`--base A --head B` reads the range from the MERGE BASE of the two, not from A's tip, so its
answer is the branch's own work and does not move as `A` advances underneath it (gh#263). See
`touched_paths` for why that is the three-dot form and why `--commit` is unaffected by it.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from .plan_parser import files_section

#: An entry naming one exact path. The only kind whose completeness is decidable.
FILE = "file"

#: A directory, glob or brace alternation. Bounds a region of the tree, absorbs what falls
#: inside it, and can never be shown complete - there is no list to be missing from.
PATTERN = "pattern"

#: An entry that resolves to no path in this repository. Contributes no coverage and counts
#: in neither delta; reported on its own with the reason it could not be placed.
UNRESOLVABLE = "unresolvable"

# --------------------------------------------------------------------------------------- #
# Per-entry reason codes. The reason STRING is what a reader repairs from; the CODE is what
# the report groups by, so that a parser artefact never sits in the same list as a typo.
# --------------------------------------------------------------------------------------- #
R_EMPTY = "empty"
R_SEPARATOR = "separator"
R_PROSE = "prose"
R_BARE_MENTION = "bare-mention"
R_PLACEHOLDER = "placeholder"
R_ABSOLUTE = "absolute"
R_PARENT_RELATIVE = "parent-relative"
R_MISPLACED = "misplaced"

#: Not a declaration anybody wrote as a path - the borrowed regex promoted it. No repair.
NOISE_CODES = frozenset({R_EMPTY, R_SEPARATOR, R_PROSE, R_BARE_MENTION})

#: A declaration that meant a path and named one this repository does not have. Actionable.
DEFECT_CODES = frozenset({R_MISPLACED, R_ABSOLUTE, R_PARENT_RELATIVE})

#: Resolves outside this repository on purpose. Correct as written.
BY_DESIGN_CODES = frozenset({R_PLACEHOLDER})

_GLOB_CHARS = "*?[{"

# `registry.py:1267`, `dev-manager/SKILL.md:201-204` - a citation, not a different file.
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")

# `<data_root>/projects/x/status/registry.md` - resolves outside the repository by design.
_PLACEHOLDER_RE = re.compile(r"<[^>]*>")

# The `(new)` marker, when a spec puts it inside the backticks rather than after them.
_NEW_MARKER_RE = re.compile(r"\s*\((?:new|NEW|New)\)\s*$")

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:/")


class GitError(RuntimeError):
    """Git could not answer. Always a malformed invocation, never a finding."""


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo` by EXPLICIT path, and raise rather than return an empty answer.

    `-C` is not decoration: a fleet lane's working directory is the launch directory, which is
    not a repository. And an empty stdout from a failed `git diff` is indistinguishable from a
    commit that touched nothing - which would report every declared entry over-declared and
    nothing undeclared, the most confident wrong answer this module can give.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {repo}: {(proc.stderr or '').strip()}")
    return proc.stdout


def range_expr(base: str, head: str) -> str:
    """The exact range expression git is handed. One place builds it; the report quotes it.

    `render` used to print `{base}..{head}` from an f-string of its own, which was a true
    description of the comparison only for as long as the two happened to agree. Deriving both
    from here means the report cannot name a range other than the one that actually ran.
    """
    return f"{base}...{head}"


def range_commits(repo: Path, base: str, head: str) -> tuple[int, int]:
    """`(commits only in base, commits only in head)` for `base...head`. One invocation.

    This is what decides whether the range can be asked the question at all. `head` only in
    `base` - the right-hand count zero - means `head` is an ancestor of `base`, so the range
    head side holds no commits and its empty diff answers nothing (gh#316).

    ASKED THIS WAY, NOT `merge-base --is-ancestor`, for a mechanical reason. `--is-ancestor`
    returns its ANSWER in the exit code with empty stdout, and `_git` raises on any non-zero
    return - so the ordinary "no, it is not an ancestor" would arrive as a `GitError` and
    `main` would report a healthy range as a malformed invocation. `rev-list --count` exits 0
    for every resolvable pair and puts the answer on stdout, so the existing wrapper stays the
    only place git failures are turned into errors: an unresolvable ref still exits 128 here
    and still becomes the `GitError` it always was.

    BOTH counts, from `--left-right`, because zero on the right has two shapes with two
    different repairs: the refs handed in the wrong way round, and `base` and `head` naming one
    commit (git counts a commit as its own ancestor). One invocation tells them apart; a second
    `rev-parse` to compare shas would be a second subprocess and a second thing to go stale.

    Unrelated histories are deliberately NOT answered here - they have commits on both sides,
    so this passes them through and the three-dot diff still raises, which is the loud failure
    `touched_paths` documents and the right one for two histories with no merge base.
    """
    out = _git(repo, "rev-list", "--count", "--left-right", range_expr(base, head))
    parts = out.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise GitError(f"git rev-list --count --left-right {range_expr(base, head)} in {repo} "
                       f"returned {out.strip()!r}, which is not two counts")
    return int(parts[0]), int(parts[1])


def touched_paths(repo: Path, base: str, head: str) -> tuple[str, ...]:
    """Repo-relative paths the range `base...head` touched, sorted and deduped.

    THREE dots, and that is the whole of gh#263. This runs `git diff`, where `A..B` is a plain
    TIP-to-TIP comparison and `A...B` diffs from the MERGE BASE - so for diff it is the TWO-dot
    form that drifts. (`git log` and `rev-list` invert that; this module runs neither, so the
    log rule must not be carried over here and the fix must not be "corrected" back on the
    strength of it.) With two dots, `--base origin/master --head <branch>` reported every file
    master had gained since the branch left as though the branch had touched it - and a checker
    whose verdict moves with unrelated drift is the defect it was built to detect, one level up.

    Three dots rather than an explicit `git merge-base` call: git resolves the base inside the
    single invocation, so there is no second subprocess to fail and no window in which `base`
    can advance between resolving it and diffing against it. The cost is that two histories
    with no merge base now raise instead of quietly returning a tip comparison, which is right
    - there is no range there to name.

    `--commit` IS UNAFFECTED, and this is the reason to leave it alone: `commit_range` returns
    `(sha^, sha)`, and `merge-base(sha^, sha) == sha^` because sha descends from its first
    parent, a merge commit included. `sha^...sha` is the same set as `sha^..sha`.

    Two dots did not only over-report, either, and the docstring should not claim it did. A
    path the branch really did edit VANISHES from a tip comparison when the base independently
    reached identical bytes for it - an upstream merge of the same edit, a cherry-pick, a
    revert. That direction is rarer and is the dangerous one: a missed overlap is exactly what
    this instrument exists to prevent.

    `--no-renames` on purpose. With rename detection a moved file reports only its NEW name,
    so a spec that declared the old path reads as over-declared and the move reads as one
    undeclared add - two wrong findings from one true event. Split into a delete and an add,
    both paths appear, and both halves of the move are checked against the declaration, which
    is what a seam actually cares about: the old path was edited too.

    The sort and the dedup are this function's contract, not an observation about git. Today
    git's tree-walk order happens to coincide with a byte sort of the full paths, so a fixture
    driving real git cannot tell a sorted result from an inherited one - which is exactly why
    the caller must not inherit it. Every downstream comparison here is order-sensitive
    (`undeclared` is a tuple a report prints and a test pins), so the order is fixed at this
    seam and nowhere else.
    """
    out = _git(repo, "diff", "--name-only", "--no-renames", range_expr(base, head))
    return tuple(sorted({line.strip() for line in out.splitlines() if line.strip()}))


def commit_range(sha: str) -> tuple[str, str]:
    """`(base, head)` for a single merged commit.

    `sha^` is the FIRST parent, so this is right for a squash commit and for a merge commit
    alike: against its first parent, a merge's diff is exactly what the merge brought in.
    """
    return f"{sha}^", sha


class AmbiguousEpicBranch(RuntimeError):
    """Two or more branches carry one epic's prefix, so no answer here is a measurement.

    RAISED, not returned as `None`, and the two are deliberately not the same outcome. `None`
    already means "no branch yet, read the declaration instead", which is a correct and common
    state for an unclaimed row. Ambiguity is neither: the branch exists, more than one candidate
    is plausible, and picking either produces a seam that LOOKS measured. Folding it into `None`
    would send a caller quietly back to the declared list and hide the one case where the repo's
    naming has drifted far enough that a human has to look.
    """


#: `E-0734-gh293-live-seam` -> `0734`; the legacy bare `E-0220` -> `0220`. Digits are taken
#: VERBATIM - see `epic_branch_prefix` for why they are never re-padded or stripped.
_EPIC_ID_RE = re.compile(r"^E-(\d+)(?:-|$)", re.IGNORECASE)


def epic_branch_prefix(epic_id: str) -> str:
    """`E-0734-gh293-live-seam` -> `e0734`. The branch-name prefix the fleet actually writes.

    The convention this relies on is published in `docs/REGISTRY.md` beside the claim contract:
    a claimed row's branch is `e<NNNN>-<short>`, where `<NNNN>` is the digits of the row's own
    id. This function is the one place that derivation lives, so a test can pin the claim rather
    than re-typing the string beside it.

    THE DIGITS ARE VERBATIM - not re-padded, not zero-stripped - and that is a deliberate
    narrowing rather than an oversight. This repository carries both `e0263-two-dot-range` and
    `e263-land`, and both `e228-land` and `e228-ground-an-escalation`. Normalising `0263` and
    `263` onto one another would make an epic-to-branch question ambiguous in cases that are not
    ambiguous today, and would do it in the direction that hurts: a raise where a clean answer
    exists. An id whose padding does not match its branch simply resolves to nothing, the caller
    falls back to the declared list, and that is exactly the behaviour that already ships.

    Raises `ValueError` on a string that is not an epic id. A caller that cannot name the epic
    has no question to ask here, and returning a prefix like `e` would match most of the board.
    """
    m = _EPIC_ID_RE.match(epic_id.strip())
    if not m:
        raise ValueError(
            f"{epic_id!r} is not an epic id; expected E-<NNNN>-<short-name> or a legacy bare "
            "E-NNNN"
        )
    return f"e{m.group(1)}"


def resolve_epic_branch(repo: Path, epic_id: str) -> str | None:
    """The FULL refname of `epic_id`'s branch in `repo`, or `None` when it has none yet.

    `None` IS A THIRD ANSWER AND CALLERS MUST TREAT IT AS ONE. "No branch" and "a branch that
    touched nothing" are opposite facts about a seam: the first says the work has not started,
    so the declaration is all there is to read; the second says the work started and moved
    nothing, so the declaration is over-declared. An empty tuple cannot carry that difference,
    which is why nothing here ever returns one to mean "absent".

    LOCAL AND REMOTE REFS BOTH, deduped on the branch name. A lane building in a worktree off
    the shared checkout writes its commits into that checkout's own ref store, so its branch is
    local; a lane on another machine reaches this repository only as `refs/remotes/<remote>/...`.
    Reading one side alone would report half the fleet as unstarted.

    WHEN ONE NAME EXISTS ON BOTH SIDES, THE LOCAL REF WINS, and the reason is the fleet's
    topology rather than a general rule: the builders here are worktrees off one checkout, so
    the local ref is the one their newest commits are on and a remote ref lags it by a push. The
    residual is stated rather than solved - if the two have genuinely diverged (a lane pushed
    from a different clone), this reports the local side only and does not merge the ranges.

    A full refname is returned rather than a short one so the answer says WHICH side won and
    cannot later collide with a tag of the same name.
    """
    prefix = epic_branch_prefix(epic_id)
    candidates: dict[str, str] = {}
    for line in _git(repo, "for-each-ref", "--format=%(refname)",
                     "refs/heads", "refs/remotes").splitlines():
        ref = line.strip()
        parts = ref.split("/")
        if ref.startswith("refs/heads/"):
            name = "/".join(parts[2:])
        elif ref.startswith("refs/remotes/") and len(parts) > 3:
            # refs/remotes/<remote>/<name> - a remote name never contains a slash, a branch
            # name may, so everything past the remote is the branch.
            name = "/".join(parts[3:])
        else:
            continue
        if not name or not (name == prefix or name.startswith(prefix + "-")):
            continue
        # Heads are walked before remotes by `for-each-ref`'s own argument order, so the first
        # writer of a name is the local ref and `setdefault` is what makes local win.
        candidates.setdefault(name, ref)

    if not candidates:
        return None
    if len(candidates) > 1:
        raise AmbiguousEpicBranch(
            f"{epic_id} matches {len(candidates)} branches in {repo}: "
            + ", ".join(sorted(candidates)) + " - refusing to guess which one is the epic's"
        )
    return next(iter(candidates.values()))


def epic_touched_paths(repo: Path, base: str, epic_id: str) -> tuple[str, ...] | None:
    """What `epic_id`'s branch has touched off `base`, or `None` when it has no branch.

    THIS IS THE LIVE SEAM, and it exists because the declaration is not one (gh#293). A spec's
    `## Files` section is a prediction written before the work; the branch is the work. One
    measured epic here declared seven paths and touched twelve, and two of the five it never
    declared had been graded FREE in a scheduling decision twenty minutes earlier - a
    disjointness proof that was confidently wrong about exactly the paths it was asked about.

    So a CLAIMED row's seam is read from here, and an UNCLAIMED row's from its declaration,
    because an unclaimed row has no branch and there is nothing else to read. `None` is the
    seam between those two worlds: it says the branch is absent, never that it was empty, and a
    caller that cannot tell those apart would read an unstarted epic as one that touches
    nothing and publish straight through its seam.

    Propagates `AmbiguousEpicBranch` rather than degrading to `None` - see that exception.
    """
    branch = resolve_epic_branch(repo, epic_id)
    if branch is None:
        return None
    return touched_paths(repo, base, branch)


def _tree(repo: Path, rev: str) -> tuple[frozenset[str], frozenset[str]]:
    """`(files, directories)` at `rev`. Directories are derived from the file list.

    Grounding classification in the tree rather than in the shape of the string is what keeps
    `harness/` (a directory, written without a trailing slash) apart from `registry.py` (a
    bare mention that names no repo-relative path at all).
    """
    files = frozenset(
        line.strip() for line in _git(repo, "ls-tree", "-r", "--name-only", rev).splitlines()
        if line.strip()
    )
    dirs = set()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return files, frozenset(dirs)


def _normalise(raw: str) -> str:
    """Separator, a leading `./`, an inline `(new)` marker, and a `:line` citation suffix.

    Nothing else. In particular this does NOT strip leading dots as a character set: that
    would turn `../shared/x.py` into `shared/x.py` and silently equate a parent-relative path
    with a repo-relative one.
    """
    p = _NEW_MARKER_RE.sub("", raw.strip().replace(chr(92), "/")).strip()
    while p.startswith("./"):
        p = p[2:]
    return _LINE_SUFFIX_RE.sub("", p).strip()


@functools.lru_cache(maxsize=512)
def _glob_re(pattern: str) -> re.Pattern[str]:
    """Compile a declared glob to an explicit, platform-independent regex.

    Deliberately NOT `fnmatch`: `fnmatch.fnmatch` folds case on Windows only, and its `*`
    crosses `/`. Both make the answer depend on where the tool ran, which is the one thing a
    detector may not do. See the module docstring.

    `*` and `?` stop at a path separator. `**` crosses one; `**/` additionally matches zero
    directories, so `skills/**/SKILL.md` covers `skills/SKILL.md`. `[...]` is a character
    class with `!` negation. `{a,b}` is a flat alternation - nested braces are not supported
    and are matched literally, because no spec here writes one and guessing is worse than
    leaving the entry to fall out as unresolvable.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i + 1:i + 2] == "*":
                if pattern[i + 2:i + 3] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if pattern[j:j + 1] in ("!", "^"):
                j += 1
            if pattern[j:j + 1] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:                       # unterminated - a literal bracket
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1:j]
                body = ("^" + body[1:]) if body.startswith("!") else body
                out.append("[" + body.replace(chr(92), chr(92) * 2) + "]")
                i = j + 1
        elif c == "{":
            j = pattern.find("}", i)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                alts = pattern[i + 1:j].split(",")
                out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


@dataclass(frozen=True)
class DeclaredEntry:
    """One entry from `## Files`, classified, with what it turned out to cover.

    `raw` is kept verbatim so a report can quote the spec rather than paraphrase it - the
    reader has to be able to find the line to fix it. `code` is the machine-readable half of
    `reason`: the report groups by it so parser noise never shares a list with a real defect.
    """

    raw: str
    path: str
    kind: str
    reason: str = ""
    covered: tuple[str, ...] = ()
    code: str = ""

    @property
    def decidable(self) -> bool:
        """Whether "was this entry complete?" is a question with an answer."""
        return self.kind == FILE

    @property
    def is_noise(self) -> bool:
        """A parser artefact rather than something a spec author can repair."""
        return self.code in NOISE_CODES

    @property
    def is_spec_defect(self) -> bool:
        """A declaration that meant a path here and named one this repository does not have."""
        return self.code in DEFECT_CODES


@dataclass(frozen=True)
class MisPathed:
    """One typo reported once: the string the spec wrote, and the path the range touched.

    Held apart from both deltas. Calling it undeclared blames the builder for a path the spec
    did declare; calling it merely unresolvable loses that the real file went unseamed.
    """

    declared: str
    actual: str
    entry: DeclaredEntry


def classify(raw: str, files: frozenset[str], dirs: frozenset[str],
             touched: tuple[str, ...], rev: str) -> DeclaredEntry:
    """Place one declared entry in the tree, or say why it could not be placed.

    Every branch that can be grounded in the tree IS grounded in the tree. The trailing-slash
    case used to be the exception - any string ending in `/` was taken on its word as a
    directory - and it reported five directories that exist nowhere in this repository as
    OVER-DECLARED. "You claimed a seam you never used" and "you named something that is not
    there" are different repairs, so a trailing slash now has to find a directory too.
    """
    p = _normalise(raw)
    if not p:
        return DeclaredEntry(raw, "", UNRESOLVABLE,
                             "empty once the citation markers are removed", code=R_EMPTY)
    if not any(c.isalnum() for c in p):
        # ``reap`/`stand-down`` yields the bare "/" between the two spans:
        # the borrowed parser reads a separator as a pathlike. Real, and observed on E-0220.
        return DeclaredEntry(raw, p, UNRESOLVABLE,
                             "no path characters - a separator caught between two adjacent "
                             "inline-code spans, not a declared file", code=R_SEPARATOR)
    if _PLACEHOLDER_RE.search(p):
        return DeclaredEntry(raw, p, UNRESOLVABLE,
                             "carries a <placeholder> and resolves outside this repository",
                             code=R_PLACEHOLDER)
    if any(c.isspace() for c in p) and "/" not in p and p not in files and p not in touched:
        # An entire prose sentence promoted by the borrowed regex, which fires on any
        # backticked token carrying a `.`. Observed once in the corpus. The tree check is what
        # keeps a real repo-root file whose name contains a space out of this branch.
        return DeclaredEntry(raw, p, UNRESOLVABLE,
                             "prose, not a path - it carries whitespace and no separator",
                             code=R_PROSE)
    if p.startswith("/") or p.startswith("~") or _WINDOWS_ABS_RE.match(p):
        return DeclaredEntry(raw, p, UNRESOLVABLE, "absolute path, not repo-relative",
                             code=R_ABSOLUTE)
    if p.startswith("../"):
        return DeclaredEntry(raw, p, UNRESOLVABLE, "parent-relative, resolves above the repo root",
                             code=R_PARENT_RELATIVE)
    if any(c in p for c in _GLOB_CHARS):
        return DeclaredEntry(raw, p, PATTERN,
                             "glob - bounds a region; completeness cannot be decided for it")
    stem = p.rstrip("/")
    if stem in dirs or any(t.startswith(stem + "/") for t in touched):
        return DeclaredEntry(raw, stem + "/", PATTERN,
                             "directory - bounds a region; completeness cannot be decided for it")
    if p in files or p in touched:
        return DeclaredEntry(raw, p, FILE)
    if p.endswith("/"):
        return DeclaredEntry(raw, p, UNRESOLVABLE,
                             f"names no directory in the tree at {rev} and the range never "
                             "touched anything under it", code=R_MISPLACED)
    if "/" not in p:
        return DeclaredEntry(raw, p, UNRESOLVABLE,
                             "a bare name with no separator - a module mentioned in prose, not "
                             "a repo-relative path", code=R_BARE_MENTION)
    return DeclaredEntry(raw, p, UNRESOLVABLE,
                         f"names no path in the tree at {rev} and the range never touched it",
                         code=R_MISPLACED)


def _covers(entry: DeclaredEntry, path: str) -> bool:
    if entry.kind == FILE:
        return path == entry.path
    if entry.kind == PATTERN:
        if any(c in entry.path for c in _GLOB_CHARS):
            return bool(_glob_re(entry.path).match(path))
        return path.startswith(entry.path)
    return False


def _basename(p: str) -> str:
    return p.rstrip("/").rsplit("/", 1)[-1]


def _mis_pathed(undeclared: tuple[str, ...],
                unresolvable: tuple[DeclaredEntry, ...]) -> tuple[MisPathed, ...]:
    """Pair each undeclared path with the mis-typed declaration that meant it, where unique.

    UNIQUE on both sides. A basename matching two undeclared paths, or two misplaced entries,
    is not one typo - it is a guess, and a guess here would erase a real undeclared path.
    Only the `misplaced` code is eligible: a `<placeholder>` entry is correct as written and
    an absolute path is a different repair, so neither may absorb a touched file.
    """
    candidates = [e for e in unresolvable if e.code == R_MISPLACED]
    pairs = []
    for base in {_basename(p) for p in undeclared}:
        hits = [p for p in undeclared if _basename(p) == base]
        decls = [e for e in candidates if _basename(e.path) == base]
        if len(hits) == 1 and len(decls) == 1:
            pairs.append(MisPathed(declared=decls[0].raw, actual=hits[0], entry=decls[0]))
    return tuple(sorted(pairs, key=lambda m: m.actual))


@dataclass(frozen=True)
class FilesDelta:
    """The comparison. Every list is evidence; none of them is a verdict."""

    spec: str
    base: str
    head: str
    section_present: bool
    declaration_usable: bool
    #: False when the range's HEAD SIDE holds no commits, so an empty touched set answers
    #: nothing (gh#316). Not the whole range: for `master...feature` with feature an ancestor
    #: the left/right counts are `1 0` - one commit, on the base side, which the diff never reads.
    #: Defaults True, which is also the right answer for `compare_paths`: a hand-built union of
    #: per-commit diffs has no single range whose usability could be in question.
    range_usable: bool = True
    #: Which shape of unusable range it was - `base` and `head` are one commit, rather than the
    #: refs being the wrong way round. Read only when `range_usable` is False.
    same_commit: bool = False
    entries: tuple[DeclaredEntry, ...] = ()
    touched: tuple[str, ...] = ()
    undeclared: tuple[str, ...] = ()
    over_declared: tuple[DeclaredEntry, ...] = ()
    unresolvable: tuple[DeclaredEntry, ...] = ()
    patterns_used: tuple[DeclaredEntry, ...] = ()
    mis_pathed: tuple[MisPathed, ...] = ()
    duplicate_entries: tuple[str, ...] = ()

    @property
    def declared_count(self) -> int:
        """DISTINCT entries. A spec listing one path twice declared it once."""
        return len(self.entries)

    @property
    def noise(self) -> tuple[DeclaredEntry, ...]:
        return tuple(e for e in self.unresolvable if e.is_noise)

    @property
    def spec_defects(self) -> tuple[DeclaredEntry, ...]:
        return tuple(e for e in self.unresolvable if e.is_spec_defect)

    @property
    def out_of_repo(self) -> tuple[DeclaredEntry, ...]:
        return tuple(e for e in self.unresolvable if e.code in BY_DESIGN_CODES)

    @property
    def clean(self) -> bool:
        """Both deltas empty AND every entry decidable.

        A run with a live pattern entry is NOT clean even with no undeclared path: the pattern
        is why there is nothing to report, and calling that a clean declaration would launder
        the one shape whose completeness was never checkable.
        """
        return (
            self.declaration_usable
            and self.range_usable
            and not self.undeclared
            and not self.over_declared
            and not self.unresolvable
            and not self.patterns_used
            and not self.mis_pathed
        )

    def as_dict(self) -> dict:
        """The machine surface, and the one the retroactive sweeps were quoted from.

        Every count a reader can take from `render` is derivable from a list here, so the two
        surfaces cannot disagree about what was FOUND.

        `range` IS THE COMPARISON, AND IT IS AUTHORITATIVE HERE (gh#317). Both surfaces now name
        the same expression through `range_expr`, so neither a reader nor a sweep has to rebuild
        it. That reconstruction - `f"{base}..{head}"` - is the two-dot form gh#263 removed, and
        until this field existed it was the natural thing for a caller to write, one level out,
        where nothing in this module could observe it. A fix single-sourced into one surface
        leaves the other free to rebuild the bug.

        `base` and `head` STAY, as provenance rather than as an invitation. They are what the
        run was given, `range` is what it compared, and a test pins `range == range_expr(base,
        head)` so the three can never drift apart. Dropping them would break every consumer that
        already reads this payload - the retroactive sweeps among them - to remove a hazard that
        carrying the authoritative value removes anyway.

        A SWEEP FILTERS ON `range_usable`, and has to. An empty `touched` used to be the only
        visible trace of a range that could not answer, and it is identical to the trace of a
        range that honestly touched nothing - two runs whose payloads differed in the `head`
        string alone. The flag is the field that separates them.
        """
        return {
            "spec": self.spec,
            "base": self.base,
            "head": self.head,
            "range": range_expr(self.base, self.head),
            "section_present": self.section_present,
            "declaration_usable": self.declaration_usable,
            "range_usable": self.range_usable,
            "same_commit": self.same_commit,
            "declared_count": self.declared_count,
            "duplicate_entries": list(self.duplicate_entries),
            "touched": list(self.touched),
            "undeclared": list(self.undeclared),
            "over_declared": [e.path for e in self.over_declared],
            "mis_pathed": [
                {"declared": m.declared, "actual": m.actual} for m in self.mis_pathed
            ],
            "unresolvable": [
                {"raw": e.raw, "reason": e.reason, "code": e.code} for e in self.unresolvable
            ],
            "spec_defects": [e.raw for e in self.spec_defects],
            "noise": [e.raw for e in self.noise],
            "out_of_repo": [e.raw for e in self.out_of_repo],
            "patterns_used": [
                {"path": e.path, "covered": list(e.covered)} for e in self.patterns_used
            ],
            "clean": self.clean,
        }


def compare(repo: Path, spec_text: str, base: str, head: str, *, spec: str = "") -> FilesDelta:
    """Read the declaration, read the range `base...head`, and state where they disagree.

    THE RANGE CHECK LIVES HERE, because this is the only place that turns a range into a
    `FilesDelta`. `touched_paths` also holds the range and is deliberately NOT guarded: it
    answers the narrower question of what the diff listed, and `()` is a truthful answer to
    that one. What this function has and that one does not is
    the range. `compare_paths` receives the touched set as DATA - it is called with a synthetic
    `base="union"` for the multi-commit case - so by the time an empty tuple reaches it, "the
    range touched nothing" and "the range could not answer" are already the same value.
    """
    behind, ahead = range_commits(repo, base, head)
    if ahead == 0:
        # gh#316. No commits on the head side, so the three-dot diff is empty by construction
        # and running it would only manufacture the over-declared list. Every range-derived
        # field is emptied because nothing was compared, not because nothing was found.
        #
        # gh#347. The declaration is still classified against an empty touched set, because
        # the count, the duplicates and the absolute / parent-relative / placeholder / noise
        # classes are parses of the spec that no range changes. `misplaced` is the exception:
        # its reason reads "and the range never touched it", which on a range that touched
        # nothing is the gh#316 false confidence again. It stays counted in `entries` and is
        # classified nowhere.
        d = compare_paths(repo, spec_text, (), head, spec=spec, base=base)
        return replace(
            d, range_usable=False, same_commit=behind == 0,
            undeclared=(), over_declared=(), patterns_used=(), mis_pathed=(),
            unresolvable=tuple(e for e in d.unresolvable if e.code != R_MISPLACED),
        )
    return compare_paths(repo, spec_text, touched_paths(repo, base, head), head,
                         spec=spec, base=base)


def compare_paths(repo: Path, spec_text: str, touched: tuple[str, ...], rev: str, *,
                  spec: str = "", base: str = "") -> FilesDelta:
    """The comparison itself, given the touched set as data.

    Split from `compare` so an epic that landed across SEVERAL merged commits can be measured
    as one epic - the union of what its commits touched, against one declaration. Per-commit
    runs would report every path the other commits touched as over-declared, which is a
    property of the split, not of the spec.

    Reads the repository and never writes it, so the report and any later gate built on it
    cannot disagree about what was found.
    """
    fs = files_section(spec_text)
    head = rev
    if not fs.seam_checkable:
        # The third outcome, not an epic that under-declared everything. `present` is kept
        # because "no section" and "a section saying nothing placeable" are different repairs.
        return FilesDelta(spec, base, head, fs.present, False, touched=touched)

    distinct: list[str] = []
    duplicates: list[str] = []
    for raw in fs.paths:
        (duplicates if raw in distinct else distinct).append(raw)

    files, dirs = _tree(repo, head)
    entries = []
    for raw in distinct:
        e = classify(raw, files, dirs, touched, head)
        entries.append(replace(e, covered=tuple(p for p in touched if _covers(e, p))))

    undeclared = tuple(p for p in touched if not any(_covers(e, p) for e in entries))
    unresolved = tuple(e for e in entries if e.kind == UNRESOLVABLE)
    mis = _mis_pathed(undeclared, unresolved)
    # Raw strings are unique after the dedupe above, so they identify an entry exactly.
    paired_paths = {m.actual for m in mis}
    paired_raws = {m.entry.raw for m in mis}

    return FilesDelta(
        spec, base, head, True, True,
        entries=tuple(entries),
        touched=touched,
        undeclared=tuple(p for p in undeclared if p not in paired_paths),
        over_declared=tuple(e for e in entries if e.kind in (FILE, PATTERN) and not e.covered),
        unresolvable=tuple(e for e in unresolved if e.raw not in paired_raws),
        patterns_used=tuple(e for e in entries if e.kind == PATTERN and e.covered),
        mis_pathed=mis,
        duplicate_entries=tuple(duplicates),
    )


def _spec_side_blocks(d: FilesDelta, block) -> None:
    """The three unresolvable classes, shared by the full report and the unusable-range one."""
    block(f"UNRESOLVABLE / SPEC DEFECT ({len(d.spec_defects)}) - meant a path here, named one "
          "that is not here:",
          [f"{e.raw}  <- {e.reason}" for e in d.spec_defects])
    block(f"UNRESOLVABLE / PARSER NOISE ({len(d.noise)}) - the section parser promoted it; "
          "nothing to repair in the spec:",
          [f"{e.raw}  <- {e.reason}" for e in d.noise])
    block(f"UNRESOLVABLE / OUT OF REPO BY DESIGN ({len(d.out_of_repo)}) - correct as written:",
          [f"{e.raw}  <- {e.reason}" for e in d.out_of_repo])


def render(d: FilesDelta) -> list[str]:
    """The human report. Each delta is named and never merged with the other."""
    # `touched` is NOT MEASURED on an unusable range - the early return in `compare` skips the
    # diff entirely - so printing `0 path(s)` there would be an empty set rendered as a count,
    # which is precisely the confusion this guard exists to end, one line above the banner that
    # ends it. The unusable-DECLARATION path is different and keeps its number: `compare_paths`
    # really does run the diff and passes a real `touched` in.
    measured = f"touched: {len(d.touched)} path(s)" if d.range_usable else "touched: not measured"
    lines = [f"# files declared vs files touched - {d.spec or '(spec)'}",
             f"range: {range_expr(d.base, d.head)}   {measured}"]
    if not d.range_usable:
        # Before the declaration guard on purpose: with no commits in the range, nothing was
        # compared whatever the spec said, so naming the declaration first would report the
        # weaker of the two facts and leave the reader believing a comparison happened.
        lines.append(
            "NO USABLE RANGE: "
            + ("`base` and `head` name the same commit, so the range holds no commits at all"
               if d.same_commit else
               "`head` is an ancestor of `base` - check whether the two refs are the wrong way "
               "round - so the head side of the range holds no commits")
            + ", and its empty diff answers nothing; this is not the same as a range that "
              "touched nothing"
        )
        if not d.declaration_usable:
            return lines

    if not d.declaration_usable:
        lines.append(
            "NO USABLE DECLARATION: "
            + ("the `## Files` section names no placeable path, so there is nothing to compare "
               "against - this is not the same as an epic that touched nothing"
               if d.section_present else "the spec has no `## Files` section at all")
        )
        return lines

    lines.append(
        f"declared: {d.declared_count} distinct entr{'y' if d.declared_count == 1 else 'ies'}"
        + (f" ({len(d.duplicate_entries)} duplicate raw entr"
           f"{'y' if len(d.duplicate_entries) == 1 else 'ies'} deduped: "
           f"{', '.join(d.duplicate_entries)})" if d.duplicate_entries else "")
    )

    def _block(title: str, items) -> None:
        lines.append(title)
        if items:
            lines.extend(f"  {i}" for i in items)
        else:
            lines.append("  none")

    if not d.range_usable:
        # gh#347. The count and the three blocks below are parses of the spec, so they hold
        # with no range at all; every block a range would have fed is left out, not printed
        # as `none`, because `none` there would be the unmeasured-empty-set confusion again.
        # The withheld misplaced entries are counted, not listed, so `declared: N` above still
        # adds up against the blocks below without printing a range claim.
        withheld = sum(1 for e in d.entries if e.code == R_MISPLACED)
        lines.append(
            "the declaration findings below are read from the spec alone and still hold; the "
            "range-derived findings are not reported, and neither is an entry whose path has a "
            "separator but is not in the tree, since whether the range touched it is the half "
            "this run could not measure"
            + (f" ({withheld} withheld: a path with a separator that is not in the tree)"
               if withheld else "")
        )
        _spec_side_blocks(d, _block)
        return lines

    _block(f"UNDECLARED ({len(d.undeclared)}) - touched, covered by no declared entry"
           + (f", excluding {len(d.mis_pathed)} reported as MIS-PATHED below:"
              if d.mis_pathed else ":"),
           d.undeclared)
    _block(f"OVER-DECLARED ({len(d.over_declared)}) - declared, never touched:",
           [e.path for e in d.over_declared])
    _block(f"MIS-PATHED ({len(d.mis_pathed)}) - one typo, one finding: the declaration named a "
           "path this repo does not have, and the file it meant was touched:",
           [f"{m.declared}  ->  {m.actual}" for m in d.mis_pathed])
    _spec_side_blocks(d, _block)
    _block(f"NOT DECIDABLE ({len(d.patterns_used)}) - directory/glob entries that absorbed "
           "paths; completeness cannot be checked for these even in principle:",
           [f"{e.path}  absorbed {len(e.covered)}: {', '.join(e.covered)}"
            for e in d.patterns_used])
    return lines


def main(argv=None) -> int:
    """The CLI. Report only.

    EXIT CODE:

    * ``0`` - the comparison ran, whatever it found. Findings do not redden this, by design:
      see the module docstring on evidence versus verdict. A range that could not answer
      (gh#316) is a 0 as well: git resolved it and exited 0, so it is a fact about the inputs
      this run discovered, not a malformed argv, and it is reported the way an unusable
      declaration is.
    * ``2`` - a malformed invocation: no repo, no spec, an unreadable spec, or a range git
      could not resolve.
    """
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(prog="python -m agentflow.files_vs_diff",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--spec", required=True, help="path to the epic's spec markdown")
    ap.add_argument("--repo", help="git checkout to read the range from (default: the resolved "
                                   "project's canonical_repo.local_checkout)")
    ap.add_argument("--commit", help="one merged commit; the range is <sha>^...<sha>")
    ap.add_argument("--base", help="range start (with --head)")
    ap.add_argument("--head", help="range end (with --base)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if bool(args.commit) == bool(args.base and args.head):
        print("give either --commit, or both --base and --head")
        return 2

    if args.repo:
        repo = Path(args.repo).expanduser()
    else:
        from .config import ConfigError, load_project, resolve_project_name
        # RESOLVE the name; never hand `None` to `load_project`, which interpolates its argument
        # straight into `projects/<name>.md` and so goes looking for `projects/None.md`. This is
        # the ONLY invocation the docs publish (docs/REGISTRY.md), so that spelling closed the
        # tool's only route into use while `$AGENTFLOW_PROJECT` sat set and unread beside it.
        # `resolve_project_name()` with no argument is the SessionStart hook's own resolution.
        name = resolve_project_name()
        if name is None:
            # Environment-only config is legitimate elsewhere, but this tool needs a project to
            # find a checkout. Say which of the three ways out the caller has, rather than
            # failing later against a path built from the word "None".
            print(
                "could not resolve a project: pass --repo, set $AGENTFLOW_PROJECT, "
                "or keep exactly one projects/<name>.md"
            )
            return 2
        try:
            repo = Path(load_project(name).canonical_repo.local_checkout).expanduser()
        except ConfigError as exc:
            print(f"could not resolve project: {exc}")
            return 2
    if not (repo / ".git").exists():
        # Catches the near-miss, not just the absent path: a SUBDIRECTORY of a real checkout
        # is where git would otherwise answer happily about the wrong root.
        print(f"not a git checkout: {repo}")
        return 2

    spec_path = Path(args.spec).expanduser()
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read spec: {exc}")
        return 2

    base, head = commit_range(args.commit) if args.commit else (args.base, args.head)
    try:
        d = compare(repo, text, base, head, spec=spec_path.name)
    except GitError as exc:
        print(str(exc))
        return 2

    print(json.dumps(d.as_dict(), indent=2) if args.json else "\n".join(render(d)))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
