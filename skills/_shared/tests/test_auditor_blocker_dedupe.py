"""gh#721 / gh#723: the auditor's blocker dedupe, its pass cadences, its notify lines, and its
gap-slug uniqueness are rules the SKILL states, and the scenario driver applies them as stated.

`raise-blocker` mints a fresh id on every call, and Step 1 sub-step 3a returns every previously
blocked epic on every pass, so the dedupe has to live in the skill's Step 6 or one stuck epic adds
a dev-manager row per wake. The driver cannot run the skill; it reads the rule's match columns out
of SKILL.md and applies them over the same lock-free `registry read` the skill names. These tests
pin the prose, the parse, and the matcher to one another.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "harness" / "auditor-scenario" / "auditor_scenario.py"
SKILL = REPO_ROOT / "skills" / "auditor" / "SKILL.md"
NOTIFY = REPO_ROOT / "skills" / "auditor" / "references" / "notify-format.md"


def _load_driver():
    # FAIL, never skip: a moved driver must break this test, not retire it.
    assert DRIVER.is_file(), f"auditor driver not found at {DRIVER}"
    spec = importlib.util.spec_from_file_location("agentflow_auditor_scenario_gh721", DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def driver():
    return _load_driver()


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


#: Step 1's prose names "`## Step 2`" inline, so its section ends at the heading line itself.
STEP2 = "\n## Step 2 "


def _section(text: str, start: str, end: str) -> str:
    a = text.index(start)
    return text[a:text.index(end, a)]


# --------------------------------------------------------------------------- #
# gh#721 - Step 6 dedupe: prose
# --------------------------------------------------------------------------- #
def test_step6_dedupes_through_a_real_lock_free_read_verb():
    step6 = _section(_skill(), "## Step 6", "## Step 7")
    assert "python -m agentflow.registry read --project <name>" in step6
    assert "**skip the raise**" in step6
    # The table header the rule reads is the shipped template's, verbatim.
    template = (REPO_ROOT / "templates" / "registry.md").read_text(encoding="utf-8")
    header = "| id | raised_by | owner | work | state | description |"
    assert header in template and header in step6


def test_the_read_verb_the_skill_names_takes_no_selector_flag_it_does_not_have():
    """`read` with no selector is the verbatim dump; a `--blockers` flag does not exist."""
    from agentflow import registry

    parser = registry._build_parser()
    read = next(a for a in parser._subparsers._group_actions[0].choices.items() if a[0] == "read")[1]
    flags = {o for act in read._actions for o in act.option_strings}
    assert "--blockers" not in flags
    assert "--blockers" not in _section(_skill(), "## Step 6", "## Step 7")


def test_step3a_says_a_still_blocked_retry_raises_nothing_new():
    step1 = _section(_skill(), "## Step 1", STEP2)
    assert "A re-included epic that is still blocked raises nothing new." in step1


# --------------------------------------------------------------------------- #
# gh#721 - Step 6 dedupe: the driver applies the skill's rule
# --------------------------------------------------------------------------- #
def test_the_driver_parses_the_skills_match_columns(driver):
    rule = driver.skill_blocker_match_rule()
    assert set(rule) == {"state", "raised_by", "description"}
    assert set(rule) == driver._MATCHER_COLUMNS
    assert "`open`" in rule["state"] and "`auditor`" in rule["raised_by"]
    assert "`<slug>:`" in rule["description"]


def test_step6_does_not_match_on_owner_or_work():
    """dev-manager reassigns blocker rows, and an epic's E- id can change; neither is the epic."""
    step6 = _section(_skill(), "## Step 6", "## Step 7")
    assert "- `owner`" not in step6 and "- `work`" not in step6
    assert "assign-blocker --owner" in step6
    assert "--description '<slug>: <blocked_on>; tried: <tried>'" in step6


def test_a_skill_column_the_matcher_ignores_turns_the_gate_red(driver, monkeypatch):
    real = driver.skill_blocker_match_rule()
    monkeypatch.setattr(driver, "skill_blocker_match_rule",
                        lambda: {**real, "id": "is fresh"})
    with pytest.raises(RuntimeError, match="update blocker_matches"):
        driver._check_match_rule_columns()


def test_a_changed_condition_value_turns_the_gate_red(driver, monkeypatch):
    real = driver.skill_blocker_match_rule()
    monkeypatch.setattr(driver, "skill_blocker_match_rule",
                        lambda: {**real, "raised_by": "is `steward`"})
    with pytest.raises(RuntimeError, match="no longer carry the values"):
        driver._check_match_rule_columns()


def test_blocker_matches_is_the_three_column_rule(driver):
    from agentflow.registry import Blocker

    hit = Blocker("B-0001", "auditor", "dev-manager", "-", "open", "epic-x: no strategy; tried: y")
    assert driver.blocker_matches(hit, "epic-x")
    for field, value in (("state", "resolved"), ("raised_by", "lead-dev")):
        miss = Blocker(**{**hit.__dict__, field: value})
        assert not driver.blocker_matches(miss, "epic-x"), field
    # A reassigned row, or one whose work cell gained an E- id, is still the same blocker.
    for field, value in (("owner", "lead-dev"), ("work", "E-0012"), ("work", "E-0012-epic-x")):
        same = Blocker(**{**hit.__dict__, field: value})
        assert driver.blocker_matches(same, "epic-x"), field
    assert not driver.blocker_matches(hit, "epic-xy")       # prefix needs the colon
    assert not driver.blocker_matches(hit, "epic")


def test_the_driver_suppresses_a_second_raise_over_the_shipped_registry(driver, tmp_path):
    """The repro from the issue, through the shipped CLI: one open row, then nothing new."""
    lock_dir = tmp_path / "locks"
    driver._registry_ready(tmp_path, lock_dir)
    assert driver.open_auditor_blockers(tmp_path, lock_dir, "epic-noverify") == []
    rc, out, err = driver._reg(tmp_path, lock_dir, "raise-blocker", "--actor", "auditor",
                               "--owner", "dev-manager", "--work", "-",
                               "--description", "epic-noverify: no verification.strategy; tried: x")
    assert rc == 0, err or out
    first = driver.open_auditor_blockers(tmp_path, lock_dir, "epic-noverify")
    assert len(first) == 1
    # Control: the CLI itself is not idempotent, so the suppression is the rule's, not the CLI's.
    rc, out, err = driver._reg(tmp_path, lock_dir, "raise-blocker", "--actor", "auditor",
                               "--owner", "dev-manager", "--work", "-",
                               "--description", "epic-noverify: no verification.strategy; tried: x")
    assert rc == 0, err or out
    assert len(driver.open_auditor_blockers(tmp_path, lock_dir, "epic-noverify")) == 2


def test_a_reassigned_open_row_still_suppresses_the_raise(driver, tmp_path, monkeypatch):
    """dev-manager hands the auditor's row to another seat; the next pass must not re-raise it."""
    fx = driver.build_fixtures(tmp_path)
    data_root, lock_dir = fx["data_root"], tmp_path / "locks"
    canned = driver.canned_subagent_output(fx)
    only_blocked = {k: v for k, v in canned.items() if "blocker" in v}
    monkeypatch.setattr(driver, "discover", lambda _root, _window: sorted(only_blocked))
    driver.run_audit_pass(data_root, lock_dir, driver.Audit(), only_blocked)
    (bid,) = driver.open_auditor_blockers(data_root, lock_dir, "epic-noverify")
    rc, out, err = driver._reg(data_root, lock_dir, "assign-blocker", "--actor", "dev-manager",
                               "--id", bid, "--owner", "lead-dev")
    assert rc == 0, err or out
    assert driver._parse_reg(data_root).blockers[bid].owner == "lead-dev"
    before = set(driver._blocker_rows(data_root))
    driver.run_audit_pass(data_root, lock_dir, driver.Audit(), only_blocked)
    assert set(driver._blocker_rows(data_root)) == before
    assert driver.open_auditor_blockers(data_root, lock_dir, "epic-noverify") == [bid]


# --------------------------------------------------------------------------- #
# gh#721 - cadence and notify lines
# --------------------------------------------------------------------------- #
def test_normal_cadence_is_defined_as_a_number():
    text = _skill()
    assert "**`<normal>` is 60 seconds** (`delaySeconds=60`" in text
    assert "The normal cadence is **60 seconds**" in text


def test_blocked_only_pass_has_its_own_cadence():
    step7 = _section(_skill(), "## Step 7", "**Step 4b decides whether")
    assert "**Blocked-only pass" in step7
    assert "`audit.idle_wake_minutes * 60` in place of `<normal>`" in step7


BLOCKED_ONLY = "audited 0: <B> blocked, nothing judgeable (<slug>, ...)"


def test_notify_format_defines_the_blocked_only_line_and_the_blocked_count():
    notify = NOTIFY.read_text(encoding="utf-8")
    assert BLOCKED_ONLY in notify
    assert "`; <B> blocked`" in notify
    assert re.search(r"^audited 0: \d+ blocked, nothing judgeable \(", notify, re.M)
    assert re.search(r"^audited \d+: .*; \d+ blocked$", notify, re.M)


def test_step5_and_step1_carry_the_same_blocked_only_line():
    text = _skill()
    step5 = _section(text, "## Step 5", "## Step 6")
    step1 = _section(text, "## Step 1", STEP2)
    assert f"<name> auditor: {BLOCKED_ONLY}" in step5
    assert f"<name> auditor: {BLOCKED_ONLY}" in step1
    assert "`; <B> blocked`" in step5


def test_the_drivers_blocked_lines_have_the_notify_format_shape(driver, tmp_path, monkeypatch):
    """Run a pass whose window holds only the blocked epic, then one mixing judged and blocked."""
    fx = driver.build_fixtures(tmp_path)
    data_root, lock_dir = fx["data_root"], tmp_path / "locks"
    canned = driver.canned_subagent_output(fx)
    only_blocked = {k: v for k, v in canned.items() if "blocker" in v}
    monkeypatch.setattr(driver, "discover", lambda _root, _window: sorted(only_blocked))
    r = driver.run_audit_pass(data_root, lock_dir, driver.Audit(), only_blocked)
    assert r["summary"] == "audited 0: 1 blocked, nothing judgeable (epic-noverify)"

    monkeypatch.setattr(driver, "discover", lambda _root, _window: sorted(canned))
    r = driver.run_audit_pass(data_root, lock_dir, driver.Audit(), canned)
    assert r["summary"].endswith("; 1 blocked"), r["summary"]
    # The second pass re-judged the blocked epic and the dedupe held it to one row.
    assert len(driver.open_auditor_blockers(data_root, lock_dir, "epic-noverify")) == 1


# --------------------------------------------------------------------------- #
# gh#723 - gap slugs unique against every surveyed slug
# --------------------------------------------------------------------------- #
def test_step43_requires_project_wide_uniqueness_via_the_survey():
    step43 = _section(_skill(), "### 4.3", "### 4.4")
    assert "unique against every slug on disk across ALL projects on the data" in step43
    assert "env.data_root, \"1970-01-01T00:00:00+00:00\", include_archived=True)}" in step43
    assert "include_archived=True)}" in step43 and "project=<name>" not in step43
    assert "This is the one survey this loop runs project-wide." in step43
    assert "append `-2`, then `-3`" in step43
    assert "within a pass with a trailing token" not in step43


def _gap(driver, data_root: Path, date: str, slug: str, state: str) -> None:
    driver._write(driver._specs_dir(data_root) / f"{date}-{slug}.md",
                  f"---\nslug: {slug}\ntitle: {slug}\nproject: {driver.PROJECT_NAME}\n"
                  f"current_state: {state}\n---\n\nbody\n")


def test_unique_gap_slug_skips_an_older_done_gap(driver, tmp_path):
    assert driver.unique_gap_slug(tmp_path, "complete-epic-a") == "complete-epic-a"
    _gap(driver, tmp_path, "2026-06-01", "complete-epic-a", "done")
    assert driver.unique_gap_slug(tmp_path, "complete-epic-a") == "complete-epic-a-2"
    _gap(driver, tmp_path, "2026-06-15", "complete-epic-a-2", "drafted")
    assert driver.unique_gap_slug(tmp_path, "complete-epic-a") == "complete-epic-a-3"
    assert driver.unique_gap_slug(tmp_path, "complete-epic-a",
                                  {"complete-epic-a-3"}) == "complete-epic-a-4"


def test_a_reaudit_gap_is_not_shadowed_in_the_survey(driver, tmp_path):
    """End to end: the second gap for the same source epic surfaces as its own drafted record."""
    from agentflow.epic_survey import survey_epics

    _gap(driver, tmp_path, "2026-06-01", "complete-epic-a", "done")
    slug = driver.synthesize_gap_spec(
        tmp_path, "epic-a", "UC-0001",
        [{"criterion": "c", "verdict": "missed", "evidence": "e", "uc_id": "UC-0001"}])
    assert slug == "complete-epic-a-2"
    recs = {r.slug: r for r in survey_epics(tmp_path, "1970-01-01T00:00:00+00:00",
                                           project=driver.PROJECT_NAME)}
    assert recs["complete-epic-a"].lifecycle_state == "done"
    assert recs[slug].current_state == "drafted"


def test_unique_gap_slug_sees_a_done_spec_in_a_sibling_project(driver, tmp_path):
    """dev-manager surveys every project and merges same-slug records before filtering."""
    sibling = tmp_path / "projects" / "other-project" / "design" / "specs"
    driver._write(sibling / "2026-06-01-complete-epic-a.md",
                  "---\nslug: complete-epic-a\ntitle: t\nproject: other-project\n"
                  "current_state: done\n---\n\nbody\n")
    assert driver.unique_gap_slug(tmp_path, "complete-epic-a") == "complete-epic-a-2"
