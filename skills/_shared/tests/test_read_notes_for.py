"""`read --notes-for <actor>` -- the phase-boundary re-read of routed Notes (gh#159).

A Note that unblocks work ("seam released, publish now") sent mid-wake used to wait a full wake,
because the boundary re-read was Questions + Directives only. This verb is what the boundary now
runs; these tests pin what it shows, what it hides, and that silence is never its answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentflow import ntfy
from agentflow.registry import main


@pytest.fixture(autouse=True)
def _no_pings(monkeypatch):
    # Network guard: NTFY_URL/NTFY_TOPIC are set in real fleet shells.
    monkeypatch.setattr(ntfy, "notify", lambda trigger, **kw: None)


@pytest.fixture
def env(tmp_path: Path):
    reg = tmp_path / "projects" / "demo" / "status" / "registry.md"
    lock = tmp_path / "locks"
    lock.mkdir()
    base = ["--file", str(reg), "--lock-dir", str(lock), "--project", "demo"]
    assert main(["init", *base]) == 0

    class Env:
        registry_path = reg

        def run(self, *args) -> int:
            return main([args[0], *base, "--timeout", "5", *args[1:]])

        def text(self) -> str:
            return reg.read_text(encoding="utf-8")

    return Env()


def _note(env, actor: str, text: str, to: str | None = None) -> None:
    args = ["note", "--actor", actor, "--text", text]
    if to:
        args += ["--to", to]
    assert env.run(*args) == 0


def _read_notes_for(env, capsys, actor: str) -> tuple[int, list[str]]:
    capsys.readouterr()
    rc = main(["read", "--file", str(env.registry_path), "--notes-for", actor])
    return rc, capsys.readouterr().out.splitlines()


def test_note_routed_to_the_actor_is_shown(env, capsys):
    _note(env, "lead-dev", "seam released, publish now", to="jr-dev-1")
    rc, out = _read_notes_for(env, capsys, "jr-dev-1")
    assert rc == 0
    assert len(out) == 1, out
    assert "lead-dev -> jr-dev-1: seam released, publish now" in out[0]
    assert not out[0].startswith("#"), "a routed note must not be reported as an all-clear"


def test_struck_note_is_hidden(env, capsys):
    _note(env, "lead-dev", "seam released, publish now", to="jr-dev-1")
    assert env.run("note", "--strike", "--actor", "jr-dev-1", "--match", "seam released") == 0
    assert "~~" in env.text(), "fixture precondition: the note was actually struck"
    rc, out = _read_notes_for(env, capsys, "jr-dev-1")
    assert rc == 0
    assert not any("seam released" in line for line in out), out
    assert out and out[-1].startswith("# no live notes routed to jr-dev-1"), out


def test_note_to_another_actor_is_hidden(env, capsys):
    _note(env, "lead-dev", "only for jr-dev-2", to="jr-dev-2")
    rc, out = _read_notes_for(env, capsys, "jr-dev-1")
    assert rc == 0
    assert not any("only for jr-dev-2" in line for line in out), out
    assert out == ["# no live notes routed to jr-dev-1 (fleet broadcasts are read at wake); 1 live note(s) checked"], out


def test_fleet_broadcast_is_not_shown(env, capsys):
    """A fleet Note is a broadcast nobody strikes, so on a live board they accumulate (125 lines
    for one lane on the first run). The boundary re-read shows only Notes routed to the reader;
    broadcasts stay with the wake read -- see the decision comment in `_do_read`."""
    _note(env, "steward", "deploy hold broken, lock is free")
    assert "steward -> fleet:" in env.text(), "fixture precondition: default route is fleet"
    rc, out = _read_notes_for(env, capsys, "jr-dev-1")
    assert rc == 0
    assert not any("deploy hold broken" in line for line in out), out


def test_empty_prints_all_clear_and_exits_zero(env, capsys):
    rc, out = _read_notes_for(env, capsys, "lead-dev")
    assert rc == 0
    assert out == ["# no live notes routed to lead-dev (fleet broadcasts are read at wake); 0 live note(s) checked"], out


def test_unknown_actor_is_refused_not_all_cleared(env, capsys):
    """A typo'd actor followed by an all-clear would be a confident false green."""
    _note(env, "lead-dev", "for you", to="jr-dev-1")
    capsys.readouterr()
    rc = main(["read", "--file", str(env.registry_path), "--notes-for", "jr_dev_1"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "agent_known" in captured.err
    assert "# no live notes" not in captured.out


def test_notes_for_is_one_selector(env, capsys):
    capsys.readouterr()
    rc = main(["read", "--file", str(env.registry_path), "--notes-for", "lead-dev", "--directives"])
    assert rc == 2
    assert "read_one_selector" in capsys.readouterr().err


def test_read_writes_nothing(env, capsys):
    _note(env, "lead-dev", "for you", to="jr-dev-1")
    before = env.text()
    _read_notes_for(env, capsys, "jr-dev-1")
    assert env.text() == before


# --------------------------------------------------------------------------------------
# The prose half: every boundary block that CONSUMES must run this verb for its own actor.
# --------------------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOUNDARY_SITES = (
    ("skills/lead-dev/SKILL.md", "lead-dev"),
    ("skills/jr-dev-1/SKILL.md", "jr-dev-1"),
    ("skills/jr-dev-2/SKILL.md", "jr-dev-2"),
    ("skills/jr-dev-3/SKILL.md", "jr-dev-3"),
    ("skills/auditor/SKILL.md", "auditor"),
    ("skills/steward/SKILL.md", "steward"),
    ("docs/REGISTRY.md", "<you>"),
)


@pytest.mark.parametrize(("rel", "actor"), _BOUNDARY_SITES)
def test_phase_boundary_rereads_routed_notes(rel: str, actor: str) -> None:
    path = _REPO_ROOT / rel
    assert path.is_file(), f"{rel} moved; this gate must fail loudly, not stop checking"
    text = path.read_text(encoding="utf-8")
    # Case-folded: the steward's block opens a sentence ("In this order, at every domain
    # boundary"), every other site opens mid-sentence. `lower()` keeps offsets for this text.
    opened = text.lower().find("in this order, at every ")
    assert opened != -1, f"{rel} has no phase-boundary block"
    closed = text.find("Consuming AFTER the status write", opened)
    assert closed != -1, f"{rel}'s phase-boundary block has no terminator"
    # Whitespace-normalized: the call may wrap between the flag and the actor.
    block = " ".join(text[opened:closed].split())
    consume = block.find(f"--notes-for {actor}")
    assert consume != -1, (
        f"{rel}: the boundary CONSUME step never re-reads `read --notes-for {actor}` -- a routed "
        "Note sent mid-wake waits a full wake again (gh#159)"
    )
    status = block.find("status --actor")
    assert status != -1 and consume < status, f"{rel}: Notes are re-read AFTER the status refresh, not in CONSUME"
