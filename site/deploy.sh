#!/usr/bin/env bash
# Build the docs site and publish it to the gh-pages branch.
#
#   ./site/deploy.sh
#
# Requires: python with `markdown` and `pymdown-extensions` installed, and push access to origin.
# Diagram/icon assets are regenerated separately via site/tools/*.py.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PY="${PYTHON:-python3}"

echo "==> building docs + syncing assets"
"$PY" "$HERE/build_docs.py"
# enrich the search index with build-time semantic neighbours (best-effort: no-op if the
# knowledge-layer extra is absent, so a lean environment still ships the lexical index).
"$PY" "$HERE/tools/gen_search_index.py" || true

echo "==> building the public read-only demo dashboard (bundled fictional fleet)"
rm -rf "$HERE/demo"
"$PY" -m contextlake kb dashboard --site "$HERE/demo" --sample

WT="$(mktemp -d)"
cleanup() { git -C "$REPO" worktree remove --force "$WT" 2>/dev/null || true; }
trap cleanup EXIT

echo "==> publishing to gh-pages"
git -C "$REPO" fetch origin gh-pages --quiet
git -C "$REPO" worktree add -f "$WT" gh-pages >/dev/null
git -C "$WT" reset --hard origin/gh-pages --quiet

# the deployable site = the page HTML + stylesheet + manifest/SEO + all assets
# (build_docs.py / tools/ / .gitignore are source, not shipped)
cp "$HERE"/*.html "$HERE"/docs.css "$HERE"/tokens.css "$HERE"/cmdk.css "$HERE"/cmdk.js "$HERE"/fonts.css "$HERE"/manifest.webmanifest "$HERE"/sitemap.xml "$HERE"/llms.txt "$HERE"/llms-full.txt "$HERE"/search-index.json "$WT"/
cp "$HERE"/*.png "$HERE"/*.jpg "$HERE"/*.webp "$HERE"/*.svg "$WT"/ 2>/dev/null || true
cp -r "$HERE"/fonts "$WT"/
cp -r "$HERE"/demo "$WT"/
# The docs/img tree, and the vendored mermaid the diagram pages load. Both are
# subdirectory/extra assets that the flat globs above do not reach, and both fail
# silently: a missing img/ shows broken pictures, a missing mermaid.min.js shows
# raw diagram source. Neither is visible in a local build.
rm -rf "$WT"/img
cp -r "$HERE"/img "$WT"/
cp "$HERE"/mermaid.min.js "$WT"/

# Remove pages that no longer exist in the built site. `reset --hard` restores the
# previous deploy and the copies above only add, so a retired page stayed live
# forever: bootstrap, ownership, storage and comparison were all still served with
# no source behind them. Only generated doc pages are considered, and only when the
# build produced a healthy set, so a broken build cannot empty the site.
BUILT="$(ls "$HERE"/*.html | wc -l)"
if [ "$BUILT" -lt 10 ]; then
  echo "==> refusing to prune: only $BUILT built pages, that looks like a failed build" >&2
else
  for f in "$WT"/*.html; do
    base="$(basename "$f")"
    case "$base" in index.html|404.html|graph-embed.html) continue ;; esac
    if [ ! -f "$HERE/$base" ]; then
      echo "==> removing retired page from gh-pages: $base"
      git -C "$WT" rm -q --ignore-unmatch "$base" || rm -f "$f"
    fi
  done
fi

# cache-bust the linked assets: GitHub Pages serves static files with a 4h
# max-age, and we reuse filenames (docs.css, fonts.css, graph-embed.html), so a
# browser keeps serving the stale copy until it expires. Stamp each linked ref
# with the current commit sha; the HTML itself has a short (10min) TTL, so a
# changed file now propagates within that window instead of 4h. The hero images
# are cross-referenced from inline CSS + preload + JS and are finalized, so we
# leave them (a version skew there would defeat the preload).
VER="$(git -C "$REPO" rev-parse --short HEAD)"
find "$WT" -maxdepth 1 -name '*.html' -print0 | xargs -0 sed -i \
  -e "s/href=\"docs\.css\"/href=\"docs.css?v=$VER\"/g" \
  -e "s/href=\"tokens\.css\"/href=\"tokens.css?v=$VER\"/g" \
  -e "s/href=\"fonts\.css\"/href=\"fonts.css?v=$VER\"/g" \
  -e "s/href=\"cmdk\.css\"/href=\"cmdk.css?v=$VER\"/g" \
  -e "s/src=\"cmdk\.js\"/src=\"cmdk.js?v=$VER\"/g" \
  -e "s/data-embed=\"graph-embed\.html\"/data-embed=\"graph-embed.html?v=$VER\"/g"
echo "==> cache-busted linked assets with ?v=$VER"

git -C "$WT" add -A
if git -C "$WT" diff --cached --quiet; then
  echo "==> no changes to deploy"
else
  git -C "$WT" commit -q -m "site: deploy"
  git -C "$WT" push origin gh-pages
  echo "==> deployed to gh-pages"
fi
