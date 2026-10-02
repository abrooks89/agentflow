"""SQLite + sqlite-vec implementation of the GraphAdapter interface (epic 702).

The agentflow/vault backend. WAL mode for concurrent reads during a run;
foreign keys ON so referential integrity fails loudly instead of rotting.
Vector search brute-forces ``vec_distance_cosine`` over the ``embedding``
table — exactly the spec's schema, no shadow vec0 virtual table — which is
plenty at the tens-of-thousands-of-entities scale this backend serves (the
pgvector backend of epic 706 owns the big-corpus story).

Migration model: a fresh database gets ``schema.sql`` as version 1 (PRAGMA
``user_version`` tracks it); future schema changes ship as
``migrations/<NNNN>_<name>.sql`` files applied in order. The directory is optional
in the sense that the runner tolerates its absence and is tested against a temp
dir — but it is NOT absent here: ``0002_add_entity_version``,
``0003_community_descriptor`` and ``0004_corpus_project`` are tracked beside this
file. The sentence said "currently absent" long after they landed.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

import sqlite_vec

from .adapter import AdapterError, GraphAdapter
from .vocab import (
    AGENT_DEFAULT_CONFIDENCE,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    REL_ORIGINS,
    REL_TIERS,
    RELS,
    TIERS,
    VocabError,
    validate_edge_provenance,
)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_SQLITE_HEADER = b"SQLite format 3\x00"

# Content columns compared for the inserted/updated/unchanged accounting.
# community_id is deliberately NOT here: epic 703 owns that column, and an
# extractor row (which never carries it) must not clobber an assigned value
# nor flip 'unchanged' to 'updated' (adversarial-review finding 5).
_ENTITY_CONTENT_COLS = (
    "tier", "kind", "name", "qualified_name", "source_paths", "span",
    "summary", "body", "frontmatter", "origin",
)
_ENTITY_JSON_COLS = ("source_paths", "span", "frontmatter")
_EDGE_CONTENT_COLS = ("origin", "confidence", "evidence")
_ENTITY_ORIGINS = frozenset({"ast", "agent"})  # entities are never 'dreamed'


def _canon(value: Any) -> str | None:
    """Canonical JSON for JSON-typed columns (stable key order => comparable)."""
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _unjson(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _now() -> str:
    # microseconds: updated_at doubles as the entity content-version marker for
    # embedding staleness (finding 6) — two content changes must never collide
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="microseconds")


def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"<{EMBEDDING_DIM}f", *vec)


# Max ids per IN-list. SQLite's default bind limit is 999 (32k in newer builds);
# chunking keeps every query far below either. Chunks always run inside one
# transaction, so atomicity is unaffected. Module-level so tests can force
# tiny chunks.
_SQL_CHUNK = 500


def _chunks(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _like_escape(value: str) -> str:
    """Neutralize LIKE wildcards so a literal stays literal (ESCAPE '\\')."""
    return str(value).replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


class SqliteGraphAdapter(GraphAdapter):
    """GraphAdapter over a single SQLite file (one file may hold many corpora)."""

    def __init__(self, db_path: Path | str, migrations_dir: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self._migrations_dir = migrations_dir if migrations_dir is not None else _DEFAULT_MIGRATIONS_DIR
        self._con: sqlite3.Connection | None = None
        self._corpus_id: str | None = None

    # ---------------------------------------------------------------- lifecycle
    def open(self, corpus_id: str, *, create: bool = True) -> None:
        if not corpus_id or not isinstance(corpus_id, str):
            raise AdapterError(f"corpus_id must be a non-empty string, got {corpus_id!r}")
        if not create:
            self._require_existing_store()
        con = sqlite3.connect(self.db_path)
        # checked before the WAL switch below, which would rewrite a foreign database
        if not create and con.execute("PRAGMA user_version").fetchone()[0] == 0:
            con.close()
            raise AdapterError(
                f"{self.db_path} is not a graph store: it is a SQLite database the "
                "graph schema was never applied to (only `graph build` creates a store)"
            )
        con.row_factory = sqlite3.Row
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        self._con = con
        self._corpus_id = corpus_id
        self._apply_migrations()
        con.execute("INSERT OR IGNORE INTO corpus (id) VALUES (?)", (corpus_id,))
        con.commit()

    def close(self) -> None:
        # NEVER commits: every successful public method commits its own work,
        # so anything still pending here is a failed batch that must die with
        # the connection (adversarial-review finding 1).
        if self._con is not None:
            self._con.close()
            self._con = None
            self._corpus_id = None

    def _require_existing_store(self) -> None:
        # sqlite3.connect() on a missing path creates it, and on a 0-byte file the
        # schema would be applied -- either way a wrong path reads as an empty
        # corpus (gh#459). A store open() created is never 0 bytes: the WAL switch
        # writes page 1 before open() returns, even mid-build.
        path = self.db_path
        hint = "check --db (only `graph build` creates a store)"
        if path.exists() and not path.is_file():
            raise AdapterError(f"no graph store at {path}: it is not a file; {hint}")
        if not path.is_file():
            raise AdapterError(f"no graph store at {path}: file does not exist; {hint}")
        if path.stat().st_size == 0:
            raise AdapterError(f"no graph store at {path}: file is 0 bytes; {hint}")
        with path.open("rb") as fh:
            header = fh.read(len(_SQLITE_HEADER))
        if header != _SQLITE_HEADER:
            raise AdapterError(f"no graph store at {path}: not a SQLite database; {hint}")

    @property
    def corpus_id(self) -> str | None:
        return self._corpus_id

    def _db(self) -> sqlite3.Connection:
        if self._con is None:
            raise AdapterError("adapter is not open — call open(corpus_id) first")
        return self._con

    def _apply_migrations(self) -> None:
        con = self._con
        assert con is not None
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            con.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            version = 1
            con.execute(f"PRAGMA user_version = {version}")
        if not self._migrations_dir.is_dir():
            return
        for path in sorted(self._migrations_dir.glob("[0-9]*.sql")):
            n = int(path.name.split("_", 1)[0])
            if n <= version:
                continue
            if n != version + 1:
                raise AdapterError(
                    f"migration gap: at version {version} but next file is {path.name}"
                )
            con.executescript(path.read_text(encoding="utf-8"))
            version = n
            con.execute(f"PRAGMA user_version = {version}")

    # ---------------------------------------------------------------- entities
    @staticmethod
    def _validated_entity(row: dict[str, Any]) -> dict[str, Any]:
        """Pure validation + normalization; touches no SQL."""
        entity_id = row.get("id")
        if not entity_id:
            raise AdapterError(f"entity row missing id: {row!r}")
        tier = row.get("tier")
        if tier not in TIERS:
            raise VocabError(f"unknown tier {tier!r} (allowed: {sorted(TIERS)})")
        origin = row.get("origin")
        if origin not in _ENTITY_ORIGINS:
            raise VocabError(
                f"unknown entity origin {origin!r} (allowed: {sorted(_ENTITY_ORIGINS)})"
            )
        content = {
            col: (_canon(row.get(col)) if col in _ENTITY_JSON_COLS else row.get(col))
            for col in _ENTITY_CONTENT_COLS
        }
        return {
            "id": entity_id,
            "content": content,
            "sha": row.get("last_seen_sha"),
            # community_id travels OUTSIDE the content comparison: 703 owns it
            "community_id": row.get("community_id"),
        }

    def upsert_entities(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        con = self._db()
        # Batch atomicity (finding 1): validate EVERY row before executing any
        # SQL, then run the writes under rollback-on-exception — a bad row must
        # never leave earlier rows committed.
        validated = [self._validated_entity(row) for row in rows]
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        try:
            for v in validated:
                entity_id, content, sha = v["id"], v["content"], v["sha"]
                existing = con.execute(
                    f"SELECT {', '.join(_ENTITY_CONTENT_COLS)}, last_seen_sha, community_id "
                    "FROM entity WHERE id = ? AND corpus_id = ?",
                    (entity_id, self._corpus_id),
                ).fetchone()
                if existing is None:
                    con.execute(
                        "INSERT INTO entity (id, corpus_id, tier, kind, name, qualified_name,"
                        " source_paths, span, summary, body, frontmatter, community_id, origin,"
                        " first_seen_sha, last_seen_sha, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (entity_id, self._corpus_id, content["tier"], content["kind"],
                         content["name"], content["qualified_name"], content["source_paths"],
                         content["span"], content["summary"], content["body"],
                         content["frontmatter"], v["community_id"], content["origin"],
                         sha, sha, _now()),
                    )
                    counts["inserted"] += 1
                    continue
                content_changed = {c: existing[c] for c in _ENTITY_CONTENT_COLS} != content
                # explicit community assignment (703's path); absent/None preserves
                community_changed = (
                    v["community_id"] is not None
                    and v["community_id"] != existing["community_id"]
                )
                if content_changed:
                    con.execute(
                        "UPDATE entity SET tier=?, kind=?, name=?, qualified_name=?,"
                        " source_paths=?, span=?, summary=?, body=?, frontmatter=?,"
                        " community_id=?, origin=?, last_seen_sha=?, updated_at=?"
                        " WHERE id = ? AND corpus_id = ?",
                        (content["tier"], content["kind"], content["name"],
                         content["qualified_name"], content["source_paths"], content["span"],
                         content["summary"], content["body"], content["frontmatter"],
                         v["community_id"] if v["community_id"] is not None
                         else existing["community_id"],
                         content["origin"],
                         sha if sha is not None else existing["last_seen_sha"],
                         _now(), entity_id, self._corpus_id),
                    )
                    counts["updated"] += 1
                elif community_changed:
                    con.execute(
                        "UPDATE entity SET community_id=? WHERE id=? AND corpus_id=?",
                        (v["community_id"], entity_id, self._corpus_id),
                    )
                    counts["updated"] += 1
                else:
                    # content-identical: the double-run no-op path. A new sha only
                    # re-stamps last_seen_sha (still counted unchanged; updated_at
                    # untouched, so embeddings stay fresh — finding 6).
                    if sha is not None and sha != existing["last_seen_sha"]:
                        con.execute(
                            "UPDATE entity SET last_seen_sha=? WHERE id=? AND corpus_id=?",
                            (sha, entity_id, self._corpus_id),
                        )
                    counts["unchanged"] += 1
        except BaseException:
            con.rollback()
            raise
        con.commit()
        return counts

    def get_entity(self, id: str) -> dict[str, Any] | None:
        con = self._db()
        row = con.execute(
            "SELECT * FROM entity WHERE id = ? AND corpus_id = ?",
            (id, self._corpus_id),
        ).fetchone()
        return self._entity_dict(row) if row is not None else None

    @staticmethod
    def _entity_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for col in _ENTITY_JSON_COLS:
            d[col] = _unjson(d[col])
        return d

    _FILTER_KEYS = frozenset(
        {"tier", "kind", "name", "qualified_name", "name_like", "path",
         "needs_embedding", "frontmatter", "limit"}
    )

    def find_entities(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        con = self._db()
        unknown = set(filter) - self._FILTER_KEYS
        if unknown:
            raise AdapterError(
                f"unknown filter key(s) {sorted(unknown)}; "
                f"supported: {sorted(self._FILTER_KEYS)}"
            )
        where = ["entity.corpus_id = ?"]
        params: list[Any] = [self._corpus_id]
        for key in ("tier", "kind", "name", "qualified_name"):
            if key in filter:
                where.append(f"entity.{key} = ?")
                params.append(filter[key])
        if "name_like" in filter:
            where.append("entity.name LIKE ? ESCAPE '\\'")
            escaped = _like_escape(filter["name_like"])
            params.append(f"%{escaped}%")
        if "path" in filter:
            where.append(
                "EXISTS (SELECT 1 FROM json_each(entity.source_paths) je WHERE je.value = ?)"
            )
            params.append(filter["path"])
        if "frontmatter" in filter:
            wanted = filter["frontmatter"]
            if not isinstance(wanted, dict):
                raise AdapterError(
                    "filter key 'frontmatter' takes a dict (containment over "
                    f"top-level keys); got {type(wanted).__name__}"
                )
            # An empty dict expresses no constraint, so it matches every row -
            # contract, not an accident of the loop below.
            for fm_key, fm_value in wanted.items():
                # Nested/array values are unsupported per the ABC contract, and
                # they are REFUSED rather than left to miss. "Matches nothing"
                # would read to the caller as "no such document", and they would
                # never learn the query could not express what they asked.
                if isinstance(fm_value, (dict, list)):
                    raise AdapterError(
                        f"frontmatter filter {fm_key!r}: nested-object and array "
                        "matching are not supported (top-level keys, exact "
                        "equality only) - see GraphAdapter.find_entities"
                    )
                # json_each, NOT json_extract with a built path. The key is bound
                # as a VALUE and never becomes path syntax, so a key containing a
                # quote, a '$' or a dot is data - no escaping, and a dotted key
                # stays a literal key rather than a nested traversal.
                #
                # This is a portability fix, not only a safety one: the built-path
                # form matched a quote-containing key on sqlite 3.50 and returned
                # NOTHING on CI's older library. A filter whose answer depends on
                # the linked SQLite version is the per-backend drift this contract
                # exists to forbid, and it fails in the silent direction.
                #
                # json_each over a NULL/absent frontmatter yields no rows, so such
                # entities never satisfy a constraint.
                where.append(
                    "EXISTS (SELECT 1 FROM json_each(entity.frontmatter) je "
                    "WHERE je.key = ? AND je.value = ?)"
                )
                params.append(str(fm_key))
                params.append(fm_value)
        if filter.get("needs_embedding"):
            # Staleness keys on CONTENT change (updated_at), never on
            # last_seen_sha — which advances every build and would re-embed
            # the whole corpus each run (finding 6).
            where.append(
                "(em.entity_id IS NULL OR em.entity_version IS NULL"
                " OR em.entity_version != entity.updated_at)"
            )
        sql = (
            "SELECT entity.* FROM entity"
            " LEFT JOIN embedding em ON em.entity_id = entity.id"
            f" WHERE {' AND '.join(where)} ORDER BY entity.id"
        )
        if "limit" in filter:
            sql += " LIMIT ?"
            params.append(int(filter["limit"]))
        return [self._entity_dict(r) for r in con.execute(sql, params)]

    # ---------------------------------------------------------------- edges
    def _validated_edge(self, row: dict[str, Any]) -> dict[str, Any]:
        rel = row.get("rel")
        origin = row.get("origin")
        confidence = row.get("confidence")
        if confidence is None and origin == "agent":
            confidence = AGENT_DEFAULT_CONFIDENCE
        validate_edge_provenance(rel, origin, confidence if confidence is not None else -1.0)
        tiers: dict[str, str] = {}
        for endpoint in ("src_id", "dst_id"):
            eid = row.get(endpoint)
            if not eid:
                raise AdapterError(f"edge row missing {endpoint}: {row!r}")
            found = self._db().execute(
                "SELECT tier FROM entity WHERE id = ? AND corpus_id = ?",
                (eid, self._corpus_id),
            ).fetchone()
            if found is None:
                raise AdapterError(
                    f"edge {endpoint} references a missing entity: {eid!r} "
                    f"(corpus {self._corpus_id!r}) — referential integrity is loud"
                )
            tiers[endpoint] = found["tier"]
        # Tier pairing (finding 7): e.g. documents is concept->symbol, the ast
        # rels are symbol->symbol; 'related' is tier-agnostic.
        required = REL_TIERS[rel]
        if required is not None and (tiers["src_id"], tiers["dst_id"]) != required:
            raise VocabError(
                f"relation {rel!r} requires tier pairing {required[0]}->{required[1]}, "
                f"got {tiers['src_id']}->{tiers['dst_id']} "
                f"({row['src_id']} -> {row['dst_id']})"
            )
        return {
            "src_id": row["src_id"], "dst_id": row["dst_id"], "rel": rel,
            "origin": origin, "confidence": confidence,
            "evidence": _canon(row.get("evidence")),
            "last_seen_sha": row.get("last_seen_sha"),
        }

    def upsert_edges(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        con = self._db()
        # validate-all-before-any-write + rollback guard (finding 1)
        validated = [self._validated_edge(raw) for raw in rows]
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        try:
            self._apply_edge_upserts(con, validated, counts)
        except BaseException:
            con.rollback()
            raise
        con.commit()
        return counts

    def _apply_edge_upserts(
        self, con: sqlite3.Connection, validated: list[dict[str, Any]],
        counts: dict[str, int],
    ) -> None:
        for row in validated:
            existing = con.execute(
                "SELECT origin, confidence, evidence, last_seen_sha FROM edge"
                " WHERE corpus_id=? AND src_id=? AND dst_id=? AND rel=?",
                (self._corpus_id, row["src_id"], row["dst_id"], row["rel"]),
            ).fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO edge (corpus_id, src_id, dst_id, rel, origin,"
                    " confidence, evidence, last_seen_sha) VALUES (?,?,?,?,?,?,?,?)",
                    (self._corpus_id, row["src_id"], row["dst_id"], row["rel"],
                     row["origin"], row["confidence"], row["evidence"],
                     row["last_seen_sha"]),
                )
                counts["inserted"] += 1
            elif {c: existing[c] for c in _EDGE_CONTENT_COLS} != {
                c: row[c] for c in _EDGE_CONTENT_COLS
            }:
                con.execute(
                    "UPDATE edge SET origin=?, confidence=?, evidence=?, last_seen_sha=?"
                    " WHERE corpus_id=? AND src_id=? AND dst_id=? AND rel=?",
                    (row["origin"], row["confidence"], row["evidence"],
                     row["last_seen_sha"] if row["last_seen_sha"] is not None
                     else existing["last_seen_sha"],
                     self._corpus_id, row["src_id"], row["dst_id"], row["rel"]),
                )
                counts["updated"] += 1
            else:
                if row["last_seen_sha"] is not None and row["last_seen_sha"] != existing["last_seen_sha"]:
                    con.execute(
                        "UPDATE edge SET last_seen_sha=?"
                        " WHERE corpus_id=? AND src_id=? AND dst_id=? AND rel=?",
                        (row["last_seen_sha"], self._corpus_id,
                         row["src_id"], row["dst_id"], row["rel"]),
                    )
                counts["unchanged"] += 1

    def replace_ast_edges(self, corpus_id: str, rows: list[dict[str, Any]]) -> dict[str, int]:
        con = self._db()
        if corpus_id != self._corpus_id:
            raise AdapterError(
                f"replace_ast_edges corpus mismatch: adapter is open on corpus "
                f"{self._corpus_id!r}, got {corpus_id!r}"
            )
        # Pre-validate EVERYTHING before touching data: a bad row must not
        # leave the graph half-swapped.
        validated = []
        for raw in rows:
            if raw.get("origin") != "ast":
                raise VocabError(
                    f"replace_ast_edges only accepts origin='ast' rows, got "
                    f"{raw.get('origin')!r} ({raw.get('rel')!r} edge)"
                )
            validated.append(self._validated_edge(raw))
        try:
            deleted = con.execute(
                "DELETE FROM edge WHERE corpus_id = ? AND origin = 'ast'",
                (corpus_id,),
            ).rowcount
            con.executemany(
                "INSERT INTO edge (corpus_id, src_id, dst_id, rel, origin,"
                " confidence, evidence, last_seen_sha) VALUES (?,?,?,?,?,?,?,?)",
                [(corpus_id, r["src_id"], r["dst_id"], r["rel"], r["origin"],
                  r["confidence"], r["evidence"], r["last_seen_sha"]) for r in validated],
            )
        except sqlite3.Error as e:
            con.rollback()
            raise AdapterError(f"replace_ast_edges failed, rolled back: {e}") from e
        con.commit()
        return {"deleted": deleted, "inserted": len(validated)}

    def retire_agent_subgraph(
        self,
        corpus_id: str,
        root_id: str,
        keep_entity_ids: list[str],
        keep_edge_keys: list[tuple[str, str, str]],
    ) -> dict[str, int]:
        """Delete the agent rows of one document that its emitter stopped
        emitting. Scope = ``root_id`` plus its ``#``-suffixed children; only
        origin='agent' rows are candidates, so ast rows sharing the id prefix
        survive (the extractor owns those — see :meth:`prune`)."""
        con = self._db()
        if corpus_id != self._corpus_id:
            raise AdapterError(
                f"retire_agent_subgraph corpus mismatch: adapter is open on corpus "
                f"{self._corpus_id!r}, got {corpus_id!r}"
            )
        if not isinstance(root_id, str) or not root_id.startswith("con:"):
            raise VocabError(
                f"retire_agent_subgraph root must be a concept id "
                f"(con:<corpus>:<type>:<slug>), got {root_id!r}"
            )
        # id scope: the root row itself, or a '<root>#<section>' child. The
        # root is a LIKE prefix, so its own wildcards are escaped away.
        scope_params = (self._corpus_id, root_id, _like_escape(root_id) + "#%")
        keep_ids = set(keep_entity_ids)
        keep_keys = {tuple(key) for key in keep_edge_keys}
        # Pass 1 candidates: agent edges out of the scope that this run did not
        # re-emit. Selected by row id so the delete needs no compound key match.
        retracted = [
            row["id"] for row in con.execute(
                "SELECT id, src_id, dst_id, rel FROM edge WHERE corpus_id = ?"
                " AND origin = 'agent'"
                " AND (src_id = ? OR src_id LIKE ? ESCAPE '\\')"
                " ORDER BY id",
                scope_params,
            )
            if (row["src_id"], row["dst_id"], row["rel"]) not in keep_keys
        ]
        # Pass 2 candidates: agent entities in scope this run did not re-emit.
        doomed = [
            row[0] for row in con.execute(
                "SELECT id FROM entity WHERE corpus_id = ?"
                " AND origin = 'agent'"
                " AND (id = ? OR id LIKE ? ESCAPE '\\')"
                " ORDER BY id",
                scope_params,
            )
            if row[0] not in keep_ids
        ]
        if not retracted and not doomed:
            return {"entities_deleted": 0, "edges_deleted": 0}
        edges_deleted = 0
        try:
            # one transaction: a half-retired document would read as a real one
            for chunk in _chunks(retracted, _SQL_CHUNK):
                marks = ",".join("?" * len(chunk))
                edges_deleted += con.execute(
                    f"DELETE FROM edge WHERE id IN ({marks})", chunk
                ).rowcount
            # prune's ordering: edges, then embeddings, then the entity itself
            for chunk in _chunks(doomed, _SQL_CHUNK):
                marks = ",".join("?" * len(chunk))
                edges_deleted += con.execute(
                    f"DELETE FROM edge WHERE corpus_id = ?"
                    f" AND (src_id IN ({marks}) OR dst_id IN ({marks}))",
                    [self._corpus_id, *chunk, *chunk],
                ).rowcount
                con.execute(
                    f"DELETE FROM embedding WHERE entity_id IN ({marks})", chunk
                )
                con.execute(
                    f"DELETE FROM entity WHERE corpus_id = ? AND id IN ({marks})",
                    [self._corpus_id, *chunk],
                )
        except sqlite3.Error as e:
            con.rollback()
            raise AdapterError(f"retire_agent_subgraph failed, rolled back: {e}") from e
        con.commit()
        return {"entities_deleted": len(doomed), "edges_deleted": edges_deleted}

    _EDGE_FILTER_KEYS = frozenset({"rel", "origin", "src_id", "dst_id", "limit"})

    def find_edges(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        """Bulk edge read (epic 703 extension): deterministic (rel, src, dst)
        order, evidence deserialized, scoped to the open corpus."""
        con = self._db()
        unknown = set(filter) - self._EDGE_FILTER_KEYS
        if unknown:
            raise AdapterError(
                f"unknown find_edges filter key(s) {sorted(unknown)}; "
                f"supported: {sorted(self._EDGE_FILTER_KEYS)}"
            )
        where = ["corpus_id = ?"]
        params: list[Any] = [self._corpus_id]
        for key in ("rel", "origin", "src_id", "dst_id"):
            if key in filter:
                where.append(f"{key} = ?")
                params.append(filter[key])
        sql = (
            f"SELECT * FROM edge WHERE {' AND '.join(where)}"
            " ORDER BY rel, src_id, dst_id"
        )
        if "limit" in filter:
            sql += " LIMIT ?"
            params.append(int(filter["limit"]))
        out = []
        for row in con.execute(sql, params):
            d = dict(row)
            d["evidence"] = _unjson(d["evidence"])
            out.append(d)
        return out

    # ---------------------------------------------------------------- traversal
    def neighbors(self, id: str, rels: list[str] = [], depth: int = 1) -> list[dict[str, Any]]:  # noqa: B006 — spec-fixed signature; `rels` is read-only here
        con = self._db()
        for rel in rels:
            if rel not in RELS:
                raise VocabError(f"unknown relation {rel!r}: the vocabulary is CLOSED")
        rel_clause = ""
        rel_params: list[Any] = []
        if rels:
            rel_clause = f" AND rel IN ({','.join('?' * len(rels))})"
            rel_params = list(rels)
        visited = {id}
        found: list[tuple[int, str, str, str]] = []  # (depth, entity_id, rel, direction)
        frontier = [id]
        for d in range(1, depth + 1):
            if not frontier:
                break
            # Chunked IN-lists (finding 8): a wide frontier must not blow the
            # SQLite bind-variable limit. Rows are re-sorted after merging so
            # traversal order is identical to a single-query run.
            rows: list[sqlite3.Row] = []
            for chunk in _chunks(frontier, _SQL_CHUNK):
                marks = ",".join("?" * len(chunk))
                rows.extend(con.execute(
                    f"SELECT src_id, dst_id, rel FROM edge WHERE corpus_id = ?"
                    f" AND (src_id IN ({marks}) OR dst_id IN ({marks})){rel_clause}",
                    [self._corpus_id, *chunk, *chunk, *rel_params],
                ).fetchall())
            rows.sort(key=lambda r: (r["rel"], r["src_id"], r["dst_id"]))
            next_frontier = []
            for row in rows:
                if row["src_id"] in visited and row["dst_id"] not in visited:
                    other, direction = row["dst_id"], "out"
                elif row["dst_id"] in visited and row["src_id"] not in visited:
                    other, direction = row["src_id"], "in"
                else:
                    continue
                visited.add(other)
                found.append((d, other, row["rel"], direction))
                next_frontier.append(other)
            frontier = next_frontier
        found.sort(key=lambda t: (t[0], t[1]))
        return [
            {"entity": self.get_entity(eid), "rel": rel, "direction": direction, "depth": d}
            for d, eid, rel, direction in found
        ]

    # ---------------------------------------------------------------- embeddings
    def upsert_embeddings(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        con = self._db()
        # validate-all-before-any-write + rollback guard (finding 1)
        for row in rows:
            vec = row.get("vec")
            if not isinstance(vec, (list, tuple)) or len(vec) != EMBEDDING_DIM:
                got = len(vec) if isinstance(vec, (list, tuple)) else type(vec).__name__
                raise AdapterError(
                    f"embedding vector must be exactly {EMBEDDING_DIM} floats, got {got}"
                )
            model = row.get("model")
            if model != EMBEDDING_MODEL:
                raise AdapterError(
                    f"embedding model mismatch: this graph is fixed to "
                    f"{EMBEDDING_MODEL!r}, got {model!r}"
                )
            entity_id = row.get("entity_id")
            exists = con.execute(
                "SELECT 1 FROM entity WHERE id = ? AND corpus_id = ?",
                (entity_id, self._corpus_id),
            ).fetchone()
            if exists is None:
                raise AdapterError(
                    f"embedding references a missing entity: {entity_id!r}"
                )
        counts = {"inserted": 0, "updated": 0}
        try:
            for row in rows:
                entity_id = row["entity_id"]
                had = con.execute(
                    "SELECT 1 FROM embedding WHERE entity_id = ?", (entity_id,)
                ).fetchone()
                con.execute(
                    "INSERT INTO embedding (entity_id, vec, model, embedded_at_sha,"
                    " entity_version)"
                    " VALUES (?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET"
                    " vec=excluded.vec, model=excluded.model,"
                    " embedded_at_sha=excluded.embedded_at_sha,"
                    " entity_version=excluded.entity_version",
                    (entity_id, _pack_vec(list(row["vec"])), row["model"],
                     row.get("embedded_at_sha"), row.get("entity_version")),
                )
                counts["updated" if had else "inserted"] += 1
        except BaseException:
            con.rollback()
            raise
        con.commit()
        return counts

    _VEC_FILTER_KEYS = frozenset({"tier", "kind"})

    def vector_search(
        self, vec: list[float], k: int, filter: dict[str, Any] | None = None
    ) -> list[tuple[str, float]]:
        con = self._db()
        if len(vec) != EMBEDDING_DIM:
            raise AdapterError(
                f"query vector must be exactly {EMBEDDING_DIM} floats, got {len(vec)}"
            )
        filter = filter or {}
        unknown = set(filter) - self._VEC_FILTER_KEYS
        if unknown:
            raise AdapterError(
                f"unknown vector_search filter key(s) {sorted(unknown)}; "
                f"supported: {sorted(self._VEC_FILTER_KEYS)}"
            )
        where = ["e.corpus_id = ?"]
        params: list[Any] = [self._corpus_id]
        for key in ("tier", "kind"):
            if key in filter:
                where.append(f"e.{key} = ?")
                params.append(filter[key])
        rows = con.execute(
            "SELECT em.entity_id, vec_distance_cosine(em.vec, ?) AS dist"
            " FROM embedding em JOIN entity e ON e.id = em.entity_id"
            f" WHERE {' AND '.join(where)} ORDER BY dist ASC, em.entity_id LIMIT ?",
            [_pack_vec(list(vec)), *params, int(k)],
        ).fetchall()
        return [(r["entity_id"], 1.0 - r["dist"]) for r in rows]

    # ---------------------------------------------------------------- communities
    def upsert_communities(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        con = self._db()
        # validate-all-before-any-write + rollback guard, same discipline as
        # the other batch methods (adversarial-review finding R2-2): a bad row
        # mid-batch must never leave earlier inserts pending for the NEXT
        # successful method's commit to sweep in.
        validated = []
        for row in rows:
            if row.get("level") is None:
                raise AdapterError(f"community row missing level: {row!r}")
            cid = row.get("id")
            if cid is not None:
                found = con.execute(
                    "SELECT 1 FROM community WHERE id = ? AND corpus_id = ?",
                    (cid, self._corpus_id),
                ).fetchone()
                if found is None:
                    raise AdapterError(
                        f"community id {cid} not found in corpus {self._corpus_id!r}"
                    )
            # Partial-update semantics (adapter contract): only keys PRESENT
            # in the row are written on UPDATE — an absent key preserves the
            # stored value, an explicit None writes NULL. This is what stops
            # a carried-forward detector row from wiping 704's report.
            fields = {}
            for col in ("parent_id", "label", "report", "member_ids",
                        "descriptor", "summarized_at_sha"):
                if col in row:
                    val = row[col]
                    if col in ("member_ids", "descriptor"):
                        val = _canon(val)
                    fields[col] = val
            validated.append({"id": cid, "level": row["level"], "fields": fields})
        counts = {"inserted": 0, "updated": 0}
        try:
            for v in validated:
                f = v["fields"]
                if v["id"] is None:
                    con.execute(
                        "INSERT INTO community (corpus_id, level, parent_id, label,"
                        " report, member_ids, descriptor, summarized_at_sha)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (self._corpus_id, v["level"], f.get("parent_id"),
                         f.get("label"), f.get("report"), f.get("member_ids"),
                         f.get("descriptor"), f.get("summarized_at_sha")),
                    )
                    counts["inserted"] += 1
                else:
                    sets = ["level=?"]
                    params: list[Any] = [v["level"]]
                    for col in ("parent_id", "label", "report", "member_ids",
                                "descriptor", "summarized_at_sha"):
                        if col in f:
                            sets.append(f"{col}=?")
                            params.append(f[col])
                    con.execute(
                        f"UPDATE community SET {', '.join(sets)}"
                        " WHERE id=? AND corpus_id=?",
                        [*params, v["id"], self._corpus_id],
                    )
                    counts["updated"] += 1
        except BaseException:
            con.rollback()
            raise
        con.commit()
        return counts

    def get_communities(self, level: int) -> list[dict[str, Any]]:
        con = self._db()
        rows = con.execute(
            "SELECT * FROM community WHERE corpus_id = ? AND level = ? ORDER BY id",
            (self._corpus_id, level),
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["member_ids"] = _unjson(d["member_ids"])
            d["descriptor"] = _unjson(d["descriptor"])
            out.append(d)
        return out

    # ---------------------------------------------------------------- corpus / ops
    def update_corpus(
        self,
        repo_ref: str | None = None,
        branch: str | None = None,
        last_indexed_sha: str | None = None,
        project: str | None = None,
    ) -> None:
        con = self._db()
        sets, params = [], []
        for col, val in (("repo_ref", repo_ref), ("branch", branch),
                         ("last_indexed_sha", last_indexed_sha),
                         ("project", project)):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        if last_indexed_sha is not None:
            sets.append("indexed_at = ?")
            params.append(_now())
        if not sets:
            return
        con.execute(
            f"UPDATE corpus SET {', '.join(sets)} WHERE id = ?",
            [*params, self._corpus_id],
        )
        con.commit()

    def get_corpus(self) -> dict[str, Any]:
        con = self._db()
        row = con.execute(
            "SELECT * FROM corpus WHERE id = ?", (self._corpus_id,)
        ).fetchone()
        if row is None:  # open() inserts the row; absence means storage rot
            raise AdapterError(f"corpus row missing for {self._corpus_id!r}")
        return dict(row)

    def stats(self) -> dict[str, Any]:
        con = self._db()
        c = self._corpus_id
        one = lambda sql: con.execute(sql, (c,)).fetchone()[0]  # noqa: E731
        entities_by_tier = dict(con.execute(
            "SELECT tier, COUNT(*) FROM entity WHERE corpus_id=? GROUP BY tier ORDER BY tier", (c,)
        ).fetchall())
        edges_by_rel = dict(con.execute(
            "SELECT rel, COUNT(*) FROM edge WHERE corpus_id=? GROUP BY rel ORDER BY rel", (c,)
        ).fetchall())
        return {
            "entities": one("SELECT COUNT(*) FROM entity WHERE corpus_id=?"),
            "edges": one("SELECT COUNT(*) FROM edge WHERE corpus_id=?"),
            "communities": one("SELECT COUNT(*) FROM community WHERE corpus_id=?"),
            "embedded": one(
                "SELECT COUNT(*) FROM embedding em"
                " JOIN entity e ON e.id = em.entity_id WHERE e.corpus_id=?"
            ),
            "last_indexed_sha": one("SELECT last_indexed_sha FROM corpus WHERE id=?"),
            "entities_by_tier": entities_by_tier,
            "edges_by_rel": edges_by_rel,
        }

    def verify(self) -> list[str]:
        """Integrity findings for the open corpus ([] = clean).

        Backend-specific operability method (the `graph verify` CLI), NOT part
        of the abstract GraphAdapter interface — it inspects physical storage
        invariants (blob sizes, cross-corpus rows) that only this backend has.
        """
        con = self._db()
        c = self._corpus_id
        findings: list[str] = []
        for row in con.execute(
            "SELECT e.id, e.rel, e.origin, e.confidence FROM edge e"
            " WHERE e.corpus_id = ?", (c,)
        ):
            if row["rel"] not in RELS:
                findings.append(f"edge {row['id']}: unknown rel {row['rel']!r}")
            elif row["origin"] not in REL_ORIGINS[row["rel"]]:
                findings.append(
                    f"edge {row['id']}: rel {row['rel']!r} does not accept "
                    f"origin {row['origin']!r}"
                )
            if row["origin"] == "ast" and row["confidence"] != 1.0:
                findings.append(
                    f"edge {row['id']}: ast confidence must be 1.0, "
                    f"got {row['confidence']}"
                )
            if row["origin"] == "dreamed" and row["confidence"] > 0.4:
                findings.append(
                    f"edge {row['id']}: dreamed confidence capped at 0.4, "
                    f"got {row['confidence']}"
                )
        for row in con.execute(
            "SELECT e.id, e.src_id, e.dst_id FROM edge e WHERE e.corpus_id = ? AND ("
            " NOT EXISTS (SELECT 1 FROM entity s WHERE s.id = e.src_id AND s.corpus_id = e.corpus_id)"
            " OR NOT EXISTS (SELECT 1 FROM entity d WHERE d.id = e.dst_id AND d.corpus_id = e.corpus_id))",
            (c,),
        ):
            findings.append(
                f"edge {row['id']}: dangling endpoint ({row['src_id']} -> {row['dst_id']})"
            )
        for row in con.execute(
            "SELECT id, tier FROM entity WHERE corpus_id = ? AND tier NOT IN ('symbol', 'concept')",
            (c,),
        ):
            findings.append(f"entity {row['id']}: unknown tier {row['tier']!r}")
        expected_blob = EMBEDDING_DIM * 4
        for row in con.execute(
            "SELECT em.entity_id, LENGTH(em.vec) AS n, em.model FROM embedding em"
            " JOIN entity e ON e.id = em.entity_id WHERE e.corpus_id = ?", (c,)
        ):
            if row["n"] != expected_blob:
                findings.append(
                    f"embedding {row['entity_id']}: blob is {row['n']} bytes, "
                    f"expected {expected_blob} ({EMBEDDING_DIM} float32)"
                )
            if row["model"] != EMBEDDING_MODEL:
                findings.append(
                    f"embedding {row['entity_id']}: model {row['model']!r} != "
                    f"{EMBEDDING_MODEL!r}"
                )
        return findings

    def prune(self, dry_run: bool = True) -> dict[str, Any]:
        """Report (default) or delete stale entities. AST-ORIGIN ONLY.

        Candidacy is restricted to origin='ast' entities: only the extractor
        re-stamps every run, so sha staleness is meaningful only for the
        symbol tier it owns. Agent-written concept entities (and their edges,
        including the `documents` tier join) are NEVER candidates — the
        concept lifecycle is the emitter's (adversarial-review finding 2):
        see :meth:`retire_agent_subgraph`.
        Deletes are chunked (finding 8) inside a single transaction.
        """
        con = self._db()
        last = con.execute(
            "SELECT last_indexed_sha FROM corpus WHERE id = ?", (self._corpus_id,)
        ).fetchone()[0]
        if last is None:
            return {"dry_run": dry_run, "candidates": [], "communities": [], "deleted": 0}
        candidates = [r[0] for r in con.execute(
            "SELECT id FROM entity WHERE corpus_id = ? AND origin = 'ast'"
            " AND (last_seen_sha IS NULL OR last_seen_sha != ?) ORDER BY id",
            (self._corpus_id, last),
        )]
        # Communities whose anchor entities are about to go (epic 703 review
        # finding 6a): report them NOW — after deletion nothing points at
        # them, and the next detection run could no longer name them.
        communities: set[int] = set()
        for chunk in _chunks(candidates, _SQL_CHUNK):
            marks = ",".join("?" * len(chunk))
            communities.update(r[0] for r in con.execute(
                f"SELECT DISTINCT community_id FROM entity WHERE corpus_id = ?"
                f" AND community_id IS NOT NULL AND id IN ({marks})",
                [self._corpus_id, *chunk],
            ))
        if dry_run or not candidates:
            return {"dry_run": dry_run, "candidates": candidates,
                    "communities": sorted(communities), "deleted": 0}
        try:
            # every chunked DELETE runs inside this one transaction; the single
            # commit below makes the whole prune atomic
            for chunk in _chunks(candidates, _SQL_CHUNK):
                marks = ",".join("?" * len(chunk))
                con.execute(
                    f"DELETE FROM edge WHERE corpus_id = ?"
                    f" AND (src_id IN ({marks}) OR dst_id IN ({marks}))",
                    [self._corpus_id, *chunk, *chunk],
                )
                con.execute(
                    f"DELETE FROM embedding WHERE entity_id IN ({marks})", chunk
                )
                con.execute(
                    f"DELETE FROM entity WHERE corpus_id = ? AND id IN ({marks})",
                    [self._corpus_id, *chunk],
                )
        except sqlite3.Error as e:
            con.rollback()
            raise AdapterError(f"prune failed, rolled back: {e}") from e
        con.commit()
        return {"dry_run": False, "candidates": candidates,
                "communities": sorted(communities), "deleted": len(candidates)}
