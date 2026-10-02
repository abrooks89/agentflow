"""autodoc_query — the read side of the autodoc graph (epic 705).

Local/global search routing, tier-crossing retrieval, and provenance-carrying
results over the two-tier graph that epics 702-704 build. Storage-agnostic:
everything here programs against the abstract ``GraphAdapter`` — the CLI is
the only composition root allowed to import a backend (test-enforced, same
rule as ``autodoc_graph``).

Packaging decision: this package ships as a sibling
module inside the SAME installable distribution as ``autodoc_graph``
(``skills/autodoc-init/references/graph/``), so the existing provision step
(``pip install -e`` of the graph package) needs no new step.
"""

from .api import global_search, local_search, query
from .types import (
    ContextItem,
    PathHop,
    QueryError,
    QueryResult,
    RoutingDecision,
    TierRef,
)

__all__ = [
    "ContextItem",
    "PathHop",
    "QueryError",
    "QueryResult",
    "RoutingDecision",
    "TierRef",
    "global_search",
    "local_search",
    "query",
]
