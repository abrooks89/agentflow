#!/usr/bin/env python3
"""The closing LAUNCHING A LANE block names the real launch directory (gh#691).

Provision writes the SessionStart and auto-allow hooks into the settings file, by default
`~/.claude/settings.local.json`. That is the PROJECT settings file for a session opened in
the directory holding that `.claude`, and for no other. So the block must name that
directory, and must say that a lane opened anywhere else loads none of the hooks.

These drive `main()` past its steps to the closing output, with a stubbed doctor (the real
one reads the live config home, not tmp_path).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402


def _closing_output(tmp_path: Path, settings_file: Path) -> str:
    doctor = mock.Mock(diagnose=lambda *_a: [], render=lambda _d: "(stubbed)\n",
                       examined_a_project=lambda _f: False,
                       unexamined_verdict=lambda r, _dir: r)
    steps = [provision.Step(n=5, title="step 5", run=lambda _o: None)]
    capture = io.StringIO()
    with mock.patch.dict(sys.modules, {"agentflow.doctor": doctor}), \
         mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_print_verification_readiness", lambda _r: None), \
         mock.patch.object(provision, "_verification_readiness", lambda _o: []), \
         mock.patch.object(sys, "stdout", capture):
        rc = provision.main(["--resume-from", "5",
                             "--suite-root", str(tmp_path), "--config-home", str(tmp_path / "cfg"),
                             "--settings-file", str(settings_file),
                             "--claude-root", str(tmp_path)])
    assert rc == 0
    out = capture.getvalue()
    return out[out.index("LAUNCHING A LANE"):out.index("Resolved-surface check")]


def test_the_launch_block_names_the_directory_holding_the_settings_files_claude_dir(tmp_path):
    home = tmp_path / "home"
    block = _closing_output(tmp_path, home / ".claude" / "settings.local.json")
    assert f"cd {home} && claude" in block
    assert "<launch_dir>" not in block


def test_the_launch_block_says_a_lane_elsewhere_loads_none_of_the_hooks(tmp_path):
    block = _closing_output(tmp_path, tmp_path / "home" / ".claude" / "settings.local.json")
    flat = " ".join(block.split())
    assert "still fire" not in flat
    assert "user-level" not in flat
    assert "loads none of those hooks: no config injection, no auto-allow" in flat


def test_a_launch_dir_with_a_space_is_quoted(tmp_path):
    home = tmp_path / "some home"
    block = _closing_output(tmp_path, home / ".claude" / "settings.local.json")
    assert f'cd "{home}" && claude' in block


def test_a_settings_file_outside_a_claude_dir_names_no_launch_dir(tmp_path):
    block = _closing_output(tmp_path, tmp_path / "s.json")
    assert "&& claude" not in block, "no directory loads this file, so none may be named"
    assert "<launch_dir>" not in block
    assert "no launch directory to name" in " ".join(block.split())


def test_launch_dir_is_the_parent_of_the_claude_dir(tmp_path):
    assert provision._launch_dir(tmp_path / ".claude" / "settings.local.json") == tmp_path
    assert provision._launch_dir(tmp_path / "settings.local.json") is None


def test_a_relative_settings_file_is_resolved_before_naming_the_launch_dir(tmp_path, monkeypatch):
    """`--settings-file .claude/settings.local.json` used to print `cd . && claude`."""
    monkeypatch.chdir(tmp_path)
    block = _closing_output(tmp_path, Path(".claude") / "settings.local.json")
    assert f"cd {tmp_path.resolve()} && claude" in block
    assert "cd . && claude" not in block
