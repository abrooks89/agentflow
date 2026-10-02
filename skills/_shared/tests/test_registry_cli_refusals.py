"""Registry CLI receipts and refusals that pointed the caller at the wrong fix.

One file for five small defects in the same CLI surface (gh#648, gh#649, gh#650, gh#655,
gh#661). Each one let a lane either write something the next `validate` rejects, or act on a
message that named the wrong flag, the wrong id or nothing at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow import ntfy, registry
from agentflow.registry import (
    LANES,
    NOTE_MAX_BYTES,
    NOTE_OVERFLOW_DIR,
    RULES,
    main,
    parse_registry,
)

DOC = Path(__file__).resolve().parents[3] / "docs" / "REGISTRY.md"


@pytest.fixture(autouse=True)
def _no_pings(monkeypatch):
    # A halted status write sends an ntfy; real fleet shells carry NTFY_URL.
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
        notes_dir = reg.parent / "notes"
        log_path = reg.parent / "registry-log.md"

        def run(self, *args) -> int:
            return main([args[0], *base, "--timeout", "5", *args[1:]])

        def text(self) -> str:
            return reg.read_text(encoding="utf-8")

    return Env()


def _live(text: str, note: str) -> bool:
    return re.search(rf"(?m)^\s*- \[[^\]]+\] {re.escape(note)}$", text) is not None


def _struck(text: str, note: str) -> bool:
    return re.search(rf"(?m)^\s*- ~~\[[^\]]+\] {re.escape(note)}~~$", text) is not None


def _refused(env, capsys, rule_key: str, *args) -> str:
    before = env.text()
    rc = env.run(*args)
    captured = capsys.readouterr()
    assert rc == 2, f"expected refusal (2), got {rc}; stderr={captured.err!r}"
    assert f"[{rule_key}]" in captured.err, captured.err
    assert env.text() == before, "a refusal must leave the registry byte-identical"
    return captured.err


def _claimed_row(env, capsys) -> str:
    assert env.run("publish", "--actor", "lead-dev", "--slug", "gamma", "--title", "gamma (p100)",
                   "--lane", "jr-dev-1") == 0
    assert env.run("claim", "--actor", "jr-dev-1", "--id", "E-0001-gamma") == 0
    capsys.readouterr()
    return "E-0001-gamma"


# --------------------------------------------------------------------------- #
# gh#648 -- release --waiting-on gets the same liveness check status has
# --------------------------------------------------------------------------- #
def test_release_parked_on_nonexistent_question_is_refused(env, capsys):
    rid = _claimed_row(env, capsys)
    err = _refused(env, capsys, "agent_waiting_on", "release", "--actor", "jr-dev-1", "--id", rid,
                   "--state", "parked", "--waiting-on", "Q-0042")
    assert "Q-0042" in err
    assert main(["validate", "--file", str(env.registry_path)]) == 0


def test_release_parked_on_malformed_id_is_refused(env, capsys):
    rid = _claimed_row(env, capsys)
    err = _refused(env, capsys, "agent_waiting_on", "release", "--actor", "jr-dev-1", "--id", rid,
                   "--state", "parked", "--waiting-on", "not-an-id")
    assert "not-an-id" in err


def test_release_parked_without_waiting_on_is_refused(env, capsys):
    """gh#707: a parked row must name what it waits on, so leaving --waiting-on out is refused
    exactly like naming a dead id -- otherwise the row is parked on nothing from its first write."""
    rid = _claimed_row(env, capsys)
    before = env.text()
    err = _refused(env, capsys, "agent_waiting_on", "release", "--actor", "jr-dev-1", "--id", rid,
                   "--state", "parked")
    assert "--waiting-on" in err
    assert env.text() == before


def test_release_parked_on_live_question_still_writes(env, capsys):
    rid = _claimed_row(env, capsys)
    assert env.run("ask", "--actor", "jr-dev-1", "--urgency", "waiting", "--question", "X?",
                   "--default", "yes", "--work", rid) == 0
    assert env.run("release", "--actor", "jr-dev-1", "--id", rid, "--state", "parked",
                   "--waiting-on", "Q-0001") == 0
    row = parse_registry(env.text()).queue[rid]
    assert (row.state, row.waiting_on) == ("parked", "Q-0001")


# --------------------------------------------------------------------------- #
# gh#649 -- ask and raise-blocker print the id they allocated
# --------------------------------------------------------------------------- #
def test_ask_prints_the_allocated_question_id(env, capsys):
    capsys.readouterr()
    assert env.run("ask", "--actor", "jr-dev-1", "--urgency", "waiting", "--question", "Should X?",
                   "--default", "yes") == 0
    assert capsys.readouterr().out.strip() == "asked: Q-0001"
    assert env.run("ask", "--actor", "jr-dev-1", "--urgency", "waiting", "--question", "Should Y?",
                   "--default", "no") == 0
    assert capsys.readouterr().out.strip() == "asked: Q-0002"


def test_raise_blocker_prints_the_allocated_blocker_id(env, capsys):
    capsys.readouterr()
    assert env.run("raise-blocker", "--actor", "jr-dev-1", "--description", "db down") == 0
    assert capsys.readouterr().out.strip() == "raised: B-0001"


def test_refused_ask_prints_no_id(env, capsys):
    capsys.readouterr()
    assert env.run("ask", "--actor", "jr-dev-1", "--urgency", "waiting", "--question", "Q?",
                   "--default", "-") == 2
    assert "asked:" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# gh#650 -- note --strike --match must hit exactly one live line
# --------------------------------------------------------------------------- #
def _three_deploy_notes(env):
    assert env.run("note", "--actor", "lead-dev", "--to", "fleet", "--text", "deploy window opens at 10") == 0
    assert env.run("note", "--actor", "lead-dev", "--to", "jr-dev-1", "--text", "deploy your PR after rebase") == 0
    assert env.run("note", "--actor", "steward", "--to", "jr-dev-1", "--text", "deploy note two for jr1") == 0


def test_strike_refuses_an_ambiguous_match_and_names_the_candidates(env, capsys):
    _three_deploy_notes(env)
    capsys.readouterr()
    err = _refused(env, capsys, "note_strike_ambiguous", "note", "--actor", "jr-dev-1", "--strike",
                   "--match", "deploy")
    assert "matched 3 live Notes" in err
    for text in ("deploy window opens at 10", "deploy your PR after rebase", "deploy note two for jr1"):
        assert text in err


def test_strike_with_a_unique_match_strikes_it_and_prints_it(env, capsys):
    _three_deploy_notes(env)
    capsys.readouterr()
    assert env.run("note", "--actor", "jr-dev-1", "--strike", "--match", "after rebase") == 0
    out = capsys.readouterr().out.strip()
    assert re.fullmatch(r"struck: \[[^\]]+\] lead-dev -> jr-dev-1: deploy your PR after rebase", out), out
    text = env.text()
    assert _struck(text, "lead-dev -> jr-dev-1: deploy your PR after rebase")
    assert _live(text, "lead-dev -> fleet: deploy window opens at 10")
    assert _live(text, "steward -> jr-dev-1: deploy note two for jr1")


def test_strike_ignores_candidates_the_actor_may_not_strike(env, capsys):
    # A match on another lane's Note is not a candidate: it could never be the one meant.
    assert env.run("note", "--actor", "lead-dev", "--to", "jr-dev-2", "--text", "deploy for jr2") == 0
    assert env.run("note", "--actor", "lead-dev", "--to", "jr-dev-1", "--text", "deploy for jr1") == 0
    capsys.readouterr()
    assert env.run("note", "--actor", "jr-dev-1", "--strike", "--match", "deploy for") == 0
    text = env.text()
    assert _struck(text, "lead-dev -> jr-dev-1: deploy for jr1")
    assert _live(text, "lead-dev -> jr-dev-2: deploy for jr2")


def test_strike_of_byte_identical_duplicates_strikes_exactly_one(env, capsys, monkeypatch):
    # Same stamp + same text: no --match can ever separate them, so ambiguity would strand both.
    monkeypatch.setattr(registry, "_now_iso", lambda: "2026-01-01T00:00Z")
    for _ in range(2):
        assert env.run("note", "--actor", "lead-dev", "--to", "jr-dev-1", "--text", "rebase now") == 0
    capsys.readouterr()
    assert env.run("note", "--actor", "jr-dev-1", "--strike", "--match", "rebase now") == 0
    line = "[2026-01-01T00:00Z] lead-dev -> jr-dev-1: rebase now"
    lines = env.text().splitlines()
    assert sum(ln.strip() == f"- ~~{line}~~" for ln in lines) == 1, lines
    assert sum(ln.strip() == f"- {line}" for ln in lines) == 1, lines


def test_strike_refuses_text_alongside_strike(env, capsys):
    assert env.run("note", "--actor", "lead-dev", "--to", "jr-dev-1", "--text", "one note") == 0
    capsys.readouterr()
    _refused(env, capsys, "note_strike_text", "note", "--actor", "jr-dev-1", "--strike",
             "--match", "one note", "--text", "done")


# --------------------------------------------------------------------------- #
# gh#655 -- refusals name the flag that was wrong, cite a rule, and help says what is required
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "args, flag, value",
    [
        (("note", "--actor", "lead-dev", "--to", "nobody", "--text", "hi"), "--to", "nobody"),
        (("reap", "--actor", "steward", "--agent", "ghost"), "--agent", "ghost"),
        (("stand-down", "--actor", "steward", "--agent", "ghost", "--reason", "r"), "--agent", "ghost"),
        (("raise-blocker", "--actor", "jr-dev-1", "--description", "x", "--owner", "nobody"),
         "--owner", "nobody"),
    ],
    ids=["note-to", "reap-agent", "stand-down-agent", "raise-blocker-owner"],
)
def test_unknown_agent_refusal_names_the_real_flag(env, capsys, args, flag, value):
    err = _refused(env, capsys, "agent_known", *args)
    assert f"{flag} {value!r}" in err
    assert "--actor" not in err
    assert f"actor {value!r}" not in err


def test_assign_blocker_unknown_owner_names_owner(env, capsys):
    assert env.run("raise-blocker", "--actor", "jr-dev-1", "--description", "x") == 0
    capsys.readouterr()
    err = _refused(env, capsys, "agent_known", "assign-blocker", "--actor", "steward", "--id", "B-0001",
                   "--owner", "nobody2")
    assert "--owner 'nobody2'" in err and "--actor" not in err


def test_unknown_actor_refusal_still_names_actor(env, capsys):
    err = _refused(env, capsys, "agent_known", "status", "--actor", "nobody", "--state", "idle")
    assert "--actor 'nobody'" in err


def test_withdraw_refusal_reads_withdrawn(env, capsys):
    assert env.run("publish", "--actor", "lead-dev", "--slug", "gamma", "--title", "g", "--lane", "jr-dev-1") == 0
    assert env.run("withdraw", "--actor", "lead-dev", "--id", "E-0001-gamma", "--reason", "dup") == 0
    capsys.readouterr()
    err = _refused(env, capsys, "queue_revise_ready", "withdraw", "--actor", "lead-dev",
                   "--id", "E-0001-gamma", "--reason", "again")
    assert "can be withdrawn" in err
    assert "withdrawd" not in err


@pytest.mark.parametrize("key", ["eta_required", "heartbeat_only_exclusive"])
def test_status_refusal_keys_cite_a_registry_rule(key):
    assert RULES.get(key, "").startswith("REGISTRY.md #Agents: ")


def test_eta_required_refusal_has_the_documented_shape(env, capsys):
    err = _refused(env, capsys, "eta_required", "status", "--actor", "jr-dev-1", "--state", "building")
    assert err.startswith("refused [eta_required]: REGISTRY.md #Agents: ")


def test_heartbeat_only_exclusive_refusal_has_the_documented_shape(env, capsys):
    err = _refused(env, capsys, "heartbeat_only_exclusive", "status", "--actor", "jr-dev-1",
                   "--heartbeat-only", "--state", "idle")
    assert err.startswith("refused [heartbeat_only_exclusive]: REGISTRY.md #Agents: ")


def test_status_help_explains_eta_and_waiting_on(capsys):
    with pytest.raises(SystemExit):
        main(["status", "--help"])
    out = capsys.readouterr().out
    # The options block, not the usage line, which names every flag once with no help.
    help_text = " ".join(out[out.index("options:"):].split())
    eta = help_text[help_text.index("--eta ETA"):help_text.index("--waiting-on WAITING_ON")]
    assert "required unless --state halted" in eta and "ISO-8601" in eta
    waiting = help_text[help_text.index("--waiting-on WAITING_ON"):]
    assert "live Q-/B- id" in waiting
    for flag in ("--work WORK", "--phase PHASE"):
        after = help_text[help_text.index(flag) + len(flag):].lstrip()
        assert not after.startswith("--"), f"{flag} has no help text"


def test_registry_doc_lane_lists_name_every_lane():
    doc = DOC.read_text(encoding="utf-8")
    lane_bullet = next(ln for ln in doc.splitlines() if ln.startswith("- `lane`: "))
    publish_row = next(ln for ln in doc.splitlines() if "publish --actor lead-dev --slug" in ln
                       and ln.startswith("|"))
    for line in (lane_bullet, publish_row):
        for lane in LANES:
            assert re.search(rf"(?<![\w-]){re.escape(lane)}(?![\w-])", line), (lane, line)


# --------------------------------------------------------------------------- #
# gh#661 -- './notes/x.md' is normalized to the spelling cull collects
# --------------------------------------------------------------------------- #
def test_dot_slash_overflow_pointer_is_normalized_and_collected(env, capsys):
    env.notes_dir.mkdir()
    (env.notes_dir / "c.md").write_text("C\n", encoding="utf-8")
    assert env.run("note", "--actor", "lead-dev", "--to", "jr-dev-2", "--text",
                   "decision: do C, see ./notes/c.md") == 0
    text = env.text()
    assert _live(text, "lead-dev -> jr-dev-2: decision: do C, see notes/c.md")
    assert "./notes/" not in text
    assert env.run("note", "--actor", "jr-dev-2", "--strike", "--match", "notes/c.md") == 0
    assert env.run("cull", "--actor", "steward") == 0
    assert not (env.notes_dir / "c.md").exists()
    assert "C\n" in env.log_path.read_text(encoding="utf-8")


def test_dot_dot_slash_pointer_is_left_alone(env, capsys):
    assert env.run("note", "--actor", "lead-dev", "--to", "jr-dev-2", "--text",
                   "outside the store: ../notes/c.md") == 0
    assert _live(env.text(), "lead-dev -> jr-dev-2: outside the store: ../notes/c.md")


def test_note_size_guard_measures_the_normalized_text(env, capsys):
    # Over the cap only because of the "./" that normalization drops: accepted.
    pointer = f"{NOTE_OVERFLOW_DIR}/x.md"
    fill = "a" * (NOTE_MAX_BYTES - len(pointer) - 1)
    dotted = f"{fill} ./{pointer}"
    assert len(dotted.encode("utf-8")) > NOTE_MAX_BYTES
    assert env.run("note", "--actor", "lead-dev", "--to", "jr-dev-2", "--text", dotted) == 0
    assert _live(env.text(), f"lead-dev -> jr-dev-2: {fill} {pointer}")
    # The same byte length spelled without "./" really is over the cap.
    plain = f"{fill}aa {pointer}"
    assert len(plain.encode("utf-8")) == len(dotted.encode("utf-8"))
    _refused(env, capsys, "note_too_long", "note", "--actor", "lead-dev", "--to", "jr-dev-2",
             "--text", plain)
