#!/usr/bin/env python3
"""agentflow multi-runtime provisioner (P1-T18).

The mechanical half of the installer. `install/SKILL.md` runs the human-facing
interview and writes the config markdown + one-way secrets; this script provisions
the runtimes those steps depend on, in a fixed order, per `install/manifest.md`.

Ordered steps (the whole install sequence, so `--dry-run` shows the full plan):

  1. Preflight — verify the manifest runtimes are present (Python 3.11+, Git,
     optional `gh`; Node + npm ONLY when autodoc is enabled), and that the Claude
     settings file steps 8 and 9 read already exists and parses the way they parse
     it, so a missing, empty, BOM-prefixed or non-JSON-object one refuses before
     anything is written (gh#743, gh#751).
  2. Config presence — `~/.agentflow/config/environment.md` must exist (written by
     the SKILL.md interview) and resolve; a broken config is a loud hard-fail.
     Also pre-creates `data_root` (parents ok) so the strict resolver at step 10
     finds it on a fresh machine (the scaffold step runs after the resolver).
  3. Secrets — the true env vars are written ONE-WAY into `settings.local.json` /
     `.env` by the interview (single source of truth per fact, never in markdown);
     this step confirms the named vars resolve (missing NAMES are a loud fail,
     values never printed), it never writes a secret.
  4. Python venv — create `<suite_root>/.venv` and install PyYAML (the ONLY pipeline
     runtime dependency).
  5. Editable install — `pip install -e skills/_shared` (the `agentflow` package),
     which kills the old `sys.path` hacks.
  6. Autodoc runtimes (autodoc ONLY) — `npm ci` in the autodoc Node tree-sitter
     package, then `pip install -e` of the autodoc_graph package (epic 702) into
     the suite venv (brings sqlite-vec). Skipped entirely unless autodoc is
     enabled; the plain pipeline needs neither Node nor sqlite-vec.
  7. Deploy skills + commands + hook scripts — place `skills/<name>/`, `commands/*.md`
     and `skills/_shared/scripts/plan-lint.sh` (to `scripts/plan-lint.sh`, a stable managed
     path; provision registers no hook for it) into the user's Claude config dir (`--claude-root`,
     default `~/.claude`) so the fleet slash commands resolve and the hook script
     has one managed host path. Copy on Windows, symlink on Unix. `_shared` is the pip package (step 5) and never
     deploys as a SKILL; `_vendored` deploys because skills reference it by a
     relative sibling path. Foreign (non-agentflow) name
     collisions refuse loudly; `--force` refreshes agentflow-owned entries. A
     JSON manifest (`<claude_root>/.agentflow-deploy.json`) records
     source → target + content hash per entry.
  8. bootstrap.py — run once to install the PreToolUse auto-allow hooks. Honors
     `CLAUDE_SETTINGS_FILE`. Skipped with `--no-auto-allow`, which removes nothing
     an earlier run wrote (docs/INSTALL.md section 13 has the manual removal).
  9. SessionStart hook — register `hooks/sessionstart-inject-config.py` in the
     settings file so config is injected at session open (a hook, not an instruction).
     A non-default config home is also written to that file's `env.AGENTFLOW_CONFIG`,
     so the hook reads the home this run provisioned; a conflicting value refuses.
 10. Presence-checks — run the resolver, which presence-checks every delegated
     SKILL.md path and hard-fails on missing required config.
 11. Scaffold data_root subtree — create `<data_root>/projects/` and the eight
     per-project subdirs of DATA_SUBTREE_DIRS (`design/{specs,plans,use-cases,
     suggestions} status wiki/pages{,/done,/archived}`; queue/ is retired). The
     `autodoc/` pillar is deliberately NOT among them, and on a fresh install
     /autodoc-init does not create it either - it resolves its root through
     resolve_autodoc_root and exits when none resolves (gh#393).
     For every configured project, seed each `status/registry.md` from
     `templates/registry.md` (the fleet coordination surface, docs/REGISTRY.md,
     `<project>` placeholder replaced), then seed each `design/use-cases/` folder with
     `registry.jsonl` (empty), `.counter` (0), and a `domains.md` tree stub
     (Phase-4 UC spine). Idempotent (write-if-absent, so a live registry or an
     allocated counter survives); the consolidated single-root layout the skills
     and the disk epic-survey read (SCHEMA §1 / docs/DATA-LAYOUT.md).
 12. Missed-wake watchdog — OPT-IN, off by default (gh#544): a plain run SKIPS it
     and registers nothing; `--wake-watch` turns it on. Opted in, it registers
     `python -m agentflow.wake_watch` with the OS scheduler, one entry per configured
     project, so a stalled board is reported by something that is not itself an
     agentflow loop. **This is the suite's ONE daemon** (single-daemon rule,
     2026-09-22, replacing the daemon-free invariant); it is report-only and never
     relaunches a lane. Windows registers it via `schtasks /Create /F` (idempotent by
     name). Elsewhere the step SKIPS and prints the exact crontab line to add, because
     a provisioner must not rewrite a crontab that holds the operator's own jobs. A run
     without the flag deletes nothing an earlier opted-in run registered (removal is
     docs/INSTALL.md section 13). Runs last: it needs step 11's registry to exist.

`--dry-run` prints this ordered plan and exits 0 WITHOUT importing `agentflow`,
touching a venv, running a subprocess, or writing a file — nothing is mutated. A
real run executes each applicable step in order and fails loudly on the first error.

Runtime declaration lives in `install/manifest.md` (human-readable); the structured
step list below is hand-kept in agreement with it. Deps for THIS script: stdlib only.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import errno
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

SESSIONSTART_HOOK_REL = "hooks/sessionstart-inject-config.py"
BOOTSTRAP_REL = "skills/orchestrator/bootstrap.py"
SHARED_PKG_REL = "skills/_shared"
# Deployment manifest under claude_root: source -> target + content hash per
# entry — the seam the future updater skill reads. Ownership record #1.
DEPLOY_MANIFEST_NAME = ".agentflow-deploy.json"
# Marker dropped inside each COPIED skill dir (ownership record #2, survives a
# lost manifest). Never written through a symlink — that would write into the
# suite itself. Excluded from content hashing.
DEPLOY_MARKER_NAME = ".agentflow-deployed"
# skills/ subdirs that are NOT Claude skills: _shared is the pip package,
# consumed via `pip install -e` (step 5) — nothing in skill prose references it
# under ~/.claude (verified: refs are `pip`-package or `<suite>/skills/_shared`
# paths). _vendored IS deployed: skills reference it as `../_vendored/...`.
SKILLS_NON_DEPLOY = {"_shared", "__pycache__"}
AUTODOC_PKG_REL = "skills/autodoc-init/references/inventory"
# The autodoc graph package (epic 702): Python `autodoc_graph` (schema, storage
# adapter, sqlite-vec backend, embedder, `graph` CLI), editable-installed into
# the suite venv — autodoc ONLY, so the plain pipeline stays PyYAML-only.
GRAPH_PKG_REL = "skills/autodoc-init/references/graph"
# The fixed per-project subtree derived from data_root + project name (SCHEMA §1).
# queue/ is RETIRED (docs/REGISTRY.md): per-agent loop state lives at
# status/<agent>-state.md; shared coordination lives only in status/registry.md.
DATA_SUBTREE_DIRS = (
    "design/specs",
    "design/plans",
    "design/use-cases",
    "design/suggestions",
    "status",
    "wiki/pages",
    "wiki/pages/done",
    "wiki/pages/archived",
)
# Seed source for each project's status/registry.md (the fleet coordination
# surface, docs/REGISTRY.md). `<project>` placeholders are replaced with the
# project name at seed time.
REGISTRY_TEMPLATE_REL = "templates/registry.md"
# Seed files scaffolded inside each project's design/use-cases/ folder (Phase-4 UC spine).
# Created only when ABSENT — never overwritten — so an already-allocated .counter or
# a non-empty registry.jsonl survives a re-run. The allocator (agentflow.use_case_id)
# reads .counter via int(text.strip()) with empty/missing == 0, so "0\n" is a valid seed.
USE_CASE_DIR = "design/use-cases"
USE_CASE_SEED_FILES = {
    "registry.jsonl": "",   # append-only birth-certificate log; empty = no UCs minted yet
    ".counter": "0\n",      # monotonic allocator counter (flock target); starts at zero
}
# The wiki pillar's own directory under a project. Its `pages/` subdirs come from
# DATA_SUBTREE_DIRS above; the two READ targets in it (`hot.md`, `digest.md`) are
# seeded from `agentflow.wiki.initial_pillar_files` — the bodies are IMPORTED, never
# restated here, so a scaffolded hot file cannot drift from a refreshed one.
WIKI_DIR = "wiki"
USE_CASE_DOMAINS_STUB = """# Use-Case Domains

Human-editable nested domain tree for this project's use cases. Each use case
carries a `Domain:` path (for example `auth/login`) that points at a node below.
Add domains as nested bullets; re-parent by moving a bullet (opaque UC-IDs mean
no references break).

- (no domains yet)
"""
# Heuristic-only scan for display ordering; the real gate is autodoc_root in config.
_AUTODOC_ROOT_SET = re.compile(r"^\s*autodoc_root\s*:\s*(?!null\b|~?\s*$)\S", re.MULTILINE)
# First fenced yaml block of a config markdown file (same shape the resolver reads).
_YAML_FENCE = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)
# Top-level data_root scalar inside that block: "quoted", 'quoted', or bare value.
_DATA_ROOT_LINE = re.compile(
    r"""^data_root\s*:\s*(?:"([^"]+)"|'([^']+)'|([^#\s]+))""", re.MULTILINE
)

# --- The missed-wake watchdog (step 12). THE ONE DAEMON. -------------------- #
# Scheduler entry name, one per configured project: `agentflow-wake-watch-<project>`.
# Project-scoped because a registry is per-project and a watcher pointed at the wrong
# board reports an all-clear about a file nobody writes.
WATCHDOG_TASK_PREFIX = "agentflow-wake-watch"
# Machine-level home for the wrapper script + its log, derived from config_home
# (`~/.agentflow/watchdog/`) rather than written as a literal: nothing here may carry a
# user's home path or a machine name, and every path below is computed from Options.
WATCHDOG_DIR_NAME = "watchdog"
# Hours between invocations. Cheap (one file read, one conditional push) and far finer
# than the failure it exists for: the three recorded blackouts lasted days. The operator
# owns this number - docs/INSTALL.md section 13 says how to change it.
WATCHDOG_INTERVAL_HOURS = 1

# Environment keys naming a directory the step-10 resolver requires to ALREADY EXIST.
# Every one of these needs the same pre-creation treatment as data_root; see
# _ensure_required_dirs for why the resolver deliberately does not create them itself.
#
# suite_root is required-to-exist too and is deliberately NOT here: it names the checkout
# this script runs from, so it always exists when right. Pre-creating it would only ever
# fire when it is WRONG, and would turn the resolver's loud "does not exist" into an
# empty directory that fails later on something unrelated (gh#417).
_REQUIRED_DIR_KEYS = ("data_root", "worktree_root", "lock_dir")


def _scalar_line(key: str) -> "re.Pattern[str]":
    """Match `key: value` in a YAML block, quoted or bare, ignoring a trailing comment."""
    return re.compile(
        r"""^%s\s*:\s*(?:"([^"]+)"|'([^']+)'|([^#\s]+))""" % re.escape(key),
        re.MULTILINE,
    )


# --------------------------------------------------------------------------- #
# Path + option resolution
# --------------------------------------------------------------------------- #
@dataclass
class Options:
    dry_run: bool
    suite_root: Path
    config_home: Path
    settings_file: Path
    autodoc: bool
    autodoc_reason: str
    # True when autodoc came from `--autodoc` / `--no-autodoc` rather than the heuristic.
    # An explicit flag is never overruled by the resolved-config re-check (gh#690): it
    # either stands or the install refuses. Defaulted so existing Options(...) keep the
    # heuristic behaviour.
    autodoc_explicit: bool = False
    # Deployment target root for skills/commands. Overridable so every test runs
    # against a tmp dir — the real ~/.claude is only exercised in a live install.
    claude_root: Path = field(default_factory=lambda: Path.home() / ".claude")
    force: bool = False
    # Step 8 (the PreToolUse auto-allow hooks). Defaulted so every existing Options(...)
    # keeps meaning "today's install"; `--no-auto-allow` is the only way to turn it off.
    auto_allow: bool = True
    # Step 12 (the missed-wake watchdog). OFF unless the operator opts in with `--wake-watch`
    # (gh#544, 2026-09-24): a fresh provision registers no scheduled task.
    wake_watch: bool = False


def _suite_root(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    # install/provision.py -> repo root is the parent of install/
    return Path(__file__).resolve().parent.parent


def _config_home(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    override = os.environ.get("AGENTFLOW_CONFIG")
    if override:
        return Path(override).expanduser()
    return _default_config_home()


def _default_config_home() -> Path:
    # A function, not a constant: the hook resolves the same default at session open, and
    # tests move the home directory under it.
    return Path.home() / ".agentflow" / "config"


def _config_home_env(config_home: Path) -> Optional[str]:
    """The AGENTFLOW_CONFIG value the settings env must carry, or None for the default home.

    The SessionStart hook runs with the session's environment, not provision's, so a config
    home chosen by --config-home or a shell-level $AGENTFLOW_CONFIG is invisible to it unless
    the settings file carries it (gh#689). Absolute, because a relative value would resolve
    against whatever directory a later session opens in.
    """
    home = config_home.expanduser().resolve()
    if home == _default_config_home().resolve():
        return None
    return str(home)


def _check_config_env(settings_path: Path, settings: dict, config_home: Path) -> None:
    """Refuse a settings `env.AGENTFLOW_CONFIG` that names a config home other than this run's.

    Overwriting it would silently re-point every lane launched against this file, and
    leaving it would have every lane read a config home this run never checked. Either way
    the operator has to say which one is meant.
    """
    if "env" not in settings:
        return
    env = settings["env"]
    # A present `"env": null` is refused like any other non-object: step 9 would have to
    # replace it, which is rewriting a value the operator wrote.
    if not isinstance(env, dict):
        kind = _JSON_KIND.get(type(env), type(env).__name__)
        raise RuntimeError(
            f'settings file "env" is {kind}, not an object: {settings_path}\n'
            "Claude Code's settings schema requires an object ({...}) of variable names to\n"
            "values, and step 9 may add AGENTFLOW_CONFIG to it. Fix it by hand."
        )
    existing = env.get("AGENTFLOW_CONFIG")
    if existing is None:
        return   # absent or null: step 9 writes it (or, for the default home, nothing)
    ours = config_home.expanduser().resolve()
    # Only an absolute value can agree: a relative one resolves against whatever directory
    # a session opens in, so it matching from provision's cwd today proves nothing.
    if (isinstance(existing, str) and existing
            and Path(existing).expanduser().is_absolute()
            and Path(existing).expanduser().resolve() == ours):
        return
    raise RuntimeError(
        f"settings file already sets env.AGENTFLOW_CONFIG={existing!r}: {settings_path}\n"
        f"This run provisions the config home {ours}, and every session launched against\n"
        "that file would read the other one. Provision will not overwrite it. Either re-run\n"
        f"with --config-home {existing} (or $AGENTFLOW_CONFIG set to it) if that is the home\n"
        "you mean, or remove the AGENTFLOW_CONFIG entry from the file's \"env\" and re-run."
    )


def _settings_file(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    override = os.environ.get("CLAUDE_SETTINGS_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "settings.local.json"


def _claude_root(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def _resolve_autodoc(flag: Optional[bool], config_home: Path) -> tuple[bool, str]:
    """(enabled, reason). Explicit flag > AGENTFLOW_AUTODOC env > light config scan > off.

    The config scan is a text heuristic so `--dry-run` needs neither PyYAML nor an
    importable agentflow - neither exists yet at the point this runs.

    IT IS A HEURISTIC AND IT USED TO BE THE ONLY GATE, while the docstring here claimed
    "the authoritative gate at real-run time is env.autodoc_root". No such second gate
    existed; a reader trusting that sentence would have believed a display hint could not
    decide anything, when it decided everything. `_confirm_autodoc_against_config` now
    makes the sentence true - it re-checks against the resolved config after step 5's
    editable install, and says so out loud when the two disagree.
    """
    if flag is True:
        return True, "forced by --autodoc"
    if flag is False:
        return False, "forced by --no-autodoc"
    env_val = os.environ.get("AGENTFLOW_AUTODOC")
    if env_val is not None:
        on = env_val.strip().lower() in {"1", "true", "yes", "on"}
        return on, f"AGENTFLOW_AUTODOC={env_val!r}"
    # `autodoc_root` is per-PROJECT as well as machine-level (SCHEMA §2). Scanning only
    # environment.md would report autodoc off for a machine whose default is null while a
    # project sets its own - which is the exact configuration the per-project field was
    # added to support, so the heuristic would be wrong first on the users who adopted it.
    scanned = []
    env_md = config_home / "environment.md"
    if env_md.exists():
        scanned.append(env_md)
        if _AUTODOC_ROOT_SET.search(env_md.read_text(encoding="utf-8")):
            return True, f"autodoc_root set in {env_md}"

    projects_dir = config_home / "projects"
    if projects_dir.is_dir():
        for md in sorted(projects_dir.glob("*.md")):
            scanned.append(md)
            if _AUTODOC_ROOT_SET.search(md.read_text(encoding="utf-8")):
                return True, f"autodoc_root set in {md}"

    if scanned:
        return False, f"autodoc_root unset/null in {len(scanned)} config file(s)"
    return False, "no config present (autodoc_root undetermined)"


def _confirm_autodoc_against_config(opts: "Options") -> None:
    """Re-decide autodoc from the RESOLVED config, now that agentflow is importable.

    `_resolve_autodoc` runs before the editable install exists, so it can only read the
    config files as text. That is a real constraint, not a shortcut - but a text scan
    cannot apply `resolve_autodoc_root`'s precedence (project override, then machine
    default, then the derived per-project path), and the derived case is invisible to a
    regex by construction: there is nothing written down to match.

    So this re-checks, and MUTATES `opts.autodoc` when it disagrees with the heuristic. An
    EXPLICIT flag is never overruled: `--no-autodoc` stays off and the disagreement is printed as
    a warning, and `--autodoc` with no resolvable root refuses (RuntimeError). It runs at the end
    of step 5 because that is the first moment the resolver can be imported and the last
    moment before step 6 acts on the answer.

    A DISAGREEMENT IS PRINTED, never applied quietly. A gate that silently corrects a
    display value is how the two drift apart again: the next reader sees the heuristic's
    reason in the banner, the run does something else, and nothing connects them.

    `AutodocRootError` is deliberately NOT caught. It means a resolved root has no
    manifest while a legacy root still holds a corpus - the migration hazard where an
    empty directory passes every existence check while the real pages sit orphaned. An
    installer that shrugged at that would scaffold the empty one and let `/autodoc-init`
    regenerate hundreds of pages from scratch.
    """
    sys.path.insert(0, str(opts.suite_root / SHARED_PKG_REL))
    from agentflow.config import (  # noqa: E402  (post editable install)
        load_environment,
        load_project,
        resolve_autodoc_root,
    )

    env = load_environment()
    resolved: list[str] = []

    projects_dir = opts.config_home / "projects"
    if projects_dir.is_dir():
        for md in sorted(projects_dir.glob("*.md")):
            root = resolve_autodoc_root(env, load_project(md.stem))
            if root:
                resolved.append(f"{md.stem} -> {root}")
    if not resolved:
        root = resolve_autodoc_root(env, None)
        if root:
            resolved.append(f"(machine default) -> {root}")

    truth = bool(resolved)
    resolved_reason = (
        f"resolved config says {'enabled' if truth else 'disabled'}"
        + (f" ({'; '.join(resolved)})" if resolved else "")
    )
    if truth == opts.autodoc:
        return

    if opts.autodoc_explicit:
        # An explicit flag is the operator's decision; the config may inform it, never
        # silently replace it (gh#690).
        if opts.autodoc:
            raise RuntimeError(
                "autodoc REFUSED: --autodoc was passed but the resolved config has no "
                "autodoc root (autodoc_root is unset/null in environment.md and every "
                "projects/*.md, and none derives), so autodoc cannot work. Set "
                "autodoc_root in the config, or drop --autodoc."
            )
        # --no-autodoc stands: autodoc stays OFF, and the disagreement is named.
        print(
            "  autodoc     : WARNING --no-autodoc keeps autodoc disabled, but the "
            f"resolved config enables it: {resolved_reason}"
        )
        return

    was, reason_was = opts.autodoc, opts.autodoc_reason
    opts.autodoc = truth
    opts.autodoc_reason = resolved_reason
    print(
        f"  autodoc     : CORRECTED {'enabled' if was else 'disabled'} -> "
        f"{'enabled' if truth else 'disabled'}"
    )
    print(f"                pre-install heuristic said: {reason_was}")
    print(f"                post-install re-check     : {opts.autodoc_reason}")

# --------------------------------------------------------------------------- #
# Step model
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    n: int
    title: str
    detail: list[str] = field(default_factory=list)
    applies: bool = True
    skip_note: str = ""
    run: Optional[Callable[[Options], None]] = None
    #: Lazy forms, for a step whose applicability an EARLIER step can still change.
    #: `applies` is read when the step list is built; these are read when the step runs.
    applies_fn: Optional[Callable[[Options], bool]] = None
    skip_note_fn: Optional[Callable[[Options], str]] = None

    def is_applicable(self, opts: "Options") -> bool:
        return self.applies_fn(opts) if self.applies_fn is not None else self.applies

    def skip_reason(self, opts: "Options") -> str:
        return self.skip_note_fn(opts) if self.skip_note_fn is not None else self.skip_note


def _run_cmd(cmd: list[str], *, env: Optional[dict] = None, cwd: Optional[Path] = None) -> None:
    """Run a subprocess, fail loudly on non-zero (no swallowed errors).

    cmd[0] is resolved via shutil.which first: subprocess.run does not apply
    PATHEXT on Windows, so bare `npm` (really `npm.cmd`) raises FileNotFoundError
    without this. An unresolvable executable fails loudly, naming the original
    command.
    """
    exe = shutil.which(cmd[0])
    if exe is None:
        raise RuntimeError(
            f"executable not found on PATH: '{cmd[0]}' (needed for: {' '.join(cmd)})\n"
            f"Install it or fix PATH, then re-run provision."
        )
    proc = subprocess.run([exe, *cmd[1:]], env=env, cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed (exit {proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


# --- real-run actions (only invoked when not --dry-run) -------------------- #
def _posix_settings_paths(settings_path: Path) -> tuple[str, str]:
    """(parent dir, file) as a POSIX remedy line spells them for *settings_path*."""
    if settings_path == Path.home() / ".claude" / "settings.local.json":
        return "~/.claude", "~/.claude/settings.local.json"
    # A --settings-file / CLAUDE_SETTINGS_FILE path: the lines must write THAT file, or
    # copying them fixes nothing and the next run refuses again.
    return f'"{settings_path.parent.as_posix()}"', f'"{settings_path.as_posix()}"'


def _missing_settings_message(settings_path: Path) -> str:
    """The refusal for an absent Claude settings file, shared by step 1 and step 9.

    Names the documented remedy (install/SKILL.md prerequisite 2, docs/INSTALL.md section 4)
    rather than "run Claude Code once": both docs warn that signing in may or may not leave
    the file behind, so that advice sends the reader round a loop that can end where it began.
    """
    parent, target = _posix_settings_paths(settings_path)
    return (
        f"settings file not found: {settings_path}\n"
        "Create it containing {} before provisioning (install/SKILL.md prerequisite 2,\n"
        "docs/INSTALL.md section 4). POSIX:\n"
        f"    mkdir -p {parent}\n"
        f"    [ -f {target} ] || echo '{{}}' > {target}\n"
        "On Windows PowerShell use the BOM-free form in install/SKILL.md (Out-File -Encoding\n"
        "utf8 writes a BOM the JSON readers reject). Or pass --settings-file (or set\n"
        "CLAUDE_SETTINGS_FILE) to point at an existing file."
    )


def _ps_quoted(settings_path: Path) -> str:
    """*settings_path* as a PowerShell single-quoted literal: `'` doubles, nothing else is special."""
    return "'" + str(settings_path).replace("'", "''") + "'"


def _resave_utf8_lines(settings_path: Path, read_encoding: str = "") -> str:
    # UTF8Encoding($false) writes no BOM. With no read_encoding, ReadAllText honours a UTF-8 or
    # UTF-16 BOM, so the contents survive; a BOM-less non-UTF-8 file needs its encoding named,
    # or ReadAllText decodes it as UTF-8 and turns every such byte into U+FFFD.
    source = f"$f, {read_encoding}" if read_encoding else "$f"
    return (
        "In PowerShell:\n"
        f"    $f = {_ps_quoted(settings_path)}\n"
        f"    [System.IO.File]::WriteAllText($f, [System.IO.File]::ReadAllText({source}),"
        " (New-Object System.Text.UTF8Encoding($false)))\n"
        "(the BOM-free form in install/SKILL.md prerequisite 2)."
    )


_JSON_KIND = {list: "an array", str: "a string", int: "a number", float: "a number",
              bool: "a boolean", type(None): "null", dict: "an object"}

# Every refusal leads with the schema rule; a step is named only where it really dereferences
# the key. Step 8 is skipped under --no-auto-allow, so its clause says so rather than taking
# opts: the refusal stays true either way, and the schema rule alone justifies it.
_SCHEMA = "Claude Code's hooks schema requires "
_STEP_8 = "step 8 (skills/orchestrator/bootstrap.py) reads it unless --no-auto-allow is passed"
_STEP_9 = "step 9 reads it"
# Tools bootstrap looks up by matcher with next(...), each scan stopping at its first match.
_BOOTSTRAP_MATCHERS = ("Bash", "Edit", "Write", "Agent")


def _check_hooks_shape(settings_path: Path, settings: dict) -> None:
    """Refuse a "hooks" section steps 8 and 9 would crash on, naming the offending key (gh#755).

    Enforces Claude Code's hooks schema as far as the two readers walk it: "hooks" is an
    object; each event's value is an array (for every event, including ones neither step
    reads); each SessionStart and PreToolUse entry is an object whose "hooks", when present,
    is an array of objects. Beyond the schema's shapes, the first PreToolUse block with
    matcher "Bash" must have a "hooks" key and each of its hooks' "if", when present, must be
    a string: bootstrap indexes that block's hooks with [] and puts every "if" in a set. Only
    the FIRST Bash block, because that is the only one bootstrap's next(...) reaches; a later
    Bash block without "hooks" is read by neither step, so refusing it would refuse a file
    both handle. Extra keys, odd matchers and empty arrays are left alone.
    """
    def refuse(where: str, problem: str, why: str) -> None:
        raise RuntimeError(
            f'settings file has a malformed "hooks" section: {settings_path}\n'
            f"{where} {problem}.\n"
            f"{why}.\n"
            "Fix it by hand, keeping the entries it holds: provision only adds hook entries to\n"
            'what is already there. An empty section is "hooks": {}; each event maps to an\n'
            'array of matcher blocks such as [{"matcher": "Bash", "hooks": []}]\n'
            "(install/SKILL.md prerequisite 2)."
        )

    def wrong(found: object, expected: str) -> str:
        return f"is {_JSON_KIND.get(type(found), type(found).__name__)}; it must be {expected}"

    if "hooks" not in settings:
        return
    hooks = settings["hooks"]
    if not isinstance(hooks, dict):
        refuse("hooks", wrong(hooks, "an object ({...})"),
               "Step 9 adds its SessionStart entry under it, and step 8 its PreToolUse\n"
               "entries unless --no-auto-allow is passed")
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            step = {"SessionStart": _STEP_9, "PreToolUse": _STEP_8}.get(event)
            refuse(f"hooks.{event}", wrong(blocks, "an array ([...])"),
                   _SCHEMA + "every hook event to map to an array of matcher blocks"
                   + (f";\n{step} and appends to it" if step else ""))

    def check_block_hooks(where: str, block: dict, step: str | None) -> None:
        inner = block["hooks"]
        read = f";\n{step}" if step else ""
        if not isinstance(inner, list):
            refuse(f"{where}.hooks", wrong(inner, "an array ([...])"),
                   _SCHEMA + "a matcher block's hooks to be an array of hook objects" + read)
        for j, hook in enumerate(inner):
            if not isinstance(hook, dict):
                refuse(f"{where}.hooks[{j}]", wrong(hook, "an object ({...})"),
                       _SCHEMA + "each hook to be an object" + read)

    for i, block in enumerate(hooks.get("SessionStart", [])):
        where = f"hooks.SessionStart[{i}]"
        if not isinstance(block, dict):
            refuse(where, wrong(block, "an object ({...})"),
                   _SCHEMA + "each entry to be a matcher block object;\n" + _STEP_9)
        if "hooks" in block:
            check_block_hooks(where, block, _STEP_9)

    pretool = hooks.get("PreToolUse", [])
    # Bootstrap calls .get on every entry up to where its last next(...) scan stops (the end,
    # when a tool has no block yet); entries past that are held to the schema only.
    first = {}
    for i, block in enumerate(pretool):
        if isinstance(block, dict) and block.get("matcher") in _BOOTSTRAP_MATCHERS:
            first.setdefault(block["matcher"], i)
    reach = max(first.get(t, len(pretool) - 1) for t in _BOOTSTRAP_MATCHERS)
    for i, block in enumerate(pretool):
        where = f"hooks.PreToolUse[{i}]"
        if not isinstance(block, dict):
            refuse(where, wrong(block, "an object ({...})"),
                   _SCHEMA + "each entry to be a matcher block object"
                   + (f";\n{_STEP_8}" if i <= reach else ""))
        if i != first.get("Bash"):
            if "hooks" in block:
                check_block_hooks(where, block, None)
            continue
        if "hooks" not in block:
            refuse(where, 'has matcher "Bash" but no "hooks" key; it must have one',
                   _SCHEMA + 'a matcher block to carry its "hooks" array;\n' + _STEP_8
                   + ", adding its Bash\nauto-allow hooks to this, the first Bash block")
        check_block_hooks(where, block, _STEP_8)
        for j, hook in enumerate(block["hooks"]):
            if "if" in hook and not isinstance(hook["if"], str):
                refuse(f"{where}.hooks[{j}].if", wrong(hook["if"], "a string"),
                       _SCHEMA + '"if" to be a permission-rule string such as "Bash(git *)";\n'
                       + _STEP_8 + ", collecting every \"if\" in this block")


def _read_settings(settings_path: Path) -> dict:
    """Parse the Claude settings file as steps 8 and 9 do, or refuse naming the problem.

    Both readers (step 9 here, step 8's skills/orchestrator/bootstrap.py) do
    `json.loads(path.read_text(encoding="utf-8"))` and then `settings.setdefault("hooks", ...)`,
    so this makes the same call and accepts exactly what they accept: UTF-8 text with no BOM
    that parses to a JSON object whose "hooks" section, if any, has the shape both readers
    walk (_check_hooks_shape, gh#755). Step 1 calls it so a bad file refuses before any write
    (gh#751); step 9 calls it as the backstop for a --resume-from run that skipped step 1.
    """
    try:
        text = settings_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        if e.object.startswith((b"\xff\xfe", b"\xfe\xff")):
            # A UTF-16 BOM names its own encoding, so the plain ReadAllText form is lossless.
            remedy = (
                "It starts with a UTF-16 byte-order mark, which Windows PowerShell 5's > and\n"
                "Out-File write by default. Re-save it as UTF-8 without a BOM; ReadAllText reads\n"
                "the UTF-16 mark, so this keeps its contents. " + _resave_utf8_lines(settings_path)
            )
        else:
            remedy = (
                "It has no byte-order mark. This remedy ASSUMES it is Windows-1252, the usual\n"
                "non-UTF-8 encoding on Windows; if it is something else, name that encoding\n"
                "instead of 1252 (reading it as UTF-8 would replace the bad bytes and lose them).\n"
                "Re-save it as UTF-8 without a BOM. "
                + _resave_utf8_lines(settings_path, "[System.Text.Encoding]::GetEncoding(1252)")
            )
        raise RuntimeError(
            f"settings file is not UTF-8 text: {settings_path}\n"
            f"byte 0x{e.object[e.start]:02x} at offset {e.start} does not decode ({e.reason}).\n"
            "Steps 8 and 9 read it as UTF-8.\n" + remedy
        ) from e
    if text.startswith("\ufeff"):
        raise RuntimeError(
            f"settings file starts with a UTF-8 byte-order mark (BOM): {settings_path}\n"
            'The JSON readers in steps 8 and 9 reject it ("Unexpected UTF-8 BOM"); Out-File\n'
            "-Encoding utf8 on Windows PowerShell writes one. Re-save it as UTF-8 without a\n"
            "BOM, keeping its contents. " + _resave_utf8_lines(settings_path)
        )
    # JSON's own whitespace set, not str.strip()'s wider one: a form feed is not JSON
    # whitespace, so a file holding one is invalid JSON, not an empty file.
    if not text.strip(" \t\n\r"):
        _parent, target = _posix_settings_paths(settings_path)
        raise RuntimeError(
            f"settings file is {'empty' if not text else 'whitespace only'}: {settings_path}\n"
            "Steps 8 and 9 parse it as JSON and there is none. It holds no settings either, so\n"
            "replace it with {} (install/SKILL.md prerequisite 2, docs/INSTALL.md section 4).\n"
            "POSIX:\n"
            f"    echo '{{}}' > {target}\n"
            "Windows PowerShell, BOM-free (Out-File -Encoding utf8 writes a BOM the JSON\n"
            "readers reject):\n"
            f"    [System.IO.File]::WriteAllText({_ps_quoted(settings_path)}, '{{}}',"
            " (New-Object System.Text.UTF8Encoding($false)))"
        )
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"settings file is not valid JSON: {settings_path}\n"
            f"{e.msg} at line {e.lineno}, column {e.colno}.\n"
            "It may hold your Claude Code settings, so fix it at that spot rather than replacing\n"
            "it: provision only adds hook entries to what is already there. A settings file must\n"
            "be a JSON object; the minimal one is {} (install/SKILL.md prerequisite 2)."
        ) from e
    if not isinstance(settings, dict):
        kind = _JSON_KIND.get(type(settings), type(settings).__name__)
        raise RuntimeError(
            f"settings file is not a JSON object: {settings_path}\n"
            f'Its top level is {kind}; steps 8 and 9 add a "hooks" key to it, which needs an\n'
            "object ({...}). Fix it by hand, keeping any settings it holds inside an object;\n"
            "the minimal one is {} (install/SKILL.md prerequisite 2)."
        )
    _check_hooks_shape(settings_path, settings)
    return settings


def _do_preflight(opts: Options) -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError(f"Python 3.11+ required, found {sys.version.split()[0]}")
    if shutil.which("git") is None:
        raise RuntimeError("Git not found on PATH (required)")
    # gh is optional — absence is not fatal.
    if opts.autodoc:
        for tool in ("node", "npm"):
            if shutil.which(tool) is None:
                raise RuntimeError(f"{tool} not found on PATH (required for autodoc)")
    # Steps 8 and 9 both read this file. Checked here, before step 2 pre-creates data_root
    # dirs and step 7 deploys into the Claude config dir, so a missing file refuses with
    # nothing written rather than after a half-install (gh#743). Same resolved path as step 9:
    # both read opts.settings_file (--settings-file > $CLAUDE_SETTINGS_FILE > default).
    if not opts.settings_file.is_file():
        raise RuntimeError(_missing_settings_message(opts.settings_file))
    # Present is not enough: an empty, BOM-prefixed or non-JSON-object file passed the check
    # above and then crashed step 8 or 9 after the same writes (gh#751). Parsed, not kept.
    settings = _read_settings(opts.settings_file)
    # Step 9 refuses a conflicting env.AGENTFLOW_CONFIG too; checked here as well so the
    # refusal lands before steps 2-8 have written anything (gh#689).
    _check_config_env(opts.settings_file, settings, opts.config_home)


def _first_yaml_block(env_md: Path) -> str:
    """Text of the first fenced yaml block in a config markdown file ('' if none).

    Text-level extraction on purpose: this script is stdlib-only (PyYAML lands in
    the suite venv at step 4), and the authoritative parse stays with the strict
    resolver in step 9 — these helpers are best-effort pre-steps, never a second
    schema.
    """
    m = _YAML_FENCE.search(env_md.read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def _unsafe_to_precreate(target: Path) -> Optional[str]:
    """Why `target` must NOT be pre-created, or None when creating it is safe.

    Step 2 reads these values with a regex, eight steps before the strict resolver
    parses the same file. The scrape is deliberate - this script is stdlib-only until
    the venv lands at step 4 - but a best-effort read must not be acted on
    irreversibly. gh#322 recorded a config whose paths had lost their prefix
    materialising two directories at the drive root, then failing several steps later
    on an unrelated key.

    The stray directory is the smaller half. Creating it also PRE-SATISFIES the
    resolver's own exists() check, so the one component that could still have caught
    the mangled value no longer can - the run dies later, somewhere else, on something
    that reads like a different problem. Refusing here is what restores the resolver's
    ability to fail loudly and in the right place.

    Narrower than gh#322's own candidate shape 1, on purpose. "Reject a path whose
    parent does not already exist" refuses exactly what this function was written to
    create: a legitimate data_root names a tree that does not exist yet, two levels
    deep under somewhere real, and that behaviour is pinned by
    test_config_presence_precreates_missing_data_root. The observed failure has a
    sharper signature - no existing ancestor at all except the anchor itself.
    """
    if not target.is_absolute():
        return f"not an absolute path: {target}"
    if not any(parent.exists() for parent in target.parents
               if parent != Path(target.anchor)):
        return f"no existing parent below the drive root: {target}"
    return None


def _ensure_required_dirs(opts: Options) -> None:
    """Pre-create every environment dir the step-10 resolver requires to already exist.

    The resolver hard-fails on a missing required directory by design, but the scaffold
    step that would create one runs AFTER the resolver - so a fresh machine died at the
    resolver step. Provision's job is to make those dirs exist first; the resolver stays
    strict. A missing or unparseable key is left for the resolver's loud, actionable
    ConfigError.

    This covers data_root, worktree_root and lock_dir. worktree_root was added later and hit
    the SAME failure this function was written to fix: a fresh install following INSTALL.md
    verbatim died at step 10, after the venv, the editable install and the skill deploy had
    all already run. lock_dir then hit it a third time (gh#417) - harmless on POSIX, where
    the template value is /tmp, fatal on a fresh Windows box. Anything added to
    _REQUIRED_DIR_KEYS is handled from then on, which is the point of doing it by list
    rather than one function per key.

    Expansion matches the resolver's exactly - Path(raw).expanduser(), nothing more - so
    the directory created is the directory later required. An unexpanded %VAR% value is
    therefore relative here as it is there, and _unsafe_to_precreate refuses it.
    """
    env_md = opts.config_home / "environment.md"
    if not env_md.exists():
        return
    block = _first_yaml_block(env_md)
    for key in _REQUIRED_DIR_KEYS:
        m = _scalar_line(key).search(block)
        if not m:
            continue  # resolver owns the loud failure for a broken or absent key
        raw = next(g for g in m.groups() if g)
        target = Path(raw).expanduser()
        reason = _unsafe_to_precreate(target)
        if reason:
            if "%" in raw:
                # Neither side expands %VAR%; say so here, where the fix is obvious.
                reason += "; %VAR% is never expanded - write the expanded absolute path"
            print(
                f"provision: not pre-creating {key} ({reason}); "
                "leaving the loud failure to the step-10 resolver",
                file=sys.stderr,
            )
            continue
        target.mkdir(parents=True, exist_ok=True)


def _do_config_presence(opts: Options) -> None:
    env_md = opts.config_home / "environment.md"
    if not env_md.exists():
        raise RuntimeError(
            f"config not found: {env_md}\n"
            "Run the install/SKILL.md interview first — it writes environment.md."
        )
    _ensure_required_dirs(opts)


def _notify_env_var_names(yaml_text: str) -> list[str]:
    """Env-var NAMES declared under notify: (url_env/topic_env); [] when kind: none."""
    names: list[str] = []
    kind = None
    in_notify = False
    for line in yaml_text.splitlines():
        if re.match(r"^notify\s*:", line):
            in_notify = True
            continue
        if in_notify:
            if line.strip() and not line[0].isspace():
                break  # dedent: notify block ended
            m = re.match(
                r"""^\s+(kind|url_env|topic_env)\s*:\s*(?:"([^"]+)"|'([^']+)'|([^#\s]+))""",
                line,
            )
            if m:
                val = next(g for g in m.groups()[1:] if g)
                if m.group(1) == "kind":
                    kind = val
                else:
                    names.append(val)
    return [] if kind == "none" else names


def _do_secrets(opts: Options) -> None:
    """Confirm the env-var NAMES declared in environment.md resolve in this shell.

    The interview owns the one-way WRITE of secret values into
    settings.local.json/.env; this step only membership-tests the NAMES against
    os.environ — values are never read or printed.
    """
    env_md = opts.config_home / "environment.md"
    if not env_md.exists():
        return  # step 2 already hard-failed a missing config before this runs
    names = _notify_env_var_names(_first_yaml_block(env_md))
    missing = [n for n in names if n not in os.environ]
    if missing:
        raise RuntimeError(
            f"secrets check FAILED: named env var(s) not set in this shell: {', '.join(missing)}\n"
            "Export them in the shell running provision (and persist them in the\n"
            'settings.local.json "env" block or .env per install/SKILL.md step 3),\n'
            "then re-run. Only NAMES are checked; values are never read or printed."
        )


def _venv_python(opts: Options) -> Path:
    venv = opts.suite_root / ".venv"
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _do_python_venv(opts: Options) -> None:
    venv = opts.suite_root / ".venv"
    if not venv.exists():
        _run_cmd([sys.executable, "-m", "venv", str(venv)])
    py = str(_venv_python(opts))
    _run_cmd([py, "-m", "pip", "install", "--upgrade", "pip"])
    _run_cmd([py, "-m", "pip", "install", "pyyaml>=6.0"])
    # THE HANDOVER POINT, and it belongs at the END of this step rather than inside the next
    # one. This is the first moment the venv and PyYAML exist, so it is the earliest point the
    # handover is possible -- and handing off from inside step 5 meant the child re-ran step 5
    # from the top: a second `pip install -e`, a duplicated step-5 header, and a parent that
    # printed `5. ...` and never printed `5. done.`. That transcript contradicts the very
    # rationale `--resume-from` was added for.
    if not _parent_can_import_deps() and not os.environ.get(_REEXEC_MARKER):
        _reexec_into_venv(opts, resume_from=5)


#: Set on the child so a re-exec can never recurse. The condition that triggers one -- a
#: parent interpreter without PyYAML -- is already false in the venv, so this is belt and
#: braces rather than the mechanism; a loop here would be an install that never terminates.
_REEXEC_MARKER = "AGENTFLOW_PROVISION_REEXEC"

#: The argv `main()` actually parsed, captured there and read by the handover.
#:
#: NOT `sys.argv[1:]`. `main(argv=None)` accepts an explicit argv and the install tests use that
#: programmatic entry, so the two genuinely differ -- and when they do, the child is spawned with
#: the WRONG options. A caller whose process argv is empty hands off to a child that sees no
#: `--config-home` / `--claude-root` / `--settings-file` at all, so those resolve to their
#: defaults: the operator's real `~/.agentflow/config` and `~/.claude`. A sandboxed run would
#: re-target itself at the live machine, silently, at the moment it changed process.
_INVOKED_ARGV: list = []


def _parent_can_import_deps() -> bool:
    """Can the interpreter running THIS script import what the later steps need?

    Only PyYAML is checked, because it is the module `agentflow.config` imports at import
    time (`config.py:33`) and therefore the one that decides whether a step may `from
    agentflow.config import ...` at all.
    """
    try:
        import yaml  # noqa: F401
    except Exception:
        # Exception, not ImportError: a HALF-INSTALLED PyYAML raises from its own module body
        # (a broken .pth, a partially-written wheel, a C-extension mismatch), and that is still
        # "this interpreter cannot use the deps" -- which is the only question being asked.
        # Catching the narrow class let such a failure escape as an unguarded traceback from
        # inside the predicate whose whole job is to answer it without raising.
        return False
    return True


def _reexec_into_venv(opts: Options, resume_from: int) -> None:
    """Hand the rest of the run to the venv interpreter, resuming at `resume_from`.

    THE SCRIPT PLEDGES TO BE STDLIB-ONLY AND FIVE LATER SITES BROKE THAT PLEDGE. Step 4
    installs PyYAML by subprocess INTO `<suite_root>/.venv`, so it never reaches the
    interpreter the user typed - and steps 5, 10 (twice), 11 and the closing doctor report all
    do `from agentflow.config import ...` in that parent. On any machine whose python does not
    already carry PyYAML - which is what "fresh clone" means - step 5 died with a raw
    `ModuleNotFoundError` traceback and exit 1. Nothing documented the hidden precondition:
    `install/manifest.md` calls PyYAML "the ONLY third-party dep" installed BY the install.

    Re-exec rather than five subprocess call sites: each of those five does real resolver
    work (autodoc roots, presence checks, the data subtree, the verification report), so
    marshalling five sets of results across a process boundary is five places to get wrong.
    One handover at the moment the deps first exist fixes all of them, and every step after
    it runs where the dependencies actually are.

    Steps 1-4 are NOT re-run: `--resume-from` skips them. They are idempotent, but re-running
    them would reprint four "done" lines the reader has already seen, and an installer whose
    output does not match its own step numbering is one nobody can follow.
    """
    py = _venv_python(opts)
    if not py.exists():
        raise RuntimeError(
            f"cannot hand off to the suite venv: {py} does not exist after the venv step. "
            "Re-run provision, or create the venv by hand and re-run."
        )
    argv = [str(py), os.path.abspath(__file__), *_INVOKED_ARGV, "--resume-from", str(resume_from)]
    print(f"  -- handing off to the suite venv ({py}) for steps {resume_from}+")
    # THE CHILD INHERITS THIS FILE DESCRIPTOR. Python block-buffers stdout when it is a pipe or
    # a file rather than a TTY, so without this flush the parent's banner, steps 1-4 and the
    # line above are emitted at parent EXIT -- after the child has already streamed steps 5-12
    # into the same fd. Every non-interactive reader (the fresh-clone drill captures through a
    # pipe, CI captures, an operator running `> install.log`) then sees the transcript
    # backwards. Only a TTY is line-buffered, which is exactly where this was not noticed.
    sys.stdout.flush()
    sys.stderr.flush()
    env = dict(os.environ, **{_REEXEC_MARKER: "1"})
    raise _HandOff(subprocess.call(argv, env=env))


class _HandOff(Exception):
    """Carries the child's exit code up to `main`, which returns it unchanged.

    An exception rather than a return value because the handover happens inside a Step's
    `run`, and `execute` has no channel for "stop here, successfully". Caught in `main` beside
    the ordinary failure handler.
    """

    def __init__(self, code: int) -> None:
        super().__init__(f"handed off to the suite venv (exit {code})")
        self.code = code


def _do_editable_install(opts: Options) -> None:
    py = str(_venv_python(opts))
    pkg = opts.suite_root / SHARED_PKG_REL
    _run_cmd([py, "-m", "pip", "install", "-e", str(pkg)])
    # First moment the resolver can be imported, and the last before step 6 reads the
    # answer. See _confirm_autodoc_against_config for why the text heuristic cannot be
    # the final word.
    _confirm_autodoc_against_config(opts)


def _do_autodoc_npm(opts: Options) -> None:
    autodoc_pkg = _autodoc_pkg_dir(opts)
    _run_cmd(["npm", "ci"], cwd=autodoc_pkg)


def _do_autodoc_graph_pip(opts: Options) -> None:
    """Editable-install the autodoc_graph package into the suite venv.

    Mirrors _do_editable_install (venv python, never bare `python`); the
    package's own pyproject pulls sqlite-vec — the ONLY dep autodoc adds to
    the venv, and only when autodoc is enabled.
    """
    py = str(_venv_python(opts))
    _run_cmd([py, "-m", "pip", "install", "-e", str(_graph_pkg_dir(opts))])


def _do_autodoc_runtime(opts: Options) -> None:
    """Step 6: both autodoc runtimes — Node inventory deps, then the graph package."""
    _do_autodoc_npm(opts)
    _do_autodoc_graph_pip(opts)


def _graph_pkg_dir(opts: Options) -> Path:
    # Fixed location under suite_root, same discipline as _autodoc_pkg_dir:
    # no environment config round-trip, loud error naming the expected path.
    p = opts.suite_root / GRAPH_PKG_REL
    if not (p / "pyproject.toml").exists():
        raise RuntimeError(
            f"autodoc graph package not found: expected {p / 'pyproject.toml'}"
        )
    return p


def _autodoc_pkg_dir(opts: Options) -> Path:
    # Autodoc's tree-sitter package lives at a fixed location under suite_root —
    # no environment config round-trip needed to find it.
    p = opts.suite_root / AUTODOC_PKG_REL
    if not (p / "package.json").exists():
        raise RuntimeError(
            f"autodoc Node package not found: expected {p / 'package.json'}"
        )
    return p


def _do_bootstrap(opts: Options) -> None:
    py = str(_venv_python(opts))
    script = opts.suite_root / BOOTSTRAP_REL
    env = {**os.environ, "CLAUDE_SETTINGS_FILE": str(opts.settings_file)}
    _run_cmd([py, str(script)], env=env)


def _do_sessionstart_hook(opts: Options) -> None:
    settings_path = opts.settings_file
    if not settings_path.is_file():
        # Backstop: step 1 already refused this, but a --resume-from run skips step 1 and
        # the file can vanish between steps.
        raise RuntimeError(_missing_settings_message(settings_path))
    settings = _read_settings(settings_path)
    _check_config_env(settings_path, settings, opts.config_home)
    # Suite venv interpreter (bare `python` lacks the agentflow package on a fresh
    # machine); both paths quoted so spaces survive the hook shell.
    hook_cmd = f'"{_venv_python(opts)}" "{opts.suite_root / SESSIONSTART_HOOK_REL}"'
    hooks = settings.setdefault("hooks", {})
    sessionstart = hooks.setdefault("SessionStart", [])
    already = any(
        h.get("command") == hook_cmd
        for block in sessionstart
        for h in block.get("hooks", [])
    )
    changed = not already
    if not already:
        sessionstart.append({"hooks": [{"type": "command", "command": hook_cmd}]})
    # Beside the hook, in the same file, so the hook reads the config home this run
    # provisioned (gh#689). An existing equal value is left exactly as written; a null one
    # is treated as absent. _check_config_env above has already refused a non-object "env".
    config_env = _config_home_env(opts.config_home)
    if config_env is not None:
        env = settings.setdefault("env", {})
        if env.get("AGENTFLOW_CONFIG") is None:
            env["AGENTFLOW_CONFIG"] = config_env
            changed = True
    if changed:
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _do_presence_checks(opts: Options) -> None:
    sys.path.insert(0, str(opts.suite_root / SHARED_PKG_REL))
    from agentflow.config import (  # noqa: E402  (post editable install)
        ConfigError,
        load_environment,
        load_project,
    )
    try:
        load_environment()
        projects_dir = opts.config_home / "projects"
        if projects_dir.is_dir():
            for md in sorted(projects_dir.glob("*.md")):
                load_project(md.stem)
    except ConfigError as e:
        raise RuntimeError(f"config presence-check FAILED (hard-fail):\n{e}")


def _verification_readiness(opts: Options) -> list:
    """Report what each configured project's done-gate can ACTUALLY do.

    Not a failure path: a library with no UI is correctly configured with no
    staging.url, and this never blocks an install. It exists so provision cannot
    print an unqualified success banner over a gate that will not run as
    designed — the same honesty the loops owe when they halt.

    Returns [(project_name, strategy, [gap, ...]), ...]. Safe to call only after
    step 10, which has already hard-failed on any unloadable config.
    """
    sys.path.insert(0, str(opts.suite_root / SHARED_PKG_REL))
    from agentflow.config import load_project  # noqa: E402  (post editable install)

    projects_dir = opts.config_home / "projects"
    if not projects_dir.is_dir():
        return []

    report = []
    for md in sorted(projects_dir.glob("*.md")):
        project = load_project(md.stem)
        strategy = project.verification.strategy
        url = project.staging.url if project.staging else None
        recipe = project.staging.deploy_recipe if project.staging else None
        gaps = []

        if strategy in ("test-suite", "smoke-cli"):
            gaps.append(
                f"strategy is `{strategy}`, so the browser done-gate never runs. Tests "
                "call functions directly and pass whether or not anything calls them — "
                "on a project with a user-facing surface this gate can go green over a "
                "broken app."
            )
        elif strategy == "none":
            gaps.append("strategy is `none`: nothing verifies work before it is marked done.")

        if url is None:
            gaps.append(
                "no staging.url, so any browser journey must start its own instance every "
                "firing — a cold start each time, and nothing persists between runs."
            )
        elif not recipe:
            gaps.append(
                "staging.url is set but staging.deploy_recipe is empty: no described path "
                "for getting a change into that environment."
            )

        report.append((project.name, strategy, gaps))
    return report


def _print_verification_readiness(report: list) -> None:
    """Print the honest closing verdict. Silent when every gate is fully wired."""
    gaps_total = sum(len(gaps) for _, _, gaps in report)
    if not gaps_total:
        print("\nagentflow provision complete.")
        return

    print(f"\nagentflow provision complete — with {gaps_total} verification gap(s).")
    print("The install itself is done. What follows is what your DONE-GATE can actually do,")
    print("so it is not discovered later from a green result that proved nothing:")
    for name, strategy, gaps in report:
        if not gaps:
            continue
        print(f"\n  [{name}]  verification.strategy = {strategy}")
        for gap in gaps:
            print(f"    - {gap}")
    print("\n  Fix by editing ~/.agentflow/config/projects/<name>.md — the resolver re-reads")
    print("  it at the next session open. A SKILL.md path in verification.strategy is the")
    print("  supported way to run an automated suite AND drive browser journeys.")


def _seed_use_case_files(project_dir: Path) -> None:
    """Seed the design/use-cases/ folder's registry, counter, and domain-tree stub.

    Idempotent by write-if-absent: each file is created only when it does not yet
    exist, so an already-allocated `.counter`, a non-empty `registry.jsonl`, or a
    hand-edited `domains.md` is never clobbered on a re-run. The allocator
    (`agentflow.use_case_id`) treats a missing/empty counter as 0, so the "0\\n"
    seed and the empty registry are equivalent to a never-minted project.
    """
    uc_dir = project_dir / USE_CASE_DIR
    uc_dir.mkdir(parents=True, exist_ok=True)
    for name, content in USE_CASE_SEED_FILES.items():
        f = uc_dir / name
        if not f.exists():
            f.write_text(content, encoding="utf-8", newline="\n")
    domains = uc_dir / "domains.md"
    if not domains.exists():
        domains.write_text(USE_CASE_DOMAINS_STUB, encoding="utf-8", newline="\n")


def _seed_wiki_pillar(project_dir: Path, project_name: str) -> None:
    """Seed the wiki pillar's READ targets (`hot.md`, `digest.md`) and its residue baseline.

    The directories alone are not enough. Both files are read before anything writes
    them — a lane opens `hot.md` at wake, dev-manager opens `digest.md` at session
    start — and an ABSENT file cannot tell a reader "no lessons yet" from "the pillar
    is broken". Seeding valid empty bodies makes the first state legible and leaves
    the second reportable.

    THE BODIES ARE IMPORTED, NOT RESTATED. `agentflow.wiki.initial_pillar_files` owns
    the single definition of a valid empty pillar, and its `hot.md` is literally
    `render_projection` on no pages — the same call the refresh makes. A hand-typed
    copy here would drift the day the projection's banner or count line changes, and
    the drift would surface as a scaffolded file contradicting every refreshed one.

    Write-if-absent, like every other seed in this step: a live `hot.md` (or a digest
    the steward has actually written) is never clobbered on a re-run.

    Import is local and post-`sys.path` insert, same as `load_environment` in the
    caller: `agentflow` is editable-installed in step 5, so it is importable by the
    time this step runs and not before.
    """
    from agentflow.wiki import initial_pillar_files  # noqa: E402  (post editable install)
    from agentflow.pillar_residue import (  # noqa: E402  (post editable install)
        Report,
        baseline_path,
        iter_live_pages,
        write_baseline,
    )

    wiki_dir = project_dir / WIKI_DIR
    wiki_dir.mkdir(parents=True, exist_ok=True)
    for name, body in initial_pillar_files(project_name).items():
        target = wiki_dir / name
        if not target.exists():
            target.write_text(body, encoding="utf-8", newline="\n")
    # An EMPTY residue baseline, dated today (gh#651). Without one, `agentflow.wiki residue`
    # exits 2 CANNOT MEASURE on every steward wake until a human records one; a pillar with NO
    # pages has no residue to excuse, so an empty baseline is its true starting state. A pillar
    # that already has pages gets none: an empty baseline there would flag every existing page
    # as NEW PAGE WITH RESIDUE and make the human's first --update-baseline a "worsening", so
    # exit 2 is left to ask a human to read the report and record one. The page set is
    # pillar_residue's own (iter_live_pages), so this cannot disagree with what the check scans.
    # Written by the module's own writer, never hand-typed JSON, and write-if-absent.
    baseline = baseline_path(wiki_dir)
    if not baseline.exists() and next(iter_live_pages(wiki_dir), None) is None:
        write_baseline(baseline, Report(), recorded=datetime.date.today().isoformat())


def _seed_registry(project_dir: Path, project_name: str, suite_root: Path) -> None:
    """Seed `status/registry.md` from `templates/registry.md` (docs/REGISTRY.md).

    Replaces every `<project>` placeholder with the project name. Write-if-absent:
    a live registry (real Agents/Queue/Questions rows) is never clobbered on a
    re-run. A missing template is a loud fail — the registry is the fleet's only
    coordination surface, so a scaffold that silently skips it breaks every loop.
    """
    registry = project_dir / "status" / "registry.md"
    if registry.exists():
        return
    template = suite_root / REGISTRY_TEMPLATE_REL
    if not template.exists():
        raise RuntimeError(
            f"registry template not found: {template}\n"
            "The suite checkout is incomplete — templates/registry.md seeds each\n"
            "project's status/registry.md (the fleet coordination surface)."
        )
    text = template.read_text(encoding="utf-8").replace("<project>", project_name)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(text, encoding="utf-8", newline="\n")


def _do_scaffold_data_root(opts: Options) -> None:
    """Create the consolidated data_root subtree (SCHEMA §1 / docs/DATA-LAYOUT.md).

    Ensures `<data_root>/projects/` exists and, for every configured
    `projects/<name>.md`, the per-project subdirs
    (`design/{specs,plans,use-cases,suggestions} status wiki/pages{,/done,/archived}` — queue/ and the
    flat refine/solution are retired; per-agent state
    lives at status/<agent>-state.md), seeds `status/registry.md` from
    `templates/registry.md` (docs/REGISTRY.md), seeds the design/use-cases/ folder
    with its registry.jsonl / .counter / domains.md (Phase-4 UC spine), then seeds the
    wiki pillar's two READ targets, `wiki/hot.md` and `wiki/digest.md`, with the empty
    bodies `agentflow.wiki` itself defines (E-0710 — the directories alone left every
    reader unable to tell "nothing learned yet" from "pillar broken"). Idempotent
    (`exist_ok=True` + write-if-absent); never deletes or overwrites. On a first
    install with one project this materializes `projects/<name>/{...}` so the skills
    and the disk epic-survey find their homes.
    """
    sys.path.insert(0, str(opts.suite_root / SHARED_PKG_REL))
    from agentflow.config import load_environment  # noqa: E402  (post editable install)

    env = load_environment()
    projects_root = Path(env.data_root).expanduser() / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)

    config_projects = opts.config_home / "projects"
    if config_projects.is_dir():
        for md in sorted(config_projects.glob("*.md")):
            project_dir = projects_root / md.stem
            for sub in DATA_SUBTREE_DIRS:
                (project_dir / sub).mkdir(parents=True, exist_ok=True)
            _seed_registry(project_dir, md.stem, opts.suite_root)
            _seed_use_case_files(project_dir)
            _seed_wiki_pillar(project_dir, md.stem)


# --------------------------------------------------------------------------- #
# Skill + command deployment (step 7)
# --------------------------------------------------------------------------- #
def _deploy_mode_for(os_name: str) -> str:
    """Unix symlinks; Windows JUNCTIONS for directories, copies for files (gh#296).

    THE BUG THIS RETURN VALUE USED TO BE. It returned "copy" on Windows, and a copy is
    stale the instant a merge lands. Nothing anywhere re-deploys after a merge - not the
    loop skills, not CI, not the steward's charter - so between a skill-touching merge and
    a manual re-provision every seat in the fleet executes the PREVIOUS version of its own
    instructions. Measured on gh#296: E-0226 changed all seven loop SKILL.md and 2.5 hours
    later 7 of 7 deployed copies still matched the pre-merge commit, while the code half of
    the same epic was live. A seat and its tooling disagreed about what "stranded" meant
    for that whole window.

    WHY JUNCTIONS AND NOT SYMLINKS. A Windows symlink needs admin or Developer Mode, which
    is a property of the machine rather than of this program, so it cannot be the default.
    A directory junction needs neither. It is created unprivileged, it tracks the source
    live including new and nested files, and `Path.unlink()` removes it without touching
    what it points at.

    WHAT THIS DOES NOT COVER, STATED SO IT IS NOT MISTAKEN FOR COVERAGE. Junctions are
    DIRECTORY-ONLY. Of the 36 entries `_deploy_sources` yields, 22 are directories and 14
    are files - the 13 `commands/*.md` wrappers and `scripts/plan-lint.sh`. Those stay
    copies and stay stale after a merge. `_stale_copy_entries` names them in step 7 rather
    than leaving the residue silent.
    """
    return "symlink" if os_name != "nt" else "junction"


def _is_link(path: Path) -> bool:
    """True for a symlink OR a Windows junction - "something that points elsewhere".

    `Path.is_symlink()` is False for a junction on Python 3.11, and `Path.is_junction()`
    does not exist before 3.12. That single fact is the whole shape of the danger in this
    module: every ownership and refresh decision below used to branch on `is_symlink()`,
    so a junction fell through to the branch written for a real directory - which meant
    `shutil.rmtree` on a link, an ownership check looking for a marker that junction mode
    must never write, and a dangling junction read as "absent" and then re-created into a
    FileExistsError. `os.readlink` succeeds on both kinds and on a DANGLING one, which is
    exactly the case the re-point path has to see.
    """
    if path.is_symlink():
        return True
    try:
        os.readlink(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        # EINVAL (POSIX) and 4390 ERROR_NOT_A_REPARSE_POINT (Windows) are the real answer
        # "no, it is not a link". Anything else - a denied stat above all - is NOT an
        # answer, and returning False for it would send a genuine link to `shutil.rmtree`
        # and make `_is_agentflow_owned` call it FOREIGN, the one refusal --force cannot
        # clear.
        if exc.errno == errno.EINVAL or getattr(exc, "winerror", None) == 4390:
            return False
        raise
    return True


def _mode_blurb(mode: str) -> str:
    """The parenthetical beside the mode in step 7.

    It used to be `'Windows copies' if os.name == 'nt' else 'Unix symlinks'`, computed from
    the PLATFORM rather than from the mode - so it went on saying "Windows copies" no matter
    what `_deploy_mode` returned. This is the line an operator reads to confirm a deploy
    change landed, which makes a hardcoded answer the worst kind of wrong.
    """
    return {
        "symlink": "Unix symlinks, live",
        "junction": "Windows junctions for directories, live; files copied",
        "copy": "copies, STALE after any merge until re-provision",
    }.get(mode, mode)


def _stale_copy_entries(suite_root: Path, mode: str) -> list[str]:
    """Deploy keys that this mode CANNOT keep live, so they go stale on the next merge.

    Junctions are directory-only, so under JUNCTION mode a file source is copied and the 13
    `commands/*.md` wrappers and `scripts/plan-lint.sh` remain exactly as stale as gh#296
    described, while the 22 skill directories no longer do. Under SYMLINK mode there is no
    residue at all: `_deploy_entry` links files too. Saying "a file is a copy under every
    mode" is the restatement that already put a false claim in `_mode_mismatch` and a wrong
    assertion in the suite - the rule lives in `_deploy_entry` and is read from there.
    """
    return sorted(
        key
        for key, source in _deploy_sources(suite_root)
        if not (mode == "symlink" or (mode == "junction" and source.is_dir()))
    )


def _stale_copy_blurb(suite_root: Path, mode: str) -> str:
    """One line naming the residue, because a partial fix that reads as total is worse.

    gh#296 is closed for directories and NOT closed for files. Printing the count and the
    first names keeps that visible at the place someone looks to confirm the fix, instead
    of leaving a reader to infer full coverage from a mode that changed.
    """
    stale = _stale_copy_entries(suite_root, mode)
    if not stale:
        return "every entry tracks the checkout live; nothing goes stale on merge"
    shown = ", ".join(stale[:3])
    return (
        f"STILL COPIED, so still stale after a merge: {len(stale)} file entr"
        f"{'y' if len(stale) == 1 else 'ies'} ({shown}"
        f"{', ...' if len(stale) > 3 else ''}) - junctions are directory-only (gh#296)"
    )


def _deploy_mode() -> str:
    return _deploy_mode_for(os.name)


def _content_hash(path: Path) -> str:
    """sha256 of a file, or of a dir's sorted (relpath, bytes) stream.

    Ignores `__pycache__`/`.pyc` (excluded from copies too) and the ownership
    marker, so a copied target hashes identically to its source and "locally
    modified" is detectable as hash drift against the manifest record.
    """
    h = hashlib.sha256()
    if path.is_dir():
        for f in sorted(path.rglob("*")):
            rel = f.relative_to(path)
            if "__pycache__" in rel.parts or f.name.endswith(".pyc") or f.name == DEPLOY_MARKER_NAME:
                continue
            if f.is_file():
                h.update(rel.as_posix().encode("utf-8"))
                h.update(b"\0")
                h.update(f.read_bytes())
                h.update(b"\0")
    else:
        h.update(path.read_bytes())
    return h.hexdigest()


def _deploy_sources(suite_root: Path) -> list[tuple[str, Path]]:
    """(key, source) pairs to deploy; key is the claude_root-relative target.

    Three kinds of thing ship, and this docstring is the only place that says so —
    a docstring that under-reports what a deploy function deploys is how a file
    ends up unmanaged on the host (gh#110).

    - Skills: every DIRECTORY under skills/ except SKILLS_NON_DEPLOY — loose files
      at the skills/ top level are repo-side docs, not skills.
    - Commands: every commands/*.md wrapper.
    - Hook scripts: individual files under skills/_shared/scripts/, each deployed to a
      stable path so the host copy is never unmanaged (gh#110). Provision registers
      no hook for them; nothing runs one unless the operator adds it. `_shared` is
      excluded from the skills sweep, so without an entry here these have no deploy
      path at all and the host copy drifts unowned.
    """
    out: list[tuple[str, Path]] = []
    skills_dir = suite_root / "skills"
    if skills_dir.is_dir():
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir() and d.name not in SKILLS_NON_DEPLOY:
                out.append((f"skills/{d.name}", d))
    commands_dir = suite_root / "commands"
    if commands_dir.is_dir():
        for f in sorted(commands_dir.glob("*.md")):
            out.append((f"commands/{f.name}", f))
    # A stable key, so the host copy of plan-lint.sh is always the managed one (gh#110).
    # No hook is registered for it here or anywhere in provision. Guarded so a suite
    # checkout without the script still provisions.
    plan_lint = suite_root / "skills/_shared/scripts/plan-lint.sh"
    if plan_lint.is_file():
        out.append(("scripts/plan-lint.sh", plan_lint))
    return out


def _mode_mismatch(target: Path, source: Path, mode: str) -> bool:
    """True when *target* exists but is not the KIND *mode* would deploy today.

    `should_link` MIRRORS `_deploy_entry` rather than restating the rule. An earlier version
    of this said "a file entry is a copy under every mode" and returned False for every
    file - which is wrong for SYMLINK mode, where `target.symlink_to` covers files too. The
    cost of that restatement was a Unix box whose `commands/*.md` had been adopted as plain
    copies staying plain copies forever, while the manifest recorded "mode": "symlink" - the
    same record-disagrees-with-disk failure this branch exists to prevent, entering by the
    door the predicate left open. Two statements of one rule is one statement too many.
    """
    should_link = mode == "symlink" or (mode == "junction" and source.is_dir())
    return _is_link(target) != should_link


def _deploy_entry(source: Path, target: Path, mode: str) -> None:
    if mode == "symlink":
        target.symlink_to(source, target_is_directory=source.is_dir())
    elif mode == "junction" and source.is_dir():
        # ITS OWN BRANCH, ABOVE THE COPY, and the reason is plainer than it first looks.
        # It is NOT that a fall-through would write the marker through the junction into the
        # checkout: every call site reaches here with the target absent, and `copytree`
        # without `dirs_exist_ok` refuses an existing target anyway. It is simply that
        # junction mode must not silently DEGRADE to copy mode - which is the bug this epic
        # is fixing, and a fall-through is exactly how it would come back.
        import _winapi  # stdlib, Windows-only; imported here so this module still loads on Unix

        _winapi.CreateJunction(str(source), str(target))
    elif source.is_dir():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # ownership marker inside the COPY only — never written through a symlink
        (target / DEPLOY_MARKER_NAME).write_text(
            json.dumps({"deployed_from": str(source)}) + "\n", encoding="utf-8"
        )
    else:
        shutil.copy2(source, target)


def _remove_target(target: Path) -> None:
    # `_is_link`, not `is_symlink()`: a junction is neither a symlink nor a file, so it used
    # to reach `shutil.rmtree`, which REFUSES a reparse point outright
    # (OSError: Cannot call rmtree on a symbolic link). That refusal is 3.11 protecting the
    # source from being recursed into and deleted - correct, and a hard crash on every
    # --force and every re-point. `unlink()` removes the link and leaves the source alone;
    # verified with a canary file in the source.
    if _is_link(target) or target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)


def _matches_a_repo_version(suite_root: Path, source: Path, target: Path) -> bool:
    """True when `target`'s bytes are a version this checkout has held for `source`'s path.

    Asks git twice rather than hashing in Python, for two separate reasons that a single
    sentence tends to blur.

    WHY NOT hashlib: git names a blob over the content AFTER its filters run. With
    `core.autocrlf=true` - the Windows default, and Windows is where these copies live - a
    working-tree CRLF file and its stored LF blob have different ids, so a stdlib
    reimplementation looks right, passes on Linux, and answers "no match" to every real case
    on the platform that has the bug.

    WHY `--path` specifically: those filters are PATH-dependent. `.gitattributes` can set
    `-text` or a clean filter for one glob and not another, so the same bytes hash to
    different ids depending on the path they are hashed as. Hashing the deployed copy under
    its own temporary path would silently answer for the wrong ruleset.

    NEVER raises, and never gates the refusal it informs. A missing git, a suite_root that is
    not a checkout, a timeout or any other failure returns False - "cannot tell", which keeps
    the conservative wording. This only ever chooses WORDS: no refusal is softened away by it,
    and `--force` behaves identically either way.
    """
    def _git(*args: str) -> "Optional[str]":
        try:
            done = subprocess.run(
                ["git", "-C", str(suite_root), *args],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    try:
        rel = source.resolve().relative_to(suite_root.resolve()).as_posix()
    except (OSError, ValueError):
        return False
    hashed = _git("hash-object", "--path", rel, str(target))
    if not hashed:
        return False
    oid = hashed.strip()
    listed = _git("rev-list", "--all", "--objects", "--", rel)
    if listed is None:
        return False
    # `rev-list --objects` prints "<oid>" for a commit and "<oid> <path>" for each blob
    # reachable under the pathspec; only the named-path lines are versions of this file.
    return any(
        line.partition(" ")[0] == oid and line.partition(" ")[2] == rel
        for line in listed.splitlines()
    )


def _is_agentflow_owned(target: Path, key: str, manifest_entries: dict,
                        suite_root: Path, source: Path) -> bool:
    """Ownership = manifest record, symlink into the suite, in-dir marker, or a
    file byte-identical to its source (already ours in all but name)."""
    if key in manifest_entries:
        return True
    if _is_link(target):
        # Was `is_symlink()`, so a junction fell through to the marker check below and was
        # judged FOREIGN - because junction mode deliberately writes no marker. With an
        # empty or reset manifest that refuses every deployed skill as someone else's.
        return target.resolve().is_relative_to(suite_root.resolve())
    if target.is_dir():
        return (target / DEPLOY_MARKER_NAME).exists()
    return target.is_file() and target.read_bytes() == source.read_bytes()


def _refuse_undeployable(
    target: Path,
    key: str,
    source: Path,
    source_hash: str,
    recorded_entries: dict,
    opts: Options,
    manifest_path: Path,
) -> None:
    """Raise if *target* may not be written. PURE: it reads and never mutates.

    Both refusals live here, and every entry is checked BEFORE the first deploy, because a
    refusal raised from inside the deploy loop leaves the earlier entries already written
    and the manifest un-rewritten — a host tree that no longer matches its own record,
    produced by the guard doing its job correctly. gh#110 made that reachable: the hook
    script's key sorts last and is the one entry likely to collide with an unmanaged host
    file, so the abort landed after all 33 skills and commands had deployed.

    Same discipline as `agentflow.wiki.refresh_projection`, which checks the whole lane set
    ahead of its first write for the same reason: a half-applied refusal is worse than the
    state it refused, because it asserts freshness it does not have.
    """
    try:
        os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        # An lstat, not `exists() or is_symlink()`: a DANGLING junction answers False to
        # both of those, so a moved suite checkout read as "nothing is there" and the
        # deploy path below then tried to create over it - FileExistsError, WinError 183.
        #
        # NOT `os.path.lexists`, which swallows EVERY OSError including a denied stat.
        # This function's whole contract is that it refuses BEFORE the loop writes anything
        # (gh#110); a permission error read as "absent" hands the failure to `_deploy_entry`
        # instead, mid-loop, with earlier entries already written and the manifest not yet
        # rewritten. Only the two errnos that genuinely mean "nothing there" are answers.
        return
    if not _is_agentflow_owned(target, key, recorded_entries, opts.suite_root, source):
        kind = key.split("/", 1)[0].rstrip("s") or "entry"
        raise RuntimeError(
            f"deploy REFUSED: {target} already exists and was not deployed by agentflow\n"
            f"A foreign {kind} named '{target.name}' occupies this slot (no manifest entry\n"
            f"in {manifest_path}, no {DEPLOY_MARKER_NAME} marker). agentflow never\n"
            f"overwrites foreign entries — not even with --force. Move or rename it,\n"
            f"then re-run provision."
        )
    if _is_link(target) and not target.exists():
        return  # broken agentflow link or junction (suite moved): re-pointed, never refused
    current_hash = _content_hash(target)
    if current_hash != source_hash:
        recorded = recorded_entries.get(key, {}).get("sha256")
        if recorded is not None and current_hash != recorded and not opts.force:
            # Say WHICH of the two shapes this is. gh#328: the documented gh#296 remedy
            # was to hand-copy the repo over the deployed file, and that produces exactly
            # this state - while the old wording told the reader their local edits would
            # be LOST when there were none to lose, which is why a lane correctly refused
            # to --force for hours. A directory has no blob, and under junction mode
            # cannot drift at all, so only FILE entries are asked.
            if target.is_file() and _matches_a_repo_version(opts.suite_root, source, target):
                what = (
                    f"deploy REFUSED: {target} holds an EARLIER REPO VERSION, not local "
                    f"work (its bytes match a commit in this checkout's history). A "
                    f"hand-sync from the repo leaves exactly this - see gh#296 - so "
                    f"--force loses nothing here."
                )
            else:
                what = (
                    f"deploy REFUSED: {target} was locally modified after deployment "
                    f"(content no longer matches the deploy manifest record, and matches "
                    f"no commit in this checkout's history). Re-run with --force to "
                    f"refresh it from the suite (local edits will be LOST), or move your "
                    f"edits upstream into {source} first."
                )
            raise RuntimeError(
                what
                + " This aborts the ENTIRE deploy - every skill and command, not just "
                "this entry - and nothing was written."
            )


def _junction_capable(claude_root: Path) -> bool:
    """Can this VOLUME hold a junction? Proven by making one and removing it, not assumed.

    Junction mode is chosen from `os.name`, which says what the platform is and nothing
    about the filesystem the config dir lives on. A junction needs NTFS/ReFS and a local
    reparse-capable volume, so a redirected or UNC `CLAUDE_CONFIG_DIR`, or an exFAT stick,
    fails - and it fails inside `CreateJunction`, which is reached PER ENTRY, mid-loop,
    after earlier entries are already written and before the manifest is rewritten. That is
    the half-applied state `_refuse_undeployable` was built to make unreachable (gh#110),
    and a capability the pure pre-check structurally cannot answer: it can prove a target is
    writable BY POLICY, never that the write is POSSIBLE.

    So the answer is bought the only honest way, once, before the first entry. These
    filesystems worked under the old copy mode; falling back to copy keeps them working
    rather than trading their install for this fleet's freshness.
    """
    if os.name != "nt":
        return False
    probe_dir = claude_root / ".agentflow-junction-probe"
    link = claude_root / ".agentflow-junction-probe-link"
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(link):
            _remove_target(link)
        import _winapi

        _winapi.CreateJunction(str(probe_dir), str(link))
    except Exception:
        return False
    else:
        return True
    finally:
        for path in (link, probe_dir):
            try:
                if os.path.lexists(path):
                    _remove_target(path)
            except OSError:
                pass


def _do_deploy_skills(opts: Options) -> None:
    """Deploy everything `_deploy_sources` yields into claude_root; loud on collisions.

    That is skills/, commands/ and the hook scripts — see `_deploy_sources` for the
    authoritative list rather than trusting a second copy of it here.

    Per entry: absent target -> deploy. Present + foreign -> RuntimeError (never
    clobbered, not even with --force). Present + agentflow-owned: identical to
    source -> untouched no-op; drifted from source but matching the manifest's
    recorded hash (a suite update) -> refresh; drifted from the RECORDED hash
    (someone hand-edited the deployed copy) -> refuse unless --force. Finishes
    by rewriting the manifest (source, target, sha256 per entry).
    """
    claude_root = opts.claude_root
    mode = _deploy_mode()
    if mode == "junction":
        claude_root.mkdir(parents=True, exist_ok=True)
        if not _junction_capable(claude_root):
            # Loud, and BEFORE the first entry. A silent downgrade would leave the fleet
            # exactly as stale as gh#296 described while step 7 reported success.
            print(
                f"  provision: {claude_root} cannot hold a junction (not NTFS/ReFS, or a "
                f"redirected/UNC path); falling back to COPY mode.",
                file=sys.stderr,
            )
            print(
                "  Deployed skills will be STALE after every merge until you re-run "
                "provision - gh#296.",
                file=sys.stderr,
            )
            mode = "copy"
    manifest_path = claude_root / DEPLOY_MANIFEST_NAME
    recorded_entries: dict = {}
    if manifest_path.exists():
        recorded_entries = json.loads(manifest_path.read_text(encoding="utf-8")).get("entries", {})

    sources = _deploy_sources(opts.suite_root)
    source_hashes = {key: _content_hash(source) for key, source in sources}

    # REFUSE BEFORE WRITING ANYTHING (gh#110). Every entry is judged first, so a refusal
    # leaves the host exactly as it found it instead of half-deployed against a manifest
    # that was never rewritten. See `_refuse_undeployable` for why that state was reachable.
    for key, source in sources:
        _refuse_undeployable(
            claude_root / key, key, source, source_hashes[key],
            recorded_entries, opts, manifest_path,
        )

    (claude_root / "skills").mkdir(parents=True, exist_ok=True)
    (claude_root / "commands").mkdir(parents=True, exist_ok=True)

    new_entries: dict = {}
    for key, source in sources:
        target = claude_root / key
        # Every key gets its parent, not just the two literals above: a nested key
        # (scripts/plan-lint.sh) otherwise lands in a directory nobody created, and
        # hardcoding a third literal is what made the last one a special case.
        target.parent.mkdir(parents=True, exist_ok=True)
        source_hash = source_hashes[key]
        if not os.path.lexists(target):  # pre-checked above; lenient here is fine
            _deploy_entry(source, target, mode)
        elif _is_link(target) and not target.exists():
            _remove_target(target)  # broken agentflow link (suite moved): re-point
            _deploy_entry(source, target, mode)
        elif _mode_mismatch(target, source, mode):
            # MIGRATION, AND IT HAS TO BE ITS OWN BRANCH. Changing the mode alone converts
            # nothing: an existing copy whose source has not changed hashes EQUAL to that
            # source, so neither branch above fires and the entry stays a copy - while the
            # manifest below records "mode": the new one. That is a record disagreeing with
            # the disk it describes, which is the exact failure the refusal machinery in
            # `_refuse_undeployable` exists to prevent, arriving by a different door. The
            # check is on the TARGET'S KIND rather than on its bytes, because bytes are
            # what a link makes indistinguishable.
            _remove_target(target)
            _deploy_entry(source, target, mode)
        elif _content_hash(target) != source_hash:
            _remove_target(target)
            _deploy_entry(source, target, mode)
        new_entries[key] = {
            "source": str(source),
            "target": str(target),
            "sha256": source_hash,
        }

    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_root": str(opts.suite_root),
                "mode": mode,
                "entries": new_entries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Missed-wake watchdog registration (step 12) — THE ONE DAEMON
# --------------------------------------------------------------------------- #
# THE SINGLE-DAEMON BUDGET IS ONE, AND THIS SPENDS IT. Read this before adding a second
# scheduled thing anywhere in this installer, because the next author to want one will
# arrive here looking for the precedent and there isn't one.
#
# The suite was daemon-free by design until an account-wide limit proved that posture
# cannot detect its own worst failure. Three blackouts — 2026-09-08, 2026-09-16,
# 2026-09-19 — stopped all six lanes for days; each was reported by nothing and ended
# when a human noticed. The argument is structural, not a gap in the search: an in-fleet
# detector is itself an agent firing, an account-wide limit refuses EVERY model turn on
# the account including that detector's (measured: the scheduled fire landed on time and
# the refusal came 739 ms later, twice), so the only thing that survives is an invoker
# that takes no model turn — and every non-model invoker is a daemon.
#
# The ruling, 2026-09-22, verbatim: *"revoke the rule. Single daemon only is the new rule
# until it is challenged."* So: exactly one daemon, and it is this one — an OS-scheduled,
# report-only invocation of `python -m agentflow.wake_watch`. It reads the registry,
# prints a report and pushes ntfy. **It never relaunches a lane, never merges and never
# writes.** A second daemon is a new decision to take to the human, not something this step
# licenses by having gone first.
def _watchdog_registrable(os_name: str) -> bool:
    """Can THIS INSTALLER register the schedule, or must the operator?

    Windows only, and the asymmetry is about what can be registered unattended rather
    than about what the watchdog needs. `schtasks /Create /F` is one non-interactive call
    that replaces a same-named task. The Unix equivalent is editing a crontab that
    belongs to the user and may already hold their own lines; a provisioner that rewrites
    it is a provisioner that can silently drop somebody else's job. So on Unix the step
    SKIPS — and hands the operator the exact line instead, which is what
    `_watchdog_skip_note` exists for. A skip that printed nothing would make the whole
    epic true only on one platform while reading as shipped on both.
    """
    return os_name == "nt"


def _watchdog_applies(opts: Options) -> bool:
    """The step's gate: the operator opted in (`--wake-watch`) AND this platform can be
    registered unattended. Opt-in first, because off-by-default is the rule (gh#544) and a
    default run must register nothing on any platform.

    A named function rather than a lambda over `os.name` because `os.name` is not patchable
    in a test — `pathlib` reads it to choose its flavour, so faking it swaps every `Path` in
    the process for one that cannot be instantiated. The platform question is therefore
    asked through `_watchdog_registrable`, which a test can substitute without touching the
    interpreter's idea of what OS it is on."""
    return opts.wake_watch and _watchdog_registrable(os.name)


def _watchdog_dir(opts: Options) -> Path:
    """`<config_home>/../watchdog` — the wrapper script and its log live here.

    Derived from `config_home` (itself derived from `$AGENTFLOW_CONFIG` or the home dir),
    so a literal home path or machine name appears nowhere in this file or in anything it
    writes. A `--config-home` override moves the watchdog with the config it watches.
    """
    return opts.config_home.parent / WATCHDOG_DIR_NAME


def _watchdog_projects(opts: Options) -> list[str]:
    """Configured project names, read WITHOUT importing agentflow.

    `--dry-run` prints the plan (including this step's skip note) without importing the
    package, touching a venv or running a subprocess, and the cron line a Unix operator
    needs names the project. So the names come from the config dir's own filenames — the
    same `projects/*.md` glob step 11 scaffolds from — rather than from the resolver.
    """
    return _configured_projects(opts)


def _configured_projects(opts: Options) -> list[str]:
    """Project names from `<config_home>/projects/*.md`, the glob steps 10 and 11 iterate."""
    projects = opts.config_home / "projects"
    if not projects.is_dir():
        return []
    return [md.stem for md in sorted(projects.glob("*.md"))]


def _watchdog_task_name(project: str) -> str:
    return f"{WATCHDOG_TASK_PREFIX}-{project}"


def _watchdog_log(opts: Options, project: str) -> Path:
    return _watchdog_dir(opts) / f"wake-watch-{project}.log"


def _watchdog_wrapper(opts: Options, project: str) -> Path:
    return _watchdog_dir(opts) / f"wake-watch-{project}.cmd"


def _watchdog_command(opts: Options, project: str) -> str:
    """The exact command line the scheduler runs. Identical on both platforms.

    THE REDIRECT IS NOT DECORATION. `wake_watch`'s exit code says whether the WATCHER
    could do its job, never what it found — 0 covers "findings, pushed" and "nothing
    found" alike, and a misconfigured ntfy channel warns on stderr and still exits 0. A
    job wired to the exit code alone therefore reports permanent health over a fleet
    nobody is watching, which is the exact failure this epic exists to remove. The report
    is written to stdout BEFORE any push is attempted, so capturing both streams keeps the
    findings even when delivery fails.

    The venv interpreter is named in full: bare `python` lacks the agentflow package, and
    a scheduled task inherits neither the login profile's activation nor its PATH.
    """
    return (
        f'"{_venv_python(opts)}" -m agentflow.wake_watch --project {project}'
        f' >> "{_watchdog_log(opts, project)}" 2>&1'
    )


def _watchdog_cron_line(opts: Options, project: str) -> str:
    """The crontab line for a non-Windows box — what the skip note hands the operator."""
    return f"0 */{WATCHDOG_INTERVAL_HOURS} * * * {_watchdog_command(opts, project)}"


def _watchdog_wrapper_body(opts: Options, project: str) -> str:
    """The `.cmd` the Task Scheduler entry points at.

    A wrapper rather than a bare `/TR` string, for two reasons. Redirection needs a shell,
    and `schtasks` has no shell; and `/TR` is one quoted argument, so a command carrying a
    quoted interpreter path, a quoted log path and `2>&1` has to survive two levels of
    quoting intact. Pointing the task at one file path removes that whole class of
    breakage — and it is the shape already proven on this host, where an existing task
    runs a `.cmd` the same way.

    It also gives the next reader somewhere to land: whoever finds this task in the
    scheduler and asks what it is gets the answer in the file the task names.
    """
    task = _watchdog_task_name(project)
    return (
        "@echo off\n"
        "REM agentflow missed-wake watchdog -- THE SUITE'S ONE DAEMON.\n"
        "REM Written by install/provision.py (step 12). Do not hand-edit: re-running\n"
        "REM provision rewrites this file.\n"
        "REM\n"
        "REM It reads the project registry from outside the fleet, prints a report and\n"
        "REM pushes ntfy when a lane is past its own declared eta. It is REPORT-ONLY: it\n"
        "REM never relaunches a lane, never merges and never writes. That half of the\n"
        "REM rule did not change when the daemon-free invariant was revoked on\n"
        "REM 2026-09-22 -- what changed is who may invoke it, never what it may do.\n"
        "REM\n"
        "REM The single-daemon budget is one and this spends it. A second daemon is a\n"
        "REM new decision, not a precedent this one sets.\n"
        "REM\n"
        f"REM Remove with:  schtasks /Delete /TN {task} /F\n"
        "REM See docs/INSTALL.md section 13 (Stopping, and undoing the install).\n"
        f"{_watchdog_command(opts, project)}\n"
    )


def _watchdog_skip_note(opts: Options) -> str:
    """Why step 12 did not register anything, in the words the operator needs.

    Not opted in (the default): say so and name the flag, and say what a skip does NOT
    do — an earlier opted-in run's task stays registered until the operator removes it.

    Opted in off Windows: the exact crontab line, INSTEAD of a silent skip. "A documented
    step is true only for users who read it" is the finding this answers: the installer's
    own transcript is the one place a non-Windows operator is guaranteed to look, so the
    line goes there rather than only in docs/INSTALL.md.
    """
    if not opts.wake_watch:
        return (
            "off by default - pass --wake-watch to register the missed-wake watchdog. "
            "This removes nothing an earlier run registered; docs/INSTALL.md section 13 "
            "says how to remove it"
        )
    projects = _watchdog_projects(opts) or ["<project>"]
    lines = [
        "no Windows Task Scheduler here - REGISTER IT BY HAND; this skip is not a no-op.",
        "         Without it nothing outside the fleet reports a stalled board. First create "
        "the directory the redirect below writes into - off Windows nothing else does, and a "
        "cron job whose redirect directory is missing never runs the command at all:",
        f'             mkdir -p "{_watchdog_dir(opts)}"',
        "         then run `crontab -e` and add:",
    ]
    lines += [f"             {_watchdog_cron_line(opts, p)}" for p in projects]
    lines += [
        "         then `crontab -l` to confirm. Report-only: it pushes ntfy and exits, and "
        "never relaunches a lane.",
        "         This is the suite's ONE daemon and that budget is now spent. "
        "docs/INSTALL.md section 13 says how to remove it.",
    ]
    return "\n".join(lines)


def _do_register_watchdog(opts: Options) -> None:
    """Register one scheduled task per configured project, idempotently.

    IDEMPOTENCE IS `/F`, and it is the whole reason the task name is deterministic.
    `schtasks /Create /F` replaces a task of the same name instead of failing or adding a
    second, so a re-run of provision converges on exactly one entry per project and also
    REPAIRS one whose paths went stale — a moved checkout changes the interpreter path
    inside the wrapper, and a watchdog pointed at a deleted venv is a watchdog that
    reports nothing while still appearing in the scheduler.

    Runs AFTER step 11 deliberately: the registry the watchdog reads is what step 11
    seeds, and registering a schedule against a board that does not exist yet would put a
    failing job on the machine as the install's last act.
    """
    if not _watchdog_applies(opts):
        raise RuntimeError(
            "watchdog registration reached without --wake-watch or on a non-Windows "
            "platform; it should have been skipped with its note. This is a bug in "
            "build_steps, not something to work around."
        )
    projects = _watchdog_projects(opts)
    if not projects:
        # Not a failure: a config with no projects is a legal (if useless) install, and
        # step 11 scaffolds nothing either. Loud rather than silent, because "no watchdog"
        # and "watchdog registered" must not look the same in the transcript.
        print(
            f"       ! no projects/*.md under {opts.config_home / 'projects'} - "
            "NOTHING was registered. Add a project and re-run provision, or register by "
            "hand per docs/INSTALL.md section 13."
        )
        return
    _watchdog_dir(opts).mkdir(parents=True, exist_ok=True)
    for project in projects:
        wrapper = _watchdog_wrapper(opts, project)
        wrapper.write_text(_watchdog_wrapper_body(opts, project), encoding="utf-8")
        task = _watchdog_task_name(project)
        _run_cmd([
            "schtasks", "/Create", "/F",
            "/TN", task,
            "/SC", "HOURLY",
            "/MO", str(WATCHDOG_INTERVAL_HOURS),
            "/TR", str(wrapper),
        ])
        print(f"       - {task}  every {WATCHDOG_INTERVAL_HOURS}h  ->  {wrapper}")


# --------------------------------------------------------------------------- #
# Ordered step list
# --------------------------------------------------------------------------- #
# Says what the flag does NOT do as well: an operator who opts out on a re-run still has
# whatever an earlier run wrote, and would otherwise believe the hooks were gone.
AUTO_ALLOW_SKIP_NOTE = (
    "--no-auto-allow: PreToolUse auto-allow hooks not installed. This removes nothing an "
    "earlier run wrote; to remove existing hooks see docs/INSTALL.md section 13"
)


def build_steps(opts: Options) -> list[Step]:
    ad_on = opts.autodoc
    config_env = _config_home_env(opts.config_home)
    return [
        Step(
            1, "Preflight: verify manifest runtimes",
            [
                "Python 3.11+ (datetime.UTC)",
                "Git present  (+ optional gh - absence non-fatal)",
                ("Node + npm present" if ad_on else "Node + npm - autodoc only, skipped"),
                f"settings file exists: {opts.settings_file}  (read by steps 8 + 9; "
                "missing, or not a BOM-free UTF-8 JSON object = refuse before anything "
                "is written; checked on a real run, not by --dry-run)",
            ],
            run=_do_preflight,
        ),
        Step(
            2, "Config presence: environment.md exists + resolves",
            [f"{opts.config_home / 'environment.md'}",
             "written by the install/SKILL.md interview; broken config = loud hard-fail",
             "pre-creates data_root (parents ok) so the step-10 resolver finds it"],
            run=_do_config_presence,
        ),
        Step(
            3, "Secrets: one-way into settings.local.json / .env",
            ["single source of truth per fact, never written into markdown",
             "the interview owns the write; this step confirms named vars resolve",
             "missing NAMES fail loudly; values are never read or printed"],
            run=_do_secrets,
        ),
        Step(
            4, "Python venv + PyYAML (only pipeline dep)",
            [f"python -m venv {opts.suite_root / '.venv'}",
             "pip install pyyaml>=6.0"],
            run=_do_python_venv,
        ),
        Step(
            5, "Editable install: pip install -e skills/_shared",
            [f"pip install -e {opts.suite_root / SHARED_PKG_REL}  (the agentflow package)"],
            run=_do_editable_install,
        ),
        Step(
            6, "Autodoc runtimes: npm ci + graph package (autodoc ONLY)",
            [f"npm ci in the Node tree-sitter inventory package at {AUTODOC_PKG_REL}",
             f"pip install -e {GRAPH_PKG_REL} into the suite venv "
             "(autodoc_graph: schema, sqlite-vec backend, embedder, `graph` CLI)"],
            # NOT `ad_on`: that was captured when this list was built, before step 5
            # re-checked against the resolved config. A captured bool cannot see a
            # correction made after capture, which would have made the re-check inert.
            applies_fn=lambda o: o.autodoc,
            skip_note_fn=lambda o: f"autodoc disabled - {o.autodoc_reason}",
            run=_do_autodoc_runtime,
        ),
        Step(
            7, "Deploy skills + commands + hook scripts into the Claude config dir",
            [f"skills/<name>/ -> {opts.claude_root / 'skills'}{os.sep}  (_shared excluded: pip package)",
             f"commands/*.md -> {opts.claude_root / 'commands'}{os.sep}",
             f"skills/_shared/scripts/plan-lint.sh -> {opts.claude_root / 'scripts'}{os.sep}"
             "  (stable managed path; provision registers no hook for it)",
             f"mode: {_deploy_mode()} ({_mode_blurb(_deploy_mode())});"
             f" manifest: {opts.claude_root / DEPLOY_MANIFEST_NAME}",
             f"live entries point INTO {opts.suite_root} - moving or deleting that checkout"
             f" re-points or dangles every seat's instructions at once",
             _stale_copy_blurb(opts.suite_root, _deploy_mode()),
             "foreign name collision = loud refusal; --force refreshes agentflow-owned entries"],
            run=_do_deploy_skills,
        ),
        Step(
            8, "bootstrap.py (once): install PreToolUse auto-allow hooks",
            [f"python {opts.suite_root / BOOTSTRAP_REL}",
             f"honors CLAUDE_SETTINGS_FILE ({opts.settings_file})"],
            applies=opts.auto_allow,
            skip_note=AUTO_ALLOW_SKIP_NOTE,
            run=_do_bootstrap,
        ),
        Step(
            9, "Install SessionStart config-injection hook",
            [f"register '{opts.suite_root / SESSIONSTART_HOOK_REL}'",
             f"in {opts.settings_file} (idempotent)",
             "hook command runs the suite .venv python (quoted paths)",
             (f"env.AGENTFLOW_CONFIG={config_env} beside it "
              "(non-default config home; a conflicting value refuses)"
              if config_env is not None
              else "default config home: no env.AGENTFLOW_CONFIG written")],
            run=_do_sessionstart_hook,
        ),
        Step(
            10, "Presence-check delegated SKILL.md paths + hard-fail on missing config",
            ["run the resolver over environment.md + every projects/<name>.md",
             "a missing required field or delegated path is a loud ConfigError"],
            run=_do_presence_checks,
        ),
        Step(
            11, "Scaffold data_root subtree: projects/<name>/{design/*,status,wiki/pages{,/done,/archived}}",
            ["mkdir <data_root>/projects/ + the eight DATA_SUBTREE_DIRS per configured project (queue/ retired; autodoc/ is not scaffolded)",
             "seed status/registry.md from templates/registry.md (<project> placeholder replaced)",
             "seed design/use-cases/ with registry.jsonl (empty), .counter (0), domains.md (tree stub)",
             "seed wiki/hot.md + wiki/digest.md from agentflow.wiki.initial_pillar_files "
             "(the pillar's own empty bodies, so a reader can tell empty from broken)",
             "seed an empty wiki/residue-baseline.json dated today when the pillar has no "
             "pages yet, so the residue check measures from the first wake",
             "idempotent (exist_ok + write-if-absent); never deletes or overwrites"],
            run=_do_scaffold_data_root,
        ),
        Step(
            12, "Register the missed-wake watchdog with the OS scheduler (THE ONE DAEMON, OPT-IN)",
            ["opt-in: runs only with --wake-watch (off by default, gh#544)",
             f"one Task Scheduler entry per configured project: "
             f"'{_watchdog_task_name('<project>')}', every {WATCHDOG_INTERVAL_HOURS}h",
             f"runs: {_watchdog_command(opts, '<project>')}",
             f"via a wrapper .cmd under {_watchdog_dir(opts)} - schtasks has no shell, and "
             "BOTH streams are captured because wake_watch's exit code says whether the "
             "WATCHER could run, never what it found",
             "idempotent: `schtasks /Create /F` replaces the same-named task, so a re-run "
             "converges on one entry per project and repairs stale paths - it never adds a second",
             "report-only: it pushes ntfy and exits. It NEVER relaunches a lane, never "
             "merges and never writes",
             "THE SINGLE-DAEMON BUDGET IS ONE AND THIS SPENDS IT (2026-09-22, "
             "revoking the daemon-free invariant). A second daemon is a new decision, not a "
             "precedent this step sets",
             f"remove: schtasks /Delete /TN {_watchdog_task_name('<project>')} /F "
             "(docs/INSTALL.md section 13)"],
            # LAZY, like step 6, and for a second reason on top of step 6's: the skip note
            # has to name the configured projects, and on a fresh machine those arrive with
            # step 2's config and step 11's scaffold - both after this list is built.
            applies_fn=_watchdog_applies,
            skip_note_fn=_watchdog_skip_note,
            run=_do_register_watchdog,
        ),
    ]


# --------------------------------------------------------------------------- #
# Rendering + execution
# --------------------------------------------------------------------------- #
def print_plan(opts: Options, steps: list[Step]) -> None:
    print("agentflow provision - DRY RUN (no changes will be made)")
    print(f"  suite_root  : {opts.suite_root}")
    print(f"  config_home : {opts.config_home}")
    print(f"  settings    : {opts.settings_file}")
    print(f"  autodoc     : {'enabled' if opts.autodoc else 'disabled'} ({opts.autodoc_reason})")
    if opts.autodoc_explicit and opts.autodoc:
        # A dry run never imports the resolver, so it cannot confirm the flag itself.
        print("                --autodoc is confirmed against the resolved config at step 5, "
              "and refused there if no autodoc_root resolves")
    print(f"  auto-allow  : {'enabled' if opts.auto_allow else 'disabled (--no-auto-allow)'}")
    print()
    print("Ordered install steps:")
    for s in steps:
        status = "" if s.is_applicable(opts) else "  [SKIPPED]"
        print(f"  {s.n}. {s.title}{status}")
        if s.is_applicable(opts):
            for d in s.detail:
                print(f"       - {d}")
        else:
            print(f"       - {s.skip_reason(opts)}")


def execute(opts: Options, steps: list[Step]) -> None:
    print("agentflow provision - applying")
    print(f"  suite_root  : {opts.suite_root}")
    print(f"  config_home : {opts.config_home}")
    print(f"  settings    : {opts.settings_file}")
    print(f"  autodoc     : {'enabled' if opts.autodoc else 'disabled'} ({opts.autodoc_reason})")
    print(f"  auto-allow  : {'enabled' if opts.auto_allow else 'disabled (--no-auto-allow)'}")
    for s in steps:
        if not s.is_applicable(opts):
            print(f"  {s.n}. {s.title} - SKIPPED ({s.skip_reason(opts)})")
            continue
        print(f"  {s.n}. {s.title} ...")
        if s.run is not None:
            s.run(opts)  # fail loudly — no try/except swallow
        print(f"  {s.n}. done.")


def _launch_dir(settings_file: Path) -> Optional[Path]:
    """The directory whose sessions load *settings_file*, or None when no directory does.

    Claude Code reads `<dir>/.claude/settings.local.json` as the PROJECT settings of a
    session opened in `<dir>`, and of no other session. So the launch directory is the
    parent of the settings file's `.claude` directory. A settings file outside a `.claude`
    directory is project settings for nobody, and naming a directory for it would be false.
    """
    settings_file = settings_file.expanduser().resolve()
    if settings_file.parent.name != ".claude":
        return None
    return settings_file.parent.parent


def _print_launch_block(settings_file: Path) -> None:
    """The closing LAUNCHING A LANE advice, naming the directory provision actually wrote for."""
    settings_file = settings_file.expanduser().resolve()
    launch_dir = _launch_dir(settings_file)
    print("\nLAUNCHING A LANE - the working directory is part of the command:")
    if launch_dir is None:
        print(f"  The hooks went into {settings_file}, which is not inside a `.claude`")
        print("  directory, so no Claude Code session loads it as project settings and there is")
        print("  no launch directory to name. Point --settings-file at <dir>/.claude/settings.local.json")
        print("  and re-run; <dir> is then the launch directory.")
        return
    shown = f'"{launch_dir}"' if any(ch.isspace() for ch in str(launch_dir)) else str(launch_dir)
    print(f"      cd {shown} && claude          # then: /lead-dev --project <name>")
    print("  Launch EVERY lane from this one launch directory, never a project repo and never")
    print(f"  a per-epic worktree. Provision wrote the fleet's hooks into {settings_file},")
    print("  the PROJECT settings file for a session opened in that directory and for no other.")
    print("  A lane opened anywhere else loads none of those hooks: no config injection, no")
    print("  auto-allow. Claude Code also keys the CLAUDE.md chain it reads and the history it")
    print("  has accumulated to the folder a session opens in, so that lane comes up bound to")
    print("  its own folder's instructions with none of the fleet's context.")


def _print_config_home_note(opts: Options) -> None:
    """Say where the hook reads its config from, as the settings file holds it now (gh#689).

    Read back from the file rather than derived from opts: a resumed run, a skipped step or
    a hand edit can all leave the file different from what this run meant to write, and the
    closing output must not claim a write that did not happen.
    """
    settings_file = opts.settings_file.expanduser().resolve()
    config_env = _config_home_env(opts.config_home)
    try:
        env = json.loads(settings_file.read_text(encoding="utf-8")).get("env")
    except (OSError, ValueError, AttributeError) as exc:
        if config_env is not None:
            print(f"  Could not read back env.AGENTFLOW_CONFIG from {settings_file} "
                  f"({type(exc).__name__}); lanes need it set to {config_env}.")
        return
    held = env.get("AGENTFLOW_CONFIG") if isinstance(env, dict) else None
    if held is None:
        if config_env is not None:
            print(f"  WARNING: {settings_file} carries no env.AGENTFLOW_CONFIG, so a lane")
            print(f"  launched there reads the default config home, not {config_env}.")
            print("  Re-run provision without --resume-from so step 9 writes it.")
        return
    print(f"  {settings_file} carries env.AGENTFLOW_CONFIG={held}, beside the hook: a lane")
    print("  launched there reads that config home with nothing set in its shell. Keep the two")
    print("  together: moving the config home means updating that value, or re-running provision.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="agentflow multi-runtime provisioner")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the ordered install steps without mutating anything")
    autodoc_grp = parser.add_mutually_exclusive_group()
    autodoc_grp.add_argument("--autodoc", dest="autodoc", action="store_true", default=None,
                             help="force-enable the autodoc Node runtime step")
    autodoc_grp.add_argument("--no-autodoc", dest="autodoc", action="store_false",
                             help="force-disable the autodoc Node runtime step")
    wake_watch_grp = parser.add_mutually_exclusive_group()
    wake_watch_grp.add_argument("--wake-watch", dest="wake_watch", action="store_true",
                                default=False,
                                help="opt in to step 12: register the report-only missed-wake "
                                     "watchdog with the OS scheduler (off by default)")
    wake_watch_grp.add_argument("--no-wake-watch", dest="wake_watch", action="store_false",
                                help="skip step 12 (the default); removes nothing an earlier "
                                     "run registered (see docs/INSTALL.md section 13)")
    parser.add_argument("--no-auto-allow", dest="auto_allow", action="store_false",
                        help="skip step 8: do not install the PreToolUse auto-allow hooks "
                             "(removes nothing an earlier run wrote; see docs/INSTALL.md section 13)")
    parser.add_argument("--suite-root", default=None, help="agentflow install root (default: repo root)")
    parser.add_argument("--config-home", default=None,
                        help="config home (default: $AGENTFLOW_CONFIG or ~/.agentflow/config)")
    parser.add_argument("--settings-file", default=None,
                        help="Claude settings file (default: $CLAUDE_SETTINGS_FILE or ~/.claude/settings.local.json)")
    parser.add_argument("--claude-root", default=None,
                        help="Claude config dir for skill/command deployment "
                             "(default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--resume-from", dest="resume_from", type=int, default=None,
                        help=argparse.SUPPRESS)   # internal: set by the venv handover
    parser.add_argument("--force", action="store_true",
                        help="refresh agentflow-owned deployed skills/commands even if "
                             "locally modified (foreign entries are still never overwritten)")
    args = parser.parse_args(argv)
    # What the child must inherit. Captured from the SAME value argparse consumed, so a
    # programmatic caller's flags reach the child exactly as an operator's do.
    global _INVOKED_ARGV
    _INVOKED_ARGV = list(argv) if argv is not None else list(sys.argv[1:])

    suite_root = _suite_root(args.suite_root)
    config_home = _config_home(args.config_home)
    autodoc, reason = _resolve_autodoc(args.autodoc, config_home)
    opts = Options(
        dry_run=args.dry_run,
        suite_root=suite_root,
        config_home=config_home,
        settings_file=_settings_file(args.settings_file),
        autodoc=autodoc,
        autodoc_reason=reason,
        autodoc_explicit=args.autodoc is not None,
        claude_root=_claude_root(args.claude_root),
        force=args.force,
        auto_allow=args.auto_allow,
        wake_watch=args.wake_watch,
    )

    steps = build_steps(opts)

    if opts.dry_run:
        print_plan(opts, steps)
        return 0

    if args.resume_from is not None:
        valid = {s.n for s in steps}
        if args.resume_from not in valid:
            # An out-of-range resume ran ZERO steps and exited 0 under the full success
            # banner - an install that did nothing and said it succeeded. The flag is
            # internal, so this is a guard against a future caller rather than an operator.
            sys.stderr.write(
                "\nagentflow provision FAILED:\n"
                f"--resume-from {args.resume_from} names no step; "
                f"this run has {sorted(valid)}\n"
            )
            return 1
        # The venv handover. Steps below the resume point already ran in the parent; they are
        # idempotent, but re-running them would reprint lines the reader has already seen, and
        # an installer whose output does not match its own step numbering is unfollowable.
        steps = [s for s in steps if s.n >= args.resume_from]

    try:
        execute(opts, steps)
    except _HandOff as h:
        # Not a failure: the rest of the run happened in the child, which printed its own
        # steps and its own banner. Its exit code is the install's exit code.
        return h.code
    except (RuntimeError, OSError) as e:
        sys.stderr.write(f"\nagentflow provision FAILED:\n{e}\n")
        return 1
    except ImportError as e:
        # A MISSING DEPENDENCY IS A FAILURE, NOT A CRASH. `ModuleNotFoundError` is neither
        # `RuntimeError` nor `OSError`, so it escaped the handler above as a raw traceback --
        # which is exactly how gh#186 presented: an installer that dies without telling an
        # operator what to do. The handover should make this unreachable on the ordinary
        # fresh-clone path; it stays because "unreachable" is a claim about today's call
        # sites, and this is the last line before a user sees a stack trace.
        sys.stderr.write(
            f"\nagentflow provision FAILED:\n{e}\n"
            "A later step needed a dependency this interpreter does not have, and the step-4 "
            "handover did not cover it. Two ways that happens, and they want opposite fixes: "
            f"the suite venv at {opts.suite_root / '.venv'} is missing the dependency (delete "
            "the venv and re-run, which rebuilds it), or the failing step runs BEFORE step 4 "
            "and so precedes the handover entirely - that is a bug in provision.py, not "
            "something to work around.\n"
        )
        return 1
    _print_verification_readiness(_verification_readiness(opts))
    # The fleet skills call bare `python -m agentflow.registry` (and other agentflow.* modules)
    # with the SESSION'S python; the suite venv lives at <suite_root>/.venv. Make that venv the
    # box's default python ONCE (login profile) so every session inherits it — not a per-launch step.
    venv_py = _venv_python(opts)
    if os.name == "nt":
        act_cmd = f"& {venv_py.parent / 'Activate.ps1'}"
        profile = "your PowerShell profile ($PROFILE)"
    else:
        act_cmd = f"source {venv_py.parent / 'activate'}"
        profile = "your login profile (~/.bashrc / ~/.zshrc)"
    print("\nNEXT - make this venv the box's DEFAULT python (one-time, not per launch):")
    print(f"  add to {profile}:")
    print(f"      {act_cmd}")
    print("  the fleet skills call bare `python -m agentflow.registry`, so every Claude Code")
    print("  session must inherit this venv. This DISPLACES the box's previous default python:")
    print("  for every Python product you build here, set canonical_repo.python in its profile")
    print("  to the product's own interpreter, or its test suite runs against this venv.")
    print("  Then, in a FRESH shell, verify:")
    print("      python -m agentflow.registry validate --project <name>   # exit 0 + 'registry valid'")
    _print_launch_block(opts.settings_file)
    _print_config_home_note(opts)
    print("  Select the project with --project <name> ON THE SLASH COMMAND; it beats the")
    print("  session default, so one launch directory serves every fleet on the box.")
    print("  DO NOT launch with `claude --settings <file>`: a --settings env block DISPLACES")
    print("  the settings env rather than merging with it, so a partial file leaves the fleet's")
    print("  other variables absent and every loop dies at resolution unable to write even its")
    print("  own status row - which makes the failure invisible on the board.")

    # LAST STEP, deliberately: an install that reports success while the surface it
    # just built does not resolve is the failure this whole diagnostic exists for.
    # Advisory rather than fatal - a fresh install legitimately has no project yet,
    # and failing the installer on that would make `doctor` the thing people skip.
    print("")
    print("Resolved-surface check (python -m agentflow.doctor):")
    try:
        from agentflow._stdio import utf8_stdio
        from agentflow.doctor import diagnose, examined_a_project, render, unexamined_verdict

        # Same forcing as `python -m agentflow.doctor` (gh#416): on a cp1252 console one
        # character outside the code page raised here, the except below swallowed it, and
        # the install ended with no report at all (gh#524). One shared helper since
        # gh#574; a stream without reconfigure (a test capture) is left alone.
        utf8_stdio()
        _print_doctor_report(opts, diagnose, render, examined_a_project, unexamined_verdict)
    except Exception as exc:  # noqa: BLE001 - never fail an install on the diagnostic
        print(f"  (doctor unavailable: {type(exc).__name__}: {exc})")
    return 0


def _print_doctor_report(opts: Options, diagnose, render, examined_a_project,
                         unexamined_verdict) -> None:
    """Run doctor once per configured project, and state the verdict over all of them.

    A bare `diagnose()` lets the resolver pick the project, and with two or more profiles
    it picks none: the report then covers environment and paths only, while `render()`'s
    verdict line, which is computed from the findings it was handed and knows nothing of
    scope, still reads "resolved surface healthy" (gh#693). So this loop owns the verdict
    across projects, and each `render()` block is headed with the project it covers.
    """
    projects = _configured_projects(opts)
    if not projects:
        findings = diagnose()
        report = render(findings)
        if not examined_a_project(findings):
            report = unexamined_verdict(report, opts.config_home / "projects")
        print(report)
        return

    failing, warned = [], []
    for name in projects:
        print(f"-- project {name} --")
        try:
            findings = diagnose(name)
        except Exception as exc:  # noqa: BLE001 - one project's crash must not hide the rest
            # `diagnose` is documented never to raise; if it does, that project is
            # reported as failing with the error, and the remaining projects still run.
            print(f"FAIL doctor raised for {name}: {type(exc).__name__}: {exc}")
            print("")
            failing.append(name)
            continue
        print(render(findings))
        if any(f.failed for f in findings):
            failing.append(name)
        elif any(f.warned for f in findings):
            warned.append(name)
    checked = f"checked: {', '.join(projects)}"
    if failing:
        print(f"resolved surface NOT healthy: {len(failing)} of {len(projects)} configured "
              f"project(s) failing: {', '.join(failing)} ({checked})")
    elif warned:
        print(f"resolved surface healthy for all {len(projects)} configured project(s), but NOT "
              f"everywhere as configured - see the warning(s) for {', '.join(warned)} ({checked})")
    else:
        print(f"resolved surface healthy for all {len(projects)} configured project(s) ({checked})")


if __name__ == "__main__":
    raise SystemExit(main())
