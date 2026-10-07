"""The storage-adapter interface — the fork seam of the autodoc graph (epic 702).

Epics 703-705 and 709 program against THIS abstract class only; no SQL leaks
above this line. That is what makes 706's pgvector implementation a drop-in.

Method set: exactly the spec's interface, plus two deliberate, documented
extensions the spec's list omitted but its constraints force:

* ``upsert_embeddings`` — the embedding pass must WRITE vectors, and the
  embedder is core code that may not import a storage backend (success
  criterion 4). Without a write path on the interface it would need raw SQL.
* ``update_corpus`` — ``stats()`` reports ``last_indexed_sha``, so something
  must set it (plus repo_ref/branch); the build flow is core code under the
  same constraint.
* ``corpus_id`` (read-only property) — composition code (the ingester) must
  be able to verify it is writing into the corpus it thinks it is BEFORE any
  write, without reaching for SQL or private attributes.
* ``find_edges`` (epic 703) — community detection needs EVERY ast edge in one
  deterministic bulk read. ``neighbors()`` cannot serve it: it reports only
  the edge that FIRST reached each neighbor, collapsing parallel rels between
  a pair, which would destroy the summed-over-parallel-edges weight rule.
* ``retire_agent_subgraph`` (downstream epic 889 / issue #1455) — a re-run of a doc
  agent drops sections and retracts relations, and nothing else can remove
  the rows it no longer emits: ``prune`` is ast-only by design, and
  ``replace_ast_edges`` refuses agent rows. Without a scoped retire on the
  interface the emitter would either leak deleted sections forever or reach
  for raw SQL, which is exactly what this seam exists to prevent.
* ``get_corpus`` (epic 704) — the deterministic renderer stamps
  ``source_repo`` from ``corpus.repo_ref`` and derives nav-page dates from
  ``corpus.indexed_at``; without a corpus read on the interface it would need
  raw SQL (``stats()`` exposes only ``last_indexed_sha``).

Row conventions: plain dicts with schema column names; JSON-typed columns
(``source_paths``, ``span``, ``frontmatter``, ``evidence``, ``member_ids``)
are passed as Python values (lists/dicts) — the backend owns serialization.
Vectors are ``list[float]`` of exactly ``vocab.EMBEDDING_DIM`` floats.

Every method is scoped to the corpus given to :meth:`open`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AdapterError(RuntimeError):
    """Loud failure surface for storage operations (never silently swallowed)."""


class GraphAdapter(ABC):
    """Abstract storage adapter for the two-tier autodoc graph."""

    # ---------------------------------------------------------------- lifecycle
    @abstractmethod
    def open(self, corpus_id: str, *, create: bool = True) -> None:
        """Open storage scoped to ``corpus_id``, creating schema + corpus row
        if absent. All subsequent calls operate on this corpus.

        ``create=False`` opens an EXISTING store only: storage that is missing
        or is not a store raises :class:`AdapterError` instead of being created,
        so a wrong path cannot read as an empty corpus. A ``corpus_id`` the
        store does not hold raises too, and nothing is written."""

    @abstractmethod
    def close(self) -> None:
        """Release the underlying storage. Idempotent. NEVER commits: every
        successful write method commits its own work, so pending state at
        close time is a failed batch that must be discarded."""

    @property
    @abstractmethod
    def corpus_id(self) -> str | None:
        """The corpus this adapter is open on (None before :meth:`open`)."""

    # ---------------------------------------------------------------- writes
    @abstractmethod
    def upsert_entities(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Idempotent by ``entity.id``. Returns ``{inserted, updated, unchanged}``.

        ``unchanged`` means content-identical (a re-stamp of ``last_seen_sha``
        with the same content still counts as unchanged — the double-run no-op
        guarantee). ``first_seen_sha`` is set on insert only.
        """

    @abstractmethod
    def upsert_edges(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Idempotent by the UNIQUE ``(corpus_id, src_id, dst_id, rel)`` tuple.
        Returns ``{inserted, updated, unchanged}``. Rows must satisfy the closed
        vocabulary + provenance rules (loud ``VocabError`` otherwise)."""

    @abstractmethod
    def replace_ast_edges(self, corpus_id: str, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Atomic swap of all ``origin='ast'`` edges for ``corpus_id``: delete
        + reinsert in one transaction (ast edges are a pure function of the
        code, rebuilt each run). Non-ast rows are a loud error. Agent/dreamed
        edges are untouched. Returns ``{deleted, inserted}``."""

    @abstractmethod
    def retire_agent_subgraph(
        self,
        corpus_id: str,
        root_id: str,
        keep_entity_ids: list[str],
        keep_edge_keys: list[tuple[str, str, str]],
    ) -> dict[str, int]:
        """Retire the agent-written rows of ONE document that its emitter no
        longer emits (downstream epic 889 / issue #1455). Returns exactly
        ``{entities_deleted, edges_deleted}``.

        Scope is the concept id subtree of ``root_id``: the root itself plus
        every ``<root_id>#<section>`` child. ``root_id`` must be a concept id
        (``con:``-prefixed) — anything else is a loud :class:`VocabError`, and
        a ``corpus_id`` that is not the open corpus is a loud
        :class:`AdapterError`.

        ONLY ``origin='agent'`` rows are ever candidates: ast rows that happen
        to fall inside the id scope are untouched, because the extractor owns
        their lifecycle (``prune``) and a doc re-run must never contradict it.

        Two passes, one transaction:

        1. agent edges whose ``src_id`` is in scope and whose
           ``(src_id, dst_id, rel)`` key is NOT in ``keep_edge_keys`` — the
           relations this run retracted;
        2. agent entities in scope not in ``keep_entity_ids``, together with
           ALL their remaining edges (either direction, any origin —
           referential integrity leaves no choice) and their embeddings.

        ``keep_edge_keys`` protects an edge only while both its endpoints
        survive; ``edges_deleted`` counts both passes.

        Both lists are what the emitter DID write, so a run that re-emits
        everything deletes nothing and returns zeros. Failure rolls the whole
        call back and raises :class:`AdapterError`: a half-retired document
        would read as a real one."""

    # ---------------------------------------------------------------- reads
    @abstractmethod
    def get_entity(self, id: str) -> dict[str, Any] | None:
        """The entity row (JSON columns deserialized), or None."""

    @abstractmethod
    def find_entities(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        """Entities matching a filter dict. Supported keys: ``tier``, ``kind``,
        ``name``, ``qualified_name`` (exact — epic 705 extension: the spec's
        router resolves symbol tokens on name / qualified_name),
        ``name_like`` (substring), ``path`` (member of source_paths),
        ``needs_embedding`` (bool: no embedding row, or one whose stored
        ``entity_version`` trails the entity's ``updated_at`` — i.e. the
        CONTENT changed since embedding; sha re-stamps never qualify),
        ``frontmatter`` (dict: containment over the entity's ``frontmatter``
        JSON — see below), ``limit``.
        Unknown keys are a loud error, not a silent ignore. Deterministic
        (id-ordered) results.

        ``frontmatter`` containment is specified HERE, in the contract, rather
        than left to each backend - this docstring is the fork seam, and a
        capability that means something slightly different per backend is
        worse than one that exists on none:

        - **Top-level keys only.** Every key in the filter dict must be present
          on the row's frontmatter with an **exactly equal** value.
        - **An empty dict matches every row** (no constraint expressed).
        - **Nested-object and array-partial matching are NOT supported**, and a
          filter value that is itself a dict or list is a loud
          :class:`AdapterError` - REFUSED, not quietly unmatched. An empty
          result would read as "no such document", and the caller would never
          learn the query could not express what they asked.

        The exclusion is deliberate rather than incidental: a document store
        with a native containment operator gets nested matching for free, while
        one built on scalar JSON path extraction does not. Rather than have the
        same call quietly behave differently depending on which backend is
        mounted, neither has it. If nested matching is ever wanted, it lands in
        every backend and in the conformance suite in ONE change."""

    @abstractmethod
    def find_edges(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        """Edge rows matching a filter dict (epic 703 extension — see module
        docstring). Supported keys: ``rel``, ``origin``, ``src_id``, ``dst_id``,
        ``limit``. Unknown keys are a loud error. Deterministic
        ``(rel, src_id, dst_id)`` order; ``evidence`` deserialized."""

    @abstractmethod
    def neighbors(self, id: str, rels: list[str] = [], depth: int = 1) -> list[dict[str, Any]]:  # noqa: B006 — spec-fixed signature; implementations must never mutate `rels`
        """The local-search primitive (epic 705). BFS over edges in BOTH
        directions from ``id`` up to ``depth`` hops, optionally restricted to
        ``rels``. Returns ``[{entity, rel, direction, depth}]`` — direction is
        'out' (id→neighbor) or 'in' (neighbor→id) for the edge that FIRST
        reached the neighbor; deterministic order (depth, then entity id)."""

    @abstractmethod
    def vector_search(
        self, vec: list[float], k: int, filter: dict[str, Any] | None = None
    ) -> list[tuple[str, float]]:
        """``[(entity_id, score)]``, best first. Score is cosine similarity
        (1 - cosine distance). ``filter`` supports ``tier`` and ``kind``."""

    # ---------------------------------------------------------------- communities
    @abstractmethod
    def upsert_communities(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Insert (id None/absent → assigned) or update (id given) community
        rows. Populated by epic 703; the table is defined here. Rows may carry
        an optional ``descriptor`` (dict — label evidence: member count, top
        symbols, top path prefixes; migration 0003).

        Partial-update semantics (epic 703 review blocker): on UPDATE, a
        column whose KEY IS ABSENT from the row dict is PRESERVED; an
        explicit ``None`` writes NULL. This is what lets the detector carry a
        community id forward without wiping 704's ``report`` /
        ``summarized_at_sha``, and lets the naming pass send label-only
        updates. ``level`` is always required (it scopes the row)."""

    @abstractmethod
    def get_communities(self, level: int) -> list[dict[str, Any]]:
        """Community rows at ``level`` (member_ids/descriptor deserialized),
        id-ordered. APPEND-ONLY SEMANTICS (epic 703): rows are never deleted
        automatically, so this returns current AND historical rows mixed.
        The CURRENT hierarchy is anchored by ``entity.community_id`` (level
        0) and ``parent_id`` chains upward; dissolved rows carry
        ``descriptor.dissolved``. Consumers (704/705) must filter by those
        anchors, never treat this listing as the live partition."""

    # ---------------------------------------------------------------- embeddings
    @abstractmethod
    def upsert_embeddings(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Rows: ``{entity_id, vec, model, embedded_at_sha, entity_version}``.
        Idempotent by ``entity_id``. Vector length must equal
        ``vocab.EMBEDDING_DIM`` and ``model`` must equal
        ``vocab.EMBEDDING_MODEL`` (both loud errors — a dimension or model
        drift would poison every later search). ``entity_version`` is the
        entity's ``updated_at`` at embed time: the content-change marker that
        drives the ``needs_embedding`` filter (``last_seen_sha`` advances on
        every build and must not trigger re-embedding)."""

    # ---------------------------------------------------------------- corpus + ops
    @abstractmethod
    def update_corpus(
        self,
        repo_ref: str | None = None,
        branch: str | None = None,
        last_indexed_sha: str | None = None,
        project: str | None = None,
    ) -> None:
        """Set corpus metadata; only non-None fields are written.
        ``indexed_at`` is stamped when ``last_indexed_sha`` is set.

        ``project`` is the project this corpus documents - the "room points at a
        project codebase" binding. The renderer stamps it onto every page, so it
        belongs to the corpus rather than to any page or agent."""

    @abstractmethod
    def get_corpus(self) -> dict[str, Any]:
        """The open corpus row: ``{id, repo_ref, branch, last_indexed_sha,
        indexed_at, project}`` (epic 704 extension — see module docstring)."""

    @abstractmethod
    def stats(self) -> dict[str, Any]:
        """``{entities, edges, communities, embedded, last_indexed_sha}`` plus
        additive breakdowns ``entities_by_tier`` and ``edges_by_rel`` (used by
        the CLI and the build smoke checks)."""

    @abstractmethod
    def prune(self, dry_run: bool = True) -> dict[str, Any]:
        """Deletion is NEVER automatic. Candidates are ``origin='ast'``
        entities whose ``last_seen_sha`` trails ``corpus.last_indexed_sha`` —
        only the extractor re-stamps every run, so staleness is meaningful
        only for the symbol tier it owns. Agent-written concept entities are
        never candidates (their lifecycle is the emitter's: see
        ``retire_agent_subgraph``). ``dry_run=True``
        (the default) only reports: ``{dry_run, candidates: [entity ids],
        communities: [community ids], deleted: 0}`` — ``communities`` names
        the distinct ``community_id`` values of the candidates (epic 703:
        their anchors are still alive at prune time; after deletion the
        detector could no longer name the dissolution). An explicit
        ``dry_run=False`` deletes the candidates plus their edges and
        embeddings and reports ``deleted``."""
