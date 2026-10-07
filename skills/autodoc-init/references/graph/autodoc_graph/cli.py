"""The `graph` CLI — operability surface for the autodoc graph (epics 702-704).

Subcommands:
  graph build       --jsonl G.jsonl --db graph.db --corpus ID --repo-ref R --project P
                    [--sha S] [--branch B] [--embed] [--ollama-url URL]
                    (--repo-ref/--project may be omitted only when the corpus already
                     carries them from an earlier build; see _require_attribution)
  graph stats       --db graph.db --corpus ID
  graph prune       --db graph.db --corpus ID [--dry-run | --delete]
  graph verify      --db graph.db --corpus ID
  graph communities --db graph.db --corpus ID [--seed N] [--band LO:HI]
                    [--min-members N] [--pin PREFIX=NAME ...] [--emit OUT.json]
  graph records     --db graph.db --corpus ID --records R.json --sha S --date D
                    [--artifact COMM.json] [--join-floor F]
  graph check-records --db graph.db --corpus ID --records R.json
                    (read-only: one agent's records file, checked as it is written)
  graph migrate-diagrams --db graph.db --corpus ID --validator validate_mermaid.py
                    [--dry-run]
  graph render      --db graph.db --corpus ID --out DIR [--force]
  graph check       --db graph.db --corpus ID [--out REPORT.json]
  graph reports-stale --db graph.db --corpus ID --artifact COMM.json

Only `build` creates a store. Every other subcommand opens an existing one and
fails (exit 1) on a --db path that is missing, 0 bytes, or not a graph store,
rather than creating one there and reporting an empty corpus (gh#459).

Deletion is NEVER automatic: `prune` defaults to dry-run and only an explicit
`--delete` removes rows. Embedding is opt-in (`--embed`) so a build works on
machines without Ollama; URL resolution is explicit `--ollama-url` >
AUTODOC_OLLAMA_URL > http://localhost:11434.

Every failure is loud: named exceptions print to stderr and exit 1; nothing
is swallowed. The extraction step is Node-side
(`skills/autodoc-init/references/inventory/emit-graph.js` produces the JSONL
this consumes); this CLI never shells out to it.
"""

from __future__ import annotations

import argparse
import json
import sys

from .adapter import AdapterError
from .communities import (
    DEFAULT_BAND,
    DEFAULT_JACCARD_THRESHOLD,
    DEFAULT_MIN_MEMBERS,
    DEFAULT_SEED,
    CommunityError,
    run_communities,
)
from .embedder import EmbedderError, OllamaEmbedder
from .ingest import IngestError, ingest_graph
from .migrate_diagrams import MigrateError, load_mermaid_validator, migrate_body_diagrams
from .naming import NamingError, OllamaNamer, rename_labels
from .records import (
    RecordsError,
    check_records,
    collect_coverage,
    diagram_carried_note,
    duplicate_claim_warning,
    enforce_join_floor,
    join_rates,
    load_assignment,
    load_records_input,
    require_reports,
    stale_report_communities,
    write_records,
)
from .render_pages import RenderError, render_pages
from .sqlite_backend import SqliteGraphAdapter
from .store_check import check_store
from .vocab import VocabError

_ERRORS = (AdapterError, VocabError, IngestError, EmbedderError, CommunityError,
           NamingError, RecordsError, RenderError, MigrateError, FileNotFoundError, OSError)


def _artifact_community_ids(path: str) -> list[int]:
    """Every community id a run must summarize: unit communities + parents."""
    with open(path, encoding="utf-8") as fh:
        artifact = json.load(fh)
    ids = [c["id"] for u in artifact.get("units", []) for c in u["communities"]]
    ids += [p["id"] for p in artifact.get("parents", [])]
    return sorted(set(ids))


# Corpus attribution the renderer refuses to stamp pages without, as
# build flag -> corpus column. Only `build` can set these, so a build that
# omits them produces a corpus that cannot render. Kept in step with
# render_pages' corpus guards by
# <suite_root>/skills/_shared/tests/test_documented_graph_recipes.py, which derives both
# sides rather than trusting either list.
_BUILD_ATTRIBUTION = (("--repo-ref", "repo_ref"), ("--project", "project"))


def _require_attribution(adapter: SqliteGraphAdapter, args: argparse.Namespace) -> None:
    """Refuse an unrenderable build HERE, where the remedy is the command just typed.

    Without this the omission surfaces at `graph render`, several steps downstream,
    telling the operator to re-run a step they already completed (gh#152).

    Not `required=True` on the parser: argparse prints a usage line and never the
    `help=` reason, so the operator would learn which flag is missing but not why it
    cannot be defaulted. And a rebuild of an already-attributed corpus legitimately
    needs no flags -- `update_corpus` skips None columns, so stored values survive a
    flagless rebuild. This permits that case deliberately instead of tripping over it.

    Runs after `adapter.open()` because those stored values are what makes the flagless
    rebuild legal and reading them needs an open adapter. `open()` creates the schema
    and an empty corpus row; nothing from the JSONL is ingested when this refuses.
    """
    stored = adapter.get_corpus()
    missing = [(flag, col) for flag, col in _BUILD_ATTRIBUTION
               if not getattr(args, flag[2:].replace("-", "_")) and not stored.get(col)]
    if not missing:
        return
    flags = " ".join(f"{flag} <value>" for flag, _col in missing)
    cols = ", ".join(f"corpus.{col}" for _flag, col in missing)
    verb = "is" if len(missing) == 1 else "are"
    raise IngestError(
        f"corpus {args.corpus!r} would be unrenderable: {cols} {verb} unset in the store "
        f"and this build supplies no value. Pass {flags} to THIS command. The renderer "
        "refuses a corpus without them (render_pages.py stamps source_repo and project "
        "from the store, and a page carrying no project cannot be filed into a per-project "
        "vault pillar), and neither is ever inferred -- a guessed project is worse than an "
        "absent one. Nothing was ingested."
    )


def _build(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus)  # the one subcommand that creates a store
    try:
        _require_attribution(adapter, args)
        report = ingest_graph(
            adapter, args.jsonl,
            sha=args.sha, corpus_id=args.corpus,
            repo_ref=args.repo_ref, branch=args.branch, project=args.project,
        )
        print(f"build: corpus={args.corpus} sha={report['sha']}")
        print(json.dumps({"entities": report["entities"], "edges": report["edges"]}, indent=2))
        if args.embed:
            embedder = OllamaEmbedder(url=args.ollama_url)
            embedded = embedder.embed_pending(adapter)
            print(f"embed: {embedded['embedded']} entities embedded via {embedder.url}")
        print(json.dumps(adapter.stats(), indent=2))
    finally:
        adapter.close()
    return 0


def _stats(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        print(json.dumps(adapter.stats(), indent=2))
    finally:
        adapter.close()
    return 0


def _prune(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        dry = not args.delete  # deletion is NEVER automatic
        result = adapter.prune(dry_run=dry)
        if dry:
            print(f"prune (dry-run): {len(result['candidates'])} deletion candidate(s); "
                  "nothing deleted. Re-run with --delete to remove them.")
        else:
            print(f"prune: deleted {result['deleted']} entity(ies) "
                  "(with their edges and embeddings)")
        for cid in result["candidates"]:
            print(f"  {cid}")
        # The backend names the anchored communities on both paths, but only a deletion
        # dissolves them: on a dry run the next `communities` run has nothing to mark.
        if result.get("communities"):
            if dry:
                print("communities anchored by deletion candidates (would dissolve if deleted "
                      "— nothing dissolves until `prune --delete` runs):")
            else:
                print("communities anchored by deleted entities (dissolving — the "
                      "next `graph communities` run marks them):")
            for comm_id in result["communities"]:
                print(f"  community {comm_id}")
    finally:
        adapter.close()
    return 0


def _verify(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        findings = adapter.verify()
    finally:
        adapter.close()
    if findings:
        print(f"verify: {len(findings)} integrity finding(s):", file=sys.stderr)
        for f in findings:
            print(f"  FAIL {f}", file=sys.stderr)
        return 1
    print("verify: OK — graph integrity checks passed")
    return 0


def _parse_band(text: str) -> tuple[int, int]:
    """SHAPE check only (LO:HI integers). Range validation is deliberately
    deferred to run_communities so the single floor-bearing error message
    (names --band, states the corpus's connected-component floor, prints the
    smallest satisfiable configuration) serves both the CLI and direct
    callers (re-review finding 3)."""
    try:
        lo, hi = (int(x) for x in text.split(":", 1))
    except ValueError as e:
        raise CommunityError(f"--band must be LO:HI (e.g. 8:24), got {text!r}") from e
    return lo, hi


def _parse_pins(pairs: list[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise CommunityError(
                f"--pin must be PREFIX=NAME, got {pair!r}"
            )
        prefix, name = pair.split("=", 1)
        pins[prefix] = name
    return pins


def _communities(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        report = run_communities(
            adapter,
            seed=args.seed,
            band=_parse_band(args.band),
            min_members=args.min_members,
            pins=_parse_pins(args.pin) if args.pin else None,
            jaccard_threshold=args.jaccard_threshold,
        )
        if args.llm_labels:
            # Second, separately-committed pass over already-labelled rows.
            # The deterministic labels above are durable: a failure here
            # exits non-zero with every community still labelled.
            namer = OllamaNamer(url=args.ollama_url, model=args.llm_model)
            renamed = rename_labels(adapter, namer)
            print(f"llm labels: renamed {renamed['renamed']} communities "
                  f"via {namer.url} ({namer.model})")
            # Refresh across EVERY level present (re-review finding 1):
            # descent packs communities from levels below fanout_level, and
            # their renamed labels must reach the artifact too. Community ids
            # are unique across levels (single-table PK), so one flat map.
            fresh = {}
            level = 0
            while True:
                rows = adapter.get_communities(level)
                if not rows:
                    break
                fresh.update({r["id"]: r["label"] for r in rows})
                level += 1
            for unit in report["units"]:
                for comm in unit["communities"]:
                    comm["label"] = fresh.get(comm["id"], comm["label"])
                first = unit["communities"][0]["label"]
                unit["label"] = first if len(unit["communities"]) == 1 \
                    else f"{first} (+{len(unit['communities']) - 1} more)"
            for parent in report["parents"]:
                parent["label"] = fresh.get(parent["id"], parent["label"])
        # resolve per-community member symbols for the coverage checklists
        by_id = {
            e["id"]: e for e in adapter.find_entities({"tier": "symbol"})
        }
        for unit in report["units"]:
            for comm in unit["communities"]:
                symbols = []
                for eid in comm["member_ids"]:
                    ent = by_id[eid]
                    span = ent.get("span") or {}
                    symbols.append({
                        "kind": ent["kind"],
                        "name": ent["name"],
                        "file": span.get("file") or (ent.get("source_paths") or [""])[0],
                        "line": span.get("start_line") or 0,
                    })
                comm["symbols"] = sorted(
                    symbols, key=lambda s: (s["file"], s["line"], s["name"])
                )
        for lvl in report["levels"]:
            print(f"level {lvl['level']}: {lvl['count']} communities")
        print(f"connected-component floor: {report['component_floor']}")
        n_comms = sum(len(u["communities"]) for u in report["units"])
        total_syms = sum(u["symbol_count"] for u in report["units"])
        largest = max((u["symbol_count"] for u in report["units"]), default=0)
        print(f"fan-out: {len(report['units'])} work units over {n_comms} "
              f"communities at level {report['fanout_level']} "
              f"(largest unit {largest}/{total_syms} symbols)")
        for u in report["units"][:8]:
            print(f"  {u['key']}: {u['label']} ({u['symbol_count']} symbols)")
        for c in report["churn"]:
            print(f"churn level {c['level']}: {len(c['created'])} created, "
                  f"{len(c['dissolved'])} dissolved, {len(c['changed'])} changed")
        if report["pins_unmatched"]:
            print(f"WARNING: pin prefix(es) matched no symbol: "
                  f"{', '.join(report['pins_unmatched'])}")
        if args.emit:
            with open(args.emit, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(report, fh, indent=2, sort_keys=True)
                fh.write("\n")
            print(f"artifact -> {args.emit}")
    finally:
        adapter.close()
    return 0


def _records(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        manifest = None
        if args.manifest:
            with open(args.manifest, encoding="utf-8") as fh:
                m = json.load(fh)
            # accept the workflow return itself, its records key, or a bare list
            if isinstance(m, list):
                manifest = m
            else:
                manifest = (m.get("records") or {}).get("files") or m.get("files")
            if not isinstance(manifest, list) or not manifest:
                raise RecordsError(
                    f"--manifest {args.manifest} carries no records file list "
                    "(expected the workflow return, its records key, or a bare list)"
                )
        records = load_records_input(args.records, manifest=manifest)
        artifact = None
        if args.artifact:
            with open(args.artifact, encoding="utf-8") as fh:
                artifact = json.load(fh)
        assignment = load_assignment(args.assignment) if args.assignment else None
        report = write_records(adapter, records, sha=args.sha, date=args.date,
                               artifact=artifact, assignment=assignment)
        # stderr, one line per page: stdout carries the JSON report
        for claim in report.get("duplicate_claims") or []:
            print(duplicate_claim_warning(claim), file=sys.stderr)
        for carried in report.get("diagrams_carried") or []:
            print(diagram_carried_note(carried), file=sys.stderr)
        rates = join_rates(adapter)
        report["join_rates"] = rates
        if artifact is not None:
            require_reports(adapter, _artifact_community_ids(args.artifact))
            report["reports_required"] = "ok"
        if args.coverage_out:
            with open(args.coverage_out, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(collect_coverage(args.records), fh, indent=2, sort_keys=True)
                fh.write("\n")
            report["coverage_out"] = args.coverage_out
        if args.join_floor is not None:
            enforce_join_floor(rates, args.join_floor)
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        adapter.close()
    return 0


def _check_records(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        with open(args.records, encoding="utf-8") as fh:
            try:
                records = json.load(fh)
            except json.JSONDecodeError as e:
                raise RecordsError(f"records file {args.records} is not valid JSON: {e}") from e
        result = check_records(adapter, records)
    finally:
        adapter.close()
    # stderr, one line per problem: stdout carries the JSON result
    for problem in result["problems"]:
        print(f"PROBLEM {problem}", file=sys.stderr)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["problems"] else 0


def _migrate_diagrams(args: argparse.Namespace) -> int:
    validate = load_mermaid_validator(args.validator)
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        report = migrate_body_diagrams(adapter, validate, dry_run=args.dry_run)
    finally:
        adapter.close()
    verb = "would move" if args.dry_run else "moved"
    for m in report["migrated"]:
        print(f"{verb} {m['file']} (diagram sha {m['diagram_sha'] or 'unknown'})",
              file=sys.stderr)
    for r in report["refused"]:
        print(f"REFUSED {r['file']}: {r['reason']}", file=sys.stderr)
    for r in report["left"]:
        print(f"left {r['file']}: {r['reason']}", file=sys.stderr)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["refused"] else 0


def _render(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        report = render_pages(adapter, args.out, force=args.force)
        dropped = (len(report["dropped_body_links"])
                   + len(report["dropped_frontmatter_links"]))
        print(f"render: {report['pages']} page(s), {report['meta_pages']} meta, "
              f"{report['nav_pages']} nav -> {args.out} "
              f"({dropped} unresolvable link(s) dropped)")
        print(json.dumps({k: report[k] for k in
                          ("pages_by_type", "dropped_body_links",
                           "dropped_frontmatter_links")}, indent=2, sort_keys=True))
    finally:
        adapter.close()
    return 0


def _check(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        report = check_store(adapter)
    finally:
        adapter.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")
    print(f"check: {report['summary']}")
    for b in report["blockers"]:
        print(f"  BLOCKER {b['kind']}: {b['detail']}", file=sys.stderr)
    return 1 if report["blockers"] else 0


def _reports_stale(args: argparse.Namespace) -> int:
    adapter = SqliteGraphAdapter(args.db)
    adapter.open(args.corpus, create=False)
    try:
        with open(args.artifact, encoding="utf-8") as fh:
            artifact = json.load(fh)
        stale = stale_report_communities(adapter, artifact.get("churn", []), artifact)
        print(json.dumps({"stale": stale, "count": len(stale)}))
    finally:
        adapter.close()
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="graph", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", required=True, help="SQLite database path")
        sp.add_argument("--corpus", required=True, help="corpus id (e.g. repo name)")

    b = sub.add_parser("build", help="ingest extraction JSONL (optionally embed)")
    common(b)
    b.add_argument("--jsonl", required=True, help="graph JSONL from emit-graph.js")
    b.add_argument("--sha", default=None, help="run sha (default: JSONL meta.sha)")
    # --repo-ref and --project are optional to argparse and required in fact:
    # _require_attribution refuses the build when a flag is absent AND the corpus
    # carries no stored value, so a rebuild of an attributed corpus still needs
    # neither. Neither is ever defaulted or inferred -- a guessed project is worse
    # than an absent one, since the renderer files pages into a per-project pillar
    # by it. The refusal used to land at render time, six steps later (gh#152).
    b.add_argument("--repo-ref", default=None)
    b.add_argument("--project", default=None)
    b.add_argument("--branch", default=None)
    b.add_argument("--embed", action="store_true",
                   help="run the incremental Ollama embedding pass after ingest")
    b.add_argument("--ollama-url", default=None,
                   help="Ollama base URL (default: $AUTODOC_OLLAMA_URL or "
                        "http://localhost:11434)")
    b.set_defaults(fn=_build)

    s = sub.add_parser("stats", help="print corpus stats as JSON")
    common(s)
    s.set_defaults(fn=_stats)

    pr = sub.add_parser("prune", help="report (default) or delete stale entities")
    common(pr)
    mode = pr.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="report candidates only (this is already the default)")
    mode.add_argument("--delete", action="store_true",
                      help="actually delete candidates + their edges/embeddings")
    pr.set_defaults(fn=_prune)

    v = sub.add_parser("verify", help="integrity checks; nonzero exit on findings")
    common(v)
    v.set_defaults(fn=_verify)

    c = sub.add_parser(
        "communities",
        help="derive + persist the Louvain community hierarchy (epic 703)",
    )
    common(c)
    c.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help="explicit Louvain seed (determinism)")
    c.add_argument("--band", default=f"{DEFAULT_BAND[0]}:{DEFAULT_BAND[1]}",
                   help="agent-count budget LO:HI for packed work units "
                        "(default 8:24); a cost dial, not a partition "
                        "property — any count of communities packs into it")
    c.add_argument("--min-members", type=int, default=DEFAULT_MIN_MEMBERS,
                   help="communities below this size merge into their "
                        "strongest-weighted neighbour")
    c.add_argument("--pin", action="append", default=[], metavar="PREFIX=NAME",
                   help="escape hatch: pin a path prefix to a named community "
                        "(repeatable; no catch-all)")
    c.add_argument("--jaccard-threshold", type=float,
                   default=DEFAULT_JACCARD_THRESHOLD,
                   help="member-overlap threshold for carrying a community id "
                        "forward across runs")
    c.add_argument("--emit", default=None,
                   help="write the communities artifact JSON (fan-out units + "
                        "per-community symbol checklists) to this path")
    c.add_argument("--llm-labels", action="store_true",
                   help="optional second pass: an LLM rewrites labels only "
                        "(never membership); deterministic labels are durable "
                        "before it runs")
    c.add_argument("--llm-model", default=None,
                   help="naming model (or $AUTODOC_NAMING_MODEL); required "
                        "when --llm-labels is set — no baked-in default")
    c.add_argument("--ollama-url", default=None,
                   help="Ollama base URL for the naming pass (default: "
                        "$AUTODOC_OLLAMA_URL or http://localhost:11434)")
    c.set_defaults(fn=_communities)

    rec = sub.add_parser(
        "records",
        help="write agent-return records (concepts, documents joins, reports)",
    )
    common(rec)
    rec.add_argument("--records", required=True,
                     help="records JSON file, OR a directory of per-agent "
                          "records files (the disk-based workflow contract)")
    rec.add_argument("--sha", required=True, help="run sha (stamps last_seen_sha "
                     "and summarized_at_sha)")
    rec.add_argument("--date", required=True, help="run date YYYY-MM-DD "
                     "(first-write created stamp)")
    rec.add_argument("--artifact", default=None,
                     help="communities artifact JSON; when given, every unit + "
                          "parent community must end the run with a report")
    rec.add_argument("--manifest", default=None,
                     help="the workflow return JSON (records.files is the "
                          "authoritative file list) — directory ingestion then "
                          "errors on orphan *.json files from prior attempts")
    rec.add_argument("--assignment", default=None,
                     help="changed-communities JSON (autodoc-update step 3); "
                          "a page claimed by several units then keeps the "
                          "assigned unit's record, else the lowest unit id's, "
                          "with a WARNING per page instead of a refusal")
    rec.add_argument("--coverage-out", default=None,
                     help="aggregate the input's covered/excluded/pages "
                          "coverage evidence to this JSON file")
    rec.add_argument("--join-floor", type=float, default=None,
                     help="fail when endpoint/component/data-model/job "
                          "symbol-join rate is under this floor (e.g. 0.8)")
    rec.set_defaults(fn=_records)

    cr = sub.add_parser(
        "check-records",
        help="read-only: check one records file's page structure and documents "
             "citations against the store's symbol index; nonzero exit on problems",
    )
    common(cr)
    cr.add_argument("--records", required=True,
                    help="ONE records JSON file (an agent's own output)")
    cr.set_defaults(fn=_check_records)

    md = sub.add_parser(
        "migrate-diagrams",
        help="one-time, idempotent: move body-stored '## Diagram' sections into "
             "the reserved _diagram key (gh#465)",
    )
    common(md)
    md.add_argument("--validator", required=True,
                    help="the suite validator, $SUITE_ROOT/scripts/validate_mermaid.py; "
                         "every diagram is validated before it moves")
    md.add_argument("--dry-run", action="store_true",
                    help="report what would move; write nothing")
    md.set_defaults(fn=_migrate_diagrams)

    rd = sub.add_parser("render", help="deterministically render the concept "
                                       "tier to a markdown tree")
    common(rd)
    rd.add_argument("--out", required=True, help="target directory (must be "
                    "empty unless --force)")
    rd.add_argument("--force", action="store_true",
                    help="wipe *.md under the target before rendering "
                         "(renderer-owned directories only)")
    rd.set_defaults(fn=_render)

    ck = sub.add_parser("check", help="store-mode integrity checks; "
                                      "nonzero exit on blockers")
    common(ck)
    ck.add_argument("--out", default=None, help="write the report JSON here")
    ck.set_defaults(fn=_check)

    rs = sub.add_parser(
        "reports-stale",
        help="which artifact communities need (re)summarization per the churn "
             "report — zero on an unchanged corpus",
    )
    common(rs)
    rs.add_argument("--artifact", required=True, help="communities artifact JSON")
    rs.set_defaults(fn=_reports_stale)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        return args.fn(args)
    except _ERRORS as e:
        print(f"graph {args.cmd} FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
