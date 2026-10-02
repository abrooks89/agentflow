"""Stabilization-test suggestion collection — written by waves, read at epic end.

Verifier subagents emit `integration_test_suggestions` for behaviors that warrant
integration testing but couldn't be covered in-wave (real DB / network / container
/ Playwright / cross-service flow). The wave forwards these to the orchestrator,
which appends them to `stabilization-tests/<epic-slug>.md`. At epic completion,
Step 9 surfaces the count + path in EPIC_DONE summary.

This module owns the file format and append/read helpers. The directory
resolves under the installed suite root: `<suite_root>/skills/_shared/
stabilization-tests/` (see `suggestions_dir`; per-call `base_dir` overrides).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def suggestions_dir(suite_root: Optional[Path] = None) -> Path:
    """Stabilization-tests directory, resolved under the suite root.

    Defaults to the suite this module is installed in (derived from
    ``__file__``); pass ``suite_root`` to resolve against another install.
    """
    root = Path(suite_root) if suite_root is not None else Path(__file__).resolve().parents[3]
    return root / "skills" / "_shared" / "stabilization-tests"


@dataclass(frozen=True)
class IntegrationTestSuggestion:
    task_id: int
    task_name: str
    feature: str
    why_deferred: str
    suggested_approach: str
    source: str = "verifier"  # "verifier (iteration N)" or "wave subagent"
    iteration: Optional[int] = None


def _epic_file(epic_slug: str, base_dir: Optional[Path] = None) -> Path:
    return (base_dir or suggestions_dir()) / f"{epic_slug}.md"


def _render_entry(s: IntegrationTestSuggestion) -> str:
    if s.source == "verifier" and s.iteration is not None:
        source_str = f"verifier (iteration {s.iteration})"
    else:
        source_str = s.source
    return (
        f"### Task {s.task_id}: {s.task_name}\n"
        f"**Feature:** {s.feature}\n"
        f"**Why deferred:** {s.why_deferred}\n"
        f"**Suggested approach:** {s.suggested_approach}\n"
        f"**Source:** {source_str}\n\n"
        f"---\n"
    )


def _header(epic_name: str) -> str:
    return (
        f"# Stabilization tests — {epic_name}\n\n"
        "Collected during epic execution. Each entry: a feature/behavior the wave "
        "subagent (or its verifier) felt deserved an integration test but couldn't "
        "write one in-wave (required real DB / network / container / Playwright / "
        "cross-service flow).\n\n"
        "## Suggestions\n\n"
    )


def append_suggestion(
    epic_slug: str,
    epic_name: str,
    suggestion: IntegrationTestSuggestion,
    base_dir: Optional[Path] = None,
) -> Path:
    """Append a suggestion to the epic's stabilization-tests file.

    Creates the file with header on first write. Returns the file path.
    """
    path = _epic_file(epic_slug, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_header(epic_name) + _render_entry(suggestion), encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write(_render_entry(suggestion))
    return path


def append_suggestions(
    epic_slug: str,
    epic_name: str,
    suggestions: list[IntegrationTestSuggestion],
    base_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Append a list of suggestions. Returns path on writes, None when list is empty."""
    if not suggestions:
        return None
    path = None
    for s in suggestions:
        path = append_suggestion(epic_slug, epic_name, s, base_dir)
    return path


def count_suggestions(epic_slug: str, base_dir: Optional[Path] = None) -> int:
    """Count entries in an epic's stabilization-tests file. 0 if file absent."""
    path = _epic_file(epic_slug, base_dir)
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    # Each entry starts with a `### Task ` heading.
    return text.count("\n### Task ") + (1 if text.startswith("### Task ") else 0)


def render_summary(
    epic_slug: str, base_dir: Optional[Path] = None
) -> Optional[str]:
    """Return a one-line summary for EPIC_DONE, or None if no suggestions."""
    path = _epic_file(epic_slug, base_dir)
    n = count_suggestions(epic_slug, base_dir)
    if n == 0:
        return None
    return f"Suggested stabilization tests: {path} ({n} suggestions)"
