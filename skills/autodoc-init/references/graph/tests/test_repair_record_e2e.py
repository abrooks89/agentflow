"""End to end: a repair record carried as /autodoc-update's "Repairing a record" says.

The migration case: Alpha lists contract_with [[Beta]], Beta's reciprocal lives only in the live
tree, and Beta has no record this run. The repair is a NEW record file, routed through Step 4.4's
own python (read from skills/autodoc-update/SKILL.md) and then Step 4.5: records, render, check,
and graph-check.js --fail-on-fixable. A reciprocal contract_with carried in frontmatter passes;
the same link carried in the body fails.

These live in the graph suite, not skills/_shared, because the graph CLI imports networkx and the
_shared job installs only skills/_shared. The prose pins for the same section stay in
skills/_shared/tests/test_autodoc_update_repair_manifest_prose.py.

graph-check.js and coverage.js import only node builtins, so the inventory node_modules are not
needed; the tests skip only when node itself is missing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autodoc_graph.cli import main
from autodoc_graph.render_pages import _EDGE_DERIVED_FIELDS


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for d in here.parents:
        if (d / "skills" / "autodoc-update" / "SKILL.md").is_file():
            return d
    raise RuntimeError(f"no ancestor of {here} holds skills/autodoc-update/SKILL.md")


SKILLS = _repo_root() / "skills"
UPDATE = SKILLS / "autodoc-update" / "SKILL.md"
GRAPH_CHECK = SKILLS / "autodoc-init" / "references" / "inventory" / "graph-check.js"
RENDER_PAGES = SKILLS / "autodoc-init" / "references" / "graph" / "autodoc_graph" / "render_pages.py"


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


def _step44_python() -> str:
    sec = _section(UPDATE.read_text(encoding="utf-8"),
                   "### Step 4.4 — Write the records manifest", "## Step 4.5 — ")
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
                          capture_output=True, text=True, check=False)


def _render_stamped_keys() -> set[str]:
    """Every frontmatter key the renderer assigns in _page_text, read from the code."""
    src = RENDER_PAGES.read_text(encoding="utf-8")
    body = src[src.index("    def _page_text("):src.index("    def _meta_text(")]
    keys = set(re.findall(r'fm\["(\w+)"\] = ', body))
    assert {"created", "updated", "project", "source_repo", "diagram_source_sha"} <= keys, keys
    return keys


def _scalar(raw: str):
    # The renderer's scalars are JSON-compatible: double-quoted strings escaping only \ and ",
    # true/false, and repr() of ints and floats. This package carries no YAML dependency.
    return json.loads(raw)


def _split_page(text: str) -> tuple[dict, str]:
    """Parse the renderer's flat frontmatter (scalars and `  - item` lists) and the body."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, text[:200]
    fm: dict = {}
    key = None
    for line in m.group(1).splitlines():
        if line.startswith("  - "):
            assert key is not None and isinstance(fm[key], list), line
            fm[key].append(_scalar(line[4:]))
            continue
        k, sep, rest = line.partition(":")
        assert sep, line
        key = k
        fm[k] = _scalar(rest.strip()) if rest.strip() else []
    return fm, m.group(2)


def _sym(eid: str, file: str) -> dict:
    name = eid.rsplit(":", 1)[-1]
    return {"type": "entity", "id": eid, "tier": "symbol", "kind": "function", "name": name,
            "qualified_name": f"{file}:{name}", "source_paths": [file],
            "span": {"file": file, "start_line": 1, "end_line": 2}, "summary": None, "origin": "ast"}


def _page(title: str, src: str, sym: str, fm_extra: dict, body: str) -> dict:
    return {"file": f"components/{title}.md", "type": "component", "title": title,
            "source_paths": [src], "summary": f"{title} summary.", "body": body,
            "frontmatter": {"component_kind": "module", "path": src, "language": "python", **fm_extra},
            "documents": [{"file": src, "kind": "function", "name": sym}], "edges": []}


def _migration_case(tmp_path: Path, carry: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Carry Beta's reciprocal through a new repair record, re-run Step 4.4's own python with its
    path appended, then Step 4.5: records, render, check, graph-check --fail-on-fixable."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH; graph-check.js cannot run")
    db = tmp_path / "graph.db"
    jsonl = tmp_path / "g.jsonl"
    lines = [_sym("sym:corp:a.py:function:f", "a.py"), _sym("sym:corp:b.py:function:h", "b.py"),
             {"type": "meta", "corpus": "corp", "repo": "o/r", "sha": "sha1", "counts": {}}]
    jsonl.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert main(["build", "--db", str(db), "--corpus", "corp", "--jsonl", str(jsonl),
                 "--repo-ref", "owner/repo", "--project", "demo"]) == 0
    prior = tmp_path / "prior.json"
    _write(prior, {"pages": [_page("Alpha", "a.py", "f", {"contract_with": ["[[Beta]]"]}, "Alpha.\n"),
                             _page("Beta", "b.py", "h", {}, "Beta body.\n")]})
    assert main(["records", "--db", str(db), "--corpus", "corp", "--records", str(prior),
                 "--sha", "sha1", "--date", "2026-09-01"]) == 0

    run_dir = tmp_path / "run"
    rec = run_dir / "records"
    rec.mkdir(parents=True)
    unit = rec / "refresh-unit-01.json"
    _write(unit, {"pages": [_page("Alpha", "a.py", "f", {"contract_with": ["[[Beta]]"]}, "Alpha, again.\n")],
                  "covered": [], "excluded": []})
    render = tmp_path / "render"
    assert main(["render", "--db", str(db), "--corpus", "corp", "--out", str(render)]) == 0

    # The repair, as "Repairing a record" says: frontmatter and body from the rendered page,
    # render-stamped and edge-derived keys stripped, the link carried where the live page holds it.
    fm, body = _split_page((render / "components" / "Beta.md").read_text(encoding="utf-8"))
    for key in _render_stamped_keys() | set(_EDGE_DERIVED_FIELDS):
        fm.pop(key, None)
    if carry == "frontmatter":
        fm["contract_with"] = ["[[Alpha]]"]
    else:
        body = body.rstrip("\n") + "\n\nContract with [[Alpha]].\n"
    repair = rec / "repair-components-Beta.json"
    _write(repair, {"pages": [{"file": "components/Beta.md", "type": "component", "title": "Beta",
                               "source_paths": ["b.py"], "summary": "Beta summary.",
                               "frontmatter": fm, "body": body}]})
    res = _run_step44(tmp_path, run_dir, [str(unit), str(repair)])
    assert res.returncode == 0, res.stderr
    assign = tmp_path / "assign.json"
    _write(assign, [])

    shutil.rmtree(render)
    assert main(["records", "--db", str(db), "--corpus", "corp", "--records", str(rec),
                 "--manifest", str(tmp_path / "run.json"), "--assignment", str(assign),
                 "--sha", "sha2", "--date", "2026-10-02"]) == 0
    assert main(["render", "--db", str(db), "--corpus", "corp", "--out", str(render)]) == 0
    assert main(["check", "--db", str(db), "--corpus", "corp", "--out", str(tmp_path / "check.json")]) == 0
    gc = subprocess.run([node, str(GRAPH_CHECK), str(render), "-", "-", str(tmp_path / "gc.json"),
                         "--fail-on-fixable"], capture_output=True, text=True, timeout=120, check=False)
    return gc, render


def test_e2e_frontmatter_carry_of_a_reciprocal_contract_with_passes_step_45(tmp_path: Path) -> None:
    gc, render = _migration_case(tmp_path, "frontmatter")
    assert gc.returncode == 0, gc.stderr
    fm, _ = _split_page((render / "components" / "Beta.md").read_text(encoding="utf-8"))
    assert fm["contract_with"] == ["[[Alpha]]"]


def test_e2e_the_same_link_carried_in_the_body_still_fails_step_45(tmp_path: Path) -> None:
    gc, _ = _migration_case(tmp_path, "body")
    assert gc.returncode == 1, gc.stderr
    assert 'fixable (not applied): components/Beta.md: contractWith: added "[[Alpha]]"' in gc.stderr
