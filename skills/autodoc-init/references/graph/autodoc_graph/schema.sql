-- autodoc graph core schema (epic 702 spec, verbatim structure).
-- Applied to a fresh database as migration 1 (PRAGMA user_version tracks the
-- applied version; future changes land as autodoc_graph/migrations/NNNN_*.sql).
-- JSON-typed columns (source_paths, span, frontmatter, evidence, member_ids)
-- are TEXT holding canonical JSON; the adapter owns (de)serialization.

-- One row per documented codebase. Multi-repo/multi-tenant scoping lives here.
CREATE TABLE corpus (
  id TEXT PRIMARY KEY,
  repo_ref TEXT,
  branch TEXT,
  last_indexed_sha TEXT,
  indexed_at TEXT
);

CREATE TABLE entity (
  id TEXT PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES corpus(id),
  tier TEXT NOT NULL,             -- 'symbol' | 'concept'
  kind TEXT NOT NULL,             -- symbol: module|function|class|method|endpoint|component|type
                                  -- concept: the 12 taxonomy types
  name TEXT NOT NULL,
  qualified_name TEXT,
  source_paths TEXT,              -- JSON array
  span TEXT,                      -- JSON {file,start_line,end_line}; NULL for concept
  summary TEXT,
  body TEXT,
  frontmatter TEXT,               -- JSON
  community_id INTEGER,           -- assigned by epic 703
  origin TEXT NOT NULL,           -- 'ast' | 'agent'
  first_seen_sha TEXT,
  last_seen_sha TEXT,             -- stale => deletion CANDIDATE, never auto-deleted
  updated_at TEXT
);

CREATE TABLE edge (
  id INTEGER PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES corpus(id),
  src_id TEXT NOT NULL REFERENCES entity(id),
  dst_id TEXT NOT NULL REFERENCES entity(id),
  rel TEXT NOT NULL,              -- closed vocabulary (autodoc_graph.vocab.RELS)
  origin TEXT NOT NULL,           -- 'ast' | 'agent' | 'dreamed'
  confidence REAL NOT NULL,       -- ast=1.0 | agent=0.7 default | dreamed<=0.4
  evidence TEXT,                  -- JSON {file,line} for ast; rationale for agent/dreamed
  last_seen_sha TEXT,
  UNIQUE(corpus_id, src_id, dst_id, rel)
);

CREATE TABLE community (
  id INTEGER PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES corpus(id),
  level INTEGER NOT NULL,         -- 0 = leaf; higher = more abstract
  parent_id INTEGER,
  label TEXT,
  report TEXT,
  member_ids TEXT,                -- JSON array of entity ids
  summarized_at_sha TEXT
);

CREATE TABLE embedding (
  entity_id TEXT PRIMARY KEY REFERENCES entity(id),
  vec BLOB NOT NULL,              -- 768-dim float32 (little-endian)
  model TEXT NOT NULL,            -- 'nomic-embed-text' (fixed)
  embedded_at_sha TEXT
);

-- Traversal + scoping indexes (additive; the spec schema defines the shapes,
-- these only serve neighbors()/find_entities()/stats()).
CREATE INDEX idx_entity_corpus_tier ON entity(corpus_id, tier);
CREATE INDEX idx_entity_corpus_kind ON entity(corpus_id, kind);
CREATE INDEX idx_entity_corpus_name ON entity(corpus_id, name);
CREATE INDEX idx_edge_src ON edge(corpus_id, src_id);
CREATE INDEX idx_edge_dst ON edge(corpus_id, dst_id);
CREATE INDEX idx_edge_rel ON edge(corpus_id, rel);
CREATE INDEX idx_community_corpus_level ON community(corpus_id, level);
