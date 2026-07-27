# How contextlake compares

contextlake is built around one idea other local code-context tools don't center on: **a
continuously mirrored fleet, not a single indexed folder.** Point it at a GitLab group, a
GitHub org, or a Bitbucket workspace, and it keeps every repo checked out on its most active
branch, kept fresh on a schedule, with a graph, semantic search, and an LLM-synthesized wiki
built on top, all local, all offline, served to your AI tools over MCP.

That fleet-mirror layer is the part the tools below don't have. Here's specifically what that
gets you, repo by repo.

## vs. Graphify

[Graphify](https://graphify.com/) parses a single codebase with tree-sitter into an AST-derived
graph, on-device, with no vector index and no generated wiki by design. That means:

- **No semantic or natural-language search.** Graphify is graph-only; if you want "find the
  code that handles refunds" instead of an exact symbol name, contextlake's embeddings layer
  gives you that out of the box.
- **No generated wiki.** contextlake turns the graph into grounded, cited prose per repo,
  "what does this service do," not just "who calls what."
- **One codebase at a time.** Graphify's own quickstart runs against a single project
  (`/graphify .`). contextlake mirrors and keeps fresh an entire org or group, so the graph,
  search, and wiki all span every repo you have, not just the one you happened to open.

## vs. GitNexus

[GitNexus](https://github.com/abhigyanpatwari/GitNexus) reaches for some of the same pieces,
a graph, vectors, a wiki, MCP serving, but what sets contextlake apart for a real engineering
org is what happens *before* any of that:

- **It's a standing mirror, not a one-shot index.** GitNexus indexes whatever you point it at,
  a repo, a ZIP, a URL, on demand. contextlake maintains a **persistent, always-fresh local
  mirror** of an entire org or group, automatically tracking each repo's most active branch
  over time, so the context your AI tools see stays current without you re-pointing anything,
  and the graph/search/wiki layers stay in sync with dozens or hundreds of repos at once, not
  whatever you last dropped in.
- **One shared MCP server, one always-current fleet.** You wire your editor up once and every
  repo in the org shows up in it automatically as it's added, not per-project.

(One footnote, not the headline: GitNexus ships under PolyForm Noncommercial 1.0.0; contextlake
is MIT.)

## Built for the fleet you actually have

If your real problem is "I have hundreds of repos and no single place that knows all of them,"
contextlake is built specifically for that: mirrored, branch-tracked, kept fresh automatically,
with a graph, search, and a wiki layered on top and kept in sync across every repo, without
you having to stitch that together yourself.
