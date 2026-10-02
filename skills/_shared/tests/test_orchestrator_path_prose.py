"""Pins the /project -> /orchestrator prose to things that exist (gh#597 gh#598 gh#599 gh#601).

Each test here guards one claim the skills used to make that the code could not honour: a
registry row the CLI rejects, a status server nobody wrote, a TODO for a helper that shipped, a
Windows-only launcher, and a resume summary that read fields status.md never carries. A pass
means those specific falsehoods are gone, not that the surrounding prose is right.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

from agentflow import config as cfg
from agentflow.status_writer import StatusState, TaskState, write_status
from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = SKILLS_ROOT / "orchestrator" / "SKILL.md"
PROJECT = SKILLS_ROOT / "project" / "SKILL.md"


def _skill_files() -> list[Path]:
    files = only_tracked(
        SKILLS_ROOT.parent,
        [p for p in SKILLS_ROOT.rglob("SKILL.md") if "_vendored" not in p.parts],
    )
    # Floor guard: a corpus that silently narrowed to nothing would pass every check below.
    assert ORCHESTRATOR.resolve() in {f.resolve() for f in files}
    return files


def test_no_skill_runs_the_windows_only_py_launcher():
    # `py` does not exist on Linux hosts, so a re-validate loop reads command-not-found as a
    # parse failure forever (gh#601).
    hits = [
        f"{f.relative_to(SKILLS_ROOT)}:{n}"
        for f in _skill_files()
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"(^|[\s`(])py -[cm] ", line)
    ]
    assert not hits, f"use `python`, not the `py` launcher: {hits}"


def test_no_stale_status_server_or_t14_placeholders():
    for f in (ORCHESTRATOR, PROJECT):
        text = f.read_text(encoding="utf-8")
        assert "ensure_running" not in text, f"{f.name}: no ensure_running() exists"
        assert "status server" not in text.lower(), f"{f.name}: there is no status server"
        assert "T14" not in text, f"{f.name}: notify_once shipped; drop the T14 placeholders"


def test_orchestrator_never_hand_writes_a_composite_raised_by():
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    # registry.py accepts bare lane names only, and `apply` requires raiser == actor (gh#597).
    assert not re.search(r"raised_by:\s*<[^>]+>\s*\(", text)
    assert "python -m agentflow.registry ask --actor <fleet_context>" in text.replace("\n   ", " ")
    assert "registry apply --actor" in text.replace("\n   ", " ")


def test_bootstrap_checks_for_a_plan_before_invoking_solution():
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    bootstrap = text.split("## Bootstrap (first invocation only)", 1)[1].split("\n## ", 1)[0]
    plan_check = bootstrap.find("plan-shaped")
    solution_step = bootstrap.find("Invoke `solution` skill")
    assert 0 <= plan_check < solution_step, "Bootstrap must branch on a plan before solution (gh#598)"


def _resume_snippet() -> str:
    text = PROJECT.read_text(encoding="utf-8")
    m = re.search(r'python - "\$STATUS_PATH" <<\'PYEOF\'\n(.*?)\nPYEOF', text, re.DOTALL)
    assert m, "from-status resume snippet not found in project/SKILL.md"
    return m.group(1)


def test_from_status_resume_summary_reads_real_fields(tmp_path, monkeypatch, capsys):
    # Run the snippet the skill prints against a board write_status actually produced (gh#599).
    board = tmp_path / "my-epic.status.md"
    state = StatusState(
        epic="My epic",
        plan="plan.md",
        started="2026-01-01T00:00:00Z",
        status="running",
        current_phase="a",
        total_waves=3,
        tasks={1: TaskState(state="done", phase="a"), 2: TaskState(state="ready", phase="a")},
    )
    write_status(board, state)
    monkeypatch.setattr(
        cfg,
        "load_environment",
        lambda: SimpleNamespace(status_board_url_template="https://board.example/{slug}"),
    )
    monkeypatch.setattr(sys, "argv", ["-", str(board)])

    exec(compile(_resume_snippet(), "project-resume-snippet", "exec"), {})

    out = capsys.readouterr().out
    assert "?" not in out, out
    assert "(slug: my-epic)" in out
    assert "Status: running" in out
    assert "Waves: 3  Tasks done: 1/2" in out
    assert "Status board: https://board.example/my-epic" in out
