#!/usr/bin/env python3
"""Tests for the autodoc Node package resolution fix in install/provision.py.

Covers three regressions bundled into one fix:

1. `_run_cmd` didn't accept `cwd=`, so `_do_autodoc_npm` had to fake a working
   directory via the `npm_config_prefix` env var (which sets npm's global prefix,
   not its cwd — the wrong lever entirely).
2. `_autodoc_pkg_dir` resolved the Node package location by loading
   `agentflow.config.load_environment()` and reading `env.autodoc_root` — a config
   round-trip for a path that is actually fixed relative to `suite_root`. On any
   config load failure it silently returned `None`.
3. Silently returning `None` meant a missing/misconfigured autodoc package produced
   a generic downstream RuntimeError instead of naming the expected path.

Fix under test: the autodoc Node package lives at a fixed location,
`<suite_root>/skills/autodoc-init/references/inventory`, resolved without touching
environment config. `_autodoc_pkg_dir` returns that `Path` when `package.json` is
present there, and raises `RuntimeError` naming the expected path otherwise (never
`Optional`/`None`). `_do_autodoc_npm` runs `npm ci` with `cwd=<pkg>`.

Run: `python -m pytest install/test_provision_autodoc.py -v`
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402


AUTODOC_PKG_REL = "skills/autodoc-init/references/inventory"


def _make_opts(suite_root: Path) -> provision.Options:
    return provision.Options(
        dry_run=False,
        suite_root=suite_root,
        config_home=suite_root / "unused-config-home",
        settings_file=suite_root / "unused-settings.json",
        autodoc=True,
        autodoc_reason="forced by --autodoc",
    )


# --------------------------------------------------------------------------- #
# _run_cmd(cmd, cwd=...)
# --------------------------------------------------------------------------- #
def test_run_cmd_accepts_cwd_and_passes_it_to_subprocess_run(tmp_path):
    target_dir = tmp_path / "somewhere"
    target_dir.mkdir()
    captured = {}

    def fake_run(cmd, env=None, cwd=None, text=None, capture_output=None):
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # which mocked: _run_cmd resolves cmd[0] via shutil.which (AF-B1); PATH-independent here.
    with mock.patch.object(provision.shutil, "which", return_value="npm"), \
         mock.patch.object(provision.subprocess, "run", side_effect=fake_run):
        provision._run_cmd(["npm", "ci"], cwd=target_dir)

    assert captured["cwd"] == target_dir


def test_run_cmd_without_cwd_still_works(tmp_path):
    def fake_run(cmd, env=None, cwd=None, text=None, capture_output=None):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch.object(provision.shutil, "which", return_value="echo"), \
         mock.patch.object(provision.subprocess, "run", side_effect=fake_run):
        provision._run_cmd(["echo", "hi"])  # must not raise (cwd is optional)


# --------------------------------------------------------------------------- #
# _autodoc_pkg_dir
# --------------------------------------------------------------------------- #
def test_autodoc_pkg_dir_resolves_fixed_path_under_suite_root(tmp_path):
    pkg_dir = tmp_path / AUTODOC_PKG_REL
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text("{}", encoding="utf-8")
    opts = _make_opts(tmp_path)

    result = provision._autodoc_pkg_dir(opts)

    assert result == pkg_dir


def test_autodoc_pkg_dir_never_reads_environment_config(tmp_path):
    # No environment.md anywhere under config_home, and config_home itself doesn't
    # even exist — if resolution touched agentflow.config.load_environment() this
    # would raise/return None instead of finding the fixed-path package.
    pkg_dir = tmp_path / AUTODOC_PKG_REL
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text("{}", encoding="utf-8")
    opts = _make_opts(tmp_path)
    assert not opts.config_home.exists()

    result = provision._autodoc_pkg_dir(opts)

    assert result == pkg_dir


def test_autodoc_pkg_dir_raises_runtimeerror_naming_expected_path_when_missing(tmp_path):
    opts = _make_opts(tmp_path)
    expected_path = tmp_path / AUTODOC_PKG_REL / "package.json"

    with pytest.raises(RuntimeError) as excinfo:
        provision._autodoc_pkg_dir(opts)

    assert str(expected_path) in str(excinfo.value)


def test_autodoc_pkg_dir_return_type_is_never_optional(tmp_path):
    # Present case returns a concrete Path (not None); the annotation itself is
    # asserted by the absent-case test raising instead of returning None.
    pkg_dir = tmp_path / AUTODOC_PKG_REL
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text("{}", encoding="utf-8")
    opts = _make_opts(tmp_path)

    result = provision._autodoc_pkg_dir(opts)

    assert result is not None
    assert isinstance(result, Path)


# --------------------------------------------------------------------------- #
# _do_autodoc_npm
# --------------------------------------------------------------------------- #
def test_do_autodoc_npm_runs_npm_ci_with_cwd_equal_to_pkg_dir(tmp_path):
    pkg_dir = tmp_path / AUTODOC_PKG_REL
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text("{}", encoding="utf-8")
    opts = _make_opts(tmp_path)

    with mock.patch.object(provision, "_run_cmd") as fake_run_cmd:
        provision._do_autodoc_npm(opts)

    fake_run_cmd.assert_called_once()
    call_args, call_kwargs = fake_run_cmd.call_args
    assert call_args[0] == ["npm", "ci"]
    assert call_kwargs.get("cwd") == pkg_dir


def test_do_autodoc_npm_env_has_no_npm_config_prefix(tmp_path):
    pkg_dir = tmp_path / AUTODOC_PKG_REL
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text("{}", encoding="utf-8")
    opts = _make_opts(tmp_path)

    with mock.patch.object(provision, "_run_cmd") as fake_run_cmd:
        provision._do_autodoc_npm(opts)

    _, call_kwargs = fake_run_cmd.call_args
    env = call_kwargs.get("env")
    if env is not None:
        assert "npm_config_prefix" not in env


def test_npm_config_prefix_gone_from_file():
    source = Path(provision.__file__).read_text(encoding="utf-8")
    assert "npm_config_prefix" not in source


def test_do_autodoc_npm_raises_when_package_missing(tmp_path):
    opts = _make_opts(tmp_path)

    with mock.patch.object(provision, "_run_cmd") as fake_run_cmd:
        with pytest.raises(RuntimeError):
            provision._do_autodoc_npm(opts)

    fake_run_cmd.assert_not_called()


# --------------------------------------------------------------------------- #
# The autodoc GATE: a pre-install heuristic, and the resolved-config re-check
# that makes it authoritative (E-0200 item 7)
# --------------------------------------------------------------------------- #
def _config_home(tmp_path: Path, env_body: str, projects: dict | None = None) -> Path:
    home = tmp_path / "config"
    (home / "projects").mkdir(parents=True)
    (home / "environment.md").write_text(env_body, encoding="utf-8")
    for name, body in (projects or {}).items():
        (home / "projects" / f"{name}.md").write_text(body, encoding="utf-8")
    return home


def test_a_project_level_autodoc_root_is_seen_by_the_preinstall_scan(tmp_path):
    """THE GAP items 2 and 3 opened. `autodoc_root` became per-PROJECT, and the scan read
    only environment.md - so a machine whose default is null while a project sets its own
    reported autodoc OFF. That is precisely the configuration the per-project field exists
    for, so the heuristic would have been wrong first on whoever adopted it."""
    home = _config_home(
        tmp_path,
        "autodoc_root: null\n",
        {"alpha": 'autodoc_root: "~/wiki/alpha-autodoc"\n'},
    )
    on, reason = provision._resolve_autodoc(None, home)
    assert on is True
    assert "alpha" in reason, f"the reason must name WHICH file set it: {reason}"


def test_the_machine_default_still_wins_when_it_is_set(tmp_path):
    home = _config_home(tmp_path, 'autodoc_root: "~/wiki/code"\n', {"alpha": "\n"})
    on, reason = provision._resolve_autodoc(None, home)
    assert on is True
    assert "environment.md" in reason


def test_all_null_is_still_off_and_says_how_many_files_it_read(tmp_path):
    """A reason that says only "unset" leaves a reader unsure whether the scan looked at
    the project files at all - which was the actual bug, and would be invisible again."""
    home = _config_home(tmp_path, "autodoc_root: null\n",
                        {"alpha": "autodoc_root: null\n", "beta": "\n"})
    on, reason = provision._resolve_autodoc(None, home)
    assert on is False
    assert "3" in reason, f"expected a count of the files scanned: {reason}"


def test_an_explicit_flag_still_beats_every_scan(tmp_path):
    home = _config_home(tmp_path, "autodoc_root: null\n")
    assert provision._resolve_autodoc(True, home)[0] is True
    assert provision._resolve_autodoc(False, home)[0] is False


def test_no_config_at_all_is_off_and_says_so_distinctly(tmp_path):
    """Distinct from "read them and found nothing": one is an install that has not been
    configured yet, the other is a decision."""
    on, reason = provision._resolve_autodoc(None, tmp_path / "absent")
    assert on is False
    assert "no config present" in reason


def test_the_recheck_CORRECTS_a_wrong_heuristic_and_prints_both_numbers(tmp_path, capsys):
    """The re-check exists because a text scan cannot apply the resolver's precedence -
    and the DERIVED per-project path is invisible to a regex by construction, since there
    is nothing written down for it to match."""
    opts = _make_opts(tmp_path)
    opts.config_home = _config_home(tmp_path, "autodoc_root: null\n", {"alpha": "\n"})
    opts.autodoc, opts.autodoc_reason = False, "autodoc_root unset/null in 2 config file(s)"

    fake_env = type("E", (), {"autodoc_root": None, "data_root": str(tmp_path)})()
    with mock.patch.dict(sys.modules, {"agentflow.config": mock.MagicMock(
        load_environment=lambda: fake_env,
        load_project=lambda n: type("P", (), {"name": n, "autodoc_root": None})(),
        resolve_autodoc_root=lambda e, p=None: "/derived/alpha/autodoc" if p else None,
    )}):
        provision._confirm_autodoc_against_config(opts)

    assert opts.autodoc is True
    out = capsys.readouterr().out
    assert "CORRECTED" in out
    assert "unset/null" in out, "the heuristic's reason must survive into the report"
    assert "derived" in out, "the resolved answer must be named too"


def test_the_recheck_is_SILENT_when_the_heuristic_was_already_right(tmp_path, capsys):
    """A gate that announces itself on every run trains the reader to skip the line that
    matters. Only a disagreement is news."""
    opts = _make_opts(tmp_path)
    opts.config_home = _config_home(tmp_path, 'autodoc_root: "~/wiki/code"\n')
    opts.autodoc, opts.autodoc_reason = True, "autodoc_root set in environment.md"

    fake_env = type("E", (), {"autodoc_root": "~/wiki/code", "data_root": str(tmp_path)})()
    with mock.patch.dict(sys.modules, {"agentflow.config": mock.MagicMock(
        load_environment=lambda: fake_env,
        load_project=lambda n: type("P", (), {"name": n, "autodoc_root": None})(),
        resolve_autodoc_root=lambda e, p=None: "/wiki/code",
    )}):
        provision._confirm_autodoc_against_config(opts)

    assert opts.autodoc is True
    assert opts.autodoc_reason == "autodoc_root set in environment.md", "reason overwritten"
    assert "CORRECTED" not in capsys.readouterr().out


def test_an_AutodocRootError_is_NOT_swallowed_by_the_recheck(tmp_path):
    """The migration hazard the resolver raises for: a resolved root with no manifest
    while a legacy root still holds the corpus. An installer that shrugged would scaffold
    the empty one and let /autodoc-init regenerate hundreds of pages from scratch."""
    from agentflow.config import AutodocRootError

    opts = _make_opts(tmp_path)
    opts.config_home = _config_home(tmp_path, "autodoc_root: null\n", {"alpha": "\n"})
    opts.autodoc = False

    def boom(env, proj=None):
        raise AutodocRootError("/new/empty", "/legacy/with/corpus")

    with mock.patch.dict(sys.modules, {"agentflow.config": mock.MagicMock(
        load_environment=lambda: type("E", (), {"autodoc_root": None, "data_root": "x"})(),
        load_project=lambda n: type("P", (), {"name": n, "autodoc_root": None})(),
        resolve_autodoc_root=boom,
        AutodocRootError=AutodocRootError,
    )}):
        with pytest.raises(AutodocRootError):
            provision._confirm_autodoc_against_config(opts)


# --------------------------------------------------------------------------- #
# gh#690: an EXPLICIT --autodoc / --no-autodoc is never silently overruled
# --------------------------------------------------------------------------- #
def _fake_config(tmp_path: Path, root):
    """agentflow.config stand-in whose resolver answers `root` for every project."""
    fake_env = type("E", (), {"autodoc_root": root, "data_root": str(tmp_path)})()
    return mock.patch.dict(sys.modules, {"agentflow.config": mock.MagicMock(
        load_environment=lambda: fake_env,
        load_project=lambda n: type("P", (), {"name": n, "autodoc_root": None})(),
        resolve_autodoc_root=lambda e, p=None: root,
    )})


def test_explicit_autodoc_with_no_resolvable_root_REFUSES(tmp_path, capsys):
    """Repro (a): autodoc_root null + `--autodoc` used to print `CORRECTED disabled -> disabled`
    and quietly finish with autodoc off. The flag cannot work, so the install refuses and names
    both the flag and the config field."""
    opts = _make_opts(tmp_path)
    opts.config_home = _config_home(tmp_path, "autodoc_root: null\n", {"alpha": "\n"})
    opts.autodoc, opts.autodoc_explicit = True, True

    with _fake_config(tmp_path, None):
        with pytest.raises(RuntimeError) as excinfo:
            provision._confirm_autodoc_against_config(opts)

    msg = str(excinfo.value)
    assert "--autodoc" in msg
    assert "autodoc_root" in msg
    assert "CORRECTED" not in capsys.readouterr().out


def test_explicit_no_autodoc_STAYS_OFF_when_config_enables_it(tmp_path, capsys):
    """Repro (b): autodoc_root set + `--no-autodoc` used to flip autodoc ON, so step 6 ran
    `npm ci`. `--no-autodoc` must never result in the autodoc install running."""
    opts = _make_opts(tmp_path)
    opts.config_home = _config_home(tmp_path, 'autodoc_root: "~/wiki/code"\n')
    opts.autodoc, opts.autodoc_reason = False, "forced by --no-autodoc"
    opts.autodoc_explicit = True

    with _fake_config(tmp_path, "/wiki/code"):
        provision._confirm_autodoc_against_config(opts)

    assert opts.autodoc is False
    assert opts.autodoc_reason == "forced by --no-autodoc"
    out = capsys.readouterr().out
    assert "CORRECTED" not in out
    assert "WARNING --no-autodoc keeps autodoc disabled" in out
    assert "/wiki/code" in out, "the warning must name what the config enables"

    step6 = next(s for s in provision.build_steps(opts) if s.n == 6)
    assert not step6.applies_fn(opts), "step 6 (npm ci) must not apply under --no-autodoc"


def test_heuristic_CORRECTED_line_prints_the_true_before_and_after(tmp_path, capsys):
    """The CORRECTED line printed `disabled -> disabled` / `enabled -> enabled`: the label was
    inverted. It must read was -> truth, and the reason prefix must appear exactly once."""
    opts = _make_opts(tmp_path)
    opts.config_home = _config_home(tmp_path, 'autodoc_root: "~/wiki/code"\n')
    opts.autodoc, opts.autodoc_reason = False, "autodoc_root unset/null in 1 config file(s)"
    assert opts.autodoc_explicit is False

    with _fake_config(tmp_path, "/wiki/code"):
        provision._confirm_autodoc_against_config(opts)

    assert opts.autodoc is True
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "  autodoc     : CORRECTED disabled -> enabled"
    assert lines[1].strip() == (
        "pre-install heuristic said: autodoc_root unset/null in 1 config file(s)"
    )
    assert lines[2].strip() == (
        "post-install re-check     : resolved config says enabled "
        "((machine default) -> /wiki/code)"
    )
    assert "\n".join(lines).count("resolved config says") == 1


def test_heuristic_CORRECTED_enabled_to_disabled(tmp_path, capsys):
    opts = _make_opts(tmp_path)
    opts.config_home = _config_home(tmp_path, "autodoc_root: null\n")
    opts.autodoc, opts.autodoc_reason = True, "AGENTFLOW_AUTODOC='1'"

    with _fake_config(tmp_path, None):
        provision._confirm_autodoc_against_config(opts)

    assert opts.autodoc is False
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "  autodoc     : CORRECTED enabled -> disabled"
    assert out.count("resolved config says") == 1
    assert "resolved config says disabled" in out


def test_main_marks_the_flag_explicit_only_when_one_was_passed(tmp_path, monkeypatch):
    """The explicit marker comes from argparse's None default, not from reason text."""
    seen = {}

    def capture(opts, steps):
        seen["explicit"] = opts.autodoc_explicit

    monkeypatch.setattr(provision, "print_plan", capture)
    base = ["--dry-run", "--config-home", str(tmp_path / "cfg")]
    for extra, want in (([], False), (["--autodoc"], True), (["--no-autodoc"], True)):
        provision.main(base + extra)
        assert seen["explicit"] is want, extra


def _dry_run_out(tmp_path, capsys, *extra) -> str:
    rc = provision.main(["--dry-run", *extra,
                         "--config-home", str(tmp_path / "cfg"),
                         "--settings-file", str(tmp_path / "s.json"),
                         "--claude-root", str(tmp_path / "claude")])
    assert rc == 0
    return capsys.readouterr().out


def test_dry_run_with_explicit_autodoc_says_it_is_confirmed_at_step_5(tmp_path, capsys):
    """A dry run never reaches the resolver, so `--dry-run --autodoc` with no autodoc root
    cannot refuse; it must at least say where the flag will be confirmed and refused."""
    out = _dry_run_out(tmp_path, capsys, "--autodoc")
    assert ("--autodoc is confirmed against the resolved config at step 5, and refused there "
            "if no autodoc_root resolves") in out


def test_dry_run_without_explicit_autodoc_prints_no_confirmation_line(tmp_path, capsys):
    for extra in ([], ["--no-autodoc"]):
        assert "confirmed against the resolved config" not in _dry_run_out(tmp_path, capsys, *extra)
