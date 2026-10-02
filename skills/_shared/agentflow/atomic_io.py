"""One atomic file write for the whole package — temp + fsync + replace, with a retry.

WHY THIS IS A SHARED MODULE AND NOT A HELPER IN EACH CALLER. There were two copies of this
routine — ``registry._atomic_write`` and ``status_writer.write_status``'s inline tail — written
at different times and subtly different: one cleaned up its temp on ``BaseException``, the other
on ``Exception``; one is called under an edit-lock, the other under nothing. When gh#57 was fixed
in the first, the second still carried the bug, on the path with MORE concurrent readers and no
writer serialisation at all. Two copies of a filesystem primitive diverge; one does not.

STDLIB ONLY, matching the ``_shared`` package's pledge.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

#: Wall-clock ceiling for the whole replace retry, and the per-attempt backoff cap.
#: These are module-level so tests can pin them and so a future tuner has ONE place to look.
#: The budget is deliberately small: ``registry`` calls this while holding the mkdir edit-lock,
#: whose hold is specified in seconds against a 5-minute stale-steal threshold. A generous
#: retry here converts a transient into a fleet-wide lock stall, which is strictly worse than
#: the failure being absorbed.
REPLACE_RETRY_TIMEOUT = 2.0
REPLACE_RETRY_MAX_DELAY = 0.25


class ReplaceRetryExhausted(PermissionError):
    """The replace retry ran out of budget — the ONE cause the gh#57 message describes.

    It subclasses ``PermissionError`` on purpose: every existing ``except PermissionError``
    in the package keeps catching it, so nothing that handles a denial today stops handling
    it. What the subclass buys is the other direction — a caller that wants to say "your
    write did not land, the destination was held, re-run" can now say it about THIS and not
    about every denial that happens to reach the same handler.

    gh#138: one handler in ``registry.main()`` reported every ``PermissionError`` as an
    exhausted retry, so a denied ``registry-log.md`` came back as "the file was held by
    another process ... re-run this command" — a cause that had not occurred, and a remedy
    that instructs an unattended lane to loop forever on a condition no retry can clear.
    """


def replace_retry(
    tmp: str | Path,
    path: Path,
    timeout: float = REPLACE_RETRY_TIMEOUT,
    max_delay: float = REPLACE_RETRY_MAX_DELAY,
) -> None:
    """``os.replace`` onto an existing path, tolerating the Windows reader window.

    On Windows a replace onto a destination another process holds open fails with
    ``PermissionError`` (WinError 5) — unlike POSIX, where the rename succeeds and existing
    readers keep the old inode. **A reader's open handle is the NORMAL state here, not an exotic
    one:** the firing contract requires every lane to read the registry lock-free at every wake,
    and ``status.md`` is walked by the fleet survey. The edit-lock serialises *writers*; nothing
    serialises readers, so the thing that blocks the replace is exactly what the design tells six
    lanes to do constantly — and contention gets MORE frequent as the fleet gets healthier.

    Observed live on 2026-08-31 with six lanes active, and it **succeeded on the immediate
    retry** — contention, never corruption. Same lesson and shape as ``registry._rmdir_retry``,
    which already tolerates WinError 32 on the lock dir.

    Only ``PermissionError`` is retried. A different ``OSError`` is a real failure and is raised
    at once: retrying it would turn a fast loud error into a slow one and tell the caller nothing
    new. The final failure still RAISES — the caller losing a note or a claim must find out. What
    this removes is the transient that never needed to reach a caller, not the failure that does.
    """
    deadline = time.monotonic() + timeout
    delay = 0.02
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            if time.monotonic() >= deadline:
                # Re-raised as the narrow type so a caller can tell "the destination was
                # held for the whole window" from "this path is denied", which are the same
                # errno and want opposite advice: retry the first, never the second.
                raise ReplaceRetryExhausted(e.errno, str(e), getattr(e, "filename", None)) from e
            time.sleep(delay)
            delay = min(delay * 2, max_delay)


def atomic_write(path: Path, text: str, encoding: str = "utf-8", newline: str | None = "") -> None:
    """Write ``text`` to ``path`` atomically: temp + fsync + retrying replace.

    A reader never observes a half-written file. ``newline=""`` (the default) preserves the
    caller's line endings byte-for-byte, which the registry's round-trip fidelity contract
    depends on; ``status_writer`` passes ``newline=None`` to keep its historical behaviour.
    """
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        replace_retry(tmp, path)
    except BaseException:
        # BaseException, not Exception: an interrupt landing between mkstemp and replace would
        # otherwise strand a `<name>.<rand>.tmp` beside the real file forever.
        #
        # And the cleanup's own error must never replace the one that caused it — a
        # PermissionError from this unlink would mask the replace failure that is the actual
        # diagnosis, so every OSError here is swallowed rather than only FileNotFoundError.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
