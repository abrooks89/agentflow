"""Lock-file helpers for the orchestrator: O_EXCL acquire, orphan detection.

Body format (one ``KEY=value`` per line; readers ignore keys they do not know)::

    PID=<holder pid>
    SPAWNED=<UTC ISO 8601>
    NONCE=<uuid4 hex>

``NONCE`` identifies one acquire, so a release only ever removes the lock it took, even when the
OS has since handed the same PID to another holder. Bodies written before ``NONCE`` existed
still parse; they simply carry no nonce.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import sys
import time
import uuid


@dataclass
class LockInfo:
    pid: int
    spawned: datetime | None
    nonce: str | None = None


class LockHeldError(RuntimeError):
    """Raised when a lock is held by an alive process."""


# Grace window for a torn/empty lock (creator mid-write between the O_EXCL
# create and the PID write). Within it we treat the lock as held; past it we
# assume the creator crashed mid-create and allow a steal so the lock self-heals.
_TORN_LOCK_GRACE = 5.0  # seconds

# A steal guard is held only for one read + one write. One older than this was left by a
# stealer that crashed inside that window, so it may be cleared.
_STEAL_GUARD_STALE = 10.0  # seconds

# How long release() keeps retrying an unlink that Windows refuses because another process
# has the lock file open (WinError 32). Readers close within milliseconds.
_RELEASE_RETRY_TIMEOUT = 5.0  # seconds
_RELEASE_RETRY_POLL = 0.01

# Bound on the create -> read -> steal loop in acquire(); each pass is one lost race.
_ACQUIRE_ATTEMPTS = 50


class LockHandle:
    def __init__(self, path: Path, nonce: str | None = None):
        self.path = path
        self.nonce = nonce

    def release(self) -> bool:
        """Remove the lock iff it is still this handle's; True only for a clean release.

        False means the lock was no longer ours (someone took it while we held it) or could
        not be removed and leaked; either way stderr says which.

        Ownership is checked by nonce, so a holder whose lock was stolen (it looked dead) cannot
        delete the thief's lock. The read and the unlink are not one atomic step, but nothing
        else rewrites a live holder's lock: only a stealer that believed this PID dead could,
        and that is the window this check narrows rather than closes.

        A Windows sharing violation (another process has the file open) is retried briefly. If
        it persists, the lock is reported on stderr and left behind instead of raised: the
        caller's critical section has already run, and once this process exits its dead PID
        makes the lock stealable.
        """
        deadline = time.monotonic() + _RELEASE_RETRY_TIMEOUT
        while True:
            try:
                if self.nonce is not None:
                    current = read_lock(self.path)
                    if current.nonce != self.nonce:
                        print(
                            f"lock_file: not releasing {self.path}: the lock was no longer "
                            f"ours (now held by PID {current.pid}); critical sections may "
                            "have overlapped",
                            file=sys.stderr,
                        )
                        return False
                self.path.unlink()
                return True
            except FileNotFoundError:
                return True
            except PermissionError as exc:
                if time.monotonic() >= deadline:
                    print(
                        f"lock_file: could not release {self.path} ({exc}); lock leaked, "
                        "it becomes stealable once this process exits",
                        file=sys.stderr,
                    )
                    return False
                time.sleep(_RELEASE_RETRY_POLL)


def _is_pid_alive(pid: int) -> bool:
    """Return True iff a process with this PID is currently alive on this host."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Never os.kill here: on Windows signal 0 is CTRL_C_EVENT, so the "probe" interrupts the
        # holder's whole console process group - it killed a live holder in the E-0981 review.
        return _is_pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, we just can't signal it
    except OSError:
        return False


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _is_pid_alive_windows(pid: int) -> bool:
    """Non-signalling probe: open the process and ask for its exit code.

    An exited process keeps a real exit code while handles to it stay open, so this reads it
    dead where a bare OpenProcess success would read it alive. A process that genuinely exited
    with code 259 reads alive; that errs toward not stealing, the same side PermissionError takes.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists under another account; anything else is gone.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell: do not hand the lock away
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _parse(text: str) -> LockInfo:
    pid = 0
    spawned: datetime | None = None
    nonce: str | None = None
    for line in text.splitlines():
        if line.startswith("PID="):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = 0  # torn mid-write; the torn-lock grace applies
        elif line.startswith("SPAWNED="):
            try:
                spawned = datetime.fromisoformat(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("NONCE="):
            nonce = line.split("=", 1)[1].strip() or None
    return LockInfo(pid=pid, spawned=spawned, nonce=nonce)


def read_lock(path: Path) -> LockInfo:
    return _parse(Path(path).read_text(encoding="utf-8"))


def _is_stealable(path: Path, info: LockInfo) -> bool:
    """A lock is stealable when its holder is dead, or it has sat torn past the grace window."""
    if info.pid > 0:
        return not _is_pid_alive(info.pid)
    # pid<=0 means a torn/empty lock: a creator is mid-write between its O_EXCL create and its
    # PID write. Stealing it while fresh would double-hold.
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        age = 0.0
    return age >= _TORN_LOCK_GRACE


def _steal(path: Path, observed: str, body: str) -> bool:
    """Take over the stale lock at ``path`` exclusively; True iff this caller now holds it.

    Raises LockHeldError while another contender is inside the steal guard.

    Every stealer must first O_EXCL-create ``<lock>.steal``, so at most one is ever inside.
    There it re-reads the lock: only if the body is still byte-for-byte the stale one it
    judged does it overwrite it with its own. No other writer can touch the file meanwhile -
    its owner is dead, new acquirers are blocked by its existence, other stealers by the
    guard - and a fresh acquire can never reproduce the stale body, because of its nonce.
    The lock is overwritten in place rather than unlinked so a reader holding it open on
    Windows cannot turn the steal into a sharing violation.

    A guard left by a stealer that crashed inside it is cleared once it is older than
    ``_STEAL_GUARD_STALE``. That clearance is stat-then-unlink, so a slow clearer can delete
    a guard that a newer stealer has just created, letting a second stealer in. Each guard
    therefore carries its creator's nonce, and a stealer re-reads it immediately before the
    overwrite and backs off if it is no longer its own; it also removes the guard only while
    it is still its own, so it never deletes the guard of a stealer that replaced it.

    Residual: the guard re-check and the overwrite are two steps. Two stealers can both
    overwrite only if the wrongful unlink and the second stealer's whole guard-create +
    lock re-read land between them - which needs a crashed stealer's guard to be cleared in
    the first place, plus a clearer stalled across that step. Accepted.
    """
    guard = Path(str(path) + ".steal")
    guard_nonce = uuid.uuid4().hex
    try:
        fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(guard) >= _STEAL_GUARD_STALE:
                guard.unlink()
        except (FileNotFoundError, PermissionError):
            pass  # another contender is clearing or holding it
        raise LockHeldError(f"lock at {path} is being stolen by another contender") from None
    except PermissionError:
        # Windows delete-pending: the previous stealer's guard unlink is waiting on a handle.
        raise LockHeldError(f"lock at {path} is being stolen by another contender") from None
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(guard_nonce)
    try:
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False  # freed meanwhile; the caller's O_EXCL create will take it
        if current != observed:
            return False  # someone else stole or re-created it; re-judge from scratch
        if not _guard_is_ours(guard, guard_nonce):
            raise LockHeldError(f"lock at {path}: steal guard was replaced by another contender")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        return True
    finally:
        _unlink_guard(guard, guard_nonce)


def _guard_is_ours(guard: Path, guard_nonce: str) -> bool:
    try:
        return guard.read_text(encoding="utf-8") == guard_nonce
    except (FileNotFoundError, PermissionError):
        return False  # removed, or mid-removal by a clearer: either way no longer ours


def _unlink_guard(guard: Path, guard_nonce: str) -> None:
    """Drop the steal guard if it is still ours, riding out a brief Windows sharing violation.

    A guard that cannot be removed only delays other stealers until it ages out, so a
    persistent failure is reported rather than raised over a steal that already succeeded.
    """
    deadline = time.monotonic() + _RELEASE_RETRY_TIMEOUT
    while True:
        try:
            if guard.read_text(encoding="utf-8") != guard_nonce:
                return  # replaced by another stealer's guard: theirs to remove
            guard.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                print(f"lock_file: could not remove steal guard {guard} ({exc})", file=sys.stderr)
                return
            time.sleep(_RELEASE_RETRY_POLL)


def acquire(path) -> LockHandle:
    """Acquire the lock at `path`. Steals from dead PIDs, one stealer at a time.

    Raises LockHeldError if a live PID holds the lock, or a concurrent steal is in progress.
    """
    path = Path(path)
    nonce = uuid.uuid4().hex
    body = (
        f"PID={os.getpid()}\nSPAWNED={datetime.now(UTC).isoformat()}\nNONCE={nonce}\n"
    )
    for _ in range(_ACQUIRE_ATTEMPTS):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            return LockHandle(path, nonce)
        try:
            observed = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue  # released between our create and our read: it is free, create again
        info = _parse(observed)
        if not _is_stealable(path, info):
            if info.pid > 0:
                raise LockHeldError(f"lock at {path} held by alive PID {info.pid}")
            raise LockHeldError(f"lock at {path} being written")
        if _steal(path, observed, body):
            return LockHandle(path, nonce)
        # The lock changed under us (stolen or re-created): re-judge it.
    raise LockHeldError(f"lock at {path} contended: gave up after {_ACQUIRE_ATTEMPTS} attempts")
