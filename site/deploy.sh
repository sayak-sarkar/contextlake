#!/usr/bin/env bash
# Build the docs site and publish it to the gh-pages branch.
#
#   ./site/deploy.sh
#
# Requires: python with `markdown` installed, and push access to origin.
# Diagram/icon assets are regenerated separately via site/tools/*.py.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PY="${PYTHON:-python3}"

echo "==> building docs + syncing assets"
"$PY" "$HERE/build_docs.py"

WT="$(mktemp -d)"
cleanup() { git -C "$REPO" worktree remove --force "$WT" 2>/dev/null || true; }
trap cleanup EXIT

echo "==> publishing to gh-pages"
git -C "$REPO" fetch origin gh-pages --quiet
git -C "$REPO" worktree add -f "$WT" gh-pages >/dev/null
git -C "$WT" reset --hard origin/gh-pages --quiet

# the deployable site = the page HTML + stylesheet + manifest/SEO + all assets
# (build_docs.py / tools/ / .gitignore are source, not shipped)
cp "$HERE"/*.html "$HERE"/docs.css "$HERE"/manifest.webmanifest "$HERE"/sitemap.xml "$HERE"/llms.txt "$WT"/
cp "$HERE"/*.png "$HERE"/*.jpg "$HERE"/*.webp "$HERE"/*.svg "$WT"/ 2>/dev/null || true

git -C "$WT" add -A
if git -C "$WT" diff --cached --quiet; then
  echo "==> no changes to deploy"
else
  git -C "$WT" commit -q -m "site: deploy"
  git -C "$WT" push origin gh-pages
  echo "==> deployed to gh-pages"
fi
