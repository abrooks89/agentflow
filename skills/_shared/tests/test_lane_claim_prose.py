"""Pin how the junior lanes find their work, and how a seat confirms an open_issue_page raise.

`## Queue` is a markdown table. A junior skill that names its work source as rows "with
`lane: jr-dev-1`" invites a grep for that literal, which matches nothing in a table, so the lane
reports itself empty while claimable rows sit in it. The skills must name the registry's own
reader (`read --claimable <lane>`) and describe `lane` as a column.

The open_issue_page passages tell a seat to raise a profile key; without a read-back step the
seat records an old -> new pair it never observed. `config show` prints the `[work_source]` line
that carries the live value, so each passage must name it.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS_ROOT / "_shared"))

import agentflow.registry as _registry_module  # noqa: E402

JUNIORS = ("jr-dev-1", "jr-dev-2", "jr-dev-3")
OPEN_ISSUE_PAGE_SEATS = ("lead-dev", "steward", "dev-manager")

# A backticked `lane: <value>` reads as a literal to grep for; the table has no such string.
LANE_LITERAL = re.compile(r"`lane: (?:jr-dev-\d|lead)`")
CLAIMABLE_CMD = re.compile(r"`(python -m agentflow\.registry read --claimable [^`]+)`")


def _skill(name: str) -> str:
    return (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")


def _claim_step(text: str) -> str:
    start = text.index("3. **Claim ONE `ready` row from your lane")
    end = text.index("\n4. ", start)
    return text[start:end]


def _work_source_bullet(text: str) -> str:
    start = text.index("  - `## Queue` →")
    end = text.index("\n  - ", start + 1)
    return text[start:end]


def _commands(section: str) -> list[str]:
    # A cited command may wrap across a prose line; the shell sees it as one line.
    return [" ".join(m.split()) for m in CLAIMABLE_CMD.findall(section)]


@pytest.mark.parametrize("lane", JUNIORS)
def test_claim_step_names_read_claimable_for_its_own_lane(lane: str) -> None:
    step = _claim_step(_skill(lane))
    cmds = _commands(step)
    assert any(f"read --claimable {lane} --project <name>" in c for c in cmds), (
        f"{lane} claim step does not name `read --claimable {lane} --project <name>`: {step!r}"
    )
    assert "NEVER claim from another lane" in step, f"{lane} lost the cross-lane claim ban"


@pytest.mark.parametrize("lane", JUNIORS)
def test_work_source_bullet_names_read_claimable_and_the_lane_column(lane: str) -> None:
    bullet = _work_source_bullet(_skill(lane))
    assert any(f"read --claimable {lane}" in c for c in _commands(bullet)), bullet
    assert "`lane` column" in bullet, f"{lane} work-source bullet does not call lane a column"


@pytest.mark.parametrize("lane", JUNIORS)
def test_no_backticked_lane_literal_to_grep_for(lane: str) -> None:
    hits = LANE_LITERAL.findall(_skill(lane))
    assert not hits, f"{lane} still presents a `lane: ...` literal a lane would grep for: {hits}"


def test_lead_dev_has_no_backticked_lane_literal() -> None:
    assert not LANE_LITERAL.findall(_skill("lead-dev"))


@pytest.mark.parametrize("lane", JUNIORS)
def test_cited_claimable_command_parses(lane: str) -> None:
    """The cited command must be one the registry CLI accepts, not plausible-looking prose."""
    cmds = _commands(_skill(lane))
    assert cmds, f"{lane} cites no `read --claimable` command"
    for cmd in cmds:
        argv = shlex.split(cmd.replace("<name>", "otherproj"))[3:]
        ns = _registry_module._build_parser().parse_args(argv)
        assert ns.claimable == lane and ns.project == "otherproj", (cmd, ns)


def _open_issue_page_passage(text: str) -> str:
    start = text.index("raising `work_source.open_issue_page`")
    end = text.index("\n\n", start) if "\n\n" in text[start:] else len(text)
    # Numbered-list passages run on without a blank line; bound them generously.
    return text[start : min(end, start + 1500)]


@pytest.mark.parametrize("seat", OPEN_ISSUE_PAGE_SEATS)
def test_open_issue_page_raise_is_confirmed_with_config_show(seat: str) -> None:
    passage = _open_issue_page_passage(_skill(seat))
    assert "`python -m agentflow.config show --project <name>`" in passage, (
        f"{seat} open_issue_page passage does not confirm the raise with config show: {passage!r}"
    )
    assert "`[work_source]`" in passage, f"{seat} does not say which line carries the value"
