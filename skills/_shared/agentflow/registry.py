"""agentflow.registry — the mediated Fleet Registry write path.

**enforcement follows failure.** The Fleet Registry (``docs/REGISTRY.md``) began as a
prose contract: markdown tables plus ``### Q-NNNN`` blocks, a documented mkdir edit-lock,
an ownership table naming a single writer per field — and the honest admission "documented
convention, no runtime helper." Reality violated the convention (an agent edited a row it
did not own; a timestamp was written that nobody read; a question shipped without a
default). This module is where that prose gets mechanized: the markdown stays the storage
and the human-readable truth, and code enforces the write contract. Agents NEVER hand-edit
the registry — every mutating write goes through this CLI, which refuses violations and
names the ``REGISTRY.md`` rule it enforced. Humans still read everything and edit via the
dev-manager-mediated paths the doc describes.

STDLIB ONLY. The ``_shared`` package's PyYAML-only pledge stays intact: the registry file
is markdown tables + ``###`` blocks, parsed with plain-text handling, never YAML.

Design: every mutation is a *surgical* text edit — a single table row rewritten, one field
line within a ``### Q`` block replaced, one bullet appended. Sections the command did not
touch are spliced back byte-for-byte, so human edits and ``git diff`` stay clean (the
round-trip fidelity contract). Each mutating command runs the four-step lock dance from
``REGISTRY.md`` "Concurrency": acquire the mkdir edit-lock -> re-read the file under the
lock (the lost-update guard) -> validate actor + ownership + schema -> apply the edit ->
atomic write (temp + ``os.replace``) -> release. Heartbeats are stamped from the real clock,
never caller-supplied — encoding the never-write-a-timestamp-you-did-not-read rule.

CLI: ``python -m agentflow.registry <subcommand> --actor <agent> ...``.
"""
from __future__ import annotations

import argparse
import os
from agentflow import atomic_io, lock_file
import re
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Optional

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
# jr-dev-3 is the INTERIM fourth builder (2026-09-03, share-readiness push). It is a plain copy
# of jr-dev-2 with the name swapped; `lead-dev-as-foreman-and-n-junior-lanes` replaces the whole
# hardcoded enum with a count-driven set and retires this literal.
AGENTS = ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor", "steward", "dev-manager")
BUILDERS = ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3")
#: agent -> the Queue lane it may claim from (auditor/dev-manager have none).
LANE_OF = {"lead-dev": "lead", "jr-dev-1": "jr-dev-1", "jr-dev-2": "jr-dev-2", "jr-dev-3": "jr-dev-3"}
LANES = ("lead", "jr-dev-1", "jr-dev-2", "jr-dev-3")

AGENT_STATES = ("building", "waiting", "halted", "idle")
#: `withdrawn` is TERMINAL-BUT-NOT-DONE, and the distinction is load-bearing: `cull` moves a
#: consumed row VERBATIM into the append-only `registry-log.md`, so a retraction filed as
#: `done` becomes a permanent, never-corrected record that the work shipped -- and until that
#: sweep runs, every lane reading the board sees retracted work rendered as completed. It is
#: also absent from RELEASE_STATES on purpose: `done` is the CLAIMANT'S completion signal,
#: and a builder cannot withdraw -- only the publisher can.
QUEUE_STATES = ("ready", "building", "parked", "review", "done", "withdrawn")
#: The two states from which no further work is ever done in a row's seam -- `done` shipped it,
#: `withdrawn` retracted it, and `cull` sweeps both. On such a row `claimed_by` is PROVENANCE
#: (who shipped it, or who last held it before it was retracted), never a claim; `reap` leaves
#: those rows entirely alone and tallies them separately. `review` is deliberately absent from
#: this tuple: it is in-flight, not terminal.
TERMINAL_QUEUE_STATES = ("done", "withdrawn")
#: An issue reference as the held predicate (`issue_intake.held_refs`) reads a board cell:
#: `gh#N` in any case, or a bare `#N` (gh#909). A row citing `GH#14` or `#12` used to go
#: unheld and be offered a second time; holding too much costs one skipped pick, holding too
#: little costs a double build. A bare `#` glued to a word (`PR#12`, `x#12`) or written as
#: `PR #12` is a pull request or not a reference at all, and does not hold. Lives here so
#: `publish` warns on exactly the forms the predicate reads. Bounded: int() refuses >4300 digits.
HELD_REF_RE = re.compile(r"(?:gh|(?<![\w#])(?<!\bpr\s))#(\d{1,9})(?!\d)", re.I | re.ASCII)
#: The only states `reap` will return to the pool. AN ALLOWLIST, and that is the whole point:
#: `state` is raw text on every mutation path -- only `validate` checks it against the enum, and
#: `reap` is BY DESIGN the verb you run on a board already known to be broken. Classifying as
#: "terminal, else return it" made every unrecognised cell (`Done`, an empty cell, `shipped`, any
#: typo) resurrect as `ready` and be counted as a returned claim, which is gh#153 reproduced
#: through a different door. Anything matching neither tuple is left alone and reported.
#:
#: Explicit rather than derived from `QUEUE_STATES`. A derivation would sweep a NEWLY ADDED state
#: into whichever branch the expression happened to favour, silently;
#: `test_the_three_reap_tuples_partition_the_state_enum` fails instead, so a new state has to be
#: classified here on purpose.
RETURNABLE_QUEUE_STATES = ("building", "parked", "review")
BLOCKER_STATES = ("open", "resolved")
#: The states a CLAIMANT may move its own row into. `withdrawn` is absent deliberately, for the
#: reason given above. `building` was absent with no reason recorded anywhere, and its absence had
#: a cost: resuming a row you had parked took TWO commands, `release --state ready` then `claim`,
#: because `_cmd_claim` requires `state == "ready"` AND an empty `claimed_by`.
#:
#: BETWEEN THOSE TWO COMMANDS THE ROW IS UNCLAIMED AND `ready` ON DISK -- each CLI call takes the
#: edit-lock, writes, and releases, so the window is a real state of the file rather than an
#: in-memory blip. That is precisely the shape `_publisher_row` admits, and BOTH verbs it guards
#: reach into it:
#:   * `withdraw` retracts a row whose builder is mid-build with a worktree and commits behind it;
#:   * `relane` REWRITES THE LANE, after which the other lane's builder claims it legitimately --
#:     and the original builder's own step-2 `claim` is then refused for lane discipline.
#: A `reap` landing there finds no claim to return, and a liveness read sees a builder holding
#: nothing.
#:
#: RELANE IS THE WORSE OF THE TWO, and it is the one I first missed. `withdraw` at least leaves the
#: row visible carrying `withdrawn` and a reason; relane-then-claim leaves the board reading as
#: ordinary healthy work while two lanes own one seam, with no signal anywhere that it happened.
#:
#: (The issue reported this as "another builder claims it out of the pool", and my first cut called
#: that unreachable because `LANE_OF` is one-to-one and `_cmd_claim` refuses another lane's row.
#: That reasoning is true and the conclusion drawn from it was FALSE: it reasons about `claim`
#: against a FIXED lane, and `relane` is precisely the verb that unfixes it. The enumeration
#: stopped one line short of its own source -- `lead-dev/SKILL.md` says in bold that TWO verbs
#: discharge the prune duty and lists `relane` first.)
RELEASE_STATES = ("ready", "building", "parked", "review", "done")
ANSWERED_BY = ("human", "dev-manager", "steward")

SECTION_NAMES = ("Agents", "Queue", "Questions", "Blockers", "Directives")

EDIT_LOCK_STALE = 300.0  # seconds — REGISTRY.md "stale >5 minutes = breakable"
DEPLOY_LOCK_STALE = 2700.0  # seconds — REGISTRY.md deploy-lock "stale >45 min"
DEFAULT_ACQUIRE_TIMEOUT = 30.0
DEFAULT_STALE_HOURS = 3.0  # `read --stale` default: heartbeat older than this AND a lapsed eta

#: `missed-wakes` grace: minutes past a row's OWN declared eta before the wake counts as missed.
#: 30 is not arbitrary -- it is the fleet's own mid-firing heartbeat ceiling, so a loop that is
#: alive re-stamps at least this often. Past eta by more than one ceiling with NO heartbeat since
#: the eta means the wake did not fire, which is a much earlier and more specific signal than
#: DEFAULT_STALE_HOURS death: the 32h fleet-wide stall of 2026-08-29 was invisible to the 3h
#: staleness rule for its first three hours and to a human for all 32.
MISSED_WAKE_GRACE_MIN = 30.0

#: `lapsed-eta` grace: minutes past a row's own declared eta before a lane that IS still
#: stamping a heartbeat counts as having stopped maintaining the field (gh#281).
#:
#: DELIBERATELY NOT `MISSED_WAKE_GRACE_MIN`, and four times it, because the two numbers answer
#: different questions. 30 minutes is the heartbeat ceiling: past it with no heartbeat, the wake
#: did not fire. But a lane whose heartbeat IS arriving is alive, and the fleet's own documented
#: in-flight procedure -- `status --heartbeat-only`, which preserves `eta` verbatim by design --
#: makes "past my eta and still working" the NORMAL shape of an honest long build. At a 30-minute
#: grace this check would fire on the whole fleet during any multi-hour epic, and an alarm that
#: fires on correct behaviour is one the reader mutes; muted is how gh#281 stayed open. Two hours
#: is past any plausible honest overrun of a declared wake: a live lane that far past its own eta
#: has stopped re-declaring, which is the defect, not the work running long.
#:
#: Not exposed as a tuning flag. `--grace-minutes` tunes the missed-wake alarm and nothing else
#: (`_do_read`'s pairing table), and sharing one knob would mean an operator tightening the wake
#: alarm silently floods this report with every lane doing exactly what the runbook says.
#:
#: WHAT THIS GRACE DELIBERATELY DOES NOT CATCH, stated because the obvious reading is wrong.
#: gh#281 cites a live 2026-09-05 row -- eta 06:00Z, heartbeat 06:54Z -- as the shape nothing
#: reported. That row is 54 minutes lapsed, INSIDE this grace, so this detector is silent on it
#: too, and by design: at 54 minutes a live heartbeat past a declared eta is indistinguishable
#: from an honest overrun. What the detector closes is the PERMANENT invisibility -- that row was
#: unreportable at any threshold for as long as it existed, and is now reported from two hours
#: on. A reader expecting the cited example itself to fire would otherwise conclude the detector
#: is broken when it is doing exactly what this constant says.
LAPSED_ETA_GRACE_MIN = 120.0

#: `read --stranded` default: how long a halted lane may wait on an unanswered question
#: before the fleet is told. 4h is the number the loop skills already carried in prose, kept
#: rather than re-derived so the code and the instruction cannot drift apart.
STRAND_THRESHOLD_HOURS = 4.0

#: The furthest ahead an ``eta`` may be declared (gh#682). The longest honest wake any loop
#: documents is a backoff stand-down: the lane schedules nothing and its next wake is the
#: five-hour ``--recovery-guard`` cron, which the scheduler jitters by up to 15 minutes. Every
#: other cadence is a ``ScheduleWakeup``, which the harness clamps to one hour. Six hours clears
#: the longest of them with margin and is still short enough that a year typo (2027 for 2026)
#: cannot switch off every liveness detector for a lane that keeps holding its claims.
#:
#: ``status`` measures it from the clock at write time. ``blind_rows`` measures it from the row's
#: OWN heartbeat, so a far eta that arrived another way (a hand edit, a row written before this
#: rule) is reported deterministically rather than depending on when the report runs.
ETA_HORIZON_HOURS = 6.0


#: Rows the horizon does not apply to. dev-manager is the human's interactive seat: it does not
#: self-wake and holds no claims, so its eta is when the human next intends to sit down, which
#: may honestly be tomorrow. The horizon exists for a lane that holds claims and can die; a far
#: eta on a row that holds nothing switches off no detector that protects work.
ETA_HORIZON_EXEMPT: frozenset[str] = frozenset({"dev-manager"})


def _eta_beyond_horizon(eta: datetime, ref: datetime) -> bool:
    """True when *eta* lies more than ``ETA_HORIZON_HOURS`` after *ref*."""
    return eta - ref > timedelta(hours=ETA_HORIZON_HOURS)

#: Phase text `reap` stamps on a row it idles. The missed-wake alarm must never fire on a reaped
#: row -- the reap already reported it, and reap PRESERVES the dead eta as evidence, so without
#: this marker every reaped row looks exactly like a missed wake forever. Making the reap say so
#: in the row beats inferring it from an empty phase, for the same reason this epic makes eta a
#: field instead of prose: an invariant you can read is worth more than one you deduce.
REAPED_PHASE = "reaped by steward (stale)"
#: The same stamp for a `reap --force` of a row `stale_rows` did not list (gh#575): the row may
#: not assert a staleness the verb never checked. Every reader asks `is_reaped_phase`, never
#: either literal, so the two stamps are one exclusion.
REAPED_FORCED_PHASE = "reaped by steward (forced)"
REAPED_PHASES = (REAPED_PHASE, REAPED_FORCED_PHASE)


def is_reaped_phase(phase: str) -> bool:
    """True when *phase* is a stamp `reap` wrote (stale or forced). Exact match, never substring."""
    return phase.strip() in REAPED_PHASES

#: A lane a human deliberately stopped. Written by ``stand-down``; cleared by that lane's
#: next real ``status`` write. The alarm excludes it the same way it excludes the other two:
#: on the ROW'S OWN TEXT, never on a list someone maintains elsewhere.
STOOD_DOWN_PHASE = "stood down by steward"
_ACQUIRE_POLL = 0.01

#: Hard cap on one coordination Note's --text, measured in UTF-8 BYTES (not characters).
#: 300 and not 600: 600 is room enough to write a paragraph, and a loop handed room for a
#: paragraph writes one — the Queue section then carries analysis nobody re-reads, on the
#: fleet's most-read surface. 300 is about two sentences, which is exactly the shape a routed
#: note needs: the fact, the action, and where the reasoning lives. The smallest real note on
#: the live surface was 92 bytes and was perfectly clear. Anything longer belongs in a file.
NOTE_MAX_BYTES = 300

#: Hard cap on an Agents row's `phase`, in UTF-8 BYTES, and deliberately SMALLER than
#: NOTE_MAX_BYTES. The incentives were exactly inverted before this existed: the machine-only
#: Notes channel was capped at 300 with a loud refusal, while `phase` — the only status text a
#: HUMAN reads, in a cell of a table they scan every row of — had no bound at all, and was used
#: as a diary. A field a person reads must be shorter than one a program reads, not longer.
#: 240 rather than 300 makes that ordering structural instead of advisory, and still leaves
#: room for a mis-launch marker plus a real sentence. `phase` names WHAT IS RUNNING NOW; the
#: narrative behind it goes to the note channel, which is what the refusal says.
PHASE_MAX_BYTES = 240

#: The ONE directory an over-cap Note's body may live in, resolved beside `registry.md`. Every
#: mention of it — the refusal advice, the reference parser, the cull sweep — is built from this
#: name, so a later epic that introduces a real overflow bucket moves the store with one edit
#: instead of a grep. It is deliberately a bare directory NAME, never a path: `registry.md`'s own
#: parent is the only anchor, which is what makes a pointer checkable rather than merely trusted.
NOTE_OVERFLOW_DIR = "notes"

#: Anti-splitting guard: this many notes on the SAME actor->recipient route, stamped inside
#: this window AND carrying more than NOTE_BURST_BYTES between them, read as one note split
#: across rows — the exact evasion a byte cap invites.
NOTE_BURST_MAX = 3
NOTE_BURST_WINDOW_MIN = 15

#: The bytes half of the burst test, and the half that keeps it honest. A row COUNT alone
#: cannot tell a split paragraph from a busy triage pass: four short, unrelated notes
#: ("E-0041 reassigned", "deploy lock free", "Q-0007 answered", "PROD IS DOWN") are ~120 bytes
#: total and are not a split note, but a count-only guard refuses the fourth — and in a
#: headless loop a refused note is a LOST message, so the urgent broadcast is the one dropped.
#: Splitting to evade the cap necessarily produces BULK; a triage pass does not. Requiring both
#: signals makes the guard mean what its refusal says.
NOTE_BURST_BYTES = NOTE_MAX_BYTES * NOTE_BURST_MAX

#: Note bodies the CLI writes on its OWN behalf (reap, and the two stale-lock steals), never on
#: an author's. The burst tally skips them: a steward that reaps dead lanes wrote no
#: notes of its own, and must not find its next real broadcast refused as a "split note".
#: Skipping them also keeps the tally honest about what is on DISK — `_run_mutation` folds the
#: edit-lock steal note into the in-memory text BEFORE the command runs, so counting it would
#: report a note the operator cannot find in the file.
_MACHINE_NOTE_MARKERS = (
    "broke stale registry edit-lock",
    "broke stale deploy lock",
    "reaped ",
    # A repair sweep is one steward decision spread over several lines; its audit trail must not
    # spend the steward's own `-> fleet` note budget and refuse the message it wanted to send.
    "repaired ## ",
    "answered Q-",
)

#: How many stranded notes the reap line names individually before it summarises the rest.
#:
#: IT NAMES THEM BY STAMP AND SENDER, NEVER BY BODY TEXT, and that is a correctness rule rather
#: than a style choice. My first cut quoted the opening of each body so the operator could pass it
#: to `note --strike --match`. Review found four separate defects caused by that quoting, because
#: an excerpt is ARBITRARY ACTOR TEXT re-emitted inside a machine-written line that other code
#: parses:
#:   1. SHADOWING. `_append_note` prepends, so the reap line is the TOPMOST note; `_strike_note`
#:      then struck the first live line containing the match. The excerpt therefore always matched
#:      the warning before the note it quoted -- the advertised round trip consumed the warning and
#:      left the stranded note live, which is gh#43 one layer down. (Since gh#650 a match that
#:      hits more than one distinct live Note is refused as ambiguous; only byte-identical
#:      duplicates still resolve to the first. An echoed excerpt would now strand the round trip
#:      instead of mis-striking it -- still wrong, so the rule stands.)
#:   2. A body containing "-> <agent>" and "stale" satisfied `_has_live_stale_note` for an
#:      UNRELATED lane, promoting it straight to reap-eligible and skipping the grace rule. A
#:      warning about a dead lane could get a live one reaped. (Closed at the predicate too since
#:      gh#285: it reads the PARSED routing header and an opening anchored at the body start.)
#:   3. A quoted note-overflow pointer keeps that overflow file permanently referenced, defeating
#:      cull's collector.
#:   4. `_guard_note_size` is never applied to this line, so a quoted body smuggled unbounded
#:      actor text onto a capped surface.
#: Stamp and sender are machine-generated, contain no routing arrow, and identify the note
#: uniquely enough to act on. That is the whole of what a warning needs.
STRANDED_NOTE_SAMPLE = 3

#: An epic id: a NUMBER and a NAME, and the number is not the priority (E-0260).
#:
#: Both shapes are accepted for READS and only the long one is ever WRITTEN. The number alone
#: stays a valid grep key -- `\bE-0708\b` still finds `E-0708-move-the-unattended-pump`, because
#: the hyphen is a word boundary -- which is the whole reason the number leads.
#:
#: WHY THE BARE FORM SURVIVES FOREVER. Every id this fleet issued before E-0260 was typed by
#: hand at `publish`, and in practice what got typed was the SPEC'S PRIORITY: `E-0200` is
#: `three-pillar-project-layout-and-vault` at p200. Those ids are in merged PR titles, in the
#: append-only registry log, in Notes and in commit messages. Rewriting them would be rewriting
#: history to make a regex tidier, so the read side simply accepts both and the write side only
#: ever emits the new shape.
#:
#: The name is lowercase `a-z0-9` tokens joined by single hyphens -- exactly what `_short_name`
#: emits -- so a hand-typed `E-0001-Bad_Case` is refused rather than quietly becoming a second
#: spelling of one epic's id.
#:
#: Digits are `[0-9]`, never `\d`: `\d` takes any Unicode decimal digit, so `E-0200` spelled in
#: Arabic-Indic digits passed every check yet compared unequal to its ASCII spelling, and `int()`
#: read it as 200 in the allocator. Every id pattern in this module follows the same rule.
_E_ID = re.compile(r"^E-[0-9]{3,}(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?$")
#: The name half of an id, or no match at all for a legacy bare id.
_E_NAME = re.compile(r"^E-[0-9]{3,}-([a-z0-9]+(?:-[a-z0-9]+)*)$")
_Q_ID = re.compile(r"^Q-[0-9]{3,}$")
_B_ID = re.compile(r"^B-[0-9]{3,}$")
_ANY_ID = re.compile(r"^[QB]-[0-9]{3,}$")

#: Question fields `repair` may never rewrite — the human-decision record (gh#132).
#:
#: `repair` is a STRUCTURAL verb: it exists so a mangled line parses again. Every field named
#: here carries a decision instead of structure, and the lifecycle verbs that write them are
#: already guarded (`_cmd_answer` refuses `q_answer_open`, `_cmd_apply` refuses `q_apply_raiser`).
#: Without this list `repair` performs the same class of edit with no field policy at all, so a
#: steward loop could write `answer:` + `answered_by: human` onto a question no human ever saw
#: and `validate` would still print `registry valid` — fabricating the exact authority the
#: autonomy envelope rests on, on the fleet's ONLY human channel.
#:
#: `raised_by` is on the list for a second, sharper reason: `q_apply_raiser` READS it to decide
#: who may flip `answered -> applied`. A verb that could rewrite it would be editing the input
#: to the guard that polices it.
#:
#: The policy is EFFECT-BASED, not a guard on what the caller types. It has been widened TWICE,
#: each time because a narrower version was demonstrated open with a live rc=0 run:
#:
#:   1. `--old-line` only. Closed by comparing the protected field LINES before and after,
#:      because the field parser is last-wins — overwriting an unprotected line with
#:      `- state: answered` shadowed the real one, exit 0, `registry valid`.
#:   2. Line text only. Closed by comparing the PARSED decision record (`_decision_record`),
#:      because a line list knows nothing about which `### Q-NNNN` block owns each line, nor
#:      about which `## Questions` heading the parser actually reads. See that function.
#:
#: That includes RESTORING a malformed decision line: writing the record is what this verb may
#: not do, whatever shape the line arrives in.
#:
#: What is NOT here is the point of the list being a list: `urgency`, `default`, `assumption`
#: and a mangled `### Q-NNNN` header are all still repairable, because refusing the whole
#: section would trade this defect for an unfixable one.
#: What the schema uses to mean ABSENT, in ONE place. `-` is the literal the CLI itself writes
#: into an empty cell, so a required field "satisfied" by `-` is a field satisfied by nothing.
#:
#: THE WRITE PATH AND THE DETECTION GATE MUST READ THE SAME SET, and before this they did not:
#: `ask` refused a blank `--default` and accepted `--default "-"`, while `validate` reported that
#: same row as "live question missing default". So the CLI wrote a row its own validator called
#: broken, and the raiser learned that only if somebody ran `validate` afterwards. Two hand-written
#: copies of "absent" is one copy too many.
ABSENT_FIELD_VALUES = frozenset({"", "-"})


def _field_is_absent(value) -> bool:
    """True when a field carries nothing a reader could act on."""
    return (value or "").strip() in ABSENT_FIELD_VALUES


#: Terminal, and NOT `answered`: nothing was decided. A question that should never have been
#: asked had no way off the board -- `withdraw` is Queue-only and refuses a `Q-` id at the arg
#: guard, and `cull` reports `0 questions` -- so retiring one cost EXACTLY the human attention it
#: wrongly spent, because the human had to answer it to clear it.
Q_RETRACTED = "retracted"

#: Every state a question block may carry. Built as a tuple so the validator, the cull and the
#: refusal text all read one list.
QUESTION_STATES = ("open", "answered", "applied", Q_RETRACTED)

#: Terminal AND nothing is waiting on them. Read by `_live_ids` (so no lane may point
#: `waiting_on` at one) and by the cull (so both are swept to `registry-log.md`). One tuple,
#: because these two readers disagreeing is how a "retracted" row would have stayed on the board
#: forever while every doc said it was culled.
CONSUMED_QUESTION_STATES = ("applied", Q_RETRACTED)

_Q_PROTECTED_FIELDS = ("state", "answer", "answered_by", "answered_at", "raised_by", "raised_at")
#: Raiser-owned, protected from `open` onward (gh#191). Not a human decision, which is why it is a
#: separate tuple: on an advisory question `assumption` IS the urgency ("proceeding on X"), and an
#: answered one carries it into the decision the human read -- so a steward-written assumption puts
#: words in the raiser's mouth. Only the raiser changes it: retract and re-ask while `open`, or
#: `apply` once answered. `urgency` and `default` stay repairable on purpose.
_Q_RAISER_FIELDS = ("assumption",)
#: Built from the tuples, never hand-copied — a hand-copied enum in this file has gone stale before.
_Q_PROTECTED_RE = re.compile(
    r"^- (" + "|".join(_Q_PROTECTED_FIELDS + _Q_RAISER_FIELDS) + r"):"
)
#: One sentence, read by every repair layer that refuses a raiser field.
_Q_RAISER_HINT = (
    "assumption is raiser-owned; the raiser retracts and re-asks (open) or applies (answered)"
)

#: Added to the protected record once a question leaves `open` — the BINDING half of a decision.
#:
#: A decision is the (asked, answered) PAIR, and neither half means anything alone. A recorded
#: "Yes, go ahead." against a repointed `question:` approves something nobody approved, and `work`
#: is the only binding `read --answered-for` actually prints, so a repointed `work` walks a real
#: answer onto a different epic. Both were demonstrated at rc=0 with `validate` green.
#:
#: They are NOT protected while the question is still `open`, and that boundary is deliberate:
#: before an answer exists there is no pairing to break, and an open question with a mangled
#: `question:` line is exactly the malformed row `repair` is FOR.
_Q_BINDING_FIELDS = ("question", "work")

#: Added to the protected record once a question is RETRACTED, for the same reason
#: `_Q_BINDING_FIELDS` protects a decided one: a retraction is a lifecycle record, and the reason
#: is the only thing that distinguishes a dead premise from an inconvenient question. Left
#: unprotected, `repair` rewrote it at exit 0 with `validate` green -- while the identical repair
#: against `answer` was refused, which is the inconsistency that made this an omission rather
#: than a decision. They are NOT protected while the question is still `open`, because before a
#: retraction exists there is no record to break.
_Q_RETRACTION_FIELDS = ("retraction", "retracted_at")

# Named REGISTRY.md rules — a refusal cites exactly one so the "why" is auditable.
RULES = {
    # Counted from AGENTS, never typed: a hand-written "six" said six while AGENTS held seven,
    # which is this epic's own drift class sitting in a refusal string a lane reads when it
    # mistypes its actor.
    "agent_known": f"REGISTRY.md #Agents: a flag naming a loop must name one of the {len(AGENTS)} fleet loops",
    "agent_own_row": "REGISTRY.md #Agents: each agent rewrites ONLY its own row",
    "agent_state_enum": "REGISTRY.md #Agents: status enum is building|waiting|halted|idle",
    "agent_waiting_on": "REGISTRY.md #Agents: when a row is parked or a status is waiting/halted, --waiting-on must name a live Q-/B- id",
    "queue_lane_discipline": "REGISTRY.md #Queue: claim only from your own lane",
    "queue_claim_ready": "REGISTRY.md #Queue: a claim flips a ready, unclaimed row",
    "queue_only_claimant": "REGISTRY.md #Queue: only the claimant edits a claimed row",
    "queue_relane_leaddev": "REGISTRY.md #Queue: only lead-dev assigns lanes, so only lead-dev re-lanes",
    "queue_withdraw_leaddev": "REGISTRY.md #Queue: only lead-dev publishes, so only lead-dev withdraws",
    "queue_revise_unclaimed": "REGISTRY.md #Queue: a publisher revises only an UNCLAIMED row; route the claimant a Note instead",
    "queue_revise_ready": "REGISTRY.md #Queue: only an unstarted (ready) publication can be re-laned or withdrawn",
    "queue_revise_needs_reason": "REGISTRY.md #Queue: relane/withdraw require --reason, so the row says WHY the work moved",
    "queue_relane_noop": "REGISTRY.md #Queue: relane must name a DIFFERENT lane",
    "queue_publish_leaddev": "REGISTRY.md #Queue: lead-dev publishes and assigns lanes",
    "queue_publish_allocates_id": "REGISTRY.md #Queue: publish ALLOCATES the epic id from --slug; a lane never types one",
    "queue_lane_enum": "REGISTRY.md #Queue: lane is lead|jr-dev-1|jr-dev-2|jr-dev-3",
    # BUILT FROM THE TUPLES, never hand-copied. This sentence had already gone stale once, and the
    # test I first wrote to catch that was vacuous: it asserted the message mentions every state,
    # and the stale text mentioned every state too -- the staleness lived in the PARENTHETICAL.
    # Deleting the list was the wrong repair (a pre-existing test rightly wants the refusal to
    # name them); deriving it is the right one, because now the copy cannot disagree with the
    # source it is copied from.
    "queue_state_enum": (
        "REGISTRY.md #Queue: state enum is " + "|".join(QUEUE_STATES)
        + " (release accepts " + "|".join(RELEASE_STATES) + ")"
    ),
    "queue_resume_waiting": "REGISTRY.md #Queue: a resumed row is not waiting on anything",
    "q_needs_default": "REGISTRY.md #Questions: every question MUST carry default: (malformed otherwise)",
    "q_auditor_not_halted": "REGISTRY.md #Question-lifecycle: auditor may raise waiting/advisory only, never halted",
    "q_urgency_enum": "REGISTRY.md #Questions: urgency is halted|waiting|advisory",
    "q_retract_raiser": "REGISTRY.md #Question-lifecycle: only the raiser may retract its own question",
    "q_retract_open_only": "REGISTRY.md #Question-lifecycle: retract takes an OPEN question; an answered one carries a decision",
    "q_retract_needs_reason": "REGISTRY.md #Question-lifecycle: a retraction records what collapsed",
    "q_advisory_needs_assumption": "REGISTRY.md #Questions: an advisory question MUST carry assumption: (the call the raiser made and proceeded on)",
    "q_answer_manager": "REGISTRY.md #Questions: only a manager role (steward or dev-manager) writes answer/answered_by/answered_at",
    "q_answer_open": "REGISTRY.md #Questions: only an open question can be answered",
    "q_answered_by_enum": "REGISTRY.md #Questions: answered_by is human|dev-manager|steward",
    "q_apply_raiser": "REGISTRY.md #Questions: only the raiser flips answered -> applied",
    "q_apply_answered": "REGISTRY.md #Questions: apply requires state answered",
    # One key for apply, retract and resolve-blocker: the recovery is the same for all three.
    "waited_on_live": "REGISTRY.md #Queue: a question or blocker a Queue row still waits on is not consumed until that row is unparked",
    "blocker_owner_resolves": "REGISTRY.md #Blockers: the assigned owner resolves the blocker",
    "blocker_assign_manager": "REGISTRY.md #Blockers: a manager role (steward or dev-manager) assigns blocker ownership",
    "blocker_assign_steward_owned": "REGISTRY.md #Blockers: the steward assigns only a blocker it owns or one with no owner; dev-manager assigns any",
    "blocker_open": "REGISTRY.md #Blockers: only an open blocker can be resolved/assigned",
    "directive_devmanager": "REGISTRY.md #Directives: written ONLY by dev-manager",
    "repair_boilerplate_line": "REGISTRY.md #What-repair-may-not-touch: a table header or separator, the Deploy lock / Notes line, or a section comment is never a repair target",
    "cull_steward": "REGISTRY.md #Ownership: culling + malformed-row repair is steward only",
    "deploy_holder": "REGISTRY.md #Queue: only the deploy-lock holder releases it",
    "deploy_builder": "REGISTRY.md #Queue: deploy lock is held by a builder lane",
    "deploy_held": "REGISTRY.md #Queue: deploy lock is held (not stale) by a live holder",
    # A SECOND refusal, not a variant of the one above, because the two recoveries are opposite:
    # `deploy_held` means WAIT (the holder is live and will release), this one means DECIDE (the
    # hold is stealable and nobody has judged the holder dead yet). A lane must be able to tell
    # them apart from the KEY alone — reading the ages back out of the message to work out which
    # one it is, is the branch that was never written.
    "deploy_stale_unbroken": "REGISTRY.md #Queue: breaking another lane's stale deploy hold is an explicit --break-stale decision",
    "deploy_not_holder": "REGISTRY.md #Queue: the deploy lock serializes every merge-to-main",
    "id_format": "REGISTRY.md: ids are E-/Q-/B- + zero-padded digits",
    "id_exists": "REGISTRY.md: referenced id does not exist in the registry",
    "id_duplicate": "REGISTRY.md: id already exists in the registry",
    # THREE keys, not one, because the three name three different recoveries. They were one
    # key until gh#134, and the single message told an operator staring at a full registry to
    # run `init` — the one action that must never be taken while the real content is on disk.
    "file_missing": "REGISTRY.md: registry file absent — run `registry init --project <name>` (or provision)",
    # Exit 3, not 2: nothing about the call was wrong, and re-running it (which re-reads the board
    # under a fresh lock) is the whole recovery.
    "edit_lock_stolen": "REGISTRY.md #Concurrency: the edit-lock was stolen, or the board changed on disk, while this command held it — nothing was written; re-run the command",
    "file_unreadable": "REGISTRY.md: registry file present but unreadable — truncated, half-synced, not a regular file, or --file/$AGENTFLOW_REGISTRY points at the wrong thing",
    # Deliberately NOT "run init": the file decoded and the content is still there, so init
    # would refuse (it never clobbers) and, if it did not, would destroy the only copy. The
    # rule names no particular defect, because the key covers two with different fixes: a lost
    # section or table (its detail carries `_MISSING_STRUCTURE_ADVICE`) and a damaged row (its
    # detail names the row). Advising a heading restore over a duplicated row sent the
    # operator hunting a defect the board did not have.
    "file_damaged": "REGISTRY.md: registry file readable but structurally damaged — the detail below names the defect",
    # gh#715. Both deliberately NOT "run init": a typo'd project told to init got a phantom
    # board no builder reads, and a board conflict is resolved by choosing a board, not making one.
    "project_unconfigured": "REGISTRY.md #Board-resolution: --project must name a project whose config loads (<config-home>/projects/<name>.md) -- fix the config error below, never `init` a board for it",
    "lock_name_empty": "REGISTRY.md #Concurrency: the edit-lock is named by the project directory above the board's status/ directory, and this board's resolved path has none -- move the board under <project>/status/",
    "registry_board_conflict": "REGISTRY.md #Board-resolution: $AGENTFLOW_REGISTRY and --project name different boards -- unset the env var, or pass --file to choose the board explicitly",
    "eta_iso": "REGISTRY.md #Agents: eta must be an ISO-8601 timestamp",
    "eta_too_far": (
        f"REGISTRY.md #Agents: eta may be at most {ETA_HORIZON_HOURS:g}h ahead -- the longest "
        "documented wake (the 5h recovery-guard cron) fits inside it; dev-manager is exempt"
    ),
    "eta_required": "REGISTRY.md #Agents: every status write declares its next wake in eta (only halted may omit it)",
    "heartbeat_only_exclusive": "REGISTRY.md #Agents: --heartbeat-only refreshes the heartbeat alone, with no other field",
    "inline_only": "REGISTRY.md: field values are single-line and '|'-free (table-cell and field-line safety)",
    "read_one_selector": "REGISTRY.md: `read` runs ONE report per call, with only its own tuning flag",
    "note_too_long": "REGISTRY.md #Queue: a coordination Note carries a decision, not the analysis behind it (300 UTF-8 bytes)",
    "phase_too_long": "REGISTRY.md #Agents: phase names what is running now, in a cell a human scans (240 UTF-8 bytes)",
    "note_split_burst": "REGISTRY.md #Queue: a burst of notes on one route is a split note — the analysis belongs in a file",
    "bad_project": "REGISTRY.md: --project is a routing slug ([A-Za-z0-9_-]+), never a path fragment",
    "note_missing": "REGISTRY.md #Queue: no live (un-struck) coordination Note matches",
    "note_not_yours": "REGISTRY.md #Queue: strike only a Note routed TO you (or to fleet); dev-manager is the janitor",
    "note_strike_ambiguous": "REGISTRY.md #Queue: each note --strike --match must hit EXACTLY ONE live Note you may strike",
    "note_strike_text": "REGISTRY.md #Queue: --strike consumes a Note and takes no --text",
    "reap_steward": "REGISTRY.md #Ownership: only the steward reaps a dead loop (cross-row liveness sweep)",
    "reap_not_stale": "REGISTRY.md #Liveness-sweep: reap only a row `read --stale` lists (or --force --reason, once a human confirmed the lane is gone)",
    "reap_self": "REGISTRY.md #Liveness-sweep: a loop never reaps its own row",
    "reap_force_needs_reason": "REGISTRY.md #Liveness-sweep: a forced reap records WHY the liveness check was overridden",
    "reap_stood_down": (
        "REGISTRY.md #Agents: a stood-down lane was stopped by a human on purpose; reaping it is "
        "dev-manager's decision, carried out with --override-stand-down"
    ),
    "reap_exempt_seat": (
        "REGISTRY.md #Liveness-sweep: the human's seat (ETA_HORIZON_EXEMPT) holds no claims or "
        "lock, so a reap recovers nothing; a stale row there is away past its declared return"
    ),
    "stand_down_steward": "REGISTRY.md #Ownership: only the steward stands a lane down (the human is not a writer)",
    "stand_down_needs_reason": "REGISTRY.md #Agents: stand-down requires --reason, so the row says WHY it is excluded",
    "stand_down_heartbeat_only": "REGISTRY.md #Agents: a stood-down row needs a full status write to come back, not a heartbeat",
    "stand_down_reaped": "REGISTRY.md #Agents: a reaped row holds no claims and is already excluded; standing it down would erase the reap mark",
    "stand_down_self": "REGISTRY.md #Liveness-sweep: a loop never stands down its own row",
    "stand_down_exempt_seat": (
        "REGISTRY.md #Liveness-sweep: the human's seat (ETA_HORIZON_EXEMPT) is never alarmed as a "
        "missed wake, so there is nothing to stand down"
    ),
    "repair_steward": "REGISTRY.md #Ownership: malformed-row repair is steward only",
    "repair_no_match": "REGISTRY.md #Ownership: repair --old-line matched no line in the section",
    "repair_blank_old_line": (
        "REGISTRY.md #Ownership: repair mends an existing damaged line; an empty or blank "
        "--old-line matches a separator and would ADD a line (a Directive, a Blocker) instead"
    ),
    "repair_corrupt": "REGISTRY.md: repair must leave the registry parseable — the replacement did not",
    "repair_queue_owned": (
        "REGISTRY.md #Queue: repair may not rewrite a Queue row `validate` already accepts unless "
        "its id is on more than one Queue line, and on a row `validate` rejects it may change only "
        "the cells `validate` flags, a flagged claimed_by or waiting_on only to '-'; where '-' "
        "alone would strand the row (a flagged claimed_by on a building/parked/review row, a "
        "dead waiting_on on a parked one) it takes exactly what reap or `release --state "
        "building` writes, and no mend may leave an in-flight row unclaimed; every cell has an "
        "owning verb (publish/relane/withdraw for lead-dev, claim/release for the claimant, "
        "reap for a dead claimant)"
    ),
    "repair_blocker_owned": (
        "REGISTRY.md #Blockers: repair may not rewrite a Blockers row `validate` already accepts "
        "unless its id is on more than one Blockers line, and on a row `validate` rejects it may "
        "change only the cells `validate` flags, a flagged owner only to '-' and state only to "
        "'open'; every cell has an owning verb (raise-blocker, assign-blocker, resolve-blocker, "
        "cull)"
    ),
    "repair_agents_owned": (
        "REGISTRY.md #Agents: repair may not rewrite an Agents row `validate` already accepts "
        "unless its agent is on more than one Agents line, and on a row `validate` rejects it "
        "may change only the flagged status, and only to 'idle'; every cell has an owning verb "
        "(status for the lane itself, stand-down and reap for the steward)"
    ),
    "repair_fails_validate": (
        "REGISTRY.md #Ownership: repair may not add a `validate` violation the board did not "
        "already have, nor leave the replaced line's own violation in place"
    ),
    "repair_heading_skeleton": (
        "REGISTRY.md #Questions: repair fixes a malformed LINE; it may not add, remove or rename "
        "a section or block heading (the registry's heading skeleton), in any declared section"
    ),
    "repair_not_a_row": (
        "REGISTRY.md #What-repair-may-not-touch: a --new-line written into a table section "
        "(Agents, Queue, Blockers) must be a row of that table, or blank; in Queue the Deploy "
        "lock / Notes line is allowed too, when no intact one is already there"
    ),
    "repair_note_line": (
        "REGISTRY.md #What-repair-may-not-touch: a Queue Note bullet is never a repair target "
        "(except to restore a mangled Deploy lock / Notes line); Notes are owned by "
        "`note --strike` and the cull, not by repair"
    ),
    # BUILT FROM THE TUPLE, never hand-copied (same reason as queue_state_enum above).
    "repair_question_field": (
        "REGISTRY.md #Questions: repair fixes STRUCTURE; no repair may change a human-decision "
        "field BY ANY ROUTE (" + "|".join(_Q_PROTECTED_FIELDS) + "), nor a raiser-owned one ("
        + "|".join(_Q_RAISER_FIELDS) + ")"
    ),
    "repair_decision_record": (
        "REGISTRY.md #Questions: no repair, in ANY declared section, may alter the PARSED "
        "decision record: a question may not lose its id, change "
        + "|".join(_Q_PROTECTED_FIELDS + _Q_RAISER_FIELDS) + ", change "
        + "|".join(_Q_BINDING_FIELDS) + " once it has left `open`, change "
        + "|".join(_Q_RETRACTION_FIELDS) + " on a retracted row, or appear already answered"
    ),
    "q_answer_human_relay": "REGISTRY.md #Questions: only dev-manager relays a human answer (--by human); other managers answer as themselves",
    "q_answer_by_self": "REGISTRY.md #Questions: a manager answers as itself -- --by steward is bound to --actor steward, --by dev-manager to --actor dev-manager",
    "directive_ack_required": "REGISTRY.md #Directives: a guarded action acknowledges every ACTIVE Directive (--ack-directive)",
    "directive_ack_no_match": "REGISTRY.md #Directives: --ack-directive must match exactly one active Directive (no-match is an error)",
    "directive_ack_ambiguous": "REGISTRY.md #Directives: --ack-directive matched more than one active Directive (ambiguous)",
    "directive_expire_needs_target": "REGISTRY.md #Directives: expire-directive needs --match (by content) or --index",
    "directive_text_empty": "REGISTRY.md #Directives: directive --text requires non-empty text",
    "directive_expire_empty": "REGISTRY.md #Directives: expire-directive --match requires a non-empty substring",
    "directive_expire_exclusive": "REGISTRY.md #Directives: expire-directive takes --match OR --index, never both",
    "directive_expire_no_match": "REGISTRY.md #Directives: expire-directive --match matched no active Directive",
    "directive_expire_ambiguous": "REGISTRY.md #Directives: expire-directive --match matched more than one active Directive",
}


# --------------------------------------------------------------------------- #
# Actor capability — which loop may run which verb, as DATA
# --------------------------------------------------------------------------- #
#: Every ROLE restriction in this CLI is one row of `ACTOR_CAPABILITY` below, and
#: `_require_capability` is the only place any of them is raised from. That single-source shape
#: is the point of the table, not a tidiness preference:
#:
#: The restrictions used to be a dozen hand-written `if a.actor != "..."` guards spread across
#: 1400 lines, so nothing could be asked "who may run `expire-directive`?" without reading the
#: function -- and a spec could therefore assign a step to a lane the CLI would refuse, with no
#: reader in the chain positioned to notice. That happened twice on consecutive epics (p190 and
#: p195, both on `expire-directive`): each spec routed the step correctly, and the step still
#: never happened. Prose drifted the same way in the other direction -- gh#219 records eleven
#: sites across six files still saying dev-manager culls, years after `cull` became steward-only.
#:
#: A hand-maintained dict sitting BESIDE the guards would drift exactly like that prose. So the
#: guards READ this table. There is no second copy of the answer to disagree with it.
#:
#: Two kinds of restriction live here and they are not interchangeable:
#:
#: * ``actors`` is a ROLE restriction -- decidable from the verb alone, before any registry is
#:   read. This is the half a spec's scope item can be checked against.
#: * ``bound_by`` names a DATA restriction (this row's claimant, this blocker's owner, this
#:   lock's holder). No static table can answer it, so it is enforced at the verb's own site and
#:   recorded here only so a reader of `read --capabilities` is not told a verb is open when in
#:   practice one loop holds it.
#:
#: The table is TOTAL over the command surface: unrestricted verbs carry ``actors=AGENTS``
#: explicitly, so "no entry" can never be mistaken for "no restriction". A missing key is a
#: KeyError at the call site, which is what a programming error should be. `test_registry.py`
#: asserts the two directions of that totality against the argument parser itself.
@dataclass(frozen=True)
class Capability:
    """One verb's actor restriction, and the refusal it raises."""

    #: The loops permitted to run this verb. `AGENTS` in full means "no role restriction".
    actors: tuple[str, ...]
    #: `RULES` key cited on refusal. Empty for an unrestricted verb (nothing to cite).
    rule: str = ""
    #: The refusal detail, formatted with ``actor=``. Byte-identical to what the hand-written
    #: guard raised before it moved here -- a lane that greps its own transcript for a refusal
    #: it has seen before must still find it.
    denial: str = ""
    #: The ownership gate the verb ALSO applies, if any. Enforced at the verb's own site, which
    #: may read ``janitors`` from here (`note`, `assign-blocker`); this string is the report's.
    bound_by: str = ""
    #: Loops exempt from ``bound_by`` because the fleet made them janitors of that surface.
    #: Read by the guard that applies the binding, so this stays the single source too.
    janitors: tuple[str, ...] = ()
    #: Flag-level narrowings: ``"<flag spelling>" -> Capability``. The verb is open, but ONE
    #: argument value is not. Enforced through `_require_capability(..., condition=...)` from
    #: the site that can see the argument.
    conditions: dict[str, Capability] = field(default_factory=dict)
    #: How a spec's prose is likely to name this verb, lowercased. The promotion-time check in
    #: `dev-manager` §7c matches scope-item text against these, so a restricted verb named in
    #: English rather than in CLI spelling is still caught.
    #:
    #: MATCHED AS A WORD SET, NOT AS A SUBSTRING -- see `_spelling_matches`. Substring matching
    #: was tried first and scored zero on both instances this epic exists to catch: p190 item 5
    #: reads "expire the interim directive" (an adjective interposed) and p195 item 3 reads
    #: "reconcile the directive and expire it" (the noun ahead of the verb). A matching surface
    #: that fails on its own ground truth is worse than none, because the report is published to
    #: §7c as authoritative.
    spellings: tuple[str, ...] = ()

    @property
    def restricted(self) -> bool:
        """True when some fleet loop may NOT run this verb."""
        return set(self.actors) != set(AGENTS)

    @property
    def every_builder(self) -> bool:
        """True when EVERY builder lane may run this verb.

        The promotion-time predicate. Promotion cannot know which lane will build the epic --
        that is `lead-dev`'s later call -- so the only question decidable there is whether the
        item is safe in *whichever* lane gets it. `set(actors) & BUILDERS` (is SOME builder
        granted it?) is the tempting spelling and is wrong: `publish`, `relane` and `withdraw`
        are granted to `lead-dev` alone, so a scope item naming one of them passes that test
        and is still unexecutable the moment the epic is laned to a junior -- p190/p195's exact
        shape, one lane over.
        """
        return set(BUILDERS) <= set(self.actors)


#: Documented CLI spelling -> Capability. Keys match `docs/REGISTRY.md`'s command surface,
#: including the three `deploy-lock` ops, because that is the spelling a spec's author reads.
ACTOR_CAPABILITY: dict[str, Capability] = {
    "status": Capability(
        actors=AGENTS,
        bound_by="rewrites ONLY the actor's own Agents row",
        spellings=("registry status",),
    ),
    "claim": Capability(
        actors=BUILDERS,
        rule="queue_lane_discipline",
        denial="{actor} owns no builder lane",
        bound_by="only a ready, unclaimed row in the actor's OWN lane",
        spellings=("claim the row", "claim a queue row", "registry claim"),
    ),
    "release": Capability(
        actors=AGENTS,
        bound_by="only the row's claimant",
        spellings=("release the row", "registry release"),
    ),
    "publish": Capability(
        actors=("lead-dev",),
        rule="queue_publish_leaddev",
        denial="{actor} may not publish Queue work",
        spellings=("publish the epic", "publish work", "publish into a lane", "registry publish"),
    ),
    "relane": Capability(
        actors=("lead-dev",),
        rule="queue_relane_leaddev",
        denial="{actor} may not relane Queue work",
        bound_by="only an UNCLAIMED, still-ready row",
        spellings=("relane", "re-lane", "move the row to another lane"),
    ),
    "withdraw": Capability(
        actors=("lead-dev",),
        rule="queue_withdraw_leaddev",
        denial="{actor} may not withdraw Queue work",
        bound_by="only an UNCLAIMED, still-ready row",
        spellings=("withdraw the row", "retract the publication", "registry withdraw"),
    ),
    "ask": Capability(
        actors=AGENTS,
        conditions={
            "--urgency halted": Capability(
                actors=tuple(a for a in AGENTS if a != "auditor"),
                rule="q_auditor_not_halted",
                denial="auditor questions are waiting/advisory only",
            ),
        },
        spellings=("raise a question", "registry ask"),
    ),
    "answer": Capability(
        actors=("steward", "dev-manager"),
        rule="q_answer_manager",
        denial="{actor} may not answer questions",
        bound_by="only an OPEN question",
        conditions={
            "--by human": Capability(
                actors=("dev-manager",),
                rule="q_answer_human_relay",
                denial=(
                    "{actor} may not record an answer as `--by human`: only dev-manager relays a "
                    "human decision, because it is the only seat that asks one. Answer as "
                    "`--by {actor}` if this is your own call, or leave it open for the pump."
                ),
            ),
            # gh#719: each manager value is bound to its own role, as `--by human` is above.
            # Without these either manager could sign the other's name and the audit trail
            # would attribute a decision to a seat that never made it.
            "--by steward": Capability(
                actors=("steward",),
                rule="q_answer_by_self",
                denial=(
                    "{actor} may not record an answer as `--by steward`: answered_by names who "
                    "decided, and a manager signs only its own name. Answer as `--by {actor}`."
                ),
            ),
            "--by dev-manager": Capability(
                actors=("dev-manager",),
                rule="q_answer_by_self",
                denial=(
                    "{actor} may not record an answer as `--by dev-manager`: answered_by names "
                    "who decided, and a manager signs only its own name. Answer as `--by {actor}`."
                ),
            ),
        },
        spellings=("answer the question", "registry answer"),
    ),
    "apply": Capability(
        actors=AGENTS,
        bound_by="only the question's raiser, and only once answered",
        spellings=("apply the answer", "registry apply"),
    ),
    "retract": Capability(
        actors=AGENTS,
        bound_by="only the question's raiser, and only while it is still open",
        spellings=("retract the question", "registry retract"),
    ),
    "raise-blocker": Capability(
        actors=AGENTS,
        spellings=("raise a blocker", "registry raise-blocker"),
    ),
    "resolve-blocker": Capability(
        actors=AGENTS,
        bound_by="only the blocker's assigned owner",
        spellings=("resolve the blocker", "registry resolve-blocker"),
    ),
    "assign-blocker": Capability(
        actors=("steward", "dev-manager"),
        rule="blocker_assign_manager",
        denial="{actor} may not assign blocker ownership",
        # The exemption below covers the OWNERSHIP clause only; the open-state rule is stated
        # inside the same string as applying to every actor, because the report prints the
        # janitor line straight after this one and would otherwise read as exempting it too.
        bound_by=(
            "only a blocker the actor owns or one with no owner "
            "(and, for every actor including the exempt one, only an OPEN blocker)"
        ),
        # dev-manager re-assigns any blocker; the steward re-pointing another seat's would
        # override that seat's call.
        janitors=("dev-manager",),
        spellings=("assign the blocker", "assign blocker ownership", "registry assign-blocker"),
    ),
    "directive": Capability(
        actors=("dev-manager",),
        rule="directive_devmanager",
        denial="{actor} may not write Directives",
        spellings=("write a directive", "record a directive", "registry directive"),
    ),
    "expire-directive": Capability(
        actors=("dev-manager",),
        rule="directive_devmanager",
        denial="{actor} may not expire Directives",
        spellings=("expire the directive", "expire a directive", "expire-directive"),
    ),
    "cull": Capability(
        actors=("steward",),
        rule="cull_steward",
        denial="{actor} may not cull",
        spellings=("cull", "sweep the registry to the log", "move closed entries to the log"),
    ),
    "deploy-lock take": Capability(
        actors=BUILDERS,
        rule="deploy_builder",
        denial="{actor} is not a builder",
        bound_by="refused while a live builder holds it (45-min + dead-heartbeat stale rule); "
                 "breaking a STALE hold needs --break-stale",
        spellings=("take the deploy lock", "deploy-lock take"),
    ),
    "deploy-lock release": Capability(
        actors=AGENTS,
        bound_by="only the current holder",
        spellings=("release the deploy lock", "deploy-lock release"),
    ),
    "deploy-lock assert": Capability(
        actors=BUILDERS,
        rule="deploy_builder",
        denial="{actor} is not a builder",
        bound_by="exit 5 unless the actor holds it and the hold is not stale",
        spellings=("assert the deploy lock", "deploy-lock assert", "prove you hold the deploy lock"),
    ),
    "note": Capability(
        actors=AGENTS,
        bound_by="--strike consumes only a Note routed TO the actor (or to `fleet`)",
        janitors=("dev-manager",),
        spellings=("route a note", "strike the note", "registry note"),
    ),
    "reap": Capability(
        actors=("steward",),
        rule="reap_steward",
        denial="{actor} may not reap loops",
        spellings=("reap the loop", "reap a dead lane", "registry reap"),
    ),
    "stand-down": Capability(
        actors=("steward",),
        rule="stand_down_steward",
        denial="{actor} may not stand a lane down",
        spellings=("stand the lane down", "stand-down", "stand down a lane"),
    ),
    "repair": Capability(
        actors=("steward",),
        rule="repair_steward",
        denial="{actor} may not repair rows",
        spellings=("repair the row", "repair a malformed row", "registry repair"),
    ),
    "preflight": Capability(
        actors=AGENTS,
        spellings=("preflight", "list the active directives"),
    ),
    "read": Capability(actors=AGENTS, spellings=("registry read",)),
    "validate": Capability(actors=AGENTS, spellings=("registry validate",)),
    "init": Capability(actors=AGENTS, spellings=("registry init",)),
}


def _require_capability(verb: str, actor: str, *, condition: Optional[str] = None) -> None:
    """Refuse ``actor`` the verb (or the flag value) the capability map does not grant it.

    The ONLY place a role restriction is raised. `ACTOR_CAPABILITY` is therefore not a
    description of the guards, it IS the guards -- the two cannot disagree, because there is
    only one of them.

    A ``verb`` with no entry raises KeyError rather than passing: a table that silently
    permitted whatever it had not heard of would be trusted and wrong, which is worse than
    having no table. The parser-coverage test makes that unreachable in practice.
    """
    cap = ACTOR_CAPABILITY[verb]
    if condition is not None:
        cap = cap.conditions[condition]
    if actor not in cap.actors:
        raise RegistryError(cap.rule, cap.denial.format(actor=actor))


#: Words carrying no verb identity, dropped before a spelling is matched. Deliberately tiny:
#: every word left in a spelling is REQUIRED to appear, so each addition here weakens every
#: spelling at once. These six are the ones that broke the two ground-truth items.
_SPELLING_STOPWORDS = frozenset({"a", "an", "the", "it", "into", "to"})

_WORD = re.compile(r"[a-z0-9][a-z0-9-]*")


def _spelling_words(spelling: str) -> list[str]:
    return [w for w in _WORD.findall(spelling.lower()) if w not in _SPELLING_STOPWORDS]


def _spelling_matches(spelling: str, text: str) -> bool:
    """True when every content word of ``spelling`` appears in ``text``, in any order.

    NOT a substring test. A spec writes English, not CLI: p190 item 5 says "expire the interim
    directive", p195 item 3 says "reconcile the directive and expire it". Substring matching on
    the phrase "expire the directive" misses both -- an interposed adjective in one, an inverted
    order in the other -- so the check scored zero on the only two items with ground truth.
    Word-set matching catches both and stays cheap to reason about: the words are all there, in
    some arrangement.
    """
    present = set(_WORD.findall(text.lower()))
    words = _spelling_words(spelling)
    return bool(words) and all(w in present for w in words)


def scope_item_hits(text: str) -> list[tuple[str, Capability]]:
    """Restricted verbs a scope item's prose names, in map order — spec item 2's matcher.

    Reports; never blocks. A cross-actor step is legitimate as long as it is visible and
    assigned, so the output of this is a sentence in a promotion turn, not a gate.
    """
    return [
        (verb, cap)
        for verb, cap in ACTOR_CAPABILITY.items()
        if cap.restricted and any(_spelling_matches(s, text) for s in cap.spellings)
    ]


def _capable_actor_note(cap: Capability) -> str:
    """Why a verb is (or is not) safe in a scope item, for a seat that does not know the lane."""
    if cap.every_builder:
        return "every builder lane may run it"
    if not set(cap.actors) & set(BUILDERS):
        return "no builder lane may run it"
    missing = [b for b in BUILDERS if b not in cap.actors]
    return f"{', '.join(missing)} may not run it, so it breaks if the epic lands in those lanes"


def scope_item_report(items: list[str]) -> list[str]:
    """`read --capabilities --scope-item ...` — the promotion check, run rather than eyeballed.

    dev-manager §7c used to say "match the item text against the spellings by eye". A check a
    human performs from memory on a report they must first mentally re-derive is the check that
    gets skipped on the pass where it matters, which is the failure mode this whole epic is
    named after. The matching rule lives in one place and is executable.
    """
    lines: list[str] = []
    flagged = 0
    for n, text in enumerate(items, start=1):
        hits = scope_item_hits(text)
        head = text if len(text) <= 72 else text[:69] + "..."
        if not hits:
            lines.append(f'scope item {n} ("{head}") -- names no restricted verb')
            continue
        if any(not cap.every_builder for _, cap in hits):
            flagged += 1
        lines.append(f'scope item {n} ("{head}")')
        for verb, cap in hits:
            lines.append(f"  verb:          {verb}")
            lines.append(f"  capable actor: {', '.join(cap.actors)}")
            if cap.every_builder:
                lines.append(f"  verdict:       OK -- {_capable_actor_note(cap)}")
            else:
                lines.append(f"  verdict:       FLAG -- {_capable_actor_note(cap)}")
                lines.append(
                    "  remedy:        rewrite as a hand-off with a named owner "
                    '(docs/REGISTRY.md, "Cross-actor scope items")'
                )
    lines.append("")
    lines.append(
        f"{len(items)} scope item(s) scanned, {flagged} FLAGGED. "
        "This REPORTS; it never blocks -- promote anyway if the epic is otherwise ready."
    )
    return lines


def capability_report() -> list[str]:
    """The capability map rendered for a human reader — `read --capabilities`.

    Restricted verbs first and labelled as such, because the one question this report exists to
    answer is "may the lane that will build this epic run this step?", and a reader scanning for
    that must not have to filter the open verbs out by hand first.
    """
    lines: list[str] = []
    restricted = [(v, c) for v, c in ACTOR_CAPABILITY.items() if c.restricted]
    open_verbs = [(v, c) for v, c in ACTOR_CAPABILITY.items() if not c.restricted]

    lines.append(
        "RESTRICTED VERBS -- a scope item naming one of these can be run ONLY by the actors listed."
    )
    lines.append(
        "  FLAG a scope item whose verb is not granted to EVERY builder lane -- promotion cannot "
        "know which"
    )
    lines.append(
        "  lane will build it, so a verb only SOME builders hold is unexecutable the moment it "
        "lands elsewhere."
    )
    lines.append(
        "  'spec prose' matches as a WORD SET: every listed word present in the item, any order, "
        "anything between."
    )
    for verb, cap in restricted:
        lines.append(f"  {verb}: {', '.join(cap.actors)}")
        lines.append(
            f"      promotion: {'OK -- every builder lane may run it' if cap.every_builder else 'FLAG -- ' + _capable_actor_note(cap)}"
        )
        if cap.bound_by:
            lines.append(f"      also bound by: {cap.bound_by}")
        if cap.janitors:
            lines.append(f"      exempt from that binding: {', '.join(cap.janitors)}")
        lines.append(f"      spec prose: {'; '.join(cap.spellings)}")
    lines.append("")
    lines.append(
        "OPEN VERBS -- any fleet loop may run; several are still bound by who owns the row."
    )
    for verb, cap in open_verbs:
        lines.append(f"  {verb}: any")
        if cap.bound_by:
            lines.append(f"      bound by: {cap.bound_by}")
        if cap.janitors:
            lines.append(f"      exempt from that binding: {', '.join(cap.janitors)}")
    conditions = [
        (verb, flag, sub)
        for verb, cap in ACTOR_CAPABILITY.items()
        for flag, sub in cap.conditions.items()
    ]
    if conditions:
        lines.append("")
        lines.append("FLAG-LEVEL RESTRICTIONS -- the verb is open, one argument value is not.")
        for verb, flag, sub in conditions:
            lines.append(f"  {verb} {flag}: {', '.join(sub.actors)}")
    return lines


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class RegistryError(Exception):
    """A refused write — carries the ``REGISTRY.md`` rule it violated."""

    def __init__(self, rule_key: str, detail: str = ""):
        self.rule_key = rule_key
        self.rule = RULES.get(rule_key, rule_key)
        self.detail = detail
        msg = f"refused [{rule_key}]: {self.rule}"
        if detail:
            msg += f"\n  {detail}"
        super().__init__(msg)


class LockHeldError(RuntimeError):
    """The registry edit-lock is held by a holder not yet judged dead (exit 3)."""


class EditLockStolenError(LockHeldError):
    """This command's own edit-lock was stolen, or the board changed under it, before its write.

    A LockHeldError because the recovery is the same retry (exit 3), but it carries a refusal
    key so a caller can tell "someone else is writing" from "my write was discarded".
    """

    rule_key = "edit_lock_stolen"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"refused [{self.rule_key}]: {RULES[self.rule_key]}\n  {detail}")


class DirectiveAckError(Exception):
    """A guarded mutation refused for want of an explicit Directive acknowledgement (exit 4).

    Deliberately NOT a RegistryError. Exit 2 means "you may not do this" (ownership or
    schema); exit 4 means "you may, once you have read what the human is standing on."
    A caller that collapsed the two would retry the wrong recovery.
    """

    def __init__(self, rule_key: str, detail: str = ""):
        self.rule_key = rule_key
        self.rule = RULES.get(rule_key, rule_key)
        self.detail = detail
        msg = f"refused [{rule_key}]: {self.rule}"
        if detail:
            msg += f"\n  {detail}"
        super().__init__(msg)


class DeployAssertError(Exception):
    """`deploy-lock assert` answered no: the caller does not hold the deploy lock (exit 5).

    Deliberately NOT a RegistryError. Exit 2 means the command was malformed or forbidden
    (unknown actor, unreadable file); exit 5 means the command was well-formed and the
    answer is simply "you do not hold it". A caller that chains `assert && merge` must be
    able to tell those apart: a typo and a lock another lane is holding call for opposite
    recoveries, and collapsing them onto 2 would make every malformed invocation look like
    a lock refusal — and every lock refusal look like a typo worth retrying.
    """

    def __init__(self, rule_key: str, detail: str = ""):
        self.rule_key = rule_key
        self.rule = RULES.get(rule_key, rule_key)
        self.detail = detail
        msg = f"refused [{rule_key}]: {self.rule}"
        if detail:
            msg += f"\n  {detail}"
        super().__init__(msg)


# --------------------------------------------------------------------------- #
# Clock helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    """Minute-precision UTC stamp matching the REGISTRY.md examples (2026-08-05T14:10Z)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")


def _parse_iso(value: str) -> Optional[datetime]:
    """Parse a registry ISO stamp (Z or offset). Returns None on '-'/blank/unparseable."""
    if not value or value.strip() in ("-", ""):
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# --------------------------------------------------------------------------- #
# Section splitting (byte-stable splice)
# --------------------------------------------------------------------------- #
_SECTION_RE = re.compile(r"^## (Agents|Queue|Questions|Blockers|Directives)[ \t]*$", re.M)


def _section_bounds(text: str) -> dict[str, tuple[int, int]]:
    """Map section name -> (body_start, section_end).

    body_start is the offset just after the heading line's newline; section_end is
    the start of the next known heading (or EOF). Splicing ``text[:body_start] +
    new_body + text[section_end:]`` leaves every other section byte-for-byte intact.
    """
    matches = list(_SECTION_RE.finditer(text))
    out: dict[str, tuple[int, int]] = {}
    for i, m in enumerate(matches):
        body_start = m.end()
        if text[body_start : body_start + 1] == "\n":
            body_start += 1
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group(1)] = (body_start, end)
    return out


#: The recovery for a lost section or table, and ONLY for that: row damage names its row instead.
_MISSING_STRUCTURE_ADVICE = (
    "a required section/table is missing; restore it (git history, or re-add the heading), "
    "never `init`"
)


def _section_missing(name: str) -> RegistryError:
    return RegistryError(
        "file_damaged", f"section '## {name}' not found in registry - {_MISSING_STRUCTURE_ADVICE}"
    )


def _get_body(text: str, name: str) -> str:
    bounds = _section_bounds(text)
    if name not in bounds:
        # `file_damaged`, never `file_missing`: the file is right here and full of content.
        # The old key's advice — "run registry init" — was the single action an operator
        # must not take while the real content is still on disk (gh#134).
        raise _section_missing(name)
    start, end = bounds[name]
    return text[start:end]


def _replace_body(text: str, name: str, new_body: str) -> str:
    bounds = _section_bounds(text)
    if name not in bounds:
        raise _section_missing(name)
    start, end = bounds[name]
    return text[:start] + new_body + text[end:]


# --------------------------------------------------------------------------- #
# Table row helpers (Agents / Queue / Blockers)
# --------------------------------------------------------------------------- #
def _cells(line: str) -> Optional[list[str]]:
    """Split a markdown table row into stripped cells, or None if not a table row."""
    if "|" not in line or not line.strip().startswith("|"):
        return None
    parts = line.split("|")
    # Drop the leading and trailing empty fragments from the boundary pipes.
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def _fmt_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _separator_index(body: str) -> Optional[int]:
    """Line index of a section body's table separator, or None when the TABLE is gone.

    THE MODULE'S ONE NOTION OF TABLE PRESENCE. AN EMPTY TABLE IS NOT A DAMAGED ONE, and the
    separator is what tells them apart: a section whose header and separator survive but
    which carries no data rows is the ordinary resting state of `## Blockers`; a section
    that lost its separator lost the table itself. `_require_intact_table` (the question
    asked before a lookup) and `_append_data_row` (the write that needs the separator's
    position) both read this so they can never drift into two answers.
    """
    for i, line in enumerate(body.split("\n")):
        cells = _cells(line)
        if cells is not None and _is_separator(cells):
            return i
    return None


def _table_lost(section: str) -> RegistryError:
    """The section survived but its table did not: damage, not absence. Defined once so the
    two raisers stay byte-identical for the operator."""
    return RegistryError(
        "file_damaged", f"table in section '## {section}' not found - {_MISSING_STRUCTURE_ADVICE}"
    )


def _require_intact_table(text: str, section: str) -> str:
    """Raise `file_damaged` when `## <section>` exists but its TABLE does not; return the body.

    THE ONE PREDICATE gh#245 ASKED FOR. Three verbs used to infer structural damage from the
    shape of their own lookup, and each got it wrong in a different direction: `status` told
    the operator to run `init` on a registry still full of content, and `claim` sent them
    hunting a missing ROW when the whole TABLE was gone. A failed lookup cannot distinguish
    "the table has rows but not yours" from "the table has no rows at all", so the question
    has to be asked directly, before the lookup's own refusal fires.

    A missing SECTION is already `file_damaged` from `_get_body`, so this narrows to the case
    that slipped between them. The body comes back so a caller about to read it (validate)
    does not fetch it twice.
    """
    body = _get_body(text, section)
    if _separator_index(body) is None:
        raise _table_lost(section)
    return body


def _find_data_row(
    body: str, key_index: int, key_value: str, header0: str, *, line_offset: int = 0
) -> Optional[int]:
    """Return the line index of the data row whose ``cells[key_index] == key_value``.

    `line_offset` is how many FILE lines precede `body`; it only feeds the refusal below, so
    the operator is pointed at lines of the file they will open, not of a slice they never
    see (the two differed by 21 on the live board).
    """
    lines = body.split("\n")
    hits = []
    for i, line in enumerate(lines):
        cells = _cells(line)
        if cells is None or _is_separator(cells):
            continue
        if cells and cells[0] == header0:  # header row
            continue
        if len(cells) > key_index and cells[key_index] == key_value:
            hits.append(i)
    if len(hits) > 1:
        # THE SKEW gh#196 NAMES, MADE LOUD AT ITS SOURCE. `parse_registry` collapses a
        # section into a dict, so it is LAST-wins; this scan was FIRST-match. A verb
        # that judged a row through the parse and wrote it through here therefore
        # judged one line and edited a different one - measured on master: seven `E-`
        # rows collapsing to six entries, the parse reporting one lane while this scan
        # targeted the other. Refusing here reaches EVERY writer that resolves a row by
        # key - both `status` paths, `claim`, `release`, `relane`, `withdraw`, publish's
        # reclaim, `resolve-blocker`, `assign-blocker`, `stand-down` and `reap` all
        # come through `_replace_data_row` - which is what makes it the class fix rather
        # than a per-verb patch. (Verbs whose lane/claimant judgement would fire first
        # ask `_require_unique_key` before judging, so the refusal is the same one.)
        raise RegistryError(
            "id_duplicate",
            f"{key_value!r} appears on {len(hits)} rows of this section (lines "
            f"{', '.join(str(h + 1 + line_offset) for h in hits)}); the parser keeps the "
            f"last row and every write refuses until the duplicate is repaired (steward "
            f"repair)",
        )
    return hits[0] if hits else None


def _locate_data_row(
    text: str, section: str, key_index: int, key_value: str, header0: str
) -> tuple[str, Optional[int]]:
    """(body, row index) for the row keyed `key_value` in `## <section>`'s table.

    Takes the whole text rather than a body so the duplicate refusal can name FILE lines:
    the body's first line is `line_offset` lines into the file.
    """
    body = _get_body(text, section)
    line_offset = text.count("\n", 0, _section_bounds(text)[section][0])
    return body, _find_data_row(body, key_index, key_value, header0, line_offset=line_offset)


def _require_unique_key(text: str, section: str, key_index: int, key_value: str, header0: str) -> None:
    """Refuse `id_duplicate` when `key_value` is on more than one row, BEFORE a verb judges it.

    `claim`, `release`, `relane` and `withdraw` judge `reg.queue[id]` (the LAST duplicate)
    for lane and claimant before their write ever reaches `_find_data_row`, so on twins that
    differ in exactly those cells the operator got `queue_lane_discipline` or
    `queue_only_claimant` naming the wrong row. Asking first makes the refusal the same one
    the write would have given.
    """
    _locate_data_row(text, section, key_index, key_value, header0)


def _replace_data_row(
    text: str, section: str, key_index: int, key_value: str, header0: str, new_cells: list[str]
) -> str:
    """Rewrite one row of `## <section>`'s table; returns the NEW BODY for the caller to splice."""
    body, idx = _locate_data_row(text, section, key_index, key_value, header0)
    if idx is None:
        raise RegistryError("id_exists", f"no row with {header0}[{key_index}]={key_value!r}")
    lines = body.split("\n")
    lines[idx] = _fmt_row(new_cells)
    return "\n".join(lines)


def _append_data_row(body: str, header0: str, new_cells: list[str], section: str) -> str:
    """Append a row to a section's table. `section` exists only for the damage message.

    It cannot be derived from `header0`: the Queue and Blockers tables both lead with
    `id`, so naming the header cell made two different damaged sections produce one
    byte-identical refusal ("table for 'id' not found"). The section name is the one
    thing the operator can act on, and every call site already knows it.
    """
    lines = body.split("\n")
    sep_idx = _separator_index(body)
    if sep_idx is None:
        raise _table_lost(section)
    last_data = None
    for i, line in enumerate(lines):
        cells = _cells(line)
        if cells is None or _is_separator(cells):
            continue
        if cells and cells[0] == header0:  # header
            continue
        last_data = i
    insert_at = (last_data if last_data is not None else sep_idx) + 1
    lines.insert(insert_at, _fmt_row(new_cells))
    return "\n".join(lines)


def _table_rows(body: str, header0: str) -> list[list[str]]:
    rows = []
    for line in body.split("\n"):
        cells = _cells(line)
        if cells is None or _is_separator(cells):
            continue
        if cells and cells[0] == header0:
            continue
        rows.append(cells)
    return rows


# --------------------------------------------------------------------------- #
# Parsed model (read side — validation + `read` filters)
# --------------------------------------------------------------------------- #
@dataclass
class AgentRow:
    agent: str
    status: str
    work: str
    phase: str
    heartbeat: str
    eta: str
    waiting_on: str


@dataclass
class QueueRow:
    id: str
    work_item: str
    lane: str
    claimed_by: str
    state: str
    waiting_on: str
    notes: str


@dataclass
class Question:
    id: str
    fields: dict[str, str]


@dataclass
class Blocker:
    id: str
    raised_by: str
    owner: str
    work: str
    state: str
    description: str


@dataclass
class Registry:
    agents: dict[str, AgentRow] = field(default_factory=dict)
    queue: dict[str, QueueRow] = field(default_factory=dict)
    deploy_holder: str = "none"
    deploy_taken_at: str = "-"
    questions: dict[str, Question] = field(default_factory=dict)
    blockers: dict[str, Blocker] = field(default_factory=dict)
    directives: list[str] = field(default_factory=list)
    #: live + struck coordination Note bodies, in file order (the "~~" wrapper is kept:
    #: consumed-ness is data the stale sweep reads, not noise to strip at parse time).
    notes: list[str] = field(default_factory=list)


#: The row key each table's parser keeps, as (predicate, finding label, why). ONE copy, read by
#: `parse_registry` (which drops a row whose key fails) and by `_table_row_problems` (which
#: reports it), so validate and the parse can never disagree about which rows exist. A dropped
#: Queue row took its claim with it: the held set never saw it and its issue read as free.
_ROW_KEY_RULES = {
    "Agents": (
        lambda key: key in AGENTS, "bad agent", f"not one of the fleet's loops ({', '.join(AGENTS)})"
    ),
    "Queue": (lambda key: bool(_E_ID.match(key)), "bad id", "not an E-NNNN[-name] row id"),
    "Blockers": (lambda key: bool(_B_ID.match(key)), "bad id", "not a B-NNNN row id"),
}


def _row_key_ok(section: str, key: str) -> bool:
    return _ROW_KEY_RULES[section][0](key)


_QBLOCK_RE = re.compile(r"^### (Q-[0-9]{3,})[ \t]*$", re.M)
_QFIELD_RE = re.compile(r"^- ([a-z_]+):[ \t]*(.*)$")
#: The Queue's ``- **Deploy lock**`` field line, matched at LINE START. One spelling for the
#: field predicate (`_DEPLOY_FIELD_RE`) and the holder parse/rewrite (`_DEPLOY_RE`): an
#: unanchored parse read the holder out of any Note that merely quoted the field when the real
#: line was damaged or gone, and the rewrite then edited that Note in place.
#: Every separator is `[ \t]`, never `\s`: under re.M `\s` crosses newlines, so a hand-written
#: bare `-` line followed by field-shaped text on the next line read as the field. The trailing
#: lookahead stops the match at its own line end: a trailing `\s*` swallowed the newline of a
#: blank line after the field, so the rewrite deleted that line.
_DEPLOY_FIELD_PREFIX = r"^[ \t]*[-*][ \t]+\*\*Deploy lock\*\*"
_DEPLOY_RE = re.compile(
    _DEPLOY_FIELD_PREFIX
    + r".*?holder:[ \t]*([^,\n]+?),[ \t]*taken-at:[ \t]*(.+?)[ \t]*(?=\r?$)",
    re.M,
)
_DEPLOY_LINE_CANONICAL = "- **Deploy lock** — holder: none, taken-at: -"
#: Two recoveries, so two messages. A line that is gone cannot come back through `repair`
#: (a blank --old-line is refused), so naming repair alone sent every lane to a dead end. A
#: line whose prefix was mangled ("Deploy lok") is indistinguishable from a gone one here,
#: and `repair` CAN mend that one, so the message names both cases rather than guessing.
_DEPLOY_LINE_MISSING = (
    "Deploy lock line missing from Queue section - restore the `**Deploy lock**` line from git "
    f"history or the registry template (`{_DEPLOY_LINE_CANONICAL}`): `repair` cannot insert "
    "a line, and mends one only when a mangled copy is still on the board (steward `repair "
    "--section Queue --old-line <that line>`); never `init`"
)
_DEPLOY_LINE_UNREADABLE = (
    "Deploy lock line unreadable (no `holder: <lane>, taken-at: <ISO>`): {line!r} - steward "
    "`repair --section Queue --old-line <that line> --new-line <the line restored, e.g. "
    f"`{_DEPLOY_LINE_CANONICAL}`>`, never `init`"
)


def _deploy_line_problem(queue_body: str) -> Optional[str]:
    """None when *queue_body* has a readable Deploy lock line; else the damage and its recovery.

    "Unreadable" is a line the field predicate still names the Deploy lock line but the holder
    parse cannot read; "missing" is no such line at all. They differ in what can mend them.
    """
    if _DEPLOY_RE.search(queue_body) is not None:
        return None
    garbled = next((ln for ln in queue_body.split("\n") if _is_deploy_lock_field(ln)), None)
    if garbled is None:
        return _DEPLOY_LINE_MISSING
    return _DEPLOY_LINE_UNREADABLE.format(line=garbled.rstrip("\r"))


def parse_registry(text: str) -> Registry:
    """Parse the full registry into a structured model (read-side; edits stay text-surgical)."""
    reg = Registry()

    agents_body = _get_body(text, "Agents")
    for cells in _table_rows(agents_body, "agent"):
        if len(cells) >= 7 and _row_key_ok("Agents", cells[0]):
            reg.agents[cells[0]] = AgentRow(*cells[:7])

    queue_body = _get_body(text, "Queue")
    m = _DEPLOY_RE.search(queue_body)
    if m:
        reg.deploy_holder = m.group(1).strip()
        reg.deploy_taken_at = m.group(2).strip()
    for cells in _table_rows(queue_body, "id"):
        if len(cells) >= 7 and _row_key_ok("Queue", cells[0]):
            reg.queue[cells[0]] = QueueRow(*cells[:7])
    reg.notes = list(_iter_note_bodies(queue_body))

    questions_body = _get_body(text, "Questions")
    for block_id, block_text in _iter_qblocks(questions_body):
        fields = {}
        for line in block_text.split("\n"):
            fm = _QFIELD_RE.match(line)
            if fm:
                fields[fm.group(1)] = fm.group(2).strip()
        reg.questions[block_id] = Question(id=block_id, fields=fields)

    blockers_body = _get_body(text, "Blockers")
    for cells in _table_rows(blockers_body, "id"):
        if len(cells) >= 6 and _row_key_ok("Blockers", cells[0]):
            reg.blockers[cells[0]] = Blocker(*cells[:6])

    directives_body = _get_body(text, "Directives")
    for line in directives_body.split("\n"):
        if line.strip().startswith("- "):
            reg.directives.append(line.strip()[2:])

    return reg


def _is_struck_note(content: str) -> bool:
    """True only for the exact full-line strike wrapper ``~~...~~`` that ``note --strike`` writes.

    ``content`` is a Note line with its ``- `` bullet marker already dropped.

    Deliberately NOT the loose ``"~~" in content`` test. A perfectly ordinary note carries
    ``~~`` in its body — "move off ~~old~~ path now" — and under the loose test the cull sweep
    would read that stray wrapper as consumption and silently delete a LIVE note nobody had
    read. Only the shape strike itself produces counts as struck: wrapper at both ends, with a
    body between them. ``_strike_note`` applies the same test for the same reason, so what can
    be consumed and what can be collected are one set of lines by construction.
    """
    body = content.strip()
    return len(body) > 4 and body.startswith("~~") and body.endswith("~~")


_NOTES_HEADER_RE = re.compile(r"^(\s*[-*]\s+\*\*Notes\*\*)(\s*[—–-]\s*)?(.*)$")
_DEPLOY_FIELD_RE = re.compile(_DEPLOY_FIELD_PREFIX)


def _is_notes_header(line: str) -> bool:
    """True only for the Queue's ``- **Notes**`` header line itself.

    Anchored on purpose. Every note rule in this module used the SUBSTRING test
    ``"**Notes**" in line``, which also matched any NOTE whose body mentioned the marker --
    "the **Notes** header was lying" -- and such a note was then skipped by the striker, by the
    cull sweep and by the live count alike. It read as delivered and could never be consumed,
    which is gh#101's defect one layer down: the header was identified by what a line CONTAINS
    rather than by what it IS.

    It delegates to `_NOTES_HEADER_RE` rather than spelling the shape a second time. A hand
    written `-  **Notes**` (two spaces) is valid CommonMark, and hand-editing a Notes bullet is
    a documented direct-edit exception -- so a stricter `startswith` here than the regex used to
    REWRITE the line does not merely miss such a header, it appends a SECOND one, counts the
    orphaned original as a live note, and lets the striker and cull sweep carry it off. Two
    spellings of one question is how that happens; there is now one.
    """
    return _NOTES_HEADER_RE.match(line) is not None


def _is_deploy_lock_field(line: str) -> bool:
    """True only for the Queue's ``- **Deploy lock**`` field line itself.

    The twin of `_is_notes_header`, and it exists for the same reason rather than for symmetry.
    The note predicates read ``_is_notes_header(line) or "**Deploy lock**" in line``: the first
    half was anchored for gh#101 and the second left as a raw substring on the same expression,
    so a note whose BODY mentioned the deploy lock -- "take the **Deploy lock** before merging",
    ordinary fleet vocabulary every SKILL.md uses -- stayed invisible to the striker, the cull
    sweep and the count.

    That half is worse under a header that reports a NUMBER than it was under a constant. A
    header stuck at "(none)" made no claim about any particular note; a count that silently
    omits one is a specific false statement on the surface every loop reads at every wake,
    and it under-reports, so a lane drains what it can see and concludes it is done.
    """
    return _DEPLOY_FIELD_RE.match(line) is not None


def _iter_note_bodies(queue_body: str):
    """Yield each coordination Note body from the Queue section, bullet marker stripped.

    Same recognition rule the striker uses (`_strike_note`), so what the sweep reads and
    what `note --strike` can consume are by construction the same set of lines.
    """
    for line in queue_body.split("\n"):
        stripped = line.lstrip()
        if not stripped.startswith("- ") or _is_notes_header(line) or _is_deploy_lock_field(line):
            continue
        yield stripped[2:]


def _is_note_bullet(line: str) -> bool:
    """True when *line* is a coordination Note by `_iter_note_bodies`' rule (struck or live)."""
    return next(_iter_note_bodies(line), None) is not None


def _iter_qblocks(questions_body: str):
    """Yield (qid, block_text) for each ### Q-NNNN block in the Questions body."""
    matches = list(_QBLOCK_RE.finditer(questions_body))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(questions_body)
        yield m.group(1), questions_body[m.start() : end]


# --------------------------------------------------------------------------- #
# Read-side projections (PURE: no clock, no file, no lock — the CLI only prints them)
# --------------------------------------------------------------------------- #
#: Triage order. Position IS the rank; anything not listed sorts after all of them.
URGENCY_ORDER = ("halted", "waiting", "advisory")

_MIN_DT = datetime.min.replace(tzinfo=UTC)


def order_open_questions(questions) -> list[Question]:
    """Return the open questions in triage order: class first, then oldest-first within a class.

    Class beats recency on purpose. A halted loop raised its question one minute ago and is
    doing nothing until it is answered; an advisory raised last year already proceeded on its
    stated assumption. Answering the oldest first would leave the stopped loop stopped.

    An urgency outside ``URGENCY_ORDER`` is a malformed row nobody can triage — which is
    exactly why it is sorted LAST and never dropped. Dropping it would hide the malformation
    forever; ranking it would let a typo jump the queue. An unreadable ``raised_at`` cannot
    claim to be the oldest, so it sorts after the readable stamps in its own class.

    Accepts a mapping (``Registry.questions``) or any iterable of ``Question``.
    """
    if hasattr(questions, "values"):
        questions = questions.values()
    open_qs = [q for q in questions if q.fields.get("state") == "open"]

    def sort_key(indexed):
        i, q = indexed
        urgency = q.fields.get("urgency", "")
        rank = URGENCY_ORDER.index(urgency) if urgency in URGENCY_ORDER else len(URGENCY_ORDER)
        raised = _parse_iso(q.fields.get("raised_at", ""))
        # i last: equal keys keep input (file) order rather than shuffling between reads.
        return (rank, raised is None, raised or _MIN_DT, i)

    return [q for _, q in sorted(enumerate(open_qs), key=sort_key)]


def active_directives(reg: Registry) -> list[str]:
    """The standing Directives — every bullet not prefixed ``[expired ...]``, in file order.

    The marker is a PREFIX, anchored. ``_cmd_expire_directive`` writes it at the head of the
    bullet and ``parse_registry`` stores the bullet with its ``- `` removed, so an expired
    directive always *starts* ``[expired ``. A substring test would instead read the marker out
    of the human's own prose — "hold merges until the [expired token] bug is fixed" would count
    as expired and vanish from the guard. That failure is silent and it fails OPEN: the one
    directive most likely to mention expiry is a hold, and the guard would wave the merge
    through while reporting nothing to acknowledge.
    """
    return [d for d in reg.directives if not directive_text_is_expired(d)]


def directive_text_is_expired(text: str) -> bool:
    """Is this Directive's text (the bullet WITHOUT its ``- ``) an expired one?

    THE ONE PREDICATE. The marker is a PREFIX, anchored — ``_cmd_expire_directive`` writes it at
    the head of the bullet. An unanchored ``"[expired" in text`` reads the marker out of the
    HUMAN'S OWN PROSE: a live directive reading "hold merges until the [expired token] bug is
    fixed" would count as expired.

    That is not hypothetical drift. Three places used to decide this and only this one was
    anchored — ``_cmd_expire_directive`` skipped such a row (so ``--index`` addressed a different
    list than the operator counted from, which is gh#88's failure mode intact) and ``_cmd_cull``
    DELETED it as consumed. Same divergence-of-copies shape as gh#57. There is one now.
    """
    return text.lstrip().startswith("[expired")


def _bullet_text(line: str) -> str:
    """The Directive text inside a raw ``- <text>`` file line, matching what ``parse_registry``
    stores. Both matchers reach their searchable surface through this, so ``--match`` and
    ``--ack-directive`` are the same set by construction rather than by claim."""
    s = line.strip()
    return s[2:] if s.startswith("- ") else s


#: A leading bracketed token, e.g. ``[2026-08-05T15:00Z]``. Whether it is a STAMP is decided by
#: `_parse_iso`, not by this shape: `[URGENT]` and `[expired ...]` are human/marker text and must
#: survive into the searchable surface.
_LEADING_BRACKET_RE = re.compile(r"^\[([^\]]*)\]\s*")


def directive_match_text(text: str) -> str:
    """The Directive surface a needle may be quoted from — its TEXT, minus the minted stamp.

    THE ONE SEARCHABLE SURFACE, shared by ``--ack-directive`` and ``expire-directive --match``.
    Both guards exist to make the caller quote a standing instruction back, which is only
    possible after reading it. ``_cmd_directive`` mints every Directive as ``[<ISO>] <text>``,
    so the bullet the human never wrote carried a distinct, guaranteed-unambiguous needle the
    CLI generated ITSELF: eight active Directives on the live board were acknowledged with eight
    timestamp fragments, the deploy lock taken, and not a word read (gh#127). That is the guard
    certifying a reading that did not happen — on the human's veto channel over every autonomous
    merge. Machine metadata has no business in a surface whose whole job is to prove human
    content was read, so it is removed here and a stamp-only needle now matches nothing and is
    refused by the existing no-match branch.

    Stripping is decided by CONTENT, not by shape: the leading bracket comes off only when
    `_parse_iso` — the file's one stamp parser — reads it as a timestamp. A human bullet opening
    ``[URGENT]`` keeps those words, and widening the minter's format later cannot silently
    reopen the hole the way an anchored ``%Y-%m-%dT%H:%MZ`` regex could.

    A date the HUMAN wrote in the body stays matchable, deliberately. Quoting it is quoting the
    body, which is a read; refusing "needles that look like dates" would be a heuristic guessing
    at meaning, and it would eventually refuse an honest quote from a Directive that is *about*
    a date. The bypass this closes was that the CLI GUARANTEED a needle for every Directive; no
    guarantee survives, and what is left requires having read the text.
    """
    stripped = text.lstrip()
    m = _LEADING_BRACKET_RE.match(stripped)
    if m and _parse_iso(m.group(1)) is not None:
        return stripped[m.end():]
    return stripped


def directive_containment_hits(new_text: str, active: list[str]) -> list[str]:
    """Active Directives whose TEXT contains, or is contained by, *new_text* (gh#236).

    The hazard, stated once: `_require_directive_acks` judges a needle's ambiguity against the
    WHOLE active board (gh#128), so when one active Directive's text is a substring of another's,
    every substring of the shorter is also a substring of the longer and the SHORTER has no
    unambiguous needle at all. `deploy-lock take` then refuses for every actor and no lane can
    merge until a human expires one of the two and restates it. This function is how the write
    path sees that coming while rewording is still free.

    BOTH ORIENTATIONS. New-inside-existing and existing-inside-new create the identical ack
    problem — which of the pair is the shorter one does not depend on which was written first.

    THE TWO SIDES HAVE DIFFERENT SHAPES, AND COMPARING THEM ALIKE IS A HOLE (gh#236 review).
    *active* holds BULLETS, which carry the CLI's minted ``[<ISO>]`` stamp, so that side must be
    put through `directive_match_text` — otherwise two Directives written in the same minute read
    as related through metadata neither human wrote. *new_text* is NOT a bullet: `_cmd_directive`
    writes ``- [<minted>] {new_text}``, so whatever is passed here becomes the row's text
    VERBATIM and is what `directive_match_text` will return for it later.

    Running the new side through `directive_match_text` therefore strips a SECOND stamp that the
    board keeps, and an operator who pastes a stamped string — which this CLI hands them, in
    `directive_ack_required`'s quoted bullets, in `expired:` output, and in this very warning's
    own "restate it" remedy — writes a row that IS a strict substring of another and gets no
    warning at all. Measured: the pair then wedged `deploy-lock take` at rc 4 for every lane,
    silently. Compare the new side RAW.

    IDENTICAL TEXT IS DELIBERATELY NOT A HIT. It is contained both ways, but it is not this
    wedge: `_require_directive_acks`'s duplicate branch decides distinctness on this same surface
    and lets one needle per copy discharge them, so nothing needs rewording and a warning there
    would be noise on the one path that already works.
    """
    # RAW, not `directive_match_text` — see the docstring. This is the text the board will hold.
    needle = new_text.strip()
    # Unreachable from the CLI: `_cmd_directive` refuses an empty --text before it gets here
    # (`directive_text_empty`), because "" is a substring of every text and is therefore the
    # MAXIMAL containment case rather than an absent one. Kept as a guard for direct callers.
    if not needle:
        return []
    hits = []
    for d in active:
        other = directive_match_text(d).strip()
        if not other or other == needle:
            continue
        if needle in other or other in needle:
            hits.append(d)
    return hits


def publish_title_warning(title: str) -> str | None:
    """A warning when a `publish --title` cites an issue other than as lowercase `gh#N`, or None.

    WARNS, never refuses: the held predicate reads `GH#N` and a bare `#N` too (`HELD_REF_RE`),
    so the row is held either way, and refusing would break a publisher mid-wake over spelling.
    The canonical form is still what the other board readers and a grep expect.
    """
    odd = [m.group(0) for m in HELD_REF_RE.finditer(title or "")
           if not m.group(0).startswith("gh#")]
    if not odd:
        return None
    return (
        f"warning: --title cites {', '.join(odd)}; write an issue reference as lowercase "
        "gh#<N> (e.g. gh#12, not GH#12 or #12). The row WAS published and is held."
    )


def directive_containment_warning(new_text: str, active: list[str]) -> str | None:
    """The operator-facing warning for `directive_containment_hits`, or None when there are none.

    It WARNS and never refuses. The command a human reaches for to fix a wedged board is this
    one, and refusing here would put a second wedge in the middle of the repair — so the
    Directive is written, the exit stays 0, and the human is told what they just built.

    It names WHICH existing Directive and what follows from it. "Similar to an existing
    Directive" teaches nothing at the one moment rewording costs nothing.
    """
    hits = directive_containment_hits(new_text, active)
    if not hits:
        return None
    quoted = "\n".join(f"  existing: {d}" for d in hits)
    return (
        f"warning: this Directive and {len(hits)} existing active "
        f"{'Directive' if len(hits) == 1 else 'Directives'} contain one another:\n"
        f"  new:      {new_text}\n"
        f"{quoted}\n"
        "warning: every substring of the shorter is also a substring of the longer, so the "
        "SHORTER of each pair has no unambiguous --ack-directive needle, and `deploy-lock take` "
        "will refuse for EVERY lane until a human expires one and restates it. The Directive WAS "
        "written and this command succeeded - reword it now (free before anyone acknowledges "
        "it), or `expire-directive` one of the pair and restate it in words the other does not "
        "contain."
    )


#: How a stale sighting is reported. 'flag' = first sighting; 'reap-eligible' = a live liveness
#: ping from the reaper, routed exactly at this agent, stamped after its current heartbeat - so
#: this silence has been seen before. It licenses the life probe, never the reap by itself.
STALE_FLAG = "flag"
STALE_REAP_ELIGIBLE = "reap-eligible"
#: A stale row a human stood down on purpose (gh#683). It stays in the report -- it may still hold
#: claims and the deploy lock -- but it is never a ping-then-reap candidate: its life probe always
#: finds the process gone, because a human stopped it. The steward escalates it to dev-manager.
STALE_STOOD_DOWN = "stood-down"

#: Opening of the liveness ping (skills/steward/SKILL.md, the "Ping on first sight" passage).
#: Matched at the START of the parsed body, never as a substring anywhere (gh#285).
LIVENESS_PING_OPENING = "heartbeat stale, first detection"


@dataclass
class StaleRow:
    """One Agents row that is dead on BOTH liveness signals, plus how to report it."""

    agent: str
    mark: str
    heartbeat: str
    eta: str
    heartbeat_age_hours: float
    row: AgentRow


def _has_live_stale_note(notes: list[str], agent: str, since: Optional[datetime] = None) -> bool:
    """True when an un-struck liveness ping about THIS agent's current silence exists.

    The steward sends the ping (since 0c2a04f / PR 507): the lane that reaps is the lane that
    pings. gh#285: the old test - "-> <agent>" anywhere in the line plus "stale" anywhere -
    promoted a lane on any note that merely said stale ("HOT-CACHE STALE after retry"), or on a
    body that quoted a routing arrow. Three anchors now, all read from the parsed note:

    - routing: the parsed header's recipient is exactly ``agent``, never an arrow in the body;
    - opening: the body STARTS with ``LIVENESS_PING_OPENING``;
    - sender: the header's actor holds the ``reap`` capability, so a lane quoting the ping back
      at the steward cannot promote the steward's own row.

    ``since`` scopes the ping to one episode of silence: only a stamp strictly after the lane's
    current heartbeat counts. The steward cannot strike its own ping (``note_not_yours``), so
    this is what retires it once the lane is relaunched (gh#379) or resumes stamping after an
    alive probe. Strict ``>``: a real ping is at least heartbeat + 3h, so a same-minute stamp is
    a live write, and ignoring it can only delay a reap. An unparseable stamp never counts.
    """
    pingers = ACTOR_CAPABILITY["reap"].actors
    for n in notes:
        raw = n.strip()
        if _is_struck_note(raw):  # the full-line wrapper only; a stray ~~ in a body is not a strike
            continue
        p = _parse_note_prefix(raw)
        if p is None or p.actor not in pingers or p.to != agent:
            continue
        if not p.body.lower().startswith(LIVENESS_PING_OPENING):
            continue
        if since is not None and not p.stamp > since:  # the ping belongs to an earlier silence
            continue
        return True
    return False


def stale_rows(registry, now: datetime, threshold_hours: float = DEFAULT_STALE_HOURS) -> list[StaleRow]:
    """Agents rows whose heartbeat is older than ``threshold_hours`` AND whose eta has
    LAPSED OR IS ABSENT.

    Both signals, never one alone: a stale heartbeat by itself reaps the loop doing the
    slowest honest work in the fleet — the one that promised a late eta and is keeping it —
    and a lapsed eta by itself reaps a loop that is overrunning but visibly breathing.

    "Or is absent" is the load-bearing half, and it is quoted from the binding rule
    (docs/REGISTRY.md: "a heartbeat staler than 3h with the eta passed **or absent**").
    ``--eta`` is optional on every documented ``status`` call site and ``_cmd_status`` defaults
    the cell to '-', so an absent eta was not an edge case — it was the ordinary shape of a
    row, and it is exactly what a loop that died early leaves behind. (`eta_required` and
    the `--heartbeat-only` guard have since made an absent eta unreachable through the
    supported write paths, so it is no longer *ordinary* — but it remains REACHABLE, via
    `init`, via `directive`, and via a hand-edited or partially-written row, which is
    precisely why this function must keep tolerating it rather than requiring a parse.) Requiring a parseable eta
    made this function blind to the only kind of corpse it was written to find: at the time
    that bug was caught, the live registry's own steward row had been dead for eleven hours
    with eta '-' and would never have been reported.

    A row with an unreadable HEARTBEAT is still skipped — staleness is measured from it, and a
    freshly initialized registry writes '-' there, so a fresh install must not report its whole
    roster as dead. ``now`` is INJECTED so the rule is testable at an exact boundary and this
    function never reads a clock of its own.

    EXACTLY ONE KIND OF PHASE IS EXCLUDED -- a reaped one (``is_reaped_phase``) -- and the two
    near-misses beside it are the substance of gh#313, not omissions.

    * ``reaped`` IS skipped. ``reap`` preserves the dead eta as evidence and the heartbeat
      freezes, so a reaped row meets both staleness conditions forever; every reap used to add
      one permanent row to this report. The lane is dead by definition and ``reap`` has already
      cleared its claim and the deploy lock, so nothing is left to watch.
    * ``stood down`` is NOT skipped, though ``missed_wakes`` and ``blind_rows`` skip it via
      ``_row_is_excluded_phase``. ``stand-down`` is explicitly NOT a reap: it leaves claims and
      the deploy lock exactly as the lane left them, and its own docstring names this report as
      the residual net over a stood-down lane that never returns. Dropping it here would leave a
      row holding the lock every merge serializes on, watched by nothing. It is marked
      ``STALE_STOOD_DOWN`` rather than flag/reap-eligible (gh#683): it is escalated, never
      pinged or reaped, and ``reap`` refuses it without ``--override-stand-down``.
    * ``halted`` is NOT skipped either, for a different reason again. A halted row is the one
      status exempt from ``eta``, so it has precisely the absent-eta shape described above, and
      excluding it would re-create the blindness that once hid an eleven-hour-dead steward row.
      A halted lane keeps firing by contract, so a fresh heartbeat is the healthy shape and is
      never reported; a stopped one is the corpse this function exists to find.

    The rule underneath all three: skip a row only when something ELSE is already watching what
    it was holding.

    Report-only: the reap stays the steward's. Returns stalest-first. Accepts a parsed
    ``Registry`` or the raw registry text.
    """
    reg = parse_registry(registry) if isinstance(registry, str) else registry
    out: list[StaleRow] = []
    for agent, row in reg.agents.items():
        # A REAPED ROW IS NOT A CORPSE TO FIND - IT IS ONE ALREADY BURIED (gh#313). `reap`
        # preserves the dead eta as evidence and the heartbeat freezes where it stopped, so a
        # reaped row satisfies both staleness conditions permanently: every reap - the
        # sanctioned remedy - used to make this alarm one row noisier forever.
        #
        # REAPED ONLY, deliberately NOT `_row_is_excluded_phase`, which also covers stood-down.
        # The two verbs have opposite claim semantics and `_cmd_stand_down` says so in its own
        # body: "NOT reap: claims and the deploy lock are left exactly as the lane left them...
        # a stood-down holder wedges the fleet silently". Reap clears both; stand-down keeps
        # them, and its docstring names THIS report as the residual net over a stood-down lane
        # that never comes back. Excluding it here would remove the only surface watching a row
        # that can hold the lock every merge in the fleet serializes on.
        if is_reaped_phase(row.phase):
            continue
        hb = _parse_iso(row.heartbeat)
        if hb is None:
            continue
        eta = _parse_iso(row.eta)
        # gh#707: an eta counts only up to ETA_HORIZON_HOURS past the row's OWN heartbeat -- the
        # furthest wake an honest lane can declare, measured the way `blind_rows` measures it.
        # Otherwise a dead lane with a far eta (a hand edit, a row older than the horizon) hides
        # from this report, and so from ping-then-reap, until the clock catches up with it.
        if eta is not None and agent not in ETA_HORIZON_EXEMPT and _eta_beyond_horizon(eta, hb):
            eta = hb + timedelta(hours=ETA_HORIZON_HOURS)
        age_hours = (now - hb).total_seconds() / 3600.0
        if age_hours <= threshold_hours or (eta is not None and eta > now):
            continue
        if row.phase.strip().startswith(STOOD_DOWN_PHASE):
            mark = STALE_STOOD_DOWN
        elif agent in ETA_HORIZON_EXEMPT:
            # The human's seat stays listed, because a seat away past its declared return is
            # worth reporting, but it is never due the life probe: it holds no claims or lock,
            # so a reap recovers nothing, and `reap` refuses it (`reap_exempt_seat`).
            mark = STALE_FLAG
        elif _has_live_stale_note(reg.notes, agent, since=hb):
            mark = STALE_REAP_ELIGIBLE
        else:
            mark = STALE_FLAG
        out.append(StaleRow(agent, mark, row.heartbeat, row.eta, age_hours, row))
    out.sort(key=lambda s: s.heartbeat_age_hours, reverse=True)
    return out


@dataclass
class MissedWake:
    """One Agents row whose OWN declared wake did not land."""

    agent: str
    eta: str
    heartbeat: str
    overdue_minutes: float
    row: AgentRow


def _row_is_excluded_phase(phase: str) -> bool:
    """Phases that put a row outside the missed-wake alarm, judged on the row's own text.

    Two were already here and a third joins them for the same reason:

    * ``REAPED_PHASE`` -- ``reap`` preserves the dead eta as evidence, so a reaped row is
      permanently past an eta it will never answer. Without this it alarms forever.
    * ``STOOD_DOWN_PHASE`` -- a human deliberately stopped this lane. gh#98: on 2026-09-01 two
      lanes were cleared to open a quiet window for a migration, and both carried live etas no
      session would answer, so the alarm was about to page for an intentional act.

    (``halted`` is the third exclusion but lives in ``status``, not ``phase``, so it is tested
    beside this rather than inside it.)

    **Why a phase marker and not a list somewhere.** Every exclusion here is readable off the row
    itself, so anyone diffing the registry can see WHY a lane is not being alarmed about. An
    exclusion held in a config file, or in the operator's memory of what they stopped, is invisible
    at exactly the moment someone is asking why the alarm stayed quiet.
    """
    p = phase.strip()
    # PREFIX for the stand-down marker, because the verb writes "<marker>: <reason>" -- the
    # reason has to live in the cell or the row does not say why it is excluded, which is the
    # whole point. Written as equality first, this predicate could never have matched a single
    # real row: the fix would have been perfectly vacuous and the tests below are built to
    # fail if it goes back.
    #
    # Anchored at the head, never a substring search, for the reason gh#88 landed on
    # `directive_text_is_expired` in THIS module: an unanchored test reads the marker out of a
    # lane's own phase prose. This is the
    # same trust model REAPED_PHASE already has -- a lane could type the marker into --phase
    # and exclude itself -- and it is not hardened here, because the registry is a cooperating
    # fleet's shared notebook, not an authorization boundary. What it does buy is that the
    # exclusion is READABLE: the row says who excluded it and why.
    return is_reaped_phase(p) or p.startswith(STOOD_DOWN_PHASE)


def missed_wakes(registry, now: datetime, grace_minutes: float = MISSED_WAKE_GRACE_MIN):
    """Agents rows past their OWN declared ``eta`` with no heartbeat since it.

    This is NOT ``stale_rows`` with a shorter fuse, and conflating them loses the point.
    ``stale_rows`` answers *is this lane dead* -- 3h of silence AND a lapsed-or-absent eta.
    This answers *did the wake this lane promised actually fire*, which is a different and
    much earlier question: on 2026-08-29 every loop in the fleet stopped waking while every
    process stayed alive, and nothing noticed for 32 hours because no rule was watching the
    one thing each lane had already told the board it would do.

    Two conditions, both required:

    * ``now`` is past ``eta`` by more than ``grace_minutes``; and
    * ``heartbeat`` is NOT newer than ``eta`` -- a lane that woke on time stamped on its way
      through, so a heartbeat after the eta is proof the wake fired even if the row is
      otherwise untidy.

    Three exclusions, all load-bearing rather than tidiness. The numeral has drifted once
    already, so it is stated once here and nowhere else:

    * ``halted`` -- the row has ALREADY told the human it is blocked: the transition into
      ``halted`` fires one ntfy (again only if it re-points to a different question).
      Alarming would page twice for one event, and an alarm that repeats something already
      reported is how a human learns to swipe the alarm away.
      This is NOT because a halted lane stops waking -- it keeps firing on schedule, and a later
      wake is how the answer reaches it (gh#148, gh#264). What unblocks it is the answer, not
      the wake, so an unanswered eta on it is not the failure this alarm exists to catch.
    * a stood-down row (``phase`` STARTS WITH ``STOOD_DOWN_PHASE`` -- the verb always appends
      ``: <reason>``, so equality would match nothing, which is what the first cut did) -- a
      human stopped this lane on purpose,
      so its unanswered eta is an intended fact rather than a missed wake. Every false page on a
      deliberate act teaches the reader to discount the alarm, and this alarm's whole value is in
      being believed: it exists because a 32-hour fleet-wide stall went unnoticed.
    * a reaped row (``is_reaped_phase(phase)``) -- ``reap`` deliberately PRESERVES the dead
      row's eta as stale evidence, so a reaped row is permanently past an eta it will never
      answer. Without this it would alarm on every sweep, forever.

    An unreadable heartbeat is skipped, matching ``stale_rows``: a freshly provisioned
    registry writes '-' there and must not report its whole roster as missing wakes.

    ``now`` is INJECTED so the boundary is testable exactly and this function never reads a
    clock of its own. Report-only -- it never writes, never notifies, never reaps. Returns
    most-overdue first.
    """
    reg = parse_registry(registry) if isinstance(registry, str) else registry
    out: list[MissedWake] = []
    for agent, row in reg.agents.items():
        if row.status == "halted" or _row_is_excluded_phase(row.phase):
            continue
        eta = _parse_iso(row.eta)
        if eta is None:
            # Not alarmable -- there is no declared wake to be late for -- but NOT nothing.
            # `blind_rows()` reports these separately; see its docstring for why silence here
            # was worse than a false alarm.
            continue
        hb = _parse_iso(row.heartbeat)
        if hb is None or hb > eta:
            continue
        overdue = (now - eta).total_seconds() / 60.0
        if overdue <= grace_minutes:
            continue
        out.append(MissedWake(agent, row.eta, row.heartbeat, overdue, row))
    out.sort(key=lambda m: m.overdue_minutes, reverse=True)
    return out


@dataclass
class LapsedEta:
    """One Agents row that is BREATHING but has stopped saying when it will wake next."""

    agent: str
    eta: str
    heartbeat: str
    lapsed_minutes: float
    row: AgentRow


def lapsed_etas(registry, now: datetime, grace_minutes: float = LAPSED_ETA_GRACE_MIN):
    """Agents rows past their own ``eta`` that ARE still stamping a heartbeat.

    THE HOLE THIS FILLS (gh#281). ``missed_wakes`` skips a row whose heartbeat is newer than
    its eta, on the reasoning that a heartbeat after the eta proves the wake fired. That is
    true about the PAST wake and says nothing about the next one. A lane that re-stamps
    ``heartbeat`` on every pass while never advancing ``eta`` therefore satisfies
    ``eta_required`` exactly once and is then reported by nobody, at any threshold, forever:
    the issue's reproduction ran 30 simulated days at a 6-minute staleness threshold and got
    ``stale=[] missed=[]`` at every sample. ``eta`` is the field the whole missed-wake design
    rests on, and the design had no check that it was still being maintained.

    A DISTINCT CLASS, not more ``MissedWake`` rows, because the two conditions take DIFFERENT
    REMEDIES and the output is what tells a reader which one to apply:

    * ``MISSED WAKE`` -- the wake did not happen. Go and probe the lane; it may be dead.
    * ``LAPSED ETA`` -- the wake happened and the lane is alive; it just never re-declared.
      Tell that lane to send one full ``status --eta``.

    Merging them into one count would destroy the only information that distinguishes those,
    which is the same argument ``blind_rows`` makes about folding its rows into the alarm.

    DISJOINT FROM ``missed_wakes`` BY CONSTRUCTION, not by coincidence. That function skips
    ``hb is None or hb > eta``; this one requires ``hb is not None and hb > eta``. The two
    predicates partition the rows with a parseable eta past its grace, so no row can appear in
    both and no such row can appear in neither. ``hb is None`` belongs to neither and is
    ``blind_rows``' ``no-heartbeat``, for the same reason it is there today.

    REGARDLESS OF HEARTBEAT AGE, and that is deliberate rather than an oversight about
    ``stale_rows``. A row can be both 5h silent and carrying a lapsed eta, and ``stale_rows``
    would name it -- but ``stale_rows`` is not on this surface: ``agentflow.wake_watch``, the
    one thing that reports when no loop is awake, calls the missed-wake detectors and never the
    stale one. Subtracting the stale rows here to avoid a second mention on a DIFFERENT report
    would make the daemon silent about precisely the deadest rows, which is the defect this
    function exists to remove, one layer out.

    SAME THREE EXCLUSIONS as ``missed_wakes`` and ``blind_rows``, reusing the same spellings:
    ``halted`` (the one status exempt from ``eta`` by contract -- ``_cmd_status`` waives
    ``eta_required`` on ``status`` alone, so an unadvanced eta there is the documented shape,
    not a defect), plus reaped and stood-down via ``_row_is_excluded_phase``. Both halves are
    needed: phase alone re-opens gh#98, status alone re-opens the page-forever class on reaped
    rows, whose preserved dead eta and frozen heartbeat can sit in exactly this shape.

    ITS OWN GRACE, ``LAPSED_ETA_GRACE_MIN``, not the wake alarm's -- see that constant for why a
    live lane needs a far longer rope than a silent one.

    ``now`` is INJECTED. Report-only. Returns most-lapsed first.
    """
    reg = parse_registry(registry) if isinstance(registry, str) else registry
    out: list[LapsedEta] = []
    for agent, row in reg.agents.items():
        if row.status == "halted" or _row_is_excluded_phase(row.phase):
            continue
        eta = _parse_iso(row.eta)
        if eta is None:
            continue  # no declared wake to have gone stale; `blind_rows()` reports it
        hb = _parse_iso(row.heartbeat)
        if hb is None:
            continue  # `blind_rows()` reports it as `no-heartbeat`
        if hb <= eta:
            continue  # `missed_wakes()` owns this row; reporting it here would double-report
        lapsed = (now - eta).total_seconds() / 60.0
        if lapsed <= grace_minutes:
            continue
        out.append(LapsedEta(agent, row.eta, row.heartbeat, lapsed, row))
    out.sort(key=lambda le: le.lapsed_minutes, reverse=True)
    return out


@dataclass(frozen=True)
class BlindRow:
    """One Agents row the missed-wake alarm structurally CANNOT judge.

    ``reason`` names WHICH of the alarm's two silent skips swallowed this row -- ``"no-eta"``
    (the eta cell does not parse) or ``"no-heartbeat"`` (the eta is fine but the heartbeat is
    unreadable, so "did the wake fire" has no evidence either way) -- or ``"eta-too-far"`` (both
    parse, but the eta lies more than ``ETA_HORIZON_HOURS`` past the heartbeat, so the alarm
    would wait that long before judging it, gh#682). Reporting the row without the reason would
    send a reader to the wrong cell.
    """

    agent: str
    eta: str
    heartbeat: str
    reason: str
    row: AgentRow


@dataclass
class StrandedHalt:
    """One `## Agents` row OBSERVED at ``halted``, joined to the open question it names.

    ``urgency`` is carried for the reader and is NEVER read to decide. That distinction is the
    whole epic: the strand check used to key on the question's declared ``urgency: halted``, a
    field written exactly once in ``_cmd_ask`` and reclassified by no verb anywhere, so on
    2026-09-02 the only halt on the board was invisible to the alarm meant to catch a stranded
    halt -- the lane read ``halted`` while its question still read ``urgency: waiting``. An
    alarm that reads the board cannot be forgotten; one that reads a declared field can only
    ever be as current as whoever last remembered to update it.
    """

    agent: str
    question: str
    urgency: str
    raised_at: str
    age_hours: float
    row: object


@dataclass
class UnjudgeableHalt:
    """A halted row whose wait CANNOT be timed, and why.

    Same argument as ``BlindRow`` one alarm over: a row the check could not judge is not a row
    the check cleared, and reporting only the fired rows makes those two silences identical.
    ``reason`` names which cell to go and look at.
    """

    agent: str
    waiting_on: str
    reason: str
    row: object


def _is_alarmable_halt(row) -> bool:
    """A `halted` row this alarm family may speak for.

    THE EXCLUSION IS THE SAME ONE `missed_wakes` AND `blind_rows` KEEP, and leaving it out here
    re-created gh#98 in a parallel alarm: `stand-down` rewrites ONLY the phase cell and preserves
    `status` and `waiting_on` verbatim -- its whole purpose is to make the alarms stop paging for
    a lane a human deliberately stopped. Without this guard a stood-down halted lane pages every
    wake, forever, with a monotonically growing age: a human clearing a lane producing exactly
    the signal of that lane dying.

    `reap` escaped by accident rather than by rule (it happens to write `status: idle`), so this
    predicate is what makes the exclusion true by construction for both verbs.

    Every caller uses THIS function -- including the all-clear's count. A count computed with a
    different predicate than the check it reports on is a number that lies about its own
    coverage, which is the second half of what this epic exists to fix.
    """
    return row.status == "halted" and not _row_is_excluded_phase(row.phase)


def stranded_halts(registry, now: datetime, threshold_hours: float = STRAND_THRESHOLD_HOURS):
    """Halted lanes waiting longer than ``threshold_hours`` on an open question, worst first.

    KEYED ON OBSERVED STATE, NEVER ON A DECLARED ONE. The row's own ``status`` cell and the
    question's own ``state`` cell are both written by the parties whose reality they describe
    and are updated whenever that reality changes; ``urgency`` is a claim made once at raise
    time. Re-keying was preferred over adding a reclassification verb because a verb still
    needs someone to remember to call it at the moment the world moves, which is the same
    declared-field failure with an extra step.

    Age is measured from the QUESTION's ``raised_at``, not from the lane's heartbeat: the thing
    being timed is how long an answer has been owed, and a halted lane keeps firing on schedule
    by contract, so its heartbeat stays fresh however long it waits.

    Report-only; never writes.
    """
    reg = parse_registry(registry) if isinstance(registry, str) else registry
    out: list[StrandedHalt] = []
    for agent, row in reg.agents.items():
        if not _is_alarmable_halt(row):
            continue
        q = reg.questions.get(row.waiting_on.strip())
        if q is None or q.fields.get("state", "").strip() != "open":
            continue  # `unjudgeable_halts()` reports these; see its docstring
        raised = _parse_iso(q.fields.get("raised_at", ""))
        if raised is None:
            continue  # likewise
        age = (now - raised).total_seconds() / 3600.0
        if age <= threshold_hours:
            # A FUTURE `raised_at` lands here with a NEGATIVE age and would fall silently
            # through both functions -- reported by neither, counted as judged. That is the
            # exact silence this epic exists to remove, so it is unjudgeable instead
            # (`raised-at-in-future`), not merely "not yet old enough".
            continue
        out.append(StrandedHalt(
            agent=agent,
            question=q.id,
            urgency=q.fields.get("urgency", "-"),
            raised_at=q.fields.get("raised_at", "-"),
            age_hours=age,
            row=row,
        ))
    out.sort(key=lambda s: (-s.age_hours, s.agent))
    return out


#: Why a `halted` row cannot be aged, and what each one means for the reader. Kept as a mapping
#: rather than prose so the enumeration and the messages cannot drift apart -- an earlier draft's
#: docstring said "three reasons" while the code emitted four.
UNJUDGEABLE_REASONS = {
    "no-referent":
        "halted with no waiting_on id at all - nothing can ever answer it",
    "referent-missing":
        "waiting_on names an id that is in neither ## Questions nor ## Blockers",
    "question-closed":
        "waiting_on names a question that is answered or applied - nobody is still working it",
    "question-retracted":
        "waiting_on names a question its raiser RETRACTED - the request was withdrawn, not "
        "answered, so this lane is waiting on nothing and needs a new question or a resume",
    "blocker-resolved":
        "waiting_on names a blocker that is already resolved - nobody is still working it",
    "blocker-referent":
        "waits on a LIVE blocker, which has an owner and a resolve verb but carries no "
        "timestamp - so this wait is real and simply cannot be aged here",
    "raised-at-unreadable":
        "the question's raised_at does not parse, so the wait cannot be timed",
    "raised-at-in-future":
        "the question's raised_at is in the future, so its age is negative and no threshold "
        "can ever be crossed",
}


def unjudgeable_halts(registry, now: Optional[datetime] = None):
    """Halted rows ``stranded_halts()`` cannot time, each with the reason a reader can act on.

    The reasons are ``UNJUDGEABLE_REASONS``; read them there rather than from a count in prose.

    **NOT all of these mean "no live referent".** An earlier draft said so and it was false in
    one case that matters: ``waiting_on`` is a ``Q-`` **or** ``B-`` id
    (``_require_live_waiting_on``, and ``_live_ids`` unions questions with unresolved blockers),
    so a lane halted on a live blocker
    is a fully sanctioned write. Reporting it as ``question-missing ... can never be answered``
    was wrong twice over -- the referent is not missing, and a blocker has an owner and a verb
    that resolves it. It gets its own reason, and the reason says the wait is real.

    Blockers carry no timestamp (``id``/``raised_by``/``owner``/``work``/``state``/
    ``description``), which is the honest limit: this alarm can see that the wait exists and
    cannot say how long it has been going on.

    Report-only; never writes. ``now`` is optional and only needed to separate a future
    ``raised_at`` from an ordinary one; omit it and that case reports as unreadable.
    """
    reg = parse_registry(registry) if isinstance(registry, str) else registry
    out: list[UnjudgeableHalt] = []
    for agent, row in reg.agents.items():
        if not _is_alarmable_halt(row):
            continue
        ref = row.waiting_on.strip()
        if ref in ("", "-"):
            reason = "no-referent"
        elif ref in reg.blockers:
            b = reg.blockers[ref]
            reason = "blocker-resolved" if b.state == "resolved" else "blocker-referent"
        else:
            q = reg.questions.get(ref)
            if q is None:
                reason = "referent-missing"
            elif q.fields.get("state", "").strip() == Q_RETRACTED:
                # ITS OWN REASON, so retracting cannot silently suppress a real halt. A lane
                # halted against a question its raiser retracted is stuck in a way nobody is
                # working, and folding that into `question-closed` would read as "somebody
                # answered it" -- the opposite of what happened.
                reason = "question-retracted"
            elif q.fields.get("state", "").strip() != "open":
                reason = "question-closed"
            else:
                raised = _parse_iso(q.fields.get("raised_at", ""))
                if raised is None:
                    reason = "raised-at-unreadable"
                elif now is not None and raised > now:
                    reason = "raised-at-in-future"
                else:
                    continue
        out.append(UnjudgeableHalt(agent=agent, waiting_on=row.waiting_on, reason=reason, row=row))
    out.sort(key=lambda u: u.agent)
    return out


def blind_rows(registry):
    """Rows `missed_wakes()` cannot see: ``eta`` does not parse, or lies beyond the horizon.

    THE ALARM'S SILENCE AND ITS ALL-CLEAR LOOKED IDENTICAL, AND THAT IS THE BUG. On
    2026-09-01 ``read --missed-wakes`` printed ``# no missed wakes`` against a live registry in
    which TWO of six lanes carried ``eta = -`` and one of them had been silent for 2.9 hours.
    The alarm was not wrong about the four it could see; it was mute about the two it could
    not, and the output gave a reader no way to tell those apart. An alarm that cannot
    distinguish "nothing is overdue" from "I cannot see a third of the fleet" is not a quiet
    alarm, it is a broken one -- and it is worse than no alarm, because it is trusted.

    This is deliberately a SEPARATE function returning a SEPARATE type rather than more
    ``MissedWake`` rows. A blind row is not overdue -- it has made no promise to break -- so
    folding it into the alarm list would report a lane as having missed a wake it never
    declared, which is a false positive, and false positives are how an alarm gets ignored.
    The caller reports both and conflates neither.

    Same three exclusions as the alarm, for the same reasons: ``halted`` legitimately carries
    no eta (it keeps waking, but no wake of its own unblocks it, and its transition already
    paged once), a reaped row is preserved dead evidence, and a
    stood-down row was stopped by a human on purpose.
    Report-only; never writes.
    """
    reg = parse_registry(registry) if isinstance(registry, str) else registry
    out: list[BlindRow] = []
    for agent, row in reg.agents.items():
        if row.status == "halted" or _row_is_excluded_phase(row.phase):
            continue
        eta_dt = _parse_iso(row.eta)
        eta_ok = eta_dt is not None
        hb = _parse_iso(row.heartbeat)
        if eta_ok and hb is not None:
            # gh#682: a parseable eta far past the row's own heartbeat is as blind as an
            # unparseable one -- the alarm waits for now to pass it, so the lane is unwatched
            # until then. Measured from the heartbeat, never the clock, so it is deterministic.
            if agent not in ETA_HORIZON_EXEMPT and _eta_beyond_horizon(eta_dt, hb):
                out.append(BlindRow(agent, row.eta, row.heartbeat, "eta-too-far", row))
            continue  # otherwise judgeable; the alarm speaks for it
        if not eta_ok and hb is None:
            # NEVER STARTED, not gone blind. `init` seeds every lane with '-' in both cells, so
            # without this a freshly provisioned registry reports its whole roster INVISIBLE on a
            # fleet that has not been launched. `missed_wakes()` guards the same case for the same
            # reason (see its docstring); omitting it here would reintroduce exactly the
            # false-positive class this function's own docstring argues against -- and six
            # INVISIBLE lines on an idle board is how a reader learns to skip INVISIBLE lines.
            continue
        reason = "no-eta" if not eta_ok else "no-heartbeat"
        out.append(BlindRow(agent, row.eta, row.heartbeat, reason, row))
    # Worst-first, matching `missed_wakes` and `stale_rows`: oldest heartbeat leads, and a row
    # with no readable heartbeat at all sorts ahead of every dated one.
    out.sort(key=lambda b: (_parse_iso(b.heartbeat) or datetime.min.replace(tzinfo=UTC), b.agent))
    return out


def judged_row_count(reg) -> int:
    """How many Agents rows the missed-wake alarm ACTUALLY LOOKED AT on this board.

    LOAD-BEARING, and the reason it is a function rather than `len(reg.agents)`: the alarm skips
    halted, reaped and stood-down rows before reading a cell, and skips rows whose eta or
    heartbeat does not parse. A count that included those would assert coverage over rows nobody
    judged -- a milder restatement of the false green the all-clear line exists to prevent, in
    the one line whose whole job is to be honest about coverage. A board where every lane has
    been stood down judges ZERO rows, and its all-clear has to say so.

    Shared by both reporters (`read --missed-wakes` and `agentflow.wake_watch`) rather than
    computed twice: two counts that agree today are two predicates, and they diverge on the day
    the exclusion set changes.
    """
    reg = parse_registry(reg) if isinstance(reg, str) else reg
    return sum(
        1
        for r in reg.agents.values()
        if r.status != "halted"
        and not _row_is_excluded_phase(r.phase)
        and _parse_iso(r.eta) is not None
        and _parse_iso(r.heartbeat) is not None
        # A row `blind_rows` reports as eta-too-far was not judged either (gh#682).
        and (r.agent in ETA_HORIZON_EXEMPT
             or not _eta_beyond_horizon(_parse_iso(r.eta), _parse_iso(r.heartbeat)))
    )


def missed_wake_line(m: MissedWake, grace_minutes: float) -> str:
    """The one wording for a missed wake, used by BOTH reporters.

    `read --missed-wakes` and `agentflow.wake_watch` said this in identical words by
    copy-paste, and the module docstring of the second claimed the sameness as a property an
    operator could rely on. A claim maintained by two people typing the same f-string goes
    false silently the first time one of them is edited -- so the sameness is now structural,
    and a reader who has seen one output is looking at the same builder that made the other.
    """
    return (
        f"{m.agent} MISSED WAKE eta={m.eta} (overdue {m.overdue_minutes:.0f}m > "
        f"{grace_minutes:g}m) last-heartbeat={m.heartbeat} status={m.row.status}"
    )


def lapsed_eta_line(le: LapsedEta, grace_minutes: float) -> str:
    """The one wording for a lapsed eta, used by BOTH reporters. Same argument as above.

    ``LAPSED ETA`` is the literal a reader distinguishes this class by, beside ``MISSED WAKE``
    and ``INVISIBLE to the missed-wake alarm``. The trailing clause carries the REMEDY, because
    the whole reason this is a separate class is that its remedy differs: a missed wake means
    go and probe a lane that may be dead, and this means tell a lane that is demonstrably alive
    to re-declare. A line that named the condition without the action would leave the reader to
    infer the difference from a word.
    """
    return (
        f"{le.agent} LAPSED ETA eta={le.eta} (lapsed {le.lapsed_minutes:.0f}m > "
        f"{grace_minutes:g}m) last-heartbeat={le.heartbeat} (NEWER than the eta -- the lane is "
        f"alive and has not re-declared; ask it for one full `status --eta`) "
        f"status={le.row.status}"
    )


def blind_row_line(b: BlindRow) -> str:
    """The one wording for a row the alarm cannot judge. Same argument as `missed_wake_line`."""
    if b.reason == "eta-too-far":
        why = f"eta={b.eta!r} is more than {ETA_HORIZON_HOURS:g}h past its heartbeat"
    else:
        cell = f"eta={b.eta!r}" if b.reason == "no-eta" else f"heartbeat={b.heartbeat!r}"
        why = f"{cell} unparseable"
    return (
        f"{b.agent} INVISIBLE to the missed-wake alarm ({b.reason}: {why}) "
        f"eta={b.eta} last-heartbeat={b.heartbeat} status={b.row.status}"
    )


# --------------------------------------------------------------------------- #
# ID allocation (max-scan of live registry + registry-log.md)
# --------------------------------------------------------------------------- #
#: Where an id is ALLOCATED, as distinct from where one is merely MENTIONED. Anchored on the exact
#: shapes the writers emit -- `_render_qblock` writes `### Q-NNNN` as a whole line, and `_fmt_row`
#: writes `| B-NNNN | ...` -- so this set is every id that has ever been handed out, and nothing
#: else.
#:
#: A NOTE LINE cannot produce one of these: `_require_inline` refuses both `|` and newlines in
#: the `text` field, so a note cannot become a table row and cannot start a `###` line. That is a
#: real guarantee, and it is narrower than "no prose can" -- two paths get round it, which is why
#: `_alloc_id` has a collision veto rather than relying on this alone:
#:   * `cull` inlines note-OVERFLOW files verbatim into the log, and those are arbitrary
#:     actor-authored markdown. `_strip_overflow` removes them before scanning.
#:   * `repair --new-line` writes a caller-supplied line straight into a section without passing
#:     through `_require_inline` -- deliberately, since repairing a malformed row requires
#:     emitting pipes.
#: THE INVARIANT: each anchor must be NO STRICTER than the reader that decides whether that line
#: is live. Over-counting is inflation, which is the bug being fixed here and merely untidy.
#: UNDER-counting is REUSE -- two rows with one id, every reference to it ambiguous -- which is
#: strictly worse than the bug. So when in doubt, match more.
#:
#: The first version of this got that backwards for `B`: it required `|` at column zero while
#: `_cells` accepts any leading whitespace, so an INDENTED blocker row was a fully live blocker
#: (`parse_registry` returns it, `read` lists it, `validate` blesses it) that the allocator alone
#: could not see -- and the next `raise-blocker` reissued its id. Reproduced through `repair`,
#: which is a sanctioned command that accepts any single line.
_ID_SITES = {
    # The parser's OWN pattern, not a restatement of it, so the two cannot drift apart.
    "Q": _QBLOCK_RE,
    # Mirrors `_cells`: `line.strip().startswith("|")`.
    "B": re.compile(r"^[ \t]*\|[ \t]*B-([0-9]{3,})[ \t]*\|", re.M),
    # Epic ids (E-0260). Same `_cells`-mirroring leading-whitespace tolerance as `B`, and it must
    # find BOTH shapes: the new `| E-0708-some-name |` that `publish` writes today, and the bare
    # legacy `| E-0220 |` rows that predate the allocator. Missing the legacy half would be REUSE
    # -- the live board and its log carry seventy-eight distinct legacy numbers up to E-0707, and
    # an allocator blind to them would start again at E-0001 and hand out ids that already name
    # other epics. The name is matched loosely (`[a-z0-9-]*`, so even a malformed `E-0708-` still
    # counts) on the docstring's own rule: over-counting is inflation, under-counting is reuse,
    # so when in doubt match more. The capture stays the DIGITS, which is what `_max_id` compares.
    "E": re.compile(r"^[ \t]*\|[ \t]*E-([0-9]{3,})(?:-[a-z0-9-]*)?[ \t]*\|", re.M),
}


#: Longest short-name `publish` will emit. Never fires on real input -- measured across all 459
#: spec slugs on this data root the longest four-token name is 41 characters -- so a slug that
#: trips it is malformed rather than merely wordy, and a refusal names it instead of silently
#: applying a second, undocumented truncation rule.
SHORT_NAME_MAX = 60

#: How many hyphen-tokens of the slug ride in the id. D1 asks for "2-6 words"; four is the
#: chosen point inside that range.
SHORT_NAME_TOKENS = 4


def _short_name(slug: str) -> str:
    """The name half of an epic id: the FIRST FOUR hyphen-tokens of the slug, and nothing cleverer.

    THE RULE IS DELIBERATELY DUMB. It has to be a pure, deterministic function of the slug,
    because two seats deriving different names from one spec would make the id stop being an
    identity -- and any rule with judgement in it (drop stop-words, keep the "interesting" tokens)
    is a rule two readers can apply differently. The spec's own recommendation, taken as written:
    predictable beats pretty. `an-epic-id-is-a-number-plus-a-name-never-its-priority` becomes
    `an-epic-id-is`, which reads poorly and resolves unambiguously, and the second property is the
    one an id needs.

    MEASURED CONSEQUENCE, stated rather than discovered later: across the 459 spec slugs on this
    data root, seven first-four-token names are shared by more than one spec -- all of the shape
    `acme-3-5-chat`, where numeric tokens eat the budget. None are in this project (agentflow:
    54 specs, zero collisions). A shared name is not a shared ID -- the number disambiguates, and
    `E-0708-acme-3-5-chat` and `E-0709-acme-3-5-chat` are two ids -- but `publish` keys its
    duplicate check and its withdrawn-row reclaim on this name, so two such specs cannot both be
    queued at once. That refuses loudly and names both slugs, which is the safe direction.

    Normalisation before truncation: anything that is not `a-z0-9` is a separator, so a slug typed
    with underscores, spaces or stray capitals derives the same name as its canonical spelling
    rather than a second one. Idempotent by construction -- the name of a name is that name.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", (slug or "").lower()) if t]
    if not tokens:
        raise RegistryError(
            "id_format",
            f"slug {slug!r} yields no name; publish needs a spec slug it can derive an id from",
        )
    name = "-".join(tokens[:SHORT_NAME_TOKENS])
    if len(name) > SHORT_NAME_MAX:
        raise RegistryError(
            "id_format",
            f"slug {slug!r} derives the {len(name)}-character name {name!r}, over the "
            f"{SHORT_NAME_MAX}-character limit; shorten the spec slug rather than the id",
        )
    return name


_OVERFLOW_SPAN = re.compile(
    r"<!-- overflow [^>]*? -->.*?<!-- end overflow [^>]*? -->", re.S
)


def _strip_overflow(text: str) -> str:
    """Remove inlined note-overflow bodies from log text before scanning it for ids.

    `cull` inlines an overflow file VERBATIM into the log -- it is about to become the only copy.
    So the log contains arbitrary actor-authored markdown, and a heading inside one would look
    exactly like a question block. That reproduces gh#38 through the back door: prose, in the file
    the allocator reads.

    Excluding these bodies cannot cause reuse, which is the axis that matters: no id is ever
    ALLOCATED inside an overflow body -- they are files someone wrote, never writer-emitted blocks
    or rows. And `_alloc_id`'s collision veto reads the UNSTRIPPED text, so an id merely mentioned
    in one still cannot be handed out twice.

    Non-greedy per span so two overflow blocks do not swallow the real log between them.
    """
    return _OVERFLOW_SPAN.sub("", text)


def _mentioned_ids(prefix: str, *texts: str) -> set[str]:
    """Every *prefix*-id appearing anywhere in *texts*, prose included. A VETO input only."""
    pat = re.compile(rf"\b{prefix}-[0-9]{{3,}}\b")
    out: set[str] = set()
    for t in texts:
        out.update(pat.findall(t))
    return out


def _max_id(prefix: str, *texts: str) -> int:
    r"""The highest id ever ALLOCATED for *prefix*, across the registry and its log.

    gh#38: this used to scan the whole file for `\bQ-\d{3,}\b`, which counts every id anyone
    QUOTED -- and `## Queue` Notes are prose living in that same file. So mentioning an id in a
    sentence permanently moved the counter, whether or not a row with that id ever existed.

    The memorable instance: a note explaining the hazard contained the sentence *"a note quoting
    Q-9999 would move this registry's next id to Q-10000 forever"*. The allocator read its own
    hypothetical and did it, which is why every id this fleet has issued since sits in the Q-100xx
    band while the real question count is in the dozens. (The live board's allocated-id sequence
    shows more than one discontinuity; this is the one whose cause is recorded. `B-` ids are far
    lower, but NOT because notes never quoted them -- they do, repeatedly. The quoted values just
    never exceeded the live maximum, which is luck rather than protection.)

    SCANNING THE LOG IS NOT OPTIONAL. `cull` moves consumed rows and blocks there verbatim, so an
    id absent from the live registry may still have been allocated. Dropping the log would make
    this reuse ids, which is far worse than inflating them -- and the log preserves the same
    structural shapes, so the anchors find them there too.
    """
    pat = _ID_SITES.get(prefix)
    if pat is None:
        # Loudly, not by falling back to the prose scan this function exists to remove. A new
        # allocated prefix needs its write shape added above, and the author needs to be told so.
        raise RegistryError(
            "id_format",
            f"no allocation site is defined for the {prefix!r} prefix; add its rendered shape to "
            "_ID_SITES so ids are counted where they are WRITTEN, never where they are quoted",
        )
    hi = 0
    for t in texts:
        for mm in pat.finditer(t):
            # The two patterns capture differently on purpose: `Q` reuses the PARSER'S regex, which
            # captures the whole id, while `B` is local and captures digits. Take the trailing
            # digits either way rather than forcing the parser's group shape to match ours -- that
            # pattern belongs to `_iter_qblocks`, and bending it here is how they drift apart.
            hi = max(hi, int(mm.group(1).rsplit("-", 1)[-1]))
    return hi


def _alloc_id(prefix: str, text: str, log_text: str, suffix: str = "") -> str:
    """Next id for *prefix*: the structural scan sets the number, a prose scan only VETOES.

    The two scans have opposite failure modes, so each is used only where its failure is safe.
    The structural scan decides the value, because letting prose drive it is gh#38. The prose scan
    cannot raise the number -- it can only reject a candidate that some text already names.

    That makes reuse impossible even if an anchor is wrong: an id the structural scan somehow
    missed is still SOMEWHERE in the text, so the veto sees it and steps past. An anchor bug
    degrades to inflation, which is the safe direction, instead of to two rows sharing an id.

    The veto reads the UNSTRIPPED log on purpose -- an id mentioned inside an inlined overflow
    body must not be handed out either, even though that body is excluded from the count.

    *suffix* is appended AFTER both scans and takes no part in either, which is what lets `E-`
    ids carry a name without weakening any of the above: both scans key on the NUMBER, so the
    veto still rejects a number that appears anywhere in the text no matter which name a mention
    of it happened to carry -- or whether it carried one at all, as every legacy `E-NNNN` does not.
    """
    n = _max_id(prefix, _strip_overflow(text), _strip_overflow(log_text)) + 1
    mentioned = _mentioned_ids(prefix, text, log_text)
    while f"{prefix}-{n:04d}" in mentioned:
        n += 1
    return f"{prefix}-{n:04d}{suffix}"


# --------------------------------------------------------------------------- #
# Question block rendering / field edit
# --------------------------------------------------------------------------- #
_Q_FIELD_ORDER = (
    "state", "urgency", "raised_by", "work", "raised_at",
    "question", "default", "assumption", "answer", "answered_by", "answered_at",
    # The retraction record, kept SEPARATE from the answer triple on purpose: a retracted
    # question was not decided, and writing the reason into `answer` would record a decision
    # nobody made. Absent on every question raised before this existed, which every reader
    # tolerates because they all go through `.get`.
    "retracted_at", "retraction",
)


def _render_qblock(qid: str, d: dict[str, str]) -> str:
    lines = [f"### {qid}"]
    for key in _Q_FIELD_ORDER:
        lines.append(f"- {key}: {d.get(key, '-')}")
    return "\n".join(lines)


def _qblock_count(questions_body: str, qid: str) -> int:
    """How many ``### qid`` headers the Questions body carries, read off the RAW headers.

    The parse is a dict keyed by id, so a duplicate is already gone by the time it exists.
    """
    return sum(1 for m in _QBLOCK_RE.finditer(questions_body) if m.group(1) == qid)


# `repair` cannot mend every duplicated question: `--old-line` always matches the FIRST block, and
# renaming or blanking a heading that carries an answer is refused as a decision-record or
# skeleton change. So the message names what actually clears each shape instead of "repair".
_QDUP_REMEDY = (
    "the cull sweeps an applied or retracted twin; steward repair can rename the first heading "
    "only when neither twin carries an answer; a twin carrying an answer needs a human hand edit"
)


def _require_unique_qid(text: str, qid: str) -> None:
    """Refuse `id_duplicate` when ``### qid`` heads more than one block, BEFORE a verb judges it.

    The Questions twin of `_require_unique_key`. `parse_registry` keeps the LAST block of a
    duplicated id and `_edit_qfield` wrote the FIRST, so `retract` and `answer` judged an open
    twin and then overwrote a block a human had already answered, rc 0. Asking first makes the
    refusal the one the write would give, and `_edit_qfield` asks again for any other caller.
    """
    n = _qblock_count(_get_body(text, "Questions"), qid)
    if n > 1:
        raise RegistryError(
            "id_duplicate",
            f"{qid!r} heads {n} blocks of ## Questions; the parser keeps the last block and "
            f"every write refuses until the duplicate is removed ({_QDUP_REMEDY})",
        )


def _edit_qfield(questions_body: str, qid: str, updates: dict[str, str]) -> str:
    """Set the named fields on the ``### qid`` block, ADDING any the block does not yet carry.

    THE APPEND HALF IS NOT A CONVENIENCE. This used to replace existing field lines and silently
    drop an update whose line was absent, which is fine only while every block on disk carries
    every field. The moment a release adds one -- E-0228 added ``retraction`` and
    ``retracted_at`` -- every question block already on disk lacks it, so the verb that writes it
    ran, exited 0, and wrote a row its own ``validate`` then called invalid. Reproduced against a
    copy of the live board: ``retract`` set ``state: retracted`` and discarded the mandatory
    reason, and the next cull would have moved that reasonless block into the append-only log.

    So the fix belongs HERE rather than in the caller: fixing `_cmd_retract` alone would leave
    the same trap armed for the next field anyone adds. New lines are inserted at their
    ``_Q_FIELD_ORDER`` position so a migrated block is byte-identical in shape to a fresh one,
    and a key outside that order is appended after the known fields rather than dropped.
    """
    if _qblock_count(questions_body, qid) > 1:
        # First-match write under a last-wins parse: never pick one twin silently.
        raise RegistryError("id_duplicate", f"{qid!r} heads more than one block of ## Questions")
    matches = list(_QBLOCK_RE.finditer(questions_body))
    for i, m in enumerate(matches):
        if m.group(1) != qid:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(questions_body)
        block = questions_body[m.start() : end]
        block_lines = block.split("\n")
        seen = set()
        last_field = None
        for j, line in enumerate(block_lines):
            fm = _QFIELD_RE.match(line)
            if not fm:
                continue
            last_field = j
            seen.add(fm.group(1))
            if fm.group(1) in updates:
                block_lines[j] = f"- {fm.group(1)}: {updates[fm.group(1)]}"
        missing = [k for k in updates if k not in seen]
        if missing:
            # Ordered by _Q_FIELD_ORDER, unknown keys last, so a block migrated by this path is
            # indistinguishable from one the renderer would have produced.
            order = {name: n for n, name in enumerate(_Q_FIELD_ORDER)}
            missing.sort(key=lambda k: (order.get(k, len(order)), k))
            at = (last_field + 1) if last_field is not None else len(block_lines)
            for offset, key in enumerate(missing):
                block_lines.insert(at + offset, f"- {key}: {updates[key]}")
        new_block = "\n".join(block_lines)
        return questions_body[: m.start()] + new_block + questions_body[end:]
    raise RegistryError("id_exists", f"question {qid} not found")


def _append_qblock(questions_body: str, qid: str, d: dict[str, str]) -> str:
    block = _render_qblock(qid, d)
    trimmed = questions_body.rstrip("\n")
    trailing = questions_body[len(trimmed):]  # preserve the section's trailing newlines
    return f"{trimmed}\n\n{block}\n{trailing}" if trimmed else f"{block}\n{trailing}"


# --------------------------------------------------------------------------- #
# Notes / Directives helpers
# --------------------------------------------------------------------------- #
#: The refusal body both Note guards share. It is part of the feature, not an error string.
#: A cap that only reports a limit teaches the author to split one note into three — the same
#: bytes in more rows, with the thread lost — so the refusal has to name the right move
#: instead. The pointer stays relative on purpose: a long path eats the budget it protects.
NOTE_OVERFLOW_ADVICE = (
    "Do NOT split this into several shorter notes — that is the same bytes in more rows, "
    "and the reader loses the thread.\n"
    f"  Write the analysis to a file in {NOTE_OVERFLOW_DIR}/ beside registry.md, named "
    "<YYYY-MM-DDTHHMMZ>-<actor>-to-<recipient>-<slug>.md,\n"
    "  then send a note carrying the decision plus the pointer "
    f"\"{NOTE_OVERFLOW_DIR}/<file>.md\".\n"
    "  The file is collected with the note when the note is culled — a bare filename in that "
    "one directory, no subdirectory and no path."
)

#: A Note's pointer as the advice above tells authors to write it: the overflow directory name,
#: a slash, and one filename ending in `.md`. Only that shape is recognised — the documented
#: one — and the leading alternation keeps `mynotes/x.md` or `../notes/x.md` from matching at
#: all. `~` is excluded from the filename class so a struck note's trailing `~~` wrapper is not
#: glued onto the name; the class is otherwise permissive because the STRICT test is
#: `_is_safe_overflow_name` below, not this regex. The trailing lookahead stops `x.mdx` from
#: reading as a pointer to `x.md` — a name the note never wrote, on a path that deletes what it
#: names — while still allowing the sentence-ending `see notes/x.md.` an author will write.
#:
#: `*` is in the leading class because `**notes/x.md**` is what a model-authored Note actually
#: writes, and a missed pointer is a file retained forever — the growth this rule exists to
#: stop. What is deliberately NOT admitted: `_` (an italic `_notes/x.md_` is indistinguishable
#: from a pointer into a directory literally named `_notes`) and case-insensitivity (on a
#: case-sensitive filesystem `Notes/x.md` is a DIFFERENT directory). Either would make the
#: sweep collect the basename a note never named and delete the wrong file, which is a worse
#: failure than retaining one. Those spellings miss, like `mynotes/`, and the file survives.
_NOTE_OVERFLOW_RE = re.compile(
    r"(?:^|[\s\"'`(\[<*])"
    + re.escape(NOTE_OVERFLOW_DIR)
    + r"/([^\s\"'`)\]>~]*\.md)(?![A-Za-z0-9_-])"
)

#: The SAME pointer, matched as loosely as the directory name allows. gh#136: the strict
#: regex above is used for two things whose failure directions are OPPOSITE, and only one of
#: them is safe to under-match.
#:
#:   * COLLECTING a file (which deletes it) -- a miss retains a file nobody points at. Clutter.
#:   * SUBTRACTING what is still referenced (which PROTECTS a file) -- a miss deletes a file a
#:     live note points at. Data loss, reported as routine cleanup.
#:
#: So the subtraction gets a matcher that errs the other way: no required prefix character, so
#: `_notes/x.md_` and `see:notes/x.md` both protect, and `mynotes/x.md` protects `x.md` too.
#: Over-matching here costs one retained file -- the cheap direction the docs already name --
#: and it can only ever ADD to the protected set, never remove from it.
#:
#: Case is still exact. `Notes/x.md` is a different directory on a case-sensitive filesystem,
#: and admitting it would let a note about some other tree pin a file here forever, which is a
#: retention with no relationship to the file at all.
#: The trailing guard is `[A-Za-z0-9]` and NOT the strict matcher's `[A-Za-z0-9_-]`. It still
#: stops `x.mdx` being read as `x.md`, and it stops rejecting `x.md_` -- which is the closing
#: half of `_notes/x.md_`, the exact spelling gh#136 reproduced. Dropping only the LEADING
#: delimiter left that case still unmatched: the italic marker breaks the pointer at both ends.
_NOTE_OVERFLOW_LOOSE_RE = re.compile(
    re.escape(NOTE_OVERFLOW_DIR) + r"/([^\s\"'`)\]>~]*\.md)(?![A-Za-z0-9])"
)

#: `./notes/x.md` -- the most natural relative spelling, and one the strict matcher above cannot
#: see, because its leading class excludes `.` so that `../notes/x.md` never matches (gh#661).
#: Rather than refuse it (a refused note in a headless loop is a lost message), `note` rewrites
#: it to `notes/x.md` on the way in. Same leading class as the strict matcher, so the `./` of a
#: `../notes/` pointer is never preceded by one and is left alone.
_NOTE_DOT_SLASH_RE = re.compile(
    r"(^|[\s\"'`(\[<*])\./(?=" + re.escape(NOTE_OVERFLOW_DIR) + r"/)"
)


def _normalize_overflow_pointers(text: str) -> str:
    """Rewrite every `./notes/<file>` pointer in a Note body to the `notes/<file>` cull collects."""
    return _NOTE_DOT_SLASH_RE.sub(r"\1", text)


#: Long fence: the inlined body is itself markdown and may carry ``` or ```` runs of its own.
_OVERFLOW_FENCE = "`````"


def _is_safe_overflow_name(name: str) -> bool:
    """True only for a bare `.md` basename that can name a file inside the overflow directory.

    This gate exists because the cull path DELETES what it collects, and a Note body is
    attacker-adjacent text: every loop writes it, and a pointer is never validated when it is
    written. A traversal accepted here deletes an arbitrary file with the steward's hands.

    Rejected: anything with a path separator on EITHER platform (a POSIX host still has to
    refuse ``a\\b.md``, because the string was written on a Windows one), a drive letter or NTFS
    stream (`:`), a NUL, `.`/`..`, and anything not ending in `.md`. The two `Pure*Path`
    basename comparisons are the whole test: a name that is its own basename under both
    flavours carries no separator, no drive and no root, which leaves nothing for a traversal
    to be built from. NUL is called out separately because it is not a separator and passes
    both of those — but it makes the name unrepresentable as a path, and `Path.resolve` raises
    `ValueError` on it, which is not what the sweep's degrade path is written to absorb.
    """
    if not name.endswith(".md") or len(name) <= len(".md"):
        return False
    if ":" in name or "\x00" in name or name in (".", ".."):
        return False
    return name == PurePosixPath(name).name and name == PureWindowsPath(name).name


def _note_overflow_refs(content: str) -> list[str]:
    """The overflow filenames one Note body points at, in order, de-duplicated.

    Returns FILENAMES, never paths — resolving one against a real directory is
    `_resolve_overflow_file`'s job, and keeping the two apart is what lets the shape rule be
    tested without a filesystem. An unsafe or undocumented pointer is SKIPPED, never raised on:
    a malformed pointer must not wedge the cull sweep that carries every other loop's notes.
    """
    names: list[str] = []
    for m in _NOTE_OVERFLOW_RE.finditer(content):
        name = m.group(1)
        if _is_safe_overflow_name(name) and name not in names:
            names.append(name)
    return names


def _note_overflow_refs_loose(content: str) -> list[str]:
    """Every overflow filename *plausibly* mentioned anywhere in *content*.

    For the still-referenced subtraction ONLY, never for collection. Same shape rule
    (`_is_safe_overflow_name`) and the same directory name; what it drops is the requirement
    that a delimiter precede the pointer, which is the part a human spelling routinely breaks.

    gh#136 reproduced it: two notes point at one overflow file, the struck one spells the
    pointer canonically and the LIVE one writes `_notes/shared.md_`. The strict matcher sees the
    first and not the second, so the file is collected and deleted while a live note still
    points at it -- and the count line reports it as one tidied file.
    """
    names: list[str] = []
    for m in _NOTE_OVERFLOW_LOOSE_RE.finditer(content):
        name = m.group(1)
        if _is_safe_overflow_name(name) and name not in names:
            names.append(name)
    return names


def _resolve_overflow_file(overflow_dir: Path, name: str) -> Optional[Path]:
    """The path `name` denotes inside `overflow_dir`, or None if it escapes it.

    Deliberately redundant with `_is_safe_overflow_name`: that one reads the string, this one
    asks the filesystem where the string actually lands, which is the only way to catch an entry
    that is syntactically a plain basename but resolves elsewhere — a symlink planted in the
    overflow directory. Both must agree before anything is deleted.

    Known limit, stated rather than implied: this catches an ENTRY that resolves out of the
    bucket. It does not second-guess the bucket. If `overflow_dir` is itself a symlink, both
    sides resolve through it and the files inside are collected normally — which is right, a
    symlinked `notes/` is still where the operator put the bucket.
    """
    if not _is_safe_overflow_name(name):
        return None
    candidate = overflow_dir / name
    try:
        base = overflow_dir.resolve(strict=False)
        final = candidate.resolve(strict=False)
    except (OSError, ValueError):
        # Not just OSError: an unrepresentable name (a NUL) raises ValueError out of the
        # stat under resolve(). Either way an unresolvable path is not a path we delete,
        # and neither may wedge the sweep that carries every other loop's notes.
        return None
    if final.parent != base:
        return None
    return candidate


def _one_line(exc: BaseException) -> str:
    """An exception message flattened to one line, for a marker that must stay one line."""
    return " ".join(str(exc).split())


def _overflow_block(name: str, content: str) -> str:
    """Render one collected overflow file for the log: named, delimited, un-confusable.

    The HTML comment markers name the file and bound its content for a human reader, and they
    survive content that closes the fence. They are NOT tamper-proof: the body is inlined
    verbatim — deliberately, since it is about to become the only copy — so a body that itself
    contains this end marker produces a second one, and a future parser reading to the first
    would truncate. Verbatim beats parseable here; if a parser is ever written, it must read to
    the LAST end marker of the named file, not the first.
    """
    ref = f"{NOTE_OVERFLOW_DIR}/{name}"
    body = content if content.endswith("\n") else content + "\n"
    return (
        f"<!-- overflow {ref} -->\n"
        f"{_OVERFLOW_FENCE}md\n"
        f"{body}"
        f"{_OVERFLOW_FENCE}\n"
        f"<!-- end overflow {ref} -->\n"
    )


#: The routing prefix `_append_note` writes: "[<ISO>] <actor> -> <recipient>: ". Tolerates the
#: full-line strike wrapper, because a struck note still counts against the burst guard — it
#: was still written, and consuming it must not buy the sender three more rows.
_NOTE_PREFIX_RE = re.compile(
    r"^(?:~~)?\s*\[([^\]]+)\]\s+([A-Za-z0-9_.-]+)\s*->\s*([A-Za-z0-9_.-]+)\s*:"
)


@dataclass(frozen=True)
class NotePrefix:
    """The parsed routing header of one coordination Note, plus the body it introduces."""

    stamp: datetime
    actor: str
    to: str
    body: str


def _parse_note_prefix(content: str) -> Optional[NotePrefix]:
    """Parse a Note body's routing prefix, or return None if it has none.

    ``content`` is a Note line with its ``- `` bullet marker already dropped. The Queue body
    holds hand-written prose too, and a note predating the routing convention has no prefix at
    all; an unparseable line is IGNORED rather than raised on, so one malformed bullet can
    never wedge every subsequent note write.
    """
    raw = content.strip()
    m = _NOTE_PREFIX_RE.match(raw)
    if not m:
        return None
    stamp = _parse_iso(m.group(1))
    if stamp is None:
        return None
    body = raw[m.end():].strip()
    if _is_struck_note(raw) and body.endswith("~~"):
        body = body[:-2].rstrip()  # drop the strike wrapper's tail so byte counts match the append
    return NotePrefix(stamp, m.group(2), m.group(3), body)


@dataclass(frozen=True)
class StrandedRef:
    """One live Note left addressed to a reaped lane: how to NAME it, not what it said."""

    stamp: Optional[datetime]
    actor: str
    order: int  # position in the file, the tiebreak that keeps rendering deterministic


def _notes_routed_to(text: str, target: str) -> list[StrandedRef]:
    """Live Notes addressed specifically to *target*, oldest first.

    Two exclusions, and each is a decision rather than a detail:

    STRUCK NOTES ARE NOT STRANDED. Striking is how a recipient records that it consumed a note,
    so a struck note has already done its job. Including them would make every reap of a
    long-lived lane report dozens of discharged notes, and a warning that is mostly noise is a
    warning nobody reads.

    A ``-> fleet`` NOTE IS NOT STRANDED EITHER, which is the subtler half. `fleet` is a broadcast
    address, not a lane: every surviving loop still reads it, so one lane dying does not orphan
    it. Matching on ``to`` alone -- rather than "any note the dead lane would have read" -- is
    what keeps this from reporting the entire fleet-wide feed as casualties of a single reap.

    AN UNPARSEABLE STAMP DOES NOT HIDE A NOTE. `_parse_note_prefix` returns None when the stamp
    does not parse, so filtering through it would drop such a note from the list AND from the
    count -- making "the count is always truthful" false at exactly the boundary where a reader
    has least other information. Routing is knowable without a readable stamp, so this reads the
    prefix pattern directly (`_NOTE_PREFIX_RE`, the same compiled object `_parse_note_prefix`
    uses, never a second spelling of it) and keeps the note with ``stamp=None``.

    ORDER: parseable stamps first, chronologically; unstamped notes after them in file order.
    Note stamps are MINUTE-resolution, so same-minute notes tie -- the sort is stable and the
    file is newest-first, so ties render newest-first. `order` is carried to break those ties
    deterministically rather than leaving the output dependent on Python's sort internals.
    """
    out: list[StrandedRef] = []
    for i, content in enumerate(_iter_note_bodies(_get_body(text, "Queue"))):
        if _is_struck_note(content):
            continue
        m = _NOTE_PREFIX_RE.match(content.strip())
        if not m or m.group(3) != target:
            continue
        out.append(StrandedRef(_parse_iso(m.group(1)), m.group(2), i))
    out.sort(key=lambda r: (r.stamp is None, r.stamp or datetime.min, r.order))
    return out


def _render_stranded(refs: list[StrandedRef], budget: int) -> str:
    """The clause `reap` appends naming notes left addressed to the lane it just reaped.

    Returns "" when nothing is stranded, so the reap line is byte-identical to what it always was
    in the common case -- a surfacing that fires on every reap regardless of whether anything is
    stranded is one its reader learns to skip.

    Each entry is STAMP AND SENDER ONLY. Never body text -- see STRANDED_NOTE_SAMPLE for the
    four defects quoting bodies caused.

    *budget* is the bytes still available on the surface `_guard_note_size` actually measures,
    which is the note's TEXT -- what follows "<actor> -> <to>: " -- and neither the routing prefix
    nor the "  - [stamp] " bullet `_append_note` adds afterwards. Getting that wrong is how the
    first cut's own cap test measured a third string the code never bounds and passed by one byte.

    THE BOUND IS HARD, WITH ONE DOCUMENTED EXCEPTION. Entries are added only while they fit, and
    whatever did not fit is counted in the tail. THE COUNT IS ALWAYS TRUTHFUL -- `len(refs)` is
    taken before any trimming, so a reader is never shown a short list that looks complete; that
    is the failure this whole issue is about, and reproducing it inside the fix would be its own
    defect. The exception: if the budget cannot fit even the bare count, the count is emitted
    ANYWAY and overruns. There is no rendering that is both truthful and smaller, and silence
    would leave a reap line reading as clean while notes sit stranded. Reachable only from a
    caller passing an absurd budget; `_cmd_reap` leaves well over 200 bytes.

    The tail is reserved before the entries are laid out, never appended afterwards. Appending it
    last is the tempting order and it is wrong: the tail is the part that says the list is
    partial, so it is the one part that must never be the thing that does not fit.
    """
    if not refs:
        return ""
    total = len(refs)
    head = f"; {total} live note(s) still addressed to it -- dev-manager may re-route or strike: "
    # Worst-case tail, reserved up front. Its width is bounded by `total`, which is known now.
    tail_reserve = len(f" +{total} more".encode("utf-8"))
    used = len(head.encode("utf-8")) + tail_reserve
    parts: list[str] = []
    for ref in refs[:STRANDED_NOTE_SAMPLE]:
        when = f"{ref.stamp:%Y-%m-%dT%H:%M}Z" if ref.stamp else "unstamped"
        entry = f"[{when} from {ref.actor}]"
        cost = len(entry.encode("utf-8")) + (3 if parts else 0)  # 3 = the " | " separator
        if used + cost > budget:
            break
        parts.append(entry)
        used += cost
    rest = total - len(parts)
    if not parts:
        # Nothing fits: still say HOW MANY, because the count is the actionable half.
        return f"; {total} live note(s) still addressed to it -- dev-manager may re-route or strike them"
    return head + " | ".join(parts) + (f" +{rest} more" if rest else "")


def _guard_note_size(body: str) -> None:
    """Refuse an over-cap Note, naming the move that keeps the thread intact."""
    n = len(body.encode("utf-8"))
    if n > NOTE_MAX_BYTES:
        raise RegistryError(
            "note_too_long",
            f"this note is {n} UTF-8 bytes; the cap is {NOTE_MAX_BYTES}.\n  {NOTE_OVERFLOW_ADVICE}",
        )


def _guard_phase_size(phase: str, *, actor: str = "<you>", composed: str = "") -> None:
    """Refuse an over-cap `phase`, naming the note channel rather than reporting a limit.

    Decidable from argv alone, which is why `_run_mutation` calls it PRE-LOCK. The two verbs
    that write the cell keep calling it too, as defence-in-depth for a direct `edit_fn` caller.
    """
    n = len(phase.encode("utf-8"))
    if n <= PHASE_MAX_BYTES:
        return
    if composed:
        raise RegistryError(
            "phase_too_long",
            f"{composed} is {n} UTF-8 bytes; the cap is {PHASE_MAX_BYTES}. The reason has to fit "
            "the cell WITH the marker in front of it. Say why in one line and route the detail "
            "as a note.",
        )
    raise RegistryError(
        "phase_too_long",
        f"this phase is {n} UTF-8 bytes; the cap is {PHASE_MAX_BYTES}. "
        "phase names WHAT IS RUNNING NOW, in a table cell a human scans every row of - it is "
        "capped below the 300-byte Note cap because the field a person reads must be shorter "
        "than the field a program reads. Send the narrative as a note "
        f"(`note --actor {actor} --to <role> --text \"...\"`, or a file in "
        f"`{NOTE_OVERFLOW_DIR}/` with a pointer) and keep the cell to the current step.",
    )


def _guard_note_burst(text: str, actor: str, to: str, body: str) -> None:
    """Refuse a split note: many rows on one actor->recipient route AND real bulk between them.

    Without this the byte cap is advisory: an author with a paragraph to send just posts it in
    four rows. Scoped to the ROUTE (this sender, this recipient) so a busy fleet-wide surface
    and unrelated senders are never throttled. Live and struck notes both count — striking is
    consuming, not un-sending, and reading a note must not buy the sender three more rows.

    BOTH signals are required, and the bytes half is what keeps this from silencing a loop.
    A row count alone reads a busy triage pass (four short, unrelated notes) as a split
    paragraph and refuses the fourth; in a headless loop that refusal is a lost message. See
    NOTE_BURST_BYTES. Machine-written notes are skipped entirely — see _MACHINE_NOTE_MARKERS.
    """
    now = _parse_iso(_now_iso())  # the same minute-resolution clock the stamps are written from
    cutoff = now - timedelta(minutes=NOTE_BURST_WINDOW_MIN)
    recent, recent_bytes = 0, 0
    for content in _iter_note_bodies(_get_body(text, "Queue")):
        prefix = _parse_note_prefix(content)
        if not prefix or prefix.actor != actor or prefix.to != to:
            continue
        # `>` and not `>=`: a stamp exactly at the cutoff is the window's far edge, not inside
        # it — minute-resolution stamps otherwise stretch a 15-minute window to nearly 16.
        if not prefix.stamp > cutoff:
            continue
        if any(marker in prefix.body for marker in _MACHINE_NOTE_MARKERS):
            continue
        recent += 1
        recent_bytes += len(prefix.body.encode("utf-8"))
    total = recent_bytes + len(body.encode("utf-8"))
    if recent >= NOTE_BURST_MAX and total > NOTE_BURST_BYTES:
        raise RegistryError(
            "note_split_burst",
            f"{recent} notes from {actor} to {to} already carry a stamp inside the last "
            f"{NOTE_BURST_WINDOW_MIN} minutes, {total} UTF-8 bytes with this one — more than "
            f"one note's worth ({NOTE_BURST_BYTES}) on a single route.\n  {NOTE_OVERFLOW_ADVICE}",
        )


def _live_notes_value(queue_body: str) -> str:
    """The value the Notes header must carry: "(none)" or "<N> live", counted as the striker sees."""
    live = sum(1 for content in _iter_note_bodies(queue_body) if not _is_struck_note(content))
    return "(none)" if live == 0 else f"{live} live"


def _stale_notes_header(text: str) -> Optional[tuple[str, int]]:
    """(what the header says, the live count) when they disagree; None when they agree.

    None as well when there is no Queue section or no header line: no header is no claim, so
    there is nothing to be stale -- and `validate` must not warn over it.
    """
    if "Queue" not in _section_bounds(text):
        return None
    body = _get_body(text, "Queue")
    for line in body.split("\n"):
        if not _is_notes_header(line):
            continue
        m = _NOTES_HEADER_RE.match(line)
        if not m:
            continue
        says, should = m.group(3).strip(), _live_notes_value(body)
        if says == should:
            return None
        return says, (0 if should == "(none)" else int(should.split()[0]))
    return None


def _refresh_notes_header(text: str) -> str:
    """Recompute the Queue ``**Notes**`` header's value from the Notes actually present.

    The header carries a value slot and nothing ever updated it: `templates/registry.md` ships
    it reading "(none)" and `_append_note` only ever inserted CHILD bullets beneath it, so a
    board holding a hundred live notes still announced an empty inbox (gh#101). That line sits
    on the one surface every loop reads at every wake, which is what made a cosmetic-looking
    staleness expensive: a lane that trusts the header skips its own routed instructions, and
    nothing anywhere reports that it did.

    The value is DERIVED here rather than maintained at each call site, and it counts through
    `_iter_note_bodies` -- the same recognition rule `_strike_note` and the cull sweep use --
    so what the header claims and what a lane can actually consume cannot drift apart. A count
    maintained by hand at each call site would be one more place to forget.

    Only `_append_note` and `_strike_note` move the live count. `cull` removes struck notes,
    which are excluded from that count already, so cull leaves this value untouched by
    construction rather than by a call that would look like protection while changing nothing.
    A header that went stale some OTHER way (a board written before gh#101, a hand edit) is
    backfilled by `_atomic_write`, which runs this on every registry write (gh#217).
    """
    body = _get_body(text, "Queue")
    value = _live_notes_value(body)
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if not _is_notes_header(line):
            continue
        m = _NOTES_HEADER_RE.match(line)
        if not m:
            continue
        lines[i] = f"{m.group(1)}{m.group(2) or ' — '}{value}"
        return _replace_body(text, "Queue", "\n".join(lines))
    return text


def _append_note(text: str, note: str) -> str:
    """Append an agent-to-agent coordination Note line under the Queue '**Notes**' bullet."""
    body = _get_body(text, "Queue")
    lines = body.split("\n")
    bullet = f"  - [{_now_iso()}] {note}"
    for i, line in enumerate(lines):
        if _is_notes_header(line):
            lines.insert(i + 1, bullet)
            return _refresh_notes_header(_replace_body(text, "Queue", "\n".join(lines)))
    # No Notes bullet -- create the header AND the note as a CHILD of it. This branch used to
    # write the note INTO the header line, which made that note unreachable: every
    # note-recognition rule in this module skips a line containing "**Notes**", so it could
    # never be struck, never be culled, and never be counted. The "(none)" seed is corrected
    # by the refresh below before the text is returned.
    return _refresh_notes_header(
        _replace_body(text, "Queue", f"- **Notes** — (none)\n{bullet}\n" + body)
    )


# --------------------------------------------------------------------------- #
# Edit-lock (mkdir; rmdir to release; 5-min stale steal with a note)
# --------------------------------------------------------------------------- #
@dataclass
class EditLock:
    path: Path
    stole_from: Optional[str] = None
    stole_age: float = 0.0
    #: Identifies this one acquire. A lock whose holder file no longer carries it was stolen.
    nonce: Optional[str] = None

    def still_ours(self) -> bool:
        # A denied read (an AV scanner, a reader mid-open on Windows) is retried, so a holder
        # that still owns the lock is not refused as "now held by unknown".
        if self.nonce is None:
            return False
        return _lock_holder(self.path, denied_retry=_HOLDER_DENIED_RETRY).nonce == self.nonce

    def release(self) -> bool:
        """Remove the lock only while it is still this acquire's; True for a clean release.

        A holder paused past the stale window can have its lock stolen. Removing the dir then
        would free the thief's lock for a third writer, so a lock that no longer carries this
        nonce is left in place and reported. The read and the rmdir are two steps; nothing but a
        stealer rewrites a held lock, and that is the window this narrows rather than closes.
        """
        if not self.still_ours():
            print(
                f"registry: not releasing {self.path}: the edit-lock is no longer ours "
                f"(now held by {_lock_holder(self.path).actor})",
                file=sys.stderr,
            )
            return False
        _rmdir_retry(self.path)
        return True


@dataclass
class _Holder:
    """What the holder file says. Locks written before the nonce existed carry only the actor."""

    actor: str
    raw: Optional[str] = None
    nonce: Optional[str] = None
    pid: int = 0
    host: Optional[str] = None
    pidns: Optional[str] = None


#: How long an ownership check rides out a denied holder read before calling it lost.
_HOLDER_DENIED_RETRY = 1.0  # seconds


def _read_holder_text(path: Path, denied_retry: float) -> str:
    deadline = time.monotonic() + denied_retry
    while True:
        try:
            return (path / "holder").read_text(encoding="utf-8")
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_ACQUIRE_POLL)


def _lock_holder(path: Path, *, denied_retry: float = 0.0) -> _Holder:
    """Parse ``<lock>/holder``: the actor and stamp lines, then ``KEY=value`` lines.

    The actor stays on line one so a reader that knows only the old two-line body still finds
    it. An unreadable or absent file is a holder nobody can be judged by. *denied_retry* keeps
    re-reading through a PermissionError for that long; the acquire loop passes none, since it
    polls anyway, but an ownership check that gave up at once would refuse a write it owns.
    """
    try:
        text = _read_holder_text(path, denied_retry)
    except OSError:
        return _Holder(actor="unknown")
    lines = text.splitlines()
    holder = _Holder(actor=lines[0].strip() if text.strip() else "unknown", raw=text)
    for line in lines[1:]:
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if key == "NONCE":
            holder.nonce = value or None
        elif key == "PID":
            try:
                holder.pid = int(value)
            except ValueError:
                holder.pid = 0  # torn mid-write: judged by age alone
        elif key == "HOST":
            holder.host = value or None
        elif key == "PIDNS":
            holder.pidns = value or None
    return holder


def _lock_age(path: Path) -> Optional[float]:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _this_host() -> str:
    return socket.gethostname()


def _pid_alive(pid: int) -> bool:
    """Portable, non-signalling liveness probe; ``lock_file`` owns the Windows and POSIX halves."""
    return lock_file._is_pid_alive(pid)


def _this_pid_namespace() -> str:
    """Which PID space ``os.getpid()`` numbers processes in, beyond what the hostname says.

    A hostname is not a PID space: WSL takes the Windows computer name, a host-network
    container shares the host's name, and an MSYS or Cygwin Python numbers processes apart
    from a native one on the same machine. The platform separates the Windows-side cases and
    the Linux PID-namespace link separates the containers; a PID from any other space names
    an unrelated process here, so probing it could call a live holder dead.
    """
    try:
        ns = os.readlink("/proc/self/ns/pid")
    except (OSError, NotImplementedError):  # no /proc here: the platform is the whole answer
        return sys.platform
    return f"{sys.platform}:{ns}"


def _holder_is_dead(holder: _Holder) -> bool:
    """True only when the holder provably died: its PID is gone and it ran in OUR PID space.

    A PID recorded on another host, or in another PID space on this one, names nothing here,
    and a lock with no PID or no PID space (torn, or written before they were recorded) can
    only be judged by age.
    """
    return (
        holder.pid > 0
        and holder.host == _this_host()
        and holder.pidns is not None
        and holder.pidns == _this_pid_namespace()
        and not _pid_alive(holder.pid)
    )


def _breakable(path: Path, stale_seconds: float) -> tuple[bool, Optional[float], _Holder]:
    age = _lock_age(path)
    holder = _lock_holder(path)
    if age is None:
        return False, age, holder  # gone meanwhile: the next mkdir takes it
    return age > stale_seconds or _holder_is_dead(holder), age, holder


def _steal_guard_path(path: Path) -> Path:
    return Path(str(path) + ".steal")


#: The steal guard is held for one re-judge, one break and one mkdir: milliseconds. One older
#: than this was left by a stealer that died inside it, whatever its PID now says.
_STEAL_GUARD_STALE = 60.0  # seconds


def _clear_aged_steal_guard(guard: Path) -> None:
    """Remove a steal guard past `_STEAL_GUARD_STALE`, even if its PID still looks alive.

    ``lock_file`` frees a guard whose PID is dead, but a hard-killed stealer's PID can be
    reused by a long-lived process, and then the guard reads alive forever. If the edit-lock
    is breakable too, as it usually is when a steal was under way, every writer would find
    the lock breakable, fail to take the guard, and exit 3 until someone deleted it by hand.

    Stat, then unlink only the body that was judged old: a guard re-created meanwhile carries
    a fresh nonce. The re-read and the unlink are still two steps; the same residual
    ``lock_file`` accepts for its own inner guard.
    """
    try:
        observed = guard.read_text(encoding="utf-8")
        age = time.time() - guard.stat().st_mtime
    except OSError:  # absent, or mid-removal on Windows: nothing aged to clear
        return
    if age < _STEAL_GUARD_STALE:
        return
    try:
        if guard.read_text(encoding="utf-8") != observed:
            return
        guard.unlink()
    except OSError:  # another contender removed or holds it: the acquire below decides
        return
    print(
        f"registry: cleared steal guard {guard} ({age:.0f}s old; its stealer died inside it)",
        file=sys.stderr,
    )


def _holder_body(actor: str, nonce: str) -> str:
    return (
        f"{actor}\n{_now_iso()}\nNONCE={nonce}\nPID={os.getpid()}\nHOST={_this_host()}\n"
        f"PIDNS={_this_pid_namespace()}\n"
    )


def _write_holder(path: Path, body: str) -> None:
    try:
        (path / "holder").write_text(body, encoding="utf-8", newline="\n")
    except OSError as e:  # fail loud — a lock we cannot attribute is not safely held
        raise RuntimeError(f"could not record edit-lock holder at {path}: {e}") from e


def _rmdir_retry(path: Path, timeout: float = 10.0) -> None:
    """Remove the lock dir, tolerating the Windows delete-pending window.

    A concurrent spinner may momentarily hold the holder file open (read_text) while
    we unlink it, yielding PermissionError (WinError 32). The reader closes promptly,
    so we retry briefly — the same lesson use_case_id learned for its counter lock.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            holder = path / "holder"
            if holder.exists():
                holder.unlink()
            path.rmdir()
            return
        except FileNotFoundError:
            return
        except (PermissionError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(_ACQUIRE_POLL)


def _force_break(path: Path) -> None:
    try:
        holder = path / "holder"
        if holder.exists():
            holder.unlink()
    except OSError:
        pass
    try:
        path.rmdir()
    except OSError:
        pass


def _steal(path: Path, observed: _Holder, stale_seconds: float, body: str) -> Optional[float]:
    """Break the lock at *path* and take it, one stealer at a time; its age if we now hold it.

    None means "not now": another stealer is inside the guard, the lock changed since it was
    judged, or a fresh writer re-took it during the break. The caller re-judges from scratch.

    Every stealer first takes ``<lock>.steal`` through ``lock_file`` (O_EXCL, with a PID and a
    nonce, so a guard left by a crashed stealer is itself breakable, and one older than
    `_STEAL_GUARD_STALE` is cleared even when its PID has been reused). Inside it the lock is
    judged again: only a holder file byte-identical to the one judged, and still breakable, is
    broken. Without that second look, two lanes that both read one stale age would each break
    the lock in turn, the second breaking the first one's fresh hold. Breakable alone is not
    enough: a lock re-taken by a holder that has since died is breakable too, but it is not
    the hold this caller judged, and the steal Note would name the wrong holder.

    Residual: the second look and the break are two steps. A paused holder releasing its own
    lock, and a fresh writer taking it, both inside that gap, would be broken. The holder's
    pre-write nonce check then refuses the robbed write, so the cost is a retry, not a loss.
    """
    guard_path = _steal_guard_path(path)
    _clear_aged_steal_guard(guard_path)
    try:
        guard = lock_file.acquire(guard_path)
    except (lock_file.LockHeldError, PermissionError):
        # PermissionError: on Windows the previous stealer's guard can be delete-pending, which
        # is contention like a held guard, not a permission fault to report as exit 1.
        return None
    try:
        breakable, age, current = _breakable(path, stale_seconds)
        if not breakable or current.raw != observed.raw:
            return None
        _force_break(path)
        try:
            path.mkdir()
        except (FileExistsError, PermissionError):  # PermissionError: gh#582, see the caller
            return None
        try:
            _write_holder(path, body)
        except Exception:
            _force_break(path)  # do not leak a lock we could not attribute
            raise
        return age
    finally:
        guard.release()


def acquire_edit_lock(
    lock_dir,
    project: str,
    actor: str,
    *,
    timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
    stale_seconds: float = EDIT_LOCK_STALE,
    poll: float = _ACQUIRE_POLL,
) -> EditLock:
    """Acquire the mkdir edit-lock, spinning past live contention, stealing when stale.

    REGISTRY.md "Concurrency": target hold is seconds; reads are lock-free; a lock older
    than five minutes is breakable, and the breaker appends a note to the shared surface.
    A lock whose recorded PID is dead on this host is breakable at once.
    """
    lock_dir = Path(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{project}-registry.lock"
    nonce = uuid.uuid4().hex
    body = _holder_body(actor, nonce)
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.mkdir()
        except PermissionError:
            # gh#582: on Windows, mkdir of a directory another process is removing at that
            # instant raises WinError 5 (PermissionError), not FileExistsError. That is
            # contention, and failing on it at once told the operator "this is not a transient"
            # about a write that a re-run then made. So it retries like a held lock. At the
            # deadline the two cases separate: a lock dir that exists is contention (exit 3);
            # a path still denied with no lock dir in the way is a real permission problem,
            # and the original error surfaces with main()'s permanent-denial message.
            if time.monotonic() >= deadline:
                try:
                    held = path.exists()
                except PermissionError:  # a delete-pending dir denies stat too: still contention
                    held = True
                if held:
                    raise LockHeldError(
                        f"registry edit-lock contended (access denied until the deadline): {path}"
                    )
                raise
            time.sleep(poll)
            continue
        except FileExistsError:
            breakable, age, observed = _breakable(path, stale_seconds)
            if breakable:
                stolen_age = _steal(path, observed, stale_seconds, body)
                if stolen_age is not None:
                    return EditLock(
                        path, stole_from=observed.actor, stole_age=stolen_age, nonce=nonce
                    )
                if time.monotonic() >= deadline:
                    raise LockHeldError(f"registry edit-lock re-taken during steal: {path}")
                time.sleep(poll)
                continue
            if time.monotonic() >= deadline:
                shown = "?" if age is None else f"{age:.0f}"
                raise LockHeldError(
                    f"registry edit-lock held by {observed.actor} "
                    f"(age {shown}s < {stale_seconds:.0f}s stale): {path}"
                )
            time.sleep(poll)
        else:
            try:
                _write_holder(path, body)
            except Exception:
                _force_break(path)  # do not leak a lock we could not attribute
                raise
            return EditLock(path, nonce=nonce)


# --------------------------------------------------------------------------- #
# Atomic write
# --------------------------------------------------------------------------- #
def _replace_retry(tmp, path: Path, timeout: float = atomic_io.REPLACE_RETRY_TIMEOUT) -> None:
    """Thin alias onto the shared primitive; see ``agentflow.atomic_io.replace_retry``."""
    atomic_io.replace_retry(tmp, path, timeout=timeout)


def _atomic_write(path: Path, text: str) -> None:
    """temp + fsync + retrying replace — the crashed-mid-write is never observed by a reader.

    ONE implementation lives in ``agentflow.atomic_io``; this stays as the registry's name for it
    so the write path reads the same as it always has. gh#57: the second copy of this routine
    (``status_writer``) still carried the bug after the first was fixed, which is why there is now
    only one.

    It is also the ONE place every registry write passes (init, cull, and every `_MUTATORS`
    verb in `_run_mutation`), which is why the Notes-header backfill lives here (gh#217).
    `_refresh_notes_header` only ran on note writes, so a board whose header was already stale
    stayed wrong through any number of status/claim/publish writes. Refreshing here repairs it
    on the next mediated write of any kind. It rewrites nothing but the header line, and only
    when that line disagrees with the live count, so a correct board is written exactly as
    given. Text with no Queue section or no header (a damaged board, a test) passes through.
    """
    if _stale_notes_header(text) is not None:
        text = _refresh_notes_header(text)
    atomic_io.atomic_write(path, text)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _require_agent(name: str, flag: str = "--actor") -> None:
    """Refuse a name that is not a fleet loop, naming the FLAG that carried it (gh#655).

    Five flags besides --actor name a loop. Blaming --actor for a bad --to sent the caller to
    fix a flag that was already right, and the same refusal came back.
    """
    if name not in AGENTS:
        raise RegistryError("agent_known", f"{flag} {name!r} is not one of {list(AGENTS)}")


def _refuse_if_waited_on(reg: "Registry", id_: str, verb: str) -> None:
    """Refuse consuming *id_* while a Queue row still names it in `waiting_on`.

    Consuming the id (apply, retract, resolve-blocker) takes it out of `_live_ids`, and
    `_queue_row_findings` then rejects every Queue row still pointing at it -- so the very next
    `validate` fails and every lane's wake read fails closed. Queue rows ONLY, because they are the
    only rows `validate` rejects for this: an `## Agents` row halted on a dead id is reported by
    `read --stranded`, not refused, and refusing on a row `validate` accepts would be a new rule.
    """
    waiting = [r for r in reg.queue.values() if _queue_ref(r) == id_]
    if waiting:
        rows = ", ".join(f"{r.id} (claimed_by {r.claimed_by or '-'}, state {r.state})" for r in waiting)
        raise RegistryError(
            "waited_on_live",
            f"{verb} {id_} refused: Queue row(s) still waiting on it: {rows}. Route each claimant "
            "a Note (unless it is you) and consume the id once no row waits on it. The claimant "
            "resumes a parked row (release --state building) or re-parks it on a successor id "
            "(release --state parked --waiting-on <new id>). A done or review row is never "
            "resumed: its claimant clears the cell (release --state <its state>, no --waiting-on), "
            "and a done row is also swept by the steward's cull.",
        )


def _queue_ref(r: "QueueRow") -> str:
    """A Queue row's `waiting_on` as `validate` and the consuming verbs both read it."""
    return (r.waiting_on or "-").strip()


def _require_live_waiting_on(reg: "Registry", waiting_on: str) -> None:
    """Refuse a waiting_on that is not a live Q-/B- id -- the check `validate` runs at every wake.

    Shared by `status` and `release` (gh#648). Unchecked on either path, a typo or a guessed id
    writes cleanly and then fails the NEXT `validate`, which every loop runs fail-closed at wake,
    so one bad write by one lane stops the whole fleet.
    """
    if not (_q_id_ok(waiting_on) or _blocker_key_ok(waiting_on)):
        raise RegistryError("agent_waiting_on", f"waiting_on {waiting_on!r} is not a Q-/B- id")
    if waiting_on not in _live_ids(reg):
        raise RegistryError("agent_waiting_on", f"waiting_on {waiting_on} is not a live question/blocker")


def _epic_name(epic_id: str) -> str:
    """The name half of an epic id, or ``""`` for a legacy bare `E-NNNN` that has none.

    `_short_name` never returns empty -- it refuses instead -- so a legacy row can never be
    matched by a derived name, which is what keeps `publish`'s reclaim path from pairing a slug
    with a numbered row that says nothing about which epic it was.
    """
    m = _E_NAME.match(epic_id.strip())
    return m.group(1) if m else ""


def _queue_key_ok(key: str) -> bool:
    return _row_key_ok("Queue", key)


def _blocker_key_ok(key: str) -> bool:
    return _row_key_ok("Blockers", key)


def _q_id_ok(key: str) -> bool:
    return bool(_Q_ID.match(key))


def _require_id(value: str, ok: Callable[[str], bool], kind: str) -> None:
    """*ok* is the table's own key rule, so an argument names exactly the rows a table keeps.

    A table cell never holds a line break, but the rules end in `$`, which also matches before a
    trailing newline -- so the break is refused here, where only an argument can carry one."""
    if "\n" in value or "\r" in value or not ok(value):
        raise RegistryError("id_format", f"{kind} id {value!r} is malformed")


# Free-text args flow verbatim into markdown table cells and "- field:" lines. A newline
# fabricates new rows / field-lines that the read-side parser then honors as authentic state
# (and a later duplicate field line overrides the real one); a '|' manufactures extra table
# cells and shifts columns. Reject both LOUDLY (fail-loud, not silent-escape) so the mediated
# path can never be used to fabricate the very ownership it exists to protect.
_INLINE_FIELDS = (
    "phase", "title", "question", "default", "assumption",
    "answer", "description", "text", "waiting_on", "work",
    # `reason` rides into the Agents row's phase cell via stand-down, so it needs the same
    # newline/pipe guard every other free-text field gets. A '|' here silently corrupts the
    # markdown table for every loop.
    "reason",
)


def _require_inline(value: Optional[str], field_name: str) -> None:
    if value is None:
        return
    flag = "--" + field_name.replace("_", "-")
    if "\n" in value or "\r" in value:
        raise RegistryError("inline_only", f"{flag} contains a newline")
    if "|" in value:
        raise RegistryError("inline_only", f"{flag} contains '|' (the table-column delimiter)")


def _guard_inline_fields(a) -> None:
    """Reject newline/pipe injection in every actor-supplied free-text field before any write."""
    for name in _INLINE_FIELDS:
        _require_inline(getattr(a, name, None), name)


_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _require_project_slug(project: Optional[str]) -> None:
    """--project routes into a filesystem path; keep it a bare slug (no '..'/'/')."""
    if project is not None and not _PROJECT_RE.match(project):
        raise RegistryError("bad_project", f"project {project!r} must match [A-Za-z0-9_-]+ (no path separators or '..')")


def _live_ids(reg: Registry) -> set[str]:
    # A RETRACTED question is consumed, exactly like an applied one: nothing is still waiting on
    # it, so a lane may not point `waiting_on` at it and the cull may sweep it. This is the line
    # that keeps `retract` from becoming a way to leave a lane halted against a dead referent.
    live = {qid for qid, q in reg.questions.items()
            if q.fields.get("state") not in CONSUMED_QUESTION_STATES}
    live |= {bid for bid, b in reg.blockers.items() if b.state != "resolved"}
    return live


# --------------------------------------------------------------------------- #
# Commands (each returns new full text; validation raises RegistryError)
# --------------------------------------------------------------------------- #
def _stamp_agent_row(text: str, actor: str, cells: list[str]) -> str:
    """Rewrite ONE Agents row — the single row-stamping path (status, and directive's heartbeat)."""
    body = _replace_data_row(text, "Agents", 0, actor, "agent", cells)
    return _replace_body(text, "Agents", body)


#: Verbs that already write the actor's own heartbeat, so the exempt-seat stamp skips them:
#: `status` stamps the row it rewrites, `directive` stamps dev-manager's row itself.
_SELF_STAMPING_VERBS = frozenset({"status", "directive"})


def _stamp_exempt_heartbeat(text: str, actor: str) -> str:
    """Refresh the heartbeat cell of an ``ETA_HORIZON_EXEMPT`` actor's own row, nothing else.

    The human's seat runs no firing contract, so it never issues the `status` write that moves
    its heartbeat; it coordinates through note, answer, resolve-blocker and the rest. Without
    this its row aged out of every liveness detector while the seat was visibly writing the
    board. Keyed on the actor, not on a verb list, so a verb added later cannot be the one that
    leaves the row ageing. Same shape as `_cmd_directive`'s stamp: status, work, phase, eta and
    waiting_on stay whatever the row owner last wrote.

    Skipped, never refused, when there is nothing honest to stamp: no row (the write is the
    payload and must not be lost for a side effect), an unreadable eta (see below), or a
    stood-down or reaped phase (a fresh
    heartbeat under a phase that says the lane is stopped is the row `stand_down_heartbeat_only`
    exists to prevent; a full `status` write is what brings such a row back).
    """
    if actor not in ETA_HORIZON_EXEMPT:
        return text
    try:
        row = parse_registry(text).agents.get(actor)
    except RegistryError:
        # Same reasoning as `_cmd_directive`: parse_registry validates every section, and a
        # board missing one must not take the verb's own write down with it.
        return text
    if row is None or _row_is_excluded_phase(row.phase):
        return text
    # An unreadable eta (a fresh board's '-') is skipped for `--heartbeat-only`'s `eta_required`
    # reason: a fresh heartbeat beside it turns a never-started row into one the missed-wake
    # alarm reports INVISIBLE. The seat's first `status` write declares its eta; stamps start
    # after that.
    if _parse_iso(row.eta) is None and row.status != "halted":
        return text
    cells = [row.agent, row.status, row.work, row.phase, _now_iso(), row.eta, row.waiting_on]
    return _stamp_agent_row(text, actor, cells)


def _status_argv_checks(a) -> None:
    """Every `status` refusal decidable from argv alone, in one place (gh#656).

    `_run_mutation` runs this PRE-LOCK, and `_cmd_status` runs it first as well, so there is one
    copy of each predicate. Under the lock a refusal could first force-break another lane's
    stale edit-lock and then discard the only record of the steal with the refused write; a
    refusal argv alone decides has no reason to take the lock at all. Board-dependent refusals
    (a missing row, a dead --waiting-on) stay in the verb.
    """
    _require_agent(a.actor)
    if getattr(a, "heartbeat_only", False):
        # Refresh liveness WITHOUT re-asserting anything else. The 30-minute heartbeat ceiling
        # means a loop must re-stamp while long subagent work runs, and until now the only way
        # to do that was to re-send every column -- so an automated re-stamp carrying a phase
        # captured minutes earlier wrote a FRESH timestamp over a claim that had already
        # finished. That row is worse than a stale one: a stale row is visibly stale, while a
        # fresh row asserting dead work reads as ground truth. (Observed: a heartbeat monitor
        # advertised two build agents for ~10 minutes after they exited.)
        #
        # Same shape as `_cmd_directive`, which already stamps dev-manager's heartbeat and
        # leaves every other cell as the row owner last wrote it, and as `_cmd_reap`, which
        # preserves a dead row's heartbeat as stale evidence. Liveness and claims are separate
        # facts; this lets them be written separately.
        conflicting = [
            name
            for name, value in (
                ("--state", a.state),
                ("--work", getattr(a, "work", None)),
                ("--phase", getattr(a, "phase", None)),
                ("--eta", getattr(a, "eta", None)),
                ("--waiting-on", getattr(a, "waiting_on", None)),
            )
            # argparse defaults the string args to "-", so only a real value is a conflict.
            if value not in (None, "-")
        ]
        if conflicting:
            raise RegistryError(
                "heartbeat_only_exclusive",
                "--heartbeat-only refreshes ONLY the heartbeat; it cannot be combined with "
                f"{', '.join(conflicting)}. Half-asserting is the bug it exists to prevent -- "
                "send a full status write when the phase really changed.",
            )
        return
    if a.state is None:
        raise RegistryError(
            "agent_state_enum",
            "status requires --state (or --heartbeat-only to refresh liveness alone)",
        )
    if a.state not in AGENT_STATES:
        raise RegistryError("agent_state_enum", f"state {a.state!r} not in {list(AGENT_STATES)}")
    work = a.work or "-"
    if work != "-" and not _queue_key_ok(work):
        raise RegistryError("id_format", f"work {work!r} is not an E- id or '-'")
    eta = a.eta or "-"
    if eta != "-" and _parse_iso(eta) is None:
        raise RegistryError("eta_iso", f"eta {eta!r} is not ISO-8601")
    # gh#682: an eta past the horizon switches off every liveness detector for this lane (each
    # waits for now to pass eta) while the lane keeps its claims. Refused here, before any
    # write, so the one-character year typo that produces it never lands.
    if (eta != "-" and a.actor not in ETA_HORIZON_EXEMPT
            and _eta_beyond_horizon(_parse_iso(eta), datetime.now(UTC))):
        raise RegistryError(
            "eta_too_far",
            f"eta {eta!r} is more than {ETA_HORIZON_HOURS:g}h from now. Declare your next "
            f"ScheduleWakeup or recovery-guard fire (at most {ETA_HORIZON_HOURS:g}h ahead).",
        )
    _guard_phase_size(a.phase or "-", actor=a.actor)
    if a.state in ("waiting", "halted") and (a.waiting_on or "-") == "-":
        raise RegistryError("agent_waiting_on", f"status {a.state} requires --waiting-on <live Q-/B- id>")

    # Last of the field checks on purpose: this is the most GENERIC one, and a generic check
    # that runs first masks the specific one behind it. Placed earlier, it turned an existing
    # "waiting without a live ref" refusal into an eta complaint -- the caller would have
    # fixed the eta and met the real error on the next attempt. (Whether a GIVEN --waiting-on
    # is live needs the board, so since gh#656 that one check runs after this, under the lock.)
    #
    # A declared next wake must be a FIELD, not prose. The missed-wake alarm reads only this
    # cell, and before this guard existed lanes split between a real eta and "next ~21:14Z"
    # buried in `phase` -- so an alarm built on it would have been a vacuous gate, passing
    # against whatever a fixture happened to contain. `halted` is exempt because a declared
    # halt is BLOCKED ON AN ANSWER it cannot get: it still wakes on schedule, but no wake of its
    # own can unblock it, and it is the same row the alarm excludes (its transition already
    # paged once). Eta is mandatory exactly where the alarm reads it.
    # (An EMPTY lane is not this and never halts -- it schedules its next check like any other
    # wake, so it keeps a real eta and stays fully visible to the alarm.)
    if eta == "-" and a.state != "halted":
        raise RegistryError(
            "eta_required",
            f"status --state {a.state} requires --eta <ISO-8601>: every wake declares when the "
            "next one is due, and the missed-wake alarm reads that cell and nothing else. "
            "Pass the time you intend to wake, e.g. --eta "
            f"{(_parse_iso(_now_iso()) + timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%MZ')}. "
            "Only --state halted may omit it.",
        )


def _cmd_status(text: str, a) -> str:
    _status_argv_checks(a)
    heartbeat_only = bool(getattr(a, "heartbeat_only", False))

    if heartbeat_only:
        reg = parse_registry(text)
        row = reg.agents.get(a.actor)
        if row is None:
            _require_intact_table(text, "Agents")
            raise RegistryError("agent_own_row", f"{a.actor} has no Agents row (run init/provision)")
        # THIS IS WHERE INVISIBILITY BECAME PERMANENT. `eta_required` (below) only guards the
        # full status write, and this path returns before reaching it -- so a row already
        # carrying `eta = -` kept it through every re-stamp, forever. And `--heartbeat-only`
        # is precisely the call the 30-minute in-flight ceiling tells every lane to use, so
        # the most-travelled path was the one that preserved the hole. Refusing here is what
        # closes the path lanes actually travel. It does NOT make the blind state unreachable,
        # and claiming otherwise would be the overclaim this suite keeps catching: `directive`
        # also stamps a heartbeat while preserving `eta` verbatim, and it is deliberately NOT
        # guarded here -- refusing to record a standing human instruction because of an unrelated
        # cell would be a worse failure than the one being prevented. That row stays REPORTED by
        # `blind_rows()`; prevention covers the ceiling re-stamp, reporting covers the rest.
        #
        # `halted` is exempt for the same reason it is exempt from the alarm and from
        # `eta_required` -- a declared halt still wakes, but no wake of its own unblocks it,
        # so it owes no eta.
        # A stood-down row must not take a fresh heartbeat. Its phase asserts the lane is
        # stopped, so re-stamping it produces the exact row this branch's own comment calls
        # worse than a stale one: a FRESH timestamp over a claim that is no longer true. A lane
        # that is genuinely back says so with a full status write, and that write is also what
        # clears the mark -- so refusing here is what makes "clears on the next real status
        # write" true rather than merely intended.
        if row.phase.strip().startswith(STOOD_DOWN_PHASE):
            raise RegistryError(
                "stand_down_heartbeat_only",
                f"{a.actor} is marked stood down ({row.phase.strip()!r}); --heartbeat-only "
                "would stamp fresh liveness onto a row that says this lane is stopped. Send "
                "one full status write (--state/--phase/--eta) to declare you are back -- that "
                "write clears the mark.",
            )
        if (
            _parse_iso(row.eta) is None
            and row.status != "halted"
            and not _row_is_excluded_phase(row.phase)
        ):
            raise RegistryError(
                "eta_required",
                "--heartbeat-only cannot refresh a row whose eta is unreadable "
                f"({row.eta!r}): it would preserve that cell and keep this row INVISIBLE to "
                "the missed-wake alarm, which reads eta and nothing else. Send one full "
                "status write with --eta to declare your next wake, then --heartbeat-only "
                "works for every re-stamp after it. Only --state halted may carry no eta.",
            )
        return _stamp_agent_row(
            text,
            a.actor,
            [row.agent, row.status, row.work, row.phase, _now_iso(), row.eta, row.waiting_on],
        )

    reg = parse_registry(text)
    if a.actor not in reg.agents:
        _require_intact_table(text, "Agents")
        raise RegistryError("agent_own_row", f"{a.actor} has no Agents row (run init/provision)")

    work = a.work or "-"
    eta = a.eta or "-"
    phase = a.phase or "-"
    waiting_on = a.waiting_on or "-"
    if a.state in ("waiting", "halted"):
        _require_live_waiting_on(reg, waiting_on)
    else:
        waiting_on = "-"

    new_cells = [a.actor, a.state, work, phase, _now_iso(), eta, waiting_on]
    return _stamp_agent_row(text, a.actor, new_cells)


# --------------------------------------------------------------------------- #
# Halted ping. Cadence decided in the public-release plan's decisions table as D6
# ("Halted-question cadence: one Ntfy ping at halt + one strand-check nag"), and
# exercised by that plan's registry-drill step 3. NOT a docs/REGISTRY.md identifier —
# earlier comments here cited it as one, which sent readers to the wrong document.
# The rule it encodes: ONE notification per halt, never one per wake.
# --------------------------------------------------------------------------- #
# Fires on the Agents-row TRANSITION into halted, and nowhere else. `ask --urgency
# halted` stays deliberately silent: the raiser flips its own row to halted immediately
# after raising the question (REGISTRY.md #Question-lifecycle), so pinging both sites
# would double-send for the normal sequence. That silence is a decision, not an omission.
def _halted_transition(prior_text: str, a) -> Optional[str]:
    """The PRIOR status when a ping should fire, else None. None is the silent case.

    Returns a string rather than a bool so the caller can say WHICH transition happened -
    `_halted_ping_detail` uses it to distinguish a first halt from a post-reap re-halt from a
    re-point. A bare bool made all three produce byte-identical alerts.

    `prior_text` is the registry as re-read under the edit-lock, so the comparison sees
    the same snapshot the edit was computed from. A halted loop re-stamps this row on
    every wake (the heartbeat ceiling); halted -> halted is that re-stamp and is silent.
    """
    if getattr(a, "state", None) != "halted":
        return None
    prior = parse_registry(prior_text).agents.get(a.actor)
    if prior is None:
        # _cmd_status refuses a missing row, so this is unreachable today. If it ever
        # became reachable, staying SILENT is the right default: never notify about a
        # row that does not exist.
        return None
    if prior.status != "halted":
        return prior.status
    # Already halted, but now BLOCKED ON SOMETHING ELSE. The human's only notification
    # names the question, so a re-point to a different Q-/B- id is new information and
    # must be announced; a plain re-stamp (same subject) stays silent.
    if (a.waiting_on or "-") != (prior.waiting_on or "-"):
        return prior.status  # "halted" — the detail renders this as a re-point
    return None


def _halted_ping_detail(actor: str, work: str, waiting_on: str, prior_status: str = "-") -> str:
    """The ntfy body: ASCII-only, notification-only, and it names where the human goes.

    ``prior_status`` is the status the row held before this flip. Exactly ONE transition can be
    named from it alone: ``halted -> halted`` with a changed subject is unambiguously a re-point,
    so that ping says so instead of looking like a repeat of the first.

    **A post-reap re-halt deliberately reads the same as a first halt, and that is a decision.**
    The obvious discriminator — "prior status was ``idle`` means the row was reaped" — is WRONG,
    and a test written to prove it disproved it instead: rows start ``idle``, so an ordinary
    first halt is *also* ``idle -> halted``. Tagging on ``idle`` labelled every first halt a
    reap. Nothing in the Agents row distinguishes the two, because the row holds no history.

    So the duplicate stands, knowingly. Deduping was rejected as worse: a loop that wakes after a
    reap and re-halts on the same open question is telling the human something true and useful —
    the loop you wrote off is alive and still stuck — and suppressing it would trade a repeated
    alert for a missing one. Recovering the distinction would mean giving the ping memory across
    processes (a reap Note, or a last-pinged field), which is a larger change than a repeated
    notification justifies. Pinned by test rather than left to chance.
    """
    on_work = f" (work {work})" if work and work != "-" else ""
    repoint = " - now blocked on a DIFFERENT id than before" if prior_status == "halted" else ""
    return f"{actor} halted on {waiting_on}{on_work}{repoint} - open dev-manager to answer."


def _send_halted_ping(actor: str, work: str, waiting_on: str, prior_status: str = "-", *, sender=None) -> None:
    """Send the single halted-question ping (plan decision D6) through ``agentflow.ntfy``. NEVER raises.

    The registry row is the durable artifact and the notification is a side effect — the
    same rule the directive/heartbeat write follows. A dead ntfy, an unconfigured topic,
    a notifier that changed shape: all of it is reported on stderr, and none of it is
    allowed to fail (or unwind) a write that already landed on disk. ``sender`` is
    ntfy's injectable transport (tests pass a fake; nothing here touches the network).
    """
    try:
        from agentflow import ntfy

        ntfy.notify(
            ntfy.TriggerType.HALTED_QUESTION,
            # The mobile banner shows the TITLE only, so it must name the loop that
            # stopped and what it is blocked on - "E-0009 - halted_question" tells the
            # human neither. notify() renders "<epic> - <trigger>", so put both here.
            epic=f"{actor} halted on {waiting_on}",
            detail=_halted_ping_detail(actor, work, waiting_on, prior_status),
            **({"sender": sender} if sender is not None else {}),
        )
    except Exception as exc:  # noqa: BLE001 — side effect; the row is already written
        print(f"warn: halted ntfy ping failed ({type(exc).__name__}: {exc}); row written", file=sys.stderr)


def _claim_argv_checks(a) -> None:
    """`claim`'s refusals that argv alone decides; run PRE-LOCK too (gh#656, `_ARGV_CHECKS`)."""
    _require_agent(a.actor)
    _require_id(a.id, _queue_key_ok, "work")
    _require_capability("claim", a.actor)


def _cmd_claim(text: str, a) -> str:
    _claim_argv_checks(a)
    reg = parse_registry(text)
    row = reg.queue.get(a.id)
    if row is None:
        _require_intact_table(text, "Queue")
        raise RegistryError("id_exists", f"{a.id} is not in the Queue")
    # Before the lane judgement: `row` is the LAST duplicate, and the twin may be in the other lane.
    _require_unique_key(text, "Queue", 0, a.id, "id")
    if row.lane != LANE_OF[a.actor]:
        raise RegistryError("queue_lane_discipline", f"{a.id} is in lane {row.lane!r}, not {LANE_OF[a.actor]!r}")
    if row.state != "ready" or row.claimed_by not in ("-", ""):
        raise RegistryError("queue_claim_ready", f"{a.id} is state={row.state} claimed_by={row.claimed_by}")
    new_cells = [row.id, row.work_item, row.lane, a.actor, "building", row.waiting_on or "-", row.notes]
    body = _replace_data_row(text, "Queue", 0, a.id, "id", new_cells)
    return _replace_body(text, "Queue", body)


def _release_argv_checks(a) -> None:
    """`release`'s refusals that argv alone decides; run PRE-LOCK too (gh#656, `_ARGV_CHECKS`)."""
    _require_agent(a.actor)
    _require_id(a.id, _queue_key_ok, "work")
    if a.state not in RELEASE_STATES:
        raise RegistryError("queue_state_enum", f"release state {a.state!r} not in {list(RELEASE_STATES)}")
    # Resuming asserts the row is moving again, so it cannot also assert what it is still
    # waiting for. Refused rather than silently dropped: half-asserting is the failure
    # `--heartbeat-only` already refuses one section over, and a row reading `building` while
    # naming a blocking question is a contradiction a later reader has no way to resolve.
    if a.state == "building" and a.waiting_on and a.waiting_on != "-":
        raise RegistryError(
            "queue_resume_waiting",
            "release --state building RESUMES a row, so it clears waiting_on; drop --waiting-on "
            "(or park it instead with --state parked --waiting-on)",
        )
    # gh#707: a parked row must name what it waits on. Without this, leaving the flag out wrote
    # a row parked on nothing -- the orphan `validate` can only report, since '-' is not an id.
    if a.state == "parked" and (a.waiting_on or "-") == "-":
        raise RegistryError(
            "agent_waiting_on",
            "release --state parked needs --waiting-on <Q-/B- id> naming what the row waits on "
            "(use the id `ask` / `raise-blocker` printed)",
        )


def _cmd_release(text: str, a) -> str:
    """Move a row you CLAIMED into another state, without ever letting go of it.

    Despite the name this is the claimant's general state-transition verb, and only the `ready`
    branch actually releases anything -- that is the one state that returns the row to the pool
    and blanks `claimed_by`. Every other state keeps the claim, which is what makes
    `--state building` a genuine resume rather than a re-acquisition: the row never passes
    through an unclaimed state, so there is no moment at which the board says the work was
    abandoned (gh#36).

    The claimant check below is what makes adding `building` safe: it already refuses anyone who
    does not hold the row, so a resume cannot be used to seize another builder's work.
    """
    _release_argv_checks(a)
    reg = parse_registry(text)
    row = reg.queue.get(a.id)
    if row is None:
        _require_intact_table(text, "Queue")
        raise RegistryError("id_exists", f"{a.id} is not in the Queue")
    # Before the claimant judgement: `row` is the LAST duplicate, and the twin may name another holder.
    _require_unique_key(text, "Queue", 0, a.id, "id")
    if row.claimed_by != a.actor:
        raise RegistryError("queue_only_claimant", f"{a.id} is claimed by {row.claimed_by!r}, not {a.actor}")
    waiting_on = a.waiting_on or "-"
    if a.state == "ready":
        claimed_by = "-"  # returned to the pool
        waiting_on = "-"
    else:
        claimed_by = a.actor
        if waiting_on != "-":
            _require_live_waiting_on(reg, waiting_on)
    # No explicit clear for the resume case: a mutation deleting one proved it was DEAD CODE.
    # `--waiting-on` is refused for `building` (`_release_argv_checks`), so by the time control reaches here on a
    # resume `a.waiting_on` is the argparse DEFAULT, which is the literal "-" and never None
    # (`sp.add_argument("--waiting-on", dest="waiting_on", default="-", help=...)`). Either way the
    # expression below yields "-". The property "a resumed row is not waiting on anything" is
    # carried by the REFUSAL plus that default, and the refusal is itself pinned -- so a line
    # restating it would read as load-bearing while doing nothing.
    #
    # Worth stating precisely because the same `or "-"` fallback is UNREACHABLE from the CLI for
    # exactly that reason, which is why two mutations of it came back green as equivalents.
    new_cells = [row.id, row.work_item, row.lane, claimed_by, a.state, waiting_on, row.notes]
    body = _replace_data_row(text, "Queue", 0, a.id, "id", new_cells)
    return _replace_body(text, "Queue", body)


def _cmd_publish(text: str, a, log_text: str) -> str:
    """Give an epic its registry identity: an ALLOCATED number plus a name derived from the slug.

    THE VERB NO LONGER ACCEPTS AN ID (E-0260). It used to require one, nothing allocated it, and
    what lead-dev typed was the spec's PRIORITY -- so identity was the priority. Two consequences
    made that untenable. Rebanding a spec, which is a routine dev-manager act and is meant to be
    cheap, renamed the epic in every Note, worktree, branch and Ntfy line keyed on it. And
    collisions were structural rather than unlucky: four drafted specs carried p220 on one day
    and only the first to publish could have its number.

    The number now comes from the same max-scan sequence that hands out `Q-` and `B-` ids, so it
    is seeded above every id the live board OR its append-only log has ever carried -- see
    `_max_id` for why the log half is not optional. The name comes from `_short_name(slug)`, a
    pure function, so two seats publishing one spec derive one id.

    THE DUPLICATE AND REPUBLISH CHECKS KEY ON THE NAME, not on a typed id, because the name is
    the only part of an id that a caller holding just a slug can compute. That preserves both
    properties the typed id used to give for free:

      * A second `publish` of a slug already queued is REFUSED. Without it lead-dev's every-firing
        §2.6 sweep would add a second row for an epic already in a lane -- one epic, two rows, two
        builders able to claim it.
      * A withdrawn row is RECLAIMED with its number intact, so `publish` stays the exact inverse
        of `withdraw` (gh#32) and an epic that comes back is the same epic. Without the reclaim
        the verb is a one-way door, and the documented causes of a withdrawal are mostly
        TRANSIENT: "deps un-satisfied" reverses when the dep lands, "conflicts with another
        lane's claims" reverses when that claim completes. The epic also stays
        `approved-for-autonomous` on disk, so §2.6's every-firing sweep re-offers it -- which
        without the reclaim would hit `id_duplicate` on every wake until an unrelated loop
        happened to cull. The withdrawal note is preserved rather than cleared: that the row was
        pulled, why, and that it came back is the history the next reader of this row needs.

    A LEGACY BARE ROW CANNOT BE RECLAIMED THIS WAY, and that is deliberate. `E-0220` carries no
    name, so nothing in it says which slug it stands for; guessing would be worse than minting.
    Re-publishing a withdrawn legacy row therefore allocates a new-shape id and leaves the old row
    withdrawn until a cull sweeps it -- untidy for exactly the two such rows that exist, and never
    wrong.
    """
    _require_capability("publish", a.actor)
    if getattr(a, "id", None):
        raise RegistryError(
            "queue_publish_allocates_id",
            f"publish allocates the id; it cannot be given one ({a.id!r}). Pass --slug <spec-slug> "
            "and the number is taken from the registry's own sequence. An id typed by a lane is "
            "the spec's priority in disguise, and a reband then renames the epic.",
        )
    name = _short_name(getattr(a, "slug", "") or "")
    # Decided here, printed by the dispatcher after the write lands, like the Directive
    # containment warning. Set UNCONDITIONALLY so a previous call's warning is never reprinted.
    _cmd_publish.last_title_warning = publish_title_warning(a.title)
    if a.lane not in LANES:
        raise RegistryError("queue_lane_enum", f"lane {a.lane!r} not in {list(LANES)}")
    reg = parse_registry(text)

    same = [r for r in reg.queue.values() if _epic_name(r.id) == name]
    standing = [r for r in same if r.state != "withdrawn" or r.claimed_by not in ("-", "")]
    if standing:
        row = standing[0]
        raise RegistryError(
            "id_duplicate",
            f"{row.id} is already in the Queue for {name!r} (state {row.state!r}, lane "
            f"{row.lane!r}); an epic gets one row. If {a.slug!r} is a DIFFERENT epic that happens "
            f"to share its first four slug tokens, rename one slug -- the board cannot tell them "
            f"apart from the id alone.",
        )
    if len(same) > 1:
        raise RegistryError(
            "id_duplicate",
            f"{len(same)} withdrawn rows carry the name {name!r} ({', '.join(r.id for r in same)}); "
            "which one this publish means is not decidable, so it refuses rather than guessing.",
        )
    if same:
        prior = same[0]
        # THE RECLAIM KEYS ON THE SLUG, NOT THE NAME. `_short_name` truncates to four tokens, so
        # two different specs can derive one name -- seven such pairs exist across the 459 slugs
        # on this data root. Reclaiming on the name alone let a DIFFERENT epic inherit this row's
        # number AND its withdrawal reason, silently, while the standing-row branch above refuses
        # the identical ambiguity loudly. Both the docstring and docs/REGISTRY.md say a publish of
        # the same SLUG reclaims; this is that sentence, enforced. Found by review.
        # The row does NOT store the slug -- only the id (four tokens) and a free-text work item -
        # so this can refuse only when the row POSITIVELY names a different one. On this fleet the
        # work item opens with the slug (`<slug> (pNNN, ...)`), which is the shape the reviewer's
        # repro used and the shape lead-dev publishes; an arbitrary title carries no slug and is
        # reclaimed exactly as before, so nothing that worked stops working.
        first = (prior.work_item or "").strip().split(" ", 1)[0]
        prior_slug = first if "-" in first else ""
        if prior_slug and prior_slug != a.slug:
            raise RegistryError(
                "id_duplicate",
                f"{prior.id} is a WITHDRAWN row for {prior_slug!r}, and {a.slug!r} only shares its "
                f"first four slug tokens ({name!r}). Reclaiming it would hand this epic another "
                f"epic's number and its withdrawal reason, so it refuses rather than guessing -- "
                f"rename one slug, or withdraw-and-cull the stale row first.",
            )
        notes = _annotate(prior.notes, f"[republished {_now_iso()} into {a.lane}]")
        body = _replace_data_row(
            text, "Queue", 0, prior.id, "id",
            [prior.id, a.title, a.lane, "-", "ready", "-", notes],
        )
        _cmd_publish.last_id = prior.id
        return _replace_body(text, "Queue", body)

    rid = _alloc_id("E", text, log_text, suffix=f"-{name}")
    new_cells = [rid, a.title, a.lane, "-", "ready", "-", ""]
    body = _append_data_row(_get_body(text, "Queue"), "id", new_cells, "Queue")
    _cmd_publish.last_id = rid
    return _replace_body(text, "Queue", body)


def _publisher_argv_checks(a, verb: str) -> str:
    """`relane` / `withdraw` refusals that argv alone decides, returning the reason; run
    PRE-LOCK too (gh#656, `_ARGV_CHECKS`)."""
    _require_capability(verb, a.actor)
    _require_id(a.id, _queue_key_ok, "work")
    reason = (getattr(a, "reason", None) or "").strip()
    if not reason:
        raise RegistryError(
            "queue_revise_needs_reason",
            f"--reason is required: a {verb} with no stated cause is indistinguishable from a "
            "mistake, and the next reader of this board is the lane whose work just moved.",
        )
    return reason


def _publisher_row(text: str, reg, a, verb: str):
    """The row a publisher may still revise, or a refusal saying why not.

    ONE guard for both revision verbs, because they are one authority: lead-dev assigns lanes
    (`docs/REGISTRY.md` and every builder skill make that its job), so lead-dev revises what it
    assigned -- and only while nobody has acted on it.

    The claimed-row refusal is the important half. `docs/REGISTRY.md` already prescribes the
    path for a row a builder holds -- route it a Note and let that builder release it cleanly --
    precisely because the claimant may be mid-build with a worktree and commits behind that row.
    A publisher yanking it would be the same class of harm `reap` is careful about, so this
    refuses and names the alternative rather than offering a --force.

    Evaluated INSIDE the mutation, which the CLI runs under the edit-lock after re-reading, so
    there is no window where a row is unclaimed at check time and claimed at write time.
    """
    reason = _publisher_argv_checks(a, verb)
    row = reg.queue.get(a.id)
    if row is None:
        _require_intact_table(text, "Queue")
        raise RegistryError("id_exists", f"{a.id} is not in the Queue")
    # Before the claimant/state judgement: `row` is the LAST duplicate; the twin may differ in either.
    _require_unique_key(text, "Queue", 0, a.id, "id")
    # `("-", "")` matches `_cmd_claim` exactly. A blank cell is how every other Queue path
    # reads "unclaimed", and a publisher alone treating it as claimed would refuse the one
    # verb that could normalise it -- while rendering `claimed by ''` at the operator.
    if row.claimed_by not in ("-", ""):
        raise RegistryError(
            "queue_revise_unclaimed",
            f"{a.id} is claimed by {row.claimed_by!r}; a publisher does not {verb} a row a "
            f"builder is holding -- it may be mid-build with commits behind it. Route "
            f"{row.claimed_by} a Note and let it release the row cleanly (REGISTRY.md #Queue).",
        )
    if row.state != "ready":
        # The remedy differs by state, so the message names the one that applies. "History
        # behind it" is true of building/parked/review/done and FALSE of `withdrawn` -- which
        # lead-dev set itself, on an untouched row -- so a single sentence would misdescribe the
        # one state this change introduced.
        remedy = (
            "re-publish it (a publish reclaims a withdrawn id)"
            if row.state == "withdrawn"
            else "it has history behind it; leave it to its claimant or to dev-manager"
        )
        raise RegistryError(
            "queue_revise_ready",
            f"{a.id} is {row.state!r}, not 'ready'; only an unstarted publication can be "
            f"{_PAST_PARTICIPLE[verb]} -- {remedy}.",
        )
    return row, reason


#: `f"{verb}d"` rendered "withdrawd" (gh#655).
_PAST_PARTICIPLE = {"relane": "relaned", "withdraw": "withdrawn"}


def _annotate(notes: str, text_: str) -> str:
    """Append to a row's notes, preserving the eligibility rationale already there."""
    existing = (notes or "").strip()
    return f"{existing} {text_}".strip()


def _cmd_relane(text: str, a) -> str:
    """Move an unclaimed, unstarted Queue row to another lane. lead-dev only.

    gh#32: `docs/REGISTRY.md` and every builder skill make lane assignment lead-dev's job and
    tell it to prune and re-lane every firing -- and the CLI had no verb for it. `publish`
    refuses a duplicate id, `release` preserves `lane` verbatim AND requires the actor be the
    claimant, `repair` and `cull` are steward-only. So a row published to the wrong lane was
    stuck there: claimable work parked behind a busy lane while an idle builder starved, with
    every loop behaving correctly. That is a missing primitive, not a policy question.
    """
    reg = parse_registry(text)
    row, reason = _publisher_row(text, reg, a, "relane")
    if a.lane not in LANES:
        raise RegistryError("queue_lane_enum", f"lane {a.lane!r} not in {list(LANES)}")
    if a.lane == row.lane:
        # A no-op that exits 0 reads as "the move happened". The caller believes the row is in
        # the other lane and stops checking, which is worse than the refusal.
        raise RegistryError(
            "queue_relane_noop",
            f"{a.id} is already in lane {row.lane!r}; nothing to move.",
        )
    notes = _annotate(row.notes, f"[relaned {_now_iso()} {row.lane}->{a.lane}: {reason}]")
    body = _replace_data_row(
        text, "Queue", 0, a.id, "id",
        [row.id, row.work_item, a.lane, "-", "ready", row.waiting_on, notes],
    )
    return _replace_body(text, "Queue", body)


def _cmd_withdraw(text: str, a) -> str:
    """Retract an unclaimed, unstarted Queue row. lead-dev only.

    The other half of the same duty: "prune every firing -- drop rows whose premises changed,
    whose deps un-satisfied, or that now conflict with another lane's claims".

    NOT a delete. The row stays on the board carrying `withdrawn` and the stated reason until
    the steward culls it, for the reason gh#98 landed on one epic earlier: a decision readable
    off the row is one anyone diffing the registry can see, while a row that simply vanishes
    leaves the lane that read the board a minute ago with no account of where its work went.

    NOT `done` either, however tempting the existing terminal state is. `done` is the CLAIMANT'S
    completion signal, and `cull` moves a consumed row VERBATIM into the append-only
    `registry-log.md` -- so a retraction filed as `done` becomes a permanent, never-corrected
    record that the work shipped, and until that sweep runs every lane reading the board sees
    retracted work rendered as completed.

    (An earlier draft justified this by claiming the auditor reconciles the `done` Queue set
    against its ledger. That is FALSE and review caught it: the audit ledger keys on epic slugs
    from the epic survey and never reads a Queue row. The decision was right; the reason was
    not.)
    """
    reg = parse_registry(text)
    row, reason = _publisher_row(text, reg, a, "withdraw")
    notes = _annotate(row.notes, f"[withdrawn {_now_iso()} from {row.lane}: {reason}]")
    body = _replace_data_row(
        text, "Queue", 0, a.id, "id",
        [row.id, row.work_item, row.lane, "-", "withdrawn", "-", notes],
    )
    return _replace_body(text, "Queue", body)


def _ask_argv_checks(a) -> None:
    """`ask`'s refusals that argv alone decides; run PRE-LOCK too (gh#656, `_ARGV_CHECKS`)."""
    _require_agent(a.actor)
    if a.urgency not in ("halted", "waiting", "advisory"):
        raise RegistryError("q_urgency_enum", f"urgency {a.urgency!r} invalid")
    if _field_is_absent(a.default):
        raise RegistryError(
            "q_needs_default",
            f"--default {(a.default or '').strip()!r} carries nothing. It is the answer the raiser "
            "would give ITSELF, and it is what the human reads first - a question that cannot "
            "propose an answer has not been grounded enough to ask. "
            f"{', '.join(sorted(repr(v) for v in ABSENT_FIELD_VALUES))} all mean absent here, and "
            "`validate` has always reported such a row as 'live question missing default' - so "
            "this refusal is the write path finally agreeing with its own gate.",
        )


def _cmd_ask(text: str, a, log_text: str) -> str:
    _ask_argv_checks(a)
    if a.urgency == "halted":
        _require_capability("ask", a.actor, condition="--urgency halted")
    # An advisory question is a call the raiser already made and kept moving on, so the assumption
    # is the whole content of the urgency. Absence is `_field_is_absent`, the same predicate the
    # validator uses - see ABSENT_FIELD_VALUES for why that is one function and not two literals.
    assumption = (a.assumption or "").strip() or "-"
    if a.urgency == "advisory" and _field_is_absent(assumption):
        raise RegistryError(
            "q_advisory_needs_assumption",
            "provide --assumption (what you assumed and proceeded on); use --urgency waiting to park instead",
        )
    work = a.work or "-"
    if work != "-" and not _queue_key_ok(work):
        raise RegistryError("id_format", f"work {work!r} is not an E- id or '-'")
    qid = _alloc_id("Q", text, log_text)
    d = {
        "state": "open",
        "urgency": a.urgency,
        "raised_by": a.actor,
        "work": work,
        "raised_at": _now_iso(),
        "question": a.question,
        "default": a.default,
        "assumption": assumption,
        "answer": "-",
        "answered_by": "-",
        "answered_at": "-",
    }
    body = _append_qblock(_get_body(text, "Questions"), qid, d)
    # Printed by the dispatcher after the write, like publish's id (gh#649).
    _cmd_ask.last_id = qid
    return _replace_body(text, "Questions", body)


def _cmd_answer(text: str, a) -> str:
    _require_capability("answer", a.actor)
    _require_id(a.id, _q_id_ok, "question")
    if a.by not in ANSWERED_BY:
        raise RegistryError("q_answered_by_enum", f"--by {a.by!r} not in {list(ANSWERED_BY)}")
    # `--by` is a CLAIM ABOUT WHO DECIDED, so it is bound to who is writing. Enum membership
    # alone let `answer --actor steward --by human` sign the human's name in one sanctioned
    # command -- no repair, no malformed line, nothing for the repair guards above to catch, and
    # `validate` green because `human` is in the enum. dev-manager is the seat that actually
    # faces the human (REGISTRY.md #Questions step 4: it relays human-only calls), so it is the
    # only actor that may relay a human answer. steward may still answer as itself. gh#719: the
    # manager values are bound the same way, each to its own role -- every --by value that has
    # a condition in the map is checked, so a new binding is a map entry, not a new branch here.
    condition = f"--by {a.by}"
    if condition in ACTOR_CAPABILITY["answer"].conditions:
        _require_capability("answer", a.actor, condition=condition)
    _require_unique_qid(text, a.id)
    reg = parse_registry(text)
    q = reg.questions.get(a.id)
    if q is None:
        raise RegistryError("id_exists", f"{a.id} not found")
    if q.fields.get("state") != "open":
        raise RegistryError("q_answer_open", f"{a.id} is state={q.fields.get('state')}, not open")
    updates = {"state": "answered", "answer": a.answer, "answered_by": a.by, "answered_at": _now_iso()}
    body = _edit_qfield(_get_body(text, "Questions"), a.id, updates)
    new_text = _replace_body(text, "Questions", body)
    # Audit trail, same mechanism as `repair`. Without it a steward-authored "human decision" is
    # byte-identical on disk to one the human actually gave: the block records WHO decided but
    # nothing records who WROTE it. The note carries the id and both roles, never the answer
    # text -- that is arbitrary actor prose, and re-emitting it into a Note is the mistake the
    # reap line already paid for (see _MACHINE_NOTE_MARKERS).
    return _append_note(new_text, f"{a.actor} -> fleet: answered {a.id} (answered_by: {a.by})")


def _cmd_apply(text: str, a) -> str:
    _require_agent(a.actor)
    _require_id(a.id, _q_id_ok, "question")
    _require_unique_qid(text, a.id)
    reg = parse_registry(text)
    q = reg.questions.get(a.id)
    if q is None:
        raise RegistryError("id_exists", f"{a.id} not found")
    if q.fields.get("raised_by") != a.actor:
        raise RegistryError("q_apply_raiser", f"{a.id} was raised by {q.fields.get('raised_by')!r}, not {a.actor}")
    if q.fields.get("state") != "answered":
        raise RegistryError("q_apply_answered", f"{a.id} is state={q.fields.get('state')}, not answered")
    _refuse_if_waited_on(reg, a.id, "apply")
    body = _edit_qfield(_get_body(text, "Questions"), a.id, {"state": "applied"})
    return _replace_body(text, "Questions", body)


def _retract_argv_checks(a) -> str:
    """`retract`'s refusals that argv alone decides, returning the reason; run PRE-LOCK too
    (gh#656, `_ARGV_CHECKS`)."""
    _require_agent(a.actor)
    _require_id(a.id, _q_id_ok, "question")
    reason = (getattr(a, "reason", None) or "").strip()
    if _field_is_absent(reason):
        raise RegistryError(
            "q_retract_needs_reason",
            "--reason must say what collapsed. A question leaving the human's queue with no "
            "reason is indistinguishable from one quietly hidden, and the retraction record is "
            "the only place a later reader can tell a dead premise from an inconvenient question.",
        )
    return reason


def _cmd_retract(text: str, a) -> str:
    """Take a question that should never have been asked OFF the human's queue, saying why.

    RAISER ONLY, and `open` ONLY. Both bounds are the same argument from opposite ends: the
    raiser is the one who can know the premise collapsed, and a question that is already
    `answered` has a decision in it -- retracting that would discard someone's answer, which is
    a different and much worse act than withdrawing a request.

    NOT `answered`. Nothing was decided, and recording a retraction as an answer would put a
    decision nobody made into the pairing that `read --answered-for` prints.

    WHY THIS EXISTS. Two live rows collapsed within two wakes of being raised: Q-10028's premise
    was true when written and false forty minutes later, and Q-10030's central invariant was
    already false when written and was disproved by its own author an hour later. Neither could
    be retired: `withdraw` is Queue-only and refuses a `Q-` id at the arg guard, `cull` reports
    `0 questions`, and the only terminal state was `answered`. So clearing a question that should
    never have been asked cost exactly the human attention it wrongly spent.

    The reason is REQUIRED. A row that leaves the queue without saying why is indistinguishable
    from one that was quietly hidden, and the next reader cannot tell a collapsed premise from an
    inconvenient question.
    """
    reason = _retract_argv_checks(a)
    _require_unique_qid(text, a.id)
    reg = parse_registry(text)
    q = reg.questions.get(a.id)
    if q is None:
        raise RegistryError("id_exists", f"{a.id} not found")
    if q.fields.get("raised_by") != a.actor:
        raise RegistryError(
            "q_retract_raiser",
            f"{a.id} was raised by {q.fields.get('raised_by')!r}, not {a.actor}. Only the raiser "
            "can know its premise collapsed; anyone else retracting it is deciding on the "
            "raiser's behalf without answering.",
        )
    st = q.fields.get("state")
    if st != "open":
        why = {
            "answered": "it carries a decision, and retracting it would discard someone's answer "
                        "rather than withdraw a request. Apply it, or raise a NEW question if "
                        "the answer no longer fits.",
            "applied": "it is already consumed - the answer was folded in. Nothing is waiting on "
                       "it, so there is nothing to retract. Raise a NEW question if the decision "
                       "needs revisiting.",
            Q_RETRACTED: "it is already retracted. Nothing further to do.",
        }.get(st, "only an OPEN question can be retracted.")
        raise RegistryError("q_retract_open_only", f"{a.id} is state={st!r}, not open: {why}")
    _refuse_if_waited_on(reg, a.id, "retract")
    body = _edit_qfield(_get_body(text, "Questions"), a.id, {
        "state": Q_RETRACTED,
        "retracted_at": _now_iso(),
        "retraction": reason,
    })
    return _replace_body(text, "Questions", body)


def _cmd_raise_blocker(text: str, a, log_text: str) -> str:
    _require_agent(a.actor)
    work = a.work or "-"
    if work != "-" and not _queue_key_ok(work):
        raise RegistryError("id_format", f"work {work!r} is not an E- id or '-'")
    bid = _alloc_id("B", text, log_text)
    owner = a.owner or "-"
    if owner != "-":
        _require_agent(owner, "--owner")
    new_cells = [bid, a.actor, owner, work, "open", a.description]
    body = _append_data_row(_get_body(text, "Blockers"), "id", new_cells, "Blockers")
    _cmd_raise_blocker.last_id = bid
    return _replace_body(text, "Blockers", body)


def _cmd_resolve_blocker(text: str, a) -> str:
    _require_agent(a.actor)
    _require_id(a.id, _blocker_key_ok, "blocker")
    reg = parse_registry(text)
    b = reg.blockers.get(a.id)
    if b is None:
        raise RegistryError("id_exists", f"{a.id} not found")
    if b.state != "open":
        raise RegistryError("blocker_open", f"{a.id} is state={b.state}, not open")
    if b.owner != a.actor:
        raise RegistryError("blocker_owner_resolves", f"{a.id} owner is {b.owner!r}, not {a.actor}")
    _refuse_if_waited_on(reg, a.id, "resolve-blocker")
    new_cells = [b.id, b.raised_by, b.owner, b.work, "resolved", b.description]
    body = _replace_data_row(text, "Blockers", 0, a.id, "id", new_cells)
    return _replace_body(text, "Blockers", body)


def _cmd_assign_blocker(text: str, a) -> str:
    _require_capability("assign-blocker", a.actor)
    _require_id(a.id, _blocker_key_ok, "blocker")
    _require_agent(a.owner, "--owner")
    reg = parse_registry(text)
    b = reg.blockers.get(a.id)
    if b is None:
        raise RegistryError("id_exists", f"{a.id} not found")
    if b.state != "open":
        raise RegistryError("blocker_open", f"{a.id} is state={b.state}, not open")
    # The steward hands away only what it holds or what nobody holds: re-assigning another
    # seat's blocker would override that seat's call, which is dev-manager's to make. The
    # exemption is read from the map so `read --capabilities` cannot drift from this guard.
    if (
        a.actor not in ACTOR_CAPABILITY["assign-blocker"].janitors
        and b.owner not in (a.actor, "-")
    ):
        raise RegistryError(
            "blocker_assign_steward_owned",
            f"{a.id} owner is {b.owner!r}; {a.actor} assigns only its own or unowned blockers",
        )
    new_cells = [b.id, b.raised_by, a.owner, b.work, b.state, b.description]
    body = _replace_data_row(text, "Blockers", 0, a.id, "id", new_cells)
    return _replace_body(text, "Blockers", body)


def _cmd_directive(text: str, a) -> str:
    _require_capability("directive", a.actor)
    # An EMPTY Directive is unrecoverable, not merely useless (gh#236 review). "" is a substring
    # of every text, so the board's ack guard then refuses `deploy-lock take` for every lane -
    # with no ack AND with any needle, since the only needle that could match is itself refused
    # as empty. The sole repair left is `expire-directive --index`, which this module documents
    # as re-basing and unsafe. Both sibling Directive surfaces already refuse empty input
    # (`directive_ack_empty`, `directive_expire_empty`); this was the one that did not.
    if not (a.text or "").strip():
        raise RegistryError("directive_text_empty", "--text was empty or whitespace-only")
    # Containment warning, decided against the board BEFORE this Directive joins it (gh#236).
    # Recorded here and printed by the dispatcher AFTER the write lands, for `expire-directive`'s
    # reason: a warning about a Directive a failed `_atomic_write` never wrote is a false report.
    # Set UNCONDITIONALLY, so a previous call's warning cannot be reprinted for a clean one.
    _cmd_directive.last_containment_warning = None
    try:
        active = active_directives(parse_registry(text))
    except RegistryError:
        # Same reasoning as the heartbeat stamp below: `parse_registry` validates ALL five
        # sections, and a registry missing one must not take the human's standing instruction
        # down with it. No board to compare against means no warning, never no Directive.
        active = None
    if active:
        _cmd_directive.last_containment_warning = directive_containment_warning(a.text, active)
    body = _get_body(text, "Directives")
    trimmed = body.rstrip("\n")
    trailing = body[len(trimmed):]
    new_line = f"- [{_now_iso()}] {a.text}"
    new_body = f"{trimmed}\n{new_line}\n{trailing}" if trimmed else f"{new_line}\n{trailing}"
    text = _replace_body(text, "Directives", new_body)
    # Writing a Directive IS proof of life: refresh dev-manager's heartbeat in this same locked
    # write (audit pattern 8 — the fleet lost its only sweeper's liveness signal for hours).
    # Heartbeat ONLY: phase/status/work/eta/waiting_on stay whatever the row owner last wrote.
    try:
        row = parse_registry(text).agents.get(a.actor)
    except RegistryError:
        # parse_registry validates ALL five sections; a registry missing one would
        # otherwise abort this write and take the Directive with it.
        row = None
    if row is None:
        # No row to stamp (a registry that predates the roster). The Directive is the
        # human's standing instruction and the payload of this call - never lose it for
        # want of a side effect; the missing row is dev-manager's own to restore.
        return text
    cells = [row.agent, row.status, row.work, row.phase, _now_iso(), row.eta, row.waiting_on]
    return _stamp_agent_row(text, a.actor, cells)


def _cmd_expire_directive(text: str, a) -> str:
    """Expire one standing Directive, addressed BY CONTENT.

    gh#88, hit live: ``--index`` is 1-based over the *currently live* set, so a 0-based reading
    silently expires the neighbour — and because expiring a row re-bases the list, the same index
    used twice targets two different directives. Exit was 0 and the output named nothing, so the
    operator could only find out by diffing the block afterwards. A Directive is a standing human
    instruction every builder re-reads before an irreversible action; expiring the wrong one
    silently removes a live safety rail.

    **Position is not an identity.** ``--match`` takes a substring that must hit exactly one live
    Directive — the same semantics ``--ack-directive`` uses, so the caller has to have read the
    thing it is expiring. ``note --strike --match`` refuses ambiguity the same way (gh#650).
    ``--index`` still works for compatibility
    but is documented as re-basing, and BOTH paths now name what they expired.
    """
    _require_capability("expire-directive", a.actor)
    body = _get_body(text, "Directives")
    lines = body.split("\n")
    bullets = [
        i for i, ln in enumerate(lines)
        if ln.strip().startswith("- ") and not directive_text_is_expired(_bullet_text(ln))
    ]

    raw_match = getattr(a, "match", None)
    index = getattr(a, "index", None)
    # PRESENCE, not truthiness. `--match "" --index 1` used to slip past the exclusivity guard
    # (empty string is falsy) and take the index path — the re-basing path this whole change
    # exists to steer callers away from, chosen silently. `--ack-directive` guards the same case
    # with its own `directive_ack_empty`; this is that guard.
    if raw_match is not None and not raw_match.strip():
        raise RegistryError(
            "directive_expire_empty",
            "--match requires a substring quoted from the Directive you read; an empty value "
            "matches every Directive and names none",
        )
    needle = (raw_match or "").strip()
    if raw_match is not None and index is not None:
        raise RegistryError(
            "directive_expire_exclusive",
            "pass --match OR --index, not both: they can name different Directives, and the "
            "one that silently wins is the failure this guard exists to prevent",
        )
    if raw_match is None and index is None:
        raise RegistryError(
            "directive_expire_needs_target",
            "expire-directive requires --match <substring> (preferred: addresses by content) "
            "or --index <n>",
        )
    # AFTER the flag-shape guards: a malformed invocation should say so rather than be masked by
    # the state of the section it never got to address.
    if not bullets:
        raise RegistryError(
            "directive_expire_no_match", "there are no active Directives to expire"
        )

    if needle:
        # Same rule as --ack-directive, and literally the same surface (`directive_match_text`):
        # nothing, or more than one, is an error. A substring that matches several Directives
        # cannot say which the caller meant, and guessing is how the wrong safety rail gets
        # removed. The minted [<ISO>] stamp is excluded here for gh#127's reason applied to the
        # sharper verb: matching the whole bullet let a caller expire a standing instruction by
        # a timestamp the CLI generated, i.e. REMOVE a safety rail without reading it.
        hits = [i for i in bullets if needle in directive_match_text(_bullet_text(lines[i]))]
        if not hits:
            raise RegistryError(
                "directive_expire_no_match",
                f"--match {needle!r} matched no active Directive (have {len(bullets)}) - quote "
                "a substring of a Directive's TEXT; its leading [<ISO>] stamp is CLI metadata, "
                "not content, and is not searched",
            )
        if len(hits) > 1:
            preview = "; ".join(_bullet_text(lines[i])[:60] for i in hits[:3])
            positions = ", ".join(str(bullets.index(i) + 1) for i in hits)
            raise RegistryError(
                "directive_expire_ambiguous",
                f"--match {needle!r} matched {len(hits)} active Directives, so it names none of "
                f"them: {preview}. Quote a longer substring — or, if they are genuinely "
                f"identical and no substring can separate them, use --index (they are at "
                f"{positions}, and the list re-bases after each expiry).",
            )
        idx = hits[0]
    else:
        if index < 1 or index > len(bullets):
            raise RegistryError("id_exists", f"no active directive at index {index} (have {len(bullets)})")
        idx = bullets[index - 1]

    # Name what was expired, on BOTH paths -- silence was half the bug. Recorded here and
    # printed by the dispatcher AFTER the write lands: printing from this pure edit function
    # announced an expiry that a failed `_atomic_write` then never performed.
    _cmd_expire_directive.last_expired = _bullet_text(lines[idx])[:120]
    lines[idx] = lines[idx].replace("- ", f"- [expired {_now_iso()}] ", 1)
    return _replace_body(text, "Directives", "\n".join(lines))


def _cmd_cull(text: str, a) -> tuple[str, str, dict[str, int], list[str]]:
    """Move consumed entries — applied AND retracted questions, done AND withdrawn queue rows,
    resolved blockers, expired directives, struck Notes — to the log.

    Returns (new_registry_text, appended_log_text, counts_by_category, overflow_filenames).
    steward only.

    Every category's retention key is STATE, never age and never count. A note nobody has
    struck is a note nobody has consumed, and it survives this sweep at ANY age.

    STAYS A PURE TEXT FUNCTION. The overflow files the culled ENTRIES point at — any of the five
    categories, not the struck notes alone (gh#78) — are *named* here and touched nowhere: the
    fourth return value is a list of filenames for `_run_mutation` —
    which already owns every file write and the log's fsync — to read, inline and delete. The
    alternative (handing this function the resolved directory) would put the durability
    ordering in one place and the deletion in another, and would make the retention rules
    untestable without a filesystem.
    """
    _require_capability("cull", a.actor)
    culled: list[str] = []

    # Questions: drop CONSUMED blocks - applied AND retracted. Both are terminal and nothing is
    # waiting on either, which is the same test `_live_ids` applies; keeping the two lists in step
    # is why that constant exists rather than a literal in each place.
    q_body = _get_body(text, "Questions")
    keep_blocks, removed = _split_qblocks_by_state(q_body, drop_state=CONSUMED_QUESTION_STATES)
    culled.extend(removed)
    if removed:
        text = _replace_body(text, "Questions", keep_blocks)

    # Queue: drop done rows, and withdrawn ones -- both are consumed, by the section's own
    # STATE-not-age retention rule. Withdrawn is swept here rather than left to accumulate for
    # the same reason done is: the Queue means claimable work, and a terminal row that stays is
    # a row every lane re-reads every wake.
    q_body = _get_body(text, "Queue")
    new_q, removed_rows = _drop_table_rows(q_body, "Queue", state_index=4, drop_state="done")
    new_q, removed_withdrawn = _drop_table_rows(
        new_q, "Queue", state_index=4, drop_state="withdrawn"
    )
    removed_rows = removed_rows + removed_withdrawn
    culled.extend(removed_rows)
    if removed_rows:
        text = _replace_body(text, "Queue", new_q)

    # Notes: drop struck coordination notes. Sequenced AFTER the done-row drop above because
    # both edit the Queue body — this re-reads the body from the already-updated text, so
    # neither write clobbers the other.
    n_body = _get_body(text, "Queue")
    kept_n, removed_n = [], []
    for ln in n_body.split("\n"):
        stripped = ln.lstrip()
        # The Queue table is excluded by the bullet test itself: `_cells` only ever returns
        # cells for a line whose strip() starts with "|", which can never also start with
        # "- ". An explicit table check here would be unreachable, and reads as protection
        # that is not there.
        is_note = (
            stripped.startswith("- ")
            and not _is_notes_header(ln)       # the header bullet itself
            and not _is_deploy_lock_field(ln)  # a Queue field line, not a note
        )
        if is_note and _is_struck_note(stripped[2:]):
            # rstrip, NOT the lstripped copy: the log receives the line VERBATIM, indent and
            # all, so pasting one back into the Queue restores a Note rather than a top-level
            # bullet that no longer reads as a child of the **Notes** header.
            removed_n.append(ln.rstrip())
        else:
            kept_n.append(ln)
    culled.extend(removed_n)
    if removed_n:
        text = _replace_body(text, "Queue", "\n".join(kept_n))

    # gh#119: leave a receipt for every withdrawn row this sweep removed. AFTER the struck-note
    # drop above, not before: these are unstruck and would survive that pass anyway, but writing
    # them into a body the same function is still filtering is how two edits to one section
    # start depending on their order.
    for receipt in _withdrawal_receipts(removed_withdrawn):
        text = _append_note(text, receipt)

    # Blockers: drop resolved rows.
    b_body = _get_body(text, "Blockers")
    new_b, removed_b = _drop_table_rows(b_body, "Blockers", state_index=4, drop_state="resolved")
    culled.extend(removed_b)
    if removed_b:
        text = _replace_body(text, "Blockers", new_b)

    # Directives: drop expired bullets.
    d_body = _get_body(text, "Directives")
    d_lines = d_body.split("\n")
    kept, removed_d = [], []
    for ln in d_lines:
        if ln.strip().startswith("- ") and directive_text_is_expired(_bullet_text(ln)):
            removed_d.append(ln.strip())
        else:
            kept.append(ln)
    culled.extend(removed_d)
    if removed_d:
        text = _replace_body(text, "Directives", "\n".join(kept))

    log_add = ""
    if culled:
        stamp = _now_iso()
        log_add = f"\n<!-- culled {stamp} -->\n" + "\n".join(culled) + "\n"
    counts = {
        "questions": len(removed),
        "queue rows": len(removed_rows),
        "blockers": len(removed_b),
        "directives": len(removed_d),
        "notes": len(removed_n),
    }
    # RETENTION AND COLLECTION READ THE SAME SURFACE FROM OPPOSITE ENDS, and the invariant is
    # that they are SYMMETRIC: a row strong enough to PROTECT a file has to be strong enough to
    # COLLECT it. `still_referenced` is computed over the whole post-cull `text`, so a pointer
    # parked in a Queue row, a Blocker, a Directive or a Question block protects a file exactly
    # like a live Note does.
    #
    # gh#78: collection read `removed_n` alone, so the halves were asymmetric. When one of those
    # other rows held the LAST pointer, culling it consumed that reference and collected
    # nothing: the body was inlined nowhere, the file deleted never, and no stranded marker
    # written. That file is then unreachable BY CONSTRUCTION — nothing walks this directory,
    # only pointers reach it — so it is not clutter that a later sweep tidies, it is a leak.
    #
    # The fix reads `culled` itself, which IS the five removed lists in sweep order —
    # questions, queue rows (done + withdrawn), struck notes, resolved blockers, expired
    # directives — concatenated by the `culled.extend(...)` calls above. Reading the same list
    # the archive receives, rather than a hand-maintained re-listing of its parts, is the point:
    # the two sets cannot drift, and a sixth category added later is covered by construction
    # rather than by remembering. Collection order is therefore deterministic and stable.
    #
    # Question blocks belong here for the same reason as the other four, and are called out
    # because it is the easy one to miss: `still_referenced` reads the Questions section too, so
    # a pointer living ONLY in a question block protects a file today — and culling that block
    # strands it in exactly the same way as any other row.
    #
    # "Orphaned" is a property of the SURFACE, not of the one row being culled: one analysis
    # routed to two lanes is two rows carrying the same pointer, and consuming one consumes one
    # reference, not the file. Subtracting what still stands is what makes the retention rule
    # true rather than assumed — without it a live routed row is left pointing at nothing, and
    # its body only in the archive the fleet does not read in the hot path.
    #
    # The two matchers stay DIFFERENT, and widening the row set above is orthogonal to that.
    # LOOSE here on purpose (gh#136): this set only ever PROTECTS a file, so the two are chosen
    # by which way each one fails — collection under-matches into clutter, and the subtraction
    # must under-match into nothing at all.
    still_referenced = set(_note_overflow_refs_loose(text))
    overflow: list[str] = []
    for ln in culled:
        for name in _note_overflow_refs(ln):
            # several culled rows may point at ONE file; collect it once, and only once nothing
            # left standing on the surface points at it
            if name not in overflow and name not in still_referenced:
                overflow.append(name)
    return text, log_add, counts, overflow


def _split_qblocks_by_state(questions_body: str, drop_state) -> tuple[str, list[str]]:
    """Split question blocks on state. ``drop_state`` is one state or a collection of them.

    A COLLECTION, because there are now two consumed states and there was very nearly a third
    kind of bug: the docs, the steward's sweep bullet and `_live_ids` were all updated to say a
    retracted question is culled like an applied one, while this function still took a single
    string and swept only `applied`. Three prose claims and one code path that did not do it.
    """
    drop_states = {drop_state} if isinstance(drop_state, str) else set(drop_state)
    matches = list(_QBLOCK_RE.finditer(questions_body))
    if not matches:
        return questions_body, []
    prefix = questions_body[: matches[0].start()]
    kept_blocks, removed = [], []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(questions_body)
        block = questions_body[m.start() : end]
        state = None
        for line in block.split("\n"):
            fm = _QFIELD_RE.match(line)
            if fm and fm.group(1) == "state":
                state = fm.group(2).strip()
                break
        if state in drop_states:
            removed.append(block.rstrip("\n"))
        else:
            kept_blocks.append(block.rstrip("\n"))
    new_body = prefix.rstrip("\n")
    if kept_blocks:
        new_body = (new_body + "\n\n" if new_body else "") + "\n\n".join(kept_blocks) + "\n"
    else:
        new_body = prefix if prefix else "\n"
    return new_body, removed


#: The annotation `withdraw` writes into a row's notes cell. Only the REASON is read out of
#: it -- the lane comes from the row's own lane cell, which is structured rather than prose.
_WITHDRAWN_ANNOTATION = re.compile(r"\[withdrawn\s+\S+\s+from\s+[^:\]]+:\s*(?P<reason>.*?)\]")


def _publish_seat() -> str:
    """The seat `publish` is bound to, READ OFF the capability table rather than spelled again.

    gh#291. The withdrawal receipt below addresses the one actor that can act on it, and that
    actor is whoever `ACTOR_CAPABILITY["publish"]` says it is. A second literal `"lead-dev"`
    here would be a copy of an authorization rule kept somewhere the rule does not look: the day
    the verb moves, the receipt keeps routing the only re-publishable record at a seat that can
    no longer re-publish it, and nothing fails -- the note still appears, in the wrong inbox.
    """
    actors = ACTOR_CAPABILITY["publish"].actors
    return actors[0] if actors else ""


def _budgeted_receipt(head: str, tail: str, reason: str) -> str:
    """Quote *reason* into *head* only as far as the coordination-note cap allows.

    The budget is computed from THIS receipt's own head and tail. The two receipts gh#291 emits
    are different lengths -- the publisher's carries the lane name as well -- so a shared room
    figure would either over-trim the short one or, worse, hand the `note` verb a line it
    refuses, which is precisely the defect `_withdrawal_receipts` warns about one docstring down.
    """
    if reason:
        room = NOTE_MAX_BYTES - len(head.encode("utf-8")) - len(tail.encode("utf-8")) - len(' ("...")')
        if room > 20:
            trimmed = reason.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip()
            ellipsis = "..." if len(trimmed) < len(reason) else ""
            head = f'{head} ("{trimmed}{ellipsis}")'
    return head + tail


def _withdrawal_receipts(rows: list[str]) -> list[str]:
    """One unstruck Note per culled `withdrawn` row, so the commitment outlives the sweep.

    gh#119. A withdrawn row is RECLAIMABLE -- a later `publish` of the same id brings it back --
    so its reason routinely carries a live condition, "re-publish when X completes", that nobody
    has revoked. `withdraw`'s own docstring says the row exists to keep that readable: *"a row
    that simply vanishes leaves the lane that read the board a minute ago with no account of
    where its work went."* The cull then removes it on the same pass as a row that is genuinely
    finished with, and the note that announced the withdrawal is culled beside it, because the
    recipient consumed it correctly and struck it. Both halves are right and together they leave
    the commitment with no home on the board.

    Nothing is DESTROYED -- the row goes verbatim into `registry-log.md`. What is lost is
    DISCOVERABILITY, which is the test the issue closes with: after a cull, can a lane still find
    that work is owed to it and under what condition? Measured on 2026-09-05, `E-0226` survived
    the sweep only in a phase cell its own lane overwrites on the next `status` write.

    A RECEIPT IS THE SMALLEST FIX THAT PASSES THAT TEST, and it needs no new retention rule: this
    module already guarantees the shape. "A note nobody has struck is a note nobody has consumed,
    and it survives this sweep at ANY age." So the sweep hands the commitment to the one carrier
    it provably cannot reach, in the lane's own inbox -- which is exactly the mitigation
    dev-manager performed by hand for `E-0059` before this was code.

    THE LANE IS NOT PARSED OUT OF PROSE. It is the row's own lane cell; only the reason is read
    from the annotation, and a reason that cannot be read still yields a receipt. Refusing to
    emit one because the prose is unparseable would reproduce the defect for exactly the rows
    whose records are already damaged.

    THE RECEIPT GOES TO BOTH SEATS WHEN THEY DIFFER (gh#291). The original single note named two
    verbs -- "re-publish it, or strike this note" -- and routed it to the row's lane. Only one of
    those is performable by a junior lane: `note --strike` is available to the note's OWN
    addressee (or a janitor), so a lane can strike a receipt routed to it, while `publish` is
    bound to `_publish_seat()` and it cannot. Not "strike is open to anyone" -- it is not, and
    the distinction is the whole asymmetry: the one verb a lane can reach on its own receipt is
    the destructive one. So for a jr-dev row the sweep was handing the last surviving
    record of an owed commitment to the one seat whose only available affordance is to DELETE it,
    and a lane following the note's own instruction discharges the record it exists to preserve.
    The lane still gets its receipt -- it is the seat that is waiting on the work -- but it is
    told to escalate rather than to re-publish, and the publisher gets its own copy carrying the
    lane name, since the lane is the half of the record the publisher's copy would otherwise
    lose. When the row's lane IS the publisher's, one note is right: two notes in one inbox is the
    same record twice.

    THAT ONE NOTE IS ADDRESSED TO THE SEAT, NOT THE LANE (gh#488). The publisher's lane is
    `lead`, its seat `lead-dev`, and `_strike_note` authorizes by addressee == actor -- with no
    lane-to-seat mapping, deliberately. A `cull -> lead:` receipt therefore had no actor able to
    consume it but the janitor, so the publisher could read its own receipt and never discharge
    it. Only an empty seat keeps lane addressing, for the reason given at the fallback below.
    """
    out: list[str] = []
    seat = _publish_seat()
    # `lead-dev` publishes into lane `lead`, so a row already the publisher's is recognised by
    # either spelling -- both derived from the seat, neither written down a second time.
    seat_lanes = {seat, LANE_OF.get(seat, seat)} if seat else set()
    for row in rows:
        cells = _cells(row)
        if not cells or len(cells) < 3:
            continue
        rid, lane = cells[0], (cells[2] or "").strip()
        notes = cells[6] if len(cells) > 6 else ""
        found = _WITHDRAWN_ANNOTATION.findall(notes or "")
        reason = (found[-1].strip() if found else "")
        publisher_tail = (
            ". Full row in registry-log.md. Re-publish it, or strike this note once it is no longer owed."
        )
        # `not seat` is the verb bound to nobody -- a configuration this module does not ship.
        # It falls back to the single pre-gh#291 note rather than addressing a receipt to the
        # empty string, because an unroutable note is worse than an imperfectly routed one.
        if not seat or lane in seat_lanes:
            out.append(_budgeted_receipt(
                f"cull -> {seat or lane}: {rid} left the board as withdrawn", publisher_tail, reason))
            continue
        out.append(_budgeted_receipt(
            f"cull -> {lane}: {rid} left the board as withdrawn",
            f". Full row in registry-log.md. Tell {seat} if it is still owed;"
            " strike this note once it is not.",
            reason,
        ))
        out.append(_budgeted_receipt(
            f"cull -> {seat}: {rid} (lane {lane}) left the board as withdrawn", publisher_tail, reason))
    return out


def _drop_table_rows(body: str, section: str, state_index: int, drop_state: str) -> tuple[str, list[str]]:
    """Drop *section*'s rows in *drop_state*. Only a row whose key `_row_key_ok` accepts is a
    candidate: a malformed key is `repair`'s to mend, never cull's to sweep. The key is the only
    gate -- a short row the parser drops on column count is still swept once its state cell is
    reached."""
    header0 = TABLE_SHAPES[section][1]
    lines = body.split("\n")
    kept, removed = [], []
    for line in lines:
        cells = _cells(line)
        if cells and not _is_separator(cells) and cells[0] not in (header0,) and _row_key_ok(section, cells[0]):
            if len(cells) > state_index and cells[state_index] == drop_state:
                removed.append(line.strip())
                continue
        kept.append(line)
    return "\n".join(kept), removed


# --------------------------------------------------------------------------- #
# Deploy lock (a Queue content line, not the mkdir edit-lock)
# --------------------------------------------------------------------------- #
def _set_deploy_line(text: str, holder: str, taken_at: str) -> str:
    body = _get_body(text, "Queue")
    new_body, n = _DEPLOY_RE.subn(
        lambda m: m.group(0)[: m.start(1) - m.start(0)] + holder + ", taken-at: " + taken_at,
        body,
        count=1,
    )
    if n == 0:
        # Lost structure, so the rule text (which names no defect) gives no recovery: say it here.
        raise RegistryError("file_damaged", _deploy_line_problem(body) or _DEPLOY_LINE_MISSING)
    return _replace_body(text, "Queue", new_body)


def _require_deploy_line(text: str) -> None:
    """Refuse `file_damaged` when the Queue has no readable Deploy lock field line.

    `release` and `assert` need this before their holder check: with the line gone the parse
    reports the default holder "none", and the caller would be told someone else holds the
    lock, or that it is FREE to take, rather than that the board is damaged.
    """
    problem = _deploy_line_problem(_get_body(text, "Queue"))
    if problem is not None:
        raise RegistryError("file_damaged", problem)


def _require_directive_acks(reg: Registry, acks) -> None:
    """Refuse a guarded mutation until EVERY active Directive has been acknowledged.

    Directives are free human prose with no scope field, so no amount of regex can decide
    whether "hold all merges until the migration lands" binds this particular deploy. The
    design does not guess: it makes the caller quote each standing instruction back, which
    is only possible after reading it. Zero active Directives = zero acks = the behaviour
    this command had before the guard existed.

    Needles are matched against `directive_match_text` — the Directive's TEXT, without the
    ``[<ISO>]`` the CLI minted. Matching the whole bullet handed every Directive a needle the
    CLI generated itself, so the sentence above was false as written (gh#127); read that
    function for why the metadata is out and why a date in the human's own body stays in.

    Match semantics are `note --strike --match`'s: a substring matching nothing, or matching
    more than one Directive, is refused. A no-op ack would be indistinguishable from a read.

    AMBIGUITY IS A PROPERTY OF THE NEEDLE AND THE BOARD, NEVER OF ARGUMENT ORDER (gh#128).
    It is decided against every ACTIVE Directive, not against the unacked remainder. Deciding
    it against the remainder made the same needle set pass in one order and refuse in the
    other: consuming a narrow needle first left a broad needle matching only one Directive
    still standing, so the broad needle was accepted as the acknowledgement of a Directive the
    caller may never have read. That is the guard certifying a reading that did not happen —
    the same defect class as gh#127, reached by ordering instead of by metadata. One needle is
    one acknowledgement; a needle naming two distinct standing instructions evidences reading
    one of them and cannot discharge both, whichever position it is written in.

    The cost is real and is the intended one: a needle that legitimately matches two Directives
    can no longer be rescued by ordering, so a caller acknowledging N Directives must supply N
    needles each unambiguous against the WHOLE board. That is what the loop prose already
    demands ("narrow the needle; never reorder until it passes").

    One case it makes strictly harder, recorded because it is a wedge and not an ergonomic
    complaint: when one Directive's text is a substring of another's ("hold merges" alongside
    "hold merges until the migration lands"), every needle for the shorter one also matches the
    longer, so the shorter has no unambiguous needle and the lock cannot be taken at all. The
    old order-sensitivity papered over it by exactly the accident this fixes. It is the same
    indistinguishable-by-construction wedge the identical-Directives branch below exists to
    prevent, one containment step weaker, and the escape is the human's: expire the contained
    Directive and restate it in words the other does not contain.
    """
    active = active_directives(reg)
    if not active:
        return
    acked: set[int] = set()
    for needle in acks or []:
        # An empty or whitespace-only needle is a substring of EVERY directive, so with a
        # single standing instruction it would match exactly one, satisfy the gate, and take
        # the lock without the caller having read a word. That is the guard's whole purpose
        # inverted, so it is refused before matching.
        if not needle.strip():
            raise DirectiveAckError(
                "directive_ack_empty",
                "--ack-directive requires a substring quoted from the Directive you read; "
                "an empty value acknowledges nothing",
            )
        hits = [i for i, d in enumerate(active) if needle in directive_match_text(d)]
        if not hits:
            # Naming the searchable surface is not noise here: the `directive_ack_required`
            # refusal QUOTES each unacked bullet with its minted stamp, so the stamp is the
            # first thing an operator sees and the likeliest thing they paste back.
            raise DirectiveAckError(
                "directive_ack_no_match",
                f"--ack-directive {needle!r} matched no active Directive - quote a substring "
                "of a Directive's TEXT; its leading [<ISO>] stamp is CLI metadata, not content, "
                "and is not searched",
            )
        # Ambiguity is about DISTINCT instructions. Two identical Directives (a human writing
        # the same standing order twice) would otherwise match every needle twice and deadlock
        # the deploy lock for the whole fleet with no phrasing able to break the tie; they are
        # indistinguishable by construction, so acknowledging one acknowledges that text and
        # the next identical needle takes the next copy.
        #
        # Distinctness is judged on the SAME surface the needle searched. Judging it on the raw
        # bullet made the minted stamp the tie-breaker: two identical standing orders written a
        # minute apart differ only in metadata, so they read as distinct, no substring of their
        # (identical) text can separate them, and the wedge this branch exists to prevent
        # happened anyway — invisibly, since two written inside one minute collide and look fine.
        #
        # It is judged on `hits` — the WHOLE active board — and not on what is still unacked;
        # see the docstring. It is also tested BEFORE the duplicate branch, so that an ambiguous
        # needle reports ambiguity from any position: tested after, the same needle would report
        # `directive_ack_ambiguous` when written first and `directive_ack_duplicate` when
        # written last, which is the order-dependence of gh#128 surviving in the refusal key.
        if len({directive_match_text(active[i]) for i in hits}) > 1:
            quoted = "; ".join(repr(active[i]) for i in hits)
            raise DirectiveAckError(
                "directive_ack_ambiguous",
                f"--ack-directive {needle!r} matched {len(hits)} distinct active "
                f"Directives: {quoted} - narrow it until it names one; acknowledging them in a "
                "different order will not help, and would acknowledge two having read one",
            )
        # Consumption still walks what is unacked: past this point every hit carries the SAME
        # text, so which copy is taken cannot change any verdict, and an ack with nothing left
        # to consume is a repeat rather than a second reading.
        fresh = [i for i in hits if i not in acked]
        if not fresh:
            raise DirectiveAckError(
                "directive_ack_duplicate",
                f"--ack-directive {needle!r} only matches Directives already acknowledged "
                "by an earlier --ack-directive on this command",
            )
        acked.add(fresh[0])
    missing = [d for i, d in enumerate(active) if i not in acked]
    if missing:
        quoted = "\n".join(f"    - {d!r}" for d in missing)
        raise DirectiveAckError(
            "directive_ack_required",
            f"{len(missing)} active Directive(s) unacknowledged - pass "
            f'--ack-directive "<substring>" once for each:\n{quoted}',
        )


def _deploy_hold_stale(reg, holder: str) -> tuple[bool, float, float]:
    """The one stealability rule — shared by `take` and `assert` so they cannot disagree.

    Stale iff the hold is >45 min old AND the holder's Agents heartbeat is >45 min old.
    `take` reads it as "I may BREAK this hold"; `assert` reads it as "this holder has no
    exclusivity left to promise". Those are the same fact, and two copies of the arithmetic
    is exactly how a guard starts green-lighting a merge window a second lane is
    simultaneously entitled to take — the incident again, displaced from "no guard" onto
    "a guard that says yes to both". Returns (stale, held_age, holder_hb_age); either age
    is +inf when its stamp is missing or unreadable, which is what makes an unreadable
    stamp fall on the STEALABLE side for both verbs at once.
    """
    # Passing this 45-minute test only makes `--break-stale` AVAILABLE (Q-10082, gh#504): two
    # stale stamps are what a long review looks like too. The loop skills' full liveness probe
    # is the precondition for a lane actually passing the flag.
    taken = _parse_iso(reg.deploy_taken_at)
    held_age = (datetime.now(UTC) - taken).total_seconds() if taken else float("inf")
    hb = reg.agents.get(holder)
    hb_dt = _parse_iso(hb.heartbeat) if hb else None
    hb_age = (datetime.now(UTC) - hb_dt).total_seconds() if hb_dt else float("inf")
    return (held_age > DEPLOY_LOCK_STALE and hb_age > DEPLOY_LOCK_STALE), held_age, hb_age


def _cmd_deploy_take(text: str, a) -> str:
    """Take the lock. Breaking ANOTHER lane's stale hold requires `--break-stale` (gh#232).

    The break used to be automatic and undeclinable: a stale hold was broken by the next
    `take` that arrived, and the module appended `{actor} -> fleet: broke stale deploy lock`
    in that actor's name. On 2026-09-20 jr-dev-1 ran its skill's liveness probe, concluded
    the holder was NOT dead, deliberately did not break the lock, waited and short-woke — and
    the retry broke the hold anyway and published a fleet note asserting, in jr-dev-1's name,
    the staleness judgement jr-dev-1 had explicitly declined to make. A note is a claim about
    what a lane decided, so the decision has to be something the lane can make.

    `--break-stale` is that decision, and it is the only thing that changed here: the
    predicate (`_deploy_hold_stale`, 45 min + dead heartbeat) is untouched, and so is the
    note's wording. A stale hold without the flag is now refused `deploy_stale_unbroken` —
    a DIFFERENT key from `deploy_held` on purpose, because a lane's recovery for the two is
    opposite (wait vs. probe the holder and decide), and the file is left byte-identical so
    the refusal costs nothing to hit.
    """
    _require_agent(a.actor)
    _require_capability("deploy-lock take", a.actor)
    reg = parse_registry(text)
    # Before ANY edit: an unacknowledged standing instruction leaves the file byte-identical.
    _require_directive_acks(reg, getattr(a, "ack_directive", None))
    holder = reg.deploy_holder
    # Re-taking your OWN hold is not a break: it refreshes the stamp, needs no flag, and must
    # never write a note accusing yourself of holding a dead lock. Passing --break-stale on
    # that path is a no-op rather than an error — the flag says "break another lane's hold if
    # it is stale", and there is no other lane's hold here to break.
    if holder not in ("none", "-", "") and holder != a.actor:
        # Held by someone else. Stealable only if >45 min AND their Agents heartbeat is dead.
        stale, held_age, hb_age = _deploy_hold_stale(reg, holder)
        if not stale:
            raise RegistryError(
                "deploy_held",
                f"deploy lock held by {holder} (held {held_age:.0f}s, holder hb age {hb_age:.0f}s)",
            )
        if not getattr(a, "break_stale", False):
            raise RegistryError(
                "deploy_stale_unbroken",
                f"deploy lock held by {holder} is STALE and STEALABLE (held "
                f"{_deploy_age(held_age)}, holder hb age {_deploy_age(hb_age)}) - nothing was "
                f"changed. This is not the wait-for-the-holder case. Probe "
                f"{holder} yourself: only if the full liveness probe finds it dead, re-run this exact "
                "command with --break-stale, which breaks the hold and routes the fleet note in your name",
            )
        text = _append_note(text, f"{a.actor} -> fleet: broke stale deploy lock (prev {holder}, {held_age:.0f}s)")
    return _set_deploy_line(text, a.actor, _now_iso())


def _cmd_deploy_release(text: str, a) -> str:
    _require_agent(a.actor)
    _require_deploy_line(text)
    reg = parse_registry(text)
    if reg.deploy_holder != a.actor:
        raise RegistryError("deploy_holder", f"deploy lock held by {reg.deploy_holder!r}, not {a.actor}")
    return _set_deploy_line(text, "none", "-")


# --------------------------------------------------------------------------- #
# init (create-if-missing) + read (lock-free)
# --------------------------------------------------------------------------- #
def _template_text(project: str, lock_name: Optional[str] = None) -> str:
    tpl = _find_template()
    text = tpl.read_text(encoding="utf-8")
    if lock_name is not None:
        # The header's lock comment must name the lock every later write takes, which follows
        # the board, not init's --project; stamp it before the generic <project> fill.
        text = text.replace("<project>-registry.lock", f"{lock_name}-registry.lock")
    return text.replace("<project>", project)


def _find_template() -> Path:
    # <repo>/skills/_shared/agentflow/registry.py -> <repo>/templates/registry.md
    here = Path(__file__).resolve()
    candidate = here.parents[3] / "templates" / "registry.md"
    if candidate.exists():
        return candidate
    raise RegistryError("file_missing", f"templates/registry.md not found near {candidate}")


def _do_init(registry_path: Path, project: str, lock_name: str) -> int:
    """Create-if-missing. Absence is the NORMAL input, so this cannot use the read guard.

    `_require_readable_registry` raises `file_missing` on absence, which is precisely the
    case `init` exists to handle — hence the inline `is_file()` rather than a call to it.

    The `is_file()` is not a stylistic variant of `exists()`: `exists()` is True for a
    DIRECTORY, so a mis-pointed `--file` (or an inherited $AGENTFLOW_REGISTRY) used to be
    answered "registry already present (not clobbered)" on exit 0 — while all 23 other
    verbs refused the same path with `file_unreadable`. That is gh#134's own defect class
    (a verb asserting something false about the file) surviving in the one verb the old
    `file_missing` advice sent people to. A directory is not a registry and never will be,
    so it refuses on 2 with the same key every other verb uses.

    What this verb does NOT do is decide whether a present regular file is a VALID
    registry: it never reads it. `init` therefore claims only what it checked — that a
    file is at the path and was left alone — and points at `validate` for the rest. An
    undecodable regular file is genuinely present, so declining to clobber it is right;
    only the old wording, which called those bytes a registry, was wrong.
    """
    if registry_path.exists():
        if not registry_path.is_file():
            raise RegistryError(
                "file_unreadable", "the path is not a regular file (a directory, device, or socket)"
            )
        print(
            f"not clobbered — a file is already present at {registry_path}; "
            f"init only creates, so run `registry validate` to check it"
        )
        return 0
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(registry_path, _template_text(project, lock_name))
    print(f"initialized registry: {registry_path}")
    return 0


def _do_capabilities(a) -> int:
    """`read --capabilities` — the map, or the map applied to `--scope-item` texts.

    Dispatched from `main()` BEFORE `_resolve_registry_path`, not from inside `_do_read`, and
    that placement is the whole point rather than a detail. The capability map is a source-code
    constant, not board state, and the seat that most needs it -- dev-manager at promotion
    (SKILL.md §7c) -- is reading a SPEC, not a registry. This first shipped one layer too deep:
    the early return skipped the file-EXISTENCE check but `main()` had already resolved the
    PATH, so the documented invocation exited 2 with `file_missing`. A refusal reads as "not
    applicable on this pass", which is exactly the silent skip the check exists to prevent.
    """
    items = list(getattr(a, "scope_item", None) or [])
    for line in (scope_item_report(items) if items else capability_report()):
        print(line)
    return 0


def _do_read(registry_path: Path, a) -> int:
    text = _read_registry_text(registry_path)
    reg = parse_registry(text)
    if a.answered_for:
        for qid, q in reg.questions.items():
            if q.fields.get("raised_by") == a.answered_for and q.fields.get("state") == "answered":
                print(f"{qid}: {q.fields.get('answer')}  (work={q.fields.get('work')})")
        return 0
    if a.claimable:
        lane = LANE_OF.get(a.claimable)
        if lane is None:
            raise RegistryError("queue_lane_discipline", f"{a.claimable} owns no lane")
        for rid, row in reg.queue.items():
            if row.lane == lane and row.state == "ready" and row.claimed_by in ("-", ""):
                print(f"{rid}: {row.work_item}")
        return 0
    # THE READ SELECTORS ARE AN IF-CHAIN OF EARLY RETURNS, so two of them silently ran the
    # first and exited 0 -- an operator asking two questions got one answer and no indication
    # which. Same shape for a tuning flag aimed at the wrong read: `--stranded --threshold-hours 1`
    # ran the strand alarm at the 4h default and said nothing. Both are refused, because a
    # SILENT wrong answer from an alarm is the failure this whole epic is about.
    _selected = [
        name for name, on in (
            ("--answered-for", bool(getattr(a, "answered_for", None))),
            ("--claimable", bool(getattr(a, "claimable", None))),
            ("--notes-for", bool(getattr(a, "notes_for", None))),
            ("--directives", bool(a.directives)),
            ("--open-questions", bool(getattr(a, "open_questions", False))),
            ("--missed-wakes", bool(getattr(a, "missed_wakes", False))),
            ("--stranded", bool(getattr(a, "stranded", False))),
            ("--stale", bool(getattr(a, "stale", False))),
        ) if on
    ]
    if len(_selected) > 1:
        raise RegistryError(
            "read_one_selector",
            f"read takes ONE selector; got {' '.join(_selected)}. They are separate reports with "
            "separate thresholds, and running them together would print one and silently drop the "
            "rest. Run them as separate commands.",
        )
    for _flag, _value, _needs in (
        ("--threshold-hours", getattr(a, "threshold_hours", None), "--stale"),
        ("--strand-hours", getattr(a, "strand_hours", None), "--stranded"),
        ("--grace-minutes", getattr(a, "grace_minutes", None), "--missed-wakes"),
    ):
        if _value is not None and _selected != [_needs]:
            raise RegistryError(
                "read_one_selector",
                f"{_flag} tunes {_needs} and nothing else; it was passed with "
                f"{' '.join(_selected) or 'no selector'}. A threshold that silently applies to "
                "no read is a number an operator believes they set.",
            )
    if getattr(a, "notes_for", None):
        # The phase-boundary re-read (gh#159): a Note that unblocks work is as time-sensitive as
        # a Directive, and without this verb it waited a full wake. READ-ONLY and lock-free.
        #
        # THE ACTOR IS VALIDATED. `--answered-for` is not, and there a typo prints nothing; here
        # nothing is followed by an all-clear, so an unvalidated `--notes-for lead_dev` would
        # print a confident "no notes" over a board full of them.
        _require_agent(a.notes_for, "--notes-for")
        # `-> fleet` NOTES ARE NOT SHOWN, deliberately. A fleet Note is a broadcast: no single
        # reader owns striking it, because a strike hides it from every lane, so in practice
        # nobody does -- the first live run of this verb printed 125 lines for one lane, nearly
        # all fleet broadcasts going back weeks. A boundary re-read that dumps that on every
        # phase transition gets skimmed, which is the failure this verb exists to end, and its
        # "act on and strike" instruction would have lanes striking broadcasts out from under
        # each other. Fleet Notes stay with the wake read. (`_notes_routed_to` excludes fleet
        # too.) A line with no routing prefix cannot be attributed to anyone, so it is not
        # shown; an unparseable STAMP does not hide a routed Note, for the reason
        # `_notes_routed_to` gives.
        live = [c for c in reg.notes if not _is_struck_note(c)]
        routed = []
        for content in live:
            m = _NOTE_PREFIX_RE.match(content.strip())
            if m and m.group(3) == a.notes_for:
                routed.append(content.strip())
        for line in routed:
            print(line)
        if not routed:
            # An explicit all-clear, as `--stranded` prints: a silent pass is indistinguishable
            # from a re-read that never ran.
            print(
                f"# no live notes routed to {a.notes_for} (fleet broadcasts are read at wake); "
                f"{len(live)} live note(s) checked"
            )
        return 0
    if a.directives:
        for d in active_directives(reg):
            print(f"- {d}")
        return 0
    if getattr(a, "open_questions", False):
        for q in order_open_questions(reg.questions):
            f = q.fields
            print(
                f"{q.id} [{f.get('urgency', '-')}] raised_by={f.get('raised_by', '-')} "
                f"raised_at={f.get('raised_at', '-')} work={f.get('work', '-')}: "
                f"{f.get('question', '-')}"
            )
        return 0
    if getattr(a, "missed_wakes", False):
        # READ-ONLY, exactly like --stale: prints and returns. The LOCKED decision on this
        # epic is NTFY ONLY -- no relaunch, ever, and that half survived the 2026-09-22
        # ruling untouched. What was revoked that day is the DAEMON-FREE invariant: the rule
        # is now SINGLE DAEMON ONLY ("revoke the rule. Single daemon only is the new
        # rule until it is challenged"), and the one permitted daemon is the OS-scheduled,
        # report-only invoker of `python -m agentflow.wake_watch`. That budget is spent. So
        # the revocation moved WHO MAY INVOKE the detector and not WHAT IT MAY DO: notify
        # costs one push; relaunch costs the single-writer rule, because relaunching on a
        # false positive starts a second copy of a live lane. Emitting the report and letting
        # dev-manager push it keeps that property in the code, not just in the prose.
        # `is None`, NOT `or`: 0.0 is a legitimate grace and it is falsy. `--grace-minutes 0`
        # asks for "report a wake the moment its eta passes", and the `or` silently rewrote that
        # to the 30-minute default -- so this reporter and `agentflow.wake_watch` (which has
        # always tested `is None`) gave DIFFERENT ANSWERS about the same row on the same board,
        # which is the one thing the shared-detector design exists to make impossible.
        _grace_arg = getattr(a, "grace_minutes", None)
        grace = MISSED_WAKE_GRACE_MIN if _grace_arg is None else _grace_arg
        _now = datetime.now(UTC)
        rows = missed_wakes(reg, _now, grace)
        # THE THIRD CLASS ON THE SAME SURFACE (gh#281). Not a second `read` verb: a verb nobody
        # runs is the hole it was meant to fill, one layer out. Not folded into `rows` either --
        # "the wake did not happen" and "the wake happened and the lane never re-declared" take
        # different remedies, and one count would erase the only thing telling the reader which.
        # Its grace is LAPSED_ETA_GRACE_MIN and NOT `grace`; see that constant.
        lapsed = lapsed_etas(reg, _now, LAPSED_ETA_GRACE_MIN)
        blind = blind_rows(reg)
        for m in rows:
            print(missed_wake_line(m, grace))
        for le in lapsed:
            print(lapsed_eta_line(le, LAPSED_ETA_GRACE_MIN))
        # ALWAYS printed when non-empty, alongside the alarm rows rather than instead of them.
        # A blind row is not a missed wake, but it is the alarm telling you what it could not
        # judge -- and reporting it only in the quiet case would hide it exactly when the
        # board is busiest.
        for b in blind:
            print(blind_row_line(b))
        if not rows:
            # The all-clear is QUALIFIED by what could not be checked. An unqualified
            # "no missed wakes" over a partially-readable board is the false green this
            # whole change exists to remove.
            # COUNT ONLY WHAT WAS ACTUALLY JUDGED -- see `judged_row_count`, which is shared
            # with the outside watcher so the two reporters cannot qualify their all-clears
            # differently.
            judged = judged_row_count(reg)
            if blind:
                print(
                    f"# no missed wakes among the {judged} checked row(s) (grace {grace:g}m) -- "
                    f"but {len(blind)} row(s) above CANNOT be checked"
                )
            else:
                print(f"# no missed wakes (grace {grace:g}m); {judged} row(s) checked")
        if not lapsed:
            # ITS OWN ALL-CLEAR, because a check that prints nothing when it does not fire is
            # indistinguishable from one that never ran -- which is the defect this whole family
            # of reports was written against, and gh#281 is that defect one field over.
            #
            # `judged_row_count` is the HONEST count here rather than a borrowed one: its
            # predicate (not halted, not reaped or stood down, eta parses, heartbeat parses) is
            # exactly the set `lapsed_etas` walks past its own skips. A test pins that equality
            # so the day one moves, this number stops being a claim about the other.
            judged = judged_row_count(reg)
            if blind:
                print(
                    f"# no lapsed etas among the {judged} checked row(s) "
                    f"(grace {LAPSED_ETA_GRACE_MIN:g}m) -- but {len(blind)} row(s) above "
                    "CANNOT be checked"
                )
            else:
                print(
                    f"# no lapsed etas (grace {LAPSED_ETA_GRACE_MIN:g}m); "
                    f"{judged} row(s) checked"
                )
        return 0
    if getattr(a, "stranded", False):
        # READ-ONLY, exactly like --stale and --missed-wakes: prints and returns. Notification
        # is the caller's, and the LOCKED no-relaunch rule that governs the missed-wake alarm
        # governs this one for the same reason.
        threshold = getattr(a, "strand_hours", None) or STRAND_THRESHOLD_HOURS
        now = datetime.now(UTC)
        rows = stranded_halts(reg, now, threshold)
        unjudgeable = unjudgeable_halts(reg, now)
        for s in rows:
            print(
                f"{s.agent} STRANDED HALT on {s.question} (waiting {s.age_hours:.1f}h > "
                f"{threshold:g}h) raised_at={s.raised_at} declared-urgency={s.urgency}"
            )
        # ALWAYS printed when non-empty, beside the alarm rows rather than instead of them: a
        # halted row this check cannot time is not a halted row it cleared, and reporting it
        # only in the quiet case would hide it exactly when the board is busiest.
        #
        # The REASON carries the consequence, from UNJUDGEABLE_REASONS. A fixed trailing
        # sentence used to say "this row can never be answered as it stands" for every reason,
        # which is false for a live blocker -- it has an owner and a verb that resolves it.
        for u in unjudgeable:
            print(
                f"{u.agent} HALTED AND UNJUDGEABLE ({u.reason}) waiting_on={u.waiting_on!r} "
                f"- {UNJUDGEABLE_REASONS.get(u.reason, 'unclassified')}"
            )
        if not rows:
            # THE ALL-CLEAR IS THE POINT OF THIS EPIC'S SECOND HALF, not a courtesy. A check
            # that prints nothing when it does not fire is indistinguishable from a check that
            # never ran, and the two were indistinguishable for every wake this alarm existed
            # as prose. Naming the count judged is what makes the silence legible.
            #
            # COUNTED WITH THE CHECK'S OWN PREDICATE (`_is_alarmable_halt`), never a second
            # spelling of it: a stood-down halted row is excluded by the check, so counting it
            # here would report one judged row where zero were looked at.
            halted = sum(1 for r in reg.agents.values() if _is_alarmable_halt(r))
            judged = halted - len(unjudgeable)
            if unjudgeable:
                print(
                    f"# no stranded halts among the {judged} judged row(s) (threshold "
                    f"{threshold:g}h) -- but {len(unjudgeable)} halted row(s) above CANNOT be judged"
                )
            else:
                print(
                    f"# no stranded halts (threshold {threshold:g}h); "
                    f"{judged} halted row(s) checked"
                )
        return 0
    if getattr(a, "stale", False):
        # READ-ONLY by construction: this branch prints and returns. It never writes a
        # Note, never reaps, never edits a row — reaping stays the steward's `reap`.
        threshold = getattr(a, "threshold_hours", None) or DEFAULT_STALE_HOURS
        stale_now = datetime.now(UTC)
        rows = stale_rows(reg, stale_now, threshold)
        # Never "(lapsed)": LAPSED ETA is a --missed-wakes class with a narrower meaning, and
        # reusing the word made two correct outputs read as a contradiction (gh#494). A row here
        # has a past eta, none at all (halted rows are eta-exempt), or one beyond the horizon
        # from its heartbeat (gh#707), so say which.
        for s in rows:
            s_eta = _parse_iso(s.eta)
            if s_eta is None:
                eta_note = "eta absent"
            elif s_eta > stale_now:
                eta_note = f"eta beyond {ETA_HORIZON_HOURS:g}h horizon from heartbeat"
            else:
                eta_note = "eta in past"
            label = s.mark
            tail = ""
            if s.mark == STALE_STOOD_DOWN:
                # The reason is the one thing that tells dev-manager why the lane stopped, so
                # it rides in the label; the tail says what to do instead of ping-then-reap.
                reason = s.row.phase.strip()[len(STOOD_DOWN_PHASE):].lstrip(": ").strip()
                label = f"{STALE_STOOD_DOWN}: {reason or '-'}"
                tail = " -- escalate to dev-manager; never ping, never reap"
            print(
                f"{s.agent} {label} heartbeat={s.heartbeat} "
                f"(age {s.heartbeat_age_hours:.1f}h > {threshold}h) eta={s.eta} ({eta_note}){tail}"
            )
        if not rows:
            # Same rule, applied to the read that was already here: this branch printed NOTHING
            # when it did not fire, so its all-clear and its never-having-run were one byte
            # apart -- the exact defect this epic names, in the file it names.
            #
            # COUNTED WITH `stale_rows`' OWN PREDICATE, WHICH CHANGED UNDER THIS COMMENT ONCE
            # ALREADY. A first draft borrowed the missed-wake branch's count, which excludes
            # reaped AND stood-down phases, and the note here said `stale_rows` "deliberately
            # does not read phase" -- true when written. gh#313 then gave it one phase skip
            # (REAPED only), and this count kept the old spelling for a commit, over-reporting
            # coverage by one per reaped row. Two spellings of one predicate diverge on the day
            # the exclusion set changes, which is exactly what this comment's own closing line
            # warns about: a number describing a different predicate than the check it sits
            # under is the same false green in a smaller font. Mirror the skip, both of them.
            judged = sum(
                1 for r in reg.agents.values()
                if not is_reaped_phase(r.phase) and _parse_iso(r.heartbeat) is not None
            )
            print(f"# no stale rows (threshold {threshold:g}h); {judged} row(s) checked")
        return 0
    sys.stdout.write(text)
    return 0


PREFLIGHT_ACTIONS = ("merge", "publish", "delete", "force-push")


def _do_preflight(registry_path: Path, a) -> int:
    """Surface every ACTIVE Directive before a consequential action. Read-only, lock-free.

    Lock-free on purpose: a check that can block behind another loop's write is a check
    agents learn to skip. It reports; the deploy-take guard is what actually refuses.
    """
    _require_agent(a.actor)
    reg = parse_registry(_read_registry_text(registry_path))
    active = active_directives(reg)
    print(f"preflight: actor={a.actor} action={a.action} active-directives={len(active)}")
    for d in active:
        print(f"- {d}")
    return 0


def _require_readable_registry(registry_path: Path) -> None:
    """The path half of the read guard, split out so a MUTATION can refuse pre-lock.

    A mutation must not acquire the edit-lock — and so must not make every other loop
    queue behind it — only to discover under that lock that `--file` names a directory.
    Same two refusals as the read, decided from the path alone.
    """
    if not registry_path.exists():
        raise RegistryError("file_missing", str(registry_path))
    if not registry_path.is_file():
        # Named separately from the decode failure so the message is true on every
        # platform rather than describing whichever OSError this one happened to raise.
        raise RegistryError("file_unreadable", "the path is not a regular file (a directory, device, or socket)")


def _read_registry_text(registry_path: Path) -> str:
    """THE registry read. Every verb goes through here; nothing calls `read_text` directly.

    Turns each of the three ways a path can fail to be a readable registry into a distinct
    documented refusal (exit 2), because they have three different recoveries:

    - absent            -> `file_missing`    -> `registry init` is the right answer
    - present, undecodable, or not a regular file -> `file_unreadable` -> fix the pointer
                           or restore the file; `init` will not help and cannot run
    - decoded but missing a required section -> `file_damaged`, raised downstream by the
                           parser (see `_get_body`), NOT here

    Absent this routing (gh#134) an unreadable registry escaped as an uncaught
    UnicodeDecodeError/OSError on exit 1 — a code the exit-code table does not admit
    exists — and a DIRECTORY passed to `--file` escaped as a PermissionError, which
    `main()` catches as an exhausted write retry and reports as "registry write did NOT
    land ... re-run this command": a WRITE-failure message out of a READ-ONLY verb that
    wrote nothing, advising a retry that can only fail again forever.

    The existence check lives HERE rather than at each call site so that "is this a
    registry I can read?" has exactly one answer in this module. Note `Path.exists()` is
    True for a directory, hence the explicit `is_file()`: on Windows the directory read
    raises PermissionError and on POSIX IsADirectoryError, and neither should depend on
    which OS is asking.
    """
    _require_readable_registry(registry_path)
    try:
        return registry_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # The path itself stays out of the message: refusals name rules, never machine paths.
        raise RegistryError("file_unreadable", f"{type(exc).__name__} reading the registry") from exc


def read_registry_checked(
    registry_path: Path, tables: tuple = ("Agents", "Queue"), rows: tuple = (),
) -> Registry:
    """Parse the registry at ``registry_path``, refusing when any of ``tables`` lost its table.

    For readers outside this module that draw a conclusion from what a table does NOT contain.
    ``parse_registry`` alone reads a section whose table is gone as zero rows, which is what an
    idle board looks like, so a damaged board would pass as a free one. Raises
    ``RegistryError`` (``file_missing``, ``file_unreadable`` or ``file_damaged``).

    ``rows`` names tables whose raw rows must also pass validate's row-shape checks
    (``_table_row_problems``): the parser silently drops a row with the wrong cell count and
    keeps only the last of a duplicated key, so a reader of what a table does NOT contain
    would see a lost row as absent work. Any finding raises ``file_damaged``.
    """
    text = _read_registry_text(Path(registry_path))
    for section in tables:
        _require_intact_table(text, section)
    problems = [
        p for section in rows for p in _table_row_problems(_get_body(text, section), section)
    ]
    if problems:
        raise RegistryError("file_damaged", "; ".join(problems))
    return parse_registry(text)


def _deploy_age(seconds: float) -> str:
    """Render an age for a human deciding whether to wait or to break the lock."""
    return "never stamped" if seconds == float("inf") else f"{seconds:.0f}s"


def _do_deploy_assert(registry_path: Path, a) -> int:
    """Exit 0 iff the actor holds the deploy lock AND that hold is not stale; else 5.

    Read-only, lock-free. "Holds it" has to mean "owns the merge window", not "is named in
    the cell": those two come apart the instant the hold goes stale, because `take`'s own
    rule then hands the same window to the next builder that wakes.


    The guard a merge is chained BEHIND: `deploy-lock assert ... && gh pr merge ...`.
    Taking the lock and merging were two commands in two tools, so a refused take stopped
    only itself. On 2026-08-31 a take was correctly refused (`deploy_held`, another lane
    was on it) and the `gh pr merge` on the next line ran anyway and landed on master.
    The `&& echo "lock taken"` that trailed the take is what made it look guarded: it
    guarded the echo. Prose cannot close that — "remember to chain them" is the same
    unenforceable class as "remember to re-read the Directives", which is why taking the
    lock already carries `--ack-directive`. This is that gate's mirror image: taking was
    gated on having read; merging is now gateable on holding.

    Routed OUTSIDE `_run_mutation` on purpose — it is modelled on `read`/`preflight`/
    `validate`, and takes no mkdir edit-lock, writes no file, appends no note. Two
    reasons, both load-bearing. A guard that can block behind another loop's unrelated
    write (exit 3, or a timeout) is a guard agents learn to drop from the chain — the
    same argument `_do_preflight` already carries. And a guard that writes has mutated
    the very state it was asked to report on, which is worse than having no guard: the
    merge would then be gated on a fact the gate itself created.
    """
    _require_agent(a.actor)
    # Same gate `take` puts on its own actor: the cell only ever names a builder, so a
    # non-builder asking "do I hold it?" is a malformed call (2), not the answer "no" (5).
    # The read verb and the write verb must agree on who can even BE an answer.
    _require_capability("deploy-lock assert", a.actor)
    text = _read_registry_text(registry_path)
    # A damaged lock line reads as holder "none", which would answer FREE and send the lane to
    # a `take` that refuses `file_damaged`. Report the damage here instead (still fails closed).
    _require_deploy_line(text)
    reg = parse_registry(text)
    holder = reg.deploy_holder
    # Name the board the answer came from. --file and $AGENTFLOW_REGISTRY both outrank
    # --project, so a lane that inherited a registry path from an earlier project switch
    # can hold the lock on one board and merge on another with nothing in the transcript
    # to catch it. This is the only line that green-lights a merge; it says which board.
    board = registry_path.parent.parent.name
    if holder == a.actor:
        # Holding a hold ANOTHER lane is already entitled to break is not exclusivity, so
        # this cannot be a bare name comparison: `take` would hand the same window to the
        # next builder that wakes, and both would be told to merge.
        stale, held_age, hb_age = _deploy_hold_stale(reg, holder)
        if stale:
            raise DeployAssertError(
                "deploy_not_holder",
                f"your hold on {board} is STALE (held {_deploy_age(held_age)}, your hb age "
                f"{_deploy_age(hb_age)}) - any builder may break it now; "
                "`deploy-lock take` again before merging",
            )
        print(f"deploy lock held by {a.actor} (taken-at: {reg.deploy_taken_at}) [board: {board}]")
        return 0
    if holder in ("none", "-", ""):
        # Say FREE rather than naming "none" as if it were a lane: the recovery for a free
        # lock (take it, then merge) is not the recovery for a held one (wait, or reap).
        raise DeployAssertError(
            "deploy_not_holder",
            f"deploy lock on {board} is FREE, not held by {a.actor} - take it before merging",
        )
    stale, held_age, hb_age = _deploy_hold_stale(reg, holder)
    # An unreadable OR future-dated taken-at degrades to naming the holder with NO age, and
    # never raises out of here. This verb exists to stop a merge, so it must not die on a
    # malformed cell: a crashing guard fails OPEN for the caller who chained it. And a
    # negative age (clock skew across hosts, or a hand-written cell) is the one case where
    # the number is worse than none — "held -7148s" tells the reader nothing about whether
    # to wait or to break it, which is the whole job of this line.
    held = (
        f"held {held_age:.0f}s"
        if 0 <= held_age < float("inf")
        else f"held-since unreadable (taken-at: {reg.deploy_taken_at!r})"
    )
    # The recovery, not just the verdict: a raw seconds count is only actionable against the
    # stale rule, and the reader cannot apply a rule this message never states.
    recovery = (
        "the hold is STALE - `deploy-lock take --break-stale` breaks it, then re-assert"
        if stale
        else "wait for the release, do not merge"
    )
    raise DeployAssertError(
        "deploy_not_holder",
        f"deploy lock on {board} held by {holder} ({held}, holder hb age "
        f"{_deploy_age(hb_age)}), not {a.actor} - {recovery}",
    )


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #
def _resolve_registry_path(a) -> Path:
    """The board a command reads and writes (REGISTRY.md #Board resolution).

    ``--file`` is the explicit override and wins outright: it is on the command line, so it can
    never be the invisible cause of a write landing elsewhere. ``--project`` is resolved through
    ``config.load_project`` (gh#715) -- a typo is a config error, never a board to ``init``.
    ``$AGENTFLOW_REGISTRY`` is the one INVISIBLE input, so when ``--project`` is also given the
    two must name the same board or the command refuses, naming both; alone it keeps working.
    """
    project = getattr(a, "project", None)
    _require_project_slug(project)
    if getattr(a, "file", None):
        return Path(a.file)
    env = os.environ.get("AGENTFLOW_REGISTRY")
    if not project:
        if env:
            return Path(env)
        raise RegistryError("file_missing", "no --file, no $AGENTFLOW_REGISTRY, and no --project to derive a path")
    derived = _project_board(project)
    if env and Path(env).resolve() != derived.resolve():
        raise RegistryError(
            "registry_board_conflict",
            f"$AGENTFLOW_REGISTRY={env} but --project {project} names {derived}; "
            "nothing was read or written",
        )
    return derived


def _project_board(project: str) -> Path:
    """``<data_root>/projects/<project>/status/registry.md`` for a CONFIGURED project only."""
    from . import config  # lazy: tests drive via --file and never touch config

    try:
        config.load_project(project)
    except config.ConfigError as e:
        raise RegistryError("project_unconfigured", f"--project {project}: {e}") from e
    env_cfg = config.load_environment()
    return Path(env_cfg.data_root) / "projects" / project / "status" / "registry.md"


def _resolve_lock_dir(a) -> Path:
    if getattr(a, "lock_dir", None):
        return Path(a.lock_dir)
    from . import config

    return Path(config.load_environment().lock_dir)


def _resolve_project(a, registry_path: Path) -> str:
    if getattr(a, "project", None):
        return a.project
    # <data_root>/projects/<name>/status/registry.md  ->  <name>
    return registry_path.parent.parent.name


def _edit_lock_name(registry_path: Path) -> str:
    """The edit-lock name: the project directory that owns the board, read from its RESOLVED path.

    The lock must be one per BOARD, so it cannot come from how the caller spelled the path: a
    junction, a relative ``registry.md`` or a ``--project`` that disagrees with ``--file`` would
    each take a different lock for the same file, and two writers would both get in. For every
    ``--project`` call the board is ``<data_root>/projects/<name>/status/registry.md``, so this
    is still ``<name>`` there. An empty name is refused: a board whose resolved path has no
    directory two levels above it (at a filesystem root or one level under it) would otherwise
    share the one ``-registry.lock`` with every other such board.
    """
    resolved = registry_path.resolve()
    name = resolved.parent.parent.name
    if not name:
        raise RegistryError(
            "lock_name_empty",
            f"{registry_path} resolves to {resolved}, which has no project directory above its "
            "parent to name the edit-lock; nothing was locked, read or written",
        )
    return name


# --------------------------------------------------------------------------- #
# Coordination Notes — the one fleet-wide, non-owned surface (append / strike)
# --------------------------------------------------------------------------- #
def _note_argv_checks(a) -> Optional[str]:
    """`note`'s refusals that argv alone decides, returning the normalized text (None for a
    strike); run PRE-LOCK too (gh#656, `_ARGV_CHECKS`)."""
    _require_agent(a.actor)
    if getattr(a, "strike", False):
        if not a.match:
            raise RegistryError("note_missing", "--strike requires --match <substring>")
        if a.text is not None:
            raise RegistryError(
                "note_strike_text",
                "--strike consumes an existing Note and writes no text, so --text would be "
                "dropped unread; strike with --match alone, then append the reply as its own note",
            )
        return None
    if not a.text or not a.text.strip():
        raise RegistryError("note_missing", "note requires --text (or --strike --match)")
    to = a.to or "fleet"
    if to != "fleet":
        _require_agent(to, "--to")
    note_text = _normalize_overflow_pointers(a.text)
    # Append only: a strike is never size-capped (see `_cmd_note`).
    _guard_note_size(note_text)
    return note_text


def _cmd_note(text: str, a) -> str:
    """Append a routed coordination Note, or strike (mark-consumed) one that matches."""
    note_text = _note_argv_checks(a)
    if getattr(a, "strike", False):
        return _strike_note(text, a.match, a.actor)
    to = a.to or "fleet"
    # --strike returned above and is not burst-guarded: consuming a note is how the surface
    # DRAINS, and a full route must never block the read that empties it.
    _guard_note_burst(text, a.actor, to, note_text)
    return _append_note(text, f"{a.actor} -> {to}: {note_text}")


def _strike_note(text: str, match: str, actor: str) -> str:
    """Strike the ONE live Note matching ``match`` that ``actor`` is entitled to consume.

    Exactly one, or a refusal (gh#650). Taking the first match struck whichever candidate was
    newest, so the Note the lane actually handled could stay live while another one -- never
    read -- was marked consumed and then deleted by the next cull. Candidates are the lines the
    actor MAY strike: a match on another lane's Note could never be the one meant, so it does
    not make the match ambiguous. Byte-identical candidates are the exception: no substring can
    ever separate them, and striking either consumes the same instruction, so the first is
    struck rather than leaving both unstrikeable forever.

    Two rules, both load-bearing now that `cull` deletes a struck Note permanently.

    Liveness is the strict full-line wrapper test (`_is_struck_note`), NOT ``"~~" in content``.
    The loose test made any note whose BODY contains ``~~`` ("move off ~~old~~ path") immortal:
    unstrikeable forever, re-read as new at every wake, and reported by the CLI as not
    existing at all. The wrapper test is also exactly what the cull sweep looks for, so what
    can be consumed and what can be collected stay the same set of lines by construction.

    Authorization is the routed recipient — ``## Ownership`` says a loop strikes what is routed
    TO it. Unenforced, that was prose: any lane could strike any note, and since striking is
    the ONLY evidence a note was read, a mis-aimed strike now hands a live instruction to
    `cull`, which deletes it before the addressee ever sees it. A note routed to ``fleet`` is
    addressed to everyone, so anyone may strike it; dev-manager may strike anything, because it
    is the janitor that owns reap/repair/cull and is the only remedy for a note routed to a
    lane that has since died. A note with NO parseable routing prefix cannot be attributed, so
    it stays strikeable by anyone — refusing those would strand every pre-convention line.
    """
    body = _get_body(text, "Queue")
    lines = body.split("\n")
    forbidden: list[str] = []
    candidates: list[tuple[int, str, str]] = []  # (line index, indent, content)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("- ") or _is_notes_header(line) or _is_deploy_lock_field(line):
            continue
        content = stripped[2:]  # drop the "- " bullet marker
        if match not in content or _is_struck_note(content):
            continue
        prefix = _parse_note_prefix(content)
        if (
            prefix
            and actor not in ACTOR_CAPABILITY["note"].janitors
            and prefix.to not in (actor, "fleet")
        ):
            forbidden.append(prefix.to)
            continue
        candidates.append((i, line[: len(line) - len(stripped)], content))
    if len({content for _i, _indent, content in candidates}) > 1:
        preview = "; ".join(content[:80] for _i, _indent, content in candidates[:5])
        raise RegistryError(
            "note_strike_ambiguous",
            f"--match {match!r} matched {len(candidates)} live Notes you may strike, so it names "
            f"none of them: {preview}. Quote a longer substring of the one you acted on -- one "
            "call per Note; when their text is identical, include the `[stamp]` prefix that "
            "tells them apart.",
        )
    if candidates:
        i, indent, content = candidates[0]
        lines[i] = f"{indent}- ~~{content}~~"
        # Printed by the dispatcher after the write lands, like expire-directive's receipt.
        _strike_note.last_struck = content
        return _refresh_notes_header(_replace_body(text, "Queue", "\n".join(lines)))
    if forbidden:
        raise RegistryError(
            "note_not_yours",
            f"the live note matching {match!r} is routed to {forbidden[0]}, not {actor} — "
            "striking it would mark another lane's coordination consumed, and `cull` then "
            "deletes it unread",
        )
    raise RegistryError("note_missing", f"no live note matching {match!r}")


# --------------------------------------------------------------------------- #
# Janitor: the steward's cross-row writes — the only verbs that edit a row the actor does
# not own. `stand-down` (a human stopped this lane) and `reap` (it died).
# --------------------------------------------------------------------------- #
def _cmd_stand_down(text: str, a) -> str:
    """Mark a lane as deliberately stopped, so the missed-wake alarm stops paging for it.

    gh#98: a human clearing a lane produced exactly the signal of that lane dying. The registry
    had a verb for "it died and I returned its claims" (``reap``) and one for "it stopped itself
    on a question" (``status --state halted``), but none for "I stopped it on purpose" -- and the
    human does not write to the registry, dev-manager is the scribe.

    Deliberately NOT ``reap``. Reap means *died, and its claims were returned*; overloading it
    would erase the distinction the report exists to draw, and would return claims from a lane
    that may be resumed with them intact.

    The mark is a PHASE, so it is evidence on the row like the other two exclusions, and it
    CLEARS ON THE LANE'S NEXT FULL ``status`` WRITE -- which is the lane itself saying it is back.
    Nothing has to remember to un-stand-down it.

    ONLY that write clears it, and the narrower claim is the true one: ``claim``, ``release``,
    ``publish``, ``note``, ``deploy-lock`` and ``ask`` never touch the Agents row at all, and
    ``directive`` stamps the heartbeat while preserving ``phase`` verbatim. So a lane that
    resumes by claiming work rather than by declaring itself stays excluded while holding a live
    claim. That is why ``--heartbeat-only`` is REFUSED here rather than merely discouraged: it is
    the one path the 30-minute in-flight ceiling tells every lane to travel, so leaving it open
    would make the gap the common case instead of the corner. What still covers the rest is the
    pair of reports that never read ``phase``: ``read --stale`` flags the frozen heartbeat, and
    the 45-minute deploy-lock stale rule still frees the lock.

    Claims are left ALONE: standing a lane down does not abandon its work, and a lane stopped
    mid-epic should find its row exactly as it left it.
    """
    phase = _stand_down_argv_checks(a)
    target = a.agent
    reg = parse_registry(text)
    # Same refusal reap gives: a missing row is not a write that failed to land (exit 1, retry),
    # it is one that can never land, and the KeyError it used to raise read as the former.
    if target not in reg.agents:
        raise RegistryError("id_exists", f"{target} has no Agents row to stand down")
    row = reg.agents[target]
    # A reaped row is idle and already returned its claims. Standing it down erased the reap
    # mark and sent a lane holding nothing back into `read --stale` as an escalation, which
    # `reap` then refused as stood down.
    if is_reaped_phase(row.phase):
        raise RegistryError(
            "stand_down_reaped",
            f"{target} is already reaped ({row.phase.strip()!r}): it holds no claims and is "
            "excluded from the missed-wake alarm. Nothing to stand down; its next full `status` "
            "write brings it back.",
        )
    # NOT reap: claims and the deploy lock are left exactly as the lane left them, because a
    # lane stopped mid-epic may be resumed with its work intact. But the deploy lock serializes
    # every merge for every builder, so a stood-down holder wedges the fleet silently --
    # say so, and let a human decide between resuming this lane and reaping it. Warning, not
    # action: releasing the lock here could step on a merge that is half-applied.
    if reg.deploy_holder == target:
        print(
            f"warning: {target} still holds the deploy lock, which serializes every merge for "
            f"the whole fleet. Standing it down does NOT release it. Resume this lane, or "
            f"`reap --override-stand-down` it once dev-manager decides it is not coming back "
            f"(adding --force --reason while `read --stale` does not list it yet).",
            file=sys.stderr,
        )
    body = _replace_data_row(
        text, "Agents", 0, target, "agent",
        # heartbeat and eta preserved as-is: they are the record of where the lane was when it
        # was stopped, and the exclusion no longer depends on them being tidy.
        [target, row.status, row.work, phase, row.heartbeat, row.eta, row.waiting_on],
    )
    return _replace_body(text, "Agents", body)


def _stand_down_argv_checks(a) -> str:
    """`stand-down`'s refusals that argv alone decides, returning the phase cell; run PRE-LOCK
    too (gh#656, `_ARGV_CHECKS`)."""
    _require_capability("stand-down", a.actor)
    _require_agent(a.agent, "--agent")
    # reap's two seat refusals, for the same reasons: the actor is plainly running, and the
    # human's seat holds no claims and is never in the missed-wake alarm to begin with.
    if a.agent == a.actor:
        raise RegistryError(
            "stand_down_self",
            f"{a.actor} cannot stand down its own row; it is plainly running",
        )
    if a.agent in ETA_HORIZON_EXEMPT:
        raise RegistryError(
            "stand_down_exempt_seat",
            f"{a.agent} is the human's seat (ETA_HORIZON_EXEMPT): it is never alarmed as a "
            "missed wake, so there is nothing to stand down.",
        )
    reason = (getattr(a, "reason", None) or "").strip()
    if not reason:
        raise RegistryError(
            "stand_down_needs_reason",
            "--reason must carry actual text: a blank reason writes a mark that says a human "
            "stopped this lane and refuses to say why, which is indistinguishable from the dead "
            "lane the alarm exists to catch. (argparse enforces the flag's PRESENCE; this is the "
            "whitespace-only value it lets through.)",
        )
    phase = f"{STOOD_DOWN_PHASE}: {reason}"
    # The COMPOSED cell, not the bare --reason: the marker prefix is what the alarm matches on
    # and it spends bytes too, so bounding the reason alone would let the cell the human reads
    # go over while the check reported it fit. Same cap and same key as `status` -- a second,
    # looser bound on a second write path is how a capped field stops being capped.
    _guard_phase_size(phase, composed=f"'{STOOD_DOWN_PHASE}: ' + this --reason")
    return phase


#: `reap --reason` cap: the reason rides in the reap Note, which must stay under NOTE_MAX_BYTES
#: with room left for the stranded-notes clause (`_render_stranded` assumes 200+ bytes).
REAP_REASON_MAX_BYTES = 100


def _reap_argv_checks(a) -> None:
    """`reap`'s refusals that argv alone decides; run PRE-LOCK too (gh#656, `_ARGV_CHECKS`)."""
    _require_capability("reap", a.actor)
    _require_agent(a.agent, "--agent")
    # gh#575: the steward reaped itself rc 0. A loop that is running cannot be the dead one.
    if a.agent == a.actor:
        raise RegistryError("reap_self", f"{a.actor} cannot reap its own row; it is plainly running")
    reason = (getattr(a, "reason", None) or "").strip()
    if getattr(a, "force", False) and _field_is_absent(reason):
        raise RegistryError(
            "reap_force_needs_reason",
            "--force skips the liveness check, so --reason must say who confirmed the lane is "
            "gone and how; it is written into the reap Note",
        )
    if len(reason.encode("utf-8")) > REAP_REASON_MAX_BYTES:
        raise RegistryError(
            "note_too_long",
            f"reap --reason is capped at {REAP_REASON_MAX_BYTES} UTF-8 bytes so the reap Note "
            f"stays under the {NOTE_MAX_BYTES}-byte Note cap; put detail in a note file",
        )


def _cmd_reap(text: str, a) -> str:
    _reap_argv_checks(a)
    target = a.agent
    reg = parse_registry(text)
    if target not in reg.agents:
        raise RegistryError("id_exists", f"{target} has no Agents row to reap")
    row = reg.agents[target]
    # The human's seat holds no claims or deploy lock, so a reap returns nothing and only
    # rewrites its row as dead. Refused before every override: neither --force nor
    # --override-stand-down changes what a reap of this row could recover.
    if target in ETA_HORIZON_EXEMPT:
        raise RegistryError(
            "reap_exempt_seat",
            f"{target} is the human's seat (ETA_HORIZON_EXEMPT): report it as away past its "
            "declared return, never reap it.",
        )
    # gh#683: stand-down promises the lane finds its row as it left it, and a stood-down lane
    # always fails the life probe because a human stopped it -- so the ping-then-reap procedure
    # would undo a deliberate pause. Refused unless the caller says the decision was made.
    if row.phase.strip().startswith(STOOD_DOWN_PHASE) and not getattr(a, "override_stand_down", False):
        raise RegistryError(
            "reap_stood_down",
            f"{target} is stood down ({row.phase.strip()!r}). Escalate to dev-manager; if the "
            "lane is not coming back, re-run with --override-stand-down to reap it.",
        )
    # gh#575: the verb had no liveness floor -- one mistyped --agent took a live lane's claims
    # and deploy lock and recorded it as '(stale)'. Judged by `stale_rows` itself, the predicate
    # `read --stale` prints from, so the report and the verb can never disagree; resolved through
    # the module at call time rather than bound at import, so there is exactly one copy of it.
    stale = any(s.agent == target for s in stale_rows(reg, datetime.now(UTC)))
    forced = bool(getattr(a, "force", False))
    if not stale and not forced:
        raise RegistryError(
            "reap_not_stale",
            f"{target} is not stale (heartbeat {row.heartbeat}, eta {row.eta}): `read --stale` "
            f"would not list it. Probe the lane; if a human has confirmed it is gone, re-run "
            f"with --force --reason \"<who confirmed, how>\".",
        )
    # 1. Idle the dead loop's row — preserve heartbeat/eta as the stale evidence.
    body = _replace_data_row(
        text, "Agents", 0, target, "agent",
        [target, "idle", "-", REAPED_PHASE if stale else REAPED_FORCED_PHASE,
         row.heartbeat, row.eta, "-"],
    )
    text = _replace_body(text, "Agents", body)
    # 2. Return the dead loop's claimed Queue rows to the pool -- but ONLY the ones that are
    #    still work. `claimed_by` on a TERMINAL row is not a claim, it is PROVENANCE: it records
    #    who shipped (`done`) or retracted (`withdrawn`) the row, and nothing will ever build in
    #    that seam again. Reaping on `claimed_by` alone read that provenance as a claim and
    #    rewrote merged epics back to `ready` -- and a `ready`, unclaimed row in a lane is exactly
    #    what `_cmd_claim` admits and `read --claimable` advertises, so the builder owning that
    #    lane takes it and rebuilds work already on main (gh#153, seen live 2026-08-31 returning
    #    two shipped epics). Nothing in the claim path can tell a resurrected row from unbuilt
    #    work: what it checks is the row's own `state` and empty `claimed_by`, and the reap had
    #    just written both.
    #
    #    A terminal row is therefore left BYTE-IDENTICAL, including `claimed_by`. Clearing it
    #    would be the same defect wearing the other face: the provenance being misread is the
    #    only record of who did the work, and erasing it destroys that instead of respecting it.
    #
    #    Exposure scaled with lane lifetime -- a long-lived seat accumulates `done` rows, and one
    #    reap returned all of them at once -- which is why this is a state filter rather than a
    #    special case for the row that happened to be observed.
    #
    #    THE JUDGEMENT AND THE WRITE LAND ON THE SAME LINE, which is why this walks the raw
    #    `## Queue` lines instead of `reg.queue`. The parsed model collapses a DUPLICATED id to
    #    its LAST row (plain dict assignment), so deciding from the parse would judge one row
    #    while this loop rewrites the other -- a `done` twin judged as the claim, or a `building`
    #    twin stranded because its `done` double was the one read. Reading and writing one line
    #    at a time makes the two agree BY CONSTRUCTION rather than by an invariant.
    #
    #    (The module-wide half of that skew is closed too, gh#196: `_find_data_row` REFUSES a
    #    duplicated key with `id_duplicate` instead of targeting the first match, `validate`
    #    reports the duplicate at wake, and the verbs that judge lane/claimant first ask
    #    `_require_unique_key` before judging. This loop never resolves a Queue row by key, so
    #    on a duplicated id it is the one Queue writer that still proceeds -- per line, which is
    #    exactly the property that makes that safe.)
    returned = 0
    preserved = 0
    unrecognized = 0
    q_lines = _get_body(text, "Queue").split("\n")
    for i, line in enumerate(q_lines):
        cells = _cells(line)
        # The same admission test `parse_registry` applies, so reap sees exactly the rows the
        # board's readers see -- no more (a line too malformed to parse is `repair`'s problem,
        # not reap's) and no fewer (a row reap skipped would keep a dead lane's claim alive with
        # nothing on the board saying so).
        if cells is None or len(cells) < 7 or not _row_key_ok("Queue", cells[0]):
            continue
        if cells[3] != target:
            continue
        if cells[4] in RETURNABLE_QUEUE_STATES:
            # `building`, `parked` and `review` are all in-flight or resumable, and a dead lane
            # must release them. `parked` especially: it becomes claimable the moment its
            # question is answered, so stranding it behind a corpse is the stall reap removes.
            # Cells 7+ are carried through rather than dropped: a wide row is malformed, and
            # truncating one here would make reap a destructive edit on a board it came to fix.
            q_lines[i] = _fmt_row(
                [cells[0], cells[1], cells[2], "-", "ready", "-", cells[6], *cells[7:]]
            )
            returned += 1
        elif cells[4] in TERMINAL_QUEUE_STATES:
            preserved += 1
        else:
            unrecognized += 1
    text = _replace_body(text, "Queue", "\n".join(q_lines))
    # 3. Clear the deploy lock if the dead loop held it.
    cleared = ""
    if reg.deploy_holder == target:
        text = _set_deploy_line(text, "none", "-")
        cleared = ", cleared deploy lock"
    # 4. Record the reap on the shared surface, naming what the reap did NOT repair.
    #
    #    gh#43. The three steps above make the dead lane's row, its claims and the deploy lock
    #    correct, and leave every Note routed to that lane pointing at it. The precise harm is
    #    NOT that those notes are unreadable forever -- these are durable seats, and a relaunched
    #    lane reads its own notes. It is that step 2 has just MOVED THE WORK and step 4 has not
    #    moved the reasoning about it: the claim goes back to the pool for some other lane to
    #    take, while the handover explaining it stays addressed to the lane that died. By the
    #    time the original seat returns, the work it was written about belongs to someone else.
    #
    #    Surfacing, not auto-re-routing: deciding who should now receive a handover is a judgment
    #    about lanes and disjointness, and re-routing by machine would put context in front of
    #    loops with no business acting on it. The reap sweep is not the only code that knows a
    #    lane stopped -- `stand-down`, the missed-wake alarm and `fleet_report.classify_lane` all
    #    read that too -- but it is the only one that ACTS on the death by moving the lane's
    #    claims, so it is the right place to say what the move left behind.
    #    The budget is measured on the note's TEXT -- what follows "<actor> -> <to>: " -- because
    #    that is the string `_guard_note_size` holds every other note to. The routing prefix and
    #    the "  - [stamp] " bullet `_append_note` adds are NOT part of it.
    #
    #    THE COUNT IS PART OF THE FIX. The old line said "returned N claim(s)" while N counted
    #    every row it touched, terminal ones included -- so the note that resurrected shipped work
    #    read as correct on the board, which is why nobody caught it. Returned and preserved are
    #    now separate tallies and each names only what it did.
    #
    #    The optional clauses are omitted when zero, for the reason `_render_stranded` gives for
    #    its own silence: a clause that fires on every reap regardless of whether it has anything
    #    to say is one its reader learns to skip. They are added BEFORE the stranded-note budget
    #    is computed, so the 300-byte Note cap still bounds the whole line.
    #
    #    `skipped` is the loud half of the allowlist. A cell outside the enum means the board is
    #    damaged in a way reap deliberately refuses to guess about -- and a row it declined to
    #    touch is a claim STILL HELD by a lane that is gone, so silence there would leave the reap
    #    reading as complete while the stall it exists to clear is still on the board.
    extra = ""
    if preserved:
        extra += f", preserved {preserved} terminal row(s)"
    if unrecognized:
        extra += f", skipped {unrecognized} unrecognized state(s)"
    # '(stale)' only when the check above found it so; a forced reap says why it was forced.
    reason = (getattr(a, "reason", None) or "").strip()
    why = "stale" if stale else "forced"
    if not _field_is_absent(reason):
        why += f": {reason}" if not stale else f"; {reason}"
    body = f"reaped {target} ({why}) — returned {returned} claim(s){extra}{cleared}"
    body += _render_stranded(
        _notes_routed_to(text, target), NOTE_MAX_BYTES - len(body.encode("utf-8"))
    )
    return _append_note(text, f"steward -> fleet: {body}")


# --------------------------------------------------------------------------- #
# Malformed-row repair — steward only; exact-line replace, then re-parse
# --------------------------------------------------------------------------- #
def _repair_target(section: str, lines: list[str], i: int, old_line: str) -> str:
    """Name the repaired line for the audit note WITHOUT quoting it.

    Derived, never echoed, and that is a correctness rule rather than a style choice — the same
    one the reap line learned the hard way (see _MACHINE_NOTE_MARKERS). `--old-line` is arbitrary
    actor text, a Queue **Notes** bullet is itself a repairable line, and `_append_note` puts this
    audit line ABOVE everything already there. An echoed excerpt would therefore also match the
    `note --strike --match` aimed at the note it quoted: `_strike_note` refuses a match that hits
    more than one distinct live Note (gh#650), so the original could no longer be struck by any
    substring of the excerpt -- and before that refusal it consumed the warning instead. A row
    id + field name identifies the edit for a human reading the log without re-emitting one byte
    of caller text.

    *lines* is the section AFTER the replacement, so ``lines[i]`` is the repaired line; *old_line*
    is only consulted when the replacement itself carries no name (a field line repaired into
    something that no longer parses as one).
    """
    line = lines[i]
    if section == "Questions":
        # Walk back to the owning block: a bare field name means nothing without its question.
        for prev in range(i, -1, -1):
            qm = _QBLOCK_RE.match(lines[prev])
            if qm:
                fm = _QFIELD_RE.match(line) or _QFIELD_RE.match(old_line)
                return f"{qm.group(1)} {fm.group(1)}" if fm else f"{qm.group(1)} line {i + 1}"
    # A blank --new-line deletes the row, so the name comes from the line it replaced. Only a
    # cell matching an id pattern or the agent enum is ever printed, never free caller text.
    for cells in (_cells(line), _cells(old_line)):
        if not cells:
            continue
        if _E_ID.match(cells[0]) or _ANY_ID.match(cells[0]):
            return cells[0]
        if section == "Agents" and cells[0] in AGENTS:
            return cells[0]
    return f"line {i + 1}"


def _decision_record(text: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """qid -> (state, the decision tuple) read from the PARSED document, not from line text.

    This is the backstop `repair` is actually held to, and it exists because two narrower
    versions of the same rule were each demonstrated open at rc=0 with `validate` green:

    * Comparing protected LINES knows nothing about which ``### Q-NNNN`` block owns each line,
      while `_iter_qblocks` decides ownership entirely from those headers. Deleting one header
      merges two blocks, so a real human answer silently lands on a DIFFERENT question -- with
      every protected line still present, in the same order, byte-identical.
    * Comparing anything inside one section trusts `--section`, which is a string the CALLER
      typed. `_section_bounds` maps headings in file order with the LAST one winning, so a
      sequence of `repair --section Blockers` calls can write a whole fabricated block over filler
      rows and crown it with a second ``## Questions`` heading. Every consumer then reads the
      planted section and the in-section guards never execute at all.

    Reading the parsed model instead makes both of those the same check: whatever route the edit
    took, the question the FLEET sees either still says what it said, or the repair is refused.

    `question`/`work` join the tuple only once the question has left `open` -- a decision is the
    (asked, answered) PAIR, so repointing what was asked, or which epic the answer binds to,
    rewrites the decision just as surely as editing the answer text.

    `retraction`/`retracted_at` join it on a RETRACTED row for the same reason: a retraction is
    the other way a question ends, and its reason is the only thing that distinguishes a dead
    premise from an inconvenient question.

    `assumption` (`_Q_RAISER_FIELDS`) is in it in every state (gh#191): it is the raiser's own
    words, and from `open` onward only the raiser may change them.

    Each value is a ``(key, value)`` PAIR, so a reader naming what changed reads the same key
    list that was compared -- a parallel hand-kept name list fell out of step with the retraction
    keys and named nothing.
    """
    out: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {}
    for qid, q in parse_registry(text).questions.items():
        state = q.fields.get("state", "-")
        keys = _Q_PROTECTED_FIELDS + _Q_RAISER_FIELDS
        if state != "open":
            keys = keys + _Q_BINDING_FIELDS
        if state == Q_RETRACTED:
            # The retraction record is the binding half of THIS lifecycle end, exactly as the
            # answer triple is of the other one. Left out, `repair` rewrote a retraction reason
            # at exit 0 with `validate` green - while the identical repair against `answer` was
            # refused, which is what made this an omission rather than a decision.
            keys = keys + _Q_RETRACTION_FIELDS
        out[qid] = (state, tuple((k, q.fields.get(k, "-")) for k in keys))
    return out


def _guard_decision_record(text: str, new_text: str) -> None:
    """Refuse a repair that moved the parsed decision record in any direction.

    Runs on EVERY repair regardless of `--section`: a repair filed under `Blockers` that
    displaces the real `## Questions` removes every existing qid from the map, and the removal
    check below is what catches it.
    """
    before, after = _decision_record(text), _decision_record(new_text)
    for qid, rec in before.items():
        if qid not in after:
            raise RegistryError(
                "repair_decision_record",
                f"{qid} would no longer be readable as a question after this repair. A recorded "
                f"decision cannot be dissolved, renamed or displaced by a repair -- whether by "
                f"deleting its `### {qid}` header (which merges its fields into the block above, "
                f"landing a real answer on a different question) or by planting a second "
                f"`## Questions` heading that shadows the real section.",
            )
        if after[qid] != rec:
            # Named from the pairs that were compared. A state change can change WHICH keys are
            # compared, so the union is walked, in first-seen order.
            b_map, a_map = dict(rec[1]), dict(after[qid][1])
            changed = [
                k for k in dict.fromkeys([*b_map, *a_map]) if b_map.get(k) != a_map.get(k)
            ]
            raiser = (
                f" {_Q_RAISER_HINT}." if any(k in _Q_RAISER_FIELDS for k in changed) else ""
            )
            raise RegistryError(
                "repair_decision_record",
                f"this repair would change what {qid} RECORDS "
                f"({', '.join(changed) or 'state/shape'}).{raiser} repair fixes STRUCTURE; the decision "
                f"record belongs to the lifecycle verbs. Note that `question` and `work` are part "
                f"of that record once a question leaves `open`: a decision is the (asked, "
                f"answered) pair, and repointing either half rewrites it. There is deliberately "
                f"NO CLI path to rewrite a decision already recorded (gh#129) -- `ask` a fresh "
                f"question that supersedes it.",
            )
    for qid, (state, _rec) in after.items():
        if qid not in before and state != "open":
            raise RegistryError(
                "repair_decision_record",
                f"{qid} would appear for the first time already in state {state!r}. A repair may "
                f"recover a question that the parser could not see, but only as `open` -- a block "
                f"that arrives carrying an answer was authored, not recovered.",
            )


_QHDR_SHAPE_RE = re.compile(r"^#*[ \t]*Q-[0-9]")


def _heading_key(line: str) -> str | None:
    """A line's place in the heading skeleton, or None if it is not a heading line at all.

    Any line that BEGINS with `#` counts, deliberately wider than `_SECTION_RE`/`_QBLOCK_RE`: a
    heading the parser cannot read today (`### Q-0001 <<mangled`, `##Questions`) is still
    structure, and must be one a repair can neither mint nor remove.

    Section-level (`#`, `##`) headings are keyed by their TEXT, so no repair can rename one into a
    second `## Questions` that shadows the real section (`_section_bounds` is last-wins). Block
    headings (`###` and deeper) are keyed by LEVEL only: which qid a `### Q-NNNN` names is identity,
    and identity is `_guard_decision_record`'s job -- keying on it here would refuse the one
    heading edit repair exists for, a mangled `### Q-NNNN` put back into shape.
    """
    # A qid-shaped line is a question block header however badly it is mangled -- hashes dropped,
    # doubled, or stripped entirely. Keying all of them alike keeps restoring a LOST header to
    # `### Q-NNNN` a no-op on the skeleton, while overwriting a row or bullet with one still
    # changes it, because what it replaces was never qid-shaped.
    if _QHDR_SHAPE_RE.match(line):
        return "###"
    if not line.startswith("#"):
        return None
    hashes = len(line) - len(line.lstrip("#"))
    # Strip only what `_SECTION_RE` itself tolerates. A full rstrip() also eats NBSP and other
    # Unicode spaces, so a `## Questions<NBSP>` the parser cannot read would key the same as the
    # real heading and could be "restored" into a live second section.
    return line.rstrip(" \t") if hashes <= 2 else "#" * hashes


def _heading_skeleton(text: str) -> list[str]:
    """The ordered heading keys of the whole file. Ordered and total, so a move is a change too."""
    return [k for k in map(_heading_key, text.split("\n")) if k is not None]


def _guard_heading_skeleton(text: str, new_text: str, old_line: str, new_line: str) -> None:
    """Refuse a repair that changes the heading skeleton -- gh#192.

    The decision-record guard stops the CONSEQUENCE (a recorded decision moving) but not the
    PRIMITIVE, and two residues survived it at rc=0:

    * On a registry whose `## Questions` is EMPTY there is no qid to lose, so a run of
      `repair --section Blockers` could plant a whole block crowned with a second `## Questions`.
      The planted section shadowed the real one and produced an OPEN question attributed to a lane
      that never asked it -- which the new-qid clause admits, because it cannot tell a recovered
      block from a conjured one.
    * Against a populated section only the crown refused; every line before it landed.

    Both need a heading that did not exist. Section membership is recomputed from headings after
    the write, so the caller's `--section` and the bytes' real home need never agree -- which is
    why this check reads the WHOLE file and is not gated on the declared section. With no heading
    mintable, the only way a new qid can appear is a `###` line that was already a heading being
    put back into shape: a block that existed and the parser could not see. That is exactly the
    recovery the new-qid clause is for.
    """
    if _heading_skeleton(text) == _heading_skeleton(new_text):
        return
    old_k, new_k = _heading_key(old_line), _heading_key(new_line)
    if old_k is None:
        what = "turn a non-heading line into a heading"
    elif new_k is None:
        what = "remove a heading"
    else:
        what = "rename or re-level a heading"
    raise RegistryError(
        "repair_heading_skeleton",
        f"this repair would {what}, changing the registry's heading skeleton (the ordered list "
        f"of section and block headings). repair fixes a malformed line; minting, deleting or "
        f"renaming structure is not that -- a planted `## Questions` shadows the real section "
        f"(the last heading of a name wins for every reader) and a planted `### Q-NNNN` conjures "
        f"a question no lane asked. A `###` header mangled in place stays repairable; a damaged "
        f"`##` section heading is restored by a human (git history), not by repair.",
    )


def _violation_set(text: str) -> tuple[set[str], set[str]]:
    """``(damage, rows)``: `validate`'s findings as two sets, damage kept apart from row problems.

    A board repair is mending may already be damaged (that is what repair is for), so damage is
    a VALUE to compare here, never an exception that aborts the comparison. A file that does not
    parse at all is one damage entry and no rows.
    """
    try:
        damage, problems = _validation_findings(text)
    except RegistryError as e:
        return {f"[{e.rule_key}] {e.detail}"}, set()
    return {f"[{e.rule_key}] {e.detail}" for e in damage}, set(problems)


_DUP_COUNT_RE = re.compile(r"appears on [0-9]+ rows")


def _relabel_key(problem: str) -> str:
    """*problem* with its duplicate count blanked: `appears on 3 rows` and `appears on 2 rows`
    are one problem at two sizes, and mending one copy must not read as a new one."""
    return _DUP_COUNT_RE.sub("appears on N rows", problem)


def _guard_validate_delta(text: str, new_text: str, section: str, lines: list[str], i: int) -> None:
    """Refuse a repair whose result `validate` would score worse than it should -- gh#681.

    Three conditions, all judged against `validate`'s own checks (`_validation_findings`). The
    probe board is the original with the target line DELETED, which is also the repaired board
    with the new line deleted, so one probe answers both "what did the old line own" and "what
    does the new line own":

    * DAMAGES: a lost table or unparseable file the result has and the original did not. The
      separator-row swap that dropped the whole `## Blockers` table is this.
    * ADDED: a row problem the NEW line owns (present after, gone once the new line is deleted)
      that the original did not have. Ownership, not a before/after diff, because a diff blames
      the repair for problems it only uncovered or relabelled: restoring a lost separator would
      "add" every row problem elsewhere on the board, and mending one of three duplicate rows
      would "add" the message `appears on 2 rows`. Either refusal wedges a board no other verb
      may write.
      ADDED also takes every NEW problem, up to a relabelled duplicate count, because ownership
      by deletion is blind to one shape: resolving a blocker a parked row waits on orphans that
      row, and so does deleting the blocker, so the probe carries the problem too. No damage
      masks row problems any more (`_validation_findings`), so the only thing a plain diff
      misreads is a count, and `_relabel_key` removes it.
    * KEPT: a row problem the TARGET line owned before (gone once it is deleted), still present
      after -- a repair that edits a broken row without fixing it.

    Problems owned by no line in play are neither, so a board broken in several places stays
    repairable one line at a time.
    """
    before_dmg, before = _violation_set(text)
    after_dmg, after = _violation_set(new_text)
    without = lines[:i] + lines[i + 1:]
    _, probe = _violation_set(_replace_body(text, section, "\n".join(without)))
    damages = sorted(after_dmg - before_dmg)
    had = {_relabel_key(v) for v in before}
    caused = {v for v in after - before if _relabel_key(v) not in had}
    added = sorted(((after - probe) - before) | caused)
    kept = sorted((before - probe) & after)
    if not (damages or added or kept):
        return
    parts = []
    if damages:
        parts.append("damages the board: " + "; ".join(damages))
    if added:
        parts.append("adds: " + "; ".join(added))
    if kept:
        parts.append("leaves the target line's own violation: " + "; ".join(kept))
    raise RegistryError(
        "repair_fails_validate",
        "this repair would leave a board `validate` refuses -- " + " | ".join(parts)
        + ". Nothing was written. Replace the line with one that passes validate.",
    )


def _protected_lines(questions_body: str) -> list[str]:
    """Every human-decision field LINE in the Questions body, in file order.

    ORDERED LIST, never a set: order and multiplicity both carry meaning here. The field parser
    is last-wins, so two `- state:` lines are not the same registry as one, and which of them
    comes last decides what the question says.
    """
    return [ln for ln in questions_body.split("\n") if _Q_PROTECTED_RE.match(ln)]


def _protected_delta(before: list[str], after: list[str]) -> str:
    """Name the fields whose lines differ, for the refusal message. Never quotes the lines."""
    names, seen = [], set()
    for ln in [x for x in before if x not in after] + [x for x in after if x not in before]:
        m = _Q_PROTECTED_RE.match(ln)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            names.append(m.group(1))
    # Same lines, different order or multiplicity — still a change, and still refused. Naming a
    # field would be wrong here because no field's TEXT moved; what moved is which one wins.
    return ", ".join(sorted(names)) if names else "the order/multiplicity of the decision lines"


#: Each table section's shape: (column count, header first cell). ONE copy, read by both
#: `_validation_findings` and `_boilerplate_kind`, so the header repair refuses is by
#: construction the header validate skips.
TABLE_SHAPES = {"Agents": (7, "agent"), "Queue": (7, "id"), "Blockers": (6, "id")}

#: Boilerplate a --new-line may not write while an intact one is already in the section.
_SINGLETON_BOILERPLATE = ("Deploy lock line", "Notes heading line")


def _boilerplate_kind(section: str, line: str) -> Optional[str]:
    """Name the boilerplate *line* is in *section*, or None for an ordinary line (gh#569).

    Only INTACT boilerplate: a mangled separator no longer parses as one, so it stays
    repairable -- which is the repair this verb exists for. The Deploy lock / Notes lines are
    judged by the parser's own predicates, so every spelling the parser reads as the real line
    (a `*` bullet, extra spaces) is refused too. The Deploy lock line counts only when the
    holder parse can READ it: one that kept its prefix but lost `holder:` / `taken-at:` is the
    damage `validate` reports, and calling it intact made it unrepairable and its restoration
    a "second" lock line, leaving the wake gate red with no mediated way out.
    """
    s = line.strip()
    if s.startswith("<!--") or s.endswith("-->"):
        return "HTML comment"
    if section == "Queue" and _DEPLOY_RE.match(line) is not None:
        return "Deploy lock line"
    if section == "Queue" and _is_notes_header(line):
        return "Notes heading line"
    cells = _cells(line)
    if cells is not None and _is_separator(cells):
        return "table separator"
    if cells and section in TABLE_SHAPES and cells[0] == TABLE_SHAPES[section][1]:
        return "table header"
    return None


def _guard_repair_new_line(section: str, body_lines: Optional[list[str]], new_line: str) -> None:
    """Refuse a --new-line that plants boilerplate (gh#569).

    A comment is never a repair. A second Deploy lock / Notes line is inert to the first-wins
    parse but shows a fabricated holder to anyone reading the raw board. Restoring the ONLY such
    line (the intact one is gone) IS the repair, so it stays allowed; separators and headers
    stay writable for the same reason -- mending a mangled one writes exactly that shape.

    *body_lines* None is the pre-lock pass: only what argv alone decides (the comment, and a
    line that is no row of a table section's table).
    """
    kind = _boilerplate_kind(section, new_line)
    if kind is None or kind in ("table separator", "table header"):
        _guard_table_row(section, new_line, headings=False)
        return
    if kind in _SINGLETON_BOILERPLATE and (body_lines is None or not any(
        _boilerplate_kind(section, ln) == kind for ln in body_lines
    )):
        return
    why = (
        "a comment would silently delete a row" if kind == "HTML comment"
        else f"a second {kind} fakes what the board appears to say"
    )
    raise RegistryError(
        "repair_boilerplate_line",
        f"--new-line is a {kind}; repair may not plant boilerplate ({why})",
    )


def _guard_table_row(section: str, new_line: str, *, headings: bool) -> None:
    """Refuse a --new-line in a table section that is not a row of that table.

    A non-row line is inert to every parser, so writing one over a real row deleted that row
    with rc=0 and `validate` green, and a run of them planted a question block line by line
    inside `## Blockers`, every step but the crown landing. What a table section may receive:
    a pipe-table line (its column count is `_guard_validate_delta`'s to judge), a blank line
    (removing a stray line writes nothing), and in `## Queue` the Deploy lock / Notes line,
    whose duplicates `_guard_repair_new_line` already refuses.

    *headings* False is the pass before the parse: heading-shaped lines are left to
    `_guard_heading_skeleton`, whose refusal names the structural change. The pass after it
    (True) refuses the heading-shaped lines that left the skeleton unchanged.
    """
    if section not in TABLE_SHAPES or not new_line.strip() or _cells(new_line) is not None:
        return
    # Only a READABLE lock line: an unreadable one is inert to the parse like any non-row line.
    if section == "Queue" and (_DEPLOY_RE.match(new_line) is not None or _is_notes_header(new_line)):
        return
    if not headings and _heading_key(new_line) is not None:
        return
    raise RegistryError(
        "repair_not_a_row",
        f"--new-line is not a row of the ## {section} table; a line written into a table "
        f"section must be a `| ... |` row of that table (or blank, to remove a stray line). "
        f"Any other line is inert there, so writing one over a row deletes that row unseen.",
    )


def _repair_argv_checks(a) -> None:
    """`repair`'s refusals that argv alone decides; run PRE-LOCK too (gh#656, `_ARGV_CHECKS`)."""
    _require_capability("repair", a.actor)
    if a.section not in SECTION_NAMES:
        raise RegistryError("file_missing", f"unknown section {a.section!r} (one of {list(SECTION_NAMES)})")
    if "\n" in a.new_line or "\r" in a.new_line:
        raise RegistryError("inline_only", "--new-line must be a single line")
    # gh#558: every section carries blank separator lines, and `line == a.old_line` matches one,
    # so a blank --old-line turned repair into an INSERT: `--section Directives --old-line ''`
    # minted a standing human instruction, rc=0. Refused before the scan, whatever the section.
    if not a.old_line.strip():
        raise RegistryError(
            "repair_blank_old_line",
            f"--old-line is {a.old_line!r}; name the exact damaged line to replace",
        )
    # gh#569: Directives are dev-manager's alone, and it has `directive` / `expire-directive`.
    # Replacing the section comment every registry carries minted a standing instruction, rc=0.
    if a.section == "Directives":
        raise RegistryError(
            "directive_devmanager",
            "repair may not write ## Directives; dev-manager owns it (`directive`, "
            "`expire-directive`), and a damaged directive is a human's to restore from git",
        )
    kind = _boilerplate_kind(a.section, a.old_line)
    if kind:
        raise RegistryError(
            "repair_boilerplate_line",
            f"--old-line is the section's {kind}; repair mends a damaged line and may not "
            f"replace one every registry carries (doing so inserts a row, or drops the lock)",
        )
    # A Note bullet is another lane's message, struck by its addressee and removed by the cull
    # with a log receipt. Writing a row over one minted a Queue row and deleted the Note, rc=0
    # and validate green. The striker's own recognition rule decides what a Note is, and by that
    # rule a mangled Deploy lock / Notes line IS a Note bullet -- so restoring the line it was
    # stays allowed (a duplicate of an intact one is refused under the lock).
    if (
        a.section == "Queue"
        and _is_note_bullet(a.old_line)
        and _boilerplate_kind(a.section, a.new_line) not in _SINGLETON_BOILERPLATE
    ):
        raise RegistryError(
            "repair_note_line",
            "--old-line is a coordination Note; repair may not replace or remove one. Notes "
            "are owned by `note --strike` (the addressee) and the cull, not by repair",
        )
    # Field policy, layer 1 of 2: the NAMED field. Redundant for safety now that the effect check
    # below closes the class, and kept anyway because it fires BEFORE the match scan — so the
    # answer is the same whether or not the line is present, and the message can name the exact
    # field the caller typed instead of a computed delta. A good error is worth a redundant guard.
    if a.section == "Questions":
        protected = _Q_PROTECTED_RE.match(a.old_line)
        if protected and protected.group(1) in _Q_RAISER_FIELDS:
            raise RegistryError(
                "repair_question_field",
                f"{protected.group(1)!r} is not structure; repair may not write it. "
                f"{_Q_RAISER_HINT}. Another actor's words in it would put them in the raiser's "
                f"mouth -- on an advisory question it IS the urgency the human reads.",
            )
        if protected:
            raise RegistryError(
                "repair_question_field",
                f"{protected.group(1)!r} is a human-decision field, not structure; repair may "
                f"not write it. The lifecycle verbs own it: `answer` records a decision on an "
                f"open question and `apply` consumes it. There is deliberately NO CLI path to "
                f"rewrite a decision already recorded (gh#129); `ask` a fresh question instead.",
            )
    _guard_repair_new_line(a.section, None, a.new_line)


def _cmd_repair(text: str, a) -> str:
    _repair_argv_checks(a)
    body = _get_body(text, a.section)
    lines = body.split("\n")
    _guard_repair_new_line(a.section, lines, a.new_line)
    for i, line in enumerate(lines):
        if line == a.old_line:
            lines[i] = a.new_line
            # Field policy, layer 2 of 2: the EFFECT. Guarding what the caller TYPED in --old-line
            # was my first cut and it was incomplete -- proven with a live rc=0 run. The field
            # parser is last-wins and `state` is the FIRST field of a canonical block, so
            # overwriting any UNPROTECTED line (`- work: E-0038`) with `- state: answered` shadows
            # the real state, `validate` prints `registry valid`, and the question is promoted
            # without an answer. Comparing the protected lines before and after closes the class
            # rather than the one route: no repair may change a decision field's value BY ANY
            # ROUTE -- named, shadowed, duplicated, reordered or deleted.
            if a.section == "Questions":
                before_p, after_p = _protected_lines(body), _protected_lines("\n".join(lines))
                if before_p != after_p:
                    delta = _protected_delta(before_p, after_p)
                    raiser = (
                        f" {_Q_RAISER_HINT}."
                        if any(f in delta.split(", ") for f in _Q_RAISER_FIELDS) else ""
                    )
                    raise RegistryError(
                        "repair_question_field",
                        f"this repair would change the decision record "
                        f"({delta}) in ## Questions.{raiser} repair fixes "
                        f"STRUCTURE and may not write a decision field by ANY route -- not by "
                        f"naming one in --old-line, and not by shadowing one from another line "
                        f"(the field parser is last-wins).\n  This includes RESTORING a decision "
                        f"field line that is itself malformed: that is still a write to the "
                        f"record, and this is the one verb that could fabricate consent, so it is "
                        f"deliberately not the verb for it -- escalate to the human instead. The "
                        f"lifecycle verbs own these fields: `answer` records a decision on an open "
                        f"question, `apply` consumes it. There is deliberately NO CLI path to "
                        f"rewrite a decision already recorded (gh#129).",
                    )
            new_text = _replace_body(text, a.section, "\n".join(lines))
            try:
                parse_registry(new_text)  # repair may not itself leave the file un-parseable
            except RegistryError as e:
                raise RegistryError("repair_corrupt", str(e)) from e
            # Field policy, layer 3 of 3: the PARSED RECORD, and the only layer NOT gated on
            # `a.section`. Layers 1 and 2 both trust `--section`, which is a string the CALLER
            # typed, and `_section_bounds` maps headings in file order with the LAST one winning
            # -- so a run of `repair --section Blockers` can write a whole block over filler rows
            # and crown it with a second `## Questions` heading that shadows the real section.
            # Every consumer then reads the planted one and the in-section layers never execute.
            # This layer reads what the FLEET will read, so a declared section cannot lie to it.
            # Ordered after the parse guard deliberately: it re-parses, and a file the repair
            # corrupted should be reported as corrupt, not as a decision-record violation.
            _guard_decision_record(text, new_text)
            # Structure policy (gh#192): the PRIMITIVE the layers above only catch the consequence
            # of. Last on purpose, so every refusal those layers already name keeps its key; this
            # one owns what they let through -- a heading planted where no decision is displaced.
            _guard_heading_skeleton(text, new_text, a.old_line, a.new_line)
            # Row shape, second pass: a heading-shaped line the skeleton check let through (it
            # replaced one of the same key) is still no row of a table section's table.
            _guard_table_row(a.section, a.new_line, headings=True)
            # Validate policy (gh#681): the row checks the wake gate runs. Last, so every refusal
            # above keeps its more specific key. `lines` holds the replacement; the original body
            # is what the ownership probe deletes the target from.
            _guard_validate_delta(text, new_text, a.section, body.split("\n"), i)
            # Ownership policy (gh#334): last, so a result validate refuses keeps that key; this
            # one owns the valid -> valid rewrite of a row whose cells other verbs own.
            if a.section == "Queue":
                _guard_queue_ownership(text, body.split("\n"), a.old_line, a.new_line)
            elif a.section == "Blockers":
                _guard_blocker_ownership(body.split("\n"), a.old_line, a.new_line)
            elif a.section == "Agents":
                _guard_agents_ownership(body.split("\n"), a.old_line, a.new_line)
            # Audit trail. A repair is the one sanctioned write of a row its actor does not own,
            # and until now it left NO trace anywhere — no log entry, no Note — so the fleet could
            # not tell a repaired row from one that was always that way. Same mechanism as the
            # stale-lock steal below, not a second one.
            note = f"repaired ## {a.section}: {_repair_target(a.section, lines, i, a.old_line)}"
            # Bounded by construction (section + id + field name), clamped anyway: `_append_note`
            # does not enforce the cap, and a repair must never fail — nor silently blow the
            # 300-byte budget — over its own audit line. Truncation is on the ENCODED bytes with
            # errors="ignore", so a clamp can never land mid-codepoint and corrupt the note.
            note = note.encode("utf-8")[:NOTE_MAX_BYTES].decode("utf-8", "ignore")
            return _append_note(new_text, f"{a.actor} -> fleet: {note}")
    raise RegistryError("repair_no_match", f"no line == --old-line in section {a.section}")


# --------------------------------------------------------------------------- #
# Validate — the detection gate: is the registry schema-valid + consistent?
# --------------------------------------------------------------------------- #
def validate_violations(text: str) -> list[str]:
    """Every row-level problem `validate` reports on *text*, in report order. Pure: no I/O.

    ONE SOURCE OF TRUTH for two callers (gh#681). `validate` prints these; `repair` compares
    them before and after its replacement (through `_validation_findings`, which this wraps),
    so a repair can never write a board the wake gate refuses. Two copies of these checks
    would drift on the day one of them gains a rule.

    Raises `RegistryError` when *text* does not parse, and `file_damaged` when a required table
    is gone -- damage is not a row problem, and each caller decides how to present it.
    """
    damage, problems = _validation_findings(text)
    if damage:
        raise damage[0]
    return problems


def _queue_row_findings(rid: str, r: QueueRow, reg: Registry) -> list[tuple[int, str]]:
    """The row checks `validate` runs, which `repair` reads off the raw line, each paired with
    the ONE column it judges.

    The column is returned, never recovered from the message, because `repair`'s ownership guard
    reads it (gh#811): on a row these checks reject, repair may change only the cells named here.
    Every check below reads exactly one cell, so that mapping is exact, not a heuristic -- save
    the in-flight-with-no-claimant check, which reads state and claimed_by and is filed under
    claimed_by, the missing cell (its coupled mend, reap's write, moves state with it).

    The id check never fires from `validate` through this function: the parser drops a row whose
    id fails `_row_key_ok("Queue", ...)`, so it never reaches here, and `_table_row_problems`
    reports it off the raw line instead. It is here for the guard, which reads the raw line.
    """
    findings: list[tuple[int, str]] = []
    if not _row_key_ok("Queue", rid):
        findings.append((0, f"Queue: bad id {rid!r}"))
    if r.lane not in LANES:
        findings.append((2, f"Queue[{rid}]: bad lane {r.lane!r}"))
    if r.state not in QUEUE_STATES:
        findings.append((4, f"Queue[{rid}]: bad state {r.state!r}"))
    if r.claimed_by not in AGENTS and r.claimed_by not in ("-", ""):
        findings.append((3, f"Queue[{rid}]: bad claimed_by {r.claimed_by!r}"))
    # AN IN-FLIGHT ROW WITH NO CLAIMANT IS STRANDED: `claim` takes only a `ready` row, `release`
    # only the claimant's, and `reap` matches an agent, so nothing ever moves it. No verb writes
    # this shape (each writes `ready`/`withdrawn` with '-', or an in-flight state with an agent),
    # so only a hand edit or a future verb could; reported so every reader fails closed. The
    # check reads two cells and is filed under claimed_by: that is the missing cell, and repair's
    # coupled mend for a flagged claimed_by on an in-flight row is already reap's write.
    elif r.state in RETURNABLE_QUEUE_STATES and r.claimed_by in ("-", ""):
        findings.append((3,
            f"Queue[{rid}]: state {r.state!r} with claimed_by {r.claimed_by!r} - in flight with "
            "no claimant, so no verb moves it (repair to reap's write: claimed_by '-', state "
            "'ready', waiting_on '-')"
        ))
    # A PARKED ROW POINTING AT A DEAD REFERENT IS ORPHANED, and nothing reported it.
    # `_cmd_release` checks `--waiting-on` at write time (gh#648, gh#707), but the referent
    # can die afterwards: after a retract (or an apply) the Queue row sits `parked`, still
    # claimed, pointing at an id in neither section -- and the strand alarm cannot see it,
    # because that alarm reads Agents rows. Reported rather than refused: this is the
    # backstop, not the write path.
    ref = _queue_ref(r)
    if ref not in ("-", "") and ref not in _live_ids(reg):
        findings.append((5,
            f"Queue[{rid}]: waiting_on {ref} is not a live question/blocker "
            "(consumed or missing) - the row is parked on nothing"
        ))
    return findings


def _queue_row_problems(rid: str, r: QueueRow, reg: Registry) -> list[str]:
    """`_queue_row_findings` without the columns, for `_validation_findings`. `repair`'s ownership
    guard reads `_queue_row_findings` itself, so "a row validate accepts" means the same to both.
    """
    return [msg for _, msg in _queue_row_findings(rid, r, reg)]


#: Queue columns in table order, for naming what a refused repair would have changed.
_QUEUE_COLUMNS = ("id", "work item", "lane", "claimed_by", "state", "waiting_on", "notes")
#: Who owns each cell of a WELL-FORMED Queue row (gh#334). lead-dev mints the row and moves it
#: (`relane`/`withdraw` also annotate notes); the claimant alone moves claimed_by/state/waiting_on.
_QUEUE_LEAD_COLS = frozenset({0, 1, 2, 6})
_QUEUE_CLAIMANT_COLS = frozenset({3, 4, 5})


def _queue_col_names(cols) -> str:
    return ", ".join(_QUEUE_COLUMNS[j] for j in sorted(cols))


def _queue_owner_routes(cols: set[int]) -> list[str]:
    """The owning verbs for *cols*, one clause per owner naming only those columns, for a
    `repair_queue_owned` refusal."""
    routes = []
    lead, claimant = cols & _QUEUE_LEAD_COLS, cols & _QUEUE_CLAIMANT_COLS
    if lead:
        routes.append(f"{_queue_col_names(lead)} belong(s) to lead-dev: `publish`, `relane` or "
                      "`withdraw`")
    if claimant:
        routes.append(f"{_queue_col_names(claimant)} belong(s) to the claimant: `claim` or "
                      "`release`; a dead claimant is cleared by `reap` (steward)")
    return routes


#: Blockers / Agents columns in table order, for naming what a refused repair would change.
_BLOCKER_COLUMNS = ("id", "raised_by", "owner", "work", "state", "description")
_AGENTS_COLUMNS = ("agent", "status", "work", "phase", "heartbeat", "eta", "waiting_on")


def _blocker_owner_routes(cols: set[int]) -> list[str]:
    """The owning verbs for Blockers *cols*, for a `repair_blocker_owned` refusal."""
    routes = []
    raiser = cols & {0, 1, 3, 5}
    if raiser:
        routes.append(f"{', '.join(_BLOCKER_COLUMNS[j] for j in sorted(raiser))} belong(s) to "
                      "the raiser: `raise-blocker` writes them once")
    if 2 in cols:
        routes.append("owner belongs to `assign-blocker`")
    if 4 in cols:
        routes.append("state belongs to the owner: `resolve-blocker`, then `cull` (steward)")
    return routes


def _agents_owner_routes(cols: set[int]) -> list[str]:
    """The owning verbs for Agents *cols*, for a `repair_agents_owned` refusal."""
    return [f"{', '.join(_AGENTS_COLUMNS[j] for j in sorted(cols))} belong(s) to the lane "
            "itself: `status` (which stamps the heartbeat); `stand-down` (steward) marks a lane "
            "a human stopped and `reap` (steward) idles a dead one"]


#: Per table: (column names, owner routes) for a cell-scoped mend's refusal.
_MEND_TABLES = {
    "Queue": (_QUEUE_COLUMNS, _queue_owner_routes),
    "Blockers": (_BLOCKER_COLUMNS, _blocker_owner_routes),
    "Agents": (_AGENTS_COLUMNS, _agents_owner_routes),
}

#: The OWNER-HELD cells a cell-scoped mend may only clear, per table: column -> (the neutral
#: values, the owning verbs for any other). A flagged cell says the old value is garbage, not
#: what the owner meant, so the steward may blank it but not choose its next value: pointing a
#: claimant, a park, a blocker owner or a lane state at a live value is a decision those verbs
#: gate (`claim`'s lane, `release`'s live referent, `assign-blocker`'s ownership limit,
#: `resolve-blocker`'s owner check), and repair would take it with none of their checks.
#: Neutral is the value that grants nothing: '-' holds no claim and parks on nothing; an `open`
#: blocker still needs its owner to close it (a mended `resolved` would fake a resolution the
#: cull then logs); `idle` is reap's value and is never excluded from the liveness alarms
#: (`halted` silences the missed-wake alarm, so it is no neutral). Not owner-held: a Queue lane
#: (lead-dev's `relane` reads no claim) and state (no state is neutral: each one is a
#: different claimant's position), and a blocker's raised_by (provenance no verb reads for
#: authority, and validate refuses '-' there).
_NEUTRAL_MENDS = {
    "Queue": {
        3: (("-", ""), "a claim is taken by the claimant's `claim` and dropped by its "
                       "`release`; a dead claimant's is cleared by `reap` (steward)"),
        5: (("-", ""), "a park is pointed by the claimant's `release` (--state parked "
                       "--waiting-on <id>); a dead claimant's is cleared by `reap` (steward)"),
    },
    "Blockers": {
        2: (("-", ""), "a blocker's owner is moved by `assign-blocker`"),
        4: (("open",), "a blocker is closed by its owner's `resolve-blocker` and swept by "
                       "`cull` (steward)"),
    },
    "Agents": {
        1: (("idle",), "a lane's status is declared by the lane itself with `status`; "
                       "`stand-down` (steward) marks a lane a human stopped"),
    },
}


def _guard_flagged_mend(section: str, key: str, cells: list[str], new_line: str,
                        findings: list[tuple[int, str]],
                        coupled: frozenset[int] = frozenset()) -> list[str]:
    """Refuse a repair of a row the row checks reject unless it changes only the FLAGGED cells,
    and an owner-held flagged cell only to its neutral value (`_NEUTRAL_MENDS`). Returns the new
    cells. ONE copy for all three tables, so the Queue rule and its twins cannot drift.

    *coupled* names unflagged cells the caller lets move WITH the mend, because clearing the
    flagged cell alone would leave a shape no verb can move (the Queue's coupled mends); the
    caller pins their exact values. A blank --new-line (a deletion) removes every unflagged cell
    too, so it is refused; a row no verb can reach never gets here, its table's guard returns
    before this.
    """
    columns, routes = _MEND_TABLES[section]
    ncol = len(columns)

    def names(cols) -> str:
        return ", ".join(columns[j] for j in sorted(cols))

    new = _cells(new_line)
    changed = (
        {j for j in range(ncol) if cells[j] != new[j]}
        if new is not None and len(new) == ncol else None
    )
    flagged = {j for j, _ in findings}
    if changed is None:
        stray = set(range(ncol)) - flagged
        what = f"Deleting the row would also remove: {names(stray)}"
    else:
        stray = changed - flagged - coupled
        what = f"It would also change: {names(stray)}"
    if stray or changed is None:
        also = f" (with {names(coupled)} coupled to that mend)" if coupled else ""
        raise RegistryError(
            key,
            f"--old-line is a {section} row the row checks `validate` runs reject ({cells[0]}: "
            + "; ".join(msg for _, msg in findings)
            + f"); repair may only mend the flagged cell(s): {names(flagged)}{also}. "
            f"{what}, which those checks did not flag. Those cells keep their owners: "
            + "; ".join(routes(stray)) + ". Mend the flagged cell(s) alone, then "
            "use the owning verb for anything else.",
        )
    for j in sorted(changed):
        neutral = _NEUTRAL_MENDS[section].get(j)
        if neutral is not None and new[j] not in neutral[0]:
            raise RegistryError(
                key,
                f"--new-line would set {section} row {cells[0]}'s {columns[j]} to {new[j]!r}. "
                f"{columns[j]} is owner-held, and repair may only clear a flagged owner-held "
                f"cell, to {neutral[0][0]!r}: {neutral[1]}. Mend it to {neutral[0][0]!r} and "
                "leave its next value to that verb.",
            )
    return new


def _guard_queue_ownership(text: str, body_lines: list[str], old_line: str, new_line: str) -> None:
    """Refuse a repair that rewrites a Queue cell some other verb owns (gh#334, gh#811).

    repair is the steward's exact-line write and it holds no lane authority: on master it moved a
    healthy row to another lane, claimed it for a builder and set it `building`, rc=0 with
    `validate` green -- every one of those a cell some other verb owns. So:

    * A row validate ACCEPTS is refused outright.
    * A row the row checks reject for a cell (bad enum, dead waiting_on) may have exactly the
      flagged cells changed and no other (gh#811): mending a bad `state` was also rewriting lane,
      claimant and notes at rc=0, because this guard used to step aside for the whole row. Each
      flag names one cell (`_queue_row_findings`), so "flagged" is exact. A blank --new-line (a
      deletion) changes every cell and is refused; a reshaped line never gets here, it is
      refused earlier as `repair_fails_validate`. A flagged claimed_by or waiting_on may only
      be cleared to '-' (`_NEUTRAL_MENDS`), and where '-' alone would strand the row the mend
      is pinned to the owning verb's own write, state included (`_queue_coupled_mend`). Nor
      may a mend leave the row `parked` on nothing, a shape `release` refuses and `validate`
      cannot see. In-flight with no claimant, a shape no verb moves, is not judged here:
      `validate` flags it (`_queue_row_findings`), so `_guard_validate_delta`, which runs
      first, refuses any mend that makes or keeps it as `repair_fails_validate`.
    * A row with the wrong cell count, a duplicated id or a bad id stays fully repairable.
    """
    cells = _cells(old_line)
    ncol, header0 = TABLE_SHAPES["Queue"]
    if cells is None or len(cells) != ncol:
        return
    # Every owning verb refuses `id_duplicate`, so repair is the only way back from a duplicate.
    if _raw_id_count(body_lines, header0, cells[0]) > 1:
        return
    # A bad id: the parser drops the row, so no owning verb and no cull can reach it -- they all
    # address rows by id through the parsed board (`validate` reports it off the raw line).
    # Holding it to cell-scoped mends left only one way to delete one: invent an id, withdraw
    # and cull it, which fabricates a log record and receipt Notes. So it stays wholly repair's.
    if not _row_key_ok("Queue", cells[0]):
        return
    findings = _queue_row_findings(cells[0], QueueRow(*cells), parse_registry(text))
    new = _cells(new_line)
    changed = (
        {j for j in range(ncol) if cells[j] != new[j]}
        if new is not None and len(new) == ncol else None
    )
    if findings:
        flagged = {j for j, _ in findings}
        pinned, verb = _queue_coupled_mend(cells, flagged)
        new = _guard_flagged_mend("Queue", "repair_queue_owned", cells, new_line, findings,
                                  frozenset(pinned) - flagged)
        if new[4] == "parked" and new[5].strip() in ("-", ""):
            raise RegistryError(
                "repair_queue_owned",
                f"--new-line would leave Queue row {cells[0]} `parked` and waiting on nothing "
                "(waiting_on '-'), a shape `release` refuses and `validate` cannot see. A "
                "row's park belongs to its claimant: `release` it to another state, or, for a "
                "dead claimant, `reap` (steward)."
                + (f" The one mend repair takes here is what {verb} writes: "
                   + _queue_cells_phrase(pinned) + "." if pinned else ""),
            )
        # '' and '-' are the same "nothing" in claimed_by and waiting_on.
        if pinned and any((new[j].strip() or "-") != v for j, v in pinned.items()):
            raise RegistryError(
                "repair_queue_owned",
                f"--new-line would set Queue row {cells[0]} to "
                + _queue_cells_phrase({j: new[j].strip() for j in pinned})
                + f". With {_queue_col_names(flagged & {3, 5})} flagged on a "
                f"`{cells[4]}` row, clearing it alone leaves a row no verb can move, so repair "
                f"takes exactly one mend, what {verb} writes: {_queue_cells_phrase(pinned)}.",
            )
        return  # the mend repair is for: only flagged cells move
    # Unknown or no cell-level change (a deletion or whitespace-only line): name every owner.
    touched = changed or set(range(ncol))
    what = _queue_col_names(changed) if changed else "the row"
    raise RegistryError(
        "repair_queue_owned",
        f"--old-line is a Queue row `validate` already accepts ({cells[0]}); repair mends a "
        f"MALFORMED row and may not rewrite a well-formed one (would change: {what}). "
        + "; ".join(_queue_owner_routes(touched)) + ". The one well-formed row repair may touch "
        "is a duplicate id (the same id on more than one Queue line), which every owning verb "
        "refuses.",
    )


def _queue_coupled_mend(cells: list[str], flagged: set[int]) -> tuple[dict[int, str], str]:
    """The exact mend a flagged owner-held Queue cell must take when clearing it ALONE would
    strand the row, as ({column: value}, the verb that writes that shape), or ({}, "").

    * claimed_by flagged on an in-flight row: '-' alone leaves `building`/`parked`/`review` with
      no claimant, which `claim` (ready only), `release` (claimant only) and `reap` (matches an
      agent) all skip, and `validate` flags it. So the mend is reap's write: claimant '-',
      state `ready`, waiting_on '-'.
    * waiting_on flagged on a `parked` row (a dead referent): '-' alone is parked on nothing,
      and the claimant's wake gate fails closed on the invalid board, so it never `release`s.
      So the mend is `release --state building`'s write: the claim stays, state `building`,
      waiting_on '-', and the claimant re-parks if it must. With no claimant to keep, reap's.
    """
    reap = ({3: "-", 4: "ready", 5: "-"}, "`reap`")
    if 3 in flagged and cells[4] in RETURNABLE_QUEUE_STATES:
        return reap
    if 5 in flagged and cells[4] == "parked":
        if cells[3].strip() in ("-", ""):
            return reap
        return {4: "building", 5: "-"}, "`release --state building`"
    return {}, ""


def _queue_cells_phrase(values: dict[int, str]) -> str:
    """'claimed_by '-', state 'ready'' for a refusal naming a coupled mend's cells."""
    return ", ".join(f"{_QUEUE_COLUMNS[j]} {v!r}" for j, v in sorted(values.items()))


def _raw_id_count(body_lines: list[str], header0: str, rid: str) -> int:
    """How many data rows of a table body carry *rid* in their first cell.

    Same row filter as validate's duplicate check, read off the RAW lines for the same reason:
    the parsed table is a dict, so a duplicate is already gone by the time it exists.
    """
    count = 0
    for ln in body_lines:
        c = _cells(ln)
        if c is None or _is_separator(c) or (c and c[0] == header0):
            continue
        if c and c[0] == rid:
            count += 1
    return count


def _blocker_row_findings(bid: str, b: Blocker) -> list[tuple[int, str]]:
    """`validate`'s checks on ONE Blockers row, each paired with the ONE column it judges (the
    guard reads the column, as `_queue_row_findings`' does). ONE copy, read by
    `_validation_findings` and by `repair`'s Blockers ownership guard, so "a row validate
    accepts" means the same to both.

    The id check is unreachable today: validate passes only parsed rows (the parser drops a row
    whose id fails `_ROW_KEY_RULES["Blockers"]`), and the guard returns before calling this on
    such a row. It stays so that if either caller ever loosens, the shared rule is still the gate
    and a bad id is reported rather than accepted.
    """
    findings: list[tuple[int, str]] = []
    if not _row_key_ok("Blockers", bid):
        findings.append((0, f"Blockers: bad id {bid!r}"))
    if b.state not in BLOCKER_STATES:
        findings.append((4, f"Blockers[{bid}]: bad state {b.state!r}"))
    if b.raised_by not in AGENTS:
        findings.append((1, f"Blockers[{bid}]: bad raised_by {b.raised_by!r}"))
    if b.owner not in AGENTS and b.owner not in ("-", ""):
        findings.append((2, f"Blockers[{bid}]: bad owner {b.owner!r}"))
    return findings


def _blocker_row_problems(bid: str, b: Blocker) -> list[str]:
    """`_blocker_row_findings` without the columns, for `_validation_findings`."""
    return [msg for _, msg in _blocker_row_findings(bid, b)]


def _agents_row_findings(agent: str, r: AgentRow) -> list[tuple[int, str]]:
    """`validate`'s checks on ONE parsed Agents row, with the column each judges. ONE copy, read
    by `_validation_findings` and by `repair`'s Agents ownership guard."""
    if r.status not in AGENT_STATES:
        return [(1, f"Agents[{agent}]: bad status {r.status!r}")]
    return []


def _guard_blocker_ownership(body_lines: list[str], old_line: str, new_line: str) -> None:
    """Refuse a repair that rewrites a Blockers cell some other verb owns.

    The Queue guard's twin. Without it repair rewrote a healthy row's owner (lead-dev ->
    steward), rc=0, and the steward then resolved a blocker it was never assigned -- walking
    round `assign-blocker`'s ownership limit and `resolve-blocker`'s owner check in two steps.
    So, as on the Queue: a row validate accepts is refused outright; on a row the row checks
    reject only the flagged cells may change, an owner-held one only to its neutral value
    (`_guard_flagged_mend`); and a row no verb can reach -- a wrong cell count, a duplicate id,
    an id the parser drops -- stays wholly repair's, deletion included.
    """
    cells = _cells(old_line)
    ncol, header0 = TABLE_SHAPES["Blockers"]
    if cells is None or len(cells) != ncol:
        return
    if _raw_id_count(body_lines, header0, cells[0]) > 1:
        return
    # The parser drops a non-`B-NNN` id, so no verb and no cull can address the row.
    if not _row_key_ok("Blockers", cells[0]):
        return
    findings = _blocker_row_findings(cells[0], Blocker(*cells))
    if findings:
        _guard_flagged_mend("Blockers", "repair_blocker_owned", cells, new_line, findings)
        return
    raise RegistryError(
        "repair_blocker_owned",
        f"--old-line is a Blockers row `validate` already accepts ({cells[0]}); repair mends a "
        "MALFORMED row and may not rewrite a well-formed one. Its cells have owning verbs: "
        "`raise-blocker` (the raiser) creates it, `assign-blocker` moves its owner, "
        "`resolve-blocker` (the owner) closes it, `cull` (steward) sweeps it once resolved. The "
        "one well-formed row repair may touch is a duplicate id (the same id on more than one "
        "Blockers line).",
    )


def _guard_agents_ownership(body_lines: list[str], old_line: str, new_line: str) -> None:
    """Refuse a repair that rewrites an Agents cell the lane or the steward's verbs own.

    The Queue and Blockers guards' twin. Without it repair rewrote a stale row's heartbeat and
    eta to fresh ones, rc 0 with `validate` green, and `read --stale` stopped reporting a dead
    lane: a faked heartbeat. A row with a bad status was rewritable in every cell the same way,
    so the Queue rule applies: only the flagged cell may change, and status only to `idle`
    (`_guard_flagged_mend`). A row no verb can reach -- a wrong cell count, an agent the parser
    drops, a duplicated agent every owning verb refuses as `id_duplicate` -- stays wholly
    repair's, deletion included.
    """
    cells = _cells(old_line)
    ncol, header0 = TABLE_SHAPES["Agents"]
    if cells is None or len(cells) != ncol:
        return
    if not _row_key_ok("Agents", cells[0]):
        return
    if _raw_id_count(body_lines, header0, cells[0]) > 1:
        return
    findings = _agents_row_findings(cells[0], AgentRow(*cells))
    if findings:
        _guard_flagged_mend("Agents", "repair_agents_owned", cells, new_line, findings)
        return
    raise RegistryError(
        "repair_agents_owned",
        f"--old-line is an Agents row `validate` already accepts ({cells[0]}); repair mends a "
        "MALFORMED row and may not rewrite a well-formed one. Its cells have owning verbs: "
        "`status` (the lane itself) writes every cell and stamps the heartbeat, `stand-down` "
        "(steward) marks a lane a human stopped, `reap` (steward) idles a dead one. The one "
        "well-formed row repair may touch is a duplicated agent (the same agent on more than one "
        "Agents line).",
    )


def _table_row_problems(body: str, name: str) -> list[str]:
    """validate's raw-line row checks on ONE table *body*: a wrong cell count (the parser drops
    the row), a key the parser does not keep (`_ROW_KEY_RULES`; it drops the row) and a key on
    more than one row (the parser keeps the last). ONE copy, read by `_validation_findings` and
    by `read_registry_checked(rows=...)`, so a reader refusing a board for lost rows means
    exactly what `validate` means.
    """
    ncol, header0 = TABLE_SHAPES[name]
    key_rule = _ROW_KEY_RULES.get(name)
    problems: list[str] = []
    seen: dict[str, int] = {}
    for line in body.split("\n"):
        cells = _cells(line)
        if cells is None or _is_separator(cells) or (cells and cells[0] == header0):
            continue
        if len(cells) != ncol:
            problems.append(f"{name}: row has {len(cells)} cells, expected {ncol}: {line.strip()!r}")
        if cells and key_rule is not None and not key_rule[0](cells[0]):
            problems.append(
                f"{name}: {key_rule[1]} {cells[0]!r} is {key_rule[2]} - the parser drops the row, "
                f"so no reader sees it; mend or remove it (steward repair): {line.strip()!r}"
            )
        if cells:
            seen[cells[0]] = seen.get(cells[0], 0) + 1
    for key, count in sorted(seen.items()):
        if count > 1:
            problems.append(
                f"{name}: {key!r} appears on {count} rows - the parser keeps only the "
                f"last row and every write refuses until the duplicate is repaired "
                f"(steward repair)"
            )
    return problems


def _validation_findings(text: str) -> tuple[list[RegistryError], list[str]]:
    """``(damage, problems)``: every lost table, and every row problem, on the same board.

    Damage does NOT stop the row checks here. The parser reads rows whether or not a table's
    separator survived, so the row problems of a damaged board are still computable -- and
    `repair` needs them: when damage hid every row problem, restoring a lost separator made all
    of them look new, and a board with one unrepairable row could never get its table back.
    `validate_violations` keeps the fail-on-damage contract by raising the first entry.
    """
    reg = parse_registry(text)
    damage: list[RegistryError] = []
    problems: list[str] = []
    # Structural: data-looking rows the parser silently drops on a wrong column count,
    # a section whose TABLE is gone, and a key that appears on more than one row.
    #
    # WHY THESE TWO WERE ADDED HERE RATHER THAN ANYWHERE ELSE (gh#245 C, gh#196).
    # `validate` is the verb whose stated job is to assert the registry is schema-valid,
    # and every loop skill now runs it at wake and fails closed. Measured on master:
    # it returned `registry valid`, exit 0, on a board whose Agents table had been
    # removed, on one whose Queue table had, and on one carrying a duplicate Queue id -
    # every one of which a WRITING verb refuses. The wake gate was blind to exactly the
    # damage the other verbs trip over, so a damaged board passed the check that exists
    # to catch it and then broke the first verb to touch it.
    #
    # The duplicate check reads the RAW rows, not `reg`: the parse collapses a section
    # into a dict, so by the time `reg` exists the second row is already gone. A check
    # written against the parsed view could not see the thing it is looking for.
    for name in TABLE_SHAPES:
        body = _get_body(text, name)
        if _separator_index(body) is None:
            damage.append(_table_lost(name))
        problems.extend(_table_row_problems(body, name))
    # Questions are blocks, not table rows, so the loop above cannot see a duplicated `### Q-`
    # header -- and the parse keeps the last block while `_edit_qfield` wrote the first.
    q_seen: dict[str, int] = {}
    for qm in _QBLOCK_RE.finditer(_get_body(text, "Questions")):
        q_seen[qm.group(1)] = q_seen.get(qm.group(1), 0) + 1
    for qid, count in sorted(q_seen.items()):
        if count > 1:
            problems.append(
                f"Questions: {qid!r} appears on {count} blocks - the parser keeps only the "
                f"last block and every write refuses until the duplicate is removed "
                f"({_QDUP_REMEDY})"
            )
    # A block ends only at the next readable header and fields are last-wins, so a header the
    # parser cannot read hands its fields to the question above it -- a live halted question
    # then reads as applied, and cull would sweep it. Flag the header rather than the merge.
    for line in _get_body(text, "Questions").split("\n"):
        if (line.startswith("#") or _QHDR_SHAPE_RE.match(line)) and not _QBLOCK_RE.match(line):
            problems.append(
                f"Questions: unreadable block header {line[:40]!r} - its fields merge into the "
                f"block above it; restore it to `### Q-NNNN` (ASCII digits) or remove it"
            )
    # Vocabulary / enums.
    for ag, r in reg.agents.items():
        problems.extend(msg for _, msg in _agents_row_findings(ag, r))
    for rid, r in reg.queue.items():
        problems.extend(_queue_row_problems(rid, r, reg))
    for qid, q in reg.questions.items():
        st = q.fields.get("state")
        if st not in QUESTION_STATES:
            problems.append(f"Questions[{qid}]: bad state {st!r}")
        if st in ("open", "answered") and _field_is_absent(q.fields.get("default", "-")):
            problems.append(f"Questions[{qid}]: live question missing default")
        if st == Q_RETRACTED and _field_is_absent(q.fields.get("retraction", "-")):
            problems.append(f"Questions[{qid}]: retracted question missing retraction reason")
        urgency = q.fields.get("urgency", "-")
        if urgency not in ("halted", "waiting", "advisory"):
            problems.append(f"Questions[{qid}]: bad urgency {urgency!r}")
        # Reported, NEVER repaired: assumption is raiser-owned prose, and filling it in with
        # another actor's words would launder the very gap this check exists to surface.
        if (st in ("open", "answered")
                and urgency == "advisory"
                and _field_is_absent(q.fields.get("assumption", "-"))):
            problems.append(f"Questions[{qid}]: advisory question missing assumption (raiser must restate it)")
        raised_by = q.fields.get("raised_by", "-")
        if raised_by not in AGENTS:
            problems.append(f"Questions[{qid}]: bad raised_by {raised_by!r}")
        answered_by = q.fields.get("answered_by", "-")
        if answered_by not in ANSWERED_BY and answered_by not in ("-", ""):
            problems.append(f"Questions[{qid}]: bad answered_by {answered_by!r}")
    for bid, b in reg.blockers.items():
        problems.extend(_blocker_row_problems(bid, b))
    if reg.deploy_holder not in AGENTS and reg.deploy_holder not in ("none", "-"):
        problems.append(f"Queue: bad deploy-lock holder {reg.deploy_holder!r}")
    # Without the field line every lock verb refuses and the holder reads "none", so a board
    # that lost it would otherwise pass the wake gate while no lane can take the lock.
    lock_problem = _deploy_line_problem(_get_body(text, "Queue"))
    if lock_problem is not None:
        problems.append(f"Queue: {lock_problem}")
    return damage, problems


def _do_validate(registry_path: Path) -> int:
    text = _read_registry_text(registry_path)
    try:
        parse_registry(text)
    except RegistryError as e:
        print(f"INVALID: registry will not parse: {e}", file=sys.stderr)
        return 2
    problems = validate_violations(text)
    # A WARNING, never a problem (gh#217). Every lane runs validate fail-closed at wake, so an
    # error here would stop claiming on a quiet board -- one with no write coming to repair the
    # header -- over a line that misleads but damages nothing. `_atomic_write` backfills it.
    stale = _stale_notes_header(text)
    if stale is not None:
        says, live = stale
        print(
            f"warning: Notes header says {says or '(empty)'} but {live} live "
            f"note{'' if live == 1 else 's'}; "
            "the next mediated write refreshes it",
            file=sys.stderr,
        )
    if problems:
        print("INVALID registry:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    print("registry valid")
    return 0


# --------------------------------------------------------------------------- #
# Mutating dispatch (the four-step lock dance)
# --------------------------------------------------------------------------- #
# command name -> (edit_fn, needs_log_text)
_MUTATORS = {
    "status": (_cmd_status, False),
    "claim": (_cmd_claim, False),
    "release": (_cmd_release, False),
    # Needs the log: the E- allocator is seeded above every id the log ever carried.
    "publish": (_cmd_publish, True),
    "relane": (_cmd_relane, False),
    "withdraw": (_cmd_withdraw, False),
    "ask": (_cmd_ask, True),
    "answer": (_cmd_answer, False),
    "apply": (_cmd_apply, False),
    "retract": (_cmd_retract, False),
    "raise-blocker": (_cmd_raise_blocker, True),
    "resolve-blocker": (_cmd_resolve_blocker, False),
    "assign-blocker": (_cmd_assign_blocker, False),
    "directive": (_cmd_directive, False),
    "expire-directive": (_cmd_expire_directive, False),
    "deploy-take": (_cmd_deploy_take, False),
    "deploy-release": (_cmd_deploy_release, False),
    "note": (_cmd_note, False),
    "reap": (_cmd_reap, False),
    "stand-down": (_cmd_stand_down, False),
    "repair": (_cmd_repair, False),
}


def _refuse_unless_still_held(lock: "EditLock", registry_path: Path, read: str) -> None:
    """Refuse the write unless this command still holds its lock over the board it read.

    A holder paused past the stale window (a suspended laptop, a stalled disk) has its lock
    broken by the next writer, which then writes its own change. Writing anyway would put this
    command's edit, computed from a board that no longer exists, over the thief's. So the lock
    must still carry this acquire's nonce AND the board on disk must still be the copy read
    under it; the second catches a writer the nonce cannot, one that never took the lock.
    Checked immediately before the write; the check and the replace are still two steps.
    """
    if not lock.still_ours():
        raise EditLockStolenError(
            f"the edit-lock at {lock.path} is now held by {_lock_holder(lock.path).actor}: "
            "another writer judged this hold stale or dead and broke it"
        )
    if _read_registry_text(registry_path) != read:
        raise EditLockStolenError(
            f"{registry_path.name} on disk is no longer the copy this command read under the lock"
        )


def _locked_write(lock: "EditLock", registry_path: Path, read: str, text: str) -> None:
    _refuse_unless_still_held(lock, registry_path, read)
    _atomic_write(registry_path, text)


@contextmanager
def _steal_note_survives_refusal(lock: "EditLock", registry_path: Path, read: str, text: str):
    """On a refusal under the lock, write the stale-lock steal Note alone, then re-raise (gh#656).

    The steal already happened -- the other lane's lock is gone whatever the verb decides -- and
    the docs promise every steal is noted. *text* is the re-read board plus that one Note; the
    refused verb's own change never reached it. Still under the lock; the refusal and its exit
    code propagate unchanged. Wraps only the verb's computation, never a write, so it cannot
    overwrite a verb's successful result.
    """
    try:
        yield
    except RegistryError:
        if lock.stole_from:
            _locked_write(lock, registry_path, read, text)
        raise


#: Per-verb argv-only refusals `_run_mutation` runs before taking the edit-lock (gh#656).
#:
#: WHY PRE-LOCK: under the lock, a refusal could first steal a stale edit-lock and only then
#: refuse. `_steal_note_survives_refusal` now keeps that steal's Note, but refusing before the
#: lock means there is no steal to record at all. Each verb calls its own helper too, so every
#: predicate has one copy. Verbs NOT listed still refuse under the lock and rely on that Note;
#: so does the board-dependent part of a listed verb (e.g. `repair`'s second-lock-line check).
_ARGV_CHECKS = {
    "status": _status_argv_checks,
    "ask": _ask_argv_checks,
    "retract": _retract_argv_checks,
    "relane": lambda a: _publisher_argv_checks(a, "relane"),
    "withdraw": lambda a: _publisher_argv_checks(a, "withdraw"),
    "reap": _reap_argv_checks,
    "claim": _claim_argv_checks,
    "release": _release_argv_checks,
    "note": _note_argv_checks,
    "stand-down": _stand_down_argv_checks,
    "repair": _repair_argv_checks,
}


def _run_mutation(a, timeout: float) -> int:
    _guard_inline_fields(a)  # reject newline/pipe injection before touching the lock or file
    registry_path = _resolve_registry_path(a)
    _require_readable_registry(registry_path)  # pre-lock: never queue the fleet behind a bad path
    lock_dir = _resolve_lock_dir(a)
    # The board's own project names the lock, never --project: see `_edit_lock_name`.
    lock_name = _edit_lock_name(registry_path)
    log_path = registry_path.parent / "registry-log.md"
    # gh#656: the argv-only refusals of the verbs in `_ARGV_CHECKS` run before the lock. After
    # the path checks, so a missing or unreadable board still names itself first.
    argv_checks = _ARGV_CHECKS.get(a.command)
    if argv_checks is not None:
        argv_checks(a)

    lock = acquire_edit_lock(lock_dir, lock_name, a.actor, timeout=timeout)
    ping_prior_status = None  # decided under the lock, sent after it (see the finally)
    try:
        text = _read_registry_text(registry_path)  # re-read UNDER the lock (lost-update guard)
        read = text  # what the pre-write check compares the board on disk against
        if lock.stole_from:
            text = _append_note(
                text,
                f"{a.actor} -> fleet: broke stale registry edit-lock (prev {lock.stole_from}, {lock.stole_age:.0f}s)",
            )
        if a.command == "cull":
            with _steal_note_survives_refusal(lock, registry_path, read, text):
                new_text, log_add, counts, overflow = _cmd_cull(text, a)
            collected: list[Path] = []
            stranded: list[tuple[str, str]] = []   # (name, why) — left on disk, recorded below
            if log_add and overflow:
                # Inline each culled note's overflow body into the same culled block, then
                # delete the file — so status/notes/ cannot become the unbounded store that
                # bounding the registry created. Inlined rather than moved beside the log on
                # purpose: registry-log.md is the ONE append-only archive the fleet already
                # accepts as unbounded and never reads in the hot path, and a second archive
                # directory would just be the same growth one directory over. Inlining also
                # keeps the culled block self-contained — a reader of the log needs nothing
                # else to reconstruct what the note said.
                overflow_dir = registry_path.parent / NOTE_OVERFLOW_DIR
                for name in overflow:
                    path = _resolve_overflow_file(overflow_dir, name)
                    if path is None or not path.is_file():
                        continue  # traversal, or (the common case) a pointer to nothing
                    try:
                        # read_bytes + explicit decode, NOT read_text: read_text applies
                        # universal newlines, and this archive is about to become the only
                        # copy — a CRLF body must not be silently rewritten on the way in.
                        content = path.read_bytes().decode("utf-8")
                    except (OSError, ValueError) as exc:
                        # DEGRADE, never abort: the note itself is what must not be lost, and
                        # the pointer stays in the culled block so the body is still findable.
                        # ValueError as well as OSError, because UnicodeDecodeError IS a
                        # ValueError and non-UTF-8 is the likeliest form of "unreadable" on
                        # this fleet (a Windows tool writing UTF-16 by default). Uncaught it
                        # aborts the whole sweep, stranding every other loop's struck notes,
                        # and does so again on every later cull.
                        print(
                            f"cull: could not read {NOTE_OVERFLOW_DIR}/{name} ({exc}); "
                            "left in place, its note carries the pointer",
                            file=sys.stderr,
                        )
                        stranded.append((name, f"could not read ({_one_line(exc)})"))
                        continue
                    log_add += _overflow_block(name, content)
                    collected.append(path)
            if log_add:
                # A robbed holder must not archive notes its registry write will never drop.
                _refuse_unless_still_held(lock, registry_path, read)
                # Log FIRST, registry SECOND — and the log's DATA durable before the registry
                # is replaced. A crash between the two then costs a duplicate log entry
                # (harmless: id allocation is a max-scan over registry + log) rather than
                # losing the culled rows. Without the fsync the append can still be in the OS
                # page cache when _atomic_write fsyncs the shorter registry, inverting that
                # order on a power loss.
                #
                # Data ordering only — neither this append nor _atomic_write fsyncs the parent
                # DIRECTORY, so on the very first cull (which creates registry-log.md) the new
                # file's directory entry is not guaranteed to survive a power loss. Closing
                # that needs a dir-fsync on both paths, which is a POSIX-only change to the
                # shared write path and is not this epic's.
                with log_path.open("a", encoding="utf-8", newline="") as f:
                    f.write(log_add)
                    f.flush()
                    os.fsync(f.fileno())
            _locked_write(lock, registry_path, read, new_text)
            # INVARIANT: delete only AFTER the inlined content is durably in the log AND the
            # registry that drops the note is written. The whole ordering exists so a crash
            # costs a duplicate rather than a loss: a delete before the log's fsync turns a
            # crash into permanent data loss (file gone, content only in a page cache), and a
            # delete before the registry write leaves a note still on the LIVE surface
            # pointing at a file that no longer exists. Every failure between these steps is
            # therefore a re-cullable duplicate, never a dangling pointer.
            #
            # An undeletable file is degraded past, not raised on — the content is already
            # archived — but it is RECORDED below, because nothing else will ever name it
            # again: no sweep walks this directory, only pointers reach it.
            deleted = 0
            for path in collected:
                try:
                    # missing_ok: another loop removing it between the read and here produced
                    # exactly the end state this wants. Telling the seat to hand-remove a file
                    # that is already gone is worse than silence.
                    path.unlink(missing_ok=True)
                    deleted += 1
                except OSError as exc:
                    print(
                        f"cull: collected {path.name} into {log_path.name} but could not "
                        f"delete it ({exc}); remove it by hand",
                        file=sys.stderr,
                    )
                    stranded.append((path.name, f"could not delete ({_one_line(exc)})"))
            # The count is what the bucket actually LOST. Counting what was collected reports
            # a shrink that did not happen on exactly the sweep the seat needs to know about.
            counts["overflow files"] = deleted
            if stranded:
                # A durable record, because stderr is invisible in an unattended firing and
                # this file is now unreachable by any future sweep. Best-effort: the cull has
                # already succeeded by this point and must not fail over its own footnote.
                marks = "".join(
                    f"<!-- stranded {NOTE_OVERFLOW_DIR}/{name}: {why} -->\n"
                    for name, why in stranded
                )
                try:
                    with log_path.open("a", encoding="utf-8", newline="") as f:
                        f.write(marks)
                except OSError as exc:
                    print(f"cull: could not record stranded files ({exc})", file=sys.stderr)
            # Singularise, so a one-item sweep does not report "1 notes" in the audit line.
            report = ", ".join(
                f"{n} {name if n != 1 else name.rstrip('s')}" for name, n in counts.items()
            )
            print(
                f"cull: {report} -> {log_path.name} "
                f"({len(text.encode('utf-8'))} -> {len(new_text.encode('utf-8'))} bytes)"
            )
            return 0
        edit_fn, needs_log = _MUTATORS[a.command]
        with _steal_note_survives_refusal(lock, registry_path, read, text):
            if needs_log:
                log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                new_text = edit_fn(text, a, log_text)
            else:
                new_text = edit_fn(text, a)
        # The single choke point every mediated verb passes, so any write by the human's seat
        # is also its proof of life (`_stamp_exempt_heartbeat`). Same locked write.
        if a.command not in _SELF_STAMPING_VERBS:
            new_text = _stamp_exempt_heartbeat(new_text, getattr(a, "actor", ""))
        _locked_write(lock, registry_path, read, new_text)
        # AFTER the write, never before it: announcing an expiry that a failed write then did
        # not perform is exactly the false report the `expired:` line was added to prevent.
        if a.command == "publish":
            # The caller cannot use an id it was never told, and lead-dev keys its worktree,
            # branch, Notes and Ntfy lines on this string. stdout rather than stderr: it is the
            # verb's RESULT, not a diagnostic. After the write for the same reason the expiry
            # line below is -- announcing an id a failed write never recorded is a false report.
            print(f"published: {_cmd_publish.last_id}")
            title_warning = getattr(_cmd_publish, "last_title_warning", None)
            if title_warning:
                print(title_warning, file=sys.stderr)
        # Same contract for the other two allocating verbs (gh#649). The id is taken under the
        # lock by a max-scan, so the caller cannot predict it -- and the next documented step
        # (`release --waiting-on`, `status --waiting-on`) needs it. Guessing it off the board
        # races the lane's own concurrent asks.
        if a.command == "ask":
            print(f"asked: {_cmd_ask.last_id}")
        if a.command == "raise-blocker":
            print(f"raised: {_cmd_raise_blocker.last_id}")
        if a.command == "note" and getattr(a, "strike", False):
            print(f"struck: {_strike_note.last_struck}")
        if a.command == "expire-directive":
            target = getattr(_cmd_expire_directive, "last_expired", None)
            if target:
                print(f"expired: {target}", file=sys.stderr)
        if a.command == "directive":
            # WARNS, never refuses: rc stays 0 and the Directive is on the board. Refusing the
            # one command a human uses to repair a wedged board would be a second wedge inside
            # the repair. stderr, like every other diagnostic here.
            warning = getattr(_cmd_directive, "last_containment_warning", None)
            if warning:
                print(warning, file=sys.stderr)
        # Decide UNDER the lock (the comparison needs the pre-edit snapshot), but SEND
        # outside it — a wedged ntfy host must never stall another loop's registry write
        # behind a socket timeout.
        # None = stay silent; a string = the status the row held before this flip, which
        # the ping uses to say WHICH transition happened. `is not None` matters: an empty
        # prior status would be falsy and would silently suppress a real ping.
        ping_prior_status = (
            _halted_transition(text, a) if a.command == "status" else None
        )
        return 0
    finally:
        lock.release()
        if ping_prior_status is not None:
            # AFTER the write AND after the release: the human is told a loop stopped
            # only once the row that says so is durable, and no other loop waits on it.
            _send_halted_ping(a.actor, a.work or "-", a.waiting_on or "-", ping_prior_status)


# --------------------------------------------------------------------------- #
# Argument parser
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m agentflow.registry",
        description="Mediated Fleet Registry write path (docs/REGISTRY.md). "
        "Agents never hand-edit the registry; every write goes through this CLI.",
    )
    # Path/lock options attach to every subcommand so they may follow it naturally:
    #   registry status --actor X --file Y --lock-dir Z
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--file", help="explicit registry.md path (wins over env/--project)")
    common.add_argument("--project", help="project routing key (derives the board via config); the edit-lock "
                        "is named by the resolved board's own project directory, never by this flag")
    common.add_argument("--lock-dir", help="edit-lock directory (else config env lock_dir)")
    common.add_argument("--timeout", type=float, default=DEFAULT_ACQUIRE_TIMEOUT, help="edit-lock acquire timeout (s)")
    sub = p.add_subparsers(dest="command", required=True)

    def actor(sp):
        sp.add_argument("--actor", required=True, help="the acting fleet loop (ownership is enforced)")

    sp = sub.add_parser("status", parents=[common], help="rewrite ONLY the actor's Agents row (heartbeat from the clock)")
    actor(sp)
    # NOT required at parse time: --heartbeat-only is the one form that omits it. A plain
    # `status` with neither is still refused, loudly, in _cmd_status.
    sp.add_argument("--state", choices=AGENT_STATES)
    sp.add_argument("--heartbeat-only", dest="heartbeat_only", action="store_true",
                    help="refresh ONLY the heartbeat, preserving state/work/phase/eta/waiting_on "
                         "as the row already carries them (for the 30-min ceiling during long work)")
    sp.add_argument("--work", default="-", help="the E-NNNN-<name> id you are on, or - for none")
    sp.add_argument("--phase", default="-",
                    help="what is running now, one short line (capped at 240 UTF-8 bytes)")
    sp.add_argument("--eta", default="-",
                    help="your next wake as ISO-8601 (e.g. 2026-01-01T12:30Z); required unless "
                         "--state halted, because the missed-wake alarm reads this cell alone; "
                         f"at most {ETA_HORIZON_HOURS:g}h from now (eta_too_far) for every "
                         "seat except dev-manager")
    sp.add_argument("--waiting-on", dest="waiting_on", default="-",
                    help="required for --state waiting|halted: a live Q-/B- id (use the id "
                         "ask / raise-blocker printed)")

    sp = sub.add_parser("claim", parents=[common], help="claim a ready Queue row from the actor's own lane")
    actor(sp)
    sp.add_argument("--id", required=True)

    sp = sub.add_parser("release", parents=[common], help="release a claimed Queue row to a new state")
    actor(sp)
    sp.add_argument("--id", required=True)
    sp.add_argument("--state", required=True, choices=RELEASE_STATES)
    sp.add_argument("--waiting-on", dest="waiting_on", default="-",
                    help="for --state parked: a live Q-/B- id (use the id ask / raise-blocker printed)")

    sp = sub.add_parser("publish", parents=[common], help="lead-dev: publish a work item into a lane")
    actor(sp)
    # ACCEPTED SO IT CAN BE REFUSED WITH A SENTENCE. Deleting the flag outright would also stop a
    # typed id, but argparse's "unrecognized arguments: --id" tells lead-dev nothing about what to
    # pass instead -- and lead-dev is a prose program whose next move comes from the refusal text.
    # `_cmd_publish` raises `queue_publish_allocates_id`, which names --slug.
    sp.add_argument("--id", help="REFUSED (E-0260): publish allocates the id -- pass --slug")
    sp.add_argument("--slug", required=True,
                    help="the spec slug; the id's name half is its first four hyphen-tokens")
    sp.add_argument("--title", required=True,
                    help="the work_item cell -- slug plus the priority suffix (pNNN), which "
                         "rides HERE and never inside the id")
    sp.add_argument("--lane", required=True, choices=LANES)

    sp = sub.add_parser(
        "relane", parents=[common],
        help="lead-dev: move an unclaimed, unstarted Queue row to another lane",
    )
    actor(sp)
    sp.add_argument("--id", required=True)
    sp.add_argument("--lane", required=True, choices=LANES, help="the lane to move it INTO")
    sp.add_argument(
        "--reason", required=True,
        help="why it is moving -- recorded on the row, because the next reader of this board is "
             "the lane whose work just changed hands",
    )

    sp = sub.add_parser(
        "withdraw", parents=[common],
        help="lead-dev: retract an unclaimed, unstarted Queue row (marks it withdrawn, not done)",
    )
    actor(sp)
    sp.add_argument("--id", required=True)
    sp.add_argument("--reason", required=True, help="why it is being retracted -- recorded on the row")

    sp = sub.add_parser("ask", parents=[common],
                        help="append a Question (requires --default; advisory also requires --assumption)")
    actor(sp)
    sp.add_argument("--urgency", required=True, choices=("halted", "waiting", "advisory"))
    sp.add_argument("--work", default="-")
    sp.add_argument("--question", required=True)
    sp.add_argument("--default", required=True)
    sp.add_argument("--assumption", default="-")

    sp = sub.add_parser("answer", parents=[common], help="steward or dev-manager: answer a question (open -> answered)")
    actor(sp)
    sp.add_argument("--id", required=True)
    sp.add_argument("--answer", required=True)
    sp.add_argument("--by", required=True, choices=ANSWERED_BY)

    sp = sub.add_parser("apply", parents=[common], help="raiser: flip answered -> applied")
    actor(sp)
    sp.add_argument("--id", required=True)

    sp = sub.add_parser("retract", parents=[common],
                        help="raiser: take an OPEN question off the human's queue, saying why "
                             "(terminal, and NOT answered - nothing was decided)")
    actor(sp)
    sp.add_argument("--id", required=True)
    sp.add_argument("--reason", required=True,
                    help="what collapsed - the premise that turned out false, or why the "
                         "question should never have been asked")

    sp = sub.add_parser("raise-blocker", parents=[common], help="append a Blocker row")
    actor(sp)
    sp.add_argument("--work", default="-")
    sp.add_argument("--description", required=True)
    sp.add_argument("--owner", default="-")

    sp = sub.add_parser("resolve-blocker", parents=[common], help="owner: resolve a blocker")
    actor(sp)
    sp.add_argument("--id", required=True)

    sp = sub.add_parser("assign-blocker", parents=[common], help="steward or dev-manager: assign blocker ownership")
    actor(sp)
    sp.add_argument("--id", required=True)
    sp.add_argument("--owner", required=True)

    sp = sub.add_parser("directive", parents=[common], help="dev-manager: write a standing Directive")
    actor(sp)
    sp.add_argument("--text", required=True)

    sp = sub.add_parser("expire-directive", parents=[common], help="dev-manager: mark a Directive expired")
    actor(sp)
    sp.add_argument("--match", help="substring that must match EXACTLY ONE live Directive (preferred: "
                                    "position is not an identity)")
    sp.add_argument("--index", type=int, help="1-based index among LIVE directives. Re-bases as rows "
                                              "expire, so a value read once and used twice targets two "
                                              "different Directives -- prefer --match")

    sp = sub.add_parser("cull", parents=[common], help="steward: move closed entries to registry-log.md")
    actor(sp)

    def ack_flag(sp):
        sp.add_argument(
            "--ack-directive", dest="ack_directive", action="append", metavar="SUBSTR",
            help="acknowledge one ACTIVE Directive by a substring of its TEXT (required once "
                 "per Directive; must match exactly one; the leading [<ISO>] stamp is CLI "
                 "metadata and is not searched)",
        )

    sp = sub.add_parser("deploy-lock", parents=[common],
                        help="take|release the deploy lock (45-min + dead-heartbeat stale rule), "
                             "or assert you hold it (read-only, lock-free; exit 5 if you do not)")
    sp.add_argument("op", choices=("take", "release", "assert"))
    actor(sp)
    # `--ack-directive` is parsed for all three ops but only consulted by `take`: `assert`
    # writes nothing, so it has nothing to acknowledge, and `release` hands the lock back.
    ack_flag(sp)
    # Same shape, same reason: parsed for all three ops, consulted only by `take`. Default OFF
    # — the break is a judgement the CALLER makes, and a flag that defaults on is not a
    # judgement (gh#232).
    sp.add_argument(
        "--break-stale", dest="break_stale", action="store_true",
        help="take: break a STALE hold held by another lane (>45 min AND the holder's Agents "
             "heartbeat dead) and route the fleet note in your name. Without it a stale hold "
             "is refused `deploy_stale_unbroken` and nothing is written. Ignored when you "
             "already hold the lock — re-taking your own hold is not a break",
    )

    # `deploy-take`/`deploy-release` stay INTERNAL dispatch keys with no argparse surface of
    # their own: every documented call site (docs/REGISTRY.md, every builder skill) spells
    # this `deploy-lock take`. A second public spelling for the one command that gates every
    # merge is how prose and code drift apart.

    sub.add_parser("init", parents=[common], help="create the registry from templates/registry.md if missing")

    sp = sub.add_parser("read", parents=[common],
                        help="lock-free full dump (+ --answered-for / --notes-for / --claimable "
                             "/ --directives / --open-questions / --stale)")
    sp.add_argument("--answered-for", dest="answered_for")
    sp.add_argument("--notes-for", dest="notes_for", metavar="ACTOR",
                    help="live Queue Notes routed to ACTOR (not fleet broadcasts), one per line, "
                         "with an explicit all-clear line when there are none")
    sp.add_argument("--claimable")
    sp.add_argument("--directives", action="store_true")
    sp.add_argument("--capabilities", action="store_true",
                    help="print the actor capability map (which loop may run which verb) and "
                         "exit — resolves and reads no registry, because a spec is what it is "
                         "checked against")
    sp.add_argument("--scope-item", dest="scope_item", action="append", metavar="TEXT",
                    help="with --capabilities: match one spec scope item's prose against the map "
                         "and report the verb + capable actor (repeatable; reports, never blocks)")
    sp.add_argument("--open-questions", dest="open_questions", action="store_true",
                    help="open questions in triage order (halted > waiting > advisory, then oldest first)")
    sp.add_argument("--stale", action="store_true",
                    help="Agents rows stale on heartbeat AND whose eta is past or absent (report only, never reaps)")
    # default None, resolved in the branch: argparse cannot otherwise tell "not passed" from
    # "passed the default", and a tuning flag paired with the WRONG read has to be refusable.
    sp.add_argument("--threshold-hours", dest="threshold_hours", type=float, default=None,
                    help=f"--stale heartbeat age threshold in hours (default {DEFAULT_STALE_HOURS:g})")
    sp.add_argument("--stranded", dest="stranded", action="store_true",
                    help="Agents rows whose OBSERVED status is halted, waiting past --strand-hours "
                         "on an open question (report only, never reaps, never relaunches)")
    sp.add_argument("--strand-hours", dest="strand_hours", type=float, default=None,
                    help=f"--stranded age threshold in hours (default {STRAND_THRESHOLD_HOURS:g})")
    sp.add_argument("--missed-wakes", dest="missed_wakes", action="store_true",
                    help="Agents rows past their OWN declared eta: MISSED WAKE (no heartbeat "
                         f"since the eta) and LAPSED ETA (still beating, eta unadvanced for "
                         f"{LAPSED_ETA_GRACE_MIN:g}m) "
                         "(report only, never reaps, never relaunches)")
    sp.add_argument("--grace-minutes", dest="grace_minutes", type=float, default=None,
                    help=f"--missed-wakes grace past eta in minutes (default {MISSED_WAKE_GRACE_MIN:g})")

    sp = sub.add_parser("preflight", parents=[common],
                        help="read-only, lock-free: list every ACTIVE Directive before a consequential action")
    actor(sp)
    sp.add_argument("--action", required=True, choices=PREFLIGHT_ACTIONS)

    sp = sub.add_parser("note", parents=[common], help="append a routed coordination Note, or --strike --match one")
    actor(sp)
    sp.add_argument("--to", help="routing target loop (default: fleet-wide)")
    sp.add_argument("--text", help="the note body (append mode)")
    sp.add_argument("--strike", action="store_true", help="mark a matching note consumed (strike-through)")
    sp.add_argument("--match", help="substring identifying the note to strike")

    sp = sub.add_parser("reap", parents=[common], help="steward: reap a dead loop (idle its row, return its claims, clear its deploy lock)")
    actor(sp)
    sp.add_argument("--agent", required=True, help="the dead loop to reap")
    sp.add_argument("--override-stand-down", dest="override_stand_down", action="store_true",
                    help="reap a stood-down lane anyway (refused without it: reap_stood_down); "
                         "only once dev-manager has decided the lane is not coming back")
    sp.add_argument("--force", action="store_true",
                    help="reap a row `read --stale` does not list (refused without it: "
                         "reap_not_stale); requires --reason; only once a human confirmed the lane is gone")
    sp.add_argument("--reason", help=f"why the reap was forced (<= {REAP_REASON_MAX_BYTES} bytes); "
                                     "written into the reap Note")

    sp = sub.add_parser(
        "stand-down",
        parents=[common],
        help="steward: mark a lane deliberately stopped so the missed-wake alarm excludes it",
    )
    actor(sp)
    sp.add_argument("--agent", required=True, help="the lane a human stopped")
    sp.add_argument(
        "--reason",
        required=True,
        help="WHY it was stopped -- written into the row, because an unexplained exclusion is "
             "indistinguishable from the dead lane the alarm exists to catch",
    )

    sp = sub.add_parser(
        "repair",
        parents=[common],
        help="steward: replace one STRUCTURALLY malformed line in a section (exact match). "
             "Refuses (exit 2, nothing written) a result that does not parse, that adds any "
             "`validate` violation the board did not already have, or that leaves the replaced "
             "line's own violation in place (repair_fails_validate); refuses any repair that "
             "CHANGES a human-decision field (" + "|".join(_Q_PROTECTED_FIELDS) + "), a "
             "raiser-owned field (" + "|".join(_Q_RAISER_FIELDS) + ") or the "
             "parsed decision record by any route, or the heading skeleton; refuses rewriting a "
             "Queue row `validate` already accepts unless its id is duplicated, or any cell "
             "`validate` did not flag on a Queue row it rejects (repair_queue_owned), and the "
             "same for Blockers (repair_blocker_owned) and Agents (repair_agents_owned) rows, "
             "keyed by id or agent; a flagged owner-held cell (Queue claimed_by/waiting_on, "
             "Blockers owner/state, Agents status) may only be cleared to its neutral value, "
             "and a Queue row that clearing would strand takes exactly what reap or "
             "`release --state building` writes. "
             "Leaves an audit "
             "Note on success",
    )
    actor(sp)
    sp.add_argument("--section", required=True, choices=SECTION_NAMES)
    sp.add_argument("--old-line", dest="old_line", required=True, help="the exact current line to replace")
    sp.add_argument("--new-line", dest="new_line", required=True, help="the corrected single line")

    sub.add_parser("validate", parents=[common], help="lock-free: assert the registry is schema-valid + consistent (exit 2 if not)")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    parser = _build_parser()
    a = parser.parse_args(argv)
    try:
        if a.command == "init":
            if not (getattr(a, "file", None) or os.environ.get("AGENTFLOW_REGISTRY") or getattr(a, "project", None)):
                raise RegistryError("file_missing", "init needs --project (or --file / $AGENTFLOW_REGISTRY)")
            registry_path = _resolve_registry_path(a)
            # Derived before anything is created: a board no write could ever lock is refused
            # here rather than made and then refused on its first write.
            lock_name = _edit_lock_name(registry_path)
            return _do_init(registry_path, a.project or registry_path.parent.parent.name, lock_name)
        if a.command == "read":
            if getattr(a, "capabilities", False):
                return _do_capabilities(a)
            registry_path = _resolve_registry_path(a)
            return _do_read(registry_path, a)
        if a.command == "validate":
            return _do_validate(_resolve_registry_path(a))
        if a.command == "preflight":
            return _do_preflight(_resolve_registry_path(a), a)
        if a.command == "deploy-lock":
            if a.op == "assert":
                # Read-only sibling of read/preflight/validate — never `_run_mutation`,
                # which would take the edit-lock and rewrite the file this verb reports on.
                return _do_deploy_assert(_resolve_registry_path(a), a)
            a.command = f"deploy-{a.op}"
            return _run_mutation(a, a.timeout)
        return _run_mutation(a, a.timeout)
    except atomic_io.ReplaceRetryExhausted as e:
        # gh#57: an exhausted replace retry, and now ONLY that. Exit 1 deliberately, NOT a new
        # code — 2/3/4/5 each name a decision this CLI made, and a caller chaining commands
        # keys on them. This is the machine saying no, not the contract saying no. What
        # changes is that it is now legible: a raw traceback told a lane nothing about whether
        # its write landed, and the answer — it did not — is the only thing worth reading.
        #
        # This clause MUST precede the bare `PermissionError` below: the exhausted-retry type
        # subclasses it, so the general handler would otherwise swallow the specific case and
        # restore exactly the misattribution gh#138 is about.
        print(
            f"registry write did NOT land: the file was held by another process for the "
            f"whole retry window ({atomic_io.REPLACE_RETRY_TIMEOUT:g}s). Nothing was written; "
            f"re-run this command. Underlying: {e}",
            file=sys.stderr,
        )
        return 1
    except PermissionError as e:
        # gh#138: any OTHER denial. The clause above used to catch these too, and told the
        # caller a story about a retry window that had not happened, ending in "re-run this
        # command" — which for an unattended lane is an instruction to loop forever on a
        # condition no retry can clear. The live cases were both `registry-log.md`: the log
        # read that `ask` and `raise-blocker` do under the lock, and the append itself.
        #
        # What this says instead is only what is known: which path was denied, and that the
        # registry was not modified — true because every path that reaches here fails before
        # or instead of the registry write, never after it.
        denied = getattr(e, "filename", None) or "a file this command needed"
        print(
            f"permission denied: {denied}. The registry was NOT modified. This is not a "
            f"transient — re-running will fail the same way until the permissions on that "
            f"path are fixed. Underlying: {e}",
            file=sys.stderr,
        )
        return 1
    except RegistryError as e:
        print(str(e), file=sys.stderr)
        return 2
    except EditLockStolenError as e:
        print(str(e), file=sys.stderr)
        return 3
    except LockHeldError as e:
        print(f"lock-held: {e}", file=sys.stderr)
        return 3
    except DirectiveAckError as e:
        # Distinct from 2 (ownership/schema) and 3 (lock held): the action is permitted,
        # the standing instruction is simply unread.
        print(str(e), file=sys.stderr)
        return 4
    except DeployAssertError as e:
        # Distinct from 2/3/4: the command was well-formed and the answer is no. A caller
        # chaining `assert && merge` reads 5 as "another lane owns the merge window" and 2
        # as "I invoked this wrong" — the same recovery for both would be wrong for one.
        print(str(e), file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
