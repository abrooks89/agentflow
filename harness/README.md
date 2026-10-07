# agentflow acceptance harness

Deterministic checks on how the agentflow skills and their shared package
behave. On any clone, after `pip install -e skills/_shared`, two commands prove
something today:

- `python harness/check.py --self-test` — the checker is wired: config loads,
  `parse_plan` imports, every assertion runs crash-free against a synthesized
  records-absent world, and the auditor ship gate is green.
- `python harness/check.py harness/harness.config.example.json --only C,F,G` —
  scenarios C, F and G assert end to end from tracked fixtures alone.

CI runs both, alongside the toy-repo rebuild and `assert_doc_counts.py` (see
*Fresh clone: what runs* below). They check a *behaviour* layer; the install-health
check is `python -m agentflow.doctor --project <name>` (see
[`docs/INSTALL.md`](../docs/INSTALL.md) §9).

Scenarios A, B and D are **property assertions over a fresh recording**:
`check.py` reads whatever a live session last wrote to `records/` and
`stubs/schedulewakeup_log.md` and checks properties of it. It never compares
against a stored baseline. So proving that a change preserved behaviour means
re-recording A, B and D (per [`run-scenario.md`](run-scenario.md)) and
re-running `check.py <config>` over the new recording. Nothing in here modifies
a live skill.

*History:* the harness was built as behaviour-frozen scaffolding for the
Phase-0 port of the skills into the `agentflow` package. The port is done; the
pre-port import path is gone, and so is the `resolver_cmd` config key that
selected it (a local config that still sets it gets a WARN and the key is
ignored).

## Layout

- `check.py` — the deterministic acceptance checker (44 wired assertions,
  A1-A9 / B1-B9 / C1-C15 / D1-D4 / E1-E2 / F1-F4 / G1). Reads everything by indirection from
  `harness.config.json`.
- `harness.config.example.json` — **tracked** example config. Copy to
  `harness.config.json` (gitignored, machine-local) and replace the
  placeholder paths.
- `fixtures/` — tracked, deterministic inputs: canned plans, registry seeds,
  the `toy-repo-seed/` files.
- `toy-repo/` — a self-contained fixture git repo (its own `.git`) used as the
  controlled target for orchestrator/skill runs. Gitignored from the outer
  repo; reconstruct it any time with `python harness/seed_toy_repo.py` (see
  below).
- `toy-profile/harness-toy.md` — **tracked** project-profile template for
  `harness-toy`, the non-fleet project every live A/B/D launch names with
  `--project harness-toy`. Its only repository is `toy-repo/`. Installed into
  `<config_home>/projects/` per [`run-scenario.md`](run-scenario.md), *Setup*, and
  removed after recording.
- `records/` — captured run records (gitignored, machine-local).
- `stubs/` — the hand-written wake log `schedulewakeup_log.md` (gitignored) and its
  format README.
- `BASELINE.md` / `POST-PORT-RESULT.md` — gitignored names left from the
  Phase-0 port, where hand-written pre/post-port notes lived. Nothing reads
  them: `check.py` never compares against a baseline.
- `assert_doc_counts.py` — fails when a tracked doc states a wired-assertion
  count or ID range that differs from `check.py`'s registry. CI runs it.
- `uc-scenario/`, `auditor-scenario/` — self-checking scenario drivers with
  their own READMEs.
- `fresh-clone-install-drill/` — on-demand drill (own README) that clones the
  repo into a throwaway tree and runs `install/provision.py` end to end against
  a throwaway config home, data root and Claude root, then re-runs the two
  fresh-clone `check.py` rows below from that clone. One limb is **`gh#186`'s
  regression guard**: a green there means the fix holds, and a return of the old
  `ModuleNotFoundError` fails the drill naming that issue.
- `missed-wake-drill/` — on-demand drill (own README): builds a board where one
  lane's declared `eta` has lapsed, and requires the steward's detector, its
  routed-Note alert leg and the out-of-process `agentflow.wake_watch` watcher to
  name the stalled lane and not the control, including when the stalled row is
  the steward's own. Writes its report to a temp directory by default, so a bare
  run never touches the live board.
- `launch-dir-drill/` — no script: a hand-written, committed record
  (`launch-dir-drill.md`) of one builder lane, launched from the machine's launch
  directory, claiming and building a real queue row end to end and confirming
  from inside that same session that its memory was present. It needs a live
  fleet session, so it cannot run on a fresh clone; its README says how to re-run
  each observation.

## Fresh clone: what runs

A fresh clone has **no recordings and no machine-local config** — by design.
Here is exactly what runs out of the box, and why the rest doesn't.

Prerequisite (one line, from the repo root):

```
pip install -e skills/_shared
```

(`check.py` imports `parse_plan` from the installed `agentflow` package.)

| Command | Fresh clone? | What it proves |
|---|---|---|
| `python harness/check.py --self-test` | **yes** | config loads, `parse_plan` imports, all the full assertion registry wired and crash-free against a synthesized records-absent world (gh#302) and, when `records/` or the wake log is on disk, crash-free against those recordings too, verdicts not graded (step 4d, gh#341) - and that every record-backed row (A1-A9, B1-B9, C4, D1-D4, E2) SKIPs as unrecorded there rather than FAILing (gh#628). Prints `# tree:` lines naming the file `agentflow.plan_parser` was imported from and the registry CLI root, and a `WARN self-test.tree` line if the imported package is not under this clone's `skills/_shared` while the registry CLI is (fix: `pip install -e skills/_shared` from this clone). Always grades against the tracked `harness.config.example.json` and prints `# config:` naming it; a machine-local `harness.config.json` is not read, so the local config does not move the verdict (gh#340) - `check.py <config>` exercises yours, and `--self-test <config>` is refused rather than silently ignored. Step 4d is the one deliberately machine-dependent part: it can FAIL only on a machine that holds recordings, and SKIPs everywhere else, CI included. |
| `python harness/check.py harness/harness.config.example.json --only C,F,G` | **yes** | Scenarios F and G in full (F1-F4, G1: an issue-intake triage pass replayed over tracked fixtures through `issue_intake`'s injectable `Runner`, no account and no network - see *Scenarios F and G* below), and Scenario C end-to-end (loud hard-fail on a malformed plan, that the failing parse creates no status board, plus C14's complement: a plan without `epic` parses) — C1-C3, C5, C6 and C8-C15 assert here from tracked fixtures alone, never a live recording. The other two rows cannot run here and report SKIP rather than passing on an input they never examined (gh#139): C4 (no wake scheduled), whose only evidence is `stubs/schedulewakeup_log.md`, which only a live recording of B or D writes (A schedules no wake) - it prints a `# unrecorded:` line marked outside `--only`, and does not affect the exit code, because neither of those scenarios was selected; and C7 (no server-IP literal), whose needle list `preport_ip_literals` the tracked example config ships empty on purpose — C7 needs a machine-local config key rather than a recording, but it does not assert on this command either, so it is excluded from the range above. Exits 0. |
| `python harness/seed_toy_repo.py` then `( cd harness/toy-repo && python -m pytest tests -q )` | **yes** | the toy-repo fixture reconstructs deterministically (same seed sha on every machine) and its suite is green. |
| `python harness/check.py --auditor-scenario` | **yes** | the auditor loop's deterministic machinery against canned subagent verdicts in a temp `data_root`: ledger rows, one drafted gap spec reusing the original UC-ID, a dev-manager blocker, writes confined to `design/specs/` + `status/`, all coordination through the shipped `agentflow.registry` CLI, and a second pass that is a no-op - graded against the pinned `AUDITOR_EXPECTED_IDS` set. Reads no machine-local config and no records. The same rows also run as step 6 of `--self-test`. See [`auditor-scenario/README.md`](auditor-scenario/README.md). |
| `python harness/uc-scenario/uc_scenario.py` | **yes** (not a CI row) | the use-case traceability spine end to end: one minted `UC-NNNN` found by a single grep across refine, solution, commit, code and flow page, and a code removal plus reconcile flipping that use case to `deprecated` without touching the plan. Rebuilds throwaway fixtures under `uc-scenario/_scratch/` (gitignored) every run and reads no machine-local state. Nothing in CI runs it. See [`uc-scenario/README.md`](uc-scenario/README.md). |
| `python harness/check.py <config>` (full suite, including A/B/D) | **no** (runs, exits 3) | needs `records/` + `stubs/schedulewakeup_log.md` from a live recording session — see below. Without them every A/B/D row and E2 (and C4) SKIPs as unrecorded, never FAILs (gh#628); a `# unrecorded:` line names each missing artifact and the run exits 3, so a fresh clone reads neither green nor as a regression. |
| `python harness/fresh-clone-install-drill/fresh_clone_install_drill.py` | **on demand** | that this table itself still holds: clones the repo into a throwaway tree, provisions into a throwaway config home / data root / Claude root, checks the artifacts of install steps 7, 8, 9 and 11 on disk, and re-runs `check.py --self-test` plus `--only C,F,G` from that clone. It clones over the network and takes minutes, so it is deliberately not a CI row — its logic is mutation-tested offline instead by `skills/_shared/tests/test_fresh_clone_install_drill.py`. One limb covers `gh#186`, inverted when that was fixed: it now requires the PyYAML-less run to complete cleanly, and fails naming the issue if the old `ModuleNotFoundError` returns. Read [`fresh-clone-install-drill/README.md`](fresh-clone-install-drill/README.md) before citing a green — the issue's second finding (split out as `gh#322`, and fixed) is covered by `install/test_provision_fresh_machine.py`, not by that limb. |

CI runs the `--self-test` row (which carries the `--auditor-scenario` rows as its step 6), the
`--only C,F,G` row and the toy-repo row, plus `harness/assert_doc_counts.py`
(`.github/workflows/ci.yml`, `harness-self-test` job). The uc-scenario row runs on a fresh
clone but is not a CI step. On a private
repo that job runs on every PR, the nightly and a dispatch, not on a master push.

### Why the recordings are machine-local

Scenarios A, B and D capture how a **live Claude session** drives the real
skills (plan emission, a full wave with commits and reviews, a builder
wake-continuation). Those records embed personal absolute paths, worktree
locations and infra hostnames from the machine that recorded them, so they are
gitignored rather than shipped. They are *evidence of a run*, not test inputs —
the tracked fixtures in `fixtures/` are the inputs.

### Recording the missing scenarios

The complete operator runbook is [`run-scenario.md`](run-scenario.md). Before A, B or
D, install the `harness-toy` profile per its *Setup* section: every live launch passes
`--project harness-toy`, because a launch without it resolves the session default, a
real fleet project. Summary of what each scenario produces:

- **Scenario A** — invoke the `solution_skill` on `fixtures/refine-toy.md`;
  record the emitted plan to `records/scenarioA/plan.md` (assertions A1-A9).
- **Scenario B** — invoke the `orchestrator_skill` on
  `fixtures/plan-single-task.md` against a freshly seeded `toy-repo`; record
  `records/scenarioB/status.md` + `records/scenarioB/wave-evidence.json`, one
  wake row in `stubs/schedulewakeup_log.md` (B1-B9, E1-E2).
- **Scenario C** — nothing to record: C1-C3 run against tracked fixtures, C3 by
  snapshotting the fixtures tree around the in-process parse and diffing, so it
  observes what that parse creates rather than reporting an absent directory.
  C4 is the exception — it reads the wake log, so it SKIPs until *any* scenario
  that schedules a wake (B or D - A schedules none) has been recorded and populated
  `stubs/schedulewakeup_log.md`.
- **Scenario D** — fire the `builder_entry` with a target; record the verbatim
  continuation prompt as a wake row, plus
  `records/scenarioD/registry-after.md` for the consume-first check (D1-D4).
- **Scenarios F and G** — nothing to record; fully covered by tracked
  fixtures, like C. They replay a triage pass over
  `fixtures/scenarioF-issues-before.json` through `issue_intake`'s injectable
  `Runner`, with no account and no network (F1-F4: `untriaged()` draws the
  visit set, the bug ends labelled and uncarved, the epic-sized one ends
  carved at `drafted`; G1: `builder_selectable()` returns the `triaged`
  non-`epic` seed alone). They previously required a live interactive pass and
  read `records/`, which is gitignored — so both were permanently red on every
  fresh clone. What they do NOT cover, and no CI run could, is the bug-vs-epic
  judgement itself; see run-scenario.md.

## Configuring `harness.config.json`

Copy `harness.config.example.json` to `harness.config.json` and edit:

- `specs_dir` / `plans_dir` — YOUR spec + plan directories (used by live
  recording sessions, not read by any assertion).
- `status_roots` — the same directories, but this key **is** read by an
  assertion: C3 probes `<root>/<slug>/status.md` and `<root>/<slug>.status.md`
  for the malformed fixture's slug, so a status board sitting there turns C3
  red. Point it at your real spec/plan tree only if you want that tree watched.
- `solution_skill` / `orchestrator_skill` / `builder_entry` — the skill and
  command names the scenarios drive (defaults match this repo).
- `resolver_cmd` — **retired** (gh#627). It used to choose between the pre-port
  and post-port import of `parse_plan`; the import is now always the installed
  `agentflow` package. An old local config that still sets it keeps working:
  `check.py` prints a `# WARN config:` line naming the key and ignores it.
  Delete it from your copy. If it is set to `null`, the config dates from
  pre-port recording and its `skills_root` likely points at the unported skills
  tree, where C6 and C7 FAIL: retire that config and regenerate it from
  `harness.config.example.json` rather than editing it.
- `skills_root` — where the skills live. May be relative; resolves against
  the config file's directory (`"../skills"` = this repo's `skills/`). When
  neither this key nor `AGENTFLOW_SKILLS_ROOT` is set it defaults to
  `~/.claude/skills`, the deployed install, which never carries `_shared`; the
  executed-CLI checks (C11-C13, C15) therefore run the `agentflow` package from
  this clone's `skills/_shared` first and fall back to `<skills_root>/_shared`,
  and FAIL naming both paths if neither has it (gh#944).
- `preport_ip_literals` — infra literals the C7 grep hunts for; keep personal
  values out of tracked files by putting them only in your local config. The
  tracked example ships this empty, so C7 reports SKIP there rather than
  passing over a needle list it does not have (gh#139); set it locally to make
  C7 assert. The whole-tree sanitize gate in `.github/workflows/ci.yml` covers
  this class only when the `SANITIZE_EXTRA_PATTERN` repository secret carries
  install-specific alternatives for it; the publish cut applies the same
  alternatives from the local extra pattern file. The gate's shipped pattern
  covers only home-directory paths and the mangled session-directory name.
- `toy_project` — the project the live A/B/D launch lines pass as `--project`
  (`harness-toy`). No assertion reads it; a test pins it equal to the shipped
  profile's `name` and to the runbook's launch lines, so leave it as shipped.

If `check.py` is run without a config it prints these instructions and exits 2.

### Exit codes (`check.py <config>`)

| Exit | Meaning |
|---|---|
| 0 | every selected assertion PASSed or honestly SKIPped |
| 1 | at least one **behavioral** assertion FAILed - a recording (or fixture) that exists and is wrong. Structural-only FAILs warn and do not set it. Takes precedence over 3. |
| 2 | usage error - argparse, an unknown `--only` group, a config file that is missing, not readable JSON, not a JSON object, or missing a required key - with the reason on stderr |
| 3 | no behavioral FAIL, but a scenario selected by `--only` (all of them, without it) was never recorded on this machine. Its rows SKIP as unrecorded and a `# unrecorded: scenario <X> - <artifact> (<ids>)` line names what to record. An unrecorded artifact owned by a scenario outside `--only` is still listed, marked as not holding the exit - that is why `--only C,F,G` exits 0 while C4 SKIPs - and the `# EXIT 3` line names only selected scenarios. |

A missing recording is always SKIP and a present-but-wrong one always FAIL (gh#628),
so exit 1 means a regression and exit 3 means a debt. One nuance: scenario B counts as
recorded once `records/scenarioB/status.md` exists, so a status board with no
`wave-evidence.json` or no scenario=B wake row is a FAIL (a broken recording), not a
SKIP. Scenario D likewise counts as recorded once `records/scenarioD/registry-after.md`
exists, so D1-D3 FAIL when it is present and no scenario=D wake row was logged.

## toy-repo seed

`toy-repo/` is reconstructed from the tracked `fixtures/toy-repo-seed/` files
by `seed_toy_repo.py`. The seed commit is deterministic (LF-normalized
contents, fixed author/committer/date):

- Seed HEAD sha: `1292c9ae2ff279c45ea0a026785c9f075bc1fe6b`
- Commit message: `Seed toy-repo calculator fixture`

Reset the fixture to a clean state at any time with:

```
git -C harness/toy-repo reset --hard 1292c9ae2ff279c45ea0a026785c9f075bc1fe6b && git -C harness/toy-repo clean -fd
```

or wipe and reseed with `python harness/seed_toy_repo.py --force`. If you
change a seed fixture, update `EXPECTED_SEED_SHA` in `seed_toy_repo.py` and
the sha above in the same commit — the script fails loudly on drift.

The seed also gives Scenario B what the orchestrator's Step 1.1 pre-flights and never
creates (gh#1105): an `origin` that is a local bare repo at
`harness/toy-origin/example/harness-toy.git` (gitignored, `file://` URL, so it matches the
toy profile's `host_ref` with no network traffic), and the epic worktree
`<worktree_root>/harness-toy/toy-calc-single-task`. `<worktree_root>` is `--worktree-root
<dir>` or `environment.md`'s; with neither, as in CI or on a machine with no `environment.md`, the worktree is
skipped with a note on stderr. Neither adds a commit, so the seed sha above is unchanged;
`--force` removes and prunes the worktree before reseeding.

## Running the fixture tests

```
( cd harness/toy-repo && python -m pytest tests -q )
```

The subshell is deliberate. The suite imports `calc` from the fixture root, which is on
`sys.path` only when pytest runs from there, so an explicit-path run from the repo root
fails to collect. In a POSIX shell the parentheses restore the parent shell's directory on
exit; a bare `cd` would leave the shell inside the fixture.
