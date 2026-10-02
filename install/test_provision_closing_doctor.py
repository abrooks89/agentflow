#!/usr/bin/env python3
"""Provision's closing doctor check examines every configured project (gh#693).

With two or more `projects/*.md` and no AGENTFLOW_PROJECT, doctor's resolver selects no
project. The closing check used to call `diagnose()` bare, so it examined no checkout,
registry or glob, emitted one INFO line, and still printed "resolved surface healthy".

These drive `main()` past its steps to the closing check with the REAL doctor, against a
sandboxed config home (`AGENTFLOW_CONFIG` and `--config-home` both point at tmp_path, since
doctor reads the former and provision iterates the latter).
"""

from __future__ import annotations

import io
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402

NL = chr(10)
_SECTIONS = ("## Agents", "## Queue", "## Questions", "## Blockers", "## Directives")


def _fwd(p: Path) -> str:
    return str(p).replace(chr(92), "/")


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# config{NL}{NL}```yaml{NL}{textwrap.dedent(body).strip()}{NL}```{NL}",
        encoding="utf-8",
    )


def _sandbox(tmp_path: Path, monkeypatch, projects: dict) -> Path:
    """A config home holding one profile per *projects* entry; value False = no registry."""
    home = tmp_path / "config"
    data_root = tmp_path / "data"
    data_root.mkdir()
    d = _fwd(tmp_path)
    _write(home / "environment.md", f"""
        schema_version: 1
        person:
          name: Sample User
        data_root: {_fwd(data_root)}
        git_host:
          kind: local
        notify:
          kind: none
        lock_dir: {d}
        shell: posix
        suite_root: {d}
        worktree_root: {d}
        vault: null
    """)
    for name, with_registry in projects.items():
        base = data_root / "projects" / name
        for sub in ("design/specs", "design/plans", "status"):
            (base / sub).mkdir(parents=True)
        if with_registry:
            (base / "status" / "registry.md").write_text(NL.join(_SECTIONS), encoding="utf-8")
        checkout = tmp_path / "repos" / name
        (checkout / ".git").mkdir(parents=True)
        _write(home / "projects" / f"{name}.md", f"""
            schema_version: 1
            name: {name}
            canonical_repo:
              host_ref: owner/{name}
              local_checkout: {_fwd(checkout)}
            verification:
              strategy: test-suite
        """)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    return home


def _closing_check(tmp_path: Path, home: Path) -> str:
    steps = [provision.Step(n=5, title="step 5", run=lambda _o: None)]
    capture = io.StringIO()
    with mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_print_verification_readiness", lambda _r: None), \
         mock.patch.object(provision, "_verification_readiness", lambda _o: []), \
         mock.patch.object(sys, "stdout", capture):
        rc = provision.main(["--resume-from", "5",
                             "--suite-root", str(tmp_path), "--config-home", str(home),
                             "--settings-file", str(tmp_path / ".claude" / "settings.local.json"),
                             "--claude-root", str(tmp_path)])
    assert rc == 0, "the closing check is advisory and never fails the install"
    out = capture.getvalue()
    return out[out.index("Resolved-surface check"):]


@pytest.fixture(autouse=True)
def _real_doctor_importable():
    """The real `agentflow.doctor`, not a stub - the point is what it actually examines."""
    pytest.importorskip("agentflow.doctor")


def test_two_profiles_are_both_diagnosed(tmp_path, monkeypatch):
    home = _sandbox(tmp_path, monkeypatch, {"toy": True, "second": True})
    report = _closing_check(tmp_path, home)
    assert "no project selected" not in report
    assert "project:toy" in report and "project:second" in report
    assert report.count("registry") >= 2, report
    assert "healthy for all 2 configured project(s)" in report, report


def test_a_broken_second_profile_turns_the_verdict_unhealthy(tmp_path, monkeypatch):
    home = _sandbox(tmp_path, monkeypatch, {"toy": True, "second": False})
    report = _closing_check(tmp_path, home)
    last = report.rstrip().splitlines()[-1]
    assert "NOT healthy" in last, report
    assert "1 of 2 configured project(s) failing: second (checked: second, toy)" in last, last
    assert "healthy for all" not in report


def test_zero_profiles_print_no_healthy_verdict_for_an_unexamined_project(tmp_path, monkeypatch):
    home = _sandbox(tmp_path, monkeypatch, {})
    report = _closing_check(tmp_path, home)
    assert "resolved surface healthy" not in report, report
    assert "no project checked" in report.lower(), report


def test_one_project_raising_does_not_hide_the_rest(tmp_path, monkeypatch):
    """`diagnose` is documented never to raise. If it does for one project, that project is
    reported failing with the error, the later projects are still checked, and the overall
    verdict still prints."""
    home = tmp_path / "config"
    (home / "projects").mkdir(parents=True)
    for name in ("a", "b", "c"):
        (home / "projects" / f"{name}.md").write_text("x", encoding="utf-8")
    checked = []

    def diagnose(name=None):
        checked.append(name)
        if name == "b":
            raise RuntimeError("boom in b")
        return []

    doctor = mock.Mock(diagnose=diagnose, render=lambda _f: "(stubbed)\n")
    with mock.patch.dict(sys.modules, {"agentflow.doctor": doctor}):
        report = _closing_check(tmp_path, home)
    assert checked == ["a", "b", "c"]
    assert "RuntimeError: boom in b" in report
    assert "(doctor unavailable" not in report
    last = report.rstrip().splitlines()[-1]
    assert "1 of 3 configured project(s) failing: b (checked: a, b, c)" in last, last
