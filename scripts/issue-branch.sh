#!/usr/bin/env bash
# Crée et pousse la branche de travail d'une issue.
#   scripts/issue-branch.sh 12
set -euo pipefail

num="${1:?usage: scripts/issue-branch.sh <numéro d'issue>}"
cd "$(git rev-parse --show-toplevel)"

title=$(gh issue view "$num" --json title --jq .title)
labels=$(gh issue view "$num" --json labels --jq '[.labels[].name] | join(",")')

prefix=feat
case "$labels" in
  *type:bug*) prefix=fix ;;
  *type:chore*|*type:docs*) prefix=chore ;;
esac

slug=$(printf '%s' "$title" \
  | iconv -f utf-8 -t ascii//TRANSLIT 2>/dev/null \
  | tr '[:upper:]' '[:lower:]' \
  | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' \
  | cut -c1-50)

branch="$prefix/$num-$slug"

git switch main
git pull --ff-only
git switch -c "$branch"
echo "Branche $branch prête — issue #$num : $title"
