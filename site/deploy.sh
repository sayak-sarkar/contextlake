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

# The set of pages this deploy publishes, decided once and used for both the copy and
# the prune below. site/ is a build directory as well as a source directory, and other
# tools drop HTML into it: a `kb dashboard --site .` run left overview.html and seven
# repo-*.html pages from a retired sample fleet sitting here, and the old wholesale
# `cp "$HERE"/*.html` republished every one of them. A page ships only if it is one of
# ours: hand-authored and tracked in git, or carrying build_docs.py's generated-page
# marker. Anything else is named on stderr and left where it is.
DEPLOY_PAGES=()
for f in "$HERE"/*.html; do
  base="$(basename "$f")"
  case "$base" in
    index.html|404.html) DEPLOY_PAGES+=("$base"); continue ;;
  esac
  if git -C "$REPO" ls-files --error-unmatch "site/$base" >/dev/null 2>&1; then
    DEPLOY_PAGES+=("$base"); continue
  fi
  if grep -q 'class="prose"' "$f"; then
    DEPLOY_PAGES+=("$base"); continue
  fi
  echo "==> not a site page, not publishing: $base" >&2
done

in_deploy_set() {
  case " ${DEPLOY_PAGES[*]-} " in *" $1 "*) return 0 ;; esac
  return 1
}

echo "==> building the public read-only demo dashboard (bundled fictional fleet)"
rm -rf "$HERE/demo"
"$PY" -m contextlake kb dashboard --site "$HERE/demo" --sample
# demo/ is replaced wholesale on gh-pages below, so a build that produced nothing would
# empty the live demo. Check the two files it cannot work without before going further.
if [ ! -f "$HERE/demo/index.html" ] || [ ! -f "$HERE/demo/data.json" ]; then
  echo "==> refusing to deploy: the demo build wrote no index.html/data.json" >&2
  exit 1
fi

WT="$(mktemp -d)"
cleanup() { git -C "$REPO" worktree remove --force "$WT" 2>/dev/null || true; }
trap cleanup EXIT

echo "==> publishing to gh-pages"
git -C "$REPO" fetch origin gh-pages --quiet
git -C "$REPO" worktree add -f "$WT" gh-pages >/dev/null
git -C "$WT" reset --hard origin/gh-pages --quiet

# the deployable site = the pages in DEPLOY_PAGES + stylesheet + manifest/SEO + all assets
# (build_docs.py / tools/ / .gitignore are source, not shipped)
for base in "${DEPLOY_PAGES[@]-}"; do
  [ -n "$base" ] && cp "$HERE/$base" "$WT"/
done
cp "$HERE"/docs.css "$HERE"/tokens.css "$HERE"/cmdk.css "$HERE"/cmdk.js "$HERE"/fonts.css "$HERE"/manifest.webmanifest "$HERE"/sitemap.xml "$HERE"/llms.txt "$HERE"/llms-full.txt "$HERE"/search-index.json "$WT"/
cp "$HERE"/*.png "$HERE"/*.jpg "$HERE"/*.webp "$HERE"/*.svg "$WT"/ 2>/dev/null || true
cp -r "$HERE"/fonts "$WT"/
# Replace demo/, do not merge into it. `cp -r` alone copied the new export over the old
# one and left everything the new one no longer has: six repo-*.html pages under
# demo/graph/ from a retired sample fleet stayed live long after the fixture behind them
# was replaced. The guard after the build above is what makes this rm safe.
rm -rf "$WT"/demo
cp -r "$HERE"/demo "$WT"/
# The docs/img tree, and the vendored mermaid the diagram pages load. Both are
# subdirectory/extra assets that the flat globs above do not reach, and both fail
# silently: a missing img/ shows broken pictures, a missing mermaid.min.js shows
# raw diagram source. Neither is visible in a local build.
rm -rf "$WT"/img
cp -r "$HERE"/img "$WT"/
cp "$HERE"/mermaid.min.js "$WT"/

# Remove pages this deploy does not publish. `reset --hard` restores the previous
# deploy and the copies above only add, so a retired page stayed live forever:
# bootstrap, ownership, storage and comparison were all still served with no source
# behind them. The test is membership of DEPLOY_PAGES, not "is the file absent from
# site/": the earlier absent-locally test let a stale page survive by being present,
# which is how a retired sample fleet stayed published. Only runs when the deploy set
# is a healthy size, so a broken build cannot empty the site.
BUILT="${#DEPLOY_PAGES[@]}"
if [ "$BUILT" -lt 10 ]; then
  echo "==> refusing to prune: only $BUILT pages to publish, that looks like a failed build" >&2
else
  for f in "$WT"/*.html; do
    base="$(basename "$f")"
    case "$base" in index.html|404.html|graph-embed.html) continue ;; esac
    if ! in_deploy_set "$base"; then
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
