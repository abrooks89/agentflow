"""The sanitize gate fails CLOSED when grep errors, at all three of its scans (gh#447).

`grep` exits 0 on a match, 1 on no match and 2 on error. The gate used to read each scan as
`hits=$(grep ... || true)` and branch on whether `hits` was empty, so an erroring grep - which
prints nothing - took the clean branch and passed the job. It now branches on the exit code.

WHAT IT CAN PROVE
* The step's own `run:` block, parsed out of `.github/workflows/ci.yml` rather than restated
  here, run under bash against a throwaway directory: a clean tree passes with the clean
  message, a planted literal fails, and a grep that exits 2 fails with the "did not run"
  message - at the tree scan, the PR-title scan and the PR-body scan separately.
* That the error test discriminates: the same forced error against a copy of the script with
  the tree site reverted to the old `|| true` idiom goes GREEN, which is the defect.

HOW THE ERROR IS FORCED
* A `grep` shim first on PATH exits 2 for the one scan under test and hands every other call
  to the real grep. The shipped pattern is never edited, so the scans run exactly as CI runs
  them. The shim picks the tree scan by its `-r` flag and a stdin scan by a marker in its input.
* One test also uses a REAL grep error (an unreadable file). It skips where the file stays
  readable - Windows, or root - because a skip there is honest and a pass would not be.

WHAT IT CANNOT PROVE
* That GitHub hands the step the title/body env it hands it here; that is the workflow's
  `env:` block, which this file does not execute.

The literals planted below are generic shapes the shipped pattern carries, assembled from pieces
so this file does not trip the gate it tests. The extra pattern the step can append from a
repository secret is out of this file's scope (EXTRA_PATTERN is cleared for every run here);
test_ci_workflow_shape.py drives that half.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

JOB = "sanitize-gate"
STEP = "Grep for personal/product literals"

CLEAN_TREE = "sanitize gate clean: no personal/product literals in first-party files"
CLEAN_TITLE = "sanitize gate clean: no personal/product literals in the PR title"
CLEAN_BODY = "sanitize gate clean: no personal/product literals in the PR body"
DID_NOT_RUN = "the scan did not run. Failing closed."

# The mangled session-directory shape, split at the account so this line is not a match.
PLANTED = "C--Users-" + "qzacct7"
FAIL_MARK = "SHIM-FAIL-THIS-SCAN"
SHIM_FIRED = "grep: shim-forced error"

# The old idiom at the tree site, for the discrimination test.
FIXED_TREE_TAIL = '-e "$pattern" .) || hits_rc=$?'
OLD_TREE_TAIL = '-e "$pattern" . || true); hits_rc=$([ -n "$hits" ] && echo 0 || echo 1)'


def _bash() -> str:
    """A bash that can run the step: derived from `git` first, bare PATH last.

    Same derivation as test_pr_review_pointer_gate.py: on Windows a bare `which("bash")` finds
    the WSL launcher before Git's shell.
    """
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
        pytest.skip("no usable bash found (Git for Windows ships one); the ubuntu CI leg runs this test")
    return candidates[0]


@pytest.fixture(scope="module")
def bash() -> str:
    b = _bash()
    probe = subprocess.run(
        [b, "-c", "export LC_ALL=C.UTF-8; printf 'abc' | grep -qP -e '(?<!x)b'"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"bash has no working `grep -P` under C.UTF-8 (rc={probe.returncode}: {probe.stderr.strip()})")
    return b


def _step_script() -> str:
    """The step's `run:` block, read out of the workflow CI executes - never a copy."""
    wf = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    matches = [s for s in wf["jobs"][JOB]["steps"] if s.get("name") == STEP]
    assert len(matches) == 1, f"expected exactly one {STEP!r} step in {JOB}, found {len(matches)}"
    return matches[0]["run"]


def _write_grep_shim(directory: Path) -> None:
    """A `grep` that exits 2 for the scan named by SHIM_FAIL, and is the real grep otherwise.

    SHIM_FAIL=tree fails the recursive tree scan; SHIM_FAIL=stdin fails a stdin scan whose
    input carries FAIL_MARK. The shim dir is PATH's first entry, so dropping it finds the real one.
    """
    shim = directory / "grep"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'PATH="${PATH#*:}"\n'
        'if [ "${SHIM_FAIL:-}" = tree ] && [ "$1" = -rniP ]; then\n'
        '  echo "grep: shim-forced error" >&2; exit 2\n'
        "fi\n"
        'if [ "${SHIM_FAIL:-}" = stdin ] && [ "$1" = -niP ]; then\n'
        "  input=$(cat)\n"
        f'  case "$input" in *{FAIL_MARK}*) echo "grep: shim-forced error" >&2; exit 2 ;; esac\n'
        "  printf '%s' \"$input\" | grep \"$@\"; exit $?\n"
        "fi\n"
        'exec grep "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(
    tmp_path: Path,
    bash: str,
    *,
    tree: dict[str, str] | None = None,
    event: str = "pull_request",
    title: str = "a clean title",
    body: str = "a clean body",
    shim_fail: str | None = None,
    script: str | None = None,
) -> subprocess.CompletedProcess:
    # The script lives OUTSIDE the scanned directory: it carries the pattern, which matches itself.
    script_path = tmp_path / "step.sh"
    script_path.write_text(script if script is not None else _step_script(), encoding="utf-8", newline="\n")
    scan_dir = tmp_path / "tree"
    scan_dir.mkdir(exist_ok=True)
    for name, text in (tree or {"README.md": "nothing to see here\n"}).items():
        (scan_dir / name).write_text(text, encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env.update({"EVENT_NAME": event, "PR_TITLE": title, "PR_BODY": body})
    env.pop("SHIM_FAIL", None)
    env.pop("EXTRA_PATTERN", None)
    argv = [bash, str(script_path)]
    if shim_fail is not None:
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir(exist_ok=True)
        _write_grep_shim(shim_dir)
        env["SHIM_FAIL"] = shim_fail
        # Prepended INSIDE bash, not via the child env: Git for Windows' bash.exe launcher puts
        # its own dirs in front of an inherited PATH, which would hide the shim. cygpath turns
        # the Windows path into one a colon-separated PATH can hold; Linux has none and needs none.
        argv = [
            bash,
            "-c",
            'd=$(cygpath -u "$1" 2>/dev/null || printf %s "$1"); PATH="$d:$PATH" exec bash "$2"',
            "_",
            str(shim_dir),
            str(script_path),
        ]
    return subprocess.run(
        argv,
        cwd=scan_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# ---------------------------------------------------------------------------
# 0 and 1: the behaviour the gate already had, unchanged
# ---------------------------------------------------------------------------


def test_clean_tree_title_and_body_pass(tmp_path, bash):
    r = _run(tmp_path, bash)
    assert r.returncode == 0, r.stdout + r.stderr
    for msg in (CLEAN_TREE, CLEAN_TITLE, CLEAN_BODY):
        assert msg in r.stdout
    assert DID_NOT_RUN not in r.stdout


def test_non_pr_event_runs_the_tree_half_only(tmp_path, bash):
    r = _run(tmp_path, bash, event="push")
    assert r.returncode == 0, r.stdout + r.stderr
    assert CLEAN_TREE in r.stdout
    assert "event is push, which carries no PR title or body" in r.stdout


def test_planted_literal_in_tree_fails(tmp_path, bash):
    r = _run(tmp_path, bash, tree={"notes.md": f"see {PLANTED} for details\n"})
    assert r.returncode != 0, r.stdout + r.stderr
    assert "literals found in tracked files" in r.stdout
    assert "notes.md" in r.stdout
    assert DID_NOT_RUN not in r.stdout


def test_planted_literal_in_title_fails(tmp_path, bash):
    r = _run(tmp_path, bash, title=f"fix {PLANTED}")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "literals in the PULL REQUEST TITLE" in r.stdout
    assert CLEAN_TREE in r.stdout


def test_planted_literal_in_body_fails(tmp_path, bash):
    r = _run(tmp_path, bash, body="line one\n" + "/ho" + "me/alice/x\n")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "literals in the PULL REQUEST BODY" in r.stdout
    assert CLEAN_TITLE in r.stdout


# ---------------------------------------------------------------------------
# 2: grep errored - each site fails closed, and says which scan
# ---------------------------------------------------------------------------


def test_tree_scan_grep_error_fails_closed(tmp_path, bash):
    r = _run(tmp_path, bash, shim_fail="tree")
    assert SHIM_FIRED in r.stderr, "the shim never ran; this test forced nothing\n" + r.stdout + r.stderr
    assert r.returncode != 0, r.stdout + r.stderr
    assert f"tree scan: grep exited 2 - {DID_NOT_RUN}" in r.stdout
    assert CLEAN_TREE not in r.stdout
    # The other halves still report before the job fails.
    assert CLEAN_TITLE in r.stdout and CLEAN_BODY in r.stdout


def test_title_scan_grep_error_fails_closed(tmp_path, bash):
    r = _run(tmp_path, bash, title=f"clean {FAIL_MARK}", shim_fail="stdin")
    assert SHIM_FIRED in r.stderr, "the shim never ran; this test forced nothing\n" + r.stdout + r.stderr
    assert r.returncode != 0, r.stdout + r.stderr
    assert f"PR title scan: grep exited 2 - {DID_NOT_RUN}" in r.stdout
    assert CLEAN_TITLE not in r.stdout
    assert CLEAN_TREE in r.stdout and CLEAN_BODY in r.stdout


def test_body_scan_grep_error_fails_closed(tmp_path, bash):
    r = _run(tmp_path, bash, body=f"clean {FAIL_MARK}", shim_fail="stdin")
    assert SHIM_FIRED in r.stderr, "the shim never ran; this test forced nothing\n" + r.stdout + r.stderr
    assert r.returncode != 0, r.stdout + r.stderr
    assert f"PR body scan: grep exited 2 - {DID_NOT_RUN}" in r.stdout
    assert CLEAN_BODY not in r.stdout
    assert CLEAN_TREE in r.stdout and CLEAN_TITLE in r.stdout


def test_real_grep_error_on_unreadable_file_fails_closed(tmp_path, bash):
    """No shim: a file grep cannot open makes the real grep exit 2."""
    r_setup = _run(tmp_path, bash)  # builds tmp_path/tree
    assert r_setup.returncode == 0, r_setup.stdout + r_setup.stderr
    locked = tmp_path / "tree" / "locked.txt"
    locked.write_text("harmless\n", encoding="utf-8")
    locked.chmod(0)
    try:
        readable = subprocess.run([bash, "-c", f"[ -r '{locked.as_posix()}' ]"], check=False).returncode == 0
        if readable:
            pytest.skip("chmod 0 leaves the file readable here (Windows, or running as root); the shim tests cover exit 2")
        r = _run(tmp_path, bash)
        assert r.returncode != 0, r.stdout + r.stderr
        assert f"tree scan: grep exited 2 - {DID_NOT_RUN}" in r.stdout
        assert CLEAN_TREE not in r.stdout
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# The error tests discriminate: the old idiom goes green under the same forced error
# ---------------------------------------------------------------------------


def test_old_or_true_idiom_reads_the_forced_error_as_clean(tmp_path, bash):
    script = _step_script()
    assert script.count(FIXED_TREE_TAIL) == 1, "the tree scan's shape changed; update this mutation"
    mutated = script.replace(FIXED_TREE_TAIL, OLD_TREE_TAIL)
    r = _run(tmp_path, bash, shim_fail="tree", script=mutated)
    assert SHIM_FIRED in r.stderr, "the shim never ran; the mutation proves nothing\n" + r.stdout + r.stderr
    assert r.returncode == 0, "the mutation did not reproduce gh#447; the error test above proves nothing\n" + r.stdout + r.stderr
    assert CLEAN_TREE in r.stdout
