"""Run the labelled query set against a live store: routing accuracy with a
per-mode confusion breakdown (success criterion 2), plus recall@5/recall@10/
MRR for the local half.

Composition root for the eval surface (the one eval module allowed to open a
storage backend). Retrieval metrics are computed only for local-expected
queries that actually routed local; global-expected queries are evaluated on
routing alone (their answers have no expected-files ground truth).

Usage:
    python -m autodoc_query.eval.run_eval --db graph.db --corpus agentflow \
        [--queries path.json] [--ollama-url URL | --deterministic-vectors] \
        [--routing-only] [--json out.json] [--min-accuracy 0.9]
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path
from time import perf_counter
from typing import Any

from autodoc_graph.embedder import OllamaEmbedder
from autodoc_graph.sqlite_backend import SqliteGraphAdapter

from ..api import assert_embedding_model, query
from ..router import route
from ..types import QueryError
from .deterministic import DeterministicEmbedder
from .metrics import confusion_matrix, mrr, ranked_files, recall_at_k, routing_accuracy


def load_queries(path: str | None) -> list[dict[str, Any]]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    blob = resources.files("autodoc_query.eval").joinpath("queries.json")
    return json.loads(blob.read_text(encoding="utf-8"))


def evaluate(
    adapter,
    queries: list[dict[str, Any]],
    *,
    embedder: Any,
    scorer: Any | None = None,
    routing_only: bool = False,
) -> dict[str, Any]:
    """Route (and optionally retrieve) every labelled query. Deterministic
    given a fixed store + embedder."""
    assert_embedding_model(embedder)

    def embed(text: str) -> list[float]:
        return embedder.embed_texts([text])[0]

    rows: list[dict[str, Any]] = []
    for entry in queries:
        row: dict[str, Any] = {
            "id": entry["id"],
            "query": entry["query"],
            "expected_mode": entry["expected_mode"],
        }
        t0 = perf_counter()
        decision, _seeds, _qvec = route(entry["query"], adapter, embed=embed)
        row["predicted_mode"] = decision.mode
        row["rule"] = decision.rule
        row["route_ms"] = (perf_counter() - t0) * 1000.0
        # SEED_FLOOR calibration evidence (finding 7): present whenever the
        # default path spent an embedding
        if "best_seed_score" in decision.evidence:
            row["best_seed_score"] = decision.evidence["best_seed_score"]
        expected_files = entry.get("expected_files") or []
        if (not routing_only and expected_files and decision.mode == "local"):
            t0 = perf_counter()
            res = query(entry["query"], adapter, embedder=embedder, scorer=scorer)
            row["query_ms"] = (perf_counter() - t0) * 1000.0
            ranked = ranked_files(res.items)
            row["recall@5"] = recall_at_k(ranked, expected_files, 5)
            row["recall@10"] = recall_at_k(ranked, expected_files, 10)
            row["mrr"] = mrr(ranked, expected_files)
            row["truncated"] = res.truncated
        rows.append(row)

    retrieved = [r for r in rows if "mrr" in r]
    summary: dict[str, Any] = {
        "queries": len(rows),
        "routing_accuracy": routing_accuracy(rows),
        "confusion": confusion_matrix(rows),
        "per_query": rows,
    }
    if retrieved:
        summary["retrieval"] = {
            "evaluated": len(retrieved),
            "recall@5": sum(r["recall@5"] for r in retrieved) / len(retrieved),
            "recall@10": sum(r["recall@10"] for r in retrieved) / len(retrieved),
            "mrr": sum(r["mrr"] for r in retrieved) / len(retrieved),
        }
    return summary


def _print_report(summary: dict[str, Any]) -> None:
    conf = summary["confusion"]
    print(f"routing accuracy: {summary['routing_accuracy']:.1%} "
          f"over {summary['queries']} queries")
    print("confusion (expected -> predicted):")
    for exp in ("local", "global"):
        print(f"  {exp:>6}: local={conf[exp]['local']:>2}  "
              f"global={conf[exp]['global']:>2}")
    misrouted = [r for r in summary["per_query"]
                 if r["expected_mode"] != r["predicted_mode"]]
    for r in misrouted:
        print(f"  MISROUTE {r['id']}: expected {r['expected_mode']}, "
              f"got {r['predicted_mode']} (rule={r['rule']}) — {r['query']!r}")
    if "retrieval" in summary:
        ret = summary["retrieval"]
        print(f"retrieval over {ret['evaluated']} local queries: "
              f"recall@5={ret['recall@5']:.2f} recall@10={ret['recall@10']:.2f} "
              f"MRR={ret['mrr']:.2f}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run_eval", description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True)
    p.add_argument("--corpus", required=True)
    p.add_argument("--queries", default=None, help="override the packaged set")
    p.add_argument("--ollama-url", default=None)
    p.add_argument("--deterministic-vectors", action="store_true",
                   help="hash-based lexical vectors instead of Ollama — "
                        "NOT semantic; use only when no endpoint is reachable")
    p.add_argument("--routing-only", action="store_true")
    p.add_argument("--scorer-url", default=None)
    p.add_argument("--scorer-model", default=None)
    p.add_argument("--json", dest="json_out", default=None)
    p.add_argument("--min-accuracy", type=float, default=None,
                   help="exit 1 when routing accuracy lands below this")
    args = p.parse_args(argv)

    if args.deterministic_vectors:
        print("WARNING: deterministic hash vectors — lexical overlap only, "
              "NOT semantic embeddings. Results understate semantic recall.",
              file=sys.stderr)
        embedder: Any = DeterministicEmbedder()
    else:
        embedder = OllamaEmbedder(url=args.ollama_url)

    adapter = SqliteGraphAdapter(args.db)
    try:
        adapter.open(args.corpus)
        summary = evaluate(adapter, load_queries(args.queries),
                           embedder=embedder, routing_only=args.routing_only)
    except QueryError as e:
        print(f"run_eval FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        adapter.close()

    summary["embedding"] = ("deterministic-hash (NOT semantic)"
                            if args.deterministic_vectors else "ollama")
    _print_report(summary)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2),
                                       encoding="utf-8")
    if args.min_accuracy is not None and summary["routing_accuracy"] < args.min_accuracy:
        print(f"routing accuracy below --min-accuracy {args.min_accuracy}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
