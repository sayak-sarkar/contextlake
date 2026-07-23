# Reading the console output

A `bootstrap` (or a standalone `index` / `embed` / `wiki`, and the mirror-tier `clone` / `update` /
`branches`) prints progress as it goes. Most lines are self-explanatory; a few are worth decoding.

## The live progress bar

One shared renderer is used by every long-running command, so it looks the same everywhere: `[████████░░░░]
42/678 (6%) · 12:30 elapsed · ~2:58:14 left · 3.4/min` (bar, done/total, percent, elapsed time, estimated
time remaining, and rate in items/min). The ETA is a moving-average estimate over recent items (that's
what the `~` marks), and it's count-based, each item counts equally rather than being weighted by size.
When a run's total isn't known up front, the bar drops the percent/ETA and shows `done · elapsed · rate`
instead, rather than guessing. Across every long-running command (including `connect`, `ingest`, and
`enrich`, which don't use the shared bar), the clock only shows up on the bar itself (where there is one)
and on section/summary lines; the per-item detail lines scrolling beneath don't repeat it, so they don't
flicker as the timestamp ticks over.

## One status vocabulary, everywhere

Every command (mirror-tier `clone`/`update`/`branches`, `index`, `embed`, `wiki`, `enrich`, `ingest`,
`connect`, `lint`, `sync`) marks each line with the same seven glyphs, so once you know the glyph you know
the outcome without reading the rest of the line:

| Glyph | Meaning | Color |
| --- | --- | --- |
| `✓` | ok | green |
| `⚠` | warn | yellow |
| `✗` | fail | red |
| `⊘` | skip | dim |
| `=` | unchanged | dim |
| `↝` | switched | cyan |
| `~` | dry-run | yellow |

Multi-stage commands (`bootstrap` and `sync`) also print `▶ <Phase>` section headers (e.g. `▶ Mirror
repositories from GitLab`, `▶ Audit repositories (health & age)`) so a long run reads as sections rather
than one undifferentiated scroll, and every long-running command ends with a one-line, glyph-prefixed
summary (`✓ Embed complete: ...`, `✓ Lint: ...`, and so on) you can skim straight to.

## The stdout / stderr split

The bar renders on stderr; the per-item result lines below it (`✓`/`⚠` and the like) stay on stdout. That
split means `contextlake wiki >> run.log` (or any stdout redirect) captures clean detail lines with no bar
artifacts or `\r` clutter, since the bar never touches stdout. When output isn't a TTY (piped, cron, a
redirected stderr), the bar itself auto-downgrades to periodic plain summary lines instead of repainting in
place. When both streams share one terminal (the default interactive case), the bar and the detail lines
interleave as the run scrolls (the bar reprints below each new detail line rather than repainting perfectly
in place); redirect stdout to a file to keep the bar as a single live line with the detail captured
separately.

## Decoding specific lines

- **`✓ <repo>: X nodes, Y edges`** is the incremental indexer's per-repo detail line (stdout; the `index`
  progress bar above it lives on stderr). **`0 nodes, 0 edges`** is normal and not an error: that repo has
  no code in a supported language (config-only, docs-only, IaC/scripts, or empty). Only repos whose HEAD
  moved are re-indexed; the rest are reported as *unchanged*.
- **`Embed complete: 0 vector(s) written (N total in store), M already up to date`**, embedding is
  incremental too. `0 written` with a large `already up to date` count means nothing changed since the last
  run; the `N total` is the whole store, not this run.
- **`Fetching 10 files: 100% ... Download complete: 0.00B`** appears once when the wiki (or built-in
  embedder) model loads. It is Hugging Face resolving the model repo's files (several GGUF quantizations +
  tokenizer/config) in your local cache, **`0.00B` means nothing was downloaded, everything was already
  cached**. It fires once per run at model load, not per repo.
- **`✓ <repo>: written (score 0.98)`**, a wiki page passed the review council and was saved. **`⚠ <repo>:
  rejected by council (score 0.31)`**, it did not clear the accept threshold; the indented `-
  accuracy/completeness/clarity: ...` lines are the per-lens reasons. **`unparseable review`** means the
  model returned a review the council couldn't score (common with the tiny built-in 0.5B model); those
  lenses are excluded from the mean rather than counted as zero. A capable backend
  (`--llm ollama`/`anthropic`/`openai`) produces far fewer rejections, see
  [Model providers](model-providers.md).
- **`contextlake serve --transport http` prints its bind URL** once it starts listening (`✓ MCP server on
  http://127.0.0.1:8765  (Ctrl-C to stop)`), so you don't have to guess the host/port before pointing an
  editor at it. `stdio` transport has no address to report and stays quiet on that line.
- **`graph --overview` on an empty store warns instead of reporting silent success.** It still writes the
  (empty) artifact, but now says `⚠ Wrote html (0 nodes, 0 edges) -> ...: the store is empty.` followed by
  a hint to run `contextlake index` first, instead of logging the same success line it would for a
  populated graph.
- **A single-writer lock message** naming another process means two runs targeted one store at once (see
  the git-hook note under [Bootstrap and keep fresh](bootstrap.md)).

Warnings from the model download itself (Hugging Face symlink/auth notices) are silenced; the real
progress still shows.

## See also

- [Bootstrap and keep fresh](bootstrap.md)
- [Index the code graph](index-code-graph.md)
- [`contextlake` command reference](cli-reference.md)
