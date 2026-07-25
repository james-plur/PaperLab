#!/usr/bin/env bash
set -euo pipefail

if [[ "${PAPERLAB_CONFIRM_HISTORY_REWRITE:-}" != "YES" ]]; then
  echo "Refusing to rewrite history. Read README.md, back up data, then run:"
  echo "PAPERLAB_CONFIRM_HISTORY_REWRITE=YES scripts/clean_lfs_history.sh"
  exit 2
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree must be clean before history cleanup."
  exit 2
fi

if ! command -v git-filter-repo >/dev/null 2>&1; then
  echo "git-filter-repo is required. Install it with: uv tool install git-filter-repo"
  exit 2
fi

backup="paperlab-before-lfs-clean-$(date +%Y%m%d-%H%M%S).bundle"
git bundle create "$backup" --all
echo "Created recovery bundle: $backup"

git filter-repo --force \
  --path data/papers.db \
  --path pdfs \
  --path reports \
  --path .gitattributes \
  --invert-paths

git reflog expire --expire=now --all
git gc --prune=now
if command -v git-lfs >/dev/null 2>&1; then
  git lfs prune --force || true
fi

echo "History cleanup complete. Verify tests and repository size before deleting $backup."
