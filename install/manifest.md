# agentflow multi-runtime manifest

The declarative runtime contract the installer provisions against. `install/provision.py`
keeps a structured step list in agreement with this file; `install/SKILL.md` runs the
human-facing interview. This is the source of truth for **what runtimes agentflow needs**.

The guiding rule of the Phase-1 port: **the pipeline is pure Python + PyYAML.** Everything
heavier (Node, tree-sitter) belongs to an *optional, delegated* feature (autodoc) and is
never pulled in for a plain refine → solution → dispatch run.

## Runtimes

| Runtime | Version | Scope | Why | Required? |
| --- | --- | --- | --- | --- |
| **Python** | 3.11+ | pipeline | `datetime.UTC`, the resolver, every `agentflow.*` module | **Required** |
| **PyYAML** | `>=6.0` | pipeline | the ONLY third-party dep — parses the typed config YAML block | **Required** |
| **Git** | any recent | pipeline | worktrees, branch/merge, the whole SDLC loop | **Required** |
| **`gh`** | any | pipeline | Issue tracking, and PR create/merge — which hardcode `gh` rather than reading `git_host.cli` at all. The **only** verified `git_host.cli`: on the issue paths `issue_intake._require_verified_tracker` refuses `glab` before spawning, and config validation rejects `tea` outright (`gh#9`) | **Optional** |
| **Node + npm** | LTS | **autodoc only** | the tree-sitter code-inventory package for `/autodoc-update` | **Optional (autodoc only)** |
| **sqlite-vec** | `>=0.1.9,<0.2` | **autodoc only** | vector search in the autodoc graph backend; arrives automatically via `pip install -e` of the `autodoc_graph` package (step 6) | **Optional (autodoc only)** |
| **networkx** | `>=3,<4` | **autodoc only** | Louvain community detection over the symbol graph; arrives automatically via the same `pip install -e` (step 6) | **Optional (autodoc only)** |
| **[Ollama](https://ollama.com)** | any, serving `nomic-embed-text` | **autodoc only** | the embedding pass (`graph build --embed`) and the community-naming pass. Embedding is **opt-in**, so a graph builds and renders without it — what you lose is vector search, and with it `graph-query`'s `local` path. URL resolution is `--ollama-url` > `AUTODOC_OLLAMA_URL` > `http://localhost:11434` | **Optional (autodoc only)** — and **not provisioned or checked**: `_do_preflight` verifies `node` and `npm` under autodoc, never this. An unreachable Ollama surfaces as a loud `EmbedderError` naming the URL at the moment you embed |

## Harness tools

Not runtimes and not provisioned by anything here - these come from the **Claude Code session**
that runs a loop. They are listed because this file is where a reader looks for what agentflow
needs, and their absence fails in the worst available shape: a clean install that dies somewhere
else, with no error.

| Tool | Scope | Why | Required? |
| --- | --- | --- | --- |
| **`ScheduleWakeup`** | every self-waking loop | a loop's continuation IS this call. Without it the loop runs exactly once and never wakes again - indistinguishable from a loop that finished | **Required** (six of the seven loops; dev-manager stopped self-waking in E-0220) |
| **`Workflow`** | wave execution; `/autodoc-init` and `/autodoc-update` | the wave engine dispatches through it, and so does the mapping phase of both autodoc skills. Without it a build dies inside the orchestrator rather than at launch; the autodoc skills stop at their Step 0 guard | **Required** for wave execution and for both autodoc skills - the autodoc mapping has **no** single-agent fallback, and a sequential crawl is forbidden. Not used by the ordinary single-agent build path |
| **`CronCreate`** | every self-waking loop's rate-limit recovery guard | each loop schedules a standing 5-hour guard with it, so a lane refused by a quota or rate-limit window (the account's, or one model's) resumes itself once the clock passes - a firing refused before its first tool call never reaches its own `ScheduleWakeup`, so the loop cannot recover from inside itself | **Recommended, not Required** - a session without it runs, wakes and builds exactly as before and loses only unattended recovery, so a rate-limit outage then ends when a human relaunches. `preflight` reports it `DEGRADED` and still exits 0 |

**Whether these ship to every Claude Code user is not settled here, in either direction.** What
is certain is the asymmetry: declaring a prerequisite that turns out to be universal costs one
line, and omitting one that is not costs a silent dead install. Check them with

```
python -m agentflow.preflight --tools "<your session's tool list, comma-separated>" --wave
```

which exits 1 and prints the remedy for anything missing. `--wave` is there because without it
`Workflow` is never checked; drop it only for a seat that runs no waves and no autodoc skill. `docs/INSTALL.md` carries
the preflight step; `skills/_shared/agentflow/preflight.py` is the check itself and is honest
about its own limit - it cannot see a session's tool list, so it has to be told.

**The two tiers are not the same word twice.** `MISSING` means this seat cannot do its job and
the check exits 1. `DEGRADED` means it can, minus one thing the row names - today only
`CronCreate`, minus unattended rate-limit recovery - and the check still exits 0. A single tier
would have forced a choice between refusing a session that works and saying nothing at all about
a tool six SKILL.md files now instruct a call to. Neither of those is a thing a reader of this
file can act on.

**None of the three is a daemon, and `CronCreate` in particular is not one.** Its jobs live in
the Claude Code session's own memory, fire only while that session is idle, and are gone when the
session exits. The suite's single-daemon budget (2026-09-22) is spent on the scheduled
`wake_watch` invocation in step 12 (registered only with `--wake-watch`) and nothing here adds
to it.

## Explicitly NOT required

- **No Node for the pipeline.** Node appears only under autodoc; a machine that never
  runs autodoc never installs it. `verification.strategy == web-journey` uses the
  harness-provided Playwright, not a project-local Node runtime.
- **No `fastapi` / `uvicorn`.** An earlier HTTP work-discovery service is cut; work
  discovery is the filesystem epic-survey. No web server ships.
- **No `markdown` library.** Config is a fenced YAML block read by PyYAML; the surrounding
  prose is human-only and never parsed.
- **No `tree-sitter` for the pipeline.** It is autodoc's inventory dependency exclusively.
- **No `sqlite-vec` or `networkx` for the pipeline.** Both belong to the autodoc graph
  package (`skills/autodoc-init/references/graph`, its own installable `autodoc_graph`
  distribution with its own pyproject) and land in the venv only when autodoc is
  enabled. `skills/_shared` stays PyYAML-only — that pledge is load-bearing.

## Provisioning order

The installer provisions in this fixed order (see `provision.py --dry-run` for the live plan):

1. Preflight — verify the runtimes above are present (Node + npm checked only when autodoc is on).
2. Config presence — `~/.agentflow/config/environment.md` exists and resolves.
3. Secrets — true env vars written ONE-WAY into `settings.local.json` / `.env` (never markdown).
4. Python venv `<suite_root>/.venv` + `pip install pyyaml>=6.0`.
5. `pip install -e skills/_shared` (the `agentflow` package — kills the old `sys.path` hacks).
6. Autodoc runtimes — `npm ci` in the tree-sitter package, then `pip install -e`
   of the `autodoc_graph` package into the suite venv. **Skipped unless autodoc is enabled.**
   Enabling autodoc after install (setting `autodoc_root` later) does not run it retroactively:
   re-run `python install/provision.py --autodoc`, with Node installed, before `/autodoc-init` -
   adding `--no-auto-allow` if the auto-allow hooks were declined, or step 8 writes them back.
7. Deploy skills + commands + one hook script into the Claude config dir (see **Skill deployment** below).
8. `bootstrap.py` once — installs the PreToolUse auto-allow hooks (honors `CLAUDE_SETTINGS_FILE`).
   **Skipped with `--no-auto-allow`**, which removes nothing an earlier run wrote (docs/INSTALL.md section 13).
9. Install the `SessionStart` config-injection hook.
10. Presence-check delegated SKILL.md paths + hard-fail on missing required config.
11. Scaffold the `data_root` subtree (per-project dirs + use-case seeds; idempotent).
12. Register the missed-wake watchdog with the OS scheduler — **opt-in with `--wake-watch`,
    off by default** (gh#544; a default run SKIPS it and removes nothing an earlier run
    registered). Opted in: one hourly, report-only
    `python -m agentflow.wake_watch --project <name>` entry per project. **Windows only**
    (`schtasks /Create /F`, idempotent by name); elsewhere it SKIPS and prints the exact
    crontab line, because a provisioner must not rewrite a crontab holding the operator's
    own jobs. **This is the suite's ONE daemon** (single-daemon rule, 2026-09-22) — it
    reports and never relaunches a lane, and that budget is now spent.

## Project selection is NOT provisioned

The installer never chooses the active project for a session, and no step above writes one.
Specifically, `provision.py` writes **no** `AGENTFLOW_PROJECT` into the settings `env` block
(step 3 writes only the *secret* env vars the config declares by name) and creates **no**
`<config_home>/settings-<name>.json` file. Its only settings-file writes are hook
registrations (steps 8–9).

Project selection is a **launch-time argument**: `claude "/lead-dev --project <name>"`. The
name rides on the slash command, beats the SessionStart hook's injected default, and is
carried into each loop's `ScheduleWakeup` continuation. A scripted launch uses the same
slash-command argument; do not script it with `--settings`, whose `env` block replaces
`settings.local.json`'s instead of merging with it — see `SKILL.md`, *Add another project later*.

Why not an env var: Claude Code's settings `env` **overrides the process environment**, so a
shell-exported `AGENTFLOW_PROJECT` is silently discarded and the session resolves the default.
A provisioning step that wrote the variable would therefore be defeated by the same override
on every box that already has one — the argument is the only channel nothing shadows.

## Skill deployment (step 7)

Step 7 makes the fleet **launchable**: after install, every `commands/*.md` wrapper resolves in a
fresh Claude Code session — the seven loops (`/lead-dev`, `/jr-dev-1`, `/jr-dev-2`, `/jr-dev-3`,
`/auditor`, `/steward`, `/dev-manager`) and the six supporting commands (`/report`,
`/graph-query`, `/wiki-save`, `/autodoc-init`, `/autodoc-update`, `/uc-touchpoints`).

**What deploys** (target root: `--claude-root`, default `$CLAUDE_CONFIG_DIR` or `~/.claude`).
**Three kinds of thing ship, not two** — `_deploy_sources` says so in its own docstring, and this
table is the other place that has to:

| Source | Target | Deployed? |
| --- | --- | --- |
| `skills/<name>/` (every dir) | `<claude_root>/skills/<name>/` | Yes |
| `commands/*.md` | `<claude_root>/commands/` | Yes |
| `skills/_vendored/` | `<claude_root>/skills/_vendored/` | **Yes** — see below |
| `skills/_shared/scripts/plan-lint.sh` | `<claude_root>/scripts/plan-lint.sh` | **Yes** — the third kind; see below |
| `skills/_shared/` (the package) | — | **No** — pip package |
| loose files under `skills/` (e.g. a stray `NOTES.md`) | — | No — repo-side docs, not skills |

**Why one file out of `_shared` deploys anyway:** the script gets a stable, managed host path,
`~/.claude/scripts/plan-lint.sh`, so the host copy is never an unowned file. Provision registers
no hook for it; nothing runs it unless the operator adds one. The
`_shared` directory is excluded from the skills sweep, so without an explicit entry the script has
no deploy path at all and the host copy drifts unowned — which is exactly what happened
(`gh#110`). The entry is guarded, so a suite
checkout without the script still provisions.

**Why `_vendored` deploys:** first-party skills reference the vendored superpowers by
**relative sibling path** — `../_vendored/brainstorming/SKILL.md` from `skills/<name>/SKILL.md`,
`../../_vendored/...` from `skills/orchestrator/prompts/*` (grounded via
`git grep -n '_vendored' skills/ commands/`; zero suite-root-absolute or by-name invocations).
Post-deploy those relative paths resolve under `<claude_root>/skills/_vendored/`, so `_vendored`
must land there as a sibling or every cross-reference dangles.

**Why the `_shared` DIRECTORY does not deploy:** it is the `agentflow` pip package, consumed via
`pip install -e` (step 5). No skill prose requires it under `~/.claude` — every `_shared`
path reference points into the suite checkout, never under `<claude_root>` (e.g. the
`<suite_root>/skills/_shared/SCHEMA.md` pointer in each loop's SKILL.md), and a command that
runs package code reaches it by module name, never by path (`commands/uc-touchpoints.md` runs
`python -m agentflow.use_case_impact`). The `plan-lint.sh` row above is the
exception and is deployed as an individual file, not as part of that directory: it is there
so the script has one stable, managed host path (gh#110), which is a different reason from the one
that ships a skill.

**Why step 7 (after pip install, before hook registration):** deployment is pure file
placement and needs nothing from the venv, but grouping it with steps 8–9 keeps every
write into the user's Claude config contiguous — and ordering it *before* hook
registration means a machine whose hooks are registered always already has its skills.

**Mechanism:** Unix **symlinks**. Windows **junctions** for directories and copies for files,
so the 22 skill directories track the suite checkout live while the 13 `commands/*.md` and the
one hook script do not (a junction points only at a directory). A Windows symlink would cover
both but needs admin or Developer Mode, which is a property of the machine and cannot be a
default; a junction needs neither. Provision proves the volume can hold one before it writes the
first entry and falls back to copies loudly if it cannot.

**Why it changed (gh#296):** a copy is stale the instant a merge lands and nothing re-deploys
afterwards, so every seat ran the PREVIOUS version of its own instructions until someone
re-provisioned by hand - measured once at 7 of 7 loop skills, for two and a half hours.

**Ownership on Windows lost a witness.** A copy carried a `.agentflow-deployed` marker and could
prove itself after a manifest loss. A junction proves ownership only by resolving under the
current `suite_root`.

**Collision policy:** a pre-existing entry NOT owned by agentflow — no manifest record, no
`.agentflow-deployed` marker, not a symlink into the suite, and not already byte-identical to
its source — is a **loud refusal**: never a skip, never clobbered, not even with `--force`.
`--force` refreshes agentflow-owned entries whose deployed copy was locally modified; a plain
re-run refreshes owned entries when the *suite* changed and no-ops when nothing changed
(idempotent, exit 0).

**Deployment manifest:** `<claude_root>/.agentflow-deploy.json` records
`source → target + sha256` per entry — the ownership record and the seam the future
updater skill will read.

## Verification

```
python install/provision.py --dry-run    # prints the ordered plan, exits 0, mutates nothing
```
