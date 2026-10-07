#!/usr/bin/env python3
"""The ORDERED run against an interpreter that cannot import the deps (gh#186).

`provision.py` pledges to be stdlib-only (`provision.py`, the `_scrape` docstring) because
PyYAML lands in the suite venv at step 4. Five later sites broke that pledge by importing
`agentflow.config` -- whose module-level `import yaml` is the thing that decides whether the
import succeeds at all -- in the PARENT interpreter, the one the operator typed. On a fresh
machine step 5 died with a raw `ModuleNotFoundError` and exit 1.

WHY THE EXISTING SUITE COULD NOT SEE IT, which is the part worth keeping. Every test in
`test_provision_fresh_machine.py` is unit-level: it patches `provision.subprocess.run` and
`provision.shutil.which`, calls individual step functions directly, and never calls
`provision.main()`. So nothing executed the ORDERED run, and both the author's machine and the
CI runner have a python that already carries PyYAML -- "fresh machine" was being modelled as
"a machine that isn't the author's" rather than "a machine with nothing installed".

These tests model the missing dependency rather than a missing machine: the condition that
matters is `_parent_can_import_deps()` returning False, and it is injected. That keeps them
hermetic and fast while still exercising `main()` end to end.
"""

from __future__ import annotations

import io
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402


@pytest.fixture
def opts(tmp_path):
    """Enough of an Options for the handover path; no step actually runs."""
    return provision.Options(
        dry_run=False,
        suite_root=tmp_path / "suite",
        config_home=tmp_path / "config",
        settings_file=tmp_path / "settings.json",
        autodoc=False,
        autodoc_reason="test",
        claude_root=tmp_path / "claude",
        force=False,
    )


# --------------------------------------------------------------------------- #
# The predicate
# --------------------------------------------------------------------------- #
def test_the_predicate_reports_what_THIS_interpreter_can_import():
    """It is about the running interpreter, not about a venv on disk - that is the whole
    distinction the bug turned on."""
    assert provision._parent_can_import_deps() is True     # the suite venv runs these tests

    with mock.patch.dict(sys.modules, {"yaml": None}):
        # `import yaml` against a None entry raises ImportError, which is what a machine
        # without PyYAML produces.
        assert provision._parent_can_import_deps() is False


def test_the_predicate_treats_a_BROKEN_dependency_as_unusable_too():
    """A half-installed PyYAML raises from its own module body - a broken .pth, a partial
    wheel, a C-extension mismatch. That is still "this interpreter cannot use the deps", which
    is the only question being asked; catching only ImportError let it escape as a traceback
    from inside the predicate whose whole job is to answer without raising."""
    import builtins
    real = builtins.__import__

    def boom(name, *a, **k):
        if name == "yaml":
            raise RuntimeError("PyYAML is installed but broken")
        return real(name, *a, **k)

    with mock.patch.object(builtins, "__import__", boom):
        assert provision._parent_can_import_deps() is False


# --------------------------------------------------------------------------- #
# The handover
# --------------------------------------------------------------------------- #
def test_step_4_hands_over_when_the_parent_cannot_import_the_deps(opts, monkeypatch):
    """THE REGRESSION TEST FOR gh#186, at the END of step 4.

    Step 4 is the first moment the venv and PyYAML exist, so it is the earliest point a
    handover is possible. An earlier draft handed off from inside step 5 instead, AFTER that
    step's `pip install -e` had already run - so the child re-ran step 5 from the top: a second
    editable install, a duplicated step-5 header, and a parent that printed `5. ...` and never
    printed `5. done.`. That transcript contradicts the rationale `--resume-from` exists for."""
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    venv_py = opts.suite_root / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")

    calls = []
    with mock.patch.object(provision, "_run_cmd", lambda *a, **k: None), \
         mock.patch.object(provision, "_venv_python", lambda _o: venv_py), \
         mock.patch.object(provision, "_parent_can_import_deps", lambda: False), \
         mock.patch.object(provision.subprocess, "call",
                           lambda argv, **k: calls.append(argv) or 0):
        with pytest.raises(provision._HandOff) as caught:
            provision._do_python_venv(opts)

    assert caught.value.code == 0
    assert calls, "no child process was started"
    argv = calls[0]
    assert argv[0] == str(venv_py), "the child must be the VENV interpreter"
    assert "--resume-from" in argv and argv[argv.index("--resume-from") + 1] == "5"


def test_step_5_NEVER_hands_over_so_the_editable_install_runs_once(opts, monkeypatch):
    """The other half of moving it. Step 5 must do its work and nothing else - if it could also
    hand over, the child would redo the `pip install -e` the parent had just finished."""
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    confirmed = []
    with mock.patch.object(provision, "_run_cmd", lambda *a, **k: None), \
         mock.patch.object(provision, "_venv_python", lambda _o: opts.suite_root / "py"), \
         mock.patch.object(provision, "_parent_can_import_deps", lambda: False), \
         mock.patch.object(provision.subprocess, "call",
                           lambda *a, **k: pytest.fail("step 5 handed over; step 4 owns that")), \
         mock.patch.object(provision, "_confirm_autodoc_against_config",
                           lambda _o: confirmed.append(True)):
        provision._do_editable_install(opts)
    assert confirmed == [True]


def test_no_handover_when_the_parent_CAN_import_the_deps(opts, monkeypatch):
    """The other side. A handover on every run would fork a process the install does not need
    and would double every later step's output."""
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    with mock.patch.object(provision, "_run_cmd", lambda *a, **k: None), \
         mock.patch.object(provision, "_venv_python", lambda _o: opts.suite_root / "py"), \
         mock.patch.object(provision, "_parent_can_import_deps", lambda: True), \
         mock.patch.object(provision.subprocess, "call",
                           lambda *a, **k: pytest.fail("handed over unnecessarily")):
        provision._do_python_venv(opts)


def test_the_child_NEVER_hands_over_again(opts, monkeypatch):
    """Loop protection. The condition is already false in the venv, so this is belt and
    braces - but a re-exec loop is an install that never terminates, and 'already false'
    is a claim about the child's interpreter rather than about this code."""
    monkeypatch.setenv(provision._REEXEC_MARKER, "1")
    with mock.patch.object(provision, "_run_cmd", lambda *a, **k: None), \
         mock.patch.object(provision, "_venv_python", lambda _o: opts.suite_root / "py"), \
         mock.patch.object(provision, "_parent_can_import_deps", lambda: False), \
         mock.patch.object(provision.subprocess, "call",
                           lambda *a, **k: pytest.fail("the child handed over again")):
        provision._do_python_venv(opts)


def test_the_child_environment_carries_the_marker(opts, monkeypatch):
    """The guard above is only real if the spawn actually sets it."""
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    venv_py = opts.suite_root / ".venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")

    seen = {}

    def fake_call(argv, **kw):
        seen.update(kw.get("env") or {})
        return 0

    with mock.patch.object(provision, "_venv_python", lambda _o: venv_py), \
         mock.patch.object(provision.subprocess, "call", fake_call):
        with pytest.raises(provision._HandOff):
            provision._reexec_into_venv(opts, resume_from=5)
    assert seen.get(provision._REEXEC_MARKER) == "1"


def test_a_missing_venv_interpreter_fails_LOUDLY_and_actionably(opts):
    """The handover's own precondition. Handing off to a path that does not exist would
    produce an OS error a reader cannot act on."""
    with mock.patch.object(provision, "_venv_python",
                           lambda _o: opts.suite_root / ".venv" / "nope"):
        with pytest.raises(RuntimeError) as e:
            provision._reexec_into_venv(opts, resume_from=5)
    assert "does not exist" in str(e.value)
    assert "re-run" in str(e.value).lower()


def test_the_child_exit_code_is_the_installs_exit_code(opts, monkeypatch):
    """A child that failed must not read as a successful install."""
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    venv_py = opts.suite_root / ".venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    with mock.patch.object(provision, "_venv_python", lambda _o: venv_py), \
         mock.patch.object(provision.subprocess, "call", lambda *a, **k: 3):
        with pytest.raises(provision._HandOff) as caught:
            provision._reexec_into_venv(opts, resume_from=5)
    assert caught.value.code == 3


# --------------------------------------------------------------------------- #
# The ORDERED run - what no test did before
# --------------------------------------------------------------------------- #
def test_main_runs_the_ORDERED_steps_and_returns_the_childs_code(tmp_path, monkeypatch):
    """`main()` end to end. Every existing fresh-machine test calls step functions directly,
    so the ordered run - the only place a step-5 failure can surface - had no coverage at all.
    """
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    ran = []

    def step(n):
        def _run(_opts):
            ran.append(n)
            if n == 5:
                raise provision._HandOff(0)
        return _run

    steps = [provision.Step(n=n, title=f"step {n}", run=step(n)) for n in range(1, 8)]
    with mock.patch.object(provision, "build_steps", lambda _o: steps):
        rc = provision.main(["--suite-root", str(tmp_path), "--config-home", str(tmp_path),
                             "--settings-file", str(tmp_path / "s.json"),
                             "--claude-root", str(tmp_path)])
    assert rc == 0
    assert ran == [1, 2, 3, 4, 5], "the parent must stop at the handover, not run on"


@pytest.fixture
def sealed_doctor():
    """`main()`'s last act imports `agentflow.doctor` and calls `diagnose()`, which resolves the
    LIVE config home -- `$AGENTFLOW_CONFIG` or `~/.agentflow/config` -- and NOT `opts.config_home`.
    So a test that drives `main()` to completion reads the operator's real fleet config no matter
    how carefully it pointed all four `--*` flags at `tmp_path`. It is read-only and swallowed by
    the installer's own `except Exception`, which is precisely why nothing complained: the test
    still passed, on a machine whose answer it never looked at.

    A sandbox that holds for three flags and leaks on the fourth is not a sandbox."""
    with mock.patch.dict(
        sys.modules,
        {"agentflow.doctor": mock.Mock(diagnose=lambda *_a, **_k: [], render=lambda _d: "(stubbed)",
                                       examined_a_project=lambda _f: False,
                                       unexamined_verdict=lambda r, _dir: r)},
    ):
        yield


def test_resume_from_skips_the_steps_the_parent_already_ran(tmp_path, sealed_doctor):
    """The child's half of the same run."""
    ran = []
    steps = [provision.Step(n=n, title=f"step {n}", run=lambda _o, n=n: ran.append(n))
             for n in range(1, 8)]
    with mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_print_verification_readiness", lambda _r: None), \
         mock.patch.object(provision, "_verification_readiness", lambda _o: []):
        rc = provision.main(["--resume-from", "5",
                             "--suite-root", str(tmp_path), "--config-home", str(tmp_path),
                             "--settings-file", str(tmp_path / "s.json"),
                             "--claude-root", str(tmp_path)])
    assert rc == 0
    assert ran == [5, 6, 7], "steps below the resume point must not re-run"


def test_main_returns_the_childs_NONZERO_code_rather_than_its_own_success(tmp_path, monkeypatch):
    """The zero case above cannot see the difference between `return h.code` and `return 0` --
    both produce 0, so the mutation that throws the child's exit code away survives it. A failed
    child reading as a successful install is the specific harm: the operator gets the success
    banner from a run that did not finish."""
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)

    def step(n):
        def _run(_opts):
            if n == 4:
                raise provision._HandOff(3)
        return _run

    steps = [provision.Step(n=n, title=f"step {n}", run=step(n)) for n in range(1, 8)]
    with mock.patch.object(provision, "build_steps", lambda _o: steps):
        rc = provision.main(["--suite-root", str(tmp_path), "--config-home", str(tmp_path),
                             "--settings-file", str(tmp_path / "s.json"),
                             "--claude-root", str(tmp_path)])
    assert rc == 3


def test_the_child_inherits_the_argv_MAIN_PARSED_not_the_process_argv(tmp_path, monkeypatch):
    """`main(argv=None)` takes an explicit argv and every test here uses that programmatic entry,
    so `sys.argv[1:]` and the argv actually parsed genuinely differ. Reading the process argv
    meant the child was spawned WITHOUT `--config-home` / `--claude-root` / `--settings-file`,
    which resolve to the operator's real `~/.agentflow/config` and `~/.claude` when absent. A
    sandboxed run would silently re-target itself at the live machine at the moment it changed
    process -- and the transcript would look identical."""
    monkeypatch.setattr(sys, "argv", ["provision.py"])       # nothing to inherit from here
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    venv_py = tmp_path / ".venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")

    calls = []

    def step(n):
        def _run(_opts):
            if n == 4:
                provision._reexec_into_venv(_opts, resume_from=5)
        return _run

    steps = [provision.Step(n=n, title=f"step {n}", run=step(n)) for n in range(1, 8)]
    passed = ["--suite-root", str(tmp_path), "--config-home", str(tmp_path / "cfg"),
              "--settings-file", str(tmp_path / "s.json"), "--claude-root", str(tmp_path / "cl")]
    with mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_venv_python", lambda _o: venv_py), \
         mock.patch.object(provision.subprocess, "call",
                           lambda argv, **k: calls.append(argv) or 0):
        rc = provision.main(list(passed))

    assert rc == 0
    argv = calls[0]
    for flag in passed:
        assert flag in argv, f"the child lost {flag!r} and would resolve it against the real box"


class _UnhandledStepError(Exception):
    """Not RuntimeError/OSError/ImportError, which `_apply` turns into a return code; this one
    propagates out of `main()`, which is the path a `finally` exists for."""


def test_the_handover_replays_exactly_opts_invoked_argv(opts, monkeypatch):
    """The child's argv is the interpreter, this script, the run's own `invoked_argv` and the
    resume point -- in that order, nothing added and nothing dropped. Read from anywhere else
    (the process argv, shared module state), a sandboxed run hands its child someone else's
    flags."""
    monkeypatch.setattr(sys, "argv", ["provision.py", "--from-the-process-argv"])
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    venv_py = opts.suite_root / ".venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    opts.invoked_argv = ("--config-home", "/sandbox/cfg", "--no-auto-allow")

    calls = []
    with mock.patch.object(provision, "_venv_python", lambda _o: venv_py), \
         mock.patch.object(provision.subprocess, "call",
                           lambda argv, **k: calls.append(argv) or 0):
        with pytest.raises(provision._HandOff):
            provision._reexec_into_venv(opts, resume_from=5)

    assert calls == [[str(venv_py), provision.os.path.abspath(provision.__file__),
                      "--config-home", "/sandbox/cfg", "--no-auto-allow",
                      "--resume-from", "5"]]


def test_provision_keeps_NO_module_level_argv():
    """The argv lives on the run's Options. A module global is shared by every in-process
    `main()`, so one call's flags could reach another call's child."""
    assert not hasattr(provision, "_INVOKED_ARGV")
    assert provision.Options.__dataclass_fields__["invoked_argv"].default == ()


@pytest.mark.parametrize("case", ["applied", "raised"])
def test_main_threads_its_argv_through_Options(tmp_path, monkeypatch, sealed_doctor, case):
    """Every step sees the argv `main()` parsed on the Options it was handed, whether the run
    completes or a step raises out of it."""
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    seen = []

    def run(o):
        seen.append(o.invoked_argv)
        if case == "raised":
            raise _UnhandledStepError("a step failed in a way the installer does not handle")

    steps = [provision.Step(n=n, title=f"step {n}", run=run) for n in range(5, 8)]
    passed = ["--resume-from", "5",
              "--suite-root", str(tmp_path), "--config-home", str(tmp_path / "cfg"),
              "--settings-file", str(tmp_path / "s.json"), "--claude-root", str(tmp_path / "cl")]
    with mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_print_verification_readiness", lambda _r: None), \
         mock.patch.object(provision, "_verification_readiness", lambda _o: []):
        if case == "raised":
            with pytest.raises(_UnhandledStepError):
                provision.main(list(passed))
        else:
            assert provision.main(list(passed)) == 0

    assert seen and seen[0] == tuple(passed), "the run must carry the argv main() parsed"


def test_concurrent_mains_hand_each_child_its_OWN_argv(tmp_path, monkeypatch):
    """Two in-process `main()` calls on different threads, both inside a run at the same time:
    each one's handover must spawn its child with its own flags. A barrier holds both runs at
    step 4 until both have parsed, which is the interleaving a shared global gets wrong."""
    import threading

    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    # A shell-level value both runs must leave exactly as found: each passes its own home
    # explicitly and never writes the variable.
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(tmp_path / "outer"))
    venv_py = tmp_path / ".venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    barrier = threading.Barrier(2, timeout=30)
    calls = {}
    lock = threading.Lock()

    def step(n):
        def _run(o):
            if n == 4:
                barrier.wait()
                provision._reexec_into_venv(o, resume_from=5)
        return _run

    steps = [provision.Step(n=n, title=f"step {n}", run=step(n)) for n in range(1, 8)]

    def fake_call(argv, **_k):
        with lock:
            calls[argv[argv.index("--config-home") + 1]] = argv
        return 0

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
         mock.patch.object(provision, "_venv_python", lambda _o: venv_py), \
         mock.patch.object(provision.subprocess, "call", fake_call):
        threads = [threading.Thread(target=drive, args=(name,)) for name in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

    assert not errors, errors
    assert results == {"a": 0, "b": 0}
    for name in ("a", "b"):
        child = calls[str(tmp_path / name)]
        assert child[2:-2] == argv_for(name), f"run {name!r} spawned a child with another argv"


def test_an_out_of_range_resume_point_FAILS_rather_than_installing_nothing(tmp_path,
                                                                           sealed_doctor):
    """The worst outcome this flag can produce is not a crash. Without the guard, a resume point
    naming no step filters the list to EMPTY, runs zero steps, and exits 0 under the full success
    banner - an install that did nothing and said it worked, on a machine whose operator now
    believes it is provisioned. The flag is internal and argparse-suppressed, so this guards a
    future caller rather than a typo."""
    ran = []
    steps = [provision.Step(n=n, title=f"step {n}", run=lambda _o, n=n: ran.append(n))
             for n in range(1, 8)]
    with mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(sys, "stderr", io.StringIO()) as err:
        rc = provision.main(["--resume-from", "99",
                             "--suite-root", str(tmp_path), "--config-home", str(tmp_path),
                             "--settings-file", str(tmp_path / "s.json"),
                             "--claude-root", str(tmp_path)])
    assert rc == 1, "an install that ran nothing must not report success"
    assert ran == [], "no step may run once the resume point is known to be bogus"
    text = err.getvalue()
    assert "--resume-from 99" in text, "the operator must see the value that was rejected"
    assert "[1, 2, 3, 4, 5, 6, 7]" in text, "and the steps this run actually has"


def test_a_missing_dependency_TELLS_THE_OPERATOR_WHICH_OF_TWO_THINGS_IT_IS(tmp_path):
    """Asserting the exit code alone lets the message rot into advice that is wrong now. The old
    text said "re-run provision with that interpreter" - which is exactly what step 4 now does by
    itself, so following it would leave an operator repeating the thing that already happened."""
    def boom(_opts):
        raise ModuleNotFoundError("No module named 'yaml'")

    steps = [provision.Step(n=1, title="boom", run=boom)]
    with mock.patch.object(provision, "build_steps", lambda _o: steps):
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            rc = provision.main(["--suite-root", str(tmp_path), "--config-home", str(tmp_path),
                                 "--settings-file", str(tmp_path / "s.json"),
                                 "--claude-root", str(tmp_path)])
    assert rc == 1
    text = err.getvalue()
    assert "No module named 'yaml'" in text, "the operator must see what was actually missing"
    assert "delete the venv and re-run" in text          # the venv-is-broken branch
    assert "BEFORE step 4" in text                       # the this-is-a-bug branch
    assert "re-run provision with that interpreter" not in text, "the stale advice came back"


def test_a_missing_dependency_is_a_FAILURE_not_a_TRACEBACK(tmp_path):
    """`ModuleNotFoundError` is neither `RuntimeError` nor `OSError`, so it escaped the
    handler as a raw traceback - an installer dying without telling an operator what to do.
    This is the last line before a user sees a stack trace."""
    def boom(_opts):
        raise ModuleNotFoundError("No module named 'yaml'")

    steps = [provision.Step(n=1, title="boom", run=boom)]
    with mock.patch.object(provision, "build_steps", lambda _o: steps):
        rc = provision.main(["--suite-root", str(tmp_path), "--config-home", str(tmp_path),
                             "--settings-file", str(tmp_path / "s.json"),
                             "--claude-root", str(tmp_path)])
    assert rc == 1


# --------------------------------------------------------------------------- #
# The one thing a mock cannot check
# --------------------------------------------------------------------------- #
def test_the_TRANSCRIPT_IS_IN_ORDER_with_a_real_child_and_a_real_pipe(tmp_path):
    """THE ONLY UNMOCKED TEST HERE, and it is unmocked because the defect it covers lives in the
    two things a mock erases: a real inherited file descriptor and a real second process.

    Python line-buffers stdout to a TTY and BLOCK-buffers it to a pipe or a file. The child
    inherits the parent's fd, so without an explicit flush before the spawn the parent's banner
    and steps 1-4 sit in the parent's buffer until the parent EXITS -- after the child has
    already streamed its own output through the same fd. The install then reads backwards to
    every non-interactive consumer: the fresh-clone drill (which captures through a pipe), CI,
    and an operator running `> install.log`. On a TTY it looks perfect, which is exactly why it
    survived review.

    Mocking `subprocess.call` cannot see this: with no real child there is no interleaving to
    get wrong. So the child here is a real `provision.py --dry-run`, which the drill's first limb
    independently establishes is inert -- it renders a plan and mutates nothing -- and every path
    it is handed points inside `tmp_path` regardless."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    child_argv = ["--dry-run", "--no-autodoc",
                  "--suite-root", str(sandbox / "suite"),
                  "--config-home", str(sandbox / "config"),
                  "--settings-file", str(sandbox / "settings.json"),
                  "--claude-root", str(sandbox / "claude")]

    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent(f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(Path(provision.__file__).parent)!r})
        import provision

        # The handover spawns `<this> <provision.py> <argv> --resume-from 5`. Point it at a real
        # interpreter so a real process actually runs; the argv makes that process a dry run.
        provision._venv_python = lambda _o: Path(sys.executable)

        opts = provision.Options(
            dry_run=False, suite_root=Path({str(sandbox / "suite")!r}),
            config_home=Path({str(sandbox / "config")!r}),
            settings_file=Path({str(sandbox / "settings.json")!r}),
            autodoc=False, autodoc_reason="test",
            claude_root=Path({str(sandbox / "claude")!r}), force=False,
            invoked_argv=tuple({child_argv!r}),
        )

        print("PARENT-SPOKE-FIRST")
        try:
            provision._reexec_into_venv(opts, resume_from=5)
        except provision._HandOff as h:
            print("PARENT-SPOKE-LAST exit=%d" % h.code)
    """), encoding="utf-8")

    # capture_output gives the driver a PIPE, which is the block-buffered case. Running it
    # through a TTY would pass with or without the flush and prove nothing.
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                         timeout=120)
    out = res.stdout
    assert res.returncode == 0, res.stderr

    first = out.find("PARENT-SPOKE-FIRST")
    handoff = out.find("handing off to the suite venv")
    child = out.find("agentflow provision - DRY RUN")
    last = out.find("PARENT-SPOKE-LAST exit=0")
    assert -1 not in (first, handoff, child, last), f"a marker is missing from:\n{out}"
    assert first < handoff < child < last, (
        "the parent's output arrived AFTER the child's - the pre-spawn flush is gone:\n" + out
    )
