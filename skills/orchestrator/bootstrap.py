#!/usr/bin/env python3
"""orchestrator bootstrap — install PreToolUse auto-allow hooks.

Why this exists
---------------
`orchestrator` dispatches general-purpose subagents (wave subagents,
spec/code-quality reviewers, fix subagents), and an unattended run stops at
every permission prompt one of their Bash, Edit or Write calls raises.

The fix is to install PreToolUse hooks that return
`permissionDecision: "allow"` for the tool patterns subagents actually use.
Per the Claude Code hooks reference (https://code.claude.com/docs/en/hooks,
read 2026-09-24), hooks from settings files also run inside subagents, and a
PreToolUse `"allow"` skips the permission prompt. With the hooks installed,
calls that match them run without a prompt.

Behavior
--------
- Idempotent — safe to re-run.
- Adds Bash patterns covering common subagent tool calls (git, go, python,
  pytest, ls, mkdir, cat, etc.). Deployment-specific patterns (e.g. ssh to
  your own hosts) come from the AGENTFLOW_EXTRA_AUTO_ALLOW env var
  (comma-separated 'Bash(...)' patterns) — never hardcoded.
- Each Bash entry is a prefix glob over the command. `Bash(git *)` matches
  every git command, `git push --force` and `git reset --hard` included.
  Several entries launch arbitrary code, among them `python`, `python3`,
  `node`, `npm run`, `make`, `cargo`, `go` (`go run`), `yarn`, `pytest`
  (conftest), `awk` (`system()`), `sed` (GNU `e`), `xargs`, `env` and `find`
  (`-exec`, `-delete`).
  The hooks reference says an `if` pattern is checked against each
  subcommand of a compound command and inside `$()`, so one matching
  subcommand is enough for the hook to run, and its `allow` covers the
  whole call.
- A command none of whose subcommands matches any entry (e.g. a bare
  `rm -rf x`, `dolt sql`) is not auto-allowed by these hooks; whether it
  then prompts depends on the session's permission mode.
- An unmatched command chained after or inside a matched one
  (`cd x && rm -rf y`, `echo $(rm -rf x)`) is allowed along with it.
- Adds Edit / Write / Agent matchers with blanket auto-allow; those three
  tools are allowed for every path and every caller.
- Appends after any existing PreToolUse entries and never removes or
  reorders them, so an existing "ask" hook (e.g. for pip or npm installs)
  stays in place. Per the hooks reference, when several PreToolUse hooks
  disagree the precedence is deny > defer > ask > allow, and deny and ask
  permission rules are still evaluated whatever the hook returns.

The user-facing statement of all this is <suite_root>/docs/INSTALL.md, *What the install
changes on your machine*. provision.py runs this script as its step 8 unless
it is given `--no-auto-allow`.

Usage
-----
    python <suite_root>/skills/orchestrator/bootstrap.py

Optional flags:
    --dry-run        — show what would change, don't write
    --print-report   — show the final hook list

Settings file location: `~/.claude/settings.local.json`. The script targets
that path; if you use a different settings file, set
`CLAUDE_SETTINGS_FILE=<path>` in the environment before running.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ALLOW_CMD = (
    'echo \'{"hookSpecificOutput":'
    '{"hookEventName":"PreToolUse","permissionDecision":"allow"}}\''
)

# Bash patterns auto-allowed for subagent tool calls. Covers what the
# orchestrator's wave subagents + reviewers actually invoke.
# These are prefix globs, so they reach further than the names suggest:
# `Bash(git *)` matches destructive git too (see the module docstring).
# A command none of whose subcommands matches an entry (e.g. a bare `rm -rf x`,
# `dolt sql`) is not auto-allowed by these hooks. Chained after a matched
# subcommand, the same command runs with it (see the module docstring).
#
# `Bash(git *)` is a glob and already matches the explicit-path form every skill
# and prompt in this suite now emits (`git -C <worktree> log`, ...). Do NOT narrow
# it to specific subcommands while sweeping for explicit paths: the wildcard sits
# after `git`, so a narrower pattern would stop matching `-C` invocations and every
# dispatched subagent would start blocking on permission prompts mid-wave.
# ---------------------------------------------------------------------------
# Hook coverage audit — 2026-05-09 (T16)
#
# Tools wave subagents actually invoke, and whether they are covered:
#
#   Bash          — covered via BASH_AUTO_ALLOW_PATTERNS (pattern-gated)
#   Edit          — covered via BLANKET_ALLOW_TOOLS
#   Write         — covered via BLANKET_ALLOW_TOOLS
#   Agent (Task)  — covered via BLANKET_ALLOW_TOOLS
#   Read          — NOT in lists. PreToolUse does fire for Read (the hooks
#                   reference lists Read, Glob and Grep among the tools it
#                   matches); this audit observed no Read prompts in subagents,
#                   so no hook was added. Not re-checked since 2026-05-09.
#   Grep          — NOT in lists. Same as Read.
#   Glob          — NOT in lists. Same as Read.
#   TodoWrite     — NOT in lists. Used by orchestrator itself (not wave subagents).
#                   If you see TodoWrite re-prompts in wave subagents, add it to
#                   BLANKET_ALLOW_TOOLS.
#
# TODO: If 'Read', 'Grep', or 'Glob' start triggering permission prompts in
# subagent contexts (visible as pauses in unattended runs), add them to
# BLANKET_ALLOW_TOOLS. The PreToolUse hook works for any tool name, not just Bash.
# ---------------------------------------------------------------------------
BASH_AUTO_ALLOW_PATTERNS = [
    'Bash(git *)',
    'Bash(go *)',
    'Bash(python *)',
    'Bash(python3 *)',
    'Bash(pytest *)',
    'Bash(ruff *)',
    'Bash(node *)',
    'Bash(npm test*)',
    'Bash(npm run *)',
    'Bash(yarn *)',
    'Bash(make *)',
    'Bash(cargo *)',
    'Bash(scp *)',
    'Bash(ls *)',
    'Bash(mkdir *)',
    'Bash(cat *)',
    'Bash(echo *)',
    'Bash(grep *)',
    'Bash(rg *)',
    'Bash(find *)',
    'Bash(wc *)',
    'Bash(head *)',
    'Bash(tail *)',
    'Bash(cd *)',
    'Bash(export *)',
    'Bash(curl -s *)',
    'Bash(curl -fsS *)',
    'Bash(jq *)',
    'Bash(sha256sum *)',
    'Bash(touch *)',
    'Bash(cp *)',
    'Bash(mv *)',
    'Bash(diff *)',
    'Bash(date *)',
    'Bash(which *)',
    'Bash(stat *)',
    'Bash(env *)',
    'Bash(pwd*)',
    'Bash(chmod *)',
    'Bash(awk *)',
    'Bash(sed *)',
    'Bash(sort *)',
    'Bash(uniq *)',
    'Bash(xargs *)',
]

BLANKET_ALLOW_TOOLS = ['Edit', 'Write', 'Agent']


def extra_patterns_from_env() -> list[str]:
    """Deployment-specific additions to the Bash auto-allow list.

    Set AGENTFLOW_EXTRA_AUTO_ALLOW to a comma-separated list of hook
    patterns (e.g. 'Bash(ssh mybox*),Bash(docker *)') to auto-allow
    infrastructure commands specific to your environment — host-specific
    ssh targets are deliberately NOT hardcoded here.
    """
    raw = os.environ.get('AGENTFLOW_EXTRA_AUTO_ALLOW', '')
    out: list[str] = []
    for chunk in raw.split(','):
        pat = chunk.strip()
        if not pat:
            continue
        if not (pat.startswith('Bash(') and pat.endswith(')')):
            raise SystemExit(
                f"AGENTFLOW_EXTRA_AUTO_ALLOW entry {pat!r} is not a "
                "'Bash(...)' hook pattern"
            )
        out.append(pat)
    return out


def settings_path() -> Path:
    override = os.environ.get('CLAUDE_SETTINGS_FILE')
    if override:
        return Path(override)
    return Path.home() / '.claude' / 'settings.local.json'


def load_settings(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f'Settings file not found: {path}')
    return json.loads(path.read_text(encoding='utf-8'))


def add_hooks(settings: dict) -> tuple[dict, int]:
    """Mutate settings in-place to add auto-allow hooks. Return (settings, n_added)."""
    hooks_section = settings.setdefault('hooks', {})
    pretool = hooks_section.setdefault('PreToolUse', [])

    n_added = 0

    # Bash matcher block — find or create
    bash_block = next((h for h in pretool if h.get('matcher') == 'Bash'), None)
    if bash_block is None:
        bash_block = {'matcher': 'Bash', 'hooks': []}
        pretool.append(bash_block)

    existing_ifs = {h.get('if') for h in bash_block['hooks']}

    for pat in BASH_AUTO_ALLOW_PATTERNS + extra_patterns_from_env():
        if pat not in existing_ifs:
            bash_block['hooks'].append({
                'type': 'command',
                'command': ALLOW_CMD,
                'if': pat,
                'timeout': 5,
                'statusMessage': f'auto-allow: {pat}',
            })
            n_added += 1

    # Blanket matcher blocks for Edit / Write / Agent
    for tool in BLANKET_ALLOW_TOOLS:
        block = next((h for h in pretool if h.get('matcher') == tool), None)
        if block is None:
            pretool.append({
                'matcher': tool,
                'hooks': [{
                    'type': 'command',
                    'command': ALLOW_CMD,
                    'timeout': 5,
                    'statusMessage': f'auto-allow: {tool}',
                }],
            })
            n_added += 1
        # If a block already exists, leave it alone — user may have customized.

    return settings, n_added


def print_report(settings: dict) -> None:
    pretool = settings.get('hooks', {}).get('PreToolUse', [])
    print('PreToolUse matchers in settings:')
    for block in pretool:
        matcher = block.get('matcher', '<no matcher>')
        n = len(block.get('hooks', []))
        print(f'  - {matcher}: {n} hooks')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--dry-run', action='store_true',
                        help="Show what would change, don't write")
    parser.add_argument('--print-report', action='store_true',
                        help='Show final hook list after applying')
    args = parser.parse_args()

    path = settings_path()
    print(f'Settings file: {path}')

    try:
        settings = load_settings(path)
    except FileNotFoundError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        print('Run Claude Code at least once to create the settings file before running bootstrap.',
              file=sys.stderr)
        return 1

    settings, n_added = add_hooks(settings)
    print(f'{"Would add" if args.dry_run else "Added"} {n_added} new auto-allow hooks.')

    if not args.dry_run:
        path.write_text(json.dumps(settings, indent=2), encoding='utf-8')
        print(f'Wrote {path}.')

    if args.print_report or args.dry_run:
        print_report(settings)

    return 0


if __name__ == '__main__':
    sys.exit(main())
