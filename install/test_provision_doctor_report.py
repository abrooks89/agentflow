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
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402
from agentflow import doctor  # noqa: E402 - a broken import must fail here, never skip

# U+2713 and U+2603 are outside cp1252, so encoding either there raises.
_REPORT = "  surface resolves ✓ project snow☃man"


def _run_main_to_the_doctor_call(tmp_path, stream, report=_REPORT):
    doctor = mock.Mock(diagnose=lambda *_a, **_k: [], render=lambda _d: report,
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


# --------------------------------------------------------------------------- #
# The closing verdict names spec-corpus (`survey:*`) warnings apart from config warnings. A
# spec with no `## Files` section is not a clamped dial, and "NOT everywhere as configured"
# over one sends the reader to a config home with nothing in it to fix.
# --------------------------------------------------------------------------- #


def _verdict_over(projects: dict) -> str:
    """The last line `_print_doctor_report` prints, with *projects* mapping name -> findings."""
    capture = io.StringIO()
    with mock.patch.object(provision, "_configured_projects", lambda _o: list(projects)), \
         mock.patch.object(sys, "stdout", capture):
        provision._print_doctor_report(mock.Mock(), lambda name, **_k: projects[name], doctor.render,
                                       doctor.examined_a_project, doctor.unexamined_verdict)
    return [ln for ln in capture.getvalue().splitlines() if ln.strip()][-1]


def _f(check: str, status: str = "warn", detail: str = "something"):
    return doctor.Finding(check, status, detail)


_SPEC = ("survey:files-missing", "warn", "spec has no `## Files` section: sample-spec")
#: The bare catch-all: an epic-survey message no kind recognised, which can be config-shaped.
_BARE = ("survey", "warn", "unreadable status file, skipping the registry read")
_CLAMP = ("config", "warn", "idle_wake_minutes 900 exceeds the 60-minute ceiling")


def test_only_spec_corpus_warnings_get_a_verdict_naming_spec_problems():
    last = _verdict_over({"alpha": [_f("project:alpha", "ok"), _f(*_SPEC), _f(*_SPEC)],
                          "beta": [_f("project:beta", "ok")]})
    assert "NOT everywhere as configured" not in last, last
    assert "configured as intended" in last, last
    assert "2 spec-corpus warning(s) (survey:*) for alpha" in last, last


def test_config_and_spec_warnings_together_name_both():
    last = _verdict_over({"alpha": [_f("project:alpha", "ok"), _f(*_CLAMP)],
                          "beta": [_f("project:beta", "ok"), _f(*_SPEC)]})
    assert "NOT everywhere as configured - see the warning(s) for alpha" in last, last
    assert "also 1 spec-corpus warning(s) (survey:*) for beta" in last, last


def test_config_only_warnings_keep_the_config_verdict_unchanged():
    last = _verdict_over({"alpha": [_f("project:alpha", "ok"), _f(*_CLAMP)]})
    assert last == ("resolved surface healthy for all 1 configured project(s), but NOT "
                    "everywhere as configured - see the warning(s) for alpha (checked: alpha)")


def test_a_failing_project_still_decides_the_verdict_over_spec_warnings():
    last = _verdict_over({"alpha": [_f("project:alpha", "FAIL"), _f(*_SPEC)]})
    assert last.startswith("resolved surface NOT healthy: 1 of 1"), last


def test_render_names_spec_problems_when_they_are_the_only_warnings():
    only_spec = doctor.render([_f("project:alpha", "ok"), _f(*_SPEC)]).rstrip().splitlines()[-1]
    mixed = doctor.render([_f(*_CLAMP), _f(*_SPEC)]).rstrip().splitlines()[-1]
    config = doctor.render([_f(*_CLAMP)]).rstrip().splitlines()[-1]

    assert "NOT everywhere" not in only_spec and "configured as intended" in only_spec, only_spec
    assert "1 spec-corpus warning(s) (survey:*)" in only_spec, only_spec
    assert "NOT everywhere as configured" in mixed and "also 1 spec-corpus" in mixed, mixed
    assert config == "resolved surface healthy, but NOT everywhere as configured — see the warning(s) above"


def test_a_bare_survey_warning_keeps_the_config_verdict():
    """Fail closed: only `survey:<kind>` is spec-corpus. The catch-all can be config-shaped."""
    last = _verdict_over({"alpha": [_f("project:alpha", "ok"), _f(*_BARE)]})
    rendered = doctor.render([_f(*_BARE)]).rstrip().splitlines()[-1]

    assert "NOT everywhere as configured - see the warning(s) for alpha" in last, last
    assert "spec-corpus" not in last, last
    assert "NOT everywhere as configured" in rendered and "spec-corpus" not in rendered, rendered


# --------------------------------------------------------------------------- #
# gh#1114: only an unimportable doctor is "unavailable". Anything the doctor raises once
# imported is a defect, named as one, with its traceback on stderr. Advisory either way:
# main() still returns 0.
# --------------------------------------------------------------------------- #


def _run_closing_check(tmp_path, doctor_module):
    """main() to the closing check with *doctor_module* as `agentflow.doctor`; (rc, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    steps = [provision.Step(n=5, title="step 5", run=lambda _o: None)]
    with mock.patch.dict(sys.modules, {"agentflow.doctor": doctor_module}), \
         mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_print_verification_readiness", lambda _r: None), \
         mock.patch.object(provision, "_verification_readiness", lambda _o: []), \
         mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
        rc = provision.main(["--resume-from", "5",
                             "--suite-root", str(tmp_path), "--config-home", str(tmp_path),
                             "--settings-file", str(tmp_path / "s.json"),
                             "--claude-root", str(tmp_path)])
    return rc, out.getvalue(), err.getvalue()


def test_an_unimportable_doctor_is_reported_unavailable(tmp_path):
    # A None entry in sys.modules makes `from agentflow.doctor import ...` raise ImportError.
    rc, out, _err = _run_closing_check(tmp_path, None)
    assert rc == 0
    assert "(doctor unavailable: ModuleNotFoundError" in out, out
    assert "FAIL closing doctor" not in out


def test_a_raising_render_is_a_named_defect_with_a_traceback(tmp_path):
    def render(_findings):
        raise ValueError("render blew up")

    stub = mock.Mock(diagnose=lambda *_a, **_k: [], render=render,
                     examined_a_project=lambda _f: False, unexamined_verdict=lambda r, _d: r)
    rc, out, err = _run_closing_check(tmp_path, stub)
    assert rc == 0, "the closing check is advisory and never fails the install"
    assert ("FAIL closing doctor raised ValueError: render blew up "
            "(a defect, not your environment)") in out, out
    assert "doctor unavailable" not in out
    assert "Traceback" in err and "render blew up" in err, err


def test_an_import_error_from_inside_render_is_still_a_defect(tmp_path):
    """The ImportError branch covers the import alone, not one raised by the doctor's code."""
    def render(_findings):
        raise ImportError("lazy import inside render")

    stub = mock.Mock(diagnose=lambda *_a, **_k: [], render=render,
                     examined_a_project=lambda _f: False, unexamined_verdict=lambda r, _d: r)
    rc, out, err = _run_closing_check(tmp_path, stub)
    assert rc == 0
    assert "FAIL closing doctor raised ImportError: lazy import inside render" in out, out
    assert "doctor unavailable" not in out
    assert "Traceback" in err


def test_a_doctor_module_that_raises_on_import_is_a_defect_not_an_escape(tmp_path):
    """Importing runs module code; a SyntaxError there is a defect and must not escape main()."""
    class _BrokenModule(types.ModuleType):
        def __getattr__(self, name):
            raise SyntaxError("simulated broken doctor module")

    rc, out, err = _run_closing_check(tmp_path, _BrokenModule("agentflow.doctor"))
    assert rc == 0
    assert "FAIL closing doctor raised SyntaxError: simulated broken doctor module" in out, out
    assert "doctor unavailable" not in out
    assert "Traceback" in err and "simulated broken doctor module" in err, err
