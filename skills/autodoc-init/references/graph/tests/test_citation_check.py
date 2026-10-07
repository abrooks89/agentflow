"""Write-time `documents` citation check (gh#465 item 5).

A citation that names a symbol the index never holds used to surface only when
`graph records` ingested the whole fan-out, one refusal per retry. The check is
now callable per records file the moment an agent writes it (`graph
check-records`), and `write_records` runs the same check over the whole batch
before any write, naming EVERY bad citation and why it cannot resolve: the
kind is wrong, the symbol is absent, or the kind is never indexed at all.
"""

from __future__ import annotations

import json

import pytest
from conftest import entity_row

from autodoc_graph.cli import main
from autodoc_graph.records import (
    RecordsError,
    SymbolIndex,
    check_records,
    citation_problem,
    write_records,
)
from autodoc_graph.sqlite_backend import SqliteGraphAdapter


def _seed(adapter):
    adapter.upsert_entities([
        entity_row("sym:corp:app/x.py:function:handler", kind="function", name="handler",
                   span={"file": "app/x.py", "start_line": 1, "end_line": 5}),
        entity_row("sym:corp:app/x.py:class:Widget", kind="class", name="Widget",
                   span={"file": "app/x.py", "start_line": 10, "end_line": 30}),
        entity_row("sym:corp:app/x.py:module:x", kind="module", name="x",
                   span={"file": "app/x.py", "start_line": 1, "end_line": 40}),
    ])


def _page(title="Widget Service", documents=None, **over):
    rec = {
        "file": f"services/{title}.md", "type": "service", "title": title,
        "source_paths": ["app/x.py"], "summary": "Serves widgets.",
        "body": "Serves widgets.\n\n## Source\n\n`app/x.py`\n",
        "frontmatter": {"service_kind": "api", "path": "app/", "language": "python",
                        "entrypoint": "app/x.py", "status": "active"},
        "documents": documents if documents is not None else [
            {"file": "app/x.py", "kind": "function", "name": "handler"}],
        "edges": [],
    }
    rec.update(over)
    return rec


def _cite(kind, name, file="app/x.py"):
    return {"file": file, "kind": kind, "name": name}


# ------------------------------------------------------------ classification
def test_a_citation_that_resolves_has_no_problem(adapter):
    _seed(adapter)
    index = SymbolIndex.from_adapter(adapter)
    assert citation_problem(_cite("function", "handler"), index) is None
    # the store mints a module entity per file, which the checklist lists too
    assert citation_problem(_cite("module", "x"), index) is None
    # a Windows-style path resolves the way symbol_id normalises it
    assert citation_problem(_cite("class", "Widget", "app\\x.py"), index) is None


def test_wrong_kind_names_the_kind_the_symbol_is_indexed_as(adapter):
    _seed(adapter)
    problem = citation_problem(_cite("function", "Widget"), SymbolIndex.from_adapter(adapter))
    assert problem is not None
    assert "wrong kind" in problem
    assert "indexed as class" in problem and "not function" in problem


def test_a_kind_never_indexed_says_no_such_citation_can_resolve(adapter):
    _seed(adapter)
    problem = citation_problem(_cite("constant", "MAX_RETRIES"),
                               SymbolIndex.from_adapter(adapter))
    assert problem is not None
    assert "kind not indexed" in problem
    assert "no 'constant' symbols at all" in problem
    assert "can never resolve" in problem
    # names what IS indexed, so the agent can pick a real kind
    assert "class, function, module" in problem


def test_an_absent_symbol_is_named_as_absent(adapter):
    _seed(adapter)
    problem = citation_problem(_cite("function", "ghost"), SymbolIndex.from_adapter(adapter))
    assert problem is not None
    assert "symbol absent" in problem and "'ghost'" in problem and "app/x.py" in problem


def test_a_file_with_no_indexed_symbols_is_named(adapter):
    _seed(adapter)
    problem = citation_problem(_cite("function", "handler", "app/y.py"),
                               SymbolIndex.from_adapter(adapter))
    assert problem is not None
    assert "file not indexed" in problem and "app/y.py" in problem


def test_a_name_indexed_under_another_kind_is_wrong_kind_even_when_the_kind_is_never_indexed(adapter):
    # `export const handler = () => ...` is indexed as a function; citing it as a
    # constant (a kind the store never holds) must say which kind to copy, not
    # steer the agent to drop a citation that would resolve
    _seed(adapter)
    problem = citation_problem(_cite("constant", "handler"), SymbolIndex.from_adapter(adapter))
    assert problem is not None
    assert "wrong kind" in problem and "indexed as function" in problem
    assert "kind not indexed" not in problem


def test_a_backslash_citation_path_is_classified_against_the_forward_slash_index(adapter):
    _seed(adapter)
    problem = citation_problem(_cite("function", "Widget", "app\\x.py"),
                               SymbolIndex.from_adapter(adapter))
    assert problem is not None
    assert "wrong kind" in problem and "app/x.py" in problem


def test_a_backslash_span_path_in_the_index_is_normalised():
    index = SymbolIndex("corp", [{"id": "sym:corp:app/x.py:class:Widget", "kind": "class",
                                  "name": "Widget", "span": {"file": "app\\x.py"}}])
    assert index.files == {"app/x.py"}
    problem = citation_problem(_cite("function", "Widget"), index)
    assert problem is not None and "wrong kind" in problem


def test_an_empty_symbol_index_fails_loud_instead_of_classifying_each_citation(adapter):
    # a typo'd --corpus (or an unbuilt store) holds no symbols: every citation
    # would read as "kind not indexed", steering agents to drop good citations
    with pytest.raises(RecordsError) as exc:
        check_records(adapter, {"pages": [_page()]})
    msg = str(exc.value)
    assert "no symbol" in msg and "'corp'" in msg and "--corpus" in msg


def test_an_empty_symbol_index_is_fine_when_nothing_is_cited(adapter):
    result = check_records(adapter, {"pages": [_page(documents=[])]})
    assert result == {"pages": 1, "citations": 0, "problems": []}


# ------------------------------------------------------------ check_records
def test_check_records_lists_every_problem_not_just_the_first(adapter):
    _seed(adapter)
    records = {"pages": [
        _page("Alpha", documents=[_cite("function", "Widget")]),
        _page("Beta", documents=[_cite("constant", "LIMIT"), _cite("function", "handler")]),
    ]}
    result = check_records(adapter, records)
    assert result["pages"] == 2 and result["citations"] == 3
    problems = result["problems"]
    assert len(problems) == 2
    assert problems[0].startswith("record 'Alpha'") and "wrong kind" in problems[0]
    assert problems[1].startswith("record 'Beta'") and "kind not indexed" in problems[1]


def test_check_records_reports_a_structural_problem_and_keeps_checking(adapter):
    _seed(adapter)
    broken = _page("Alpha")
    del broken["summary"]
    records = {"pages": [broken, _page("Beta", documents=[_cite("function", "ghost")])]}
    problems = check_records(adapter, records)["problems"]
    assert len(problems) == 2
    assert "summary" in problems[0]
    assert "symbol absent" in problems[1]


def test_a_malformed_page_is_not_also_citation_checked(adapter):
    _seed(adapter)
    broken = _page("Alpha", documents=[_cite("function", "ghost")])
    del broken["summary"]
    problems = check_records(adapter, {"pages": [broken]})["problems"]
    assert len(problems) == 1 and "summary" in problems[0]


def test_check_records_validates_meta_pages_too(adapter):
    _seed(adapter)
    meta = {"file": "notes/random.md", "title": "Random", "body": "x"}
    problems = check_records(adapter, {"pages": [_page()], "meta_pages": [meta]})["problems"]
    assert len(problems) == 1
    assert "meta record 'Random'" in problems[0] and "not an allowed meta page" in problems[0]


@pytest.mark.parametrize("key", ["pages", "meta_pages"])
@pytest.mark.parametrize("value", [{"a": 1}, ["not a page"], "pages"])
def test_a_misshapen_records_list_is_a_problem_not_a_crash(adapter, key, value):
    # an AttributeError here escaped the CLI's error handling as a bare traceback
    _seed(adapter)
    result = check_records(adapter, {"pages": [_page()], key: value} if key == "meta_pages"
                           else {key: value})
    assert len(result["problems"]) >= 1
    assert all(key in p for p in result["problems"])


def test_check_records_on_a_clean_batch_reports_nothing(adapter):
    _seed(adapter)
    result = check_records(adapter, {"pages": [_page()]})
    assert result == {"pages": 1, "citations": 1, "problems": []}


def test_check_records_writes_nothing(adapter):
    _seed(adapter)
    check_records(adapter, {"pages": [_page()]})
    assert adapter.find_entities({"tier": "concept"}) == []


# ------------------------------------------------------------ ingest
def test_write_records_names_every_bad_citation_before_any_write(adapter):
    _seed(adapter)
    records = {"pages": [
        _page("Alpha", documents=[_cite("function", "Widget")]),
        _page("Beta", documents=[_cite("constant", "LIMIT")]),
    ]}
    with pytest.raises(RecordsError) as exc:
        write_records(adapter, records, sha="s1", date="2026-10-02")
    msg = str(exc.value)
    assert "2 documents citation(s)" in msg
    assert "record 'Alpha'" in msg and "wrong kind" in msg
    assert "record 'Beta'" in msg and "kind not indexed" in msg
    assert adapter.find_entities({"tier": "concept"}) == []


# ------------------------------------------------------------ CLI
def _store(tmp_path):
    db = tmp_path / "graph.db"
    a = SqliteGraphAdapter(db)
    a.open("corp")
    _seed(a)
    a.close()
    return db


def _write(tmp_path, records, name="map-unit-01.json"):
    p = tmp_path / name
    p.write_text(json.dumps(records), encoding="utf-8")
    return p


def test_check_records_cli_exits_zero_on_a_clean_file(tmp_path, capsys):
    db = _store(tmp_path)
    rec = _write(tmp_path, {"pages": [_page()], "reports": []})
    assert main(["check-records", "--db", str(db), "--corpus", "corp",
                 "--records", str(rec)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["problems"] == [] and out["citations"] == 1


def test_check_records_cli_exits_one_and_names_each_problem(tmp_path, capsys):
    db = _store(tmp_path)
    rec = _write(tmp_path, {"pages": [_page(documents=[_cite("constant", "LIMIT")])]})
    assert main(["check-records", "--db", str(db), "--corpus", "corp",
                 "--records", str(rec)]) == 1
    captured = capsys.readouterr()
    assert len(json.loads(captured.out)["problems"]) == 1
    assert "PROBLEM record 'Widget Service'" in captured.err
    assert "kind not indexed" in captured.err


def test_check_records_cli_refuses_a_bare_array(tmp_path, capsys):
    db = _store(tmp_path)
    rec = _write(tmp_path, [_page()])
    assert main(["check-records", "--db", str(db), "--corpus", "corp",
                 "--records", str(rec)]) == 1
    assert "JSON object" in capsys.readouterr().err


def test_check_records_cli_fails_loud_without_a_store(tmp_path, capsys):
    rec = _write(tmp_path, {"pages": [_page()]})
    assert main(["check-records", "--db", str(tmp_path / "missing.db"),
                 "--corpus", "corp", "--records", str(rec)]) == 1
    assert "check-records FAILED" in capsys.readouterr().err


def test_check_records_cli_fails_loud_on_a_corpus_with_no_symbols(tmp_path, capsys):
    db = _store(tmp_path)
    rec = _write(tmp_path, {"pages": [_page()]})
    assert main(["check-records", "--db", str(db), "--corpus", "crop",
                 "--records", str(rec)]) == 1
    err = capsys.readouterr().err
    assert "check-records FAILED" in err and "'crop'" in err
    assert "kind not indexed" not in err
