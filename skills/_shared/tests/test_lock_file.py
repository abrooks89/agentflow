"""lock_file: exclusive stale-lock steal (gh#910) and ownership-checked, contention-tolerant
release (gh#910, gh#912).

The race test uses real processes: the bug it pins (every contender "winning" a dead-PID lock)
only exists across processes, so a mock would prove nothing.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import agentflow
from agentflow import lock_file

_SHARED = str(Path(agentflow.__file__).resolve().parent.parent)

_WORKER = r"""
import os, sys, time, uuid
from pathlib import Path
from agentflow import lock_file

gate, lock, counter, log, rounds, mode, dead_pid = sys.argv[1:8]
while not os.path.exists(gate):
    time.sleep(0.001)
for _ in range(int(rounds)):
    deadline = time.monotonic() + 20
    while True:
        try:
            handle = lock_file.acquire(lock)
            break
        except (lock_file.LockHeldError, PermissionError, FileNotFoundError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.005)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"E {os.getpid()}\n")
    n = int(Path(counter).read_text(encoding="utf-8"))
    time.sleep(0.02)
    Path(counter).write_text(str(n + 1), encoding="utf-8")
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"X {os.getpid()}\n")
    if mode == "crash":
        # Die holding it, as a crashed holder does: a dead PID and a nonce nobody else has,
        # so the next acquire must go through the steal path.
        Path(lock).write_text(
            f"PID={dead_pid}\nNONCE={uuid.uuid4().hex}\n", encoding="utf-8", newline="\n"
        )
    else:
        handle.release()
"""


@pytest.fixture
def make_dead_pid():
    """Mint PIDs that were alive a moment ago and are not now - the shape a crashed holder leaves.

    Each child's Popen is held until the test ends. Dropping it closes the process handle, and
    Windows may then give the PID to a new process, which the probe reads as a live holder.
    """
    held = []  # keeps each Popen, and so its handle, alive; never read

    def make() -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        held.append(proc)
        return proc.pid

    yield make
    held.clear()


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.mark.parametrize(
    ("mode", "rounds"),
    [("release", 2), ("crash", 3)],
    ids=["holders-release", "every-holder-crashes"],
)
def test_concurrent_stealers_of_a_dead_lock_enter_one_at_a_time(
    tmp_path, make_dead_pid, mode, rounds
):
    """`crash` makes every acquisition after the first a steal, not just the opening one."""
    workers = 6
    dead_pid = make_dead_pid()
    lock = tmp_path / "x.lock"
    counter = tmp_path / "counter"
    log = tmp_path / "log"
    gate = tmp_path / "gate"
    _write(lock, f"PID={dead_pid}\nSPAWNED=2020-01-01T00:00:00+00:00\n")
    _write(counter, "0")
    env = {**os.environ, "PYTHONPATH": _SHARED + os.pathsep + os.environ.get("PYTHONPATH", "")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(gate), str(lock), str(counter), str(log),
             str(rounds), mode, str(dead_pid)],
            env=env,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(workers)
    ]
    time.sleep(0.5)  # let every worker reach the gate, so they all meet the stale lock at once
    gate.touch()
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err

    entries = log.read_text(encoding="utf-8").split()
    pairs = list(zip(entries[0::2], entries[1::2], strict=True))
    events = [pairs[i : i + 2] for i in range(0, len(pairs), 2)]
    assert len(events) == workers * rounds
    for (enter, enter_pid), (leave, leave_pid) in events:
        assert (enter, leave) == ("E", "X"), f"critical sections overlapped: {entries}"
        assert enter_pid == leave_pid, f"critical sections overlapped: {entries}"
    assert counter.read_text(encoding="utf-8") == str(workers * rounds)
    if mode == "crash":
        assert lock_file.read_lock(lock).pid == dead_pid
    else:
        assert not lock.exists()


def test_a_dead_holders_legacy_lock_without_a_nonce_is_still_stolen(tmp_path, make_dead_pid):
    lock = tmp_path / "x.lock"
    _write(lock, f"PID={make_dead_pid()}\nSPAWNED=2020-01-01T00:00:00+00:00\n")
    handle = lock_file.acquire(lock)
    info = lock_file.read_lock(lock)
    assert info.pid == os.getpid()
    assert info.nonce == handle.nonce
    assert handle.release() is True
    assert not lock.exists()


def test_a_live_holder_is_not_stolen_from(tmp_path):
    lock = tmp_path / "x.lock"
    lock_file.acquire(lock)
    with pytest.raises(lock_file.LockHeldError):
        lock_file.acquire(lock)


def test_a_fresh_torn_lock_is_treated_as_held(tmp_path):
    lock = tmp_path / "x.lock"
    _write(lock, "PID=")
    with pytest.raises(lock_file.LockHeldError):
        lock_file.acquire(lock)
    assert lock.read_text(encoding="utf-8") == "PID="


def test_a_steal_in_progress_makes_others_wait_and_leaves_the_lock_alone(tmp_path, make_dead_pid):
    lock = tmp_path / "x.lock"
    stale = f"PID={make_dead_pid()}\n"
    _write(lock, stale)
    Path(str(lock) + ".steal").touch()
    with pytest.raises(lock_file.LockHeldError, match="being stolen"):
        lock_file.acquire(lock)
    assert lock.read_text(encoding="utf-8") == stale


def test_a_steal_guard_left_by_a_crashed_stealer_ages_out(tmp_path, make_dead_pid):
    lock = tmp_path / "x.lock"
    _write(lock, f"PID={make_dead_pid()}\n")
    guard = Path(str(lock) + ".steal")
    guard.touch()
    old = time.time() - lock_file._STEAL_GUARD_STALE - 1
    os.utime(guard, (old, old))
    with pytest.raises(lock_file.LockHeldError):
        lock_file.acquire(lock)  # this pass clears the guard
    handle = lock_file.acquire(lock)
    assert lock_file.read_lock(lock).nonce == handle.nonce
    assert not guard.exists()


def test_a_stealer_whose_guard_was_replaced_backs_off(tmp_path, make_dead_pid, monkeypatch):
    """A slow clearer of an aged-out guard can delete a fresh one, letting a second stealer
    create its own. The first must notice before overwriting, and leave the new guard alone."""
    lock = tmp_path / "x.lock"
    stale = f"PID={make_dead_pid()}\n"
    _write(lock, stale)
    guard = Path(str(lock) + ".steal")
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == lock and guard.exists():
            _write(guard, "another-stealers-nonce")  # the guard swap, mid-steal
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(lock_file.LockHeldError, match="guard was replaced"):
        lock_file.acquire(lock)
    monkeypatch.undo()
    assert lock.read_text(encoding="utf-8") == stale
    assert guard.read_text(encoding="utf-8") == "another-stealers-nonce"


def test_release_leaves_a_lock_that_now_carries_another_holders_nonce(tmp_path, capsys):
    """Same PID, different nonce: the shape of a reused PID or a thief that judged us dead."""
    lock = tmp_path / "x.lock"
    handle = lock_file.acquire(lock)
    other = f"PID={os.getpid()}\nSPAWNED=2020-01-01T00:00:00+00:00\nNONCE=someone-else\n"
    _write(lock, other)
    assert handle.release() is False
    assert lock.read_text(encoding="utf-8") == other
    assert "the lock was no longer ours" in capsys.readouterr().err


def test_release_of_an_already_gone_lock_is_fine(tmp_path):
    lock = tmp_path / "x.lock"
    handle = lock_file.acquire(lock)
    lock.unlink()
    assert handle.release() is True


_REAL_UNLINK = Path.unlink


def _deny_unlink(monkeypatch, lock: Path, times: int | None) -> dict:
    """Make unlink of `lock` raise WinError 32 `times` times (None = always), as a peer's open
    handle does on Windows."""
    calls = {"n": 0}

    def fake(self, *args, **kwargs):
        if self == lock and (times is None or calls["n"] < times):
            calls["n"] += 1
            raise PermissionError(32, "The process cannot access the file", str(self))
        return _REAL_UNLINK(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake)
    return calls


def test_release_retries_a_transient_sharing_violation(tmp_path, monkeypatch):
    lock = tmp_path / "x.lock"
    handle = lock_file.acquire(lock)
    calls = _deny_unlink(monkeypatch, lock, 3)
    assert handle.release() is True
    assert calls["n"] == 3
    assert not lock.exists()


def test_a_persistent_sharing_violation_warns_and_does_not_raise(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lock_file, "_RELEASE_RETRY_TIMEOUT", 0.05)
    lock = tmp_path / "x.lock"
    handle = lock_file.acquire(lock)
    _deny_unlink(monkeypatch, lock, None)
    assert handle.release() is False
    err = capsys.readouterr().err
    assert "lock leaked" in err
    assert str(lock) in err


_windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the Ctrl-C probe hazard is Windows-only")


@_windows_only
def test_probing_a_live_group_leader_does_not_signal_it(monkeypatch):
    """gh#927: os.kill(pid, 0) is CTRL_C_EVENT on Windows and killed a live holder that led its
    own process group. The probe must answer without touching the process."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        def _no_kill(*_a, **_k):
            raise AssertionError("the Windows probe must not call os.kill")

        monkeypatch.setattr(os, "kill", _no_kill)
        assert lock_file._is_pid_alive(child.pid) is True
        time.sleep(0.5)
        assert child.poll() is None, "probing the holder terminated it"
    finally:
        monkeypatch.undo()
        child.kill()
        child.wait()


@_windows_only
def test_an_exited_process_reads_dead_while_a_handle_to_it_is_still_open():
    """Popen keeps its process handle open after exit, which is exactly the case where an
    open-succeeds-so-alive probe lies."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert lock_file._is_pid_alive(child.pid) is False
