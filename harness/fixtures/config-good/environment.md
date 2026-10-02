# environment (harness fixture — valid)

Config-home fixture consumed by the SessionStart hook test (P1-T3). `AGENTFLOW_CONFIG`
points at this directory; the resolver reads the first fenced yaml block below.
Paths use `~` so the fixture validates on any machine (home always exists). No personal
literals — this file ships in the public tree.

```yaml
schema_version: 1

person:
  name: Sample User

data_root: "~"

git_host:
  kind: local

notify:
  kind: none

lock_dir: "~"
shell: posix
suite_root: "~"
worktree_root: "~"
```

Prose after the block is ignored by the resolver.
