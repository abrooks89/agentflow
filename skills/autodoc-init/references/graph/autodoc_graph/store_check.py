"""Store-mode integrity checks — graph-check's audit dimensions as queries
(epic 704, scope item 5).

Where the markdown-mode ``graph-check.js`` audits generated files after the
fact, these checks run over the store the renderer reads, so most of the old
audit surface is either true by construction (a rendered wikilink came from a
resolved row) or answerable as a query:

* orphan     -> a concept entity with no inbound or outbound agent edge
* coverage   -> symbol-tier entities with no inbound ``documents`` edge
* wikilinks  -> no store analogue BY CONSTRUCTION; the replacement is the
  integrity assertion that every edge endpoint resolves (also enforced at
  write time by the backend — this catches out-of-band corruption)
* frontmatter -> required per-type fields checked on the stored record

Adapter-interface only (portable to the 706 backend). Blockers fail the CLI;
warnings are reported, never fatal.
"""

from __future__ import annotations

from typing import Any

from .adapter import GraphAdapter
from .records import AGENT_RELS, load_type_fields


def check_store(adapter: GraphAdapter) -> dict[str, Any]:
    """Run every store check. Returns ``{blockers, warnings, summary, ...}``."""
    type_fields = load_type_fields()
    concepts = [e for e in adapter.find_entities({"tier": "concept"})]
    symbols = adapter.find_entities({"tier": "symbol"})
    by_id = {e["id"] for e in concepts} | {e["id"] for e in symbols}

    agent_edges: list[dict[str, Any]] = []
    for rel in sorted(AGENT_RELS):
        agent_edges.extend(adapter.find_edges({"rel": rel}))
    all_edges = adapter.find_edges({})

    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    # Integrity: every edge endpoint resolves (write-time enforced; re-assert).
    for edge in all_edges:
        for end in ("src_id", "dst_id"):
            if edge[end] not in by_id:
                blockers.append({
                    "kind": "danglingEdge",
                    "detail": f"edge {edge['src_id']} -{edge['rel']}-> "
                              f"{edge['dst_id']}: {end} does not resolve",
                })

    # Orphans: concept with no agent edge in either direction.
    touched: set[str] = set()
    for edge in agent_edges:
        if edge.get("origin") in ("agent", "dreamed"):
            touched.add(edge["src_id"])
            touched.add(edge["dst_id"])
    for ent in concepts:
        if ent["kind"] == "meta":
            continue
        if ent["id"] not in touched:
            warnings.append({
                "kind": "orphanConcept",
                "detail": f"{ent['qualified_name']} ({ent['id']}) has no agent edge "
                          "in or out",
            })

    # Coverage: symbols with no inbound documents edge.
    documented = {
        e["dst_id"] for e in agent_edges if e["rel"] == "documents"
    }
    uncovered = [s for s in symbols if s["id"] not in documented]
    warnings.extend(
        {"kind": "uncoveredSymbol", "detail": s["id"]} for s in uncovered
    )

    # Duplicate rendered paths: two concepts sharing a qualified_name would
    # last-writer-win at render time (adversarial finding 2) — blocker.
    by_file: dict[str, list[str]] = {}
    for ent in concepts:
        qn = (ent.get("qualified_name") or "").replace("\\", "/").casefold()
        if qn:
            by_file.setdefault(qn, []).append(ent["id"])
    for qn, owners in sorted(by_file.items()):
        if len(owners) > 1:
            blockers.append({
                "kind": "duplicateFile",
                "detail": f"{qn} rendered by {len(owners)} entities: "
                          f"{', '.join(sorted(owners))}",
            })

    # Concept record contract: body, summary, _created, folder, required fields.
    folder_of = {t: spec["folder"] for t, spec in type_fields["types"].items()}
    for ent in concepts:
        page = ent.get("qualified_name") or ent["id"]
        fm = ent.get("frontmatter") or {}
        if not (ent.get("body") or "").strip():
            blockers.append({"kind": "emptyBody", "detail": page})
        if not fm.get("_created"):
            blockers.append({"kind": "missingCreated", "detail": page})
        if ent["kind"] == "meta":
            continue
        if not (ent.get("summary") or "").strip():
            warnings.append({"kind": "missingSummary", "detail": page})
        folder = folder_of.get(ent["kind"])
        if folder is None:
            blockers.append({
                "kind": "unknownType", "detail": f"{page}: kind {ent['kind']!r}",
            })
            continue
        if not str(page).replace("\\", "/").startswith(folder + "/"):
            blockers.append({
                "kind": "folderMismatch",
                "detail": f"{page}: type {ent['kind']!r} belongs under {folder}/",
            })
        spec = type_fields["types"][ent["kind"]]
        missing = [
            f for f in spec["required"]
            if fm.get(f) in (None, "", []) or fm.get(f) == {}
        ]
        if ent["kind"] == "endpoint" and not fm.get("route") and not fm.get("routes"):
            missing.append("route|routes")
        if missing:
            blockers.append({
                "kind": "missingFrontmatter",
                "detail": f"{page}: {', '.join(missing)}",
            })

    summary = (
        f"{len(blockers)} blocker(s), {len(warnings)} warning(s) — "
        f"{len(concepts)} concept(s), {len(symbols)} symbol(s), "
        f"{len(uncovered)} symbol(s) without a documents edge"
    )
    return {
        "blockers": blockers,
        "warnings": warnings,
        "concepts": len(concepts),
        "symbols": len(symbols),
        "uncovered_symbols": len(uncovered),
        "summary": summary,
    }
