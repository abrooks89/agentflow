"""The /project skill's missing-`project:` ask offers installed projects only.

Nothing provisions a directory for a `personal` or `other` bucket, so a spec routed there lands
nowhere; the backlog skill offers neither. /project asks the same question when a spec has no
`project:` field, so it offers neither too, and a reply naming no installed project falls back
to the current project.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "project" / "SKILL.md"


def _ask_line() -> str:
    text = SKILL.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if "Spec has no `project:` field" in ln]
    assert len(lines) == 1, "expected exactly one missing-project ask in the /project skill"
    return lines[0]


def test_missing_project_ask_offers_installed_projects_only():
    ask = _ask_line()
    assert "installed `~/.agentflow/config/projects/*.md` names" in ask
    assert not re.search(r"`(personal|other)`", ask), ask


def test_project_skill_offers_no_fallback_bucket_anywhere():
    text = SKILL.read_text(encoding="utf-8")
    assert "personal" not in text.lower()
    assert not re.search(r"`other`", text)


def test_unmatched_reply_falls_back_to_the_current_project():
    ask = _ask_line()
    assert "names no installed project" in ask
    assert "resolve_project_name(<project-arg>)" in ask
