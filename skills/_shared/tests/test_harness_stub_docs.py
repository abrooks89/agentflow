"""The harness docs must not claim a ScheduleWakeup stub intercepts the real call.

`harness/stubs/` tracks only a README: no hook or code stands between a recording
session and the real `ScheduleWakeup`. The runbook and the stubs README used to say a
stub captured each call as a log row "so no real wake loop is ever entered", so an
operator running Scenario B or D in a bypass-permissions session registered a real wake
believing none existed. These pins keep the false claims out and the operator's actual
duties (write the row by hand, stop the registered wake) in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentflow.tracked_paths import tracked_files

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = REPO_ROOT / "harness" / "run-scenario.md"
STUBS_README = REPO_ROOT / "harness" / "stubs" / "README.md"


def _flat(path: Path) -> str:
    """Whitespace-collapsed text, so a re-wrap cannot hide or fake a phrase."""
    return " ".join(path.read_text(encoding="utf-8").split())


#: Phrases that claimed automatic capture or a suppressed wake.
FALSE_CLAIMS = [
    (RUNBOOK, "so no real wake loop is ever entered"),
    (RUNBOOK, "is captured by the stub"),
    (RUNBOOK, "Stubbed wakes only"),
    (RUNBOOK, "record stubs active"),
    (RUNBOOK, "Confirm no second wake fires on its own"),
    (STUBS_README, "instead of registering a real wake, the stub"),
    (STUBS_README, "with **recording no-ops**"),
    (STUBS_README, "The stub never re-invokes the prompt"),
    (STUBS_README, "routed through the ScheduleWakeup stub"),
]


@pytest.mark.parametrize(("doc", "phrase"), FALSE_CLAIMS, ids=lambda v: getattr(v, "name", v))
def test_docs_do_not_claim_automatic_capture(doc: Path, phrase: str) -> None:
    assert phrase not in _flat(doc), f"{doc.name} still claims: {phrase!r}"


@pytest.mark.parametrize("doc", [RUNBOOK, STUBS_README], ids=lambda p: p.name)
def test_docs_state_nothing_intercepts_and_rows_are_hand_written(doc: Path) -> None:
    text = _flat(doc)
    assert "Nothing in this repo intercepts" in text or "Nothing in this directory intercepts" in text
    assert "by hand" in text


@pytest.mark.parametrize("doc", [RUNBOOK, STUBS_README], ids=lambda p: p.name)
def test_docs_warn_bypass_session_registers_a_real_wake_and_say_how_to_stop_it(doc: Path) -> None:
    text = _flat(doc)
    # Every recording session registers one, not only a bypass-permissions session.
    assert "Any recording session registers a real wake" in text
    assert "bypass permissions does so without prompting" in text
    # The remedy: the wake is session-scoped, so ending the session before it fires stops it.
    # The deadline is the real call's delay; the logged `wake_at` is an operator-chosen value.
    assert "end the session" in text
    assert "before the real wake fires (the call's `delaySeconds` after it was made" in text
    assert "before `wake_at`" not in text


def test_stubs_dir_tracks_no_intercepting_code() -> None:
    """The docs' claim rests on this: if stub code ever lands, revisit both docs."""
    tracked = tracked_files(REPO_ROOT, lambda rel: rel.startswith("harness/stubs/"))
    rels = [p.relative_to(REPO_ROOT).as_posix() for p in tracked]
    assert rels == ["harness/stubs/README.md"]
