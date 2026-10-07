#!/usr/bin/env python
"""SessionStart config-injection hook (agentflow).

Runs once at session open. Resolves the typed-YAML config via ``agentflow.config``
and injects a compact resolved-config block so every downstream skill has resolved
values WITHOUT re-reading config itself. This replaces any "read config at step 0"
instruction with a hook (spec §4: a hook, not an instruction).

Contract:
  * Success -> compact resolved-config block (paths, git host, notify NAMES,
    verification strategy, active builders) to STDOUT, exit 0. When the resolved
    project's repo carries a ``CLAUDE.md``, that file is appended as a second
    block. A lane launched from the machine's launch directory sits in no repo, so
    cwd traversal finds no repo instructions; injecting them here binds a lane to
    the rules of the PROJECT IT RESOLVED, not of the folder it was opened in. A
    project with no such file is a normal case: nothing extra is emitted, exit 0.
  * ConfigError -> full error (field / file / remediation) to STDERR, exit
    NON-ZERO. This is the hard-fail-loudly enforcement point: a config failure is
    loud and visible at session open, before any skill runs. An unreadable project
    ``CLAUDE.md`` is such a failure — running a lane unbound by rules it is
    supposed to load is worse than not starting it.

Config home resolves from ``$AGENTFLOW_CONFIG`` (or ``~/.agentflow/config/``).
Active project resolves from ``$AGENTFLOW_PROJECT``; absent that, a single project
file under ``projects/`` is the default. Environment-only config (no project) is
valid — the project fields simply read "(no project)".

This hook OWNS no resolution logic of its own. Name resolution and block rendering
live in ``agentflow.config`` (``resolve_project_name`` / ``render_resolved_block`` /
``render_project_claude_md``) and are imported here, so the session-open block and
``python -m agentflow.config show --project <name>`` can never drift apart. The hook
passes no explicit project — it is the session DEFAULT; a ``--project <name>``
argument on a slash command overrides it later, downstream.

Secrets: prints only env-var NAMES for notify, never a secret value.
"""

from __future__ import annotations

import sys

try:
    from agentflow.config import (
        ConfigError,
        failure_report,
        load_environment,
        load_project,
        render_project_claude_md,
        render_resolved_block,
        resolve_project_name,
    )
except ImportError as exc:  # pragma: no cover - version-skew guard, not logic
    # `render_resolved_block` / `resolve_project_name` / `render_project_claude_md` /
    # `failure_report` live in agentflow.config, not here, so this file requires a
    # package at least that new. If the deployed package is older (stale editable
    # install, or a clone
    # where provision.py has not run yet), say WHICH half is behind - a raw
    # ImportError traceback here reads as "the hook is broken" and every session
    # silently loses its resolved-config block at the worst possible moment.
    sys.stderr.write(
        "agentflow SessionStart config injection FAILED\n"
        f"  {exc}\n"
        "  remediation: the hook and the agentflow package are version-skewed - "
        "re-run `python install/provision.py --force` so both come from the same "
        "commit\n"
    )
    raise SystemExit(1) from exc


def main() -> int:
    try:
        env = load_environment()
        # No explicit project: the hook injects the session default. Precedence
        # ($AGENTFLOW_PROJECT, then the single projects/<name>.md) lives in the resolver.
        name = resolve_project_name()
        proj = load_project(name) if name else None
        # Both renders sit inside THIS try so there is exactly one ConfigError
        # handler: a second copy would drift from this one, and the render of the
        # project CLAUDE.md raises ConfigError on an unreadable file.
        out = render_resolved_block(env, proj) + "\n"
        repo_rules = render_project_claude_md(proj)  # "" when there is none
        if repo_rules:
            out += repo_rules + "\n"
    except ConfigError as e:
        # The specific error first (a seat's ntfy carries line 1), then the headline,
        # then the full error: file, field_path, reason, remediation are all in str(e).
        sys.stderr.write(failure_report(e, "agentflow SessionStart config injection FAILED"))
        sys.stderr.flush()
        return 1

    # The project CLAUDE.md is arbitrary prose - em dashes, arrows, quotes - while
    # a Windows console hands Python a legacy cp1252 stdout. Writing that block
    # unreconfigured raises UnicodeEncodeError and the hook dies with a raw
    # traceback mid-write, losing the resolved-config block it had already built.
    # Force UTF-8 for the write; `replace` so no single odd byte can still abort it.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.write(out)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
