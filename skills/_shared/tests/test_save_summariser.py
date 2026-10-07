"""The summariser spawn: non-recursive, bounded, non-fatal, auditable — and never `claude` itself.

WHY THIS FILE EXISTS. `skills/wiki-save/SKILL.md` left the summariser behind a seam and held the
spawn at Q-10043. Q-10043 was answered on 2026-09-05 — option A, an inline bounded one-shot the
seat launches at its own phase boundary — and `agentflow.save_summariser` is that implementation.
It is the first path in this suite that starts an AGENT SESSION (not the first subprocess: the
tracker CLI and git are already shelled out to elsewhere), so the guardrails around it are the
deliverable and not the trimming.

THE CENTRE OF THIS MODULE IS THE NON-RECURSION TRIO. The spawned session hits phase boundaries of
its own; a save that can trigger a save spawns a save that spawns a save. Three cases, and each
depends on ONE limb alone so that deleting a limb reds a case named for it:

    test_the_spawn_marks_the_child_disabled          — the spawn SETS the marker
    test_a_disabled_session_refuses_to_save          — the entry point REFUSES on the marker
    test_a_save_triggered_session_does_not_itself_save — the two composed

`test_the_non_recursion_trio_is_present` is parametrised over those three names, so removing one
is a red case rather than a quieter suite.

NOTHING HERE EVER SPAWNS `claude`. Every test drives the module through an injected fake runner,
and `_no_real_spawns` is an autouse fixture that replaces the real `subprocess` runner with one
that fails the test on sight — so a case that forgets to inject one is caught by name rather than
by a mysterious timeout on a machine that happens to have the CLI installed.

WHAT THESE CANNOT DO. They cannot prove a real model returns a parseable page, that a page is any
good, or that a live seat calls this at its boundary at all — the seats' own procedures carry that
duty since E-0213, and `test_skill_call_sites.py` gates which five seats hold it.
"""

from __future__ import annotations

import ast
import inspect
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentflow import save_summariser as mod
from agentflow.save_summariser import (
    DEFAULT_TIMEOUT_S,
    DISABLE_ENV,
    MAX_CONTEXT_CHARS,
    NOTHING_DURABLE,
    TRIGGERS,
    SaveOutcome,
    SaveSpawnError,
    build_argv,
    build_prompt,
    compose_and_save,
    computed_created,
    parse_summary,
    saving_disabled,
)
from agentflow.stream_report import STREAM_CHARS
from agentflow.tracked_paths import tracked_files
from agentflow.wiki import GENERATED_MARKER, initial_pillar_files

#: Every write here passes ``write_page``'s residue guard, which resolves the configured person
#: from the environment config. A hermetic one (``conftest.residue_config``), so the suite does
#: not depend on the machine's config: absent on a CI runner, it would refuse every write.
pytestmark = pytest.mark.usefixtures("residue_config")

#: The REAL default runner, captured at import time. `_no_real_spawns` below replaces the module
#: attribute for every test, so the only way to exercise the genuine one is to have kept a
#: reference before the fixture ran.
_REAL_RUNNER = mod._subprocess_runner

GOOD_REPLY = (
    "SLUG: a-lesson-about-locks\n"
    "TITLE: A lock file survives the crash that orphaned it\n"
    "BODY:\n"
    "The in-flight guard is process-local. See [[registry]].\n"
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
#: A resolved shim path, spelled without backslashes so the literal cannot be read as an escape.
_SHIM = "C:/tools/claude.CMD"


@pytest.fixture(autouse=True)
def _no_real_spawns(monkeypatch):
    """No test in this file may start a real process, injected runner or not.

    The default runner is reachable by simply omitting `runner=`, and on a machine that has the
    agent CLI installed that omission would pass — slowly, non-deterministically, and by actually
    running a model. Replacing it with a failing stub turns the omission into a named failure.
    """

    class _RealRunnerReached(BaseException):
        """Deliberately NOT an ``Exception``.

        ``compose_and_save`` catches ``Exception`` on every path (guardrail 3: a failed save
        never raises into the caller's firing). An ``AssertionError`` tripwire is therefore
        swallowed into ``status="failed"``, so a future case asserting only ``outcome.failed``
        would go green having actually reached the real runner. A ``BaseException`` escapes the
        guardrail and fails the test by name, which is what a tripwire is for.
        """

    def _explode(argv, *, env, timeout, stdin):
        raise _RealRunnerReached(
            f"a test reached the REAL subprocess runner (argv={argv[:1]}); every case in this "
            "file must inject a fake runner"
        )

    monkeypatch.setattr(mod, "_subprocess_runner", _explode)


def _seed_pillar(root: Path) -> Path:
    """Lay down what the provisioner lays down: `initial_pillar_files` plus `pages/`.

    Seeded from the provisioner's own definition rather than hand-typed, so this fixture and a
    freshly provisioned project cannot drift apart (gh#501: `compose_and_save` now refuses a root
    that lacks the pillar markers).
    """
    (root / "pages").mkdir(parents=True)
    for name, body in initial_pillar_files("demo").items():
        (root / name).write_text(body, encoding="utf-8", newline="\n")
    return root


@pytest.fixture()
def wiki_root(tmp_path: Path) -> Path:
    """A provisioned, empty wiki pillar. `refresh_projection` refuses a missing root."""
    return _seed_pillar(tmp_path / "wiki")


def _runner(reply: str = GOOD_REPLY, *, code: int = 0, err: str = "", record: list | None = None):
    """A fake runner in the module's shape, recording what it was handed."""

    def run(argv, *, env, timeout, stdin):
        if record is not None:
            record.append({"argv": argv, "env": env, "timeout": timeout})
        return code, reply, err

    return run


def _pages(root: Path) -> list:
    return sorted(p.name for p in (root / "pages").glob("*.md"))


# --------------------------------------------------------------------------- #
# 1. Non-recursion — the guardrail the feature does not ship without
# --------------------------------------------------------------------------- #
_NON_RECURSION_CASES = (
    "test_the_spawn_marks_the_child_disabled",
    "test_a_disabled_session_refuses_to_save",
    "test_a_save_triggered_session_does_not_itself_save",
)


@pytest.mark.parametrize("case", _NON_RECURSION_CASES)
def test_the_non_recursion_trio_is_present(case: str) -> None:
    """One case per limb of the trio, so a limb cannot be deleted without a named failure.

    The trio is three assertions about two mechanisms plus their composition, and a suite that
    keeps only the composition case looks green while either half is quietly removed — the
    composition can be satisfied by the surviving half alone.
    """
    fn = globals().get(case)
    assert callable(fn), (
        f"{case} is gone from this module. It is one limb of the non-recursion guard: the spawn "
        "sets the marker, the entry point refuses on it, and the two compose. Deleting a limb "
        "leaves a save that can spawn a save"
    )


def test_the_spawn_marks_the_child_disabled(wiki_root: Path) -> None:
    """LIMB 1, alone: the environment handed to the child carries the marker.

    Asserted on what the RUNNER received, not on `os.environ`, because the marker is written into
    an explicit mapping rather than inherited — that is the whole reason it cannot be lost across
    the boundary, and asserting it anywhere else would not see it.
    """
    seen: list = []
    compose_and_save(
        wiki_root, trigger="park", context="a firing", runner=_runner(record=seen), environ={}
    )
    assert seen, "the runner was never called, so nothing was asserted about the child"
    child_env = seen[0]["env"]
    assert child_env.get(DISABLE_ENV), (
        f"the child environment does not carry {DISABLE_ENV}: {sorted(child_env)}. The spawned "
        "session reaches phase boundaries of its own, so without the marker it triggers a save "
        "that spawns a save that spawns a save"
    )
    assert saving_disabled(child_env), (
        f"{DISABLE_ENV} is present on the child but does not read as disabled — the setter and "
        "the reader disagree, which is the guard passing on one side only"
    )


@pytest.mark.parametrize("value", ["1", "0", "true", "false", "yes", " x "])
def test_a_disabled_session_refuses_to_save(wiki_root: Path, value: str) -> None:
    """LIMB 2, alone: a session carrying the marker writes nothing and starts nothing.

    Parametrised over values a shell might plausibly carry — including `0` and `false`, which are
    the ones a naive truthiness check would read as permission. The marker's only job is to be
    there; a value the reader does not recognise must never be read as permission.
    """
    seen: list = []
    outcome = compose_and_save(
        wiki_root,
        trigger="halt",
        context="a firing",
        runner=_runner(record=seen),
        environ={DISABLE_ENV: value},
    )
    assert outcome.status == "disabled", f"expected 'disabled', got {outcome.status!r}"
    assert not outcome.wrote
    assert DISABLE_ENV in outcome.reason, "the refusal does not name the variable it fired on"
    assert not seen, "a disabled session still started a process"
    assert _pages(wiki_root) == [], "a disabled session still wrote a page"


def test_a_save_triggered_session_does_not_itself_save(wiki_root: Path) -> None:
    """LIMB 3: THE COMPOSITION. A save-triggered session, given the child's own environment,
    does not save.

    The fake runner stands in for the spawned session: it re-enters `compose_and_save` with
    exactly the environment it was handed, which is what a real child would inherit. Exactly one
    page comes out of the whole thing.

    The inner status is asserted to be `disabled` SPECIFICALLY, not merely "did not write". The
    in-flight guard would also stop the second call, and passing on that would prove the wrong
    mechanism — which is why the entry point checks the marker BEFORE it takes the guard.
    """
    inner: list = []

    def recursive_runner(argv, *, env, timeout, stdin):
        inner.append(
            compose_and_save(
                wiki_root,
                trigger="park",
                context="the child's own firing",
                runner=_runner(),
                environ=env,
            )
        )
        return 0, GOOD_REPLY, ""

    outer = compose_and_save(
        wiki_root, trigger="park", context="the parent firing", runner=recursive_runner, environ={}
    )

    assert outer.wrote, f"the outer save did not write: {outer.render()}"
    assert len(inner) == 1
    assert inner[0].status == "disabled", (
        f"the save-triggered session came back {inner[0].status!r}, not 'disabled'. If it was "
        "stopped by the in-flight guard instead, the non-recursion marker is not what held it — "
        "and the guard is process-local, so it would not hold a real child at all"
    )
    assert _pages(wiki_root) == ["a-lesson-about-locks.md"], (
        f"a save-triggered save left {_pages(wiki_root)}; recursion produced more than one page"
    )


def test_the_spawn_refuses_when_the_marker_cannot_be_set(wiki_root: Path, monkeypatch) -> None:
    """FAIL CLOSED. If the child environment cannot be built carrying the marker, nothing starts.

    The direction is the point: a lost marker must cost a page, never produce an unguarded child.
    """
    monkeypatch.setattr(mod, "DISABLE_VALUE", "")  # a marker that does not read as set
    seen: list = []
    outcome = compose_and_save(
        wiki_root, trigger="block", context="c", runner=_runner(record=seen), environ={}
    )
    assert outcome.status == "failed"
    assert not seen, "a process was started with an environment that does not carry the marker"
    assert _pages(wiki_root) == []


def test_child_env_raises_rather_than_returning_an_unmarked_mapping(monkeypatch) -> None:
    """The refusal is an exception, not a flag a caller can forget to check."""
    assert mod._child_env({"PATH": "x"})[DISABLE_ENV]
    monkeypatch.setattr(mod, "DISABLE_VALUE", "")
    with pytest.raises(SaveSpawnError):
        mod._child_env({"PATH": "x"})


# --------------------------------------------------------------------------- #
# 2. Bounded and single
# --------------------------------------------------------------------------- #
def test_the_runner_is_given_a_timeout_and_is_called_exactly_once(wiki_root: Path) -> None:
    """One call per boundary, with a bound on it. No retry lives anywhere in this path."""
    seen: list = []
    compose_and_save(
        wiki_root, trigger="end-of-epic", context="c", runner=_runner(record=seen), environ={}
    )
    assert len(seen) == 1, f"the summariser was called {len(seen)} times; a boundary gets one call"
    assert seen[0]["timeout"] == DEFAULT_TIMEOUT_S


def test_a_failing_call_is_not_retried(wiki_root: Path) -> None:
    """A retry storm is the failure mode a tired seat cannot see. There is no second attempt."""
    seen: list = []
    outcome = compose_and_save(
        wiki_root,
        trigger="park",
        context="c",
        runner=_runner("", code=3, err="boom", record=seen),
        environ={},
    )
    assert len(seen) == 1, f"a failed summariser was called {len(seen)} times"
    assert outcome.failed


def test_a_second_spawn_cannot_start_while_one_is_in_flight(wiki_root: Path) -> None:
    """A seat with a spawn running does not start another.

    The inner call is given an environment WITHOUT the marker, so the non-recursion refusal is
    out of the way and this case depends on the in-flight guard alone.
    """
    inner: list = []

    def reentrant(argv, *, env, timeout, stdin):
        inner.append(
            compose_and_save(
                wiki_root, trigger="park", context="c", runner=_runner(), environ={}
            )
        )
        return 0, GOOD_REPLY, ""

    compose_and_save(
        wiki_root, trigger="park", context="c", runner=reentrant, environ={}
    )
    assert inner[0].status == "in-flight", (
        f"a second spawn started while one was in flight (got {inner[0].status!r})"
    )


def test_the_in_flight_guard_is_released_after_a_raising_runner(wiki_root: Path) -> None:
    """The guard cannot go stale into a permanent refusal — including after an exception.

    Represented by a process-local lock released under `try/finally`. A first call whose runner
    raised must leave the next call free; if it did not, one bad firing would disable saving for
    the life of the seat and nothing would say so.
    """

    def raiser(argv, *, env, timeout, stdin):
        raise RuntimeError("the child died")

    first = compose_and_save(wiki_root, trigger="park", context="c", runner=raiser, environ={})
    assert first.failed

    second = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(), environ={}
    )
    assert second.wrote, (
        f"the second save came back {second.status!r}: the in-flight guard was not released "
        "after a failure, so one bad firing wedged saving permanently"
    )


def test_the_context_handed_to_the_child_is_bounded(wiki_root: Path) -> None:
    """A prompt cannot grow with the firing. Truncation is visible, not silent."""
    prompt = build_prompt("park", "x" * (MAX_CONTEXT_CHARS * 3))
    assert "[context truncated]" in prompt
    assert len(prompt) < MAX_CONTEXT_CHARS * 2, (
        f"the prompt is {len(prompt)} chars for a context capped at {MAX_CONTEXT_CHARS}"
    )


# --------------------------------------------------------------------------- #
# 3. Non-fatal, and loud
# --------------------------------------------------------------------------- #
_FAILURE_MODES = {
    "runner-raises": None,
    "non-zero-exit": ("", 7, "the model refused"),
    "timeout": ("", -1, "timed out after 180s"),
    "empty-output": ("", 0, ""),
    "whitespace-output": ("   \n\n", 0, ""),
    "no-slug": ("TITLE: t\nBODY:\nsee [[x]]\n", 0, ""),
    "no-title": ("SLUG: s\nBODY:\nsee [[x]]\n", 0, ""),
    "no-body-marker": ("SLUG: s\nTITLE: t\nsee [[x]]\n", 0, ""),
    "empty-body": ("SLUG: s\nTITLE: t\nBODY:\n\n", 0, ""),
    "no-wikilink": ("SLUG: s\nTITLE: t\nBODY:\nnothing links out\n", 0, ""),
    "slug-with-separator": ("SLUG: a/b\nTITLE: t\nBODY:\nsee [[x]]\n", 0, ""),
}


@pytest.mark.parametrize("mode", sorted(_FAILURE_MODES))
def test_every_failure_writes_nothing_says_why_and_never_raises(
    wiki_root: Path, mode: str
) -> None:
    """One case per failure path. Each returns `failed` with a reason and leaves `pages/` empty.

    Parametrised per mode rather than asserted over one representative failure: these are eleven
    separate branches, and a single-failure test lets ten of them regress into either a raise
    (which would kill the firing) or a silently written empty page (which is the failure the
    inherited requirement names by name).
    """
    spec = _FAILURE_MODES[mode]
    if spec is None:
        def runner(argv, *, env, timeout, stdin):
            raise OSError("no such binary")
    else:
        reply, code, err = spec
        runner = _runner(reply, code=code, err=err)

    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=runner, environ={}
    )
    assert outcome.status == "failed", f"{mode}: expected 'failed', got {outcome.status!r}"
    assert not outcome.wrote
    assert outcome.reason.strip(), f"{mode}: failed with an empty reason — a silent failure"
    assert _pages(wiki_root) == [], f"{mode}: a page was written anyway: {_pages(wiki_root)}"


# --------------------------------------------------------------------------- #
# gh#501 - a root that is not a provisioned pillar is refused BEFORE any spawn
# --------------------------------------------------------------------------- #
# The 2026-09-07 mis-aim: a save was handed the personal vault root, `write_page`'s mkdir made a
# `pages/` there, and the page landed outside every pillar. Each shape below is one real way a root
# can look like a pillar without being one; the assertions name MARKERS, never the reason's prose.
def _files_under(root: Path) -> list:
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _vault_root_shape(root: Path) -> None:
    """The mis-aim's own debris: a GENERATED hot.md and a pages/, and no digest.md."""
    (root / "pages").mkdir(parents=True)
    (root / "hot.md").write_text(initial_pillar_files("demo")["hot.md"], encoding="utf-8")


def _vault_wiki_shape(root: Path) -> None:
    """A human hot cache (frontmatter, no GENERATED marker) beside a digest.md and a pages/."""
    (root / "pages").mkdir(parents=True)
    (root / "hot.md").write_text("---\ntype: hot\n---\n# Hot cache\n\nhand-written\n", encoding="utf-8")
    (root / "digest.md").write_text(initial_pillar_files("demo")["digest.md"], encoding="utf-8")


def _no_pages_shape(root: Path) -> None:
    """Both scaffolded files and no pages/ - write_page's mkdir would fabricate the store."""
    root.mkdir(parents=True)
    for name, body in initial_pillar_files("demo").items():
        (root / name).write_text(body, encoding="utf-8")


_NOT_A_PILLAR = {
    # shape -> (builder, the marker the reason must name)
    "empty-dir": (lambda r: r.mkdir(parents=True), "digest.md"),
    "vault-root": (_vault_root_shape, "digest.md"),
    "vault-wiki": (_vault_wiki_shape, "GENERATED"),
    "no-pages": (_no_pages_shape, "pages/"),
}


@pytest.mark.parametrize("shape", sorted(_NOT_A_PILLAR))
def test_a_root_that_is_not_a_provisioned_pillar_is_refused_before_any_spawn(
    tmp_path: Path, shape: str
) -> None:
    """gh#501: `failed`, the reason names the missing marker, the runner is never called, and
    nothing is created under the root - no pages/, no page, no hot*.md."""
    build, marker = _NOT_A_PILLAR[shape]
    root = tmp_path / "not-a-pillar"
    build(root)
    before = _files_under(root)
    calls: list = []

    outcome = compose_and_save(
        root, trigger="park", context="c", lane="jr-dev-2", runner=_runner(record=calls), environ={}
    )

    assert outcome.status == "failed", f"{shape}: {outcome.render()}"
    assert marker in outcome.reason, f"{shape}: the reason does not name {marker!r}: {outcome.reason}"
    assert calls == [], f"{shape}: the summariser was spawned for a save that could never land"
    assert _files_under(root) == before, (
        f"{shape}: the refused save still changed the root: {before} -> {_files_under(root)}"
    )


def test_a_nonexistent_root_is_refused_and_not_fabricated(tmp_path: Path) -> None:
    """write_page's `mkdir(parents=True)` must no longer be able to conjure a pillar."""
    root = tmp_path / "projects" / "typo" / "wiki"
    calls: list = []
    outcome = compose_and_save(
        root, trigger="park", context="c", runner=_runner(record=calls), environ={}
    )
    assert outcome.status == "failed", outcome.render()
    assert calls == []
    assert not root.exists(), f"a refused save fabricated {root}"
    assert not (tmp_path / "projects").exists()


def test_a_rewritten_digest_still_counts_as_a_pillar(wiki_root: Path) -> None:
    """The check is EXISTENCE-only. The steward rewrites digest.md (its heading changed to an
    em-dash); a content check would fail every save the first time it did."""
    (wiki_root / "digest.md").write_text(
        "# demo \N{EM DASH} fleet digest\n\nwhatever the steward wrote\n", encoding="utf-8"
    )
    outcome = compose_and_save(wiki_root, trigger="park", context="c", runner=_runner(), environ={})
    assert outcome.wrote, outcome.render()


def test_the_disabled_refusal_still_comes_before_the_pillar_check(tmp_path: Path) -> None:
    """Guard order: a save-started session reports `disabled` even when the root is also bad."""
    outcome = compose_and_save(
        tmp_path / "nowhere", trigger="park", context="c", runner=_runner(),
        environ={DISABLE_ENV: "1"},
    )
    assert outcome.status == "disabled", outcome.render()


def test_the_pillar_check_comes_before_the_in_flight_guard(wiki_root: Path, tmp_path: Path) -> None:
    """Guard order, the other side: a mis-aimed save made while another spawn holds the lock must
    still report `failed` naming the markers. Checked inside the lock it would read `in-flight` -
    a benign skip that hides exactly the mis-aim gh#501 exists to surface."""
    inner: list = []

    def reentrant(argv, *, env, timeout, stdin):
        inner.append(
            compose_and_save(
                tmp_path / "nowhere", trigger="park", context="c", runner=_runner(), environ={}
            )
        )
        return 0, GOOD_REPLY, ""

    compose_and_save(wiki_root, trigger="park", context="c", runner=reentrant, environ={})
    assert inner and inner[0].status == "failed", inner[0].render() if inner else "never called"
    assert "the directory itself" in inner[0].reason


def test_the_seeded_fixture_carries_the_generated_marker(wiki_root: Path) -> None:
    """Pins the fixture itself, so the positive cases above are not passing on a vault-shaped root."""
    assert GENERATED_MARKER in (wiki_root / "hot.md").read_text(encoding="utf-8")
    assert (wiki_root / "digest.md").is_file() and (wiki_root / "pages").is_dir()


def test_an_unknown_trigger_is_a_reason_not_a_crash(wiki_root: Path) -> None:
    """The trigger is a checked value: an audit line nobody can group by is not an audit line."""
    outcome = compose_and_save(
        wiki_root, trigger="whenever", context="c", runner=_runner(), environ={}
    )
    assert outcome.failed and "whenever" in outcome.reason


def test_nothing_durable_is_not_a_failure(wiki_root: Path) -> None:
    """Most firings learn nothing durable. That outcome must not render as a broken save.

    A status of `failed` here would train a seat to ignore the failure line entirely, which is
    how the loud-failure requirement dies without anyone editing it.
    """
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(NOTHING_DURABLE), environ={}
    )
    assert outcome.status == "nothing-durable"
    assert not outcome.wrote and not outcome.failed
    assert outcome.spawned is True
    assert outcome.reason.strip()
    assert _pages(wiki_root) == []


@pytest.mark.parametrize(
    "reply",
    [
        f"`{NOTHING_DURABLE}`",
        f"{NOTHING_DURABLE}.",
        "Nothing durable",
        f'"{NOTHING_DURABLE}"\n',
        f"```\n{NOTHING_DURABLE}\n```",
    ],
)
def test_a_wrapped_or_recased_nothing_durable_is_still_nothing_durable(
    wiki_root: Path, reply: str
) -> None:
    """gh#971: the prompt shows the token in backticks, so the model echoes it back that way.

    Reading that as a malformed page records the most common outcome as a failed save.
    """
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(reply), environ={}
    )
    assert outcome.status == "nothing-durable", outcome.render()
    assert _pages(wiki_root) == []
    with pytest.raises(ValueError, match="is not a page"):
        parse_summary(reply)


def test_a_reply_that_only_mentions_nothing_durable_is_not_the_token(wiki_root: Path) -> None:
    """The normalisation strips wrapping, it does not search: a sentence is not the token."""
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c",
        runner=_runner(f"I think the answer is {NOTHING_DURABLE}"), environ={},
    )
    assert outcome.status == "failed"


def test_a_fenced_reply_writes_a_page_with_no_stray_fence(wiki_root: Path) -> None:
    """gh#971: a wrapping fence left in the body renders the rest of the page as code."""
    reply = "```\nSLUG: fenced-page\nTITLE: A fenced page\nBODY:\nLesson text, see [[other]].\n```"
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(reply), lane="lead-dev",
        environ={},
    )
    assert outcome.status == "wrote", outcome.render()
    assert _pages(wiki_root) == ["fenced-page.md"]
    assert "```" not in (wiki_root / "pages" / "fenced-page.md").read_text(encoding="utf-8")


def test_a_tilde_fenced_reply_and_token_are_unwrapped_like_backtick_ones() -> None:
    """``~~~`` is the other CommonMark fence; left in, it unbalances the page the same way."""
    _, _, body = parse_summary("~~~\nSLUG: s\nTITLE: t\nBODY:\nsee [[x]]\n~~~")
    assert "~~~" not in body
    assert mod.is_nothing_durable("~~~\nNOTHING DURABLE\n~~~")


def test_a_body_that_ends_in_its_own_code_block_keeps_the_closing_fence() -> None:
    """Only a WRAPPING fence is stripped; a body's own closing fence is not."""
    reply = "SLUG: s\nTITLE: t\nBODY:\nsee [[x]]\n```\ncode\n```\n"
    _, _, body = parse_summary(reply)
    assert body.count("```") == 2


def test_a_slug_with_a_trailing_md_is_written_without_a_doubled_extension(
    wiki_root: Path,
) -> None:
    reply = "SLUG: my-lesson.md\nTITLE: t\nBODY:\nsee [[x]]\n"
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(reply), environ={}
    )
    assert outcome.status == "wrote", outcome.render()
    assert outcome.slug == "my-lesson"
    assert _pages(wiki_root) == ["my-lesson.md"]


@pytest.mark.parametrize("slug", ["My Lesson Here", "my lesson", "My-Lesson", "a--b", "-a", "a_b"])
def test_a_non_kebab_slug_is_refused_as_a_failed_save(wiki_root: Path, slug: str) -> None:
    """gh#971: refused, not normalised; the create-only store would keep a bad name forever."""
    reply = f"SLUG: {slug}\nTITLE: t\nBODY:\nsee [[x]]\n"
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(reply), environ={}
    )
    assert outcome.status == "failed", outcome.render()
    assert "kebab-case" in outcome.reason
    assert slug not in outcome.reason
    assert _pages(wiki_root) == []


def test_a_refresh_failure_still_reports_the_page_as_written(
    wiki_root: Path, monkeypatch
) -> None:
    """The page is on disk. Reporting `wrote=False` would be a second lie on top of the first.

    The recovery is a projection rebuild, not a rewrite of the page, so the outcome says written
    AND says the hot tier is behind.
    """

    def boom(root, **kw):
        raise OSError("hot file is read-only")

    monkeypatch.setattr(mod, "refresh_projection", boom)
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(), environ={}
    )
    assert outcome.wrote
    assert "refresh_projection" in outcome.reason
    assert _pages(wiki_root) == ["a-lesson-about-locks.md"]


# --------------------------------------------------------------------------- #
# 4. Auditable
# --------------------------------------------------------------------------- #
def test_the_outcome_records_the_trigger_the_start_the_duration_and_the_slug(
    wiki_root: Path,
) -> None:
    """Guardrail 4's fields, asserted individually — this record is what survives of Q-10015's
    "every spawn is auditable" once the reconciler does not exist."""
    outcome = compose_and_save(
        wiki_root, trigger="end-of-epic", context="c", runner=_runner(), environ={}
    )
    assert outcome.trigger == "end-of-epic"
    assert outcome.started_at.endswith("Z") and len(outcome.started_at) == 20
    assert outcome.duration_s >= 0.0
    assert outcome.exit_code == 0
    assert outcome.wrote is True
    assert outcome.slug == "a-lesson-about-locks"
    assert outcome.path and outcome.path.endswith("a-lesson-about-locks.md")


def test_the_audit_line_carries_the_reason_on_a_failure(wiki_root: Path) -> None:
    """A rendered line that omits WHY is a line a reader has to go looking behind."""
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner("", code=9, err="the model refused"),
        environ={},
    )
    line = outcome.render()
    assert "trigger=park" in line and "wrote=no" in line and "exit=9" in line
    assert "the model refused" in line


# --------------------------------------------------------------------------- #
# 4b. The reason names BOTH streams — gh#478
#
# The line under all of this used to be `(stderr or stdout or "")[:200]`. On this toolchain
# stderr carries an ADVISORY that accompanies successful runs too (a probe on another model
# exited 0 and still printed it), and stdout carries the line that ended the run — so the
# reason reported the advisory every time, and the single 200-char budget meant the real cause
# could not appear even as a tail. It produced gh#451 titled after the wrong cause, B-0023 on
# the registry with the same wrong cause, and Q-10080 — a question put to a human on a false
# premise. Five seats repeated it for about ten sweeps.
#
# Each case below pins ONE limb of the repair, so reverting that limb reds a case named for
# it, and `test_the_stream_assertions_fail_against_the_bug_and_against_its_mirror` is the
# positive control: it feeds the same assertions the buggy line and its inversion and proves
# they go red.
# --------------------------------------------------------------------------- #
#: The measured pair from gh#478, in the arrangement that fooled five seats: the harmless
#: line on stderr, the terminating one on stdout.
_ADVISORY = (
    "Warning: the requested model is unavailable to this account and a fallback was used. "
    "This notice is also printed on runs that succeed."
)
_TERMINATING = "Claude usage limit reached; this session cannot continue (resets 18:00Z)"


def _failed_reason(wiki_root: Path, *, out: str = "", err: str = "", code: int = 9) -> str:
    """Drive one non-zero exit through the module and hand back the reason a human would read."""
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(out, code=code, err=err), environ={}
    )
    assert outcome.status == "failed" and outcome.exit_code == code
    return outcome.reason


def _legacy_reason(code: int, stdout: str, stderr: str) -> str:
    """gh#478's line, reconstructed verbatim, for the positive control to fail against."""
    return f"the summariser exited {code}: {(stderr or stdout or '').strip()[:200]}"


def _inverted_reason(code: int, stdout: str, stderr: str) -> str:
    """The repair the issue rules out by name: the same line with the precedence flipped.

    It is not half a fix. The module's own default runner reports a timeout and a missing CLI
    as `(<negative code>, "", "<text>")` — cause on stderr, empty stdout — so this ordering hides those the
    way the original hides a model limit.
    """
    return f"the summariser exited {code}: {(stdout or stderr or '').strip()[:200]}"


def _attributes(reason: str, *, stdout: str, stderr: str) -> bool:
    """True when `reason` attributes each stream's text to the stream that carried it."""
    return f"stdout: {stdout}" in reason and f"stderr: {stderr}" in reason


def test_a_failure_whose_cause_arrived_on_stderr_is_attributed_to_stderr(wiki_root: Path) -> None:
    """The stream the old line happened to get right must stay right, and stay LABELLED.

    Labelled is the load-bearing half. An unlabelled cause is what let an advisory be read as
    one; a reader who can see `stderr:` in front of the text can weigh it.
    """
    reason = _failed_reason(wiki_root, err="the model refused")
    assert "stderr: the model refused" in reason
    assert "stdout: <empty>" in reason


def test_a_failure_whose_cause_arrived_on_stdout_is_attributed_to_stdout(wiki_root: Path) -> None:
    """The mirror case, and the one the old line could only get right by accident.

    With stderr empty, `stderr or stdout` did fall through to stdout — so this case is guarded
    by the LABEL, not by the text: the old reason showed the cause with nothing saying where
    it came from, and the assertion below is on the attribution.
    """
    reason = _failed_reason(wiki_root, out="the model refused")
    assert "stdout: the model refused" in reason
    assert "stderr: <empty>" in reason


def test_both_streams_survive_when_both_spoke(wiki_root: Path) -> None:
    """Neither stream may be dropped. This is the case the old line could not represent at all."""
    reason = _failed_reason(wiki_root, out="a cause", err="an advisory")
    assert _attributes(reason, stdout="a cause", stderr="an advisory")


def test_a_process_that_said_nothing_says_so_on_both_streams(wiki_root: Path) -> None:
    """Silence is a diagnosis, and it must not render as a formatting bug.

    A bare `stdout:` with nothing after it sends a reader looking for text that was never
    there; `<empty>` says the stream was read and had nothing, which — with both empty — means
    the exit code is the whole story.
    """
    reason = _failed_reason(wiki_root, code=-1)
    assert "stdout: <empty>" in reason and "stderr: <empty>" in reason
    assert "exited -1" in reason
    assert "stdout:;" not in reason and not reason.rstrip().endswith("stderr:")


def test_the_measured_gh478_shape_shows_the_terminating_cause(wiki_root: Path) -> None:
    """THE case. The exact arrangement from the issue: advisory on stderr, cause on stdout.

    Asserted on the cause being VISIBLE and ATTRIBUTED, not on the advisory being suppressed —
    the advisory is real output and hiding it would be a second editorial guess about which
    stream matters, which is the mistake one level up from the bug.
    """
    reason = _failed_reason(wiki_root, out=_TERMINATING, err=_ADVISORY)
    assert f"stdout: {_TERMINATING}" in reason, (
        "the terminating cause is missing from the reason a human reads — this is gh#478 back: "
        f"{reason!r}"
    )
    assert "stderr:" in reason and _ADVISORY[:40] in reason
    assert reason.index("stdout:") < reason.index("stderr:"), (
        "stdout is printed first because it is the stream this module otherwise parses; the "
        "order is a reading convenience and both streams are always present"
    )


def test_the_budget_is_per_stream_so_an_advisory_cannot_starve_the_cause(wiki_root: Path) -> None:
    """The second half of gh#478: one budget over one winner is how the cause got squeezed out.

    A 3,000-character advisory on stderr must not consume the allowance that carries a short
    cause on stdout. The truncation is also MARKED and COUNTED — a silently shortened stream
    reads as complete, which is the failure this whole section exists to end.
    """
    advisory = "advisory " * 400
    reason = _failed_reason(wiki_root, out=_TERMINATING, err=advisory)
    assert f"stdout: {_TERMINATING}" in reason, f"a long stderr starved stdout: {reason!r}"
    assert "chars)" in reason, "the clamped stream does not say it was clamped"
    assert len(reason) < 4 * STREAM_CHARS, (
        f"the reason grew to {len(reason)} chars; the per-stream clamp is not being applied"
    )

    # THE MIRROR, and it is the direction that actually discriminates. A SHARED budget spent
    # over the joined string survives the case above, because stdout is rendered first and a
    # short cause there fits inside any sane clamp — a mutant that replaced the per-stream
    # budget with a shared one stayed green on it. Only a long stdout can prove the second
    # stream still has a budget of its own: under a shared clamp the stderr label and its
    # text are cut off entirely, which is the starvation this assertion is named after.
    chatty = "chatty stdout " * 400
    mirrored = _failed_reason(wiki_root, out=chatty, err=_TERMINATING)
    assert f"stderr: {_TERMINATING}" in mirrored, (
        f"a long stdout starved stderr; the budget is shared, not per stream: {mirrored!r}"
    )
    assert len(mirrored) < 4 * STREAM_CHARS


def test_the_reason_can_be_written_into_the_registry_it_is_destined_for(wiki_root: Path) -> None:
    """A reason that cannot be recorded is a reason nobody reads, which is gh#478 in a new coat.

    These strings travel the fleet's registry, a markdown table whose mediated writer refuses
    the ENTIRE write on one `|` or newline in a free-text field. The first cut of this fix
    separated the two labelled streams with ` | ` — which would have made the one reason this
    epic exists to deliver unwritable through the only path that delivers it.

    Checked against `registry._require_inline` ITSELF, not a copy of its rule. The guard and
    this assertion then move together; a local re-implementation is a second copy that stops
    tracking the first one the day it changes.
    """
    from agentflow import registry as registry_mod

    piped = "fatal: run `git log | head -5` | tee out.txt"
    reason = _failed_reason(wiki_root, code=7, out=piped, err="advisory\nwrapped\nthrice")
    registry_mod._require_inline(reason, "reason")
    registry_mod._require_inline(reason, "text")
    # The substitution is visible, not silent: the text is still there, one character changed.
    assert "git log / head -5" in reason and "|" not in reason
    assert "stdout:" in reason and "stderr: advisory wrapped thrice" in reason


def test_the_reason_stays_one_line_however_the_child_wrapped_it(wiki_root: Path) -> None:
    """`render()` emits ONE audit line. A raw multi-line advisory would split it into several,
    and a reader scanning a log cannot tell a continuation from the next event."""
    reason = _failed_reason(wiki_root, out="cause line\nsecond line", err="advisory\nwrapped")
    assert "\n" not in reason
    assert "cause line second line" in reason


def test_the_stream_assertions_fail_against_the_bug_and_against_its_mirror() -> None:
    """POSITIVE CONTROL: the cases above can fail, and they fail on both wrong answers.

    A suite where the guarded property holds by accident teaches nothing. Feeding the same
    check the original line AND the `stdout or stderr` inversion shows each dropping a stream
    — which is also the argument, in executable form, for why flipping the precedence is not
    the fix.
    """
    legacy = _legacy_reason(9, _TERMINATING, _ADVISORY)
    assert not _attributes(legacy, stdout=_TERMINATING, stderr=_ADVISORY)
    assert _TERMINATING not in legacy, "the control is stale: the bug no longer drops stdout"

    inverted = _inverted_reason(9, _TERMINATING, _ADVISORY)
    assert not _attributes(inverted, stdout=_TERMINATING, stderr=_ADVISORY)
    assert _ADVISORY not in inverted, "the mirror drops stderr, which is the timeout/CLI cause"

    # ...and the real thing passes the check the two above fail.
    assert _attributes(
        f"the summariser exited 9; {mod.describe_streams(_TERMINATING, _ADVISORY)}",
        stdout=_TERMINATING,
        stderr=_ADVISORY,
    )


def test_the_audit_record_serialises_whole() -> None:
    """`as_dict` must not drop a field, or a JSON consumer sees a shorter record than a reader."""
    outcome = SaveOutcome(trigger="park", status="failed", started_at="2026-09-05T00:00:00Z",
                          duration_s=1.0, reason="r")
    assert set(outcome.as_dict()) >= {
        "trigger", "status", "started_at", "duration_s", "exit_code", "wrote", "spawned", "slug",
        "reason", "word_count",
    }


# --------------------------------------------------------------------------- #
# 4b. `spawned` — "nothing ran" and "it ran and failed" are different lines (gh#437)
# --------------------------------------------------------------------------- #
def _counting(inner, calls: list):
    """Wrap a runner so a case can assert how many times the save called it."""

    def run(argv, *, env, timeout, stdin):
        calls.append(argv)
        return inner(argv, env=env, timeout=timeout, stdin=stdin)

    return run


def test_a_never_started_child_is_failed_with_spawned_no(wiki_root: Path) -> None:
    """The DEFAULT runner on a missing binary: `failed`, `spawned=no`, one call, no retry.

    `subprocess.Popen` is replaced with what the OS does for a binary it cannot find, so no real
    process is involved; what is under test is the runner's mapping and the save's reading of it.
    """

    def _absent(argv, **kw):
        raise FileNotFoundError(2, "absent")

    calls: list = []
    real_which, real_run = mod.shutil.which, mod.subprocess.Popen
    try:
        mod.shutil.which = lambda name: None
        mod.subprocess.Popen = _absent
        outcome = compose_and_save(
            wiki_root, trigger="park", context="c", runner=_counting(_REAL_RUNNER, calls),
            environ={},
        )
    finally:
        mod.shutil.which, mod.subprocess.Popen = real_which, real_run

    assert outcome.status == "failed", outcome.render()
    assert outcome.exit_code == mod.NEVER_STARTED
    assert outcome.spawned is False
    assert "spawned=no" in outcome.render()
    assert outcome.as_dict()["spawned"] is False
    assert len(calls) == 1, f"a never-started spawn was attempted {len(calls)} times (no retry)"


def test_a_started_child_that_exits_non_zero_is_failed_with_spawned_yes(wiki_root: Path) -> None:
    """A process that started and exited non-zero ran. cmd.exe's 'not recognized' is this case."""
    calls: list = []
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c",
        runner=_counting(_runner("", code=1, err="'claude' is not recognized"), calls),
        environ={},
    )
    assert outcome.status == "failed", outcome.render()
    assert outcome.spawned is True
    assert "spawned=yes" in outcome.render()
    assert outcome.as_dict()["spawned"] is True
    assert len(calls) == 1, f"a failed summariser was called {len(calls)} times (no retry)"


def test_a_timeout_counts_as_spawned(wiki_root: Path) -> None:
    """The timeout's `-1` is a process that started; only NEVER_STARTED reads as `spawned=no`."""
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c",
        runner=_runner("", code=-1, err="timed out after 180s"), environ={},
    )
    assert outcome.failed and outcome.spawned is True, outcome.render()


def test_a_clean_write_is_spawned_yes(wiki_root: Path) -> None:
    outcome = compose_and_save(wiki_root, trigger="park", context="c", runner=_runner(), environ={})
    assert outcome.wrote and outcome.spawned is True, outcome.render()


def test_an_unparseable_reply_is_failed_with_spawned_yes(wiki_root: Path) -> None:
    """The parse-failure branch still reports the process that produced the reply."""
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner("no shape at all"), environ={}
    )
    assert outcome.failed and outcome.spawned is True, outcome.render()


def test_a_disabled_save_renders_spawned_dash(wiki_root: Path) -> None:
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(), environ={DISABLE_ENV: "1"}
    )
    assert outcome.status == "disabled"
    assert outcome.spawned is None
    assert "spawned=-" in outcome.render()
    assert outcome.as_dict()["spawned"] is None


def test_an_in_flight_skip_renders_spawned_dash(wiki_root: Path) -> None:
    inner: list = []

    def reentrant(argv, *, env, timeout, stdin):
        inner.append(
            compose_and_save(wiki_root, trigger="park", context="c", runner=_runner(), environ={})
        )
        return 0, GOOD_REPLY, ""

    compose_and_save(wiki_root, trigger="park", context="c", runner=reentrant, environ={})
    assert inner[0].status == "in-flight"
    assert inner[0].spawned is None
    assert "spawned=-" in inner[0].render()


def test_a_raising_injected_runner_renders_spawned_dash(wiki_root: Path) -> None:
    def raiser(argv, *, env, timeout, stdin):
        raise RuntimeError("the child died")

    outcome = compose_and_save(wiki_root, trigger="park", context="c", runner=raiser, environ={})
    assert outcome.failed and outcome.spawned is None, outcome.render()


# --------------------------------------------------------------------------- #
# 5. The write path — `created` is COMPUTED, never typed
# --------------------------------------------------------------------------- #
def test_created_is_not_a_parameter_anywhere_a_caller_can_reach() -> None:
    """The strongest form of "never typed" is a caller who cannot supply it.

    `_validated_created` checks SHAPE only, and `select()` ranks `created` lexicographically over
    a create-only store — so one mistyped date pins a page to the top of the hot tier permanently,
    correctable only by a newer page the bad one still outranks. A keyword nobody can pass is a
    rule that cannot be forgotten at a phase boundary.
    """
    params = inspect.signature(compose_and_save).parameters
    assert "created" not in params, (
        f"compose_and_save accepts `created` ({sorted(params)}). The inherited requirement is "
        "that it is computed, never typed"
    )


def test_the_written_page_carries_the_computed_date_and_the_lane(wiki_root: Path) -> None:
    """The page on disk, read back — not the arguments that went in.

    The expected date is computed HERE, from the stdlib, rather than by calling
    `computed_created()`. Asserting a function against itself moves both sides of the comparison
    together: a `computed_created` hardcoded to a constant passed that version of this test,
    which is precisely the stale-date defect the inherited requirement exists to prevent.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    compose_and_save(
        wiki_root, trigger="park", context="c", lane="jr-dev-1", runner=_runner(), environ={}
    )
    text = (wiki_root / "pages" / "a-lesson-about-locks.md").read_text(encoding="utf-8")
    assert f"created: '{today}'" in text or f"created: {today}" in text, (
        f"the page's `created` is not today ({today}): {text.splitlines()[:5]}"
    )
    assert "lane: jr-dev-1" in text
    assert "[[registry]]" in text


def test_created_follows_the_clock_rather_than_a_constant(monkeypatch) -> None:
    """A moved clock must move the date. A constant would rank every page identically forever.

    `select()` ranks `created` lexicographically descending over a create-only store, so a stuck
    or invented date is not a cosmetic wrong field — it decides which pages a seat ever reads.
    """
    before = computed_created()
    monkeypatch.setattr(
        mod, "_now_utc", lambda: datetime(2031, 12, 25, tzinfo=timezone.utc)
    )
    assert computed_created() == "2031-12-25", (
        f"computed_created() ignored a moved clock (still {computed_created()!r}, was {before!r})"
    )


def test_a_successful_save_refreshes_the_projection(wiki_root: Path) -> None:
    """`write_page` without `refresh_projection` leaves the page invisible to every seat that
    reads the hot tier. Both calls, or the store grows a page nobody meets."""
    compose_and_save(
        wiki_root, trigger="park", context="c", lane="steward", runner=_runner(), environ={}
    )
    assert (wiki_root / "hot.md").is_file()
    assert (wiki_root / "hot-steward.md").is_file()
    assert "a-lesson-about-locks" in (wiki_root / "hot.md").read_text(encoding="utf-8")


def test_a_taken_slug_is_written_once_under_a_suffix_that_links_back(wiki_root: Path) -> None:
    """`pages/` is create-only, and a finished draft is not thrown away over its name.

    The create-only rule's own remedy is a NEW page that links to the existing one. The draft
    still exists only here, so this is the one place that remedy can be applied at no extra
    model cost: one retry under `<slug>-2`, carrying a link to `<slug>`.
    """
    first = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(), environ={}
    )
    original = (wiki_root / "pages" / "a-lesson-about-locks.md").read_text(encoding="utf-8")
    second = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(), environ={}
    )
    assert first.wrote and second.wrote, second.render()
    assert second.slug == "a-lesson-about-locks-2"
    assert second.path and second.path.endswith("a-lesson-about-locks-2.md")
    assert second.reason == "slug a-lesson-about-locks was taken; wrote a-lesson-about-locks-2"
    assert _pages(wiki_root) == ["a-lesson-about-locks-2.md", "a-lesson-about-locks.md"]
    suffixed = (wiki_root / "pages" / "a-lesson-about-locks-2.md").read_text(encoding="utf-8")
    assert "Earlier page under the same name: [[a-lesson-about-locks]]" in suffixed
    assert "The in-flight guard is process-local." in suffixed
    # Never an overwrite: the first page is byte-for-byte what it was.
    assert (wiki_root / "pages" / "a-lesson-about-locks.md").read_text(
        encoding="utf-8"
    ) == original


def test_a_double_collision_fails_naming_both_slugs_and_tries_no_third(wiki_root: Path) -> None:
    """One retry, not a counter. A third save of one slug writes nothing and names both."""
    for _ in range(2):
        assert compose_and_save(
            wiki_root, trigger="park", context="c", runner=_runner(), environ={}
        ).wrote
    third = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(), environ={}
    )
    assert third.failed, third.render()
    assert "slug a-lesson-about-locks was taken" in third.reason
    assert "a-lesson-about-locks-2 is taken too" in third.reason
    assert "PageExistsError" in third.reason
    assert _pages(wiki_root) == ["a-lesson-about-locks-2.md", "a-lesson-about-locks.md"]


# --------------------------------------------------------------------------- #
# 5b. A non-zero exit does not discard a reply that parses — the reply decides
# --------------------------------------------------------------------------- #
#: The measured shape: a harness advisory on stderr, a complete page on stdout, exit 1.
_SETTINGS_ADVISORY = "Warning: a permission rule in settings.json could not be parsed"


def test_a_non_zero_exit_with_a_complete_reply_writes_the_page(wiki_root: Path) -> None:
    """The exit code alone no longer discards a page; the anomaly stays in the audit record."""
    outcome = compose_and_save(
        wiki_root, trigger="end-of-epic", context="c",
        runner=_runner(GOOD_REPLY, code=1, err=_SETTINGS_ADVISORY), environ={},
    )
    assert outcome.status == "wrote", outcome.render()
    assert outcome.exit_code == 1
    assert outcome.spawned is True
    assert outcome.slug == "a-lesson-about-locks"
    assert _pages(wiki_root) == ["a-lesson-about-locks.md"]
    assert outcome.reason.startswith("summariser exited 1 but its reply parsed; stdout: ")
    assert f"stderr: {_SETTINGS_ADVISORY}" in outcome.reason
    assert outcome.as_dict()["exit_code"] == 1


def test_a_non_zero_exit_joins_its_note_with_a_refresh_failure(
    wiki_root: Path, monkeypatch
) -> None:
    """Both anomalies survive: the exit code note and the projection failure."""

    def boom(root, **kw):
        raise OSError("hot file is read-only")

    monkeypatch.setattr(mod, "refresh_projection", boom)
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c",
        runner=_runner(GOOD_REPLY, code=1, err=_SETTINGS_ADVISORY), environ={},
    )
    assert outcome.wrote, outcome.render()
    assert "summariser exited 1 but its reply parsed" in outcome.reason
    assert "refresh_projection failed" in outcome.reason
    assert "\n" not in outcome.reason


def test_a_non_zero_exit_with_error_text_on_stdout_stays_failed(wiki_root: Path) -> None:
    """A reply that does not parse is still a failure, with both streams and the refusal."""
    out = "Error: the session could not continue"
    with pytest.raises(ValueError) as refusal:
        parse_summary(out)
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c",
        runner=_runner(out, code=1, err=_SETTINGS_ADVISORY), environ={},
    )
    assert outcome.status == "failed", outcome.render()
    assert outcome.exit_code == 1
    assert outcome.reason.startswith(
        f"the summariser exited 1; {mod.describe_streams(out, _SETTINGS_ADVISORY)}"
    )
    assert _attributes(outcome.reason, stdout=out, stderr=_SETTINGS_ADVISORY)
    assert "is missing SLUG, TITLE, BODY" in outcome.reason
    assert str(refusal.value).split(";")[0] in outcome.reason
    assert _pages(wiki_root) == []


def test_nothing_durable_after_a_non_zero_exit_names_the_exit_code(wiki_root: Path) -> None:
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c",
        runner=_runner(NOTHING_DURABLE, code=1, err=_SETTINGS_ADVISORY), environ={},
    )
    assert outcome.status == "nothing-durable", outcome.render()
    assert outcome.exit_code == 1
    assert "exited 1" in outcome.reason
    assert _pages(wiki_root) == []


@pytest.mark.parametrize(
    "code, err",
    [
        (-1, "timed out after 180s"),
        (mod.NEVER_STARTED, "agent CLI not found: 'claude'"),
        (-1, "agent CLI failed to run: SubprocessError: x"),
    ],
)
def test_a_runner_synthesised_failure_keeps_todays_reason(
    wiki_root: Path, code: int, err: str
) -> None:
    """Empty stdout means there is no reply to judge; the reason is unchanged, nothing appended."""
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner("", code=code, err=err),
        environ={},
    )
    assert outcome.failed
    assert outcome.reason == f"the summariser exited {code}; {mod.describe_streams('', err)}"


def test_the_audit_record_carries_the_word_count(wiki_root: Path) -> None:
    """`--json` prints `as_dict`; the compliance measurement must be in it, not only in render."""
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", runner=_runner(), environ={}
    )
    record = outcome.as_dict()
    assert "word_count" in record
    assert record["word_count"] == outcome.word_count and outcome.word_count


# --------------------------------------------------------------------------- #
# 6. The parser, and the shape the child is asked for
# --------------------------------------------------------------------------- #
def test_parse_summary_returns_the_three_parts() -> None:
    slug, title, body = parse_summary(GOOD_REPLY)
    assert slug == "a-lesson-about-locks"
    assert title.startswith("A lock file")
    assert "[[registry]]" in body


@pytest.mark.parametrize(
    "reply",
    ["", "   ", "SLUG: s\n", "TITLE: t\nBODY:\nx\n", "SLUG: s\nTITLE: t\nBODY:\n   \n"],
)
def test_parse_summary_refuses_loudly(reply: str) -> None:
    """The inherited requirement, at the line that owns it: nothing back is an ERROR, not a page.

    `write_page` cannot make this check — it refuses an empty body only incidentally, for
    carrying no wikilink — so a parser that shrugged here would produce a page written EMPTY,
    which is exactly the failure the requirement names.
    """
    with pytest.raises(ValueError):
        parse_summary(reply)


def test_the_prompt_asks_for_the_shape_the_parser_reads() -> None:
    """A prompt and a parser that disagree fail on every real call and on no test.

    The prompt's own instructions are fed back through the parser, so a change to one that is not
    made to the other reds here rather than in production.
    """
    prompt = build_prompt("park", "a firing")
    for token in ("SLUG:", "TITLE:", "BODY:", NOTHING_DURABLE, "[[wikilink]]"):
        assert token in prompt, f"the prompt never mentions {token!r}"
    assert "park" in prompt


def test_the_argv_is_a_one_shot_with_no_session_to_resume() -> None:
    """No resume flag, no permission grant: the child composes text and writes nothing."""
    argv = build_argv()
    assert argv[0] == mod.AGENT_CLI
    assert argv[-1] == "-p", "print mode is the last flag; the prompt itself is not in argv"
    assert not any("You are composing" in a for a in argv), (
        "THE PROMPT MUST NOT BE IN ARGV. shutil.which resolves the agent to a .CMD batch shim on "
        "Windows, and a batch shim truncates a multi-line argument at the first newline - so the "
        "child silently receives line one of the firing and writes a confident page about an "
        "epic it was told nothing about. It goes on stdin"
    )
    assert not any(a.startswith("--resume") or "permissions" in a for a in argv)


def test_the_prompt_reaches_the_child_on_stdin(wiki_root: Path) -> None:
    """The firing must arrive as STDIN, whole. This is the case that would have caught it.

    MEASURED, and the failure was silent. ``shutil.which`` resolves the agent to ``claude.CMD``
    on Windows — a BATCH SHIM — and a batch shim truncates a multi-line argument at the
    first newline. The prompt is inherently multi-line, so the child received line one of ~4500
    characters and then wrote a confident, well-formed page about an epic it had been told
    nothing about, filling the gaps from the environment block and the operator's email. Exit 0,
    no error, no empty reply.

    Every other case in this file injects a fake runner, so none of them can see WHERE the
    prompt travels — they only see that the module called something. This one asserts the
    delivery channel itself.
    """
    seen: dict = {}

    def runner(argv, *, env, timeout, stdin):
        seen["argv"], seen["stdin"] = list(argv), stdin
        return 0, GOOD_REPLY, ""

    out = mod.compose_and_save(
        wiki_root, trigger="end-of-epic", lane="jr-dev-2",
        context="a multi-line firing" + chr(10) + "second line" + chr(10) + "third line",
        runner=runner,
    )
    assert out.wrote, out.render()
    assert "second line" in seen["stdin"] and "third line" in seen["stdin"], (
        "the firing's later lines never reached the child. Anything that carries the prompt "
        "through a shell or a batch shim loses everything after the first newline, and the "
        "child then invents the rest of the page rather than failing"
    )
    assert not any("second line" in a for a in seen["argv"]), (
        "the prompt is being passed in argv. It must go on stdin: argv has a length ceiling, a "
        "shim in the path, and newline semantics that differ per platform"
    )


def test_the_child_gets_a_summariser_system_prompt() -> None:
    """Without it the default agentic system prompt applies, and the answer is narration.

    Measured with the tools already removed: told to compose a page and holding no tools, the
    session replied *"I don't have the epic content yet - let me find the project before writing
    anything"* and stopped. It is not confused about the input; it is being a coding agent,
    because that is what the default system prompt makes it. The role has to be replaced, not
    appended to.
    """
    argv = build_argv()
    assert "--system-prompt" in argv, (
        "no system prompt override: the child runs as an agent and narrates an investigation it "
        "cannot perform instead of emitting the page"
    )
    role = argv[argv.index("--system-prompt") + 1]
    assert "no tools" in role.lower() and "only input" in role.lower(), (
        "the role must tell the child there is nothing else to find. A role that omits that "
        "leaves it asking for material that does not exist"
    )


def test_the_prompt_states_the_one_job_rule_and_both_word_targets() -> None:
    """The 2026-09-05 Directive: a page does ONE job - share a lesson, or record an audit.

    The old prompt asked for a page that was "short". That is a request, and the five pages it
    produced ran 368-651 words against targets of 120-200 and under 100. "Short" carries no
    referent: the writer has nothing to compare against, so it writes until it feels done.

    Both numbers are read from the module constants rather than restated here, so a tuned target
    cannot leave this test asserting the old one - the same reason the vocabulary gate imports
    the dispatcher's own frozensets instead of copying them.
    """
    prompt = build_prompt("end-of-epic", "a firing")
    lo, hi = mod.LESSON_TARGET_WORDS
    assert f"{lo}-{hi}" in prompt, (
        f"the prompt never states the lesson target {lo}-{hi}; a writer told only to be "
        "'short' has no referent to measure itself against"
    )
    assert str(mod.AUDIT_TARGET_WORDS) in prompt, "the prompt never states the audit target"
    for token in ("LESSON", "AUDIT", "ONE of two jobs"):
        assert token in prompt, (
            f"the prompt never names {token!r}. The Directive's test is PURPOSE, not length: a "
            "page that has not chosen its job cannot be trimmed to it, and a word count alone "
            "just produces a shorter page that still does two things"
        )
    assert "CITE, DO NOT RESTATE" in prompt, (
        "the prompt never says to cite rather than restate, which is where most of the excess "
        "words came from - re-telling a PR the page already links"
    )


def test_the_outcome_measures_the_words_it_wrote(wiki_root: Path) -> None:
    """A target nothing measures is a target nobody can tell you missed.

    The targets are deliberately NOT a gate - dev-manager proposed the numbers and marked them
    correctable, and refusing a page on a provisional figure would discard good writing to
    honour an unsettled number. So the obligation this module can honestly carry is measurement:
    the audit line reports the count, compliance becomes checkable against real pages, and the
    number can be tuned against data rather than against a feeling.
    """
    body = "A lesson with [[a link]] and exactly ten words here now."
    reply = f"SLUG: measured-page{chr(10)}TITLE: Measured{chr(10)}BODY:{chr(10)}{body}{chr(10)}"

    def runner(argv, *, env, timeout, stdin):
        return 0, reply, ""

    out = mod.compose_and_save(
        wiki_root, trigger="end-of-epic", lane="jr-dev-2",
        context="a firing", runner=runner,
    )
    assert out.wrote, out.render()
    assert out.word_count == len(body.split()), (
        f"word_count is {out.word_count!r}, not the {len(body.split())} words actually written"
    )
    assert f"words={out.word_count}" in out.render(), (
        "the audit line omits the count, so the only place compliance is visible is a field "
        "nobody reads; guardrail 4 exists to be read"
    )


def test_a_failed_save_reports_no_word_count(wiki_root: Path) -> None:
    """Nothing was written, so there is nothing to have counted.

    A zero here would read as "a page with no words" rather than "no page", and the two are
    exactly the states the audit record exists to keep apart.
    """

    def runner(argv, *, env, timeout, stdin):
        return 7, "", "the model declined"

    out = mod.compose_and_save(
        wiki_root, trigger="park", lane="jr-dev-2", context="a firing", runner=runner,
    )
    assert out.failed and out.word_count is None
    assert "words=" not in out.render()


def test_the_child_is_given_no_tools() -> None:
    """The boundary is enforced by argv, not by the prompt. This is the case that proves it.

    MEASURED on the first real save this module ran. The prompt already said "Write nothing to
    disk. Your reply IS the page", and the child WROTE THE PAGE. It is a full agent session with
    file tools and this suite's own skills deployed into it, so "compose a durable wiki page"
    reads as an instruction to use the writer it can see. The result: a page in the store under
    the wrong lane, through none of this module's validation, while the parent reported
    ``wrote=no``.

    A prompt is a request. ``--tools ""`` is the thing that makes it true.
    """
    argv = build_argv()
    assert "--tools" in argv, (
        "the child is spawned with the built-in tool set available. It will write the page "
        "itself rather than returning it, bypassing parse_summary and write_page's refusals, "
        "and the parent's audit record will say wrote=no while a page exists"
    )
    assert argv[argv.index("--tools") + 1] == "", (
        "--tools must be given the empty string, which is how the CLI spells 'no tools at "
        "all'; naming any tool re-opens the write path this exists to close"
    )
    assert "--strict-mcp-config" in argv, (
        "a bounded one-shot has no use for configured MCP servers, and loading them widens "
        "what the child can reach for the same reason the built-in tools did"
    )


# --------------------------------------------------------------------------- #
# 7. The seam: this is the ONLY place an agent session starts
# --------------------------------------------------------------------------- #
def _names_the_agent_cli(source: str) -> bool:
    """True when *source* carries the agent CLI as a STRING LITERAL — an argv element.

    A substring scan is the obvious version and it is wrong in both directions here: it fires on
    `render_project_claude_md` and on `".claude"`, neither of which starts anything, and it would
    keep firing on every future identifier that happens to contain the word. A literal is what an
    argv is built from, so that is what the sweep looks for.
    """
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value == mod.AGENT_CLI
        for node in ast.walk(ast.parse(source))
    )


def test_the_literal_scan_is_not_vacuous() -> None:
    """The checker is self-tested: one that never fires would pass the sweep below forever."""
    assert _names_the_agent_cli(f'argv = [{mod.AGENT_CLI!r}, "-p", prompt]\n')
    assert not _names_the_agent_cli('def render_project_claude_md(p): return ".claude"\n')


def test_the_runner_resolves_the_cli_through_pathext_not_the_bare_name() -> None:
    """The spawn must survive a CLI that only resolves via ``PATHEXT``.

    MEASURED, not hypothetical. The first real save attempted after this module deployed failed
    with ``agent CLI not found: 'claude' ([WinError 2])`` on the platform the fleet runs on: the
    agent installs as ``claude.CMD`` beside an extensionless shell script, and
    ``subprocess.run`` does not consult ``PATHEXT`` the way a shell does. ``shutil.which`` does.

    The whole suite was green through that failure, and could not have caught it — every
    other case injects a fake runner precisely so no test starts a process, which leaves the one
    line that names a real executable untested by construction. So this asserts the RESOLUTION,
    not the spawn: what argv the runner would hand ``subprocess.Popen``, with ``subprocess.Popen``
    itself replaced.
    """
    seen: dict = {}

    def _capture(argv, **kw):
        seen["argv"] = list(argv)
        raise FileNotFoundError(2, "not really run")

    real_which, real_run = mod.shutil.which, mod.subprocess.Popen
    try:
        mod.shutil.which = lambda name: _SHIM if name == mod.AGENT_CLI else None
        mod.subprocess.Popen = _capture
        rc, out, err = _REAL_RUNNER(
            mod.build_argv(), env={mod.DISABLE_ENV: mod.DISABLE_VALUE}, timeout=1.0, stdin="p"
        )
    finally:
        mod.shutil.which, mod.subprocess.Popen = real_which, real_run

    assert seen["argv"][0] == _SHIM, (
        f"the runner passed {seen['argv'][0]!r} to subprocess.Popen rather than the resolved "
        "path. On a platform where the CLI is a PATHEXT shim the bare name raises "
        "FileNotFoundError, and the save fails for a reason that has nothing to do with the model"
    )
    assert seen["argv"][1:] == mod.build_argv()[1:], (
        "resolution must replace argv[0] and disturb nothing else - compared against build_argv "
        "rather than a literal tail, so adding a flag there does not red this for the wrong reason"
    )
    assert rc == mod.NEVER_STARTED and "not found" in err, (
        "a genuinely missing CLI must still report itself; resolution is not permission to "
        "swallow the failure into a silent no-op"
    )


def test_an_unresolvable_cli_falls_back_to_the_bare_name() -> None:
    """``which`` finding nothing must not stop the attempt — it must reach the named error.

    The fallback keeps the failure legible: the reason line names what was looked for. Returning
    early on a ``which`` miss would report the same "nothing written" with no CLI named, which is
    the difference between "install the agent" and "the model declined".
    """
    seen: dict = {}

    def _capture(argv, **kw):
        seen["argv"] = list(argv)
        raise FileNotFoundError(2, "absent")

    real_which, real_run = mod.shutil.which, mod.subprocess.Popen
    try:
        mod.shutil.which = lambda name: None
        mod.subprocess.Popen = _capture
        rc, out, err = _REAL_RUNNER(
            mod.build_argv(), env={mod.DISABLE_ENV: mod.DISABLE_VALUE}, timeout=1.0, stdin="p"
        )
    finally:
        mod.shutil.which, mod.subprocess.Popen = real_which, real_run

    assert seen["argv"][0] == mod.AGENT_CLI
    assert rc == mod.NEVER_STARTED and mod.AGENT_CLI in err


def test_no_other_agentflow_module_starts_an_agent_session() -> None:
    """The narrow, true claim — and the one worth gating.

    NOT "nothing in the suite spawns a process": several sibling modules already shell out to
    the tracker CLI and to git, and a gate asserting otherwise is falsifiable by one grep and
    teaches the next reader the suite is more pristine than it is. NO COUNT IS WRITTEN HERE ON
    PURPOSE — the first draft of this docstring said "three" and was wrong, because it was
    derived from a truncated grep rather than from the tree. A count in a docstring is a claim
    nothing re-measures, so it rots into the same falsifiable shape it was written to replace;
    the test body below enumerates the real set every run. What IS true, and what guardrail 5
    rests on, is that exactly one module starts an AGENT SESSION — so moving to a reconciler
    later stays a one-file change.
    """
    package = Path(mod.__file__).parent
    # FROM THE INDEX, not a walk (gh#175). The docstring above already refuses to write a count
    # because a truncated grep produced a wrong one; a corpus that includes whatever untracked
    # `.py` is lying in the package directory is the same failure with no docstring to correct it.
    modules = tracked_files(package, lambda rel: "/" not in rel and rel.endswith(".py"))
    assert len(modules) > 20 and any(m.name == Path(mod.__file__).name for m in modules), (
        f"the module corpus collapsed to {len(modules)} files and this check has no other floor"
    )
    for m in modules:
        assert m.is_file(), f"tracked but missing from the working tree: {m}"
    offenders = [
        path.name
        for path in modules
        if path.name != Path(mod.__file__).name
        and _names_the_agent_cli(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        f"{offenders} name the agent CLI. The spawn lives in one module on purpose: guardrail 5 "
        "is that swapping the inline one-shot for a reconciler is a one-file change"
    )


#: The two halves of a stream pair, under the spellings this package actually uses. The gate
#: below was first written against the literal pair `{"stdout", "stderr"}` and went GREEN on
#: two live instances of the identical expression in `fleet_report`, which unpacks its runner
#: as `code, out, err` — so the gate's reach was fine and its PREDICATE was the hole. A gate
#: keyed to one spelling of a thing measures the spelling, not the thing.
_OUT_NAMES = frozenset({"out", "stdout", "output", "outs"})
_ERR_NAMES = frozenset({"err", "stderr", "error", "errors", "errs"})
#: Decoration a local name picks up around either half. Stripped before the sets above are
#: consulted, so `raw_stderr` and `err_text` are the two streams they look like.
_NAME_AFFIXES = ("raw_", "std_", "_raw", "_text", "_str", "_data", "_bytes", "_lines")


def _stream_half(name: str) -> str | None:
    """``"out"``, ``"err"`` or None for a local variable name, ignoring decoration.

    WHAT THIS DELIBERATELY CANNOT SEE, written down rather than implied: a name that is
    neither an enumerated half nor an enumerated half wearing an enumerated affix —
    `child_stream_a`, `first`, `lhs`. This is a heuristic over local naming, so widen the
    three constants above when a new spelling shows up; do not read a green sweep as proof
    that no such expression exists, only that none of these spellings does.
    """
    bare = name.strip("_").lower()
    for _ in range(3):  # `raw_stderr_text` is two layers; three is slack, not a guess
        if bare in _OUT_NAMES:
            return "out"
        if bare in _ERR_NAMES:
            return "err"
        for affix in _NAME_AFFIXES:
            if affix.startswith("_") and bare.endswith(affix):
                bare = bare[: -len(affix)].strip("_")
                break
            if affix.endswith("_") and bare.startswith(affix):
                bare = bare[len(affix) :].strip("_")
                break
        else:
            return None
    return None


def _picks_one_stream(source: str) -> bool:
    """True when `source` picks a stream by which one is non-empty — under ANY naming.

    An `or` whose Name operands are one out-half and one err-half is the offence, whichever
    order they are in and whatever they are called. `(err or out or "")` is the same defect as
    `(stderr or stdout or "")`; only the unpacking differs.

    AST and not grep, for a reason this repair creates itself: the fix's docstrings and its
    call-site comments quote the buggy expression verbatim, so the next author knows what not
    to write. A literal sweep would red on the explanation and go green the moment someone
    deleted it — a gate that punishes the warning and rewards its removal.
    """
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
            continue
        halves = {
            half
            for v in node.values
            if isinstance(v, ast.Name) and (half := _stream_half(v.id)) is not None
        }
        if halves == {"out", "err"}:
            return True
    return False


def test_the_stream_expression_scan_is_not_vacuous() -> None:
    """The checker is self-tested: one that never fires would pass the sweep below forever.

    The `err or out` cases are not hypothetical spellings — they are the two live instances in
    `fleet_report` that the first cut of this gate could not see, transcribed.
    """
    assert _picks_one_stream("reason = (stderr or stdout or '').strip()[:200]\n")
    assert _picks_one_stream("reason = stdout or stderr\n")
    assert _picks_one_stream("reason = f'git branch exited {c}: {(err or out or \"\").strip()}'\n")
    assert _picks_one_stream("reason = raw_stdout or err_text\n")
    assert not _picks_one_stream("reason = describe_streams(stdout, stderr)\n")
    assert not _picks_one_stream("x = 'stderr or stdout'  # prose naming the bug\n")
    # Not every `or` over two names is a stream pick; a gate that says so gets switched off.
    assert not _picks_one_stream("value = left or right\n")
    assert not _picks_one_stream("both = stdout or output\n")


def test_the_stream_expression_scan_sees_the_sibling_spelling_in_the_real_tree() -> None:
    """The self-test above uses transcriptions; this one proves the gate reaches the FILE.

    `fleet_report` is where the name-scoped predicate failed, so it is the file whose presence
    in the scanned corpus is worth asserting by name rather than trusting to a count.
    """
    package = Path(mod.__file__).parent
    modules = {m.name for m in tracked_files(package, lambda rel: "/" not in rel and rel.endswith(".py"))}
    assert "fleet_report.py" in modules, "the sibling that broke the first predicate is unscanned"
    legacy = "code, out, err = active(argv)\nr = f'x {(err or out or \"\").strip()[:200]}'\n"
    assert _picks_one_stream(legacy), "the gate cannot see the spelling it was widened for"


def test_no_agentflow_module_picks_one_of_the_two_streams() -> None:
    """gh#478 was a TWO-SITE assumption — `save_summariser` and `issue_intake` held the same
    wrong line — and a single-site fix to a two-site assumption is the defect this gate exists
    to stop reappearing.

    Both orders are offences. `stdout or stderr` is not the repair: the default runners here
    synthesise a timeout and a missing binary as `(<negative code>, "", "<text>")`, so a stdout-first
    precedence drops those exactly as stderr-first drops a model limit. Report both streams
    (`agentflow.stream_report.describe_streams`) or select a stream on an EXPLICIT test of its
    content — never on which one happens to be non-empty.

    Corpus from the git index, not a walk (gh#175). A module added but not yet `git add`ed is
    therefore unscanned; that is the documented trade and `add` is enough, a commit is not.
    """
    package = Path(mod.__file__).parent
    modules = tracked_files(package, lambda rel: "/" not in rel and rel.endswith(".py"))
    assert len(modules) > 20, (
        f"the module corpus collapsed to {len(modules)} files and this check has no other floor"
    )
    # A NAMED floor on top of the count. An index-derived corpus silently omits a module that
    # has not been `git add`ed, so a gate against a two-site assumption has to prove both sites
    # — and the module holding their shared answer — were actually read this run.
    assert {
        "save_summariser.py", "issue_intake.py", "stream_report.py", "fleet_report.py"
    } <= {m.name for m in modules}, (
        "gh#478's two named sites, the two siblings a name-scoped predicate missed, and the "
        "module holding their shared answer must all be inside the scanned corpus"
    )
    offenders = [
        path.name for path in modules if _picks_one_stream(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        f"{offenders} choose one of a process's two streams by which one is non-empty, under "
        "some naming of the pair. That is gh#478: the non-empty one is routinely an advisory "
        "that accompanies successful runs too, so the terminating cause is never reported. "
        "Report both (agentflow.stream_report.describe_streams) or select on an EXPLICIT test "
        "of a stream's content — never on which one happens to be truthy"
    )


def test_the_default_runner_maps_a_timeout_onto_a_non_zero_exit(monkeypatch) -> None:
    """A timeout is reported the way every caller above already handles a failure.

    Asserted against the real runner with `subprocess.Popen` stubbed, so it still starts nothing.
    The real-process version, which proves the tree is gone, is the gh#969 pair below.
    """
    killed: list = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **kw: _FakeProc(
        subprocess.TimeoutExpired(cmd="x", timeout=1)
    ))
    monkeypatch.setattr(mod, "_kill_tree", killed.append)
    code, out, err = _REAL_RUNNER(["x"], env={}, timeout=1.0, stdin="")
    assert code == -1 and out == ""
    assert len(killed) == 1, "a timeout must kill the child's tree, not abandon it"
    # `startswith`, not `in`. TimeoutExpired IS a SubprocessError, so deleting the dedicated
    # timeout branch drops the call into the generic handler below it — whose message happens to
    # embed TimeoutExpired's own "... timed out after 1 seconds". A substring check passed that
    # deletion and proved only that Python's exception text mentions timeouts.
    assert err.startswith("timed out after"), (
        f"the timeout branch is gone: this came back through the generic handler ({err!r})"
    )


def test_the_default_runner_maps_a_permission_error_onto_never_started(monkeypatch) -> None:
    """An OS refusal other than a missing file is still a process that never started (gh#437)."""

    def fake_popen(*a, **kw):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    code, out, err = _REAL_RUNNER(["x"], env={}, timeout=1.0, stdin="")
    assert code == mod.NEVER_STARTED and out == "" and "PermissionError" in err


def test_the_default_runner_maps_undecodable_output_onto_a_started_exit(monkeypatch) -> None:
    """POSIX raises the decode error after the child ran, so it must not read as never-started."""
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **kw: _FakeProc(
        UnicodeDecodeError("utf-8", bytes([0xFF]), 0, 1, "invalid start byte")
    ))
    monkeypatch.setattr(mod, "_kill_tree", lambda proc: None)
    code, out, err = _REAL_RUNNER(["x"], env={}, timeout=1.0, stdin="")
    assert code == -1 and out == "" and "not UTF-8" in err


def test_an_interrupt_during_the_call_kills_the_tree_before_propagating(monkeypatch) -> None:
    """The child is in its own group, so a Ctrl+C on the parent never reaches it (gh#969 review).

    The runner must kill the tree itself and then re-raise: swallowing the interrupt would turn
    an operator's stop into a failed save, and not killing would leave the model session running.
    """
    killed: list = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **kw: _FakeProc(KeyboardInterrupt()))
    monkeypatch.setattr(mod, "_kill_tree", killed.append)
    with pytest.raises(KeyboardInterrupt):
        _REAL_RUNNER(["x"], env={}, timeout=1.0, stdin="")
    assert len(killed) == 1, "an interrupt must kill the child's tree before it propagates"


class _FakeProc:
    """A started child whose ``communicate`` raises ``exc``: the runner's post-spawn branches."""

    pid = 0
    returncode = None

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen's own parameter name
        raise self._exc


# --------------------------------------------------------------------------- #
# gh#969 - the timeout bounds the call even when a GRANDCHILD holds the pipes
# --------------------------------------------------------------------------- #
#: Short enough to keep the suite quick, long enough for the grandchild to record its pid.
_TREE_TIMEOUT_S = 4.0
#: How long the grandchild would sleep if nothing killed it. Far past the timeout, so a runner
#: that waits for it to exit on its own is measured as a failure, not as slow.
_GRANDCHILD_SLEEP_S = 30
#: Room for interpreter start-up, the tree kill and the bounded drain.
_TREE_SLACK_S = 8.0


def _alive(pid: int) -> bool:
    """Whether ``pid`` is still running. Never signals it: ``os.kill`` on Windows TERMINATES."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _assert_tree_bounded_and_gone(argv: list, pidfile: Path) -> None:
    start = time.monotonic()
    code, out, err = _REAL_RUNNER(argv, env=dict(os.environ), timeout=_TREE_TIMEOUT_S, stdin="hi")
    elapsed = time.monotonic() - start
    grandchild = int(pidfile.read_text().strip()) if pidfile.is_file() else None
    try:
        assert elapsed < _TREE_TIMEOUT_S + _TREE_SLACK_S, (
            f"the runner returned after {elapsed:.1f}s on a {_TREE_TIMEOUT_S:g}s timeout: it "
            "waited for a grandchild that holds the pipes"
        )
        assert (code, out) == (-1, "") and err.startswith("timed out after"), (code, out, err)
        assert grandchild is not None, "the grandchild never recorded its pid; the probe is void"
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(grandchild), f"grandchild {grandchild} survived the timeout"
    finally:
        if grandchild is not None and _alive(grandchild):
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(grandchild)], capture_output=True)
            else:
                os.kill(grandchild, signal.SIGKILL)


@pytest.mark.skipif(os.name != "nt", reason="the batch-shim tree is the Windows shape")
def test_a_timeout_kills_a_batch_shims_grandchild_and_returns_on_time(tmp_path: Path) -> None:
    """gh#969, the measured shape: the CLI is a ``.cmd`` shim and the session is its grandchild."""
    pidfile = tmp_path / "grandchild.pid"
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        f"time.sleep({_GRANDCHILD_SLEEP_S})\n"
        "print('late')\n",
        encoding="utf-8",
    )
    shim = tmp_path / "agent.cmd"
    shim.write_text(
        f'@echo off\r\n"{sys.executable}" "{sleeper}" "{pidfile}"\r\n', encoding="utf-8"
    )
    _assert_tree_bounded_and_gone([str(shim)], pidfile)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups; Windows has its own case")
def test_a_timeout_kills_a_shells_backgrounded_grandchild_and_returns_on_time(
    tmp_path: Path,
) -> None:
    """gh#969 on POSIX: a backgrounded grandchild inherits the pipes the same way."""
    pidfile = tmp_path / "grandchild.pid"
    script = f'sleep {_GRANDCHILD_SLEEP_S} & echo $! > "{pidfile}"; wait'
    _assert_tree_bounded_and_gone(["sh", "-c", script], pidfile)


def test_the_four_triggers_are_the_decided_set() -> None:
    """The module's trigger vocabulary is the skill's, not a second one that can drift."""
    assert TRIGGERS == ("end-of-epic", "park", "halt", "block")


# --------------------------------------------------------------------------- #
# The lane is refused before the spawn, not after it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_lane", ["lead dev|x", "../escape"])
def test_an_invalid_lane_is_refused_without_a_model_call(wiki_root: Path, bad_lane: str) -> None:
    calls: list = []
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", lane=bad_lane,
        runner=_runner(record=calls), environ={},
    )

    assert outcome.status == "failed", outcome.render()
    assert "lane refused before spawning" in outcome.reason
    assert calls == [], "the summariser was spawned for a lane the writer would refuse"
    assert _pages(wiki_root) == []


def test_a_case_twin_lane_is_refused_without_a_model_call(wiki_root: Path) -> None:
    first = compose_and_save(
        wiki_root, trigger="park", context="c", lane="lead-dev", runner=_runner(), environ={}
    )
    assert first.status == "wrote" and first.reason == "", first.render()

    calls: list = []
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", lane="LEAD-DEV",
        runner=_runner(record=calls), environ={},
    )

    assert outcome.status == "failed", outcome.render()
    assert "differ only by case" in outcome.reason
    assert first.path in outcome.reason
    assert calls == []
    assert _pages(wiki_root) == ["a-lesson-about-locks.md"]


def test_a_same_case_lane_still_saves(wiki_root: Path) -> None:
    """Control: the pre-spawn check passes a lane the store already carries."""
    compose_and_save(
        wiki_root, trigger="park", context="c", lane="lead-dev", runner=_runner(), environ={}
    )
    calls: list = []
    second = _runner(GOOD_REPLY.replace("a-lesson-about-locks", "another-lesson"), record=calls)
    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", lane="lead-dev", runner=second, environ={}
    )

    assert outcome.status == "wrote" and outcome.reason == "", outcome.render()
    assert len(calls) == 1
    assert _pages(wiki_root) == ["a-lesson-about-locks.md", "another-lesson.md"]


def test_an_unparseable_page_does_not_cost_a_laned_save(wiki_root: Path) -> None:
    """The lane check skips a page it cannot parse; the page is written and the refresh that
    follows names the bad file in the outcome, exactly as before the lane check existed."""
    bad = wiki_root / "pages" / "broken.md"
    bad.write_text("no frontmatter here [[registry]]\n", encoding="utf-8")
    calls: list = []

    outcome = compose_and_save(
        wiki_root, trigger="park", context="c", lane="lead-dev",
        runner=_runner(record=calls), environ={},
    )

    assert outcome.status == "wrote", outcome.render()
    assert len(calls) == 1
    assert "refresh_projection failed" in outcome.reason
    assert str(bad) in outcome.reason
    assert _pages(wiki_root) == ["a-lesson-about-locks.md", "broken.md"]
