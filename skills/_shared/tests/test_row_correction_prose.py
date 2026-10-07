"""Question and Blocker text is write-once, so the one sanctioned correction is supersede-and-retire.

No verb amends a row's prose: `retract` (raiser only, `open` only, `--reason` required) retires a
question, and `resolve-blocker` (owner only, `open` only, no reason flag) retires a blocker. The
correction is therefore a NEW row first and the old one retired second, with the old row's
retirement naming the new one. A Note filed beside the stale row is not a correction, because a
reader of the row has no reason to go looking for it.

Each seat that raises, owns or routes these rows must say so where it reads the Question and
Blocker rules, and the registry doc must say it in both sections. Pinned per file and per clause,
so a partial deletion names what went missing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

#: The eight files the decision names. A hand-kept list on purpose: the rule is scoped to these
#: seats and the registry doc, not to every file that happens to mention a question.
FILES = (
    "docs/REGISTRY.md",
    "skills/lead-dev/SKILL.md",
    "skills/jr-dev-1/SKILL.md",
    "skills/jr-dev-2/SKILL.md",
    "skills/jr-dev-3/SKILL.md",
    "skills/steward/SKILL.md",
    "skills/auditor/SKILL.md",
    "skills/dev-manager/SKILL.md",
)


def _text(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", FILES)
def test_question_correction_is_supersede_and_retire(rel: str) -> None:
    text = _text(rel)
    assert "supersede-and-retire" in text, f"{rel}: the correction is not named"
    assert 'premise expired: <clause>; superseded by Q-NNNN"' in text, (
        f"{rel}: the retract reason must name the clause and the superseding id"
    )
    assert "cannot be retracted" in text, (
        f"{rel}: an answered question cannot be retracted - a new question is the path"
    )


@pytest.mark.parametrize("rel", FILES)
def test_blocker_correction_is_the_owners(rel: str) -> None:
    text = _text(rel)
    assert "names the old id" in text, f"{rel}: the corrected blocker must name the old id"
    assert "does not own it routes the owner a Note" in text, (
        f"{rel}: a non-owner's path to a false blocker description is not stated"
    )


@pytest.mark.parametrize("rel", FILES)
def test_raise_blocker_descriptions_are_grounded(rel: str) -> None:
    text = _text(rel)
    assert "MERGEABLE/CLEAN as of $(date -u +%H:%MZ) (`gh pr view`)" in text, (
        f"{rel}: the ask grounding rule is not extended to raise-blocker descriptions"
    )


@pytest.mark.parametrize("rel", FILES)
def test_a_note_beside_a_row_is_not_a_correction(rel: str) -> None:
    assert "a note beside a row is not a correction" in _text(rel).lower(), rel


def test_no_amend_verb_or_supersedes_field_is_taught() -> None:
    """The decision is prose only: no `amend` verb and no `supersedes:` field."""
    for rel in FILES:
        text = _text(rel)
        assert "supersedes:" not in text, rel
        assert "registry amend" not in text, rel


@pytest.mark.parametrize("rel", FILES)
def test_corrected_blocker_names_its_owner(rel: str) -> None:
    """`raise-blocker` defaults `--owner` to `-`; the raiser and the owner are often different
    seats, so a correction raised without it lands unowned until a manager assigns it."""
    assert "corrected blocker with `--owner <its own role>`" in _text(rel), (
        f"{rel}: the owner's correction must pass --owner, or the new row is unowned"
    )


#: Seats whose owned-Blocker sweep has an "open this wake" path. No verb edits a Blocker's
#: description and `resolve-blocker` takes no reason, so neither may tell the owner to record
#: anything there.
OWNER_SWEEPS = {
    "skills/lead-dev/SKILL.md": "lead-dev-state.md",
    "skills/dev-manager/SKILL.md": "dev-manager-state.md",
    "skills/steward/SKILL.md": "steward-state.md",
}


@pytest.mark.parametrize("rel", sorted(OWNER_SWEEPS))
def test_owner_sweep_never_writes_a_blocker_description(rel: str) -> None:
    text = _text(rel)
    for stale in (
        "in the description so the next wake",
        "note what's pending in its\ndescription",
        "(record the how/when)",
        "B-NNNN`) with the disposition",
    ):
        assert stale not in text, f"{rel}: teaches a write no verb performs: {stale!r}"
    assert "no verb edits a Blocker's description" in text, rel
    assert OWNER_SWEEPS[rel] in text, rel
