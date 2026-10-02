"""Retrieval + routing metrics, reimplemented cleanly (no external prior
art): recall@k, MRR, the routing confusion matrix, and the suffix-based file
matcher that keeps the query set corpus-root-agnostic."""

from __future__ import annotations

from typing import Any


def match_file(source_path: str, expected: str) -> bool:
    """True when ``source_path`` names ``expected``. Suffix-based (the store
    holds repo-relative paths whose root prefix depends on where extraction
    ran) and separator-safe; the suffix must start at a path boundary."""
    norm = source_path.replace("\\", "/")
    exp = expected.replace("\\", "/")
    return norm == exp or norm.endswith("/" + exp)


def ranked_files(items: list[dict[str, Any] | Any]) -> list[str]:
    """Unique source files of a result, in item-rank order."""
    out: list[str] = []
    for item in items:
        paths = item["source_paths"] if isinstance(item, dict) else item.source_paths
        for p in paths or []:
            if p not in out:
                out.append(p)
    return out


def recall_at_k(ranked: list[str], expected: list[str], k: int) -> float:
    """Fraction of expected files found within the top-k ranked files."""
    if not expected:
        raise ValueError("recall needs a non-empty expected set")
    top = ranked[:k]
    hit = sum(1 for e in expected if any(match_file(p, e) for p in top))
    return hit / len(expected)


def mrr(ranked: list[str], expected: list[str]) -> float:
    """Reciprocal rank of the FIRST expected file found (0.0 when none is)."""
    for i, p in enumerate(ranked):
        if any(match_file(p, e) for e in expected):
            return 1.0 / (i + 1)
    return 0.0


def confusion_matrix(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """``{expected_mode: {predicted_mode: count}}`` over both modes."""
    conf = {"local": {"local": 0, "global": 0},
            "global": {"local": 0, "global": 0}}
    for row in rows:
        conf[row["expected_mode"]][row["predicted_mode"]] += 1
    return conf


def routing_accuracy(rows: list[dict[str, Any]]) -> float:
    if not rows:
        raise ValueError("routing accuracy over zero queries is meaningless")
    hits = sum(1 for r in rows if r["expected_mode"] == r["predicted_mode"])
    return hits / len(rows)
