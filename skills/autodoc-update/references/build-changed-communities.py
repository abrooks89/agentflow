#!/usr/bin/env python3
"""Build the ``changedCommunities`` argument the update workflow hard-fails without.

WHY THIS FILE EXISTS. ``skills/autodoc-update/SKILL.md`` Step 3 described this derivation in
prose -- group changed files by community, map to affected pages through the manifest index --
and no command performed it. So the one argument the update workflow raises without was produced
by nothing in the file. In a runbook whose fenced blocks execute in order, an obligation stated
only in prose is not under-specified, it is absent.

WHAT IT READS, all produced earlier in the same run:
  --communities  .source/.communities.json   the work units regenerated at TO (Step 3.5)
  --manifest     .manifest.json              pages -> {type, source_paths}
  --changed      Step 2's --diff-filter=d list, repo-relative, one path per line
  --deleted      Step 2's --diff-filter=D list, same format

WHAT IT EMITS: exactly what update-workflow.js destructures --
  [{key, label, descriptor:{member_count, top_symbols, top_prefixes},
    changedFiles:[repoRelPath], affectedPages:[autodocRelPath],
    symbols:[{name, file, line, kind}]}]
plus, LAST and only when it has a page, one entry keyed ``symbolless`` (label "symbol-less changed
files", zeroed descriptor, ``symbols: []``) carrying the pages that only property 1's third rule
reached. A communities unit already using that key is refused with ValueError. Two keys appear
only when they have content (property 7): ``contextPages`` on an entry, and ``coveredBy`` on a
checklist row.

WITH --checklist-dir the symbols move OUT of that payload and onto disk: each entry's array is
written to DIR/<key>.json and the entry carries ``checklistPath`` + ``symbolCount`` in place of
``symbols``. Nothing else about the output changes, and WITHOUT the flag the output is
byte-identical to what it has always been. The reason is size, not taste: the workflow
interpolates every symbol into the refresh agent's PROMPT TEXT, and the pending catch-up run is
4636 symbols across 24 units -- 535KB of payload, 94.9% of it symbols, against the 512KB ceiling
the Workflow tool applies to a baked variant script. Delivering the checklist by path changes no
agent task and no control flow: refresh agents already read the taxonomy, the changed sources and
the affected pages from paths they are handed.

An EMPTY symbols array gets NO checklistPath and keeps its inline empty shape, deliberately. The
refresh template's no-symbols fallback fires on ``{{^symbols}}``, so handing such a unit a path to
an empty file would replace "refresh by reading the diff" with an instruction to go read nothing --
and on the measured corpus that is 15 of 24 units, changed silently.

SEVEN PROPERTIES THAT ARE NOT OBVIOUS, each one a defect this file used to have:

1. A SYMBOL-LESS CHANGED FILE IS NOT DROPPED. Docs, config, fixtures and data files carry no graph
   symbols, and an earlier version attached a file to a unit ONLY through that unit's symbol list.
   Measured on the live corpus that discarded 107 of 254 changed files, and with them 10 pages the
   manifest explicitly documents as sourced from those very files. Step 3's own prose already
   specified the remedy -- "attaches to the community whose ``top_prefixes`` covers it" -- and it
   was simply unimplemented. ``_prefix_owner`` implements it: longest matching prefix wins, so a
   file under a deep prefix is not claimed by a shallower unit that also matches. A file neither
   rule claims is resolved THIRD straight from the manifest reverse-index (gh#486): a page some
   unit already owns stays with that unit, whose changedFiles gains the file so its agent knows
   that prose changed too; a page no unit owns goes to the ``symbolless`` entry, whose empty
   symbols fire the refresh's read-the-diff mode. Only a file no page names is left over, and
   it is named in the NOTE.

2. affectedPages IS COMPUTED FROM THE UNIT'S WHOLE changedFiles SET, symbol-bearing or not. The
   manifest reverse-index is keyed on PATHS; routing it through the symbol intersection threw away
   its reach for exactly the files least likely to have symbols and most likely to be documented.

3. A PAGE IS ASSIGNED TO AT MOST ONE UNIT. Unit packing means several units can legitimately touch
   files behind one page -- measured, 44 of 111 affected pages were claimed by two or more units at
   once. Each entry becomes one refresh agent, so that is two agents independently emitting a
   record for the same page in the same run, and ``graph records`` sees the later one. Ownership
   goes to the unit with the most changed files behind that page, then the most deleted ones,
   ties broken by unit key so the result is deterministic rather than dict-order.

4. MATCHING IS EXACT ON CASE AND SEPARATOR, deliberately. Both sides come from the same git diff
   and the same graph extraction, so normalising would paper over a real disagreement between them
   rather than fix it. It is asserted by a test so that a future "helpful" case-fold is a red
   suite, not a silent re-attribution.

5. A UTF-8 BOM ON --changed IS STRIPPED. Without it the first path silently carries \\ufeff, maps to
   nothing, and is reported inside the ordinary "carry no graph symbols" note -- a wrong answer
   wearing an expected one's clothes.

6. A DELETED FILE ROUTES ITS PAGES, NOT ITSELF. Only a refresh agent can deprecate a page, and it
   only touches pages in its affectedPages, so a page citing a deleted file that reaches no entry
   outlives its code and fails the golden gate's source-paths check. Deleted files go through the
   same three rules as changed ones -- symbol (an unpruned store can still name a deleted file),
   prefix, manifest -- and weigh in page ownership, but never join changedFiles
   (there is nothing at TO to read) and never add checklist rows. So a deletion-only range is a
   valid run whose entries carry pages and no changed files.

7. A PAGE OWNED ELSEWHERE IS SHOWN, NOT HIDDEN. Property 3 gives each page one owner, and the
   units that lose the tie still have the page's files in changedFiles and their symbols in the
   checklist. Handed no page, such an agent read "no pages -- likely all-new code" and minted
   duplicate records for symbols the page already documents. Each losing unit gets the page in
   ``contextPages`` as ``{page, owner}``, read-only, and each of its checklist rows whose file only
   a page owned elsewhere cites carries ``coveredBy``: that page, for the agent to read before it
   decides whether the symbol is already documented there.
"""
import argparse
import json
import os
import sys

SYMBOLLESS_KEY = "symbolless"


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_changed(path):
    """Step 2's list. ``utf-8-sig`` so a BOM cannot become part of the first path."""
    with open(path, encoding="utf-8-sig") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def _prefix_owner(path, prefix_index):
    """The unit whose declared prefix best covers *path*, or None.

    Longest match wins: a file under ``skills/autodoc-init/references`` belongs to the unit that
    declared that prefix, not to one that merely declared ``skills``.
    """
    best_len, best_key = 0, None
    for prefix, key in prefix_index:
        if not prefix:
            continue
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            if len(prefix) > best_len:
                best_len, best_key = len(prefix), key
    return best_key


def build(communities, manifest, changed, stats=None, deleted=()):
    """Return the changedCommunities list. Pure, so the tests drive it directly.

    *stats*, if a dict, receives ``recovered_pages`` (pages only pass 4 reached through a changed
    file) and ``resolved_orphans`` (unclaimed changed files that named any page) for the coverage
    line. *deleted* is Step 2's deleted list (property 6).
    """
    changed = set(changed)
    # The two diff filters are disjoint; if a caller disagrees, the path exists at TO and an agent
    # can read it, so it is treated as changed.
    deleted = set(deleted) - changed
    pages = manifest.get("pages", {})
    page_sources = {p: set(meta.get("source_paths") or []) for p, meta in pages.items()}

    units = communities.get("units", [])
    if any(u.get("key") == SYMBOLLESS_KEY for u in units):
        # The key is a filename downstream (refresh-<key>.json); two entries sharing it would
        # have one agent's output overwrite the other's.
        raise ValueError("a communities unit already uses the reserved key %r" % SYMBOLLESS_KEY)

    # Pass 1 - symbol attribution, and collect each unit's declared prefixes for pass 2.
    per_unit = {}
    prefix_index = []
    order = []
    for unit in units:
        key = unit.get("key")
        order.append(key)
        files, symbols = set(), []
        descriptor = {"member_count": 0, "top_symbols": [], "top_prefixes": []}
        for comm in unit.get("communities", []):
            d = comm.get("descriptor") or {}
            # Units are a PACKING of several communities, so the descriptor is a merge. Taking
            # communities[0] would describe a fraction of what the agent is asked to review.
            descriptor["member_count"] += int(d.get("member_count") or 0)
            for k in ("top_symbols", "top_prefixes"):
                for v in d.get(k) or []:
                    if v not in descriptor[k]:
                        descriptor[k].append(v)
            for sym in comm.get("symbols") or []:
                f = sym.get("file")
                if not f:
                    continue
                files.add(f)
                if f in changed:
                    # kind is carried verbatim from the artifact, which took it from the store's
                    # own entity: a refresh citation is keyed on (file, kind, name), so an agent
                    # left to guess it (a Python dunder is "function", not "method") gets the
                    # whole records batch refused (gh#527).
                    symbols.append({"name": sym.get("name"), "file": f, "line": sym.get("line"),
                                    "kind": sym.get("kind")})
        per_unit[key] = {
            "key": key,
            "label": unit.get("label"),
            "descriptor": descriptor,
            "changed": set(files & changed),
            "deleted": set(files & deleted),
            "symbols": symbols,
        }
        for p in descriptor["top_prefixes"]:
            prefix_index.append((p, key))

    # Pass 2 - the documented fallback for files no symbol names.
    for field, paths in (("changed", changed), ("deleted", deleted)):
        attributed = {f for u in per_unit.values() for f in u[field]}
        for f in sorted(paths - attributed):
            owner = _prefix_owner(f, prefix_index)
            if owner is not None:
                per_unit[owner][field].add(f)

    # Pass 3 - pages, each owned by exactly one unit (property 3). Every other unit that touched
    # the page is remembered, so it can be shown the page it does not own (property 7).
    # Ranked by changed files first: the owner re-verifies the page against what is still there,
    # so deleted files only decide between units with no changed file behind the page.
    claims, claimants = {}, {}
    for key in order:
        mine, gone = per_unit[key]["changed"], per_unit[key]["deleted"]
        if not mine and not gone:
            continue
        for page, srcs in page_sources.items():
            rank = (len(srcs & mine), len(srcs & gone))
            if any(rank):
                claimants.setdefault(page, set()).add(key)
                prev = claims.get(page)
                if prev is None or rank > prev[0] or (rank == prev[0] and str(key) < str(prev[1])):
                    claims[page] = (rank, key)
    owned = {}
    for page, (_rank, key) in claims.items():
        owned.setdefault(key, []).append(page)

    # Pass 4 - the third attribution rule, after symbol and prefix: files neither claimed are
    # resolved straight from the manifest reverse-index (property 1). A page a unit already owns
    # keeps its owner, who is told the orphan changed too; a page nobody owns goes to the one
    # SYMBOLLESS_KEY entry.
    attributed = {f for u in per_unit.values() for f in u["changed"] | u["deleted"]}
    orphans = (changed | deleted) - attributed
    loose_files, loose_pages, recovered, resolved = set(), set(), set(), set()
    for page, srcs in page_sources.items():
        hit = srcs & orphans
        if not hit:
            continue
        resolved |= hit & changed
        if page in claims:
            owner = per_unit[claims[page][1]]
            owner["changed"] |= hit & changed
            owner["deleted"] |= hit & deleted
        else:
            loose_files |= hit & changed
            loose_pages.add(page)
            if hit & changed:
                recovered.add(page)
    if stats is not None:
        stats["recovered_pages"] = len(recovered)
        stats["resolved_orphans"] = len(resolved)

    out = []
    for key in order:
        u = per_unit[key]
        mine = owned.get(key, [])
        # A unit with only deleted files still has work when it owns a page: deprecating it.
        if not u["changed"] and not mine:
            continue
        entry = {
            "key": u["key"],
            "label": u["label"],
            "descriptor": u["descriptor"],
            "changedFiles": sorted(u["changed"]),
            "affectedPages": sorted(mine),
            "symbols": u["symbols"],
        }
        elsewhere = sorted(p for p, keys in claimants.items()
                           if key in keys and claims[p][1] != key)
        if elsewhere:
            entry["contextPages"] = [{"page": p, "owner": claims[p][1]} for p in elsewhere]
            for sym in u["symbols"]:
                if any(sym["file"] in page_sources[p] for p in mine):
                    continue
                covering = [p for p in elsewhere if sym["file"] in page_sources[p]]
                if covering:
                    sym["coveredBy"] = covering[0]
        out.append(entry)
    if loose_pages:
        out.append({
            "key": SYMBOLLESS_KEY,
            "label": "symbol-less changed files",
            "descriptor": {"member_count": 0, "top_symbols": [], "top_prefixes": []},
            "changedFiles": sorted(loose_files),
            "affectedPages": sorted(loose_pages),
            "symbols": [],
        })
    return out


def externalize_checklists(entries, checklist_dir):
    """Move each entry's ``symbols`` array onto disk, in place. Returns *entries*.

    Only called when --checklist-dir is given, so the no-flag output stays byte-identical. A unit
    whose symbols array is EMPTY is skipped entirely and keeps that empty array: see the module
    docstring -- the template's fallback keys on ``{{^symbols}}``, and a path to an empty file is
    not the same instruction.
    """
    os.makedirs(checklist_dir, exist_ok=True)
    for entry in entries:
        symbols = entry.get("symbols") or []
        if not symbols:
            continue
        key = str(entry["key"])
        if key != os.path.basename(key) or key in (".", ".."):
            # The key becomes a filename. Community keys are `unit-NN`, so this cannot fire on
            # real input -- but a key carrying a separator would write outside DIR silently.
            raise ValueError(
                "community key %r cannot be used as a checklist filename" % (entry["key"],))
        path = os.path.abspath(os.path.join(checklist_dir, key + ".json"))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(symbols, fh, indent=2)
        del entry["symbols"]
        entry["checklistPath"] = path
        entry["symbolCount"] = len(symbols)
    return entries


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--communities", required=True, help=".source/.communities.json")
    ap.add_argument("--manifest", required=True, help=".manifest.json")
    ap.add_argument("--changed", required=True, help="Step 2's --diff-filter=d list")
    ap.add_argument("--deleted", required=True, help="Step 2's --diff-filter=D list")
    ap.add_argument("--out", required=True, help="where to write the JSON list")
    ap.add_argument("--checklist-dir", default=None,
                    help="OPTIONAL: write each unit's symbols array to DIR/<key>.json and carry "
                         "checklistPath + symbolCount in the output instead of the array itself. "
                         "Omit for the historical inline shape; passing it EMPTY is an error, "
                         "not a way to ask for that shape.")
    args = ap.parse_args(argv)

    # BEFORE any work, not just before the write: `open(..., "w")` truncates anyway, so
    # removing it next to the write was dead code. What it must prevent is a run that FAILS
    # partway leaving the previous run's file on this fixed path, where Step 4 reads it as this
    # run's answer.
    if os.path.exists(args.out):
        os.remove(args.out)

    # An EXPLICITLY EMPTY --checklist-dir is a mistake, never a request for the inline shape.
    # `--checklist-dir "$VAR"` with $VAR unset expands to `--checklist-dir ""`, and a truthiness
    # branch reads that as "flag absent": the run emits the historical inline payload, says
    # nothing about it, and the size reduction the runbook depends on silently does not happen --
    # which is only discovered when the workflow rejects 535KB of args. Refuse it here, after the
    # stale-output removal so this failure clears that file like every other one. The flag being
    # ABSENT is untouched: that is still the documented inline path.
    if args.checklist_dir is not None and not args.checklist_dir.strip():
        print("FATAL: --checklist-dir was passed an empty value. That is an unset shell variable, "
              "not a request for the inline shape -- omit the flag entirely to get that.",
              file=sys.stderr)
        return 1

    changed = read_changed(args.changed)
    deleted = read_changed(args.deleted)
    if not changed and not deleted:
        # Distinguishable on purpose: an empty diff is a Step 2 problem (FROM == TO, or a diff
        # that wrote nothing), not a mapping that legitimately found no community. Both would
        # otherwise emit []. A range of deletions alone is not empty: its pages need deprecating.
        print("FATAL: --changed and --deleted are both empty; Step 2 produced no diff, so there "
              "is nothing to group. If FROM == TO the run should have stopped at Step 2.",
              file=sys.stderr)
        return 1

    manifest = load(args.manifest)
    if not manifest.get("pages"):
        # Without this a wrong --manifest is a fully successful run with every affectedPages
        # empty: no agent is told to touch any page, and the run reports success.
        print("FATAL: --manifest has no `pages` index, so no page can ever be marked affected "
              "and every refresh agent would be told there is nothing to update.", file=sys.stderr)
        return 1

    stats = {}
    result = build(load(args.communities), manifest, changed, stats, deleted)

    # PRESENCE, not truthiness: the empty value is already refused above, and branching on `is not
    # None` is what keeps it refused rather than quietly re-routed to the inline shape.
    if args.checklist_dir is not None:
        externalize_checklists(result, args.checklist_dir)
        externalized = [c for c in result if "checklistPath" in c]
        print("checklists: %d of %d unit(s) delivered by path under %s (%d symbol(s)); "
              "%d empty checklist(s) stay inline"
              % (len(externalized), len(result), os.path.abspath(args.checklist_dir),
                 sum(c["symbolCount"] for c in externalized),
                 len(result) - len(externalized)), file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)

    mapped = {f for c in result for f in c["changedFiles"]}
    pages = [p for c in result for p in c["affectedPages"]]
    # The symbolless entry is not a unit; counting it would print "1 unit(s)" for a diff no
    # community matched, the same kind of misreport gh#486 is about.
    units = sum(1 for c in result if c["key"] != SYMBOLLESS_KEY)
    gone = set(deleted)
    citing_deleted = sum(1 for p in pages
                         if gone & set(manifest["pages"][p].get("source_paths") or []))
    print("changedCommunities: %d unit(s), %d of %d changed file(s) mapped, %d page(s) affected; "
          "%d symbol-less file(s) resolved via the manifest, %d page(s) reached only that way; "
          "%d affected page(s) cite one of %d deleted file(s)"
          % (units, len(mapped), len(changed), len(pages),
             stats["resolved_orphans"], stats["recovered_pages"],
             citing_deleted, len(deleted)), file=sys.stderr)
    assert len(pages) == len(set(pages)), "a page was claimed by two units; property 3 is broken"
    if not result:
        print("NOTE: zero units matched. That is legal - pass [] explicitly - but check that "
              "--changed holds repo-relative paths in the form the graph recorded.",
              file=sys.stderr)
    unmapped = sorted(set(changed) - mapped)
    if unmapped:
        # Named, not counted: after all three rules these are files no unit claims by symbol or
        # prefix AND no manifest page names, which is a gap worth seeing, not a reassuring total.
        print("NOTE: %d changed file(s) matched no unit and no manifest page: %s%s"
              % (len(unmapped), ", ".join(unmapped[:5]),
                 " ..." if len(unmapped) > 5 else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
