"""`graph communities` CLI subcommand — end-to-end over a temp store.

Covers: happy path WITH SHIPPED DEFAULTS (partition + labels + packed work
units + artifact emission — review blocker 1: the run must complete
regardless of community count), determinism across invocations, the
regression shape of the 525/525-to-'infra' failure (symbols distribute, no
unit holding more than ~40%), invalid band values failing loudly naming
--band, pin flags (including names with spaces — the $COMM_PINS shell
contract), and malformed pins.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autodoc_graph.cli import main
from autodoc_graph.sqlite_backend import SqliteGraphAdapter

# gh#152: `build` refuses a corpus it would leave unrenderable, so every FIRST build
# below carries the attribution the renderer needs. Held in one constant because these
# tests are about ingest/prune/verify/community behaviour, not attribution -- the
# refusal itself is exercised in tests/test_cli_attribution.py.
ATTRIB = ["--repo-ref", "owner/corp", "--project", "demo"]


def _entity(id, file, name, kind="function"):
    return {
        "type": "entity", "id": id, "tier": "symbol", "kind": kind,
        "name": name, "qualified_name": f"{file}:{name}",
        "source_paths": [file],
        "span": {"file": file, "start_line": 1, "end_line": 2},
        "summary": None, "origin": "ast",
    }


def _edge(src, dst, rel="calls"):
    return {"type": "edge", "rel": rel, "src_id": src, "dst_id": dst,
            "origin": "ast", "confidence": 1.0,
            "evidence": {"file": "a.py", "line": 3}}


def _fixture_jsonl(tmp_path):
    """Three dense clusters across three directories + one loner."""
    lines = []
    ids = {}
    for dirname in ("core", "web", "jobs"):
        cluster = []
        for i in range(4):
            eid = f"sym:corp:{dirname}/m{i}.py:function:{dirname}_fn{i}"
            lines.append(_entity(eid, f"{dirname}/m{i}.py", f"{dirname}_fn{i}"))
            cluster.append(eid)
        ids[dirname] = cluster
        for a in cluster:
            for b in cluster:
                if a < b:
                    lines.append(_edge(a, b))
    lines.append(_entity("sym:corp:tools/lone.py:function:lone_fn",
                         "tools/lone.py", "lone_fn"))
    lines.append({"type": "meta", "corpus": "corp", "repo": "", "sha": "sha1",
                  "counts": {}})
    p = tmp_path / "graph.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def _built_db(tmp_path):
    db = tmp_path / "g.db"
    assert main(["build", "--jsonl", str(_fixture_jsonl(tmp_path)),
                 "--db", str(db), "--corpus", "corp", *ATTRIB]) == 0
    return db


def test_communities_default_band_completes_and_writes_artifact(tmp_path, capsys):
    """Review blocker 1: shipped defaults must complete on any non-empty
    corpus — packing decouples agent fan-out from community count."""
    db = _built_db(tmp_path)
    out = tmp_path / "communities.json"
    rc = main(["communities", "--db", str(db), "--corpus", "corp",
               "--emit", str(out)])  # DEFAULTS — no --band
    assert rc == 0, capsys.readouterr().err
    printed = capsys.readouterr().out
    assert "level 0" in printed
    assert "work units" in printed
    assert "component floor" in printed.replace("-", " ")

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["corpus"] == "corp"
    assert artifact["units"], "packing must always produce units"
    assert len(artifact["units"]) <= 24
    total = 0
    for unit in artifact["units"]:
        assert unit["key"] and unit["label"]
        for comm in unit["communities"]:
            assert isinstance(comm["id"], int)  # units keyed by community ID
            assert comm["label"]
            assert comm["descriptor"]["member_count"] == len(comm["member_ids"])
            # checklist material: every community resolves its symbols
            assert comm["symbols"], "checklists must be non-empty per community"
            for s in comm["symbols"]:
                assert set(s) == {"kind", "name", "file", "line"}
            total += len(comm["member_ids"])
    assert total == 13  # every symbol in exactly one unit's community

    # regression: the 525/525-to-'infra' shape — no unit > ~40% of symbols
    largest = max(u["symbol_count"] for u in artifact["units"])
    assert largest / total <= 0.4

    # every symbol has a community_id in the store
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        assert all(
            e["community_id"] is not None
            for e in a.find_entities({"tier": "symbol"})
        )
    finally:
        a.close()


def test_communities_twice_is_identical(tmp_path, capsys):
    db = _built_db(tmp_path)
    out1, out2 = tmp_path / "c1.json", tmp_path / "c2.json"
    assert main(["communities", "--db", str(db), "--corpus", "corp",
                 "--emit", str(out1)]) == 0
    assert main(["communities", "--db", str(db), "--corpus", "corp",
                 "--emit", str(out2)]) == 0
    capsys.readouterr()
    a1 = json.loads(out1.read_text(encoding="utf-8"))
    a2 = json.loads(out2.read_text(encoding="utf-8"))
    assert a1["units"] == a2["units"]
    assert a1["levels"] == a2["levels"]


def test_communities_invalid_band_names_the_flag(tmp_path, capsys):
    db = _built_db(tmp_path)
    rc = main(["communities", "--db", str(db), "--corpus", "corp",
               "--band", "24:8"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--band" in err
    assert "1:1" in err  # smallest satisfiable configuration named


def test_communities_pin_flag_with_spaces(tmp_path, capsys):
    """The canonical SCHEMA example shape ('Legacy Subsystem') — a pin name
    with a space must arrive as ONE argument and be stored verbatim."""
    db = _built_db(tmp_path)
    out = tmp_path / "c.json"
    rc = main(["communities", "--db", str(db), "--corpus", "corp",
               "--pin", "web=Web Layer", "--emit", str(out)])
    assert rc == 0, capsys.readouterr().err
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        labels = [r["label"] for r in a.get_communities(0)]
        assert "Web Layer" in labels
    finally:
        a.close()


def _working_bash() -> str | None:
    """A REAL bash (Git Bash preferred) — on Windows, PATH 'bash' may be the
    WSL stub that exits 1 when no distribution is installed."""
    candidates = [
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ]
    for cand in candidates:
        if not cand or not Path(cand).exists():
            continue
        probe = subprocess.run(
            [cand, "-c", "echo ok"], capture_output=True, text=True, check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return cand
    return None


_BASH = _working_bash()


@pytest.mark.skipif(_BASH is None, reason="no working bash available")
def test_comm_pins_shell_snippet_preserves_spaces(tmp_path):
    """Doc-test for the SKILL.md $COMM_PINS contract (review finding 5): the
    per-line PREFIX=NAME output assembled into a bash array must deliver a
    space-containing pin as a single --pin argument."""
    script = (
        'COMM_PINS=()\n'
        'while IFS= read -r pin; do\n'
        '  [ -n "$pin" ] && COMM_PINS+=(--pin "$pin")\n'
        'done <<\'PINS\'\n'
        'legacy/subsys=Legacy Subsystem\n'
        'tools/scripts=Tooling\n'
        'PINS\n'
        # one line per argv element — proves the space stayed inside ONE arg
        'printf \'%s\\n\' "${COMM_PINS[@]}"\n'
    )
    res = subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    argv = res.stdout.splitlines()
    assert argv == ["--pin", "legacy/subsys=Legacy Subsystem",
                    "--pin", "tools/scripts=Tooling"]


def test_communities_malformed_pin_fails_loudly(tmp_path, capsys):
    db = _built_db(tmp_path)
    rc = main(["communities", "--db", str(db), "--corpus", "corp",
               "--pin", "no-equals-sign"])
    assert rc == 1
    assert "pin" in capsys.readouterr().err.lower()


def test_communities_empty_corpus_fails_loudly(tmp_path, capsys):
    db = tmp_path / "empty.db"
    a = SqliteGraphAdapter(db)
    a.open("corp")
    a.close()
    rc = main(["communities", "--db", str(db), "--corpus", "corp"])
    assert rc == 1
    assert "no symbol entities" in capsys.readouterr().err
