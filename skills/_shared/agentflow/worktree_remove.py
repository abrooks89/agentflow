"""Report whether one epic worktree is safe to remove, refusing whenever a live lane might own it
(gh#498).

REPORT-ONLY (Q-10093). This module measures and prints a verdict; it never deletes anything. Seven
review rounds each found another way an automated delete could take a live lane's tree, so removal
stays a manual one-line command a human runs. When every check passes, the report prints that
command; when any check refuses, it prints the refusal and no command.

A worktree's git state cannot tell "dead" from "a live lane that has not written yet". A lane
claims an epic, runs `worktree add`, and then READS for a while: until its first write the tree is
clean, has no commits of its own, and sits on a branch `--merged` lists, which is byte-identical
to a tree whose lane finished and left. Domain F's measurement matched exactly that twice in one
sweep, and four minutes after it said "remove" the tree held a staged new file that existed
nowhere else on the machine.

SO THE SIGNAL THAT DECIDES IS THE REGISTRY, NOT THE REPOSITORY. The registry knows which lane is
building which epic; git cannot. This helper answers in a fixed order:

1. The path must be a registered worktree DIRECTLY under `<worktree_root>/<project>/`. Anything
   else - the canonical checkout, a sibling project's tree, a typo - is refused before any other
   question, because the rest of the checks are only meaningful for a per-epic tree. The path
   must BE the registered tree, not a symlink or junction onto one: everything after this step
   works from git's registered path, so an alias cannot borrow a dead tree's name to remove a
   live one.
2. The epic is resolved from the registered directory name, then the branch. An unresolvable
   epic is REFUSED: without it the liveness check below cannot be asked, and "could not ask" must
   never read as "nobody owns it".
3. The registry is read FRESH, in this call, and is read as an ALLOWLIST: it must validate, carry
   exactly one `## Agents` and one `## Queue` table in the schema's shape, and every line anywhere
   in the file that names the epic must be recognised as safe - a terminal Queue row, an idle
   Agents row, or a Notes bullet. Anything else refuses. A dead lane whose row still says
   `building` is refused too, on purpose: reap it first, and the reap is what records that it
   was dead.
4. Only then is the tree re-measured, in the SAME call that reports: no worktree registered
   beneath it, no symlink or junction anywhere inside it, no `.git` below its root but its own,
   `status --porcelain --ignored --untracked-files=all` showing nothing but
   regenerable caches, and no commit of its own whose change has not landed on the base branch. A
   measurement taken by an earlier pass - a delegated subagent, a report run minutes ago - is
   never an input here, so a stale "clean" cannot read as "removable".

The command the report prints is `git worktree remove` WITHOUT `--force`, so git's own dirty-tree
refusal stays in place behind this one between the report and the human running it. It is NOT a
guard for ignored files - git deletes those without asking - which is why step 4 refuses them
itself. Nor is it printed for a tree git has locked, which git would refuse, or for a path its
double quotes cannot carry safely. The verdict is true only at the instant it was measured: a
human re-runs the report immediately before running the command.

Refusals carry a stable key and print as ``refused [<key>]: <rule>`` with the detail indented
beneath, the same shape `agentflow.registry` uses, so a lane greps one format across the suite.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import stat
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import registry as _registry
from .fleet_report import _WT_EPIC_NUM, _claims_worktree, _norm_wt_path

#: Every refusal this helper can raise, keyed the way `registry.RULES` is.
RULES = {
    "worktree_outside_root": (
        "the report judges only a per-epic tree directly under <worktree_root>/<project>/"
    ),
    "worktree_not_registered": (
        "the report judges only a tree git has registered for this checkout"
    ),
    "worktree_path_alias": (
        "the report judges the registered path itself, never a symlink or junction onto it"
    ),
    "worktree_epic_unresolved": (
        "the report needs the epic resolved from the path or branch, so liveness can be asked"
    ),
    "worktree_registry_unreadable": (
        "the report reads the registry fresh and whole; one that fails validation, strays "
        "from the schema's shape, or names the epic on a line not recognised as safe is refused"
    ),
    "worktree_lane_live": (
        "an ## Agents row that is not idle names this epic - a live lane owns this worktree"
    ),
    "worktree_queue_live": (
        "a ## Queue row on this epic is not terminal - a lane owns it or is about to"
    ),
    "worktree_unmeasurable": (
        "a git measurement of the worktree failed at report time, so it cannot be judged clean"
    ),
    "worktree_nested_worktree": (
        "another registered worktree lies inside this one, and removing it would delete that tree"
    ),
    "worktree_nested_repo": (
        "a .git entry lies below the tree root - a repository whose work this tree's status cannot see"
    ),
    "worktree_link_inside": (
        "a symlink or junction lies inside the tree, and removal can follow it and empty its target"
    ),
    "worktree_ignored_content": (
        "the tree holds ignored files that are not regenerable caches, and removal deletes them"
    ),
    "worktree_dirty_at_removal": (
        "status --porcelain is non-empty at report time - uncommitted or untracked work"
    ),
    "worktree_unpushed_at_removal": (
        "the tree has commits on no remote whose change has not landed on the base branch"
    ),
    "worktree_locked": (
        "git has the tree locked (`git worktree lock`), so the printed command could not remove it"
    ),
    "worktree_path_unquotable": (
        "a path in the command holds a character a double-quoted bash or PowerShell argument "
        "would end or expand, so no command is printed for it"
    ),
}

#: What ends or expands a double-quoted argument in bash or PowerShell: `"`, `$` and the
#: backtick in both, `!` (history expansion) in interactive bash, PowerShell's curly double
#: quotes, and any control character, which would split or corrupt the one-liner.
_UNQUOTABLE = frozenset('"$`!\u201c\u201d\u201e')

#: Ignored paths removal may delete: caches any run regenerates. Matched per path SEGMENT (or
#: suffix for `.pyc`), so `a/__pycache__/x.pyc` is a cache and `__pycache__-notes.md` is not.
#: Everything else ignored - `.claude/worktrees/`, `*.bak*`, a local config - is refused.
REGENERABLE_DIRS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules")
REGENERABLE_SUFFIXES = (".pyc",)

_EMPTY_CELLS = ("", "-")
_IDLE = "idle"


@dataclass(frozen=True)
class Refusal:
    key: str
    detail: str = ""

    @property
    def rule(self) -> str:
        return RULES[self.key]

    def __str__(self) -> str:
        msg = f"refused [{self.key}]: {self.rule}"
        return f"{msg}\n  {self.detail}" if self.detail else msg


@dataclass
class Verdict:
    worktree: Path
    branch: str = ""
    epic: str = ""
    #: git's registered path for the tree; what is measured and what the manual command names.
    registered: str = ""
    refusals: list[Refusal] = field(default_factory=list)

    @property
    def removable(self) -> bool:
        return not self.refusals


# A report must never take a live lane's index.lock: `git status` refreshes the index by
# default, which would race that lane's own `git add`.
# In a partial clone, a diff that needs a missing blob would lazily fetch it and write a promisor
# pack into the store the tree shares with the canonical checkout. With lazy fetch off the diff
# fails instead, and a failed diff reads as "not landed", which keeps the tree.
_READ_ONLY_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """git by EXPLICIT path: the fleet's launch directory is not a repository."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_READ_ONLY_ENV,
    )


def _git_bytes(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """`_git` with raw bytes out: a byte comparison must not pass through a locale codec."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False, env=_READ_ONLY_ENV
    )


def _err(proc: subprocess.CompletedProcess) -> str:
    err = proc.stderr or ""
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return f"exited {proc.returncode}: {err.strip()}"


def _worktree_list(
    checkout: Path,
) -> tuple[Optional[list[tuple[str, str, Optional[str]]]], str]:
    """``([(registered path, branch or "", lock reason or None)...], "")`` from git, or
    ``(None, why)``. A lock with no reason reads as ``""``, so only ``None`` means unlocked."""
    proc = _git(checkout, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return None, f"git worktree list {_err(proc)}"
    out: list[tuple[str, str, Optional[str]]] = []
    current: Optional[str] = None
    branch = ""
    locked: Optional[str] = None
    for line in proc.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            current, branch, locked = line[len("worktree ") :].strip(), "", None
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/") :].strip()
        elif line == "locked" or line.startswith("locked "):
            locked = line[len("locked") :].strip()
        elif not line.strip() and current is not None:
            out.append((current, branch, locked))
            current = None
    return out, ""


def _registered_branch(checkout: Path, worktree: Path) -> tuple[Optional[str], str, str]:
    """``(registered path, branch or "" for detached, "")`` if git registered *worktree*.

    ``(None, "", why)`` otherwise. The path returned is GIT'S spelling, not the caller's: the
    caller's may reach the same directory through a link, and only git's is safe to act on.
    """
    entries, note = _worktree_list(checkout)
    if entries is None:
        return None, "", note
    want = _norm_wt_path(worktree)
    for path, branch, _locked in entries:
        if _norm_wt_path(path) == want:
            return path, branch, ""
    note = f"{worktree} is not in `git -C {checkout} worktree list`"
    if Path(worktree).is_dir():
        note += _residue_hint(Path(worktree))
    return None, "", note


#: Entry names a non-empty leftover's refusal shows before eliding the rest.
_RESIDUE_PREVIEW = 5


def _residue_hint(worktree: Path) -> str:
    """What the directory at an unregistered path measures as, appended to the refusal's detail.

    `git worktree remove` deregisters the tree before it deletes the directory, so when a shell
    or process still has its working directory inside (Windows refuses that delete) git reports
    the failure but the tree is already gone from `worktree list`, and the directory stays. The
    guard cannot see that history, so only what it measures here is stated as fact, and a
    removal step is offered only for an EMPTY plain directory, in forms that refuse a non-empty
    one in each shell. PowerShell's `rmdir` is `Remove-Item`, which prompts and, on a yes,
    deletes the contents, so it is never what the PowerShell form names.
    """
    head = (
        ", but a directory is at that path.\n  Git does not list it; if it was a worktree of "
        "this checkout, git has deregistered it."
    )
    if _is_link(worktree):
        # A recursive delete through a junction is how a target's contents get emptied.
        return head + (
            " The path is itself a symlink or junction, so this report gives no command for it; "
            "a human decides what it points at before touching it."
        )
    try:
        with os.scandir(worktree) as it:
            names = sorted(e.name for e in it)
    except OSError as exc:
        return head + f" Its contents could not be listed ({exc}), so this report gives no command."
    if names:
        shown = ", ".join(repr(n) for n in names[:_RESIDUE_PREVIEW])
        if len(names) > _RESIDUE_PREVIEW:
            shown += ", ..."
        what = (
            " A `.git` entry is there, so it may be a working tree or clone of another repository."
            if ".git" in names
            else ""
        )
        return head + (
            f" It is not empty: it holds {len(names)} "
            f"{'entry' if len(names) == 1 else 'entries'} ({shown}).{what}\n  This report gives "
            "no command for a directory that is not empty; a human checks what is inside before "
            "deleting anything by hand."
        )
    abs_path = str(worktree.absolute())
    if _bad_chars(abs_path):
        p, quoting = "<path>", f", quoting {abs_path!r} for the shell"
    else:
        p, quoting = abs_path, ""
    return head + (
        " It is empty - typically what a `git worktree remove` leaves when its final delete "
        "lost to a shell or process still using the directory.\n  A human clears it by closing "
        "any shell or process whose working directory is inside it, then removing the empty "
        f'directory{quoting}: `rmdir "{p}"` in bash, or `[IO.Directory]::Delete("{p}")` in '
        "PowerShell (not PowerShell's `rmdir`, an alias that can delete contents). Both refuse "
        "a directory that is not empty; if one does, re-run this report, which lists what is "
        "inside."
    )


def _is_link(path: Path) -> bool:
    """A symlink, or on Windows a junction or any other reparse point."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _walk_tree(root: Path) -> tuple[list[str], list[str], list[str]]:
    """``(links, nested repos, walk errors)`` for everything below *root*.

    A link is never descended into, and its NAME earns it nothing: `npm link` makes
    `node_modules/<pkg>` a junction onto a checkout elsewhere, the name passes the regenerable
    allowlist, and `git worktree remove` follows it and empties the target outside the tree.

    ANY `.git` entry, file or directory and in any letter case, is a repository of its own, and
    its work is invisible to this tree's `status`: an editable install clones into `.venv/src/<pkg>`
    with a real `.git`, the path is an allowlisted cache, and its uncommitted edits and unpushed
    commits go with the tree. The one exemption is the tree's OWN gitfile, a regular file at the
    root. A nested `.git` directory is still walked, since a link inside it is deleted the same way.
    """
    links: list[str] = []
    nested: list[str] = []
    errors: list[str] = []

    def onerror(exc: OSError) -> None:
        errors.append(f"{exc.filename}: {exc.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
        here = Path(dirpath)
        keep = []
        for name in dirnames:
            rel = (here / name).relative_to(root).as_posix()
            if _is_link(here / name):
                links.append(rel)
                continue
            if name.lower() == ".git":
                nested.append(rel)
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            rel = (here / name).relative_to(root).as_posix()
            if _is_link(here / name):
                links.append(rel)
            elif name.lower() == ".git" and not (here == root and name == ".git"):
                nested.append(rel)
    return links, nested, errors


def _literal(path: str) -> str:
    """`_norm_wt_path` WITHOUT resolving: the spelling git registered, compared as written."""
    return str(path).replace("\\", "/").rstrip("/").lower()


def resolve_epic(directory: str, branch: str) -> str:
    """The epic token both spellings carry: its number, from the directory first, then the branch.

    EMPTY WHEN NEITHER CARRIES A NUMBER, and the caller refuses. A slug alone is not enough: the
    directory `e0768-account-limit-keystone` and the work cell `E-0768-the-fleet-cannot-survive`
    share no substring (gh#456), so without the number a live claim can fail to match and the
    tree reads unowned - the one error this helper exists to rule out.
    """
    for text in (directory, branch):
        m = _WT_EPIC_NUM.search(text or "")
        if m:
            return f"E-{m.group(1)}"
    return ""


def _norm_status(cell: str) -> str:
    return cell.strip().strip("`").strip().lower()


def live_owners(reg: _registry.Registry, directory: str, branch: str) -> list[Refusal]:
    """Every registry row that says a lane may own this worktree. Pure: the caller reads the file.

    Matching reuses `fleet_report._claims_worktree` (epic number first, substring fallback), which
    is deliberately generous: a false "owned" costs one skipped cleanup, a false "unowned" costs
    the only copy of a file. Both the directory and the branch are tried.

    BOTH TABLES ARE READ AS "LIVE UNLESS PROVEN OVER". An Agents row is live unless its status is
    `idle`; a Queue row is live unless its state is terminal or it is a `ready` row nobody has
    claimed. A typo, a new state, or a hand-edited cell therefore holds the tree rather than
    releasing it. A reaped row is no exception: `reap` writes it `idle`, so one that still says
    anything else was not reaped by `reap`.
    """

    def names(cell: str) -> bool:
        cell = cell.strip()
        if cell in _EMPTY_CELLS:
            return False
        return any(_claims_worktree(n, [cell]) for n in (directory, branch) if n)

    out: list[Refusal] = []
    for row in reg.agents.values():
        if _norm_status(row.status) != _IDLE and names(row.work):
            out.append(
                Refusal(
                    "worktree_lane_live",
                    f"{row.agent} is `{row.status.strip()}` on `{row.work.strip()}` (heartbeat "
                    f"{row.heartbeat.strip()}). Leave the tree; if that lane is dead, reap it first.",
                )
            )
    for row in reg.queue.values():
        state = row.state.strip()
        claimant = row.claimed_by.strip()
        if state in _registry.TERMINAL_QUEUE_STATES:
            continue
        if state == "ready" and claimant in _EMPTY_CELLS:
            continue
        if names(row.id) or names(row.work_item):
            kind = "in flight" if state in _registry.RETURNABLE_QUEUE_STATES else "claimed"
            out.append(
                Refusal(
                    "worktree_queue_live",
                    f"Queue {row.id} is `{state}` ({kind}), claimed by `{claimant or '-'}`. "
                    "Leave the tree.",
                )
            )
    return out


#: The two sections the ownership question is asked of, with each table's key-column header.
_OWNER_TABLES = (("Agents", "agent"), ("Queue", "id"))

#: The cells that say WHO owns WHAT, by column index. The registry CLI writes only ASCII into
#: them, so a non-ASCII character there is a hand edit whose meaning cannot be trusted: a
#: Cyrillic E or Devanagari digits read as an epic id to a person and as nothing to a regex, and
#: folding lookalikes is a list that never ends. Refusing is the closed form of that list.
_KEY_CELLS = {"Agents": (0, 1, 2, 6), "Queue": (0, 2, 3, 4)}

#: A heading line of either owner section, at any level or case: two of either refuses.
_OWNER_HEADING = re.compile(r"#{1,6}[ \t]*(agents|queue)[ \t]*", re.I)


def _epic_mention(epic: str) -> re.Pattern:
    """Any spelling of *epic*'s number a line can carry: `E-0787`, `e-0787`, `e0787`, `E-787`.

    Generous on purpose, like `_claims_worktree`: a line matched in error costs one kept tree.
    """
    num = int(epic.split("-", 1)[1])
    return re.compile(rf"(?<![0-9])e-?0*{num}(?![0-9])", re.I | re.ASCII)


def _fold(line: str) -> str:
    """*line* with lookalikes folded to ASCII before an id is matched against it.

    NFKC maps fullwidth and other compatibility digits to 0-9, format characters (zero-width
    space and the like) are dropped, and every dash punctuation character becomes `-`. Without
    this, `E‐0901` or `E-0​901` names the epic to a reader and to nothing here.
    """
    out = []
    for ch in unicodedata.normalize("NFKC", line):
        cat = unicodedata.category(ch)
        if cat == "Cf":
            continue
        out.append("-" if cat == "Pd" else ch)
    return "".join(out)


def _schema_headers() -> dict[str, str]:
    """Each owner table's header row, taken from the registry module's own template."""
    template = _registry._template_text("-")
    out = {}
    for section, key in _OWNER_TABLES:
        for line in _registry._get_body(template, section).split("\n"):
            cells = _registry._cells(line)
            if cells and cells[0] == key:
                out[section] = line.strip()
                break
        else:
            # The rule text no longer carries the remedy, so the detail does. It names the
            # shipped template, not the live board: the registry's own advice ("re-add the
            # heading, never `init`") would send a lane to hand-edit an intact live registry.
            raise _registry.RegistryError(
                "file_damaged",
                f"templates/registry.md has no ## {section} header - restore that file from git "
                "history; the live registry is not the damaged file",
            )
    return out


@dataclass
class _Table:
    """Where one owner section and its one table sit, as 0-based line indices of the file."""

    span: range
    rows: dict[int, list[str]]


def _locate_tables(lines: list[str], headers: dict[str, str]) -> tuple[dict[str, _Table], str]:
    """``({section: table}, "")`` when both owner tables have the schema's shape, else ``({}, why)``.

    The shape is positive: exactly one heading per section, the table's header row equal to the
    schema's, a separator of the schema's width under it, then data rows that each start with a
    pipe and carry the schema's cell count, up to the first blank line. A pipe-led line anywhere
    else in the section is a second table the parser would also read, so it refuses.
    """
    for section in ("Agents", "Queue"):
        found = [
            i
            for i, ln in enumerate(lines)
            if (m := _OWNER_HEADING.fullmatch(ln.strip())) and m.group(1).lower() == section.lower()
        ]
        if len(found) != 1:
            return {}, f"{len(found)} `## {section}` headings, not exactly one"
        if lines[found[0]].rstrip() != f"## {section}":
            return {}, f"the {section} heading is `{lines[found[0]].strip()}`, not `## {section}`"
    starts = [i for i, ln in enumerate(lines) if _registry._SECTION_RE.fullmatch(ln.rstrip())]
    out: dict[str, _Table] = {}
    for section, _key in _OWNER_TABLES:
        head = next(i for i in starts if lines[i].rstrip() == f"## {section}")
        end = next((i for i in starts if i > head), len(lines))
        span = range(head + 1, end)
        want = headers[section]
        width = len(_registry._cells(want) or [])
        at = next((i for i in span if lines[i].lstrip().startswith("|")), None)
        if at is None or lines[at].rstrip() != want:
            got = "none" if at is None else f"`{lines[at].strip()}`"
            return {}, f"## {section} header row is {got}, not the schema's `{want}`"
        sep = _registry._cells(lines[at + 1]) if at + 1 < end else None
        if not (sep and _registry._is_separator(sep) and len(sep) == width):
            return {}, f"## {section} header row is not followed by a {width}-cell separator"
        rows: dict[int, list[str]] = {}
        i = at + 2
        while i < end and lines[i].strip():
            cells = _registry._cells(lines[i])
            if not lines[i].startswith("|") or cells is None:
                return {}, f"## {section} line {i + 1} sits in the table without a leading pipe"
            if len(cells) != width:
                return {}, (
                    f"## {section} row `{cells[0] if cells else ''}` has {len(cells)} cells, not "
                    f"{width} - its columns cannot be trusted to say who owns what"
                )
            bad = [c for c in _KEY_CELLS[section] if not cells[c].isascii()]
            if bad:
                return {}, (
                    f"## {section} line {i + 1} has a non-ASCII character in a key cell - an id "
                    "spelled with lookalikes names the epic to a reader and nothing here"
                )
            rows[i] = cells
            i += 1
        stray = [
            j
            for j in span
            if j not in rows and j not in (at, at + 1) and lines[j].lstrip().startswith("|")
        ]
        if stray:
            return {}, f"## {section} line {stray[0] + 1} is a table row outside the one table"
        heads = [j for j in span if lines[j].lstrip().startswith("#")]
        if heads:
            return {}, (
                f"## {section} line {heads[0] + 1} is a heading inside the section - what sits "
                "under it cannot be told apart from the section's own content"
            )
        out[section] = _Table(span=span, rows=rows)
    return out, ""


def _validate(registry_path: Path) -> str:
    """``""`` when `registry validate` passes on the file, else what it said. Run in-process."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = _registry._do_validate(Path(registry_path))
    except _registry.RegistryError as exc:
        return str(exc)
    return "" if rc == 0 else " ".join(buf.getvalue().split())[:400]


def _read_owners(registry_path: Path, epic: str, directory: str, branch: str) -> list[Refusal]:
    """Read the registry NOW and ask it who owns the tree. An ALLOWLIST, failing closed.

    Four review rounds each found a registry shape the parser read wrong - a second section,
    reordered columns, a row without its leading pipe - and each fix named one more shape. So the
    rule is now positive. The registry must validate and hold both owner tables in the schema's
    exact shape (`_locate_tables`), the parse must account for every row, and then EVERY line of
    the file that names the epic, wherever it sits and however it is indented, must be one of:
    a well-formed Queue row in a terminal state, a well-formed Agents row whose status is `idle`,
    or a Notes bullet (Notes carry decisions, not claims). A well-formed row that is live refuses
    as live; any other line refuses as unreadable. `live_owners` then runs as a second layer,
    which still catches a row that names the epic only by its slug.
    """

    def unreadable(why: str) -> list[Refusal]:
        return [Refusal("worktree_registry_unreadable", why.replace("\n", " "))]

    try:
        text = _registry._read_registry_text(Path(registry_path))
        headers = _schema_headers()
        reg = _registry.parse_registry(text)
    except _registry.RegistryError as exc:
        return unreadable(str(exc))
    lines = text.split("\n")
    tables, why = _locate_tables(lines, headers)
    if not tables:
        return unreadable(why)
    missing = [a for a in _registry.AGENTS if a not in reg.agents]
    if missing:
        return unreadable(
            f"## Agents parsed without {', '.join(missing)} - a lane that cannot be read cannot "
            "be ruled out as the owner"
        )
    for section, parsed in (("Agents", reg.agents), ("Queue", reg.queue)):
        keys = [cells[0] for cells in tables[section].rows.values()]
        if len(set(keys)) != len(keys):
            return unreadable(
                f"## {section} repeats a key - a duplicate id hides every row but its last, and "
                "a hidden row may be the owner"
            )
        dropped = [k for k in keys if k not in parsed]
        if dropped or len(parsed) != len(keys):
            first = dropped[0] if dropped else "?"
            return unreadable(
                f"## {section} holds {len(keys)} rows but {len(parsed)} parsed (first unparsed: "
                f"`{first}`) - an unparsed row may be the owner"
            )
    why = _validate(registry_path)
    if why:
        return unreadable(f"registry validate fails: {why}")

    mention = _epic_mention(epic)
    agents, queue = tables["Agents"], tables["Queue"]
    live: list[Refusal] = []
    for i, line in enumerate(lines):
        if not mention.search(_fold(line)):
            continue
        if i in queue.rows:
            cells = queue.rows[i]
            if cells[4] in _registry.TERMINAL_QUEUE_STATES:
                continue
            live.append(
                Refusal(
                    "worktree_queue_live",
                    f"Queue {cells[0]} is `{cells[4]}`, claimed by `{cells[3] or '-'}`. "
                    "Leave the tree.",
                )
            )
        elif i in agents.rows:
            cells = agents.rows[i]
            if _norm_status(cells[1]) == _IDLE:
                continue
            live.append(
                Refusal(
                    "worktree_lane_live",
                    f"{cells[0]} is `{cells[1]}` on a row naming {epic} (heartbeat {cells[4]}). "
                    "Leave the tree; if that lane is dead, reap it first.",
                )
            )
        elif (
            i in queue.span
            and line.lstrip().startswith("- ")
            and not _registry._is_deploy_lock_field(line)
        ):
            continue  # a Notes bullet, live or struck
        else:
            return unreadable(
                f"line {i + 1} names {epic} but is not a well-formed Queue or Agents row or a "
                f"Notes bullet, so what it says about the tree cannot be read: `{line.strip()}`"
            )
    return live_owners(reg, directory, branch) or live


def _is_regenerable(path: str) -> bool:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if any(p in REGENERABLE_DIRS for p in parts):
        return True
    return bool(parts) and parts[-1].endswith(REGENERABLE_SUFFIXES)


def _resolve_base(checkout: Path, base_branch: Optional[str]) -> tuple[str, str]:
    """``(base branch name, "")`` from the project config, else the remote's default; or ``("", why)``."""
    if base_branch:
        return base_branch, ""
    proc = _git(checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    head = proc.stdout.strip()
    if proc.returncode != 0 or not head.startswith("origin/"):
        return "", f"no base branch configured and origin/HEAD unresolved ({_err(proc)})"
    return head[len("origin/") :], ""


def _landed_upstream(worktree: Path, base: str) -> tuple[bool, str]:
    """Has the branch's NET change already landed on ``origin/<base>``? ``(landed, detail)``.

    THE FLEET SQUASH-MERGES (`gh pr merge --squash --delete-branch`), so a finished branch's own
    commits are on no remote ref and `log --not --remotes` is never empty for it. What did land is
    one commit carrying the branch's whole change. So the branch's net change is taken the same
    way - its fork point diffed against its final TREE - and `_verbatim_upstream` asks whether a
    commit with that exact patch is already upstream. A local commit made after the merge changes
    the net patch, so no upstream commit matches and it refuses.

    READ-ONLY (gh#614): the tree is diffed directly, so no object is written into the store this
    tree shares with the canonical checkout. This used to build a synthetic commit for `git
    cherry` and then demand the byte-identical patch as proof. Byte equality of the two exact
    diffs (full blob ids, no renames) means the same file changes, and cherry's patch-id is a
    function of those changes, so cherry could never refuse a tree the proof accepted: the filter
    decided nothing, and dropping it leaves every verdict as it was.

    The exact diff carries whole-file blob ids (`--full-index`), so a squash made after another PR
    changed the same file between the fork point and the squash matches nothing and the tree is
    kept. Four review rounds of tree-merge fallbacks
    each let a new shape of local work through (a partial revert, a merge driver, a cherry-pick
    plus a revert), so a drifted squash fails safe: a human or the owning lane removes it.
    Anything that cannot be asked reads as not landed.
    """
    upstream = f"origin/{base}"
    mb = _git(worktree, "merge-base", upstream, "HEAD")
    if mb.returncode != 0 or not mb.stdout.strip():
        return False, f"merge-base {upstream} HEAD {_err(mb)}"
    # Pinned once, so both halves of the comparison read the same tree even if HEAD moves.
    tree = _git(worktree, "rev-parse", "--verify", "HEAD^{tree}")
    if tree.returncode != 0 or not tree.stdout.strip():
        return False, f"rev-parse HEAD^{{tree}} {_err(tree)}"
    return _verbatim_upstream(worktree, mb.stdout.strip(), tree.stdout.strip(), upstream)


#: One fixed diff shape for both sides of the byte comparison, so no config can make two
#: different patches print alike: full blob ids, binary content, no renames, no textconv.
_EXACT_DIFF = (
    "-c",
    "core.quotePath=true",
    "diff-tree",
    "--ignore-submodules=none",
    "-p",
    "--binary",
    "--full-index",
    "--no-renames",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


def _verbatim_upstream(worktree: Path, mb: str, tree: str, upstream: str) -> tuple[bool, str]:
    """Is there a commit on *upstream* whose patch is BYTE-identical to the branch's net patch?

    Bytes, not a patch-id: a patch-id strips whitespace from every diff line, so a local commit
    that only re-indents a line the branch added - Python scope, YAML nesting - would match, and
    that commit would be deleted with the tree. Equality of the two diffs is the proof: the same
    patch applied at the fork point gives the branch's *tree*, and it is upstream.
    """
    mine = _git_bytes(worktree, *_EXACT_DIFF, mb, tree)
    if mine.returncode != 0:
        return False, f"diff-tree {_err(mine)}"
    commits = _git(worktree, "rev-list", "--no-merges", f"{mb}..{upstream}")
    if commits.returncode != 0:
        return False, f"rev-list {_err(commits)}"
    for sha in commits.stdout.split():
        theirs = _git_bytes(worktree, *_EXACT_DIFF, f"{sha}^", sha)
        if theirs.returncode != 0:
            return False, f"diff-tree {sha} {_err(theirs)}"
        if theirs.stdout == mine.stdout:
            return True, ""
    return False, f"no commit on {upstream} carries the branch's net patch byte for byte"


def measure(
    worktree: Path, checkout: Optional[Path] = None, base_branch: Optional[str] = None
) -> list[Refusal]:
    """Re-measure the tree NOW. Called only from `check`, never fed an older reading."""
    out: list[Refusal] = []
    if checkout is not None:
        entries, note = _worktree_list(checkout)
        if entries is None:
            return [Refusal("worktree_unmeasurable", note)]
        here = _norm_wt_path(worktree)
        # Git refuses `worktree remove` on a locked tree, so the command would only fail; a lock
        # also usually means someone meant to keep it.
        locks = [lk for p, _, lk in entries if lk is not None and _norm_wt_path(p) == here]
        if locks:
            out.append(Refusal("worktree_locked", f"lock reason: {locks[0] or '(none given)'}"))
        nested = [p for p, _, _ in entries if _norm_wt_path(p).startswith(here + "/")]
        if nested:
            out.append(
                Refusal(
                    "worktree_nested_worktree",
                    f"{len(nested)} registered worktree(s) inside it, first: {nested[0]}",
                )
            )

    # Before any ignored path is classified by name: a link's name says nothing about its target.
    links, nested_repos, walk_errors = _walk_tree(worktree)
    if walk_errors:
        return out + [
            Refusal("worktree_unmeasurable", f"walking the tree for links: {walk_errors[0]}")
        ]
    if nested_repos:
        out.append(
            Refusal(
                "worktree_nested_repo",
                f"{len(nested_repos)} .git entr(ies) below the root, first: {nested_repos[0]}",
            )
        )
    if links:
        out.append(
            Refusal(
                "worktree_link_inside",
                f"{len(links)} link(s) inside the tree, first: {links[0]}",
            )
        )

    status = _git(
        worktree,
        "status",
        "--porcelain",
        "--ignored",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    unpushed = _git(worktree, "log", "--oneline", "HEAD", "--not", "--remotes")
    # `status` does not see an edit under --assume-unchanged or --skip-worktree, and removal
    # deletes it; `ls-files -v` shows those flags as a lowercase tag or `S`.
    flags = _git(worktree, "ls-files", "-v")
    failed = [
        Refusal("worktree_unmeasurable", f"`git {name}` {_err(proc)}")
        for name, proc in (
            ("status --porcelain", status),
            ("log --not --remotes", unpushed),
            ("ls-files -v", flags),
        )
        if proc.returncode != 0
    ]
    if failed:
        return out + failed

    lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
    dirty = [ln for ln in lines if not ln.startswith("!! ")]
    ignored = [ln[3:].strip() for ln in lines if ln.startswith("!! ")]
    kept = [p for p in ignored if not _is_regenerable(p)]
    hidden = [ln[2:] for ln in flags.stdout.splitlines() if ln[:1].islower() or ln[:1] == "S"]
    if dirty:
        out.append(
            Refusal("worktree_dirty_at_removal", f"{len(dirty)} path(s), first: {dirty[0].strip()}")
        )
    if hidden:
        out.append(
            Refusal(
                "worktree_dirty_at_removal",
                f"index flags hide edits: {len(hidden)} path(s) marked assume-unchanged or "
                f"skip-worktree, first: {hidden[0]}",
            )
        )
    if kept:
        out.append(
            Refusal(
                "worktree_ignored_content",
                f"{len(kept)} ignored path(s) removal would delete, first: {kept[0]}",
            )
        )
    commits = unpushed.stdout.strip().splitlines()
    if commits:
        base, why = _resolve_base(checkout if checkout is not None else worktree, base_branch)
        if base:
            landed, why = _landed_upstream(worktree, base)
            if landed:
                return out
        out.append(
            Refusal(
                "worktree_unpushed_at_removal",
                f"{len(commits)} commit(s) on no remote, newest: {commits[0].strip()}; {why}",
            )
        )
    return out


def check(
    checkout: Path,
    worktree_root: Path,
    project: str,
    worktree: Path,
    registry_path: Path,
    base_branch: Optional[str] = None,
) -> Verdict:
    """Answer "may this tree be removed right now?" in the module docstring's order.

    Reads the registry and measures the tree inside this call. There is no parameter through
    which an earlier answer can be supplied, which is the whole of the gh#498 repair.
    ``base_branch`` is the project's configured base; without it the remote's default is used.
    """
    v = Verdict(worktree=worktree)
    root = Path(worktree_root) / project
    if _norm_wt_path(worktree.parent) != _norm_wt_path(root):
        v.refusals.append(
            Refusal("worktree_outside_root", f"{worktree} is not a direct child of {root}")
        )
        return v

    registered, branch, note = _registered_branch(checkout, worktree)
    if registered is None:
        v.refusals.append(Refusal("worktree_not_registered", note))
        return v
    if _is_link(worktree) or _norm_wt_path(worktree) != _literal(registered):
        v.refusals.append(
            Refusal(
                "worktree_path_alias",
                f"{worktree} reaches registered tree {registered} through a link; pass the "
                "registered path",
            )
        )
        return v
    v.registered, v.branch = registered, branch
    reg_path = Path(registered)

    v.epic = resolve_epic(reg_path.name, branch)
    if not v.epic:
        v.refusals.append(
            Refusal(
                "worktree_epic_unresolved",
                f"neither `{reg_path.name}` nor branch `{branch or '(detached)'}` names an epic",
            )
        )
        return v

    v.refusals.extend(_read_owners(registry_path, v.epic, reg_path.name, branch))
    if v.refusals:
        return v

    v.refusals.extend(measure(reg_path, checkout, base_branch))
    v.refusals.extend(_unquotable(checkout, registered))
    return v


def _bad_chars(path: str) -> list[str]:
    """What in *path* would end or expand it inside a double-quoted bash or PowerShell argument."""
    bad = [repr(ch) for ch in sorted(set(path)) if ch in _UNQUOTABLE or not ch.isprintable()]
    # Inside bash double quotes a backslash escapes a backslash or the closing quote, so a
    # doubled one (a UNC path's leading pair) collapses and a trailing one (a drive root)
    # swallows the quote. A single inner backslash, as in an ordinary Windows path, is kept.
    if "\\\\" in path:
        bad.append("a doubled backslash")
    if path.endswith("\\"):
        bad.append("a trailing backslash")
    return bad


def _unquotable(checkout: Path, registered: str) -> list[Refusal]:
    """A refusal for each path the printed command would wrap in double quotes unsafely."""
    out = []
    for label, path in (("checkout", str(checkout)), ("worktree", registered)):
        bad = _bad_chars(path)
        if bad:
            out.append(
                Refusal(
                    "worktree_path_unquotable",
                    f"{label} path {path!r} holds {', '.join(bad)}; remove "
                    "the tree by hand, quoting the path for your shell",
                )
            )
    return out


#: Printed around the removal pair. Git deregisters the tree BEFORE it deletes the directory, so
#: a delete that loses to a process still inside (Windows refuses it) leaves a directory git no
#: longer lists: `worktree list` reads as success while the directory stays behind.
REMOVAL_BEFORE = ("# first close any shell or process whose working directory is inside the tree",)
REMOVAL_AFTER = (
    "# then check the directory itself is gone, not just absent from `git worktree list`:",
    "# git deregisters the tree even when deleting the directory fails. If it is still there,",
    "# re-run this report, which says how to clear what is left.",
)


def removal_command(checkout: Path, v: Verdict) -> list[str]:
    """The manual one-liner a human runs for a removable *v*; empty for a refused one.

    Printed, never executed (Q-10093). No `--force`, so git still refuses a tree that became
    dirty between the report and the run. Paths go in double quotes, so a path that would end or
    expand them prints nothing, even for a verdict built without `check`.
    """
    if not v.removable or not v.registered or _unquotable(checkout, v.registered):
        return []
    return [
        f'git -C "{checkout}" worktree remove "{v.registered}"',
        f'git -C "{checkout}" worktree prune',
    ]


def main(argv=None) -> int:
    """The CLI. Report-only, always: it prints a verdict and, when removable, the command.

    EXIT CODE, the same contract as `agentflow.refs` and `agentflow.backups`:

    * ``0`` - removable; the manual removal command is printed.
    * ``1`` - refused; no command is printed.
    * ``2`` - malformed call, or the project could not be resolved.

    The tree is left untouched in every case.
    """
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(prog="python -m agentflow.worktree_remove", description=__doc__)
    ap.add_argument("worktree", help="the tree to judge: <worktree_root>/<project>/<epic-branch>")
    ap.add_argument(
        "--project",
        help="project whose checkout, worktree root and registry to use (default: the resolved "
        "session project, same precedence every other agentflow CLI uses)",
    )
    ap.add_argument(
        "--registry",
        help="registry file to read (default: <data_root>/projects/<name>/status/registry.md)",
    )
    args = ap.parse_args(argv)

    from . import config as _config

    try:
        env = _config.load_environment()
        name = _config.resolve_project_name(args.project)
        if not name:
            print("could not resolve project: pass --project <name>")
            return 2
        proj = _config.load_project(name)
    except _config.ConfigError as exc:
        print(f"could not resolve project: {exc}")
        return 2

    checkout = Path(proj.canonical_repo.local_checkout).expanduser()
    base_branch = getattr(proj.canonical_repo, "base_branch", None) or None
    worktree_root = Path(env.worktree_root).expanduser()
    registry_path = (
        Path(args.registry)
        if args.registry
        else Path(env.data_root) / "projects" / name / "status" / "registry.md"
    )
    worktree = Path(args.worktree).expanduser()

    v = check(checkout, worktree_root, name, worktree, registry_path, base_branch)

    print(f"# agentflow worktree_remove - {worktree}")
    print(f"branch: {v.branch or '-'}   epic: {v.epic or '-'}")
    for r in v.refusals:
        print(r)
    if v.refusals:
        print("LEFT IN PLACE.")
        return 1
    print("removable: every check passed at this instant. Nothing was removed (report-only,")
    print("Q-10093); re-run this report, then a human removes it with:")
    for line in REMOVAL_BEFORE:
        print(f"  {line}")
    for line in removal_command(checkout, v):
        print(f"  {line}")
    for line in REMOVAL_AFTER:
        print(f"  {line}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
