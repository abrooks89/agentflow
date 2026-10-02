"""Store-mode integrity checks (epic 704 scope 5): each markdown-era audit
dimension becomes a query over the store."""

from __future__ import annotations

from conftest import entity_row

from autodoc_graph.records import write_records
from autodoc_graph.store_check import check_store


def _seed_concepts(adapter):
    adapter.upsert_entities([
        entity_row("sym:corp:app/x.py:function:handler",
                   kind="function", name="handler"),
        entity_row("sym:corp:app/x.py:class:Widget", kind="class", name="Widget"),
    ])
    write_records(adapter, {"pages": [
        {
            "file": "services/Widget Service.md", "type": "service",
            "title": "Widget Service", "source_paths": ["app/x.py"],
            "summary": "Serves widgets.", "body": "Serves widgets.\n",
            "frontmatter": {"service_kind": "api", "path": "app/",
                            "language": "python", "entrypoint": "app/x.py"},
            "documents": [{"file": "app/x.py", "kind": "function", "name": "handler"}],
            "edges": [],
        },
    ]}, sha="s", date="2026-08-07")


def test_clean_store_has_no_blockers(adapter):
    _seed_concepts(adapter)
    report = check_store(adapter)
    assert report["blockers"] == []
    assert report["concepts"] == 1


def test_uncovered_symbols_are_reported(adapter):
    _seed_concepts(adapter)
    report = check_store(adapter)
    # Widget (the class) has no inbound documents edge
    assert report["uncovered_symbols"] == 1
    assert any(w["kind"] == "uncoveredSymbol" and "Widget" in w["detail"]
               for w in report["warnings"])


def test_orphan_concept_is_flagged(adapter):
    _seed_concepts(adapter)
    write_records(adapter, {"pages": [{
        "file": "processes/Lonely.md", "type": "process", "title": "Lonely",
        "source_paths": ["app/x.py"], "summary": "s.", "body": "b.\n",
        "frontmatter": {"trigger": "manual"}, "documents": [], "edges": [],
    }]}, sha="s", date="2026-08-07")
    report = check_store(adapter)
    assert any(w["kind"] == "orphanConcept" and "Lonely" in w["detail"]
               for w in report["warnings"])


def test_duplicate_qualified_name_is_a_blocker(adapter):
    """Adversarial finding 2: two concepts rendering to one path would
    last-writer-win — the store check catches rot that slipped past the
    writer (e.g. rows written around it)."""
    _seed_concepts(adapter)
    adapter.upsert_entities([{
        "id": "con:corp:process:widget-service", "tier": "concept",
        "kind": "process", "name": "Widget Service (process)",
        "qualified_name": "services/Widget Service.md",  # collides
        "source_paths": ["app/x.py"], "span": None, "summary": "s",
        "body": "b", "frontmatter": {"_created": "2026-08-07", "trigger": "manual"},
        "origin": "agent", "last_seen_sha": "s",
    }])
    report = check_store(adapter)
    dupes = [b for b in report["blockers"] if b["kind"] == "duplicateFile"]
    assert dupes and "services/widget service.md" in dupes[0]["detail"]


def test_contract_rot_is_a_blocker(adapter):
    """A concept written around the records writer (missing required
    frontmatter, empty body) is caught by the store check."""
    _seed_concepts(adapter)
    adapter.upsert_entities([{
        "id": "con:corp:job:rogue", "tier": "concept", "kind": "job",
        "name": "Rogue", "qualified_name": "jobs/Rogue.md",
        "source_paths": ["app/x.py"], "span": None, "summary": None,
        "body": "", "frontmatter": {}, "origin": "agent", "last_seen_sha": "s",
    }])
    report = check_store(adapter)
    kinds = {b["kind"] for b in report["blockers"]}
    assert "emptyBody" in kinds
    assert "missingCreated" in kinds
    assert "missingFrontmatter" in kinds
