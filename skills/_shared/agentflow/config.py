"""Typed-YAML config resolver for the agentflow suite.

Validates the two config surfaces defined in ``_shared/SCHEMA.md``:

- ``environment.md`` — per-machine facts (paths, notify, git host, locks).
- ``projects/<name>.md`` — per-product facts (repo, deploy, verification, vocab).

Contract (SCHEMA §5): resolve ``$AGENTFLOW_CONFIG`` (or ``~/.agentflow/config/``),
extract the FIRST fenced ```` ```yaml ```` block of the target markdown file, then
validate presence / type / enum / R* conditionals / path existence / ``owner/repo``
shape / ``name==filename`` / env-var-name presence / delegated presence-check.

HARD-FAIL LOUD, NEVER PARTIAL: on the FIRST unmet requirement raise ``ConfigError``.
Never returns a config with an unresolved token; never substitutes a default for a
required field; optional gaps resolve to the declared default / ``None``.

Secrets: reads only env-var NAMES from markdown; never a secret VALUE (spec §4).
Deps: PyYAML only.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import yaml

_LOG = logging.getLogger(__name__)

#: The harness clamps every ``ScheduleWakeup(delaySeconds=...)`` into this range and
#: reports nothing when it does. A loop that asks for more sleeps LESS than it wrote
#: down, so its state file records an interval that never happened - the machine is
#: fine and the written record is false, which is the harder failure to notice.
WAKE_DELAY_MIN_S = 60
WAKE_DELAY_MAX_S = 3600

DEFAULT_MTIME_CUTOFF = "2026-04-29T00:00:00+00:00"
DEFAULT_QUESTION_STYLE = "mobile-two-turn"
#: Where inside a vault the mirror writes, when nothing overrides it. ONE definition:
#: the dataclass default, the config reader and the resolver all read this name, because
#: three copies of a default is three places a change has to be remembered - and a
#: mutation of any one of them leaves the other two answering, so nothing can test it.
DEFAULT_VAULT_SUBDIR = "wiki/epics"
#: The cadence a publish target has when the profile does not say otherwise. Publishing
#: is a human action taken on purpose, so the default describes that and nothing else —
#: there is no schedule here for anything to read as an automation trigger.
DEFAULT_PUBLISH_CADENCE = "manual, en masse"
_YAML_BLOCK = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)
_OWNER_REPO = re.compile(r"^[^/\s]+/[^/\s]+$")
_VERIFICATION_ENUM = {"web-journey", "test-suite", "smoke-cli", "none"}
_QUESTION_STYLE_ENUM = {"inline", "mobile-two-turn"}


# --------------------------------------------------------------------------- #
# Error
# --------------------------------------------------------------------------- #
class ConfigError(Exception):
    """Raised on the FIRST unmet config requirement — loud, never swallowed."""

    def __init__(self, field_path: str, file, reason: str, remediation: str):
        self.field_path = field_path
        self.file = str(file)
        self.reason = reason
        self.remediation = remediation
        super().__init__(
            f"{self.file}: [{field_path}] {reason}\n  remediation: {remediation}"
        )


# --------------------------------------------------------------------------- #
# Dataclasses — Environment
# --------------------------------------------------------------------------- #
@dataclass
class Person:
    name: str


@dataclass
class GitHost:
    kind: str
    cli: str


@dataclass
class Notify:
    kind: str
    url_env: Optional[str] = None
    topic_env: Optional[str] = None
    ascii_only: bool = True


@dataclass
class Vault:
    root: str
    #: Where inside the vault the mirror writes: `<root>/<subdir>/<project>/<slug>/<kind>.md`.
    #: Defaulted to the value `mirror-epic.sh` hardcodes today, so an existing install that
    #: never sets it keeps writing exactly where it already writes.
    subdir: str = DEFAULT_VAULT_SUBDIR
    save_skill: Optional[str] = None
    autodoc_update_skill: Optional[str] = None
    mirror_helper: Optional[str] = None
    hot_cache: Optional[str] = None


@dataclass
class ParallelBuilders:
    enabled: bool = False
    count: int = 1


@dataclass
class Environment:
    schema_version: int
    person: Person
    data_root: str
    git_host: GitHost
    notify: Notify
    lock_dir: str
    shell: str
    suite_root: str
    worktree_root: str
    autodoc_root: Optional[str] = None
    mtime_cutoff_iso: str = DEFAULT_MTIME_CUTOFF
    question_style: str = DEFAULT_QUESTION_STYLE
    editor_cmd: str = "${EDITOR:-code}"
    status_board_url_template: Optional[str] = None
    vault: Optional[Vault] = None
    #: The `vault` key was PRESENT and empty - the interview asked and the answer was no.
    #: Distinct from the key being absent, which means nobody has been asked yet. Both
    #: resolve to no vault, so nothing about mirroring changes; what changes is that a
    #: reader can tell a decision from an unfinished install.
    vault_declined: bool = False
    parallel_builders: ParallelBuilders = field(default_factory=ParallelBuilders)


# --------------------------------------------------------------------------- #
# Dataclasses — Project
# --------------------------------------------------------------------------- #
@dataclass
class CanonicalRepo:
    host_ref: str
    local_checkout: str
    base_branch: str = "main"
    python: Optional[str] = None  # the PRODUCT's interpreter; never the suite venv's


@dataclass
class PublishRepo:
    """The PUBLIC repo a project is published to — deliberately, occasionally, by a human.

    READ-ONLY to every builder. No loop pushes to it, opens a PR against it, or grounds
    against it; the rule is stated once, next to the key, in ``skills/_shared/SCHEMA.md``
    §2. It is a *separate dataclass* from ``CanonicalRepo`` on purpose: it carries no
    ``local_checkout`` and no ``base_branch``, so there is nothing here a builder could
    mistake for a working tree or a merge base.

    Absent is the normal case and the correct encoding of "there is no public repo yet" —
    never synthesise a default. See ``docs/runbooks/publish.md`` for the human procedure.
    """

    host_ref: str
    cadence: str = DEFAULT_PUBLISH_CADENCE


@dataclass
class Staging:
    url: Optional[str] = None
    deploy_host_ssh: Optional[str] = None
    checkout_path: Optional[str] = None
    deploy_recipe: Optional[str] = None


@dataclass
class Verification:
    strategy: str
    url: Optional[str] = None
    roles: list = field(default_factory=list)
    test_login: Optional[str] = None


@dataclass
class WorkSource:
    kind: str = "filesystem"
    bug_triage: bool = False
    #: How many open issues one issue-intake read may answer for; past it the intake readers
    #: refuse (``issue_intake._refuse_past_the_page``). A profile key and never a CLI flag: a
    #: seat must not be able to step past that guard on its own.
    open_issue_page: int = 200


@dataclass
class GenerationSkills:
    polishing: Optional[str] = None
    provision: Optional[str] = None


@dataclass
class Communities:
    """The community escape hatch (SCHEMA §2, epic 703). Whole block optional.
    ``pins`` maps a repo-relative path prefix to a named community — the
    deliberately weak override for a repo where the derived partition is
    genuinely wrong. No catch-all (an empty/root prefix hard-fails), never
    required, unlisted paths stay derived."""

    pins: dict = field(default_factory=dict)


@dataclass
class Audit:
    """The auditor loop's per-project dial (SCHEMA §2). Whole block optional; every
    field defaulted. Empty ``roles`` ⇒ the auditor reuses ``verification.roles`` (a
    consumer-side Forced fallback, not applied here)."""

    window: int = 7
    roles: list = field(default_factory=list)
    idle_wake_minutes: int = 60
    notify: bool = True


@dataclass
class Project:
    schema_version: int
    name: str
    canonical_repo: CanonicalRepo
    verification: Verification
    deprecated_mirror: Optional[str] = None
    deprecated_mirror_checkout: Optional[str] = None
    #: The public repo this project is published to, by hand. NOT a git remote any loop
    #: may use - see PublishRepo. Absent (the normal case) means nothing is published.
    publish_repo: Optional[PublishRepo] = None
    staging: Optional[Staging] = None
    domains: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    harness_notes: Optional[str] = None
    excluded_domains: list = field(default_factory=list)  # never auto-dispatched unless the epic carries build_approved_by:
    work_source: WorkSource = field(default_factory=WorkSource)
    generation_skills: GenerationSkills = field(default_factory=GenerationSkills)
    audit: Audit = field(default_factory=Audit)
    communities: Optional[Communities] = None
    # Per-project overrides of the machine-level defaults. A two-project fleet sharing one
    # environment-level autodoc_root has every loop binding the SAME code-wiki, so one
    # project's autodoc pass writes into the other's tree - measured live on this box.
    autodoc_root: Optional[str] = None
    vault: Optional[Vault] = None
    #: As on `Environment`: an explicitly empty `vault` key, meaning asked-and-declined.
    vault_declined: bool = False


# --------------------------------------------------------------------------- #
# Unknown-key rejection — the allowlist is DERIVED, never transcribed
# --------------------------------------------------------------------------- #
# A hand-maintained set of legal keys is a second copy of the schema, and the two
# drift the moment someone adds a field and forgets the list. So the reader IS the
# allowlist: every primitive validator below records the key it read, and after a
# block is built, any key nobody read is unknown by definition. Add a field and it
# is legal automatically; delete its reader and the field goes loud, which is the
# direction you want the failure to point.
#
# WHY THIS EXISTS AT ALL: a typo'd or renamed key is silently ignored today, so the
# file says one thing and the resolver does another. Measured on this machine's own
# config, one project file declares a `verification.url` that nothing has ever read
# (see the cross-check further down) — the exact shape this rejects.
_consumed: dict = {}
_consumed_keep: list = []      # holds block refs so id() cannot be recycled mid-parse
_opaque: dict = {}             # (id(block), key) whose VALUE is data, not schema


def _note(block, key, *, opaque: bool = False) -> None:
    """Record that *key* of *block* was read. Called by every validator below."""
    if not isinstance(block, dict):
        return
    _consumed.setdefault(id(block), set()).add(key)
    _consumed_keep.append(block)
    if opaque:
        _opaque[(id(block), key)] = True


def _start_recording() -> None:
    _consumed.clear()
    _consumed_keep.clear()
    _opaque.clear()


def _optional_block(data: dict, key: str):
    """`data[key]` when the optional block is present, else None — and note the key.

    The gate itself is a read. Without this, a block written as `vault:` with an
    empty body is never touched by any validator and would be rejected as unknown.
    """
    _note(data, key)
    return data.get(key)


def _reject_unknown_keys(data: dict, file, prefix: str = "") -> None:
    """Raise on the FIRST key in *data* that no validator read.

    Walks only into blocks that were themselves read as schema. An OPAQUE value —
    `communities.pins`, whose keys are user-chosen path prefixes — is recorded and
    not descended into, because rejecting a pin for not being a schema key would be
    exactly backwards.
    """
    seen = _consumed.get(id(data), set())
    for key in data:
        path = f"{prefix}{key}"
        if key not in seen:
            raise ConfigError(
                path, file,
                f"unknown key '{path}' — nothing in the schema reads it",
                "remove the key, or fix the spelling; a key the resolver never reads "
                "makes the file disagree with the running config silently",
            )
        value = data[key]
        if isinstance(value, dict) and not _opaque.get((id(data), key)):
            _reject_unknown_keys(value, file, f"{path}.")


#: Every key the schema reads, per block path ("" = a file's top level), environment and
#: project unioned. A key not read YET is not an unknown key: the builders read in order and
#: stop at the first failure, so at the moment a required key is found missing, legitimate
#: keys further down (`autodoc_root`, `worktree_root`) are still unread - and suggesting one
#: as a misspelling would tell the operator to rename a correct key. Only keys outside this
#: set may be suggested. Unioning the two files over-excludes a little, which costs at most
#: a missed hint and never a wrong one. Kept equal to the builders' own reads by a test that
#: derives the same map from this module's source, so a new reader cannot drift past it.
_SCHEMA_KEYS: dict = {
    "": frozenset({
        "aliases", "audit", "autodoc_root", "canonical_repo", "communities", "data_root",
        "deprecated_mirror", "deprecated_mirror_checkout", "domains", "editor_cmd",
        "excluded_domains", "generation_skills", "git_host", "harness_notes", "lock_dir",
        "mtime_cutoff_iso", "name", "notify", "parallel_builders", "person", "publish_repo",
        "question_style", "schema_version", "shell", "staging", "status_board_url_template",
        "suite_root", "vault", "verification", "work_source", "worktree_root",
    }),
    "audit": frozenset({"idle_wake_minutes", "notify", "roles", "window"}),
    "canonical_repo": frozenset({"base_branch", "host_ref", "local_checkout", "python"}),
    "communities": frozenset({"pins"}),
    "generation_skills": frozenset({"polishing", "provision"}),
    "git_host": frozenset({"cli", "kind"}),
    "notify": frozenset({"ascii_only", "kind", "topic_env", "url_env"}),
    "parallel_builders": frozenset({"count", "enabled"}),
    "person": frozenset({"name"}),
    "publish_repo": frozenset({"cadence", "host_ref"}),
    "staging": frozenset({"checkout_path", "deploy_host_ssh", "deploy_recipe", "url"}),
    "vault": frozenset({
        "autodoc_update_skill", "hot_cache", "mirror_helper", "root", "save_skill", "subdir",
    }),
    "verification": frozenset({"roles", "strategy", "test_login", "url"}),
    "work_source": frozenset({"bug_triage", "kind", "open_issue_page"}),
}


def _near_miss(data, key: str, block: str) -> Optional[str]:
    """The UNKNOWN key in *data* that looks like a misspelling of *key*, or None.

    WHY: `_reject_unknown_keys` runs only after every required check passes, so a typo'd
    REQUIRED key was reported as missing, the operator added it as advised, and only the
    next run named the stray spelling (gh#882). Looking among the block's unknown keys at
    the moment the required one is found missing names both in one run. *block* is the
    block's path in `_SCHEMA_KEYS`; a schema key is never a candidate, read yet or not.
    Only an ABSENT key gets a hint: one present but null was spelled right.
    """
    if not isinstance(data, dict) or key in data:
        return None
    known = _SCHEMA_KEYS.get(block, frozenset())
    strays = [k for k in data if isinstance(k, str) and k not in known]
    match = difflib.get_close_matches(key, strays, n=1)
    return match[0] if match else None


def _missing_key(data, key: str, fp: str, file, reason: str, remediation: str) -> ConfigError:
    """The ConfigError for a missing required *key*, naming a near-miss stray when there is one.

    One builder for every missing-key raise, so every block (environment, project,
    verification, ...) gets the hint and none drifts back to the bare message.
    """
    stray = _near_miss(data, key, fp.rpartition(".")[0])
    if stray is None:
        return ConfigError(fp, file, reason, remediation)
    return ConfigError(
        fp, file,
        f"{reason}; found '{stray}' - a misspelling?",
        f"rename '{stray}:' to '{key}:', or {remediation}",
    )


# --------------------------------------------------------------------------- #
# Primitive validators (each raises ConfigError on the FIRST failure)
# --------------------------------------------------------------------------- #
def _require_block(data: dict, key: str, file) -> dict:
    _note(data, key)
    if not isinstance(data, dict) or data.get(key) is None:
        raise _missing_key(data, key, key, file, f"required block '{key}' is missing", f"add the '{key}:' block")
    block = data[key]
    if not isinstance(block, dict):
        raise ConfigError(key, file, f"'{key}' must be a mapping", f"make '{key}' a yaml block")
    return block


def _require_int(data: dict, key: str, file, fp: Optional[str] = None) -> int:
    fp = fp or key
    _note(data, key)
    if key not in data or data[key] is None:
        raise _missing_key(data, key, fp, file, f"required key '{key}' is missing", f"add '{key}:' (int)")
    v = data[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(fp, file, f"expected int, got {type(v).__name__}", f"set {fp} to an integer")
    return v


def _require_str(data: dict, key: str, file, fp: Optional[str] = None) -> str:
    fp = fp or key
    _note(data, key)
    if key not in data or data[key] is None:
        raise _missing_key(data, key, fp, file, f"required key '{key}' is missing", f"add '{key}:' (string)")
    v = data[key]
    if not isinstance(v, str):
        raise ConfigError(fp, file, f"expected string, got {type(v).__name__}", f"set {fp} to a string")
    return v


def _require_enum(data: dict, key: str, choices: set, file, fp: Optional[str] = None) -> str:
    fp = fp or key
    v = _require_str(data, key, file, fp)
    if v not in choices:
        raise ConfigError(fp, file, f"'{v}' not in {sorted(choices)}", f"set {fp} to one of {sorted(choices)}")
    return v


def _opt_bool(data: dict, key: str, file, default: bool, fp: Optional[str] = None) -> bool:
    fp = fp or key
    _note(data, key)
    if key not in data or data[key] is None:
        return default
    v = data[key]
    if not isinstance(v, bool):
        raise ConfigError(fp, file, f"expected bool, got {type(v).__name__}", f"set {fp} to true/false")
    return v


def _opt_int(data: dict, key: str, file, default: int, fp: Optional[str] = None) -> int:
    fp = fp or key
    _note(data, key)
    if key not in data or data[key] is None:
        return default
    v = data[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(fp, file, f"expected int, got {type(v).__name__}", f"set {fp} to an integer")
    return v


def _opt_str(data: dict, key: str, file, default=None, fp: Optional[str] = None):
    fp = fp or key
    _note(data, key)
    if key not in data or data[key] is None:
        return default
    v = data[key]
    if not isinstance(v, str):
        raise ConfigError(fp, file, f"expected string, got {type(v).__name__}", f"set {fp} to a string")
    return v


def _opt_list(data: dict, key: str, file, fp: Optional[str] = None) -> list:
    fp = fp or key
    _note(data, key)
    if key not in data or data[key] is None:
        return []
    v = data[key]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(fp, file, "expected a list of strings", f"make {fp} a yaml list of strings")
    return v


def _require_owner_repo(block: dict, key: str, file, fp: str) -> str:
    """A required `owner/repo` host ref.

    Two blocks now carry one — `canonical_repo` and `publish_repo` — and a typo in
    either is a repo name, so both get the same check and the same message. Kept as a
    helper rather than two inline copies because the second copy is where the error
    text starts to drift from the first.
    """
    value = _require_str(block, key, file, fp)
    if not _OWNER_REPO.match(value):
        raise ConfigError(fp, file, f"'{value}' is not owner/repo", "use the form owner/repo")
    return value


def _require_dir(value: str, fp: str, file) -> str:
    if not value.strip():
        # An empty string is a str, so it clears _require_str, and Path("") is Path(".") -
        # which exists and is a directory, so every later check passes too. The field then
        # silently resolves to whatever directory the process happened to start in. For
        # worktree_root that is the exact failure this suite exists to prevent: a lane
        # building somewhere nobody chose. Reject it here, where every path field benefits.
        raise ConfigError(fp, file, f"'{fp}' is empty", f"set {fp} to a real directory path")
    p = Path(value).expanduser()
    if not p.exists():
        raise ConfigError(fp, file, f"required path does not exist: {p}", f"create the directory or fix {fp}")
    if not p.is_dir():
        raise ConfigError(fp, file, f"path is not a directory: {p}", f"point {fp} at a directory")
    return str(p)


def _check_path_if_set(value, fp: str, file, *, must_be_dir=False) -> Optional[str]:
    if value is None:
        return None
    p = Path(value).expanduser()
    if not p.exists():
        raise ConfigError(fp, file, f"configured path does not exist: {p}", f"create it or set {fp}: null")
    if must_be_dir and not p.is_dir():
        raise ConfigError(fp, file, f"path is not a directory: {p}", f"point {fp} at a directory")
    return str(p)


@dataclass(frozen=True)
class PendingSkillDeploy:
    """Skills a caller is about to deploy into ``<claude_root>/skills/`` (gh#1047).

    Only the installer passes this. A profile may point a delegated field at a SKILL.md
    under the Claude config dir that the installer itself deploys, and the installer loads
    the profile before that deploy runs, so the strict presence check failed every fresh
    machine. With this, a missing ``<claude_root>/skills/<name>/<rel>`` is accepted when
    *name* is in *shipped* and ``<suite_root>/skills/<name>/<rel>`` is a file. The
    installer's post-deploy load passes nothing and gets the strict check back.
    """

    claude_root: Path
    suite_root: Path
    shipped: frozenset = frozenset()


def _pending_deploy_source(p: Path, pending: Optional[PendingSkillDeploy]) -> Optional[Path]:
    """The suite file that will be deployed to *p*, or None when *p* is not one."""
    if pending is None:
        return None
    shipped = {os.path.normcase(n) for n in pending.shipped}
    claude_root = Path(pending.claude_root).expanduser()
    # Lexical first, so a stale link under skills/ (a moved suite checkout) cannot steer
    # the comparison; resolved second, so a symlinked Claude dir spelled two ways matches.
    # Both collapse `..`, so a path that climbs out of skills/ never lines up.
    for norm in (lambda q: Path(os.path.abspath(q)), lambda q: q.resolve()):
        skills, target = norm(claude_root / "skills"), norm(p)
        n = len(skills.parts)
        if len(target.parts) < n + 2:   # <name>/<file> at the least; a bare dir never ships
            continue
        if [os.path.normcase(x) for x in target.parts[:n]] != [os.path.normcase(x) for x in skills.parts]:
            continue
        rel = target.parts[n:]
        if os.path.normcase(rel[0]) not in shipped:
            continue
        source = Path(pending.suite_root).expanduser() / "skills" / Path(*rel)
        if source.is_file():
            return source
    return None


def _check_skill_path_if_set(value, fp: str, file, *, executable=False,
                             pending: Optional[PendingSkillDeploy] = None) -> Optional[str]:
    """Delegated SKILL.md / script presence-check — a configured-but-missing path is loud.

    *pending* (installer only, gh#1047): a missing path the caller is about to deploy
    passes, checked against its suite source instead.
    """
    # Blank means unset. Path("") is Path("."), which exists, so a blank value would pass
    # the check below and resolve to the process's cwd - printed as `= .` in the resolved
    # block and followed as if it were a skill. No delegated path is ever a bare directory.
    if value is None or not value.strip():
        return None
    p = Path(value).expanduser()
    checked = p
    if not p.exists():
        source = _pending_deploy_source(p, pending)
        if source is None:
            raise ConfigError(fp, file, f"delegated path missing: {p}", f"install the SKILL.md/script or set {fp}: null")
        checked = source
    if executable and not os.access(str(checked), os.X_OK):
        raise ConfigError(fp, file, f"delegated script not executable: {checked}", f"make {checked} executable or set {fp}: null")
    return str(p)


def _require_env_name(name, fp: str, file) -> str:
    """Presence-check the NAMED env var. Reads the name only — never the secret value."""
    if not isinstance(name, str):
        raise ConfigError(fp, file, f"expected env-var NAME (string), got {type(name).__name__}", f"set {fp} to an env var name")
    if name not in os.environ:  # membership test — does not read the value
        raise ConfigError(fp, file, f"named env var '{name}' absent from os.environ", f"export {name} (value stays in .env/settings.local.json)")
    return name


# --------------------------------------------------------------------------- #
# First-yaml-block loader
# --------------------------------------------------------------------------- #
def _load_first_yaml_block(file: Path) -> dict:
    if not file.exists():
        raise ConfigError("<file>", file, f"config file not found: {file}", "run the agentflow installer or create the config file")
    text = file.read_text(encoding="utf-8")
    m = _YAML_BLOCK.search(text)
    if not m:
        raise ConfigError("<yaml-block>", file, "no fenced ```yaml block found", "add a ```yaml ... ``` block with the typed config")
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise ConfigError("<yaml-block>", file, f"invalid YAML: {e}", "fix the YAML syntax in the first fenced block")
    if not isinstance(data, dict):
        raise ConfigError("<yaml-block>", file, "top-level yaml is not a mapping", "the yaml block must be key: value pairs")
    return data


def config_home(config_home: "Optional[Path | str]" = None) -> Path:
    """Config root: *config_home* if given, else ``$AGENTFLOW_CONFIG`` (expanded), else
    ``~/.agentflow/config``.

    Public so every caller — the SessionStart hook included — resolves the root
    HERE. A second copy of these four lines is how the hook and the resolver drift.

    *config_home* lets an in-process caller that serves a home other than the process's
    (the installer, given ``--config-home``) say so per call. Pinning the variable
    instead is process-global: two overlapping calls each restore the other's value.
    """
    if config_home is not None:
        return Path(config_home).expanduser()
    override = os.environ.get("AGENTFLOW_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agentflow" / "config"


_config_home = config_home  # legacy private alias — existing internal callers unchanged


# --------------------------------------------------------------------------- #
# Environment builder
# --------------------------------------------------------------------------- #
def _build_environment(data: dict, file: Path, *,
                       pending: Optional[PendingSkillDeploy] = None) -> Environment:
    _start_recording()
    sv = _require_int(data, "schema_version", file)
    if sv != 1:
        raise ConfigError("schema_version", file, f"unsupported schema_version {sv}", "set schema_version: 1")

    person_b = _require_block(data, "person", file)
    person = Person(
        name=_require_str(person_b, "name", file, "person.name"),
    )

    data_root = _require_dir(_require_str(data, "data_root", file), "data_root", file)
    autodoc_root = _check_path_if_set(_opt_str(data, "autodoc_root", file), "autodoc_root", file, must_be_dir=True)

    mtime_cutoff = _opt_str(data, "mtime_cutoff_iso", file, DEFAULT_MTIME_CUTOFF)
    try:
        datetime.fromisoformat(mtime_cutoff)
    except ValueError:
        raise ConfigError("mtime_cutoff_iso", file, f"not ISO-8601: {mtime_cutoff!r}", "use e.g. 2026-04-29T00:00:00+00:00")

    question_style = _opt_str(data, "question_style", file, DEFAULT_QUESTION_STYLE)
    if question_style not in _QUESTION_STYLE_ENUM:
        raise ConfigError("question_style", file, f"'{question_style}' not in {sorted(_QUESTION_STYLE_ENUM)}", f"set question_style to one of {sorted(_QUESTION_STYLE_ENUM)}")

    gh_b = _require_block(data, "git_host", file)
    gh_kind = _require_enum(gh_b, "kind", {"github", "gitea", "gitlab", "local"}, file, "git_host.kind")
    gh_cli = _opt_str(gh_b, "cli", file, None, "git_host.cli")
    if gh_cli is None:
        gh_cli = "gh" if gh_kind == "github" else "none"
    elif gh_cli not in {"gh", "glab", "none"}:
        raise ConfigError("git_host.cli", file, f"'{gh_cli}' not in ['gh', 'glab', 'none']", "set git_host.cli to gh|glab|none")
    git_host = GitHost(kind=gh_kind, cli=gh_cli)

    nt_b = _require_block(data, "notify", file)
    nt_kind = _require_enum(nt_b, "kind", {"ntfy", "none"}, file, "notify.kind")
    url_env = topic_env = None
    if nt_kind != "none":  # R* conditional
        url_env = _require_env_name(_require_str(nt_b, "url_env", file, "notify.url_env"), "notify.url_env", file)
        topic_env = _require_env_name(_require_str(nt_b, "topic_env", file, "notify.topic_env"), "notify.topic_env", file)
    notify = Notify(
        kind=nt_kind,
        url_env=url_env,
        topic_env=topic_env,
        ascii_only=_opt_bool(nt_b, "ascii_only", file, True, "notify.ascii_only"),
    )

    lock_dir = _require_dir(_require_str(data, "lock_dir", file), "lock_dir", file)
    shell = _require_enum(data, "shell", {"posix", "powershell"}, file)
    editor_cmd = _opt_str(data, "editor_cmd", file, "${EDITOR:-code}")
    suite_root = _require_dir(_require_str(data, "suite_root", file), "suite_root", file)
    # Required, never defaulted: a silently-defaulted worktree path is how a lane ends up
    # building in a directory nobody chose. The launch dir is the harness home; where the
    # work happens is config, not cwd.
    if "worktree_root" not in data or data["worktree_root"] is None:
        # Field-specific remediation rather than the generic "add 'worktree_root:' (string)".
        # This is the ONE error an existing downstream install hits on upgrade, so a
        # remediation that omits an example value costs every one of them a second round
        # trip when they guess a path that does not exist yet.
        raise _missing_key(
            data,
            "worktree_root",
            "worktree_root",
            file,
            "required key 'worktree_root' is missing",
            "add 'worktree_root: ~/worktrees' - an EXISTING directory under which each "
            "lane's per-epic worktree is created as <worktree_root>/<project>/<epic-branch> "
            "(the claim branch name, e<NNNN>-<short>; <epic-slug> in a hand run); create it first, then re-run. "
            "See SCHEMA.md.",
        )
    worktree_root = _require_dir(_require_str(data, "worktree_root", file), "worktree_root", file)
    status_tpl = _opt_str(data, "status_board_url_template", file, None)

    vault = None
    # `in` rather than `.get(...) is None`: those two answers are the whole point. A key
    # that is present and empty is a recorded "no"; an absent one is a question nobody
    # asked. `_optional_block` below reads the key either way, so the schema gate is
    # satisfied and an explicit null is not an unknown key.
    vault_declined_flag = "vault" in data and data.get("vault") is None
    if _optional_block(data, "vault") is not None:
        vb = _require_block(data, "vault", file)
        vault = Vault(
            root=_require_dir(_require_str(vb, "root", file, "vault.root"), "vault.root", file),  # R* iff block present
            subdir=_opt_str(vb, "subdir", file) or DEFAULT_VAULT_SUBDIR,
            save_skill=_check_skill_path_if_set(_opt_str(vb, "save_skill", file), "vault.save_skill", file, pending=pending),
            autodoc_update_skill=_check_skill_path_if_set(_opt_str(vb, "autodoc_update_skill", file), "vault.autodoc_update_skill", file, pending=pending),
            mirror_helper=_check_skill_path_if_set(_opt_str(vb, "mirror_helper", file), "vault.mirror_helper", file, executable=True, pending=pending),
            hot_cache=_check_path_if_set(_opt_str(vb, "hot_cache", file), "vault.hot_cache", file),
        )

    pb = ParallelBuilders()
    if _optional_block(data, "parallel_builders") is not None:
        pbb = _require_block(data, "parallel_builders", file)
        enabled = _opt_bool(pbb, "enabled", file, False, "parallel_builders.enabled")
        count = _opt_int(pbb, "count", file, 1, "parallel_builders.count")
        if count not in {0, 1, 2, 3}:
            raise ConfigError("parallel_builders.count", file, f"count {count} not in {{0,1,2,3}}", "set parallel_builders.count to 0, 1, 2 or 3")
        if count > 1 and not enabled:
            raise ConfigError("parallel_builders.count", file, "count > 1 requires enabled: true", "set parallel_builders.enabled: true")
        pb = ParallelBuilders(enabled=enabled, count=count)

    built_env = Environment(
        schema_version=sv,
        person=person,
        data_root=data_root,
        git_host=git_host,
        notify=notify,
        lock_dir=lock_dir,
        shell=shell,
        suite_root=suite_root,
        worktree_root=worktree_root,
        autodoc_root=autodoc_root,
        mtime_cutoff_iso=mtime_cutoff,
        question_style=question_style,
        editor_cmd=editor_cmd,
        status_board_url_template=status_tpl,
        vault=vault,
        vault_declined=vault_declined_flag,
        parallel_builders=pb,
    )
    # AFTER the constructor, never before it: the constructor's own arguments are
    # readers, so a check placed above the return sees a half-filled consumed-set and
    # rejects live keys. Measured on this machine: `aliases` is read inside Project(...).
    _reject_unknown_keys(data, file)
    return built_env


# --------------------------------------------------------------------------- #
# Project builder
# --------------------------------------------------------------------------- #
def _clamped_idle_wake(minutes: int, file) -> int:
    """Return *minutes* clamped to what ``ScheduleWakeup`` will actually grant.

    Clamping here rather than rejecting is deliberate. The value is a user dial with a
    harmless intent - "check less often" - and hard-failing a project at step 0 over it
    would take a whole fleet down for a preference. But leaving it unclamped is worse
    than either: the loop writes ``next wake in 120m`` into its state file, the harness
    grants 60, and nobody can see the disagreement because the clamp is silent. So the
    resolved config carries the number that will really happen, and the WARNING says
    which number was asked for.
    """
    ceiling = WAKE_DELAY_MAX_S // 60
    floor = WAKE_DELAY_MIN_S // 60 or 1
    if minutes > ceiling:
        _LOG.warning(
            "audit.idle_wake_minutes=%d exceeds the %d-minute ScheduleWakeup ceiling; "
            "using %d (%s)", minutes, ceiling, ceiling, file
        )
        return ceiling
    if minutes < floor:
        _LOG.warning(
            "audit.idle_wake_minutes=%d is below the %d-minute ScheduleWakeup floor; "
            "using %d (%s)", minutes, floor, floor, file
        )
        return floor
    return minutes

def _build_project(data: dict, file: Path, name: str, *,
                   pending: Optional[PendingSkillDeploy] = None) -> Project:
    _start_recording()
    sv = _require_int(data, "schema_version", file)
    if sv != 1:
        raise ConfigError("schema_version", file, f"unsupported schema_version {sv}", "set schema_version: 1")

    decl_name = _require_str(data, "name", file)
    if decl_name != name:  # name == filename stem (routing integrity)
        raise ConfigError("name", file, f"name '{decl_name}' != filename stem '{name}'", f"rename the file to {decl_name}.md or set name: {name}")

    cr_b = _require_block(data, "canonical_repo", file)
    host_ref = _require_owner_repo(cr_b, "host_ref", file, "canonical_repo.host_ref")
    canonical = CanonicalRepo(
        host_ref=host_ref,
        local_checkout=_require_dir(_require_str(cr_b, "local_checkout", file, "canonical_repo.local_checkout"), "canonical_repo.local_checkout", file),
        base_branch=_opt_str(cr_b, "base_branch", file, "main", "canonical_repo.base_branch"),
        python=_check_path_if_set(_opt_str(cr_b, "python", file, None, "canonical_repo.python"), "canonical_repo.python", file, must_be_dir=False),
    )

    deprecated_mirror = _opt_str(data, "deprecated_mirror", file)
    deprecated_mirror_checkout = _check_path_if_set(_opt_str(data, "deprecated_mirror_checkout", file), "deprecated_mirror_checkout", file, must_be_dir=True)

    # The publish target. Absent stays absent - no default is synthesised, because a
    # synthesised publish target is a public repo nobody chose. `host_ref` gets the SAME
    # _OWNER_REPO check canonical_repo gets: a typo here is a repo name, and the one place
    # it would surface is a human about to push a private history somewhere public.
    # A key written with an EMPTY body is an error here, not a silent absence. Every
    # other optional block in this file lets `key:` with no children mean "absent",
    # and for `vault:` that is deliberate (it encodes an explicit decline). This key
    # exists to close the silent-swallow class, so "I wrote publish_repo and nothing
    # happened" is the one outcome it must never produce.
    publish_repo = None
    if "publish_repo" in data and _optional_block(data, "publish_repo") is None:
        raise ConfigError(
            "publish_repo", file, "'publish_repo:' is present but empty",
            "give it a host_ref, or delete the key — absent means nothing is published",
        )
    if _optional_block(data, "publish_repo") is not None:
        pr_b = _require_block(data, "publish_repo", file)
        pub_host_ref = _require_owner_repo(pr_b, "host_ref", file, "publish_repo.host_ref")
        cadence = _opt_str(pr_b, "cadence", file, DEFAULT_PUBLISH_CADENCE, "publish_repo.cadence")
        if not cadence.strip():
            # `_opt_str` returns the default only for absent/None, so `cadence: ""`
            # survives as an empty string and prints "(cadence: )" into the resolved
            # block — dropping the one sentence that says publishing is a human act.
            raise ConfigError(
                "publish_repo.cadence", file, "publish_repo.cadence is empty",
                f"describe the human act, or drop the key to get '{DEFAULT_PUBLISH_CADENCE}'",
            )
        publish_repo = PublishRepo(host_ref=pub_host_ref, cadence=cadence)

    proj_autodoc_root = _check_path_if_set(
        _opt_str(data, "autodoc_root", file), "autodoc_root", file, must_be_dir=True
    )
    proj_vault = None
    proj_vault_declined = "vault" in data and data.get("vault") is None
    if _optional_block(data, "vault") is not None:
        pvb = _require_block(data, "vault", file)
        proj_vault = Vault(
            root=_require_dir(_require_str(pvb, "root", file, "vault.root"), "vault.root", file),
            save_skill=_check_skill_path_if_set(_opt_str(pvb, "save_skill", file), "vault.save_skill", file, pending=pending),
            autodoc_update_skill=_check_skill_path_if_set(_opt_str(pvb, "autodoc_update_skill", file), "vault.autodoc_update_skill", file, pending=pending),
            mirror_helper=_check_skill_path_if_set(_opt_str(pvb, "mirror_helper", file), "vault.mirror_helper", file, executable=True, pending=pending),
            hot_cache=_check_path_if_set(_opt_str(pvb, "hot_cache", file), "vault.hot_cache", file),
        )

    ver_b = _require_block(data, "verification", file)
    strategy = _require_str(ver_b, "strategy", file, "verification.strategy")
    # Required, so blank is an error here: the skill-path check below reads blank as unset,
    # which is right for optional delegated paths and wrong for this one.
    if not strategy.strip():
        raise ConfigError("verification.strategy", file, "'verification.strategy' is empty",
                          "set verification.strategy to an enum value or a SKILL.md path")
    if strategy not in _VERIFICATION_ENUM:  # enum value OR resolvable SKILL.md path
        _check_skill_path_if_set(strategy, "verification.strategy", file, pending=pending)
    # READ, do not ignore. `verification.url` was declared in a live project file and
    # nothing had ever read it: the binding below overwrites the field from staging.url,
    # so the declared value was dead and a divergence between the two would have been
    # silent. Reading it here makes it a known key AND gives the cross-check something
    # to compare.
    declared_url = _opt_str(ver_b, "url", file, None, "verification.url")
    verification = Verification(
        strategy=strategy,
        roles=_opt_list(ver_b, "roles", file, "verification.roles"),
        test_login=_opt_str(ver_b, "test_login", file, None, "verification.test_login"),
    )
    if declared_url is not None and strategy != "web-journey":
        raise ConfigError(
            "verification.url", file,
            f"declared but strategy is '{strategy}' — only web-journey binds a url",
            "remove verification.url, or set verification.strategy: web-journey",
        )

    staging = None
    if _optional_block(data, "staging") is not None:
        st_b = _require_block(data, "staging", file)
        staging = Staging(
            url=_opt_str(st_b, "url", file, None, "staging.url"),
            deploy_host_ssh=_opt_str(st_b, "deploy_host_ssh", file, None, "staging.deploy_host_ssh"),
            checkout_path=_opt_str(st_b, "checkout_path", file, None, "staging.checkout_path"),
            deploy_recipe=_opt_str(st_b, "deploy_recipe", file, None, "staging.deploy_recipe"),  # prose|skill-path: kept verbatim
        )

    # staging.url required iff verification.strategy == web-journey; verification.url binds to it
    if strategy == "web-journey":
        st_url = staging.url if staging else None
        if not st_url:
            raise ConfigError("staging.url", file, "required when verification.strategy == web-journey", "set staging.url to the staging URL")
        # The hazard here is a check that can never fire: compare the FIELD after the
        # binding below and it reads `st_url != st_url`, green on every run and
        # indistinguishable from a guard that works. What averts it is not the order of
        # these two statements - it is that `declared_url` was captured from the raw
        # block above and is never re-read from `verification`. Verified by moving the
        # assignment ahead of this check: the guard still fires. Keep the local; a future
        # edit that compares `verification.url` instead re-creates the trap, and the
        # divergent-values test is what would catch it.
        if declared_url is not None and declared_url != st_url:
            raise ConfigError(
                "verification.url", file,
                f"declared {declared_url!r} but staging.url is {st_url!r}; the resolver "
                "binds staging.url, so the declared value would be silently ignored",
                "make the two match, or delete verification.url and let staging.url be the "
                "single source",
            )
        verification.url = st_url

    work_source = WorkSource()
    if _optional_block(data, "work_source") is not None:
        ws_b = _require_block(data, "work_source", file)
        ws_kind = _opt_str(ws_b, "kind", file, "filesystem", "work_source.kind")
        if ws_kind not in {"github-issues", "gitea", "filesystem", "none"}:
            raise ConfigError("work_source.kind", file, f"'{ws_kind}' not a valid work_source kind", "use github-issues|gitea|filesystem|none")
        page = _opt_int(ws_b, "open_issue_page", file, 200, "work_source.open_issue_page")
        # Rejected, not clamped: 0 refuses every read of a non-empty tracker, and a negative
        # page hands the tracker CLI a nonsense --limit. Neither is a usable preference.
        if page < 1:
            raise ConfigError("work_source.open_issue_page", file, f"open_issue_page {page} is below 1",
                              "set work_source.open_issue_page to 1 or more (default 200)")
        work_source = WorkSource(kind=ws_kind,
                                 bug_triage=_opt_bool(ws_b, "bug_triage", file, False, "work_source.bug_triage"),
                                 open_issue_page=page)

    generation_skills = GenerationSkills()
    if _optional_block(data, "generation_skills") is not None:
        gs_b = _require_block(data, "generation_skills", file)
        generation_skills = GenerationSkills(
            polishing=_check_skill_path_if_set(_opt_str(gs_b, "polishing", file), "generation_skills.polishing", file, pending=pending),
            provision=_check_skill_path_if_set(_opt_str(gs_b, "provision", file), "generation_skills.provision", file, pending=pending),
        )

    communities = None
    if _optional_block(data, "communities") is not None:
        cm_b = _require_block(data, "communities", file)
        _note(cm_b, "pins", opaque=True)  # keys are user-chosen path prefixes, not schema
        pins_raw = cm_b.get("pins")
        pins: dict = {}
        if pins_raw is not None:
            if not isinstance(pins_raw, dict):
                raise ConfigError(
                    "communities.pins", file,
                    f"expected a mapping of path-prefix -> community name, got {type(pins_raw).__name__}",
                    'make pins a yaml mapping, e.g. pins: {"legacy/subsys": "Legacy Subsystem"}',
                )
            for prefix, cname in pins_raw.items():
                if not isinstance(prefix, str) or not prefix.strip().strip("/").strip() or prefix.strip().strip("/") == ".":
                    raise ConfigError(
                        "communities.pins", file,
                        f"pin prefix {prefix!r} would be a catch-all (empty/root prefix)",
                        "pin NAMED path prefixes only; delete the entry or name a real subtree",
                    )
                if not isinstance(cname, str) or not cname.strip():
                    raise ConfigError(
                        "communities.pins", file,
                        f"pin {prefix!r} must map to a non-empty community name (string), got {cname!r}",
                        "set the pin value to the community name string",
                    )
                pins[prefix] = cname
        communities = Communities(pins=pins)

    audit = Audit()  # whole block optional; every field defaulted (SCHEMA §2)
    if _optional_block(data, "audit") is not None:
        au_b = _require_block(data, "audit", file)
        window = _opt_int(au_b, "window", file, 7, "audit.window")
        # Rejected, not clamped like idle_wake_minutes below: a window under 1 is not a
        # harmless preference. 0 audits nothing and reports the designed-healthy empty pass
        # forever; a negative slice drops the oldest epics. Neither ever goes red.
        if window < 1:
            raise ConfigError("audit.window", file, f"window {window} is below 1", "set audit.window to 1 or more (5-10 is typical)")
        audit = Audit(
            window=window,
            roles=_opt_list(au_b, "roles", file, "audit.roles"),  # [] ⇒ consumer falls back to verification.roles
            idle_wake_minutes=_clamped_idle_wake(
                _opt_int(au_b, "idle_wake_minutes", file, 60, "audit.idle_wake_minutes"), file
            ),
            notify=_opt_bool(au_b, "notify", file, True, "audit.notify"),
        )

    built_project = Project(
        schema_version=sv,
        name=decl_name,
        canonical_repo=canonical,
        autodoc_root=proj_autodoc_root,
        vault=proj_vault,
        vault_declined=proj_vault_declined,
        verification=verification,
        deprecated_mirror=deprecated_mirror,
        deprecated_mirror_checkout=deprecated_mirror_checkout,
        publish_repo=publish_repo,
        staging=staging,
        domains=_opt_list(data, "domains", file),
        aliases=_opt_list(data, "aliases", file),
        harness_notes=_opt_str(data, "harness_notes", file, None),
        excluded_domains=_opt_list(data, "excluded_domains", file),
        work_source=work_source,
        generation_skills=generation_skills,
        audit=audit,
        communities=communities,
    )
    # AFTER the constructor, never before it: the constructor's own arguments are
    # readers, so a check placed above the return sees a half-filled consumed-set and
    # rejects live keys. Measured on this machine: `aliases` is read inside Project(...).
    _reject_unknown_keys(data, file)
    return built_project


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def environment_path(config_home: "Optional[Path | str]" = None) -> Path:
    """``<config-home>/environment.md``, the file :func:`load_environment` reads."""
    return _config_home(config_home) / "environment.md"


def error_line(exc: BaseException, file: Optional[Path] = None) -> str:
    """A config load failure on one line, for a caller that reports it rather than raising.

    A :class:`ConfigError` keeps its remediation, which is what the operator has to fix. A
    ``UnicodeDecodeError`` names no file on its own, so *file* (the config that was being read)
    is put in front of it.
    """
    if isinstance(exc, UnicodeDecodeError) and file is not None:
        return f"{file}: not valid UTF-8 ({exc.reason} at byte {exc.start})"
    return " ".join(ln.strip() for ln in str(exc).splitlines() if ln.strip())


def failure_report(exc: ConfigError, headline: str) -> str:
    """The stderr text for a config that failed to load, specific error FIRST.

    Seats ntfy the first stderr line on a failed load, and a banner shows only the start of
    it. With the fixed headline on line 1 they sent the headline and dropped the facts
    (gh#918). So line 1 is ``[<field>] <reason's first line>`` - field and reason ahead of
    the file path, which can be long - then the headline, then the full error (file, field,
    reason, remediation) exactly as ``str(exc)`` gives it. Shared by ``config show`` and the
    SessionStart hook so the two cannot drift.
    """
    reason = next((ln for ln in exc.reason.splitlines() if ln.strip()), "(no reason given)")
    return f"[{exc.field_path}] {reason}\n{headline}\n{exc}\n"


def load_environment(*, config_home: "Optional[Path | str]" = None,
                     pending_deploy: Optional[PendingSkillDeploy] = None) -> Environment:
    """Load + validate ``<config-home>/environment.md``. Raises ConfigError loudly.

    *config_home* overrides ``$AGENTFLOW_CONFIG`` for this call only (see :func:`config_home`).
    *pending_deploy* is for the installer alone, before its skill deploy (see
    :class:`PendingSkillDeploy`); every other caller leaves it None and gets the strict check.
    """
    file = environment_path(config_home)
    data = _load_first_yaml_block(file)
    if pending_deploy is None:   # the exact call every non-installer caller has always made
        return _build_environment(data, file)
    return _build_environment(data, file, pending=pending_deploy)


#: Where the last name :func:`resolve_project_name` returned came from, as ``(name, source)``.
#: A module-level note rather than a new return value: every caller does
#: ``load_project(resolve_project_name(arg))``, and changing either signature would break
#: them all. ``load_project`` reads it only when the name matches, so a stale note can
#: at worst describe a name that was resolved earlier in the same process. Its scope is
#: same-name-only - a different name gets no source - and it is not thread-safe: two
#: threads resolving concurrently can overwrite each other's note.
_last_resolution: Optional[tuple] = None


def _project_name_error(name: str, file: Path, source: Optional[str],
                        config_home: "Optional[Path | str]" = None) -> Optional[ConfigError]:
    """Why *name* cannot be loaded as a project, or None when its file exists.

    A name is a FILE STEM. One with a separator or ``..`` reaches other files: ``--project
    ../environment`` loaded environment.md as a project and failed on a missing ``name:``,
    which points nowhere near the typo (gh#881). And an unknown name used to get "run the
    agentflow installer" - heavy, wrong advice for the likeliest launch mistake, reaching a
    phone through the seats' first-line ntfy. So the FIRST line carries the key facts: the
    name, where it came from, what IS configured, and the closest match. The installer
    remediation stays for the one case it fits: nothing configured at all.

    ``field_path`` stays ``<file>`` for an unknown name: callers key on it to report "config
    file not found" without echoing the message.
    """
    origin = f" ({source})" if source else ""
    # `:` because `C:foo` is drive-relative on Windows and escapes projects/ as surely as a
    # separator does. Dots only as the whole name: `a..b` is a legal stem.
    if any(sep in name for sep in ("/", "\\", ":")) or name in (".", ".."):
        return ConfigError(
            "<project>", file,
            f"invalid project name '{name}'{origin}: a name is a bare file stem, not a path; "
            f"configured: {', '.join(configured_project_names(config_home)) or 'none'}",
            "pass the bare name of a projects/<name>.md file - separators, drive letters "
            "and '..' would load some other file as a project",
        )
    if file.exists():
        return None
    # Listed only on the failure path: a successful load has no use for the directory scan.
    configured = configured_project_names(config_home)
    listing = ", ".join(configured)
    if not configured:
        return ConfigError(
            "<file>", file, f"config file not found: {file}",
            "run the agentflow installer or create the config file",
        )
    guess = difflib.get_close_matches(name, configured, n=1)
    hint = f"; did you mean '{guess[0]}'?" if guess else ""
    if source == "from $AGENTFLOW_PROJECT":
        fix = "set $AGENTFLOW_PROJECT to a configured name"
    else:
        fix = "pass a configured name to --project"
    return ConfigError(
        "<file>", file,
        f"unknown project '{name}'{origin}; configured: {listing}{hint}",
        f"{fix}, or create {file} for a new project",
    )


def load_project(name: str, *, source: Optional[str] = None,
                 config_home: "Optional[Path | str]" = None,
                 pending_deploy: Optional[PendingSkillDeploy] = None) -> Project:
    """Load + validate ``<config-home>/projects/<name>.md``. Raises ConfigError loudly.

    *source* says where *name* came from, for the error message. Left None it is taken
    from the last :func:`resolve_project_name` call when that returned this same name.
    *config_home* overrides ``$AGENTFLOW_CONFIG`` for this call only (see :func:`config_home`).
    *pending_deploy*: as for :func:`load_environment`.
    """
    if source is None and _last_resolution is not None and _last_resolution[0] == name:
        source = _last_resolution[1]
    file = _config_home(config_home) / "projects" / f"{name}.md"
    problem = _project_name_error(name, file, source, config_home)
    if problem is not None:
        raise problem
    data = _load_first_yaml_block(file)
    if pending_deploy is None:   # the exact call every non-installer caller has always made
        return _build_project(data, file, name)
    return _build_project(data, file, name, pending=pending_deploy)


def resolve_project_name(explicit: Optional[str] = None, *,
                         config_home: "Optional[Path | str]" = None) -> Optional[str]:
    """Active project name, highest precedence first:

    1. ``explicit`` — the ``--project <name>`` argument off the slash command. It
       BEATS the session's injected default, so one box can run several fleets.
    2. ``$AGENTFLOW_PROJECT`` — the SessionStart / settings-file default.
    3. the single ``projects/<name>.md`` file, when there is exactly one.
    4. ``None`` — environment-only config, which is valid.

    Called with ``explicit=None`` this reproduces the SessionStart hook's
    resolution EXACTLY; the hook's failure semantics are unchanged. *config_home*
    overrides ``$AGENTFLOW_CONFIG`` for step 3 only (see :func:`config_home`).
    """
    global _last_resolution
    if explicit:
        _last_resolution = (explicit, "from --project")
        return explicit
    from_env = os.environ.get("AGENTFLOW_PROJECT")
    if from_env:
        _last_resolution = (from_env, "from $AGENTFLOW_PROJECT")
        return from_env
    projects_dir = _config_home(config_home) / "projects"
    if projects_dir.is_dir():
        candidates = sorted(p.stem for p in projects_dir.glob("*.md"))
        if len(candidates) == 1:  # single-project default
            _last_resolution = (candidates[0], "the only configured project")
            return candidates[0]
    _last_resolution = None
    return None


class AutodocRootError(Exception):
    """A resolved autodoc root that would silently produce a wrong answer.

    Deliberately NOT a ConfigError: the config is well-formed. What is wrong is the
    FILESYSTEM state it points at, and callers that degrade gracefully on config
    problems must not degrade on this one.
    """

    def __init__(self, resolved: str, legacy: str):
        self.resolved = resolved
        self.legacy = legacy
        super().__init__(
            f"autodoc root {resolved!r} has no .manifest.json, but the legacy root "
            f"{legacy!r} still holds a corpus.\n"
            "  remediation: move the corpus to the resolved root BEFORE repointing config, "
            "or clear the legacy root. Answering from an empty root prints a genuine-looking "
            "zero, and a zero is read as evidence that the code is missing."
        )


def _autodoc_has_manifest(root) -> bool:
    return (Path(root) / ".manifest.json").is_file()


def _autodoc_foreign_project(root, name: str) -> bool:
    """True only when *root*'s manifest positively names a project other than *name*.

    A missing, unreadable or non-object manifest, or one with no ``project`` field, says nothing
    about ownership, so it is not foreign: those roots resolve exactly as they did before gh#759.
    """
    try:
        data = json.loads((Path(root) / ".manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    documents = data.get("project") if isinstance(data, dict) else None
    return isinstance(documents, str) and bool(documents) and documents != name


def _autodoc_has_corpus(root) -> bool:
    """A root that holds real pages, manifest or not."""
    p = Path(root)
    if not p.is_dir():
        return False
    if _autodoc_has_manifest(p):
        return True
    return any(p.glob("*/*.md"))


def resolve_autodoc_root(env, proj=None):
    """The code-wiki root for *proj*, or None when autodoc is unconfigured.

    Precedence: the project's own override, then the environment default, then the
    derived ``<data_root>/projects/<name>/autodoc``. With no override, an environment default
    whose ``.manifest.json`` names a different ``project`` is skipped (gh#759): the derived root
    if it exists, else None, and never AutodocRootError. A manifest with no ``project`` field
    is not skipped.

    THREE STATES, AND THE THIRD IS WHY THIS RAISES INSTEAD OF RETURNING None:

    1. unconfigured  -> None; callers degrade exactly as they do today.
    2. resolved with a manifest, or with nothing anywhere to contradict it -> the path.
    3. resolved, manifest-less, AND the environment's legacy root still holds a corpus
       -> **AutodocRootError**. This is the migration hazard: an empty scaffolded root
       passes every exists/is_dir gate while the real corpus sits orphaned elsewhere.

    Returning None in case 3 would be worse than useless. ``find_touchpoints`` is
    documented never to raise, so the tool prints ``## flow (0)`` and exits 0 - and a
    wrong root is then indistinguishable from a genuine zero. The auditor treats a
    use-case that greps to nothing as positive evidence of a defect, so a silent wrong
    root manufactures false gap epics.
    """
    if proj is not None and not hasattr(proj, "name"):
        # A NAME is not a project. `getattr` on a str finds no `autodoc_root`, so the override
        # is silently skipped and this returns the environment-level legacy root -- another
        # project's real, populated corpus, which passes every downstream existence check and
        # never trips AutodocRootError. The failure surfaces as the WRONG PROJECT'S PAGES, far
        # from here (gh#397). Refuse rather than coerce: calling load_project() here would make
        # the broken form work and leave every caller that writes it uncorrected.
        raise TypeError(
            "resolve_autodoc_root() takes a project object, not %s. Pass load_project(%r). "
            "A name silently skips the project override and returns another project's corpus."
            % (type(proj).__name__, proj)
        )

    legacy = getattr(env, "autodoc_root", None)
    override = getattr(proj, "autodoc_root", None) if proj is not None else None

    # gh#759: with no override, a machine-level root whose manifest names ANOTHER project is that
    # project's corpus. Taking it here returned the wrong code for every project on the box, and
    # the derived per-project root was never reached. An explicit override is the operator's own
    # statement and is never second-guessed.
    if not override and legacy and proj is not None and _autodoc_foreign_project(legacy, proj.name):
        legacy = None

    resolved = override or legacy
    if resolved is None and proj is not None and getattr(env, "data_root", None):
        derived = Path(env.data_root) / "projects" / proj.name / "autodoc"
        resolved = str(derived) if derived.is_dir() else None
    if resolved is None:
        return None

    # A skipped foreign root is out of the guard too (legacy is None): it is not this project's
    # orphaned corpus, so it says nothing about whether the resolved root is a migration hazard.
    if not _autodoc_has_manifest(resolved) and legacy and str(legacy) != str(resolved):
        if _autodoc_has_corpus(legacy):
            raise AutodocRootError(str(resolved), str(legacy))
    return str(resolved)


#: Roots searched for a vault, relative to the user's home. Deliberately short and
#: deliberately shallow: this exists to give the install interview real candidates to
#: OFFER, not to find every vault on the disk. A miss costs one typed path; a deep walk
#: costs seconds on every install and finds other people's backups.
VAULT_SEARCH_ROOTS: tuple[str, ...] = (".", "CODE", "Documents", "vaults", "obsidian")

#: How deep below each root to look. 1 = the root's own children.
VAULT_SEARCH_DEPTH = 1


def find_vault_candidates(home=None, roots=None, depth=None) -> list:
    """Directories that look like Obsidian vaults, for the install interview to offer.

    A vault is identified by a `.obsidian/` directory inside it - the marker Obsidian
    itself writes, so this recognises real vaults rather than folders named hopefully.

    NEVER RAISES and never blocks. An unreadable directory, a permission error, a home
    that does not exist: all of them yield fewer candidates, never an exception. This runs
    during install, where the whole point is that the vault question cannot stop the
    install - "no vault" is a first-class answer and a failed search is just a shorter
    list to choose from.

    Returns absolute paths, sorted, deduplicated.
    """
    from pathlib import Path

    base = Path(home).expanduser() if home else Path.home()
    roots = VAULT_SEARCH_ROOTS if roots is None else roots
    depth = VAULT_SEARCH_DEPTH if depth is None else depth

    found = set()

    def _scan(d, level):
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if (entry / ".obsidian").is_dir():
                    found.add(str(entry.resolve()))
                elif level < depth:
                    _scan(entry, level + 1)
            except OSError:
                continue

    for root in roots:
        try:
            start = (base / root).resolve()
        except OSError:
            continue
        if not start.is_dir():
            continue
        if (start / ".obsidian").is_dir():
            found.add(str(start))
        _scan(start, 0)

    return sorted(found)

def configured_project_names(config_home=None) -> list:
    """Every project this machine has a config file for, sorted.

    The vault's `mirror-epic.sh` routes by project name against an allowlist, and that
    allowlist was a literal inside the script. So the NEXT project a user configures routed
    silently to `_unsorted/` until somebody edited a bash file inside a vault - invisible
    from the agentflow side, where the project is plainly configured and plainly working.

    This is the same move `resolve_vault_root` made: the caller passes what config resolved
    instead of letting the script guess. A filename is the right source because that is what
    `load_project` itself keys on - a name with a config file is a project by definition.

    Never raises: a missing or unreadable config home is an empty list, because a caller
    using this to build an env var must not be the thing that fails an install.
    """
    from pathlib import Path

    home = Path(config_home) if config_home else _config_home()
    try:
        return sorted(p.stem for p in (home / "projects").glob("*.md"))
    except OSError:
        return []

def _resolve_vault_field(env, proj, name: str) -> Optional[str]:
    """The vault field *name* for *proj*: the project's value, then the environment's, else None.

    ONE rule for every optional vault field a reader resolves, so the resolvers built on
    it cannot drift into different precedences.
    """
    override = getattr(getattr(proj, "vault", None), name, None) if proj is not None else None
    default = getattr(getattr(env, "vault", None), name, None)
    return override or default or None


def resolve_vault_root(env, proj=None) -> Optional[str]:
    """The vault root for *proj*, or None when no vault is configured.

    Precedence mirrors ``resolve_autodoc_root``: the project's own override, then the
    environment default, then None. Absent means the feature is OFF - every mirror site
    already knows how to no-op, so "no vault" is a first-class answer rather than a
    degraded mode.

    WHY THIS EXISTS: `vault.root` was decorative. The schema accepted it, the resolver
    stored it, and NOTHING read it back out - `mirror-epic.sh` resolves its own root from
    `WIKI_VAULT_PATH`, defaulting to a hardcoded path, and no part of agentflow ever
    exported that variable. So a user whose vault is anywhere else got one of two silent
    outcomes: the helper's feature-detect saw no directory and exited 0 having mirrored
    nothing, or it wrote into a stranger's folder that happened to sit at the default path.
    Neither reports anything. A config field with no reader is a promise read by no one.
    """
    return _resolve_vault_field(env, proj, "root")


def resolve_mirror_helper(env, proj=None) -> Optional[str]:
    """The wiki-mirror helper for *proj*, or None when none is configured.

    Same precedence as ``resolve_vault_root``: the project's own override, then the
    environment default. A resolver rather than an inline lookup so the block, doctor and
    any later reader share one rule instead of copies that drift.
    """
    return _resolve_vault_field(env, proj, "mirror_helper")


def resolve_vault_save_skill(env, proj=None) -> Optional[str]:
    """The knowledge-base save skill for *proj*, or None when none is configured.

    Same precedence as ``resolve_vault_root``. Printed in the resolved block because the
    done-gate files its note only when a save skill is configured, and a lane that cannot
    see the value cannot tell which case it is in.
    """
    return _resolve_vault_field(env, proj, "save_skill")


def resolve_vault_autodoc_update_skill(env, proj=None) -> Optional[str]:
    """The code-wiki refresh skill for *proj*, or None when none is configured.

    Same precedence as ``resolve_vault_save_skill``. Printed in the resolved block because
    lead-dev and the orchestrator follow ``<vault.autodoc_update_skill>`` after a merge, and
    a placeholder the block does not print is one no loop can fill.
    """
    return _resolve_vault_field(env, proj, "autodoc_update_skill")


def _redact_userinfo(value: str) -> str:
    """*value* with any URL credentials replaced, for printing.

    `deprecated_mirror` is documented as an `owner/repo` ref but is not validated, and the
    resolved block goes into every session transcript - a pasted clone URL carrying a token
    must not ride along with it.
    """
    parts = urlsplit(value)
    if parts.scheme and "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit((parts.scheme, f"<redacted>@{host}", parts.path, parts.query, parts.fragment))
    return value


def vault_declined(env, proj=None) -> bool:
    """Whether "no vault" is a RECORDED ANSWER rather than a question nobody asked.

    Only ever consulted when `resolve_vault_root` returns None, so the two flags are
    combined rather than ranked: a decline recorded at either level is a decline, and a
    project-level one cannot mask an environment vault it never overrode.

    WHY THIS EXISTS: the install interview's third outcome - asked, answered no - was
    documented in three places as the thing that stops `doctor` offering vault candidates
    to somebody who already declined. Nothing read it. `vault: null` and an absent key
    both arrived here as `vault=None`, so the diagnostic re-offered on every run and the
    prose describing the convention was a promise the code could not keep.

    NOT an opt-out. A project that declines while the environment configures a vault still
    resolves the environment's root, because turning a project's mirroring off is a
    different feature from recording an answer, and only the second one was asked for.
    """
    return bool(getattr(env, "vault_declined", False)) or bool(
        getattr(proj, "vault_declined", False) if proj is not None else False
    )


def resolve_vault_subdir(env, proj=None) -> str:
    """The vault subdir for *proj*, following the same precedence as the root.

    Returned even when no vault is configured, because it is a layout constant rather than
    a switch: a caller that has a root needs a subdir, and one that has neither never asks.
    """
    override = getattr(getattr(proj, "vault", None), "subdir", None) if proj is not None else None
    default = getattr(getattr(env, "vault", None), "subdir", None)
    return override or default or DEFAULT_VAULT_SUBDIR

def work_source_host_conflict(env: Environment, proj: Optional[Project]) -> Optional[str]:
    """The reason issue intake will refuse ``proj.work_source`` on ``env.git_host``, or None.

    The two halves live in different files (environment vs project profile), so neither
    loader can see the pairing on its own. A ``gitea`` tracker has no verified argv builder,
    so intake refuses it whenever ``git_host.cli`` would reach a tracker at all; with cli
    ``none`` it is the documented no-op and nothing is flagged. The cli set mirrors
    ``issue_intake._TRACKER_CLIS``. Returned rather than raised: intake already refuses
    loudly on its own, and a hard fail here would take down every lane on the project at
    session open.
    """
    if proj is None:
        return None
    cli = env.git_host.cli
    if proj.work_source.kind == "gitea" and cli in ("gh", "glab"):
        return (
            f"work_source.kind 'gitea' has no verified issue-intake argv builder and "
            f"git_host.cli '{cli}' would reach it; set work_source.kind to github-issues if "
            "the issues live on GitHub, or git_host.cli to none to disable intake"
        )
    return None


def render_resolved_block(env: Environment, proj: Optional[Project]) -> str:
    """The resolved-config block, byte-identical to the SessionStart injection.

    Secrets: prints only env-var NAMES for notify, never a secret value.
    """
    lines = ["=== agentflow resolved config ==="]

    # Paths
    lines.append("[paths]")
    lines.append(f"  data_root    = {env.data_root}")
    lines.append(f"  lock_dir     = {env.lock_dir}")
    lines.append(f"  suite_root   = {env.suite_root}")
    lines.append(f"  worktree_root = {env.worktree_root}")
    # The project's root, never the bare environment field: every loop binds
    # {autodoc_root} out of this block, so printing the machine default here is how a
    # two-project fleet has one project's autodoc pass write into the other's tree.
    try:
        _adr = resolve_autodoc_root(env, proj)
    except AutodocRootError as _e:
        _adr = f"(UNUSABLE: {_e.args[0].splitlines()[0]})"
    lines.append(f"  autodoc_root = {_adr or '(unset)'}")
    # Printed for the same reason autodoc_root is: the mirror helper takes its root from
    # WIKI_VAULT_PATH and falls back to a hardcoded path, so a loop that cannot SEE the
    # resolved root cannot pass it, and the fallback quietly decides where documents land.
    _vr = resolve_vault_root(env, proj)
    if _vr:
        _vr_line = _vr
    elif vault_declined(env, proj):
        _vr_line = "(no vault - declined at install; every mirror site no-ops)"
    else:
        _vr_line = "(no vault - every mirror site no-ops)"
    lines.append(f"  vault_root   = {_vr_line}")
    if _vr:
        lines.append(f"  vault_subdir = {resolve_vault_subdir(env, proj)}")
        # The mirror routes by project NAME against an allowlist. Printed here for the
        # same reason vault_root is: a loop that cannot see the list cannot pass it, and
        # the script's own literal then decides which projects exist.
        lines.append(
            f"  vault_projects = {' '.join(configured_project_names()) or '(none)'}"
        )
        # Printed only when configured, as with [publish]: the done-gate keys its
        # knowledge-base note on this line being present.
        _vss = resolve_vault_save_skill(env, proj)
        if _vss:
            lines.append(f"  vault_save_skill = {_vss}")
        # Spelled as the `<vault.autodoc_update_skill>` placeholder it fills, and printed
        # only when configured, as with vault_save_skill.
        _aus = resolve_vault_autodoc_update_skill(env, proj)
        if _aus:
            lines.append(f"  vault.autodoc_update_skill = {_aus}")

    # Git host
    lines.append(f"[git host] {env.git_host.kind} (cli: {env.git_host.cli})")

    # Notify — NAMES only, never secret values
    if env.notify.kind == "none":
        lines.append("[notify] none")
    else:
        lines.append(
            f"[notify] {env.notify.kind} "
            f"(url_env: {env.notify.url_env}, topic_env: {env.notify.topic_env}, "
            f"ascii_only: {env.notify.ascii_only})"
        )

    # Active builders
    pb = env.parallel_builders
    lines.append(f"[active builders] enabled: {pb.enabled}, count: {pb.count}")
    # Unconditional: every environment resolves a style, and a seat told to honour it
    # can only do so if the block shows it.
    lines.append(f"[question_style] {env.question_style}")

    # Project + verification strategy
    if proj is None:
        lines.append("[project] (no project)")
        lines.append("[verification] (no project)")
    else:
        lines.append(f"[project] {proj.name} (repo: {proj.canonical_repo.host_ref})")
        # These four keys are spelled EXACTLY as the skill placeholders they fill
        # (/project, /solution, /orchestrator say "off the resolved block"), so a reader
        # can match them by eye. Before these lines the loader parsed all four and the
        # block printed none, which left a first-time operator reading the profile by hand.
        cr = proj.canonical_repo
        lines.append(f"  canonical_repo.local_checkout = {cr.local_checkout}")
        lines.append(f"  base-branch = {cr.base_branch}")
        # Only when set, as with [publish]: absent is the normal case.
        if proj.deprecated_mirror:
            lines.append(f"  deprecated_mirror = {_redact_userinfo(proj.deprecated_mirror)}")
        lines.append(f"  vault.mirror_helper = {resolve_mirror_helper(env, proj) or '(unset)'}")
        # Printed ONLY when configured. An unconditional "[publish] (none)" line would put
        # a public repo in front of every loop on every project that has none, and the
        # absent case is the normal one - see PublishRepo. The line names the rule as well
        # as the repo, because the block is what a loop actually reads at session open.
        if proj.publish_repo:
            lines.append(
                f"[publish] {proj.publish_repo.host_ref} "
                f"(cadence: {proj.publish_repo.cadence}) "
                "READ-ONLY: never push, PR, or ground against it - a human publishes"
            )
        if proj.canonical_repo.python:
            lines.append(f"[product python] {proj.canonical_repo.python}  (use THIS for the product's tests; bare `python` is the suite venv)")
        v = proj.verification
        vurl = f", url: {v.url}" if v.url else ""
        vroles = f", roles: {', '.join(v.roles)}" if v.roles else ""
        lines.append(f"[verification] {v.strategy}{vurl}{vroles}")
        # Unconditional, defaults included: issue intake reads these three keys, and a seat
        # that cannot see them cannot tell a defaulted profile from a configured one (gh#1031).
        ws = proj.work_source
        lines.append(
            f"[work_source] kind: {ws.kind}, bug_triage: {ws.bug_triage}, "
            f"open_issue_page: {ws.open_issue_page}"
        )
        # Printed only when flagged: the block is what every loop reads at session open,
        # so a misconfiguration named here is seen before intake refuses on it.
        _conflict = work_source_host_conflict(env, proj)
        if _conflict:
            lines.append(f"[work_source] CONFLICT: {_conflict}")

    lines.append("=================================")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Declared-dependency check — the auditor's fallback when [product python] is unset
# --------------------------------------------------------------------------- #
# With no `canonical_repo.python`, an audit probe runs under the default `python`, which in a
# fleet session is this suite's venv. A probe that then dies on a missing third-party import
# reads like a product regression. The auditor is read-only (no scratch venv, no install), so
# it checks once per pass whether the product's DECLARED distributions are present here and
# blocks the probe when they are not. Presence only: versions are not compared, because
# comparing them needs a specifier parser this package does not depend on.
_PYPROJECT_SOURCE = "pyproject.toml [project].dependencies"
_REQUIREMENTS_SOURCE = "requirements.txt"
_UNSET = "product interpreter unset; ran under default python"
DECLARED_DEPS_PRESENT_EVIDENCE = f"{_UNSET} (declared deps present); versions are not compared"
# Names what was read, so a product declaring its dependencies elsewhere (setup.py, a lock
# file) is never reported as having none.
DECLARED_DEPS_NONE_EVIDENCE = (
    f"{_UNSET} (no dependencies declared in pyproject.toml [project] or requirements.txt)"
)
DECLARED_DEPS_REMEDY = "set canonical_repo.python in projects/<name>.md"

# PEP 508 distribution name at the head of a requirement string.
_REQ_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def normalize_dist_name(name: str) -> str:
    """PEP 503 normalized form: runs of ``-_.`` collapse to ``-``, lowercased."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(line: str) -> Optional[str]:
    """The distribution name in one requirement line, or None for a line that names none.

    Strips extras, version specifiers, markers and comments. Skips blank lines, comments,
    option lines (``-r``, ``-e``, ``-c``, ``--index-url`` ...), and bare paths or URLs, which
    name no distribution without building them. A ``name @ url`` direct reference keeps its
    name.
    """
    s = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
    if not s or s.startswith(("-", ".", "/", "~")):
        return None
    # A bare local archive (`pkg-1.0-py3-none-any.whl`, `pkg.tar.gz`) names a file, not a
    # distribution. `name @ <url-to-archive>` is a direct reference and keeps its name.
    head = s.split(";", 1)[0].strip()
    if "@" not in head and re.search(r"\.(?:whl|zip|tar\.gz|tgz|tar\.bz2|tar\.xz)$", head, re.IGNORECASE):
        return None
    m = _REQ_NAME.match(s)
    if not m:
        return None
    rest = s[m.end():].lstrip()
    # `git+https://...`, `https://...`, `C:\...`: the "name" matched is a scheme or a drive.
    if rest.startswith((":", "+", "/", "\\")):
        return None
    return m.group(0)


# --------------------------------------------------------------------------- #
# Environment markers: a small PEP 508 subset, evaluated for THIS interpreter
# --------------------------------------------------------------------------- #
# The check reports what the default python can import here, so a requirement whose marker is
# false on this platform (`pywin32; sys_platform == "win32"` on Linux) is not declared for it.
# Only a subset is understood; anything outside it fails open to "applies", so an unparsed
# marker never hides a requirement.
_MARKER_VARS = frozenset({
    "sys_platform", "platform_system", "os_name", "python_version", "platform_machine",
    "implementation_name",
})
_MARKER_TOKEN = re.compile(
    r"\s*(?:(?P<str>'[^']*'|\"[^\"]*\")|(?P<op>==|!=|<=|>=|<|>)|(?P<paren>[()])"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_.]*))"
)


class _MarkerUnsupported(Exception):
    """A marker outside the supported subset; the caller treats the requirement as applicable."""


def _marker_environment() -> dict:
    """Marker variable values for the running interpreter."""
    import os
    import platform

    return {
        "sys_platform": sys.platform,
        "platform_system": platform.system(),
        "os_name": os.name,
        "python_version": "{}.{}".format(*sys.version_info[:2]),
        "platform_machine": platform.machine(),
        "implementation_name": sys.implementation.name,
    }


_MARKER_WS_END = re.compile(r"\s*\Z")


def _marker_tokens(marker: str) -> list:
    tokens, pos = [], 0
    while pos < len(marker):
        if _MARKER_WS_END.match(marker, pos):
            break
        m = _MARKER_TOKEN.match(marker, pos)
        if not m:
            raise _MarkerUnsupported(marker)
        kind = m.lastgroup
        text = m.group(kind)
        tokens.append((kind, text[1:-1] if kind == "str" else text))
        pos = m.end()
    return tokens


def _version_tuple(value: str) -> tuple:
    if not re.fullmatch(r"\d+(?:\.\d+)*", value):
        raise _MarkerUnsupported(value)
    return tuple(int(p) for p in value.split("."))


def _marker_compare(var: str, op: str, value: str, env: dict) -> bool:
    actual = env[var]
    if var == "python_version":
        a, b = _version_tuple(actual), _version_tuple(value)
        width = max(len(a), len(b))
        a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    elif op in ("==", "!="):
        a, b = actual, value
    else:
        raise _MarkerUnsupported(f"{var} {op}")
    return {
        "==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
    }[op]


_REVERSED_OP = {"==": "==", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


def _marker_eval(tokens: list, env: dict) -> bool:
    """Recursive descent over ``or`` / ``and`` / parentheses; ``and`` binds tighter."""
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else (None, None)

    def take():
        nonlocal pos
        if pos >= len(tokens):
            raise _MarkerUnsupported("truncated marker")
        pos += 1
        return tokens[pos - 1]

    def clause() -> bool:
        if peek() == ("paren", "("):
            take()
            result = disjunction()
            if take() != ("paren", ")"):
                raise _MarkerUnsupported("unbalanced parentheses")
            return result
        (lk, lv), (ok, ov), (rk, rv) = take(), take(), take()
        if ok != "op":
            raise _MarkerUnsupported(f"operator {ov!r}")
        if lk == "word" and rk == "str" and lv in _MARKER_VARS:
            return _marker_compare(lv, ov, rv, env)
        if lk == "str" and rk == "word" and rv in _MARKER_VARS:
            return _marker_compare(rv, _REVERSED_OP[ov], lv, env)
        raise _MarkerUnsupported(f"clause {lv!r} {ov} {rv!r}")

    def conjunction() -> bool:
        result = clause()
        while peek() == ("word", "and"):
            take()
            result = clause() and result
        return result

    def disjunction() -> bool:
        result = conjunction()
        while peek() == ("word", "or"):
            take()
            result = conjunction() or result
        return result

    result = disjunction()
    if pos != len(tokens):
        raise _MarkerUnsupported("trailing tokens")
    return result


def requirement_marker(line: str) -> Optional[str]:
    """The environment marker of one requirement string, or None when it carries none."""
    s = re.split(r"(?:^|\s)#", line, maxsplit=1)[0]
    # A direct reference's URL may hold a `;`, so its marker needs whitespace before the `;`.
    parts = re.split(r"\s;" if "@" in s.split(";", 1)[0] else ";", s, maxsplit=1)
    if len(parts) < 2:
        return None
    # Per-requirement options (`--hash=...`) may follow the marker on a requirements line.
    marker = re.split(r"\s--", parts[1], maxsplit=1)[0].strip()
    return marker or None


def marker_applies(marker: Optional[str], env: Optional[dict] = None) -> bool:
    """Whether a requirement with this marker applies to the running interpreter.

    Supports ``<var> <op> '<value>'`` and ``'<value>' <op> <var>`` clauses over
    ``sys_platform``, ``platform_system``, ``os_name``, ``python_version``,
    ``platform_machine`` and ``implementation_name``, joined by ``and`` / ``or`` with
    parentheses. ``==`` / ``!=`` work for every variable; ``<``, ``<=``, ``>``, ``>=`` only for
    ``python_version``, compared as integer tuples. Anything else (``extra``, ``in``, an
    unknown variable, malformed text) returns True: an unparsed marker never drops a
    requirement, and this never raises.
    """
    if not marker:
        return True
    try:
        return _marker_eval(_marker_tokens(marker), _marker_environment() if env is None else env)
    except (_MarkerUnsupported, RecursionError):
        # Deeply nested parentheses exhaust the recursive-descent stack; that is malformed
        # input like any other and must not escape as a traceback.
        return True


@dataclass(frozen=True)
class DeclaredDeps:
    """Result of ``declared_deps_check``. ``source`` is None when nothing was declared."""

    repo: str
    source: Optional[str]
    declared: tuple
    missing: tuple

    @property
    def declared_count(self) -> int:
        return len(self.declared)

    @property
    def blocks(self) -> bool:
        return bool(self.missing)

    @property
    def evidence(self) -> str:
        return DECLARED_DEPS_NONE_EVIDENCE if self.source is None else DECLARED_DEPS_PRESENT_EVIDENCE

    @property
    def blocked_on(self) -> Optional[str]:
        if not self.missing:
            return None
        return (
            "probe cannot run: product interpreter unset and declared distributions missing "
            f"under default python ({self.source}): {', '.join(self.missing)}; "
            f"remedy: {DECLARED_DEPS_REMEDY}"
        )

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "source": self.source,
            "declared_count": self.declared_count,
            "missing": list(self.missing),
            "evidence": self.evidence,
            "blocked_on": self.blocked_on,
        }


def _declared_requirements(root: Path) -> tuple:
    """(source, [requirement strings]) from pyproject [project].dependencies, else
    requirements.txt; (None, []) when neither declares any."""
    import tomllib

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(
                "project.dependencies", pyproject,
                f"pyproject.toml could not be read: {exc}",
                "fix the file - the auditor cannot tell which dependencies the product declares",
            ) from exc
        project = data.get("project", {})
        if not isinstance(project, dict):
            raise ConfigError(
                "project.dependencies", pyproject,
                "[project] is not a table",
                "declare [project] as a TOML table",
            )
        deps = project.get("dependencies")
        if deps:
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                raise ConfigError(
                    "project.dependencies", pyproject,
                    "[project].dependencies is not a list of requirement strings",
                    "declare dependencies as a TOML array of strings",
                )
            return _PYPROJECT_SOURCE, deps
    reqs = root / "requirements.txt"
    if reqs.is_file():
        lines = _requirements_lines(reqs, set(), None)
        if any(requirement_name(line) for line in lines):
            return _REQUIREMENTS_SOURCE, lines
    return None, []


# `-r base.txt`, `-rbase.txt`, `--requirement base.txt`, `--requirement=base.txt`. Constraint
# files (`-c`) only narrow versions and declare nothing, so they are not followed.
_REQ_INCLUDE = re.compile(r"^(?:-r\s*|--requirement(?:\s*=\s*|\s+))(?P<path>\S+)")


def _requirements_lines(path: Path, visited: set, included_from: Optional[Path]) -> list:
    """Every line of a requirements file, with ``-r`` includes expanded in place.

    An include resolves against the including file's directory. A file already read is
    skipped, so a cycle terminates. An include that cannot be read fails like an unreadable
    ``requirements.txt``: a declaration the check cannot see must not read as none.
    """
    visited.add(path.resolve())
    label = "requirements.txt" if included_from is None else (
        f"requirements include {path} (from {included_from})"
    )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            "requirements", path, f"{label} could not be read: {exc}",
            "fix the file's permissions or encoding (it must be UTF-8)" if included_from is None
            else "fix the -r path, or the included file's permissions or encoding (UTF-8)",
        ) from exc
    out: list = []
    for line in lines:
        m = _REQ_INCLUDE.match(re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip())
        if not m:
            out.append(line)
            continue
        target = m.group("path")
        if re.match(r"[A-Za-z][A-Za-z0-9+.-]+://", target):
            raise ConfigError(
                "requirements", path, f"{label} includes a remote file ({target})",
                "the check reads local files only; vendor the included file into the repo",
            )
        child = path.parent / target
        if child.resolve() not in visited:
            out.extend(_requirements_lines(child, visited, path))
    return out


def declared_deps_check(repo_root, installed: Optional[set] = None) -> DeclaredDeps:
    """Which of the product's declared distributions are absent under THIS interpreter.

    ``installed`` is a set of distribution names (any spelling) for tests; by default it is
    every distribution ``importlib.metadata`` can see. Names compare PEP 503 normalized. A
    requirement whose environment marker is false here is not declared for this interpreter,
    so it is neither checked nor counted (see ``marker_applies``).
    Raises ``ConfigError`` when the repo root is missing or a declaration file is unreadable.
    """
    root = Path(repo_root).expanduser()
    if not root.is_dir():
        raise ConfigError(
            "canonical_repo.local_checkout", root, "repo root does not exist",
            "point --repo at the product checkout",
        )
    source, reqs = _declared_requirements(root)
    names: list = []
    seen: set = set()
    for req in reqs:
        name = requirement_name(req)
        if name and normalize_dist_name(name) not in seen and marker_applies(requirement_marker(req)):
            seen.add(normalize_dist_name(name))
            names.append(name)
    if installed is None:
        import importlib.metadata

        installed = {d.metadata["Name"] for d in importlib.metadata.distributions()
                     if d.metadata["Name"]}
    have = {normalize_dist_name(n) for n in installed}
    missing = tuple(n for n in names if normalize_dist_name(n) not in have)
    return DeclaredDeps(repo=str(root), source=source, declared=tuple(names), missing=missing)


def render_project_claude_md(proj):
    """The resolved project's repo ``CLAUDE.md``, or "" when there is nothing to emit.

    Launching from the machine's single launch directory means a project repo's own
    ``CLAUDE.md`` is no longer picked up by cwd traversal - and for this suite that file
    carries the session-URL directive and the sanitize-gate rule, both load-bearing.
    Emitting it here makes repo rules follow the PROJECT YOU SELECTED rather than the
    FOLDER YOU OPENED: each project's lane gets that project's rules, and neither leaks
    into the other.

    A missing file is a normal case, not an error - emit nothing, say nothing. An
    UNREADABLE one is a loud failure like any other config fault, because silently
    dropping rules a lane is supposed to be bound by is the exact failure this exists to
    prevent.

    Lives here rather than in the hook so the session-open block and
    ``python -m agentflow.config show`` can never drift apart - the same constraint the
    hook's docstring already states for ``render_resolved_block``.
    """
    if proj is None:
        return ""
    path = Path(proj.canonical_repo.local_checkout).expanduser() / "CLAUDE.md"
    if not path.is_file():
        return ""  # normal: a project need not carry repo instructions
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, NOT an OSError: a CLAUDE.md saved in the
        # editor's legacy codepage rather than UTF-8 is a read failure that OSError alone
        # does not catch, and uncaught it exits with a raw traceback instead of the
        # file/field/reason/remediation this contract promises. Same class of fault, same
        # loud structured failure.
        raise ConfigError(
            "canonical_repo.local_checkout",
            str(path),
            "project CLAUDE.md exists but could not be read: {}".format(exc),
            "fix the file's permissions or encoding (it must be UTF-8) - a lane must not "
            "run unbound by rules it is supposed to load",
        ) from exc
    header = "=== {} repo CLAUDE.md ({}) ===".format(proj.name, path)
    return header + "\n" + body.rstrip() + "\n" + "=" * len(header)


# --------------------------------------------------------------------------- #
# CLI — `python -m agentflow.config show [--project <name>]`
#       `python -m agentflow.config declared-deps --repo <repo_root>`
# --------------------------------------------------------------------------- #
def _cmd_show(args: argparse.Namespace) -> int:
    """Print the resolved block for the named project. Same resolver, same hard-fail."""
    try:
        env = load_environment()
        name = resolve_project_name(args.project)
        proj = load_project(name) if name else None
        # Both renders sit inside this try, exactly as in the SessionStart hook: one
        # ConfigError handler covers resolution AND the unreadable-CLAUDE.md raise.
        out = render_resolved_block(env, proj) + "\n"
        repo_rules = render_project_claude_md(proj)  # "" when there is none
        if repo_rules:
            out += repo_rules + "\n"
    except ConfigError as e:
        # Same shape the SessionStart hook uses: the specific error, the headline, then
        # the full error (file / field / reason / remediation all live in str(e)).
        sys.stderr.write(failure_report(e, "agentflow config resolution FAILED"))
        sys.stderr.flush()
        return 1

    # `show` exists so an operator can see what the session was handed. Emitting less
    # than the hook does — dropping the repo rules, or dying on prose the hook printed
    # fine — means debugging a block the lane never got, so this half mirrors the hook
    # line for line: same appended block, same UTF-8 forcing for a legacy cp1252 stdout
    # (the shared agentflow._stdio helper main() calls first, gh#574).
    sys.stdout.write(out)
    sys.stdout.flush()
    return 0


def _cmd_declared_deps(args: argparse.Namespace) -> int:
    """Print the declared-deps result as JSON. Same failure shape as ``show``."""
    try:
        result = declared_deps_check(args.repo)
    except ConfigError as e:
        sys.stderr.write("agentflow declared-deps check FAILED\n")
        sys.stderr.write(f"{e}\n")
        sys.stderr.flush()
        return 2
    sys.stdout.write(json.dumps(result.to_dict()) + "\n")
    sys.stdout.flush()
    return 1 if result.blocks else 0


def main(argv: Optional[list] = None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m agentflow.config",
        description="Resolve and print the agentflow typed-YAML config.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    show = sub.add_parser(
        "show",
        help="print the resolved-config block (the same block SessionStart injects)",
        description=(
            "Print the resolved-config block for the active project. With --project, "
            "the named project WINS over $AGENTFLOW_PROJECT and over the session's "
            "injected block."
        ),
    )
    show.add_argument(
        "--project",
        metavar="NAME",
        default=None,
        help=(
            "active project name (matches projects/<NAME>.md). Omit to use "
            "$AGENTFLOW_PROJECT, else the single projects/<name>.md file."
        ),
    )

    deps = sub.add_parser(
        "declared-deps",
        help="check the product's declared dependencies are installed under this python",
        description=(
            "Read pyproject.toml [project].dependencies, else requirements.txt, at --repo and "
            "print JSON naming any declared distribution not installed under this "
            "interpreter. Exit 0 when none is missing (or none is declared), 1 when any is, "
            "2 when a declaration file cannot be read. Versions are not compared."
        ),
    )
    deps.add_argument("--repo", metavar="PATH", required=True, help="the product repo root")

    args = parser.parse_args(argv)
    if args.command == "show":
        return _cmd_show(args)
    if args.command == "declared-deps":
        return _cmd_declared_deps(args)
    parser.error(f"unknown command: {args.command}")  # unreachable — argparse guards
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
