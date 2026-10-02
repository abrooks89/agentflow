"""`graph build` refuses a corpus it would leave unrenderable, at build time (gh#152).

`render` refuses a corpus whose `repo_ref` or `project` is unset, and only `build` can set
them. Before this guard, a build that omitted the flags succeeded, and the chain died five
steps later at `render` with a remedy that pointed back at a command the operator had
already finished. These tests pin the three behaviours that fix is made of:

* a fresh corpus is REFUSED, with both flags named, and nothing is ingested;
* an already-attributed corpus accepts a FLAGLESS rebuild (`update_corpus` skips None
  columns, so the stored values survive) -- the guard permits that deliberately rather
  than tripping over it;
* what `build` accepts, `render` can render -- the two guards agree in behaviour, not
  merely in the source that `test_documented_graph_recipes.py` parses.
"""

from __future__ import annotations

import json

from autodoc_graph.cli import main
from autodoc_graph.render_pages import RenderError, render_pages
from autodoc_graph.sqlite_backend import SqliteGraphAdapter

CORPUS = "corp"
SYM = f"sym:{CORPUS}:a.py:function:f"


def _jsonl(tmp_path, sha="sha1"):
    lines = [
        {"type": "entity", "id": SYM, "tier": "symbol", "kind": "function",
         "name": "f", "qualified_name": None, "source_paths": ["a.py"],
         "span": {"file": "a.py", "start_line": 1, "end_line": 2},
         "summary": None, "origin": "ast"},
        {"type": "meta", "corpus": CORPUS, "repo": "", "sha": sha, "counts": {}},
    ]
    p = tmp_path / f"graph-{sha}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def _corpus_row(db):
    a = SqliteGraphAdapter(db)
    a.open(CORPUS)
    try:
        return a.get_corpus(), a.stats()
    finally:
        a.close()


def test_flagless_build_on_a_fresh_corpus_is_refused_naming_both_flags(tmp_path, capsys):
    db = tmp_path / "g.db"
    rc = main(["build", "--jsonl", str(_jsonl(tmp_path)), "--db", str(db),
               "--corpus", CORPUS])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--repo-ref" in err and "--project" in err, err
    # The remedy has to be the command just typed, not the one six steps later.
    assert "THIS command" in err, err
    row, stats = _corpus_row(db)
    assert stats["entities"] == 0, "the refusal must land before anything is ingested"
    assert not row["repo_ref"] and not row["project"]


def test_a_build_missing_only_project_names_only_project(tmp_path, capsys):
    db = tmp_path / "g.db"
    rc = main(["build", "--jsonl", str(_jsonl(tmp_path)), "--db", str(db),
               "--corpus", CORPUS, "--repo-ref", "owner/repo"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--project" in err
    assert "--repo-ref" not in err, (
        "naming a flag the operator already supplied sends them looking in the wrong "
        f"place: {err}"
    )


def test_an_attributed_build_succeeds_and_stamps_the_corpus(tmp_path):
    db = tmp_path / "g.db"
    assert main(["build", "--jsonl", str(_jsonl(tmp_path)), "--db", str(db),
                 "--corpus", CORPUS, "--repo-ref", "owner/repo",
                 "--project", "demo"]) == 0
    row, stats = _corpus_row(db)
    assert (row["repo_ref"], row["project"]) == ("owner/repo", "demo")
    assert stats["entities"] == 1


def test_a_flagless_rebuild_of_an_attributed_corpus_is_permitted(tmp_path):
    """The mid-migration path, stated deliberately instead of relied on by accident."""
    db = tmp_path / "g.db"
    assert main(["build", "--jsonl", str(_jsonl(tmp_path)), "--db", str(db),
                 "--corpus", CORPUS, "--repo-ref", "owner/repo",
                 "--project", "demo"]) == 0
    assert main(["build", "--jsonl", str(_jsonl(tmp_path, sha="sha2")), "--db", str(db),
                 "--corpus", CORPUS]) == 0
    row, _ = _corpus_row(db)
    assert (row["repo_ref"], row["project"]) == ("owner/repo", "demo"), (
        "update_corpus skips None columns, so a flagless rebuild must preserve the "
        "stored attribution rather than clearing it"
    )


def test_what_build_accepts_render_can_render(tmp_path):
    """The behavioural half of the gh#152 fix: the two guards must agree in FACT.

    A source-level check that both name the same fields is worth having, but it cannot
    catch a `build` that accepts a corpus `render` still refuses. This can.
    """
    db = tmp_path / "g.db"
    assert main(["build", "--jsonl", str(_jsonl(tmp_path)), "--db", str(db),
                 "--corpus", CORPUS, "--repo-ref", "owner/repo",
                 "--project", "demo"]) == 0
    adapter = SqliteGraphAdapter(db)
    adapter.open(CORPUS)
    try:
        # No concept-tier entities in this fixture, so there is nothing to write; the
        # point is that the renderer's corpus guards do not refuse what build accepted.
        render_pages(adapter, tmp_path / "out")
    except RenderError as exc:
        assert "corpus." not in str(exc), (
            "`build` accepted a corpus the renderer still refuses on attribution -- the "
            f"exact gap gh#152 is about: {exc}"
        )
    finally:
        adapter.close()
