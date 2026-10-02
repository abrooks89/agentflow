"""Fixed issues are closed by the auditor, never by the seat that built the fix: the prose pins.

p145 PR 2. `agentflow.issue_intake` ships the listing (`fix-claims`) and the mediated close
(`close`, which refuses builders, the steward and the fix's own builder). Nothing calls either
unless the skills say to, so this file pins the prose that wires them: the auditor's Step 4b,
the steward's closure-debt report, dev-manager §3.4's fixed row, the builders' never-close rule,
the merge-readiness lines for a red provenance gate, and PR-PROSE's citing and verdict sections.

WHY THESE FORMS. Every assertion is on a form that tells the cases apart - the command, the
section, the exact field spelling - never on a word about it. "closure" and "auditor" were
already scattered through these files before the wiring existed.

WHY SECTION-SCOPED. A file-wide substring passes with the line parked in a section no wake
reads. Each predicate slices the section the rule belongs to first.

POSITIVE CONTROLS. Each group's predicate is run on the pre-epic text, embedded below as quoted
lines (not read from git), and must fail there. A predicate that passes on the unwired text
pins nothing.

WHAT IT CANNOT PROVE. That a wake follows the prose, or that the listing and the close behave:
test_fix_claims.py covers the commands, test_pr_review_pointer_gate.py the CI check.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "skills"

FIX_CLAIMS_CMD = "python -m agentflow.issue_intake fix-claims --project <name>"
AUDITOR_CLOSE_CMD = "python -m agentflow.issue_intake close --project <name> --actor auditor"
DM_CLOSE_CMD = "python -m agentflow.issue_intake close --project <name> --actor dev-manager"
AUDITOR_STRIKE = "note --strike --actor auditor"
WITHDRAW_HEAD = "Agentflow [dev-manager]"
WITHDRAW_WORDS = "take an ask back"


def _read(rel: str) -> str:
    path = SKILLS / rel
    assert path.is_file(), f"{rel} not found - moved, or this pin's path is stale"
    return path.read_text(encoding="utf-8")


def _slice(text: str, start: str, end: str) -> str:
    """From `start` to the next `end` after it; empty when `start` is absent."""
    i = text.find(start)
    if i == -1:
        return ""
    j = text.find(end, i + len(start))
    return text[i:] if j == -1 else text[i:j]


# ── auditor ──────────────────────────────────────────────────────────────────

STEP4B = "## Step 4b — "


def _auditor_failures(text: str) -> list[str]:
    bad = []
    s4, s4b, s5 = text.find("## Step 4 "), text.find(STEP4B), text.find("## Step 5 ")
    if not (s4 != -1 and s4b != -1 and s5 != -1 and s4 < s4b < s5):
        bad.append("no `## Step 4b — ` between `## Step 4 ` and `## Step 5 `")
    step = _slice(text, STEP4B, "\n## Step 5 ")
    for needle in (FIX_CLAIMS_CMD, AUDITOR_CLOSE_CMD, "Verdict: routed", "close_failed",
                   AUDITOR_STRIKE, "PR-PROSE.md"):
        if needle not in step:
            bad.append(f"Step 4b lacks {needle!r}")
    if not _withdrawal_rule(step):
        bad.append(f"no Step 4b line carries both {WITHDRAW_HEAD!r} and {WITHDRAW_WORDS!r}")
    boundary = _slice(text, "LOAD-BEARING BOUNDARY", "\n\n")
    if "closures" not in boundary or "not a product write" not in boundary:
        bad.append("the read-only boundary does not list tracker closures as a non-product write")
    step7 = _slice(text, "## Step 7 — Wake or stop", "## The rate-limit recovery")
    if "Step 4b" not in _slice(step7, "Empty pass", "backoff helper"):
        bad.append("Step 7's empty-pass text does not name Step 4b")
    if "Step 4b" not in _slice(text, "## End-of-pass checklist", "\n## "):
        bad.append("the end-of-pass checklist does not name Step 4b")
    return bad


def _withdrawal_rule(section: str) -> bool:
    return any(WITHDRAW_HEAD in ln and WITHDRAW_WORDS in ln for ln in section.splitlines())


def test_auditor_runs_the_fixed_issue_pass():
    bad = _auditor_failures(_read("auditor/SKILL.md"))
    assert not bad, bad


def test_auditor_description_names_the_pass_and_stays_under_the_limit():
    text = _read("auditor/SKILL.md")
    desc = next(ln for ln in text.splitlines() if ln.startswith("description: "))
    assert "fixed-issue pass" in desc
    assert len(desc) - len("description: ") < 1024


#: The auditor before this epic: Step 4 ran straight into Step 5, and the boundary listed no
#: tracker write. Plus the superseded withdrawal wording, which let any comment by the shared
#: account (every builder's review comment included) set the scope of its own verification.
PRE_EPIC_AUDITOR = "\n".join([
    "> **Read-only against the product (LOAD-BEARING BOUNDARY).** The auditor never edits product code,",
    "> state**. Nothing else; the shared memory store is read-only to it (see **Memory**, below).",
    "",
    "## Step 4 — Gap synthesis (main loop, never a subagent)",
    "## Step 5 — Record + notify",
    "## Step 7 — Wake or stop",
    "**Empty pass (nothing new to audit):** send the empty notify line, then idle-wake. **Ask the",
    "backoff helper for the delay instead of taking `audit.idle_wake_minutes` straight:**",
    "## The rate-limit recovery guard (`CronCreate`) — a SECOND wake leg, not a replacement",
    "## End-of-pass checklist",
])
SUPERSEDED_WITHDRAWAL = "Only a comment by the authenticated account can take one back."


def test_positive_control_auditor():
    assert _auditor_failures(PRE_EPIC_AUDITOR)
    assert not _withdrawal_rule(PRE_EPIC_AUDITOR)
    assert not _withdrawal_rule(SUPERSEDED_WITHDRAWAL)


# ── steward ──────────────────────────────────────────────────────────────────

G_HEAD = "**G. Issue triage.**"


def _steward_failures(text: str) -> list[str]:
    bad = []
    g = _slice(text, G_HEAD, "\n## ")
    for needle in ("fix-claims", "closure debt"):
        if needle not in g:
            bad.append(f"domain G lacks {needle!r}")
    if "issue_intake close" in text:
        bad.append("the steward names the close command as a literal")
    if any("--actor steward" in ln and "close" in ln for ln in text.splitlines()):
        bad.append("a line carries `--actor steward` and `close`")
    for phrase in ("You close on your own approval", "you are the approver"):
        if phrase in text:
            bad.append(f"the steward still says {phrase!r}")
    return bad


def test_steward_reports_closure_debt_and_closes_nothing():
    bad = _steward_failures(_read("steward/SKILL.md"))
    assert not bad, bad


PRE_EPIC_STEWARD = "\n".join([
    "**G. Issue triage.** Open issues in `<canonical_repo.host_ref>` that are verifiably fixed (below),",
    "plus duplicates and issues whose premise the code has moved past. **Report the open count and its",
    "trend** — a backlog growing faster than it closes is itself a finding.",
    "",
    "## Closing issues — you are the approver",
    "",
    "**You close on your own approval; you do not queue closures for a human.** The check is not",
])


def test_positive_control_steward():
    bad = _steward_failures(PRE_EPIC_STEWARD)
    assert any("domain G lacks" in b for b in bad)
    assert any("approver" in b for b in bad)


# ── dev-manager §3.4 ─────────────────────────────────────────────────────────


def _dev_manager_failures(text: str) -> list[str]:
    bad = []
    section = _slice(text, "### 3.4 Issue intake", "### 3.5 ")
    rows = [ln for ln in section.splitlines() if ln.strip().startswith("| **fixed**")]
    if not rows or "auditor" not in rows[0]:
        bad.append("§3.4's table has no `**fixed**` row naming the auditor")
    if "unless steward-verified fixed" in section:
        bad.append("§3.4 still lets the steward close")
    if DM_CLOSE_CMD not in section:
        bad.append("§3.4 does not route dev-manager's close through the close command")
    return bad


def test_dev_manager_has_a_fixed_row_and_closes_only_through_the_command():
    bad = _dev_manager_failures(_read("dev-manager/SKILL.md"))
    assert not bad, bad


PRE_EPIC_DEV_MANAGER = "\n".join([
    "### 3.4 Issue intake (when `work_source.bug_triage` is true)",
    "   | **close** — duplicate / won't-fix / already shipped | **human** (CARDINAL RULE) unless steward-verified fixed | comment reason, close |",
    "### 3.5 Feature-suggestion intake (when suggestions are pending)",
])


def test_positive_control_dev_manager():
    assert len(_dev_manager_failures(PRE_EPIC_DEV_MANAGER)) == 3


# ── lead-dev and the orchestrator ────────────────────────────────────────────


def _lead_dev_failures(text: str) -> list[str]:
    bad = []
    s25 = _slice(text, "## 2.5 Ground before build", "## 2.6 ")
    for needle in ("--to auditor", "never closes an issue", "closing\nkeyword", "PR title",
                   "Verdict: remainder"):
        if needle not in s25 and needle.replace("\n", " ") not in s25:
            bad.append(f"§2.5 lacks {needle!r}")
    preflight = _slice(text, "THE MERGE PREFLIGHT IS THREE GUARDS", "1. **List what the human")
    for needle in ("PR provenance gate", "not merge-ready"):
        if needle not in preflight:
            bad.append(f"§4's merge preflight lacks {needle!r}")
    return bad


def test_lead_dev_builders_never_close_and_a_red_gate_is_not_merge_ready():
    bad = _lead_dev_failures(_read("lead-dev/SKILL.md"))
    assert not bad, bad


def _orchestrator_failures(text: str) -> list[str]:
    s7d = _slice(text, "**7d —", "- **`hold`:**")
    return [n for n in ("PR provenance gate", "not merge-ready") if n not in s7d]


def test_orchestrator_7d_treats_a_red_provenance_gate_as_not_merge_ready():
    bad = _orchestrator_failures(_read("orchestrator/SKILL.md"))
    assert not bad, bad


PRE_EPIC_LEAD_DEV = "\n".join([
    "## 2.5 Ground before build — canonical repo + premise probe (MANDATORY, every item)",
    "Never \"build around\" a broken premise unattended.",
    "## 2.6 Publish the Queue — disjoint jr-dev lanes (every firing, after selection + grounding)",
    "**THE MERGE PREFLIGHT IS THREE GUARDS WITH THREE DIFFERENT PLACEMENTS, and the placement follows",
    "step — which is why \"run these checks\" is not enough. In this order:",
    "1. **List what the human is standing on** — `python -m agentflow.registry preflight --actor",
])
PRE_EPIC_ORCHESTRATOR_7D = "\n".join([
    "   **7d — On next wake or immediately after button click (state.status == \"awaiting_merge_decision\")**",
    "     - **A red required check** follows `skills/lead-dev/SKILL.md` §4 *Re-running CI*: re-run with",
    "   - **`hold`:**",
])


def test_positive_control_lead_dev_and_orchestrator():
    assert len(_lead_dev_failures(PRE_EPIC_LEAD_DEV)) == 7
    assert len(_orchestrator_failures(PRE_EPIC_ORCHESTRATOR_7D)) == 2


def test_juniors_never_close():
    for seat in ("jr-dev-1", "jr-dev-2", "jr-dev-3"):
        bullet = _slice(_read(f"{seat}/SKILL.md"), "- **Never generate work:**", "\n- **")
        assert "no issue closure" in bullet, seat


# ── PR-PROSE ─────────────────────────────────────────────────────────────────


def _pr_prose_failures(text: str) -> list[str]:
    bad = []
    verdict = _slice(text, "## Fixed-issue verdict comment", "\n## ")
    for needle in ("Checked against:", "Verdict: fixed", "Verdict: remainder", "Verdict: routed",
                   "Routed:"):
        if needle not in verdict:
            bad.append(f"the verdict-comment section does not define {needle!r}")
    citing = _slice(text, "## Citing an issue", "\n## ")
    for needle in ("Source: gh#N", "closing keyword", "PR title", "commit message"):
        if needle not in citing:
            bad.append(f"`## Citing an issue` lacks {needle!r}")
    exemption = _slice(text, "- **The provenance line**", "\n- **")
    if not exemption or "(`Source:`, `Epic:`, `Closes #N`)" in exemption or "fork" not in exemption:
        bad.append("the provenance-line exemption still offers `Closes #N` to loops")
    return bad


def test_pr_prose_defines_citing_and_the_verdict_comment():
    bad = _pr_prose_failures(_read("_shared/PR-PROSE.md"))
    assert not bad, bad


PRE_EPIC_PR_PROSE = "\n".join([
    "- **The provenance line** (`Source:`, `Epic:`, `Closes #N`). The `pr-provenance-gate` job in",
    "  `.github/workflows/ci.yml` hard-fails a body carrying none, so counting it against a budget",
    "- **The review pointer** (`Review:`, below) - same gate, same reason.",
    "## Review pointer",
])


def test_positive_control_pr_prose():
    assert len(_pr_prose_failures(PRE_EPIC_PR_PROSE)) == 10
