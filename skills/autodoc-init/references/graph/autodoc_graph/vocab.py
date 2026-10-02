"""Closed vocabularies and fixed constants of the autodoc graph (epic 702 spec).

Adding a relation is a schema change, not an agent decision — an open
vocabulary makes traversal unqueryable. Everything here is spec-fixed:
change requires a spec revision, not a call-site convenience.
"""

from __future__ import annotations

# The closed relation vocabulary (spec: 8 rels).
RELS: frozenset[str] = frozenset({
    "imports",      # symbol→symbol  ast     module import
    "calls",        # symbol→symbol  ast     resolved call site
    "defines",      # symbol→symbol  ast     container → member
    "routes_to",    # symbol→symbol  ast     route decl → handler
    "documents",    # concept→symbol agent   the tier join
    "depends_on",   # concept→concept agent  asserted architectural dependency
    "part_of",      # concept→concept agent  composition
    "related",      # any            agent|dreamed  weak association
})

# Relations rebuilt from scratch each run (pure functions of the code).
AST_RELS: frozenset[str] = frozenset({"imports", "calls", "defines", "routes_to"})

ORIGINS: frozenset[str] = frozenset({"ast", "agent", "dreamed"})
TIERS: frozenset[str] = frozenset({"symbol", "concept"})

# Which origins may assert each relation (the spec's rel table, enforced).
# An agent-origin 'calls' edge would collide with replace_ast_edges swaps and
# corrupt the deterministic tier; a dreamed 'documents' would fabricate the tier
# join. Closed matrix, loudly enforced.
REL_ORIGINS: dict[str, frozenset[str]] = {
    "imports": frozenset({"ast"}),
    "calls": frozenset({"ast"}),
    "defines": frozenset({"ast"}),
    "routes_to": frozenset({"ast"}),
    "documents": frozenset({"agent"}),
    "depends_on": frozenset({"agent"}),
    "part_of": frozenset({"agent"}),
    "related": frozenset({"agent", "dreamed"}),
}

# Required (src_tier, dst_tier) per relation; None = tier-agnostic ('related').
# Enforced where endpoints are fetched (the storage backend).
REL_TIERS: dict[str, tuple[str, str] | None] = {
    "imports": ("symbol", "symbol"),
    "calls": ("symbol", "symbol"),
    "defines": ("symbol", "symbol"),
    "routes_to": ("symbol", "symbol"),
    "documents": ("concept", "symbol"),   # the tier join
    "depends_on": ("concept", "concept"),
    "part_of": ("concept", "concept"),
    "related": None,
}

# Origin → confidence rules: ast is exact, agent defaults to 0.7,
# dreamed is capped at 0.4 (the vault's provenance model, generalized).
AST_CONFIDENCE = 1.0
AGENT_DEFAULT_CONFIDENCE = 0.7
DREAMED_MAX_CONFIDENCE = 0.4

# 768 dims and nomic-embed-text are FIXED, not defaults: the target retrieval
# store's schema declares Vector(768) and embeds via Ollama nomic-embed-text;
# matching it is what lets the query layer target the existing store.
EMBEDDING_DIM = 768
EMBEDDING_MODEL = "nomic-embed-text"


class VocabError(ValueError):
    """A row violated the closed vocabulary or provenance rules."""


def validate_edge_provenance(rel: str, origin: str, confidence: float) -> None:
    """Loudly reject edges outside the closed vocabulary / provenance rules."""
    if rel not in RELS:
        raise VocabError(
            f"unknown relation {rel!r}: the vocabulary is CLOSED ({sorted(RELS)})"
        )
    if origin not in ORIGINS:
        raise VocabError(f"unknown origin {origin!r} (allowed: {sorted(ORIGINS)})")
    if origin not in REL_ORIGINS[rel]:
        raise VocabError(
            f"relation {rel!r} does not accept origin {origin!r} "
            f"(allowed origins: {sorted(REL_ORIGINS[rel])})"
        )
    if origin == "ast" and confidence != AST_CONFIDENCE:
        raise VocabError(
            f"ast edges are exact: confidence must be {AST_CONFIDENCE}, got {confidence}"
        )
    if origin == "dreamed" and confidence > DREAMED_MAX_CONFIDENCE:
        raise VocabError(
            f"dreamed edges are capped at confidence {DREAMED_MAX_CONFIDENCE}, "
            f"got {confidence}"
        )
    if not 0.0 <= confidence <= 1.0:
        raise VocabError(f"confidence out of range [0,1]: {confidence}")
