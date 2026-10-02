"""A documented `graph` CLI invocation must be one an operator can actually run.

WHY THIS FILE EXISTS (gh#152). The documented autodoc chain is
`emit-graph.js -> graph build -> graph communities -> graph records -> graph render ->
graph check`. Both `build` recipes omitted `--repo-ref` and `--project`; the renderer
refuses a corpus missing either. So an operator followed the prose, and the chain died
several steps LATER, at `render`, with a remedy that pointed back at a command they had
already finished. Prose and the code it instructs disagreed, and nothing was watching.

That is the third time in this repo (gh#59, gh#67, gh#152), so the fix is not "add the two
flags". It is this gate. Adding the flags without it just resets the clock until the next
rewrite of the chain.

WHY EVERY INPUT IS DERIVED AND NOTHING IS LISTED. gh#152's own triage is the cautionary
tale: it ran `grep -rn "graph build"`, got ten descriptive mentions and zero invocations,
and concluded the invocations had been removed. They had not. They are spelled
`python -m autodoc_graph.cli build`. A pattern that could not match the spelling reported
absence, and absence was read as removal. So:

* the SCRIPT NAMES come from `pyproject.toml`'s `[project.scripts]`, keeping only entry
  points that target `autodoc_graph.cli` -- rename the console script and this follows it;
* the SUBCOMMANDS come from `cli.py`'s own `sub.add_parser(...)` calls;
* the REQUIRED OPTIONS of each subcommand come from that subcommand's `add_argument`
  calls (including the ones the `common()` helper injects, read from `common`'s body);
* the FLAGS A LATER STEP CAN REFUSE ON come from `render_pages.py`'s corpus guards.

Both source files are parsed with `ast`, never imported. That is deliberate twice over: the
gate needs no graph dependencies (this suite installs none), and an oracle that cannot be
perturbed by an import-time side effect is a better oracle.

WHAT MAKES A LINE AN INVOCATION RATHER THAN A MENTION is also derived, not a fence rule: a
candidate is an INVOCATION when it supplies every option argparse marks `required=True`
for that subcommand, a MENTION when it supplies none of them, and a PARTIAL -- reported as
a finding -- when it supplies some. `graph build --embed` in a troubleshooting table is
correctly a mention; a recipe that dropped `--db` is correctly a finding rather than being
demoted to a harmless mention.

THERE IS NO `pytest.skip` HERE. A skip is not a kill: a gate that scanned nothing reports
the same green as a gate that scanned everything. Every derivation that comes up empty, and
every floor that goes unmet, is an assertion failure with the reason in it.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GRAPH_PKG = REPO_ROOT / "skills" / "autodoc-init" / "references" / "graph"
CLI_PY = GRAPH_PKG / "autodoc_graph" / "cli.py"
RENDER_PY = GRAPH_PKG / "autodoc_graph" / "render_pages.py"
PYPROJECT = GRAPH_PKG / "pyproject.toml"
CLI_MODULE = "autodoc_graph.cli"

_GIT = "git"

# The corpus: EVERY tracked markdown file in the repo, minus the vendored third-party
# closure (re-vendored rather than patched, so its prose is not ours to gate).
#
# It was `skills/` until this gate's own review. That root was the shape of where the
# recipes happen to live, not the shape of the risk: a brand-new `graph build` written into
# `commands/`, `docs/`, or `README.md` -- the operator-facing prose most likely to grow one
# -- passed green, while the identical line one directory over was caught. Scoping a sweep
# to where the problem last appeared is the gh#59 lesson wearing a different path. Widening
# added two candidates, both flagless descriptive mentions, and no failures.
#
# Nothing here is excluded for being test data: a fixture that must contain a deliberately
# broken invocation would need an exclusion added WITH ITS REASON, the same way _vendored
# carries one, rather than a root that quietly never looked.
_CORPUS_EXCLUDE = ("skills/_vendored/",)

# ---------------------------------------------------------------- non-vacuity floors
#
# Each is a FLOOR the derivation must reach, never the source of truth. The sets may grow;
# they may not silently shrink. A broken ast matcher that returned an empty required-flag
# set would otherwise green-light every recipe in the tree, which is the one failure mode
# of this design that looks exactly like success.
RATCHET_BUILD_FLAGS = frozenset({"--repo-ref", "--project"})
MIN_INVOCATIONS = 11
MIN_BUILD_INVOCATIONS = 2
# Per file, per subcommand: a corpus-wide count is satisfiable by one file while the other
# silently drops out of scope. That is the gh#59 lesson, so the floor is per file.
PER_FILE_FLOOR = {
    "skills/autodoc-init/SKILL.md": ("build", "communities", "records", "render", "check"),
    "skills/autodoc-update/SKILL.md": ("build", "communities", "records", "render", "check"),
}
# Descriptive mentions that must be FOUND and classified as mentions. If the extractor's
# reach shrinks so it stops seeing them, that is red, not green -- the prototype for this
# gate silently missed two of these because it tokenised before stripping backticks, and
# a missed candidate is indistinguishable from a correctly dismissed one.
PINNED_MENTIONS = {
    ("skills/autodoc-init/references/taxonomy.md", "build"),
    ("skills/autodoc-update/SKILL.md", "build"),
    ("skills/graph-query/SKILL.md", "build"),
}


# ---------------------------------------------------------------- argparse, read as text
@dataclass
class Subcommand:
    name: str
    options: set[str] = field(default_factory=set)
    required: set[str] = field(default_factory=set)


def _option_strings(call: ast.Call) -> tuple[set[str], bool]:
    opts = {a.value for a in call.args if isinstance(a, ast.Constant)
            and isinstance(a.value, str) and a.value.startswith("-")}
    req = any(kw.arg == "required" and isinstance(kw.value, ast.Constant) and kw.value.value
              for kw in call.keywords)
    return opts, req


def parse_cli(source: str) -> dict[str, Subcommand]:
    """Subcommand -> its option strings and its required ones, from `cli.py`'s `_parser`."""
    tree = ast.parse(source)
    parser_fn = next((n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "_parser"), None)
    assert parser_fn is not None, (
        f"{CLI_PY}: no `_parser` function found. This gate reads the CLI's real option "
        "surface out of that function; if it was renamed, teach this gate the new name "
        "rather than letting the derivation quietly return nothing."
    )

    # Options the `common(sp)` helper injects, read from its own body rather than restated.
    helpers: dict[str, tuple[set[str], set[str]]] = {}
    for node in parser_fn.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        opts, reqs = set(), set()
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "add_argument"):
                o, r = _option_strings(call)
                opts |= o
                if r:
                    reqs |= o
        helpers[node.name] = (opts, reqs)

    subs: dict[str, Subcommand] = {}
    by_var: dict[str, Subcommand] = {}
    for node in ast.walk(parser_fn):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "add_parser"
                and node.value.args and isinstance(node.value.args[0], ast.Constant)):
            sc = Subcommand(node.value.args[0].value)
            subs[sc.name] = sc
            by_var[node.targets[0].id] = sc
        elif isinstance(node, ast.Call):
            fn = node.func
            if (isinstance(fn, ast.Attribute) and fn.attr == "add_argument"
                    and isinstance(fn.value, ast.Name) and fn.value.id in by_var):
                sc = by_var[fn.value.id]
                opts, req = _option_strings(node)
                sc.options |= opts
                if req:
                    sc.required |= opts
            elif (isinstance(fn, ast.Name) and fn.id in helpers and node.args
                  and isinstance(node.args[0], ast.Name) and node.args[0].id in by_var):
                sc = by_var[node.args[0].id]
                opts, reqs = helpers[fn.id]
                sc.options |= opts
                sc.required |= reqs

    assert subs, (
        f"{CLI_PY}: parsed `_parser` but derived NO subcommands. Every assertion below "
        "would then pass by examining nothing."
    )
    return subs


# ---------------------------------------------------------------- the refusal oracle
@dataclass
class Refusal:
    lineno: int
    field: str | None          # the corpus column the guard reads, when classifiable
    message_flags: list[str]   # flags the operator-facing message names


_FLAG_IN_TEXT = re.compile(r"--[a-z0-9][a-z0-9-]*")


def _raise_message(node: ast.Raise) -> str:
    parts: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            parts.append(sub.value)
    return " ".join(parts)


def _guarded_field(test: ast.expr) -> str | None:
    """`if not corpus.get("X")` / `if not corpus["X"]` -> "X"."""
    if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
        return None
    inner = test.operand
    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "get" and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "corpus" and inner.args
            and isinstance(inner.args[0], ast.Constant)):
        return inner.args[0].value
    if (isinstance(inner, ast.Subscript) and isinstance(inner.value, ast.Name)
            and inner.value.id == "corpus" and isinstance(inner.slice, ast.Constant)):
        return inner.slice.value
    return None


def parse_refusals(source: str) -> tuple[list[Refusal], list[int]]:
    """Corpus refusals in `render_pages.py`, plus the line numbers of any refusal that
    reads as a corpus refusal to an operator but that this classifier could not classify.

    The second list is the load-bearing half. A guard rewritten into a shape the matcher
    does not know would silently SHRINK the required set -- the one way this gate fails
    toward silence rather than toward noise. So the fallback keys on the message the
    operator actually reads, not on the syntax.
    """
    tree = ast.parse(source)
    refusals: list[Refusal] = []
    classified: set[int] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        fld = _guarded_field(node.test)
        if fld is None:
            continue
        for stmt in node.body:
            if (isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call)
                    and isinstance(stmt.exc.func, ast.Name)
                    and stmt.exc.func.id == "RenderError"):
                msg = _raise_message(stmt)
                refusals.append(Refusal(stmt.lineno, fld, _FLAG_IN_TEXT.findall(msg)))
                classified.add(stmt.lineno)

    unclassified: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "RenderError"):
            continue
        if node.lineno in classified:
            continue
        msg = _raise_message(node)
        if "corpus." in msg or re.search(r"graph\s+\w+\s+--", msg):
            unclassified.append(node.lineno)

    refusals.sort(key=lambda r: r.lineno)
    return refusals, sorted(unclassified)


def conventional_flag(fld: str) -> str:
    return "--" + fld.replace("_", "-")


def parse_build_attribution(source: str) -> set[str]:
    """The flags `cli.py::_require_attribution` refuses on, from `_BUILD_ATTRIBUTION`.

    The build-time guard is a THIRD statement of the same fact, alongside the renderer's
    guards and the prose. Three copies drift in three directions unless something reads
    all three, so this gate reads all three.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_BUILD_ATTRIBUTION"
                and isinstance(node.value, (ast.Tuple, ast.List))):
            out = set()
            for pair in node.value.elts:
                assert isinstance(pair, (ast.Tuple, ast.List)) and pair.elts, (
                    f"{CLI_PY}: _BUILD_ATTRIBUTION entries must be (flag, column) pairs."
                )
                out.add(pair.elts[0].value)
            return out
    raise AssertionError(
        f"{CLI_PY}: no `_BUILD_ATTRIBUTION` assignment found. That table is what makes "
        "`build` refuse at build time instead of leaving the failure to `render`; if it "
        "was renamed, teach this gate the new name rather than dropping the check."
    )


@dataclass
class Oracle:
    required: set[str]                            # build flags a later step refuses on
    excluded: list[tuple[str, str, str]]          # (field, flag, why it is not required)
    orphans: list[tuple[int, str]]                # (lineno, flag build does not accept)
    disagreements: list[tuple[int, str, list[str]]]


def build_oracle(refusals: list[Refusal], build: Subcommand) -> Oracle:
    required: set[str] = set()
    excluded: list[tuple[str, str, str]] = []
    orphans: list[tuple[int, str]] = []
    disagreements: list[tuple[int, str, list[str]]] = []

    for ref in refusals:
        flag = conventional_flag(ref.field)
        if flag in build.options:
            required.add(flag)
            # The guard and the message must name the same flag. A message that names a
            # different one sends the operator to a command that will not fix it.
            if ref.message_flags and flag not in ref.message_flags:
                disagreements.append((ref.lineno, flag, ref.message_flags))
        else:
            excluded.append((ref.field, flag, f"no `{flag}` option on `build`"))
        for named in ref.message_flags:
            if named not in build.options:
                orphans.append((ref.lineno, named))

    return Oracle(required, excluded, orphans, disagreements)


# ---------------------------------------------------------------- prose extraction
@dataclass
class Candidate:
    path: str
    lineno: int
    sub: str
    flags: set[str]
    kind: str          # INVOCATION | PARTIAL | MENTION
    missing_required: set[str]
    text: str


_MARKUP = str.maketrans({"`": " ", "|": " ", "*": " "})


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations before anything else looks at a line.

    Not decorative: `autodoc-init/SKILL.md`'s `records` recipe is five physical lines, and
    a line-oriented reader sees two of its nine flags and blinds itself to the rest.
    """
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not buf:
            start = i
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append((start, buf + line))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def _tokenize(line: str) -> list[str]:
    for posix in (True, False):
        try:
            return shlex.split(line, posix=posix)
        except ValueError:
            continue
    return line.split()


def scan_markdown(text: str, path: str, scripts: set[str],
                  subs: dict[str, Subcommand]) -> list[Candidate]:
    """Every place this prose names a graph subcommand, classified."""
    heads = re.compile(
        r"(?:" + "|".join(sorted(re.escape(s) for s in scripts | {CLI_MODULE})) + r")\s+"
        r"(?:" + "|".join(sorted(re.escape(s) for s in subs)) + r")\b"
    )
    found: list[Candidate] = []
    for lineno, logical in _logical_lines(text):
        stripped = logical.translate(_MARKUP)
        if not heads.search(stripped):
            continue
        tokens = _tokenize(stripped)
        for i, tok in enumerate(tokens[:-1]):
            head = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if head not in scripts and head != CLI_MODULE:
                continue
            sub = tokens[i + 1]
            if sub not in subs:
                continue
            flags = {t.split("=", 1)[0] for t in tokens[i + 2:] if t.startswith("--")}
            missing = subs[sub].required - flags
            if not missing:
                kind = "INVOCATION"
            elif missing == subs[sub].required:
                kind = "MENTION"
            else:
                kind = "PARTIAL"
            found.append(Candidate(path, lineno, sub, flags, kind, missing, logical.strip()))
    return found


# ---------------------------------------------------------------- corpus
def _tracked_markdown() -> list[str]:
    try:
        proc = subprocess.run([_GIT, "-C", str(REPO_ROOT), "ls-files", "-z"],
                              capture_output=True, check=False)
    except OSError as exc:  # pragma: no cover - environment failure, not a code path
        raise AssertionError(
            f"this gate derives its corpus from the git index and could not run {_GIT!r}: "
            f"{exc}. It deliberately does NOT fall back to walking the working tree -- a "
            "tree walk is what lets the same commit produce different verdicts here and in CI."
        ) from exc
    if proc.returncode != 0:
        raise AssertionError(
            f"`{_GIT} ls-files` failed in {REPO_ROOT} (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()!r}."
        )
    paths = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
    if not paths:
        raise AssertionError(
            f"`{_GIT} ls-files` reported an EMPTY index under {REPO_ROOT}. An empty corpus "
            "makes this gate pass by scanning nothing, which is indistinguishable from a "
            "gate that works -- so it fails here instead."
        )
    md = [p for p in paths
          if p.endswith(".md") and not p.startswith(_CORPUS_EXCLUDE)]
    assert md, (
        "no tracked markdown in the git index outside "
        f"{sorted(_CORPUS_EXCLUDE)}. This gate is guarding nothing."
    )
    return sorted(md)


def graph_scripts(pyproject_text: str) -> set[str]:
    """Console scripts whose entry point is the graph CLI, from `[project.scripts]`.

    Derived so a rename carries the gate with it -- and so `graph-query`, a different
    module's entry point, is excluded by the same rule rather than by a hand exception.
    """
    block = re.search(r"^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)",
                      pyproject_text, re.M | re.S)
    assert block, f"{PYPROJECT}: no [project.scripts] table; cannot derive script names."
    names = {m.group(1) for m in
             re.finditer(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"' + re.escape(CLI_MODULE) + r':',
                         block.group(1), re.M)}
    assert names, (
        f"{PYPROJECT}: [project.scripts] declares no entry point targeting {CLI_MODULE}. "
        "The console-script spelling is how half the prose in this repo names the CLI; "
        "deriving nothing here would make those lines invisible."
    )
    return names


# ---------------------------------------------------------------- the state under test
@dataclass
class Survey:
    subs: dict[str, Subcommand]
    scripts: set[str]
    oracle: Oracle
    refusals: list[Refusal]
    unclassified: list[int]
    candidates: list[Candidate]


def survey() -> Survey:
    subs = parse_cli(CLI_PY.read_text(encoding="utf-8"))
    refusals, unclassified = parse_refusals(RENDER_PY.read_text(encoding="utf-8"))
    oracle = build_oracle(refusals, subs["build"])
    scripts = graph_scripts(PYPROJECT.read_text(encoding="utf-8"))
    candidates: list[Candidate] = []
    for rel in _tracked_markdown():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        candidates += scan_markdown(text, rel, scripts, subs)
    return Survey(subs, scripts, oracle, refusals, unclassified, candidates)


_SURVEY = None


def _s() -> Survey:
    global _SURVEY
    if _SURVEY is None:
        _SURVEY = survey()
    return _SURVEY


# ---------------------------------------------------------------- floors
def test_subcommand_derivation_is_not_empty():
    subs = _s().subs
    assert "build" in subs, "`cli.py` declares no `build` subparser; the oracle has no home."
    assert subs["build"].required >= {"--db", "--corpus", "--jsonl"}, (
        "`build`'s required options no longer include the three the recipes carry. That is "
        f"either a real CLI change or a broken derivation; derived: {sorted(subs['build'].required)}"
    )


def test_console_script_names_are_derived_from_pyproject():
    assert "graph" in _s().scripts, (
        "the `graph` console script is how most prose in this repo spells the CLI; "
        f"derived: {sorted(_s().scripts)}"
    )


def test_refusal_oracle_meets_its_ratchet_floor():
    got = _s().oracle.required
    assert RATCHET_BUILD_FLAGS <= got, (
        f"the renderer's corpus refusals derived to {sorted(got)}, which no longer covers "
        f"{sorted(RATCHET_BUILD_FLAGS)}. This set may GROW; it may not shrink. A shrinking "
        "required set silently green-lights every recipe in the tree, so a genuine removal "
        "of one of these guards must edit this floor deliberately."
    )
    assert len(got) >= len(RATCHET_BUILD_FLAGS)


def test_every_render_refusal_is_classified_or_excluded_with_a_reason():
    s = _s()
    assert not s.unclassified, (
        f"{RENDER_PY.name} raises RenderError at line(s) {s.unclassified} with a message "
        "that reads as a corpus refusal, but this gate could not classify the guard that "
        "produced it. An unclassified guard drops out of the required-flag set silently, "
        "which is the one way this gate fails toward a false green. Teach `_guarded_field` "
        "the new shape."
    )
    fields = {r.field for r in s.refusals}
    assert "indexed_at" in fields, (
        "the `corpus.indexed_at` refusal was not seen at all. It is the control: it proves "
        "the classifier examined every corpus guard rather than tripping over one. Its "
        "absence from the EXCLUDED list would otherwise look identical to a clean scan."
    )
    assert any(f == "indexed_at" for f, _flag, _why in s.oracle.excluded), (
        "`indexed_at` was classified but not excluded. `build` has no `--indexed-at`, so it "
        "is satisfied by ingest, not by a flag; requiring it of the recipes would be wrong."
    )


def test_build_refuses_at_build_time_on_exactly_what_render_refuses_on():
    """The whole point of gh#152: the error lands where the remedy is.

    `build` must refuse on precisely the set `render` refuses on -- no wider (it would
    reject a build that could render) and no narrower (the gap is the original defect,
    a corpus that builds clean and dies at render).
    """
    guarded = parse_build_attribution(CLI_PY.read_text(encoding="utf-8"))
    required = _s().oracle.required
    assert guarded == required, (
        f"`cli.py::_BUILD_ATTRIBUTION` refuses on {sorted(guarded)} while "
        f"`{RENDER_PY.name}`'s corpus guards refuse on {sorted(required)}. "
        f"Only in build: {sorted(guarded - required)}; only in render: "
        f"{sorted(required - guarded)}. A field render refuses on that build does not is "
        "gh#152 verbatim: the corpus builds clean and dies several steps later."
    )


def test_no_refusal_names_a_flag_build_cannot_accept():
    orphans = _s().oracle.orphans
    assert not orphans, (
        "the renderer tells an operator to pass a flag `graph build` does not accept: "
        + "; ".join(f"{RENDER_PY.name}:{ln} names {flag}" for ln, flag in orphans)
    )


def test_guard_and_message_name_the_same_flag():
    bad = _s().oracle.disagreements
    assert not bad, (
        "a corpus guard reads one field while its message tells the operator to pass a "
        "different flag: "
        + "; ".join(f"{RENDER_PY.name}:{ln} guards {flag} but says {named}"
                    for ln, flag, named in bad)
    )


def test_extraction_meets_its_floors():
    s = _s()
    invocations = [c for c in s.candidates if c.kind == "INVOCATION"]
    assert len(invocations) >= MIN_INVOCATIONS, (
        f"found only {len(invocations)} documented graph invocations; the floor is "
        f"{MIN_INVOCATIONS}. An extractor whose reach shrank would pass every assertion "
        "below by examining almost nothing."
    )
    builds = [c for c in invocations if c.sub == "build"]
    assert len(builds) >= MIN_BUILD_INVOCATIONS, (
        f"found only {len(builds)} documented `build` invocations; the floor is "
        f"{MIN_BUILD_INVOCATIONS} (init and update each run the chain)."
    )
    for path, wanted in PER_FILE_FLOOR.items():
        got = {c.sub for c in invocations if c.path == path}
        assert set(wanted) <= got, (
            f"{path} yielded invocations for {sorted(got)}, missing {sorted(set(wanted) - got)}. "
            "The floor is per file on purpose: a corpus-wide count stays satisfied when one "
            "file drops out of scope entirely."
        )


def test_descriptive_mentions_are_found_and_dismissed():
    seen = {(c.path, c.sub) for c in _s().candidates if c.kind == "MENTION"}
    missing = PINNED_MENTIONS - seen
    assert not missing, (
        f"these descriptive mentions were not found and classified as mentions: "
        f"{sorted(missing)}. A candidate the extractor never saw looks exactly like one it "
        "correctly dismissed, so they are pinned rather than trusted."
    )


# ---------------------------------------------------------------- the gate itself
def test_documented_invocations_supply_every_required_option():
    bad = [c for c in _s().candidates if c.kind == "PARTIAL"]
    assert not bad, (
        "these lines read as invocations of the graph CLI but omit an option argparse "
        "marks required, so pasting one is an immediate usage error:\n"
        + "\n".join(f"  {c.path}:{c.lineno} `{c.sub}` missing {sorted(c.missing_required)}"
                    for c in bad)
    )


def test_documented_build_invocations_supply_every_flag_render_can_refuse_on():
    s = _s()
    required = s.oracle.required
    bad = [(c, sorted(required - c.flags))
           for c in s.candidates if c.kind == "INVOCATION" and c.sub == "build"]
    bad = [(c, miss) for c, miss in bad if miss]
    assert not bad, (
        "a documented `build` omits a flag only `build` can set and `render` later refuses "
        "on, so the chain dies several steps downstream with a remedy pointing back at a "
        "command the operator already finished (gh#152):\n"
        + "\n".join(f"  {c.path}:{c.lineno} missing {miss}" for c, miss in bad)
        + f"\n  (required set derived from {RENDER_PY.name}'s corpus guards: "
        + f"{sorted(required)})"
    )


# ---------------------------------------------------------------- the gate can fail
#
# Every branch above, exercised against synthetic sources so the assertions are known to be
# reachable. Without these, PARTIAL and the orphan/unclassified branches are untested code
# that happens to have no live hits.
_FAKE_CLI = '''
def _parser():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()

    def common(sp):
        sp.add_argument("--db", required=True)
        sp.add_argument("--corpus", required=True)

    b = sub.add_parser("build")
    common(b)
    b.add_argument("--jsonl", required=True)
    b.add_argument("--repo-ref", default=None)
    b.add_argument("--project", default=None)
    return p
'''

_FAKE_RENDER = '''
class _Renderer:
    def __init__(self, adapter, out_dir):
        corpus = adapter.get_corpus()
        if not corpus.get("repo_ref"):
            raise RenderError("corpus.repo_ref is not set - run `graph build --repo-ref` first")
        if not corpus.get("project"):
            raise RenderError("corpus.project is not set - run `graph build --project X` first")
        if not corpus.get("indexed_at"):
            raise RenderError("corpus.indexed_at is not set - ingest before rendering")
'''


def _fake_subs():
    return parse_cli(_FAKE_CLI)


def test_mutation_flagless_build_is_caught_in_the_module_spelling():
    subs = _fake_subs()
    refusals, _ = parse_refusals(_FAKE_RENDER)
    required = build_oracle(refusals, subs["build"]).required
    text = "```bash\npython -m autodoc_graph.cli build --db D --corpus C --jsonl J\n```\n"
    (cand,) = scan_markdown(text, "f.md", {"graph"}, subs)
    assert cand.kind == "INVOCATION"
    assert required - cand.flags == {"--repo-ref", "--project"}


def test_mutation_flagless_build_is_caught_in_the_console_script_spelling():
    subs = _fake_subs()
    refusals, _ = parse_refusals(_FAKE_RENDER)
    required = build_oracle(refusals, subs["build"]).required
    text = "    graph build --db D --corpus C --jsonl J --repo-ref R\n"
    (cand,) = scan_markdown(text, "f.md", {"graph"}, subs)
    assert cand.kind == "INVOCATION"
    assert required - cand.flags == {"--project"}


def test_mutation_a_build_missing_a_required_option_is_a_partial_not_a_mention():
    subs = _fake_subs()
    text = "graph build --corpus C --jsonl J --repo-ref R --project P\n"
    (cand,) = scan_markdown(text, "f.md", {"graph"}, subs)
    assert cand.kind == "PARTIAL", (
        "a recipe that dropped a required option must be a finding. Demoting it to a "
        "harmless mention is how it would escape."
    )
    assert cand.missing_required == {"--db"}


def test_mutation_a_third_refusal_widens_the_required_set_by_itself():
    subs = _fake_subs()
    subs["build"].options.add("--branch")
    src = _FAKE_RENDER.replace(
        '        if not corpus.get("indexed_at"):',
        '        if not corpus.get("branch"):\n'
        '            raise RenderError("corpus.branch is not set - run `graph build --branch` first")\n'
        '        if not corpus.get("indexed_at"):',
    )
    refusals, _ = parse_refusals(src)
    assert build_oracle(refusals, subs["build"]).required == {
        "--repo-ref", "--project", "--branch"}


def test_mutation_a_refusal_naming_an_absent_flag_is_an_orphan():
    subs = _fake_subs()
    src = _FAKE_RENDER.replace("--repo-ref` first", "--owner` first")
    refusals, _ = parse_refusals(src)
    oracle = build_oracle(refusals, subs["build"])
    assert ("--owner" in [f for _ln, f in oracle.orphans])
    assert oracle.disagreements, (
        "the guard reads repo_ref while the message sends the operator to --owner; that "
        "disagreement must be a finding in its own right."
    )


def test_mutation_an_unclassifiable_guard_is_a_finding_not_a_silent_shrink():
    src = _FAKE_RENDER.replace(
        '        if not corpus.get("repo_ref"):',
        '        if _missing(corpus, "repo_ref"):',
    )
    refusals, unclassified = parse_refusals(src)
    assert {r.field for r in refusals} == {"project", "indexed_at"}, (
        "the required set really does shrink when a guard is rewritten -- which is why the "
        "unclassified list below has to be non-empty."
    )
    assert unclassified, (
        "a RenderError whose message reads as a corpus refusal but whose guard could not be "
        "classified must be reported. Silence here is the false green this gate exists to "
        "prevent."
    )


def test_mutation_continuation_lines_are_joined_before_flags_are_read():
    subs = _fake_subs()
    text = ("graph build --db D --corpus C \\\n"
            "  --jsonl J \\\n"
            "  --repo-ref R --project P\n")
    (cand,) = scan_markdown(text, "f.md", {"graph"}, subs)
    assert cand.flags == {"--db", "--corpus", "--jsonl", "--repo-ref", "--project"}
    assert cand.lineno == 1


def test_mutation_prose_and_table_mentions_stay_mentions():
    subs = _fake_subs()
    prose = "the chain (`emit-graph.js` -> `graph build` -> `graph communities`)\n"
    assert [c.kind for c in scan_markdown(prose, "f.md", {"graph"}, subs)] == ["MENTION"]
    table = "| no embedded entities | embedding skipped -- `graph build --embed` |\n"
    (cell,) = scan_markdown(table, "f.md", {"graph"}, subs)
    assert cell.kind == "MENTION" and cell.flags == {"--embed"}, (
        "a table cell naming one real flag is still a citation, not a recipe. A "
        "flag-presence heuristic gets this one wrong; the required-option rule does not."
    )
