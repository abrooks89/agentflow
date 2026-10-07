"""ci.yml and nightly.yml run the checks the ci-spend-cut epic says they run, on each event.

A private repo pays for every job minute, so ci.yml trims what a private PR runs and skips every
job on a private push; nightly.yml calls ci.yml to run the full matrix. The trims live in
GitHub expressions, which no local tool evaluates, so a mistake there only surfaces as a
missing or extra check on a live run - or, worse, as a required check that silently stopped
posting. This file pins the shape.

WHAT IT CAN PROVE
* For each event (pull_request, push, schedule, workflow_dispatch) on a private and a public
  repo, the set of check names ci.yml produces, computed by a small evaluator that knows EXACTLY
  the matrix, `if:` and `name:` expression shapes these files use and FAILS on any other shape
  rather than skipping it. A private PR runs a public PR's checks minus the trim set below; a
  schedule, a dispatch and a public push run everything a public PR runs except the provenance
  gate; a private push runs nothing.
* nightly.yml's shape: its two triggers, one call of ci.yml that fails open on its gate and
  passes `secrets: inherit`, the only write permission on the report job, and caller
  permissions covering the nested jobs'.
* Every pip `cache-dependency-path` names a file that exists.
* The gate and report `run:` blocks, parsed out of nightly.yml rather than restated here, run
  under bash with a `gh` shim first on PATH, across every case the spec lists.
* The sanitize gate's `run:` block, parsed out of ci.yml the same way (gh#821): the extra
  pattern from the SANITIZE_EXTRA_PATTERN repository secret joins the tree, PR-title and
  PR-body scans when set, the step says so in one notice line and scans the generic shapes
  when it is empty, and a broken one fails every scan closed. And that the shipped pattern
  carries only the generic shapes, pinned structurally so no private term is named here.

WHAT IT CANNOT PROVE
* That GitHub's evaluator agrees with this one. The evaluator reads only the expression shapes
  written in ci.yml; the PR's own run and a dispatch prove the real one.
* Anything about a live `gh` host: the shim answers from env vars and records its argv.
* That `secrets: inherit` on the nightly's call hands ci.yml the caller's SANITIZE_EXTRA_PATTERN
  secret without a workflow_call declaration; GitHub documents that it does, and a nightly run
  is what proves it.

BASH: as in test_pr_review_pointer_gate.py, bash is derived from where `git` lives before any
bare PATH lookup (the WSL stub in System32 is not a usable bash). With no usable bash each
script-running test SKIPS; the ubuntu CI leg runs them all.

No required-contexts list lives here on purpose: recording that list in the tree is gh#354's fix.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
NIGHTLY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly.yml"

# The eight legs a private PR drops: four windows legs and the four py3.12 legs.
TRIM = {
    "graph pytest (windows-latest)",
    "inventory node --test (windows-latest)",
    "orchestrator node --test (windows-latest)",
    "harness check.py (windows-latest)",
    "graph pytest (ubuntu-24.04, py3.12)",
    "_shared pytest (ubuntu-24.04, py3.12)",
    "install pytest (ubuntu-24.04, py3.12)",
    "harness check.py (ubuntu-24.04, py3.12)",
}
PROVENANCE = "PR provenance gate"
TRACKING_TITLE = "Nightly CI is red on master"

# ---------------------------------------------------------------------------
# The evaluator: exactly the expression shapes ci.yml uses, nothing else
# ---------------------------------------------------------------------------

_PRED = "(github.event_name == 'pull_request' && github.event.repository.private)"
_MATRIX_EXPR = re.compile(
    r"^\$\{\{\s*fromJSON\(" + re.escape(_PRED) + r" && '([^']*)' \|\| '([^']*)'\)\s*\}\}$"
)
_IF_PR_ONLY = "github.event_name == 'pull_request'"
_IF_PUSH_SKIP = "github.event_name != 'push' || !github.event.repository.private"
_NAME_SUFFIX = "${{ matrix.python != '3.11' && format(', py{0}', matrix.python) || '' }}"


class UnknownShape(Exception):
    """An expression this evaluator does not know. Never read as a pass or a skip."""


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(wf: dict) -> dict:
    # yaml.safe_load reads the bare key `on:` as the boolean True.
    return wf.get("on", wf.get(True))


def _matrix_value(v, pr_private: bool):
    if isinstance(v, list):
        return v
    m = _MATRIX_EXPR.match(str(v))
    if not m:
        raise UnknownShape(f"matrix expression: {v!r}")
    return yaml.safe_load(m.group(1) if pr_private else m.group(2))


def _job_runs(job: dict, event: str, private: bool) -> bool:
    cond = str(job.get("if", "")).strip()
    cond = re.sub(r"^\$\{\{\s*(.*?)\s*\}\}$", r"\1", cond)
    if cond == "":
        return True
    if cond == _IF_PR_ONLY:
        return event == "pull_request"
    if cond == _IF_PUSH_SKIP:
        return event != "push" or not private
    raise UnknownShape(f"job if: {cond!r}")


def check_names(wf: dict, event: str, private: bool) -> list[str]:
    """Every check name `wf` posts for `event` on a private or public repo, sorted."""
    out: list[str] = []
    pr_private = event == "pull_request" and private
    for job in wf["jobs"].values():
        if not _job_runs(job, event, private):
            continue
        mx = (job.get("strategy") or {}).get("matrix")
        combos: list[dict] = [{}]
        if mx:
            oss = _matrix_value(mx["os"], pr_private)
            pys = _matrix_value(mx.get("python", [None]), pr_private)
            combos = [{"os": o, "python": p} for o in oss for p in pys]
            for inc in _matrix_value(mx.get("include", []), pr_private):
                # An include whose os is already in the matrix would EXTEND that combo, not
                # add a leg; this evaluator does not model that, so it refuses it.
                if inc["os"] in oss:
                    raise UnknownShape(f"include extends an existing os leg: {inc!r}")
                combos.append(inc)
        for c in combos:
            n = job["name"].replace("${{ matrix.os }}", str(c.get("os")))
            py = c.get("python")
            n = n.replace(_NAME_SUFFIX, "" if py in ("3.11", None) else f", py{py}")
            if "${{" in n:
                raise UnknownShape(f"name expression: {job['name']!r}")
            out.append(n)
    return sorted(out)


@pytest.fixture(scope="module")
def ci() -> dict:
    return _load(CI_WORKFLOW)


@pytest.fixture(scope="module")
def nightly() -> dict:
    return _load(NIGHTLY_WORKFLOW)


# ---------------------------------------------------------------------------
# The evaluator refuses what it does not know
# ---------------------------------------------------------------------------


def test_evaluator_fails_on_an_unknown_if_shape():
    wf = {"jobs": {"j": {"name": "j", "if": "github.ref == 'refs/heads/master'"}}}
    with pytest.raises(UnknownShape):
        check_names(wf, "push", True)


def test_evaluator_fails_on_an_unknown_matrix_shape():
    wf = {"jobs": {"j": {"name": "j (${{ matrix.os }})",
                         "strategy": {"matrix": {"os": "${{ fromJSON(inputs.oses) }}"}}}}}
    with pytest.raises(UnknownShape):
        check_names(wf, "pull_request", True)


def test_evaluator_fails_on_an_unknown_name_expression():
    wf = {"jobs": {"j": {"name": "j (${{ matrix.arch }})",
                         "strategy": {"matrix": {"os": ["ubuntu-latest"]}}}}}
    with pytest.raises(UnknownShape):
        check_names(wf, "pull_request", False)


# ---------------------------------------------------------------------------
# ci.yml: what runs on each event
# ---------------------------------------------------------------------------


def test_ci_triggers_keep_every_trigger_and_add_workflow_call(ci):
    on = _on(ci)
    assert set(on) >= {"push", "pull_request", "workflow_dispatch", "workflow_call"}
    assert on["push"]["branches"] == ["master", "main"]
    # gh#331: `edited` re-scans a title or body changed after the last commit.
    assert "edited" in on["pull_request"]["types"]
    # A bare workflow_call: no inputs, no declared secrets (the nightly's call inherits them).
    assert not on["workflow_call"]


def test_ci_cancels_superseded_pr_runs_only(ci):
    cc = ci.get("concurrency")
    assert isinstance(cc, dict), cc
    assert "github.workflow" in cc["group"]
    assert "github.event.pull_request.number || github.run_id" in cc["group"]
    assert str(cc["cancel-in-progress"]).replace(" ", "") == "${{github.event_name=='pull_request'}}"


def test_public_pr_runs_the_full_matrix(ci):
    pub = check_names(ci, "pull_request", False)
    assert len(pub) == 20 and len(set(pub)) == 20, pub
    assert TRIM <= set(pub)
    assert PROVENANCE in pub


def test_private_pr_is_public_pr_minus_the_trim_set(ci):
    pub = set(check_names(ci, "pull_request", False))
    priv = check_names(ci, "pull_request", True)
    assert set(priv) == pub - TRIM, sorted(set(priv) ^ (pub - TRIM))
    assert len(priv) == 12, priv


@pytest.mark.parametrize(
    ("event", "private"),
    [("schedule", True), ("schedule", False), ("workflow_dispatch", True),
     ("workflow_dispatch", False), ("push", False)],
)
def test_non_pr_runs_everything_but_the_provenance_gate(ci, event, private):
    pub_pr = set(check_names(ci, "pull_request", False))
    assert set(check_names(ci, event, private)) == pub_pr - {PROVENANCE}


def test_private_push_runs_no_job(ci):
    assert check_names(ci, "push", True) == []


def test_every_pip_cache_dependency_path_exists(ci):
    seen = 0
    for key, job in ci["jobs"].items():
        for step in job.get("steps", []):
            w = step.get("with") or {}
            if str(step.get("uses", "")).startswith("actions/setup-python") and w.get("cache") == "pip":
                seen += 1
                dep = w.get("cache-dependency-path")
                assert dep and (REPO_ROOT / dep).is_file(), f"{key}: {dep!r} does not exist"
    for key in ("graph-tests", "shared-tests", "install-tests", "harness-self-test"):
        sp = [s for s in ci["jobs"][key]["steps"]
              if str(s.get("uses", "")).startswith("actions/setup-python")]
        assert sp and (sp[0].get("with") or {}).get("cache") == "pip", f"{key} lacks cache: pip"
    assert seen == 4, seen


# ---------------------------------------------------------------------------
# nightly.yml: shape
# ---------------------------------------------------------------------------


def _caller(nightly: dict) -> tuple[str, dict]:
    callers = [(k, j) for k, j in nightly["jobs"].items()
               if j.get("uses") == "./.github/workflows/ci.yml"]
    assert len(callers) == 1, [k for k, _ in callers]
    return callers[0]


def test_nightly_triggers(nightly):
    on = _on(nightly)
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert on["schedule"] == [{"cron": "41 7 * * *"}]


def test_nightly_has_no_workflow_level_concurrency_and_no_default_permissions(nightly):
    # A group that could equal ci.yml's would deadlock the call.
    assert "concurrency" not in nightly
    assert nightly.get("permissions") == {}


def test_nightly_calls_ci_exactly_once_and_fails_open(nightly):
    _, job = _caller(nightly)
    assert job["needs"] == "gate"
    cond = str(job.get("if"))
    assert "!cancelled()" in cond and "needs.gate.outputs.run != 'false'" in cond, cond


def test_issue_write_is_on_the_report_job_only(nightly):
    writers = {k: j.get("permissions") for k, j in nightly["jobs"].items()
               if "write" in str(j.get("permissions"))}
    assert writers == {"report": {"issues": "write"}}, writers
    cond = str(nightly["jobs"]["report"]["if"])
    assert "always()" in cond and "needs.ci.result == 'failure'" in cond, cond
    assert nightly["jobs"]["gate"]["permissions"] == {"actions": "read"}


def test_caller_permissions_cover_the_nested_jobs(nightly, ci):
    _, job = _caller(nightly)
    granted = job.get("permissions") or {}
    # checkout needs contents: read; every job that declares its own permissions must get them.
    needed = {"contents": "read"}
    for nested in ci["jobs"].values():
        for scope, level in (nested.get("permissions") or {}).items():
            needed[scope] = level
    rank = {"none": 0, "read": 1, "write": 2}
    for scope, level in needed.items():
        assert rank[granted.get(scope, "none")] >= rank[level], (scope, level, granted)


def test_nightly_run_blocks_read_only_env(nightly):
    for key in ("gate", "report"):
        steps = nightly["jobs"][key]["steps"]
        assert len(steps) == 1, f"{key} grew a second step; this file drives step 0 only"
        assert "${{" not in steps[0]["run"], f"{key}'s run: block interpolates an expression"


# ---------------------------------------------------------------------------
# nightly.yml: the gate and report scripts under bash with a gh shim
# ---------------------------------------------------------------------------


def _bash() -> str:
    """A bash that can run the scripts: derived from `git` first, bare PATH last.

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


# Each call is recorded as its argv joined by \x1f, calls separated by \x1e, so a multi-line
# --body survives the round trip.
_SHIM = (
    "#!/usr/bin/env bash\n"
    'for a in "$@"; do printf \'%s\\037\' "$a" >> "$SHIM_LOG"; done\n'
    "printf '\\036' >> \"$SHIM_LOG\"\n"
    'case "$1 $2" in\n'
    '  "run list") printf \'%s\' "${SHIM_RUN_LIST_OUT:-}"; exit "${SHIM_RUN_LIST_RC:-0}" ;;\n'
    '  "issue list") printf \'%s\' "${SHIM_ISSUE_LIST_OUT:-}"; exit "${SHIM_ISSUE_LIST_RC:-0}" ;;\n'
    '  "issue comment"|"issue create") exit "${SHIM_WRITE_RC:-0}" ;;\n'
    '  *) echo "gh shim: unexpected call: $*" >&2; exit 97 ;;\n'
    "esac\n"
)

SHA_HEAD = "a" * 40
SHA_OTHER = "b" * 40


def _run_step(tmp_path: Path, nightly: dict, job: str, env_extra: dict[str, str]):
    script = tmp_path / f"{job}.sh"
    script.write_text(nightly["jobs"][job]["steps"][0]["run"], encoding="utf-8", newline="\n")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "gh"
    shim.write_text(_SHIM, encoding="utf-8", newline="\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "gh.log"
    log.write_text("", encoding="utf-8")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    env.update(
        {
            "GH_TOKEN": "shim",
            "GITHUB_REPOSITORY": "someone/somerepo",
            "GITHUB_SHA": SHA_HEAD,
            "GITHUB_OUTPUT": out.as_posix(),
            "GITHUB_SERVER_URL": "https://github.example",
            "GITHUB_RUN_ID": "4242",
            "SHIM_LOG": log.as_posix(),
        }
    )
    env.update(env_extra)
    proc = subprocess.run(
        [_bash(), str(script)], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    calls = [c.split("\x1f")[:-1] for c in log.read_text(encoding="utf-8").split("\x1e") if c]
    return proc, out.read_text(encoding="utf-8"), calls


def _gate(tmp_path, nightly, event, **env):
    return _run_step(tmp_path, nightly, "gate", {"GITHUB_EVENT_NAME": event, **env})


def test_gate_dispatch_always_runs_without_a_lookup(tmp_path, nightly):
    proc, out, calls = _gate(tmp_path, nightly, "workflow_dispatch",
                             SHIM_RUN_LIST_OUT=f'[{{"headSha":"{SHA_HEAD}"}}]')
    assert proc.returncode == 0, proc.stderr
    assert out.strip() == "run=true"
    assert calls == []


def test_gate_schedule_on_the_last_green_sha_skips(tmp_path, nightly):
    proc, out, calls = _gate(tmp_path, nightly, "schedule",
                             SHIM_RUN_LIST_OUT=f'[{{"headSha":"{SHA_HEAD}"}}]')
    assert proc.returncode == 0, proc.stderr
    assert out.strip() == "run=false"
    assert "::notice::" in proc.stdout and SHA_HEAD in proc.stdout
    assert calls == [["run", "list", "-R", "someone/somerepo", "--workflow", "nightly.yml",
                      "--branch", "master", "--status", "success", "--limit", "1",
                      "--json", "headSha"]]


def test_gate_schedule_on_a_moved_master_runs(tmp_path, nightly):
    proc, out, _ = _gate(tmp_path, nightly, "schedule",
                         SHIM_RUN_LIST_OUT=f'[\n  {{\n    "headSha": "{SHA_OTHER}"\n  }}\n]\n')
    assert proc.returncode == 0, proc.stderr
    assert out.strip() == "run=true"
    assert "::warning::" not in proc.stdout


@pytest.mark.parametrize("lookup", ["", "[]", "not json"])
def test_gate_schedule_with_an_empty_lookup_fails_open(tmp_path, nightly, lookup):
    proc, out, _ = _gate(tmp_path, nightly, "schedule", SHIM_RUN_LIST_OUT=lookup)
    assert proc.returncode == 0, proc.stderr
    assert out.strip() == "run=true"
    assert "::warning::" in proc.stdout


def test_gate_schedule_with_a_failed_lookup_fails_open_and_warns(tmp_path, nightly):
    proc, out, _ = _gate(tmp_path, nightly, "schedule",
                         SHIM_RUN_LIST_OUT=f'[{{"headSha":"{SHA_HEAD}"}}]', SHIM_RUN_LIST_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert out.strip() == "run=true"
    assert "::warning::" in proc.stdout and "exited 1" in proc.stdout


def _report(tmp_path, nightly, **env):
    return _run_step(tmp_path, nightly, "report", env)


def _writes(calls):
    return [c for c in calls if c[:2] in (["issue", "comment"], ["issue", "create"])]


def _body_of(call):
    return call[call.index("--body") + 1]


def test_report_comments_on_the_open_tracking_issue(tmp_path, nightly):
    listed = (f'[{{"number":3,"title":"{TRACKING_TITLE} (old)"}},'
              f'{{"number":17,"title":"{TRACKING_TITLE}"}}]')
    proc, _, calls = _report(tmp_path, nightly, SHIM_ISSUE_LIST_OUT=listed)
    assert proc.returncode == 0, proc.stderr
    writes = _writes(calls)
    assert len(writes) == 1 and writes[0][:3] == ["issue", "comment", "17"], writes
    body = _body_of(writes[0])
    assert "https://github.example/someone/somerepo/actions/runs/4242" in body
    assert SHA_HEAD in body


def test_report_creates_the_issue_when_none_is_open(tmp_path, nightly):
    proc, _, calls = _report(tmp_path, nightly, SHIM_ISSUE_LIST_OUT="[]")
    assert proc.returncode == 0, proc.stderr
    writes = _writes(calls)
    assert len(writes) == 1 and writes[0][:2] == ["issue", "create"], writes
    assert writes[0][writes[0].index("--title") + 1] == TRACKING_TITLE
    assert "--label" not in writes[0]
    body = _body_of(writes[0])
    assert "https://github.example/someone/somerepo/actions/runs/4242" in body
    assert SHA_HEAD in body


@pytest.mark.parametrize(
    "title",
    [f"Re: {TRACKING_TITLE}", f"{TRACKING_TITLE} again", "Nightly CI is red"],
)
def test_report_does_not_treat_a_partial_title_match_as_the_tracking_issue(tmp_path, nightly, title):
    proc, _, calls = _report(tmp_path, nightly,
                             SHIM_ISSUE_LIST_OUT=f'[{{"number":9,"title":"{title}"}}]')
    assert proc.returncode == 0, proc.stderr
    writes = _writes(calls)
    assert len(writes) == 1 and writes[0][:2] == ["issue", "create"], writes


@pytest.mark.parametrize(
    "env",
    [{"SHIM_ISSUE_LIST_RC": "1"}, {"SHIM_ISSUE_LIST_OUT": "[]", "SHIM_WRITE_RC": "1"}],
    ids=["list-fails", "create-fails"],
)
def test_report_fails_loudly_on_a_gh_error(tmp_path, nightly, env):
    proc, _, _ = _report(tmp_path, nightly, **env)
    assert proc.returncode != 0


# ---------------------------------------------------------------------------
# ci.yml: the sanitize gate's extra pattern (gh#821)
# ---------------------------------------------------------------------------
# The shipped pattern carries generic shapes only; install-specific alternatives arrive from
# the repository secret SANITIZE_EXTRA_PATTERN as EXTRA_PATTERN. These drive the step's own
# `run:` block, parsed out of ci.yml, under bash with the gh shim first on PATH - it must make
# no gh call at all. Every planted term is a placeholder that names nothing.

SANITIZE_JOB = "sanitize-gate"
SANITIZE_STEP = "Grep for personal/product literals"
EXTRA_TERM = "acmeproduct"
EXTRA_HOST = "zephyrhost"
PLACEHOLDER_EXTRA = f"(?<!super){EXTRA_TERM}|{EXTRA_HOST}\\b"
EXTRA_APPLIED = (
    "sanitize gate: extra pattern from repository secret SANITIZE_EXTRA_PATTERN applied to "
    "every scan"
)
EXTRA_NOTICE = (
    "::notice::sanitize gate: no extra pattern configured (repository secret "
    "SANITIZE_EXTRA_PATTERN is empty) - scanning the generic shapes only"
)
CLEAN = {
    "tree": "sanitize gate clean: no personal/product literals in first-party files",
    "title": "sanitize gate clean: no personal/product literals in the PR title",
    "body": "sanitize gate clean: no personal/product literals in the PR body",
}
FOUND = {
    "tree": "literals found in tracked files",
    "title": "literals in the PULL REQUEST TITLE",
    "body": "literals in the PULL REQUEST BODY",
}
DID_NOT_RUN = "the scan did not run. Failing closed."


def _sanitize_step(ci: dict) -> dict:
    steps = [s for s in ci["jobs"][SANITIZE_JOB]["steps"] if s.get("name") == SANITIZE_STEP]
    assert len(steps) == 1, f"expected one {SANITIZE_STEP!r} step, found {len(steps)}"
    return steps[0]


@pytest.fixture(scope="module")
def pcre_bash() -> str:
    """A bash whose grep has a working -P under C.UTF-8, as the step needs; else SKIP."""
    b = _bash()
    probe = subprocess.run(
        [b, "-c", "export LC_ALL=C.UTF-8; printf 'abc' | grep -qP -e '(?<!x)b'"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"bash has no working `grep -P` under C.UTF-8 (rc={probe.returncode})")
    return b


def _run_sanitize(
    tmp_path: Path,
    ci: dict,
    bash: str,
    *,
    extra: str | None,
    tree: dict[str, str] | None = None,
    title: str = "a clean title",
    body: str = "a clean body",
    event: str = "pull_request",
):
    """The step's `run:` block, against a throwaway tree. `extra=None` leaves EXTRA_PATTERN
    unset, as on a contributor's machine; "" is what GitHub sends when the secret is empty."""
    # Outside the scanned directory: the script carries the pattern, and the extra pattern
    # would otherwise find itself in the tree half.
    script = tmp_path / "sanitize.sh"
    script.write_text(_sanitize_step(ci)["run"], encoding="utf-8", newline="\n")
    scan_dir = tmp_path / "tree"
    scan_dir.mkdir(exist_ok=True)
    for name, text in (tree or {"README.md": "nothing to see here\n"}).items():
        (scan_dir / name).write_text(text, encoding="utf-8", newline="\n")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "gh"
    shim.write_text(_SHIM, encoding="utf-8", newline="\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "gh.log"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    env.update({"EVENT_NAME": event, "PR_TITLE": title, "PR_BODY": body,
                "SHIM_LOG": log.as_posix()})
    env.pop("EXTRA_PATTERN", None)
    if extra is not None:
        env["EXTRA_PATTERN"] = extra
    proc = subprocess.run(
        [bash, str(script)], cwd=scan_dir, env=env, capture_output=True, text=True,
        timeout=60, check=False,
    )
    calls = [c for c in log.read_text(encoding="utf-8").split("\x1e") if c]
    assert calls == [], f"the sanitize step called gh: {calls}"
    return proc


def test_sanitize_step_reads_the_extra_pattern_from_the_repository_secret(ci, nightly):
    """Through env, from `secrets`, never spliced into the script (gh#866): GitHub lists a
    step's env at the top of its log and masks a secret there, where a `vars` value prints in
    clear. The `_shared pytest` step reads the same secret. The nightly's call passes
    `secrets: inherit`, because a called workflow sees none of the caller's secrets unless
    they are passed, and an inherited secret needs no workflow_call declaration."""
    step = _sanitize_step(ci)
    assert step["env"]["EXTRA_PATTERN"] == "${{ secrets.SANITIZE_EXTRA_PATTERN }}"
    assert "${{" not in step["run"]
    readers = [s["env"]["SANITIZE_EXTRA_PATTERN"] for job in ci["jobs"].values()
               for s in job.get("steps", []) if "SANITIZE_EXTRA_PATTERN" in (s.get("env") or {})]
    assert readers == ["${{ secrets.SANITIZE_EXTRA_PATTERN }}"], readers
    assert not re.search(r"\$\{\{\s*vars\.SANITIZE_EXTRA_PATTERN",
                         CI_WORKFLOW.read_text(encoding="utf-8")), "read from `vars`, unmasked"
    _, call = _caller(nightly)
    assert "with" not in call, call
    assert call.get("secrets") == "inherit", call
    assert not _on(ci)["workflow_call"], "an inherited secret needs no workflow_call declaration"


def test_sanitize_step_applies_the_extra_pattern_to_tree_title_and_body(tmp_path, ci, pcre_bash):
    proc = _run_sanitize(
        tmp_path, ci, pcre_bash, extra=PLACEHOLDER_EXTRA,
        tree={"notes.md": f"see {EXTRA_HOST} for details\nand super{EXTRA_TERM} is exempt\n"},
        title=f"fix {EXTRA_TERM.upper()}",
        body=f"line one\nmentions {EXTRA_HOST}\n",
    )
    out = proc.stdout
    assert proc.returncode == 1, out + proc.stderr
    assert out.count(EXTRA_APPLIED) == 1 and EXTRA_NOTICE not in out
    for half in ("tree", "title", "body"):
        assert FOUND[half] in out, half
        assert CLEAN[half] not in out, half
    assert "notes.md:1:" in out and "notes.md:2:" not in out, "the exemption did not hold"
    assert PLACEHOLDER_EXTRA not in out + proc.stderr, "the step echoed the extra pattern"


@pytest.mark.parametrize("extra", ["", None], ids=["empty-secret", "unset"])
def test_sanitize_step_without_an_extra_pattern_says_so_and_scans_generic_shapes(
    tmp_path, ci, pcre_bash, extra
):
    planted = {"notes.md": f"see {EXTRA_HOST}\n"}
    proc = _run_sanitize(tmp_path, ci, pcre_bash, extra=extra, tree=planted,
                         title=f"fix {EXTRA_TERM}", body=EXTRA_HOST)
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert out.count(EXTRA_NOTICE) == 1 and EXTRA_APPLIED not in out
    for half in ("tree", "title", "body"):
        assert CLEAN[half] in out, half
    # Control: the generic shapes still run with no extra pattern at all.
    home = "/ho" + "me/" + "qzacct7/x"
    proc = _run_sanitize(tmp_path, ci, pcre_bash, extra=extra, body=home)
    assert proc.returncode == 1 and FOUND["body"] in proc.stdout, proc.stdout


def test_sanitize_step_fails_closed_on_an_extra_pattern_grep_cannot_parse(
    tmp_path, ci, pcre_bash
):
    """Appended to the one pattern, a broken extra pattern breaks every scan - each fails closed
    with its own message, rather than the gate falling back to the generic shapes."""
    proc = _run_sanitize(tmp_path, ci, pcre_bash, extra=f"{EXTRA_TERM}(")
    out = proc.stdout
    assert proc.returncode == 1, out + proc.stderr
    for half, label in (("tree", "tree"), ("title", "PR title"), ("body", "PR body")):
        assert f"{label} scan: grep exited 2 - {DID_NOT_RUN}" in out, half
        assert CLEAN[half] not in out, half


def _top_level_alternatives(pattern: str) -> list[str]:
    """`pattern` split on the `|` that sit outside every group and character class."""
    out: list[str] = []
    cur: list[str] = []
    depth, in_class, escaped = 0, False, False
    for ch in pattern:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


#: The shapes ci.yml may ship, by their opening text: the mangled session-directory name and
#: the absolute home-directory paths. Neither names anybody.
GENERIC_SHAPES = ("C--Users-", "(?:[A-Za-z]:[\\\\/]Users[\\\\/]")


def _non_generic(pattern: str) -> list[int]:
    alts = _top_level_alternatives(pattern)
    return [i for i, alt in enumerate(alts) if not alt.startswith(GENERIC_SHAPES)]


def test_ci_yml_ships_only_the_generic_shapes():
    """gh#821: ci.yml ships in every public cut, so an install-specific alternative written
    there is published by the gate meant to keep it out. Pinned structurally - every top-level
    alternative of the shipped pattern is one of the generic shapes - so this file never has
    to name what must not be there. A new GENERIC shape belongs in GENERIC_SHAPES; anything
    install-specific belongs in the extra pattern (the local file and the repository
    secret), never in ci.yml."""
    from agentflow import pillar_residue

    shipped = pillar_residue.sanitize_pattern(CI_WORKFLOW)
    assert len(_top_level_alternatives(shipped)) == len(GENERIC_SHAPES)
    assert _non_generic(shipped) == [], (
        f"ci.yml's sanitize pattern has non-generic top-level alternative(s) at index "
        f"{_non_generic(shipped)}; install-specific terms go in the extra pattern"
    )
    line = pillar_residue.sanitize_pattern_line(CI_WORKFLOW)
    assert "''" not in line, "a shell '' split in the shipped line is how a literal term hides"

    # Control, against a placeholder: the same check catches an alternative slipped in front.
    planted = line.replace("pattern='", f"pattern='{EXTRA_TERM[:4]}''{EXTRA_TERM[4:]}|", 1)
    assert planted != line
    decoded = planted.strip()[len("pattern='"):-1].replace("''", "")
    assert _non_generic(decoded) == [0]


def test_ci_yml_carries_none_of_this_runs_extra_alternatives():
    """The real check, where this run has the private alternatives (CI's `_shared pytest` step
    passes SANITIZE_EXTRA_PATTERN; a public copy has none and SKIPS). No alternative may match a
    line of ci.yml, and none may appear in its source text, with shell and Python joins
    collapsed so a split spelling is caught too. Indices only in the messages, through
    pytest.fail rather than assert: a failing assert's rewritten explanation prints its
    operands, and here an operand is the alternative."""
    extra = os.environ.get("SANITIZE_EXTRA_PATTERN", "").strip()
    if not extra:
        pytest.skip("SANITIZE_EXTRA_PATTERN is empty: no private alternatives to check for")
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    joined = re.sub(r"""['"][ \t]*\+?[ \t]*['"]""", "", text)
    for i, alt in enumerate(_top_level_alternatives(extra)):
        rx = re.compile(alt, re.IGNORECASE)
        hits = [n for n, ln in enumerate(joined.splitlines(), 1) if rx.search(ln)]
        if hits:
            pytest.fail(f"extra alternative #{i} matches ci.yml (joins collapsed) at {hits}")
        if alt.lower() in joined.lower():
            pytest.fail(f"ci.yml spells extra alternative #{i}")
