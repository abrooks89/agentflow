# environment (harness fixture — missing required key)

Identical to `config-good` with the required `data_root` key REMOVED. Loading this must
raise `ConfigError{field_path: data_root}` so the SessionStart hook (P1-T3) exits
non-zero — the hard-fail-loudly enforcement point. No personal literals.

```yaml
schema_version: 1

person:
  name: Sample User

git_host:
  kind: local

notify:
  kind: none

lock_dir: "~"
shell: posix
suite_root: "~"
worktree_root: "~"
```
