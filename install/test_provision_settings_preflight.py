#!/usr/bin/env python3
"""Step 1 refuses a missing Claude settings file before anything is written (gh#743).

Steps 8 and 9 read the settings file, but only step 9 checked for it, so a fresh machine
without one got steps 2-7 (data_root pre-creation, venv, editable install, skill deploy into
the Claude config dir) before the refusal. Preflight now checks the same resolved path, and
step 9 keeps its check as a backstop with the same message.

Present is not enough (gh#751): a file that is empty, BOM-prefixed, not UTF-8, not valid JSON
or not a JSON object passed the existence check and crashed step 8 or 9 after the same writes.
Step 1 now parses it the way both readers do and refuses those too, naming the problem.

Parsing is not enough either (gh#755): an object whose "hooks" section has the wrong shape
crashed step 8 or 9 with a bare AttributeError/KeyError/TypeError. Step 1 now checks the shape
both readers walk and refuses naming the offending key path.

Run: `python -m pytest install/test_provision_settings_preflight.py -v`
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def _layout(tmp_path: Path) -> dict:
    """A config home whose environment.md names a not-yet-existing data_root (so step 2 WOULD
    create it), and a Claude config dir holding one sentinel (so step 7 WOULD add to it)."""
    config_home = tmp_path / "cfg"
    config_home.mkdir()
    data_root = tmp_path / "data" / "nested"
    (config_home / "environment.md").write_text(
        f'```yaml\nschema_version: 1\ndata_root: "{data_root.as_posix()}"\n```\n',
        encoding="utf-8",
    )
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    (claude_root / "sentinel.txt").write_text("pre-existing", encoding="utf-8")
    return {"config_home": config_home, "claude_root": claude_root, "data_root": data_root}


def _main(tmp_path: Path, lay: dict, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()

    def no_subprocess(cmd, **_kw):
        raise AssertionError(f"a subprocess ran before the refusal: {cmd}")

    with mock.patch.object(provision, "_run_cmd", no_subprocess), \
            redirect_stdout(out), redirect_stderr(err):
        rc = provision.main([
            "--no-autodoc",
            "--suite-root", str(tmp_path / "suite"),
            "--config-home", str(lay["config_home"]),
            "--claude-root", str(lay["claude_root"]),
            *extra,
        ])
    return rc, out.getvalue(), err.getvalue()


def test_missing_settings_file_refuses_at_preflight_with_nothing_written(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_SETTINGS_FILE", raising=False)
    lay = _layout(tmp_path)
    missing = tmp_path / "nope" / "settings.local.json"
    before = _tree(tmp_path)
    claude_before = _tree(lay["claude_root"])

    rc, out, err = _main(tmp_path, lay, "--settings-file", str(missing))

    assert rc == 1
    assert "1. Preflight" in out and "1. done." not in out, "must refuse IN step 1"
    assert "2. Config presence" not in out
    assert str(missing) in err
    assert "echo '{}'" in err and "--settings-file" in err
    assert "Run Claude Code once" not in err
    assert _tree(lay["claude_root"]) == claude_before, "Claude config dir was written to"
    assert _tree(tmp_path) == before, "something was written before the refusal"
    assert not lay["data_root"].exists()


def test_preflight_checks_the_env_resolved_path_step_9_would_use(tmp_path, monkeypatch):
    lay = _layout(tmp_path)
    from_env = tmp_path / "env-named" / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_FILE", str(from_env))

    rc, _out, err = _main(tmp_path, lay)

    assert rc == 1
    assert str(from_env) in err


def test_settings_file_flag_beats_env_in_the_resolver(tmp_path, monkeypatch):
    present = tmp_path / "present.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_FILE", str(tmp_path / "absent.json"))

    assert provision._settings_file(str(present)) == present


def _opts(tmp_path: Path, settings: Path) -> provision.Options:
    return provision.Options(
        dry_run=False, suite_root=tmp_path, config_home=tmp_path / "cfg",
        settings_file=settings, autodoc=False, autodoc_reason="forced by --no-autodoc",
        claude_root=tmp_path / "claude",
    )


def test_existing_settings_file_passes_preflight(tmp_path):
    settings = tmp_path / "settings.local.json"
    settings.write_text("{}", encoding="utf-8")

    provision._do_preflight(_opts(tmp_path, settings))


def test_directory_at_the_settings_path_refuses_in_both_steps(tmp_path):
    """A directory passes .exists(), then step 9's read_text raises after steps 2-7 wrote."""
    as_dir = tmp_path / "settings.local.json"
    as_dir.mkdir()
    opts = _opts(tmp_path, as_dir)

    with pytest.raises(RuntimeError, match="settings file not found"):
        provision._do_preflight(opts)
    with pytest.raises(RuntimeError, match="settings file not found"):
        provision._do_sessionstart_hook(opts)


def test_remedy_lines_name_a_non_default_path_not_the_default(tmp_path):
    custom = tmp_path / "elsewhere" / "custom.json"

    msg = provision._missing_settings_message(custom)

    assert f'mkdir -p "{custom.parent.as_posix()}"' in msg
    assert f"""[ -f "{custom.as_posix()}" ] || echo '{{}}' > "{custom.as_posix()}\"""" in msg
    assert "~/.claude/settings.local.json" not in msg


def test_remedy_lines_use_the_tilde_form_for_the_default_path():
    msg = provision._missing_settings_message(Path.home() / ".claude" / "settings.local.json")

    assert "    mkdir -p ~/.claude\n" in msg
    assert ("[ -f ~/.claude/settings.local.json ] || echo '{}' > "
            "~/.claude/settings.local.json") in msg


def test_step_9_backstop_uses_the_same_message(tmp_path):
    missing = tmp_path / "nope.json"
    opts = provision.Options(
        dry_run=False, suite_root=tmp_path, config_home=tmp_path / "cfg",
        settings_file=missing, autodoc=False, autodoc_reason="forced by --no-autodoc",
        claude_root=tmp_path / "claude",
    )

    with pytest.raises(RuntimeError) as step9:
        provision._do_sessionstart_hook(opts)
    with pytest.raises(RuntimeError) as step1:
        provision._do_preflight(opts)

    assert str(step9.value) == str(step1.value) == provision._missing_settings_message(missing)


def test_dry_run_still_exits_0_and_writes_nothing_with_the_file_missing(tmp_path, monkeypatch):
    """--dry-run is documented as print-the-plan-and-mutate-nothing; it runs no check, so a
    missing settings file does not fail it. The plan does name the path step 1 will check."""
    monkeypatch.delenv("CLAUDE_SETTINGS_FILE", raising=False)
    lay = _layout(tmp_path)
    missing = tmp_path / "nope" / "settings.local.json"
    before = _tree(tmp_path)

    rc, out, _err = _main(tmp_path, lay, "--dry-run", "--settings-file", str(missing))

    assert rc == 0
    assert _tree(tmp_path) == before
    step1 = out.split("  1. ", 1)[1].split("  2. ", 1)[0]
    assert f"settings file exists: {missing}" in step1


# (id, file bytes, fragments the refusal must carry). Each is a shape that passed the gh#743
# existence check and then crashed step 8 or 9's json.loads / setdefault after steps 2-7 wrote.
_BAD_SETTINGS = [
    ("bom", b"\xef\xbb\xbf{}", ["byte-order mark (BOM)", "UTF8Encoding($false)"]),
    ("empty", b"", ["settings file is empty", "echo '{}' >"]),
    ("whitespace-only", b" \r\n\t\n", ["settings file is whitespace only", "echo '{}' >"]),
    ("invalid-json", b'{\n  "a": 1,\n}', ["not valid JSON", "at line 3, column 1"]),
    ("top-level-list", b"[]", ["not a JSON object", "top level is an array"]),
    ("top-level-string", b'"x"', ["not a JSON object", "top level is a string"]),
    ("not-utf8", b'{"a": "\xff"}', ["not UTF-8 text", "byte 0xff at offset 7"]),
]
# Only an empty file may be answered with "write {}": every other shape can hold real settings.
# docs/INSTALL.md section 4 is about writing that {}, so only these refusals cite it.
_MAY_REPLACE = {"empty", "whitespace-only"}


@pytest.mark.parametrize("content, fragments", [(c, f) for _i, c, f in _BAD_SETTINGS],
                         ids=[i for i, _c, _f in _BAD_SETTINGS])
def test_unparseable_settings_file_refuses_at_preflight_with_nothing_written(
        tmp_path, monkeypatch, content, fragments):
    monkeypatch.delenv("CLAUDE_SETTINGS_FILE", raising=False)
    lay = _layout(tmp_path)
    settings = tmp_path / "settings.local.json"
    settings.write_bytes(content)
    before = _tree(tmp_path)
    claude_before = _tree(lay["claude_root"])

    rc, out, err = _main(tmp_path, lay, "--settings-file", str(settings))

    assert rc == 1
    assert "1. Preflight" in out and "1. done." not in out, "must refuse IN step 1"
    assert "2. Config presence" not in out
    assert "Traceback" not in err
    assert str(settings) in err
    for fragment in fragments:
        assert fragment in err
    assert "install/SKILL.md prerequisite 2" in err
    assert _tree(lay["claude_root"]) == claude_before, "Claude config dir was written to"
    assert _tree(tmp_path) == before, "something was written before the refusal"
    assert not lay["data_root"].exists()
    assert settings.read_bytes() == content, "the settings file itself was rewritten"


@pytest.mark.parametrize("name, content", [(i, c) for i, c, _f in _BAD_SETTINGS],
                         ids=[i for i, _c, _f in _BAD_SETTINGS])
def test_only_an_empty_file_is_told_to_become_braces(tmp_path, name, content):
    settings = tmp_path / "settings.local.json"
    settings.write_bytes(content)

    with pytest.raises(RuntimeError) as refused:
        provision._do_preflight(_opts(tmp_path, settings))

    assert ("echo '{}' >" in str(refused.value)) == (name in _MAY_REPLACE)
    assert ("docs/INSTALL.md section 4" in str(refused.value)) == (name in _MAY_REPLACE)


@pytest.mark.parametrize("content", [c for _i, c, _f in _BAD_SETTINGS],
                         ids=[i for i, _c, _f in _BAD_SETTINGS])
def test_step_9_backstop_gives_the_preflight_message_not_a_decode_error(tmp_path, content):
    """A --resume-from run skips step 1; step 9 must still refuse in words, not a traceback."""
    settings = tmp_path / "settings.local.json"
    settings.write_bytes(content)
    opts = _opts(tmp_path, settings)

    with pytest.raises(RuntimeError) as step9:
        provision._do_sessionstart_hook(opts)
    with pytest.raises(RuntimeError) as step1:
        provision._do_preflight(opts)

    assert not isinstance(step9.value, ValueError)  # JSONDecodeError / UnicodeDecodeError
    assert str(step9.value) == str(step1.value)
    assert settings.read_bytes() == content


def test_valid_object_with_surrounding_whitespace_passes_preflight(tmp_path):
    settings = tmp_path / "settings.local.json"
    settings.write_bytes(b'\r\n  {"env": {"A": "1"}}\n')

    provision._do_preflight(_opts(tmp_path, settings))


@pytest.mark.parametrize("content, ps_line", [
    (b"\xef\xbb\xbf{}", "    $f = {q}\n"),
    (b'{"a": "\xff"}', "    $f = {q}\n"),
    (b"", "    [System.IO.File]::WriteAllText({q}, '{{}}',"),
], ids=["bom", "not-utf8", "empty"])
def test_powershell_remedy_escapes_a_single_quote_in_the_path(tmp_path, content, ps_line):
    """A folder like O'Brien closed the single-quoted literal early; PowerShell doubles the '."""
    folder = tmp_path / "O'Brien"
    folder.mkdir()
    settings = folder / "settings.local.json"
    settings.write_bytes(content)

    with pytest.raises(RuntimeError) as refused:
        provision._do_preflight(_opts(tmp_path, settings))

    escaped = "'" + str(settings).replace("'", "''") + "'"
    assert "O''Brien" in escaped
    assert ps_line.format(q=escaped) in str(refused.value)


def test_bomless_non_utf8_remedy_names_1252_rather_than_claiming_a_lossless_resave(tmp_path):
    """ReadAllText($f) on BOM-less cp1252 decodes as UTF-8 and turns each such byte into U+FFFD."""
    settings = tmp_path / "settings.local.json"
    settings.write_bytes('{"name": "caf\u00e9"}'.encode("cp1252"))

    with pytest.raises(RuntimeError) as refused:
        provision._do_preflight(_opts(tmp_path, settings))

    msg = str(refused.value)
    assert "ReadAllText($f, [System.Text.Encoding]::GetEncoding(1252))" in msg
    assert "ASSUMES it is Windows-1252" in msg
    assert "ReadAllText($f)," not in msg
    assert "keeping its contents" not in msg


def test_utf16_remedy_keeps_the_plain_readalltext_form(tmp_path):
    settings = tmp_path / "settings.local.json"
    settings.write_bytes("{}".encode("utf-16"))

    with pytest.raises(RuntimeError) as refused:
        provision._do_preflight(_opts(tmp_path, settings))

    msg = str(refused.value)
    assert "UTF-16 byte-order mark" in msg
    assert "ReadAllText($f)," in msg
    assert "1252" not in msg


# --- gh#755: a JSON object whose "hooks" section steps 8 and 9 would crash on ---------------
#
# (id, settings object, key path the refusal must name, crash_driven). crash_driven=False marks
# a refusal no reader would crash on: it is there because Claude Code's hooks schema says so,
# not because step 8 or 9 dereferences it. The parity test below holds both labels to account.
_BAD_HOOKS = [
    ("hooks-array", {"hooks": []}, "hooks", True),
    ("hooks-null", {"hooks": None}, "hooks", True),
    ("hooks-string", {"hooks": "x"}, "hooks", True),
    ("sessionstart-object", {"hooks": {"SessionStart": {}}}, "hooks.SessionStart", True),
    ("pretooluse-string", {"hooks": {"PreToolUse": "Bash"}}, "hooks.PreToolUse", True),
    ("other-event-object", {"hooks": {"Stop": {"hooks": []}}}, "hooks.Stop", False),
    ("sessionstart-entry-string",
     {"hooks": {"SessionStart": [{"hooks": []}, "x"]}}, "hooks.SessionStart[1]", True),
    ("pretooluse-entry-array",
     {"hooks": {"PreToolUse": [{"matcher": "Edit"}, []]}}, "hooks.PreToolUse[1]", True),
    ("sessionstart-block-hooks-null",
     {"hooks": {"SessionStart": [{"hooks": None}]}}, "hooks.SessionStart[0].hooks", True),
    ("bash-block-hooks-object",
     {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": {"a": 1}}]}},
     "hooks.PreToolUse[0].hooks", True),
    # Step 8 only reads a non-Bash PreToolUse block's matcher, so its "hooks" is schema-only.
    ("edit-block-hooks-string",
     {"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": "x"}]}},
     "hooks.PreToolUse[0].hooks", False),
    ("sessionstart-hook-string",
     {"hooks": {"SessionStart": [{"hooks": [{"command": "a"}, "b"]}]}},
     "hooks.SessionStart[0].hooks[1]", True),
    ("bash-hook-number",
     {"hooks": {"PreToolUse": [{"matcher": "Edit"}, {"matcher": "Bash", "hooks": [{}, 3]}]}},
     "hooks.PreToolUse[1].hooks[1]", True),
    ("bash-block-without-hooks",
     {"hooks": {"PreToolUse": [{"matcher": "Write"}, {"matcher": "Bash"}]}},
     "hooks.PreToolUse[1]", True),
    # bootstrap builds a set of every "if" in the first Bash block: unhashable ones crash it.
    ("bash-if-array",
     {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"if": ["Bash(ls*)"]}]}]}},
     "hooks.PreToolUse[0].hooks[0].if", True),
    ("bash-if-object",
     {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"if": {}}]}]}},
     "hooks.PreToolUse[0].hooks[0].if", True),
    # Schema-only: entries step 8 never dereferences (non-Bash hooks, a later Bash block's
    # hooks, and an entry past where every next(...) scan has already stopped).
    ("edit-hook-number",
     {"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [3]}]}},
     "hooks.PreToolUse[0].hooks[0]", False),
    ("second-bash-hooks-string",
     {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []},
                               {"matcher": "Bash", "hooks": "x"}]}},
     "hooks.PreToolUse[1].hooks", False),
    ("entry-past-every-scan",
     {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}, {"matcher": "Edit"},
                               {"matcher": "Write"}, {"matcher": "Agent"}, "x"]}},
     "hooks.PreToolUse[4]", False),
]

_REALISTIC = {
    "env": {"A": "1"},
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "x", "if": "Bash(ls*)"}]},
            {"matcher": "Edit"},
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}],
    },
}
_GOOD_HOOKS = [
    ("absent", {"env": {"A": "1"}}),
    ("bash-if-string",
     {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"if": "Bash(git *)"}, {}]}]}}),
    ("empty-object", {"hooks": {}}),
    ("empty-event-lists", {"hooks": {"SessionStart": [], "PreToolUse": []}}),
    ("realistic", _REALISTIC),
    ("unrelated-event-only", {"hooks": {"Stop": [{"hooks": [{"command": "x"}]}]}}),
    ("extra-keys-and-odd-matcher",
     {"hooks": {"PreToolUse": [{"matcher": 7, "note": "kept"}, {"matcher": "Bash", "hooks": []}]}}),
    # Only the first Bash block is indexed by bootstrap; a later one without "hooks" is unread.
    ("second-bash-block-without-hooks",
     {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}, {"matcher": "Bash"}]}}),
]


def _write_json(path: Path, obj) -> bytes:
    content = json.dumps(obj).encode("utf-8")
    path.write_bytes(content)
    return content


@pytest.mark.parametrize("obj, where", [(o, w) for _i, o, w, _c in _BAD_HOOKS],
                         ids=[i for i, _o, _w, _c in _BAD_HOOKS])
def test_malformed_hooks_refuses_at_preflight_with_nothing_written(
        tmp_path, monkeypatch, obj, where):
    monkeypatch.delenv("CLAUDE_SETTINGS_FILE", raising=False)
    lay = _layout(tmp_path)
    settings = tmp_path / "settings.local.json"
    content = _write_json(settings, obj)
    before = _tree(tmp_path)
    claude_before = _tree(lay["claude_root"])

    rc, out, err = _main(tmp_path, lay, "--settings-file", str(settings))

    assert rc == 1
    assert "1. Preflight" in out and "1. done." not in out, "must refuse IN step 1"
    assert "2. Config presence" not in out
    assert "Traceback" not in err
    assert f'settings file has a malformed "hooks" section: {settings}' in err
    assert f"\n{where} " in err, f"refusal does not name {where}"
    assert "install/SKILL.md prerequisite 2" in err
    assert _tree(lay["claude_root"]) == claude_before, "Claude config dir was written to"
    assert _tree(tmp_path) == before, "something was written before the refusal"
    assert not lay["data_root"].exists()
    assert settings.read_bytes() == content, "the settings file itself was rewritten"


@pytest.mark.parametrize("obj, kind", [
    ({"hooks": []}, "an array"), ({"hooks": None}, "null"), ({"hooks": "x"}, "a string"),
    ({"hooks": {"SessionStart": {}}}, "an object"),
], ids=["array", "null", "string", "object"])
def test_malformed_hooks_refusal_names_the_json_kind_found(tmp_path, obj, kind):
    settings = tmp_path / "settings.local.json"
    _write_json(settings, obj)

    with pytest.raises(RuntimeError) as refused:
        provision._do_preflight(_opts(tmp_path, settings))

    assert f" is {kind}; it must be " in str(refused.value)


@pytest.mark.parametrize("name, obj, crash_driven", [(i, o, c) for i, o, _w, c in _BAD_HOOKS],
                         ids=[i for i, _o, _w, _c in _BAD_HOOKS])
def test_malformed_hooks_refusal_claims_only_reads_that_happen(tmp_path, name, obj, crash_driven):
    """Schema first; a step is named only where it dereferences the key, and step 8 always
    with its --no-auto-allow escape, so the message stays true when that flag is passed."""
    settings = tmp_path / "settings.local.json"
    _write_json(settings, obj)

    with pytest.raises(RuntimeError) as refused:
        provision._do_preflight(_opts(tmp_path, settings))

    msg = str(refused.value)
    if not name.startswith("hooks-"):  # the top-level "hooks" kind, not a schema row
        assert "Claude Code's hooks schema requires" in msg
    if not crash_driven:
        assert "step 8" not in msg.lower() and "step 9" not in msg.lower()
    else:  # a crash-driven row names the step that would have crashed
        assert "step 8" in msg.lower() or "step 9" in msg.lower()
    assert msg.lower().count("step 8") == msg.count("unless --no-auto-allow is passed")


@pytest.mark.parametrize("obj", [o for _i, o, _w, _c in _BAD_HOOKS],
                         ids=[i for i, _o, _w, _c in _BAD_HOOKS])
def test_step_9_backstop_refuses_malformed_hooks_with_the_preflight_message(tmp_path, obj):
    settings = tmp_path / "settings.local.json"
    content = _write_json(settings, obj)
    opts = _opts(tmp_path, settings)

    with pytest.raises(RuntimeError) as step9:
        provision._do_sessionstart_hook(opts)
    with pytest.raises(RuntimeError) as step1:
        provision._do_preflight(opts)

    assert str(step9.value) == str(step1.value)
    assert settings.read_bytes() == content


@pytest.mark.parametrize("obj", [o for _i, o in _GOOD_HOOKS], ids=[i for i, _o in _GOOD_HOOKS])
def test_well_formed_hooks_pass_preflight(tmp_path, obj):
    settings = tmp_path / "settings.local.json"
    _write_json(settings, obj)

    provision._do_preflight(_opts(tmp_path, settings))


# --- parity: the check is exactly as strict as the two readers, plus the schema-only rows ---

def _load_bootstrap():
    path = Path(__file__).resolve().parent.parent / provision.BOOTSTRAP_REL
    spec = importlib.util.spec_from_file_location("_af_bootstrap_gh755", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _step_8(obj) -> None:
    _load_bootstrap().add_hooks(copy.deepcopy(obj))


def _step_9_unchecked(tmp_path: Path, obj) -> None:
    """Step 9's real code with the shape check switched off, so it meets the raw object."""
    settings = tmp_path / "unchecked.json"
    _write_json(settings, obj)
    with mock.patch.object(provision, "_check_hooks_shape", lambda *_a: None):
        provision._do_sessionstart_hook(_opts(tmp_path, settings))


@pytest.mark.parametrize("obj", [o for _i, o in _GOOD_HOOKS], ids=[i for i, _o in _GOOD_HOOKS])
def test_every_accepted_shape_runs_through_steps_8_and_9(tmp_path, monkeypatch, obj):
    """In install order: step 8 mutates and saves, then the real step 9 (check on) reads that."""
    monkeypatch.delenv("AGENTFLOW_EXTRA_AUTO_ALLOW", raising=False)
    settings = tmp_path / "settings.local.json"
    after_8, _n = _load_bootstrap().add_hooks(copy.deepcopy(obj))
    _write_json(settings, after_8)

    provision._do_sessionstart_hook(_opts(tmp_path, settings))

    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert any(b.get("matcher") == "Bash" for b in saved["hooks"]["PreToolUse"])
    assert saved["hooks"]["SessionStart"], "step 9 added no SessionStart entry"


@pytest.mark.parametrize("obj, crash_driven", [(o, c) for _i, o, _w, c in _BAD_HOOKS],
                         ids=[i for i, _o, _w, _c in _BAD_HOOKS])
def test_every_refused_shape_would_crash_a_reader_unless_marked_schema_only(
        tmp_path, monkeypatch, obj, crash_driven):
    monkeypatch.delenv("AGENTFLOW_EXTRA_AUTO_ALLOW", raising=False)
    crashed = []
    for name, reader in (("step 8", lambda: _step_8(obj)),
                         ("step 9", lambda: _step_9_unchecked(tmp_path, obj))):
        try:
            reader()
        except (AttributeError, KeyError, TypeError) as e:
            crashed.append(f"{name}: {type(e).__name__}")

    if crash_driven:
        assert crashed, "refused, but neither reader crashes on it: the check is too strict"
    else:
        assert not crashed, f"marked schema-only but a reader crashes: {crashed}"
