"""One-time migration of body-stored diagrams into `_diagram` (gh#465 item 4).

Older pages hold their validated mermaid as a `## Diagram` section in the BODY,
where the `_diagram` carry-forward (gh#497) cannot see it. The migration moves
each such diagram into `_diagram`/`_diagram_sha` exactly as the diagram phase
stores one, strips the section, refuses (never drops) an invalid diagram, and is
a no-op on a second run. The rendered page keeps exactly one Diagram section.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import entity_row

from autodoc_graph.cli import main
from autodoc_graph.migrate_diagrams import (
    MigrateError,
    load_mermaid_validator,
    migrate_body_diagrams,
)
from autodoc_graph.records import concept_id, write_records
from autodoc_graph.render_pages import render_pages
from autodoc_graph.sqlite_backend import SqliteGraphAdapter

VALIDATOR = Path(__file__).resolve().parents[5] / "scripts" / "validate_mermaid.py"

_ER = "erDiagram\n  GADGET {\n    int id\n  }"
_SECTION = f"## Diagram\n\n```mermaid\n{_ER}\n```\n\n"
_GADGET = "data-models/Gadget.md"
_GADGET_ID = concept_id("corp", "data-model", "Gadget")


def _gadget(body=None, frontmatter=None, title="Gadget"):
    return {
        "file": f"data-models/{title}.md", "type": "data-model", "title": title,
        "source_paths": ["app/x.py"], "summary": "A gadget.",
        "body": body if body is not None else "A gadget.\n\n" + _SECTION + "## Fields\n\n`id`\n",
        "frontmatter": frontmatter if frontmatter is not None else {
            "model_kind": "schema", "path": "app/x.py", "language": "python",
            "diagram": True, "diagram_source_sha": "abc1234"},
        "documents": [], "edges": [],
    }


def _service(body):
    return {
        "file": "services/Gadget Service.md", "type": "service", "title": "Gadget Service",
        "source_paths": ["app/x.py"], "summary": "Serves gadgets.", "body": body,
        "frontmatter": {"service_kind": "api", "path": "app/", "language": "python",
                        "entrypoint": "app/x.py"},
        "documents": [], "edges": [],
    }


def _seed(adapter, pages, meta_pages=()):
    adapter.upsert_entities([entity_row("sym:corp:app/x.py:class:Widget",
                                        kind="class", name="Widget")])
    adapter.update_corpus(repo_ref="owner/repo", branch="main", last_indexed_sha="s1",
                          project="demo")
    write_records(adapter, {"pages": list(pages), "meta_pages": list(meta_pages)},
                  sha="s1", date="2026-08-07")


def _ok(_source):
    return []


@pytest.fixture
def validate():
    return load_mermaid_validator(VALIDATOR)


# ------------------------------------------------------------ the move
def test_body_diagram_moves_into_the_reserved_key(adapter, validate):
    _seed(adapter, [_gadget()])
    report = migrate_body_diagrams(adapter, validate)
    ent = adapter.get_entity(_GADGET_ID)
    assert ent["frontmatter"]["_diagram"] == _ER
    # the drawn-at sha the page already carried, as the diagram phase stores it
    assert ent["frontmatter"]["_diagram_sha"] == "abc1234"
    assert "## Diagram" not in ent["body"]
    assert ent["body"] == "A gadget.\n\n## Fields\n\n`id`\n"
    assert report["migrated"] == [{"file": _GADGET, "diagram_sha": "abc1234",
                                   "render_identical": True}]
    assert report["refused"] == [] and report["left"] == []
    assert report["dry_run"] is False


def test_a_page_with_no_recorded_sha_gets_an_empty_one(adapter, validate):
    _seed(adapter, [_gadget(frontmatter={"model_kind": "schema", "path": "app/x.py",
                                         "language": "python"})])
    report = migrate_body_diagrams(adapter, validate)
    # empty, never a guessed sha: diagram-staleness then flags it for a redraw
    assert adapter.get_entity(_GADGET_ID)["frontmatter"]["_diagram_sha"] == ""
    assert report["migrated"][0]["diagram_sha"] == ""


def test_second_run_is_a_no_op(adapter, validate):
    _seed(adapter, [_gadget()])
    migrate_body_diagrams(adapter, validate)
    before = adapter.get_entity(_GADGET_ID)
    report = migrate_body_diagrams(adapter, validate)
    assert report["migrated"] == [] and report["refused"] == [] and report["left"] == []
    assert adapter.get_entity(_GADGET_ID) == before


def test_dry_run_reports_and_writes_nothing(adapter, validate):
    _seed(adapter, [_gadget()])
    before = adapter.get_entity(_GADGET_ID)
    report = migrate_body_diagrams(adapter, validate, dry_run=True)
    assert report["dry_run"] is True
    assert [m["file"] for m in report["migrated"]] == [_GADGET]
    assert adapter.get_entity(_GADGET_ID) == before


def test_invalid_mermaid_is_refused_and_kept(adapter, validate):
    bad = "## Diagram\n\n```mermaid\nnotADiagram\n  A --> B\n```\n\n"
    _seed(adapter, [_gadget(body="A gadget.\n\n" + bad + "## Fields\n\n`id`\n")])
    before = adapter.get_entity(_GADGET_ID)
    report = migrate_body_diagrams(adapter, validate)
    assert report["migrated"] == []
    assert len(report["refused"]) == 1
    assert report["refused"][0]["file"] == _GADGET
    assert "unknown diagram type" in report["refused"][0]["reason"]
    assert adapter.get_entity(_GADGET_ID) == before  # never dropped


def test_a_section_carrying_more_than_one_fence_is_refused(adapter):
    prose = "## Diagram\n\nA caption worth keeping.\n\n```mermaid\n" + _ER + "\n```\n\n"
    _seed(adapter, [_gadget(body="A gadget.\n\n" + prose + "## Fields\n\n`id`\n")])
    report = migrate_body_diagrams(adapter, _ok)
    assert report["migrated"] == []
    assert "besides one mermaid fence" in report["refused"][0]["reason"]


def test_a_second_fence_inside_the_section_is_refused_even_by_a_lenient_validator(adapter):
    # the in-section guard, not the validator, refuses this: the lazy match would
    # otherwise swallow both fences as one "diagram"
    two = f"## Diagram\n\n```mermaid\n{_ER}\n```\n\n```mermaid\n{_ER}\n```\n\n"
    _seed(adapter, [_gadget(body="A gadget.\n\n" + two + "## Fields\n\n`id`\n")])
    before = adapter.get_entity(_GADGET_ID)
    report = migrate_body_diagrams(adapter, _ok)
    assert report["migrated"] == []
    assert "besides one mermaid fence" in report["refused"][0]["reason"]
    assert adapter.get_entity(_GADGET_ID) == before


def _store_body(adapter, body):
    # bypasses write_records, which would normalise the body on the way in
    ent = adapter.get_entity(_GADGET_ID)
    adapter.upsert_entities([{**ent, "body": body}])


def test_a_crlf_body_is_migrated(adapter):
    _seed(adapter, [_gadget()])
    _store_body(adapter, ("A gadget.\n\n" + _SECTION + "## Fields\n\n`id`\n").replace("\n", "\r\n"))
    report = migrate_body_diagrams(adapter, _ok)
    assert report["refused"] == [] and [m["file"] for m in report["migrated"]] == [_GADGET]
    ent = adapter.get_entity(_GADGET_ID)
    assert ent["frontmatter"]["_diagram"] == _ER
    assert ent["body"] == "A gadget.\n\n## Fields\n\n`id`\n"
    assert migrate_body_diagrams(adapter, _ok)["migrated"] == []


def test_a_trailing_section_leaves_no_blank_tail(adapter):
    _seed(adapter, [_gadget(body="A gadget.\n\n## Fields\n\n`id`\n\n" + _SECTION)])
    report = migrate_body_diagrams(adapter, _ok)
    assert [m["file"] for m in report["migrated"]] == [_GADGET]
    assert adapter.get_entity(_GADGET_ID)["body"] == "A gadget.\n\n## Fields\n\n`id`\n"


def test_two_diagram_sections_are_refused(adapter):
    body = "A gadget.\n\n" + _SECTION + "## Fields\n\n`id`\n\n" + _SECTION
    _seed(adapter, [_gadget(body=body)])
    report = migrate_body_diagrams(adapter, _ok)
    assert report["migrated"] == []
    assert "2 '## Diagram' sections" in report["refused"][0]["reason"]


def test_a_kind_the_renderer_draws_no_diagram_for_is_left_in_the_body(adapter):
    body = "Serves gadgets.\n\n" + _SECTION + "## Source\n\n`app/x.py`\n"
    meta = {"file": "overview.md", "title": "corp Overview",
            "body": "Overview.\n\n" + _SECTION + "## Map\n\nx\n"}
    _seed(adapter, [_service(body)], meta_pages=[meta])
    report = migrate_body_diagrams(adapter, _ok)
    assert report["migrated"] == [] and report["refused"] == []
    left = {e["file"]: e["reason"] for e in report["left"]}
    assert set(left) == {"services/Gadget Service.md", "overview.md"}
    assert "renders no _diagram" in left["overview.md"]
    svc = adapter.get_entity(concept_id("corp", "service", "Gadget Service"))
    assert svc["body"].count("## Diagram") == 1 and "_diagram" not in svc["frontmatter"]


def test_a_page_already_holding_a_stored_diagram_is_left(adapter):
    _seed(adapter, [_gadget()])
    ent = adapter.get_entity(_GADGET_ID)
    adapter.upsert_entities([{**ent, "frontmatter": {**ent["frontmatter"],
                                                      "_diagram": _ER,
                                                      "_diagram_sha": "s1"}}])
    report = migrate_body_diagrams(adapter, _ok)
    assert report["migrated"] == []
    assert "already holds a stored _diagram" in report["left"][0]["reason"]


# ------------------------------------------------------------ render parity
def _render(adapter, out):
    render_pages(adapter, out)
    text = (out / _GADGET).read_text(encoding="utf-8")
    # updated: is the store's content-change stamp, and the move IS a store change
    return re.sub(r"^updated: .*$", "updated: X", text, flags=re.M)


def test_render_is_unchanged_by_the_migration(adapter, validate, tmp_path):
    _seed(adapter, [_gadget()])
    before = _render(adapter, tmp_path / "before")
    migrate_body_diagrams(adapter, validate)
    after = _render(adapter, tmp_path / "after")
    assert after == before
    assert after.count("## Diagram") == 1


def test_a_section_out_of_canonical_place_still_renders_once(adapter, validate, tmp_path):
    body = "A gadget.\n\n## Fields\n\n`id`\n\n" + _SECTION + "## Notes\n\nn\n"
    _seed(adapter, [_gadget(body=body)])
    report = migrate_body_diagrams(adapter, validate)
    assert report["migrated"][0]["render_identical"] is False
    after = _render(adapter, tmp_path / "after")
    assert after.count("## Diagram") == 1 and _ER in after


# ------------------------------------------------------------ validator loading
def test_missing_validator_file_fails_loud(tmp_path):
    with pytest.raises(MigrateError, match="not found"):
        load_mermaid_validator(tmp_path / "nope.py")


def test_validator_without_a_validate_function_fails_loud(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(MigrateError, match="validate"):
        load_mermaid_validator(stub)


# ------------------------------------------------------------ CLI
def _store(tmp_path, pages):
    db = tmp_path / "graph.db"
    a = SqliteGraphAdapter(db)
    a.open("corp")
    _seed(a, pages)
    a.close()
    return db


def _cli(db, *extra):
    return main(["migrate-diagrams", "--db", str(db), "--corpus", "corp",
                 "--validator", str(VALIDATOR), *extra])


def test_cli_dry_run_then_apply_then_no_op(tmp_path, capsys):
    db = _store(tmp_path, [_gadget()])
    assert _cli(db, "--dry-run") == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and [m["file"] for m in dry["migrated"]] == [_GADGET]
    assert _cli(db) == 0
    applied = json.loads(capsys.readouterr().out)
    assert [m["file"] for m in applied["migrated"]] == [_GADGET]
    assert _cli(db) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["migrated"] == [] and again["refused"] == []


def test_cli_exits_one_when_a_diagram_is_refused(tmp_path, capsys):
    bad = "## Diagram\n\n```mermaid\nnotADiagram\n```\n\n"
    db = _store(tmp_path, [_gadget(body="A gadget.\n\n" + bad + "## Fields\n\n`id`\n")])
    assert _cli(db) == 1
    err = capsys.readouterr().err
    assert "REFUSED data-models/Gadget.md" in err


def test_cli_fails_loud_on_a_missing_validator(tmp_path, capsys):
    db = _store(tmp_path, [_gadget()])
    assert main(["migrate-diagrams", "--db", str(db), "--corpus", "corp",
                 "--validator", str(tmp_path / "nope.py")]) == 1
    assert "migrate-diagrams FAILED" in capsys.readouterr().err
