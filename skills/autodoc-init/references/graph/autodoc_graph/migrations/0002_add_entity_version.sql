-- Embedding staleness must key on CONTENT change, not on last_seen_sha (which
-- advances on every build even for unchanged entities — re-embedding the whole
-- corpus every run). entity_version stores the entity's updated_at at embed
-- time; an embedding is stale when it differs from the entity's current
-- updated_at. embedded_at_sha keeps its spec meaning (the run sha at embed
-- time). Additive column via the migration mechanism (adversarial-review
-- remediation, finding 6).
ALTER TABLE embedding ADD COLUMN entity_version TEXT;

-- Backfill for upgraded v1 databases (finding R2-4): existing embeddings are
-- assumed current for their entity's present content — without this, every
-- stored embedding reads as stale (entity_version NULL) and the first run
-- after the upgrade re-embeds the whole corpus. Fresh databases have no
-- embedding rows yet, so this is a no-op there.
UPDATE embedding SET entity_version =
  (SELECT updated_at FROM entity WHERE entity.id = embedding.entity_id);
