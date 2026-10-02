"""Agent-return record writer — the concept tier's single write path (epic 704).

Agents never touch storage and never mint identity. They return RECORDS —
plain data describing pages, community reports, meta pages, and validated
diagrams — and this module turns them into store rows through the 702
adapter:

* concept entities  -> ``upsert_entities`` (tier='concept', origin='agent',
  id derived here as ``con:<corpus>:<type>:<slug(title)>``)
* ``documents`` joins -> ``upsert_edges`` (concept→symbol, origin='agent',
  confidence defaulted by the backend to 0.7); targets are resolved from the
  agent's ``(file, kind, name)`` citation to the extractor's
  ``sym:<corpus>:<path>:<kind>:<name>`` identity — an unresolvable citation
  is a hard error naming the record, never a silent drop
* concept relations -> ``upsert_edges`` (``depends_on``/``part_of``/
  ``related`` only; anything else is a hard error naming the record)
* community reports -> ``upsert_communities`` with ``summarized_at_sha``
  stamped (partial-update semantics keep everything else intact)

Validation is all-or-nothing: every record is validated BEFORE the first
write, so a bad record leaves the store untouched (the adapter's own batch
rollback covers write-time failures).

The record shapes accepted here are the same shapes the six prompt modules
(``references/prompts/``) instruct agents to return — this module is the
enforcement side of that contract.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

from .adapter import GraphAdapter

AGENT_RELS = frozenset({"documents", "depends_on", "part_of", "related"})
CONCEPT_RELS = frozenset({"depends_on", "part_of", "related"})

# Types whose symbol-join rate is gated by --join-floor (spec success
# criterion 3); narrative types are reported but exempt.
JOIN_FLOOR_TYPES = ("component", "data-model", "endpoint", "job")

_PAGE_REQUIRED = ("file", "type", "title", "source_paths", "summary", "body", "frontmatter")
_SLUG_KEEP = re.compile(r"[^a-z0-9]+")

# Frontmatter keys must be plain identifiers: a key containing a newline or
# colon would break out of the rendered YAML block (adversarial finding 6).
_FM_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

# Basenames the deterministic renderer owns — a page record claiming one
# would be silently clobbered by the nav layer (adversarial finding 2).
_RESERVED_BASENAMES = frozenset({
    "_index.md", "index.md", "overview.md", "log.md", "hot.md", "CLAUDE.md",
    "Start Here.md", "Glossary.md",
})

# Filename-invalid characters per the taxonomy naming convention.
_BAD_FILENAME_CHARS = re.compile(r'[:\\#|^\[\]<>"*?]')


class RecordsError(RuntimeError):
    """A record violated the agent-return contract. Always names the record."""


def load_type_fields() -> dict[str, Any]:
    """The shared page contract (folders, required frontmatter, meta files)."""
    text = (resources.files("autodoc_graph") / "type_fields.json").read_text(encoding="utf-8")
    return json.loads(text)


def slugify(title: str) -> str:
    """Deterministic ASCII slug for concept identity. Lossy by design —
    collisions between DISTINCT titles are detected and rejected loudly."""
    slug = _SLUG_KEEP.sub("-", str(title).lower()).strip("-")
    return slug


def concept_id(corpus: str, type_: str, title: str) -> str:
    slug = slugify(title)
    if not slug:
        raise RecordsError(
            f"title {title!r} slugifies to nothing — cannot derive a concept id"
        )
    return f"con:{corpus}:{type_}:{slug}"


def symbol_id(corpus: str, path: str, kind: str, name: str) -> str:
    """Mirror of emit-graph.js entityId: path-and-name keyed, forward slashes."""
    norm = str(path).replace("\\", "/")
    return f"sym:{corpus}:{norm}:{kind}:{name}"


# ------------------------------------------------------------------ validation
def _require_str(rec_name: str, rec: dict, key: str) -> str:
    val = rec.get(key)
    if not isinstance(val, str) or not val.strip():
        raise RecordsError(f"record {rec_name!r}: required field {key!r} is missing or empty")
    return val


def _validate_page(rec: dict[str, Any], type_fields: dict[str, Any]) -> None:
    name = rec.get("title") or rec.get("file") or "<untitled>"
    if "id" in rec:
        raise RecordsError(
            f"record {name!r} carries its own id {rec['id']!r} — agents never mint "
            "entity ids; the writer derives con:<corpus>:<type>:<slug>"
        )
    for key in _PAGE_REQUIRED:
        if key == "frontmatter":
            if not isinstance(rec.get("frontmatter"), dict):
                raise RecordsError(f"record {name!r}: frontmatter must be an object")
            continue
        if key == "source_paths":
            sp = rec.get("source_paths")
            # Tours teach existing pages and have no source of their own —
            # the taxonomy/graph-check exemption, mirrored at write time.
            may_be_empty = rec.get("type") == "tour"
            if not isinstance(sp, list) or (not sp and not may_be_empty) or not all(
                isinstance(p, str) and p.strip() for p in sp
            ):
                raise RecordsError(f"record {name!r}: source_paths must be a non-empty list")
            continue
        _require_str(name, rec, key)
    type_ = rec["type"]
    spec = type_fields["types"].get(type_)
    if spec is None:
        raise RecordsError(
            f"record {name!r}: unknown type {type_!r} "
            f"(taxonomy types: {sorted(type_fields['types'])})"
        )
    file = rec["file"].replace("\\", "/")
    parts = file.split("/")
    folder = parts[0] if len(parts) > 1 else ""
    if folder != spec["folder"] or not file.endswith(".md"):
        raise RecordsError(
            f"record {name!r}: file {rec['file']!r} does not live in the "
            f"{spec['folder']}/ folder its type {type_!r} requires"
        )
    # Exactly <folder>/<Name>.md — deeper nesting has no place in the tree
    # and is how a hostile record escapes the folder contract (finding 2).
    if len(parts) != 2:
        raise RecordsError(
            f"record {name!r}: file {rec['file']!r} must be exactly "
            f"{spec['folder']}/<Name>.md — no deeper nesting"
        )
    basename = parts[1]
    if basename in _RESERVED_BASENAMES:
        raise RecordsError(
            f"record {name!r}: file {rec['file']!r} claims the reserved nav "
            f"basename {basename!r} — the renderer owns nav pages"
        )
    stem = basename.removesuffix(".md")
    if (not stem or _BAD_FILENAME_CHARS.search(stem)
            or any(ord(c) < 0x20 for c in stem)
            or stem != stem.strip(" .")):
        raise RecordsError(
            f"record {name!r}: file {rec['file']!r} has an invalid page "
            "filename (empty, control/invalid characters, or edge dots/spaces)"
        )
    fm = rec["frontmatter"]
    for key in fm:
        if not isinstance(key, str) or not _FM_KEY_RE.match(key):
            raise RecordsError(
                f"record {name!r}: frontmatter key {key!r} is not a plain "
                "identifier — reserved (_-prefixed) and structured keys are "
                "writer-owned, and a key with a newline or colon would break "
                "out of the rendered YAML block"
            )
    if "drop_diagram" in rec and not isinstance(rec["drop_diagram"], bool):
        raise RecordsError(
            f"record {name!r}: drop_diagram must be true or false, got "
            f"{rec['drop_diagram']!r}"
        )
    missing = [f for f in spec["required"] if _empty(fm.get(f))]
    if missing:
        raise RecordsError(
            f"record {name!r}: frontmatter missing required {type_} field(s): "
            f"{', '.join(missing)}"
        )
    if type_ == "endpoint" and _empty(fm.get("route")) and _empty(fm.get("routes")):
        raise RecordsError(
            f"record {name!r}: endpoint frontmatter needs 'route' (or 'routes' "
            "for a route family)"
        )
    for entry in rec.get("documents") or []:
        for key in ("file", "kind", "name"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise RecordsError(
                    f"record {name!r}: documents entry {entry!r} needs file/kind/name"
                )
    for edge in rec.get("edges") or []:
        rel = edge.get("rel")
        if rel == "documents":
            raise RecordsError(
                f"record {name!r}: 'documents' edges are emitted via the documents "
                "field (concept→symbol citations), not the edges list"
            )
        if rel not in CONCEPT_RELS:
            raise RecordsError(
                f"record {name!r}: relation {rel!r} is outside the closed agent "
                f"vocabulary {sorted(AGENT_RELS)}"
            )
        for key in ("dst_type", "dst_title"):
            if not isinstance(edge.get(key), str) or not edge[key].strip():
                raise RecordsError(f"record {name!r}: edge {edge!r} needs {key}")


def _empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def _validate_meta(rec: dict[str, Any], type_fields: dict[str, Any]) -> None:
    name = rec.get("title") or rec.get("file") or "<untitled meta>"
    for key in ("file", "title", "body"):
        _require_str(name, rec, key)
    if "drop_diagram" in rec:
        # meta pages keep a stored diagram on re-write; there is no drop path for them
        raise RecordsError(f"meta record {name!r}: drop_diagram is not supported on meta records")
    if rec["file"] not in type_fields["meta_files"]:
        raise RecordsError(
            f"meta record {name!r}: file {rec['file']!r} is not an allowed meta page "
            f"({', '.join(type_fields['meta_files'])})"
        )


def _community_row(adapter: GraphAdapter, community_id: int) -> dict[str, Any] | None:
    level = 0
    while True:
        rows = adapter.get_communities(level)
        if not rows:
            return None
        for row in rows:
            if row["id"] == community_id:
                return row
        level += 1


def _validate_report(adapter: GraphAdapter, rep: dict[str, Any]) -> dict[str, Any]:
    cid = rep.get("community_id")
    if not isinstance(cid, int):
        raise RecordsError(f"report {rep!r}: community_id must be an integer")
    row = _community_row(adapter, cid)
    if row is None:
        raise RecordsError(f"report names unknown community {cid}")
    if (row.get("descriptor") or {}).get("dissolved"):
        raise RecordsError(
            f"report names community {cid} ({row.get('label')!r}) which is dissolved — "
            "reports attach to the current hierarchy only"
        )
    summary = rep.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise RecordsError(f"report for community {cid}: summary is missing or empty")
    findings = rep.get("findings")
    if not isinstance(findings, list) or not findings:
        # The report shape is "a summary plus RANKED FINDINGS" (spec scope 3);
        # an empty findings list is a hollow report, not a valid one.
        raise RecordsError(
            f"report for community {cid}: findings must be a non-empty list "
            "(a report is a summary plus ranked findings)"
        )
    for f in findings:
        if not isinstance(f, dict) or not str(f.get("text", "")).strip():
            raise RecordsError(f"report for community {cid}: finding {f!r} needs text")
    return row


# ------------------------------------------------------------------ input
def load_records_input(
    path: Path | str, manifest: list[str] | None = None
) -> dict[str, Any]:
    """A records payload from a FILE or a DIRECTORY of per-agent files.

    Directory mode is the disk-based workflow contract (adversarial finding
    5): each agent writes its own ``<runDir>/records/<label>.json``; this
    merges every ``*.json`` (sorted, deterministic) by concatenating the
    ``pages``/``reports``/``meta_pages``/``diagrams`` lists. Coverage keys
    (``covered``/``excluded``) are ignored here — see
    :func:`collect_coverage`.

    Directory mode also returns ``page_units``, a list parallel to ``pages``
    naming the unit that emitted each page: the file stem with a ``refresh-``
    or ``map-`` prefix stripped (``refresh-unit-05.json`` -> ``unit-05``). The
    file name is the only place a unit id exists, so it is captured here or
    lost; :func:`resolve_duplicate_claims` needs it (gh#455). Single-file mode
    carries no provenance.

    ``manifest`` (re-review item 1) is the authoritative file list from the
    workflow return (``records.files``): in directory mode, any ``*.json``
    present on disk but NOT in the manifest is a loud error (an orphan from
    a prior attempt at the same runDir would otherwise be swept in), and a
    manifest entry missing from disk is equally loud.
    """
    p = Path(path)
    if p.is_file():
        if manifest is not None:
            raise RecordsError(
                "--manifest applies to directory ingestion; got a single records file"
            )
        with p.open(encoding="utf-8") as fh:
            return json.load(fh)
    if not p.is_dir():
        raise RecordsError(f"records input {p} is neither a file nor a directory")
    files = sorted(p.glob("*.json"))
    if not files:
        raise RecordsError(f"records directory {p} contains no *.json files")
    if manifest is not None:
        # Compare by BASENAME: file names are label-derived and unique by
        # construction (map-<unit>.json, parent-<id>.json, …), and the
        # manifest may carry a different path style (Git-Bash /c/… vs
        # Windows C:\…) for the same run directory. A same-named file from a
        # prior attempt is not an orphan — the new run's agent overwrote it.
        listed = {Path(str(m).replace("\\", "/")).name for m in manifest}
        on_disk = {f.name for f in files}
        orphans = sorted(on_disk - listed)
        if orphans:
            raise RecordsError(
                f"records directory {p} contains file(s) NOT in the run manifest "
                f"(stale leftovers from a prior attempt?): {', '.join(orphans)} — "
                "clear the directory before dispatch or fix the manifest"
            )
        missing = sorted(listed - on_disk)
        if missing:
            raise RecordsError(
                f"run manifest names file(s) missing from {p}: {', '.join(missing)}"
            )
    merged: dict[str, list] = {"pages": [], "reports": [], "meta_pages": [], "diagrams": []}
    page_units: list[str] = []
    for f in files:
        with f.open(encoding="utf-8") as fh:
            try:
                part = json.load(fh)
            except json.JSONDecodeError as e:
                raise RecordsError(f"records file {f} is not valid JSON: {e}") from e
        if not isinstance(part, dict):
            raise RecordsError(f"records file {f} must contain a JSON object")
        for key in list(merged):
            val = part.get(key)
            if val is None:
                continue
            if not isinstance(val, list):
                raise RecordsError(f"records file {f}: {key} must be a list")
            merged[key].extend(val)
            if key == "pages":
                page_units.extend([_unit_key(f)] * len(val))
    merged["page_units"] = page_units
    return merged


def _unit_key(f: Path) -> str:
    # exactly one known prefix: the first match returns, so a stem such as
    # "refresh-map-unit-01" keeps its inner "map-" rather than being peeled twice
    stem = f.stem
    for prefix in ("refresh-", "map-"):
        if stem.startswith(prefix):
            return stem[len(prefix):]
    return stem


def _page_key(file: str) -> str:
    # the same normalisation the duplicate-path check applies
    return file.replace("\\", "/").casefold()


def load_assignment(path: Path | str) -> dict[str, str]:
    """``{page_key: unit_key}`` from the changed-communities artifact
    (``build-changed-communities.py``): a list of ``{key, affectedPages, ...}``.

    Only PLAIN entries assign ownership. Step 3.6 appends ANNOTATED entries
    (``<path> — consumer of changed contract — ...``, ``<path> — contract pair
    — ...``): re-verify fan-out, which may name one page under several units,
    so they are skipped. A plain page under two units is refused, because
    ``build-changed-communities`` asserts single ownership.

    Anything else is a loud error: a half-read assignment would silently turn
    assigned pages into "partitioner gaps" and keep the wrong record. An empty
    list is legal: Step 3 writes ``[]`` when the diff maps to no community,
    and the rolling audit can still emit records in that run.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise RecordsError(f"--assignment {path} is unreadable: {e}") from e
    if not isinstance(data, list):
        raise RecordsError(
            f"--assignment {path} must be a JSON list of "
            "{key, affectedPages} entries (the changed-communities artifact)"
        )
    owner: dict[str, str] = {}
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise RecordsError(f"--assignment {path}: entry {i} is not an object")
        key = entry.get("key")
        pages = entry.get("affectedPages")
        if not isinstance(key, str) or not key.strip():
            raise RecordsError(f"--assignment {path}: entry {i} has no string key")
        if not isinstance(pages, list) or not all(isinstance(x, str) for x in pages):
            raise RecordsError(
                f"--assignment {path}: entry {key!r} affectedPages must be a "
                "list of strings"
            )
        for page in pages:
            if len(page.split(" — ")) > 1:
                continue  # annotated fan-out entry: re-verify, not ownership
            pk = _page_key(page)
            if pk in owner and owner[pk] != key:
                raise RecordsError(
                    f"--assignment {path}: page {page!r} is assigned to both "
                    f"{owner[pk]!r} and {key!r}"
                )
            owner[pk] = key
    return owner


def _unit_sort_key(unit: str) -> tuple[Any, ...]:
    # unit-N first, by number (unit-100 after unit-11); any other key (the
    # "symbolless" catch-all) after every unit-N. The raw string breaks ties
    # such as unit-2 vs unit-02, so file order never decides.
    m = re.fullmatch(r"unit-(\d+)", unit)
    if m:
        return (0, int(m.group(1)), unit)
    # re.split with a capture group keeps str/int at fixed parities
    natural = [int(tok) if i % 2 else tok for i, tok in enumerate(re.split(r"(\d+)", unit))]
    return (1, natural, unit)


def resolve_duplicate_claims(
    pages: list[dict[str, Any]],
    page_units: list[str] | None,
    assignment: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve pages claimed by more than one unit (registry Q-10077).

    Rule 1: the unit the page was ASSIGNED to wins, even over a richer record.
    Rule 2: if no claimant was assigned it, the lowest unit id wins
    (deterministic; "richest" would need a judge and vary per run).
    Every collision is returned so the caller can warn: an unassigned claim is
    a partitioner gap, and a logged gap is countable across runs.

    Still hard errors: a collision without provenance (a batch with no
    collision needs none), and one unit claiming the same page twice (no
    tiebreak can say which of its own records it meant).
    Returns ``(kept_pages, collisions, dropped)``; ``dropped`` pairs each
    discarded record with its unit, so later resolution errors can name it.
    """
    groups: dict[str, list[int]] = {}
    for i, rec in enumerate(pages):
        groups.setdefault(_page_key(rec["file"]), []).append(i)
    if not any(len(idxs) > 1 for idxs in groups.values()):
        return list(pages), [], []
    if page_units is None or len(page_units) != len(pages):
        raise RecordsError(
            "duplicate-claim resolution needs per-page unit provenance "
            "(directory-mode records input); got none or a mismatched list"
        )
    drop: set[int] = set()
    collisions: list[dict[str, Any]] = []
    for pk, idxs in groups.items():
        if len(idxs) < 2:
            continue
        units = [page_units[i] for i in idxs]
        file = pages[idxs[0]]["file"]  # refined below to the KEPT record's spelling
        if len(set(units)) != len(units):
            twice = sorted({u for u in units if units.count(u) > 1}, key=_unit_sort_key)
            raise RecordsError(
                f"unit(s) {', '.join(twice)} claim the file {file!r} more than "
                "once — page paths must be unique within a unit"
            )
        claimants = sorted(units, key=_unit_sort_key)
        assigned = assignment.get(pk)
        kept = assigned if assigned in claimants else claimants[0]
        drop.update(i for i in idxs if page_units[i] != kept)
        file = next(pages[i]["file"] for i in idxs if page_units[i] == kept)
        collisions.append({
            "file": file,
            "claimants": claimants,
            "assigned": assigned,
            "kept": kept,
            "dropped": [u for u in claimants if u != kept],
        })
    collisions.sort(key=lambda c: _page_key(c["file"]))
    kept_pages = [rec for i, rec in enumerate(pages) if i not in drop]
    dropped = [(pages[i], page_units[i]) for i in sorted(drop)]
    return kept_pages, collisions, dropped


_DIAGRAM_HEADING_RE = re.compile(r"## Diagram[ \t]*")
_FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})")


def _section_headings(body: str) -> list[tuple[int, str]]:
    """(offset, line) of each ``## `` heading that is not inside a fenced block.

    A ``## Diagram`` line inside a code fence is example text, not a section:
    treating it as one would cut the fence around it (and delete fence content).
    """
    headings: list[tuple[int, str]] = []
    fence: str | None = None
    pos = 0
    for line in body.splitlines(keepends=True):
        text = line.rstrip("\n")
        m = _FENCE_RE.match(text)
        if fence is None:
            if m is not None:
                fence = m.group(1)
            elif text.startswith("## "):
                headings.append((pos, text))
        elif (m is not None and m.group(1)[0] == fence[0]
              and len(m.group(1)) >= len(fence) and not text[m.end():].strip()):
            fence = None
        pos += len(line)
    return headings


def diagram_section_spans(body: str | None) -> list[tuple[int, int]]:
    """[start, end) of every ``## Diagram`` section, each up to the next heading."""
    body = body or ""
    heads = _section_headings(body)
    spans = []
    for i, (start, text) in enumerate(heads):
        if _DIAGRAM_HEADING_RE.fullmatch(text):
            end = heads[i + 1][0] if i + 1 < len(heads) else len(body)
            spans.append((start, end))
    return spans


def first_section_offset(body: str) -> int | None:
    """Offset of the first ``## `` heading outside a fenced block, or None."""
    heads = _section_headings(body)
    return heads[0][0] if heads else None


def body_diagram_section(body: str | None) -> str | None:
    """The ``## Diagram`` section of a page body, up to the next ``## `` heading."""
    spans = diagram_section_spans(body)
    if not spans:
        return None
    start, end = spans[0]
    return body[start:end].rstrip("\n") + "\n\n"


def _splice_diagram_section(body: str, section: str) -> str:
    # same placement as render_pages._insert_diagram: before the first section
    at = first_section_offset(body)
    if at is None:
        return body.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n"
    return body[:at] + section + body[at:]


def diagram_carried_note(c: dict[str, Any]) -> str:
    """One stderr line per carried-forward diagram (gh#497): visible, not blocking."""
    if c.get("source") == "body":
        return (
            f"NOTE: page {c['file']!r} re-written without its '## Diagram' body "
            "section; kept the stored section"
        )
    sha = (c.get("diagram_sha") or "unknown")[:8]
    return (
        f"NOTE: page {c['file']!r} re-written without an embedded diagram; "
        f"kept its stored diagram (sha {sha})"
    )


def duplicate_claim_warning(c: dict[str, Any]) -> str:
    """One stderr line per resolved collision; the wording is the countable part."""
    if c["assigned"] in c["claimants"]:
        how = f"assigned to {c['assigned']}"
    elif c["assigned"] is None:
        how = "assigned to none (partitioner gap)"
    else:
        how = f"assigned to {c['assigned']}, which did not claim it (partitioner gap)"
    return (
        f"WARNING: page {c['file']!r} claimed by {', '.join(c['claimants'])}; "
        f"{how}; kept {c['kept']}; dropped {', '.join(c['dropped'])}"
    )


def collect_coverage(path: Path | str) -> dict[str, Any]:
    """Aggregate the coverage evidence from a records file/directory into the
    shape ``coverage.js`` consumes: ``{pages, covered, excluded}``."""
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.glob("*.json"))
    out: dict[str, list] = {"pages": [], "covered": [], "excluded": []}
    for f in files:
        with f.open(encoding="utf-8") as fh:
            part = json.load(fh)
        for rec in part.get("pages") or []:
            out["pages"].append({"source_paths": rec.get("source_paths") or []})
        out["covered"].extend(part.get("covered") or [])
        out["excluded"].extend(part.get("excluded") or [])
    return out


def _now_iso() -> str:
    # microseconds: the stamp is compared against entity.updated_at (also
    # microsecond-precision) — a seconds-truncated stamp would read OLDER
    # than a same-second entity write and false-stale every fresh report
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="microseconds")


def _artifact_parents(artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((artifact or {}).get("parents") or [])


# ------------------------------------------------------------------ write path
def write_records(
    adapter: GraphAdapter,
    records: dict[str, Any],
    *,
    sha: str,
    date: str,
    artifact: dict[str, Any] | None = None,
    assignment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate EVERY record, then persist concepts, edges, and reports.

    ``assignment`` (``{page_key: unit_key}``, see :func:`load_assignment`)
    turns a page claimed by several units from a refusal into a resolved
    collision (:func:`resolve_duplicate_claims`), listed in the report's
    ``duplicate_claims``. Without it a duplicate path is refused as before.

    ``artifact`` (the communities artifact) enables two report behaviors:
    parent reports are stamped with their ``children`` (drives staleness),
    and a SINGLE-CHILD parent with no explicit report INHERITS its child's
    report verbatim — a pure roll-up costs no agent (adversarial finding 1a).

    Returns ``{pages, meta_pages, entities, documents_edges, concept_edges,
    reports, inherited_reports}``. Any contract violation raises
    :class:`RecordsError` naming the offending record before anything is
    written.
    """
    if not sha or not date:
        raise RecordsError("write_records needs a non-empty sha and date")
    corpus = adapter.corpus_id
    if not corpus:
        raise RecordsError("adapter is not open on a corpus")
    type_fields = load_type_fields()
    pages: list[dict[str, Any]] = list(records.get("pages") or [])
    meta_pages: list[dict[str, Any]] = list(records.get("meta_pages") or [])
    reports: list[dict[str, Any]] = list(records.get("reports") or [])
    diagrams: list[dict[str, Any]] = list(records.get("diagrams") or [])

    for rec in pages:
        _validate_page(rec, type_fields)
    for rec in meta_pages:
        _validate_meta(rec, type_fields)

    collisions: list[dict[str, Any]] | None = None
    dropped_by_title: dict[str, tuple[str, str]] = {}
    if assignment is not None:
        pages, collisions, dropped = resolve_duplicate_claims(
            pages, records.get("page_units"), assignment
        )
        kept_titles = {rec["title"] for rec in pages}
        # a title a kept record also carries is not what the tiebreak removed
        dropped_by_title = {
            rec["title"]: (rec["file"], unit)
            for rec, unit in dropped
            if rec["title"] not in kept_titles
        }

    def _dropped_note(title: str) -> str:
        # names the tiebreak when a reference misses because of it; still fails
        if title not in dropped_by_title:
            return ""
        file, unit = dropped_by_title[title]
        return (f" (that title belonged to a claim of {file!r} by {unit} dropped "
                "by the duplicate-claim tiebreak)")

    # Duplicate file paths clobber silently at render time (finding 2):
    # reject within the batch (casefolded — the tree may land on a
    # case-insensitive filesystem)...
    files_seen: dict[str, str] = {}
    for rec in [*pages, *meta_pages]:
        key = rec["file"].replace("\\", "/").casefold()
        if key in files_seen:
            raise RecordsError(
                f"records {files_seen[key]!r} and {rec['title']!r} both claim "
                f"the file {rec['file']!r} — page paths must be unique"
            )
        files_seen[key] = rec["title"]

    # Identity derivation + batch collision detection (slugs are lossy).
    ids: dict[str, dict[str, Any]] = {}
    for rec in pages:
        eid = concept_id(corpus, rec["type"], rec["title"])
        if eid in ids:
            raise RecordsError(
                f"records {ids[eid]['title']!r} and {rec['title']!r} collide on "
                f"concept id {eid} — retitle one of them"
            )
        ids[eid] = rec
    for rec in meta_pages:
        eid = concept_id(corpus, "meta", rec["title"])
        if eid in ids:
            raise RecordsError(
                f"meta record {rec['title']!r} collides on concept id {eid}"
            )
        ids[eid] = rec

    # Store-side identity checks + created/diagram carry-forward, still before
    # writes. A stored diagram survives a re-write that omits it (gh#497): the
    # frontmatter is otherwise replaced wholesale and nothing downstream notices.
    created_by_id: dict[str, str] = {}
    stored_diagram_by_id: dict[str, dict[str, Any]] = {}
    # Older pages hold the diagram as a `## Diagram` section in the BODY, which the
    # `_diagram` carry cannot see (gh#465); carried the same way, into the new body.
    stored_body_section_by_id: dict[str, str] = {}
    for eid, rec in ids.items():
        existing = adapter.get_entity(eid)
        if existing is None:
            created_by_id[eid] = date
            continue
        if existing.get("name") != rec["title"]:
            raise RecordsError(
                f"record {rec['title']!r} collides with stored entity {eid} "
                f"titled {existing.get('name')!r} — slugs are lossy; retitle"
            )
        prev_fm = existing.get("frontmatter") or {}
        created_by_id[eid] = prev_fm.get("_created") or date
        if prev_fm.get("_diagram"):
            stored_diagram_by_id[eid] = {
                "_diagram": prev_fm["_diagram"],
                "_diagram_sha": prev_fm.get("_diagram_sha"),
            }
        # a stored _diagram replaces any body section at render time, so the body
        # section is carried (and reported) only for a page without one
        prev_section = body_diagram_section(existing.get("body"))
        if (prev_section is not None and not prev_fm.get("_diagram")
                and body_diagram_section(rec["body"]) is None):
            stored_body_section_by_id[eid] = prev_section

    # ...and against the store: a record claiming a file that a DIFFERENT
    # stored entity already renders to would clobber it (finding 2).
    stored_files: dict[str, str] = {}
    for ent in adapter.find_entities({"tier": "concept"}):
        qn = (ent.get("qualified_name") or "").replace("\\", "/").casefold()
        if qn:
            stored_files[qn] = ent["id"]
    for eid, rec in ids.items():
        owner = stored_files.get(rec["file"].replace("\\", "/").casefold())
        if owner is not None and owner != eid:
            raise RecordsError(
                f"record {rec['title']!r} claims file {rec['file']!r} which is "
                f"already rendered by stored entity {owner} — page paths must "
                "be unique"
            )

    # Attach validated diagrams to their target page records. Agents return
    # {type, title, embedded, mermaid, reason}: non-embedded entries are the
    # documented omission path (validate gate failed twice) and are skipped;
    # source_sha defaults to the run sha.
    page_by_key = {(rec["type"], rec["title"]): rec for rec in pages}
    # Diagram records key their target by PAGE type + title, but agents have been
    # observed echoing the MERMAID kind (sequenceDiagram / erDiagram / ...) into
    # `type` instead. Title alone identifies a page whenever it is unambiguous
    # across the batch, so fall back to it rather than failing a whole run on a
    # naming confusion the prompt used to invite. Ambiguous titles still hard-fail.
    _title_counts: dict[str, int] = {}
    for rec in pages:
        _title_counts[rec["title"]] = _title_counts.get(rec["title"], 0) + 1
    page_by_title = {
        rec["title"]: rec for rec in pages if _title_counts[rec["title"]] == 1
    }
    diagram_patch: dict[str, dict[str, str]] = {}
    diagram_matched: set[int] = set()  # id() of page records a diagram entry hit
    for d in diagrams:
        if "embedded" in d and not d["embedded"]:
            continue
        for key in ("type", "title", "mermaid"):
            if not isinstance(d.get(key), str) or not d[key].strip():
                raise RecordsError(f"diagram record {d!r} needs {key}")
        source_sha = d.get("source_sha") or sha
        target = page_by_key.get((d["type"], d["title"]))
        if target is None:
            target = page_by_title.get(d["title"])
        if target is None:
            eid = concept_id(corpus, d["type"], d["title"])
            if adapter.get_entity(eid) is None:
                raise RecordsError(
                    f"diagram targets unknown page {d['title']!r} (type {d['type']!r})"
                    + _dropped_note(d["title"])
                )
            diagram_patch[eid] = {"_diagram": d["mermaid"], "_diagram_sha": source_sha}
        else:
            if target.get("drop_diagram") is True:
                raise RecordsError(
                    f"record {target['title']!r} sets drop_diagram but the batch "
                    "also supplies a diagrams entry for it — drop it or replace "
                    "it, not both"
                )
            diagram_matched.add(id(target))
            target["frontmatter"]["_diagram"] = d["mermaid"]
            target["frontmatter"]["_diagram_sha"] = source_sha

    # Resolve documents citations and concept edges BEFORE any write.
    documents_rows: list[dict[str, Any]] = []
    for rec in pages:
        src = concept_id(corpus, rec["type"], rec["title"])
        for entry in rec.get("documents") or []:
            dst = symbol_id(corpus, entry["file"], entry["kind"], entry["name"])
            if adapter.get_entity(dst) is None:
                raise RecordsError(
                    f"record {rec['title']!r}: documents citation "
                    f"({entry['file']}, {entry['kind']}, {entry['name']}) resolves to "
                    f"no symbol entity ({dst}) — cite checklist symbols exactly"
                )
            documents_rows.append({
                "src_id": src, "dst_id": dst, "rel": "documents",
                "origin": "agent", "evidence": {"file": entry["file"]},
                "last_seen_sha": sha,
            })
    concept_rows: list[dict[str, Any]] = []
    for rec in pages:
        src = concept_id(corpus, rec["type"], rec["title"])
        for edge in rec.get("edges") or []:
            dst = concept_id(corpus, edge["dst_type"], edge["dst_title"])
            if dst not in ids and adapter.get_entity(dst) is None:
                raise RecordsError(
                    f"record {rec['title']!r}: {edge['rel']} target "
                    f"{edge['dst_title']!r} (type {edge['dst_type']!r}) resolves to "
                    "no concept entity in this batch or the store"
                    + _dropped_note(edge["dst_title"])
                )
            if dst == src:
                raise RecordsError(
                    f"record {rec['title']!r}: {edge['rel']} edge points at itself"
                )
            concept_rows.append({
                "src_id": src, "dst_id": dst, "rel": edge["rel"],
                "origin": "agent",
                "evidence": {"source": "agent-return"},
                "last_seen_sha": sha,
            })

    report_rows = [_validate_report(adapter, rep) for rep in reports]

    # Report payloads: summary + ranked findings, stamped with the write time
    # (drives content-staleness — finding 3) and, for parents, the child set.
    parents = _artifact_parents(artifact)
    children_of = {p["id"]: sorted(p.get("children") or []) for p in parents}
    now = _now_iso()
    payload_by_id: dict[int, dict[str, Any]] = {}
    report_writes: list[tuple[int, int, dict[str, Any]]] = []  # (id, level, payload)
    for rep, row in zip(reports, report_rows, strict=True):
        cid = rep["community_id"]
        payload = {
            "summary": rep["summary"].strip(),
            "findings": [
                {"text": str(f["text"]).strip(),
                 "entities": [str(e) for e in (f.get("entities") or [])]}
                for f in rep["findings"]
            ],
            "summarized_at": now,
        }
        if cid in children_of:
            payload["children"] = children_of[cid]
        payload_by_id[cid] = payload
        report_writes.append((cid, row["level"], payload))

    # Single-child parent chains COLLAPSE (finding 1a): a pure roll-up parent
    # inherits its only child's report verbatim — no agent was (or should
    # have been) spent on it. Parents arrive children-first, so chains of
    # single-child parents inherit transitively.
    inherited = 0
    for p in parents:
        kids = children_of[p["id"]]
        if len(kids) != 1 or p["id"] in payload_by_id:
            continue
        child = kids[0]
        source = payload_by_id.get(child)
        if source is None:
            child_row = _community_row(adapter, child)
            if child_row is None or not (child_row.get("report") or "").strip():
                raise RecordsError(
                    f"single-child parent {p['id']} ({p.get('label')!r}) cannot "
                    f"inherit: child {child} has no report in this batch or the "
                    "store"
                )
            source = json.loads(child_row["report"])
        parent_row = _community_row(adapter, p["id"])
        if parent_row is None:
            raise RecordsError(f"artifact names unknown parent community {p['id']}")
        payload = {
            "summary": source["summary"],
            "findings": source.get("findings") or [],
            "summarized_at": now,
            "children": kids,
            "inherited_from": child,
        }
        payload_by_id[p["id"]] = payload
        report_writes.append((p["id"], parent_row["level"], payload))
        inherited += 1

    # ---- writes (validation is complete; adapter batches are atomic) ----
    entity_rows = []
    diagrams_carried: list[dict[str, Any]] = []
    diagrams_dropped: list[str] = []
    for eid, rec in ids.items():
        is_meta = rec in meta_pages
        fm = dict(rec.get("frontmatter") or {})
        fm["_created"] = created_by_id[eid]
        body = rec["body"]
        stored_diagram = stored_diagram_by_id.get(eid)
        stored_section = stored_body_section_by_id.get(eid)
        file = rec["file"].replace("\\", "/")
        # a diagrams entry for this page supersedes whatever was stored; a
        # diagram_patch lands after the write, so it is not a carry either
        covered = id(rec) in diagram_matched or eid in diagram_patch
        if (stored_diagram is not None or stored_section is not None) and not covered:
            if rec.get("drop_diagram") is True:
                diagrams_dropped.append(file)
            else:
                if stored_diagram is not None:
                    fm.update(stored_diagram)
                    diagrams_carried.append(
                        {"file": file, "diagram_sha": stored_diagram["_diagram_sha"]}
                    )
                if stored_section is not None:
                    body = _splice_diagram_section(body, stored_section)
                    diagrams_carried.append(
                        {"file": file, "diagram_sha": None, "source": "body"}
                    )
        entity_rows.append({
            "id": eid,
            "tier": "concept",
            "kind": "meta" if is_meta else rec["type"],
            "name": rec["title"],
            "qualified_name": rec["file"].replace("\\", "/"),
            "source_paths": rec.get("source_paths") or [],
            "span": None,
            "summary": rec.get("summary"),
            "body": body,
            "frontmatter": fm,
            "origin": "agent",
            "last_seen_sha": sha,
        })
    entity_counts = adapter.upsert_entities(entity_rows)

    for eid, patch in diagram_patch.items():
        stored = adapter.get_entity(eid)
        fm = dict(stored.get("frontmatter") or {})
        fm.update(patch)
        adapter.upsert_entities([{**stored, "frontmatter": fm}])

    if documents_rows:
        adapter.upsert_edges(documents_rows)
    if concept_rows:
        adapter.upsert_edges(concept_rows)

    for cid, level, payload in report_writes:
        adapter.upsert_communities([{
            "id": cid,
            "level": level,
            "report": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "summarized_at_sha": sha,
        }])

    report = {
        "pages": len(pages),
        "meta_pages": len(meta_pages),
        "entities": entity_counts,
        "documents_edges": len(documents_rows),
        "concept_edges": len(concept_rows),
        "reports": len(report_writes),
        "inherited_reports": inherited,
        "diagrams_carried": diagrams_carried,
        "diagrams_dropped": diagrams_dropped,
    }
    if collisions is not None:
        report["duplicate_claims"] = collisions
    return report


# ------------------------------------------------------------------ join rate
def join_rates(adapter: GraphAdapter) -> dict[str, dict[str, Any]]:
    """Per concept type: how many entities carry >=1 symbol-granularity
    ``documents`` edge. The number the spec gates on (criterion 3)."""
    joined_src = {e["src_id"] for e in adapter.find_edges({"rel": "documents"})}
    rates: dict[str, dict[str, Any]] = {}
    for ent in adapter.find_entities({"tier": "concept"}):
        kind = ent["kind"]
        if kind == "meta":
            continue
        bucket = rates.setdefault(kind, {"total": 0, "joined": 0, "rate": 0.0})
        bucket["total"] += 1
        if ent["id"] in joined_src:
            bucket["joined"] += 1
    for bucket in rates.values():
        bucket["rate"] = bucket["joined"] / bucket["total"] if bucket["total"] else 0.0
    return rates


def enforce_join_floor(
    rates: dict[str, dict[str, Any]],
    floor: float,
    types: tuple[str, ...] = JOIN_FLOOR_TYPES,
) -> None:
    """Fail loudly when a gated type's symbol-join rate is under the floor."""
    failing = [
        f"{t}: {rates[t]['joined']}/{rates[t]['total']} = {rates[t]['rate']:.0%}"
        for t in types
        if t in rates and rates[t]["total"] > 0 and rates[t]["rate"] < floor
    ]
    if failing:
        raise RecordsError(
            f"symbol-join rate under the {floor:.0%} floor for: {'; '.join(failing)}"
        )


# ------------------------------------------------------------------ staleness
def _report_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    text = (row.get("report") or "").strip()
    if not text or not row.get("summarized_at_sha"):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None  # unparseable report == no report: stale


def _members_moved_since(
    adapter: GraphAdapter, row: dict[str, Any], stamp: str
) -> bool:
    """True when any member entity's CONTENT changed after the report was
    written (updated_at moves on content change only — sha re-stamps never
    touch it). Membership-only churn misses content-only commits
    (adversarial finding 3); this closes that hole."""
    try:
        stamp_dt = _dt.datetime.fromisoformat(stamp)
    except ValueError:
        return True  # unparseable stamp: treat as stale, never as fresh
    for member in row.get("member_ids") or []:
        ent = adapter.get_entity(member)
        if ent is None:
            return True  # a member vanished (pruned) — the report lies
        updated = ent.get("updated_at")
        if not updated:
            return True
        try:
            if _dt.datetime.fromisoformat(updated) > stamp_dt:
                return True
        except ValueError:
            return True
    return False


def stale_report_communities(
    adapter: GraphAdapter,
    churn: list[dict[str, Any]],
    artifact: dict[str, Any],
) -> list[int]:
    """Which artifact communities need (re)summarization.

    A LEAF (unit community) is stale when 703's churn says it was created or
    its membership changed, when it has no stored report, or when any member
    entity's content (``updated_at``) moved after the report was written —
    content-only commits re-summarize (spec scope 3; adversarial finding 3).

    A PARENT is stale when it has no report, its child set changed since its
    report was stamped, any child is dissolved, or any child is itself stale
    (children-first propagation). An unchanged corpus yields ZERO
    (spec criterion 10)."""
    moved: set[int] = set()
    for level in churn or []:
        moved.update(int(c) for c in level.get("created") or [])
        moved.update(int(c["id"]) for c in level.get("changed") or [])

    leaf_ids = sorted({
        c["id"] for u in (artifact.get("units") or []) for c in u["communities"]
    })
    parents = _artifact_parents(artifact)

    stale: set[int] = set()
    for cid in leaf_ids:
        row = _community_row(adapter, cid)
        if row is None:
            raise RecordsError(f"stale-report check names unknown community {cid}")
        payload = _report_payload(row)
        if cid in moved or payload is None:
            stale.add(cid)
            continue
        stamp = payload.get("summarized_at")
        if not stamp or _members_moved_since(adapter, row, stamp):
            stale.add(cid)

    # parents arrive children-first, so child staleness propagates upward
    for p in parents:
        pid = p["id"]
        row = _community_row(adapter, pid)
        if row is None:
            raise RecordsError(f"stale-report check names unknown parent {pid}")
        payload = _report_payload(row)
        kids = sorted(p.get("children") or [])
        if pid in moved or payload is None:
            stale.add(pid)
            continue
        if payload.get("children") != kids:
            stale.add(pid)  # child set changed since the report was written
            continue
        child_stale = False
        for child in kids:
            child_row = _community_row(adapter, child)
            if child_row is None or (child_row.get("descriptor") or {}).get("dissolved"):
                child_stale = True  # a dissolved child stales its parent
                break
            if child in stale:
                child_stale = True
                break
        if child_stale:
            stale.add(pid)
    return sorted(stale)


def require_reports(adapter: GraphAdapter, community_ids: list[int]) -> None:
    """Criterion 4: every fan-out community must end the run with a non-empty
    report and a set ``summarized_at_sha`` — fail naming the community."""
    for cid in community_ids:
        row = _community_row(adapter, cid)
        if row is None:
            raise RecordsError(f"require_reports: unknown community {cid}")
        if not (row.get("report") or "").strip():
            raise RecordsError(
                f"community {cid} ({row.get('label')!r}) has an EMPTY report after "
                "the run — every fan-out community must be summarized"
            )
        if not row.get("summarized_at_sha"):
            raise RecordsError(
                f"community {cid} ({row.get('label')!r}) has a report but no "
                "summarized_at_sha stamp"
            )
