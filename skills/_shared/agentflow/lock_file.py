"""Lock-file helpers for the orchestrator: O_EXCL acquire, orphan detection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path


@dataclass
class LockInfo:
    pid: int
    spawned: datetime | None


class LockHeldError(RuntimeError):
    """Raised when a lock is held by an alive process."""


class LockHandle:
    def __init__(self, path: Path):
        self.path = path

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# Grace window for a torn/empty lock (creator mid-write between the O_EXCL
# create and the PID write). Within it we treat the lock as held; past it we
# assume the creator crashed mid-create and allow a steal so the lock self-heals.
_TORN_LOCK_GRACE = 5.0  # seconds


def _is_pid_alive(pid: int) -> bool:
    """Return True iff a process with this PID is currently alive on this host."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, we just can't signal it
    except OSError:
        return False


def read_lock(path: Path) -> LockInfo:
    text = Path(path).read_text(encoding="utf-8")
    pid = 0
    spawned: datetime | None = None
    for line in text.splitlines():
        if line.startswith("PID="):
            pid = int(line.split("=", 1)[1].strip())
        elif line.startswith("SPAWNED="):
            try:
                spawned = datetime.fromisoformat(line.split("=", 1)[1].strip())
            except ValueError:
                pass
    return LockInfo(pid=pid, spawned=spawned)


def acquire(path) -> LockHandle:
    """Acquire the lock at `path`. Steals from dead PIDs.

    Raises LockHeldError if a live PID holds the lock.
    """
    path = Path(path)
    body = f"PID={os.getpid()}\nSPAWNED={datetime.now(UTC).isoformat()}\n"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        return LockHandle(path)
    except FileExistsError:
        info = read_lock(path)
        if info.pid > 0:
            if _is_pid_alive(info.pid):
                raise LockHeldError(
                    f"lock at {path} held by alive PID {info.pid}"
                )
            # Dead holder; steal it.
        else:
            # pid<=0 means a torn/empty lock: a creator is mid-write between
            # its O_EXCL create and its PID write. Stealing here would double-
            # hold. Treat as held while fresh; only steal if it's been sitting
            # torn past the grace window (creator likely crashed mid-create).
            try:
                age = datetime.now(UTC).timestamp() - os.path.getmtime(path)
            except OSError:
                age = 0.0
            if age < _TORN_LOCK_GRACE:
                raise LockHeldError(f"lock at {path} being written")
        path.write_text(body, encoding="utf-8", newline="\n")
        return LockHandle(path)
