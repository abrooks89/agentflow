"""The `wiki-save` summariser seam's INLINE implementation — a bounded one-shot agent session.

`skills/wiki-save/SKILL.md` defines a seam: *something* hands `save` a ``(title, body)`` for a
firing and `save` writes it through ``agentflow.wiki``. Q-10043 chose **option A, inline from the
seat** (2026-09-05): the seat launches a bounded one-shot at its own phase boundary, waits on it,
and writes the page itself. This module is that implementation, and it is the ONLY place in the
suite that starts an agent session.

WHAT IS AND IS NOT PRECEDENT HERE, stated narrowly because the broad version is falsifiable by one
grep. This is **not** the first subprocess in the suite: ``issue_intake`` shells out to the tracker
CLI, and ``files_vs_diff`` and ``fleet_report`` shell out to git. It IS the first path that starts
an **agent session** — a model, given a prompt, whose output the suite then acts on.

Q-10015 IS SCOPED BY THIS, NOT REPEALED. That decision — a scheduler outside the fleet starts
headless sessions, and no loop launches another — governs **durable seats**: an identity, a wake
cadence, a claim, and the ability to run away. A reconciler is what makes starting one of those
auditable and idempotent, and starting one is still forbidden to a loop. Q-10043 governs a
**bounded one-shot**: no identity, no cadence, no claim, it never resumes, and it exits. The two
are different objects and only the second one lives here.

THE FIVE GUARDRAILS, and where each is enforced:

1. **NON-RECURSIVE — a hard assertion, not prose.** The spawned session hits phase boundaries of
   its own, so if it could trigger a save it would spawn a save that spawns a save. Two halves,
   both real, both tested alone and in composition:

   * :func:`_child_env` puts ``AGENTFLOW_SAVE_DISABLED=1`` on the environment block handed to the
     child, and **refuses to return an environment that does not carry it**;
   * :func:`compose_and_save` REFUSES at its first line when it finds that variable set in its own
     environment, returning ``disabled`` and writing nothing.

   **What happens if the marker is lost across the boundary.** The marker is not inherited by
   accident — it is written into an explicit ``env`` mapping this module builds and hands to the
   runner, so there is no inheritance step to lose it in. If that mapping cannot be built with the
   marker on it, :func:`_child_env` raises and **no process is started at all**. That is the
   fail-closed direction: the failure mode of a lost marker is *no save*, never *an unguarded
   child*. A save that does not happen costs one page; an unguarded child costs a spawn storm.

2. **BOUNDED AND SINGLE.** One call per boundary, :data:`DEFAULT_TIMEOUT_S` on it, no retry of any
   kind, and an in-flight guard (:data:`_IN_FLIGHT`) so a seat with a spawn running cannot start a
   second. *In flight* is represented by a **process-local, non-blocking**
   :class:`threading.Lock`, acquired and released inside one call frame under ``try/finally``.
   That representation cannot go stale into a permanent refusal because it is not persisted:
   it is released on every exit path including an exception, and a seat that dies takes it with
   it. A lock FILE would have the opposite property — it would survive the crash that orphaned it
   and would then need a stale-breaking regime of its own, which is a lot of machinery to protect
   a step that is allowed to be skipped.

3. **NON-FATAL.** Every failure path — the runner raising, a non-zero exit, a timeout, empty
   output, output that will not parse, a body the writer refuses — returns a
   :class:`SaveOutcome` with ``status="failed"`` and a REASON. Nothing here raises into the
   caller's firing. But it fails LOUDLY in the sense the skill demands: the caller is handed the
   reason and :meth:`SaveOutcome.render` prints it. An empty or wrong page written silently is
   the failure this must not have, so a page is written only from output that parsed and that
   ``write_page`` accepted.

4. **AUDITABLE.** :class:`SaveOutcome` carries the trigger, the UTC start, the duration, the exit
   code, whether it wrote, and the slug if it did. **It is a return value and one rendered line,
   not a new store** — deliberately. A JSONL under ``wiki/`` would be a new file kind in a pillar
   whose whole design is *pages plus pure projections*, and the projection sweep would have to
   learn to leave it alone; a registry row would put per-save churn on a lock-protected
   coordination file no other lane reads, and guardrail 3 says this step must never touch the
   firing's real end-state writes. The audit's readers are the seat that spawned it (which must
   say it failed) and the human reading that seat, and a returned record plus a printed line
   reaches both. When it DOES write, the page itself is the durable artefact and the record names
   its slug.

5. **BEHIND THE SEAM.** Inline is the DEFAULT implementation, not the only one. The seat calls
   :func:`compose_and_save`; moving to a reconciler later replaces this file and changes no
   caller.

TWO REQUIREMENTS INHERITED FROM THE SKILL, honoured here:

* **A summariser that returns nothing or errors fails LOUDLY.** ``write_page`` cannot catch it —
  an empty body is refused only incidentally, for carrying no ``[[wikilink]]`` — so the check is
  owned here (:func:`parse_summary`, and the empty/timeout/exit branches around it).
* **``created`` is COMPUTED, never typed.** It is not a parameter of :func:`compose_and_save` at
  all; the module reads the clock. ``wiki._validated_created`` checks SHAPE only (``2026-13-45``
  passes it) and ``select()`` ranks ``created`` lexicographically descending over a create-only
  store, so one mistyped date pins a page to the top of the hot tier permanently, correctable
  only by a newer page that the bad one still outranks.

THE CHILD NEVER WRITES THE STORE. It composes text; this module validates it and calls
``write_page``. So the child needs no write permissions, and a model that misbehaves produces a
rejected draft rather than a bad page.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from agentflow.stream_report import describe_streams
from agentflow.wiki import (
    check_new_lane,
    missing_pillar_markers,
    refresh_projection,
    write_page,
)

#: The environment variable that says "this session was started BY a save; it may not save".
#: Set on the child by :func:`_child_env`; refused by :func:`compose_and_save`.
DISABLE_ENV = "AGENTFLOW_SAVE_DISABLED"

#: The value the spawn writes. Any non-empty value disables — see :func:`saving_disabled`.
DISABLE_VALUE = "1"

#: The agent CLI the one-shot runs. A bare name, resolved on PATH: pinning an absolute path here
#: would bind the suite to one machine's install layout.
AGENT_CLI = "claude"

#: Opt-in for the LIVE smoke run (gh#272). Absent everywhere by default, so CI collects the live
#: test, skips it, and spends nothing. See :func:`live_spawn_blocked_reason`.
LIVE_ENV = "AGENTFLOW_LIVE_SAVE"

#: The three causes that stop a live spawn, as stable tokens. They lead the sentences
#: :func:`live_spawn_blocked_reason` returns so a caller can branch on the CAUSE without matching
#: prose, and so a skip line names which of the three fired rather than only that one did.
LIVE_SKIP_NO_OPT_IN = "no-opt-in"
LIVE_SKIP_SAVE_DISABLED = "save-disabled"
LIVE_SKIP_NO_AGENT_CLI = "no-agent-cli"
LIVE_SKIP_CAUSES = (LIVE_SKIP_NO_OPT_IN, LIVE_SKIP_SAVE_DISABLED, LIVE_SKIP_NO_AGENT_CLI)

#: Seconds a single summariser call may take before it is abandoned. Generous enough for a model
#: to read a bounded context and write a short page, short enough that a wedged child cannot hold
#: a phase boundary open. There is NO retry: a boundary gets one call or none.
DEFAULT_TIMEOUT_S = 180.0

#: Hard cap on the context handed to the child. The skill's rule is that composing a page must
#: never need a re-read of the whole firing; this is that rule with a number on it. Over the cap
#: the context is truncated with a visible marker rather than refused, because a shorter page is
#: worth more than no page and this step is never allowed to fail the firing.
MAX_CONTEXT_CHARS = 12000

#: The exact token the child emits when the firing learned nothing durable. That is the EXPECTED
#: outcome of most firings and it is not a failure, so it gets its own status rather than being
#: rendered as an empty save.
NOTHING_DURABLE = "NOTHING DURABLE"

#: The four phase boundaries, as decided (Q-10042). Kept here so the audit record's ``trigger``
#: is a checked value and not free text: a mistyped trigger in an audit line is a record nobody
#: can group by.
TRIGGERS = ("end-of-epic", "park", "halt", "block")

#: Every terminal state of one save attempt. ``nothing-durable`` is deliberately NOT ``failed``.
STATUSES = ("wrote", "nothing-durable", "failed", "disabled", "in-flight")

#: In-flight guard — process-local, non-blocking, released under ``try/finally``. See guardrail 2
#: in the module docstring for why this is not a lock file.
_IN_FLIGHT = threading.Lock()

# runner(argv, *, env, timeout) -> (returncode, stdout, stderr)
#
# The ``issue_intake`` shape — an injectable callable returning that triple, defaulting to a real
# ``subprocess.run`` — plus the two things a bounded, non-recursive spawn cannot do without. They
# are keyword-only so the positional shape stays identical to its sibling's, and ``env`` is a
# PARAMETER rather than something the runner reads for itself precisely so a test can assert that
# the marker reached the child.
Runner = Callable[..., tuple[int, str, str]]


class SaveSpawnError(Exception):
    """Raised only where refusing to start is the safe answer — see :func:`_child_env`.

    It never escapes :func:`compose_and_save`, which turns it into a ``failed`` outcome. It exists
    as an exception rather than a return so that the fail-closed path cannot be reached by
    forgetting to check a flag.
    """


@dataclass(frozen=True)
class SaveOutcome:
    """What one save attempt did. The audit record of guardrail 4.

    ``reason`` is empty only on a clean ``wrote``. Every other status carries one, including
    ``nothing-durable``, because "the summariser judged there was nothing to say" and "the
    summariser fell over" must never render as the same line.
    """

    trigger: str
    status: str
    started_at: str          # ISO-8601 UTC, second resolution
    duration_s: float
    exit_code: Optional[int] = None
    slug: Optional[str] = None
    path: Optional[str] = None
    reason: str = ""
    #: Words in the written body. The Directive's compliance evidence: a target nothing
    #: measures is a target nobody can tell you missed.
    word_count: Optional[int] = None

    @property
    def wrote(self) -> bool:
        return self.status == "wrote"

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    def render(self) -> str:
        """One audit line. Says what triggered it, how long it took, and whether it wrote."""
        parts = [
            f"trigger={self.trigger}",
            f"status={self.status}",
            f"started={self.started_at}",
            f"duration={self.duration_s:.2f}s",
            f"exit={self.exit_code if self.exit_code is not None else '-'}",
            f"wrote={'yes' if self.wrote else 'no'}",
        ]
        if self.slug:
            parts.append(f"slug={self.slug}")
        if self.word_count is not None:
            parts.append(f"words={self.word_count}")
        if self.reason:
            parts.append(f"reason={self.reason}")
        return "save_summariser: " + "  ".join(parts)

    def as_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 3),
            "exit_code": self.exit_code,
            "wrote": self.wrote,
            "slug": self.slug,
            "path": self.path,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Clock — this module reads one; `agentflow.wiki` deliberately does not
# --------------------------------------------------------------------------- #
def _now_utc() -> datetime:
    """The wall clock, in UTC. One function so a test has one thing to move."""
    return datetime.now(timezone.utc)


def computed_created() -> str:
    """Today's date as ``YYYY-MM-DD``, UTC. The ONLY source of a page's ``created``.

    Not a parameter anywhere in this module, on purpose. The skill's inherited requirement is
    that ``created`` is computed and never typed, and the strongest form of that rule is a
    caller who cannot supply it even by mistake.
    """
    return _now_utc().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Guardrail 1 — non-recursion, both halves
# --------------------------------------------------------------------------- #
def saving_disabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True when THIS session was started by a save and therefore may not save.

    Present-and-non-empty is the rule. An empty value is read as absent because an empty variable
    is how a shell spells "unset" in practice; anything else at all — ``1``, ``true``, ``no``,
    ``0`` — disables, which is the fail-closed reading of a marker whose only job is to be there.
    A value this function does not recognise must never be read as permission.
    """
    env = os.environ if environ is None else environ
    return bool(str(env.get(DISABLE_ENV, "") or "").strip())


def _child_env(environ: Optional[Mapping[str, str]] = None) -> dict:
    """The environment block for the child, carrying :data:`DISABLE_ENV`. Raises if it cannot.

    THE FAIL-CLOSED HALF OF GUARDRAIL 1. The marker is written into an explicit mapping handed to
    the runner rather than left to inheritance, so there is no step in which it can quietly go
    missing — and the post-condition below is checked anyway, because a guard whose only proof is
    the line above it is a guard a refactor walks through. If the marker is not on the mapping
    this raises :class:`SaveSpawnError` and the caller starts no process: a save that does not
    happen is cheap, and a child that can save is a spawn storm.
    """
    base = os.environ if environ is None else environ
    env = {str(k): str(v) for k, v in base.items()}
    env[DISABLE_ENV] = DISABLE_VALUE
    if not saving_disabled(env):
        raise SaveSpawnError(
            f"refusing to spawn: the child environment does not carry {DISABLE_ENV}, so the "
            "spawned session could trigger a save of its own and spawn another"
        )
    return env


# --------------------------------------------------------------------------- #
# The live smoke gate (gh#272) — why a real spawn is or is not attempted here
# --------------------------------------------------------------------------- #
def live_spawn_blocked_reason(
    environ: Optional[Mapping[str, str]] = None,
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """``None`` when a live spawn may be attempted here; otherwise WHY it may not.

    WHY THIS IS PRODUCT CODE AND NOT THREE ``if``\\ s IN A TEST. gh#272 is the shape where a gate
    passed while proving the opposite of what it claimed, and a live test that is skipped is
    indistinguishable from one that passed unless the skip carries a reason. A reason computed by
    an inline expression in the test is a reason nothing checks; computed here it is a function
    the always-running suite can drive through all three causes and one un-blocked case, so
    "the live run silently stopped running" is a red rather than a quieter ``s``.

    The three causes, in the order they are checked, each returning a sentence that LEADS with its
    token from :data:`LIVE_SKIP_CAUSES`:

    * **no opt-in** — :data:`LIVE_ENV` unset. First because it is the CI answer and the cheapest:
      no PATH lookup, no process, no tokens. A live model call is the only step in this suite that
      costs money, so it is opt-in rather than opt-out and its default is off everywhere.
    * **save-disabled** — :data:`DISABLE_ENV` set, i.e. this session was itself started by a save.
      :func:`compose_and_save` would return ``disabled`` and never spawn, so a live test run here
      would assert nothing while looking like it had. Checked BEFORE the CLI lookup because it is
      a statement about this session that no amount of installed CLI changes.
    * **no agent CLI** — :func:`shutil.which` finds nothing for :data:`AGENT_CLI`. The runner would
      return the "agent CLI not found" failure, which is a true report about the machine and a
      useless one about the code.

    ``which`` is injectable for the same reason ``environ`` is: the cause a test wants to exercise
    is usually not the one the machine it runs on happens to be in.
    """
    env = os.environ if environ is None else environ
    lookup = shutil.which if which is None else which

    if not str(env.get(LIVE_ENV, "") or "").strip():
        return (
            f"{LIVE_SKIP_NO_OPT_IN}: {LIVE_ENV} is not set, so no live summariser spawn was "
            f"attempted (set {LIVE_ENV}=1 to run one real spawn against the agent CLI)"
        )
    if saving_disabled(env):
        return (
            f"{LIVE_SKIP_SAVE_DISABLED}: {DISABLE_ENV} is set, so this session was started by a "
            "save and compose_and_save would return 'disabled' without spawning anything"
        )
    if not lookup(AGENT_CLI):
        return (
            f"{LIVE_SKIP_NO_AGENT_CLI}: {AGENT_CLI!r} was not found on PATH, so there is no agent "
            "CLI here to spawn"
        )
    return None


# --------------------------------------------------------------------------- #
# The one-shot
# --------------------------------------------------------------------------- #
def _subprocess_runner(argv: list, *, env: dict, timeout: float, stdin: str) -> tuple:
    """Default runner: run ``argv`` and return ``(returncode, stdout, stderr)``.

    A timeout is a NON-ZERO EXIT here, not an exception, because every caller above treats a
    non-zero exit as "nothing written, and here is why" and a timeout means exactly that. The
    ``-1`` code and the stderr text keep the two distinguishable in the audit record.

    ``argv[0]`` IS RESOLVED HERE, and resolution belongs at the spawn rather than in
    :func:`build_argv`, for two independent reasons. On Windows the agent CLI installs as a
    ``.CMD`` shim beside an extensionless shell script; ``subprocess.run`` does not consult
    ``PATHEXT``, so the bare name raises ``FileNotFoundError`` on the platform this fleet
    actually runs on — measured, not predicted: the first real save attempted after deploy
    failed with ``[WinError 2]`` while ``shutil.which`` found ``claude.CMD`` immediately.
    Second, the child receives an EXPLICIT environment (guardrail 1), so its ``PATH`` is
    whatever the caller passed; resolving in the parent against the parent's ``PATH`` means an
    absolute path reaches the child and no lookup has to succeed twice.

    The fallback is the bare name, deliberately. When ``which`` finds nothing the spawn should
    still be attempted and still fail with the "agent CLI not found" reason below, naming what
    was looked for — swallowing it here would turn a missing CLI into a silent no-op.
    """
    resolved = shutil.which(argv[0]) or argv[0]
    argv = [resolved, *argv[1:]]
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            env=env,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout:g}s"
    except FileNotFoundError as e:
        return -1, "", f"agent CLI not found: {argv[0]!r} ({e})"
    except (OSError, subprocess.SubprocessError) as e:
        return -1, "", f"agent CLI failed to run: {type(e).__name__}: {e}"
    return proc.returncode, proc.stdout, proc.stderr


def build_prompt(trigger: str, context: str) -> str:
    """The one-shot's whole instruction. Bounded, and it asks for a parseable answer.

    The child is told to answer in :func:`parse_summary`'s shape or with :data:`NOTHING_DURABLE`
    and nothing else. It is NOT told to write anything: this module writes the page, so the child
    needs no store access and a misbehaving model produces a rejected draft rather than a bad
    page.
    """
    body = context.strip()
    if len(body) > MAX_CONTEXT_CHARS:
        body = body[:MAX_CONTEXT_CHARS] + "\n[context truncated]"
    return (
        "You are composing ONE durable wiki page for an agentflow project, at the "
        f"`{trigger}` phase boundary of a firing that has just ended.\n\n"
        "A page does exactly ONE of two jobs. Decide which before you write a word.\n"
        "  LESSON - something a future lane would otherwise re-learn: a measured incident, "
        "a decision and the alternative it beat, a mechanism that behaves differently from "
        "how it reads, or a rule with the failure that produced it. Aim for "
        f"{LESSON_TARGET_WORDS[0]}-{LESSON_TARGET_WORDS[1]} words: the claim, the minimum "
        "evidence that earns it, the rule that survives.\n"
        "  AUDIT - a record of a critical action: what was done, when, by whom, what "
        f"resulted, and what it cost. Aim for under {AUDIT_TARGET_WORDS} words.\n\n"
        "CITE, DO NOT RESTATE. A page that links a PR, a spec or another page does not "
        "re-tell it. Cut narrative, throat-clearing, context the reader reaches by following "
        "a link, and any second worked example of a point already made. Every sentence must "
        "serve the one job you chose.\n\n"
        "A rerun command, a transient CI colour, or a restatement of what the board already "
        "says is NOT durable.\n\n"
        f"If this firing learned nothing durable, reply with exactly `{NOTHING_DURABLE}` and "
        "nothing else. That is the expected outcome of most firings and it is not a failure.\n\n"
        "Otherwise reply in exactly this shape, with no preamble and no code fence:\n"
        "SLUG: <kebab-case-name, no slashes>\n"
        "TITLE: <one line>\n"
        "BODY:\n"
        "<the page - at the length its ONE job calls for, carrying at least one [[wikilink]]>\n\n"
        "Write nothing to disk. Your reply IS the page.\n\n"
        "--- the firing ---\n"
        f"{body}\n"
    )


#: Flags that make the child a TEXT RESPONDER rather than an agent. ``--tools ""`` disables the
#: built-in set outright; ``--strict-mcp-config`` keeps configured MCP servers out of a one-shot
#: that has no use for them.
NO_TOOLS_FLAGS: tuple = ("--tools", "", "--strict-mcp-config", "--safe-mode")

#: Word targets from the 2026-09-05 Directive ("wiki pages are too verbose - cut them to
#: the minimum that does the job"). These are the PROMPT'S targets and deliberately NOT a
#: gate. The numbers were proposed by dev-manager and marked correctable, and
#: refusing a page on a provisional figure would throw away good writing to honour a number
#: nobody has settled. What this module owes instead is MEASUREMENT: every outcome carries
#: ``word_count``, so compliance is checkable against real pages and the target is tunable
#: against data rather than against a feeling. Make it a ceiling once the number is settled.
LESSON_TARGET_WORDS: tuple = (120, 200)
AUDIT_TARGET_WORDS = 100

#: The child's own system prompt. Without it the default agentic one applies, and a session told
#: to "compose a durable wiki page" with no tools narrates an orientation it cannot perform
#: ("let me look around the working directory") instead of answering. Measured.
SUMMARISER_SYSTEM_PROMPT = (
    "You are a one-shot text summariser. You have no tools, no working directory and no way to "
    "investigate anything. The user message is the COMPLETE and ONLY input you will ever receive "
    "about this subject; there is nothing else to find and there is no further turn. Do not "
    "narrate, do not plan, do not offer to look anything up. Emit the requested text and nothing "
    "else."
)

SUMMARISER_ROLE_FLAGS: tuple = ("--system-prompt", SUMMARISER_SYSTEM_PROMPT)


def build_argv() -> list:
    """The child's argv. One shot, print mode, no session to resume, no tools, no customisations.

    THE PROMPT IS NOT IN HERE. It goes on STDIN, and that is not a style choice — it is the
    difference between the child reading the firing and the child inventing one. Measured, and
    the measurement is worth keeping because the failure is silent:

    ``shutil.which`` resolves the agent to ``claude.CMD`` on Windows, a BATCH SHIM, and a batch
    shim TRUNCATES A MULTI-LINE ARGUMENT AT THE FIRST NEWLINE. The summariser's prompt is
    inherently multi-line, so the child received the first line of roughly four thousand
    characters and nothing else — then wrote a confident, well-formed page about an epic it
    had never been told anything about, filling the gaps from the only material it did have:
    the environment block, a date, and the operator's email address. No error, no non-zero exit,
    no empty reply. A plausible page is the worst possible output for a memory store.

    The probe that first "confirmed" argv delivery is part of the lesson. It put a marker token
    on line one and asked for it back; the token arrived, the request on line three did not, and
    the model echoed the only content it had. It passed while proving the opposite of what it
    claimed — a probe whose positive result is indistinguishable from the failure it is
    supposed to detect.

    Stdin has no such limit, no length ceiling, and no shell in the path to reinterpret it.
    

    THE PROMPT IS NOT WHAT KEEPS THE CHILD OUT OF THE STORE - THESE FLAGS ARE. Measured on the
    first real save this module ever ran: the prompt says "Write nothing to disk. Your reply IS
    the page", and the child wrote the page anyway. That is not disobedience, it is what the
    child IS - a full agent session with file tools and this suite's own skills deployed into
    it, which reads "compose a durable wiki page" and reaches for the writer it can see. The
    page it produced was good. It was also filed under the WRONG LANE, through none of this
    module's validation, while the parent's audit record said ``wrote=no`` - an audit line that
    is false about the world is worse than no audit line, because guardrail 4 exists to be
    believed.

    So the boundary the design draws - child returns text, parent validates and writes - has to
    be enforced where it cannot be reinterpreted. With the tools gone the child CANNOT write,
    so the parent must, and every page in the store has been through :func:`parse_summary` and
    ``write_page``'s refusals.
    """
    return [AGENT_CLI, *NO_TOOLS_FLAGS, *SUMMARISER_ROLE_FLAGS, "-p"]


def parse_summary(text: str) -> tuple:
    """``(slug, title, body)`` from the child's reply, or a loud refusal.

    Raises :class:`ValueError` naming what was missing. Every branch matters: the inherited
    requirement is that a summariser returning nothing or nonsense fails LOUDLY, and the failure
    it names is a page written EMPTY rather than a run that stops. ``write_page`` cannot catch
    that — it refuses an empty body only incidentally, for carrying no ``[[wikilink]]`` — so the
    check lives here, where the reply is read.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("the summariser returned nothing")
    if raw == NOTHING_DURABLE:
        raise ValueError(f"{NOTHING_DURABLE} is not a page; the caller handles it before parsing")

    slug = title = None
    body_lines: list = []
    in_body = False
    for line in raw.splitlines():
        if in_body:
            body_lines.append(line)
            continue
        stripped = line.strip()
        if stripped.upper().startswith("SLUG:"):
            slug = stripped[len("SLUG:"):].strip()
        elif stripped.upper().startswith("TITLE:"):
            title = stripped[len("TITLE:"):].strip()
        elif stripped.upper() == "BODY:":
            in_body = True

    missing = [
        name for name, value in (("SLUG", slug), ("TITLE", title), ("BODY", "x" if in_body else ""))
        if not value
    ]
    if missing:
        raise ValueError(
            f"the summariser's reply is missing {', '.join(missing)}; it answered "
            f"{raw[:120]!r}"
        )
    body = "\n".join(body_lines).strip()
    if not body:
        raise ValueError("the summariser's reply has a BODY: header and no body under it")
    return slug, title, body + "\n"


# --------------------------------------------------------------------------- #
# The seam's inline implementation
# --------------------------------------------------------------------------- #
def compose_and_save(
    wiki_root,
    *,
    trigger: str,
    context: str,
    lane: Optional[str] = None,
    runner: Optional[Runner] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    environ: Optional[Mapping[str, str]] = None,
) -> SaveOutcome:
    """Compose one page with a bounded one-shot and write it. NEVER raises into the caller.

    ``created`` is absent from this signature on purpose — see :func:`computed_created`.

    Returns a :class:`SaveOutcome` in every case, including the ones where nothing happened. The
    guards run in this order, and the order is the point: the non-recursion refusal is first, so
    no later bug can reach a spawn from inside a save-started session.

    Then the PILLAR check (gh#501): *wiki_root* must be a provisioned pillar
    (:func:`agentflow.wiki.missing_pillar_markers`), or the save is ``failed`` naming the missing
    markers. It sits after the non-recursion refusal, so a save-started session still reports
    ``disabled``; before the in-flight guard and the spawn, so a save that could never land costs
    no summariser run. ``write_page`` now refuses a non-pillar itself (gh#521); this check stays
    first so the refusal is a ``failed`` outcome before the spawn, not a raise after it.

    Then the LANE check (:func:`agentflow.wiki.check_new_lane`), also before the spawn: a lane
    that is not a bare name, or that differs only by case from a live page's lane, is ``failed``
    with the writer's reason and no model call. ``write_page`` repeats the check at the create.
    """
    started = _now_utc()
    started_at = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    clock0 = time.monotonic()

    def done(status: str, **kw) -> SaveOutcome:
        return SaveOutcome(
            trigger=trigger,
            status=status,
            started_at=started_at,
            duration_s=time.monotonic() - clock0,
            **kw,
        )

    if trigger not in TRIGGERS:
        return done("failed", reason=f"unknown trigger {trigger!r}; expected one of {list(TRIGGERS)}")

    # Guardrail 1, first line. A session started BY a save does not save.
    if saving_disabled(environ):
        return done(
            "disabled",
            reason=(
                f"{DISABLE_ENV} is set, so this session was started by a save and may not start "
                "one of its own"
            ),
        )

    # gh#501. Refuse a root that is not a provisioned pillar BEFORE spawning or writing anything.
    missing = missing_pillar_markers(wiki_root)
    if missing:
        return done(
            "failed",
            reason=(
                f"{wiki_root} is not a provisioned agentflow wiki pillar (missing: "
                f"{', '.join(missing)}); the pillar is <data_root>/projects/<name>/wiki, seeded by "
                "install/provision.py. Refusing rather than writing a page into an unprovisioned "
                "or mistyped path"
            ),
        )

    # The lane is known before the spawn, so a lane the writer would refuse (not a bare name, or
    # a case-twin of a live lane) is refused here and costs no summariser run.
    if lane is not None:
        try:
            lane = check_new_lane(Path(wiki_root), lane)
        except Exception as e:  # noqa: BLE001 — guardrail 3: a refusal is a reason, never a raise
            return done("failed", reason=f"lane refused before spawning: {type(e).__name__}: {e}")

    # Guardrail 2. Non-blocking: a boundary that finds a spawn already running skips its save
    # rather than queueing behind it, because the page is optional and the boundary is not.
    if not _IN_FLIGHT.acquire(blocking=False):
        return done("in-flight", reason="a summariser spawn is already running in this process")
    try:
        try:
            env = _child_env(environ)
        except SaveSpawnError as e:
            return done("failed", reason=str(e))

        active = runner if runner is not None else _subprocess_runner
        try:
            returncode, stdout, stderr = active(
                build_argv(), env=env, timeout=timeout, stdin=build_prompt(trigger, context)
            )
        except Exception as e:  # noqa: BLE001 — guardrail 3: a save never fails the firing
            return done("failed", reason=f"the summariser runner raised {type(e).__name__}: {e}")

        if returncode != 0:
            # BOTH streams, labelled, budgeted separately (gh#478). This line used to read
            # `(stderr or stdout or "")[:200]`, which reported the advisory this CLI prints on
            # stderr during SUCCESSFUL runs and could not show the cause that arrived on
            # stdout at all. Do not "fix" it to `stdout or stderr`: the runner above
            # synthesises its timeout and missing-CLI causes ON STDERR with an empty stdout,
            # so that ordering hides those instead. Neither stream is reliably the cause --
            # see `agentflow.stream_report` for the measurement and the cost.
            return done(
                "failed",
                exit_code=returncode,
                reason=f"the summariser exited {returncode}; {describe_streams(stdout, stderr)}",
            )

        if (stdout or "").strip() == NOTHING_DURABLE:
            return done(
                "nothing-durable",
                exit_code=returncode,
                reason="the summariser judged this firing learned nothing durable",
            )

        try:
            slug, title, body = parse_summary(stdout)
        except ValueError as e:
            return done("failed", exit_code=returncode, reason=str(e))

        try:
            path = write_page(
                Path(wiki_root),
                slug=slug,
                title=title,
                body=body,
                created=computed_created(),
                lane=lane,
            )
        except Exception as e:  # noqa: BLE001 — every writer refusal is a reason, never a raise
            return done(
                "failed",
                exit_code=returncode,
                slug=slug,
                reason=f"write_page refused the page: {type(e).__name__}: {e}",
            )

        note = ""
        try:
            refresh_projection(Path(wiki_root))
        except Exception as e:  # noqa: BLE001 — the page is on disk; say so, and say what failed
            # `wrote` STAYS true. The page exists; reporting otherwise would be a second lie on
            # top of the first failure, and the recovery (`python -m agentflow.wiki refresh`) is
            # a projection rebuild, not a rewrite of the page.
            note = (
                f"page written but refresh_projection failed ({type(e).__name__}: {e}); "
                "the page is on disk and invisible to the hot tier until a refresh succeeds"
            )
        return done(
            "wrote",
            exit_code=returncode,
            slug=slug,
            path=str(path),
            reason=note,
            word_count=len(body.split()),
        )
    finally:
        _IN_FLIGHT.release()


# --------------------------------------------------------------------------- #
# CLI — the by-hand entry. The seats call `compose_and_save` directly.
# --------------------------------------------------------------------------- #


def main(argv: Optional[list] = None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m agentflow.save_summariser",
        description="Compose one wiki page with a bounded one-shot summariser and write it.",
    )
    parser.add_argument("--wiki-root", required=True, help="<data_root>/projects/<name>/wiki")
    parser.add_argument("--trigger", required=True, choices=list(TRIGGERS))
    parser.add_argument("--lane", default=None, help="the seat writing it; drives hot-<lane>.md")
    parser.add_argument("--context", required=True, help="what this firing did and concluded")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--json", action="store_true", help="emit the audit record as JSON")
    args = parser.parse_args(argv)

    outcome = compose_and_save(
        Path(args.wiki_root),
        trigger=args.trigger,
        context=args.context,
        lane=args.lane,
        timeout=args.timeout,
    )
    if args.json:
        print(json.dumps(outcome.as_dict(), indent=2))
    else:
        print(outcome.render())
    # A failed save is REPORTED, not fatal: exit 0 keeps a caller that chains on this from
    # treating "no page this firing" as a broken step. The reason is on the line above.
    return 0


if __name__ == "__main__":  # pragma: no cover — thin CLI shim
    raise SystemExit(main())
