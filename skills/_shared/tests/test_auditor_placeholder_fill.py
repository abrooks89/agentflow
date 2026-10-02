"""gh#724: every placeholder the audit-agent contract takes is one the auditor's Step 2 fills.

The subagent sees only the filled contract, so a placeholder the caller never fills reaches it
empty. The case that prompted this: the contract had no interpreter placeholder at all, so a
test-suite audit ran its probe under whatever `python` was on PATH, which in a fleet session is
the agentflow suite venv rather than the product's.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = REPO_ROOT / "skills" / "auditor" / "references" / "audit-agent.md"
SKILL = REPO_ROOT / "skills" / "auditor" / "SKILL.md"

_BULLET_PLACEHOLDER = re.compile(r"^\s*- `\{([a-z_]+)\}`", re.MULTILINE)


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


def _contract_placeholders() -> set[str]:
    text = CONTRACT.read_text(encoding="utf-8")
    return set(_BULLET_PLACEHOLDER.findall(
        _section(text, "Caller-supplied context", "## Part 1")))


def _step2_placeholders() -> set[str]:
    text = SKILL.read_text(encoding="utf-8")
    return set(_BULLET_PLACEHOLDER.findall(_section(text, "## Step 2 ", "## Step 4 ")))


def test_the_contract_takes_the_product_interpreter():
    assert "product_python" in _contract_placeholders()


def test_step_2_fills_every_placeholder_the_contract_declares():
    declared = _contract_placeholders()
    assert len(declared) >= 7, f"the placeholder parse looks broken: {sorted(declared)}"
    unfilled = declared - _step2_placeholders()
    assert not unfilled, f"the contract declares placeholders Step 2 never fills: {sorted(unfilled)}"


def test_step_0_2_reads_the_product_python_line():
    step02 = _section(SKILL.read_text(encoding="utf-8"), "### 0.2", "### 0.3")
    assert "[product python]" in step02


def test_probe_commands_are_sourced_from_the_solution_doc_not_config():
    text = CONTRACT.read_text(encoding="utf-8")
    probes = _section(text, "### `test-suite` / `smoke-cli`", "### `none`")
    assert "configured probes" not in text
    assert "verification.test_commands" in probes
    assert "{product_python}" in probes


def test_an_unset_product_interpreter_runs_under_default_python_not_a_blocker():
    """An unset `canonical_repo.python` is a normal project state. Blocking on it would give
    every epic of such a project a Blockers row and no ledger row, so none is ever judged."""
    probes = _section(CONTRACT.read_text(encoding="utf-8"),
                      "### `test-suite` / `smoke-cli`", "### `none`")
    assert "product interpreter unset; ran under default python" in probes
    assert "is `null` on a Python product" not in probes
    step2 = _section(SKILL.read_text(encoding="utf-8"), "## Step 2 ", "## Step 4 ")
    assert "A `null` is not a blocker" in step2
