"""Harness-tool preflight: refuse to run a loop whose session cannot continue it.

THE FAILURE THIS EXISTS FOR. Every self-waking loop's continuation is one `ScheduleWakeup`
call, and the wave engine is one `Workflow` call. Neither tool is provisioned by anything in
this repo - both come from the Claude Code session - so a session that lacks one produces a loop
that **runs exactly once and stops**, which is indistinguishable from a loop that finished its
work. There is no error, no empty queue, no halted row: the lane simply never wakes again, and
the fleet's own missed-wake alarm cannot see it, because a lane that never wrote a first row has
no declared eta to be late for.

That is the worst shape this repo's docs name: a clean install that dies somewhere else.

WHAT THIS MODULE CAN AND CANNOT DO, because the boundary decides the design.

It **cannot** discover the session's tool list. Python has no view of the harness; the tools are
not importable, not on `PATH`, and not in the environment. Only the session knows. So the
knowledge stays where it is and the RULE moves here: a caller passes the tool names it can see,
and this module decides whether that is enough and says exactly what to do if it is not.

The honest consequence, stated rather than left implicit: **a loop that never calls this is
still silent.** Detection is not automatic and this module does not claim to make it so. What it
buys is that the requirement is written down once, in code, with a test on it - instead of six
prose paragraphs that drift - and that the remedy is one string rather than something each
reader has to invent.

TWO TIERS, ADDED WHEN THE RECOVERY GUARD SHIPPED (2026-09-22). `CronCreate` became load-bearing
prose in six SKILL.md files - it is how a lane resumes itself after an account-limit blackout,
because a firing refused before its first tool call never reaches its own `ScheduleWakeup`. But
it is NOT required in the sense the other two are: a session without it runs, wakes and builds
normally and loses only unattended recovery. So it is reported at a **recommended** tier -
listed, remedied, and visible in every check - while `ok` and the exit code continue to answer
only "can this seat do its job". Declaring it Required would refuse a session that works; leaving
it undeclared would let a reader meet a `CronCreate` instruction that fails with nothing anywhere
saying it was conditional. The tier is the honest third answer between those two wrong ones.

Deliberately NOT here (the spec's own out-of-scope): any judgement about whether these tools
ship to every Claude Code user, and any fallback execution path for a missing `Workflow`. This
declares and detects. A fallback is its own epic.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

_NL = chr(10)

#: The tool a self-waking loop cannot do without. Five of the six loops self-continue;
#: dev-manager stopped when E-0220 moved the unattended pump to the steward, so it is the one
#: seat that needs neither tool.
SELF_WAKE_TOOL = "ScheduleWakeup"

#: The tool the wave-execution engine cannot do without.
WAVE_TOOL = "Workflow"

#: The tool the rate-limit recovery guard uses, and it is DELIBERATELY NOT REQUIRED.
#:
#: The distinction this constant exists to hold: a lane without `ScheduleWakeup` runs once and
#: stops, so that tool is required. A lane without `CronCreate` runs, wakes, builds and reports
#: exactly as it always did - it loses one thing only, and only in one situation: it cannot
#: resume itself after an account-limit blackout, because a firing refused before its first tool
#: call never reaches its own `ScheduleWakeup`. That is a degraded fleet, not a dead one, and
#: calling it Required would refuse a session that is perfectly able to work.
#:
#: What it must NOT be is undeclared. Six loops now instruct a `CronCreate` call, so a reader
#: whose session lacks the tool otherwise discovers it by following an instruction that fails,
#: with nothing anywhere saying the instruction was conditional.
RECOVERY_TOOL = "CronCreate"

#: Tools reported at the recommended tier: absent is DEGRADED, never MISSING, and never exit 1.
RECOMMENDED_TOOLS = (RECOVERY_TOOL,)

#: Remedies, one per tool. Written as what a reader DOES, not as what is wrong: the missing
#: remedy is the actual gap the source assessment found in README's glossary, which ends at
#: "check the tool is in your session tool list" and stops there.
_REMEDY = {
    SELF_WAKE_TOOL: (
        "This loop self-continues by calling ScheduleWakeup at the end of every wake. Without "
        "it the loop runs once and never wakes again, which looks exactly like a loop that "
        "finished. Check your session's tool list; if the tool is absent, either enable it for "
        "this session or drive the loop from an external scheduler, re-invoking the same slash "
        "command (with its --project argument) on the cadence the loop declares in its eta."
    ),
    WAVE_TOOL: (
        "Wave execution dispatches through the Workflow tool. Without it a build dies inside "
        "the orchestrator rather than at launch. Check your session's tool list; if the tool "
        "is absent, run the work through the ordinary single-agent path instead of a wave - "
        "agentflow ships no fallback wave engine, deliberately. /autodoc-init and "
        "/autodoc-update have no single-agent path at all: without Workflow they cannot run, "
        "so enable the tool for the session before using either."
    ),
    RECOVERY_TOOL: (
        "Each loop schedules a standing 5-hour recovery guard with CronCreate, so a lane refused "
        "by a quota or rate-limit window (the account's, or one model's) resumes itself once the "
        "clock passes. Without it the loop still runs, wakes and builds exactly as before; what "
        "it loses is unattended recovery - "
        "a blackout then ends only when a human relaunches, which is what the three 2026-09 "
        "blackouts cost. That is a degraded fleet, not a stopped one, so this check never "
        "refuses a launch over it. If the tool is absent, either enable it for this session or "
        "accept that a rate-limit outage needs a human to end it, and say so in the lane's row."
    ),
}


class MissingHarnessTool(RuntimeError):
    """A required harness tool is not in the caller's tool list.

    Raised rather than returned for the `require_*` entry points, because the whole point is a
    LOUD stop: a caller that swallows this is back to the silent single-run it describes.
    """


@dataclass(frozen=True)
class PreflightReport:
    """What the caller asked for, what it has, and what is missing."""

    required: tuple[str, ...]
    available: tuple[str, ...]
    missing: tuple[str, ...]
    #: Tools the caller should have but can run without. Reported, never fatal.
    recommended: tuple[str, ...] = ()
    #: The recommended tools that are absent. `ok` deliberately ignores this.
    degraded: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing REQUIRED is missing. A degraded tier never makes this false.

        Two tiers and one boolean, on purpose: `ok` answers "may this seat launch", and a seat
        with no `CronCreate` may. Folding the recommended tier into this flag would refuse a
        session that can do every job the fleet actually asks of it, and a refusal that can be
        argued with is a refusal operators learn to bypass.
        """
        return not self.missing

    def render(self) -> str:
        """One line per tool in both tiers, then the remedy for each one that is absent.

        Every tool is listed, present or not. A report that prints only failures cannot be
        told apart from a report that never ran - which is the same class of silence this
        module exists to remove.

        THE TWO TIERS USE DIFFERENT WORDS, and the words carry the difference. `MISSING` means
        this seat cannot do its job; `DEGRADED` means it can, minus recovery. A single word for
        both would make the one that stops a launch unreadable next to the one that does not.
        """
        lines = [
            f"{'MISSING' if t in self.missing else 'ok     '}  {t}" for t in self.required
        ]
        lines += [
            f"{'DEGRADED' if t in self.degraded else 'ok      '} {t}" for t in self.recommended
        ]
        absent = tuple(self.missing) + tuple(self.degraded)
        if absent:
            lines.append("")
            for tool in absent:
                lines.append(f"{tool}: {_REMEDY[tool]}")
        return _NL.join(lines) + _NL


def check_harness_tools(
    available: Iterable[str],
    *,
    needs_self_wake: bool = True,
    needs_wave: bool = False,
    wants_recovery: bool = True,
) -> PreflightReport:
    """Compare a caller's tool list against what it says it needs. Never raises.

    `available` is whatever the caller can see - a session's tool list, a test's fixture. The
    comparison is by exact name and is deliberately not fuzzy: a tool called something almost
    like `ScheduleWakeup` is not `ScheduleWakeup`, and guessing here would turn a loud stop
    into a confident wrong answer.

    `wants_recovery` defaults ON, and the default is the point. An opt-in report would be a
    declaration nobody reads: the tool would be load-bearing in six SKILL.md files and absent
    from every check anyone actually runs. It costs one extra line of output on a healthy
    session and it never changes the exit code.
    """
    have = tuple(dict.fromkeys(str(t).strip() for t in available if str(t).strip()))
    required: list[str] = []
    if needs_self_wake:
        required.append(SELF_WAKE_TOOL)
    if needs_wave:
        required.append(WAVE_TOOL)
    recommended = RECOMMENDED_TOOLS if wants_recovery else ()
    missing = tuple(t for t in required if t not in have)
    degraded = tuple(t for t in recommended if t not in have)
    return PreflightReport(tuple(required), have, missing, tuple(recommended), degraded)


def require_harness_tools(
    available: Iterable[str],
    *,
    needs_self_wake: bool = True,
    needs_wave: bool = False,
    wants_recovery: bool = True,
) -> PreflightReport:
    """`check_harness_tools`, but raises `MissingHarnessTool` when something is missing.

    This is the entry point a loop uses at wake. The exception message carries the whole
    report, so whatever surfaces it - a traceback, a log line, an ntfy - carries the remedy
    with it rather than a bare tool name someone then has to look up.
    """
    report = check_harness_tools(
        available,
        needs_self_wake=needs_self_wake,
        needs_wave=needs_wave,
        wants_recovery=wants_recovery,
    )
    if not report.ok:
        raise MissingHarnessTool(
            f"required harness tool(s) not available: {', '.join(report.missing)}"
            + _NL + _NL + report.render()
        )
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m agentflow.preflight --tools <comma-separated>` - exit 1 if anything is missing.

    A CLI as well as a function because the caller is usually a loop reading its own tool list,
    and a shell line is the only thing a prose skill can be told to run. Exit 1 rather than a
    printed warning: this is the one check whose whole value is that it STOPS the wake.
    """
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="python -m agentflow.preflight",
        description="Check the harness tools a loop needs are in this session's tool list.",
    )
    ap.add_argument(
        "--tools", required=True,
        help="comma-separated tool names visible to the caller (its own session tool list)",
    )
    ap.add_argument(
        "--no-self-wake", action="store_true",
        help=f"this caller does not self-continue, so {SELF_WAKE_TOOL} is not required",
    )
    ap.add_argument(
        "--wave", action="store_true",
        help=f"this caller dispatches waves, so {WAVE_TOOL} is required too",
    )
    ap.add_argument(
        "--no-recovery", action="store_true",
        help=(
            f"do not report on {RECOVERY_TOOL}. It is never required and never affects the "
            "exit code; this only silences the line for a seat that will not schedule a guard"
        ),
    )
    args = ap.parse_args(argv)

    report = check_harness_tools(
        args.tools.split(","),
        needs_self_wake=not args.no_self_wake,
        needs_wave=args.wave,
        wants_recovery=not args.no_recovery,
    )
    sys.stdout.write(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
