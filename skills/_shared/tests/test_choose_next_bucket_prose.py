"""choose-next's bucket prose matches what `blocked_status` computes (gh#704).

Selection is computed by code, but the explanation the operator reads comes from the prose in
Steps 2 and 3. The prose quotes `blocked_status`'s own ordered list, so this pins the quote to
the docstring and pins the ended-board states the list alone does not spell out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow import epic_survey as s

SKILL = Path(__file__).resolve().parents[2] / "choose-next" / "SKILL.md"


def _step(n: int) -> str:
    text = SKILL.read_text(encoding="utf-8")
    body = text.split(f"### Step {n}:", 1)[1]
    return body.split("\n### ", 1)[0]


def _docstring_order() -> list[str]:
    doc = s.blocked_status.__doc__ or ""
    block = doc.split("Evaluated in this order (first match wins):", 1)[1].split("\n\n", 1)[0]
    lines = [ln.strip() for ln in block.strip().splitlines()]
    assert lines and all(ln.startswith("* ") for ln in lines), lines
    return lines


def test_docstring_order_names_the_fall_through():
    # The last bucket is where a done_on_hold parent lands; a list that stops at "unknown"
    # leaves the reader to guess.
    assert "done_on_hold" in _docstring_order()[-1]
    assert "CHAINED" in _docstring_order()[-1]


def test_step3_quotes_the_docstring_order_verbatim():
    step3 = [ln.strip() for ln in _step(3).splitlines()]
    order = _docstring_order()
    start = step3.index(order[0]) if order[0] in step3 else -1
    assert start >= 0, f"Step 3 does not quote blocked_status's order; first line: {order[0]!r}"
    assert step3[start : start + len(order)] == order


def test_step2_names_the_ended_board_states():
    step2 = _step(2)
    for state in ("abandoned", "failed", "done_on_hold"):
        assert f"`{state}`" in step2, state


def test_step3_explains_ended_parents_honestly():
    step3 = _step(3)
    assert "will never land" in step3
    assert "warn_stranded_terminal_dependencies" in step3
    assert "awaiting a human merge" in step3
    # The old reading, that every MISSING-DEP is a not-yet-written epic, is gone.
    assert not re.search(r"MISSING-DEP\*\* \(treat as near-ready", step3)


@pytest.mark.parametrize(
    ("parent", "bucket"),
    [("abandoned", "MISSING-DEP"), ("failed", "MISSING-DEP"), ("done_on_hold", "CHAINED")],
)
def test_the_prose_matches_the_computed_bucket(parent, bucket):
    def rec(slug, state, deps=()):
        return s.EpicRecord(
            slug=slug, title=slug, lifecycle_state=state, depends_on=list(deps), project="demo"
        )

    recs = [rec("parent", parent), rec("child", "speccing", ["parent"])]
    assert s.blocked_status(recs[1], recs) == bucket
    blocks = _bucket_explanations()
    assert bucket in blocks, f"Step 3 explains no {bucket} bucket"
    assert f"`{parent}`" in blocks[bucket], f"Step 3 does not put {parent} in {bucket}"
    for other, text in blocks.items():
        if other != bucket:
            assert f"`{parent}`" not in text, f"Step 3 also puts {parent} in {other}"


EXPLANATION = "What each bucket tells the operator:"


def _bucket_explanations() -> dict[str, str]:
    """Maps each bucket to its bullet, sub-bullets included, in Step 3's explanation list.

    Only the text after EXPLANATION counts: the verbatim docstring quote above it names every
    parent on its bucket's line, so a check that read it would pass whatever the explanation says.
    """
    step3 = _step(3)
    assert EXPLANATION in step3, "Step 3 lost its per-bucket explanation"
    tail = step3.split(EXPLANATION, 1)[1].split("\n\n", 1)[0]
    blocks: dict[str, str] = {}
    current = None
    for ln in tail.strip().splitlines():
        m = re.match(r"- \*\*([A-Z-]+)\*\*", ln)
        if m:
            current = m.group(1)
            blocks[current] = ln
        elif current is not None:
            blocks[current] += "\n" + ln
    return blocks
