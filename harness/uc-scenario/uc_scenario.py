#!/usr/bin/env python3
"""Phase-4 SHIP GATE — end-to-end use-case traceability acceptance scenario (T11).

Self-contained, deterministic, re-runnable. Builds throwaway fixtures (a toy
git code repo + design/specs, design/plans and flow docs) under ``_scratch/`` — NEVER touches
the live vault or real project data — then proves the whole spine:

    1. Mint an opaque UC via ``agentflow.use_case_id.allocate`` (birth-cert row).
    2. Place it across all five hops:
         refine  — ``### UC-NNNN`` functional-story block (Domain + Status)
         solution— ``## Use Cases`` doc-of-record + task-frontmatter ``use_cases``
         commit  — a ``Use-Case: UC-NNNN`` git trailer in the toy repo
         code    — an activation-chain anchor inside tracked source
         flow    — an ``<autodoc_root>/flows/`` page frontmatter + ``## Use Cases`` row
    3. Prove ``grep -r UC-NNNN`` (files) + ``git log --grep`` (commit) returns
       every hop, and ``agentflow.use_case_impact`` locates the same floor.
    4. Prove the flow-page ``use_cases`` is an OPAQUE scalar list (gate-inert:
       no ``[[wikilink]]``) and that ``plan_parser`` threads ``use_cases`` into
       task frontmatter (additive field — golden Scenario A stays green).
    5. Reconcile the flow page against code via the liveness protocol:
         chain present -> Status ``completed`` (self-heal live)
         simulated code-removal + reconcile -> Status flips to ``deprecated``
         with a ``> [!stale]`` banner, WITHOUT touching the plan, the spec or the plan doc.

Emits ``PASS/FAIL <id>: <detail>`` lines and a summary; exits 0 iff every
assertion passes. Mirrors ``harness/check.py`` in spirit but stands alone so
the Phase-0/1 golden gate (31 PASS) is untouched.

Usage:
    python harness/uc-scenario/uc_scenario.py
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def _rm_readonly(func, path, _exc):
    """rmtree onerror: clear the read-only bit git sets on pack/objects (Windows)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)

SCRIPT_DIR = Path(__file__).resolve().parent
SCRATCH = SCRIPT_DIR / "_scratch"

# Make the editable-installed agentflow package importable regardless of cwd.
_PKG_PARENT = SCRIPT_DIR.parents[1] / "skills" / "_shared"
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from agentflow import use_case_id, use_case_impact  # noqa: E402
from agentflow.plan_parser import parse_plan  # noqa: E402

PROJECT_NAME = "uc-demo"
EPIC_SLUG = "phase4-use-case-traceability"
DATE = "2026-07-18"
DOMAIN = "auth/login"
ACTIVATION_FN = "def uc_login("  # the activation chain the reconcile reads for

# --------------------------------------------------------------------------- #
# Result plumbing (PASS/FAIL <id>: detail, exit 0 iff all pass)
# --------------------------------------------------------------------------- #
_RESULTS: list[tuple[str, str, str]] = []


def record(status: str, aid: str, detail: str) -> None:
    _RESULTS.append((status, aid, detail))


def check(aid: str, cond: bool, ok_detail: str, bad_detail: str) -> bool:
    record("PASS" if cond else "FAIL", aid, ok_detail if cond else bad_detail)
    return cond


# --------------------------------------------------------------------------- #
# Toy git repo helpers — throwaway, isolated identity, never the real repo
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str, check_rc: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [
            "git",
            "-c", "user.email=harness@agentflow.local",
            "-c", "user.name=agentflow-harness",
            "-c", "commit.gpgsign=false",
            "-C", str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
    )
    if check_rc and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


# --------------------------------------------------------------------------- #
# Fixture construction
# --------------------------------------------------------------------------- #
def build_fixtures() -> dict:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, onerror=_rm_readonly)
    data_root = SCRATCH / "data"
    autodoc_root = SCRATCH / "autodoc"
    code_repo = SCRATCH / "code-repo"
    project_dir = data_root / "projects" / PROJECT_NAME
    (project_dir / "design" / "specs").mkdir(parents=True)
    (project_dir / "design" / "plans").mkdir(parents=True)
    (autodoc_root / "flows").mkdir(parents=True)
    code_repo.mkdir(parents=True)

    # --- HOP 0: mint the opaque UC via the real allocator ------------------ #
    uc_ids = use_case_id.allocate(
        project_dir,
        1,
        epic_slug=EPIC_SLUG,
        titles=["Citizen exchanges GitHub OAuth for a session token"],
        date=DATE,
    )
    uc = uc_ids[0]

    # --- HOP 1: refine functional-story block ------------------------------ #
    (project_dir / "design" / "specs" / "uc-traceability-design.md").write_text(
        "---\ntitle: UC traceability refine\n---\n\n"
        "## Functional stories\n\n"
        f"### {uc} — Citizen exchanges GitHub OAuth for a session token\n\n"
        "**Functional story:** A returning citizen clicks \"Sign in with GitHub\", "
        "authorizes, and lands back in the app already holding a session token.\n\n"
        f"Domain: {DOMAIN}\n"
        "Status: backlog\n",
        encoding="utf-8",
        newline="\n",
    )

    # --- HOP 2a: solution doc-of-record ------------------------------------ #
    (project_dir / "design" / "plans" / "uc-traceability.md").write_text(
        "---\ntitle: UC traceability solution\n---\n\n"
        "## Use Cases\n\n"
        "| UC | Domain | Status | Summary |\n"
        "|----|--------|--------|---------|\n"
        f"| {uc} | {DOMAIN} | backlog | Citizen exchanges GitHub OAuth for a session token. |\n\n"
        "**Technical story:** the OAuth callback handler mints a session via "
        f"`{ACTIVATION_FN.rstrip('(')}` and returns it to the browser.\n\n"
        f"Owning tasks: 1 (threads `{uc}`).\n",
        encoding="utf-8",
        newline="\n",
    )

    # --- HOP 2b: plan with task-frontmatter use_cases (additive field) ----- #
    (project_dir / "design" / "plans" / "plan.md").write_text(
        "---\n"
        'title: "UC traceability plan"\n'
        f'date: "{DATE}"\n'
        f'epic: "{EPIC_SLUG}"\n'
        "slug: uc-traceability\n"
        'spec: "design/plans/uc-traceability.md"\n'
        "per_wave_worktrees: true\n\n"
        "phases:\n  - phase-1\n\n"
        "test_commands:\n  regression: \"python -m pytest -q\"\n\n"
        "tasks:\n"
        "  1:\n"
        "    phase: phase-1\n"
        "    deps: []\n"
        f"    use_cases: [\"{uc}\"]\n"
        "    verification:\n"
        "      acceptance_criteria:\n"
        "        - \"calc.py exposes uc_login().\"\n"
        "      file_allowlist:\n"
        "        - \"calc.py\"\n"
        "---\n\n# UC traceability plan\n\n## Task 1\nImplement uc_login().\n",
        encoding="utf-8",
        newline="\n",
    )

    # --- HOP 3+4: toy code repo, activation chain, commit trailer ---------- #
    calc = code_repo / "calc.py"
    calc.write_text(
        f"# {uc}: activation-chain anchor for the GitHub-OAuth session flow.\n"
        f"{ACTIVATION_FN}user):\n"
        '    """Mint a session token for an authenticated citizen."""\n'
        '    return f"session::{user}"\n',
        encoding="utf-8",
        newline="\n",
    )
    _git(code_repo, "init", "-q", "-b", "main")
    _git(code_repo, "add", "calc.py")
    _git(
        code_repo,
        "commit",
        "-q",
        "-m",
        "feat: add uc_login session minting\n\n"
        f"Wire the GitHub-OAuth callback to a session token.\n\nUse-Case: {uc}",
    )

    # --- HOP 5: flow page (opaque scalar use_cases + ## Use Cases row) ------ #
    # Status starts backlog (plan-derived); the reconcile step owns post-ship.
    flow_page = autodoc_root / "flows" / "GitHub OAuth Session.md"
    flow_page.write_text(
        "---\n"
        "title: GitHub OAuth Session\n"
        "type: flow\n"
        "status: active\n"
        f"updated: {DATE}\n"
        "source_paths:\n"
        "  - calc.py\n"
        "entrypoint: \"[[GitHub OAuth Callback]]\"\n"
        "touches:\n"
        "  - \"[[Session Store]]\"\n"
        "use_cases:\n"
        f"  - \"{uc}\"\n"
        "---\n\n"
        "GitHub OAuth Session — how a citizen trades a GitHub grant for a session.\n\n"
        "## What it does\n\n"
        "The callback validates the grant and mints a session token.\n\n"
        "## Use Cases\n\n"
        "| UC | Domain | Status | Summary |\n"
        "|----|--------|--------|---------|\n"
        f"| {uc} | {DOMAIN} | backlog | Citizen exchanges GitHub OAuth for a session token. |\n",
        encoding="utf-8",
        newline="\n",
    )

    return {
        "uc": uc,
        "data_root": data_root,
        "autodoc_root": autodoc_root,
        "code_repo": code_repo,
        "project_dir": project_dir,
        "flow_page": flow_page,
        "calc": calc,
    }


# --------------------------------------------------------------------------- #
# grep chain — real recursive text grep + git-log trailer grep
# --------------------------------------------------------------------------- #
def _grep_r(root: Path, needle: str) -> list[str]:
    """`grep -rn` over files; degrade to a portable Python scan if grep absent.

    Runs with ``cwd=root`` and a ``.`` target so hits are ROOT-RELATIVE
    (``./autodoc/flows/…``) — an absolute Windows path would carry a drive
    ``C:`` colon that collides with grep's own ``path:line:text`` delimiter.
    """
    try:
        proc = subprocess.run(
            ["grep", "-rn", "--binary-files=without-match", "--exclude-dir=.git", needle, "."],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        if proc.returncode in (0, 1):  # 1 == no match, not an error
            return [ln for ln in proc.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    # Fallback: pure-Python recursive scan, emitting root-relative paths.
    hits: list[str] = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or ".git" in f.parts:
            continue
        rel = f.relative_to(root).as_posix()
        try:
            for i, line in enumerate(
                f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                if needle in line:
                    hits.append(f"./{rel}:{i}:{line}")
        except OSError:
            continue
    return hits


def _classify_hop(path_str: str, fx: dict) -> str | None:
    """Map a grep hit path to its spine hop."""
    p = path_str.replace("\\", "/")
    if "/design/specs/" in p:
        return "refine"
    if "/design/plans/" in p:
        return "solution"
    if "/flows/" in p:
        return "flow"
    if "/code-repo/" in p:
        return "code"
    return None


def run_grep_chain(fx: dict) -> dict:
    uc = fx["uc"]
    hops: dict[str, list[str]] = {
        "refine": [],
        "solution": [],
        "code": [],
        "flow": [],
        "commit": [],
    }
    for line in _grep_r(SCRATCH, uc):
        path = line.split(":", 1)[0]
        hop = _classify_hop(path, fx)
        if hop:
            hops[hop].append(line)
    # commit hop: trailers are in git history, invisible to a working-tree grep.
    proc = _git(fx["code_repo"], "log", f"--grep=Use-Case: {uc}",
                "--pretty=format:%h %s", check_rc=False)
    hops["commit"] = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return hops


# --------------------------------------------------------------------------- #
# Liveness reconcile — reads CODE, never the plan (faithful to taxonomy Step 9b)
# --------------------------------------------------------------------------- #
_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_ROW_RE_TMPL = r"^\|\s*{uc}\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"


def _flow_source_paths(text: str) -> list[str]:
    m = _FM_RE.match(text)
    if not m:
        return []
    paths: list[str] = []
    in_block = False
    for raw in m.group(1).splitlines():
        if raw.startswith("source_paths:"):
            in_block = True
            continue
        if in_block:
            stripped = raw.strip()
            if stripped.startswith("- "):
                paths.append(stripped[2:].strip().strip('"').strip("'"))
            elif not raw.startswith(" ") and stripped:
                break
    return paths


def _chain_live(code_repo: Path, source_paths: list[str], uc: str) -> bool:
    """The activation chain is live iff a tracked source_path still carries it.

    Reads ONLY code (the file's presence + the activation anchor). It never
    consults the plan/registry — that is the whole point of the reconcile.
    """
    tracked = set(
        _git(code_repo, "ls-files", check_rc=False).stdout.splitlines()
    )
    for rel in source_paths:
        if rel not in tracked:
            continue
        f = code_repo / rel
        try:
            body = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if ACTIVATION_FN in body or uc in body:
            return True
    return False


def reconcile_flow_page(flow_page: Path, code_repo: Path, uc: str, to_sha: str) -> str:
    """Reconcile the flow page's per-UC Status against live code. Returns status."""
    text = flow_page.read_text(encoding="utf-8")
    live = _chain_live(code_repo, _flow_source_paths(text), uc)
    new_status = "completed" if live else "deprecated"

    row_re = re.compile(_ROW_RE_TMPL.format(uc=re.escape(uc)), re.MULTILINE)

    def _sub(m: re.Match) -> str:
        domain, _old_status, summary = m.group(1), m.group(2), m.group(3)
        return f"| {uc} | {domain} | {new_status} | {summary} |"

    text, n = row_re.subn(_sub, text)
    if n != 1:
        raise RuntimeError(f"expected exactly one ## Use Cases row for {uc}, patched {n}")

    # Stale banner on deprecation (idempotent — only add if absent).
    banner = (
        f"> [!stale] {uc} deprecated: activation chain removed as of {to_sha[:8]}.\n"
    )
    if new_status == "deprecated" and banner not in text:
        # place the banner immediately after the frontmatter block
        m = _FM_RE.match(text)
        insert_at = m.end() if m else 0
        text = text[:insert_at] + "\n" + banner + text[insert_at:]

    # Bump updated: on any touch (frontmatter stamp).
    text = re.sub(r"^updated: .*$", f"updated: {DATE}", text, count=1, flags=re.MULTILINE)
    flow_page.write_text(text, encoding="utf-8", newline="\n")
    return new_status


def _row_status(flow_page: Path, uc: str) -> str | None:
    text = flow_page.read_text(encoding="utf-8")
    m = re.search(_ROW_RE_TMPL.format(uc=re.escape(uc)), text, re.MULTILINE)
    return m.group(2).strip() if m else None


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #
def run_assertions(fx: dict, grep_hops: dict) -> None:
    uc = fx["uc"]

    # UC1 — mint + birth certificate
    reg = fx["project_dir"] / "design" / "use-cases" / "registry.jsonl"
    row = use_case_impact.read_registry_row(reg, uc)
    check(
        "UC1",
        use_case_impact.is_valid_uc_id(uc) and row is not None and row.get("epic") == EPIC_SLUG,
        f"minted opaque {uc}; birth-cert row present (epic={EPIC_SLUG!r}, origin={row.get('origin') if row else None!r})",
        f"mint/registry failed: uc={uc!r} row={row!r}",
    )

    # UC2..UC6 — every spine hop found by the grep chain
    hop_labels = {
        "refine": "refine functional-story block",
        "solution": "solution doc-of-record + task-frontmatter",
        "commit": "Use-Case: trailer (git log --grep)",
        "code": "activation-chain anchor in tracked source",
        "flow": "flow-page frontmatter + ## Use Cases row",
    }
    ids = {"refine": "UC2", "solution": "UC3", "commit": "UC4", "code": "UC5", "flow": "UC6"}
    for hop in ("refine", "solution", "commit", "code", "flow"):
        hits = grep_hops[hop]
        sample = hits[0].split(":", 3)[-1].strip() if hits and hop != "commit" else (hits[0] if hits else "")
        check(
            ids[hop],
            bool(hits),
            f"{hop} hop [{hop_labels[hop]}] — {len(hits)} hit(s): {sample[:88]!r}",
            f"{hop} hop MISSING — grep chain returned no {hop} touchpoint for {uc}",
        )

    # UC7 — the headline: one grep chain returns ALL FIVE hops
    covered = [h for h in ("refine", "solution", "commit", "code", "flow") if grep_hops[h]]
    check(
        "UC7",
        len(covered) == 5,
        f"grep -r {uc} (+ git log --grep) returns the FULL chain: {covered}",
        f"grep chain incomplete — only {covered} of 5 hops",
    )

    # UC8 — the impact tool locates the same floor across all five stages
    report = use_case_impact.find_touchpoints(
        uc,
        data_root=fx["data_root"],
        project_name=PROJECT_NAME,
        code_repo=fx["code_repo"],
        autodoc_root=fx["autodoc_root"],
    )
    stages_hit = {s for s in use_case_impact.STAGES if report.touchpoints[s]}
    check(
        "UC8",
        stages_hit == set(use_case_impact.STAGES) and report.registry_row is not None,
        f"use_case_impact floor = {report.total()} ref(s) across all stages "
        f"{sorted(stages_hit)}; header={uc} epic={report.registry_row.get('epic') if report.registry_row else None!r}",
        f"impact tool gap — stages hit {sorted(stages_hit)} != {sorted(use_case_impact.STAGES)}",
    )

    # UC9 — plan_parser threads use_cases into task frontmatter (additive field)
    plan = parse_plan(fx["project_dir"] / "design" / "plans" / "plan.md")
    threaded = plan.tasks[1].use_cases
    check(
        "UC9",
        threaded == [uc],
        f"plan_parser threaded task.use_cases == {threaded} (additive optional field)",
        f"task-frontmatter thread broken: task.use_cases == {threaded!r}, want [{uc!r}]",
    )

    # UC10 — flow-page use_cases is an OPAQUE scalar list (gate-inert, no wikilinks)
    fm = _FM_RE.match(fx["flow_page"].read_text(encoding="utf-8"))
    fm_text = fm.group(1) if fm else ""
    uc_block = re.search(r"use_cases:\n((?:\s*-\s.*\n?)+)", fm_text)
    block = uc_block.group(1) if uc_block else ""
    check(
        "UC10",
        uc in block and "[[" not in block and "]]" not in block,
        f"flow use_cases frontmatter carries {uc} as an opaque scalar (no [[wikilink]]) — inert to graph-check",
        f"flow use_cases is NOT gate-safe opaque-scalar: {block!r}",
    )


def run_reconcile(fx: dict) -> None:
    uc, code_repo, flow_page = fx["uc"], fx["code_repo"], fx["flow_page"]

    # Snapshot the plan-owned docs to prove reconcile never edits them.
    plan_md = fx["project_dir"] / "design" / "plans" / "plan.md"
    sol_md = fx["project_dir"] / "design" / "plans" / "uc-traceability.md"
    refine_md = fx["project_dir"] / "design" / "specs" / "uc-traceability-design.md"
    before = {p: _digest(p) for p in (plan_md, sol_md, refine_md)}

    # UC11 — reconcile with code PRESENT flips backlog -> completed (self-heal live)
    head = _git(code_repo, "rev-parse", "HEAD").stdout.strip()
    status_live = reconcile_flow_page(flow_page, code_repo, uc, head)
    check(
        "UC11",
        status_live == "completed" and _row_status(flow_page, uc) == "completed",
        f"code present -> reconcile set {uc} Status = completed (liveness protocol read the activation chain)",
        f"live reconcile wrong: returned {status_live!r}, row={_row_status(flow_page, uc)!r}",
    )

    # --- simulate code removal: git rm the activation chain, commit ---------- #
    _git(code_repo, "rm", "-q", "calc.py")
    _git(code_repo, "commit", "-q", "-m", "refactor: drop uc_login session flow")
    removal_sha = _git(code_repo, "rev-parse", "HEAD").stdout.strip()

    # UC12 — reconcile after removal flips completed -> deprecated + stale banner
    status_gone = reconcile_flow_page(flow_page, code_repo, uc, removal_sha)
    flow_text = flow_page.read_text(encoding="utf-8")
    has_banner = "> [!stale]" in flow_text and uc in flow_text.split("> [!stale]", 1)[1][:120]
    check(
        "UC12",
        status_gone == "deprecated"
        and _row_status(flow_page, uc) == "deprecated"
        and has_banner,
        f"code removed -> reconcile flipped {uc} Status = deprecated on the flow page + `> [!stale]` banner ({removal_sha[:8]})",
        f"deprecated flip failed: status={status_gone!r} row={_row_status(flow_page, uc)!r} banner={has_banner}",
    )

    # UC13 — reconcile touched ONLY the flow page: plan/solution/refine unchanged
    after = {p: _digest(p) for p in (plan_md, sol_md, refine_md)}
    unchanged = [p for p in before if before[p] == after[p]]
    check(
        "UC13",
        len(unchanged) == 3,
        "reconcile read code, never copied plan status: plan.md / solution / refine byte-identical after both passes",
        f"reconcile mutated plan-owned docs: changed={[p.name for p in before if before[p] != after[p]]}",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def main() -> int:
    print("# agentflow Phase-4 SHIP GATE — use-case traceability acceptance scenario (T11)")
    print(f"# scratch={SCRATCH}  (throwaway fixtures; live vault untouched)")
    fx = build_fixtures()
    uc = fx["uc"]
    print(f"# minted UC = {uc}  epic={EPIC_SLUG}  domain={DOMAIN}\n")

    grep_hops = run_grep_chain(fx)

    print(f"# ---- grep chain for {uc} (one id, every hop) --------------------------")
    for hop in ("refine", "solution", "commit", "code", "flow"):
        hits = grep_hops[hop]
        head = hits[0] if hits else "(none)"
        # Trim absolute scratch prefix for readability.
        head = head.replace(str(SCRATCH) + "\\", "").replace(str(SCRATCH) + "/", "")
        print(f"#   {hop:<9}: {len(hits)} hit(s) | {head}")
    print("# ----------------------------------------------------------------------\n")

    run_assertions(fx, grep_hops)
    run_reconcile(fx)

    npass = sum(1 for s, _, _ in _RESULTS if s == "PASS")
    nfail = sum(1 for s, _, _ in _RESULTS if s == "FAIL")
    for status, aid, detail in _RESULTS:
        print(f"{status} {aid}: {detail}")
    print(f"\n# summary: {npass} PASS  {nfail} FAIL  ({len(_RESULTS)} assertions)")
    if nfail == 0:
        print(f"# SHIP GATE: PASS — one `grep {uc}` walks refine -> solution -> commit -> code -> flow; "
              "code-removal + reconcile flipped the UC to deprecated without touching the plan.")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
