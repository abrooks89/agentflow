#!/usr/bin/env bash
# file-gate.sh — verifies that the branch's whole change set only touches files in the allowlist.
#
# Usage: bash file-gate.sh <allowlist-path> [<base-ref>]
#   allowlist-path: file with one path per line, relative to the repo root. Blank lines,
#                   whole-line `#` comments and inline ` # ...` comments are ignored; a trailing
#                   ` (new)` annotation is stripped.
#   base-ref:       the ref the branch was cut from (e.g. `origin/master` or `main`). Omitted,
#                   it falls back to `origin/HEAD`. If neither resolves, the gate exits 2: it
#                   never passes against a base it could not find.
#
# Scope checked, every path repo-root-relative wherever the gate is run from:
#   the diff from merge-base(<base-ref>, HEAD) to the working tree and to the index (so
#   committed, staged and unstaged edits all count), plus untracked files not ignored, with
#   --no-renames so a rename shows both its deleted and its added side. Anything under
#   .tdd-loop/, and the allowlist file itself while it is untracked, are the loop's own state
#   and never count. A tracked allowlist is a change like any other.
#
# The base guard refuses a base that names the current branch or that branch's own
# remote-tracking ref (refs/remotes/<remote>/<branch>). It compares names, not commits: at the
# start of a loop the base and HEAD are legitimately the same commit, so a base given as a raw
# SHA or a `~0` spelling of the branch is not caught. Pass the ref the branch was cut from.
#
# Exit 0: all changed files are in allowlist
# Exit 1: changed files outside allowlist (printed to stderr)
# Exit 2: usage error, git not available, or the base could not be resolved

set -euo pipefail

ALLOWLIST="${1:-}"
BASE="${2:-}"

if [[ -z "$ALLOWLIST" ]]; then
  echo "usage: file-gate.sh <allowlist-path> [<base-ref>]" >&2
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

if ! TOP="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "not inside a git work tree" >&2
  exit 2
fi

# The allowlist's own repo-relative path, resolved from its directory so a cwd-relative or
# absolute argument both work. Empty when it lives outside this work tree.
ALLOW_DIR="$(dirname -- "$ALLOWLIST")"
ALLOW_SELF=""
if ALLOW_TOP="$(cd -- "$ALLOW_DIR" && git rev-parse --show-toplevel 2>/dev/null)" \
  && [[ "$ALLOW_TOP" == "$TOP" ]]; then
  ALLOW_SELF="$(cd -- "$ALLOW_DIR" && git rev-parse --show-prefix)$(basename -- "$ALLOWLIST")"
fi

# Allowlist entries: trim (which also drops a CRLF file's CR), drop whole-line and inline
# " #" comments and blanks, strip a trailing " (new)" and a leading "./".
mapfile -t ALLOWED < <(
  sed -e 's/[[:space:]]#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$ALLOWLIST" \
    | grep -vE '^(#|$)' \
    | sed -e 's/[[:space:]]*(new)$//' -e 's|^\./||' \
    || true
)

# Every later git call runs from the top, so every path it prints is repo-root-relative.
cd -- "$TOP"

if [[ -z "$BASE" ]]; then
  if ! BASE="$(git symbolic-ref -q --short refs/remotes/origin/HEAD)"; then
    echo "file-gate: no <base-ref> given and origin/HEAD is not set; pass the ref this branch was cut from" >&2
    exit 2
  fi
fi

if ! BASE_SHA="$(git rev-parse -q --verify "${BASE}^{commit}")"; then
  echo "file-gate: base ref does not resolve: $BASE" >&2
  exit 2
fi

# A base that is the branch itself, or the branch's own copy on a remote, makes the branch
# scope empty, which would pass every committed edit. The branch's upstream is NOT refused:
# cutting tdd/<slug> from origin/<base-branch> makes that its upstream, and it is the right base.
BASE_FULL="$(git rev-parse -q --symbolic-full-name "$BASE" 2>/dev/null || true)"
HEAD_FULL="$(git symbolic-ref -q HEAD || true)"
SELF_BASE=0
if [[ -n "$BASE_FULL" && -n "$HEAD_FULL" ]]; then
  if [[ "$BASE_FULL" == "$HEAD_FULL" ]]; then
    SELF_BASE=1
  else
    while IFS= read -r remote; do
      [[ "$BASE_FULL" == "refs/remotes/${remote}/${HEAD_FULL#refs/heads/}" ]] && SELF_BASE=1
    done < <(git remote)
  fi
fi
if [[ $SELF_BASE -eq 1 ]]; then
  echo "file-gate: base ref $BASE is the current branch or its own remote copy; pass the ref it was cut from" >&2
  exit 2
fi

if ! MB="$(git merge-base "$BASE_SHA" HEAD)"; then
  echo "file-gate: no merge-base between $BASE and HEAD" >&2
  exit 2
fi

# NUL-separated so git never quotes a path (a `"`, `\`, tab or newline in a name).
mapfile -d '' -t CHANGED < <(
  {
    git diff -z --name-only --no-renames "$MB"
    git diff -z --name-only --no-renames --cached "$MB"
    git ls-files -z --others --exclude-standard
  } | sort -zu
)

# The allowlist is exempt only while untracked: committed or staged, it is a branch change.
if [[ -n "$ALLOW_SELF" ]] && git ls-files --error-unmatch -- ":(literal)$ALLOW_SELF" >/dev/null 2>&1; then
  ALLOW_SELF=""
fi

OFFENDERS=()
COUNT=0
for f in "${CHANGED[@]}"; do
  [[ -z "$f" ]] && continue
  [[ "$f" == .tdd-loop/* || "$f" == "$ALLOW_SELF" ]] && continue
  COUNT=$((COUNT + 1))
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
  echo "file-gate OK: ${COUNT} changed file(s) since merge-base with ${BASE} (${MB:0:7}), all in allowlist"
  exit 0
fi

echo "file-gate FAIL: changed files outside allowlist (since merge-base with ${BASE}, ${MB:0:7}):" >&2
for o in "${OFFENDERS[@]}"; do
  echo "  $o" >&2
done
echo "" >&2
echo "Either revert these edits or surface to user that the allowlist needs to expand." >&2
exit 1
