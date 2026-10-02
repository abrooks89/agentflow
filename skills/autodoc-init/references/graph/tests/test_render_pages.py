"""Deterministic markdown renderer over the store (epic 704).

The renderer is the ONLY writer of the markdown tree: frontmatter is
entity.frontmatter plus renderer-stamped universal fields, bodies are
agent prose, and every rendered wikilink comes from a row that resolved.
Rendering twice over an unchanged store is byte-identical.
"""

from __future__ import annotations

import re

import pytest
from conftest import entity_row

from autodoc_graph.adapter import AdapterError
from autodoc_graph.records import write_records
from autodoc_graph.render_pages import (
    MARKER_FILE,
    WIKILINK_RE,
    RenderError,
    _insert_diagram,
    render_pages,
)


def _seed(adapter, *, with_flow=False, meta=False):
    adapter.upsert_entities([
        entity_row("sym:corp:app/x.py:function:handler",
                   kind="function", name="handler"),
        entity_row("sym:corp:app/x.py:class:Widget", kind="class", name="Widget"),
    ])
    adapter.update_corpus(repo_ref="owner/repo", branch="main", last_indexed_sha="sha1",
                          project="demo")
    pages = [
        {
            "file": "services/Widget Service.md", "type": "service",
            "title": "Widget Service", "source_paths": ["app/x.py"],
            "summary": "Serves widgets.",
            "body": "Serves widgets via [[Widget Helper]] and [[Ghost Page]].\n\n"
                    "## Source\n\n`app/x.py`\n",
            "frontmatter": {"service_kind": "api", "path": "app/",
                            "language": "python", "entrypoint": "app/x.py",
                            "status": "active"},
            "documents": [{"file": "app/x.py", "kind": "function", "name": "handler"}],
            "edges": [],
        },
        {
            "file": "components/Widget Helper.md", "type": "component",
            "title": "Widget Helper", "source_paths": ["app/x.py"],
            "summary": "Helps widgets.", "body": "Helps widgets.\n",
            "frontmatter": {"component_kind": "util", "path": "app/x.py",
                            "language": "python"},
            "documents": [{"file": "app/x.py", "kind": "class", "name": "Widget"}],
            "edges": [{"rel": "related", "dst_type": "service",
                       "dst_title": "Widget Service"}],
        },
    ]
    if with_flow:
        pages.append({
            "file": "flows/Widget Lifecycle.md", "type": "flow",
            "title": "Widget Lifecycle", "source_paths": ["app/x.py"],
            "summary": "Cradle to grave.",
            "body": "Cradle to grave.\n\n## What it does\n\nSteps.\n",
            "frontmatter": {"flow_kind": "pipeline", "status": "active"},
            "documents": [],
            "edges": [{"rel": "related", "dst_type": "service",
                       "dst_title": "Widget Service"}],
        })
    meta_pages = []
    if meta:
        meta_pages = [{"file": "overview.md", "title": "corp Overview",
                       "body": "Big picture of [[Widget Service]].\n"}]
    diagrams = []
    if with_flow:
        diagrams = [{
            "type": "flow", "title": "Widget Lifecycle", "embedded": True,
            "mermaid": "flowchart TD\n  A --> B\n  %% source: app/x.py",
            "source_sha": "sha1",
        }]
    write_records(adapter, {"pages": pages, "meta_pages": meta_pages,
                            "diagrams": diagrams},
                  sha="sha1", date="2026-08-07")


def _read(out, rel):
    return (out / rel).read_text(encoding="utf-8")


def test_render_writes_pages_with_stamped_frontmatter(adapter, tmp_path):
    _seed(adapter)
    out = tmp_path / "rendered"
    report = render_pages(adapter, out)
    text = _read(out, "services/Widget Service.md")
    assert text.startswith("---\n")
    assert 'type: "service"' in text
    assert 'title: "Widget Service"' in text
    assert 'created: "2026-08-07"' in text
    assert 'source_repo: "owner/repo"' in text
    assert '- "app/x.py"' in text
    assert "_created" not in text  # reserved keys never rendered
    assert report["pages"] == 2


def test_render_is_byte_identical_twice(adapter, tmp_path):
    _seed(adapter, with_flow=True, meta=True)
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    first = {p.relative_to(out).as_posix(): p.read_bytes()
             for p in out.rglob("*.md")}
    render_pages(adapter, out, force=True)
    second = {p.relative_to(out).as_posix(): p.read_bytes()
              for p in out.rglob("*.md")}
    assert first == second
    assert all(b"\r" not in body for body in second.values())  # LF endings
    # a DIFFERENT target directory renders identical bytes too: content must
    # never depend on the disposable sibling dir's name (live-smoke finding)
    other = tmp_path / "rendered2"
    render_pages(adapter, other)
    third = {p.relative_to(other).as_posix(): p.read_bytes()
             for p in other.rglob("*.md")}
    assert first == third


def test_unresolvable_body_links_are_dropped_not_repaired(adapter, tmp_path):
    _seed(adapter)
    out = tmp_path / "rendered"
    report = render_pages(adapter, out)
    text = _read(out, "services/Widget Service.md")
    assert "[[Widget Helper]]" in text          # resolves -> stays a link
    assert "[[Ghost Page]]" not in text         # cannot resolve -> unwrapped
    assert "Ghost Page" in text                 # prose text is preserved
    dropped = [d for d in report["dropped_body_links"] if d["target"] == "Ghost Page"]
    assert dropped and dropped[0]["page"] == "services/Widget Service.md"


def test_related_frontmatter_comes_from_edge_rows(adapter, tmp_path):
    _seed(adapter, with_flow=True)
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    helper = _read(out, "components/Widget Helper.md")
    assert '- "[[Widget Service]]"' in helper   # outbound related edge
    # flow backlink: the flow's related edge renders on the touched page too
    service = _read(out, "services/Widget Service.md")
    assert '- "[[Widget Lifecycle]]"' in service


def test_dreamed_edges_are_never_rendered(adapter, tmp_path):
    _seed(adapter)
    # Same (src, dst, rel) tuple: the dreamed upsert REPLACES the agent edge
    # (UNIQUE key), so the page must lose its rendered wikilink — a dream is
    # a proposal, never a rendered link.
    adapter.upsert_edges([{
        "src_id": "con:corp:component:widget-helper",
        "dst_id": "con:corp:service:widget-service",
        "rel": "related", "origin": "dreamed", "confidence": 0.3,
        "evidence": {"rationale": "dreamed"},
    }])
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    helper = _read(out, "components/Widget Helper.md")
    assert '[[Widget Service]]' not in helper
    assert "related:" not in helper


def test_dreamed_only_relation_renders_nothing(adapter, tmp_path):
    _seed(adapter)
    adapter.upsert_edges([{
        "src_id": "con:corp:service:widget-service",
        "dst_id": "con:corp:component:widget-helper",
        "rel": "related", "origin": "dreamed", "confidence": 0.3,
        "evidence": {"rationale": "dreamed"},
    }])
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    service = _read(out, "services/Widget Service.md")
    assert "related:" not in service  # only a dreamed edge exists for this page


def test_diagram_section_is_rendered_after_summary(adapter, tmp_path):
    _seed(adapter, with_flow=True)
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    flow = _read(out, "flows/Widget Lifecycle.md")
    assert 'diagram: true' in flow
    assert 'diagram_source_sha: "sha1"' in flow
    body = flow.split("---\n", 2)[2]
    diagram_at = body.index("## Diagram")
    what_at = body.index("## What it does")
    assert diagram_at < what_at
    assert "```mermaid\nflowchart TD" in flow
    assert "_diagram" not in flow.split("## Diagram")[0]  # not leaked into YAML


def test_nav_layer_is_deterministic(adapter, tmp_path):
    _seed(adapter, meta=True)
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    idx = _read(out, "services/_index.md")
    assert 'type: "meta"' in idx
    assert "[[Widget Service]]" in idx and "Serves widgets." in idx
    master = _read(out, "index.md")
    assert "[[Widget Service]]" in master and "[[Widget Helper]]" in master
    overview = _read(out, "overview.md")
    assert "Big picture" in overview and 'title: "corp Overview"' in overview


def test_broken_wikilinks_impossible_by_construction(adapter, tmp_path):
    """Spec criterion 6: a rendered wikilink cannot dangle because the edge
    row behind it had to resolve at WRITE time — injecting an edge whose
    dst_id does not exist fails before rendering ever happens."""
    _seed(adapter)
    with pytest.raises(AdapterError, match="missing entity"):
        adapter.upsert_edges([{
            "src_id": "con:corp:service:widget-service",
            "dst_id": "con:corp:service:ghost-service",  # resolves to nothing
            "rel": "related", "origin": "agent",
            "evidence": {"source": "agent-return"},
        }])
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    # every [[link]] in every rendered file resolves against the rendered set
    stems = {p.stem for p in out.rglob("*.md")}
    titles = set()
    for p in out.rglob("*.md"):
        text = p.read_text(encoding="utf-8")
        m = re.search(r'^title: "(.*)"$', text, flags=re.MULTILINE)
        if m:
            titles.add(m.group(1))
    for p in out.rglob("*.md"):
        # '[' and newline are excluded for the same reason WIKILINK_RE excludes
        # them: without that, a literal '[[' in a fixture body makes this helper
        # capture the prose after it and report a dangling link that is not one.
        # That is not hypothetical - it is why the swallow fixture had to be kept
        # out of the shared seed rather than written into it.
        for target in re.findall(r"\[\[([^\]\[|#\n]+)", p.read_text(encoding="utf-8")):
            assert target in stems | titles, f"{p.name} links dangling [[{target}]]"


def test_render_stamps_the_store_primary_marker(adapter, tmp_path):
    """Adversarial finding 4: every rendered tree carries a deterministic
    .rendered-by-store marker; /autodoc-update pre-flights on it and takes
    the records path (epic 706), so the marker points there rather than at
    the retired follow-up epic (gh#611)."""
    _seed(adapter)
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    marker = (out / MARKER_FILE).read_text(encoding="utf-8")
    assert marker.startswith("corpus: corp\n")
    assert "run /autodoc-update" in marker
    assert "lands its updates as records" in marker
    assert "autodoc-update-record-conversion" not in marker
    assert "do NOT run" not in marker
    # deterministic across renders (no timestamps)
    render_pages(adapter, out, force=True)
    assert (out / MARKER_FILE).read_text(encoding="utf-8") == marker


def test_renderer_refuses_unsafe_frontmatter_key_defensively(adapter, tmp_path):
    """Adversarial finding 6, renderer side: even a key smuggled past the
    writer (written around it) must never reach the YAML block."""
    _seed(adapter)
    ent = adapter.get_entity("con:corp:component:widget-helper")
    fm = dict(ent["frontmatter"])
    fm["evil\ninjected"] = "true"
    adapter.upsert_entities([{**ent, "frontmatter": fm}])
    with pytest.raises(RenderError, match="injection"):
        render_pages(adapter, tmp_path / "rendered")


def test_render_refuses_nonempty_dir_without_force(adapter, tmp_path):
    _seed(adapter)
    out = tmp_path / "rendered"
    out.mkdir()
    (out / "stray.md").write_text("agent wrote here", encoding="utf-8")
    with pytest.raises(RenderError, match=r"stray.md"):
        render_pages(adapter, out)


def test_status_defaults_to_active_and_tags_default_to_type(adapter, tmp_path):
    _seed(adapter)
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    helper = _read(out, "components/Widget Helper.md")
    assert 'status: "active"' in helper
    assert '- "component"' in helper  # default tags carry the type


def test_hostile_title_is_quoted_safely(adapter, tmp_path):
    _seed(adapter)
    adapter.update_corpus(repo_ref="owner/repo", last_indexed_sha="sha1", project="demo")
    write_records(adapter, {"pages": [{
        "file": "processes/Evil Process.md", "type": "process",
        "title": 'Evil "Quoted: [Process]"',
        "source_paths": ["app/x.py"], "summary": "s.",
        "body": "Body.\n", "frontmatter": {"trigger": "manual"},
        "documents": [], "edges": [],
    }]}, sha="sha1", date="2026-08-07")
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    text = _read(out, "processes/Evil Process.md")
    assert 'title: "Evil \\"Quoted: [Process]\\""' in text


def test_unresolvable_frontmatter_relational_link_is_dropped(adapter, tmp_path):
    _seed(adapter)
    write_records(adapter, {"pages": [{
        "file": "processes/P.md", "type": "process", "title": "P",
        "source_paths": ["app/x.py"], "summary": "s.", "body": "Body.\n",
        "frontmatter": {"trigger": "manual",
                        "involves": ["[[Widget Service]]", "[[Nobody Home]]"]},
        "documents": [], "edges": [],
    }]}, sha="sha1", date="2026-08-07")
    out = tmp_path / "rendered"
    report = render_pages(adapter, out)
    text = _read(out, "processes/P.md")
    assert '"[[Widget Service]]"' in text
    assert "Nobody Home" not in text
    assert any(d["target"] == "Nobody Home"
               for d in report["dropped_frontmatter_links"])


# --------------------------------------------------------------------------- #
# The corpus is the "room", and every page carries its project (E-0200 item 4b)
# --------------------------------------------------------------------------- #
def test_every_rendered_page_carries_the_corpus_project(adapter, tmp_path):
    """The project is a property of the CORPUS, stamped like source_repo. It is not
    written by an agent and not inferred from a path: a page that cannot say which
    project it belongs to cannot be filed into a per-project vault pillar at all."""
    _seed(adapter)
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    for page in ("services/Widget Service.md", "components/Widget Helper.md"):
        assert 'project: "demo"' in _read(out, page), page


def test_the_renderer_REFUSES_when_the_corpus_has_no_project(adapter, tmp_path):
    """Loud, not silent. Rendering without a project would emit a whole corpus of
    unattributable pages, and the cost of finding that out later is re-rendering
    every one of them - the same shape as the existing repo_ref refusal."""
    _seed(adapter)
    adapter.update_corpus(repo_ref="owner/repo", branch="main", last_indexed_sha="sha1")
    con = adapter._db()  # the only way to unset it: the setter writes non-None only
    con.execute("UPDATE corpus SET project = NULL")
    con.commit()
    with pytest.raises(RenderError) as ei:
        render_pages(adapter, tmp_path / "rendered")
    assert "corpus.project" in str(ei.value)
    assert "--project" in str(ei.value)  # names the fix, not just the fault


def test_project_is_stamped_before_source_repo_in_key_order(adapter, tmp_path):
    """Key order is part of the rendered contract - the renderer is byte-identical
    across runs, so an unordered field would churn diffs on every build."""
    _seed(adapter)
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    text = _read(out, "services/Widget Service.md")
    assert text.index('project: "demo"') < text.index('source_repo: "owner/repo"')


def test_a_page_project_does_NOT_come_from_the_record(adapter, tmp_path):
    """A record carrying `project` must not win. The room decides, so a rogue or
    stale record cannot file its page under someone else's project."""
    _seed(adapter)
    write_records(adapter, {"pages": [{
        "file": "processes/Claimer.md", "type": "process",
        "title": "Claimer", "source_paths": ["app/x.py"], "summary": "s.",
        "body": "Body.\n", "frontmatter": {"trigger": "manual", "project": "somewhere-else"},
        "documents": [], "edges": [],
    }]}, sha="sha1", date="2026-08-07")
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    text = _read(out, "processes/Claimer.md")
    assert 'project: "demo"' in text
    assert "somewhere-else" not in text


# --------------------------------------------------------------------------- #
# gh#432: a stray '[[' in prose must not swallow the next real wikilink
# --------------------------------------------------------------------------- #
# The target class once excluded ']' and '|' but not '[' and not a newline, so a
# lone '[[' anywhere in a body opened a match that ran forward to the next ']]'
# however many lines away. _resolve_body asked _resolve() about that whole blob,
# got False (no page is named that), and substituted the span with its own inner
# text -- deleting the prose between and stripping the brackets off the real
# link that terminated it. The golden gate only caught the live instance because
# the wreckage left an unclosed '[['; a swallow that ends on a still-parsing
# line would delete prose and pass links-resolve in silence.

_SWALLOW_BODY = (
    "The written page carries a `[[` wikilink and a `word_count` above 10.\n"
    "\n"
    "Intervening prose that must survive the render.\n"
    "\n"
    "- Subject under test: both part of [[Widget Helper]].\n"
)


def test_stray_double_bracket_in_prose_does_not_swallow_a_later_link(
    adapter, tmp_path
):
    _seed(adapter)
    write_records(adapter, {"pages": [{
        "file": "processes/Syntax Notes.md", "type": "process",
        "title": "Syntax Notes", "source_paths": ["app/x.py"], "summary": "s.",
        "body": _SWALLOW_BODY, "frontmatter": {"trigger": "manual"},
        "documents": [], "edges": [],
    }]}, sha="sha1", date="2026-08-07")
    out = tmp_path / "rendered"
    report = render_pages(adapter, out)
    text = _read(out, "processes/Syntax Notes.md")
    assert "`[[` wikilink" in text            # code span intact, not emptied
    assert "[[Widget Helper]]" in text        # real link keeps BOTH brackets
    assert "Intervening prose that must survive" in text
    assert "[[Widget Helper]]." in text       # and the trailing prose with it
    # nothing was dropped: neither the code span nor the resolvable link is a
    # dead target, so the drop report for this page must be empty
    assert [d for d in report["dropped_body_links"]
            if d["page"] == "processes/Syntax Notes.md"] == []


def test_no_wikilink_match_ever_spans_a_newline(adapter):
    """Asserted on the pattern itself, not on one rendered page: the invariant
    is that a wikilink is a single-line construct, so no match may contain a
    line break no matter what prose surrounds it."""
    texts = (
        _SWALLOW_BODY,
        "Open `[[` here.\n\nClose [[Widget Helper]] four lines later.\n",
        "[[A]]\n[[B]]\n",
        "A bare [[ with no closer\nand a [[Real Link]] below.\n",
        "[[Piped|label]]\ntext\n[[Other|alias]]\n",
        # KEEP THIS ONE. Every case above is rescued by the '[' exclusion alone,
        # because the ']]' that ends the run belongs to a real '[[link]]' whose
        # own '[' stops it -- so they all stay green with the newline exclusion
        # removed, and the newline half was covered by nothing. Here the closing
        # ']]' is a Lua long-string terminator with no '[' before it, so only
        # the newline exclusion can stop the swallow.
        "Match the literal `[[` token.\n\nProse that must survive the render.\n\n"
        "Lua closes a long string with ]] at the end.\n",
    )
    for text in texts:
        for match in WIKILINK_RE.finditer(text):
            assert "\n" not in match.group(0), (text, match.group(0))


def test_a_target_containing_an_open_bracket_does_not_match():
    """'[' cannot appear in a page name, and allowing it is exactly what let a
    match run past one '[[' and into the next."""
    assert WIKILINK_RE.search("[[Foo [Bar]]") is None
    assert WIKILINK_RE.search("[[Foo|la[bel]]") is None
    # the well-formed neighbours still match
    assert WIKILINK_RE.search("[[Foo Bar]]").group(1) == "Foo Bar"


def test_piped_alias_still_matches_and_yields_its_label(adapter, tmp_path):
    """The alias form is unaffected by the narrowing: a resolvable piped link
    renders verbatim, and an unresolvable one still unwraps to its LABEL."""
    match = WIKILINK_RE.search("see [[Widget Helper|the helper]] here")
    assert match.group(1) == "Widget Helper"
    assert match.group(2) == "the helper"

    _seed(adapter)
    write_records(adapter, {"pages": [{
        "file": "processes/Aliased.md", "type": "process", "title": "Aliased",
        "source_paths": ["app/x.py"], "summary": "s.",
        "body": "Use [[Widget Helper|the helper]] not [[Ghost Page|a ghost]].\n",
        "frontmatter": {"trigger": "manual"}, "documents": [], "edges": [],
    }]}, sha="sha1", date="2026-08-07")
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    text = _read(out, "processes/Aliased.md")
    assert "[[Widget Helper|the helper]]" in text   # resolves -> verbatim
    assert "a ghost" in text                        # unresolvable -> label
    assert "[[Ghost Page" not in text


def test_single_line_unresolvable_links_still_unwrap_unchanged(adapter, tmp_path):
    """Pin, not a new behaviour: dropping body links to pages that do not exist
    (replacing them with their label or target text) is deliberate. Narrowing
    the pattern must not be mistaken for a change to it, so both the bare and
    the piped unwrap are nailed down here alongside the drop report."""
    _seed(adapter)
    write_records(adapter, {"pages": [{
        "file": "processes/Unwrap Pin.md", "type": "process",
        "title": "Unwrap Pin", "source_paths": ["app/x.py"], "summary": "s.",
        "body": "A [[Nowhere Page]] and a [[Nowhere Two|second one]].\n",
        "frontmatter": {"trigger": "manual"}, "documents": [], "edges": [],
    }]}, sha="sha1", date="2026-08-07")
    out = tmp_path / "rendered"
    report = render_pages(adapter, out)
    text = _read(out, "processes/Unwrap Pin.md")
    assert "A Nowhere Page and a second one.\n" in text
    assert "[[" not in text.split("---\n", 2)[2]
    targets = {d["target"] for d in report["dropped_body_links"]
               if d["page"] == "processes/Unwrap Pin.md"}
    assert targets == {"Nowhere Page", "Nowhere Two"}


# ------------------------------------ one '## Diagram' section per page (gh#465 item 3)
_FLOW_ID = "con:corp:flow:widget-lifecycle"
_STORED_MERMAID = "flowchart TD\n  A --> B\n  %% source: app/x.py"


def test_insert_diagram_is_idempotent():
    body = "Intro.\n\n## What it does\n\nSteps.\n"
    once = _insert_diagram(body, _STORED_MERMAID)
    assert _insert_diagram(once, _STORED_MERMAID) == once
    assert once.count("## Diagram") == 1


def test_insert_diagram_replaces_a_body_diagram_section_with_the_stored_one():
    body = ("Intro.\n\n## Diagram\n\n```mermaid\nflowchart LR\n  OLD --> X\n```\n\n"
            "## What it does\n\nSteps.\n")
    out = _insert_diagram(body, _STORED_MERMAID)
    assert out.count("## Diagram") == 1
    assert "OLD --> X" not in out and "A --> B" in out
    assert out.index("## Diagram") < out.index("## What it does")
    assert out.startswith("Intro.\n\n") and out.endswith("## What it does\n\nSteps.\n")


def test_insert_diagram_replaces_a_trailing_body_diagram_section():
    body = "Intro.\n\n## Diagram\n\n```mermaid\nflowchart LR\n  OLD --> X\n```\n"
    out = _insert_diagram(body, _STORED_MERMAID)
    assert out.count("## Diagram") == 1 and "OLD --> X" not in out


def test_page_with_body_and_stored_diagram_renders_one_section(adapter, tmp_path):
    _seed(adapter, with_flow=True)
    ent = adapter.get_entity(_FLOW_ID)
    body = ("Cradle to grave.\n\n## Diagram\n\n```mermaid\nflowchart LR\n  OLD --> X\n"
            "```\n\n## What it does\n\nSteps.\n")
    adapter.upsert_entities([{**ent, "body": body}])
    out = tmp_path / "rendered"
    render_pages(adapter, out)
    flow = _read(out, "flows/Widget Lifecycle.md")
    assert flow.count("## Diagram") == 1
    assert "A --> B" in flow and "OLD --> X" not in flow


def test_insert_diagram_strips_every_body_diagram_section():
    body = ("Intro.\n\n## Diagram\n\n```mermaid\nflowchart LR\n  OLD --> X\n```\n\n"
            "## What it does\n\nSteps.\n\n"
            "## Diagram\n\n```mermaid\nflowchart LR\n  OLDER --> Y\n```\n\n"
            "## Notes\n\nn\n")
    out = _insert_diagram(body, _STORED_MERMAID)
    assert out.count("## Diagram") == 1
    assert "OLD --> X" not in out and "OLDER --> Y" not in out and "A --> B" in out
    assert out == ("Intro.\n\n## Diagram\n\n```mermaid\n" + _STORED_MERMAID + "\n```\n\n"
                   "## What it does\n\nSteps.\n\n## Notes\n\nn\n")
    assert _insert_diagram(out, _STORED_MERMAID) == out


def test_insert_diagram_strips_a_second_trailing_section_without_a_blank_tail():
    body = ("Intro.\n\n## Diagram\n\n```mermaid\nflowchart LR\n  OLD --> X\n```\n\n"
            "## What it does\n\nSteps.\n\n"
            "## Diagram\n\n```mermaid\nflowchart LR\n  OLDER --> Y\n```\n")
    out = _insert_diagram(body, _STORED_MERMAID)
    assert out.count("## Diagram") == 1 and "OLDER --> Y" not in out
    assert out.endswith("## What it does\n\nSteps.\n")


def test_insert_diagram_leaves_a_fenced_diagram_heading_alone():
    fence = "~~~markdown\n## Diagram\n\nexample text\n~~~\n"
    body = "Intro.\n\n## Usage\n\n" + fence
    out = _insert_diagram(body, _STORED_MERMAID)
    # the fence is example text: kept whole, and the real section is added once
    assert fence in out
    assert out.count("## Diagram") == 2
    assert out.index("A --> B") < out.index("## Usage")
