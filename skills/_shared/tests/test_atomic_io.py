"""``atomic_io.ReplaceRetryExhausted``: the re-raise names each fact once and keeps every attribute.

The re-raise used to pass ``str(e)`` as the new error's strerror. ``str(e)`` already holds the
errno and the paths, and OSError's own ``__str__`` adds the errno in front and the filename
behind, so the message an operator read named each of them twice. These tests drive the real
retry loop, never a stub of it. ``os.replace`` is patched through ``atomic_io.os``, which is the
process-global ``os`` module, so the patch applies to every caller for the test's duration and is
undone by ``monkeypatch`` afterwards.
"""
from __future__ import annotations

import json
import os

import pytest

from agentflow import atomic_io


def _exhaust(monkeypatch, tmp_path, exc):
    dest = tmp_path / "dest.md"
    dest.write_text("x", encoding="utf-8")
    src = tmp_path / "src.tmp"
    src.write_text("y", encoding="utf-8")

    def always_denied(a, b):
        raise exc(str(a), str(b))

    monkeypatch.setattr(atomic_io.os, "replace", always_denied)
    with pytest.raises(atomic_io.ReplaceRetryExhausted) as caught:
        atomic_io.replace_retry(src, dest, timeout=0.01, max_delay=0.001)
    return caught.value, src, dest


def test_the_message_names_the_errno_and_the_path_once(tmp_path, monkeypatch):
    err, _src, dest = _exhaust(
        monkeypatch, tmp_path, lambda a, b: PermissionError(13, "held by a reader", b))
    msg = str(err)
    # OSError.__str__ quotes paths with repr(), so count the quoted spelling.
    assert msg.count("Errno 13") == 1, msg
    assert msg.count(repr(str(dest))) == 1, msg
    assert msg.count("held by a reader") == 1, msg
    assert "WinError" not in msg, msg


def test_both_paths_of_a_real_replace_failure_appear_once_each(tmp_path, monkeypatch):
    """``os.replace`` reports src as filename and dest as filename2; neither may double or drop."""
    err, src, dest = _exhaust(
        monkeypatch, tmp_path, lambda a, b: PermissionError(13, "held by a reader", a, None, b))
    msg = str(err)
    assert msg.count(repr(str(src))) == 1, msg
    assert msg.count(repr(str(dest))) == 1, msg
    assert msg.count("Errno 13") == 1, msg
    assert "WinError" not in msg, msg


def test_the_reraise_keeps_every_attribute_and_its_type(tmp_path, monkeypatch):
    err, src, dest = _exhaust(
        monkeypatch, tmp_path, lambda a, b: PermissionError(13, "held by a reader", a, None, b))
    assert isinstance(err, PermissionError)
    assert isinstance(err, OSError)
    assert err.errno == 13
    assert err.strerror == "held by a reader"
    assert err.filename == str(src)
    assert err.filename2 == str(dest)
    assert isinstance(err.__cause__, PermissionError), "the original stays chained"


def test_a_bare_message_permission_error_keeps_its_text(tmp_path, monkeypatch):
    """``PermissionError("text")`` has no strerror; the re-raise must not print ``None`` for it."""
    err, _src, _dest = _exhaust(
        monkeypatch, tmp_path, lambda a, b: PermissionError("held by a reader"))
    msg = str(err)
    assert msg.count("held by a reader") == 1, msg
    assert "None None" not in msg, msg
    assert err.strerror == "held by a reader"


def test_a_windows_error_code_is_kept_and_named_once(tmp_path, monkeypatch):
    """A real Windows denial carries winerror 5 beside errno 13.

    Portable by construction: on Windows the winerror argument is kept and the re-raise takes
    the five-argument form; elsewhere OSError ignores it and has no winerror attribute, so the
    same input takes the three-argument form. Either way the code and each path appear once.
    """
    err, src, dest = _exhaust(
        monkeypatch, tmp_path, lambda a, b: PermissionError(13, "Access is denied", a, 5, b))
    msg = str(err)
    assert msg.count("Errno 13") + msg.count("WinError 5") == 1, msg
    assert msg.count(repr(str(src))) == 1, msg
    assert msg.count(repr(str(dest))) == 1, msg
    assert "WinError None" not in msg, msg
    assert err.errno == 13
    assert err.filename == str(src) and err.filename2 == str(dest)
    if os.name == "nt":
        assert err.winerror == 5, "the Windows error code must survive the re-raise"
        assert "WinError 5" in msg, msg


def test_a_directly_built_instance_still_formats_once():
    e = atomic_io.ReplaceRetryExhausted(13, "denied", "some/path.md")
    assert str(e).count("Errno 13") == 1
    assert str(e).count("some/path.md") == 1
    assert e.filename == "some/path.md"


# --------------------------------------------------------------------------- #
# write_settings_json: the one spelling of a settings file, shared by both its writers
# --------------------------------------------------------------------------- #
def test_settings_json_keeps_non_ascii_as_utf8_with_LF_and_no_trailing_newline(tmp_path):
    path = tmp_path / "settings.local.json"
    atomic_io.write_settings_json(path, {"env": {"NOTE": "Zo\u00eb \u2713"}})
    raw = path.read_bytes()
    assert raw == '{\n  "env": {\n    "NOTE": "Zo\u00eb \u2713"\n  }\n}'.encode("utf-8")
    assert b"\r\n" not in raw and not raw.endswith(b"\n")


def test_settings_json_writes_a_lone_surrogate_in_its_escaped_form(tmp_path):
    path = tmp_path / "settings.local.json"
    atomic_io.write_settings_json(path, {"env": {"NOTE": "\ud800 \u00eb"}})
    raw = path.read_bytes()
    # The whole document falls back to escapes: the only lossless spelling of the surrogate.
    assert raw == b'{\n  "env": {\n    "NOTE": "\\ud800 \\u00eb"\n  }\n}'
    assert json.loads(raw.decode("utf-8"))["env"]["NOTE"] == "\ud800 \u00eb"


def test_settings_json_writes_through_a_symlink_to_its_target(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"this platform cannot create a symlink here: {e}")
    atomic_io.write_settings_json(link, {"a": 1})
    assert link.is_symlink(), "the link was replaced by a regular file"
    assert target.read_bytes() == b'{\n  "a": 1\n}'
    assert sorted(x.name for x in tmp_path.iterdir()) == ["link.json", "real.json"]


# --------------------------------------------------------------------------- #
# gh#1168: mkstemp's 0600 temp must not become the target's mode on POSIX
# --------------------------------------------------------------------------- #
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


def _mode(path) -> int:
    return path.stat().st_mode & 0o7777


@posix_only
def test_an_existing_file_keeps_its_mode(tmp_path):
    path = tmp_path / "registry.md"
    path.write_text("old", encoding="utf-8")
    os.chmod(path, 0o664)
    atomic_io.atomic_write(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
    assert _mode(path) == 0o664, oct(_mode(path))


@posix_only
def test_a_new_file_gets_the_umask_default(tmp_path):
    old = os.umask(0o022)
    try:
        path = tmp_path / "status.md"
        atomic_io.atomic_write(path, "x")
        assert _mode(path) == 0o644, oct(_mode(path))
        os.umask(0o002)
        other = tmp_path / "other.md"
        atomic_io.atomic_write(other, "x")
        assert _mode(other) == 0o664, oct(_mode(other))
    finally:
        os.umask(old)


@posix_only
def test_special_bits_are_not_carried_onto_the_new_inode(tmp_path):
    path = tmp_path / "board.md"
    path.write_text("x", encoding="utf-8")
    os.chmod(path, 0o1664)  # sticky + 0664
    atomic_io.atomic_write(path, "y")
    assert _mode(path) == 0o664, oct(_mode(path))


@posix_only
def test_settings_json_ends_owner_only_even_over_a_wider_file(tmp_path):
    path = tmp_path / "settings.local.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)
    atomic_io.write_settings_json(path, {"a": 1})
    assert _mode(path) == 0o600, oct(_mode(path))


@posix_only
def test_an_explicit_mode_wins_over_the_existing_one(tmp_path):
    path = tmp_path / "f.md"
    path.write_text("x", encoding="utf-8")
    os.chmod(path, 0o664)
    atomic_io.atomic_write(path, "y", mode=0o640)
    assert _mode(path) == 0o640, oct(_mode(path))


@posix_only
def test_the_umask_read_leaves_the_umask_unchanged():
    before = os.umask(0o027)
    try:
        assert atomic_io._current_umask() == 0o027
        assert atomic_io._current_umask() == 0o027
    finally:
        os.umask(before)


def test_atomic_write_does_not_chmod_on_windows(tmp_path, monkeypatch):
    """Windows has no POSIX bits to carry, and os.fchmod is not available there."""
    calls = []
    monkeypatch.setattr(atomic_io.os, "name", "nt")
    monkeypatch.setattr(atomic_io.os, "fchmod", lambda *a: calls.append(a), raising=False)
    path = tmp_path / "w.md"
    atomic_io.atomic_write(path, "x")
    assert path.read_text(encoding="utf-8") == "x"
    assert calls == []
