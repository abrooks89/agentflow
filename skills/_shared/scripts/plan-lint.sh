#!/usr/bin/env bash
# PostToolUse hook (Write|Edit): lint epic plan files at write time.
#
# THIS HOOK REPORTS; IT CANNOT BLOCK. PostToolUse fires AFTER the tool has run, so
# exit 2 neither prevents nor undoes the write — it fails the file to the writing
# session, which is then expected to fix it. (Only PreToolUse can block on exit 2.)
# So a false positive here is not a wedge, it is a standing FALSE ACCUSATION against
# a file already on disk, and the accused session may not be the one that can fix it.
# That asymmetry is why the scope below is narrow on purpose and excludes first.
#
# 1. Normalize CRLF -> LF in place (Windows bulk-edit corruption guard).
#    KNOWN PLATFORM GAP, not fixed here and tracked separately: under Git Bash the
#    MSYS `grep` opens files in text mode and strips CR before matching, so the
#    guard's own `grep -q $'\r'` is never true on Windows and the rewrite never runs
#    there. It does run on Linux and macOS.
# 2. Run agentflow.plan_parser.parse_plan on the file. On parse failure exit 2 with
#    the error on stderr so the writing session sees it immediately and fixes
#    the plan BEFORE the orchestrator wastes a wake on it (catches the
#    string-vs-int deps class, bad verification blocks, DAG errors).
#
# Scope: plan files under EITHER spelling of the plans dir — the canonical
# */design/plans/*.md, and the legacy */docs/superpowers/plans/*.md — that carry
# epic-plan YAML frontmatter (a `tasks:` key). Everything else exits 0 untouched.
# The legacy arm exists because that path is a junction onto the same files, and
# writes still arrive spelled that way; without it those writes go unchecked.
#
# EXCLUDED, AND EXCLUDED FIRST: the orchestrator's own status files, `*.status.md`
# and `status-*.md`. They live in the SAME directory as the plans and they carry a
# `tasks:` key too, so the frontmatter check cannot separate them — every one of them
# reaches parse_plan and fails it (73 of the 165 `tasks:`-bearing files measured across
# both plan trees on one machine). The FILENAME is the only thing that tells them apart, and
# it is the half the orchestrator controls. Without this arm the orchestrator gets
# accused by this hook every time it writes its own status file, which is the exact
# "hook blames the file for the hook's own problem" shape gh#110 exists to close.
# `case` takes the FIRST matching arm, so the ORDER here is the mechanism: move this
# arm below the in-scope ones and it can never run.
set -u

input="$(cat)"

fp="$(PLAN_LINT_JSON="$input" python -c '
import os, json
try:
    d = json.loads(os.environ.get("PLAN_LINT_JSON", ""))
except Exception:
    raise SystemExit(0)
fp = ((d.get("tool_input") or {}).get("file_path", "") or "").replace("\\", "/")
print(fp)
')"

case "$fp" in
  *.status.md|*/status-*.md) exit 0 ;;
  */design/plans/*.md|*/docs/superpowers/plans/*.md) ;;
  *) exit 0 ;;
esac
[ -f "$fp" ] || exit 0

# CRLF -> LF in place (binary-safe; only rewrites when CRLF is present).
if grep -q $'\r' "$fp" 2>/dev/null; then
  python -c "
import sys
p = sys.argv[1]
b = open(p, 'rb').read()
open(p, 'wb').write(b.replace(b'\r\n', b'\n'))
" "$fp"
fi

# Parse-validate epic plans only (frontmatter with a tasks: key).
err="$(python -c "
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8', errors='replace')
if not text.startswith('---'):
    raise SystemExit(0)          # no frontmatter -> not an epic plan
fm = text.split('---', 2)[1] if text.count('---') >= 2 else ''
if 'tasks:' not in fm:
    raise SystemExit(0)          # frontmatter without tasks -> not an epic plan

try:
    from agentflow.plan_parser import parse_plan
    parse_plan(path)
except Exception as e:
    print(f'{type(e).__name__}: {e}')
    raise SystemExit(1)
" "$fp" 2>&1)"

if [ -n "$err" ]; then
  echo "plan-lint: $fp failed parse_plan validation — the orchestrator will reject this plan on first wake. Fix it now: $err" >&2
  exit 2
fi
exit 0
