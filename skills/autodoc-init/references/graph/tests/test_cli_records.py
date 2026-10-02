"""CLI surface of epic 704: records / render / check / reports-stale, plus the
parents chain in the communities artifact."""

from __future__ import annotations

import json

from autodoc_graph.cli import main
from autodoc_graph.sqlite_backend import SqliteGraphAdapter


def _entity(id, file="a.py", kind="function", **over):
    name = id.rsplit(":", 1)[-1]
    e = {
        "type": "entity", "id": id, "tier": "symbol", "kind": kind,
        "name": name, "qualified_name": f"{file}:{name}",
        "source_paths": [file],
        "span": {"file": file, "start_line": 1, "end_line": 2},
        "summary": None, "origin": "ast",
    }
    e.update(over)
    return e


def _build(tmp_path, db):
    ents = [
        _entity("sym:corp:a.py:function:f"),
        _entity("sym:corp:a.py:function:g"),
        _entity("sym:corp:b.py:function:h", file="b.py"),
        _entity("sym:corp:b.py:function:i", file="b.py"),
    ]
    lines = [*ents,
             {"type": "edge", "rel": "calls",
              "src_id": "sym:corp:a.py:function:f",
              "dst_id": "sym:corp:a.py:function:g",
              "origin": "ast", "confidence": 1.0,
              "evidence": {"file": "a.py", "line": 3}},
             {"type": "meta", "corpus": "corp", "repo": "o/r", "sha": "sha1",
              "counts": {}}]
    jsonl = tmp_path / "g.jsonl"
    jsonl.write_text("\n".join(json.dumps(x) for x in lines) + "\n",
                     encoding="utf-8")
    assert main(["build", "--db", str(db), "--corpus", "corp",
                 "--jsonl", str(jsonl), "--repo-ref", "owner/repo",
                 "--project", "demo"]) == 0


def _records_file(tmp_path, reports=None):
    records = {
        "pages": [{
            "file": "services/Alpha.md", "type": "service", "title": "Alpha",
            "source_paths": ["a.py"], "summary": "The alpha service.",
            "body": "The alpha service.\n\n## Source\n\n`a.py`\n",
            "frontmatter": {"service_kind": "api", "path": "a.py",
                            "language": "python", "entrypoint": "a.py"},
            "documents": [{"file": "a.py", "kind": "function", "name": "f"}],
            "edges": [],
        }],
        "reports": reports or [],
        "meta_pages": [],
        "diagrams": [],
    }
    p = tmp_path / "records.json"
    p.write_text(json.dumps(records), encoding="utf-8")
    return p


def test_records_render_check_pipeline(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    capsys.readouterr()  # drop the build output
    rec = _records_file(tmp_path)
    assert main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(rec), "--sha", "sha1",
                 "--date", "2026-08-07"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pages"] == 1 and out["documents_edges"] == 1
    assert out["join_rates"]["service"]["rate"] == 1.0

    target = tmp_path / "rendered"
    assert main(["render", "--db", str(db), "--corpus", "corp",
                 "--out", str(target)]) == 0
    assert (target / "services" / "Alpha.md").exists()
    assert (target / "index.md").exists()

    assert main(["check", "--db", str(db), "--corpus", "corp",
                 "--out", str(tmp_path / "check.json")]) == 0
    report = json.loads((tmp_path / "check.json").read_text(encoding="utf-8"))
    assert report["blockers"] == []


def test_records_rejects_bad_record_loudly(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    bad = {"pages": [{"file": "services/X.md", "type": "service", "title": "X",
                      "source_paths": ["a.py"], "summary": "s", "body": "",
                      "frontmatter": {"service_kind": "api", "path": "a",
                                      "language": "py", "entrypoint": "a"}}]}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    assert main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(p), "--sha", "s", "--date", "d"]) == 1
    assert "body" in capsys.readouterr().err


def test_join_floor_flag_gates(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    records = {
        "pages": [{
            "file": "jobs/Bare Job.md", "type": "job", "title": "Bare Job",
            "source_paths": ["a.py"], "summary": "s.", "body": "b.\n",
            "frontmatter": {"job_kind": "cron", "schedule": "daily",
                            "handler": "a.py:f"},
            "documents": [], "edges": [],
        }],
    }
    p = tmp_path / "records.json"
    p.write_text(json.dumps(records), encoding="utf-8")
    assert main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(p), "--sha", "s", "--date", "2026-08-07",
                 "--join-floor", "0.8"]) == 1
    assert "job" in capsys.readouterr().err


def test_communities_artifact_carries_parents(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    art = tmp_path / "comm.json"
    assert main(["communities", "--db", str(db), "--corpus", "corp",
                 "--band", "1:2", "--emit", str(art)]) == 0
    capsys.readouterr()
    artifact = json.loads(art.read_text(encoding="utf-8"))
    assert "parents" in artifact
    unit_ids = {c["id"] for u in artifact["units"] for c in u["communities"]}
    for parent in artifact["parents"]:
        assert parent["id"] not in unit_ids  # parents are ABOVE the fan-out set
        assert parent["children"], "every parent rolls up at least one child"
        assert set(parent["children"]) & (
            unit_ids | {p["id"] for p in artifact["parents"]}
        )


def test_reports_stale_zero_after_summarization(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    art = tmp_path / "comm.json"
    assert main(["communities", "--db", str(db), "--corpus", "corp",
                 "--band", "1:2", "--emit", str(art)]) == 0
    capsys.readouterr()
    artifact = json.loads(art.read_text(encoding="utf-8"))
    ids = sorted({c["id"] for u in artifact["units"] for c in u["communities"]}
                 | {p["id"] for p in artifact["parents"]})

    # before any report: everything is stale
    assert main(["reports-stale", "--db", str(db), "--corpus", "corp",
                 "--artifact", str(art)]) == 0
    stale = json.loads(capsys.readouterr().out)
    assert stale["stale"] == ids

    reports = [{"community_id": cid, "summary": f"community {cid}",
                "findings": [{"text": "f1", "entities": []}]} for cid in ids]
    rec = _records_file(tmp_path, reports=reports)
    assert main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(rec), "--sha", "sha1", "--date", "2026-08-07",
                 "--artifact", str(art)]) == 0
    capsys.readouterr()

    # re-run detection on the unchanged corpus: fresh artifact, zero stale
    art2 = tmp_path / "comm2.json"
    assert main(["communities", "--db", str(db), "--corpus", "corp",
                 "--band", "1:2", "--emit", str(art2)]) == 0
    capsys.readouterr()
    assert main(["reports-stale", "--db", str(db), "--corpus", "corp",
                 "--artifact", str(art2)]) == 0
    stale2 = json.loads(capsys.readouterr().out)
    assert stale2 == {"stale": [], "count": 0}


def test_require_reports_fails_when_a_community_is_empty(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    art = tmp_path / "comm.json"
    assert main(["communities", "--db", str(db), "--corpus", "corp",
                 "--band", "1:2", "--emit", str(art)]) == 0
    capsys.readouterr()
    rec = _records_file(tmp_path)  # no reports at all
    assert main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(rec), "--sha", "s", "--date", "2026-08-07",
                 "--artifact", str(art)]) == 1
    assert "EMPTY report" in capsys.readouterr().err


def test_get_corpus_reads_the_open_row(tmp_path):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        row = a.get_corpus()
    finally:
        a.close()
    assert row["id"] == "corp"
    assert row["repo_ref"] == "owner/repo"
    assert row["last_indexed_sha"] == "sha1"
    assert row["indexed_at"]


# ------------------------------------------------ duplicate page claims (gh#455)
def _claim(title, body, file="services/Alpha.md"):
    return {"file": file, "type": "service", "title": title,
            "source_paths": ["a.py"], "summary": "s.", "body": body,
            "frontmatter": {"service_kind": "api", "path": "a.py",
                            "language": "python", "entrypoint": "a.py"},
            "documents": [], "edges": []}


def _dup_dir(tmp_path, claims=(("unit-20", "Alpha Twenty"), ("unit-02", "Alpha"),
                               ("unit-11", "Alpha Eleven")), file="services/Alpha.md"):
    d = tmp_path / "records"
    d.mkdir()
    for unit, title in claims:
        (d / f"refresh-{unit}.json").write_text(
            json.dumps({"pages": [_claim(title, "b" * 50 + "\n", file)]}),
            encoding="utf-8")
    return d


def _assignment(tmp_path, entries):
    path = tmp_path / "changed.json"
    path.write_text(json.dumps([
        {"key": key, "label": "l", "descriptor": {}, "changedFiles": ["a.py"],
         "affectedPages": pages, "symbols": []} for key, pages in entries
    ]), encoding="utf-8")
    return path


def _run_records(db, records, assign):
    return main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(records), "--sha", "s",
                 "--date", "2026-08-07", "--assignment", str(assign)])


def test_records_resolves_duplicate_claims_with_assignment(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    capsys.readouterr()
    assign = tmp_path / "changed.json"
    assign.write_text(json.dumps([
        {"key": "unit-11", "label": "l", "descriptor": {}, "changedFiles": ["a.py"],
         "affectedPages": ["components/Other.md"], "symbols": []},
    ]), encoding="utf-8")
    assert main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(_dup_dir(tmp_path)), "--sha", "s",
                 "--date", "2026-08-07", "--assignment", str(assign)]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["pages"] == 1
    assert out["duplicate_claims"] == [{
        "file": "services/Alpha.md", "claimants": ["unit-02", "unit-11", "unit-20"],
        "assigned": None, "kept": "unit-02", "dropped": ["unit-11", "unit-20"],
    }]
    assert cap.err.strip() == (
        "WARNING: page 'services/Alpha.md' claimed by unit-02, unit-11, unit-20; "
        "assigned to none (partitioner gap); kept unit-02; dropped unit-11, unit-20"
    )


def test_records_without_assignment_still_refuses_duplicates(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    assert main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(_dup_dir(tmp_path)), "--sha", "s",
                 "--date", "2026-08-07"]) == 1
    assert "both claim" in capsys.readouterr().err


def test_records_malformed_assignment_exits_1(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    records = _dup_dir(tmp_path)
    for body in ("not json", json.dumps({"key": "unit-01"}),
                 json.dumps([{"key": "unit-01", "affectedPages": "a.md"}])):
        assign = tmp_path / "changed.json"
        assign.write_text(body, encoding="utf-8")
        capsys.readouterr()
        assert main(["records", "--db", str(db), "--corpus", "corp",
                     "--records", str(records), "--sha", "s", "--date", "2026-08-07",
                     "--assignment", str(assign)]) == 1
        assert "--assignment" in capsys.readouterr().err


def test_records_assigned_unit_wins_through_the_real_assignment_loader(tmp_path, capsys):
    """The assignment names the page in mixed case; the lookup must casefold
    both sides, or the assigned unit silently loses to the lowest id."""
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    page = "services/Alpha Service.md"
    records = _dup_dir(tmp_path, (("unit-02", "Alpha"), ("unit-11", "Alpha Eleven")),
                       file=page)
    assign = _assignment(tmp_path, [("unit-11", [page])])
    capsys.readouterr()
    assert _run_records(db, records, assign) == 0
    cap = capsys.readouterr()
    (claim,) = json.loads(cap.out)["duplicate_claims"]
    assert claim["kept"] == "unit-11" and claim["assigned"] == "unit-11"
    assert "gap" not in cap.err and "kept unit-11" in cap.err


def test_records_assigned_unit_that_did_not_claim_keeps_lowest_claimant(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    records = _dup_dir(tmp_path, (("unit-11", "Alpha Eleven"), ("unit-02", "Alpha")))
    assign = _assignment(tmp_path, [("unit-07", ["services/Alpha.md"])])
    capsys.readouterr()
    assert _run_records(db, records, assign) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["pages"] == 1
    (claim,) = out["duplicate_claims"]
    assert claim["kept"] == "unit-02" and claim["kept"] in claim["claimants"]
    assert claim["assigned"] == "unit-07"
    assert "did not claim it" in cap.err


def test_records_single_file_without_collisions_accepts_assignment(tmp_path, capsys):
    """No collision, nothing to resolve: provenance is not needed."""
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    assign = _assignment(tmp_path, [("unit-01", ["services/Alpha.md"])])
    capsys.readouterr()
    assert _run_records(db, _records_file(tmp_path), assign) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["pages"] == 1 and out["duplicate_claims"] == []
    assert "WARNING" not in cap.err


def test_records_single_file_with_a_collision_needs_provenance(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    single = tmp_path / "one.json"
    single.write_text(json.dumps({"pages": [_claim("Alpha", "b\n"),
                                            _claim("Alpha Two", "b\n")]}),
                      encoding="utf-8")
    assign = _assignment(tmp_path, [("unit-01", ["services/Alpha.md"])])
    capsys.readouterr()
    assert _run_records(db, single, assign) == 1
    assert "provenance" in capsys.readouterr().err


def test_records_empty_assignment_list_resolves_as_unassigned(tmp_path, capsys):
    # a docs-only run: Step 3 writes [] and the rolling audit still emits records
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    assign = tmp_path / "changed.json"
    assign.write_text("[]", encoding="utf-8")
    capsys.readouterr()
    assert _run_records(db, _dup_dir(tmp_path), assign) == 0
    assert "partitioner gap" in capsys.readouterr().err


# --------------------------------------- stored diagram carry-forward (gh#497)
def test_records_notes_a_carried_forward_diagram(tmp_path, capsys):
    db = tmp_path / "graph.db"
    _build(tmp_path, db)
    first = json.loads(_records_file(tmp_path).read_text(encoding="utf-8"))
    first["diagrams"] = [{"type": "service", "title": "Alpha",
                          "mermaid": "flowchart TD\n  A --> B",
                          "source_sha": "418bec0c1234"}]
    p1 = tmp_path / "first.json"
    p1.write_text(json.dumps(first), encoding="utf-8")
    assert main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(p1), "--sha", "s1", "--date", "2026-08-07"]) == 0
    capsys.readouterr()
    p2 = _records_file(tmp_path)  # same page, no diagrams entry
    assert main(["records", "--db", str(db), "--corpus", "corp",
                 "--records", str(p2), "--sha", "s2", "--date", "2026-09-23"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["diagrams_carried"] == [
        {"file": "services/Alpha.md", "diagram_sha": "418bec0c1234"}]
    assert cap.err.strip() == (
        "NOTE: page 'services/Alpha.md' re-written without an embedded diagram; "
        "kept its stored diagram (sha 418bec0c)"
    )
