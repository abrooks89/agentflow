"""The save-time residue guard: ``write_page`` refuses a page the pillar ratchet would flag.

The ratchet (:mod:`agentflow.pillar_residue`) runs AFTER a page lands, in the steward's sweep,
so a dirty page used to be found a wake later and could then only be superseded. The writer now
refuses it before the create, with the same two classes the ratchet counts. The refusal names
the class and where it matched, and NEVER the matched text: an error that quoted the term would
put it in a run log, an audit line or a registry row, which is the leak this guard exists to stop.

No literal from either class is spelled here. The person is the hermetic fixture's fake name; the
generic sanitize probe, a home path, is assembled from adjacent string literals so a line grep of
this file does not match it; the install-specific probe is a placeholder in a test-local extra
pattern file.
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from agentflow import save_summariser as summariser
from agentflow.wiki import (
    WikiFormatError,
    initial_pillar_files,
    write_page,
)

from conftest import write_environment

#: A generic sanitize-class term, assembled so this file is not itself a finding.
HOME_PATH = "/home/" "someacct"
#: A placeholder alternative for the test-local extra pattern file. Names nothing.
PRODUCT = "acmeproduct"

pytestmark = pytest.mark.usefixtures("residue_config")


def _pillar(tmp_path: Path) -> Path:
    """A provisioned pillar at ``<tmp>/projects/demo/wiki``, the real layout under a data root."""
    root = tmp_path / "projects" / "demo" / "wiki"
    (root / "pages").mkdir(parents=True)
    for name, text in initial_pillar_files("demo").items():
        (root / name).write_text(text, encoding="utf-8", newline="\n")
    return root


def _write(root: Path, *, slug: str = "a-lesson", title: str = "A lesson", body: str) -> Path:
    return write_page(root, slug=slug, title=title, body=body, created="2026-09-30")


def _assert_no_term(message: str, *terms: str) -> None:
    """The matched text, as the guard would match it: word-bounded and case-insensitive."""
    for term in terms:
        assert not re.search(rf"\b{re.escape(term)}\b", message, re.IGNORECASE), (
            f"the refusal quoted a matched term: {message!r}"
        )


# --------------------------------------------------------------------------- #
# The writer
# --------------------------------------------------------------------------- #
def test_a_page_naming_the_configured_person_is_refused_by_class_and_line(
    tmp_path: Path, residue_config: str
) -> None:
    root = _pillar(tmp_path)
    body = f"First line, see [[registry]].\nSecond line.\nAsked {residue_config} directly.\n"
    with pytest.raises(WikiFormatError) as caught:
        _write(root, body=body)

    message = str(caught.value)
    # A WikiFormatError, so every caller that already handles the writer's refusals handles
    # this one; its own subclass, so a caller can tell it apart without parsing prose.
    assert type(caught.value).__name__ == "PageResidueError"
    assert "person" in message
    assert "body line 3" in message
    _assert_no_term(message, residue_config)
    assert not (root / "pages" / "a-lesson.md").exists(), "a refused page must not be written"


def test_a_title_naming_the_person_is_refused_and_says_title(
    tmp_path: Path, residue_config: str
) -> None:
    root = _pillar(tmp_path)
    with pytest.raises(WikiFormatError) as caught:
        _write(root, title=f"What {residue_config} decided", body="See [[registry]].\n")
    assert "person" in str(caught.value) and "title" in str(caught.value)
    _assert_no_term(str(caught.value), residue_config)


def test_a_slug_naming_the_person_is_refused_without_quoting_the_slug(
    tmp_path: Path, monkeypatch
) -> None:
    """A slug is matched like the rest. The message cannot name the slug: it IS the term."""
    home = tmp_path / "single-word-config"
    write_environment(home, tmp_path / "single-root", person="Exampleperson")
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    root = _pillar(tmp_path)
    with pytest.raises(WikiFormatError) as caught:
        _write(root, slug="exampleperson-directives", body="See [[registry]].\n")
    assert "person" in str(caught.value) and "slug" in str(caught.value)
    _assert_no_term(str(caught.value), "Exampleperson")


def test_a_sanitize_class_term_is_refused(tmp_path: Path) -> None:
    root = _pillar(tmp_path)
    body = f"See [[registry]].\nThe checkout lived under {HOME_PATH}/work.\n"
    with pytest.raises(WikiFormatError) as caught:
        _write(root, body=body)
    message = str(caught.value)
    assert "sanitize" in message and "body line 2" in message
    _assert_no_term(message, "someacct")
    assert not (root / "pages" / "a-lesson.md").exists()


def test_the_projects_extra_pattern_joins_the_sanitize_class(tmp_path: Path) -> None:
    """The ratchet unions the project's local extra pattern; the writer must refuse the same."""
    root = _pillar(tmp_path)
    extra = root.parent / "publish" / "sanitize-extra-pattern.txt"
    extra.parent.mkdir()
    extra.write_text(f"# placeholder\n{PRODUCT}\n", encoding="utf-8")
    with pytest.raises(WikiFormatError) as caught:
        _write(root, body=f"See [[registry]] and {PRODUCT}.\n")
    assert "sanitize" in str(caught.value) and "body line 1" in str(caught.value)
    _assert_no_term(str(caught.value), PRODUCT)


def test_a_clean_page_writes(tmp_path: Path) -> None:
    root = _pillar(tmp_path)
    path = _write(root, body="The human decided by role. See [[registry]].\n")
    assert path.is_file()
    assert "[[registry]]" in path.read_text(encoding="utf-8")


def test_a_dirty_page_already_on_disk_does_not_block_a_clean_write(
    tmp_path: Path, residue_config: str
) -> None:
    """Only the NEW page is judged. The existing corpus is the ratchet's, and retiring a dirty
    page is a move into ``pages/archived/`` that never goes through the writer."""
    root = _pillar(tmp_path)
    (root / "pages" / "old-dirty.md").write_text(
        f"---\ntitle: old\ncreated: 2026-09-01\n---\n{residue_config} said so. [[registry]]\n",
        encoding="utf-8",
    )
    assert _write(root, body="A clean successor. See [[registry]].\n").is_file()


def test_an_unmeasurable_config_refuses_the_write(tmp_path: Path, monkeypatch) -> None:
    """A page that was not measured is not known to be clean."""
    empty = tmp_path / "no-config-here"
    empty.mkdir()
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(empty))
    root = _pillar(tmp_path)
    with pytest.raises(WikiFormatError) as caught:
        _write(root, body="Entirely clean. See [[registry]].\n")
    assert "cannot be measured" in str(caught.value)
    # The refusal reaches registry rows, so the config location must not ride in it.
    assert str(empty) not in str(caught.value)
    assert "no-config-here" not in str(caught.value)
    assert not (root / "pages" / "a-lesson.md").exists()


def test_an_unusable_extra_pattern_refuses_the_write(tmp_path: Path) -> None:
    """An extra pattern that exists and will not compile is unmeasurable, not 'not configured'."""
    root = _pillar(tmp_path)
    extra = root.parent / "publish" / "sanitize-extra-pattern.txt"
    extra.parent.mkdir()
    extra.write_text("(unclosed\n", encoding="utf-8")
    with pytest.raises(WikiFormatError) as caught:
        _write(root, body="Entirely clean. See [[registry]].\n")
    assert "cannot be measured" in str(caught.value)


# --------------------------------------------------------------------------- #
# The summariser
# --------------------------------------------------------------------------- #
def test_build_prompt_tells_the_summariser_to_name_people_by_role() -> None:
    prompt = summariser.build_prompt("park", "a firing")
    assert "refer to people by role, never by personal name" in prompt.casefold()


@pytest.mark.parametrize("code", [0, 1])
def test_compose_and_save_with_a_reply_naming_the_person_fails_without_the_name(
    tmp_path: Path, residue_config: str, capsys, code: int
) -> None:
    """Exit 1 too: a non-zero exit normally puts the stream excerpts in the reason, and those
    excerpts are the reply - which is where the name is."""
    root = _pillar(tmp_path)
    reply = (
        "SLUG: directives-of-the-day\n"
        f"TITLE: What {residue_config} decided\n"
        "BODY:\n"
        f"{residue_config} decided it. See [[registry]].\n"
    )

    def runner(argv, *, env, timeout, stdin):
        return code, reply, ""

    outcome = summariser.compose_and_save(root, trigger="park", context="x", runner=runner)

    assert outcome.failed
    assert "person" in outcome.reason and "title" in outcome.reason
    rendered = outcome.render() + repr(outcome.as_dict())
    out = capsys.readouterr()
    for text in (outcome.reason, rendered, out.out, out.err):
        _assert_no_term(text, residue_config)
    assert sorted(p.name for p in (root / "pages").glob("*.md")) == []


def test_compose_and_save_with_a_dirty_slug_does_not_echo_the_slug(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The outcome's ``slug`` field renders into the audit line, so a refused slug stays out."""
    home = tmp_path / "single-word-config"
    write_environment(home, tmp_path / "single-root", person="Exampleperson")
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    root = _pillar(tmp_path)
    reply = "SLUG: exampleperson-directives\nTITLE: Directives\nBODY:\nSee [[registry]].\n"

    def runner(argv, *, env, timeout, stdin):
        return 0, reply, ""

    outcome = summariser.compose_and_save(root, trigger="park", context="x", runner=runner)

    assert outcome.failed and outcome.slug is None
    assert "person" in outcome.reason and "slug" in outcome.reason
    out = capsys.readouterr()
    for text in (outcome.render(), repr(outcome.as_dict()), out.out, out.err):
        _assert_no_term(text, "Exampleperson")


def test_residue_is_refused_before_a_refusal_that_would_quote_the_slug(
    tmp_path: Path, monkeypatch
) -> None:
    """A dirty slug on a body with no link: the link refusal quotes the slug, so it must not win."""
    home = tmp_path / "single-word-config"
    write_environment(home, tmp_path / "single-root", person="Exampleperson")
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    root = _pillar(tmp_path)
    with pytest.raises(WikiFormatError) as caught:
        _write(root, slug="exampleperson-notes", body="No link at all.\n")
    assert type(caught.value).__name__ == "PageResidueError"
    _assert_no_term(str(caught.value), "Exampleperson")
