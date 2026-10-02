# agentflow

[![CI](https://github.com/abrooks89/agentflow/actions/workflows/ci.yml/badge.svg)](https://github.com/abrooks89/agentflow/actions/workflows/ci.yml)

**An installable AI software-delivery system: a fleet of self-paced Claude Code agent loops that take work from idea → spec → plan → built, tested, reviewed, merged code, coordinating through one shared markdown file with exactly one channel to the human.**

---

## What it is, in 30 seconds

agentflow is **a set of Claude Code skills you install on your own machine.** There is no server to host: a "loop" is one Claude Code session running a program written in prose, and it keeps itself going by scheduling its own next wake. The only thing that runs outside a session is one optional watchdog on your OS scheduler — it reads the board and pushes an alert when a loop misses a wake, and it never relaunches anything.

- **What it does.** You approve a spec in one session. The other loops write the technical plan, build it in a git worktree, review it, open a pull request, merge it, and tell you what happened.
- **What it writes.** Markdown. Specs, plans, wiki pages, and one coordination file per project — a database whose pages are files you can diff, grep and hand-edit.
- **Who talks to you.** Exactly one loop (`dev-manager`). Every other loop notifies and never asks.
- **Who it's for.** One developer, on a machine they own, working on their own repositories. There are no accounts, no roles and nothing multi-user: the fleet has exactly one human seat, and six of its seven loops run with permission prompts off — the box is the containment, not a prompt ([**Security & blast radius**](#security--blast-radius)).
- **What it is not.** Not a service, not a hosted product, not a dashboard — [`docs/ROADMAP.md`](docs/ROADMAP.md) names those cuts on purpose.

**The directory it creates**, once you have picked a `data_root` and named a project:

```
<data_root>/projects/<name>/
├── design/     # what we INTEND — specs, plans, use cases, suggestions   (a human writes these)
├── status/     # what the fleet is DOING — registry.md + status boards   (the loops write these)
├── wiki/       # what we LEARNED — durable pages + a generated hot tier  (the sessions write these)
└── autodoc/    # what the code IS — the generated code wiki              (NOT created for you — see The four pillars)
```

Those four buckets are the **pillars**, and the axis between them is *who writes them* rather than what they contain — [*The four pillars*](#the-four-pillars), below.

**The first thing to run.** Clone the repo, open Claude Code inside it, and ask it to install itself:

```
git clone https://github.com/abrooks89/agentflow.git ~/agentflow
cd ~/agentflow && claude
```

> Read install/SKILL.md and install agentflow on this machine. Interview me for anything you need.

The install skill interviews you, writes the config, builds the venv, deploys the skills, and then walks you through launching the fleet. **The second thing to run — and the one to remember — is the diagnostic:**

```
python -m agentflow.doctor --project <name>
```

It prints the entire resolved surface in one pass — config, paths, project, checkout, registry, the document globs, autodoc, vault — with one line per check and a verdict at the end. [`docs/INSTALL.md`](docs/INSTALL.md) §9 shows that output annotated, including the three lines that say "not configured" and are supposed to, and why a *correct* fresh install shows the spec and plan globs as `info` rather than a failure (their directories exist and hold nothing until you write a spec).

Never done anything like this before? [**`docs/INSTALL.md`**](docs/INSTALL.md) is the same install written for a reader with no terminal experience. The mechanics — the typed config schema, `provision.py`'s twelve steps, the deployment policy — are [`install/SKILL.md`](install/SKILL.md) and [`install/manifest.md`](install/manifest.md).

---

## The fleet, in one sentence

**Builders build; the manager talks to the human.**

Seven self-paced loops coordinate through one file per project — the **Fleet Registry**. You launch each loop once with a slash command; after that it keeps itself going by scheduling its own next wake. Every loop reads the whole registry each time it wakes and writes only the rows it owns, and it writes them through a CLI (`python -m agentflow.registry`) that takes a lock and refuses any edit outside those rows. Only **dev-manager** ever asks you a question.

That is a contract enforced by a mandated command, not a guarantee enforced by the filesystem. The mechanism underneath it — the edit-lock, the ownership rules, the schema check every loop runs before it acts, and a frank account of what that enforcement is and is not worth — is [`docs/REGISTRY.md`](docs/REGISTRY.md), under *The write surface is fully mediated*, *The honest ceiling* and *The firing contract*; [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) gives the same thing in one paragraph.

```
lead-dev   ──▶┌────────────────────────────┐
jr-dev-1   ──▶│       Fleet Registry       │
jr-dev-2   ──▶│    (status/registry.md)    │◀──▶ dev-manager ══ questions ══▶ HUMAN
jr-dev-3   ──▶│ Agents · Queue · Questions │     (sole asker; promotes drafted   ▲
auditor    ──▶│   Blockers · Directives    │      → approved-for-autonomous;     ┆
steward    ──▶└────────────────────────────┘      runs only while you sit in it) ┆

               Ntfy (all seven loops) ┄┄ notifications only, never a reply ┄┄┄┄┄┄┄┘

  ──▶  reads the whole file, writes only its own rows      ══▶  the ONLY question channel
  ◀──▶ dev-manager reads and writes rows too                ┄┄▲  notifications, not questions
```

*All seven loops write their own rows; only dev-manager asks you anything; everything else just notifies.*

- **Questions → dev-manager → human.** Any loop appends a `## Questions` row (`halted`/`waiting`/`advisory`, always with a `default:`); the steward answers what delegated authority covers every wake; dev-manager pumps them each pass, answers whatever delegated question is still open, and asks the human the rest.
- **Blockers route by ownership:** jr-dev blockers → lead-dev; auditor blockers → dev-manager. Blockers are agent-resolvable by definition — never human decisions.
- **One human surface — exactly one session: dev-manager.** The other six are launched once and never opened again. Promoting a spec to `approved-for-autonomous` in the dev-manager session *is* the standing approval, and it covers the epic's whole lifecycle: build, open a PR, and **squash-merge that PR to your repo's main branch** — with no human reading the PR (`skills/orchestrator/SKILL.md`, Step 9.2 §7c, which skips the merge question outright for that state). That is one of two merge routes: an issue labelled `triaged` and not `epic` is built and merged the same way with no spec, and the `triaged` label is its approval. There is no per-epic dispatch confirm downstream. The rails are a notification-only Ntfy notice and the `## Directives` veto — and `## Directives` is an **abort window, not a review step**: it is re-read immediately before every irreversible action, so a late instruction can veto a merge and can never un-ship one ([`docs/REGISTRY.md`](docs/REGISTRY.md), *The firing contract*). What that authorization is actually worth: **Security & blast radius**, below.

### The seven loops, one paragraph each

- **`lead-dev` — the build hub.** Selects approved epics and triaged issues, proves two pieces of work touch disjoint files and seams, publishes the rest into the Queue lanes for the juniors, and builds the `lead` lane itself. It is the only loop that decides what gets built next, and the selection rule is code (`agentflow.epic_survey.selectable()`), not prose it re-derives each wake.
- **`jr-dev-1` — the first junior builder.** Claims only rows lead-dev published into its own lane; it never selects its own work. Off by default: the `parallel_builders` dial has to open its lane before it will build anything.
- **`jr-dev-2` — the second junior builder.** Identical contract, second lane, `count >= 2`.
- **`jr-dev-3` — the interim fourth builder, and it is labelled interim in the code.** It is a hand-copy of `jr-dev-2` on a third junior lane, added as a stopgap and gated on `count == 3`. The `lead-dev-as-foreman-and-n-junior-lanes` epic replaces the whole hardcoded enum with a count-driven set and retires it (`skills/_shared/agentflow/registry.py`, the *Vocabulary* block). Naming it here rather than hiding it is deliberate: it is in `AGENTS`, in `BUILDERS`, in `LANES`, in `commands/`, and in the gates — a doc set that stopped at six is how those gates went quiet about it in the first place.
- **`auditor` — post-build intent feedback.** Reads the last N *done* epics and asks whether the epic's **intent** was accomplished — its use cases, not its task list — verifying live where the project's `verification.strategy` allows. It is read-only against the product and files drafted gap specs for a human to promote. Every wake it also checks each ask of an open issue a merged change claims to fix, and closes the fully fixed ones; the seat that built a fix never closes it.
- **`steward` — maintenance, and the registry janitor.** Sweeps what *time* breaks rather than what work creates: spec frontmatter that lags merged reality, stale lane claims, an orphaned deploy lock, deployed-vs-source and schema-vs-code drift, backup retention, closure debt (open issues a merged change claims to fix; it reports them and closes none). It holds the four registry-repair verbs — `reap`, `stand-down`, `repair` and `cull` — which the CLI refuses to anyone else. It runs fine with no builder active.
- **`dev-manager` — your seat, and the only one.** Answers the fleet's questions, promotes specs from `drafted` to `approved-for-autonomous`, triages tracker issues, and owns the blockers the auditor escalates. **It has no unattended mode: it does not self-wake and schedules nothing** (E-0220 moved the sweep and the cull to the steward), so it exists only while you have the session open. That is the trade — the steward pumps between your visits, and anything needing a decision waits for you in `## Questions`.

All *coordination* between those seven is registry rows. The only per-loop files are private continuation state (`status/<agent>-state.md`, plus the auditor's `audit-ledger.md` and `audits/`), and each is single-writer: exactly one loop writes it, its own.

### The four pillars

A project's data root holds four buckets, and the axis that separates them is **who writes them** — not what they contain. That is what stops them collapsing into one another as the corpus grows:

| Pillar | Answers | Written by | Scaffolded by install? |
|---|---|---|---|
| `design/` | what we **intend** to build | a human | yes |
| `status/` | what the fleet is **doing** | the loops | yes |
| `wiki/` | what we **learned** | the sessions | yes |
| `autodoc/` | what the code **is** | generated from the code | **no — see below** |

A file whose writer does not match its bucket is in the wrong bucket, and that is a sharper test than asking what a file is *about*: a postmortem and a status board can both be "about" an epic while belonging to different pillars.

**Three of the four are scaffolded by the installer. `autodoc/` is not, and this is the one place this README will not round up.** `install/provision.py` creates eight directories per project — `design/{specs,plans,use-cases,suggestions}`, `status`, `wiki/pages{,/done,/archived}` — and no `autodoc/`. Nor does `/autodoc-init` create it on a fresh install: it exits unless an `autodoc_root` already resolves for the project (the project profile's override, or the machine-level `environment.autodoc_root`).

**To build a corpus:** set `autodoc_root` in your `environment.md` to an existing directory (create it first — the config loader refuses a path that does not exist, and nothing creates it for you); install Node LTS (with npm) if the machine lacks it, and re-run `python install/provision.py --autodoc` from the checkout — add `--no-auto-allow` if you declined the auto-allow hooks, because a re-run without it writes them back ([`docs/INSTALL.md`](docs/INSTALL.md) §13); then run `/autodoc-init`. The re-run is not optional on a machine installed with autodoc off, which is the default: provision step 6 (`npm ci`, then the `autodoc_graph` install into the suite venv) was skipped, and without it `/autodoc-init` stops at its Step 0 guard — `doctor` reports the gap as a `warn` naming the same remedy. The mapping phase of both autodoc skills also needs the `Workflow` session tool, with no single-agent fallback (below). Know one thing first — that field is machine-level, so it points every project without its own override at that one directory until a corpus is built there; `doctor` accepts it for each of them (`info` until the corpus is built, `ok` after). Both autodoc paths resolve per project through `resolve_autodoc_root`, so the per-project override is honored when the corpus is first built and on every refresh; on a box with more than one project, both take the project as `AUTODOC_PROJECT=<name>` and refuse to start without it. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#four-pillars-separated-by-authorship) has the resolution order.

**Before you point that field at anything, know that it is machine-level**, which is to say it is one directory for the whole box. Every project without its own override resolves its autodoc pillar to that same directory. Once a corpus is built there, its `.manifest.json` names the project it documents, and every other project skips it: it falls to its derived `<data_root>/projects/<name>/autodoc/` if that exists, or to no autodoc root. Before that first build nothing records an owner, so `doctor` cannot tell which project the directory is for: it checks that the root resolves, whether a corpus has been built there (`info` before, `ok` after) and whether the autodoc runtimes are installed (`warn` if not). `resolve_autodoc_root` *does* define a per-project override in `projects/<name>.md`, ranked above the machine field, and a derived `<data_root>/projects/<name>/autodoc/` below it (taken only when that directory already exists); `doctor`, the use-case touchpoint tools, `/autodoc-init`, `/autodoc-update` and `graph-query` all honor that precedence, so a corpus is built and refreshed at the same resolved root. A corpus already built at the machine-level root is not followed when a per-project override points elsewhere: while the override names an empty directory the resolver refuses both paths, until the corpus is moved there or the machine-level root is cleared.

What mirrors to the optional vault today is narrower than the three document pillars: design specs and plans, and nothing else. The `backlog` and `project` skills hand the delegated `vault.mirror_helper` a spec, the `solution` skill hands it a plan, and no other skill calls it. **`autodoc/` is not mirrored yet**, so a built corpus is untracked: its pages can be re-rendered from the graph store (`<autodoc_root>/.source/graph.db`, [`/autodoc-init` Step 4](skills/autodoc-init/SKILL.md#step-4--write-records-to-the-store-then-render-deterministic)), but that store lives inside the same tree, so deleting the tree loses both ([Step 7](skills/autodoc-init/SKILL.md#step-7--commit-the-autodoc-retired) says why nothing commits it). `wiki/` pages are not mirrored either.

The pillar split has a mechanical consequence at the boundary: **`status/` is lock-protected coordination and never mirrors.** The reason, not just the prohibition: a sync client copies a file whenever it notices the bytes change, and it cannot see the `mkdir` edit-lock the registry's write path takes — so it races a lock it does not know exists, and the loser is a half-written coordination file that every loop then reads as truth. No skill hands the mirror helper a `status/` path. The helper ships with the vault, not this repo, so whether it refuses one is not checked here.

**Glossary** — terms the rest of this page uses:

| Term | What it means here |
|---|---|
| **`ScheduleWakeup`** | Claude Code's built-in timed self-wake tool, and the reason a loop is *self-paced* rather than cron'd. A loop's continuation is one call — `ScheduleWakeup(delaySeconds=…, reason=…, prompt="/lead-dev --project <name> <target>")` ([`skills/lead-dev/SKILL.md`](skills/lead-dev/SKILL.md) §0) — and the `prompt=` is the load-bearing part: it is what re-enters the same loop on the next wake, so it carries the resolved project and target as literal text, never the `$ARGUMENTS` token, which does not expand on a self-wake. Check the tool is in your session's tool list before launching a loop - `python -m agentflow.preflight --tools "..."` does it and prints the remedy - because a session without it produces a loop that runs once and never wakes again, which is indistinguishable from one that finished. Declared in `install/manifest.md` alongside `Workflow`, which the wave engine and the two autodoc skills need for the same reason. |
| **`CronCreate`** | Claude Code's session-scoped cron tool, used by every self-waking loop for exactly one thing: the standing **rate-limit recovery guard**, a second wake leg beside the hourly `ScheduleWakeup`. It exists because a firing that a quota or rate-limit window (the account's, or one model's) refuses before its first tool call never reaches its own `ScheduleWakeup`, so the loop cannot recover from inside itself. Session-scoped and in-memory - it dies with the session, auto-expires after 7 days, and reports nothing during an outage - so it is **recommended, not required**: a session without it loses unattended recovery and nothing else. It is not a daemon and does not touch the single-daemon budget. |
| **firing** | One wake of one loop. A *state-bearing* firing is one that touches coordination state; each begins with the whole-registry read set out in `docs/REGISTRY.md`'s firing contract. |
| **epic** | One spec-sized unit of work — a `design/specs/` functional design and a `design/plans/` technical plan sharing a slug — tracked as one `E-NNNN` row in the registry's `## Queue`. |
| **lane** | One column of claimable Queue work: `lead`, `jr-dev-1`, `jr-dev-2`, `jr-dev-3`. lead-dev publishes rows into lanes and computes file/seam disjointness at publish time; the juniors cannot self-select. |
| **`approved-for-autonomous`** | The `current_state` value in a spec's frontmatter that makes its epic dispatchable. Only dev-manager sets it, promoting from `drafted`. |
| **Ntfy** | Push notifications via [ntfy](https://ntfy.sh). Optional — `notify.kind: none` turns it off. One-way fleet-wide: a notice never expects a reply. |
| **the builders dial** | `environment.parallel_builders`, the multi-builder module's dial — two fields: `enabled`, the on/off switch (defaults `false`), and `count`, how many **junior builder lanes** run (defaults `1`; must be `0`, `1`, `2` or `3`, and any `count` above `1` is rejected unless `enabled: true` — [`skills/_shared/agentflow/config.py`](skills/_shared/agentflow/config.py), the `parallel_builders` block). By count: `0` — `lead-dev` standalone, no junior launched; `1` — `jr-dev-1` active; `2` — `jr-dev-1` + `jr-dev-2`; `3` — the interim third junior lane, `jr-dev-3`, as well. That is the gate each junior executes at wake ([`skills/jr-dev-1/SKILL.md`](skills/jr-dev-1/SKILL.md), *Module gate*); the SessionStart hook injects both fields as `[active builders] enabled: <bool>, count: <n>`. |

Binding schema, firing contract, and question lifecycle: [`docs/REGISTRY.md`](docs/REGISTRY.md).

---

## Security & blast radius

Read this before you install. The fleet is built to run unattended, and unattended means what it says.

- **Six of the seven loops run with permission prompts off.** Everything but `dev-manager` is launched as `claude --dangerously-skip-permissions` ([`install/SKILL.md`](install/SKILL.md), *Launch the fleet*) and can run anything the account can run. That is deliberate: a loop that self-continues while nobody is watching does not *fail* at an approval prompt, it **hangs** — and a hung loop is indistinguishable from a working one until you look. The containment is the box, not the prompt.
- **Two routes authorize a merge, not just a build.** (a) Promoting one spec to `approved-for-autonomous` is a standing approval for that epic's whole lifecycle: build → open a PR → `gh pr merge --squash` to your repo's main branch → and then whatever your git host runs on a push to that branch. (b) An issue on your tracker labelled `triaged` and not `epic` is built and merged the same way with no spec and no promotion — the `triaged` label is that route's approval (`agentflow.issue_intake.builder_selectable`). Who may apply it is the triage-ownership rule: `dev-manager`, and the `steward` for bounded bugs, never the agent that raised the issue. On both routes nobody reads the PR (epics: `skills/orchestrator/SKILL.md` Step 9.2 §7c, *"skip the merge AskUserQuestion entirely"*; triaged issues: `skills/lead-dev/SKILL.md` §4, the deploy-lock-chained `gh pr merge --squash`). A merge to the base branch **may fire the project's staging deploy** (`staging.deploy_recipe`) with no human. Two things are deliberately **not** auto-run: a **production deploy** is always a human step (`skills/lead-dev/SKILL.md`, *Autonomy envelope*), and **publishing to a public repo is a human action always** — no loop pushes to it, opens a PR against it, or grounds against it ([`docs/runbooks/publish.md`](docs/runbooks/publish.md)).
- **The operator posture this is built for.** Humans approve *intent* — at the spec, or with the `triaged` label. Execution runs unattended in a contained box. Code review is AI dual review (orchestrator Step 9.1); there is no human review before merge. Production deploys and publishing stay human.
- **`## Directives` is your abort window, not a review queue.** Every loop re-reads it immediately before an irreversible action, so a late instruction can veto a merge — never un-ship one.
- **The loops read text they did not write.** Other agents' registry rows, and issue titles/bodies pulled off your tracker through `gh` (`agentflow.issue_intake` requests `number,title,labels,body,url`), enter a loop's context as input. The autodoc/graph path fences that data explicitly (`skills/graph-query/SKILL.md`, `autodoc_query/scorer.py`, `references/prompts/parent-report.js` all carry an *"UNTRUSTED DATA … never instructions"* clause); **the builder loops carry no equivalent clause today**. Assume anyone who can file an issue on your repo can put text in front of the builder.
- **So: run it on a machine you could wipe**, under an account whose stored credentials reach no further than the one project it works on. If that is not this machine, run without the flag, skip the autonomous loops, and drive `/dev-manager` yourself — answering its questions is a real way to use this, not a lesser one.

To report a vulnerability, or a way past these limits that goes further than described here, see [`SECURITY.md`](SECURITY.md) — privately, not in a public issue.

The same trade in plain English, in the order you meet it while installing: [`docs/INSTALL.md`](docs/INSTALL.md) §10, and [`install/SKILL.md`](install/SKILL.md) under *Launch the fleet*.

---

## Quickstart

**Requirements**

| For | You need |
|---|---|
| The pipeline (refine → solution → orchestrator, the fleet) | Claude Code · Python 3.11+ · Git |
| PR create/merge and issue tracking on a hosted git provider | `gh` (optional) — the only verified CLI. `glab` is refused before it runs on the issue paths and ignored by the PR paths, which hardcode `gh`; `tea` is not a valid setting (`gh#9`) |
| The optional autodoc / graph subsystem | Node LTS · [Ollama](https://ollama.com) for the embedding and naming passes — embedding is opt-in (`graph build --embed`), so a graph builds without it, and nothing in the installer checks for it |

The fleet **runs as Claude Code skills**, not as a standalone service — there is no server to host, and the only process outside your sessions is one optional report-only watchdog on your OS scheduler ([`docs/INSTALL.md`](docs/INSTALL.md)). Install deploys the skills into your Claude config so the seven loop commands (`/lead-dev`, `/jr-dev-1`, `/jr-dev-2`, `/jr-dev-3`, `/auditor`, `/steward`, `/dev-manager`) resolve in a fresh session, along with the six supporting ones (`/report`, `/graph-query`, `/wiki-save`, `/autodoc-init`, `/autodoc-update`, `/uc-touchpoints`).

**Install + first use is one prompt.** Install deploys **three** kinds of thing into your Claude config: every directory under `skills/` (except `_shared`, which is the pip package), the `commands/*.md` wrappers, and one hook script, `skills/_shared/scripts/plan-lint.sh`, deployed to the stable path `<claude_root>/scripts/plan-lint.sh` so the host copy is never unmanaged (`_deploy_sources` in [`install/provision.py`](install/provision.py) — its docstring is the other place that says so, because a deploy function that under-reports what it deploys is how a file ends up unmanaged on the host). Provision registers no hook for that script, so nothing runs it until you add one yourself. The installer itself lives at `install/` in the repo root — outside all three — so it is never deployed and no `/install` command exists; you point Claude Code at the file directly. Clone the repo (`git clone https://github.com/abrooks89/agentflow.git ~/agentflow`), then:

```
cd ~/agentflow && claude
```

and say:

> Read install/SKILL.md and install agentflow on this machine. Interview me for anything you need.

The install skill is the single hands-on guide: it interviews you for the environment + first project, runs `install/provision.py`, deploys the fleet skills and hooks, seeds the registries, makes the suite venv the box's default python, and then **walks you through launching and operating the seven loops**. New to this? [**`docs/INSTALL.md`**](docs/INSTALL.md) is the plain-English walkthrough of the same install, step by step. Full runbook: [`install/SKILL.md`](install/SKILL.md) (mechanics in [`install/manifest.md`](install/manifest.md)).

**What install changes on your machine** — worth knowing before you run it:

- A suite virtualenv at `<suite_root>/.venv`, with PyYAML and an editable install of the `agentflow` package (provision steps 4–5).
- A line in your shell profile that activates that venv (`source .venv/bin/activate`, or the PowerShell `$PROFILE` equivalent) — **this displaces whatever `python` the box had** (`install/SKILL.md` Step 7).
- The fleet skills, slash commands and the one hook script deployed under your Claude config dir — `~/.claude/skills/`, `~/.claude/commands/`, `~/.claude/scripts/` by default, symlinked on Unix; on Windows the skill *directories* are junctions and the individual command and script *files* are copies, tracked in `<claude_root>/.agentflow-deploy.json` (provision step 7).
- A **SessionStart** hook registered in your Claude settings file (`~/.claude/settings.local.json` by default) that injects the resolved config into each session (provision step 9).
- **PreToolUse auto-allow hooks written into that same settings file** (provision step 8, via [`skills/orchestrator/bootstrap.py`](skills/orchestrator/bootstrap.py)). They return `allow` for the `Edit`, `Write` and `Agent` tools and for a list of `Bash(…)` globs including `Bash(git *)`, `Bash(chmod *)`, `Bash(mv *)` and `Bash(curl -s *)`. `settings.local.json` is the *project* settings file for a session rooted at your home directory — not the user-global `settings.json` — so these apply to every session you start from there, which for the fleet is every session. Read `BASH_AUTO_ALLOW_PATTERNS` in that file before you decide you're happy with it. To install without them, decline the hooks when the install interview asks, or pass `--no-auto-allow` to `install/provision.py`; to remove ones an earlier install wrote, see [`docs/INSTALL.md`](docs/INSTALL.md) section 13.
- The project data tree scaffolded under your configured `data_root` — eight directories per project (`design/{specs,plans,use-cases,suggestions}`, `status`, `wiki/pages{,/done,/archived}`), a seeded registry, and the use-case spine (provision step 11). Not `autodoc/` — nothing here creates it, and `/autodoc-init` does not create it either unless an `autodoc_root` already resolves for the project ([*The four pillars*](#the-four-pillars)).

Putting the suite venv first on the session PATH is the whole story on a **dedicated box or user account**; on a machine that also builds Python products, set `canonical_repo.python` in each product's profile so the builders run the product's suite with its own interpreter, never the suite venv (the first live run found this the hard way). `provision.py` hard-fails loudly on missing required config (a missing named env var or unresolvable path is never silently defaulted); Node tree-sitter installs **only** when autodoc is enabled.

**Before you launch a loop, check three tools the installer cannot provide.** `ScheduleWakeup`, `Workflow` and `CronCreate` come from the Claude Code session, not from anything here, and none of the three absences announces itself. The check compares a list *you* supply against what the seat needs, so ask Claude for its actual tool list and pass that — a list typed out of this page proves only that you can type:

```
python -m agentflow.preflight --tools "<your session's tool list, comma-separated>" --wave
```

It exits 1 and prints the remedy for anything missing. `--wave` is what adds `Workflow` to the requirement: it is needed for wave execution and by `/autodoc-init` and `/autodoc-update`, whose mapping phase runs through it and has **no** single-agent fallback, so drop the flag only for a seat that will run neither waves nor autodoc, and add `--no-self-wake` for one that does not self-continue. `CronCreate` sits at a **recommended** tier — it is reported on every run and prints `DEGRADED` rather than `MISSING`, because a session without it still runs, wakes and builds and loses only the 5-hour rate-limit recovery guard each loop schedules; the exit code never turns on it. [`docs/INSTALL.md`](docs/INSTALL.md) carries the full step, including what to do when one is absent.

---

## What's in the box

### The pipeline skills — `refine` → `solution` → `orchestrator`
Work flows from a functional design (`refine`) to a technical plan (`solution`, via the vendored `writing-plans`) to an execution engine (`orchestrator`) that runs the plan as waves of parallel subagents with per-task review, regression auto-fix, and a status board. Supporting skills: `project`, `choose-next`, `backlog`, `tdd-loop`, `polish-mode`, `wiki-save`, `autodoc-init`, `autodoc-update`. The pipeline is **pure Python + PyYAML + Git** — no Node, no web server, no database engine.

### The autodoc GRAPH subsystem — the showcase
`autodoc` turns a source repo into a persistent, cross-linked code wiki. Underneath the wiki is a **two-tier knowledge graph** (its own installable package, `skills/autodoc-init/references/graph/`):

- **Exact symbol tier.** A Node tree-sitter pass extracts every symbol — functions, classes, routes — into deterministic JSONL. These are `tier='symbol'`, `origin='ast'`: exact, rebuilt from scratch each run, a pure function of the code.
- **LLM concept tier.** Agents write architectural concepts (`tier='concept'`, `origin='agent'`) and join them to the code through **`documents`** edges — the tier join. The relation vocabulary is **closed at 8 relations**; adding one is a schema change, not an agent decision, because an open vocabulary makes traversal unqueryable.
- **Derived communities replace hand-written area maps.** NetworkX **Louvain** over the AST edges partitions the graph into communities — every symbol lands in exactly one, or the run hard-fails naming the orphan. The old hand-maintained "areas" map is gone; the map is now *derived* from the code.
- **Deterministic renderer.** The store is primary; a single renderer is the *only* writer of the markdown tree. Rendering twice over an unchanged store is byte-identical — so link integrity and frontmatter completeness are true by construction, not audited after the fact.
- **Graph-query — light graph-RAG with provenance on every item.** A **deterministic router** (no LLM round-trip in the default path) sends a question `local` or `global`, retrieval crosses the tier join, and every returned item carries its full traversal path with each hop's `rel`/`origin`/`confidence`. An exact AST fact, an agent assertion, and a speculative "dreamed" link are always distinguishable — anything reached through a dreamed edge is marked `[via hypothesized link]` in its own citation.

```
$ graph-query --db "$AUTODOC/.source/graph.db" --corpus agentflow --pretty 'what decides whether an epic is `dispatchable`, and where does `ready_set` come in?'

routing: local (rule=entity_resolution) evidence={"entity_ids": ["sym:agentflow:skills/_shared/agentflow/epic_survey.py:function:dispatchable", "sym:agentflow:skills/_shared/agentflow/epic_survey.py:function:ready_set"], "tokens": ["dispatchable", "ready_set"]}
TRUNCATED: fanout_cap(25) dropped 35 neighbors of con:agentflow:component:epic-survey; retained_entity_cap(50) trimmed 17 lower-ranked entities; retained_entity_cap(50) trimmed 344 tier-crossed entities
50 context item(s):
  - dispatchable (skills/_shared/agentflow/epic_survey.py:2046-2132)  [tier=symbol origin=ast score=1.00 min_conf=1.00 depth=0]
      def dispatchable(
  - ready_set (skills/_shared/agentflow/epic_survey.py:1439-1477)  [tier=symbol origin=ast score=1.00 min_conf=1.00 depth=0]
      def ready_set(records: list[EpicRecord]) -> list[EpicRecord]:
  ...
  - _done_slugs (skills/_shared/agentflow/epic_survey.py:1402-1436)  [tier=symbol origin=ast score=0.75 min_conf=1.00 depth=1]
      def _done_slugs(records: list[EpicRecord]) -> set[str]:
  ...
  - epic_survey (skills/_shared/agentflow/epic_survey.py, skills/choose-next/SKILL.md, skills/lead-dev/SKILL.md, skills/backlog/SKILL.md, skills/dev-manager/SKILL.md, skills/auditor/SKILL.md, docs/DATA-LAYOUT.md, skills/_shared/tests/test_epic_survey.py)  [tier=concept origin=agent score=0.52 min_conf=0.70 depth=1]
      Walks the project data tree and reconciles spec, plan, and status files into one merged Epic Record per epic slug — the
  ...
timings ms: embed_ms=0, traverse_ms=83, tier_cross_ms=3361, total_ms=3454
```

*(Real output, trimmed — `...` marks elided items — from this repo's own corpus, built at commit `fe37970` (a working-repository commit, which the public cut does not contain). `--db` is the graph store `/autodoc-init` builds at `<autodoc_root>/.source/graph.db`; `--corpus` is the repo stem it was built under; without `--pretty` the same result prints as JSON. The backticked names are what fire `rule=entity_resolution`: the router resolves them to symbols and seeds from them at score 1.00, then the traversal reaches the concept `epic_survey` across the tier join (`origin=agent`, `min_conf=0.70`). Local mode also embeds the question (`--ollama-url`, default `$AUTODOC_OLLAMA_URL`); this corpus carries no embedding rows, so the named symbols are the only seeds, and this capture ran with a stand-in embedder — hence `embed_ms=0`. The line spans were true at `fe37970` and are the one part of a citation that rots, so check the symbol, not the number. The citation format `name (file:start-end)` is defined in [`autodoc_query/types.py`](skills/autodoc-init/references/graph/autodoc_query/types.py). The caller's model writes the final prose; the graph hands it cited, ranked context.)*

An **adapter seam** (`skills/autodoc-init/references/graph/autodoc_graph/adapter.py`) is the fork point: every consumer programs against one abstract storage interface, no SQL leaks above that line, so the SQLite + `sqlite-vec` backend can be swapped for a pgvector fork as a drop-in.

**Running more than one project — say it on the slash command.**

```
claude --dangerously-skip-permissions "/lead-dev --project agentflow"
```

Every loop takes `--project <name>`, the name beats the session's injected default, and each self-waking loop carries it into its own `ScheduleWakeup` continuation (dev-manager has none; it runs only while a human has it open) — so the fleet stays on the project you launched it on, wake after wake. One box, several fleets, no settings juggling. **Do not export a shell variable:** Claude Code's settings `env` overrides the process environment, so `AGENTFLOW_PROJECT=other claude ...` is silently ignored and the session quietly runs the *default* project — a wrong-project run that looks exactly like a right one. That override is why the argument travels on the prompt instead. Scripted launches do the same: plain `claude` from the launch directory with `--project <name>` on the slash command. Do not use a per-project `--settings` file: its `env` block replaces `settings.local.json`'s instead of merging, so every collected env var disappears and the lane fails at its first step; see [`install/SKILL.md`](install/SKILL.md).

---

## Evidence

This repo does not ask you to take its correctness on faith.

**Test surface** — every layer below runs on the full CI matrix, `ubuntu-latest` **and** `windows-latest`, nightly whenever master has moved since the last green nightly ([`nightly.yml`](.github/workflows/nightly.yml) calls [`ci.yml`](.github/workflows/ci.yml)) and on manual dispatch. A public repository runs the full matrix on every pull request and push; a private copy runs a trimmed set on a pull request, dropping most `windows-latest` legs, and no job on a push to master. The *WHAT RUNS WHERE* comment at the top of `ci.yml` is the authority. The right-hand column is how each number was produced, so you can reproduce it. **The counts are stated as collected on CI's `ubuntu-latest` leg**, which is the baseline the guard below compares against: collection can differ by platform, so a mismatch elsewhere says nothing about drift and is deliberately not treated as a failure.

| Layer | Tests | Counted with |
|---|---:|---|
| autodoc graph + query | **480** | `cd skills/autodoc-init/references/graph && python -m pytest --collect-only -q` |
| tree-sitter inventory (Node) | **319** | `cd skills/autodoc-init/references/inventory && npm ci && node --test` |
| orchestrator `wave_group.js` (Node) | **9** | `node --test skills/orchestrator/tests/wave_group.test.mjs` |
| `_shared` (config, epic-survey, registry, plan parser, waves) | **5301** | `cd skills/_shared && python -m pytest --collect-only -q` |
| install / provision | **361** | `python -m pytest install --collect-only -q` |
| acceptance harness | **44 wired assertions** | `python harness/check.py --self-test` |

**No commit is pinned above, on purpose.** These counts used to be stamped "measured against master `<sha>`", and both halves rotted: nothing re-writes the sha on merge, and the numbers drifted four separate times underneath it. They are now re-derived from this table on every CI run by [`skills/_shared/tests/test_evidence_table.py`](skills/_shared/tests/test_evidence_table.py) — it parses these rows, runs each command from the directory the row names, and fails if a count no longer matches. A live check against the tree you are reading beats a snapshot of a commit you cannot resolve.

**What that guard does and does not cover, stated rather than implied.** It runs every row whose command it can execute *in the environment it is running in*, and it **declares** the rest unrunnable-with-a-reason instead of skipping them, so a new unrunnable row turns it red. It also compares counts only on the baseline platform, for the reason above. Three rows — `_shared`, install and harness — need only the suite's own interpreter and are checked everywhere; that is the guard's floor. The other three declare a prerequisite. The orchestrator row needs `node` and `bash`: the `shared-tests` CI job that hosts the guard installs Node for it, so it is checked there, and the guard fails rather than excuses it if either is missing under CI (CI's `orchestrator-node-tests` job also runs the same file on both platforms). In `shared-tests` that leaves two rows excused: the tree-sitter inventory row, whose command performs a network `npm ci` the guard never runs, and the autodoc-graph row, whose dependencies a different job installs. Run the same suite on a machine that has those dependencies — a developer checkout — and the graph row is checked too. The inventory row is exercised by CI's `inventory-tests` job on both platforms, which proves those tests pass but does not compare their count to the number above; it is the only figure here that no run of this guard can ever check, in any environment.

**Adversarial-review methodology.** Every epic runs the same loop: **build → dual adversarial review → remediation → re-review → independent closure verification.** Reviews are hostile on purpose — a reviewer's append-attack becomes a committed regression test — and closure is verified against the tree, not against the report that claimed the fix (see *Scars*, below).

**Prose programs get property tests.** Because skills are prose whose interpreter changes underneath them, the [`harness/`](harness/README.md) records how the live skills behave (plan emission, a full build wave, a builder wake-continuation) and asserts properties of that recording. It keeps no baseline to diff against, so proving behavior was preserved after a change means re-recording scenarios A, B and D and re-running `check.py`. Recordings are evidence of a run; the tracked fixtures are the inputs, and `--self-test` plus `--only C,F,G` run on any clone.

---

## Scars — what failure taught us

Every mechanism below exists because something broke first. The design principle is **enforcement follows failure**, and we own it publicly.

> **Provenance.** The public repository is a **single-commit cut**: its history begins at one
> commit that carries the whole tree, and the development history behind it is not published.
> That history lives in two private repositories: the archived `agentflow-dev`, and after it the
> maintainers' working repository, whose pull requests the CHANGELOG's `(#N)` numbers refer to.
> **No commit of either resolves in the public cut, and four shas cited below — `5d6b0cf`,
> `4287191`, `a25266b` and `024e8de` — are `agentflow-dev` commits.** Each entry that cites one
> also names the artifact the failure left in *this* tree, which does resolve. The plain `gh#N`
> references throughout this tree point at that working repository's tracker too, so in a public
> copy they are labels, not links: they do not resolve.

- **A template variable killed an entire phase.** An externalized prompt hard-required a `{{kind}}` template var that real callers never supplied; the strict engine killed the whole Refresh phase and runs quietly returned `communitiesRefreshed: 0`. → A **template-vars-vs-contextSpec drift gate** now asserts that every variable the engine hard-requires is declared by its prompt's context spec — swept across all seven templates, it immediately found a second casualty of the same family. *(agentflow-dev `5d6b0cf`; in this tree: the test `every template variable the engine hard-requires is declared by the contextSpec` in [`skills/autodoc-init/references/inventory/test/prompt-modules.test.js`](skills/autodoc-init/references/inventory/test/prompt-modules.test.js))*

- **A credit wall killed builders mid-run; their work should not die with them.** Records that traveled home *through* agent returns were lost when an agent was cut off. → A **disk-based records contract**: every record-producing agent writes its output to `<runDir>/records/<label>.json` and returns only a light manifest; parents read their children's files directly, and page bodies never ride inline — a 256KB `RETURN_BUDGET_BYTES` guard throws if bulk data ever leaks back through a return. Completed work survives a sibling's death. *(agentflow-dev `4287191`; in this tree: [`skills/autodoc-init/references/init-workflow.js`](skills/autodoc-init/references/init-workflow.js), `recordsPathFor` and `RETURN_BUDGET_BYTES`. The motivating run — 5 of 24 agents cut off, 19 agents' work intact — is operator history.)*

- **Prose misjudged which work was ready to dispatch.** Work-selection reasoned about in prose drifted between runs. → Selection **moved into code**: `selectable()` in `agentflow.epic_survey` is the single, testable definition of what may be dispatched; the skills call it instead of re-deriving the judgment each time — and instead of composing `ready_set()` and `dispatchable()` by hand, which they did until an order that could not be inferred from the names silently stranded approved work. *(agentflow-dev `a25266b`; in this tree: [`skills/_shared/agentflow/epic_survey.py`](skills/_shared/agentflow/epic_survey.py))*

- **A fix was reported that never shipped.** A remediation ran through a heredoc patcher whose assert-less `str.replace` silently never matched; the lazy gate shipped unchanged while the report claimed it was fixed. → **Reports are verified against git, not memory.** Every claimed fix is re-checked against the committed tree via `git grep`, the drift gate now requires *exactly one* marked template-engine block per workflow file and byte-compares it against `engine.js`, and the reviewer's append-attack is a committed regression test. *(agentflow-dev `024e8de`; in this tree: the tests `workflow-inlined engines are byte-identical to engine.js` and `drift gate catches the reviewer append-attack (second drifted block)` in [`skills/autodoc-init/references/inventory/test/prompt-modules.test.js`](skills/autodoc-init/references/inventory/test/prompt-modules.test.js). That the gate was revert-checked live — driven red, then restored green — is operator history.)*

- **A partial batch persisted after a failure.** A half-written batch left the store in a state no one intended. → **Pre-validate-all + rollback + `close()` never commits.** The concept-tier record writer validates *every* record before the first write, so a bad record leaves the store untouched; and the storage adapter's `close()` deliberately never commits — pending state at close time is a failed batch that must be discarded, not salvaged. *(in this tree: [`skills/autodoc-init/references/graph/autodoc_graph/records.py`](skills/autodoc-init/references/graph/autodoc_graph/records.py) and [`skills/autodoc-init/references/graph/autodoc_graph/adapter.py`](skills/autodoc-init/references/graph/autodoc_graph/adapter.py))*

---

## Manifesto

agentflow is a set of **programs whose interpreter is a language model**: the procedures are `SKILL.md` files written in prose, and a Claude Code agent is the runtime that reads and executes them — no compiler between intent and behavior, and a program you can read in one sitting.

Its state lives in a **database whose pages are files**: every spec, plan, queue lane, and coordination row is markdown a human can diff and edit by hand, with a `mkdir` lock for concurrency — git history is the log for the code, and the data root, which git does not manage, keeps an append-only `registry-log.md` beside its status boards — deliberately inspectable, and with at most one daemon in the whole suite: an opt-in scheduled watchdog that reports and never acts.

And its rules obey one law — **enforcement follows failure**: not a single gate, lock, or contract in this repository was designed in the abstract; each is the scar of a specific failure, and the *Scars* section above names them.

---

## More

- [`docs/INSTALL.md`](docs/INSTALL.md) — the plain-English install walkthrough, for a reader with no prior terminal experience.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the deep dive: prose-as-code, markdown-as-database, the graduation criterion, the fleet registry, and the graph subsystem.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — what's near, what's mid, and what we are deliberately not doing.
- [`docs/REGISTRY.md`](docs/REGISTRY.md) · [`docs/DATA-LAYOUT.md`](docs/DATA-LAYOUT.md) — the coordination surface and the single-root data layout.
- [`docs/runbooks/publish.md`](docs/runbooks/publish.md) — the working-repo / publish-repo split, and the human-only procedure for pushing to a public repo. No loop ever runs it.
- [`skills/_shared/SCHEMA.md`](skills/_shared/SCHEMA.md) — the frozen typed-config schema both config files are validated against.
- [`harness/README.md`](harness/README.md) — the acceptance harness: property tests for prose programs.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to run the suites and get a pull request past the CI gates, written for a person.
- [`SECURITY.md`](SECURITY.md) — how to report a vulnerability privately, and how secrets are handled.
- [`ACKNOWLEDGEMENTS.md`](ACKNOWLEDGEMENTS.md) — vendored code, prior art and runtime dependencies.
- [`LICENSE`](LICENSE) — MIT.

*Provenance note: agentflow's mechanisms were hardened while operating a production multi-tenant platform — a separate codebase, neither this repo nor `agentflow-dev` — whose own repository merged 430+ PRs in June–July 2026. Claims about this repository are checkable in-tree; claims about that operating history are labeled as such.*
