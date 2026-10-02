"""The ONE test in this suite that starts a real agent session — and the checker it runs (gh#272).

WHY THIS FILE EXISTS. `save_summariser` shipped with 14/14 CI green, 55 mutation-proved tests and
an adversarial pass, and the first run against a live model found three defects in a row that no
test could have reached: a bare `claude` that `subprocess.run` cannot resolve behind a Windows
PATHEXT shim; a CHILD that wrote the page itself, in the wrong lane, through none of the
validation, **while the parent's audit record said `wrote=no`**; and a prompt that never arrived,
because a `.CMD` batch shim truncates a multi-line argv at the first newline — so the child got
line one of ~4500 characters, wrote a confident page about an epic it knew nothing about, and
exited 0.

All three are fixed. This file is the GATE, not the fixes. What it closes is the reason they were
reachable at all: every case in `test_save_summariser.py` injects a fake runner *precisely so that
no test starts a process*, which is correct test design and is also the hole — the line naming a
real executable, the argv the child receives, and the child's actual behaviour are untested by
construction, so green CI on that module is evidence about the parser and nothing else.

WHY IT IS A SEPARATE MODULE. `test_save_summariser.py` carries an autouse `_no_real_spawns`
fixture that replaces the real runner with a stub raising a `BaseException` — deliberately not an
`Exception`, because `compose_and_save` catches `Exception` on every path and would swallow an
`AssertionError` tripwire into `status="failed"`. A live test cannot live in that file without
deleting the tripwire that protects every other case in it, so it lives here instead. Nothing here
is autouse and the live case is the only one that spawns.

THE TWO HALVES, and the reason for the split:

* **The live case** — one real spawn, gated (:func:`~agentflow.save_summariser.live_spawn_blocked_reason`),
  asserting the three properties gh#272 names. It costs tokens, so it runs only when a human opts
  in with ``AGENTFLOW_LIVE_SAVE=1``.
* **The checker, and its red proofs** — the assertion helpers below are PURE functions over an
  outcome and a directory, so every defect the live case is supposed to catch can be MIMICKED with
  an injected runner and proved to red, in CI, for free. That is what stops this gate becoming the
  thing it was written about: a live case that is skipped everywhere is indistinguishable from a
  live case that passes, unless the machinery it would have run is itself under test.

WHAT THIS CANNOT DO. One spawn against one model on one machine is a SMOKE test: it proves the
path is connected end to end, not that a page is any good, not that the model is deterministic,
and not that a different agent CLI behaves the same. It is bounded on purpose (one spawn, one
page, a short context, its own timeout) because the issue names *staying cheap enough that nobody
routes around it* as a design requirement, and a gate people disable protects nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentflow import save_summariser as mod
from agentflow.save_summariser import (
    LIVE_ENV,
    LIVE_SKIP_CAUSES,
    LIVE_SKIP_NO_AGENT_CLI,
    LIVE_SKIP_NO_OPT_IN,
    LIVE_SKIP_SAVE_DISABLED,
    NOTHING_DURABLE,
    SaveOutcome,
    compose_and_save,
    computed_created,
    live_spawn_blocked_reason,
    parse_summary,
)
from agentflow.wiki import initial_pillar_files, write_page

#: The live spawn's own timeout, deliberately SHORTER than ``DEFAULT_TIMEOUT_S``. The module's
#: default is sized for a real phase boundary where a slow page still beats no page; a smoke run
#: is sized for a human waiting on a suite. Overridable for a slow machine without editing code.
LIVE_TIMEOUT_S = float(os.environ.get("AGENTFLOW_LIVE_SAVE_TIMEOUT", "120"))

#: The canary the child must echo, and the reason it is where it is.
#:
#: gh#272's defect 3 was found by a probe that PASSED WHILE PROVING THE OPPOSITE: it put a marker
#: token on line one of the prompt and asked for it back, the token arrived, the request three
#: lines down did not, and the model echoed the only content it had. The corrected form of that
#: probe puts the marker in the LAST section of the prompt — ``build_prompt`` appends the context
#: after ~2500 characters of instruction — so a reply carrying it is proof the child read to the
#: end, and a `.CMD` shim truncating at the first newline cannot produce one.
LIVE_CANARY = "QX7F2"

#: Where a child that regained file tools would plausibly file a page: the parent's real working
#: directory, and the live data_root's wiki store. `_subprocess_runner` passes no ``cwd``, so the
#: child inherits the parent's - which is how defect 2 reached the real store in the first place.
#: Read-only, and every path is skipped when absent, so this never fails for a missing tree.
def _live_escape_search_roots() -> tuple:
    roots = [Path.cwd()]
    try:
        from agentflow.config import load_environment

        roots.append(Path(load_environment().data_root))
    except Exception:  # noqa: BLE001 - a checker must never be the thing that breaks the gate
        pass
    return tuple(roots)


LIVE_ESCAPE_SEARCH_ROOTS = _live_escape_search_roots()

#: The firing handed to the live child. Short (a smoke run pays per token), lesson-shaped (so
#: ``NOTHING DURABLE`` is the wrong answer and an unhelpfully vague context is not what reds the
#: case), and it states the canary requirement as part of the firing rather than as a change to
#: ``build_prompt`` — the prompt under test must be the one production uses.
LIVE_CONTEXT = (
    "Firing: agentflow lane jr-dev-1, epic E-0272, end-of-epic boundary.\n"
    "Incident id: " + LIVE_CANARY + ".\n"
    "What happened: the summariser spawn's tests all injected a fake runner, so the line naming a "
    "real executable and the argv the child receives were untested by construction. Three defects "
    "shipped behind 14/14 green CI and were only found by running it once against a live model.\n"
    "The rule that survives: a component whose contract is 'start a process and read what it "
    "says' needs one live run in its definition of done; mocking the process boundary makes that "
    "whole defect class unreachable forever.\n"
    "Related page to link: [[save-as-an-agentflow-capability]].\n"
    "Requirement for the page you write: it is filed under incident id " + LIVE_CANARY + ", so "
    "the body must state that id verbatim."
)


# --------------------------------------------------------------------------- #
# Helpers — the checker the live case runs, kept pure so it can be red-proved
# --------------------------------------------------------------------------- #
def pages_on_disk(wiki_root: Path) -> list[Path]:
    """Every page actually present, walked RECURSIVELY.

    ``write_page`` only ever creates ``pages/<slug>.md``, so a flat glob would answer this
    question for the writer. It would not answer it for the WORLD, which is what an audit record
    is a claim about: ``read_pages`` walks ``pages/`` recursively, so a checker that only looks
    where the HONEST writer writes cannot see a page filed one directory over.

    BE PRECISE ABOUT WHAT THIS DOES NOT COVER, because an earlier draft of this docstring claimed
    the whole of gh#272 defect 2 and review caught it. The recursion widens the search only WITHIN
    ``wiki_root``. Defect 2 filed its page into the REAL store, which is not under this root and
    never will be, so nothing here can see it - `assert_no_page_escaped_the_root` is the checker
    for that half, and the two are not substitutes for each other.

    ``hot.md`` and ``hot-<lane>.md`` are excluded by construction — ``refresh_projection`` writes
    them at the wiki root, outside ``pages/`` — so a projection refresh never counts as a page.
    """
    pages = wiki_root / "pages"
    return sorted(p for p in pages.rglob("*.md") if p.is_file())


def assert_child_returned_page_shape(stdout: str) -> tuple:
    """gh#272 assertion (a): the CHILD returned the SLUG/TITLE/BODY shape on stdout.

    Asserted against the child's raw reply rather than against the outcome, because those are
    different claims. The outcome says the parent got a page; this says the child sent one in the
    shape the parser was promised — and under defect 3 (prompt truncated at the first newline) the
    shape instruction, which sits ~2600 characters into the prompt, never reaches the child at
    all, so a reply in the right shape is itself evidence the prompt survived past line one.
    """
    assert stdout.strip(), "the child returned nothing on stdout"
    assert stdout.strip() != NOTHING_DURABLE, (
        "the child judged the firing learned nothing durable. That is a legal answer to a vague "
        "firing but the wrong one here: LIVE_CONTEXT is a lesson with a stated rule, so this is "
        "evidence about the prompt or the model, not a flake to retry"
    )
    try:
        slug, title, body = parse_summary(stdout)
    except ValueError as e:  # re-raised as an assertion so the gate reds rather than errors
        raise AssertionError(
            f"the child's reply is not in the SLUG/TITLE/BODY shape: {e}"
        ) from e
    return slug, title, body


def assert_prompt_reached_the_child(stdout: str) -> None:
    """gh#272 assertion (a2): the child read to the END of the prompt, not just its first line.

    A miss here is a FINDING, not a flake. Two causes are worth distinguishing before anything is
    weakened: the prompt was truncated on its way to the child (defect 3, and the whole reason
    this file exists), or the model paraphrased away an id the context told it to keep. The first
    is a regression in the spawn; the second is a prompt-quality signal. Deleting the assertion
    answers neither.
    """
    assert LIVE_CANARY.lower() in (stdout or "").lower(), (
        f"the child's reply does not carry the canary {LIVE_CANARY!r}, which the context states "
        "at the very END of the prompt. Either the prompt was truncated before the child read it "
        "(gh#272 defect 3) or the model dropped an id it was told to keep -- investigate which "
        f"before weakening this. Reply was: {(stdout or '')[:300]!r}"
    )


#: The flags the module names as THE defence keeping the child out of the store. Not the prompt -
#: `save_summariser.build_argv` says so in its own docstring, and gh#272 defect 2 is what happened
#: when the prompt was the only thing asked to hold that line.
NO_TOOLS_FLAGS = ("--tools", "--strict-mcp-config", "--safe-mode")


def assert_argv_kept_the_child_toolless(argv: list) -> None:
    """The argv the REAL runner was handed still carries the no-tools flags.

    THIS IS THE ASSERTION THAT COSTS NOTHING AND GUARDS THE MOST. The live case captures argv on
    its way to `subprocess.run` and, until this existed, asserted nothing about it - while the
    epic's own framing is that "the argv the child actually receives" is untested by construction.

    It is also the only in-band check on the defect-2 mechanism. `assert_audit_matches_disk` can
    only see the parent's own wiki root; a child with file tools restored writes wherever its
    inherited working directory points, which is NOT that root, and every other assertion in the
    live case stays green. Verifying the flags survived to the spawn is not a substitute for
    observing the write - see `assert_no_page_escaped_the_root` - but it is the half that catches
    a regression BEFORE it reaches a real store.
    """
    assert argv, "no argv was captured; the runner was never called"
    missing = [f for f in NO_TOOLS_FLAGS if f not in argv]
    assert not missing, (
        f"the argv handed to the real runner is missing {missing}, which are the flags that keep "
        f"the child out of the store (gh#272 defect 2). argv was: {argv!r}"
    )
    tools_at = argv.index("--tools")
    assert argv[tools_at + 1 : tools_at + 2] == [""], (
        f"--tools is present but its value is {argv[tools_at + 1 : tools_at + 2]!r}, not the empty "
        "string; a non-empty tool list hands the child a writer"
    )


def assert_no_page_escaped_the_root(search_roots, canary: str, allowed: Path) -> None:
    """No page carrying the canary was filed anywhere OUTSIDE the parent's own wiki root.

    `assert_audit_matches_disk` scopes the world to `wiki_root`, and that scoping is what makes it
    blind to the defect it is named for: gh#272 defect 2 filed a page into the REAL store, in the
    wrong lane, through none of the parent's validation. A page written there is not in `wiki_root`
    and never will be, so the record and the temp directory agree with each other perfectly while
    the store has a rogue page in it.

    The canary is what makes this checkable cheaply. The child is told about exactly one incident
    id, so any file carrying it was produced by THIS run - a match outside `allowed` is a page the
    child wrote itself.
    """
    escaped = []
    for root in search_roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for found in root.rglob("*.md"):
            if not found.is_file() or allowed in found.parents or found == allowed:
                continue
            try:
                if canary in found.read_text(encoding="utf-8", errors="replace"):
                    escaped.append(str(found))
            except OSError:
                continue
    assert not escaped, (
        f"gh#272 DEFECT 2 IS BACK: {len(escaped)} page(s) carrying the run's canary {canary!r} "
        f"were filed OUTSIDE the parent's wiki root: {escaped}. The child wrote into a store "
        "the parent never validated, and every audit-vs-disk assertion stayed green because that "
        "checker can only see the root it was handed"
    )


def assert_audit_matches_disk(outcome: SaveOutcome, wiki_root: Path) -> None:
    """gh#272 assertion (c) — THE POINT OF THE EPIC: the record agrees with the world.

    Defect 2 was not "a page was written badly". It was **the parent's audit record saying
    ``wrote=no`` while a page sat on disk** — an audit line that is false about the world, which
    is worse than no audit line, because guardrail 4 exists to be believed and a reader has no
    second source to check it against.

    So this checks BOTH directions, and each direction reds on its own:

    * ``wrote=yes`` — the named page is on disk at the path the record gives, its filename is the
      slug the record names, and it is the ONLY page there. A record naming one page while two
      exist is the same class of lie as naming none while one exists.
    * ``wrote=no`` (any non-``wrote`` status: ``failed``, ``nothing-durable``, ``disabled``,
      ``in-flight``) — ``pages/`` is EMPTY. This is the direction that would have caught defect 2,
      and it is the reason the checker takes the whole directory rather than the record's own
      path: a record cannot be asked about a page it does not know exists.
    """
    on_disk = pages_on_disk(wiki_root)
    listing = [str(p) for p in on_disk]

    if not outcome.wrote:
        assert not on_disk, (
            f"THE AUDIT RECORD IS FALSE ABOUT THE WORLD: it says wrote=no (status="
            f"{outcome.status!r}) and {len(on_disk)} page(s) are on disk: {listing}. This is "
            "gh#272 defect 2 -- something other than compose_and_save wrote into the store"
        )
        return

    assert outcome.slug, "the record says wrote=yes and names no slug"
    assert outcome.path, "the record says wrote=yes and names no path"
    written = Path(outcome.path)
    assert written.is_file(), (
        f"THE AUDIT RECORD IS FALSE ABOUT THE WORLD: it says wrote=yes at {outcome.path!r} and "
        f"nothing is there. Pages actually present: {listing}"
    )
    assert written.stem == outcome.slug, (
        f"the record names slug {outcome.slug!r} and the page it points at is "
        f"{written.stem!r}; the slug in an audit line is how a human finds the page again"
    )
    assert on_disk == [written], (
        f"the record names exactly one page ({written}) and the store holds {len(on_disk)}: "
        f"{listing}. An extra page means a second writer this record does not account for"
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def wiki_root(tmp_path: Path) -> Path:
    """A provisioned, empty wiki pillar under ``tmp_path``. NEVER the real ``<data_root>``.

    ``pages/`` is create-only and a page written into the wrong store cannot be moved back out,
    so a live run that writes anywhere but a temporary directory is a permanent mistake. The live
    case takes this fixture for that reason and not for isolation alone.
    """
    root = tmp_path / "wiki"
    (root / "pages").mkdir(parents=True)
    # Seeded exactly as the provisioner seeds a pillar: compose_and_save refuses a root without
    # these markers (gh#501), so a pages/-only fixture would test the refusal, not the save.
    for name, body in initial_pillar_files("demo").items():
        (root / name).write_text(body, encoding="utf-8", newline="\n")
    return root


# --------------------------------------------------------------------------- #
# 1. The gate itself — a skip must never be silent
# --------------------------------------------------------------------------- #
#: Each cause, with an environment and a ``which`` that produce it and nothing else. Written as a
#: case per cause rather than one table-driven walk over a compound condition: a disjunction goes
#: green on any one surviving limb, so deleting two of the three checks would red nothing.
_GATE_CASES = (
    (
        LIVE_SKIP_NO_OPT_IN,
        {},
        lambda name: "C:/tools/claude.CMD",
    ),
    (
        LIVE_SKIP_SAVE_DISABLED,
        {LIVE_ENV: "1", mod.DISABLE_ENV: "1"},
        lambda name: "C:/tools/claude.CMD",
    ),
    (
        LIVE_SKIP_NO_AGENT_CLI,
        {LIVE_ENV: "1"},
        lambda name: None,
    ),
)


@pytest.mark.parametrize("cause,environ,which", _GATE_CASES, ids=[c[0] for c in _GATE_CASES])
def test_every_blocked_live_run_names_why_it_was_blocked(cause, environ, which) -> None:
    """A skip carries a REASON, and the reason names which of the three causes fired.

    This is the anti-gh#272 assertion about this file itself. A live test that is skipped
    everywhere is indistinguishable from one that passes; the only thing that separates them for
    a human reading a suite is a skip line that says *why*, and the only thing that keeps that
    line honest is a test on the function that produces it.
    """
    reason = live_spawn_blocked_reason(environ, which=which)
    assert reason, f"{cause}: the live run was blocked and no reason was given"
    assert reason.startswith(cause + ":"), (
        f"the reason for {cause} must lead with its own token so a caller can branch on the "
        f"cause without matching prose; got {reason!r}"
    )
    assert len(reason) > len(cause) + 20, (
        f"the reason for {cause} is a bare token with no sentence a human can act on: {reason!r}"
    )


def test_the_gate_opens_when_all_three_conditions_are_met() -> None:
    """The control. A gate that is always closed proves nothing about the three cases above.

    Without this, ``live_spawn_blocked_reason`` could ``return "no-opt-in: ..."`` unconditionally
    and every case above would still pass — which is exactly the false-green shape gh#272 is
    about, reproduced in the gate written to prevent it.
    """
    assert (
        live_spawn_blocked_reason({LIVE_ENV: "1"}, which=lambda name: "C:/tools/claude.CMD")
        is None
    )


def test_this_machines_gate_state_is_either_open_or_explained() -> None:
    """Whatever THIS machine's real state is, it is one of the four known answers.

    Read against the real environment on purpose: the cases above drive synthetic inputs, and a
    fourth cause appearing in production — a new env var, a renamed CLI — would leave them green
    while the live run silently stopped running everywhere.
    """
    reason = live_spawn_blocked_reason()
    if reason is not None:
        assert reason.split(":", 1)[0] in LIVE_SKIP_CAUSES, (
            f"the live run was blocked for a cause this file does not know about: {reason!r}"
        )


# --------------------------------------------------------------------------- #
# 2. Red proofs — each gh#272 defect, mimicked with an injected runner
# --------------------------------------------------------------------------- #
# These are SUBSTITUTES and are labelled as such. They do not start a process; they drive
# `compose_and_save` with a runner that reproduces the SHAPE of each defect, so the checkers the
# live case depends on are proved to red in CI, on both platforms, for nothing. What they cannot
# prove is that a real child ever behaves this way -- that is the live case's job, and neither
# half substitutes for the other.

_HONEST_REPLY = (
    "SLUG: live-smoke-" + LIVE_CANARY.lower() + "\n"
    "TITLE: A mocked process boundary hides a whole defect class\n"
    "BODY:\n"
    "Incident " + LIVE_CANARY + ": three defects shipped behind green CI. See [[registry]].\n"
)


def _runner(reply: str, *, code: int = 0, err: str = "", writes_into: Path | None = None):
    """A fake runner in the module's shape. ``writes_into`` mimics a child that writes the store."""

    def run(argv, *, env, timeout, stdin):
        if writes_into is not None:
            write_page(
                writes_into,
                slug="a-page-the-parent-never-knew-about",
                title="Written by the child, not the parent",
                body="This page bypassed every validation the parent owns. [[registry]]\n",
                created=computed_created(),
                lane="some-other-lane",
            )
        return code, reply, err

    return run


def test_control_the_honest_path_passes_every_checker(wiki_root: Path) -> None:
    """The control for the three red proofs below. A checker that always reds proves nothing.

    Runs the same three assertions the live case runs, over an injected honest reply, and requires
    them all to PASS. If this reds, a red below is not evidence about the defect it names.
    """
    outcome = compose_and_save(
        wiki_root,
        trigger="end-of-epic",
        context=LIVE_CONTEXT,
        lane="jr-dev-1",
        runner=_runner(_HONEST_REPLY),
        environ={},
    )
    assert outcome.wrote, f"the control did not write: {outcome.render()}"
    assert_child_returned_page_shape(_HONEST_REPLY)
    assert_prompt_reached_the_child(_HONEST_REPLY)
    assert_audit_matches_disk(outcome, wiki_root)


def test_red_proof_defect_2_child_writes_the_page_while_the_record_says_no(
    wiki_root: Path,
) -> None:
    """gh#272 defect 2, mimicked: a page on disk that the audit record does not account for.

    The child returns ``NOTHING DURABLE`` — so the parent writes nothing and records ``wrote=no``,
    truthfully as far as it knows — while having written a page itself, under a different lane,
    through none of the parent's validation. That combination is exactly what shipped: an audit
    line that is false about the world while every line of the parent behaved correctly.
    """
    outcome = compose_and_save(
        wiki_root,
        trigger="end-of-epic",
        context=LIVE_CONTEXT,
        runner=_runner(NOTHING_DURABLE, writes_into=wiki_root),
        environ={},
    )
    assert not outcome.wrote and outcome.status == "nothing-durable"
    with pytest.raises(AssertionError, match="FALSE ABOUT THE WORLD"):
        assert_audit_matches_disk(outcome, wiki_root)


def test_red_proof_a_record_claiming_a_page_that_is_not_on_disk(wiki_root: Path) -> None:
    """The other direction: ``wrote=yes`` over an empty store.

    Not reachable through ``compose_and_save`` today — the ``wrote`` branch runs only after
    ``write_page`` returns a path — which is precisely why it is asserted against a fabricated
    record instead of skipped. The checker's value is that it survives a future refactor in which
    the status and the write stop being set on the same line.
    """
    lying = SaveOutcome(
        trigger="end-of-epic",
        status="wrote",
        started_at="2026-09-05T00:00:00Z",
        duration_s=1.0,
        exit_code=0,
        slug="a-page-that-was-never-written",
        path=str(wiki_root / "pages" / "a-page-that-was-never-written.md"),
    )
    with pytest.raises(AssertionError, match="FALSE ABOUT THE WORLD"):
        assert_audit_matches_disk(lying, wiki_root)


def test_red_proof_a_second_page_appears_beside_the_one_the_record_names(
    wiki_root: Path,
) -> None:
    """A truthful ``wrote=yes`` that is nonetheless an incomplete account of the store.

    The record names one page and two exist. gh#272's defect 2 in its subtler form: the parent's
    line is true about the page it wrote and silent about the one it did not, and a human reading
    the audit has no way to learn the second exists.
    """
    outcome = compose_and_save(
        wiki_root,
        trigger="end-of-epic",
        context=LIVE_CONTEXT,
        runner=_runner(_HONEST_REPLY),
        environ={},
    )
    assert outcome.wrote
    write_page(
        wiki_root,
        slug="a-page-nobody-recorded",
        title="A second writer",
        body="Nothing in the audit record accounts for this. [[registry]]\n",
        created=computed_created(),
    )
    with pytest.raises(AssertionError, match="exactly one page"):
        assert_audit_matches_disk(outcome, wiki_root)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "Sure! Here is a page about your epic.",
        "SLUG: x\nTITLE: y\n",
        NOTHING_DURABLE,
    ],
    ids=["empty", "prose-with-no-headers", "no-body-section", "nothing-durable"],
)
def test_red_proof_the_child_did_not_return_the_page_shape(reply: str) -> None:
    """gh#272 assertion (a), red-proved over the four ways a reply misses the shape.

    ``prose-with-no-headers`` is the defect-3 signature: under a truncated prompt the child never
    receives the shape instruction, so it answers conversationally and exits 0.
    """
    with pytest.raises(AssertionError):
        assert_child_returned_page_shape(reply)


def test_red_proof_a_reply_that_never_saw_the_end_of_the_prompt() -> None:
    """gh#272 defect 3, mimicked: a well-formed page that cannot have read the whole prompt.

    This reply parses. It is confident, correctly shaped, and about roughly the right subject —
    which is what made the real defect so expensive — and it does not carry the canary the last
    section of the prompt stated, because a child that got line one never saw that section.
    """
    plausible = (
        "SLUG: mocking-the-process-boundary\n"
        "TITLE: A confident page about a firing it was never told about\n"
        "BODY:\n"
        "Composed from the environment block and a date. [[registry]]\n"
    )
    assert_child_returned_page_shape(plausible)  # it parses; that is the trap
    with pytest.raises(AssertionError, match="canary"):
        assert_prompt_reached_the_child(plausible)


# --------------------------------------------------------------------------- #
# 3. THE LIVE CASE — one real spawn, opt-in, bounded
# --------------------------------------------------------------------------- #
def test_live_one_real_spawn_writes_one_page_and_the_record_is_true(wiki_root: Path) -> None:
    """One real spawn against the real CLI, asserting all three gh#272 properties at once.

    ONE SPAWN, NOT THREE. The issue names *staying cheap enough that nobody routes around it* as a
    design requirement, so the three assertions share a single call rather than each taking their
    own: three cases would be three model invocations and three times the wait, for no extra
    coverage — they are three claims about the same run.

    THE RUNNER IS THE REAL ONE AND IT IS STILL RECORDED. The default is wrapped rather than
    replaced, and ``compose_and_save`` is called with ``runner=None``, so the module's own
    default-selection line is exercised and ``_subprocess_runner`` — including the
    ``shutil.which`` resolution that gh#272 defect 1 was about — runs unmodified. The wrapper
    exists only to keep the child's raw stdout, which the outcome does not carry and assertion (a)
    is about.
    """
    blocked = live_spawn_blocked_reason()
    if blocked:
        pytest.skip(blocked)

    seen: dict = {}
    real = mod._subprocess_runner

    def recording(argv, *, env, timeout, stdin):
        seen["argv"] = argv
        seen["stdin"] = stdin
        code, out, err = real(argv, env=env, timeout=timeout, stdin=stdin)
        seen["code"], seen["stdout"], seen["stderr"] = code, out, err
        return code, out, err

    mod._subprocess_runner = recording
    try:
        outcome = compose_and_save(
            wiki_root,
            trigger="end-of-epic",
            context=LIVE_CONTEXT,
            lane="jr-dev-1",
            timeout=LIVE_TIMEOUT_S,
            environ=dict(os.environ),
        )
    finally:
        mod._subprocess_runner = real

    assert seen, "compose_and_save returned without calling the runner at all"
    detail = (
        f"\n  audit: {outcome.render()}"
        f"\n  exit:  {seen.get('code')!r}"
        f"\n  err:   {(seen.get('stderr') or '')[:400]!r}"
        f"\n  out:   {(seen.get('stdout') or '')[:800]!r}"
    )

    # (a) the CHILD returned the shape, and read the whole prompt to do it.
    assert seen.get("code") == 0, f"the child exited non-zero{detail}"
    assert_child_returned_page_shape(seen.get("stdout", ""))
    assert_prompt_reached_the_child(seen.get("stdout", ""))

    # (b) the PARENT wrote the page.
    assert outcome.wrote, f"the child returned a page and the parent did not write it{detail}"

    # The flags that keep the child out of the store survived all the way to the spawn. Free,
    # and the only in-band check on the defect-2 MECHANISM rather than its aftermath.
    assert_argv_kept_the_child_toolless(seen.get("argv") or [])

    # (c) THE POINT: the audit record agrees with what is actually on disk, both directions.
    assert_audit_matches_disk(outcome, wiki_root)

    # ...and nothing was filed OUTSIDE that root. Review found the checker above passes green
    # while a tool-enabled child writes into the real store, because its whole world is the temp
    # root it was handed - the exact "gate passes while the thing it guards is broken" shape this
    # epic exists to remove. The canary makes the wider search cheap and unambiguous.
    assert_no_page_escaped_the_root(LIVE_ESCAPE_SEARCH_ROOTS, LIVE_CANARY, wiki_root.resolve())

    # And the page is a page, not an empty file with a header on it.
    written = Path(outcome.path)
    text = written.read_text(encoding="utf-8")
    assert "[[" in text, f"the written page carries no wikilink{detail}"
    assert outcome.word_count and outcome.word_count > 10, (
        f"the record's word_count is {outcome.word_count!r}, which is not a page{detail}"
    )
