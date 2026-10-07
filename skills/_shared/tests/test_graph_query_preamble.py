"""graph-query resolves its autodoc root PER PROJECT, never off the machine-level field (gh#395).

`environment.autodoc_root` is machine-level: on a box running two projects it names ONE corpus.
A query is read-only, but a query answered from the wrong corpus is a confident, well-cited
answer about the wrong codebase -- worse than an error, because nothing about it looks empty.

Two layers, because each catches what the other cannot:

* a STATIC gate over what the fenced bash actually runs (comments stripped), so the bare read
  cannot come back unnoticed even where the execution fixture would happen to agree with it;
* an EXECUTION gate that lifts the preamble's python heredoc out of SKILL.md and runs it against
  a throwaway config home with two projects, each owning its own corpus, plus the migration
  case -- so "calls the resolver" is proven to mean "prints THIS project's root", and a swallowed
  AutodocRootError is proven to still refuse.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentflow import config

SKILL = Path(__file__).resolve().parents[2] / "graph-query" / "SKILL.md"
# The package that was IMPORTED -- the subprocess must run the same tree the suite runs.
PKG_PARENT = Path(config.__file__).resolve().parent.parent


def _fenced_bash_blocks(text: str) -> list[str]:
    out, buf, inside = [], [], False
    for line in text.split("\n"):
        s = line.strip()
        fence = s.startswith("```")
        if not inside and fence and s[3:].strip().lower() in ("bash", "sh", "shell"):
            inside, buf = True, []
            continue
        if inside and fence:
            out.append("\n".join(buf))
            inside = False
            continue
        if inside:
            buf.append(line)
    return out


def _strip_comments(text: str) -> str:
    """Drop `#` comments (shell and python alike) so a sentence ABOUT a read is not a read."""
    out = []
    for line in text.split("\n"):
        q, cut = None, None
        for i, ch in enumerate(line):
            if q:
                if ch == q:
                    q = None
            elif ch in "'\"":
                q = ch
            elif ch == "#" and (i == 0 or line[i - 1].isspace()):
                cut = i
                break
        out.append(line if cut is None else line[:cut].rstrip())
    return "\n".join(out)


def _preamble_block() -> str:
    """The fenced block that assigns $AUTODOC -- the config preamble."""
    blocks = [b for b in _fenced_bash_blocks(SKILL.read_text(encoding="utf-8"))
              if re.search(r"^\s*GRAPH_DB=", b, re.M)]
    assert len(blocks) == 1, f"expected exactly one preamble block defining GRAPH_DB, got {len(blocks)}"
    return blocks[0]


def _preamble_python() -> str:
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", _preamble_block(), re.S)
    assert m, "the preamble no longer carries a quoted `<<'PY'` heredoc -- the execution gate has nothing to run"
    return m.group(1)


# --------------------------------------------------------------------------- #
# Static: what the block RUNS
# --------------------------------------------------------------------------- #
def test_the_preamble_CALLS_the_resolver_and_emits_its_result() -> None:
    shell = _strip_comments(_preamble_block())
    # The CALL, not the name: the import line carries the name too.
    assert re.search(r"^\s*root\s*=\s*resolve_autodoc_root\s*\(", shell, re.M), (
        "graph-query never assigns its root from resolve_autodoc_root(...), so a project's own "
        "autodoc_root override is invisible and the query runs against the machine-level corpus"
    )
    assert re.search(r"emit\(\s*\"AUTODOC\"\s*,\s*root\s*\)", shell), (
        "AUTODOC is emitted from something other than the resolver's result"
    )


def test_the_preamble_never_reads_autodoc_root_bare() -> None:
    shell = _strip_comments(_preamble_block())
    bare = re.findall(r"\b[A-Za-z_]\w*\s*\.\s*autodoc_root\b", shell)
    assert not bare, f"bare attribute read of autodoc_root in graph-query's preamble: {bare} (gh#395)"


def test_the_preamble_catches_AutodocRootError_and_guards_every_emitted_value() -> None:
    shell = _strip_comments(_preamble_block())
    handler = re.search(r"except\s+AutodocRootError\b[^\n]*:\n(\s+[^\n]*\n)", shell)
    assert handler and "SystemExit" in handler.group(1), (
        "AutodocRootError is not turned into a SystemExit -- either it escapes as a traceback "
        "or it is swallowed and the query runs on a guessed root"
    )
    for var in ("AUTODOC", "REPO_STEM"):
        assert re.search(r"^\s*\[ -n \"\$" + var + r"\" \] \|\| exit 1", shell, re.M), (
            f"${var} is never guarded after the eval, and eval exits 0 even when python refused"
        )


# --------------------------------------------------------------------------- #
# Execution: the preamble's python against a real config home
# --------------------------------------------------------------------------- #
def _yaml_path(p: Path) -> str:
    return str(p).replace("\\", "/")


def _corpus(p: Path, *, manifest: bool, project: str | None = None,
            source_path: str | None = None) -> Path:
    # The manifest's source_path stem is deliberately NOT the profile's host_ref stem
    # (`<name>-repo`, below): the corpus id comes from what the build recorded, and a fixture
    # where the two agree could not tell the right source from the wrong one.
    (p / "endpoints").mkdir(parents=True, exist_ok=True)
    (p / "endpoints" / "x.md").write_text("page\n", encoding="utf-8")
    if manifest:
        fields = {}
        if project:
            fields["project"] = project
            fields["source_path"] = source_path or f".source/{project}"
        body = json.dumps(fields)
        (p / ".manifest.json").write_text(body, encoding="utf-8")
    return p


def _write_env(home: Path, d: str, autodoc_root: Path | None) -> None:
    extra = f"autodoc_root: {_yaml_path(autodoc_root)}\n" if autodoc_root else ""
    body = textwrap.dedent(f"""\
        schema_version: 1
        person:
          name: Sample User
        data_root: {d}
        git_host:
          kind: local
        notify:
          kind: none
        lock_dir: {d}
        shell: posix
        suite_root: {d}
        worktree_root: {d}
        """) + extra
    (home / "environment.md").write_text(f"# environment\n\n```yaml\n{body}```\n", encoding="utf-8")


def _write_project(home: Path, name: str, d: str, autodoc_root: Path | None) -> None:
    extra = f"autodoc_root: {_yaml_path(autodoc_root)}\n" if autodoc_root else ""
    body = textwrap.dedent(f"""\
        schema_version: 1
        name: {name}
        canonical_repo:
          host_ref: owner/{name}-repo
          local_checkout: {d}
        verification:
          strategy: test-suite
        """) + extra
    (home / "projects").mkdir(parents=True, exist_ok=True)
    (home / "projects" / f"{name}.md").write_text(f"# project {name}\n\n```yaml\n{body}```\n", encoding="utf-8")


def _run(home: Path, project: str, *, session_default: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("AGENTFLOW_PROJECT", "AUTODOC_PROJECT")}
    env["AGENTFLOW_CONFIG"] = str(home)
    env["PYTHONPATH"] = str(PKG_PARENT)
    if session_default:
        env["AGENTFLOW_PROJECT"] = session_default
    return subprocess.run(
        [sys.executable, "-B", "-c", _preamble_python(), project],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _emitted(out: str) -> dict[str, str]:
    import shlex
    got = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        got[k] = shlex.split(v)[0] if v else ""
    return got


@pytest.fixture
def box(tmp_path):
    """A two-project box: alpha and beta each own a corpus, and the machine-level field names
    a THIRD, populated one -- so a bare read prints a real, plausible, wrong path."""
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    home.mkdir()
    machine = _corpus(tmp_path / "machine-corpus", manifest=True, project="machine")
    alpha = _corpus(tmp_path / "alpha-corpus", manifest=True, project="alpha")
    beta = _corpus(tmp_path / "beta-corpus", manifest=True, project="beta")
    _write_env(home, d, machine)
    _write_project(home, "alpha", d, alpha)
    _write_project(home, "beta", d, beta)
    return {"home": home, "d": d, "tmp": tmp_path, "machine": machine, "alpha": alpha, "beta": beta}


@pytest.mark.parametrize("project", ["alpha", "beta"])
def test_each_project_resolves_to_ITS_OWN_corpus(box, project) -> None:
    # The session default names the OTHER project: the explicit project argument must win.
    other = "beta" if project == "alpha" else "alpha"
    r = _run(box["home"], project, session_default=other)
    assert r.returncode == 0, r.stderr
    got = _emitted(r.stdout)
    assert got["AUTODOC"] == _yaml_path(box[project]), (
        f"{project} resolved to {got['AUTODOC']!r}, not its own corpus -- the machine-level "
        f"root is {_yaml_path(box['machine'])!r}, the other project's is {_yaml_path(box[other])!r}"
    )
    assert got["REPO_STEM"] == project, (
        f"REPO_STEM {got['REPO_STEM']!r} is not the manifest's corpus id {project!r} - "
        f"derived from the profile's host_ref ({project}-repo) it orphans the built corpus"
    )


def test_the_migration_hazard_is_a_named_FATAL_not_a_traceback_and_not_an_answer(box) -> None:
    """Project's own root is scaffolded-but-empty while the machine root still holds a corpus:
    AutodocRootError. Swallowing it either answers from the legacy corpus or crashes."""
    empty = box["tmp"] / "gamma-new"
    empty.mkdir()
    _write_project(box["home"], "gamma", box["d"], empty)
    r = _run(box["home"], "gamma")
    assert r.returncode != 0, f"answered instead of refusing: {r.stdout!r}"
    assert "AUTODOC=" not in r.stdout
    assert "Traceback" not in r.stderr, r.stderr
    assert "FATAL" in r.stderr and "'gamma'" in r.stderr, r.stderr
    assert "remediation" in r.stderr, "the refusal dropped the resolver's remedy text: " + r.stderr


def test_an_unconfigured_project_is_a_named_FATAL(tmp_path) -> None:
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    home.mkdir()
    _write_env(home, d, None)
    _write_project(home, "solo", d, None)
    r = _run(home, "solo")
    assert r.returncode != 0 and "AUTODOC=" not in r.stdout
    assert "Traceback" not in r.stderr, r.stderr
    assert "FATAL" in r.stderr and "'solo'" in r.stderr, r.stderr


def test_an_unknown_project_is_a_named_FATAL(box) -> None:
    r = _run(box["home"], "nosuch")
    assert r.returncode != 0 and "AUTODOC=" not in r.stdout
    assert "Traceback" not in r.stderr, r.stderr
    assert "FATAL" in r.stderr and "'nosuch'" in r.stderr, r.stderr


def test_an_unconfigured_projects_remedy_names_the_profile_field_autodoc_init_now_reads(tmp_path) -> None:
    """/autodoc-init resolves its root through the same resolver (gh#393), so the remedy can send
    the operator to the project profile and then to /autodoc-init, and the two agree."""
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    home.mkdir()
    _write_env(home, d, None)
    _write_project(home, "solo", d, None)
    r = _run(home, "solo")
    assert r.returncode != 0
    assert "/autodoc-init" in r.stderr and "project profile" in r.stderr, r.stderr


def test_a_missing_environment_is_a_named_FATAL_not_a_traceback(box) -> None:
    (box["home"] / "environment.md").unlink()
    r = _run(box["home"], "beta")
    assert r.returncode != 0 and "AUTODOC=" not in r.stdout
    assert "Traceback" not in r.stderr, r.stderr
    assert "FATAL" in r.stderr and "'beta'" in r.stderr, r.stderr


# --------------------------------------------------------------------------- #
# Execution: the corpus must say it documents $PROJECT (gh#536)
# --------------------------------------------------------------------------- #
# A root that resolves cleanly can still be another project's corpus -- a mis-set override, a
# legacy root shared by two projects. /autodoc-update refuses that on the manifest's project
# field; graph-query must too, or it answers confidently about the wrong code.
def _assert_refused(r: subprocess.CompletedProcess, project: str) -> None:
    assert r.returncode != 0, f"answered instead of refusing: {r.stdout!r}"
    assert "AUTODOC=" not in r.stdout
    assert "Traceback" not in r.stderr, r.stderr
    assert "FATAL" in r.stderr and repr(project) in r.stderr, r.stderr


def test_a_corpus_whose_manifest_names_ANOTHER_project_is_refused(box) -> None:
    # alpha's profile points at beta's corpus: it resolves, and it is the wrong code.
    _write_project(box["home"], "alpha", box["d"], box["beta"])
    r = _run(box["home"], "alpha")
    _assert_refused(r, "alpha")
    assert "'beta'" in r.stderr, "the refusal does not name what the corpus documents: " + r.stderr
    root = _yaml_path(box["beta"])
    assert root in r.stderr.replace("\\", "/"), "the refusal does not name the root: " + r.stderr


def test_a_manifest_with_no_project_field_is_refused(box) -> None:
    anon = _corpus(box["tmp"] / "anon-corpus", manifest=True)
    _write_project(box["home"], "alpha", box["d"], anon)
    r = _run(box["home"], "alpha")
    _assert_refused(r, "alpha")
    assert "names no project" in r.stderr, r.stderr


@pytest.mark.parametrize("body", ["[]", "{not json"])
def test_a_non_object_or_unparseable_manifest_is_refused(box, body) -> None:
    odd = _corpus(box["tmp"] / "odd-corpus", manifest=False)
    (odd / ".manifest.json").write_text(body, encoding="utf-8")
    _write_project(box["home"], "alpha", box["d"], odd)
    _assert_refused(_run(box["home"], "alpha"), "alpha")


def test_inherited_values_are_cleared_before_the_eval() -> None:
    # The guards test non-emptiness, so an AUTODOC inherited from the caller's shell would let a
    # refused corpus through. The unset must come before the eval that re-assigns them.
    skill = Path(__file__).resolve().parents[2] / "graph-query" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    unset_at = text.find("unset AUTODOC REPO_STEM")
    assert unset_at != -1, "preamble no longer clears AUTODOC/REPO_STEM before resolving"
    assert unset_at < text.find('eval "$(python - "$AUTODOC_PROJECT"'), "the unset must precede the eval"


def test_a_missing_manifest_is_refused(tmp_path) -> None:
    # No legacy root, so the resolver itself accepts a manifest-less root -- the check must not.
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    home.mkdir()
    bare = _corpus(tmp_path / "bare-corpus", manifest=False)
    _write_env(home, d, None)
    _write_project(home, "solo", d, bare)
    r = _run(home, "solo")
    _assert_refused(r, "solo")
    assert ".manifest.json" in r.stderr, r.stderr


def test_a_matching_manifest_passes(box) -> None:
    r = _run(box["home"], "alpha")
    assert r.returncode == 0, r.stderr
    assert _emitted(r.stdout)["AUTODOC"] == _yaml_path(box["alpha"])


# --------------------------------------------------------------------------- #
# The selector is AUTODOC_PROJECT, required, and $PROJECT is never read (gh#759 item 3)
# --------------------------------------------------------------------------- #
# /autodoc-update's run carrier exports PROJECT, so a shell that just ran an update still holds
# that run's project. Read as the selector, it would quietly pick the corpus for the next query.
def test_the_preamble_requires_AUTODOC_PROJECT_and_never_reads_PROJECT() -> None:
    shell = _strip_comments(_preamble_block())
    assert re.search(r"\$\{AUTODOC_PROJECT:\?", shell), (
        "the preamble does not require AUTODOC_PROJECT with ${AUTODOC_PROJECT:?...}"
    )
    stray = re.findall(r"\$\{?PROJECT\b", shell)
    assert not stray, f"the preamble still reads $PROJECT: {stray}"
    assert re.search(r"python - \"\$AUTODOC_PROJECT\"", shell), (
        "the resolver is not handed $AUTODOC_PROJECT as its project"
    )


def test_the_command_tells_the_operator_to_set_AUTODOC_PROJECT() -> None:
    cmd = Path(__file__).resolve().parents[3] / "commands" / "graph-query.md"
    text = cmd.read_text(encoding="utf-8")
    assert "`AUTODOC_PROJECT`" in text, "commands/graph-query.md does not name AUTODOC_PROJECT"
    assert "Set `PROJECT`" not in text, "commands/graph-query.md still says to set PROJECT"


def _bash() -> str:
    """Git's own bash first; a bare PATH lookup on Windows finds the WSL launcher."""
    import shutil

    candidates: list[str] = []
    git = shutil.which("git")
    if git:
        for base in list(Path(git).resolve().parents)[:3]:
            for rel in ("bin/bash.exe", "usr/bin/bash.exe", "bin/bash"):
                c = base / rel
                if c.is_file() and str(c) not in candidates:
                    candidates.append(str(c))
    found = shutil.which("bash")
    if found and "system32" not in found.lower() and found not in candidates:
        candidates.append(found)
    if not candidates:
        pytest.skip("no usable bash found (Git for Windows ships one)")
    return candidates[0]


def _run_shell(home: Path, scratch: Path, **extra: str) -> subprocess.CompletedProcess:
    """The WHOLE preamble block in bash, `python` pinned to the suite's interpreter and tree."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("AUTODOC", "AUTODOC_PROJECT", "PROJECT", "REPO_STEM", "PYTHONPATH")
           and not k.startswith("AGENTFLOW_")}
    env["AGENTFLOW_CONFIG"] = str(home)
    env["PYTHONPATH"] = str(PKG_PARENT)
    env["AF_TEST_PY"] = _yaml_path(Path(sys.executable))
    env.update(extra)
    script = ('python() { "$AF_TEST_PY" -B "$@"; }\n' + _preamble_block()
              + '\necho "AUTODOC=$AUTODOC"\necho PREAMBLE-COMPLETED\n')
    path = scratch / "graph-query-preamble.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run([_bash(), str(path)], env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=120, stdin=subprocess.DEVNULL)


def test_the_preamble_REFUSES_without_AUTODOC_PROJECT_even_with_PROJECT_set(box) -> None:
    (box["alpha"] / ".source").mkdir()
    (box["alpha"] / ".source" / "graph.db").write_text("", encoding="utf-8")
    r = _run_shell(box["home"], box["tmp"], PROJECT="alpha")
    assert r.returncode != 0, f"queried with no AUTODOC_PROJECT: {r.stdout!r}"
    assert "PREAMBLE-COMPLETED" not in r.stdout
    assert "AUTODOC_PROJECT" in r.stderr, r.stderr


def test_the_preamble_resolves_the_project_AUTODOC_PROJECT_names_not_PROJECT(box) -> None:
    for p in ("alpha", "beta"):
        (box[p] / ".source").mkdir()
        (box[p] / ".source" / "graph.db").write_text("", encoding="utf-8")
    r = _run_shell(box["home"], box["tmp"], AUTODOC_PROJECT="beta", PROJECT="alpha")
    assert r.returncode == 0, r.stderr
    assert "PREAMBLE-COMPLETED" in r.stdout
    assert f"AUTODOC={_yaml_path(box['beta'])}" in r.stdout, r.stdout


# --------------------------------------------------------------------------- #
# The corpus id is the build's, read from the manifest, never the current host_ref
# --------------------------------------------------------------------------- #
def _one_project_home(tmp_path: Path, corpus: Path) -> Path:
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    home.mkdir()
    _write_env(home, d, None)
    _write_project(home, "alpha", d, corpus)
    return home


def test_a_renamed_repo_still_queries_the_corpus_the_build_stored(tmp_path) -> None:
    """The profile's host_ref moved to `<owner>/alpha-private`; the corpus was built as `alpha`."""
    corpus = _corpus(tmp_path / "alpha-corpus", manifest=True, project="alpha",
                     source_path=".source/alpha")
    home = _one_project_home(tmp_path, corpus)
    proj_file = home / "projects" / "alpha.md"
    proj_file.write_text(proj_file.read_text(encoding="utf-8").replace(
        "owner/alpha-repo", "owner/alpha-private"), encoding="utf-8")

    r = _run(home, "alpha")

    assert r.returncode == 0, r.stderr
    assert _emitted(r.stdout)["REPO_STEM"] == "alpha"


@pytest.mark.parametrize("source_path, stem", [
    (".source/widget/", "widget"),
    (".source\\widget", "widget"),
    ("widget", "widget"),
])
def test_the_corpus_id_is_the_last_segment_of_the_recorded_source_path(tmp_path, source_path,
                                                                       stem) -> None:
    corpus = _corpus(tmp_path / "alpha-corpus", manifest=True, project="alpha",
                     source_path=source_path)
    r = _run(_one_project_home(tmp_path, corpus), "alpha")
    assert r.returncode == 0, r.stderr
    assert _emitted(r.stdout)["REPO_STEM"] == stem


@pytest.mark.parametrize("value", [None, "", "/", 7])
def test_a_manifest_with_no_usable_source_path_is_refused_not_guessed(tmp_path, value) -> None:
    corpus = _corpus(tmp_path / "alpha-corpus", manifest=False)
    fields = {"project": "alpha"}
    if value is not None:
        fields["source_path"] = value
    (corpus / ".manifest.json").write_text(json.dumps(fields), encoding="utf-8")

    r = _run(_one_project_home(tmp_path, corpus), "alpha")

    assert r.returncode != 0
    assert "source_path" in r.stderr and "REPO_STEM" not in r.stdout, (r.stdout, r.stderr)
