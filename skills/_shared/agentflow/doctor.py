"""``python -m agentflow.doctor`` — one diagnostic over the whole resolved surface.

WHY THIS IS NOT JUST A STEWARD DOMAIN, since that is the obvious question: the
steward is a LOOP, and a diagnostic you can only run when the fleet is healthy
enough to start a loop is not a diagnostic. You reach for ``doctor`` precisely when
nothing works. It also has to run at install time and in CI, neither of which can
invoke a loop.

So the *periodic execution* is the steward's — it joins the sweep rotation and
reports findings the way every other domain does — and the *runnable-when-broken*
entry point is this CLI. **One implementation, two callers**: ``diagnose()`` is a
library function returning data, and both callers render it.

THE LOAD-BEARING PROPERTY IS THAT NOTHING HERE RAISES. Every check is individually
guarded, because the state this tool exists for is the one where the resolver
itself hard-fails. A doctor that dies on a broken config cannot see its only
patient — so a failure inside a check becomes a FAILING FINDING carrying the
exception text, never a traceback out of ``main()``.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from shutil import which
from typing import Callable, Optional

OK = "ok"
FAIL = "FAIL"
INFO = "info"
WARN = "warn"

_NL = chr(10)


@dataclass(frozen=True)
class Finding:
    """One check's verdict. ``status`` is OK / FAIL / INFO / WARN.

    INFO is not a soft FAIL — it is for a surface that is legitimately absent (no
    vault configured, no autodoc root). Reporting those as failures would train a
    reader to skim past the output, which is how a diagnostic dies.

    WARN is *resolved, but not as the operator configured it* — a fourth case the
    other three cannot express. Nothing is broken, so it is not FAIL; nothing is
    absent, so it is not INFO; and it is emphatically not OK, because the value in
    force is not the one somebody wrote down. Before this tier existed that case fell
    out of the report entirely and the run still ended "resolved surface healthy".
    For the same reason INFO is not a soft FAIL, **WARN does not fail the exit code**:
    a health check that exits 1 over a clamped preference is one people stop running.
    """

    check: str
    status: str
    detail: str
    remediation: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    @property
    def warned(self) -> bool:
        return self.status == WARN

    @property
    def spec_corpus(self) -> bool:
        """A ``survey:<kind>`` warning: the spec corpus has a problem, not the configuration.

        The verdict names these apart, because "NOT everywhere as configured" over a spec
        with no ``## Files`` section sends the reader to a config home with nothing in it to fix.
        The bare ``survey`` catch-all is excluded on purpose (fail closed): it collects messages
        no kind recognised, which can be config-shaped (an unreadable status file, records from
        another project), so it keeps the louder config verdict.
        """
        return self.warned and self.check.startswith("survey:")


def spec_note(count: int) -> str:
    """The one wording for spec-corpus warnings, shared with install/provision.py's verdict."""
    return f"{count} spec-corpus warning(s) (survey:*)"


def _run(check: str, fn: Callable[[], Finding]) -> Finding:
    """Call *fn*, turning any escape into a failing finding rather than a crash."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — the whole point; see the module docstring
        # No generic remediation here on purpose. A ConfigError already carries its
        # own "remediation:" line inside the message, and a generic override would
        # replace the one useful sentence in the report with a platitude.
        return Finding(check, FAIL, f"{type(exc).__name__}: {exc}")


class _ResolutionWarnings(logging.Handler):
    """Collects config-resolution warnings so the REPORT carries them, not only stderr.

    Same move as ``fleet_report._SkipHandler``: capture the emitting call's warnings
    without touching ``config.py``'s signature. The warning ``config.py`` emits is
    correct and stays exactly as it is — only the reader changes.

    WHY THE REPORT AND NOT JUST STDERR: ``doctor > report.txt`` is the obvious way to
    capture a diagnostic, and it keeps stdout while discarding stderr. So a warning
    that reaches stderr alone is invisible in precisely the artifact somebody saves,
    pastes and reads later — sitting next to a stdout line calling the surface healthy.

    Deduplicates by message. The surface used to be resolved twice per run, which
    printed each warning twice, and a warning printed twice is one a reader learns to
    skip. ``diagnose`` no longer resolves twice; this stays as the backstop for any
    future caller that does.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken log record must not break the doctor
            self.handleError(record)
            return
        if message not in self.messages:
            self.messages.append(message)

    def findings(self) -> list[Finding]:
        return [Finding(_warning_check(m), WARN, m) for m in self.messages]


def _warning_check(message: str) -> str:
    """``config:<dial>`` when the message opens with ``<dial>=<value>``, else ``config``.

    Cosmetic only — the whole message is always the finding's detail. It exists so two
    different clamped dials do not both render as an undifferentiated ``config``.
    """
    head = message.split(" ", 1)[0]
    if "=" in head:
        return "config:" + head.split("=", 1)[0]
    return "config"


#: Survey warnings another check already names in its own finding, as (pattern, owning check).
#: Group 1 is the text that check's detail carries; a message is left out of the survey findings
#: only when that text IS in the owner's detail, so a check that raised, or one that names only
#: some values, drops nothing it did not itself report.
_OWNED_SURVEY_WARNINGS = (
    (re.compile(r"^(done-spec pointer check could not (?:read|parse): .*)$", re.S),
     "done-spec-pointers"),
    (re.compile(r"^done spec pointer does not resolve \([^)]*\): (.+?) \("), "done-spec-pointers"),
    (re.compile(r"^unknown queue_class, not dispatchable: (.+?) \(e\.g\. "), "vocabulary"),
)

#: Message opening -> finding name. Cosmetic, like `_warning_check`: the whole message is always
#: the detail, and anything unrecognised still reports as plain ``survey``.
_SURVEY_KINDS = (
    ("spec has no `## Files` section", "survey:files-missing"),
    ("spec `## Files` section declares no path", "survey:files-pathless"),
    ("unknown queue_class", "survey:queue_class"),
    ("slug collision", "survey:slug-collision"),
    ("frontmatter declares", "survey:frontmatter"),
    ("done-spec pointer check", "survey:done-spec"),
    ("done spec pointer does not resolve", "survey:done-spec"),
    # Pass 3 refuses both (neither done nor selectable) rather than guess a run out of the
    # board, so each is a corpus defect a lane must fix, named after the survey's own state.
    ("duplicate status boards for", "survey:duplicate-status-boards"),
    ("unrecognised board status", "survey:unrecognised-board-status"),
)


class _SurveyWarnings(_ResolutionWarnings):
    """The ``agentflow.epic_survey`` warnings a diagnosis triggers, for the same reason.

    ``_check_vocabulary`` runs the full survey, which warns per spec that a promoting seat
    must refuse (no ``## Files``, a pathless one, a slug collision, an unreadable file). Those
    reached stderr only, so ``doctor > report.txt`` kept a verdict of "healthy" next to a
    corpus the fleet cannot promote.
    """

    def findings(self, reported: Optional[list[Finding]] = None) -> list[Finding]:
        """WARN findings for every message no finding in *reported* already names."""
        by_check = {f.check: f.detail for f in reported or ()}
        return [Finding(_survey_check(m), WARN, m) for m in self.messages
                if not _owned_elsewhere(m, by_check)]


def _owned_elsewhere(message: str, by_check: dict[str, str]) -> bool:
    for pattern, owner in _OWNED_SURVEY_WARNINGS:
        m = pattern.match(message)
        if m:
            return m.group(1) in by_check.get(owner, "")
    return False


def _survey_check(message: str) -> str:
    for opening, check in _SURVEY_KINDS:
        if message.startswith(opening):
            return check
    if ", skipping:" in message:
        return "survey:skipped"
    if "depends_on" in message:
        return "survey:depends_on"
    return "survey"


@contextmanager
def _collecting(logger_name: str, handler: logging.Handler):
    """Attach *handler* to one logger for the duration of one diagnosis.

    Scoped rather than global on purpose: ``diagnose()`` is a library function with two
    callers, and a handler left attached would go on collecting into a dead object for
    whoever imported the module next.

    The level is pinned to WARNING for the same window, as ``fleet_report`` does for this
    logger: NOTSET inherits, and a host that set the root above WARNING would drop the very
    records the report exists to carry, putting "healthy" back over an unpromotable corpus.
    """
    logger = logging.getLogger(logger_name)
    previous_level = logger.level
    if previous_level > logging.WARNING or previous_level == logging.NOTSET:
        logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _collecting_config_warnings():
    return _collecting("agentflow.config", _ResolutionWarnings())


def _check_environment(resolved: dict, config_home=None) -> Finding:
    from agentflow.config import load_environment

    env = load_environment(config_home=config_home)
    # Hand the resolved object back to `diagnose` rather than have it resolve again.
    # Every resolution re-emits every warning, so a second one is not merely wasted work.
    resolved["env"] = env
    return Finding("environment", OK, f"resolves; data_root={env.data_root}")


def _check_paths(env) -> list[Finding]:
    out = []
    for field in ("data_root", "lock_dir", "suite_root", "worktree_root"):
        value = getattr(env, field, None)

        def one(field=field, value=value) -> Finding:
            if value is None:
                return Finding(f"path:{field}", FAIL, "unset", f"set {field} in environment.md")
            p = Path(value)
            if not p.is_dir():
                return Finding(
                    f"path:{field}",
                    FAIL,
                    f"not a directory: {p}",
                    f"create it, or point {field} somewhere real",
                )
            return Finding(f"path:{field}", OK, str(p))

        out.append(_run(f"path:{field}", one))
    return out


def _resolve_project_name(project: Optional[str], resolved: dict, config_home=None) -> Finding:
    from agentflow.config import resolve_project_name

    resolved["name"] = resolve_project_name(project, config_home=config_home)
    return Finding("project", OK, f"selected {resolved['name']!r}")


def _check_project(name: str, resolved: dict, config_home=None) -> Finding:
    from agentflow.config import load_project

    proj = load_project(name, config_home=config_home)
    resolved["proj"] = proj   # same one-resolution rule as _check_environment above
    return Finding(f"project:{name}", OK, f"resolves; repo={proj.canonical_repo.host_ref}")


def _check_checkout(proj) -> Finding:
    checkout = Path(proj.canonical_repo.local_checkout)
    if not checkout.is_dir():
        return Finding(
            "checkout", FAIL, f"missing: {checkout}",
            "clone the repo, or fix canonical_repo.local_checkout",
        )
    if not (checkout / ".git").exists():
        return Finding(
            "checkout", FAIL, f"not a git repository: {checkout}",
            "point canonical_repo.local_checkout at a clone",
        )
    return _check_origin(checkout, proj.canonical_repo.host_ref)


#: Seconds a `git remote` probe may take before the origin counts as unmeasured.
_GIT_PROBE_TIMEOUT = 30


class _Unmeasured(Exception):
    """git could not answer, so the origin was neither confirmed nor refuted."""


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    """`git -C <checkout> <args>`, or `_Unmeasured` when git cannot be run at all.

    GIT_CEILING_DIRECTORIES stops git's upward search at the checkout's parent, so a
    `.git` git refuses is reported as unmeasured rather than answered by whatever
    repository happens to enclose the checkout.
    """
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(checkout.parent)}
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True, text=True, timeout=_GIT_PROBE_TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        raise _Unmeasured(f"git timed out after {_GIT_PROBE_TIMEOUT}s") from None
    except OSError as exc:
        raise _Unmeasured(f"git could not run: {type(exc).__name__}") from None


def _git_said(done: subprocess.CompletedProcess) -> str:
    # git's own words, minus any URL credentials it might have quoted.
    said = _strip_userinfo((done.stderr.strip().splitlines() or [""])[0])
    return f"git exited {done.returncode}: {said}".rstrip(": ")


def _git_remote(checkout: Path, *args: str) -> str:
    """stdout of `git -C <checkout> remote <args>`, or `_Unmeasured` when git cannot answer."""
    done = _git(checkout, "remote", *args)
    if done.returncode != 0:
        raise _Unmeasured(_git_said(done))
    return done.stdout


def _ref_exists(checkout: Path, ref: str) -> bool:
    """Whether *ref* exists under exactly that name; `_Unmeasured` when git cannot answer.

    `show-ref --verify` takes a full refname, not revision syntax, so `main~1` is absent
    rather than resolved to a commit. It exits 1, silently, for an absent ref; anything
    else is git failing to answer, not answering no. It still opens a loose ref by path,
    so on a case-insensitive filesystem `MAIN` finds `main`; `for-each-ref` lists the
    names as stored, and an exact match there is what settles the spelling.
    """
    done = _git(checkout, "show-ref", "--verify", "--quiet", ref)
    if done.returncode == 1:
        return False
    if done.returncode != 0:
        raise _Unmeasured(_git_said(done))
    listed = _git(checkout, "for-each-ref", "--format=%(refname)", ref)
    if listed.returncode != 0:
        raise _Unmeasured(_git_said(listed))
    return ref in listed.stdout.splitlines()


def _check_base_branch(proj) -> Finding:
    """canonical_repo.base_branch must name a branch the checkout has (gh#745).

    Every worktree is cut from it and every PR targets it, so a name that resolves
    nowhere fails the first build, far from the profile line that caused it. A remote
    tracking ref counts: a fresh clone has only the default branch locally.

    A branch only in refs/heads WARNs when the checkout has an origin: the seats that
    read `origin/<base_branch>` find nothing there, though a local build would work.
    """
    checkout = Path(proj.canonical_repo.local_checkout)
    branch = proj.canonical_repo.base_branch
    if not (checkout / ".git").exists():
        return Finding("base_branch", INFO, f"{branch!r} not checked - no git checkout to look in")
    refs = (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")
    try:
        found = [ref for ref in refs if _ref_exists(checkout, ref)]
        if found == [refs[0]] and "origin" in _git_remote(checkout).split():
            return Finding(
                "base_branch", WARN,
                f"{branch} exists only as {refs[0]}; the checkout has an origin but no "
                f"{refs[1]}, which seats that read origin/{branch} need",
                f"push {branch} to origin (or fetch, if it is already there)",
            )
        if found:
            return Finding("base_branch", OK, f"{branch} ({found[0]})")
    except _Unmeasured as why:
        return Finding("base_branch", OK, f"{branch} (not verified: {why})")
    return Finding(
        "base_branch", FAIL,
        f"{branch!r} resolves in {checkout} as neither {refs[0]} nor {refs[1]}",
        "set canonical_repo.base_branch to an existing branch",
    )


def _strip_userinfo(text: str) -> str:
    """*text* with every `scheme://user:secret@` reduced to `scheme://`.

    Greedy to the LAST `@` before the path, so a password that itself holds an `@`
    leaves no fragment behind.
    """
    return re.sub(r"(?<=://)[^/\s]+@", "", text)


def _origin_ref(url: str) -> Optional[str]:
    """The `owner/name` an origin URL points at, or None when it does not name one.

    Handles `scheme://[userinfo@]host[:port]/.../owner/name[.git]`, scp-style
    `[user@]host:owner/name[.git]`, and a local path. The authority, where credentials
    live, is dropped before anything is kept, so the result can never carry a secret.
    The last two path segments are the ref, which is how `owner/repo` host refs read
    on hosts that serve from a sub-path. A query or fragment is cut first: neither
    names the repository, and a query can carry a token.
    """
    rest = re.split(r"[?#]", url.strip(), maxsplit=1)[0]
    if "://" in rest:
        rest = rest.split("://", 1)[1].partition("/")[2]
    elif re.match(r"^[^/\\]+:", rest) and not re.match(r"^[A-Za-z]:[/\\]", rest):
        rest = rest.split(":", 1)[1]
    parts = [p for p in re.split(r"[/\\]", rest) if p]
    if len(parts) < 2:
        return None
    owner, name = parts[-2], parts[-1]
    if "@" in owner or "@" in name:
        return None
    return f"{owner}/{name[:-4] if name.endswith('.git') else name}"


def _check_origin(checkout: Path, expected: str) -> Finding:
    """SCHEMA: local_checkout's `origin` must resolve to canonical_repo.host_ref (gh#692).

    * git cannot answer - `ok`, marked not verified. Same stance as the autodoc import
      probe: a false WARN is worse than an unverified line.
    * no origin - WARN. The orchestrator runs a checkout with no origin as a supported
      local-only repo (skills/orchestrator/SKILL.md, step 7b: "no origin -> local-only
      repo. Skip push + PR machinery"), so it is not broken, but nothing confirms the
      checkout is the repo the profile names.
    * origin naming another repo - FAIL. The fleet pushes branches and opens PRs through
      that remote, so every one of them would land on the wrong repository.

    Only `owner/name` is ever printed, never the URL, which can carry a token.
    """
    try:
        if "origin" not in _git_remote(checkout).split():
            return Finding(
                "checkout", WARN,
                f"{checkout} has no origin remote, so nothing confirms it is {expected}",
                f"add an origin for {expected} if the project is hosted; without one the "
                "orchestrator treats it as local-only and skips push and PR",
            )
        url = _git_remote(checkout, "get-url", "origin").strip()
    except _Unmeasured as why:
        return Finding("checkout", OK, f"{checkout} (origin not verified: {why})")
    actual = _origin_ref(url)
    if actual is None:
        return Finding(
            "checkout", WARN,
            f"{checkout} origin does not name an owner/repo, so it cannot be compared with "
            f"{expected}",
            "point origin at the repo canonical_repo.host_ref names",
        )
    if not _same_repo(actual, expected):
        return Finding(
            "checkout", FAIL,
            f"{checkout} origin is {actual}, but canonical_repo.host_ref is {expected}",
            f"point origin at {expected}, or fix canonical_repo.host_ref if {actual} is right",
        )
    return Finding("checkout", OK, str(checkout))


def _same_repo(a: str, b: str) -> bool:
    """Whether two host refs name the same repository.

    Owner and repo names are case-insensitive on the hosts this suite targets, and a
    trailing ``.git`` is a clone-URL suffix rather than part of the name. Both forms
    clear the resolver's ``owner/repo`` shape check, so both reach here.
    """
    def norm(ref: str) -> str:
        ref = ref.strip().casefold()
        return ref[:-4] if ref.endswith(".git") else ref
    return norm(a) == norm(b)


def _check_publish(proj) -> Finding:
    """Report the working/publish split, so a misconfiguration needs no YAML reading.

    Three outcomes, and the middle one is the point of the check:

    * no ``publish_repo`` — INFO, not FAIL. Absent is the normal case for every project
      that has no public repo, and calling that a failure is how a reader learns to skim.
    * ``publish_repo`` equal to the working repo — FAIL. The split is then a fiction: the
      "public" target IS the private repo, and the next en-masse push publishes exactly
      the history the split exists to keep private. This is the misconfiguration you
      cannot see by reading two lines of YAML that are far apart.
    * a distinct target — OK, printed as ``working -> publish`` so the direction is on
      the page and cannot be inferred backwards.

    "Equal" is decided on the NORMALISED refs, not the literal strings. Git host owner
    and repo names are case-insensitive and ``owner/repo.git`` is the same repo as
    ``owner/repo``, so a hand-edited profile that names the working repo with the casing
    the host UI displays produces two strings and one repo. Compared literally that
    reports OK and prints ``working owner/repo -> publish Owner/Repo``, which reads to a
    human as a split — and the runbook's precondition tells the publisher to trust
    exactly this line before an unrevertible push. A check that reassures about the
    misconfiguration it exists to catch is worse than no check.
    """
    working = proj.canonical_repo.host_ref
    publish = proj.publish_repo
    if publish is None:
        return Finding("publish", INFO, f"working repo {working}; no publish target configured")
    if _same_repo(publish.host_ref, working):
        return Finding(
            "publish", FAIL,
            f"publish_repo {publish.host_ref} is the working repo — the split is a fiction",
            "point publish_repo at the PUBLIC repo, or delete the key until one exists",
        )
    return Finding(
        "publish", OK,
        f"working {working} -> publish {publish.host_ref} "
        f"({publish.cadence}; human-driven, no loop pushes there)",
    )


def _check_registry(env, name: str) -> Finding:
    path = Path(env.data_root) / "projects" / name / "status" / "registry.md"
    if not path.is_file():
        return Finding(
            "registry", FAIL, f"missing: {path}",
            "run provision, or create the registry for this project",
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    missing = [s for s in ("## Agents", "## Queue", "## Questions", "## Blockers", "## Directives")
               if s not in text]
    if missing:
        return Finding(
            "registry", FAIL, f"{path} is missing section(s): {missing}",
            "the registry schema requires all five sections",
        )
    return Finding("registry", OK, str(path))


def _literal_prefix(glob: str) -> str:
    """The leading path segments of *glob* that carry no wildcard - the directory it targets.

    ``projects/demo/design/specs/*.md`` targets ``projects/demo/design/specs``; a glob
    with no wildcard at all (``projects/demo/status``) targets itself.
    """
    parts = glob.split("/")
    for i, part in enumerate(parts):
        if any(ch in part for ch in "*?["):
            return "/".join(parts[:i])
    return glob


def _check_globs(env, name: str) -> list[Finding]:
    from agentflow.epic_survey import DEFAULT_SURVEY_PATTERNS

    root = Path(env.data_root)
    project_dir = root / "projects" / name

    def subtree() -> Finding:
        if project_dir.is_dir():
            return Finding("glob", OK, str(project_dir))
        # No project subtree at all is an install step that never ran for this project,
        # not a layout that moved. Three per-glob FAILs saying "one of them moved" would
        # send the reader after a migration that never happened (gh#745).
        return Finding(
            "glob", FAIL, f"{project_dir} does not exist, so no document glob can match",
            "project subtree not scaffolded - re-run python install/provision.py",
        )

    # Guarded like the per-glob probes below: diagnose() is documented never to raise.
    root_finding = _run("glob", subtree)
    if root_finding.status != OK:
        return [root_finding]
    scoped = DEFAULT_SURVEY_PATTERNS.for_project(name)
    out = []
    for label, globs in (
        ("spec", scoped.spec_globs),
        ("plan", scoped.plan_globs),
        ("status", scoped.status_dir_globs),
    ):

        def one(label=label, globs=globs) -> Finding:
            hits = [p for g in globs for p in root.glob(g)]
            if hits:
                return Finding(f"glob:{label}", OK, f"{len(hits)} match(es)")
            # Zero hits is two different states, and only one is a failure (gh#416). The
            # directory the family targets EXISTING is a layout that agrees with the globs
            # and simply holds nothing yet - the state every fresh install ships in, since
            # the installer scaffolds that directory. The directory being ABSENT is the
            # layout having moved out from under the defaults, which is what this check
            # exists to catch, so that stays a FAIL.
            target = root / _literal_prefix(globs[0])
            if target.is_dir():
                return Finding(
                    f"glob:{label}", INFO,
                    f"{target} exists but holds nothing yet",
                    "nothing to fix - it fills as the first documents are written",
                )
            return Finding(
                f"glob:{label}", FAIL,
                f"no files match {list(globs)} under {root}, and {target} does not exist",
                "the layout and the glob defaults disagree — one of them moved",
            )

        out.append(_run(f"glob:{label}", one))
    return out


# gh#254 thresholds. A vocabulary cut lands in the CODE when the checkout updates and in the
# CORPUS when the migration runs, and either order leaves specs carrying a value the running
# build rejects. The survey already warns once per unknown value - but that warning reads the
# same whether one spec has a typo or the whole project is mid-migration, which is the thing a
# reader needs to tell apart. The RATIO is what separates them: two independent typos cannot
# both reach half of one corpus, while a migration takes nearly all of it at once (the case in
# gh#252 was 93 of 108). The floor keeps a two-spec project from reporting a migration because
# one spec is wrong.
_VOCAB_MASS_SHARE = 0.5
_VOCAB_MASS_FLOOR = 3


def _is_vocab_mass(n: int, total: int) -> bool:
    return n >= _VOCAB_MASS_FLOOR and n / total >= _VOCAB_MASS_SHARE


def _named(values: list[str]) -> str:
    return ", ".join(repr(v) for v in values) + (" is" if len(values) == 1 else " are")


def _check_vocabulary(env, name: str) -> Finding:
    """Tell a corpus-wide queue_class migration apart from a stray bad value (gh#254)."""
    from agentflow.epic_survey import KNOWN_QUEUE_CLASSES, survey_epics
    from agentflow.vocabulary import QUEUE_CLASS_REWRITES

    records = survey_epics(env.data_root, env.mtime_cutoff_iso, project=name)
    declared = [r for r in records if r.queue_class is not None]
    if not declared:
        return Finding("vocabulary", OK, "no epic declares a queue_class")

    counts: dict[str, int] = {}
    for r in declared:
        if r.queue_class not in KNOWN_QUEUE_CLASSES:
            counts[r.queue_class] = counts.get(r.queue_class, 0) + 1
    if not counts:
        return Finding(
            "vocabulary", OK, f"{len(declared)} queue_class value(s), all in vocabulary"
        )

    # The ratio decides severity; membership decides the remedy. `migrate_queue_class`
    # rewrites only the keys of QUEUE_CLASS_REWRITES, so sending a value outside it there is
    # a run that changes nothing and leaves this check red, and withholding the command from
    # a handful of retired values sends the reader to hand-edit what the migration exists to
    # do. A value outside the map on a mass of specs is a vocabulary newer than this
    # checkout, which only updating the checkout fixes.
    total = len(declared)
    retired = sorted(v for v in counts if v in QUEUE_CLASS_REWRITES)
    unknown = sorted(v for v in counts if v not in QUEUE_CLASS_REWRITES)
    ahead = [v for v in unknown if _is_vocab_mass(counts[v], total)]
    stray = [v for v in unknown if v not in ahead]
    remedies = []
    if retired:
        remedies.append(
            f"{_named(retired)} retired: run `python -m agentflow.migrate_queue_class "
            f"--project {name} --dry-run` and then `--apply`, which rewrites each to its current "
            f"value - not by hand"
        )
    if ahead:
        remedies.append(
            f"{_named(ahead)} on too many specs to be a typo, yet neither known nor retired "
            f"by this build: the vocabulary is newer than this checkout, so update the "
            f"checkout so the code that knows it is the code running (the migration has "
            f"nothing to rewrite)"
        )
    if stray:
        remedies.append(
            f"{_named(stray)} neither known nor retired, on too few specs to be a vocabulary "
            f"change: fix the specs themselves"
        )
    remedy = "; ".join(remedies)

    value, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if _is_vocab_mass(n, total):
        others = sorted(v for v in counts if v != value)
        return Finding(
            "vocabulary", FAIL,
            f"{n} of {total} epic(s) declaring a queue_class carry {value!r}, which "
            f"this build does not know"
            + ("; also " + ", ".join(f"{v!r} x{counts[v]}" for v in others) if others else ""),
            "that is a VOCABULARY CHANGE, not a typo: the corpus and the running code "
            f"disagree about the vocabulary. {remedy}",
        )
    return Finding(
        "vocabulary", WARN,
        "out-of-vocabulary queue_class on "
        f"{sum(counts.values())} of {total} epic(s): "
        + ", ".join(f"{v!r} x{c}" for v, c in sorted(counts.items())),
        remedy,
    )


class _SkippedSpecs(logging.Handler):
    """Collects the specs the done-spec resolver could not read or parse (gh#365 review).

    The resolver names them on the ``agentflow.epic_survey`` logger only. During ``diagnose()``
    that logger's records go to the survey collector, which leaves these two shapes to this
    check (``_OWNED_SURVEY_WARNINGS``); without this handler the report would say "every
    pointer resolves" about a corpus in which some specs were never checked.
    Keyed on the message prefix the resolver uses for exactly those two skips.
    """

    _PREFIXES = ("done-spec pointer check could not read:", "done-spec pointer check could not parse:")

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.skipped: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken log record must not break the doctor
            self.handleError(record)
            return
        if message.startswith(self._PREFIXES) and message not in self.skipped:
            self.skipped.append(message)


def _check_done_spec_pointers(env, proj, name: str) -> Finding:
    """Done specs whose declared paths stopped resolving (gh#365).

    `warn_unresolved_done_spec_paths` existed with no caller, so a spec's evidence trail still
    died unreported the moment it moved into `done/`. This is that caller: the steward runs
    `diagnose` every wake, and a terminal user can run it on demand. The check walks the corpus
    once, which is why it lives here and not in `survey_epics` (see that function's docstring).

    WARN, not FAIL: the record is historical and nothing the fleet runs depends on it, so it
    must not turn the exit code red. Every pointer is named in the detail, because each one is a
    separate edit and the report is the artifact somebody saves.
    """
    from agentflow.epic_survey import POINTER_DEPTH_SHIFT, warn_unresolved_done_spec_paths

    checkout = Path(proj.canonical_repo.local_checkout)
    # `load_project` already requires the checkout to exist, so this only guards the race of it
    # being removed mid-run. Without it, `## Files` entries cannot be judged, only provenance.
    repo = checkout if checkout.is_dir() else None
    handler = _SkippedSpecs()
    logger = logging.getLogger("agentflow.epic_survey")
    logger.addHandler(handler)
    try:
        found = warn_unresolved_done_spec_paths(env.data_root, repo_root=repo, project=name)
    finally:
        logger.removeHandler(handler)
    scope = "" if repo is not None else " (`## Files` not judged: checkout missing)"
    skipped = handler.skipped
    if not found and not skipped:
        return Finding("done-spec-pointers", OK, f"every done-spec pointer resolves{scope}")
    parts = []
    if found:
        shifted = sum(1 for u in found if u.kind == POINTER_DEPTH_SHIFT)
        listed = "; ".join(f"{u.slug} {u.source} {u.pointer!r} ({u.kind})" for u in found)
        parts.append(
            f"{len(found)} declared path(s) in done specs do not resolve ({shifted} "
            f"depth-shift, {len(found) - shifted} unresolved){scope}: {listed}"
        )
    else:
        parts.append(f"no broken pointer in the specs that were checked{scope}")
    if skipped:
        parts.append(f"{len(skipped)} spec(s) NOT checked: " + "; ".join(skipped))
    remediation = []
    if found:
        remediation.append(
            "depth-shift: add one `../` (the spec moved into done/ after it was written); "
            "unresolved: renamed, deleted or mistyped - fix that pointer by hand"
        )
    if skipped:
        remediation.append("unchecked: re-save that spec as valid UTF-8 with parseable frontmatter")
    return Finding("done-spec-pointers", WARN, " | ".join(parts), "; ".join(remediation))


#: Seconds the fresh-interpreter import probe may take before its answer counts as unknown.
_GRAPH_PROBE_TIMEOUT = 30


def _graph_importable(module: str = "autodoc_graph") -> Optional[bool]:
    """Can a FRESH interpreter of this venv import *module*? True / False / None (unknown).

    Not an in-process ``find_spec``, because of provision.py's closing report (gh#620 review):
    it runs doctor in-process, in the venv child whose ``sys.path`` was fixed at start, and
    step 6's ``pip install -e`` writes an editable ``.pth`` that only a NEW interpreter reads.
    ``find_spec`` there says "not installed" about a package installed seconds earlier.

    A probe that times out answers None - a false WARN is worse than an unverified line. Only
    when no interpreter can be started at all does it fall back to ``find_spec``.
    """
    try:
        done = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True, timeout=_GRAPH_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return find_spec(module) is not None
    return done.returncode == 0


def _check_autodoc(env, proj) -> Finding:
    from agentflow.config import resolve_autodoc_root

    root = resolve_autodoc_root(env, proj)
    if root is None:
        return Finding("autodoc", INFO, "no autodoc root configured or derived")
    initialised = (Path(root) / ".manifest.json").is_file()
    # A root is not a runnable autodoc (gh#620). Install step 6 - `npm ci` plus the editable
    # autodoc_graph install - runs only when autodoc was on AT PROVISION TIME, and the install
    # interview says to leave autodoc_root null. Set it afterwards and this line used to say
    # "run autodoc-init", which then died after the clone on `python -m autodoc_graph.cli`.
    # Probed only once a root resolves: an install that never enabled autodoc owes neither.
    missing = []
    graph = _graph_importable()
    if graph is False:
        missing.append("the autodoc_graph package")
    if which("node") is None:
        missing.append("node")
    unverified = (
        " (autodoc_graph import probe timed out; not verified)" if graph is None else ""
    )
    if missing:
        # A bare re-run also re-runs step 8 and writes back auto-allow hooks the operator
        # declined (docs/INSTALL.md section 13), so the flag rides along in the remedy.
        remedy = (
            "install Node LTS (with npm) if it is absent, then re-run "
            "`python install/provision.py --autodoc` from the agentflow checkout, which runs "
            "install step 6 (npm ci, then the autodoc_graph install into the suite venv); add "
            "`--no-auto-allow` if you declined the auto-allow hooks, or the re-run puts them back"
        )
        if not initialised:
            remedy += "; only then run autodoc-init"
        # WARN, not FAIL: the pipeline runs without autodoc, so an optional surface must not
        # turn the exit code red. Not INFO either: somebody configured a root, so the surface
        # resolves but cannot be used as configured - which is the case WARN exists for.
        state = "initialised" if initialised else "no .manifest.json yet"
        return Finding(
            "autodoc", WARN,
            f"{root} ({state}) but not runnable here: missing {' and '.join(missing)}",
            remedy,
        )
    if not initialised:
        return Finding(
            "autodoc", INFO, f"{root} (no .manifest.json — never initialised){unverified}",
            "run autodoc-init if this project should have a code-wiki",
        )
    return Finding("autodoc", OK, f"{root}{unverified}")


def _check_vault(env, proj) -> list[Finding]:
    vault = getattr(proj, "vault", None) or getattr(env, "vault", None)
    if vault is None:
        # "No vault" is a legitimate answer, so this stays INFO. But an install that was
        # never ASKED and one that answered no arrive here identically as vault=None, and
        # the difference matters: the first is an unfinished interview, the second is a
        # decision. `vault_declined` is what tells them apart - an explicitly empty `vault`
        # key. Until it existed the convention was documented in three places and read
        # nowhere, so this line re-offered candidates every run to somebody who had already
        # said no, which is how a diagnostic teaches people to skip it.
        from agentflow.config import vault_declined

        if vault_declined(env, proj):
            return [Finding(
                "vault", INFO,
                "no vault - recorded as declined (`vault: null`)",
            )]
        # Imported here, matching this module's lazy-import style, and NOT wrapped in a
        # try/except. `find_vault_candidates` is documented never to raise, so a guard here
        # could only ever hide a mistake of mine - and it did: the first version called it
        # with no import at all, and a broad except turned that NameError into an empty
        # list. A silently empty offer list is indistinguishable from a machine with no
        # vaults on it.
        from agentflow.config import find_vault_candidates

        candidates = find_vault_candidates()
        if not candidates:
            return [Finding("vault", INFO, "no vault configured; none found on disk either")]
        shown = ", ".join(candidates[:3])
        more = f" (+{len(candidates) - 3} more)" if len(candidates) > 3 else ""
        return [Finding(
            "vault", INFO,
            f"no vault configured; {len(candidates)} found on disk: {shown}{more}",
            "set `vault.root`, or write `vault: null` to record that the answer is no",
        )]
    out = []

    def root_check() -> Finding:
        p = Path(vault.root)
        if not p.is_dir():
            return Finding("vault:root", FAIL, f"not a directory: {p}", "fix vault.root")
        if not os.access(p, os.W_OK):
            return Finding("vault:root", FAIL, f"not writable: {p}", "fix permissions on vault.root")
        return Finding("vault:root", OK, str(p))

    out.append(_run("vault:root", root_check))

    helper = getattr(vault, "mirror_helper", None)
    if helper:

        def helper_check() -> Finding:
            p = Path(helper)
            if not p.is_file():
                return Finding(
                    "vault:mirror_helper", FAIL, f"missing: {p}", "fix vault.mirror_helper",
                )
            return Finding("vault:mirror_helper", OK, str(p))

        out.append(_run("vault:mirror_helper", helper_check))
    return out


def diagnose(project: Optional[str] = None, *, config_home=None) -> list[Finding]:
    """Every check, as data. Never raises — see the module docstring.

    Config-resolution and epic-survey warnings are collected for the whole run and appended
    as WARN findings at the end, so a clamped dial or an unpromotable spec reaches the report
    instead of only stderr. A survey warning a check already names is not repeated.

    *config_home* overrides ``$AGENTFLOW_CONFIG`` for this run's config reads (the installer
    passes the home it provisioned), so no caller has to pin the process-global variable.
    """
    with _collecting_config_warnings() as warnings, \
            _collecting("agentflow.epic_survey", _SurveyWarnings()) as survey:
        findings = _checks(project, config_home)
    return findings + warnings.findings() + survey.findings(findings)


def _checks(project: Optional[str], config_home=None) -> list[Finding]:
    """The checks themselves. Split out so `diagnose` can wrap the whole run once."""
    findings: list[Finding] = []
    # Every resolved object is produced ONCE, here, and reused by the checks that need
    # it. Resolving a second time re-runs every validation and re-emits every warning.
    resolved: dict = {}
    env_finding = _run("environment", lambda: _check_environment(resolved, config_home))
    findings.append(env_finding)
    if env_finding.failed:
        # Nothing below can be trusted without a resolved environment, and guessing
        # would print a page of derived failures that all restate this one.
        findings.append(
            Finding("(remaining checks)", INFO,
                    "skipped — they all read the environment that just failed")
        )
        return findings

    env = resolved["env"]
    findings.extend(_check_paths(env))

    # The suite's one resolver, not a direct read of $AGENTFLOW_PROJECT: it also honours
    # the single-projects/<name>.md default, which is exactly the state provision.py ships
    # in - so reading the variable alone made the installer's closing self-check examine
    # no project at all and still call the surface healthy (gh#416).
    name_finding = _run("project", lambda: _resolve_project_name(project, resolved, config_home))
    if name_finding.failed:
        findings.append(name_finding)
        return findings
    name = resolved["name"]
    if not name:
        findings.append(
            Finding("project", INFO, "no project selected — pass --project to check one")
        )
        return findings

    proj_finding = _run(f"project:{name}", lambda: _check_project(name, resolved, config_home))
    findings.append(proj_finding)
    if proj_finding.failed:
        findings.append(Finding("(project checks)", INFO, f"skipped — {name} does not resolve"))
        return findings

    proj = resolved["proj"]
    findings.append(_run("checkout", lambda: _check_checkout(proj)))
    findings.append(_run("base_branch", lambda: _check_base_branch(proj)))
    findings.append(_run("publish", lambda: _check_publish(proj)))
    findings.append(_run("registry", lambda: _check_registry(env, name)))
    findings.extend(_check_globs(env, name))
    findings.append(_run("vocabulary", lambda: _check_vocabulary(env, name)))
    findings.append(_run("done-spec-pointers", lambda: _check_done_spec_pointers(env, proj, name)))
    findings.append(_run("autodoc", lambda: _check_autodoc(env, proj)))
    findings.extend(_check_vault(env, proj))
    return findings


def render(findings: list[Finding]) -> str:
    """One line per check, with the failures restated at the end rather than buried."""
    width = max((len(f.check) for f in findings), default=0)
    lines = [f"{f.status:<4} {f.check:<{width}}  {f.detail}" for f in findings]
    bad = [f for f in findings if f.failed]
    warned = [f for f in findings if f.warned]
    lines.append("")
    if bad:
        lines.append(f"{len(bad)} FAILING:")
        lines.extend(f"  {f.check}: {f.remediation or f.detail}" for f in bad)
    if warned:
        lines.append(f"{len(warned)} WARNING:")
        lines.extend(f"  {f.check}: {f.remediation or f.detail}" for f in warned)
    spec = [f for f in warned if f.spec_corpus]
    if not bad and not warned:
        lines.append("resolved surface healthy")
    elif not bad and len(spec) == len(warned):
        # Only the spec corpus is wrong. Saying "NOT everywhere as configured" here sends the
        # reader to a config home that holds nothing to fix.
        lines.append(f"resolved surface healthy and configured as intended; {spec_note(len(spec))}"
                     " — see above")
    elif not bad:
        # The verdict has to carry the qualification, because the verdict is the line
        # people read. "healthy" on its own, under a warning saying a dial the operator
        # set is not the dial in force, is the contradiction this whole tier exists for.
        verdict = "resolved surface healthy, but NOT everywhere as configured"
        if spec:
            verdict += f"; also {spec_note(len(spec))}"
        lines.append(f"{verdict} — see the warning(s) above")
    return _NL.join(lines) + _NL


def examined_a_project(findings: list[Finding]) -> bool:
    """Whether *findings* cover a project surface, not just environment and paths."""
    return any(f.check.startswith("project:") for f in findings)


def unexamined_verdict(report: str, projects_dir: Path) -> str:
    """*report* with a healthy verdict replaced, when no project surface was examined.

    `render()` computes its verdict from the findings it was handed and knows nothing of
    scope, so a run that selected no project still ended "resolved surface healthy" over a
    checkout, registry and globs it never looked at (gh#693, gh#745). Only a healthy
    verdict is replaced: a FAILING list is true whatever the scope, and stays as it is.
    Shared with install/provision.py's closing check, so the two cannot word it apart.
    """
    body, _, verdict = report.rstrip(_NL).rpartition(_NL)
    if not verdict.startswith("resolved surface healthy"):
        return report
    # A spec-corpus-only verdict never reaches here: the survey runs inside the per-project checks.
    qualifier = " (see the warning(s) above)" if "NOT everywhere" in verdict else ""
    # The package's one enumeration of configured projects, which is also the candidate set
    # the resolver counts when it declines to pick one - so the number printed here is the
    # number that made the selection fail.
    from agentflow.config import configured_project_names

    profiles = configured_project_names(projects_dir.parent)
    if not profiles:
        scope = (f"no project checked - no projects/*.md under {projects_dir}, so no "
                 "checkout, registry or glob was examined")
    else:
        scope = (f"{len(profiles)} profile(s) configured under {projects_dir}, none selected "
                 "- pass --project <name> to check one")
    return f"{body}{_NL}environment and paths only{qualifier}: {scope}{_NL}"


def main(argv: Optional[list[str]] = None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="python -m agentflow.doctor",
        description="Diagnose the resolved agentflow surface.",
    )
    ap.add_argument(
        "--project", default=None,
        help="project to check (default: AGENTFLOW_PROJECT, else the single projects/<name>.md)",
    )
    args = ap.parse_args(argv)
    findings = diagnose(args.project)
    report = render(findings)
    if not examined_a_project(findings):
        from agentflow.config import config_home

        report = unexamined_verdict(report, config_home() / "projects")
    # This write is outside every per-check guard, and a config value, project name or
    # user folder reaches the report verbatim - so on a legacy cp1252 console a single
    # non-cp1252 character used to raise out of main() (gh#416). Same forcing as
    # config.py's `show`, now the shared agentflow._stdio helper called at the top of
    # main() (gh#574); a stream without reconfigure (a test capture) is left alone.
    sys.stdout.write(report)
    # FAIL alone decides the exit code. A WARN is reported loudly and exits 0 on
    # purpose - see Finding's docstring. Anything that reads this as an oversight
    # should read the test that pins it before changing it.
    return 1 if any(f.failed for f in findings) else 0


if __name__ == "__main__":  # pragma: no cover — thin CLI shim
    raise SystemExit(main())
