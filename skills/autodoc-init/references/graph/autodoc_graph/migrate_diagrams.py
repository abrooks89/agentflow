"""One-time migration of body-stored diagrams into the reserved ``_diagram`` key
(gh#465).

Older pages keep their validated mermaid as a ``## Diagram`` section in the
stored BODY. The ``_diagram`` carry-forward (gh#497) cannot see a diagram there,
so the rolling audit's re-emits kept dropping them. This moves each one into
``_diagram`` / ``_diagram_sha`` exactly as the diagram phase stores it
(``records.write_records``' diagram path), and strips the section from the body;
the renderer then draws it back as the page's one Diagram section.

Nothing is ever dropped. A page is only moved when:

* its kind is one the renderer draws ``_diagram`` on (``DIAGRAM_KINDS``) — on any
  other kind, a meta page included, the key is never rendered, so the diagram is
  LEFT in the body (the body-section carry in ``write_records`` covers it);
* it holds no stored ``_diagram`` already (that one supersedes the section at
  render time, so there is nothing to move);
* its body has exactly one ``## Diagram`` section, holding one mermaid fence and
  nothing else (a caption would be lost in the move);
* the mermaid passes the suite's validator (``$SUITE_ROOT/scripts/validate_mermaid.py``).

A page failing either of the last two is REFUSED and reported; it stays exactly
as stored. The move is idempotent: a migrated body has no section left, so a
second run finds nothing.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adapter import GraphAdapter
from .records import diagram_section_spans
from .render_pages import DIAGRAM_KINDS, _insert_diagram

Validator = Callable[[str], list[str]]

# heading, one mermaid fence, and nothing but whitespace around it
_SECTION_RE = re.compile(
    r"\A## Diagram[ \t]*\n\s*```mermaid[^\S\n]*\n(.*?)\n```[ \t]*\s*\Z", re.DOTALL
)


class MigrateError(RuntimeError):
    """The migration could not run at all (as opposed to refusing one page)."""


def load_mermaid_validator(path: Path | str) -> Validator:
    """``validate(source) -> [errors]`` from the suite's validate_mermaid.py.

    Loaded by path because the script lives at the suite root, outside this
    package; there is no default location to guess, so the caller names it.
    """
    p = Path(path)
    if not p.is_file():
        raise MigrateError(f"mermaid validator {p} not found — pass the suite's "
                           "$SUITE_ROOT/scripts/validate_mermaid.py")
    spec = importlib.util.spec_from_file_location("validate_mermaid", p)
    if spec is None or spec.loader is None:
        raise MigrateError(f"mermaid validator {p} cannot be loaded as a Python module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    validate = getattr(module, "validate", None)
    if not callable(validate):
        raise MigrateError(f"mermaid validator {p} defines no validate(source) function")
    return validate


def _plan(ent: dict[str, Any], validate: Validator) -> tuple[str, dict[str, Any]]:
    """('migrate' | 'refuse' | 'leave', detail) for one entity with a body section."""
    file = ent["qualified_name"]
    body = ent["body"].replace("\r\n", "\n").replace("\r", "\n")
    fm = dict(ent.get("frontmatter") or {})
    if ent["kind"] not in DIAGRAM_KINDS:
        return "leave", {"file": file, "reason": (
            f"kind {ent['kind']!r} renders no _diagram; the diagram stays in the body")}
    if fm.get("_diagram"):
        return "leave", {"file": file, "reason": (
            "already holds a stored _diagram, which supersedes the body section at render")}
    spans = diagram_section_spans(body)
    if len(spans) != 1:
        return "refuse", {"file": file, "reason": (
            f"{len(spans)} '## Diagram' sections; resolve to one by hand")}
    start, end = spans[0]
    m = _SECTION_RE.match(body[start:end])
    if m is None or "```" in m.group(1):
        return "refuse", {"file": file, "reason": (
            "the '## Diagram' section holds something besides one mermaid fence; "
            "moving it would drop that content")}
    mermaid = m.group(1)
    errors = validate(mermaid)
    if errors:
        return "refuse", {"file": file, "reason": (
            "mermaid fails validation: " + "; ".join(errors))}
    new_body = body[:start] + body[end:]
    if end == len(body):  # a trailing section went: no blank tail
        new_body = new_body.rstrip("\n") + "\n"
    # the drawn-at sha the page already carries; never guessed, so an unknown one
    # stays empty and diagram-staleness flags the page for a redraw
    sha = str(fm.get("diagram_source_sha") or "")
    fm["_diagram"] = mermaid
    fm["_diagram_sha"] = sha
    identical = _insert_diagram(new_body, mermaid).rstrip("\n") == body.rstrip("\n")
    return "migrate", {
        "file": file, "diagram_sha": sha, "render_identical": identical,
        "row": {**ent, "body": new_body, "frontmatter": fm},
    }


def migrate_body_diagrams(
    adapter: GraphAdapter, validate: Validator, *, dry_run: bool = False
) -> dict[str, Any]:
    """Move every movable body-stored diagram into ``_diagram``.

    Returns ``{dry_run, migrated, refused, left}``. ``migrated`` entries carry
    ``render_identical``: False means the section sat somewhere other than where
    the renderer places a diagram, so the rendered page keeps its one Diagram
    section but in the canonical spot. Moves land in ONE atomic adapter batch.
    """
    migrated: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    left: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for ent in adapter.find_entities({"tier": "concept"}):
        if not diagram_section_spans((ent.get("body") or "").replace("\r\n", "\n")):
            continue
        action, detail = _plan(ent, validate)
        if action == "migrate":
            rows.append(detail.pop("row"))
            migrated.append(detail)
        elif action == "refuse":
            refused.append(detail)
        else:
            left.append(detail)
    if rows and not dry_run:
        adapter.upsert_entities(rows)
    return {"dry_run": dry_run, "migrated": migrated, "refused": refused, "left": left}
