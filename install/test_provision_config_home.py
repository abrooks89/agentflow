#!/usr/bin/env python3
"""Provision hands its config home to readers and children explicitly; it never pins it.

agentflow.config used to read the home only from $AGENTFLOW_CONFIG, so provision set the
variable for the length of a call and restored it after - in main() and again in
_load_config_or_fail. The variable is process-global: two overlapping in-process calls each
saved the other's value as "prior", and the first to finish restored a home the second was
still reading from. These pin the replacement - the home passed per call to every in-process
reader, and in an explicit env= to every child that resolves it - and that os.environ is
left untouched throughout.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
_WAIT = 10


def _home(tmp_path: Path, name: str, project: str) -> Path:
    home = tmp_path / name
    (home / "projects").mkdir(parents=True)
    (home / "projects" / f"{project}.md").write_text("x", encoding="utf-8")
    return home


def _opts(tmp_path: Path, home: Path, suite_root: Path = REPO) -> provision.Options:
    return provision.Options(
        dry_run=False, suite_root=suite_root, config_home=home,
        settings_file=tmp_path / f"{home.name}-settings.json", autodoc=False,
        autodoc_reason="not under test", claude_root=tmp_path / f"{home.name}-claude",
    )


@pytest.fixture
def outer(tmp_path, monkeypatch) -> str:
    """A shell-level $AGENTFLOW_CONFIG naming neither run's home."""
    value = str(tmp_path / "outer-home")
    monkeypatch.setenv("AGENTFLOW_CONFIG", value)
    return value


def test_overlapping_config_reads_each_see_their_OWN_home(tmp_path, monkeypatch, outer):
    """Two `_load_config_or_fail` calls on two threads, interleaved non-LIFO: A starts, B
    starts, A finishes, B finishes. With a set-and-restore of the variable, B saved A's home
    as its "prior", A read its projects from B's home, and A's restore then left B reading
    the shell's. Each read is snapshotted to prove the variable never moves."""
    from agentflow import config

    home_a = _home(tmp_path, "home-a", "pa")
    home_b = _home(tmp_path, "home-b", "pb")
    a_in, b_in, a_done = threading.Event(), threading.Event(), threading.Event()
    seen_env, timeouts = [], []
    first = set()
    lock = threading.Lock()

    def read(file):
        who = threading.current_thread().name
        with lock:
            seen_env.append(os.environ.get("AGENTFLOW_CONFIG"))
            is_first = who not in first
            first.add(who)
        if is_first and who == "A":
            a_in.set()
            if not b_in.wait(_WAIT):
                timeouts.append("A waiting for B")
        elif is_first and who == "B":
            b_in.set()
            if not a_done.wait(_WAIT):
                timeouts.append("B waiting for A")
        return {}

    monkeypatch.setattr(config, "_load_first_yaml_block", read)
    monkeypatch.setattr(config, "_build_environment", lambda _data, file: file)
    monkeypatch.setattr(config, "_build_project", lambda _data, file, _name: file)

    results, errors = {}, []

    def drive(name, opts, done=None):
        try:
            results[name] = provision._load_config_or_fail(opts)
        except Exception as exc:  # surfaced below; a thread's exception is otherwise lost
            errors.append((name, exc))
        finally:
            if done is not None:
                done.set()

    ta = threading.Thread(target=drive, name="A", args=("A", _opts(tmp_path, home_a), a_done))
    tb = threading.Thread(target=drive, name="B", args=("B", _opts(tmp_path, home_b)))
    ta.start()
    assert a_in.wait(_WAIT), "run A never reached its first config read"
    tb.start()
    ta.join(_WAIT * 3)
    tb.join(_WAIT * 3)

    assert not errors, errors
    assert not timeouts, timeouts
    a, b = home_a.resolve(), home_b.resolve()
    assert results["A"] == (a / "environment.md", {"pa": a / "projects" / "pa.md"})
    assert results["B"] == (b / "environment.md", {"pb": b / "projects" / "pb.md"})
    assert seen_env and set(seen_env) == {outer}, "provision moved $AGENTFLOW_CONFIG mid-read"
    assert os.environ["AGENTFLOW_CONFIG"] == outer


@pytest.fixture
def sealed_doctor():
    """main()'s closing doctor, stubbed: it is not under test here, only the steps' view."""
    with mock.patch.dict(
        sys.modules,
        {"agentflow.doctor": mock.Mock(diagnose=lambda *_a, **_k: [],
                                       render=lambda _d: "(stubbed)",
                                       examined_a_project=lambda _f: False,
                                       unexamined_verdict=lambda r, _dir: r)},
    ):
        yield


def test_overlapping_mains_never_touch_the_variable(tmp_path, outer, sealed_doctor):
    """Both runs inside a step at once: each step sees its own run's home and the shell's
    $AGENTFLOW_CONFIG, not whichever run wrote it last."""
    barrier = threading.Barrier(2, timeout=_WAIT * 3)
    seen, lock = {}, threading.Lock()

    def step(o):
        barrier.wait()
        with lock:
            seen[o.config_home.name] = (os.environ.get("AGENTFLOW_CONFIG"),
                                        o.config_home.expanduser().resolve())
        barrier.wait()

    steps = [provision.Step(n=10, title="step 10", run=step)]

    def argv_for(name):
        return ["--suite-root", str(tmp_path), "--config-home", str(tmp_path / name),
                "--settings-file", str(tmp_path / f"{name}.json"),
                "--claude-root", str(tmp_path / f"{name}-cl")]

    results, errors = {}, []

    def drive(name):
        try:
            results[name] = provision.main(argv_for(name))
        except Exception as exc:  # surfaced below; a thread's exception is otherwise lost
            errors.append(exc)

    with mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_print_verification_readiness", lambda _r: None), \
         mock.patch.object(provision, "_verification_readiness", lambda _o: []):
        threads = [threading.Thread(target=drive, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(_WAIT * 6)

    assert not errors, errors
    assert results == {"a": 0, "b": 0}
    assert seen == {n: (outer, (tmp_path / n).resolve()) for n in ("a", "b")}
    assert os.environ["AGENTFLOW_CONFIG"] == outer


def test_bootstrap_child_gets_the_home_in_its_OWN_env(tmp_path, monkeypatch, outer):
    """Bootstrap runs in a child; the home crosses the process boundary in env=, which is a
    copy - the parent's os.environ is not the carrier."""
    monkeypatch.setenv("AF_PROBE_INHERITED", "kept")
    home = tmp_path / "cfg"
    opts = _opts(tmp_path, home, suite_root=tmp_path)
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(kwargs.get("env"))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch.object(provision.shutil, "which", lambda c: c), \
         mock.patch.object(provision.subprocess, "run", fake_run):
        provision._do_bootstrap(opts)

    assert len(captured) == 1
    env = captured[0]
    assert env["AGENTFLOW_CONFIG"] == str(home.resolve())
    assert env["CLAUDE_SETTINGS_FILE"] == str(opts.settings_file)
    assert env["AF_PROBE_INHERITED"] == "kept"
    assert env is not os.environ
    assert os.environ["AGENTFLOW_CONFIG"] == outer


def test_venv_handover_child_gets_the_home_in_its_OWN_env(tmp_path, monkeypatch, outer):
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    home = tmp_path / "cfg"
    opts = _opts(tmp_path, home, suite_root=tmp_path)
    venv_py = tmp_path / ".venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    captured = []

    def fake_call(argv, **kwargs):
        captured.append(kwargs.get("env"))
        return 0

    with mock.patch.object(provision, "_venv_python", lambda _o: venv_py), \
         mock.patch.object(provision.subprocess, "call", fake_call):
        with pytest.raises(provision._HandOff):
            provision._reexec_into_venv(opts, resume_from=5)

    assert len(captured) == 1
    assert captured[0]["AGENTFLOW_CONFIG"] == str(home.resolve())
    assert captured[0][provision._REEXEC_MARKER] == "1"
    assert os.environ["AGENTFLOW_CONFIG"] == outer
    assert provision._REEXEC_MARKER not in os.environ


# --------------------------------------------------------------------------- #
# The real readers, against two real homes: one in the variable, one in opts
# --------------------------------------------------------------------------- #
def _real_home(root: Path, name: str, strategy: str) -> Path:
    """A loadable config home: environment.md (data root under *root*) + one `toy` profile."""
    home = root / name
    data = root / f"{name}-data"
    data.mkdir(parents=True)
    d = root.as_posix()
    fence = "```"
    env = (f"schema_version: 1\nperson:\n  name: Sample User\ndata_root: {data.as_posix()}\n"
           "git_host:\n  kind: local\nnotify:\n  kind: none\n"
           f"lock_dir: {d}\nshell: posix\nsuite_root: {d}\nworktree_root: {d}\n")
    proj = (f"schema_version: 1\nname: toy\ncanonical_repo:\n  host_ref: owner/repo\n"
            f"  local_checkout: {d}\nverification:\n  strategy: {strategy}\n")
    (home / "projects").mkdir(parents=True)
    (home / "environment.md").write_text(f"# env\n\n{fence}yaml\n{env}{fence}\n", encoding="utf-8")
    (home / "projects" / "toy.md").write_text(f"# toy\n\n{fence}yaml\n{proj}{fence}\n",
                                              encoding="utf-8")
    return home


@pytest.fixture
def two_real_homes(tmp_path, monkeypatch):
    """(inner, outer): the variable names OUTER, opts names INNER; each is fully loadable, so a
    reader that ignores opts reads the wrong home successfully instead of failing loudly."""
    outer = _real_home(tmp_path, "outer", "test-suite")
    inner = _real_home(tmp_path, "inner", "smoke-cli")
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(outer))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    return inner, outer


def test_the_data_root_scaffold_reads_opts_home_not_the_variable(tmp_path, two_real_homes):
    inner, outer = two_real_homes
    provision._do_scaffold_data_root(_opts(tmp_path, inner))
    assert (tmp_path / "inner-data" / "projects" / "toy" / "status" / "registry.md").is_file()
    assert not (tmp_path / "outer-data" / "projects").exists(), "scaffolded the variable's home"
    assert os.environ["AGENTFLOW_CONFIG"] == str(outer)


def test_verification_readiness_reads_opts_home_not_the_variable(tmp_path, two_real_homes):
    inner, outer = two_real_homes
    report = provision._verification_readiness(_opts(tmp_path, inner))
    assert [(name, strategy) for name, strategy, _gaps in report] == [("toy", "smoke-cli")]
    assert os.environ["AGENTFLOW_CONFIG"] == str(outer)
