# agentflow

[![CI](https://github.com/abrooks89/agentflow/actions/workflows/ci.yml/badge.svg)](https://github.com/abrooks89/agentflow/actions/workflows/ci.yml)

**agentflow is an installable AI software-delivery system. It is a fleet of seven Claude Code loops, and six of them are self-waking. The loops take work from idea → spec → plan → built, tested, reviewed, merged code. They coordinate through one shared markdown file, the registry. The fleet has exactly one channel to the human.**

---

## What it is

agentflow is **a set of Claude Code skills you install on your own machine.** It has no server for you to host. A "loop" is one Claude Code session that runs a skill written in prose. A loop schedules its own next wake.

Only one thing runs outside a session: an optional watchdog on your OS scheduler. The watchdog reads the registry and sends a notification when a loop misses a wake. It never relaunches anything.

- **What it does.** You promote a spec in one session. The other loops then write the plan, build it in a git worktree and review it. They open a pull request, merge it and send you a notification about the result.
- **What it writes.** Markdown: specs, plans, wiki pages and one registry file per project. The result is a database whose pages are files. You can diff, grep and edit these files by hand.
- **Who talks to you.** Exactly one loop (`dev-manager`). Every other loop sends notifications and never asks you a question.
- **Who it's for.** One developer who works on their own project repos, on a machine that they own. agentflow has no accounts and no roles, and it is not multi-user. The fleet has exactly one human. Six of its seven loops run with permission prompts off, so the machine is the containment ([**Security & blast radius**](#security--blast-radius)).
- **What it is not.** It is not a service, a hosted product or a dashboard. [`docs/ROADMAP.md`](docs/ROADMAP.md) names these as intentional cuts.

**The directory it creates**, after you choose a `data_root` and a project name:

```
<data_root>/projects/<name>/
├── design/     # what we INTEND — specs, plans, use cases, suggestions   (a human writes these)
├── status/     # what the fleet is DOING — registry.md + status boards   (the loops write these)
├── wiki/       # what we LEARNED — durable pages + a generated hot tier  (the sessions write these)
└── autodoc/    # what the code IS — the generated code wiki              (NOT created for you — see The four pillars)
```

These four directories are the **pillars**. Each pillar is defined by *who writes it*. See [*The four pillars*](#the-four-pillars), below.

**The first thing to run.** Clone the agentflow repo and open Claude Code in it:

```
git clone https://github.com/abrooks89/agentflow.git ~/agentflow
cd ~/agentflow && claude
```

Then ask Claude Code to install agentflow:

> Read install/SKILL.md and install agentflow on this machine. Interview me for anything you need.

The install skill interviews you, writes the config, builds the suite venv and deploys the skills. Then it guides you while you launch the fleet.

**The second thing to run is the doctor:**

```
python -m agentflow.doctor --project <name>
```

The doctor prints everything that agentflow resolves, in one run: config, paths, project, project repo, registry, the document globs, autodoc and the vault. (The vault is an optional notes directory outside the agentflow repo.) It prints one line for each check and a verdict at the end. [`docs/INSTALL.md`](docs/INSTALL.md) §9 shows that output with notes. Three of its lines say "not configured", and that is correct. A *correct* fresh install also shows the spec and plan globs as `info`, not as a failure, because their directories hold nothing until you write a spec.

If you have no terminal experience, read [**`docs/INSTALL.md`**](docs/INSTALL.md). It gives the same install for that reader. [`install/SKILL.md`](install/SKILL.md) and [`install/manifest.md`](install/manifest.md) give the mechanics: the typed config schema, the twelve steps of `provision.py` and the deployment policy.

---

## The fleet

Seven loops coordinate through one file per project: the **registry**. The diagram below labels it "Fleet Registry". The human launches each loop once with a slash command. After that, each loop except dev-manager is self-waking: it schedules its own next wake. Only **dev-manager** asks the human a question.

Every loop reads the whole registry at each wake and writes only the rows that it owns. It writes them through the registry CLI (`python -m agentflow.registry`). The registry CLI takes the edit-lock and refuses any edit outside those rows. The registry CLI enforces these rules; the filesystem does not.

[`docs/REGISTRY.md`](docs/REGISTRY.md) describes the edit-lock, the ownership rules, the schema gate that every loop runs before it acts, and what that enforcement is worth. See its sections *The write surface is fully mediated*, *The honest ceiling* and *The firing contract*. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) gives the same mechanism in one paragraph.

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

- **Questions → dev-manager → human.** Any loop can append a `## Questions` row. Each question is `halted`/`waiting`/`advisory`, and it always has a `default:`. At every wake, the steward answers the questions that delegated authority covers. Each time dev-manager runs, it answers each delegated question that is still open, and it asks the human the rest.
- **Blockers route by ownership:** junior blockers go to lead-dev, and auditor blockers go to dev-manager. By definition, a loop can resolve every blocker. A blocker is never a human decision.
- **One human surface: the dev-manager session.** The human launches each of the other six loops once and never opens it again. A merge has two routes. (1) In the dev-manager session, the promotion of a spec to `approved-for-autonomous` *is* the standing approval for the epic's whole lifecycle: build, open a PR, and **merge that PR to the project repo's base branch** as a squash merge. No human reads the PR. `skills/orchestrator/SKILL.md`, Step 9.2 §7c, skips the merge question for that state. (2) An issue labelled `triaged` and not `epic` is built and merged the same way, with no spec. The `triaged` label is its approval.

  No later step asks for a confirmation of each epic. Two safeguards remain: a one-way Ntfy notification, and a row in `## Directives`. `## Directives` is an **abort window, not a review step**. Every loop re-reads it immediately before every irreversible action. So a directive can stop a merge, but it cannot reverse one ([`docs/REGISTRY.md`](docs/REGISTRY.md), *The firing contract*). **Security & blast radius**, below, says what the two merge routes are worth.

### The seven loops

- **`lead-dev` — the builder that selects the work.** It selects promoted epics and triaged issues. It proves that two Queue rows touch disjoint files and seams. (A seam is the area of the code that a Queue row changes.) It publishes the other Queue rows into the lanes for the juniors, and it builds the `lead` lane itself. lead-dev is the only loop that selects the next Queue row. The selection rule is code (`agentflow.epic_survey.selectable()`), not prose that lead-dev derives again at each wake.
- **`jr-dev-1` — the first junior.** It claims only the Queue rows that lead-dev published into its own lane, and it never selects its own work. It is off by default: it builds nothing until `parallel_builders` opens its lane.
- **`jr-dev-2` — the second junior.** It has the same contract as jr-dev-1, on the second lane. It needs `count >= 2`.
- **`jr-dev-3` — the interim third junior.** The code labels it interim. It is a hand-copy of `jr-dev-2` on a third junior lane, added as a stopgap, and its start gate requires `count == 3`. The `lead-dev-as-foreman-and-n-junior-lanes` epic replaces the hardcoded enum with a count-driven set and retires jr-dev-3 (`skills/_shared/agentflow/registry.py`, the *Vocabulary* block). jr-dev-3 is in `AGENTS`, `BUILDERS`, `LANES`, `commands/` and the gates. This page names it because the gates first missed it when the docs stopped at six loops.
- **`auditor` — post-build intent feedback.** It reads the last N *done* epics. For each one, it asks whether the epic met its **intent** (its use cases, not its task list). Where the project's `verification.strategy` allows it, the auditor verifies live. It is read-only on the project repo, and it files drafted gap specs for the human to promote. At every wake, it also examines each request in each open issue that a merged change claims to fix, and it closes the fully fixed issues. The loop that built a fix never closes that issue.
- **`steward` — maintenance and registry repair.** It sweeps for what *time* breaks, not for what work creates: spec frontmatter that is behind the merged changes, stale lane claims, an orphaned deploy-lock, deployed-vs-source drift, schema-vs-code drift, backup retention and closure debt. Closure debt is open issues that a merged change claims to fix; the steward reports them and closes none. It holds the four registry-repair verbs, `reap`, `stand-down`, `repair` and `cull`. The registry CLI refuses these verbs to any other actor. The steward runs correctly with no active builder.
- **`dev-manager` — the human's session, and the only one.** It answers the fleet's questions and promotes specs from `drafted` to `approved-for-autonomous`. It triages issues on the git host, and it owns the blockers that the auditor sends. **It has no unattended mode: it does not self-wake and schedules nothing.** (E-0220 moved the sweep and the cull to the steward.) So dev-manager exists only while the human has the session open. The steward works between the human's sessions, and anything that needs a decision waits in `## Questions`.

All *coordination* between the seven loops is registry rows. The only per-loop files are the private state files (`status/<agent>-state.md`), plus the auditor's `audit-ledger.md` and `audits/`. Each of these files has a single writer: the loop that owns it.

### The four pillars

Each project's directory, `<data_root>/projects/<name>/`, holds four pillars. The pillars are separated by **who writes them**, not by what they contain. This rule keeps the pillars apart as they grow.

| Pillar | What it holds | Who writes it | Created by `install/provision.py`? |
|---|---|---|---|
| `design/` | what we **intend** to build: specs, plans, use cases and suggestions | the human | yes |
| `status/` | what the fleet is **doing**: the registry and the status boards | the loops | yes |
| `wiki/` | what we **learned**: durable pages | any session | yes |
| `autodoc/` | what the code **is**: the corpus | generated from the code | **no — see below** |

A file whose writer does not match its pillar is in the wrong pillar. This test is sharper than a question about what a file is *about*. For example, a postmortem and a status board can both be "about" an epic, but they belong to different pillars.

#### What mirrors to the vault

The vault is an optional notes directory outside this repository. Today, only `design/` specs and plans mirror to it:

| Pillar | Mirrors to the vault? |
|---|---|
| `design/` | Specs and plans only. |
| `status/` | **Never.** |
| `wiki/` | No. |
| `autodoc/` | **Not yet.** |

The mirror helper is the script that `vault.mirror_helper` names. Only three skills call it: `backlog` and `project` give it a spec, and `solution` gives it a plan.

**`status/` is lock-protected coordination and never mirrors.** A sync client copies a file each time it sees the bytes change. It cannot see the `mkdir` edit-lock that the registry CLI takes for each write, so it can race that lock. The result can be a half-written registry, which every loop then reads as the truth. No skill gives the mirror helper a `status/` path. The mirror helper ships with the vault, not with this repository, so this repository does not check whether it refuses one.

**`autodoc/` is not mirrored yet**, so nothing tracks a built corpus. You can render its pages again from the graph store (`<autodoc_root>/.source/graph.db`, [`/autodoc-init` Step 4](skills/autodoc-init/SKILL.md#step-4--write-records-to-the-store-then-render-deterministic)). But the graph store is in the same directory as the pages, so if you delete that directory, you lose both. [Step 7](skills/autodoc-init/SKILL.md#step-7--commit-the-autodoc-retired) tells why nothing commits the corpus.

#### Where the autodoc corpus lives

The corpus is what `/autodoc-init` builds from the code: the rendered pages, the graph store and `.manifest.json`. It lives in one directory, the **autodoc root**.

`install/provision.py` does not create `autodoc/`. It creates eight directories for each project: `design/{specs,plans,use-cases,suggestions}`, `status` and `wiki/pages{,/done,/archived}`. `/autodoc-init` also does not create `autodoc/` on a fresh install. It exits unless an autodoc root already resolves for the project.

`resolve_autodoc_root` resolves each project to one autodoc root. It tries three roots, in this order:

| Order | Root | Where it is set | When the project uses it |
|---|---|---|---|
| 1 | project override | `autodoc_root` in `projects/<name>.md` | when the project profile sets it |
| 2 | environment default | `environment.autodoc_root` in `environment.md` | when the project has no project override, unless the `.manifest.json` there names a different project |
| 3 | derived root | `<data_root>/projects/<name>/autodoc/` | only when that directory already exists |

If none of the three applies, the project has no autodoc root. `doctor`, the use-case touchpoint tools, `/autodoc-init`, `/autodoc-update` and `graph-query` all use this order. So a corpus is built and refreshed at the same root. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#four-pillars-separated-by-authorship) also gives the order.

**The environment default is one directory for the whole machine.** Every project with no project override resolves to it. After a corpus is built there, its `.manifest.json` names the project that it documents. Every other project then skips that directory and resolves to its derived root, or to no autodoc root. Before the first build, no manifest names a project, so `doctor` cannot tell which project the directory is for. For each project that resolves to it, `doctor` checks that the root resolves, whether a corpus is built there (`info` before the build, `ok` after) and whether the autodoc runtimes are installed (`warn` if not).

A project override has priority even when a corpus is already built at the environment default. While the override names an empty directory, `resolve_autodoc_root` refuses both paths. To fix this, move the corpus to the override directory, or clear the environment default.

**To build a corpus:**

1. Create a directory for the corpus. Nothing creates it for you, and the config loader refuses a path that does not exist.
2. Set `autodoc_root` in your `environment.md` to that directory.
3. If the machine does not have Node LTS (with npm), install it.
4. From the agentflow repo, run `python install/provision.py --autodoc` again. If you declined the auto-allow hooks, add `--no-auto-allow`. Without that flag, the run writes the hooks again ([`docs/INSTALL.md`](docs/INSTALL.md) §13).
5. Run `/autodoc-init`.

Step 4 is necessary if the install had autodoc off, which is the default. In that case, `provision.py` skipped its step 6 (`npm ci`, then the `autodoc_graph` install into the suite venv). Without it, `/autodoc-init` stops at its Step 0 gate, and `doctor` reports the gap as a `warn` that names the same remedy. Both autodoc skills also need the `Workflow` session tool for the phase that maps the code. That phase has no single-subagent fallback. On a machine with more than one project, both skills require the project as `AUTODOC_PROJECT=<name>` and refuse to start without it.

**Glossary** — the terms that the rest of this page uses:

| Term | What it means here |
|---|---|
| **`ScheduleWakeup`** | Claude Code's built-in tool for a timed self-wake. It is why a loop is *self-waking* and not started by cron. A loop schedules its next wake with one call: `ScheduleWakeup(delaySeconds=…, reason=…, prompt="/lead-dev --project <name> <target>")` ([`skills/lead-dev/SKILL.md`](skills/lead-dev/SKILL.md) §0). The `prompt=` starts the same loop again on the next wake. So it carries the resolved project and target as literal text. It never carries the `$ARGUMENTS` token, because that token does not expand on a self-wake. Before you launch a loop, make sure that the tool is in your session's tool list. `python -m agentflow.preflight --tools "..."` does this check and prints the remedy. Without the tool, a loop runs once and never wakes again, and it looks the same as a loop that finished. `install/manifest.md` declares this tool together with `Workflow`, which wave execution and the two autodoc skills need for the same reason. |
| **`CronCreate`** | Claude Code's session-scoped cron tool. Every self-waking loop uses it for one thing only: the **rate-limit recovery guard**, a second way to wake the loop beside the hourly `ScheduleWakeup`. A quota or rate-limit window (the account's, or one model's) can refuse a wake before its first tool call. That wake never reaches its own `ScheduleWakeup`, so the loop cannot recover from inside itself. The guard is session-scoped and in memory. It stops when the session stops, expires after 7 days, and reports nothing during an outage. So it is **recommended, not required**: a session without it loses unattended recovery and nothing else. It is not a daemon, and it does not count against the limit of one daemon. |
| **wake** | One scheduled start of one loop. `docs/REGISTRY.md` calls a wake a *firing*. A *state-bearing* wake touches the registry, and it starts with a read of the whole registry, as the firing contract in `docs/REGISTRY.md` specifies. |
| **epic** | One spec-sized item of work: a spec in `design/specs/` (what to build) and a plan in `design/plans/` (how to build it), with the same slug. The registry tracks it as one `E-NNNN` row in its `## Queue`. |
| **lane** | One column of Queue rows that a builder can claim: `lead`, `jr-dev-1`, `jr-dev-2`, `jr-dev-3`. lead-dev publishes Queue rows into lanes, and it checks at that time that the rows do not share files or seams. A junior cannot select its own work. |
| **`approved-for-autonomous`** | The `current_state` value in a spec's frontmatter that makes its epic selectable. Only dev-manager sets it, when it promotes the spec from `drafted`. |
| **Ntfy** | Push notifications through [ntfy](https://ntfy.sh). Ntfy is optional: `notify.kind: none` disables it. Notifications go one way for the whole fleet: a notification never expects a reply. |
| **`parallel_builders`** | The `environment.parallel_builders` block of the multi-builder module. It has two fields. `enabled` is the on/off switch (default `false`). `count` is how many **juniors** run (default `1`). `count` must be `0`, `1`, `2` or `3`, and the config loader rejects any `count` above `1` unless `enabled: true` ([`skills/_shared/agentflow/config.py`](skills/_shared/agentflow/config.py), the `parallel_builders` block). By count: `0` — `lead-dev` runs alone; `1` — `jr-dev-1` is active; `2` — `jr-dev-1` + `jr-dev-2`; `3` — `jr-dev-3`, the interim third junior, is also active. Each junior applies this rule as its start gate when it wakes ([`skills/jr-dev-1/SKILL.md`](skills/jr-dev-1/SKILL.md), *Module gate*). The SessionStart hook injects both fields as `[active builders] enabled: <bool>, count: <n>`. |

[`docs/REGISTRY.md`](docs/REGISTRY.md) is the authority for the registry schema, *The firing contract* and the *Question lifecycle*.

---

## Security & blast radius

Read this before you install. The fleet runs unattended by design.

- **Six of the seven loops run with permission prompts off.** The human starts every loop except `dev-manager` with `claude --dangerously-skip-permissions` ([`install/SKILL.md`](install/SKILL.md), *Launch the fleet*). These loops can run anything that the account can run. This is deliberate. If a self-waking loop gets an approval prompt while nobody watches, it does not *fail*. It **hangs**, and a hung loop looks the same as a working loop until a human examines it. The containment is the machine.
- **Two routes give the fleet approval to merge, not only to build.** (a) The promotion of one spec to `approved-for-autonomous` is a standing approval for the whole lifecycle of its epic: build → open a PR → `gh pr merge --squash --delete-branch` to the project repo's base branch → whatever the git host runs on a push to that branch. (b) An issue on your tracker labelled `triaged` and not `epic` is built and merged the same way, with no spec and no promotion. The `triaged` label is the approval for this route (`agentflow.issue_intake.builder_selectable`). The triage-ownership rule controls who can apply it: `dev-manager`, and the `steward` for bounded bugs. The loop that raised the issue never applies it. On both routes, no human reads the PR. For an epic, see `skills/orchestrator/SKILL.md` Step 9.2 §7c, *"skip the merge AskUserQuestion entirely"*. For a triaged issue, see `skills/lead-dev/SKILL.md` §4, where the deploy-lock chains the `gh pr merge --squash --delete-branch`. A merge to the base branch **can start the project's staging deploy** (`staging.deploy_recipe`) with no human. Two actions deliberately do **not** run automatically. A **production deploy** is always a human step (`skills/lead-dev/SKILL.md`, *Autonomy envelope*). **The public cut is always a human action**: no loop pushes to the public repository, opens a PR against it, or grounds against it ([`docs/runbooks/publish.md`](docs/runbooks/publish.md)).
- **The posture that this design expects.** The human approves *intent*, at the spec or with the `triaged` label. The loops execute the work unattended, on a contained machine. Code review is AI dual review (orchestrator Step 9.1). No human reviews the code before the merge. Production deploys and the public cut stay human actions.
- **`## Directives` is your abort window, not a review queue.** Every loop re-reads it immediately before an irreversible action. So a directive that arrives late can stop a merge, but it cannot reverse one.
- **The loops read text they did not write.** Registry rows that other loops wrote enter a loop's context as input. So do issue titles and bodies that `gh` pulls from the git host (`agentflow.issue_intake` requests `number,title,labels,body,url`). autodoc fences that data explicitly: `skills/graph-query/SKILL.md`, `autodoc_query/scorer.py` and `references/prompts/parent-report.js` all carry an *"UNTRUSTED DATA … never instructions"* clause. **The builders carry no equivalent clause today.** Assume that anyone who can file an issue on the project repo can put text into the context of a builder.
- **Registry roles are declared, not proven.** The registry CLI checks each write against the `--actor` that it is passed. This stops a loop that writes outside its role by mistake. But any shell on the machine can pass any actor, including `dev-manager`'s `--by human` answer ([`docs/REGISTRY.md`](docs/REGISTRY.md), *Command surface*).
- **So, run the fleet on a machine that you could wipe**, under an account whose stored credentials reach no further than the one project that the fleet works on. If this machine does not meet that condition, run without the skip-permissions flag, do not run the autonomous loops, and use `/dev-manager` yourself. Answering its questions is a valid way to use agentflow.

To report a vulnerability, or a way past these limits that goes further than this section describes, see [`SECURITY.md`](SECURITY.md). Report it privately, not in a public issue.

[`docs/INSTALL.md`](docs/INSTALL.md) §10 and [`install/SKILL.md`](install/SKILL.md), under *Launch the fleet*, give the same trade-off in plain English, in the order that you meet it during the install.

---

## Quickstart

**Requirements**

| For | You need |
|---|---|
| The pipeline (refine → solution → orchestrator, the fleet) | Claude Code · Python 3.11+ · Git |
| Create and merge PRs, and track issues, on a git host | `gh` (optional). It is the only verified CLI. The issue paths refuse `glab` before it runs. The PR paths ignore `glab`, because they hardcode `gh`. `tea` is not a valid setting (`gh#9`). |
| The optional autodoc subsystem | Node LTS · [Ollama](https://ollama.com) for the embedding and naming passes. Embedding is opt-in (`graph build --embed`), so a graph builds without Ollama. Nothing in `install/` checks for Ollama. |

The fleet **runs as Claude Code skills**, not as a standalone service. There is no server to host. The only process outside your sessions is one optional watchdog on your OS scheduler, which only reports ([`docs/INSTALL.md`](docs/INSTALL.md)). The install skill deploys the skills into your Claude config. Then the seven loop slash commands (`/lead-dev`, `/jr-dev-1`, `/jr-dev-2`, `/jr-dev-3`, `/auditor`, `/steward`, `/dev-manager`) resolve in a new session. So do the six other slash commands (`/report`, `/graph-query`, `/wiki-save`, `/autodoc-init`, `/autodoc-update`, `/uc-touchpoints`).

`install/provision.py` deploys **three** kinds of thing into your Claude config: every directory under `skills/` (except `_shared`, which is the pip package), the `commands/*.md` wrappers, and one hook script. The hook script is `skills/_shared/scripts/plan-lint.sh`. `provision.py` deploys it to the stable path `<claude_root>/scripts/plan-lint.sh`, so the copy on the machine is never unmanaged. The code for this list is `_deploy_sources` in [`install/provision.py`](install/provision.py). Its docstring also lists the three kinds, because a deploy function that under-reports what it deploys leaves unmanaged files on the machine. `provision.py` registers no hook for the hook script, so nothing runs it until you add a hook yourself.

The install skill and `provision.py` are in `install/` at the agentflow repo root. That directory is outside all three kinds, so nothing deploys it, and no `/install` slash command exists. You tell Claude Code to read `install/SKILL.md` directly.

**Install and first use are one prompt.** Clone the agentflow repo (`git clone https://github.com/abrooks89/agentflow.git ~/agentflow`). Then start Claude Code in it:

```
cd ~/agentflow && claude
```

Then say:

> Read install/SKILL.md and install agentflow on this machine. Interview me for anything you need.

The install skill is the single hands-on guide. It interviews you about the environment and the first project, and it runs `install/provision.py`. It deploys the fleet skills and hooks, seeds the registries, and makes the suite venv the default python of the machine. Then it **shows you how to launch and operate the seven loops**. If you are new to agentflow, [**`docs/INSTALL.md`**](docs/INSTALL.md) explains the same install step by step, in plain English. The full runbook is [`install/SKILL.md`](install/SKILL.md), and the mechanics are in [`install/manifest.md`](install/manifest.md).

**What install changes on your machine**

- A suite venv at `<suite_root>/.venv`, with PyYAML and an editable install of the `agentflow` package (`provision.py` steps 4–5).
- A line in your shell profile that activates that venv: `source .venv/bin/activate`, or the equivalent in the PowerShell `$PROFILE`. **This line displaces whatever `python` the machine had** (`install/SKILL.md` Step 7).
- The fleet skills, the slash commands and the one hook script, deployed under your Claude config directory (`provision.py` step 7). The defaults are `~/.claude/skills/`, `~/.claude/commands/` and `~/.claude/scripts/`. On Unix, they are symlinks. On Windows, the skill *directories* are junctions and the individual command and script *files* are copies. `<claude_root>/.agentflow-deploy.json` tracks them.
- A **SessionStart** hook registered in your Claude settings file (`~/.claude/settings.local.json` by default). The hook injects the resolved config into each session (`provision.py` step 9).
- **PreToolUse auto-allow hooks written into that same settings file** (`provision.py` step 8, through [`skills/orchestrator/bootstrap.py`](skills/orchestrator/bootstrap.py)). These hooks return `allow` for the `Edit`, `Write` and `Agent` tools. They also return `allow` for a list of `Bash(…)` globs, for example `Bash(git *)`, `Bash(chmod *)`, `Bash(mv *)` and `Bash(curl -s *)`. `settings.local.json` is the *project* settings file for a session that starts in your home directory, not the user-global `settings.json`. So these hooks apply to every session that you start from your home directory. For the fleet, that is every session. Read `BASH_AUTO_ALLOW_PATTERNS` in `skills/orchestrator/bootstrap.py` before you accept these hooks. To install without them, decline the hooks when the install interview asks, or pass `--no-auto-allow` to `install/provision.py`. To remove hooks that an earlier install wrote, see [`docs/INSTALL.md`](docs/INSTALL.md) section 13.
- The project scaffold in your configured `data_root` (`provision.py` step 11). Each project gets eight directories (`design/{specs,plans,use-cases,suggestions}`, `status` and `wiki/pages{,/done,/archived}`), a seeded registry and the use-case spine. Nothing here creates `autodoc/`. `/autodoc-init` also does not create it, unless an `autodoc_root` already resolves for the project ([*The four pillars*](#the-four-pillars)).

On a **dedicated machine or user account**, the suite venv first on the session PATH is all you need. If the machine also builds Python project repos, set `canonical_repo.python` in the profile of each project. Then the builders run each project repo's test suite with its own interpreter, never with the suite venv. The first live run found this problem. `provision.py` hard-fails loudly when required config is absent: it never silently uses a default for a missing named env var or a path that it cannot resolve. `provision.py` installs Node tree-sitter **only** when autodoc is enabled.

**Before you launch a loop, check three tools that the install skill cannot provide.** `ScheduleWakeup`, `Workflow` and `CronCreate` come from the Claude Code session, not from agentflow. If one is absent, nothing tells you. Preflight compares a list that *you* supply with what the loop needs. A list that you type from this page does not prove that your session has the tools. So ask Claude for your session's actual tool list, and pass that list to preflight:

```
python -m agentflow.preflight --tools "<your session's tool list, comma-separated>" --wave
```

Preflight exits 1 and prints the remedy for each absent tool. `--wave` adds `Workflow` to the required tools. Wave execution needs `Workflow`, and so do `/autodoc-init` and `/autodoc-update`, whose mapping phase runs through it with **no** single-subagent fallback. Omit `--wave` only for a loop that will run no waves and no autodoc. Add `--no-self-wake` for a loop that is not self-waking. `CronCreate` is at a **recommended** level. Preflight reports it on every run, and prints `DEGRADED`, not `MISSING`, when it is absent. A session without it still runs, wakes and builds, and loses only the 5-hour rate-limit recovery guard that each loop schedules. The exit code never depends on `CronCreate`. [`docs/INSTALL.md`](docs/INSTALL.md) gives the full step, including what to do when a tool is absent.

---

## Components

### The pipeline skills — `refine` → `solution` → `orchestrator`
Work goes from a spec (`refine`) to a plan (`solution`, through the vendored `writing-plans`) to an execution engine (`orchestrator`). The orchestrator runs the plan as waves of parallel subagents, with per-task review, regression auto-fix and a status board. Supporting skills: `project`, `choose-next`, `backlog`, `tdd-loop`, `polish-mode`, `wiki-save`, `autodoc-init`, `autodoc-update`. The pipeline is **pure Python + PyYAML + Git**. It needs no Node, no web server and no database engine.

### The autodoc graph subsystem
`autodoc` turns a source repo into a corpus of persistent, cross-linked pages that document the code. Below the pages is a **two-tier knowledge graph**, which is its own installable package (`skills/autodoc-init/references/graph/`).

- **Exact symbol tier.** A Node tree-sitter pass extracts every symbol (functions, classes, routes) into deterministic JSONL. These symbols are `tier='symbol'`, `origin='ast'`. They are exact, rebuilt from nothing on each run, and a pure function of the code.
- **LLM concept tier.** Subagents write architectural concepts (`tier='concept'`, `origin='agent'`). They join the concepts to the code through **`documents`** edges, which are the tier join. The relation vocabulary is **closed at 8 relations**, because an open vocabulary makes traversal unqueryable. A new relation is a schema change, not a subagent decision.
- **Derived communities replace hand-written area maps.** NetworkX **Louvain** runs over the AST edges and divides the graph into communities. Every symbol goes into exactly one community, or the run hard-fails and names the orphan. The old hand-maintained "areas" map is gone. The map is now *derived* from the code.
- **Deterministic renderer.** The graph store is primary. One renderer is the *only* writer of the markdown pages. Two renders over an unchanged graph store are byte-identical. So link integrity and frontmatter completeness are true by construction, and need no audit after the fact.
- **Graph-query — light graph-RAG with provenance on every item.** A **deterministic router** sends each question to `local` or `global`, with no LLM round-trip in the default path. Retrieval crosses the tier join. Every returned item carries its full traversal path, with the `rel`/`origin`/`confidence` of each hop. An exact AST fact, a subagent assertion and a speculative "dreamed" link are always distinguishable. An item reached through a dreamed edge has the mark `[via hypothesized link]` in its own citation.

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

This is real output from the agentflow repo's own corpus, built at commit `fe37970`. It is trimmed, and `...` marks elided items. `fe37970` is a working-repository commit, and the public cut does not contain it. `--db` is the graph store that `/autodoc-init` builds at `<autodoc_root>/.source/graph.db`. `--corpus` is the repo stem that the corpus was built under. Without `--pretty`, the same result prints as JSON.

The backticked names in the question fire `rule=entity_resolution`. The router resolves them to symbols and seeds from them at score 1.00. The traversal then crosses the tier join and reaches the concept `epic_survey` (`origin=agent`, `min_conf=0.70`). Local mode also embeds the question (`--ollama-url`, default `$AUTODOC_OLLAMA_URL`). This corpus carries no embedding rows, so the named symbols are the only seeds. This capture ran with a stand-in embedder, so `embed_ms=0`.

The line spans were true at `fe37970`. They are the one part of a citation that goes out of date, so check the symbol, not the line number. [`autodoc_query/types.py`](skills/autodoc-init/references/graph/autodoc_query/types.py) defines the citation format `name (file:start-end)`. The caller's model writes the final prose; the graph gives it cited, ranked context.

The **adapter seam** (`skills/autodoc-init/references/graph/autodoc_graph/adapter.py`) is the fork point. Every consumer uses one abstract storage interface, and no SQL is above that interface. So a pgvector fork can replace the SQLite + `sqlite-vec` backend as a drop-in.

**To run more than one project, put the project name on the slash command.**

```
claude --dangerously-skip-permissions "/lead-dev --project agentflow"
```

Every loop takes `--project <name>`, and the name has priority over the session's injected default. Each self-waking loop passes the name to its next wake through `ScheduleWakeup`. (dev-manager has no next wake; it runs only while the human has its session open.) So the fleet stays on the project that you launched it on, and one machine can run several fleets.

**Do not export a shell variable.** Claude Code's settings `env` overrides the process environment. So `AGENTFLOW_PROJECT=other claude ...` has no effect and gives no warning. The session runs the *default* project, and that wrong-project run looks exactly like a correct run. This is why the argument goes on the prompt. A scripted launch does the same: it runs plain `claude` from the launch directory, with `--project <name>` on the slash command. Do not use a per-project `--settings` file. Its `env` block replaces the `env` block of `settings.local.json` instead of merging with it. Every collected env var disappears, and the loop fails at its first step. See [`install/SKILL.md`](install/SKILL.md).

---

## Evidence

**Test surface.** Every layer below runs on the full CI matrix: `ubuntu-latest` **and** `windows-latest`. The full matrix runs nightly when master has changed since the last green nightly run ([`nightly.yml`](.github/workflows/nightly.yml) calls [`ci.yml`](.github/workflows/ci.yml)), and on manual dispatch. A public repository runs the full matrix on every pull request and every push. A private copy runs a smaller set on a pull request, without most `windows-latest` jobs, and runs no job on a push to master. The *WHAT RUNS WHERE* comment at the top of `ci.yml` is the authority for these rules.

The right-hand column is the command that produced each number, so you can reproduce it. **Each count is a floor**: the command gives at least the printed number, on any platform. Each floor is the count rounded down to two significant figures, so a pull request that adds tests does not edit this table.

| Layer | Tests | Counted with |
|---|---:|---|
| autodoc graph + query | **480+** | `python -m pytest skills/autodoc-init/references/graph --collect-only -q` |
| tree-sitter inventory (Node) | **320+** | `( cd skills/autodoc-init/references/inventory && npm ci && node --test )` |
| orchestrator `wave_group.js` (Node) | **33+** | `node --test skills/orchestrator/tests/wave_group.test.mjs` |
| `_shared` (config, epic-survey, registry, plan parser, waves) | **6600+** | `python -m pytest skills/_shared --collect-only -q` |
| install / provision | **360+** | `python -m pytest install --collect-only -q` |
| acceptance harness | **44+ wired assertions** | `python harness/check.py --self-test` |

**The table pins no commit, on purpose.** The counts once had the stamp "measured against master `<sha>`", and both halves became stale. No step wrote a new sha on a merge, and the numbers became wrong four separate times under it. Now [`skills/_shared/tests/test_evidence_table.py`](skills/_shared/tests/test_evidence_table.py) derives each count again on every CI run. It parses these rows and runs each command from the repository root. It fails if a count falls below its floor, or rises past 1.25 times it (then the floor is stale). It also fails a cell that is not written as a two-significant-figure floor. Change a cell only when this gate fails. Write the new count rounded down to two significant figures, followed by `+` (5359 becomes `5300+`).

**What this gate checks, and what it does not check.** The gate runs every row whose command it can execute *in its current environment*. It **declares** each other row unrunnable, with a reason, and does not skip it. So a new unrunnable row makes the gate fail. The gate compares counts on every platform, Windows included. Three rows — `_shared`, install and harness — need only the suite's own interpreter and are checked everywhere; that is the gate's floor. The other three declare a prerequisite. The orchestrator row needs `node` and `bash`. The `shared-tests` CI job that hosts the gate installs Node for it, so the gate checks that row there. Under CI, the gate fails, and does not excuse the row, if either tool is missing. (CI's `orchestrator-node-tests` job also runs the same file on both platforms.) In `shared-tests`, two rows stay excused. The tree-sitter inventory row does a network `npm ci`, which the gate never runs. The autodoc-graph row has dependencies that a different job installs. On a machine that has those dependencies, such as a developer's clone, the gate checks the graph row too. CI's `inventory-tests` job runs the inventory tests on both platforms. That proves the tests pass, but it does not compare their count with the table. The inventory count is the only number here that no run of this gate can check, in any environment.

**Adversarial-review methodology.** Every epic follows the same sequence: **build → dual adversarial review → remediation → re-review → independent closure verification.** The reviews are hostile on purpose. A reviewer's append-attack becomes a committed regression test. Closure is verified against the tree, not against the report that claimed the fix (see *Scars*, below).

**Skills get property tests.** A skill is prose, and its interpreter can change while the skill text stays the same. So the [`harness/`](harness/README.md) records how the live skills behave (plan emission, a full build wave, a builder's next wake) and asserts properties of that recording. It keeps no baseline to compare with. To prove that a change kept the old behavior, record scenarios A, B and D again and run `check.py` again. A recording is evidence of one run, and the tracked fixtures are the inputs. `--self-test` and `--only C,F,G` run on any clone.

---

## Scars

Every mechanism below exists because something broke first. The design principle is **enforcement follows failure**.

> **Provenance.** The public repository is a **single-commit cut**. Its history starts at one
> commit that holds the whole tree, and the development history behind it is not public. That
> history is in two private repositories: the archived `agentflow-dev`, and after it the
> maintainers' working repository. The CHANGELOG's `(#N)` numbers refer to that working
> repository's pull requests. **No commit of either repository resolves in the public cut. The
> four shas cited below are `agentflow-dev` commits: `5d6b0cf`, `4287191`, `a25266b` and
> `024e8de`.** Each entry that cites one also names the artifact that the failure left in *this*
> tree, and that artifact resolves. The plain `gh#N` references in this tree also point at the
> working repository's tracker. In a public copy they are labels, and they do not resolve.

- **A template variable stopped a whole phase.** An externalized prompt hard-required a `{{kind}}` template variable that real callers never supplied. The strict engine stopped the whole Refresh phase, and runs quietly returned `communitiesRefreshed: 0`. → A **template-vars-vs-contextSpec drift gate** now asserts that each prompt's context spec declares every variable that the engine hard-requires. Run across all seven templates, it immediately found a second failure of the same type. *(agentflow-dev `5d6b0cf`; in this tree: the test `every template variable the engine hard-requires is declared by the contextSpec` in [`skills/autodoc-init/references/inventory/test/prompt-modules.test.js`](skills/autodoc-init/references/inventory/test/prompt-modules.test.js))*

- **A credit limit stopped subagents in the middle of a run.** Records that traveled back *through* subagent returns were lost when a subagent was cut off. → A **disk-based records contract**. Each subagent that produces records writes them to `<runDir>/records/<label>.json` and returns only a light manifest. Parents read their children's files directly. Page bodies never travel inline, and a 256KB `RETURN_BUDGET_BYTES` gate throws an error if bulk data leaks back through a return. Completed work survives when a sibling subagent is cut off. *(agentflow-dev `4287191`; in this tree: [`skills/autodoc-init/references/init-workflow.js`](skills/autodoc-init/references/init-workflow.js), `recordsPathFor` and `RETURN_BUDGET_BYTES`. The run that caused this change, where 5 of 24 subagents were cut off and the work of 19 stayed intact, is operating history.)*

- **Prose misjudged which Queue rows were ready to select.** When prose did the selection, the result drifted between runs. → Selection **moved into code**. `selectable()` in `agentflow.epic_survey` is the single, testable definition of which Queue rows may be selected. The skills call it, and do not derive the judgment again each time. They also no longer combine `ready_set()` and `dispatchable()` by hand. They did that until a call order, which the names did not make clear, silently stranded approved Queue rows. *(agentflow-dev `a25266b`; in this tree: [`skills/_shared/agentflow/epic_survey.py`](skills/_shared/agentflow/epic_survey.py))*

- **A report claimed a fix that was never committed.** A remediation used a heredoc patcher whose `str.replace` had no assert and silently never matched. The lazy gate stayed unchanged, but the report claimed that it was fixed. → **Reports are verified against git, not memory.** Every claimed fix is checked again against the committed tree with `git grep`. The drift gate now requires *exactly one* marked template-engine block per workflow file, and byte-compares it against `engine.js`. The reviewer's append-attack is a committed regression test. *(agentflow-dev `024e8de`; in this tree: the tests `workflow-inlined engines are byte-identical to engine.js` and `drift gate catches the reviewer append-attack (second drifted block)` in [`skills/autodoc-init/references/inventory/test/prompt-modules.test.js`](skills/autodoc-init/references/inventory/test/prompt-modules.test.js). A live revert check of the gate, which drove it red and then restored it to green, is operating history.)*

- **A partial batch stayed in the graph store after a failure.** A half-written batch left the graph store in a state that no one intended. → **Pre-validate-all + rollback + `close()` never commits.** The concept-tier record writer validates *every* record before the first write, so a bad record leaves the graph store unchanged. The storage adapter's `close()` deliberately never commits. Uncommitted state at close time is a failed batch, and it is discarded. *(in this tree: [`skills/autodoc-init/references/graph/autodoc_graph/records.py`](skills/autodoc-init/references/graph/autodoc_graph/records.py) and [`skills/autodoc-init/references/graph/autodoc_graph/adapter.py`](skills/autodoc-init/references/graph/autodoc_graph/adapter.py))*

---

## Manifesto

agentflow is a set of **programs whose interpreter is a language model**. Each program is a skill: a `SKILL.md` file in prose. A Claude Code session is the runtime that reads a skill and runs it. No compiler sits between the intent and the behavior, and you can read a skill in one session.

agentflow keeps its state in a **database whose pages are files**. Every spec, plan, Queue lane and registry row is markdown that a person can diff and edit by hand. The edit-lock, a `mkdir` lock, controls concurrent writes. Git history is the log for the code. Git does not manage the data root, so the data root keeps an append-only `registry-log.md` beside its status boards. The state is easy to examine by design. agentflow has at most one daemon: the optional watchdog, which runs on a schedule, reports and never acts.

agentflow's rules obey one law: **enforcement follows failure**. No gate, lock or contract in this repository started as an abstract design. Each one comes from a specific failure, and the *Scars* section above names them.

---

## More

- [`docs/INSTALL.md`](docs/INSTALL.md) — the step-by-step install guide in plain English, for a reader with no terminal experience.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the detailed architecture: prose-as-code, markdown-as-database, the graduation criterion, the registry and the autodoc graph.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — near-term work, mid-term work, and the work that we decided not to do.
- [`docs/REGISTRY.md`](docs/REGISTRY.md) · [`docs/DATA-LAYOUT.md`](docs/DATA-LAYOUT.md) — the registry and the single-root data layout.
- [`docs/runbooks/publish.md`](docs/runbooks/publish.md) — the working-repo / publish-repo split, and the human-only procedure that pushes the public cut. No loop runs it.
- [`skills/_shared/SCHEMA.md`](skills/_shared/SCHEMA.md) — the frozen typed-config schema that both config files are validated against.
- [`harness/README.md`](harness/README.md) — the acceptance harness: property tests for skills.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to run the test suites and get a pull request through the CI gates.
- [`SECURITY.md`](SECURITY.md) — how to report a vulnerability privately, and how agentflow handles secrets.
- [`ACKNOWLEDGEMENTS.md`](ACKNOWLEDGEMENTS.md) — vendored code, prior art and runtime dependencies.
- [`LICENSE`](LICENSE) — MIT.

*Provenance note: agentflow's mechanisms were hardened while operating a production multi-tenant platform. That platform is a separate codebase, neither this repo nor `agentflow-dev`, and its own repository merged 430+ PRs in June–July 2026. You can check claims about this repository in this tree. Claims about that operating history are labeled as such.*
