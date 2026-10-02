"""The deterministic query router (epic 705 scope 1).

A question goes ``local`` or ``global``. Cheap and explainable — NO LLM
round-trip in the default path. The rule order is spec-fixed:

1. **Explicit override wins.** ``mode='local'|'global'`` is honored verbatim,
   never silently downgraded.
2. **Entity resolution → local.** Symbol-shaped tokens (CamelCase,
   snake_case, dotted paths, path-like tokens, backticked spans) are resolved
   via ``find_entities()`` exact-then-prefix on ``name``; any hit routes
   local with the matched ids as evidence.
3. **Breadth markers → global.** A CLOSED marker list, plus a closed set of
   plural abstract heads. Open-ended heuristics would break determinism.
4. **Default → local with a measured fallback.** Embed and seed as for local;
   a best-seed score below ``SEED_FLOOR`` re-routes to global, recording the
   re-route. This is the only path that spends an embedding before deciding.

Routing is deterministic: the same question against the same corpus routes
identically every time (``find_entities`` is id-ordered, markers are checked
in fixed order, and the embedding model is pinned).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .tuning import DEFAULT_K, SEED_FLOOR
from .types import QueryError, RoutingDecision

MODES = ("auto", "local", "global")

# Closed breadth-marker list (spec scope 1). Multi-word markers are matched as
# substrings of the lowercased question; single words at word boundaries.
BREADTH_MARKERS: tuple[str, ...] = (
    "overview",
    "architecture",
    "how does the system",
    "what are the main",
    "across",
    "generally",
    "summarize",
    "summarise",
    "compare",
)

# Closed plural-abstract-head set (the spec's "plural abstract heads").
PLURAL_ABSTRACT_HEADS: frozenset[str] = frozenset({
    "areas", "components", "concepts", "layers", "modules", "packages",
    "parts", "patterns", "pieces", "responsibilities", "services",
    "subsystems", "systems", "themes", "tiers",
})

# Tokens that look like code symbols (spec: CamelCase, snake_case, dotted
# paths, anything containing '/' or a source-file extension, backticked spans).
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_CAMEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*$|^[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*$")
_SNAKE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+$")
_DOTTED_RE = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+$")
_FILE_EXT_RE = re.compile(r"\.(py|ts|tsx|js|jsx|go|rs|java|rb|c|h|cpp|cs)$")
_STRIP_PUNCT = ".,;:!?\"'()[]{}"

# Multi-word markers match at word boundaries, never as raw substrings —
# 'systemd' must not trigger 'how does the system' (adversarial finding 4).
_MULTI_MARKER_RES: dict[str, re.Pattern[str]] = {
    m: re.compile(r"\b" + re.escape(m) + r"\b")
    for m in BREADTH_MARKERS if " " in m
}

_MAX_TOKENS = 8          # resolution cost guard: first N symbol-shaped tokens
_MAX_EVIDENCE_IDS = 10   # evidence stays readable
_PREFIX_MIN_LEN = 3      # avoid degenerate one-letter prefix sweeps


def _looks_symbol_shaped(token: str) -> bool:
    if "/" in token or _FILE_EXT_RE.search(token):
        return True
    return bool(
        _CAMEL_RE.match(token)
        or _SNAKE_RE.match(token)
        or _DOTTED_RE.match(token)
    )


def extract_symbol_tokens(question: str) -> list[str]:
    """Symbol-shaped tokens in question order: backticked spans first (an
    explicit signal from the asker), then bare tokens. Deduplicated."""
    tokens: list[str] = []
    seen: set[str] = set()

    def add(tok: str) -> None:
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    for span in _BACKTICK_RE.findall(question):
        add(span.strip())
    without_ticks = _BACKTICK_RE.sub(" ", question)
    for raw in without_ticks.split():
        tok = raw.strip(_STRIP_PUNCT)
        # trailing sentence dot must not defeat the dotted-path check
        if _looks_symbol_shaped(tok):
            add(tok)
    return tokens[:_MAX_TOKENS]


def resolve_entities(adapter, tokens: list[str]) -> dict[str, list[str]]:
    """Exact-then-prefix resolution on ``name``; path-shaped tokens also try
    the ``path`` filter (a member of ``source_paths``). Returns
    ``{token: [entity ids]}`` for tokens that resolved."""
    hits: dict[str, list[str]] = {}
    for tok in tokens:
        exact = adapter.find_entities({"name": tok, "limit": _MAX_EVIDENCE_IDS})
        ids = [e["id"] for e in exact]
        if not ids:
            by_qn = adapter.find_entities(
                {"qualified_name": tok, "limit": _MAX_EVIDENCE_IDS})
            ids = [e["id"] for e in by_qn]
        if not ids and "/" in tok:
            by_path = adapter.find_entities({"path": tok, "limit": _MAX_EVIDENCE_IDS})
            ids = [e["id"] for e in by_path]
        if not ids and len(tok) >= _PREFIX_MIN_LEN:
            near = adapter.find_entities({"name_like": tok, "limit": 50})
            low = tok.lower()
            ids = [e["id"] for e in near
                   if str(e.get("name", "")).lower().startswith(low)]
            ids = ids[:_MAX_EVIDENCE_IDS]
        if ids:
            hits[tok] = ids
    return hits


def find_breadth_marker(question: str) -> str | None:
    """First matching marker from the closed list, else a plural abstract
    head, else None. Fixed check order keeps routing deterministic."""
    low = question.lower()
    words = [w.strip(_STRIP_PUNCT) for w in low.split()]
    for marker in BREADTH_MARKERS:
        if " " in marker:
            if _MULTI_MARKER_RES[marker].search(low):
                return marker
        elif marker in words:
            return marker
    for word in words:
        if word in PLURAL_ABSTRACT_HEADS:
            return word
    return None


def route(
    question: str,
    adapter,
    *,
    mode: str = "auto",
    embed: Callable[[str], list[float]] | None = None,
    k: int = DEFAULT_K,
    seed_floor: float = SEED_FLOOR,
) -> tuple[RoutingDecision, list[tuple[str, float]] | None, list[float] | None]:
    """Decide local vs global. Returns ``(decision, seeds, qvec)`` — seeds and
    the query vector are non-None only when the default path spent an
    embedding, so local search never embeds the same question twice."""
    if mode not in MODES:
        raise QueryError(f"unknown mode {mode!r}: expected one of {MODES}")

    # Rule 1: explicit override wins, never silently downgraded.
    if mode != "auto":
        return RoutingDecision(mode=mode, rule="override"), None, None

    # Rule 2: entity resolution → local.
    tokens = extract_symbol_tokens(question)
    if tokens:
        hits = resolve_entities(adapter, tokens)
        if hits:
            ids: list[str] = []
            for tok in tokens:
                ids.extend(i for i in hits.get(tok, []) if i not in ids)
            decision = RoutingDecision(
                mode="local",
                rule="entity_resolution",
                evidence={"tokens": list(hits), "entity_ids": ids[:_MAX_EVIDENCE_IDS]},
            )
            return decision, None, None

    # Rule 3: breadth markers → global.
    marker = find_breadth_marker(question)
    if marker is not None:
        decision = RoutingDecision(
            mode="global", rule="breadth_marker", evidence={"marker": marker},
        )
        return decision, None, None

    # Rule 4: default → local, with the measured SEED_FLOOR fallback. The only
    # path that spends an embedding before deciding.
    if embed is None:
        raise QueryError(
            "router default path needs an embedder: no override, no entity "
            "resolution, and no breadth marker fired, so the question must be "
            "embedded to measure seed quality (pass embed=)"
        )
    qvec = embed(question)
    seeds = adapter.vector_search(qvec, k)
    best = seeds[0][1] if seeds else None
    if best is None or best < seed_floor:
        decision = RoutingDecision(
            mode="global",
            rule="seed_floor_reroute",
            evidence={"best_seed_score": best, "seed_floor": seed_floor},
            reroute_from="local",
        )
        return decision, seeds, qvec
    decision = RoutingDecision(
        mode="local",
        rule="default_local",
        evidence={"best_seed_score": best, "seed_floor": seed_floor},
    )
    return decision, seeds, qvec
