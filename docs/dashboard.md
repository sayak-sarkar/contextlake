# The dashboard: a guided tour

The dashboard is the human window into everything contextlake builds: a **local,
offline-first** single-page app over your knowledge store. No accounts, no cloud, no
build step, one command and it opens in your browser. Read-only by default; see
[§10](#10-mutating-routes---allow-mutations) for the opt-in write actions.

> New here? Skim [QUICKSTART](../QUICKSTART.md) first. For what the graph/wiki/search
> tiers actually do, see [knowledge-layer.md](knowledge-layer.md).

## 1. Get some data in (or don't)

The dashboard reads your indexed store. If you've already run `contextlake index` (or
`contextlake bootstrap`), you're set. **Just want to look around first?** Every screen
below works against a bundled, generic demo fleet, no setup, no real data:

```bash
contextlake dashboard --serve --sample      # a fictional "acme" fleet, served live
```

To build against your own repos, index a workspace once:

```bash
contextlake index --workspace ~/work        # or `contextlake bootstrap` for the full pipeline
```

## 2. Launch it

```bash
contextlake dashboard --serve --open         # live, against your store; opens your browser
```

| Flag | What it does |
|---|---|
| `--serve` | Run it **live** against your store (everything on demand, no caps). |
| `--site DIR` | Export a **static** `file://`-safe copy (a representative slice). |
| `--sample` | Build from the **bundled demo fleet**, guaranteed generic, safe to share. |
| `--anonymize` | For a real-store `--site`: hash authors, drop URLs + prose (shareable). |
| `--open` | Open the result in your browser. |
| `--group-depth N` | How many namespace path segments deep to group repos in the fleet overview (default `1`). Raise it to split one big flat group into finer sub-groups. |
| `--allow-mutations` | `--serve` only: also expose sync/add-repo/MCP-server actions (see §10). Loopback host only; refused with `--sample`. |
| `--workspace DIR` | `--allow-mutations`: where **Add repo** clones new repos (default: alongside the store). |

> Browsing your whole fleet? Use `--serve`, it renders each repo on demand with no
> caps. A `--site` export is a fixed, shareable slice.

> [!WARNING]
> **Before you share a `--site` export:** a real-store export inlines repo names,
> git-author identities, and connector URLs, so it prints a "do not publish unscrubbed"
> warning. For anything you intend to share, build it with **`--anonymize`** (hashes author
> identities, drops external URLs + README/wiki prose) or **`--sample`** (the bundled,
> guaranteed-generic demo fleet).

## 3. The fleet overview

Stat cards, a **knowledge-confidence** bar, and your repos grouped by namespace.

![Fleet overview, cards layout](https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/fleet-cards.png)

Prefer denser views? Switch the layout, **Cards / List / Table** (your choice is
remembered):

![Fleet overview, list layout](https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/fleet-list.png)

![Fleet overview, table layout](https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/fleet-table.png)

Not sure what a control means? The **ⓘ "What am I looking at?"** button explains nodes,
edges, the three confidence levels, and the Live vs. Static data source:

![The info popover explaining nodes, edges, confidence, and data source](https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/info-popover.png)

## 4. A repo up close

Click any repo for its **anatomy** (node kinds and top symbols) plus README, curated
wiki, owners (ranked from git history), and connector links. Every symbol has a
one-click **Blast radius**, and every fact carries its provenance.

![A repo's anatomy: kinds and top symbols](https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/repo-anatomy.png)

## 5. Diagrams

A repo's **Diagrams** tab renders the same Mermaid text `contextlake graph --repo <id>
--format <fmt>` produces, inline as SVG: **Relations** (the generic relation graph,
always available), **Classes** (classes/interfaces/structs/enums and their methods),
**States** (an entity's guarded state-machine transitions), **Data model** (SQL `table`/
`view` definitions and their foreign keys), and **Deployment** (Terraform/HCL resources
grouped by inferred category: network/compute/storage/database/security/module).

A sixth format, **Sequence** (`--format sequencediagram`), needs a single symbol as its
seed rather than a whole repo, so it isn't offered here — it's on the symbol page's
**Call sequence** card instead (§7).

No new extraction: each tab renders data `index` already collected. A format is only
enabled when the repo actually has the relevant node kind (e.g. **Classes** stays
disabled for a repo with no classes) — this is read from the same anatomy census the
repo page's Kinds card already shows, not a separate check. The raw Mermaid source sits
below the rendered diagram with a one-click copy, for pasting into a PR or design doc.

Live-only (not part of a `--site` export, same as MCP console/Settings below) — Mermaid
itself is lazy-loaded into the page only the first time this tab is opened.

## 6. Architecture & relationships

The cross-repo dependency graph, a **namespace** mindmap and a **dependency** flow,
one interactive graph, alongside dependency / HTTP-flow / event-flow tables, each with
confidence and provenance (never shown as ground truth).

A repo page's tables add a fourth tab, **Data flow**: which files read or write which
SQL tables/views inside that one repo. Unlike the other three, this isn't a repo→repo
edge — dependency/HTTP-flow/event-flow join on a node shared across repos (a package,
an endpoint, a topic), but a table/view definition is only ever known within the repo
that defines it, so a file's read/write only ever resolves inside its own repo. Data
flow is therefore always scoped to the repo you're looking at, never cross-repo.

![The architecture graph: cross-repo dependencies](https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/architecture.png)

## 7. Change impact (blast radius)

Pick a symbol (from search or a repo's symbol list) and see what a change would touch,
hop by hop, with the confidence of each path. A **Call sequence** card renders that same
neighborhood as a Mermaid sequence diagram (`--format sequencediagram`, §5) seeded by
the symbol you're on.

![Blast radius: what a change to a symbol would touch](https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/blast-radius.png)

The breadcrumb keeps going from there: **repo → symbol → Diagram → Wiki → Links → Ticket**, one click
each to that symbol's repo-scoped architecture graph, curated wiki, and connector links
(Jira/Confluence/GitLab). Wiki, Links, and Ticket only appear when they actually apply: an absent wiki,
connector link, or per-symbol ticket is omitted from the trail, never shown as a dead crumb. **Ticket** is
distinct from the repo-level Links crumb: it's an issue attributed to *this specific symbol* (from its own
docstring or the git-blame commit message on its defining line — see
[Connect and enrich](connect-enrich.md#connectors)), live-JQL confirmed the same way a branch-derived key
is. Clicking it opens the real tracker URL directly, not another dashboard view.

## 8. Generate a wiki

No wiki for a repo yet? Its **Wiki** tab hands you the exact command (one click to copy):

```bash
contextlake wiki acme/catalog-api --llm builtin
```

`--llm` enables the LLM tier inline, `builtin` runs a small CPU model with no Ollama or
API key (install the `llm-local` extra first); `ollama` / `openai` use those backends.
The positional repo id scopes generation to just that repo. Once it's generated, the page
renders right in the Wiki tab, grounded in the repo's real symbols, with a provenance
footer citing the exact commit and source files.

![The Wiki tab: a generated page grounded in real symbols, with a provenance footer citing the commit and source files](https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/dashboard/wiki.png)

See [knowledge-layer.md → Curated wiki](knowledge-layer.md#curated-wiki).

## 9. MCP console & Settings

Two read-only panels, live-only (not part of a `--site` export; both describe this
machine/process, not the graph itself):

**MCP**: the live tool catalog for `contextlake serve` against this store (introspected
from the real server, so it can never drift from what's actually exposed), plus a
copyable `.mcp.json` / `.vscode/mcp.json` snippet for wiring an editor to it.

**Settings**: the active `kb.toml` at a glance: store path/size/schema version, the
mirror root, configured connectors, and the embedder/LLM tiers. No in-browser editing:
it's a summary, not a form; edit `kb.toml` directly to change anything.

## 10. Mutating routes (`--allow-mutations`)

Everything above is read-only. `contextlake dashboard --serve --allow-mutations`
additionally exposes three write actions, each behind an explicit confirm dialog in
the browser: **Sync now** on a repo page (`git pull --ff-only` + reindex), **Add
repo** on the fleet overview (clone a URL into `--workspace` + index it), and
**Start / Stop / Restart** for a separate `contextlake serve --transport http`
process on the MCP console tab (not the stdio server your editor spawns -- this
dashboard can't see or manage that one).

Refused outright with `--sample` (the demo fleet is fictional -- nothing on disk to
sync/clone) and with any `--host` other than `127.0.0.1`/`localhost` (mutating
routes are loopback-only by design). A random per-launch token is minted at
startup and wired into the served page; every `POST` must carry it in an
`X-Contextlake-Token` header matching exactly, and the request's `Host` header
must name this host:port -- both close the classic localhost-server holes (a
form-encoded POST from any open browser tab, and DNS rebinding around the
loopback bind, respectively). A mutation takes the store's single-writer lock
for its own duration only, so a concurrent CLI command sees a clean `409`
instead of an interleaved write.

---

Everything here runs entirely on your machine, and is read-only unless you opt into
`--allow-mutations` (§10).
