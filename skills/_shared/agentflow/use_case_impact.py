"""Advisory use-case impact / touchpoint tool (Phase 4 T10).

``uc-touchpoints UC-0147`` prints the touchpoint *floor* for a single opaque
UC-id, grouped by SDLC stage:

    refine   — the functional story block(s) in the project's refine docs
    solution — the doc-of-record + task-frontmatter threads in solution docs
    commits  — ``Use-Case: UC-0147`` git trailers in the project code repo
    code     — ``git grep`` hits for the id inside the code repo
    flow     — the flow pages that host the id under ``autodoc_root``

It reads the registry birth-cert row for the title/epic header. It is a
*floor*, not a ceiling: a missing commit trailer just means that hop won't
show — the tool never claims completeness. It is strictly ADVISORY:

    - NEVER edits any file.
    - NEVER gates / blocks / raises to the caller.
    - ALWAYS exits 0, even when config is unresolvable or git is absent.

Scanning is plain-Python (portable, no ``grep`` binary dependency); git hops
shell out to ``git`` and degrade to empty on any failure. The opaque id is
matched literally, so it is inert to graph-check (no wikilinks involved).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Stages in canonical print order (the spine's hop order).
STAGES = ("refine", "solution", "commits", "code", "flow")

_UC_ID_RE = re.compile(r"^UC-\d{4,}$")
_GIT_TIMEOUT = 15  # seconds; a wedged git must never hang the advisory tool


@dataclass
class Touchpoint:
    """One located reference to the UC-id."""

    location: str  # file path (repo-relative where practical) or commit sha
    line: Optional[int]  # 1-indexed line number, or None for commit-level hits
    text: str  # the matching line / commit subject, stripped


@dataclass
class ImpactReport:
    """The touchpoint floor for one UC-id, grouped by stage."""

    uc_id: str
    registry_row: Optional[dict]  # {id, minted, epic, title, origin} or None
    touchpoints: dict = field(default_factory=lambda: {s: [] for s in STAGES})

    def total(self) -> int:
        return sum(len(v) for v in self.touchpoints.values())


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def is_valid_uc_id(uc_id: str) -> bool:
    """True iff ``uc_id`` is a well-formed opaque ``UC-NNNN`` id."""
    return bool(_UC_ID_RE.match(uc_id or ""))


# --------------------------------------------------------------------------- #
# Registry (birth-cert) lookup
# --------------------------------------------------------------------------- #
def read_registry_row(registry_path, uc_id: str) -> Optional[dict]:
    """Return the birth-cert row for ``uc_id`` from ``registry.jsonl``, or None.

    Tolerates a missing file and malformed lines — this is advisory; a broken
    row must never crash the tool.
    """
    path = Path(registry_path)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id") == uc_id:
            return row
    return None


# --------------------------------------------------------------------------- #
# Plain-Python "grep": scan a directory tree for literal id occurrences
# --------------------------------------------------------------------------- #
def _scan_tree(root, uc_id: str, *, glob: str = "**/*.md") -> list:
    """Literal-substring scan of ``root`` for ``uc_id``; returns Touchpoints.

    Portable stand-in for ``grep -rn`` — no external binary. Silently skips
    unreadable files (advisory floor, never fatal).
    """
    hits: list = []
    base = Path(root)
    if not base.is_dir():
        return hits
    for f in sorted(base.glob(glob)):
        if not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            if uc_id in line:
                try:
                    loc = str(f.relative_to(base))
                except ValueError:
                    loc = str(f)
                hits.append(Touchpoint(location=loc, line=i, text=line.strip()))
    return hits


# --------------------------------------------------------------------------- #
# git hops — best-effort, degrade to [] on any failure
# --------------------------------------------------------------------------- #
def _run_git(repo, args: list) -> Optional[str]:
    """Run ``git -C <repo> <args>``; return stdout or None on any failure."""
    if repo is None:
        return None
    repo_path = Path(repo)
    if not repo_path.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        # git grep exits 1 on "no matches" — that's not an error, just empty.
        return proc.stdout
    return proc.stdout


def _git_log_trailers(repo, uc_id: str) -> list:
    """Commits carrying a ``Use-Case: <id>`` trailer, newest first."""
    out = _run_git(
        repo,
        [
            "log",
            f"--grep=Use-Case: {uc_id}",
            "--pretty=format:%h\t%s",
        ],
    )
    hits: list = []
    if not out:
        return hits
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        sha, _, subject = line.partition("\t")
        hits.append(Touchpoint(location=sha, line=None, text=subject.strip()))
    return hits


def _git_grep(repo, uc_id: str) -> list:
    """``git grep -n <id>`` hits inside tracked code (excludes commit history)."""
    out = _run_git(repo, ["grep", "-n", "--no-color", uc_id])
    hits: list = []
    if not out:
        return hits
    for line in out.splitlines():
        # format: path:lineno:matchtext
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path, lineno, text = parts
        try:
            ln: Optional[int] = int(lineno)
        except ValueError:
            ln = None
        hits.append(Touchpoint(location=path, line=ln, text=text.strip()))
    return hits


# --------------------------------------------------------------------------- #
# Assemble the report
# --------------------------------------------------------------------------- #
def find_touchpoints(
    uc_id: str,
    *,
    data_root=None,
    project_name: Optional[str] = None,
    code_repo=None,
    autodoc_root=None,
    registry_path=None,
) -> ImpactReport:
    """Build the touchpoint floor for ``uc_id`` across all resolvable roots.

    Every root is optional: an unresolved root simply yields an empty stage.
    The tool never raises for a caller — bad input yields an empty report.
    """
    report = ImpactReport(uc_id=uc_id, registry_row=None)

    if not is_valid_uc_id(uc_id):
        return report

    project_dir = None
    if data_root is not None and project_name:
        project_dir = Path(data_root) / "projects" / project_name

    # Registry row (title/epic header).
    reg = registry_path
    if reg is None and project_dir is not None:
        reg = project_dir / "design" / "use-cases" / "registry.jsonl"
    if reg is not None:
        report.registry_row = read_registry_row(reg, uc_id)

    # refine / solution — plain-Python grep under the project data dir.
    if project_dir is not None:
        report.touchpoints["refine"] = _scan_tree(
            project_dir / "design" / "specs", uc_id
        )
        report.touchpoints["solution"] = _scan_tree(
            project_dir / "design" / "plans", uc_id
        )

    # commits / code — git hops in the project code repo.
    report.touchpoints["commits"] = _git_log_trailers(code_repo, uc_id)
    report.touchpoints["code"] = _git_grep(code_repo, uc_id)

    # flow — plain-Python grep under autodoc flows.
    if autodoc_root is not None:
        report.touchpoints["flow"] = _scan_tree(
            Path(autodoc_root) / "flows", uc_id
        )

    return report


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(report: ImpactReport) -> str:
    """Human-readable touchpoint floor, grouped by stage."""
    lines: list = []
    row = report.registry_row
    if row:
        title = row.get("title", "")
        epic = row.get("epic", "")
        header = f"{report.uc_id} — {title}".rstrip(" —")
        lines.append(header)
        if epic:
            lines.append(f"epic: {epic}")
    else:
        lines.append(report.uc_id)
        lines.append("(no registry row found — id may be unminted)")
    lines.append(f"touchpoint floor: {report.total()} reference(s)")
    lines.append("")

    for stage in STAGES:
        hits = report.touchpoints.get(stage, [])
        lines.append(f"## {stage} ({len(hits)})")
        if not hits:
            lines.append("  (none)")
        for tp in hits:
            if tp.line is not None:
                lines.append(f"  {tp.location}:{tp.line}: {tp.text}")
            else:
                lines.append(f"  {tp.location}: {tp.text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Config-resolving CLI entrypoint — ALWAYS exits 0
# --------------------------------------------------------------------------- #
def _resolve_project_name(explicit, config_home) -> Optional[str]:
    """Explicit arg wins; else the sole project file under config; else None."""
    if explicit:
        return explicit
    projects_dir = Path(config_home) / "projects"
    if not projects_dir.is_dir():
        return None
    candidates = sorted(p.stem for p in projects_dir.glob("*.md"))
    if len(candidates) == 1:
        return candidates[0]
    return None


def main(argv: Optional[list] = None) -> int:
    """CLI: ``uc-touchpoints UC-0147 [project]``. Advisory — always returns 0."""
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    positional = [a for a in args if not a.startswith("-")]

    if not positional:
        print("usage: uc-touchpoints UC-NNNN [project]", file=sys.stderr)
        return 0  # advisory: never a nonzero gate

    uc_id = positional[0]
    explicit_project = positional[1] if len(positional) > 1 else None

    if not is_valid_uc_id(uc_id):
        print(f"{uc_id}: not a well-formed UC-id (expected UC-NNNN)", file=sys.stderr)
        return 0

    data_root = code_repo = autodoc_root = None
    autodoc_error = None
    project_name = explicit_project
    try:
        from . import config as _config

        env = _config.load_environment()
        data_root = env.data_root
        project_name = _resolve_project_name(
            explicit_project, _config._config_home()
        )
        proj = None
        if project_name:
            try:
                proj = _config.load_project(project_name)
                code_repo = proj.canonical_repo.local_checkout
            except Exception as e:  # noqa: BLE001 — advisory, degrade gracefully
                print(f"(project config unavailable: {e})", file=sys.stderr)
        # The PROJECT's root, resolved - never the bare environment field, which is one
        # machine-level path shared by every project on the box.
        try:
            autodoc_root = _config.resolve_autodoc_root(env, proj)
        except _config.AutodocRootError as e:
            # Captured here and re-raised BELOW, outside the degrade-gracefully arm.
            # Every other config problem here degrades to a partial report, which is
            # right; this one must not, because the report it would produce is a
            # confident zero from an empty root, and a zero is read downstream as
            # evidence that the code is missing.
            autodoc_error = e
    except Exception as e:  # noqa: BLE001 — advisory, degrade gracefully
        print(f"(environment config unavailable: {e})", file=sys.stderr)

    if autodoc_error is not None:
        raise autodoc_error

    report = find_touchpoints(
        uc_id,
        data_root=data_root,
        project_name=project_name,
        code_repo=code_repo,
        autodoc_root=autodoc_root,
    )
    print(render(report), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover — thin CLI shim
    raise SystemExit(main())
