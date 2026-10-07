"""Prose pins for three autodoc runbook rules that no code path enforces.

1. The gate-triage section of /autodoc-update forks three ways, and the third fork is the
   migration case: a live tree that carries earlier graph-check --fix repairs fails link-recall
   on pages no agent touched, and only an $AUTODOC vs $RENDER_TARGET diff can show it.
2. The taxonomy names the MISSING diagram class next to STALE, and states that fixable
   graph-check findings are fatal on the update path while init only warns.
3. AUTODOC_PROJECT is exported in the same shell command as Step 1's block, never as a
   standalone fenced step that a fresh shell then forgets.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
UPDATE = SKILLS / "autodoc-update" / "SKILL.md"
TAXONOMY = SKILLS / "autodoc-init" / "references" / "taxonomy.md"


def _flat(s: str) -> str:
    return " ".join(s.split())


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


def _fenced_bash_blocks(text: str) -> list[str]:
    out, buf, inside = [], [], False
    for line in text.split("\n"):
        s = line.strip()
        fence = s.startswith("```")
        if not inside and fence and s[3:].strip().lower() in ("bash", "sh", "shell"):
            inside, buf = True, []
            continue
        if inside and fence:
            out.append("\n".join(buf))
            inside = False
            continue
        if inside:
            buf.append(line)
    return out


# --- 1. the third triage fork -------------------------------------------------------------

def _triage() -> str:
    text = UPDATE.read_text(encoding="utf-8")
    return _section(text, "### The gate failed on a page. Triage BEFORE you repair anything.",
                    "### Repairing a record")


def _fork_bullets(section: str) -> list[str]:
    # A fork is a top-level bullet whose lead is bold; continuation lines are indented.
    bullets, cur = [], None
    for line in section.split("\n"):
        if line.startswith("- **"):
            cur = [line]
            bullets.append(cur)
        elif cur is not None and line.startswith("  ") and line.strip():
            cur.append(line)
        else:
            cur = None
    return [_flat(" ".join(b)) for b in bullets]


def test_triage_section_has_three_forks() -> None:
    forks = _fork_bullets(_triage())
    assert len(forks) == 3, f"expected three triage forks, found {len(forks)}: {[f[:60] for f in forks]}"
    assert forks[0].startswith("- **The record already carries the defect.**")
    assert forks[1].startswith("- **The record is clean and the rendered page is not.**")


def test_third_fork_diffs_the_live_tree_against_the_render() -> None:
    fork = _fork_bullets(_triage())[-1]
    assert "$AUTODOC/<page>" in fork and "$RENDER_TARGET/<page>" in fork, (
        "the migration fork must diff the live page against the rendered page"
    )
    assert "--fix" in fork and "link-recall" in fork, "the fork must name earlier --fix repairs failing link-recall"
    assert "copy-back" in fork, "the fork must say why the repair persists: the gate stops before copy-back"


def test_third_fork_explains_why_the_record_diff_cannot_see_it() -> None:
    fork = _fork_bullets(_triage())[-1]
    assert "$RUN_DIR/records" in fork and "no record" in fork, (
        "the fork must say a page no agent touched has no record this run, so the record-vs-render "
        "diff has nothing to compare"
    )


def test_third_fork_routes_through_the_repair_procedure() -> None:
    fork = _fork_bullets(_triage())[-1]
    assert "Repairing a record" in fork, "the fork must point at the existing repair procedure"
    assert "$SRC" in fork, "the carried repair must be verified against the source"
    assert "re-enter Step 4.5" in fork


# --- 2. taxonomy: MISSING class and the update-side fatal fixable ------------------------

def _taxonomy() -> str:
    return TAXONOMY.read_text(encoding="utf-8")


def test_taxonomy_staleness_names_the_missing_class() -> None:
    passage = _flat(_section(_taxonomy() + "\n---", "**Staleness (currency):**", "\n---"))
    assert "MISSING" in passage and "STALE" in passage, passage
    assert "flow" in passage and "data-model" in passage
    assert "8000" in passage, "the staleness budget formula must be stated"
    assert "exits 0" in passage, "the report must be stated as advisory (exit 0)"


def test_taxonomy_states_fixable_findings_are_fatal_on_update() -> None:
    graph = _flat(_section(_taxonomy(), "## Graph integrity (graph-reviewer)", "## Diagram Gate"))
    assert "--fail-on-fixable" in graph, "the update path's graph-check flag must be named"
    assert "/autodoc-update" in graph and "Step 4.5" in graph
    assert "/autodoc-init" in graph and "Step 5a" in graph
    assert re.search(r"fixable[^.]*\*\*fatal\*\*", graph, re.I), (
        "the taxonomy must say fixable findings are fatal on the update path"
    )


# --- 3. AUTODOC_PROJECT export lives in Step 1's own command -----------------------------

def _step1() -> str:
    text = UPDATE.read_text(encoding="utf-8")
    return _section(text, "## Step 1 — Config preamble", "## Step 2 — ")


def test_no_standalone_export_step() -> None:
    step1 = _step1()
    assert "export it first" not in step1
    for block in _fenced_bash_blocks(step1):
        code = [ln for ln in block.split("\n") if ln.strip() and not ln.strip().startswith("#")]
        assert not (len(code) == 1 and code[0].strip().startswith("export AUTODOC_PROJECT")), (
            "a fenced block holding only the export runs in its own shell and is lost before Step 1"
        )


def test_export_is_the_first_line_of_the_step1_command() -> None:
    step1 = _step1()
    flat = _flat(step1)
    assert "first line of the same shell command that runs" in flat
    assert "separate call does not survive" in flat
    first = _fenced_bash_blocks(step1)[0]
    assert "rm -f /tmp/autodoc-env.current" in first, "Step 1's first fence must be the config block"
    head = first.split("\n")[0]
    assert "export AUTODOC_PROJECT=<name>" in head, (
        "the Step 1 command must open by naming the export that is prepended to it"
    )
