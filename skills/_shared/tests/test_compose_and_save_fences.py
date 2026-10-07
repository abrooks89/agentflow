"""Every `compose_and_save(` fence a seat copies must survive a Windows data root and a hostile context.

THE DEFECT. The lead-dev and dev-manager fences used to put the root inside an ordinary string:
`compose_and_save("<data_root>/projects/<name>/wiki", ..., context="<...>")`. A seat substitutes
its data root into that literal, and a Windows root carries `\\U...` -- a unicode escape -- so the
fence fails at COMPILE time with `SyntaxError: (unicode error)`. `compose_and_save` promises to
return a `failed` outcome rather than raise, but a fence that never compiles never reaches it, so
the error lands in the firing. The inline `context="..."` was fragile the same way for a
backslash or a double quote in the summary.

WHAT THIS PINS. Each fenced block that calls `compose_and_save(` in a tracked `skills/*/SKILL.md`
is filled in the way a seat fills it -- every `<...>` placeholder replaced -- with the hostile
values below, and must (1) compile and (2) when executed against a stubbed config and a recording
`compose_and_save`, hand over a root built from the data root and the context verbatim.

The `write_page(` fence in wiki-save/SKILL.md had the same two defects (an undefined `wiki_root`
and an inline `body="..."`), so it is pinned the same way below, against a recording `write_page`.

WHAT IT CANNOT PROVE. That a seat copies the fence rather than retyping the call; and the one
text the raw triple-quoted context cannot hold (three double quotes in a row) is not exercised,
because the fences document it as the residual rather than handle it.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentflow import config, save_summariser, wiki
from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]

#: A Windows data root: `\U`, `\d` and `\a` are each an escape (or an error) in a non-raw string.
DATA_ROOT = r"C:\Users\someone\docs\agentflow"
NAME = "agentflow"
#: Backslashes (including a trailing one), both quote characters, an escape-looking `\N{`, and
#: more than one line -- everything a real summary can carry except three double quotes in a row.
CONTEXT = "Merged the fix under C:\\Users\\someone\\x and said \"done\".\nIt's the seat's 'own' note \\N{ \\"
LANE = "jr-dev-1"
#: The scalar placeholders a `write_page(` fence carries. Plain values: they sit in ordinary
#: string literals, and what makes them safe is that they are a slug, a line and a date.
SCALARS = {"kebab-slug": "a-lesson", "one line": "A lesson", "YYYY-MM-DD": "2026-01-02"}

#: The fences this fix rewrote. A floor, so a renamed seat or a reworded call reds here instead of
#: the corpus quietly shrinking to nothing.
REQUIRED = ("lead-dev", "dev-manager", "wiki-save")

_FENCE = re.compile(r"^```python[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)
_PLACEHOLDER = re.compile(r"<([^<>\n]+)>")


def _value(placeholder: str, context: str) -> str:
    """What a seat writes in place of `<placeholder>`."""
    if placeholder == "name":
        return NAME
    if placeholder == "lane":
        return LANE
    if placeholder in SCALARS:
        return SCALARS[placeholder]
    if "data_root" in placeholder:
        return DATA_ROOT
    return context


def _fill(src: str, context: str = CONTEXT) -> str:
    return _PLACEHOLDER.sub(lambda m: _value(m.group(1), context), src)


def _fences(call: str = "compose_and_save(") -> list[tuple[str, str]]:
    found = []
    for path in only_tracked(SKILLS_ROOT, SKILLS_ROOT.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        for block in _FENCE.findall(text):
            if call in block:
                found.append((path.parent.name, block))
    return found


FENCES = _fences()
WRITE_PAGE_FENCES = _fences("write_page(")
#: Same floor as `REQUIRED`, for the `write_page(` fences.
REQUIRED_WRITE_PAGE = ("wiki-save",)


def test_the_named_fences_are_in_the_corpus() -> None:
    seats = {seat for seat, _ in FENCES}
    missing = [s for s in REQUIRED if s not in seats]
    assert not missing, (
        f"no fenced `compose_and_save(` call found in {missing}. Either the fence moved or the "
        "corpus came back empty; both leave the compile check below covering nothing"
    )


@pytest.mark.parametrize("seat,block", FENCES, ids=[s for s, _ in FENCES])
def test_the_fence_compiles_with_a_windows_root_and_a_plain_context(seat: str, block: str) -> None:
    """The reported failure on its own: the data root, with a summary that has nothing to escape."""
    src = _fill(block, context="a plain one-line summary")
    try:
        compile(src, f"<{seat} compose_and_save fence>", "exec")
    except SyntaxError as exc:
        pytest.fail(
            f"{seat}/SKILL.md's compose_and_save fence does not compile with a Windows data root "
            f"({exc.msg}): the root is pasted into a string literal instead of built from config."
            f"\n--- filled fence ---\n{src}"
        )


@pytest.mark.parametrize("seat,block", FENCES, ids=[s for s, _ in FENCES])
def test_the_fence_compiles_with_a_windows_root_and_a_hostile_context(
    seat: str, block: str
) -> None:
    src = _fill(block)
    try:
        compile(src, f"<{seat} compose_and_save fence>", "exec")
    except SyntaxError as exc:
        pytest.fail(
            f"{seat}/SKILL.md's compose_and_save fence does not compile once a seat fills it in "
            f"({exc.msg}). A fence that cannot compile raises into the firing before the save can "
            f"return `failed`. Build the root in code from config and pass the context through a "
            f"raw triple-quoted variable.\n--- filled fence ---\n{src}"
        )


@pytest.mark.parametrize("seat,block", FENCES, ids=[s for s, _ in FENCES])
def test_the_fence_resolves_the_root_from_config_and_passes_the_context_verbatim(
    seat: str, block: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def recorder(wiki_root, **kw):
        calls.append((wiki_root, kw))
        return SimpleNamespace(render=lambda: "stub outcome")

    monkeypatch.setattr(config, "load_environment", lambda: SimpleNamespace(data_root=DATA_ROOT))
    monkeypatch.setattr(save_summariser, "compose_and_save", recorder)
    exec(compile(_fill(block), f"<{seat} fence>", "exec"), {"__name__": "fence"})

    assert len(calls) == 1, f"{seat}'s fence made {len(calls)} compose_and_save calls, not one"
    wiki_root, kw = calls[0]
    assert Path(wiki_root) == Path(DATA_ROOT) / "projects" / NAME / "wiki", (
        f"{seat}'s fence did not build <data_root>/projects/<name>/wiki from config: {wiki_root!r}"
    )
    assert kw["context"] == CONTEXT, (
        f"{seat}'s fence altered the context on the way in: {kw['context']!r}"
    )


def test_the_named_write_page_fences_are_in_the_corpus() -> None:
    seats = {seat for seat, _ in WRITE_PAGE_FENCES}
    missing = [s for s in REQUIRED_WRITE_PAGE if s not in seats]
    assert not missing, (
        f"no fenced `write_page(` call found in {missing}. Either the fence moved or the corpus "
        "came back empty; both leave the write_page checks below covering nothing"
    )


@pytest.mark.parametrize("seat,block", WRITE_PAGE_FENCES, ids=[s for s, _ in WRITE_PAGE_FENCES])
@pytest.mark.parametrize(
    "body", ["a plain one-line page [[a-link]]", CONTEXT], ids=["plain", "hostile"]
)
def test_the_write_page_fence_compiles_with_a_windows_root(
    seat: str, block: str, body: str
) -> None:
    src = _fill(block, context=body)
    try:
        compile(src, f"<{seat} write_page fence>", "exec")
    except SyntaxError as exc:
        pytest.fail(
            f"{seat}/SKILL.md's write_page fence does not compile once a seat fills it in "
            f"({exc.msg}). Build the root in code from config and pass the body through a raw "
            f"triple-quoted variable.\n--- filled fence ---\n{src}"
        )


@pytest.mark.parametrize("seat,block", WRITE_PAGE_FENCES, ids=[s for s, _ in WRITE_PAGE_FENCES])
def test_the_write_page_fence_resolves_the_root_from_config_and_passes_the_body_verbatim(
    seat: str, block: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes, refreshes = [], []

    def write_recorder(wiki_root, **kw):
        writes.append((wiki_root, kw))
        return Path(wiki_root) / "pages" / f"{kw['slug']}.md"

    monkeypatch.setattr(config, "load_environment", lambda: SimpleNamespace(data_root=DATA_ROOT))
    monkeypatch.setattr(wiki, "write_page", write_recorder)
    monkeypatch.setattr(wiki, "refresh_projection", lambda root, **kw: refreshes.append(root) or [])
    exec(compile(_fill(block), f"<{seat} fence>", "exec"), {"__name__": "fence"})

    expected_root = Path(DATA_ROOT) / "projects" / NAME / "wiki"
    assert len(writes) == 1, f"{seat}'s fence made {len(writes)} write_page calls, not one"
    wiki_root, kw = writes[0]
    assert Path(wiki_root) == expected_root, (
        f"{seat}'s fence did not build <data_root>/projects/<name>/wiki from config: {wiki_root!r}"
    )
    assert kw["body"] == CONTEXT, f"{seat}'s fence altered the body on the way in: {kw['body']!r}"
    assert (kw["slug"], kw["title"], kw["created"], kw["lane"]) == (
        SCALARS["kebab-slug"],
        SCALARS["one line"],
        SCALARS["YYYY-MM-DD"],
        LANE,
    ), f"{seat}'s fence passed unexpected write_page arguments: {kw!r}"
    assert [Path(r) for r in refreshes] == [expected_root], (
        f"{seat}'s fence did not refresh the projection of the root it wrote to: {refreshes!r}"
    )
