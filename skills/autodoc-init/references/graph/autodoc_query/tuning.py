"""Numeric tuning constants of the query layer (epic 705).

Every budget guard the spec requires is named here, once. These are tunable
defaults — but DEPTH_CAP is a hard spec constraint (depth>2 is a validation
error, not a clamp) and SEED_FLOOR is part of the router contract (the only
path that spends an embedding before deciding).
"""

from __future__ import annotations

# ---------------------------------------------------------------- router
# Best-seed cosine-similarity floor for the default-local path: below it the
# question is judged too unanchored for entity-seeded traversal and re-routes
# to global (recorded as reroute_from='local').
#
# CALIBRATION CAVEAT (adversarial finding 7): this default was validated
# against the deterministic hash-vector embedder (lexical overlap), not real
# nomic-embed-text output — real nomic cosine similarities run higher for
# unrelated pairs (often 0.4+), so 0.30 likely under-reroutes there. It is
# operator-tunable end to end (route(seed_floor=), query(seed_floor=),
# graph-query --seed-floor); measure the real distribution via
# run_eval's per-query best_seed_score once an embedding endpoint is
# reachable, then recalibrate HERE.
SEED_FLOOR = 0.30

# ---------------------------------------------------------------- local search
DEFAULT_K = 8              # vector_search seed count
DEPTH_CAP = 2              # hard spec cap; depth>2 is a validation error
DEFAULT_DEPTH = 2
HOP_DECAY = 0.75           # per-hop score decay (rank blend component)
DOC_CROSS_DECAY = 0.9      # tier-crossing hop factor (outside the depth budget)
DEFAULT_FANOUT_CAP = 25    # neighbors retained per expanded node (hub guard)
DEFAULT_MAX_ENTITIES = 50  # total retained-entity cap on assembled context
DEFAULT_LOCAL_BUDGET_MS = 2500   # wall-clock; the spec's local p95 target

# ---------------------------------------------------------------- global search
DEFAULT_GLOBAL_BUDGET_MS = 8000  # wall-clock; the spec's global target
SCORE_BATCH_SIZE = 8       # reports per scoring call (deterministic batches)
SCORE_PARALLELISM = 4      # concurrent scoring calls (map step)
RELEVANCE_FLOOR = 5.0      # keep reports scored >= floor (0-10 scale)
MAX_KEPT_REPORTS = 8       # top-ranked kept after the filter step
