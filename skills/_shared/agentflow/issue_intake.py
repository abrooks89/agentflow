"""Shared issue-intake helper — one filter for dev-manager, lead-dev and steward.

The tracker is reached only through the configured ``git_host.cli``;
``work_source.kind`` decides whether there is a tracker at all. The no-op rules
are deliberate and documented, not defensive silence:

    kind "github-issues" AND cli "gh"                                    -> shell out
    any other kind in {"github-issues", "gitea"} x cli in {"gh", "glab"} -> refuse loudly
    anything else (kind none/filesystem, cli none)                       -> [] / no call

**The argv shapes below are gh's against GitHub Issues: ``gh`` is the only
VERIFIED cli and ``github-issues`` the only VERIFIED kind.** A tracker reached
through another CLI (``glab``; ``tea`` is rejected earlier, at config load)
refuses loudly before spawning rather than sending gh flags at a different
surface, and so does ``work_source.kind: gitea`` whatever the cli is: ``gh``
pointed at a Gitea repo ref reads and labels GitHub.
``gitea`` stays declared-but-unimplemented until someone adds a per-CLI argv
builder (issue #9). See ``_require_verified_tracker``.

Four labels drive the fleet (exact, case-sensitive matches):

    untriaged          — NOT ``triaged`` and NOT cross-linked from a refine spec
                         whose frontmatter ``source:`` names ``gh#N``
                         (dev-manager §3.4 reads these)
    builder_selectable — ``triaged`` AND NOT ``epic`` AND NOT ``in-progress``
                         AND NOT ``needs-decision`` (lead-dev section 2 steps
                         3.5 and 4 build only these)
    label_claimed      — ``triaged`` AND NOT ``epic`` AND ``in-progress``:
                         held work, kept visible so a claimed bug never falls
                         out of every view at once
    awaiting_decision  — ``triaged`` AND NOT ``epic`` AND ``needs-decision``
                         AND NOT ``in-progress``: waiting on a human's product
                         call, not on a builder (gh#446)

The last three partition bug work (``triaged`` AND NOT ``epic``) exactly: every
such issue is in one of builder_selectable, label_claimed or awaiting_decision,
and in only one. Claimed wins over awaiting, because held work is live work.

NOTHING IN THIS REPOSITORY WRITES ``in-progress`` ON ITS OWN YET. The ``claim``
and ``release`` subcommands below are the mechanism, but no loop's ``SKILL.md``
is instructed to run either, so on today's tree the exclusion is inert and
``builder_selectable`` returns exactly what it returned before. Deciding WHICH
lane runs ``claim`` — the builder at branch creation, or the foreman when it
hands the issue out — is gh#56's follow-up and is a prose change, not this one.

Every process boundary is loud: a non-zero exit, a missing binary or malformed
JSON raises ``IssueIntakeError``. Tests inject ``runner``; the default shells
out via ``subprocess``.

CLI: ``python -m agentflow.issue_intake untriaged|selectable|awaiting|mirror|fix-claims --project <name> [--json]``
     ``python -m agentflow.issue_intake claim|release --project <name> --issue <N> [--json]``
     ``python -m agentflow.issue_intake close --project <name> --actor <seat> --issue <N> --comment-file <path> [--dry-run]``
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import yaml

from agentflow.stream_report import describe_streams

TRIAGED_LABEL = "triaged"
EPIC_LABEL = "epic"
BUG_LABEL = "bug"

#: A lane holds a claim on this issue. A tracker LABEL, and not the ``in-progress`` that is a
#: use-case spine status or a registry epic status — different namespaces that cannot collide
#: mechanically, but a reader will conflate them. This one asserts only "somebody is building
#: this issue right now", which is why it gates selection rather than describing progress.
IN_PROGRESS_LABEL = "in-progress"

#: The issue body leaves a product choice open, so a call must precede any build. Applied by
#: whoever files or triages the issue, removed at disposition once the call is made (gh#446).
NEEDS_DECISION_LABEL = "needs-decision"

_TRACKER_KINDS = {"github-issues", "gitea"}
_TRACKER_CLIS = {"gh", "glab"}
_LIST_FIELDS = "number,title,labels,body,url"
_CLI_TIMEOUT = 60  # seconds; a wedged tracker CLI must never hang a wake

#: How many open issues one read may answer for. Readers that must be complete ask the tracker
#: for ONE MORE row than this and refuse when it comes back (gh#714): exactly this many open
#: issues is a complete read, and only the extra row proves some are past the page. Raise it
#: when the tracker legitimately outgrows it. `fleet_report`'s own `issue_limit` is a separate
#: knob: that reader reports saturation beside its counts rather than refusing.
OPEN_ISSUE_PAGE = 200

_SOURCE_REF_RE = re.compile(r"gh#(\d{1,9})(?!\d)")  # bounded: int() refuses >4300-digit strings
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_SOURCE_KEY_RE = re.compile(r"^source:(.*)$")

# runner(argv) -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], tuple[int, str, str]]


class IssueIntakeError(Exception):
    """Raised on any tracker-CLI failure — non-zero exit, missing binary, bad JSON."""


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    labels: tuple[str, ...]
    body: str = ""
    url: str = ""


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def _subprocess_runner(argv: list) -> tuple:
    """Default runner: run ``argv`` and return ``(returncode, stdout, stderr)``."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_CLI_TIMEOUT, encoding="utf-8",
        )
    except FileNotFoundError as e:
        raise IssueIntakeError(f"tracker CLI not found: {argv[0]!r} ({e})") from e
    except subprocess.SubprocessError as e:
        raise IssueIntakeError(f"tracker CLI failed to run: {' '.join(argv)} ({e})") from e
    return proc.returncode, proc.stdout, proc.stderr


def _run(argv: list, runner: Optional[Runner]) -> str:
    """Run ``argv`` through ``runner`` (default subprocess); raise on non-zero exit."""
    active = runner if runner is not None else _subprocess_runner
    returncode, stdout, stderr = active(argv)
    if returncode != 0:
        # The second site of gh#478's assumption, fixed in the same pass as the first. `gh`
        # prints advisories (new-version notices, auth warnings) on stderr that also
        # accompany successful runs, while an API refusal can arrive on stdout -- so
        # `stderr or stdout` could report the advisory and drop the refusal. Both streams,
        # labelled and clamped per stream; see `agentflow.stream_report` for why no
        # precedence, including the mirror-image `stdout or stderr`, is correct.
        raise IssueIntakeError(
            f"`{' '.join(argv)}` exited {returncode}; {describe_streams(stdout, stderr)}"
        )
    return stdout


_VERIFIED_CLIS = ("gh",)
#: The tracker kinds that ``_VERIFIED_CLIS`` has an argv builder for. A kind missing here
#: refuses even when the cli is verified: the cli picks the command surface, the kind picks
#: which service the repo ref names, and gh can only ever reach GitHub.
_VERIFIED_KINDS = ("github-issues",)


def _require_verified_tracker(kind: str, cli: str) -> None:
    """The argv shapes below are gh's against GitHub Issues. Fail loudly rather than emit
    gh flags at another CLI (glab/tea take a different surface: `--output json`, no field
    list, `--label` on update) or aim gh at a tracker kind it cannot reach. Adding one is
    a per-CLI argv builder, not a rename — tracked as issue #9."""
    if kind not in _VERIFIED_KINDS or cli not in _VERIFIED_CLIS:
        raise IssueIntakeError(
            f"issue intake has no verified argv shape for work_source.kind '{kind}' via cli "
            f"'{cli}' (verified: {', '.join(_VERIFIED_KINDS)} via {', '.join(_VERIFIED_CLIS)}); "
            "add a per-CLI argv builder before enabling it"
        )


def _tracker_active(kind: str, cli: str) -> bool:
    """The documented no-op rule: only a real tracker kind with a real CLI shells out."""
    return kind in _TRACKER_KINDS and cli in _TRACKER_CLIS


# --------------------------------------------------------------------------- #
# Tracker reads / writes
# --------------------------------------------------------------------------- #
def _issue_number(value) -> int:
    """An issue number is a whole positive integer, nothing that merely coerces.

    ``bool`` is an ``int`` subclass (True would parse as issue #1) and a float
    would silently truncate to the WRONG issue — either would send a label to the
    wrong row. ``int(float("inf"))`` raises OverflowError, which is neither
    TypeError nor ValueError, so coercion cannot be the guard.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        else:
            raise IssueIntakeError(f"issue row has a non-integer 'number': {value!r}")
    if value <= 0:
        raise IssueIntakeError(f"issue row has a non-positive 'number': {value!r}")
    return value


def _parse_issue(raw: dict) -> Issue:
    if not isinstance(raw, dict) or "number" not in raw:
        raise IssueIntakeError(f"issue row missing 'number': {raw!r}")
    raw_labels = raw.get("labels") or []
    if isinstance(raw_labels, (str, bytes)) or not isinstance(raw_labels, (list, tuple)):
        # A bare string would iterate per character and silently invent labels.
        raise IssueIntakeError(
            f"issue #{raw['number']} has a malformed 'labels' value: {raw_labels!r} "
            "(expected a list)"
        )
    labels = []
    for label in raw_labels:
        # gh emits [{"name": ...}]; tolerate a bare string list from other CLIs.
        name = label.get("name") if isinstance(label, dict) else label
        if not isinstance(name, str):
            raise IssueIntakeError(f"issue #{raw['number']} has a malformed label: {label!r}")
        labels.append(name)
    number = _issue_number(raw["number"])
    return Issue(
        number=number,
        title=str(raw.get("title") or ""),
        labels=tuple(labels),
        body=str(raw.get("body") or ""),
        url=str(raw.get("url") or ""),
    )


def list_open_issues(
    host_ref: str,
    *,
    kind: str,
    cli: str,
    runner: Optional[Runner] = None,
    limit: int = OPEN_ISSUE_PAGE,
) -> list[Issue]:
    """Open issues for ``host_ref`` via ``<cli> issue list``; ``[]`` when there is no tracker."""
    if not _tracker_active(kind, cli):
        return []
    _require_verified_tracker(kind, cli)
    argv = [
        cli, "issue", "list", "--repo", host_ref, "--state", "open",
        "--limit", str(limit), "--json", _LIST_FIELDS,
    ]
    stdout = _run(argv, runner)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise IssueIntakeError(f"`{' '.join(argv)}` returned malformed JSON: {e}") from e
    if not isinstance(data, list):
        raise IssueIntakeError(
            f"`{' '.join(argv)}` returned {type(data).__name__}, expected a JSON list"
        )
    return [_parse_issue(row) for row in data]


def label_issue(
    number: int,
    labels: Iterable[str],
    host_ref: str,
    *,
    kind: str,
    cli: str,
    runner: Optional[Runner] = None,
) -> None:
    """``<cli> issue edit N --repo R --add-label a,b``; no-op when there is no tracker."""
    if not _tracker_active(kind, cli):
        return
    _require_verified_tracker(kind, cli)
    if isinstance(labels, (str, bytes)):
        # Iterating a str yields characters: "triaged" would become 7 labels.
        raise IssueIntakeError(
            f"label_issue(#{number}): labels must be a sequence of names, got {labels!r}"
        )
    names = [str(label) for label in labels]
    if any("," in name for name in names):
        # The argv joins on ',' — an embedded comma would split into two labels.
        raise IssueIntakeError(f"label_issue(#{number}): label names may not contain ',': {names!r}")
    if not names:
        raise IssueIntakeError(f"label_issue(#{number}): no labels given")
    argv = [cli, "issue", "edit", str(number), "--repo", host_ref, "--add-label", ",".join(names)]
    _run(argv, runner)


def unlabel_issue(
    number: int,
    labels: Iterable[str],
    host_ref: str,
    *,
    kind: str,
    cli: str,
    runner: Optional[Runner] = None,
) -> None:
    """``<cli> issue edit N --repo R --remove-label a,b``; no-op when there is no tracker.

    The exact mirror of ``label_issue``, guards included, because the two failure modes are
    the same ones: a bare string iterates per character, and an embedded comma splits one
    name into two. A label removed by accident is as wrong as one added by accident.
    """
    if not _tracker_active(kind, cli):
        return
    _require_verified_tracker(kind, cli)
    if isinstance(labels, (str, bytes)):
        # Iterating a str yields characters: "triaged" would become 7 labels.
        raise IssueIntakeError(
            f"unlabel_issue(#{number}): labels must be a sequence of names, got {labels!r}"
        )
    names = [str(label) for label in labels]
    if any("," in name for name in names):
        # The argv joins on ',' — an embedded comma would split into two labels.
        raise IssueIntakeError(
            f"unlabel_issue(#{number}): label names may not contain ',': {names!r}"
        )
    if not names:
        raise IssueIntakeError(f"unlabel_issue(#{number}): no labels given")
    argv = [cli, "issue", "edit", str(number), "--repo", host_ref, "--remove-label", ",".join(names)]
    _run(argv, runner)


def _find_open_issue(
    number: int,
    host_ref: str,
    *,
    kind: str,
    cli: str,
    runner: Optional[Runner] = None,
    limit: int = OPEN_ISSUE_PAGE,
) -> Issue:
    """The one open issue numbered ``number``, or a loud refusal naming WHY it was not found.

    Two misses that look identical from here are reported differently on purpose. A short page
    that does not contain the number proves the issue is not open. A read that found more than
    ``limit`` open issues proves nothing at all — the issue may be on the page nobody asked
    for — and answering "no such issue" from a truncated read is a confident wrong answer.
    ``fleet_report`` already refuses that conflation on the read path; a write path must not
    be laxer than the read it is based on.
    """
    issues = list_open_issues(host_ref, kind=kind, cli=cli, runner=runner, limit=limit + 1)
    for issue in issues:
        if issue.number == number:
            return issue
    _refuse_past_the_page(issues, limit, host_ref, f"issue #{number} is not among")
    raise IssueIntakeError(f"no open issue #{number} on {host_ref}")


def _refuse_past_the_page(issues: list, page: int, host_ref: str, lead: str) -> None:
    """Raise when ``issues`` holds more than ``page`` rows: some open issues are past the page.

    The caller asks the tracker for ``page + 1`` rows, so the extra row is PROOF of
    truncation, and exactly ``page`` open issues is a complete read that must not be refused.
    Refusing at ``len == limit`` instead cannot tell those two apart.

    The one guard for every reader of ``list_open_issues`` that cannot report a partial result
    beside its answer. gh returns newest-first, so the rows past the page are the OLDEST open
    issues, and a list mode that printed the page anyway would drop exactly the issues that
    have waited longest, with exit 0 (gh#714). ``fleet_report`` is deliberately not a caller:
    it renders the saturation next to the counts instead of refusing.
    """
    if len(issues) > page:
        raise IssueIntakeError(
            f"{lead} the first {page} open issues on {host_ref}; more are open than that, "
            "so the page came back full and 'absent' and 'past the page' are indistinguishable"
        )


def _require_tracker_for_claim(action: str, number: int, kind: str, cli: str) -> None:
    """Claiming with no tracker REFUSES; it does not quietly succeed.

    This is the one deliberate asymmetry against ``label_issue``, whose no-op is documented
    and correct: reading a tracker that does not exist yields ``[]``, which is honest. But a
    claim that silently did not happen leaves the caller believing it holds a claim that was
    never written anywhere, and the whole point of the marker is that the claim outlives the
    lane's own memory of taking it. A claim mechanism that can no-op is worse than none.
    """
    if not _tracker_active(kind, cli):
        raise IssueIntakeError(
            f"cannot {action} #{number}: no tracker configured "
            f"(work_source.kind={kind!r}, git_host.cli={cli!r}); a claim that is not written "
            "somewhere is not a claim"
        )


def claim_issue(
    number: int,
    host_ref: str,
    *,
    kind: str,
    cli: str,
    runner: Optional[Runner] = None,
    limit: int = OPEN_ISSUE_PAGE,
) -> tuple:
    """Mark issue ``number`` as being built. Returns ``(issue, wrote)``.

    Reads before writing so a second claim is a no-op rather than an error: lead-dev RESUMES
    an in-progress item at every wake, so re-claiming is the normal path and raising here
    would fire a false alarm on every wake until the lane learned to ignore it. ``wrote``
    keeps the two outcomes distinguishable without making one of them a failure.

    Refuses an issue a builder could not have been given: untriaged (nobody dispositioned it)
    or ``epic`` (a human promotion gate). Marking either strands it behind a label that
    ``builder_selectable`` reads and nothing else would ever clear.
    """
    _require_tracker_for_claim("claim", number, kind, cli)
    issue = _find_open_issue(number, host_ref, kind=kind, cli=cli, runner=runner, limit=limit)
    if TRIAGED_LABEL not in issue.labels:
        raise IssueIntakeError(
            f"refusing to claim #{number}: it is not `{TRIAGED_LABEL}`, so no builder may "
            "select it and a claim on it means nothing"
        )
    if EPIC_LABEL in issue.labels:
        raise IssueIntakeError(
            f"refusing to claim #{number}: it is labelled `{EPIC_LABEL}`, which is a human "
            "promotion decision, not build work"
        )
    if IN_PROGRESS_LABEL in issue.labels:
        return issue, False
    label_issue(number, [IN_PROGRESS_LABEL], host_ref, kind=kind, cli=cli, runner=runner)
    return issue, True


def release_issue(
    number: int,
    host_ref: str,
    *,
    kind: str,
    cli: str,
    runner: Optional[Runner] = None,
    limit: int = OPEN_ISSUE_PAGE,
) -> tuple:
    """Clear the claim on issue ``number``. Returns ``(issue, wrote)``.

    Deliberately NOT symmetric with ``claim_issue``'s triaged/epic guards. Release runs on
    merge and on abandonment, and by then the issue may have been relabelled ``epic`` or had
    ``triaged`` stripped — refusing on that basis would wedge the marker onto exactly the
    issues that most need it cleared.
    """
    _require_tracker_for_claim("release", number, kind, cli)
    issue = _find_open_issue(number, host_ref, kind=kind, cli=cli, runner=runner, limit=limit)
    if IN_PROGRESS_LABEL not in issue.labels:
        return issue, False
    unlabel_issue(number, [IN_PROGRESS_LABEL], host_ref, kind=kind, cli=cli, runner=runner)
    return issue, True


# --------------------------------------------------------------------------- #
# Spec cross-links: frontmatter `source:` naming gh#N
# --------------------------------------------------------------------------- #
def issue_source_ref(number: int) -> str:
    """The frontmatter token a carved spec carries for its originating issue."""
    return f"gh#{number}"


def _source_text_from_yaml(block: str) -> Optional[str]:
    """The stringified ``source:`` value, or None when the block is not parseable YAML."""
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or data.get("source") is None:
        return ""
    value = data["source"]
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value)
    return str(value)


def _source_text_from_lines(block: str) -> str:
    """Raw fallback: the ``source:`` line plus its indented continuation lines."""
    out: list = []
    capturing = False
    for line in block.splitlines():
        if capturing:
            if line and (line[0].isspace() or line.startswith("-")):
                out.append(line)
                continue
            break
        m = _SOURCE_KEY_RE.match(line)
        if m:
            out.append(m.group(1))
            capturing = True
    return "\n".join(out)


def spec_linked_issue_numbers(refine_dir) -> set[int]:
    """Issue numbers named as ``gh#N`` in the frontmatter ``source:`` of any ``*.md`` under ``refine_dir``."""
    base = Path(refine_dir)
    linked: set[int] = set()
    try:
        if not base.is_dir():
            return linked
        specs = sorted(base.rglob("*.md"))
    except OSError as e:
        # A junction/symlink loop walks until the OS gives up; an unreadable
        # parent makes is_dir() itself raise EACCES (pathlib does not swallow it).
        raise IssueIntakeError(f"could not walk {base}: {type(e).__name__}: {e}") from e
    for spec in specs:
        if not spec.is_file():
            continue
        try:
            text = spec.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise IssueIntakeError(f"could not read {spec}: {type(e).__name__}: {e}") from e
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue
        block = m.group(1)
        source = _source_text_from_yaml(block)
        if source is None:
            source = _source_text_from_lines(block)
        linked.update(int(n) for n in _SOURCE_REF_RE.findall(source))
    return linked


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
def untriaged(issues: Iterable[Issue], linked: set[int]) -> list[Issue]:
    """Issues no one has disposed: not ``triaged`` and not cross-linked from a spec."""
    return [i for i in issues if TRIAGED_LABEL not in i.labels and i.number not in linked]


def _triaged_bug_work(issues: Iterable[Issue]) -> list[Issue]:
    """Issues that are bug work at all: ``triaged`` and not ``epic``.

    Kept separate from ``builder_selectable`` because the two questions genuinely differ.
    "Is this bug work?" is what the derived views group BY; "may a builder pick it up NOW?"
    is that minus whatever is already being built. Collapsing them into one predicate makes a
    claimed bug fall out of the mirror AND out of the fleet report's in-progress column at
    exactly the moment both should name it — which reads identically to an idle fleet.
    """
    return [i for i in issues if TRIAGED_LABEL in i.labels and EPIC_LABEL not in i.labels]


def builder_selectable(issues: Iterable[Issue]) -> list[Issue]:
    """Issues a builder may pick up: ``triaged``, not ``epic``, not awaiting a decision, and
    not already claimed.

    Four exclusions, four different reasons:

        not ``triaged``      — nobody has dispositioned it yet (dev-manager §3.4)
        ``epic``             — promotion to an epic is a human gate, not build work
        ``needs-decision``   — a product call must come first - a human gate, not build
                               work, like epic (gh#446)
        ``in-progress``      — a lane already holds it (gh#56)

    The ``in-progress`` clause had no expression here at all until gh#56. On 2026-08-31 a lane was mid-build
    on an issue with a live worktree and ten modified files and this function still offered
    it; the only mitigation available was stripping ``triaged`` by hand, which overloads a
    triage-state label to mean availability and destroys the record that the issue was ever
    triaged. NOTE, and this is the honest limit of the change: no loop skill is instructed to
    run ``claim``/``release`` yet, so nothing writes the marker and this clause is inert on
    today's tree. Wiring it is gh#56's follow-up (see the module docstring).
    """
    return [i for i in _triaged_bug_work(issues)
            if IN_PROGRESS_LABEL not in i.labels and NEEDS_DECISION_LABEL not in i.labels]


def label_claimed(issues: Iterable[Issue]) -> list[Issue]:
    """Bug work carrying the claim marker — what ``builder_selectable`` drops as held.

    With ``builder_selectable`` and ``awaiting_decision`` it partitions
    ``_triaged_bug_work`` exactly: no gap, no overlap. The three exist as a set on purpose:
    every consumer that renders "what bug work is there" must be able to reassemble the whole
    set, or an excluded issue is not merely unselectable but invisible, which is the harder
    failure to notice and the harder one to recover from.
    """
    return [i for i in _triaged_bug_work(issues) if IN_PROGRESS_LABEL in i.labels]


def awaiting_decision(issues: Iterable[Issue]) -> list[Issue]:
    """Unclaimed bug work carrying ``needs-decision`` — what ``builder_selectable`` drops as
    waiting on a human's product call (gh#446).

    "Unclaimed" is ``label_claimed``'s own definition negated (no ``in-progress``), so the
    three views partition ``_triaged_bug_work`` exactly. A claimed decision-bearing issue is
    ``label_claimed``'s: a lane holds it, so it is live work before it is an open question.
    Without this view the label that keeps the issue off the build queue would also keep it
    off every page a human reads, and the call it is waiting on would never be asked for.
    """
    return [i for i in _triaged_bug_work(issues)
            if NEEDS_DECISION_LABEL in i.labels and IN_PROGRESS_LABEL not in i.labels]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _render_line(issue: Issue) -> str:
    return f"#{issue.number}  {issue.title}  [{', '.join(issue.labels)}]"


_CLAIM_MODES = ("claim", "release")
#: Modes that act on one issue and so require ``--issue``.
_ISSUE_MODES = (*_CLAIM_MODES, "close")

#: Printed on every claim/release. The mechanism exists; the instruction to USE it does not,
#: and an operator who runs this by hand deserves to be told which half they are holding.
#: DELETE THIS the moment a loop skill runs claim/release — a note that outlives its fact is
#: how the next reader learns to disbelieve the notes.
_UNWIRED_NOTE = (
    "note: no loop skill is instructed to run claim/release yet, so nothing but a hand "
    "invocation writes `in-progress` (gh#56 follow-up)"
)


def _run_claim(mode: str, number: int, host_ref: str, *, kind: str, cli: str, as_json: bool) -> int:
    """The claim/release CLI path. A run that wrote nothing SAYS it wrote nothing.

    Four outcomes, four distinct lines. Rendering "claimed" for a claim that was already held
    would make the idempotent path indistinguishable from the writing one, and the caller
    that most needs to tell them apart is a lane deciding whether it just took the issue or
    merely found it already taken.
    """
    fn = claim_issue if mode == "claim" else release_issue
    issue, wrote = fn(number, host_ref, kind=kind, cli=cli)
    if mode == "claim":
        verb, change = ("claimed", f"{IN_PROGRESS_LABEL} added") if wrote else \
            ("already claimed", "nothing written")
    else:
        verb, change = ("released", f"{IN_PROGRESS_LABEL} removed") if wrote else \
            ("not claimed", "nothing written")
    print(f"issue_intake: {_UNWIRED_NOTE}", file=sys.stderr)
    if as_json:
        print(json.dumps(
            {"issue": issue.number, "action": mode, "changed": wrote, "labels": list(issue.labels)},
            indent=2,
        ))
    else:
        print(f"{verb} #{issue.number}  {issue.title}  [{change}]")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m agentflow.issue_intake",
        description="List untriaged, builder-selectable or awaiting-decision open issues "
                    "for a project, or claim/release the in-progress marker on one.",
    )
    parser.add_argument("mode", choices=["untriaged", "selectable", "awaiting", "mirror",
                                         "fix-claims", *_ISSUE_MODES])
    parser.add_argument("--project", required=True, help="project name under the config home")
    parser.add_argument("--json", action="store_true", help="emit a JSON list instead of text lines")
    parser.add_argument("--issue", type=int, default=None,
                        help="issue number; required by claim/release/close, rejected by the list modes")
    parser.add_argument("--actor", choices=_agents(), default=None,
                        help="close only: the registry seat asking to close")
    parser.add_argument("--comment-file", default=None,
                        help="close only: the verdict comment to post before closing")
    parser.add_argument("--dry-run", action="store_true",
                        help="close only: run every check and write nothing")
    args = parser.parse_args(argv)

    # Both directions, deliberately. A --issue silently ignored on `selectable` is the same
    # lie in miniature as a claim that silently does not happen.
    if args.mode in _ISSUE_MODES and args.issue is None:
        parser.error(f"{args.mode} needs --issue <number>")
    if args.mode not in _ISSUE_MODES and args.issue is not None:
        parser.error(f"--issue is only meaningful for {' / '.join(_ISSUE_MODES)}, not {args.mode}")
    close_only = {"--actor": args.actor is not None,
                  "--comment-file": args.comment_file is not None,
                  "--dry-run": args.dry_run}
    if args.mode == "close":
        if args.actor is None or args.comment_file is None:
            parser.error("close needs --actor <seat> and --comment-file <path>")
        if args.json:
            parser.error("--json is not meaningful for close")
    else:
        for flag, given in close_only.items():
            if given:
                parser.error(f"{flag} is only meaningful for close, not {args.mode}")

    from . import config as _config

    try:
        env = _config.load_environment()
        proj = _config.load_project(args.project)
        project_dir = Path(env.data_root) / "projects" / args.project
        if args.mode == "fix-claims":
            return _run_fix_claims(env, proj, args.project, project_dir / "design" / "specs",
                                   project_dir / "status", as_json=args.json)
        if args.mode == "close":
            return _run_close(env, proj, args.project, project_dir / "design" / "specs",
                              project_dir / "status", actor=args.actor, number=args.issue,
                              comment_file=args.comment_file, dry_run=args.dry_run)
        if args.mode in _CLAIM_MODES:
            return _run_claim(
                args.mode, args.issue, proj.canonical_repo.host_ref,
                kind=proj.work_source.kind, cli=env.git_host.cli, as_json=args.json,
            )
        host_ref = proj.canonical_repo.host_ref
        issues = list_open_issues(host_ref, kind=proj.work_source.kind, cli=env.git_host.cli,
                                  limit=OPEN_ISSUE_PAGE + 1)
        # Every list mode answers "which open issues are X", and a truncated page answers it
        # wrongly for the oldest ones. Refuse rather than print a list that looks complete.
        _refuse_past_the_page(issues, OPEN_ISSUE_PAGE, host_ref,
                              f"refusing `{args.mode}`: it would see only")
        specs_dir = Path(env.data_root) / "projects" / args.project / "design" / "specs"
        if args.mode == "mirror":
            return _run_mirror(env, proj, args.project, issues, specs_dir)
        if args.mode == "untriaged":
            selected = untriaged(issues, spec_linked_issue_numbers(specs_dir))
        elif args.mode == "awaiting":
            selected = awaiting_decision(issues)
        else:
            selected = builder_selectable(issues)
    except (_config.ConfigError, IssueIntakeError) as e:
        print(f"issue_intake: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([asdict(i) for i in selected], indent=2))
    else:
        for issue in selected:
            print(_render_line(issue))
    return 0



# --------------------------------------------------------------------------- #
# The bug mirror - a DERIVED view in the design pillar (E-0212)
#
# Issues stay the system of record. These files are a rendering of the tracker,
# regenerated from it and never hand-edited, so they cannot become a second
# source of truth that drifts from the first.
# --------------------------------------------------------------------------- #

#: Marker line the write-if-changed comparison strips before diffing. A reader needs it
#: (fresh mirror vs stale one); the comparison must not see it, or every regeneration
#: rewrites the file and the churn defect is back.
GENERATED_AT_PREFIX = "<!-- generated-at: "

_DO_NOT_EDIT = (
    "> **Generated file - do not edit.** This is a rendering of the issue tracker, which is the\n"
    "> system of record. Edit the issue; the next regeneration overwrites anything written here."
)


@dataclass(frozen=True)
class ClosedIssue:
    """A closed issue plus the provenance of its closing."""

    number: int
    title: str
    labels: tuple
    closed_at: str
    closing_prs: tuple = ()      # (number, url) pairs, in ascending number order
    merge_shas: tuple = ()       # parallel to closing_prs; "" where the lookup failed

    @property
    def outcome(self) -> str:
        """What actually happened, distinguished because the two are genuinely different.

        An issue closed WITHOUT a PR was dispositioned - not-real, duplicate, obsolete, or
        answered - and reading that as "shipped" would overstate what the fleet delivered.
        """
        return "shipped" if self.closing_prs else "closed without a PR"


def _month_of(closed_at: str) -> str:
    """``2026-09-03T00:56:12Z`` -> ``2026-09``. Raises on anything it cannot read."""
    if not closed_at or len(closed_at) < 7 or closed_at[4] != "-":
        raise IssueIntakeError(f"unreadable closedAt: {closed_at!r}")
    return closed_at[:7]


def in_flight(issues: Iterable[Issue], claimed_refs: Iterable[str]) -> list:
    """Bug work a lane is currently building, by EITHER claim route.

    Two routes, and honouring both is what stops them competing:

      * a ``gh#N`` appearing in a claimed registry row — E-0212's route, and still the one
        with a liveness regime behind it: the registry is reaped, so this route cannot rot;
      * the ``in-progress`` marker on the issue itself — gh#56's route, and the only one
        ``builder_selectable`` can see, because that function is handed issues and nothing else.

    The original wording here — *derived from the registry rather than from a label, because a
    label is a second place to update and the copy is what goes stale* — was widened, not
    overturned, and the reason it had to be is worth keeping: the registry claim is the RECORD
    of who is building what; the marker is the GATE that stops a selector offering the issue.
    They are written at different moments by different mechanisms, so they are not two copies
    of one fact. Reading only one of them is what would make them disagree in the rendered
    view, because ``builder_selectable`` already excludes the marker either way.
    """
    refs = set()
    for raw in claimed_refs:
        refs.update(int(m) for m in _SOURCE_REF_RE.findall(raw or ""))
    return sorted(
        (i for i in _triaged_bug_work(issues)
         if i.number in refs or IN_PROGRESS_LABEL in i.labels),
        key=lambda i: i.number,
    )


def render_open_index(
    issues: Iterable[Issue],
    linked: set,
    generated_at: str,
    *,
    claimed_refs: Iterable[str] = (),
) -> str:
    """The open-bug index, grouped by the states a bug actually moves through."""
    issues = list(issues)
    flying = in_flight(issues, claimed_refs)
    flying_nums = {i.number for i in flying}
    groups = [
        ("In flight", "a lane is building it now", flying),
        ("Triaged - selectable",
         "`triaged`, not `epic`, not `needs-decision`, not claimed (no `in-progress` marker"
         " and no registry claim); lead-dev may claim it",
         [i for i in builder_selectable(issues) if i.number not in flying_nums]),
        # Registry-claimed rows are filtered here as in selectable: in_flight honours both
        # claim routes, awaiting_decision can only see the marker.
        ("Awaiting a decision",
         "`triaged` and `needs-decision`, not claimed; a human must make the product call"
         " and record it on the issue - not build work until then",
         [i for i in awaiting_decision(issues) if i.number not in flying_nums]),
        ("Untriaged", "waiting on dev-manager's interactive pass", untriaged(issues, linked)),
    ]

    out = ["# Open bugs", "", _DO_NOT_EDIT, "", f"{GENERATED_AT_PREFIX}{generated_at} -->", ""]
    for heading, gloss, rows in groups:
        out.append(f"## {heading} ({len(rows)})")
        out.append("")
        out.append(f"_{gloss}._")
        out.append("")
        if not rows:
            # Say it explicitly. An empty section that renders as nothing is
            # indistinguishable from a section that failed to render.
            out.append("None.")
        else:
            for i in sorted(rows, key=lambda x: x.number):
                labels = ", ".join(f"`{lbl}`" for lbl in sorted(i.labels)) or "-"
                out.append(f"- **#{i.number}** {i.title}  \n  {labels}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def render_closed_month(closed: Iterable[ClosedIssue], month: str, generated_at: str) -> str:
    """One month of closed issues, with the provenance that makes the closure checkable."""
    rows = sorted(closed, key=lambda c: c.number)
    out = [f"# Bugs closed {month}", "", _DO_NOT_EDIT, "",
           f"{GENERATED_AT_PREFIX}{generated_at} -->", ""]
    if not rows:
        out.append("None.")
        return "\n".join(out) + "\n"
    out.append(f"{len(rows)} closed.")
    out.append("")
    for c in rows:
        labels = ", ".join(f"`{lbl}`" for lbl in sorted(c.labels)) or "-"
        out.append(f"### #{c.number} {c.title}")
        out.append("")
        out.append(f"- labels: {labels}")
        out.append(f"- closed: {c.closed_at}")
        out.append(f"- outcome: {c.outcome}")
        if c.closing_prs:
            for (num, url), sha in zip(c.closing_prs, c.merge_shas or ("",) * len(c.closing_prs)):
                # A missing sha is stated, never silently omitted: the reader must be able
                # to tell "no merge commit recorded" from "this mirror did not look".
                shown = sha[:7] if sha else "sha unavailable"
                out.append(f"- closed by: PR #{num} ({shown}) {url}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _body_without_stamp(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith(GENERATED_AT_PREFIX))


def write_if_changed(path, body: str) -> bool:
    """Write ``body`` to ``path`` only if it differs ignoring the generated-at line.

    Returns True when it wrote. The stamp is excluded from the comparison so a
    regeneration against unchanged tracker state leaves the file byte-identical on disk -
    the regression test for the churn defect - while the stamp itself still ships, so a
    reader can date the mirror.
    """
    path = Path(path)
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing is not None and _body_without_stamp(existing) == _body_without_stamp(body):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return True


def _run_mirror(env, proj, project: str, issues, specs_dir) -> int:
    """Regenerate the derived bug files. Reports what it wrote AND what it left alone.

    Printing the untouched files matters as much as printing the written ones: a run that
    changed nothing is the expected case on most sweeps, and it has to be distinguishable
    from a run that did not happen.
    """
    from datetime import datetime, timezone

    bugs_dir = Path(env.data_root) / "projects" / project / "design" / "bugs"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    linked = spec_linked_issue_numbers(specs_dir)

    registry = Path(env.data_root) / "projects" / project / "status" / "registry.md"
    claimed, claim_note = _claimed_refs(registry)

    wrote, left = [], []
    index = bugs_dir / "index.md"
    body = render_open_index(issues, linked, stamp, claimed_refs=claimed)
    (wrote if write_if_changed(index, body) else left).append(index.name)

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        closed = list_closed_issues(
            proj.canonical_repo.host_ref, kind=proj.work_source.kind,
            cli=env.git_host.cli, month=month,
        )
    except IssueIntakeError as e:
        # Degrade: the open index is already written and useful. Refusing to write anything
        # because the closed lookup failed would make the whole mirror hostage to the
        # newest half of it.
        print(f"issue_intake: closed-issue lookup failed, open index still written: {e}",
              file=sys.stderr)
        closed = None

    if closed is not None:
        cm = bugs_dir / f"closed-{month}.md"
        # ONLY the current month. Earlier months are frozen - regenerating a closed month
        # can only ever rewrite history.
        (wrote if write_if_changed(cm, render_closed_month(closed, month, stamp)) else left).append(cm.name)

    if claim_note:
        print(f"issue_intake: {claim_note}", file=sys.stderr)
    print(f"bug mirror: wrote {wrote or 'nothing'}; unchanged {left or 'nothing'} ({stamp})")
    return 0


def _claimed_refs(registry_path) -> tuple:
    """The registry text a bug can be claimed BY - Agents rows and CLAIMED Queue rows only.

    Deliberately not the whole board. `in_flight` greps for `gh#N`, and a `gh#N` can appear on
    the registry in half a dozen places that assert nothing about who is building it: a routed
    Note, an answered Question, a resolved Blocker, a `done` queue row. Feeding it the raw file
    marks every one of those "in flight", and `fleet_report._attach_bug_work` - the OTHER
    rendering of this same fact - restricts to exactly these two surfaces. Two views of one
    fact disagreeing is the drift this whole mirror exists to prevent, so they read the same
    rows or the mirror is not worth having.

    Returns (refs, note). A registry that will not parse yields no refs and a NOTE saying so,
    never silence: with no claims the in-flight group renders empty, which is also what a
    genuinely idle fleet looks like, and those two must not be indistinguishable.
    """
    if not registry_path.exists():
        return [], f"no registry at {registry_path}; in-flight grouping skipped"
    try:
        from . import registry as _registry
        reg = _registry.parse_registry(registry_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - a half-written board must not kill the mirror
        return [], f"registry unreadable ({e}); in-flight grouping skipped"

    refs = [f"{r.work} {r.phase}" for r in reg.agents.values()]
    refs += [f"{r.id} {r.work_item} {r.notes}" for r in reg.queue.values()
             if r.claimed_by not in ("-", "")]
    return refs, ""


def list_closed_issues(
    host_ref: str,
    *,
    kind: str,
    cli: str,
    month: str,
    runner: Optional[Runner] = None,
    limit: int = 200,
) -> list:
    """Closed issues whose ``closedAt`` falls in ``month`` (``YYYY-MM``)."""
    if not _tracker_active(kind, cli):
        return []
    _require_verified_tracker(kind, cli)
    argv = [
        cli, "issue", "list", "--repo", host_ref, "--state", "closed",
        "--limit", str(limit),
        "--json", "number,title,labels,closedAt,closedByPullRequestsReferences",
    ]
    raw = _run(argv, runner)
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError as e:
        raise IssueIntakeError(f"malformed JSON from {cli}: {e}") from e

    out = []
    for r in rows:
        closed_at = r.get("closedAt") or ""
        if not closed_at.startswith(month):
            continue
        prs = tuple(sorted(
            (int(p["number"]), str(p.get("url", "")))
            for p in (r.get("closedByPullRequestsReferences") or [])
        ))
        out.append(ClosedIssue(
            number=_issue_number(r.get("number")),
            title=str(r.get("title", "")),
            labels=tuple(str(x.get("name", "")) for x in (r.get("labels") or [])),
            closed_at=closed_at,
            closing_prs=prs,
            merge_shas=("",) * len(prs),
        ))
    return sorted(out, key=lambda c: c.number)


# --------------------------------------------------------------------------- #
# Fix claims: which open issues a merged PR or a done spec says it fixed, who built the fix,
# and the one mediated path that may close such an issue.
#
# The seat that built a fix never verifies or closes it, and who raised the issue does not
# matter. Builders and the steward never close. The auditor closes after checking each ask,
# and dev-manager only on the human's word. `close` enforces that in code, not only in prose.
# --------------------------------------------------------------------------- #

#: How many merged PRs one read may answer for. Read with the same one-row-past-the-page
#: probe as OPEN_ISSUE_PAGE, so a read that stops at the limit is reported as capped rather
#: than passed off as complete.
MERGED_PR_LIMIT = 1000
_MERGED_FIELDS = "number,title,body,mergedAt,mergeCommit,headRefName"

#: The two seats `close` admits. Builders never close; the steward closes nothing.
CLOSING_SEATS = ("auditor", "dev-manager")

#: GitHub caps a comment at 65,536 characters; leave room so a post never truncates.
EVIDENCE_MAX_CHARS = 60_000

#: Refusal codes, in the order `close` checks them. The order is the contract.
REFUSAL_CODES = ("actor_may_not_close", "not_open", "fixer_is_actor", "fixer_unknown",
                 "evidence_shape")
_REFUSAL_EXIT = 2

#: Epic numbers, spelled as fleet_report's worktree matcher accepts them (``E-0890``,
#: ``e0890``). Defined here, not imported, because fleet_report imports this module.
_EPIC_NUM_RE = re.compile(r"(?i)\be-?(\d{3,})")
_ROW_EPIC_RE = re.compile(r"^E-(\d{3,})")
#: GitHub's nine closing keywords, and nothing broader: "closing #12" closes nothing.
_CLOSE_KW = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_LINE_SOURCE_RE = re.compile(r"^\W{0,4}source\W{0,4}:\W{0,2}(.*)$", re.I)
_LINE_EPIC_OR_SOURCE_RE = re.compile(r"^\W{0,4}(?:source|epic)\W{0,4}:\W{0,2}(.*)$", re.I)
_LINE_CLOSE_RE = re.compile(
    r"^\W{0,4}" + _CLOSE_KW + r"\W{0,2}\s*:?[\s*_]*"
    r"((?:gh)?#\d+(?:\s*(?:,|and)\s*(?:gh)?#\d+)*)",
    re.I,
)
_ANY_REF_RE = re.compile(r"(?:gh)?#(\d{1,6})\b", re.I)
_TITLE_REF_RE = re.compile(r"gh#(\d{1,6})\b", re.I)
_ATTRIBUTION_RE = re.compile(r"^Agentflow \[([^\]\s]+)\]")
_SPEC_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_CHECKED_RE = re.compile(r"^Checked against:(.*)$")
_VERDICT_RE = re.compile(r"^Verdict:\s*(fixed|remainder|routed)\b")


def _agents() -> tuple:
    """The registry's seat vocabulary, imported lazily as ``_claimed_refs`` does."""
    from . import registry as _registry
    return _registry.AGENTS


def _first_nonblank(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _epic_num(token: str) -> str:
    return token.lstrip("0") or "0"


@dataclass(frozen=True)
class DoneSpecLink:
    """A spec at ``current_state: done`` and the issues its frontmatter ``source:`` names."""

    slug: str
    date: str               # the file's YYYY-MM-DD prefix, "" when it has none
    issues: frozenset


@dataclass(frozen=True)
class Claimant:
    """One thing that claims an issue fixed: a merged PR or a done spec."""

    kind: str               # "pr" | "spec"
    pr: int = 0
    sha7: str = ""
    merged_at: str = ""
    branch: str = ""
    slug: str = ""
    date: str = ""

    @property
    def when(self) -> str:
        return self.merged_at if self.kind == "pr" else self.date

    @property
    def ref(self) -> str:
        """How a verdict comment's ``Checked against:`` line names this claimant."""
        return f"#{self.pr}" if self.kind == "pr" else f"spec {self.slug}"

    def render(self) -> str:
        if self.kind == "pr":
            return f"#{self.pr} {self.sha7 or '-'} {self.merged_at or '-'}"
        return f"spec {self.slug}"


@dataclass(frozen=True)
class FixClaim:
    number: int
    title: str
    claimants: tuple        # Claimant, in the fixed order: PRs newest first, then specs

    @property
    def newest(self) -> Optional[Claimant]:
        return self.claimants[0] if self.claimants else None


def _pr_claimed_numbers(pr: dict) -> set:
    """Issue numbers a merged PR claims to fix: title ``gh#N``, a ``Source:`` line, or a line
    that STARTS with a closing keyword immediately followed by the reference(s)."""
    hit = {int(n) for n in _TITLE_REF_RE.findall(str(pr.get("title") or ""))}
    for line in str(pr.get("body") or "").splitlines():
        stripped = line.strip()
        if _LINE_SOURCE_RE.match(stripped):
            # The whole line, not the capture: the capture's leading \W{0,2} can eat the `#`
            # of a bare `Source: #N`, and the word "source" itself carries no reference.
            hit |= {int(n) for n in _ANY_REF_RE.findall(stripped)}
        m = _LINE_CLOSE_RE.match(stripped)
        if m:
            hit |= {int(n) for n in _ANY_REF_RE.findall(m.group(1))}
    return hit


def _pr_claimant(pr: dict) -> Claimant:
    commit = pr.get("mergeCommit") or {}
    oid = str(commit.get("oid") or "") if isinstance(commit, dict) else ""
    return Claimant(
        kind="pr", pr=_issue_number(pr.get("number")), sha7=oid[:7],
        merged_at=str(pr.get("mergedAt") or ""), branch=str(pr.get("headRefName") or ""),
    )


def fix_claims(issues: Iterable[Issue], merged_prs: Iterable[dict],
               done_spec_links: Iterable[DoneSpecLink]) -> list:
    """Open issues that a merged PR or a done spec claims fixed. Pure: reads no registry.

    A number not among ``issues`` is ignored, which is also how a cited PR number drops out.
    Claimants come PRs first, newest ``mergedAt`` first, then done specs, newest date prefix
    first and then by slug, so a claim's first claimant is stable run to run.
    """
    by_number = {i.number: i for i in issues}
    found: dict = {}
    for pr in merged_prs:
        nums = _pr_claimed_numbers(pr) & by_number.keys()
        if nums:
            c = _pr_claimant(pr)
            for n in nums:
                found.setdefault(n, []).append(c)
    for link in done_spec_links:
        for n in set(link.issues) & by_number.keys():
            found.setdefault(n, []).append(Claimant(kind="spec", slug=link.slug, date=link.date))

    out = []
    for n, cs in found.items():
        prs = sorted((c for c in cs if c.kind == "pr"), key=lambda c: (c.merged_at, c.pr),
                     reverse=True)
        specs = sorted((c for c in cs if c.kind == "spec"), key=lambda c: c.slug)
        specs.sort(key=lambda c: c.date, reverse=True)  # stable: slug order within a date
        out.append(FixClaim(number=n, title=by_number[n].title, claimants=tuple(prs + specs)))
    return sorted(out, key=lambda c: (c.newest.when, c.number))


def _frontmatter_value(block: str, key: str) -> str:
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict):
        value = data.get(key)
        return "" if value is None else str(value).strip()
    m = re.search(rf"^{re.escape(key)}:\s*([^#\n]*)", block, re.M)
    return m.group(1).strip() if m else ""


def done_spec_links(specs_dir) -> list:
    """Every spec under ``specs_dir`` at ``current_state: done`` whose ``source:`` names gh#N."""
    base = Path(specs_dir)
    try:
        if not base.is_dir():
            return []
        specs = sorted(base.rglob("*.md"))
    except OSError as e:
        raise IssueIntakeError(f"could not walk {base}: {type(e).__name__}: {e}") from e
    out = []
    for spec in specs:
        if not spec.is_file():
            continue
        try:
            text = spec.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise IssueIntakeError(f"could not read {spec}: {type(e).__name__}: {e}") from e
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue
        block = m.group(1)
        if _frontmatter_value(block, "current_state") != "done":
            continue
        source = _source_text_from_yaml(block)
        if source is None:
            source = _source_text_from_lines(block)
        nums = frozenset(int(n) for n in _SOURCE_REF_RE.findall(source))
        if not nums:
            continue
        dm = _SPEC_DATE_RE.match(spec.stem)
        slug = _frontmatter_value(block, "slug") or (spec.stem[dm.end():] if dm else spec.stem)
        out.append(DoneSpecLink(slug=slug, date=dm.group(1) if dm else "", issues=nums))
    return out


def _row_first_token(row) -> str:
    parts = str(row.work_item or "").split()
    return parts[0] if parts else ""


def _row_seat(row, agents: tuple) -> Optional[str]:
    seat = str(row.claimed_by or "").strip()
    return seat if seat in agents else None


def fixers(pr: dict, queue_rows: Iterable) -> frozenset:
    """Registry seats that built merged PR ``pr`` (the gh PR dict: ``body``, ``headRefName``).

    Two sources, unioned: the body's first non-blank line when it starts ``Agentflow [<seat>]``
    with ``<seat>`` in ``AGENTS``, and the ``claimed_by`` of every Queue row the PR matches, by
    epic number (branch, ``Source:`` or ``Epic:`` line) or by the row's first work-item token
    equalling the branch minus any leading ``<prefix>/``. The branch only finds a row; a seat
    named inside it is the work's subject, never its builder. Empty means unknown.
    """
    agents = _agents()
    seats = set()
    m = _ATTRIBUTION_RE.match(_first_nonblank(str(pr.get("body") or "")))
    if m and m.group(1) in agents:
        seats.add(m.group(1))

    branch = str(pr.get("headRefName") or "")
    epics = {_epic_num(n) for n in _EPIC_NUM_RE.findall(branch)}
    for line in str(pr.get("body") or "").splitlines():
        lm = _LINE_EPIC_OR_SOURCE_RE.match(line.strip())
        if lm:
            epics |= {_epic_num(n) for n in _EPIC_NUM_RE.findall(lm.group(1))}
    bare_branch = branch.rsplit("/", 1)[-1]

    for row in queue_rows:
        seat = _row_seat(row, agents)
        if seat is None:
            continue
        rm = _ROW_EPIC_RE.match(str(row.id or ""))
        if (rm and _epic_num(rm.group(1)) in epics) or (
                bare_branch and _row_first_token(row) == bare_branch):
            seats.add(seat)
    return frozenset(seats)


def spec_fixers(slug: str, queue_rows: Iterable) -> frozenset:
    """``claimed_by`` of every Queue row whose work item's first token is exactly ``slug``."""
    agents = _agents()
    return frozenset(
        seat for row in queue_rows
        if _row_first_token(row) == slug and (seat := _row_seat(row, agents)) is not None
    )


def read_queue_rows(status_dir) -> tuple:
    """``(rows, note)``: the live ``## Queue`` rows of ``registry.md`` plus every culled row
    in ``registry-log.md``. A file missing or unreadable adds no rows and the note names it,
    so an empty note means both were read."""
    from . import registry as _registry

    status = Path(status_dir)
    rows, notes = [], []
    live = status / "registry.md"
    try:
        text = live.read_text(encoding="utf-8")
    except FileNotFoundError:
        notes.append(f"no registry at {live}")
    except OSError as e:
        notes.append(f"registry.md unreadable ({type(e).__name__}: {e})")
    else:
        try:
            rows.extend(_registry.parse_registry(text).queue.values())
        except _registry.RegistryError as e:  # a damaged board is a note, not a crash
            notes.append(f"registry.md unparseable ({type(e).__name__}: {e})")

    log = status / "registry-log.md"
    try:
        log_text = log.read_text(encoding="utf-8")
    except FileNotFoundError:
        notes.append(f"no registry log at {log}")
    except OSError as e:
        notes.append(f"registry-log.md unreadable ({type(e).__name__}: {e})")
    else:
        for line in log_text.splitlines():
            cells = _registry._cells(line)
            if cells and len(cells) >= 7 and _registry._E_ID.match(cells[0]):
                rows.append(_registry.QueueRow(*cells[:7]))
    return rows, "; ".join(notes)


def claim_fixers(claim: Optional[FixClaim], prs_by_number: dict, queue_rows: list,
                 rows_complete: bool) -> tuple:
    """``(known, unknown)`` for a claim: the union of its claimants' builders, and whether
    the builder could be any seat (no claimant, a claimant that resolves to nobody, or a
    registry that was not fully read). ``known`` is kept even when unknown, because a seat
    the record DOES name is refused as the fixer whatever else is missing."""
    if claim is None or not claim.claimants:
        return frozenset(), True
    known, unknown = set(), not rows_complete
    for c in claim.claimants:
        if c.kind == "pr":
            seats = fixers(prs_by_number.get(c.pr, {}), queue_rows)
        else:
            seats = spec_fixers(c.slug, queue_rows)
        if not seats:
            unknown = True
        known |= seats
    return frozenset(known), unknown


def _render_fixers(known: frozenset, unknown: bool) -> str:
    """``?`` whenever the builder could be any seat: a partial list would read as complete.
    The seats the record does name stay in the JSON ``fixed_by`` field."""
    return "?" if unknown else ", ".join(sorted(known))


def list_merged_prs(
    host_ref: str,
    *,
    kind: str,
    cli: str,
    runner: Optional[Runner] = None,
    limit: int = MERGED_PR_LIMIT,
) -> list:
    """Merged PRs as gh PR dicts via ``<cli> pr list --state merged``; ``[]`` with no tracker."""
    if not _tracker_active(kind, cli):
        return []
    _require_verified_tracker(kind, cli)
    argv = [
        cli, "pr", "list", "--repo", host_ref, "--state", "merged",
        "--limit", str(limit), "--json", _MERGED_FIELDS,
    ]
    stdout = _run(argv, runner)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise IssueIntakeError(f"`{' '.join(argv)}` returned malformed JSON: {e}") from e
    if not isinstance(data, list):
        raise IssueIntakeError(
            f"`{' '.join(argv)}` returned {type(data).__name__}, expected a JSON list"
        )
    for row in data:
        if not isinstance(row, dict) or "number" not in row:
            raise IssueIntakeError(f"merged PR row missing 'number': {row!r}")
        _issue_number(row["number"])
    return data


def _view_json(argv: list, runner: Optional[Runner]):
    stdout = _run(argv, runner)
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise IssueIntakeError(f"`{' '.join(argv)}` returned malformed JSON: {e}") from e


def _authenticated_login(cli: str, runner: Optional[Runner]) -> str:
    data = _view_json([cli, "api", "user"], runner)
    login = data.get("login") if isinstance(data, dict) else None
    if not isinstance(login, str) or not login:
        raise IssueIntakeError(f"`{cli} api user` returned no login: {data!r}")
    return login


def _issue_comments(number: int, host_ref: str, cli: str, runner: Optional[Runner]) -> list:
    argv = [cli, "issue", "view", str(number), "--repo", host_ref, "--json", "comments"]
    data = _view_json(argv, runner)
    comments = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(comments, list):
        raise IssueIntakeError(f"`{' '.join(argv)}` returned no comments list: {data!r}")
    return comments


def _comment_author(comment) -> str:
    author = comment.get("author") if isinstance(comment, dict) else None
    return str(author.get("login") or "") if isinstance(author, dict) else ""


def _names_claimant(line: str, claimant: Claimant) -> bool:
    if claimant.kind == "pr":
        return re.search(rf"#{claimant.pr}(?!\d)", line) is not None
    return re.search(rf"\bspec {re.escape(claimant.slug)}(?![\w-])", line) is not None


def claim_state(claim: FixClaim, comments: list, login: str) -> tuple:
    """``(state, failed_closes)`` from a closing seat's verdict comments on the issue.

    Only a comment by the authenticated account whose first non-blank line is
    ``Agentflow [auditor]`` or ``Agentflow [dev-manager]`` counts, and only when its
    ``Checked against:`` line names the claim's newest claimant: a later merge that names the
    issue makes it unchecked again. ``remainder`` / ``routed`` mark it checked; each ``fixed``
    on a still-open issue is a close that did not land.
    """
    newest = claim.newest
    checked, failed = False, 0
    heads = {f"Agentflow [{seat}]" for seat in CLOSING_SEATS}
    for comment in comments:
        if not login or _comment_author(comment) != login:
            continue
        body = str(comment.get("body") or "")
        if _first_nonblank(body) not in heads:
            continue
        lines = [ln.strip() for ln in body.splitlines()]
        if newest is None or not any(
                (m := _CHECKED_RE.match(ln)) and _names_claimant(m.group(1), newest)
                for ln in lines):
            continue
        verdicts = {m.group(1) for ln in lines if (m := _VERDICT_RE.match(ln))}
        if verdicts & {"remainder", "routed"}:
            checked = True
        if "fixed" in verdicts:
            failed += 1
    return ("checked" if checked else "unchecked"), failed


def _no_tracker_line(kind: str, cli: str) -> str:
    return (f"fix-claims: no issue tracker for this project "
            f"(work_source.kind={kind!r}, git_host.cli={cli!r})")


def _probe_page(rows: list, page: int) -> tuple:
    """``(rows, capped)`` from a read that asked for ``page + 1`` rows: the extra row is the
    proof some are past the page (the OPEN_ISSUE_PAGE pattern), and it is dropped."""
    return (rows[:page], True) if len(rows) > page else (rows, False)


def _run_fix_claims(env, proj, project: str, specs_dir, status_dir, *, as_json: bool,
                    runner: Optional[Runner] = None) -> int:
    kind, cli = proj.work_source.kind, env.git_host.cli
    host_ref = proj.canonical_repo.host_ref
    if not _tracker_active(kind, cli):
        print(_no_tracker_line(kind, cli))
        return 0
    issues, open_capped = _probe_page(
        list_open_issues(host_ref, kind=kind, cli=cli, runner=runner,
                         limit=OPEN_ISSUE_PAGE + 1), OPEN_ISSUE_PAGE)
    prs, prs_capped = _probe_page(
        list_merged_prs(host_ref, kind=kind, cli=cli, runner=runner,
                        limit=MERGED_PR_LIMIT + 1), MERGED_PR_LIMIT)
    claims = fix_claims(issues, prs, done_spec_links(specs_dir))
    rows, reg_note = read_queue_rows(status_dir)
    prs_by_number = {int(p["number"]): p for p in prs}

    login = _authenticated_login(cli, runner) if claims else ""
    records = []
    for claim in claims:
        known, unknown = claim_fixers(claim, prs_by_number, rows, not reg_note)
        state, failed = claim_state(claim, _issue_comments(claim.number, host_ref, cli, runner),
                                    login)
        records.append({
            "issue": claim.number, "title": claim.title,
            "claimants": [asdict(c) for c in claim.claimants],
            "fixed_by": sorted(known), "fixers_unknown": unknown,
            "state": state, "failed_closes": failed,
        })

    unchecked = sum(1 for r in records if r["state"] == "unchecked")
    summary = (
        f"fix-claims: {len(records)} open issue(s) claimed fixed, {unchecked} unchecked; "
        f"{len(issues)} open issue(s) read"
        + (f" (capped at {OPEN_ISSUE_PAGE})" if open_capped else "")
        + f", {len(prs)} merged PR(s) read"
        + (f" (capped at {MERGED_PR_LIMIT})" if prs_capped else "")
        + (f"; registry: unread ({reg_note})" if reg_note else "")
    )
    if as_json:
        print(json.dumps({
            "claims": records,
            "summary": {
                "claimed": len(records), "unchecked": unchecked,
                "open_read": len(issues), "open_capped": open_capped,
                "open_limit": OPEN_ISSUE_PAGE,
                "merged_read": len(prs), "merged_capped": prs_capped,
                "merged_limit": MERGED_PR_LIMIT,
                "registry_note": reg_note, "line": summary,
            },
        }, indent=2))
        return 0
    for claim, r in zip(claims, records):
        line = (f"#{claim.number}  {claim.title}  claimed-by: "
                + " | ".join(c.render() for c in claim.claimants)
                + f"  fixed-by: {_render_fixers(frozenset(r['fixed_by']), r['fixers_unknown'])}"
                + f"  state: {r['state']}")
        if r["failed_closes"]:
            line += f"  failed-closes: {r['failed_closes']}"
        print(line)
    print(summary)
    return 0


def _refuse(code: str, number: int, title: str, fixed_by: str, why: str) -> int:
    shown = f"#{number}  {title}" if title else f"#{number}"
    print(f"{code}: {shown}  fixed-by: {fixed_by}  ({why})", file=sys.stderr)
    return _REFUSAL_EXIT


def _evidence_break(text: str, actor: str, newest: Optional[Claimant]) -> Optional[str]:
    """Why the comment breaks the verdict-comment shape, or None when it holds.

    When the issue has a claim, ``Checked against:`` must name its newest claimant: the
    listing counts a posted-but-unclosed ``fixed`` comment toward ``failed_closes`` only then,
    so a comment naming anything else would slip past the retry budget."""
    if len(text) > EVIDENCE_MAX_CHARS:
        return f"comment is {len(text)} characters, over {EVIDENCE_MAX_CHARS}"
    if _first_nonblank(text) != f"Agentflow [{actor}]":
        return f"first non-blank line is not exactly `Agentflow [{actor}]`"
    lines = [ln.strip() for ln in text.splitlines()]
    checked = [m.group(1).strip() for ln in lines if (m := _CHECKED_RE.match(ln))]
    if not checked:
        return "no `Checked against:` line"
    if not any(checked):
        return "`Checked against:` line is empty"
    if newest is not None and not any(_names_claimant(c, newest) for c in checked):
        return f"`Checked against:` does not name the newest claimant {newest.ref}"
    if not any(re.match(r"^Verdict: fixed\b", ln) for ln in lines):
        return "no `Verdict: fixed` line"
    return None


def _run_close(env, proj, project: str, specs_dir, status_dir, *, actor: str, number: int,
               comment_file: str, dry_run: bool, runner: Optional[Runner] = None) -> int:
    """The mediated close. Refuses, in REFUSAL_CODES order, before any write."""
    if actor not in CLOSING_SEATS:
        return _refuse("actor_may_not_close", number, "", "not read",
                       f"{actor} may not close an issue; only {' / '.join(CLOSING_SEATS)} may")

    kind, cli = proj.work_source.kind, env.git_host.cli
    host_ref = proj.canonical_repo.host_ref
    _require_tracker_for_claim("close", number, kind, cli)
    _require_verified_tracker(kind, cli)

    view_argv = [cli, "issue", "view", str(number), "--repo", host_ref,
                 "--json", "number,title,state,body,url"]
    raw = _view_json(view_argv, runner)
    if not isinstance(raw, dict):
        raise IssueIntakeError(f"`{' '.join(view_argv)}` returned {type(raw).__name__}")
    issue = _parse_issue({**raw, "labels": []})
    # `issue view` answers for a pull request number too, and `issue close` would close it.
    if "/pull/" in str(raw.get("url") or "") or issue.number != number:
        return _refuse("not_open", number, issue.title, "not read",
                       f"#{number} is a pull request, not an issue ({raw.get('url')!r})")
    if str(raw.get("state") or "").upper() != "OPEN":
        return _refuse("not_open", number, issue.title, "not read",
                       f"issue state is {raw.get('state')!r}")

    # The same reads and the same unknown rule as the listing, so the two never disagree.
    prs, _capped = _probe_page(
        list_merged_prs(host_ref, kind=kind, cli=cli, runner=runner,
                        limit=MERGED_PR_LIMIT + 1), MERGED_PR_LIMIT)
    claims = fix_claims([issue], prs, done_spec_links(specs_dir))
    rows, reg_note = read_queue_rows(status_dir)
    known, unknown = claim_fixers(claims[0] if claims else None,
                                  {int(p["number"]): p for p in prs}, rows, not reg_note)
    fixed_by = _render_fixers(known, unknown)

    if actor in known:
        return _refuse("fixer_is_actor", number, issue.title, fixed_by,
                       f"{actor} built the fix, and the seat that built a fix never closes it")
    if unknown and actor != "auditor":
        why = "no merged PR or done spec claims it" if not claims else (
            "the record does not name every builder"
            + (f"; registry: {reg_note}" if reg_note else ""))
        return _refuse("fixer_unknown", number, issue.title, fixed_by,
                       f"{why}, so {actor} cannot show it did not build the fix")

    path = Path(comment_file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise IssueIntakeError(f"could not read --comment-file {path}: {type(e).__name__}: {e}") from e
    broken = _evidence_break(text, actor, claims[0].newest if claims else None)
    if broken:
        return _refuse("evidence_shape", number, issue.title, fixed_by, broken)

    if dry_run:
        print(f"would close #{number}  {issue.title}")
        return 0
    # Post the bytes that were checked, not whatever the caller's file holds by now.
    fd, checked_copy = tempfile.mkstemp(prefix="issue-intake-close-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        _run([cli, "issue", "comment", str(number), "--repo", host_ref,
              "--body-file", checked_copy], runner)
    finally:
        os.unlink(checked_copy)
    try:
        _run([cli, "issue", "close", str(number), "--repo", host_ref, "--reason", "completed"],
             runner)
    except IssueIntakeError as e:
        print(f"issue_intake: posted the verdict comment on #{number} but the close failed: {e}",
              file=sys.stderr)
        return 1
    print(f"closed #{number}  {issue.title}")
    return 0


if __name__ == "__main__":  # pragma: no cover — thin CLI shim
    raise SystemExit(main())
