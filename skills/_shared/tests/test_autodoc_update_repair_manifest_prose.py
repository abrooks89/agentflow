"""Pins for three autodoc runbook passages that drifted from the code they describe.

1. The migration fork of the /autodoc-update gate triage sends the operator to write a repair
   record for a page that had NO record this run - a NEW file in $RUN_DIR/records. `graph records
   --manifest` refuses any *.json on disk that the manifest does not list, and the manifest is
   written only by Step 4.4's block. So the fork and "Repairing a record" must route the new file
   through Step 4.4's block, and the "not by hand" rule must name that as its one exception.
   The behavioural half runs Step 4.4's own python against the real records loader.
2. The taxonomy's staleness lead-in must describe what diagram-staleness.js compares: git history
   between diagram_source_sha and last_sha, not one sha against another.
3. The taxonomy's blocker-gate sentence must not say the run fails "after --fix": neither init
   Step 5a nor update Step 4.5 passes --fix. It must name every blocker kind graph-check's
   classify() emits, and say that Step 4.5's --fail-on-fixable makes a fixable finding fatal.
4. "Repairing a record" must say where each carried link goes (frontmatter, body, or an edges
   entry) and which render-stamped keys to strip. The end-to-end run of that migration case lives
   in the graph suite (tests/test_repair_record_e2e.py), since the graph CLI needs networkx.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2]
UPDATE = SKILLS / "autodoc-update" / "SKILL.md"
TAXONOMY = SKILLS / "autodoc-init" / "references" / "taxonomy.md"
GRAPH_PKG = SKILLS / "autodoc-init" / "references" / "graph"


def _flat(s: str) -> str:
    return " ".join(s.split())


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


def _update() -> str:
    return UPDATE.read_text(encoding="utf-8")


# --- 1. a new repair record reaches the manifest through Step 4.4 -------------------------

def _migration_fork() -> str:
    triage = _section(_update(), "### The gate failed on a page. Triage BEFORE you repair anything.",
                      "### Repairing a record")
    return _flat(triage[triage.index("- **The page has no record this run"):])


def _repair_section() -> str:
    return _flat(_section(_update(), "### Repairing a record", "### Backlog bookkeeping"))


def _step44() -> str:
    return _section(_update(), "### Step 4.4 — Write the records manifest", "## Step 4.5 — ")


def test_migration_fork_says_the_repair_is_a_new_record_file() -> None:
    fork = _migration_fork()
    assert "new record file" in fork, "the fork must say the page has no record, so the repair is a NEW file"
    assert "Step 4.4" in fork, "the fork must route the new file through Step 4.4's manifest block"
    assert "a `contract_with` or alias repair goes in the record's `frontmatter`, not its body" in fork, (
        "graph-check reads contract_with and aliases from frontmatter; the fork must route the repair there"
    )


def test_repair_section_routes_a_new_file_through_step_44() -> None:
    sec = _repair_section()
    assert "new record file" in sec
    assert "$RUN_DIR/records/" in sec, "the section must say where the new file goes"
    assert "Step 4.4" in sec and "recordsFiles" in sec, (
        "the section must say: re-run Step 4.4's block with recordsFiles plus the new path appended"
    )
    assert "graph records" in sec and "NOT in the run manifest" in sec, (
        "the section must name the failure an unlisted file causes, in the loader's own words"
    )


def test_not_by_hand_rule_names_its_one_exception() -> None:
    text = _flat(_update())
    i = text.index("**by Step 4.4's block, not by hand**")
    para = text[i:i + 900]
    assert "Repairing a record" in para, (
        "the 'not by hand' rule must point at the one sanctioned addition to the manifest list"
    )
    assert "only exception" in para


def test_step44_paste_comment_names_the_repair_exception() -> None:
    sec = _step44()
    paste = sec[sec.index("# PASTE the workflow's returned"):sec.index("cat > /tmp/autodoc-records-files.json")]
    assert "Repairing a record" in paste, (
        "the verbatim-paste comment must name repair records as the one addition, or it contradicts "
        "the repair procedure"
    )
    comment = _flat(" ".join(ln.lstrip("#") for ln in paste.splitlines()))
    assert "appended to the pasted array by its literal full path" in comment, (
        "the FILES heredoc is quoted, so the comment must say the appended path is literal"
    )


def _step44_python() -> str:
    sec = _step44()
    start = sec.index("python - <<'PY' || exit 1\n") + len("python - <<'PY' || exit 1\n")
    return sec[start:sec.index("\nPY\n", start)]


def _write(path: Path, obj) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


def _run_step44(tmp_path: Path, run_dir: Path, files: list[str]) -> subprocess.CompletedProcess:
    rf = tmp_path / "records-files.json"
    _write(rf, files)
    env = dict(os.environ, RUN_DIR=str(run_dir), RECORDS_FILES=str(rf),
               RUN_MANIFEST=str(tmp_path / "run.json"), UPDATE_COV=str(tmp_path / "cov.json"))
    return subprocess.run([sys.executable, "-c", _step44_python()], env=env,
                          capture_output=True, text=True)


def _records_module():
    """The real loader `graph records --manifest` calls, from THIS tree."""
    sys.path.insert(0, str(GRAPH_PKG))
    try:
        from autodoc_graph import records
    finally:
        sys.path.remove(str(GRAPH_PKG))
    assert Path(records.__file__).resolve().is_relative_to(GRAPH_PKG), records.__file__
    return records


def _load_records(records_dir: Path, manifest_path: Path):
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    return _records_module().load_records_input(records_dir, manifest=m["files"])


@pytest.fixture
def run_with_repair(tmp_path: Path):
    run_dir = tmp_path / "run"
    rec = run_dir / "records"
    rec.mkdir(parents=True)
    agent = rec / "refresh-unit-01.json"
    _write(agent, {"pages": [{"file": "components/A.md", "body": "a", "source_paths": ["a.py"]}],
                   "covered": [], "excluded": []})
    repair = rec / "repair-components-B.json"
    _write(repair, {"pages": [{"file": "components/B.md", "body": "b [[A]]", "source_paths": ["b.py"]}]})
    return tmp_path, run_dir, rec, str(agent), str(repair)


def test_unlisted_repair_record_is_refused_by_graph_records(run_with_repair) -> None:
    tmp_path, run_dir, rec, agent, _repair = run_with_repair
    res = _run_step44(tmp_path, run_dir, [agent])
    assert res.returncode == 0, res.stderr
    with pytest.raises(_records_module().RecordsError, match="NOT in the run manifest"):
        _load_records(rec, tmp_path / "run.json")


def test_repair_record_appended_to_step44_paste_is_accepted(run_with_repair) -> None:
    tmp_path, run_dir, rec, agent, repair = run_with_repair
    res = _run_step44(tmp_path, run_dir, [agent, repair])
    assert res.returncode == 0, res.stderr
    merged = _load_records(rec, tmp_path / "run.json")
    assert sorted(p["file"] for p in merged["pages"]) == ["components/A.md", "components/B.md"]


# --- 2. staleness lead-in matches diagram-staleness.js -----------------------------------

def _staleness() -> str:
    text = TAXONOMY.read_text(encoding="utf-8")
    # The passage is the file's last section, so give the end marker somewhere to land.
    return _flat(_section(text + "\n---", "**Staleness (currency):**", "\n---"))


def test_staleness_lead_in_describes_the_git_history_walk() -> None:
    s = _staleness()
    assert "against the current sha" not in s, "the script walks git history; it no longer compares two shas"
    assert "git history" in s and "last_sha" in s and "diagram_source_sha" in s
    assert "touched" in s, "STALE means a commit after diagram_source_sha touched a source path"
    assert "fallback" in s.lower(), "the no-clone sha-comparison fallback must be named, since the report labels it"


# --- 3. the blocker-gate sentence --------------------------------------------------------

def test_blocker_gate_does_not_claim_a_fix_pass() -> None:
    gate = _flat(_blocker_gate())
    assert "after `--fix`" not in gate, "neither init Step 5a nor update Step 4.5 passes --fix"
    assert "Step 5a" in gate and "Step 4.5" in gate, "the sentence must hold for both gates and name them"


# --- 4. a repair record carries each link where the live page holds it -------------------

GRAPH_CHECK = SKILLS / "autodoc-init" / "references" / "inventory" / "graph-check.js"
RENDER_PAGES = GRAPH_PKG / "autodoc_graph" / "render_pages.py"


def _render_stamped_keys() -> set[str]:
    """Every frontmatter key the renderer assigns in _page_text, read from the code."""
    src = RENDER_PAGES.read_text(encoding="utf-8")
    body = src[src.index("    def _page_text("):src.index("    def _meta_text(")]
    keys = set(re.findall(r'fm\["(\w+)"\] = ', body))
    assert {"created", "updated", "project", "source_repo", "diagram_source_sha"} <= keys, keys
    return keys


def _edge_derived_fields() -> tuple[str, ...]:
    src = RENDER_PAGES.read_text(encoding="utf-8")
    m = re.search(r"^_EDGE_DERIVED_FIELDS = \(([^)]*)\)", src, re.M)
    assert m, "render_pages.py no longer defines _EDGE_DERIVED_FIELDS"
    return tuple(re.findall(r'"(\w+)"', m.group(1)))


def test_repair_section_says_where_each_carried_link_goes() -> None:
    sec = _repair_section()
    assert "`contract_with`" in sec and "`aliases`" in sec, (
        "frontmatter relational fields (graph-check's reciprocal contract_with and alias fixes) "
        "are carried in the record's frontmatter, and the section must name them"
    )
    placed = dict(re.findall(r"- \*\*A (frontmatter field|body wikilink)\*\* goes in `(\w+)`\.", sec))
    assert placed == {"frontmatter field": "frontmatter", "body wikilink": "body"}, (
        f"each link goes in the part of the record where the live page holds it: {placed}"
    )
    assert re.search(r"- \*\*`related` or `depends_on`\*\* cannot go in `frontmatter`: [^-]*"
                     r"Add an `edges` entry instead", sec), "related/depends_on are routed to an edges entry"
    for field in _edge_derived_fields():
        assert f"`{field}`" in sec, f"{field} is edge-derived; the section must route it to an edges entry"
    assert "`edges`" in sec and "dst_type" in sec and "dst_title" in sec, (
        "the section must give the edges entry's shape, as records.py validates it"
    )


def test_repair_section_says_where_frontmatter_comes_from_and_what_to_strip() -> None:
    sec = _repair_section()
    assert "- `frontmatter`, as an object: the frontmatter of `$RENDER_TARGET/<page>`" in sec, (
        "the record's frontmatter needs a named source"
    )
    m = re.search(r"minus every key the renderer stamps on its own: ([^.]*)\. Also drop ([^.]*)\.", sec)
    assert m, "the strip list and the 'Also drop' sentence must sit together in the frontmatter bullet"
    strip, also = m.groups()
    edge = set(_edge_derived_fields())
    for key in sorted(_render_stamped_keys() - edge):
        assert f"`{key}`" in strip, f"the renderer stamps {key!r}; the strip list must name it"
    for field in sorted(edge):
        assert f"`{field}`" in also, f"{field} is rendered from edge rows; the 'Also drop' sentence must name it"
    assert "edge rows" in also
    assert "`## Diagram`" in sec, "a rendered flow/data-model page carries a ## Diagram section to strip"


def test_repair_section_says_where_summary_comes_from() -> None:
    src = RENDER_PAGES.read_text(encoding="utf-8")
    assert "summary" not in _render_stamped_keys(), "the page frontmatter now carries summary; update the prose"
    assert """lines.append(f"- [[{e['name']}]]" + (f" — {summary}" if summary else ""))""" in src, (
        "the folder _index.md nav line is where the renderer writes summary"
    )
    sec = _repair_section()
    assert "`summary`, which the rendered page does not carry" in sec
    assert "`$RENDER_TARGET/<folder>/_index.md`, as `- [[<Title>]] — <summary>`" in sec
    assert "as the rendered page states them" not in sec


def test_repair_section_says_the_appended_path_is_literal() -> None:
    sec = _repair_section()
    assert "literal path" in sec, (
        "the FILES heredoc is quoted, so $RUN_DIR is not expanded; the appended path must be literal"
    )
    raw = _section(_update(), "### Repairing a record", "### Backlog bookkeeping")
    long = [ln for ln in raw.splitlines() if len(ln) > 100]
    assert not long, f"rewrap the repair section to 100 columns: {long}"


# --- 5. the blocker gate matches graph-check.js -------------------------------------------

def _classify_blocker_kinds() -> set[str]:
    src = GRAPH_CHECK.read_text(encoding="utf-8")
    body = src[src.index("export function classify("):src.index("return { blockers, warnings };")]
    kinds = set(re.findall(r"blockers\.push\(\{\s*kind: '(\w+)'", body))
    assert len(kinds) >= 5, kinds
    return kinds


def _blocker_gate() -> str:
    text = TAXONOMY.read_text(encoding="utf-8")
    return _section(text, "**Blocker gate:**", "**The update path is stricter.**")


def test_blocker_gate_names_every_blocker_kind_classify_emits() -> None:
    gate = _flat(_blocker_gate())
    for kind in sorted(_classify_blocker_kinds()):
        assert f"`{kind}`" in gate, f"classify() emits blocker kind {kind!r}; the gate must name it"
    assert "exactly two" not in gate and "residual" not in gate, (
        "no --fix runs on either gate, so nothing is 'residual', and there are more than two kinds"
    )


def test_blocker_gate_states_the_fixable_rule_for_each_gate() -> None:
    src = GRAPH_CHECK.read_text(encoding="utf-8")
    assert "process.exit(blockers.length > 0 || fixable.length > 0 ? 1 : 0);" in src
    gate = _flat(_blocker_gate())
    init = gate[gate.index("Step 5a"):gate.index("Step 4.5")]
    assert "no flag" in init and "warnings" in init, "init Step 5a: no flag, so fixable findings are warnings"
    assert "A blocker exits 1;" in init and "exits 0" not in init, "init Step 5a: a blocker exits 1"
    assert "`GRAPH_RC`" in init, "init Step 5a's block stores the exit status; it does not exit itself"
    upd = gate[gate.index("Step 4.5"):]
    assert "`--fail-on-fixable`" in upd and "any fixable finding" in upd and "fatal" in upd, (
        "update Step 4.5: --fail-on-fixable exits 1 on any blocker OR any fixable finding"
    )
    assert "never fatal" not in gate, "a fixable broken link IS fatal on Step 4.5"
    assert not [ln for ln in _blocker_gate().splitlines() if len(ln) > 100], "rewrap the gate to 100 columns"


def test_init_step_5a_block_stores_the_exit_status_without_exiting() -> None:
    init = (SKILLS / "autodoc-init" / "SKILL.md").read_text(encoding="utf-8")
    fence = _section(init, "### 5a — Graph-reviewer", "### 5b").split("```")[1]
    assert "GRAPH_RC=$?" in fence, "the taxonomy says Step 5a stores the exit status in GRAPH_RC"
    assert "--fail-on-fixable" not in fence and "--fix" not in fence, "the taxonomy says Step 5a passes no flag"



# --- 6. warning kinds, fix classes and the candidates claim match graph-check.js ----------

_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10}
_KIND = r"""kind: ['"`](\w+)['"`]"""
_REPORT_LOOP = re.compile(r"^  for \(const \w+ of report\.(\w+) \|\| \[\]\) \{\n(.*?)^  \}", re.M | re.S)
_STEP61 = "Step 6.1 graph-reviewer gate (runs in Step 4.5)"


def _graph_check_src() -> str:
    return GRAPH_CHECK.read_text(encoding="utf-8")


def _classify_body() -> str:
    src = _graph_check_src()
    return src[src.index("export function classify("):src.index("return { blockers, warnings };")]


def _autofix_body() -> str:
    src = _graph_check_src()
    return src[src.index("export function autoFix("):src.index("return { fixed, residual };")]


def _classify_warning_kinds() -> set[str]:
    body = _classify_body()
    kinds = re.findall(r"warnings\.push\(\{\s*" + _KIND, body)
    assert len(kinds) == body.count("warnings.push("), (
        "a warnings.push in classify() has a kind this parser cannot read (variable or templated)"
    )
    assert {"brokenLink", "orphan", "aliasCollision"} <= set(kinds), kinds
    return set(kinds)


def _autofix_fix_kinds() -> list[str]:
    body = _autofix_body()
    kinds = re.findall(r"fixed\.push\(\{[^}]*?" + _KIND, body)
    assert len(kinds) == body.count("fixed.push("), "a fixed.push in autoFix() has an unreadable kind"
    assert len(set(kinds)) == len(kinds), f"two fix classes share a kind: {kinds}"
    return kinds


def _fixable_warning_kinds() -> set[str]:
    """Warning kinds classify() emits from a report field autoFix repairs."""
    fixed_fields = set(re.findall(r"report\.(\w+) \|\| \[\]", _autofix_body()))
    assert {"brokenLinks", "missingFrontmatter"} <= fixed_fields, fixed_fields
    kinds = set()
    for field, block in _REPORT_LOOP.findall(_classify_body()):
        if field in fixed_fields:
            kinds |= set(re.findall(r"warnings\.push\(\{\s*" + _KIND, block))
    # brokenLinkCandidate sits in the brokenLinks loop but needs a recorded candidate, and
    # checkGraph records none (pinned below), so the gate never emits it.
    kinds.discard("brokenLinkCandidate")
    assert "brokenLink" in kinds, kinds
    return kinds


def _autofix_header() -> str:
    src = _graph_check_src()
    return src[src.index("// autoFix(report, "):src.index("export function autoFix(")]


def _step61() -> str:
    return _flat(_section(_update(), "### Step 6.1 — Graph-reviewer gate", "### Step 6.2"))


def test_step61_names_every_warning_kind_classify_emits() -> None:
    sec = _step61()
    for kind in sorted(_classify_warning_kinds()):
        assert f"`{kind}`" in sec, f"classify() emits warning kind {kind!r}; the {_STEP61} must name it"


def test_step61_names_exactly_the_fixable_warning_kinds() -> None:
    sec = _step61()
    m = re.search(r"(\w+) more warning kinds are the fixable classes above: ([^.]*)\.", sec)
    assert m, f"the {_STEP61} must say which warning kinds are fixable"
    named = set(re.findall(r"`(\w+)`", m.group(2)))
    expected = _fixable_warning_kinds()
    assert named == expected, (
        f"autoFix repairs the report fields behind {sorted(expected)}; the {_STEP61} names {sorted(named)}"
    )
    assert _NUMBER_WORDS.get(m.group(1).lower()) == len(expected), m.group(1)
    lead = sec[sec.index("**Warnings are reported, not fatal:**"):m.start()]
    for kind in expected:
        assert f"`{kind}`" not in lead, f"{kind} is fixable; it must not be listed as never fatal"


def test_repair_count_matches_the_fix_classes_autofix_applies() -> None:
    n = len(_autofix_fix_kinds())
    text = _flat(TAXONOMY.read_text(encoding="utf-8"))
    m = re.search(r"\(`--fix`\): (\w+) repairs", text)
    assert m and _NUMBER_WORDS.get(m.group(1)) == n, (
        f"autoFix applies {n} fix classes; the taxonomy's safe auto-fix scope says {m and m.group(1)!r}"
    )
    header = _autofix_header()
    assert "two safe fix classes" not in header, "autoFix applies more than two fix classes"
    lettered = re.findall(r"^// \(([a-z])\) ", header, re.M)
    assert len(lettered) == n, f"the autoFix header lists {lettered}; autoFix applies {n} fix classes"
    word = next(w for w, v in _NUMBER_WORDS.items() if v == n)
    assert f"ONLY the {word} safe fix classes" in header


def test_taxonomy_does_not_claim_broken_links_record_candidates() -> None:
    src = _graph_check_src()
    assert "brokenLinks.push({ page: pg.file, target, candidates: [], class: cls });" in src, (
        "checkGraph now records candidates; update the taxonomy's broken-wikilinks line"
    )
    text = TAXONOMY.read_text(encoding="utf-8")
    bullet = _flat(_section(text, "- **broken wikilinks**", "- **orphans**"))
    assert "candidates for the fixer" not in bullet and "records any" not in bullet, (
        "checkGraph always records candidates: []; the taxonomy must not say it records any"
    )
    assert "No candidates are recorded at check time" in bullet
    assert "`--fix` finds a single near-match" in bullet, "autoFix's fuzzy matching finds the candidate"
