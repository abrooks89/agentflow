#!/usr/bin/env python
"""The fresh-clone install claim, run against an actual fresh clone rather than asserted.

WHY THIS EXISTS. `harness/README.md` publishes a table of what runs "out of the box" on a
fresh clone, and `docs/INSTALL.md` publishes a clone-then-provision recipe. The only recorded
evidence for either was captured against beta-test HEAD `3c959cd` - a commit that no longer
exists, because the working repository's master was re-cut on 2026-08-28 with a new root.
So the repository's most public promise ("clone this, run the installer, it works") was
backed by a citation nobody can resolve. That is worse than no evidence: it reads as verified.

This drill clones the repo into a throwaway tree, installs into a throwaway config home, data
root and Claude root, and walks the whole path a new contributor walks:

  1. `install/provision.py --dry-run` exits 0 and mutates nothing - checked by proving the
     sandbox data root is still empty afterwards, not by trusting the banner;
  2. the REAL run on an interpreter that has no PyYAML, which is what a new contributor's
     shell usually is. This limb is the REGRESSION GUARD for BUG_ISSUE below: it requires
     that run to complete and exit 0 by handing itself over to the venv step 4 builds, and
     it requires the handover line in the transcript so the green cannot come from an
     interpreter that quietly had PyYAML;
  3. the real run on the interpreter step 4 builds (`<clone>/.venv`), which must reach the
     last step and exit 0;
  4. the artifacts of steps 7, 8, 9 and 11 - deploy manifest, deployed skills and commands,
     the PreToolUse and SessionStart hooks in the settings file, the per-project data subtree,
     the seeded registry - each looked for on disk. Steps 1-6, 10 and 12 leave nothing to
     inspect, which the limb says outright rather than implying twelve-step coverage;
  5. `harness/check.py --self-test`, from the clone, on the venv interpreter;
  6. `harness/check.py --only C,F,G` after copying `harness.config.example.json` verbatim,
     placeholders unreplaced, which is exactly what the README tells a reader to do;
  7. a record naming the tree id, the sha, the OS, both interpreters with their versions and
     the per-limb outcome, so the next audit cites a run instead of re-deriving one. The TREE
     id is the citable one: a single-commit share cut carries the same files under a new
     commit, so the sha names a commit the cut does not contain while the tree id survives.

WHAT IT DOES NOT PROVE, said here rather than left for an auditor to discover:

  * **It proves one OS, the one it ran on.** CI covers Windows and Linux for the test suite,
    but this drill's result is a single-platform result and the record says which.
  * **It does not run the record-dependent scenario groups.** Groups A, B, D and E2 read
    `records/`, which is deliberately never committed (`harness/run-scenario.md`), so a fresh
    clone fails them by design. The drill runs the reconstructable groups only and records
    the full-suite numbers as context, not as a gate.
  * **It does not exercise the human interview in `install/SKILL.md`.** The drill WRITES a
    config by hand and hands it to `provision.py`. Everything the interview decides - the
    questions, the secret one-way write, the judgement about what a machine actually has -
    is untested here, and a green run says nothing about it.
  * **The autodoc Node runtime step is disabled** (`--no-autodoc`), so step 6 is skipped
    rather than exercised. That keeps the drill runnable without a Node toolchain; it also
    means npm-side breakage is invisible to it.

THE SECOND LIMB WAS A BUG WITNESS UNTIL BUG_ISSUE WAS FIXED, AND IS NOW ITS REGRESSION
GUARD - inverted rather than deleted, because a fix nothing checks is a claim. A green there
means the documented command works on a PyYAML-less interpreter; a death carrying that bug's
signature is reported as a REGRESSION by name, and any OTHER failure is refused rather than
filed under it. That issue's second finding, split out as gh#322, is fixed for the case it
recorded: step 2 refuses to pre-create a scraped `data_root` or `worktree_root` that is
relative, or whose only existing ancestor is the drive root, and leaves the loud failure to
the step-10 resolver. It still pre-creates a well-formed path under an existing directory,
by design, because a fresh data root does not exist yet. This drill writes a well-formed
config and never exercises a malformed one; the gh#322 tests in
`install/test_provision_fresh_machine.py` cover that offline.

Usage:
  python harness/fresh-clone-install-drill/fresh_clone_install_drill.py \
      [--out <dir>] [--clone-from <path-or-url>] [--ref <ref>] [--bare-python <path>]

Exits 0 iff every limb passed or skipped with a stated reason. Writes
`<out>/fresh-clone-install-drill.md`; `--out` defaults to a temp directory, so a bare run
never touches a live config home, data root or Claude root.

WHAT `--clone-from` ACTUALLY DOES, said plainly because the flag looks inert:
this drill clones that target and then EXECUTES code from it - `install/provision.py` and
`harness/check.py` - on this machine, in this user account, outside any container. The
sandboxing here is about not disturbing your LIVE agentflow install; it is not a security
boundary against hostile code. The default is this project's own repository. Point it
somewhere else only if you would have run that repository's installer by hand anyway.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Optional

NL = chr(10)

DEFAULT_CLONE_FROM = "https://github.com/abrooks89/agentflow.git"
DEFAULT_REF = "master"
RECORD_NAME = "fresh-clone-install-drill.md"

# The defect limb 2 used to witness and now guards against. Written once so the record, the
# PASS line and the regression wording can never drift apart.
BUG_ISSUE = "gh#186"

# The project name the sandbox config declares. Short, generic, and not a real product.
PROJECT = "drill"

# A limb either returns PASS, returns SKIP with a stated reason, or RAISES DrillFailure.
# There is deliberately no "FAIL" status to return: a failing limb must stop the drill, not
# be collected into a summary line that a reader can skim past.
PASS, SKIP = "PASS", "SKIP"

# Interpreter names probed for the PyYAML-less limb, best first. Names only - never an
# absolute path, which would be machine-specific and would also fail the tree's sanitize gate.
BARE_PYTHON_CANDIDATES = ("python3.13", "python3.12", "python3.11", "python3", "python", "py")

# The discriminator that separates "the old bug is back" from "something else broke", as ONE
# phrase rather than two independent substring tests. Why this is not `"ModuleNotFoundError"
# in out and "yaml" in out`: step 4 shells out to `pip install pyyaml>=6.0`, and _run_cmd
# echoes the command line verbatim into the error it raises, so ANY step-4 failure puts "yaml"
# into the transcript. That reduces the test to `"ModuleNotFoundError" in out`, which an
# unrelated import failure (a distutils-less sdist build, say) satisfies at step 4 - never
# reaching the step-5 code path gh#186 is about. Under the old witness that produced a green
# for a bug it had not observed; under the inverted limb it would file an unrelated breakage
# as a gh#186 regression and send the next reader to the wrong code. Match the message Python
# actually emits, in both directions.
_GH186_SIGNATURE = re.compile(r"ModuleNotFoundError: No module named ['\"]yaml['\"]")

# The line `install/provision.py` prints at the END of step 4 when the interpreter running it
# cannot import PyYAML: `  -- handing off to the suite venv (<py>) for steps 5+`. Matched on
# the phrase alone because the interpreter path and the step number are run-specific.
#
# THIS IS WHAT KEEPS THE INVERTED LIMB FROM BEING VACUOUS. Requiring only "exit 0 and
# 11. done." passes just as happily on an interpreter that turned out to have PyYAML all
# along - the same blind spot the old witness had, in mirror image, and one that would go
# unnoticed precisely because everything is green. A run that did not hand over did not
# exercise the fix. `test_fresh_clone_install_drill.py` renders this f-string out of
# provision.py's own source and requires this pattern to match it, so rewording the handover
# there cannot quietly leave this limb passing on a transcript it no longer recognizes.
_HANDOVER_SIGNATURE = re.compile(r"handing off to the suite venv")

# The last UNCONDITIONAL step's completion line, and the only in-band evidence a run reached the
# end rather than returning 0 from somewhere in the middle. Shared by limbs 2 and 3 so the two
# cannot drift into checking different things about the same claim.
#
# WHY 11 AND NOT THE HIGHEST STEP NUMBER. Step 12 (the missed-wake watchdog registration, E-0768)
# is opt-in (`--wake-watch`, off by default since gh#544) and platform-gated: a default run prints
# `12. ... - SKIPPED` on every platform, and only an opted-in Windows run prints `12. done.`.
# Requiring the highest number would therefore fail this drill over an install that did exactly
# the right thing. Step 11 is the last step every run completes, so it is the last line whose
# absence means something went wrong. If a future step lands after 11 and is unconditional, this
# constant moves to it.
_INSTALL_COMPLETE = "11. done."

# The minimum the installer itself demands (install/provision.py step 1 preflight). A probed
# interpreter below this dies at step 1 with "Python 3.11+ required" - a clean RuntimeError,
# no handover and no completion, so limb 2 would abort the whole drill on a machine where
# nothing is wrong. Gate the probe instead, and SKIP when only an old one exists.
BARE_PYTHON_MIN = (3, 11)

# The twelve-step install's observable outputs, as (label, relative-path-from-a-root) pairs
# resolved in limb 4. Kept as data so a missing one names itself in the failure.
# Kept byte-identical to install/provision.py's DATA_SUBTREE_DIRS. This is a
# THIRD transcription of that tuple and it is presence-only, so a drift here
# stays green while silently verifying fewer directories than a fresh clone
# actually produces. test_fresh_clone_install_drill.py asserts the two are equal
# so the drift cannot recur silently.
_SUBTREE_DIRS = (
    "design/specs",
    "design/plans",
    "design/use-cases",
    "design/suggestions",
    "status",
    "wiki/pages",
    "wiki/pages/done",
    "wiki/pages/archived",
)

_SUMMARY_RE = re.compile(r"#\s*summary:\s*(\d+)\s+PASS\s+(\d+)\s+FAIL\s+(\d+)\s+SKIP")


class DrillFailure(RuntimeError):
    """A limb did not hold. Carries the sentence a reader needs, not a traceback."""


@dataclass
class Completed:
    """What a Runner returns. A three-field stand-in so a fake needs no subprocess."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


class Runner:
    """The one seam every external command goes through.

    Everything expensive or networked in this drill - the clone, four provision runs, two
    checker runs - is a `Runner.run`. The CI companion injects a fake so it can break one
    limb at a time offline, which is the only way to prove a limb can fail at all.
    """

    def run(self, cmd: list[str], cwd: Optional[Path] = None,
            env: Optional[dict] = None, timeout: int = 1800) -> Completed:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                cmd, cwd=str(cwd) if cwd else None, env=env, timeout=timeout,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            # Converted rather than propagated: a timed-out clone on a slow link is an
            # ordinary outcome, and letting TimeoutExpired escape exits through a traceback
            # instead of the drill's own error path, which is where the sandbox location and
            # the "what to do next" sentence live.
            raise DrillFailure(
                f"`{' '.join(cmd[:3])} ...` exceeded {timeout}s and was killed. A slow network "
                "or a wedged child; re-run, or pass a local path to --clone-from."
            ) from exc
        return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")


@dataclass
class LimbResult:
    """One limb's verdict, plus the sentence stating what it does NOT cover.

    The caveat is not decoration. Every PASS in this drill is narrow, and a reader who cites
    a bare "PASS" has cited something the limb never checked - which is how a vacuous green
    gets shipped. The renderer prints headline and caveat together, always.
    """

    name: str
    status: str
    headline: str
    does_not_cover: str


@dataclass
class Sandbox:
    """Every path this drill is allowed to write. Nothing outside it is touched."""

    root: Path
    clone: Path
    config_home: Path
    data_root: Path
    claude_root: Path
    settings_file: Path
    worktree_root: Path
    lock_dir: Path

    @property
    def venv_python(self) -> Path:
        if os.name == "nt":
            return self.clone / ".venv" / "Scripts" / "python.exe"
        return self.clone / ".venv" / "bin" / "python"

    @property
    def project_dir(self) -> Path:
        return self.data_root / "projects" / PROJECT


@dataclass
class Ctx:
    """Everything the limbs read. One object so a test can build a fake one cheaply."""

    sandbox: Sandbox
    runner: Runner
    env: dict
    bare_python: Optional[str] = None
    bare_python_skip_reason: str = ""
    golden: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Sandbox construction
# --------------------------------------------------------------------------- #
def build_sandbox(out_dir: Path) -> Sandbox:
    """Lay out the throwaway roots and write a minimal, valid config into the config home.

    The config is deliberately the smallest thing the strict resolver accepts:
    `git_host.kind: local` so no host CLI is needed, `notify.kind: none` so step 3 has no
    env-var NAMES to membership-test, and `verification.strategy: test-suite` so no staging
    URL is required. A richer config would test the resolver, not the installer.
    """
    root = out_dir
    sb = Sandbox(
        root=root,
        clone=root / "clone",
        config_home=root / "config-home",
        data_root=root / "data-root",
        claude_root=root / "claude-root",
        settings_file=root / "claude-root" / "settings.local.json",
        worktree_root=root / "worktrees",
        lock_dir=root / "locks",
    )
    for d in (sb.config_home / "projects", sb.data_root, sb.claude_root,
              sb.worktree_root, sb.lock_dir):
        d.mkdir(parents=True, exist_ok=True)
    sb.settings_file.write_text("{}" + NL, encoding="utf-8")

    def _posix(p: Path) -> str:
        return p.as_posix()

    (sb.config_home / "environment.md").write_text(NL.join((
        "# environment (fresh-clone drill sandbox)",
        "",
        "Written by the drill, never by a human. Every path below is inside the drill's",
        "throwaway output directory.",
        "",
        "```yaml",
        "schema_version: 1",
        "",
        "person:",
        "  name: Drill Operator",
        "",
        f'data_root: "{_posix(sb.data_root)}"',
        "",
        "git_host:",
        "  kind: local",
        "",
        "notify:",
        "  kind: none",
        "",
        f'lock_dir: "{_posix(sb.lock_dir)}"',
        "shell: " + ("powershell" if os.name == "nt" else "posix"),
        f'suite_root: "{_posix(sb.clone)}"',
        f'worktree_root: "{_posix(sb.worktree_root)}"',
        "```",
        "",
    )), encoding="utf-8")

    (sb.config_home / "projects" / f"{PROJECT}.md").write_text(NL.join((
        f"# projects/{PROJECT}.md (fresh-clone drill sandbox)",
        "",
        "```yaml",
        "schema_version: 1",
        f'name: "{PROJECT}"',
        "",
        "canonical_repo:",
        '  host_ref: "example/example"',
        f'  local_checkout: "{_posix(sb.clone)}"',
        '  base_branch: "master"',
        "",
        "verification:",
        "  strategy: test-suite",
        "",
        "work_source:",
        "  kind: filesystem",
        "```",
        "",
    )), encoding="utf-8")
    return sb


def child_env(sandbox: Sandbox, base: Optional[dict] = None) -> dict:
    """The environment every child process inherits.

    `--config-home` ALONE DOES NOT SANDBOX THIS SCRIPT. Steps 10 and 11 call
    `agentflow.config.load_environment()`, which resolves its config home from
    `$AGENTFLOW_CONFIG` or `~/.agentflow/config` and takes no argument - so the flag steers
    provision's own bookkeeping while the resolver walks off to the machine's REAL config and
    scaffolds into the REAL data root. Exporting `AGENTFLOW_CONFIG` is what actually contains
    the run; the flag is passed too so the two agree.

    `AGENTFLOW_PROJECT` is removed for a different reason: the closing doctor check reads it,
    and an ambient project name from the operator's shell names a project this fresh config
    home has never heard of, producing a FAIL that is about the shell, not the install.
    """
    env = dict(os.environ if base is None else base)
    env["AGENTFLOW_CONFIG"] = str(sandbox.config_home)
    env["CLAUDE_CONFIG_DIR"] = str(sandbox.claude_root)
    env["CLAUDE_SETTINGS_FILE"] = str(sandbox.settings_file)
    env.pop("AGENTFLOW_PROJECT", None)
    # Children write to a pipe, so on Windows they default to the ANSI code page while this
    # drill decodes UTF-8 with errors="replace" - which mangles the em-dashes provision.py
    # prints and lands them in a record meant to be read. Ask for UTF-8 on both sides.
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def provision_argv(python: str, sandbox: Sandbox, *, dry_run: bool) -> list[str]:
    argv = [
        python, str(sandbox.clone / "install" / "provision.py"),
        "--suite-root", str(sandbox.clone),
        "--config-home", str(sandbox.config_home),
        "--claude-root", str(sandbox.claude_root),
        "--settings-file", str(sandbox.settings_file),
        "--no-autodoc",
    ]
    if dry_run:
        argv.append("--dry-run")
    return argv


# --------------------------------------------------------------------------- #
# Clone + interpreter discovery
# --------------------------------------------------------------------------- #
def _reject_option_lookalike(label: str, value: str) -> None:
    """Refuse a value that git would read as an option rather than as data.

    A leading dash is the whole attack surface here: `--clone-from` and `--ref` are
    interpolated into a git argv, and git's ext:: transport turns a crafted clone URL into
    command execution. `--` guards the clone positionals, but `checkout` cannot use it for a
    ref, so the check has to exist anyway - and one refusal covering both is easier to trust
    than two different mitigations.
    """
    if value.startswith("-"):
        raise DrillFailure(
            f"{label}={value!r} starts with '-', which git parses as an option, not as data. "
            f"Refusing rather than passing it through."
        )


def clone_repo(runner: Runner, clone_from: str, ref: str, dest: Path) -> tuple[str, str]:
    """Clone and pin. Returns (sha, tree id), both of which the record must carry.

    A resolvable identifier is the whole point of the record: the evidence this drill
    replaces was cited to a commit that no longer exists, so a record that names nothing
    resolvable reproduces the exact failure that made the drill necessary.

    The TREE id is the one that survives publication. The shared repository is a fresh
    single-commit cut of the same files, so every working-history sha - including the one
    resolved here - is absent from it, while `HEAD^{tree}` hashes the files alone and comes
    out identical. An unresolvable tree is therefore refused exactly as an unresolvable sha is.
    """
    _reject_option_lookalike("--clone-from", clone_from)
    _reject_option_lookalike("--ref", ref)
    # `--` before the positionals: without it a value beginning with `-` is still parsed as a
    # git OPTION, and git's ext:: transport is enabled for a directly-invoked clone, so a
    # crafted --clone-from would run a command rather than fetch a repo. Today this flag is
    # typed by the operator on their own box, but the separator costs one token and the guard
    # has to already be there the first time somebody wraps this drill around an untrusted ref.
    res = runner.run(["git", "clone", "--quiet", "--", clone_from, str(dest)])
    if res.returncode != 0:
        raise DrillFailure(f"clone of {clone_from!r} failed: {res.output.strip()[:400]}")
    # NOT `checkout --quiet -- <ref>`: after `--` git reads PATHS, so that form would hunt for
    # a file named e.g. "master" and fail. The ref goes before the separator, which settles
    # ref-vs-path; a leading-dash ref is refused by _reject_option_lookalike above instead.
    res = runner.run(["git", "-C", str(dest), "checkout", "--quiet", ref, "--"])
    if res.returncode != 0:
        raise DrillFailure(f"could not check out ref {ref!r}: {res.output.strip()[:400]}")
    res = runner.run(["git", "-C", str(dest), "rev-parse", "HEAD"])
    sha = res.stdout.strip()
    if res.returncode != 0 or not sha:
        raise DrillFailure("cloned tree has no resolvable HEAD sha - the record would be uncitable")
    res = runner.run(["git", "-C", str(dest), "rev-parse", "HEAD^{tree}"])
    tree = res.stdout.strip()
    if res.returncode != 0 or not tree:
        raise DrillFailure(
            "cloned tree has no resolvable HEAD^{tree} id - the record would name only a "
            "commit that a single-commit cut does not carry, so it would be uncitable there"
        )
    return sha, tree


def interpreter_version(runner: Runner, python: str) -> str:
    """The interpreter's X.Y.Z, asked of the interpreter itself.

    The record names interpreters by basename only (see render_record), and `python3` says
    nothing about WHICH Python ran - on Ubuntu 24.04 it is 3.12, which CI's 3.11 legs never
    exercised, and that version is the reason a Linux run is worth recording at all. An
    answer that is not X.Y.Z is refused rather than recorded, for the same reason the sha is:
    a record that cannot say what it ran is a claim, not evidence.
    """
    res = runner.run([python, "-c", "import platform; print(platform.python_version())"])
    version = res.stdout.strip()
    if res.returncode != 0 or not re.fullmatch(r"\d+\.\d+\.\d+\S*", version):
        raise DrillFailure(
            f"could not read the version of interpreter {Path(python).name!r} "
            f"(exit {res.returncode}, output {res.output.strip()[:200]!r}) - the record "
            "would name an interpreter without saying which Python it was"
        )
    return version


def describe_os(read_os_release: Optional[Callable[[], dict]] = None) -> str:
    """The OS line for the record: the distribution's own name where one exists.

    On Linux `platform.version()` is a kernel build string ("#35-Ubuntu SMP PREEMPT_DYNAMIC
    ..."), which does not say "Ubuntu 24.04" - the one fact a Linux record exists to carry.
    os-release's PRETTY_NAME does. Where there is no os-release (Windows, macOS) the read
    raises OSError and the label falls back to the system + version string, which on Windows
    carries the build number that tells 10 from 11.
    """
    reader = read_os_release or platform.freedesktop_os_release
    try:
        pretty = reader()["PRETTY_NAME"]
    except OSError:
        # platform.version(), not platform.release(): on Windows 11 release() still returns
        # "10", so a record built from it names the wrong OS while looking precise. The build
        # number is unambiguous (11 is >= 22000) and is what a reader can actually check.
        return f"{platform.system()} {platform.version()} ({platform.machine()})"
    return f"{pretty} ({platform.system()} {platform.release()}, {platform.machine()})"


def _has_yaml(runner: Runner, python: str) -> Optional[bool]:
    """True/False if the probe ran, None if the interpreter could not be invoked at all."""
    res = runner.run([python, "-c", "import yaml"])
    if res.returncode == 0:
        return True
    if "ModuleNotFoundError" in res.output or "ImportError" in res.output:
        return False
    return None


def _is_new_enough(runner: Runner, python: str) -> Optional[bool]:
    """Does this interpreter meet the installer's own floor? None if it cannot be invoked.

    Checked SEPARATELY from PyYAML because the two failures need different handling: a
    PyYAML-carrying interpreter never triggers the handover, while a 3.10 interpreter cannot
    even reach it - `provision.py` step 1 raises "Python 3.11+ required" and exits cleanly,
    long before step 4. Without this gate, a box whose only PyYAML-less python is the system
    3.10 (Ubuntu 22.04, say) makes the drill hard-FAIL and write no record, on a machine where
    nothing is actually wrong.
    """
    probe = "import sys; sys.exit(0 if sys.version_info >= %r else 3)" % (BARE_PYTHON_MIN,)
    res = runner.run([python, "-c", probe])
    if res.returncode == 0:
        return True
    if res.returncode == 3:
        return False
    return None


def find_bare_python(runner: Runner, explicit: Optional[str] = None) -> tuple[Optional[str], str]:
    """Locate an interpreter WITHOUT PyYAML. Returns (path_or_None, reason).

    An operator-supplied interpreter that DOES have PyYAML is a loud failure, not a skip: the
    operator asked for the PyYAML-less limb and would otherwise get a green from a limb that
    never ran the condition it names - and now that the limb requires SUCCESS, that green
    would look exactly like the real thing.
    """
    if explicit:
        state = _has_yaml(runner, explicit)
        if state is None:
            raise DrillFailure(f"--bare-python {explicit!r} could not be invoked")
        if state:
            raise DrillFailure(
                f"--bare-python {explicit!r} HAS PyYAML installed, so provision would never "
                "hand over and this limb would pass without exercising the fix. Pass an "
                "interpreter without it, or omit the flag."
            )
        if _is_new_enough(runner, explicit) is False:
            raise DrillFailure(
                f"--bare-python {explicit!r} is older than Python "
                f"{BARE_PYTHON_MIN[0]}.{BARE_PYTHON_MIN[1]}, which the installer's own step-1 "
                "preflight rejects before it can reach the step-4 handover. It cannot "
                "exercise the fix. Pass a newer interpreter without PyYAML, or omit the flag."
            )
        return explicit, "supplied by --bare-python"
    too_old: list[str] = []
    for name in BARE_PYTHON_CANDIDATES:
        found = shutil.which(name)
        if not found:
            continue
        if _has_yaml(runner, found) is not False:
            continue
        if _is_new_enough(runner, found) is not True:
            too_old.append(name)
            continue
        return found, f"probed: {name} on PATH has no PyYAML"
    if too_old:
        # A SKIP with a stated reason, never a FAIL: the machine is fine, it simply has no
        # interpreter that can reach the handover. Failing here would abort a drill whose
        # other five limbs are perfectly runnable.
        return None, (
            "the only PyYAML-less interpreter(s) on PATH ("
            + ", ".join(too_old)
            + f") are older than Python {BARE_PYTHON_MIN[0]}.{BARE_PYTHON_MIN[1]}, which the "
            "installer rejects at step 1 before the step-4 handover is reachable. "
            "Pass --bare-python <path> to a newer one to run this limb."
        )
    return None, (
        "no interpreter without PyYAML was found on PATH (tried: "
        + ", ".join(BARE_PYTHON_CANDIDATES)
        + "). Pass --bare-python <path> to run this limb."
    )


def ensure_suite_venv(ctx: Ctx) -> None:
    """Make `<clone>/.venv` exist even when limb 2 skipped.

    Limb 2's run passes through step 4, which builds the venv and puts PyYAML in it - so in
    the normal case limb 3's interpreter is a by-product of limb 2 (it is the interpreter limb
    2 hands over TO). When limb 2 skips there is no such by-product, and limb 3 would have
    nothing to run. This reproduces step 4 exactly (venv + PyYAML) rather than reaching for
    the ambient interpreter, which would silently change what limb 3 is about.
    """
    sb = ctx.sandbox
    if sb.venv_python.exists():
        return
    res = ctx.runner.run([sys.executable, "-m", "venv", str(sb.clone / ".venv")], env=ctx.env)
    if res.returncode != 0:
        raise DrillFailure(f"could not build the suite venv: {res.output.strip()[:400]}")
    res = ctx.runner.run([str(sb.venv_python), "-m", "pip", "install", "--quiet", "pyyaml>=6.0"],
                         env=ctx.env)
    if res.returncode != 0:
        raise DrillFailure(f"could not install PyYAML into the suite venv: {res.output.strip()[:400]}")


# --------------------------------------------------------------------------- #
# Limbs
# --------------------------------------------------------------------------- #
def _tree(root: Path, skip: Optional[Path] = None) -> dict:
    """Map every path under `root` to a digest of its bytes (None for directories).

    A path SET is not enough to say "byte-for-byte", which is what the record claims: a run
    that rewrote the contents of a file already present would compare equal. Hashing keeps the
    claim and the check the same statement. `skip` excludes the clone, whose .venv the install
    legitimately creates and which would otherwise swamp the diff.
    """
    if not root.exists():
        return {}
    out: dict = {}
    for p in root.rglob("*"):
        if skip is not None and (p == skip or skip in p.parents):
            continue
        if p.is_dir():
            out[p] = None
        else:
            try:
                out[p] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                out[p] = "unreadable"
    return out


def limb_dry_run(ctx: Ctx) -> LimbResult:
    """1. `--dry-run` exits 0 and writes nothing.

    "Writes nothing" is checked over the WHOLE sandbox before and after - not just the data
    root. The data root is the least likely leak site: a real dry-run regression would touch
    the Claude root (step 7 deploys skills and commands there) or the settings file (steps 8
    and 9 rewrite it), and a check scoped to `data_root` would stay green through all of it.
    The clone is excluded because nothing in it is written by a dry run and its contents are
    large.
    """
    sb = ctx.sandbox
    before = _tree(sb.root, skip=sb.clone)
    python = str(sb.venv_python) if sb.venv_python.exists() else sys.executable
    res = ctx.runner.run(provision_argv(python, sb, dry_run=True), env=ctx.env)
    if res.returncode != 0:
        raise DrillFailure(
            f"--dry-run exited {res.returncode}, not 0. A dry run that cannot even print its "
            f"plan is the first thing a new contributor hits:{NL}{res.output.strip()[:600]}"
        )
    after = _tree(sb.root, skip=sb.clone)
    added = sorted(set(after) - set(before))
    changed = sorted(p for p in set(after) & set(before) if after[p] != before[p])
    removed = sorted(set(before) - set(after))
    if added or changed or removed:
        detail = [f"added {p}" for p in added[:5]] + \
                 [f"CHANGED {p}" for p in changed[:5]] + \
                 [f"removed {p}" for p in removed[:5]]
        raise DrillFailure(
            "--dry-run MUTATED the sandbox, which it promises not to do: " + "; ".join(detail)
        )
    return LimbResult(
        name="dry-run is inert",
        status=PASS,
        headline="`provision.py --dry-run` exited 0 and left the whole sandbox - data root, "
                 "Claude root and settings file - byte-for-byte as it found it (every file "
                 "digested before and after, not merely listed).",
        does_not_cover="Nothing about whether the steps it PRINTS actually work - a dry run "
                       "renders a plan, it does not execute one. Limbs 3 and 4 carry that.",
    )


def limb_pyyaml_less_run(ctx: Ctx) -> LimbResult:
    """2. The PyYAML-less real run must COMPLETE, by handing itself over to the suite venv.

    This limb was the bug witness for BUG_ISSUE and is now its regression guard - inverted
    when the fix landed, not deleted, because the sentence "the documented install works on a
    fresh machine" is exactly the sort of claim that rots back to false with nobody watching.

    Three distinct outcomes, kept distinct on purpose. A death carrying the old signature is
    named as a REGRESSION of that issue. A death carrying anything else is refused rather than
    filed under it - the same discipline the witness had, pointing the other way. And a
    success is only accepted with the handover line in the transcript, because a run that
    never handed over never touched the fix.
    """
    limb_name = f"PyYAML-less run completes via the venv handover ({BUG_ISSUE} regression guard)"
    sb = ctx.sandbox
    if not ctx.bare_python:
        return LimbResult(
            name=limb_name,
            status=SKIP,
            headline=f"SKIPPED, not passed: {ctx.bare_python_skip_reason}",
            does_not_cover=f"A skip here says NOTHING about {BUG_ISSUE} in either direction. "
                           "It is not evidence the fix holds and not evidence it is broken.",
        )
    res = ctx.runner.run(provision_argv(ctx.bare_python, sb, dry_run=False), env=ctx.env)
    out = res.output
    if res.returncode != 0:
        if _GH186_SIGNATURE.search(out):
            raise DrillFailure(
                f"REGRESSION OF {BUG_ISSUE}: the PyYAML-less run died (exit {res.returncode}) "
                "with the ModuleNotFoundError naming 'yaml' that issue is about. The handover "
                "at the end of provision step 4 is not firing, so the documented install is "
                "broken again for every contributor whose interpreter lacks PyYAML - which is "
                f"what a fresh clone means. Fix the handover, do not relax this limb:{NL}"
                + out.strip()[-800:]
            )
        raise DrillFailure(
            f"the PyYAML-less run failed (exit {res.returncode}) but NOT with the "
            f"ModuleNotFoundError naming 'yaml'. That is a DIFFERENT failure and must not be "
            f"laundered into {BUG_ISSUE}: recording it there sends the next reader to code "
            f"that is working and leaves whatever actually broke unnamed:{NL}"
            + out.strip()[-800:]
        )
    if not _HANDOVER_SIGNATURE.search(out):
        raise DrillFailure(
            "the PyYAML-less run exited 0 but never printed the handover line, so it never "
            "handed over - which means the interpreter it ran on could import PyYAML after "
            "all and this limb proved nothing. A green from a run that skipped the mechanism "
            "under test is the same blind spot the old bug witness had, in mirror image. "
            f"Check --bare-python / the PATH probe before touching {BUG_ISSUE}."
        )
    if _INSTALL_COMPLETE not in out:
        raise DrillFailure(
            f"the PyYAML-less run handed over and exited 0 but never printed `"
            f"{_INSTALL_COMPLETE}`. The handover carries the CHILD's exit code, so a child "
            "that stopped early and returned 0 reaches the operator as a clean parent exit "
            "with half an install behind it."
        )
    return LimbResult(
        name=limb_name,
        status=PASS,
        headline=(
            "The documented command works on an ordinary interpreter WITHOUT PyYAML, which is "
            "what a new contributor's shell usually is. `provision.py` ran steps 1-4 in that "
            "bare interpreter, built `<clone>/.venv` and installed PyYAML into it at step 4, "
            "then handed itself over to that venv for steps 5+ and reached "
            f"`{_INSTALL_COMPLETE}` with exit 0. Both the handover line and the last step are "
            "required, so this green cannot come from an interpreter that quietly had PyYAML "
            f"all along. This limb was {BUG_ISSUE}'s bug witness and is now its regression "
            "guard: a death here carrying `ModuleNotFoundError: No module named 'yaml'` is "
            "reported as a REGRESSION of it by name, and any other failure is refused rather "
            "than filed under it."
        ),
        does_not_cover=(
            f"{BUG_ISSUE}'s second finding, split out as gh#322 and fixed for the case it "
            "recorded: step 2 now refuses to pre-create a scraped `data_root` or "
            "`worktree_root` that is relative, or whose only existing ancestor is the drive "
            "root, and leaves the loud failure to the step-10 resolver. This drill writes a "
            "well-formed config and never exercises a malformed one, so a green above is about "
            "the handover half of that issue only; the gh#322 tests in "
            "`install/test_provision_fresh_machine.py` cover the malformed case offline. Nor does "
            "this limb inspect WHAT the handed-over child produced - limb 4 reads the sandbox "
            "after limb 3 has re-run the install on the venv interpreter, so an artifact that "
            "only the handover path gets wrong would not show up there."
        ),
    )


def limb_real_install(ctx: Ctx) -> LimbResult:
    """3. The real run on the venv interpreter reaches the last step and exits 0."""
    sb = ctx.sandbox
    ensure_suite_venv(ctx)
    res = ctx.runner.run(provision_argv(str(sb.venv_python), sb, dry_run=False), env=ctx.env)
    if res.returncode != 0:
        raise DrillFailure(
            f"the real install on the suite venv interpreter exited {res.returncode}, not 0:"
            f"{NL}{res.output.strip()[-1500:]}"
        )
    if _INSTALL_COMPLETE not in res.output:
        raise DrillFailure(
            "the install exited 0 but never printed step 11 done. Exit 0 without the last "
            "step is the shape of a run that stopped early and reported success anyway."
        )
    ctx.golden["install_stdout_tail"] = res.output.strip()[-400:]
    return LimbResult(
        name="real install completes on the suite interpreter",
        status=PASS,
        headline="`provision.py` on `<clone>/.venv` walked every applicable step, printed "
                 "`11. done.` and exited 0.",
        does_not_cover=(
            "That the steps did what they say - exit 0 is the installer's own opinion of "
            "itself. Limb 4 goes and looks. It also does not cover step 6 (autodoc Node "
            "runtime), which `--no-autodoc` skips."
        ),
    )


def limb_artifacts(ctx: Ctx) -> LimbResult:
    """4. The twelve steps' claimed outputs exist on disk in the sandbox."""
    sb = ctx.sandbox
    missing = []

    manifest = sb.claude_root / ".agentflow-deploy.json"
    if not manifest.is_file():
        missing.append(f"deploy manifest {manifest.name}")

    skills = sb.claude_root / "skills"
    n_skills = len([p for p in skills.iterdir() if p.is_dir()]) if skills.is_dir() else 0
    if n_skills < 1:
        missing.append("at least one deployed skill under claude_root/skills/")

    commands = sb.claude_root / "commands"
    n_commands = len(list(commands.glob("*.md"))) if commands.is_dir() else 0
    if n_commands < 1:
        missing.append("at least one deployed command under claude_root/commands/")

    settings_text = sb.settings_file.read_text(encoding="utf-8") if sb.settings_file.is_file() else ""
    if "SessionStart" not in settings_text:
        missing.append("a SessionStart hook registered in the settings file (step 9)")
    # Step 8 (bootstrap.py) writes its PreToolUse auto-allow hooks into the SAME file step 9
    # rewrites, so the file being present said nothing about step 8 having run. One more
    # substring turns an unchecked step into a checked one.
    if "PreToolUse" not in settings_text:
        missing.append("the PreToolUse auto-allow hooks bootstrap.py installs (step 8)")

    for sub in _SUBTREE_DIRS:
        if not (sb.project_dir / sub).is_dir():
            missing.append(f"data subtree dir projects/{PROJECT}/{sub}")

    registry = sb.project_dir / "status" / "registry.md"
    if not registry.is_file():
        missing.append(f"seeded projects/{PROJECT}/status/registry.md")
    elif not registry.read_text(encoding="utf-8").strip():
        missing.append(f"NON-EMPTY projects/{PROJECT}/status/registry.md (the file is blank)")

    if missing:
        raise DrillFailure(
            "the install exited 0 but these artifacts it claims to produce are absent: "
            + "; ".join(missing)
        )

    ctx.golden["skills_deployed"] = n_skills
    ctx.golden["commands_deployed"] = n_commands
    return LimbResult(
        name="the artifacts of steps 7, 8, 9 and 11 are on disk",
        status=PASS,
        headline=(
            f"Deploy manifest present; {n_skills} skill(s) and {n_commands} command(s) "
            "deployed into the throwaway Claude root (step 7); PreToolUse auto-allow hooks "
            "written (step 8); SessionStart hook registered (step 9); "
            f"`projects/{PROJECT}/` subtree scaffolded with a seeded registry (step 11)."
        ),
        does_not_cover=(
            "EIGHT OF THE TWELVE STEPS HAVE NO ARTIFACT CHECK HERE - steps 1-6, 10 and 12 "
            "leave nothing this limb inspects; step 6 did not run at all under --no-autodoc, "
            "and step 12 is skipped because this drill does not pass --wake-watch. "
            "This limb is named for the four it does check rather than for the install as a "
            "whole, because 'the twelve steps' artifacts' would have read as a completeness "
            "claim it cannot support. And within those four it is only a PRESENCE check: not "
            "whether any deployed skill RUNS, whether the hooks fire in a real session, or "
            "whether the registry seed is semantically valid."
        ),
    )


def limb_self_test(ctx: Ctx) -> LimbResult:
    """5. `check.py --self-test` from the clone, on the venv interpreter."""
    sb = ctx.sandbox
    res = ctx.runner.run(
        [str(sb.venv_python), str(sb.clone / "harness" / "check.py"), "--self-test"],
        cwd=sb.clone, env=ctx.env,
    )
    if res.returncode != 0:
        raise DrillFailure(
            f"`check.py --self-test` exited {res.returncode} on the fresh clone. The README "
            f"lists this as a fresh-clone row:{NL}{res.output.strip()[-1200:]}"
        )
    # A COUNT, not a slice of stdout. An earlier version stored the last 300 characters
    # and interpolated them into a one-line bullet, which broke the bullet across the
    # record and began mid-word. The section is called "Golden script numbers"; give it
    # a number, and leave the transcript to the log.
    n_self_test = len([ln for ln in res.output.splitlines() if ln.startswith("PASS self-test.")])
    if n_self_test == 0:
        # Gated rather than recorded. A count parsed from a format that has since changed
        # would print "0 self-test check(s) PASS, exit 0" into the record, which reads as a
        # measured number rather than as a broken parser - the same shape as a stale claim.
        raise DrillFailure(
            "`check.py --self-test` exited 0 but printed no 'PASS self-test.' lines. Either "
            "the self-test ran nothing, or its output format changed and this drill's parser "
            "is now silently wrong. Both need a human, and neither may be recorded as a zero."
        )
    ctx.golden["self_test_pass"] = n_self_test
    return LimbResult(
        name="check.py --self-test on the fresh clone",
        status=PASS,
        headline="`harness/check.py --self-test` exited 0 against the tracked example config, "
                 "so the assertion registry is wired and crash-free.",
        does_not_cover=(
            "Any assertion's VERDICT. The self-test proves the wiring imports and does not "
            "crash; it deliberately runs with no records present, so it cannot tell a correct "
            "assertion from an inverted one."
        ),
    )


def limb_scenarios(ctx: Ctx) -> LimbResult:
    """6. `--only C,F,G` after copying the example config verbatim, placeholders unreplaced.

    Verbatim on purpose. `harness/README.md` tells a reader to copy the example and edit it;
    the groups that are supposed to run on a fresh clone must run BEFORE any editing, or the
    "fresh clone?" column is promising something conditional on work nobody described.
    """
    sb = ctx.sandbox
    harness = sb.clone / "harness"
    example = harness / "harness.config.example.json"
    if not example.is_file():
        raise DrillFailure(f"the tracked example config is missing from the clone: {example}")
    shutil.copyfile(example, harness / "harness.config.json")

    res = ctx.runner.run(
        [str(sb.venv_python), str(harness / "check.py"), "--only", "C,F,G"],
        cwd=sb.clone, env=ctx.env,
    )
    summary = _SUMMARY_RE.search(res.output)
    if res.returncode != 0:
        raise DrillFailure(
            f"`check.py --only C,F,G` exited {res.returncode} on a fresh clone with the "
            f"example config copied verbatim. The README promises these groups run out of "
            f"the box:{NL}{res.output.strip()[-1500:]}"
        )
    if not summary:
        raise DrillFailure(
            "`check.py --only C,F,G` exited 0 but printed no `# summary:` line. Exit 0 with "
            "no assertions run is a vacuous green, and this drill will not record one."
        )
    n_pass, n_fail, n_skip = (int(x) for x in summary.groups())
    if n_pass < 1 or n_fail != 0:
        raise DrillFailure(
            f"`--only C,F,G` summary reads {n_pass} PASS / {n_fail} FAIL / {n_skip} SKIP, "
            "which is not the clean, non-empty result the README's table promises."
        )
    ctx.golden["cfg_pass"] = n_pass
    ctx.golden["cfg_fail"] = n_fail
    ctx.golden["cfg_skip"] = n_skip

    # Full-suite numbers, recorded as CONTEXT and never gated on. Groups A, B, D and E2 (and
    # C4) read `records/` or the wake log, which harness/run-scenario.md says are deliberately
    # never committed, so on a fresh clone those rows SKIP as unrecorded and check.py exits 3
    # (gh#628; exit 1 is reserved for a real FAIL). Recording the counts and the exit code
    # keeps a future reader from mistaking the expected unrecorded SKIPs for a regression.
    full = ctx.runner.run(
        [str(sb.venv_python), str(harness / "check.py")], cwd=sb.clone, env=ctx.env,
    )
    full_summary = _SUMMARY_RE.search(full.output)
    if full_summary:
        ctx.golden["full_pass"] = int(full_summary.group(1))
        ctx.golden["full_fail"] = int(full_summary.group(2))
        ctx.golden["full_skip"] = int(full_summary.group(3))
        ctx.golden["full_exit"] = full.returncode

    return LimbResult(
        name="reconstructable scenario groups on a fresh clone",
        status=PASS,
        headline=(
            f"After copying `harness.config.example.json` VERBATIM (placeholders unreplaced), "
            f"`check.py --only C,F,G` reported {n_pass} PASS / {n_fail} FAIL / {n_skip} SKIP "
            "and exited 0."
        ),
        does_not_cover=(
            "Groups A, B, D and E2 (and C4), which read the gitignored `records/` tree or the "
            "wake log and SKIP as unrecorded (exit 3) on any fresh clone BY DESIGN. Their SKIP "
            "count and exit code are recorded in this drill's golden numbers as context, not "
            "as a gate, and are not a regression."
        ),
    )


LIMBS: tuple[Callable[[Ctx], LimbResult], ...] = (
    limb_dry_run,
    limb_pyyaml_less_run,
    limb_real_install,
    limb_artifacts,
    limb_self_test,
    limb_scenarios,
)


# --------------------------------------------------------------------------- #
# Record
# --------------------------------------------------------------------------- #
def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def render_record(*, now: datetime, sha: str, tree: str, clone_from: str, ref: str,
                  os_label: str, bare_python: Optional[str], bare_python_note: str,
                  venv_python: str, limbs: list[LimbResult], golden: dict,
                  bare_python_version: Optional[str] = None,
                  venv_python_version: Optional[str] = None) -> str:
    """Render the citable artifact. Every fact an auditor would otherwise re-derive."""
    g = golden

    def _num(key: str, fallback: str = "not captured") -> str:
        return str(g[key]) if key in g else fallback

    lines = [
        "# Fresh-clone install drill",
        "",
        f"- **Run at:** {_iso(now)} (clock injected, not read inside a limb)",
        f"- **Cloned from:** `{clone_from}` at ref `{ref}`",
        f"- **Resolved tree:** `{tree}` (`git rev-parse HEAD^{{tree}}`; a single-commit cut "
        "of the same files carries this id, so this is the one to cite)",
        f"- **Resolved sha:** `{sha}` (a commit in the cloned history; a single-commit cut "
        "does not carry it)",
        f"- **OS:** {os_label}",
        # Both interpreter lines are REDUCED to a basename / sandbox-relative form on purpose.
        # An absolute path here carries the operator's username on Windows, and the README
        # tells you to paste this record into a pull request - so the one artifact designed to
        # be published was the one place a home path leaked. The file already states the rule
        # for BARE_PYTHON_CANDIDATES; this is the same rule applied where it actually escapes.
        "- **Bare interpreter (no PyYAML):** "
        + (f"{Path(bare_python).name} (Python {bare_python_version or 'version not captured'})"
           if bare_python else "none - " + bare_python_note),
        "- **Suite interpreter:** `<sandbox>/clone/.venv` (built by install step 4), Python "
        f"{venv_python_version or 'version not captured'}",
        "- **Autodoc:** disabled (`--no-autodoc`); step 6 skipped, not exercised",
        "",
        "## Limbs",
        "",
    ]
    for limb in limbs:
        lines.append(f"### {limb.status} - {limb.name}")
        lines.append("")
        lines.append(limb.headline)
        lines.append("")
        lines.append(f"*Does not cover:* {limb.does_not_cover}")
        lines.append("")

    lines += [
        "## Golden script numbers",
        "",
        f"- `check.py --self-test`: {_num('self_test_pass')} self-test check(s) PASS, exit 0",
        f"- `check.py --only C,F,G`: {_num('cfg_pass')} PASS / {_num('cfg_fail')} FAIL / "
        f"{_num('cfg_skip')} SKIP, exit 0",
        f"- full suite (all groups): {_num('full_pass')} PASS / {_num('full_fail')} FAIL / "
        f"{_num('full_skip')} SKIP, exit {_num('full_exit')} - the SKIPs are chiefly the "
        "record-dependent assertions in groups A, B, D and E2 (and C4), which SKIP as "
        "unrecorded on any fresh clone; exit 3 means unrecorded, not a FAIL (exit 1 would)",
        f"- deployed into the throwaway Claude root: {_num('skills_deployed')} skill(s), "
        f"{_num('commands_deployed')} command(s)",
        "",
        "## What this establishes",
        "",
        "A clone of the published repo at the tree named above, installed into a throwaway",
        "config home, data root and Claude root, reaches the end of `install/provision.py`",
        "and leaves behind the artifacts the twelve steps claim to produce. The two",
        "fresh-clone rows the harness README advertises - `check.py --self-test` and the",
        "reconstructable scenario groups - both run green from that clone with the tracked",
        "example config copied verbatim, placeholders unreplaced.",
        "",
        f"It also establishes the first half of {BUG_ISSUE} as FIXED and still fixed: the",
        "documented command, run on an ordinary interpreter without PyYAML, hands itself over",
        "to the venv step 4 builds and completes. That limb was the bug's witness and is now",
        "its regression guard - inverted rather than deleted, so a re-break is named as a",
        f"regression instead of quietly re-arriving. {BUG_ISSUE}'s second finding, split out as",
        "gh#322, is fixed for the case it recorded: step 2 refuses to pre-create a scraped",
        "`data_root` or `worktree_root` that is relative, or whose only existing ancestor is",
        "the drive root, and leaves the loud failure to the step-10 resolver. This run does",
        "not exercise that fix - the drill writes a well-formed config - and the gh#322 tests",
        "in `install/test_provision_fresh_machine.py` are where it is covered.",
        "",
        "## What it deliberately does not establish",
        "",
        f"1. **One OS only.** This run is {os_label} and says nothing about any other. CI",
        "   covers Windows and Linux for the test suite; this drill's result is",
        "   single-platform and is cited as such.",
        "2. **The record-dependent scenario groups did not run.** Groups A, B, D and E2 read",
        "   `records/`, which is deliberately never committed (`harness/run-scenario.md`), so",
        "   a fresh clone fails them by design. Their numbers appear above as context only.",
        "3. **The human interview was never exercised.** The drill WRITES a minimal config by",
        "   hand and hands it to `provision.py`. Everything `install/SKILL.md` decides - the",
        "   questions, the one-way secret write, the judgement about what the machine has -",
        "   is untested here, and a green run says nothing about it.",
        "4. **Autodoc's Node runtime step was skipped**, so npm-side breakage is invisible to",
        "   this drill.",
        "",
        "Each limit is stated here rather than left for an auditor to discover, because a",
        "record that reads as broader than the run it describes is how a narrow green becomes",
        "a wide claim.",
        "",
    ]
    return NL.join(lines)


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #
def run_drill(out_dir: Path, now: Optional[datetime] = None, *,
              runner: Optional[Runner] = None,
              clone_from: str = DEFAULT_CLONE_FROM,
              ref: str = DEFAULT_REF,
              bare_python: Optional[str] = None,
              sandbox: Optional[Sandbox] = None,
              sha: Optional[str] = None,
              tree: Optional[str] = None,
              read_os_release: Optional[Callable[[], dict]] = None) -> str:
    """Run every limb. Returns the record text; raises DrillFailure on the first limb that fails.

    `runner`, `sandbox`, `sha`/`tree` and `read_os_release` are injection seams for the CI
    companion, which must break one limb at a time without cloning or provisioning anything.
    `sha` and `tree` stand in for the clone together or not at all: injecting one alone would
    leave the other to a clone that was never meant to run.
    """
    now = now or datetime.now(UTC)
    runner = runner or Runner()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sb = sandbox or build_sandbox(out_dir / "sandbox")
    if (sha is None) != (tree is None):
        raise ValueError("inject `sha` and `tree` together, or neither")
    if sha is None:
        sha, tree = clone_repo(runner, clone_from, ref, sb.clone)

    found_bare, bare_note = find_bare_python(runner, bare_python)
    ctx = Ctx(
        sandbox=sb,
        runner=runner,
        env=child_env(sb),
        bare_python=found_bare,
        bare_python_skip_reason=bare_note,
    )

    results = [limb(ctx) for limb in LIMBS]

    # Read AFTER the limbs: the venv only exists once limb 2 or limb 3 has built it.
    bare_version = interpreter_version(runner, found_bare) if found_bare else None
    venv_version = interpreter_version(runner, str(sb.venv_python))

    record = render_record(
        now=now, sha=sha, tree=tree, clone_from=clone_from, ref=ref,
        os_label=describe_os(read_os_release),
        bare_python=found_bare, bare_python_note=bare_note,
        bare_python_version=bare_version, venv_python_version=venv_version,
        venv_python=str(sb.venv_python), limbs=results, golden=ctx.golden,
    )
    (out_dir / RECORD_NAME).write_text(record, encoding="utf-8")
    return record


def main_cli(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="fresh_clone_install_drill.py", description=__doc__)
    ap.add_argument("--out", default=None,
                    help="directory for the sandbox and the record (default: a temp dir)")
    ap.add_argument("--clone-from", default=DEFAULT_CLONE_FROM,
                    help=f"repo path or URL to clone (default: {DEFAULT_CLONE_FROM})")
    ap.add_argument("--ref", default=DEFAULT_REF, help=f"ref to check out (default: {DEFAULT_REF})")
    ap.add_argument("--bare-python", default=None,
                    help="an interpreter WITHOUT PyYAML, for the venv-handover limb "
                         "(default: probe PATH; the limb SKIPs if none is found)")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="fresh-clone-drill-"))
    try:
        record = run_drill(out, clone_from=args.clone_from, ref=args.ref,
                           bare_python=args.bare_python)
    except DrillFailure as exc:
        sys.stderr.write(f"DRILL FAILED: {exc}{NL}")
        # Name the sandbox on the failure path too. A failed run has already cloned the repo
        # and may have built a venv - hundreds of megabytes - and when `--out` defaulted to a
        # temp dir, this line is the operator's only way to find it, either to read the
        # transcript or to delete it. Printing it only on success is how a drill that fails
        # leaves litter nobody can locate.
        sys.stderr.write(f"# sandbox left in place for inspection: {out}{NL}")
        return 1
    sys.stdout.write(record)
    sys.stdout.write(f"{NL}# record written: {out / RECORD_NAME}{NL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
