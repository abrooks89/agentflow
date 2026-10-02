#!/usr/bin/env python3
"""Tests for the missed-wake watchdog registration in install/provision.py (E-0768, step 12).

THE ONE DAEMON. The suite's daemon-free invariant was revoked on 2026-09-22 in favour of
a single-daemon rule, and this step is what spends that budget: an OS-scheduled,
report-only invocation of `python -m agentflow.wake_watch`. These tests pin the four
properties that make it the permitted one rather than the first of many — it reports and
never relaunches, it is idempotent, it is undoable, and it says at the registration site
that the budget is now spent.

They also pin the half that is easiest to lose: on a platform this installer cannot
register unattended, the step must hand the operator the exact line instead of skipping
quietly. A silent skip would make the epic true on one platform while reading as shipped
on both.

And, since gh#544 (2026-09-24), that the step is OPT-IN: a default run registers
nothing on any platform and its SKIPPED line names `--wake-watch`. Every test that
exercises registration or the crontab hand-off therefore opts in explicitly.

Every subprocess is faked. Nothing here creates a real scheduled task.

Run: `python -m pytest install/test_provision_watchdog.py -v`
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision


def _make_opts(tmp_path: Path, *, wake_watch: bool = True) -> provision.Options:
    """Opted in unless a test says otherwise: most of this file pins what the step does
    WHEN it runs. The default-off contract is pinned by the tests that pass
    `wake_watch=False` and by the ones that go through `main()`, which build Options
    from argparse and never from here."""
    return provision.Options(
        dry_run=False,
        suite_root=tmp_path / "suite",
        config_home=tmp_path / "agentflow" / "config",
        settings_file=tmp_path / "settings.local.json",
        autodoc=False,
        autodoc_reason="forced by --no-autodoc",
        wake_watch=wake_watch,
    )


def _with_projects(opts: provision.Options, *names: str) -> provision.Options:
    d = opts.config_home / "projects"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / f"{n}.md").write_text("```yaml\nname: x\n```\n", encoding="utf-8")
    return opts


class _FakeSchtasks:
    """Records `_run_cmd` calls instead of running them."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, env=None, cwd=None):
        self.calls.append(list(cmd))

    @property
    def task_names(self) -> list[str]:
        return [c[c.index("/TN") + 1] for c in self.calls if "/TN" in c]


@pytest.fixture
def fake_run(monkeypatch):
    fake = _FakeSchtasks()
    monkeypatch.setattr(provision, "_run_cmd", fake)
    return fake


@pytest.fixture
def on_windows(monkeypatch):
    """Force the Windows answer WITHOUT touching `os.name`: patching that swaps every
    `Path` in the process for the wrong flavour and takes pytest's own teardown down with
    it. The platform question is asked through this one function precisely so a test can
    answer it."""
    monkeypatch.setattr(provision, "_watchdog_registrable", lambda _n: True)


@pytest.fixture
def off_windows(monkeypatch):
    monkeypatch.setattr(provision, "_watchdog_registrable", lambda _n: False)


# --------------------------------------------------------------------------- #
# Platform gate
# --------------------------------------------------------------------------- #
def test_only_windows_is_registered_by_the_installer():
    assert provision._watchdog_registrable("nt") is True
    assert provision._watchdog_registrable("posix") is False


def test_the_non_windows_skip_hands_over_the_exact_cron_line(tmp_path, off_windows):
    """A skip that printed nothing would be the whole failure this epic is about, one
    platform down: the operator would read `[SKIPPED]` and learn nothing about what is now
    not watching their fleet."""
    opts = _with_projects(_make_opts(tmp_path), "alpha", "beta")
    step = provision.build_steps(opts)[-1]

    assert step.is_applicable(opts) is False
    note = step.skip_reason(opts)
    for project in ("alpha", "beta"):
        assert f"--project {project}" in note
    assert "crontab -e" in note
    assert "-m agentflow.wake_watch" in note
    assert "2>&1" in note, "a cron line without the redirect loses every finding"
    assert "never relaunches" in note
    assert "INSTALL.md" in note, "the operator needs the removal instructions too"


def test_the_skip_note_names_a_placeholder_when_no_project_is_configured(tmp_path, off_windows):
    """The note is printed during `--dry-run`, which may run before any project exists.
    A crash there would take the whole plan print down over a step that is skipped anyway."""
    opts = _make_opts(tmp_path)
    note = provision.build_steps(opts)[-1].skip_reason(opts)
    assert "--project <project>" in note


def test_dry_run_reaches_the_note_without_importing_agentflow(tmp_path, off_windows, monkeypatch):
    """`--dry-run` promises to print the plan without importing `agentflow`. The skip note
    names the configured projects, so it must read them off the config dir rather than
    through the resolver."""
    opts = _with_projects(_make_opts(tmp_path), "alpha")
    monkeypatch.setitem(sys.modules, "agentflow", None)  # any import of it now raises
    assert "--project alpha" in provision.build_steps(opts)[-1].skip_reason(opts)


# --------------------------------------------------------------------------- #
# Opt-in, off by default (gh#544)
# --------------------------------------------------------------------------- #
def test_options_default_is_off(tmp_path):
    opts = provision.Options(
        dry_run=True, suite_root=tmp_path, config_home=tmp_path / "config",
        settings_file=tmp_path / "s.json", autodoc=False, autodoc_reason="x",
    )
    assert opts.wake_watch is False


def test_not_opted_in_the_step_is_skipped_even_on_windows(tmp_path, on_windows):
    opts = _with_projects(_make_opts(tmp_path, wake_watch=False), "alpha")
    step = provision.build_steps(opts)[-1]
    assert step.run is provision._do_register_watchdog
    assert step.is_applicable(opts) is False
    note = step.skip_reason(opts)
    assert "--wake-watch" in note
    assert "REGISTER IT BY HAND" not in note, "off by default is not a skip the operator must repair"
    assert "crontab" not in note, "the crontab hand-off is for an operator who opted in"
    assert "removes nothing" in note, "a default run must not read as having removed a task"


def test_not_opted_in_off_windows_hands_over_no_cron_line(tmp_path, off_windows):
    opts = _with_projects(_make_opts(tmp_path, wake_watch=False), "alpha")
    note = provision.build_steps(opts)[-1].skip_reason(opts)
    assert "--wake-watch" in note
    assert "crontab" not in note and "--project alpha" not in note


def test_not_opted_in_execute_registers_nothing(tmp_path, fake_run, on_windows, capsys):
    """The run path, not just the gate: `execute` must print SKIPPED naming the flag and
    call no scheduler at all."""
    opts = _with_projects(_make_opts(tmp_path, wake_watch=False), "alpha")
    provision.execute(opts, [provision.build_steps(opts)[-1]])
    assert fake_run.calls == []
    out = capsys.readouterr().out
    assert "12. Register the missed-wake watchdog" in out
    assert "- SKIPPED (off by default - pass --wake-watch" in out


def test_register_refuses_when_not_opted_in(tmp_path, fake_run, on_windows):
    """Guard against a caller that bypasses `build_steps`: no opt-in, no scheduled task."""
    opts = _with_projects(_make_opts(tmp_path, wake_watch=False), "alpha")
    with pytest.raises(RuntimeError, match="--wake-watch"):
        provision._do_register_watchdog(opts)
    assert fake_run.calls == []


def _dry_run(tmp_path: Path, capsys, *flags: str) -> str:
    _with_projects(_make_opts(tmp_path), "alpha")
    rc = provision.main([
        "--dry-run", *flags,
        "--suite-root", str(tmp_path / "suite"),
        "--config-home", str(tmp_path / "agentflow" / "config"),
        "--settings-file", str(tmp_path / "settings.local.json"),
        "--claude-root", str(tmp_path / "claude"),
        "--no-autodoc",
    ])
    assert rc == 0
    return capsys.readouterr().out


def _step12_block(out: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("12. "))
    return lines[start:]


def test_main_without_the_flag_skips_step_12_and_names_the_flag(tmp_path, on_windows, capsys):
    """What an operator actually runs. A default-on mutation anywhere between argparse and
    the gate (the argparse default, the Options default, or `_watchdog_applies` ignoring
    the flag) fails here."""
    block = _step12_block(_dry_run(tmp_path, capsys))
    assert block[0].endswith("[SKIPPED]"), block
    assert "--wake-watch" in block[1], block


def test_main_with_no_wake_watch_matches_the_default(tmp_path, on_windows, capsys):
    block = _step12_block(_dry_run(tmp_path, capsys, "--no-wake-watch"))
    assert block[0].endswith("[SKIPPED]"), block


def test_main_with_the_flag_plans_the_registration(tmp_path, on_windows, capsys):
    block = _step12_block(_dry_run(tmp_path, capsys, "--wake-watch"))
    assert not block[0].endswith("[SKIPPED]"), block
    assert any("schtasks /Delete" in ln for ln in block), block


def test_main_with_the_flag_off_windows_hands_over_the_cron_line(tmp_path, off_windows, capsys):
    block = "\n".join(_step12_block(_dry_run(tmp_path, capsys, "--wake-watch")))
    assert "[SKIPPED]" in block
    assert "crontab -e" in block and "--project alpha" in block


# --------------------------------------------------------------------------- #
# The registered command line
# --------------------------------------------------------------------------- #
def test_the_command_runs_the_venv_python_not_a_bare_one(tmp_path):
    opts = _make_opts(tmp_path)
    cmd = provision._watchdog_command(opts, "alpha")
    assert str(provision._venv_python(opts)) in cmd
    assert "-m agentflow.wake_watch --project alpha" in cmd


def test_the_command_captures_both_streams(tmp_path):
    """wake_watch exits 0 on findings and on none alike, and warns to stderr when the ntfy
    channel is unconfigured. A job wired to the exit code alone reports permanent health
    over a fleet nobody is watching."""
    cmd = provision._watchdog_command(_make_opts(tmp_path), "alpha")
    assert ">>" in cmd and "2>&1" in cmd


def test_every_path_is_derived_from_options_not_a_literal_home(tmp_path):
    """Requirement 4. A `--config-home` / `--suite-root` override must move the whole
    watchdog with it; nothing may be pinned to one machine.

    Asserted by MOVING the roots and requiring the command to move with them, rather than
    by grepping the result for a home path — on a box where the test's tmp dir lives under
    the home dir, that grep fails on a correct implementation and proves nothing on any
    other."""
    one = provision._watchdog_command(_make_opts(tmp_path / "one"), "alpha")
    two = provision._watchdog_command(_make_opts(tmp_path / "two"), "alpha")
    assert one != two
    assert str(tmp_path / "one") in one and str(tmp_path / "two") not in one
    opts = _make_opts(tmp_path)
    assert provision._watchdog_dir(opts) == opts.config_home.parent / "watchdog"
    assert str(provision._venv_python(opts)) in provision._watchdog_command(opts, "alpha")


def test_the_task_name_is_deterministic_and_project_scoped():
    """Determinism is what makes `/F` idempotent rather than destructive, and the project
    scope is what keeps a watcher pointed at the board its fleet actually writes."""
    assert provision._watchdog_task_name("alpha") == "agentflow-wake-watch-alpha"
    assert provision._watchdog_task_name("beta") != provision._watchdog_task_name("alpha")


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_registration_creates_one_task_per_project(tmp_path, fake_run, on_windows):
    opts = _with_projects(_make_opts(tmp_path), "alpha", "beta")
    provision._do_register_watchdog(opts)
    assert fake_run.task_names == ["agentflow-wake-watch-alpha", "agentflow-wake-watch-beta"]


def test_re_running_provision_does_not_create_a_second_entry(tmp_path, fake_run, on_windows):
    """Requirement 3, as far as a unit test can reach it: a second run asks the scheduler for
    the SAME one task name, and asks for it with `/F`.

    That is an argv assertion, not a scheduler one. `_run_cmd` is monkeypatched here, so what
    `schtasks /Create /F` actually does to a pre-existing entry — replace it rather than fail
    or stack a second — is PROXIED by the presence of the flag, on the documented behaviour of
    the tool. Proving convergence for real would mean writing to the live Task Scheduler from a
    unit test, which is not a trade this suite makes. The repair half is not proxied:
    `test_a_re_run_repairs_a_stale_wrapper` reads the rewritten wrapper off disk."""
    opts = _with_projects(_make_opts(tmp_path), "alpha")
    provision._do_register_watchdog(opts)
    provision._do_register_watchdog(opts)
    assert set(fake_run.task_names) == {"agentflow-wake-watch-alpha"}
    for call in fake_run.calls:
        assert "/F" in call, "without /F the second run either fails or duplicates"


def test_a_re_run_repairs_a_stale_wrapper(tmp_path, fake_run, on_windows):
    """A moved checkout changes the interpreter path. A watchdog pointed at a deleted venv
    still appears in the scheduler while reporting nothing — the silence this epic is about."""
    opts = _with_projects(_make_opts(tmp_path), "alpha")
    wrapper = provision._watchdog_wrapper(opts, "alpha")
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    # A marker no tmp path can collide with: the first draft asserted on the word "stale",
    # which pytest builds this test's own directory name out of, so it failed on correct
    # output. A fixture token has to be unique against the paths under test, not just vivid.
    wrapper.write_text("@echo off\nREM ZZ-PREVIOUS-CONTENTS-ZZ\n", encoding="utf-8")
    provision._do_register_watchdog(opts)
    rewritten = wrapper.read_text(encoding="utf-8")
    assert "ZZ-PREVIOUS-CONTENTS-ZZ" not in rewritten
    assert str(provision._venv_python(opts)) in rewritten


def test_the_wrapper_says_what_it_is_and_how_to_remove_it(tmp_path):
    body = provision._watchdog_wrapper_body(_make_opts(tmp_path), "alpha")
    assert "schtasks /Delete /TN agentflow-wake-watch-alpha /F" in body
    assert "REPORT-ONLY" in body
    assert "never relaunches" in body
    assert "ONE DAEMON" in body


def test_no_projects_configured_says_so_instead_of_registering_silently(
    tmp_path, fake_run, on_windows, capsys
):
    opts = _make_opts(tmp_path)
    opts.config_home.mkdir(parents=True, exist_ok=True)
    provision._do_register_watchdog(opts)
    assert fake_run.calls == []
    assert "NOTHING was registered" in capsys.readouterr().out


def test_registration_refuses_loudly_if_reached_off_windows(tmp_path, fake_run, off_windows):
    """The gate lives in `build_steps`; this is the guard against a future caller that
    bypasses it. A crontab is the operator's file and may hold their own jobs — rewriting
    it unattended is how a provisioner silently drops somebody else's scheduled work."""
    opts = _with_projects(_make_opts(tmp_path), "alpha")
    with pytest.raises(RuntimeError, match="non-Windows"):
        provision._do_register_watchdog(opts)
    assert fake_run.calls == []


# --------------------------------------------------------------------------- #
# The budget statement
# --------------------------------------------------------------------------- #
def test_the_registration_site_carries_the_single_daemon_budget(tmp_path):
    """Requirement 5, and the reason it is a test rather than a convention: the next author
    who wants a second daemon arrives HERE looking for a precedent. What they must find is
    the statement that the budget is one and already spent."""
    src = Path(provision.__file__).read_text(encoding="utf-8")
    site = src[src.index("Missed-wake watchdog registration"):]
    assert "SINGLE-DAEMON BUDGET IS ONE" in site
    assert "2026-09-22" in site
    assert "second daemon is a new decision" in site


def test_the_step_detail_states_report_only_and_the_budget(tmp_path, on_windows):
    """The dry-run plan is what an operator reads before consenting to the install, so the
    two facts that make this daemon acceptable have to be in the plan, not only in source."""
    opts = _with_projects(_make_opts(tmp_path), "alpha")
    detail = "\n".join(provision.build_steps(opts)[-1].detail)
    assert "NEVER relaunches" in detail
    assert "SINGLE-DAEMON BUDGET IS ONE AND THIS SPENDS IT" in detail
    assert "schtasks /Delete" in detail, "the plan must also say how to undo it"


def test_the_watchdog_step_runs_after_the_scaffold_step(tmp_path, on_windows):
    """It reads the registry step 11 seeds. Registering a schedule against a board that does
    not exist yet would leave a failing job behind as the install's last act."""
    steps = provision.build_steps(_make_opts(tmp_path))
    numbers = {s.run: s.n for s in steps}
    assert numbers[provision._do_register_watchdog] > numbers[provision._do_scaffold_data_root]
    assert steps[-1].run is provision._do_register_watchdog
