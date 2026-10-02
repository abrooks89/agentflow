"""gh#582: a contention WinError 5 during the edit-lock acquire is retried, not reported as a
permanent permission failure. Deterministic: Path.mkdir is patched for the lock path only."""
from __future__ import annotations

import os
import pathlib
import time

import pytest

from agentflow.registry import LockHeldError, acquire_edit_lock

_REAL_MKDIR = pathlib.Path.mkdir


def _deny_lock_mkdir(monkeypatch, times, *, create_before_denying=False):
    """Make mkdir of the registry lock dir raise PermissionError `times` times (None = always)."""
    calls = {"n": 0}

    def fake(self, *args, **kwargs):
        if self.name.endswith("-registry.lock") and (times is None or calls["n"] < times):
            calls["n"] += 1
            if create_before_denying and not self.exists():
                _REAL_MKDIR(self)
            raise PermissionError(5, "Access is denied", str(self))
        return _REAL_MKDIR(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", fake)
    return calls


def test_transient_access_denied_is_retried_then_acquired(tmp_path, monkeypatch):
    calls = _deny_lock_mkdir(monkeypatch, 3)
    lock = acquire_edit_lock(tmp_path / "locks", "demo", "lead-dev", timeout=5.0, poll=0.01)
    assert calls["n"] == 3
    assert lock.path.is_dir()
    lock.release()


def test_denied_while_lock_dir_exists_is_contention_not_permanent(tmp_path, monkeypatch):
    """Past the deadline with the lock dir present, the answer is LockHeldError (exit 3)."""
    _deny_lock_mkdir(monkeypatch, None, create_before_denying=True)
    with pytest.raises(LockHeldError):
        acquire_edit_lock(tmp_path / "locks", "demo", "lead-dev", timeout=0.1, poll=0.01)


def test_denied_with_no_lock_dir_stays_a_permission_error(tmp_path, monkeypatch):
    """A path denied for the whole window with nothing in the way is a real permission problem."""
    _deny_lock_mkdir(monkeypatch, None)
    with pytest.raises(PermissionError):
        acquire_edit_lock(tmp_path / "locks", "demo", "lead-dev", timeout=0.1, poll=0.01)


def test_steal_path_tolerates_access_denied_on_re_mkdir(tmp_path, monkeypatch):
    """A stale lock is broken, and the re-mkdir that follows hits the same transient."""
    lock_dir = tmp_path / "locks"
    first = acquire_edit_lock(lock_dir, "demo", "jr-dev-1", timeout=1.0)
    old = time.time() - 600
    os.utime(first.path, (old, old))
    real = pathlib.Path.mkdir
    state = {"lock_calls": 0}

    def fake(self, *args, **kwargs):
        if self.name.endswith("-registry.lock"):
            state["lock_calls"] += 1
            if state["lock_calls"] == 2:  # 1 = sees the stale dir; 2 = the re-mkdir after the break
                raise PermissionError(5, "Access is denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", fake)
    lock = acquire_edit_lock(lock_dir, "demo", "lead-dev", timeout=5.0, poll=0.01)
    assert state["lock_calls"] >= 3
    assert lock.path.is_dir()
    lock.release()
