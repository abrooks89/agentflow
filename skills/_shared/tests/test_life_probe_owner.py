"""The steward skill and the registry doc must name the same seat as owner of the life probe.

gh#461: steward/SKILL.md told the steward "you run no life probe — that probe is dev-manager's"
while docs/REGISTRY.md said "the life probe and the reap are BOTH the steward's since E-0220",
and the code (`ACTOR_CAPABILITY["reap"]`) agreed with the doc. A loop reads its own SKILL, so
the steward was told to leave a job it alone can do. The same skill said "Ping on first sight"
without a command, so nothing in the fleet wrote the ping `_has_live_stale_note` consumes, and
the second-sighting reap could never become eligible.

These gates pin both halves: one owner, named identically in both files and in the code, and a
runnable ping whose text the predicate actually matches.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parent

# Same idiom as test_skill_call_sites.py: prefer the tree under test over an editable install.
sys.path.insert(0, str(SKILLS_ROOT / "_shared"))

from agentflow.registry import ACTOR_CAPABILITY, AGENTS  # noqa: E402

_BULLET_HEAD = "- **A measurement, not a liveness verdict.**"
_SEATS = "|".join(re.escape(a) for a in sorted(AGENTS, key=len, reverse=True))
# The sentence that mentions the life probe: from the previous full stop to the next one.
_PROBE_SENTENCE = re.compile(r"[^.]*\blife probe\b[^.]*")
_SEAT = re.compile(rf"\b({_SEATS})\b")
_OWNER = re.compile(rf"\b({_SEATS})'s\b")
_PING_ANCHOR = "Ping on first sight"
_PING_END = "**Probe before you"
_NOTE_CMD = re.compile(r"python -m agentflow\.registry note\b[^\n`]*")
_TEXT_ARG = re.compile(r'--text\s+"([^"]*)"')
_TO_ARG = re.compile(r"--to\s+(\S+)")
# `_cmd_note` prepends "<actor> -> <target>: "; budget the longest real target.
_PREFIX_BYTES = len(f"steward -> {max(AGENTS, key=len)}: ".encode("utf-8"))
_NOTE_CAP_BYTES = 300


def _read(path: Path) -> str:
    # FAIL, never skip: a moved file must not turn this gate into one that checks nothing.
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} not found - moved, or this gate is stale"
    return path.read_text(encoding="utf-8", errors="replace")


def _bullets(text: str) -> list[str]:
    """Every cadence-report 'measurement' bullet, flattened to one line each."""
    lines = text.splitlines()
    found: list[str] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith(_BULLET_HEAD):
            body = [line.strip()]
            for nxt in lines[i + 1:]:
                if not nxt.strip() or nxt.lstrip().startswith("- "):
                    break
                body.append(nxt.strip())
            found.append(" ".join(body))
    return found


def _owner(rel: str) -> str:
    bullets = _bullets(_read(REPO_ROOT.joinpath(*rel.split("/"))))
    assert len(bullets) == 1, (
        f"{rel}: expected exactly one {_BULLET_HEAD!r} bullet, found {len(bullets)} - "
        "the locator is stale, so this gate would compare nothing"
    )
    sentences = _PROBE_SENTENCE.findall(bullets[0])
    assert len(sentences) == 1, (
        f"{rel}: expected one sentence naming the life probe in the cadence-report bullet, "
        f"found {len(sentences)}: {bullets[0]}"
    )
    sentence = sentences[0]
    assert not re.search(r"\bno life probe\b", sentence), (
        f"{rel}: the cadence-report bullet denies running the life probe: {sentence}"
    )
    # Every seat the sentence mentions, possessive or not: a sentence that hands the probe to
    # one seat and ends on another seat's possessive names two, and must not pass as one owner.
    named = set(_SEAT.findall(sentence))
    owners = _OWNER.findall(sentence)
    assert owners and named == {owners[0]}, (
        f"{rel}: the life-probe sentence must name exactly one seat, as its owner; "
        f"it names {sorted(named)} with possessives {owners}: {sentence}"
    )
    return owners[0]


def test_the_steward_skill_and_the_registry_doc_name_the_same_life_probe_owner() -> None:
    skill = _owner("skills/steward/SKILL.md")
    doc = _owner("docs/REGISTRY.md")
    assert skill == doc, (
        f"steward/SKILL.md says the life probe is {skill}'s, docs/REGISTRY.md says {doc}'s - "
        "the loop that reads its own SKILL is told a different owner than the binding doc"
    )


def test_the_named_owner_is_the_only_actor_the_code_lets_reap() -> None:
    actors = ACTOR_CAPABILITY["reap"].actors
    assert len(actors) == 1, f"reap is granted to {actors}; this gate assumes one owner"
    for rel in ("skills/steward/SKILL.md", "docs/REGISTRY.md"):
        assert _owner(rel) == actors[0], (
            f"{rel} names {_owner(rel)} as life-probe owner but only {actors[0]} may reap"
        )


def _ping_passage() -> str:
    text = _read(SKILLS_ROOT / "steward" / "SKILL.md")
    assert text.count(_PING_ANCHOR) == 1, (
        f"steward/SKILL.md: expected exactly one {_PING_ANCHOR!r}, found {text.count(_PING_ANCHOR)}"
    )
    start = text.index(_PING_ANCHOR)
    end = text.find(_PING_END, start)
    assert end != -1, f"steward/SKILL.md: no {_PING_END!r} after {_PING_ANCHOR!r}"
    return text[start:end]


def test_the_steward_ping_is_a_runnable_note_the_stale_predicate_matches() -> None:
    passage = _ping_passage()
    cmds = _NOTE_CMD.findall(passage)
    assert len(cmds) == 1, (
        f"the 'Ping on first sight' passage carries {len(cmds)} `registry note` commands, not one - "
        "without a command nothing writes the ping and no stale row ever becomes reap-eligible"
    )
    cmd = cmds[0]
    assert "--actor steward" in cmd, f"ping is not written as steward: {cmd}"
    assert "--project <name>" in cmd, f"ping is not project-scoped: {cmd}"
    to = _TO_ARG.search(cmd)
    assert to, f"ping has no --to, so it is fleet-wide and `_has_live_stale_note` cannot see it: {cmd}"
    # Any fixed seat here would route every ping to that one lane, so the predicate would only
    # ever see pings about it.
    assert to.group(1) == "<lane>", (
        f"ping must be routed --to the OBSERVED lane (`--to <lane>`), not {to.group(1)}: {cmd}"
    )
    text = _TEXT_ARG.search(cmd)
    assert text, f"ping has no --text: {cmd}"
    assert "stale" in text.group(1).lower(), (
        f"ping text lacks 'stale', so `_has_live_stale_note` never matches it: {text.group(1)!r}"
    )
    size = _PREFIX_BYTES + len(text.group(1).encode("utf-8"))
    assert size <= _NOTE_CAP_BYTES, f"ping renders to ~{size} bytes, over the {_NOTE_CAP_BYTES}-byte cap"


def test_the_ping_passage_says_second_sighting_probes_then_reaps() -> None:
    flat = " ".join(_ping_passage().split())
    assert "second sighting" in flat and "life probe" in flat, (
        "the ping passage does not say the second sighting runs the life probe"
    )
    assert re.search(r"registry reap --actor steward --project <name> --agent <lane>", flat), (
        "the ping passage does not give the reap command for the second sighting"
    )
