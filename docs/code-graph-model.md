# The code graph model

## What the graph captures

Indexing builds a typed graph of your source. tree-sitter extracts files, classes, functions/methods,
interfaces, imports, an intra-repo **call graph**, and an **inheritance graph** (`inherits` edges for
`extends` / `implements` / base classes), so "what extends `BaseController`?" is one hop, and changing a
base class shows its subclasses in `blast_radius`. The diagram below imports its colors from the same
module `contextlake kb graph` and the dashboard render with, so node kinds match everywhere. Edge
relations mostly do: 10 of them carry a dedicated hue (`calls`, `imports`, `contains`, `depends_on`,
`publishes`, `tracked_by`, `documented_by`, `flow`, `exposes`, `calls_http`), while `inherits` and
`references` ride the neutral default edge color, and `reads`, `writes`, `uses` and
`transitions_to`, which this page documents further down, are real edges the diagram does not
show at all:

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/graph-vocabulary.png" alt="The knowledge-graph vocabulary: all 52 node kinds in 10 bands (symbols, containers, service surfaces, data model, infrastructure, presentation, configuration, documents, cross-source, boundary) each with their color, and 12 edge relations (calls, imports, contains, depends_on, publishes, flow, exposes, calls_http, tracked_by, documented_by, and inherits and references on the neutral default) each with their color, plus a confidence key: solid = extracted, dashed = inferred, dotted = ambiguous." width="820">
</p>

### Languages

tree-sitter covers **27 languages across 25 grammars** (TypeScript and TSX share one grammar), and the
parser registry is pluggable.

Depth differs across three tiers, and a single number would hide it.

| Tier | Languages | What you get |
| --- | --- | --- |
| Full | C, C++, JavaScript, TypeScript, TSX, Python | definitions, imports, calls, **plus** module-level variables and class fields |
| Definitions | C#, Go, Java, Kotlin, Ruby, Rust, PHP, Scala, Swift, Dart, Zig, Perl, Bash, Elixir | definitions, imports and calls |
| Referenceable names | CSS, HTML, Nix, Make, Dockerfile | the names other files refer to: CSS class, id and element selectors; HTML element ids; Nix attributes; Make targets; Dockerfile build stages and the images they build on |
| Components | Svelte, Vue | each `<script>` parsed as JavaScript and each `<style>` as CSS, reported at their line in the file |

Most files reach a grammar by extension. A build file has none, so `Makefile`, `GNUmakefile`,
`Dockerfile` and `Containerfile` are routed by name instead, matched on the stem before the first
dot: `Makefile.am` and `Dockerfile.prod` both hit, and `MyMakefile` does not. Make targets are the
names a person types at a shell and a CI job invokes, which makes them the shortest answer to
"what does this project expect of itself". Make's own special targets (`.PHONY`, `.SUFFIXES`) are
not extracted, and neither are variables.

A Dockerfile yields its build stages and the external images it builds on, told apart from each
other: in `FROM builder AS test` the base is a stage declared earlier in the same file, not an
image anybody pulls. The `COPY --from=` reference back to a stage is **not** extracted, because a
stage name is file-local and resolving it across files would link two unrelated `builder` stages
to each other. Dockerfile's grammar is an [optional extra](installing.md), `[kb-dockerfile]`; without
it, Dockerfiles are skipped and the run names the extra rather than reporting the file as
unsupported.

On one public JavaScript tree the full tier's extra kinds were 463 of its 783 nodes. Two partial cases
are worth naming rather than leaving to be discovered: Zig declares a struct as
`const Engine = struct {...}`, a variable declaration rather than a definition node, so Zig types are
not extracted; and an HTML `class=` attribute is a *use* of a name a stylesheet defines, so it is not
yet emitted as anything.

| Ecosystem | Languages |
| --- | --- |
| JVM | Java, Kotlin, Scala |
| .NET | C# |
| Web / JS | JavaScript, TypeScript, TSX |
| Systems | C, C++, Rust, Go |
| Scripting | Python, Ruby, PHP |

Frameworks are indexed through their base language: **React / Next.js / Node.js** are JS/TS(X),
**Angular** is TS (its templates are HTML), and **.NET** is C#.

Missing yours? "Pluggable" is meant literally: a language is a grammar package plus a few table
entries. [Adding a language](adding-a-language.md) is the ordered recipe, with the verification
commands that prove a new grammar works.

#### C++ completeness

A method defined outside its class (`ReturnType Class::method(...) { ... }`, at any `::` qualification
depth) resolves to a `method` contained by its class, repo-wide -- not just the file it's declared in --
so an out-of-line definition in one file still shows up under its class even when the class itself is
declared in a different header. A `namespace { ... }` block is its own containing node, the same way a
class or file is. Header files (`.h`) are parsed as C++, so a class declared in a header and defined in a
matching `.cpp` is visible as one unit rather than the class half going missing. This parsing choice is
kept transparent to `languages` filtering: `.h` files are indexed whenever either `"c"` or `"cpp"` is
enabled, since C and C++ headers are shared infrastructure -- restricting `kb.toml`'s `languages` to just
`["c"]` still indexes `.h` files, no need to list both. An `#ifdef`/`#else` (or `#ifndef`/`#else`)
pair -- two definitions of the same method in different branches of the *same* conditional -- collapses
into one node instead of appearing as duplicate, ambiguous call targets. A bare `#ifndef` header guard with
no `#else` does **not** trigger this: both copies would sit in the same (only) branch, so nothing collapses,
and genuinely distinct overloads anywhere are kept as separate definitions, never merged into one. See
[Health and maintenance](indexing-the-code-graph.md#health-and-maintenance) above: `doctor` flags any C/C++ shard indexed before
these fixes landed, as a signal to re-index.

### Infrastructure: Terraform / HCL

`.tf` files build an infrastructure dependency graph: `resource` / `data` / `variable` / `output` /
`module` / `local` definitions with `depends_on` edges resolving `var.` / `module.` / `data.` / resource
references across files in a repo. `resource` nodes are semantically searchable.

Resolution is repo-wide, so a block address defined identically in separate root-module directories (for
example `environments/prod` and `environments/staging`) surfaces as an `AMBIGUOUS` edge; directory-scoped
resolution is a future refinement. Render it with `contextlake kb graph --repo <repo> --format
deploymentdiagram` (a Mermaid flowchart grouped by inferred category: network/compute/storage/
database/security/other/module, where `other` catches any resource type none of the keyword lists
claims), see [Visualize](visualizing-the-graph.md).

### Databases: SQL DDL

`.sql` files build a referential graph: `table` / `view` / `procedure` definitions with `references` edges
from foreign-key `REFERENCES` clauses, resolved across files in a repo. `table` and `view` nodes are
semantically searchable.

It uses a regex DDL extractor (the fleet's T-SQL/PL-SQL defeats a tree-sitter AST), so it targets the
high-value defs and FK references and is a **deliberate undercount**. Render it with
`contextlake kb graph --repo <repo> --format erdiagram` (a Mermaid ER diagram), see [Visualize](visualizing-the-graph.md).

**Measured, not just asserted:** `tests/kb/fixtures/sql/` is a small, synthetic, hand-labelled
orders/customers/inventory corpus with a checked ground truth of every FK a human reading the DDL would
call real (`expected_edges.json`, 13 edges); `tests/kb/test_sql_fixture_corpus.py` scores the parser's
emitted `references` edges against it on every CI run, and asserts this page still quotes what it
measures. Current numbers on that corpus: **precision 1.00 (9 true positives / 0 false positives),
recall 0.69 (9 / 13 ground-truth edges found)**, a small, hand-built corpus, not a claim about the whole
fleet, but real and reproducible. Two documented gap classes account for the four missed edges, both by
design, not bugs: a **self-referencing FK** (`referred_by`/`parent_category_id`-style hierarchies) is
dropped because the extractor excludes `target == name`, and an FK **attached via a separate
`ALTER TABLE ... ADD CONSTRAINT`** statement is never captured because the scope tracker only scans
`REFERENCES` inside a `CREATE TABLE`'s own text span. Precision was 0.90 on this corpus until the
extractor learned to blank out `--` and `/* */` comments before matching: its one false positive was a
commented-out `REFERENCES` line, dead DDL history that resolved into a real-looking edge because the
table it named still existed elsewhere in the repo. That case is now pinned as a negative in the corpus
test. These are the numbers to distrust a graph `INFERRED` SQL edge by, and
the floors in the corpus test are meant to be ratcheted up as the extractor improves, not treated as a
target already met.

### Data contracts: XML Schema

`.xsd` files build a schema graph: every **global** component becomes a node, and every name one
component gives another becomes a `references` edge, resolved across files in a repo. A global
`xs:element` is a `schema_element`, because it is the name a message, a document root or a SOAP body
actually carries and therefore the name a person searches for. A global `complexType`, `simpleType`,
`group`, `attributeGroup` or `attribute` is a `schema_type`, with which one it is recorded as the
`schema_construct` attribute rather than as five kinds. Both are semantically searchable.

References come from `type=`, `base=`, `ref=`, `itemType=` and `memberTypes=`. The namespace prefix is
stripped first, since it is a per-file alias for a namespace URI: `tns:PartyType` in one file and the
`<xs:complexType name="PartyType">` that defines it in another land on one node. The built-in
datatypes (`xs:string`, `xs:dateTime`, and the rest) are not references and are dropped, since they
name nothing the graph holds.

Three deliberate limits:

- **Only global components are nodes.** A locally-scoped `<xs:element name="Id"/>` nested inside a
  complex type has no name anything can refer to, and minting one would put thousands of identical
  `Id` nodes into a single schema set. A reference found inside one is attributed to the innermost
  global component enclosing it.
- **Schema names never resolve onto code symbols.** `schema_type` and `schema_element` are their own
  kinds rather than a reuse of `struct` and `typedef`, because reference resolution is by name across
  the repo: sharing a kind with C++ would let `type="Address"` resolve, confidently and with nothing
  reporting the guess, onto an unrelated `struct Address`.
- **`.xsd` never reaches the XML config scanner.** `name` is one of that scanner's key attributes, so
  a schema sent there files every component as a `config_key` setting.

Like the SQL extractor, this is a scanner rather than a tree parser, and for the same three reasons:
entity expansion on untrusted mirrored input, hand-edited files a strict parser abandons whole, and
line numbers the stdlib tree parsers do not report.

### Transformations: XSLT

`.xsl` and `.xslt` files build a stylesheet call graph. `<xsl:call-template name="X"/>` is a call
by name, so a stylesheet has one, and it was not in the graph at all before. Named templates,
match templates and `xsl:function` declarations are `function` nodes; top-level `xsl:variable`
and `xsl:param` declarations are `global_variable` nodes, which is a stylesheet's configuration
surface. `$name` reads become `uses` edges, attributed to the template they sit in.

A match template has no name to be called by, so its match pattern is its node name, with the
pattern and any `mode` recorded as attributes. It is not an identifier; it is the only handle
such a template has, and one with no handle cannot be pointed at from anywhere.

Unlike XML Schema, this mints no new node kinds, and the reason is worth stating because the two
decisions look inconsistent. Schema references resolve on name alone across the whole repo, so a
schema type sharing `struct` with C++ would collide. Calls and variable reads are filtered by
**language family** first, and `xsl` is its own family, so an `xsl:template` named `format`
cannot resolve onto a Python `format`. The isolation is already there.

Two limits, stated rather than left to be discovered. `<xsl:import>` and `<xsl:include>` are not
edges: the target is a relative href, and the only honest edge would point at a file node that
may not exist in the repository. XPath calls to an `xsl:function` are not edges either: the
function is a node, but the call sits inside a `select` expression and reading that needs an
XPath parser.

### Embedded SQL: Pro\*C

`.pc` files are C with `EXEC SQL` statements written into the source. They are read twice, and
the split is the whole design: the **C parse** sees the file with every `EXEC SQL` statement
blanked out, and the **dataflow pass** sees it intact.

The mask exists because of a measurement. Handed straight to the C grammar, `EXEC SQL INCLUDE
SQLCA;` and `EXEC SQL BEGIN DECLARE SECTION;` parse as declarations, producing `global_variable`
nodes named `SQLCA`, `SQL` and `SECTION` -- names of things that do not exist, in a kind that
bare identifiers elsewhere in the repo resolve `uses` edges onto. Blanking is
length-and-newline-preserving, so every line number the C parse cites is still the real one, and
only the statements are blanked: the host variables declared between the declare-section markers
are ordinary C and stay in the graph.

The dataflow pass needs the SQL, because the SQL is the point: which tables the file reads and
writes is what an `EXEC SQL` statement is there to say. It already normalises table names through
the same recipe `.sql` gives its `table` nodes, so an `EXEC SQL SELECT ... FROM CUSTOMERS` and a
`CREATE TABLE dbo.[Customers]` in another file land on one node without a second copy of that
rule existing anywhere.

`.pc` follows C for language filtering rather than having a flag of its own: `--languages c`
selects it, `--languages python` does not.

### Architecture decisions (ADRs)

A repo's own decision records become first-class `adr` nodes in that repo's shard. The match is any
`.md` file with a directory segment named `adr`, `adrs` or `decisions` anywhere in its path, case
insensitively, so `docs/adr/`, `docs/decisions/`, `decisions/`, `adrs/` and `architecture/adr/` all
qualify, one file per decision. Each node takes its
title from the file's first `# ` heading (or the filename otherwise). Unlike connector-sourced
content (`enrich`/`connect`, external systems reached over the network), an ADR is authored, checked
into the repo's own git history: a recorded fact, not something to attribute or hedge on. `adr` nodes
are semantically searchable, and their content feeds into [wiki generation](generating-the-wiki.md) as a
grounded "Recorded decisions" section, cited alongside the repo's other extracted facts. No column
data, no edges to other graph nodes: an ADR mentioning a class by name isn't a verified reference the
way an import or call site is, so nothing is inferred from that mention.

### Entity state machines

Guarded assignments to a status/state/stage field (`if order.status == Created: order.status = Paid`)
become `transitions_to` edges between `state` nodes, labeled with the method that makes the transition.
Only *guarded* transitions are emitted: the source state must be established by a preceding comparison on
the same field, so a diagram never claims a transition the code doesn't actually establish. Python, JS/TS,
and C# (regex, every edge `INFERRED`). Render with `contextlake kb graph --repo <repo> --format statediagram`
(a Mermaid entity state machine), see [Visualize](visualizing-the-graph.md).

`transitions_to` is deliberately **not** in `impact`'s default relation set: unlike a table schema a
query depends on, a state value is rarely a thing other code breaks against when it changes (renaming an
enum member is a language-level rename, not a graph-discoverable break); pass `--relation transitions_to`
explicitly if you do want that walk.

### Intra-repo dataflow: reads and writes

Application code querying a table or view it never explicitly imports still shows up in the graph: a
literal `SELECT ... FROM` / `INSERT INTO` / `UPDATE ... SET` / `DELETE FROM` in a string (any language,
SQL text looks the same embedded anywhere) becomes a `reads` or `writes` edge from the file to the
`table`/`view` node the SQL DDL extractor already found, resolved by name across the whole repo the same
way an FK `references` edge is. A query against a table this repo never defines is an honest miss, not a
guessed link. `reads`/`writes` are in `impact`'s default relation set, so `contextlake kb impact <table>`
answers "what code touches this table" out of the box.

### Constants: their value, and every place it is read

A constant node records the declaration it was written with, so the graph answers what a value
actually is and not only that a name exists: `MAX_RETRY = 3`, `PAGE_SIZE = 50`,
`#define TIMEOUT 30`. It is stored as written, collapsed to one line and capped, and it is called
a declaration rather than a value because nothing has been parsed out of it.

Each place a constant's value is read becomes a `uses` edge from the file to the constant,
citing its own line. Like `calls`, this is stored **once per occurrence**, so "where is this read"
is answerable exhaustively rather than as one edge with an arbitrary line attached.

What is deliberately not a use: the declaration itself, a write (`TOTAL += 1`, `global TOTAL`),
an import, and an attribute (`cfg.MAX_RETRY` reads an attribute of `cfg`). A bare name is also
never matched against a class field, because a data member is reached as `self.x` or `this->x` and
a bare `x` is a local: on one public C++ tree, allowing it attributed 588 reads of a loop counter
to a class member of the same name.

Where a name has several definitions, the edge is marked ambiguous rather than pointed at a guess,
and anything reporting a constant's use count should filter on confidence. `uses` is in `impact`'s
default relation set, so `contextlake kb impact MAX_RETRY` answers "what depends on this value".

### Web topology: endpoints and routes

Two web-topology layers sit on top of the definitions.

**HTTP endpoints** a repo exposes or calls become shared `endpoint` nodes that join across repos into
`flow` edges, from the caller repo to the exposer repo. Detection is regex-based and framework-targeted,
so read the list as what is actually matched:

- **Python**: FastAPI and Flask decorator routing (`@app.get("...")`, `@router.route("...")`).
- **JavaScript and TypeScript**: the Express-shaped call form,
  `app`/`router`/`api`/`server` `.get|.post|.put|.delete|.patch("...")`. Fastify code that happens to
  use one of those variable names is caught incidentally. **NestJS is not supported**: its
  `@Controller` / `@Get()` decorator routing matches nothing here.
- **C#**: minimal-API `MapGet`/`MapPost`/… **and** attribute routing, `[HttpGet("...")]` and
  `[Route("...")]`, so a conventional MVC or Web API controller is picked up too.
- Next.js App Router `route.ts` handlers, via the file convention.

Every one of these edges is `INFERRED` by construction, a likely undercount rather than an assertion.

**Frontend routes** become repo-scoped, embeddable `route` nodes from three frameworks:

| Framework | Source | Normalization rules |
| --- | --- | --- |
| Next.js App Router | `app/**/page.*` file convention | route groups `(name)` dropped; dynamic `[id]` / `[...slug]` collapsed to `{}` |
| React Router | flat JSX `<Route path=...>` and the data-router object form `createBrowserRouter([{ path, Component, children, index }])` | `index: true` resolves to the parent path |
| Angular | `Routes` tables | `redirectTo` skipped; lazy `loadChildren` captured as the mount path |

The object-literal forms use a tree-sitter AST walk anchored on the route-table container (a
`Routes`-typed declarator, or the array argument to `RouterModule.forRoot` / `RouterModule.forChild` /
`provideRouter` / `create*Router`), so
nested `children` compose into full paths and bare `{path:...}` config objects are never mis-read as routes.

> [!NOTE]
> **Skipped rather than guessed.** Not yet extracted: Luigi navigation configs, Angular lazy
> `loadChildren` sub-trees, React `loader` / `lazy`, realtime / WebSocket channels, templates, and
> stylesheets.

> [!NOTE]
> **Shared nodes aren't owned by one repo.** An `endpoint`/`topic`/`module`/package node's id
> doesn't encode a repo: two repos that both import `requests`, call the same route, or publish
> to the same topic produce the identical node, which the store dedupes to one row. Its `repo`
> reads as a pseudo-repo (`"(shared)"`, `"(packages)"`) rather than any one real repo, by design:
> which repos actually touch it is a question the cross-repo edges answer, not the node itself.

### Manifests and cross-repo dependencies

Indexing also reads manifests (`pyproject.toml`, `package.json`, `*.csproj`, `pom.xml`) to build a
**cross-repo dependency graph** through shared package nodes. Agents traverse all of this over MCP, from
finding a definition to cross-repo `blast_radius` ("what could break if I change this"); see
[the full tool list under Serve](serving-over-mcp.md).

Each `depends_on` edge records what the manifest actually says, not just the package name:

| On the edge | What it holds |
| --- | --- |
| `attrs["constraint"]` | the version as written, unparsed: `>=1.9.0`, `^4.17.1`, `[redis]>=5.0`, or a whole environment marker. Absent, not empty, when the manifest pinned nothing. |
| `attrs["group"]` | `runtime`, `dev`, `peer`, `optional:<extra>`, or `group:<name>` for a PEP 735 dependency group, so an extra a user opts into is distinguishable from a dependency the package cannot start without. |
| `provenance.source_line` | the line the dependency was declared on, not the top of the file. |

The group vocabulary is the same across all four ecosystems, so nothing reading the graph needs to
know which one it came from: Maven's `<scope>test</scope>` and npm's `devDependencies` both read as
`dev`. Nothing interprets a constraint. Deciding whether an installed version satisfies `^4.17.1` is
a package manager's job, and the author's own text is the honest record of what was chosen.

The same change-impact walk is a one-liner from the shell: `contextlake kb impact <symbol> [--hops N]` lists
what calls / depends on a node, no editor needed. When a symbol name (e.g. `Node`, `Catalog`) is defined in
more than one repo, `impact` lists the candidates and you narrow it with `--repo <repo>` rather than
getting a silent best-guess.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-impact.png" alt="contextlake kb impact charge output: changing charge in acme/catalog-api affects place_order at hop 1 via a calls edge, tagged inferred, showing hop distance, relation, and confidence for each affected node." width="820">
</p>
