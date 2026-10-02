"""The plan-lint hook: one copy, one deploy path, and a scope that covers both spellings.

WHY THIS FILE EXISTS (gh#110). `plan-lint.sh` existed twice and was owned nowhere.

* The repo copy, `skills/_shared/scripts/plan-lint.sh`, had NO deploy path at all —
  `SKILLS_NON_DEPLOY` excludes `_shared`, so `_deploy_sources()` never emitted it and
  nothing ever placed it on a host.
* The copy that actually ran was a July file sitting unmanaged in the Claude config dir. It
  scoped only the legacy `*/docs/superpowers/plans/*.md` spelling and imported
  `lib.plan_parser` from a skill directory that has since been retired, so every longform
  plan write hit `ModuleNotFoundError` and the hook exited 2 blaming the PLAN for its own
  broken dependency. A hook that reports a failure caused by a dependency IT lost is worse
  than no hook.

WHAT EXIT 2 DOES, AND DOES NOT DO. This is a PostToolUse hook: it fires AFTER the tool has
run, so exit 2 neither prevents nor undoes the write. It fails the file to the writing
session — a report, not a gate; only PreToolUse can block. Every "rejected" below means
exactly that and nothing stronger. It also sets the cost of a false positive: a wrong
verdict is a standing false accusation against a file already on disk, so a scope that is
too WIDE is as much a defect as one that is too narrow, which is what limb 2 is about.

So there are three properties here, and each one is a separate limb below:

1. `_deploy_sources()` emits `scripts/plan-lint.sh`. The KEY is what is asserted, because
   the key IS the deployed path — `~/.claude/scripts/plan-lint.sh` is the one stable,
   managed location, so the host copy is always the tracked one. Provision registers no hook
   for it; an operator who adds one points it at this path. A test that only checked "the
   script appears somewhere in the pairs" would pass with the file deployed to a path that
   moves between releases.

2. The shipped script's scope matches BOTH spellings. The legacy path is a junction onto
   the same files, so writes still arrive spelled that way; a second case arm can only add
   gating. This is driven through REAL `bash` with REAL JSON on stdin rather than by
   re-implementing the case statement in Python — a Python re-implementation asserts a copy
   of the logic and stays green while the shipped logic rots, which is the exact shape of
   the bug being closed.

3. The script does not reference `longform-orchestrator` or `lib.plan_parser`. That is the
   regression guard that matters: it is the dead dependency that made the live copy fail
   closed, and it is invisible to any test that only checks exit codes on well-formed input.

SCOPE IS OBSERVED THROUGH THE VALIDATOR'S VERDICT — say so, because the obvious alternative
does not work here. These tests hold one malformed plan body fixed, move only the PATH, and
read the exit code and the stderr banner. That means they depend on `agentflow` being
importable inside the shelled `bash`: the exit 2 they assert is the validator's, not the case
statement's.

The tempting alternative — observing scope through the CRLF-normalisation step, which runs
for an in-scope path and not an out-of-scope one — is UNAVAILABLE on this platform and would
be silently vacuous if used. Under Git Bash the MSYS `grep` opens files in text mode and
strips CR before matching, so the guard's own `grep -q $'\r'` is never true on Windows and
the rewrite never runs; both scope cases would look identical and every assertion would pass
for the wrong reason. That platform gap is real, is NOT fixed here, and is recorded in the
shipped script's header for the next reader; it does not affect Linux or macOS, where the
rewrite does run.

Every shelling test runs with this suite's own interpreter first on PATH and skips outright
if bash can reach none — a shell with no `python` makes the payload parse yield an empty
path, which looks exactly like a deliberate out-of-scope exit and would have reported the
scope arm as broken.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> _shared/ -> skills/ -> suite root. Derived, never hard-coded: the suite is
# checked out under a different path on every machine and in every worktree.
SUITE_ROOT = Path(__file__).resolve().parents[3]
PROVISION_PY = SUITE_ROOT / "install" / "provision.py"
SCRIPT = SUITE_ROOT / "skills" / "_shared" / "scripts" / "plan-lint.sh"

# The claude_root-relative key. Spelled out ONCE, here, because it is the assertion: this
# string is the deployed path, `~/.claude/scripts/plan-lint.sh`, the stable managed location.
DEPLOY_KEY = "scripts/plan-lint.sh"


def _provision():
    """`install/provision.py` imported as a module, by location.

    It is not on the import path (it is a script, not part of the `agentflow` package) and
    it is stdlib-only with a `__main__` guard, so loading it executes nothing.
    """
    spec = importlib.util.spec_from_file_location("_e0110_provision", PROVISION_PY)
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: `@dataclass` resolves its string annotations through
    # `sys.modules[cls.__module__]`, and a module absent from that table makes the
    # decorator raise on a class it would otherwise build fine.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bash() -> str:
    exe = shutil.which("bash")
    if exe is None:
        pytest.skip("bash unavailable — the shipped hook can only be driven through a shell")
    return exe


def _hook_env() -> dict:
    """The environment the hook runs under, with THIS suite's interpreter as `python`.

    The script shells out to `python` twice — once to pull `file_path` out of the payload,
    once to validate — so a shell whose PATH has no `python` makes every run look like a
    silent out-of-scope exit. Prepending `sys.executable`'s directory removes that
    confound and matches what a provisioned session has.
    """
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def _require_shell_python() -> None:
    """Skip rather than mis-report when bash cannot reach an interpreter at all."""
    probe = subprocess.run(
        [_bash(), "-c", "python -c 'print(1)'"],
        capture_output=True, text=True, env=_hook_env(),
    )
    if probe.returncode != 0:
        pytest.skip(f"no `python` reachable from bash: {probe.stderr.strip()!r}")


def _run_hook(file_path: Path) -> subprocess.CompletedProcess:
    """Feed the REAL script the REAL hook payload for `file_path`."""
    _require_shell_python()
    payload = json.dumps({"tool_input": {"file_path": str(file_path)}})
    return subprocess.run(
        [_bash(), str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=_hook_env(),
    )


# An epic plan the parser cannot accept: `tasks:` is present (so the hook does not exit
# early on "not an epic plan") and the YAML is unterminated (so any parser rejects it,
# without this fixture having to track the plan schema).
MALFORMED_PLAN = "---\ntasks: [unclosed\n---\n\n# body\n"


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MALFORMED_PLAN, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. The deploy path.
# --------------------------------------------------------------------------- #
def test_deploy_sources_ships_the_plan_lint_script_at_the_stable_key():
    """The installer emits `scripts/plan-lint.sh` -> `skills/_shared/scripts/plan-lint.sh`.

    Asserting the KEY, not merely the presence of the source: the key is the claude_root-
    relative destination, and `~/.claude/scripts/plan-lint.sh` is the one stable path the
    script is managed at. Deploy it under any other key and whatever unmanaged file already
    sits at that path stays there, untracked.
    """
    pairs = _provision()._deploy_sources(SUITE_ROOT)
    by_key = dict(pairs)

    assert DEPLOY_KEY in by_key, (
        f"install/provision.py::_deploy_sources emits no '{DEPLOY_KEY}' entry, so the repo's "
        f"plan-lint.sh has no deploy path and the host copy stays unmanaged (gh#110). "
        f"Keys emitted: {sorted(by_key)[:8]}..."
    )
    assert by_key[DEPLOY_KEY] == SCRIPT, (
        f"'{DEPLOY_KEY}' is deployed from {by_key[DEPLOY_KEY]}, not from the repo copy at "
        f"{SCRIPT} — the point of the entry is that the shipped file is the tracked one."
    )


def _tmp_suite(root: Path) -> Path:
    """A minimal suite checkout: one skill, one command, and the hook script."""
    (root / "skills" / "demo-skill").mkdir(parents=True)
    (root / "skills" / "demo-skill" / "SKILL.md").write_text("demo\n", encoding="utf-8")
    (root / "commands").mkdir(parents=True)
    (root / "commands" / "demo.md").write_text("demo\n", encoding="utf-8")
    (root / "skills" / "_shared" / "scripts").mkdir(parents=True)
    (root / "skills" / "_shared" / "scripts" / "plan-lint.sh").write_text(
        SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return root


def test_a_real_deploy_places_the_script_at_the_nested_key(tmp_path: Path):
    """A real `_do_deploy_skills` run lands the script at `<claude_root>/scripts/plan-lint.sh`.

    Two things at once, and both are the epic. The entry has to be REACHED by the deploy
    loop (limb 1 only proves it is emitted), and the loop has to create the parent of a
    NESTED key — it used to create `skills/` and `commands/` by name, so a third root landed
    in a directory nobody had made. The fix is per-key parents rather than a third literal;
    a literal is what made the last one a special case.

    Runs against a synthetic suite and a `tmp_path` claude_root: milliseconds, and it never
    reads or writes the machine's real config dir.
    """
    provision = _provision()
    suite = _tmp_suite(tmp_path / "suite")
    claude_root = tmp_path / "claude"
    claude_root.mkdir()

    provision._do_deploy_skills(
        provision.Options(
            dry_run=False,
            suite_root=suite,
            config_home=tmp_path / "config",
            settings_file=tmp_path / "settings.json",
            autodoc=False,
            autodoc_reason="not under test",
            claude_root=claude_root,
        )
    )

    deployed = claude_root / DEPLOY_KEY
    assert deployed.exists(), (
        f"provision finished without placing {DEPLOY_KEY} under {claude_root}. The copy at "
        f"that stable path is then whatever unmanaged file already sits there (gh#110). "
        f"Deployed: {sorted(p.name for p in claude_root.iterdir())}"
    )
    manifest = json.loads((claude_root / provision.DEPLOY_MANIFEST_NAME).read_text("utf-8"))
    assert DEPLOY_KEY in manifest["entries"], (
        "the script was placed but not recorded in the deploy manifest, so the next run "
        "cannot tell its own copy from a foreign one."
    )


# --------------------------------------------------------------------------- #
# 2. The scope: both spellings, and nothing else.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rel",
    [
        "projects/demo/design/plans/2026-01-01-a-plan.md",          # canonical
        "docs/superpowers/plans/2026-01-01-a-plan.md",              # legacy junction spelling
    ],
    ids=["canonical-design-plans", "legacy-superpowers-plans"],
)
def test_the_hook_is_in_scope_for_both_plan_path_spellings(tmp_path: Path, rel: str):
    """In scope => a malformed plan is REJECTED (exit 2) under either spelling.

    The legacy arm is not cosmetic: `docs/superpowers/plans` is a junction onto the same
    files, so a write can arrive under either spelling and only the matched one is gated.
    Drop the legacy arm and this case's rc goes to 0 — the hook waves the bad plan through
    and the orchestrator finds out on its next wake.
    """
    plan = tmp_path / rel
    _write_plan(plan)

    result = _run_hook(plan)

    assert result.returncode == 2, (
        f"a malformed epic plan written to {rel} was NOT rejected (rc={result.returncode}), "
        f"so the scope case statement did not match this spelling. Both spellings reach the "
        f"same files; an unmatched one is an ungated plan write (gh#110). "
        f"stderr={result.stderr!r}"
    )
    assert "failed parse_plan validation" in result.stderr, (
        f"rejected, but not by the plan validator: stderr={result.stderr!r}"
    )


def test_the_hook_ignores_a_path_that_is_not_a_plan(tmp_path: Path):
    """Out of scope => exit 0 on the SAME bytes the in-scope cases reject.

    Identical content, different path, opposite outcome: that is what makes the pair above a
    statement about the case statement rather than about the validator. It is also the guard
    on the widening — a second arm must add one spelling, not open the hook onto every
    markdown file a session writes.
    """
    other = tmp_path / "src" / "notes" / "readme.md"
    _write_plan(other)

    result = _run_hook(other)

    assert result.returncode == 0, (
        f"a file outside every plans directory was linted — the scope case now matches paths "
        f"it has no business touching. rc={result.returncode} stderr={result.stderr!r}"
    )
    assert result.stderr == "", f"unexpected output for an out-of-scope path: {result.stderr!r}"


# --------------------------------------------------------------------------- #
# 3. The dead dependency that made the live copy fail closed.
# --------------------------------------------------------------------------- #
def test_the_script_does_not_import_the_retired_longform_orchestrator():
    """No `longform-orchestrator` sys.path insert, no `lib.plan_parser` import.

    This is the regression guard that matters. The host copy did exactly this, against a
    skill directory that no longer exists, so every plan write exited 2 with
    `ModuleNotFoundError: No module named 'lib'` printed as if the plan were at fault. The
    import that belongs here is `agentflow.plan_parser`, which resolves from the pip
    package the installer already places.
    """
    text = SCRIPT.read_text(encoding="utf-8")

    assert "longform-orchestrator" not in text, (
        "plan-lint.sh references the retired longform-orchestrator skill directory. That "
        "directory does not exist; the hook then fails closed on EVERY plan write and "
        "reports the failure as a plan validation error (gh#110)."
    )
    assert "lib.plan_parser" not in text and "lib/plan_parser" not in text, (
        "plan-lint.sh reaches for `lib.plan_parser`, the retired skill-local module. The "
        "shipped dependency is `agentflow.plan_parser`."
    )
    assert "from agentflow.plan_parser import parse_plan" in text, (
        "plan-lint.sh no longer imports the validator it exists to run."
    )


def test_the_rejection_comes_from_the_validator_and_not_from_a_missing_module(tmp_path: Path):
    """Exit 2, and the reason is the PLAN — not an import the hook can no longer resolve.

    The runtime half of the guard above, and the one the exit codes alone cannot give you:
    a broken import is caught by the same `except` as a bad plan and produces the same
    exit 2 with the same "failed parse_plan validation" banner. That is precisely how the
    host copy stayed broken for two months while looking like a working gate. So the
    assertion is on what is ABSENT from stderr.
    """
    plan = tmp_path / "projects" / "demo" / "design" / "plans" / "broken.md"
    _write_plan(plan)

    result = _run_hook(plan)

    assert result.returncode == 2, (
        f"rc={result.returncode} stderr={result.stderr!r}"
    )
    assert "ModuleNotFoundError" not in result.stderr, (
        "the hook exited 2 because of a MISSING MODULE, not because the plan is bad — the "
        f"live host copy's exact failure mode (gh#110). stderr={result.stderr!r}"
    )
    assert "ImportError" not in result.stderr, (
        f"the validator import did not resolve: stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# 4. The exclusion: orchestrator status files share the directory AND the key.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rel",
    [
        pytest.param("projects/demo/design/plans/x.status.md", id="canonical-dot-status"),
        pytest.param("projects/demo/design/plans/status-x.md", id="canonical-status-prefix"),
        pytest.param("docs/superpowers/plans/x.status.md", id="legacy-dot-status"),
        pytest.param("docs/superpowers/plans/status-x.md", id="legacy-status-prefix"),
    ],
)
def test_the_hook_ignores_orchestrator_status_files(tmp_path: Path, rel: str):
    """A status file exits 0 even though its bytes fail the validator.

    The orchestrator writes its run state INTO the plans directory, and those files carry a
    `tasks:` key of their own — so neither the directory nor the frontmatter check can tell
    them from plans, and the filename is the only discriminator the orchestrator controls.

    Measured on the live corpus when this arm was added: of 165 files carrying `tasks:` under
    the two plans trees, 92 were plans and 73 were status files, and all 73 failed the
    validator. Without this exclusion the deploy this epic ships would take a hook that
    currently fires on nothing and make it fail 73 files it was never meant to read.

    The assertion is sharp because the BYTES are held fixed: `MALFORMED_PLAN` exits 2 at a
    plan filename (limb 2) and must exit 0 here. Only the name moved, so a pass cannot be
    explained by the fixture being acceptable to the parser.
    """
    status_file = tmp_path / rel
    _write_plan(status_file)

    result = _run_hook(status_file)

    assert result.returncode == 0, (
        f"the hook read an orchestrator STATUS file as a plan and failed it "
        f"(rc={result.returncode}). These are not plans; failing them is a standing false "
        f"accusation against the orchestrator's own output, which is the shape gh#110 "
        f"exists to close. stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# 5. A refusal must leave the host exactly as it found it.
# --------------------------------------------------------------------------- #
def _tree_snapshot(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def test_a_foreign_collision_refuses_without_deploying_anything(tmp_path: Path):
    """A foreign file at the NESTED key aborts the run and writes nothing at all.

    This is the path the live machine actually hits: `~/.claude/scripts/plan-lint.sh` was
    hand-placed years before it had a deploy entry, so it carries no manifest record and no
    ownership marker. Refusing it is correct and is not what is under test.

    What is under test is WHEN it refuses. The hook script's key sorts after every skill and
    command, so a refusal raised from inside the deploy loop landed only after all the other
    entries had been written — and before the manifest was rewritten, leaving a host tree
    that no longer matched its own record. Checking every entry first makes the refusal
    total: the caller either gets a complete deploy or an untouched directory.
    """
    provision = _provision()
    suite = _tmp_suite(tmp_path / "suite")
    claude_root = tmp_path / "claude"
    (claude_root / "scripts").mkdir(parents=True)
    (claude_root / "scripts" / "plan-lint.sh").write_text(
        "#!/usr/bin/env bash\n# hand-placed, not ours\n", encoding="utf-8"
    )
    before = _tree_snapshot(claude_root)

    with pytest.raises(RuntimeError, match="deploy REFUSED"):
        provision._do_deploy_skills(
            provision.Options(
                dry_run=False,
                suite_root=suite,
                config_home=tmp_path / "config",
                settings_file=tmp_path / "settings.json",
                autodoc=False,
                autodoc_reason="not under test",
                claude_root=claude_root,
            )
        )

    assert _tree_snapshot(claude_root) == before, (
        "the refusal fired only after other entries had been deployed, so the host tree was "
        "left modified while the manifest was never rewritten — a tree that disagrees with "
        "its own record, produced by the guard working. Refuse before writing anything."
    )
    assert not (claude_root / provision.DEPLOY_MANIFEST_NAME).exists(), (
        "a refused run wrote a deploy manifest; it must describe a deploy that happened."
    )
