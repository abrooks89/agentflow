"""Epic 705 scope 1: the deterministic query router.

Rule order (spec-fixed): explicit override > entity resolution > breadth
markers > default-local-with-SEED_FLOOR-fallback. Every decision is a
RoutingDecision{mode, rule, evidence, reroute_from}; no LLM round-trip in
the default path — only the fallback spends an embedding.
"""

from __future__ import annotations

import pytest

from autodoc_query.router import route
from autodoc_query.tuning import SEED_FLOOR
from autodoc_query.types import QueryError, RoutingDecision
from tests.conftest import entity_row


def _unit_vec(axis: int) -> list[float]:
    vec = [0.0] * 768
    vec[axis] = 1.0
    return vec


@pytest.fixture
def corpus(adapter):
    """A tiny corpus: two named symbols, one embedded on axis 0."""
    adapter.upsert_entities([
        entity_row("sym:corp:app/x.py:function:write_records", name="write_records"),
        entity_row("sym:corp:app/x.py:class:SqliteGraphAdapter",
                   name="SqliteGraphAdapter", kind="class"),
        entity_row("sym:corp:app/x.py:function:write_records_batch",
                   name="write_records_batch"),
    ])
    adapter.upsert_embeddings([{
        "entity_id": "sym:corp:app/x.py:function:write_records",
        "vec": _unit_vec(0), "model": "nomic-embed-text",
        "embedded_at_sha": "s1", "entity_version": "v1",
    }])
    return adapter


# ---------------------------------------------------------------- overrides
def test_explicit_local_override_wins_on_breadth_question(corpus):
    decision, _, _ = route("overview of the architecture", corpus, mode="local")
    assert decision.mode == "local"
    assert decision.rule == "override"
    assert decision.reroute_from is None


def test_explicit_global_override_on_entity_question_is_honored(corpus):
    # criterion 3: an explicit mode='global' on an entity-shaped question is
    # honored, never silently re-routed.
    decision, _, _ = route("what does `write_records` do?", corpus, mode="global")
    assert decision.mode == "global"
    assert decision.rule == "override"


def test_invalid_mode_is_a_loud_error(corpus):
    with pytest.raises(QueryError, match="mode"):
        route("anything", corpus, mode="both")


# ---------------------------------------------------------------- entity resolution
def test_snake_case_token_resolves_to_local(corpus):
    decision, _, _ = route("what does write_records do?", corpus)
    assert decision.mode == "local"
    assert decision.rule == "entity_resolution"
    assert "sym:corp:app/x.py:function:write_records" in decision.evidence["entity_ids"]


def test_camelcase_token_resolves_to_local(corpus):
    decision, _, _ = route("how is SqliteGraphAdapter opened?", corpus)
    assert decision.mode == "local"
    assert decision.rule == "entity_resolution"


def test_backticked_span_resolves_exact_then_prefix(corpus):
    # `write_records_ba` matches no exact name; prefix matching finds
    # write_records_batch.
    decision, _, _ = route("explain `write_records_ba`", corpus)
    assert decision.mode == "local"
    ids = decision.evidence["entity_ids"]
    assert "sym:corp:app/x.py:function:write_records_batch" in ids


def test_qualified_name_token_resolves_to_local(corpus):
    # adversarial finding 5: the spec resolves on name AND qualified_name;
    # a path:name token matches no bare name but hits qualified_name exactly
    corpus.upsert_entities([
        entity_row("sym:corp:app/deep/frob.py:function:zzfrob",
                   name="zzfrob", qualified_name="app/deep/frob.py:zzfrob"),
    ])
    decision, _, _ = route("explain `app/deep/frob.py:zzfrob` please", corpus)
    assert decision.mode == "local"
    assert decision.rule == "entity_resolution"
    assert ("sym:corp:app/deep/frob.py:function:zzfrob"
            in decision.evidence["entity_ids"])


def test_entity_resolution_beats_breadth_markers(corpus):
    decision, _, _ = route("overview of write_records across the system", corpus)
    assert decision.rule == "entity_resolution"
    assert decision.mode == "local"


def test_plain_prose_tokens_do_not_resolve(corpus):
    # 'records' is not symbol-shaped; without markers this goes down the
    # default path (which needs an embedder — proving no resolution fired).
    embeds = []

    def embed(text):
        embeds.append(text)
        return _unit_vec(0)

    decision, _, _ = route("how are records written?", corpus, embed=embed)
    assert decision.rule in ("default_local", "seed_floor_reroute")
    assert embeds  # embedding was spent => rules 1-3 did not fire


# ---------------------------------------------------------------- breadth markers
def test_breadth_marker_routes_global(corpus):
    decision, _, _ = route("give me an overview of the pipeline", corpus)
    assert decision.mode == "global"
    assert decision.rule == "breadth_marker"
    assert decision.evidence["marker"] == "overview"


def test_multiword_marker_routes_global(corpus):
    decision, _, _ = route("how does the system fit together?", corpus)
    assert decision.mode == "global"
    assert decision.rule == "breadth_marker"


def test_plural_abstract_head_routes_global(corpus):
    decision, _, _ = route("what are the subsystems here?", corpus)
    assert decision.mode == "global"
    assert decision.rule == "breadth_marker"


def test_multiword_marker_needs_a_word_boundary(corpus):
    # adversarial finding 4: 'how does the systemd ...' must NOT match the
    # 'how does the system' marker via raw substring
    embeds = []

    def embed(text):
        embeds.append(text)
        return _unit_vec(0)

    decision, _, _ = route("how does the systemd unit get restarted?",
                           corpus, embed=embed)
    assert decision.rule != "breadth_marker"
    assert embeds  # it fell through to the default (embedding) path
    # positive control: the exact marker phrase still fires
    control, _, _ = route("how does the system fit together?", corpus)
    assert control.rule == "breadth_marker"


# ---------------------------------------------------------------- default path
def test_default_path_good_seed_stays_local(corpus):
    decision, seeds, qvec = route("something about persisting", corpus,
                                  embed=lambda _t: _unit_vec(0))
    assert decision.mode == "local"
    assert decision.rule == "default_local"
    assert decision.evidence["best_seed_score"] >= SEED_FLOOR
    # the spent embedding is handed back so local search never re-embeds
    assert seeds and qvec is not None


def test_default_path_below_floor_reroutes_to_global(corpus):
    decision, _, _ = route("something about persisting", corpus,
                           embed=lambda _t: _unit_vec(1))  # orthogonal
    assert decision.mode == "global"
    assert decision.rule == "seed_floor_reroute"
    assert decision.reroute_from == "local"
    assert decision.evidence["best_seed_score"] < SEED_FLOOR


def test_default_path_no_seeds_reroutes_to_global(adapter):
    adapter.upsert_entities([entity_row("sym:corp:app/x.py:function:f1")])
    decision, _, _ = route("anything at all", adapter,
                           embed=lambda _t: _unit_vec(0))
    assert decision.mode == "global"
    assert decision.rule == "seed_floor_reroute"
    assert decision.evidence["best_seed_score"] is None


def test_default_path_without_embedder_is_loud(corpus):
    with pytest.raises(QueryError, match="embed"):
        route("no markers no symbols here", corpus, embed=None)


def test_seed_floor_is_configurable(corpus):
    # findings 6+7: the floor threads through route(); evidence names the
    # floor actually used
    embed = lambda _t: [0.8, 0.6] + [0.0] * 766  # noqa: E731 — best seed 0.8
    lo, _, _ = route("something about persisting", corpus, embed=embed,
                     seed_floor=0.5)
    assert lo.rule == "default_local"
    hi, _, _ = route("something about persisting", corpus, embed=embed,
                     seed_floor=0.9)
    assert hi.rule == "seed_floor_reroute"
    assert hi.evidence["seed_floor"] == 0.9


# ---------------------------------------------------------------- determinism
def test_routing_is_deterministic(corpus):
    a = route("what does write_records do?", corpus)[0]
    b = route("what does write_records do?", corpus)[0]
    assert a == b
    assert isinstance(a, RoutingDecision)
