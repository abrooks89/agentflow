"""subagent_contract.py — Component 1: EvidenceBlock, coerce_review_verdict, build_enriched_redispatch_prompt.

Spec reference: wave-subagent-silent-failure-hardening / Component 1.

Provides:
- EvidenceBlock: structured evidence a wave subagent must attach to its JSON return
- ChecklistItem: one item on the verifier's acceptance checklist
- coerce_review_verdict: demotes an untrustworthy 'clean' verdict to 'inconclusive' or 'blocker'
- build_enriched_redispatch_prompt: assembles a fresh-dispatch prompt with prior reviewer findings

Demotion rules (pure, never mutates input):
  1. verdict='clean' AND evidence missing or stdout_tail empty  → 'inconclusive'
  2. verdict='clean' AND any ChecklistItem.verified is False    → 'blocker'
  3. verdict='clean' AND checklist is empty list                → 'inconclusive'
  (Rules are evaluated in the order above; first match wins.)
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

# Demotion-rule constants — one string per reason, for audit trail clarity.
_REASON_EVIDENCE_MISSING = "evidence missing — verdict downgraded to inconclusive"
_REASON_STDOUT_EMPTY = "stdout_tail is empty — cannot confirm test run succeeded; verdict downgraded to inconclusive"
_REASON_CHECKLIST_EMPTY = "checklist is empty — no acceptance criteria verified; verdict downgraded to inconclusive"
_REASON_CHECKLIST_UNVERIFIED = "one or more checklist items are unverified (verified=False); verdict downgraded to blocker"


@dataclass
class EvidenceBlock:
    """Structured evidence a wave subagent attaches to prove its work is real.

    command_run and stdout_tail are required; the rest have safe defaults so
    partial construction never raises TypeError.
    """

    command_run: str
    stdout_tail: str
    exit_code: int = 0
    git_log_oneline_branch: str = ""
    diff_stat: str = ""


@dataclass
class ChecklistItem:
    """One item on the verifier's acceptance checklist."""

    item: str
    verified: bool
    how: str


# ---------------------------------------------------------------------------
# coerce_review_verdict
# ---------------------------------------------------------------------------


def coerce_review_verdict(raw: dict) -> tuple[str, str]:
    """Demote an untrustworthy 'clean' verdict.

    Args:
        raw: The raw verdict dict from a verifier/reviewer subagent.
             Expected keys: 'verdict' (str), optionally 'evidence' (EvidenceBlock),
             optionally 'checklist' (list[ChecklistItem]).

    Returns:
        (new_verdict, audit_reason) — a tuple of two strings.  If no demotion
        applies, audit_reason is an empty string and new_verdict equals raw['verdict'].

    This function is pure: it never mutates ``raw``.
    """
    verdict: str = raw.get("verdict", "")

    # Only clean verdicts are candidates for demotion.
    if verdict != "clean":
        return verdict, ""

    # Rule 1: evidence missing entirely.
    evidence: EvidenceBlock | None = raw.get("evidence")
    if evidence is None:
        return "inconclusive", _REASON_EVIDENCE_MISSING

    # Rule 1 (continued): evidence present but stdout_tail is empty.
    if not evidence.stdout_tail:
        return "inconclusive", _REASON_STDOUT_EMPTY

    # Rule 2: any checklist item is unverified (checked before empty-list rule
    # so an all-False non-empty list → blocker, not inconclusive).
    checklist: list[ChecklistItem] | None = raw.get("checklist")
    if checklist is not None and any(not ci.verified for ci in checklist):
        return "blocker", _REASON_CHECKLIST_UNVERIFIED

    # Rule 3: checklist present but empty.
    if checklist is not None and len(checklist) == 0:
        return "inconclusive", _REASON_CHECKLIST_EMPTY

    # Happy path — nothing to demote.
    return "clean", ""


# ---------------------------------------------------------------------------
# build_enriched_redispatch_prompt
# ---------------------------------------------------------------------------


def build_enriched_redispatch_prompt(
    original_prompt: str,
    reviewer_findings: list[str],
) -> str:
    """Assemble a fresh-dispatch prompt that includes prior reviewer findings.

    The returned string always contains the exact header
    '## Prior reviewer feedback (must address)' so that the re-dispatched
    subagent cannot miss the findings from the previous attempt.

    Args:
        original_prompt: The original task prompt verbatim.
        reviewer_findings: List of finding strings from the prior review.

    Returns:
        A combined prompt string ready to be passed to a new subagent dispatch.
    """
    findings_block = "\n".join(f"- {f}" for f in reviewer_findings) if reviewer_findings else "_none_"

    return (
        f"{original_prompt}\n\n"
        f"## Prior reviewer feedback (must address)\n\n"
        f"{findings_block}\n"
    )
