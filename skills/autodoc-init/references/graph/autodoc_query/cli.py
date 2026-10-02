"""graph-query — the query layer's CLI and its ONE composition root.

Only here may a storage backend be imported and wired (the same rule and the
same test enforcement as autodoc_graph.cli). The skill surface
(skills/graph-query) shells out to this command; `local:` / `global:`
question prefixes are explicit overrides, never silently downgraded.

Output is a JSON QueryResult by default (machine consumers: the skill, the
eval harness); ``--pretty`` renders routed, cited context for humans.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from autodoc_graph.adapter import AdapterError
from autodoc_graph.embedder import EmbedderError
from autodoc_graph.sqlite_backend import SqliteGraphAdapter
from autodoc_graph.vocab import VocabError

from .api import query
from .scorer import ScorerError
from .tuning import (
    DEFAULT_DEPTH,
    DEFAULT_FANOUT_CAP,
    DEFAULT_GLOBAL_BUDGET_MS,
    DEFAULT_K,
    DEFAULT_LOCAL_BUDGET_MS,
    DEFAULT_MAX_ENTITIES,
    SCORE_BATCH_SIZE,
    SCORE_PARALLELISM,
    SEED_FLOOR,
)
from .types import QueryError, QueryResult, RoutingDecision

_ERRORS = (QueryError, ScorerError, EmbedderError, AdapterError, VocabError,
           sqlite3.Error)


def parse_prefix(question: str, mode_flag: str | None) -> tuple[str, str, bool]:
    """`local:` / `global:` prefixes are skill-surface overrides. A prefix
    that CONTRADICTS an explicit --mode is a loud error, not a guess."""
    prefix_mode: str | None = None
    stripped = question
    for prefix in ("local:", "global:"):
        if question.lower().startswith(prefix):
            prefix_mode = prefix[:-1]
            stripped = question[len(prefix):].strip()
            break
    if prefix_mode and mode_flag and mode_flag not in ("auto", prefix_mode):
        raise QueryError(
            f"mode conflict: question prefix {prefix_mode!r} vs --mode "
            f"{mode_flag!r} — pick one"
        )
    return stripped, (prefix_mode or mode_flag or "auto"), prefix_mode is not None


def _render_pretty(res: QueryResult) -> str:
    d: RoutingDecision = res.routing
    lines = [
        f"routing: {d.mode} (rule={d.rule}"
        + (f", rerouted from {d.reroute_from}" if d.reroute_from else "")
        + f") evidence={json.dumps(d.evidence, sort_keys=True)}",
    ]
    if res.truncated:
        lines.append("TRUNCATED: " + "; ".join(res.truncation_reasons))
    if res.reports:
        lines.append(f"kept {len(res.reports)} community report(s):")
        for rep in res.reports:
            lines.append(
                f"  [{rep['score']:.0f}/10] {rep['label']} (community "
                f"{rep['community_id']}, level {rep['level']}): {rep['summary']}"
            )
    lines.append(f"{len(res.items)} context item(s):")
    for item in res.items:
        lines.append(
            f"  - {item.citation}  [tier={item.tier} origin={item.origin} "
            f"score={item.score:.2f} min_conf={item.min_confidence:.2f} "
            f"depth={item.depth}]"
        )
        if item.summary:
            lines.append(f"      {item.summary.splitlines()[0][:120]}")
    if res.timings_ms:
        parts = ", ".join(f"{k}={v:.0f}" for k, v in res.timings_ms.items())
        lines.append(f"timings ms: {parts}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="graph-query",
        description="query the autodoc graph (local/global routed search)",
    )
    p.add_argument("question", help="the question; a 'local:' or 'global:' "
                                    "prefix forces that mode")
    p.add_argument("--db", required=True, help="SQLite database path")
    p.add_argument("--corpus", required=True, help="corpus id")
    p.add_argument("--mode", choices=["auto", "local", "global"], default=None)
    p.add_argument("--k", type=int, default=DEFAULT_K)
    p.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    p.add_argument("--expand-tiers", default="both",
                   choices=["both", "symbols_only", "concepts_only"])
    p.add_argument("--include-dreamed", action="store_true",
                   help="opt in to origin='dreamed' edges (items arrive "
                        "flagged speculative)")
    p.add_argument("--fanout-cap", type=int, default=DEFAULT_FANOUT_CAP)
    p.add_argument("--max-entities", type=int, default=DEFAULT_MAX_ENTITIES)
    p.add_argument("--seed-floor", type=float, default=SEED_FLOOR,
                   help="best-seed similarity below which the default path "
                        "re-routes to global (see tuning.py on calibration)")
    p.add_argument("--score-batch-size", type=int, default=SCORE_BATCH_SIZE,
                   help="reports per scoring call (global map step)")
    p.add_argument("--score-parallelism", type=int, default=SCORE_PARALLELISM,
                   help="concurrent scoring calls per wave; the wall-clock "
                        "budget is re-checked between waves")
    p.add_argument("--local-budget-ms", type=float, default=DEFAULT_LOCAL_BUDGET_MS)
    p.add_argument("--global-budget-ms", type=float, default=DEFAULT_GLOBAL_BUDGET_MS)
    p.add_argument("--ollama-url", default=None,
                   help="embedding endpoint (default: $AUTODOC_OLLAMA_URL or "
                        "http://localhost:11434)")
    p.add_argument("--scorer-url", default=None,
                   help="report-scoring endpoint (default: $AUTODOC_SCORER_URL "
                        "> $AUTODOC_OLLAMA_URL > http://localhost:11434; a "
                        "path ending in /v1 is treated as OpenAI-compatible)")
    p.add_argument("--scorer-model", default=None,
                   help="report-scoring model (default: $AUTODOC_SCORER_MODEL; "
                        "required when global search actually scores)")
    p.add_argument("--pretty", action="store_true",
                   help="human-readable rendering instead of JSON")
    return p


def main(argv: list[str] | None = None,
         embedder: Any | None = None,
         scorer: Any | None = None) -> int:
    """``embedder``/``scorer`` are injectable for tests only; the console
    entrypoint always constructs the real ones from flags/env."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    # A read-only query surface must never write: opening a nonexistent path
    # would CREATE a fresh store and then misdiagnose as an empty corpus
    # (adversarial finding 3). Refuse before any adapter construction.
    if not Path(args.db).is_file():
        print(
            f"graph-query FAILED: no graph store at {args.db} — check the "
            "path, or run /autodoc-init to build one",
            file=sys.stderr,
        )
        return 1
    adapter = SqliteGraphAdapter(args.db)
    try:
        question, mode, via_prefix = parse_prefix(args.question, args.mode)
        adapter.open(args.corpus)
        res = query(
            question, adapter, mode=mode,
            embedder=embedder, ollama_url=args.ollama_url,
            scorer=scorer, scorer_url=args.scorer_url,
            scorer_model=args.scorer_model,
            k=args.k, depth=args.depth, expand_tiers=args.expand_tiers,
            include_dreamed=args.include_dreamed, fanout_cap=args.fanout_cap,
            max_entities=args.max_entities,
            local_budget_ms=args.local_budget_ms,
            global_budget_ms=args.global_budget_ms,
            seed_floor=args.seed_floor,
            score_batch_size=args.score_batch_size,
            score_parallelism=args.score_parallelism,
        )
        if via_prefix:  # name the override's source in the decision evidence
            res.routing = dataclasses.replace(res.routing,
                                              evidence={"via": "cli prefix"})
        print(_render_pretty(res) if args.pretty
              else json.dumps(res.to_dict(), indent=2))
        return 0
    except _ERRORS as e:
        print(f"graph-query FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
