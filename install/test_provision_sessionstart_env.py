#!/usr/bin/env python3
"""Step 9 persists a non-default config home beside the SessionStart hook (gh#689).

The hook runs with the session's environment, not provision's. A config home chosen by
`--config-home` or a shell-level $AGENTFLOW_CONFIG therefore never reached it: provision
wrote the hook with no env, and every lane read the default home instead.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.delenv("AGENTFLOW_CONFIG", raising=False)
    return h


def _opts(tmp_path: Path, config_home: Path, settings: dict | None = None) -> provision.Options:
    settings_file = tmp_path / "settings.local.json"
    settings_file.write_text(json.dumps(settings or {}), encoding="utf-8")
    return provision.Options(
        dry_run=False, suite_root=tmp_path / "suite", config_home=config_home,
        settings_file=settings_file, autodoc=False, autodoc_reason="not under test",
        claude_root=tmp_path / "claude",
    )


def _saved(opts: provision.Options) -> dict:
    return json.loads(opts.settings_file.read_text(encoding="utf-8"))


def test_a_non_default_config_home_is_written_beside_the_hook(tmp_path, home):
    cfg = tmp_path / "elsewhere" / "cfg"
    opts = _opts(tmp_path, cfg)
    provision._do_sessionstart_hook(opts)
    saved = _saved(opts)
    assert saved["env"]["AGENTFLOW_CONFIG"] == str(cfg.resolve())
    assert saved["hooks"]["SessionStart"], "the hook itself must still be registered"


def test_the_default_config_home_writes_no_env_key(tmp_path, home):
    opts = _opts(tmp_path, home / ".agentflow" / "config")
    provision._do_sessionstart_hook(opts)
    saved = _saved(opts)
    assert "env" not in saved
    assert saved["hooks"]["SessionStart"]


def test_other_env_entries_are_kept(tmp_path, home):
    cfg = tmp_path / "cfg"
    opts = _opts(tmp_path, cfg, {"env": {"OTHER": "x"}})
    provision._do_sessionstart_hook(opts)
    assert _saved(opts)["env"] == {"OTHER": "x", "AGENTFLOW_CONFIG": str(cfg.resolve())}


def test_a_conflicting_existing_value_refuses_and_writes_nothing(tmp_path, home):
    cfg = tmp_path / "cfg"
    other = str(tmp_path / "other-cfg")
    opts = _opts(tmp_path, cfg, {"env": {"AGENTFLOW_CONFIG": other}})
    before = opts.settings_file.read_bytes()
    with pytest.raises(RuntimeError) as excinfo:
        provision._do_sessionstart_hook(opts)
    msg = str(excinfo.value)
    assert "env.AGENTFLOW_CONFIG" in msg and other in msg
    assert "will not overwrite" in msg
    assert opts.settings_file.read_bytes() == before


def test_a_conflicting_value_refuses_even_for_the_default_home(tmp_path, home):
    """Sessions would read the other home while this run provisioned the default one."""
    opts = _opts(tmp_path, home / ".agentflow" / "config",
                 {"env": {"AGENTFLOW_CONFIG": str(tmp_path / "other-cfg")}})
    with pytest.raises(RuntimeError, match="will not overwrite"):
        provision._do_sessionstart_hook(opts)


def test_preflight_refuses_the_conflict_before_anything_is_written(tmp_path, home):
    opts = _opts(tmp_path, tmp_path / "cfg",
                 {"env": {"AGENTFLOW_CONFIG": str(tmp_path / "other-cfg")}})
    with pytest.raises(RuntimeError, match="will not overwrite"):
        provision._do_preflight(opts)


def test_a_non_object_env_refuses(tmp_path, home):
    opts = _opts(tmp_path, tmp_path / "cfg", {"env": ["AGENTFLOW_CONFIG"]})
    with pytest.raises(RuntimeError, match='"env" is an array'):
        provision._do_sessionstart_hook(opts)


def test_a_null_env_refuses_in_preflight_and_step_9(tmp_path, home):
    """`"env": null` used to pass the check as absent and then TypeError in step 9."""
    opts = _opts(tmp_path, tmp_path / "cfg", {"env": None})
    for step in (provision._do_preflight, provision._do_sessionstart_hook):
        with pytest.raises(RuntimeError, match='"env" is null'):
            step(opts)


def test_a_null_value_is_treated_as_absent_and_written(tmp_path, home):
    cfg = tmp_path / "cfg"
    opts = _opts(tmp_path, cfg, {"env": {"AGENTFLOW_CONFIG": None}})
    provision._do_preflight(opts)
    provision._do_sessionstart_hook(opts)
    assert _saved(opts)["env"]["AGENTFLOW_CONFIG"] == str(cfg.resolve())


def test_a_relative_existing_value_refuses_even_when_it_resolves_equal(tmp_path, home, monkeypatch):
    """Relative resolves against whatever directory a session opens in, not provision's."""
    monkeypatch.chdir(tmp_path)
    opts = _opts(tmp_path, tmp_path / "cfg", {"env": {"AGENTFLOW_CONFIG": "cfg"}})
    with pytest.raises(RuntimeError, match="will not overwrite"):
        provision._do_sessionstart_hook(opts)


def test_a_rerun_with_the_same_value_changes_nothing(tmp_path, home):
    opts = _opts(tmp_path, tmp_path / "cfg")
    provision._do_sessionstart_hook(opts)
    first = opts.settings_file.read_bytes()
    provision._do_sessionstart_hook(opts)
    assert opts.settings_file.read_bytes() == first


def test_an_equal_value_spelled_differently_is_left_as_written(tmp_path, home):
    cfg = tmp_path / "cfg"
    spelled = str(tmp_path / "x" / ".." / "cfg")
    opts = _opts(tmp_path, cfg, {"env": {"AGENTFLOW_CONFIG": spelled}})
    provision._do_sessionstart_hook(opts)
    assert _saved(opts)["env"]["AGENTFLOW_CONFIG"] == spelled


def _closing(tmp_path: Path, config_home: Path, run_step_9: bool = True) -> str:
    doctor = mock.Mock(diagnose=lambda *_a, **_k: [], render=lambda _d: "(stubbed)\n",
                       examined_a_project=lambda _f: False,
                       unexamined_verdict=lambda r, _dir: r)
    settings_file = tmp_path / "h" / ".claude" / "settings.local.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("{}", encoding="utf-8")
    run = provision._do_sessionstart_hook if run_step_9 else (lambda _o: None)
    steps = [provision.Step(n=9, title="step 9", run=run)]
    capture = io.StringIO()
    with mock.patch.dict(sys.modules, {"agentflow.doctor": doctor}), \
         mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_print_verification_readiness", lambda _r: None), \
         mock.patch.object(provision, "_verification_readiness", lambda _o: []), \
         mock.patch.object(sys, "stdout", capture):
        rc = provision.main(["--resume-from", "9",
                             "--suite-root", str(tmp_path), "--config-home", str(config_home),
                             "--settings-file", str(settings_file),
                             "--claude-root", str(tmp_path)])
    assert rc == 0
    return " ".join(capture.getvalue().split())


def test_the_closing_output_names_a_persisted_config_home(tmp_path, home):
    cfg = tmp_path / "cfg"
    out = _closing(tmp_path, cfg)
    assert f"carries env.AGENTFLOW_CONFIG={cfg.resolve()}" in out


def test_the_closing_output_never_claims_a_write_that_did_not_happen(tmp_path, home):
    """Read back from the file: with step 9 not run, the file holds nothing, and says so."""
    cfg = tmp_path / "cfg"
    out = _closing(tmp_path, cfg, run_step_9=False)
    assert "carries env.AGENTFLOW_CONFIG" not in out
    assert "carries no env.AGENTFLOW_CONFIG" in out
    assert str(cfg.resolve()) in out


def test_the_persisted_env_makes_the_hook_read_the_non_default_home(tmp_path, home):
    """The gh#689 repro, end to end: the session shell has no AGENTFLOW_CONFIG, only the env
    the settings file gives it. The real hook script must resolve the provisioned home."""
    cfg = tmp_path / "cfg"
    data_root = tmp_path / "data"
    data_root.mkdir()
    d = tmp_path.as_posix()
    cfg.mkdir()
    (cfg / "environment.md").write_text(
        "```yaml\nschema_version: 1\nperson:\n  name: Sample User\n"
        f"data_root: {data_root.as_posix()}\ngit_host:\n  kind: local\nnotify:\n  kind: none\n"
        f"lock_dir: {d}\nshell: posix\nsuite_root: {d}\nworktree_root: {d}\n```\n",
        encoding="utf-8",
    )
    opts = _opts(tmp_path, cfg)
    provision._do_sessionstart_hook(opts)
    persisted = _saved(opts)["env"]

    env = {k: v for k, v in os.environ.items()
           if k not in ("AGENTFLOW_CONFIG", "AGENTFLOW_PROJECT")}
    env.update(persisted)
    env["PYTHONPATH"] = str(REPO / provision.SHARED_PKG_REL)
    done = subprocess.run([sys.executable, str(REPO / provision.SESSIONSTART_HOOK_REL)],
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert data_root.as_posix() in done.stdout.replace("\\", "/"), done.stdout

    # Control: without the persisted env the same hook reads the (empty) default home.
    env.pop("AGENTFLOW_CONFIG")
    bare = subprocess.run([sys.executable, str(REPO / provision.SESSIONSTART_HOOK_REL)],
                          capture_output=True, text=True, env=env, timeout=60)
    assert bare.returncode != 0 or data_root.as_posix() not in bare.stdout.replace("\\", "/")


def test_the_closing_output_is_silent_for_the_default_home(tmp_path, home):
    out = _closing(tmp_path, home / ".agentflow" / "config")
    assert "env.AGENTFLOW_CONFIG" not in out


def test_the_dry_run_plan_says_what_step_9_will_write(tmp_path, home):
    cfg = tmp_path / "cfg"
    opts = _opts(tmp_path, cfg)
    step9 = next(s for s in provision.build_steps(opts) if s.n == 9)
    assert any(f"env.AGENTFLOW_CONFIG={cfg.resolve()}" in line for line in step9.detail)


def test_a_config_home_flag_reaches_the_steps_that_resolve_config(tmp_path, home):
    """--config-home with no shell-level variable: bootstrap, the presence checks and the
    scaffold resolve config through agentflow.config, so they checked the default home while
    this run provisioned another. The home now travels in opts, and to the closing doctor as
    an argument; $AGENTFLOW_CONFIG is never set, so overlapping runs cannot cross."""
    cfg = tmp_path / "cfg"
    seen = {}
    steps = [provision.Step(n=n, title=f"step {n}",
                            run=lambda o, n=n: seen.__setitem__(
                                n, (os.environ.get("AGENTFLOW_CONFIG"), o.config_home.expanduser().resolve())))
             for n in (7, 10, 11)]
    doctor_homes = []
    doctor = mock.Mock(diagnose=lambda *_a, **k: doctor_homes.append(k.get("config_home")) or [],
                       render=lambda _d: "(stubbed)\n",
                       examined_a_project=lambda _f: False,
                       unexamined_verdict=lambda r, _dir: r)
    settings_file = tmp_path / "h" / ".claude" / "settings.local.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text("{}", encoding="utf-8")
    with mock.patch.dict(sys.modules, {"agentflow.doctor": doctor}), \
         mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_print_verification_readiness", lambda _r: None), \
         mock.patch.object(provision, "_verification_readiness", lambda _o: []), \
         mock.patch.object(sys, "stdout", io.StringIO()):
        rc = provision.main(["--suite-root", str(tmp_path), "--config-home", str(cfg),
                             "--settings-file", str(settings_file),
                             "--claude-root", str(tmp_path)])
    assert rc == 0
    assert seen == {n: (None, cfg.resolve()) for n in (7, 10, 11)}
    assert doctor_homes == [cfg.resolve()]
    assert "AGENTFLOW_CONFIG" not in os.environ


# --------------------------------------------------------------------------- #
# gh#985 item 3 - an AGENTFLOW_PROJECT naming an unconfigured project
# --------------------------------------------------------------------------- #
def _with_profile(cfg: Path, name: str = "toy") -> Path:
    (cfg / "projects").mkdir(parents=True, exist_ok=True)
    (cfg / "projects" / f"{name}.md").write_text("```yaml\nname: toy\n```\n", encoding="utf-8")
    return cfg


def test_a_settings_env_project_that_is_not_configured_refuses(tmp_path, home, monkeypatch):
    """The settings env shadows the shell, so the installed hook is CERTAIN to exit 1."""
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    cfg = _with_profile(tmp_path / "cfg")
    opts = _opts(tmp_path, cfg, {"env": {"AGENTFLOW_PROJECT": "otherproj"}})
    before = opts.settings_file.read_text(encoding="utf-8")

    for check in (provision._do_preflight, provision._do_sessionstart_hook):
        with pytest.raises(RuntimeError) as excinfo:
            check(opts)
        msg = str(excinfo.value)
        assert "env.AGENTFLOW_PROJECT='otherproj'" in msg
        assert str(opts.settings_file) in msg, "must say where the value came from"
        assert "configured: toy" in msg
    assert opts.settings_file.read_text(encoding="utf-8") == before, "nothing may be written"


def test_a_settings_env_project_that_is_configured_passes(tmp_path, home, monkeypatch):
    # The settings value shadows the shell's, so an unconfigured shell value is moot here.
    monkeypatch.setenv("AGENTFLOW_PROJECT", "otherproj")
    cfg = _with_profile(tmp_path / "cfg")
    opts = _opts(tmp_path, cfg, {"env": {"AGENTFLOW_PROJECT": "toy"}})
    out = io.StringIO()
    with mock.patch.object(sys, "stdout", out):
        provision._do_sessionstart_hook(opts)
    assert "WARNING" not in out.getvalue()
    assert _saved(opts)["hooks"]["SessionStart"]


def test_a_shell_only_project_that_is_not_configured_warns_and_exits_0(
        tmp_path, home, monkeypatch):
    """Only the installing shell names it: a likely failure, not a certain one - WARN."""
    monkeypatch.setenv("AGENTFLOW_PROJECT", "otherproj")
    cfg = _with_profile(tmp_path / "cfg")
    out = _closing(tmp_path, cfg)   # asserts main() returned 0
    assert "WARNING: $AGENTFLOW_PROJECT='otherproj' in this shell's environment" in out
    assert "configured: toy" in out


# --- gh#1046: the step-9 rewrite is atomic and keeps non-ASCII values as UTF-8 ---------------

def _raw_settings(tmp_path: Path, home: Path, settings: dict) -> provision.Options:
    """Options over a settings file written as raw UTF-8, as an operator's is."""
    opts = _opts(tmp_path, home / ".agentflow" / "config")
    opts.settings_file.write_bytes(
        json.dumps(settings, indent=2, ensure_ascii=False).encode("utf-8"))
    return opts


def test_non_ascii_values_survive_the_rewrite_as_utf8(tmp_path, home):
    opts = _raw_settings(tmp_path, home, {"env": {"WHO": "Zo\u00eb", "WHERE": "\u65e5\u672c"}})
    provision._do_sessionstart_hook(opts)
    raw = opts.settings_file.read_bytes()
    assert "Zo\u00eb".encode("utf-8") in raw and "\u65e5\u672c".encode("utf-8") in raw
    # Not a bare backslash-u search: a JSON-escaped Windows path can legitimately hold one.
    assert b"\\u00eb" not in raw and b"\\u65e5" not in raw, "rewritten as escapes"
    saved = _saved(opts)
    assert saved["env"] == {"WHO": "Zo\u00eb", "WHERE": "\u65e5\u672c"}
    assert saved["hooks"]["SessionStart"], "the hook must still be registered"


def test_a_failure_mid_write_leaves_the_original_intact(tmp_path, home, monkeypatch):
    opts = _raw_settings(tmp_path, home, {"env": {"WHO": "Zo\u00eb"}})
    before = opts.settings_file.read_bytes()
    listing = sorted(p.name for p in opts.settings_file.parent.iterdir())

    def boom(*_a, **_k):
        raise OSError(5, "simulated crash before the rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash"):
        provision._do_sessionstart_hook(opts)
    assert opts.settings_file.read_bytes() == before
    assert sorted(p.name for p in opts.settings_file.parent.iterdir()) == listing, \
        "no stray temp file may be left beside the settings file"


def test_the_atomic_write_is_valid_json_with_the_hook(tmp_path, home):
    opts = _opts(tmp_path, home / ".agentflow" / "config")
    provision._do_sessionstart_hook(opts)
    text = opts.settings_file.read_bytes().decode("utf-8")
    assert not text.endswith("\n"), "byte shape unchanged: json.dumps adds no trailing newline"
    saved = json.loads(text)
    cmds = [h["command"] for b in saved["hooks"]["SessionStart"] for h in b["hooks"]]
    assert len(cmds) == 1 and provision._is_agentflow_sessionstart(cmds[0])
    assert [p.name for p in opts.settings_file.parent.iterdir() if p.suffix == ".tmp"] == []


def test_a_lone_surrogate_is_written_escaped_not_crashed(tmp_path, home):
    # json.loads accepts "\ud800"; UTF-8 cannot carry it, so the escaped spelling is kept.
    opts = _opts(tmp_path, home / ".agentflow" / "config",
                 {"env": {"ODD": "\ud800", "WHO": "Zo\u00eb"}})
    provision._do_sessionstart_hook(opts)
    saved = _saved(opts)
    assert saved["env"] == {"ODD": "\ud800", "WHO": "Zo\u00eb"}
    assert saved["hooks"]["SessionStart"], "the hook must still be registered"


def test_a_symlinked_settings_file_stays_a_link(tmp_path, home):
    opts = _raw_settings(tmp_path, home, {})
    target = tmp_path / "dotfiles" / "settings.local.json"
    target.parent.mkdir()
    target.write_bytes(opts.settings_file.read_bytes())
    opts.settings_file.unlink()
    try:
        opts.settings_file.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    provision._do_sessionstart_hook(opts)
    assert opts.settings_file.is_symlink(), "the link must not be replaced by a regular file"
    assert json.loads(target.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
