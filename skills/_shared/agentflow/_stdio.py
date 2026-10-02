"""UTF-8 forcing for every agentflow CLI's stdout and stderr (gh#574).

On Windows a pipe or console stdout defaults to the legacy code page (cp1252), so one
character outside it - an arrow a lane wrote into a registry note - made `registry read`
raise UnicodeEncodeError and blinded every lane's wake read, while `validate` stayed
green. gh#524 fixed three modules one line at a time; this is the one shared helper, and
tests/test_cli_stdio_utf8.py pins that every CLI entry point calls it.
"""

from __future__ import annotations

import sys

__all__ = ["utf8_stdio"]


def _force_utf8(stream: object) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        # A stream without reconfigure (a test capture, a None stream under pythonw)
        # is left exactly as the caller handed it.
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        # A detached or closed stream refuses reconfigure. The helper runs first thing
        # in main(), so raising here would turn a cosmetic encoding upgrade into a CLI
        # that cannot start at all; the stream keeps its encoding and the write that
        # follows reports its own failure if there is one.
        return


def utf8_stdio() -> None:
    """Reconfigure sys.stdout and sys.stderr to UTF-8 with errors='replace'.

    Streams that cannot be reconfigured are left untouched. Never raises.
    """
    _force_utf8(sys.stdout)
    _force_utf8(sys.stderr)
