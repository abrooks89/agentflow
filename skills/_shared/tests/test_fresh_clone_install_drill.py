"""The fresh-clone drill's own logic, mutation-tested in CI so its limbs can still fail.

The drill itself lives in `harness/fresh-clone-install-drill/` because it is meant to be RUN
against a real clone and CITED by an auditor. This module is the other half.

It does NOT clone and does NOT provision. Both are networked and take minutes, and a suite
that does them stops being run - at which point the drill's limbs are asserted rather than
gated, which is precisely the failure the drill exists to correct. Instead every external
command goes through the drill's `Runner` seam, and this module injects a fake one plus a
pre-built sandbox, so a whole run takes milliseconds and touches nothing outside `tmp_path`.

Each test here breaks one limb's assertion and requires the drill to FAIL. Every one of them
is paired with a CONTROL rig, built identically and left unmutated, which must PASS in the
same test. Without the control, a mutation test proves only that the drill failed, not that
it failed BECAUSE of the mutation - and a mutation that does not actually remove the property
under test proves nothing at all. `test_a_cosmetic_mutation_does_NOT_kill_the_handover_limb`
holds the other end of that: an edit to incidental wording must leave the limb green, or the
limb is keying on the transcript rather than on the behaviour.

LIMB 2 INVERTED WHEN gh#186's FIRST FINDING WAS FIXED, and these tests inverted with it. It
used to require the PyYAML-less run to DIE; it now requires that run to COMPLETE via the
step-4 handover into the suite venv. The three tests around it hold the three outcomes apart:
a signature-carrying death is a REGRESSION named as such, any other death must not be
laundered into that issue, and a success without the handover line in the transcript is a
green from a run that never touched the mechanism under test.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DRILL_PATH = REPO_ROOT / "harness" / "fresh-clone-install-drill" / "fresh_clone_install_drill.py"

FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"
FAKE_TREE = "fedcba9876543210fedcba9876543210fedcba98"
FAKE_BARE_PYTHON = "fake-bare-python"
FAKE_PRETTY_NAME = "Ubuntu 24.04.1 LTS"
FAKE_BARE_VERSION = "3.12.3"
FAKE_VENV_VERSION = "3.12.7"


def test_drill_subtree_dirs_match_the_provisioner(drill):
    """The drill's _SUBTREE_DIRS is a SECOND transcription of provision.DATA_SUBTREE_DIRS.

    Why this assertion exists rather than a comment: the drill's own check is
    presence-only, so a drift stays GREEN while silently verifying fewer directories
    than a fresh clone actually produces. That is the failure this test makes loud —
    it was found live when `wiki/pages/{done,archived}` were added to the provisioner
    and this copy was left behind, and the fresh-clone drill is precisely the artifact
    a reverify epic trusts.
    """
    spec = importlib.util.spec_from_file_location(
        "_provision_for_subtree_crosscheck", REPO_ROOT / "install" / "provision.py"
    )
    provision = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = provision
    spec.loader.exec_module(provision)
    assert drill._SUBTREE_DIRS == provision.DATA_SUBTREE_DIRS


DRILL_README = REPO_ROOT / "harness" / "fresh-clone-install-drill" / "README.md"

#: Spelled-out counts the limb-4 sentence could plausibly carry. Deliberately not a
#: general number parser: the point is to read the ONE sentence a human reads, and a
#: word this map does not know is a failure worth seeing rather than a silent skip.
_COUNT_WORDS = {
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def test_the_drill_README_states_the_SUBTREE_DIRS_count_it_actually_checks(drill):
    """The limb-4 sentence is a THIRD transcription of the same tuple, and the only
    one nothing checked (gh#329).

    `_SUBTREE_DIRS` is pinned against the provisioner by the test above, so the two code
    copies cannot drift. The README said SIX while both tuples held EIGHT - it was written
    before `wiki/pages/{done,archived}` landed and nothing carried the sentence forward.

    Why that is worth a test and not just a corrected word: the number is the only part of
    the sentence a reader can check the limb against, and it is what they read INSTEAD of
    reading the limb. A doc that undercounts a gate makes the gate look narrower than it is,
    which is the direction that gets a gate deleted as redundant.
    """
    text = DRILL_README.read_text(encoding="utf-8")
    match = re.search(r"all (\w+) data-subtree", text)
    assert match is not None, (
        "limb 4's sentence no longer matches 'all <count> data-subtree'. If it was"+
        " reworded, reword this assertion with it - do not delete it."
    )
    word = match.group(1)
    assert word in _COUNT_WORDS, (
        f"the README says {word!r} data-subtree directories; that is not a count this test"
        f" knows how to read, so it cannot be checked against the tuple"
    )
    assert _COUNT_WORDS[word] == len(drill._SUBTREE_DIRS), (
        f"limb 4 claims {word} ({_COUNT_WORDS[word]}) data-subtree directories but"
        f" _SUBTREE_DIRS holds {len(drill._SUBTREE_DIRS)}"
    )


@pytest.fixture(scope="module")
def drill():
    if not DRILL_PATH.is_file():
        # NOT pytest.skip. The drill is tracked in this repository, beside this test, so
        # its absence is breakage - a rename, a bad merge, a deletion - and never an
        # environmental condition. A skip here would turn all 30 assertions green-by-
        # vacancy at the exact moment the thing they guard stopped existing.
        raise AssertionError(f"drill missing at {DRILL_PATH} - it is tracked; this is breakage")
    spec = importlib.util.spec_from_file_location("_fresh_clone_drill_for_test", DRILL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# The rig: a fake Runner replaying the drill's measured happy path
# --------------------------------------------------------------------------- #
# The gh#186 death, kept for the REGRESSION test. This is the transcript the limb used to
# require and must now name as a regression - the fix is only guarded if this exact shape
# still turns the drill red.
BARE_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "install/provision.py", line 483, in _do_editable_install\n'
    '  File "skills/_shared/agentflow/config.py", line 33, in <module>\n'
    "ModuleNotFoundError: No module named 'yaml'\n"
)

# The fixed behaviour, and the rig's default: steps 1-4 in the bare interpreter, the handover
# line, then the child's steps 5-12 on the same captured stdout. The handover line is quoted
# from provision.py's `_reexec_into_venv`; a test below re-derives it from that source so this
# fixture cannot drift into agreeing with a drill that stopped recognizing the real thing.
BARE_HANDOVER_STDOUT = (
    "agentflow provision - applying\n"
    "  4. Python venv + PyYAML (only pipeline dep) ...\n"
    "  -- handing off to the suite venv (<sandbox>/clone/.venv/bin/python) for steps 5+\n"
    "  5. Editable install of skills/_shared ...\n"
    "  11. Scaffold data_root subtree ...\n"
    "  11. done.\n"
)

DRY_RUN_STDOUT = (
    "agentflow provision - DRY RUN (no changes will be made)\n"
    "Ordered install steps:\n"
    "  1. Preflight: verify manifest runtimes\n"
    "  11. Scaffold data_root subtree\n"
)

INSTALL_STDOUT = (
    "agentflow provision - applying\n"
    "  1. Preflight: verify manifest runtimes ...\n"
    "  1. done.\n"
    "  11. Scaffold data_root subtree ...\n"
    "  11. done.\n"
    "Resolved-surface check (python -m agentflow.doctor):\n"
    "  resolved surface healthy\n"
)

SELF_TEST_STDOUT = (
    "# SELF-TEST: agentflow harness check.py wiring\n"
    "PASS self-test.config: harness.config.example.json loads\n"
    "PASS self-test.registry: 43 assertions wired (A1..G1)\n"
)

ONLY_CFG_STDOUT = (
    "PASS C1: loud hard-fail on a malformed plan\n"
    "# summary: 19 PASS  0 FAIL  0 SKIP  (behavioral FAIL=0, structural FAIL=0)\n"
)

FULL_STDOUT = (
    "SKIP A2: unrecorded: no harness/records/scenarioA/plan.md on this machine "
    "(scenario A not recorded) - unproven here rather than violated; record it per "
    "harness/run-scenario.md, Scenario A\n"
    "# summary: 19 PASS  0 FAIL  25 SKIP  (behavioral FAIL=0, structural FAIL=0)\n"
    "# unrecorded: scenario A - harness/records/scenarioA/plan.md (A1, A2, A3, A4, A5, A6, "
    "A7, A8, A9)\n"
    "# EXIT 3: no behavioral FAIL, but scenario(s) A, B, D are unrecorded on this machine - "
    "record them per harness/run-scenario.md, or narrow with --only (exit 1 means a real "
    "FAIL)\n"
)


class FakeRunner:
    """Replays the drill's measured outcomes. One editable dict per command class.

    Classifying by command class rather than by call index is deliberate: a test that
    mutated "the third call" would silently start mutating a different limb the moment
    anyone reordered the drill, and would keep reporting green.
    """

    def __init__(self, drill, sandbox):
        self.drill = drill
        self.sandbox = sandbox
        self.calls: list[str] = []
        self.side_effects: dict = {}
        C = drill.Completed
        self.outcomes = {
            "yaml-probe": C(1, "", "ModuleNotFoundError: No module named 'yaml'\n"),
            # The interpreter-version gate. New-enough by default so the happy path runs; a
            # test flips it to prove a too-old machine SKIPs limb 2 rather than FAILing it.
            "version-probe": C(0, "", ""),
            "provision-dry": C(0, DRY_RUN_STDOUT, ""),
            "provision-bare": C(0, BARE_HANDOVER_STDOUT, ""),
            "provision-venv": C(0, INSTALL_STDOUT, ""),
            "check-selftest": C(0, SELF_TEST_STDOUT, ""),
            "check-only": C(0, ONLY_CFG_STDOUT, ""),
            "check-full": C(3, FULL_STDOUT, ""),
            # The interpreters' own X.Y.Z. Two DIFFERENT versions on purpose, so a record that
            # printed one of them on both lines cannot pass the version test below.
            "pyver-bare": C(0, FAKE_BARE_VERSION + "\n", ""),
            "pyver-venv": C(0, FAKE_VENV_VERSION + "\n", ""),
            # The real clone path. Only reached when a test runs the drill WITHOUT the
            # sha/tree seam, so the identity the record prints is the one clone_repo returned.
            "git-clone": C(0, "", ""),
            "git-checkout": C(0, "", ""),
            "git-rev-parse-sha": C(0, FAKE_SHA + "\n", ""),
            "git-rev-parse-tree": C(0, FAKE_TREE + "\n", ""),
        }

    def key(self, cmd: list[str]) -> str:
        joined = " ".join(cmd)
        if cmd[0] == "git":
            if "clone" in cmd:
                return "git-clone"
            if "checkout" in cmd:
                return "git-checkout"
            if "rev-parse" in cmd:
                return "git-rev-parse-tree" if cmd[-1] == "HEAD^{tree}" else "git-rev-parse-sha"
        if "import yaml" in joined:
            return "yaml-probe"
        if "version_info" in joined:
            return "version-probe"
        if "python_version()" in joined:
            return "pyver-bare" if cmd[0] == FAKE_BARE_PYTHON else "pyver-venv"
        if "provision.py" in joined:
            if "--dry-run" in cmd:
                return "provision-dry"
            return "provision-bare" if cmd[0] == FAKE_BARE_PYTHON else "provision-venv"
        if "check.py" in joined:
            if "--self-test" in cmd:
                return "check-selftest"
            return "check-only" if "--only" in cmd else "check-full"
        return "unexpected: " + joined

    def run(self, cmd, cwd=None, env=None, timeout=1800):
        k = self.key(cmd)
        self.calls.append(k)
        effect = self.side_effects.get(k)
        if effect is not None:
            effect()
        if k not in self.outcomes:
            raise AssertionError(f"the drill ran a command this rig does not model: {k}")
        return self.outcomes[k]


class Rig:
    """A sandbox plus a fake Runner, pre-seeded so an unmutated run passes every limb."""

    def __init__(self, drill, root: Path):
        self.drill = drill
        self.out = root
        self.sandbox = drill.build_sandbox(root / "sandbox")
        self.runner = FakeRunner(drill, self.sandbox)
        # The os-release reader the drill is handed. A test swaps it to prove the OS line is
        # read from PRETTY_NAME, and that a machine without os-release still gets a label.
        self.os_release = lambda: {"PRETTY_NAME": FAKE_PRETTY_NAME}
        self._seed_clone()
        self._seed_install_artifacts()

    def _seed_clone(self):
        sb = self.sandbox
        (sb.clone / "harness").mkdir(parents=True, exist_ok=True)
        (sb.clone / "install").mkdir(parents=True, exist_ok=True)
        (sb.clone / "harness" / "harness.config.example.json").write_text(
            json.dumps({"specs_dir": "<PLACEHOLDER>"}), encoding="utf-8")
        # The interpreter step 4 builds, so `ensure_suite_venv` is a no-op in the rig.
        sb.venv_python.parent.mkdir(parents=True, exist_ok=True)
        sb.venv_python.write_text("#!fake\n", encoding="utf-8")

    def _seed_install_artifacts(self):
        """Everything limb 4 goes looking for, as the real install would have left it."""
        sb = self.sandbox
        (sb.claude_root / ".agentflow-deploy.json").write_text("{}", encoding="utf-8")
        (sb.claude_root / "skills" / "lead-dev").mkdir(parents=True, exist_ok=True)
        (sb.claude_root / "commands").mkdir(parents=True, exist_ok=True)
        (sb.claude_root / "commands" / "lead-dev.md").write_text("x", encoding="utf-8")
        sb.settings_file.write_text(
            json.dumps({"hooks": {"SessionStart": [{"hooks": []}],
                                  "PreToolUse": [{"hooks": []}]}}), encoding="utf-8")
        for sub in self.drill._SUBTREE_DIRS:
            (sb.project_dir / sub).mkdir(parents=True, exist_ok=True)
        (sb.project_dir / "status" / "registry.md").write_text(
            "# Fleet Registry\n", encoding="utf-8")

    def run(self, now=None, bare_python=FAKE_BARE_PYTHON, clone=False) -> str:
        # `bare_python` is a parameter, not a constant, so a test can exercise the PROBE path
        # (bare_python=None) as well as the --bare-python path. The two branches have
        # different failure semantics - explicit is a loud refusal, probed is a stated SKIP.
        return self.drill.run_drill(
            self.out / "record", now=now, runner=self.runner,
            sandbox=self.sandbox, bare_python=bare_python, read_os_release=self.os_release,
            # `clone=True` drops the seam so the identity comes from clone_repo's real path.
            **({} if clone else {"sha": FAKE_SHA, "tree": FAKE_TREE}),
        )


@pytest.fixture
def rig(drill, tmp_path):
    def _make(name: str) -> Rig:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        return Rig(drill, root)
    return _make


# --------------------------------------------------------------------------- #
# The control: an unmutated rig passes every limb
# --------------------------------------------------------------------------- #
def test_the_drill_passes_on_an_unmutated_rig(rig):
    """The baseline every mutation test is measured against.

    If this ever goes red, every mutation test below becomes vacuous - they would all raise
    DrillFailure regardless of what they broke.
    """
    record = rig("control").run()
    assert "# Fresh-clone install drill" in record
    assert FAKE_SHA in record


def test_the_drill_writes_only_under_the_out_dir_it_was_given(rig, tmp_path):
    """It builds a config home, a data root and a Claude root, so it must be containable.

    `--out` defaults to a temp dir for the same reason: a run that reached the machine's real
    `~/.claude` or real data root would be indistinguishable from an install nobody asked for.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    rig("contained").run()
    assert not list(elsewhere.rglob("*")), "the drill wrote outside its out_dir"


# --------------------------------------------------------------------------- #
# Limb 1 - dry run
# --------------------------------------------------------------------------- #
def test_the_drill_FAILS_when_the_dry_run_exits_nonzero(drill, rig):
    control = rig("c1")
    control.run()                                   # control: green before the mutation
    broken = rig("m1")
    broken.runner.outcomes["provision-dry"] = drill.Completed(2, "", "boom")
    with pytest.raises(drill.DrillFailure, match="dry-run exited 2"):
        broken.run()


def test_the_drill_FAILS_when_the_dry_run_mutates_the_data_root(drill, rig):
    """A dry run that also creates a directory still exits 0 and still reads right.

    This is the only limb that can catch it, because the promise ("writes nothing") is not
    something the installer's own exit code can report on.
    """
    control = rig("c2")
    control.run()
    broken = rig("m2")
    stray = broken.sandbox.data_root / "written-by-a-dry-run"

    def _mutate():
        stray.mkdir(parents=True, exist_ok=True)

    broken.runner.side_effects["provision-dry"] = _mutate
    with pytest.raises(drill.DrillFailure, match="MUTATED the sandbox"):
        broken.run()
    assert stray.is_dir(), "the mutation must actually have happened, not just been declared"


def test_the_drill_FAILS_when_the_dry_run_touches_the_claude_root_not_the_data_root(drill, rig):
    """The likelier leak site, and the one a data-root-only check would have missed.

    Step 7 deploys into the Claude root and steps 8-9 rewrite the settings file, so a dry-run
    regression would show up THERE first. The original limb compared `data_root` alone and
    would have stayed green through all of it.
    """
    rig("c1").run()
    broken = rig("c2")
    stray = broken.sandbox.claude_root / "skills" / "leaked-by-a-dry-run"

    def _mutate():
        stray.mkdir(parents=True, exist_ok=True)

    broken.runner.side_effects["provision-dry"] = _mutate
    with pytest.raises(drill.DrillFailure, match="MUTATED the sandbox"):
        broken.run()
    assert stray.is_dir()


def test_the_drill_FAILS_when_the_dry_run_REWRITES_an_existing_file(drill, rig):
    """"Byte-for-byte" is what the record claims, so a path-set comparison is not enough.

    Nothing is added or removed here - an existing file's CONTENTS change. A check that
    compared only the set of paths would report the sandbox untouched.
    """
    rig("b1").run()
    broken = rig("b2")
    victim = broken.sandbox.settings_file

    def _mutate():
        victim.write_text('{"clobbered": true}', encoding="utf-8")

    before = victim.read_text(encoding="utf-8")
    broken.runner.side_effects["provision-dry"] = _mutate
    with pytest.raises(drill.DrillFailure, match="CHANGED"):
        broken.run()
    assert victim.read_text(encoding="utf-8") != before, "the mutation did not change anything"


# --------------------------------------------------------------------------- #
# Limb 2 - the PyYAML-less run and its venv handover (gh#186's regression guard)
# --------------------------------------------------------------------------- #
def test_the_handover_limb_FAILS_as_a_REGRESSION_when_the_pyyaml_less_run_dies(drill, rig):
    """The whole reason the limb was inverted instead of deleted.

    This is the exact transcript gh#186 produced, replayed. Before the fix it was the limb's
    PASS; after it, it must turn the drill red AND say the word regression with the issue
    named, because "the install broke again the way it broke last time" is a different message
    from "the install broke" and only one of them points at the code that regressed.
    """
    control = rig("c3")
    control.run()                                   # control: green before the mutation
    broken = rig("m3")
    broken.runner.outcomes["provision-bare"] = drill.Completed(
        1, "agentflow provision - applying\n", BARE_TRACEBACK)
    with pytest.raises(drill.DrillFailure, match="REGRESSION OF gh#186"):
        broken.run()


def test_the_handover_limb_FAILS_when_the_failure_is_a_DIFFERENT_failure(drill, rig):
    """A non-zero exit is not the property under test; the named ModuleNotFoundError is.

    The discipline the witness had, preserved pointing the other way. Without it any breakage
    at all - a missing git, a bad config, a permissions error - would be recorded as gh#186
    coming back, sending the next reader to a handover that is working fine while whatever
    actually broke goes unnamed.
    """
    control = rig("c4")
    control.run()
    broken = rig("m4")
    broken.runner.outcomes["provision-bare"] = drill.Completed(
        1, "", "agentflow provision FAILED:\nGit not found on PATH (required)\n")
    with pytest.raises(drill.DrillFailure, match="NOT with the ModuleNotFoundError"):
        broken.run()
    # And it must not have used the regression wording for a failure that is not one.
    try:
        broken.run()
    except drill.DrillFailure as exc:
        assert "REGRESSION" not in str(exc), "an unrelated failure was laundered into gh#186"


def test_the_handover_limb_FAILS_when_the_run_never_HANDED_OVER(drill, rig):
    """The mirror image of the old blind spot, and the one a green would hide.

    Exit 0 and `11. done.` are exactly what an interpreter that HAD PyYAML all along prints:
    no handover, because none was needed. Accepting that would make the limb pass on a machine
    where the fix was never executed - and unlike the old vacuous green, this one looks like
    success, so nobody would go looking.
    """
    control = rig("c3b")
    control.run()
    broken = rig("m3b")
    broken.runner.outcomes["provision-bare"] = drill.Completed(
        0,
        "agentflow provision - applying\n  5. Editable install ...\n  11. done.\n",
        "",
    )
    with pytest.raises(drill.DrillFailure, match="never printed the handover line"):
        broken.run()


def test_the_handover_limb_FAILS_when_the_handed_over_child_stops_early(drill, rig):
    """The handover returns the CHILD's exit code, so a child that quit early exits 0 too.

    Same shape as limb 3's `11. done.` check and deliberately the same constant, because the
    handover adds a second process that can stop early while the parent still reports success.
    """
    control = rig("c3c")
    control.run()
    broken = rig("m3c")
    broken.runner.outcomes["provision-bare"] = drill.Completed(
        0,
        "  -- handing off to the suite venv (<sandbox>/clone/.venv/bin/python) for steps 5+\n"
        "  5. Editable install ...\n  5. done.\n",
        "",
    )
    with pytest.raises(drill.DrillFailure, match="never printed `11. done.`"):
        broken.run()


def test_a_cosmetic_mutation_does_NOT_kill_the_handover_limb(drill, rig):
    """The other end of non-vacuity: the limb must key on the behaviour, not on the transcript.

    A mutation that leaves the property under test intact has to leave the limb GREEN. If
    reworded banners, different step wording or a different venv path turned this limb red,
    its passes would be reporting on incidental text and every mutation kill above would be
    measuring the wrong thing.
    """
    rewritten = rig("cosmetic")
    rewritten.runner.outcomes["provision-bare"] = drill.Completed(
        0,
        "completely different banner\n"
        "  -- handing off to the suite venv (D:/somewhere/else/.venv/Scripts/python.exe) "
        "for steps 5+\n"
        "  different step wording entirely ...\n"
        "  11. done.\n",
        "",
    )
    record = rewritten.run()
    assert "PASS - PyYAML-less run completes via the venv handover" in record.replace("### ", "")


def test_the_handover_signature_matches_what_the_provisioner_actually_prints(drill):
    """The drill's handover pattern is a SECOND transcription of a line provision.py emits.

    Derived from that source rather than trusted, for the same reason `_SUBTREE_DIRS` is
    cross-checked above: nothing about a reworded print statement fails loudly on its own, and
    the failure it would cause here is a silent one - the limb would start rejecting every
    real handover, or (worse, once someone relaxed it) stop noticing that none happened.
    """
    src = (REPO_ROOT / "install" / "provision.py").read_text(encoding="utf-8")
    m = re.search(r'print\(f"([^"]*handing off[^"]*)"\)', src)
    assert m, "provision.py no longer prints a `handing off ...` line the drill can key on"
    rendered = m.group(1).replace("{py}", "/somewhere/.venv/bin/python").replace(
        "{resume_from}", "5")
    assert drill._HANDOVER_SIGNATURE.search(rendered), (
        f"the drill's handover pattern does not match what provision.py prints: {rendered!r}"
    )


def test_the_handover_limb_SKIPS_loudly_when_no_bare_interpreter_exists(drill, rig, monkeypatch):
    """A skip must be legible as a skip, never as a quiet pass.

    The limb cannot run without a PyYAML-less interpreter, and on a machine that has none the
    honest outcome is "not run". The text has to say so in both directions - a skip is not
    evidence the fix holds any more than it was evidence the bug was present.
    """
    monkeypatch.setattr(drill, "find_bare_python", lambda runner, explicit=None: (None, "none on PATH"))
    record = rig("skip").run()
    assert "SKIPPED, not passed" in record
    assert "none on PATH" in record
    assert "says NOTHING about gh#186 in either direction" in record


def test_an_explicit_bare_python_that_HAS_pyyaml_is_a_loud_failure(drill, rig):
    """The operator asked for the limb; handing it the wrong interpreter must not skip quietly.

    Louder now than it was under the witness: an interpreter WITH PyYAML never hands over, so
    the mistake no longer produces a confusing red - it produces a plausible green.
    """
    r = rig("badbare")
    r.runner.outcomes["yaml-probe"] = drill.Completed(0, "", "")
    with pytest.raises(drill.DrillFailure, match="HAS PyYAML installed"):
        r.run()


# --------------------------------------------------------------------------- #
# Limb 3 - the real install
# --------------------------------------------------------------------------- #
def test_the_drill_FAILS_when_the_real_install_exits_nonzero(drill, rig):
    control = rig("c5")
    control.run()
    broken = rig("m5")
    broken.runner.outcomes["provision-venv"] = drill.Completed(1, "", "provision FAILED")
    with pytest.raises(drill.DrillFailure, match="exited 1, not 0"):
        broken.run()


def test_the_drill_FAILS_when_the_install_exits_0_without_reaching_the_last_step(drill, rig):
    """Exit 0 that never printed `11. done.` is the shape of an early stop reporting success.

    Found by asking what a green exit code alone would have accepted: an installer that
    returned 0 from step 3 would satisfy every other check in this limb.
    """
    control = rig("c6")
    control.run()
    broken = rig("m6")
    broken.runner.outcomes["provision-venv"] = drill.Completed(
        0, "agentflow provision - applying\n  3. done.\n", "")
    with pytest.raises(drill.DrillFailure, match="never printed step 11 done"):
        broken.run()


# --------------------------------------------------------------------------- #
# Limb 4 - the artifacts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("victim,needle", [
    ("manifest", "deploy manifest"),
    ("skills", "at least one deployed skill"),
    ("commands", "at least one deployed command"),
    ("hook", "SessionStart hook"),
    ("subtree", "data subtree dir"),
    ("registry", "registry.md"),
])
def test_the_drill_FAILS_when_a_claimed_artifact_is_absent(drill, rig, victim, needle):
    """Every artifact the twelve steps claim is individually load-bearing.

    Parametrized rather than written once so a limb that checked only the easy one (the
    manifest) could not pass this file. Each case deletes exactly one thing and requires the
    drill to name it.
    """
    control = rig(f"c7-{victim}")
    control.run()

    broken = rig(f"m7-{victim}")
    sb = broken.sandbox
    if victim == "manifest":
        (sb.claude_root / ".agentflow-deploy.json").unlink()
    elif victim == "skills":
        (sb.claude_root / "skills" / "lead-dev").rmdir()
    elif victim == "commands":
        (sb.claude_root / "commands" / "lead-dev.md").unlink()
    elif victim == "hook":
        sb.settings_file.write_text("{}", encoding="utf-8")
    elif victim == "subtree":
        # rmtree, not rmdir: `wiki/pages` now has `done/` and `archived/` beneath it, so a
        # bare rmdir raises "directory not empty" and the test dies on its own setup instead
        # of on the assertion it exists to make.
        shutil.rmtree(sb.project_dir / "wiki" / "pages")
    elif victim == "registry":
        (sb.project_dir / "status" / "registry.md").write_text("   \n", encoding="utf-8")

    with pytest.raises(drill.DrillFailure, match=needle):
        broken.run()


# --------------------------------------------------------------------------- #
# Limbs 5 and 6 - the golden script
# --------------------------------------------------------------------------- #
def test_the_drill_FAILS_when_the_self_test_is_red(drill, rig):
    control = rig("c8")
    control.run()
    broken = rig("m8")
    broken.runner.outcomes["check-selftest"] = drill.Completed(1, "FAIL self-test.config\n", "")
    with pytest.raises(drill.DrillFailure, match="--self-test` exited 1"):
        broken.run()


def test_the_drill_FAILS_when_the_scenario_groups_are_red(drill, rig):
    control = rig("c9")
    control.run()
    broken = rig("m9")
    broken.runner.outcomes["check-only"] = drill.Completed(
        1, "# summary: 3 PASS  16 FAIL  0 SKIP\n", "")
    with pytest.raises(drill.DrillFailure, match="only C,F,G` exited 1"):
        broken.run()


def test_the_drill_FAILS_on_a_green_exit_with_no_assertions_run(drill, rig):
    """Exit 0 with no `# summary:` line is a vacuous green, and this drill will not record one.

    This is the failure mode the whole file exists to prevent: a gate that reports success
    because it ran nothing. `--only` with a group letter nothing matches produces exactly
    this shape, and the exit code alone cannot tell it from a real pass.
    """
    control = rig("c10")
    control.run()
    broken = rig("m10")
    broken.runner.outcomes["check-only"] = drill.Completed(0, "# agentflow harness check.py\n", "")
    with pytest.raises(drill.DrillFailure, match="no `# summary:` line"):
        broken.run()


def test_the_drill_FAILS_when_the_summary_reports_zero_assertions(drill, rig):
    """A summary of 0 PASS / 0 FAIL is the same vacuity wearing a summary line."""
    control = rig("c11")
    control.run()
    broken = rig("m11")
    broken.runner.outcomes["check-only"] = drill.Completed(
        0, "# summary: 0 PASS  0 FAIL  0 SKIP  (behavioral FAIL=0, structural FAIL=0)\n", "")
    with pytest.raises(drill.DrillFailure, match="not the clean, non-empty result"):
        broken.run()


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #
def test_the_record_carries_every_section_an_auditor_needs(rig):
    """A record that says "passed" and nothing else cannot be cited, only believed.

    The evidence this drill replaces was a PASS cited to a commit that no longer exists, so
    the sha, the clock and the platform are not decoration - they are the whole difference
    between a citation that resolves and one that does not.
    """
    now = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    record = rig("record").run(now=now)
    for needle in (
        "# Fresh-clone install drill",
        "2026-09-03T14:30Z",
        FAKE_SHA,
        "**OS:**",
        "**Suite interpreter:**",
        "## Limbs",
        "## Golden script numbers",
        "## What this establishes",
        "## What it deliberately does not establish",
    ):
        assert needle in record, f"the record is missing {needle!r}"


def test_the_record_carries_the_golden_numbers_it_measured(rig):
    """The counts, not just the verdict - a later run's drift is only visible against numbers."""
    record = rig("golden").run()
    assert "19 PASS / 0 FAIL / 0 SKIP" in record
    assert "19 PASS / 0 FAIL / 25 SKIP, exit 3" in record
    assert "1 skill(s), 1 command(s)" in record


def test_every_limb_states_what_it_does_NOT_cover(drill, rig):
    """A PASS with no stated blind spot is the one an auditor over-reads.

    Asserted per limb rather than once over the document, because a single "does not cover"
    line somewhere in the record would satisfy a whole-document check while five limbs
    quietly shipped bare verdicts.
    """
    record = rig("caveats").run()
    assert record.count("*Does not cover:*") == len(drill.LIMBS)


def test_the_record_names_gh186_as_guarded_and_its_second_finding_as_FIXED_as_gh322(drill, rig):
    """What the record must say now, and what it must no longer say.

    The old scare text ("witnesses an open defect", "green here means the bug is still
    present") became FALSE the moment the fix merged, and a record is pasted into PRs and
    audits by people who never open the module - so a stale sentence there is a false claim in
    the one artifact designed to travel. Asserted as an absence as well as a presence, because
    inverting a limb and leaving its prose behind is the easy half-done version of this
    change.

    The second finding went the same way (gh#372): it was split out as gh#322 and fixed, and
    the record kept calling it open. This test used to REQUIRE "SECOND FINDING IS STILL OPEN";
    it is inverted rather than deleted, for the same reason limb 2 was. The honest residue
    stays asserted: the drill still does not exercise a malformed config, and the record has
    to point at the tests that do rather than let a green read as covering it.
    """
    record = rig("guard").run()
    assert "gh#186" in record
    assert "regression guard" in record
    assert "gh#322, is fixed for the case it recorded" in record
    assert "install/test_provision_fresh_machine.py" in record, (
        "the record must say where the malformed-config case IS covered, since the drill is not"
    )
    for stale in ("WITNESSES AN OPEN DEFECT", "GREEN HERE MEANS THE BUG IS STILL PRESENT",
                  "INVERT THIS LIMB", "STILL OPEN: the documented command"):
        assert stale not in record, f"the record still carries the pre-fix claim {stale!r}"
    for stale in ("SECOND FINDING IS STILL OPEN", "SECOND finding is not fixed"):
        assert stale.lower() not in record.lower(), (
            f"the record still calls gh#322 open ({stale!r}) - it shipped (gh#372)"
        )


def test_the_record_names_the_limits_it_refuses_to_claim(rig):
    """The four honest limits stay in the artifact, not in someone's memory of writing it."""
    record = rig("limits").run()
    low = record.lower()
    assert "one os only" in low, "the single-platform limit must stay named"
    assert "record-dependent scenario groups did not run" in low
    assert "human interview was never exercised" in low
    assert "autodoc" in low


# --------------------------------------------------------------------------- #
# The sandbox contract
# --------------------------------------------------------------------------- #
def test_the_child_env_sandboxes_the_resolver_not_just_the_flag(drill, tmp_path, monkeypatch):
    """`--config-home` alone does not contain this installer, and the drill must know that.

    `agentflow.config.config_home()` reads `$AGENTFLOW_CONFIG` or `~/.agentflow/config` and
    takes no argument, so steps 10 and 11 would resolve to the machine's REAL config and
    scaffold into the REAL data root while the flag steered provision's own bookkeeping.
    Exporting the variable is what actually contains the run.
    """
    monkeypatch.setenv("AGENTFLOW_PROJECT", "some-ambient-project")
    sb = drill.build_sandbox(tmp_path / "sandbox")
    env = drill.child_env(sb)
    assert env["AGENTFLOW_CONFIG"] == str(sb.config_home)
    assert "AGENTFLOW_PROJECT" not in env, (
        "an ambient project name makes the closing doctor check report a spurious FAIL "
        "about a project the fresh config home has never heard of"
    )
    assert env["CLAUDE_CONFIG_DIR"] == str(sb.claude_root)
    assert env["CLAUDE_SETTINGS_FILE"] == str(sb.settings_file)


def test_the_provision_argv_passes_both_the_flag_and_the_sandbox_roots(drill, tmp_path):
    sb = drill.build_sandbox(tmp_path / "sandbox")
    argv = drill.provision_argv("py", sb, dry_run=True)
    assert "--dry-run" in argv
    for flag, value in (("--config-home", sb.config_home), ("--claude-root", sb.claude_root),
                        ("--settings-file", sb.settings_file), ("--suite-root", sb.clone)):
        assert argv[argv.index(flag) + 1] == str(value)
    assert "--no-autodoc" in argv, "step 6 is deliberately skipped; the record says so"


def test_the_sandbox_config_is_accepted_by_the_strict_resolver(drill, tmp_path, monkeypatch):
    """The hand-written config has to be real, or every limb after it is testing a fixture.

    This is the one place the drill's sandbox meets the actual resolver rather than a fake:
    if `build_sandbox` emits something `agentflow.config` rejects, the real drill dies at
    step 10 for a reason that has nothing to do with the install.
    """
    from agentflow.config import load_environment, load_project

    sb = drill.build_sandbox(tmp_path / "sandbox")
    # `build_sandbox` deliberately does NOT create the clone directory - `git clone` refuses a
    # non-empty destination, so the real drill lets the clone create it. `suite_root` and
    # `local_checkout` both point there, and the resolver requires both to exist, so the config
    # only becomes valid once the clone has landed. Stand in for it here.
    sb.clone.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(sb.config_home))
    env = load_environment()
    assert Path(env.data_root).expanduser() == sb.data_root
    project = load_project(drill.PROJECT)
    assert project.name == drill.PROJECT


def test_a_clone_with_no_resolvable_sha_is_refused(drill, tmp_path):
    """A record that cannot name a commit reproduces the failure that made this drill exist."""
    class _NoSha:
        """Clone and checkout succeed; `rev-parse` returns 0 with empty output.

        The empty-but-successful case, not the non-zero one: a green exit that produced no sha
        is what would actually slip an uncitable record past a naive returncode check.
        """

        def run(self, cmd, cwd=None, env=None, timeout=1800):
            return drill.Completed(0, "", "")

    with pytest.raises(drill.DrillFailure, match="no resolvable HEAD sha"):
        drill.clone_repo(_NoSha(), "somewhere", "master", tmp_path / "dest")


# --------------------------------------------------------------------------- #
# Option-lookalike refusal (security review, gh#187 round 1)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["--upload-pack=touch pwned", "-ext::sh -c whoami", "--help"])
def test_a_clone_target_that_git_would_read_as_an_option_is_refused(drill, tmp_path, bad):
    """`--clone-from` is interpolated into a git argv, and git's ext:: transport executes.

    `--` guards the clone positionals, but `checkout` cannot use it for a ref (after `--` git
    reads PATHS), so the refusal is the mitigation that covers both call sites.
    """
    class _NeverCalled:
        def run(self, cmd, cwd=None, env=None, timeout=1800):
            raise AssertionError(f"git ran with a rejected value: {cmd}")

    with pytest.raises(drill.DrillFailure, match="starts with '-'"):
        drill.clone_repo(_NeverCalled(), bad, "master", tmp_path / "dest")


def test_a_ref_that_git_would_read_as_an_option_is_refused(drill, tmp_path):
    class _NeverCalled:
        def run(self, cmd, cwd=None, env=None, timeout=1800):
            raise AssertionError(f"git ran with a rejected ref: {cmd}")

    with pytest.raises(drill.DrillFailure, match="starts with '-'"):
        drill.clone_repo(_NeverCalled(), "https://example.invalid/r.git", "--help",
                         tmp_path / "dest")


def test_a_legitimate_ref_is_NOT_refused(drill, tmp_path):
    """The control. A refusal that also refuses ordinary input is not a guard, it is an outage."""
    seen: list[list[str]] = []

    class _Ok:
        def run(self, cmd, cwd=None, env=None, timeout=1800):
            seen.append(list(cmd))
            if "rev-parse" in cmd:
                return drill.Completed(0, "a" * 40, "a" * 40)
            return drill.Completed(0, "", "")

    sha, tree = drill.clone_repo(_Ok(), "https://example.invalid/r.git", "master",
                                 tmp_path / "dest")
    assert sha == "a" * 40
    assert tree == "a" * 40
    clone = next(c for c in seen if "clone" in c)
    assert "--" in clone and clone.index("--") < clone.index("https://example.invalid/r.git")
    checkout = next(c for c in seen if "checkout" in c)
    # The ref must precede the separator: `checkout -- master` would look for a FILE.
    assert checkout.index("master") < checkout.index("--")


# --------------------------------------------------------------------------- #
# The record is published, so it must not carry the operator's home path
# --------------------------------------------------------------------------- #
def test_the_record_does_not_embed_an_absolute_interpreter_path(rig):
    """The README tells you to paste this record into a pull request.

    On Windows an absolute interpreter path contains the operator's username, and the tree's
    sanitize gate exists to keep exactly that out of published text. The file already states
    this rule for BARE_PYTHON_CANDIDATES; this asserts it where the value actually escapes.
    """
    record = rig("published").run()
    home = str(Path.home())
    assert home not in record, "the record embeds the operator's home directory"
    assert home.replace("\\", "/") not in record
    assert "/home/" not in record
    assert "<sandbox>/clone/.venv" in record, "the suite interpreter line lost its redaction"


# --------------------------------------------------------------------------- #
# The laundering case the first version of the discriminator let through
# --------------------------------------------------------------------------- #
def test_an_unrelated_ModuleNotFoundError_that_also_mentions_yaml_is_NOT_called_a_regression(
        drill, rig):
    """The case that matters, and the one a two-substring test could not see.

    The limb used to require "ModuleNotFoundError" in the transcript AND "yaml" in the
    transcript, independently. Step 4 shells out to `pip install pyyaml>=6.0` and provision
    echoes the failing command line verbatim, so ANY step-4 failure supplies the "yaml" half
    for free. Under the witness that produced a full GREEN from a failure that never reached
    step 5. Under the inverted limb the same looseness is worse, not better: it would file a
    distutils-less sdist build as gh#186 coming back, and the next person would go read a
    handover that works.
    """
    rig("l1").run()
    broken = rig("l2")
    broken.runner.outcomes["provision-bare"] = broken.drill.Completed(
        1,
        "  4. Python venv + PyYAML (only pipeline dep) ...\n",
        "RuntimeError: command failed: pip install pyyaml>=6.0\n"
        "ModuleNotFoundError: No module named 'distutils'\n",
    )
    with pytest.raises(drill.DrillFailure, match="NOT with the") as caught:
        broken.run()
    assert "REGRESSION" not in str(caught.value)


def test_the_limb_records_a_PASS_for_an_ordinary_successful_handover(rig):
    """The control. A limb that reds on the real fixed behaviour is not a guard, it is noise."""
    record = rig("l3").run()
    assert "PASS - PyYAML-less run completes via the venv handover" in record.replace("### ", "")


# --------------------------------------------------------------------------- #
# The interpreter-version gate
# --------------------------------------------------------------------------- #
def test_a_too_old_bare_interpreter_SKIPS_limb_2_rather_than_failing_the_drill(rig):
    """A box whose only PyYAML-less python is 3.10 is a normal box, not a broken one.

    Such an interpreter dies in provision's step-1 preflight ("Python 3.11+ required") long
    before the step-4 handover, so an ungated probe made limb 2 hard-FAIL and write no record
    at all - aborting five perfectly runnable limbs over a machine that is fine.
    """
    broken = rig("old")
    broken.runner.outcomes["version-probe"] = broken.drill.Completed(3, "", "")
    record = broken.run(bare_python=None)
    assert "SKIP" in record
    assert "older than Python 3.11" in record


# --------------------------------------------------------------------------- #
# The citable identity: tree id, OS distribution, interpreter versions
# --------------------------------------------------------------------------- #
class _GitFake:
    """Clone and checkout succeed; each `rev-parse` answers from a per-argument table.

    Keyed on the rev-parse ARGUMENT rather than call order, so the tree lookup can be broken
    alone while the sha lookup keeps answering - the only way to prove the tree has its own gate.
    """

    def __init__(self, drill, answers: dict):
        self.drill = drill
        self.answers = answers

    def run(self, cmd, cwd=None, env=None, timeout=1800):
        if "rev-parse" in cmd:
            return self.answers[cmd[-1]]
        return self.drill.Completed(0, "", "")


@pytest.mark.parametrize("broken_tree", ["empty", "nonzero"])
def test_a_clone_with_no_resolvable_TREE_is_refused(drill, tmp_path, broken_tree):
    """The tree id is what a single-commit cut carries, so the record must never lack one.

    The sha still resolves in the mutation: a record naming only the sha would look complete
    and be uncitable against the published repository, which contains no such commit.
    """
    C = drill.Completed
    control = _GitFake(drill, {"HEAD": C(0, FAKE_SHA + "\n"), "HEAD^{tree}": C(0, FAKE_TREE + "\n")})
    assert drill.clone_repo(control, "somewhere", "master", tmp_path / "c") == (FAKE_SHA, FAKE_TREE)

    bad = C(0, "", "") if broken_tree == "empty" else C(128, "", "fatal: bad revision")
    mutant = _GitFake(drill, {"HEAD": C(0, FAKE_SHA + "\n"), "HEAD^{tree}": bad})
    with pytest.raises(drill.DrillFailure, match=r"no resolvable HEAD\^\{tree\} id"):
        drill.clone_repo(mutant, "somewhere", "master", tmp_path / "m")


def test_the_record_carries_the_resolved_TREE_beside_the_sha(rig):
    """`Resolved tree:` is the line the re-verify record is compared against the cut by."""
    record = rig("tree").run()
    assert f"- **Resolved tree:** `{FAKE_TREE}`" in record
    assert f"- **Resolved sha:** `{FAKE_SHA}`" in record
    sha_line = next(ln for ln in record.splitlines() if ln.startswith("- **Resolved sha:**"))
    assert "single-commit cut does not carry it" in sha_line, (
        "the sha line must say it is not the citable identity, or a reader cites the commit"
    )


def test_the_record_carries_the_TREE_that_clone_repo_resolved(rig):
    """Every other rig run injects the tree, so this is the only one through the real path.

    A run_drill that dropped clone_repo's tree would print `Resolved tree: None` on the very
    line the re-verify compares against the cut, while every injected-seam test stayed green.
    """
    r = rig("clone-path")
    record = r.run(clone=True)
    assert "git-rev-parse-tree" in r.runner.calls, "the drill never asked git for the tree"
    assert f"- **Resolved tree:** `{FAKE_TREE}`" in record
    assert f"- **Resolved sha:** `{FAKE_SHA}`" in record


def test_injecting_a_sha_without_a_tree_is_refused(drill, rig):
    """Half a seam would send the drill off to clone for the missing half."""
    r = rig("half-seam")
    with pytest.raises(ValueError, match="together"):
        drill.run_drill(r.out / "record", runner=r.runner, sandbox=r.sandbox, sha=FAKE_SHA,
                        bare_python=FAKE_BARE_PYTHON, read_os_release=r.os_release)


def test_the_OS_line_names_the_distribution_from_PRETTY_NAME(drill):
    """A Linux kernel build string does not say "Ubuntu 24.04"; os-release does."""
    label = drill.describe_os(lambda: {"PRETTY_NAME": FAKE_PRETTY_NAME, "NAME": "Ubuntu"})
    assert label.startswith(FAKE_PRETTY_NAME)
    # Control: a different distribution must produce a different label, so the pass above
    # cannot come from a constant that happens to contain the fixture string.
    other = drill.describe_os(lambda: {"PRETTY_NAME": "Debian GNU/Linux 12 (bookworm)"})
    assert other.startswith("Debian GNU/Linux 12") and FAKE_PRETTY_NAME not in other


def test_the_OS_line_falls_back_when_there_is_no_os_release(drill):
    """Windows and macOS have no os-release; the read raises OSError and the label survives."""
    import platform

    def _no_os_release():
        raise OSError("no os-release file")

    label = drill.describe_os(_no_os_release)
    assert label == f"{platform.system()} {platform.version()} ({platform.machine()})"


def test_the_record_OS_line_carries_PRETTY_NAME_end_to_end(rig):
    """The seam is wired through run_drill, not only through the helper."""
    record = rig("os").run()
    os_line = next(ln for ln in record.splitlines() if ln.startswith("- **OS:**"))
    assert FAKE_PRETTY_NAME in os_line


def test_the_record_names_BOTH_interpreter_versions(rig):
    """3.12 is the point of a Linux run, and a basename alone does not say which Python ran."""
    record = rig("pyver").run()
    bare = next(ln for ln in record.splitlines() if ln.startswith("- **Bare interpreter"))
    suite = next(ln for ln in record.splitlines() if ln.startswith("- **Suite interpreter"))
    assert f"(Python {FAKE_BARE_VERSION})" in bare
    assert f"Python {FAKE_VENV_VERSION}" in suite
    assert FAKE_VENV_VERSION not in bare and FAKE_BARE_VERSION not in suite, (
        "each line must carry its OWN interpreter's version"
    )


@pytest.mark.parametrize("probe", ["pyver-bare", "pyver-venv"])
def test_an_unreadable_interpreter_version_FAILS_the_drill(drill, rig, probe):
    """A record that cannot say which Python ran is refused, like one that cannot name a tree."""
    rig(f"c-{probe}").run()                           # control: green unmutated
    broken = rig(f"m-{probe}")
    broken.runner.outcomes[probe] = drill.Completed(0, "not a version\n", "")
    with pytest.raises(drill.DrillFailure, match="could not read the version"):
        broken.run()
