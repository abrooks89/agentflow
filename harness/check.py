#!/usr/bin/env python3
"""Deterministic acceptance checker for the agentflow harness.

What it proves today, on any clone with ``pip install -e skills/_shared``:

- ``--self-test``: the tracked example config loads, ``parse_plan`` imports, every
  assertion in the registry is wired and runs crash-free against a synthesized
  records-absent world, and the auditor ship gate is green. On a machine that holds
  recordings, step 4d also runs every assertion over them for crashes only.
- ``<config> --only C,F,G``: scenarios C, F and G assert end to end against tracked
  fixtures alone (C4 and C7 SKIP there, each with its reason).

Scenarios A, B and D are PROPERTY assertions over whatever recording is on disk
(``records/`` + ``stubs/schedulewakeup_log.md``, gitignored, written by a live
session per ``run-scenario.md``). Nothing here compares against a stored baseline:
proving behaviour preserved after a change means re-recording A, B and D and
re-running this checker over the fresh recording.

History: the harness was built for the Phase-0 port of the skills into the
``agentflow`` package. That port is done, so the pre-port import path and the
``resolver_cmd`` switch that selected it are gone; a config that still sets
``resolver_cmd`` is read without error and the key is ignored with a WARN.

It emits exactly one ``PASS/FAIL/SKIP <id>: <detail>`` line per assertion id,
then exits:

    0  every in-scope assertion passed or honestly SKIPped
    1  at least one BEHAVIORAL assertion FAILed (structural-only failures warn,
       never fail the exit code)
    2  usage error: argparse, an unknown --only group, a config passed alongside
       --self-test, or a config file that is missing, not readable JSON, not a
       JSON object, or missing a required key (reason on stderr)
    3  no behavioral FAIL, but a scenario selected by --only (all, by default) was
       never recorded on this machine: its rows SKIP via `_unrecorded` and a
       `# unrecorded:` line names each missing artifact (gh#628)

A missing recording is SKIP, never FAIL; a recording that exists and is wrong is
FAIL. Exit 1 therefore always means a real regression, exit 3 only a debt.

It reads every path / skill / command name by indirection from
``harness.config.json`` - nothing personal or environmental is hardcoded - and
imports ``parse_plan`` from the installed ``agentflow`` package.

CLI
---
    check.py --self-test                 validate wiring with records synthesized absent
                                         (+ a crash-only pass over any recordings on disk)
    check.py <config.json>               run every assertion
    check.py <config.json> --only A      run one scenario group
    check.py <config.json> --only C,F,G  run several (the reconstructable ones)
    check.py --auditor-scenario          run the auditor ship gate (no config, no records)

Record layout (this file is the contract the P0-T6..T10 recorders follow)
-------------------------------------------------------------------------
    records/scenarioA/plan.md            solution-emitted plan (A2-A9)
    records/scenarioB/status.md          orchestrator status board (B2, B5-B7, E2)
    records/scenarioB/wave-evidence.json {worktree_path, commits:[{sha,files:[]}],
                                          pytest_head:{result}}   (B2-B4, B8)
    records/scenarioC/                    MUST NOT contain a status.md (C3)
    records/scenarioD/registry-after.md  post-wake registry snapshot (D4)
    stubs/schedulewakeup_log.md          pipe-delimited wake rows (B9, C4, D1-D3)

Path relativity in wave-evidence.json is part of that contract, not a detail. Each
`commits[].files` entry MUST be either worktree-relative POSIX (`tests/test_calc.py`;
backslashes are accepted and normalised) or absolute UNDER the recorded
`worktree_path`. B3 and B4 PLACE these paths rather than basename-matching them
(gh#139), so a mis-relativised entry is a FAIL naming the record - not a silent
mismatch. It reaches that FAIL by one of two arms, and the distinction is worth
stating because the arms are not interchangeable: a repo-root-relative entry
(`harness/toy-repo/calc.py`) PLACES perfectly well, it just places outside the
allowlist, so it fails as `commit diff outside allowlist`; only an absolute path
that is not under `worktree_path` is genuinely unplaceable, and that fails as
`recorded path(s) not resolvable against worktree_path=`. `commits` recording no
file at all is also a FAIL: B4 cannot say "all committed files are within the
allowlist" having examined none.

check.py never runs B1-B9 in CI (`--only C,F,G` is the CI invocation) and no recorded
fixture is tracked. That is NOT the same as the contract going unenforced until
someone records: `skills/_shared/tests/test_scenario_b_allowlist_paths.py` calls B2,
B3 and B4 directly against synthetic records, and that suite runs on both CI legs, so
a recorder emitting a mis-relativised path is caught there before any recording is
made. What the recording machine is the only place for is this contract meeting a
REAL record.

F and G read NO record. They reconstruct from tracked fixtures through
issue_intake's injectable Runner (E-0360), so they run on a clean checkout:

    fixtures/scenarioF-issues-before.json  four seeds, classified by LABEL
    fixtures/scenarioF-dispositions.json   the section 3.4 table, as data
    fixtures/scenarioF-carved/*.md         the carve F3 expects

Behavioral assertions read locations from harness.config.json; harness-internal
fixtures / records / stubs resolve relative to this script's own directory (they
are not personal or environmental paths).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = SCRIPT_DIR / "fixtures"
RECORDS_DIR = SCRIPT_DIR / "records"
WAKE_LOG = SCRIPT_DIR / "stubs" / "schedulewakeup_log.md"
DEFAULT_CONFIG = SCRIPT_DIR / "harness.config.json"
EXAMPLE_CONFIG = SCRIPT_DIR / "harness.config.example.json"

REQUIRED_CONFIG_KEYS = (
    "specs_dir",
    "plans_dir",
    "status_roots",
    "solution_skill",
    "orchestrator_skill",
    "builder_entry",
)

#: The JSON type each REQUIRED_CONFIG_KEYS value must have, as (python type, name the
#: error message uses). `status_roots` is iterated and each entry fed to Path (C3); the
#: rest are path or skill-name strings. A str is iterable, so it must not pass for a
#: list: it would iterate by character instead of failing (gh#989).
REQUIRED_CONFIG_TYPES = {
    "specs_dir": (str, "string"),
    "plans_dir": (str, "string"),
    "status_roots": (list, "list of strings"),
    "solution_skill": (str, "string"),
    "orchestrator_skill": (str, "string"),
    "builder_entry": (str, "string"),
}

#: Keys a config may still carry from the Phase-0 port, read without error and
#: ignored. `resolver_cmd` selected the pre-port `lib.plan_parser` import when null;
#: that branch is gone (gh#627), so a machine-local config setting the key must not
#: crash - and must not look as if it still chooses anything, hence the WARN.
IGNORED_CONFIG_KEYS = ("resolver_cmd",)

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Result:
    status: str  # PASS | FAIL | SKIP
    detail: str
    #: (scenario, artifact) when this SKIP stands for a recording that was never made.
    #: Set only by `_unrecorded`; `run_suite` reads it to pick exit 3 over exit 0.
    unrecorded: Optional[tuple[str, str]] = None


#: Exit code when nothing FAILed but an in-scope scenario was never recorded (gh#628).
#: Not 2: that is already "config not found" (`main`) and argparse's usage error.
EXIT_UNRECORDED = 3


def _unrecorded(scenario: str, artifact: str, section: str = "") -> Result:
    """The ONE shape of "this recording does not exist on this machine" (gh#628).

    A missing recording is not a regression: nothing was observed, so nothing was
    violated. It used to be FAIL in A1-A9, B1-B9, D1-D3 and E2 and SKIP in C4 and D4,
    so a fresh clone's red could not be told apart from a broken builder. Every
    absent-record early return goes through here; a recording that EXISTS but is wrong
    still FAILs at its own call site. `scenario` is the scenario whose recording would
    have supplied `artifact`, named repo-root-relative (the cwd check.py is documented
    and run from) so the reader can `ls` it - "B/D" for the wake log C4 reads, which either of those
    three writes. `run_suite` holds the exit at 3 only when one of those letters is in
    the selected groups. `section` names the run-scenario.md section that clears the
    debt when it is narrower than "Scenario <X>".
    """
    section = section or f"Scenario {scenario}"
    return Result(
        SKIP,
        f"unrecorded: no {artifact} on this machine (scenario {scenario} not recorded) - "
        f"unproven here rather than violated; record it per harness/run-scenario.md, {section}",
        unrecorded=(scenario, artifact),
    )


@dataclass
class Assertion:
    id: str
    group: str          # A | B | C | D | F | G  (E1/E2 ride in group B)
    behavioral: bool    # False => structural (warn-only on FAIL)
    fn: Callable[["Context"], Result]


# ---------------------------------------------------------------------------
# Context: config + resolved parse_plan + lazily-loaded records
# ---------------------------------------------------------------------------


class Context:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.ignored_keys = [k for k in IGNORED_CONFIG_KEYS if k in self.config]
        self.builder_entry = self.config.get("builder_entry")
        self.import_error: Optional[str] = None
        self.parse_plan = self._load_parse_plan()

    # -- skill-dir resolution (config names the skill; root is a portable
    #    convention, never a personal literal) -----------------------------
    def skills_root(self) -> Path:
        import os

        override = self.config.get("skills_root") or os.environ.get(
            "AGENTFLOW_SKILLS_ROOT"
        )
        if override:
            p = Path(override).expanduser()
            if not p.is_absolute():
                # Relative skills_root resolves against the config file's own
                # directory so the tracked example config works from any clone
                # location ("../skills" -> <repo>/skills).
                p = (self.config_path.parent / p).resolve()
            return p
        return Path.home() / ".claude" / "skills"

    def orchestrator_dir(self) -> Path:
        return self.skills_root() / self.config["orchestrator_skill"]

    def solution_dir(self) -> Path:
        return self.skills_root() / self.config["solution_skill"]

    def ignored_key_warnings(self) -> list[str]:
        lines = []
        for k in self.ignored_keys:
            if k == "resolver_cmd" and self.config.get(k) is None:
                # null was the pre-port setting, so the rest of the file most likely
                # dates from then too: deleting the key alone leaves a skills_root
                # aimed at the unported tree, which C6/C7 then FAIL on.
                lines.append(
                    f"# WARN config: key 'resolver_cmd' is null in {self.config_path.name} and is ignored - "
                    "this looks like a pre-port recording config, and its skills_root "
                    "likely targets the unported skills tree (C6/C7 FAIL there). Retire "
                    "it and regenerate from harness.config.example.json rather than "
                    "editing it (gh#627)."
                )
            else:
                lines.append(
                    f"# WARN config: key {k!r} in {self.config_path.name} is ignored - it "
                    "selected the retired pre-port import path; parse_plan always imports "
                    "from the installed agentflow package now (gh#627). Delete the key to "
                    "silence this."
                )
        return lines

    def _load_parse_plan(self):
        """Import parse_plan from the installed agentflow package."""
        try:
            mod = importlib.import_module("agentflow.plan_parser")
            return mod.parse_plan
        except Exception as exc:  # noqa: BLE001 - record, surface via self-test
            self.import_error = f"{type(exc).__name__}: {exc}"
            return None

    # -- wake log ---------------------------------------------------------
    def wake_rows(self, scenario: Optional[str] = None) -> list[dict]:
        rows = _parse_wake_log(WAKE_LOG)
        if scenario is None:
            return rows
        return [r for r in rows if r["scenario"] == scenario]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WAKE_RE = re.compile(
    r"^\s*(?P<ts>\S+)\s*\|\s*scenario=(?P<scenario>\S+)\s*\|\s*"
    r"wake_at=(?P<wake_at>\S+)\s*\|\s*prompt=(?P<prompt>.*)$"
)


_WAKE_SCENARIOS = frozenset({"A", "B", "C", "D"})
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


def _parse_wake_log(path: Path) -> list[dict]:
    """Recorded wake rows only - format examples are not wakes (gh#604).

    The stub README's record-format line (`<now-UTC> | scenario=<A|B|C|D> | ...`) is
    seeded into every log header inside a code fence and matches `_WAKE_RE`; counted as
    a row it turned a header-only log into C4 PASS. So: skip fenced blocks, and keep a
    row only when scenario is a real scenario letter.

    Timestamps are deliberately NOT validated here: a hand-typed scenario=C row with a
    bad timestamp is still a recorded C wake and must turn C4 red, and B9 names an
    unparseable timestamp itself. Timestamp judgement belongs to the consumer.
    """
    if not path.exists():
        return []
    rows: list[dict] = []
    fence: Optional[str] = None  # the opening run (e.g. "```", "~~~~") while inside a fence
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        fm = _FENCE_RE.match(line)
        if fm:
            run = fm.group(1)
            if fence is None:
                fence = run
                continue
            # CommonMark: closed only by the same char, at least as long, nothing after it.
            if run[0] == fence[0] and len(run) >= len(fence) and not line[len(run):].strip():
                fence = None
                continue
        if fence is not None or not line or line.startswith("#") or line.startswith("<!--"):
            continue
        m = _WAKE_RE.match(raw)
        if not m:
            continue
        if m.group("scenario") not in _WAKE_SCENARIOS:
            continue
        rows.append(
            {
                "ts": m.group("ts"),
                "scenario": m.group("scenario"),
                "wake_at": m.group("wake_at"),
                "prompt": m.group("prompt").strip(),
            }
        )
    return rows


def _parse_iso(value: str) -> Optional[datetime]:
    """Always timezone-aware, so two stamps compare instead of raising (gh#612).

    A naive stamp is read as UTC, not reported as mixed: the wake log's only writer is
    the operator, following stubs/README.md's record format `<now-UTC> | ... |
    wake_at=<future-UTC>`, so both fields are UTC by contract and a missing `Z` is
    that contract written loosely, not a second clock. A mixed-timezone FAIL would
    red a row that honours B9's invariant.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _find_plan_file(dirpath: Path) -> Optional[Path]:
    if not dirpath.is_dir():
        return None
    preferred = dirpath / "plan.md"
    if preferred.exists():
        return preferred
    md = sorted(dirpath.glob("*.md"))
    return md[0] if md else None


_DRIVE_RE = re.compile(r"^([A-Za-z]):/")


def _posix(pathstr: str) -> str:
    """Separator-normalized form. Recordings are made on one machine while CI runs
    both ubuntu and windows, so `tests\\test_calc.py` and `tests/test_calc.py` are
    the same path; the drive letter is upper-cased so `c:/` vs `C:/` cannot turn a
    root comparison into a spurious red."""
    p = pathstr.strip().replace("\\", "/")
    return _DRIVE_RE.sub(lambda m: m.group(1).upper() + ":/", p)


def _is_absolute_posix(p: str) -> bool:
    return p.startswith("/") or bool(_DRIVE_RE.match(p))


def _rel_to_root(pathstr: str, root: str = "") -> Optional[str]:
    """Root-relative POSIX form of a recorded path, or None if it cannot be placed.

    None rather than a basename ON PURPOSE (gh#139). This replaced a `_basename`
    helper that both sides of B4's allowlist comparison ran through, which made
    `vendor/calc.py` indistinguishable from the allowlisted `calc.py` and collapsed
    `tests/test_calc.py` to `test_calc.py` - so an allowlist meant to constrain a
    diff constrained nothing. A path this cannot place is a path the allowlist
    cannot judge, and the caller must turn that into a FAIL naming the record;
    falling back to the basename here would put the hole straight back.
    """
    p = posixpath.normpath(_posix(pathstr))
    if _is_absolute_posix(p):
        r = posixpath.normpath(_posix(root)).rstrip("/") if root else ""
        if not r or not _is_absolute_posix(r):
            return None                      # absolute path, no root to place it against
        # gh#218 item 6: `_posix` upper-cases the DRIVE LETTER, and before this the body
        # was still compared byte-for-byte - so a record whose two halves disagree in case
        # ('c:/WT' against 'C:/wt/calc.py') read as a path outside its own worktree and
        # failed. A false RED, but ours. Case-folding is applied to drive paths ONLY: on a
        # POSIX root '/Home' and '/home' really are different directories, and folding
        # there would buy this repair with a false GREEN somewhere else.
        drive_paths = bool(_DRIVE_RE.match(p)) and bool(_DRIVE_RE.match(r))
        lhs, rhs = (p.lower(), r.lower()) if drive_paths else (p, r)
        if not (lhs == rhs or lhs.startswith(rhs + "/")):
            return None                      # absolute path outside the recorded worktree
        # Slice off the FOLDED comparison, not the raw one: 'C:/WT' against root 'C:/wt' is
        # the same directory, and `p != r` would be True there and take the strip branch.
        p = p[len(r) + 1:] if lhs != rhs else ""
    if not p or p == "." or p.startswith("../"):
        return None                          # empty, or escapes the root
    return p


def _scenario_b_worktree(state, evidence) -> str:
    """The root recorded paths are relative to - the same fallback B2 reports.

    B2 calls this rather than carrying its own copy, so "the same" is structural
    instead of a claim that happens to be true today.
    """
    wt = (evidence or {}).get("worktree_path")
    if not wt and state is not None:
        for t in state.tasks.values():
            if getattr(t, "wave_worktree_path", None):
                wt = t.wave_worktree_path
                break
    return wt or ""


def _import_parser_module(ctx: Context):
    """Return the plan_parser module (for exception classes), or None."""
    try:
        return importlib.import_module("agentflow.plan_parser")
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Scenario A - refine -> solution emits a parseable plan
# ---------------------------------------------------------------------------


_SCENARIO_A_ARTIFACT = "harness/records/scenarioA/plan.md"
_SCENARIO_B_ARTIFACT = "harness/records/scenarioB/status.md"


def _load_scenario_a_plan(ctx: Context):
    """Return (plan, verdict). plan is None on any failure, and verdict is then the
    Result to return: unrecorded SKIP when no plan was recorded, FAIL otherwise."""
    plan_file = _find_plan_file(RECORDS_DIR / "scenarioA")
    if plan_file is None:
        return None, _unrecorded("A", _SCENARIO_A_ARTIFACT)
    if ctx.parse_plan is None:
        return None, Result(FAIL, f"parse_plan unavailable ({ctx.import_error})")
    try:
        return ctx.parse_plan(plan_file), None
    except Exception as exc:  # noqa: BLE001
        return None, Result(FAIL, f"parse_plan raised: {type(exc).__name__}: {exc}")


def a1_record_present(ctx: Context) -> Result:
    plan, verdict = _load_scenario_a_plan(ctx)
    if plan is None:
        return verdict
    return Result(PASS, "scenarioA plan recorded and parses clean")


def a2_epic(ctx: Context) -> Result:
    plan, verdict = _load_scenario_a_plan(ctx)
    if plan is None:
        return verdict
    if plan.epic and str(plan.epic).strip():
        return Result(PASS, f"epic={plan.epic!r}")
    return Result(FAIL, "epic missing/empty")


def a3_spec(ctx: Context) -> Result:
    plan, verdict = _load_scenario_a_plan(ctx)
    if plan is None:
        return verdict
    if plan.spec and str(plan.spec).strip():
        return Result(PASS, f"spec={plan.spec!r}")
    return Result(FAIL, "spec missing/empty")


def a4_phases(ctx: Context) -> Result:
    plan, verdict = _load_scenario_a_plan(ctx)
    if plan is None:
        return verdict
    if isinstance(plan.phases, list) and plan.phases:
        return Result(PASS, f"phases={plan.phases}")
    return Result(FAIL, "phases missing/empty")


def a5_phase_membership(ctx: Context) -> Result:
    plan, verdict = _load_scenario_a_plan(ctx)
    if plan is None:
        return verdict
    if not plan.tasks:
        return Result(FAIL, "plan has no tasks")
    strays = [tid for tid, t in plan.tasks.items() if t.phase not in plan.phases]
    if strays:
        return Result(FAIL, f"tasks {strays} reference phases not in {plan.phases}")
    return Result(PASS, f"{len(plan.tasks)} task(s), all phases declared")


def a6_acyclic_dag(ctx: Context) -> Result:
    plan, verdict = _load_scenario_a_plan(ctx)
    if plan is None:
        return verdict
    # Same refusal A5 makes, for the same reason (gh#218): the loop below is the
    # whole assertion, so over zero tasks it reports an acyclic DAG and resolving
    # deps having examined neither. A5 reddening on this input is not a substitute
    # - A6's own verdict is the false statement.
    if not plan.tasks:
        return Result(FAIL, "plan has no tasks")
    # parse_plan already ran _validate_dag (acyclic + known deps) without raising;
    # re-confirm dep references resolve to known task ids.
    ids = set(plan.tasks)
    bad = [(tid, d) for tid, t in plan.tasks.items() for d in t.deps if d not in ids]
    if bad:
        return Result(FAIL, f"deps reference unknown tasks: {bad}")
    return Result(PASS, f"acyclic DAG, all deps resolve across {len(plan.tasks)} "
                        f"task(s) (validated at parse time)")


def a7_test_commands(ctx: Context) -> Result:
    plan, verdict = _load_scenario_a_plan(ctx)
    if plan is None:
        return verdict
    reg = (plan.test_commands or {}).get("regression")
    if reg and str(reg).strip():
        return Result(PASS, f"test_commands.regression={reg!r}")
    return Result(FAIL, "test_commands.regression missing/empty")


def a8_slug(ctx: Context) -> Result:
    plan, verdict = _load_scenario_a_plan(ctx)
    if plan is None:
        return verdict
    if plan.slug and str(plan.slug).strip():
        return Result(PASS, f"slug={plan.slug!r}")
    return Result(FAIL, "slug missing/empty")


def a9_worktrees_and_verification(ctx: Context) -> Result:
    plan, verdict = _load_scenario_a_plan(ctx)
    if plan is None:
        return verdict
    if plan.per_wave_worktrees is not True:
        return Result(FAIL, f"per_wave_worktrees is {plan.per_wave_worktrees!r}, want True")
    # Same refusal A5 and A6 make (gh#218). Half of A9's claim is a statement about
    # every task's verification block, and over zero tasks that half is a claim
    # about nothing while the PASS reads as coverage.
    if not plan.tasks:
        return Result(FAIL, "plan has no tasks")
    # verification blocks must be well-formed-or-null: parse_plan already coerced
    # each into VerificationBlock | None, so simply confirm the type invariant.
    for tid, t in plan.tasks.items():
        if t.verification is not None and not hasattr(t.verification, "acceptance_criteria"):
            return Result(FAIL, f"task {tid} verification malformed")
    return Result(PASS, f"per_wave_worktrees=True; verification blocks "
                        f"well-formed-or-null across {len(plan.tasks)} task(s)")


# ---------------------------------------------------------------------------
# Scenario B - single-task plan -> one wave -> review -> commit
# ---------------------------------------------------------------------------


def _load_scenario_b(ctx: Context):
    """Return (status_state, evidence_dict, error)."""
    base = RECORDS_DIR / "scenarioB"
    status_path = base / "status.md"
    evidence_path = base / "wave-evidence.json"
    if not status_path.exists():
        # The status board is what makes scenario B "recorded": every B row (and E2)
        # turns this into `_unrecorded`. A board with no wave-evidence.json beside it
        # is a partial recording, which stays FAIL at the call sites.
        return None, None, None
    try:
        sw = importlib.import_module("agentflow.status_writer")
        state = sw.read_status(status_path)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"status.md unreadable: {type(exc).__name__}: {exc}"
    evidence = None
    if evidence_path.exists():
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return state, None, f"wave-evidence.json unparseable: {exc}"
        if not isinstance(evidence, dict):
            # Valid JSON of the wrong shape is a malformed recording, refused here like
            # an unparseable one - every B row reads it with `.get`, so passing a list
            # through crashed B2/B3/B4/B8 instead of naming the defect (gh#769).
            return state, None, (
                f"wave-evidence.json is not a JSON object (got {type(evidence).__name__})"
            )
    return state, evidence, None


def _scenario_b_allowlist(ctx: Context) -> list[str]:
    """file_allowlist for the single task, read from the plan-single-task fixture."""
    if ctx.parse_plan is None:
        return []
    fixture = FIXTURES_DIR / "plan-single-task.md"
    if not fixture.exists():
        return []
    try:
        plan = ctx.parse_plan(fixture)
    except Exception:  # noqa: BLE001
        return []
    allow: list[str] = []
    for t in plan.tasks.values():
        if t.verification:
            allow.extend(t.verification.file_allowlist)
    return allow


def b1_record_present(ctx: Context) -> Result:
    state, evidence, err = _load_scenario_b(ctx)
    if state is None:
        return Result(FAIL, err) if err else _unrecorded("B", _SCENARIO_B_ARTIFACT)
    if evidence is None:
        return Result(FAIL, err or "wave-evidence.json missing")
    return Result(PASS, "scenarioB status.md + wave-evidence.json present")


#: The two ESCALATION headings the orchestrator's Step 1.1 opens before any wave can run.
_STEP_1_1_STOPS = ("ESCALATION: Target tree unresolvable", "ESCALATION: Worktree mandate")


def _scenario_b_step_1_1(state) -> Optional[str]:
    """Why the recorded run did NOT get past Step 1.1 to Step 2, or None when it did.

    Scenario B used to stop at Step 1.1 by design of the fixture (gh#1105): no epic worktree
    and no origin matching host_ref. A recording of that stop must not read as a wave run.
    Stopped: an unanswered Step 1.1 escalation is on the board. Reached Step 2: the board
    carries a write only Step 2 or later makes - `preflight_audit_status` (Step 2's own
    write) or a task moved off `pending` (Step 2 marks the ready set).
    """
    for esc in state.escalations:
        if esc.answer is None and any(stop in esc.question for stop in _STEP_1_1_STOPS):
            heading = next(s for s in _STEP_1_1_STOPS if s in esc.question)
            return f"run stopped at orchestrator Step 1.1: open escalation {esc.id} ({heading})"
    moved = [tid for tid, t in state.tasks.items() if t.state != "pending"]
    if state.preflight_audit_status is None and not moved:
        return (
            "run never reached orchestrator Step 2: no preflight_audit_status and every task "
            "still pending"
        )
    return None


def b2_worktree(ctx: Context) -> Result:
    state, evidence, err = _load_scenario_b(ctx)
    if state is None:
        return Result(FAIL, err) if err else _unrecorded("B", _SCENARIO_B_ARTIFACT)
    # The same resolution B3/B4 relativise recorded paths against - called, not
    # copied. It used to be duplicated here, so B2 could report one root while the
    # allowlist comparison used another and nothing would say so.
    wt = _scenario_b_worktree(state, evidence)
    stopped = _scenario_b_step_1_1(state)
    # The Step 1.1 stop is named first: it is the reason no worktree was recorded, so the
    # bare "no worktree_path" line would hide the actual finding.
    if stopped and "Step 1.1" in stopped:
        return Result(FAIL, stopped)
    if not wt:
        return Result(FAIL, "no worktree_path recorded (wave-evidence or task state)")
    if stopped:
        return Result(FAIL, f"worktree recorded ({wt}) but {stopped}")
    return Result(PASS, f"worktree created: {wt}; run got past Step 1.1 to Step 2")


def b3_commit_touches_calc(ctx: Context) -> Result:
    """The wave touched the toy repo's own calc.py, not something merely named that.

    Placed rather than basename-matched (gh#139), for the same reason as B4: there
    is exactly one `calc.py` in the toy repo, at its root, so the loose reading buys
    no legitimate flexibility while admitting `vendor/calc.py` as proof. Leaving B3
    loose and B4 tight would be worse than either - one record could go green on
    "touched calc.py" and red on "stayed in the allowlist" about the same wrong file.
    The literal stays literal on purpose: deriving it from the allowlist would make
    B3 tautological with respect to B4.
    """
    state, evidence, err = _load_scenario_b(ctx)
    if state is None and err is None:
        return _unrecorded("B", _SCENARIO_B_ARTIFACT)
    if evidence is None:
        return Result(FAIL, err or "wave-evidence.json missing")
    root = _scenario_b_worktree(state, evidence)
    commits = evidence.get("commits") or []
    for c in commits:
        files = c.get("files") or []
        if any(_rel_to_root(f, root) == "calc.py" for f in files):
            return Result(PASS, f">=1 commit touches calc.py (sha={c.get('sha')})")
    return Result(FAIL, f"no recorded commit touches calc.py (commits={len(commits)})")


def b4_diff_in_allowlist(ctx: Context) -> Result:
    """Every committed file is inside the plan's file_allowlist.

    Both sides used to be reduced to basenames (gh#139), which made the comparison
    lossy in the exact direction that matters: the fixture allowlists
    `tests/test_calc.py`, which collapsed to `test_calc.py`, and a committed
    `vendor/calc.py` laundered into the allowlisted `calc.py`. B4 then reported that
    out-of-allowlist path as "within allowlist" - not merely a missed catch, a wrong
    statement. Compare placed paths instead, relativising absolute recordings against
    the worktree the record itself declares.

    The same wrong statement had a second source, one step further in: evidence that
    resolves to ZERO committed paths also produced an empty `touched - allow` and
    therefore a PASS asserting something about "all committed files". That is refused
    below - see the `if not touched` arm.
    """
    state, evidence, err = _load_scenario_b(ctx)
    if state is None and err is None:
        return _unrecorded("B", _SCENARIO_B_ARTIFACT)
    if evidence is None:
        return Result(FAIL, err or "wave-evidence.json missing")
    allow_raw = _scenario_b_allowlist(ctx)
    if not allow_raw:
        return Result(FAIL, "could not resolve file_allowlist from plan-single-task fixture")
    unplaceable_allow = sorted(a for a in allow_raw if _rel_to_root(a) is None)
    if unplaceable_allow:
        # Shrinking the allowlist by dropping these would loosen the gate silently.
        return Result(FAIL, f"allowlist entry is not worktree-relative: {unplaceable_allow}")
    allow = {_rel_to_root(a) for a in allow_raw}
    root = _scenario_b_worktree(state, evidence)
    touched: set[str] = set()
    unplaceable: list[str] = []
    for c in (evidence.get("commits") or []):
        for f in (c.get("files") or []):
            rel = _rel_to_root(f, root)
            if rel is None:
                unplaceable.append(f)
            else:
                touched.add(rel)
    if unplaceable:
        # A malformed record, not a missing one: the evidence exists and cannot be
        # judged, so this is red rather than skipped.
        return Result(
            FAIL,
            f"recorded path(s) not resolvable against worktree_path={root!r}: {sorted(unplaceable)}",
        )
    if not touched:
        # gh#139's own property, one level in from the basename hole above: with no
        # committed path recorded, `touched - allow` is empty for the same reason
        # C7's `sum(...)` was zero over an empty needle list, and the PASS below
        # would assert a fact about "all committed files" having examined none.
        # Red rather than skipped, by the same reasoning as the unplaceable arm:
        # the evidence exists and contains nothing to judge. Four shapes reach
        # here - commits absent, `commits: []`, a commit with `files: []`, and an
        # evidence file that parsed to `{}`.
        return Result(
            FAIL,
            f"wave-evidence.json records no committed file path "
            f"({len(evidence.get('commits') or [])} commit(s)) - nothing to judge "
            f"against allowlist {sorted(allow)}",
        )
    stray = touched - allow
    if stray:
        return Result(FAIL, f"commit diff outside allowlist: {sorted(stray)} (allow={sorted(allow)})")
    return Result(PASS, f"all committed files within allowlist {sorted(allow)}")


def b5_status_done(ctx: Context) -> Result:
    state, evidence, err = _load_scenario_b(ctx)
    if state is None:
        return Result(FAIL, err) if err else _unrecorded("B", _SCENARIO_B_ARTIFACT)
    done = [tid for tid, t in state.tasks.items() if t.state == "done"]
    if done:
        return Result(PASS, f"task(s) {done} marked done in status.md")
    return Result(FAIL, "no task marked done in status.md")


def b6_test_pass(ctx: Context) -> Result:
    state, evidence, err = _load_scenario_b(ctx)
    if state is None:
        return Result(FAIL, err) if err else _unrecorded("B", _SCENARIO_B_ARTIFACT)
    if state.test_status.result == "pass":
        return Result(PASS, "test_status.result == pass")
    return Result(FAIL, f"test_status.result == {state.test_status.result!r}")


def b7_review_verdict(ctx: Context) -> Result:
    state, evidence, err = _load_scenario_b(ctx)
    if state is None:
        return Result(FAIL, err) if err else _unrecorded("B", _SCENARIO_B_ARTIFACT)
    verdicts = [
        (tid, t.verifier_verdict)
        for tid, t in state.tasks.items()
        if t.verifier_verdict is not None
    ]
    if verdicts:
        return Result(PASS, f"review verdict recorded: {verdicts}")
    return Result(FAIL, "no verifier_verdict recorded on any task")


def b8_independent_pytest(ctx: Context) -> Result:
    state, evidence, err = _load_scenario_b(ctx)
    if state is None and err is None:
        return _unrecorded("B", _SCENARIO_B_ARTIFACT)
    if evidence is None:
        return Result(FAIL, err or "wave-evidence.json missing")
    head = evidence.get("pytest_head") or {}
    if head.get("result") == "pass":
        return Result(PASS, "independent pytest on worktree HEAD passed")
    return Result(FAIL, f"pytest_head.result == {head.get('result')!r}")


def b9_one_future_wake(ctx: Context) -> Result:
    rows = ctx.wake_rows("B")
    if not rows:
        # Unrecorded only if B itself was never recorded. A B status board with no B
        # wake row means the recorded wave failed to schedule its wake: a regression.
        if not (RECORDS_DIR / "scenarioB" / "status.md").exists():
            return _unrecorded("B", f"harness/stubs/{WAKE_LOG.name} scenario=B row")
        return Result(FAIL, "scenario B recorded (status.md present) but no scenario=B wake row logged")
    if len(rows) != 1:
        return Result(FAIL, f"expected exactly 1 scenario=B wake row, found {len(rows)}")
    row = rows[0]
    ts, wake_at = _parse_iso(row["ts"]), _parse_iso(row["wake_at"])
    if ts is None or wake_at is None:
        return Result(FAIL, f"unparseable timestamps: ts={row['ts']} wake_at={row['wake_at']}")
    if wake_at <= ts:
        return Result(FAIL, f"wake_at {row['wake_at']} not strictly after ts {row['ts']}")
    return Result(PASS, f"one future ScheduleWakeup logged (wake_at={row['wake_at']})")


# ---------------------------------------------------------------------------
# Scenario E - reviewer-prompt-path guard (E1) + re-prompt guard (E2)
# ---------------------------------------------------------------------------


def e1_reviewer_prompt_path(ctx: Context) -> Result:
    # hard gate: the vendored 5.0.5 reviewer prompts must exist under the
    # suite's _vendored/review-prompts/ (guards the dual-superpowers-version swap).
    review_root = ctx.orchestrator_dir().parent / "_vendored" / "review-prompts"
    spec_p = review_root / "spec-reviewer-prompt.md"
    code_p = review_root / "code-quality-reviewer-prompt.md"
    missing = [str(p) for p in (spec_p, code_p) if not p.exists()]
    if missing:
        return Result(FAIL, f"resolved reviewer-prompt path missing: {missing}")
    return Result(PASS, f"resolved reviewer prompts == vendored 5.0.5 files under {review_root}")


_MARKER_ESC = "escalation_prompted"
_MARKER_MERGE = "merge_decision_prompted"


def e2_reprompt_guard(ctx: Context) -> Result:
    state, evidence, err = _load_scenario_b(ctx)
    if state is None:
        return Result(FAIL, err) if err else _unrecorded("B", _SCENARIO_B_ARTIFACT)
    summaries = [d.summary for d in state.decisions]
    esc = [s for s in summaries if _MARKER_ESC in s]
    merge = [s for s in summaries if _MARKER_MERGE in s]
    if not esc:
        return Result(FAIL, f"{_MARKER_ESC} marker not written on transition wake")
    if not merge:
        return Result(FAIL, f"{_MARKER_MERGE} marker not written on transition wake")
    # Re-prompt guard: the buttoned prompt must NOT re-fire on the second wake.
    # Each marker gates its own prompt, so it appears exactly once across the
    # recorded 2-wake replay.
    if len(esc) != 1:
        return Result(FAIL, f"{_MARKER_ESC} re-fired ({len(esc)}x) - marker did not gate re-prompt")
    if len(merge) != 1:
        return Result(FAIL, f"{_MARKER_MERGE} re-fired ({len(merge)}x) - marker did not gate re-prompt")
    return Result(
        PASS,
        f"{_MARKER_ESC} + {_MARKER_MERGE} written once each; no re-fire across 2-wake replay",
    )


# ---------------------------------------------------------------------------
# Scenario C - loud hard-fail on missing required plan key
# ---------------------------------------------------------------------------


def _parse_malformed_plan(ctx: Context):
    """Return (raised_exception_or_None, error_string_or_None).

    RE-POINTED BY gh#74, and the property is unchanged: a malformed plan must
    hard-fail LOUDLY, naming the offending key, rather than warn-and-continue. What
    changed is which shape is malformed. `plan-missing-epic.md` used to serve here;
    gh#74 made `epic` optional, because `solution/` holds human-authored operator
    documents as well as orchestrator plans and requiring orchestrator frontmatter of
    both dropped fifty files from every survey, silently. An out-of-vocabulary task
    `type` is still malformed - two live plans carry `type: scaffold` - so the fixture
    moved and the assertion did not.
    """
    fixture = FIXTURES_DIR / "plan-bad-task-type.md"
    if not fixture.exists():
        return None, "fixtures/plan-bad-task-type.md not found"
    if ctx.parse_plan is None:
        return None, f"parse_plan unavailable ({ctx.import_error})"
    try:
        ctx.parse_plan(fixture)
    except Exception as exc:  # noqa: BLE001 - the loud fail we WANT
        return exc, None
    return None, None  # parsed without raising -> C1 fails


def c1_loud_raise(ctx: Context) -> Result:
    exc, err = _parse_malformed_plan(ctx)
    if err:
        return Result(FAIL, err)
    if exc is None:
        return Result(FAIL, "parse_plan(plan-bad-task-type.md) did NOT raise - warn-and-continue")
    return Result(PASS, f"parse_plan raised {type(exc).__name__} (loud fail)")


def c2_names_offending_key(ctx: Context) -> Result:
    """A loud fail that does not say WHICH key is wrong sends the reader back to
    bisecting the file by hand, so naming it is a separate assertion from raising."""
    exc, err = _parse_malformed_plan(ctx)
    if err:
        return Result(FAIL, err)
    if exc is None:
        return Result(FAIL, "no exception raised")
    msg = f"{type(exc).__name__}: {exc}"
    if "type" in str(exc).lower() or isinstance(exc, KeyError) and "type" in repr(exc):
        return Result(PASS, f"error names the offending key: {msg}")
    return Result(FAIL, f"error does not name 'type': {msg}")


_SLUG_RE = re.compile(r"^slug:\s*(\S+)\s*$", re.MULTILINE)


def _status_board_paths(root: Path) -> list[Path]:
    """Every file under `root` shaped like a status board.

    Both conventions `epic_survey` recognises: `<dir>/status.md` and
    `<slug>.status.md`. Shape, not name, because a board written by a parser
    that should have raised would be named after the plan, not after us.
    """
    if not root.is_dir():
        return []
    return [
        p
        for p in root.rglob("*")
        if p.is_file() and (p.name == "status.md" or p.name.endswith(".status.md"))
    ]


def c3_no_status_created(ctx: Context) -> Result:
    """The failing parse must create no status board - and this must be OBSERVED.

    Rewritten by gh#139. It used to glob `records/scenarioC/**/status.md` and PASS
    on the empty list. That directory is gitignored, and `README.md` instructs that
    Scenario C records nothing into it on ANY machine - so the glob returned [] on
    every run, and C3's PASS was a statement about a directory nobody writes rather
    than about the parse. Worse, the parse this assertion is about happens IN-PROCESS
    on a tracked fixture, so a board written by a warn-and-continue parser would land
    beside that fixture and never under `records/` at all: C3 was watching a place
    the fixture path cannot reach.

    So watch the places it CAN reach: snapshot the fixtures tree, run the parse, diff.
    The status roots the config names are probed too, since a board for this fixture's
    slug can only have come from parsing this fixture. PASS now names what it looked at.

    Three surfaces, deliberately different in kind. The fixtures tree is judged by the
    before/after DIFF, so a red here means THIS parse wrote the board (a state check
    would also redden for a board that had always been tracked). The parse runs from an
    empty throwaway cwd, which catches a parser writing `status.md` relative to the
    working directory without ever making C3 sensitive to an unrelated status.md in the
    repo root. The configured status roots stay a state check, per the slug argument
    above. A board found under the fixtures tree BEFORE the parse is neither - it is a
    broken harness, and says so, because it would mask an overwrite from the diff.
    """
    fixture = FIXTURES_DIR / "plan-bad-task-type.md"
    if not fixture.exists():
        return Result(
            FAIL,
            f"fixtures/plan-bad-task-type.md not found at {fixture} - the tracked "
            "fixture C3 watches is missing, so nothing can be observed here; this is "
            "a broken harness, not a clean parse",
        )
    if ctx.parse_plan is None:
        # SKIP, not PASS: with no parser there is no parse to watch, and "nothing
        # was created" is then a claim about an event that never happened. Same
        # reasoning as D4's absent-record SKIP below.
        return Result(
            SKIP,
            f"parse_plan unavailable ({ctx.import_error}) - C3 watches what a parse "
            "creates, and no parse ran, so it is unproven here rather than clean "
            "(run `pip install -e skills/_shared` from the repo root)",
        )
    m = _SLUG_RE.search(fixture.read_text(encoding="utf-8"))
    if m is None:
        return Result(FAIL, f"no parseable `slug:` line in {fixture.name} - broken tracked fixture")
    slug = m.group(1).strip("\"'")

    # A board already under the fixtures tree is a broken harness, not a verdict on
    # this parse: C3's claim is about what the parse CREATED, and a pre-existing
    # board both falsifies that reading and masks an overwrite, since a rewritten
    # file never appears in the created-diff below. Caught before the parse so the
    # message can say which it is.
    preexisting = sorted(str(p) for p in _status_board_paths(FIXTURES_DIR))
    if preexisting:
        return Result(
            FAIL,
            f"status board(s) already under the fixtures tree BEFORE the parse: "
            f"{preexisting} - while these are present C3 cannot distinguish a board "
            "this parse created from one it overwrote, so this is a broken harness "
            "rather than a clean or dirty parse",
        )

    before = set(FIXTURES_DIR.rglob("*"))
    exc: Optional[BaseException] = None
    # Parse from a throwaway working directory. A parser regressed into writing
    # `status.md` relative to CWD - rather than beside the plan - lands the board
    # somewhere neither the fixtures diff nor the configured roots can see. Watching
    # the REAL cwd instead would make C3 red for any unrelated status.md sitting in
    # the repo root, so the surface is emptied rather than filtered.
    cwd_before = os.getcwd()
    with tempfile.TemporaryDirectory() as cwd_probe:
        try:
            os.chdir(cwd_probe)
            ctx.parse_plan(fixture)
        except Exception as raised:  # noqa: BLE001 - C1 grades the raise; C3 grades the side effects
            exc = raised
        finally:
            os.chdir(cwd_before)
        cwd_boards = sorted(str(p) for p in _status_board_paths(Path(cwd_probe)))
    created = set(FIXTURES_DIR.rglob("*")) - before

    # Defence in depth, and nothing more - said plainly because the comment here
    # used to credit this filter with catching a pre-existing board reported as
    # "created by the malformed parse" (gh#218). It cannot: the `preexisting` guard
    # above returns FAIL before the parse for exactly that case, so by the time
    # this line runs the fixtures tree held no board and every board found is
    # necessarily in `created`. Dropping `if p in created` leaves the suite green.
    # It is kept because it makes the DIFF reading structural rather than a
    # consequence of a guard twenty lines away that a later edit could move.
    stray = sorted(str(p) for p in _status_board_paths(FIXTURES_DIR) if p in created)
    stray.extend(cwd_boards)
    roots = [Path(r).expanduser() for r in (ctx.config.get("status_roots") or [])]
    # These stay a state check rather than a diff, on the reasoning already in the
    # docstring: a board named for THIS malformed fixture's slug can only have come
    # from parsing this fixture, so finding one left by an earlier run is still the
    # violation and not a false red.
    for base in roots:
        for candidate in (base / slug / "status.md", base / f"{slug}.status.md"):
            if candidate.exists():
                stray.append(str(candidate))
    # Retained safety net, not evidence: README.md says nothing is ever recorded
    # under records/scenarioC, so this arm can only fire if an operator planted a
    # board there by hand. Dropping it would silently stop catching that.
    #
    # Shape-matched through `_status_board_paths`, not name-matched, so all three
    # of C3's surfaces agree on what a board IS. This arm alone still globbed
    # `**/status.md`, which is the narrower of the two conventions `epic_survey`
    # recognises - and the wrong half: `_status_board_paths` exists precisely
    # because a board written by a parser that should have raised is named after
    # the PLAN (`toy-calc-bad-task-type.status.md`), not after us. So the one
    # filename this arm would actually have to catch was the one it skipped.
    stray.extend(str(p) for p in _status_board_paths(RECORDS_DIR / "scenarioC"))
    if stray:
        return Result(FAIL, f"status board(s) present despite hard-fail: {sorted(set(stray))}")

    raised = f"parse raised {type(exc).__name__}" if exc else "parse returned WITHOUT raising"
    # `len(roots)` alone read as coverage it does not have: on the tracked example
    # config both entries are `/path/to/your/...` placeholders that do not exist, so
    # probing them examines nothing. Report how many are real.
    live_roots = sum(1 for b in roots if b.is_dir())
    return Result(
        PASS,
        f"no status board created by the malformed parse ({raised}; {len(before)} "
        f"fixture path(s) watched, {len(created)} created, none board-shaped; a "
        f"throwaway cwd watched for a board written relative to the working "
        f"directory; {live_roots} of {len(roots)} configured status_root(s) exist "
        f"and were probed for slug {slug!r})",
    )


def c4_no_wake_scheduled(ctx: Context) -> Result:
    """No scenario=C wake was scheduled - only claimable when wakes were recorded.

    Rewritten by gh#139. `stubs/schedulewakeup_log.md` is gitignored and only a live
    recording writes it; `_parse_wake_log` returns [] for a missing file, so on every
    fresh clone C4 read an empty list and reported the absence of a C row as proof
    that none was scheduled. Absence of the log is not absence of the wake.

    There is no fixture-path substitute here: a wake is fired by the live skill through
    the real ScheduleWakeup and logged by hand, and the in-process parse C1-C3 run has
    no wake surface at all. Per run-scenario.md, Scenario C produces no C row even in a
    FULL live recording - so the only available evidence that the recording surface was
    live is the census of the other scenarios' rows. With no rows at all, nothing was observed: SKIP.
    """
    rows = ctx.wake_rows()
    hits = [r for r in rows if r["scenario"] == "C"]
    if hits:
        return Result(FAIL, f"{len(hits)} scenario=C wake row(s) logged despite hard-fail")
    if not rows:
        # Scenario C itself records nothing, and A schedules no wake (run-scenario.md,
        # Scenario A), so the rows C4 needs are written by B or D only. Hence "B/D":
        # under `--only C,F,G` (CI) neither is selected, so this SKIP is reported but
        # does not hold the exit (see run_suite).
        return _unrecorded(
            "B/D",
            f"harness/stubs/{WAKE_LOG.name} wake rows (fenced/placeholder format-example rows are not counted)",
            "Scenario B or D",
        )
    seen = sorted({r["scenario"] for r in rows})
    return Result(PASS, f"{len(rows)} wake row(s) recorded (scenarios {seen}), none scenario=C")


# Bound the grep: only scan source/text files, skip heavy vendored/VCS dirs so the
# checker never wanders into node_modules / .git / __pycache__ on the skills tree.
_GREP_EXTS = {".md", ".py", ".sh", ".txt", ".json", ".yaml", ".yml", ".js", ".toml"}
_GREP_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}


def _grep_count(root: Path, needle: str) -> int:
    if not root.exists():
        return 0
    n = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in _GREP_SKIP_DIRS:
                    stack.append(p)
                continue
            if p.suffix.lower() not in _GREP_EXTS:
                continue
            try:
                n += p.read_text(encoding="utf-8", errors="ignore").count(needle)
            except OSError:
                continue
    return n


def _grep_space(root: Path) -> int:
    """How many files `_grep_count` would READ under `root`.

    The size of the search space, never the number of hits. That distinction is
    the whole of `self-test.no-false-green`: C6's hits were 0 across hundreds of
    files, and C7's hits were 0 across zero configured needles. Only one of those
    two zeroes is evidence of a clean tree.
    """
    if not root.exists():
        return 0
    n = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in _GREP_SKIP_DIRS:
                    stack.append(p)
            elif p.suffix.lower() in _GREP_EXTS:
                n += 1
    return n


def _hits_over_space(root: Path, hits: int) -> str:
    """`N hit(s) in M file(s) read`, or that the root was never read (gh#218).

    C6/C7/C9's own repair, one register down. This string is SKIP detail rather than
    a PASS, so a bare `0` breaches nothing as stated - but a zero over a directory
    that does not exist reads exactly like a zero over a clean tree, and it is the
    number an operator uses to decide the port is done.
    """
    if not root.exists():
        return f"root {root.name}/ absent - nothing read"
    return f"{hits} hit(s) in {_grep_space(root)} file(s) read"


# Unreferenced since gh#627 removed the pre-port SKIP arms; gh#218 decides its fate.
def _preport_grep_evidence(ctx: Context) -> str:
    orch, sol = ctx.orchestrator_dir(), ctx.solution_dir()
    sp_orch = _hits_over_space(orch, _grep_count(orch, "sys.path.insert"))
    sp_sol = _hits_over_space(sol, _grep_count(sol, "sys.path.insert"))
    # IP literal(s) are read from the (gitignored) config so no personal
    # literal is hardcoded in this tracked, public file.
    ip_literals = ctx.config.get("preport_ip_literals", [])
    ip_note = ""
    if ip_literals:
        skills = ctx.skills_root()
        ip_hits = sum(_grep_count(skills, lit) for lit in ip_literals)
        ip_note = (f"; {len(ip_literals)} server-IP literal(s) under skills_root: "
                   f"{_hits_over_space(skills, ip_hits)}")
    return (
        f"pre-port grep evidence: sys.path.insert in {ctx.config['orchestrator_skill']}: {sp_orch}; "
        f"in {ctx.config['solution_skill']}: {sp_sol}{ip_note}"
    )


def c5_resolver_present(ctx: Context) -> Result:
    parser = _import_parser_module(ctx)
    if parser is None:
        return Result(FAIL, "agentflow.plan_parser not importable")
    return Result(PASS, "agentflow.plan_parser resolves")


def c6_no_syspath_in_ported(ctx: Context) -> Result:
    """No `sys.path.insert` survives in EITHER ported skill.

    Same search-space guard as C7 and C9, third application of one rule (gh#139).
    C6 was the weakest of the three: it had no existence guard at all, so both
    ported skill dirs missing, or both present and empty, produced `0 + 0` hits
    and the PASS `zero sys.path.insert in ported skills` - a claim about two
    directories that had not been read, stating no coverage of any kind.

    Guarded PER ROOT rather than on the sum, and that is the deliberate part.
    C6's claim is conjunctive - it is about the orchestrator skill AND the
    solution skill - so a sum hides exactly the case the guard is for: an
    orchestrator tree of two hundred files and an empty `solution/` sums to a
    healthy number while half the claim rests on nothing. A missing root is a
    FAIL and not a SKIP for the same reason C7 fails a missing `skills_root`:
    the port is done, so the ported skills exist by definition, and a root that
    is absent - or present holding
    nothing this grep reads - is a machine-local misconfiguration, and the
    operator is not getting the assertion they configured.

    Known residual, shared with C7/C9 and deliberately deferred: `_grep_count`
    reads only `_GREP_EXTS`.
    """
    roots = [
        (ctx.config["orchestrator_skill"], ctx.orchestrator_dir()),
        (ctx.config["solution_skill"], ctx.solution_dir()),
    ]
    missing = [f"{name} ({path})" for name, path in roots if not path.exists()]
    if missing:
        return Result(
            FAIL,
            f"ported skill dir(s) do not exist: {missing} - "
            "sys.path.insert went unexamined there",
        )
    spaces = {name: _grep_space(path) for name, path in roots}
    unread = sorted(name for name, n in spaces.items() if n == 0)
    if unread:
        return Result(
            FAIL,
            f"ported skill dir(s) {unread} hold no file this grep reads "
            f"(extensions {sorted(_GREP_EXTS)}) - sys.path.insert went unexamined there",
        )
    hits = sum(_grep_count(path, "sys.path.insert") for _, path in roots)
    if hits:
        return Result(FAIL, f"{hits} surviving sys.path.insert in ported skills")
    coverage = ", ".join(f"{name}={spaces[name]}" for name, _ in roots)
    return Result(
        PASS,
        f"zero sys.path.insert in {sum(spaces.values())} file(s) read "
        f"across both ported skills ({coverage})",
    )


def c7_no_ip_literal_in_ported(ctx: Context) -> Result:
    """No server-IP literal survives in the ported skills - when there is one to hunt.

    Two ways this used to go green having examined nothing (gh#139):

    1. `preport_ip_literals` is EMPTY, which is what the tracked example config ships
       and therefore what CI runs. `sum(... for lit in [])` is 0, so the PASS was
       unconditional. The empty list is not a misconfiguration - the whole design
       keeps personal literals out of this public tree and asks the operator to name
       them only in the machine-local config - but "nothing to look for" is a
       different fact from "the ported skills are clean", and C7 was stating the
       second while knowing only the first. SKIP, with the rationale, is the honest
       verdict; the whole-tree sanitize gate in ci.yml covers this class publicly.
    2. `skills_root` does not exist, which `_grep_count` reports as 0 hits. With
       needles configured that is a machine-local misconfiguration, and it must be
       loud - C9 already guards its own grep exactly this way.
    3. `skills_root` EXISTS but holds no file `_grep_count` would read. The first
       repair guarded on `root.exists()`, which moved the same defect one step in
       rather than closing it: `sum(...)` is still 0, now over an empty search
       space instead of an empty needle list. So gate on the size of the search
       space, and put that size in the PASS so the claim states what it examined.

    Known residual, NARROWED but not closed: `_grep_count` only reads `_GREP_EXTS`
    and swallows OSError per file, so a needle planted in a file with an
    unlisted extension (`skills/_shared/planted.env`, say) is still not read and
    C7 still reports zero hits. Deliberately deferred - widening the traversal
    changes what the live `_grep_count` call sites read (C6, C7 and C9; the
    pre-port evidence helper has had no caller since gh#627), which is a bigger
    change than this repair. What the
    space guard DOES close is the zero-files case: C7 can no longer claim a clean
    tree having read nothing at all.
    """
    ip_literals = ctx.config.get("preport_ip_literals", [])
    if not ip_literals:
        return Result(
            SKIP,
            "no preport_ip_literals configured, so nothing was examined - the tracked "
            "example config ships this key empty on purpose (personal literals belong "
            "only in the machine-local harness.config.json), and the whole-tree "
            "sanitize gate in .github/workflows/ci.yml covers this class for the "
            "public tree; set the key locally to make C7 assert",
        )
    root = ctx.skills_root()
    if not root.exists():
        return Result(
            FAIL,
            f"skills_root does not exist: {root} - {len(ip_literals)} configured "
            "literal(s) went unexamined",
        )
    space = _grep_space(root)
    if space == 0:
        # FAIL, not SKIP, and the distinction is deliberate. The empty needle list
        # above is the tracked config's INTENDED shape, so SKIP states the truth
        # there. An existing root with nothing greppable in it is the opposite: the
        # operator configured needles, so they asked for an assertion, and they are
        # not getting one. That is a machine-local misconfiguration and belongs in
        # the same arm as a nonexistent root.
        return Result(
            FAIL,
            f"skills_root {root} holds no file this grep reads (extensions "
            f"{sorted(_GREP_EXTS)}) - {len(ip_literals)} configured literal(s) "
            "went unexamined",
        )
    hits = sum(_grep_count(root, lit) for lit in ip_literals)
    if hits:
        return Result(FAIL, f"{hits} surviving server-IP literal(s) in ported skills")
    return Result(
        PASS,
        f"zero of {len(ip_literals)} configured literal(s) in "
        f"{space} file(s) read under {root}",
    )


def c8_resolver_hard_fails(ctx: Context) -> Result:
    exc, err = _parse_malformed_plan(ctx)
    if err:
        return Result(FAIL, err)
    if exc is None:
        return Result(FAIL, "resolver did NOT hard-fail on a malformed plan")
    if "type" in str(exc).lower():
        return Result(PASS, f"resolver hard-fails naming key: {type(exc).__name__}: {exc}")
    return Result(FAIL, f"resolver raised but did not name the offending key: {exc}")

def c14_a_document_plan_parses(ctx: Context) -> Result:
    """The other half of gh#74, and the reason `plan-missing-epic.md` is KEPT.

    C1/C2 prove a malformed plan fails loudly. Nothing proved the complement, and it
    is the complement that broke: `solution/` holds human-authored operator documents
    as well as orchestrator plans, requiring orchestrator frontmatter of both dropped
    fifty files from every survey, and the only evidence was a skip line no caller
    read. Deleting the old fixture along with its assertion would leave the harness
    with no record that the behaviour was retired deliberately - and nothing stopping
    a later reader from restoring the strictness C1 still, correctly, demands.
    """
    fixture = FIXTURES_DIR / "plan-missing-epic.md"
    if not fixture.exists():
        return Result(FAIL, "fixtures/plan-missing-epic.md not found")
    try:
        plan = ctx.parse_plan(fixture)
    except Exception as exc:
        return Result(FAIL, f"document plan still rejected: {type(exc).__name__}: {exc}")
    if getattr(plan, "epic", None):
        return Result(FAIL, f"parsed but invented an epic: {plan.epic!r}")
    return Result(PASS, "a plan without `epic` parses (gh#74), with epic left empty")

# Typed-timestamp placeholder the retired upstream autoloop state files carried.
# It has never existed in this repo; C9 is the regression gate that keeps a
# future port from re-introducing it.
_TYPED_TS_NEEDLE = "HH:MMZ"


def c9_no_typed_timestamp_placeholder(ctx: Context) -> Result:
    """No typed/estimated-timestamp placeholder survives under the skills root.

    Timestamps are read from the clock (`date -u`) or stamped by the registry CLI
    (`_stamp_agent_row`), never typed into a state file or a prompt. A
    fill-in-the-blank `HH:MMZ` placeholder invites an agent to invent one, which
    silently converts a live lane into a "lapsed eta" false death.

    Unlike C6/C7 this runs on BOTH sides of the port: the prose it guards is the
    skills tree itself, which exists pre- and post-port alike. The mechanism is
    substring counting, so it gates the literal `HH:MMZ` only (the spec's stated
    Done-means) - regex-shaped variants are deliberately out of scope.

    Same search-space guard as C7, for the same reason and stated the same way
    (gh#139). C9 already refused a nonexistent root, which is where C7's first
    repair stopped - and stopping there moves the defect one step in rather than
    closing it: an existing `skills_root` holding no file `_grep_count` reads
    still yields 0 hits, and the PASS then named a root over a search space of
    zero files. Unlike C7 there is no honest-SKIP arm here, because C9's needle
    is a module constant rather than a config key: there is ALWAYS something to
    look for, so nowhere to look for it is always a misconfiguration. FAIL.

    Known residual, shared with C7 and deliberately deferred: `_grep_count` reads
    only `_GREP_EXTS`, so a needle in a file with an unlisted extension is still
    unread. The space guard closes the zero-files case, not that one.
    """
    root = ctx.skills_root()
    if not root.exists():
        return Result(FAIL, f"skills_root does not exist: {root}")
    space = _grep_space(root)
    if space == 0:
        return Result(
            FAIL,
            f"skills_root {root} holds no file this grep reads (extensions "
            f"{sorted(_GREP_EXTS)}) - the {_TYPED_TS_NEEDLE!r} placeholder went "
            "unexamined",
        )
    hits = _grep_count(root, _TYPED_TS_NEEDLE)
    if hits:
        return Result(
            FAIL,
            f"{hits} typed-timestamp placeholder(s) {_TYPED_TS_NEEDLE!r} under {root} - "
            "timestamps come from the clock / the CLI stamp, never typed",
        )
    return Result(
        PASS,
        f"zero {_TYPED_TS_NEEDLE!r} placeholders in {space} file(s) read under {root}",
    )


# --- C10: steward's read-only cadence report ------------------------------
# Anchor on the domain-heading shape steward's Selection sweep already uses
# (`**A. `, `**B. `, `**B2. `, `**C. ` ...), so the block is delimited by the
# next domain heading rather than by a line count that any edit would shift.
_DOMAIN_HEAD_RE = re.compile(r"^\*\*(?P<key>[A-Z]\d?)\.[^\n]*$", re.M)
_B2_READONLY_MARKERS = ("read-only", "never enforced", "never a reap", "never a verdict")
#: The prohibition must be STATED, not merely un-contradicted: a B2 block that
#: simply drops the bullet must not pass vacuously. Only backticks/bold may sit
#: between the negation and the token.
_B2_PROHIBITION_RE = re.compile(r"(?:never|not|no)\s+[`*]*raise-blocker")

#: What a REVERSAL actually looks like: `raise-blocker` carrying CLI flags, or
#: introduced by an imperative. Proximity to a nearby "no"/"not" is NOT evidence
#: of negation - those are ordinary English words and matched almost any prose,
#: which let a full reversal pass this gate.
_B2_CALL_SITE_RES = (
    re.compile(r"raise-blocker[^\n]*?--(?:actor|project|owner|description)"),
    re.compile(r"(?:file|run|issue|escalate|use|also|hand it up)[^\n]{0,40}?[`*]*raise-blocker"),
)


def _steward_cadence_block(ctx: Context):
    """Return (block_text, error). The `**B2.` sub-domain of steward's sweep."""
    path = ctx.skills_root() / "steward" / "SKILL.md"
    if not path.exists():
        return None, f"steward SKILL.md not found at {path}"
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"steward SKILL.md unreadable: {type(exc).__name__}: {exc}"
    heads = list(_DOMAIN_HEAD_RE.finditer(text))
    for i, m in enumerate(heads):
        if m.group("key") != "B2":
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        return text[m.start():end], None
    found = [m.group("key") for m in heads]
    return None, (
        "steward SKILL.md has no `**B2.` cadence-report sub-domain "
        f"(domain headings found: {found})"
    )


# --- C13: the slash argument beats the injected default ----------------------
# Two profiles, never one: with a single `projects/*.md` the resolver's
# single-stem default would return the right answer for the wrong reason and the
# gate would pass without testing precedence at all.
_C13_ENV_YAML = (
    "schema_version: 1\n"
    "person:\n  name: Harness Fixture\n"
    "data_root: {d}\n"
    "git_host:\n  kind: local\n"
    "notify:\n  kind: none\n"
    "lock_dir: {d}\n"
    "shell: posix\n"
    "suite_root: {d}\n"
    "worktree_root: {d}\n"
)
_C13_PROJ_YAML = (
    "schema_version: 1\n"
    "name: {name}\n"
    "canonical_repo:\n  host_ref: owner/repo\n  local_checkout: {d}\n"
    "verification:\n  strategy: test-suite\n"
)


def _c13_write_block(path: Path, body: str) -> None:
    path.write_text("# generated\n\n```yaml\n" + body + "```\n", encoding="utf-8")


def c13_slash_argument_beats_injected_default(ctx: Context) -> Result:
    """`--project beta` wins over an injected default of alpha. BEHAVIOURAL.

    The epic exists because the active project was chosen BEFORE any skill ran:
    Claude Code's settings `env` overrides the shell, so `AGENTFLOW_PROJECT=x
    claude` is silently ignored and the only reliable lever was a per-project
    settings file. This EXECUTES the real CLI against a throwaway config home
    and asserts the argument wins. Prose gates cannot reach precedence - only
    running it can, which is why this lives here and not in a grep.
    """
    suite, why = _shared_package_root(ctx, "config.py")
    if suite is None:
        # FAIL, not SKIP: the clone always ships skills/_shared, so finding none is a
        # broken checkout, and a SKIP here exited 0 with the precedence check unrun.
        return Result(FAIL, f"{why}; cannot execute `agentflow.config show`")
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "config"
        (home / "projects").mkdir(parents=True)
        d = td.replace("\\", "/")
        _c13_write_block(home / "environment.md", _C13_ENV_YAML.format(d=d))
        for nm in ("alpha", "beta"):
            _c13_write_block(home / "projects" / f"{nm}.md", _C13_PROJ_YAML.format(name=nm, d=d))
        env = dict(os.environ)
        env["AGENTFLOW_CONFIG"] = str(home)
        env["AGENTFLOW_PROJECT"] = "alpha"  # the injected default the argument must beat
        env["PYTHONPATH"] = str(suite)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "agentflow.config", "show", "--project", "beta"],
                capture_output=True, text=True, env=env, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return Result(FAIL, f"could not run `agentflow.config show`: {type(exc).__name__}: {exc}")
        if proc.returncode != 0:
            return Result(
                FAIL,
                f"`config show --project beta` exited {proc.returncode}: {proc.stderr.strip()[:200]}",
            )
        if "[project] beta" not in proc.stdout:
            return Result(FAIL, f"argument did NOT win; block says: {proc.stdout.strip()[:200]}")
        if "[project] alpha" in proc.stdout:
            return Result(FAIL, "block names BOTH projects - resolution is ambiguous")
        return Result(PASS, "`--project beta` beats AGENTFLOW_PROJECT=alpha (executed, not inferred)")


def c10_steward_cadence_report_is_read_only(ctx: Context) -> Result:
    """Steward's cadence sweep exists and is not routed through `raise-blocker`.

    Structural (warn-only): this gates prose, not behavior. The load-bearing half
    is the negative one - domain B already discharges stale `## Agents` rows as a
    Blocker, so a cadence report that ALSO raises one gives dev-manager two
    Blocker paths about the same rows, which is how a double-reap happens.

    A mention of `raise-blocker` is allowed only where it is being forbidden. Two
    INDEPENDENT conditions, because either alone is bypassable: the prohibition
    must be STATED (else deleting the bullet passes vacuously), and no occurrence
    may read as a CALL SITE (else a reversal that happens to sit near an ordinary
    "no"/"not" passes).
    """
    block, err = _steward_cadence_block(ctx)
    if block is None:
        return Result(FAIL, err)
    lowered = block.lower()
    if "idle" not in lowered:
        return Result(FAIL, "B2 block never says `idle` rows are included in the sweep")
    if not any(mk in lowered for mk in _B2_READONLY_MARKERS):
        return Result(
            FAIL,
            f"B2 block carries no read-only marker (want one of {list(_B2_READONLY_MARKERS)})",
        )
    if not _B2_PROHIBITION_RE.search(lowered):
        return Result(
            FAIL,
            "B2 block never forbids `raise-blocker` outright; the prohibition must be "
            "STATED - deleting the bullet is not the same as honouring it",
        )
    unguarded = []
    for rx in _B2_CALL_SITE_RES:
        for m in rx.finditer(lowered):
            unguarded.append(block[max(0, m.start() - 40):m.end() + 20].replace("\n", " "))
    if unguarded:
        return Result(
            FAIL,
            "B2 cadence report routes through `raise-blocker` (double-reap path): "
            f"{unguarded}",
        )
    return Result(
        PASS,
        f"steward B2 cadence report present ({len(block)} chars), covers idle rows, "
        "read-only, `raise-blocker` only ever negated",
    )


# ---------------------------------------------------------------------------
# C11 / C12 - executed-CLI drill assertions
# ---------------------------------------------------------------------------
# C1..C10 read prose and parsed artifacts. These two do not: each builds a
# throwaway registry in a temp dir, runs the real `agentflow.registry` CLI
# against it as a subprocess, and asserts on the exit code and the emitted
# output. That is the point of mechanizing the drill - the question pump and
# the directive gate become checkable WITHOUT standing up a live three-loop
# session, and a grep-for-prose check would hand that dependency straight back.

_CLI_MODULE = "agentflow.registry"
_CLI_TIMEOUT = 120.0

#: Both spellings of the deploy-lock take mutation: the CLI exposes it as the
#: `deploy-lock` subcommand with a `take` operand, while the internal command
#: name (and the drill contract) is `deploy-take`. Probing both keeps C12
#: pinned to the guard's BEHAVIOUR rather than to which subparser it hangs off.
_DEPLOY_TAKE_SPELLINGS = (("deploy-lock", "take"), ("deploy-take",))


def _shared_package_root(ctx: Context, module: str) -> tuple[Optional[Path], str]:
    """The `skills/_shared` dir whose `agentflow/<module>` exists, or (None, why).

    The clone comes first because the default skills_root (`~/.claude/skills`) is the
    deployed tree, which never carries `_shared` - a check keyed on skills_root alone
    found nothing there and SKIPped green (gh#944).
    """
    candidates = [
        SCRIPT_DIR.parent / "skills" / "_shared",  # this repo: harness/ sits beside skills/
        ctx.skills_root() / "_shared",             # a separately configured skills tree
    ]
    for cand in candidates:
        if (cand / "agentflow" / module).is_file():
            return cand, ""
    return None, (
        f"agentflow/{module} not found under any of: {', '.join(str(c) for c in candidates)} - "
        "neither this clone nor skills_root has a skills/_shared package"
    )


def _registry_cli_root(ctx: Context) -> tuple[Optional[Path], str]:
    """Directory to run ``python -m agentflow.registry`` from, or (None, why)."""
    root, why = _shared_package_root(ctx, "registry.py")
    if root is None:
        return None, f"{why}; cannot execute the registry CLI"
    return root, ""


#: What `notify()` reads when the suite's own config does not resolve — its documented
#: fallback (`ntfy.py`, `_resolved_notify`). Stripping this pair is therefore never
#: WRONG, only ever incomplete; `_notify_env_names` is what completes it.
_DEFAULT_NOTIFY_ENV_NAMES = ("NTFY_URL", "NTFY_TOPIC")


def _notify_env_names(root: Path) -> tuple[str, ...]:
    """Every env var `notify()` might read for a destination — resolved, not assumed.

    Since gh#133 `notify()` honours `notify.url_env` / `notify.topic_env` from the
    resolved config. A strip keyed on the literal pair therefore covers the default
    install and silently stops covering an install that renamed them — and a renamed
    install is the one shape where this strip is the only thing between an acceptance
    drill and a real notification host.

    Returns the resolved names AND the defaults, because `notify()` itself falls back
    to the defaults when the config does not resolve. The child could read either, so
    the child inherits neither.

    An unresolvable config here degrades to the defaults — exactly the behaviour that
    shipped before this function existed. The failure is handled by stripping fewer
    names rather than by raising, because a drill helper has no channel to report on
    and refusing to launch would turn a missing config into a failed acceptance run.
    """
    names = list(_DEFAULT_NOTIFY_ENV_NAMES)
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from agentflow.config import load_environment

        notify_cfg = load_environment().notify
        for attr in ("url_env", "topic_env"):
            value = getattr(notify_cfg, attr, None)
            if value and value not in names:
                names.append(value)
    except Exception:
        # Documented above: degrade to the defaults. Deliberately not re-raised.
        return tuple(names)
    return tuple(names)


def _isolated_child_env(root: Path) -> dict[str, str]:
    """A copy of this process's environment that is safe to hand a WRITING child.

    The child inherits neither a live fleet registry path nor a reachable
    notification host - a stray fleet ping must never leave the box from an
    acceptance check. The notification names are RESOLVED rather than matched
    literally, so the guard cannot quietly stop covering an install that renamed
    them (gh#227).

    One helper rather than a strip at each call site, because a strip at each call
    site is how one of them went without (gh#411): the auditor-scenario driver drove
    `registry status` writes on the operator's unstripped environment for as long as
    it existed. Every child in this file that can write through the registry CLI
    builds its environment here. The one other `dict(os.environ)` in this file, the
    config-override drill, sets its own `AGENTFLOW_CONFIG` and runs only the
    read-only `config show`; `test_drill_env_strip.py` pins that census.
    """
    env = dict(os.environ)
    for var in ("AGENTFLOW_REGISTRY", *_notify_env_names(root)):
        env.pop(var, None)
    return env


def _run_registry_cli(root: Path, argv: list[str]) -> tuple[Optional[int], str, str, str]:
    """Run the registry CLI as a real subprocess. Returns (rc, stdout, stderr, error)."""
    env = _isolated_child_env(root)
    flags = 0
    if os.name == "nt":  # keep the child out of the parent's console group
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [sys.executable, "-m", _CLI_MODULE, *argv],
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        return None, "", "", f"registry CLI timed out after {_CLI_TIMEOUT:.0f}s: {argv}"
    except OSError as exc:
        return None, "", "", f"could not exec the registry CLI: {type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr, ""


def _is_argparse_rejection(rc: Optional[int], stderr: str) -> bool:
    """True when argparse rejected the command SURFACE (unknown subcommand/flag).

    argparse exits 2 and leads with a usage block; a RegistryError also exits 2
    but prints only its own message. Telling them apart is what lets C12 fall
    back to the other spelling instead of reading a surface error as a verdict.
    """
    return rc == 2 and stderr.lstrip().lower().startswith("usage:")


def _run_deploy_take(root: Path, base: list[str], extra: list[str]):
    """Run the deploy-take mutation under whichever spelling the CLI accepts."""
    first = None
    for spelling in _DEPLOY_TAKE_SPELLINGS:
        rc, out, err, fail = _run_registry_cli(root, [*spelling, *base, *extra])
        attempt = (rc, out, err, fail, " ".join(spelling))
        if fail or not _is_argparse_rejection(rc, err):
            return attempt
        if first is None:
            first = attempt
    return first


def _seed_registry(root: Path, tmp: Path) -> tuple[Optional[Path], str]:
    """Create a schema-valid registry via the CLI's own `init`. Returns (path, error)."""
    path = tmp / "registry.md"
    rc, _out, err, fail = _run_registry_cli(
        root, ["init", "--file", str(path), "--project", "demo"]
    )
    if fail:
        return None, fail
    if rc != 0 or not path.exists():
        return None, f"seeding via `registry init` failed (rc={rc}): {err.strip()!r}"
    return path, ""


def _qblock(qid: str, state: str, urgency: str, raised_at: str) -> str:
    """One `### Q-NNNN` block in REGISTRY.md field order."""
    answered = state == "answered"
    return "\n".join(
        [
            f"### {qid}",
            f"- state: {state}",
            f"- urgency: {urgency}",
            "- raised_by: lead-dev",
            "- work: E-0001",
            f"- raised_at: {raised_at}",
            f"- question: drill probe {qid}",
            "- default: proceed as stated",
            "- assumption: the stated default holds",
            "- answer: " + ("yes" if answered else "-"),
            "- answered_by: " + ("human" if answered else "-"),
            "- answered_at: " + ("2026-01-15T00:00:00Z" if answered else "-"),
        ]
    )


#: (qid, state, urgency, raised_at), written to the file in THIS order - which is
#: deliberately not the answer, so a command that dumps the section verbatim
#: cannot pass. The seed also breaks every degenerate sort that would coincide
#: with the contract's ordering on a bare three-row fixture:
#:   contract -> 0014, 0015, 0013, 0012  (halted oldest-first, waiting, advisory)
#:   file     -> 0012, 0013, 0014, 0015
#:   oldest   -> 0012, 0014, 0013, 0015
#:   newest   -> 0015, 0013, 0014, 0012
#:   by id    -> 0012, 0013, 0014, 0015
#: Q-0011 is answered, not open, and must not appear at all.
_C11_SEED = (
    ("Q-0011", "answered", "halted", "2025-12-01T00:00:00Z"),
    ("Q-0012", "open", "advisory", "2026-01-01T00:00:00Z"),
    ("Q-0013", "open", "waiting", "2026-03-03T00:00:00Z"),
    ("Q-0014", "open", "halted", "2026-02-02T00:00:00Z"),
    ("Q-0015", "open", "halted", "2026-05-05T00:00:00Z"),
)
_C11_EXPECTED = ["Q-0014", "Q-0015", "Q-0013", "Q-0012"]
_C11_CLOSED = "Q-0011"


def c11_question_pump_orders_by_class(ctx: Context) -> Result:
    """`read --open-questions` emits halted, then waiting, then advisory.

    Class beats recency. The dev-manager pass drains this list top-down, so an
    ordering that lets an old advisory outrank a halted lane answers the one
    loop that is actually stopped last - the failure the drill step used to
    catch only by being run by hand.

    Asserts POSITIONS, not membership: every wrong sort this fixture can produce
    still contains all four ids, so "they are all there" proves nothing.
    """
    root, err = _registry_cli_root(ctx)
    if root is None:
        return Result(FAIL, err)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path, err = _seed_registry(root, tmp)
        if path is None:
            return Result(FAIL, err)
        text = path.read_text(encoding="utf-8")
        marker = "\n## Blockers"
        if marker not in text:
            return Result(FAIL, "seeded registry has no '## Blockers' section to anchor against")
        blocks = "\n\n".join(_qblock(*row) for row in _C11_SEED)
        path.write_text(text.replace(marker, f"\n{blocks}\n{marker}", 1), encoding="utf-8")
        rc, out, cli_err, fail = _run_registry_cli(
            root, ["read", "--file", str(path), "--project", "demo", "--open-questions"]
        )
    if fail:
        return Result(FAIL, fail)
    if rc != 0:
        return Result(FAIL, f"`read --open-questions` exited {rc}: {cli_err.strip()!r}")
    missing = [qid for qid in _C11_EXPECTED if qid not in out]
    if missing:
        return Result(FAIL, f"open question(s) {missing} absent from the emitted list: {out!r}")
    emitted = sorted(_C11_EXPECTED, key=out.find)
    if emitted != _C11_EXPECTED:
        return Result(
            FAIL,
            f"open-question order is {emitted}, want {_C11_EXPECTED} (halted before waiting "
            "before advisory, oldest first within a class) - recency beat class",
        )
    if _C11_CLOSED in out:
        return Result(FAIL, f"non-open question {_C11_CLOSED} leaked into the open list: {out!r}")
    return Result(
        PASS,
        f"`read --open-questions` emitted {emitted} - class beats recency; "
        f"{_C11_CLOSED} (answered) excluded",
    )


#: Free-text directive plus the substring the acknowledgement must match. There
#: is no scope field to classify on: the gate is acknowledgement, not parsing.
_C12_DIRECTIVE = "no deploys until the release note is signed off"
_C12_ACK = "release note is signed off"
_C12_ACK_FLAG = "--ack-directive"
_C12_REFUSE_RC = 4
_C12_TAKER = "lead-dev"


def c12_directives_gate_the_merge_path(ctx: Context) -> Result:
    """An active Directive refuses deploy-take with exit 4 until acknowledged.

    Three independent claims, because any one alone is passable by an
    implementation that is still wrong:

    1. the refusal uses exit 4 - distinct from 2 (ownership/schema) and 3 (lock
       held), so a caller can tell "a human said something" from "you have a
       bug";
    2. the file is byte-identical afterwards - the guard runs BEFORE the
       mutation rather than rolling one back;
    3. the same call carrying a matching --ack-directive succeeds and the deploy
       lock holder is actually set - a gate that cannot be passed is an outage.
    """
    root, err = _registry_cli_root(ctx)
    if root is None:
        return Result(FAIL, err)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path, err = _seed_registry(root, tmp)
        if path is None:
            return Result(FAIL, err)
        base = [
            "--file", str(path),
            "--lock-dir", str(tmp / "locks"),
            "--project", "demo",
            "--timeout", "30",
        ]
        rc, _out, cli_err, fail = _run_registry_cli(
            root, ["directive", *base, "--actor", "dev-manager", "--text", _C12_DIRECTIVE]
        )
        if fail:
            return Result(FAIL, fail)
        if rc != 0:
            return Result(FAIL, f"could not seed an active Directive (rc={rc}): {cli_err.strip()!r}")

        take = [*base, "--actor", _C12_TAKER]
        before = path.read_bytes()
        rc, out, cli_err, fail, spelling = _run_deploy_take(root, take, [])
        if fail:
            return Result(FAIL, fail)
        said = f"{out}\n{cli_err}"
        problems = []
        if rc != _C12_REFUSE_RC:
            problems.append(
                f"unacked `{spelling}` exited {rc}, want {_C12_REFUSE_RC} "
                f"(2=ownership/schema, 3=lock held); said {said.strip()!r}"
            )
        if path.read_bytes() != before:
            problems.append(
                "registry file changed on the REFUSED take - the guard must run before "
                "any mutation, not roll one back"
            )
        if _C12_ACK_FLAG not in said:
            problems.append(f"refusal never names {_C12_ACK_FLAG}: {said.strip()!r}")
        if _C12_ACK not in said:
            problems.append(f"refusal never quotes the unacked directive: {said.strip()!r}")
        if problems:
            return Result(FAIL, "; ".join(problems))

        rc, out, cli_err, fail, spelling = _run_deploy_take(root, take, [_C12_ACK_FLAG, _C12_ACK])
        if fail:
            return Result(FAIL, fail)
        if rc != 0:
            return Result(
                FAIL,
                f"acknowledged `{spelling}` still refused (rc={rc}): "
                f"{(out + cli_err).strip()!r} - a gate that cannot be passed is an outage",
            )
        held = re.search(r"holder:\s*(?P<who>[^,\n]+?)\s*,", path.read_text(encoding="utf-8"))
        holder = held.group("who") if held else None
    if holder != _C12_TAKER:
        return Result(
            FAIL,
            f"deploy lock holder is {holder!r} after an acknowledged take, want {_C12_TAKER!r}",
        )
    return Result(
        PASS,
        f"`{spelling}` refused with exit {_C12_REFUSE_RC} naming {_C12_ACK_FLAG}, left the file "
        f"byte-identical, and the acknowledged take set holder={_C12_TAKER}",
    )


# --- C15: the steward's delegated sweep disposes of every open question (gh#224) ----------
#: (qid, urgency, raised_at, raised_by). The file order is the id order, which is not the
#: sweep order. Q-0023 is the NEWEST and an advisory - it sorts LAST under the read's own
#: class-then-age order - but a parked Queue row waits on it, so the sweep must take it FIRST.
#: A sweep that only obeyed `read --open-questions` would answer it last, which is the gh#224
#: shape: the board's widest constraint sitting behind its own timestamp.
_C15_SEED = (
    ("Q-0021", "advisory", "2026-01-01T00:00:00Z", "lead-dev"),
    ("Q-0022", "waiting", "2026-02-02T00:00:00Z", "lead-dev"),
    ("Q-0023", "advisory", "2026-03-03T00:00:00Z", "jr-dev-1"),
    ("Q-0024", "advisory", "2026-04-04T00:00:00Z", "lead-dev"),
)
_C15_PARKED_ROW = "| E-0031-demo | demo work | jr-dev-1 | jr-dev-1 | parked | Q-0023 | - |"
_C15_READ_ORDER = ["Q-0022", "Q-0021", "Q-0023", "Q-0024"]
_C15_SWEEP_ORDER = ["Q-0023", "Q-0022", "Q-0021", "Q-0024"]
#: The classification is JUDGEMENT, and no CI run can make it - it is supplied as data, exactly
#: as scenario F supplies the bug-vs-epic call. What C15 covers is everything around it.
#: Two declines, so the ONE-note-for-several form the skill allows is the form exercised.
_C15_DISPOSITION = {"Q-0021": "answer", "Q-0022": "decline", "Q-0023": "answer", "Q-0024": "decline"}
_C15_REASON = {"Q-0022": "data-model change (new table)", "Q-0024": "provider choice (spend)"}
_C15_CAVEAT = (
    "classification supplied as data and the sweep order restated here from the skill's prose, "
    "not read from it; the delegated-vs-human-only judgement is not covered"
)


def _c15_qblock(qid: str, urgency: str, raised_at: str, raised_by: str) -> str:
    return "\n".join([
        f"### {qid}", "- state: open", f"- urgency: {urgency}", f"- raised_by: {raised_by}",
        "- work: -", f"- raised_at: {raised_at}", f"- question: drill probe {qid}",
        "- default: proceed as stated", "- assumption: the stated default holds",
        "- answer: -", "- answered_by: -", "- answered_at: -",
    ])


def _waiting_on_ids(text: str) -> set:
    """Every Q- id a `## Agents` or `## Queue` row names in its `waiting_on` column.

    Read by HEADER, not by position: the two tables put the column in different places, and a
    positional read of one would silently return the other's `notes` or `eta`.
    """
    ids, col = set(), None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            col = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if "waiting_on" in cells:
            col = cells.index("waiting_on")
        elif col is not None and len(cells) > col and cells[col].startswith("Q-"):
            ids.add(cells[col])
    return ids


def _field(text: str, qid: str, name: str) -> Optional[str]:
    m = re.search(rf"^###\s+{re.escape(qid)}\s*$(?P<body>.*?)(?=^###\s|^##\s|\Z)", text, re.M | re.S)
    if m is None:
        return None
    fm = re.search(rf"^-\s*{name}:\s*(.*?)\s*$", m.group("body"), re.M)
    return fm.group(1) if fm else None


def c15_steward_sweep_disposes_of_every_open_question(ctx: Context) -> Result:
    """steward domain P, *Delegated-authority answering*: every wake, every open question gets
    a recorded outcome - answered `--by steward`, or declined with a reason routed to the seat.

    A reconstruction, not a reading of the skill: the ordering rule and the classification are
    restated here as code and data, and what runs for real is every registry write and read the
    procedure depends on, through the real CLI against a throwaway registry. It asserts four
    things a live wake would have to satisfy:

    1. ORDER - a question a parked row waits on is taken first, even though the read ranks it
       last; the board carries the data the lift needs (`waiting_on`), read by header;
    2. NO HUMAN RELAY - a human-only question cannot be closed by the steward signing the
       human's name (`--by human` refused, file byte-identical);
    3. EVERY OUTCOME RECORDED - after the sweep nothing that was open is un-dispositioned: each
       id is answered by the steward or named in a steward -> dev-manager decline Note - here
       ONE note carrying both declines, the combined form the skill allows;
    4. THE ANSWER REACHES THE WAITER - the parked row's raiser sees it via `--answered-for`.
    """
    root, err = _registry_cli_root(ctx)
    if root is None:
        return Result(FAIL, err)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path, err = _seed_registry(root, tmp)
        if path is None:
            return Result(FAIL, err)
        text = path.read_text(encoding="utf-8")
        q_marker, row_anchor = "\n## Blockers", "|---|---|---|---|---|---|---|\n\n## Questions"
        if q_marker not in text or row_anchor not in text:
            return Result(FAIL, "seeded registry lost the Queue/Blockers anchors this drill seeds against")
        blocks = "\n\n".join(_c15_qblock(*row) for row in _C15_SEED)
        text = text.replace(q_marker, f"\n{blocks}\n{q_marker}", 1)
        text = text.replace(row_anchor, row_anchor.replace("\n\n", f"\n{_C15_PARKED_ROW}\n\n", 1), 1)
        path.write_text(text, encoding="utf-8")
        base = ["--file", str(path), "--lock-dir", str(tmp / "locks"), "--project", "demo",
                "--timeout", "30"]

        def cli(*argv):
            return _run_registry_cli(root, [*argv[:1], *base, *argv[1:]])

        rc, out, cli_err, fail = cli("validate")
        if fail or rc != 0:
            return Result(FAIL, fail or f"seeded registry is not valid (rc={rc}): {cli_err.strip()!r}")

        # Step 1 + 2: read, then lift what the board says is holding work.
        rc, out, cli_err, fail = cli("read", "--open-questions")
        if fail or rc != 0:
            return Result(FAIL, fail or f"`read --open-questions` exited {rc}: {cli_err.strip()!r}")
        read_order = sorted((q for q in _C15_DISPOSITION if q in out), key=out.find)
        if read_order != _C15_READ_ORDER:
            return Result(FAIL, f"read order is {read_order}, want {_C15_READ_ORDER} - the seed no "
                                "longer puts the blocking question last, so the lift proves nothing")
        blocking = _waiting_on_ids(path.read_text(encoding="utf-8"))
        sweep = [q for q in read_order if q in blocking] + [q for q in read_order if q not in blocking]
        if sweep != _C15_SWEEP_ORDER:
            return Result(FAIL, f"sweep order is {sweep}, want {_C15_SWEEP_ORDER} (waiting_on ids "
                                f"read off the board: {sorted(blocking)})")

        # Steps 3 + 4, in sweep order, with the classification supplied as data.
        problems, declined = [], []
        for qid in sweep:
            if _C15_DISPOSITION[qid] == "answer":
                rc, _o, cli_err, fail = cli("answer", "--actor", "steward", "--id", qid,
                                            "--answer", f"drill answer {qid}", "--by", "steward")
                if fail or rc != 0:
                    problems.append(fail or f"steward could not answer {qid} (rc={rc}): {cli_err.strip()!r}")
                continue
            before = path.read_bytes()
            rc, _o, cli_err, fail = cli("answer", "--actor", "steward", "--id", qid,
                                        "--answer", "x", "--by", "human")
            if fail:
                return Result(FAIL, fail)
            if rc == 0 or path.read_bytes() != before:
                problems.append(f"steward closed human-only {qid} as `--by human` (rc={rc})")
            declined.append(f"{qid} human-only: {_C15_REASON[qid]}")
        text = "; ".join(declined) + " - left open for the seat"
        rc, _o, cli_err, fail = cli("note", "--actor", "steward", "--to", "dev-manager", "--text", text)
        if fail or rc != 0:
            problems.append(fail or f"combined decline note refused (rc={rc}): {cli_err.strip()!r}")
        if problems:
            return Result(FAIL, "; ".join(problems))

        final = path.read_text(encoding="utf-8")
        rc, notes, cli_err, fail = cli("read", "--notes-for", "dev-manager")
        if fail or rc != 0:
            return Result(FAIL, fail or f"`read --notes-for dev-manager` exited {rc}: {cli_err.strip()!r}")
        rc, answered, cli_err, fail = cli("read", "--answered-for", "jr-dev-1")
        if fail or rc != 0:
            return Result(FAIL, fail or f"`read --answered-for jr-dev-1` exited {rc}: {cli_err.strip()!r}")
    undisposed = []
    decline_lines = [ln for ln in notes.splitlines() if "steward -> dev-manager:" in ln]
    for qid, how in _C15_DISPOSITION.items():
        if how == "answer":
            if (_field(final, qid, "state"), _field(final, qid, "answered_by")) != ("answered", "steward"):
                undisposed.append(f"{qid} not answered by steward")
        elif not any(re.search(rf"\b{qid} human-only:", ln) for ln in decline_lines):
            undisposed.append(f"{qid} left open with no decline reason on the board")
        elif _field(final, qid, "state") != "open":
            undisposed.append(f"human-only {qid} did not stay open for the seat")
    if "Q-0023" not in answered:
        undisposed.append("the parked row's raiser cannot see the answer via --answered-for")
    if undisposed:
        return Result(FAIL, "; ".join(undisposed))
    return Result(
        PASS,
        f"swept {sweep} (blocking Q-0023 lifted over read order {read_order}); answered as steward, "
        f"`--by human` refused, human-only Q-0022 + Q-0024 left open with reasons in one routed "
        f"note ({_C15_CAVEAT})",
    )


# ---------------------------------------------------------------------------
# Scenario D - builder command self-paced wake-continuation
# ---------------------------------------------------------------------------


def _no_d_wake_row() -> Result:
    """D1-D3's verdict when the log holds no scenario=D row (gh#628 review).

    `registry-after.md` is written by the consume-first run of the base D procedure,
    so its presence proves D WAS recorded - a missing D row is then the regression
    B9 treats a missing B row as, not a debt.
    """
    after = RECORDS_DIR / "scenarioD" / "registry-after.md"
    if after.exists():
        return Result(
            FAIL,
            "scenario D recorded (records/scenarioD/registry-after.md present) but no "
            "scenario=D wake row logged",
        )
    return _unrecorded("D", f"harness/stubs/{WAKE_LOG.name} scenario=D row")


def _builder_cmd(ctx: Context) -> str:
    """The bare command token from builder_entry, e.g. '/lead-dev'."""
    return (ctx.builder_entry or "").strip()


_ARGUMENTS_TOKEN_RE = re.compile(r"\$\{?(?:ARGUMENTS|\d+)\}?")
_PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")


def d1_continuation_reinvokes_builder(ctx: Context) -> Result:
    """The continuation re-invokes builder_entry with the RESOLVED arguments as literal text.

    gh#602: lead-dev SKILL.md forbids writing `$ARGUMENTS` into a ScheduleWakeup prompt -
    the token expands when a human types the command, not when a wake re-invokes it, so
    every self-wake re-enters on the session-default project. The accepted shapes are
    `<cmd> --project <name> <target>` and `<cmd> <target>`.

    Limit: neither harness.config.json nor the wake row records the Scenario D LAUNCH
    arguments, so D1 cannot tell whether the launch passed `--project` and cannot demand
    it. It requires only: the prompt starts with builder_entry as a whole word, no literal
    `$ARGUMENTS` / `$N`, a well-formed `--project <name>` / `--project=<name>` wherever one
    appears, and - after stripping those and the bare `--recovery-guard` as Step 0 does -
    a non-empty literal target with no leftover `--` flag. Scenario D
    launches with a `<target>` (run-scenario.md), so an empty target is a FAIL here even
    though lead-dev allows one in general.
    """
    cmd = _builder_cmd(ctx)
    if not cmd:
        return Result(FAIL, "harness.config.json builder_entry is empty")
    rows = ctx.wake_rows("D")
    if not rows:
        return _no_d_wake_row()
    row = rows[0]
    prompt = row["prompt"]
    # The prompt IS the command line a wake re-enters, so it must begin with the command
    # as a whole word - a substring match accepted `/lead-devX foo` and `run /lead-dev E-1`.
    rest = prompt[len(cmd):] if prompt.startswith(cmd) else None
    if rest is None or (rest and not rest[0].isspace()):
        return Result(FAIL, f"continuation prompt does not re-invoke builder_entry {cmd!r}: {prompt!r}")
    if _ARGUMENTS_TOKEN_RE.search(prompt):
        return Result(
            FAIL,
            f"continuation prompt carries a literal $ARGUMENTS/$N token, which a wake does not "
            f"expand (write the resolved --project/target as literal text): {prompt!r}",
        )
    # Strip exactly what lead-dev Step 0 strips (SKILL.md Step 0.1): `--project <name>` /
    # `--project=<name>` anywhere, and the bare `--recovery-guard`. What remains is <target>.
    tokens = rest.split()
    target: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--recovery-guard":
            i += 1
            continue
        if tok == "--project" or tok.startswith("--project="):
            if tok == "--project":
                name = tokens[i + 1] if i + 1 < len(tokens) else ""
                i += 2
            else:
                name = tok.split("=", 1)[1]
                i += 1
            if not name or name.startswith("-") or _PLACEHOLDER_RE.match(name):
                return Result(FAIL, f"continuation prompt has --project with no literal project name: {prompt!r}")
            continue
        target.append(tok)
        i += 1
    if not target:
        return Result(FAIL, f"continuation prompt has no literal target after {cmd!r}: {prompt!r}")
    if any(t.startswith("--") for t in target):
        return Result(FAIL, f"continuation prompt target carries an unrecognised flag: {prompt!r}")
    if any(_PLACEHOLDER_RE.match(t) for t in target):
        return Result(FAIL, f"continuation prompt carries an unresolved <placeholder> target: {prompt!r}")
    return Result(PASS, f"continuation re-invokes {cmd} with literal resolved arguments: {prompt!r}")


def d2_verbatim_baseline(ctx: Context) -> Result:
    rows = ctx.wake_rows("D")
    if not rows:
        return _no_d_wake_row()
    prompt = rows[0]["prompt"]
    if not prompt.strip():
        return Result(FAIL, "scenario=D continuation prompt is empty")
    return Result(PASS, f"verbatim continuation-prompt baseline captured: {prompt!r}")


def d3_no_second_wake(ctx: Context) -> Result:
    rows = ctx.wake_rows("D")
    if not rows:
        return _no_d_wake_row()
    if len(rows) != 1:
        return Result(FAIL, f"a second wake fired: {len(rows)} scenario=D rows (stub should record one)")
    return Result(PASS, "exactly one scenario=D wake row; no spontaneous second wake")


def _registry_question_state(text: str, qid: str) -> Optional[str]:
    """The `- state:` value of question block `### <qid>`, or None if absent."""
    m = re.search(
        rf"^###\s+{re.escape(qid)}\s*$(?P<body>.*?)(?=^###\s|\Z)",
        text,
        re.M | re.S,
    )
    if m is None:
        return None
    sm = re.search(r"^-\s*state:\s*(\S+)", m.group("body"), re.M)
    return sm.group(1) if sm else None


def _registry_queue_row(text: str, eid: str) -> Optional[dict]:
    """The Queue table row for `eid` as a dict, or None if absent.

    Columns per docs/REGISTRY.md: id | work item | lane | claimed_by | state |
    waiting_on | notes.
    """
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) >= 6 and cells[0] == eid:
            return {
                "id": cells[0],
                "lane": cells[2],
                "claimed_by": cells[3],
                "state": cells[4],
                "waiting_on": cells[5],
            }
    return None


def _import_registry():
    """The registry module whose row-key rule D4 defers to, or None. Never raises."""
    try:
        from agentflow import registry  # noqa: PLC0415 - optional, degrades to FAIL
    except Exception:  # noqa: BLE001 - a missing install must not crash the harness
        return None
    return registry


def d4_consume_first(ctx: Context) -> Result:
    """Consume-first (REGISTRY.md firing contract step 2): a builder wake that
    finds an answered Question row applies it BEFORE claiming new work.

    Reads the seed ids from fixtures/scenarioD-registry-before.md (an answered
    question, a Queue row parked on it, and a fresh ready row) and asserts the
    recorded post-wake registry (records/scenarioD/registry-after.md) shows the
    answer applied: question flipped to `applied`, the parked row resumed, and
    the fresh ready row NOT claimed in its place.

    No recording => SKIP (an honest, actionable debt). A present record that
    violates any of those invariants => FAIL, and so does a broken tracked
    fixture (absent, malformed, or not seeded `answered`): D4 reads the ids it
    asserts on out of that file, so a fixture it cannot resolve has to be loud
    rather than quietly unproven. A D4 FAIL therefore always means the harness
    saw something wrong on this machine. What it can no longer mean is "nobody
    recorded a wake here" - that ambiguity is what made the old red unreadable.
    """
    fixture = FIXTURES_DIR / "scenarioD-registry-before.md"
    if not fixture.exists():
        return Result(FAIL, "fixtures/scenarioD-registry-before.md not found")
    before = fixture.read_text(encoding="utf-8")

    # ASCII digits only (gh#1123): a non-ASCII-digit id is not a registry id.
    qm = re.search(r"^###\s+(Q-[0-9]+)\s*$", before, re.M)
    # A Queue row is whatever the registry's own parser keeps (gh#1136): a private pattern here
    # missed named ids (`E-0101-export`) and accepted ids the registry drops (`E-1`).
    registry = _import_registry()
    if registry is None:
        return Result(FAIL, "agentflow.registry not importable - D4 cannot tell a Queue row id")
    parked_id = None
    ready_id = None
    for m in re.finditer(r"^\|\s*([^|\n]*?)\s*\|", before, re.M):
        if not registry._row_key_ok("Queue", m.group(1)):
            continue
        row = _registry_queue_row(before, m.group(1))
        if row is None:
            continue
        if row["state"] == "parked" and parked_id is None:
            parked_id = row["id"]
        elif row["state"] == "ready" and ready_id is None:
            ready_id = row["id"]
    if qm is None or parked_id is None or ready_id is None:
        return Result(
            FAIL,
            "scenarioD-registry-before.md fixture malformed: need one answered "
            f"question + one parked + one ready Queue row (found q={qm and qm.group(1)}, "
            f"parked={parked_id}, ready={ready_id})",
        )
    qid = qm.group(1)
    if _registry_question_state(before, qid) != "answered":
        return Result(FAIL, f"fixture question {qid} must seed state: answered")

    after_path = RECORDS_DIR / "scenarioD" / "registry-after.md"
    if not after_path.exists():
        # SKIP, not FAIL: nothing recorded means nothing was observed, which is
        # not the same as a builder behaving wrongly. Keeping this red on every
        # fresh clone would make a D4 FAIL mean two things at once.
        return _unrecorded(
            "D", "harness/records/scenarioD/registry-after.md", "Scenario D consume-first"
        )
    after = after_path.read_text(encoding="utf-8")

    q_state = _registry_question_state(after, qid)
    if q_state != "applied":
        return Result(FAIL, f"{qid} state is {q_state!r} after the wake, want 'applied' (answer not consumed)")
    resumed = _registry_queue_row(after, parked_id)
    if resumed is None:
        return Result(FAIL, f"parked row {parked_id} missing from recorded registry")
    if resumed["state"] == "parked":
        return Result(FAIL, f"{parked_id} still parked after the wake - answered question not applied")
    fresh = _registry_queue_row(after, ready_id)
    if fresh is not None and fresh["claimed_by"] not in ("-", ""):
        return Result(
            FAIL,
            f"builder claimed fresh work {ready_id} (claimed_by={fresh['claimed_by']}) "
            f"instead of consuming the answered {qid} first",
        )
    return Result(
        PASS,
        f"consume-first held: {qid} applied, {parked_id} resumed (state={resumed['state']}), "
        f"{ready_id} left unclaimed",
    )


# ---------------------------------------------------------------------------
# Scenario F - dev-manager issue intake (triage pass)
# Scenario G - lead-dev sees only triaged, non-epic issues
# ---------------------------------------------------------------------------

_TRIAGED_LABEL = "triaged"
_EPIC_LABEL = "epic"
_BUG_LABEL = "bug"
_ISSUES_FIXTURE = FIXTURES_DIR / "scenarioF-issues-before.json"
#: The contract table's disposition per seed role, as DATA (see the file's own `_why`).
#: An assertion that also chose the disposition would be asserting its own script.
_DISPOSITIONS = FIXTURES_DIR / "scenarioF-dispositions.json"
#: The carve F3 expects, tracked rather than recorded - the scenario-D precedent.
_CARVED_DIR = FIXTURES_DIR / "scenarioF-carved"

#: Stand-ins for the toy tracker. Nothing reaches a network: `label_issue` and
#: `list_open_issues` both take an injectable Runner, and these are the values that make
#: `_tracker_active` true so the argv is actually built.
_STUB_HOST_REF = "toy-owner/toy-repo"
_STUB_KIND = "github-issues"
_STUB_CLI = "gh"


def _seeded_triaged_epic(before: dict) -> Optional[int]:
    """The seed carrying BOTH `triaged` and `epic` - the one that makes G1 discriminating.

    Same rule as `_scenario_f_seed`: classify by the seeded labels, never by number. It lives
    beside that function rather than inside it so the five-tuple every assertion unpacks stays
    as it is; only G1 needs this role.

    Why the role exists at all: without it every seed in the fixture is decided by the
    `triaged` clause alone, so an implementation of `builder_selectable` that dropped the
    `epic` exclusion entirely still returned the right answer. Verified by mutation - that
    version of G1 passed, which is how the gap was found.
    """
    for num in sorted(before):
        labels = before[num]["labels"]
        if _TRIAGED_LABEL in labels and _EPIC_LABEL in labels:
            return num
    return None


def _load_issue_list(path: Path, what: str):
    """Return ({number: {"labels": set, "title": str}}, error). Shape-checked."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{what} unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(raw, list):
        return None, f"{what} must be a JSON list of issues"
    issues: dict[int, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("number"), int):
            return None, f"{what} entry lacks an integer 'number': {entry!r}"
        labels = entry.get("labels") or []
        if isinstance(labels, (str, bytes)) or not isinstance(labels, (list, tuple)):
            return None, f"{what} issue #{entry['number']} 'labels' must be a list"
        # Accept both the fixture's bare strings and the CLI's {"name": ...} objects.
        try:
            names = {
                (lab.get("name") if isinstance(lab, dict) else lab) for lab in labels
            }
        except TypeError:
            return None, f"{what} issue #{entry['number']} has an unhashable labels entry"
        if not all(isinstance(n, str) for n in names):
            return None, f"{what} issue #{entry['number']} has a malformed labels entry"
        issues[entry["number"]] = {"labels": names, "title": str(entry.get("title", ""))}
    return issues, None


def _scenario_f_seed():
    """Return (bug_no, epic_no, triaged_no, before, error) read from the fixture.

    Classification is by the seeded labels, never by hardcoded numbers: the
    untriaged `bug`-labelled issue, the untriaged unlabelled (epic-sized) issue,
    and the one already carrying `triaged`.
    """
    if not _ISSUES_FIXTURE.exists():
        return None, None, None, None, "fixtures/scenarioF-issues-before.json not found"
    before, err = _load_issue_list(_ISSUES_FIXTURE, "scenarioF-issues-before.json fixture")
    if before is None:
        return None, None, None, None, err
    bug_no = epic_no = triaged_no = None
    for num in sorted(before):
        labels = before[num]["labels"]
        if _TRIAGED_LABEL in labels:
            triaged_no = triaged_no if triaged_no is not None else num
        elif _BUG_LABEL in labels:
            bug_no = bug_no if bug_no is not None else num
        elif not labels:
            epic_no = epic_no if epic_no is not None else num
    if None in (bug_no, epic_no, triaged_no):
        return (
            None, None, None, None,
            "scenarioF-issues-before.json fixture malformed: need one untriaged `bug`, "
            "one untriaged unlabelled (epic-sized) and one `triaged` issue "
            f"(found bug={bug_no}, epic={epic_no}, triaged={triaged_no})",
        )
    return bug_no, epic_no, triaged_no, before, None


def _import_issue_intake():
    """The product module under test, or None. Never raises - F/G degrade to FAIL."""
    try:
        from agentflow import issue_intake  # noqa: PLC0415 - optional, degrades to FAIL
    except Exception:  # noqa: BLE001 - a missing install must not crash the harness
        return None
    return issue_intake


#: What the intake pass is judged to have decided. Not derivable in CI, by construction:
#: dev-manager SKILL.md 3.4 names its own decider ("dev-manager (delegated)") and no triage
#: function exists. Every PASS below says so rather than letting green imply otherwise -
#: Q-10035 made that the load-bearing part of the answer, not a nicety: "a green F2 that
#: reads as proof the fleet classifies correctly is a vacuous gate".
#:
#: IF THE DISPOSITION EVER MOVES INTO CODE, revisit F2/F3 to assert it directly and delete
#: this caveat (Q-10035). Until then the caveat is the honest half of the assertion.
_JUDGEMENT_CAVEAT = "disposition supplied by fixture; the bug-vs-epic judgement is not covered"


def _reconstruct_scenario_f():
    """Replay the triage pass against the tracked fixture. Returns (after, argv, error).

    Three product behaviours do real work here and each can fail the run:

      * `untriaged()` decides WHICH issues the pass visits. The already-`triaged` seed is
        excluded by the product, not skipped by this function - that is the whole of F4.
      * `label_issue()` builds the argv. The after-state is parsed back OUT of that argv,
        so a mis-built `--add-label` shows up as a wrong label set rather than passing
        silently.
      * the Runner seam carries it with no network, which is why this runs in CI at all.
    """
    ii = _import_issue_intake()
    if ii is None:
        return None, None, None, ("agentflow.issue_intake not importable - install the "
                                  "package (`pip install -e skills/_shared`) before running F/G")
    bug_no, epic_no, triaged_no, before, seed_err = _scenario_f_seed()
    if seed_err:
        return None, None, None, seed_err
    try:
        table = json.loads(_DISPOSITIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, None, None, f"scenarioF-dispositions.json unreadable: {type(exc).__name__}: {exc}"
    for role in ("bug", "epic", "pre_triaged"):
        if not isinstance(table.get(role), dict) or "add_labels" not in table[role]:
            return None, None, None, f"scenarioF-dispositions.json lacks an `add_labels` for role {role!r}"

    issues = [ii.Issue(number=n, title=before[n]["title"], labels=tuple(sorted(before[n]["labels"])))
              for n in sorted(before)]

    # The PRODUCT decides who gets visited. `linked` is empty: no spec cites these seeds yet,
    # which is the state a first triage pass runs in.
    try:
        visit = [i.number for i in ii.untriaged(issues, set())]
    except Exception as exc:  # noqa: BLE001 - report, never crash the harness
        return None, None, None, f"untriaged() raised {type(exc).__name__}: {exc}"

    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return 0, "", ""

    role_of = {bug_no: "bug", epic_no: "epic", triaged_no: "pre_triaged"}
    for number in visit:
        # A seed outside the three named roles reaching the visit set is itself the defect
        # F1 reports - never a KeyError, and never silently skipped.
        add = table.get(role_of.get(number, "pre_triaged"), {}).get("add_labels", [])
        if not add:
            continue
        try:
            ii.label_issue(number, add, _STUB_HOST_REF,
                           kind=_STUB_KIND, cli=_STUB_CLI, runner=runner)
        except Exception as exc:  # noqa: BLE001
            return None, None, None, f"label_issue(#{number}) raised {type(exc).__name__}: {exc}"

    # Rebuild the after-state from the argv, NOT from the table: this is the step that makes
    # F2/F3 assertions about `label_issue` rather than restatements of the fixture.
    after = {n: {"labels": set(before[n]["labels"]), "title": before[n]["title"]} for n in before}
    for argv in calls:
        try:
            number = int(argv[argv.index("edit") + 1])
            added = argv[argv.index("--add-label") + 1].split(",")
        except (ValueError, IndexError):
            return None, None, None, f"label_issue built an argv F cannot read back: {argv!r}"
        if number not in after:
            return None, None, None, f"label_issue targeted #{number}, which is not a seeded issue"
        after[number]["labels"] |= {a for a in added if a}
    return after, calls, visit, None


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)", re.S)


def _frontmatter_field(text: str, key: str) -> Optional[str]:
    """Top-level scalar `key:` value from a spec's YAML frontmatter (quotes stripped)."""
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return None
    fm = re.search(rf"^{re.escape(key)}:\s*(?P<val>.*)$", m.group("body"), re.M)
    if fm is None:
        return None
    return fm.group("val").strip().strip("\"'")


def _carved_specs() -> list[Path]:
    """The carve, tracked rather than recorded (scenario-D precedent: never commit a record;
    a fixture is an input the assertion reasons about, which is a different thing).

    Membership comes from the git index (gh#410): an untracked spec dropped beside the carve
    would otherwise vouch for F2/F4 on one machine and not in CI. Rooted at the harness dir,
    not the carve dir, so a carve dir holding only untracked files reads as empty rather than
    tripping the helper's empty-index refusal."""
    if not _CARVED_DIR.is_dir():
        return []
    from agentflow.tracked_paths import tracked_files  # noqa: PLC0415 - lazy so modes that never reach F2/F4 still run without agentflow; this path requires it

    prefix = _CARVED_DIR.relative_to(SCRIPT_DIR).as_posix() + "/"
    return tracked_files(
        SCRIPT_DIR,
        lambda rel: rel.startswith(prefix) and "/" not in rel[len(prefix):] and rel.endswith(".md"),
    )


class _UnreadableSpec(Exception):
    """A carved spec could not be read. Never treat it as 'not sourced from this
    issue' — that turns an unreadable record into a false PASS."""


def _specs_sourced_from(number: int) -> list[Path]:
    """Carved specs whose frontmatter `source:` cites gh#<number>.

    Raises _UnreadableSpec (caught by each caller into a shaped FAIL) rather than
    skipping a spec it cannot read.
    """
    needle = f"gh#{number}"
    hits: list[Path] = []
    for spec in _carved_specs():
        try:
            source = _frontmatter_field(spec.read_text(encoding="utf-8"), "source")
        except (OSError, UnicodeDecodeError) as exc:
            raise _UnreadableSpec(
                f"carved spec {spec.name} unreadable: {type(exc).__name__}: {exc}"
            ) from exc
        if source and re.search(rf"{re.escape(needle)}(?!\d)", source):
            hits.append(spec)
    return hits


def f1_record_present(ctx: Context) -> Result:
    """The pass reconstructs, and it visited exactly the issues `untriaged()` selects.

    Renamed in spirit, not in id: there is no record to be present any more. What it now
    proves is stronger - that the product's own `untriaged()` drew the visit set, so a
    regression that starts re-triaging settled issues reddens here first.
    """
    bug_no, epic_no, triaged_no, _, seed_err = _scenario_f_seed()
    if seed_err:
        return Result(FAIL, seed_err)
    after, calls, visit, err = _reconstruct_scenario_f()
    if after is None:
        return Result(FAIL, err)
    want = sorted([bug_no, epic_no])
    # Assert the VISIT SET, not the edits. An earlier version checked only which issues were
    # edited, and a mutation deleting `untriaged()`'s whole `triaged` filter survived it: the
    # extra issues entered the visit set, drew an empty disposition, and were skipped without
    # any edit. What `untriaged()` DECIDED is the fact this assertion is named after.
    if sorted(visit) != want:
        extra = sorted(set(visit) - set(want))
        note = (f" - `untriaged()` offered already-disposed issue(s) {extra}" if extra else "")
        return Result(FAIL, f"`untriaged()` returned {sorted(visit)}, want {want}{note}")
    edited = sorted({int(a[a.index("edit") + 1]) for a in calls})
    if edited != want:
        return Result(FAIL, f"pass edited {edited}, want {want}")
    return Result(PASS, f"`untriaged()` drew the visit set {sorted(visit)} over "
                        f"{len(after)} seeded issue(s); each edited through the injected "
                        f"Runner, no network ({_JUDGEMENT_CAVEAT})")


def f2_bug_triaged_not_carved(ctx: Context) -> Result:
    bug_no, _, _, _, seed_err = _scenario_f_seed()
    if seed_err:
        return Result(FAIL, seed_err)
    after, _calls, _visit, err = _reconstruct_scenario_f()
    if after is None:
        return Result(FAIL, err)
    row = after.get(bug_no)
    if row is None:
        return Result(FAIL, f"bug #{bug_no} missing from the reconstructed after-state")
    if _TRIAGED_LABEL not in row["labels"]:
        return Result(FAIL, f"bug #{bug_no} not labelled `{_TRIAGED_LABEL}` after the pass: {sorted(row['labels'])}")
    if _BUG_LABEL not in row["labels"]:
        return Result(FAIL, f"bug #{bug_no} lost its `{_BUG_LABEL}` label: {sorted(row['labels'])}")
    if _EPIC_LABEL in row["labels"]:
        return Result(FAIL, f"bug #{bug_no} was labelled `{_EPIC_LABEL}` - bounded defect treated as epic")
    # The search space before the search (gh#218). `_specs_sourced_from` returns []
    # both for "the carve produced no spec citing this bug" and for "there is no
    # carved-spec directory to read", and only the first is evidence. F3's detail
    # already names this count; F2's PASS rested on it without ever reading it.
    present = _carved_specs()
    if not present:
        return Result(
            FAIL,
            f"no spec files under {_CARVED_DIR.name}/ ({len(present)} spec file(s) "
            f"present) - `no spec carved for #{bug_no}` would be a zero over an "
            "unread directory rather than an observation",
        )
    try:
        carved = _specs_sourced_from(bug_no)
    except _UnreadableSpec as exc:
        return Result(FAIL, str(exc))
    if carved:
        return Result(FAIL, f"bug #{bug_no} had a spec carved for it: {[c.name for c in carved]} (bugs get no spec)")
    return Result(PASS, f"bug #{bug_no} ends `{_TRIAGED_LABEL}`+`{_BUG_LABEL}` from the argv "
                        f"`label_issue` built, no spec carved among the {len(present)} spec "
                        f"file(s) present ({_JUDGEMENT_CAVEAT})")


def f3_epic_carved(ctx: Context) -> Result:
    _, epic_no, _, _, seed_err = _scenario_f_seed()
    if seed_err:
        return Result(FAIL, seed_err)
    after, _calls, _visit, err = _reconstruct_scenario_f()
    if after is None:
        return Result(FAIL, err)
    row = after.get(epic_no)
    if row is None:
        return Result(FAIL, f"epic-sized #{epic_no} missing from the reconstructed after-state")
    missing = [lab for lab in (_TRIAGED_LABEL, _EPIC_LABEL) if lab not in row["labels"]]
    if missing:
        return Result(FAIL, f"epic-sized #{epic_no} lacks label(s) {missing} after the pass: {sorted(row['labels'])}")
    try:
        carved = _specs_sourced_from(epic_no)
    except _UnreadableSpec as exc:
        return Result(FAIL, str(exc))
    if not carved:
        return Result(
            FAIL,
            f"no carved spec under fixtures/scenarioF-carved/ cites gh#{epic_no} in its "
            f"`source:` frontmatter ({len(_carved_specs())} spec file(s) present)",
        )
    spec = carved[0]
    try:
        state = _frontmatter_field(spec.read_text(encoding="utf-8"), "current_state")
    except (OSError, UnicodeDecodeError) as exc:
        return Result(FAIL, f"{spec.name} unreadable: {exc}")
    if state != "drafted":
        return Result(FAIL, f"{spec.name} current_state is {state!r}, want 'drafted' (carve must not self-promote)")
    return Result(
        PASS,
        f"epic-sized #{epic_no} ends `{_TRIAGED_LABEL}`+`{_EPIC_LABEL}` from the argv "
        f"`label_issue` built; {spec.name} drafted with source gh#{epic_no} "
        f"({_JUDGEMENT_CAVEAT})",
    )


def f4_already_triaged_untouched(ctx: Context) -> Result:
    _, _, triaged_no, before, seed_err = _scenario_f_seed()
    if seed_err:
        return Result(FAIL, seed_err)
    after, _calls, visit, err = _reconstruct_scenario_f()
    if after is None:
        return Result(FAIL, err)
    if triaged_no in visit:
        return Result(FAIL, f"already-triaged #{triaged_no} entered the visit set - "
                            "`untriaged()` must exclude anything already carrying `triaged`")
    row = after.get(triaged_no)
    if row is None:
        return Result(FAIL, f"already-triaged #{triaged_no} missing from the reconstructed after-state")
    seeded = before[triaged_no]["labels"]
    if row["labels"] != seeded:
        return Result(
            FAIL,
            f"already-triaged #{triaged_no} labels changed: {sorted(seeded)} -> {sorted(row['labels'])} "
            "(triage must skip issues already labelled `triaged`)",
        )
    # Same space guard as F2, same reason (gh#218): "not carved" is only an
    # observation once there is a carved-spec directory to have observed.
    present = _carved_specs()
    if not present:
        return Result(
            FAIL,
            f"no spec files under {_CARVED_DIR.name}/ ({len(present)} spec file(s) "
            f"present) - `not carved` for #{triaged_no} would be a zero over an "
            "unread directory rather than an observation",
        )
    try:
        carved = _specs_sourced_from(triaged_no)
    except _UnreadableSpec as exc:
        return Result(FAIL, str(exc))
    if carved:
        return Result(FAIL, f"already-triaged #{triaged_no} had a spec carved: {[c.name for c in carved]}")
    return Result(PASS, f"already-triaged #{triaged_no} untouched (labels {sorted(seeded)}), not "
                        f"carved among the {len(present)} spec file(s) present - `untriaged()` "
                        f"never offered it to the pass")


def g1_untriaged_invisible_to_builder(ctx: Context) -> Result:
    """The builder's bug selection names the `triaged` bug and never an untriaged one.

    Reads the seed numbers from fixtures/scenarioF-issues-before.json (the same
    stub issue list backs both scenarios) so nothing is hardcoded.
    """
    bug_no, epic_no, triaged_no, before, seed_err = _scenario_f_seed()
    if seed_err:
        return Result(FAIL, seed_err)
    ii = _import_issue_intake()
    if ii is None:
        return Result(FAIL, "agentflow.issue_intake not importable - install the package "
                            "(`pip install -e skills/_shared`) before running G")
    issues = [ii.Issue(number=n, title=before[n]["title"], labels=tuple(sorted(before[n]["labels"])))
              for n in sorted(before)]
    try:
        selected = [i.number for i in ii.builder_selectable(issues)]
    except Exception as exc:  # noqa: BLE001 - report, never crash the harness
        return Result(FAIL, f"builder_selectable() raised {type(exc).__name__}: {exc}")

    if triaged_no not in selected:
        return Result(FAIL, f"builder_selectable() omits the triaged bug #{triaged_no}: {selected}")
    leaked = [f"#{n}" for n in (bug_no, epic_no) if n in selected]
    if leaked:
        return Result(
            FAIL,
            f"builder selection names untriaged issue(s) {leaked} - "
            "unlabelled issues must be invisible to the builder",
        )
    # The OTHER half of the rule, and the half a three-seed fixture could not see: `triaged`
    # is necessary but not sufficient. An epic-labelled issue is a human promotion decision,
    # so a builder picking one up bypasses the section 7c gate.
    epic_seed = _seeded_triaged_epic(before)
    if epic_seed is None:
        return Result(FAIL, "fixture has no `triaged`+`epic` seed, so the `epic` exclusion "
                            "is untested - see _seeded_triaged_epic")
    if epic_seed in selected:
        return Result(
            FAIL,
            f"builder selection names `epic`-labelled #{epic_seed} - `triaged` alone does "
            "not make an issue selectable; an epic needs the human promotion gate",
        )
    # No judgement caveat here, deliberately: unlike F2/F3 this calls the product function
    # that decides, so a green G1 IS the rule holding.
    return Result(PASS, f"builder_selectable() over the tracked fixture returns {selected}: "
                        f"triaged non-epic #{triaged_no} only; untriaged #{bug_no}/#{epic_no} "
                        f"and `epic`-labelled #{epic_seed} all invisible")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def build_registry() -> list[Assertion]:
    A = lambda i, b, f: Assertion(i, "A", b, f)  # noqa: E731
    B = lambda i, b, f: Assertion(i, "B", b, f)  # noqa: E731
    C = lambda i, b, f: Assertion(i, "C", b, f)  # noqa: E731
    D = lambda i, b, f: Assertion(i, "D", b, f)  # noqa: E731
    F = lambda i, b, f: Assertion(i, "F", b, f)  # noqa: E731
    G = lambda i, b, f: Assertion(i, "G", b, f)  # noqa: E731
    return [
        A("A1", False, a1_record_present),
        A("A2", True, a2_epic),
        A("A3", True, a3_spec),
        A("A4", True, a4_phases),
        A("A5", True, a5_phase_membership),
        A("A6", True, a6_acyclic_dag),
        A("A7", True, a7_test_commands),
        A("A8", True, a8_slug),
        A("A9", True, a9_worktrees_and_verification),
        B("B1", False, b1_record_present),
        B("B2", True, b2_worktree),
        B("B3", True, b3_commit_touches_calc),
        B("B4", True, b4_diff_in_allowlist),
        B("B5", True, b5_status_done),
        B("B6", True, b6_test_pass),
        B("B7", True, b7_review_verdict),
        B("B8", True, b8_independent_pytest),
        B("B9", True, b9_one_future_wake),
        C("C1", True, c1_loud_raise),
        C("C2", True, c2_names_offending_key),
        C("C3", True, c3_no_status_created),
        C("C4", True, c4_no_wake_scheduled),
        C("C5", True, c5_resolver_present),
        C("C6", True, c6_no_syspath_in_ported),
        C("C7", True, c7_no_ip_literal_in_ported),
        C("C8", True, c8_resolver_hard_fails),
        C("C9", True, c9_no_typed_timestamp_placeholder),
        C("C10", False, c10_steward_cadence_report_is_read_only),
        C("C11", True, c11_question_pump_orders_by_class),
        C("C12", True, c12_directives_gate_the_merge_path),
        C("C13", True, c13_slash_argument_beats_injected_default),
        C("C14", True, c14_a_document_plan_parses),
        C("C15", True, c15_steward_sweep_disposes_of_every_open_question),
        D("D1", True, d1_continuation_reinvokes_builder),
        D("D2", True, d2_verbatim_baseline),
        D("D3", True, d3_no_second_wake),
        D("D4", True, d4_consume_first),
        F("F1", False, f1_record_present),
        F("F2", True, f2_bug_triaged_not_carved),
        F("F3", True, f3_epic_carved),
        F("F4", True, f4_already_triaged_untouched),
        G("G1", True, g1_untriaged_invisible_to_builder),
        # E1/E2 ride in the orchestrator scenario group (B).
        B("E1", True, e1_reviewer_prompt_path),
        B("E2", True, e2_reprompt_guard),
    ]


EXPECTED_IDS = (
    [f"A{i}" for i in range(1, 10)]
    + [f"B{i}" for i in range(1, 10)]
    + [f"C{i}" for i in range(1, 16)]
    + [f"D{i}" for i in range(1, 5)]
    + ["E1", "E2"]
    + [f"F{i}" for i in range(1, 5)]
    + ["G1"]
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _missing_config_hint() -> str:
    """Actionable guidance printed whenever the harness config is absent."""
    return (
        f"  {DEFAULT_CONFIG.name} is machine-local (it names YOUR specs/plans dirs and\n"
        f"  skill roots) and is gitignored, so a fresh clone does not have one.\n"
        f"  To create it:  copy {EXAMPLE_CONFIG}\n"
        f"           to:   {DEFAULT_CONFIG}\n"
        f"  then replace the placeholder paths. See harness/README.md ('Fresh clone:\n"
        f"  what runs') for what each key means and what runs without recordings."
    )


#: Every scenario group check.py knows. Derived from nothing else on purpose: the registry is
#: built after the argument is validated, and an empty selection must be an error before the
#: run starts rather than a green "0 PASS" after it.
SCENARIO_GROUPS = ("A", "B", "C", "D", "F", "G")


class UsageError(Exception):
    """A bad invocation (unknown --only group, unreadable or malformed config). `main` turns it into
    a stderr message and exit 2, so exit 1 keeps meaning a behavioral FAIL (gh#628)."""


def parse_only(only: Optional[str]) -> Optional[set]:
    """`--only C` or `--only C,F,G` -> a set of groups. Raises on an unknown letter.

    A silently-empty selection is the failure mode worth spending code on: a typo would
    otherwise print `0 PASS 0 FAIL` and exit 0, which reads exactly like a clean run.
    """
    if not only:
        return None
    groups = {g.strip().upper() for g in only.split(",") if g.strip()}
    unknown = sorted(groups - set(SCENARIO_GROUPS))
    if unknown or not groups:
        raise UsageError(
            f"FATAL: --only {only!r} names no known scenario group "
            f"{unknown or ''} - valid groups are {', '.join(SCENARIO_GROUPS)}"
        )
    return groups


def _config_type_errors(cfg: dict) -> list:
    """`key: expected <type>, got <type>` for every present required key of the wrong type.

    The one type check both `check.py <config>` and `--self-test` use (gh#989): a wrong
    type otherwise crashed an assertion (C3 `TypeError`, exit 1) - a config typo read as
    a behavioral regression.
    """
    errors = []
    for key, (kind, name) in REQUIRED_CONFIG_TYPES.items():
        if key not in cfg:
            continue
        value = cfg[key]
        ok = isinstance(value, kind)
        if ok and kind is list:
            ok = all(isinstance(item, str) for item in value)
        if not ok:
            got = type(value).__name__
            if isinstance(value, list):
                got = "list containing " + ", ".join(sorted({type(i).__name__ for i in value if not isinstance(i, str)}))
            errors.append(f"{key}: expected {name}, got {got}")
    return errors


def _check_config_shape(config_path: Path) -> None:
    """Refuse a config that is not a JSON object carrying every REQUIRED_CONFIG_KEYS key
    with the REQUIRED_CONFIG_TYPES type.

    Without this a missing key surfaced as an assertion crash (`KeyError` in C6, exit 1)
    and a `[]` or `"x"` config as a traceback - a config typo read as a behavioral
    regression, which is what exit 1 is reserved for (gh#943).
    """
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise UsageError(
            f"FATAL: config {config_path} is not readable JSON: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(cfg, dict):
        raise UsageError(
            f"FATAL: config {config_path} must be a JSON object, got {type(cfg).__name__} - "
            f"regenerate it from {EXAMPLE_CONFIG.name}"
        )
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
    if missing:
        raise UsageError(
            f"FATAL: config {config_path} is missing required key(s) {missing} - "
            f"see {EXAMPLE_CONFIG.name} for every key it must carry"
        )
    wrong = _config_type_errors(cfg)
    if wrong:
        raise UsageError(
            f"FATAL: config {config_path} has required key(s) of the wrong type: "
            f"{'; '.join(wrong)} - see {EXAMPLE_CONFIG.name}"
        )


def run_suite(config_path: Path, only: Optional[str]) -> int:
    groups = parse_only(only)
    _check_config_shape(config_path)
    try:
        ctx = Context(config_path)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise UsageError(
            f"FATAL: config {config_path} is not readable JSON: {type(exc).__name__}: {exc}"
        ) from exc
    registry = build_registry()
    if groups is not None:
        registry = [a for a in registry if a.group in groups]

    print(f"# agentflow harness check.py - config={config_path}")
    print(f"# builder_entry={ctx.builder_entry!r}"
          + (f"  --only {only}" if only else ""))
    for line in ctx.ignored_key_warnings():
        print(line)
    if ctx.import_error:
        print(f"# WARN parse_plan import error: {ctx.import_error}")

    behavioral_fail = 0
    structural_fail = 0
    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    #: (scenario, artifact) -> ids SKIPped for want of that recording, in run order.
    unrecorded: dict[tuple[str, str], list[str]] = {}

    for a in registry:
        try:
            res = a.fn(ctx)
        except Exception as exc:  # noqa: BLE001 - graceful, never crash the gate
            res = Result(FAIL, f"assertion crashed: {type(exc).__name__}: {exc}")
        counts[res.status] += 1
        tag = "" if a.behavioral else " [structural]"
        print(f"{res.status} {a.id}{tag}: {res.detail}")
        if res.status == SKIP and res.unrecorded:
            unrecorded.setdefault(res.unrecorded, []).append(a.id)
        if res.status == FAIL:
            if a.behavioral:
                behavioral_fail += 1
            else:
                structural_fail += 1

    print(
        f"# summary: {counts[PASS]} PASS  {counts[FAIL]} FAIL  {counts[SKIP]} SKIP  "
        f"(behavioral FAIL={behavioral_fail}, structural FAIL={structural_fail})"
    )
    if structural_fail and not behavioral_fail:
        print("# NOTE: structural-only failures - warned, do not fail the exit code")

    # gh#628. An unrecorded scenario holds the run non-zero - a fresh clone must not
    # read as green - but with its own code, so it never reads as a regression either.
    # It holds only when the scenario that would write the artifact was SELECTED: C4
    # reads a log that B/D write, and `--only C,F,G` asked for neither. Only the
    # SELECTED letters are held, so `--only B,C` names B and not D.
    held: set[str] = set()
    for (scenario, artifact), ids in unrecorded.items():
        letters = set(scenario.split("/"))
        selected = letters if groups is None else letters & groups
        in_scope = bool(selected)
        held |= selected
        scope = "" if in_scope else f" [scenario {scenario} outside --only {only}; does not hold the exit]"
        print(f"# unrecorded: scenario {scenario} - {artifact} ({', '.join(ids)}){scope}")
    if behavioral_fail:
        return 1
    if held:
        print(
            f"# EXIT {EXIT_UNRECORDED}: no behavioral FAIL, but scenario(s) "
            f"{', '.join(sorted(held))} are unrecorded on this machine - record them per "
            "harness/run-scenario.md, or narrow with --only (exit 1 means a real FAIL)"
        )
        return EXIT_UNRECORDED
    return 0


AUDITOR_SCENARIO = SCRIPT_DIR / "auditor-scenario" / "auditor_scenario.py"
AUDITOR_ROW_PREFIX = "##ROW "

#: The checks the auditor driver must emit, in emission order (gh#218 item 3). The gate
#: grades rows against THIS set, never against whatever arrived: a denominator taken from
#: the rows cannot disagree with them, so a driver regressed to 1 of its 15 checks printed
#: `1/1 checks PASS` and shipped. It lives here rather than in the driver because an
#: expectation the driver declares for itself is dropped by the same edit that drops the
#: check. Not in EXPECTED_IDS either - that count is asserted by four docs (E-0370).
#: Adding or removing a driver check means editing this tuple in the same change.
AUDITOR_EXPECTED_IDS = (
    "A-sat-ledger",
    "LEDGER-row-shape",
    "B-halfmet-gap",
    "C-write-scope",
    "D-no-approval",
    "REPORT-anchors",
    "F1-agents-own-rows",
    "F2-auditor-blocker-dm",
    "F2b-empty-pass-continues",
    "F3-blocker-ownership",
    "F4-cull-to-registry-log",
    "REGISTRY-valid",
    "E-second-pass-noop",
    "G1-no-developer-N",
    "G2-builder-entry",
)


def _auditor_id_drift(ids: list[str]) -> list[str]:
    """Why the emitted ids are not exactly AUDITOR_EXPECTED_IDS; empty when they are.

    Order is not graded - the set and the multiplicity are. A missing id is a check that
    did not run, an extra id is one nobody pinned, and a duplicate would let a repeated
    row stand in for an absent one in any count.
    """
    problems = []
    missing = [i for i in AUDITOR_EXPECTED_IDS if i not in ids]
    extra = [i for i in ids if i not in AUDITOR_EXPECTED_IDS]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if missing:
        problems.append(f"{len(missing)} expected check(s) never reported: {missing}")
    if extra:
        problems.append(f"{len(extra)} unpinned check(s) reported: {extra} "
                        "(add them to AUDITOR_EXPECTED_IDS)")
    if dupes:
        problems.append(f"check(s) reported more than once: {dupes}")
    return problems


def run_auditor_scenario() -> int:
    """Run the auditor ship gate in a throwaway tree. 0 iff every check PASSes.

    A limb of this file, deliberately NOT rows in `build_registry()`: EXPECTED_IDS is
    the count four read-only docs and `assert_doc_counts.py` assert, so scenario ids
    cannot live there (E-0370).

    Run as a SUBPROCESS, not imported. Loading the driver in this process made the
    gate validate the WRONG CHECKOUT: `check.py` imports `agentflow.plan_parser`
    before it ever reaches the driver, so `agentflow` is already bound in
    `sys.modules` and the driver's own `sys.path.insert` is inert — the registry
    assertions then graded the pip-installed tree instead of the branch under
    review, and a branch that broke `registry.py` shipped green. A fresh
    interpreter with the driver's own `skills/_shared` ahead of PYTHONPATH has no
    such prior binding; the driver additionally REFUSES to run if it still resolves
    a foreign tree, so this is proved rather than assumed.
    """
    if not AUDITOR_SCENARIO.is_file():
        print(f"FAIL auditor-gate: driver not found at {AUDITOR_SCENARIO}")
        return 1

    pkg_parent = SCRIPT_DIR.parent / "skills" / "_shared"   # harness/ sits beside skills/
    # The driver writes `registry status` through the CLI, and a transition into
    # `halted` sends a real ping - so it gets the same stripped environment as the
    # registry drill, not the operator's (gh#411).
    env = _isolated_child_env(pkg_parent)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(pkg_parent)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONDONTWRITEBYTECODE"] = "1"   # no __pycache__ churn in the tree under review

    # A temp tree, so running the gate never clobbers a developer's own
    # harness/auditor-scenario/_scratch/.
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [sys.executable, str(AUDITOR_SCENARIO), "--emit-rows", "--scratch", td],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    rows: list[tuple[str, str, str]] = []
    narrative: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith(AUDITOR_ROW_PREFIX):
            parts = line[len(AUDITOR_ROW_PREFIX):].split("\t", 2)
            if len(parts) == 3:
                status, aid, detail = parts
                rows.append((status, aid, detail.replace("\\t", "\t")
                                               .replace("\\n", "\n")
                                               .replace("\\\\", "\\")))
                continue
        narrative.append(line)

    if not rows:
        # A crash, a refusal to run, or a driver that emitted no rows. Surface
        # everything it said: a gate that cannot report is a FAIL, never a pass.
        for line in narrative + proc.stderr.splitlines():
            if line.strip():
                # Re-encode through this console's codec before printing. What we are
                # echoing is arbitrary subprocess stderr, and on a default Windows
                # console (cp1252) a single em dash in it turns this reporting loop
                # into an UnicodeEncodeError traceback - losing the very message that
                # explains why the gate refused to run. The failure path must be the
                # one thing that cannot fail.
                enc = sys.stdout.encoding or "utf-8"
                print(f"# {line}".encode(enc, "replace").decode(enc))
        print(f"FAIL auditor-gate: driver produced no assertion rows (exit {proc.returncode})")
        return 1

    for status, aid, detail in rows:
        print(f"{status} auditor.{aid}: {detail}")
    drift = _auditor_id_drift([aid for _, aid, _ in rows])
    for problem in drift:
        print(f"FAIL auditor-gate: {problem}")
    # Counted over the PINNED set: an unpinned row passing adds nothing to the numerator.
    npass = sum(1 for s, aid, _ in rows if s == PASS and aid in AUDITOR_EXPECTED_IDS)
    # Anything that is not PASS (a SKIP, a lowercase or empty status) is not a pass.
    nfail = sum(1 for s, _, _ in rows if s != PASS)
    if nfail or drift or proc.returncode != 0:
        for line in proc.stderr.splitlines():
            if line.strip():
                print(f"# {line}")
    print(f"# auditor ship gate: {npass}/{len(AUDITOR_EXPECTED_IDS)} expected checks PASS")
    # The driver's own exit code is part of the contract: a nonzero exit with every
    # row green means it failed somewhere the rows do not cover.
    return 1 if (nfail or drift or proc.returncode != 0) else 0


def _parsed_fixture_probe(name: str):
    """Probe: the tracked fixture an assertion parses, plus a parser to parse it."""

    def probe(ctx: Context):
        return (FIXTURES_DIR / name).is_file() and ctx.parse_plan is not None

    return probe


def _scenario_f_fixture_probe(ctx: Context):
    """Probe: the seeded issue list F1, F3 and G1 replay the intake over."""
    return _scenario_f_seed()[3]


def _scenario_f_carve_probe(ctx: Context):
    """Probe: BOTH inputs an F2/F4 PASS rests on, as a conjunction.

    F2 and F4 were registered against `_scenario_f_fixture_probe`, which reads the
    seeded ISSUE list. That is a real input of theirs, but it is not the one their
    no-spec-carved half reads, and it stays truthy while `fixtures/scenarioF-carved/`
    is absent - so the probe graded a different assertion and vouched for exactly the
    empty world gh#218 found them passing over. `min`, not `sum`, for the same reason
    C6 and C7 use it: the claim is conjunctive, and a populated issue list must not be
    allowed to vouch for an unread carve directory.

    F3 keeps the narrower probe deliberately: its PASS requires a spec citing the epic,
    so it cannot go green over an empty carve at all, and widening its probe would
    assert a coupling that is not load-bearing there.
    """
    return min(len(_scenario_f_seed()[3] or {}), len(_carved_specs()))


#: Assertions allowed to return PASS with no records on disk, each paired with a
#: probe returning the input that PASS actually rests on.
#:
#: Deny-by-default on purpose: an id absent from this table that goes green on an
#: empty world is the gh#139 defect, and a NEW assertion is caught without anyone
#: remembering to register it. The value is a probe rather than a prose reason
#: because C7 was exactly a reason that had quietly stopped being true - it grepped
#: `preport_ip_literals`, and the config CI runs ships that key as `[]`, so a
#: sentence saying "greps the skills tree for infra literals" would still have read
#: correct while the assertion examined nothing.
#:
#: C4 is deliberately ABSENT: it SKIPs on the records-absent world, so it never
#: reaches this check, and a revert of its fix lands it back in PASS with no entry -
#: which is the point.
#:
#: C7 was absent on the same premise and that premise was WRONG (gh#220). "C7 SKIPs
#: on this world" is not a fact about the world; it is a fact about whether
#: `preport_ip_literals` is empty, which this gate never reads. The tracked example
#: config ships the key empty, so CI only ever sees the SKIP - but C7's own SKIP
#: message tells the operator to "set the key locally to make C7 assert", and the
#: moment they do, C7 returns an HONEST PASS having read hundreds of files, is
#: unregistered, and is reported as a false green. The self-test then exited 1 on
#: every configured developer machine while CI stayed green, which is the one failure
#: mode this whole check exists to make impossible. Registering C7 is strictly
#: stronger than leaving it out: absence guarded nothing on the only machines where
#: C7 runs at all.
#:
#: C6 and C9 are PRESENT but their probes can no longer be the thing that catches
#: them: since gh#139 both gate on the same search space their probe measures, so a
#: PASS implies a truthy probe by construction. They stay listed anyway, because the
#: entry is what makes the redundancy legible - deleting it would put them back in
#: deny-by-default, where a future revert of the space guard reads as an unregistered
#: assertion rather than as the regression it is.
#:
#: What this table cannot see, stated so nobody reads more into a green than is
#: there: an assertion that examines a present, non-empty input through a lossy key
#: (B4's basename comparison) has a truthy probe and passes here; so does one that
#: reads the wrong field of a present record. Only the empty-input world is covered.
ABSENCE_PASS_EVIDENCE = {
    "C1": _parsed_fixture_probe("plan-bad-task-type.md"),
    "C2": _parsed_fixture_probe("plan-bad-task-type.md"),
    "C3": _parsed_fixture_probe("plan-bad-task-type.md"),
    "C5": lambda ctx: _import_parser_module(ctx) is not None,
    # min, not sum: C6's claim is conjunctive over two roots, and a sum lets a
    # populated orchestrator tree vouch for an empty solution/. The probe must
    # mirror the predicate it certifies or it grades a different assertion.
    "C6": lambda ctx: min(_grep_space(ctx.orchestrator_dir()), _grep_space(ctx.solution_dir())),
    # min, for the same reason as C6, against a conjunction of the two gh#139
    # repairs: C7 passes only when it had needles to look for AND files to look in,
    # so the probe must go to 0 if EITHER is reverted. `len(needles)` alone would
    # vouch for an empty search space; `_grep_space` alone would vouch for an empty
    # needle list - each is exactly one of the two zeroes C7 used to confuse.
    "C7": lambda ctx: min(
        len(ctx.config.get("preport_ip_literals", [])), _grep_space(ctx.skills_root())
    ),
    "C8": _parsed_fixture_probe("plan-bad-task-type.md"),
    "C9": lambda ctx: _grep_space(ctx.skills_root()),
    "C10": lambda ctx: _steward_cadence_block(ctx)[0],
    "C11": lambda ctx: _registry_cli_root(ctx)[0],
    "C12": lambda ctx: _registry_cli_root(ctx)[0],
    "C13": lambda ctx: _shared_package_root(ctx, "config.py")[0],
    "C14": _parsed_fixture_probe("plan-missing-epic.md"),
    "C15": lambda ctx: _registry_cli_root(ctx)[0],
    "E1": lambda ctx: (ctx.orchestrator_dir().parent / "_vendored" / "review-prompts").is_dir(),
    "F1": _scenario_f_fixture_probe,
    # min over the seeded issue list AND the carved-spec dir: F2/F4 assert an
    # ABSENCE of a carved spec, so their PASS rests on a carve directory that was
    # actually read. See `_scenario_f_carve_probe` for why F3 stays on the narrow one.
    "F2": _scenario_f_carve_probe,
    "F3": _scenario_f_fixture_probe,
    "F4": _scenario_f_carve_probe,
    "G1": _scenario_f_fixture_probe,
}


@contextlib.contextmanager
def _records_absent_world():
    """Rebind RECORDS_DIR/WAKE_LOG at paths that do not exist, for the duration.

    gh#302. `--self-test` steps 4 and 4b are both statements about the world with no
    records - step 4's own line says "with no records" - but they ran against whatever
    the machine had on disk, and `records/` and `stubs/schedulewakeup_log.md` are
    gitignored, so that world differed by checkout. Step 4b could not survive the
    difference: with this repo's recordings present, 42 of 43 rows PASS and 24 of them
    (A1-A9, B1-B9, C4, D1-D4, E2) are deliberately unregistered in
    ABSENCE_PASS_EVIDENCE, because a records-absent PASS is exactly what they must not
    return - so grading the ambient world reports 24 false greens (measured). It was
    guarded with a SKIP instead, which meant the one gate against a vacuous PASS did
    not run on any machine that had ever recorded a scenario. Synthesizing the world
    keeps the guard's reasoning and drops its blind spot: 4b now grades on every
    machine, whatever it has recorded. (`ctx is None` still grades nothing, but step
    2 has already set ok=False by then, so that path cannot hide a false green.)

    Every read of either path inside those two steps is a module-global lookup in a
    function body (`Context.wake_rows`, `_load_scenario_a_plan`, `_load_scenario_b`,
    C3, C4's message, D4), so rebinding here redirects all of them at once. The
    temporary directory is created but the two paths under it never are.
    """
    global RECORDS_DIR, WAKE_LOG
    saved = (RECORDS_DIR, WAKE_LOG)
    with tempfile.TemporaryDirectory() as td:
        RECORDS_DIR = Path(td) / "no-records"
        WAKE_LOG = Path(td) / "no-stubs" / "schedulewakeup_log.md"
        try:
            yield
        finally:
            RECORDS_DIR, WAKE_LOG = saved


def _plan_parser_file(ctx: Context) -> Optional[str]:
    """Resolved `__file__` of the plan_parser module parse_plan came from, or None."""
    mod = _import_parser_module(ctx)
    f = getattr(mod, "__file__", None) if mod is not None else None
    return str(Path(f).resolve()) if f else None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _tree_lines(ctx: Context) -> list[str]:
    """Which tree(s) this run exercises (gh#628, the gh#24 hazard).

    parse_plan and issue_intake import whatever `agentflow` is INSTALLED, while the
    registry CLI (C11/C12/C15) runs from this clone's own `skills/_shared`. With a
    second clone's editable install on the path one run mixes two trees; saying so
    is the whole fix. WARN, not FAIL: the self-test grades the harness's wiring, and
    a mixed tree is an environment fact the operator may intend (a sibling clone
    checked against this one), so it must be loud but not a wiring verdict.
    """
    parser_file = _plan_parser_file(ctx)
    root, why = _registry_cli_root(ctx)
    lines = [
        f"# tree: plan_parser imported from {parser_file or '(not importable)'}",
        f"# tree: registry CLI root {root if root is not None else '(none: ' + why + ')'}",
    ]
    clone_shared = SCRIPT_DIR.parent / "skills" / "_shared"
    if (
        parser_file
        and root is not None
        and root.resolve() == clone_shared.resolve()
        and not _is_under(Path(parser_file), clone_shared)
    ):
        lines.append(
            f"WARN self-test.tree: agentflow.plan_parser imported from {parser_file}, "
            f"which is NOT under this clone's registry CLI root {root} - this run mixes "
            "two trees; run `pip install -e skills/_shared` from this clone's root"
        )
    return lines


#: Every assertion whose only evidence is a live recording (gh#628). The self-test
#: requires each to SKIP through `_unrecorded` when records are absent, and no other
#: row to claim that marker, since it decides exit 3.
RECORD_BACKED_IDS = frozenset(
    [f"A{i}" for i in range(1, 10)]
    + [f"B{i}" for i in range(1, 10)]
    + ["E2", "C4", "D1", "D2", "D3", "D4"]
)


def run_self_test(config_path: Optional[Path] = None) -> int:
    """Validate wiring with no records required. Exits 0 iff the harness is sound.

    `config_path` exists for the tests, which cross the verdict over a synthesized
    configured world; the command never passes it (gh#340, see below).
    """
    print("# SELF-TEST: agentflow harness check.py wiring")
    ok = True

    # Always the tracked example config, never the machine-local one (gh#340). The
    # local config is gitignored, so reading it when present made the verdict a
    # property of the machine: a non-empty `preport_ip_literals` alone moved step
    # 4b's graded count, the gh#220 shape that the tests had closed and the command
    # had not. Pinning, rather than only naming the config that decided, is what
    # makes two runs of one tree agree; the trade is that a malformed local config
    # no longer fails here. `check.py <config>` rejects it instead: `run_suite` refuses
    # a non-object config or a missing required key with exit 2 (gh#943).
    if config_path is None:
        config_path = EXAMPLE_CONFIG
        if DEFAULT_CONFIG.exists():
            print(
                f"# NOTE: {DEFAULT_CONFIG.name} present - the self-test does NOT read it and "
                f"grades against {EXAMPLE_CONFIG.name}, so the local config does not move the "
                "verdict (run `check.py <config>` to exercise yours)"
            )
    print(f"# config: {config_path}")

    # 1. config loads and carries the required keys
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
        if missing:
            ok = False
            print(f"FAIL self-test.config: {config_path} missing keys {missing}")
        elif wrong := _config_type_errors(cfg):
            ok = False
            print(f"FAIL self-test.config: {config_path} wrong-typed keys: {'; '.join(wrong)}")
        else:
            print(f"PASS self-test.config: {config_path.name} loads, all required keys present and correctly typed")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"FAIL self-test.config: {type(exc).__name__}: {exc}")
        if not config_path.exists():
            print(_missing_config_hint())

    # 2. Context builds and parse_plan imports via the config-named mechanism
    ctx = None
    try:
        ctx = Context(config_path)
        if ctx.parse_plan is None:
            ok = False
            print(f"FAIL self-test.import: parse_plan not importable ({ctx.import_error})")
            if "agentflow" in (ctx.import_error or ""):
                print(
                    "  hint: the resolver imports the installed agentflow package - "
                    "run `pip install -e skills/_shared` from the repo root first"
                )
        else:
            print("PASS self-test.import: parse_plan resolved via agentflow.plan_parser")
        # WARN, not FAIL: a stale key selects nothing any more, so it is not a
        # wiring defect - but it must not read as if it still chooses the import.
        for line in ctx.ignored_key_warnings():
            print(line)
        for line in _tree_lines(ctx):
            print(line)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"FAIL self-test.import: Context build failed: {type(exc).__name__}: {exc}")

    # 3. registry is complete (every expected assertion id present exactly once)
    registry = build_registry()
    ids = [a.id for a in registry]
    dupes = {i for i in ids if ids.count(i) > 1}
    absent = [i for i in EXPECTED_IDS if i not in ids]
    extra = [i for i in ids if i not in EXPECTED_IDS]
    if absent or extra or dupes:
        ok = False
        print(f"FAIL self-test.registry: absent={absent} extra={extra} dupes={sorted(dupes)}")
    else:
        print(f"PASS self-test.registry: {len(ids)} assertions wired ({EXPECTED_IDS[0]}..{EXPECTED_IDS[-1]})")

    # 4 through 4c run inside a SYNTHETIC records-absent world (gh#302), never the
    # ambient one, so their verdicts do not depend on which gitignored recordings the
    # machine happens to hold; with the config pinned above (gh#340) nothing
    # machine-local decides them. See `_records_absent_world` for why 4b cannot grade
    # the ambient world at all. Step 4d reads the recordings, for crashes only.
    crashed = []
    results: dict[str, Result] = {}
    ambient_records = RECORDS_DIR.exists() or WAKE_LOG.exists()
    if ambient_records:
        print(
            f"# NOTE: {RECORDS_DIR.name}/ or {WAKE_LOG.name} present - steps 4-4c "
            "synthesize the records-absent world and do NOT read them; step 4d runs "
            "them for crashes only (run `check.py <config>` to grade the recordings)"
        )
    with _records_absent_world():
        # 4. every assertion runs against ABSENT records without crashing
        if ctx is not None:
            for a in registry:
                try:
                    results[a.id] = a.fn(ctx)
                except Exception as exc:  # noqa: BLE001
                    crashed.append(f"{a.id}:{type(exc).__name__}")
        if crashed:
            ok = False
            print(f"FAIL self-test.no-crash: assertions raised on missing records: {crashed}")
        else:
            print(f"PASS self-test.no-crash: all {len(registry)} assertions degrade gracefully with no records")

        # 4b. no assertion goes green on an input it never examined (gh#139).
        #     Step 4 proves nothing CRASHES when records are absent. It cannot see the
        #     opposite failure - a PASS that is unconditional because the thing being
        #     examined was not there - which is how C3, C4 and C7 shipped green for
        #     every clone but one. PASS is a claim to have observed something, so with
        #     records absent an assertion must FAIL, SKIP, or point at a present,
        #     non-empty input. Results are reused from step 4 rather than re-running the
        #     registry: several rows spawn subprocesses.
        if ctx is None:
            pass  # already failed at step 2; there is nothing to grade
        else:
            false_green = []
            graded = 0
            for a in registry:
                res = results.get(a.id)
                if res is None or res.status != PASS:
                    continue
                graded += 1
                probe = ABSENCE_PASS_EVIDENCE.get(a.id)
                if probe is None:
                    false_green.append(a.id)
                    continue
                try:
                    if not probe(ctx):
                        false_green.append(a.id)
                except Exception as exc:  # noqa: BLE001 - a probe that cannot run is not evidence
                    false_green.append(f"{a.id}(probe raised {type(exc).__name__})")
            if false_green:
                ok = False
                print(
                    f"FAIL self-test.no-false-green: {false_green} returned PASS with no records "
                    "and no non-empty input to point at - either the assertion examines nothing "
                    "(the gh#139 defect), or it is honest and belongs in ABSENCE_PASS_EVIDENCE "
                    "with a probe naming what it reads"
                )
            else:
                print(
                    f"PASS self-test.no-false-green: each of the {graded} PASS(es) with no "
                    "records names a present, non-empty input"
                )

        # 4c. a missing recording is uniformly an unrecorded SKIP (gh#628): every
        #     record-backed row goes through `_unrecorded`, and nothing else claims
        #     the marker that turns a run's exit into 3.
        if ctx is not None:
            wired = {a.id for a in registry}
            not_skipped = sorted(
                i for i in RECORD_BACKED_IDS & wired
                if i in results and not (results[i].status == SKIP and results[i].unrecorded)
            )
            stray = sorted(
                i for i, r in results.items() if r.unrecorded and i not in RECORD_BACKED_IDS
            )
            # A rename must not silently shrink the graded set: every record-backed id
            # has to be wired, not just the ones that happen to be.
            unwired = sorted(RECORD_BACKED_IDS - wired)
            if not_skipped or stray or unwired:
                ok = False
                print(
                    f"FAIL self-test.unrecorded: with no records, {not_skipped} did not SKIP "
                    f"as unrecorded, {stray} claimed unrecorded without being record-backed, "
                    f"and {unwired} are record-backed but not wired"
                )
            else:
                print(
                    f"PASS self-test.unrecorded: all {len(RECORD_BACKED_IDS & wired)} "
                    "record-backed assertions SKIP as unrecorded with no records, none FAIL"
                )

    # 4d. every assertion runs against the AMBIENT recordings without crashing (gh#341).
    #     gh#302 moved step 4 into the synthesized world, which cost the crash coverage
    #     it used to get on a recorder machine: a malformed or unexpected recording no
    #     longer raised anywhere --self-test looked. This pass returns it without
    #     re-fusing the two concerns - verdicts are discarded, because grading this
    #     world is exactly what 4b cannot do, and only an exception is a finding.
    #     Nothing to read means nothing to prove, so a fresh clone (and CI) SKIPs.
    #     Every row reruns, not just the ones that read recordings: those reads go
    #     through shared helpers (`Context.wake_rows`, `_load_scenario_b`, ...), so
    #     no reliable reader list can be derived, and RECORD_BACKED_IDS is not one -
    #     C3 reads records/ and is not in it. A hand-kept list would rot silently
    #     and a missing row is lost coverage; one extra registry pass, on recorder
    #     machines only, is the cheaper side of that trade.
    if ctx is not None and ambient_records:
        ambient_crashed = []
        for a in registry:
            try:
                a.fn(ctx)
            except Exception as exc:  # noqa: BLE001
                ambient_crashed.append(f"{a.id}:{type(exc).__name__}")
        if ambient_crashed:
            ok = False
            print(
                f"FAIL self-test.ambient-no-crash: assertions raised on the recordings on "
                f"disk: {ambient_crashed}"
            )
        else:
            print(
                f"PASS self-test.ambient-no-crash: all {len(registry)} assertions ran against "
                "the recordings on disk without raising (verdicts not graded)"
            )
    elif ctx is not None:
        print("SKIP self-test.ambient-no-crash: no recordings on disk, step 4 already covers that world")

    # 5. record shape guards reject malformed input with a shaped message
    #    (nothing else imports this file, so this is the guards' only coverage)
    shape_cases = [
        ('[{"number": 1, "labels": 7}]', "must be a list"),
        ('[{"number": 1, "labels": "triaged"}]', "must be a list"),
        ('[{"number": 1, "labels": [7]}]', "malformed labels"),
        ('[{"number": 1, "labels": [{"name": ["x"]}]}]', "unhashable labels"),
        ('{"number": 1}', "must be a JSON list"),
        ('[{"title": "no number"}]', "integer 'number'"),
    ]
    shape_bad = []
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "issues.json"
        for payload, needle in shape_cases:
            probe.write_text(payload, encoding="utf-8")
            issues, err = _load_issue_list(probe, "probe")
            if issues is not None or not err or needle not in err:
                shape_bad.append(f"{payload[:32]!r}->{err!r}")
        probe.write_bytes(b'[{"number": 1, "labels": ["' + bytes([0xff, 0xfe]) + b' bad utf8"]}]')
        issues, err = _load_issue_list(probe, "probe")
        if issues is not None or not err:
            shape_bad.append("undecodable JSON accepted")
    if shape_bad:
        ok = False
        print(f"FAIL self-test.shapes: malformed records not rejected: {shape_bad}")
    else:
        print(f"PASS self-test.shapes: {len(shape_cases) + 1} malformed record shapes rejected with a shaped message")

    # 6. the auditor ship gate: self-contained fixtures, no records — the one
    #    scenario driver check.py owns end to end (E-0370). It runs in its own
    #    interpreter (see run_auditor_scenario) so it imports the tree it ships
    #    beside rather than whatever this process already bound. Still guarded on
    #    parse_plan: if agentflow is not importable at all, step 2 has already
    #    failed the run and the gate's output would only repeat that.
    if ctx is not None and ctx.parse_plan is not None:
        if run_auditor_scenario() != 0:
            ok = False
            print("FAIL self-test.auditor-gate: auditor ship gate red (see auditor.* lines above)")
        else:
            print("PASS self-test.auditor-gate: auditor ship gate green")
    else:
        print("SKIP self-test.auditor-gate: agentflow not importable (see self-test.import)")

    print(f"# SELF-TEST {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="agentflow acceptance harness checker")
    parser.add_argument("config", nargs="?", help="path to harness.config.json")
    parser.add_argument("--self-test", action="store_true", help="validate wiring, no records needed")
    parser.add_argument("--auditor-scenario", action="store_true",
                        help="run the auditor ship-gate driver (self-contained fixtures, no records)")
    parser.add_argument(
        "--only",
        metavar="GROUPS",
        help="run only these scenario groups - one letter or a comma-separated list "
             "(A,B,C,D,F,G), e.g. --only C,F,G",
    )
    args = parser.parse_args(argv)

    if args.auditor_scenario:
        return run_auditor_scenario()

    if args.self_test:
        # Refused, not ignored: the self-test grades the tracked example config only
        # (gh#340), and a config named here would read as the one that was checked.
        if args.config:
            print(
                f"usage error: --self-test takes no config (got {args.config}); it always "
                f"grades {EXAMPLE_CONFIG.name}. Run `check.py {args.config}` to check that config.",
                file=sys.stderr,
            )
            return 2
        return run_self_test()

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG
    if not config_path.exists():
        print(f"FATAL: config not found: {config_path}", file=sys.stderr)
        print(_missing_config_hint(), file=sys.stderr)
        return 2
    try:
        return run_suite(config_path, args.only)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
