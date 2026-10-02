"""Deterministic markdown renderer over the autodoc graph store (epic 704).

The store is primary; this module is the ONLY writer of the markdown tree.

* **Frontmatter** = ``entity.frontmatter`` plus universal fields stamped
  HERE — ``type``/``title``/``created``/``updated``/``source_repo``/
  ``source_paths`` (+ ``status``/``tags`` defaults). Agents are no longer
  responsible for date freshness.
* **Body** = ``entity.body`` — agent-authored prose. Body wikilinks that do
  not resolve against the store are DROPPED (unwrapped to plain text) and
  reported, never repaired.
* **``related:``/``depends_on:`` are rendered from edge rows** (agent
  origin only — ``dreamed`` edges are proposals and are never rendered, per
  the vault's never-auto-modify rule). Flow backlinks are the inverse
  traversal of a flow's ``related`` edges, rendered on the touched pages.
* **Diagrams** arrive pre-validated in the reserved ``_diagram`` frontmatter
  key; the renderer places the ``## Diagram`` section and stamps
  ``diagram: true`` / ``diagram_source_sha``.
* **Deterministic output**: entities in sorted-id order, fixed frontmatter
  key order, LF endings, whitelist-quoted YAML scalars. Rendering twice
  over an unchanged store is byte-identical.

The renderer writes into a target directory that must be EMPTY unless
``force=True`` (which wipes only ``*.md``) — replacing a live tree is an
explicit human-approved cutover, never a side effect.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .adapter import GraphAdapter
from .records import diagram_section_spans, first_section_offset, load_type_fields

# Targets and labels exclude '[' and newlines as well as ']'/'|': no legitimate
# wikilink spans a line break, and without that exclusion a stray '[[' in prose
# (documenting wikilink syntax, say) opens a match that runs forward to the next
# ']]' many lines later — _resolve_body then unwraps the whole span, deleting the
# prose between and stripping the brackets off the real link that ended it.
WIKILINK_RE = re.compile(r"\[\[([^\]\[|\n]+)(?:\|([^\]\[\n]+))?\]\]")

# Reserved frontmatter keys: writer/renderer plumbing, never emitted as YAML.
_RESERVED_PREFIX = "_"

# Defensive twin of records._FM_KEY_RE (adversarial finding 6): the records
# writer rejects non-identifier keys at intake, and the renderer refuses to
# emit one regardless — a key containing ':' or a newline would break out of
# the YAML block.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

# Marker file the renderer stamps into every tree it writes. After cutover it
# travels with the live tree and tells /autodoc-update to STOP: the tree is
# store-primary and hand-edits (or the not-yet-converted update path) would
# be silently undone by the next render (adversarial finding 4).
MARKER_FILE = ".rendered-by-store"

# Universal key order; everything else follows alphabetically.
_KEY_ORDER = ("type", "title", "aliases", "created", "updated", "project",
              "source_repo", "source_paths", "status", "tags")

# Frontmatter fields whose wikilink entries are machine-consumed (taxonomy
# "Cross-Linking Discipline"): every entry must resolve, so unresolvable ones
# are dropped and reported. `related`/`depends_on` are NOT here — they are
# edge-derived and agent-supplied values for them are discarded.
_RELATIONAL_LIST_FIELDS = (
    "calls_endpoints", "renders", "used_by", "consumed_by", "involves",
    "exposes_endpoints", "steps", "covers", "contract_with",
    "applies_to", "affects", "touches", "aliases_links",
)
_RELATIONAL_SCALAR_FIELDS = (
    "service", "owned_by", "runs", "request_model", "response_model",
    "entrypoint", "supersedes",
)
_EDGE_DERIVED_FIELDS = ("related", "depends_on")

# One-line type descriptions for the deterministic _index pages (taxonomy
# "What it is" column, abridged).
_TYPE_DESCRIPTIONS = {
    "endpoint": "One HTTP/WS route.",
    "page": "One frontend route or view.",
    "component": "A reusable UI or functional unit.",
    "service": "A deployable app, daemon, or library.",
    "process": "A named multi-step internal workflow.",
    "vendor": "An external or vendored dependency.",
    "runbook": "An operational procedure.",
    "data-model": "A schema, type, or table.",
    "decision": "An Architecture Decision Record.",
    "flow": "A cross-cutting runtime narrative.",
    "job": "Scheduled or triggered background work.",
    "tour": "A dependency-ordered onboarding reading sequence.",
}


class RenderError(RuntimeError):
    """Loud rendering failure — never a silently wrong tree."""


def _norm_key(s: str) -> str:
    return re.sub(r"[\s\-/_]+", " ", str(s).lower()).strip()


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def _yaml_scalar(value: Any, *, page: str, key: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if value is None:
        return '""'
    if not isinstance(value, str):
        raise RenderError(
            f"{page}: frontmatter {key!r} has unsupported value type "
            f"{type(value).__name__} (flat YAML only)"
        )
    if any(ord(c) < 0x20 for c in value):
        raise RenderError(
            f"{page}: frontmatter {key!r} contains control characters — "
            "frontmatter scalars are single-line"
        )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _emit_frontmatter(fm: dict[str, Any], page: str) -> str:
    keys = [k for k in _KEY_ORDER if k in fm]
    keys += sorted(k for k in fm if k not in _KEY_ORDER)
    lines = ["---"]
    for key in keys:
        if not isinstance(key, str) or not _SAFE_KEY_RE.match(key):
            raise RenderError(
                f"{page}: frontmatter key {key!r} is not a safe identifier — "
                "refusing to emit (YAML block injection)"
            )
        value = fm[key]
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(
                f"  - {_yaml_scalar(item, page=page, key=key)}" for item in value
            )
        elif isinstance(value, dict):
            raise RenderError(f"{page}: frontmatter {key!r} is nested — flat YAML only")
        else:
            lines.append(f"{key}: {_yaml_scalar(value, page=page, key=key)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _insert_diagram(body: str, mermaid: str) -> str:
    section = f"## Diagram\n\n```mermaid\n{mermaid.rstrip()}\n```\n\n"
    # A body may already carry a `## Diagram` section (older body-stored diagrams,
    # gh#465): strip every such section and put the stored one where the first was.
    spans = diagram_section_spans(body)
    if spans:
        rest, prev = [], spans[0][0]
        for start, end in spans:
            rest.append(body[prev:start])
            prev = end
        rest.append(body[prev:])
        tail = "".join(rest)
        if tail and spans[-1][1] == len(body):  # a trailing section went: no blank tail
            tail = tail.rstrip("\n") + "\n"
        if not tail:
            return body[:spans[0][0]] + section.rstrip("\n") + "\n"
        return body[:spans[0][0]] + section + tail
    at = first_section_offset(body)
    if at is None:
        return body.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n"
    return body[:at] + section + body[at:]


class _Renderer:
    def __init__(self, adapter: GraphAdapter, out_dir: Path) -> None:
        self.adapter = adapter
        self.out = Path(out_dir)
        self.type_fields = load_type_fields()
        corpus = adapter.get_corpus()
        # These corpus guards are an oracle, not just a check: every documented
        # `graph build` recipe must supply a flag for each field refused here, and
        # `cli.py::_require_attribution` must refuse the same set at build time.
        # <suite_root>/skills/_shared/tests/test_documented_graph_recipes.py derives all three
        # sides from source and fails if they diverge -- so adding a refusal below
        # widens that gate by itself. Write the guard as `if not corpus.get("field")`
        # or teach that module the new shape; an unrecognised shape is a finding
        # there rather than a silent hole.
        if not corpus.get("repo_ref"):
            raise RenderError(
                "corpus.repo_ref is not set — run `graph build --repo-ref` first; "
                "the renderer stamps source_repo from the store"
            )
        if not corpus.get("project"):
            raise RenderError(
                "corpus.project is not set — run `graph build --project <name>` first; "
                "the renderer stamps project from the store, and a page without one "
                "cannot be filed into a per-project vault pillar"
            )
        if not corpus.get("indexed_at"):
            raise RenderError("corpus.indexed_at is not set — ingest before rendering")
        self.repo_ref: str = corpus["repo_ref"]
        self.project: str = corpus["project"]
        self.nav_date: str = str(corpus["indexed_at"])[:10]
        self.entities = [
            e for e in adapter.find_entities({"tier": "concept"})
        ]
        self.pages = [e for e in self.entities if e["kind"] != "meta"]
        self.metas = [e for e in self.entities if e["kind"] == "meta"]
        self._build_link_index()
        self._build_edge_maps()
        self.dropped_body: list[dict[str, str]] = []
        self.dropped_fm: list[dict[str, str]] = []

    # -------------------------------------------------------------- indexes
    def _build_link_index(self) -> None:
        index: dict[str, str] = {}
        for e in self.entities:
            keys = {e["name"], _stem(e["qualified_name"] or "")}
            for alias in (e.get("frontmatter") or {}).get("aliases") or []:
                if isinstance(alias, str):
                    keys.add(alias)
            for key in keys:
                nk = _norm_key(key)
                if nk:
                    index.setdefault(nk, e["id"])
        self.link_index = index

    def _build_edge_maps(self) -> None:
        by_id = {e["id"]: e for e in self.entities}
        related_out: dict[str, set[str]] = {}
        depends_out: dict[str, set[str]] = {}
        flow_backlinks: dict[str, set[str]] = {}
        for rel, bucket in (("related", related_out), ("depends_on", depends_out)):
            for edge in self.adapter.find_edges({"rel": rel, "origin": "agent"}):
                src, dst = edge["src_id"], edge["dst_id"]
                if src in by_id and dst in by_id:
                    bucket.setdefault(src, set()).add(by_id[dst]["name"])
                    if rel == "related" and by_id[src]["kind"] == "flow":
                        # flow backlink: inverse traversal of the flow's edges
                        flow_backlinks.setdefault(dst, set()).add(by_id[src]["name"])
        self.related_out = related_out
        self.depends_out = depends_out
        self.flow_backlinks = flow_backlinks

    def _resolve(self, target: str) -> bool:
        name = target.split("#", 1)[0].strip()
        if not name:  # [[#heading]] — same-page anchor, always fine
            return True
        return _norm_key(name) in self.link_index

    # -------------------------------------------------------------- pieces
    def _resolve_body(self, page: str, body: str) -> str:
        def sub(match: re.Match) -> str:
            target = match.group(1).strip()
            label = (match.group(2) or "").strip()
            if self._resolve(target):
                return match.group(0)
            self.dropped_body.append({"page": page, "target": target})
            return label or target

        return WIKILINK_RE.sub(sub, body)

    def _filter_fm_links(self, page: str, fm: dict[str, Any]) -> dict[str, Any]:
        out = dict(fm)
        for field in _EDGE_DERIVED_FIELDS:
            if field in out:
                # edge-derived fields are rendered from rows, never trusted
                # from agent frontmatter
                self.dropped_fm.append({"page": page, "field": field,
                                        "target": "<agent-supplied; edge-derived>"})
                del out[field]
        for field in _RELATIONAL_LIST_FIELDS:
            if field not in out or not isinstance(out[field], list):
                continue
            kept = []
            for item in out[field]:
                target = _link_target(item)
                if target is None or self._resolve(target):
                    kept.append(item)
                else:
                    self.dropped_fm.append({"page": page, "field": field,
                                            "target": target})
            out[field] = kept
        for field in _RELATIONAL_SCALAR_FIELDS:
            if field not in out:
                continue
            target = _link_target(out[field])
            if target is not None and not self._resolve(target):
                self.dropped_fm.append({"page": page, "field": field,
                                        "target": target})
                out[field] = ""
        return out

    def _page_text(self, ent: dict[str, Any]) -> str:
        page = ent["qualified_name"]
        raw_fm = {k: v for k, v in (ent.get("frontmatter") or {}).items()}
        created = raw_fm.get("_created")
        if not created:
            raise RenderError(
                f"{page}: entity {ent['id']} has no _created stamp — it was not "
                "written through the records writer"
            )
        mermaid = raw_fm.get("_diagram")
        diagram_sha = raw_fm.get("_diagram_sha")
        fm = {k: v for k, v in raw_fm.items() if not k.startswith(_RESERVED_PREFIX)}
        fm = self._filter_fm_links(page, fm)
        fm["type"] = ent["kind"]
        fm["title"] = ent["name"]
        fm["created"] = created
        fm["updated"] = str(ent.get("updated_at") or "")[:10]
        fm["project"] = self.project
        fm["source_repo"] = self.repo_ref
        fm["source_paths"] = list(ent.get("source_paths") or [])
        fm.setdefault("status", "active")
        fm.setdefault("tags", [ent["kind"]])
        related = set(self.related_out.get(ent["id"], set()))
        related |= self.flow_backlinks.get(ent["id"], set())
        related.discard(ent["name"])
        if related:
            fm["related"] = [f"[[{t}]]" for t in sorted(related)]
        depends = self.depends_out.get(ent["id"], set()) - {ent["name"]}
        if depends:
            fm["depends_on"] = [f"[[{t}]]" for t in sorted(depends)]
        body = (ent.get("body") or "").replace("\r\n", "\n").replace("\r", "\n")
        body = self._resolve_body(page, body)
        if mermaid and ent["kind"] in ("flow", "data-model"):
            body = _insert_diagram(body, str(mermaid))
            fm["diagram"] = True
            fm["diagram_source_sha"] = str(diagram_sha or "")
        return _emit_frontmatter(fm, page) + "\n" + body.rstrip("\n") + "\n"

    def _meta_text(self, ent: dict[str, Any]) -> str:
        page = ent["qualified_name"]
        created = (ent.get("frontmatter") or {}).get("_created")
        if not created:
            raise RenderError(f"{page}: meta entity {ent['id']} has no _created stamp")
        fm = {
            "type": "meta",
            "title": ent["name"],
            "created": created,
            "updated": str(ent.get("updated_at") or "")[:10],
        }
        body = (ent.get("body") or "").replace("\r\n", "\n").replace("\r", "\n")
        body = self._resolve_body(page, body)
        return _emit_frontmatter(fm, page) + "\n" + body.rstrip("\n") + "\n"

    # -------------------------------------------------------------- nav layer
    def _nav_pages(self) -> dict[str, str]:
        by_folder: dict[str, list[dict[str, Any]]] = {}
        folder_of = {t: spec["folder"] for t, spec in self.type_fields["types"].items()}
        for ent in self.pages:
            folder = folder_of.get(ent["kind"])
            if folder:
                by_folder.setdefault(folder, []).append(ent)
        nav: dict[str, str] = {}
        type_of_folder = {spec["folder"]: t for t, spec in self.type_fields["types"].items()}
        for folder in sorted(by_folder):
            ents = sorted(by_folder[folder], key=lambda e: e["name"].lower())
            type_ = type_of_folder[folder]
            fm = {
                "type": "meta",
                "title": f"{folder.capitalize()} Index",
                "created": self.nav_date,
                "updated": self.nav_date,
            }
            lines = [_TYPE_DESCRIPTIONS.get(type_, ""), ""]
            for e in ents:
                summary = (e.get("summary") or "").strip()
                lines.append(f"- [[{e['name']}]]" + (f" — {summary}" if summary else ""))
            body = "\n".join(lines).strip() + "\n"
            nav[f"{folder}/_index.md"] = (
                _emit_frontmatter(fm, f"{folder}/_index.md") + "\n" + body
            )
        # master catalog — titled by CORPUS, never by the target directory
        # (the render target is a disposable sibling; its basename must not
        # leak into content or two renders into different dirs would differ)
        name = self.adapter.corpus_id
        fm = {"type": "meta", "title": f"{name} Index",
              "created": self.nav_date, "updated": self.nav_date}
        lines = []
        for spec in self.type_fields["types"].values():
            folder = spec["folder"]
            ents = by_folder.get(folder)
            if not ents:
                continue
            lines.append(f"## {folder} ({len(ents)})")
            lines.append("")
            lines.extend(
                f"- [[{e['name']}]]"
                for e in sorted(ents, key=lambda e: e["name"].lower())
            )
            lines.append("")
        nav["index.md"] = (
            _emit_frontmatter(fm, "index.md") + "\n" + "\n".join(lines).strip() + "\n"
        )
        return nav

    # -------------------------------------------------------------- rendering
    def render(self, force: bool) -> dict[str, Any]:
        self._prepare_out(force)
        # Store-primary marker (finding 4): deterministic content, travels
        # with the tree at cutover; /autodoc-update pre-flights on it.
        marker = self.out / MARKER_FILE
        with marker.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                f"corpus: {self.adapter.corpus_id}\n"
                "This tree is rendered deterministically from the autodoc graph "
                "store (epic 704).\n"
                "Do NOT hand-edit pages: the next `graph render` silently "
                "undoes any direct edit.\n"
                "To refresh it, run /autodoc-update: it detects this marker, "
                "lands its updates as records\n"
                "in the store and re-renders the tree (epic 706). Do not re-run "
                "/autodoc-init on it.\n"
            )
        written: list[str] = []
        for ent in sorted(self.pages, key=lambda e: e["id"]):
            written.append(self._write(ent["qualified_name"], self._page_text(ent)))
        for ent in sorted(self.metas, key=lambda e: e["id"]):
            written.append(self._write(ent["qualified_name"], self._meta_text(ent)))
        nav = self._nav_pages()
        for rel in sorted(nav):
            written.append(self._write(rel, nav[rel]))
        by_type: dict[str, int] = {}
        for ent in self.pages:
            by_type[ent["kind"]] = by_type.get(ent["kind"], 0) + 1
        return {
            "pages": len(self.pages),
            "meta_pages": len(self.metas),
            "nav_pages": len(nav),
            "files": written,
            "pages_by_type": dict(sorted(by_type.items())),
            "dropped_body_links": self.dropped_body,
            "dropped_frontmatter_links": self.dropped_fm,
        }

    def _prepare_out(self, force: bool) -> None:
        if self.out.exists():
            existing = sorted(p for p in self.out.rglob("*") if p.is_file())
            if existing and not force:
                raise RenderError(
                    f"render target {self.out} is not empty (found "
                    f"{existing[0].relative_to(self.out).as_posix()}) — the renderer "
                    "writes into an empty sibling directory; pass force=True only "
                    "for a directory the renderer owns"
                )
            if force:
                for p in existing:
                    if p.suffix == ".md":
                        p.unlink()
        else:
            self.out.mkdir(parents=True)

    def _write(self, rel: str, text: str) -> str:
        rel = rel.replace("\\", "/")
        if rel.startswith("/") or ".." in rel.split("/"):
            raise RenderError(f"refusing to write outside the target tree: {rel!r}")
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return rel


def _link_target(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = WIKILINK_RE.search(value)
    return match.group(1).strip() if match else None


def render_pages(
    adapter: GraphAdapter, out_dir: Path | str, *, force: bool = False
) -> dict[str, Any]:
    """Render the open corpus's concept tier into ``out_dir``. See module
    docstring for the contract. Returns a render report."""
    return _Renderer(adapter, Path(out_dir)).render(force)
