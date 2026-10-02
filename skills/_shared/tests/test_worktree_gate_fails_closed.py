"""The worktree tool/flag gate fails CLOSED when grep errors, at both of its scans (gh#509).

`grep` exits 0 on a match, 1 on no match and 2 on error. The gate used to read the hits scan as
`hits=$(grep ... || true)` and branch on whether `hits` was empty, so an erroring grep - which
prints nothing - took the clean branch and passed the job. The file count above it piped the
same grep into `wc -l || true`, so a grep that errored on SOME files still counted more than
zero and the gate carried on. Both now branch on grep's own exit code - the shape gh#447 gave
the sanitize gate (test_sanitize_gate_fails_closed.py).

WHAT IT CAN PROVE
* The step's own `run:` block, parsed out of `.github/workflows/ci.yml` rather than restated
  here, run under bash in a throwaway directory holding a `skills/` tree: a clean tree passes
  with the clean message, a planted tool name or isolation flag fails, and a grep that exits 2
  fails closed - at the hits scan and the file-count scan separately, both when grep printed
  nothing and when it errored part-way through real output.
* That the error tests discriminate: the same forced error against a copy of the script with a
  site reverted to the old `|| true` idiom goes GREEN, which is the defect.

HOW THE ERROR IS FORCED
* A `grep` shim first on PATH exits 2 for the one scan under test and hands every other call to
  the real grep. The shipped pattern is never edited. The shim picks the hits scan by its
  leading `-rniE` flag and the file-count scan by its leading `-rIl`.
* One test also uses a REAL grep error (an unreadable file). It skips where the file stays
  readable - Windows, or root - because a skip there is honest and a pass would not be.

WHAT IT CANNOT PROVE
* That `actions/checkout` hands the step the tree it hands it here.

This file sits inside the tree the gate scans, so the names it plants are assembled from pieces,
the same trick ci.yml plays on the sanitize pattern.
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

JOB = "worktree-tool-gate"
STEP = "Grep for native worktree tool / per-subagent isolation flag"

CLEAN = "worktree gate clean: no native worktree tool or isolation flag in the first-party skills tree"
FOUND = "worktree gate: a first-party skill file still names"
SCANNED_NONE = "worktree gate: scanned no files"
DID_NOT_RUN = "the scan did not run. Failing closed."
PART_WAY = "the scan errored part-way and ALSO found the hits above. Failing closed."

PLANTED_TOOL = "Enter" + "Worktree"
PLANTED_FLAG = "isolation" + ': "work' + 'tree"'
SHIM_FIRED = "grep: shim-forced error"

# The old idiom at each site, for the discrimination tests.
FIXED_HITS_TAIL = '-- "$pattern" skills/) || hits_rc=$?'
OLD_HITS_TAIL = '-- "$pattern" skills/ || true); hits_rc=$([ -n "$hits" ] && echo 0 || echo 1)'
FIXED_COUNT_TAIL = "-e '' skills/) || files_rc=$?"
OLD_COUNT_TAIL = "-e '' skills/ || true); files_rc=0"


def _bash() -> str:
    """A bash that can run the step: derived from `git` first, bare PATH last.

    Same derivation as test_sanitize_gate_fails_closed.py: on Windows a bare `which("bash")`
    finds the WSL launcher before Git's shell.
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
        [b, "-c", "printf 'abc\\n' | grep -qiE -e 'B' && grep --version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"bash has no working `grep -E` (rc={probe.returncode}: {probe.stderr.strip()})")
    return b


def _step_script() -> str:
    """The step's `run:` block, read out of the workflow CI executes - never a copy."""
    wf = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    matches = [s for s in wf["jobs"][JOB]["steps"] if s.get("name") == STEP]
    assert len(matches) == 1, f"expected exactly one {STEP!r} step in {JOB}, found {len(matches)}"
    return matches[0]["run"]


def _write_grep_shim(directory: Path) -> None:
    """A `grep` that exits 2 for the scan named by SHIM_FAIL, and is the real grep otherwise.

    SHIM_FAIL=hits / count exits 2 having printed nothing; hits-partial / count-partial runs the
    real grep first and then exits 2, which is what an error on one file part-way through looks
    like. The shim dir is PATH's first entry, so dropping it finds the real grep.
    """
    shim = directory / "grep"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'PATH="${PATH#*:}"\n'
        'case "${SHIM_FAIL:-}:$1" in\n'
        '  hits:-rniE|count:-rIl) echo "grep: shim-forced error" >&2; exit 2 ;;\n'
        '  hits-partial:-rniE|count-partial:-rIl) grep "$@"; echo "grep: shim-forced error" >&2; exit 2 ;;\n'
        "esac\n"
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
    shim_fail: str | None = None,
    script: str | None = None,
) -> subprocess.CompletedProcess:
    # The script lives OUTSIDE the scanned directory: it carries the pattern, which matches itself.
    script_path = tmp_path / "step.sh"
    script_path.write_text(script if script is not None else _step_script(), encoding="utf-8", newline="\n")
    work = tmp_path / "work"
    for name, text in (tree or {"lane/SKILL.md": "nothing to see here\n"}).items():
        target = work / "skills" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    (work / "skills").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("SHIM_FAIL", None)
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
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_shim_ran(r: subprocess.CompletedProcess) -> None:
    assert SHIM_FIRED in r.stderr, "the shim never ran; this test forced nothing\n" + r.stdout + r.stderr


# ---------------------------------------------------------------------------
# 0 and 1: the behaviour the gate already had, unchanged
# ---------------------------------------------------------------------------


def test_clean_tree_passes(tmp_path, bash):
    r = _run(tmp_path, bash)
    assert r.returncode == 0, r.stdout + r.stderr
    assert CLEAN in r.stdout
    assert "worktree gate scanning 1 file(s)" in r.stdout
    assert DID_NOT_RUN not in r.stdout


def test_planted_tool_name_fails(tmp_path, bash):
    r = _run(tmp_path, bash, tree={"lane/SKILL.md": f"call {PLANTED_TOOL.lower()} first\n"})
    assert r.returncode != 0, r.stdout + r.stderr
    assert FOUND in r.stdout
    assert "lane/SKILL.md" in r.stdout
    assert CLEAN not in r.stdout and DID_NOT_RUN not in r.stdout


def test_planted_isolation_flag_fails(tmp_path, bash):
    r = _run(tmp_path, bash, tree={"lane/workflows/wave.js": f"spawn({{ {PLANTED_FLAG} }})\n"})
    assert r.returncode != 0, r.stdout + r.stderr
    assert FOUND in r.stdout
    assert "wave.js" in r.stdout
    assert CLEAN not in r.stdout


def test_empty_skills_tree_fails_as_scanned_nothing(tmp_path, bash):
    r = _run(tmp_path, bash, tree={"lane/empty.md": ""})
    assert r.returncode != 0, r.stdout + r.stderr
    assert SCANNED_NONE in r.stdout
    assert DID_NOT_RUN not in r.stdout


# ---------------------------------------------------------------------------
# 2: grep errored - each site fails closed
# ---------------------------------------------------------------------------


def test_hits_scan_grep_error_fails_closed(tmp_path, bash):
    r = _run(tmp_path, bash, shim_fail="hits")
    _assert_shim_ran(r)
    assert r.returncode != 0, r.stdout + r.stderr
    assert f"worktree gate: grep exited 2 - {DID_NOT_RUN}" in r.stdout
    assert CLEAN not in r.stdout


def test_hits_scan_part_way_error_reports_the_hits_it_found(tmp_path, bash):
    r = _run(tmp_path, bash, tree={"lane/SKILL.md": f"use {PLANTED_TOOL}\n"}, shim_fail="hits-partial")
    _assert_shim_ran(r)
    assert r.returncode != 0, r.stdout + r.stderr
    assert f"worktree gate: grep exited 2 - {PART_WAY}" in r.stdout
    assert "lane/SKILL.md" in r.stdout
    assert CLEAN not in r.stdout


def test_count_scan_grep_error_fails_closed(tmp_path, bash):
    r = _run(tmp_path, bash, shim_fail="count")
    _assert_shim_ran(r)
    assert r.returncode != 0, r.stdout + r.stderr
    assert f"worktree gate: file-count grep exited 2 - {DID_NOT_RUN}" in r.stdout
    assert CLEAN not in r.stdout


def test_count_scan_part_way_error_fails_closed(tmp_path, bash):
    """The case the old `| wc -l || true` let through: some files listed, grep still exited 2."""
    r = _run(tmp_path, bash, shim_fail="count-partial")
    _assert_shim_ran(r)
    assert r.returncode != 0, r.stdout + r.stderr
    assert f"worktree gate: file-count grep exited 2 - {DID_NOT_RUN}" in r.stdout
    assert "worktree gate scanning" not in r.stdout
    assert CLEAN not in r.stdout


def test_real_grep_error_on_unreadable_file_fails_closed(tmp_path, bash):
    """No shim: a file grep cannot open makes the real grep exit 2."""
    r_setup = _run(tmp_path, bash)  # builds tmp_path/work/skills
    assert r_setup.returncode == 0, r_setup.stdout + r_setup.stderr
    locked = tmp_path / "work" / "skills" / "locked.md"
    locked.write_text("harmless\n", encoding="utf-8")
    locked.chmod(0)
    try:
        readable = subprocess.run([bash, "-c", f"[ -r '{locked.as_posix()}' ]"], check=False).returncode == 0
        if readable:
            pytest.skip("chmod 0 leaves the file readable here (Windows, or running as root); the shim tests cover exit 2")
        r = _run(tmp_path, bash)
        assert r.returncode != 0, r.stdout + r.stderr
        assert DID_NOT_RUN in r.stdout
        assert CLEAN not in r.stdout
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# The error tests discriminate: the old idiom goes green under the same forced error
# ---------------------------------------------------------------------------


def test_old_or_true_idiom_at_hits_site_reads_the_forced_error_as_clean(tmp_path, bash):
    script = _step_script()
    assert script.count(FIXED_HITS_TAIL) == 1, "the hits scan's shape changed; update this mutation"
    r = _run(tmp_path, bash, shim_fail="hits", script=script.replace(FIXED_HITS_TAIL, OLD_HITS_TAIL))
    _assert_shim_ran(r)
    assert r.returncode == 0, "the mutation did not reproduce gh#509; the error test proves nothing\n" + r.stdout + r.stderr
    assert CLEAN in r.stdout


def test_old_or_true_idiom_at_count_site_passes_a_part_way_error(tmp_path, bash):
    script = _step_script()
    assert script.count(FIXED_COUNT_TAIL) == 1, "the file-count scan's shape changed; update this mutation"
    r = _run(tmp_path, bash, shim_fail="count-partial", script=script.replace(FIXED_COUNT_TAIL, OLD_COUNT_TAIL))
    _assert_shim_ran(r)
    assert r.returncode == 0, "the mutation did not reproduce gh#509; the error test proves nothing\n" + r.stdout + r.stderr
    assert CLEAN in r.stdout
