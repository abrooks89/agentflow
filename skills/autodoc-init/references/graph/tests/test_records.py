"""Agent-return record writer (epic 704).

Agents return records; the writer derives ids, validates the closed agent
vocabulary, resolves `documents` targets to symbol entities, and persists
everything through the adapter. Every rejection is loud and names the record.
"""

from __future__ import annotations

import json

import pytest
from conftest import entity_row

from autodoc_graph.records import (
    RecordsError,
    collect_coverage,
    concept_id,
    diagram_carried_note,
    duplicate_claim_warning,
    enforce_join_floor,
    join_rates,
    load_assignment,
    load_records_input,
    require_reports,
    resolve_duplicate_claims,
    slugify,
    stale_report_communities,
    symbol_id,
    write_records,
)


def _seed_symbols(adapter):
    """Two symbol entities matching the (file, kind, name) resolver contract."""
    adapter.upsert_entities([
        entity_row("sym:corp:app/x.py:function:handler",
                   kind="function", name="handler",
                   span={"file": "app/x.py", "start_line": 1, "end_line": 5}),
        entity_row("sym:corp:app/x.py:class:Widget",
                   kind="class", name="Widget",
                   span={"file": "app/x.py", "start_line": 10, "end_line": 30}),
    ])


def _page(**over):
    """A minimal valid page record as a map agent returns it."""
    rec = {
        "file": "services/Widget Service.md",
        "type": "service",
        "title": "Widget Service",
        "source_paths": ["app/x.py"],
        "summary": "Serves widgets.",
        "body": "Serves widgets.\n\n## Source\n\n`app/x.py`\n",
        "frontmatter": {
            "service_kind": "api", "path": "app/", "language": "python",
            "entrypoint": "app/x.py", "status": "active",
        },
        "documents": [{"file": "app/x.py", "kind": "function", "name": "handler"}],
        "edges": [],
    }
    rec.update(over)
    return rec


def _records(**over):
    recs = {"pages": [_page()], "reports": [], "meta_pages": [], "diagrams": []}
    recs.update(over)
    return recs


# --------------------------------------------------------------- identity
def test_slugify_is_stable_and_safe():
    assert slugify("Widget Service") == "widget-service"
    assert slugify("POST /api/projects") == "post-api-projects"
    assert slugify("  Weird -- Título! ") == "weird-t-tulo"


def test_concept_and_symbol_id_shapes():
    assert concept_id("corp", "service", "Widget Service") == "con:corp:service:widget-service"
    # mirrors emit-graph.js entityId (path stays verbatim, forward slashes)
    assert symbol_id("corp", "app\\x.py", "function", "handler") == \
        "sym:corp:app/x.py:function:handler"


# --------------------------------------------------------------- happy path
def test_write_records_persists_concepts_and_documents_edges(adapter):
    _seed_symbols(adapter)
    report = write_records(adapter, _records(), sha="sha1", date="2026-08-07")
    ent = adapter.get_entity("con:corp:service:widget-service")
    assert ent is not None
    assert ent["tier"] == "concept" and ent["kind"] == "service"
    assert ent["origin"] == "agent"
    assert ent["qualified_name"] == "services/Widget Service.md"
    assert ent["frontmatter"]["_created"] == "2026-08-07"
    edges = adapter.find_edges({"rel": "documents"})
    assert len(edges) == 1
    assert edges[0]["src_id"] == "con:corp:service:widget-service"
    assert edges[0]["dst_id"] == "sym:corp:app/x.py:function:handler"
    assert edges[0]["origin"] == "agent"
    assert edges[0]["confidence"] == pytest.approx(0.7)
    assert report["pages"] == 1
    assert report["documents_edges"] == 1


def test_created_is_carried_forward_on_rewrite(adapter):
    _seed_symbols(adapter)
    write_records(adapter, _records(), sha="sha1", date="2026-08-01")
    page = _page(body="Serves widgets differently now.\n")
    write_records(adapter, _records(pages=[page]), sha="sha2", date="2026-08-07")
    ent = adapter.get_entity("con:corp:service:widget-service")
    assert ent["frontmatter"]["_created"] == "2026-08-01"  # first-write date survives
    assert ent["last_seen_sha"] == "sha2"


def test_concept_edges_resolve_within_batch(adapter):
    _seed_symbols(adapter)
    p1 = _page()
    p2 = _page(
        file="components/Widget Helper.md", type="component", title="Widget Helper",
        frontmatter={"component_kind": "util", "path": "app/x.py", "language": "python"},
        documents=[{"file": "app/x.py", "kind": "class", "name": "Widget"}],
        edges=[{"rel": "part_of", "dst_type": "service", "dst_title": "Widget Service"}],
    )
    write_records(adapter, _records(pages=[p1, p2]), sha="s", date="2026-08-07")
    edges = adapter.find_edges({"rel": "part_of"})
    assert len(edges) == 1
    assert edges[0]["src_id"] == "con:corp:component:widget-helper"
    assert edges[0]["dst_id"] == "con:corp:service:widget-service"


def test_meta_pages_are_stored_as_meta_kind(adapter):
    _seed_symbols(adapter)
    recs = _records(meta_pages=[
        {"file": "overview.md", "title": "corp Overview", "body": "The big picture."},
    ])
    write_records(adapter, recs, sha="s", date="2026-08-07")
    ent = adapter.get_entity("con:corp:meta:corp-overview")
    assert ent is not None and ent["kind"] == "meta"
    assert ent["qualified_name"] == "overview.md"


def test_meta_page_outside_allowed_set_is_rejected(adapter):
    recs = _records(pages=[], meta_pages=[
        {"file": "evil.md", "title": "Evil", "body": "x"},
    ])
    with pytest.raises(RecordsError, match=r"evil.md"):
        write_records(adapter, recs, sha="s", date="2026-08-07")


def test_diagram_lands_in_frontmatter(adapter):
    _seed_symbols(adapter)
    page = _page(
        file="data-models/Widget.md", type="data-model", title="Widget",
        frontmatter={"model_kind": "schema", "path": "app/x.py", "language": "python"},
        documents=[{"file": "app/x.py", "kind": "class", "name": "Widget"}],
    )
    recs = _records(pages=[page], diagrams=[
        {"type": "data-model", "title": "Widget",
         "mermaid": "erDiagram\n  WIDGET {\n  }\n  %% source: app/x.py",
         "source_sha": "sha1"},
    ])
    write_records(adapter, recs, sha="sha1", date="2026-08-07")
    ent = adapter.get_entity("con:corp:data-model:widget")
    assert ent["frontmatter"]["_diagram"].startswith("erDiagram")
    assert ent["frontmatter"]["_diagram_sha"] == "sha1"


def test_diagram_for_unknown_page_is_rejected(adapter):
    recs = _records(pages=[], diagrams=[
        {"type": "flow", "title": "Ghost Flow", "mermaid": "flowchart TD", "source_sha": "s"},
    ])
    with pytest.raises(RecordsError, match="Ghost Flow"):
        write_records(adapter, recs, sha="s", date="2026-08-07")


def test_tour_pages_allow_empty_source_paths(adapter):
    """Tours teach existing pages and have no source of their own — the
    graph-check exemption, mirrored at write time."""
    _seed_symbols(adapter)
    tour = {
        "file": "tours/Widget Tour.md", "type": "tour", "title": "Widget Tour",
        "source_paths": [], "summary": "Learn widgets.",
        "body": "Learn widgets bottom-up.\n",
        "frontmatter": {"tour_kind": "subsystem",
                        "steps": ["[[Widget Service]]"]},
        "documents": [], "edges": [],
    }
    write_records(adapter, _records(pages=[tour]), sha="s", date="2026-08-07")
    ent = adapter.get_entity("con:corp:tour:widget-tour")
    assert ent is not None and ent["source_paths"] == []


# --------------------------------------------------------------- rejections
def test_agent_minted_id_is_rejected(adapter):
    _seed_symbols(adapter)
    page = _page(id="con:corp:service:sneaky")
    with pytest.raises(RecordsError, match="mint"):
        write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


def test_unknown_rel_is_rejected_naming_the_record(adapter):
    _seed_symbols(adapter)
    page = _page(edges=[{"rel": "calls", "dst_type": "service", "dst_title": "X"}])
    with pytest.raises(RecordsError, match=r"Widget Service.*calls|calls.*Widget Service"):
        write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


def test_documents_rel_inside_edges_is_rejected(adapter):
    _seed_symbols(adapter)
    page = _page(edges=[{"rel": "documents", "dst_type": "service", "dst_title": "X"}])
    with pytest.raises(RecordsError, match="documents"):
        write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


def test_unresolvable_documents_target_is_rejected(adapter):
    _seed_symbols(adapter)
    page = _page(documents=[{"file": "app/x.py", "kind": "function", "name": "ghost"}])
    with pytest.raises(RecordsError, match="ghost"):
        write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


def test_unresolvable_concept_edge_target_is_rejected(adapter):
    _seed_symbols(adapter)
    page = _page(edges=[{"rel": "related", "dst_type": "service", "dst_title": "Nowhere"}])
    with pytest.raises(RecordsError, match="Nowhere"):
        write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


def test_empty_body_is_rejected(adapter):
    _seed_symbols(adapter)
    page = _page(body="   \n")
    with pytest.raises(RecordsError, match="body"):
        write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


def test_missing_required_frontmatter_field_is_rejected(adapter):
    _seed_symbols(adapter)
    page = _page(frontmatter={"service_kind": "api", "path": "app/", "language": "python"})
    with pytest.raises(RecordsError, match="entrypoint"):
        write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


def test_endpoint_requires_route_or_routes(adapter):
    _seed_symbols(adapter)
    base = {
        "file": "endpoints/POST api-widgets.md", "type": "endpoint",
        "title": "POST /api/widgets", "source_paths": ["app/x.py"],
        "summary": "Creates a widget.", "body": "Creates a widget.\n",
        "documents": [], "edges": [],
    }
    fm = {"method": "POST", "aliases": ["POST /api/widgets"],
          "auth": "required", "handler": "app/x.py:handler"}
    with pytest.raises(RecordsError, match="route"):
        write_records(adapter, _records(pages=[{**base, "frontmatter": dict(fm)}]),
                      sha="s", date="2026-08-07")
    ok = {**base, "frontmatter": {**fm, "route": "/api/widgets"}}
    write_records(adapter, _records(pages=[ok]), sha="s", date="2026-08-07")


def test_folder_type_mismatch_is_rejected(adapter):
    _seed_symbols(adapter)
    page = _page(file="jobs/Widget Service.md")
    with pytest.raises(RecordsError, match="jobs"):
        write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


def test_slug_collision_within_batch_is_rejected(adapter):
    _seed_symbols(adapter)
    p1 = _page()
    p2 = _page(file="services/Widget  Service.md", title="Widget  Service")
    with pytest.raises(RecordsError, match="collide"):
        write_records(adapter, _records(pages=[p1, p2]), sha="s", date="2026-08-07")


def test_validation_failure_writes_nothing(adapter):
    _seed_symbols(adapter)
    good = _page()
    bad = _page(file="components/Bad.md", type="component", title="Bad",
                frontmatter={"component_kind": "util", "path": "a", "language": "py"},
                body="")
    with pytest.raises(RecordsError):
        write_records(adapter, _records(pages=[good, bad]), sha="s", date="2026-08-07")
    assert adapter.get_entity("con:corp:service:widget-service") is None


# --------------------------------------------------------------- reports
def _seed_community(adapter, label="core"):
    adapter.upsert_communities([{
        "id": None, "level": 0, "label": label,
        "member_ids": ["sym:corp:app/x.py:function:handler"],
    }])
    return adapter.get_communities(0)[-1]["id"]


def test_report_is_written_with_summarized_sha(adapter):
    _seed_symbols(adapter)
    cid = _seed_community(adapter)
    recs = _records(reports=[{
        "community_id": cid, "summary": "The core community.",
        "findings": [{"text": "handler is the hub", "entities": ["handler"]}],
    }])
    write_records(adapter, recs, sha="shaX", date="2026-08-07")
    row = adapter.get_communities(0)[0]
    assert row["summarized_at_sha"] == "shaX"
    assert "core community" in row["report"]


def test_report_for_unknown_community_is_rejected(adapter):
    _seed_symbols(adapter)
    recs = _records(reports=[{"community_id": 999, "summary": "x", "findings": [{"text": "f1", "entities": []}]}])
    with pytest.raises(RecordsError, match="999"):
        write_records(adapter, recs, sha="s", date="2026-08-07")


def test_report_with_empty_summary_is_rejected(adapter):
    _seed_symbols(adapter)
    cid = _seed_community(adapter)
    recs = _records(reports=[{"community_id": cid, "summary": " ", "findings": [{"text": "f1", "entities": []}]}])
    with pytest.raises(RecordsError, match="summary"):
        write_records(adapter, recs, sha="s", date="2026-08-07")


def test_report_for_dissolved_community_is_rejected(adapter):
    _seed_symbols(adapter)
    cid = _seed_community(adapter)
    adapter.upsert_communities([{
        "id": cid, "level": 0, "descriptor": {"dissolved": True},
    }])
    recs = _records(reports=[{"community_id": cid, "summary": "x", "findings": [{"text": "f1", "entities": []}]}])
    with pytest.raises(RecordsError, match="dissolved"):
        write_records(adapter, recs, sha="s", date="2026-08-07")


def test_require_reports_names_the_empty_community(adapter):
    _seed_symbols(adapter)
    cid = _seed_community(adapter)
    with pytest.raises(RecordsError, match=str(cid)):
        require_reports(adapter, [cid])
    recs = _records(reports=[{"community_id": cid, "summary": "ok", "findings": [{"text": "f1", "entities": []}]}])
    write_records(adapter, recs, sha="s", date="2026-08-07")
    require_reports(adapter, [cid])  # no raise


# --------------------------------------------------------------- join rate
def test_join_rates_and_floor(adapter):
    _seed_symbols(adapter)
    joined = _page(
        file="data-models/Widget.md", type="data-model", title="Widget",
        frontmatter={"model_kind": "schema", "path": "app/x.py"},
        documents=[{"file": "app/x.py", "kind": "class", "name": "Widget"}],
    )
    bare = _page(
        file="data-models/Gadget.md", type="data-model", title="Gadget",
        frontmatter={"model_kind": "schema", "path": "app/x.py"},
        documents=[],
    )
    write_records(adapter, _records(pages=[joined, bare]), sha="s", date="2026-08-07")
    rates = join_rates(adapter)
    assert rates["data-model"]["total"] == 2
    assert rates["data-model"]["joined"] == 1
    assert rates["data-model"]["rate"] == pytest.approx(0.5)
    with pytest.raises(RecordsError, match="data-model"):
        enforce_join_floor(rates, 0.8)
    enforce_join_floor(rates, 0.5)  # at the floor: passes


# --------------------------------------------------------------- staleness
def _artifact(unit_ids, parents=None):
    return {
        "units": [{"key": "unit-01",
                   "communities": [{"id": cid, "label": f"c{cid}"} for cid in unit_ids]}],
        "parents": parents or [],
    }


def _report(cid, summary="ok"):
    return {"community_id": cid, "summary": summary,
            "findings": [{"text": "f1", "entities": []}]}


def test_stale_report_communities_zero_when_unchanged(adapter):
    _seed_symbols(adapter)
    cid = _seed_community(adapter)
    write_records(adapter, _records(reports=[_report(cid)]), sha="s", date="2026-08-07")
    churn = [{"level": 0, "created": [], "dissolved": [], "changed": []}]
    assert stale_report_communities(adapter, churn, _artifact([cid])) == []


def test_stale_report_communities_flags_changed_and_empty(adapter):
    _seed_symbols(adapter)
    c1 = _seed_community(adapter, "core")
    c2 = _seed_community(adapter, "edge")
    write_records(adapter, _records(reports=[_report(c1)]), sha="s", date="2026-08-07")
    churn = [{"level": 0, "created": [], "dissolved": [],
              "changed": [{"id": c1, "jaccard": 0.6, "added": 2, "removed": 1}]}]
    # c1 changed (membership moved), c2 has no report at all: both stale
    assert stale_report_communities(adapter, churn, _artifact([c1, c2])) == sorted([c1, c2])


def test_content_only_change_stales_the_report(adapter):
    """Adversarial finding 3 / reviewer D2: a commit that changes a member's
    CONTENT but not community membership must re-summarize — churn alone
    (created/changed keyed on membership) would miss it forever."""
    _seed_symbols(adapter)
    cid = _seed_community(adapter)
    write_records(adapter, _records(reports=[_report(cid)]), sha="s", date="2026-08-07")
    churn = [{"level": 0, "created": [], "dissolved": [], "changed": []}]
    assert stale_report_communities(adapter, churn, _artifact([cid])) == []
    # content-only change: same membership, new summary -> updated_at moves
    adapter.upsert_entities([entity_row(
        "sym:corp:app/x.py:function:handler", kind="function", name="handler",
        summary="def handler():  # changed body",
    )])
    assert stale_report_communities(adapter, churn, _artifact([cid])) == [cid]


def test_dissolved_child_stales_the_parent(adapter):
    """Adversarial finding 3 / reviewer D3: a parent whose child dissolved
    must re-summarize even when nothing else moved."""
    _seed_symbols(adapter)
    child = _seed_community(adapter, "child")
    adapter.upsert_communities([{
        "id": None, "level": 1, "label": "parent",
        "member_ids": ["sym:corp:app/x.py:function:handler"],
    }])
    parent = adapter.get_communities(1)[-1]["id"]
    artifact = _artifact([child], parents=[
        {"id": parent, "level": 1, "label": "parent", "children": [child]},
    ])
    write_records(adapter, _records(reports=[_report(child)]),
                  sha="s", date="2026-08-07", artifact=artifact)
    churn = [{"level": 0, "created": [], "dissolved": [], "changed": []}]
    assert stale_report_communities(adapter, churn, artifact) == []
    adapter.upsert_communities([{
        "id": child, "level": 0, "descriptor": {"dissolved": True},
    }])
    assert parent in stale_report_communities(adapter, churn, artifact)


def test_stale_child_propagates_to_parent(adapter):
    _seed_symbols(adapter)
    child = _seed_community(adapter, "child")
    adapter.upsert_communities([{
        "id": None, "level": 1, "label": "parent",
        "member_ids": ["sym:corp:app/x.py:function:handler"],
    }])
    parent = adapter.get_communities(1)[-1]["id"]
    artifact = _artifact([child], parents=[
        {"id": parent, "level": 1, "label": "parent", "children": [child]},
    ])
    write_records(adapter, _records(reports=[_report(child)]),
                  sha="s", date="2026-08-07", artifact=artifact)
    churn = [{"level": 0, "created": [], "dissolved": [],
              "changed": [{"id": child, "jaccard": 0.5, "added": 1, "removed": 0}]}]
    stale = stale_report_communities(adapter, churn, artifact)
    assert child in stale and parent in stale


def test_parent_child_set_change_stales_parent(adapter):
    _seed_symbols(adapter)
    c1 = _seed_community(adapter, "c1")
    c2 = _seed_community(adapter, "c2")
    adapter.upsert_communities([{
        "id": None, "level": 1, "label": "parent",
        "member_ids": ["sym:corp:app/x.py:function:handler"],
    }])
    parent = adapter.get_communities(1)[-1]["id"]
    art1 = _artifact([c1], parents=[
        {"id": parent, "level": 1, "label": "parent", "children": [c1]},
    ])
    write_records(adapter, _records(reports=[_report(c1)]),
                  sha="s", date="2026-08-07", artifact=art1)
    churn = [{"level": 0, "created": [], "dissolved": [], "changed": []}]
    assert stale_report_communities(adapter, churn, art1) == []
    # the parent now rolls up a different child set -> stale
    art2 = _artifact([c1, c2], parents=[
        {"id": parent, "level": 1, "label": "parent", "children": [c1, c2]},
    ])
    write_records(adapter, _records(reports=[_report(c2)]),
                  sha="s", date="2026-08-07")
    assert parent in stale_report_communities(adapter, churn, art2)


# --------------------------------------------------------------- roll-up
def test_single_child_parent_inherits_report_verbatim(adapter):
    """Adversarial finding 1a: a 1-child parent chain collapses — the parent
    inherits its child's report at write time, no agent spent."""
    _seed_symbols(adapter)
    child = _seed_community(adapter, "child")
    adapter.upsert_communities([{
        "id": None, "level": 1, "label": "mid",
        "member_ids": ["sym:corp:app/x.py:function:handler"],
    }])
    mid = adapter.get_communities(1)[-1]["id"]
    adapter.upsert_communities([{
        "id": None, "level": 2, "label": "root",
        "member_ids": ["sym:corp:app/x.py:function:handler"],
    }])
    root = adapter.get_communities(2)[-1]["id"]
    artifact = _artifact([child], parents=[
        {"id": mid, "level": 1, "label": "mid", "children": [child]},
        {"id": root, "level": 2, "label": "root", "children": [mid]},
    ])
    report = write_records(
        adapter,
        _records(reports=[_report(child, summary="the child summary")]),
        sha="s", date="2026-08-07", artifact=artifact,
    )
    assert report["inherited_reports"] == 2  # mid inherits child; root inherits mid
    mid_row = next(r for r in adapter.get_communities(1) if r["id"] == mid)
    root_row = next(r for r in adapter.get_communities(2) if r["id"] == root)
    for row, src_id in ((mid_row, child), (root_row, mid)):
        payload = json.loads(row["report"])
        assert payload["summary"] == "the child summary"  # verbatim
        assert payload["inherited_from"] == src_id
        assert row["summarized_at_sha"] == "s"
    require_reports(adapter, [child, mid, root])  # all stamped


def test_single_child_parent_with_no_child_report_fails(adapter):
    _seed_symbols(adapter)
    child = _seed_community(adapter, "child")
    adapter.upsert_communities([{
        "id": None, "level": 1, "label": "parent",
        "member_ids": ["sym:corp:app/x.py:function:handler"],
    }])
    parent = adapter.get_communities(1)[-1]["id"]
    artifact = _artifact([child], parents=[
        {"id": parent, "level": 1, "label": "parent", "children": [child]},
    ])
    with pytest.raises(RecordsError, match="inherit"):
        write_records(adapter, _records(), sha="s", date="2026-08-07",
                      artifact=artifact)


def test_multi_child_parent_never_inherits(adapter):
    _seed_symbols(adapter)
    c1 = _seed_community(adapter, "c1")
    c2 = _seed_community(adapter, "c2")
    adapter.upsert_communities([{
        "id": None, "level": 1, "label": "parent",
        "member_ids": ["sym:corp:app/x.py:function:handler"],
    }])
    parent = adapter.get_communities(1)[-1]["id"]
    artifact = _artifact([c1, c2], parents=[
        {"id": parent, "level": 1, "label": "parent", "children": [c1, c2]},
    ])
    report = write_records(
        adapter, _records(reports=[_report(c1), _report(c2)]),
        sha="s", date="2026-08-07", artifact=artifact,
    )
    assert report["inherited_reports"] == 0
    with pytest.raises(RecordsError, match=str(parent)):
        require_reports(adapter, [parent])


def test_report_requires_nonempty_findings(adapter):
    """Adversarial finding 7: findings: [] is a hollow report, not a report."""
    _seed_symbols(adapter)
    cid = _seed_community(adapter)
    recs = _records(reports=[{"community_id": cid, "summary": "ok", "findings": []}])
    with pytest.raises(RecordsError, match="findings"):
        write_records(adapter, recs, sha="s", date="2026-08-07")


# --------------------------------------------------------------- file paths
def test_duplicate_file_within_batch_is_rejected(adapter):
    """Adversarial finding 2: two records claiming one path would silently
    last-writer-win at render time."""
    _seed_symbols(adapter)
    p1 = _page()
    p2 = _page(title="Widget Service II",
               file="services/Widget Service.md")
    with pytest.raises(RecordsError, match="both claim"):
        write_records(adapter, _records(pages=[p1, p2]), sha="s", date="2026-08-07")


def test_file_claimed_by_stored_entity_is_rejected(adapter):
    _seed_symbols(adapter)
    write_records(adapter, _records(), sha="s", date="2026-08-07")
    thief = _page(
        title="Widget Servicing", file="services/Widget Service.md",
    )
    with pytest.raises(RecordsError, match="already rendered"):
        write_records(adapter, _records(pages=[thief]), sha="s", date="2026-08-07")


def test_reserved_nav_basenames_are_rejected(adapter):
    _seed_symbols(adapter)
    for bad in ("services/_index.md", "services/index.md", "services/Glossary.md"):
        page = _page(file=bad)
        with pytest.raises(RecordsError, match="reserved nav"):
            write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


def test_deep_nesting_is_rejected(adapter):
    _seed_symbols(adapter)
    page = _page(file="services/nested/Widget Service.md")
    with pytest.raises(RecordsError, match="no deeper nesting"):
        write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


# --------------------------------------------------------------- key injection
def test_frontmatter_key_injection_is_rejected(adapter):
    """Adversarial finding 6: a key with a newline would break out of the
    rendered YAML block."""
    _seed_symbols(adapter)
    for bad_key in ("evil\ninjected: true", "evil: injected", "_diagram", "9lives"):
        page = _page(frontmatter={
            "service_kind": "api", "path": "app/", "language": "python",
            "entrypoint": "app/x.py", bad_key: "x",
        })
        with pytest.raises(RecordsError, match="identifier"):
            write_records(adapter, _records(pages=[page]), sha="s", date="2026-08-07")


# --------------------------------------------------------------- disk input
def test_load_records_input_merges_a_directory(adapter, tmp_path):
    """Adversarial finding 5: the disk-based workflow contract — per-agent
    files under a run dir, merged deterministically."""
    _seed_symbols(adapter)
    d = tmp_path / "records"
    d.mkdir()
    (d / "map-unit-01.json").write_text(json.dumps({
        "pages": [_page()],
        "reports": [],
        "covered": [{"community": "1", "name": "handler", "line": 1}],
        "excluded": [],
    }), encoding="utf-8")
    (d / "flows.json").write_text(json.dumps({
        "pages": [{
            "file": "flows/Widget Flow.md", "type": "flow", "title": "Widget Flow",
            "source_paths": ["app/x.py"], "summary": "s.", "body": "b.",
            "frontmatter": {"flow_kind": "pipeline"}, "documents": [], "edges": [],
        }],
    }), encoding="utf-8")
    merged = load_records_input(d)
    assert len(merged["pages"]) == 2
    report = write_records(adapter, merged, sha="s", date="2026-08-07")
    assert report["pages"] == 2
    cov = collect_coverage(d)
    assert len(cov["pages"]) == 2 and len(cov["covered"]) == 1


def test_manifest_rejects_orphan_records_files(adapter, tmp_path):
    """Re-review item 1: a leftover *.json from a PRIOR attempt at the same
    runDir must not be silently swept into the batch."""
    d = tmp_path / "records"
    d.mkdir()
    listed = d / "map-unit-01.json"
    listed.write_text(json.dumps({"pages": []}), encoding="utf-8")
    orphan = d / "map-unit-99.json"
    orphan.write_text(json.dumps({"pages": []}), encoding="utf-8")
    with pytest.raises(RecordsError, match="map-unit-99"):
        load_records_input(d, manifest=[str(listed)])
    # manifest naming a missing file is equally loud
    with pytest.raises(RecordsError, match="missing"):
        load_records_input(d, manifest=[str(listed), str(orphan),
                                        str(d / "ghost.json")])
    # exact manifest: clean merge
    merged = load_records_input(d, manifest=[str(listed), str(orphan)])
    assert merged["pages"] == []


def test_load_records_input_rejects_bad_shapes(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RecordsError, match=r"no .*json"):
        load_records_input(empty)
    bad = tmp_path / "records"
    bad.mkdir()
    (bad / "x.json").write_text("not json", encoding="utf-8")
    with pytest.raises(RecordsError, match="not valid JSON"):
        load_records_input(bad)


def test_unembedded_diagrams_are_skipped_not_fatal(adapter):
    _seed_symbols(adapter)
    page = _page(
        file="data-models/Widget.md", type="data-model", title="Widget",
        frontmatter={"model_kind": "schema", "path": "app/x.py"},
        documents=[],
    )
    recs = _records(pages=[page], diagrams=[
        {"type": "data-model", "title": "Widget", "embedded": False,
         "reason": "validate failed twice"},
    ])
    write_records(adapter, recs, sha="shaZ", date="2026-08-07")
    ent = adapter.get_entity("con:corp:data-model:widget")
    assert "_diagram" not in ent["frontmatter"]


# --------------------------------------------------------------- duplicate claims (gh#455)
_DUP = "services/Widget Service.md"


def _claim(title, body_len=10):
    return _page(title=title, file=_DUP, body="x" * body_len + "\n")


def test_assigned_unit_wins_even_when_its_record_is_shorter(adapter):
    """Q-10077 rule 1: assigned-unit-wins is not richest-wins. The live
    2026-09-19 collision dropped the LONGER record, deliberately."""
    _seed_symbols(adapter)
    recs = _records(
        pages=[_claim("Widget Service Long", 9580), _claim("Widget Service", 7748)],
        page_units=["unit-05", "unit-18"],
    )
    report = write_records(adapter, recs, sha="s", date="2026-08-07",
                           assignment={_DUP.casefold(): "unit-18"})
    assert report["pages"] == 1
    assert report["duplicate_claims"] == [{
        "file": _DUP, "claimants": ["unit-05", "unit-18"], "assigned": "unit-18",
        "kept": "unit-18", "dropped": ["unit-05"],
    }]
    assert adapter.get_entity("con:corp:service:widget-service") is not None
    assert adapter.get_entity("con:corp:service:widget-service-long") is None


def test_unassigned_page_keeps_lowest_unit_id_and_names_the_gap(adapter):
    """Q-10077 rules 2 and 3: no claimant was assigned the page, so the lowest
    unit id wins whatever the input order, and the warning calls it a
    partitioner gap (the countable half)."""
    _seed_symbols(adapter)
    recs = _records(
        pages=[_claim("Widget Service Twenty", 900), _claim("Widget Service", 5),
               _claim("Widget Service Eleven", 400)],
        page_units=["unit-20", "unit-02", "unit-11"],
    )
    report = write_records(adapter, recs, sha="s", date="2026-08-07", assignment={})
    (claim,) = report["duplicate_claims"]
    assert claim == {
        "file": _DUP, "claimants": ["unit-02", "unit-11", "unit-20"],
        "assigned": None, "kept": "unit-02", "dropped": ["unit-11", "unit-20"],
    }
    assert adapter.get_entity("con:corp:service:widget-service") is not None
    warning = duplicate_claim_warning(claim)
    assert warning.startswith("WARNING: page 'services/Widget Service.md'")
    assert "partitioner gap" in warning and "kept unit-02" in warning


def test_lowest_unit_id_is_natural_not_lexical(adapter):
    _seed_symbols(adapter)
    recs = _records(pages=[_claim("Widget Service Big"), _claim("Widget Service")],
                    page_units=["unit-100", "unit-11"])
    report = write_records(adapter, recs, sha="s", date="2026-08-07", assignment={})
    assert report["duplicate_claims"][0]["kept"] == "unit-11"


def test_assigned_page_warning_is_not_a_gap():
    line = duplicate_claim_warning({
        "file": _DUP, "claimants": ["unit-05", "unit-18"], "assigned": "unit-18",
        "kept": "unit-18", "dropped": ["unit-05"],
    })
    assert "assigned to unit-18" in line and "partitioner gap" not in line


def test_without_assignment_duplicates_still_refuse(adapter):
    """No --assignment (autodoc-init): byte-identical refusal, provenance or not."""
    _seed_symbols(adapter)
    recs = _records(pages=[_claim("Widget Service A"), _claim("Widget Service B")],
                    page_units=["unit-01", "unit-02"])
    with pytest.raises(RecordsError, match="both claim"):
        write_records(adapter, recs, sha="s", date="2026-08-07")


def test_same_unit_duplicate_still_refuses(adapter):
    _seed_symbols(adapter)
    recs = _records(pages=[_claim("Widget Service A"), _claim("Widget Service B"),
                           _claim("Widget Service")],
                    page_units=["unit-03", "unit-03", "unit-04"])
    with pytest.raises(RecordsError, match=r"unit-03 claim the file .* more than once"):
        write_records(adapter, recs, sha="s", date="2026-08-07",
                      assignment={_DUP.casefold(): "unit-04"})
    assert adapter.find_entities({"tier": "concept"}) == []


def test_resolution_without_provenance_refuses(adapter):
    _seed_symbols(adapter)
    recs = _records(pages=[_claim("Widget Service A"), _claim("Widget Service B")])
    with pytest.raises(RecordsError, match="provenance"):
        write_records(adapter, recs, sha="s", date="2026-08-07", assignment={})


def test_meta_page_duplicates_still_refuse_under_assignment(adapter):
    _seed_symbols(adapter)
    meta = {"file": "overview.md", "title": "corp Overview", "body": "b."}
    recs = _records(pages=[], page_units=[],
                    meta_pages=[meta, {**meta, "title": "corp Overview II"}])
    with pytest.raises(RecordsError, match="both claim"):
        write_records(adapter, recs, sha="s", date="2026-08-07", assignment={})


def test_directory_input_records_unit_provenance(tmp_path):
    d = tmp_path / "records"
    d.mkdir()
    (d / "refresh-unit-05.json").write_text(json.dumps({"pages": [_page(), _page()]}),
                                            encoding="utf-8")
    (d / "refresh-symbolless.json").write_text(json.dumps({"pages": [_page()]}),
                                               encoding="utf-8")
    (d / "map-unit-01.json").write_text(json.dumps({"pages": [_page()]}),
                                        encoding="utf-8")
    merged = load_records_input(d)
    assert merged["page_units"] == ["unit-01", "symbolless", "unit-05", "unit-05"]
    single = tmp_path / "one.json"
    single.write_text(json.dumps(_records()), encoding="utf-8")
    assert "page_units" not in load_records_input(single)


def _write_assignment(tmp_path, entries):
    path = tmp_path / "changed.json"
    path.write_text(json.dumps([{"key": k, "affectedPages": p} for k, p in entries]),
                    encoding="utf-8")
    return path


def test_annotated_assignment_entries_do_not_assign(tmp_path):
    """Step 3.6 fan-out may list one page under several units; only the plain
    entry owns it."""
    path = _write_assignment(tmp_path, [
        ("unit-01", ["endpoints/Push.md"]),
        ("unit-02", ["endpoints/Push.md \u2014 consumer of changed contract \u2014 "
                     "re-verify claims about it"]),
        ("unit-03", ["endpoints/Push.md \u2014 contract pair \u2014 re-verify BOTH "
                     "sides against source",
                     "services/Only Annotated.md \u2014 contract pair \u2014 x"]),
    ])
    assert load_assignment(path) == {"endpoints/push.md": "unit-01"}


def test_plain_page_under_two_units_is_refused(tmp_path):
    path = _write_assignment(tmp_path, [("unit-01", ["endpoints/Push.md"]),
                                        ("unit-02", ["endpoints/push.md"])])
    with pytest.raises(RecordsError, match="assigned to both"):
        load_assignment(path)


def test_empty_assignment_list_is_a_legal_empty_assignment(tmp_path):
    """Step 3 writes [] when the diff maps to no community (docs-only runs),
    and the rolling audit still emits records then, so [] must load."""
    path = _write_assignment(tmp_path, [])
    assert load_assignment(path) == {}


def test_collision_is_reported_under_the_kept_records_spelling(adapter):
    _seed_symbols(adapter)
    recs = _records(
        pages=[_page(title="Widget Service Upper", file="services/WIDGET SERVICE.md"),
               _page(title="Widget Service", file=_DUP)],
        page_units=["unit-03", "unit-01"],
    )
    report = write_records(adapter, recs, sha="s", date="2026-08-07", assignment={})
    (claim,) = report["duplicate_claims"]
    assert claim["kept"] == "unit-01" and claim["file"] == _DUP


def test_symbolless_sorts_after_every_numbered_unit(adapter):
    _seed_symbols(adapter)
    recs = _records(pages=[_claim("Widget Service Fallback"), _claim("Widget Service")],
                    page_units=["symbolless", "unit-900"])
    report = write_records(adapter, recs, sha="s", date="2026-08-07", assignment={})
    (claim,) = report["duplicate_claims"]
    assert claim["claimants"] == ["unit-900", "symbolless"]
    assert claim["kept"] == "unit-900"


def test_equal_unit_numbers_tie_break_on_the_raw_key_not_file_order():
    pages = [_claim("Widget Service A"), _claim("Widget Service B")]
    for order in (["unit-2", "unit-02"], ["unit-02", "unit-2"]):
        kept, (claim,), _ = resolve_duplicate_claims(pages, order, {})
        assert claim["claimants"] == ["unit-02", "unit-2"]
        assert claim["kept"] == "unit-02"
        assert kept == [pages[order.index("unit-02")]]


def test_edge_to_a_dropped_title_names_the_tiebreak(adapter):
    _seed_symbols(adapter)
    caller = _page(title="Widget Caller", file="services/Widget Caller.md",
                   edges=[{"rel": "depends_on", "dst_type": "service",
                           "dst_title": "Widget Service Long"}])
    recs = _records(pages=[_claim("Widget Service Long"), _claim("Widget Service"), caller],
                    page_units=["unit-05", "unit-18", "unit-18"])
    with pytest.raises(RecordsError, match=(
            r"resolves to no concept entity.*that title belonged to a claim of "
            r"'services/Widget Service\.md' by unit-05 dropped by the "
            r"duplicate-claim tiebreak")):
        write_records(adapter, recs, sha="s", date="2026-08-07",
                      assignment={_DUP.casefold(): "unit-18"})
    assert adapter.find_entities({"tier": "concept"}) == []


def test_miss_on_a_title_the_kept_record_shares_does_not_blame_the_tiebreak(adapter):
    # both claims carry "Widget Service"; the edge misses on its dst_type, not
    # because the tiebreak removed the title, so the note must not appear
    _seed_symbols(adapter)
    caller = _page(title="Widget Caller", file="services/Widget Caller.md",
                   edges=[{"rel": "depends_on", "dst_type": "component",
                           "dst_title": "Widget Service"}])
    recs = _records(pages=[_claim("Widget Service"), _claim("Widget Service"), caller],
                    page_units=["unit-05", "unit-18", "unit-18"])
    with pytest.raises(RecordsError) as exc:
        write_records(adapter, recs, sha="s", date="2026-08-07",
                      assignment={_DUP.casefold(): "unit-18"})
    assert "tiebreak" not in str(exc.value)


def test_diagram_to_a_dropped_title_names_the_tiebreak(adapter):
    _seed_symbols(adapter)
    recs = _records(
        pages=[_claim("Widget Service Long"), _claim("Widget Service")],
        page_units=["unit-05", "unit-18"],
        diagrams=[{"type": "service", "title": "Widget Service Long",
                   "embedded": True, "mermaid": "graph TD; A-->B"}],
    )
    with pytest.raises(RecordsError, match=(
            r"diagram targets unknown page 'Widget Service Long'.*dropped by the "
            r"duplicate-claim tiebreak")):
        write_records(adapter, recs, sha="s", date="2026-08-07",
                      assignment={_DUP.casefold(): "unit-18"})


# ------------------------------------------- stored diagram carry-forward (gh#497)
_WIDGET_ID = "con:corp:data-model:widget"
_OLD_ER = "erDiagram\n  WIDGET {\n  }\n  %% source: app/x.py"


def _widget_page(**over):
    page = _page(
        file="data-models/Widget.md", type="data-model", title="Widget",
        frontmatter={"model_kind": "schema", "path": "app/x.py", "language": "python"},
        documents=[{"file": "app/x.py", "kind": "class", "name": "Widget"}],
    )
    page.update(over)
    return page


def _widget_diagram(mermaid=_OLD_ER, sha="418bec0c1234"):
    return {"type": "data-model", "title": "Widget", "mermaid": mermaid,
            "source_sha": sha}


def _seed_widget_diagram(adapter):
    _seed_symbols(adapter)
    write_records(adapter, _records(pages=[_widget_page()], diagrams=[_widget_diagram()]),
                  sha="s1", date="2026-08-07")


def test_rewrite_without_diagram_entry_keeps_stored_diagram(adapter):
    _seed_widget_diagram(adapter)
    report = write_records(adapter, _records(pages=[_widget_page(body="New body.\n")]),
                           sha="s2", date="2026-09-23")
    fm = adapter.get_entity(_WIDGET_ID)["frontmatter"]
    assert fm["_diagram"] == _OLD_ER
    assert fm["_diagram_sha"] == "418bec0c1234"
    assert report["diagrams_carried"] == [
        {"file": "data-models/Widget.md", "diagram_sha": "418bec0c1234"}]
    assert report["diagrams_dropped"] == []


def test_rewrite_with_matching_diagram_entry_replaces_stored_diagram(adapter):
    _seed_widget_diagram(adapter)
    new = "erDiagram\n  WIDGET {\n    int id\n  }"
    report = write_records(
        adapter, _records(pages=[_widget_page()], diagrams=[_widget_diagram(new, "s2")]),
        sha="s2", date="2026-09-23")
    fm = adapter.get_entity(_WIDGET_ID)["frontmatter"]
    assert fm["_diagram"] == new and fm["_diagram_sha"] == "s2"
    assert report["diagrams_carried"] == []


def test_drop_diagram_removes_stored_diagram_and_reports_it(adapter):
    _seed_widget_diagram(adapter)
    report = write_records(adapter, _records(pages=[_widget_page(drop_diagram=True)]),
                           sha="s2", date="2026-09-23")
    fm = adapter.get_entity(_WIDGET_ID)["frontmatter"]
    assert "_diagram" not in fm and "_diagram_sha" not in fm
    assert report["diagrams_dropped"] == ["data-models/Widget.md"]
    assert report["diagrams_carried"] == []


def test_non_bool_drop_diagram_is_refused(adapter):
    _seed_widget_diagram(adapter)
    with pytest.raises(RecordsError, match=r"'Widget'.*drop_diagram"):
        write_records(adapter, _records(pages=[_widget_page(drop_diagram="yes")]),
                      sha="s2", date="2026-09-23")


def test_drop_diagram_with_matching_entry_is_refused(adapter):
    _seed_widget_diagram(adapter)
    with pytest.raises(RecordsError, match=r"'Widget'.*drop_diagram"):
        write_records(
            adapter,
            _records(pages=[_widget_page(drop_diagram=True)], diagrams=[_widget_diagram()]),
            sha="s2", date="2026-09-23")
    # refused before any write: the stored diagram is intact
    assert adapter.get_entity(_WIDGET_ID)["frontmatter"]["_diagram"] == _OLD_ER


def test_first_write_without_stored_diagram_is_unaffected(adapter):
    _seed_symbols(adapter)
    report = write_records(adapter, _records(pages=[_widget_page()]),
                           sha="s1", date="2026-08-07")
    assert "_diagram" not in adapter.get_entity(_WIDGET_ID)["frontmatter"]
    assert report["diagrams_carried"] == [] and report["diagrams_dropped"] == []


def test_page_absent_from_batch_keeps_its_diagram_untouched(adapter):
    _seed_widget_diagram(adapter)
    before = adapter.get_entity(_WIDGET_ID)
    report = write_records(adapter, _records(), sha="s2", date="2026-09-23")
    after = adapter.get_entity(_WIDGET_ID)
    assert after["frontmatter"] == before["frontmatter"]
    assert after["last_seen_sha"] == before["last_seen_sha"]
    assert report["diagrams_carried"] == []


def test_unembedded_diagram_entry_still_carries_the_stored_diagram(adapter):
    # a diagram that failed validation arrives embedded:false; the stored one survives
    _seed_widget_diagram(adapter)
    entry = {**_widget_diagram("erDiagram\n  BROKEN", "s2"), "embedded": False}
    report = write_records(adapter, _records(pages=[_widget_page()], diagrams=[entry]),
                           sha="s2", date="2026-09-23")
    assert adapter.get_entity(_WIDGET_ID)["frontmatter"]["_diagram"] == _OLD_ER
    assert report["diagrams_carried"] == [
        {"file": "data-models/Widget.md", "diagram_sha": "418bec0c1234"}]


def test_drop_diagram_on_a_meta_record_is_refused(adapter):
    _seed_symbols(adapter)
    recs = _records(pages=[], meta_pages=[
        {"file": "Overview.md", "title": "corp Overview", "body": "b\n", "drop_diagram": True}])
    with pytest.raises(RecordsError, match="drop_diagram is not supported on meta"):
        write_records(adapter, recs, sha="s", date="2026-09-23")


# ------------------------------- body-stored diagram carry-forward (gh#465 item 1)
# Older pages keep their diagram as a `## Diagram` section in the stored BODY, not
# in `_diagram`, so the gh#497 carry had nothing to carry and a re-emit that
# omitted the section dropped it with `diagrams_dropped: []`.
_BODY_ER = "erDiagram\n  GADGET {\n  }"
_BODY_SECTION = f"## Diagram\n\n```mermaid\n{_BODY_ER}\n```\n\n"
_GADGET_ID = "con:corp:data-model:gadget"


def _gadget_page(body=None, **over):
    page = _page(
        file="data-models/Gadget.md", type="data-model", title="Gadget",
        frontmatter={"model_kind": "schema", "path": "app/x.py", "language": "python"},
        documents=[{"file": "app/x.py", "kind": "class", "name": "Widget"}],
        body=body if body is not None else (
            "A gadget.\n\n" + _BODY_SECTION + "## Fields\n\n`id`\n"),
    )
    page.update(over)
    return page


def _seed_gadget_body_diagram(adapter):
    _seed_symbols(adapter)
    write_records(adapter, _records(pages=[_gadget_page()]), sha="s1", date="2026-08-07")
    stored = adapter.get_entity(_GADGET_ID)
    assert "_diagram" not in stored["frontmatter"]  # body-stored, the exposed shape
    assert stored["body"].count("## Diagram") == 1


def test_rewrite_dropping_a_body_diagram_section_carries_it_forward(adapter):
    _seed_gadget_body_diagram(adapter)
    new_body = "A gadget, re-audited.\n\n## Fields\n\n`id`, `name`\n"
    report = write_records(adapter, _records(pages=[_gadget_page(body=new_body)]),
                           sha="s2", date="2026-09-24")
    body = adapter.get_entity(_GADGET_ID)["body"]
    assert body.count("## Diagram") == 1
    assert _BODY_ER in body
    assert "A gadget, re-audited." in body and "`id`, `name`" in body
    # placed where the renderer places a diagram: before the first section heading
    assert body.index("## Diagram") < body.index("## Fields")
    # the carried section ends at the next heading: a carry that ran to end-of-body
    # would bring the OLD '## Fields' along and this would hold two
    assert body == ("A gadget, re-audited.\n\n" + _BODY_SECTION
                    + "## Fields\n\n`id`, `name`\n")
    assert body.count("## Fields") == 1
    assert report["diagrams_carried"] == [
        {"file": "data-models/Gadget.md", "diagram_sha": None, "source": "body"}]
    assert report["diagrams_dropped"] == []


def test_body_diagram_carry_appends_when_the_new_body_has_no_sections(adapter):
    _seed_gadget_body_diagram(adapter)
    write_records(adapter, _records(pages=[_gadget_page(body="Just prose.\n")]),
                  sha="s2", date="2026-09-24")
    body = adapter.get_entity(_GADGET_ID)["body"]
    assert body.startswith("Just prose.\n\n## Diagram")
    assert body.count("## Diagram") == 1 and _BODY_ER in body


def test_rewrite_keeping_its_body_diagram_is_not_a_carry(adapter):
    _seed_gadget_body_diagram(adapter)
    report = write_records(adapter, _records(pages=[_gadget_page()]),
                           sha="s2", date="2026-09-24")
    assert adapter.get_entity(_GADGET_ID)["body"].count("## Diagram") == 1
    assert report["diagrams_carried"] == [] and report["diagrams_dropped"] == []


def test_diagrams_entry_supersedes_a_body_diagram_section(adapter):
    _seed_gadget_body_diagram(adapter)
    new = "erDiagram\n  GADGET {\n    int id\n  }"
    report = write_records(
        adapter,
        _records(pages=[_gadget_page(body="A gadget.\n\n## Fields\n\n`id`\n")],
                 diagrams=[{"type": "data-model", "title": "Gadget", "mermaid": new,
                            "source_sha": "s2"}]),
        sha="s2", date="2026-09-24")
    ent = adapter.get_entity(_GADGET_ID)
    assert "## Diagram" not in ent["body"]  # the entry replaces it; no second copy
    assert ent["frontmatter"]["_diagram"] == new
    assert report["diagrams_carried"] == [] and report["diagrams_dropped"] == []


def test_drop_diagram_drops_a_body_diagram_section_and_reports_it(adapter):
    _seed_gadget_body_diagram(adapter)
    report = write_records(
        adapter,
        _records(pages=[_gadget_page(body="A gadget.\n", drop_diagram=True)]),
        sha="s2", date="2026-09-24")
    assert "## Diagram" not in adapter.get_entity(_GADGET_ID)["body"]
    assert report["diagrams_dropped"] == ["data-models/Gadget.md"]
    assert report["diagrams_carried"] == []


def test_drop_diagram_reports_a_page_once_when_both_stores_held_a_diagram(adapter):
    _seed_gadget_body_diagram(adapter)
    write_records(adapter, _records(pages=[_gadget_page()], diagrams=[
        {"type": "data-model", "title": "Gadget", "mermaid": _BODY_ER,
         "source_sha": "s1"}]), sha="s1", date="2026-08-07")
    report = write_records(
        adapter,
        _records(pages=[_gadget_page(body="A gadget.\n", drop_diagram=True)]),
        sha="s2", date="2026-09-24")
    assert report["diagrams_dropped"] == ["data-models/Gadget.md"]


def test_meta_page_body_diagram_is_carried_forward(adapter):
    meta = {"file": "overview.md", "title": "corp Overview",
            "body": "Overview.\n\n" + _BODY_SECTION + "## Map\n\nx\n"}
    write_records(adapter, _records(pages=[], meta_pages=[meta]),
                  sha="s1", date="2026-08-07")
    report = write_records(
        adapter,
        _records(pages=[], meta_pages=[{**meta, "body": "Overview v2.\n\n## Map\n\ny\n"}]),
        sha="s2", date="2026-09-24")
    body = adapter.get_entity(concept_id("corp", "meta", "corp Overview"))["body"]
    assert body.count("## Diagram") == 1 and _BODY_ER in body
    assert "Overview v2." in body
    assert report["diagrams_carried"] == [
        {"file": "overview.md", "diagram_sha": None, "source": "body"}]


def test_body_diagram_carry_note_names_the_body_section():
    note = diagram_carried_note(
        {"file": "data-models/Gadget.md", "diagram_sha": None, "source": "body"})
    assert "data-models/Gadget.md" in note and "## Diagram" in note
    assert note.startswith("NOTE:")


def test_page_with_stored_diagram_and_body_section_is_carried_once(adapter):
    # the renderer replaces the body section with _diagram, so only _diagram is
    # carried: one diagrams_carried entry, one stderr note
    _seed_gadget_body_diagram(adapter)
    write_records(adapter, _records(pages=[_gadget_page()], diagrams=[
        {"type": "data-model", "title": "Gadget", "mermaid": _BODY_ER,
         "source_sha": "s1"}]), sha="s1", date="2026-08-07")
    report = write_records(
        adapter, _records(pages=[_gadget_page(body="A gadget.\n\n## Fields\n\n`id`\n")]),
        sha="s2", date="2026-09-24")
    ent = adapter.get_entity(_GADGET_ID)
    assert report["diagrams_carried"] == [
        {"file": "data-models/Gadget.md", "diagram_sha": "s1"}]
    assert ent["frontmatter"]["_diagram"] == _BODY_ER
    assert "## Diagram" not in ent["body"]


_FENCED_HEADING = "```markdown\n## Diagram\n\nexample text\n```\n\n"


def test_diagram_heading_inside_a_fence_is_not_a_body_section(adapter):
    _seed_symbols(adapter)
    fenced = "A gadget.\n\n## Usage\n\n" + _FENCED_HEADING + "## Fields\n\n`id`\n"
    write_records(adapter, _records(pages=[_gadget_page(body=fenced)]),
                  sha="s1", date="2026-08-07")
    report = write_records(
        adapter, _records(pages=[_gadget_page(body="A gadget.\n\n## Fields\n\n`id`\n")]),
        sha="s2", date="2026-09-24")
    # nothing to carry: the fenced line was example text, not a diagram section
    assert report["diagrams_carried"] == []
    assert "## Diagram" not in adapter.get_entity(_GADGET_ID)["body"]


def test_fenced_diagram_heading_does_not_count_as_keeping_the_section(adapter):
    _seed_gadget_body_diagram(adapter)
    new_body = "A gadget.\n\n## Usage\n\n" + _FENCED_HEADING + "## Fields\n\n`id`\n"
    report = write_records(adapter, _records(pages=[_gadget_page(body=new_body)]),
                           sha="s2", date="2026-09-24")
    body = adapter.get_entity(_GADGET_ID)["body"]
    assert report["diagrams_carried"] == [
        {"file": "data-models/Gadget.md", "diagram_sha": None, "source": "body"}]
    assert body == "A gadget.\n\n" + _BODY_SECTION + "## Usage\n\n" + _FENCED_HEADING + (
        "## Fields\n\n`id`\n")
