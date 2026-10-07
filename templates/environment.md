# environment.md — per-machine config (TEMPLATE)

This is a **template**. The installer (`install/SKILL.md`) fills the real values from an
interview and writes the result to `~/.agentflow/config/environment.md` (config home is
`$AGENTFLOW_CONFIG` or `~/.agentflow/config/`). The `agentflow.config` resolver reads the
FIRST fenced YAML block below; prose after it is human notes, ignored by the resolver.

The typed schema and every validation rule this block must satisfy are frozen in
`skills/_shared/SCHEMA.md` §1. The values shown here are illustrative placeholders — swap
them for your machine. Secrets are NEVER stored here: `notify.url_env` / `notify.topic_env`
carry the NAME of an env var; the installer writes the real value one-way into
`settings.local.json` / `.env`.

```yaml
schema_version: 1

person:
  name: "Ada"                 # R — display name for addressing + ntfy prose

data_root: "~/agentflow-data" # R — root of projects/<name>/{design/{specs,plans,use-cases,suggestions},status,wiki/pages{,/done,/archived}}
autodoc_root: null            # O (default null) — MACHINE-LEVEL code-wiki root for /autodoc-init|update. A per-project
                              #     `autodoc_root` in projects/<name>.md beats it; leaving this null is the usual answer
                              #     (see the note below — a value here applies to EVERY project without its own override)
mtime_cutoff_iso: "2026-01-01T00:00:00+00:00"  # O — epic-survey mtime floor
question_style: mobile-two-turn  # O (the default) — asking-questions protocol; set inline to opt out

git_host:
  kind: github                # R — github | gitea | gitlab | local
  cli: gh                     # O — gh | glab | none (default gh when kind==github)
                              #     Only gh is verified: glab is refused at the first tracker
                              #     call and ignored by the hardcoded-gh PR paths.

notify:
  kind: ntfy                  # R — ntfy | none
  url_env: "NTFY_URL"         # R* iff kind!=none — NAME of the env var holding the ntfy URL
  topic_env: "NTFY_TOPIC"     # R* iff kind!=none — NAME of the env var holding the ntfy topic
  ascii_only: true            # O (default true) — Forced ntfy ASCII-only body opinion

lock_dir: "/tmp"              # R — mkdir edit-lock dir. /tmp is the POSIX value only; on Windows
                              #     write e.g. "~/AppData/Local/Temp/agentflow-locks". Only ~ is expanded;
                              #     a Windows %-variable never is, and is read as a relative path.
shell: posix                  # R — posix | powershell
editor_cmd: "${EDITOR:-code}" # O (default "${EDITOR:-code}")
suite_root: "~/agentflow"     # R — install root; jr-dev skills inherit from lead-dev here
worktree_root: "~/worktrees"  # R — root every lane's per-epic worktree is created under, laid out
                              #     <worktree_root>/<project>/<epic-branch>, the claim branch name
                              #     (<epic-slug> in a hand run). No default: a lane launches
                              #     from the launch directory, which is in no repo, so nothing can be
                              #     inferred from cwd. Must exist — create it before first launch.
status_board_url_template: null  # O (default null) — e.g. "https://board.example/{slug}"; null => print status.md path

# THREE states, and the third is the one worth writing down:
#   key absent   — the install interview never asked
#   vault: null  — asked, and the answer was no. Mirroring behaves identically (there is no
#                  vault either way); `doctor` reads the difference and stops offering
#                  candidates to somebody who already declined. `vault:` with an empty body
#                  is the same answer - the value is what is read, not the spelling.
#   a block      — as below
vault:                        # O block — omit entirely, or null, to disable wiki mirror + knowledge capture
  root: "~/wiki"              # R* iff vault present
  subdir: "wiki/epics"        # O (default wiki/epics) — <root>/<subdir>/<project>/<slug>/
  save_skill: "~/wiki/skills/save/SKILL.md"                 # O — Delegated SKILL.md (presence-checked)
  autodoc_update_skill: "~/wiki/skills/autodoc-update/SKILL.md"  # O — Delegated SKILL.md
  mirror_helper: "~/wiki/bin/mirror-epic.sh"               # O — Delegated script (feature-detected -x)
  hot_cache: "~/wiki/hot.md" # O — hot-cache base

parallel_builders:            # O — opt-in multi-builder module (default OFF)
  enabled: false              # O (default false) — the module's on/off switch
  count: 1                    # O (default 1) — junior lanes 0/1/2/3: 0 => none; 1 => jr-dev-1;
                              #     2 => + jr-dev-2; 3 => + jr-dev-3 (the interim fourth builder)
```

## Notes (human-only, ignored by the resolver)

- **Secrets stay out of markdown.** `notify.*_env` fields hold env-var NAMES; the real
  values live one-way in `settings.local.json` / `.env`. The resolver reads the name and
  looks the value up in `os.environ` at resolve time — a missing named var is a loud
  hard-fail, never a silent default.
- **`data_root`** is the single root all project work lives under. Each project gets the fixed subtree `<data_root>/projects/<name>/{design/{specs,plans,use-cases,suggestions},status,wiki/pages{,/done,/archived}}/`:
  `design/specs/` holds functional design (refine) docs, `design/plans/` holds technical plans,
  `status/` holds orchestrator status boards + the Fleet Registry (`registry.md` /
  `registry-log.md`, seeded by the installer) + per-agent loop state (`<agent>-state.md`),
  and `design/use-cases/` holds use-case docs (Phase-4 traceability). The `queue/` directory is
  retired. The installer scaffolds these; the disk epic-survey globs them. See
  `docs/DATA-LAYOUT.md` and `docs/REGISTRY.md`.
- **`vault`** is optional. Omit the whole block to run without a wiki; when present, the
  three Delegated helper paths are presence-checked and any missing one degrades its step to
  the Forced "skip silently, never fatal" behavior. **`vault.save_skill` is not
  `skills/wiki-save/`**, and the two are easy to conflate because both are called "save":
  `vault.save_skill` is a *Delegated external* `SKILL.md` on your machine that the orchestrator
  and lead-dev FOLLOW to file an epic note into your vault (skipped silently when no `vault` block
  exists), while `skills/wiki-save/` is the suite's own writer for the `wiki/` pillar under
  `data_root`. Different writer, different destination, no relationship
  beyond the word.
- **`autodoc_root`** is the managed code-documentation wiki root consumed by `/autodoc-init` and
  `/autodoc-update`, and **this field is the machine-level one**. Read the precedence before you
  set it (`agentflow.config.resolve_autodoc_root` — the resolver `doctor` and the touchpoint tools
  use, as do `/autodoc-init` and `/autodoc-update`): the project's own `autodoc_root` in
  `projects/<name>.md` wins, then this field, then the derived
  `<data_root>/projects/<name>/autodoc/` **only if that directory already exists**, and otherwise
  autodoc is simply unconfigured. Two consequences worth having in front of you:
  - **A value here applies to every project without its own override.** Once a corpus built there
    names its project in `.manifest.json`, every other project skips it and falls through to its
    derived path or to no root; before that first build, every such project resolves to the same
    directory and `doctor` reports it `ok`. Prefer the
    per-project override unless the machine genuinely has one code wiki for everything.
  - **Nothing creates `<data_root>/projects/<name>/autodoc/`** — not the installer, and not
    `/autodoc-init` either. That skill resolves its root through `resolve_autodoc_root`, as
    `/autodoc-update` does, and exits with `SystemExit` when none resolves; the `mkdir` runs
    after the check. On a fresh install with no `autodoc_root` set, `/autodoc-init` exits rather
    than scaffolding, so building a first corpus requires an `autodoc_root` on an existing
    directory, in the project profile (preferred) or here.
    A fresh install reporting "no autodoc root configured or derived" is correct, not broken.

  To migrate an existing code wiki, point the appropriate `autodoc_root` at it — the data stays
  where it is; the skills resolve their suite paths + repo coordinates from config. **They never
  commit the subtree into "whatever git repo encloses it"**, and the vault
  mirror does not cover autodoc/ (it mirrors design specs and plans only). A built corpus is
  untracked, with or without a vault: pages re-render from the graph store in `.source/graph.db`,
  which lives inside the tree, so deleting the tree loses both.
  Obsidian is an accepted host but not required.
- **`parallel_builders`** is the multi-builder module: `enabled` is the on/off switch and
  `count` is the number of junior builder lanes. The gates that actually execute are the
  *Module gate* sections of the junior skills — `skills/jr-dev-1/SKILL.md` halts unless
  `count >= 1`, `skills/jr-dev-2/SKILL.md` unless `count >= 2`, `skills/jr-dev-3/SKILL.md`
  unless `count == 3` — so `0` leaves lead-dev standalone, `1` activates jr-dev-1 alone, `2`
  activates both of the first two juniors, and `3` adds `jr-dev-3`, each with its own registry
  `## Queue` lane. The resolver rejects any value outside `{0,1,2,3}`, and rejects `count > 1`
  without `enabled: true`. `3` and `jr-dev-3` are **interim**: the lane is a hand-copy of
  `jr-dev-2` that a later epic replaces with a count-driven set, and both go together.
- **`question_style`** selects the `asking-questions` protocol branch. `mobile-two-turn` is the
  default: a prose turn carrying the full decision, then a separate button turn, because a client
  that renders only the cards in a turn containing an `AskUserQuestion` call hides same-turn
  prose. `inline` is the opt-out, for a client that shows prose and cards together: decision prose
  and the `AskUserQuestion` call share one turn. The protocol logic itself is not configurable;
  only which branch runs is.
- **`status_board_url_template`** is nullable because agentflow ships no dashboard; the
  `status.md` data contract is preserved for a future board. `null` => handoff messages print
  the `status.md` path instead of a URL.
