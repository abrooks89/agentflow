"""Opaque UC-ID allocator + append-only provenance registry (Phase 4 T1).

Mints zero-padded monotonic ``UC-NNNN`` ids under
``<project_root>/design/use-cases/``. A flock-style lock (reusing ``lock_file``) over
``.counter`` serializes minters so concurrent callers never collide.

The registry (``registry.jsonl``) is an append-only *birth certificate*: one
row per id, ``{id, minted, epic, title, origin}``. It stores NO domain/status
(those are mutable and live in refine/solution/flow docs — single source of
truth). Digits carry no meaning; re-parenting a domain never touches an id.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from . import lock_file

_ID_WIDTH = 4  # UC-0001 .. UC-9999 .. UC-12345 (width is a floor, not a cap)
_LOCK_SUFFIX = ".lock"
_ACQUIRE_TIMEOUT = 30.0  # seconds a minter will spin for the counter lock
_ACQUIRE_POLL = 0.01


def _format_id(n: int) -> str:
    return f"UC-{n:0{_ID_WIDTH}d}"


def _use_cases_dir(project_root) -> Path:
    return Path(project_root) / "design" / "use-cases"


def _acquire_counter_lock(lock_path: Path) -> lock_file.LockHandle:
    """Acquire the counter lock, spinning past live-held contention.

    ``lock_file.acquire`` raises ``LockHeldError`` when a live PID holds the
    lock (and steals it from dead PIDs). Minters are short-lived, so we poll
    until the current holder releases rather than failing immediately.
    """
    deadline = time.monotonic() + _ACQUIRE_TIMEOUT
    while True:
        try:
            return lock_file.acquire(lock_path)
        except lock_file.LockHeldError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_ACQUIRE_POLL)
        except FileNotFoundError:
            # Holder unlinked the lock between our EXCL-create failing and
            # read_lock opening it — it's free now; retry immediately.
            if time.monotonic() >= deadline:
                raise
            time.sleep(_ACQUIRE_POLL)
        except PermissionError:
            # Windows delete-pending: the holder's release() unlinked the lock
            # while another spinner still held a read handle, so the file sits
            # in delete-pending until that handle closes. Any open on it —
            # read_lock's read_text OR the O_EXCL create — fails with
            # PermissionError (WinError 5/32) for that window. The lock is
            # mid-release, i.e. transiently busy: retry, same as the
            # PermissionError tolerance in _release_counter_lock.
            if time.monotonic() >= deadline:
                raise
            time.sleep(_ACQUIRE_POLL)


def _release_counter_lock(handle: lock_file.LockHandle) -> None:
    """Release the counter lock, tolerating Windows delete contention.

    On Windows a concurrent spinner may momentarily hold the lock file open in
    ``read_lock`` while we unlink it, yielding ``PermissionError`` (WinError 32).
    The reader closes promptly, so retry briefly before giving up.
    """
    deadline = time.monotonic() + _ACQUIRE_TIMEOUT
    while True:
        try:
            handle.release()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_ACQUIRE_POLL)


def _read_counter(counter_path: Path) -> int:
    try:
        text = counter_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0
    if not text:
        return 0
    return int(text)


def allocate(
    project_root,
    count: int,
    *,
    epic_slug: str,
    titles: list[str],
    date: str,
    origin: str = "refine",
) -> list[str]:
    """Mint ``count`` fresh opaque UC-ids and record their provenance.

    Args:
        project_root: project data dir; ids live under ``<root>/design/use-cases/``.
        count: how many ids to mint (>= 1).
        epic_slug: owning epic, stored in each registry row.
        titles: one functional-story title per id (``len(titles) == count``).
        date: mint timestamp/date string, stored verbatim as ``minted``.
        origin: minting stage; defaults to ``"refine"`` (where ids are born).

    Returns:
        The newly minted ids, in allocation order (e.g. ``["UC-0148", ...]``).

    Raises:
        ValueError: on non-positive ``count`` or a title/count length mismatch.
        LockHeldError: if the counter lock can't be acquired within the timeout.
    """
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")
    if len(titles) != count:
        raise ValueError(
            f"titles length {len(titles)} != count {count}"
        )

    uc_dir = _use_cases_dir(project_root)
    uc_dir.mkdir(parents=True, exist_ok=True)
    counter_path = uc_dir / ".counter"
    registry_path = uc_dir / "registry.jsonl"
    lock_path = counter_path.with_suffix(counter_path.suffix + _LOCK_SUFFIX)

    handle = _acquire_counter_lock(lock_path)
    try:
        start = _read_counter(counter_path)
        ids = [_format_id(start + i) for i in range(1, count + 1)]

        rows = "".join(
            json.dumps(
                {
                    "id": uc_id,
                    "minted": date,
                    "epic": epic_slug,
                    "title": title,
                    "origin": origin,
                },
                ensure_ascii=False,
            )
            + "\n"
            for uc_id, title in zip(ids, titles)
        )
        with registry_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(rows)

        counter_path.write_text(
            str(start + count), encoding="utf-8", newline="\n"
        )
        return ids
    finally:
        _release_counter_lock(handle)
