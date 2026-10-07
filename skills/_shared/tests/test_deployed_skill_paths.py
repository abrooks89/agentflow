"""A path a deployed skill tells a reader to open must resolve once deployed.

WHY THIS FILE EXISTS (gh#59). A skill is shipped by copying its directory to `~/.claude/skills/`.
Anything outside that directory — the repo root's `templates/`, `docs/`, `harness/` — does not come
with it. Several skills nonetheless instruct a reader to open such a file, so an installed user
follows an instruction to read something they do not have. It resolves fine for us, because every
lane on this machine can also see the checkout; it fails for exactly the person who installed the
suite and nothing else.

WHY THE FAILING SET IS COMPUTED AND NOT LISTED. gh#59's own body warns: *"Worth grepping the full
set rather than fixing the two known ones — that is the sweep-by-filename failure this repo already
has a lesson about."* It was right to. The set was believed to be small and enumerable; each sweep
that followed found shapes the one before it could not see, among them:

* references BELOW `SKILL.md` (`auditor/references/ledger-format.md`), invisible to a
  SKILL.md-only sweep;
* `skills/_vendored/`, which **is** deployed and which nobody had swept — its
  `brainstorming/SKILL.md` is off by one `../`, the same shape as gh#59 itself;
* extensionless executables (`scripts/review-package`), invisible to any regex anchored on a file
  extension;
* one file where the sweep listed a line and stopped one line short of the break beneath it.

So this module walks the corpus and resolves each reference itself. A hand-written inventory would
have shipped with the same blind spots as the sweep that produced it.

THE MODEL, and it is the whole of gh#157. This gate answers ONE question: *of the paths named in a
file that ships, which can a reader who has only `~/.claude/` not open?* So it never asks the
filesystem. It builds a MANIFEST — the set of `claude_root`-relative paths that exist after
`install/provision.py` runs — maps every scanned file to its own deployed path, resolves each token
LEXICALLY (`posixpath.normpath`) against the roots a deployed reader has, and asks whether the
result is in the manifest.

The verdict therefore depends on the DESTINATION, never on the SPELLING. That is not a nicety. The
previous version stat'd `(source.parent / token)` against the checkout, so from `skills/lead-dev/`
the token `../_shared/SCHEMA.md` PASSED — `<repo>/skills/lead-dev/../_shared/SCHEMA.md` is a real
file — while `skills/_shared/SCHEMA.md`, the same destination, correctly failed. Two spellings, one
destination, opposite verdicts, and the passing one was the wrong green: `_shared` is in
`SKILLS_NON_DEPLOY`, so no installed user has it under either name.

Nor could that be repaired by stat'ing a materialised deploy tree. `_deploy_mode_for()` returns
`"symlink"` off Windows, so on a Unix dev box `~/.claude/skills/lead-dev` points back INTO the
checkout and `../_shared/SCHEMA.md` physically resolves at the real deploy target too. `..` on a
filesystem follows that filesystem's parents; lexical normalisation has no parents to follow. A
destination that normalises above `claude_root` is unreachable by construction rather than by luck.

EVERYTHING IS DERIVED FROM THE INSTALLER, for the same reason the failing set is computed:

* WHAT SHIPS — `_deploy_sources` is lifted out of `install/provision.py` and executed as an
  isolated function (see `_installer_deploy_sources`), so what ships is read, not restated. A
  future non-deploying subdirectory, or a FOURTH kind of deployed entry, is covered here with no
  code change. It emits THREE kinds today: skill directories, `commands/*.md` wrappers, and
  `scripts/plan-lint.sh`. This sentence said "exactly two" until gh#171 widened `_READABLE`, which
  is when the omission started to matter — `plan-lint.sh` is `.sh`, so it entered the scanned
  corpus for the first time in that same change, and a docstring claiming it does not ship would
  have been read as the reason it was not scanned. `provision.py` states the real shape at its own
  `_deployed_entries`: of 36 entries, 22 are directories and 14 are files.
* WHAT IS SCANNED — the corpus is those same pairs (gh#156). It used to be `SKILLS_ROOT.iterdir()`,
  which meant the shipped `commands/*.md` wrappers were judged by nothing, while
  `commands/autodoc-init.md` told its reader to open a file.
* WHICH OF THOSE FILES ARE OPENED is `_READABLE`, and it covers markdown/YAML/text prose AND the
  shipped executables — `.py`, `.sh` and extensionless scripts (gh#171). The executables are not
  a rounding error on that list: the extensionless `subagent-driven-development/scripts/*` named
  as this gate's motivating case two paragraphs above were themselves read by nothing until that
  set was widened, so the gate was reasoning about a class of file it did not open. The set is
  not enumerated here — a second copy would drift from the literal;
  `test_the_readable_set_reaches_every_shipped_py_sh_and_extensionless_file` derives the shipped executables from
  the index and holds it. What is still unread is `.js`, `.ts`, `.sql`, `.json`, `.go` and
  `.html`, named in the residual list above `KNOWN_BROKEN`.
* WHAT DOES NOT SHIP — a non-shipping destination is simply one the manifest lacks. The old
  `_nonshipping_roots()` listing of repo-root directories survives only as an EXTRACTOR precision
  aid (is this token a path at all?), never as a verdict.

...AND WHICH FILES EXIST IS DERIVED FROM THE GIT INDEX, which is the same idea one level up. The
installer says what ships; `git ls-files` says what is there at this commit. The corpus used to be
an `rglob` over the working tree, so untracked build artifacts under a shipped skill — an
`.egg-info/SOURCES.txt`, a vendored `node_modules/**/README.md` — were read as though they were
instructions this suite ships. Being gitignored, they did it with `git status` clean and the suite
red, which is the worst signal this repo can emit: nothing to bisect and no way for CI, which
checks out tracked files only, to reproduce it. See `_tracked_paths`; there is deliberately no
fallback to a tree walk, because the fallback IS the bug.

WHAT THIS GATE CAN AND CANNOT DO — stated plainly, because a gate that overstates itself is worse
than none, and this repo has a filed issue that is exactly that mistake:

* It CAN prove that a **relative, literal** path named in a shipped file — backticked, in a
  markdown link, alone inside a fenced block, or in bare prose (gh#144) — resolves under a root a
  deployed reader has, PROVIDED the token's FIRST SEGMENT is a name this gate already knows: a
  deploy root, a directory in the deployed tree, a non-shipping repo-root directory, a
  `SKILLS_NON_DEPLOY` skill, or `..`. Every backing assertion in this file uses such a head,
  because `_is_candidate` cannot see any other kind.
* That head filter is what keeps the gate quiet enough to survive, and it is equally its LARGEST
  SILENCE, so it is stated here rather than left to be discovered. A backticked, extensioned,
  perfectly ordinary broken reference under an unfamiliar head — `runbooks/deploy.md`,
  `playbooks/x.md`, `assets/thing.md` — never reaches candidacy at all, because nothing in this
  suite is called `runbooks/`. Pinned by `test_an_unfamiliar_head_is_the_gates_largest_silence`.
* Its extraction tiers do NOT have the same reach, and the difference is deliberate. A BACKTICKED
  token needs no file extension, because `scripts/review-package` has none. A PROSE or FENCED
  token must end in an extension the installer actually deploys, because prose is full of
  slash-bearing non-paths (`and/or`, `24h/72h`, `PASS/FAIL`, `pro/con`). The noise it removes is
  named rather than tallied: `report/ledger`, `solution/plan` — agentflow
  names skills after ordinary English nouns — and `install/scaffold`, because `install/` is a
  repo-root DIRECTORY, not a skill. Reading source files (gh#171) added
  `report/summarized_at_sha`, two dict keys slash-joined in a code comment: a comment
  alternates identifiers exactly as prose alternates nouns, so the rule earns its keep on the
  widened corpus for the same reason it did on the narrow one.
  `test_the_extension_rule_costs_what_the_docstring_says` recomputes that set against the live
  corpus and asserts it.
* THAT RULE HAS A PRICE, and it was a decision, not a free lunch. The same test partitions what
  else the rule hides into the shapes below, and each is stated here rather than smoothed over:
  upstream-superpowers text inside `skills/_vendored/` — provenance and test-prompt fixtures in
  `systematic-debugging/`, an extensionless directory mention inside a fence in
  `test-driven-development/` — a possessive artifact (`../requesting-code-review/SKILL.md's`), and
  a `file:symbol` code citation (`orchestrator/webhook.py:handle`), which is a win rather than a
  cost. Not one of the hidden destinations is an instruction this suite owns. That is why the
  trade was taken. It was not taken because it cost nothing.
* SO THE RESIDUAL, in full. An extensionless path named only in bare prose or in a fence is
  invisible. So is any token spelled `./...` — its head is `.`, which the head filter above does
  not know, so `./../_shared/SCHEMA.md` is a silence in the gh#59 class exactly as
  `../_shared/SCHEMA.md` was. That is the head filter's doing, not the `./` clause in
  `_is_candidate`'s `startswith` tuple, which is subsumed by it; the assertion in
  `test_an_unfamiliar_head_is_the_gates_largest_silence` says so and says why it matters. It is
  recorded as a named limit rather than filed as a bug. And so is anything under an unfamiliar
  head, above. See `test_bare_prose_noise_is_not_reported` and
  `test_an_unfamiliar_head_is_the_gates_largest_silence`.
* It CANNOT judge whether a reference is an *instruction to open* or a *citation for provenance*.
  That distinction needs a verb, and drawing it wrong in either direction is worse than not drawing
  it — so this gate does not. It asks only "could a reader who tried to open this find it?"
* It deliberately ignores absolute paths, `<placeholder>` paths, URLs, and anything carrying a
  shell or template variable. Those resolve at runtime against values this gate does not have.
  Every one of the four is pinned by an assertion in `test_the_gate_can_actually_fail`; absolute
  paths were the one that was not, until this commit.
* It CANNOT describe what any particular installed machine holds. Deriving the corpus and the
  manifest from the index models an install from a CLEAN CHECKOUT of this commit, and
  `_deploy_entry` in `install/provision.py` does not work that way: it `copytree`s the whole
  working-tree directory (minus `__pycache__`/`*.pyc`), or symlinks it, so a box that has run
  `npm ci` or `pip install -e` genuinely ships its untracked and gitignored files to
  `~/.claude/skills/` too. An installed user can therefore hold files this gate never scanned, and
  can hold a destination this gate would call unresolvable. That narrowing is the trade, and it is
  the right way round rather than a gap to be closed: a verdict that moves with whatever happens to
  be lying in one developer's checkout is not a verdict about the commit, it is not reproducible in
  CI — which checks out tracked files only — and it fails with `git status` clean. Gate the commit.
  Anyone tempted to widen this back to the working tree is asked to read `_tracked_paths`, which
  says what that cost.

The narrowness is the point. A gate that buries a real break under legitimate lines gets
allowlisted into uselessness within a week, and the allowlist is then exactly where the real defect
hides.
"""

from __future__ import annotations

import ast
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from agentflow.tracked_paths import tracked_paths

SKILLS_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = SKILLS_ROOT.parent

#: The git executable, named rather than inlined so a test can point at one that is not installed
#: and prove the loud-failure path without mocking anything this module owns.
_GIT = "git"


def _tracked_paths() -> frozenset[str]:
    """Every path in the GIT INDEX at `SUITE_ROOT`, `SUITE_ROOT`-relative POSIX. The ONLY source.

    THE IMPLEMENTATION MOVED to `agentflow.tracked_paths` (gh#175) and this is now a two-line
    binding of it to this module's own `SUITE_ROOT` and `_GIT`, which the tests below monkeypatch.
    Both must keep being read HERE, at call time, or those three tests would silently stop
    exercising the failure modes they name. The argument for the function is unchanged and lives
    with the code; what follows is why this module cared first.

    THE DEFECT THIS EXISTS TO KILL, because it is invisible in the one place people look. The
    corpus used to be an `rglob` over the working tree, so whatever happened to be lying in the
    checkout was scanned. A `.egg-info/SOURCES.txt` from a local `pip install -e`, and a
    `node_modules/**/README.md` from an `npm ci`, both sit under a SHIPPED skill and both are
    gitignored — so `git status --porcelain` printed nothing while the suite ran red. Measured on
    one commit: the live tree reported offenders that a tracked-only export of that same commit
    did not. CI could never reproduce it, because CI checks out tracked files only.

    A red suite with a clean `git status` is the worst signal this repo can emit: the lane hitting
    it has nothing to diagnose from and the natural next move is to bisect its own work.

    It is the epic's own thesis applied one level up. gh#157 made the VERDICT depend on the
    destination rather than the spelling, by resolving against a manifest of what `provision.py`
    deploys instead of stat'ing the checkout. The CORPUS never got the same treatment. It does now:
    what ships is still read from the installer, but WHICH FILES EXIST is read from the index.
    Same commit, same corpus, same manifest, same verdict — on this box, in CI, on a fresh clone.

    NO FALLBACK. If git cannot answer, this raises. A silent fall back to a tree walk would
    reinstate the bug on exactly the machines where it is hardest to see, and it would do so with
    the suite green, which is how the original latency survived so long.
    """
    return tracked_paths(SUITE_ROOT, git=_GIT)


def _installer_source() -> str:
    return (SUITE_ROOT / "install" / "provision.py").read_text(encoding="utf-8")


def _non_deploy() -> set[str]:
    """The skills/ subdirs provision does NOT ship — READ from the installer, never restated.

    Parsed with `ast`, not imported. Executing an installer to learn a constant is the wrong
    trade twice over: it runs module-level code whose job is to touch the machine, and it failed
    outright here (loading `provision.py` by file location leaves `__module__` unresolvable, so
    its dataclasses raise during class creation). Parsing gets the same fact with no execution.

    A second hand-written copy of this set would drift from the installer, and the drift would
    surface as a gate that is confidently wrong about what a user actually has.
    """
    for node in ast.walk(ast.parse(_installer_source())):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SKILLS_NON_DEPLOY" for t in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError(
        "install/provision.py no longer defines SKILLS_NON_DEPLOY as a literal — this gate reads "
        "the deployable set from there and must be re-pointed rather than left guessing."
    )


def _installer_deploy_sources(tracked: "frozenset[str] | None" = None) -> list[tuple[str, Path]]:
    """`(claude_root-relative key, source)` pairs — the installer's OWN function, executed here.

    gh#156, and the reason it is done this hard way. What ships is not one fact but a SHAPE:
    `skills/<dir>` for every directory except `SKILLS_NON_DEPLOY`, **and `commands/*.md`**. The
    gate used to encode half of it (`SKILLS_ROOT.iterdir()` plus the non-deploy set) and was
    therefore structurally unable to notice the other half — the shipped command files were judged
    by nothing, and the ones that tell their reader to open a path
    (`commands/autodoc-init.md` and `commands/autodoc-update.md`, both naming
    `skills/autodoc-init/references/taxonomy.md`) are now JUDGED, where before they were merely
    unread.

    Restating the shape here would put the same class of bug one edit away: the next kind of thing
    provision learns to deploy would be invisible to this gate exactly as commands/ was. So the
    `_deploy_sources` FunctionDef is lifted out of the parsed installer and compiled on its own,
    with `SKILLS_NON_DEPLOY` supplied from `_non_deploy()`. No module-level installer code runs —
    that is the same trade `_non_deploy` makes, for the same reason.

    `install/provision.py` is READ-ONLY to this epic. This function is how that stays true while
    the derivation gets stronger.

    THE EMITTED PAIRS ARE THEN INTERSECTED WITH THE INDEX, and that is not a second-guessing of the
    installer. `_deploy_sources` calls `skills_dir.iterdir()`, so on a real machine an untracked
    directory dropped under `skills/` becomes a "deployed skill" here and does not exist for anyone
    else — the same working-tree dependence `_tracked_paths` removes one level down. What ships is
    still the installer's answer; whether the source exists at this commit is the index's.

    `tracked` is threaded in by callers that already hold it, purely so one `_deployed()` shells out
    to git ONCE rather than three times. It is not a seam for supplying a different index.
    """
    tracked = _tracked_paths() if tracked is None else tracked
    tree = ast.parse(_installer_source())
    fn = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_deploy_sources"
        ),
        None,
    )
    if fn is None:
        raise AssertionError(
            "install/provision.py no longer defines a top-level `_deploy_sources` — this gate "
            "derives BOTH its corpus and its manifest from that function, and must be re-pointed "
            "rather than left restating a shape the installer has moved on from."
        )
    namespace: dict = {"Path": Path, "SKILLS_NON_DEPLOY": _non_deploy()}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<provision:_deploy_sources>", "exec"),
         namespace)
    return [
        (key, source)
        for key, source in namespace["_deploy_sources"](SUITE_ROOT)
        if _tracked_children(source, tracked) or _index_rel(source) in tracked
    ]


def _index_rel(source: Path) -> str:
    """A deploy source as the index spells it: `SUITE_ROOT`-relative, POSIX, no filesystem asked.

    A RESIDUAL, named rather than guarded because a guard here would be behaviour with no caller.
    This spelling is compared byte-exactly against the index, while `source` came from
    `skills_dir.iterdir()` — the DISK spelling. Under `core.ignorecase=true`, git's Windows
    default, a case-only directory rename can leave the index holding one spelling and the disk the
    other; the two then stop matching and that skill leaves the corpus silently rather than loudly.
    Nothing in this suite is in that state, and a case-only rename git has actually recorded moves
    both spellings together. If one ever does diverge, fold the case at this comparison — do not
    reinstate a filesystem lookup, which is the defect `_tracked_paths` removed.
    """
    return source.relative_to(SUITE_ROOT).as_posix()


def _tracked_children(source: Path, tracked: "frozenset[str]") -> list[str]:
    """The index entries BENEATH a directory source, as paths relative to that source.

    Sorted, so the corpus order is a property of the commit rather than of a directory walk.
    """
    prefix = f"{_index_rel(source)}/"
    return sorted(p[len(prefix):] for p in tracked if p.startswith(prefix))


# WHAT THIS GATE READS: file extensions, plus `""`, which is how `Path.suffix` spells a file that
# has no extension at all. Membership is tested against `Path.suffix` at both call sites in
# `_deployed()`, so `""` is the member that reaches an extensionless script — there is no
# separate predicate for them, and adding one would only be a second place to forget.
#
# THAT EMPTY STRING IS THE WHOLE OF gh#171's FIRST HALF. This set used to be
# `{".md", ".yaml", ".yml", ".txt"}`, so every shipped `.py`, `.sh` and extensionless file was
# extracted from by NOTHING — including
# `_vendored/subagent-driven-development/scripts/{review-package,sdd-workspace,task-brief}`, which
# this module's own docstring cites as its MOTIVATING extensionless case. A gate that reasons
# about a class of file it does not open is the overstatement this file's opening paragraph warns
# against, pointed at itself. The old rationale — "binary/compiled assets are shipped, not
# read" — was retired in gh#170 for describing none of what it actually excluded; the behaviour
# outlived it by one epic, and this is that behaviour catching up.
#
# THE WIDENING WAS MEASURED BEFORE IT WAS TAKEN, because gh#171 priced its yield as "low" and an
# unmeasured estimate is exactly the thing this file keeps having to retract. On the corpus at
# this commit: `.py` alone surfaced six offenders; adding `.sh` and `""` on top surfaced THE SAME
# SIX. So the extensionless half — the half the docstring had been citing as motivating all
# along — cost nothing, and the entire price was `.py`'s: five genuine unrooted citations,
# REPAIRED in the same commit rather than excused, and one naming PATTERN
# (`migrations/<NNNN>_<name>.sql`) reworded into the `<placeholder>` form `_SKIP_SUBSTRINGS`
# already drops for every `.md` in the suite. Nothing was added to `KNOWN_BROKEN`, whose own
# message says an entry is not a fix.
#
# THOSE COUNTS ARE NOT RECORDED HERE AS STANDING FACTS. They describe one commit's corpus and
# nothing recomputes them, which is the only standard this file applies to a number. What IS
# enforced is the SET: `test_the_readable_set_reaches_every_shipped_py_sh_and_extensionless_file` derives the shipped
# executables from the index and reds if this literal is narrowed back.
#
# IT IS STILL NOT "every file a reader reads", and that is stated rather than rationalised.
# Shipped `.js`, `.ts`, `.sql`, `.json`, `.go` and `.html` are read by nothing here. That is a
# remaining silence of the same shape as the one just closed; it is named in the residual list
# above `KNOWN_BROKEN` and deliberately not totalled.
_READABLE = {".md", ".yaml", ".yml", ".txt", ".py", ".sh", ""}

# Test-data trees, excluded from BOTH the scan and the derived directory names.
#
# They ship — so they stay in the MANIFEST, and a reference to one resolves — but their contents
# are not instructions: one of them is literally called `bad-source-path-page.md` and exists to
# carry a path that does not resolve. Scanning them would report a fixture doing its job. Worse,
# letting their subdirectory names into `_asset_dirnames` taught the filter that `canonical/`,
# `endpoints/` and `frontend/` were asset conventions, which then admitted unrelated prose
# examples elsewhere in the suite. One exclusion, two effects.
_TEST_TREE_PARTS = {"fixtures", "test", "tests", "toy-repo", "__pycache__"}

# Never copied by `_deploy_entry`'s `ignore_patterns`, so never in the manifest either.
_NEVER_COPIED_PARTS = {"__pycache__"}


def _in_test_tree(rel: str) -> bool:
    """Judged on the DEPLOYED-relative path, not an absolute one.

    An absolute checkout path drags in whatever the machine's directories happen to be called,
    which makes the corpus depend on where someone cloned the repo — and makes a fixture suite
    under `tmp_path` behave differently from the real one for no reason a reader could predict.
    """
    return any(part in _TEST_TREE_PARTS for part in rel.split("/"))


# ---------------------------------------------------------------------------
# What a deployed reader actually has.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Deployed:
    """The installed world, as `claude_root`-relative POSIX strings. No checkout in sight.

    `manifest` holds files AND directories, because a reference can legitimately name either.
    `files` is the scan corpus: readable, non-fixture, and paired with the checkout path only so
    the bytes can be read — never so a path can be resolved against it.
    """

    manifest: frozenset[str]
    files: tuple[tuple[str, Path], ...]
    asset_dirnames: frozenset[str]
    extensions: frozenset[str]
    roots: frozenset[str]


def _deployed() -> _Deployed:
    """Enumerate what the installer deploys FROM THE GIT INDEX — an install from a CLEAN CHECKOUT.

    Not what a given machine holds: `_deploy_entry` copies the working-tree directory whole, so a
    real install can hold more than this. The module docstring's CANNOT section says why modelling
    the checkout is nonetheless the right frame.

    Mirrors `_deploy_entry` on that basis: a directory source is copied whole (minus
    `__pycache__`/`*.pyc`), a file source is copied as itself. Nothing else exists under
    `claude_root` — no repo root, no `docs/`, no `templates/`, no `install/`, and no
    `skills/_shared/`.

    WHAT SHIPS is `_installer_deploy_sources()`, read from `install/provision.py` and unchanged.
    WHICH FILES EXIST is `_tracked_paths()`, and that closes the second half of gh#171: the
    enumeration used to be `source.rglob("*")`, which made the corpus and the manifest a function
    of whatever the checkout happened to hold. Untracked build artifacts under a shipped skill — a
    `.egg-info/SOURCES.txt`, a vendored `node_modules/**/README.md` — were scanned as if they were
    instructions this suite ships, and being gitignored they left `git status` clean while doing it.
    Directories are the prefixes of index entries, for the same reason: git tracks no empty
    directory, and neither does a tracked-only export, so neither should the manifest.

    The one behaviour deliberately NOT taken from the index is `_deploy_entry`'s own exclusions —
    `__pycache__` and `*.pyc` are filtered here as before, so this stays a statement about what the
    installer copies rather than about what git happens to hold.

    `asset_dirnames` is the extractor's narrowing, and it is DERIVED for the same reason the
    failing set is computed: a hand-written list of "directories that look like assets" goes stale
    the first time a skill grows a new one. It is also what keeps the gate quiet enough to survive.
    The first cut matched any backticked token containing a slash, and fired on `green/red`,
    `auth/login`, `exact/path/to/file.py` (a documentation placeholder) and a bare
    `ScheduleWakeup(...)` code fragment. None is a shipped asset. `agentflow/status_writer.py`
    was listed here too, as "an import path, not a file to open", and that example is WITHDRAWN
    (E-0767): it names a tracked file under the non-shipping `_shared/` tree, so it is a real
    break of this gate's own class, and `_nonshipping_packages` now admits its head. The dotted
    `agentflow.status_writer` carries no slash and is still never extracted. A gate that reports those gets allowlisted into uselessness
    inside a week, and the allowlist is then exactly where the real break hides.
    """
    tracked = _tracked_paths()
    manifest: set[str] = set()
    files: list[tuple[str, Path]] = []
    dirnames: set[str] = set()
    extensions: set[str] = set()
    roots: set[str] = set()

    for key, source in _installer_deploy_sources(tracked):
        roots.add(key.split("/", 1)[0])
        manifest.add(key.split("/", 1)[0])
        manifest.add(key)
        if _index_rel(source) in tracked:  # a FILE source — `commands/*.md`
            extensions.add(source.suffix)
            if source.suffix in _READABLE and not _in_test_tree(key):
                files.append((key, source))
            continue
        # A DIRECTORY source: exactly what the index holds beneath it, and nothing else.
        dirnames.add(source.name)  # a sibling hop can be written bare: `brainstorming/SKILL.md`
        for rel in _tracked_children(source, tracked):
            segments = rel.split("/")
            if any(p in _NEVER_COPIED_PARTS for p in segments) or rel.endswith(".pyc"):
                continue
            for depth in range(1, len(segments)):  # git tracks files; the dirs are their prefixes
                parent = f"{key}/{'/'.join(segments[:depth])}"
                manifest.add(parent)
                if not _in_test_tree(parent):
                    dirnames.add(segments[depth - 1])
            dep = f"{key}/{rel}"
            manifest.add(dep)
            child = source / rel  # joined ONLY so the bytes can be read, never to resolve a token
            extensions.add(child.suffix)
            if child.suffix in _READABLE and not _in_test_tree(dep):
                files.append((dep, child))

    return _Deployed(
        manifest=frozenset(manifest),
        files=tuple(sorted(files)),
        # Non-deployed skill dirs are candidate HEADS too — `_shared/SCHEMA.md` spelled bare must
        # be seen, and "does it ship?" is the manifest's answer, never the extractor's (gh#157).
        # Their importable PACKAGES are heads for the same reason, one level down (E-0767).
        asset_dirnames=frozenset(
            dirnames
            | _nonshipping_roots(roots, tracked)
            | _non_deploy()
            | _nonshipping_packages(tracked)
        ),
        extensions=frozenset(e for e in extensions if e),
        roots=frozenset(roots),
    )


def _nonshipping_roots(
    deployed_roots: "frozenset[str] | set[str] | None" = None,
    tracked: "frozenset[str] | None" = None,
) -> frozenset[str]:
    """Repo-root directories that do NOT ship — an EXTRACTOR aid now, no longer a verdict.

    Without this the gate was blind to its own headline example. Measured AT b92cc7f, the commit
    before the first SHRINK — pinned to that hash, re-measured there, and unable to drift:
    `docs/REGISTRY.md` was backticked at 11 sites across 8 DEPLOYED skills, and `docs/` never
    ships, so every one of them was the filed defect verbatim — and the in-skill directory names
    alone could not see a single one, because they are learned from INSIDE skills and no skill
    carries a `docs/`. The unseen half of the class was larger than the half anyone had enumerated.
    They read `<suite_root>/docs/REGISTRY.md` now, and the gate standing green is what says so —
    which is exactly why this derivation must not be trimmed on the grounds that nothing currently
    uses it.

    It also removes a coincidence the gate was resting on. `templates/` was only ever a candidate
    because the orchestrator happens to carry a `templates/` subdirectory; rename that one folder
    and the entire filed gh#59 class would have gone silent with the suite still green.

    WHAT CHANGED. The subtracted set is no longer the hand-written `{"skills", "commands"}` — it is
    the top-level component of every key the installer emits. So this stays true if provision stops
    deploying `commands/`, and it stays true if it starts deploying a third root. And crucially it
    is now only asking "could this token be a path?"; whether the destination ships is the
    manifest's answer, which is why `skills/_shared/...` needs no rule of its own any more.

    AND IT IS READ FROM THE INDEX, not from `SUITE_ROOT.iterdir()`. This feeds `asset_dirnames`,
    which is a head filter — so a stray untracked directory at the repo root (a `.venv` sibling, a
    scratch `notes/`) used to WIDEN the extractor on one machine and not another, silently. A repo
    root directory is one that holds a tracked file; nothing else is a directory this repo has.
    """
    tracked = _tracked_paths() if tracked is None else tracked
    if deployed_roots is None:
        deployed_roots = {key.split("/", 1)[0] for key, _ in _installer_deploy_sources(tracked)}
    return frozenset(
        head
        for head in {p.split("/", 1)[0] for p in tracked if "/" in p}
        if not head.startswith(".") and head not in deployed_roots
    )


def _nonshipping_packages(tracked: "frozenset[str] | None" = None) -> frozenset[str]:
    """Importable package names inside a `SKILLS_NON_DEPLOY` skill — candidate HEADS (E-0767).

    `_non_deploy()` makes `_shared` a head, so `_shared/SCHEMA.md` is seen. It does nothing for a
    token written one level down: `agentflow/status_writer.py` names a real tracked file at
    `skills/_shared/agentflow/status_writer.py`, `_shared/` never ships, and the head `agentflow`
    was learned from nowhere — `dirnames` is learned from INSIDE deployed skills and this one is
    inside the skill that is excluded by definition. So the token never reached candidacy, the
    gate reported nothing, and five deployed orchestrator files told their readers to open a path
    that does not exist under `~/.claude/skills/`.

    THIS REVERSES A DELIBERATE DECISION, so the reason is recorded rather than left to a diff.
    `_deployed()`'s docstring used to cite this exact token as extractor noise, "an import path,
    not a file to open". As a dotted module name that is right, and `agentflow.status_writer` is
    still not extracted — no slash, so `_is_candidate` drops it before any head filter runs. But
    the SLASH spelling is not an import path; it is a file path, to a file the reader cannot
    reach, and telling the two apart is the whole of what this epic fixes. The noise argument
    survives for `green/red` and `auth/login`, which name no tracked file at all.

    WHY `__init__.py` AND NOT THE SUBDIRECTORY LIST. Measured at `06485ec`: `skills/_shared/`
    holds `agentflow`, `scripts` and `tests`, and exactly one of them is a package. Admitting all
    three heads surfaces seven tokens that are not this repo's files — `tests/test_file.py` and
    `tests/exact/path/to/test.py` name the READER's test directory in instructional prose. An
    importable package is also precisely the thing whose name appears in the dotted form a
    corrected citation uses, so the two spellings this gate must separate are both derived from
    one fact rather than from a list someone maintains.

    READ FROM THE INDEX for the same reason `_nonshipping_roots` is: an untracked scratch package
    under `skills/_shared/` would widen the extractor on one machine and not another, silently.
    """
    tracked = _tracked_paths() if tracked is None else tracked
    non_deploy = _non_deploy()
    packages: set[str] = set()
    for path in tracked:
        segments = path.split("/")
        if len(segments) < 4 or segments[0] != "skills" or segments[-1] != "__init__.py":
            continue
        if segments[1] not in non_deploy:
            continue
        packages.add(segments[2])
    return frozenset(packages)


# ---------------------------------------------------------------------------
# Extraction.
# ---------------------------------------------------------------------------
# TIER 1 — a path reference as this suite mostly writes them: inside backticks, relative, literal.
# NOT anchored on a file extension: `scripts/review-package` has none, and an extension-anchored
# pattern is blind to every extensionless executable the suite ships.
_BACKTICKED = re.compile(r"`([^`\n]{1,200})`")

# FENCED BLOCKS ARE NOT STRIPPED, and this comment exists so nobody re-adds the strip that used to
# sit here. Its stated reason was that fences hold "glob illustrations, shell transcripts and JSON
# samples ... none of them is prose telling a reader to open a file". The corpus falsifies that.
# A path ALONE IN A FENCE was seen by NEITHER tier — the prose pass had the fence blanked out, and
# `_BACKTICKED`'s `[^`\n]` cannot span a fence's own delimiters — a blind spot that was hiding a
# live break when it was found:
#
#   `_vendored/review-prompts/code-quality-reviewer-prompt.md` writes one destination twice inside
#   ONE fence, on ADJACENT lines — `../requesting-code-review/code-reviewer.md`, which resolves,
#   and directly beneath it `requesting-code-review/code-reviewer.md`, which does not. Two
#   spellings, one destination, opposite verdicts: gh#157's exact headline shape, surviving inside
#   a fence. It is repaired in the same commit that made it visible.
#
# Fence contents therefore go through the PROSE rules — extension required — and that is what keeps
# shell transcripts quiet. The delta was measured on both sides of that repair rather than argued:
# against the corpus as it stood BEFORE it, opening fences admitted candidate pairs that all
# resolved except the single break above, and added no false positive at all; after it the two
# spellings collapse onto one token, and that token resolves. The sizes are not typed here — they
# move with the corpus, and nothing in this file recomputes them.
#
# Two of the pairs that fences newly admit are first-party and worth naming:
# `orchestrator/prompts/code-reviewer-evidence-wrapper.md` and its spec-reviewer twin each put a
# path alone in a fence under prose asserting "This path is guaranteed present post-install". They
# are silent today, but unguarded, and this gate is the natural checker for exactly that guarantee.
# Regression: `test_a_path_alone_in_a_fence_is_reported`.

# TIER 2a — a markdown link target. The truest positives in `_vendored/` live here, and the naive
# prose scan cannot reach them: the destination sits inside parentheses and `(` is in
# `_SKIP_SUBSTRINGS`, so the token would die on this module's own filter.
_MD_LINK = re.compile(r"\]\(([^)\s]{1,200})\)")

# TIER 2b — bare prose (gh#144). Broad on purpose; the POSITIVE tests below do the narrowing.
_PROSE = re.compile(r"[^\s`]*/[^\s`]*")

# Rejected before resolution. Each resolves against something this gate does not have, so
# flagging it would be a false positive, and false positives are how a gate dies.
_SKIP_SUBSTRINGS = ("://", "<", ">", "$", "{", "}", "*", "|", "(", '"', "=")

# Decoration a path picks up in prose. `_` is CONSPICUOUSLY ABSENT and must stay absent: stripping
# it turns `_vendored/brainstorming` into `vendored/brainstorming` and silently drops a candidate,
# and `_vendored`/`_shared` are precisely the underscore-prefixed directories this gate is about.
#
# The LEADING set is narrower than the trailing one for the same kind of reason. A leading `.` is
# part of the path, not decoration: stripping it turned `.tdd-loop/allowlist.txt` — a runtime
# directory in the USER's repo, correctly out of scope — into `tdd-loop/allowlist.txt`, whose head
# is a real skill directory, and the gate reported a file it was never meant to see. Measured, not
# imagined: that false positive appeared in the first run of this widening.
_EDGE_LEAD = "*([\"'`"
_EDGE_TRAIL = "*,;:.!?)]\"'`"


def _candidate_paths(
    text: str, dep: "_Deployed | None" = None, *, require_prose_extension: bool = True
) -> set[str]:
    """Tokens that could name a file a reader is expected to open. Broad extract, POSITIVE filter.

    SCOPE, stated so nobody reads a pass as broader than it is. IN: `templates/x.md`,
    `references/y.md`, `../other-skill/SKILL.md`, `skills/_shared/SCHEMA.md` — the class gh#59 is
    about, where "does this ship?" has a definite answer. OUT by construction rather than by
    allowlist: absolute paths, `<placeholder>` and `$VAR` paths, URLs, import paths,
    `<data_root>`-relative state paths, and anything carrying shell or code punctuation.

    THE TIERS AND WHY THEY DIFFER. Widening to bare prose (gh#144) is a false-positive problem,
    not a regex problem. The overwhelming majority of slash-bearing tokens in this suite's prose
    are alternations (`and/or`, `spec/plan`, `PASS/FAIL`), ratios (`3/3`, `24h/72h`), slash
    commands (`/project`) and XML-ish tags. The filters this module already had take out the rest
    of that noise. What still reaches candidacy does so because agentflow's skills are named after
    ordinary nouns (`report/ledger`, `solution/plan`) or because a repo-root
    directory shares a noun's name (`install/scaffold`) — their heads ARE real directory names.
    That surviving set is named and asserted by
    `test_the_extension_rule_costs_what_the_docstring_says`; it is not counted here.

    The separator that removes them is a file extension the installer actually deploys. So prose
    and fence tokens must carry one; backticked tokens need not, which is what keeps
    `scripts/review-package` visible.

    THAT SEPARATOR IS NOT FREE, and the module docstring states its price rather than claiming
    there is none: it also suppresses real destinations, every one of them inside `_vendored/` —
    upstream provenance and test-prompt text, not instructions this suite owns, which is why the
    trade was taken.

    THE NUMBERS ARE NOT TYPED HERE. `test_the_extension_rule_costs_what_the_docstring_says`
    recomputes the whole funnel against the live corpus and fails if the shape moves, which is the
    treatment every hand-typed count in this file's history has eventually needed. That test is
    the only caller that passes `require_prose_extension=False`; the knob exists so the trade can
    be MEASURED rather than asserted, and it is not a behaviour switch for the gate.
    """
    d = _deployed() if dep is None else dep
    out: set[str] = set()

    for raw in _BACKTICKED.findall(text):
        tok = _clean(raw)
        if tok and _is_candidate(tok, d, require_extension=False):
            out.add(tok)

    prose = _BACKTICKED.sub(" ", text)
    for raw in _MD_LINK.findall(prose) + _PROSE.findall(prose):
        tok = _clean(raw)
        if tok and _is_candidate(tok, d, require_extension=require_prose_extension):
            out.add(tok)
    return out


def _clean(raw: str) -> str:
    """First whitespace-delimited word, stripped of markdown decoration and sentence punctuation.

    Returns `""` for a token carrying runtime punctuation, and it does so BEFORE dropping a `#`
    fragment. Order matters: `../../status/audits/2026-01-01-1.md#<slug>` is a link written inside
    a generated artefact in the user's data root, and splitting the fragment off first leaves a
    plausible-looking relative path with its `<slug>` — the very marker that says "resolved at
    runtime, not here" — quietly removed. That false positive appeared in the first run of this
    widening too.
    """
    parts = raw.strip().split()
    if not parts:
        return ""
    word = parts[0].lstrip(_EDGE_LEAD).rstrip(_EDGE_TRAIL)
    if any(s in word for s in _SKIP_SUBSTRINGS):
        return ""
    return word.split("#", 1)[0]


def _is_candidate(tok: str, dep: _Deployed, *, require_extension: bool) -> bool:
    if "/" not in tok or tok.endswith("/"):
        return False
    if any(s in tok for s in _SKIP_SUBSTRINGS):
        return False
    if tok.startswith(("/", "~", "./")) or (len(tok) > 1 and tok[1] == ":"):
        return False
    head = tok.split("/", 1)[0]
    # A sibling hop, a file inside a directory some skill genuinely carries, a repo-root directory,
    # or a `claude_root`-rooted path. `..` is IN scope deliberately: `_vendored/brainstorming`'s
    # `../asking-questions/SKILL.md` was off by exactly one level, and it is the best find of the
    # sweep that produced this gate — dropping the shape to keep the extractor tidy would drop the
    # finding with it.
    if head != ".." and head not in dep.asset_dirnames and head not in dep.roots:
        return False
    if require_extension and posixpath.splitext(tok.rsplit("/", 1)[-1])[1] not in dep.extensions:
        return False
    return True


# ---------------------------------------------------------------------------
# Resolution — lexical, against the manifest, never against a filesystem.
# ---------------------------------------------------------------------------
def _owning_skill_root(dep_path: str, manifest: frozenset[str]) -> str:
    """The nearest ancestor that IS a skill — the deepest directory holding a `SKILL.md`.

    Not the top-level directory under `skills/`, which is what a first cut used and what made
    `_vendored/brainstorming/SKILL.md`'s broken `../asking-questions/SKILL.md` look fine:
    `_vendored/` deploys as ONE directory holding many sub-skills, so `..` from there escapes to
    the skills root by an accident of nesting no reader reproduces.

    Both roots are needed and neither alone is right. Dropping this one turns every reference that
    resolves ONLY against the owning skill root into a false positive — `references/inventory/*.js`
    cited from `autodoc-init/references/`, and `prompts/*.md` cited both from
    `orchestrator/agents/` and, in a JSON sample, from inside `orchestrator/prompts/` itself.

    HOW MANY there are is not typed here. An earlier draft typed it, named two of those citing
    directories and missed the third, and the wrong total then read as checked.
    `test_the_owning_skill_root_is_load_bearing` recomputes the set against the live corpus
    instead. Keeping the naive version hid the single best finding of the sweep. The fix is to ask
    what a reader would call "this skill", and a `SKILL.md` is what marks one.

    Asked of the MANIFEST, so a `commands/*.md` wrapper — a flat file with no `SKILL.md` anywhere
    above it — simply falls back to its own directory instead of walking off the top.
    """
    parts = dep_path.split("/")
    for i in range(len(parts) - 1, 0, -1):
        cand = "/".join(parts[:i])
        if "/" not in cand:  # a deploy root (`skills`, `commands`) is nobody's skill
            break
        if f"{cand}/SKILL.md" in manifest:
            return cand
    return posixpath.dirname(dep_path)


def _resolves(dep_path: str, token: str, dep: _Deployed) -> bool:
    """True if a reader could find this token from where the reference actually leaves them.

    THREE ROOTS, all of them things an installed user demonstrably has:

    1. the directory of the file doing the referring — a relative path in a document resolves
       against that document first;
    2. the skill that owns it, which is what a reader means by "this skill";
    3. `claude_root` itself, because `skills/lead-dev/SKILL.md` and `commands/report.md` name
       exactly what `~/.claude/` contains, and a `commands/*.md` wrapper has no other root.

    Root 3 is NOT the repo root, and the distinction is the point. Admitting SUITE_ROOT was the old
    bug: `templates/registry.md` would "pass" because `<repo>/templates/registry.md` exists on a
    developer machine, while the instruction still cannot be followed. Under `claude_root` that
    same token fails, because `templates/` is not deployed — the manifest says so.

    Resolution is `posixpath.normpath`, not a stat. A destination that normalises above
    `claude_root` (`../../x`) is unreachable BY CONSTRUCTION, which is what makes the three
    spellings of one non-shipping destination — `skills/_shared/SCHEMA.md`, `_shared/SCHEMA.md`,
    `../_shared/SCHEMA.md` — all fail without any of them being special-cased.
    """
    for root in (
        posixpath.dirname(dep_path),
        _owning_skill_root(dep_path, dep.manifest),
        "",
    ):
        dest = posixpath.normpath(posixpath.join(root, token))
        if dest == ".." or dest.startswith("../"):
            continue  # escapes claude_root: nothing up there ships
        if dest in dep.manifest:
            return True
    return False


def unresolvable() -> list[tuple[str, str]]:
    """Every (deployed file, token) a reader could not find. Computed, never listed.

    The file key is `claude_root`-relative, which for this repo's layout is byte-identical to the
    repo-relative path — `skills/x/y.md` and `commands/z.md` sit at the same place in both worlds.
    That is a convenience of the layout, not a fact this module leans on.
    """
    dep = _deployed()
    found: list[tuple[str, str]] = []
    for dep_path, f in dep.files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for tok in sorted(_candidate_paths(text, dep)):
            if not _resolves(dep_path, tok, dep):
                found.append((dep_path, tok))
    return found


# ---------------------------------------------------------------------------
# The ratchet.
#
# Every entry is a real break a reader would hit. The list may only SHRINK: adding to it is how a
# gate becomes a formality, and this repo already deletes an exemption the moment its reason
# expires (see `KNOWN_UNBRANCHED` in test_human_surface_gates.py, and the ratchet it documents).
#
# SHRINK 1, 2026-09-02 (b92cc7f -> fa04d59): 20 -> 15. Five references repaired, not excused. The
# boundary was every break in a file E-0200's `## Files` does not name at all — with one exception
# worth stating rather than smoothing over: `auditor/references/ledger-format.md` is not named
# either, and its two breaks were still held back, to travel with the auditor SKILL.md work they
# belong to.
#
# SHRINK 2, 2026-09-02 (fa04d59 -> e4e8c7e): 15 -> 5, the boundary moved from E-0200's DECLARED
# `## Files` to its LIVE REMAINDER. There is no single merge that closes that epic's SKILL.md line
# as a unit, so the remainder was judged by reading its three open items (4b, 11b, 12a),
# which touch config, `install/provision.py` and `wiki.py`, and no skill prose. A seam is a claim
# about FUTURE work, and merged items leave it. Both juniors were told BEFORE the edit.
#
# SHRINK 3, 2026-09-02 (e4e8c7e -> 3ba21cb): 5 -> 0, AND THE BLOCKER ON THEM WAS NEVER REAL.
# Shrink 2 recorded these five as "ONE decision, gated on a human" because both fix shapes gh#59
# proposes were said to touch `install/provision.py`. That was asserted, not measured, and
# measuring it took two commands:
#
#   * A skill-local `templates/` DOES deploy - `shutil.copytree` ships a skill's whole directory,
#     which is why `orchestrator/templates/` is installed and repo-root `templates/` is not. So
#     shape (a) needed no installer change either.
#   * And shape (a) would still have been WRONG: `install/provision.py` READS
#     `templates/registry.md` (`REGISTRY_TEMPLATE_REL`) to seed each project, so the file belongs
#     at the repo root and copies inside skills would be a second source of truth.
#
# What the five actually were, once read rather than counted:
#
#   * THREE entries (FOUR prose sites) were the same OBSOLETE MANUAL STEP - "create the registry
#     from templates/registry.md if missing" - and `python -m agentflow.registry init` already
#     does exactly that: it resolves the template itself, refuses to clobber an existing file, and
#     writes atomically. NOT "under the edit-lock" - review caught that, and `init` is precisely
#     the one write path that never takes it (`main()` returns `_do_init` before reaching
#     `_run_mutation`, the sole caller of `acquire_edit_lock`). That is defensible for CREATING an
#     absent file and it is not what makes the command better than the instruction; what does is
#     that it exists, resolves the template, and is idempotent. Replaced the prose with the command.
#   * TWO entries (THREE prose sites) were a genuine asset read (`gap-epic.md`, which the auditor
#     writes gap specs FROM), fixed by the `<suite_root>/` rooting shipped for the `docs/` half.
#
#   The entry/site split is not pedantry: KNOWN_BROKEN is keyed on (file, token), so two
#   occurrences in one file collapse to one entry. The first draft of this block said "FOUR / ONE",
#   which took one number from sites and the other from nothing - and the two summed to the right
#   total, which is exactly what made it read as checked.
#
# No design decisions, no installer changes, no duplicated assets. The lesson is the blocker, not
# the paths: this list carried a "gated on a human" note across wake after wake, and each
# restatement made it read more settled. A wrong stop produces silence, so nothing re-derives it.
#
# WIDENING, 2026-09-03 (this commit): STILL 0, and the number is load-bearing rather than lucky.
# The extractor now reads bare prose, markdown links and FENCED BLOCKS (gh#144), the corpus now
# includes `commands/*.md` (gh#156), and every verdict is now a manifest lookup instead of a stat
# against the checkout (gh#157). That surfaced four references this gate had never been able to
# report — two repaired, two reclassified:
#
#   * `skills/dev-manager/SKILL.md` -> `report/SKILL.md` — REPAIRED, not excused. A genuine
#     unrooted sibling reference in bare prose; the same file already writes that destination
#     correctly elsewhere as `skills/report/SKILL.md`, so the fix is that spelling.
#   * `skills/_vendored/review-prompts/code-quality-reviewer-prompt.md` ->
#     `requesting-code-review/code-reviewer.md` — REPAIRED, not excused, and it is the reason
#     fence scanning belongs in THIS commit rather than a follow-up. The SAME fence names the SAME
#     destination on the line above as `../requesting-code-review/code-reviewer.md`, which resolves.
#     Two spellings, one destination, opposite verdicts, adjacent lines: gh#157's headline shape,
#     surviving inside a fence because neither extraction tier could see in. Rooted to match its
#     neighbour. See the comment above `_MD_LINK` for the measured cost of opening fences.
#   * `skills/orchestrator/templates/plan-frontmatter.yaml` -> two `docs/runbooks/*.md` — moved to
#     NOT_A_PATH, which is where they belong: they are commented-out EXAMPLE values under
#     `# Example (uncomment and populate):`, naming files in the USER's own project repo, the same
#     class as the `docs/adr/ADR-NNN` entry that has sat there since the gate was written. There
#     is nothing here to repair, so recording them as a debt we owe would be false.
#
# Two false positives the widening also produced were fixed in the EXTRACTOR rather than excused,
# and both are recorded at their filters: a stripped leading `.` (`.tdd-loop/allowlist.txt`) and a
# `#fragment` split that ran before the `<placeholder>` check (`...2026-01-01-1.md#<slug>`). An
# allowlist entry would have hidden both bugs and left the next instance of each unreported.
#
# WIDENING 2, 2026-09-14 (this commit): STILL 0, and again load-bearing rather than lucky. gh#171's
# first half: `_READABLE` grew to cover the shipped EXECUTABLES — `.py`, `.sh` and extensionless
# files — so 40 files this gate had never opened entered the corpus, among them the three
# `_vendored/subagent-driven-development/scripts/*` the module docstring had been citing as its
# motivating case while reading none of them. Six references surfaced. FIVE were repaired:
#
#   * `autodoc_graph/cli.py` and `autodoc_graph/ingest.py` -> `inventory/emit-graph.js`. The file
#     is real and ships — `skills/autodoc-init/references/inventory/emit-graph.js` — but the
#     citation was relative shorthand written from the `references/` directory, which is not a
#     root any reader stands at. Rooted at `skills/`.
#   * `autodoc_graph/cli.py` and `autodoc_graph/render_pages.py` ->
#     `skills/_shared/tests/test_documented_graph_recipes.py`. Already `skills/`-rooted, and still
#     unreachable: `_shared` is in `SKILLS_NON_DEPLOY`, so no installed reader has it under ANY
#     spelling. That is the gh#146 shape, and this repo's existing answer to it is the
#     `<suite_root>/` rooting the `docs/` half already uses — which says "this lives in the
#     agentflow repo, not in your install" and is dropped before resolution because `<` is in
#     `_SKIP_SUBSTRINGS`.
#   * `autodoc-update/references/build-changed-communities.py` -> `autodoc-update/SKILL.md`, which
#     drops the `skills/` root. Rooted.
#
# The SIXTH was not a break. `autodoc_graph/sqlite_backend.py` -> `migrations/NNNN_name.sql` is a
# naming PATTERN for files that do not exist yet, spelled indistinguishably from a path. It is now
# written `migrations/<NNNN>_<name>.sql` — the same `<placeholder>` mechanism that already keeps
# every `<data_root>/...` and `<suite_root>/...` in the suite's prose out of candidacy — rather
# than a new exemption. A `KNOWN_BROKEN` entry would have claimed a debt that does not exist, and
# a `NOT_A_PATH` entry would have taught the gate to ignore a shape it can simply be told about.
#
# WHAT THE COUNT MEANS. It is still A FLOOR, NOT A CENSUS — the original wording was right and an
# earlier draft of this paragraph widened it wrongly, which is this repo's signature defect: prose
# stated at a wider scope than what was measured.
#
# Precisely: KNOWN_BROKEN is a census of breaks in tokens whose FIRST SEGMENT is a name this gate
# already knows — a deploy root, a directory in the deployed tree, a non-shipping repo-root
# directory, a `SKILLS_NON_DEPLOY` skill, or `..` — extracted from backticks, markdown links,
# fenced blocks, or bare prose carrying a deployed file extension, across the `_READABLE` files in
# skills AND commands — prose and shipped executables alike, since gh#171.
#
# Everything outside that sentence is invisible, and the residual is much larger than one item:
#
#   * A TOKEN UNDER A PACKAGE HEAD ONE LEVEL INSIDE A NON-SHIPPING SKILL. CLOSED for importable
#     packages (E-0767): `agentflow/status_writer.py` named a real file no deployed reader can
#     open, `_non_deploy()` made `_shared` a head but nothing made `agentflow` one, and five
#     orchestrator files carried the citation. `_nonshipping_packages` derives the head from
#     `__init__.py` in the index, so `scripts/` and `tests/` stay out and the READER's own
#     `tests/test_file.py` is still not mistaken for one of ours. What REMAINS open is the
#     non-package case: a token under `_shared/scripts/` or `_shared/tests/` is still invisible,
#     and closing it costs seven false positives measurement says are not worth buying today.
#   * A TOKEN UNDER AN UNFAMILIAR HEAD. `runbooks/deploy.md` is not seen even backticked, even
#     extensioned, even in an imperative sentence — `_is_candidate` applies a head filter and
#     nothing here is called `runbooks/`. This is the biggest hole by far; it is why the CAN-bullet
#     in the module docstring carries the head proviso, and it is pinned by
#     `test_an_unfamiliar_head_is_the_gates_largest_silence`.
#   * An extensionless path named only in bare prose or in a fence (`see report/SKILL`).
#   * Any token spelled `./...`. Its head is `.`, which is not a name this gate knows, so the head
#     filter drops it and `./../_shared/SCHEMA.md` is a gh#59-class silence.
#   * Any reference inside a shipped file `_READABLE` does not open — `.js`, `.ts`, `.sql`,
#     `.json`, `.go`, `.html`. The `.py`/`.sh`/extensionless third of this bullet is CLOSED
#     (gh#171): those are read now, and what the widening cost is recorded at `_READABLE` rather
#     than smoothed over. The remainder is deliberately not totalled — nothing here recomputes
#     such a total, which is the only standard this file applies to a number.
#
# The rule for removing one is not "make the gate quiet" — it is to make the reference resolve, by
# shipping the asset inside the skill that names it, by rooting the path, or by deleting an
# instruction that should never have been there.
# ---------------------------------------------------------------------------
#: Read the SHRINK log above before adding anything here. Adding an entry requires raising
#: KNOWN_BROKEN_CEILING in the same diff, deliberately and with a stated reason.
KNOWN_BROKEN: dict[tuple[str, str], str] = {}

# NOT a path this repo ships, and never will be. Separate from KNOWN_BROKEN on purpose: that list
# must shrink, this one need not, and conflating "we owe a fix" with "the gate cannot tell" is how
# an allowlist stops meaning anything.
NOT_A_PATH: dict[tuple[str, str], str] = {
    (
        "skills/autodoc-init/references/taxonomy.md",
        "references/frontmatter.md",
    ): "prose analogy — cites the PERSONAL WIKI's file to explain a convention, not an instruction to open anything in this repo",
    (
        "skills/autodoc-init/references/taxonomy.md",
        "docs/adr/ADR-NNN",
    ): "prose analogy — describes what an ADR IS in the USERs own project, with a placeholder number; nothing here is meant to sit at that path",
    (
        "skills/_vendored/README.md",
        "_vendored/brainstorming",
    ): "provenance, not an instruction — 'skills/refine/ is a diverged FORK of _vendored/brainstorming' states a lineage; nobody is told to open it",
    (
        "skills/orchestrator/templates/plan-frontmatter.yaml",
        "docs/runbooks/platform-cutover.md",
    ): "commented-out EXAMPLE value under '# Example (uncomment and populate):' — an extra_context_files entry, whose own contract in the comment block above it says it resolves against the PROJECT REPO ROOT",
    (
        "skills/orchestrator/templates/plan-frontmatter.yaml",
        "docs/runbooks/platform-stabilization-smoke.md",
    ): "second half of that same commented-out example; a path that names its own root is a different construct, the reason <suite_root>/ paths are dropped before resolution too",
}


# The recorded size of the ratchet, which must always EQUAL len(KNOWN_BROKEN): neither number can
# move without the other in the SAME diff. That is what a reviewer sees. It does NOT make widening
# impossible — an entry plus a matching raise is green, and is the deliberate path the SHRINK log
# describes. Enforcement is a human reading the raise, not the suite refusing it. Say it that way:
# the earlier version claimed "raising it fails", which is false, and it is the same overclaim as
# the "may only SHRINK" prose it replaced (review mutation-tested that one and a widening passed).
KNOWN_BROKEN_CEILING = 0


def test_the_ratchet_cannot_widen() -> None:
    """`KNOWN_BROKEN` may only shrink, and until now nothing enforced it.

    Adding an entry is how a gate becomes a formality: the next real break gets excused by the
    same commit that records it, with the suite green. Shrinking now requires lowering the
    ceiling in the same diff, which a reviewer sees; growing cannot be done quietly at all.
    """
    assert len(KNOWN_BROKEN) <= KNOWN_BROKEN_CEILING, (
        f"KNOWN_BROKEN grew to {len(KNOWN_BROKEN)} against a ceiling of {KNOWN_BROKEN_CEILING}. "
        "Repair the reference instead. If a break is genuinely new and genuinely cannot be fixed "
        "now, raise the ceiling in the same commit and say why — deliberately, not incidentally."
    )
    assert len(KNOWN_BROKEN) == KNOWN_BROKEN_CEILING, (
        f"KNOWN_BROKEN is {len(KNOWN_BROKEN)} but the ceiling still reads {KNOWN_BROKEN_CEILING}. "
        "Lower the ceiling to match — a ceiling above the real count is headroom for a silent "
        "widening, which is the whole thing this test exists to prevent."
    )


@pytest.mark.parametrize(
    "path,token,reason",
    sorted((k[0], k[1], v) for k, v in KNOWN_BROKEN.items()),
    ids=lambda v: v.split("/")[-1][:24],
)
def test_each_known_break_states_its_reason(path: str, token: str, reason: str) -> None:
    """An exemption with no stated reason outlives its reason, because nobody can check it."""
    assert len(reason.split()) >= 10, f"{path} -> {token}: KNOWN_BROKEN reason too thin: {reason!r}"


@pytest.mark.parametrize("path,token", sorted(KNOWN_BROKEN), ids=lambda v: v.split("/")[-1])
def test_each_known_break_is_still_a_break(path: str, token: str) -> None:
    """An exemption that outlives its reason goes on exempting the next thing at that spot.

    If this fails, the reference was fixed — delete the entry rather than reword it.
    """
    assert (SUITE_ROOT / path).is_file(), (
        f"{path} is no longer SCANNED — deleted, renamed, or moved out of the readable/deployed "
        "set. That is not the same as fixed, and the message below would have told you it was. "
        "Delete this entry only if the instruction genuinely no longer ships to anyone."
    )
    assert (path, token) in set(unresolvable()), (
        f"{path} -> {token} now RESOLVES, so its KNOWN_BROKEN entry is stale. Delete the entry; "
        "do not reword it. A stale exemption keeps exempting whatever later occupies that slot."
    )


def test_no_deployed_skill_references_a_path_its_reader_cannot_find() -> None:
    """The gate itself: a NEW unresolvable reference fails immediately."""
    excused = set(KNOWN_BROKEN) | set(NOT_A_PATH)
    offenders = [pair for pair in unresolvable() if pair not in excused]
    assert not offenders, (
        "These deployed files name a path that resolves from NONE of the roots a reader has — "
        "not the referring file's own directory, not the skill that owns it, and not the root of "
        "`~/.claude/` itself:\n"
        + "\n".join(f"  {p}  ->  {t}" for p, t in offenders)
        + "\n\nShip the asset inside the skill that names it, root the path, or delete the "
        "instruction. Adding it to KNOWN_BROKEN is not a fix and the list may only shrink."
    )


def test_the_gate_can_actually_fail() -> None:
    """A gate nobody has seen fail is a gate nobody should trust.

    Shapes that defeated the human sweep are asserted here directly, because each is something a
    future 'simplification' of the extractor would silently drop. Deliberately uncounted - the
    previous wording said "two" and went stale twice as assertions were added beneath it.
    """
    # An extensionless executable is a path. An extension-anchored regex cannot see it, and this
    # suite ships such files under `_vendored/subagent-driven-development/scripts/`. Backticked
    # tier only — see `_candidate_paths`.
    assert "scripts/review-package" in _candidate_paths("run `scripts/review-package BASE HEAD`")
    # A bare filename is not a path reference; requiring the slash is what keeps this gate quiet.
    assert _candidate_paths("see `README`") == set()
    # Runtime-resolved paths are out of scope by construction, not by allowlist.
    assert _candidate_paths("`<suite_root>/docs/REGISTRY.md`") == set()
    assert _candidate_paths("`$SUITE_ROOT/scripts/validate_mermaid.py`") == set()
    # ABSOLUTE PATHS — the last of the four things the module docstring says this gate
    # deliberately ignores, and the only one of the four that had no assertion anywhere behind it.
    # A claim in a docstring with nothing exercising it is how the other three would eventually
    # have gone wrong too, so all three spellings are pinned: a POSIX root, a `~` home reference,
    # and a Windows drive letter. Each names a destination that DOES ship and IS admitted when
    # written relatively (asserted a few lines below), so the absolute spelling is the only thing
    # keeping them out.
    #
    # WHAT ACTUALLY REJECTS THEM, stated because the alternative is the mistake this file has
    # already made once: it is the HEAD FILTER, not the `startswith` tuple that reads as if it
    # were. `/x/y.md` has head `""`, `~/x/y.md` has head `"~"`, `C:/x/y.md` has head `"C:"`, and
    # none is a deploy root, an asset directory or `..`. Delete `"/"`, `"~"` or the drive-letter
    # clause from `_is_candidate` and these three assertions stay GREEN — measured, not assumed.
    # The clauses are a redundant guard that happens to be subsumed today; the assertions below
    # pin the EFFECT the docstring claims, which is the part that must not change.
    assert _candidate_paths("`/home/x/.claude/skills/lead-dev/SKILL.md`") == set()
    assert _candidate_paths("`~/.claude/skills/lead-dev/SKILL.md`") == set()
    assert _candidate_paths("`C:/Users/x/.claude/skills/lead-dev/SKILL.md`") == set()
    # THE FALSE POSITIVES THAT KILLED THE FIRST CUT. Each fired before the asset-directory
    # narrowing, and each would have taught a reader to ignore this gate.
    for noise in ("`green/red`", "`auth/login`",
                  "`exact/path/to/file.py`", "`status/registry.md`"):
        assert _candidate_paths(noise) == set(), noise + " is not a shipped asset reference"
    # `agentflow/status_writer.py` USED TO SIT IN THAT TUPLE and E-0767 took it out, because the
    # claim was false in the one spelling that matters. It names a tracked file under the
    # non-shipping `_shared/` tree, so a deployed reader told to open it cannot — that is this
    # gate's own class, not noise. Both polarities are pinned here and not only in the dedicated
    # test, because the same stale claim was living in three places at once: this tuple,
    # `_deployed()`'s docstring, and nothing at all on the reporting side.
    assert _candidate_paths("`agentflow/status_writer.py`") == {"agentflow/status_writer.py"}
    assert _candidate_paths("`agentflow.status_writer`") == set()
    # THE CANARY THE RATCHET USED TO PROVIDE FOR FREE, and stopped providing the moment
    # KNOWN_BROKEN emptied. While entries existed, `test_each_known_break_is_still_a_break`
    # asserted as a side effect that the extractor could still SEE a `templates/...` token at all;
    # with the list empty that test is skipped, and review mutation-proved the consequence — kill
    # both sources of the `templates` head (the repo-root derivation AND
    # `orchestrator/templates/`) and the whole gh#59 headline class goes invisible with the suite
    # fully green.
    #
    # So assert the two non-shipping roots directly, independent of what KNOWN_BROKEN happens to
    # hold. An emptied ratchet must not mean an untested detector — that is how a gate that has
    # finished its job quietly stops being one. Every widening below carries the same canary, for
    # the same reason: see `test_an_empty_ratchet_still_proves_the_extractor_sees_each_class`.
    assert "templates/registry.md" in _candidate_paths("seed it from `templates/registry.md`")
    assert "docs/REGISTRY.md" in _candidate_paths("the schema is `docs/REGISTRY.md`")
    # THE THIRD VARIANT (gh#146): `skills/` ships, `skills/_shared/` does not, and those are
    # different facts. It no longer needs a rule of its own — `skills/` is a deploy root, so the
    # token is a candidate, and the MANIFEST is what knows `skills/_shared/` never arrives. That
    # is the gh#157 shape: candidacy is about spelling, the verdict is about destination.
    assert "skills/_shared/SCHEMA.md" in _candidate_paths("the rule is `skills/_shared/SCHEMA.md`")
    # ...and the shipping half is now ADMITTED and resolves, where it used to be excluded from
    # candidacy. That exclusion pinned a DECISION taken under the old checkout-stat model, where
    # `skills/lead-dev/SKILL.md` resolved from no root, so admitting it would have meant recording
    # unfixed sites rather than judging anything. Under the manifest it resolves cleanly against
    # `claude_root` — which is exactly what an installed user has — so the decision cost nothing to
    # reverse, and reversing it is what lets `commands/autodoc-init.md`'s `skills/autodoc-init/...`
    # line be JUDGED rather than merely scanned.
    assert "skills/lead-dev/SKILL.md" in _candidate_paths("read `skills/lead-dev/SKILL.md`")


def test_the_non_deploy_set_comes_from_the_installer(monkeypatch, tmp_path) -> None:
    """PROVENANCE, not membership — the first version of this test could not tell them apart.

    It asserted `"_shared" in _non_deploy()`, which a hardcoded `return {"_shared"}` satisfies:
    the exact hand-written restatement the function's own docstring forbids would have passed its
    own guard. Pointing the parser at a throwaway installer is the only way to prove the value was
    READ rather than typed here.

    Asserting the real set literally would BE that restatement, so it is deliberately not done.
    """
    fake_install = tmp_path / "install"
    fake_install.mkdir()
    (fake_install / "provision.py").write_text(
        'SKILLS_NON_DEPLOY = {"zzz-not-a-real-skill"}\n', encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "SUITE_ROOT", tmp_path)
    assert _non_deploy() == {"zzz-not-a-real-skill"}, (
        "_non_deploy() did not read the installer it was pointed at, so it is restating a "
        "hand-written set — which is the one thing its docstring says must never happen."
    )


# ---------------------------------------------------------------------------
# A synthetic suite, so each widened class is proved on a corpus that cannot drift.
#
# Every helper in this module reads `SUITE_ROOT` as a module global AT CALL TIME, so repointing
# that one name swaps the whole world — corpus, manifest, non-deploy set and all. That is the
# mechanism `test_the_non_deploy_set_comes_from_the_installer` already used; these fixtures
# generalise it rather than inventing a second one.
#
# The fake installer is a real, executable `_deploy_sources` — not a stub — because that is the
# only way a test can prove the corpus FOLLOWS the installer instead of merely agreeing with it
# today. Tests below edit it mid-test and assert that the gate changes its mind.
#
# AND EACH SUITE IS A REAL GIT REPOSITORY, for the same reason. The corpus is the index now, so a
# fixture that stubbed the index would only prove this module agrees with itself. `git init` plus
# `git add` costs a few milliseconds and buys tests that create an actual untracked file and an
# actual `.gitignore` and let real git decide.
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_GIT, "-C", str(root), *args], capture_output=True, check=True, text=True
    )


def _index_everything(root: Path) -> None:
    """`git add -A` — a no-op until the fixture repo exists, so callers need not care about order.

    Deliberately NOT `-f`: an ignored file must stay out of the index, because that is precisely
    the shape `test_a_gitignored_file_is_excluded_too` exists to pin.
    """
    if (root / ".git").exists():
        _git(root, "add", "-A")



def _fake_installer(non_deploy, *, deploy_commands: bool) -> str:
    lines = [
        f"SKILLS_NON_DEPLOY = {sorted(non_deploy)!r}",
        "",
        "",
        "def _deploy_sources(suite_root):",
        "    out = []",
        "    skills_dir = suite_root / 'skills'",
        "    if skills_dir.is_dir():",
        "        for d in sorted(skills_dir.iterdir()):",
        "            if d.is_dir() and d.name not in SKILLS_NON_DEPLOY:",
        "                out.append(('skills/' + d.name, d))",
    ]
    if deploy_commands:
        lines += [
            "    commands_dir = suite_root / 'commands'",
            "    if commands_dir.is_dir():",
            "        for f in sorted(commands_dir.glob('*.md')):",
            "            out.append(('commands/' + f.name, f))",
        ]
    return "\n".join([*lines, "    return out", ""])


def _install_fake_installer(root: Path, non_deploy, *, deploy_commands: bool = True) -> None:
    (root / "install").mkdir(parents=True, exist_ok=True)
    (root / "install" / "provision.py").write_text(
        _fake_installer(non_deploy, deploy_commands=deploy_commands), encoding="utf-8"
    )
    _index_everything(root)  # tests swap the installer mid-run; the index must follow it


def _synthetic_suite(
    monkeypatch,
    root: Path,
    files: "dict[str, str]",
    *,
    non_deploy: "tuple[str, ...]" = ("_shared",),
    deploy_commands: bool = True,
) -> Path:
    """Write a throwaway suite, COMMIT-TRACK it, and point the module at it. Returns its root."""
    for rel, body in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    _install_fake_installer(root, non_deploy, deploy_commands=deploy_commands)
    _git(root, "init", "-q")
    _index_everything(root)
    monkeypatch.setattr(sys.modules[__name__], "SUITE_ROOT", root)
    return root


def test_an_unbackticked_reference_is_reported(monkeypatch, tmp_path) -> None:
    """gh#144. The gate's own docstring called this its live blind spot; this is its regression.

    Both shapes here are ones actually measured in this suite: a plain prose mention, and a
    markdown-BOLD one (`**../x/SKILL.md**`), which is how sibling references are written
    throughout `_vendored/`. Neither carries a backtick anywhere in the file, asserted below so the
    test cannot quietly start passing through the tier-1 extractor instead.
    """
    body = (
        "Before starting, read docs/REGISTRY.md for the schema.\n"
        "Then follow **../missing-skill/SKILL.md** to the end.\n"
    )
    assert "`" not in body
    _synthetic_suite(monkeypatch, tmp_path, {
        "skills/alpha/SKILL.md": body,
        "docs/REGISTRY.md": "the schema\n",
    })
    assert set(unresolvable()) == {
        ("skills/alpha/SKILL.md", "docs/REGISTRY.md"),
        ("skills/alpha/SKILL.md", "../missing-skill/SKILL.md"),
    }


def test_a_non_shipping_skill_is_judged_from_the_installers_own_set(monkeypatch, tmp_path) -> None:
    """gh#146's detector half, proved by DERIVATION rather than by naming `_shared`.

    Nothing about the corpus, the citation or the filesystem changes between the two halves of
    this test. Only the installer's `SKILLS_NON_DEPLOY` does — and the verdict follows it. A
    hand-written `{"_shared"}` anywhere in this module would pass the first assertion and fail
    the second, which is exactly the drift the derivation exists to prevent.
    """
    files = {
        "skills/alpha/SKILL.md": "the rule is `skills/beta/RULES.md`\n",
        "skills/beta/RULES.md": "rules\n",
    }
    root = _synthetic_suite(monkeypatch, tmp_path, files)
    assert ("skills/alpha/SKILL.md", "skills/beta/RULES.md") not in unresolvable()

    _install_fake_installer(root, ("_shared", "beta"))
    assert ("skills/alpha/SKILL.md", "skills/beta/RULES.md") in unresolvable(), (
        "the installer stopped shipping skills/beta and the gate did not notice — it is reading a "
        "restated set, not SKILLS_NON_DEPLOY"
    )


def test_a_vendored_file_resolves_against_the_roots_a_deployed_reader_has(
    monkeypatch, tmp_path
) -> None:
    """gh#147's detector half. `_vendored/` deploys as ONE directory, and that cuts both ways.

    A sub-skill's neighbourhood is RICHER than a normal skill's — `../other/SKILL.md` and
    `../../alpha/SKILL.md` are both things an installed reader can actually reach, so neither may
    be reported. But a `skills/`-prefixed token written inside `_vendored/` is neither a sibling
    hop nor a known asset directory, and under the old model it was judged by a stat against the
    checkout. Here it is judged against the manifest, which is the only place the fact lives.

    The BARE sibling (`other/SKILL.md`, no `../`) is the third case, and the one that makes this
    test bite. It is the shape of the sweep's single best finding: it resolves only if "the skill
    that owns this file" is taken to be `_vendored/`, the deploy unit, rather than `sub/`, the
    directory whose `SKILL.md` a reader is actually reading. Take the deepest-`SKILL.md` rule out
    and this reference goes green while an installed reader still cannot follow it.
    """
    _synthetic_suite(monkeypatch, tmp_path, {
        "skills/_vendored/sub/SKILL.md": (
            "canon is `skills/_shared/SCHEMA.md`; siblings are `../other/SKILL.md` "
            "and `../../alpha/SKILL.md`, or sometimes written `other/SKILL.md`\n"
        ),
        "skills/_vendored/other/SKILL.md": "other\n",
        "skills/alpha/SKILL.md": "alpha\n",
        "skills/_shared/SCHEMA.md": "schema\n",
    })
    assert {t for f, t in unresolvable() if f == "skills/_vendored/sub/SKILL.md"} == {
        "skills/_shared/SCHEMA.md",
        "other/SKILL.md",
    }


def test_one_destination_spelled_three_ways_is_reported_three_times(monkeypatch, tmp_path) -> None:
    """gh#157, and the reason the whole model changed. THE VERDICT IS THE DESTINATION.

    All three tokens name `skills/_shared/SCHEMA.md`, which `SKILLS_NON_DEPLOY` keeps out of every
    installed tree. Under the previous model `../_shared/SCHEMA.md` PASSED, because the gate stat'd
    it against the checkout — where the file is real, as asserted below — so the spelling picked
    the answer. None of the three is special-cased anywhere in this module: they normalise to one
    destination, and that destination is not in the manifest.
    """
    root = _synthetic_suite(monkeypatch, tmp_path, {
        "skills/alpha/SKILL.md": (
            "canon: `skills/_shared/SCHEMA.md`, also written `_shared/SCHEMA.md`, "
            "also written `../_shared/SCHEMA.md`\n"
        ),
        "skills/_shared/SCHEMA.md": "schema\n",
    })
    assert (root / "skills" / "_shared" / "SCHEMA.md").is_file(), (
        "the destination must EXIST in the checkout for this test to mean anything — that is the "
        "condition under which the old stat-based model returned a wrong green"
    )
    assert {t for f, t in unresolvable() if f == "skills/alpha/SKILL.md"} == {
        "skills/_shared/SCHEMA.md",
        "_shared/SCHEMA.md",
        "../_shared/SCHEMA.md",
    }


def test_the_scanned_corpus_follows_what_the_installer_deploys(monkeypatch, tmp_path) -> None:
    """gh#156. The shipped `commands/*.md` were judged by NOTHING, and they cite paths.

    The second half is the point: the corpus is not "skills plus commands", it is whatever
    `_deploy_sources` emits. Take the commands clause out of the installer and those files stop
    shipping, so they stop being judged — no edit to this module required, in either direction.
    """
    files = {
        "commands/wrapper.md": "Read `docs/REGISTRY.md` first.\n",
        "skills/alpha/SKILL.md": "alpha\n",
        "docs/REGISTRY.md": "the schema\n",
    }
    root = _synthetic_suite(monkeypatch, tmp_path, files)
    assert ("commands/wrapper.md", "docs/REGISTRY.md") in unresolvable()

    _install_fake_installer(root, ("_shared",), deploy_commands=False)
    assert not [f for f, _ in unresolvable() if f.startswith("commands/")], (
        "the installer stopped deploying commands/ and the gate kept scanning them — the corpus is "
        "restated here rather than derived from _deploy_sources"
    )


def test_bare_prose_noise_is_not_reported(monkeypatch, tmp_path) -> None:
    """The widening's whole cost, paid up front. If this test is ever loosened, read gh#144 again.

    Every line is a real shape from this suite's own prose, and the last ones are the hard half:
    with only a head test, `report/ledger`, `solution/plan`, `jr-dev-1/jr-dev-2` and
    `install/scaffold` all pass — the skill-named ones because agentflow names skills after
    ordinary English nouns, and `install/` because it is a repo-root DIRECTORY. Those heads ARE
    real directories.

    The extension requirement on prose tokens is what separates them, and it is a DECISION with a
    measured price, not a free win. `test_the_extension_rule_costs_what_the_docstring_says` states
    what the rule costs; do not restate a cost of zero here, because that is what an earlier
    version of this docstring did and it was false.

    Asserted TWICE on purpose. First against the REAL derived directory names, which is the only
    place those heads exist; then end-to-end, which is what a reader of the gate cares about.
    """
    prose = (
        "Decide and/or escalate within 24h/72h; the split was 3/3.\n"
        "Background: https://example.com/docs/REGISTRY.md and nothing else.\n"
        "Sweep with skills/*/SKILL.md when you need every skill.\n"
        "Raised by jr-dev-1/jr-dev-2, tallied in the report/ledger, owned by solution/plan,\n"
        "and if it breaks, something is wrong with the install/scaffold.\n"
        "PASS/FAIL, pro/con, main/master, stdout/stderr, open/answered.\n"
    )
    assert _candidate_paths(prose) == set(), "prose noise reached candidacy against the real suite"

    _synthetic_suite(monkeypatch, tmp_path, {
        "skills/alpha/SKILL.md": prose,
        "skills/report/SKILL.md": "report\n",
        "skills/solution/SKILL.md": "solution\n",
        "docs/REGISTRY.md": "the schema\n",
    })
    assert unresolvable() == []


def test_a_path_alone_in_a_fence_is_reported(monkeypatch, tmp_path) -> None:
    """A PATH ALONE IN A FENCE — the blind spot that was hiding a live break when it was found.

    Fenced blocks used to be blanked out of the prose pass, and `_BACKTICKED`'s `[^`\\n]` cannot
    span a fence's delimiters, so a path alone in a fence was seen by NEITHER tier. The rationale
    given for that — "none of them is prose telling a reader to open a file" — was falsified by
    the corpus: `_vendored/review-prompts/code-quality-reviewer-prompt.md` spelled ONE destination
    two ways on ADJACENT lines inside one fence, and only the broken spelling was invisible.

    This fixture is that exact shape, reduced. The body carries no backtick outside the fence
    delimiters — asserted, so the test cannot quietly start passing through tier 1 instead — and
    the two spellings must get OPPOSITE verdicts, which is the whole of gh#157 restated inside a
    fence.
    """
    body = (
        "Dispatch the reviewer with this template:\n"
        "\n"
        "```\n"
        "Task tool (../other/prompt.md):\n"
        "  Use template at other/prompt.md\n"
        "```\n"
    )
    assert body.count("`") == 6, "the only backticks here must be the two fence delimiters"
    _synthetic_suite(monkeypatch, tmp_path, {
        "skills/_vendored/sub/SKILL.md": body,
        "skills/_vendored/other/prompt.md": "prompt\n",
    })
    assert {t for f, t in unresolvable() if f == "skills/_vendored/sub/SKILL.md"} == {
        "other/prompt.md"
    }, (
        "a path alone inside a fence must be judged like any other prose token — the sibling "
        "spelling `../other/prompt.md` on the line above it resolves, and reporting neither (or "
        "both) is the failure this test exists to catch"
    )


def test_an_unfamiliar_head_is_the_gates_largest_silence() -> None:
    """A STATED limit, not a discovered one. This test exists so nobody has to find it twice.

    `_is_candidate` applies a HEAD FILTER: a token's first segment must already be a name this
    gate knows. That is what keeps the extractor quiet enough to survive, and it means a
    backticked, extensioned, entirely ordinary broken reference is INVISIBLE if it happens to sit
    under a directory name this suite does not use. `KNOWN_BROKEN` is therefore a FLOOR and not a
    census, which is what the ratchet block says and what an earlier draft of it wrongly widened.

    Pinned rather than merely written down, because a limit nothing asserts is a limit the next
    reader will re-derive from a false negative in production.
    """
    dep = _deployed()
    # The contrast case: identical shape, familiar head, seen.
    assert _candidate_paths("open `docs/REGISTRY.md` first", dep) == {"docs/REGISTRY.md"}
    for token in ("runbooks/deploy.md", "playbooks/x.md", "assets/thing.md"):
        assert _candidate_paths(f"open `{token}` first", dep) == set(), (
            f"{token} reached candidacy — if this suite has grown a directory by that name the "
            "assertion should move to another unfamiliar head, NOT be deleted: the silence is "
            "still there for every head that is still unfamiliar"
        )
    # The `./` silence, same class as gh#59 and named in the module docstring's residual list.
    assert _candidate_paths("`../_shared/SCHEMA.md`", dep) == {"../_shared/SCHEMA.md"}
    assert _candidate_paths("`./../_shared/SCHEMA.md`", dep) == set(), (
        "a './'-spelled token has head '.', which is not a deploy root, not an asset directory "
        "and not '..', so the HEAD FILTER drops it before any destination is computed — the same "
        "mechanism as the unfamiliar heads above, not a rule of its own. Naming a different cause "
        "here would be an attribution nothing tests: deleting './' from _is_candidate's "
        "startswith tuple leaves this assertion green, because the head filter rejects '.' on its "
        "own. So this spelling of a non-shipping destination is never extracted, and that is "
        "recorded as a limit rather than fixed."
    )


def test_a_nonshipping_packages_head_is_admitted_and_the_dotted_form_is_not() -> None:
    """The E-0767 class, pinned on BOTH sides: the slash form must be seen, the dotted form must not.

    `_non_deploy()` makes `_shared` a candidate head, which is what lets `_shared/SCHEMA.md` be
    seen. It does nothing one level down. `agentflow/status_writer.py` names a tracked file at
    `skills/_shared/agentflow/status_writer.py`, `_shared/` never ships, and `agentflow` was a
    name this gate had learned from nowhere — `dirnames` is learned from inside DEPLOYED skills
    and this package lives inside the one skill excluded by definition. So the token never reached
    candidacy, the gate reported nothing, and five deployed orchestrator files told their readers
    to open a path that does not exist under their own root.

    WHY BOTH POLARITIES. Admitting the head is only half a fix: if the dotted `agentflow.status_writer`
    were ALSO extracted, the corrected spelling would report as a break and the next reader would
    allowlist the class back into silence. The two assertions together are what make the fix
    directional. They fail for different reasons — the first if the head derivation is dropped,
    the second if the extractor ever starts splitting on dots — so neither substitutes for the other.

    AND IT IS DERIVED, so the assertion below is about a mechanism rather than a name. Reverting
    `_nonshipping_packages` to a `{"agentflow"}` literal keeps this test green; what it would break
    is the guarantee that a SECOND package added under `_shared/` is covered the day it lands. That
    is asserted separately, by deriving the expected set here from the index the same way.
    """
    dep = _deployed()
    packages = _nonshipping_packages()
    assert packages, (
        "no importable package was found under any SKILLS_NON_DEPLOY skill — this derivation feeds "
        "a head filter, so an empty set silently restores the E-0767 silence rather than failing"
    )
    assert packages <= dep.asset_dirnames, (
        f"{sorted(packages - dep.asset_dirnames)} is an importable package inside a non-shipping "
        "skill and is NOT a candidate head, so a slash-spelled citation of anything inside it is "
        "invisible to this gate — the exact E-0767 silence"
    )
    # The derivation is a mechanism, not a name: every package it finds must behave this way.
    for pkg in sorted(packages):
        slash = f"{pkg}/status_writer.py"
        assert _candidate_paths(f"see `{slash}`", dep) == {slash}, (
            f"{slash} did not reach candidacy; the head filter is rejecting a package this "
            "repo ships inside its non-shipping skill"
        )
        assert not _resolves("skills/orchestrator/SKILL.md", slash, dep), (
            f"{slash} RESOLVED for a deployed reader — if `_shared/` has started shipping, this "
            "whole epic's premise is gone and the gate should be re-derived, not re-pointed"
        )
        # The corrected spelling, and the rooted spelling, are both silent by construction.
        assert _candidate_paths(f"see `{pkg}.status_writer`", dep) == set(), (
            f"the dotted `{pkg}.status_writer` reached candidacy — the corrected form would now "
            "report as a break, which is how a fixed class gets allowlisted back into silence"
        )
        assert _candidate_paths(f"see `<suite_root>/skills/_shared/{pkg}/status_writer.py`", dep) == set()


def test_the_owning_skill_root_is_load_bearing() -> None:
    """Root 2 of `_resolves` earns its place, and the corpus is asked rather than a number typed.

    `_owning_skill_root`'s docstring used to say HOW MANY references depend on this root. It named
    two of the citing directories, missed a third, and the resulting total then read as checked —
    which is the defect this file keeps producing whenever a measurement is written down instead
    of computed. So the set is computed here and the docstring names the shapes.

    Every pair collected below resolves against the skill that owns the referring file and against
    NOTHING else a deployed reader has. Delete root 2 from `_resolves` and each becomes a wrong
    red: an instruction that an installed user can follow, reported as a break.
    """
    dep = _deployed()

    def _resolves_without_the_owning_root(dep_path: str, token: str) -> bool:
        for root in (posixpath.dirname(dep_path), ""):
            dest = posixpath.normpath(posixpath.join(root, token))
            if dest == ".." or dest.startswith("../"):
                continue
            if dest in dep.manifest:
                return True
        return False

    depends_on_owner: list[tuple[str, str]] = []
    for dep_path, f in dep.files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for tok in sorted(_candidate_paths(text, dep)):
            if _resolves(dep_path, tok, dep) and not _resolves_without_the_owning_root(
                dep_path, tok
            ):
                depends_on_owner.append((dep_path, tok))

    assert depends_on_owner, (
        "no reference in the corpus resolves only via the owning skill root any more, so root 2 "
        "of `_resolves` is currently doing nothing measurable and the docstring above it claims "
        "otherwise. Do not delete the root on that evidence alone — a skill-root-relative "
        "reference is a shape this suite writes constantly and one will be back — but DO make the "
        "prose match, because a rationale nothing exercises is the next thing to go stale."
    )


def test_the_extension_rule_costs_what_the_docstring_says() -> None:
    """The prose/fence extension rule is a TRADE, and this recomputes BOTH sides of it.

    An earlier version of this file said the rule removed the alternation noise "without costing a
    single true positive", at "a measured recall cost of zero on this corpus". Neither was true and
    nothing checked. So the check lives here: run the real corpus through the extractor twice, with
    the rule and without, and assert that everything the rule SUPPRESSES is one of the shapes the
    docstrings name.

    A shape outside that set appearing here means the trade has changed and the prose describing it
    is now wrong. Correct the prose, or drop the rule. Do not widen this test's allowance — that is
    how "costs zero" got written the first time.

    THE ONE LEGITIMATE WIDENING, because gh#171 performed it and a blanket prohibition would have
    forced the next reader to choose between obeying this docstring and doing the right thing.
    `alternations` is BOTH the lower bound on side one and the only exit from side two's exhaustive
    partition, so adding to it really does widen the allowance — the instruction above is not
    pedantry. It is legitimate ONLY when the new token is the same SHAPE the rule already exists to
    remove, the addition is argued at the literal rather than dropped into it, and the token's
    source is named so a later reader can re-judge it. `report/summarized_at_sha` qualifies: it is
    two dict keys slash-joined in a comment at `communities.py:729`, structurally identical to
    `report/ledger`, and it appeared only because gh#171 made `.py` readable at all.

    A token that merely LOOKS unresolvable is not the same shape and does not qualify. If you are
    reaching for this paragraph to excuse a real broken path, the answer is still to fix the path.
    """
    dep = _deployed()
    # Suppressed OFFENDERS, not suppressed tokens: a token the rule hides but which would have
    # resolved anyway costs nothing, and counting it would overstate the price in the other
    # direction.
    suppressed: dict[str, set[str]] = {}
    for dep_path, f in dep.files:
        text = f.read_text(encoding="utf-8", errors="replace")
        strict = _candidate_paths(text, dep)
        wide = _candidate_paths(text, dep, require_prose_extension=False)
        for tok in wide - strict:
            if not _resolves(dep_path, tok, dep):
                suppressed.setdefault(tok, set()).add(dep_path)

    # SIDE ONE — the noise the rule exists to remove. Most of these heads are skills named after
    # ordinary English nouns; `install/` is a repo-root DIRECTORY, which is why "named after
    # nouns" alone never explained the whole set. This literal IS the count the docstrings refer
    # to, which is why they name the test instead of typing a number.
    #
    # `report/summarized_at_sha` ARRIVED WITH gh#171 and is the same shape in a different
    # language: two dict keys slash-joined in a code comment in `autodoc_graph/communities.py`,
    # a file nothing read until `_READABLE` grew to cover shipped `.py`. It is noise for exactly
    # the reason the others are — head `report/` is a skill named after an ordinary noun — so
    # it belongs on THIS side of the partition rather than in SIDE TWO's allowance. Source
    # comments join identifiers with a slash at least as readily as prose joins nouns, which is
    # a cost of reading code worth naming here rather than leaving as a puzzle for the next
    # widening.
    #
    # `agentflow/vault` ARRIVED WITH E-0767, which admitted `agentflow` as a candidate head so the
    # slash-spelled module citations could be reported at all. Source: `autodoc_graph/
    # sqlite_backend.py` line 3, the module docstring's opening sentence "The agentflow/vault
    # backend." Two nouns slash-joined in English prose - the same shape as `solution/plan` - and
    # named here rather than dropped in, because a head widening pays its price on THIS side of
    # the partition and the price should be legible as a price.
    alternations = {
        "report/ledger",
        "solution/plan",
        "install/scaffold",
        "report/summarized_at_sha",
        "agentflow/vault",
    }
    assert alternations <= set(suppressed), (
        "the extension rule stopped suppressing " + repr(sorted(alternations - set(suppressed)))
        + " — either the prose changed or the filter did; the docstrings name this set"
    )

    # SIDE TWO — the price, as an EXHAUSTIVE partition. Every remaining suppressed offender must
    # fall into one of the named shapes below, all of which the docstrings describe:
    #
    #   * a `file:symbol` code citation, which is a win rather than a cost;
    #   * a possessive artifact (`.../SKILL.md's`);
    #   * upstream text inside `_vendored/`, which this suite carries but does not author.
    #
    # A shape outside that list means the rule is now hiding an instruction this suite DOES own,
    # and every docstring describing its cost is understating it.
    for token, sites in sorted((t, s) for t, s in suppressed.items() if t not in alternations):
        shape = (
            ":" in token.rsplit("/", 1)[-1]
            or token.endswith("'s")
            or all(s.startswith("skills/_vendored/") for s in sites)
        )
        assert shape, (
            f"{token} (at {sorted(sites)}) is suppressed by the extension rule and is NONE of the "
            "shapes the docstrings account for — not a file:symbol citation, not a possessive "
            "artifact, not upstream text inside _vendored/. Correct the stated cost or drop the "
            "rule; do not widen this allowance."
        )


def _deployed_files_only(dep: _Deployed) -> set[str]:
    """The manifest's FILES — every entry that is not the prefix of another.

    Derived from the MANIFEST and not from `dep.files`, which is the point: `dep.files` is
    filtered BY `_READABLE`, so asking it what `_READABLE` ought to contain would be circular and
    would pass against any set at all. The manifest holds files and directories together, and git
    tracks no empty directory — so an entry nothing is nested under is a file.
    """
    return {p for p in dep.manifest if not any(q.startswith(f"{p}/") for q in dep.manifest)}


def test_the_readable_set_reaches_every_shipped_py_sh_and_extensionless_file() -> None:
    """gh#171. The gate used to reason about a class of file it did not read. PINNED, not written.

    `_READABLE` was `{".md", ".yaml", ".yml", ".txt"}`, so every shipped `.py`, `.sh` and
    extensionless script was extracted from by NOTHING — including
    `_vendored/subagent-driven-development/scripts/*`, which the module docstring cites as this
    gate's motivating extensionless case. The set was widened, the five real citations it found
    were repaired, and this is what stops a later tidy-up narrowing it back on the reading that
    the literal is arbitrary. It is not: every member is reached by a file this suite ships.

    BOTH HALVES ARE DERIVED FROM THE INDEX, never listed here. A hand-written roster of expected
    files would go stale the first time a skill grows a script and would then read as checked —
    the failure every typed number in this file's history has eventually had.

    IT ASSERTS THE CONSEQUENCE, NOT THE LITERAL, deliberately. Extensionless is spelled `""`
    because that is what `Path.suffix` returns and what `_deployed()`'s two call sites test; a
    refactor expressing "no extension" some other way should keep this green, while any change
    that loses the REACH must red it. Asserting `"" in _READABLE` would have inverted that.
    """
    dep = _deployed()
    scanned = {f for f, _ in dep.files}
    shipped = _deployed_files_only(dep)

    # THE MOTIVATING CASE, named because the module docstring names it. If these ever stop
    # shipping, re-point the assertion at whatever the docstring cites then — do not delete it.
    motivating = {
        p
        for p in shipped
        if p.startswith("skills/_vendored/subagent-driven-development/scripts/")
    }
    assert motivating, (
        "the extensionless scripts this module's docstring cites as its motivating case no longer "
        "ship, so this pin is now asserting about an empty set. Re-point it at the case the "
        "docstring cites now; deleting it restores the silence gh#171 closed."
    )
    assert motivating <= scanned, (
        "shipped extensionless scripts are outside the scan corpus: "
        + repr(sorted(motivating - scanned))
        + " — `_READABLE` has been narrowed and this gate is once again reasoning about a class "
        "of file it does not open, which is exactly gh#171."
    )

    # AND THE CLASS THEY BELONG TO. Every shipped executable outside a test tree, same treatment.
    # A group that empties is a re-point, not a pass: an empty `<=` is vacuously true, which is how
    # a pin stops pinning without anyone noticing.
    for suffix in (".py", ".sh", ""):
        group = {
            p
            for p in shipped
            if posixpath.splitext(p)[1] == suffix and not _in_test_tree(p)
        }
        assert group, (
            f"nothing with suffix {suffix!r} ships outside a test tree any more, so this leg "
            "asserts nothing. Re-point it, or delete the leg deliberately — do not leave it "
            "green and empty."
        )
        assert group <= scanned, (
            f"shipped {suffix or 'extensionless'} files are outside the scan corpus: "
            + repr(sorted(group - scanned))
            + " — `_READABLE` must keep covering the executables this suite ships (gh#171)."
        )


def test_an_empty_ratchet_still_fails_on_a_broken_fixture(monkeypatch, tmp_path) -> None:
    """The gate must fail from a STANDING START, with nothing excused and nothing recorded.

    `KNOWN_BROKEN` is empty today, and an empty allowlist is indistinguishable from a broken
    detector unless something proves the difference. Both lists are emptied here explicitly rather
    than relying on their live contents, so this stays a proof about the GATE the day someone
    legitimately records an exemption.
    """
    monkeypatch.setattr(sys.modules[__name__], "KNOWN_BROKEN", {})
    monkeypatch.setattr(sys.modules[__name__], "NOT_A_PATH", {})
    _synthetic_suite(monkeypatch, tmp_path, {
        "skills/alpha/SKILL.md": "seed it from `templates/registry.md` before the first run\n",
        "templates/registry.md": "the template\n",
    })
    with pytest.raises(AssertionError, match=r"templates/registry\.md"):
        test_no_deployed_skill_references_a_path_its_reader_cannot_find()


def test_an_empty_ratchet_still_proves_the_extractor_sees_each_class() -> None:
    """Ratchet-independent canaries, one per class this change added. Same lesson as `templates/`.

    When `KNOWN_BROKEN` emptied, `test_each_known_break_is_still_a_break` stopped supplying — as a
    side effect nobody had noticed it was supplying — the only standing proof that the extractor
    could still SEE a `templates/...` token at all. Review mutation-proved the consequence: kill
    the head and the whole headline class goes invisible with the suite green.

    So every class this widening added gets the same treatment, asserted against the REAL suite and
    independent of what the ratchet holds. The fixtures above prove the verdicts end-to-end; these
    prove the extractor's REACH, which is the half an emptied list stops covering.
    """
    dep = _deployed()
    # BARE PROSE (gh#144) — no backticks, no markdown link, nothing to anchor on but the shape.
    assert "docs/REGISTRY.md" in _candidate_paths("open docs/REGISTRY.md before starting", dep)
    # A MARKDOWN LINK, where `_vendored`'s truest positives live and where `(` in _SKIP_SUBSTRINGS
    # would otherwise kill the token on this module's own filter.
    assert "../requesting-code-review/code-reviewer.md" in _candidate_paths(
        "see [code-reviewer.md](../requesting-code-review/code-reviewer.md) for the prompt", dep
    )
    # THREE SPELLINGS, ONE DESTINATION (gh#157) — all three must reach candidacy, or the fixture
    # above would be proving the extractor's silence rather than the resolver's judgement.
    assert {"skills/_shared/SCHEMA.md", "_shared/SCHEMA.md", "../_shared/SCHEMA.md"} <= (
        _candidate_paths(
            "`skills/_shared/SCHEMA.md` `_shared/SCHEMA.md` `../_shared/SCHEMA.md`", dep
        )
    )
    # A PATH ALONE IN A FENCE. Neither tier could reach this before: the
    # prose pass had fences blanked out, and `_BACKTICKED` cannot span a fence's own delimiters.
    assert "docs/REGISTRY.md" in _candidate_paths("```\ndocs/REGISTRY.md\n```\n", dep)
    # THE CORPUS (gh#156) — commands ship, so commands are scanned; `_vendored` too, which no
    # SKILL.md-shaped sweep reaches.
    assert any(f.startswith("commands/") for f, _ in dep.files)
    assert any(f.startswith("skills/_vendored/") for f, _ in dep.files)


def test_the_corpus_is_the_index_not_whatever_is_lying_in_the_tree(monkeypatch, tmp_path) -> None:
    """THE PROPERTY. One commit, one corpus — whatever else the working tree happens to hold.

    This is the whole of the fix stated as an invariant, and it is built rather than mocked: a real
    repository, a real untracked file, real `git ls-files`. The file is written under a SHIPPED
    skill, carries a `.md` extension the corpus scans, and names a destination that does not
    resolve — so under the old `rglob` corpus it produced an offender out of nothing anyone
    committed.

    THE THREE STEPS MATTER IN ORDER, because two of them are the ways this test could pass while
    proving nothing:

    1. the stray must be REACHABLE BY A TREE WALK — asserted directly against `rglob`, the exact
       call the fix removed. Without this the test could be green because the fixture was inert;
    2. the corpus, manifest, asset names and verdict must all be UNCHANGED by its presence;
    3. `git add` it and the corpus must CHANGE. Otherwise "unchanged" might mean the gate stopped
       looking at that subtree at all, which would be requirement 3's failure — a quiet loss of
       reach — wearing this fix's clothes.
    """
    root = _synthetic_suite(monkeypatch, tmp_path, {
        "skills/alpha/SKILL.md": "read `docs/REGISTRY.md` first\n",
        "docs/REGISTRY.md": "the schema\n",
    })
    before = _deployed()
    assert before.files, "the fixture corpus is empty; nothing below would mean anything"
    baseline_offenders = unresolvable()

    stray = root / "skills" / "alpha" / "STRAY.md"
    stray.write_text("now open `docs/NOT-SHIPPED.md`\n", encoding="utf-8")

    # (1) a tree walk — the removed implementation — would have read it.
    assert stray in set((root / "skills" / "alpha").rglob("*"))
    assert not _git(root, "ls-files", "skills/alpha/STRAY.md").stdout.strip(), "must be UNTRACKED"

    # (2) the index did not move, so nothing the gate derives may move either.
    after = _deployed()
    assert after.files == before.files, "an untracked file entered the CORPUS"
    assert after.manifest == before.manifest, "an untracked file entered the MANIFEST"
    assert after.asset_dirnames == before.asset_dirnames
    assert after.extensions == before.extensions
    assert unresolvable() == baseline_offenders, "an untracked file changed the VERDICT"

    # (3) and the subtree is still genuinely in reach — track it and the gate must see it.
    _index_everything(root)
    assert ("skills/alpha/STRAY.md", "docs/NOT-SHIPPED.md") in unresolvable(), (
        "tracking the file did not bring it into the corpus, so 'unchanged' above was the gate "
        "having lost reach into that subtree rather than the index deciding what it holds"
    )


def test_the_real_untracked_build_artifacts_are_not_instructions(monkeypatch, tmp_path) -> None:
    """The regression, in the SHAPES that actually did it. Not a reduced stand-in.

    Both live under `skills/autodoc-init/references/` on any machine that has run the install:
    `graph/*.egg-info/SOURCES.txt` and `PKG-INFO` from a `pip install -e`, and
    `inventory/node_modules/**/README.md` from an `npm ci`. Their content is why they bite — a
    `SOURCES.txt` is nothing BUT relative paths, and an upstream README is full of prose paths —
    and the widening in gh#144 is what first gave them a voice: before it, extraction was
    backtick-only, so prose inside a vendored README produced no candidate at all. The corpus was
    always tree-based; the widening turned a latency into a red suite.

    Tracking them at the end is what stops this being a test of an inert fixture: the same two
    files, once in the index, must be ordinary corpus members and be JUDGED like any other.
    """
    root = _synthetic_suite(monkeypatch, tmp_path, {
        "skills/alpha/SKILL.md": "alpha\n",
        "docs/REGISTRY.md": "the schema\n",
    })
    clean = _deployed()

    egg = root / "skills" / "alpha" / "graph" / "autodoc_graph.egg-info"
    egg.mkdir(parents=True)
    (egg / "SOURCES.txt").write_text(
        "pyproject.toml\nautodoc_graph/adapter.py\nautodoc_graph.egg-info/PKG-INFO\n",
        encoding="utf-8",
    )
    vendored = root / "skills" / "alpha" / "inventory" / "node_modules" / "tree-sitter-wasms"
    vendored.mkdir(parents=True)
    (vendored / "README.md").write_text(
        "Prebuilt WASM binaries. See [the guide](docs/MISSING.md) before importing.\n",
        encoding="utf-8",
    )

    assert _deployed() == clean, (
        "an untracked *.egg-info/ or node_modules/ under a shipped skill changed what this gate "
        "scans — the exact defect: a red suite that `git status` cannot explain and CI, which "
        "checks out tracked files only, cannot reproduce"
    )
    assert unresolvable() == []

    _index_everything(root)
    readme = "skills/alpha/inventory/node_modules/tree-sitter-wasms/README.md"
    assert {"skills/alpha/graph/autodoc_graph.egg-info/SOURCES.txt", readme} <= {
        f for f, _ in _deployed().files
    }, "tracking the artifacts did not put them in the corpus, so the exclusion above proved nothing"
    assert (readme, "docs/MISSING.md") in unresolvable(), (
        "and once in the corpus their content must be JUDGED — otherwise 'excluded' above was the "
        "gate being unable to see that subtree at all rather than the index deciding what it holds"
    )


def test_a_gitignored_file_is_excluded_too(monkeypatch, tmp_path) -> None:
    """The real artifacts are IGNORED — which is what made the defect invisible, not merely wrong.

    `git ls-files` already excludes them by excluding everything untracked, so today this is
    covered twice over. It is pinned anyway, because the obvious future "improvement" —
    `ls-files --others` to catch a file someone forgot to add — silently reinstates exactly this
    bug, and would do it while every other test in this module stayed green.

    The clean-status assertion is the defect's signature, and the reason it deserves its own test:
    a lane facing a red suite and an empty `git status --porcelain` has no signal to diagnose from,
    and starts by suspecting its own work.
    """
    root = _synthetic_suite(monkeypatch, tmp_path, {"skills/alpha/SKILL.md": "alpha\n"})
    clean = _deployed()

    (root / ".gitignore").write_text("*.egg-info\nnode_modules/\n", encoding="utf-8")
    junk = root / "skills" / "alpha" / "thing.egg-info"
    junk.mkdir()
    (junk / "SOURCES.txt").write_text("open docs/NOT-SHIPPED.md\n", encoding="utf-8")
    _index_everything(root)  # picks up .gitignore; must NOT pick up what it ignores

    assert "egg-info" not in _git(root, "status", "--porcelain").stdout, (
        "the fixture must reproduce the INVISIBILITY half of the defect — an artifact `git status` "
        "does not mention at all — or it is testing an ordinary untracked file, which the test "
        "above already covers"
    )
    assert "skills/alpha/thing.egg-info/SOURCES.txt" in _git(
        root, "ls-files", "--others"
    ).stdout, "the file must be one `--others` WOULD surface, or this pins nothing"
    assert _deployed().files == clean.files
    assert _deployed().manifest == clean.manifest
    assert unresolvable() == []


def test_no_git_fails_loudly_instead_of_falling_back_to_a_tree_walk(monkeypatch, tmp_path) -> None:
    """Requirement 4, and it is not defensive programming — a fallback would BE the bug.

    A working-tree fallback reinstates the defect on precisely the machines where it is hardest to
    see, and does it with the suite green. So both ways git can fail to answer must raise, and the
    message must name the cause rather than leaving a reader with an empty corpus and a pass.

    Neither half mocks this module. The first points `_GIT` at a binary that is not installed; the
    second points `SUITE_ROOT` at a directory that is genuinely not a work tree.
    """
    real_git = _GIT  # captured BEFORE the swap; reading it after would read the swapped value
    root = _synthetic_suite(monkeypatch, tmp_path / "suite", {"skills/alpha/SKILL.md": "alpha\n"})
    assert _deployed().files, "the fixture must work before its failure modes mean anything"

    monkeypatch.setattr(sys.modules[__name__], "_GIT", "git-that-is-not-installed")
    with pytest.raises(AssertionError, match="could not run"):
        _deployed()
    monkeypatch.setattr(sys.modules[__name__], "_GIT", real_git)

    outside = tmp_path / "not-a-repo"
    (outside / "skills" / "alpha").mkdir(parents=True)
    (outside / "skills" / "alpha" / "SKILL.md").write_text("alpha\n", encoding="utf-8")
    _install_fake_installer(outside, ("_shared",))
    monkeypatch.setattr(sys.modules[__name__], "SUITE_ROOT", outside)
    with pytest.raises(AssertionError, match="ls-files"):
        _deployed()
    assert root.is_dir()  # the working suite is untouched; only the pointer moved


def test_an_empty_index_fails_loudly_rather_than_scanning_nothing(monkeypatch, tmp_path) -> None:
    """The third branch, and the only one that would otherwise turn into a PASS rather than a crash.

    A missing git raises and a non-work-tree raises, so both are already loud. An empty index is
    the dangerous one: `ls-files` SUCCEEDS and answers nothing, every derived set comes back empty,
    the gate scans no file and reports no offender — green, and indistinguishable from a gate that
    works. This module's own docstring opens by saying a gate that overstates itself is worse than
    none; scanning nothing while reporting success is the purest form of that.

    Asserted against `_tracked_paths` rather than `_deployed`, so removing the guard reds this test
    for the right reason instead of tripping over some later missing-file error.
    """
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    _git(empty, "init", "-q")
    assert not _git(empty, "ls-files").stdout.strip(), "the fixture must have an EMPTY index"

    monkeypatch.setattr(sys.modules[__name__], "SUITE_ROOT", empty)
    with pytest.raises(AssertionError, match="EMPTY index"):
        _tracked_paths()
