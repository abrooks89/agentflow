-- E-0200 item 4b: the corpus row is the "room", and a room points at a project.
-- The renderer stamps `project` onto every page from this column, the way it
-- already stamps `source_repo` from `repo_ref` — so a page's project is a
-- property of the corpus it was built from, never something an agent decides
-- per page or the mirror infers from a path.
--
-- Additive and NULL for every corpus that predates it. NULL is not "no project":
-- it is "this room was never told", and the renderer refuses to render on it
-- rather than emitting pages that cannot be filed into a per-project pillar.
ALTER TABLE corpus ADD COLUMN project TEXT;
