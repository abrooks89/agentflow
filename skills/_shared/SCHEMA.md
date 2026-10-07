# Agentflow — Typed-YAML Config Schema (FROZEN)

This document is the ratified, behavior-frozen schema for the agentflow suite's two
config surfaces:

- `environment.md` — per-machine facts (paths, notify, git host, service URLs, shell, locks).
- `projects/<name>.md` — per-product facts (repo, branches, deploy, verification, domains, vocab).

It is the single source of truth the `agentflow.config` resolver validates against
(P1-T2) and the SessionStart hook injects from (P1-T3). It ratifies the ground-01
`§2`/`§3` field lists **verbatim** — Phase 1 freezes behavior; no field is invented,
renamed, or dropped here beyond the additions called out in **§0**.

Config home resolves from `$AGENTFLOW_CONFIG` or defaults to `~/.agentflow/config/`.
The machine values live in the FIRST fenced ` ```yaml ` block of each markdown file;
prose after the block is human notes, ignored by the resolver.

---

## §0. Field-tag legend + ground-04 additions

Legend for the `R/O` tag on every field:

- **R** — required. Absent ⇒ `ConfigError` (hard-fail, loud, never defaulted).
- **O** — optional. Absent ⇒ declared default (or `None`); never fatal.
- **R\*** — required-conditionally. Required only when a related field/block is enabled;
  the exact predicate is stated in the field's conditional-required rule.

Type vocabulary:

- `path` — absolute or `~`-expanded string that MUST resolve (dir/file per field).
- `env-name` — the NAME of an env var; the VALUE is read from `os.environ` at resolve
  time and is NEVER stored in markdown (secrets stay in `settings.local.json`/`.env`).
- `string`, `int`, `bool`, `prose`, `list[string]`, `enum[...]`, `skill-path` — as named.
- `skill-path` — a `path` to a delegated `SKILL.md` / script; presence-checked (see §5).

**Ground-04 additions ratified into this schema (beyond ground-01 §2/§3):**

- `environment.mtime_cutoff_iso` — ISO-8601 timestamp, **O**, default
  `2026-04-29T00:00:00+00:00`. Consumed by the filesystem epic-survey
  (`agentflow.epic_survey.survey_epics`, P1-T9) as the mtime floor: records older than
  this cutoff are ignored during discovery. Frozen default is the pre-port constant.
- `environment.parallel_builders` — the opt-in multi-builder module is a two-field struct
  (see §2 field + rule below): **`enabled` is the on/off switch** (default `false`) and
  **`count` is the number of junior builder lanes**, one of **0/1/2/3**. `0` ⇒ no junior
  lane, `lead-dev` standalone; `1` ⇒ the `jr-dev-1` lane only; `2` ⇒ `jr-dev-1` + `jr-dev-2`;
  `3` ⇒ those two plus `jr-dev-3`, the INTERIM fourth builder (a hand-copy of `jr-dev-2`
  that `lead-dev-as-foreman-and-n-junior-lanes` replaces with a count-driven set — see the
  *Vocabulary* block in `agentflow/registry.py`, which is where the lane enum lives).
  The gates that actually execute live in the junior skills' *Module gate*
  sections: `skills/jr-dev-1/SKILL.md` halts unless `count >= 1`, `skills/jr-dev-2/SKILL.md`
  unless `count >= 2`, `skills/jr-dev-3/SKILL.md` unless `count == 3`. The SessionStart hook
  injects the two fields separately, as
  `[active builders] enabled: <bool>, count: <n>` (`hooks/sessionstart-inject-config.py`).
  This is the ground-04 restatement of the ground-01 `parallel_builders.{enabled,count}`
  struct; both forms are documented in §2 so the resolver accepts the frozen struct while
  the lane semantics stay explicit.

**`environment.worktree_root` (R):** the per-machine root every lane's
per-epic worktree is created under. It is required because a lane launches from the
machine's single launch directory, which sits in no repo, so no worktree path can be
inferred from the working directory and every git action has to name one. Full field entry + validation rule in §1.

---

## §1. `environment.md` — per-machine typed-YAML schema (ratifies ground-01 §2)

```yaml
# ~/.agentflow/config/environment.md  (typed block)
schema_version: 1                    # int, R

person:
  name: string                       # R — addressing + ntfy prose ("Ada")

data_root: path                      # R — root for projects/<name>/{design/{specs,plans,use-cases,suggestions},status,wiki/pages{,/done,/archived}}
autodoc_root: path | null            # O, default null — managed code-wiki root for /autodoc-init|update; point at an existing code-wiki folder to migrate in place (data stays)
mtime_cutoff_iso: string             # O, default "2026-04-29T00:00:00+00:00" — epic-survey mtime floor (ground-04)
question_style: enum[inline|mobile-two-turn]  # O, default mobile-two-turn — selects the asking-questions protocol branch; inline opts out, for a client that shows prose and cards together

git_host:
  kind: enum[github|gitea|gitlab|local]   # R
  cli:  enum[gh|glab|none]                # O, default gh if kind==github else none

notify:
  kind:      enum[ntfy|none]         # R
  url_env:   env-name                # R* iff kind!=none — e.g. "NTFY_URL"
  topic_env: env-name                # R* iff kind!=none — e.g. "NTFY_TOPIC"
  ascii_only: bool                   # O, default true (some ntfy servers reject multibyte bodies with HTTP 400)

lock_dir: path                       # R — mkdir edit-lock dir (/tmp POSIX | ~/AppData/Local/Temp/agentflow-locks Windows; only ~ expands)
shell:    enum[posix|powershell]     # R
editor_cmd: string                   # O, default "${EDITOR:-code}"
suite_root: path                     # R — agentflow install root (jr-dev skills inherit from lead-dev here)
worktree_root: path                  # R — root every lane's per-epic worktree is created under: <worktree_root>/<project>/<epic-branch> (claim branch name; <epic-slug> in a hand run)
status_board_url_template: string | null  # O, default null — e.g. "https://board.example/{slug}"; null ⇒ print status.md path

vault:                               # O block — omit/null to disable wiki mirror + knowledge capture
  root:                 path         # R* iff vault present
  subdir:               string       # O (default "wiki/epics") — <root>/<subdir>/<project>/<slug>/
  save_skill:           path | null  # O — Delegated SKILL.md (presence-checked)
  autodoc_update_skill: path | null  # O — Delegated SKILL.md
  mirror_helper:        path | null  # O — Delegated script (feature-detected -x)
  hot_cache:            path | null  # O — hot.md base

parallel_builders:                   # O — the opt-in multi-builder module (default OFF)
  enabled: bool                      # O, default false
  count:   int                       # O, default 1  — junior lanes 0/1/2/3: 0 ⇒ none; 1 ⇒ jr-dev-1; 2 ⇒ + jr-dev-2; 3 ⇒ + jr-dev-3 (interim)
```

### environment.* field table (R/O + type)

| field | type | R/O | notes |
|---|---|---|---|
| `schema_version` | int | R | must equal 1 |
| `person.name` | string | R | addressing + ntfy prose |
| `data_root` | path (dir) | R | root of `projects/<name>/{design/{specs,plans,use-cases,suggestions},status,wiki/pages{,/done,/archived}}` |
| `autodoc_root` | path \| null | O (default null) | managed code-wiki root consumed by `/autodoc-init` + `/autodoc-update`; migrate an existing code-wiki folder by pointing this at it (data stays in place) |
| `mtime_cutoff_iso` | string (ISO-8601) | O (default `2026-04-29T00:00:00+00:00`) | epic-survey mtime floor (ground-04) |
| `question_style` | enum[inline\|mobile-two-turn] | O (default `mobile-two-turn`) | selects the vendored `asking-questions` protocol branch; `mobile-two-turn` = strict two-turn, the default; `inline` = prose+call one turn, the opt-out for a client that shows prose and cards together. |
| `git_host.kind` | enum[github\|gitea\|gitlab\|local] | R | |
| `git_host.cli` | enum[gh\|glab\|none] | O (default gh if kind==github else none) | |
| `notify.kind` | enum[ntfy\|none] | R | |
| `notify.url_env` | env-name | R\* | required iff `notify.kind != none` |
| `notify.topic_env` | env-name | R\* | required iff `notify.kind != none` |
| `notify.ascii_only` | bool | O (default true) | Forced ntfy ASCII-only opinion |
| `lock_dir` | path (dir) | R | mkdir edit-lock dir (`/tmp` POSIX; on Windows an absolute or `~/` path such as `~/AppData/Local/Temp/agentflow-locks`: only `~` is expanded, never an environment variable) |
| `shell` | enum[posix\|powershell] | R | |
| `editor_cmd` | string | O (default `${EDITOR:-code}`) | |
| `suite_root` | path (dir) | R | install root; jr-dev skills inherit from `<suite_root>/skills/lead-dev/SKILL.md` |
| `worktree_root` | path (dir) | R | per-machine absolute root under which every lane's per-epic worktree is created, laid out `<worktree_root>/<project>/<epic-branch>`: the claim branch name `e<NNNN>-<short>` in a fleet run, `<epic-slug>` in a hand run with no registry row. No default: a silently-defaulted worktree path is how a lane ends up building in a directory nobody chose. |
| `status_board_url_template` | string \| null | O (default null) | null ⇒ print `status.md` path |
| `vault.root` | path (dir) | R\* | required iff `vault:` block present. **Exported to the mirror as `WIKI_VAULT_PATH`** — the helper otherwise defaults to a hardcoded path, so a root nobody passes is a root nobody uses. |
| `vault.subdir` | string | O (default `wiki/epics`) | layout is `<root>/<subdir>/<project>/<slug>/<kind>.md` |
| `vault.save_skill` | skill-path \| null | O | Delegated, presence-checked |
| `vault.autodoc_update_skill` | skill-path \| null | O | Delegated, presence-checked |
| `vault.mirror_helper` | skill-path \| null | O | Delegated script, feature-detected (`-x`) |
| `vault.hot_cache` | path \| null | O | `hot.md` base |
| `parallel_builders.enabled` | bool | O (default false) | opt-in module gate |
| `parallel_builders.count` | int | O (default 1) | junior lanes 0/1/2/3: `0` ⇒ none (`lead-dev` standalone); `1` ⇒ `jr-dev-1` only; `2` ⇒ `jr-dev-1` + `jr-dev-2`; `3` ⇒ + `jr-dev-3`, the interim fourth builder |

### environment conditional-required (R\*) + validation rules

- **notify:** `notify.url_env` and `notify.topic_env` are required only when
  `notify.kind != none`.
- **vault:** `vault.root` is required only when a `vault:` block exists (whole block is O).
  An EXPLICITLY EMPTY key (`vault: null`, or `vault:` with no body) is a third state and is
  read: it records that the install interview asked and the answer was no, and `doctor` stops
  offering vault candidates once it is present. An ABSENT key means nobody was asked. Both
  resolve to no vault, so mirroring is identical either way.
- **paths:** every `path` field must exist. `suite_root`, `data_root`, `lock_dir`,
  `worktree_root` must be directories.
- **worktree_root:** absolute (or `~`-expanded) per-machine path, resolved beside
  `suite_root` in the builder. It is the root every lane's per-epic worktree is created
  under — `<worktree_root>/<project>/<epic-branch>`, the claim branch name in a fleet run and
  `<epic-slug>` in a hand run — so one machine has one worktree home
  and all fleets share it, partitioned by project. Absence is a hard `ConfigError` on
  `worktree_root`, reason `required key 'worktree_root' is missing`, and the remediation
  below **verbatim** — not the generic `add '<field>:' (string)` the other required keys
  get, because this is the one error an existing install hits on upgrade and a remediation
  without an example value costs every one of them a round trip guessing a path that does
  not exist yet. A configured path that does not exist, or exists as a file, fails on the
  same field. It is required rather than defaulted because a silently-defaulted worktree
  path is how a lane ends up building in a directory nobody chose — the launch directory is
  the harness home, and the work location is data.

  <!-- remediation:worktree_root:begin -->
  ```text
  add 'worktree_root: ~/worktrees' - an EXISTING directory under which each lane's per-epic worktree is created as <worktree_root>/<project>/<epic-branch> (the claim branch name, e<NNNN>-<short>; <epic-slug> in a hand run); create it first, then re-run. See SCHEMA.md.
  ```
  <!-- remediation:worktree_root:end -->

  That block is quoted here rather than described because prose about an error message drifts
  from the message silently. `skills/_shared/tests/test_one_launch_dir.py` extracts
  the block between those two markers and requires it to equal the remediation
  `agentflow.config` actually raises, byte for byte. The two are written independently and
  checked equal — neither imports the other, so editing one alone turns the suite red.
- **env-name:** every `env-name`'s named var must be present in the process env at
  resolve time, else HARD FAIL (a missing secret is a loud failure, never a default).
- **parallel_builders:** `count in {0,1,2,3}`; `count > 1` requires `enabled: true`
  (both raise `ConfigError` on `parallel_builders.count`). `count` is the number of
  junior lanes: `0` leaves lead-dev standalone, `1` activates `jr-dev-1` alone, `2`
  activates `jr-dev-1` + `jr-dev-2`, and `3` adds `jr-dev-3` — each opening the matching
  registry `## Queue` lane (`docs/REGISTRY.md`; the lane tuple is `registry.LANES`).
  `3` is an interim value: it exists only while `jr-dev-3` does.
- **mtime_cutoff_iso:** must parse as ISO-8601; default `2026-04-29T00:00:00+00:00`.
- **question_style:** must be `inline` or `mobile-two-turn`; default `mobile-two-turn`. `inline` opts out,
  for a client that shows prose and cards together.
  Consumed by the `asking-questions` skill (`skills/asking-questions/SKILL.md`) to pick its
  protocol branch; the protocol logic itself is Forced (not configurable) — only which
  branch runs is configured.

### The `data_root` subtree (the consolidated single-root layout)

`data_root` is the ONE root all project work lives under. Every project owns a fixed
per-project subtree derived from `data_root` + the project `name` (the routing key). There is
exactly one home per work-file kind:

**Four pillars, and the axis that separates them is AUTHORSHIP.** Each bucket answers a
different question and has a different writer, which is what stops them collapsing into one
another: `design/` is what we INTEND and a human writes it; `autodoc/` is what the code IS and
is generated from the code; `wiki/` is what we LEARNED and the sessions write it; `status/` is
what the fleet is DOING and the loops write it. A file whose writer does not match its bucket is
in the wrong bucket.

**Three of the four are scaffolded; on a fresh install `autodoc/` is created by nothing at all.** `install/provision.py`'s
`DATA_SUBTREE_DIRS` is eight entries and contains no `autodoc/`; the pillar's canonical home is
`<data_root>/projects/<name>/autodoc/`, and **`/autodoc-init` does not bring it into being** —
it resolves the root through `resolve_autodoc_root` and exits with `SystemExit` when none resolves,
and the `mkdir` runs after that check. On a fresh install the pillar therefore exists only where
a human set an `autodoc_root`, in the project profile or in `environment.md`, to an existing
directory.
`resolve_autodoc_root` — the resolver `doctor`, the touchpoint tools, `/autodoc-init`,
`/autodoc-update` and `graph-query` use — is the project's `autodoc_root` override (§2), then `environment.autodoc_root`,
then the derived per-project path only when that directory already exists, and otherwise `None`.
With no project override, an `environment.autodoc_root` whose `.manifest.json` names a different
`project` is skipped: the resolver returns the derived path if it exists, else `None`, and
never raises `AutodocRootError` against the skipped corpus. An explicit override is never checked,
and a manifest with no `project` field resolves as before. So a machine-level root with no
manifest, or with no `project` field, still wins for every project on the box and reports healthy,
which is why the per-project override exists and is the one to prefer wherever the resolver is what
reads it.

**Epic identity vs priority — they are different fields and neither derives the other.** An epic's
registry identity is `E-<NNNN>-<short-name>`: a number allocated from the registry's own monotonic
sequence at `publish`, plus the first four hyphen-tokens of the spec slug. Its **priority** is the
`priority:` integer in the spec's frontmatter, and it appears on human surfaces only as a trailing
suffix (`... (p260)`) — in the Queue row's `work item` cell, never inside the id. The two change
independently: rebanding a spec moves the suffix and nothing else, and an epic keeps its number for
life, including across a `withdraw` and re-`publish`. The 100-step banding of `priority:` is a
*sort* convention and carries no identity. Ids issued before the allocator are bare `E-NNNN`
numbers that in practice WERE the priority; they remain readable forever and are never rewritten.
Full contract, including the allocator's cull-proof seeding, in `docs/REGISTRY.md` `## Queue`.

**The `wiki/` hot tier is two-level, and both levels are PROJECTIONS rather than documents.**
`hot.md` is the project-wide top-N over `pages/`; `hot-<lane>.md` is the same function over the
same page set with a lane filter applied, so a builder loads builder lessons and the steward
loads sweep lessons instead of seven seats loading one flat store. Neither is hand-edited: they are
regenerated from `pages/`, which is why the projection needs no lock. Memory is the hot tier of
this pillar, not a store beside it.

**The `pages/` UNIT is one page per durable lesson or decision**, written at the end of the firing that produced it. `pages/` therefore grows **per OUTCOME, never
per wake**: a firing that learned nothing durable writes nothing, and one that learned two
separable things writes two pages rather than one page with two headings. The unit is what makes
the top-N projection mean anything — a per-wake unit would rank the hot tier by how often a lane
wakes rather than by what was learned, and a bundled page ranks as one thing with half
of it invisible. The writer is `<suite_root>/skills/wiki-save/SKILL.md`, the only thing in the suite
that creates a page; it also owns the four trigger points and the read boundary, which are
procedure rather than schema and are not restated here.

```
<data_root>/
└── projects/
    └── <name>/
        ├── design/       # what we INTEND to build       — human-authored
        │   ├── specs/       # functional design docs        — <slug>-design.md
        │   │   ├── done/       # TERMINAL partition — a VIEW of current_state (agentflow.reconcile)
        │   │   └── archived/   # TERMINAL partition — closed without delivering
        │   ├── plans/       # technical plan docs           — <slug>.md   (same two partitions)
        │   ├── use-cases/   # use-case docs (traceability spine)
        │   └── suggestions/ # harvested feature suggestions
        ├── autodoc/      # what the code IS  — NOT scaffolded; /autodoc-init needs autodoc_root set first
        ├── wiki/         # what we LEARNED              — written by sessions
        │   ├── pages/       # durable pages, Karpathy-wiki style — CREATION-ONLY
        │   │   ├── done/       # terminal partition; not read by the projection
        │   │   └── archived/   # terminal partition; not read by the projection
        │   ├── hot.md       # PROJECTION over pages/ — project-wide top-N. Generated.
        │   ├── hot-<lane>.md # PROJECTION over pages/ — one per lane. Generated.
        │   └── digest.md    # human-facing, written by the STEWARD on its sweep
        └── status/       # orchestrator status boards        — <slug>.status.md / <slug>/status.md
                          #   ONE board per slug, in either form: a second refuses the epic (neither done nor selectable) until one remains
                          # + registry.md / registry-log.md (Fleet Registry, the single coordination surface all SEVEN loops share; see docs/REGISTRY.md)
                          #   Queue ids are `E-<NNNN>-<short-name>`: see "Epic identity vs priority" below.
                          # + <agent>-state.md (per-agent loop state — lead-dev-state.md, jr-dev-1-state.md, …)
                          # + audit-ledger.md + audits/ (auditor loop)
```

The `queue/` directory is **retired**: all per-agent loop state consolidates at
`status/<agent>-state.md`, shared coordination lives only in the registry, and the per-loop
questions files are gone — questions are registry `## Questions` rows (`docs/REGISTRY.md`).

The disk epic-survey (`agentflow.epic_survey`, §4.9) globs this layout by default
(`projects/*/design/specs/*.md`, `projects/*/design/plans/*.md`, `projects/*/status`), and each
of the two bucket globs also reaches one level down into every entry of
`epic_survey.TERMINAL_PARTITIONS` (`projects/*/design/specs/done/*.md`,
`projects/*/design/specs/archived/*.md`, and the same two under `plans/`), derived by
`epic_survey._bucket_globs` so a record `reconcile` files away stays surveyed. Passing
`survey_epics(..., project=<name>)` replaces that `*` segment with the project name, so the survey
sees ONLY `projects/<name>/` — **the builder loops (`lead-dev`, `jr-dev-*`, `auditor`, `steward`)
must pass it**, and this is code-enforced scope, not a naming convention: two fleets share one
`data_root`, and the unscoped default would let one fleet's builder claim the other's specs.
The unscoped default is retained deliberately for the three project-wide readers —
dev-manager (refinement grooms every project), `choose-next` (human selection), and `backlog`
(a discoverability confirmation) — each of which says so explicitly at its call site.
The installer
scaffolds `projects/` and, per configured project, the eight directories of
`provision.DATA_SUBTREE_DIRS` — `design/{specs,plans,use-cases,suggestions}`, `status`,
`wiki/pages{,/done,/archived}` — and seeds `status/registry.md` from `templates/registry.md`.
The terminal partitions under `design/specs/` and `design/plans/` are created by
`agentflow.reconcile` when it first has a terminal record to file. `autodoc/` is created by
nothing at all — see the pillar note above. Skills address all of these
by config token (`<data_root>` + `<name>`), never by literal path.

#### Epic-frontmatter vocabulary the survey enforces (`queue_class`, `project`, `current_state`)

Three spec/plan frontmatter fields decide autonomous dispatch, and all three are checked in
`agentflow.epic_survey.dispatchable()` — code, not prose the builders are trusted to remember:

| value of `queue_class` | dispatchable | meaning |
|---|---|---|
| *unset* | yes | legacy / untagged; the normal promotion path |
| `fleet-buildable` | yes | explicitly built by the fleet |
| `brainstorm-pin` | no | needs a brainstorm before it can be specced |
| `local-tooling` | no | local-session dev tooling, built by hand |
| `on-server-ops` | no | manual infra / credential ops |
| anything else | no | **out of vocabulary** — dropped AND named in a survey warning line |

##### The retired value, and the only places it is allowed to appear

`fleet-buildable` replaced an earlier spelling as a HARD cut — the old value is off the
allow-list outright rather than accepted alongside the new one, so a spec still carrying it is out
of vocabulary, warns once, and dispatches nothing. `agentflow.migrate_queue_class`
rewrites a project's corpus — `--project <name>` plus **either `--dry-run` or `--apply`**, since a
bare run refuses rather than guess which was meant. It reaches exactly the files `survey_epics()`
parses and writes no backups of its own.

The retired spelling is written in **one** place — `agentflow.vocabulary.RETIRED_QUEUE_CLASS` —
and everything that needs it imports it. Three occurrences in the tree are **permanent and
exempt** — that sanctioned home plus two records outside the code — and any sweep or gate over
this vocabulary must name them rather than count to one:

| exempt occurrence | why it stays |
|---|---|
| `skills/_shared/agentflow/vocabulary.py` | the constant itself — the one sanctioned home |
| `CHANGELOG.md` | an immutable historical record; it says what was true when it was written |
| `harness/fixtures/scenarioF-carved/2026-08-30-multi-run-export-bundles.md` | a recorded harness fixture; rewriting it falsifies the baseline it is evidence for |

`current_state` must be `approved-for-autonomous` or the record is not dispatchable, checked in
the same function and normalised for casing and stray whitespace. An **unset** `current_state`
does NOT dispatch — deliberately unlike the `project`-unset rule below it. The asymmetry is the
point: `project:` postdates the earliest specs, so its absence is legitimately unknown, whereas a
spec cannot predate its own approval, so an unset approval is simply not an approval.

#### The spec BODY precondition: a `## Files` section (promotion gate)

Frontmatter decides dispatchability; one part of the **body** decides publishability. A spec may
not be promoted to `approved-for-autonomous` unless it carries a `## Files` section naming the
files it will touch, as **real repo-relative paths**.

`lead-dev` proves two lanes are disjoint from the `domain` field and that listing. With neither
usable it cannot prove disjointness and correctly refuses to publish - and the epic then sits
approved, looks like progress on every board, is claimable in principle forever, and nothing
reports why it never moves.

The check is `agentflow.plan_parser.files_section(text)`. **Two parts, and only the first is a
gate:** `present` says the section exists; `seam_checkable` says it also names something
path-shaped, and the survey warns when it is empty. Neither can judge whether the listing is
*right* - a section reading "the loop skills" is `present` and useless - so the promoting seat
reads it (`dev-manager/SKILL.md` §7c).

**The extraction rule is one backticked, repo-relative path per entry**; an un-backticked entry
declares nothing. `- src/widget.py (new)` gives `present` with zero paths, so it is not
`seam_checkable`, and `survey_epics` warns on it at `drafted` and `approved-for-autonomous` with a
message distinct from the missing-heading one.

`queue_class` is an **allow-list**: only the first two rows dispatch. A deny-list of the three
known-unbuildable values would let `bug`, `feature` or a typo sail through. The out-of-vocabulary case is a *warning*, never an exception — one mistyped
spec must not break dev-manager's project-wide refinement survey. dev-manager assigns blocker ownership; the steward repairs
this field on grooming and refuses to promote a non-dispatchable class to the approved state.

`project:` names the owning project. `dispatchable(..., project=<name>)` drops a record whose
`project:` is set and differs from `<name>`; `project:` unset still dispatches (the field
postdates the earliest specs). This is the record-level half of the project scope above — the
glob narrowing is the other half, and both exist because either alone is bypassable.

#### `renamed_from:` — records a rename so an orphaned mirror can be reported

`renamed_from: <previous slug>` is an optional spec-frontmatter key that records the spec's
immediately-previous slug, normalised exactly as `slug:` is. A mirror is keyed on slug and
`mirror_of` is a one-shot snapshot, so without this key a rename's old mirror is indistinguishable
from the mirror of a deleted spec. It is **one hop only** (the old slug of the old slug is not
followed) and **same routing project only**. The only reader is the vault's `bin/mirror-epic.sh
--reconcile` (the delegated `vault.mirror_helper`, an external vault script that does not ship with
agentflow), and it is **report-only**: a claimed orphan prints as `RENAMED`, or with a qualifier
(`RENAMED-PENDING`, `-DIRTY`, `-UNTRACKED`, `-INVALID`, `-CONFLICT`, `-AMBIGUOUS`) saying why it
could not be retired safely, and nothing is deleted. The report is a lead, not a verdict: a
canonical named `*.MD` or `*.markdown`, under a dot-named project dir, or reached through a symlink
is invisible to reconcile's live set, so its live mirror can print as `ORPHAN` or `RENAMED`. Retiring the old mirror is a future epic that
first single-sources the writer's routing, so reconcile and the writer cannot disagree about which
mirrors are live. Nothing validates spec-frontmatter keys today, so a misspelled `renamed_from` is
silently ignored; project-level unknown keys, by contrast, are rejected by
`agentflow.config._reject_unknown_keys`.

---

## §2. `projects/<name>.md` — per-product typed-YAML schema (ratifies ground-01 §3)

One file per product. Filename stem == `name` == routing key (replaces the pin closed list).

```yaml
# ~/.agentflow/config/projects/app.md  (typed block)
schema_version: 1                    # int, R
name: string                         # R — must equal filename stem; routing key

canonical_repo:
  host_ref:       string             # R — "owner/repo" on environment.git_host  (acme/app)
  local_checkout: path               # R — ~/CODE/app
  base_branch:    string             # O, default "main"
  python:         path | null        # O — the PRODUCT's interpreter (its own venv). Builders run the product's
                                     #     tests/tools with THIS, never the ambient `python` (which is the suite venv)

deprecated_mirror:           string | null  # O — legacy mirror ref to flag+re-resolve ("legacy/mirror")
deprecated_mirror_checkout:  path   | null  # O — legacy clone to re-ground away from (~/CODE/legacy)

publish_repo:                        # O — the PUBLIC repo this project is published to. Absent = nothing is
                                     #     published, which is the normal case. Never synthesised.
  host_ref: string                   # R* iff block present — "owner/repo", same validation as canonical_repo
  cadence:  string                   # O, default "manual, en masse" — describes the human act, not a schedule
  #
  # READ-ONLY TO EVERY BUILDER — the rule is stated in full in §2's validation rules
  # below, and stated only there so it cannot be weakened in one copy and left standing
  # in another. This is NOT `deprecated_mirror`, which means the opposite thing
  # ("frozen legacy, ignore it").

staging:                             # O — omit for non-deployed projects
  url:            string | null      # R* iff verification.strategy==web-journey
  deploy_host_ssh: string | null     # O — ssh alias of the box a merge rebuilds ("staging-box")
  checkout_path:   path   | null     # O — staging-host checkout for the drift guard (~/app-staging)
  deploy_recipe:   prose | skill-path | null   # O — Configured procedure or Delegated SKILL.md (§4.2)

verification:
  strategy: enum[web-journey|test-suite|smoke-cli|none] | skill-path   # R
  url:      (see staging.url)        # web-journey binds to staging.url
  roles:    list[string]             # O — personas for role-based web journeys (web-journey only)
  test_login: prose | null           # O — how a test subagent authenticates

domains: list[string]                # O — build-order bands (core-engine, enhance-engine, ...)
aliases: list[string]                # O — routing cue words ("the app", product nicknames, domain terms, ...)
harness_notes: prose | null          # O — project-specific grounding footguns kept verbatim
excluded_domains: list[string]       # O — epic `domain:`/`project:` values the fleet must NEVER auto-select; a per-epic
                                     #     `build_approved_by: "<who> <date>"` frontmatter field lifts the ban for that epic only.
                                     #     COARSE knob for a single domain/program only — it is NOT what keeps two fleets
                                     #     on one data_root apart; that is the project scope (survey glob + dispatchable's
                                     #     project-mismatch drop). That scope is an argument the BUILDER passes,
                                     #     not a config field - the guard against forgetting it is the CI test
                                     #     that reads every builder SKILL.md call site (test_skill_call_sites.py)

work_source:                         # O
  kind:       enum[github-issues|gitea|filesystem|none]   # O, default filesystem
                                     #   NOTE: issue intake's argv shapes are gh's against GitHub
                                     #   Issues; `gh` is the only VERIFIED cli and github-issues the
                                     #   only VERIFIED kind. `gitea` refuses loudly on every tracker
                                     #   read and write whatever git_host.cli is (no per-CLI argv
                                     #   builder), as does any cli but gh; with git_host.cli none,
                                     #   reads and label writes stay the documented no-op
                                     #   (claim/release still refuse: no tracker). `gitea` is
                                     #   flagged in the resolved config block whenever
                                     #   git_host.cli would reach the tracker.
  bug_triage: bool                   # O, default false — dev-manager triages open issues (§3.4); lead-dev builds only issues labeled `triaged`
  open_issue_page: int               # O, default 200, must be >= 1 — how many open issues one issue-intake read
                                     #   answers for; past it the list modes and claim/release refuse. Raise it
                                     #   here when the tracker legitimately outgrows it (there is no CLI flag).

generation_skills:                   # O — Delegated skills that *create* work (lead-dev only)
  polishing: skill-path | null       # O — a polish-mode equivalent
  provision: skill-path | null       # O — tenant/seed provisioning

audit:                               # O — the auditor loop's per-project dial (whole block O; every field defaulted)
  window:            int             # O, default 7   — last-N done epics considered per pass (5-10; below 1 hard-fails)
  roles:             list[string]    # O, default []  — Playwright personas for web-journey verification; [] ⇒ falls back to verification.roles
  idle_wake_minutes: int             # O, default 60  — long-wake backoff when no unaudited done epics
                                     #   CAPPED AT 60 by the resolver: ScheduleWakeup clamps
                                     #   delaySeconds to [60, 3600] and reports nothing when it
                                     #   does, so a larger value would be recorded but never slept
  notify:            bool            # O, default true — send a one-line pass summary via the configured notify endpoint

communities:                         # O — autodoc community escape hatch (whole block O)
  pins:                              # O, default {} — path-prefix -> community-name overrides
    "legacy/subsys": "Legacy Subsystem"   # pins the named subtree to a named community

autodoc_root: path | null            # O — this project's code-wiki root; must be an existing directory.
                                     #     Ranked ABOVE environment.autodoc_root by resolve_autodoc_root

vault:                               # O block — this project's vault; vault.root ranks above environment.vault.root
  root:                 path         # R* iff vault present — must be an existing directory
  save_skill:           path | null  # O — Delegated SKILL.md (presence-checked)
  autodoc_update_skill: path | null  # O — Delegated SKILL.md
  mirror_helper:        path | null  # O — Delegated script (feature-detected -x)
  hot_cache:            path | null  # O — hot.md base
                                     # NO `subdir` key here (unlike environment.vault): it is rejected as unknown.
                                     # `vault: null`, or `vault:` with no body, is the explicit "declined" state
```

### project.* field table (R/O + type)

| field | type | R/O | notes |
|---|---|---|---|
| `schema_version` | int | R | must equal 1 |
| `name` | string | R | must equal filename stem (routing integrity) |
| `canonical_repo.host_ref` | string `owner/repo` | R | on `environment.git_host` |
| `canonical_repo.local_checkout` | path (git worktree) | R | `origin` must resolve to `host_ref`; doctor: foreign origin FAIL, no origin WARN |
| `canonical_repo.base_branch` | string | O (default `main`) | deploy-firing base |
| `canonical_repo.python` | path \| null | O | product interpreter; presence-checked; builders never use bare `python` for the product |
| `deprecated_mirror` | string \| null | O | legacy mirror ref, flag+re-resolve |
| `deprecated_mirror_checkout` | path \| null | O | legacy clone |
| `publish_repo` | block | O | the PUBLIC publish target; absent ⇒ nothing is published (normal case). Read-only to every builder — rule stated in the §2 validation rules |
| `publish_repo.host_ref` | string `owner/repo` | R\* | required iff `publish_repo:` block present; same `owner/repo` validation as `canonical_repo.host_ref` |
| `publish_repo.cadence` | string | O (default `manual, en masse`) | prose describing the human act; nothing reads it as a schedule |
| `staging.url` | string \| null | R\* | required iff `verification.strategy == web-journey` |
| `staging.deploy_host_ssh` | string \| null | O | ssh alias of rebuild box |
| `staging.checkout_path` | path \| null | O | staging-host checkout for drift guard |
| `staging.deploy_recipe` | prose \| skill-path \| null | O | Configured procedure or Delegated (§4.2) |
| `verification.strategy` | enum[web-journey\|test-suite\|smoke-cli\|none] \| skill-path | R | enum value OR resolvable SKILL.md path |
| `verification.url` | string \| null | R\* | binds to `staging.url` under web-journey |
| `verification.roles` | list[string] | O | web-journey personas only |
| `verification.test_login` | prose \| null | O | how a test subagent authenticates |
| `domains` | list[string] | O | build-order bands |
| `aliases` | list[string] | O | routing cue words |
| `harness_notes` | prose \| null | O | project-specific grounding, verbatim |
| `excluded_domains` | list[string] | O | never auto-dispatched; per-epic `build_approved_by:` overrides |
| `work_source.kind` | enum[github-issues\|gitea\|filesystem\|none] | O (default filesystem) | |
| `work_source.bug_triage` | bool | O (default false) | dev-manager triages open issues (§3.4); lead-dev builds only issues labeled `triaged` (`agentflow.issue_intake`) |
| `work_source.open_issue_page` | int >= 1 | O (default 200) | the open-issue page `agentflow.issue_intake` reads; more open than this and the list modes and claim/release refuse, naming this key. Below 1 or non-int hard-fails |
| `generation_skills.polishing` | skill-path \| null | O | Delegated (lead-dev only) |
| `generation_skills.provision` | skill-path \| null | O | Delegated (lead-dev only) |
| `audit` | block | O | the auditor loop's per-project dial; whole block optional, every field defaulted |
| `audit.window` | int | O (default 7) | last-N done epics considered per audit pass (intended 5-10). **Below 1 is a ConfigError at load**: 0 would make every pass an empty healthy pass |
| `audit.roles` | list[string] | O (default `[]`) | Playwright personas for web-journey verification; empty ⇒ auditor falls back to `verification.roles` |
| `audit.idle_wake_minutes` | int | O (default 60) | long-wake backoff minutes when no unaudited done epics remain. **Clamped to 1-60 at load, with a WARNING naming both numbers** — ScheduleWakeup clamps `delaySeconds` to `[60, 3600]` and reports nothing, so an unclamped dial would be recorded and never slept. |
| `audit.notify` | bool | O (default true) | send a one-line ASCII pass summary via the configured notify endpoint |
| `communities` | block | O | autodoc community escape hatch; whole block optional |
| `communities.pins` | map[string→string] | O (default `{}`) | path-prefix → community-name overrides for the derived partition; empty/root prefix rejected (no catch-all) |
| `autodoc_root` | path (dir) \| null | O | per-project code-wiki root; must exist and be a directory. `resolve_autodoc_root` ranks it above `environment.autodoc_root`, then the derived `<data_root>/projects/<name>/autodoc/` (only if it exists). With no override, an `environment.autodoc_root` whose `.manifest.json` names a different `project` is skipped for the derived path, or `None`, never `AutodocRootError`; a set override is never checked against its manifest, and a manifest with no `project` field resolves as before. The override to prefer: honored by `doctor`, the touchpoint tools, `/autodoc-init`, `/autodoc-update` and `graph-query` |
| `vault` | block \| null | O | per-project vault; `resolve_vault_root` ranks `vault.root` above `environment.vault.root`. An explicitly empty key (`vault: null`, or `vault:` with no body) records "declined"; see the validation rules |
| `vault.root` | path (dir) | R\* | required iff `vault:` block present |
| `vault.save_skill` | skill-path \| null | O | Delegated, presence-checked |
| `vault.autodoc_update_skill` | skill-path \| null | O | Delegated, presence-checked |
| `vault.mirror_helper` | skill-path \| null | O | Delegated script, feature-detected (`-x`) |
| `vault.hot_cache` | path \| null | O | `hot.md` base |

### project conditional-required (R\*) + validation rules

- **name == filename:** `name` must equal the filename stem (routing integrity) — else
  `ConfigError`.
- **staging.url (R\*):** required iff `verification.strategy == web-journey`;
  `verification.url` binds to it under that strategy.
- **verification.strategy:** an enum value OR a resolvable `SKILL.md` path
  (presence-checked). Under `web-journey`, `roles` + `test_login` are configured.
- **host_ref:** must parse as `owner/repo`; `local_checkout` must be a git worktree whose
  `origin` resolves to `host_ref` (canonical-repo grounding). `python -m agentflow.doctor`
  checks it: an `origin` naming a different `owner/repo` is a FAIL that names both; no
  `origin` at all is a WARN, because a checkout without one runs as a local-only repo
  (no push, no PR); a `git` that cannot answer leaves the line `ok`, marked not verified.
  Only `owner/repo` is printed, never the URL. The comparison is `owner/repo` only: the
  profile has no host field to compare against, so the same `owner/repo` on a different
  host passes.
- **publish_repo (whole block O):** absent ⇒ `None`, and nothing is published — the normal
  case, and the correct encoding of "there is no public repo yet". No default is ever
  synthesised. `host_ref` is R\* when the block is present and takes the same `owner/repo`
  check as `canonical_repo.host_ref`; `cadence` defaults to `manual, en masse`.
  An empty `publish_repo:` block is an error, not an absence — "I wrote the key and
  nothing happened" is the one outcome this key must never produce. An empty `cadence`
  is an error for the same reason.
  **The read-only rule — this is its one home; everything else points here:** no loop
  pushes to `publish_repo`, opens a PR against it, or grounds against it. It carries no `local_checkout` and no
  `base_branch` because there is nothing for a builder to clone, branch from, or merge
  into. **Publishing is a human action, always** — the procedure is
  `docs/runbooks/publish.md`. The resolved-config block prints a `[publish]` line **only**
  when the key is set, and `python -m agentflow.doctor` reports the working/publish split
  (INFO when absent; FAIL when `publish_repo.host_ref` equals `canonical_repo.host_ref`,
  because that split is a fiction). Additive — pre-publish_repo configs are unaffected.
- **Delegated `skill-path` fields** (`staging.deploy_recipe` when a path,
  `generation_skills.*`, `verification.strategy` when a path): presence-checked
  (`Path.exists()` / `os.access(x, X_OK)` for scripts). A missing *required* delegated
  path hard-fails; a missing *optional* one degrades the dependent
  step to its Forced "skip silently" behavior. A delegated path to a shipped skill may
  point under the suite checkout or under the Claude config dir's `skills/<name>/`: provision deploys that copy at step 7, so its pre-deploy
  loads (steps 2 and 5) accept it when the suite holds the same file, and step 10 checks the
  deployed path strictly.
- **audit (whole block O, all fields defaulted):** absent block ⇒ every field its declared
  default (`window=7`, `roles=[]`, `idle_wake_minutes=60`, `notify=true`). `window` and
  `idle_wake_minutes` are ints (bool rejected); `roles` a list of strings; `notify` a bool.
  The `roles` empty-list ⇒ `verification.roles` fallback is a **Forced** auditor-loop
  behavior (the resolver stores the literal list; the fallback is applied by the consumer),
  never a resolver-side rewrite. No field is R\*; a bare `audit:` with no keys is legal and
  resolves entirely to defaults. Additive — pre-audit configs (no block) are unaffected.
- **communities (whole block O):** absent block ⇒ `None` (no override — the derived
  partition stands as-is, which is the normal case). `pins` is an optional mapping of
  repo-relative path prefix → community name, consumed by the autodoc skills as
  `--pin PREFIX=NAME` flags on `graph communities`. Hard-fail only on malformed: a non-mapping
  `pins`, a non-string/empty name, or an empty/root prefix (that would be a catch-all — the
  exact failure mode the derived layer deleted). Unlisted paths always stay derived; a pin
  matching no symbol is reported by the CLI, not a config error. Additive — pre-communities
  configs (no block) are unaffected.
- **autodoc_root (O):** absent or null ⇒ no project override, and `resolve_autodoc_root` falls
  through to `environment.autodoc_root`, then the derived per-project path. The environment root
  is skipped when its `.manifest.json` names a different `project`: the derived path is
  taken if it exists, else `None`, and `AutodocRootError` never fires against the skipped corpus;
  a manifest with no `project` field is not skipped. When set it must name an existing
  directory, else `ConfigError`, and it is never checked against its manifest.
- **vault (whole block O):** `vault.root` is R\* when the block is present and must be an
  existing directory; the four delegated paths are presence-checked as in §1. The block has no
  `subdir` key, so writing one is an unknown-key `ConfigError`; a project with its own `vault:`
  block resolves the subdir to the default `wiki/epics`. Three states, as at the environment
  level: a populated block overrides `environment.vault`; an ABSENT key means nobody was asked;
  an EXPLICITLY EMPTY key (`vault: null`, or `vault:` with no body) records that the answer was
  no (`vault_declined`). A project-level decline is a recorded answer, not an opt-out: while
  `environment.vault` is configured, the project still resolves the environment's root.

---

## §3. Conditional-required rules — consolidated

The three canonical R\* predicates every consumer must honor (called out explicitly per
the P1-T1 acceptance criteria):

1. `notify.url_env` **and** `notify.topic_env` required **iff** `notify.kind != none`.
2. `staging.url` required **iff** `verification.strategy == web-journey`.
3. `name` **==** filename stem (routing integrity — a hard equality gate, not a default).

Additional R\* predicates:

- `vault.root` required iff a `vault:` block exists.
- `publish_repo.host_ref` required iff the `publish_repo:` block exists.
- `parallel_builders.count > 1` requires `parallel_builders.enabled == true`.

---

## §4. Three-tier disposition (Forced / Configured / Delegated)

Every behavior in the suite that is not a plain typed field carries one of three
dispositions:

- **Forced** — a skill opinion baked into procedure prose; kept verbatim, not configurable.
- **Configured** — procedure/prose surfaced as a typed field or prose block.
- **Delegated** — resolved to a presence-checked `SKILL.md` / script path.

These dispositions are the schema's working vocabulary: field tables in §1/§2
annotate prose-valued fields with them (e.g. `deploy_recipe` is Configured prose
or a Delegated SKILL.md path; a missing optional Delegated path degrades its step
to the Forced "skip silently, never fatal" behavior).

---

## §5. Resolver contract (summary — full impl is P1-T2)

- Public API: `agentflow.config.load_environment() -> Environment`,
  `agentflow.config.load_project(name) -> Project`. Deps: PyYAML only.
- Load: resolve `$AGENTFLOW_CONFIG` or `~/.agentflow/config/`; extract the FIRST fenced
  ` ```yaml ` block; `yaml.safe_load`; ignore prose after the block.
- Validate per §1/§2: presence (R vs O), type coercion, enum membership, R\* conditionals,
  path existence, `owner/repo` shape, `name==filename`, env-name presence in `os.environ`,
  delegated `skill-path` presence check.
- **HARD-FAIL loud, never partial:** on the FIRST unmet requirement raise
  `agentflow.config.ConfigError{field_path, file, reason, remediation}`. Never returns a
  config with an unresolved token; never substitutes a default for a required field; never
  proceeds on a `<placeholder>`. Optional gaps resolve to declared default/`None`.
- **Secrets:** reads only env-var NAMES from markdown; values live one-way in
  `settings.local.json`/`.env`. The resolver never writes config and never reads secret
  values from markdown.
## §6. Config WRITE scope - who may edit these files

§1-§5 say what the config MEANS. This says who may CHANGE it, and it is a product rule rather
than one machine's preference: cross-project config clobber is harm any multi-project user can
suffer, not a taste.

**A loop may edit the config surface of the project it is building, and never any other
project's.**

| target | may a loop write it? |
|---|---|
| `projects/<the project it is building>.md` | **yes** |
| `environment.md` | **only when the epic's `## Files` section names it** - it is machine-level and shared by every fleet on the box, so it stays spec-gated |
| `projects/<any other project>.md`, or another project's `data_root` subtree | **never** |

Edits are **additive or field-scoped**. Never delete or repoint a field another project reads.

**Deploying a skill change is not a reason to cross that line.** A loop makes a `SKILL.md`
change live with `python install/provision.py --deploy-only`, which runs the skill-deploy step
alone: it reads the suite checkout, writes only the Claude config dir, and touches no project's
`data_root` subtree. Run it from the primary checkout after the merge lands, never from a
per-epic worktree: deployed skills point into the checkout that ran the deploy, so a worktree
deploy leaves every seat pointing at a directory that is deleted after merge, and the tool
refuses it. A loop never runs a full `provision` to deploy a skill. The full install's
data_root scaffold step, which seeds every configured project's subtree write-if-absent, is the
one documented exception to the "never" row above, and it belongs to whoever installs or
re-provisions the box - not to a loop building one project.

**Why this is not a blanket ban.** The `## Files` section is part of what a human approves at
promotion, and promotion IS the standing approval (`dev-manager` §7c) - so a config edit named
there is already approved, and a blanket ban would contradict the promotion gate rather than
supplement it. It would also leave the suite unable to change its own schema without splitting
each change across two actors (a builder writes the resolver, a human writes the YAML), which is
how the two drift apart - and drift in `environment.md` stops every lane on the box.

**What contains the residual risk**, in order of how much work each does:

1. `load_environment()` hard-fails LOUDLY at Step 0. A broken config stops every lane immediately,
   before any work, naming the exact field.
2. **Unknown-key rejection** (§1, §2) turns a wrong or misspelled field from silently swallowed
   into a loud error naming the key and the file.
3. `doctor` reports the resolved surface, so a misconfiguration is visible without reading YAML.

Every loop `SKILL.md` carries the one-line form of this rule beside its project-selection step;
this section is the full statement they point at.
