# Visualize the graph

`contextlake graph` draws a **bounded** slice of the graph. The whole thing (hundreds of thousands of
nodes) is far too large to render, so every view is scoped from a seed and capped:

```bash
contextlake graph --overview --open                 # repos-as-nodes: the architecture map
contextlake graph --name CatalogService --kind class  # a symbol's neighbourhood (default 2 hops)
contextlake graph --node <id> --hops 3              # expand around an exact node id
contextlake graph --search "payment" --open         # seed from a full-text search
contextlake graph --repo acme/catalog-api           # one repo's internal code graph
```

`contextlake graph --repo <repo>` renders one repo's internal code graph to a single self-contained HTML
page: nodes coloured by kind and sized by degree, edges by relation, with an in-page layout switcher,
search, and a minimap; it opens straight from `file://`:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/graph-repo.png" alt="The offline HTML code graph for acme/catalog-api: file, class, and method nodes (CatalogService, PaymentClient, place_order, charge, refund) coloured by kind and linked by calls/contains edges, with a legend, layout switcher, and corner minimap." width="820">
</p>

Seed with one of `--node` / `--name` (+`--kind`) / `--search` / `--repo` / `--overview`. Bound the result
with `--hops` (default 2), `--max-nodes` (500), `--max-edges` (400 for `--repo` views -- a dense repo can
pack well over 500 edges into 500 nodes, which used to exceed Mermaid's own render limit and fail outright;
capping edges independently means it always renders, possibly truncated, never errors), `--max-fanout` (50,
a per-node cap that stops hub nodes from exploding), `--relation`, and `--direction {in,out,both}`, whatever
is dropped is **logged**, never silently truncated. For a `--repo` view over `--max-nodes`, which nodes
survive the cut is now ranked by degree (highest-connected nodes kept first, ties broken by node id)
rather than an arbitrary node-id order, so a truncated diagram keeps the most connected part of the repo
instead of whatever happened to sort first. On the dashboard, a repo too large to show in one diagram gets
a "scope to one module" dropdown (its top-level path segments) instead of an arbitrary slice.

## Output formats

Output is chosen with `--format`:

- **`html`** (default), a single **self-contained, offline** page (cytoscape.js is inlined, so it opens
  from `file://` with no network, handy air-gapped / behind a proxy). Nodes are coloured by kind and sized
  by degree; edges are styled by relation/confidence with their labels hidden until you click a node (so
  the view stays readable). Pan, zoom, drag, and a **layout switcher** (`cose`, `concentric`,
  `breadthfirst`, `circle`, `grid`) in the page, set the initial one with `--layout`. `--open` launches the
  browser; `--cdn` produces a small online-only file instead.
- **`dot`**, Graphviz (`contextlake graph ... --format dot | dot -Tsvg > g.svg`).
- **`mermaid`**, the relation graph, pastes into Markdown / GitHub.
- **`classdiagram`**, a **Mermaid UML class diagram** for a repo (or a seeded slice): classes / interfaces
  / structs with their methods as members, and `inherits` edges as inheritance arrows (`<|--` extends,
  `<|..` implements). Great for a PR or design doc: `contextlake graph --repo acme/app --format classdiagram`.
- **`sequencediagram`**, a **Mermaid call-order trace** from one seeded function, each caller's callees
  ordered by call-site line, the order they actually appear in the source: `contextlake graph --name
  process_order --format sequencediagram`. Needs exactly one seed (`--node`/`--name`/`--search`, not
  `--repo`/`--overview`; there's no single obvious ordering across unrelated seeds), and depth follows
  the view's own `--hops`. Recursion/cycles stop cleanly (a function already on the current call path
  isn't re-entered) instead of hanging.
- **`statediagram`**, a **Mermaid entity state machine**: guarded assignments to a status/state/stage field
  (`if order.status == Created: order.status = Paid`) become transitions, labeled with the method that
  makes them. Only *guarded* transitions are emitted: the source state must be established by a preceding
  comparison on the same field, so a diagram never claims a transition the code doesn't actually establish
  (an honest undercount, not a guess). Best with `--repo`, like `classdiagram`: `contextlake graph --repo
  acme/app --format statediagram`. A `--name`/`--node` seed can reach the state nodes (via the file that
  declares them) but not their transitions past the view's `--hops`; use `--repo` for the full picture.
  Multiple entities in view each get their own composite block; a single-entity view renders flat.
- **`erdiagram`**, a **Mermaid ER diagram** of `table`/`view` definitions and their foreign-key
  `references` edges, from the SQL DDL extractor (see [Index & Code Graph](index-code-graph.md)):
  `contextlake graph --repo acme/app --format erdiagram`. No attribute/column data (the extractor
  only captures `CREATE TABLE`/`VIEW` names and FK targets), so entities render as bare boxes with
  relationship lines, not column lists. A `REFERENCES` clause always points child-row to parent-row,
  so the parent is drawn on the "one" side of the notation. **Only sees raw `.sql` DDL** — an
  ORM-defined schema (SQLAlchemy, Entity Framework, TypeORM model classes, no literal `CREATE TABLE`
  text anywhere) renders an empty diagram with guidance, not a bug.
- **`deploymentdiagram`**, a **Mermaid flowchart** of Terraform/HCL `resource`/`data`/`module`
  definitions grouped by an inferred category (network/compute/storage/database/security/module),
  from the HCL extractor (see [Index & Code Graph](index-code-graph.md)): `contextlake graph --repo
  acme/infra --format deploymentdiagram`. Category is a keyword heuristic over the resource type prefix (e.g.
  `aws_security_group.web` -> security); `depends_on` edges reconstructed from `var.`/`module.`/
  type-name interpolation references draw the connections between resources. A single-category view
  renders flat (no subgraph wrapper). **Terraform-only** (HCL is the only IaC language the extractor
  parses): a repo with no `.tf` files renders an empty diagram with guidance, not a bug.
- **`graphml`**, the standard [GraphML](http://graphml.graphdrawing.org/) interchange format for
  [Gephi](https://gephi.org/)/[yEd](https://www.yworks.com/products/yed): `contextlake graph --repo
  acme/app --format graphml --output g.graphml`. Nodes/edges carry real attributes (kind, name, repo,
  file, line, lang / relation, confidence, weight) as GraphML `<data>` keys, so Gephi's own filter and
  color-by-attribute tools work directly against them — not just a bare shape.
- **`cypher`**, `CREATE` statements for a [Neo4j](https://neo4j.com/)/[FalkorDB](https://www.falkordb.com/)
  import: `contextlake graph --repo acme/app --format cypher --output g.cypher`, then `cypher-shell -f
  g.cypher` (or FalkorDB's own loader). Node labels come from `kind`, relationship types from `relation`
  — both backtick-quoted (contextlake's kind/relation vocabularies are open text, not a fixed enum, so
  quoting handles arbitrary values without a lossy sanitization pass into PascalCase/UPPER_SNAKE).
- **`json`**, the raw `{nodes, edges, meta}` for cytoscape / custom tooling (Gephi/yEd users want
  `--format graphml` instead — real typed attributes, not a bespoke shape to parse).

For interactive exploration of a large graph, `contextlake graph --serve` runs a local web UI where
clicking a node **expands** it (fetches its neighbours on demand) so you can walk the graph without
pre-rendering all of it.

## Composed namespace C4 diagram

`contextlake graph --c4` renders a different kind of view: a composed **C4-Context/Container** diagram over
the whole fleet, namespaces are the boundaries, repos are the containers inside them, and the aggregated
`depends_on`, HTTP `flow`, and event `flow` edges become the labeled inter-service connections (grouped by
flavor and weight, e.g. `http x3`). It renders graph data that `index`/`connect` already extracted, so it
runs fully offline and adds no new extraction pass. `--group-depth N` (default `1`) controls how deep into
the namespace path the boundaries are drawn, and `--repos <glob>` scopes the diagram to matching repos.
Because it only draws coupling the graph already resolved (weight-ranked), it doesn't invent links, and
folding event-flow in alongside HTTP keeps it from telling an HTTP-only half story:

```bash
contextlake graph --c4 --group-depth 2 --open       # HTML, open in the browser
contextlake graph --c4 --format dot > c4.dot        # clustered DOT, copy-pasteable
```

Output is chosen with `--format`: `html` (default, an interactive page with namespace boundaries as
compound nodes, written to `<store>/graphs/c4.html`), `dot` (Graphviz clustered DOT with `subgraph
cluster_*` boundaries), or `json` (the raw payload). `--format mermaid`, `classdiagram`, `sequencediagram`,
`statediagram`, `erdiagram`, and `deploymentdiagram` aren't supported with `--c4` (the command exits
with an error), and `--serve` doesn't apply either, the C4 view is a generated file, not a live server.

### C1: external systems

`--c4 --c1` adds a layer on top of the same view: one dashed box per distinct host an indexed repo
calls over HTTP that never resolves to any indexed repo's exposed route, connected by a
`calls_external x<weight>` edge, drawn outside every namespace boundary:

```bash
contextlake graph --c4 --c1 --group-depth 2 --open
```

**Deliberately unclassified.** contextlake can't tell a genuine third-party dependency (Stripe,
GitHub's API) apart from an internal service this fleet simply hasn't indexed yet — both look
identical here: an HTTP call whose target path matches no indexed repo's `exposes` route. Read the
box labels yourself; you'll recognize your own internal hosts. `--c1` requires `--c4` (it has no
meaning on its own) and needs no new extraction pass — the host was already captured at index time,
just never used until this view asks for it.

## See also

- [The dashboard](dashboard.md)
- [Index the code graph](index-code-graph.md)
- [Serve it to your editor](serve.md)
