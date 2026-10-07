# Changelog

## Unreleased

- **Provision no longer sets `$AGENTFLOW_CONFIG` to point its own readers at a config home.**
  `main()` and the presence check each set the variable for the length of a call and restored
  it after. The variable is process-global, so two overlapping in-process runs each saved the
  other's value as the one to restore: the first to finish left the second reading the shell's
  home, or the other run's. `config_home`, `environment_path`, `load_environment`, `load_project`
  and `resolve_project_name` (and doctor's `diagnose`) now take an optional `config_home`; left
  out, they read the variable exactly as before. Provision passes its home to every in-process
  reader, and gives its child processes (the bootstrap step, the venv handover) a copy of the
  environment carrying it, as they inherited before. `os.environ` is no longer written.

- **One function writes `settings.local.json`.** The JSON spelling (indent 2, non-ASCII kept as
  UTF-8, lone surrogates falling back to escapes, LF, no trailing newline, through a symlink to
  its target, atomic) was copied into provision and the auto-allow bootstrap. Both now call
  `atomic_io.write_settings_json`, so the two writers of the one file cannot drift apart.

- **Junior lanes find their rows with `read --claimable`, not a grep for `lane: jr-dev-N`.**
  `## Queue` is a markdown table, so that literal matched nothing and a lane could report itself
  empty while rows were claimable. The jr-dev skills now name
  `python -m agentflow.registry read --claimable <lane> --project <name>` and describe `lane` as a
  column; the cross-lane claim ban is kept. The lead-dev, steward and dev-manager
  `open_issue_page` passages now confirm the old and new value with `agentflow.config show`. A
  prose test pins both, and parses the cited command through the registry's own parser.

- **choose-next offers drafted and approved epics, and hands `/project` a file that exists.**
  One predicate, `is_candidate`, now defines the candidate set for the skill and `blocked_status`.
  `blocks_count` skips dependents whose run is over. MISSING-DEP names a dep spelled as a file
  stem, and cards flag unapproved picks. `handoff_path` passes the surveyed spec or plan, never a
  path outside `data_root` or an ambiguous match, instead of a rebuilt path that missed records.

- **The steward never runs a command a refusal names, and trusts a worktree removal only once the
  directory is gone.** `worktree remove` deregisters a tree even when a shell inside it makes the
  delete fail, so the note asks the human to close such shells first and the next pass re-runs
  the guard on the path. A prose test pins both rules.

- **`provision --deploy-only` makes a skill change live without touching any project's data.**
  It runs step 7 (deploy skills, commands and the hook script into the Claude config dir) alone:
  no preflight, venv, hooks, presence checks, closing report, and above all no step 11, which
  seeds every configured project's `data_root` subtree. A lane deploying a `SKILL.md` change had
  no way to do it without writing into other projects' subtrees, which SCHEMA §6 forbids; that
  rule now names `--deploy-only` as the lane's path and keeps the full install's scaffold as the
  one documented exception. `--deploy-only` refuses `--resume-from`, `--autodoc` and
  `--wake-watch` (usage error, exit 2), since each asks for a step it does not run; the skip
  flags and `--force` are accepted. `--dry-run --deploy-only` lists step 7 alone. Run it from
  the primary checkout after the merge lands: deployed skills point into the checkout that ran
  the deploy, so `--deploy-only` refuses a suite root that is a linked git worktree (exit 2),
  which would leave every seat pointing at a directory deleted after merge.

- **The auto-allow bootstrap writes `settings.local.json` the way provision does.**
  `skills/orchestrator/bootstrap.py` now writes the file atomically through the shared
  `atomic_io` helper, keeps non-ASCII text as UTF-8 instead of `\uXXXX` escapes, and leaves the
  file untouched (bytes and mtime) when it has no hook to add. Its spelling matches provision's,
  so the two writers of the one file no longer flip it between CRLF and LF on Windows. Separately,
  the error raised when a replace retry runs out (`ReplaceRetryExhausted`) names the errno and
  each path once instead of twice, and keeps the destination path and any Windows error code.

- **`provision.main()` no longer leaks its argv into the next call.** The argv the venv handover
  passes to the child was a module global that `main()` set and never reset, so a second
  in-process caller that handed over spawned its child with the first caller's flags (its paths,
  its `--resume-from`) spliced in. The argv now travels on the run's own `Options.invoked_argv`,
  so overlapping in-process calls, including calls on different threads, never swap flags. A
  one-shot command-line install was never affected.

- **Spec-corpus warnings are named apart from config warnings.** `doctor` reports epic-survey
  problems (a spec with no `## Files` section, a pathless one, a slug collision, and the rest) as
  `survey:<kind>` WARN findings. They used to end the run, and every install, with "healthy, but
  NOT everywhere as configured", a config-clamp verdict over a spec problem. A run whose only
  warnings are `survey:<kind>` now ends `resolved surface healthy and configured as intended; N
  spec-corpus warning(s) (survey:*)`; a run with config warnings too keeps the config verdict and
  adds the spec count. The bare `survey` catch-all (a message no kind recognised) still counts as
  a config warning. Exit codes are unchanged. The steward's resolved-surface step rolls
  `survey:<kind>` rows up into one line per kind with a count instead of one finding per row.

- **`provision --autodoc` warns instead of refusing.** An explicit `--autodoc` or `--no-autodoc`,
  and now `AGENTFLOW_AUTODOC` too, is honoured when the resolved config disagrees, with one
  `WARNING` line naming what decided (the variable's raw value included, so an unrecognised value
  that reads as off is visible). Only an answer from the config scan, or no answer, is still
  corrected. The deciding tier is recorded on the install options (`autodoc_source`).

- **Fixed issues are closed by the auditor, never by the seat that built the fix.**
  `python -m agentflow.issue_intake fix-claims --project <name>` lists the open issues a merged PR
  (its title's `gh#N`, a `Source:` line, or a line-start closing keyword) or a done spec's
  `source:` claims to fix, with who built each fix and whether a closing seat has checked it.
  `issue_intake close` is the only mediated close: it refuses, in order, `actor_may_not_close`
  (any seat but the auditor and dev-manager), `not_open`, `fixer_is_actor`, `fixer_unknown` (any
  seat but the auditor) and `evidence_shape`, then posts the verdict comment and closes. The
  auditor gains a fixed-issue pass on every wake (Step 4b) that checks each ask on the base branch
  and closes, leaves a remainder, or routes the judgement to dev-manager; the steward reports
  closure debt and closes nothing; dev-manager closes a fixed issue only on the human's word and
  through the same command; builders never close. PR-PROSE.md gains *Citing an issue* and the
  *Fixed-issue verdict comment*, and the `PR provenance gate` now fails a same-repo PR (or any
  body opening `Agentflow [`) whose title, body or commit messages put a closing keyword before an
  issue reference; a PR from a fork keeps `Closes #N`. lead-dev's merge preflight and orchestrator
  step 7d treat a red provenance gate as not merge-ready.

## 0.3.0 — 2026-09-30 (not published as a release)

Every pull request merged since 0.2.0, grouped by theme; the (#N) numbers are the pull requests each
line covers, in the maintainers' private working repository (not `agentflow-dev`), which is never
published: a public repository is a single-commit cut of it. The entries written as each change
merged follow, unchanged, under *Entries as recorded*.

### Install surface

- `install/provision.py` checks the Claude settings file before any write and refuses one that is
  unparseable or has a wrongly-shaped hooks key; it runs under the suite venv, pre-creates the lock
  directory, names the home directory as the launch directory, deploys Windows skills as junctions
  and keeps its closing report readable on a cp1252 console. The missed-wake watchdog is opt-in; the
  auto-allow hooks are installed by default and can be declined with `--no-auto-allow`. A
  non-default config home persists for the hook, and a first run no longer inherits the maintainer's
  own setup. Fresh-clone installs were re-verified on Windows and Linux (#54, #181, #187, #321,
  #351, #532, #542, #565, #566, #613, #618, #629, #637, #711, #750, #752, #756, #763).
- Every loop takes its project on the slash command (`/lead-dev --project <name>`), resolved as an
  argument rather than from the working directory; the `--settings` launch form is gone. The config
  loader rejects keys nothing reads and an `audit.window` below 1, the resolved config block prints
  checkout, base branch and mirror, configured ntfy env-var names are honoured, an explicit
  `--autodoc` or `--no-autodoc` is honoured or refused, never silently overruled by config, and the
  data root moved to the pillar layout (#4, #22, #53, #85, #89, #104, #108, #214, #226, #353, #403, #631, #781,
  #796, #800).
- New `python -m agentflow.doctor`: a diagnostic that runs when the install is broken, with a WARN
  tier; it passes a fresh install and tells a pending `queue_class` migration from a bad value (#91,
  #168, #442, #522).
- README, INSTALL, ARCHITECTURE and the harness docs describe the product that ships and re-measure
  the evidence they cite; `CONTRIBUTING.md`, `SECURITY.md`, issue forms and a bare MIT `LICENSE`
  were added. The security docs name both merge routes (approved epics and `triaged` issues), state
  that production stays a human step, and describe the operator posture (#16, #178, #190, #235,
  #284, #474, #552, #560, #564, #584, #590, #634, #642, #665, #802, #803).

### Share-cut (publish) tooling

- `publish check` and `publish cut` build a single-commit share cut in a local `--out` directory,
  ready to push to a separate `publish_repo`, with a runbook. A cut needs an explicit project, its
  shape is verified before push, `.env` directories and case variants stay out, a local
  sensitive-terms list guards it with fence-aware waivers, and a recorded human waiver satisfies the
  ledger check (#208, #555, #667, #735, #764, #784, #797).
- PR gates: the sanitize gate sees a plain home path, re-scans a PR description when it is edited,
  scans the whole index for control bytes and fails closed when grep errors; the provenance gate
  requires a cited source and a `Review:` pointer in every PR body, and its failure text suggests
  placeholders rather than real ids (#7, #27, #172, #173, #330, #352, #394, #449, #508, #563).
- Shipped instruction prose states rules without their event history, and a PR-time gate refuses
  issue numbers, ids and dates in it. The owner's name is gone from code, tests and docs,
  `PR-PROSE.md` sets the voice for PR and issue text, and a tracked `CLAUDE.md` tells sessions not
  to put session URLs in the repo (#39, #212, #541, #549, #640, #643, #644, #646, #647).

### CI spend

- The private repo's CI skips master pushes, drops non-required PR legs, runs the full matrix in a
  skip-if-unchanged nightly, caches pip and cancels superseded runs; a public repo keeps the full
  matrix (#615).
- A PR body is final when the PR is created, so no later edit re-runs every job, and a re-run
  retries only the failed jobs (#616).

### Registry and closure rules

- Registry writes are guarded: unverifiable writes are refused, and writes that allocate an id or
  strike a Note print what they did; `repair` re-validates and refuses a changed heading skeleton or
  a blank `--old-line`; `answer --by` is bound to its manager and `--project` is checked against
  config; `relane`, `withdraw` and a one-command resume are new; Directives are named and expired by
  their words and an overlapping one warns; `validate` runs at every loop wake and fails closed; a
  contended write on Windows is retried and reported apart from a denial (#10, #58, #90, #97, #106,
  #118, #123, #124, #203, #216, #228, #237, #241, #249, #258, #399, #400, #425, #490, #523, #559,
  #562, #568, #581, #591, #668, #688, #762, #792).
- Liveness: a heartbeat is separate from a claim (`status --heartbeat-only`); the next wake is a
  declared field with a missed-wake alarm and an optional outside watcher (`agentflow.wake_watch`);
  a lapsed eta is its own class; a stopped lane reads differently from a dead one; wake intervals
  are capped at what the harness grants; `--break-stale` requires the liveness probe (#25, #34, #62,
  #72, #102, #107, #179, #290, #305, #362, #375, #464, #469, #470, #475, #507, #510, #528, #531,
  #587, #685, #709, #771).
- No lane halts on an empty queue; it schedules the next check. Reap and cull belong to the steward,
  never resurrect shipped work, leave receipts (including to the seat that can re-publish) and keep
  the rows of lanes deliberately stood down. The steward runs the unattended pump and owns
  lane-hygiene Blockers, and lanes re-read routed Notes at phase boundaries (#14, #15, #33, #77,
  #84, #115, #165, #174, #199, #262, #265, #266, #267, #270, #426, #489, #539, #588, #589, #758).
- Merges chain behind the deploy lock: whoever was blocked first goes first, every builder lane is a
  listed holder, breaking a stale hold is an explicit decision, and the two shapes of a wedged
  deploy are told apart (#20, #76, #130, #256, #383, #493, #573, #586).
- Epic survey and closure: a stale plan file saved beside a spec cannot reopen a finished spec; a
  `## Files` section is a promotion precondition and is measured against what the merge touched;
  unresolvable or cross-project `depends_on` slugs are named or refused; `queue_class` is an
  allow-list, with a migration for the retired value; every builder lane writes a status board at
  merge; an undecodable, BOM'd or vanished file costs one record, not the survey (#23, #63, #65,
  #66, #79, #87, #99, #113, #120, #164, #231, #238, #253, #259, #260, #303, #308, #320, #323, #335,
  #346, #357, #360, #369, #370, #376, #382, #385, #389, #396, #406, #407, #453, #570, #746, #765,
  #780).
- Human decisions: dev-manager triages open issues into bugs, drafted epics or closures;
  decision-bearing issues are not offered as build work; the approval gate is enforced in code; an
  escalation is grounded before it reaches the human; the steward is the primary delegated answerer
  and records a decline; brainstorm pins reach the survey; a gitea work source is refused on every
  intake path (#8, #41, #194, #221, #255, #295, #526, #607, #617, #734, #749).

### Wiki pillar

- A `wiki/` pillar with two readers. `/save` ships as the wiki-save capability with a summariser; it
  refuses a target that is not a provisioned pillar or differs only by case, records and routes a
  failed save, and writes a hot file only when it changed (#193, #210, #250, #268, #271, #274, #283,
  #304, #342, #348, #381, #520, #530, #554, #669, #772).
- The pillar's residue has an owner, a real size and a ratchet the sweep runs; the baseline is
  seeded on an empty pillar (#468, #477, #800).

### Other changes

- Autodoc and graph query: one resolver for the autodoc root, per project; the corpus is bound to
  its project and stamped on every page; the update path reads records, is checked against a
  known-good rendering and has a runbook that parses; deleted and unclaimed files keep their pages
  routed; stored diagrams carry forward and are judged stale by git history; graph-query refuses a
  missing or foreign store (#3, #86, #96, #117, #125, #126, #131, #135, #140, #151, #154, #160,
  #229, #243, #384, #392, #398, #401, #421, #424, #428, #429, #506, #525, #529, #534, #535, #537,
  #538, #550, #610, #630, #635, #636, #710, #757, #777, #799).
- Orchestrator and waves: baked wave scripts live under the project data root, git calls take
  explicit paths, unattended paths never raise a question prompt, the human-surface gate is scoped
  per site, blocked tasks are never ready, wave timeouts fire and escalate, task bodies read `##`
  and `###` headings and keep fenced code, and one epic-worktree command is used everywhere (#5,
  #11, #30, #69, #81, #609, #633, #732, #767, #789).
- `/report` and the fleet board: one picture with WORK and DONE sections, derived DRY and BLOCKED
  lane conditions, the epics a free lane could actually start, open PRs, steward answers, ended
  boards kept out of the claimable set, and the stream that actually ended a run (#73, #92, #93,
  #94, #95, #251, #275, #289, #349, #457, #466, #491, #686, #687, #754, #776).
- Spec backups: the format lives in code with a reaper, a backup group moves with its spec, a
  rename-orphaned refusal is named, and `renamed_from` is a report-only marker (#185, #309, #343,
  #513, #556, #561).
- Branch and worktree hygiene: stale tracking refs are swept only when the remote is clear, orphan
  pruning asks the remote and spares a sibling epic's worktrees, unregistered and abandoned
  worktrees are reported and never removed, and `worktree_remove` writes nothing and refuses locked
  or unquotable trees (#100, #234, #366, #391, #439, #462, #484, #496, #511, #519, #567, #639).
- Auditor: evidence pointers are resolved, the ship gate reads the shipped registry, the audit
  contract has a blocker return and the product interpreter, and a blocked epic is retried outside
  the audit window (#112, #201, #225, #427, #480, #736).
- Loop skills: an interim fourth builder lane (`jr-dev-3`), a gated rule that long-running seats
  hand work to subagents, a gated naming rule, a first-spec path for dev-manager, and fixes to
  selection prose, forwarded arguments and the suite-run `cd` that wedged a worktree (#46, #47,
  #155, #195, #269, #278, #390, #422, #483, #512, #583, #664, #753, #775).
- Test harness and gates: gate corpora come from the git index, assertions state what they examined
  and refuse PASS over empty inputs, the harness prints which tree it tested and exits with distinct
  codes, and several false passes were fixed (#35, #60, #61, #71, #141, #142, #143, #145, #158,
  #170, #177, #182, #188, #215, #222, #242, #301, #307, #327, #339, #356, #367, #371, #404, #408,
  #409, #414, #443, #460, #499, #500, #516, #517, #518, #571, #608, #632, #768).

### Entries as recorded

- **A first run no longer meets the maintainer's own setup.** `person.mobile_default` and a
  project's `readiness_gate` block had no reader and are removed. The resolver rejects unknown
  keys, so a config that still sets either now fails to load, naming the key: delete the line.
  `question_style` now defaults to `mobile-two-turn` and is printed in the resolved block; set
  `question_style: inline` to opt out. The block prints `vault_save_skill` when one is configured,
  and the done-gate's knowledge-base note runs only then. lead-dev no longer has a fixed first
  selection step for a staging login. orchestrator Step 9.1.5 no longer offers a polishing pass,
  because the skill it invoked does not ship; in a standalone run it prints a one-line note when it
  detects a testable frontend.

- **The missed-wake watchdog is opt-in, off by default (gh#544).** `install/provision.py` step 12
  now registers the scheduled `agentflow.wake_watch` task only with `--wake-watch`; a default run
  prints `SKIPPED` naming the flag, on every platform, and the macOS/Linux crontab hand-off is
  printed only to an operator who opted in. A default run deletes nothing: a task an earlier
  install registered stays until the operator removes it (`docs/INSTALL.md` section 13, item 6).
  The loop skills now say the watchdog leg of a stand-down exists only where it was opted in.

- **Community and license surface for a first-time reader.** `LICENSE` is now the bare MIT text,
  so GitHub's license matcher has nothing extra to trip on; the vendored-code paragraph it carried
  moved to `ACKNOWLEDGEMENTS.md`, which now also names every runtime dependency (PyYAML, sqlite-vec,
  networkx) with its license. New `CONTRIBUTING.md` (suite commands copied from CI, and what trips
  each gate an outside pull request meets) and `SECURITY.md` (private reporting, what install
  changes, how secrets are handled). GitHub issue forms for bugs and features, neither of which
  applies a fleet routing label, and a pull request template that no longer assumes the reader is a
  loop and carries no example issue number the provenance gate could mistake for a citation. Every
  first-party version declaration now matches `VERSION`, and `test_release_surface.py` keeps all of
  this true.

- **`LAPSED ETA`: a lane that keeps breathing over an `eta` it never advances is now reported
  (gh#281).** `eta` is the field the whole missed-wake design rests on, and nothing was watching
  whether it was still being maintained. A lane could satisfy `eta_required` once and never touch
  the cell again: its heartbeat never ages out, so `read --stale` was silent on it forever at any
  `--threshold-hours`, and its heartbeat is always newer than its eta, so `read --missed-wakes`
  skipped it by the rule that says a heartbeat after the eta proves the wake fired — true about
  the wake that already happened, silent about the next one. The issue reproduced it over 30
  simulated days at a 6-minute staleness threshold: `stale=[] missed=[]` at every sample. One row
  on the live board was in this shape on 2026-09-05, reported by nothing.

  **Reported through the EXISTING missed-wake surface, as a DISTINCT class.** `read
  --missed-wakes` and `agentflow.wake_watch` both print it; there is deliberately no new `read`
  verb, because a second verb is another thing nobody runs — which is the hole this closes, one
  layer out. It is not folded into the missed-wake count either, because the two remedies differ:
  `MISSED WAKE` means probe a lane that may be dead, `LAPSED ETA` means tell a lane that is
  demonstrably alive to send one full `status --eta`. The predicates are disjoint by construction
  (`hb <= eta` versus `hb > eta`), so no row is reported twice, and each class prints its own
  qualified all-clear — a check that says nothing when it does not fire is indistinguishable from
  one that was never wired in.

  **Its grace is 120 minutes and is not tunable by `--grace-minutes`.** The false-positive class
  here is the fleet's normal operating shape: `--heartbeat-only` is what the 30-minute in-flight
  ceiling tells every lane to use and it preserves `eta` verbatim by design, so at the wake
  alarm's 30-minute grace this would fire on the whole fleet during any multi-hour epic. An alarm
  that fires on correct behaviour is one the reader mutes, and muted is how this stayed open.

  The steward's manual "read the eta column yourself every sweep" stopgap comes out with it.

- **Every self-waking loop now schedules a second wake leg: a standing five-hour rate-limit
  recovery guard (`CronCreate`).** An account-wide limit refuses a firing *before its first tool
  call*, so that firing never reaches its own `ScheduleWakeup`. The lane then exits with no
  pending wake, no row it could write, and nothing late enough for the missed-wake alarm to
  name — it cannot recover from inside itself, by construction, and three blackouts in 2026-09
  (09-08, 09-16, 09-19) each ended when a human noticed, roughly seven days of fleet time. The
  guard is the leg that was never asked to run during the refusal: scheduled in advance, fired
  later on a clock the limit has by then passed.

  **A clock only — never a crash, an error, a `halted` row, or an unknown-cause stop**
  (2026-09-22). It is a standing guard rather than something a failure path creates, precisely
  because a standing clock cannot be asked what went wrong and so cannot answer it wrongly.

  **It does not spend the single-daemon budget.** That budget is one, and the installer's
  watchdog step spent it. A `CronCreate` job starts no process, writes nothing to disk and
  survives no exit — it is in-session self-scheduling, the same category as `ScheduleWakeup` on
  a longer clock. What it therefore cannot do is stated in every loop that schedules one: it dies
  with the session, auto-expires after seven days, reports nothing during an outage, and fires at
  5h ±15m rather than on the hour.

- **The guard survives lead-dev's stale-cron sweep, and the rule that makes it survive is written
  where the sweeper reads it.** lead-dev has swept session crons since 2026-06-28, when a stale
  one re-fired a done epic seven times — and the guard is a session cron that loop created, so the
  sweep would have eaten it. §1.3 now carries the distinction as a table: a stale cron's prompt
  names a work target, the guard's carries the literal token `--recovery-guard` and no target at
  all, so the two predicates are disjoint. **The identity is checkable, not a convention to
  remember**: grep a `CronList` row's prompt, never its schedule or its position. Every loop
  strips the token at its argument split alongside `--project`, so it can never be read as a work
  target the way `--project` once was.

- **Idle wakes now ask `python -m agentflow.backoff` what to do instead of hard-coding an hour.**
  The helper compares a fleet-wide marker — remote-tracking refs and the dispatchable queue, both
  local reads with no network call — against a shared append-only observation log, and lengthens
  the wake while nothing moves *fleet-wide*. Fleet-wide is the load-bearing word: between two
  blackouts the six lanes logged 523 firings and roughly 8,025 assistant turns across 3.7 days
  while the steward measured zero claimable rows 62 wakes running and master sat still for 70
  hours, so one lane's own idleness is not evidence and one lane seeing movement resets the streak
  for all six. The two non-builders keep their own cadence language: the auditor takes the smaller
  of the helper's answer and its configured idle window (that window is a ceiling on how long an
  empty pass may wait), the steward the larger of the helper's answer and its own ladder.

  **The answer is a verb, not a number, and that is what makes it do anything.** `ScheduleWakeup`
  is clamped into [60, 3600] and the base cadence IS 3600, so a helper that could only ever name a
  delay could only ever name the hour it replaced — a backoff that costs a marker observation per
  lane per wake and reduces nothing. The last line is therefore
  `action=<schedule|stand_down|uncertain> delaySeconds=<seconds|none>`, and the floor is
  `stand_down`: the lane schedules **nothing**, and the standing five-hour `--recovery-guard` cron
  that the same release ships is its wake. That is why the floor and the guard window are the same
  five hours. Every loop carries the interlock beside it — **never stand down on a wake where
  `CronList` did not show the guard present** — and names the whole backstop chain: own
  `ScheduleWakeup` → the five-hour guard → `agentflow.wake_watch` on the OS scheduler, which
  cannot wake a lane but does report one that went quiet.

  **"Provably frozen" is keyed on the clock as well as the count.** The streak counts observations
  over a log six lanes share, so it reaches the ladder's top inside a single wake cycle — a
  45-minute lull, or one lane mid-build that has pushed nothing, would otherwise be
  indistinguishable from the measured 70-hour freeze. The floor now also requires the oldest STILL
  observation in the run to be at least five hours old: never sleep longer than you have watched,
  so the evidence window is the sleep it buys. A run whose age cannot be established never earns
  the floor.

  **The merged-PR signal was dropped rather than kept for symmetry.** It cost a `gh pr list`
  round-trip per lane per wake and could say nothing the refs signal does not once that ref is
  fetched — a merge moves master, and the refs signal reads master's remote-tracking ref locally,
  as of the checkout's last fetch (a merge nobody has fetched yet reads as no movement until the
  next fetch). Removing it also removed a whole class
  of inert fleet: with PRs as one of three signals, a fleet with no tracker configured had an
  unreadable signal on every observation and could never reach STILL at all.

- **`CronCreate` is declared as a harness dependency at a new RECOMMENDED tier** — in
  `preflight.py`, `install/manifest.md`, `README.md`, `docs/INSTALL.md` and
  `docs/ARCHITECTURE.md`, beside `ScheduleWakeup` and `Workflow`. Deliberately not Required: a
  session without it runs, wakes and builds exactly as before and loses only unattended recovery,
  so `preflight` prints `DEGRADED` rather than `MISSING` and still exits 0. Required would refuse
  a session that works — a refusal operators learn to bypass — and undeclared would leave a reader
  following a `CronCreate` instruction that fails with nothing anywhere saying it was conditional.
  `--no-recovery` silences the line for a seat that will schedule no guard, and never changes the
  exit code.


- **The missed-wake alarm can now fire when no loop is awake to run it: `python -m
  agentflow.wake_watch --project <name>`.** `missed_wakes()` worked; every caller of it lived
  inside a loop's own sweep, and the steward — the loop that runs the sweep — is one of the lanes
  it watches. So the detector's coverage was exactly the set of moments when a loop was still
  waking, which is the complement of the set of moments the alarm is for. On 2026-09-04 nothing
  had died: the process census was unchanged, two lanes heartbeated mid-sweep, and four of seven
  rows were 74–144 minutes past their etas with the steward's own row among them. **The timers
  simply stopped, and a human typing a command is the only reason it was ever reported.**

  The new command needs no session, no loop and nothing running. It reads the registry through the
  normal resolver, calls the SAME `missed_wakes()` and `blind_rows()` — reused, never restated, so
  the three exclusions cannot drift into two answers — and pushes what it finds to ntfy. Point a
  scheduler at it, or type it. **It never relaunches a lane**: notify costs one push, relaunch
  costs the single-writer rule, because relaunching on a false positive starts a second copy of a
  live lane.

- **The installer now registers that watchdog, instead of documenting it and hoping.** `install/
  provision.py` gains step 12: one OS-scheduler entry per configured project running `python -m
  agentflow.wake_watch --project <name>` hourly. Before this, the mechanism existed and nothing
  invoked it — measured on the fleet's own host, 227 registered scheduled tasks and not one of
  them this. A documented step is true only for the users who read it.

  **Windows registers it; macOS and Linux are told exactly what to add, and told loudly.**
  `schtasks /Create /F` is one non-interactive call that replaces a same-named task, which is what
  makes a re-run converge on a single entry rather than stack duplicate watchers — and repairs one
  whose paths went stale when a checkout moved. A crontab has no such call: it is the operator's
  file and may hold their own jobs, so a provisioner that rewrote it could silently drop them.
  Rather than skip quietly, the step prints the exact `crontab -e` line, project by project, into
  the installer's own transcript — the one place a Unix operator is guaranteed to look. Removal is
  [`docs/INSTALL.md`](docs/INSTALL.md) §13, item 6, on both platforms.

  Two details that are the point rather than polish. The task runs through a small wrapper script
  so **both output streams are captured**: `wake_watch` exits 0 on findings and on a clean board
  alike, so a job wired to the exit code alone reports permanent health over a fleet nobody is
  watching. And the registration site carries the single-daemon budget statement in full, with a
  test pinning it there — the next author who wants a second daemon arrives at exactly that spot
  looking for a precedent, and what they find instead is that the budget is one and already spent.

- **The daemon-free invariant is revoked; the rule is now SINGLE DAEMON ONLY (2026-09-22).** The
  suite ran no daemons by design. Three account-limit blackouts — 2026-09-08, 2026-09-16,
  2026-09-19 — stopped every lane for days at a time, were reported by nothing, and each ended
  only when a human relaunched by hand. No **in-fleet** detector can close that class: an in-fleet
  detector is itself an agent firing, an account-wide limit refuses every model turn on the
  account including that detector's, and the only invoker that survives takes no model turn — which
  makes it a daemon. So the suite now permits **exactly one**: an OS-scheduled, report-only
  invocation of `python -m agentflow.wake_watch`, which pushes to ntfy and never relaunches, never
  merges and never writes. **That budget is one and it is now spent** — a second daemon is a new
  decision. The no-relaunch rule on the watcher itself is unchanged and still test-enforced; what
  changed is who may invoke it. Setup in [`docs/INSTALL.md`](docs/INSTALL.md), rule and rationale
  in [`docs/REGISTRY.md`](docs/REGISTRY.md) → liveness sweep.

  **Its exit code says whether the WATCHER could do its job, never what it found** — a scheduler
  has one failure channel, and "the watcher is broken" arriving as the same signal as "the fleet is
  stalled" is the confusion this closes. That has a consequence worth stating rather than papering
  over: delivery counts as part of the job, and a push is only attempted when there is something to
  push, so a broken ntfy shows up as exit 1 **only on a board with findings**. The report is
  therefore written to **stdout before any push is attempted** — an early cut lost the findings
  entirely when the send raised, which is precisely the moment stdout is the only channel left —
  and both `--help` and `docs/REGISTRY.md` now tell the operator to capture stdout and stderr, not
  only the exit code.

  Two things the shared rule was only claiming to share are now structural: the per-row wording is
  one builder (`missed_wake_line` / `blind_row_line`) reached by both reporters instead of the same
  f-string typed twice, and the all-clear carries the **judged-row count** on both paths — stand
  every lane down and the old watcher reported full health having examined nothing. Also fixed on
  the way through: `read --missed-wakes` resolved `--grace-minutes` with `or`, so `0` was silently
  rewritten to 30 and the two callers disagreed about the same row; and the push's `epic=` field is
  now ASCII-folded like `detail`, so a non-ASCII directory name (the project is derived from the
  path when `--project` is absent) can no longer silence the alarm on a stalled fleet.

  Drilled end-to-end by `harness/missed-wake-drill/missed_wake_drill.py`, which stalls a lane, then
  the steward's own row, then a lane stood down through the real verb, and runs the watcher in a
  separate interpreter each time.

- **lead-dev can finally discharge the duty it was given: `relane` and `withdraw` (gh#32).**
  `docs/REGISTRY.md` and every builder skill make lane assignment lead-dev's job and tell it to
  prune the Queue every firing — and the CLI had no verb for it. `publish` refused a duplicate id,
  `release` preserved `lane` verbatim *and* required the actor be the claimant, `repair` and `cull`
  were dev-manager-only. So a row published to the wrong lane was stuck there: claimable work
  parked behind a busy lane while an idle builder starved, with every loop behaving correctly. A
  missing primitive, not a policy question.

  It bit again on 2026-09-01 in a sharper form. `E-0200`'s 33-file migration unparked when its
  question was answered, and its `## Files` list overlapped `E-0059` — published to the other lane
  while `E-0200` was still parked — on `install/provision.py`, `templates/environment.md` and
  `skills/dev-manager/SKILL.md`. With no verb to drop the row, the only available move was a routed
  Note asking a lane not to claim what the board still advertised as `ready`. That lane consumed it
  correctly and then recorded *"lead-dev withdrew it"* — which had not happened and could not.
  **Coordination degrading into prose that gets re-interpreted is the cost of a missing verb.**

  Both verbs are lead-dev-only and share one guard: the row must be **unclaimed** and still
  **`ready`** — an unstarted publication. A claimed row is refused with the alternative named
  (route its claimant a Note and let it release cleanly), because that builder may be mid-build
  with commits behind the row; neither verb takes a `--force`. `--reason` is required and lands on
  the row, preserving the publish-time eligibility rationale already in `notes`.

  `withdraw` sets a new `withdrawn` state rather than the existing terminal `done`, because `cull`
  moves a consumed row **verbatim into the append-only `registry-log.md`** — a retraction filed as
  `done` would stand there forever as a record that the work shipped, and until the sweep every
  lane reading the board would see retracted work rendered as completed.

  **Review caught this fix reintroducing gh#32's own failure mode.** The first cut made `withdrawn`
  a one-way door: no lead-dev verb could bring the row back, while the documented causes of a
  withdrawal are mostly transient ("deps un-satisfied" reverses when the dep lands, "conflicts with
  another lane's claims" when that claim completes) — and the epic stays `approved-for-autonomous`
  on disk, so the every-firing sweep would re-offer it and hit `id_duplicate` on every wake until
  an unrelated loop happened to cull. A `publish` now **reclaims** a withdrawn, unclaimed id,
  preserving the withdrawal note; every other state is still a duplicate, and the refusal now names
  which state. The state refusal also stopped asserting "a row that has moved past ready has
  history behind it" at withdrawn rows — true of the other four states, false of the one this
  change introduced.

  **And review falsified the justification this change was built on.** Four places claimed the
  auditor reconciles the `done` Queue set against its ledger by exact set equality. It does not:
  the audit ledger keys on epic slugs from the epic survey and never reads a Queue row. The
  decision was right and the reason was wrong, which is its own defect — corrected everywhere,
  with the false claim named rather than quietly replaced. Also fixed: `_publisher_row` read a
  blank `claimed_by` cell as claimed while `claim`, `--claimable` and `validate` all read it as
  unclaimed; and `docs/REGISTRY.md`'s state schema, the `queue_state_enum` rule text, the `cull`
  docstring, its `docs/REGISTRY.md` appositive and `dev-manager/SKILL.md`'s cull list had all
  drifted off the six-state enum. Two new tests pin the rule text and the binding doc against
  `QUEUE_STATES` so the next state cannot drift silently.

- **The missed-wake alarm can tell a deliberately-stopped lane from a dead one (gh#98).** When a
  human clears a lane by hand it leaves exactly the signal of that lane dying: a live `eta` no
  session will ever answer, so the alarm pages for an intentional act. It happened on 2026-09-01,
  when two lanes were cleared to open a quiet window for a migration. Every false page on a
  deliberate act teaches the reader to discount the alarm, and this alarm's entire value is in
  being believed — it exists because a 32-hour fleet-wide stall went unnoticed.
  **`stand-down --actor dev-manager --agent <lane> --reason "<why>"`** writes `stood down by
  dev-manager: <why>` into that row's `phase`, and `missed_wakes()` and `blind_rows()` exclude it
  the way they already exclude a halt and a reaped row: **on the row's own text**. An exclusion
  held in a config file, or in the operator's memory of what they stopped, is invisible at exactly
  the moment someone asks why the alarm stayed quiet.

  Deliberately **not** `reap`, which means *died, and its claims were returned*: overloading it
  would erase the distinction the report exists to draw and would return claims from a lane that
  may be resumed with them intact. Claims, the deploy lock, `heartbeat` and `eta` are left exactly
  as the lane left them. The mark **clears on that lane's next full `status` write** — the lane
  itself saying it is back — so nothing has to remember to undo it. `--heartbeat-only` is refused
  against a stood-down row, which is what makes that true rather than merely intended: it preserves
  the phase, so it would otherwise keep the mark alive indefinitely while stamping fresh liveness
  onto a row asserting the lane is stopped. `--reason` is required, non-blank, and pipe-guarded like
  every other free-text field that lands in a table cell. If the stopped lane holds the deploy lock
  the CLI warns and does not release it: releasing could step on a half-applied merge, and that call
  is a human's.

  One decision reversed mid-build and recorded because the fix would otherwise have been perfectly
  vacuous: the exclusion was first written as `phase.strip() == STOOD_DOWN_PHASE`, which no row the
  verb produces can ever satisfy — the reason has to live in the cell or the row does not say why it
  is excluded. It is an anchored **prefix** now, never a substring search, for the reason gh#88
  landed on `directive_text_is_expired` in the same module: an unanchored test reads the marker
  out of a lane's own phase prose. The
  honest-coverage count in the all-clear line skips it too, since counting a row nobody judged as
  "checked" is a milder version of the false green that report exists to remove.

- **`expire-directive` is addressed by content, and says what it expired (gh#88).** `--index` is
  1-based over the *live* set, so a 0-based reading expired the neighbour — and because expiring
  a row re-bases the list, the same index used twice targeted two different Directives. Exit was
  0 and the output named nothing, so the only way to notice was to diff the block afterwards.
  It happened for real on 2026-09-01. A Directive is a standing human instruction every builder
  re-reads before an irreversible action, so expiring the wrong one silently removes a live
  safety rail. **`--match <substring>` must now hit exactly one live Directive** — the same
  semantics `--ack-directive` already uses, which means the caller has to have
  read the thing it is expiring. `--index` still works, is documented as re-basing, and **both**
  paths print `expired: <the directive>`.

- **Registry and status writes survive a concurrent reader on Windows (gh#57).** `os.replace`
  onto an existing path fails with `PermissionError` (WinError 5) when another process holds the
  destination open — and a reader's open handle is the **normal** state here, since the firing
  contract has every lane read the registry lock-free at every wake and the edit-lock serialises
  writers only. Hit live on 2026-08-31 with six lanes active; the same command succeeded on
  immediate retry, so it is contention, not corruption. The replace now retries with bounded
  backoff (2s ceiling, sub-second per attempt) and **still raises on exhaustion** — a lane that
  loses a note or a claim must find out. At the CLI boundary that exhaustion is now a legible
  message saying the write did NOT land, rather than a raw traceback.
  **There were two copies of this routine and only one had the bug fixed:** `status_writer`
  carried the identical bare replace on a path with *more* concurrent readers (`epic_survey`
  walks every board) and no edit-lock at all, plus a narrower cleanup that stranded temps on
  interrupt. Both now call one shared `agentflow.atomic_io`, so the next fix cannot land in one
  copy and miss the other.

- **`lead-dev` stops halting on an empty queue too, so no lane halts on no-work anywhere.** The
  entry below retired terminal halting for the two junior lanes and the auditor but excluded the
  foreman, whose halt fired once polish mode came back dry twice. That exclusion was an
  enumeration gap rather than a decision: the site list was built by grepping `empty_wakes`, and
  lead-dev's halt never used that token. **The condition it fired on is genuinely stronger** —
  polish mode *generates* work, so two dry runs really do mean the product has no work anywhere
  — but the halt was never a stopping rule. It was the only channel the seat had to *tell*
  anyone, and it stopped because stopping was the signal. That signal is now reported instead:
  an ntfy line plus an idle-wake, which reaches the human **without stopping the seat that feeds
  every other lane** — a halted foreman idles four loops, not one. `docs/REGISTRY.md` drops its
  lead-dev exception, so `halted` means *blocked on an answer this lane cannot get* for every
  lane with no exceptions; the juniors' terminal halt while staging still lacked the test-user
  login the done-gate needs — another no-work condition only lead-dev can clear — becomes an
  idle-wake for the same reason.

- **No lane halts on empty input any more — it schedules the next check and keeps checking.**
  `jr-dev-1`, `jr-dev-2` and the `auditor` used to halt terminally after 3 consecutive empty
  wakes, with the count kept as `empty_wakes` in each lane's state-file frontmatter. Both halves
  were wrong together: a terminal halt needs a human to notice, and **the counter survived the
  relaunch**, so a lane that halted at 3/3 came back at 3/3 and halted again on its first empty
  wake — the human's recovery action did not work. On 2026-09-01 that happened three times in one
  night across three lanes while 13 triaged bugs and 4 approved epics sat unclaimed. `empty_wakes`
  is deleted rather than left unread (a counter nothing reads is how the next reader rebuilds the
  halt around it); `starvation_note_written` stays, because writing the note once is good and has
  nothing to do with halting. **`lead-dev` was excluded at the time and is now covered too** — see
  the entry above. `halted` is redefined in
  `docs/REGISTRY.md` to mean *blocked on an answer this lane cannot get* — never *has no work
  yet* — which is what makes the existing `--waiting-on` requirement coherent rather than
  awkward. Retires the interim standing Directive that carried this behaviour.

- **`fleet-context` threaded through `/project` and `solution`** — closing the last unattended
  question leak. A builder handing an approved spec to `/project` reached `solution`'s Dependencies
  gate, which fired `AskUserQuestion` unconditionally on **every** plan it authored, with no human
  watching; the Routing / Init check / Dispatch / Resume gates in `/project` and the unknown-slug and
  technical decision-gates in `solution` had the same hole (Dispatch only survived by the
  `approved-for-autonomous` auto-skip — a state coincidence, not the contract). Both skills now carry
  the orchestrator's contract: under an explicit `fleet-context: <agent>` marker every gate becomes a
  registry Question row with a mandatory already-computed default (Dependencies defaults to the list
  detected one step earlier — never an invented one) and the run proceeds or parks instead of
  blocking. `/project` propagates the marker into `solution` and the orchestrator bootstrap so the
  branch holds end-to-end; standalone interactive runs still show the buttons. A test guards the
  invariant — every `AskUserQuestion` site in a skill either sits under the marker or is an
  allowlisted human surface (`dev-manager`, `asking-questions`, `choose-next`). The guard checks
  `project`/`solution` per GATE (a deleted branch fails it) and other skills per section (the
  orchestrator states its contract once, globally). Two orchestrator gates it found unbranched are
  recorded with a ratchet and tracked in issue #12 — the invariant holds for what the test claims,
  not for the whole tree yet.
- **Four registry rules moved from prose to code** (from the first fleet-consistency audit, where
  each of them failed on the first live night). **Project scope** — `survey_epics(...,
  project=<name>)` narrows the globs to `projects/<name>/`, and `dispatchable(records,
  excluded_domains, project=<name>)` drops any record whose frontmatter `project:` is set and
  differs; both layers, because either alone is bypassable. Every builder call site now passes it
  (`lead-dev`, `jr-dev-1`, `jr-dev-2`, `auditor`, `steward`, and the auditor harness scenario);
  dev-manager, `choose-next` and `backlog` stay project-wide on purpose and now say so at the call
  site. **`queue_class` is an allow-list** — only unset / `autoloop-buildable` dispatch, so `bug`
  and `feature` no longer sail through the old three-value deny-list; an out-of-vocabulary value
  gets a survey warning line, never an exception. **Advisory questions need a real assumption** —
  `ask --urgency advisory` without `--assumption` is refused (exit 2), and `validate` reports —
  never repairs — a LIVE (`open`/`answered`) advisory row whose `assumption` is still `-`. **`directive` stamps dev-manager's
  heartbeat** in the same locked write (heartbeat cell only, phase untouched). `docs/REGISTRY.md`
  and `SCHEMA.md` record all four as enforced, with the `queue_class` vocabulary table.
- **dev-manager issue intake** — the human's seat now triages open tracker issues (§3.4)
  before any builder can touch them: bounded defect → label `triaged` (+`bug`); needs design →
  carve a `drafted` spec with `source: gh#N` and label `triaged`+`epic`; undecided architecture
  → `brainstorm-pin`; closing stays a human call. Unattended wakes run it read-only and put
  the untriaged count in the digest. lead-dev's bug-burn / open-bug selection narrows to
  `triaged` non-`epic` issues via the new shared `agentflow.issue_intake` helper (list, filter,
  label, `untriaged|selectable` CLI); steward-filed issues carry `triaged` only when the
  steward verified them bounded. Harness scenarios F (triage) and G (untriaged invisible).
- **Baked wave scripts live under the project data_root** — `bake_wave_script` now takes an
  explicit `out_dir` (`wave_script_dir(data_root, project.name)` →
  `<data_root>/projects/<name>/status/workflows/`) and refuses the template directory; the
  orchestrator removes the variant on the reconcile wake (`remove_wave_script`). The suite
  checkout stays clean after a build, two projects cannot collide on a tag, and the 0.2.0
  `.gitignore` bandage is gone so a regression shows up as a dirty tree again.

## 0.2.0 — 2026-08-29 (first live fleet run)

Hardening from the first unattended run against a real product: two epics shipped and gated,
one session-limit crash resumed from state, ~9 hours wall clock.

- **steward** — sixth loop. Sweeps what time breaks (stale frontmatter, orphaned locks,
  deploy/schema drift, silent failures, dead issues); fixes the evidence-checkable, backlogs the
  structural. Registry actor with its own row, notes, and blockers only.
- **dev-manager self-wake** — `/dev-manager --unattended` between the human's visits: answers
  within delegated authority, sweeps liveness, culls, digests human-only questions via ntfy.
  Never asks a question into an empty room. Delegated authority now has an explicit
  never-self-answer list (new modules/services, auth/roles/credentials, data model, spend).
- **Liveness is evidence** — a stale heartbeat is a symptom; the sweep probes branch commits,
  worktree mtimes, and child processes before reaping. Every loop refreshes its row at least
  every 30 minutes while subagent work runs.
- **`project.excluded_domains`** + per-epic `build_approved_by:` override in `dispatchable()`.
- **`canonical_repo.python`** — the product's own interpreter; builders never run a product
  suite with bare `python` (the suite venv). `install/SKILL.md` Step 7 — putting the suite venv
  on the session's PATH — explains the displacement.
- **Bug-burn wake** — every third epic `lead-dev` selects, it takes an open triaged bug
  instead: the `epics_since_bug_burn` counter is a count of epics, not of wakes, so a wake
  that selects no epic never advances it; on the wake where it reaches 3 the bug replaces
  the epic and the counter resets to 0 (`skills/lead-dev/SKILL.md` step 4.5). The bug is
  picked by blast radius, not by issue number. The counter starts at 0 on a fresh install
  and is never inherited from a predecessor loop.
- **Reviewer verdicts on the PR** — the dual review posts one summary comment before merge.
- Multi-project launch form documented (`--settings` override; shell env is ignored) —
  `install/SKILL.md`, *Add another project later*. Superseded in 0.3.0: the `--settings` form
  is gone, and the project goes on the slash command (`--project <name>`).
- Fresh public-ready working repo (private): single initial commit, history-bearing dev repo
  archived.

## 0.1.0 — 2026-08-28 (public-ready cut, not published)

Behavior-frozen port of `agentflow-dev` — the private, archived predecessor named in the
README's provenance box. The date above is the root commit of the private working repository
(`agentflow 0.1.0 — fresh public-ready cut`); a public repository begins instead at its own
single share-cut commit. Registry CLI with full mediation (08-08).
