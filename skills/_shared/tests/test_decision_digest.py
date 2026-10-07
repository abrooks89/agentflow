"""Tests for the "Waiting on you" decision queue at the top of the steward's digest.

Covers: grouping by the decision required (two parked PRs held on one question become one group,
and the question itself joins it), an unavailable or truncated source rendered as such and never
as zero, the parked-PR ledger reported absent when its file is missing, each source reader over
tmp fixtures (registry Questions, drafted specs at the promotion preconditions, the open-issue
read with untriaged / needs-decision / closure-debt views, the PR ledger), the CLI's markers and
its ``--write`` path, and the dev-manager prose order: the groups come before the question pump.

Every read here is a tmp fixture or an injected runner. Nothing touches a live data root, a
tracker or the network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentflow import decision_digest as dd
from agentflow import wiki as wiki_module
from agentflow.evidence import Reading, State
from agentflow.decision_digest import (
    END_MARKER,
    START_MARKER,
    Item,
    build,
    group_items,
)

SKILLS_ROOT = Path(__file__).resolve().parents[2]
DEV_MANAGER_SKILL = SKILLS_ROOT / "dev-manager" / "SKILL.md"
STEWARD_SKILL = SKILLS_ROOT / "steward" / "SKILL.md"


def _empty_readers(**over):
    """Every source read in full and empty, with *over* replacing the named readers."""
    readers = {
        name: (lambda n=name: Reading.empty([], source=dd.SOURCE_LABELS[n]))
        for name in dd.SOURCE_ORDER
    }
    readers.update(over)
    return readers


def _pr(number: int, cites: tuple = (), epic: str = "e") -> Item:
    return Item(kind="parked-pr", ref=f"PR #{number}", title=f"epic {epic}", cites=cites)


# --------------------------------------------------------------------------- #
# Grouping by the decision required
# --------------------------------------------------------------------------- #
def test_two_prs_held_on_one_question_become_one_group():
    prs = [_pr(11, ("Q-0007",)), _pr(12, ("Q-0007",))]
    readers = _empty_readers(**{"parked-prs": lambda: Reading.ok(prs, source="parked PRs")})
    section = build("demo", readers=readers)
    assert len(section.groups) == 1
    group = section.groups[0]
    assert group.key == "Q-0007"
    assert [i.ref for i in group.items] == ["PR #11", "PR #12"]
    text = section.render()
    assert text.count("Q-0007") >= 1
    assert "2 items" in text


def test_the_question_and_the_prs_it_holds_collapse_into_one_group():
    q = Item(kind="question", ref="Q-0007", title="hold or merge?", cites=("Q-0007",))
    readers = _empty_readers(
        questions=lambda: Reading.ok([q], source="registry questions"),
        **{"parked-prs": lambda: Reading.ok([_pr(11, ("Q-0007",))], source="parked PRs")},
    )
    groups = build("demo", readers=readers).groups
    assert [g.key for g in groups] == ["Q-0007"]
    assert groups[0].decision.startswith("Answer Q-0007")
    assert len(groups[0].items) == 2


def test_prs_on_different_questions_stay_apart_and_uncited_ones_batch():
    groups = group_items(
        [
            _pr(1, ("Q-0001",)),
            _pr(2, ("Q-0002",)),
            _pr(3),
            _pr(4),
        ]
    )
    keys = [g.key for g in groups]
    assert keys == ["Q-0001", "Q-0002", "batch:parked-pr"]
    assert [i.ref for i in groups[2].items] == ["PR #3", "PR #4"]


def test_a_blocker_id_groups_like_a_question_id():
    groups = group_items([_pr(1, ("B-0003",)), _pr(2, ("B-0003",))])
    assert [g.key for g in groups] == ["B-0003"]


def test_needs_decision_issues_are_one_decision_each_not_a_batch():
    items = [
        Item(kind="needs-decision", ref="gh#5", title="a"),
        Item(kind="needs-decision", ref="gh#6", title="b"),
    ]
    assert [g.key for g in group_items(items)] == ["gh#5", "gh#6"]


def test_batchable_kinds_each_collapse_to_one_group_with_a_count():
    items = [Item(kind="closure-debt", ref=f"gh#{n}", title="t") for n in (1, 2, 3)]
    items += [Item(kind="spec", ref=f"spec s{n}", title="t") for n in (1, 2)]
    groups = group_items(items)
    assert [(g.key, len(g.items)) for g in groups] == [
        ("batch:closure-debt", 3),
        ("batch:spec", 2),
    ]
    assert "3" in groups[0].decision and "2" in groups[1].decision


# --------------------------------------------------------------------------- #
# Unavailable / truncated / absent: never zero
# --------------------------------------------------------------------------- #
def test_an_unavailable_source_renders_as_unavailable_never_as_zero():
    readers = _empty_readers(
        questions=lambda: Reading.unavailable("registry.md missing", source="registry questions")
    )
    text = build("demo", readers=readers).render()
    line = next(ln for ln in text.splitlines() if ln.startswith("- registry questions"))
    assert "UNAVAILABLE" in line and "registry.md missing" in line
    assert not re.search(r"\b0\b", line), line
    assert "Nothing is waiting on you" not in text
    assert "could not be read in full" in text


def test_a_truncated_source_renders_its_count_as_a_floor():
    items = [Item(kind="spec", ref="spec a", title="A")]
    readers = _empty_readers(
        specs=lambda: Reading.truncated(items, "1 spec unreadable", source="drafted specs")
    )
    text = build("demo", readers=readers).render()
    line = next(ln for ln in text.splitlines() if ln.startswith("- drafted specs"))
    assert "TRUNCATED" in line and "at least 1" in line and "1 spec unreadable" in line
    assert "spec a" in text, "a truncated reading's items are still shown"


def test_a_reader_that_raises_is_reported_unavailable_with_the_exception():
    def boom():
        raise RuntimeError("tracker exploded")

    text = build("demo", readers=_empty_readers(untriaged=boom)).render()
    line = next(ln for ln in text.splitlines() if ln.startswith("- untriaged issues"))
    assert "UNAVAILABLE" in line and "RuntimeError" in line and "tracker exploded" in line


def test_a_reader_returning_a_non_reading_is_refused():
    with pytest.raises(TypeError):
        build("demo", readers=_empty_readers(specs=lambda: []))


def test_all_sources_complete_and_empty_says_nothing_waiting():
    text = build("demo", readers=_empty_readers()).render()
    assert "Nothing is waiting on you" in text


def test_parked_pr_source_is_absent_when_the_ledger_is_missing(tmp_path):
    reading = dd.read_parked_prs(tmp_path / "status" / dd.PR_LEDGER_FILENAME)
    assert reading.state is State.UNAVAILABLE
    assert reading.coverage.startswith("absent")
    text = build("demo", readers=_empty_readers(**{"parked-prs": lambda: reading})).render()
    line = next(ln for ln in text.splitlines() if ln.startswith("- parked PRs"))
    assert "absent" in line and not re.search(r"\b0\b", line)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_render_is_wrapped_in_exactly_one_pair_of_markers():
    text = build("demo", readers=_empty_readers()).render()
    assert text.startswith(START_MARKER + "\n")
    assert text.endswith(END_MARKER + "\n")
    assert text.count(START_MARKER) == 1 and text.count(END_MARKER) == 1


def test_a_title_carrying_a_marker_or_newline_cannot_fake_lines():
    evil = Item(kind="spec", ref="spec x", title=f"ok\n{END_MARKER}\n- fake line")
    readers = _empty_readers(specs=lambda: Reading.ok([evil], source="drafted specs"))
    text = build("demo", readers=readers).render()
    assert text.count(END_MARKER) == 1
    assert "\n- fake line" not in text


# --------------------------------------------------------------------------- #
# Source readers over tmp fixtures
# --------------------------------------------------------------------------- #
_REGISTRY = """# Registry

## Agents

| agent | status | work | phase | heartbeat | eta | waiting_on |
|---|---|---|---|---|---|---|

## Queue

| id | work_item | lane | claimed_by | state | waiting_on | notes |
|---|---|---|---|---|---|---|

## Questions

### Q-0001
- state: open
- urgency: halted
- raised_by: jr-dev-1
- raised_at: 2026-09-01T10:00Z
- question: Which export format?
- default: tarball

### Q-0002
- state: open
- urgency: advisory
- raised_by: jr-dev-2
- raised_at: 2026-09-01T10:00Z
- question: Advisory one
- default: x
- assumption: y

### Q-0003
- state: answered
- urgency: waiting
- raised_by: jr-dev-2
- raised_at: 2026-09-01T10:00Z
- question: Already answered
- default: x

### Q-0004
- state: open
- urgency: waiting
- raised_by: lead-dev
- raised_at: 2026-09-01T09:00Z
- question: Merge policy?
- default: hold

## Blockers

| id | raised_by | owner | work | state | description |
|---|---|---|---|---|---|

## Directives
"""


def test_read_questions_keeps_open_halted_and_waiting_only(tmp_path):
    path = tmp_path / "registry.md"
    path.write_text(_REGISTRY, encoding="utf-8")
    reading = dd.read_questions(path)
    assert reading.state is State.VALUE
    assert [i.ref for i in reading.value] == ["Q-0001", "Q-0004"]
    assert reading.value[0].cites == ("Q-0001",)


def test_read_questions_on_a_missing_registry_is_unavailable(tmp_path):
    reading = dd.read_questions(tmp_path / "registry.md")
    assert reading.state is State.UNAVAILABLE


def _spec(dir_: Path, name: str, state: str, queue_class: str, files: bool) -> None:
    body = f"---\ntitle: T {name}\nslug: {name}\ncurrent_state: {state}\nqueue_class: {queue_class}\n---\n\n# x\n"
    if files:
        body += "\n## Files\n\n- `skills/a.py`\n"
    (dir_ / f"2026-09-01-{name}.md").write_text(body, encoding="utf-8")


def test_read_drafted_specs_applies_the_promotion_preconditions(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    _spec(specs, "ready", "drafted", "fleet-buildable", True)
    _spec(specs, "nofiles", "drafted", "fleet-buildable", False)
    _spec(specs, "local", "drafted", "local-tooling", True)
    _spec(specs, "approved", "approved-for-autonomous", "fleet-buildable", True)
    reading = dd.read_drafted_specs(specs)
    assert reading.state is State.VALUE
    assert [i.ref for i in reading.value] == ["spec ready"]


def test_read_drafted_specs_reports_an_unreadable_spec_as_truncated(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    _spec(specs, "ready", "drafted", "fleet-buildable", True)
    (specs / "2026-09-01-broken.md").write_text("---\n: [unclosed\n---\n", encoding="utf-8")
    reading = dd.read_drafted_specs(specs)
    assert reading.state is State.TRUNCATED
    assert "broken" in reading.coverage
    assert [i.ref for i in reading.value] == ["spec ready"]


def test_read_drafted_specs_on_a_missing_directory_is_unavailable(tmp_path):
    assert dd.read_drafted_specs(tmp_path / "nope").state is State.UNAVAILABLE


def _gh_runner(issues: list, prs: list | None = None, fail: bool = False):
    calls = []

    def run(argv):
        calls.append(argv)
        if fail:
            return 1, "", "gh: network down"
        if argv[1] == "issue":
            limit = int(argv[argv.index("--limit") + 1])
            return 0, json.dumps(issues[:limit]), ""
        limit = int(argv[argv.index("--limit") + 1])
        return 0, json.dumps((prs or [])[:limit]), ""

    run.calls = calls
    return run


def _issue(n: int, labels=(), title="t") -> dict:
    return {
        "number": n,
        "title": title,
        "labels": [{"name": x} for x in labels],
        "body": "",
        "url": f"https://example.invalid/i/{n}",
    }


def test_open_issue_read_is_truncated_past_the_page():
    run = _gh_runner([_issue(n) for n in range(1, 5)])
    reading = dd.read_open_issues("o/r", kind="github-issues", cli="gh", page=3, runner=run)
    assert reading.state is State.TRUNCATED
    assert len(reading.value) == 3


def test_open_issue_read_failure_is_unavailable_and_propagates_to_every_view(tmp_path):
    run = _gh_runner([], fail=True)
    issues = dd.read_open_issues("o/r", kind="github-issues", cli="gh", page=10, runner=run)
    assert issues.state is State.UNAVAILABLE
    assert dd.untriaged_items(issues, tmp_path).state is State.UNAVAILABLE
    assert dd.needs_decision_items(issues).state is State.UNAVAILABLE
    debt = dd.closure_debt_items(
        issues, tmp_path, host_ref="o/r", kind="github-issues", cli="gh", runner=run
    )
    assert debt.state is State.UNAVAILABLE


def test_no_tracker_is_unavailable_not_empty():
    reading = dd.read_open_issues("o/r", kind="none", cli="none", page=10)
    assert reading.state is State.UNAVAILABLE


def test_issue_views_untriaged_and_needs_decision(tmp_path):
    run = _gh_runner(
        [
            _issue(1),
            _issue(2, ("triaged", "bug", "needs-decision")),
            _issue(3, ("triaged", "bug")),
        ]
    )
    issues = dd.read_open_issues("o/r", kind="github-issues", cli="gh", page=10, runner=run)
    untriaged = dd.untriaged_items(issues, tmp_path / "specs")
    assert [i.ref for i in untriaged.value] == ["gh#1"]
    nd = dd.needs_decision_items(issues)
    assert [i.ref for i in nd.value] == ["gh#2"]


def test_closure_debt_lists_open_issues_a_merged_pr_claims(tmp_path):
    prs = [
        {
            "number": 40,
            "title": "Fix it (gh#3)",
            "body": "",
            "mergedAt": "2026-09-02T00:00:00Z",
            "mergeCommit": {"oid": "abcdef1234"},
            "headRefName": "b",
        }
    ]
    run = _gh_runner([_issue(3, ("triaged", "bug")), _issue(4)], prs)
    issues = dd.read_open_issues("o/r", kind="github-issues", cli="gh", page=10, runner=run)
    debt = dd.closure_debt_items(
        issues, tmp_path / "specs", host_ref="o/r", kind="github-issues", cli="gh", runner=run
    )
    assert debt.state is State.VALUE
    assert [i.ref for i in debt.value] == ["gh#3"]
    assert "#40" in debt.value[0].detail


def test_parked_prs_from_the_ledger_keep_manual_only_and_cite_question_ids(tmp_path):
    ledger = tmp_path / dd.PR_LEDGER_FILENAME
    ledger.write_text(
        json.dumps(
            {
                "prs": [
                    {"pr": 11, "epic": "a", "merge": "manual", "question": "Q-0007"},
                    {"pr": 12, "epic": "b", "merge": "manual", "question": "Q-0007"},
                    {"pr": 13, "epic": "c", "merge": "auto"},
                ]
            }
        ),
        encoding="utf-8",
    )
    reading = dd.read_parked_prs(ledger)
    assert reading.state is State.VALUE
    assert [(i.ref, i.cites) for i in reading.value] == [
        ("PR #11", ("Q-0007",)),
        ("PR #12", ("Q-0007",)),
    ]
    groups = group_items(reading.value)
    assert [g.key for g in groups] == ["Q-0007"]


def test_parked_prs_ledger_of_an_unknown_shape_is_unavailable(tmp_path):
    ledger = tmp_path / dd.PR_LEDGER_FILENAME
    ledger.write_text('"just a string"', encoding="utf-8")
    assert dd.read_parked_prs(ledger).state is State.UNAVAILABLE


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _pillar(tmp_path: Path, digest: str) -> Path:
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    (root / "digest.md").write_bytes(digest.encode("utf-8"))
    return root


def test_cli_render_prints_the_marked_section(monkeypatch, capsys):
    monkeypatch.setattr(dd, "default_readers", lambda project: _empty_readers())
    assert dd.main(["render", "--project", "demo"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(START_MARKER) and out.rstrip("\n").endswith(END_MARKER)


def test_cli_render_write_inserts_the_region_and_keeps_the_narrative(tmp_path, monkeypatch, capsys):
    narrative = "# demo - fleet digest\r\n\r\nLast written: x\r\n"
    root = _pillar(tmp_path, narrative)
    monkeypatch.setattr(dd, "default_readers", lambda project: _empty_readers())
    assert dd.main(["render", "--project", "demo", "--write", "--wiki-root", str(root)]) == 0
    data = (root / "digest.md").read_bytes().decode("utf-8")
    assert data.startswith(START_MARKER)
    assert data.endswith(narrative)
    assert "wrote" in capsys.readouterr().out


def test_cli_rejects_a_bad_project_slug(capsys):
    assert dd.main(["render", "--project", "../x"]) == 2
    assert "decision digest" in capsys.readouterr().err


def test_cli_write_refuses_a_root_that_is_not_a_pillar(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dd, "default_readers", lambda project: _empty_readers())
    (tmp_path / "w").mkdir()
    assert (
        dd.main(["render", "--project", "demo", "--write", "--wiki-root", str(tmp_path / "w")]) == 2
    )


# --------------------------------------------------------------------------- #
# Prose: dev-manager presents the groups before the question pump; steward inserts it
# --------------------------------------------------------------------------- #
_PUMP = "### 1. QUESTION PUMP"


def _text(path: Path) -> str:
    assert path.is_file(), f"{path} not found - the skill moved, or this gate's path is stale"
    return path.read_text(encoding="utf-8")


def _section(text: str, head: str) -> str:
    start = text.find(head)
    assert start != -1, f"no {head!r} heading"
    end = text.find("\n### ", start + len(head))
    return text[start:] if end == -1 else text[start:end]


def test_dev_manager_presents_waiting_on_you_groups_before_the_question_pump():
    text = _text(DEV_MANAGER_SKILL)
    pump = text.find(_PUMP)
    assert pump != -1, f"dev-manager/SKILL.md has no {_PUMP!r} heading"
    section = " ".join(_section(text, "### 0.6 ").split())  # prose wraps mid-phrase
    assert "Waiting on you" in section, "§0.6 does not present the Waiting-on-you groups"
    assert text.find("Waiting on you") < pump
    assert re.search(r"before the (question )?pump", section, re.I)
    assert re.search(r"one batch answer per group", section, re.I)
    assert re.search(r"one explicit decision", section, re.I)
    assert re.search(r"grant", section, re.I), "a batch merge must name the autonomy grant path"


def _steward_digest_section() -> str:
    text = _text(STEWARD_SKILL)
    head = "## The digest"
    start = text.find(head)
    assert start != -1
    end = text.find("\n## ", start + len(head))
    return text[start:end]


def test_steward_renders_the_queue_after_the_narrative_is_written():
    # Regenerating the narrative rewrites the whole file, so a render run before it is erased
    # and the digest carries no queue until the next sweep - the window dev-manager reads in.
    section = _steward_digest_section()
    cmd = "python -m agentflow.decision_digest render --project <name> --write"
    assert section.count(cmd) == 1
    regenerate = section.find("Regenerate it, never append")
    write_it = section.find("Then write the")
    assert regenerate != -1 and write_it != -1
    assert section.find(cmd) > max(regenerate, write_it)
    flat = " ".join(section.split())
    assert "after the narrative is written" in flat
    assert "before you write the narrative" not in flat
    assert "never write either marker" in flat


def test_architecture_names_the_waiting_on_you_section():
    arch = SKILLS_ROOT.parent / "docs" / "ARCHITECTURE.md"
    assert "agentflow.decision_digest" in _text(arch)


def test_wiki_markers_are_the_digest_modules_markers():
    assert wiki_module.WAITING_START == START_MARKER
    assert wiki_module.WAITING_END == END_MARKER


# --------------------------------------------------------------------------- #
# Review fixes: unset queue_class, closure debt routed not closed, citation choice, links
# --------------------------------------------------------------------------- #
def test_a_drafted_spec_with_no_queue_class_passes_like_dispatchable_does(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    body = "---\ntitle: Unset\nslug: unset\ncurrent_state: drafted\n---\n\n## Files\n\n- `a.py`\n"
    (specs / "2026-09-01-unset.md").write_text(body, encoding="utf-8")
    _spec(specs, "typo", "drafted", "bug", True)
    reading = dd.read_drafted_specs(specs)
    assert reading.state is State.VALUE
    assert [i.ref for i in reading.value] == ["spec unset"]
    assert reading.value[0].link == "2026-09-01-unset.md"


def _claimed_issue_runner(comments: list):
    """A fake tracker with one open issue a merged PR claims, serving *comments* if asked."""
    prs = [
        {
            "number": 40,
            "title": "Fix it (gh#3)",
            "body": "",
            "mergedAt": "2026-09-02T00:00:00Z",
            "mergeCommit": {"oid": "abcdef1234"},
            "headRefName": "b",
        }
    ]
    base = _gh_runner([_issue(3, ("triaged", "bug"))], prs)
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[1:3] == ["api", "user"]:
            return 0, json.dumps({"login": "bot"}), ""
        if argv[1:3] == ["issue", "view"]:
            return 0, json.dumps({"comments": comments}), ""
        return base(argv)

    run.calls = calls
    return run


def test_closure_debt_is_routed_to_the_auditor_never_offered_as_a_close(tmp_path):
    # The auditor already judged this claim `remainder`: partly fixed, open on purpose. The
    # digest does not pay a comments read per claim to tell that apart, so the batch must not
    # offer a close at all - a yes would close an issue the auditor found not fully fixed.
    remainder = {
        "author": {"login": "bot"},
        "body": "Agentflow [auditor]\nChecked against: master abc1234; claimed fixed by #40\n"
        "Verdict: remainder",
    }
    run = _claimed_issue_runner([remainder])
    issues = dd.read_open_issues("o/r", kind="github-issues", cli="gh", page=10, runner=run)
    debt = dd.closure_debt_items(
        issues, tmp_path / "specs", host_ref="o/r", kind="github-issues", cli="gh", runner=run
    )
    assert not [c for c in run.calls if c[1:3] in (["issue", "view"], ["api", "user"])]
    assert [i.ref for i in debt.value] == ["gh#3"]
    assert debt.value[0].link == "https://example.invalid/i/3"
    groups = group_items(debt.value)
    decision = groups[0].decision
    assert "auditor" in decision
    assert not re.match(r"close\b", decision, re.I), decision
    text = build("demo", readers=_empty_readers(**{"closure-debt": lambda: debt})).render()
    assert "<https://example.invalid/i/3>" in text


def test_dev_manager_never_offers_to_close_claimed_fixes_and_treats_no_region_as_a_finding():
    section = " ".join(_section(_text(DEV_MANAGER_SKILL), "### 0.6 ").split())
    assert "close these shipped bugs" not in section
    assert re.search(r"auditor's to verify and close", section)
    assert re.search(r"no region is a finding", section)
    assert re.search(r'never "nothing waiting"', section)


def test_a_cited_open_question_wins_over_an_earlier_unrelated_id():
    q = Item(kind="question", ref="Q-0007", title="hold?", cites=("Q-0007",))
    pr = _pr(11, ("B-0009", "Q-0007"))
    groups = group_items([pr, q])
    assert [g.key for g in groups] == ["Q-0007"]
    assert groups[0].decision.startswith("Answer Q-0007")
    # with no open question among the ids, the first citation still decides
    assert [g.key for g in group_items([pr])] == ["B-0009"]


def test_an_untriaged_issue_naming_a_question_stays_in_the_triage_batch():
    run = _gh_runner(
        [
            _issue(1, title="follow-up to Q-0007"),
            _issue(2, ("triaged", "bug", "needs-decision"), title="blocked on Q-0007"),
        ]
    )
    issues = dd.read_open_issues("o/r", kind="github-issues", cli="gh", page=10, runner=run)
    untriaged = dd.untriaged_items(issues, Path("does-not-exist"))
    nd = dd.needs_decision_items(issues)
    assert untriaged.value[0].cites == ()
    assert nd.value[0].cites == ("Q-0007",)
    q = Item(kind="question", ref="Q-0007", title="hold?", cites=("Q-0007",))
    groups = group_items([q, *untriaged.value, *nd.value])
    by_key = {g.key: [i.kind for i in g.items] for g in groups}
    assert by_key == {"Q-0007": ["question", "needs-decision"], "batch:untriaged": ["untriaged"]}


def test_a_file_name_link_renders_as_code_not_as_an_html_tag():
    spec = Item(kind="spec", ref="spec s", title="S", link="design-spec.md")
    text = build(
        "demo", readers=_empty_readers(specs=lambda: Reading.ok([spec], source="drafted specs"))
    ).render()
    assert "(`design-spec.md`)" in text
    assert "<design-spec.md>" not in text
