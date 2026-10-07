"""autodoc-update Step 2 refuses to diff across a rewritten history (gh#420).

`git diff FROM TO` is a tree diff and succeeds across unrelated histories, so a manifest whose
`last_sha` is not an ancestor of `origin/<branch>` reported ~30 phantom changed files on every
run, silently. Step 2 now carries an ancestry guard between computing FROM/TO and the first
`reset --hard` / `diff`.

WHAT IT CAN PROVE
* The guard lives in the Step 2 bash block (found by heading, not line number) and precedes
  the `reset --hard` and the first `git -C "$SRC" diff`.
* The guard lines themselves, extracted from SKILL.md (never a copy), exit nonzero naming both
  shas when FROM is off TO's history or missing from the clone, and exit 0 when FROM is an
  ancestor of TO.
* The resolve-guard lines (FROM= and TO=) also run, and stop with their own message when the
  manifest's last_sha or the branch ref cannot be resolved (gh#1008).

WHAT IT CANNOT PROVE
* That an operator picks a correct re-base point. The guard deliberately does not pick one.
* The rest of Step 2 (fetch, reset, diffs, env carrier) - only the two guarded regions run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL = REPO_ROOT / "skills" / "autodoc-update" / "SKILL.md"
STEP2_HEADING = "## Step 2 — Pull the source repo and diff"
GUARD_START = "# ancestry-guard:"
GUARD_END = "# end ancestry-guard"


def _step2_block() -> str:
    text = SKILL.read_text(encoding="utf-8")
    idx = text.find(STEP2_HEADING)
    assert idx >= 0, f"Step 2 heading not found in {SKILL}"
    m = re.search(r"```bash\n(.*?)```", text[idx:], re.S)
    assert m, "no ```bash block under the Step 2 heading"
    return m.group(1)


def _guard_lines() -> str:
    block = _step2_block()
    assert GUARD_START in block and GUARD_END in block, "ancestry-guard markers missing from Step 2"
    return block[block.index(GUARD_START) : block.index(GUARD_END)]


def test_guard_checks_present_in_step2() -> None:
    guard = _guard_lines()
    assert re.search(r'merge-base --is-ancestor "\$FROM" "\$TO"', guard), guard
    assert re.search(r'cat-file -e "\$FROM\^\{commit\}"', guard), guard
    assert guard.count("exit 1") >= 2, "each check must exit 1 on failure"


def test_guard_precedes_reset_and_diff() -> None:
    block = _step2_block()
    lines = block.splitlines()

    def first(pred) -> int:
        for i, line in enumerate(lines):
            if not line.lstrip().startswith("#") and pred(line):
                return i
        raise AssertionError("line not found in Step 2 block")

    ancestry = first(lambda s: "merge-base --is-ancestor" in s)
    exists = first(lambda s: "cat-file -e" in s)
    reset = first(lambda s: "reset --hard" in s)
    diff = first(lambda s: 'git -C "$SRC" diff' in s)
    to_line = first(lambda s: s.startswith("TO="))
    assert to_line < exists < ancestry, "checks must follow TO= and cat-file must run first"
    assert ancestry < reset and ancestry < diff, "ancestry check must precede reset --hard and diff"
    assert exists < reset and exists < diff, "existence check must precede reset --hard and diff"


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


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, **_GIT_ENV}
    r = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=True
    )
    return r.stdout.strip()


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _git("add", name, cwd=repo)
    _git("commit", "-q", "-m", f"edit {name}", cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


def _run_guard(src: Path, frm: str, to: str) -> subprocess.CompletedProcess[str]:
    script = "set -u\n" + _guard_lines() + "echo GUARD-PASSED\n"
    env = {
        **os.environ,
        **_GIT_ENV,
        "SRC": src.as_posix(),
        "FROM": frm,
        "TO": to,
        "source_branch": "master",
    }
    return subprocess.run(
        [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=60
    )


@pytest.fixture
def repos(tmp_path: Path):
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "master", str(origin))
    work = tmp_path / "work"
    _git("init", "-q", "-b", "master", str(work))
    _git("remote", "add", "origin", str(origin), cwd=work)
    a1 = _commit(work, "app.py", "v1\n")
    a2 = _commit(work, "lib.py", "v1\n")
    _git("push", "-q", "origin", "master", cwd=work)
    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone))
    return work, clone, a1, a2


def test_guard_passes_when_from_is_ancestor(repos) -> None:
    _work, clone, a1, a2 = repos
    to = _git("rev-parse", "origin/master", cwd=clone)
    assert to == a2
    r = _run_guard(clone, a1, to)
    assert r.returncode == 0, r.stderr
    assert "GUARD-PASSED" in r.stdout


def test_guard_refuses_rewritten_history(repos) -> None:
    work, clone, a1, _a2 = repos
    # Rewrite: a fresh root with overlapping files, force-pushed over master.
    _git("checkout", "-q", "--orphan", "rewrite", cwd=work)
    _git("rm", "-rq", "--cached", ".", cwd=work)
    # Content differs from a1: the same tree, message and second would hash to a1 exactly and
    # make it an ancestor of the "rewritten" history after all.
    _commit(work, "app.py", "v1 rewritten\n")
    _commit(work, "lib.py", "v2\n")
    _git("push", "-q", "--force", "origin", "rewrite:master", cwd=work)
    _git("fetch", "-q", "origin", "master", cwd=clone)
    to = _git("rev-parse", "origin/master", cwd=clone)
    # FROM's object is still in the clone - exactly the issue's state - but off TO's history.
    _git("cat-file", "-e", f"{a1}^{{commit}}", cwd=clone)
    r = _run_guard(clone, a1, to)
    assert r.returncode != 0
    assert "GUARD-PASSED" not in r.stdout
    assert a1 in r.stderr and to in r.stderr, r.stderr
    assert "not an ancestor" in r.stderr and "phantom" in r.stderr, r.stderr


def test_guard_refuses_missing_from_object(repos, tmp_path: Path) -> None:
    _work, clone, _a1, a2 = repos
    other = tmp_path / "other"
    _git("init", "-q", "-b", "master", str(other))
    ghost = _commit(other, "x.txt", "never in the clone\n")
    r = _run_guard(clone, ghost, a2)
    assert r.returncode != 0
    assert "GUARD-PASSED" not in r.stdout
    assert ghost in r.stderr and a2 in r.stderr, r.stderr
    assert "is not a commit in" in r.stderr, r.stderr


# --------------------------------------------------------------------------------------------
# resolve-guard (gh#1008): a FROM or TO that could not be resolved stops with its own message,
# before the ancestry guard can misreport it as a rewritten history.
# --------------------------------------------------------------------------------------------
RESOLVE_START = "# resolve-guard:"
RESOLVE_END = "# end resolve-guard"


def _resolve_lines() -> str:
    block = _step2_block()
    assert RESOLVE_START in block and RESOLVE_END in block, "resolve-guard markers missing"
    return block[block.index(RESOLVE_START) : block.index(RESOLVE_END)]


def _run_resolve(src: Path, autodoc: Path, branch: str = "master"):
    script = "set -u\n" + _resolve_lines() + 'echo "RESOLVED FROM=$FROM TO=$TO"\n'
    env = {**os.environ, **_GIT_ENV, "SRC": src.as_posix(), "AUTODOC": autodoc.as_posix(),
           "source_branch": branch}
    return subprocess.run([_bash(), "-c", script], env=env, capture_output=True, text=True,
                          timeout=60)


def _manifest(tmp_path: Path, body: str) -> Path:
    autodoc = tmp_path / "autodoc"
    autodoc.mkdir(exist_ok=True)
    (autodoc / ".manifest.json").write_text(body, encoding="utf-8")
    return autodoc


def test_resolve_guard_precedes_the_ancestry_guard() -> None:
    block = _step2_block()
    assert block.index(RESOLVE_END) < block.index(GUARD_START)
    resolve = _resolve_lines()
    assert resolve.count("exit 1") >= 2, "both FROM= and TO= must fail the block"


def test_resolve_guard_passes_on_a_readable_manifest_and_a_real_ref(repos, tmp_path) -> None:
    _work, clone, a1, a2 = repos
    r = _run_resolve(clone, _manifest(tmp_path, '{"last_sha": "%s"}' % a1))
    assert r.returncode == 0, r.stderr
    assert f"RESOLVED FROM={a1} TO={a2}" in r.stdout


@pytest.mark.parametrize("body", ["{}", '{"last_sha": ""}', '{"last_sha": null}', "not json"])
def test_unreadable_last_sha_is_named_and_not_called_a_rewrite(repos, tmp_path, body) -> None:
    _work, clone, _a1, _a2 = repos
    r = _run_resolve(clone, _manifest(tmp_path, body))
    assert r.returncode != 0 and "RESOLVED" not in r.stdout
    assert "could not read last_sha" in r.stderr, r.stderr
    assert "rewritten" not in r.stderr.replace("not a rewritten history", ""), r.stderr


def test_missing_manifest_is_named(repos, tmp_path) -> None:
    _work, clone, _a1, _a2 = repos
    empty = tmp_path / "no-manifest"
    empty.mkdir()
    r = _run_resolve(clone, empty)
    assert r.returncode != 0 and "could not read last_sha" in r.stderr, r.stderr


def test_unresolvable_branch_is_named_and_not_called_a_rewrite(repos, tmp_path) -> None:
    _work, clone, a1, _a2 = repos
    r = _run_resolve(clone, _manifest(tmp_path, '{"last_sha": "%s"}' % a1), branch="no-such")
    assert r.returncode != 0 and "RESOLVED" not in r.stdout
    assert "origin/no-such does not resolve" in r.stderr, r.stderr
    assert "rewritten" not in r.stderr.replace("not a rewritten history", ""), r.stderr
