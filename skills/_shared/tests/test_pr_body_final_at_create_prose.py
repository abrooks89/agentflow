"""The PR body is final at `gh pr create`, and a red check is re-run with `--failed` only.

WHY THIS FILE EXISTS. `.github/workflows/ci.yml` runs every job on the `edited` event, so each
`gh pr edit` re-runs the full matrix on a SHA that was just tested. On 2026-09-24, 70 of the 92
same-SHA PR re-runs in 30 days came from one habit: a lane created the PR, posted its review as a
comment ON the PR, then edited the `Review:` pointer into the body. The review already existed
before create, so the fix is prose: post the review on the cited issue first, create the PR with
the pointer in place, skip a body edit that changes nothing, and re-run only failed jobs.

WHAT THIS FILE CAN AND CANNOT PROVE -- it pins the TEXT, not the behaviour.

* It CAN prove the rule is written where each loop reads it: PR-PROSE.md's *Review pointer*,
  lead-dev §4 (which the juniors inherit verbatim), orchestrator steps 7b and 7d, and the
  reader-facing copies in CLAUDE.md, the PR template and the install docs.
* Two checks are DERIVED from the whole tracked SKILL.md corpus rather than a hand list: every
  `pr edit ... --body` site has `unchanged` nearby, and every `run rerun` line names `--failed`.
  Each has a floor, so an empty corpus fails instead of passing vacuously.
* It CANNOT prove a loop follows the text, that the posted comment URL is right, or that CI
  runs once per head SHA. The PR's own dogfood check (one run per head SHA) is the behavioural
  evidence, and it needs Actions to be running jobs.

Corpora are tracked files only, via `agentflow.tracked_paths.only_tracked`, so an untracked
scratch file in a checkout cannot move a verdict.
"""

from __future__ import annotations

from pathlib import Path

from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parent

PR_PROSE = SKILLS_ROOT / "_shared" / "PR-PROSE.md"
LEAD_DEV = SKILLS_ROOT / "lead-dev" / "SKILL.md"
ORCHESTRATOR = SKILLS_ROOT / "orchestrator" / "SKILL.md"
JUNIORS = tuple(SKILLS_ROOT / f"jr-dev-{n}" / "SKILL.md" for n in (1, 2, 3))
INSTALL_SKILL = REPO_ROOT / "install" / "SKILL.md"
INSTALL_DOC = REPO_ROOT / "docs" / "INSTALL.md"
REPO_CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
PR_TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"

# How far from a `pr edit ... --body` line the word `unchanged` may sit.
UNCHANGED_WINDOW = 10


def _tracked(*paths: Path) -> list[Path]:
    kept = only_tracked(REPO_ROOT, paths)
    missing = sorted(str(p.relative_to(REPO_ROOT)) for p in paths if p not in kept)
    assert not missing, f"not tracked, so not in the corpus: {missing}"
    return kept


def _read(path: Path) -> str:
    (tracked,) = _tracked(path)
    return tracked.read_text(encoding="utf-8")


def _section(path: Path, start: str, end: str) -> str:
    text = _read(path)
    i = text.find(start)
    assert i != -1, f"{path.name}: start marker {start!r} not found"
    j = text.find(end, i + len(start))
    assert j != -1, f"{path.name}: end marker {end!r} not found after {start!r}"
    return text[i:j]


def _review_pointer() -> str:
    return _section(PR_PROSE, "## Review pointer", "\n## ")


def _lead_dev_s4() -> str:
    return _section(LEAD_DEV, "## 4. Execute via /project", "## 5. ")


def _orch_7b() -> str:
    return _section(ORCHESTRATOR, "**7b —", "**7c —")


def _orch_7d() -> str:
    return _section(ORCHESTRATOR, "**7d —", "- **`hold`:**")


def _skill_corpus() -> list[Path]:
    corpus = only_tracked(REPO_ROOT, sorted(SKILLS_ROOT.glob("*/SKILL.md")))
    assert corpus, "no tracked skills/*/SKILL.md; the derivation is broken"
    return corpus


# ---------------------------------------------------------------------------
# PR-PROSE.md *Review pointer*
# ---------------------------------------------------------------------------


def test_pr_prose_writes_the_pointer_at_create():
    s = _review_pointer()
    assert "gh pr create" in s
    assert "issue comment" in s
    assert "issues/<n>#issuecomment-<id>" in s
    assert "pull/<n>#issuecomment" not in s


def test_pr_prose_rules_out_a_post_create_pointer_edit():
    s = _review_pointer()
    assert "gh pr edit" in s
    assert "edited" in s
    assert "never adds or re-points" in s


def test_pr_prose_names_the_no_issue_fallback():
    s = _review_pointer()
    assert "cites no issue" in s
    assert "Review: none" in s


# ---------------------------------------------------------------------------
# lead-dev §4 and orchestrator 7b
# ---------------------------------------------------------------------------


def test_lead_dev_posts_the_review_on_the_issue_before_create():
    s = _lead_dev_s4()
    assert "issue comment" in s
    assert "gh pr create" in s
    assert "pr comment <n>" not in s


def test_orchestrator_7b_carries_a_review_line():
    s = _orch_7b()
    assert "Review:" in s
    assert "Review: none" in s
    assert "PR-PROSE.md" in s


def test_orchestrator_7b_skips_an_unchanged_edit():
    s = _orch_7b()
    assert "--json title,body" in s
    assert "unchanged" in s
    assert "pr edit skipped" in s


def test_every_body_edit_site_checks_for_unchanged():
    sites: list[str] = []
    bad: list[str] = []
    for path in _skill_corpus():
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "pr edit" in line and "--body" in line:
                where = f"{path.relative_to(REPO_ROOT).as_posix()}:{i + 1}"
                sites.append(where)
                lo, hi = max(0, i - UNCHANGED_WINDOW), i + UNCHANGED_WINDOW + 1
                if not any("unchanged" in ln for ln in lines[lo:hi]):
                    bad.append(where)
    assert sites, "no `pr edit ... --body` line in any tracked SKILL.md; the derivation is broken"
    assert not bad, (
        f"body-edit site(s) with no `unchanged` check within {UNCHANGED_WINDOW} lines: {bad}. "
        "Every `gh pr edit` fires `edited` and re-runs every CI job."
    )


# ---------------------------------------------------------------------------
# Re-running CI
# ---------------------------------------------------------------------------


def test_lead_dev_states_the_rerun_rule():
    s = _lead_dev_s4()
    for needle in (
        "Re-running CI",
        "gh run rerun",
        "--failed",
        "0 steps",
        "billing",
        "PR provenance gate",
        "sanitize grep gate",
    ):
        assert needle in s, f"lead-dev §4 lacks {needle!r}"


def test_orchestrator_7d_cites_the_rerun_rule():
    s = _orch_7d()
    assert "Re-running CI" in s
    assert "--failed" in s


def test_every_run_rerun_names_failed():
    hits: list[str] = []
    bad: list[str] = []
    for path in _skill_corpus():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if "run rerun" in line:
                where = f"{path.relative_to(REPO_ROOT).as_posix()}:{i + 1}"
                hits.append(where)
                if "--failed" not in line:
                    bad.append(where)
    assert hits, "no `run rerun` line in any tracked SKILL.md; this check would pass vacuously"
    assert not bad, f"`run rerun` without `--failed` on the same line: {bad}"


def test_juniors_inherit_lead_dev():
    # Regression pin, green on the base by design: the rule lives once, in lead-dev §4.
    for path in _tracked(*JUNIORS):
        text = path.read_text(encoding="utf-8")
        assert "skills/lead-dev/SKILL.md" in text, path.parent.name
        assert "Follow it verbatim" in text, path.parent.name
        own = [ln for ln in text.splitlines() if "run rerun" in ln]
        assert not own, f"{path.parent.name} carries its own re-run text: {own}"


# ---------------------------------------------------------------------------
# Reader-facing copies
# ---------------------------------------------------------------------------


def test_install_docs_name_the_issue_comment():
    for path in _tracked(INSTALL_SKILL, INSTALL_DOC):
        text = path.read_text(encoding="utf-8")
        assert "pr comment" not in text, path.relative_to(REPO_ROOT).as_posix()
        assert "issue comment" in text, path.relative_to(REPO_ROOT).as_posix()


def test_repo_copies_use_the_issue_url():
    for path in _tracked(REPO_CLAUDE_MD, PR_TEMPLATE):
        text = path.read_text(encoding="utf-8")
        assert "issues/<n>#issuecomment-<id>" in text, path.relative_to(REPO_ROOT).as_posix()
        assert "pull/<n>#issuecomment" not in text, path.relative_to(REPO_ROOT).as_posix()
