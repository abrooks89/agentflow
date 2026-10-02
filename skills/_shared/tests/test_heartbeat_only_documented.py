"""Every loop told to re-stamp on a ceiling must also be told the honest way to do it.

`status --heartbeat-only` exists so a loop can prove liveness WITHOUT re-asserting
`state`/`work`/`phase`/`eta`/`waiting_on`. Without it, satisfying the 30-minute ceiling means
re-sending every column, so an automated re-stamp carrying a stale `--phase` writes a fresh
timestamp over work that already finished — a row worse than a stale one, because a stale row
is visibly stale while a fresh row asserting dead work reads as ground truth. That happened in
this fleet: a monitor advertised two exited build agents for ~10 minutes on a healthy timestamp.

The flag shipped as code with ZERO prose anywhere (`grep heartbeat-only *.md` returned nothing),
which is the shape this repo has been bitten by repeatedly — a capability nothing tells anyone
to use. This module is the gate for the prose half.

WHAT THIS GATE CAN AND CANNOT DO — stated because a gate that overstates itself is worse than
none (issue #28 is exactly that mistake, made in this repo, by the author of this file):

* It CAN prove that a skill instructing a 30-minute refresh also NAMES `--heartbeat-only`
  somewhere in the same file. That is enough to stop the flag being silently undocumented, and
  enough to fail if someone deletes the paragraph wholesale.
* It CANNOT prove the mention is near the ceiling text, correct, or that a loop will follow it.
  This is a file-level check. Do not read a pass here as "the prose is right".
"""

from __future__ import annotations

from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[2]

FLAG = "--heartbeat-only"

# The phrase every loop uses for the ceiling. A skill that says this is a skill that will
# re-stamp, and therefore needs to know the honest form.
CEILING_MARKER = "every 30 minutes"

# Loops that re-stamp their own row under the ceiling. dev-manager is deliberately ABSENT:
# its heartbeat is refreshed as a side effect of writing a Directive (heartbeat cell only —
# the same semantic this flag provides), so it never runs a ceiling re-stamp of its own.
REFRESHING_LOOPS = ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor", "steward")


def _skill_text(name: str) -> str:
    path = SKILLS_ROOT / name / "SKILL.md"
    # FAIL, never skip: a renamed skill would silently stop being guarded.
    assert path.is_file(), f"{name}/SKILL.md not found at {path} — renamed, or this list is stale"
    return path.read_text(encoding="utf-8", errors="replace")


def _flat(text: str) -> str:
    """Collapse whitespace so a phrase matches regardless of line wrapping.

    These are hand-wrapped markdown files: `lead-dev` writes "at least every\\n     30 minutes",
    so a naive substring search for the phrase fails on the one file that most needed gating.
    Caught by this gate on its first run against its own targets.
    """
    return " ".join(text.split())


@pytest.mark.parametrize("skill", REFRESHING_LOOPS)
def test_a_loop_told_to_restamp_is_told_how_to_restamp_honestly(skill: str) -> None:
    text = _flat(_skill_text(skill))
    assert CEILING_MARKER in text, (
        f"{skill}/SKILL.md no longer contains {CEILING_MARKER!r}. If the ceiling moved or was "
        "reworded, update CEILING_MARKER here — do not let this gate pass vacuously on a file "
        "that no longer says what it is being gated for."
    )
    assert FLAG in text, (
        f"{skill}/SKILL.md instructs a {CEILING_MARKER} re-stamp but never names {FLAG}. A loop "
        "following it will re-send every column, and any stale --phase it carries becomes a "
        "fresh timestamp over finished work."
    )


def test_the_flag_this_gate_guards_actually_exists() -> None:
    """The prose is only worth gating if the CLI really accepts it.

    Without this, deleting the flag from `registry.py` would leave five skills documenting a
    command that errors, and every assertion above would still pass — the gate would be
    protecting a fiction.
    """
    registry = (SKILLS_ROOT / "_shared" / "agentflow" / "registry.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert '"--heartbeat-only"' in registry, (
        "registry.py no longer registers --heartbeat-only, but five SKILL.md files tell loops "
        "to use it. Fix the CLI or remove the prose — do not leave them disagreeing."
    )
