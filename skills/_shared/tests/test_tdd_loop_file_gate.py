"""tdd-loop's file gate checks the whole branch scope, and the SKILL's own steps pass it.

The gate (`skills/tdd-loop/file-gate.sh`) is the loop's hard drift check and part of its only
definition of done, so it must be wrong in neither direction:

* NOT FALSE-FAIL. Following SKILL Step 1 then Step 4 as written, with no edits, passes: the gate
  never counts its own allowlist or `.tdd-loop/`, and Step 1 hides `.tdd-loop/` via
  `info/exclude` (never `.gitignore`, which would itself be an out-of-scope edit). A ` (new)`
  marker on an allowlist line is an annotation, not part of the path. Running from a
  subdirectory gives the root's verdict, because every path is compared repo-root-relative.
* NOT FALSE-PASS. The scope is the merge-base with the base ref through the working tree plus
  untracked files, so a COMMITTED out-of-scope file still fails, and `--no-renames` makes both
  sides of a rename count, so `git mv` of an out-of-scope file onto an allowlisted name fails.
* NO VACUOUS BASE. A base it cannot resolve, or one that names the branch itself or its own
  remote copy, exits 2. The branch's upstream is NOT refused: Step 2 cuts from
  `origin/<base-branch>`, which git makes the upstream, and that is the right base.

Step 7 and the critic prompt diff against the resolved base ref, never a literal `main`, because
a master-based repo has no `main` and the diff errors.

Every repo here is a throwaway under tmp_path with its own identity, `core.autocrlf=false` and a
fixed initial branch, so the host's git config cannot change a verdict. The SKILL commands are
read out of SKILL.md, not restated, so a doc edit that breaks them breaks this file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "skills" / "tdd-loop"
GATE = SKILL_DIR / "file-gate.sh"
SKILL_MD = SKILL_DIR / "SKILL.md"
CRITIC_MD = SKILL_DIR / "critic-prompt.md"

BASE = "main"
BRANCH = "tdd/x"


def _bash() -> str | None:
    """Git's bash first: on Windows a bare PATH lookup can find the WSL launcher in system32."""
    git = shutil.which("git")
    if git:
        for base in list(Path(git).resolve().parents)[:3]:
            for rel in ("bin/bash.exe", "usr/bin/bash.exe", "bin/bash"):
                c = base / rel
                if c.is_file():
                    return str(c)
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


@pytest.fixture(scope="module")
def bash() -> str:
    b = _bash()
    if b is None or shutil.which("git") is None:
        pytest.skip("needs bash and git (Git for Windows ships both)")
    return b


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """`main` with src.py and other.py committed, checked out on a fresh `tdd/x` cut from it."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", BASE)
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    (root / "src.py").write_text("a = 1\n", encoding="utf-8", newline="\n")
    (root / "other.py").write_text("b = 1\n", encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "checkout", "-q", "-b", BRANCH)
    return root


def _section(heading: str) -> str:
    """The body of the SKILL section whose heading line starts with *heading*."""
    text = SKILL_MD.read_text(encoding="utf-8")
    m = re.search(rf"^#+ {re.escape(heading)}.*?$(.*?)(?=^#+ |\Z)", text, re.M | re.S)
    assert m, f"no section {heading!r} in {SKILL_MD.name}"
    return m.group(1)


def _first_bash_fence(body: str) -> str:
    m = re.search(r"^\s*```bash\n(.*?)^\s*```", body, re.M | re.S)
    assert m, "section has no ```bash fence"
    return "\n".join(line.strip() for line in m.group(1).splitlines())


def _sh(bash: str, cwd: Path, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash, "-c", script], cwd=cwd, capture_output=True, text=True, timeout=60, check=False
    )


def _gate(bash: str, cwd: Path, allowlist: str, *base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash, GATE.as_posix(), allowlist, *base],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _allow(repo: Path, *lines: str, exclude: bool = True) -> str:
    """Write `.tdd-loop/allowlist.txt`; *exclude* hides `.tdd-loop/` as Step 1 does."""
    (repo / ".tdd-loop").mkdir(exist_ok=True)
    (repo / ".tdd-loop" / "allowlist.txt").write_text(
        "".join(f"{x}\n" for x in lines), encoding="utf-8", newline="\n"
    )
    if exclude:
        with (repo / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(".tdd-loop/\n")
    return ".tdd-loop/allowlist.txt"


def _offenders(proc: subprocess.CompletedProcess[str]) -> set[str]:
    return {ln.strip() for ln in proc.stderr.splitlines() if ln.startswith("  ")}


# 1. The allowlist never trips the gate --------------------------------------------------------


def test_step1_then_step4_as_written_passes_with_no_edits(bash: str, repo: Path) -> None:
    step1 = _first_bash_fence(_section("1. Read spec"))
    r = _sh(bash, repo, step1)
    assert r.returncode == 0, r.stderr
    exclude = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip()
    )
    assert ".tdd-loop/" in exclude.read_text(encoding="utf-8").splitlines()
    assert not (repo / ".gitignore").exists(), "Step 1 must not edit a tracked ignore file"

    (repo / ".tdd-loop" / "allowlist.txt").write_text(
        "src.py\ntests/test_src.py (new)\n", encoding="utf-8", newline="\n"
    )
    step4 = _first_bash_fence(_section("4. Implement")).replace(
        "<suite_root>", REPO_ROOT.as_posix()
    )
    step4 = step4.replace("<base-ref>", BASE)
    assert "<" not in step4, f"unsubstituted placeholder in Step 4 command: {step4}"
    r = _sh(bash, repo, step4)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "file-gate OK" in r.stdout


def test_gate_never_counts_its_own_allowlist_even_unignored(bash: str, repo: Path) -> None:
    allow = _allow(repo, "src.py", exclude=False)
    (repo / ".tdd-loop" / "notes.txt").write_text("scratch\n", encoding="utf-8")
    r = _gate(bash, repo, allow, BASE)
    assert r.returncode == 0, r.stderr


# 2. The whole branch scope, both sides of a rename --------------------------------------------


@pytest.mark.parametrize("commit", [False, True], ids=["staged", "committed"])
def test_rename_of_out_of_scope_file_onto_allowlisted_name_fails(
    bash: str, repo: Path, commit: bool
) -> None:
    allow = _allow(repo, "new.py")
    _git(repo, "mv", "other.py", "new.py")
    if commit:
        _git(repo, "commit", "-q", "-m", "mv")
    r = _gate(bash, repo, allow, BASE)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _offenders(r) == {"other.py"}


def test_committed_out_of_scope_file_on_branch_fails(bash: str, repo: Path) -> None:
    allow = _allow(repo, "src.py")
    (repo / "x.py").write_text("y\n", encoding="utf-8")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-q", "-m", "impl")
    r = _gate(bash, repo, allow, BASE)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _offenders(r) == {"x.py"}


def test_base_ahead_of_branch_point_does_not_count(bash: str, repo: Path) -> None:
    """Commits on the base after the cut are not the branch's: the diff starts at the merge-base."""
    allow = _allow(repo, "src.py")
    _git(repo, "checkout", "-q", BASE)
    (repo / "upstream.py").write_text("u\n", encoding="utf-8")
    _git(repo, "add", "upstream.py")
    _git(repo, "commit", "-q", "-m", "base moves on")
    _git(repo, "checkout", "-q", BRANCH)
    (repo / "src.py").write_text("a = 2\n", encoding="utf-8")
    r = _gate(bash, repo, allow, BASE)
    assert r.returncode == 0, r.stderr


def test_unresolvable_or_self_base_fails_loudly(bash: str, repo: Path) -> None:
    allow = _allow(repo, "src.py")
    for base in [("no-such-branch",), (BRANCH,), ()]:
        r = _gate(bash, repo, allow, *base)
        assert r.returncode == 2, f"base {base}: rc={r.returncode} {r.stdout}{r.stderr}"
        assert "file-gate OK" not in r.stdout


def test_base_defaults_to_origin_head(bash: str, repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _git(repo, "init", "-q", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", BASE)
    _git(repo, "remote", "set-head", "origin", BASE)
    allow = _allow(repo, "src.py")
    (repo / "x.py").write_text("y\n", encoding="utf-8")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-q", "-m", "impl")
    r = _gate(bash, repo, allow)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _offenders(r) == {"x.py"}


# 3. No literal main in the critic diff --------------------------------------------------------


@pytest.mark.parametrize("doc", [SKILL_MD, CRITIC_MD], ids=lambda p: p.name)
def test_docs_diff_against_resolved_base_not_literal_main(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    assert "main...HEAD" not in text
    assert "git diff <base-ref>...HEAD" in text


def test_skill_describes_what_the_gate_compares() -> None:
    body = _section("File Gate")
    assert "merge-base" in body and "--no-renames" in body and "untracked" in body
    assert "`git diff --name-only HEAD`" not in body


# 4. (new) markers and subdirectories ----------------------------------------------------------


def test_new_marker_matches_new_file(bash: str, repo: Path) -> None:
    allow = _allow(repo, "src.py", "tests/test_src.py (new)")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_src.py").write_text("t\n", encoding="utf-8")
    r = _gate(bash, repo, allow, BASE)
    assert r.returncode == 0, r.stderr


def test_crlf_allowlist_lines_match(bash: str, repo: Path) -> None:
    allow = _allow(repo)
    (repo / allow).write_bytes(b"src.py\r\ntests/test_src.py (new)\r\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_src.py").write_text("t\n", encoding="utf-8")
    (repo / "src.py").write_text("a = 2\n", encoding="utf-8")
    r = _gate(bash, repo, allow, BASE)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("in_scope", [True, False], ids=["in-scope", "out-of-scope"])
def test_subdirectory_gives_root_verdict(bash: str, repo: Path, in_scope: bool) -> None:
    lines = ["src.py", "tests/test_src.py"] if in_scope else ["src.py"]
    _allow(repo, *lines)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_src.py").write_text("t\n", encoding="utf-8")
    if not in_scope:
        (repo / "x.py").write_text("y\n", encoding="utf-8")
    root = _gate(bash, repo, ".tdd-loop/allowlist.txt", BASE)
    sub = _gate(bash, repo / "tests", "../.tdd-loop/allowlist.txt", BASE)
    assert root.returncode == (0 if in_scope else 1), root.stdout + root.stderr
    assert sub.returncode == root.returncode, sub.stdout + sub.stderr
    assert _offenders(sub) == _offenders(root)
    if not in_scope:
        assert _offenders(root) == {"tests/test_src.py", "x.py"}


# Base resolution against a real remote -------------------------------------------------------


def _tracking_branch_from_origin(repo: Path, tmp_path: Path) -> None:
    """Re-cut `tdd/x` from `origin/main` the way Step 2 says, so it tracks `origin/main`."""
    remote = tmp_path / "remote.git"
    _git(repo, "init", "-q", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", BASE)
    _git(repo, "remote", "set-head", "origin", BASE)
    _git(repo, "checkout", "-q", BASE)
    _git(repo, "branch", "-q", "-D", BRANCH)
    _git(repo, "checkout", "-q", "--track", "-b", BRANCH, f"origin/{BASE}")
    assert _git(repo, "rev-parse", "--abbrev-ref", "@{upstream}").strip() == f"origin/{BASE}"


@pytest.mark.parametrize("base", [(f"origin/{BASE}",), ()], ids=["explicit", "origin-head"])
def test_branch_tracking_its_base_gets_a_content_verdict(
    bash: str, repo: Path, tmp_path: Path, base: tuple[str, ...]
) -> None:
    """Step 2 cuts from origin/<base-branch>, which git makes the branch's upstream: not a self-base."""
    _tracking_branch_from_origin(repo, tmp_path)
    allow = _allow(repo, "src.py")
    (repo / "src.py").write_text("a = 2\n", encoding="utf-8")
    ok = _gate(bash, repo, allow, *base)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    (repo / "x.py").write_text("y\n", encoding="utf-8")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-q", "-m", "impl")
    bad = _gate(bash, repo, allow, *base)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert _offenders(bad) == {"x.py"}


def test_branch_own_remote_counterpart_is_refused_as_base(
    bash: str, repo: Path, tmp_path: Path
) -> None:
    _tracking_branch_from_origin(repo, tmp_path)
    _git(repo, "push", "-q", "origin", BRANCH)
    allow = _allow(repo, "src.py")
    r = _gate(bash, repo, allow, f"origin/{BRANCH}")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "file-gate OK" not in r.stdout


# Allowlist parsing edges ----------------------------------------------------------------------


def test_inline_comment_is_stripped(bash: str, repo: Path) -> None:
    allow = _allow(repo, "src.py  # the module under test", "tests/test_src.py (new) # added")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_src.py").write_text("t\n", encoding="utf-8")
    (repo / "src.py").write_text("a = 2\n", encoding="utf-8")
    r = _gate(bash, repo, allow, BASE)
    assert r.returncode == 0, r.stderr


def test_tracked_allowlist_outside_tdd_loop_is_not_exempt(bash: str, repo: Path) -> None:
    """Only the loop's untracked state is exempt; an allowlist committed on the branch is a change."""
    (repo / "sub").mkdir()
    (repo / "sub" / "allow.txt").write_text("src.py\n", encoding="utf-8")
    _git(repo, "add", "sub/allow.txt")
    _git(repo, "commit", "-q", "-m", "allowlist")
    r = _gate(bash, repo, "sub/allow.txt", BASE)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _offenders(r) == {"sub/allow.txt"}
    _git(repo, "rm", "-q", "--cached", "sub/allow.txt")
    _git(repo, "commit", "-q", "-m", "untrack")
    r = _gate(bash, repo, "sub/allow.txt", BASE)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create these names")
def test_path_git_would_quote_matches_exactly(bash: str, repo: Path) -> None:
    name = 'we"ird\\na\tme.py'
    allow = _allow(repo, name)
    (repo / name).write_text("w\n", encoding="utf-8")
    r = _gate(bash, repo, allow, BASE)
    assert r.returncode == 0, r.stdout + r.stderr


def test_non_ascii_path_matches_exactly(bash: str, repo: Path) -> None:
    """git would octal-quote this without -z; it must still match its allowlist line."""
    name = "café file.py"
    allow = _allow(repo, name)
    (repo / name).write_text("w\n", encoding="utf-8")
    r = _gate(bash, repo, allow, BASE)
    assert r.returncode == 0, r.stdout + r.stderr
