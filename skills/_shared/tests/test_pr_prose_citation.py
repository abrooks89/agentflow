"""Every skill that publishes to the git host cites PR-PROSE.md.

gh#207: `PR-PROSE.md` is the single statement of the attribution line, the prose cap and the
session-URL prohibition, and it binds every seat that writes to the host. The steward files host
issues and prescribes their bodies, yet its SKILL.md never cited the file - so the one seat the
rule was written for could not find it on its own reading path. PR-PROSE.md itself carried a
"known uncovered site" paragraph admitting the gap.

WHAT IT CAN PROVE
* The set of host-authoring skills is DERIVED from the SKILL.md text (any skill naming a host
  write verb), not kept as a hand list, so a new authoring seat that forgets the citation goes
  red without anyone editing this file.
* Each derived skill names `PR-PROSE.md` somewhere, and PR-PROSE.md no longer admits an
  uncovered site.

WHAT IT CANNOT PROVE
* That the citation sits next to the write it governs, or that a loop reading it follows it.
  A pass means the pointer exists, not that the prose it points at is obeyed.
* A skill that writes to the host through a verb not listed below (a raw `gh api` POST, say) is
  outside the derivation. The floor assertion below stops the set going silently empty; it does
  not make the verb list complete.
"""

from __future__ import annotations

from pathlib import Path

from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parent
PR_PROSE = SKILLS_ROOT / "_shared" / "PR-PROSE.md"

# The host write verbs as the skills spell them (`<git_host.cli> issue create ...`).
AUTHORING_VERBS = (
    "issue create",
    "pr create",
    "issue comment",
    "pr comment",
    "issue edit",
    "pr edit",
    "issue close",
)


def _authoring_skills() -> dict[str, Path]:
    # Tracked files only: an untracked scratch SKILL.md in a checkout must not colour the result.
    corpus = only_tracked(REPO_ROOT, sorted(SKILLS_ROOT.glob("*/SKILL.md")))
    return {
        p.parent.name: p
        for p in corpus
        if any(verb in p.read_text(encoding="utf-8") for verb in AUTHORING_VERBS)
    }


def test_derived_authoring_set_has_a_floor():
    # Without this, a derivation that matches nothing (a renamed verb, a moved tree) passes
    # the citation test vacuously.
    skills = _authoring_skills()
    assert skills, "no SKILL.md names a host write verb; the derivation is broken"
    # polish-mode joined via gh#540: it files host issues, and until it named the verb the
    # derivation could not see it.
    # auditor joined via p145: its fixed-issue pass comments on and closes issues.
    missing = {"lead-dev", "steward", "polish-mode", "auditor"} - set(skills)
    assert not missing, f"derivation lost known host-authoring skills: {sorted(missing)}"


def test_every_authoring_skill_cites_pr_prose():
    uncited = sorted(
        name
        for name, path in _authoring_skills().items()
        if "PR-PROSE.md" not in path.read_text(encoding="utf-8")
    )
    assert not uncited, (
        f"host-authoring skill(s) {uncited} never cite skills/_shared/PR-PROSE.md, the single "
        "statement of attribution, prose cap and session-URL prohibition"
    )


def test_pr_prose_admits_no_uncovered_site():
    assert "known uncovered site" not in PR_PROSE.read_text(encoding="utf-8")
