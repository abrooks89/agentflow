"""The backlog skill's instructions, held to the code they drive.

The skill is prose an agent follows, so its snippets and its frontmatter template are the program.
The doc pins below keep stale wording out; the behavioural tests EXTRACT the runnable pieces from
SKILL.md and run them against a scratch data root with the real survey, so an instruction that
drifts away from what the survey actually does fails here rather than in a user's session.

WHAT IT CAN PROVE: the title template survives a title with `: `, the collision check finds a
same-slug spec in any date, partition or project, and the Step 6 verification tells a confirmed
pin from a collision and from an unparseable file.
WHAT IT CANNOT PROVE: that a live session runs the snippets rather than paraphrasing them.
"""

from __future__ import annotations

import os
import re
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentflow.config
from agentflow.epic_survey import survey_epics

SKILL = Path(__file__).resolve().parents[2] / "backlog" / "SKILL.md"
CUTOFF = "2020-01-01T00:00:00+00:00"
SLUG = "retry-the-upload"
COLON_TITLE = "Fix: retry the upload on timeout"

_FENCE = re.compile(r"^([ \t]*)```(\w*)\n(.*?)^\1```", re.DOTALL | re.MULTILINE)


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _blocks(lang: str) -> list[str]:
    return [textwrap.dedent(m.group(3)) for m in _FENCE.finditer(_text()) if m.group(2) == lang]


def _python_block(marker: str) -> str:
    found = [b for b in _blocks("python") if marker in b]
    assert len(found) == 1, f"expected exactly one python block in SKILL.md containing {marker!r}, got {len(found)}"
    return found[0]


def _collision_snippet() -> str:
    return _python_block("collisions")


def _verify_snippet() -> str:
    return _python_block('print("CONFIRMED")')


def _title_template_line() -> str:
    lines = [ln for b in _blocks("yaml") for ln in b.splitlines() if ln.startswith("title:")]
    assert len(lines) == 1, f"expected one `title:` line in the frontmatter template, got {lines}"
    assert "<one-line title>" in lines[0]
    return lines[0]


def _section(heading: str) -> str:
    text = _text()
    start = text.index(heading)
    nxt = text.find("\n### ", start + len(heading))
    nxt2 = text.find("\n## ", start + len(heading))
    ends = [e for e in (nxt, nxt2) if e != -1]
    return text[start:min(ends)] if ends else text[start:]


@pytest.fixture
def data_root(tmp_path, monkeypatch) -> Path:
    """A scratch data root the extracted snippets resolve through `load_environment`."""
    root = tmp_path / "data"
    (root / "projects" / "toy" / "design" / "specs").mkdir(parents=True)
    env = SimpleNamespace(data_root=str(root), mtime_cutoff_iso=CUTOFF)
    monkeypatch.setattr(agentflow.config, "load_environment", lambda: env)
    return root


def _write_spec(path: Path, title_line: str, slug: str = SLUG, state: str = "speccing", project: str = "toy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nslug: {slug}\n{title_line}\ncurrent_state: {state}\ndepends_on: []\nproject: {project}\n---\n\n# Body\n",
        encoding="utf-8",
    )
    return path


def _templated_title(title: str) -> str:
    return _title_template_line().replace("<one-line title>", title)


def _run(snippet: str, capsys, **subs: str) -> str:
    for key, value in subs.items():
        snippet = snippet.replace(f"<{key}>", value)
    leftover = re.findall(r"<[a-z][a-z-]*>", snippet)
    assert not leftover, f"snippet still carries placeholders the test did not fill: {leftover}"
    exec(compile(snippet, str(SKILL), "exec"), {"__name__": "backlog_snippet"})
    return capsys.readouterr().out


def _surveyed_slugs(root: Path) -> dict[str, str]:
    return {r.slug: r.title for r in survey_epics(root, CUTOFF, include_archived=True)}


# --------------------------------------------------------------------------- doc pins


def test_heading_and_trigger_are_current():
    text = _text()
    assert "backlog-it" not in text
    assert not re.search(r"^# Pin\b", text, re.MULTILINE)
    assert re.search(r"^# Backlog$", text, re.MULTILINE)
    assert "Plain `/backlog` with no input" in text


def test_no_fallback_bucket_routing():
    text = _text()
    assert "personal" not in text.lower()
    assert "`other`" not in text
    assert "fallback bucket" not in text.replace("There is no fallback bucket", "")
    assert "resolve_project_name" in text
    assert "say so" in text.lower()


def test_confirmation_prints_the_dated_path_and_examples_match_it():
    text = _text()
    assert "> Pinned `<slug>` — surveyed in the planning queue at `projects/<name>/design/specs/YYYY-MM-DD-<slug>.md`" in text
    assert "visible on dashboard" not in text
    replies = re.findall(r"^Reply: \*\"(.*)\"\*$", text, re.MULTILINE)
    assert replies, "no example replies found"
    shape = re.compile(
        r"Pinned `([a-z0-9-]+)` — surveyed in the planning queue at "
        r"`projects/<name>/design/specs/\d{4}-\d{2}-\d{2}-([a-z0-9-]+)\.md`"
    )
    for reply in replies:
        m = shape.fullmatch(reply)
        assert m and m.group(1) == m.group(2), f"example reply does not match the template: {reply!r}"


def test_template_quotes_the_title_and_says_how_to_escape():
    assert re.fullmatch(r'title: "<one-line title>"', _title_template_line())
    text = _text()
    assert r"\"" in text and r"\\" in text


def test_collision_check_runs_before_writing():
    step5 = _section("### Step 5")
    assert step5.index("collisions") < step5.index("File: `<data_root>/projects/<name>/design/specs/YYYY-MM-DD-<slug>.md`")
    snippet = _collision_snippet()
    assert "TERMINAL_PARTITIONS" in snippet and "include_archived=True" in snippet
    assert "do NOT write" in step5


def test_step6_never_confirms_pinned_on_a_collision_or_a_miss():
    step6 = _section("### Step 6")
    assert "source_path" in _verify_snippet()
    assert "Only `CONFIRMED` is a successful pin" in step6
    assert 'never produce a "Pinned" message' in step6
    assert "never call it pinned" in step6


def test_not_surveyed_names_malformed_frontmatter_and_the_parser():
    step6 = _section("### Step 6")
    assert "Malformed frontmatter" in step6
    parse = _python_block("parse_spec_frontmatter")
    assert "from agentflow.plan_parser import" in parse and "yaml.YAMLError" in parse


# --------------------------------------------------------------------------- behaviour


def test_templated_title_with_colon_is_surveyed_and_unquoted_is_not(data_root, capsys):
    specs = data_root / "projects" / "toy" / "design" / "specs"
    good = _write_spec(specs / f"2026-01-02-{SLUG}.md", _templated_title(COLON_TITLE))
    assert _surveyed_slugs(data_root).get(SLUG) == COLON_TITLE
    assert "CONFIRMED" in _run(_verify_snippet(), capsys, slug=SLUG, **{"written-path": str(good)})

    # Control: the same title unquoted is the failure the quoting exists to prevent.
    bad_slug = "unquoted-control"
    bad = _write_spec(specs / f"2026-01-02-{bad_slug}.md", f"title: {COLON_TITLE}", slug=bad_slug)
    assert bad_slug not in _surveyed_slugs(data_root)
    out = _run(_verify_snippet(), capsys, slug=bad_slug, **{"written-path": str(bad)})
    assert out.strip() == "NOT SURVEYED"
    parsed = _run(_python_block("parse_spec_frontmatter"), capsys, **{"written-path": str(bad)})
    assert parsed.startswith("malformed frontmatter:")


def test_escaped_double_quote_in_title_survives(data_root):
    title = 'Rename the "Ship" button: phase two'
    escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    _write_spec(data_root / "projects" / "toy" / "design" / "specs" / f"2026-01-02-{SLUG}.md", _templated_title(escaped))
    assert _surveyed_slugs(data_root).get(SLUG) == title


def test_step6_reports_another_file_when_a_done_spec_wins_the_slug(data_root, capsys):
    older = _write_spec(
        data_root / "projects" / "otherproj" / "design" / "specs" / "done" / f"2025-03-04-{SLUG}.md",
        _templated_title("Earlier work"), state="done", project="otherproj",
    )
    new = _write_spec(data_root / "projects" / "toy" / "design" / "specs" / f"2026-01-02-{SLUG}.md", _templated_title(COLON_TITLE))
    out = _run(_verify_snippet(), capsys, slug=SLUG, **{"written-path": str(new)})
    assert out.strip() == f"SURVEYED FROM ANOTHER FILE: {older}"


def test_collision_check_reports_free_slug(data_root, capsys):
    specs = data_root / "projects" / "toy" / "design" / "specs"
    _write_spec(specs / f"2026-01-02-longer-{SLUG}.md", _templated_title("A longer slug"), slug=f"longer-{SLUG}")
    assert _run(_collision_snippet(), capsys, slug=SLUG).strip() == "slug is free"


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "before-cutoff"])
@pytest.mark.parametrize("partition", ["", "done", "archived"])
def test_collision_check_finds_an_older_dated_spec_in_every_partition(data_root, capsys, partition, stale):
    specs = data_root / "projects" / "otherproj" / "design" / "specs"
    old = _write_spec((specs / partition if partition else specs) / f"2019-07-08-{SLUG}.md",
                      _templated_title("Old one"), state=partition or "speccing", project="otherproj")
    if stale:
        # Before CUTOFF the survey never reads the file, so only the glob half can find it.
        past = 1_500_000_000
        os.utime(old, (past, past))
        assert SLUG not in _surveyed_slugs(data_root)
    out = _run(_collision_snippet(), capsys, slug=SLUG)
    assert str(old) in out.splitlines()


def test_collision_check_finds_a_frontmatter_slug_under_another_filename(data_root, capsys):
    other = _write_spec(data_root / "projects" / "toy" / "design" / "specs" / "2026-01-01-renamed-file.md",
                        _templated_title("Same slug, other name"))
    assert str(other) in _run(_collision_snippet(), capsys, slug=SLUG).splitlines()


def test_fallback_project_honours_the_project_argument():
    # The current-project fallback must pass the invoking --project value, or a lane on a
    # non-default project pins into the session default.
    text = _text()
    assert "resolve_project_name(<project-arg>)" in text
    assert "resolve_project_name())" not in text


def test_collision_check_finds_a_pre_cutoff_design_suffixed_spec(data_root, capsys):
    # The survey strips a -design suffix when it derives a slug, so the glob half must too.
    specs = data_root / "projects" / "toy" / "design" / "specs"
    old = _write_spec(specs / f"2019-01-01-{SLUG}-design.md", _templated_title("Design form"))
    past = 1_500_000_000
    os.utime(old, (past, past))
    assert str(old) in _run(_collision_snippet(), capsys, slug=SLUG).splitlines()
