"""No first-party gate may derive its corpus by walking the working tree (gh#171, gh#175).

WHAT THIS GATE IS FOR, AND WHY A LIST WOULD NOT HAVE DONE. gh#171 fixed one check. gh#175 was
filed as "five more"; running this detector over master found **11 modules and 25 walk sites**.
The six the filing missed are missed for a reason a list cannot fix: two alias the root through a
function-local (`home = SKILLS_ROOT / loop`), and two derive it from an IMPORTED module
(`Path(config.__file__).parent`) rather than from this file's own `__file__`. A hand list is only
ever as complete as what somebody thought of; a derived set makes the next site red on the commit
that adds it -- and it did exactly that while this gate was being written, catching an eleventh
module the author's own probe had missed.

THE DEFECT THE CLASS PRODUCES. A corpus from `rglob` is a verdict about whatever is lying in one
developer's checkout, not about the commit. It fails as **`git status --porcelain` silent and the
suite red at the same time** -- no git signal to diagnose from, the natural next move is to bisect
your own work, and CI cannot reproduce any of it because CI checks out tracked files only.
Measured on this repo while writing the fix: `skills/_shared/.pytest_cache/README.md`, generated
by running pytest itself and gitignored, was inside a live gate corpus.

WHAT IT DOES NOT CLAIM, stated because a gate that overstates itself is worse than none.

*Its corpus is every tracked `.py` under the three CI pytest trees -- `skills/_shared/tests/`,
`skills/autodoc-init/references/graph/tests/`, `install/` -- plus `harness/`, recursively, and
nothing else* (gh#410 widened it from `_shared` alone). Shipped package code outside those trees
is not examined. The gate only parses files and never imports them, so a tree whose suite cannot
import `agentflow` is still in reach: the graph suite carries its own stdlib `tracked_files`
(`graph/tests/tracked.py`), which the detector does not need to recognise because a converted
site has no walk left to flag. A walk WRAPPED rather than replaced is recognised only as a
bare-name call to `only_tracked`/`tracked_files`.

*One check (C3) reads the working tree on purpose: two walks, `harness/check.py`'s before and
after `FIXTURES_DIR.rglob('*')` snapshots, exempted by expression AND count, not by module*. C3
diffs them to catch a status board the parse CREATED -- a file that is untracked by
construction, so an index-derived snapshot would make that diff permanently empty and C3 a
guaranteed PASS. A third copy of the same expression anywhere in the module is a finding.

*It is a static check over walk EXPRESSIONS*, so it proves a repo-rooted walk is handed to
`only_tracked`/`tracked_files`, not that the result is then used.

*Four bindings still escape it*: a root reached through a function PARAMETER, a `for` target, a
walrus, and a comprehension variable. Each was measured against the detector rather than assumed.
Widening to them means dataflow rather than name binding, which is a different tool; the honest
position is that this gate raises the cost of the mistake without making it impossible.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agentflow.tracked_paths import tracked_paths

SKILLS_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = SKILLS_ROOT.parent

#: The trees this gate examines: the three CI pytest trees (`.github/workflows/ci.yml`) plus
#: `harness/`, whose scripts CI runs as gates of their own.
CORPUS_PREFIXES = (
    "skills/_shared/tests/",
    "skills/autodoc-init/references/graph/tests/",
    "install/",
    "harness/",
)

#: The two call shapes that make a walk a statement about the commit.
WRAPPERS = frozenset({"only_tracked", "tracked_files"})

#: Filesystem enumerations, by the shape the root appears in: the receiver for a Path method,
#: the first argument for the `os` functions. `listdir`/`scandir` are here because a detector
#: that only knew `glob` would teach the next author which spelling evades it.
_METHOD_WALKS = frozenset({"glob", "rglob", "iterdir"})
_OS_WALKS = frozenset({"walk", "listdir", "scandir"})

#: Modules allowed to keep a raw repo-rooted walk, each with the reason it is still here.
#: An entry whose module no longer has a raw walk is refused below -- an exemption that outlives
#: its reason is how a list stops describing the tree it guards.
#: Keys are repo-relative paths.
EXEMPT = {
    "skills/_shared/tests/test_skill_call_sites.py": (
        "ten raw walks, and this file is held by another epic's uncommitted work at the time "
        "gh#175 shipped (lead's E-0743 seam). It is the one site the issue names as ALREADY "
        "scanning an untracked file, so the entry is a debt with an owner, not a decision: "
        "converting it is the gh#175 remainder and this exemption is what makes that visible."
    ),
}

#: Walk EXPRESSIONS that must read the working tree because observing it is their job, each with
#: the exact number of sites allowed to carry it. Narrower than EXEMPT on purpose: any other raw
#: walk in the same module is still a finding, and so is one copy too many of an exempted
#: expression -- keyed on text alone, a new copy pasted anywhere in the module would pass.
EXEMPT_SITES = {
    ("harness/check.py", "FIXTURES_DIR.rglob('*')"): (2, (
        "C3's before/after snapshot of the fixtures tree. It exists to catch a status board the "
        "malformed parse CREATED, and a created file is untracked by construction -- an "
        "index-derived snapshot would make `created` permanently empty and C3 a guaranteed PASS. "
        "The working tree is the thing under observation here, not a corpus."
    )),
}


def _root_name(node: ast.AST) -> str | None:
    """The leftmost Name of a path chain: `(REPO_ROOT / 'docs').rglob` -> `REPO_ROOT`."""
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.BinOp):
            node = node.left
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            return node.id if isinstance(node, ast.Name) else None


def _is_path_shaped(node: ast.AST) -> bool:
    """Only PATH ALGEBRA propagates the alias: `A / b`, `A.parent`, `A.parents[n]`, `A.resolve()`.

    Without this restriction `text = path.read_text()` makes `text` a repo root and the alias set
    grows to every local in the module -- ninety names in one file, measured while building this.
    A set that wide is how a gate starts reporting on code it never examined.
    """
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return True
    if isinstance(node, ast.Attribute) and node.attr in ("parent", "parents"):
        return True
    if isinstance(node, ast.Subscript):
        return _is_path_shaped(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in ("resolve", "absolute", "joinpath")
    return False


def _repo_rooted_names(tree: ast.Module) -> set[str]:
    """Names bound to a path derived from some `__file__`, at ANY scope, to a fixpoint.

    Any scope, because two of the real sites alias inside a function. Any `__file__` and not only
    this module's, because `Path(config.__file__).parent` is a repo root reached through the
    import system -- and that one was invisible to the first version of this detector.
    """
    names: set[str] = set()
    for _ in range(6):
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target = node.target  # `ROOT: Path = ...` binds exactly as `ROOT = ...` does
            else:
                continue
            if not isinstance(target, ast.Name) or target.id in names:
                continue
            derived = "__file__" in ast.unparse(node.value)
            aliased = _root_name(node.value) in names and _is_path_shaped(node.value)
            if derived or aliased:
                names.add(target.id)
                grew = True
        if not grew:
            break
    return names


def raw_walks(source: str) -> list[tuple[int, str]]:
    """(line, expression) for every repo-rooted walk NOT handed to a wrapper."""
    tree = ast.parse(source)
    roots = _repo_rooted_names(tree)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in _METHOD_WALKS:
            receiver = node.func.value
        elif (isinstance(node.func, ast.Attribute) and node.func.attr in _OS_WALKS
                and node.args):
            receiver = node.args[0]  # `os.walk(ROOT)` / `os.listdir(ROOT)` - root is the ARG
        else:
            continue
        if _root_name(receiver) not in roots:
            continue
        cur, wrapped = node, False
        while cur in parents:
            cur = parents[cur]
            if (isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name)
                    and cur.func.id in WRAPPERS):
                wrapped = True
                break
        if not wrapped:
            out.append((node.lineno, ast.unparse(node)[:100]))
    return sorted(out)


def _test_modules(root: Path = SUITE_ROOT) -> list[str]:
    """Every tracked module in the corpus trees, repo-relative. This gate's corpus is the index too.

    RECURSIVE, and the parameter exists so that is provable: pytest recurses under `testpaths`,
    so a gate module one directory down is a live gate and must be examined.
    """
    return sorted(
        rel for rel in tracked_paths(root)
        if rel.endswith(".py") and rel.startswith(CORPUS_PREFIXES)
    )


# --------------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------------
def test_no_unexempted_module_derives_a_gate_corpus_from_the_working_tree() -> None:
    offenders = []
    examined = 0
    for rel in _test_modules():
        if rel in EXEMPT:
            continue
        examined += 1
        seen: dict[str, list[int]] = {}
        for lineno, expr in raw_walks((SUITE_ROOT / rel).read_text(encoding="utf-8")):
            seen.setdefault(expr, []).append(lineno)
        for expr, lines in seen.items():
            allowed = EXEMPT_SITES.get((rel, expr), (0, ""))[0]
            if len(lines) > allowed:
                # Every line is reported: which copy is the "extra" one is not knowable here.
                offenders.extend(
                    f"{rel}:{lineno}  {expr}"
                    + (f"  ({len(lines)} sites, {allowed} exempted)" if allowed else "")
                    for lineno in lines
                )
    assert examined > 100, (
        f"only {examined} modules were examined - a gate whose exemptions have eaten its corpus "
        "reports the same green as one with nothing to find"
    )
    assert not offenders, (
        "these walk a path derived from __file__ -- a REPO root, not a tmp_path fixture -- and "
        "so make the verdict a function of this checkout rather than of the commit:\n  "
        + "\n  ".join(offenders)
        + "\n\nDerive the corpus from the index: `tracked_files(<root>, <predicate>)` or "
        "`only_tracked(<root>, <the walk>)` from agentflow.tracked_paths (in the graph suite, "
        "which never installs agentflow: `tests.tracked.tracked_files`). "
        "Do NOT reach for a git pathspec instead: git's globbing is fnmatch without FNM_PATHNAME, "
        "so `skills/*/SKILL.md` crosses `/` and returns the vendored tree as well."
    )


def test_the_corpus_of_this_gate_is_not_empty() -> None:
    """A gate that scans nothing passes, and reads exactly like a gate that works."""
    modules = _test_modules()
    assert len(modules) > 100, f"only {len(modules)} modules discovered - scanning a stub"
    # One named module per corpus tree: a prefix that silently stopped matching would shrink
    # the corpus while the count above stayed comfortably over its floor.
    for rel in (
        "skills/_shared/tests/test_config.py",
        "skills/_shared/tests/test_skill_call_sites.py",
        "skills/autodoc-init/references/graph/tests/test_query_no_storage_import.py",
        "install/test_install_surface.py",
        "harness/check.py",
    ):
        assert rel in modules, f"{rel} is missing from this gate's corpus"


@pytest.mark.parametrize("name, why", sorted(EXEMPT.items()))
def test_every_exemption_still_names_a_real_raw_walk(name: str, why: str) -> None:
    """An exemption is a debt, and a paid debt must leave the ledger.

    If the named module is gone, or has been converted, this entry survives as a permanent hole
    that a future raw walk in that same file would fall straight through.
    """
    path = SUITE_ROOT / name
    assert path.is_file(), f"{name} is exempted but does not exist - delete the entry"
    assert raw_walks(path.read_text(encoding="utf-8")), (
        f"{name} no longer has a raw repo-rooted walk, so its exemption is now a hole rather "
        f"than a debt. Delete the entry. Its stated reason was: {why}"
    )
    assert len(why) > 80, "an exemption without a real reason is an exclusion list"


@pytest.mark.parametrize(
    "site, count, why", sorted((site, n, why) for site, (n, why) in EXEMPT_SITES.items())
)
def test_every_site_exemption_still_names_a_real_raw_walk(
    site: tuple[str, str], count: int, why: str
) -> None:
    """Same ledger rule, per expression, at the exact count: a site that has moved or been
    converted must leave, and a lowered count must be lowered here too -- a slack count is room
    for a new copy to land unseen."""
    rel, expr = site
    path = SUITE_ROOT / rel
    assert path.is_file(), f"{rel} is exempted but does not exist - delete the entry"
    found = sum(1 for _line, e in raw_walks(path.read_text(encoding="utf-8")) if e == expr)
    assert found == count, (
        f"{rel} has {found} raw walk(s) `{expr}`, but the exemption allows exactly {count}. "
        f"Fix the count (or delete the entry if none remain). Its stated reason was: {why}"
    )
    assert len(why) > 80, "an exemption without a real reason is an exclusion list"


# --------------------------------------------------------------------------------------------
# The detector itself, both directions, because a detector that cannot fail proves nothing
# --------------------------------------------------------------------------------------------
_RAW = '''
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def corpus():
    return sorted(ROOT.rglob("*.md"))
'''

_WRAPPED = '''
from pathlib import Path
from agentflow.tracked_paths import only_tracked
ROOT = Path(__file__).resolve().parents[2]
def corpus():
    return only_tracked(ROOT, ROOT.rglob("*.md"))
'''

_ALIASED = '''
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def corpus(name):
    home = ROOT / name
    return sorted(home.glob("references/*.md"))
'''

_IMPORTED_ROOT = '''
from pathlib import Path
from agentflow import config
def corpus():
    pkg = Path(config.__file__).parent
    return sorted(pkg.glob("*.py"))
'''

_FIXTURE = '''
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def test_writes_nowhere_else(tmp_path):
    other = tmp_path / "elsewhere"
    return sorted(other.rglob("*")) + sorted(tmp_path.rglob("*"))
'''


def test_the_detector_catches_a_plain_raw_walk() -> None:
    assert raw_walks(_RAW), "the gate cannot fail, so its green above means nothing"


def test_the_detector_catches_a_walk_rooted_on_a_LOCAL_alias() -> None:
    """`home = SKILLS_ROOT / loop` then `home.glob(...)` - a real site, and the shape that a
    module-level-only scan misses."""
    assert raw_walks(_ALIASED)


def test_the_detector_catches_a_root_reached_through_an_IMPORTED_module() -> None:
    """`Path(config.__file__).parent` is a repo root the import system found. gh#175 names this
    site explicitly and the first draft of this detector could not see it."""
    assert raw_walks(_IMPORTED_ROOT)


def test_the_detector_passes_a_wrapped_walk() -> None:
    assert raw_walks(_WRAPPED) == []


def test_a_tmp_path_FIXTURE_walk_is_not_flagged() -> None:
    """Precision, and it is what keeps the gate usable. Fixture walks are the overwhelming
    majority in this suite; a detector that flagged them would be answered with exemptions until
    the list, not the tree, defined the rule."""
    assert raw_walks(_FIXTURE) == []


# --------------------------------------------------------------------------------------------
# The helper's actual property, built rather than mocked: a real repo, a real untracked file
# --------------------------------------------------------------------------------------------
def _git(root: Path, *args: str):
    import subprocess

    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          check=True, text=True)


def test_an_untracked_file_is_excluded_and_git_add_brings_it_back(tmp_path) -> None:
    """THE PROPERTY, stated once here so eleven call sites do not each have to restate it.

    THE THREE STEPS MATTER IN ORDER, because two of them are how this test could pass while
    proving nothing:

    1. the stray must be REACHABLE BY A TREE WALK - asserted against `rglob`, the exact call the
       fix removes. Without it the fixture could be inert and everything below vacuous;
    2. it must be ABSENT from both helpers while untracked;
    3. `git add` it and it must APPEAR. Otherwise "absent" might mean the helper stopped seeing
       that directory at all - a quiet loss of reach wearing the fix's clothes.
    """
    from agentflow.tracked_paths import only_tracked, tracked_files

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "kept.md").write_text("tracked", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "docs/kept.md")

    stray = repo / "docs" / "STRAY.md"
    stray.write_text("untracked", encoding="utf-8")

    # (1) a tree walk - the removed implementation - reads it.
    assert stray in set((repo / "docs").rglob("*.md"))

    # (2) neither helper does.
    walked = list((repo / "docs").rglob("*.md"))
    assert only_tracked(repo, walked) == [repo / "docs" / "kept.md"]
    assert tracked_files(repo, lambda rel: rel.endswith(".md")) == [repo / "docs" / "kept.md"]

    # (3) and the directory is still genuinely in reach.
    _git(repo, "add", "docs/STRAY.md")
    assert stray in only_tracked(repo, walked)
    assert stray in tracked_files(repo, lambda rel: rel.endswith(".md"))


def test_only_tracked_drops_a_path_from_outside_the_root_rather_than_raising(tmp_path) -> None:
    """A caller merging two roots would otherwise have to special-case one of them, and
    `relative_to` raises rather than answering False."""
    from agentflow.tracked_paths import only_tracked

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.md").write_text("x", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "a.md")

    outsider = tmp_path / "elsewhere.md"
    outsider.write_text("x", encoding="utf-8")
    assert only_tracked(repo, [repo / "a.md", outsider]) == [repo / "a.md"]


def test_the_predicate_is_a_python_callable_not_a_git_pathspec(tmp_path) -> None:
    """The trap this API exists to remove. `git ls-files 'a/*/c.md'` is fnmatch WITHOUT
    FNM_PATHNAME, so `*` crosses `/` and the pathspec matches two levels down as well - measured
    on this repo as 33 paths where the Python glob returns 21.
    """
    from agentflow.tracked_paths import tracked_files

    repo = tmp_path / "repo"
    (repo / "a" / "b" / "deep").mkdir(parents=True)
    (repo / "a" / "b" / "c.md").write_text("one segment", encoding="utf-8")
    (repo / "a" / "b" / "deep" / "c.md").write_text("two segments", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")

    one_segment = tracked_files(repo, lambda rel: rel.count("/") == 2 and rel.endswith("/c.md"))
    assert one_segment == [repo / "a" / "b" / "c.md"], (
        "the predicate must mean what the Python glob meant: exactly one segment under `a/`"
    )
    assert _git(repo, "ls-files", "a/*/c.md").stdout.split() == ["a/b/c.md", "a/b/deep/c.md"], (
        "git's `*` crossed `/` and matched two segments down as well as one - that is WHY the "
        "predicate is Python. If git ever stops doing this, the warning in tracked_files is stale"
    )


_ANNOTATED = '''
from pathlib import Path
ROOT: Path = Path(__file__).resolve().parents[2]
def corpus():
    return sorted(ROOT.rglob("*.md"))
'''

_ITERDIR = '''
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def corpus():
    return [p for p in ROOT.iterdir() if p.suffix == ".md"]
'''

_OS_WALK = '''
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def corpus():
    return [f for _d, _s, fs in os.walk(ROOT) for f in fs]
'''


def test_the_detector_catches_an_ANNOTATED_root() -> None:
    """`ROOT: Path = ...` is an AnnAssign, not an Assign. Adding a type annotation to an existing
    root is invisible in review and would otherwise remove the whole module from this gate's
    reach without touching a single walk."""
    assert raw_walks(_ANNOTATED)


def test_the_detector_catches_iterdir_and_os_walk() -> None:
    """A detector that knew only `glob` would teach the next author which spelling evades it."""
    assert raw_walks(_ITERDIR)
    assert raw_walks(_OS_WALK)


def test_the_corpus_reaches_a_test_module_one_directory_down(tmp_path) -> None:
    """`skills/_shared/pyproject.toml` sets `testpaths = ["tests"]` and pytest RECURSES, so a gate
    module in a subdirectory is a live gate. The first version of this file stopped at one
    directory, which would have left the next such module silently unexamined -- the exact shape
    the docstring says a hand list would leave.
    """
    root = tmp_path / "repo"
    nested = root / "skills" / "_shared" / "tests" / "gates"
    nested.mkdir(parents=True)
    (root / "skills" / "_shared" / "tests" / "test_flat.py").write_text("x = 1", encoding="utf-8")
    (nested / "test_nested.py").write_text("x = 1", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")

    assert _test_modules(root) == [
        "skills/_shared/tests/gates/test_nested.py",
        "skills/_shared/tests/test_flat.py",
    ]
