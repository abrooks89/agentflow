"""Epic 705 scope 4 / success criterion 4: tier crossing as an explicit API
contract — both directions, outside the depth budget, with the undocumented
case explicit (`documented_by: []`), and one-tier filters."""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import pytest

from autodoc_query.api import global_search, local_search
from autodoc_query.cli import main
from autodoc_query.tuning import MAX_TIER_REFS_PER_ITEM
from tests.query_helpers import (
    HUB_CONCEPT,
    HUB_SEED,
    A,
    B,
    FakeEmbedder,
    K,
    build_hub_graph,
    build_query_graph,
    concept_row,
    unit_vec,
)


@pytest.fixture
def graph(adapter):
    build_query_graph(adapter)
    return adapter


def _by_id(res):
    return {i.entity_id: i for i in res.items}


def test_symbol_question_surfaces_documenting_concept(graph):
    # criterion 4, direction 1: a symbol-level question returns >= 1
    # tier='concept' item. k=1/depth=0 pins the seed set to the symbol alone,
    # proving the documents join runs OUTSIDE the depth budget.
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)),
                       k=1, depth=0)
    items = _by_id(res)
    assert items[K].tier == "concept"
    # the concept was reached through the documents join, outside the budget
    assert items[K].path[-1].rel == "documents"
    # and the symbol side names the concept documenting it
    refs = items[A].documented_by
    assert [r.entity_id for r in refs] == [K]


def test_concept_question_lands_on_symbols_with_spans(graph):
    # criterion 4, direction 2: a concept-level question returns >= 1
    # tier='symbol' item with a non-null span (exact file:line citation).
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(1)))
    items = _by_id(res)
    assert items[K].tier == "concept"     # the seed
    symbol_items = [i for i in res.items if i.tier == "symbol"]
    assert symbol_items
    assert any(i.span is not None for i in symbol_items)
    # the concept item lists the symbols it documents
    assert A in [r.entity_id for r in items[K].documents]


def test_undocumented_symbol_reports_empty_documented_by(graph):
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)))
    items = _by_id(res)
    assert items[B].documented_by == []   # a signal, not an omission
    assert items[B].documents is None     # inapplicable field stays None


def test_symbols_only_filter(graph):
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)),
                       expand_tiers="symbols_only")
    assert res.items
    assert all(i.tier == "symbol" for i in res.items)
    # the annotation contract still holds on the one tier returned
    assert _by_id(res)[A].documented_by is not None


def test_concepts_only_filter(graph):
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(1)),
                       expand_tiers="concepts_only")
    assert res.items
    assert all(i.tier == "concept" for i in res.items)


def test_cross_tier_items_dedupe_against_traversal(graph):
    # K is reachable BOTH by graph traversal from A (documents is an edge)
    # and by the tier-crossing pass; it must appear exactly once.
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)))
    ids = [i.entity_id for i in res.items]
    assert ids.count(K) == 1


# ---------------------------------------------------------------- hub guard
# The tier-ref cap: a concept page documenting dozens of symbols must not
# repeat every ref on every item. Measured on build_hub_graph, which
# saturates the cap on every retained item (the per-item worst case).

def _cli_json(db_path, question, *flags):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["--db", str(db_path), "--corpus", "corp", question, *flags],
                    embedder=FakeEmbedder(unit_vec(0)))
    assert code == 0
    return buf.getvalue()


@pytest.fixture
def hub(adapter):
    build_hub_graph(adapter)
    return adapter


def test_hub_item_refs_are_capped_with_total_and_reason(hub):
    # hub-00 documents 40 symbols: the list is cut to the cap,
    # the pre-cap total survives, and the cut is named on the item AND in
    # the result-level truncation reasons (loud, never a silent trim)
    res = local_search("q", hub, embedder=FakeEmbedder(unit_vec(0)))
    concept = _by_id(res)[HUB_CONCEPT]
    assert len(concept.documents) == MAX_TIER_REFS_PER_ITEM
    assert concept.tier_refs_total == 40
    assert concept.tier_refs_truncation == (
        f"tier_ref_cap({MAX_TIER_REFS_PER_ITEM}) kept "
        f"{MAX_TIER_REFS_PER_ITEM} of 40 documents refs")
    # survivors: equal confidence here, so entity-id order decides
    ids = [r.entity_id for r in concept.documents]
    assert ids == sorted(ids)
    seed = _by_id(res)[HUB_SEED]
    assert len(seed.documented_by) == MAX_TIER_REFS_PER_ITEM
    assert seed.tier_refs_total == 20
    assert "documented_by" in seed.tier_refs_truncation
    assert res.truncated
    assert any(r.startswith(f"tier_ref_cap({MAX_TIER_REFS_PER_ITEM})")
               for r in res.truncation_reasons)


def test_cap_prefers_stronger_edges(adapter):
    # a weaker edge sorts behind stronger ones even when its id sorts first
    build_hub_graph(adapter)
    weak = "con:corp:services:aaa-weak"
    adapter.upsert_entities([concept_row(weak, "Weak Notes")])
    adapter.upsert_edges([{"src_id": weak, "dst_id": HUB_SEED, "rel": "documents",
                           "origin": "agent", "confidence": 0.2,
                           "evidence": {"source": "agent-return"}}])
    res = local_search("q", adapter, embedder=FakeEmbedder(unit_vec(0)))
    seed = _by_id(res)[HUB_SEED]
    assert weak not in [r.entity_id for r in seed.documented_by]
    assert seed.tier_refs_total == 21


def test_non_hub_item_is_unchanged(graph):
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)))
    items = _by_id(res)
    assert [r.entity_id for r in items[A].documented_by] == [K]
    assert items[A].tier_refs_total == 1
    assert items[A].tier_refs_truncation is None
    assert items[B].tier_refs_total == 0
    assert not any(r.startswith("tier_ref_cap") for r in res.truncation_reasons)


# The graph-query SKILL.md Token Budget table, parsed from the doc so this
# test enforces what the doc states: {max_entities: tokens at ~4 chars/token},
# measured as the CLI's JSON stdout on build_hub_graph.
SKILL_MD = Path(__file__).resolve().parents[4] / "graph-query" / "SKILL.md"
_TABLE_ROW = re.compile(r"`--max-entities (\d+)`.*~([\d,]+) tokens \|\s*$")


def _skill_table_tokens() -> dict[int, int]:
    rows = {}
    for line in SKILL_MD.read_text(encoding="utf-8").splitlines():
        if m := _TABLE_ROW.search(line):
            rows[int(m.group(1))] = int(m.group(2).replace(",", ""))
    assert sorted(rows) == [10, 25, 50], f"Token Budget table not parsed: {rows}"
    return rows


SKILL_TABLE_TOKENS = _skill_table_tokens()
# 10% headroom: absorbs question length, timing digits and a small field
# addition; the uncapped output measured 1.4-2.6x these numbers, so a lost
# cap still fails. Growth past it means the documented table is stale.
BUDGET_MARGIN = 1.10


@pytest.mark.parametrize("max_entities", sorted(SKILL_TABLE_TOKENS))
def test_serialized_output_within_documented_budget(hub, db_path, max_entities):
    out = _cli_json(db_path, "how does `s00` work?",
                    "--max-entities", str(max_entities))
    assert len(json.loads(out)["items"]) == max_entities
    bound = SKILL_TABLE_TOKENS[max_entities] * 4 * BUDGET_MARGIN
    assert len(out) <= bound, (len(out), bound)


class _AllRelevant:
    def score_reports(self, question, reports):
        return [9.0] * len(reports)


def test_global_path_reports_tier_ref_cap(hub):
    # global search shares cross_tiers: a kept report naming a hub concept
    # must surface the same per-item cut and the result-level reason
    hub.upsert_entities([{**hub.get_entity(HUB_CONCEPT), "community_id": 1}])
    hub.upsert_communities([{
        "level": 0, "label": "hub", "member_ids": [HUB_CONCEPT],
        "report": json.dumps({
            "summary": "the hub service",
            "findings": [{"text": "documents the ingest path",
                          "entities": [HUB_CONCEPT]}],
            "summarized_at": "2026-08-07T00:00:00Z",
        }),
    }])
    res = global_search("q", hub, scorer=_AllRelevant())
    concept = _by_id(res)[HUB_CONCEPT]
    assert len(concept.documents) == MAX_TIER_REFS_PER_ITEM
    assert concept.tier_refs_truncation is not None
    assert any(r.startswith(f"tier_ref_cap({MAX_TIER_REFS_PER_ITEM})")
               for r in res.truncation_reasons)
