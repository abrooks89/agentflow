"""The merge-preflight prose states exit codes; this pins them to what the CLI returns.

WHY THIS FILE EXISTS. gh#48 and gh#19 moved the deploy-lock guard contract out of
``docs/REGISTRY.md`` and the CLI, where it had lived alone, and into four SKILL.md files that
autonomous loops actually read. That is the right place for it -- a rule each loop must
independently re-derive is a rule that will be independently got wrong -- but it created five
prose copies of numbers that only the code knows, and at the moment they were written NOTHING
would have caught them drifting.

That is not a hypothetical risk here. Adversarial review of the very commit that added the prose
found TWO of the new claims already false on the day they were written:

* ``preflight returns 0 unconditionally`` -- it exits 2 for a registry it cannot read or an
  ``--actor`` it does not know. The rule built on that premise (never chain it) is correct; the
  premise was not, and a loop told "it cannot fail" has no reason to read the status at all.
* ``exit 2 from the take is ordinary contention`` -- exit 2 is EVERY ``RegistryError``, of which
  contention (``deploy_held``) is one. The taught recovery, short-wake and retry, is an unbounded
  loop against a command that will never succeed for the other keys.

Both are the same shape: a false invariant stated confidently where a narrower true claim was
available. Prose cannot be type-checked, so this module derives the truth by RUNNING the CLI and
asserts the prose agrees.

WHAT THIS GATE CAN AND CANNOT DO -- stated plainly, because a gate that overstates itself is
worse than none (this repo has a filed issue that is exactly that mistake):

* It CAN prove that every exit code the prose attributes to a named refusal key is the code the
  CLI actually returns for that key, and that no file re-asserts the two falsified claims.
* It CANNOT prove the prose is otherwise correct, well-placed, or that a loop will follow it.
  A pass here means the numbers are honest, not that the paragraph is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow import ntfy
from agentflow.registry import main
from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parent

# Every file that now states a deploy-guard exit code. Adding a fifth prose copy without adding
# it here is the drift this module exists to stop, so the membership itself is asserted below
# from a grep rather than trusted as a hand-kept list.
PROSE_FILES = (
    SKILLS_ROOT / "lead-dev" / "SKILL.md",
    SKILLS_ROOT / "jr-dev-1" / "SKILL.md",
    SKILLS_ROOT / "jr-dev-2" / "SKILL.md",
    SKILLS_ROOT / "jr-dev-3" / "SKILL.md",
    SKILLS_ROOT / "orchestrator" / "SKILL.md",
    REPO_ROOT / "docs" / "REGISTRY.md",
)


@pytest.fixture(autouse=True)
def _no_pings(monkeypatch):
    """Hard network guard: NTFY_URL/NTFY_TOPIC are set in real fleet shells."""
    monkeypatch.setattr(ntfy, "notify", lambda trigger, **kw: None)


@pytest.fixture
def board(tmp_path: Path):
    """An initialized registry plus a `run()` that invokes main() in-process."""
    reg = tmp_path / "projects" / "demo" / "status" / "registry.md"
    lock = tmp_path / "locks"
    lock.mkdir()
    base = ["--file", str(reg), "--lock-dir", str(lock), "--project", "demo"]
    assert main(["init", *base]) == 0

    class Board:
        path = reg
        lock_dir = lock

        def run(self, *args, timeout: str = "5") -> int:
            return main([args[0], *base, "--timeout", timeout, *args[1:]])

        def run_at(self, path: Path, *args, timeout: str = "5") -> int:
            """Same, against an arbitrary --file (for the unreadable-board cases)."""
            return main(
                [args[0], "--file", str(path), "--lock-dir", str(lock),
                 "--project", "demo", "--timeout", timeout, *args[1:]]
            )

    return Board()


# ---------------------------------------------------------------------------
# The codes, DERIVED. Each helper produces one refusal and returns the real code,
# so a change in registry.py moves these numbers and the prose assertions below fail.
# ---------------------------------------------------------------------------

def _code_deploy_held(board) -> int:
    assert board.run("deploy-lock", "take", "--actor", "jr-dev-1") == 0
    return board.run("deploy-lock", "take", "--actor", "lead-dev")


def _code_agent_known(board) -> int:
    return board.run("deploy-lock", "take", "--actor", "not-a-loop")


def _code_directive_unacked(board) -> int:
    assert board.run("directive", "--actor", "dev-manager", "--text", "hold merges") == 0
    return board.run("deploy-lock", "take", "--actor", "lead-dev")


def _code_edit_lock_held(board) -> int:
    (board.lock_dir / "demo-registry.lock").mkdir()
    try:
        return board.run("deploy-lock", "take", "--actor", "lead-dev", timeout="0")
    finally:
        (board.lock_dir / "demo-registry.lock").rmdir()


def _code_assert_not_holder(board) -> int:
    assert board.run("deploy-lock", "take", "--actor", "jr-dev-1") == 0
    return board.run("deploy-lock", "assert", "--actor", "lead-dev")


def test_the_refusal_keys_the_prose_names_return_the_codes_the_prose_states(board):
    """`deploy_held` is 2 and so are three other things; that distinction is the whole point.

    The prose was originally written as "exit 2 here is normal contention". It is not: 2 is every
    RegistryError. This asserts the four codes the prose now names, derived by running them.
    """
    observed = {
        "deploy_held": _code_deploy_held(board),
        "edit_lock_held": _code_edit_lock_held(board),
        "assert_not_holder": _code_assert_not_holder(board),
    }
    assert observed == {"deploy_held": 2, "edit_lock_held": 3, "assert_not_holder": 5}, observed


def test_exit_2_is_NOT_specific_to_contention_which_is_why_the_prose_says_read_the_key(board):
    """The pin for the falsified claim: two unrelated refusals share code 2.

    If a future change gave contention its own code, this fails and the prose telling readers to
    read the KEY rather than the code would need revisiting -- which is the correct trigger.
    """
    assert _code_agent_known(board) == 2
    assert _code_deploy_held(board) == 2


def test_an_unacknowledged_directive_is_its_OWN_code_not_folded_into_2(board):
    """Exit 4 is what makes "read the standing instructions" mechanical rather than advisory."""
    assert _code_directive_unacked(board) == 4


def test_preflight_exits_nonzero_on_a_board_it_cannot_read(board, tmp_path):
    """The claim "preflight always exits 0" was FALSE, and this is the input that proves it.

    Kept as a live assertion rather than a comment because the prose fix ("it never returns
    nonzero because of what it FOUND") is only worth writing while the distinction is real.
    """
    missing = tmp_path / "nope" / "registry.md"
    assert board.run_at(missing, "preflight", "--actor", "lead-dev", "--action", "merge") == 2
    assert board.run("preflight", "--actor", "not-a-loop", "--action", "merge") == 2
    # ...and it DOES exit 0 on a readable board, which is what makes the never-chain rule bite.
    assert board.run("preflight", "--actor", "lead-dev", "--action", "merge") == 0


def test_preflight_still_exits_0_while_REPORTING_a_directive_that_forbids_the_merge(board):
    """The exact property the never-chain rule rests on, asserted rather than asserted-about.

    This is the sentence the prose now makes: nonzero never comes from what it FOUND. A future
    change that made preflight refuse on an active Directive would break the `&&` guidance in
    four files, and it should fail here first.
    """
    assert board.run("directive", "--actor", "dev-manager", "--text", "do not merge today") == 0
    assert board.run("preflight", "--actor", "lead-dev", "--action", "merge") == 0


# ---------------------------------------------------------------------------
# The prose half.
# ---------------------------------------------------------------------------

# Sentences whose falsity was demonstrated by running the code. Re-introducing any of them is a
# regression to a claim that was measured wrong, so they are banned by text rather than by review.
BANNED = (
    ("returns 0 unconditionally", "preflight exits 2 for a board it cannot read"),
    ("always exits 0", "preflight exits 2 for a board it cannot read"),
    ("Because it cannot fail", "preflight can fail; it just never fails on what it found"),
    ("Exit 2 from the take is\n  ordinary contention", "exit 2 is every RegistryError, not contention"),
    ("Exit 2 here is normal contention", "exit 2 is every RegistryError, not contention"),
)


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name if p.name != "SKILL.md" else p.parent.name)
def test_no_file_reasserts_a_claim_the_CLI_falsified(path: Path):
    text = path.read_text(encoding="utf-8")
    offenders = [(phrase, why) for phrase, why in BANNED if phrase in text]
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} states a claim measured FALSE against the CLI: {offenders}. "
        "These were shipped once and caught in review of the same commit; prefer the narrower true "
        "claim, which is what the surviving text says."
    )


def test_this_gates_list_covers_every_file_that_states_one_of_these_codes():
    """A sixth prose copy added elsewhere must join PROSE_FILES, or this gate silently shrinks.

    The hazard is not a missing file; it is a gate that keeps passing while its coverage falls,
    which is indistinguishable from health.
    """
    marker = re.compile(r"deploy_held|--ack-directive|deploy-lock assert")
    corpus = only_tracked(
        REPO_ROOT,
        list(SKILLS_ROOT.rglob("SKILL.md")) + list((REPO_ROOT / "docs").rglob("*.md")),
    )
    found = {
        p for p in corpus
        if "_vendored" not in p.parts and marker.search(p.read_text(encoding="utf-8", errors="replace"))
    }
    assert set(PROSE_FILES) <= found, (
        "the derivation stopped reaching files PROSE_FILES already names "
        f"({sorted(str(p.relative_to(REPO_ROOT)) for p in set(PROSE_FILES) - found)}). Without "
        "this floor the check below passes on an EMPTY set, which is the silent-shrink the "
        "docstring warns about wearing the fix's clothes."
    )
    missing = sorted(str(p.relative_to(REPO_ROOT)) for p in found - set(PROSE_FILES))
    assert not missing, (
        f"{missing} describe the deploy-lock guard but are not in PROSE_FILES, so the banned-claim "
        "check above never reads them. Add them to PROSE_FILES."
    )


# ---------------------------------------------------------------------------
# `--break-stale` and the exit-2 enumeration (gh#504, gh#246 item 3).
# ---------------------------------------------------------------------------

BUILDER_SKILLS = tuple(p for p in PROSE_FILES if p.parent.name in ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3"))
TAKE_ENUMERATORS = tuple(p for p in PROSE_FILES if p.name == "SKILL.md")


def _flat(path: Path) -> str:
    """Prose with emphasis markers dropped and wrapping collapsed, so a re-flow is not a failure."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8").replace("**", ""))


@pytest.mark.parametrize("path", BUILDER_SKILLS, ids=lambda p: p.parent.name)
def test_the_liveness_probe_is_a_precondition_for_passing_break_stale(path: Path):
    """Q-10082: the 45-minute test makes the break AVAILABLE; the probe decides whether to USE it.

    "if you judge it dead" left the judgement to whatever the lane felt like checking, and a long
    review looks exactly like death from two stale stamps alone.
    """
    text = _flat(path)
    hits = re.findall(r"full liveness probe[^.]*precondition for passing `--break-stale`", text)
    # lead-dev states the rule twice: the deploy-lock paragraph AND the merge-preflight refusal
    # step, which is the one a lane is actually on when deploy_stale_unbroken comes back.
    need = 2 if path.parent.name == "lead-dev" else 1
    assert len(hits) >= need, (
        f"{path.relative_to(REPO_ROOT)} must say the full liveness probe is a precondition for "
        f"passing --break-stale ({need} place(s), found {len(hits)})"
    )
    assert "judge it dead" not in text and "judge the holder dead" not in text, (
        f"{path.relative_to(REPO_ROOT)} still leaves the break to an unprobed judgement"
    )


def test_the_take_exit_2_keys_the_prose_names_are_real_rules():
    """The examples below are pinned by name, so each name must be a key the CLI can raise."""
    from agentflow.registry import RULES

    for key in ("deploy_held", "deploy_stale_unbroken", "deploy_builder", "agent_known",
                "file_missing", "file_unreadable", "file_damaged"):
        assert key in RULES, key


@pytest.mark.parametrize("path", TAKE_ENUMERATORS, ids=lambda p: p.parent.name)
def test_the_take_exit_2_enumeration_closes_by_negation(path: Path):
    """gh#246 item 3: a list of malformed-call keys went stale the day `file_unreadable` and
    `file_damaged` were added. "Every other exit 2" cannot; the named examples are still checked
    so the newer keys are visible to a reader who greps for them.
    """
    text = _flat(path)
    m = re.search(r"every other exit 2(.{0,400})", text, re.IGNORECASE)
    assert m, f"{path.relative_to(REPO_ROOT)} must close the take's exit-2 keys by negation"
    if path.parent.name != "lead-dev":  # lead-dev already closed by negation; gh#246 left it be
        for key in ("file_unreadable", "file_damaged"):
            assert f"`{key}`" in m.group(1), f"{path.relative_to(REPO_ROOT)} does not name {key}"


@pytest.mark.parametrize("path", PROSE_FILES + (REPO_ROOT / "skills" / "_shared" / "agentflow" / "registry.py",),
                         ids=lambda p: p.parent.name if p.name == "SKILL.md" else p.name)
def test_no_surface_leaves_break_stale_to_an_unprobed_judgement(path: Path):
    """The orchestrator's copy and the CLI's own refusal message are read at the moment of deciding."""
    text = _flat(path)
    assert "judge it dead" not in text and "judge the holder dead" not in text, (
        f"{path.relative_to(REPO_ROOT)} still says the break is a judgement call"
    )


@pytest.mark.parametrize("path", [p for p in BUILDER_SKILLS if p.parent.name != "lead-dev"],
                         ids=lambda p: p.parent.name)
def test_the_copied_probe_thresholds_match_lead_devs_liveness_paragraph(path: Path):
    """jr-dev skills restate the probe inline; a threshold moved in lead-dev must fail here too."""
    lead = _flat(SKILLS_ROOT / "lead-dev" / "SKILL.md")
    liveness = lead[lead.index("Liveness is evidence, not a timestamp."):][:600]
    for thresh in ("heartbeat staler than 3h", "in the last 3h", "modified in the last 30 min"):
        assert thresh in liveness, f"lead-dev's Liveness paragraph no longer says {thresh!r}"
    text = _flat(path)
    for thresh in ("heartbeat staler than 3h", "branch in 3h", "modified in 30 min"):
        assert thresh in text, f"{path.relative_to(REPO_ROOT)} lost the probe threshold {thresh!r}"
