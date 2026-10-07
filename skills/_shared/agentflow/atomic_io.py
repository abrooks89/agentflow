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

import json
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
                #
                # e.strerror, not str(e): str(e) already carries the errno and the paths, and
                # OSError's own __str__ prefixes the errno and appends the filenames again, so
                # the message named each twice. Rebuilt from the parts instead. A bare
                # PermissionError("text") has no strerror, so its text is kept from str(e).
                strerror = e.strerror if e.strerror is not None else str(e)
                winerror = getattr(e, "winerror", None)
                if winerror is not None:
                    exhausted = ReplaceRetryExhausted(
                        e.errno, strerror, e.filename, winerror, e.filename2
                    )
                else:
                    # filename2 is set after construction: the five-argument form prints
                    # "[WinError None]" on Windows whenever the winerror slot is None.
                    exhausted = ReplaceRetryExhausted(e.errno, strerror, e.filename)
                    exhausted.filename2 = e.filename2
                raise exhausted from e
            time.sleep(delay)
            delay = min(delay * 2, max_delay)


def _current_umask() -> int:
    """The process umask. POSIX offers no read-only call, so set it and put it straight back."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _target_mode(path: Path, mode: int | None) -> int:
    """The permission bits the written file should end with.

    An explicit ``mode`` wins. Otherwise an existing file keeps its own bits, and a new file
    gets what ``open()`` would have given it, ``0o666`` less the umask. ``mkstemp`` creates its
    temp at 0600, and the replace carries the temp's mode onto the target, so without this every
    write would narrow a shared 0664 board to its writer's account alone.
    """
    if mode is not None:
        return mode
    try:
        # 0o777, not 0o7777: the replace makes a new inode owned by the writer, and setuid,
        # setgid or sticky copied off another account's file do not belong on it.
        return path.stat().st_mode & 0o777
    except FileNotFoundError:
        return 0o666 & ~_current_umask()


def atomic_write(
    path: Path,
    text: str,
    encoding: str = "utf-8",
    newline: str | None = "",
    mode: int | None = None,
) -> None:
    """Write ``text`` to ``path`` atomically: temp + fsync + retrying replace.

    A reader never observes a half-written file. ``newline=""`` (the default) preserves the
    caller's line endings byte-for-byte, which the registry's round-trip fidelity contract
    depends on; ``status_writer`` passes ``newline=None`` to keep its historical behaviour.

    On POSIX the file keeps its existing permission bits, or gets the umask default when new;
    ``mode`` overrides both. On Windows there are no such bits to carry, so nothing changes.
    A filesystem that refuses chmod raises here and the write does not land: a board quietly
    narrowed to one account is the failure this exists to prevent, so it is not swallowed.
    """
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            # Before the write, so the fsync below covers the mode as well as the bytes.
            if os.name != "nt":
                os.fchmod(f.fileno(), _target_mode(path, mode))
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


def write_settings_json(path: Path, settings: dict) -> None:
    """Write a Claude Code settings file (``settings.local.json``) the one way every writer does.

    Two writers own this file - the installer's hook step and the auto-allow bootstrap - and
    each carried its own copy of this spelling. Two copies of a byte format drift, and two
    writers that disagree on bytes flip-flop the file on every run. So both call this.

    indent 2; ``ensure_ascii=False`` so an operator's non-ASCII values stay UTF-8 rather than
    becoming backslash-u escapes; no trailing newline; LF (``atomic_write``'s ``newline=""``).
    Atomic because the file carries every session's env block and hooks: a kill mid-write must
    not leave it truncated.
    """
    text = json.dumps(settings, indent=2, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate parses as JSON but cannot be written as UTF-8; the escaped form is
        # the only lossless spelling of it.
        text = json.dumps(settings, indent=2)
    # resolve(): replacing a symlink would swap the link for a regular file and leave a
    # dotfiles-managed target behind; write the file the link points at instead.
    # 0600 explicitly: the env block can carry tokens, so this file stays owner-only whatever
    # mode it had before, unlike the shared boards atomic_write otherwise preserves.
    atomic_write(path.resolve(), text, mode=0o600)
