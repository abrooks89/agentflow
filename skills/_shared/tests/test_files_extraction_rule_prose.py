"""The `## Files` extraction rule must be stated where the promoting seat and a spec author read.

`plan_parser.files_section` extracts only backticked paths, so an un-backticked entry declares
nothing: the section is `present` and not `seam_checkable`. A rule that lives only in a regex
comment is a rule nobody applies while writing a spec, so each reader's surface is pinned here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from agentflow import plan_parser

SHARED = Path(__file__).resolve().parents[1]
SKILLS = SHARED.parent

_RULE = re.compile(r"one\s+backticked,\s+repo-relative\s+path\s+per\s+(list\s+)?entry", re.I)
_NOTHING = re.compile(r"un-backticked\s+entry\s+declares\s+nothing", re.I)


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i: text.index(end, i + len(start))]


def _dev_manager_7c() -> str:
    text = (SKILLS / "dev-manager" / "SKILL.md").read_text(encoding="utf-8")
    return _section(text, "**7c. Promote", "**CAPABILITY CHECK")


def _schema_files_rule() -> str:
    text = (SHARED / "SCHEMA.md").read_text(encoding="utf-8")
    return _section(text, "#### The spec BODY precondition: a `## Files` section", "`queue_class` is")


@pytest.mark.parametrize(
    "where, read",
    [
        ("dev-manager SKILL.md 7c", _dev_manager_7c),
        ("SCHEMA.md Files precondition", _schema_files_rule),
        ("plan_parser.files_section docstring", lambda: plan_parser.files_section.__doc__ or ""),
    ],
)
def test_the_extraction_rule_is_stated(where: str, read) -> None:
    text = read()
    assert _RULE.search(text), f"{where} does not state: one backticked, repo-relative path per entry"
    assert _NOTHING.search(text), f"{where} does not state: an un-backticked entry declares nothing"


def test_the_stated_rule_matches_the_parser() -> None:
    """The prose and the regex agree: backticked counts, un-backticked does not."""
    bare = plan_parser.files_section("## Files\n- src/widget.py (new)\n")
    ticked = plan_parser.files_section("## Files\n- `src/widget.py` (new)\n")
    assert bare.present and bare.paths == () and not bare.seam_checkable
    assert ticked.paths == ("src/widget.py",) and ticked.seam_checkable
