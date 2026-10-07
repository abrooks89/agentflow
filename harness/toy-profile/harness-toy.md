# projects/harness-toy.md — the harness's toy project (TEMPLATE)

The project the live harness scenarios (A, B and D in `harness/run-scenario.md`) name with
`--project harness-toy`, so a recording never fires a builder on a real fleet project. Its
only repository is the seeded `harness/toy-repo` fixture.

Install it per `harness/run-scenario.md`, *Setup: the harness-toy project*: copy this file to
`<config_home>/projects/harness-toy.md` (`<config_home>` is `$AGENTFLOW_CONFIG`, else
`~/.agentflow/config`) and replace `<agentflow_clone>` with the absolute path of this clone.
Left unreplaced, `canonical_repo.local_checkout` names no directory and the resolver fails
loudly on it. The schema is `skills/_shared/SCHEMA.md` §2; the resolver reads the first fenced
YAML block below and ignores this prose.

```yaml
schema_version: 1
name: "harness-toy"

canonical_repo:
  host_ref: "example/harness-toy"
  local_checkout: "<agentflow_clone>/harness/toy-repo"
  base_branch: "main"

verification:
  strategy: test-suite

work_source:
  kind: none
```

## Notes (human-only, ignored by the resolver)

- **`host_ref` names no real repository.** The toy repo's `origin` is a local bare repo the
  seed builds at `harness/toy-origin/example/harness-toy.git` (gitignored), reached by a
  `file://` URL. Its path ends in `example/harness-toy`, so the orchestrator's Step 1.1
  identity check and `agentflow.doctor` both read it as `host_ref`, with no network traffic.
  A push lands in that bare repo and nowhere else; opening a pull request against
  `example/harness-toy` fails rather than reaching a repository somebody owns.
- **`work_source.kind: none`** keeps every tracker read off: the toy project has no issues.
- **`data_root` is not a profile key.** It is machine-level (`environment.md`), and the
  resolver rejects it here as an unknown key. A recording therefore writes under the
  machine's real `data_root`, inside `<data_root>/projects/harness-toy/` when the project
  it is given is `harness-toy`, a subtree no other project reads; builder worktrees land
  under `<worktree_root>/harness-toy/`. Scenario B's plan declares `project: harness-toy`, so its
  board, wave scripts and worktree land in the same subtrees; the seed creates that worktree
  (`<worktree_root>/harness-toy/toy-calc-single-task`) because the orchestrator never does,
  and the runbook says how to remove everything afterwards.
