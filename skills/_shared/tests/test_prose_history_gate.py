"""The instruction-prose history gate (`.github/scripts/prose_history_gate.py`) does what CI relies on.

The gate is a step in ci.yml's `sanitize-gate` job, not a pytest test: one slip in the prose
should cost one red check, not one per leg of this suite's matrix. So this file never scans the
real tree for hits. It proves the gate itself.

WHAT IT CAN PROVE
* Wiring: the named step sits in the `sanitize-gate` job after the literal scan, runs the script,
  carries `if: ${{ !cancelled() }}`, and leaves `pillar_residue`'s single `pattern=` line single.
* Each refused token class, planted alone in a throwaway git repo with its own LICENSE, fails and
  names its `file:line`; each example form the prose is allowed to use passes. The allowlist is
  passed in, so these cases do not depend on the real `DATE_OK`.
* Every real `DATE_OK` entry carries a reason and still matches a line in its file.
* The set derived from this checkout is not vacuous and excludes what it must.
* It fails closed (exit 2, "did not run") outside a git repo, on an empty set, on a LICENSE with
  no copyright line, and on a named path outside the set; a stale allowlist entry exits 1.

WHAT IT CANNOT PROVE
* That the prose is free of narrative with no token. That is the full read at review time.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "prose_history_gate.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

JOB = "sanitize-gate"
STEP = "Instruction prose carries no history"
LITERAL_STEP = "Grep for personal/product literals"
DID_NOT_RUN = "the scan did not run"

HOLDER = "Quillfeather"  # the fixture repo's copyright holder; no real person
PLANT = "skills/demo/SKILL.md"
PLANT_LINE = 3

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="the gate reads git ls-files")


def _load_gate():
    spec = importlib.util.spec_from_file_location("prose_history_gate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway checkout: one guarded file per kind, plus paths the set must leave out."""
    root = tmp_path / "repo"
    files = {
        "LICENSE": f"MIT License\n\nCopyright (c) 2026 Jane {HOLDER}\n",
        PLANT: "# Demo\n\nclean line\n",
        "commands/demo.md": "a command wrapper\n",
        "skills/demo/tests/fixture.md": "gh#1 Q-10001 2026-01-02\n",
        "skills/_vendored/other/SKILL.md": "gh#1 Q-10001 2026-01-02\n",
        "skills/demo/helper.py": "# gh#1\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


def _plant(repo: Path, text: str) -> None:
    (repo / PLANT).write_text(f"# Demo\n\n{text}\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 1. Wiring
# ---------------------------------------------------------------------------


def test_the_step_is_wired_into_the_sanitize_job_after_the_literal_scan():
    wf = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    steps = wf["jobs"][JOB]["steps"]
    names = [s.get("name") for s in steps]
    assert names.count(STEP) == 1, names
    assert names.index(STEP) > names.index(LITERAL_STEP), names
    step = steps[names.index(STEP)]
    assert step["run"].strip() == "python3 .github/scripts/prose_history_gate.py"
    assert step.get("if") == "${{ !cancelled() }}"


def test_the_sanitize_job_still_holds_exactly_one_pattern_line():
    from agentflow import pillar_residue

    line = pillar_residue.sanitize_pattern_line(CI_WORKFLOW)
    assert line.strip().startswith("pattern='"), line


# ---------------------------------------------------------------------------
# 2. Planted tokens
# ---------------------------------------------------------------------------

REFUSED = [
    ("gh#12", "GH"),
    ("GH#12", "GH"),
    ("issue #9", "ISSUE"),
    ("Q-10001", "ID"),
    ("E-0731", "ID"),
    ("E-0042", "ID"),
    ("2026-01-02", "DATE"),
    (HOLDER, "NAME"),
]


@pytest.mark.parametrize("token,kind", REFUSED, ids=[k + ":" + t for t, k in REFUSED])
def test_a_planted_history_token_fails_and_names_its_line(repo, capsys, token, kind):
    _plant(repo, f"a rule, see {token} for why")
    rc = gate.run([], root=repo, date_ok={})
    out = capsys.readouterr().out
    assert rc == 1, out
    assert f"{PLANT}:{PLANT_LINE}: {kind} " in out, out
    assert "1 line(s) carry history" in out, out
    assert "::error::" in out, out


ALLOWED = ["Q-0007", "B-0012", "E-NNNN", "gh#N", "written to specs/2026-05-10-demo.md"]
DEMO_OK = {PLANT: (("specs/2026-05-10-", "path example"),)}


@pytest.mark.parametrize("text", ALLOWED)
def test_an_example_form_passes(repo, capsys, text):
    _plant(repo, f"for example {text}")
    # An allowlist entry must match its file, so only the dated case is handed one.
    rc = gate.run([], root=repo, date_ok=DEMO_OK if "2026" in text else {})
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "2 files scanned; 0 line(s) carry history: none" in out, out


def test_a_date_outside_its_allowlisted_literal_still_fails(repo, capsys):
    _plant(repo, "specs/2026-05-10-demo.md, decided 2026-05-11")
    rc = gate.run([], root=repo, date_ok={PLANT: (("specs/2026-05-10-", "path example"),)})
    out = capsys.readouterr().out
    assert rc == 1 and f"{PLANT}:{PLANT_LINE}: DATE 2026-05-11" in out, out
    assert "2026-05-10" not in out.split("files scanned")[0], out


def test_a_stale_allowlist_entry_fails(repo, capsys):
    rc = gate.run([], root=repo, date_ok={PLANT: (("specs/2026-05-10-", "path example"),)})
    out = capsys.readouterr().out
    assert rc == 1 and "STALE DATE_OK entry" in out and PLANT in out, out


def test_one_file_mode_scans_only_that_file(repo, capsys):
    _plant(repo, "see gh#12")
    rc = gate.run(["commands/demo.md"], root=repo, date_ok={})
    out = capsys.readouterr().out
    assert rc == 0 and "1 files scanned; 0 line(s)" in out, out


# ---------------------------------------------------------------------------
# 3. The real allowlist
# ---------------------------------------------------------------------------


def test_every_real_date_ok_entry_has_a_reason_and_matches_its_file():
    assert gate.DATE_OK, "the allowlist parsed empty"
    for path, entries in gate.DATE_OK.items():
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        for literal, reason in entries:
            assert gate.DATE.search(literal), f"{path}: {literal!r} carries no date to allow"
            assert reason.strip(), f"{path}: {literal!r} has no reason"
            assert any(literal in ln for ln in text.splitlines()), f"{path}: {literal!r} is stale"


# ---------------------------------------------------------------------------
# 4. Non-vacuity of the derived set
# ---------------------------------------------------------------------------


def test_the_set_derived_from_this_checkout_is_the_prose_set():
    files = gate.derive_set(gate.repo_root(REPO_ROOT))
    for lane in ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor", "steward"):
        assert f"skills/{lane}/SKILL.md" in files, lane
    assert "install/SKILL.md" in files
    assert not [f for f in files if f.startswith("skills/_vendored/")]
    assert not [f for f in files if "/tests/" in f or "/test/" in f]
    assert not [f for f in files if f.endswith(".py")]


def test_the_fixture_set_excludes_vendored_tests_and_code(repo):
    assert gate.derive_set(repo) == ["commands/demo.md", PLANT]


# ---------------------------------------------------------------------------
# 5. Fail-closed
# ---------------------------------------------------------------------------


def test_outside_a_git_repo_it_exits_2_and_says_it_did_not_run(tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path))
    res = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=outside, env=env, capture_output=True, text=True, check=False
    )
    assert res.returncode == 2, res
    assert DID_NOT_RUN in res.stdout, res


def test_an_empty_set_exits_2(tmp_path, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "LICENSE").write_text(f"Copyright (c) 2026 Jane {HOLDER}\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    assert gate.run([], root=root, date_ok={}) == 2
    assert DID_NOT_RUN in capsys.readouterr().out


def test_a_license_with_no_copyright_line_exits_2(repo, capsys):
    (repo / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    assert gate.run([], root=repo, date_ok={}) == 2
    assert DID_NOT_RUN in capsys.readouterr().out


def test_a_named_path_outside_the_set_exits_2(repo, capsys):
    assert gate.run(["skills/demo/helper.py"], root=repo, date_ok={}) == 2
    out = capsys.readouterr().out
    assert DID_NOT_RUN in out and "not in the instruction-prose set" in out, out
