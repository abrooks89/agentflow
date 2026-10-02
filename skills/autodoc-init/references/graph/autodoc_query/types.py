"""Result and provenance types of the query layer (epic 705).

Provenance is NON-OPTIONAL: every ContextItem carries entity identity, tier,
origin, the traversal path that reached it (each hop's rel/origin/confidence),
and a derived ``min_confidence``. A caller must always be able to tell an
exact AST fact from an agent assertion from a dreamed link.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# The citation marker the spec requires on anything reached through a
# dreamed edge ("the rendered answer must say so in the citation").
SPECULATIVE_MARKER = "via hypothesized link"


class QueryError(RuntimeError):
    """Loud failure surface of the query layer — never an empty result."""


@dataclass(frozen=True)
class RoutingDecision:
    """Why a question went local or global. Attached to every result."""

    mode: str                      # 'local' | 'global'
    rule: str                      # 'override' | 'entity_resolution' |
    #                                'breadth_marker' | 'default_local' |
    #                                'seed_floor_reroute'
    evidence: dict[str, Any] = field(default_factory=dict)  # rule-specific
    reroute_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "rule": self.rule,
            "evidence": dict(self.evidence),
            "reroute_from": self.reroute_from,
        }


@dataclass(frozen=True)
class PathHop:
    """One traversed edge, with full edge provenance."""

    src_id: str
    dst_id: str
    rel: str
    direction: str       # 'out' (src reached dst) | 'in' (edge points back)
    origin: str          # 'ast' | 'agent' | 'dreamed'
    confidence: float


@dataclass(frozen=True)
class TierRef:
    """A lightweight cross-tier reference carried on a ContextItem."""

    entity_id: str
    name: str
    kind: str
    span: dict[str, Any] | None = None


@dataclass
class ContextItem:
    """One retrieved entity with non-optional provenance."""

    entity_id: str
    tier: str                       # 'symbol' | 'concept'
    kind: str
    name: str
    source_paths: list[str]
    span: dict[str, Any] | None     # {file, start_line, end_line}; None => concept
    origin: str                     # entity origin: 'ast' | 'agent'
    path: list[PathHop]             # traversal path from the seed ([] = seed)
    min_confidence: float           # min edge confidence along `path` (1.0 = seed)
    score: float
    depth: int
    summary: str | None = None
    speculative: bool = False       # reached through a dreamed edge
    # Tier-crossing contract: a symbol ALWAYS carries `documented_by`
    # (explicitly [] when undocumented — a signal, not an omission); a concept
    # ALWAYS carries `documents`. The inapplicable field stays None.
    documented_by: list[TierRef] | None = None
    documents: list[TierRef] | None = None
    community: dict[str, Any] | None = None   # global items: {id, level, label, score}

    @property
    def citation(self) -> str:
        """Rendered file:line citation; names the hypothesized-link caveat."""
        if self.span and self.span.get("file"):
            where = (f"{self.span['file']}:{self.span.get('start_line', '?')}-"
                     f"{self.span.get('end_line', '?')}")
        elif self.source_paths:
            where = ", ".join(self.source_paths)
        else:
            where = self.tier
        cite = f"{self.name} ({where})"
        if self.speculative:
            cite += f" [{SPECULATIVE_MARKER}]"
        return cite

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["citation"] = self.citation
        return d


@dataclass
class QueryResult:
    """Assembled, cited context. The caller's model writes the prose."""

    question: str
    routing: RoutingDecision
    items: list[ContextItem]
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)
    # Global-search evidence (empty for local): what was scored, what was kept.
    reports: list[dict[str, Any]] = field(default_factory=list)
    communities_scored: list[dict[str, Any]] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def communities_kept(self) -> list[dict[str, Any]]:
        return [c for c in self.communities_scored if c.get("kept")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "routing": self.routing.to_dict(),
            "items": [i.to_dict() for i in self.items],
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "reports": self.reports,
            "communities_scored": self.communities_scored,
            "communities_kept": self.communities_kept,
            "timings_ms": self.timings_ms,
        }
