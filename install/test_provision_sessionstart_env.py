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
    doctor = mock.Mock(diagnose=lambda *_a: [], render=lambda _d: "(stubbed)\n",
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
