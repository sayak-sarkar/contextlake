# site/

Source for the contextlake website (https://sayak.in/contextlake/), served from
the repo's `gh-pages` branch via GitHub Pages.

## Layout

- `index.html` — the landing page (hand-authored).
- `build_docs.py` — renders the repo's markdown (`README.md`, `QUICKSTART.md`,
  `docs/*.md`, `CHANGELOG.md`) into the branded docs pages, and syncs the shared
  brand assets in from `../docs`.
- `docs.css` — styles for the docs pages.
- `manifest.webmanifest`, `sitemap.xml`, `llms.txt` — PWA + SEO/AI metadata.
- `hero-scene*.webp` — the only site-specific images (the hero); everything else
  (icons, marks, pebbles, og-card, graph) is single-sourced in `../docs/img` and
  `../docs/branding` and copied in at build time (so it is not duplicated in git).
- `tools/` — the SVG diagram + icon generators that produce the assets under
  `../docs/img` / `../docs/branding`.

Generated pages and synced assets are gitignored (see `.gitignore`); only the
source above is tracked.

## Build & deploy

```bash
python3 site/build_docs.py   # build into site/ (HTML + synced assets)
./site/deploy.sh             # build, then publish to the gh-pages branch
```

Regenerate diagrams/icons when the brand changes: `python3 site/tools/gen_*.py`.
