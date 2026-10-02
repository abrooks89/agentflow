"""Every merging lane carries the deploy-lock ordering rule on its own acquire path (gh#167).

The deploy lock is mutual exclusion only: `registry.py` keeps no queue, so a freed lock goes to
whichever lane polls first, and a lane that waited an hour with a merge-ready PR can lose it to
one whose CI is still running. The fix is a convention, not code: a lane refused `deploy_held`
writes `lock-wait since <ISO>` into its Agents phase, and a lane about to take a FREE lock yields
to an earlier live marker. A convention only works if every lane that takes the lock reads it, so
this pins the rule into each merging lane's SKILL.md and into docs/REGISTRY.md.

WHAT IT CAN PROVE: each lane's file states the marker and the reason it exists (the lock is
"mutual exclusion, not a queue"); docs/REGISTRY.md states the marker and cites gh#167.
WHAT IT CANNOT PROVE: that any lane actually yields at a live acquire.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MERGING_LOOPS = ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3")
MARKER = "lock-wait since"


@pytest.mark.parametrize("loop", MERGING_LOOPS)
def test_each_merging_lane_states_the_ordering_rule(loop):
    text = (REPO_ROOT / "skills" / loop / "SKILL.md").read_text(encoding="utf-8")
    assert MARKER in text, f"{loop}/SKILL.md lost the deploy-lock ordering marker"
    assert "mutual exclusion, not a queue" in text, (
        f"{loop}/SKILL.md states the marker but not its reason (the lock is mutual exclusion, "
        "not a queue)"
    )


def test_registry_doc_states_the_ordering_rule():
    text = (REPO_ROOT / "docs" / "REGISTRY.md").read_text(encoding="utf-8")
    assert MARKER in text and "gh#167" in text
