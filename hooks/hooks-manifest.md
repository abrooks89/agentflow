# agentflow hooks manifest

Hooks are the enforcement points that agentflow relies on the harness to run — as
opposed to instructions a skill must remember to follow. Per spec §4: *a hook, not an
instruction*. Each entry below is registered as a Claude Code hook in
`~/.claude/settings.local.json` — the file `install/provision.py` actually writes (override with
`--settings-file <path>` or `$CLAUDE_SETTINGS_FILE`).

**That file is project-local to your home directory, not user-global.** `settings.local.json` is
the *project* settings file for a session whose project root happens to be `~`; the user-global
file is `settings.json` beside it. So these hooks fire in sessions started from your home
directory and **not** in a session started anywhere else — there is no upward search. That is the
reason the fleet fixes a single launch directory rather than treating it as a convention: a lane
opened inside a project checkout loads none of this and fails at step 0.

## `sessionstart-inject-config.py`

| | |
| --- | --- |
| **Event** | `SessionStart` |
| **Runs** | once, at session open, before any skill runs |
| **Reads** | `$AGENTFLOW_CONFIG` (config home; default `~/.agentflow/config/`), `$AGENTFLOW_PROJECT` (active project; else the single `projects/<name>.md` default) |
| **Depends on** | the installed `agentflow.config` resolver — `load_environment`, `load_project`, `ConfigError`, plus `resolve_project_name()` and `render_resolved_block()` |
| **Exit 0** | prints a compact resolved-config block to **STDOUT** — paths, git host, notify NAMES, verification strategy, active builders — so every downstream skill has resolved values without reading config itself |
| **Exit non-zero** | on `ConfigError`, prints the full error (file / field path / reason / remediation) to **STDERR**. This is the **hard-fail-loudly** enforcement point: a broken config surfaces loudly at session open instead of silently mid-run |

### Why a hook

This replaces every "read the config at step 0" instruction scattered across the
skills. A skill can forget to read config; the harness cannot forget to run a
registered `SessionStart` hook. Centralizing resolution here also means config is
validated exactly once per session, and a validation failure blocks the session at
open rather than corrupting work partway through.

### Where the logic lives

The hook owns **no** resolution logic. Config home (`config_home()`), active-project
resolution (`resolve_project_name(explicit=None)`) and block rendering
(`render_resolved_block(env, proj)`) are all public in `agentflow.config`; the hook
imports them. A second copy in the hook is exactly how the session-open block and
`python -m agentflow.config show --project <name>` would drift apart — one resolver,
one renderer, two callers.

The hook calls `resolve_project_name()` with **no** explicit name: it injects the
session **default** (`$AGENTFLOW_PROJECT`, else the single `projects/<name>.md`). A
`--project <name>` argument on a slash command outranks that default, and is applied
downstream by the skill — not here. The hook's own resolution order and failure
semantics are unchanged.

### Secrets

The resolved block prints only env-var **NAMES** for notify (e.g. `url_env`,
`topic_env`) — never a secret value. The resolver itself only ever reads env-var
names, never their values.

### Registration

`install/provision.py` registers this hook automatically (its SessionStart-hook step),
deriving the command from the **resolved `suite_root`** — never a hand-written absolute
path. The registered command runs the **suite venv interpreter** (bare `python` lacks the
`agentflow` package on a fresh machine), with both paths quoted so spaces survive:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "\"<suite_root>/.venv/bin/python\" \"<suite_root>/hooks/sessionstart-inject-config.py\"" }
        ]
      }
    ]
  }
}
```

(`<suite_root>/.venv/Scripts/python.exe` on Windows.) To register by hand, put that block in
`~/.claude/settings.local.json` and substitute your resolved suite root — the directory
containing this repo's checkout — into both paths; `provision.py` does the same thing
idempotently, into that same file unless `--settings-file <path>` names another.

### Verification

```
AGENTFLOW_CONFIG=<config-good>        python hooks/sessionstart-inject-config.py   # exit 0  + block on stdout
AGENTFLOW_CONFIG=<config-missing-key> python hooks/sessionstart-inject-config.py   # exit !=0 + field/remediation on stderr
AGENTFLOW_PROJECT=<no-such-profile>   python hooks/sessionstart-inject-config.py   # exit 1  + stderr: the error first, then the headline, then the full error
```

Drift check — the hook's block and the CLI's block must be byte-identical for the
same project, or the shared resolver has been forked:

```
AGENTFLOW_PROJECT=<name> python hooks/sessionstart-inject-config.py > /tmp/hook.out
python -m agentflow.config show --project <name> | diff - /tmp/hook.out            # no output
```
