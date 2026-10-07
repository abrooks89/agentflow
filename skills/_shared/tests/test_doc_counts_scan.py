"""assert_doc_counts' SCAN layer must actually see a sixth location (gh#139).

`assert_doc_counts.py` has two layers on purpose. KNOWN pins the four files we
know state the wired-assertion count. SCAN exists for the file nobody listed --
its docstring: "any OTHER line that talks about wired assertions must not state
a stale number -- that catches a sixth location added later, which a fixed list
never would."

SCAN could not do that job. Its third alternative required a literal `(`,
because it was written verbatim against `harness/README.md`'s parenthesised
"(43 wired assertions," and no alternative covered the bold row shape
`**43 wired assertions**` that `README.md` uses. `main()` skips every file in
KNOWN, so the one file carrying that shape was never scanned and the blindness
was invisible: a new, unlisted doc copying the README row stated a stale count
and the gate passed.

The tests build a THROWAWAY git repo and point `assert_doc_counts.REPO` at it.
That is not fastidiousness: `scan_candidates()` reads the git index and nothing
else, so a probe written into the live checkout is
untracked and invisible to SCAN no matter what the pattern says -- a naive test
would pass before and after and prove nothing. `git add -N` in the live checkout
would work, but leaves an index entry behind if the test dies mid-run.

The three controls are load-bearing. The cheapest wrong fix here is a bare
`(\\d+) assertions`, which reads `harness/uc-scenario/README.md`'s "all 13
assertions PASS" -- a DIFFERENT registry's true count -- as a stale statement of
this one, turning the gate red on correct prose.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"

#: Copied into the throwaway repo at their real relative paths so KNOWN_COUNT
#: and KNOWN_RANGES resolve there exactly as they do in the checkout.
MIRRORED = (
    "README.md",
    "harness/README.md",
    "docs/ARCHITECTURE.md",
    "harness/auditor-scenario/README.md",
)

PROBE_REL = "docs/sixth-location.md"


def _load_adc():
    """Import harness/assert_doc_counts.py by path — it is a script, not a module."""
    path = HARNESS_DIR / "assert_doc_counts.py"
    assert path.is_file(), f"{path} not found - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location("agentflow_assert_doc_counts", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def adc():
    return _load_adc()


def _run_gate_over_mirror(adc, tmp_path: Path, probe_text: str | None) -> int:
    """Mirror the count-stating docs into a fresh git repo, add `probe_text` as an
    UNLISTED file, and run the gate against that tree."""
    assert shutil.which("git"), "git is required: scan_candidates() scopes by `git ls-files`"
    mirror = tmp_path / "mirror"
    for rel in MIRRORED:
        src, dst = REPO_ROOT / rel, mirror / rel
        assert src.is_file(), f"{rel} missing from the checkout - MIRRORED is stale"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    if probe_text is not None:
        probe = mirror / PROBE_REL
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(probe_text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=mirror, check=True)
    # add only, never commit: `git ls-files` reads the index, so no identity is
    # needed and the mirror stays a single throwaway directory.
    subprocess.run(["git", "add", "-A"], cwd=mirror, check=True)

    original = adc.REPO
    adc.REPO = mirror
    try:
        return adc.main()
    finally:
        adc.REPO = original


def test_the_mirror_itself_is_clean_with_no_probe(adc, tmp_path, capsys) -> None:
    """If the mirror were already red, every case below would prove nothing."""
    rc = _run_gate_over_mirror(adc, tmp_path, None)
    assert rc == 0, capsys.readouterr().out


def test_an_unlisted_file_stating_a_stale_count_is_caught(adc, tmp_path, capsys) -> None:
    """The sixth-location case SCAN exists for, in the shape README.md uses.

    The `41` below is deliberately stale — it has to be, or there is nothing for the
    gate to catch — and it survives in a tracked file only because SCAN is scoped to
    `*.md` (`assert_doc_counts.SCAN_SUFFIX`, filtered over the git index),
    so this `.py` module is never a scan candidate. That dependence is stated here
    rather than left implicit (gh#218): widening SCAN to `*.py` makes this literal a
    finding against the live checkout and self-reds the gate. If you widen it, move
    the stale number out of module scope — build it at call time from
    `check.EXPECTED_IDS` — instead of editing it to the current count, which would
    delete the assertion.
    """
    rc = _run_gate_over_mirror(adc, tmp_path, "| harness | **41 wired assertions** | x |\n")
    out = capsys.readouterr().out
    assert rc == 1, f"gate passed an unlisted doc stating 41: {out}"
    assert PROBE_REL in out and "41" in out


def test_an_unlisted_file_stating_the_correct_count_is_not_flagged(adc, tmp_path, capsys) -> None:
    """Control: SCAN reports staleness, not the mere mention of a count.

    The count is derived, not typed: a literal here went stale the moment C15 was wired.
    """
    rc = _run_gate_over_mirror(
        adc, tmp_path, f"| harness | **{adc.expected_count()} wired assertions** | x |\n"
    )
    assert rc == 0, capsys.readouterr().out


def test_another_registrys_count_is_not_read_as_this_one(adc, tmp_path, capsys) -> None:
    """Control: harness/uc-scenario/README.md's real line. A bare `(\\d+) assertions`
    alternative would turn correct prose red here."""
    rc = _run_gate_over_mirror(adc, tmp_path, "# exits 0 iff all 13 assertions PASS\n")
    assert rc == 0, capsys.readouterr().out


def test_id_range_prose_is_not_read_as_a_count(adc, tmp_path, capsys) -> None:
    """Control: run-scenario.md and harness/README.md name id ranges constantly."""
    rc = _run_gate_over_mirror(adc, tmp_path, "covers assertions A1-A9, C1-C14 and F1-F4\n")
    assert rc == 0, capsys.readouterr().out
