#!/usr/bin/env bash
# file-gate.sh — verifies that the current diff only touches files in the allowlist.
#
# Usage: bash file-gate.sh <allowlist-path>
#   allowlist-path: file with one path per line (relative to repo root)
#
# Exit 0: all changed files are in allowlist
# Exit 1: changed files outside allowlist (printed to stderr)
# Exit 2: usage error or git not available

set -euo pipefail

ALLOWLIST="${1:-}"

if [[ -z "$ALLOWLIST" ]]; then
  echo "usage: file-gate.sh <allowlist-path>" >&2
  exit 2
fi

if [[ ! -f "$ALLOWLIST" ]]; then
  echo "allowlist not found: $ALLOWLIST" >&2
  exit 2
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git not on PATH" >&2
  exit 2
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "not inside a git work tree" >&2
  exit 2
fi

# Build allowlist set, ignore blank lines and comments
mapfile -t ALLOWED < <(grep -vE '^\s*(#|$)' "$ALLOWLIST" | sed 's|^\./||')

# All changed files: staged + unstaged + untracked, vs HEAD
mapfile -t CHANGED < <(
  {
    git diff --name-only HEAD
    git diff --name-only --cached
    git ls-files --others --exclude-standard
  } | sort -u
)

OFFENDERS=()
for f in "${CHANGED[@]}"; do
  [[ -z "$f" ]] && continue
  ok=0
  for a in "${ALLOWED[@]}"; do
    if [[ "$f" == "$a" ]]; then
      ok=1
      break
    fi
  done
  if [[ $ok -eq 0 ]]; then
    OFFENDERS+=("$f")
  fi
done

if [[ ${#OFFENDERS[@]} -eq 0 ]]; then
  echo "file-gate OK: ${#CHANGED[@]} changed file(s), all in allowlist"
  exit 0
fi

echo "file-gate FAIL: changed files outside allowlist:" >&2
for o in "${OFFENDERS[@]}"; do
  echo "  $o" >&2
done
echo "" >&2
echo "Either revert these edits or surface to user that the allowlist needs to expand." >&2
exit 1
