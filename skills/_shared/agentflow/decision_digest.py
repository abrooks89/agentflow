"""The "Waiting on you" queue: what is waiting on the human, grouped by the decision it needs.

The steward's ``wiki/digest.md`` is news: what happened this sweep. This module builds the other
half, the queue, from the five places a decision can be waiting:

* registry Questions that are ``open`` and ``halted`` or ``waiting`` (``advisory`` ones already
  proceeded on their assumption);
* drafted specs that pass the promotion preconditions (a seam-checkable ``## Files`` section,
  ``plan_parser.files_section``, and a ``queue_class`` that is unset or dispatchable, the same
  allow-list ``epic_survey.dispatchable`` applies);
* untriaged issues and ``needs-decision`` bugs, through ``issue_intake``;
* closure debt: open issues a merged PR or a done spec claims fixed (``issue_intake.fix_claims``).
  Closing those is the auditor's verify-then-close pass, so the batch routes them there and never
  offers a close;
* PRs parked under ``merge: manual``, from ``status/autonomy-prs.json`` when that ledger exists.

GROUPED BY THE DECISION, NOT BY THE SOURCE. An item that cites a ``Q-`` or ``B-`` id joins that
id's group, so two PRs parked on one question are one decision, not two. An item citing several
ids joins the first one that is an open question in this build, else its first citation. Only
items that are held on an id cite one: a parked PR through its ledger record, a ``needs-decision``
bug through its title. An untriaged issue never does, because answering a question does not
triage it. Uncited items of a
batchable kind (specs, untriaged issues, closure debt, parked PRs) collapse into one batch per
kind; a question or a ``needs-decision`` bug is its own decision.

EVERY SOURCE IS A ``Reading`` (``skills/_shared/EVIDENCE.md``). A source that could not be read is
printed as unavailable and a partial one as a floor, never as zero; the header says "at least"
whenever any source was incomplete. Each source is an injectable reader, so tests drive fixtures
and nothing here needs a live tracker.

CLI::

    python -m agentflow.decision_digest render --project <name> [--write] [--wiki-root PATH]

prints the section between ``<!-- waiting-on-you:start -->`` and ``<!-- waiting-on-you:end -->``.
``--write`` also replaces that region at the top of the project's ``wiki/digest.md`` through
``wiki.write_waiting_region``, leaving the steward's narrative below it byte-identical.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

import yaml

from agentflow import issue_intake as _intake
from agentflow.evidence import Reading, State
from agentflow.wiki import WAITING_END as END_MARKER
from agentflow.wiki import WAITING_START as START_MARKER

__all__ = [
    "DigestSection",
    "END_MARKER",
    "Group",
    "Item",
    "PR_LEDGER_FILENAME",
    "SOURCE_LABELS",
    "SOURCE_ORDER",
    "START_MARKER",
    "Source",
    "build",
    "default_readers",
    "group_items",
    "main",
]

#: The sources, in the order they are read and listed.
SOURCE_ORDER = ("questions", "specs", "untriaged", "needs-decision", "closure-debt", "parked-prs")
SOURCE_LABELS = {
    "questions": "registry questions",
    "specs": "drafted specs",
    "untriaged": "untriaged issues",
    "needs-decision": "needs-decision bugs",
    "closure-debt": "closure debt",
    "parked-prs": "parked PRs",
}
#: The PR ledger the autonomy wiring writes; absent until that lands.
PR_LEDGER_FILENAME = "autonomy-prs.json"

#: Kinds whose uncited items collapse into one batch decision, with the batch's wording.
_BATCH = {
    "spec": "Promote {n} drafted spec(s) that pass the promotion preconditions",
    "untriaged": "Triage {n} untriaged issue(s)",
    "closure-debt": (
        "Route {n} open issue(s) a merged fix claims to the auditor's verify-then-close pass"
        " - never close them from here"
    ),
    "parked-pr": "Merge {n} parked PR(s) - only through the autonomy grant path",
}
_DECISION_ID_RE = re.compile(r"\b[QB]-[0-9]{3,}\b")
_MAX_TEXT = 160

Reader = Callable[[], Reading]


@dataclass(frozen=True)
class Item:
    """One thing waiting on the human. ``cites`` are the ``Q-``/``B-`` ids it is held on."""

    kind: str
    ref: str
    title: str
    link: str = ""
    detail: str = ""
    cites: tuple = ()


@dataclass(frozen=True)
class Group:
    """One decision: its key, the decision in words, and every item it would settle."""

    key: str
    decision: str
    items: tuple


@dataclass(frozen=True)
class Source:
    name: str
    reading: Reading


@dataclass(frozen=True)
class DigestSection:
    project: str
    groups: tuple
    sources: tuple = field(default=())

    def render(self) -> str:
        return render(self)


# --------------------------------------------------------------------------- #
# Grouping (PURE)
# --------------------------------------------------------------------------- #
def _group_key(item: Item, open_questions: frozenset = frozenset()) -> str:
    """The decision an item needs: a cited Q-/B- id first, then its kind's batch, else itself.

    Of several citations, one naming an open question read in this build wins over the record's
    first, so an unrelated id earlier in a record cannot pull the item away from its question.
    """
    if item.cites:
        return next((c for c in item.cites if c in open_questions), item.cites[0])
    if item.kind in _BATCH:
        return f"batch:{item.kind}"
    return item.ref


def group_items(items: Iterable[Item]) -> list[Group]:
    """Collapse *items* into decisions, in order of each decision's first item."""
    items = list(items)
    open_questions = frozenset(i.ref for i in items if i.kind == "question")
    order: list[str] = []
    by_key: dict[str, list[Item]] = {}
    for item in items:
        key = _group_key(item, open_questions)
        if key not in by_key:
            order.append(key)
            by_key[key] = []
        by_key[key].append(item)
    return [Group(key=k, decision=_decision(k, by_key[k]), items=tuple(by_key[k])) for k in order]


def _decision(key: str, items: Sequence[Item]) -> str:
    if key.startswith("batch:"):
        return _BATCH[key[len("batch:") :]].format(n=len(items))
    question = next((i for i in items if i.kind == "question" and i.ref == key), None)
    if question is not None:
        return f"Answer {key}: {_clean(question.title)}"
    if _DECISION_ID_RE.fullmatch(key):
        return f"Resolve {key} (not among the open questions read; ground it on the board)"
    first = items[0]
    return f"Decide {key}: {_clean(first.title)}"


# --------------------------------------------------------------------------- #
# Build + render
# --------------------------------------------------------------------------- #
def build(project: str, *, readers: Optional[Mapping[str, Reader]] = None) -> DigestSection:
    """Read every source and group what is waiting. *readers* defaults to the live readers."""
    active = default_readers(project) if readers is None else readers
    sources = []
    items: list[Item] = []
    for name in SOURCE_ORDER:
        label = SOURCE_LABELS[name]
        reader = active.get(name)
        if reader is None:
            reading = Reading.unavailable("no reader configured", source=label)
        else:
            try:
                reading = reader()
            except Exception as exc:  # reported in the section, never swallowed
                reading = Reading.unavailable(f"{type(exc).__name__}: {exc}", source=label)
            if not isinstance(reading, Reading):
                raise TypeError(f"reader {name!r} returned {type(reading).__name__}, not Reading")
        sources.append(Source(name=name, reading=reading))
        if reading.value:
            items.extend(reading.value)
    return DigestSection(project=project, groups=tuple(group_items(items)), sources=tuple(sources))


def _clean(text: str) -> str:
    """One safe line: no control characters, no marker-shaped comment, bounded length."""
    flat = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text)).split())
    flat = flat.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    return flat if len(flat) <= _MAX_TEXT else flat[: _MAX_TEXT - 3] + "..."


def _source_line(source: Source) -> str:
    r = source.reading
    label = SOURCE_LABELS.get(source.name, source.name)
    n = len(r.value or ())
    if r.state is State.VALUE:
        body = f"{n} item(s)"
    elif r.state is State.EMPTY:
        body = "read in full, nothing waiting"
    elif r.state is State.UNAVAILABLE:
        body = f"UNAVAILABLE - {_clean(r.coverage)}"
    elif r.state is State.TRUNCATED:
        body = f"TRUNCATED - at least {n} item(s); {_clean(r.coverage)}"
    else:
        body = f"STALE as of {_clean(r.as_of or '')} - {n} item(s)"
    return f"- {label}: {body}"


def render(section: DigestSection) -> str:
    """The markdown region, markers included, ``\\n`` line endings, ending in a newline."""
    incomplete = [s for s in section.sources if not s.reading.is_complete]
    n_items = sum(len(g.items) for g in section.groups)
    lines = [START_MARKER, "## Waiting on you", ""]
    if section.groups:
        floor = "at least " if incomplete else ""
        lines.append(f"_{floor}{len(section.groups)} decision(s), {n_items} item(s)._")
    elif incomplete:
        lines.append("_Nothing found in the sources read, and that is not 'nothing waiting'._")
    else:
        lines.append("_Nothing is waiting on you._")
    if incomplete:
        lines.append(f"_{len(incomplete)} source(s) could not be read in full; see Sources below._")
    lines.append("")
    for idx, group in enumerate(section.groups, 1):
        count = f" - {len(group.items)} items" if len(group.items) > 1 else ""
        lines.append(f"{idx}. **{_clean(group.decision)}**{count}")
        for item in group.items:
            line = f"   - {_clean(item.ref)}: {_clean(item.title)}"
            if item.detail:
                line += f" ({_clean(item.detail)})"
            if item.link:
                link = _clean(item.link)
                # a bare file name in <...> would read as an HTML tag and vanish when rendered
                line += f" <{link}>" if "://" in link else f" (`{link.replace('`', '')}`)"
            lines.append(line)
    if section.groups:
        lines.append("")
    lines.append("**Sources**")
    lines.append("")
    lines.extend(_source_line(s) for s in section.sources)
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Source readers - each returns a Reading of Items
# --------------------------------------------------------------------------- #
def _derive(reading: Reading, fn, label: str) -> Reading:
    """Map a reading's value through *fn*, keeping its state. Incomplete stays incomplete."""
    if reading.state is State.UNAVAILABLE:
        return Reading.unavailable(reading.coverage, source=label, as_of=reading.as_of)
    out = fn(reading.value or [])
    if reading.state is State.TRUNCATED:
        return Reading.truncated(out, reading.coverage, source=label, as_of=reading.as_of)
    if reading.state is State.STALE:
        return Reading.stale(out, reading.as_of, source=label, coverage=reading.coverage)
    return Reading.ok(out, source=label) if out else Reading.empty([], source=label)


def read_questions(registry_path: Path) -> Reading:
    """Open ``halted``/``waiting`` Questions, triage-ordered. A missing board is unavailable.

    No registry field marks a question as delegated to the steward: the steward answers what
    it can and routes a human-only decline as a Note. So every open question still open here
    is listed; an answered one has already left ``open``.
    """
    from agentflow import registry as _registry

    label = SOURCE_LABELS["questions"]
    try:
        reg = _registry.read_registry_checked(Path(registry_path), tables=())
    except _registry.RegistryError as exc:
        return Reading.unavailable(f"registry not read: {exc}", source=label)
    items = []
    for q in _registry.order_open_questions(reg.questions):
        urgency = q.fields.get("urgency", "")
        if urgency == "advisory":
            continue
        detail = f"{urgency or 'no urgency'}, raised by {q.fields.get('raised_by', '-')}"
        default = q.fields.get("default", "")
        if default and default != "-":
            detail += f", default: {default}"
        items.append(
            Item(
                kind="question",
                ref=q.id,
                title=q.fields.get("question", ""),
                detail=detail,
                cites=(q.id,),
            )
        )
    return Reading.ok(items, source=label) if items else Reading.empty([], source=label)


def read_drafted_specs(specs_dir: Path) -> Reading:
    """Drafted specs at the bucket root that pass the promotion preconditions."""
    from agentflow.epic_survey import DISPATCHABLE_QUEUE_CLASSES
    from agentflow.plan_parser import files_section

    label = SOURCE_LABELS["specs"]
    base = Path(specs_dir)
    try:
        if not base.is_dir():
            return Reading.unavailable(f"no specs directory at {base.name}", source=label)
        paths = sorted(p for p in base.glob("*.md") if p.is_file())
    except OSError as exc:
        return Reading.unavailable(f"could not list {base.name}: {exc}", source=label)
    items, skipped = [], []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8-sig")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
            data = (yaml.safe_load(m.group(1)) if m else None) or {}
            if not isinstance(data, dict):
                raise ValueError("frontmatter is not a mapping")
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
            skipped.append(f"{path.name} ({type(exc).__name__})")
            continue
        state = str(data.get("current_state") or "").strip().lower()
        # Mirrors epic_survey: a non-string queue_class is unset, and unset is dispatchable.
        raw_qc = data.get("queue_class")
        queue_class = raw_qc if isinstance(raw_qc, str) else None
        if state != "drafted":
            continue
        if queue_class is not None and queue_class not in DISPATCHABLE_QUEUE_CLASSES:
            continue
        if not files_section(text).seam_checkable:
            continue
        slug = str(data.get("slug") or path.stem)
        items.append(
            Item(
                kind="spec",
                ref=f"spec {slug}",
                title=str(data.get("title") or slug),
                link=path.name,
                detail=f"priority {data.get('priority', '-')}",
            )
        )
    if skipped:
        return Reading.truncated(
            items, f"{len(skipped)} spec(s) unreadable: " + ", ".join(skipped), source=label
        )
    return Reading.ok(items, source=label) if items else Reading.empty([], source=label)


def read_open_issues(host_ref: str, *, kind: str, cli: str, page: int, runner=None) -> Reading:
    """The open-issue page, probed one past *page* so a full page reads as truncated."""
    label = "open issues"
    if not _intake._tracker_active(kind, cli):
        return Reading.unavailable(
            f"no issue tracker (work_source.kind={kind!r}, cli={cli!r})", source=label
        )
    try:
        issues = _intake.list_open_issues(
            host_ref, kind=kind, cli=cli, runner=runner, limit=page + 1
        )
    except _intake.IssueIntakeError as exc:
        return Reading.unavailable(f"tracker not read: {exc}", source=label)
    if len(issues) > page:
        return Reading.truncated(
            issues[:page], f"first {page} open issues; more are open", source=label
        )
    return Reading.ok(issues, source=label) if issues else Reading.empty([], source=label)


def _issue_item(kind: str, issue, *, cite_title: bool = False) -> Item:
    """An issue as an Item. Only a ``needs-decision`` bug cites the ids in its title: an
    untriaged issue that mentions a question still needs triage after the question is answered."""
    cites = tuple(dict.fromkeys(_DECISION_ID_RE.findall(issue.title))) if cite_title else ()
    return Item(kind=kind, ref=f"gh#{issue.number}", title=issue.title, link=issue.url, cites=cites)


def untriaged_items(issues: Reading, specs_dir: Path) -> Reading:
    label = SOURCE_LABELS["untriaged"]
    if issues.state is State.UNAVAILABLE:
        return _derive(issues, list, label)
    try:
        linked = _intake.spec_linked_issue_numbers(specs_dir)
    except _intake.IssueIntakeError as exc:
        return Reading.unavailable(f"spec cross-links not read: {exc}", source=label)
    return _derive(
        issues,
        lambda xs: [_issue_item("untriaged", i) for i in _intake.untriaged(xs, linked)],
        label,
    )


def needs_decision_items(issues: Reading) -> Reading:
    return _derive(
        issues,
        lambda xs: [
            _issue_item("needs-decision", i, cite_title=True) for i in _intake.awaiting_decision(xs)
        ],
        SOURCE_LABELS["needs-decision"],
    )


def closure_debt_items(
    issues: Reading, specs_dir: Path, *, host_ref: str, kind: str, cli: str, runner=None
) -> Reading:
    """Open issues a merged PR or a done spec claims fixed: shipped, still open.

    Every claim is listed, including one the auditor already judged ``remainder`` or ``routed``:
    telling those apart costs a comments read per claim, and the batch does not need it, because
    it routes the claims to the auditor's verify-then-close pass rather than offering a close.
    """
    label = SOURCE_LABELS["closure-debt"]
    if issues.state is State.UNAVAILABLE:
        return _derive(issues, list, label)
    try:
        prs = _intake.list_merged_prs(
            host_ref, kind=kind, cli=cli, runner=runner, limit=_intake.MERGED_PR_LIMIT + 1
        )
        links = _intake.done_spec_links(specs_dir)
    except _intake.IssueIntakeError as exc:
        return Reading.unavailable(f"merged PRs or done specs not read: {exc}", source=label)
    prs, prs_capped = _intake._probe_page(prs, _intake.MERGED_PR_LIMIT)
    claims = _intake.fix_claims(issues.value or [], prs, links)
    urls = {i.number: i.url for i in issues.value or []}
    items = [
        Item(
            kind="closure-debt",
            ref=f"gh#{c.number}",
            title=c.title,
            link=urls.get(c.number) or "",
            detail=f"claimed by {c.newest.render()}" if c.newest else "",
        )
        for c in claims
    ]
    if prs_capped:
        return Reading.truncated(
            items, f"merged PR read capped at {_intake.MERGED_PR_LIMIT}", source=label
        )
    return _derive(issues, lambda _xs: items, label)


def read_parked_prs(ledger_path: Path) -> Reading:
    """PRs the ledger records at merge level ``manual``. A missing ledger is ABSENT, not zero."""
    label = SOURCE_LABELS["parked-prs"]
    path = Path(ledger_path)
    if not path.is_file():
        return Reading.unavailable(
            f"absent: no status/{PR_LEDGER_FILENAME}; the PR ledger has not been written for "
            "this project",
            source=label,
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Reading.unavailable(f"{PR_LEDGER_FILENAME} not read: {exc}", source=label)
    if isinstance(data, dict) and isinstance(data.get("prs"), list):
        records = [(None, r) for r in data["prs"]]
    elif isinstance(data, list):
        records = [(None, r) for r in data]
    elif isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()):
        records = list(data.items())
    else:
        return Reading.unavailable(f"{PR_LEDGER_FILENAME} has an unrecognised shape", source=label)
    items, bad = [], 0
    for key, rec in records:
        if not isinstance(rec, dict):
            bad += 1
            continue
        pr = rec.get("pr", key)
        level = str(rec.get("merge") or rec.get("merge_level") or "").strip().lower()
        state = str(rec.get("state") or "open").strip().lower()
        if pr in (None, ""):
            bad += 1
            continue
        if level != "manual" or state != "open":
            continue
        cites = tuple(
            dict.fromkeys(
                m for v in rec.values() if isinstance(v, str) for m in _DECISION_ID_RE.findall(v)
            )
        )
        url = rec.get("url")
        items.append(
            Item(
                kind="parked-pr",
                ref=f"PR #{pr}",
                title=f"epic {rec.get('epic', '-')}",
                link=url if isinstance(url, str) else "",
                detail="merge: manual",
                cites=cites,
            )
        )
    if bad:
        return Reading.truncated(items, f"{bad} ledger record(s) unreadable", source=label)
    return Reading.ok(items, source=label) if items else Reading.empty([], source=label)


def default_readers(project: str) -> dict:
    """The live readers for *project*. Raises ``ValueError`` when the project does not resolve."""
    from agentflow import config
    from agentflow.registry import _PROJECT_RE

    if not _PROJECT_RE.match(project or ""):
        raise ValueError(f"project {project!r} must match [A-Za-z0-9_-]+")
    try:
        env = config.load_environment()
        proj = config.load_project(project)
    except (config.ConfigError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"--project {project}: config did not load: {exc}") from exc
    project_dir = Path(env.data_root) / "projects" / project
    status, specs = project_dir / "status", project_dir / "design" / "specs"
    kind, cli = proj.work_source.kind, env.git_host.cli
    host_ref, page = proj.canonical_repo.host_ref, proj.work_source.open_issue_page
    memo: dict = {}

    def issues() -> Reading:
        if "r" not in memo:
            memo["r"] = read_open_issues(host_ref, kind=kind, cli=cli, page=page)
        return memo["r"]

    return {
        "questions": lambda: read_questions(status / "registry.md"),
        "specs": lambda: read_drafted_specs(specs),
        "untriaged": lambda: untriaged_items(issues(), specs),
        "needs-decision": lambda: needs_decision_items(issues()),
        "closure-debt": lambda: closure_debt_items(
            issues(), specs, host_ref=host_ref, kind=kind, cli=cli
        ),
        "parked-prs": lambda: read_parked_prs(status / PR_LEDGER_FILENAME),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    """0 rendered (an unreadable source is rendered, not a failure); 2 refused; 3 filesystem."""
    from agentflow import wiki
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    p = argparse.ArgumentParser(
        prog="python -m agentflow.decision_digest",
        description="the Waiting-on-you section of the steward's digest",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("render", help="print the section; --write also puts it in digest.md")
    sp.add_argument("--project", required=True)
    sp.add_argument(
        "--write",
        action="store_true",
        help="replace the region at the top of the project's wiki/digest.md",
    )
    sp.add_argument("--wiki-root", dest="wiki_root", help="explicit wiki/ path for --write")
    a = p.parse_args(None if argv is None else list(argv))

    try:
        section = build(a.project, readers=default_readers(a.project)).render()
        print(section, end="")
        if not a.write:
            return 0
        root = wiki._resolve_wiki_root(a)
        changed = wiki.write_waiting_region(root, section)
    except (FileNotFoundError, ValueError) as exc:
        print(f"decision digest: refused - {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"decision digest: filesystem error - {exc!r}", file=sys.stderr)
        return 3
    print(f"decision digest: {'wrote' if changed else 'unchanged'} {root / wiki.DIGEST_FILENAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
