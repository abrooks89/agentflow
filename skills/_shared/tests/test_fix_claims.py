"""Tests for fix claims and the refusing close path in ``agentflow.issue_intake``.

Every tracker call goes through a fake runner that answers by argv shape; no real ``gh``
process and no network is ever touched. The new names are imported at module scope, so this
file fails at collection on a tree that does not have them.
"""

import json
from pathlib import Path

import pytest

from agentflow import issue_intake as ii
from agentflow.issue_intake import (
    MERGED_PR_LIMIT,
    OPEN_ISSUE_PAGE,
    REFUSAL_CODES,
    Claimant,
    DoneSpecLink,
    FixClaim,
    Issue,
    IssueIntakeError,
    claim_fixers,
    claim_state,
    done_spec_links,
    fix_claims,
    fixers,
    list_merged_prs,
    main,
    read_queue_rows,
    spec_fixers,
)
from agentflow import registry as _registry
from agentflow.registry import QueueRow

HOST_REF = "owner/repo"
LOGIN = "fleet-bot"


# --------------------------------------------------------------------------- #
# Fixtures and builders
# --------------------------------------------------------------------------- #
def _issue(n: int, title: str = "") -> Issue:
    return Issue(number=n, title=title or f"issue {n}", labels=())


def _pr(number: int, *, title: str = "", body: str = "", merged_at: str = "2026-09-01T00:00:00Z",
        branch: str = "", sha: str = "abcdef0123456789") -> dict:
    return {"number": number, "title": title, "body": body, "mergedAt": merged_at,
            "mergeCommit": {"oid": sha}, "headRefName": branch}


def _row(rid: str, work: str, seat: str, state: str = "done") -> QueueRow:
    lane = seat if seat.startswith("jr-dev") else "lead"
    return QueueRow(rid, work, lane, seat, state, "-", "")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _comment(body: str, login: str = LOGIN) -> dict:
    return {"author": {"login": login}, "body": body}


def _verdict(seat: str, ref: str, verdict: str) -> str:
    return (f"Agentflow [{seat}]\n\nChecked against: master 1234567; claimed fixed by {ref}\n"
            f"Verdict: {verdict}\n\n| # | ask | verdict | evidence |\n")


class TrackerRunner:
    """Answers each tracker argv by shape and records every call. Unknown argv fails loudly."""

    def __init__(self, *, open_issues=(), merged=(), comments=None, view=None,
                 login=LOGIN, fail=None):
        self.open_issues = list(open_issues)
        self.merged = list(merged)
        self.comments = comments or {}
        self.view = view or {}
        self.login = login
        self.fail = fail or {}
        self.calls: list = []
        self.bodies: list = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        key = tuple(argv[1:3])
        if key in self.fail:
            return 1, "", self.fail[key]
        if key == ("issue", "list"):
            return 0, json.dumps(self.open_issues), ""
        if key == ("pr", "list"):
            return 0, json.dumps(self.merged), ""
        if key == ("api", "user"):
            return 0, json.dumps({"login": self.login}), ""
        if key == ("issue", "view"):
            n = int(argv[3])
            fields = argv[argv.index("--json") + 1]
            if fields == "comments":
                return 0, json.dumps({"comments": self.comments.get(n, [])}), ""
            return 0, json.dumps(self.view[n]), ""
        if key == ("issue", "comment"):
            body_file = argv[argv.index("--body-file") + 1]
            self.bodies.append(Path(body_file).read_text(encoding="utf-8"))
            return 0, "", ""
        if key == ("issue", "close"):
            return 0, "", ""
        raise AssertionError(f"unexpected argv {argv}")

    def writes(self):
        return [c for c in self.calls if tuple(c[1:3]) in (("issue", "comment"), ("issue", "close"))]


class _Env:
    def __init__(self, cli: str, data_root: str):
        class _GH:
            pass

        self.git_host = _GH()
        self.git_host.cli = cli
        self.data_root = data_root


class _Proj:
    def __init__(self, ws_kind: str = "github-issues"):
        class _CR:
            pass

        class _WS:
            pass

        self.canonical_repo = _CR()
        self.canonical_repo.host_ref = HOST_REF
        self.work_source = _WS()
        self.work_source.kind = ws_kind


_QUEUE_HEADER = ("| id | work item | lane | claimed_by | state | waiting_on | notes |\n"
                 "|---|---|---|---|---|---|---|\n")
_LIVE_ROW = ("| E-0890-gh707-registry-write-guards | gh707-registry-write-guards (gh#707)"
             " | jr-dev-3 | jr-dev-3 | done | - |  |\n")


def _registry_text() -> str:
    """The shipped registry template with one live Queue row, so it parses as the real one."""
    text = _registry._template_text("demo")
    assert _QUEUE_HEADER in text
    return text.replace(_QUEUE_HEADER, _QUEUE_HEADER + _LIVE_ROW, 1)


REGISTRY_LOG = """# Registry log

| E-0885-gh743-provision-settings-preflight | gh743-provision-settings-preflight (gh#743) | lead | lead-dev | done | - |  |
| E-0870-dm-built-thing | dm-built-thing (gh#70) | lead | dev-manager | done | - |  |
| E-0871-auditor-built-thing | auditor-built-thing (gh#71) | lead | auditor | done | - |  |
not a table line
| Q-001 | a question row | x | y | z | w | v |
"""


@pytest.fixture
def project(monkeypatch, tmp_path: Path):
    """A demo project on disk with both registry files, and the config loaders stubbed."""
    from agentflow import config as _config

    status = tmp_path / "projects" / "demo" / "status"
    _write(status / "registry.md", _registry_text())
    _write(status / "registry-log.md", REGISTRY_LOG)
    (tmp_path / "projects" / "demo" / "design" / "specs").mkdir(parents=True)
    monkeypatch.setattr(_config, "load_environment", lambda: _Env("gh", str(tmp_path)))
    monkeypatch.setattr(_config, "load_project", lambda name: _Proj())
    return tmp_path / "projects" / "demo"


def _use(monkeypatch, runner: TrackerRunner) -> TrackerRunner:
    monkeypatch.setattr(ii, "_subprocess_runner", runner)
    return runner


# --------------------------------------------------------------------------- #
# fix_claims
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pr", [
    _pr(1, title="Widget guard (gh#10)"),
    _pr(2, body="Agentflow [lead-dev]\n\nSource: gh#10\n"),
    _pr(3, body="**Source:** gh#10"),
    _pr(4, body="Source: #10"),
    _pr(5, body="Closes #10"),
    _pr(6, body="- **Fixes:** gh#10"),
    _pr(7, body="Resolved #11, #10 and gh#12"),
], ids=["title", "source", "bold-source", "source-bare-ref", "closes", "bold-fixes", "list"])
def test_a_pr_claims_the_fix_through_each_documented_shape(pr) -> None:
    claims = fix_claims([_issue(10)], [pr], [])
    assert [c.number for c in claims] == [10]
    assert claims[0].claimants[0].pr == pr["number"]


@pytest.mark.parametrize("body", [
    "**Does not close gh#10.**",
    "Closes E-0714 / gh#296. The work on gh#10 stays untouched.",
    "It also fixes #10 in passing.",
    "Closing #10",
    "Title mentions #10 only in prose",
], ids=["does-not-close", "keyword-then-other-ref", "mid-sentence", "not-a-github-keyword",
        "prose"])
def test_a_keyword_that_does_not_open_the_line_claims_nothing(body) -> None:
    assert fix_claims([_issue(10)], [_pr(1, body=body)], []) == []


def test_a_title_hash_ref_without_gh_does_not_claim() -> None:
    assert fix_claims([_issue(10)], [_pr(1, title="Widget (#10)")], []) == []


def test_a_cited_number_that_is_not_an_open_issue_is_ignored() -> None:
    """A cited PR number, or a closed issue, is simply not in the open list."""
    claims = fix_claims([_issue(10)], [_pr(1, body="Source: gh#10 after #99 (a PR)")], [])
    assert [c.number for c in claims] == [10]


def test_a_done_spec_claims_its_source_issue(tmp_path: Path) -> None:
    specs = tmp_path / "specs"
    _write(specs / "2026-09-01-done-thing.md",
           "---\nslug: done-thing\ncurrent_state: done   # reconciled\nsource: gh#10\n---\nbody\n")
    _write(specs / "2026-09-02-drafted-thing.md",
           "---\ncurrent_state: drafted\nsource: gh#11\n---\n")
    _write(specs / "2026-09-03-no-frontmatter.md", "just text gh#12\n")
    links = done_spec_links(specs)
    assert links == [DoneSpecLink(slug="done-thing", date="2026-09-01", issues=frozenset({10}))]
    claims = fix_claims([_issue(10), _issue(11), _issue(12)], [], links)
    assert [(c.number, c.claimants[0].ref) for c in claims] == [(10, "spec done-thing")]


def test_a_done_spec_without_a_slug_key_takes_its_slug_from_the_file_name(tmp_path: Path) -> None:
    _write(tmp_path / "2026-09-01-from-name.md", "---\ncurrent_state: done\nsource: gh#5\n---\n")
    assert done_spec_links(tmp_path)[0].slug == "from-name"


def test_claimants_come_prs_newest_first_then_specs_by_date_then_slug() -> None:
    prs = [_pr(1, body="Source: gh#10", merged_at="2026-09-01T00:00:00Z"),
           _pr(2, body="Source: gh#10", merged_at="2026-09-05T00:00:00Z")]
    links = [DoneSpecLink("b-spec", "2026-09-03", frozenset({10})),
             DoneSpecLink("a-spec", "2026-09-03", frozenset({10})),
             DoneSpecLink("c-spec", "2026-09-09", frozenset({10}))]
    for order in (links, list(reversed(links))):
        refs = [c.ref for c in fix_claims([_issue(10)], prs, order)[0].claimants]
        assert refs == ["#2", "#1", "spec c-spec", "spec a-spec", "spec b-spec"]


def test_claims_are_ordered_by_their_newest_claimant_oldest_first() -> None:
    prs = [_pr(1, body="Source: gh#10", merged_at="2026-09-09T00:00:00Z"),
           _pr(2, body="Source: gh#11", merged_at="2026-09-02T00:00:00Z")]
    assert [c.number for c in fix_claims([_issue(10), _issue(11)], prs, [])] == [11, 10]


def test_a_claimant_carries_number_sha7_merged_at_and_branch() -> None:
    c = fix_claims([_issue(10)], [_pr(7, body="Source: gh#10", branch="e0890-x")], [])[0]
    assert c.claimants[0] == Claimant(kind="pr", pr=7, sha7="abcdef0",
                                      merged_at="2026-09-01T00:00:00Z", branch="e0890-x")


# --------------------------------------------------------------------------- #
# fixers / spec_fixers
# --------------------------------------------------------------------------- #
def test_fixers_reads_the_attribution_line() -> None:
    assert fixers(_pr(1, body="Agentflow [jr-dev-3]\n\nbody"), []) == {"jr-dev-3"}


def test_fixers_reads_an_attribution_line_that_continues() -> None:
    assert fixers(_pr(1, body="Agentflow [jr-dev-1] - gh#645: widget"), []) == {"jr-dev-1"}


def test_fixers_matches_a_row_by_the_branch_epic_number() -> None:
    rows = [_row("E-0890-gh707-registry-write-guards", "gh707-registry-write-guards", "jr-dev-3")]
    assert fixers(_pr(1, branch="e0890-gh707-registry-write-guards"), rows) == {"jr-dev-3"}


def test_fixers_matches_a_row_by_a_source_epic_line() -> None:
    rows = [_row("E-0890-anything", "unrelated-work", "jr-dev-2")]
    assert fixers(_pr(1, body="Summary\n\nSource: E-0890", branch="topic"), rows) == {"jr-dev-2"}


def test_fixers_matches_a_row_by_an_epic_line() -> None:
    rows = [_row("E-0890-anything", "unrelated-work", "jr-dev-2")]
    assert fixers(_pr(1, body="Epic: e0890 widget", branch="topic"), rows) == {"jr-dev-2"}


def test_fixers_matches_a_prefixed_branch_against_the_first_work_item_token() -> None:
    rows = [_row("E-0700-other", "gh675-wave-timeout-events (gh#675)", "lead-dev")]
    assert fixers(_pr(1, branch="fix/gh675-wave-timeout-events"), rows) == {"lead-dev"}


def test_a_seat_named_in_the_branch_is_never_the_builder() -> None:
    rows = [_row("E-0100-other", "other-work", "jr-dev-1")]
    assert fixers(_pr(1, branch="e0879-auditor-blocker-contract"), rows) == frozenset()


def test_orchestrator_attribution_alone_names_nobody() -> None:
    assert fixers(_pr(1, body="Agentflow [orchestrator]\n\nbody"), []) == frozenset()


def test_an_attribution_line_that_is_not_the_first_does_not_count() -> None:
    assert fixers(_pr(1, body="Summary line\nAgentflow [lead-dev]\n"), []) == frozenset()


def test_a_row_with_no_claimant_adds_nothing() -> None:
    rows = [_row("E-0890-x", "x", "-")]
    assert fixers(_pr(1, branch="e0890-x"), rows) == frozenset()


def test_two_rows_sharing_an_epic_id_add_both_claimants() -> None:
    rows = [_row("E-0210-one", "one", "jr-dev-1"), _row("E-0210-two", "two", "jr-dev-2")]
    assert fixers(_pr(1, branch="e0210-one"), rows) == {"jr-dev-1", "jr-dev-2"}


def test_attribution_and_rows_union() -> None:
    rows = [_row("E-0840-x", "x", "jr-dev-2"), _row("E-0829-y", "y", "jr-dev-1")]
    pr = _pr(590, body="Agentflow [jr-dev-2]\n\nSource: E-0829", branch="e0840-x")
    assert fixers(pr, rows) == {"jr-dev-1", "jr-dev-2"}


def test_spec_fixers_matches_the_exact_first_token_only() -> None:
    rows = [_row("E-0818-build-the-single-commit", "build-the-single-commit-share-cut-tooling",
                 "lead-dev"),
            _row("E-0819-x", "build-the-single-commit extra words", "jr-dev-1")]
    assert spec_fixers("build-the-single-commit", rows) == {"jr-dev-1"}
    assert spec_fixers("build-the-single", rows) == frozenset()


# --------------------------------------------------------------------------- #
# read_queue_rows / claim_fixers
# --------------------------------------------------------------------------- #
def test_read_queue_rows_reads_both_files(project: Path) -> None:
    rows, note = read_queue_rows(project / "status")
    assert note == ""
    assert {r.id for r in rows} == {"E-0890-gh707-registry-write-guards",
                                    "E-0885-gh743-provision-settings-preflight",
                                    "E-0870-dm-built-thing", "E-0871-auditor-built-thing"}


def test_a_missing_log_gives_the_live_rows_and_a_note_naming_it(project: Path) -> None:
    (project / "status" / "registry-log.md").unlink()
    rows, note = read_queue_rows(project / "status")
    assert [r.id for r in rows] == ["E-0890-gh707-registry-write-guards"]
    assert "registry-log.md" in note


def test_a_damaged_registry_is_a_note_not_a_crash(project: Path) -> None:
    _write(project / "status" / "registry.md", "# registry with no sections\n")
    rows, note = read_queue_rows(project / "status")
    assert "registry.md unparseable (RegistryError" in note
    assert {r.id for r in rows} >= {"E-0870-dm-built-thing"}


def test_a_programming_error_in_the_parse_is_not_swallowed(monkeypatch, project: Path) -> None:
    def boom(text):
        raise TypeError("a refactor broke this")

    monkeypatch.setattr(_registry, "parse_registry", boom)
    with pytest.raises(TypeError, match="a refactor broke this"):
        read_queue_rows(project / "status")


def _claim(*claimants) -> FixClaim:
    return FixClaim(number=10, title="t", claimants=tuple(claimants))


def test_claim_fixers_is_the_union_over_claimants() -> None:
    rows = [_row("E-0890-x", "x", "jr-dev-3")]
    prs = {1: _pr(1, body="Agentflow [lead-dev]"), 2: _pr(2, branch="e0890-x")}
    known, unknown = claim_fixers(_claim(Claimant("pr", 1), Claimant("pr", 2)), prs, rows, True)
    assert (known, unknown) == ({"lead-dev", "jr-dev-3"}, False)


def test_claim_fixers_unknown_when_a_claimant_resolves_to_nothing() -> None:
    prs = {1: _pr(1, body="Agentflow [lead-dev]"), 2: _pr(2, branch="topic")}
    known, unknown = claim_fixers(_claim(Claimant("pr", 1), Claimant("pr", 2)), prs, [], True)
    assert (known, unknown) == ({"lead-dev"}, True)


def test_claim_fixers_unknown_with_no_claimant() -> None:
    assert claim_fixers(None, {}, [], True) == (frozenset(), True)
    assert claim_fixers(_claim(), {}, [], True) == (frozenset(), True)


def test_claim_fixers_unknown_when_the_rows_were_not_read() -> None:
    prs = {1: _pr(1, body="Agentflow [lead-dev]")}
    assert claim_fixers(_claim(Claimant("pr", 1)), prs, [], False) == ({"lead-dev"}, True)


# --------------------------------------------------------------------------- #
# list_merged_prs
# --------------------------------------------------------------------------- #
def test_list_merged_prs_argv_and_parse() -> None:
    runner = TrackerRunner(merged=[_pr(1)])
    assert list_merged_prs(HOST_REF, kind="github-issues", cli="gh", runner=runner) == [_pr(1)]
    assert runner.calls == [["gh", "pr", "list", "--repo", HOST_REF, "--state", "merged",
                             "--limit", str(MERGED_PR_LIMIT), "--json",
                             "number,title,body,mergedAt,mergeCommit,headRefName"]]


def test_list_merged_prs_no_tracker_makes_no_call() -> None:
    runner = TrackerRunner()
    assert list_merged_prs(HOST_REF, kind="filesystem", cli="none", runner=runner) == []
    assert runner.calls == []


def test_list_merged_prs_refuses_an_unverified_tracker() -> None:
    runner = TrackerRunner()
    with pytest.raises(IssueIntakeError, match="no verified argv shape"):
        list_merged_prs(HOST_REF, kind="gitea", cli="gh", runner=runner)
    assert runner.calls == []


@pytest.mark.parametrize("stdout", ["not json", "{}", '[{"title": "no number"}]'])
def test_list_merged_prs_is_loud_on_malformed_output(stdout) -> None:
    with pytest.raises(IssueIntakeError):
        list_merged_prs(HOST_REF, kind="github-issues", cli="gh",
                        runner=lambda argv: (0, stdout, ""))


def test_list_merged_prs_is_loud_on_nonzero_exit() -> None:
    with pytest.raises(IssueIntakeError, match="boom"):
        list_merged_prs(HOST_REF, kind="github-issues", cli="gh",
                        runner=lambda argv: (1, "", "boom"))


# --------------------------------------------------------------------------- #
# claim_state
# --------------------------------------------------------------------------- #
_PR_CLAIM = _claim(Claimant("pr", 7, sha7="abcdef0"), Claimant("pr", 3))


@pytest.mark.parametrize("seat", ["auditor", "dev-manager"])
@pytest.mark.parametrize("verdict", ["remainder", "routed"])
def test_a_closing_seat_remainder_or_routed_verdict_marks_checked(seat, verdict) -> None:
    assert claim_state(_PR_CLAIM, [_comment(_verdict(seat, "#7 (abcdef0)", verdict))],
                       LOGIN) == ("checked", 0)


def test_a_verdict_on_an_older_claimant_leaves_it_unchecked() -> None:
    assert claim_state(_PR_CLAIM, [_comment(_verdict("auditor", "#3", "remainder"))],
                       LOGIN) == ("unchecked", 0)


def test_a_fixed_verdict_on_an_older_claimant_is_not_a_failed_close() -> None:
    """Two failed closes against #3 must not spend the retry budget of the newer #7."""
    stale = [_comment(_verdict("auditor", "#3", "fixed"))] * 2
    assert claim_state(_PR_CLAIM, stale, LOGIN) == ("unchecked", 0)


def test_a_verdict_naming_a_longer_number_does_not_match() -> None:
    assert claim_state(_PR_CLAIM, [_comment(_verdict("auditor", "#70", "remainder"))],
                       LOGIN) == ("unchecked", 0)


def test_fixed_verdicts_on_an_open_issue_count_as_failed_closes() -> None:
    one = [_comment(_verdict("auditor", "#7", "fixed"))]
    assert claim_state(_PR_CLAIM, one, LOGIN) == ("unchecked", 1)
    assert claim_state(_PR_CLAIM, one * 2, LOGIN) == ("unchecked", 2)


@pytest.mark.parametrize("seat", ["lead-dev", "jr-dev-1", "steward", "orchestrator"])
def test_a_non_closing_seat_verdict_does_not_count(seat) -> None:
    comments = [_comment(_verdict(seat, "#7", "remainder")), _comment(_verdict(seat, "#7", "fixed"))]
    assert claim_state(_PR_CLAIM, comments, LOGIN) == ("unchecked", 0)


def test_a_well_shaped_verdict_by_another_account_does_not_count() -> None:
    comments = [_comment(_verdict("auditor", "#7", "remainder"), login="stranger")]
    assert claim_state(_PR_CLAIM, comments, LOGIN) == ("unchecked", 0)


def test_a_spec_claimant_is_named_as_spec_slug() -> None:
    claim = _claim(Claimant("spec", slug="done-thing", date="2026-09-01"))
    ok = [_comment(_verdict("auditor", "spec done-thing", "routed"))]
    longer = [_comment(_verdict("auditor", "spec done-thing-two", "routed"))]
    assert claim_state(claim, ok, LOGIN) == ("checked", 0)
    assert claim_state(claim, longer, LOGIN) == ("unchecked", 0)


# --------------------------------------------------------------------------- #
# The fix-claims listing
# --------------------------------------------------------------------------- #
def _open(*numbers):
    return [{"number": n, "title": f"issue {n}", "labels": []} for n in numbers]


def test_listing_prints_one_line_per_claim_then_the_summary(monkeypatch, project, capsys) -> None:
    runner = _use(monkeypatch, TrackerRunner(
        open_issues=_open(707, 743, 99),
        merged=[_pr(801, body="Source: gh#707", branch="e0890-gh707-registry-write-guards",
                    merged_at="2026-09-02T00:00:00Z"),
                _pr(802, body="Closes #743", branch="e0885-gh743-provision-settings-preflight",
                    merged_at="2026-09-03T00:00:00Z")],
        comments={743: [_comment(_verdict("auditor", "#802 (abcdef0)", "remainder"))]},
    ))
    assert main(["fix-claims", "--project", "demo"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "#707  issue 707  claimed-by: #801 abcdef0 2026-09-02T00:00:00Z  fixed-by: jr-dev-3"
        "  state: unchecked",
        "#743  issue 743  claimed-by: #802 abcdef0 2026-09-03T00:00:00Z  fixed-by: lead-dev"
        "  state: checked",
        "fix-claims: 2 open issue(s) claimed fixed, 1 unchecked; 3 open issue(s) read,"
        " 2 merged PR(s) read",
    ]
    assert ["gh", "api", "user"] in runner.calls
    assert runner.writes() == []


def test_listing_json_carries_records_and_summary(monkeypatch, project, capsys) -> None:
    _use(monkeypatch, TrackerRunner(open_issues=_open(707),
                                    merged=[_pr(801, body="Source: gh#707", branch="e0890-x")]))
    assert main(["fix-claims", "--project", "demo", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["claims"][0]["issue"] == 707
    assert data["claims"][0]["fixed_by"] == ["jr-dev-3"]
    assert data["summary"]["claimed"] == 1 and data["summary"]["unchecked"] == 1


def test_listing_appends_failed_closes(monkeypatch, project, capsys) -> None:
    fixed = _comment(_verdict("auditor", "#801", "fixed"))
    _use(monkeypatch, TrackerRunner(open_issues=_open(707),
                                    merged=[_pr(801, body="Source: gh#707", branch="e0890-x")],
                                    comments={707: [fixed, fixed]}))
    assert main(["fix-claims", "--project", "demo"]) == 0
    assert capsys.readouterr().out.splitlines()[0].endswith("state: unchecked  failed-closes: 2")


def test_a_newer_merge_resets_failed_closes(monkeypatch, project, capsys) -> None:
    fixed = _comment(_verdict("auditor", "#801", "fixed"))
    _use(monkeypatch, TrackerRunner(
        open_issues=_open(707),
        merged=[_pr(809, body="Source: gh#707", branch="e0890-x", merged_at="2026-09-09T00:00:00Z"),
                _pr(801, body="Source: gh#707", branch="e0890-x")],
        comments={707: [fixed, fixed]}))
    assert main(["fix-claims", "--project", "demo"]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert line.startswith("#707  issue 707  claimed-by: #809 ")
    assert line.endswith("state: unchecked")


def test_listing_prints_the_summary_for_an_empty_list(monkeypatch, project, capsys) -> None:
    runner = _use(monkeypatch, TrackerRunner(open_issues=_open(1), merged=[]))
    assert main(["fix-claims", "--project", "demo"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "fix-claims: 0 open issue(s) claimed fixed, 0 unchecked; 1 open issue(s) read,"
        " 0 merged PR(s) read"
    ]
    assert ["gh", "api", "user"] not in runner.calls


def test_listing_asks_one_row_past_each_page(monkeypatch, project, capsys) -> None:
    runner = _use(monkeypatch, TrackerRunner())
    assert main(["fix-claims", "--project", "demo"]) == 0
    by_kind = {tuple(c[1:3]): c for c in runner.calls}
    for key, page in ((("issue", "list"), OPEN_ISSUE_PAGE), (("pr", "list"), MERGED_PR_LIMIT)):
        argv = by_kind[key]
        assert argv[argv.index("--limit") + 1] == str(page + 1)


def test_a_full_open_issue_read_prints_capped(monkeypatch, project, capsys) -> None:
    _use(monkeypatch, TrackerRunner(open_issues=_open(*range(OPEN_ISSUE_PAGE + 1, 0, -1))))
    assert main(["fix-claims", "--project", "demo"]) == 0
    summary = capsys.readouterr().out.splitlines()[-1]
    assert f"{OPEN_ISSUE_PAGE} open issue(s) read (capped at {OPEN_ISSUE_PAGE})" in summary
    assert "merged PR(s) read (capped" not in summary


def test_exactly_a_page_of_open_issues_is_not_capped(monkeypatch, project, capsys) -> None:
    _use(monkeypatch, TrackerRunner(open_issues=_open(*range(OPEN_ISSUE_PAGE, 0, -1))))
    assert main(["fix-claims", "--project", "demo"]) == 0
    summary = capsys.readouterr().out.splitlines()[-1]
    assert f"{OPEN_ISSUE_PAGE} open issue(s) read," in summary
    assert "(capped" not in summary


def test_exactly_a_page_of_merged_prs_is_not_capped(monkeypatch, project, capsys) -> None:
    merged = [_pr(n) for n in range(MERGED_PR_LIMIT, 0, -1)]
    _use(monkeypatch, TrackerRunner(open_issues=_open(1), merged=merged))
    assert main(["fix-claims", "--project", "demo"]) == 0
    summary = capsys.readouterr().out.splitlines()[-1]
    assert f"{MERGED_PR_LIMIT} merged PR(s) read" in summary
    assert "(capped" not in summary


def test_a_full_merged_pr_read_prints_capped(monkeypatch, project, capsys) -> None:
    merged = [_pr(n) for n in range(MERGED_PR_LIMIT + 1, 0, -1)]
    _use(monkeypatch, TrackerRunner(open_issues=_open(1), merged=merged))
    assert main(["fix-claims", "--project", "demo"]) == 0
    summary = capsys.readouterr().out.splitlines()[-1]
    assert f"{MERGED_PR_LIMIT} merged PR(s) read (capped at {MERGED_PR_LIMIT})" in summary
    assert "open issue(s) read (capped" not in summary


def test_a_missing_registry_reads_question_mark_and_exits_zero(monkeypatch, project, capsys) -> None:
    (project / "status" / "registry.md").unlink()
    _use(monkeypatch, TrackerRunner(open_issues=_open(707),
                                    merged=[_pr(801, body="Agentflow [jr-dev-3]\nSource: gh#707")]))
    assert main(["fix-claims", "--project", "demo"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "fixed-by: ?  state:" in out[0]
    assert "; registry: unread (no registry at" in out[-1]


def test_a_missing_registry_with_no_attribution_reads_bare_question_mark(
        monkeypatch, project, capsys) -> None:
    (project / "status" / "registry.md").unlink()
    _use(monkeypatch, TrackerRunner(open_issues=_open(707),
                                    merged=[_pr(801, body="Source: gh#707")]))
    assert main(["fix-claims", "--project", "demo"]) == 0
    assert "fixed-by: ?  state:" in capsys.readouterr().out.splitlines()[0]


def test_the_json_keeps_the_named_seats_when_the_fixers_are_unknown(
        monkeypatch, project, capsys) -> None:
    (project / "status" / "registry.md").unlink()
    _use(monkeypatch, TrackerRunner(open_issues=_open(707),
                                    merged=[_pr(801, body="Agentflow [jr-dev-3]\nSource: gh#707")]))
    assert main(["fix-claims", "--project", "demo", "--json"]) == 0
    record = json.loads(capsys.readouterr().out)["claims"][0]
    assert (record["fixed_by"], record["fixers_unknown"]) == (["jr-dev-3"], True)


def test_listing_with_no_tracker_says_so_and_exits_zero(monkeypatch, project, capsys) -> None:
    from agentflow import config as _config

    monkeypatch.setattr(_config, "load_project", lambda name: _Proj("filesystem"))
    runner = _use(monkeypatch, TrackerRunner())
    assert main(["fix-claims", "--project", "demo"]) == 0
    assert capsys.readouterr().out.startswith("fix-claims: no issue tracker for this project (")
    assert runner.calls == []


@pytest.mark.parametrize("failing", [("issue", "list"), ("pr", "list"), ("api", "user")])
def test_a_failed_tracker_read_exits_one_and_never_reads_as_zero(
        failing, monkeypatch, project, capsys) -> None:
    _use(monkeypatch, TrackerRunner(open_issues=_open(707),
                                    merged=[_pr(801, body="Source: gh#707")],
                                    fail={failing: "tracker down"}))
    assert main(["fix-claims", "--project", "demo"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tracker down" in captured.err


def _usage_error(argv, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


def test_fix_claims_rejects_issue(capsys) -> None:
    err = _usage_error(["fix-claims", "--project", "demo", "--issue", "5"], capsys)
    assert "--issue is only meaningful for" in err


@pytest.mark.parametrize("extra", [["--actor", "auditor"], ["--comment-file", "x"], ["--dry-run"]],
                         ids=["actor", "comment-file", "dry-run"])
def test_fix_claims_rejects_the_close_only_flags(extra, capsys) -> None:
    err = _usage_error(["fix-claims", "--project", "demo", *extra], capsys)
    assert f"{extra[0]} is only meaningful for close" in err


@pytest.mark.parametrize("mode", ["untriaged", "selectable", "awaiting", "mirror", "claim"])
def test_other_modes_reject_the_close_only_flags(mode, capsys) -> None:
    # Only claim takes --issue; the list modes get the close-only flag alone, so the refusal
    # is the close-only check and not the --issue one.
    extra = ["--issue", "5"] if mode == "claim" else []
    err = _usage_error([mode, "--project", "demo", *extra, "--dry-run"], capsys)
    assert f"--dry-run is only meaningful for close, not {mode}" in err


# --------------------------------------------------------------------------- #
# close
# --------------------------------------------------------------------------- #
def _view(n: int, state: str = "OPEN") -> dict:
    return {"number": n, "title": f"issue {n}", "state": state, "body": "", "url": f"u/{n}"}


def _good_file(tmp_path: Path, actor: str, name: str = "c.md", ref: str = "#801 (abcdef0)") -> str:
    path = tmp_path / name
    path.write_text(_verdict(actor, ref, "fixed"), encoding="utf-8")
    return str(path)


def _close(actor: str, n: int, comment_file: str, *extra) -> list:
    return ["close", "--project", "demo", "--actor", actor, "--issue", str(n),
            "--comment-file", comment_file, *extra]


# Issue 10: fixed by jr-dev-3 (row via branch). Issue 20: attribution dev-manager.
# Issue 21: attribution auditor. Issue 30: one claimant resolves to nobody.
# Issue 40: nothing claims it. Issue 50: dev-manager claimant and an unresolved one.
_MERGED = [
    _pr(801, body="Source: gh#10", branch="e0890-gh707-registry-write-guards"),
    _pr(802, body="Agentflow [dev-manager]\n\nSource: gh#20"),
    _pr(803, body="Agentflow [auditor]\n\nSource: gh#21"),
    _pr(804, body="Source: gh#30", branch="topic-with-no-row"),
    _pr(805, body="Agentflow [dev-manager]\n\nSource: gh#50", merged_at="2026-09-05T00:00:00Z"),
    _pr(806, body="Source: gh#50", branch="no-row", merged_at="2026-09-06T00:00:00Z"),
]


def _close_runner(*numbers, state="OPEN") -> TrackerRunner:
    return TrackerRunner(merged=_MERGED, view={n: _view(n, state) for n in numbers})


def test_refusal_codes_are_in_the_spec_order() -> None:
    assert REFUSAL_CODES == ("actor_may_not_close", "not_open", "fixer_is_actor",
                             "fixer_unknown", "evidence_shape")


@pytest.mark.parametrize("actor", ["lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "steward"])
def test_actor_may_not_close_refuses_before_any_read(actor, monkeypatch, project, tmp_path,
                                                     capsys) -> None:
    # Issue 20 is open, its fixer is dev-manager (not this actor), and the file is well shaped
    # for this actor, so every later gate would pass: only the actor gate refuses.
    runner = _use(monkeypatch, _close_runner(20))
    assert main(_close(actor, 20, _good_file(tmp_path, actor), "--dry-run")) == 2
    assert capsys.readouterr().err.startswith("actor_may_not_close: #20")
    assert runner.calls == []


def test_close_argparse_refuses_a_seat_outside_agents() -> None:
    with pytest.raises(SystemExit):
        main(_close("orchestrator", 10, "x"))


@pytest.mark.parametrize("missing", ["--actor", "--comment-file", "--issue"])
def test_close_requires_its_arguments(missing) -> None:
    argv = _close("auditor", 10, "x")
    i = argv.index(missing)
    del argv[i:i + 2]
    with pytest.raises(SystemExit):
        main(argv)


def test_close_rejects_json() -> None:
    with pytest.raises(SystemExit):
        main(_close("auditor", 10, "x", "--json"))


@pytest.mark.parametrize("n", [10, 20, 21, 30, 40])
def test_not_open_refuses_a_closed_issue_whatever_its_fixers(n, monkeypatch, project, tmp_path,
                                                             capsys) -> None:
    runner = _use(monkeypatch, _close_runner(n, state="CLOSED"))
    assert main(_close("auditor", n, _good_file(tmp_path, "auditor"))) == 2
    assert capsys.readouterr().err.startswith(f"not_open: #{n}  issue {n}")
    assert runner.writes() == []
    assert not any(c[1:3] == ["pr", "list"] for c in runner.calls)


def test_a_pull_request_number_is_refused_before_any_fixer_read(monkeypatch, project, tmp_path,
                                                                capsys) -> None:
    """`issue view` answers for a PR number, and `issue close` would close the PR."""
    view = {**_view(804), "url": "https://github.com/owner/repo/pull/804"}
    runner = _use(monkeypatch, TrackerRunner(merged=_MERGED, view={804: view}))
    assert main(_close("auditor", 804, _good_file(tmp_path, "auditor"))) == 2
    err = capsys.readouterr().err
    assert err.startswith("not_open: #804  issue 804")
    assert "is a pull request, not an issue" in err
    assert runner.writes() == []
    assert not any(c[1:3] == ["pr", "list"] for c in runner.calls)


@pytest.mark.parametrize("actor,n", [("dev-manager", 20), ("auditor", 21)])
def test_fixer_is_actor_refuses_the_seat_that_built_the_fix(actor, n, monkeypatch, project,
                                                            tmp_path, capsys) -> None:
    runner = _use(monkeypatch, _close_runner(n))
    assert main(_close(actor, n, _good_file(tmp_path, actor))) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"fixer_is_actor: #{n}  issue {n}  fixed-by: {actor}")
    assert runner.writes() == []


def test_fixer_is_actor_for_a_seat_named_only_by_a_queue_row(monkeypatch, project, tmp_path,
                                                             capsys) -> None:
    """The registry-log row E-0870 names dev-manager as the builder of branch e0870-...."""
    runner = _use(monkeypatch, TrackerRunner(
        merged=[_pr(807, body="Source: gh#60", branch="e0870-dm-built-thing")],
        view={60: _view(60)}))
    assert main(_close("dev-manager", 60, _good_file(tmp_path, "dev-manager"))) == 2
    assert capsys.readouterr().err.startswith("fixer_is_actor: #60")
    assert runner.writes() == []


@pytest.mark.parametrize("n", [30, 40], ids=["unresolved-claimant", "nothing-claims-it"])
def test_fixer_unknown_refuses_dev_manager(n, monkeypatch, project, tmp_path, capsys) -> None:
    runner = _use(monkeypatch, _close_runner(n))
    assert main(_close("dev-manager", n, _good_file(tmp_path, "dev-manager"))) == 2
    assert capsys.readouterr().err.startswith(f"fixer_unknown: #{n}  issue {n}  fixed-by: ?")
    assert runner.writes() == []


def test_fixer_unknown_when_the_registry_cannot_be_read(monkeypatch, project, tmp_path,
                                                         capsys) -> None:
    (project / "status" / "registry-log.md").unlink()
    runner = _use(monkeypatch, _close_runner(10))
    assert main(_close("dev-manager", 10, _good_file(tmp_path, "dev-manager"))) == 2
    err = capsys.readouterr().err
    assert err.startswith("fixer_unknown: #10  issue 10  fixed-by: ?  (")
    assert "registry-log.md" in err
    assert runner.writes() == []


def test_a_capped_merged_pr_read_does_not_make_the_fixers_unknown(monkeypatch, project, tmp_path,
                                                                  capsys) -> None:
    """Close takes the listing's unknown rule (spec item 2), so the two never disagree."""
    merged = [_MERGED[0]] + [_pr(n) for n in range(2000, 2000 + MERGED_PR_LIMIT)]
    runner = _use(monkeypatch, TrackerRunner(merged=merged, view={10: _view(10)}))
    assert main(_close("dev-manager", 10, _good_file(tmp_path, "dev-manager"), "--dry-run")) == 0
    assert capsys.readouterr().out == "would close #10  issue 10\n"
    assert runner.writes() == []


@pytest.mark.parametrize("n,ref", [(30, "#804"), (40, "master sha 1234567")],
                         ids=["unresolved-claimant", "nothing-claims-it"])
def test_the_auditor_passes_fixer_unknown_to_a_clean_dry_run(n, ref, monkeypatch, project,
                                                            tmp_path, capsys) -> None:
    runner = _use(monkeypatch, _close_runner(n))
    assert main(_close("auditor", n, _good_file(tmp_path, "auditor", ref=ref), "--dry-run")) == 0
    assert capsys.readouterr().out == f"would close #{n}  issue {n}\n"
    assert runner.writes() == []


def test_dev_manager_passes_a_claim_fixed_by_a_builder(monkeypatch, project, tmp_path,
                                                       capsys) -> None:
    runner = _use(monkeypatch, _close_runner(10))
    assert main(_close("dev-manager", 10, _good_file(tmp_path, "dev-manager"), "--dry-run")) == 0
    assert capsys.readouterr().out == "would close #10  issue 10\n"
    assert runner.writes() == []


def test_the_specific_fixer_refusal_comes_before_the_unknown_one(monkeypatch, project, tmp_path,
                                                                 capsys) -> None:
    _use(monkeypatch, _close_runner(50))
    assert main(_close("dev-manager", 50, _good_file(tmp_path, "dev-manager"))) == 2
    assert capsys.readouterr().err.startswith("fixer_is_actor: #50  issue 50  fixed-by: ?  (")


_MALFORMED = "no attribution line\nVerdict: fixed\n"


@pytest.mark.parametrize("n,code", [(20, "fixer_is_actor"), (40, "fixer_unknown")])
def test_a_fixer_refusal_comes_before_the_evidence_one(n, code, monkeypatch, project, tmp_path,
                                                       capsys) -> None:
    """The code routes the caller: an evidence_shape is retried, a fixer refusal is not."""
    path = tmp_path / "bad.md"
    path.write_text(_MALFORMED, encoding="utf-8")
    runner = _use(monkeypatch, _close_runner(n))
    assert main(_close("dev-manager", n, str(path))) == 2
    assert capsys.readouterr().err.startswith(f"{code}: #{n}")
    assert runner.writes() == []


_GOOD = "Agentflow [auditor]\n\nChecked against: master 1234567; claimed fixed by #801\nVerdict: fixed\n"


@pytest.mark.parametrize("text,why", [
    (_GOOD.replace("Agentflow [auditor]", "Agentflow [dev-manager]"), "first non-blank line"),
    (_GOOD.replace("Agentflow [auditor]", "Agentflow [auditor] - extra"), "first non-blank line"),
    (_GOOD.replace("Checked against:", "Checked:"), "Checked against:"),
    ("Agentflow [auditor]\nChecked against:\nVerdict: fixed\n", "`Checked against:` line is empty"),
    (_GOOD.replace("#801", "#799"), "does not name the newest claimant #801"),
    (_GOOD.replace("Verdict: fixed", "Verdict: remainder"), "Verdict: fixed"),
    (_GOOD + "x" * ii.EVIDENCE_MAX_CHARS, "characters, over"),
], ids=["wrong-seat", "not-exact", "no-checked-against", "empty-checked-against",
        "stale-claimant", "no-verdict-fixed", "too-long"])
def test_evidence_shape_refuses_each_break(text, why, monkeypatch, project, tmp_path,
                                           capsys) -> None:
    path = tmp_path / "bad.md"
    path.write_text(text, encoding="utf-8")
    runner = _use(monkeypatch, _close_runner(10))
    assert main(_close("auditor", 10, str(path))) == 2
    err = capsys.readouterr().err
    assert err.startswith("evidence_shape: #10  issue 10  fixed-by: jr-dev-3")
    assert why in err
    assert runner.writes() == []


def test_the_shape_passes_with_leading_blank_lines(monkeypatch, project, tmp_path, capsys) -> None:
    path = tmp_path / "ok.md"
    path.write_text("\n\n" + _GOOD, encoding="utf-8")
    _use(monkeypatch, _close_runner(10))
    assert main(_close("auditor", 10, str(path), "--dry-run")) == 0


def test_a_clean_close_comments_then_closes(monkeypatch, project, tmp_path, capsys) -> None:
    runner = _use(monkeypatch, _close_runner(10))
    path = _good_file(tmp_path, "auditor")
    assert main(_close("auditor", 10, path)) == 0
    assert capsys.readouterr().out == "closed #10  issue 10\n"
    comment, close = runner.writes()
    assert comment[:-1] == ["gh", "issue", "comment", "10", "--repo", HOST_REF, "--body-file"]
    assert close == ["gh", "issue", "close", "10", "--repo", HOST_REF, "--reason", "completed"]
    # The text travels by a private copy of what was checked, deleted once posted.
    assert comment[-1] != path and not Path(comment[-1]).exists()
    assert runner.bodies == [Path(path).read_text(encoding="utf-8")]
    view = [c for c in runner.calls if c[1:3] == ["issue", "view"]]
    assert view == [["gh", "issue", "view", "10", "--repo", HOST_REF,
                     "--json", "number,title,state,body,url"]]


def test_a_rewrite_after_the_check_is_not_what_gets_posted(monkeypatch, project, tmp_path,
                                                           capsys) -> None:
    path = _good_file(tmp_path, "auditor")
    checked = Path(path).read_text(encoding="utf-8")
    runner = _use(monkeypatch, _close_runner(10))
    real_read = Path.read_text

    def read_then_rewrite(self, *a, **kw):
        text = real_read(self, *a, **kw)
        if str(self) == path:
            self.write_text("Agentflow [auditor]\nunchecked text\n", encoding="utf-8")
        return text

    monkeypatch.setattr(Path, "read_text", read_then_rewrite)
    assert main(_close("auditor", 10, path)) == 0
    monkeypatch.setattr(Path, "read_text", real_read)
    assert runner.bodies == [checked]


def test_a_close_that_fails_after_the_comment_exits_one_and_says_both(
        monkeypatch, project, tmp_path, capsys) -> None:
    runner = _close_runner(10)
    runner.fail = {("issue", "close"): "close refused"}
    _use(monkeypatch, runner)
    assert main(_close("auditor", 10, _good_file(tmp_path, "auditor"))) == 1
    err = capsys.readouterr().err
    assert "posted the verdict comment on #10" in err
    assert "close failed" in err and "close refused" in err
    assert [c[1:3] for c in runner.calls[-2:]] == [["issue", "comment"], ["issue", "close"]]


def test_dry_run_sends_no_write(monkeypatch, project, tmp_path, capsys) -> None:
    runner = _use(monkeypatch, _close_runner(10))
    assert main(_close("auditor", 10, _good_file(tmp_path, "auditor"), "--dry-run")) == 0
    assert runner.writes() == []


def test_close_with_no_tracker_refuses_loudly(monkeypatch, project, tmp_path, capsys) -> None:
    from agentflow import config as _config

    monkeypatch.setattr(_config, "load_project", lambda name: _Proj("filesystem"))
    runner = _use(monkeypatch, TrackerRunner())
    assert main(_close("auditor", 10, _good_file(tmp_path, "auditor"))) == 1
    assert "no tracker configured" in capsys.readouterr().err
    assert runner.calls == []


def test_close_counts_a_done_spec_claimant(monkeypatch, project, tmp_path, capsys) -> None:
    _write(project / "design" / "specs" / "2026-09-01-dm-built-thing.md",
           "---\ncurrent_state: done\nsource: gh#70\n---\n")
    runner = _use(monkeypatch, TrackerRunner(merged=[], view={70: _view(70)}))
    assert main(_close("dev-manager", 70, _good_file(tmp_path, "dev-manager"))) == 2
    assert capsys.readouterr().err.startswith("fixer_is_actor: #70")
    assert runner.writes() == []
