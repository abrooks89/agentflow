"""gh#114: the audit window must bound UNAUDITED work, not the corpus.

`discover()` used to sort every done epic by its ended-proxy, truncate to `audit.window`, and
only then drop the ones the ledger says were already audited. An already-audited epic therefore
CONSUMED a slot, and an unaudited done epic below the cut was never returned. The pass then
reported "nothing new to audit" and idle-woke.

That failure is silent and self-concealing: an empty pass is the designed-healthy state, so
nothing reddens and nobody is paged. It is reachable rather than exotic because the ended proxy
is a file mtime -- any bulk rewrite re-dates the corpus and re-sorts it. The gh#111 backfill
rewrote 21 specs, four of them done and audited days earlier, onto one identical mtime above
every genuinely-recent epic; at `window = 5` that consumed four of five slots and the margin was
a single slot.

These tests are written in that incident's shape: N already-audited epics dated ABOVE one
unaudited epic, with N >= window.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "harness" / "auditor-scenario" / "auditor_scenario.py"


SILENCED_LOGGER = "agentflow.epic_survey"


def _load_driver(name: str = "agentflow_auditor_scenario_gh114"):
    """Import the auditor scenario driver by path -- it is a script, not a package module.

    NO save/restore of logging here, deliberately. The driver used to run
    `logging.getLogger("agentflow.epic_survey").setLevel(logging.ERROR)` at module scope, which
    reconfigured logging for the whole process: `test_epic_survey` asserts on WARNING records
    from that logger, so importing this file turned 53 unrelated tests red while every test in
    the importing module stayed green, and only on collection order.

    The fix belongs in the driver and now lives there (`_silence_survey_noise`, called from
    `main()`). A save/restore at this call site would cover this importer only -- and
    `test_never_halt_on_empty.py` already imports the same driver without one, so the landmine
    would still be armed for it and for importer three. Worse, restoring here would MASK a
    regression of the driver from the guard test below, which is the one thing that would name
    the cause.
    """
    # FAIL, never skip: if the driver moves, this gate must break loudly rather than quietly
    # stop covering the ordering it exists to pin.
    assert DRIVER.is_file(), f"auditor driver not found at {DRIVER} - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location(name, DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclasses resolve their module at class creation
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def driver():
    return _load_driver()


def _record(driver, slug: str, mtime: str):
    """A done EpicRecord, built from the REAL class so a field rename breaks this loudly."""
    from agentflow.epic_survey import EpicRecord

    return EpicRecord(slug=slug, title=slug, lifecycle_state="done", last_modified=mtime)


def _write_ledger(driver, data_root: Path, slugs) -> None:
    """Write a ledger naming `slugs`, then PROVE the driver's own parser reads it back.

    A hand-written fixture the parser cannot read would make every assertion below pass for the
    wrong reason -- the filter would find an empty audited set and the truncation order would
    stop mattering.
    """
    path = driver._ledger_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(f"| {s} | 2026-07-18 | 1 | 0 | 0 | 0 | - | - |\n" for s in slugs)
    path.write_text("| epic | date |\n| ------ | ------ |\n" + rows, encoding="utf-8")
    assert driver._ledger_audited_set(path) == set(slugs), (
        "the fixture ledger does not parse back to the slugs it names, so it discriminates "
        "nothing"
    )


def _patch_survey(driver, monkeypatch, records):
    monkeypatch.setattr(driver, "survey_epics", lambda *a, **k: list(records))


def test_an_unaudited_epic_survives_a_window_full_of_already_audited_ones(
    driver, monkeypatch, tmp_path
):
    """The gh#111 incident, reproduced: four re-dated audited epics above one unaudited epic.

    Under the old order this returned `[]` -- the four consumed every slot at `window = 4`, the
    pass reported nothing new, and the unaudited epic was invisible with nothing going red.
    """
    bulk = "2026-09-02T01:32:47"                       # the backfill's single identical mtime
    audited = [f"audited-{i}" for i in range(4)]
    records = [_record(driver, s, bulk) for s in audited]
    records.append(_record(driver, "shipped-but-unaudited", "2026-09-01T01:00:00"))
    _patch_survey(driver, monkeypatch, records)
    _write_ledger(driver, tmp_path, audited)

    assert driver.discover(tmp_path, window=4) == ["shipped-but-unaudited"]


def test_the_window_still_truncates_after_the_filter(driver, monkeypatch, tmp_path):
    """Filtering first must not turn the window into "everything".

    The bound is still a bound; it now bounds the unaudited set. Newest first, so a pass that can
    only take N takes the N that matter most.
    """
    records = [
        _record(driver, "old", "2026-09-01T00:00:00"),
        _record(driver, "newest", "2026-09-03T00:00:00"),
        _record(driver, "middle", "2026-09-02T00:00:00"),
    ]
    _patch_survey(driver, monkeypatch, records)
    _write_ledger(driver, tmp_path, [])

    assert driver.discover(tmp_path, window=2) == ["newest", "middle"]


def test_the_fixture_discriminates_the_ledger_is_what_changes_the_answer(
    driver, monkeypatch, tmp_path
):
    """A control: same records, empty ledger. Without it, the test above could pass vacuously.

    If the ledger were unreadable or ignored, this case and the first would return the same
    thing. They must not.
    """
    bulk = "2026-09-02T01:32:47"
    audited = [f"audited-{i}" for i in range(4)]
    records = [_record(driver, s, bulk) for s in audited]
    records.append(_record(driver, "shipped-but-unaudited", "2026-09-01T01:00:00"))
    _patch_survey(driver, monkeypatch, records)
    _write_ledger(driver, tmp_path, [])

    assert driver.discover(tmp_path, window=4) == audited, (
        "with nothing audited the window is the four newest, so the ledger really is what moves "
        "the answer in the test above"
    )


def test_a_missing_ledger_audits_everything_rather_than_nothing(driver, monkeypatch, tmp_path):
    """No ledger means nothing has been audited yet -- the empty set, never a crash or a skip.

    Reordering the filter ahead of the truncation moved this read earlier, so the missing-file
    branch now decides more than it used to.
    """
    records = [_record(driver, "only-one", "2026-09-03T00:00:00")]
    _patch_survey(driver, monkeypatch, records)
    assert not driver._ledger_path(tmp_path).exists()

    assert driver.discover(tmp_path, window=5) == ["only-one"]


def _write_state(driver, data_root: Path, blocked) -> None:
    """Write an auditor state file naming `blocked`, then PROVE the driver reads it back.

    Same discipline as `_write_ledger` above and for the same reason: a fixture the driver's own
    parser cannot read would make every assertion below pass for the wrong reason -- the blocked
    set would come back empty and the partition would stop mattering.
    """
    path = driver._state_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    driver.write_auditor_state(data_root, audited=[], blocked=list(blocked),
                               scheduled_wake=True, halt_to_human=False)
    assert driver._state_blocked_set(path) == set(blocked), (
        "the fixture state file does not parse back to the slugs it names, so it discriminates "
        "nothing"
    )


def test_a_blocked_epic_does_not_consume_a_window_slot(driver, monkeypatch, tmp_path):
    """gh#223, the starvation case: blocked epics sorted ABOVE the work that can be judged.

    A blocked epic gets NO ledger row by design (Step 6 -- no fabricated verdict), and the ledger
    is the sole dedupe, so it is permanently unaudited. Before this, the window bounded the whole
    unaudited set, so `window` blocked epics filled it on every pass and judgeable work below the
    cut was never returned. The pass then emitted the healthy-looking empty-pass line, which is
    the gh#114 signature arriving through a different door.

    Dated above the judgeable epic on purpose: that ordering is reachable rather than exotic,
    because the ended proxy is a file mtime and any bulk rewrite re-dates the corpus.
    """
    blocked = [f"blocked-{i}" for i in range(3)]
    records = [_record(driver, s, "2026-09-03T00:00:00") for s in blocked]
    records.append(_record(driver, "judgeable", "2026-09-01T00:00:00"))
    _patch_survey(driver, monkeypatch, records)
    _write_ledger(driver, tmp_path, [])
    _write_state(driver, tmp_path, blocked)

    got = driver.discover(tmp_path, window=3)

    assert "judgeable" in got, (
        f"discover returned {got!r}: three blocked epics consumed the whole window=3 budget and "
        "the one epic the auditor could actually judge fell below the cut. That is gh#223 -- the "
        "pass audits nothing, forever, while its notify line reads healthy."
    )
    assert got[:1] == ["judgeable"], (
        f"discover returned {got!r}: the judgeable epic must come FIRST, inside the window, with "
        "retries appended after it. A retry that sorts above judgeable work has only moved the "
        "starvation one place along."
    )


def test_a_blocked_epic_is_still_returned_for_retry(driver, monkeypatch, tmp_path):
    """The other direction, and the one that separates a fix from a quieter bug.

    "Blocked epics must not consume the window" is satisfied just as well by dropping them
    entirely -- and that would stop the auditor ever re-judging an epic whose blocker later
    clears, which is worse than the bug being fixed and even harder to see. The retry must
    survive OUTSIDE the budget, not be filtered away.
    """
    blocked = ["blocked-and-waiting"]
    records = [_record(driver, "blocked-and-waiting", "2026-09-03T00:00:00"),
               _record(driver, "judgeable", "2026-09-01T00:00:00")]
    _patch_survey(driver, monkeypatch, records)
    _write_ledger(driver, tmp_path, [])
    _write_state(driver, tmp_path, blocked)

    got = driver.discover(tmp_path, window=1)

    assert got == ["judgeable", "blocked-and-waiting"], (
        f"discover returned {got!r}. The judgeable epic must fill the window=1 budget and the "
        "blocked epic must still be returned behind it: a blocker clears, and the epic becomes "
        "judgeable, so dropping it would retire the retry silently."
    )


def test_the_state_fixture_discriminates_the_blocked_list_is_what_changes_the_answer(
    driver, monkeypatch, tmp_path
):
    """The control. Without it, both tests above could pass on a partition that ignores state.

    Identical records and an identical ledger; the ONLY difference is an empty blocked list. If
    the answer does not move, the assertions above are measuring the sort order rather than the
    blocked set, and `_state_blocked_set` could return a constant with everything still green.
    """
    records = [_record(driver, "would-be-blocked", "2026-09-03T00:00:00"),
               _record(driver, "judgeable", "2026-09-01T00:00:00")]
    _patch_survey(driver, monkeypatch, records)
    _write_ledger(driver, tmp_path, [])
    _write_state(driver, tmp_path, [])

    assert driver.discover(tmp_path, window=1) == ["would-be-blocked"], (
        "with an EMPTY blocked list the newest epic must take the single window slot by ordinary "
        "sort order. If it does not, the partition is firing on something other than the blocked "
        "set and the two tests above prove nothing about gh#223."
    )


def test_a_missing_state_file_blocks_nothing_rather_than_everything(
    driver, monkeypatch, tmp_path
):
    """A first pass has no previous blocked list, and that is ordinary rather than an error.

    Asserted because the missing-file branch is deliberately the same shape as
    `_ledger_audited_set`'s, which is the hazard: an unconditional `return set()` there reads
    exactly like this legitimate branch and would restore gh#223 with everything else green.
    """
    records = [_record(driver, "one", "2026-09-03T00:00:00"),
               _record(driver, "two", "2026-09-01T00:00:00")]
    _patch_survey(driver, monkeypatch, records)
    _write_ledger(driver, tmp_path, [])
    assert not driver._state_path(tmp_path).exists()

    assert driver.discover(tmp_path, window=2) == ["one", "two"]


def test_importing_the_driver_does_not_reconfigure_process_logging():
    """The driver must not silence a shared logger just by being imported.

    Measured across a FRESH import rather than read off ambient state, so the assertion is about
    what importing DOES and cannot be satisfied or broken by whatever ran before it. Reading the
    global level instead would make this test pass or fail on suite order -- the very property
    the bug had.

    If this fails, `test_epic_survey` is about to fail too, in a file with no connection to this
    one. This is the assertion that names the cause.
    """
    logger = logging.getLogger(SILENCED_LOGGER)
    before = logger.level
    _load_driver("agentflow_auditor_scenario_gh114_freshimport")
    assert logger.level == before, (
        f"importing the auditor driver changed {SILENCED_LOGGER} from {before} to "
        f"{logger.level} process-wide; test_epic_survey asserts on that logger's WARNING "
        "records and will fail on collection order alone. Keep the setLevel inside main()."
    )


# --------------------------------------------------------------------------- #
# The order is stated in five places. They have to agree.
# --------------------------------------------------------------------------- #
SKILL_MD = REPO_ROOT / "skills" / "auditor" / "SKILL.md"
REPORT_FORMAT_MD = REPO_ROOT / "skills" / "auditor" / "references" / "report-format.md"

# Each entry: (file, a substring that must be ABSENT, why it is wrong).
# The needles are the OLD order's shape, in the exact words each site used for it.
STALE_ORDER_SITES = [
    (SKILL_MD, "survey → done → window → filter already-audited",
     "the Step 1 heading, the first thing a reader meets"),
    (SKILL_MD, "sorted by ended-proxy desc → `audit.window` →",
     "the end-of-pass checklist -- the auditor's own self-certification"),
    (SKILL_MD, "- `audit.window` — last-N done epics to consider",
     "the audit.window definition, which says what the window bounds"),
    (REPORT_FORMAT_MD, "last N done epics",
     "the report Window gloss, which is user-facing output"),
]


def test_no_site_still_states_the_pre_gh114_order():
    """One transposition, six sites -- and the first pass moved only one of them.

    The body of Step 1 was fixed while the section HEADING above it, the end-of-pass CHECKLIST
    below it, the `audit.window` DEFINITION, the driver's call-site COMMENT and the report FORMAT
    all went on stating sort-then-truncate. The checklist was the worst of them: it is the
    auditor's own self-certification, so a lane that followed the corrected body would then tick a
    box asserting it had done the defect.

    Nothing in the repo bound those statements together, which is why one edit could leave five
    contradictions and a green suite. This is that binding.
    """
    offenders = [
        (path.name, needle, why)
        for path, needle, why in STALE_ORDER_SITES
        if needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, "these still describe the pre-gh#114 order:\n" + "\n".join(
        f"  {name}: {why}\n    still contains: {needle!r}" for name, needle, why in offenders
    )


def test_the_needles_still_describe_a_shape_the_files_could_have():
    """A guard against the guard: an absence test passes trivially once its needle goes stale.

    If someone rewords a site, the needle above stops matching anything and the assertion above
    becomes vacuous -- green forever, checking nothing. Each site must still exist and still talk
    about the ordering, so a reword is forced to come here and update the needle.
    """
    for path, _needle, why in STALE_ORDER_SITES:
        text = path.read_text(encoding="utf-8")
        assert "audit.window" in text, f"{path.name} no longer mentions audit.window ({why})"
    body = SKILL_MD.read_text(encoding="utf-8")
    assert "filter already-audited → window" in body, "the corrected heading is gone"
    assert "BEFORE any truncation" in body, "the corrected Step 1 body is gone"


def test_the_driver_and_the_skill_agree_on_the_order(driver):
    """The prose and the code that models it must not drift apart.

    They already had: `discover()` and its own docstring disagreed with each other on master,
    which is how the executable model shipped the bug the prose describes.
    """
    import inspect

    src = inspect.getsource(driver.discover)
    filter_at = src.index("_ledger_audited_set")
    window_at = src.index("[:window]")
    assert filter_at < window_at, (
        "discover() truncates before it filters -- the gh#114 defect. The ledger filter must "
        "run first so the window bounds UNAUDITED work."
    )
    assert "ledger filter -> sort -> window" in src or "ledger filter" in src.split('"""')[1], (
        "discover()'s docstring no longer states the order it implements"
    )
