#!/bin/sh
# Create (or reuse) a user-owned fork of curl/curl, sync it with upstream,
# and clone it as a sibling of this repository — the layout the relative
# `repo` path in ../curl.hcl expects. Requires a logged-in `gh`
# (run `gh auth login` first).
set -eu
owner=$(gh api user -q .login)

gh repo view "$owner/curl" --json name >/dev/null 2>&1 \
  || gh repo fork curl/curl --clone=false
gh repo sync "$owner/curl" --source curl/curl

dest="$(cd "$(dirname "$0")/../../../.." && pwd)/curl"
if [ -d "$dest/.git" ]; then
  git -C "$dest" fetch origin
  echo "fork already cloned at $dest"
else
  git clone "git@github.com:$owner/curl.git" "$dest"
fi
