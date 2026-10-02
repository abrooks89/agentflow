# Launch-directory drill

There is no script here. The record is [`launch-dir-drill.md`](launch-dir-drill.md), written by
hand and committed.

That is the point rather than a shortcut. The assertion this drill discharges is one the epic that
raised it singled out as *"the real proof, and it cannot be faked by a unit test"*: a builder lane,
launched from the machine's launch directory, **claims and builds a real queue row end to end**, and
confirms **from inside that same session** that its accumulated memory is present. A harness script
can build a sandbox that looks like a lane; it cannot be one. Every observation in the record was
therefore made from inside a live fleet session, and the record names them so a sceptic can re-run
each one on the same machine and compare.

## Why it exists

`one-launch-dir-per-machine` moved the fleet to a single launch directory per machine and said what
that buys: the session's `CLAUDE.md` chain, its auto-memory, and the settings that carry the fleet's
hooks all key on the folder a session is opened in, so a lane opened anywhere else comes up
unbriefed and reads as *stale* rather than as *broken*.

Twelve checks were written for that epic. Ten held against live artifacts. The one that mattered
most did not, and it failed in a way worth naming, because nothing about it looked like a failure:

- One session was launched from the launch directory and **built a real row end to end** — and
  recorded no memory confirmation at all.
- Two other sessions **confirmed they were briefed** — and were idle, with nothing claimed.

Both halves were evidenced. No artifact showed one session doing both, and the half that was missing
for a *building* lane is the load-bearing one: the `SessionStart` hook is user-level and fires from
any working directory, so what the launch directory actually controls is the `CLAUDE.md` chain and
the accumulated session history. Split evidence reads as coverage until someone asks which session
produced which half.

## What the record establishes

Observations 1, 2, 3, 4 and 6 are stated as a command and its observed output. Observations 5 and 7
are values and file-checks that have no single command — they name the artifact to check instead.

1. **The launch directory is not a repository.** `git -C <launch-dir> rev-parse
   --is-inside-work-tree` fails there. This is the premise every later claim rests on — a lane
   sitting in no repo is why `worktree_root` is required config and why every git action in the
   fleet's skills carries an explicit `-C`.
2. **Which instruction file the session read**, by path and byte count.
3. **Which settings file the session loaded**, by path and byte count — stated as the real path
   rather than as the phrase the instruction file uses for it, because the whole subject of this
   drill is guidance that has to be *checkable*.
4. **Auto-memory present, confirmed from inside the session** — path, byte count, index-entry count
   and mtime, each with the command that produced it. The count carries its predicate: a count
   without one is a claim nobody else can reproduce.
5. **The real queue row that same session claimed and built** — epic, pull request, squash commit,
   merge time, CI result, plus the lane state file that ties those to *this* session rather than to
   some session. This is what makes it a drill rather than a fixture.
6. **Independent corroboration that the property bit during the run.** Mid-session a command that
   changed directory into the launch directory and asked git for its remote failed with *not a git
   repository*. An observation that a rule *fired* is worth more than an observation that it was
   *followed*. The record cites the three places that actually assume this property — the CI
   worktree gate's comment, `SCHEMA.md` §1, and INSTALL.md's worktree-cleanup paragraph — and says
   explicitly that INSTALL.md §10 is **not** one of them, because §10 warns about the inverse case.
7. **The two host files the epic marked confirm-only, confirmed** — with what was checked and what
   was found, since "confirmed, not edited" is worthless if nobody can see what was confirmed.

## What it deliberately does not prove

- **One machine, one operating system, one run.** The record names the platform. Nothing here says
  the property holds on a machine set up differently — see the discrepancy the record raises at the
  end.
- **It is not a regression test.** A companion assertion in
  `skills/_shared/tests/test_one_launch_dir.py` holds this record in place — it must exist and
  still carry each observation, the epic id, the PR number and the squash sha — so the directory
  cannot be quietly deleted or the record gutted. That is the transferable half of the sibling
  drills' pattern, and it is all of it: the assertion pins the *text*, not the property. If the
  launch form changes tomorrow this record does not go red, it goes *stale*, which is exactly the
  failure the drill was written about. The guard against that is the reader enumeration in
  [`docs/INSTALL.md`](../../docs/INSTALL.md) §10, which tells a future sweep which kinds of reader
  to walk.
- **It says nothing about a lane launched the wrong way.** There is no control run. A negative case
  would mean deliberately launching a lane from inside a repo and watching it die at step 0 without
  leaving a row, which costs a real build to observe and was not done.
- **The memory check is a presence check.** The file was there, it was the right size, and its index
  parsed. Whether the session actually *used* what it read is not observable from inside it.

## Paths in the record

The record refers to the launch directory as `<launch-dir>` rather than by its absolute path, and to
the per-directory memory folder under `~/.claude/projects/` by its shape rather than its name. The
mangled folder name is a literal the sanitize gate catches by pattern
(`.github/workflows/ci.yml`, *"Grep for personal/product literals"*); a bare home path is not caught
by that pattern today, and is kept out by the same convention the rest of the docs follow rather
than by the gate. The record states how to resolve `<launch-dir>` — it is the directory whose
`.claude/settings.local.json` the installer wrote — so a reader on any machine can expand every
placeholder without being told the answer.

## In CI

**The drill is not re-run, and none should pretend to.** A CI job that re-derived these observations
would be running in a checkout, from a working directory of GitHub's choosing, with no fleet session
anywhere — it would be asserting the premise it is supposed to test.

What CI does hold is the record's *presence and content*, in
`skills/_shared/tests/test_one_launch_dir.py`. The two sibling drills here (`missed-wake-drill/`,
`fresh-clone-install-drill/`) go further: each has a companion that breaks a limb of the drill in
turn and requires it to fail. That half needs a script to mutate and there is none here, for the
reason at the top of this file.
