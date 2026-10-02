"""Shared fixtures for the autodoc_graph test suite."""

from __future__ import annotations

import pytest

from autodoc_graph.sqlite_backend import SqliteGraphAdapter


def entity_row(id: str, **over) -> dict:
    """A minimal valid symbol-tier entity row."""
    row = {
        "id": id,
        "tier": "symbol",
        "kind": "function",
        "name": id.rsplit(":", 1)[-1],
        "qualified_name": f"app/x.py:{id.rsplit(':', 1)[-1]}",
        "source_paths": ["app/x.py"],
        "span": {"file": "app/x.py", "start_line": 1, "end_line": 2},
        "summary": "def f():",
        "body": None,
        "frontmatter": None,
        "origin": "ast",
    }
    row.update(over)
    return row


def edge_row(src: str, dst: str, rel: str = "calls", **over) -> dict:
    row = {
        "src_id": src,
        "dst_id": dst,
        "rel": rel,
        "origin": "ast",
        "confidence": 1.0,
        "evidence": {"file": "app/x.py", "line": 3},
    }
    row.update(over)
    return row


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "graph.db"


@pytest.fixture
def adapter(db_path):
    a = SqliteGraphAdapter(db_path)
    a.open("corp")
    yield a
    a.close()
