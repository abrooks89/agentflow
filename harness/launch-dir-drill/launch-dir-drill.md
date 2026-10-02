# Launch-directory drill — record

| | |
|---|---|
| **run** | 2026-09-04 |
| **lane** | `lead-dev`, agentflow fleet |
| **launched from** | the machine's launch directory (`<launch-dir>`) — not a repository checkout, not a per-epic worktree |
| **platform** | Windows 11 build 10.0.26200 (AMD64) |
| **assertion discharged** | *"Launch one builder lane from the launch directory, have it claim and build a queue row end to end, and confirm from inside that session that auto-memory is present."* |
| **verdict** | **PASS**, with the limits in [`README.md`](README.md) — read them before citing it |

Written by hand from inside the session that produced it. See `README.md` for why there is no
script.

## Notation

`<launch-dir>` is this machine's launch directory: the one folder every fleet session is opened
in. It is written as a placeholder because a machine-specific path in a tracked file is exactly
what the sanitize gate exists to keep out.

**Resolving it is not a lookup — there is no `launch_dir` field in the config, by design.** The
launch directory is implicit, which is precisely why `worktree_root` had to become required
config: a lane sits in no repo, so nothing about where work happens can be inferred from the
working directory (`skills/_shared/SCHEMA.md` §1, and `templates/environment.md`'s note on that
field). What identifies the launch directory instead is a property, and it is checkable:

> **It is the directory whose `.claude/settings.local.json` the installer wrote** — the file
> carrying the `SessionStart` hook, the auto-allow matchers and the resolved `"env"` block.
> `install/provision.py` resolves that path as `Path.home() / ".claude" / "settings.local.json"`
> and `skills/orchestrator/bootstrap.py` agrees, so on any machine installed the documented way
> it is the home directory. On the machine this run was made on, it is.

`<mangled-launch-dir>` is that same path with its separators and colon replaced by hyphens, which
is the folder name Claude Code derives for its per-directory session store under
`~/.claude/projects/`. The mangling is mechanical: expand `<launch-dir>` and the folder name
follows.

Every command below is written with the placeholder in place of the path. Substitute the
machine's own value and each one runs verbatim.

## Observation 1 — the launch directory is no repository

```
$ git -C <launch-dir> rev-parse --is-inside-work-tree
fatal: not a git repository (or any of the parent directories): .git
```

This is the premise, not a curiosity. A lane's working directory is deliberately outside every
repo, which is why no worktree path can be inferred from it (`worktree_root` is required config,
never defaulted — `skills/_shared/SCHEMA.md` §1) and why every git action in the fleet's skills
carries an explicit `-C <path>`.

## Observation 2 — the instruction chain the session read

```
$ wc -c <launch-dir>/CLAUDE.md
21319 (bytes; the path echoes back expanded)
```

This is the file Claude Code finds by walking upward from the working directory. It is the
widest-reach instruction file on the machine and this repository does not track it — which is why
`docs/INSTALL.md` §10 now enumerates untracked readers as their own kinds, and says what to do
when one of them disagrees with a tracked page.

## Observation 3 — the settings the session loaded

```
$ wc -c ~/.claude/settings.local.json
22837 (bytes; the path echoes back expanded)
```

**Stated as a path rather than as a description, deliberately.** The host instruction file
describes this file as *"project-local to the home root"*, which is accurate — `settings.local.json`
is the *project* settings file for a session rooted at the home directory, and nothing searches
upward for it — but a description is not a path, and the subject of this drill is guidance that
has to be checkable. The two agree here. The record carries the path so that the next reader can
tell.

This file is what carries the fleet's `SessionStart` hook, its auto-allow matchers and the
environment the loops resolve from. A session opened anywhere else loads none of it and dies at
step 0 without leaving a row — the failure `docs/INSTALL.md` describes as *a lane sitting stale on
the board while its window is plainly alive*.

## Observation 4 — auto-memory present, confirmed from inside the session

```
$ wc -c ~/.claude/projects/<mangled-launch-dir>/memory/MEMORY.md
22118 (bytes; the path echoes back expanded)
$ grep -c '^- \[' ~/.claude/projects/<mangled-launch-dir>/memory/MEMORY.md
117
$ date -u -r ~/.claude/projects/<mangled-launch-dir>/memory/MEMORY.md +%Y-%m-%dT%H:%MZ
2026-09-04T14:34Z
```

The count is stated with the command that produced it because a count is a claim about its
predicate: 117 is the number of *index entries*, i.e. lines beginning a markdown list item with a
link, and a different reasonable predicate gives a different number.

This is the half the source epic could never evidence for a lane that was building. The
`SessionStart` hook is user-level and fires from any working directory, so it is *not* what the
launch directory buys; the accumulated per-directory session store is. Confirming it from a
session that was idle proves the store exists. Confirming it from a session that was mid-build is
the assertion.

## Observation 5 — the real queue row that same session claimed and built

| | |
|---|---|
| epic | p202 `private-working-repo-and-public-publish-target` |
| pull request | #208 |
| squash commit | `e3b194a` |
| merged | 2026-09-04T14:35:58Z |
| CI | 14/14 green; master green after merge |

Everything in that table is checkable after the fact from the git host (`gh pr view 208`), which
is the point at which "that same session" stops being checkable with it — the load-bearing word in
this observation is *same*, and a merge record cannot carry it.

**The artifact that does:** `<data_root>/projects/agentflow/status/lead-dev-state.md`, the lane's
own state file, carries observations 2–4's byte counts and this p202 → #208 → `e3b194a` chain in a
single block written during the run, along with a correction the lane made to its own first
evidence pass. It is not tamper-proof and is not offered as such; it is the contemporaneous record,
written before the merge, that ties the two halves to one session.

Any approved seam-free row on the board was acceptable as the vehicle; this is the one the lane
selected on its own wake. It is what makes this a drill and not a fixture — the session was doing
its ordinary work, and the observations above were taken from that work rather than staged around
it.

## Observation 6 — the property bit during the run, and was not merely asserted

Mid-session, a command written in the older style — change directory into the launch directory,
then ask git about the repository there —

```
$ cd <launch-dir> && git remote get-url origin
fatal: not a git repository (or any of the parent directories): .git
```

It matters more than observation 1 does: observation 1 shows the property *holds*, this shows it
*fires* — a real command written against the old assumption failed loudly, in the session, rather
than quietly operating on the wrong tree.

**What this failure corresponds to in the repository** — stated carefully, because the obvious
citation is the wrong one. `docs/INSTALL.md` §10 warns about the *inverse* case, a lane started
inside a repo binding to the wrong instruction chain; it does not warn that a git command run in
the launch directory fails for want of a repo, and on the arrangement §10 describes it would not.
The places that do assume this property are:

- `.github/workflows/ci.yml`, the worktree tool/flag gate's comment — *"from the launch directory
  that is no repo at all, so a lane using either one silently operates on nothing"*. That gate
  exists to ban exactly the pattern this command used.
- `skills/_shared/SCHEMA.md` §1 — *"a lane launches from the machine's single launch directory,
  which sits in no repo"*, given as the reason `worktree_root` cannot be defaulted.
- `docs/INSTALL.md`'s leftover-worktree cleanup, which tells the reader to name both ends of a
  `git worktree` command *"rather than relying on where your shell happens to be, which from the
  launch directory is no repository at all"*.

The remedy is the one all three give: name the checkout explicitly with `git -C <path>`.

## Observation 7 — the two host files the epic marked confirm-only, confirmed

Recorded because "confirmed, not edited" is worthless if nobody can see what was confirmed. Both
are host files outside this repository and neither was written to.

| file | what was checked | result |
|---|---|---|
| `<launch-dir>/CLAUDE.md`, launch line | polarity of the launch guidance | prescribes `cd <launch-dir>`, plain `claude`, `--project` on the slash command; carries an explicit **NEVER** on the `--settings` form with the env-displacement mechanism spelled out |
| `~/.agentflow/config/projects/agentflow.md` | residual `--settings` guidance | none prescriptive; the string appears only inside the prohibition, plus a note that the retained `settings-*.json` files should not be used |

Both are hits a grep would count and a counter would misread. Neither is drift.

## What this run also surfaced

Not part of the assertion, recorded because it was observed and would otherwise be lost.

**The documentation and this machine disagreed about what a launch directory *is*, and the
documentation was wrong.** As found, `docs/INSTALL.md` steps 10 and 12 had the reader launch from
the agentflow checkout, and `install/SKILL.md`'s *Launch the fleet* still says "the machine's
launch directory — the checkout". On this machine the launch directory is the home root and
observation 1 shows it is not a repository at all.

That is not two valid arrangements. The installer writes its hooks and its `"env"` block to
`Path.home() / ".claude" / "settings.local.json"` (`install/provision.py`), and that file is the
project settings file for a session rooted at the home directory *and for no other* — so a fleet
launched from the checkout loads none of it: no resolved-config block, no auto-allow matchers, no
injected project `CLAUDE.md`, and a lane that dies at step 0 without leaving a row. `docs/INSTALL.md`
was corrected in this commit. `install/SKILL.md` and the loop skills that restate the rule were
outside this epic's write scope and still carry the checkout wording; that is the follow-up.
