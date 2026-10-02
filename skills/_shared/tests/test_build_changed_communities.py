"""Tests for the Step 3 changedCommunities assembler.

Several of these exist because a review demonstrated the first version's tests could not fail for
the reasons their names gave, or covered nothing at all:

- the descriptor-dedupe test built ONE community and passed a duplicate inside it, so it never
  exercised the merge across two communities that its name describes;
- no test scoped affectedPages to the owning unit, so a mutant mapping pages against the WHOLE
  diff left all 14 green while changing the real answer;
- no test pinned case sensitivity, so a mutant case-folding the changed set left all 14 green.

Each of those now has a test that goes red on exactly that mutation.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SRC = (Path(__file__).resolve().parents[2]
        / "autodoc-update" / "references" / "build-changed-communities.py")


def _load():
    # Hyphens in the filename, so it is not importable by name. Loading it by path also pins the
    # location the SKILL.md invokes: a rename breaks that call site and this test together.
    spec = importlib.util.spec_from_file_location("build_changed_communities", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bcc = _load()


def _comm(label, files_and_syms, member_count=10, top=("A",), prefixes=()):
    return {
        "id": abs(hash(label)) % 1000,
        "label": label,
        "descriptor": {
            "member_count": member_count,
            "top_symbols": list(top),
            "top_prefixes": list(prefixes),
        },
        "member_ids": [],
        "symbols": [
            {"file": f, "kind": "function", "line": i + 1, "name": n}
            for f, names in files_and_syms.items()
            for i, n in enumerate(names)
        ],
    }


def _unit(key, label, communities):
    return {"key": key, "label": label, "symbol_count": "0", "communities": communities}


MANIFEST = {"pages": {
    "components/Alpha.md": {"type": "component", "source_paths": ["a.py"]},
    "components/Beta.md": {"type": "component", "source_paths": ["b.py", "c.py"]},
    "components/Gamma.md": {"type": "component", "source_paths": ["z.py"]},
    "components/Docs.md": {"type": "component", "source_paths": ["docs/guide.md"]},
    "index.md": {"type": "index"},          # no source_paths key at all -- nav pages carry none
}}


# --------------------------------------------------------------------------- grouping

def test_a_unit_with_no_changed_file_is_dropped_entirely():
    """Each entry becomes one refresh agent, so an untouched unit must not appear at all."""
    comms = {"units": [_unit("unit-01", "touched", [_comm("t", {"a.py": ["f1"]})]),
                       _unit("unit-02", "untouched", [_comm("u", {"z.py": ["f2"]})])]}
    got = bcc.build(comms, MANIFEST, ["a.py"])
    assert [c["key"] for c in got] == ["unit-01"]


def test_symbols_are_sliced_to_the_changed_files_not_the_whole_unit():
    comms = {"units": [_unit("unit-01", "u",
                             [_comm("c", {"a.py": ["keep"], "z.py": ["drop1", "drop2"]})])]}
    got = bcc.build(comms, MANIFEST, ["a.py"])
    assert [s["name"] for s in got[0]["symbols"]] == ["keep"]


def test_a_changed_file_no_symbol_names_is_attached_by_prefix_not_dropped():
    """Step 3's own documented fallback. Leaving it unimplemented discarded 107 of 254 real
    changed files, and with them pages the manifest explicitly sources from those files."""
    comms = {"units": [_unit("unit-01", "u",
                             [_comm("c", {"a.py": ["f"]}, prefixes=("docs",))])]}
    got = bcc.build(comms, MANIFEST, ["a.py", "docs/guide.md"])
    assert got[0]["changedFiles"] == ["a.py", "docs/guide.md"]
    assert "components/Docs.md" in got[0]["affectedPages"], (
        "the page sourced from the symbol-less file was not reached"
    )


def test_the_longest_matching_prefix_wins():
    """A shallow prefix must not claim a file that a deeper one covers, or every root-level
    prefix swallows the tree below it."""
    comms = {"units": [
        _unit("unit-01", "shallow", [_comm("s", {"a.py": ["f"]}, prefixes=("docs",))]),
        _unit("unit-02", "deep", [_comm("d", {"b.py": ["g"]}, prefixes=("docs/deep",))]),
    ]}
    got = {c["key"]: c for c in bcc.build(comms, MANIFEST, ["a.py", "b.py", "docs/deep/x.md"])}
    assert "docs/deep/x.md" in got["unit-02"]["changedFiles"]
    assert "docs/deep/x.md" not in got["unit-01"]["changedFiles"]


def test_a_prefix_matches_on_a_path_boundary_not_a_substring():
    """`docs` must not claim `docsite/x.md`."""
    comms = {"units": [_unit("unit-01", "u",
                             [_comm("c", {"a.py": ["f"]}, prefixes=("docs",))])]}
    got = bcc.build(comms, MANIFEST, ["a.py", "docsite/x.md"])
    assert "docsite/x.md" not in got[0]["changedFiles"]


def test_changed_files_are_the_intersection_not_the_diff():
    comms = {"units": [_unit("unit-01", "u", [_comm("c", {"a.py": ["f"]})])]}
    got = bcc.build(comms, MANIFEST, ["a.py", "unrelated.py"])
    assert got[0]["changedFiles"] == ["a.py"]


def test_matching_is_case_sensitive_and_a_fold_would_change_the_real_answer():
    """Both sides come from the same git diff and the same extraction, so a case-fold hides a
    real disagreement rather than fixing one. A mutant folding the changed set escaped every
    other test in this file."""
    comms = {"units": [_unit("unit-01", "u", [_comm("c", {"a.py": ["f"]})])]}
    assert bcc.build(comms, MANIFEST, ["A.PY"]) == []
    assert bcc.build(comms, MANIFEST, ["a.py"])[0]["changedFiles"] == ["a.py"]


# --------------------------------------------------------------------------- pages

def test_affected_pages_are_scoped_to_the_owning_unit():
    """A mutant mapping pages against the whole changed set instead of the unit's own files left
    all of this file's tests green while changing the live answer. This is that mutant's test."""
    comms = {"units": [
        _unit("unit-01", "owns-a", [_comm("x", {"a.py": ["f"]})]),
        _unit("unit-02", "owns-b", [_comm("y", {"b.py": ["g"]})]),
    ]}
    got = {c["key"]: c for c in bcc.build(comms, MANIFEST, ["a.py", "b.py"])}
    assert got["unit-01"]["affectedPages"] == ["components/Alpha.md"]
    assert got["unit-02"]["affectedPages"] == ["components/Beta.md"], (
        "a unit was handed a page sourced from another unit's changed file"
    )


def test_a_page_is_claimed_by_exactly_one_unit():
    """Each entry is one refresh agent. Two units sharing a page means two agents emitting a
    record for it in the same run, and `graph records` keeps whichever landed last. Measured on
    the live corpus before this rule: 44 of 111 pages were claimed twice."""
    comms = {"units": [
        _unit("unit-01", "one", [_comm("x", {"b.py": ["f"]})]),
        _unit("unit-02", "two", [_comm("y", {"c.py": ["g"]})]),
    ]}
    got = bcc.build(comms, MANIFEST, ["b.py", "c.py"])
    pages = [p for c in got for p in c["affectedPages"]]
    assert pages.count("components/Beta.md") == 1, "Beta.md was claimed by both units"


def test_page_ownership_goes_to_the_unit_with_the_most_changed_files_behind_it():
    comms = {"units": [
        _unit("unit-01", "one-file", [_comm("x", {"b.py": ["f"]})]),
        _unit("unit-02", "two-files", [_comm("y", {"b.py": ["g"], "c.py": ["h"]})]),
    ]}
    got = {c["key"]: c for c in bcc.build(comms, MANIFEST, ["b.py", "c.py"])}
    assert "components/Beta.md" in got["unit-02"]["affectedPages"]
    assert "components/Beta.md" not in got["unit-01"]["affectedPages"]


def test_a_page_with_no_source_paths_key_is_not_a_crash():
    """`index.md` in the fixture has none, which is real: nav pages carry no source_paths."""
    comms = {"units": [_unit("unit-01", "u", [_comm("c", {"b.py": ["f"]})])]}
    assert bcc.build(comms, MANIFEST, ["b.py"])[0]["affectedPages"] == ["components/Beta.md"]


# --------------------------------------------------------------------------- descriptor

def test_descriptor_merges_across_two_real_communities_in_one_unit():
    """Units are a PACKING of communities. The earlier version of this test built ONE community
    and fed it a duplicate, so it passed without ever exercising the merge it is named for."""
    comms = {"units": [_unit("unit-01", "u", [
        _comm("first", {"a.py": ["f"]}, member_count=10, top=("A",), prefixes=("p1",)),
        _comm("second", {"a.py": ["g"]}, member_count=5, top=("B",), prefixes=("p2",)),
    ])]}
    d = bcc.build(comms, MANIFEST, ["a.py"])[0]["descriptor"]
    assert d["member_count"] == 15, "member_count did not sum across the unit's communities"
    assert d["top_symbols"] == ["A", "B"]
    assert d["top_prefixes"] == ["p1", "p2"]


def test_a_symbol_two_communities_of_one_unit_share_is_listed_once():
    """The dedupe, tested across TWO communities -- which is the only shape that can exercise it."""
    comms = {"units": [_unit("unit-01", "u", [
        _comm("first", {"a.py": ["f"]}, top=("SHARED",)),
        _comm("second", {"a.py": ["g"]}, top=("SHARED",)),
    ])]}
    assert bcc.build(comms, MANIFEST, ["a.py"])[0]["descriptor"]["top_symbols"] == ["SHARED"]


# --------------------------------------------------------------------------- contract + CLI

def test_every_entry_carries_exactly_the_keys_the_workflow_destructures():
    comms = {"units": [_unit("unit-01", "u", [_comm("c", {"a.py": ["f"]})])]}
    got = bcc.build(comms, MANIFEST, ["a.py"])
    assert set(got[0]) == {"key", "label", "descriptor", "changedFiles", "affectedPages", "symbols"}
    assert set(got[0]["symbols"][0]) == {"name", "file", "line", "kind"}


# --------------------------------------------------------------------------- kind (gh#527)
# A refresh agent's `documents` citation resolves on (file, kind, name) to the store's symbol id,
# and `graph records` refuses the WHOLE batch on one that resolves to nothing. With only
# name/file/line in the checklist, agents guessed the kind -- and guessed "method" for Python
# dunders the store holds as "function", two refreshes running.

_GRAPH_PKG = Path(__file__).resolve().parents[2] / "autodoc-init" / "references" / "graph"


def _store_symbol_id():
    """The store's own id builder, from THIS tree -- a copy of the formula would pin nothing."""
    import sys
    sys.path.insert(0, str(_GRAPH_PKG))
    try:
        from autodoc_graph import records
    finally:
        sys.path.remove(str(_GRAPH_PKG))
    assert Path(records.__file__).resolve().is_relative_to(_GRAPH_PKG), records.__file__
    return records.symbol_id


def _store_shaped_comms():
    # Rows exactly as `graph communities --emit` writes them (cli.py copies ent["kind"]):
    # a Python class, its dunder (extract-symbols captures every Python def as "function"),
    # and a JS method, so no single hard-coded kind can satisfy every row.
    comm = _comm("c", {})
    comm["symbols"] = [
        {"file": "app/wiki.py", "kind": "class", "line": 3, "name": "Wiki"},
        {"file": "app/wiki.py", "kind": "function", "line": 4, "name": "__init__"},
        {"file": "app/view.js", "kind": "method", "line": 9, "name": "render"},
    ]
    return {"units": [_unit("unit-01", "u", [comm])]}


def test_every_checklist_entry_carries_the_store_kind_inline_and_on_disk(tmp_path):
    changed = ["app/wiki.py", "app/view.js"]
    inline = bcc.build(_store_shaped_comms(), MANIFEST, changed)[0]["symbols"]
    assert inline, "the fixture produced no checklist rows"
    assert all(r.get("kind") for r in inline), inline
    assert {(r["name"], r["kind"]) for r in inline} == {
        ("Wiki", "class"), ("__init__", "function"), ("render", "method")}

    d = tmp_path / "checklists"
    entries = bcc.externalize_checklists(
        bcc.build(_store_shaped_comms(), MANIFEST, changed), str(d))
    on_disk = json.loads(Path(entries[0]["checklistPath"]).read_text(encoding="utf-8"))
    assert on_disk == inline, "the checklist file lost or reshaped the kind"


def test_a_dunder_entry_resolves_to_the_store_symbol_id_exactly():
    """What `graph records` does with the citation: symbol_id(corpus, file, kind, name) must be
    the id the store minted for that entity. A guessed "method" here is the refused batch."""
    symbol_id = _store_symbol_id()
    store_id = symbol_id("corpus", "app/wiki.py", "function", "__init__")
    row = next(r for r in bcc.build(_store_shaped_comms(), MANIFEST, ["app/wiki.py"])[0]["symbols"]
               if r["name"] == "__init__")
    assert symbol_id("corpus", row["file"], row["kind"], row["name"]) == store_id
    assert row["kind"] != "method"


def test_no_unit_matching_returns_empty_which_is_legal_and_not_an_error():
    comms = {"units": [_unit("unit-01", "u", [_comm("c", {"a.py": ["f"]})])]}
    assert bcc.build(comms, MANIFEST, ["nothing.py"]) == []


def _files(tmp_path, changed_bytes=b"a.py\n", manifest=None, deleted_bytes=b""):
    comms = tmp_path / "c.json"
    comms.write_text(json.dumps(
        {"units": [_unit("unit-01", "u", [_comm("c", {"a.py": ["f"]})])]}), encoding="utf-8")
    man = tmp_path / "m.json"
    man.write_text(json.dumps(MANIFEST if manifest is None else manifest), encoding="utf-8")
    ch = tmp_path / "changed.txt"
    ch.write_bytes(changed_bytes)
    dl = tmp_path / "deleted.txt"
    dl.write_bytes(deleted_bytes)
    return ["--communities", str(comms), "--manifest", str(man),
            "--changed", str(ch), "--deleted", str(dl), "--out", str(tmp_path / "o.json")]


def test_a_utf8_bom_on_changed_does_not_eat_the_first_path(tmp_path):
    """Without stripping it, the first path silently carries the BOM, matches nothing, and is
    reported inside the ordinary unmatched-files note -- a wrong answer dressed as an expected one."""
    argv = _files(tmp_path, changed_bytes=b"\xef\xbb\xbfa.py\n")
    assert bcc.main(argv) == 0
    out = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert out and out[0]["changedFiles"] == ["a.py"], "the BOM was carried into the path"


def test_an_empty_changed_file_is_refused_rather_than_producing_an_empty_result(tmp_path):
    argv = _files(tmp_path, changed_bytes=b"\n   \n")
    assert bcc.main(argv) == 1
    assert not (tmp_path / "o.json").exists()


def test_a_manifest_with_no_pages_is_refused_rather_than_silently_affecting_nothing(tmp_path):
    """Otherwise a wrong --manifest is a fully successful run in which no agent is told to touch
    any page, and the run reports success."""
    argv = _files(tmp_path, manifest={"pages": {}})
    assert bcc.main(argv) == 1


def test_a_stale_output_is_gone_even_when_the_run_FAILS(tmp_path):
    """The version of this test that ran the SUCCESS path could not fail: `open(..., "w")`
    truncates, so removing the file first changed nothing observable. The property that matters
    is the failure path - the fixed path must not still hold the previous run's answer for Step 4
    to pick up."""
    argv = _files(tmp_path, changed_bytes=b"\n")   # refused at the empty-changed guard
    out = tmp_path / "o.json"
    out.write_text('[{"key": "stale"}]', encoding="utf-8")
    assert bcc.main(argv) == 1
    assert not out.exists(), "a failed run left the previous run's output on the fixed path"


def test_a_successful_run_replaces_a_stale_output(tmp_path):
    argv = _files(tmp_path)
    out = tmp_path / "o.json"
    out.write_text('[{"key": "stale"}]', encoding="utf-8")
    assert bcc.main(argv) == 0
    assert json.loads(out.read_text(encoding="utf-8"))[0]["key"] == "unit-01"


def test_main_writes_the_file_and_exits_zero_on_a_real_mapping(tmp_path):
    argv = _files(tmp_path)
    assert bcc.main(argv) == 0
    assert json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))[0]["key"] == "unit-01"


@pytest.mark.parametrize("missing", ["--communities", "--manifest", "--changed", "--deleted",
                                     "--out"])
def test_every_argument_is_required(missing, tmp_path):
    args = {"--communities": "c", "--manifest": "m", "--changed": "ch", "--deleted": "dl",
            "--out": "o"}
    argv = [a for k, v in args.items() if k != missing for a in (k, v)]
    with pytest.raises(SystemExit):
        bcc.main(argv)


# ------------------------------------------------------- --checklist-dir (E-0743)
# The checklist moves out of the workflow PAYLOAD and onto disk, because the workflow
# interpolates every symbol into the refresh agent's prompt text and a catch-up run's 4636 symbols
# are 94.9% of a 535KB payload against a 512KB cap.

def _mixed_comms():
    """Two units: one with symbols in a changed file, one attached only by prefix (so: none).

    The second shape is not contrived -- it is 15 of the 24 units in the pending catch-up run,
    every unit whose changed files are docs/config/fixtures that carry no graph symbols.
    """
    return {"units": [
        _unit("unit-01", "has-symbols", [_comm("c", {"a.py": ["f1", "f2"]})]),
        _unit("unit-02", "prefix-only", [_comm("d", {"z.py": ["g"]}, prefixes=("docs",))]),
    ]}


def _run_with_checklists(tmp_path, extra=()):
    comms = tmp_path / "c.json"
    comms.write_text(json.dumps(_mixed_comms()), encoding="utf-8")
    man = tmp_path / "m.json"
    man.write_text(json.dumps(MANIFEST), encoding="utf-8")
    ch = tmp_path / "changed.txt"
    ch.write_text("a.py\ndocs/guide.md\n", encoding="utf-8")
    dl = tmp_path / "deleted.txt"
    dl.write_text("", encoding="utf-8")
    out = tmp_path / "o.json"
    argv = ["--communities", str(comms), "--manifest", str(man),
            "--changed", str(ch), "--deleted", str(dl), "--out", str(out), *extra]
    assert bcc.main(argv) == 0
    return {c["key"]: c for c in json.loads(out.read_text(encoding="utf-8"))}


def test_checklist_dir_replaces_symbols_with_a_path_and_a_count(tmp_path):
    """The whole point: the rows leave the payload. An entry still carrying `symbols` alongside
    the path would save nothing -- the payload is the thing being cut."""
    d = tmp_path / "checklists"
    got = _run_with_checklists(tmp_path, ["--checklist-dir", str(d)])
    entry = got["unit-01"]
    assert "symbols" not in entry, "the symbols array is still riding the payload"
    assert entry["symbolCount"] == 2
    assert entry["checklistPath"] == str((d / "unit-01.json").resolve())
    assert set(entry) == {"key", "label", "descriptor", "changedFiles", "affectedPages",
                          "checklistPath", "symbolCount"}


def test_the_checklist_file_round_trips_the_original_symbol_rows(tmp_path):
    """A path to a file that lost or reshaped the rows is worse than the inline list it replaced:
    the agent reads it, believes it, and the coverage gate grades against rows that never existed."""
    d = tmp_path / "checklists"
    got = _run_with_checklists(tmp_path, ["--checklist-dir", str(d)])
    inline = {c["key"]: c for c in bcc.build(_mixed_comms(), MANIFEST, ["a.py", "docs/guide.md"])}
    on_disk = json.loads(Path(got["unit-01"]["checklistPath"]).read_text(encoding="utf-8"))
    assert on_disk == inline["unit-01"]["symbols"]
    assert [s["name"] for s in on_disk] == ["f1", "f2"]
    assert set(on_disk[0]) == {"name", "file", "line", "kind"}


def test_an_empty_checklist_stays_inline_and_gets_no_path(tmp_path):
    """The template's no-symbols fallback fires on `{{^symbols}}`. Handing this unit a path to an
    empty file replaces "refresh by reading the diff" with "go read nothing" -- and it is the
    MAJORITY shape (15 of 24 units on the live corpus), so getting it wrong is silent and wide."""
    d = tmp_path / "checklists"
    got = _run_with_checklists(tmp_path, ["--checklist-dir", str(d)])
    entry = got["unit-02"]
    assert entry["changedFiles"] == ["docs/guide.md"], "fixture no longer exercises a prefix-only unit"
    assert entry["symbols"] == []
    assert "checklistPath" not in entry
    assert "symbolCount" not in entry
    assert not (d / "unit-02.json").exists(), "an empty checklist was written anyway"


def test_without_the_flag_the_output_is_exactly_what_it_was_before(tmp_path):
    """The flag is OPTIONAL and every existing caller omits it, so the no-flag path must be
    byte-identical to the pre-change shape -- which is precisely `build()`'s own output."""
    got = _run_with_checklists(tmp_path)
    expected = {c["key"]: c
                for c in bcc.build(_mixed_comms(), MANIFEST, ["a.py", "docs/guide.md"])}
    assert got == expected
    for entry in got.values():
        assert set(entry) == {"key", "label", "descriptor", "changedFiles",
                              "affectedPages", "symbols"}
        assert "checklistPath" not in entry
    assert not (tmp_path / "checklists").exists(), "a checklist dir appeared without the flag"


def test_the_checklist_dir_is_created_if_it_does_not_exist(tmp_path):
    d = tmp_path / "nested" / "checklists"
    assert not d.exists()
    _run_with_checklists(tmp_path, ["--checklist-dir", str(d)])
    assert (d / "unit-01.json").exists()


def test_a_key_that_would_escape_the_checklist_dir_is_refused(tmp_path):
    """Keys are `unit-NN`, so this cannot fire on real input -- which is exactly why a silent
    write outside DIR would go unnoticed if one ever did."""
    entries = [{"key": "../evil", "symbols": [{"name": "f", "file": "a.py", "line": 1}]}]
    with pytest.raises(ValueError):
        bcc.externalize_checklists(entries, str(tmp_path / "checklists"))


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_explicitly_empty_checklist_dir_is_refused_rather_than_silently_inline(empty, tmp_path):
    """`--checklist-dir "$VAR"` with $VAR unset expands to an empty argument, and a truthiness
    branch read that as "flag absent": the run emitted the inline payload, printed nothing about
    it, and the size reduction the runbook depends on just did not happen -- discovered only when
    the workflow rejected 535KB of args. The refusal has to be LOUD and the output file must not
    survive it, same as every other FATAL here."""
    comms = tmp_path / "c.json"
    comms.write_text(json.dumps(_mixed_comms()), encoding="utf-8")
    man = tmp_path / "m.json"
    man.write_text(json.dumps(MANIFEST), encoding="utf-8")
    ch = tmp_path / "changed.txt"
    ch.write_text("a.py\ndocs/guide.md\n", encoding="utf-8")
    dl = tmp_path / "deleted.txt"
    dl.write_text("", encoding="utf-8")
    out = tmp_path / "o.json"
    out.write_text('[{"key": "stale"}]', encoding="utf-8")
    argv = ["--communities", str(comms), "--manifest", str(man), "--changed", str(ch),
            "--deleted", str(dl), "--out", str(out), "--checklist-dir", empty]
    assert bcc.main(argv) == 1
    assert not out.exists(), (
        "the empty --checklist-dir was refused but the previous run's answer is still on the "
        "fixed path for Step 4 to pick up"
    )


def test_omitting_the_flag_is_still_the_supported_inline_path(tmp_path):
    """The refusal above is about an EXPLICIT empty value only. Absent stays absent: every caller
    that has never passed the flag must keep working, or the guard broke the default path."""
    got = _run_with_checklists(tmp_path)
    assert got["unit-01"]["symbols"], "the no-flag run stopped producing inline symbols"
    assert "checklistPath" not in got["unit-01"]


# ------------------------------------------------ symbol-less changed files (gh#486)
# A changed file that no unit claims by symbol OR by prefix used to vanish: its manifest pages never
# reached any refresh agent, and the coverage line read "0 page(s) affected" as if nothing was
# documented from it. The manifest reverse-index is keyed on paths, so it can resolve such a file
# with no unit at all.

_ENTRY_KEYS = {"key", "label", "descriptor", "changedFiles", "affectedPages", "symbols"}

ORPHAN_MANIFEST = {"pages": {
    "skills/x.md": {"type": "skill", "source_paths": ["skills/x/SKILL.md"]},
    "components/Alpha.md": {"type": "component", "source_paths": ["a.py", "README.md"]},
    "notes/Loose.md": {"type": "note", "source_paths": ["notes/loose.md"]},
    "components/Docs.md": {"type": "component", "source_paths": ["docs/guide.md"]},
    "index.md": {"type": "index"},
}}


def _one_unit(prefixes=()):
    return {"units": [_unit("unit-01", "u", [_comm("c", {"a.py": ["f"]}, prefixes=prefixes)])]}


def test_a_changed_file_no_unit_claims_still_reaches_the_page_that_documents_it():
    """The issue's own case. A SKILL.md carries no graph symbols and sits under no unit's prefix,
    and the manifest says a page is sourced from it -- the old build returned [] and that page was
    never refreshed, while the run reported success."""
    got = bcc.build(_one_unit(), ORPHAN_MANIFEST, ["skills/x/SKILL.md"])
    assert [c["key"] for c in got] == ["symbolless"], "the orphan's page reached no entry"
    entry = got[0]
    assert set(entry) == _ENTRY_KEYS
    assert entry["label"] == "symbol-less changed files"
    assert entry["descriptor"] == {"member_count": 0, "top_symbols": [], "top_prefixes": []}
    assert entry["changedFiles"] == ["skills/x/SKILL.md"]
    assert entry["affectedPages"] == ["skills/x.md"]
    assert entry["symbols"] == [], "empty symbols is what fires the diff-reading refresh mode"


def test_an_orphan_named_by_no_page_produces_no_symbolless_entry():
    """The control: recovery goes through the manifest, it does not attach every stray file to a
    catch-all entry. Without this, 'emit symbolless for every orphan' would pass the test above."""
    assert bcc.build(_one_unit(), ORPHAN_MANIFEST, ["nowhere/else.txt"]) == []
    got = bcc.build(_one_unit(), ORPHAN_MANIFEST, ["a.py", "nowhere/else.txt"])
    assert [c["key"] for c in got] == ["unit-01"]
    assert "nowhere/else.txt" not in got[0]["changedFiles"]


def test_an_orphan_whose_page_a_unit_already_owns_joins_that_units_changed_files():
    """The quieter half of gh#486: the page WAS refreshed (via a.py), but its agent was never told
    README.md changed too, so prose sourced from README.md was refreshed against a diff that did
    not include it. The page must still be owned exactly once."""
    got = bcc.build(_one_unit(), ORPHAN_MANIFEST, ["a.py", "README.md"])
    assert [c["key"] for c in got] == ["unit-01"], "an owned page leaked into a symbolless entry"
    assert got[0]["changedFiles"] == ["README.md", "a.py"]
    pages = [p for c in got for p in c["affectedPages"]]
    assert pages.count("components/Alpha.md") == 1


def test_owned_and_unowned_orphans_together_and_symbolless_is_emitted_last():
    comms = {"units": [
        _unit("unit-01", "u", [_comm("c", {"a.py": ["f"]})]),
        _unit("unit-02", "v", [_comm("d", {"z.py": ["g"]})]),
    ]}
    got = bcc.build(comms, ORPHAN_MANIFEST, ["a.py", "z.py", "README.md", "notes/loose.md"])
    assert [c["key"] for c in got] == ["unit-01", "unit-02", "symbolless"]
    assert got[0]["changedFiles"] == ["README.md", "a.py"]
    assert got[1]["changedFiles"] == ["z.py"]
    assert got[2]["changedFiles"] == ["notes/loose.md"]
    assert got[2]["affectedPages"] == ["notes/Loose.md"]
    pages = [p for c in got for p in c["affectedPages"]]
    assert len(pages) == len(set(pages)), "a page was claimed twice"


def test_a_prefix_attached_file_is_not_stolen_into_symbolless():
    """Pass 2 already gave docs/guide.md a unit; it is not an orphan and must stay there."""
    got = bcc.build(_one_unit(prefixes=("docs",)), ORPHAN_MANIFEST, ["a.py", "docs/guide.md"])
    assert [c["key"] for c in got] == ["unit-01"]
    assert got[0]["changedFiles"] == ["a.py", "docs/guide.md"]
    assert "components/Docs.md" in got[0]["affectedPages"]


def test_a_unit_already_keyed_symbolless_is_refused_loudly():
    """The key is a filename downstream (refresh-<key>.json). Two entries sharing it would have one
    agent's output overwrite the other's."""
    comms = {"units": [_unit("symbolless", "u", [_comm("c", {"a.py": ["f"]})])]}
    with pytest.raises(ValueError, match="symbolless"):
        bcc.build(comms, ORPHAN_MANIFEST, ["a.py", "skills/x/SKILL.md"])


def _orphan_cli(tmp_path, changed_text, extra=()):
    comms = tmp_path / "c.json"
    comms.write_text(json.dumps(_one_unit()), encoding="utf-8")
    man = tmp_path / "m.json"
    man.write_text(json.dumps(ORPHAN_MANIFEST), encoding="utf-8")
    ch = tmp_path / "changed.txt"
    ch.write_text(changed_text, encoding="utf-8")
    dl = tmp_path / "deleted.txt"
    dl.write_text("", encoding="utf-8")
    out = tmp_path / "o.json"
    argv = ["--communities", str(comms), "--manifest", str(man),
            "--changed", str(ch), "--deleted", str(dl), "--out", str(out), *extra]
    assert bcc.main(argv) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_the_symbolless_entry_stays_inline_under_checklist_dir(tmp_path):
    """Its symbols are empty by construction, so a path would replace 'read the diff' with 'read
    nothing' -- the same rule every other empty-checklist unit already follows."""
    d = tmp_path / "checklists"
    got = {c["key"]: c for c in _orphan_cli(tmp_path, "a.py\nskills/x/SKILL.md\n",
                                             ["--checklist-dir", str(d)])}
    entry = got["symbolless"]
    assert entry["symbols"] == []
    assert "checklistPath" not in entry
    assert "symbolCount" not in entry
    assert not (d / "symbolless.json").exists()
    assert "checklistPath" in got["unit-01"], "fixture no longer externalizes a real unit"


def test_the_coverage_line_counts_recovered_pages_and_the_note_names_only_pageless_files(
        tmp_path, capsys):
    """Before gh#486 this scenario printed '0 unit(s), 0 of 2 changed file(s) mapped, 0 page(s)
    affected' -- a zero that read as 'nothing documented here' when a page plainly was."""
    _orphan_cli(tmp_path, "skills/x/SKILL.md\nloose.txt\n")
    err = capsys.readouterr().err
    assert ("1 page(s) affected; 1 symbol-less file(s) resolved via the manifest, "
            "1 page(s) reached only that way") in err
    assert "changedCommunities: 0 unit(s)," in err, "the symbolless entry was counted as a unit"
    note = [ln for ln in err.splitlines() if ln.startswith("NOTE:")]
    assert any("loose.txt" in ln and "no manifest page" in ln for ln in note), note
    assert not any("skills/x/SKILL.md" in ln for ln in note), "a recovered file is still reported lost"


def test_only_unclaimed_files_are_resolved_through_the_manifest():
    """The third rule is for files NO unit claimed. Without the restriction, a file unit-02 owns by
    symbol is handed to unit-01 as well whenever unit-01 wins a page naming both, and the coverage
    line counts it as a symbol-less recovery. Every other fixture here keeps the two sets apart,
    which is why dropping the restriction survived review."""
    comms = {"units": [
        _unit("unit-01", "u", [_comm("c", {"a.py": ["f"], "a2.py": ["h"]})]),
        _unit("unit-02", "v", [_comm("d", {"b.py": ["g"]})]),
    ]}
    man = {"pages": {"components/Shared.md": {"type": "component",
                                              "source_paths": ["a.py", "a2.py", "b.py"]}}}
    stats = {}
    got = {c["key"]: c for c in bcc.build(comms, man, ["a.py", "a2.py", "b.py"], stats)}
    assert got["unit-01"]["affectedPages"] == ["components/Shared.md"]
    assert got["unit-01"]["changedFiles"] == ["a.py", "a2.py"], "a claimed file was re-attributed"
    assert "symbolless" not in got
    assert stats == {"recovered_pages": 0, "resolved_orphans": 0}


def test_the_symbolless_entry_is_sorted_and_its_counts_are_not_interchangeable():
    """Every other orphan fixture recovers one page from one file, so swapping the two counters
    or dropping the sort passed. Here six unowned pages come from five files, both named out of
    order: set iteration order is hash-seeded, so a fixture of two or three items kills an
    unsorted output only some of the time."""
    files = ["skills/%s/SKILL.md" % n for n in ("q", "c", "x", "a", "m")]
    man = {"pages": {"components/P%d.md" % (9 - i): {"type": "component", "source_paths": [f]}
                     for i, f in enumerate(files)}}
    man["pages"]["flows/Zeta.md"] = {"type": "flow", "source_paths": [files[0]]}
    stats = {}
    got = bcc.build(_one_unit(), man, files, stats)
    assert [c["key"] for c in got] == ["symbolless"]
    assert got[0]["affectedPages"] == sorted(man["pages"])
    assert got[0]["changedFiles"] == sorted(files)
    assert stats == {"recovered_pages": 6, "resolved_orphans": 5}


# ------------------------------------------------------------ deleted files (gh#695)
# Only a refresh agent can deprecate a page, and it only touches its affectedPages. A page citing
# only deleted files that reached no entry was never deprecated and failed the golden gate's
# source-paths check, and a range of deletions alone was refused as an empty diff.

DELETION_MANIFEST = {"pages": {
    "services/App Service.md": {"type": "service", "source_paths": ["app/service.py"]},
    "components/Newmod.md": {"type": "component", "source_paths": ["app/newmod.py"]},
    "components/Mixed.md": {"type": "component",
                            "source_paths": ["app/service.py", "app/oldmod.py"]},
}}


def _service_unit(prefixes=("app",), extra_files=None):
    files = {"app/service.py": ["serve"], **(extra_files or {})}
    return {"units": [_unit("unit-01", "app", [_comm("c", files, prefixes=prefixes)])]}


def test_a_page_citing_a_deleted_file_reaches_the_unit_whose_prefix_covers_it():
    """The issue's repro: before the fix the only unit was unit-01 with App Service.md, and
    Newmod.md appeared in no entry."""
    got = bcc.build(_service_unit(), DELETION_MANIFEST, ["app/service.py"],
                    deleted=["app/newmod.py"])
    assert [c["key"] for c in got] == ["unit-01"]
    assert got[0]["affectedPages"] == [
        "components/Mixed.md", "components/Newmod.md", "services/App Service.md"]
    assert got[0]["changedFiles"] == ["app/service.py"], "a deleted path was handed out to read"


def test_a_page_no_unit_reaches_through_a_deleted_file_goes_to_symbolless():
    got = bcc.build(_service_unit(prefixes=()), DELETION_MANIFEST, ["app/service.py"],
                    deleted=["app/newmod.py"])
    assert [c["key"] for c in got] == ["unit-01", "symbolless"]
    assert got[1]["affectedPages"] == ["components/Newmod.md"]
    assert got[1]["changedFiles"] == [], "a deleted path was handed out to read"
    pages = [p for c in got for p in c["affectedPages"]]
    assert len(pages) == len(set(pages))


def test_a_deleted_file_a_stale_symbol_names_routes_its_page_but_adds_no_checklist_row():
    """An unpruned store can still carry the deleted file's symbols. They route the page to the
    unit that documented them, but nothing at TO carries them, so they are never checklist rows."""
    comms = _service_unit(prefixes=(), extra_files={"app/newmod.py": ["gone"]})
    got = bcc.build(comms, DELETION_MANIFEST, ["app/service.py"], deleted=["app/newmod.py"])
    assert [c["key"] for c in got] == ["unit-01"]
    assert "components/Newmod.md" in got[0]["affectedPages"]
    assert [s["name"] for s in got[0]["symbols"]] == ["serve"]
    assert got[0]["changedFiles"] == ["app/service.py"]


def test_a_page_citing_live_and_deleted_files_is_still_owned_exactly_once():
    got = bcc.build(_service_unit(), DELETION_MANIFEST, ["app/service.py"],
                    deleted=["app/oldmod.py"])
    pages = [p for c in got for p in c["affectedPages"]]
    assert pages.count("components/Mixed.md") == 1


def test_deleted_files_do_not_outvote_a_changed_one_for_page_ownership():
    """The owner re-verifies the page against the code still there. Two deleted files behind a
    page must not hand it to a unit with nothing left to read over the unit whose file changed."""
    comms = {"units": [
        _unit("unit-01", "app", [_comm("x", {"app/service.py": ["serve"]})]),
        _unit("unit-02", "lib", [_comm("y", {"lib/keep.py": ["k"]}, prefixes=("lib",))]),
    ]}
    man = {"pages": {"services/Svc.md": {"type": "service", "source_paths": [
        "app/service.py", "lib/old1.py", "lib/old2.py"]}}}
    got = {c["key"]: c for c in bcc.build(comms, man, ["app/service.py", "lib/keep.py"],
                                          deleted=["lib/old1.py", "lib/old2.py"])}
    assert got["unit-01"]["affectedPages"] == ["services/Svc.md"]
    assert got["unit-02"]["affectedPages"] == []
    assert got["unit-02"]["contextPages"] == [{"page": "services/Svc.md", "owner": "unit-01"}]


def test_deleted_files_decide_ownership_only_between_units_with_no_changed_file_on_the_page():
    comms = {"units": [
        _unit("unit-01", "one", [_comm("x", {"a.py": ["f"]}, prefixes=("gone1",))]),
        _unit("unit-02", "two", [_comm("y", {"b.py": ["g"]}, prefixes=("gone2",))]),
    ]}
    man = {"pages": {"components/Old.md": {"type": "component", "source_paths": [
        "gone1/x.py", "gone2/y.py", "gone2/z.py"]}}}
    got = {c["key"]: c for c in bcc.build(comms, man, ["a.py", "b.py"],
                                          deleted=["gone1/x.py", "gone2/y.py", "gone2/z.py"])}
    assert got["unit-02"]["affectedPages"] == ["components/Old.md"]
    assert got["unit-01"]["affectedPages"] == []


def test_a_path_in_both_lists_is_treated_as_changed():
    got = bcc.build(_service_unit(), DELETION_MANIFEST, ["app/service.py"],
                    deleted=["app/service.py"])
    assert got[0]["changedFiles"] == ["app/service.py"]


def test_a_deletion_only_range_is_a_valid_run_not_an_empty_diff(tmp_path, capsys):
    """Before the fix this was rc=1 with 'Step 2 produced no changed files' while FROM != TO."""
    argv = _files(tmp_path, changed_bytes=b"", manifest=DELETION_MANIFEST,
                  deleted_bytes=b"app/newmod.py\n")
    assert bcc.main(argv) == 0
    out = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert [(c["key"], c["changedFiles"], c["affectedPages"]) for c in out] == [
        ("symbolless", [], ["components/Newmod.md"])]
    assert "1 affected page(s) cite one of 1 deleted file(s)" in capsys.readouterr().err


def test_a_deletion_only_unit_that_owns_a_page_is_emitted_with_no_changed_files():
    got = bcc.build(_service_unit(), DELETION_MANIFEST, [], deleted=["app/newmod.py"])
    assert [(c["key"], c["changedFiles"], c["affectedPages"]) for c in got] == [
        ("unit-01", [], ["components/Newmod.md"])]
    assert got[0]["symbols"] == []


# ------------------------------------------------- a page owned by another unit (gh#696)
# Property 3 gives each page one owner. The unit that loses the tie keeps the file and its symbols
# but used to get affectedPages [], so its prompt said "likely all-new code".

def _split_units():
    return {"units": [
        _unit("unit-01", "one", [_comm("x", {"app/service.py": ["a"]})]),
        _unit("unit-02", "two", [_comm("y", {"app/service.py": ["b", "c", "d"]})]),
    ]}


def test_the_unit_that_loses_a_page_sees_it_as_read_only_context():
    got = {c["key"]: c for c in bcc.build(_split_units(), DELETION_MANIFEST, ["app/service.py"])}
    assert got["unit-01"]["affectedPages"] == ["components/Mixed.md", "services/App Service.md"]
    assert "contextPages" not in got["unit-01"], "the owner was shown its own pages as context"
    assert got["unit-02"]["affectedPages"] == []
    assert got["unit-02"]["contextPages"] == [
        {"page": "components/Mixed.md", "owner": "unit-01"},
        {"page": "services/App Service.md", "owner": "unit-01"},
    ]


def test_symbols_behind_a_page_owned_elsewhere_are_marked_covered_by_it():
    got = {c["key"]: c for c in bcc.build(_split_units(), DELETION_MANIFEST, ["app/service.py"])}
    assert {s["name"]: s.get("coveredBy") for s in got["unit-02"]["symbols"]} == {
        "b": "components/Mixed.md", "c": "components/Mixed.md", "d": "components/Mixed.md"}
    assert all("coveredBy" not in s for s in got["unit-01"]["symbols"])


def test_a_symbol_whose_file_the_unit_documents_itself_is_not_marked():
    """unit-02 owns Other.md, which cites the same file: its own page covers the symbol, so the
    marker would send the agent to a page it cannot edit for no reason."""
    man = {"pages": {**DELETION_MANIFEST["pages"],
                     "components/Other.md": {"type": "component",
                                             "source_paths": ["app/service.py", "app/two.py"]}}}
    comms = {"units": [
        _unit("unit-01", "one", [_comm("x", {"app/service.py": ["a"]})]),
        _unit("unit-02", "two", [_comm("y", {"app/service.py": ["b"], "app/two.py": ["e"]})]),
    ]}
    got = {c["key"]: c for c in bcc.build(comms, man, ["app/service.py", "app/two.py"])}
    assert got["unit-02"]["affectedPages"] == ["components/Other.md"]
    assert all("coveredBy" not in s for s in got["unit-02"]["symbols"])
    assert [c["page"] for c in got["unit-02"]["contextPages"]] == [
        "components/Mixed.md", "services/App Service.md"]


def test_the_covered_marker_survives_onto_the_checklist_file(tmp_path):
    entries = bcc.externalize_checklists(
        bcc.build(_split_units(), DELETION_MANIFEST, ["app/service.py"]), str(tmp_path / "ck"))
    unit2 = next(e for e in entries if e["key"] == "unit-02")
    on_disk = json.loads(Path(unit2["checklistPath"]).read_text(encoding="utf-8"))
    assert all(r["coveredBy"] == "components/Mixed.md" for r in on_disk)
    assert unit2["contextPages"][0]["owner"] == "unit-01"
