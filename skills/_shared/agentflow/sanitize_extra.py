"""The local-only extra sanitize pattern: alternatives that must never ship (gh#821).

``ci.yml``'s sanitize gate ships in every cut, so the pattern written there carries only
GENERIC shapes - the mangled session-directory name and absolute home-directory paths - which
name nobody. An alternative specific to one install would be published by the very file meant
to keep it out, so it lives in a local-only file in the data root, beside the sensitive-terms
list (``agentflow.publish``, precondition 3 of ``docs/runbooks/publish.md``)::

    <data_root>/projects/<project>/publish/sanitize-extra-pattern.txt

FORMAT. One regular-expression alternative per line, valid for both Python ``re`` and
``grep -P`` because the same alternatives also run in CI. Surrounding whitespace is stripped;
a blank line, and a line whose first non-blank character is ``#``, is skipped. The alternatives
are OR-ed. The same text joined with ``|`` is what the private repository's Actions variable
``SANITIZE_EXTRA_PATTERN`` holds, so the PR-time gate applies it too.

THIS IS THE ONE READER. The cut's ``sanitize-terms`` check (:mod:`agentflow.publish`) and the
wiki ratchet's ``sanitize`` class (:mod:`agentflow.pillar_residue`) both resolve the file here
and union it with the ``ci.yml`` pattern through :func:`union`, so the two cannot come to
disagree about what the extra pattern is.

NOTHING HERE PRINTS THE PATTERN OR A LINE OF THE FILE. An error names the path, a line number
and an offset only: a run log is itself something that can be published, and an error message
that quoted the alternative it could not compile would publish exactly what the file exists to
keep private. That is also why the regex errors below are re-raised ``from None`` - a chained
``re.error`` carries the pattern it failed on into any traceback.

STDLIB ONLY. No network, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

__all__ = [
    "EXTRA_PATTERN_RELPATH",
    "ExtraPattern",
    "ExtraPatternUnreadable",
    "project_extra_path",
    "read_extra_pattern",
    "resolve_extra_path",
    "union",
]

#: The file inside ``<data_root>/projects/<project>/``, beside ``publish/sensitive-terms.txt``.
EXTRA_PATTERN_RELPATH = Path("publish") / "sanitize-extra-pattern.txt"

#: Where neither an explicit path nor a project was given. Both CLIs spell the flag the same.
NO_SOURCE = "no --sanitize-extra-pattern and no --project"


class ExtraPatternUnreadable(Exception):
    """The extra pattern exists, or was meant to, but cannot be used.

    Unreadable, not UTF-8, a line that does not compile, or a path the config could not
    resolve. Callers map it to "cannot measure", never to a pass: a check that silently fell
    back to the generic shapes would report a clean tree it never scanned for these terms.
    """


@dataclass(frozen=True)
class ExtraPattern:
    """The extra pattern, configured or not, and where it came from.

    ``pattern`` is the OR-ed alternatives, or None when nothing is configured. ``where`` says
    why nothing is (the path that does not exist, the file that lists none, or that no source
    was named), so a "not configured" line always says where it looked.
    """

    pattern: str | None
    path: Path | None
    where: str = ""
    alternatives: int = 0

    @property
    def configured(self) -> bool:
        return self.pattern is not None

    def sources(self) -> str:
        """What a sanitize scan applied, for its status line. Never the pattern itself."""
        if self.configured:
            return f"ci.yml pattern + extra pattern from {self.path}"
        return f"ci.yml pattern; extra pattern not configured: {self.where}"


def project_extra_path(data_root: str | Path, project: str) -> Path:
    """``<data_root>/projects/<project>/publish/sanitize-extra-pattern.txt``."""
    return Path(data_root) / "projects" / project / EXTRA_PATTERN_RELPATH


def resolve_extra_path(project: str) -> Path:
    """The project's extra-pattern path, from the environment config's data root.

    The same derivation ``agentflow.publish`` uses for the sensitive-terms list. Raises
    ``ValueError`` for a project key that is not a plain slug (it becomes a path segment), and
    whatever ``config.load_environment`` raises when the environment will not load; the caller
    owns turning either into "cannot measure".
    """
    from agentflow import config
    from agentflow.registry import _PROJECT_RE

    if not _PROJECT_RE.match(project):
        raise ValueError(
            f"project {project!r} must match [A-Za-z0-9_-]+ (no path separators or '..')"
        )
    return project_extra_path(config.load_environment().data_root, project)


def read_extra_pattern(path: Path | None, *, why: str | None = None) -> ExtraPattern:
    """Read the extra pattern at ``path``.

    * ``path`` None and ``why`` given: the path could not be resolved (a config that did not
      load) - :class:`ExtraPatternUnreadable`, because nothing was looked at.
    * ``path`` None otherwise: not configured, no source named.
    * a path that does not exist, or a file with no alternatives: not configured, saying so.
    * unreadable, not UTF-8, a line that does not compile, or alternatives that do not compile
      once OR-ed: :class:`ExtraPatternUnreadable`.
    """
    if path is None:
        if why:
            raise ExtraPatternUnreadable(f"no extra pattern path: {why}")
        return ExtraPattern(None, None, NO_SOURCE)
    if not path.exists():
        return ExtraPattern(None, path, f"{path} does not exist")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ExtraPatternUnreadable(f"cannot read {path}: {type(exc).__name__}") from None
    except UnicodeDecodeError:
        raise ExtraPatternUnreadable(f"{path} is not UTF-8") from None
    alternatives: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        alt = raw.strip()
        if not alt or alt.startswith("#"):
            continue
        try:
            re.compile(alt)
        except re.error as exc:
            raise ExtraPatternUnreadable(
                f"{path} line {n} is not a valid regular expression (error at offset {exc.pos})"
            ) from None
        alternatives.append(alt)
    if not alternatives:
        return ExtraPattern(None, path, f"{path} lists 0 alternatives")
    pattern = "|".join(alternatives)
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ExtraPatternUnreadable(
            f"the {len(alternatives)} alternatives in {path} do not compile once OR-ed together "
            f"(error at offset {exc.pos})"
        ) from None
    return ExtraPattern(pattern, path, alternatives=len(alternatives))


def union(base: str, extra: ExtraPattern | None) -> str:
    """``base`` OR-ed with the extra pattern when one is configured, else ``base`` unchanged.

    Appended after the ``ci.yml`` pattern, in the order the CI step appends the repository
    variable, so the two build the same expression. ``|`` binds loosest, and every alternative
    was compiled on its own before it got here, so no grouping is needed to keep them apart.
    """
    if extra is None or not extra.configured:
        return base
    return f"{base}|{extra.pattern}"
