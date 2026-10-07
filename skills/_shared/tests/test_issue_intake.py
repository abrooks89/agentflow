"""Tests for the shared issue-intake helper (dev-manager issue intake, task 1).

Covers every acceptance criterion in the plan: the public surface, the
kind/cli no-op rules, the untriaged / builder-selectable filters, the
spec cross-link parser, the label-edit arg vector, loud failures on a
non-zero runner exit or malformed JSON, and the CLI. Every runner is a fake —
no real `gh` / `glab` process is ever spawned in this suite.
"""

import json
from pathlib import Path

import pytest

from agentflow import issue_intake as ii
from agentflow.issue_intake import (
    BUG_LABEL,
    EPIC_LABEL,
    IN_PROGRESS_LABEL,
    NEEDS_DECISION_LABEL,
    TRIAGED_LABEL,
    Issue,
    IssueIntakeError,
    builder_selectable,
    claim_issue,
    issue_source_ref,
    label_claimed,
    label_issue,
    list_open_issues,
    main,
    release_issue,
    spec_linked_issue_numbers,
    unlabel_issue,
    untriaged,
)
from agentflow.config import WorkSource

HOST_REF = "owner/repo"

_RAW = [
    {"number": 901, "title": "Bounded bug", "labels": [{"name": "bug"}], "body": "b1", "url": "u/901"},
    {"number": 902, "title": "Epic-sized", "labels": [], "body": "", "url": "u/902"},
    {
        "number": 903,
        "title": "Already triaged",
        "labels": [{"name": "triaged"}, {"name": "bug"}],
        "body": "b3",
        "url": "u/903",
    },
    {"number": 904, "title": "Triaged epic", "labels": [{"name": "triaged"}, {"name": "epic"}]},
]


class FakeRunner:
    """Records every arg vector; returns canned (returncode, stdout, stderr)."""

    def __init__(self, stdout: str = "[]", returncode: int = 0, stderr: str = ""):
        self.calls: list = []
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.returncode, self.stdout, self.stderr


def _issue(number: int, *labels: str) -> Issue:
    return Issue(number=number, title=f"issue {number}", labels=tuple(labels))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Constants / dataclass
# --------------------------------------------------------------------------- #
def test_label_constants() -> None:
    assert TRIAGED_LABEL == "triaged"
    assert EPIC_LABEL == "epic"
    assert BUG_LABEL == "bug"
    assert IN_PROGRESS_LABEL == "in-progress"
    assert NEEDS_DECISION_LABEL == "needs-decision"


def test_the_module_docstring_enumerates_every_label_and_admits_nothing_writes_the_marker() -> None:
    """The module docstring is this file's stated contract and the nearest thing the repo has
    to a canonical label vocabulary - there is no single label-vocabulary doc. It said "Two
    label sets" and spelled builder_selectable out as `triaged` AND NOT `epic`, which gh#56
    makes false, and a stale enumeration is worse than none because it reads authoritative.

    The second half of this assertion is the honesty gate: no loop skill runs claim/release,
    so the exclusion is inert, and a mechanism that ships with correct-sounding prose beside
    an unwired clause is the exact shape of gh#139 and gh#152. When the follow-up wires it,
    this test goes red and the sentence has to be removed rather than quietly left to rot.
    """
    doc = ii.__doc__ or ""
    assert "Two label sets" not in doc
    for label in (TRIAGED_LABEL, EPIC_LABEL, IN_PROGRESS_LABEL, NEEDS_DECISION_LABEL):
        assert f"``{label}``" in doc, f"module docstring does not name {label}"
    assert "NOTHING IN THIS REPOSITORY WRITES ``in-progress`` ON ITS OWN YET." in doc
    # The marker is unwired, so the docstring must name the gate that does fire.
    assert "``held_issue_numbers``" in doc


def test_issue_is_frozen_with_defaults() -> None:
    i = Issue(number=1, title="t", labels=("bug",))
    assert i.body == "" and i.url == ""
    with pytest.raises(Exception):
        i.number = 2  # type: ignore[misc]


def test_issue_source_ref() -> None:
    assert issue_source_ref(902) == "gh#902"


# --------------------------------------------------------------------------- #
# list_open_issues
# --------------------------------------------------------------------------- #
def test_list_open_issues_arg_vector_and_parse() -> None:
    runner = FakeRunner(stdout=json.dumps(_RAW))
    issues = list_open_issues(HOST_REF, kind="github-issues", cli="gh", runner=runner, limit=50)
    assert runner.calls == [
        ["gh", "issue", "list", "--repo", HOST_REF, "--state", "open", "--limit", "50",
         "--json", "number,title,labels,body,url"]
    ]
    assert [i.number for i in issues] == [901, 902, 903, 904]
    assert issues[0] == Issue(number=901, title="Bounded bug", labels=("bug",), body="b1", url="u/901")
    assert issues[2].labels == ("triaged", "bug")
    # missing body/url fields default, never KeyError
    assert issues[3].body == "" and issues[3].url == ""


def test_list_open_issues_default_limit() -> None:
    runner = FakeRunner(stdout="[]")
    assert list_open_issues(HOST_REF, kind="github-issues", cli="gh", runner=runner) == []
    argv = runner.calls[0]
    assert argv[0] == "gh"
    assert argv[argv.index("--limit") + 1] == "200"


def test_unverified_cli_refuses_rather_than_emitting_gh_flags() -> None:
    """The argv shapes are gh's. A tracker CLI whose surface differs (glab, tea)
    must fail loudly before a process is spawned, not send gh flags at it."""
    runner = FakeRunner(stdout="[]")
    with pytest.raises(IssueIntakeError) as exc:
        list_open_issues(HOST_REF, kind="gitea", cli="glab", runner=runner)
    assert "no verified argv shape" in str(exc.value)
    assert runner.calls == []
    with pytest.raises(IssueIntakeError):
        label_issue(1, ["triaged"], HOST_REF, kind="gitea", cli="glab", runner=runner)
    assert runner.calls == []


def test_malformed_scalars_raise_typed_errors() -> None:
    """Every process boundary is loud AND typed: no raw ValueError/TypeError."""
    for raw, needle in (
        ([{"number": "abc"}], "non-integer 'number'"),
        ([{"number": [1]}], "non-integer 'number'"),
        # bool is an int subclass: True would silently parse as issue #1
        ([{"number": True}], "non-integer 'number'"),
        # a float would truncate to the WRONG issue, and inf raises OverflowError
        # (neither TypeError nor ValueError), so coercion cannot be the guard
        ([{"number": 12.9}], "non-integer 'number'"),
        ([{"number": float("inf")}], "non-integer 'number'"),
        ([{"number": 0}], "non-positive 'number'"),
        ([{"number": 1, "labels": 7}], "malformed 'labels'"),
        ([{"number": 1, "labels": "triaged"}], "malformed 'labels'"),
    ):
        runner = FakeRunner(stdout=json.dumps(raw))
        with pytest.raises(IssueIntakeError) as exc:
            list_open_issues(HOST_REF, kind="github-issues", cli="gh", runner=runner)
        assert needle in str(exc.value)


def test_numeric_string_number_is_still_accepted() -> None:
    """Some CLIs quote the number; that is a shape we tolerate, unlike a float."""
    runner = FakeRunner(stdout=json.dumps([{"number": "902", "title": "t"}]))
    assert list_open_issues(HOST_REF, kind="github-issues", cli="gh", runner=runner)[0].number == 902


def test_label_issue_refuses_a_bare_string_and_embedded_commas() -> None:
    """The write path needs the read path's shape guard: iterating "triaged"
    yields characters, and the argv joins on ',' so an embedded comma splits."""
    runner = FakeRunner()
    with pytest.raises(IssueIntakeError) as exc:
        label_issue(7, "triaged", HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert "sequence of names" in str(exc.value)
    with pytest.raises(IssueIntakeError) as exc:
        label_issue(7, ["a,b"], HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert "may not contain" in str(exc.value)
    assert runner.calls == []


def test_walk_failure_raises_typed_error(tmp_path, monkeypatch) -> None:
    """The junction-loop branch: rglob itself raising must be typed, not a traceback."""
    refine = tmp_path / "design" / "specs"
    refine.mkdir(parents=True)

    def boom(self, *a, **kw):
        raise OSError(3, "The system cannot find the path specified")

    monkeypatch.setattr(Path, "rglob", boom)
    with pytest.raises(IssueIntakeError) as exc:
        spec_linked_issue_numbers(refine)
    assert "could not walk" in str(exc.value)


def test_unreadable_refine_parent_raises_typed_error(tmp_path, monkeypatch) -> None:
    """is_dir() itself raises EACCES on POSIX when the parent is unreadable —
    it must sit inside the guarded region, not above it."""
    refine = tmp_path / "design" / "specs"
    refine.mkdir(parents=True)

    def boom(self, *a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_dir", boom)
    with pytest.raises(IssueIntakeError) as exc:
        spec_linked_issue_numbers(refine)
    assert "could not walk" in str(exc.value)


def test_unreadable_refine_tree_raises_typed_error(tmp_path, monkeypatch) -> None:
    """A junction loop or an unreadable spec is an IssueIntakeError, not a traceback."""
    refine = tmp_path / "design" / "specs"
    refine.mkdir(parents=True)
    (refine / "a.md").write_text("---\nsource: gh#1\n---\n", encoding="utf-8")
    assert spec_linked_issue_numbers(refine) == {1}

    real_read = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "a.md":
            raise PermissionError(13, "denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(IssueIntakeError) as exc:
        spec_linked_issue_numbers(refine)
    assert "could not read" in str(exc.value)


@pytest.mark.parametrize(
    "kind,cli",
    [("none", "gh"), ("filesystem", "gh"), ("github-issues", "none"), ("gitea", "none"), ("none", "none")],
)
def test_list_open_issues_noop_never_calls_runner(kind: str, cli: str) -> None:
    runner = FakeRunner(stdout=json.dumps(_RAW))
    assert list_open_issues(HOST_REF, kind=kind, cli=cli, runner=runner) == []
    assert runner.calls == []


def test_list_open_issues_nonzero_exit_raises() -> None:
    runner = FakeRunner(stdout="", returncode=1, stderr="auth required")
    with pytest.raises(IssueIntakeError) as exc:
        list_open_issues(HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert "auth required" in str(exc.value)


def test_a_nonzero_exit_reports_both_streams_labelled() -> None:
    """gh#478's second site. This module held the same `stderr or stdout` line as
    `save_summariser`, so it had the same defect: `gh` prints advisories (version notices,
    auth warnings) on stderr that accompany successful runs too, and whenever one was present
    a refusal printed on stdout was dropped entirely rather than shown after it.

    Both streams, each labelled. The inversion — `stdout or stderr` — is not the repair and is
    gated package-wide in `test_save_summariser.py`; see `agentflow.stream_report` for why.
    """
    runner = FakeRunner(
        stdout="HTTP 422: cannot list issues", returncode=1, stderr="a new gh is available"
    )
    with pytest.raises(IssueIntakeError) as exc:
        list_open_issues(HOST_REF, kind="github-issues", cli="gh", runner=runner)
    message = str(exc.value)
    assert "stdout: HTTP 422: cannot list issues" in message, (
        f"the cause on stdout was dropped in favour of the advisory on stderr: {message!r}"
    )
    assert "stderr: a new gh is available" in message
    assert "exited 1" in message


def test_a_silent_nonzero_exit_names_both_streams_as_empty() -> None:
    """A bare label with nothing after it reads like a broken f-string; `<empty>` is a claim."""
    with pytest.raises(IssueIntakeError) as exc:
        list_open_issues(
            HOST_REF, kind="github-issues", cli="gh",
            runner=FakeRunner(stdout="", returncode=2, stderr=""),
        )
    assert "stdout: <empty>; stderr: <empty>" in str(exc.value)


def test_list_open_issues_malformed_json_raises() -> None:
    runner = FakeRunner(stdout="not json {")
    with pytest.raises(IssueIntakeError):
        list_open_issues(HOST_REF, kind="github-issues", cli="gh", runner=runner)


def test_list_open_issues_non_list_json_raises() -> None:
    runner = FakeRunner(stdout=json.dumps({"number": 1}))
    with pytest.raises(IssueIntakeError):
        list_open_issues(HOST_REF, kind="github-issues", cli="gh", runner=runner)


def test_list_open_issues_default_runner_is_subprocess(monkeypatch) -> None:
    """Without an injected runner the module shells out via subprocess.run."""
    seen: list = []

    class _Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def _fake_run(argv, **kwargs):
        seen.append(list(argv))
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        return _Proc()

    monkeypatch.setattr(ii.subprocess, "run", _fake_run)
    assert list_open_issues(HOST_REF, kind="github-issues", cli="gh") == []
    assert seen and seen[0][:3] == ["gh", "issue", "list"]


def test_default_runner_missing_binary_raises(monkeypatch) -> None:
    def _raise(argv, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(ii.subprocess, "run", _raise)
    with pytest.raises(IssueIntakeError):
        list_open_issues(HOST_REF, kind="github-issues", cli="gh")


# --------------------------------------------------------------------------- #
# spec_linked_issue_numbers
# --------------------------------------------------------------------------- #
def test_spec_linked_issue_numbers_parses_quoted_and_multiple(tmp_path: Path) -> None:
    refine = tmp_path / "design" / "specs"
    _write(refine / "a.md", '---\nslug: a\nsource: "carved from gh#902 (see also gh#905)"\n---\n# a\n')
    _write(refine / "b.md", "---\nslug: b\nsource: gh#910\n---\nbody mentions gh#999 but not in frontmatter\n")
    _write(refine / "c.md", "---\nslug: c\nsource: 'a person said so'\n---\n")
    _write(refine / "d.md", "# no frontmatter\nsource: gh#777\n")
    _write(refine / "notes.txt", "---\nsource: gh#888\n---\n")
    assert spec_linked_issue_numbers(refine) == {902, 905, 910}


def test_spec_linked_issue_numbers_list_value_and_nested_dir(tmp_path: Path) -> None:
    refine = tmp_path / "design" / "specs"
    _write(refine / "list.md", '---\nsource:\n  - gh#11\n  - "gh#12 and gh#13"\n---\n')
    _write(refine / "archive" / "old.md", "---\nsource: gh#14\n---\n")
    assert spec_linked_issue_numbers(refine) == {11, 12, 13, 14}


def test_spec_linked_issue_numbers_unparseable_yaml_falls_back(tmp_path: Path) -> None:
    refine = tmp_path / "design" / "specs"
    # an unquoted colon-in-prose breaks yaml; the raw `source:` line still yields the ref
    _write(refine / "bad.md", "---\nslug: bad\nsource: person: said gh#42\ntitle: [broken\n---\n")
    assert spec_linked_issue_numbers(refine) == {42}


def test_spec_linked_issue_numbers_missing_dir_is_empty(tmp_path: Path) -> None:
    assert spec_linked_issue_numbers(tmp_path / "nope") == set()


def test_a_bom_prefixed_spec_is_still_cross_linked(tmp_path: Path) -> None:
    """gh#1000: PowerShell's Out-File writes a UTF-8 BOM, which sat in front of `---` and hid
    the frontmatter. b.md is the BOM-less control."""
    refine = tmp_path / "specs"
    refine.mkdir()
    (refine / "a.md").write_bytes(b"\xef\xbb\xbf---\nstatus: speccing\nsource: gh#30\n---\n# A\n")
    (refine / "b.md").write_bytes(b"---\nstatus: speccing\nsource: gh#31\n---\n# B\n")
    assert spec_linked_issue_numbers(refine) == {30, 31}


def test_a_bom_prefixed_done_spec_is_still_a_fix_claimant(tmp_path: Path) -> None:
    (tmp_path / "2026-09-01-bom.md").write_bytes(
        b"\xef\xbb\xbf---\ncurrent_state: done\nsource: gh#30\n---\n# A\n")
    (tmp_path / "2026-09-02-plain.md").write_bytes(
        b"---\ncurrent_state: done\nsource: gh#31\n---\n# B\n")
    links = ii.done_spec_links(tmp_path)
    assert sorted((ln.slug, sorted(ln.issues)) for ln in links) == [("bom", [30]), ("plain", [31])]


# --------------------------------------------------------------------------- #
# Filters — label sets are case-sensitive exact matches
# --------------------------------------------------------------------------- #
def test_untriaged_drops_triaged_and_linked() -> None:
    issues = [_issue(901, "bug"), _issue(902), _issue(903, "triaged", "bug"), _issue(904, "Triaged")]
    out = untriaged(issues, linked={902})
    assert [i.number for i in out] == [901, 904]


def test_untriaged_preserves_order_and_empty() -> None:
    assert untriaged([], linked=set()) == []
    issues = [_issue(5), _issue(3), _issue(4)]
    assert [i.number for i in untriaged(issues, linked=set())] == [5, 3, 4]


def test_builder_selectable_keeps_triaged_non_epic() -> None:
    issues = [
        _issue(901, "bug"),
        _issue(903, "triaged", "bug"),
        _issue(904, "triaged", "epic"),
        _issue(905, "triaged"),
        _issue(906, "TRIAGED"),
        _issue(907, "epic"),
    ]
    assert [i.number for i in builder_selectable(issues)] == [903, 905]


def test_a_claimed_issue_is_not_selectable_and_an_ordinary_one_still_is() -> None:
    """THE regression test for gh#56 - a lane was mid-build on an issue with a live worktree
    and ten modified files, and builder_selectable still offered it.

    The control is half the test. An exclusion clause that swallowed every triaged issue
    would satisfy the first assertion on its own, and this is the same fixture-blindness the
    harness's `epic` seed exists to prevent (harness/check.py, `_seeded_triaged_epic`).
    """
    claimed = _issue(52, "triaged", "bug", "in-progress")   # a lane holds it
    ordinary = _issue(53, "triaged", "bug")                 # the control
    assert [i.number for i in builder_selectable([claimed, ordinary])] == [53]


def test_the_marker_exclusion_is_case_sensitive_like_the_other_two() -> None:
    """Same register as the `TRIAGED` seed above: the module documents exact, case-sensitive
    matches, so a differently-cased label is a DIFFERENT label and must not gate anything."""
    issues = [_issue(905, "triaged", "In-Progress"), _issue(906, "triaged", "IN-PROGRESS")]
    assert [i.number for i in builder_selectable(issues)] == [905, 906]


def test_a_claimed_epic_is_still_excluded_for_being_an_epic() -> None:
    """The three clauses are order-independent; an issue may fail more than one at a time."""
    assert builder_selectable([_issue(904, "triaged", "epic", "in-progress")]) == []
    assert label_claimed([_issue(904, "triaged", "epic", "in-progress")]) == []


def test_a_decision_bearing_issue_is_not_selectable_and_an_ordinary_one_still_is() -> None:
    """gh#446 - a triaged bug whose body leaves a product call open is not build work; a lane
    that takes it either stalls or makes the call itself. 13 of 31 triaged bugs were this
    shape when it was measured. The control rules out an exclusion that swallows everything."""
    decision = _issue(446, "triaged", "bug", NEEDS_DECISION_LABEL)
    ordinary = _issue(447, "triaged", "bug")                 # the control
    assert [i.number for i in builder_selectable([decision, ordinary])] == [447]


def test_the_decision_exclusion_is_case_sensitive_like_the_others() -> None:
    issues = [_issue(905, "triaged", "Needs-Decision"), _issue(906, "triaged", "NEEDS-DECISION")]
    assert [i.number for i in builder_selectable(issues)] == [905, 906]


def test_a_decision_bearing_issue_is_still_bug_work_for_every_other_view() -> None:
    """The label gates SELECTION only. An issue a lane is already building, or one a registry
    row claims, must stay visible in label_claimed and in_flight even while it waits on a
    call - dropping it from the shared base list would hide live work."""
    held = _issue(448, "triaged", "bug", NEEDS_DECISION_LABEL, "in-progress")
    registry_held = _issue(449, "triaged", "bug", NEEDS_DECISION_LABEL)
    assert [i.number for i in label_claimed([held])] == [448]
    assert [i.number for i in ii.in_flight([held, registry_held], ["gh#449"])] == [448, 449]


def test_the_builder_selectable_docstring_lists_the_decision_exclusion_with_its_reason() -> None:
    doc = builder_selectable.__doc__ or ""
    assert "``needs-decision``" in doc
    assert "a product call must come first" in doc


def test_awaiting_decision_is_unclaimed_triaged_bug_work_carrying_the_label() -> None:
    """gh#446 - the view that keeps a decision-bearing issue in front of the humans who must
    make the call. Controls: an ordinary bug, a CLAIMED decision-bearing bug (held, so it
    belongs to label_claimed), a decision-bearing epic and an untriaged one (not bug work)."""
    issues = [
        _issue(810, "triaged", "bug", NEEDS_DECISION_LABEL),                   # awaiting
        _issue(811, "triaged", "bug"),                                         # selectable
        _issue(812, "triaged", "bug", NEEDS_DECISION_LABEL, IN_PROGRESS_LABEL),  # claimed
        _issue(813, "triaged", "epic", NEEDS_DECISION_LABEL),                  # an epic
        _issue(814, "bug", NEEDS_DECISION_LABEL),                              # untriaged
        _issue(815, "triaged", "Needs-Decision"),                              # wrong case
    ]
    assert [i.number for i in ii.awaiting_decision(issues)] == [810]


def test_a_claimed_decision_bearing_issue_is_claimed_not_awaiting() -> None:
    """A lane already holds it, so it is live work first; listing it as waiting on a call too
    would double-count it and break the partition below."""
    held = _issue(816, "triaged", "bug", NEEDS_DECISION_LABEL, IN_PROGRESS_LABEL)
    assert [i.number for i in label_claimed([held])] == [816]
    assert ii.awaiting_decision([held]) == []


def test_selectable_claimed_and_awaiting_partition_the_bug_work_exactly() -> None:
    """Pins the three views so they cannot drift apart: every bug-work issue is in exactly
    one of them. If an issue can be dropped by one view without being picked up by another it
    is not busy, it is invisible - and invisible is the failure nobody notices, because it
    looks exactly like an idle fleet (gh#446 is the case that opened the third view)."""
    issues = [
        _issue(901, "bug"),                                              # untriaged
        _issue(903, "triaged", "bug"),                                   # selectable
        _issue(904, "triaged", "epic"),                                  # not bug work
        _issue(905, "triaged", "bug", "in-progress"),                    # claimed
        _issue(906, "triaged", "epic", "in-progress"),                   # not bug work either
        _issue(907, "triaged", "bug", "needs-decision"),                 # awaiting a call
        _issue(908, "triaged", "bug", "needs-decision", "in-progress"),  # claimed wins
    ]
    selectable = {i.number for i in builder_selectable(issues)}
    claimed = {i.number for i in label_claimed(issues)}
    awaiting = {i.number for i in ii.awaiting_decision(issues)}
    assert selectable == {903}
    assert claimed == {905, 908}
    assert awaiting == {907}
    assert selectable & claimed == set()
    assert selectable & awaiting == set()
    assert claimed & awaiting == set()
    bug_work = {i.number for i in issues
                if "triaged" in i.labels and "epic" not in i.labels}
    assert selectable | claimed | awaiting == bug_work


def test_the_docstrings_state_a_three_way_partition_not_a_gap() -> None:
    """The previous wording named the unclaimed decision-bearing issue as a row no view
    returned. That is fixed; a docstring still admitting it would teach the next reader
    that the gap is accepted."""
    for doc in (ii.__doc__ or "", label_claimed.__doc__ or ""):
        assert "awaiting_decision" in doc
        assert "in neither" not in doc
        assert "except for one row" not in doc


# --------------------------------------------------------------------------- #
# label_issue / unlabel_issue
# --------------------------------------------------------------------------- #
def test_label_issue_arg_vector() -> None:
    runner = FakeRunner()
    rc = label_issue(902, ["triaged", "epic"], HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert rc is None
    assert runner.calls == [
        ["gh", "issue", "edit", "902", "--repo", HOST_REF, "--add-label", "triaged,epic"]
    ]


def test_label_issue_nonzero_exit_raises() -> None:
    runner = FakeRunner(returncode=2, stderr="label not found")
    with pytest.raises(IssueIntakeError) as exc:
        label_issue(902, ["triaged"], HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert "label not found" in str(exc.value)


def test_label_issue_empty_labels_raises_without_runner_call() -> None:
    runner = FakeRunner()
    with pytest.raises(IssueIntakeError):
        label_issue(902, [], HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert runner.calls == []


@pytest.mark.parametrize("kind,cli", [("none", "gh"), ("filesystem", "gh"), ("github-issues", "none")])
def test_label_issue_noop_never_calls_runner(kind: str, cli: str) -> None:
    runner = FakeRunner()
    label_issue(902, ["triaged"], HOST_REF, kind=kind, cli=cli, runner=runner)
    assert runner.calls == []


def test_unlabel_issue_arg_vector() -> None:
    """`label_issue` only ever builds `--add-label`; the clear path is a new argv shape and
    gets its own assertion rather than being assumed symmetric."""
    runner = FakeRunner()
    rc = unlabel_issue(52, [IN_PROGRESS_LABEL], HOST_REF, kind="github-issues", cli="gh",
                       runner=runner)
    assert rc is None
    assert runner.calls == [
        ["gh", "issue", "edit", "52", "--repo", HOST_REF, "--remove-label", "in-progress"]
    ]


@pytest.mark.parametrize("labels", ["in-progress", ["a,b"], []])
def test_unlabel_issue_refuses_malformed_labels_without_calling_the_runner(labels) -> None:
    """A label removed by accident is as wrong as one added by accident, so the guards are
    the same three: a bare string iterates per character, an embedded comma splits one name
    into two, and an empty list would edit with no names at all."""
    runner = FakeRunner()
    with pytest.raises(IssueIntakeError):
        unlabel_issue(52, labels, HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert runner.calls == []


def test_unlabel_issue_nonzero_exit_raises() -> None:
    runner = FakeRunner(returncode=2, stderr="label not found")
    with pytest.raises(IssueIntakeError) as exc:
        unlabel_issue(52, [IN_PROGRESS_LABEL], HOST_REF, kind="github-issues", cli="gh",
                      runner=runner)
    assert "label not found" in str(exc.value)


# --------------------------------------------------------------------------- #
# claim_issue / release_issue — the mechanism that writes the marker (gh#56)
# --------------------------------------------------------------------------- #
class _ListThenEdit:
    """Runner that answers `issue list` from a script and records everything else.

    The claim path reads before it writes, so a single canned stdout cannot serve both calls;
    this hands out the next scripted page per list call and returns success for edits.
    """

    def __init__(self, *pages):
        self.pages = [json.dumps(p) for p in pages]
        self.calls: list = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "list" in argv:
            page = self.pages.pop(0) if self.pages else self.pages_exhausted()
            return 0, page, ""
        return 0, "", ""

    def pages_exhausted(self):
        raise AssertionError("the runner was asked to list more times than the test scripted")

    @property
    def edits(self):
        return [c for c in self.calls if "edit" in c]


_TRIAGED_ROW = {"number": 52, "title": "Bounded bug", "labels": [{"name": "triaged"}, {"name": "bug"}]}
_CLAIMED_ROW = {"number": 52, "title": "Bounded bug",
                "labels": [{"name": "triaged"}, {"name": "bug"}, {"name": "in-progress"}]}


def test_claim_writes_the_marker_once_and_a_second_claim_writes_nothing() -> None:
    """Re-claiming is the NORMAL path - lead-dev resumes an in-progress item at every wake -
    so a second claim is a no-op, not an error. It stays distinguishable through the return
    value rather than through a raised alarm nobody could act on."""
    runner = _ListThenEdit([_TRIAGED_ROW], [_CLAIMED_ROW])

    issue, wrote = claim_issue(52, HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert (issue.number, wrote) == (52, True)
    assert runner.edits == [
        ["gh", "issue", "edit", "52", "--repo", HOST_REF, "--add-label", "in-progress"]
    ]

    issue, wrote = claim_issue(52, HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert wrote is False
    assert len(runner.edits) == 1, "the second claim must not write again"


def test_release_clears_the_marker_and_releasing_an_unclaimed_issue_writes_nothing() -> None:
    runner = _ListThenEdit([_CLAIMED_ROW], [_TRIAGED_ROW])

    _, wrote = release_issue(52, HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert wrote is True
    assert runner.edits == [
        ["gh", "issue", "edit", "52", "--repo", HOST_REF, "--remove-label", "in-progress"]
    ]

    _, wrote = release_issue(52, HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert wrote is False
    assert len(runner.edits) == 1


@pytest.mark.parametrize("kind,cli", [("filesystem", "gh"), ("none", "gh"), ("github-issues", "none")])
def test_claiming_with_no_tracker_refuses_rather_than_silently_succeeding(kind, cli) -> None:
    """The anti-silent-success test, and the one deliberate asymmetry against `label_issue`.

    Reading a tracker that does not exist yields [] and that is honest. A CLAIM that silently
    did not happen leaves the lane believing it holds something written nowhere - so both
    claim and release refuse, before any process could be spawned.
    """
    runner = _ListThenEdit()
    for fn in (claim_issue, release_issue):
        with pytest.raises(IssueIntakeError) as exc:
            fn(52, HOST_REF, kind=kind, cli=cli, runner=runner)
        assert "no tracker configured" in str(exc.value)
    assert runner.calls == []


def test_claiming_an_issue_that_is_not_open_is_loud() -> None:
    """A typo'd number must not quietly do nothing. `list_open_issues` sees only open issues,
    so a closed issue and an absent one correctly refuse the same way."""
    runner = _ListThenEdit([])
    with pytest.raises(IssueIntakeError) as exc:
        claim_issue(52, HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert "no open issue #52" in str(exc.value)
    assert runner.edits == []


def test_claiming_from_a_full_page_refuses_rather_than_guessing() -> None:
    """A page that came back exactly `limit` long proves nothing about what is not on it, so
    'absent' and 'past the page' get different messages. Reporting 'no such issue' from a
    truncated read would be a confident wrong answer - the conflation the report's read path
    already refuses."""
    # limit=3, so the claim asks for 4: the fourth row proves issues exist past the page.
    page = [{"number": n, "title": "t", "labels": []} for n in range(700, 704)]
    runner = _ListThenEdit(page)
    with pytest.raises(IssueIntakeError) as exc:
        claim_issue(52, HOST_REF, kind="github-issues", cli="gh", runner=runner, limit=3)
    msg = str(exc.value)
    assert "first 3 open issues" in msg and "indistinguishable" in msg
    assert "no open issue" not in msg
    assert runner.edits == []
    argv = runner.calls[0]
    assert argv[argv.index("--limit") + 1] == "4"


def test_claiming_from_a_page_of_exactly_limit_rows_is_a_complete_answer() -> None:
    """gh#714 review: exactly `limit` open issues is a complete read, so an absent number is
    genuinely not open. Refusing it would be the off-by-one the probe row exists to remove."""
    page = [{"number": n, "title": "t", "labels": []} for n in range(700, 703)]
    runner = _ListThenEdit(page)
    with pytest.raises(IssueIntakeError, match="no open issue #52"):
        claim_issue(52, HOST_REF, kind="github-issues", cli="gh", runner=runner, limit=3)
    assert runner.edits == []


@pytest.mark.parametrize("labels,because", [
    ([{"name": "bug"}], "not `triaged`"),
    ([{"name": "triaged"}, {"name": "epic"}], "labelled `epic`"),
])
def test_claiming_an_unselectable_issue_refuses_before_writing(labels, because) -> None:
    """Marking an issue no builder could be given strands it behind a label that
    `builder_selectable` reads and nothing else would ever clear."""
    runner = _ListThenEdit([{"number": 52, "title": "t", "labels": labels}])
    with pytest.raises(IssueIntakeError) as exc:
        claim_issue(52, HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert because in str(exc.value)
    assert runner.edits == []


def test_release_does_not_reapply_claims_triaged_epic_guards() -> None:
    """Deliberately asymmetric. Release runs on merge and on abandonment, by which time the
    issue may have been relabelled `epic` or had `triaged` stripped; refusing then would wedge
    the marker onto exactly the issues that most need it cleared."""
    row = {"number": 52, "title": "t", "labels": [{"name": "epic"}, {"name": "in-progress"}]}
    runner = _ListThenEdit([row])
    _, wrote = release_issue(52, HOST_REF, kind="github-issues", cli="gh", runner=runner)
    assert wrote is True
    assert runner.edits[0][-2:] == ["--remove-label", "in-progress"]


def test_claim_on_an_unverified_cli_refuses_before_spawning() -> None:
    runner = _ListThenEdit([_TRIAGED_ROW])
    with pytest.raises(IssueIntakeError) as exc:
        claim_issue(52, HOST_REF, kind="gitea", cli="glab", runner=runner)
    assert "no verified argv shape" in str(exc.value)
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# CLI: python -m agentflow.issue_intake untriaged|selectable|awaiting --project <name> [--json]
# --------------------------------------------------------------------------- #
class _Env:
    def __init__(self, kind: str, cli: str, data_root: str):
        class _GH:
            pass

        self.git_host = _GH()
        self.git_host.kind = kind
        self.git_host.cli = cli
        self.data_root = data_root


class _Proj:
    def __init__(self, host_ref: str, ws_kind: str, open_issue_page: int = ii.OPEN_ISSUE_PAGE):
        class _CR:
            pass

        self.canonical_repo = _CR()
        self.canonical_repo.host_ref = host_ref
        # the real dataclass, so a field intake starts reading cannot be missing from the fake
        self.work_source = WorkSource(kind=ws_kind, open_issue_page=open_issue_page)


@pytest.fixture
def cli_env(monkeypatch, tmp_path: Path):
    """Stub config loaders + runner so main() is exercised end-to-end offline."""
    from agentflow import config as _config

    refine = tmp_path / "projects" / "demo" / "design" / "specs"
    _write(refine / "carved.md", "---\nsource: gh#902\n---\n")
    _write(tmp_path / "projects" / "demo" / "status" / "registry.md", _held_registry())
    monkeypatch.setattr(_config, "load_environment", lambda: _Env("github", "gh", str(tmp_path)))
    monkeypatch.setattr(_config, "load_project", lambda name: _Proj(HOST_REF, "github-issues"))
    runner = FakeRunner(stdout=json.dumps(_RAW))
    monkeypatch.setattr(ii, "_subprocess_runner", runner)
    return runner


def test_cli_untriaged_text(cli_env, capsys) -> None:
    assert main(["untriaged", "--project", "demo"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["#901  Bounded bug  [bug]"]
    assert cli_env.calls[0][:5] == ["gh", "issue", "list", "--repo", HOST_REF]


# --------------------------------------------------------------------------- #
# The held set: an issue already on the board is not offered again
# --------------------------------------------------------------------------- #
def _held_registry(queue: str = "", agents: str = "", notes: str = "") -> str:
    """A fake board in the registry's own shape; only the cells under test vary."""
    return (
        "# Fleet Registry - demo\n\n"
        "## Agents\n\n"
        "| agent | status | work | phase | heartbeat | eta | waiting_on |\n"
        "|---|---|---|---|---|---|---|\n" + agents + "\n"
        "## Queue\n\n"
        "- **Deploy lock** - holder: none, taken-at: -\n"
        "- **Notes** - (none)\n" + notes + "\n"
        "| id | work item | lane | claimed_by | state | waiting_on | notes |\n"
        "|---|---|---|---|---|---|---|\n" + queue + "\n"
        "## Questions\n\n"
        "## Blockers\n\n"
        "| id | raised_by | owner | work | state | description |\n"
        "|---|---|---|---|---|---|\n\n"
        "## Directives\n\n"
    )


def _held(tmp_path: Path, **cells) -> frozenset:
    path = tmp_path / "registry.md"
    _write(path, _held_registry(**cells))
    return ii.read_held_set(path)


_BUGS = [_issue(n, "triaged", "bug") for n in (44, 446, 447)]


@pytest.mark.parametrize("row", [
    "| E-0001 | BUG gh#446 - fix | jr-dev-1 | jr-dev-1 | building | - | - |\n",
    "| E-0001 | BUG gh#446 - fix | jr-dev-1 | - | ready | - | - |\n",
], ids=["claimed", "unclaimed-ready"])
def test_a_live_queue_row_citing_an_issue_keeps_it_out_of_selectable(tmp_path, row) -> None:
    """A published row has already handed its issue out, claimed or not. gh#44 is the control
    for exact matching: it is a prefix of gh#446 and must stay selectable."""
    held = _held(tmp_path, queue=row)
    assert held == {446}
    assert [i.number for i in builder_selectable(_BUGS, held)] == [44, 447]


@pytest.mark.parametrize("status", ["building", "waiting", "halted"])
def test_an_agents_row_citing_an_issue_keeps_it_out_of_selectable(tmp_path, status) -> None:
    agents = f"| lead-dev | {status} | E-0001 | fixing gh#447 | - | - | - |\n"
    assert [i.number for i in builder_selectable(_BUGS, _held(tmp_path, agents=agents))] == [44, 446]


def test_an_idle_agents_row_citing_an_issue_does_not_hold_it(tmp_path) -> None:
    """An idle row's phase records finished work, e.g. the completion phase of a lane that
    published an issue; it must not keep that issue held until the next status write."""
    agents = "| lead-dev | idle | - | no spec: issue-published, Source: gh#447 | - | - | - |\n"
    held = _held(tmp_path, agents=agents)
    assert 447 not in held
    assert [i.number for i in builder_selectable(_BUGS, held)] == [44, 446, 447]


@pytest.mark.parametrize("status, holds", [
    ("`building`", True),
    ("Building", True),
    ("stalled", True),
    ("`idle`", False),
    ("IDLE", False),
], ids=["backticked-building", "capitalised-building", "off-enum", "backticked-idle", "upper-idle"])
def test_an_off_enum_status_cell_never_shrinks_the_held_set(tmp_path, status, holds) -> None:
    """Only an idle row stops holding; a damaged or off-enum status cell keeps holding, so a
    malformed board can never offer held work as free."""
    agents = f"| lead-dev | {status} | E-0001 | fixing gh#447 | - | - | - |\n"
    assert (447 in _held(tmp_path, agents=agents)) is holds


@pytest.mark.parametrize("cells", [
    {"queue": "| E-0001 | BUG gh#446 - fix | jr-dev-1 | jr-dev-1 | done | - | - |\n"},
    {"queue": "| E-0001 | BUG gh#446 - fix | jr-dev-1 | - | withdrawn | - | - |\n"},
    {"notes": "- lead-dev: gh#446 came up while reviewing something else\n"},
    {"queue": "| E-0001 | BUG gh#447 - fix | jr-dev-1 | jr-dev-1 | building | - | see gh#446 |\n"},
], ids=["done-row", "withdrawn-row", "coordination-note", "notes-cell"])
def test_a_finished_row_or_a_note_citing_an_issue_does_not_hold_it(tmp_path, cells) -> None:
    held = _held(tmp_path, **cells)
    assert 446 not in held
    assert 446 in {i.number for i in builder_selectable(_BUGS, held)}


@pytest.mark.parametrize("ref", ["gh#446", "GH#446", "Gh#446", "#446", "(#446)"],
                         ids=["canonical", "upper", "mixed", "bare", "bare-in-parens"])
@pytest.mark.parametrize("where", ["queue", "agents"])
def test_an_issue_cited_in_any_case_or_bare_is_held(tmp_path, ref, where) -> None:
    """gh#909: the held predicate matched only lowercase ``gh#N``, so a row citing ``GH#14`` or
    ``#12`` left the issue selectable while a lane built it. gh#44 stays the prefix control."""
    cells = {
        "queue": {"queue": f"| E-0001 | BUG {ref} - fix | jr-dev-1 | - | ready | - | - |\n"},
        "agents": {"agents": f"| lead-dev | building | E-0001 | fixing {ref} | - | - | - |\n"},
    }[where]
    held = _held(tmp_path, **cells)
    assert held == {446}
    assert [i.number for i in builder_selectable(_BUGS, held)] == [44, 447]


@pytest.mark.parametrize("state", ["done", "withdrawn"])
@pytest.mark.parametrize("ref", ["#446", "GH#446"])
def test_a_finished_row_citing_an_issue_bare_or_upper_does_not_hold_it(tmp_path, ref, state) -> None:
    held = _held(tmp_path, queue=f"| E-0001 | BUG {ref} - fix | jr-dev-1 | - | {state} | - | - |\n")
    assert 446 not in held


@pytest.mark.parametrize("ref", ["PR #446", "pr #446", "PR#446", "pr#446", "x#446", "##446"])
def test_a_pull_request_or_glued_hash_reference_does_not_hold_an_issue(tmp_path, ref) -> None:
    """A ``#`` glued to a word, or written ``PR #N``, names a pull request or nothing; only
    ``gh#N`` in any case and a free-standing ``#N`` hold (``registry.HELD_REF_RE``)."""
    held = _held(tmp_path, queue=f"| E-0001 | BUG {ref} follow-up | jr-dev-1 | - | ready | - | - |\n")
    assert held == frozenset()


def test_builder_selectable_without_a_held_set_is_label_only() -> None:
    assert [i.number for i in builder_selectable(_BUGS)] == [44, 446, 447]


def test_a_missing_registry_refuses_rather_than_reading_as_a_free_board(tmp_path) -> None:
    with pytest.raises(IssueIntakeError, match="held work cannot be told from free work"):
        ii.read_held_set(tmp_path / "absent.md")


@pytest.mark.parametrize("text", [
    "## Agents\n\n| broken",
    _held_registry().replace("|---|---|---|---|---|---|---|\n\n## Questions",
                             "\n## Questions"),
], ids=["no-queue-section", "queue-table-gone"])
def test_an_unparseable_registry_refuses(tmp_path, text) -> None:
    path = tmp_path / "registry.md"
    _write(path, text)
    with pytest.raises(IssueIntakeError, match="held work cannot be told from free work"):
        ii.read_held_set(path)


_DUP_BUILDING = "| E-0100 | gh#5 fix thing | jr-dev-1 | jr-dev-1 | building | - | - |\n"
_DUP_DONE = "| E-0100 | gh#7 other | jr-dev-2 | jr-dev-2 | done | - | - |\n"


@pytest.mark.parametrize("queue, problem", [
    (_DUP_BUILDING + _DUP_DONE, "appears on 2 rows"),
    (_DUP_DONE + _DUP_BUILDING, "appears on 2 rows"),
    ("| E-0101 | gh#6 fix | jr-dev-1 | jr-dev-1 | building | - |\n", "row has 6 cells, expected 7"),
], ids=["a-dup-id-building-first", "a2-dup-id-done-first", "b-short-row"])
def test_a_board_whose_queue_rows_the_parser_would_drop_refuses(tmp_path, queue, problem) -> None:
    """gh#1000: the parser keeps the last of a duplicated id and drops a short row, so the held
    row vanished and selectable offered the issue with exit 0. Same defects `validate` names."""
    path = tmp_path / "registry.md"
    _write(path, _held_registry(queue=queue))
    with pytest.raises(IssueIntakeError, match="held work cannot be told from free work") as exc:
        ii.read_held_set(path)
    assert problem in str(exc.value)


def test_a_board_whose_agents_rows_the_parser_would_drop_refuses(tmp_path) -> None:
    """The held set reads Agents work cells too, so a duplicated lane row drops one of them."""
    agents = (
        "| jr-dev-1 | building | E-0100 | fixing gh#5 | - | - | - |\n"
        "| jr-dev-1 | idle | - | - | - | - | - |\n"
    )
    path = tmp_path / "registry.md"
    _write(path, _held_registry(agents=agents))
    with pytest.raises(IssueIntakeError, match="held work cannot be told from free work") as exc:
        ii.read_held_set(path)
    assert "appears on 2 rows" in str(exc.value)


def test_a_well_formed_ready_row_still_reads_as_held(tmp_path) -> None:
    """The control for the refusals above: a valid board still reads."""
    assert _held(tmp_path, queue="| E-0103 | gh#9 fix | jr-dev-1 | | ready | - | - |\n") == {9}


def test_cli_selectable_refuses_on_a_duplicate_queue_id(cli_env, capsys) -> None:
    from agentflow import config as _config

    reg = Path(_config.load_environment().data_root) / "projects" / "demo" / "status" / "registry.md"
    _write(reg, _held_registry(queue=_DUP_BUILDING.replace("gh#5 ", "gh#903 ") + _DUP_DONE))
    assert main(["selectable", "--project", "demo", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "appears on 2 rows" in captured.err


def _mirror_index(cli_env) -> str:
    from agentflow import config as _config

    root = Path(_config.load_environment().data_root) / "projects" / "demo"
    assert main(["mirror", "--project", "demo"]) == 0
    return (root / "design" / "bugs" / "index.md").read_text(encoding="utf-8")


_NOT_ON_BOARD = "not on the board"


@pytest.mark.parametrize("damage", ["separator-removed", "registry-missing"])
def test_mirror_on_an_unreadable_board_says_so_in_the_index(cli_env, capsys, damage) -> None:
    """gh#1000: the index asserted `In flight (0)` and glossed selectable bugs as "not on the
    board" when the board was never read - indistinguishable from an idle fleet."""
    from agentflow import config as _config

    reg = Path(_config.load_environment().data_root) / "projects" / "demo" / "status" / "registry.md"
    if damage == "registry-missing":
        reg.unlink()
    else:
        header = "| id | work item | lane | claimed_by | state | waiting_on | notes |\n"
        _write(reg, _held_registry().replace(header + "|---|---|---|---|---|---|---|\n", header))
    text = _mirror_index(cli_env)
    assert _NOT_ON_BOARD not in text
    assert ii.BOARD_UNREADABLE_BANNER in text
    assert "**#903**" in text  # still listed, just no longer vouched for
    assert "in-flight grouping skipped" in capsys.readouterr().err


def test_mirror_on_a_readable_board_carries_no_banner(cli_env, capsys) -> None:
    """The control: a readable board renders exactly what it rendered before gh#1000."""
    text = _mirror_index(cli_env)
    assert "Board unreadable" not in text
    assert _NOT_ON_BOARD in text
    expected = ii.render_open_index([ii._parse_issue(r) for r in _RAW], {902}, "x")
    assert ii._body_without_stamp(text) == ii._body_without_stamp(expected)


def test_cli_selectable_subtracts_the_held_set(cli_env, capsys) -> None:
    from agentflow import config as _config

    reg = Path(_config.load_environment().data_root) / "projects" / "demo" / "status" / "registry.md"
    _write(reg, _held_registry(queue="| E-0001 | BUG gh#903 - fix | jr-dev-1 | - | ready | - | - |\n"))
    assert main(["selectable", "--project", "demo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_selectable_refuses_when_the_registry_is_missing(cli_env, capsys) -> None:
    from agentflow import config as _config

    reg = Path(_config.load_environment().data_root) / "projects" / "demo" / "status" / "registry.md"
    reg.unlink()
    assert main(["selectable", "--project", "demo", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "held work cannot be told from free work" in captured.err


def test_cli_selectable_json(cli_env, capsys) -> None:
    assert main(["selectable", "--project", "demo", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["number"] for d in data] == [903]
    assert data[0]["labels"] == ["triaged", "bug"]
    assert data[0]["url"] == "u/903"


_DECISION_ROW = {
    "number": 905,
    "title": "Waiting on a product call",
    "labels": [{"name": "triaged"}, {"name": "bug"}, {"name": "needs-decision"}],
    "body": "b5",
    "url": "u/905",
}


def test_cli_awaiting_lists_the_decision_bearing_bugs_and_selectable_does_not(
        monkeypatch, cli_env, capsys) -> None:
    """gh#446 - the reader path for awaiting_decision(). Without it the dev-manager pass that
    owes the call has no command that names the issues waiting on it. `selectable` over the
    same tracker read is the control: the two must not return each other's rows."""
    monkeypatch.setattr(ii, "_subprocess_runner",
                        FakeRunner(stdout=json.dumps(_RAW + [_DECISION_ROW])))
    assert main(["awaiting", "--project", "demo", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["number"] for d in data] == [905]
    assert data[0]["url"] == "u/905"

    assert main(["selectable", "--project", "demo", "--json"]) == 0
    assert [d["number"] for d in json.loads(capsys.readouterr().out)] == [903]


def test_cli_awaiting_text_uses_the_list_line_format(monkeypatch, cli_env, capsys) -> None:
    monkeypatch.setattr(ii, "_subprocess_runner",
                        FakeRunner(stdout=json.dumps(_RAW + [_DECISION_ROW])))
    assert main(["awaiting", "--project", "demo"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "#905  Waiting on a product call  [triaged, bug, needs-decision]"
    ]


def test_cli_noop_kind_prints_nothing_and_never_runs(monkeypatch, cli_env, capsys) -> None:
    from agentflow import config as _config

    monkeypatch.setattr(_config, "load_project", lambda name: _Proj(HOST_REF, "filesystem"))
    assert main(["untriaged", "--project", "demo"]) == 0
    assert capsys.readouterr().out == ""
    assert cli_env.calls == []


def test_cli_runner_failure_is_loud(monkeypatch, cli_env, capsys) -> None:
    monkeypatch.setattr(ii, "_subprocess_runner", FakeRunner(returncode=1, stderr="boom"))
    assert main(["untriaged", "--project", "demo"]) == 1
    assert "boom" in capsys.readouterr().err


def test_cli_config_error_is_loud(monkeypatch, capsys) -> None:
    from agentflow import config as _config

    def _raise():
        raise _config.ConfigError("data_root", "environment.md", "missing", "add it")

    monkeypatch.setattr(_config, "load_environment", _raise)
    assert main(["untriaged", "--project", "demo"]) == 1
    assert "data_root" in capsys.readouterr().err


def test_cli_rejects_bad_subcommand() -> None:
    with pytest.raises(SystemExit):
        main(["bogus", "--project", "demo"])


def _untriaged_page(count: int) -> str:
    """``count`` open issues newest-first, none triaged, so every one is on every list."""
    return json.dumps([
        {"number": n, "title": f"issue {n}", "labels": []} for n in range(count, 0, -1)
    ])


@pytest.mark.parametrize("mode", ["untriaged", "selectable", "awaiting", "mirror"])
def test_cli_list_modes_refuse_when_more_than_the_page_is_open(
        mode, monkeypatch, cli_env, capsys) -> None:
    """gh#714. gh returns newest-first, so the rows past the page are the oldest open issues.
    Printing the page anyway dropped them with exit 0; every list mode now refuses instead,
    loudly and before writing anything (the mirror included). The probe row - one more than
    the page - is the proof that issues exist past it."""
    monkeypatch.setattr(ii, "_subprocess_runner",
                        FakeRunner(stdout=_untriaged_page(ii.OPEN_ISSUE_PAGE + 1)))
    assert main([mode, "--project", "demo"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"refusing `{mode}`" in captured.err
    assert f"first {ii.OPEN_ISSUE_PAGE} open issues" in captured.err
    assert "page came back full" in captured.err


@pytest.mark.parametrize("count", [0, 1], ids=["exactly-the-page", "one-short"])
def test_cli_list_mode_at_or_under_the_page_is_complete_and_lists(
        count, monkeypatch, cli_env, capsys) -> None:
    """Exactly OPEN_ISSUE_PAGE open issues is a complete read and must list - refusing it was
    the off-by-one - and one short is the plain control. Both include the oldest issue."""
    size = ii.OPEN_ISSUE_PAGE - count
    monkeypatch.setattr(ii, "_subprocess_runner", FakeRunner(stdout=_untriaged_page(size)))
    assert main(["untriaged", "--project", "demo", "--json"]) == 0
    listed = [d["number"] for d in json.loads(capsys.readouterr().out)]
    assert len(listed) == size
    assert 1 in listed  # the oldest issue is on the list


def test_cli_list_modes_ask_the_tracker_for_one_row_past_the_page(cli_env, capsys) -> None:
    """The guard refuses only on MORE than OPEN_ISSUE_PAGE rows, so the argv must request one
    more than that; requesting exactly the page could never show the proof row, and the guard
    would pass every truncated read."""
    assert main(["untriaged", "--project", "demo"]) == 0
    argv = cli_env.calls[0]
    assert argv[argv.index("--limit") + 1] == str(ii.OPEN_ISSUE_PAGE + 1)


# --------------------------------------------------------------------------- #
# work_source.open_issue_page: the refusal's way out is a profile key, not a flag
# --------------------------------------------------------------------------- #
def test_cli_refusal_at_the_default_page_names_the_key_and_the_profile(
        monkeypatch, cli_env, capsys) -> None:
    """Absent key: the page is still 200 and 201 open still refuses, but the refusal now says
    how to get out - which key, in which file - instead of leaving 'retry next wake'."""
    from agentflow import config as _config

    monkeypatch.setattr(ii, "_subprocess_runner", FakeRunner(stdout=_untriaged_page(201)))
    assert main(["untriaged", "--project", "demo"]) == 1
    err = capsys.readouterr().err
    assert "first 200 open issues" in err
    assert "raise work_source.open_issue_page (now 200)" in err
    assert str(_config.config_home() / "projects" / "demo.md") in err


def test_cli_list_mode_reads_the_configured_page(monkeypatch, cli_env, capsys) -> None:
    """With the key at 300, 201 open issues is a complete read: it lists, oldest included,
    and the tracker is asked for 301 rows so the guard keeps its proof row at the new size."""
    from agentflow import config as _config

    monkeypatch.setattr(_config, "load_project",
                        lambda name: _Proj(HOST_REF, "github-issues", open_issue_page=300))
    runner = FakeRunner(stdout=_untriaged_page(201))
    monkeypatch.setattr(ii, "_subprocess_runner", runner)
    assert main(["untriaged", "--project", "demo", "--json"]) == 0
    listed = [d["number"] for d in json.loads(capsys.readouterr().out)]
    assert len(listed) == 201 and 1 in listed
    argv = runner.calls[0]
    assert argv[argv.index("--limit") + 1] == "301"


def test_cli_configured_page_still_refuses_past_itself(monkeypatch, cli_env, capsys) -> None:
    """Raising the key moves the guard; it does not remove it."""
    from agentflow import config as _config

    monkeypatch.setattr(_config, "load_project",
                        lambda name: _Proj(HOST_REF, "github-issues", open_issue_page=300))
    monkeypatch.setattr(ii, "_subprocess_runner", FakeRunner(stdout=_untriaged_page(301)))
    assert main(["selectable", "--project", "demo"]) == 1
    err = capsys.readouterr().err
    assert "first 300 open issues" in err and "(now 300)" in err


def test_cli_claim_reads_the_configured_page(monkeypatch, cli_env, capsys) -> None:
    """claim/release share the guard, so they must share the page: with the key at 300 the
    claim asks for 301 rows, not the 201 the old constant fixed."""
    from agentflow import config as _config

    monkeypatch.setattr(_config, "load_project",
                        lambda name: _Proj(HOST_REF, "github-issues", open_issue_page=300))
    rows = [{"number": n, "title": f"issue {n}", "labels": [{"name": "triaged"}]}
            for n in range(201, 0, -1)]
    runner = FakeRunner(stdout=json.dumps(rows))
    monkeypatch.setattr(ii, "_subprocess_runner", runner)
    assert main(["claim", "--project", "demo", "--issue", "1"]) == 0
    argv = runner.calls[0]
    assert argv[argv.index("--limit") + 1] == "301"


# --------------------------------------------------------------------------- #
# CLI: claim / release
# --------------------------------------------------------------------------- #
def test_cli_claim_then_reclaim_says_which_one_wrote(monkeypatch, cli_env, capsys) -> None:
    """A run that changed nothing has to be distinguishable from a run that did not happen -
    the same discipline the mirror's `wrote ... unchanged ...` line exists for."""
    runner = _ListThenEdit([_TRIAGED_ROW], [_CLAIMED_ROW])
    monkeypatch.setattr(ii, "_subprocess_runner", runner)

    assert main(["claim", "--project", "demo", "--issue", "52"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("claimed #52  Bounded bug  [in-progress added]")
    assert runner.edits == [
        ["gh", "issue", "edit", "52", "--repo", HOST_REF, "--add-label", "in-progress"]
    ]

    assert main(["claim", "--project", "demo", "--issue", "52"]) == 0
    assert "already claimed #52" in capsys.readouterr().out
    assert len(runner.edits) == 1


def test_cli_release_json_carries_the_changed_flag(monkeypatch, cli_env, capsys) -> None:
    """`changed` is the machine-readable form of wrote-vs-already; a caller that cannot see it
    has to guess, and the guess is what makes a claim look held when it was not."""
    runner = _ListThenEdit([_CLAIMED_ROW], [_TRIAGED_ROW])
    monkeypatch.setattr(ii, "_subprocess_runner", runner)

    assert main(["release", "--project", "demo", "--issue", "52", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first == {"issue": 52, "action": "release", "changed": True,
                     "labels": ["triaged", "bug", "in-progress"]}

    assert main(["release", "--project", "demo", "--issue", "52", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False


def test_cli_claim_says_on_stderr_that_no_loop_runs_it_yet(monkeypatch, cli_env, capsys) -> None:
    """The mechanism ships wired to nothing. An operator invoking it by hand is told so, and
    this assertion goes red the moment the follow-up removes the note - which is the point:
    a note that outlives its fact teaches the next reader to disbelieve the notes."""
    monkeypatch.setattr(ii, "_subprocess_runner", _ListThenEdit([_TRIAGED_ROW]))
    assert main(["claim", "--project", "demo", "--issue", "52"]) == 0
    assert "no loop skill is instructed to run claim/release yet" in capsys.readouterr().err


def test_cli_claim_failure_is_loud_and_exits_one(monkeypatch, cli_env, capsys) -> None:
    """No new exit codes: a refusal lands on main()'s one loud path like every other."""
    monkeypatch.setattr(ii, "_subprocess_runner", _ListThenEdit([]))
    assert main(["claim", "--project", "demo", "--issue", "52"]) == 1
    assert "no open issue #52" in capsys.readouterr().err


def test_cli_requires_an_issue_number_for_claim_and_release() -> None:
    for mode in ("claim", "release"):
        with pytest.raises(SystemExit):
            main([mode, "--project", "demo"])


@pytest.mark.parametrize("mode", ["untriaged", "selectable", "awaiting", "mirror"])
def test_cli_rejects_an_issue_number_on_a_list_mode(mode: str) -> None:
    """Rejected, not ignored. A flag that quietly does nothing is the same lie in miniature
    as a claim that quietly does not happen."""
    with pytest.raises(SystemExit):
        main([mode, "--project", "demo", "--issue", "52"])


# --------------------------------------------------------------------------- #
# work_source.kind gitea: refused whatever the cli, except the cli-none no-op
# --------------------------------------------------------------------------- #
def test_gitea_kind_with_gh_refuses_every_tracker_path_before_spawning() -> None:
    """gh is the verified cli, but it can only reach GitHub: a Gitea repo ref handed to it
    would be read and labelled on the wrong tracker. The kind gates, not only the cli."""
    runner = FakeRunner(stdout=json.dumps(_RAW))
    calls = (
        lambda: list_open_issues(HOST_REF, kind="gitea", cli="gh", runner=runner),
        lambda: label_issue(3, ["triaged", "bug"], HOST_REF, kind="gitea", cli="gh", runner=runner),
        lambda: unlabel_issue(3, ["triaged"], HOST_REF, kind="gitea", cli="gh", runner=runner),
        lambda: ii.list_closed_issues(HOST_REF, kind="gitea", cli="gh", month="2026-01",
                                      runner=runner),
        lambda: claim_issue(3, HOST_REF, kind="gitea", cli="gh", runner=runner),
        lambda: release_issue(3, HOST_REF, kind="gitea", cli="gh", runner=runner),
    )
    for call in calls:
        with pytest.raises(IssueIntakeError) as exc:
            call()
        assert "no verified argv shape for work_source.kind 'gitea'" in str(exc.value)
    assert runner.calls == []


def test_gitea_kind_with_cli_none_stays_the_documented_noop() -> None:
    runner = FakeRunner(stdout=json.dumps(_RAW))
    assert list_open_issues(HOST_REF, kind="gitea", cli="none", runner=runner) == []
    assert label_issue(3, ["triaged"], HOST_REF, kind="gitea", cli="none", runner=runner) is None
    assert unlabel_issue(3, ["triaged"], HOST_REF, kind="gitea", cli="none", runner=runner) is None
    assert ii.list_closed_issues(HOST_REF, kind="gitea", cli="none", month="2026-01",
                                 runner=runner) == []
    assert runner.calls == []


def test_cli_untriaged_refuses_a_gitea_project_on_gh(monkeypatch, cli_env, capsys) -> None:
    from agentflow import config as _config

    monkeypatch.setattr(_config, "load_project", lambda name: _Proj(HOST_REF, "gitea"))
    assert main(["untriaged", "--project", "demo"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no verified argv shape for work_source.kind 'gitea'" in captured.err
    assert cli_env.calls == []


def test_cli_untriaged_gitea_project_with_cli_none_prints_nothing(
        monkeypatch, cli_env, tmp_path, capsys) -> None:
    from agentflow import config as _config

    monkeypatch.setattr(_config, "load_environment", lambda: _Env("gitea", "none", str(tmp_path)))
    monkeypatch.setattr(_config, "load_project", lambda name: _Proj(HOST_REF, "gitea"))
    assert main(["untriaged", "--project", "demo"]) == 0
    assert capsys.readouterr().out == ""
    assert cli_env.calls == []


# --- gh#1123: refs and epic ids are ASCII-digit only, Queue keys use the shared predicate -----


def _arabic(digits: str) -> str:
    """`digits` spelled in Arabic-Indic digits: `\\d` matches these and `int()` reads them."""
    return "".join(chr(0x0660 + int(c)) for c in digits)


def _fullwidth(digits: str) -> str:
    return "".join(chr(0xFF10 + int(c)) for c in digits)


@pytest.mark.parametrize("spell", [_arabic, _fullwidth])
def test_a_source_ref_in_NON_ASCII_digits_names_no_issue(spell):
    assert ii._SOURCE_REF_RE.findall(f"gh#{spell('123')}") == []
    assert ii._SOURCE_REF_RE.findall(f"gh#12{spell('3')}") == [], "run-on cut short to gh#12"
    assert ii._SOURCE_REF_RE.findall("gh#123") == ["123"], "control"


@pytest.mark.parametrize("spell", [_arabic, _fullwidth])
def test_a_merged_PR_naming_its_issue_in_NON_ASCII_digits_claims_nothing(spell):
    for body in (f"Fixes #{spell('123')}", f"Fixes #12{spell('3')}",
                 f"Source: gh#{spell('123')}", f"Also addresses gh#{spell('123')}"):
        assert ii._pr_claimed_numbers({"title": "x", "body": body}) == set(), body
    assert ii._pr_claimed_numbers({"title": f"gh#{spell('123')} fix", "body": ""}) == set()
    assert ii._pr_claimed_numbers({"title": "gh#7 x", "body": "Fixes #123"}) == {7, 123}, (
        "control"
    )


@pytest.mark.parametrize("spell", [_arabic, _fullwidth])
def test_epic_numbers_in_NON_ASCII_digits_match_no_branch_and_no_row(spell):
    assert ii._EPIC_NUM_RE.findall(f"e{spell('1234')}-x") == []
    assert ii._EPIC_NUM_RE.findall(f"e123{spell('4')}-x") == [], "run-on cut short to e123"
    assert ii._ROW_EPIC_RE.match(f"E-{spell('1234')}-x") is None
    assert ii._ROW_EPIC_RE.match(f"E-123{spell('4')}") is None
    assert ii._EPIC_NUM_RE.findall("e1234-x") == ["1234"], "control"
    assert ii._ROW_EPIC_RE.match("E-1234-x").group(1) == "1234", "control"


def test_a_PR_and_a_row_both_spelling_the_epic_in_NON_ASCII_digits_name_no_fixer():
    """Both sides used to agree on the same non-ASCII digits, so the row's seat was credited."""
    row = ii._registry.QueueRow(f"E-{_arabic('1234')}-x", "other", "lead", "lead-dev",
                                "done", "-", "")
    pr = {"headRefName": f"e{_arabic('1234')}-x", "body": ""}
    assert ii.fixers(pr, [row]) == frozenset()


def test_the_log_row_read_rejects_exactly_what_the_shared_Queue_predicate_rejects(
    tmp_path, monkeypatch,
):
    (tmp_path / "registry-log.md").write_text(
        "| E-0768 | toy | lead | lead-dev | done | - | |\n"
        "| E-0769 | toy | lead | jr-dev-1 | done | - | |\n",
        encoding="utf-8",
    )
    rows, _ = ii.read_queue_rows(tmp_path)
    assert {r.id for r in rows} == {"E-0768", "E-0769"}, "control: the predicate accepts both"

    rule = ii._registry._ROW_KEY_RULES["Queue"]
    monkeypatch.setitem(ii._registry._ROW_KEY_RULES, "Queue",
                        (lambda key: key != "E-0768" and rule[0](key), *rule[1:]))
    rows, _ = ii.read_queue_rows(tmp_path)
    assert {r.id for r in rows} == {"E-0769"}, "a Queue key check bypassed registry._row_key_ok"


@pytest.mark.parametrize("number", ["\u0661\u0662", "\uff11\uff12", "\xb2"],
                         ids=["arabic-indic", "fullwidth", "superscript"])
def test_an_issue_number_in_NON_ASCII_digits_is_refused_not_coerced(number):
    """`str.isdigit` took these: `int()` read the first two as 12 and raised a bare ValueError
    on the superscript, which no caller catches as a refusal (gh#1123)."""
    with pytest.raises(IssueIntakeError):
        ii._parse_issue({"number": number, "title": "x", "labels": []})
    assert ii._parse_issue({"number": "12", "title": "x", "labels": []}).number == 12, "control"
