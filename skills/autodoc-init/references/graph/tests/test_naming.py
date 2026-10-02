"""Optional LLM label-naming pass (epic 703 scope 3).

The pass renames labels ONLY — it may never change membership — and runs as a
second, separately-committed pass over already-labelled rows: deterministic
labels are durable BEFORE it starts, so a naming failure exits non-zero with
every community still labelled.
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import edge_row, entity_row

import autodoc_graph.naming as naming_mod
from autodoc_graph.cli import main
from autodoc_graph.communities import run_communities
from autodoc_graph.naming import (
    NAMING_MODEL_ENV,
    NamingError,
    OllamaNamer,
    rename_labels,
    resolve_naming_model,
)
from autodoc_graph.sqlite_backend import SqliteGraphAdapter


def _seed_and_run(adapter):
    """Two labelled communities in the store."""
    core = [entity_row(
        f"sym:corp:core/c{i}.py:function:core_fn{i}",
        name=f"core_fn{i}", source_paths=[f"core/c{i}.py"],
        span={"file": f"core/c{i}.py", "start_line": 1, "end_line": 2},
    ) for i in range(4)]
    web = [entity_row(
        f"sym:corp:web/w{i}.py:function:web_fn{i}",
        name=f"web_fn{i}", source_paths=[f"web/w{i}.py"],
        span={"file": f"web/w{i}.py", "start_line": 1, "end_line": 2},
    ) for i in range(4)]
    adapter.upsert_entities(core + web)
    edges = []
    for members in (core, web):
        ids = [e["id"] for e in members]
        for a in ids:
            for b in ids:
                if a < b:
                    edges.append(edge_row(a, b, "calls"))
    adapter.upsert_edges(edges)
    return run_communities(adapter, band=(1, 50))


# ---------------------------------------------------------------- resolution
def test_model_resolution_explicit_beats_env(monkeypatch):
    monkeypatch.setenv(NAMING_MODEL_ENV, "env-model")
    assert resolve_naming_model("cli-model") == "cli-model"
    assert resolve_naming_model(None) == "env-model"


def test_model_resolution_without_any_source_is_loud(monkeypatch):
    monkeypatch.delenv(NAMING_MODEL_ENV, raising=False)
    with pytest.raises(NamingError, match=NAMING_MODEL_ENV):
        resolve_naming_model(None)


def test_url_resolution_follows_the_embedder_convention(monkeypatch):
    monkeypatch.setenv("AUTODOC_OLLAMA_URL", "http://example.test:11434")
    namer = OllamaNamer(model="m", transport=lambda url, payload=None: {})
    assert namer.url == "http://example.test:11434"


# ---------------------------------------------------------------- renaming
def _fake_transport(reply: str):
    def transport(url, payload=None):
        assert payload is not None and "prompt" in payload
        return {"model": payload["model"], "response": reply}
    return transport


def test_rename_changes_labels_only_and_never_membership(adapter):
    _seed_and_run(adapter)
    before = {r["id"]: r for r in adapter.get_communities(0)}
    namer = OllamaNamer(model="m", transport=_fake_transport("Shiny Name"))
    result = rename_labels(adapter, namer)
    assert result["renamed"] >= 2
    after = {r["id"]: r for r in adapter.get_communities(0)}
    assert set(before) == set(after)
    for cid, row in after.items():
        assert row["label"].startswith("Shiny Name")
        assert row["member_ids"] == before[cid]["member_ids"]  # membership untouched
        assert row["descriptor"] == before[cid]["descriptor"]
        assert row["parent_id"] == before[cid]["parent_id"]


def test_duplicate_llm_labels_are_deduped_per_level(adapter):
    """Review finding 3: a model answering the same name for every community
    must not collapse two communities into one display label."""
    _seed_and_run(adapter)
    namer = OllamaNamer(model="m", transport=_fake_transport("Shiny Name"))
    rename_labels(adapter, namer)
    labels = sorted(r["label"] for r in adapter.get_communities(0))
    assert labels == ["Shiny Name", "Shiny Name-2"]


def test_rename_preserves_704_report(adapter):
    """Review blocker 2 companion: the naming pass sends label-only updates,
    so an existing community report survives the rename."""
    _seed_and_run(adapter)
    row = adapter.get_communities(0)[0]
    adapter.upsert_communities([{
        "id": row["id"], "level": 0,
        "report": "704 summary", "summarized_at_sha": "sha704",
    }])
    namer = OllamaNamer(model="m", transport=_fake_transport("New Label"))
    rename_labels(adapter, namer)
    fresh = next(r for r in adapter.get_communities(0) if r["id"] == row["id"])
    assert fresh["report"] == "704 summary"
    assert fresh["summarized_at_sha"] == "sha704"
    assert fresh["label"].startswith("New Label")


def test_hostile_llm_reply_is_sanitized(adapter):
    _seed_and_run(adapter)
    namer = OllamaNamer(
        model="m",
        transport=_fake_transport("Core <script>alert(1)</script> {{Layer}}"),
    )
    rename_labels(adapter, namer)
    for r in adapter.get_communities(0):
        assert re.fullmatch(r"[A-Za-z0-9 _.\-]+", r["label"]), r["label"]
        assert "<" not in r["label"] and "{" not in r["label"]


def test_entirely_unsafe_llm_reply_is_a_loud_failure(adapter):
    _seed_and_run(adapter)
    namer = OllamaNamer(model="m", transport=_fake_transport("<<<>>>{}"))
    with pytest.raises(NamingError, match=r"unsafe|empty"):
        rename_labels(adapter, namer)


def test_naming_failure_raises_but_deterministic_labels_are_durable(adapter):
    _seed_and_run(adapter)
    before = {r["id"]: r["label"] for r in adapter.get_communities(0)}
    assert all(before.values())

    def broken(url, payload=None):
        raise OSError("connection refused")

    namer = OllamaNamer(model="m", transport=broken)
    with pytest.raises(NamingError):
        rename_labels(adapter, namer)
    after = {r["id"]: r["label"] for r in adapter.get_communities(0)}
    assert after == before  # every community still labelled


def test_empty_llm_reply_is_a_loud_failure(adapter):
    _seed_and_run(adapter)
    namer = OllamaNamer(model="m", transport=_fake_transport("   "))
    with pytest.raises(NamingError, match="empty"):
        rename_labels(adapter, namer)


# ---------------------------------------------------------------- CLI wiring
def _built_db(tmp_path):
    db = tmp_path / "g.db"
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        _seed_and_run(a)
    finally:
        a.close()
    return db


def test_cli_llm_labels_without_model_fails_actionably(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(NAMING_MODEL_ENV, raising=False)
    db = _built_db(tmp_path)
    rc = main(["communities", "--db", str(db), "--corpus", "corp",
               "--band", "1:50", "--llm-labels"])
    assert rc == 1
    assert NAMING_MODEL_ENV in capsys.readouterr().err


def test_cli_llm_labels_failure_is_nonzero_with_durable_labels(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AUTODOC_OLLAMA_URL", "http://127.0.0.1:9")
    db = _built_db(tmp_path)
    out = tmp_path / "c.json"
    rc = main(["communities", "--db", str(db), "--corpus", "corp",
               "--band", "1:50", "--llm-labels", "--llm-model", "m",
               "--emit", str(out)])
    assert rc == 1
    assert "127.0.0.1:9" in capsys.readouterr().err
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        assert all(r["label"] for r in a.get_communities(0))  # durable
    finally:
        a.close()


def _built_descent_db(tmp_path):
    """A corpus whose packing DESCENDS: 8 pair-linked cluster pairs at level
    1, one pair oversized (20+20 symbols vs ideal ~8.5) so its level-0
    children are packed instead of the level-1 parent."""
    db = tmp_path / "descent.db"
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        for p in range(8):
            size = 20 if p == 0 else 2
            reps = []
            for side in ("a", "b"):
                f = f"pair{p}/{side}/mod.py"
                rows = [entity_row(
                    f"sym:corp:{f}:function:p{p}{side}_fn{i}",
                    name=f"p{p}{side}_fn{i}", source_paths=[f],
                    span={"file": f, "start_line": i + 1, "end_line": i + 2},
                ) for i in range(size)]
                a.upsert_entities(rows)
                ids = [r["id"] for r in rows]
                a.upsert_edges([
                    edge_row(ids[i], ids[i + 1], "calls")
                    for i in range(len(ids) - 1)
                ])
                reps.append(ids[0])
            a.upsert_edges([edge_row(reps[0], reps[1], "imports")])
    finally:
        a.close()
    return db


def test_cli_llm_labels_refreshes_descended_unit_labels(tmp_path, capsys, monkeypatch):
    """Re-review finding 1: post-descent units contain communities BELOW the
    packing level; the artifact refresh after --llm-labels must cover every
    level, or descended communities ship stale labels while the store is
    already renamed."""
    monkeypatch.setattr(
        naming_mod, "_urllib_transport", _fake_transport("Renamed"),
    )
    db = _built_descent_db(tmp_path)
    out = tmp_path / "c.json"
    rc = main(["communities", "--db", str(db), "--corpus", "corp",
               "--llm-labels", "--llm-model", "m", "--emit", str(out)])
    assert rc == 0, capsys.readouterr().err
    artifact = json.loads(out.read_text(encoding="utf-8"))

    store = {}
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        level = 0
        while True:
            rows = a.get_communities(level)
            if not rows:
                break
            store.update({r["id"]: r["label"] for r in rows})
            level += 1
    finally:
        a.close()

    packed_ids = [c["id"] for u in artifact["units"] for c in u["communities"]]
    # the descent shape actually happened: more packables than the top level
    assert len(packed_ids) > len(artifact["levels"][-1]["ids"])
    for u in artifact["units"]:
        for c in u["communities"]:
            assert c["label"] == store[c["id"]], (
                f"artifact label {c['label']!r} stale vs store "
                f"{store[c['id']]!r} for community {c['id']}"
            )


def test_cli_llm_labels_success_renames_and_updates_artifact(tmp_path, capsys, monkeypatch):
    """In-process fake: patch the namer's transport factory via env-free DI."""
    monkeypatch.setattr(
        naming_mod, "_urllib_transport",
        _fake_transport("Renamed Community"),
    )
    db = _built_db(tmp_path)
    out = tmp_path / "c.json"
    rc = main(["communities", "--db", str(db), "--corpus", "corp",
               "--band", "1:50", "--llm-labels", "--llm-model", "m",
               "--emit", str(out)])
    assert rc == 0, capsys.readouterr().err
    artifact = json.loads(out.read_text(encoding="utf-8"))
    art_labels = sorted(
        c["label"] for u in artifact["units"] for c in u["communities"]
    )
    assert all(lab.startswith("Renamed Community") for lab in art_labels)
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        labels = sorted(r["label"] for r in a.get_communities(0))
        # deduped per level — no two communities collapse onto one label
        assert labels == ["Renamed Community", "Renamed Community-2"]
        # membership untouched by the pass
        assert all(r["member_ids"] for r in a.get_communities(0))
    finally:
        a.close()
