#!/usr/bin/env python3
"""The installer's closing doctor report on a legacy-codepage console (gh#524).

`python -m agentflow.doctor` forces its stdout to utf-8 with `errors="replace"` before
writing the report (gh#416). The installer prints the same report as its LAST act and did
not: on a cp1252 console one character outside that code page raised `UnicodeEncodeError`,
the call site's own `except Exception` caught it, and the install ended with
`(doctor unavailable: ...)` instead of the report. Nothing crashed, which is why it hid -
the self-check just reported nothing.

These drive `main()` to that call site with a cp1252 byte stream standing in for the console
and a stubbed doctor (the real one reads the operator's live config home, not tmp_path).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402

# U+2713 and U+2603 are outside cp1252, so encoding either there raises.
_REPORT = "  surface resolves ✓ project snow☃man"


def _run_main_to_the_doctor_call(tmp_path, stream, report=_REPORT):
    doctor = mock.Mock(diagnose=lambda *_a: [], render=lambda _d: report,
                       examined_a_project=lambda _f: False,
                       unexamined_verdict=lambda r, _dir: r)
    steps = [provision.Step(n=5, title="step 5", run=lambda _o: None)]
    with mock.patch.dict(sys.modules, {"agentflow.doctor": doctor}), \
         mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_print_verification_readiness", lambda _r: None), \
         mock.patch.object(provision, "_verification_readiness", lambda _o: []), \
         mock.patch.object(sys, "stdout", stream):
        return provision.main(["--resume-from", "5",
                               "--suite-root", str(tmp_path), "--config-home", str(tmp_path),
                               "--settings-file", str(tmp_path / "s.json"),
                               "--claude-root", str(tmp_path)])


def _run_on_a_cp1252_console(tmp_path, report=_REPORT):
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    try:
        rc = _run_main_to_the_doctor_call(tmp_path, console, report)
        console.flush()
        return rc, raw.getvalue().decode("utf-8", errors="replace")
    finally:
        console.detach()   # the wrapper must not close `raw` when it is collected


def test_the_report_survives_a_cp1252_console(tmp_path):
    rc, out = _run_on_a_cp1252_console(tmp_path)
    assert rc == 0
    assert "(doctor unavailable" not in out
    assert "surface resolves" in out and "project snow" in out
    assert "✓" in out and "☃" in out, "the report's own characters must arrive"


def test_an_unencodable_character_is_replaced_not_fatal(tmp_path):
    """A path decoded with surrogateescape carries a lone surrogate, which even utf-8 refuses
    to encode strictly. `errors="replace"` is what keeps the rest of the report."""
    rc, out = _run_on_a_cp1252_console(tmp_path, "  folder caf\udce9 resolves")
    assert rc == 0
    assert "(doctor unavailable" not in out
    assert "  folder caf? resolves" in out


def test_a_stream_without_reconfigure_is_left_alone(tmp_path):
    """A test capture (or any stream lacking reconfigure) must not be touched, and must not
    turn the report into `(doctor unavailable: AttributeError ...)`."""
    capture = io.StringIO()
    assert not hasattr(capture, "reconfigure")
    rc = _run_main_to_the_doctor_call(tmp_path, capture)
    out = capture.getvalue()
    assert rc == 0
    assert "(doctor unavailable" not in out
    assert _REPORT in out
