"""The autodoc config preambles, RUN: the real fenced bash out of each SKILL.md, in bash.

A static gate proves a call is written; only running the block proves what the shell is left
holding when python refuses. Every case here lifts the block text verbatim out of the SKILL.md and
executes it against a throwaway config home, with two rewrites and nothing else:

* `/tmp/` becomes the test's own scratch directory, so the run pointer and the carrier land there
  and a live run's pointer on this machine is never removed or replaced;
* `python` is a shell function that runs the interpreter the suite runs, against the tree the
  suite imported, so the block cannot quietly exercise some other install of the package.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentflow import config

SKILLS = Path(__file__).resolve().parents[2]
INIT = SKILLS / "autodoc-init" / "SKILL.md"
UPDATE = SKILLS / "autodoc-update" / "SKILL.md"
# The package that was IMPORTED -- the subprocess must run the same tree the suite runs.
PKG_PARENT = Path(config.__file__).resolve().parent.parent
POINTER_WRITE = re.compile(r"^printf '%s\\n' \"\$AUTODOC_ENV\" > /tmp/autodoc-env\.current", re.M)


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


def _block(skill: Path, pattern: re.Pattern[str]) -> str:
    blocks = [b for b in _fenced_bash_blocks(skill.read_text(encoding="utf-8")) if pattern.search(b)]
    assert len(blocks) == 1, f"{skill.parent.name}: expected one block matching {pattern.pattern!r}, got {len(blocks)}"
    return blocks[0]


def _bash() -> str:
    """Git's own bash first; a bare PATH lookup on Windows finds the WSL launcher."""
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


def _yaml_path(p: Path) -> str:
    return str(p).replace("\\", "/")


# Everything a preamble reads or emits. Stripped from the inherited environment so the host
# session's own project, config home or carrier values can never decide a case.
_SCRUB = ("AUTODOC", "AUTODOC_PROJECT", "AUTODOC_ENV", "PROJECT", "SUITE_ROOT", "REPO", "BRANCH",
          "REPO_STEM", "RENDER_TARGET", "PYTHONPATH", "GIT_HOST_KIND", "LOCAL_CHECKOUT")


def _env(home: Path | None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB and not k.startswith("AGENTFLOW_")}
    if home is not None:
        env["AGENTFLOW_CONFIG"] = str(home)
    env["PYTHONPATH"] = str(PKG_PARENT)
    env["AF_TEST_PY"] = _yaml_path(Path(sys.executable))
    env.update(extra)
    return env


def _run_block(block: str, scratch: Path, env: dict[str, str], tail: str = "") -> subprocess.CompletedProcess[str]:
    tmp = scratch / "tmp"
    tmp.mkdir(exist_ok=True)
    prefix = _yaml_path(tmp) + "/"
    # On Linux the scratch dir itself lives under /tmp/. Shield paths the test already pointed
    # into its own scratch tree (e.g. an inlined $AUTODOC) so only the block's /tmp/ is rewritten,
    # and check with the inserted prefix removed so the replacement cannot match itself.
    shield = "\0SCRATCH\0"
    body = block.replace(_yaml_path(scratch), shield).replace("/tmp/", prefix)
    assert not re.search(r"(?<![\w.-])/tmp/", body.replace(prefix, "")), "a /tmp/ path survived the rewrite"
    body = body.replace(shield, _yaml_path(scratch))
    script = 'python() { "$AF_TEST_PY" -B "$@"; }\n' + body + "\n" + tail
    # From a UTF-8 file, not `bash -c`: the blocks carry non-ASCII prose, and a Windows command
    # line re-encodes it on the way into bash.
    path = scratch / "preamble.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run(
        [_bash(), str(path)], env=env, capture_output=True, text=True, encoding="utf-8",
        timeout=120, stdin=subprocess.DEVNULL,
    )


# --------------------------------------------------------------------------- #
# A throwaway config home
# --------------------------------------------------------------------------- #
def _corpus(p: Path, *, project: str | None) -> Path:
    (p / "endpoints").mkdir(parents=True, exist_ok=True)
    (p / "endpoints" / "x.md").write_text("page\n", encoding="utf-8")
    (p / ".manifest.json").write_text(json.dumps({"project": project} if project else {}), encoding="utf-8")
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
    home.mkdir(parents=True, exist_ok=True)
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


@pytest.fixture
def box(tmp_path):
    """Two projects, each with its own root; no machine-level root, so nothing can fall back."""
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    alpha = tmp_path / "alpha-corpus"
    beta = tmp_path / "beta-corpus"
    alpha.mkdir()
    beta.mkdir()
    _write_env(home, d, None)
    _write_project(home, "alpha", d, alpha)
    _write_project(home, "beta", d, beta)
    return {"home": home, "d": d, "tmp": tmp_path, "alpha": alpha, "beta": beta}


_REPORT = 'echo "AUTODOC=$AUTODOC"; echo "PROJECT=$PROJECT"; echo "REPO=$REPO"; echo PREAMBLE-COMPLETED\n'


def _init_preamble() -> str:
    return _block(INIT, POINTER_WRITE)


def _update_step1() -> str:
    return _block(UPDATE, POINTER_WRITE)


def _vars(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


# --------------------------------------------------------------------------- #
# autodoc-init resolves per project and refuses ambiguity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("project", ["alpha", "beta"])
def test_init_resolves_the_named_projects_own_root_over_the_session_default(box, project) -> None:
    other = "beta" if project == "alpha" else "alpha"
    r = _run_block(_init_preamble(), box["tmp"],
                   _env(box["home"], AUTODOC_PROJECT=project, AGENTFLOW_PROJECT=other), _REPORT)
    assert r.returncode == 0, r.stderr
    got = _vars(r.stdout)
    assert got["AUTODOC"] == _yaml_path(box[project]), (
        f"{project} resolved to {got['AUTODOC']!r}; its own root is {_yaml_path(box[project])!r}"
    )
    assert got["PROJECT"] == project
    assert got["REPO"] == f"owner/{project}-repo"


def test_init_carries_the_git_host_and_local_checkout_step_1_clones_from(box) -> None:
    """Step 1 picks its clone by host kind and falls back to the local checkout, so the preamble
    resolves both from config rather than leaving Step 1 to read the session's environment."""
    tail = 'echo "GIT_HOST_KIND=$GIT_HOST_KIND"; echo "LOCAL_CHECKOUT=$LOCAL_CHECKOUT"\n'
    r = _run_block(_init_preamble(), box["tmp"], _env(box["home"], AUTODOC_PROJECT="alpha"), tail)
    assert r.returncode == 0, r.stderr
    got = _vars(r.stdout)
    assert got["GIT_HOST_KIND"] == "local"
    assert got["LOCAL_CHECKOUT"] == box["d"]
    carrier = Path((box["tmp"] / "tmp" / "autodoc-env.current").read_text(encoding="utf-8").strip())
    text = carrier.read_text(encoding="utf-8")
    assert "GIT_HOST_KIND=local" in text and "LOCAL_CHECKOUT=" in text, "not carried to Step 1's block"


def test_init_refuses_an_ambiguous_multi_project_box(box) -> None:
    r = _run_block(_init_preamble(), box["tmp"], _env(box["home"], AGENTFLOW_PROJECT="alpha"), _REPORT)
    assert r.returncode != 0, f"guessed a project instead of refusing: {r.stdout!r}"
    assert "PREAMBLE-COMPLETED" not in r.stdout
    assert "AUTODOC_PROJECT" in r.stderr and "alpha, beta" in r.stderr, r.stderr
    assert not (box["tmp"] / "tmp" / "autodoc-env.current").exists(), "a refused preamble left a pointer"


def test_init_on_a_single_project_box_needs_no_name(tmp_path) -> None:
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    solo = tmp_path / "solo-corpus"
    solo.mkdir()
    _write_env(home, d, None)
    _write_project(home, "solo", d, solo)
    r = _run_block(_init_preamble(), tmp_path, _env(home), _REPORT)
    assert r.returncode == 0, r.stderr
    assert _vars(r.stdout)["AUTODOC"] == _yaml_path(solo)
    assert _vars(r.stdout)["PROJECT"] == "solo"


def test_init_names_the_migration_hazard_instead_of_a_traceback(tmp_path) -> None:
    """The project's own root is empty while the machine-level root still holds a corpus."""
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    machine = _corpus(tmp_path / "machine-corpus", project="machine")
    fresh = tmp_path / "gamma-new"
    fresh.mkdir()
    _write_env(home, d, machine)
    _write_project(home, "gamma", d, fresh)
    r = _run_block(_init_preamble(), tmp_path, _env(home, AUTODOC_PROJECT="gamma"), _REPORT)
    assert r.returncode != 0 and "PREAMBLE-COMPLETED" not in r.stdout
    assert "Traceback" not in r.stderr, r.stderr
    assert "FATAL" in r.stderr and "'gamma'" in r.stderr, r.stderr
    # For a first init the machine-level corpus is ANOTHER project's, so "move the corpus to the
    # resolved root" would move that project's pages into this one's. The remedy is to give the
    # corpus its owner and clear the machine-level field.
    assert "move the corpus" not in r.stderr, r.stderr
    assert "holds another corpus" in r.stderr, r.stderr
    assert "its own autodoc_root override" in r.stderr, r.stderr
    assert "set autodoc_root: null in environment.md" in r.stderr, r.stderr
    assert _yaml_path(machine) in r.stderr, r.stderr


# --------------------------------------------------------------------------- #
# autodoc-init refuses a root whose corpus documents another project
# --------------------------------------------------------------------------- #
def _legacy_box(tmp_path: Path, manifest_project: str | None) -> dict:
    """Two projects, neither with an override; the machine-level root holds one corpus."""
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    legacy = _corpus(tmp_path / "legacy-corpus", project=manifest_project)
    _write_env(home, d, legacy)
    _write_project(home, "alpha", d, None)
    _write_project(home, "beta", d, None)
    return {"home": home, "tmp": tmp_path, "legacy": legacy}


def test_init_refuses_a_root_whose_corpus_documents_another_project(tmp_path) -> None:
    """beta's OVERRIDE names alpha's corpus. The resolver never checks an override (gh#759), so
    this preamble's own manifest check is what stops a `--force` re-init rebuilding over
    alpha's pages."""
    b = _legacy_box(tmp_path, "alpha")
    _write_project(b["home"], "beta", _yaml_path(tmp_path), b["legacy"])
    r = _run_block(_init_preamble(), b["tmp"], _env(b["home"], AUTODOC_PROJECT="beta"), _REPORT)
    assert r.returncode != 0, f"init proceeded into another project's corpus: {r.stdout!r}"
    assert "PREAMBLE-COMPLETED" not in r.stdout and "run carrier:" not in r.stdout, r.stdout
    out = r.stdout + r.stderr
    assert "FATAL" in out and "'alpha'" in out and "'beta'" in out, out
    assert not (b["tmp"] / "tmp" / "autodoc-env.current").exists(), "a refused preamble wrote a pointer"


def test_init_without_an_override_never_resolves_onto_another_projects_machine_root(tmp_path) -> None:
    """gh#759: beta has no override and the machine-level root holds alpha's corpus. The
    resolver skips it, so beta gets its derived root when that exists and no root otherwise --
    never alpha's pages."""
    b = _legacy_box(tmp_path, "alpha")
    r = _run_block(_init_preamble(), b["tmp"], _env(b["home"], AUTODOC_PROJECT="beta"), _REPORT)
    assert r.returncode != 0, f"init proceeded into another project's corpus: {r.stdout!r}"
    assert "PREAMBLE-COMPLETED" not in r.stdout and "run carrier:" not in r.stdout, r.stdout
    assert not (b["tmp"] / "tmp" / "autodoc-env.current").exists(), "a refused preamble wrote a pointer"
    assert "no autodoc root resolves for project 'beta'" in r.stderr, r.stderr

    derived = tmp_path / "projects" / "beta" / "autodoc"
    derived.mkdir(parents=True)
    r = _run_block(_init_preamble(), b["tmp"], _env(b["home"], AUTODOC_PROJECT="beta"), _REPORT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _vars(r.stdout)["AUTODOC"] == _yaml_path(derived)


@pytest.mark.parametrize("manifest_project", ["beta", None], ids=["same project", "null project"])
def test_init_proceeds_when_the_corpus_is_this_projects_or_unowned(tmp_path, manifest_project) -> None:
    b = _legacy_box(tmp_path, manifest_project)
    r = _run_block(_init_preamble(), b["tmp"], _env(b["home"], AUTODOC_PROJECT="beta"), _REPORT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PREAMBLE-COMPLETED" in r.stdout
    assert _vars(r.stdout)["AUTODOC"] == _yaml_path(b["legacy"])


# --------------------------------------------------------------------------- #
# A refusal stops the run even when the shell already carries the values
# --------------------------------------------------------------------------- #
# The guards after each resolution test that a value is non-empty, not that THIS block set it.
# autodoc-update's own run carrier exports AUTODOC, SUITE_ROOT and PROJECT, so a reader who
# sourced a carrier and re-runs a preamble is exactly the shell these cases build.
def _inherited(d: str, autodoc: Path, project: str) -> dict[str, str]:
    return {"AUTODOC": _yaml_path(autodoc), "SUITE_ROOT": d, "PROJECT": project,
            "REPO": f"owner/{project}-repo", "BRANCH": "master"}


def _assert_stopped(r: subprocess.CompletedProcess[str], scratch: Path) -> None:
    assert r.returncode != 0, f"the run carried on past a refusal on inherited values: {r.stdout!r}"
    assert "FATAL" in r.stderr, r.stderr
    assert "run carrier:" not in r.stdout and "PREAMBLE-COMPLETED" not in r.stdout, r.stdout
    assert not (scratch / "tmp" / "autodoc-env.current").exists(), "a refused preamble wrote a pointer"


def test_update_step1_refusal_stops_with_AUTODOC_already_exported(box) -> None:
    # alpha's corpus is real and carries a manifest, so every later guard in the block would pass
    # on the inherited values: only the refusal itself can stop this run.
    _corpus(box["alpha"], project="alpha")
    env = _env(box["home"], **_inherited(box["d"], box["alpha"], "alpha"))
    r = _run_block(_update_step1(), box["tmp"], env, _REPORT)
    _assert_stopped(r, box["tmp"])
    assert "AUTODOC_PROJECT" in r.stderr, r.stderr


def test_init_ambiguity_refusal_stops_with_values_already_exported(box) -> None:
    env = _env(box["home"], **_inherited(box["d"], box["alpha"], "alpha"))
    r = _run_block(_init_preamble(), box["tmp"], env, _REPORT)
    _assert_stopped(r, box["tmp"])


def test_init_no_root_refusal_stops_with_values_already_exported(tmp_path) -> None:
    d = _yaml_path(tmp_path)
    home = tmp_path / "config"
    _write_env(home, d, None)
    _write_project(home, "solo", d, None)
    stale = tmp_path / "somebody-elses-corpus"
    stale.mkdir()
    env = _env(home, AUTODOC_PROJECT="solo", **_inherited(d, stale, "solo"))
    r = _run_block(_init_preamble(), tmp_path, env, _REPORT)
    _assert_stopped(r, tmp_path)
    assert "'solo'" in r.stderr, r.stderr


def _emitted_names(block: str) -> list[str]:
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", block, re.S)
    assert m, "the preamble no longer carries a quoted <<'PY' heredoc"
    names = re.findall(r"^\s*emit\(\"([A-Z_]+)\"", m.group(1), re.M)
    assert names, "the preamble emits nothing"
    return names


@pytest.mark.parametrize("which", ["init", "update"])
def test_the_resolution_is_gated_on_pythons_exit_status_and_clears_what_it_emits(which) -> None:
    """The stronger form: the python's own status stops the block, so no guard downstream has to
    tell an inherited value from an emitted one. The unset backs it for every name it emits."""
    block = _init_preamble() if which == "init" else _update_step1()
    shell = "\n".join(line for line in block.split("\n") if not line.lstrip().startswith("#"))
    assert not re.search(r"eval\s+\"\$\(python", shell), (
        f"{which}: the resolution is still eval'd straight from a substitution, whose failure eval "
        "cannot see"
    )
    gate = re.search(r"^\)\"\s*\|\|\s*exit 1\s*$", shell, re.M)
    capture = re.search(r"^(\w+)=\"\$\(python - <<'PY'$", shell, re.M)
    assert capture and gate and capture.start() < gate.start(), (
        f"{which}: the python's exit status is not what stops the block"
    )
    var = capture.group(1)
    assert re.search(r"^eval \"\$" + var + r"\"$", shell[gate.end():], re.M), (
        f"{which}: the captured resolution is never eval'd after its status gate"
    )
    unset = re.search(r"^unset ([A-Z_ ]+)$", shell[:capture.start()], re.M)
    assert unset, f"{which}: nothing clears the inherited values before the resolution"
    missing = set(_emitted_names(block)) - set(unset.group(1).split())
    assert not missing, f"{which}: emitted but never cleared first: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# The wrapper-tag leak check: "saw nothing bad" must not share a word with "saw nothing"
# --------------------------------------------------------------------------- #
# grep exits 1 on no match and 2 when it could not read its target, and `|| echo clean` cannot
# tell them apart. The block text is the real one, minus the carrier source line: the target
# variable is handed in directly so each case controls exactly what the check is pointed at.
_LEAK = re.compile(r"LEAKED WRAPPER TAGS")
_LEAK_TARGET = {"init": "RENDER_TARGET", "update": "AUTODOC"}


def _leak_block(which: str) -> str:
    block = _block(INIT if which == "init" else UPDATE, _LEAK)
    lines = [ln for ln in block.split("\n") if "/tmp/autodoc-env.current" not in ln]
    assert len(lines) == len(block.split("\n")) - 1, "expected exactly one carrier source line"
    return "\n".join(lines)


def _run_leak(which: str, tmp_path: Path, target: str) -> subprocess.CompletedProcess[str]:
    return _run_block(_leak_block(which), tmp_path, _env(None, **{_LEAK_TARGET[which]: target}))


def _page(root: Path, name: str, body: str) -> None:
    (root / "endpoints").mkdir(parents=True, exist_ok=True)
    (root / "endpoints" / name).write_text(body, encoding="utf-8")


@pytest.mark.parametrize("which", ["init", "update"])
def test_leak_check_positive_control_a_seeded_tag_is_reported(which, tmp_path) -> None:
    root = tmp_path / "tree"
    _page(root, "a.md", "fine\n")
    _page(root, "b.md", "x </content> y\n")
    r = _run_leak(which, tmp_path, _yaml_path(root))
    assert "LEAKED WRAPPER TAGS" in r.stdout, r.stdout + r.stderr
    assert "b.md" in r.stdout
    assert "clean" not in r.stdout.split()
    assert r.returncode != 0, f"a leaked tag was reported and the run still passed: {r.stdout!r}"


@pytest.mark.parametrize("which", ["init", "update"])
def test_leak_check_a_clean_tree_is_clean(which, tmp_path) -> None:
    root = tmp_path / "tree"
    _page(root, "a.md", "fine\n")
    r = _run_leak(which, tmp_path, _yaml_path(root))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == "clean", r.stdout


@pytest.mark.parametrize(("which", "case"), [
    *((w, c) for c in ("missing dir", "empty var", "no pages") for w in ("init", "update")),
    # update scans with --exclude-dir=.source, so pages only under the clone are no pages at all.
    ("update", "only .source pages"),
])
def test_leak_check_that_read_nothing_fails_instead_of_saying_clean(which, case, tmp_path) -> None:
    if case == "missing dir":
        target = _yaml_path(tmp_path / "no" / "such" / "dir")
    elif case == "empty var":
        target = ""
    elif case == "only .source pages":
        clone = tmp_path / "tree" / ".source"
        clone.mkdir(parents=True)
        (clone / "README.md").write_text("fine\n", encoding="utf-8")
        target = _yaml_path(tmp_path / "tree")
    else:
        (tmp_path / "empty-tree").mkdir()
        target = _yaml_path(tmp_path / "empty-tree")
    r = _run_leak(which, tmp_path, target)
    assert r.returncode != 0, f"{case}: the check looked at nothing and passed: {r.stdout!r}"
    assert "clean" not in r.stdout.split(), f"{case}: reported clean without reading a page: {r.stdout!r}"


# --------------------------------------------------------------------------- #
# autodoc-update's manifest block: the same unset-then-status shape as Step 1
# --------------------------------------------------------------------------- #
_MANIFEST_BLOCK = re.compile(r"^MANIFEST_VARS=", re.M)
_MANIFEST_TAIL = 'echo "source_path=$source_path"; echo MANIFEST-BLOCK-COMPLETED\n'


# A shell that sourced a previous run's carrier already holds all four names.
_STALE_MANIFEST = {"source_repo": "owner/stale-repo", "source_branch": "stale",
                   "source_path": "/stale/source", "project": "alpha"}


def _manifest_block(root: Path) -> str:
    """The real block, minus its carrier source line, run in a shell that already holds
    $AUTODOC, $PROJECT and all four manifest names. They are exported from the script, not the
    process environment: Windows environment names are case-insensitive, so `project` and
    `PROJECT` cannot both be handed in that way."""
    block = _block(UPDATE, _MANIFEST_BLOCK)
    lines = [ln for ln in block.split("\n") if "/tmp/autodoc-env.current" not in ln]
    assert len(lines) == len(block.split("\n")) - 1, "expected exactly one carrier source line"
    inherited = {"AUTODOC": _yaml_path(root), "PROJECT": "alpha", **_STALE_MANIFEST}
    # The carrier this block sources defines env_put; the test hands it a no-op in its place.
    prefix = "env_put() { :; }\n" + "".join(f"export {k}='{v}'\n" for k, v in inherited.items())
    return prefix + "\n".join(lines)


def _manifest_corpus(root: Path, **fields: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".manifest.json").write_text(json.dumps(fields), encoding="utf-8")
    return root


_FULL_MANIFEST = {"source_repo": "owner/alpha-repo", "source_branch": "master",
                  "source_path": "/fresh/source", "project": "alpha"}


def test_update_manifest_block_stops_on_the_pythons_refusal_with_the_fields_inherited(tmp_path) -> None:
    fields = {k: v for k, v in _FULL_MANIFEST.items() if k != "source_path"}
    root = _manifest_corpus(tmp_path / "corpus", **fields)
    env = _env(None)
    r = _run_block(_manifest_block(root), tmp_path, env, _MANIFEST_TAIL)
    assert r.returncode != 0, f"ran on with an inherited source_path: {r.stdout!r}"
    assert "MANIFEST-BLOCK-COMPLETED" not in r.stdout, r.stdout
    assert "has no source_path" in r.stderr, r.stderr
    # The python's own status stopped it, not the per-name guard that backs it up.
    assert "did not bind" not in r.stdout, f"stopped by the guard, not the python's status: {r.stdout!r}"


def test_update_manifest_block_clears_inherited_fields_before_binding(tmp_path) -> None:
    """A python that exits 0 without printing a name (the shape the guards exist for) must leave
    that name EMPTY, not holding what the shell inherited, so the guards can see it."""
    root = _manifest_corpus(tmp_path / "corpus", **_FULL_MANIFEST)
    env = _env(None)
    silent = "python() { :; }\n" + _manifest_block(root)
    r = _run_block(silent, tmp_path, env, _MANIFEST_TAIL)
    assert r.returncode != 0, f"guards passed on inherited values: {r.stdout!r}"
    assert "MANIFEST-BLOCK-COMPLETED" not in r.stdout, r.stdout
    assert "did not bind" in r.stdout, r.stdout


def test_update_manifest_block_binds_a_complete_manifest(tmp_path) -> None:
    """Control for the two cases above: the real python and a complete manifest run through."""
    root = _manifest_corpus(tmp_path / "corpus", **_FULL_MANIFEST)
    env = _env(None)
    r = _run_block(_manifest_block(root), tmp_path, env, _MANIFEST_TAIL)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MANIFEST-BLOCK-COMPLETED" in r.stdout
    assert _vars(r.stdout)["source_path"] == "/fresh/source"
