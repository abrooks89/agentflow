#!/usr/bin/env bash
# E-0200 item 4 - the ONE-TIME three-pillar bucket move. Not shipped code.
# Usage: migrate_layout.sh <data_root>
# Moves, per project:  refine -> design/specs      solution -> design/plans
#                      use-cases -> design/use-cases
#                      status/feature-suggestions.md -> design/suggestions/
# Creates:             design/suggestions, wiki/pages, and the done+archived partitions
# status/ is NEVER touched, and autodoc/ is NOT scaffolded here - an empty autodoc/
# passes every exists+is_dir gate while the real corpus sits orphaned, which makes
# /autodoc-init regenerate ~585 pages. That bucket is created by the corpus move.
set -euo pipefail
ROOT="${1:?usage: migrate_layout.sh <data_root>}"
[ -d "$ROOT/projects" ] || { echo "not a data_root: $ROOT" >&2; exit 1; }

for proj in "$ROOT"/projects/*/; do
  name=$(basename "$proj")
  mkdir -p "$proj/design" "$proj/wiki/pages"
  for pair in "refine:design/specs" "solution:design/plans" "use-cases:design/use-cases"; do
    src="${pair%%:*}"; dst="${pair##*:}"
    if [ -d "$proj/$src" ]; then
      [ -e "$proj/$dst" ] && { echo "REFUSING: $proj$dst already exists" >&2; exit 1; }
      mv "$proj/$src" "$proj/$dst"
      echo "  $name: $src -> $dst"
    fi
  done
  mkdir -p "$proj/design/suggestions"
  if [ -f "$proj/status/feature-suggestions.md" ]; then
    mv "$proj/status/feature-suggestions.md" "$proj/design/suggestions/feature-suggestions.md"
    echo "  $name: status/feature-suggestions.md -> design/suggestions/"
  fi
  # done/ + archived/ partitions exist under every authored bucket (item 5 fills them)
  for b in specs plans use-cases suggestions; do
    mkdir -p "$proj/design/$b/done" "$proj/design/$b/archived"
  done
  mkdir -p "$proj/wiki/pages/done" "$proj/wiki/pages/archived"
done
echo "migration complete: $ROOT"
