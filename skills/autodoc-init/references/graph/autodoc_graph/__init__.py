"""autodoc_graph — the storage-agnostic graph core of autodoc (epic 702).

Two-tier entity model: exact tree-sitter symbols (tier='symbol', extracted by
the Node inventory package into deterministic JSONL) joined with LLM-curated
concepts (tier='concept', written by epic 704) through a closed 8-relation
edge vocabulary. This package owns the schema, the storage-adapter interface
(the fork seam epics 703-706 program against), the SQLite + sqlite-vec
backend, the Ollama embedding pass, the JSONL ingester, and the `graph` CLI.

Import discipline (spec constraint, enforced by test): `ingest`, `embedder`,
`adapter`, and `vocab` never import the SQLite backend — they see storage only
through the abstract `GraphAdapter` interface.
"""

__version__ = "0.3.0"
