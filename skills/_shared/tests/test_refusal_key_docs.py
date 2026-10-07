"""The refusal-key taxonomy and its documentation are one contract; keep them in step (gh#246).

WHAT EVERY REFUSAL PROMISES. Every value in `registry.RULES` opens with `REGISTRY.md`, and most go
on to name a section — `REGISTRY.md #Queue: claim only from your own lane`; the rest cite the file
alone. So a lane that hits a refusal is being sent somewhere to read the rule. The first gate
holds that every cited section exists as a heading in `docs/REGISTRY.md`, so a new key cannot cite
a section nobody wrote and ship green. gh#205 was the same defect pointing the other way, and it
reached master.

AND THE EXIT CODES ARE THE OTHER HALF. `docs/REGISTRY.md` tells callers to branch on the exit
code, and every loop skill tells agents which codes are retryable. The second gate holds that every
code the CLI can return has a row in the exit table, with the codes derived from the SOURCE rather
than a list, because that table is maintained by hand and had drifted: **`1` had no row at all**
while being the one code whose two producers carry opposite recoveries. The third holds that the
exit-1 row names both of them.

AND THE KEYS A SKILL NAMES (gh#413). A key a tracked `skills/<name>/SKILL.md` tells a lane to branch
on is one the lane will hold and grep for, so the fourth gate holds that every such key is also
named in `docs/REGISTRY.md`, matched as a whole token. It runs one way only: a key no skill names
is "fix the call", its message is the whole answer, and documenting it anyway stays allowed.

Every check here is a pure function of its inputs plus a thin test that feeds it the real ones,
so the red-proofs at the bottom pass synthetic inputs rather than monkeypatching the filesystem.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from agentflow.registry import RULES
from agentflow.tracked_paths import tracked_files

SKILLS_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = SKILLS_ROOT.parent
DOC = SUITE_ROOT / "docs" / "REGISTRY.md"
SOURCE = SKILLS_ROOT / "_shared" / "agentflow" / "registry.py"

#: The producers exit 1 is KNOWN to have, asserted as an exact set rather than a floor. A third
#: clause returning 1 is the same defect class that made gh#246's one-row proposal harmful, and a
#: `>= 2` floor was blind to it: review added a `TimeoutError: return 1` handler and the whole
#: 2997-test suite stayed green while the table described two of three outcomes.
EXIT_1_PRODUCERS = frozenset({"atomic_io.ReplaceRetryExhausted", "PermissionError"})


def _doc() -> str:
    text = DOC.read_text(encoding="utf-8")
    assert len(text) > 5000, f"{DOC} is {len(text)} bytes - this gate would be reading a stub"
    return text


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# Pure readers
# --------------------------------------------------------------------------------------------
def headings(text: str) -> set[str]:
    """Every markdown heading, squashed to letters and digits so `## Queue` matches ``## `Queue`...``."""
    return {re.sub(r"[^a-z0-9]", "", h.lower())
            for h in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", text)}


def cited_sections(rules: dict[str, str]) -> dict[str, str]:
    """{key: section} for every rule message that names one. A message may cite the file alone."""
    out = {}
    for key, message in rules.items():
        m = re.match(r"REGISTRY\.md\s*#([A-Za-z0-9-]+)", message)
        if m:
            out[key] = m.group(1)
    return out


def resolves(section: str, known: set[str]) -> bool:
    """A citation resolves when a heading STARTS with it, not when one merely contains it.

    Unbounded containment was the first spelling and review was right to flag it: `#Agents` would
    have resolved against "Liveness sweep ... every wake" if that sentence happened to contain the
    word, which is a false pass in a gate whose whole job is to catch a citation pointing nowhere.
    A prefix is the honest reading of `## Queue` followed by a dash and a summary, and every
    section cited when it was tightened resolved under it - measured, not assumed.
    """
    squashed = re.sub(r"[^a-z0-9]", "", section.lower())
    return any(h.startswith(squashed) for h in known)


def dangling_citations(rules: dict[str, str], doc: str) -> list[str]:
    known = headings(doc)
    return sorted(
        f"{key} -> #{section}"
        for key, section in cited_sections(rules).items()
        if not resolves(section, known)
    )


def _int_exits(node: ast.AST) -> list[tuple[int, ast.AST]]:
    """(code, statement) for every integer the subtree hands back as a process exit code."""
    out = []
    for stmt in ast.walk(node):
        if (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, int)):
            out.append((stmt.value.value, stmt))
        # `sys.exit(2)` and `raise SystemExit(2)` are the same contract by another spelling, and
        # collecting only `ast.Return` meant either one shipped a caller-visible code invisibly.
        call = None
        if isinstance(stmt, ast.Call):
            call = stmt
        elif isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call):
            call = stmt.exc
        if call is not None and call.args and isinstance(call.args[0], ast.Constant):
            target = ast.unparse(call.func)
            if target in ("sys.exit", "exit", "SystemExit") and isinstance(call.args[0].value, int):
                out.append((call.args[0].value, stmt))
    return out


def dispatch_targets(main_fn: ast.FunctionDef) -> set[str]:
    """The helpers `main` hands its exit code to - `return _do_validate(...)` and friends.

    Read off `main` rather than pattern-matched on a name prefix, so a renamed or newly added
    dispatch target is picked up without editing this file.
    """
    names = set()
    for stmt in ast.walk(main_fn):
        if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            if isinstance(func, ast.Name):
                names.add(func.id)
    return names


def exit_codes(source: str) -> dict[int, list[str]]:
    """{code: [producer labels]} for every exit code the CLI can hand a caller, by AST.

    DERIVED, not listed: the whole defect this file guards is a hand-maintained table drifting
    from the code it describes.

    THE FIRST VERSION OF THIS READ ONLY `main`'S EXCEPT-CLAUSES, and review proved that was the
    exception HALF of the contract rather than the contract. `main` returns whatever its dispatch
    helpers return, and `_do_validate` already hands back `2` from a plain `if problems:` branch -
    verified live, `validate` against a damaged registry exits 2 through that path. So a
    `return 6` added to any helper, or a `sys.exit(6)` anywhere, shipped with no row and a green
    suite. Three channels are collected now: `main`'s handlers, `main`'s own body, and each
    dispatch target's body.

    That also derives `0` from the helpers, which is why no success code is named by hand.
    """
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    main_fn = functions.get("main")
    if main_fn is None:
        return {}

    found: dict[int, list[str]] = {}

    def record(code: int, label: str) -> None:
        found.setdefault(code, []).append(label)

    handled = set()
    for block in ast.walk(main_fn):
        if not isinstance(block, ast.Try):
            continue
        for handler in block.handlers:
            label = ast.unparse(handler.type) if handler.type else "bare except"
            for code, stmt in _int_exits(handler):
                record(code, label)
                handled.add(id(stmt))

    for code, stmt in _int_exits(main_fn):
        if id(stmt) not in handled:
            record(code, "main body")

    for name in sorted(dispatch_targets(main_fn)):
        target = functions.get(name)
        if target is None:
            continue
        for code, _stmt in _int_exits(target):
            record(code, f"{name} body")

    return found


def exit_table_rows(doc: str) -> set[int]:
    """The codes the exit table gives a row to. Read from the table, never from prose.

    A code named in a sentence elsewhere in the file does NOT count. `5` is a live example of why
    the distinction matters rather than of the defect: it has a row here AND is described at
    length in the `deploy-lock assert` row of the verb table further down. A reader that counted
    prose would have reported `5` documented either way, so it could not have told you whether
    the table was complete.
    """
    rows: set[int] = set()
    in_table = False
    for line in doc.splitlines():
        if re.match(r"\|\s*code\s*\|\s*means\s*\|", line):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            m = re.match(r"\|\s*`(\d+)`\s*\|", line)
            if m:
                rows.add(int(m.group(1)))
    return rows


def undocumented_exit_codes(source: str, doc: str) -> list[int]:
    return sorted(set(exit_codes(source)) - exit_table_rows(doc))


def names_key(key: str, text: str) -> bool:
    """True when `text` names `key` as a WHOLE token.

    Underscores are part of the token, so `deploy_held` is not named by `deploy_held_x` and
    `held` is not named by `deploy_held`. A substring rule would let a longer key stand in for a
    shorter one, and the gate below would pass on a doc that never names the key a lane holds.
    """
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])", text) is not None


def skill_keys_missing_from_doc(rules: dict[str, str], skills: list[str], doc: str) -> list[str]:
    """Keys some skill names that the doc does not. One direction only: extra doc keys are fine."""
    return sorted(
        key for key in rules
        if any(names_key(key, text) for text in skills) and not names_key(key, doc)
    )


def _skill_texts() -> list[str]:
    # Python-glob semantics on the index, not the `skills/*/SKILL.md` pathspec, whose `*`
    # crosses `/` and would pull vendored skills in (see `tracked_files`).
    files = tracked_files(SUITE_ROOT, lambda r: re.fullmatch(r"skills/[^/]+/SKILL\.md", r) is not None)
    names = {f.parent.name for f in files}
    assert {"lead-dev", "steward", "auditor", "orchestrator"} <= names, (
        f"the skill corpus is missing a fleet skill ({sorted(names)}) - a gate over a narrowed "
        "corpus passes by reading less, so it fails here instead"
    )
    return [f.read_text(encoding="utf-8") for f in files]


# --------------------------------------------------------------------------------------------
# The gates, against the real tree
# --------------------------------------------------------------------------------------------
def test_every_section_a_refusal_cites_exists_in_the_doc() -> None:
    """A refusal sends a lane somewhere to read the rule. The somewhere must be real."""
    cited = cited_sections(RULES)
    assert len(cited) > 40, (
        f"only {len(cited)} of {len(RULES)} refusals cite a section - if the citation convention "
        "changed, this gate is measuring the wrong thing and must be re-pointed, not deleted"
    )
    dangling = dangling_citations(RULES, _doc())
    assert not dangling, (
        "these refusal messages cite a section of docs/REGISTRY.md that does not exist, so the "
        "lane they refuse is sent to a heading it will not find:\n  " + "\n  ".join(dangling)
    )


def test_every_exit_code_the_cli_can_return_has_a_row_in_the_exit_table() -> None:
    """The table is a contract callers branch on; a code with no row is an unanswerable verdict."""
    doc = _doc()
    assert exit_table_rows(doc), (
        "the exit table was not found in docs/REGISTRY.md - this gate cannot pass by reading "
        "nothing, so it fails here instead"
    )
    source = _source()
    missing = undocumented_exit_codes(source, doc)
    produced = exit_codes(source)
    assert not missing, (
        f"exit code(s) {missing} are returned by registry.main but have no row in the exit "
        "table. Produced by: "
        + "; ".join(f"{c} <- {', '.join(produced.get(c, ['success']))}" for c in missing)
    )


def test_every_refusal_key_a_skill_names_is_named_in_the_doc() -> None:
    """A key a skill tells a lane to branch on is one the lane will hold and grep for.

    A key no skill names is "fix the call", and its message is the whole answer, so this runs
    one way only: documenting a key no skill names stays allowed.
    """
    missing = skill_keys_missing_from_doc(RULES, _skill_texts(), _doc())
    assert not missing, (
        "these refusal keys are named in a skill but not in docs/REGISTRY.md, so a lane holding "
        "one has nowhere to look it up - add a row to the refusal-key table:\n  "
        + "\n  ".join(missing)
    )


def test_exit_1_is_documented_as_TWO_outcomes_because_its_recoveries_are_opposite() -> None:
    """THE REASON THIS FILE EXISTS, and the half gh#246 got wrong.

    The issue proposed a single row: "a write was attempted and did not land after the retry
    window", recovery "re-run". That is true of `ReplaceRetryExhausted` and FALSE of the
    `PermissionError` clause gh#138 added, whose own message says *not a transient - re-running
    will fail the same way*. One row would have documented an unattended lane into the infinite
    retry gh#138 exists to remove.
    """
    producers = set(exit_codes(_source()).get(1, []))
    assert producers == set(EXIT_1_PRODUCERS), (
        f"the producers of exit 1 are now {sorted(producers)}, documented as "
        f"{sorted(EXIT_1_PRODUCERS)}. THE ROW BELOW DESCRIBES A FIXED SET OF OUTCOMES: an "
        "addition needs a third clause written into it, a removal makes it overstate. Re-read the "
        "row and change both together - never just widen this set. A `>= 2` floor stood here "
        "first and a third producer walked straight through it with the whole suite green"
    )
    row = next((ln for ln in _doc().splitlines() if re.match(r"\|\s*`1`\s*\|", ln)), None)
    assert row, "no row for exit 1 in the exit table"
    # The MEANS cell specifically, not the whole row: the recovery cell names both outcomes too,
    # so a whole-row search is satisfied by a row that explains only one of them and then tells
    # the reader what to do about both. Asserted where the meaning is actually stated.
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert len(cells) == 3, f"the exit table is code/means/recovery; this row has {len(cells)}: {row}"
    means = cells[1].lower()
    assert "did not land" in means, (
        "the exit-1 MEANS cell must name the transient half in the words the CLI prints, so an "
        "operator can match the message they are holding: " + cells[1]
    )
    assert "permission denied" in means, (
        "the exit-1 MEANS cell must name the PERMANENT half too, or the table describes one "
        "outcome and a lane retries the other forever: " + cells[1]
    )


# --------------------------------------------------------------------------------------------
# Red-proofs. A gate that cannot fail proves nothing about the green above it.
# --------------------------------------------------------------------------------------------
#: The fixture carries a SECOND, row-shaped table after the exit table on purpose. Without it the
#: reader's "stop at the end of the table" guard is unexercised: every `| `n` |` line in the file
#: would be inside the exit table anyway, so removing the guard changes no answer and a mutant
#: that deletes it survives. That is measured, not hypothetical - it survived the first run.
_DOC_FIXTURE = """
## Queue

| code | means | the recovery |
|---|---|---|
| `0` | done | proceed |
| `2` | schema | fix the call |

Prose elsewhere mentioning `5` must not count as a row. Neither may another table's rows:

| `9` | a different table entirely | not an exit code |
"""

_DISPATCH_WITH_SEVEN = """
def main():
    try:
        return run()
    except Boom:
        return 7
"""

#: A handler outside `main`, for the same reason: without it, narrowing the walk to `main` and
#: walking the whole module give identical answers on every other fixture.
_HANDLER_OUTSIDE_MAIN = """
def helper():
    try:
        return work()
    except Whatever:
        return 99


def main():
    try:
        return run()
    except Boom:
        return 7
"""

_DISPATCH_ONE_CODE_TWO_CLAUSES = """
def main():
    try:
        return run()
    except Retryable:
        return 1
    except PermissionError:
        return 1
"""


def test_the_citation_reader_catches_a_dangling_section_and_passes_a_real_one() -> None:
    assert dangling_citations({"bad": "REGISTRY.md #NoSuchSection: x"}, _DOC_FIXTURE) == [
        "bad -> #NoSuchSection"]
    assert dangling_citations({"ok": "REGISTRY.md #Queue: x"}, _DOC_FIXTURE) == []
    # Asserted on `cited_sections` and not only through `dangling_citations`: a widened regex that
    # matched the file-only form with an EMPTY section reports no dangling citation either, because
    # "" is a substring of every heading. The absence has to be checked where it is decided.
    assert "filewide" not in cited_sections({"filewide": "REGISTRY.md: no section cited"}), (
        "a message citing the file alone names no section and must not enter the cited set"
    )
    assert dangling_citations({"filewide": "REGISTRY.md: no section cited"}, _DOC_FIXTURE) == []
    # A heading that merely MENTIONS the word does not resolve a citation to it. Containment was
    # the first spelling; this is the false pass it allowed. The heading below CONTAINS "queue"
    # without starting with it, which is the only shape that tells a prefix rule from a
    # containment rule - a fixture whose headings simply lack the word passes under both, and the
    # first version of this red-proof did exactly that and let the mutant live.
    mentions_only = "\n## The queue lane discipline, discussed elsewhere\n\nprose\n"
    assert dangling_citations({"q": "REGISTRY.md #Queue: x"}, mentions_only) == ["q -> #Queue"]
    assert resolves("Queue", headings("## `Queue` - claimable work, disjoint lanes")), (
        "the real doc spells its headings as a backticked name then a dash and a summary, so a "
        "prefix must still resolve or this gate reds on every cited section at once"
    )


def test_the_exit_table_reader_sees_only_table_rows() -> None:
    assert exit_table_rows(_DOC_FIXTURE) == {0, 2}, (
        "the reader must stop at the end of the exit table - the fixture's trailing `| `9` |` row "
        "belongs to another table and is not an exit code"
    )
    assert exit_table_rows("no table here, just `| `9` |` in a sentence") == set()


def test_the_exit_code_deriver_reads_the_dispatch_not_the_helpers() -> None:
    assert exit_codes(_DISPATCH_WITH_SEVEN) == {7: ["Boom"]}
    assert exit_codes("def helper():\n    return 0\n") == {}, (
        "a bare `return 0` outside main's handlers is not an exit-code contract"
    )
    assert exit_codes(_HANDLER_OUTSIDE_MAIN) == {7: ["Boom"]}, (
        "a helper's own except-clause return is not part of the CLI's exit contract; only "
        "`main`'s dispatch is"
    )
    assert exit_codes(_DISPATCH_ONE_CODE_TWO_CLAUSES) == {
        1: ["Retryable", "PermissionError"]}, "two clauses on one code must both be reported"


def test_the_exit_gate_catches_a_code_with_no_row() -> None:
    """The real defect, replayed on synthetic inputs: a code the CLI hands back and the table
    skips."""
    assert undocumented_exit_codes(_DISPATCH_WITH_SEVEN, _DOC_FIXTURE) == [7]
    assert undocumented_exit_codes("def main():\n    try:\n        return run()\n"
                                   "    except Boom:\n        return 2\n", _DOC_FIXTURE) == []


def test_the_gate_catches_the_THREE_channels_review_found_escaping_it() -> None:
    """Each of these shipped green against the first version of the deriver, which read only
    `main`'s except-clauses. Every one is a code a caller really sees."""
    via_helper = """
def _do_thing(p):
    if bad:
        return 6
    return 0


def main():
    try:
        return _do_thing(p)
    except Boom:
        return 2
"""
    assert undocumented_exit_codes(via_helper, _DOC_FIXTURE) == [6], (
        "a dispatch helper's own return IS the process exit code - `_do_validate` already hands "
        "back 2 that way, verified live against a damaged registry"
    )

    via_main_body = """
def main():
    if early:
        return 8
    try:
        return run()
    except Boom:
        return 2
"""
    assert undocumented_exit_codes(via_main_body, _DOC_FIXTURE) == [8]

    via_sys_exit = """
import sys


def main():
    try:
        if worse:
            sys.exit(9)
        return run()
    except Boom:
        return 2
"""
    assert undocumented_exit_codes(via_sys_exit, _DOC_FIXTURE) == [9], (
        "`sys.exit(n)` is the same contract by another spelling"
    )

    via_raise = """
def main():
    try:
        raise SystemExit(4)
    except Boom:
        return 2
"""
    assert 4 in exit_codes(via_raise), "`raise SystemExit(n)` is too"


def test_the_deriver_labels_each_code_with_where_it_came_from() -> None:
    """The label is what a reader uses to find the clause; a code with no provenance sends them
    grepping the whole module."""
    labelled = exit_codes("""
def _do_thing(p):
    return 6


def main():
    if early:
        return 8
    try:
        return _do_thing(p)
    except Boom:
        return 2
""")
    assert labelled == {6: ["_do_thing body"], 8: ["main body"], 2: ["Boom"]}


def test_the_key_matcher_counts_only_whole_tokens() -> None:
    """A substring-only occurrence is not a mention: the gate would pass on the wrong key."""
    assert names_key("deploy_held", "refused `deploy_held`: another builder holds it")
    assert names_key("deploy_held", "deploy_held")
    assert not names_key("deploy_held", "refused `deploy_held_x` instead")
    assert not names_key("deploy_held", "refused `xdeploy_held` instead")
    assert not names_key("held", "refused `deploy_held`")
    assert not names_key("id_format", "the id1format key")


def test_the_skill_key_gate_runs_one_way_and_names_what_is_missing() -> None:
    rules = {"named_both": "m", "named_skill": "m", "named_doc": "m", "named_none": "m"}
    skills = ["branch on `named_both` or `named_skill`", "and `named_skill_extra` is another key"]
    doc = "the doc names `named_both` and `named_doc`, and `named_skill_extra` only as a substring"
    assert skill_keys_missing_from_doc(rules, skills, doc) == ["named_skill"]
