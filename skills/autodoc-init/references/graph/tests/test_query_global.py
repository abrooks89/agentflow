"""Epic 705 scope 3: global search — map → filter → synthesize over the LIVE
community hierarchy. Anchored on entity.community_id + parent chains (never
raw get_communities), deterministic batches, level descent, honest
which-communities-kept reporting, and loud zero-report failure."""

from __future__ import annotations

import json
import time

import pytest

from autodoc_query.api import global_search
from autodoc_query.types import QueryError
from tests.conftest import entity_row


def _report(summary: str, entities: list[str]) -> str:
    return json.dumps({
        "summary": summary,
        "findings": [{"text": f"finding about {summary}", "entities": entities}],
        "summarized_at": "2026-08-07T00:00:00Z",
    })


def test_finding_entities_by_name_resolve_within_the_community(adapter):
    # 704's real records cite finding entities by NAME, not id (live-smoke
    # finding). Names resolve preferring the kept community's own members —
    # here two symbols share the name 'helper' in different communities.
    h1 = "sym:corp:app/a/helper.py:function:helper"
    h2 = "sym:corp:app/b/helper.py:function:helper"
    adapter.upsert_entities([
        entity_row(h1, name="helper", community_id=1,
                   span={"file": "app/a/helper.py", "start_line": 1, "end_line": 5}),
        entity_row(h2, name="helper", community_id=2,
                   span={"file": "app/b/helper.py", "start_line": 1, "end_line": 5}),
    ])
    adapter.upsert_communities([
        {"level": 0, "label": "a", "member_ids": [h1],
         "report": _report("side a", ["helper"])},
        {"level": 0, "label": "b", "member_ids": [h2]},
    ])
    res = global_search("q", adapter, scorer=StubScorer({1: 9.0}))
    assert [i.entity_id for i in res.items] == [h1]
    assert res.items[0].community["community_id"] == 1


def test_unresolvable_finding_entities_are_skipped_not_fatal(adapter):
    adapter.upsert_entities([
        entity_row(S1, name="ingest_records", community_id=1),
    ])
    adapter.upsert_communities([
        {"level": 0, "label": "a", "member_ids": [S1],
         "report": _report("side a", ["no_such_symbol_anywhere", S1])},
    ])
    res = global_search("q", adapter, scorer=StubScorer({1: 9.0}))
    assert [i.entity_id for i in res.items] == [S1]
    # ...and the skip is RECORDED, not silent (adversarial finding 9)
    assert res.reports[0]["unresolved_entities"] == ["no_such_symbol_anywhere"]


def test_ambiguous_corpus_wide_name_is_skipped_and_recorded(adapter):
    # adversarial finding 9: the corpus-wide fallback resolves ONLY a unique
    # name; two out-of-community candidates are ambiguity, skipped + recorded
    h1 = "sym:corp:app/a/dup.py:function:dup_fn"
    h2 = "sym:corp:app/b/dup.py:function:dup_fn"
    adapter.upsert_entities([
        entity_row(S1, name="ingest_records", community_id=1),
        entity_row(h1, name="dup_fn", community_id=2),
        entity_row(h2, name="dup_fn", community_id=3),
    ])
    adapter.upsert_communities([
        {"level": 0, "label": "a", "member_ids": [S1],
         "report": _report("side a", ["dup_fn", S1])},
        {"level": 0, "label": "b", "member_ids": [h1]},
        {"level": 0, "label": "c", "member_ids": [h2]},
    ])
    res = global_search("q", adapter, scorer=StubScorer({1: 9.0}))
    assert [i.entity_id for i in res.items] == [S1]
    assert res.reports[0]["unresolved_entities"] == ["dup_fn"]


def test_unique_corpus_wide_name_still_resolves(adapter):
    only = "sym:corp:app/only.py:function:one_of_a_kind"
    adapter.upsert_entities([
        entity_row(S1, name="ingest_records", community_id=1),
        entity_row(only, name="one_of_a_kind", community_id=2),
    ])
    adapter.upsert_communities([
        {"level": 0, "label": "a", "member_ids": [S1],
         "report": _report("side a", ["one_of_a_kind"])},
        {"level": 0, "label": "b", "member_ids": [only]},
    ])
    res = global_search("q", adapter, scorer=StubScorer({1: 9.0}))
    assert [i.entity_id for i in res.items] == [only]
    assert res.reports[0]["unresolved_entities"] == []


S1 = "sym:corp:app/ingest.py:function:ingest_records"
S2 = "sym:corp:app/ingest.py:function:validate_rows"
S3 = "sym:corp:app/render.py:function:render_pages"
S4 = "sym:corp:app/render.py:function:write_index"


@pytest.fixture
def community_graph(adapter):
    """Two live level-0 communities (ids 1, 2) under one live level-1 parent
    (id 4); a DISSOLVED row (id 3) and an unanchored historical parent (id 5)
    both carrying reports that must never be scored."""
    adapter.upsert_entities([
        entity_row(S1, name="ingest_records", community_id=1),
        entity_row(S2, name="validate_rows", community_id=1),
        entity_row(S3, name="render_pages", community_id=2),
        entity_row(S4, name="write_index", community_id=2),
    ])
    adapter.upsert_communities([
        {"level": 0, "label": "ingest", "member_ids": [S1, S2],
         "report": _report("ingest side", [S1, S2])},
        {"level": 0, "label": "render", "member_ids": [S3, S4],
         "report": _report("render side", [S3, S4])},
        {"level": 0, "label": "ghost", "member_ids": [],
         "report": _report("dissolved ghost", [S1]),
         "descriptor": {"dissolved": True}},
        {"level": 1, "label": "pipeline", "member_ids": [S1, S2, S3, S4],
         "report": _report("whole pipeline", [S1, S3])},
        {"level": 1, "label": "stale-parent", "member_ids": [],
         "report": _report("stale parent", [S2])},
    ])
    # wire the live parent chain: rows 1, 2 -> parent 4 (partial update)
    adapter.upsert_communities([
        {"id": 1, "level": 0, "parent_id": 4},
        {"id": 2, "level": 0, "parent_id": 4},
    ])
    return adapter


class StubScorer:
    """Deterministic scorer: scores by community id; records every call."""

    def __init__(self, scores: dict[int, float]):
        self.scores = scores
        self.calls: list[list[int]] = []

    def score_reports(self, question, reports):
        self.calls.append([cid for cid, _ in reports])
        return [self.scores.get(cid, 0.0) for cid, _ in reports]

    def scored_ids(self) -> set[int]:
        return {cid for call in self.calls for cid in call}


# ---------------------------------------------------------------- level descent
def test_starts_at_highest_level_and_keeps(community_graph):
    scorer = StubScorer({4: 9.0})
    res = global_search("how does the pipeline fit together?", community_graph,
                        scorer=scorer)
    kept = res.communities_kept
    assert [c["community_id"] for c in kept] == [4]
    assert kept[0]["score"] == 9.0 and kept[0]["level"] == 1
    # level 0 was never scored — the kept set at level 1 was non-empty
    assert scorer.scored_ids() == {4}
    # synthesis resolved the kept report's finding entities with provenance
    ids = [i.entity_id for i in res.items]
    assert S1 in ids and S3 in ids
    assert res.reports and res.reports[0]["summary"] == "whole pipeline"


def test_descends_only_when_kept_set_is_empty(community_graph):
    scorer = StubScorer({4: 1.0, 1: 8.0, 2: 3.0})
    res = global_search("q", community_graph, scorer=scorer)
    assert scorer.scored_ids() == {4, 1, 2}
    kept = res.communities_kept
    assert [c["community_id"] for c in kept] == [1]   # 2 is under the floor
    # honest reporting: everything scored is visible with its score
    scored = {c["community_id"]: c["score"] for c in res.communities_scored}
    assert scored == {4: 1.0, 1: 8.0, 2: 3.0}


def test_dissolved_and_unanchored_rows_are_never_scored(community_graph):
    scorer = StubScorer({})
    global_search("q", community_graph, scorer=scorer)
    assert 3 not in scorer.scored_ids()   # dissolved
    assert 5 not in scorer.scored_ids()   # historical, not in any parent chain


def test_nothing_kept_anywhere_returns_empty_with_evidence(community_graph):
    scorer = StubScorer({})
    res = global_search("q", community_graph, scorer=scorer)
    assert res.items == []
    assert res.communities_kept == []
    assert len(res.communities_scored) == 3   # 4, then 1 and 2
    assert not res.truncated


# ---------------------------------------------------------------- determinism
def test_batches_are_deterministic_and_id_sorted(adapter):
    ids = []
    rows = []
    for i in range(5):
        sid = f"sym:corp:app/m{i}.py:function:f{i}"
        ids.append(sid)
        rows.append(entity_row(sid, name=f"f{i}", community_id=i + 1))
    adapter.upsert_entities(rows)
    adapter.upsert_communities([
        {"level": 0, "label": f"c{i}", "member_ids": [ids[i]],
         "report": _report(f"community {i}", [ids[i]])}
        for i in range(5)
    ])
    scorer = StubScorer({})
    global_search("q", adapter, scorer=scorer, batch_size=2)
    assert sorted(scorer.calls) == [[1, 2], [3, 4], [5]]
    scorer2 = StubScorer({})
    global_search("q", adapter, scorer=scorer2, batch_size=2)
    assert sorted(scorer2.calls) == sorted(scorer.calls)


# ---------------------------------------------------------------- provenance
def test_global_items_carry_community_provenance(community_graph):
    scorer = StubScorer({4: 9.0})
    res = global_search("q", community_graph, scorer=scorer)
    for item in res.items:
        assert item.community is not None
        assert item.community["community_id"] == 4
        assert item.path == []
        assert item.origin in ("ast", "agent")
        assert 0 < item.min_confidence <= 1.0
    # tier annotation applies to global results too
    sym = next(i for i in res.items if i.tier == "symbol")
    assert sym.documented_by == []


# ---------------------------------------------------------------- loud failures
def test_no_communities_is_loud(adapter):
    adapter.upsert_entities([entity_row(S1, name="ingest_records")])
    with pytest.raises(QueryError, match="communit"):
        global_search("q", adapter, scorer=StubScorer({}))


def test_zero_reports_is_loud(adapter):
    adapter.upsert_entities([entity_row(S1, name="ingest_records", community_id=1)])
    adapter.upsert_communities([{"level": 0, "label": "bare", "member_ids": [S1]}])
    with pytest.raises(QueryError, match="report"):
        global_search("q", adapter, scorer=StubScorer({}))


def test_empty_corpus_is_loud(adapter):
    with pytest.raises(QueryError, match="empty"):
        global_search("q", adapter, scorer=StubScorer({}))


def test_malformed_report_json_names_the_community(community_graph):
    community_graph.upsert_communities([
        {"id": 4, "level": 1, "report": "not json {{"},
    ])
    with pytest.raises(QueryError, match="community 4"):
        global_search("q", community_graph, scorer=StubScorer({4: 9.0}))


# ---------------------------------------------------------------- budget
def test_wall_clock_budget_truncates_loudly(community_graph):
    scorer = StubScorer({4: 9.0})
    res = global_search("q", community_graph, scorer=scorer, budget_ms=0)
    assert res.truncated is True
    assert any("wall_clock" in r for r in res.truncation_reasons)


class SlowScorer(StubScorer):
    def __init__(self, scores, delay_s):
        super().__init__(scores)
        self.delay_s = delay_s

    def score_reports(self, question, reports):
        time.sleep(self.delay_s)
        return super().score_reports(question, reports)


def test_budget_is_enforced_between_scoring_waves(adapter):
    # adversarial finding 1: enqueueing every batch up front made the budget
    # dead code during the map step — a slow scorer ran arbitrarily past it
    # with truncated=False. Waves must re-check the clock as they complete
    # and stop with the unscored batch count named, keeping partial scores.
    ids, rows = [], []
    for i in range(6):
        sid = f"sym:corp:app/m{i}.py:function:f{i}"
        ids.append(sid)
        rows.append(entity_row(sid, name=f"f{i}", community_id=i + 1))
    adapter.upsert_entities(rows)
    adapter.upsert_communities([
        {"level": 0, "label": f"c{i}", "member_ids": [ids[i]],
         "report": _report(f"community {i}", [ids[i]])}
        for i in range(6)
    ])
    scorer = SlowScorer({i: 9.0 for i in range(1, 7)}, delay_s=0.05)
    res = global_search("q", adapter, scorer=scorer, batch_size=1,
                        score_parallelism=1, budget_ms=60)
    assert res.truncated is True
    assert any("wall_clock" in r and "unscored" in r
               for r in res.truncation_reasons)
    # partial map: some batches scored, some loudly left
    assert 0 < len(res.communities_scored) < 6
    # filter still ran over the partial scores — kept set is non-empty
    assert res.communities_kept
