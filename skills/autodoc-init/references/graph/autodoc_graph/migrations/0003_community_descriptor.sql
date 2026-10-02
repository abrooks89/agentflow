-- Epic 703: store the deterministic label's evidence alongside the label.
-- descriptor is TEXT holding canonical JSON: {member_count, top_symbols,
-- top_prefixes} — written by the community detector, read by the fan-out
-- artifact emitter. Additive; NULL for rows that predate the detector.
ALTER TABLE community ADD COLUMN descriptor TEXT;
