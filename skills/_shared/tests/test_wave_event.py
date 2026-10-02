"""Tests for the wave-event reporter's input contract (gh#680).

The reporter is the serialized write path for a wave's durable state, so every field it writes
is validated against status_writer's own types, and every bad input - including a missing or
unreadable board - is refused with `wave-event error:` and exit 2, never a traceback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentflow import wave_event
from agentflow.status_writer import StatusState, TaskState, read_status, write_status


def _board(tmp_path: Path) -> Path:
    path = tmp_path / "omega.status.md"
    write_status(
        path,
        StatusState(
            epic="omega",
            plan="/plans/omega.md",
            started="2026-01-01T00:00:00+00:00",
            status="running",
            current_phase="p1",
            tasks={1: TaskState(state="in_flight"), 2: TaskState(state="in_flight")},
        ),
    )
    return path


def _task_state(path: Path, *extra: str) -> list[str]:
    return ["task-state", "--status", str(path), "--task", "1", *extra]


def _refused(capsys, argv: list[str]) -> str:
    assert wave_event.main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("wave-event error: "), err
    assert "Traceback" not in err
    return err


def test_the_verdict_vocabulary_is_status_writers():
    assert wave_event.VERIFIER_VERDICTS == {"clean", "warnings", "blocker_persistent"}
    assert wave_event.FINDING_SEVERITIES == {"low", "medium", "blocker"}


def test_an_unknown_verifier_verdict_is_refused_and_nothing_is_written(tmp_path, capsys):
    path = _board(tmp_path)
    before = path.read_text(encoding="utf-8")
    err = _refused(
        capsys, _task_state(path, "--state", "code_review", "--verifier-verdict", "bogus")
    )
    assert "bogus" in err
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("verdict", sorted(wave_event.VERIFIER_VERDICTS))
def test_every_schema_verdict_is_written(tmp_path, verdict):
    path = _board(tmp_path)
    assert (
        wave_event.main(_task_state(path, "--state", "code_review", "--verifier-verdict", verdict))
        == 0
    )
    assert read_status(path).tasks[1].verifier_verdict == verdict


@pytest.mark.parametrize(
    "findings",
    [
        "[1, 2]",  # a list, the issue's repro
        '"3"',  # a scalar
        '{"low": 1, "critical": 2}',  # a severity the schema does not name
        '{"low": -1}',  # a negative count
        '{"blocker": true}',  # a bool is not a count
        '{"medium": "2"}',  # a string is not a count
        '{"low": 1.5}',  # nor is a float
    ],
)
def test_a_findings_value_that_is_not_a_severity_count_mapping_is_refused(
    tmp_path,
    capsys,
    findings,
):
    path = _board(tmp_path)
    before = path.read_text(encoding="utf-8")
    _refused(capsys, _task_state(path, "--state", "done", "--findings", findings))
    assert path.read_text(encoding="utf-8") == before


def test_malformed_findings_json_is_refused(tmp_path, capsys):
    _refused(capsys, _task_state(_board(tmp_path), "--state", "done", "--findings", "{low:"))


@pytest.mark.parametrize(
    "findings",
    [
        {"low": 1, "medium": 0, "blocker": 0},
        {"blocker": 2},  # absent severities read as zero downstream
    ],
)
def test_a_severity_count_mapping_is_written(tmp_path, findings):
    path = _board(tmp_path)
    assert (
        wave_event.main(_task_state(path, "--state", "done", "--findings", json.dumps(findings)))
        == 0
    )
    assert read_status(path).tasks[1].verifier_findings_count == findings


def test_the_library_path_validates_too(tmp_path):
    """Validation lives in report_task_state, not only the CLI, so a direct caller is held to it."""
    path = _board(tmp_path)
    with pytest.raises(ValueError, match="verifier verdict"):
        wave_event.report_task_state(path, 1, "code_review", verifier_verdict="bogus")
    with pytest.raises(ValueError, match="findings"):
        wave_event.report_task_state(path, 1, "done", findings=[1, 2])


@pytest.mark.parametrize(
    "argv",
    [
        ["note", "--summary", "hi"],
        ["task-state", "--task", "1", "--state", "done"],
    ],
)
def test_a_missing_board_is_refused_without_a_traceback_or_a_lock(tmp_path, capsys, argv):
    missing = tmp_path / "nope.status.md"
    err = _refused(capsys, [argv[0], "--status", str(missing), *argv[1:]])
    assert "not found" in err
    assert list(tmp_path.iterdir()) == []  # no stray `.wave-event.lock` beside it


def test_a_board_in_a_missing_directory_is_refused(tmp_path, capsys):
    _refused(
        capsys, ["note", "--status", str(tmp_path / "gone" / "x.status.md"), "--summary", "hi"]
    )


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter here\n",
        "---\nepic: omega\n---\n",  # frontmatter missing required keys
        "---\n: : [\n---\n",  # frontmatter that is not YAML
        "---\n- a list\n---\n",  # frontmatter that is not a mapping
    ],
)
def test_an_unreadable_board_is_refused(tmp_path, capsys, text):
    path = tmp_path / "bad.status.md"
    path.write_text(text, encoding="utf-8")
    err = _refused(capsys, ["note", "--status", str(path), "--summary", "hi"])
    assert "unreadable" in err
    assert path.read_text(encoding="utf-8") == text


def test_a_valid_note_still_lands(tmp_path):
    path = _board(tmp_path)
    assert (
        wave_event.main(["note", "--status", str(path), "--task", "2", "--summary", "hello"]) == 0
    )
    assert read_status(path).decisions[-1].summary == "hello"
