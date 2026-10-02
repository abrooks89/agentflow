"""The share cut: a readiness gate (``check``) and a local-only single-commit cut (``cut``).

Reached as ``python -m agentflow.publish check|cut``. The human procedure that uses both is
``docs/runbooks/publish.md``; this module is the part of it a machine can do safely.

WHAT ``cut`` BUILDS. A new git repo at ``--out`` holding exactly ONE parentless commit whose
tree is ``--rev``'s tree minus :data:`EXCLUDED_PATTERNS`. The share is a fresh cut rather than a
push of the working history because that history carries what the share must not (session
trailers on some commits, a personal author address on every one). A cut shares no object with
that history except the file contents it ships.

LEAK SAFETY, which is the whole design:

* Only BLOB objects of the kept files cross into ``--out`` (``pack-objects`` over their ids,
  ``unpack-objects`` inside ``--out``). The source commit, its parents and its tree objects never
  do; the trees are rebuilt inside ``--out`` by ``write-tree`` from an index filled there. Nothing
  is fetched or cloned from the source, so no ref, reflog, alternate or config comes along.
* Every git call that touches ``--out`` runs with a hermetic config: no system config
  (``GIT_CONFIG_NOSYSTEM``), an empty global config, and ``core.autocrlf=false`` set locally
  before checkout. So no ambient identity, signing key, line-ending rewrite or hook path from the
  operator's machine reaches the commit.
* Author and committer are the constant :data:`AUTHOR_NAME` plus a GitHub noreply address,
  passed through the four ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` variables.
* Every subprocess goes through :func:`_run`, the one call site, so a test can record every argv
  and prove nothing outward (no remote, fetch, push or ``gh``) ever runs.

BYTE-EXACT ON EVERY OS. The tree is built from git objects, never by extracting files and adding
them back (on Windows that silently rewrites modes and line endings). With nothing excluded the
cut's tree id equals ``git rev-parse <rev>^{tree}``; the Linux re-verify cites that id.

WHAT ``check`` REPORTS. One line per check, ``PASS|FAIL|REPORT <name> <detail>``. A check that
cannot measure prints ``FAIL <name> cannot measure - <why>`` and makes the exit ``2``. Findings
print names, counts, paths and line numbers ONLY - never a matched credential, never a matched
sanitize term or local sensitive term, never a ledger cell. The flagged shapes are assembled
from fragments so this file never matches its own gate.

STDLIB ONLY. No network, no model call, no clock beyond the commit timestamp git itself stamps.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime as _dt
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Mapping, Sequence

from agentflow.pillar_residue import ResidueUnmeasurable, sanitize_pattern
from agentflow.sanitize_extra import (
    ExtraPattern,
    ExtraPatternUnreadable,
    project_extra_path,
    read_extra_pattern,
)

# --------------------------------------------------------------------------- #
# Constants - the parts of the cut that are fixed on purpose
# --------------------------------------------------------------------------- #
#: The cut's author AND committer name. A constant, with no flag, so no operator's name can
#: reach the commit and the cut commands already written in sibling specs stay valid.
AUTHOR_NAME = "agentflow"

#: The cut commit's message. Fixed text that cites no private sha.
COMMIT_MESSAGE = (
    "agentflow: single-commit share cut\n"
    "\n"
    "A fresh cut of the product tree. It carries no history of the repository it was cut from.\n"
)

#: The only branch the cut repo has.
CUT_BRANCH = "master"

#: Every key the cut's own config may hold, canonical (lower-case) spelling. Anything else in
#: its local or worktree config is refused: a remote, a URL rewrite, an include, a hook path,
#: a credential helper, a proxy, an ssh command or an fsmonitor can each redirect step (7)'s
#: push or run code during it, and a list of what to refuse is never complete. These are the
#: keys ``git init`` writes on Linux, Windows or macOS (``filemode``, ``symlinks``,
#: ``ignorecase`` and ``precomposeunicode`` depend on the filesystem), plus the three
#: :func:`build_cut` sets. The two ``extensions`` keys are repository-format markers a git that
#: defaults to SHA-256 or reftable writes at init; neither runs anything or names a URL.
ALLOWED_CONFIG_KEYS = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.symlinks",
        "core.ignorecase",
        "core.precomposeunicode",
        "core.autocrlf",
        "core.safecrlf",
        "extensions.objectformat",
        "extensions.refstorage",
    }
)

#: The runbook the gate requires to ship in the cut.
RUNBOOK_PATH = "docs/runbooks/publish.md"

#: The ledger file inside ``<data_root>/projects/<project>/status/``.
LEDGER_FILENAME = "key-rotation-ledger.md"

#: The fields of the ledger's ``## Waiver`` section, one ``- <field>: <value>`` bullet each.
#: A complete waiver passes ``ledger-dated`` with rows still undated. It never fills
#: ``rotated_on``, and there is no flag that stands in for it: the ledger lives in the data root,
#: never ships, and only the human's recorded word puts a waiver there.
WAIVER_FIELDS: tuple[str, ...] = ("waived_by", "waived_on", "scope", "reason", "evidence")
#: The only scope a waiver may name.
WAIVER_SCOPE = "publish"
_WAIVER_HEADING_RE = re.compile(r"^##\s+Waiver\s*$")
#: A level-2 heading that starts with "waiver" in any case but is not exactly ``## Waiver``
#: (``## waiver``, ``## Waiver (publish)``). It waives nothing; the FAIL detail says so.
_NEAR_WAIVER_HEADING_RE = re.compile(r"^\s*##(?!#)\s*waiver", re.IGNORECASE)
_SECTION_END_RE = re.compile(r"^#{1,2}\s")
_WAIVER_BULLET_RE = re.compile(r"^\s*[-*]\s+([a-z_]+)\s*:(.*)$")
#: A fenced-code-block delimiter: three or more backticks or tildes, indented at most three
#: spaces. Group 1 is the run, group 2 what follows it (an info string on an opening fence).
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

#: The local-only term list inside ``<data_root>/projects/<project>/``. It lives in the data
#: root, never in the repo, so the terms it guards against never ship with the cut.
SENSITIVE_TERMS_RELPATH = Path("publish") / "sensitive-terms.txt"

#: The install guide whose Last-verified line cites the tree the Linux drill last passed on.
INSTALL_DOC = "docs/INSTALL.md"
_LAST_VERIFIED_RE = re.compile(
    r"Last verified fresh-clone install:\*\*\s+tree\s+`([0-9a-f]{40}(?:[0-9a-f]{24})?)`"
)

#: The exclusion list, explicit and tested. It KEEPS ``skills/_vendored/`` and inherits nothing
#: from ``ci.yml``'s directory exclusions: the cut ships the vendored skills and their license.
#: :func:`is_excluded` applies each to every path segment, ignoring case.
EXCLUDED_PATTERNS: tuple[str, ...] = (".env", ".env.*", "*.pem", ".worktrees/")

#: GitHub noreply shape: ``<digits>+<user>@users.noreply.github.com`` or ``<user>@...``.
NOREPLY_EMAIL_RE = re.compile(
    r"^(?:[0-9]+\+)?[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?"
    r"@users\.noreply\.github\.com$"
)

# The session-URL shape: the bare host string followed by a ``/session_`` id path. The bare host
# alone is NOT matched - the two rule statements that forbid these links name it. Assembled from
# fragments so this line is not itself a match (and never spells the agent CLI's name as a bare
# literal, which test_save_summariser reserves for the one module that starts a session).
_SESSION_HOST = re.escape("clau" + "de.ai" + "/code")
SESSION_URL_RE = re.compile(_SESSION_HOST + "/" + "session" + r"_[A-Za-z0-9]", re.IGNORECASE)
#: The session trailer, only at the start of a line (where git puts a trailer).
SESSION_TRAILER_RE = re.compile(r"(?im)^" + "clau" + "de-" + "session" + r"\s*:")

_NB = r"(?<![A-Za-z0-9])"
#: Credential shapes: (name, regex). Every one is assembled from fragments so the source line is
#: not itself a match, and the name is all a finding ever prints.
KEY_SHAPES: tuple[tuple[str, str], ...] = (
    ("anthropic-key", _NB + "sk" + "-ant-" + r"[A-Za-z0-9_\-]{20,}"),
    ("github-pat-classic", _NB + "gh" + "p_" + r"[A-Za-z0-9]{36}"),
    ("github-oauth", _NB + "gh" + "o_" + r"[A-Za-z0-9]{36}"),
    ("github-server-to-server", _NB + "gh" + "s_" + r"[A-Za-z0-9]{36}"),
    ("github-user-to-server", _NB + "gh" + "u_" + r"[A-Za-z0-9]{36}"),
    ("github-refresh", _NB + "gh" + "r_" + r"[A-Za-z0-9]{36}"),
    ("github-pat-fine-grained", _NB + "github" + "_pat_" + r"[A-Za-z0-9_]{22,}"),
    ("pem-private-key", "-----" + "BEGIN " + r"(?:[A-Z0-9]+ )*" + "PRIVATE " + "KEY" + "-----"),
    ("aws-access-key", _NB + "AK" + "IA" + r"[0-9A-Z]{16}"),
    ("slack-token", _NB + "xo" + "x" + r"[abprs]-[A-Za-z0-9\-]{10,}"),
    ("google-api-key", _NB + "AI" + "za" + r"[0-9A-Za-z_\-]{35}"),
)
_KEY_MATCHERS = tuple((name, re.compile(rx)) for name, rx in KEY_SHAPES)

_GH_REF_RE = re.compile(r"\bgh#[0-9]+")
_HEX_TOKEN_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")

#: Git environment variables that could redirect a ``git -C <dir>`` call to another repo, index
#: or object store. Stripped from every call so the only repo touched is the one named.
_REDIRECTING_GIT_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)

PASS, FAIL, REPORT, UNMEASURABLE = "PASS", "FAIL", "REPORT", "UNMEASURABLE"


class PublishUnmeasurable(Exception):
    """The gate could not measure. Maps to exit 2 - never to 0."""


class CutRefused(Exception):
    """``cut`` or ``check`` refused its inputs before writing or reading anything. Maps to
    exit 2."""


class GitFailed(Exception):
    """A git call exited non-zero where success was required."""


# --------------------------------------------------------------------------- #
# The ONE subprocess call site
# --------------------------------------------------------------------------- #
def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _REDIRECTING_GIT_VARS:
        env.pop(key, None)
    return env


def _run(
    argv: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run ``argv``. Every subprocess this module starts goes through here.

    The input bytes are never included in an error, because for ``cat-file`` and
    ``pack-objects`` they are object ids and for ``commit-tree`` the fixed message - but the
    rule is kept general so a later caller cannot leak file content through an exception.
    """
    proc = subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        env=_base_env() if env is None else env,
        check=False,
    )
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitFailed(f"{' '.join(argv[:4])} ... exited {proc.returncode}: {err}")
    return proc


def _git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo), *args], env=env, stdin=stdin, check=check)


# --------------------------------------------------------------------------- #
# Tree reading (from git objects, never a directory walk)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Entry:
    mode: str
    kind: str
    oid: str
    path: str


def _ls_tree(repo: Path, treeish: str) -> list[Entry]:
    out = _git(repo, "ls-tree", "-r", "-z", "--full-tree", treeish).stdout
    entries = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, kind, oid = meta.decode("ascii").split(" ")
        entries.append(Entry(mode, kind, oid, path.decode("utf-8", "surrogateescape")))
    return entries


def _read_blobs(repo: Path, oids: Iterable[str]) -> dict[str, bytes]:
    """Every blob's exact bytes, via one ``cat-file --batch`` call."""
    wanted = list(dict.fromkeys(oids))
    if not wanted:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin=("\n".join(wanted) + "\n").encode()).stdout
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in wanted:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode("ascii").split(" ")
        if len(header) != 3:
            raise PublishUnmeasurable(f"object {oid} could not be read from the cut ({header[-1]})")
        size = int(header[2])
        blobs[oid] = out[nl + 1 : nl + 1 + size]
        pos = nl + 1 + size + 1
    return blobs


def _is_binary(data: bytes) -> bool:
    """grep's ``--binary-files=without-match`` heuristic under a UTF-8 locale: a NUL byte or an
    invalid UTF-8 sequence makes the file binary, and binary files are skipped."""
    if b"\0" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def is_excluded(path: str) -> bool:
    """True when ``path`` (repo-relative, ``/``-separated) matches :data:`EXCLUDED_PATTERNS`.

    Every segment is tested, case-folded. The patterns are gitignore-style, where ``.env``
    names a directory as well as a file, and ``.ENV`` or ``.Worktrees`` is the same file on a
    case-insensitive checkout; a last-segment, exact-case match shipped all three into the cut
    while the check reported none. ``.worktrees/`` keeps its trailing slash: a directory only.

    A cut built on Linux can carry a ``\\`` inside a name, and Windows reads it as a separator
    and drops trailing dots and spaces, so ``cfg\\.env`` and ``.env `` check out as ``.env``.
    Both are normalised before comparing.
    """
    parts = [p.rstrip(". ").casefold() for p in re.split(r"[/\\]", path) if p]
    for i, seg in enumerate(parts):
        if seg == ".env" or seg.startswith(".env.") or seg.endswith(".pem"):
            return True
        if seg == ".worktrees" and i < len(parts) - 1:
            return True
    return False


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    name: str
    status: str
    detail: str
    extra: tuple[str, ...] = ()

    def lines(self) -> list[str]:
        if self.status == UNMEASURABLE:
            head = f"{FAIL} {self.name} cannot measure - {self.detail}"
        else:
            head = f"{self.status} {self.name} {self.detail}"
        return [head, *(f"  {x}" for x in self.extra)]


@dataclass
class CutFiles:
    """The decoded text of every file in the cut, plus the commit message."""

    paths: list[str]
    text: dict[str, str]
    binary: set[str]
    message: str


def _as_is(path: str) -> str:
    return path


def _hits(
    pattern: re.Pattern,
    files: CutFiles,
    *,
    include_binary: bool,
    show: Callable[[str], str] = _as_is,
) -> list[str]:
    """``path:line`` for every line matching ``pattern``, the path passed through ``show``
    (:func:`_path_display`). Never the line itself."""
    found = []
    for path in files.paths:
        if path in files.binary and not include_binary:
            continue
        for n, line in enumerate(files.text[path].splitlines(), 1):
            if pattern.search(line):
                found.append(f"{show(path)}:{n}")
    return found


def _path_display(
    files: CutFiles,
    terms: Sequence[tuple[int, str]],
    dropped: Sequence[str] = (),
    extra: re.Pattern | None = None,
) -> Callable[[str], str]:
    """How every check prints a path, so no check's output can carry a sensitive term.

    A path that holds any term as a plain case-insensitive SUBSTRING prints as its position:
    ``<cut path #K>`` in the cut's file listing, or ``<dropped path #K>`` among the paths the
    cut dropped. Substring on purpose, looser than the content match: masking a path that did
    not need it costs a lookup, and failing to mask one publishes the term in the run log.
    A path the local extra sanitize pattern (``extra``, compiled ignoring case) matches is
    masked the same way: those alternatives are kept out of ``ci.yml`` for the same reason the
    terms are kept out of the tree."""
    folded = [term.casefold() for _, term in terms]

    def held(path: str) -> bool:
        return any(t in path.casefold() for t in folded) or bool(extra and extra.search(path))

    labels: dict[str, str] = {}
    for kind, paths in (("cut", files.paths), ("dropped", dropped)):
        for i, path in enumerate(paths, 1):
            if held(path):
                labels.setdefault(path, f"<{kind} path #{i}>")

    def show(path: str) -> str:
        if path in labels:
            return labels[path]
        # A path outside both listings cannot be given a position, so it is withheld whole.
        if held(path):
            return "<path withheld>"
        return path

    return show


def _is_iso_date(text: str) -> bool:
    if not _DATE_RE.match(text):
        return False
    try:
        _dt.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _unfenced(lines: list[str]) -> Iterable[tuple[int, str]]:
    """``(1-based line number, line)`` for every line outside a fenced code block.

    A fence opens on three or more backticks or tildes and closes on a run of the SAME
    character at least as long with nothing after it, so a shorter run or the other character
    inside a block is content (that is how a block shows a fence). A backtick opener whose
    info string holds a backtick is not a fence. A fence never closed runs to the end of the
    file: a pasted example that lost its closing line must not start counting as real text.
    The delimiter lines themselves are never yielded."""
    fence: tuple[str, int] | None = None
    for n, line in enumerate(lines, 1):
        m = _FENCE_RE.match(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = (m.group(1)[0], len(m.group(1)))
                continue
            yield n, line
        elif (
            m
            and m.group(1)[0] == fence[0]
            and len(m.group(1)) >= fence[1]
            and not m.group(2).strip()
        ):
            fence = None


def _near_waiver_headings(lines: list[str]) -> list[int]:
    """Line numbers of unfenced headings that look like a waiver heading but are not one."""
    return [
        n
        for n, line in _unfenced(lines)
        if _NEAR_WAIVER_HEADING_RE.match(line) and not _WAIVER_HEADING_RE.match(line)
    ]


def _read_waiver(lines: list[str]) -> dict[str, str] | None:
    """The ledger's ``## Waiver`` fields, or None when it has no such section.

    A section runs to the next ``#`` or ``##`` heading; bullets outside one waive nothing.
    Lines inside a fenced code block (``` or ~~~) are skipped entirely: a filled-in example
    pasted in a fence is documentation, not the human's recorded word.
    Raises :class:`PublishUnmeasurable` naming the field when a waiver is present but not
    complete and valid: a field missing, empty or given twice (across sections too), a
    ``waived_on`` that is not an ISO date, or a ``scope`` other than :data:`WAIVER_SCOPE`.
    A waiver that cannot be read exactly one way is not read at all."""
    fields: dict[str, str] = {}
    found = inside = False
    for _, line in _unfenced(lines):
        if _WAIVER_HEADING_RE.match(line):
            found = inside = True
            continue
        if _SECTION_END_RE.match(line):
            inside = False
            continue
        m = _WAIVER_BULLET_RE.match(line) if inside else None
        if m is None or m.group(1) not in WAIVER_FIELDS:
            continue
        if m.group(1) in fields:
            raise PublishUnmeasurable(f"the waiver gives {m.group(1)} twice")
        fields[m.group(1)] = m.group(2).strip()
    if not found:
        return None
    for key in WAIVER_FIELDS:
        if not fields.get(key):
            raise PublishUnmeasurable(f"the waiver is missing {key}")
    if not _is_iso_date(fields["waived_on"]):
        raise PublishUnmeasurable("the waiver waived_on is not an ISO date")
    if fields["scope"] != WAIVER_SCOPE:
        raise PublishUnmeasurable(f"the waiver scope is not {WAIVER_SCOPE}")
    return fields


def check_ledger(ledger: Path | None, why: str | None = None) -> Result:
    """Every ledger row carries a ``rotated_on`` date, or the ledger records a complete waiver.

    A waiver passes with its signer and date and the undated count; its reason, its evidence
    and every table cell stay unprinted. An incomplete or invalid waiver is unmeasurable."""
    name = "ledger-dated"
    if ledger is None:
        return Result(name, UNMEASURABLE, f"no ledger path: {why or 'pass --ledger or --project'}")
    try:
        lines = ledger.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        return Result(name, UNMEASURABLE, f"cannot read {ledger}: {type(exc).__name__}")
    except UnicodeDecodeError:
        return Result(name, UNMEASURABLE, f"{ledger} is not UTF-8")

    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    header_at = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|") and "rotated_on" in cells(line):
            header_at = i
            break
    if header_at is None:
        return Result(name, UNMEASURABLE, f"{ledger} has no table with a rotated_on column")
    header = cells(lines[header_at])
    col = header.index("rotated_on")
    if header_at + 1 >= len(lines) or not re.match(r"^\s*\|[\s:|-]+\|?\s*$", lines[header_at + 1]):
        return Result(name, UNMEASURABLE, f"{ledger}: the rotated_on table has no separator row")
    rows = []
    for line in lines[header_at + 2 :]:
        if not line.lstrip().startswith("|"):
            break
        row = cells(line)
        if len(row) < len(header):
            return Result(
                name,
                UNMEASURABLE,
                f"{ledger}: a row has {len(row)} cells, the header has {len(header)}",
            )
        rows.append(row)
    if not rows:
        return Result(name, UNMEASURABLE, f"{ledger}: the rotated_on table has no rows")
    dated = sum(1 for row in rows if _is_iso_date(row[col]))
    try:
        waiver = _read_waiver(lines)
    except PublishUnmeasurable as exc:
        return Result(name, UNMEASURABLE, f"{ledger}: {exc}")
    if waiver is not None:
        return Result(
            name,
            PASS,
            f"waived by {waiver['waived_by']} on {waiver['waived_on']}: "
            f"{len(rows) - dated} of {len(rows)} rows undated",
        )
    status = PASS if dated == len(rows) else FAIL
    detail = f"{dated} of {len(rows)} rows dated"
    near = _near_waiver_headings(lines) if status == FAIL else []
    if near:
        # Line numbers only: the heading is the ledger's text, and a finding never prints it.
        detail += (
            f"; line(s) {', '.join(map(str, near))} look like a waiver heading but are not "
            "exactly '## Waiver', so they waive nothing"
        )
    return Result(name, status, detail)


def check_session_urls(files: CutFiles, show: Callable[[str], str] = _as_is) -> Result:
    found = _hits(SESSION_URL_RE, files, include_binary=True, show=show)
    found += _hits(SESSION_TRAILER_RE, files, include_binary=True, show=show)
    msg_hits = SESSION_URL_RE.search(files.message) or SESSION_TRAILER_RE.search(files.message)
    if msg_hits:
        found.append("<cut commit message>")
    if found:
        return Result("session-urls", FAIL, f"{len(found)} line(s)", tuple(sorted(set(found))))
    return Result("session-urls", PASS, "0 lines")


#: The local extra sanitize pattern, read once before any check runs: the pattern (configured
#: or not), or why it could not be read, which the ``sanitize-terms`` line prints as cannot
#: measure.
_LoadedExtra = tuple[ExtraPattern | None, str | None]


def _load_extra_pattern(path: Path | None, why: str | None = None) -> _LoadedExtra:
    """:func:`agentflow.sanitize_extra.read_extra_pattern`, with an unreadable file turned into
    the reason the ``sanitize-terms`` line prints. The reason never carries the pattern."""
    try:
        return read_extra_pattern(path, why=why), None
    except ExtraPatternUnreadable as exc:
        return None, str(exc)


def check_sanitize_terms(
    files: CutFiles,
    ci_yml: Path,
    show: Callable[[str], str] = _as_is,
    *,
    extra: _LoadedExtra | None = None,
) -> Result:
    """No text file matches the cut's ``ci.yml`` sanitize pattern, UNIONED with the local extra
    pattern (:mod:`agentflow.sanitize_extra`) when one is configured.

    The status line names the sources it applied, so a run that scanned the generic shapes alone
    says so. It never prints the pattern or a matched line. An extra file that exists and cannot
    be used is cannot measure, mirroring ``sensitive-terms``: falling back to the generic shapes
    would pass a cut nobody scanned for the private alternatives."""
    name = "sanitize-terms"
    loaded, error = extra if extra is not None else _load_extra_pattern(None)
    if error is not None:
        return Result(name, UNMEASURABLE, error)
    try:
        pattern = re.compile(sanitize_pattern(ci_yml, loaded), re.IGNORECASE)
    except ResidueUnmeasurable as exc:
        return Result(name, UNMEASURABLE, str(exc))
    sources = loaded.sources()
    found = _hits(pattern, files, include_binary=False, show=show)
    if found:
        return Result(
            name, FAIL, f"{len(found)} line(s) match the sanitize pattern ({sources})", tuple(found)
        )
    return Result(
        name,
        PASS,
        f"0 matches over {len(files.paths) - len(files.binary)} text files ({sources})",
    )


def _read_sensitive_terms(path: Path) -> list[tuple[int, str]]:
    """``(list line number, term)`` for every term in the local list at ``path``.

    One term per line, surrounding whitespace stripped. A blank line is skipped, and so is a
    line whose first non-blank character is ``#`` (a whole-line comment). A ``#`` anywhere
    else is part of the term, so a term may hold one. Raises ``OSError`` or
    ``UnicodeDecodeError``; neither message carries a term (the caller prints the type only)."""
    terms = []
    for n, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        term = raw.strip()
        if term and not term.startswith("#"):
            terms.append((n, term))
    return terms


#: Sensitive-term loading, before any check runs: the terms (possibly none) and, when the list
#: gave no terms to match, the result the ``sensitive-terms`` line prints instead.
_LoadedTerms = tuple[list[tuple[int, str]], Result | None]


def _load_sensitive_terms(terms_path: Path | None, why: str | None = None) -> _LoadedTerms:
    """The local list's terms, or no terms plus the ``sensitive-terms`` result that says why.

    No list is ``not configured`` and passes: this is an extra guard, not a new requirement.
    The status line names the path it looked for, so "not configured" is never a surprise. A
    list that exists but cannot be read, or a path the config could not resolve, cannot be
    measured."""
    name = "sensitive-terms"
    if terms_path is None and why:
        return [], Result(name, UNMEASURABLE, f"no terms list path: {why}")
    if terms_path is None:
        return [], Result(name, PASS, "not configured: no --sensitive-terms and no --project")
    if not terms_path.exists():
        return [], Result(name, PASS, f"not configured: {terms_path} does not exist")
    try:
        terms = _read_sensitive_terms(terms_path)
    except OSError as exc:
        return [], Result(
            name, UNMEASURABLE, f"cannot read {terms_path}: {type(exc).__name__}"
        )
    except UnicodeDecodeError:
        return [], Result(name, UNMEASURABLE, f"{terms_path} is not UTF-8")
    if not terms:
        return [], Result(name, PASS, f"not configured: {terms_path} lists 0 terms")
    return terms, None


def _term_matcher(term: str) -> re.Pattern:
    """Case-insensitive, whole word where only a LETTER or DIGIT continues a word.

    ``_`` and punctuation are boundaries, so a term is found inside an identifier
    (``TERM_API_KEY``, ``term_client``) and an address (``jane@term.example``). A letter or
    digit is not, so a longer word holding the term (``terms``, ``term2``) is not a hit;
    the path mask in :func:`_path_display` is the looser substring test. A term that itself
    begins or ends with a non-alphanumeric character (``@term.example``) carries its own
    boundary on that side, so no lookaround is added there."""
    left = r"(?<![^\W_])" if term[0].isalnum() else ""
    right = r"(?![^\W_])" if term[-1].isalnum() else ""
    return re.compile(left + re.escape(term) + right, re.IGNORECASE)


def check_sensitive_terms(
    files: CutFiles,
    terms_path: Path | None,
    why: str | None = None,
    *,
    loaded: _LoadedTerms | None = None,
    show: Callable[[str], str] | None = None,
) -> Result:
    """No file, path or commit message in the cut holds a term from the local-only list.

    The list is ``<data_root>/projects/<project>/publish/sensitive-terms.txt``
    (:data:`SENSITIVE_TERMS_RELPATH`): terms that must never ship but cannot be added to
    ``ci.yml``'s sanitize pattern, because ``ci.yml`` ships in the cut. Matching is
    :func:`_term_matcher`'s. A finding prints the location and the term's LINE NUMBER in the
    list, never the term: a run log is itself something that can be published. Every path is
    printed through :func:`_path_display`, which :func:`run_check` hands to every other check
    too, so a path that holds a term reaches no check's output.

    ``loaded`` is :func:`_load_sensitive_terms`'s result when the caller already read the
    list, and ``show`` the caller's display; both default to reading and building here."""
    name = "sensitive-terms"
    terms, early = loaded if loaded is not None else _load_sensitive_terms(terms_path, why)
    if early is not None:
        return early
    show = show or _path_display(files, terms)
    matchers = [(n, _term_matcher(term)) for n, term in terms]
    found: list[str] = []
    for path in files.paths:
        found += [
            f"{show(path)} (the path itself, list line {n})"
            for n, rx in matchers
            if rx.search(path)
        ]
    for path in files.paths:
        text = files.text[path]
        for n, rx in matchers:
            if not rx.search(text):
                continue
            found += [
                f"{show(path)}:{k} (list line {n})"
                for k, line in enumerate(text.splitlines(), 1)
                if rx.search(line)
            ]
    found += [
        f"<cut commit message> (list line {n})" for n, rx in matchers if rx.search(files.message)
    ]
    if found:
        return Result(
            name,
            FAIL,
            f"{len(found)} location(s) match a term on the local list {terms_path}",
            tuple(found),
        )
    return Result(
        name,
        PASS,
        f"0 matches for {len(terms)} term(s) from {terms_path} over {len(files.paths)} files",
    )


def check_key_shapes(files: CutFiles, show: Callable[[str], str] = _as_is) -> Result:
    extra = []
    for shape, rx in _KEY_MATCHERS:
        paths = [show(p) for p in files.paths if rx.search(files.text[p])]
        if paths:
            extra.append(f"{shape}: {len(paths)} file(s): {', '.join(paths)}")
    if extra:
        return Result("key-shapes", FAIL, f"{len(extra)} shape(s) found", tuple(extra))
    return Result("key-shapes", PASS, f"0 files match any of {len(KEY_SHAPES)} shapes")


def check_excluded_paths(
    files: CutFiles, dropped: Sequence[str], show: Callable[[str], str] = _as_is
) -> Result:
    present = [p for p in files.paths if is_excluded(p)]
    extra = [f"in cut: {show(p)}" for p in present]
    extra += [f"dropped by cut: {show(p)}" for p in dropped]
    if extra:
        return Result(
            "excluded-paths",
            FAIL,
            f"{len(present)} in the cut, {len(dropped)} dropped from the source rev; "
            "remove them from the source branch",
            tuple(extra),
        )
    return Result(
        "excluded-paths",
        PASS,
        "0 paths match " + " ".join(EXCLUDED_PATTERNS) + " (any segment, any case)",
    )


def check_tracker_links(
    files: CutFiles,
    host_ref: str | None,
    why: str | None = None,
    show: Callable[[str], str] = _as_is,
) -> Result:
    name = "tracker-links"
    if not host_ref and why:
        return Result(name, UNMEASURABLE, f"no owner: {why}")
    if not host_ref or "/" not in host_ref:
        return Result(name, UNMEASURABLE, "no owner: pass --host-ref or --project")
    owner = host_ref.split("/", 1)[0]
    rx = re.compile(
        r"github\.com/" + re.escape(owner) + r"/[^/\s)\]>]+/(?:issues|pull)/[0-9]", re.IGNORECASE
    )
    found = _hits(rx, files, include_binary=True, show=show)
    if found:
        return Result(
            name, FAIL, f"{len(found)} line(s) link an issue or PR under the owner", tuple(found)
        )
    return Result(name, PASS, "0 links to an issue or PR under the owner")


def check_runbook(files: CutFiles) -> Result:
    if RUNBOOK_PATH in files.paths:
        return Result("runbook-present", PASS, RUNBOOK_PATH)
    return Result("runbook-present", FAIL, f"{RUNBOOK_PATH} is not in the cut")


def _ident_ok(header: str) -> bool:
    """True when a raw ``author``/``committer`` header value is the identity ``build_cut``
    stamps: :data:`AUTHOR_NAME` plus an address :data:`NOREPLY_EMAIL_RE` accepts."""
    m = re.match(r"^(.*) <([^<>]*)> [0-9]+ [+-][0-9]{4}$", header)
    return bool(m) and m.group(1) == AUTHOR_NAME and bool(NOREPLY_EMAIL_RE.match(m.group(2)))


def _shown_key(key: str) -> str:
    """A config key with its subsection masked: ``url.<base>.insteadof`` names a URL and
    ``credential.<url>.helper`` may too, and a finding never prints one."""
    section, _, rest = key.partition(".")
    sub, dot, var = rest.rpartition(".")
    return f"{section}.*.{var}" if dot else key


def check_cut_shape(tree: Path) -> Result:
    """The repo at ``tree`` is still what ``build_cut`` made: one parentless commit, stamped by
    the cut identity, on :data:`CUT_BRANCH` alone, no hook file, and a repo config holding
    only :data:`ALLOWED_CONFIG_KEYS`. The content checks read only HEAD's tree, so without this
    a cut somebody committed to again, or added a remote to, passes them. Findings name what
    is wrong, never the identity, remote, URL or include path found."""
    name = "cut-shape"
    try:
        count = _git(tree, "rev-list", "--all", "--count").stdout.decode().strip()
        raw = _git(tree, "cat-file", "commit", "HEAD").stdout.decode("utf-8", "replace")
        refs = _git(tree, "for-each-ref", "--format=%(refname)").stdout.decode().split()
        # Every key git will obey in this repo, with the file it came from. `--includes` lists
        # an included file's keys under the scope of the file that included them, and the
        # `worktree` scope is `config.worktree`, which `extensions.worktreeConfig` switches on:
        # a narrower read (`--local`, or one named key) missed both (gh#728). A git without
        # `--show-scope` (older than 2.26) fails here, which is "cannot measure", never a pass.
        cfg = _git(tree, "config", "--list", "--show-scope", "--name-only", "--includes")
        # The common dir, not the git dir: hooks are read from there. core.hooksPath is refused
        # below, so `<common dir>/hooks` is the only place git will look for one.
        common = _git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except GitFailed as exc:
        return Result(name, UNMEASURABLE, f"cannot read the cut: {exc}")
    # System, global and command-line scope belong to the machine running the check, not to
    # the cut; only the repo's own files travel with the directory.
    keys: list[tuple[str, str]] = []
    for line in cfg.stdout.decode("utf-8", "replace").splitlines():
        scope, tab, key = line.partition("\t")
        if not tab:
            return Result(name, UNMEASURABLE, f"cannot parse git config's scope output: {line!r}")
        if scope in ("local", "worktree"):
            keys.append((scope, key))
    hooks_dir = Path(common.stdout.decode("utf-8", "replace").strip()) / "hooks"
    try:
        # git runs only `hooks/<hook name>`, so the `*.sample` files its default template
        # ships are inert under any name git looks for. They are allowed: a re-run `git init`
        # restores them, and refusing an inert file would send the publisher to rebuild a cut
        # nothing is wrong with. Anything else counts, whatever its mode, because Git for
        # Windows runs a hook with no executable bit.
        hooks = [
            p.name
            for p in (hooks_dir.iterdir() if hooks_dir.is_dir() else ())
            if not p.name.endswith(".sample")
        ]
    except OSError as exc:
        return Result(name, UNMEASURABLE, f"cannot list the cut's hooks: {type(exc).__name__}")
    headers = raw.partition("\n\n")[0].splitlines()
    fields: dict[str, list[str]] = {}
    for line in headers:
        key, _, value = line.partition(" ")
        fields.setdefault(key, []).append(value)
    # Every key outside the allow-list is refused. The classes below only give the commonest
    # refusals a message of their own; each key lands in exactly one, so no count repeats one.
    # git prints the canonical key, whose section and variable are already lower case. The
    # subsection (a remote name, a URL base) may itself hold dots, so split off both ends.
    remotes: dict[str, set[str]] = {}  # remote name -> scopes
    rewrites: dict[str, set[str]] = {}  # rewritten URL base -> scopes
    includes: list[str] = []  # scope of each include.* / includeIf.* key
    hooks_path: list[str] = []  # scope of each core.hooksPath
    other: list[str] = []  # "<scope> <masked key>" for everything else refused
    for scope, key in keys:
        if key in ALLOWED_CONFIG_KEYS:
            continue
        section, _, rest = key.partition(".")
        sub, dot, var = rest.rpartition(".")
        if section == "remote" and dot:
            remotes.setdefault(sub, set()).add(scope)
        elif section == "url" and dot and var in ("insteadof", "pushinsteadof"):
            # rewrites the URL step (7)'s push by URL goes to
            rewrites.setdefault(sub, set()).add(scope)
        elif section in ("include", "includeif"):
            # the included file's own keys are listed, and refused, under this scope as well
            includes.append(scope)
        elif key == "core.hookspath":
            hooks_path.append(scope)
        else:
            other.append(f"{scope} {_shown_key(key)}")

    def where(scopes: Iterable[str]) -> str:
        return f" ({', '.join(sorted(set(scopes)))} config)"

    problems = []
    if count != "1":
        problems.append(f"{count} commits reachable, want 1")
    if fields.get("parent"):
        problems.append(f"HEAD has {len(fields['parent'])} parent(s), want none")
    for role in ("author", "committer"):
        values = fields.get(role, [])
        if len(values) != 1 or not _ident_ok(values[0]):
            problems.append(f"{role} is not {AUTHOR_NAME} with a noreply address")
    if remotes:
        scopes = [s for v in remotes.values() for s in v]
        problems.append(f"{len(remotes)} remote(s) configured, want none{where(scopes)}")
    if rewrites:
        scopes = [s for v in rewrites.values() for s in v]
        problems.append(
            f"{len(rewrites)} URL rewrite(s) (insteadOf) configured, want none{where(scopes)}"
        )
    if includes:
        problems.append(
            f"{len(includes)} config include(s) (include.* / includeIf.*) configured, "
            f"want none{where(includes)}"
        )
    if hooks_path:
        problems.append(f"core.hooksPath is set, want unset{where(hooks_path)}")
    if other:
        problems.append(
            f"{len(other)} config key(s) outside the allow-list: {', '.join(sorted(other))}"
        )
    if hooks:
        problems.append(f"{len(hooks)} hook file(s) besides git's inert *.sample files, want none")
    extra_refs = [r for r in refs if r != f"refs/heads/{CUT_BRANCH}"]
    if extra_refs:
        problems.append(f"{len(extra_refs)} ref(s) besides refs/heads/{CUT_BRANCH}, want none")
    if f"refs/heads/{CUT_BRANCH}" not in refs:
        problems.append(f"no refs/heads/{CUT_BRANCH}")
    if problems:
        return Result(name, FAIL, f"{len(problems)} problem(s); rebuild the cut", tuple(problems))
    return Result(
        name,
        PASS,
        f"1 parentless commit by {AUTHOR_NAME}, refs/heads/{CUT_BRANCH} only, "
        "only allow-listed config keys, no hook",
    )


def report_sha_citations(
    files: CutFiles,
    source: Path | None,
    why: str | None = None,
    show: Callable[[str], str] = _as_is,
) -> Result:
    name = "sha-citations"
    if source is None:
        return Result(
            name, REPORT, f"not measured: {why or 'no --source and no --project checkout'}"
        )
    sites: list[tuple[str, int, str]] = []
    for path in files.paths:
        if path in files.binary:
            continue
        for n, line in enumerate(files.text[path].splitlines(), 1):
            for tok in _HEX_TOKEN_RE.findall(line):
                sites.append((path, n, tok))
    # A line that names the same sha twice is one citation to review, not two.
    sites = list(dict.fromkeys(sites))
    tokens = list(dict.fromkeys(tok for _, _, tok in sites))
    commits: set[str] = set()
    if tokens:
        try:
            out = _git(
                source,
                "cat-file",
                "--batch-check=%(objecttype)",
                stdin=("\n".join(tokens) + "\n").encode(),
            ).stdout
        except GitFailed as exc:
            return Result(name, REPORT, f"not measured: {exc}")
        answers = out.decode("utf-8", "replace").splitlines()
        # A token that does not resolve prints `<token> missing` / `<token> ambiguous`, one line
        # per input either way, so the answers line up with the tokens.
        for tok, ans in zip(tokens, answers):
            if ans.strip() == "commit":
                commits.add(tok)
    cited = [f"{show(p)}:{n} {t}" for p, n, t in sites if t in commits]
    nfiles = len({p for p, _, t in sites if t in commits})
    return Result(
        name,
        REPORT,
        f"{len(cited)} in {nfiles} file(s) (never changes the exit code)",
        tuple(cited),
    )


def report_tracker_refs(files: CutFiles) -> Result:
    n = sum(len(_GH_REF_RE.findall(files.text[p])) for p in files.paths if p not in files.binary)
    return Result("tracker-refs", REPORT, f"{n} plain gh#N references")


def report_install_evidence(
    files: CutFiles, tree: Path, source: Path | None, why: str | None = None
) -> Result:
    """How far the cut has moved from the tree the Linux install was last verified on.

    A REPORT, not a hard check: the drill README's cut-time rule also needs every
    :data:`INSTALL_DOC` hunk inside the Last-verified block, which only a person reading the
    diff can judge. The verified tree is read from ``source`` because a single-commit cut
    holds no older tree; the two sides are compared entry by entry, so the cut tree need not
    exist in ``source`` either."""
    name = "install-evidence"
    text = files.text.get(INSTALL_DOC)
    if text is None:
        return Result(name, REPORT, f"not measured: {INSTALL_DOC} is not in the cut")
    m = _LAST_VERIFIED_RE.search(text)
    if not m:
        return Result(
            name,
            REPORT,
            f"not measured: {INSTALL_DOC}'s Last-verified line cites no tree id; "
            "re-run the Linux drill before step (1)",
        )
    verified = m.group(1)
    if source is None:
        return Result(
            name, REPORT, f"not measured: {why or 'no --source and no --project checkout'}"
        )
    try:
        old = {e.path: (e.mode, e.oid) for e in _ls_tree(source, verified)}
        new = {e.path: (e.mode, e.oid) for e in _ls_tree(tree, "HEAD")}
    except GitFailed:
        return Result(
            name,
            REPORT,
            f"not measured: verified tree {verified} is not in the source checkout; "
            "fetch it or re-run the Linux drill",
        )
    differ = sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))
    others = [p for p in differ if p != INSTALL_DOC]
    return Result(
        name,
        REPORT,
        f"{len(others)} file(s) besides {INSTALL_DOC} differ from verified tree {verified} "
        "(never changes the exit code)",
    )


def _load_cut(tree: Path) -> CutFiles:
    if not tree.is_dir():
        raise PublishUnmeasurable(f"{tree} is not a directory")
    top = _git(tree, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        raise PublishUnmeasurable(f"{tree} is not a git repository")
    top_path = Path(top.stdout.decode("utf-8", "replace").strip())
    try:
        same = os.path.samefile(top_path, tree)
    except OSError:
        same = False
    if not same:
        raise PublishUnmeasurable(
            f"{tree} is inside a git repository but is not its top level; the gate reads the "
            "cut's own commit, so it must be pointed at the cut's root"
        )
    head = _git(tree, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    if head.returncode != 0:
        raise PublishUnmeasurable(f"{tree} has no HEAD commit")
    entries = _ls_tree(tree, "HEAD")
    if not entries:
        raise PublishUnmeasurable(f"{tree}'s HEAD has zero files; an empty cut is not a clean one")
    blobs = _read_blobs(tree, [e.oid for e in entries if e.kind == "blob"])
    text: dict[str, str] = {}
    binary: set[str] = set()
    for e in entries:
        data = blobs.get(e.oid, b"")
        if _is_binary(data):
            binary.add(e.path)
        text[e.path] = data.decode("utf-8", "replace")
    raw = _git(tree, "cat-file", "commit", "HEAD").stdout.decode("utf-8", "replace")
    message = raw.partition("\n\n")[2]
    return CutFiles([e.path for e in entries], text, binary, message)


# --------------------------------------------------------------------------- #
# Config resolution (lazy: tests pass every override, the real run passes --project)
# --------------------------------------------------------------------------- #
#: The three fields a project supplies, as (Namespace attribute, flag).
_ROUTING_FLAGS = (("ledger", "--ledger"), ("source", "--source"), ("host_ref", "--host-ref"))


@dataclass
class Resolved:
    ledger: Path | None
    host_ref: str | None
    source: Path | None
    notes: list[str]
    #: Why a field is still None although a project was named: the config error that stopped
    #: it, keyed ``ledger`` / ``host_ref`` / ``source`` / ``sensitive_terms``. Empty when
    #: nothing failed to load, so a direct :func:`run_check` caller's "pass --<flag> or
    #: --project" messages stand.
    why: dict[str, str] = field(default_factory=dict)
    #: The local-only sensitive-terms list: ``--sensitive-terms``, else derived from the data
    #: root when a project is named. None with no project and no flag (``not configured``).
    sensitive_terms: Path | None = None
    #: The local-only extra sanitize pattern: ``--sanitize-extra-pattern``, else derived from the
    #: data root beside the sensitive-terms list. None with no project and no flag.
    sanitize_extra: Path | None = None


def _resolved_line(a: argparse.Namespace, r: Resolved) -> str:
    """The first line of output: what this run resolved, so the wrong project is on screen."""

    def shown(value: object) -> str:
        return "unresolved" if value is None else str(value)

    if not a.project:
        project = "(none: explicit flags)"
    elif (
        all(getattr(a, attr) for attr, _ in _ROUTING_FLAGS)
        and a.sensitive_terms
        and a.sanitize_extra
    ):
        project = f"{a.project} (unused: all flags explicit)"
    else:
        project = a.project
    return (
        f"resolved project {project}; ledger {shown(r.ledger)}; "
        f"source {shown(r.source)}; host-ref {shown(r.host_ref)}"
    )


def _resolve(a: argparse.Namespace) -> Resolved:
    """The ledger, owner and source for this run, from explicit flags and ``--project``.

    Never from the session default (``$AGENTFLOW_PROJECT``): an all-PASS run built from
    another project's ledger and checkout looks exactly like a correct one, so a run that
    names neither a project nor all three fields is refused (gh#729)."""
    missing = [flag for attr, flag in _ROUTING_FLAGS if not getattr(a, attr)]
    if missing and not a.project:
        raise CutRefused(
            f"no project named and {', '.join(missing)} not given; pass --project <name>, or "
            "all of --ledger, --source and --host-ref (the session default project is never "
            "used, so the gate cannot quietly run against the wrong one)"
        )
    notes: list[str] = []
    why: dict[str, str] = {}
    ledger = Path(a.ledger) if a.ledger else None
    host_ref = a.host_ref
    source = Path(a.source) if a.source else None
    terms = Path(a.sensitive_terms) if a.sensitive_terms else None
    extra = Path(a.sanitize_extra) if a.sanitize_extra else None
    # A run with all three routing flags and no project has no data root to find the terms
    # list or the extra pattern in; their checks report that as "not configured".
    if a.project and (
        ledger is None or host_ref is None or source is None or terms is None or extra is None
    ):
        from agentflow import config

        name = a.project
        # The project file alone gives host_ref and the source checkout; the environment is
        # read only for the data-root paths. Loading both under one try let an environment
        # error that publishing never touches (an unset notifier env var) blank all three
        # (gh#657).
        try:
            proj = config.load_project(name)
        except (config.ConfigError, OSError) as exc:
            reason = f"project {name!r} did not load: {config.error_line(exc)}"
            notes.append(f"config not resolved ({reason})")
            for key, value in (
                ("ledger", ledger),
                ("host_ref", host_ref),
                ("source", source),
                ("sensitive_terms", terms),
                ("sanitize_extra", extra),
            ):
                if value is None:
                    why[key] = reason
            return Resolved(ledger, host_ref, source, notes, why, terms, extra)
        if host_ref is None:
            host_ref = proj.canonical_repo.host_ref
        if source is None:
            source = Path(proj.canonical_repo.local_checkout).expanduser()
        if ledger is None or terms is None or extra is None:
            try:
                env = config.load_environment()
            except (config.ConfigError, OSError) as exc:
                reason = f"the environment config did not load: {config.error_line(exc)}"
                for key, value, what in (
                    ("ledger", ledger, "ledger path"),
                    ("sensitive_terms", terms, "sensitive-terms path"),
                    ("sanitize_extra", extra, "sanitize extra pattern path"),
                ):
                    if value is None:
                        notes.append(f"{what} not resolved ({reason})")
                        why[key] = reason
            else:
                project_dir = Path(env.data_root) / "projects" / name
                if ledger is None:
                    ledger = project_dir / "status" / LEDGER_FILENAME
                if terms is None:
                    terms = project_dir / SENSITIVE_TERMS_RELPATH
                if extra is None:
                    extra = project_extra_path(env.data_root, name)
    return Resolved(ledger, host_ref, source, notes, why, terms, extra)


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def run_check(
    tree: Path,
    *,
    ledger: Path | None,
    host_ref: str | None,
    source: Path | None,
    ci_yml: Path | None = None,
    dropped: Sequence[str] = (),
    why: Mapping[str, str] | None = None,
    sensitive_terms: Path | None = None,
    sanitize_extra: Path | None = None,
) -> tuple[int, list[str]]:
    """Run every check over the cut at ``tree``. Returns ``(exit code, output lines)``.

    ``why`` is :attr:`Resolved.why`: the config error behind a ``None`` field, which the
    check that needed the field names instead of asking for a flag. ``sensitive_terms`` is
    the local-only term list (:func:`check_sensitive_terms`) and ``sanitize_extra`` the
    local-only extra sanitize pattern (:func:`check_sanitize_terms`); None is
    ``not configured``."""
    why = why or {}
    files = _load_cut(tree)
    ci = ci_yml if ci_yml is not None else tree / ".github" / "workflows" / "ci.yml"
    # Read once, before any check prints: every check prints its paths through the same
    # display, so a path holding a sensitive term, or one the extra pattern matches, reaches
    # no line of the output.
    loaded = _load_sensitive_terms(sensitive_terms, why.get("sensitive_terms"))
    extra = _load_extra_pattern(sanitize_extra, why.get("sanitize_extra"))
    extra_pattern = extra[0]
    extra_rx = (
        re.compile(extra_pattern.pattern, re.IGNORECASE)
        if extra_pattern is not None and extra_pattern.configured
        else None
    )
    show = _path_display(files, loaded[0], dropped, extra_rx)
    hard = [
        check_cut_shape(tree),
        check_ledger(ledger, why.get("ledger")),
        check_session_urls(files, show),
        check_sanitize_terms(files, ci, show, extra=extra),
        check_sensitive_terms(files, sensitive_terms, loaded=loaded, show=show),
        check_key_shapes(files, show),
        check_excluded_paths(files, dropped, show),
        check_tracker_links(files, host_ref, why.get("host_ref"), show),
        check_runbook(files),
    ]
    reports = [
        report_sha_citations(files, source, why.get("source"), show),
        report_tracker_refs(files),
        report_install_evidence(files, tree, source, why.get("source")),
    ]
    lines = [ln for r in hard + reports for ln in r.lines()]
    if any(r.status == UNMEASURABLE for r in hard):
        return 2, lines
    if any(r.status == FAIL for r in hard):
        return 1, lines
    return 0, lines


def _cmd_check(a: argparse.Namespace) -> int:
    r = _resolve(a)
    # First, before any note: the notes explain this line, and stderr is unbuffered.
    print(_resolved_line(a, r), flush=True)
    for note in r.notes:
        print(f"note: {note}", file=sys.stderr)
    code, lines = run_check(
        Path(a.tree),
        ledger=r.ledger,
        host_ref=r.host_ref,
        source=r.source,
        ci_yml=Path(a.ci_yml) if a.ci_yml else None,
        why=r.why,
        sensitive_terms=r.sensitive_terms,
        sanitize_extra=r.sanitize_extra,
    )
    print("\n".join(lines))
    return code


# --------------------------------------------------------------------------- #
# cut
# --------------------------------------------------------------------------- #
def _hermetic_env(tmp: Path, identity_email: str | None = None) -> dict[str, str]:
    """Env for every call into ``--out``: no system config, an empty global config."""
    empty = tmp / "empty-global-config"
    empty.write_bytes(b"")
    env = _base_env()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(empty)
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
        "EMAIL",
        # Command-line-scope config outranks the local config the cut sets, and a template dir
        # would copy the operator's hooks into <out>/.git.
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_TEMPLATE_DIR",
    ):
        env.pop(key, None)
    for key in [k for k in env if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]:
        env.pop(key)
    if identity_email is not None:
        env["GIT_AUTHOR_NAME"] = AUTHOR_NAME
        env["GIT_AUTHOR_EMAIL"] = identity_email
        env["GIT_COMMITTER_NAME"] = AUTHOR_NAME
        env["GIT_COMMITTER_EMAIL"] = identity_email
    return env


def build_cut(source: Path, rev: str, out: Path, author_email: str) -> dict:
    """Build the single-commit repo at ``out``. Returns what it built. Writes nothing outside
    ``out`` (bar a temp dir holding one empty config file) and never touches the source's refs."""
    if not NOREPLY_EMAIL_RE.match(author_email):
        raise CutRefused(
            "--author-email must be a GitHub noreply address "
            "(<digits>+<user>@users.noreply.github.com or <user>@users.noreply.github.com)"
        )
    if rev.startswith("-"):
        raise CutRefused(f"--rev {rev!r} looks like an option")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise CutRefused(f"{out} exists and is not an empty directory; the cut never overwrites")
    if not source.is_dir():
        raise CutRefused(f"source {source} is not a directory")
    probe = _git(source, "rev-parse", "--git-dir", check=False)
    if probe.returncode != 0:
        raise CutRefused(f"source {source} is not a git repository")
    resolved = _git(source, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}", check=False)
    if resolved.returncode != 0:
        raise CutRefused(f"--rev {rev!r} does not resolve to a commit in {source}")
    source_sha = resolved.stdout.decode("ascii").strip()
    source_tree = _git(source, "rev-parse", f"{source_sha}^{{tree}}").stdout.decode().strip()

    entries = _ls_tree(source, source_tree)
    gitlinks = [e.path for e in entries if e.kind != "blob"]
    if gitlinks:
        raise CutRefused(
            f"the source tree has {len(gitlinks)} submodule entr(ies) ({', '.join(gitlinks)}); "
            "a cut cannot carry a pointer to a commit the share does not hold"
        )
    kept = [e for e in entries if not is_excluded(e.path)]
    dropped = [e.path for e in entries if is_excluded(e.path)]

    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentflow-cut-") as tmp_name:
        tmp = Path(tmp_name)
        env = _hermetic_env(tmp)
        empty_template = tmp / "empty-template"
        empty_template.mkdir()
        _git(
            out,
            "init",
            "-q",
            f"--initial-branch={CUT_BRANCH}",
            f"--template={empty_template}",
            env=env,
        )
        _git(out, "config", "core.autocrlf", "false", env=env)
        _git(out, "config", "core.safecrlf", "false", env=env)
        # A reflog entry is stamped with the ambient identity (OS account and hostname), not
        # the cut's author, so the refs are written with no reflog at all.
        _git(out, "config", "core.logAllRefUpdates", "false", env=env)

        blob_ids = list(dict.fromkeys(e.oid for e in kept))
        if blob_ids:
            pack = _git(
                source,
                "pack-objects",
                "--stdout",
                "-q",
                stdin=("\n".join(blob_ids) + "\n").encode(),
            ).stdout
            _git(out, "unpack-objects", "-q", env=env, stdin=pack)
        info = b"".join(
            f"{e.mode} {e.oid}\t".encode() + e.path.encode("utf-8", "surrogateescape") + b"\0"
            for e in kept
        )
        _git(out, "update-index", "-z", "--index-info", env=env, stdin=info)
        tree_id = _git(out, "write-tree", env=env).stdout.decode().strip()
        # Git for Windows prints "Ignoring path" and exits 0 for a name NTFS cannot hold, so
        # the tree can be short of what was fed in while every call succeeded.
        written = _git(out, "ls-tree", "-r", "-z", "--name-only", tree_id, env=env).stdout
        written_paths = {p.decode("utf-8", "surrogateescape") for p in written.split(b"\0") if p}
        if len(written_paths) != len(kept):
            missing = sorted({e.path for e in kept} - written_paths)
            raise GitFailed(
                f"the cut's tree holds {len(written_paths)} files, {len(kept)} were kept; not "
                f"written: {', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}"
            )
        commit_id = (
            _git(
                out,
                "commit-tree",
                tree_id,
                "-F",
                "-",
                env=_hermetic_env(tmp, author_email),
                stdin=COMMIT_MESSAGE.encode(),
            )
            .stdout.decode()
            .strip()
        )
        _git(out, "update-ref", f"refs/heads/{CUT_BRANCH}", commit_id, env=env)
        _git(out, "symbolic-ref", "HEAD", f"refs/heads/{CUT_BRANCH}", env=env)
        _git(out, "checkout-index", "-a", "-f", env=env)
        _git(out, "update-index", "-q", "--refresh", env=env, check=False)
        dirty = _git(out, "status", "--porcelain", env=env).stdout
        leaked = _git(out, "cat-file", "-e", source_sha, env=env, check=False).returncode == 0

    if leaked:
        raise GitFailed(
            f"the source commit {source_sha} is in {out}'s object store; do not share it"
        )
    if dirty.strip():
        raise PublishUnmeasurable(f"{out} checked out with a dirty working tree")
    if not _SHA_RE.match(tree_id) or not _SHA_RE.match(commit_id):
        raise GitFailed("git returned a malformed tree or commit id")
    return {
        "source": source_sha,
        "source_tree": source_tree,
        "tree": tree_id,
        "commit": commit_id,
        "files": len(kept),
        "dropped": dropped,
    }


def _cmd_cut(a: argparse.Namespace) -> int:
    r = _resolve(a)
    # First, before any note: the notes explain this line, and stderr is unbuffered.
    print(_resolved_line(a, r), flush=True)
    for note in r.notes:
        print(f"note: {note}", file=sys.stderr)
    if r.source is None and "source" in r.why:
        raise CutRefused(f"no source checkout: {r.why['source']}")
    if r.source is None:
        raise CutRefused(
            "no source checkout: pass --source or a --project whose "
            "canonical_repo.local_checkout exists"
        )
    out = Path(a.out)
    built = build_cut(r.source, a.rev, out, a.author_email)
    print(f"source {built['source']}")
    print(f"tree {built['tree']}")
    print(f"commit {built['commit']}")
    print(f"files {built['files']}")
    code, lines = run_check(
        out,
        ledger=r.ledger,
        host_ref=r.host_ref,
        source=r.source,
        ci_yml=Path(a.ci_yml) if a.ci_yml else None,
        dropped=built["dropped"],
        why=r.why,
        sensitive_terms=r.sensitive_terms,
        sanitize_extra=r.sanitize_extra,
    )
    print("\n".join(lines))
    return code


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument(
        "--project",
        help="project routing key (ledger, owner and source from config); required unless "
        "--ledger, --source and --host-ref are all given. The session default is never used",
    )
    sp.add_argument("--ledger", help="explicit key-rotation ledger path (default: from config)")
    sp.add_argument(
        "--sensitive-terms",
        dest="sensitive_terms",
        help="explicit local-only sensitive-terms list (default: "
        "<data_root>/projects/<project>/publish/sensitive-terms.txt; absent = not configured)",
    )
    sp.add_argument(
        "--sanitize-extra-pattern",
        dest="sanitize_extra",
        help="explicit local-only extra sanitize pattern file, unioned with ci.yml's pattern "
        "(default: <data_root>/projects/<project>/publish/sanitize-extra-pattern.txt; "
        "absent = not configured)",
    )
    sp.add_argument(
        "--ci-yml",
        dest="ci_yml",
        help="explicit ci.yml for the sanitize pattern (default: the cut's own)",
    )
    sp.add_argument(
        "--source",
        help="working checkout that shas resolve against (default: canonical_repo.local_checkout)",
    )
    sp.add_argument(
        "--host-ref", dest="host_ref", help="<owner>/<repo> (default: canonical_repo.host_ref)"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m agentflow.publish")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="readiness gate over a built cut")
    c.add_argument("--tree", required=True, help="the cut directory")
    _common(c)
    k = sub.add_parser("cut", help="build a local single-commit cut, then check it")
    k.add_argument("--out", required=True, help="new or empty directory to build the cut in")
    k.add_argument(
        "--author-email",
        dest="author_email",
        required=True,
        help="GitHub noreply address for the cut's author and committer",
    )
    k.add_argument("--rev", default="origin/master", help="source rev (default: origin/master)")
    _common(k)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Exit codes: 0 every hard check passed; 1 a hard check failed; 2 could not measure, or
    the cut refused its inputs. Never 0 when anything could not be measured."""
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    a = build_parser().parse_args(None if argv is None else list(argv))
    try:
        return _cmd_check(a) if a.cmd == "check" else _cmd_cut(a)
    except CutRefused as exc:
        print(f"publish {a.cmd}: REFUSED - {exc}", file=sys.stderr)
        return 2
    except PublishUnmeasurable as exc:
        print(f"publish {a.cmd}: CANNOT MEASURE - {exc}", file=sys.stderr)
        return 2
    except (GitFailed, OSError) as exc:
        print(f"publish {a.cmd}: FAILED - {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
