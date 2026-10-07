"""The registry docs say what the CLI's ownership checks are, and no more (gh#1173).

`--actor` is a free argument: `_require_agent` and `_require_capability` check the role a caller
NAMES, so `answer --actor dev-manager --by human` writes `answered_by: human` from any shell.
REGISTRY.md called that "CODE-enforced, not advisory", and "ownership is enforced per command",
which reads as access control. The docs now say the CLI checks a declared role and does not
prove identity, and README's Security section says so in one line. This pins both.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_DOC = REPO_ROOT / "docs" / "REGISTRY.md"
README = REPO_ROOT / "README.md"


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^## |\Z)", text, flags=re.S | re.M)
    assert match, f"no {heading!r} section"
    return match.group(1)


def test_the_ownership_table_no_longer_calls_the_human_answer_code_enforced() -> None:
    table = _section(REGISTRY_DOC.read_text(encoding="utf-8"), "## Ownership summary (single writer per field)")
    assert "CODE-enforced, not advisory" not in table
    assert "self-declared" in table


def test_the_command_surface_says_the_cli_does_not_prove_identity() -> None:
    text = REGISTRY_DOC.read_text(encoding="utf-8")
    assert "ownership is enforced per command" not in text
    start = text.index("**Command surface**")
    lead = text[start : text.index("| write | command |", start)]
    assert "does not prove identity" in lead, lead
    assert "--actor" in lead


def test_readme_security_names_declared_roles() -> None:
    security = _section(README.read_text(encoding="utf-8"), "## Security & blast radius")
    assert "Registry roles are declared, not proven" in security
