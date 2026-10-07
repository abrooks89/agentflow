# Fleet Registry — the single coordination surface

All seven agentflow loops — **lead-dev**, **jr-dev-1**, **jr-dev-2**, **jr-dev-3**, **auditor**,
**steward**, **dev-manager** — coordinate through **one** stateful file per project:

```
<data_root>/projects/<name>/status/registry.md      (live)
<data_root>/projects/<name>/status/registry-log.md  (append-only cull target; never read in the hot path)
<data_root>/projects/<name>/status/notes/           (the analysis behind a Note; a Note points, never carries.
                                                     Bounded by the same sweep: a file is inlined into the
                                                     culled block and deleted when its Note is culled)
```

Every loop **reads the whole registry at every state-bearing firing** (reads are lock-free) and
**writes only the rows it owns** — never by hand, always through the mediated write CLI
**`python -m agentflow.registry`**, which holds the mkdir edit-lock, re-reads under it, and
refuses any ownership or schema violation (see [Writing — the mediated CLI](#writing--the-mediated-cli)).
**dev-manager is the sole human surface, and the steward is the janitor** (E-0220 split the two):
dev-manager alone writes Directives and alone relays a human answer (`--by human`); the two
manager roles *share* answering questions and assigning blocker ownership; and the **steward
alone** repairs malformed rows and culls closed entries to `registry-log.md`. Builders never talk
to the human. Every clause of that sentence is one row of `ACTOR_CAPABILITY` — `registry read
--capabilities` prints it, and the map is what the CLI enforces. **Ntfy is notification-only fleet-wide** —
it never carries a question expecting a reply.

The one-sentence architecture: **builders build; the manager talks to the human.**

> **History.** This registry replaces an earlier four-section *fleet board* — Active, Queues,
> Blockers, Auditor. That board is not part of this repository, and neither is the
> `docs/FLEET-BOARD.md` that specified it. **Nor does its shape survive anywhere in the tree
> any more** — this note used to say the acceptance harness still carried it, naming a
> `_BOARD_SKELETON` fixture and a `_render_board` in
> `harness/auditor-scenario/auditor_scenario.py`. Neither symbol exists anywhere in the
> repository (gh#205, re-measured at `529105b`): the module is real, the symbols are not, and
> E-0370 refreshed the auditor ship gate onto the shipped registry without updating this
> paragraph. The prose is what was wrong, so the prose is what changed — no fixture was
> re-added to make it true.
> What earned its place carries forward, and is specified below: the mkdir edit-lock (now with
> an explicit 5-minute stale rule), the deploy-lock semantics (the 45-minute stale rule), and
> disjoint per-lane work publishing — lead-dev computes file/seam disjointness at publish time.
> The four sections became five. **Dropped:** the board's Auditor section — the auditor now
> keeps its own `## Agents` row like any loop and writes its pass record to
> `status/audit-ledger.md` + `status/audits/` (see `docs/DATA-LAYOUT.md`), so the shared surface
> carries no section only one loop uses. **Added:** `## Questions`, which brings agent-to-human
> asks onto the shared surface (they previously lived in per-loop files no other loop read), and
> `## Directives`, the human's standing-instruction channel.

---

## The file

Five sections, always in this order. The template (`templates/registry.md`) is seeded by the
installer. A write against a missing registry does NOT re-create it — it refuses `file_missing`.
Create it with `init` and never by hand.

### `## Agents` — one live row per loop

| agent | status | work | phase | heartbeat | eta | waiting_on |
|---|---|---|---|---|---|---|
| lead-dev | building | E-0042-staging-deploy-gate | implement | 2026-08-05T14:10Z | 2026-08-05T16:00Z | - |
| jr-dev-1 | waiting | E-0038-export-pipeline-artifacts | parked | 2026-08-05T13:55Z | - | Q-0007 |
| jr-dev-2 | idle | - | - | 2026-08-05T13:40Z | - | - |
| jr-dev-3 | idle | - | - | 2026-08-05T13:45Z | - | - |
| auditor | idle | - | - | 2026-08-05T09:00Z | - | - |
| steward | idle | - | - | 2026-08-05T08:30Z | - | - |
| dev-manager | idle | - | - | 2026-08-05T12:00Z | - | - |

- Each agent writes **only its own row** (via `registry status --actor <you> --state <state>
  --eta <ISO>`, where `halted` may omit `--eta`, or `--heartbeat-only` alone): at wake, at every phase boundary (heartbeat + eta
  refresh), and at sleep.
- `status` enum: `building | waiting | halted | idle`.
  - `waiting` — has work parked on a question/blocker but can still claim other work.
  - `halted` — **blocked on an answer this lane cannot get for itself**; `waiting_on` names the
    live `Q-`/`B-` id it is blocked on. Halted agents keep firing on schedule and re-reading this
    file — that is how answers reach them.

    **`halted` NEVER means "has no work yet" — for ANY lane, with no exceptions.** An empty
    `jr-dev-1`/`jr-dev-2`/`jr-dev-3` lane, an empty audit window, and an empty backlog on `lead-dev` all
    schedule the next check and keep checking — indefinitely, with no counter and no halt. This is
    the definition that makes the `waiting_on` requirement above coherent rather
    than awkward: a lane with nothing to do has no `Q-`/`B-` id to point at, so on 2026-09-01
    jr-dev-1 recorded that "an EMPTY-LANE halt has no honest row [...] so the halt must invent a
    referent". Under this rule that case cannot arise. Terminal halting on empty input was
    removed because it needed a human to notice AND the counter that triggered it survived the
    relaunch, so the human's recovery action did not work — three lanes re-halted in one night
    while 13 triaged bugs and 4 approved epics sat waiting.
- `waiting_on` MUST reference a live `Q-`/`B-` id when status is `waiting`/`halted` — the same
  rule the `## Queue` section states for a `parked` row. **Where it is enforced:** at write time,
  `status --state waiting|halted` refuses a missing id or one that is not live (`agent_waiting_on`,
  exit 2, row unchanged, via `_require_live_waiting_on`). A referent that dies AFTER the write (the
  question applied or retracted, the blocker resolved, the row removed) is not a `validate`
  failure on an Agents row: every lane runs `validate` fail-closed at wake, so one stranded lane
  would stop the fleet. For a `halted` row it is reported instead, by `read --stranded` (as
  unjudgeable, with its reason) and by the fleet report's BLOCKED ON YOU section, which names the
  lane, its `waiting_on`, the reason and the fix. A `waiting` row whose referent dies is not
  reported by either surface.
- **`phase` names WHAT IS RUNNING NOW, and `status` / `stand-down` cap it at 240 UTF-8 bytes**
  (`phase_too_long`, exit 2). It is capped **below** the 300-byte Note cap on purpose, and that
  ordering is the rule rather than the number.

  **Refused BEFORE the edit lock is taken**, in the same place and for the same reason as the
  Note cap: the size is decidable from the arguments alone, and a refusal under the lock could
  first force-break another lane's stale lock — appending the "broke stale registry edit-lock"
  audit line into the in-memory text — and then refuse, discarding the only record that the
  steal happened. Refusing first means a rejected `status` leaves no trace of any kind.
  Since gh#656 the same holds for the argv-only refusals of `status`, `ask`, `retract`,
  `relane`, `withdraw`, `reap`, `claim`, `release`, `note`, `stand-down` and `repair`
  (`eta_required`, `heartbeat_only_exclusive`, `queue_revise_needs_reason`, `q_needs_default`,
  `agent_waiting_on` on a `release --state parked` with no `--waiting-on`,
  `repair_blank_old_line`, `repair_boilerplate_line`, `note_strike_text`, ...): each runs before
  the lock. A refusal that needs the board (a dead `--waiting-on`, a `--strike` matching
  nothing), and any argv-only refusal of a verb not listed here, still runs under it; if the
  write had just broken a stale lock, the steal Note alone is written before the refusal is
  returned.

  **The cap is a guard on two verbs, not an invariant of the file.** `repair` is the steward's
  structural surgery and is deliberately outside it: a cell that needs repairing may be
  malformed in any way at all, including over-long, and a repair that refused to touch it would
  leave the registry unparseable. Do not read a row and infer its phase is under 240 bytes.

  **The incentives were exactly inverted before this existed.** The machine-only Notes channel
  had a loud byte cap and a refusal that teaches the move; `phase` — the *only* status text a
  human reads, in a cell of a table they scan every row of in seconds — had no bound at all, and was
  used across the live board as a diary. A field a person reads must be shorter than a field a
  program reads.

  - **What belongs in it:** the current step. "implement", "verifying against staging",
    "proj-arg alpha over injected beta; grounding E-0224".
  - **What does not:** the reasoning, the history of the wake, what you decided and why. That is
    a `note`, and the refusal names the note channel for exactly this reason — an author told
    only *"too long"* deletes the fact to make it fit, while an author told where narrative goes
    moves the narrative and leaves the cell saying what is running.
  - **`--heartbeat-only` is unaffected by the cap.** It preserves whatever the row already
    holds, so a row written before the cap existed keeps beating. The cap guards what an actor
    **writes**, never what the row already carries; the alternative would silence exactly the
    lanes whose heartbeat matters most.
  - **`stand-down` is bounded on the COMPOSED cell**, not on the bare `--reason`: the
    `stood down by steward: ` marker spends bytes too, and it is what the missed-wake alarm
    matches on. A second, looser bound on a second write path is how a capped field stops being
    capped.
- **`eta` is MANDATORY on every `status` write except `--state halted`** — the CLI refuses one
  that leaves it empty (`eta_required`). It is the lane's own declaration of when it next intends
  to wake, and it is the single field the missed-wake alarm reads. `halted` is exempt because a
  declared halt is blocked on an answer rather than on its next wake: it keeps firing on
  schedule, but no wake of its own unblocks it, and its transition already paged once. It is the
  same row the alarm excludes, so eta is required exactly where the alarm reads it.
  - **It must be the FIELD, never prose.** A next wake written as `next ~21:14Z` inside `phase`
    with `eta` set to `-` is unreadable to any watchdog, and an alarm built on parsing it would
    be a gate that passes against whatever text it happens to meet.
  - `--heartbeat-only` is unaffected **only while the row's own eta is readable**: it preserves the eta already on the row, and REFUSES (`eta_required`, exit 2) when that cell does not parse -- preserving an unreadable eta is what kept a row permanently invisible to this alarm. Send one full `status --eta` write to declare your next wake, then every re-stamp after it works.
  - **`eta` may be at most 6 hours ahead** (`ETA_HORIZON_HOURS`) — `status` refuses a later one
    before any write (`eta_too_far`, exit 2, gh#682). Every liveness detector waits for the clock
    to pass `eta`, so one far eta (a year typo) switches all of them off for a lane that still
    holds its claims. The longest documented wake fits inside the horizon: a backoff stand-down
    schedules nothing and is woken by its five-hour `--recovery-guard` cron, jittered by up to
    15 minutes, and declares the `eta=` the backoff prints (at most 5h15m out); every other wake is a `ScheduleWakeup`, which the harness clamps to one hour.
    A row that carries a far eta anyway (a hand edit) is
    reported by `read --missed-wakes` and `wake_watch` as `INVISIBLE ... (eta-too-far ...)` once
    its eta is more than the horizon past **its own heartbeat** — measured against the row, not
    the clock, so the report is deterministic.
    The same measure caps the eta `read --stale` honours (gh#707): past the horizon from its
    heartbeat the row is judged as if its eta had lapsed there, so it reaches ping-then-reap.
  - **The `dev-manager` row is exempt from the horizon** (`ETA_HORIZON_EXEMPT`): `status` accepts
    any eta on it, and `blind_rows` never reports it `eta-too-far`. It is the human's interactive
    seat, which does not self-wake and holds no claims, so its eta is the human's next sitting,
    which may be tomorrow. The horizon guards a lane that holds claims and can die; a far eta on
    a row that holds nothing switches off no detector that protects work.
  - **Every registry write the exempt seat makes refreshes its own heartbeat**, and only that
    cell: `status`, `work`, `phase`, `eta` and `waiting_on` stay as the row owner last wrote
    them, the same stamp `directive` applies. The seat runs no firing contract, so it never
    sends the `status` write that moves a loop's heartbeat; it coordinates through `note`,
    `answer`, `ask`, `apply`, `retract`, `raise-blocker`, `resolve-blocker`, `assign-blocker`
    and `expire-directive`. The stamp keys on the actor being in `ETA_HORIZON_EXEMPT`, not on a
    list of verbs, so no verb can be the one that leaves the row ageing while the seat works.
    It is skipped, never refused, when there is no row to stamp, when the row is stood down or
    reaped (a full `status` write brings such a row back), or when the row's eta does not parse,
    for the `--heartbeat-only` reason: a fresh heartbeat beside an unreadable eta makes the row
    `INVISIBLE` to the missed-wake alarm. On a fresh board the seat's first `status` write
    declares its eta, and stamps start after that. No other actor's row moves.
  - **`read --stale` never marks the exempt row `reap-eligible`, and `reap` refuses it**
    (`reap_exempt_seat`, exit 2, board unchanged, whatever `--force` or
    `--override-stand-down` says). The seat holds no claims or deploy lock, so a reap recovers
    nothing. A listed exempt row stays `flag`: the seat is away past its declared return, which
    is worth reporting, and it is not a reap candidate.
- Timestamps are read from the clock (`date -u`), never estimated — a drifting or invented
  timestamp silently converts a live lane into a "lapsed eta" false death.

### `## Queue` — claimable work, disjoint lanes

- **Deploy lock** — `holder: none|lead-dev|jr-dev-1|jr-dev-2|jr-dev-3` (any lane in `registry.BUILDERS`), `taken-at: <ISO>`. Serializes
  every merge-to-main and every live verification gate across the builders. Stale >45 min
  AND the holder's Agents heartbeat dead → the hold becomes STEALABLE, but since gh#232 it is
  never broken for you: `take` refuses `deploy_stale_unbroken` and the caller must re-run with
  `--break-stale`, so the fleet note naming the break is written by a lane that decided. A hold
  that is merely held, and not yet stale, still refuses `deploy_held` — that one you wait out.
  - **Whoever was blocked first goes first (gh#167).** The lock is mutual exclusion, not a
    queue, and `registry.py` keeps no order: a freed lock goes to whichever lane polls first, so
    a lane that waited an hour with a merge-ready PR could lose it to one whose CI was still
    running. The order is a convention every builder follows at its acquire step:
    - A lane refused `deploy_held` with a merge-ready PR writes `lock-wait since <ISO>; PR <n>
      green` into its `phase` (`--state building`; the ISO is its first refusal, to the second,
      unchanged across re-polls; every full `status` write repeats it, `--heartbeat-only` keeps
      it). The marker is in `phase` because `--state waiting` requires a live `Q-`/`B-` id and a
      lock is neither.
    - Before every `take`, a builder reads `## Agents` and yields to any live row carrying an
      earlier marker than its own (or any marker, if it has none). Live means the heartbeat is
      newer than the row's `eta`, or the `eta` is less than 30 minutes past (the missed-wake
      grace). An exact tie goes to the alphabetically earlier agent name. A marker on a row that
      fails the liveness test is ignored.
    - A lane drops its marker once it holds the lock, and as soon as its PR stops being
      merge-ready (CI red, a conflict, review changes) or it switches work; it re-stamps with a
      new ISO only when the PR is green again.
    - Enforcing FIFO in code (a `waiting_since` queue) is deliberately out of scope at three lanes.
  - **Chain the merge behind `deploy-lock assert` — never behind the take.** Taking the lock
    and merging are two commands in two tools, so a refused take stops only itself:

    ```
    python -m agentflow.registry deploy-lock assert --actor <you> --project <name> \
      && gh pr merge <n> --repo <owner>/<repo> --squash --delete-branch
    ```

    `assert` is read-only and lock-free (no edit-lock, no write, no note): exit `0` iff
    `holder` is you **and your hold is not stale**, exit `5` otherwise, naming the current
    holder, how long they have held it, and their heartbeat age — or saying the lock is
    FREE. The staleness clause is not decoration: a hold `take` would let another builder
    break is not exclusivity, and a name-only comparison would green-light both lanes at
    once. If `assert` says your own hold went stale, `deploy-lock take` again (that
    refreshes `taken-at`) and re-assert. On 2026-08-31 the take was correctly refused
    (`deploy_held`, another lane had it) and the `gh pr merge` on the next line ran anyway
    and landed on master. The `&& echo "lock taken"` that trailed the take was the trap: it
    looked like a guard and guarded the echo. Put the merge on the right-hand side of a `&&`
    whose left-hand side is `assert`, and the pair cannot half-run.

    It narrows the unguarded window; it does not close it. Between the `assert` and the end
    of the merge, a lane whose hold has since gone stale can still be broken by another
    builder — as with the lock generally, only a write-block would make that literally
    impossible. Keep the merge on the right-hand side, and keep it short.
- **Notes** — routed agent-to-agent coordination lines (`lead-dev -> jr-dev-1: ...`), struck
  through in the same pass they are acted on (`note --strike --match "<substr>"`, one live
  match per call, and an ambiguous match is refused — see the firing contract's **CONSUME
  FIRST**). Human instructions never ride
  here — see `## Directives`. **Every loop that is written to strikes, steward included** — a
  lane that only appends is an inbox the retention rule below can never reach, by design.
  - **Retention is keyed on STATE — never age, never a row count.** A struck Note is culled
    verbatim to `registry-log.md` under the same `<!-- culled <ISO> -->` marker as applied
    Questions, done Queue rows and resolved Blockers. An **unstruck Note is never culled, at
    any age**: struck is the only evidence the routed agent actually read the line, so an age
    or count rule silently drops coordination nobody consumed. The sweep's test is the strict
    full-line `~~...~~` wrapper `--strike` writes, and that strictness is load-bearing — an
    ordinary Note carries `~~` in its body ("move off ~~old~~ path"), so a loose "contains
    `~~`" test would read that as consumption and delete the Note while it was still live.
  - **Only the routed recipient strikes** (`note_not_yours`, exit 2). A Note routed to `fleet`
    is addressed to everyone, so any loop may strike it, and **dev-manager may strike anything**
    — it is the janitor of *this* surface, recorded as `janitors=("dev-manager",)` on the `note`
    row of `ACTOR_CAPABILITY`. A janitor exemption is per-surface, and this one did not travel
    with the janitorial verbs: E-0220 made `reap`, `stand-down`, `repair` and `cull` steward-only
    and left the Notes exemption exactly where it was. This is enforced, not advisory: striking is
    the only evidence a Note was read, so a mis-aimed strike hands a live instruction to `cull`,
    which deletes it before the addressee ever sees it.
  - **One Note is capped at 300 UTF-8 bytes**, refused by the CLI before any write
    (`note_too_long`, exit 2), pre-lock, in the same shape as the `inline_only` guard. The split
    that evades a byte cap is refused too, on **both** signals together: 3+ Notes on one
    `<actor> -> <recipient>` route stamped inside 15 minutes **and** more than 900 UTF-8 bytes
    between them (`note_split_burst`). The bytes half is what keeps it honest — a row count
    alone reads a busy triage pass as a split paragraph and refuses the fourth note, and in a
    headless loop a refused note is a lost message. Notes the CLI writes itself (reap, the two
    stale-lock steals) never count against an author's route. Both guards sit on the **append**
    path only; `--strike` is how the surface drains and must never be blocked by a full route
    or an over-cap `--text`.
  - **The live-note count has a threshold: 40.** Crossing it is a signal in the fleet report's
    health diff (`HEALTH_THRESHOLDS["note_backlog"]`), not merely a number rendered beside the
    struck count. It is the one health threshold that is **not 1**, and deliberately so: the
    other four count things that should never happen at all, while routing a note is ordinary
    healthy traffic, and a threshold of 1 fires on every board forever — a metric nobody reads
    rather than an alarm.

    **40 is a drain rate, not a size.** A seven-seat fleet routing roughly a note apiece per
    wake puts a day's ordinary traffic near 30, and every seat is instructed to strike what it
    consumed at its wake read. Past 40, the channel is not busy — it has stopped being drained,
    and every routed finding sitting in it is unread.

    **It is reported as a LEVEL, not only as a crossing.** The health diff labels the edge
    (`crossed` the report it goes over, `cleared` when it comes back) but emits `over` on every
    report in between. A pure edge detector is silent forever on a metric that never comes back
    down, which is what a monotonically growing note backlog is — so the threshold would have
    fired zero times on the board that motivated it.

  - **The analysis goes in a file; the Note carries the decision plus a pointer.** Write it to
    `notes/` beside `registry.md` as `<YYYY-MM-DDTHHMMZ>-<actor>-to-<recipient>-<slug>.md`, and
    reference it from the Note as `notes/<file>.md`. The reference stays relative on purpose —
    a long absolute path eats the byte budget it exists to protect.
  - **The pointer is a bare filename in that one directory** — no subdirectory, no `..`, no
    drive letter, no absolute path. `./notes/<file>.md` is the one other spelling accepted:
    `note` rewrites it to `notes/<file>.md` on the way in, because the sweep's matcher cannot
    see a leading `./`. Anything else is not a pointer the sweep recognises: it is skipped,
    silently and without deleting anything, and the file it names is left where it is.
  - **An overflow file does not outlive its Note.** When `cull` moves a struck Note to
    `registry-log.md` it reads the file that Note points at, inlines the content into the same
    culled block (delimited by `<!-- overflow notes/<file>.md -->` … `<!-- end overflow … -->`)
    and deletes the file. An orphaned overflow file is garbage by definition — the only thing
    that could reference it was the block that just moved. This is the rule that keeps `notes/`
    from becoming a second unbounded store as the price of bounding the first: **one** archive,
    `registry-log.md`, which nothing reads in the hot path. An **unstruck** Note's file is
    untouched, at any age — same STATE key as the Note itself. A pointer to a file that is not
    there (every Note written before this rule, and any Note re-culled after it) costs nothing:
    the sweep proceeds and deletes nothing.
  - **Orphaned is a property of the surface, not of the one Note being culled.** One analysis
    routed to two lanes is two Notes carrying the same pointer; striking one consumes one
    reference, not the file. The sweep collects a file only when nothing left on the surface
    names it, so the file survives until the sweep that culls its last reference. Otherwise the
    other lane wakes to a live routed Note pointing at nothing.
  - **A file the sweep could read but not delete, or could not read at all, stays on disk** —
    the Note is what must not be lost, so the sweep degrades rather than aborting. Nothing
    walks `notes/`; only pointers reach it, and the pointer just moved. So the survivor is
    recorded in `registry-log.md` as `<!-- stranded notes/<file>.md: <why> -->`, and the report
    line counts only what was actually deleted. Grep the log for `stranded` when the bucket
    looks bigger than the surface explains; those files are removed by hand.
  - **Two matchers, because the pointer is read for two jobs whose failures are opposite.**
    *Collection* — deciding a file may be deleted — is matched literally: `**notes/x.md**` is
    recognised, and `Notes/x.md` is not, because on a case-sensitive filesystem that names a
    different directory and matching it would collect the bare filename and delete a file
    inside `notes/` the Note never named. There, an unrecognised spelling costs one retained
    file and a wrong match costs the wrong file, so under-matching is the safe side.
    *Retention* — deciding a file is still pointed at — uses a deliberately looser matcher that
    needs no delimiter before the pointer, so `_notes/x.md_` and `see:notes/x.md` both protect.
    **The direction reverses here and that is the whole reason for the second matcher:** a
    spelling the strict regex misses on a LIVE note used to mean the file was deleted while
    something still pointed at it, reported as routine cleanup (gh#136). Over-matching in the
    retention half costs one retained file; under-matching costs the file itself.

| id | work item | lane | claimed_by | state | waiting_on | notes |
|---|---|---|---|---|---|---|
| E-0042-staging-deploy-gate | staging deploy gate (p120) | lead | lead-dev | building | - | |
| E-0038-export-pipeline-artifacts | export pipeline artifacts (p140) | jr-dev-1 | jr-dev-1 | parked | Q-0007 | parked on answer |
| E-0051-vocab-guard-ast-port | vocab guard AST port (p160) | jr-dev-2 | - | ready | - | |

- `id`: **`E-<NNNN>-<short-name>` — an allocated number and a name, and neither is the
  priority.** `publish` mints it; it REFUSES a `--id` and takes `--slug` instead. The number
  comes from the registry's own monotonic sequence, seeded above every `E-` id the live board
  **or** `registry-log.md` has ever carried, so a `cull` cannot reset it. The name is the first
  four hyphen-tokens of the spec slug — a pure function, so two seats derive one id. The number
  alone stays a valid grep key: `E-0042` still finds `E-0042-staging-deploy-gate`, because the
  hyphen is a word boundary.

  **Priority rides in `work item`, never in the id** (`staging deploy gate (p120)`). That is the
  point of the scheme. Rebanding a spec is a routine dev-manager act, and before this the epic's
  id WAS its priority — lead-dev typed the number at `publish` and typed the band — so a reband
  renamed the epic in every Note, worktree, branch and Ntfy line keyed on it, and two drafted
  specs in one band could not both be published.

  **Legacy bare `E-NNNN` ids stay readable forever.** Every id issued before the allocator is a bare
  number, and those numbers are in merged PR titles, in the log and in commit messages. Reads
  accept both shapes; writes only ever emit the new one. No history is rewritten. A bare legacy
  id carries no name, so surfaces that render the name from the id leave the cell blank for one
  rather than guessing.
- `lane`: `lead` (lead-dev only) | `jr-dev-1` | `jr-dev-2` | `jr-dev-3`. **lead-dev publishes and assigns
  lanes**, computing file/seam disjointness at publish time — the junior builders cannot
  compute eligibility and must not self-select. Fill the jr-dev-1 lane to ≤3 first; a
  starving lane idles (expected, not a fault).
- `state`: `ready | building | parked | review | done | withdrawn` — `done` and `withdrawn`
  rows are both culled. `withdrawn` is lead-dev retracting an unclaimed, unstarted
  publication (see `withdraw` below); it is terminal but NOT shipped, and deliberately
  distinct from `done` because `cull` moves a consumed row verbatim into the append-only
  `registry-log.md`, where a retraction filed as `done` would stand forever as a record
  that the work shipped. A `publish` of the same SLUG RECLAIMS a withdrawn row — keeping its
  number, since the epic that comes back is the same epic — so the retraction is reversible
  without a cull. (A withdrawn *legacy* row cannot be reclaimed: a bare `E-0220` says nothing
  about which slug it stood for, so re-publishing allocates a new-shape id and leaves the old
  row for the next cull.)
- `waiting_on`: a `parked` row's `waiting_on` MUST reference a live `Q-`/`B-` id — the question
  or blocker the row is parked on. `release --waiting-on` refuses an id that is not live
  (`agent_waiting_on`, exit 2, row unchanged), and so does `release --state parked` with no
  `--waiting-on` at all (gh#707, same key). `validate` reports a row whose id was later
  consumed or removed as "parked on nothing". `release --state ready` clears the cell.
- An in-flight row (`building`, `parked`, `review`) MUST carry a claimant. `claim` takes only a
  `ready` row, `release` only the claimant's own, and `reap` matches an agent, so a row in flight
  with `claimed_by` `-` (or blank) is one no verb moves. No verb writes that shape; `validate`
  reports it (`Queue[<id>]: state '<state>' with claimed_by '-' - in flight with no claimant`),
  and the steward's `repair` takes reap's write (`claimed_by` `-`, `state` `ready`, `waiting_on` `-`).
- Only epics in the **approved** dispatch state with an eligible `queue_class` enter the
  Queue. **Eligibility is CODE-enforced, not a publishing convention** —
  `agentflow.epic_survey.dispatchable()` treats `queue_class` as an **allow-list**: unset or
  `fleet-buildable` dispatch, and every other value is dropped (the known-unbuildable
  `local-tooling` / `on-server-ops` / `brainstorm-pin`, plus any out-of-vocabulary value such as
  `bug` or `feature`, which the survey also names in a warning). The same call drops any record
  whose frontmatter `project:` is set and differs from the active project, and builders survey
  with `survey_epics(..., project=<name>)` so a sibling fleet on the same `data_root` never
  enters the candidate set in the first place. **Approval is promotion**: dev-manager promoting a spec to the approved state in its
  human session IS the standing human approval — there is no separate per-epic dispatch
  confirm anywhere. A dispatch emits a notification-only Ntfy notice, and the dispatching
  builder's pre-irreversible re-read honors any `## Directives` hold.
- Claim = flip `claimed_by` + `state` under the edit-lock; only the claimant edits a claimed
  row.
- **The claim carries a BRANCH NAME, and it is `e<NNNN>-<short>`** - the row id's digits
  verbatim, lowercased, the hyphen after the `E` dropped, then a short slug. `E-0734-gh293-live-seam`
  builds on `e0734-gh293-live-seam`. This is part of the claim contract rather than a habit
  because something reads it: `agentflow.files_vs_diff.epic_touched_paths()` turns a row id into
  the paths that row's branch has actually touched, which is how a claimed row's seam is
  computed (`lead-dev` 2.6, gh#293) - the declared `## Files` is a prediction made before the
  work, and disjointness against a claimed row must be measured against the work.
  - **The digits are taken verbatim: not re-padded, not zero-stripped.** `E-0220` resolves
    `e0220-...` and never `e220-...`. That is deliberate - this repository carries
    `e0263-two-dot-range` beside `e263-land`, so normalising the padding would make questions
    ambiguous that are answerable today. A name that never carries the prefix as a whole token
    (`e220-...` for `E-0220`) is simply not this epic's: the resolver returns `None`, and the
    seam is read from the declaration.
  - **A lane namespace resolves; any other placement of the id refuses.** The match is on the
    start of the branch name's last `/` segment, so `jr2/e0734-x` is E-0734's branch exactly as
    `e0734-x` is. A name carrying the prefix as a whole token anywhere else (`fix-e0734-x`,
    `e0734-x/sub`) raises `OffConventionEpicBranch`, a subclass of `AmbiguousEpicBranch`,
    naming the branch; the repair is a rename. Given a base, a spent off-convention leftover
    (a squash-merged `fix-e0734-x`) drops out first, since its work is already in the base;
    only a live one refuses. One exception: a name whose last segment is
    another epic's own `e<NNNN>-` branch belongs to that epic, so `e0932-review-e0734-fixes`
    is E-0932's and does not refuse for E-0734.
  - **A spent branch is `None`, not `()`.** Spent means no commits off the base, or every path
    it touched already has the same content in the base (a merged branch, squash included). The
    fleet cuts the branch at claim time, before any work, so an empty diff there means "not
    started", never "touches nothing"; and a merged leftover holds nothing the base lacks.
  - **Local and remote refs both**, deduped on the branch name, so a lane building in a worktree
    off the shared checkout and a lane that only ever arrives as `origin/...` are both findable.
    Where one name exists on both sides the local ref is read, because these builders are
    worktrees off one checkout and the remote lags them by a push.
  - **Two live branches on one prefix raise rather than resolve.** Guessing would produce a seam
    that looks measured and is read off the wrong branch - worse than the declared-list fallback
    it would be mistaken for. Spent branches drop out before that count, so a merged leftover
    beside the live branch resolves to the live one - until the base changes one of the
    leftover's paths again, since spent is judged tip to tip. Delete merged leftover branches.

**Resuming a row you parked is ONE command, and that matters.** `release --state building` keeps
`claimed_by` exactly as it is, so the row never passes through an unclaimed state. Before this
existed the only path was `release --state ready` then `claim`, and between those two calls -- each
of which takes the edit-lock, writes and releases -- the row was genuinely unclaimed and `ready`
**on disk**.

That is precisely the shape `_publisher_row` admits, and it guards **both** of lead-dev's revision
verbs, so both reach into the window:

- **`withdraw`** retracts a row whose builder is mid-build with a worktree and commits behind it.
- **`relane`** rewrites the row's lane, after which the other lane's builder claims it
  legitimately -- and the original builder's own step-2 `claim` is then refused for lane
  discipline. This is the WORSE of the two: `withdraw` at least leaves the row visible carrying
  its reason, while relane-then-claim leaves the board reading as ordinary healthy work with two
  lanes owning one seam and no signal that it happened.

A `reap` landing in the window finds no claim to return, and a liveness read sees a builder
holding nothing.

(An earlier version of this section claimed the second-builder hazard was unreachable, reasoning
that each builder claims from exactly one lane. That reasoning is true of `claim` against a FIXED
lane, and `relane` is the verb that unfixes it -- the conclusion was false and adversarial review
caught it.)

`--waiting-on` is refused on a resume rather than ignored: a row reading `building` while naming a
blocking question is a contradiction a later reader cannot resolve.

#### What `reap` may not touch — `claimed_by` on a terminal row is PROVENANCE, not a claim

`reap` returns a dead lane's work to the pool. It decides what to return on **`state`, never on
`claimed_by` alone** — and on an **allowlist**, so a `state` cell it does not recognise is left
alone rather than swept into the return branch:

| the dead lane's row is | `reap` | why |
|---|---|---|
| `building` · `parked` · `review` | **returns it**: `claimed_by` and `waiting_on` cleared, `state` → `ready` | all in-flight or resumable. A `parked` row especially — it becomes claimable the moment its question is answered, so leaving it behind a dead lane is the exact stall `reap` exists to remove |
| `done` · `withdrawn` | **changes nothing at all** — not `state`, and **not `claimed_by`** | terminal: nothing will ever build in that seam again |
| anything else (`Done`, an empty cell, a typo) | **changes nothing, and says so** — counted as `skipped` in the reap Note | `state` is free text on every mutation path; only `validate` holds it to the enum, and `reap` is the verb you run on a board already known to be broken |

The distinction is what `claimed_by` MEANS in each case. On a live row it is a claim — somebody is
holding this work. On a terminal row the row is finished, so nobody is holding anything and the
cell is a **record of who shipped it** (or, for `withdrawn`, who last held it before it was
retracted).

Reaping on `claimed_by` alone read that record as a claim and rewrote merged epics back to
`ready`. **The harm is the claim path, not the epic survey.** A `ready` row with an empty
`claimed_by` sitting in lane `jr-dev-1` is exactly what `read --claimable jr-dev-1` lists and what
`claim` accepts — those check the row's own `state` and `claimed_by`, and the reap had just
written both — so the builder owning that lane takes the row and rebuilds work already on main. (The
`selectable()` survey never enters into it: it reads epic **spec files on disk**, not Queue rows.
An earlier version of this section named it as the mechanism and was wrong.) Seen live
2026-08-31 returning two shipped epics at once; exposure scales with lane lifetime, since a
long-lived seat accumulates `done` rows and one reap returns all of them together.

**Clearing `claimed_by` on a terminal row is the same defect wearing the other face**, which is why
the table says *nothing at all* rather than *nothing but the claim*: the provenance being misread
is the only surviving record of who did the work.

**The judgement and the write must land on the same line.** `reap` walks the raw `## Queue` lines
rather than the parsed model, because the two disagree on a **duplicated id**: `parse_registry`
keys a dict on the id and is last-wins, while the text-surgical writer rewrites the **first**
matching line. Deciding from one and writing through the other judges one row and edits a
different one, and both orderings are harmful — the `done`-first board resurrects the shipped row
and overwrites its cells from the duplicate while the real claim stays held; the `building`-first
board strands a live claim forever. `validate` reports a duplicated table key (and a duplicated
`Q-` id) as a violation, so every loop's fail-closed wake check sees either board; `claim`,
`release` and the publisher verbs `relane` and `withdraw` refuse a duplicated id as
`id_duplicate` before judging it.

The reap Note counts the outcomes separately — `returned N claim(s)[, preserved M terminal
row(s)][, skipped K unrecognized state(s)]` — with the optional clauses omitted when zero. The old
line said `returned N claim(s)` with `N` counting every row it touched, terminal ones included;
that count was true of what the code did and false about what it meant, so the board read as
correct while shipped work sat back in the pool. A reap happens because a lane died, which is
exactly when nobody is watching that lane closely.


### `## Questions` — the only path from any agent to the human

```markdown
### Q-0007
- state: open                # open -> answered -> applied, OR open -> retracted
                             #   (applied AND retracted blocks are culled; both are consumed)
- urgency: waiting           # halted | waiting | advisory
- raised_by: jr-dev-1
- work: E-0038-export-pipeline-artifacts
- raised_at: 2026-08-05T13:50Z
- question: Export format for pipeline artifacts — tarball per run, or loose files under runs/<id>/?
- default: tarball per run (single artifact, simple retention policy)
- assumption: -              # advisory only: what the raiser is proceeding on — REQUIRED (and
                             #   non-placeholder) when urgency is advisory; the CLI refuses otherwise
- answer: -                  # answerer-owned from here down (steward or dev-manager)
- answered_by: -             # human (dev-manager relay only) | dev-manager | steward
- answered_at: -
- retracted_at: -            # written by `retract` only; absent on a question raised before
- retraction: -              #   E-0228, and added in place by the first verb that sets it
```

The last two are the **retraction record**, and they are deliberately NOT the answer triple: a
retracted question was withdrawn, not decided. Both join the protected decision record once the
row is `retracted`, so `repair` may no longer rewrite the reason — the same rule that already
protects `answer` on a decided row.

- **`urgency` is WRITE-ONCE, and no alarm reads it.** It is set by `ask` in a single dict
  literal and reclassified by no verb anywhere in the suite. That is deliberate, not an omission
  to be filled: a reclassification verb still needs someone to remember to call it at the moment
  the world changes, which is the same declared-field failure with an extra step.

  So it is what the raiser believed at raise time, and nothing more. Read it as evidence about
  the raiser, never as the current state of the question. **Anything that needs to know whether a
  lane is actually stuck reads the board instead** — `read --stranded` joins every `## Agents`
  row observed in the `halted` row state to the open question it names, and both of those cells
  are maintained by the parties whose reality they describe. On 2026-09-02 the board's only
  `halted` row was invisible to the alarm meant to catch a stranded `halted` lane, because that
  row's `halted` status was on the board while its question still read `urgency: waiting`.

  `urgency` still does three real jobs, and each is read at a moment when it IS current: it orders
  `read --open-questions` (halted > waiting > advisory, oldest first), it orders dev-manager's own
  triage pass the same way, and `ask --urgency advisory` requires a non-placeholder
  `--assumption`. It is also REPORTED beside a stranded row so a reader can see what the raiser
  believed - reported, never decided on. dev-manager's §1b cross-check corrects a
  stale value **in its sort order** and does not write it back — see that section for the
  incident behind the distinction.
- Any agent appends; the **steward** (primary) or **dev-manager** (fallback) writes the answer
  fields, and **only dev-manager** asks the human; the **raiser** flips `answered -> applied` after folding the answer in.
- Ids `Q-NNNN`, allocated under the edit-lock by max-scan of this section + `registry-log.md`.
- **Every question MUST carry `default:`** — the answer the raiser would give itself. A
  live question without a default fails `validate`, so the route is the corrupt-board one:
  dev-manager fails closed and routes the violation to the steward (dev-manager SKILL §0.1),
  and the steward's remedy is in its SKILL's validate-first section.
- Urgency is set by the raiser under the classification rule (below) and cross-checked by
  dev-manager against Agents rows as ground truth.

### `## Blockers` — agent-resolvable obstacles

| id | raised_by | owner | work | state | description |
|---|---|---|---|---|---|
| B-0003 | jr-dev-2 | lead-dev | E-0051-vocab-guard-ast-port | open | worktree base branch missing on remote |

- Obstacles an **agent** can resolve (broken env, merge conflict, missing dep, stale
  heartbeat found by the liveness sweep). **Never human decisions** — those are Questions;
  if resolving a blocker turns out to need a human call, the owner raises a linked Question
  and notes `-> Q-NNNN` here.
- `owner` = the agent responsible for resolution; a manager role (steward **or** dev-manager)
  assigns ownership on its pass. Lane-hygiene blockers (a lane still holding a claim or the deploy
  lock, an orphaned lock) are owned by `steward`, the only seat that may `reap`; the steward
  resolves them in its owned-Blocker sweep (`skills/steward/SKILL.md`, domain P).
- `state`: `open | resolved` (resolved rows are culled).
- **A Blocker's description is write-once too, so ground it as `ask` requires:** state the
  measurement behind each load-bearing clause and when you took it - "PR NNN
  MERGEABLE/CLEAN as of $(date -u +%H:%MZ) (`gh pr view`)", not "PR NNN is MERGEABLE/CLEAN". To correct one, its
  owner raises the corrected blocker with `--owner <its own role>` and a description that
  names the old id, then resolves the old one. A seat that finds a blocker's description false
  and does not own it routes the owner a Note asking for that; a Note beside a row is not a correction.

### `## Directives` — standing human instructions

```markdown
- [2026-08-05T15:00Z] Hold all merges to the release branch until E-0042 ships.
```

- Written **only by dev-manager**, transcribing the human verbatim (the human writes nothing
  directly anywhere in this file).
- Writing a directive **CODE-refreshes dev-manager's own `## Agents` heartbeat** in the same
  locked write (heartbeat cell only — `status`, `work`, `phase`, `eta`, `waiting_on` are left
  exactly as they were). A directive is proof dev-manager was alive at that instant, and without
  the stamp an interactive seat could write one while its row aged into the reap window the
  steward sweeps (E-0220). dev-manager does not run that sweep and has not since — but it is a
  row on the same board, subject to it like every other, so the seat would be reaped for silence
  in the very act of working, and the fleet's only human surface would go `idle` on the board
  while a human sat at it.
- **Every agent re-reads this section immediately before any irreversible action** (merge,
  publish, delete, force-push) and aborts/re-plans on contradiction. This is also the
  human's veto channel for pending autonomous dispatches.
- **The leading `[<ISO>]` is CLI metadata and is NOT matchable.** Both substring matchers over
  this section — `deploy-lock take --ack-directive` and `expire-directive --match` — search the
  Directive's **text** only. The stamp is written by the CLI, not the human, so while it was
  searchable every Directive carried a distinct, guaranteed-unambiguous needle that the tool had
  generated itself and that conveys nothing about the content: on 2026-09-03 eight active
  Directives were acknowledged with eight timestamp fragments and the deploy lock taken with
  nothing read (gh#127). A stamp-only needle now matches nothing and is refused. Dates the
  **human** wrote in the body stay matchable on purpose — quoting one is quoting the body, and a
  rule that refused "needles that look like dates" would be a heuristic guessing at meaning, one
  that eventually refuses an honest quote from a Directive that is *about* a date. Both readers
  (`preflight`, `read --directives`) still **print** the stamp: it left the matching surface, not
  the reading one.
- **Writing a Directive whose text contains, or is contained by, an ACTIVE one WARNS** (gh#236). Ambiguity is judged against the whole active board, so every substring of the shorter of such a pair is also a substring of the longer: the shorter has no unambiguous `--ack-directive` needle and `deploy-lock take` then refuses for every lane until a human expires one and restates it. The warning names the other Directive and prints on stderr after the write. It **warns and never refuses** — the Directive lands and the command exits `0`, because refusing here would wedge the one command a human uses to repair a wedged board. Reword while it is still free, or `expire-directive` one of the pair. Identical text is deliberately not warned about: one needle per copy already discharges it.
- **An EMPTY `directive --text` is REFUSED** (`directive_text_empty`, gh#236 review). `""` is a substring of every text, so an empty Directive is the maximal containment case rather than a harmless one: once written, `deploy-lock take` refuses for every lane both with no ack and with any needle, and the only repair left is `expire-directive --index`, which this file documents as re-basing and unsafe. The two sibling surfaces (`directive_ack_empty`, `directive_expire_empty`) already refused empty input; this was the one that did not.
- the steward culls expired directives.

---

## Ownership summary (single writer per field)

| Surface | Writer |
|---|---|
| `## Agents` row | that agent only |
| `## Queue` rows | lead-dev publishes/assigns lanes; claimant edits claimed rows; deploy-lock line by its holder |
| `## Questions` | raiser creates + flips `answered -> applied`; the steward (primary delegated answerer, `--by steward`) or dev-manager (fallback) fills `answer`/`answered_by`/`answered_at` + flips `open -> answered`; **only dev-manager may record `--by human`**: the CLI refuses that write from any other declared `--actor`, but the actor is self-declared, so it checks the role a caller names, not who the caller is (*Command surface*, below) |
| `## Blockers` | raiser creates; owner resolves; a manager role (steward **or** dev-manager) assigns ownership — the steward only a blocker it owns or one with no owner (`-`), dev-manager any (`blocker_assign_steward_owned`) |
| `## Directives` | dev-manager only |
| steward findings (stale rows, orphaned deploy lock, drift) | apart from the two manager verbs it SHARES with dev-manager (`answer`, `--by steward` only, and `assign-blocker`, own or unowned blockers only), steward writes NOTHING but its own `## Agents` row, `note`s (append **and** `--strike`, on Notes routed to it), and `raise-blocker` rows owned by steward, which it resolves itself (`resolve-blocker`); it ALSO holds `reap`, `stand-down`, `repair` and `cull` since E-0220, the only verbs in the fleet that write a row their actor does not own |
| Culling + malformed-row repair | steward only (moved from dev-manager, E-0220) |

`registry-log.md` receives culled blocks verbatim with a `<!-- culled <ISO> -->` marker — applied
Questions, done **and withdrawn** Queue rows, resolved Blockers, expired Directives, and
**struck Notes** — plus the
`notes/` overflow file each culled Note pointed at, inlined into the same block and then deleted.

### The per-VERB view of the same facts: `ACTOR_CAPABILITY`

The table above answers *"who writes this surface?"*. The other question — *"who may run this
**verb**?"* — is answered by `ACTOR_CAPABILITY` in `skills/_shared/agentflow/registry.py`, printed
by `registry read --capabilities`.

**That map is the enforcement, not a description of it.** Every role restriction in the CLI is one
row of it, raised through the single `_require_capability` helper; there is no second copy of the
answer that could disagree. The table above is prose about the same facts and **must** agree with
it — where they differ the map is right, and the prose is the bug. That is not hypothetical: the
restrictions used to be a dozen hand-written `if a.actor != ...` guards, and eleven sites across
six files still described `cull` as dev-manager's work years after it became steward-only
(`gh#219`), because nothing could check a sentence against a scattered `if`. Every concrete
`<verb> --actor <role>` in this file is now asserted against the map by `test_registry.py`.

The map distinguishes two restrictions that read alike and are not:

- a **role** restriction (`cull` is steward's) is decidable from the verb alone, before any
  registry is read — this is the half a spec's scope item can be checked against;
- an **ownership** restriction (`release` belongs to the row's claimant) depends on board data, so
  no static table can answer it. The map records it as prose so a reader is not told a verb is open
  when in practice one loop holds it, and the verb's own guard enforces it.

### Cross-actor scope items — the hand-off convention

An epic's scope may legitimately need a step its own builder lane cannot run. **Write it as a
hand-off with a named owner, never as an item of this epic's own scope.**

The distinction is not bookkeeping. An item inside `## Proposed scope` is something this epic's
verification is measured against, so an item its builder is forbidden to execute makes the epic's
criterion unmeetable **by construction** — it ships `done` carrying a criterion it never could have
satisfied. That is not a hypothetical failure mode: it happened on two consecutive epics, both on
`expire-directive`, and both times the builder routed the step correctly and the step still never
happened. `lead-dev` believed it was dev-manager's (it had routed, correctly); dev-manager believed
it was the epic's (it had deferred, reasonably). Both parties behaved correctly at every step,
which is exactly why no amount of diligence closed it.

A hand-off is a separate section, and it carries three things:

```markdown
## Hand-offs

- **owner:** dev-manager — the only actor `expire-directive` is granted to
  **step:** expire the interim `[2026-09-01T14:24Z]` directive
  **trigger:** once this epic's SKILL text has merged and the directive is redundant
```

- **owner** — the actor that can actually run it, taken from `read --capabilities`, not guessed.
- **step** — what to do, in the owner's own vocabulary.
- **trigger** — *when*, which is the field that closes the deadlock. "As the last step of this PR"
  is not a trigger any other seat can act on; "once X has merged" is.

Then route it: the owner learns about it through the same channel it learns about everything else —
a `note --to <owner>`, or a `raise-blocker --owner <owner>` if the epic genuinely cannot close
without it. **A routed Note is a claim on someone else's attention, so strike it when it is
consumed** (`note --strike`); the p190/p195 routing note sat live and unstruck between two lanes
for two days, which is what made the ownership inversion invisible on the board.

`dev-manager` §7c runs the promotion-time check that catches the un-rewritten case. It **reports**;
it does not block, because a visible, assigned cross-actor step is fine and an invisible one is not.

---

## Writing — the mediated CLI

Agents never hand-edit `registry.md`. Every mutating write goes through one CLI, which performs
the four-step lock dance (acquire → re-read → ownership/schema-checked edit → atomic write →
release) and **refuses violations loudly** — a refused write exits nonzero and names the rule it
enforced (`refused [rule_key]: REGISTRY.md #Section: ...`). This is what
makes the single-writer and re-read-under-lock invariants hold *by code*, not by an agent
remembering to, for the `--actor` a caller declares (*Command surface*). Storage stays plain markdown (human-readable, git-diffable); sections the write
did not touch are spliced back byte-for-byte.

**Exit codes** — distinct because the recoveries are distinct. A caller that collapsed any two
of these would retry the wrong one:

| code | means | the recovery |
|---|---|---|
| `0` | done (or, for a read, the answer is yes) | proceed |
| `1` | **TWO outcomes, and they do not share a recovery** - read the message. `registry write did NOT land` is the transient one: the file was held for the whole replace-retry window and nothing was written. `permission denied: <path>` is NOT transient; the registry was not modified and re-running will fail identically until that path's permissions are fixed | re-run on `did NOT land`; on `permission denied` **stop and fix the path** - retrying it is the infinite loop gh#138 removed from the message and must not be reintroduced by this table |
| `2` | ownership / schema: you may not do this, or you invoked it wrong | fix the call, or stop |
| `3` | the mkdir **edit-lock** is held by a writer the CLI cannot yet judge dead: the lock is under 5 minutes old and does not record a dead PID on this host (its holder may in fact have died elsewhere). Also `refused [edit_lock_stolen]`: this command's own hold was broken by another writer, or the board changed on disk, after it read the board and before it wrote - **nothing was written** and its lock was left to the new holder | retry the whole command; it re-reads the board under a fresh lock. Neither case is a verdict on your call |
| `4` | an ACTIVE Directive is unacknowledged (`--ack-directive`) | read the quoted Directive, acknowledge it with a substring of its **text**, re-run. The refusal quotes each bullet with its `[<ISO>]` stamp, but the stamp is not searched — pasting it back is refused as `directive_ack_no_match` |
| `5` | `deploy-lock assert`: you do **not** hold the deploy lock (or your hold went stale) | take it, or wait for the named holder — never merge |

An unreadable registry (truncated, half-synced, or a `--file`/`$AGENTFLOW_REGISTRY` pointed at
the wrong thing — a directory included) is a `file_unreadable` refusal on `2`, never a traceback:
a caller that branches on the code needs a case for it, not a stack.

Note the deliberate 2-vs-5 split: 5 says the command was well-formed and the answer is *no*,
2 says the command itself was wrong. `assert && merge` needs to tell a lock another lane is
holding from a typo, because the two call for opposite responses.

**Refusal keys a skill tells you to branch on** — one exit code carries many refusals, and the
key in `refused [rule_key]` is what separates them. Every key a skill names is named somewhere in
this document; the ones the sections above do not already cover are below (all exit 2 except
`directive_ack_ambiguous`, which exits 4 with the other directive-acknowledgement refusals). A key
no skill names means "fix the call", and its message is the whole answer:

| key | means | the recovery |
|---|---|---|
| `agent_known` | a flag that names a loop (`--actor`, `--owner`, `--to`, `--agent` and the rest) named something that is not a fleet loop; the message names the flag that carried it | fix that flag's value — a malformed call, so never short-wake and retry it |
| `deploy_builder` | `deploy-lock take` or `assert` was run by an actor that is not a builder lane | a malformed call, not contention: only a builder takes or asserts the lock, so fix the actor and never wait on it |
| `directive_ack_ambiguous` | one `--ack-directive` needle matched two or more DISTINCT active Directives. Judged against the whole active board, so it fires from any position on the command line | narrow the needle until it names one Directive. Reordering the acks never helps, and would acknowledge two having read one |
| `id_format` | an id or `--work` value is not a well-formed `E-`/`Q-`/`B-` id — a bare slug passed as `--work` included — or a `publish` slug derives no usable name | pass the real id the CLI printed, or `-` for `--work` when there is none; put the slug in the free text. Nothing was written |
| `blocker_open` | `resolve-blocker` or `assign-blocker` named a Blocker that is no longer `open` | nothing to do on that row — it is already resolved, and the cull sweeps it |
| `blocker_owner_resolves` | `resolve-blocker` was run by someone other than the Blocker's assigned owner | leave it to the owner; if it should be yours, a manager re-assigns it first with `assign-blocker` |
| `waited_on_live` | `apply`, `retract` or `resolve-blocker` named an id a `## Queue` row still names in `waiting_on`; consuming it would leave that row parked on nothing and fail the next `validate`. The message lists each such row and its `claimed_by` | nothing was written. Route each named claimant a Note (unless it is you) and consume the id on a later wake, once no row waits on it. The claimant resumes a parked row (`release --state building`) or re-parks it on the successor id (`release --state parked --waiting-on <new id>`); a `done` or `review` row is never resumed — its claimant clears the cell (`release --state <its state>` with no `--waiting-on`), and a `done` row is also swept by the steward's `cull` |
| `repair_steward` | `repair` was run by an actor other than the steward | a malformed row is the steward's work item: route it there, and if you are the steward, `repair` is yours to run |

**Invocation** (config resolves `data_root` + `lock_dir` from `environment.md`):

```
python -m agentflow.registry <command> --actor <your-loop> --project <project-name> [args]
```

**Command surface** — ownership is checked per command against the `--actor` you declare; that
role may only touch what this document grants it, and a refusal cites the exact rule. The CLI
checks a declared role and does not prove identity: `--actor` is a free argument, so any shell
that can run the CLI can name any role, `dev-manager` included. These checks stop a lane writing
outside its role by mistake; they are not access control against something on the same box:

| write | command |
|---|---|
| your Agents row (heartbeat auto-stamped) | `status --actor <you> --state building\|waiting\|halted\|idle [--work E-NNNN-<name>] [--phase P] --eta ISO [--waiting-on Q-/B-NNNN]` (`--eta` required unless `--state halted`) |
| claim a ready row in your lane | `claim --actor <you> --id E-NNNN-<name>` |
| release a claimed row | `release --actor <you> --id E-NNNN-<name> --state ready\|building\|parked\|review\|done [--waiting-on Q-NNNN]` — `--waiting-on` must name a live `Q-`/`B-` id, the same check `status` runs, and `--state parked` requires it (`agent_waiting_on`, exit 2, row unchanged); use the id `ask` / `raise-blocker` printed |
| RESUME a row you parked | `release --actor <you> --id E-NNNN-<name> --state building` (atomic; keeps your claim) |
| lead-dev publishes work into a lane | `publish --actor lead-dev --slug <spec-slug> --title "<slug> (pNNN, <domain>)" --lane lead\|jr-dev-1\|jr-dev-2\|jr-dev-3` — **allocates** the id and prints `published: E-NNNN-<name>` on stdout; passing `--id` is refused |
| lead-dev moves an UNCLAIMED, unstarted row to another lane | `relane --actor lead-dev --id E-NNNN-<name> --lane <new> --reason "<why>"` — the reason is recorded on the row. Refuses a claimed row (route its claimant a Note instead), a row past `ready`, and a lane equal to the current one |
| lead-dev retracts an UNCLAIMED, unstarted row | `withdraw --actor lead-dev --id E-NNNN-<name> --reason "<why>"` — sets state `withdrawn` (**not** `done`: `done` is the claimant's completion signal, and `cull` moves a consumed row VERBATIM into the append-only `registry-log.md` — so a retraction filed there becomes a permanent, never-corrected record that the work shipped) and leaves the row visible with its reason until `cull` sweeps it |
| raise a Question (`--default` required; `--assumption` required for `advisory`) | `ask --actor <you> --urgency halted\|waiting\|advisory --question "..." --default "..." --assumption "..." (required for advisory) [--work E-NNNN-<name>]` — **allocates** the id and prints `asked: Q-NNNN` on stdout; use the id it prints for the `--waiting-on` that follows |
| steward or dev-manager answers (open → answered) | `answer --actor dev-manager --id Q-NNNN --answer "..." --by human\|dev-manager`. **`--by human` is bound to `--actor dev-manager`** — it is the only seat that asks the human, so it is the only one that may relay one (`q_answer_human_relay`). The steward is the primary delegated answerer and answers only `--by steward`. **Every manager value of `--by` is bound to its own role**: `--by steward` only with `--actor steward`, `--by dev-manager` only with `--actor dev-manager` — either manager signing the other's name is refused (`q_answer_by_self`, exit 2, nothing written). Every `answer` appends an audit Note naming both roles. A `Q-` id that heads more than one block is refused before any judgement (`id_duplicate`, exit 2, nothing written), as for `apply` and `retract`: the parser keeps the last block and the writer edits the first, so judging one and writing the other overwrote an answer already given. `validate` reports the twin as well. Clearing it depends on its shape: the cull sweeps an applied or retracted twin, steward `repair` can rename the first heading only when neither twin carries an answer (`--old-line` always matches the first block, and touching an answered heading is `repair_decision_record` or `repair_heading_skeleton`), and a twin carrying an answer needs a human hand edit |
| raiser applies (answered → applied) | `apply --actor <raiser> --id Q-NNNN` — refuses a duplicated `Q-` id (`id_duplicate`, exit 2, nothing written) |
| raiser retracts (open → retracted) — the OTHER terminal end, and not an answer | `retract --actor <raiser> --id Q-NNNN --reason "<what collapsed>"` — raiser only, `open` only, reason required. Terminal and consumed: it leaves `read --open-questions`, no lane may point `waiting_on` at it, and the cull sweeps it with the applied rows. It retires the QUESTION, not the LANE — a lane still `halted` against it is reported by `read --stranded` as `question-retracted`. Refuses a duplicated `Q-` id (`id_duplicate`, exit 2, nothing written) before judging raiser or state |
| raise / resolve / assign a Blocker | `raise-blocker --actor <you> --description "..." [--work E-NNNN-<name>] [--owner <role>]` (prints `raised: B-NNNN` on stdout; use the id it prints) · `resolve-blocker --actor <owner> --id B-NNNN` · `assign-blocker --actor steward\|dev-manager --id B-NNNN --owner <role>` — either manager role assigns, the same pair that may `answer`; the steward only a blocker whose owner is `steward` or `-`, dev-manager any (`blocker_assign_steward_owned`, exit 2) |
| dev-manager writes / expires a Directive (`directive` also stamps dev-manager's own heartbeat) | `directive --actor dev-manager --text "..."` · `expire-directive --actor dev-manager --match "<substring>"` (by CONTENT — the Directive's text, never its minted `[<ISO>]` stamp; must hit exactly one live Directive. `--index N` still works but is 1-based over the LIVE set and RE-BASES as rows expire, so one value used twice targets two different Directives -- it silently expired the wrong instruction on 2026-09-01. Both paths now print what they expired.) |
| the steward culls closed entries to the log | `cull --actor steward` |
| take / release the deploy lock (builders) | `deploy-lock take\|release --actor <builder>` |
| **prove you hold the deploy lock, immediately before merging** — read-only, lock-free, writes nothing; chain the merge behind it (see `## Queue`) | `deploy-lock assert --actor <builder>` — exit `0` iff `holder` is you and your hold is not stale; exit `5` naming the holder, how long they have held it and their heartbeat age (or saying the lock is FREE). An unknown `--actor` or an unreadable board is `2`, not `5`: the two do not share a recovery |
| **list the active Directives a guarded action must acknowledge** — read-only and lock-free | `preflight --actor <loop> --action merge\|publish\|delete\|force-push` — prints the count and every active Directive. It REPORTS; `deploy-lock take --ack-directive` is what refuses. It **never returns nonzero because of what it found** — a contradicting Directive prints and still exits `0` — so it must be read as a separate judgement step and **never chained into the action with `&&`**, which would carry you straight past what it printed. It still exits `2` per the table above for a registry it could not read or an unknown `--actor`, so read the status: a silent nonzero is an empty list that reads as "no Directives" |
| append / strike a coordination Note (`--text` capped at 300 UTF-8 bytes; a loop strikes only what is routed **to it** or to `fleet` — dev-manager, this surface's janitor, may strike any) | `note --actor <you> [--to <role>] --text "..."` · `note --strike --actor <you> --match "<substr>"` — `--match` must hit exactly one live Note you may strike; more than one is refused (`note_strike_ambiguous`, exit 2) with the candidates listed. `--text` with `--strike` is refused (`note_strike_text`). Prints `struck: <note>` on stdout |
| the steward reaps a dead loop | `reap --actor steward --agent <dead-loop> [--override-stand-down] [--force --reason "<who confirmed, how>"]` — refuses a row `read --stale` would not list (`reap_not_stale`, exit 2, board unchanged; judged by `stale_rows`, the same predicate) unless `--force` is passed with a `--reason` (`reap_force_needs_reason`), which the steward does only once a human has confirmed the lane is gone — the reason goes into the reap Note, which says `(stale)` only when staleness was checked and true; refuses `--agent` equal to `--actor` (`reap_self`); refuses the human's seat, any row in `ETA_HORIZON_EXEMPT`, with no override (`reap_exempt_seat`, exit 2, board unchanged); refuses a stood-down row unless `--override-stand-down` is passed (`reap_stood_down`, exit 2, board unchanged), which the steward does only after dev-manager decides that lane is not coming back; idles its row (phase `reaped by steward (stale)`, or `reaped by steward (forced)` after a `--force` reap of a row `read --stale` did not list; both are excluded alike), returns its `building` / `parked` / `review` claims to `ready`, clears its deploy lock. Terminal rows (`done`, `withdrawn`) and rows whose `state` is outside the enum are left untouched, and the Note tallies all three outcomes separately — see **What `reap` may not touch** below |
| the steward marks a lane a human deliberately stopped | `stand-down --actor steward --agent <lane> --reason "<why>"` — writes `stood down by steward: <why>` into that row's `phase`, which is what the missed-wake alarm reads to exclude it. Claims, deploy lock, `heartbeat` and `eta` are all left ALONE (it is not `reap`), and the mark clears on that lane's next full `status` write. A stood-down row stays in `read --stale`, labelled `stood-down: <why>` — never `flag` or `reap-eligible` — and is escalated to dev-manager, never pinged or reaped by the sweep. Refuses, exit 2 and board unchanged: an agent with no Agents row (`id_exists`, as `reap` does), `--agent` equal to `--actor` (`stand_down_self`), the human's seat or any row in `ETA_HORIZON_EXEMPT` (`stand_down_exempt_seat`), which the alarm never pages for, and a row already reaped (`stand_down_reaped`), because the mark would erase the reap and send a lane holding nothing back into `read --stale` as an escalation that `reap` then refuses |
| the steward repairs a **structurally** malformed row | `repair --actor steward --section <S> --old-line "<exact>" --new-line "<fixed>"` — re-parses (`repair_corrupt`); runs `validate`'s own checks on the board before and after, and refuses a result that loses a table (or the parse) the board still had, that ADDS a row violation the board did not already have (a duplicate whose count merely shrinks is not new), or that leaves a violation the replaced line itself owned (one that deleting that line alone would clear) — `repair_fails_validate`, exit 2, board byte-unchanged, no audit Note; violations already on the board elsewhere, including the row problems a lost table sits beside, do not block it; refuses any repair that CHANGES the parsed decision record of a question, **whatever section it is filed under** (`repair_question_field` / `repair_decision_record`), and any repair that adds, removes or renames a section or block heading (`repair_heading_skeleton`, gh#192), and a blank `--old-line`, which would insert a line rather than mend one (`repair_blank_old_line`, gh#558), or a boilerplate one — a table header or separator, the Deploy lock or Notes line, a section comment (`repair_boilerplate_line`, gh#569) — and, in a table section (`## Agents`, `## Queue`, `## Blockers`), any `--new-line` that is not a row of that table (`repair_not_a_row`): a `| ... |` line, a blank line to remove a stray one, or in `## Queue` the Deploy lock or Notes line, never a bullet or prose, nor a heading-shaped line that leaves the skeleton unchanged (one that changes it is `repair_heading_skeleton`), because a non-row line is inert there and writing one over a row deletes it unseen — and a Queue Note bullet as `--old-line`, struck or live (`repair_note_line`), because Notes belong to `note --strike` and the cull — and a `## Queue` row `validate` already accepts unless its id is duplicated or malformed, or any cell the row checks did not flag on a `## Queue` row they reject, or a flagged `claimed_by` or `waiting_on` mended to anything but `-` (or, where `-` alone would strand the row, anything but what `reap` or `release --state building` writes) (`repair_queue_owned`), because its lane and claim belong to `publish`, `relane`, `withdraw`, `claim`, `release` and `reap` — and a `## Blockers` row `validate` already accepts unless its id is duplicated or malformed, or any cell the row checks did not flag on a `## Blockers` row they reject, or a flagged `owner` mended to anything but `-` or a flagged `state` to anything but `open` (`repair_blocker_owned`), because its owner and state belong to `assign-blocker` and `resolve-blocker` — and an `## Agents` row `validate` already accepts unless its agent is duplicated, or any cell but a flagged status on an `## Agents` row they reject, or that status mended to anything but `idle` (`repair_agents_owned`), because its heartbeat and eta belong to the lane's own `status` and its phase to `stand-down` and `reap`, so a repaired row could fake the heartbeat of a dead lane — and refuses `## Directives` outright (`directive_devmanager`) — see **What `repair` may not touch** below; every success appends an audit Note routed `steward -> fleet` |
| **which loop may run which verb** — read-only, and it reads no registry at all | `read --capabilities` — prints `ACTOR_CAPABILITY`: restricted verbs with the actors granted them and a **promotion verdict** for each (`OK` when every builder lane holds it, `FLAG` otherwise), open verbs with the ownership gate each still carries plus any janitor exempt from it, and the flag-level narrowings (`ask --urgency halted`, and `answer --by human|steward|dev-manager`, each bound to one role). It needs no `--project` and no board — it neither reads nor even *resolves* one, because the thing checked against it is a **spec** |
| **check a spec's scope items against the map** — the promotion-time check, run rather than eyeballed | `read --capabilities --scope-item "<item text>" [--scope-item ...]` — names the item, the verb, the capable actor, and `FLAG`/`OK`. Prose matches as a **word set** (every listed word present, any order, anything between), because a spec writes English: "expire the *interim* directive" and "reconcile the directive and expire it" both name `expire-directive` and neither contains the CLI spelling. It **reports**; it never blocks — see `dev-manager` §7c and **Cross-actor scope items** above |
| check the registry is schema-valid | `validate` — lock-free; exit 2 + the violations if not, including a table key on more than one row, a `Q-` id heading more than one block, and a `## Queue` whose Deploy lock line is **unreadable** (the `- **Deploy lock**` prefix survives but `holder: <lane>, taken-at: <ISO>` does not read: the steward `repair`s that line in place) or **missing** (no such line: restore it from git history or the registry template, since `repair` cannot insert a line). Either one fails every lane's wake gate, because without a readable line every lock verb refuses `file_damaged` |
| create a registry that does not exist | `init --project <name>` — resolves the template itself; never clobbers an existing file. `<name>` must be a configured project: an unconfigured one is refused (`project_unconfigured`) rather than given a board no builder reads — see **Board resolution** below |

**Reads are lock-free** — read the file directly, or use `read [--answered-for <you> | --claimable <you> | --directives]`
for the common filters. **Free-text values must be single-line and contain no `|`**; the CLI
rejects a newline or pipe (`inline_only`) so a value can never fabricate a row or field it does
not own.

### Board resolution — which file a command reads and writes

Three inputs can name the board. The rule is built so a write can never land on a board the
caller did not see named (gh#715):

1. **`--file <path>`** — the explicit override, and it wins outright. It is on the command line,
   so it cannot be the invisible cause of a wrong-board write. With `--file`, `--project` only
   names `init`'s project; it is not checked against config (this is how tests and drills address
   scratch boards), and it never names the edit-lock, which follows the board (see
   **Concurrency**).
2. **`--project <name>`** — resolved through `config.load_project`, then joined under
   `data_root` as `<data_root>/projects/<name>/status/registry.md`. A name with no loadable
   config is refused (`project_unconfigured`, exit 2) with the config error and its
   remediation — never "run init", which is how a typo used to get a phantom board. `init`
   takes the same path, so it refuses an unconfigured project too.
3. **`$AGENTFLOW_REGISTRY`** — the one input nothing prints. Alone (no `--file`, no
   `--project`) it is the board, exactly as before. With `--project` it must name the SAME
   board: if the two differ the command refuses (`registry_board_conflict`, exit 2), naming the
   env var's path and the project's path, and reads or writes nothing. It used to win silently,
   which let a leftover env var carry a dev-manager Directive onto a board no builder reads.

Recovery for `registry_board_conflict`: unset `$AGENTFLOW_REGISTRY` if `--project` is the board
you meant, or pass `--file` to choose a board explicitly.

### The write surface is fully mediated

Every registry write has a mediating command — including the three surfaces that were once
direct-edit exceptions: coordination **Notes** (`note` append / strike), the steward's cross-row
**liveness sweep** (`reap`, dev-manager's until E-0220 moved it), and **malformed-row repair**
(`repair`, steward-only since the same split, which re-parses, refuses a fix that leaves the file
broken or that `validate` would score worse than it should, refuses any repair that changes a question's parsed decision record regardless of the
section it is filed under, and leaves an audit Note behind). There is **no sanctioned
hand-edit**: a change made outside the CLI is a contract violation.

The honest ceiling: the CLI *mandates* the write path and refuses violations when called, but it
cannot physically stop an LLM agent from writing the file directly. **`validate` is the backstop** —
a lock-free schema + consistency check. The firing contract below **requires** every loop to run it
at wake, **fail-closed** (an invalid registry blocks the firing rather than acting on corruption),
and CI runs it on the seeded template (`.github/workflows/ci.yml`, the shared-tests job's
"Registry validate gate (seeded template must be schema-valid)" step, which `init`s a fresh
registry and validates it).

**Be precise about which of those two holds today.** Both are wired. The CI gate runs on the seeded
template; the wake gate is carried by all seven loop `SKILL.md` wake procedures, each naming the
refusal path (exit 2 → claim nothing, route the violation to the steward, which alone holds
`repair`), and a gate in `skills/_shared/tests/test_skill_call_sites.py` fails the build if a wake
read drops the call. Be equally precise about what that is worth: it makes the check *run*, which
is a conformance property, and says nothing about how much the check *catches* — that is the
strength question below, and it is separate. So a hand-edit that corrupts a row is caught at the
next `validate` **or write** — loudly, with the exact violation, never silently honored — and the
wake `validate` is now the earlier of the two rather than a step nobody performed. Mandate +
refusal + validation is what "enforced by code" means here; a filesystem-level
write-block is the only thing that would make it literally impossible, and that is deliberately out
of scope for a file-native, git-diffable store.

### Rules that moved from prose to code

The first fleet-consistency audit found four rules that lived only as expectations in this
document and in the skill prose, and each of them failed on the first live night. The table has
grown past those four since -- it is the running list, not that audit's minutes. They are now
enforced by the code that reads and writes these surfaces — the prose above describes what the
code does, it is no longer the only thing holding the rule up. The project-scope rule is listed
as its two layers — glob scope and record filter — because either one alone is bypassable:

| rule | enforced by | on violation |
|---|---|---|
| a builder's survey sees only its own project's subtree | `survey_epics(..., project=<name>)` narrows the globs to `projects/<name>/` | a sibling fleet's specs are never returned; the unscoped default (dev-manager / `choose-next` / `backlog`) is deliberate and documented at each call site |
| a record belonging to another project is never dispatchable | `selectable(records, excluded_domains, project=<name>)` (the builder-facing call; `dispatchable` is inside it and its own docstring says not to compose it by hand) | dropped when frontmatter `project:` is set and differs; `project:` unset still dispatches (legacy specs predate the field) |
| a promoted epic can actually be published | the spec carries a `## Files` section naming real repo-relative paths, checked at promotion (`dev-manager` §7c) via `agentflow.plan_parser.files_section()` | `lead-dev` seam-checks disjointness from `domain` + that listing; with neither usable it refuses to publish, and the epic sits approved-but-unclaimable with **nothing reporting why**. Only `present` is mechanical - whether the listing is specific enough is the promoting seat's read |
| an unpromoted or terminal epic is never dispatchable | `dispatchable()` keeps only `current_state: approved-for-autonomous` | case- and whitespace-tolerant; **unset does not dispatch**; the drop is silent because non-approved is the normal case (325 of 329 records in one survey) and a warning there would train readers to ignore the line that matters |
| `queue_class` is an ALLOW-list, not a deny-list | `dispatchable()` keeps only unset / `fleet-buildable` | every other value dropped; an out-of-vocabulary value (e.g. `bug`, `feature`) additionally raises a survey warning line — a warning, never an exception, so one bad spec cannot break dev-manager's refinement survey |
| an advisory question carries a real assumption | `ask --urgency advisory` requires `--assumption` | refused, exit 2, naming the rule; `validate` reports a LIVE (`open`/`answered`) advisory row whose `assumption` is still `-`, and never repairs it |
| a Directive stamps dev-manager's heartbeat | `directive` stamps the dev-manager `## Agents` heartbeat in the same locked write | heartbeat cell only; phase and every other field untouched |
| a merge-to-main runs only while the merger HOLDS the deploy lock | `deploy-lock assert` — read-only and lock-free, so the merge can be chained behind it in one expression that cannot half-run | exit 5 naming the holder and how long they have held it; the chained merge never starts. Prose could not hold this one up: "remember to chain the commands" is the same unenforceable class as "remember to re-read the Directives", which is why taking the lock already carries `--ack-directive`. This is that gate's mirror image — taking was gated on having read, merging is now gateable on holding |

**The `## Files` row above is a check on the SHAPE of the listing, never its completeness — and
completeness is what disjointness depends on.** `python -m agentflow.files_vs_diff --spec <spec>
--commit <sha>` measures the rest: it reads the same `files_section()` declaration and diffs it
against what the merged range actually touched, reporting two deltas that are not the same finding.

**Before either delta can mean anything, the range has to be able to answer — and the fourth
outcome is that it could not.** `NO USABLE RANGE` fires when `base` and `head` name the same commit,
or the refs are the wrong way round, so the range holds no commits and nothing was compared
whatever the spec declared. What the spec says about itself is still reported under the banner
(gh#347) — the distinct-entry count, the duplicates, and the spec-defect, parser-noise and
out-of-repo classes are parses of the spec that no range changes — while both deltas, MIS-PATHED,
the directory/glob coverage and any entry whose path has a separator but is not in the tree are
withheld, since each is half a claim about the range; the report counts that last class
(`(K withheld: ...)`) so the declared total still adds up. It is reported ahead of the declaration guard on purpose: naming the
declaration first would report the weaker of the two facts and leave a reader believing a
comparison happened. The header says `touched: not measured` rather than a count, because **an
empty `touched` used to be the only visible trace of a range that could not answer, and that is
byte-identical to the trace of a range which honestly touched nothing** — two runs whose payloads
differed in the `head` string alone (gh#316). `as_dict` carries `range_usable` and `range_commits`
so the two are separable downstream, and **a sweep must filter on `range_usable`**: without it,
"declared everything correctly" and "measured nothing at all" arrive as the same confident zero.
**UNDECLARED** — touched, named by nothing in the section — is the disjointness hazard, because the
seam check could not see the path and a publish that relied on the declaration was wrong without
anyone learning. **OVER-DECLARED** — named, never touched — is the opposite failure: it parks other
lanes behind a seam nothing was ever going to use. Entries that resolve to no path here are
reported apart from both, in three classes rather than one, because a single bucket could not tell
a typo from a footnote: **spec defect** (a declaration that meant a path here and named one that is
not here), **parser noise** (the `/` the parser caught between two inline-code spans, a bare module
name, a whole prose sentence — the borrowed regex promoted it and there is nothing to repair), and
**out of repo by design** (`<data_root>/…` placeholders). One typo produces ONE finding: where an
undeclared path's basename uniquely matches one mis-pathed declaration, the pair is reported as
**MIS-PATHED** naming both strings and removed from both deltas, with the exclusion stated in the
UNDECLARED heading so the smaller number is never a quiet one. Directory, glob and brace entries are
flagged as what they are: they bound a region, so they legitimately cover what falls inside them and
can never be shown complete. It REPORTS and does not gate — exit is non-zero only for a malformed
invocation — because nothing measured this gap before it (gh#161), and a first measurement is not
yet a verdict.

**A corpus-wide number from it is a claim about a JOIN METHOD, not a fact about the fleet.**
Per epic and per range the answer is reproducible; across a fleet it is not, because nothing here
defines which merged commits belong to which spec. That join is reconstructed by hand from
`completion-truth` frontmatter, and three independent derivations of "the merged agentflow epics"
produced three different epic sets. The tool's own first sweep is the worked example: it keyed on a
sha in the verb form (`squash <sha>`) and so missed an epic recording `PR #125 f10226f` instead —
the epic that turned out to be the corpus's worst under-declarer. A derived set inherited its
source's shape assumption and reported the gap as absence, which is the defect family the tool was
built to measure. So quote a sweep as "N epics joined by <rule>", never as "the fleet's undeclared
count"; widen the rule and the number moves, and that is the honest reading rather than an error in
either run. Three specs in the current corpus carry a completion truth that names **no** resolvable
sha at all (PR numbers only, or a sha from before the repository was re-cut) and are outside every
rule — the residue is part of the measurement, not a rounding error.

---

## Concurrency — the edit-lock

The lock the CLI takes internally on every write:

```
<lock_dir>/<project.name>-registry.lock      (mkdir-lock; rmdir to release)
```

- **The board names its lock, not the command line.** `<project.name>` is the directory above
  the board's `status/` directory, read from the board's **resolved** path (junctions and
  symlinks followed, a relative path made absolute). One board therefore has one lock however a
  writer reaches it: through a junction, as a relative `registry.md`, or by `--file`. For every
  `--project <name>` call the board is `<data_root>/projects/<name>/status/registry.md`, so the
  lock is `<name>-registry.lock`, as it always was. `--project` never names the lock: when it
  disagrees with the project the board sits under, the write still goes ahead under the board's
  lock, and that is deliberate, since `--file` with any `--project` is how drills address scratch
  boards. A board whose resolved path has no directory two levels above it (at a filesystem root
  or one level under it) is refused, `refused [lock_name_empty]` on exit `2`, before any lock is
  taken or the board's text is read: an empty name would be one lock shared by every such board.
  Move the board under `<project>/status/`. `init` derives the same name before it creates
  anything: it refuses such a board the same way, and the lock path it stamps into a new board's
  header comment is the board's own lock, even when `--project` names another project.

- The CLI takes it for every registry **write** and releases immediately. Target hold:
  **seconds** — never across a build, a subagent fan-out, or a live verification run.
- **Reads are lock-free.** Writes are short and section-scoped, so a torn read self-heals at
  the next read.
- **Re-read under the lock** before writing — never apply edits to a pre-lock copy (the
  lost-update guard).
- **Stale >5 minutes = breakable.** The CLI breaks it for you and records the break as a fleet
  Queue Note, `<you> -> fleet: broke stale registry edit-lock (prev <holder>, <age>s)` — never on
  your Agents row. That Note is written even when your write is then refused (gh#656).
  (The 45-minute rule is the DEPLOY lock's, not this lock's — do not conflate them.)
- **A dead holder on this host is broken at once.** The `holder` file records the actor, a
  stamp, then `NONCE=`, `PID=`, `HOST=` and `PIDNS=` lines. `PIDNS` names the PID space the
  holder ran in (the platform, plus the Linux PID-namespace link where there is one), because a
  hostname alone is not one: WSL and native Windows, a host-network container and its host, or
  an MSYS Python and a native one can share a name and still number processes apart. When
  `HOST` and `PIDNS` both match this writer's and that PID is no longer running, the next writer
  breaks the lock on its first look, with the same Note, instead of waiting out the 5 minutes. A
  PID from another host or another PID space is never probed, and a holder file without these
  lines is judged by age alone.
- **One stealer at a time.** A writer that judges the lock breakable first takes
  `<lock>.steal` (an exclusive-create lock file that records its own PID, so a guard left by a
  crashed stealer is itself breakable; one older than a minute is cleared even if that PID now
  belongs to another process, since a guard is held for milliseconds), then judges the lock
  again inside it, and breaks it only if the holder file is unchanged and still breakable. Two
  lanes that read one stale age therefore end with exactly one holder; the other spins and gets
  exit `3`.
- **A robbed holder writes nothing.** Immediately before its write the CLI checks that its lock
  still carries the nonce it recorded and that the board on disk is still the copy it read
  under that lock. If either has changed it writes nothing, removes nothing, and exits `3` with
  `refused [edit_lock_stolen]`. Releasing likewise removes the lock only while it still carries
  that nonce, so a holder that outlived its lock cannot free the thief's. Each check and the step
  it guards are two operations, so this narrows the window rather than closing it.
- **`lock_dir` must be local to the host every lane runs on.** Staleness is the lock's age by the
  local clock against its mtime; on a network share the mtime comes from another machine's
  clock, and any skew between the two makes a live lock look stale (or a dead one fresh). The
  dead-PID rule needs it too: only a lock written on this host, in this writer's PID space,
  names a process this writer can probe. Lanes in WSL, in containers or under MSYS that share a
  `lock_dir` with native Windows lanes still contend correctly, but each judges the others'
  locks by age alone.

---

## The firing contract (every loop, every state-bearing firing)

1. **WAKE READ** (lock-free). Read the ENTIRE registry before doing anything else. Run
   `python -m agentflow.registry validate --project <name>` first and **fail closed** — on exit 2,
   branch on the refusal KEY on stderr, never the bare code. `refused [file_missing]` means the
   board is ABSENT: create it with `init --project <name>`, then re-run `validate`.
   `project_unconfigured`, `registry_board_conflict` (unset `$AGENTFLOW_REGISTRY` or pass
   `--file`) and `bad_project` are config or argument errors, not corruption: fix them per
   **Board resolution**; never `init` or route them to the steward. Any other exit 2
   (`file_unreadable`, `file_damaged`, or an `INVALID` violation list) means the file is corrupt:
   do not act on it; surface the violation (the steward `repair`s it) rather than building on
   bad state. Then extract:
   (a) your answered questions; (b) open Directives — binding on this firing; (c) Blockers
   you own; (d) Queue rows claimable in your lane; (e) all other Agents rows.
   **Strand check — READ THE BOARD, never a declared field:** `read --stranded` reports every
   `## Agents` row observed in the `halted` row state whose `waiting_on` names an open question
   raised more than 4h ago. If it prints any row AND dev-manager's heartbeat is staler than its
   normal cadence, send ONE notification-only Ntfy nag ("open dev-manager - questions pending").
   A nag, never a question channel.

   **It prints an all-clear naming what it judged when it does not fire**, and reports separately
   any `halted` row it could NOT judge — one with no referent, or naming a question that is
   missing or already closed. That row is MORE stuck than a stranded one: nothing is still
   working a question that can answer it.

   **Never key this on a question's `urgency` field.** That value is written once by `ask` and
   reclassified by no verb, so it is what the raiser believed at raise time and nothing more. A
   row's `status` and a question's `state` are maintained by the parties whose reality they
   describe; that is what makes them safe to alarm on.

   **FIVE of the seven seats run this check, not all seven.** `lead-dev`, `jr-dev-1`, `jr-dev-2`,
   `jr-dev-3` and `auditor` carry it; `steward` and `dev-manager` carry only the phase-boundary
   sentence that refers to it. For dev-manager that is deliberate - the nag it produces reads
   "open dev-manager", and a seat cannot nag itself into a session. For steward it is an open
   question rather than a decision (Q-10048): it is the seat awake when every builder is idle,
   which is exactly when a stranded row goes unseen longest.
2. **CONSUME FIRST.** Apply answered questions before claiming any new work: unpark the
   parked Queue row, resume or re-plan, retro-correct any advisory assumption, and (in step
   4) flip the question to `applied`. Honor Directives before anything else.
   Routed `## Queue` **Notes** consume the same way: act on each Note addressed to you, then
   strike **every Note you acted on** in the same pass — `note --strike --actor <you>
   --project <name> --match "<substr>"`, one call per Note, each `--match` hitting exactly one
   live line (the CLI refuses a match that hits several, listing them). Conditional, never unconditional: `--strike` refuses (exit 2) when nothing live
   matches, so a pass with no routed Notes strikes nothing. The hazard is symmetric — a
   handled Note left live is re-read as new work next wake, while striking one you did not
   handle silently drops fleet coordination — which is why "acted on" is the qualifier that
   decides.
3. **WORK.** As the loop's own procedure defines. Never hold the edit-lock during work.
4. **WRITE** (mediated). Every write is a `python -m agentflow.registry <cmd> --actor <you>
   --project <name> ...` call — the CLI acquires the edit-lock, **re-reads under it**, applies
   the section-scoped, ownership-scoped edit, writes, and releases. Never hand-edit the file;
   the CLI refuses violations (exit 2, citing the rule) so the invariants hold by code.
5. **MID-FIRING TOUCHES.**
   - **Phase-boundary heartbeat — in this order, at every boundary** (plan → build → verify →
     merge):
     1. **CONSUME.** Lock-free re-read `## Questions` for your own `state: answered` rows
        (`python -m agentflow.registry read --answered-for <you> --project <name>`), fold each
        answer in (unpark the parked row, resume or re-plan, retro-correct any advisory
        assumption you proceeded on), flip it with `apply --actor <you> --project <name>
        --id Q-NNNN`, re-read `## Directives`, and re-read the `## Queue` Notes routed to you
        (`read --notes-for <you> --project <name>`), acting on and striking each one you act
        on exactly as step 2 does.
     2. **THEN refresh** your `## Agents` row (`status --actor <you> --project <name> --state
        building [--work E-NNNN-<name>] --phase "<what is running>" --eta <ISO>`) — heartbeat
        + eta. A full write re-sends every column: without `--state` it is refused
        (`agent_state_enum`, exit 2, row not refreshed), and a builder that omits `--work`
        blanks its row's `work` cell to `-`.

     Consuming AFTER the status write stamps a heartbeat asserting a phase the answer may have
     just invalidated. The boundary is a NARROWED **steps 1+2+4**: the re-read is `## Questions`
     + `## Directives` + the Notes routed to you ONLY — never the full step-1 wake read (no
     `validate`, no strand check), or every phase transition costs a wake. Notes are in because
     a Note that unblocks work ("seam released, publish now") is time-sensitive, and left out
     it waited a full wake (gh#159). This is what makes halted-detection trustworthy, and it is
     how a mid-flight builder picks up answers, directives and Notes without waiting for its
     next wake.
   - **Pre-irreversible re-read:** immediately before any irreversible action (merge,
     publish, delete, force-push), lock-free re-read the registry; check Directives and your
     own open/answered questions; abort or re-plan on contradiction. A late answer can
     always veto a merge, never un-ship one.

> **Conformance (step 1), and its limit.** The fail-closed `validate` call is instructed by all
> seven loop `SKILL.md` wake procedures, and `skills/_shared/tests/test_skill_call_sites.py`
> fails if one of them drops it — anchored on each skill's own wake read, so naming `validate`
> in the phase-boundary exclusion below does not satisfy it. This is a gate on the *skills*,
> which are prose an LLM interprets: it proves every lane is instructed to run the check, not
> that every firing ran it. Treat it as instructed-and-gated, which is what changed, and not as
> proof of execution, which nothing in a prose fleet can give you.

---

## Question lifecycle

**0 — GROUND IT.** Before raising, the agent states **in the question body the measurement behind
each load-bearing clause, and when it took it** — "E-0200 unclaimed as of 05:24Z
(`read --claimable`)", not "E-0200 is unclaimed".

This mirrors the precondition the build side already carries (`skills/lead-dev/SKILL.md` §2.5,
*"approval is never a grounding waiver"*), and it exists because the two sides were asymmetric:
**grounding was mandatory before a loop spent its own hour and absent before it spent the human's
attention** — the scarcer of the two, and the one nobody can get back.

Measured, from this fleet's own record. Two consecutive `lead-dev` wakes raised two questions and
both collapsed: **Q-10028**'s premise was true when written at 05:24Z and false at 06:04Z, and the
peer correction that caught it had *itself expired* before it was read. **Q-10030** was raised to
replace it "on invariants that do not decay", and its central invariant was **already false when
written** — disproved by its own author an hour later, in the same wake. Both times the
measurement that would have prevented the escalation took under ten minutes and was performed
later in that same wake.

**The rule is not "use invariants".** Q-10030 tried that and picked a false one. It is *show the
reading*, so a clause with nothing beside it is visible as unmeasured — a false invariant is
indistinguishable from a true one until somebody checks.

**`--default` must carry a real answer.** `-` and blank are both refused at the write path
(`q_needs_default`). It is the answer the raiser would give itself and the first thing the human
reads; a question that cannot propose one has not been grounded enough to ask. This is also the
write path finally agreeing with its own gate — `validate` has always reported a `-` default as
*"live question missing default"*, while `ask` accepted it.

**1 — RAISE.** The agent hits a decision it cannot own and classifies by a strict rule:

- **advisory** — a safe, REVERSIBLE default exists → append the question with `assumption:`
  filled, state the assumption in the work log, and PROCEED on it. No waiting. Irreversible
  actions never proceed on assumption (minimum class: waiting). **CODE-enforced:**
  `ask --urgency advisory` without a real `--assumption` is refused (exit 2, naming the rule) —
  the placeholder `-` no longer counts, and stuffing the assumption prose into `--default` is not
  a substitute. `validate` flags a LIVE advisory row whose `assumption` is still `-` — `open` or
  `answered` only, matching the sibling `default` check: an `applied` row is consumed
history that the steward culls, and flagging it forever would leave a real registry
  permanently red. It
  **reports, never repairs** — an assumption is raiser-owned and no other actor may write it.
- **waiting** (the default posture) — the current item genuinely needs the answer, or the
  action is irreversible, BUT other claimable work exists → park the item (Queue row
  `parked`, `waiting_on: Q-NNNN`), append the question, claim other work.
- **halted** (last resort, BOTH conditions required) — the answer gates ALL safe progress
  AND the Queue has nothing claimable in this agent's lane → append the question, set own
  Agents row `halted` / `waiting_on: Q-NNNN`, send a notification-only Ntfy ping
  ("<agent> halted on Q-NNNN — open dev-manager"), end the firing. Halted agents keep firing
  on schedule so answers can reach them.

The **auditor** may raise `waiting`/`advisory` questions only, never `halted`. **lead-dev is
a builder here**: same rule, no special human path.

**2 — ROW.** Appended under the edit-lock in the agent's normal write phase; id by max-scan.

**3 — PUMP** (dev-manager, Step 1 of every pass, before any refinement — unblocking existing
agents beats feeding new work). Collect `open` questions; sort **halted > waiting >
advisory**, oldest `raised_at` first. Cross-check against Agents rows as ground truth: a row
showing `halted` upgrades its question; a raiser that kept building downgrades a claimed
"halted" to `waiting`. The liveness sweep is **not** part of this pass — E-0220 moved it, with
`reap`, to the steward, which runs it on every one of its own wakes.

**4 — ASK.** Two seats answer within delegated authority (product priority, scope
boundaries, spec interpretation that the grounding fully determines). The **steward** is the
primary one: its domain P reads every open question every wake, answers the delegated ones →
`answered_by: steward`, and routes each human-only decline to dev-manager as a
`Q-NNNN human-only: <class> - <why>` Note (`skills/steward/SKILL.md`, domain P).
**dev-manager** is the fallback for a delegated question still open when a human is in the
seat, answering as refinement hub → `answered_by: dev-manager` (`skills/dev-manager/SKILL.md`
§1c). Both classify against the same human-only list; a steward decline is a classification
dev-manager re-checks against that list, and it answers the row if the steward was too cautious.
Genuinely human-only calls go to the human in sorted order, batched, via the asking-questions
protocol. **Human-only questions wait for the human — there is no aged-default
auto-ratification.** If open questions coincide with fleet starvation (every builder
`halted`, or `idle` with nothing claimable), dev-manager sends the **stoppage alert**: a
loud, notification-only Ntfy naming the blocking questions. Unasked lower-priority rows stay
`open` for the next pass.

**5 — WRITE-BACK** (locked). Fill `answer` / `answered_by` / `answered_at`, flip
`open -> answered`. **`answered_by: human` is a claim only dev-manager may make**, because it is the only seat that put the question to a person; `answer --actor steward --by human` is refused (`q_answer_human_relay`). Enum membership alone used to be the whole check, so the steward could sign the human's name in one sanctioned command with nothing malformed to notice and `validate` green. The same bind holds between the two managers (gh#719): `answered_by: steward` only from `--actor steward`, `answered_by: dev-manager` only from `--actor dev-manager` (`q_answer_by_self`). Every `answer` now appends an audit Note naming the actor AND the `answered_by` it recorded, so who WROTE a decision is on the record next to who MADE it. If the answer changes a spec, update the spec in the same pass and cite
the path in the answer. If the answer is an operational override ("abort the merge"), ALSO
transcribe it into `## Directives` so the pre-irreversible re-read catches it.

**6 — CONSUME.** The raiser's next registry read — wake, phase-boundary touch, or
pre-irreversible re-read — finds `answered`. Applying it is the FIRST action of that firing.
Unpark, resume or re-plan, retro-correct assumptions, flip to `applied` under the lock.
There is no push channel; answers travel only through registry reads, which the firing
contract makes universal.

**6a — RETRACT (the other terminal state, and NOT an answer).** A question whose premise
collapsed comes off the human's queue with
`retract --actor <raiser> --project <name> --id Q-NNNN --reason "<what collapsed>"`.

- **Raiser only, `open` only, reason required.** The raiser is the one who can know the premise
  died; an `answered` row carries a decision, and retracting that would discard someone's answer
  rather than withdraw a request; and a row that leaves the queue without saying why is
  indistinguishable from one quietly hidden.
- **It is NOT `answered`, because nothing was decided.** The reason lands in `retraction:` and
  `retracted_at:`, kept separate from the answer triple on purpose — writing it into `answer`
  would record a decision nobody made into the pairing `read --answered-for` prints.
- **`retracted` is CONSUMED**, exactly like `applied`: `_live_ids` excludes it, so no lane may
  point `waiting_on` at a retracted question, and the cull sweeps it.
- **It cannot silently suppress a real halt.** A lane sitting `halted` against a question its
  raiser retracted is reported by `read --stranded` as `question-retracted`, with its own reason
  rather than folded into `question-closed` — "the request was withdrawn, not answered" is a
  different fix from "somebody answered it".
- **Correcting a question is supersede-and-retire; a Note beside a row is not a correction,**
  because a reader of the row has no reason to look for it. Question text is write-once: `ask` the
  corrected question first, then
  `retract --id <old> --reason "premise expired: <clause>; superseded by Q-NNNN"`. An `answered`
  question cannot be retracted; if its premise expired after the answer, ask a new question.

**Why it had to exist.** Before it, a question had no terminal state but `answered`: `withdraw` is
Queue-only and refuses a `Q-` id at the arg guard, and `cull` reports `0 questions`. So retiring a
question that should never have been asked cost **exactly the human attention it wrongly spent** —
the human had to answer it to clear it. Q-10030 is the row that needed this and could not have it.

**7 — CULL.** the steward moves `applied` **and `retracted`** blocks to `registry-log.md`. Both
are consumed; neither is waiting on anything.

### What `repair` may not touch

`repair` is a **structural** verb. It exists so a line that no longer parses can be made to parse
again — a mangled `### Q-NNNN` header that orphans a whole block, a bullet the field regex cannot
read, a stray line, a bad `urgency` enum. It is not a lifecycle verb and it holds no authority
over what a question *says*.

**CODE-enforced (`repair_question_field` / `repair_decision_record`, exit 2):** no `repair`,
filed under **any** section, may change what a question RECORDS —

`state` · `answer` · `answered_by` · `answered_at` · `raised_by` · `raised_at` · `assumption`
— plus `question` · `work` once the question has left `open`
— plus `retraction` · `retracted_at` once the question is `retracted`

**The rule is about EFFECT, and the effect is measured on the PARSED document.** Before and
after the replacement, `repair` builds `qid -> (state, ((key, value), ...))` from
`parse_registry`. The keys are `state`, `answer`, `answered_by`, `answered_at`, `raised_by`,
`raised_at` and `assumption` in every state; `question` and `work` are added once the question
has left `open`, and `retraction` and `retracted_at` once it is `retracted`. A field missing from
the block reads as `-`. `repair` refuses if any question that existed before is missing
afterwards, if any question's tuple changed, or if a question appears for the first time in any
state but `open`. Two narrower versions of this rule shipped first and each was
demonstrated open with a live `rc=0`, `validate`-green run:

1. **Guarding `--old-line`** — what the caller *typed*. The field parser is last-wins and
   `state` is the first field of a block, so overwriting an *unprotected* line (`- work: E-0038`)
   with `- state: answered` shadowed the real state.
2. **Comparing the protected field LINES** — text, with no notion of ownership. `### Q-NNNN`
   headers are what assign fields to questions, so deleting one header merges two blocks and
   lands a real human answer on a *different* question, with every protected line still present,
   in the same order, byte-identical.

**The parsed check runs on EVERY repair, whatever `--section` says.** That is not belt-and-braces:
`--section` is a string the caller typed, section headings are mapped in file order with the
**last one winning**, and `## Blockers` sits *after* `## Questions`. So a run of
`repair --section Blockers` could write a whole question block over filler blocker rows and crown
it with a second `## Questions` heading — after which every consumer reads the planted section
and no Questions-gated guard ever executes. Displacing the real section removes every existing
qid from the map, and the removal check is what catches it.

Naming a protected field in `--old-line`, and the line-level comparison, are both still in place
ahead of the parsed check, purely because they can name the exact field you asked for. The parsed
comparison is the backstop that closes the class.

**`question` and `work` join the protected record once a question leaves `open`.** A decision is
the (asked, answered) **pair**, and neither half means anything alone: a real "Yes, go ahead." on
a trivial lint question becomes approval to merge unreviewed if `question:` can be repointed under
it, and `work:` is the only binding `read --answered-for` actually prints, so repointing that
walks a real answer onto a different epic. While the question is still `open` both stay
repairable — there is no pairing to break yet, and an open question with a mangled `question:`
line is exactly the malformed row `repair` is for.

**A question the parser cannot see may be recovered only as `open`.** Repairing a mangled
`### Q-NNNN` header brings its block back, which is the point; a block that arrives already
carrying an answer was authored, not recovered, so that is refused.

**No repair may change the heading skeleton (`repair_heading_skeleton`, exit 2, gh#192).** The
skeleton is the ordered list of every line that begins with `#`: section-level headings (`#`,
`##`) by their text, block headings (`###` and deeper) by their level. The check reads the whole
file, before and after, whatever `--section` says. The decision-record check stops the
*consequence*; this stops the *primitive*. Without it, on a registry whose `## Questions` was
empty there was no qid to lose, so a run of `repair --section Blockers` could plant a whole block
crowned with a second `## Questions` — shadowing the real section and conjuring an `open`
question attributed to a lane that never asked it, every step `rc=0`. So a repair may not turn a
row into a heading, delete a heading, or rename a `##` heading — including restoring a damaged
`##` section heading, which is a human's job (git history). A `###` header mangled *in place*
stays repairable, because keying block headings by level leaves the skeleton unchanged; that is
now the only way a new qid can appear, and only for a block that was already on disk.

**A blank `--old-line` is refused (`repair_blank_old_line`, exit 2, gh#558).**
Every section carries blank separator lines, and `--old-line` is matched by exact equality, so an
empty or whitespace-only `--old-line` used to match a separator and turn the verb into an insert:
`repair --section Directives --old-line ''` minted a standing Directive, which only dev-manager may
write, and the same route added `## Blockers` rows, every step `rc=0`. A blank `--old-line` is now
refused before the scan, in every section, and the file is left untouched.

**`## Directives` is refused outright (`directive_devmanager`, exit 2, gh#569).** Only dev-manager
writes it, and it has `directive` and `expire-directive`; replacing the section comment every
registry carries minted a standing instruction with `rc=0`. A damaged Directive is a human's to
restore (git history).

**A boilerplate `--old-line` is refused (`repair_boilerplate_line`, exit 2, gh#569).** Every
registry carries the same scaffolding, so replacing any of it with a row is an insert, exactly as a
blank `--old-line` was: a table **header** or **separator** row, the Queue's `- **Deploy lock**`
line (whose replacement also deleted the lock) or `- **Notes**` line, and any section **HTML
comment**. The check is on the line's shape before the scan, in every section, and the file is left
untouched. It refuses only *intact* boilerplate: a separator mangled so that it no longer parses as
one is exactly what `repair` exists to restore, and stays repairable. The same holds for a
Deploy lock line whose `holder:` / `taken-at:` no longer read: it is not the intact line, so it
is a valid `--old-line`, and writing the readable line back over it is not a second one. There is deliberately no
row-count guard: a wrong-column row is dropped by the parser, so fixing one legitimately makes a
row appear.

**In a table section, a `--new-line` must be a row of that table (`repair_not_a_row`, exit 2).**
`## Agents`, `## Queue` and `## Blockers` are tables, and every parser reads only their `| ... |`
rows. Any other line is inert there, so writing a bullet or a line of prose over a real row
deleted that row with `rc=0` and `validate` green, and a run of them planted a question block line
by line inside `## Blockers`. A table section may now receive a `| ... |` line (its column count is
`repair_fails_validate`'s to judge), a blank line (removing a stray line writes nothing), and in
`## Queue` the Deploy lock or Notes line, which `repair_boilerplate_line` already refuses while an
intact one is there. Bullets and prose are refused by shape before the lock. A heading-shaped
line that changes the skeleton is refused as `repair_heading_skeleton`, above; one that leaves the
skeleton unchanged (a block heading mended in place) is refused here, in a second pass after the
parse. The file is left untouched.

**A Queue Note bullet is never a repair target (`repair_note_line`, exit 2).** Notes are owned by
`note --strike`, which lets a lane strike only what is routed to it or to `fleet`, and by the cull,
which removes struck Notes and logs each one verbatim. `repair` bypassed both: replacing a live
Note with a well-formed Queue row minted that row and silently deleted another lane's Note, `rc=0`
and `validate` green, and a blank `--new-line` over a Note deleted it outright. Any `--old-line` in
`## Queue` that the striker itself would read as a Note — a `- ` bullet that is not the Deploy lock
or Notes line, struck or live — is refused before the lock, and the file is left untouched. One
exception, because by that same rule a mangled `- **Deploy lock**` or `- **Notes**` line *is* a
Note bullet: writing the intact Deploy lock or Notes line back over it stays allowed, and a second
copy of one that is already intact is still refused as `repair_boilerplate_line`. A Note
whose `- ` marker was mangled away is no longer a Note to any parser, so this key does not cover
it; writing the bullet back is a non-row line in a table section and is refused as
`repair_not_a_row`. `repair` therefore neither removes nor writes a Note bullet in either
direction: the sender re-`note`s a mangled Note, and the stray line it left stays removable with a
blank `--new-line`.

**A well-formed Blockers row is never a repair target (`repair_blocker_owned`, exit 2).** Every
cell of a row `validate` accepts has an owning verb: `raise-blocker` creates it, `assign-blocker`
moves its owner, `resolve-blocker` (the owner) closes it, and `cull` sweeps it once resolved.
`repair` bypassed all of them: rewriting a healthy row's owner from `lead-dev` to `steward` left
`validate` green with `rc=0`, and the steward could then resolve a blocker it was never assigned,
walking round the ownership limit on `assign-blocker`. On a row the row checks reject (a bad
state, `raised_by` or `owner`), only the flagged cells may change: `owner` only to `-`
(`assign-blocker` picks the next one) and `state` only to `open` (`resolve-blocker`, by the owner,
closes it), and deleting such a row is refused. A wrong cell count, an id the parser drops, or a
duplicate id, which no owning verb can reach, stays wholly repairable, deletion included. The file
is left untouched.

**A well-formed Queue row is never a repair target (`repair_queue_owned`, exit 2).** The same rule
on the Queue table: `publish`, `relane` and `withdraw` (lead-dev) own the id, work item, lane and
notes, and `claim` and `release` (the claimant) own `claimed_by`, `state` and `waiting_on`, with
`reap` clearing a dead claimant's. `repair` once moved a healthy row to another lane, claimed it
for a builder and set it `building`, `rc=0` and `validate` green. So on a row that has the
Queue's seven cells and passes every row check `validate` runs (the `lane`, `state` and
`claimed_by` enums, and a `waiting_on` that names a live question or blocker), `repair` may not
rewrite any owner-held cell: it refuses any change to the row at all, a whitespace-only one or a
deletion (a blank `--new-line`) included. A reshaped `--new-line` never reaches this guard; it is
refused earlier as `repair_fails_validate`. The refusal names the cells the change would have
touched and the owning verbs they belong to.

Three kinds of Queue line stay fully repairable, in every cell, and may be deleted with a blank
`--new-line`:

- a **duplicate id** (the same id on more than one Queue line), because every owning verb refuses
  it as `id_duplicate`;
- a line **without exactly seven cells**, which the parser cannot read as a row at all;
- a row whose **id is malformed** (it fails the Queue id pattern, `_E_ID`). The parser drops that
  row, so no owning verb and no cull can reach it, because they all address rows by id through the
  parsed board. `validate` reads it off the raw line instead and exits 2 naming it
  (`Queue: bad id '...'`), so repair is the one way back. Without this, the only way to remove one
  was to invent an id, withdraw the row and cull it, which fabricates a log record and receipt
  Notes.

The same key rules, one table the parser and `validate` both read (`_ROW_KEY_RULES`), cover the
other two tables: an Agents row naming no fleet loop exits 2 as `Agents: bad agent '...'`, and a
Blockers row whose id is not `B-NNNN` (`_B_ID`) as `Blockers: bad id '...'`. Each such row is
dropped by the parser and stays wholly repairable, deletion included.

**Adding a seat.** The seats are the `AGENTS` tuple in `registry.py`, and the key rule above
reads it, so ship a new seat in this order: first merge the change that adds it to `AGENTS`;
then deploy skills (`python install/provision.py --deploy-only`); then fast-forward every lane's
checkout; only then let the new seat write its first registry row. A lane still on older code
has the old `AGENTS`, so its parser drops the new seat's row and its `validate` exits 2 as
`Agents: bad agent '...'`. Every lane runs `validate` fail-closed at wake, so one early row
freezes the fleet.

Ids use ASCII digits only (`0-9`): an id spelled with any other decimal digits is refused, as a
row key and as an argument. A Questions line that starts with `#` (or is qid-shaped) but is not a
readable `### Q-NNNN` header exits 2 as `Questions: unreadable block header '...'`: a block ends
only at the next readable header, so the stray header's fields would otherwise merge into the
question above it.

**On any other Queue row the row checks reject, `repair` may change only the flagged cells (also
`repair_queue_owned`, exit 2).** The checks are the row checks `validate` runs, which `repair`
reads off the raw line, and each judges exactly one cell, save one: a bad lane enum flags `lane`, a bad
state enum `state`, a bad `claimed_by` enum `claimed_by`, and a dead referent `waiting_on`.
The exception reads two cells: an in-flight `state` with no claimant is filed under
`claimed_by`, the missing cell, and its mend is the coupled one below. A
repair of such a row passes only when its `--new-line` leaves every unflagged cell as it was, so
mending a corrupted `state` may not also move the row's lane, claimant or notes. Two flagged
cells may be mended in one repair. A flagged owner-held cell, `claimed_by` or `waiting_on`, may
only be cleared to `-`, never pointed at an agent or a live referent. Where `-` alone would strand
the row, the mend is pinned, with `state` as a coupled cell: a flagged `claimed_by` on a
`building`, `parked` or `review` row takes exactly what `reap` writes (`claimed_by` `-`, `state`
`ready`, `waiting_on` `-`), and a dead `waiting_on` on a claimed `parked` row takes what `release
--state building` writes (`state` `building`, `waiting_on` `-`, the claim kept), or `reap`'s
write if the row is unclaimed. No mend may leave or put an in-flight row unclaimed: `validate`
flags that shape, so such a repair is refused as `repair_fails_validate`, before ownership is
judged. A mend may still not leave the row `parked` with `waiting_on` `-` or
blank, a shape `release` refuses and `validate` cannot see; that refusal points to the
claimant's `release` or, for a dead claimant, the steward's `reap`. A blank `--new-line` (a
deletion) is refused, and the refusal lists every unflagged cell deleting the row would remove;
a reshaped line is refused earlier as `repair_fails_validate`. The refusal lists the problems
the row checks reported, the flagged cells `repair` may mend, and the unflagged cells it tried to
change, with their owning verbs. The file is left untouched.

**A well-formed Agents row is never a repair target (`repair_agents_owned`, exit 2).** Every cell
of a row `validate` accepts is written by the lane itself through `status`, which also stamps the
heartbeat from the clock, or by the steward through `stand-down` and `reap`. `repair` bypassed all
three: rewriting a stale row's heartbeat and eta to fresh ones left `validate` green with `rc=0`,
and `read --stale` stopped reporting a dead lane. On a row with a bad status only `status` may
change, and only to `idle`, so a halted lane mended this way reads as `idle` until its next
`status` write; `idle` keeps the missed-wake alarm on, where `halted` would silence it. An unknown
agent, a wrong cell count, or a duplicated agent, which every owning verb refuses as
`id_duplicate`, stays wholly repairable. The audit Note names the agent. The file is left untouched.

**What these three keys do not close.** Shape cannot tell restoring a row from minting one. A stray
non-row line already sitting in a table section — prose, or a bullet that is not a Note — can
still be replaced with a well-formed row, and that row is new. Restoring a pipe-stripped row is
exactly that edit and must keep working, so `repair` does not refuse it; the audit Note every
successful `repair` appends is the only record that the row arrived that way. That Note names the
row by its id (or, in `## Agents`, its agent; otherwise its line number), never by quoting caller text; when a blank
`--new-line` deletes a duplicate, the name is read from the line it replaced.

**A result `validate` would refuse is refused (`repair_fails_validate`, exit 2).**
Re-parsing proves only that the file still parses; `validate`'s checks are stricter, and a
repair is held to them. `repair` runs `validate`'s own checks -- the function `validate` prints
from -- on the board before and after the replacement, and on a probe board with the target
line deleted, and refuses when the result

- **damages** the board: it loses a table (`file_damaged`) or the parse that the board still
  had, as replacing the `## Blockers` separator row with a data row would;
- **adds** a row violation the board did not already have: a valid row repaired into
  `bad state`, a row repaired to another wrong cell count, a key repaired into a duplicate, or a
  blocker resolved out from under the parked row that waits on it. A duplicate whose count
  merely shrinks (`appears on 3 rows` becoming `2`) is the same violation, not a new one; or
- **keeps** a violation the replaced line owned. A line owns a violation when deleting that line
  alone would clear it, so a repair that edits a broken row without fixing it is refused too.

Row violations are computed even while a table is lost, so damage never hides them: restoring a
lost separator is judged on the separator, not on every row problem the board already carried.
Violations already on the board elsewhere block nothing, so a board broken in several places is
mended one line at a time. A refusal writes nothing and posts no audit Note. A boilerplate line
replaced with a row is refused earlier, by shape (`repair_boilerplate_line`, above), so the
separator case here is now defence in depth.

Everything that does not change a decision line stays repairable — the mangled `### Q-NNNN`
header, the bad `urgency` enum, a stray line, a bullet the field regex cannot read. Refusing the
whole section outright would close this hole by opening another: a `## Questions` block that
nobody can repair at all.

**Restoring a *malformed* decision line is refused too, and that is the decided policy, not an
accident.** A decision field whose colon is broken is invisible to the parser, so writing it back
into shape is a write to the human-decision record — the exact act this verb may not perform,
whatever shape the line arrives in. `repair` is the one verb that could fabricate consent, so it
is not the one allowed to reconstruct it: a structurally broken decision field **escalates to the
human**. The refusal message says so.

Why those seven. Question rows are the fleet's only human channel and **promotion IS approval**,
so the first six fields (`_Q_PROTECTED_FIELDS`) *are* the record that a person was asked and
replied. `answer`/`answered_by`/`answered_at` are that record directly; `state` is the promotion
itself. `raised_by` is on the list for a sharper reason: the `apply` guard READS it to decide who
may flip `answered -> applied`, so a verb able to rewrite it would be editing the input to the
guard that polices it. `raised_at` is what the fleet times a question by: triage ranks open
questions oldest-first within an urgency class, and the stranded-halt alarm measures how long an
answer has been owed from it, so a rewritten stamp reorders the human's queue or keeps the stranded-halt
alarm quiet. The seventh, `assumption` (`_Q_RAISER_FIELDS`), is in its own tuple because it is not a
human decision: it is the raiser's own words. On an advisory question it is what the raiser is
proceeding on, and an answered question carries it into the decision the human read, so a
steward-written assumption puts words in the raiser's mouth. Only the raiser changes it: retract
and re-ask while the question is `open`, or `apply` once it is answered. `urgency` and `default`
stay repairable on purpose. Before this rule, `repair --section Questions` could write `answered_by: human` onto
a question no human ever saw, exit 0, and `validate` would still print `registry valid` — the
only Questions check it runs is the `answered_by` enum, and `human` is in the enum. `validate` is
therefore not a backstop for any of this: it never had an opinion about who wrote the answer.

**There is no CLI path to correct a recorded answer.** That is deliberate here and not an
oversight: the lifecycle writes an answer once (`answer` on an `open` question) and consumes it
once (`apply`, by the raiser). A wrong or superseded answer is corrected by `ask`ing a fresh
question that supersedes it, not by rewriting history. The missing correction verb is tracked as
**gh#129** and is out of scope for this guard — do not improvise one.

**Every successful `repair` leaves an audit trail.** It appends one Note routed
`steward -> fleet` reading `repaired ## <Section>: <id> <field>`. The note names the target by
derived id and field only — it never quotes `--old-line` or `--new-line`, because an echoed
excerpt of a line that reads like a Note (a mangled one, or Note text pasted into a row) would sit
above the note it quoted and swallow the `note --strike --match` meant for it. Repair audit notes are machine
notes: they do not count against the steward's own note-burst budget.

---

## Liveness sweep (steward since E-0220, every wake — never rotated)

**Missed wakes are a separate, earlier check:** `read --missed-wakes` lists rows past their OWN
declared `eta` by more than 30 minutes with no heartbeat since — a lane that promised a wake and
did not take it. It is report-only and **ntfy-only, never a relaunch** (see the single-daemon rule
below). A declared halt, a reaped row and a **stood-down** row are never alarmed on — every
exclusion is readable off the row's own text, never a list anyone maintains elsewhere.
"Stood-down" means the steward's registry `stand-down` verb, never the backoff's
`action=stand_down`: a lane that takes that action is judged like any other row, and stays quiet
by declaring the `eta=` its answer line carries — the recovery guard's next fire plus the
scheduler's jitter — as its row's `eta`.
Staleness below asks whether a lane
is DEAD; this asks whether its wake FIRED, and the two must not be conflated.

**The same read also lists `LAPSED ETA` rows, and they are a DIFFERENT class (gh#281).** A lane
that keeps re-stamping its heartbeat over an `eta` it never advances is reported by neither of
the other two checks at any threshold: its heartbeat never ages out, so staleness is silent on it
forever, and its heartbeat is always newer than its eta, so the missed-wake rule skips it. It is
the shape `--heartbeat-only` manufactures — that verb preserves `eta` verbatim by design — so it
is the ordinary residue of an honest long build that nobody ever re-declared after. The two
classes stay separate in the output because **their remedies differ**: `MISSED WAKE` means go and
probe a lane that may be dead; `LAPSED ETA` means tell a lane that is demonstrably alive to send
one full `status --eta`. Its grace is **120 minutes**, not the wake alarm's 30, and is **not**
tunable by `--grace-minutes` — at 30 it would fire on the whole fleet during any multi-hour epic,
and an alarm that fires on correct behaviour is one the reader mutes. The two predicates are
disjoint by construction (`hb <= eta` versus `hb > eta`), so no row is ever reported twice.

**A row neither check can judge is printed as `INVISIBLE to the missed-wake alarm`**, with the
reason: `no-eta` (the eta cell does not parse), `no-heartbeat` (the heartbeat does not), or
`eta-too-far` (both parse, but the eta is more than `ETA_HORIZON_HOURS` past the row's own
heartbeat, so the alarm would wait that long before judging it — gh#682; never the exempt
`dev-manager` row). Such a row is not
counted in the all-clear's `row(s) checked`.

**Who sweeps is the steward. What watches from OUTSIDE is `python -m agentflow.wake_watch
--project <name>`.** The distinction is load-bearing: the sweep runs inside the steward's own
wake, and the steward is one of the lanes it watches, so a stalled steward is the one row nothing
in the fleet can report — three times live, and on 2026-09-04 nothing had died at all, the timers
simply stopped. `wake_watch` is the same `missed_wakes()`, `lapsed_etas()` and `blind_rows()` — **reused, never
restated**, so the three exclusions cannot drift into two answers — run from a shell with no loop
alive, pushing findings to ntfy. It is **report-only and never relaunches**, for the reason above.
**Its exit code says whether the WATCHER could do its job, never what it found**: 0 on a stalled
fleet, non-zero only when the watcher itself could not run or could not deliver (unreadable
registry, unresolvable config, a notify that raised). A scheduler has one failure channel, and a green exit from this command is a statement
about the command, not about the fleet — the push is the only report. `--no-notify` prints the
report and sends nothing, for a dry run against a live board.

**So WIRE THE JOB TO CAPTURE STDOUT AND STDERR, not only the exit code.** That is the whole
mitigation, and it is one line of scheduler config. With `NTFY_URL` unset and a lane genuinely
stalled, this command exits **0**, writes the finding to **stdout** and a warning to **stderr**:
`notify()` returns `False` when it was switched off by `notify.kind: none` or found no destination,
but this command does not read that value (its `pushed` means a send was attempted), so delivery is
not observed from inside the command, and a job reading the exit code alone reports permanent
health over a fleet nobody is watching. The report is written to stdout
**before** any push is attempted, so an ntfy outage costs you the ping and not the finding. One
correlation to know and not to read backwards: a push is only attempted when there is something to
push, so a broken ntfy surfaces as exit 1 **only on a board that had findings**.

**THE SINGLE-DAEMON RULE — this command's scheduled invoker is the one daemon the suite runs.**
The architecture was daemon-free by design, and that invariant was **revoked on 2026-09-22**
(verbatim: *"revoke the rule. Single daemon only is the new rule until it is
challenged"*). The reason is structural rather than a preference. Three blackouts — 2026-09-08,
2026-09-16 and 2026-09-19 — stopped every lane for days at a time, were reported by nothing, and
each ended only when a human relaunched by hand. An **in-fleet** detector cannot close that class:
any in-fleet detector is itself an agent firing, and a quota or rate-limit window (the account's,
or a single model's) refuses *every* model turn it covers, that detector's included, so the only
invoker that survives the failure is one
that takes no model turn — and every non-model invoker is a daemon. So the suite now runs exactly
one: an **OS-scheduled, report-only** invocation of this command (Windows Task Scheduler on the
fleet's machine, or a cron on another host), which pushes to ntfy and **never relaunches, never
merges, never writes**. **That budget is one and it is now spent** — a second daemon is a new
decision, not a precedent this one sets. Wiring that entry is machine-level ops and belongs to
the installer and the operator, never to this detector's own code: `wake_watch` registers nothing
and starts nothing. `install/provision.py` step 12 does the wiring, **only when the operator opts
in with `--wake-watch`** (off by default, gh#544) — one hourly task per project on Windows,
idempotent by name; elsewhere it skips and prints the crontab line, because a provisioner that
rewrote a crontab could drop the operator's own jobs (`docs/INSTALL.md` §13). So **an entry is
absent unless the operator opted in**, and its absence is invisible from inside. A human typing
the command is still a valid caller, and the steward's own sweep still runs the same check from
inside. It is drilled from outside a session by
`harness/missed-wake-drill/missed_wake_drill.py`.

Scan Agents rows for a heartbeat staler than 3h with the eta passed or absent. An eta counts
only up to the 6h horizon past the row's own heartbeat (gh#707; `dev-manager` exempt), so a dead
lane cannot hide behind a far eta; `read --stale` annotates that row `eta beyond 6h horizon`.
**The scan excludes exactly one kind of row: a reaped one**, whose phase is exactly
`reaped by steward (stale)` or `reaped by steward (forced)` (`is_reaped_phase`) — `reap` keeps the
dead eta as evidence and freezes the heartbeat, so a reaped row would otherwise be listed forever.
**A `halted` row is NOT excluded.** A halted lane is blocked on an answer and keeps firing on
schedule, so its heartbeat stays fresh and a frozen one means the lane stopped; and `halted` is the
one state exempt from `eta`, so a dead halted row carries exactly the absent-eta shape this scan
exists to catch. A stood-down row is listed too, under its own mark (below). A stale
timestamp is a *symptom*, not a verdict, so the order is **ping on first sight, probe right
before reap**. On the first sighting (`flag`), send the routed `heartbeat stale, first detection`
ping and nothing more. On the second sighting (`reap-eligible`), run the life probe — a commit on the
claimed branch in the last 3h, a file under the worktree modified in the last 30 min, or a
test-runner/subagent process parented to that lane all mean ALIVE (long reviews and long
workflows are silent on the registry). One cause is worth naming because none of those probes
can see it: a lane launched from anywhere but the machine's launch directory comes up without
the fleet's settings and fails at step 0, before its first write — so a session that is open
and erroring right now presents as a row frozen at its last heartbeat, and the wrong
directory, not the loop, is what to check (`docs/INSTALL.md` §10). Every loop, in turn, must refresh its row at least
every 30 minutes while subagent work runs on its behalf. **Use `status --heartbeat-only` for a
ceiling re-stamp**: it stamps the clock and preserves `state`/`work`/`phase`/`eta`/`waiting_on` **It refuses when the row's own eta is unreadable** (`eta_required`): re-stamping would preserve that cell and keep the row invisible to the missed-wake alarm. One full `status --eta` write clears it.
exactly as the row carries them. The full form makes a loop re-send every column, so a re-stamp
carrying a `phase` captured minutes earlier writes a *fresh* timestamp over work that has already
finished — worse than a stale row, because a stale row is visibly stale and the probe above
catches it, while a fresh row asserting dead work reads as ground truth. Combining
`--heartbeat-only` with any row field is refused before any write. The ping comes first and the
probe second: write the Notes ping on first sight, then **wait one full sweep interval** — if the
heartbeat is still stale at the steward's next wake, run the life probe, and only if it finds the
process gone `reap` the row (which rows that returns and
which it leaves byte-identical is **What `reap` may not touch** above, and is not restated here),
raise a Blocker documenting the return, and Ntfy the human (notification-only). **`reap` holds
the floor itself (gh#575):** it refuses a row `read --stale` would not list (`reap_not_stale`,
judged by `stale_rows`, never a copy) and a steward reaping its own row (`reap_self`), so a
mistyped `--agent` cannot take a live lane's claims and deploy lock. The one escape is
`--force --reason "<who confirmed, how>"`, for a lane a human has confirmed is gone before it
reads stale; the reason is written into the reap Note, which says `(stale)` only when the check
ran and found it so. Only the steward
may run `reap`, since E-0220; the Blocker and the Ntfy are the sweeping seat's own steps around
the verb, not part of it. "Grace" is one passive interval, and **no loop in the fleet ever wakes
another session** — that half of the old daemon-free invariant is untouched by the single-daemon
rule above, which buys one OS-scheduled *reporter* and no relauncher at all.

**What `read --stale` marks, and what carries the ping** (gh#285). Each stale row that is not
stood down is marked `flag` or `reap-eligible`. `flag` is a first sighting of this silence. `reap-eligible` means a
live liveness ping for this silence already exists, so the row is due its life probe; it is
never a reap by itself. The ping is the Notes line the reaper writes: sent by the lane that
holds `reap` (the steward), routed `--to` exactly the observed lane, with text that STARTS with
`heartbeat stale, first detection`, and stamped after that lane's current heartbeat. Anything
else - a note that merely says "stale", the opening quoted mid-text or quoted back by another
lane, a routing arrow inside a body - leaves the row at `flag`. The steward never strikes its
own ping (it cannot: `note_not_yours`); a ping expires on the lane's next heartbeat, which is
what keeps a relaunched or resumed lane from inheriting an earlier silence's ping.
The exempt `dev-manager` row is never `reap-eligible`, ping or no ping: it stays `flag`, is
reported as away past its declared return, and `reap` refuses it (`reap_exempt_seat`). Its
heartbeat moves on every write the seat makes, so a listed row means the seat has written
nothing for over 3h past its eta.

**A stood-down row carries a third mark, `stood-down: <reason>`** (gh#683), whatever pings exist.
It stays in the report because `stand-down` leaves its claims and deploy lock in place, but it is
never a ping-then-reap candidate: a human stopped that lane, so its life probe always finds the
process gone. The sweep escalates it to dev-manager with one Note, and `reap` refuses it
(`reap_stood_down`) unless `--override-stand-down` records that dev-manager decided the lane is
not coming back. That flag does not lift the liveness floor: a stood-down lane `read --stale`
does not list yet is refused `reap_not_stale` too, and is reaped with `--override-stand-down
--force --reason "dev-manager decided <lane> is not coming back (<ref>)"`; the dev-manager
decision is the human confirmation `--force` requires.

**The deploy lock is two different cases and the sweep only closes one of them.** `reap` clears
the lock when the lane being reaped is the lane holding it — that is part of the verb, and it
falls out of the sweep for free. A lock that is merely ORPHANED — held past the 45-minute rule
with the holder's heartbeat stale, but the holder not being reaped this pass — is NOT the
steward's to clear. `deploy-lock` is deliberately absent from the steward's exception list in
the Cadence report below, and the capability map bars it two different ways that are worth
keeping straight: `deploy-lock take` is a ROLE restriction (builders only, so the steward is
refused outright), while `deploy-lock release` carries no role restriction at all and is
instead `bound_by` **only the current holder** — a steward that never acquires the lock is
never its holder, so there is nothing for it to release. The steward raises a Blocker owned by
steward carrying the evidence plus a `note` to the holder, and its owned-Blocker sweep closes
it: the holder releases the lock, or a probe-proven `reap` of the holder clears it
(`skills/steward/SKILL.md`, "The deploy lock — LOCK-AWARE, NEVER LOCK-HOLDING"). Conflating the
two is how a heading retitle silently reassigns a duty: this sentence was written under a
heading that named dev-manager, and said nothing false until the heading above it changed.

### Cadence report — steward, read-only evidence (never a verdict)

The 30-minute in-flight refresh above is **reported, never enforced**. On any wake, **steward
MAY** compare every `## Agents` row — `idle` rows included — against that row's own declared
cadence and route the measurement to dev-manager as evidence: a single
`note --actor steward --project <name> --to dev-manager`, one line per wake. Five constraints,
all load-bearing:

- **A measurement, not a liveness verdict.** Report the measured heartbeat age, the cadence it
  was compared against, and WHICH rule supplied that cadence. Never "X is dead", never "X
  should be reaped": the life probe and the reap are BOTH the steward's since E-0220.
- **Never `raise-blocker`, and never a reap request.** The Blocker path for stale rows is
  already the liveness sweep's; two Blocker paths about the same rows is how a double-reap
  happens. Discharge is the `note` plus steward's own state file — no Blocker, no Ntfy.
- **Never a write to another loop's row.** No `reap`, `repair`, `status`, `claim`, `release`
  or `deploy-lock` against a row that is not steward's own; `reap`, `stand-down` and `repair` are the deliberate exceptions and are the steward's alone
  (see the ownership summary). The report is evidence dev-manager may act on, never a request
  that it act.
- **Cadence is not a column.** The `## Agents` schema carries no cadence field, so every
  number is resolved from outside the registry — the row's own `eta` once past, the 30-minute
  in-flight ceiling, or the loop's declared wake interval in its own SKILL.md. The report MUST
  name where it read each cadence from, so a cadence that has since changed shows up in the
  evidence instead of silently skewing the comparison.
- **Noise control, or dev-manager learns to skim it.** ONE note per wake, hard cap. Exclude
  only what `read --stale` excludes: a reaped row. A `halted` row is measured like any other — a
  halted lane is blocked on an answer and keeps firing on schedule, so its heartbeat stays fresh,
  and a frozen one means the lane stopped. Escalate the wording only after
  the same row measures stale across three consecutive wakes, keeping that per-row streak in
  steward's own state file; below three, state the number flatly and move on. On an idle fleet
  every row passes its cadence within the hour, so without this the report is a full table
  every wake and stops being read — which costs more than not sending it.

---

## Human surfaces — exactly one

| Path | Status |
|---|---|
| dev-manager in-session asks (asking-questions protocol) | **the only question channel** |
| lead-dev in-session asks / blocker surfacing | removed — Question rows |
| per-epic dispatch confirms (any builder) | removed — promotion IS approval; Ntfy notice + Directives veto |
| orchestrator gates inside fleet-launched runs | removed — Question rows via the fleet invocation context. Step 8 and Step 9.1.5 were the last two gaps and both landed in #12: Step 8 raises a Question row (`urgency: waiting`, default *"Pause for spec review"*), Step 9.1.5 skips outright on a fleet run and raises nothing. Their `KNOWN_UNBRANCHED` entries were **deleted, not reworded** — a stale entry would go on exempting whatever section later occupied that heading. Two entries remain and neither is an unbranched gate: Step 9.2 is covered by state rather than the marker, and the escape-hatches list is not a gate at all. **What the gate cannot prove (#28):** it is SECTION-scoped, so a new `AskUserQuestion` added inside an already-branched section passes green |
| `project` gates inside fleet-launched runs (Routing, Init check, Dispatch, Resume) | removed — Question rows; the `fleet-context` marker arrives from the builder that invoked it, and `project` propagates it into `solution` and the orchestrator bootstrap |
| `solution` gates inside fleet-launched runs (Dependencies, Unknown slug, technical decision-gates) | removed — Question rows; `solution` has no `status.md` to inherit from, so its mode comes from the marker its caller passes (`project`, or the orchestrator invoking it directly) |
| orchestrator / `project` / `solution` gates in standalone interactive runs | kept — a human at the keyboard IS the surface there |
| `choose-next` epic picker | kept — **exempt on purpose, not overlooked**: it is a human-seat picker with no builder caller (nothing in the fleet invokes it unattended), so it keeps its buttons |
| Ntfy (all loops) | notification-only, never question-bearing |
