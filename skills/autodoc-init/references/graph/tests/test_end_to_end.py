"""End-to-end over the package's own multi-language fixture repo (success
criteria 1, 2, 5, 6): Node extraction -> JSONL -> build -> stats -> re-run
no-op -> file removal -> prune dry-run.

Requires Node plus the inventory package's node_modules (`npm ci` in
skills/autodoc-init/references/inventory — the provision step installs it).
A missing runtime fails loudly; nothing is skipped silently.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autodoc_graph.cli import main
from autodoc_graph.sqlite_backend import SqliteGraphAdapter

HERE = Path(__file__).resolve().parent
FIXTURE_REPO = HERE / "fixtures" / "repo"
INVENTORY_PKG = HERE.parent.parent / "inventory"
EMIT = INVENTORY_PKG / "emit-graph.js"

DETERMINISTIC_RELS = ("imports", "calls", "defines", "routes_to")


def run_emit(repo: Path, out: Path, corpus: str, sha: str) -> None:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node not found on PATH — required for extraction e2e")
    if not (INVENTORY_PKG / "node_modules").is_dir():
        raise RuntimeError(
            f"inventory package deps missing: run `npm ci` in {INVENTORY_PKG}"
        )
    res = subprocess.run(
        [node, str(EMIT), str(repo), str(out), corpus, sha],
        capture_output=True, text=True, check=False,  # loud RuntimeError below
    )
    if res.returncode != 0:
        raise RuntimeError(f"emit-graph.js failed:\n{res.stdout}\n{res.stderr}")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    jsonl = tmp / "graph.jsonl"
    db = tmp / "graph.db"
    run_emit(FIXTURE_REPO, jsonl, "fix", "sha1")
    # Only this first build carries attribution: the rebuilds below deliberately omit
    # it, which proves the gh#152 guard permits a rebuild of an already-attributed
    # corpus rather than demanding the flags on every run.
    assert main(["build", "--jsonl", str(jsonl), "--db", str(db), "--corpus", "fix",
                 "--repo-ref", "owner/fix", "--project", "demo"]) == 0
    return {"tmp": tmp, "jsonl": jsonl, "db": db}


def _stats(db: Path) -> dict:
    a = SqliteGraphAdapter(db)
    a.open("fix")
    try:
        return a.stats()
    finally:
        a.close()


# criterion 1: symbol counts + non-empty deterministic edge sets
def test_symbol_counts_match_extraction_and_all_rels_nonempty(built):
    s = _stats(built["db"])
    meta = json.loads(
        built["jsonl"].read_text(encoding="utf-8").strip().splitlines()[-1])
    assert s["entities"] == meta["counts"]["entities"]
    assert s["entities"] > 8  # modules + symbols across 4 languages
    for rel in DETERMINISTIC_RELS:
        assert s["edges_by_rel"].get(rel, 0) > 0, f"no {rel} edges in fixture graph"


# criterion 2: double run is a no-op — THE regeneration test
def test_double_run_is_noop(built, capsys):
    stats_before = _stats(built["db"])
    jsonl2 = built["tmp"] / "graph2.jsonl"
    run_emit(FIXTURE_REPO, jsonl2, "fix", "sha1")
    assert jsonl2.read_bytes() == built["jsonl"].read_bytes(), \
        "extraction must be byte-deterministic"
    assert main(["build", "--jsonl", str(jsonl2), "--db", str(built["db"]),
                 "--corpus", "fix"]) == 0
    out = capsys.readouterr().out
    assert '"inserted": 0' in out
    assert _stats(built["db"]) == stats_before


# criterion 5: prune dry-run reports after file removal, deletes nothing
def test_prune_dry_run_after_file_removal(built, capsys):
    mutated = built["tmp"] / "mutated-repo"
    shutil.copytree(FIXTURE_REPO, mutated)
    (mutated / "app" / "util.py").unlink()
    jsonl3 = built["tmp"] / "graph3.jsonl"
    run_emit(mutated, jsonl3, "fix", "sha2")
    assert main(["build", "--jsonl", str(jsonl3), "--db", str(built["db"]),
                 "--corpus", "fix"]) == 0
    capsys.readouterr()
    entities_before = _stats(built["db"])["entities"]
    assert main(["prune", "--db", str(built["db"]), "--corpus", "fix"]) == 0
    out = capsys.readouterr().out
    assert "sym:fix:app/util.py:module:util" in out
    assert "sym:fix:app/util.py:function:util_fn" in out
    assert "nothing deleted" in out
    assert _stats(built["db"])["entities"] == entities_before
    # explicit delete removes exactly the candidates
    assert main(["prune", "--db", str(built["db"]), "--corpus", "fix",
                 "--delete"]) == 0
    assert _stats(built["db"])["entities"] == entities_before - 2


def test_all_e2e_entities_are_symbol_tier(built):
    # this epic populates the symbol tier ONLY; the concept tier belongs to 704
    assert set(_stats(built["db"])["entities_by_tier"]) == {"symbol"}
