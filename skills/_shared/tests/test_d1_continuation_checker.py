"""D1 must reject a `$ARGUMENTS` continuation that lead-dev forbids (gh#602).

`skills/lead-dev/SKILL.md` requires the ScheduleWakeup continuation to carry the
RESOLVED `--project <name> <target>` as literal text: `$ARGUMENTS` expands when a
human types the slash command, not when a wake re-invokes it. D1 used to accept
any non-empty tail after `builder_entry`, so `/lead-dev $ARGUMENTS` PASSed. The
PASS controls are load-bearing - a D1 that never passes satisfies the red cases.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"


def _load_check_module():
    """Import harness/check.py by path - it is a script, not a package module."""
    path = HARNESS_DIR / "check.py"
    assert path.is_file(), f"harness/check.py not found at {path} - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location("agentflow_harness_check_d1", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check():
    return _load_check_module()


def _run_d1(check, tmp_path, prompt, builder_entry="/lead-dev"):
    """Write one scenario=D row with `prompt` and run D1 through the shipped wake_rows."""

    class Ctx:
        pass

    Ctx.wake_rows = check.Context.wake_rows
    ctx = Ctx()
    ctx.builder_entry = builder_entry
    log = tmp_path / "schedulewakeup_log.md"
    log.write_text(
        "2026-01-01T00:00:00+00:00 | scenario=D | wake_at=2026-01-01T01:00:00+00:00 "
        f"| prompt={prompt}\n",
        encoding="utf-8",
    )
    orig = check.WAKE_LOG
    check.WAKE_LOG = log
    try:
        return check.d1_continuation_reinvokes_builder(ctx)
    finally:
        check.WAKE_LOG = orig


@pytest.mark.parametrize(
    "prompt",
    [
        "/lead-dev $ARGUMENTS",
        "/lead-dev --project agentflow $ARGUMENTS",
        "/lead-dev ${ARGUMENTS}",
        "/lead-dev $1",
        "/lead-dev --project agentflow ${1}",
    ],
)
def test_d1_fails_a_literal_arguments_token(check, tmp_path, prompt) -> None:
    result = _run_d1(check, tmp_path, prompt)
    assert result.status == check.FAIL, f"D1 accepted {prompt!r}: {result.detail}"
    assert "$ARGUMENTS" in result.detail


@pytest.mark.parametrize(
    "prompt",
    [
        "/lead-dev",
        "/lead-dev --project agentflow",
        "/lead-dev --project",
        "/lead-dev --project <name> E-0001-x",
        "/lead-dev --project agentflow <target>",
        # Step 0 strips these the same way, so each leaves no target (review B1).
        "/lead-dev --project=agentflow",
        "/lead-dev --project=",
        "/lead-dev --recovery-guard",
        "/lead-dev --project agentflow --recovery-guard",
        "/lead-dev --project agentflow --project",
        # A leftover flag is not a target.
        "/lead-dev --project agentflow --verbose",
    ],
)
def test_d1_fails_without_a_literal_resolved_target(check, tmp_path, prompt) -> None:
    result = _run_d1(check, tmp_path, prompt)
    assert result.status == check.FAIL, f"D1 accepted {prompt!r}: {result.detail}"


@pytest.mark.parametrize(
    "prompt",
    [
        "/lead-devX E-0001-x",
        "run /lead-dev E-0001-x",
        " /lead-dev-2 E-0001-x",
    ],
)
def test_d1_fails_when_the_prompt_does_not_start_with_the_builder(check, tmp_path, prompt) -> None:
    result = _run_d1(check, tmp_path, prompt)
    assert result.status == check.FAIL, f"D1 accepted {prompt!r}: {result.detail}"
    assert "does not re-invoke" in result.detail


@pytest.mark.parametrize(
    "prompt",
    [
        "/lead-dev --project agentflow E-0001-x",
        "/lead-dev E-0001-x",
        "/lead-dev --project=agentflow E-0001-x",
        "/lead-dev --project agentflow E-0001-x --recovery-guard",
    ],
)
def test_d1_passes_the_resolved_literal_shapes(check, tmp_path, prompt) -> None:
    """Control: the shapes lead-dev SKILL.md prescribes (and Step 0 accepts) still PASS."""
    result = _run_d1(check, tmp_path, prompt)
    assert result.status == check.PASS, f"D1 rejected {prompt!r}: {result.detail}"


def test_the_readme_continuation_example_is_one_d1_accepts(check) -> None:
    """gh#612: README's ScheduleWakeup glossary row showed `prompt="/lead-dev $ARGUMENTS"`,
    the one form this check exists to reject, in the first place a new reader meets the
    continuation. Only prompts inside a `ScheduleWakeup(` call count: the row goes on to
    NAME the token in prose to say why it is wrong, and that sentence must not red."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    prompts = re.findall(r'ScheduleWakeup\([^)]*?prompt="([^"]*)"', text)
    assert prompts, "README.md no longer shows a ScheduleWakeup(...prompt=...) example - moved?"
    bad = [p for p in prompts if check._ARGUMENTS_TOKEN_RE.search(p)]
    assert not bad, f"README.md shows a continuation a wake cannot expand: {bad}"
