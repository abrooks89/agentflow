#!/usr/bin/env python3
"""Auditor SHIP GATE (A7) — post-build intent-feedback-loop acceptance scenario.

Self-contained, deterministic, re-runnable. Builds throwaway fixtures under a
caller-supplied scratch dir (a temp ``data_root`` with toy "done" epics + a toy
UC registry) — NEVER touches the live vault or real project data — then drives
the auditor's **deterministic** machinery and judges the observable
feedback-loop outcomes by real filesystem artifacts.

The live audit-agent subagent (Playwright / probes) is non-deterministic, so this
scenario **injects CANNED VERDICTS** standing in for subagent output:

    epic-satisfied  -> one UC, verdict = satisfied         (proven; no gap)
    epic-halfmet    -> one UC, two shortfalls (partial + missed)  (one gap epic)
    epic-noverify   -> no verification.strategy -> BLOCKER (not a verdict)

Canned verdicts are legitimate — they stand in for a non-deterministic agent.
Reimplementing a surface that SHIPS is not, so fleet coordination is driven
through the shipped ``agentflow.registry`` CLI rather than reproduced here.
Given those canned outputs it exercises, against toy fixtures under a temp
``data_root``, the auditor's deterministic steps reproduced faithfully from
``skills/auditor/SKILL.md`` + the reference formats (ledger-format.md,
report-format.md, templates/gap-epic.md):

    discovery (real ``agentflow.epic_survey``) -> ledger dedupe filter ->
    gap synthesis (cluster + drafted spec w/ provenance) -> ledger/report writes ->
    registry writes through the shipped ``agentflow.registry`` CLI -> steward cull.

Assertions (each a PASS/FAIL line; exit 0 iff every one passes) map to the spec's
"Ship gate (acceptance)" list (a)-(g):

  (a) satisfied epic -> ledger row (cells named by ledger-format.md), NO gap epic.
  (b) half-met epic  -> exactly ONE drafted gap spec in design/specs/ with
      current_state: drafted and the provenance keys templates/gap-epic.md names,
      REUSING the original UC-ID.
  (c) nothing written outside design/specs/ + status/ (no product-code/merge/deploy
      writes) — observed across BOTH the fixture data_root and the suite root, so a
      write that escapes the fixture is visible; also polices every registry write,
      since registry.md and registry-log.md live under status/.
  (d) NO 'approved-for-autonomous' in the artifacts the pass WROTE. The shipped
      skills/auditor/SKILL.md is not scanned — canned verdicts never execute it.
  (e) a SECOND pass audits nothing (ledger dedupe filter works) and raises no new
      blocker: the blocked epic is re-judged, and the Step 6 dedupe rule (parsed out of
      skills/auditor/SKILL.md, applied over a lock-free `registry read`) skips its raise.
  (f) registry: every loop in ``agentflow.registry.AGENTS`` writes its OWN
      ## Agents row and nothing else; an auditor blocker (no verification.strategy),
      carrying the keys of the blocker return parsed out of audit-agent.md,
      -> a ## Blockers row OWNED BY dev-manager, and NOT a human-halt; a seeded
      jr-dev blocker -> a row owned by lead-dev whose raiser is REFUSED
      (blocker_owner_resolves) when it tries to resolve it; a steward cull rolls the
      resolved row to registry-log.md (dev-manager is refused: cull_steward); the
      shipped validator accepts everything written.
      (f2b) the EMPTY pass — the case the scenario's own fixtures never produce —
      schedules a wake and does not halt, proved against THIS DRIVER'S reproduction of
      the continuation rule, not against skills/auditor/SKILL.md's prose.
  (g) rename: zero surviving developer-1|developer-2|developer-3 skill-name refs;
      builder_entry resolves to /lead-dev in the TRACKED example config.

Usage:
    python harness/auditor-scenario/auditor_scenario.py
    python harness/check.py --auditor-scenario      # same rows, run by the gate
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path


def _rm_readonly(func, path, _exc):
    """rmtree onerror: clear the read-only bit git may set (Windows)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


SCRIPT_DIR = Path(__file__).resolve().parent
SCRATCH = SCRIPT_DIR / "_scratch"

# Machine-readable row marker (`--emit-rows`). harness/check.py runs this driver as a
# SUBPROCESS — the only way to guarantee it imports the tree it ships beside — and
# parses these lines back out of stdout. Narrative `#` output stays human-facing.
ROW_PREFIX = "##ROW "
SUITE_ROOT = SCRIPT_DIR.parents[1]              # the agentflow repo root (harness/auditor-scenario/ -> repo)
HARNESS_DIR = SUITE_ROOT / "harness"

# Make the agentflow package that ships BESIDE this driver importable regardless of cwd.
_PKG_PARENT = SUITE_ROOT / "skills" / "_shared"
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from agentflow import use_case_id                     # noqa: E402
from agentflow import registry as registry_cli        # noqa: E402 — shipped CLI, driven in-process
from agentflow.config import Audit                    # noqa: E402 — real defaults (window=7)
from agentflow.epic_survey import survey_epics        # noqa: E402
from agentflow.registry import AGENTS                 # noqa: E402 — the ONE definition of the fleet
from agentflow.tracked_paths import tracked_files     # noqa: E402


def _assert_own_tree() -> None:
    """PROVE the import above resolved to the tree this driver ships beside.

    The `sys.path.insert` is inert if anything already bound `agentflow` in
    `sys.modules` — which is exactly what an entry point does when it imports the
    pip-installed package before loading this driver. Every registry assertion
    below would then grade a FOREIGN checkout and report it green: a branch that
    breaks `registry.py` would ship through the gate untouched.

    So: refuse to run, loudly, naming both paths. A SKIP would restore the same
    silent false-green this gate exists to remove — the gate must be red, not
    absent, when it cannot see the code under review.
    """
    imported = Path(registry_cli.__file__).resolve()
    shipped = (_PKG_PARENT / "agentflow" / "registry.py").resolve()
    if imported == shipped:
        return
    sys.stderr.write(
        # ASCII ONLY, deliberately. This is the one message that must survive being
        # printed on a default Windows console (cp1252), where an em dash raises
        # UnicodeEncodeError - and an error path that crashes while reporting the
        # error leaves the operator with a traceback instead of the two paths they
        # need. Prose elsewhere in this file may use typographic characters; this
        # block may not.
        "FATAL auditor-gate: REFUSING TO RUN - this driver imported a different "
        "`agentflow` than the one it ships beside, so every assertion below would "
        "grade a foreign checkout.\n"
        f"  imported: {imported}\n"
        f"  expected: {shipped}\n"
        "  cause: `agentflow` was already bound in sys.modules before this module "
        "loaded, making its sys.path.insert inert. Run this driver in its own "
        "process with SUITE_ROOT/skills/_shared ahead of PYTHONPATH "
        "(harness/check.py --auditor-scenario does exactly that).\n"
    )
    raise SystemExit(2)


_assert_own_tree()

def _silence_survey_noise() -> None:
    """Survey logs a WARNING per non-plan solution doc — expected here, so quiet it.

    CALLED FROM `main()`, NOT AT IMPORT, and that placement is load-bearing. This is a
    script, but tests import it by path for its helpers, and a module-scope `setLevel`
    reconfigures logging for the WHOLE process that imports it. `test_epic_survey` asserts
    on WARNING records from this exact logger, so importing this file used to turn 53
    unrelated tests red -- while every test in the importing module stayed green, and only
    on collection order.

    Two importers already exist (`test_never_halt_on_empty.py` and the gh#114 discovery
    tests), so a save/restore at each call site would have to be repeated by importer three
    and would look like closure without being it. Owning the side effect where it is caused
    covers every importer, including the ones not written yet.
    """
    logging.getLogger("agentflow.epic_survey").setLevel(logging.ERROR)

PROJECT_NAME = "audit-demo"
DATE = "2026-07-18"
PASS_INDEX = "1"
CUTOFF = "2026-01-01T00:00:00+00:00"     # well before the freshly-written fixtures
NOW_ISO = f"{DATE}T12:00:00+00:00"        # frozen so ledger/report output is deterministic
_ETA = f"{DATE}T18:00:00+00:00"           # every `status --state building` must declare one

DOMAIN = "billing/invoices"

# The SHIPPED format documents this driver reads its shapes out of, rather than
# retyping them as literals it would then assert against itself.
LEDGER_FORMAT_MD = SUITE_ROOT / "skills" / "auditor" / "references" / "ledger-format.md"
REPORT_FORMAT_MD = SUITE_ROOT / "skills" / "auditor" / "references" / "report-format.md"
GAP_TEMPLATE_MD = SUITE_ROOT / "templates" / "gap-epic.md"
AUDIT_AGENT_MD = SUITE_ROOT / "skills" / "auditor" / "references" / "audit-agent.md"
AUDITOR_SKILL_MD = SUITE_ROOT / "skills" / "auditor" / "SKILL.md"

# --------------------------------------------------------------------------- #
# Result plumbing (PASS/FAIL <id>: detail, exit 0 iff all pass) — mirrors uc_scenario
# --------------------------------------------------------------------------- #
_RESULTS: list[tuple[str, str, str]] = []


def check(aid: str, cond: bool, ok_detail: str, bad_detail: str) -> bool:
    _RESULTS.append(("PASS" if cond else "FAIL", aid, ok_detail if cond else bad_detail))
    return cond


# --------------------------------------------------------------------------- #
# Path helpers (config tokens, never literals)
# --------------------------------------------------------------------------- #
def _project_dir(data_root: Path) -> Path:
    return data_root / "projects" / PROJECT_NAME


def _status_dir(data_root: Path) -> Path:
    return _project_dir(data_root) / "status"


def _specs_dir(data_root: Path) -> Path:
    # Renamed from `_refine_dir`: the three-pillar migration moved this bucket to
    # design/specs/ and the old name outlived it. A rename, not a path change.
    return _project_dir(data_root) / "design" / "specs"


def _ledger_path(data_root: Path) -> Path:
    return _status_dir(data_root) / "audit-ledger.md"


def _report_path(data_root: Path) -> Path:
    return _status_dir(data_root) / "audits" / f"{DATE}-{PASS_INDEX}.md"


def _registry_path(data_root: Path) -> Path:
    return _status_dir(data_root) / "registry.md"


def _registry_log_path(data_root: Path) -> Path:
    return _status_dir(data_root) / "registry-log.md"


def _state_path(data_root: Path) -> Path:
    return _status_dir(data_root) / "auditor-state.md"


# --------------------------------------------------------------------------- #
# SHIPPED-FORMAT READERS — the shapes below are PARSED out of the documents that
# define them. A literal retyped here is a shape the gate can only agree with
# itself about; an anchor that stops matching raises loudly rather than degrading
# to a vacuous pass.
# --------------------------------------------------------------------------- #
def _read_format(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"format document missing: {path} — the gate reads its shapes from it")
    return path.read_text(encoding="utf-8")


def _ledger_header_rows() -> tuple[str, str]:
    """The ledger table's header + separator lines, verbatim from ledger-format.md."""
    lines = _read_format(LEDGER_FORMAT_MD).split("\n")
    for i, line in enumerate(lines):
        if line.startswith("| epic |"):
            sep = lines[i + 1] if i + 1 < len(lines) else ""
            if not set(sep.strip()) <= set("|- ") or not sep.strip():
                raise RuntimeError(
                    f"{LEDGER_FORMAT_MD.name}: header at line {i + 1} is not followed by a "
                    f"separator row (got {sep!r})"
                )
            return line, sep
    raise RuntimeError(f"{LEDGER_FORMAT_MD.name}: no `| epic |` ledger header row found")


def _ledger_columns() -> list[str]:
    return _table_cells(_ledger_header_rows()[0])


#: The anchor in SKILL.md Step 6 that introduces the blocker-dedupe match conditions. Matched on
#: any whitespace so a rewrap of the prose does not move it.
_DEDUPE_ANCHOR_RE = re.compile(r"a row \*\*matches\*\* when all of\s+these hold:[ \t]*\n")


def skill_blocker_match_rule() -> dict[str, str]:
    """The Step 6 dedupe rule, parsed out of skills/auditor/SKILL.md: {column: condition}.

    The rule is the SKILL's. This driver cannot run the skill, so it applies the rule the skill
    states, and reads that rule here so a column added to or dropped from the prose turns the
    gate red instead of leaving a matcher that agrees only with itself. Each bullet under the
    anchor reads "- `<column>` <condition>;"; the list ends at the first non-bullet line.
    """
    text = _read_format(AUDITOR_SKILL_MD)
    anchor = _DEDUPE_ANCHOR_RE.search(text)
    if anchor is None:
        raise RuntimeError(f"{AUDITOR_SKILL_MD.name}: Step 6 dedupe anchor "
                           f"{_DEDUPE_ANCHOR_RE.pattern!r} not found")
    rule: dict[str, str] = {}
    for line in text[anchor.end():].lstrip("\n").split("\n"):
        m = re.match(r"^- `([a-z_]+)` (.+?)[;.]?$", line)
        if not m:
            break
        rule[m.group(1)] = m.group(2)
    if not rule:
        raise RuntimeError(f"{AUDITOR_SKILL_MD.name}: Step 6 dedupe anchor is followed by no conditions")
    return rule


#: The literal value `blocker_matches` tests per column, as the skill's condition must spell it.
#: `owner` and `work` are absent on purpose (SKILL.md Step 6 says why): a reassigned row, or an
#: epic that gained an `E-` id, is still the same open blocker.
_MATCH_STATE = "open"
_MATCH_RAISER = "auditor"
_MATCHER_LITERALS = {"state": f"`{_MATCH_STATE}`", "raised_by": f"`{_MATCH_RAISER}`",
                     "description": "`<slug>:`"}
_MATCHER_COLUMNS = frozenset(_MATCHER_LITERALS)


def blocker_matches(row, slug: str) -> bool:
    """True when `row` (a shipped `registry.Blocker`) is the open auditor blocker for `slug`.

    One predicate per column the skill names; `_check_match_rule_columns` refuses a drift in
    either the column set or the literal value each condition carries.
    """
    return (row.state == _MATCH_STATE
            and row.raised_by == _MATCH_RAISER
            and row.description.startswith(f"{slug}:"))


def _check_match_rule_columns() -> None:
    rule = skill_blocker_match_rule()
    if set(rule) != _MATCHER_COLUMNS:
        raise RuntimeError(
            f"{AUDITOR_SKILL_MD.name} Step 6 matches on columns {sorted(rule)} but this "
            f"driver's matcher tests {sorted(_MATCHER_COLUMNS)}; update blocker_matches to the skill"
        )
    drifted = {c: rule[c] for c, lit in _MATCHER_LITERALS.items() if lit not in rule[c]}
    if drifted:
        raise RuntimeError(
            f"{AUDITOR_SKILL_MD.name} Step 6 conditions {drifted} no longer carry the values this "
            f"driver's matcher tests {_MATCHER_LITERALS}; update blocker_matches to the skill"
        )


def _gap_provenance_keys() -> list[str]:
    """The provenance keys, in order, from the frontmatter block of templates/gap-epic.md."""
    keys: list[str] = []
    seen_block = False
    for line in _read_format(GAP_TEMPLATE_MD).split("\n"):
        if not seen_block:
            if line.rstrip() == "provenance:":
                seen_block = True
            continue
        m = re.match(r"^  ([a-z_]+):", line)
        if not m:
            break
        keys.append(m.group(1))
    if not keys:
        raise RuntimeError(f"{GAP_TEMPLATE_MD.name}: no `provenance:` key block found")
    return keys


# The template is TWO halves: a ```markdown fence holding the skeleton the auditor copies into a
# spec, and prose BELOW it documenting that skeleton. Only the fence reaches a written spec, so the
# body sections are parsed out of the fence rather than by sweeping the file for `## ` -- a whole-file
# sweep picks up the documentation's own headings (`## Invariants`, `## Provenance block (required)`,
# ...) and would demand them of every gap spec.
_GAP_FENCE_RE = re.compile(r"^```markdown\n(.*?)^```$", re.DOTALL | re.MULTILINE)

# `## Hand-offs` is the one body section the template declares optional in its own words -- "OMIT
# this section entirely when the whole scope is the builder's own". It is dropped from the REQUIRED
# set, not from the parse: a gap whose scope is entirely its builder's own is correct without it,
# and this scenario's fixture is exactly that gap.
_GAP_OPTIONAL_SECTIONS = ("Hand-offs",)


def _gap_body_sections() -> list[str]:
    """The `## ` body headings, in order, from the skeleton fence of templates/gap-epic.md."""
    m = _GAP_FENCE_RE.search(_read_format(GAP_TEMPLATE_MD))
    if not m:
        raise RuntimeError(f"{GAP_TEMPLATE_MD.name}: no ```markdown skeleton fence found")
    heads = [ln[3:].strip() for ln in m.group(1).split("\n") if ln.startswith("## ")]
    if not heads:
        raise RuntimeError(f"{GAP_TEMPLATE_MD.name}: skeleton fence declares no `## ` body sections")
    return heads


def _gap_required_body_sections() -> list[str]:
    """Body sections a written gap spec MUST carry (every skeleton section bar the optional one)."""
    return [h for h in _gap_body_sections() if h not in _GAP_OPTIONAL_SECTIONS]


def _report_required_bullets() -> list[str]:
    """The pass-header bullet labels, in order, from report-format.md's shape block."""
    labels: list[str] = []
    started = False
    for line in _read_format(REPORT_FORMAT_MD).split("\n"):
        if not started:
            if line.startswith("# Audit Pass "):
                started = True
            continue
        m = re.match(r"^- \*\*(.+?):\*\*", line)
        if m:
            labels.append(m.group(1))
        elif labels:
            break
    if not labels:
        raise RuntimeError(f"{REPORT_FORMAT_MD.name}: no `- **Label:**` pass-header bullets found")
    return labels


def _report_verdict_header() -> tuple[str, str]:
    """The per-section verdict table's header + separator, verbatim from report-format.md."""
    lines = _read_format(REPORT_FORMAT_MD).split("\n")
    for i, line in enumerate(lines):
        if line.startswith("| item |"):
            return line, lines[i + 1]
    raise RuntimeError(f"{REPORT_FORMAT_MD.name}: no `| item |` verdict-table header found")


_JSON_FENCE_RE = re.compile(r"^```json\n(.*?)^```$", re.DOTALL | re.MULTILINE)


def contract_blocker_return() -> dict:
    """The blocker return, parsed out of the one ```json fence in audit-agent.md that has a
    `blocker` key. The canned blocker is checked against THIS, so the gate cannot pass on a
    shape the real subagent contract never emits."""
    fences = [f for f in _JSON_FENCE_RE.findall(_read_format(AUDIT_AGENT_MD)) if '"blocker"' in f]
    if len(fences) != 1:
        raise RuntimeError(f"{AUDIT_AGENT_MD.name}: expected exactly one ```json fence with a "
                           f"\"blocker\" key, found {len(fences)}")
    try:
        shape = json.loads(fences[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{AUDIT_AGENT_MD.name}: the blocker fence is not valid JSON: {exc}") from exc
    if not isinstance(shape, dict) or not isinstance(shape.get("blocker"), dict) or not shape["blocker"]:
        raise RuntimeError(f"{AUDIT_AGENT_MD.name}: the blocker fence has no non-empty `blocker` object")
    return shape


def blocker_shape_mismatches(result: dict) -> list[str]:
    """Every way `result` differs in keys from the contract's blocker return (empty = agrees)."""
    shape = contract_blocker_return()
    problems: list[str] = []
    if set(result) != set(shape):
        problems.append(f"top-level keys {sorted(result)} != contract {sorted(shape)}")
    blocker = result.get("blocker")
    if not isinstance(blocker, dict):
        problems.append(f"`blocker` is {type(blocker).__name__}, contract has an object")
    elif set(blocker) != set(shape["blocker"]):
        problems.append(f"blocker keys {sorted(blocker)} != contract {sorted(shape['blocker'])}")
    return problems


def _table_cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


# --------------------------------------------------------------------------- #
# Fixture construction — a temp data_root with three toy "done" epics
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _refine_doc(uc: str, slug: str, title: str) -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        f"slug: {slug}\n"
        f"epic: {slug}\n"
        f"project: {PROJECT_NAME}\n"
        "status: done\n"
        f"domain: {DOMAIN}\n"
        "---\n\n"
        "## Functional stories\n\n"
        f"### {uc} — {title}\n\n"
        f"- **Functional story:** As a billing user, I can {title.lower()}, so that "
        "invoices reconcile.\n\n"
        f"Domain: {DOMAIN}\n"
        "Status: in-progress\n"
    )


def _solution_doc(uc: str, slug: str, title: str, acceptance: list[str]) -> str:
    ac_lines = "\n".join(f"        - \"{a}\"" for a in acceptance)
    return (
        "---\n"
        f'title: "{title} — solution"\n'
        f"slug: {slug}\n"
        "---\n\n"
        "## Use Cases\n\n"
        "| UC | Domain | Status | Summary |\n"
        "|----|--------|--------|---------|\n"
        f"| {uc} | {DOMAIN} | in-progress | {title}. |\n\n"
        f"### {uc} — {title}\n\n"
        f"**Functional summary:** {title}.\n\n"
        "**Technical story:** wire the invoice reconcile path end to end.\n\n"
        "Owning tasks: 1\n\n"
        "## Task 1\n\n"
        "verification:\n"
        "  acceptance_criteria:\n"
        f"{ac_lines}\n"
    )


def _status_done(slug: str) -> str:
    """A minimal orchestrator status.md (status: done) — the survey's done-evidence."""
    return (
        "---\n"
        f"epic: {slug}\n"
        f"plan: design/plans/{slug}.md\n"
        f"started: {DATE}T09:00:00+00:00\n"
        "status: done\n"
        "current_phase: phase-1\n"
        f"project: {PROJECT_NAME}\n"
        f"ended: {DATE}T11:00:00+00:00\n"
        "tasks:\n"
        "  1:\n"
        "    state: done\n"
        "    phase: phase-1\n"
        "---\n\n"
        f"# Epic Status: {slug}\n\nDone.\n"
    )


def build_fixtures(scratch: Path) -> dict:
    if scratch.exists():
        shutil.rmtree(scratch, onerror=_rm_readonly)
    data_root = scratch / "data"
    project_dir = _project_dir(data_root)
    (project_dir / "design" / "specs").mkdir(parents=True)
    (project_dir / "design" / "plans").mkdir(parents=True)
    (project_dir / "status").mkdir(parents=True)

    # --- Mint the toy UC registry via the REAL allocator (refine's job) -------- #
    # Deterministic: fresh counter each run -> UC-0001 (satisfied), UC-0002 (halfmet).
    uc_sat = use_case_id.allocate(
        project_dir, 1, epic_slug="epic-satisfied",
        titles=["Export an invoice as PDF"], date=DATE,
    )[0]
    uc_half = use_case_id.allocate(
        project_dir, 1, epic_slug="epic-halfmet",
        titles=["Email an invoice to the customer"], date=DATE,
    )[0]

    epics = {
        "epic-satisfied": {"uc": uc_sat, "title": "Export an invoice as PDF",
                           "ac": ["GET /invoice/:id.pdf returns a PDF."]},
        "epic-halfmet": {"uc": uc_half, "title": "Email an invoice to the customer",
                         "ac": ["Compose screen renders.", "Send actually delivers the email."]},
        "epic-noverify": {"uc": None, "title": "Reconcile a partial payment",
                          "ac": ["Ledger balances after a partial payment."]},
    }

    for slug, meta in epics.items():
        uc = meta["uc"] or "UC-0009"  # noverify still has a spine UC block; it's just un-judgeable
        _write(project_dir / "design" / "specs" / f"{slug}-design.md",
               _refine_doc(uc, slug, meta["title"]))
        _write(project_dir / "design" / "plans" / f"{slug}.md",
               _solution_doc(uc, slug, meta["title"], meta["ac"]))
        _write(project_dir / "status" / f"{slug}.status.md", _status_done(slug))
        # Nudge status.md mtime ordering so "sort by ended-proxy (mtime) desc" is stable.
        time.sleep(0.01)

    return {"data_root": data_root, "epics": epics, "uc_sat": uc_sat, "uc_half": uc_half}


# --------------------------------------------------------------------------- #
# CANNED subagent output — stands in for the live (non-deterministic) audit agent
# --------------------------------------------------------------------------- #
def canned_subagent_output(fx: dict) -> dict:
    """One canned result per epic. `verdicts` epics are judged; `blocker` epics are
    un-judgeable (routed to the registry, never to a gap epic or a fabricated verdict)."""
    uc_sat, uc_half = fx["uc_sat"], fx["uc_half"]
    return {
        "epic-satisfied": {
            "epic": "epic-satisfied", "strategy_used": "test-suite",
            "verdicts": [
                {"uc_id": uc_sat, "criterion": "GET /invoice/:id.pdf returns a PDF.",
                 "verdict": "satisfied",
                 "evidence": "probe `pytest -k pdf` -> 1 passed; `Content-Type: application/pdf`",
                 "note": "PDF export live"},
            ],
        },
        "epic-halfmet": {
            "epic": "epic-halfmet", "strategy_used": "web-journey",
            "verdicts": [
                {"uc_id": uc_half, "criterion": "Compose screen renders.",
                 "verdict": "partial",
                 "evidence": "screenshots/epic-halfmet-compose.png (compose renders)",
                 "note": "reachable but incomplete"},
                {"uc_id": uc_half, "criterion": "Send actually delivers the email.",
                 "verdict": "missed",
                 "evidence": "probe `curl -X POST /invoice/1/email` -> 200 but no SMTP call logged",
                 "note": "send silently no-ops"},
            ],
        },
        "epic-noverify": {
            "epic": "epic-noverify", "strategy_used": None,
            "blocker": {
                "blocked_on": "no verification.strategy",
                "tried": "resolved project config; verification block absent, cannot pick a probe",
            },
        },
    }


# --------------------------------------------------------------------------- #
# Auditor deterministic machinery (reproduced faithfully from SKILL + formats)
# --------------------------------------------------------------------------- #
SHORTFALLS = {"partial", "missed", "regressed"}

# ---- Discovery (Step 1) --------------------------------------------------- #
def discover(data_root: Path, window: int) -> list[str]:
    """epic_survey -> done -> ledger filter -> sort by ended-proxy (mtime) desc -> window.

    The ledger filter runs BEFORE the truncation, and that order is the fix for gh#114 rather
    than a style choice. Truncating first let an already-audited epic consume a window slot, so
    an unaudited done epic below the cut was never returned and the pass reported "nothing new
    to audit" -- silent, because an empty pass is the designed-healthy state and never reddens.

    It is reachable because the ended proxy is a file mtime, so any bulk rewrite re-dates the
    corpus and re-sorts it. The gh#111 backfill rewrote 21 specs, four of them done and audited
    days earlier, onto one identical mtime above every genuinely-recent epic; at window=5 that
    consumed four of five slots. Filtering first makes the window bound UNAUDITED work, which is
    the only thing it was meant to bound.
    """
    # Builder-side survey: scoped to the configured project, per the auditor SKILL Step 1.
    records = survey_epics(data_root, CUTOFF, project=PROJECT_NAME)
    done = [r for r in records if r.lifecycle_state == "done"]
    audited = _ledger_audited_set(_ledger_path(data_root))
    unaudited = [r for r in done if r.slug not in audited]
    unaudited.sort(key=lambda r: r.last_modified, reverse=True)

    # gh#223: a blocked epic gets NO ledger row (Step 6), and the ledger is the sole dedupe,
    # so it is permanently unaudited and re-enters this window on every pass. Retrying it is
    # correct - a blocker clears and the epic becomes judgeable - but it must not be retried
    # out of the budget meant for work the auditor can actually judge. Partition, so the
    # window bounds JUDGEABLE work and the retries ride outside it.
    previously_blocked = _state_blocked_set(_state_path(data_root))
    fresh = [r for r in unaudited if r.slug not in previously_blocked]
    retry = [r for r in unaudited if r.slug in previously_blocked]
    return [r.slug for r in fresh[:window]] + [r.slug for r in retry]


# ---- Ledger (Step 5.1 + format) ------------------------------------------- #
_LEDGER_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|", re.MULTILINE)


def _ledger_header() -> str:
    """Prose header + the table lines READ OUT OF references/ledger-format.md."""
    header_row, sep_row = _ledger_header_rows()
    return (
        f"# Audit Ledger — {PROJECT_NAME}\n\n"
        "<!-- Append-only. One row per audited epic. This table is ALSO the already-audited\n"
        "     filter: the audit pass skips any slug in the `epic` column. Do not edit or\n"
        "     reorder existing rows. -->\n\n"
        f"{header_row}\n{sep_row}\n"
    )


def _state_blocked_set(state_path: Path) -> set[str]:
    """The slugs the LAST pass recorded as blocked, read back out of the auditor state file.

    `write_auditor_state` has emitted `blocked: [...]` since the repo was cut and NOTHING read
    it, which is why gh#223 could exist: the pass knew perfectly well which epics it could not
    judge, wrote the list down, and then selected the next window as if it had never been told.
    A field with a writer and no reader is indistinguishable from a field nobody maintains.

    Returns an empty set when the file is absent - a first pass has no previous blocked list,
    and that is the ordinary case rather than an error. The missing-file branch is deliberately
    the SAME shape as `_ledger_audited_set`'s above, and that similarity is the hazard: an
    unconditional `return set()` here reads exactly like the legitimate branch two functions
    up, and would silently restore gh#223 with every test still green except the ones written
    for it. `test_a_blocked_epic_is_still_returned_for_retry` is what catches that.
    """
    if not state_path.exists():
        return set()
    text = state_path.read_text(encoding="utf-8")
    m = re.search(r"^blocked:\s*(\[.*?\])\s*$", text, re.MULTILINE)
    if m is None:
        return set()
    try:
        value = json.loads(m.group(1))
    except json.JSONDecodeError:
        return set()
    return {str(s) for s in value}


def _ledger_audited_set(ledger_path: Path) -> set[str]:
    if not ledger_path.exists():
        return set()
    audited: set[str] = set()
    for m in _LEDGER_ROW_RE.finditer(ledger_path.read_text(encoding="utf-8")):
        cell = m.group(1)
        if cell in ("epic", "------"):        # header / separator
            continue
        audited.add(cell)
    return audited


def ledger_append_row(data_root: Path, slug: str, tally: dict, gap_slugs: list[str]) -> None:
    ledger_path = _ledger_path(data_root)
    if not ledger_path.exists():
        _write(ledger_path, _ledger_header())
    gaps = ", ".join(f"`{g}`" for g in gap_slugs) if gap_slugs else "-"
    report_link = f"[report](audits/{DATE}-{PASS_INDEX}.md#{slug})"
    row = (f"| {slug} | {DATE} | {tally['satisfied']} | {tally['partial']} | "
           f"{tally['missed']} | {tally['regressed']} | {gaps} | {report_link} |\n")
    with ledger_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(row)


# ---- Gap synthesis (Step 4) ----------------------------------------------- #
#: Survey cutoff for the gap-slug uniqueness check: no mtime cutoff, per SKILL.md Step 4.3.
_ALL_TIME_CUTOFF = "1970-01-01T00:00:00+00:00"


def unique_gap_slug(data_root: Path, candidate: str, taken_this_pass: set[str] = frozenset()) -> str:
    """SKILL.md Step 4.3: a gap slug unique against EVERY surveyed slug on the data root.

    The survey strips the date prefix and keeps the more-progressed record on a slug
    collision, so a drafted gap reusing an older done gap's slug would be shadowed and never
    reach dev-manager's queue. The survey is project-wide (no `project=`) because dev-manager's
    candidate survey is, and it merges same-slug records across projects before it filters.
    `complete-<source_epic>` is deterministic, so a `--re-audit`
    of the same epic always proposes the same candidate; suffix `-2`, `-3`, ... until free.
    """
    taken = {r.slug for r in survey_epics(data_root, _ALL_TIME_CUTOFF, include_archived=True)}
    taken |= set(taken_this_pass)
    if candidate not in taken:
        return candidate
    n = 2
    while f"{candidate}-{n}" in taken:
        n += 1
    return f"{candidate}-{n}"


def synthesize_gap_spec(data_root: Path, source_epic: str, uc_id: str,
                        shortfalls: list[dict], taken_this_pass: set[str] = frozenset()) -> str:
    """Cluster a source epic's shortfalls into ONE coherent drafted gap spec.

    Reuses the original UC-ID (completing an existing intent — UC continuity rule).
    Returns the gap slug written to design/specs/YYYY-MM-DD-<slug>.md, made unique against
    the project's surveyed slugs and `taken_this_pass` (SKILL.md Step 4.3).
    """
    gap_slug = unique_gap_slug(data_root, f"complete-{source_epic}", taken_this_pass)
    regressed = any(s["verdict"] == "regressed" for s in shortfalls)
    tags = "[regression]" if regressed else "[]"
    evidence_ref = f"../../status/audits/{DATE}-{PASS_INDEX}.md#{source_epic}"
    # gh#111: RESOLVE it, do not merely emit it. This scenario is the only place both the spec and
    # the report exist as real files, so it is the only place the pointer can be checked against a
    # filesystem rather than against a string. The depth was wrong here for exactly as long as it
    # was wrong everywhere else, and this driver emitted a dead pointer and exited PASS.
    _resolved = (_specs_dir(data_root) / evidence_ref.split("#")[0]).resolve()
    _expect = (_project_dir(data_root) / "status" / "audits" / f"{DATE}-{PASS_INDEX}.md").resolve()
    assert _resolved == _expect, (
        f"evidence pointer does not resolve: {evidence_ref} from {_specs_dir(data_root)} "
        f"-> {_resolved}, but the report is written at {_expect}"
    )
    ev_lines = "\n".join(
        f"- `{s['criterion']}` came back **{s['verdict']}**: {s['evidence']}"
        for s in shortfalls
    )
    # The provenance keys, and their ORDER, come from templates/gap-epic.md — not from
    # four literals typed here that assertion (b) would then read back.
    prov_values = {
        "source_epic": source_epic,
        "audit_pass": DATE,
        "use_cases": f'["{uc_id}"]',          # REUSED id — completing an existing intent
        "evidence": evidence_ref,
    }
    prov_keys = _gap_provenance_keys()
    unknown = [k for k in prov_keys if k not in prov_values]
    if unknown:
        raise RuntimeError(
            f"{GAP_TEMPLATE_MD.name} names provenance keys this driver cannot fill: {unknown}"
        )
    prov_block = "".join(f"  {k}: {prov_values[k]}\n" for k in prov_keys)
    body = (
        "---\n"
        f'title: "Complete the intent of {source_epic}"\n'
        f"date: {DATE}\n"
        f"slug: {gap_slug}\n"
        f"project: {PROJECT_NAME}\n"
        "status: drafted\n"
        "current_state: drafted\n"          # auditor writes ONLY this — never approved-for-autonomous
        f"tags: {tags}\n"
        "provenance:\n"
        f"{prov_block}"
        "---\n\n"
        f"# Complete the intent of {source_epic}\n\n"
        "## Gap (one sentence)\n\n"
        f"The use case {uc_id} is only partially met: the capability is reachable but the "
        "acceptance criteria are not all satisfied live.\n\n"
        "## Evidence\n\n"
        f"{ev_lines}\n\n"
        f"See the audit report section {evidence_ref}.\n\n"
        "## Proposed scope\n\n"
        "Close the remaining acceptance criteria so the use case is satisfied end to end.\n\n"
        "## Use case continuity\n\n"
        f"- Completing existing intent: reuse `{uc_id}` (its status stays in-progress on "
        "the spine until this gap closes). The auditor never calls allocate().\n\n"
        # A REAL entry for the fixture's toy product, deliberately backticked -- backticked tokens
        # are what `files_section` extracts as DECLARED PATHS, which is right for an entry the
        # writer means and wrong for an unedited placeholder. The template's own placeholder line
        # stays unbackticked for that reason; this is not that line.
        "## Files\n\n"
        "- `app/invoices/email.py` -- make the send path actually call SMTP, not no-op 200\n"
    )
    # The body sections, like the provenance keys above, are read off templates/gap-epic.md rather
    # than trusted to four literals typed here. `## Files` went missing from this driver for as long
    # as it went missing from the prose (gh#310), and nothing noticed, because nothing compared the
    # spec it wrote against the template it claims to write from.
    missing = [h for h in _gap_required_body_sections() if f"\n## {h}\n" not in body]
    if missing:
        raise RuntimeError(
            f"{GAP_TEMPLATE_MD.name} requires body sections this driver does not write: {missing}"
        )
    _write(_specs_dir(data_root) / f"{DATE}-{gap_slug}.md", body)
    return gap_slug


# ---- Report (Step 5.2 + format) ------------------------------------------- #
def write_report(data_root: Path, sections: list[dict], rollup: dict) -> None:
    # Header bullet labels + the verdict-table header come from report-format.md.
    bullet_values = {
        "Window": "last N done epics (config `audit.window`)",
        "Epics audited": str(rollup["audited"]),
        "Roll-up": (f"{rollup['satisfied']} fully satisfied, {rollup['with_gaps']} with gaps, "
                    f"{rollup['gap_epics']} gap epics filed"),
    }
    labels = _report_required_bullets()
    unknown = [lb for lb in labels if lb not in bullet_values]
    if unknown:
        raise RuntimeError(
            f"{REPORT_FORMAT_MD.name} requires pass-header bullets this driver cannot fill: {unknown}"
        )
    verdict_header, verdict_sep = _report_verdict_header()
    lines = [f"# Audit Pass — {PROJECT_NAME} — {DATE} ({PASS_INDEX})", ""]
    lines += [f"- **{lb}:** {bullet_values[lb]}" for lb in labels]
    lines += [""]
    for sec in sections:
        lines += [f"## {sec['slug']}", ""]
        lines += [verdict_header, verdict_sep]
        for v in sec["verdicts"]:
            lines.append(f"| {v['uc_id']} — {v['criterion']} | {v['verdict']} | {v['evidence']} |")
        lines.append("")
        gaps = sec["gap_slugs"]
        if gaps:
            lines.append("**Gap epics filed from this section:** "
                         + ", ".join(f"`{g}`" for g in gaps))
        else:
            lines.append("**Gap epics filed from this section:** none")
        lines.append("")
    _write(_report_path(data_root), "\n".join(lines) + "\n")


# ---- Auditor state (Step 6/7) --------------------------------------------- #
def decide_continuation(audited: list[str], blocked: list[str]) -> tuple[bool, bool]:
    """The never-halt behaviour itself, as ONE function: -> (scheduled_wake, halt_to_human).

    THIS IS WHAT F2 IS SUPPOSED TO BE TESTING, and until it existed there was nothing for
    the gate to be wrong about. `scheduled_wake` reached the writer as a literal `True`
    from its only call site, `halt_to_human` was a hardcoded string INSIDE the writer, and
    the assertion read both back out of the file that writer had just produced. Every half
    of it was self-witness: it would have passed with the never-halt behaviour deleted from
    the suite entirely.

    The decision ignores `audited`, and that is the point rather than an oversight - an
    empty pass schedules a wake and keeps checking exactly like a full one. The parameters
    exist so that a reintroduced halt has somewhere to be written (`if not audited: return
    False, True` is the shape of the regression) and so the gate can ask for the EMPTY-pass
    answer, which is the case the live scenario never produces.
    """
    return True, False


def write_auditor_state(data_root: Path, audited: list[str], blocked: list[str],
                        scheduled_wake: bool, halt_to_human: bool) -> None:
    """Persist the pass. Neither continuation field is decided here - see decide_continuation.

    `halt_to_human` used to be a hardcoded "false" on this line. A writer that can only
    write the right answer cannot record a wrong one, so no reader of its output can
    detect one either.
    """
    body = (
        "---\n"
        "loop: auditor\n"
        f"last_pass: {DATE}\n"
        f"pass_index: {PASS_INDEX}\n"
        f"scheduled_wake: {str(scheduled_wake).lower()}\n"
        f"audited: {json.dumps(audited)}\n"
        f"blocked: {json.dumps(blocked)}\n"
        f"halt_to_human: {str(halt_to_human).lower()}\n"
        "---\n\n"
        "# auditor loop state\n\n"
        f"Pass {DATE}-{PASS_INDEX}: audited {audited}; blocked {blocked}.\n"
    )
    _write(_state_path(data_root), body)


# --------------------------------------------------------------------------- #
# Registry driver — the SHIPPED fleet surface, not a reimplementation of it.
# There is no local board renderer, no local ownership rule and no local cull:
# every one of those is `agentflow.registry`'s, driven through its own argv.
# --------------------------------------------------------------------------- #
def _reg(data_root: Path, lock_dir: Path, *argv: str) -> tuple[int, str, str]:
    """Drive the SHIPPED registry CLI in-process -> (rc, stdout, stderr).

    Same argv contract, same exit codes and the same `refused [key]` stderr as
    `python -m agentflow.registry`, in one interpreter instead of ~20 subprocess
    spawns (this runs inside `check.py --self-test`). `--project` is deliberately
    NOT passed: `_resolve_project` derives it from the registry path, which keeps
    the file under `status/` where assertion (c) polices it.
    """
    args = list(argv) + ["--file", str(_registry_path(data_root)), "--lock-dir", str(lock_dir)]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = registry_cli.main(args)
        except SystemExit as exc:          # argparse errors; never swallow silently
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


def _refused(rc: int, err: str, key: str) -> bool:
    """A refusal is identified by its BRACKETED KEY, never by message prose.

    Four raised keys are missing from `registry.RULES` and degrade to
    `refused [eta_required]: eta_required` — a prose assertion would be vacuous.
    """
    return rc == 2 and f"refused [{key}]" in err


def _parse_reg(data_root: Path):
    """Read the registry back through the SHIPPED parser, not a local regex."""
    return registry_cli.parse_registry(_registry_path(data_root).read_text(encoding="utf-8"))


def _agents_rows(data_root: Path) -> dict[str, tuple]:
    return {name: tuple(row.__dict__.values()) for name, row in _parse_reg(data_root).agents.items()}


def _blocker_rows(data_root: Path) -> dict[str, tuple]:
    return {bid: tuple(b.__dict__.values()) for bid, b in _parse_reg(data_root).blockers.items()}


def open_auditor_blockers(data_root: Path, lock_dir: Path, slug: str) -> list[str]:
    """Ids of the open auditor blockers for `slug` that SKILL.md Step 6 says suppress a raise.

    Reads through `registry read` with no selector (the lock-free verbatim dump the skill
    names) and parses its stdout with the shipped parser.
    """
    _check_match_rule_columns()
    rc, out, err = _reg(data_root, lock_dir, "read")
    if rc != 0:
        raise RuntimeError(f"the lock-free registry read failed rc={rc}: {err or out}")
    return [bid for bid, row in registry_cli.parse_registry(out).blockers.items()
            if blocker_matches(row, slug)]


def _raw_blocker_line(data_root: Path, bid: str) -> str:
    """The blocker's row EXACTLY as it sits in registry.md (what the cull must move)."""
    for line in _registry_path(data_root).read_text(encoding="utf-8").split("\n"):
        if line.strip().startswith(f"| {bid} |"):
            return line.strip()
    return ""


def _registry_ready(data_root: Path, lock_dir: Path) -> None:
    """Seed the registry from the shipped template on first use (idempotent)."""
    if _registry_path(data_root).exists():
        return
    rc, out, err = _reg(data_root, lock_dir, "init", "--project", PROJECT_NAME)
    if rc != 0:
        raise RuntimeError(f"registry init failed rc={rc}: {err or out}")


# --------------------------------------------------------------------------- #
# One audit pass (main loop) — ties the machinery together
# --------------------------------------------------------------------------- #
def run_audit_pass(data_root: Path, lock_dir: Path, audit: Audit,
                   canned: dict) -> dict:
    """Drive one deterministic audit pass. Returns a summary dict for assertions."""
    # Step 0.5 — read-at-wake: every loop refreshes its OWN ## Agents row. The fleet
    # is `registry.AGENTS`, imported: a local list of loop names drifted once already.
    _registry_ready(data_root, lock_dir)
    for loop in AGENTS:
        state = "building" if loop == "auditor" else "idle"
        phase = "auditing" if loop == "auditor" else "idle"
        rc, out, err = _reg(data_root, lock_dir, "status", "--actor", loop,
                            "--state", state, "--phase", phase, "--eta", _ETA)
        if rc != 0:
            raise RuntimeError(f"seeding the {loop} Agents row was refused rc={rc}: {err or out}")

    # Step 1 — discovery (survey -> done -> ledger filter -> window). See discover().
    window = discover(data_root, audit.window)

    audited: list[str] = []
    blocked: list[str] = []
    report_sections: list[dict] = []
    all_gap_slugs: list[str] = []
    rollup = {"audited": 0, "satisfied": 0, "with_gaps": 0, "gap_epics": 0}

    for slug in window:
        result = canned.get(slug)
        if result is None:
            continue
        # Step 6 — a blocker is un-judgeable: a ## Blockers row owned by dev-manager,
        # recorded in state, NO ledger row, NO gap epic, NO fabricated verdict.
        if "blocker" in result:
            # The Step 6 dedupe, as SKILL.md states it: read the board through the same
            # lock-free `read` the skill names, and skip the raise when an open auditor row
            # already names this epic, whoever owns it now. The rule lives in the skill;
            # this driver applies it (`blocker_matches`, held to the skill's column list by
            # `_check_match_rule_columns`) because it cannot run the skill itself.
            # `--work` takes an E- id or `-`; the fixtures' epics carry no E- id, so `-`.
            work = "-"
            if not open_auditor_blockers(data_root, lock_dir, slug):
                desc = (f"{slug}: {result['blocker']['blocked_on']}; "
                        f"tried: {result['blocker']['tried']}")
                rc, out, err = _reg(data_root, lock_dir, "raise-blocker",
                                    "--actor", "auditor", "--owner", "dev-manager",
                                    "--work", work, "--description", desc)
                if rc != 0:
                    raise RuntimeError(f"raising the auditor blocker failed rc={rc}: {err or out}")
            blocked.append(slug)
            continue

        verdicts = result["verdicts"]
        tally = {"satisfied": 0, "partial": 0, "missed": 0, "regressed": 0}
        for v in verdicts:
            tally[v["verdict"]] += 1
        shortfalls = [v for v in verdicts if v["verdict"] in SHORTFALLS]

        # Step 4 — cluster shortfalls into ONE coherent drafted gap epic (per source epic).
        gap_slugs: list[str] = []
        if shortfalls:
            uc_id = shortfalls[0]["uc_id"]     # same UC -> one cluster, reuse its id
            gap_slugs.append(synthesize_gap_spec(data_root, slug, uc_id, shortfalls,
                                                 set(all_gap_slugs)))
            rollup["with_gaps"] += 1
        else:
            rollup["satisfied"] += 1

        # Step 5.1 — one ledger row per audited epic (append-only).
        ledger_append_row(data_root, slug, tally, gap_slugs)
        audited.append(slug)
        all_gap_slugs += gap_slugs
        report_sections.append({"slug": slug, "verdicts": verdicts, "gap_slugs": gap_slugs})

    rollup["audited"] = len(audited)
    rollup["gap_epics"] = len(all_gap_slugs)

    # Step 5.2 — one pass report (only when something was audited).
    if audited:
        write_report(data_root, report_sections, rollup)

    # Step 5.4 — the registry has NO per-pass audit section (auditor SKILL 4.6): the
    # auditor's own ## Agents row, plus the ledger row and the report file, ARE the
    # breadcrumb. So end the pass by refreshing that row and nothing else.
    # The empty-pass line is reserved for a pass that had nothing to do. A pass that audited
    # nothing BECAUSE everything it drew was blocked is a different state and must not borrow
    # the healthy sentence: that is the gh#114 self-concealing signature arriving through
    # another door, and it is exactly what let gh#223 sit unnoticed while the notify read fine.
    if audited:
        summary = (f"audited {len(audited)}: {rollup['satisfied']} fully met, "
                   f"{rollup['gap_epics']} gap epics filed for approval")
        if blocked:
            # notify-format.md: blocked epics are always counted, right after the counts.
            summary += f"; {len(blocked)} blocked"
    elif blocked:
        summary = (f"audited 0: {len(blocked)} blocked, nothing judgeable "
                   f"({', '.join(blocked)})")
    else:
        summary = "audited 0: nothing new to audit"
    summary_phase = f"audited-{len(audited)}-gaps-{rollup['gap_epics']}"
    rc, out, err = _reg(data_root, lock_dir, "status", "--actor", "auditor",
                        "--state", "idle", "--phase", summary_phase, "--eta", _ETA)
    if rc != 0:
        raise RuntimeError(f"the end-of-pass auditor row refresh failed rc={rc}: {err or out}")

    # Step 7 — persist state, schedule wake. An empty pass idle-wakes and keeps checking; it
    # never halts and keeps no counter, so there is no empty_wakes field to model.
    wake, halt = decide_continuation(audited, blocked)
    write_auditor_state(data_root, audited, blocked, scheduled_wake=wake, halt_to_human=halt)

    return {"window": window, "audited": audited, "blocked": blocked,
            "gap_slugs": all_gap_slugs, "summary": summary, "rollup": rollup}


# --------------------------------------------------------------------------- #
# Filesystem snapshot (for assertion c — writes confined to design/specs/ + status/)
# --------------------------------------------------------------------------- #
# Directory names that churn on their own account (bytecode caches, tool caches,
# VCS metadata, vendored installs). A change under one of these is never the
# product write assertion (c) is hunting, and including them would make the
# assertion flap for reasons unrelated to the auditor.
_VOLATILE_DIR_NAMES = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".eggs",
})


def _walk_files(root: Path, exclude: tuple[Path, ...]) -> list[Path]:
    """Every file under `root`, pruning volatile dirs and the `exclude` subtrees."""
    if not root.is_dir():
        return []
    excluded = {p.resolve() for p in exclude}
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames
                       if d not in _VOLATILE_DIR_NAMES
                       and (here / d).resolve() not in excluded]
        found.extend(here / fn for fn in filenames)
    return found


def snapshot_tree(data_root: Path, *, suite_root: Path = SUITE_ROOT,
                  exclude: tuple[Path, ...] = ()) -> dict[str, float]:
    """mtime snapshot of everything assertion (c) has to be able to SEE.

    Assertion (c)'s claim is "nothing written OUTSIDE design/specs/ + status/".
    Snapshotting the fixture `data_root` alone could not observe the writes it
    forbids: a leak lands in the repo, the skills tree, a product file — all
    outside the fixture. A reviewer proved the hole by adding a write of
    `SUITE_ROOT/harness/LEAKED_PRODUCT_WRITE.txt` inside the driver; the gate
    stayed 15/15 green. So the suite root is observed too, and any change there
    is stray by construction.

    Keys are prefixed by which root they came from (`data_root/…`, `suite/…`) so
    the two namespaces cannot collide and the failure detail names the tree.
    """
    snap: dict[str, float] = {}
    for label, root in (("data_root", data_root), ("suite", suite_root)):
        if not root.is_dir():
            continue
        # The scratch tree IS the fixture; when the driver runs in place it sits
        # under suite_root, and it is already graded through `data_root`.
        for f in _walk_files(root, exclude if label == "suite" else ()):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            snap[f"{label}/{f.relative_to(root).as_posix()}"] = mtime
    return snap


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #
def assert_pass1(fx: dict, result: dict, before: dict, after: dict) -> None:
    data_root = fx["data_root"]
    ledger_text = _ledger_path(data_root).read_text(encoding="utf-8")
    columns = _ledger_columns()
    spec_gaps = list(_specs_dir(data_root).glob(f"{DATE}-*.md"))

    # (a) satisfied epic -> a ledger row in the SHIPPED column shape, NO gap epic filed.
    sat_line = next((ln for ln in ledger_text.split("\n")
                     if ln.strip().startswith("| epic-satisfied |")), None)
    cells = _table_cells(sat_line) if sat_line else []
    shaped = len(cells) == len(columns)
    row = dict(zip(columns, cells)) if shaped else {}
    sat_no_gap = shaped and row.get("gap_epics") == "-"
    sat_has_gap_file = any("source_epic: epic-satisfied" in g.read_text(encoding="utf-8")
                           for g in spec_gaps)
    check("A-sat-ledger", sat_line is not None and shaped and sat_no_gap and not sat_has_gap_file,
          f"satisfied epic has a ledger row in the {len(columns)}-cell shape "
          f"ledger-format.md names, gap_epics='-', and NO gap spec filed",
          f"satisfied epic ledger/gap wrong: row={sat_line!r} cells={len(cells)}/{len(columns)} "
          f"gap_epics={row.get('gap_epics')!r} has_gap_file={sat_has_gap_file}")

    # (LEDGER-row-shape) the header the driver writes IS the format document's header,
    # and every data row carries the same cell count. The reduction, concretely: the
    # shape is read out of ledger-format.md instead of retyped and read back.
    header_row, sep_row = _ledger_header_rows()
    ledger_lines = [ln for ln in ledger_text.split("\n") if ln.strip().startswith("|")]
    header_ok = bool(ledger_lines) and ledger_lines[0] == header_row and ledger_lines[1] == sep_row
    data_lines = ledger_lines[2:]
    bad_width = [ln for ln in data_lines if len(_table_cells(ln)) != len(columns)]
    check("LEDGER-row-shape", header_ok and bool(data_lines) and not bad_width,
          f"ledger header is byte-equal to {LEDGER_FORMAT_MD.name} and all {len(data_lines)} "
          f"data rows carry its {len(columns)} cells",
          f"ledger shape drifted from {LEDGER_FORMAT_MD.name}: header_ok={header_ok} "
          f"rows={len(data_lines)} bad_width={bad_width}")

    # (b) half-met epic -> exactly ONE drafted gap spec, current_state: drafted,
    #     provenance keys (and order) per templates/gap-epic.md, reusing the original UC-ID.
    prov_keys = _gap_provenance_keys()
    half_gaps = [g for g in spec_gaps if "source_epic: epic-halfmet" in g.read_text(encoding="utf-8")]
    ok_b = False
    detail_b = f"expected exactly 1 gap spec for epic-halfmet, found {len(half_gaps)}"
    if len(half_gaps) == 1:
        txt = half_gaps[0].read_text(encoding="utf-8")
        drafted = "current_state: drafted" in txt
        reuses_uc = f'use_cases: ["{fx["uc_half"]}"]' in txt
        found_keys: list[str] = []
        in_block = False
        for line in txt.split("\n"):
            if not in_block:
                if line.rstrip() == "provenance:":
                    in_block = True
                continue
            m = re.match(r"^  ([a-z_]+):", line)
            if not m:
                break
            found_keys.append(m.group(1))
        prov = found_keys == prov_keys and f"audit_pass: {DATE}" in txt
        ok_b = drafted and reuses_uc and prov
        detail_b = (f"gap={half_gaps[0].name} drafted={drafted} "
                    f"reuses_uc({fx['uc_half']})={reuses_uc} "
                    f"provenance_keys={found_keys} (want {prov_keys})")
    check("B-halfmet-gap", ok_b,
          f"exactly ONE drafted gap spec reusing UC-ID {fx['uc_half']}: {detail_b}", detail_b)

    # (c) nothing written outside design/specs/ + status/ — which also covers every
    #     registry write, because registry.md and registry-log.md live under status/.
    #     The snapshot spans the fixture data_root AND the suite root (see
    #     snapshot_tree), so a write that escapes the fixture — into the repo, the
    #     skills tree, a product file — is visible here and stray by construction.
    allowed = (f"data_root/projects/{PROJECT_NAME}/design/specs/",
               f"data_root/projects/{PROJECT_NAME}/status/")
    changed = [p for p in after if p not in before or after[p] != before[p]]
    gone = [p for p in before if p not in after]
    stray = sorted(p for p in changed + gone if not p.startswith(allowed))
    check("C-write-scope", not stray,
          f"every write confined to design/specs/ + status/ ({len(changed)} files touched, "
          f"registry included); zero writes anywhere else under {SUITE_ROOT.name}/ "
          f"({len(before)} paths observed across the fixture and the suite root)",
          f"auditor wrote OUTSIDE design/specs/+status/: {stray}")

    # (d) NO 'approved-for-autonomous' in the artifacts THIS PASS WROTE.
    #
    # WHAT THIS COVERS, EXACTLY: the drafted-only invariant on OUTPUT - the gap specs,
    # ledger, report, registry and state file this pass produced carry no self-approval
    # stamp. The must-exist pre-check stops a missing artifact passing by having nothing
    # to scan.
    #
    # WHAT IT DOES NOT COVER: the shipped `skills/auditor/SKILL.md`. The verdicts driving
    # this pass are CANNED, so the skill's prose never executes and its text is never read
    # here. A reviewer stamped `approved-for-autonomous` into that skill and this assertion
    # stayed green - correctly, because the artifacts really were clean. Policing the
    # skill's own text is a different assertion, and it is not this one.
    must_exist = [_ledger_path(data_root), _report_path(data_root),
                  _registry_path(data_root), _state_path(data_root)]
    absent = [p.name for p in must_exist if not p.exists()]
    auditor_outputs = spec_gaps + must_exist + [_registry_log_path(data_root)]
    hits = [p.name for p in auditor_outputs if p.exists()
            and "approved-for-autonomous" in p.read_text(encoding="utf-8")]
    check("D-no-approval", not hits and not absent,
          f"no 'approved-for-autonomous' in the {len(auditor_outputs)} artifacts this pass "
          f"WROTE (drafted-only invariant), every must-exist artifact present; the shipped "
          f"skills/auditor/SKILL.md text is NOT scanned - canned verdicts never execute it",
          f"'approved-for-autonomous' leaked into auditor output: {hits}"
          if hits else f"artifacts the scan needs are absent, so it proved nothing: {absent}")

    # (REPORT-anchors) every report link the LEDGER emits resolves to a section heading,
    # and the report carries the header bullets + verdict-table header report-format.md names.
    report_text = _report_path(data_root).read_text(encoding="utf-8")
    linked = re.findall(r"\[report\]\(audits/[^)#]+#([^)]+)\)", ledger_text)
    missing_anchors = [s for s in linked if f"\n## {s}\n" not in "\n" + report_text]
    labels = _report_required_bullets()
    missing_bullets = [lb for lb in labels if f"- **{lb}:**" not in report_text]
    verdict_header, _ = _report_verdict_header()
    has_verdict_table = verdict_header in report_text
    check("REPORT-anchors",
          bool(linked) and not missing_anchors and not missing_bullets and has_verdict_table,
          f"every ledger report link resolves to a report section ({sorted(linked)}), and the "
          f"report carries the {labels} header bullets + the verdict-table header",
          f"report/ledger inconsistent: linked={linked} missing_anchors={missing_anchors} "
          f"missing_bullets={missing_bullets} verdict_table={has_verdict_table}")


def assert_pass2(data_root: Path, lock_dir: Path, canned: dict, result2: dict, ledger_before: str,
                 specs_before: set, blockers_before: set) -> None:
    # (e) a SECOND pass audits nothing (ledger dedupe filter works) and re-raises nothing.
    ledger_after = _ledger_path(data_root).read_text(encoding="utf-8")
    specs_after = set(p.name for p in _specs_dir(data_root).glob(f"{DATE}-*.md"))
    blockers_after = set(_blocker_rows(data_root))
    no_new_ledger = ledger_after == ledger_before
    no_new_gaps = specs_after == specs_before
    # Never byte-compare registry.md: the heartbeat cell is stamped from the real clock.
    no_new_blockers = blockers_after == blockers_before
    # The blocker half proves the Step 6 dedupe, not a window that skipped the epic: the
    # blocked epic must have been RE-JUDGED this pass (Step 1 sub-step 3a re-includes it and
    # the canned subagent returns the blocker again), and still exactly one open auditor row
    # names it. Without the dedupe the re-judge raises a second identical row.
    canned_blocked = sorted(slug for slug, r in canned.items() if "blocker" in r)
    rejudged = bool(canned_blocked) and sorted(result2["blocked"]) == canned_blocked
    open_rows = {s: open_auditor_blockers(data_root, lock_dir, s) for s in canned_blocked}
    one_row_each = bool(open_rows) and all(len(ids) == 1 for ids in open_rows.values())
    check("E-second-pass-noop",
          result2["audited"] == [] and no_new_ledger and no_new_gaps and no_new_blockers
          and rejudged and one_row_each,
          "second pass audited nothing: 0 new ledger rows, 0 new gap specs, 0 new B- rows; "
          f"blocked epic(s) {result2['blocked']} were re-judged and the SKILL.md Step 6 dedupe "
          "skipped the raise (one open auditor row each)",
          f"second pass was not a no-op: audited={result2['audited']} "
          f"new_ledger={not no_new_ledger} new_gaps={not no_new_gaps} "
          f"new_blockers={sorted(blockers_after - blockers_before)} "
          f"blocked={result2['blocked']} open_auditor_rows={open_rows}")


def f2_passes(owned_by_dm: bool, nonloop_owner_refused: bool, not_halt: bool,
              canned: dict) -> tuple[bool, list[str], dict]:
    """F2's pass/fail, separated from the registry reads so a test can drive it directly.

    The routing is only worth proving for a shape the real subagent emits, so every canned
    blocker is held to the blocker return parsed out of audit-agent.md, and at least one must
    exist. Returns (passed, canned blocker slugs, per-slug shape problems)."""
    canned_blockers = sorted(slug for slug, r in canned.items() if "blocker" in r)
    shape_problems = {slug: probs for slug in canned_blockers
                      if (probs := blocker_shape_mismatches(canned[slug]))}
    shape_ok = bool(canned_blockers) and not shape_problems
    return (owned_by_dm and nonloop_owner_refused and not_halt and shape_ok,
            canned_blockers, shape_problems)


def assert_registry(data_root: Path, lock_dir: Path, canned: dict) -> None:
    """(f) Ownership, refusals and the cull — all judged on the SHIPPED registry."""
    validations: list[str] = []
    checkpoints = 0

    def _validate(when: str) -> None:
        nonlocal checkpoints
        checkpoints += 1
        rc, out, err = _reg(data_root, lock_dir, "validate")
        if rc != 0 or "registry valid" not in out:
            validations.append(f"{when}: rc={rc} out={out.strip()!r} err={err.strip()!r}")

    # (f1) the fleet is `registry.AGENTS`, each loop owns exactly its own row, and a
    #      non-loop actor is refused. No local list of loop names, and NO LOOP COUNT
    #      either: the count is exactly the literal that drifted last time (this gate
    #      shipped saying "five" while the fleet was six). What is asserted is the
    #      identity — the set the shipped template seeds IS the set the package
    #      exports — which stays true through the next loop the fleet grows.
    seeded = set(_agents_rows(data_root))
    fleet_ok = bool(AGENTS) and seeded == set(AGENTS)
    own_row_only: list[str] = []
    for loop in AGENTS:
        before = _agents_rows(data_root)
        rc, out, err = _reg(data_root, lock_dir, "status", "--actor", loop, "--state", "idle",
                            "--phase", f"own-row-probe-{loop}", "--eta", _ETA)
        after = _agents_rows(data_root)
        touched = sorted(k for k in after if after[k] != before.get(k))
        if rc != 0 or touched != [loop]:
            own_row_only.append(f"{loop}: rc={rc} touched={touched} err={err.strip()[:60]!r}")
    rc_h, _, err_h = _reg(data_root, lock_dir, "status", "--actor", "human",
                          "--state", "idle", "--phase", "idle", "--eta", _ETA)
    human_refused = _refused(rc_h, err_h, "agent_known")
    _validate("after the Agents-row writes")
    check("F1-agents-own-rows", fleet_ok and not own_row_only and human_refused,
          f"registry.AGENTS is the fleet {sorted(AGENTS)}, seeded whole from the shipped "
          f"template; each loop's `status` write "
          f"changed exactly its own row, and --actor human was refused [agent_known]",
          f"Agents-row ownership wrong: fleet_ok={fleet_ok} seeded={sorted(seeded)} "
          f"cross_writes={own_row_only} human_refused={human_refused}")

    # (f2) the auditor's blocker -> a ## Blockers row owned by dev-manager, and NOT a halt.
    #      That no blocker is owned by a non-loop is proved by the REFUSAL, not by grepping
    #      the column the driver just wrote.
    blockers = _blocker_rows(data_root)
    auditor_blk = [b for b in blockers.values()
                   if b[1] == "auditor" and b[5].startswith("epic-noverify:")]
    owned_by_dm = bool(auditor_blk) and auditor_blk[0][2] == "dev-manager" and auditor_blk[0][4] == "open"
    rc_o, _, err_o = _reg(data_root, lock_dir, "raise-blocker", "--actor", "auditor",
                          "--owner", "human", "--work", "-", "--description", "probe")
    nonloop_owner_refused = _refused(rc_o, err_o, "agent_known")
    state_text = _state_path(data_root).read_text(encoding="utf-8")
    not_halt = "halt_to_human: false" in state_text and "scheduled_wake: true" in state_text
    f2_ok, canned_blockers, shape_problems = f2_passes(
        owned_by_dm, nonloop_owner_refused, not_halt, canned)
    check("F2-auditor-blocker-dm", f2_ok,
          "auditor blocker routed to an open ## Blockers row owned by dev-manager; a non-loop "
          "owner is REFUSED [agent_known]; the loop scheduled a wake and did NOT halt-to-human; "
          f"the canned blocker {canned_blockers} has the keys of the blocker return in "
          f"{AUDIT_AGENT_MD.name}",
          f"auditor blocker routing wrong: owned_by_dm={owned_by_dm} "
          f"nonloop_owner_refused={nonloop_owner_refused} not_halt={not_halt} "
          f"canned_blockers={canned_blockers} shape_problems={shape_problems} "
          f"rows={list(blockers.values())}")

    # (f2b) THE EMPTY PASS, which the scenario itself never produces.
    #
    # F2 above reads a state file written by a pass that audited something, so it can only
    # ever see the easy case. The behaviour that failed overnight - the whole reason this
    # gate exists - is the pass that finds NOTHING: that is when a loop used to count itself
    # up to three and stop. Asking `decide_continuation` for the empty answer directly is
    # the only way to put that case in front of an assertion, because no arrangement of the
    # scenario's fixtures produces an empty audit window.
    #
    # WHAT THIS COVERS, EXACTLY: `decide_continuation` is THIS DRIVER'S reproduction of the
    # continuation rule - the auditor SKILL is prose with no importable decision function,
    # so there is nothing shipped to call. The assertion proves the reproduction continues
    # on empty; it does NOT read `skills/auditor/SKILL.md`. A reviewer reintroduced the halt
    # rule into that skill and this stayed green, which is the honest limit of a canned
    # stand-in: canned verdicts are legitimate here, claiming they cover the shipped prose
    # is not. Guarding SKILL.md's text is a separate assertion nobody has written yet.
    empty_wake, empty_halt = decide_continuation([], [])
    check("F2b-empty-pass-continues", empty_wake and not empty_halt,
          "an empty pass schedules a wake and does not halt - proved against this driver's "
          "reproduction of the continuation rule (`decide_continuation`), NOT against the "
          "prose in skills/auditor/SKILL.md, which no assertion here reads",
          f"an empty pass would halt or not re-wake: scheduled_wake={empty_wake} "
          f"halt_to_human={empty_halt} - this is the overnight failure, reintroduced")

    # (f3) a seeded jr-dev blocker is OWNED BY lead-dev, and its RAISER cannot resolve it.
    #      The refusal is the half that stops this being the driver reading back the column
    #      it just wrote.
    before_ids = set(_blocker_rows(data_root))
    rc_r, out_r, err_r = _reg(data_root, lock_dir, "raise-blocker", "--actor", "jr-dev-1",
                              "--owner", "lead-dev", "--work", "-",
                              "--description", "epic-checkout: staging unreachable; "
                                               "tried: retried deploy twice")
    new_ids = sorted(set(_blocker_rows(data_root)) - before_ids)
    jr_id = new_ids[0] if len(new_ids) == 1 else ""
    rows = _blocker_rows(data_root)
    owned_by_ld = bool(jr_id) and rows[jr_id][2] == "lead-dev" and rows[jr_id][1] == "jr-dev-1"
    rc_x, _, err_x = _reg(data_root, lock_dir, "resolve-blocker", "--actor", "jr-dev-1",
                          "--id", jr_id or "B-9999")
    raiser_refused = _refused(rc_x, err_x, "blocker_owner_resolves")
    _validate("after the jr-dev blocker")
    check("F3-blocker-ownership", rc_r == 0 and owned_by_ld and raiser_refused,
          f"seeded jr-dev-1 blocker {jr_id} is owned by lead-dev, and jr-dev-1 resolving its own "
          f"blocker is REFUSED [blocker_owner_resolves]",
          f"blocker ownership wrong: raise_rc={rc_r} err={err_r.strip()[:80]!r} new_ids={new_ids} "
          f"owned_by_ld={owned_by_ld} raiser_refused={raiser_refused} "
          f"resolve_err={err_x.strip()[:80]!r}")

    # (f4) the OWNER resolves; dev-manager may NOT cull (E-0220 moved it to steward); the
    #      steward cull rolls the resolved row to registry-log.md verbatim and leaves the
    #      Agents rows alone — compared against `registry.AGENTS`, never against a count
    #      typed here, so this survives the fleet growing another loop.
    rc_res, _, err_res = _reg(data_root, lock_dir, "resolve-blocker", "--actor", "lead-dev",
                              "--id", jr_id or "B-9999")
    # Snapshot the row AFTER the resolve: the cull moves the row as it stands at cull time
    # (state `resolved`), so a pre-resolve snapshot would never match and this conjunct
    # would fail for a reason that has nothing to do with the cull.
    raw_row = _raw_blocker_line(data_root, jr_id) if jr_id else ""
    rc_dm, _, err_dm = _reg(data_root, lock_dir, "cull", "--actor", "dev-manager")
    dm_refused = _refused(rc_dm, err_dm, "cull_steward")
    rc_cull, out_cull, err_cull = _reg(data_root, lock_dir, "cull", "--actor", "steward")
    cull_line = out_cull.strip()
    cull_shaped = bool(re.match(r"^cull: .* -> registry-log\.md \(\d+ -> \d+ bytes\)$", cull_line))
    cull_counted = " 1 blocker," in cull_line
    log_path = _registry_log_path(data_root)
    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    marked = bool(re.search(r"<!-- culled \d{4}-\d\d-\d\dT\d\d:\d\dZ -->", log_text))
    rolled_verbatim = bool(raw_row) and raw_row in log_text
    survivors = _blocker_rows(data_root)
    still_there = [b for b in survivors.values() if "epic-checkout" in b[5]]
    agents_intact = set(_agents_rows(data_root)) == set(AGENTS)
    _validate("after the steward cull")
    check("F4-cull-to-registry-log",
          rc_res == 0 and dm_refused and rc_cull == 0 and cull_shaped and cull_counted
          and marked and rolled_verbatim and not still_there and agents_intact,
          f"owner lead-dev resolved {jr_id}; dev-manager's cull was REFUSED [cull_steward]; the "
          f"steward cull rolled the row verbatim under a `<!-- culled ...Z -->` marker "
          f"({cull_line!r}); no surviving ## Blockers row mentions epic-checkout and all "
          f"{len(AGENTS)} Agents rows survive",
          f"cull wrong: resolve_rc={rc_res} ({err_res.strip()[:60]!r}) dm_refused={dm_refused} "
          f"cull_rc={rc_cull} shaped={cull_shaped} counted={cull_counted} marked={marked} "
          f"verbatim={rolled_verbatim} survivors={[b[0] for b in still_there]} "
          f"agents_intact={agents_intact} err={err_cull.strip()[:80]!r}")

    # (REGISTRY-valid) the shipped validator accepts everything this driver wrote — the
    # cheapest proof that none of it is a shape the shipped parser rejects.
    check("REGISTRY-valid", not validations,
          f"`registry validate` returned 0 and printed `registry valid` at all "
          f"{checkpoints} post-mutation checkpoints",
          f"the shipped validator rejected what this driver wrote: {validations}")


def assert_rename() -> None:
    # (g1) zero surviving developer-1|developer-2|developer-3 skill-name refs.
    pat = re.compile(r"developer-[123]")
    offenders: list[str] = []
    # Tracked files only (gh#410): a scratch copy under skills/ must not move this verdict.
    for base in ("skills", "commands"):
        for f in tracked_files(SUITE_ROOT, lambda rel, b=base: rel.startswith(b + "/")):
            if not f.is_file() or f.suffix not in (".md", ".py", ".json", ".js"):
                continue
            if "_vendored" in f.parts or "node_modules" in f.parts:
                continue
            try:
                if pat.search(f.read_text(encoding="utf-8", errors="ignore")):
                    offenders.append(str(f.relative_to(SUITE_ROOT)))
            except OSError:
                continue
    check("G1-no-developer-N", not offenders,
          "zero surviving developer-1|developer-2|developer-3 skill-name refs in skills/ + commands/",
          f"developer-N skill-name refs survive: {offenders}")

    # (g2) builder_entry resolves to /lead-dev — read from the TRACKED example config, so
    # this runs on a bare clone. The machine-local harness.config.json is gitignored, and
    # reading it unguarded is what used to abort this driver before it printed a summary.
    cfg_path = HARNESS_DIR / "harness.config.example.json"
    if not cfg_path.exists():
        check("G2-builder-entry", False, "", f"tracked config not found: {cfg_path}")
        return
    be = json.loads(cfg_path.read_text(encoding="utf-8")).get("builder_entry")
    check("G2-builder-entry", be == "/lead-dev",
          f"{cfg_path.name} builder_entry == {be!r}",
          f"builder_entry is {be!r} in {cfg_path.name}, expected '/lead-dev'")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run_ship_gate(scratch: Path) -> list[tuple[str, str, str]]:
    """Build fixtures under `scratch`, drive both passes, emit every assertion.

    Returns the (status, id, detail) rows. Callers: `main()` and `harness/check.py`
    (`--auditor-scenario`, and self-test step 6).
    """
    _RESULTS.clear()
    fx = build_fixtures(scratch)
    data_root = fx["data_root"]
    lock_dir = scratch / "locks"          # OUTSIDE data_root: the edit-lock is not an auditor write
    audit = Audit()   # real defaults from agentflow.config — window=7, roles=[], notify=True
    canned = canned_subagent_output(fx)
    print(f"# project={PROJECT_NAME}  window={audit.window}  "
          f"UCs: satisfied={fx['uc_sat']} halfmet={fx['uc_half']}")
    print("# canned verdicts: epic-satisfied=satisfied, epic-halfmet=partial+missed, "
          "epic-noverify=BLOCKER(no verification.strategy)")

    # ---- PASS 1 ----------------------------------------------------------- #
    # `scratch` is excluded from the suite-root half of the snapshot: it IS the
    # fixture (it sits under the suite root on an in-place run) and is graded
    # through `data_root`. Everything else under the suite root is off-limits.
    before = snapshot_tree(data_root, exclude=(scratch,))
    r1 = run_audit_pass(data_root, lock_dir, audit, canned)
    after = snapshot_tree(data_root, exclude=(scratch,))
    print(f"# pass 1: window={r1['window']}  audited={r1['audited']}  "
          f"blocked={r1['blocked']}  gaps={r1['gap_slugs']}")
    print(f"#          summary: {r1['summary']}")

    assert_pass1(fx, r1, before, after)

    # ---- REGISTRY (f) — ownership, refusals, cull -------------------------- #
    assert_registry(data_root, lock_dir, canned)

    # ---- PASS 2 (e) — must be a no-op ------------------------------------- #
    ledger_before = _ledger_path(data_root).read_text(encoding="utf-8")
    specs_before = set(p.name for p in _specs_dir(data_root).glob(f"{DATE}-*.md"))
    blockers_before = set(_blocker_rows(data_root))
    r2 = run_audit_pass(data_root, lock_dir, audit, canned)
    print(f"# pass 2: window={r2['window']}  audited={r2['audited']}  summary: {r2['summary']}")
    assert_pass2(data_root, lock_dir, canned, r2, ledger_before, specs_before, blockers_before)

    # ---- RENAME (g) -------------------------------------------------------- #
    assert_rename()

    return list(_RESULTS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scratch", type=Path, default=SCRATCH,
                    help="throwaway fixture dir (default: ./_scratch beside this driver)")
    ap.add_argument("--emit-rows", action="store_true",
                    help=f"also emit each assertion as a machine-readable "
                         f"'{ROW_PREFIX}<status>\\t<id>\\t<detail>' line, for harness/check.py")
    args = ap.parse_args(argv)
    _silence_survey_noise()

    print("# agentflow Auditor SHIP GATE (A7) — post-build intent-feedback-loop scenario")
    print(f"# scratch={args.scratch}  (throwaway fixtures; live vault untouched)")
    print(f"# graded tree: {Path(registry_cli.__file__).resolve().parent}")

    rows = run_ship_gate(args.scratch)

    if args.emit_rows:
        for status, aid, detail in rows:
            flat = detail.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
            print(f"{ROW_PREFIX}{status}\t{aid}\t{flat}")

    npass = sum(1 for s, _, _ in rows if s == "PASS")
    nfail = sum(1 for s, _, _ in rows if s == "FAIL")
    print("")
    for status, aid, detail in rows:
        print(f"{status} {aid}: {detail}")
    print(f"\n# summary: {npass} PASS  {nfail} FAIL  ({len(rows)} assertions)")
    if nfail == 0:
        print("# SHIP GATE: PASS — auditor deterministic machinery closes the "
              "build->verify->refine loop: satisfied epic proven, half-met epic drafted a "
              "provenance-stamped gap (UC reused), writes confined to design/specs/+status/, "
              "no self-approval, second pass a no-op, and the SHIPPED registry routes blockers "
              "by ownership, refuses non-owners, and culls to registry-log.md; rename clean.")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
