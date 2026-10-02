"""JSONL → adapter ingestion (epic 702).

Consumes the deterministic JSONL the Node extraction side
(``skills/autodoc-init/references/inventory/emit-graph.js``) writes: entity
lines, edge lines, one meta trailer. Validates EVERYTHING before touching
storage (closed rel vocabulary, provenance rules, line-level JSON errors with
line numbers), then:

* entities  -> ``upsert_entities`` stamped with the run sha (idempotent by id)
* ast edges -> ``replace_ast_edges`` (atomic swap; pure function of the code)
* other edges -> ``upsert_edges`` (agent/dreamed edges persist across runs)
* corpus    -> ``update_corpus(last_indexed_sha=sha, ...)``

Storage-agnostic core: sees storage only through the GraphAdapter interface
(enforced by test).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapter import GraphAdapter
from .vocab import validate_edge_provenance

_LINE_TYPES = {"entity", "edge", "meta"}


class IngestError(RuntimeError):
    """Loud ingest failure (malformed JSONL, missing sha, unknown line type)."""


def load_jsonl(path: Path | str) -> dict[str, Any]:
    """Parse + validate a graph JSONL file into ``{entities, edges, meta}``."""
    path = Path(path)
    entities: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    meta: dict[str, Any] | None = None
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise IngestError(f"{path}: line {lineno} is not valid JSON: {e}") from e
            kind = obj.get("type")
            if kind not in _LINE_TYPES:
                raise IngestError(
                    f"{path}: line {lineno} has unknown type {kind!r} "
                    f"(allowed: {sorted(_LINE_TYPES)})"
                )
            if kind == "entity":
                if not obj.get("id"):
                    raise IngestError(f"{path}: line {lineno} entity has no id")
                entities.append(obj)
            elif kind == "edge":
                validate_edge_provenance(
                    obj.get("rel"), obj.get("origin"),
                    obj.get("confidence", -1.0),
                )
                if not obj.get("src_id") or not obj.get("dst_id"):
                    raise IngestError(
                        f"{path}: line {lineno} edge missing src_id/dst_id"
                    )
                edges.append(obj)
            else:
                if meta is not None:
                    raise IngestError(f"{path}: line {lineno} has a second meta line")
                meta = obj
    return {"entities": entities, "edges": edges, "meta": meta}


def ingest_graph(
    adapter: GraphAdapter,
    path: Path | str,
    *,
    sha: str | None = None,
    corpus_id: str | None = None,
    repo_ref: str | None = None,
    branch: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Ingest a JSONL file into an OPEN adapter. Returns a count report.

    ``project`` binds this corpus to the project it documents. It is passed
    straight through to the corpus row, where the renderer reads it; nothing
    here infers it, because a guessed project is worse than an absent one -
    the renderer refuses on absent and files pages wrongly on guessed."""
    data = load_jsonl(path)
    meta = data["meta"] or {}
    run_sha = sha if sha is not None else meta.get("sha")
    if not run_sha:
        raise IngestError(
            f"{path}: no run sha — the JSONL meta line carries none and no "
            "explicit sha= was given. The sha drives incremental runs and "
            "prune candidacy; refusing to ingest without one."
        )
    run_corpus = corpus_id if corpus_id is not None else meta.get("corpus")
    if not run_corpus:
        raise IngestError(
            f"{path}: no corpus id — the JSONL meta line carries none and no "
            "explicit corpus_id= was given"
        )
    # Corpus check BEFORE any write (adversarial-review finding 4): ingesting
    # repo A's JSONL into repo B's open corpus must leave the DB untouched,
    # not blow up only after the entities were already committed.
    if adapter.corpus_id is not None and run_corpus != adapter.corpus_id:
        raise IngestError(
            f"{path}: corpus mismatch — adapter is open on corpus "
            f"{adapter.corpus_id!r} but this JSONL belongs to {run_corpus!r}; "
            "nothing was written"
        )

    entity_rows = [
        {**{k: v for k, v in e.items() if k != "type"}, "last_seen_sha": run_sha}
        for e in data["entities"]
    ]
    entity_counts = adapter.upsert_entities(entity_rows)

    ast_rows = []
    other_rows = []
    for e in data["edges"]:
        row = {**{k: v for k, v in e.items() if k != "type"}, "last_seen_sha": run_sha}
        (ast_rows if e.get("origin") == "ast" else other_rows).append(row)
    edge_counts = adapter.replace_ast_edges(run_corpus, ast_rows)
    other_counts = adapter.upsert_edges(other_rows) if other_rows else None

    adapter.update_corpus(
        repo_ref=repo_ref, branch=branch, last_indexed_sha=run_sha, project=project
    )
    report: dict[str, Any] = {
        "sha": run_sha,
        "entities": entity_counts,
        "edges": edge_counts,
    }
    if other_counts is not None:
        report["non_ast_edges"] = other_counts
    return report
