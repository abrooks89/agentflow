"""autodoc-update reports its stale community-report count instead of a fake gate (gh#698).

`graph reports-stale` exits 1 on an error but 0 whatever it counts, and prints JSON. Step 4.5
used to run it as `... || exit 1` and discard the output, so the step read as a gate on the stale
COUNT, which that exit status can never be, and nobody ever saw the count. It now keeps the JSON
in the run directory, still stops on a crash, fails when the output is not the expected
`{"stale": [...], "count": N}` object, prints `N stale community reports`, and carries the count
to the Step 5 log entry.

WHAT IT CAN PROVE
* The count lines, extracted from SKILL.md (never a copy), pass on valid output, print the count
  and put STALE_REPORTS on the carrier; and fail on missing, non-JSON, or wrong-shape output.
* The `reports-stale` call writes into $RUN_DIR and sits before the count lines and the render.
* The Step 5 log block, run for real against a temp tree, writes the count into log.md and refuses
  to write an entry when the count never reached the carrier.

WHAT IT CANNOT PROVE
* That `graph reports-stale` computes the right set - the graph suite covers that.
* The rest of Step 4.5 (records, render, check) - only the extracted region runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL = REPO_ROOT / "skills" / "autodoc-update" / "SKILL.md"
STEP45_HEADING = "## Step 4.5 — Land the records, re-render, check"
STEP5_HEADING = "## Step 5 — Update manifest, log, hot"
COUNT_START = "# reports-stale-count:"
COUNT_END = "# end reports-stale-count"
CARRIER_LINE = '. "$(cat /tmp/autodoc-env.current)" || exit 1'


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _bash_blocks_under(heading: str) -> list[str]:
    text = _text()
    idx = text.find(heading)
    assert idx >= 0, f"{heading!r} not found in {SKILL}"
    # "## Step", not "## ": the Step 5 log heredoc itself writes a "## [date]" line
    nxt = text.find("\n## Step ", idx + len(heading))
    section = text[idx: nxt if nxt >= 0 else len(text)]
    return re.findall(r"```bash\n(.*?)```", section, re.S)


def _step45_block() -> str:
    blocks = [b for b in _bash_blocks_under(STEP45_HEADING) if "cli reports-stale" in b]
    assert len(blocks) == 1, "expected exactly one Step 4.5 block running cli reports-stale"
    return blocks[0]


def _count_lines() -> str:
    block = _step45_block()
    assert COUNT_START in block and COUNT_END in block, "reports-stale-count markers missing"
    return block[block.index(COUNT_START): block.index(COUNT_END)]


def _log_block() -> str:
    blocks = [b for b in _bash_blocks_under(STEP5_HEADING) if "log.md" in b and "ENTRY" in b]
    assert len(blocks) == 1, "expected exactly one Step 5 log block"
    return blocks[0]


def _bash() -> str:
    """Git's own bash first; a bare PATH lookup on Windows finds the WSL launcher."""
    candidates: list[str] = []
    git = shutil.which("git")
    if git:
        for base in list(Path(git).resolve().parents)[:3]:
            for rel in ("bin/bash.exe", "usr/bin/bash.exe", "bin/bash"):
                c = base / rel
                if c.is_file() and str(c) not in candidates:
                    candidates.append(str(c))
    found = shutil.which("bash")
    if found and "system32" not in found.lower() and found not in candidates:
        candidates.append(found)
    if not candidates:
        pytest.skip("no usable bash found (Git for Windows ships one)")
    return candidates[0]


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), "-c", script], env={**os.environ, **env},
        capture_output=True, text=True, timeout=60,
    )


def _run_count(run_dir: Path, payload: str | None) -> tuple[subprocess.CompletedProcess[str], str]:
    if payload is not None:
        (run_dir / "reports-stale.json").write_text(payload, encoding="utf-8")
    carrier = run_dir / "carrier.sh"
    carrier.write_text("", encoding="utf-8")
    # The runbook's env_put appends to the run carrier; this one appends to a temp file.
    script = (
        "set -u\n"
        'env_put() { printf \'%s=%q\\n\' "${1}" "${2}" >> "$CARRIER"; }\n'
        + _count_lines()
        + "echo COUNT-PASSED\n"
    )
    res = _run(script, {"RUN_DIR": run_dir.as_posix(), "CARRIER": carrier.as_posix()})
    return res, carrier.read_text(encoding="utf-8")


# ---------------------------------------------------------------- placement
def test_reports_stale_output_is_kept_in_the_run_dir_and_checked_before_render() -> None:
    lines = [ln for ln in _step45_block().splitlines() if not ln.lstrip().startswith("#")]
    call = next(i for i, ln in enumerate(lines) if "cli reports-stale" in ln)
    assert '> "$RUN_DIR/reports-stale.json"' in lines[call], lines[call]
    # the exit status catches a crash (reports-stale exits 1 on an error); it is never the
    # gate on the count - that is the shape check and the reported number below
    assert lines[call].rstrip().endswith("|| exit 1"), (
        "reports-stale exits 1 on an error; without `|| exit 1` a crash runs on to the shape check"
    )
    count = next(i for i, ln in enumerate(lines) if ln.startswith("STALE_REPORTS="))
    render = next(i for i, ln in enumerate(lines) if "cli render" in ln)
    assert call < count < render


# ---------------------------------------------------------------- the condition
def test_valid_output_passes_prints_count_and_carries_it(tmp_path: Path) -> None:
    res, carrier = _run_count(tmp_path, json.dumps({"stale": [3, 7, 9], "count": 3}))
    assert res.returncode == 0, res.stderr
    assert "COUNT-PASSED" in res.stdout
    assert "3 stale community reports" in res.stdout
    assert "STALE_REPORTS=3" in carrier


def test_zero_stale_is_a_pass_not_a_failure(tmp_path: Path) -> None:
    res, carrier = _run_count(tmp_path, json.dumps({"stale": [], "count": 0}))
    assert res.returncode == 0, res.stderr
    assert "0 stale community reports" in res.stdout
    assert "STALE_REPORTS=0" in carrier


@pytest.mark.parametrize(
    "payload",
    [
        None,                                        # the command crashed: no file at all
        "",                                          # ...or wrote nothing
        "Traceback (most recent call last):\n",      # ...or wrote something else
        json.dumps({"stale": [1, 2]}),               # count missing
        json.dumps({"count": 2}),                    # stale list missing
        json.dumps({"stale": [1, 2], "count": 5}),   # count disagrees with the list
        json.dumps({"stale": [], "count": False}),   # a bool is not a count
        json.dumps([1, 2]),                          # not an object
    ],
    ids=["missing", "empty", "not-json", "no-count", "no-stale", "mismatch", "bool", "list"],
)
def test_malformed_output_fails_and_carries_nothing(tmp_path: Path, payload: str | None) -> None:
    res, carrier = _run_count(tmp_path, payload)
    assert res.returncode != 0, res.stdout
    assert "COUNT-PASSED" not in res.stdout
    assert "FATAL" in res.stderr
    assert "STALE_REPORTS" not in carrier


# ---------------------------------------------------------------- the log entry
def _run_log(tmp_path: Path, stale_reports: str) -> subprocess.CompletedProcess[str]:
    block = _log_block()
    assert block.startswith(CARRIER_LINE), "log block must open by sourcing the run carrier"
    # The carrier source line is replaced by plain env; REPLACE placeholders are filled the way
    # an operator fills them. Everything else runs as written.
    body = block[len(CARRIER_LINE):].split("\n", 1)[1]
    body = re.sub(r"\bREPLACE\b", "x", body.split("if grep -q REPLACE", 1)[0]) + (
        "if grep -q REPLACE" + body.split("if grep -q REPLACE", 1)[1]
    )
    autodoc = tmp_path / "autodoc"
    autodoc.mkdir()
    return _run(body, {
        "AUTODOC": autodoc.as_posix(),
        "FROM": "a" * 40,
        "TO": "b" * 40,
        "STALE_REPORTS": stale_reports,
    })


def test_log_entry_carries_the_stale_count(tmp_path: Path) -> None:
    res = _run_log(tmp_path, "4")
    assert res.returncode == 0, res.stdout + res.stderr
    log = (tmp_path / "autodoc" / "log.md").read_text(encoding="utf-8")
    assert "4 stale community reports" in log


def test_log_entry_refused_when_the_count_never_reached_the_carrier(tmp_path: Path) -> None:
    res = _run_log(tmp_path, "")
    assert res.returncode != 0
    assert "STALE_REPORTS" in res.stdout + res.stderr
    log = tmp_path / "autodoc" / "log.md"
    assert not log.exists() or not log.read_text(encoding="utf-8").strip()


def test_run_report_names_the_stale_count() -> None:
    text = _text()
    idx = text.find("## Step 6 — Verify + report")
    assert idx >= 0
    section = text[idx: text.find("\n### ", idx)]
    assert "stale community reports" in section and "$STALE_REPORTS" in section
