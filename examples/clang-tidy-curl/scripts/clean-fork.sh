#!/bin/sh
# Reset the fork after a demo campaign: close every open codemods PR and
# delete every codemods/* branch on the fork. Subtask state in the local
# database is untouched (use `codemods doctor --fix` / `codemods abandon`
# for that side).
set -eu
owner=$(gh api user -q .login)
repo="$owner/curl"

gh pr list --repo "$repo" --state open --json number,headRefName \
    --jq '.[] | select(.headRefName | startswith("codemods/")) | .number' |
while read -r n; do
  gh pr close "$n" --repo "$repo" --delete-branch \
    --comment "Closed by codemods clean-fork script."
done

# Branches pushed but never PR'd (or left behind by closed PRs).
git ls-remote --heads "git@github.com:$repo.git" "refs/heads/codemods/*" |
awk '{print $2}' |
while read -r ref; do
  git push "git@github.com:$repo.git" --delete "${ref#refs/heads/}"
done
