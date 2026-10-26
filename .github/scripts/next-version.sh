#!/usr/bin/env bash
# Prints the next release version from the Conventional Commits since the last release tag, or nothing
# when there is nothing to release. A breaking change bumps the minor version while the major is 0.
set -euo pipefail

last=$(git tag --merged HEAD --list 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)
range=${last:+$last..}HEAD
subjects=$(git log --format=%s "$range")
bodies=$(git log --format=%B "$range")

# Here-strings, not pipes: grep -q stops reading early, and under pipefail git log would fail with SIGPIPE.
if grep -qE '^BREAKING[ -]CHANGE:' <<<"$bodies" || grep -qE '^[a-z]+(\([^)]*\))?!:' <<<"$subjects"; then
  bump=major
elif grep -qE '^feat(\([^)]*\))?:' <<<"$subjects"; then
  bump=minor
elif grep -qE '^fix(\([^)]*\))?:' <<<"$subjects"; then
  bump=patch
else
  exit 0
fi

IFS=. read -r major minor patch <<<"${last#v}"
major=${major:-0} minor=${minor:-0} patch=${patch:-0}
if [[ $bump == major && $major == 0 ]]; then
  bump=minor
fi
case $bump in
  major) echo "$((major + 1)).0.0" ;;
  minor) echo "$major.$((minor + 1)).0" ;;
  patch) echo "$major.$minor.$((patch + 1))" ;;
esac
