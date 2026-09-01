# The code graph model

Every node kind and edge kind `contextlake kb index` emits, and what each one is derived from.
This is the vocabulary the graph is queried in, so read it when a query returns nothing and you
need to know whether the thing you asked about is modelled at all.

After the general code model and its language table, eleven sections cover the sources the
indexer reads beyond ordinary program text: Terraform, SQL DDL, XML Schema, XSLT, embedded SQL,
decision records, entity state machines, intra-repo dataflow, constants, web topology and
manifests. Each names what it extracts and, where the extraction is inferred rather than
declared, says so.

## What the graph captures

Indexing builds a typed graph of your source. tree-sitter reads each file and pulls out:

- files, classes, functions and methods, interfaces, and imports
- an **intra-repo call graph**, so you can ask who calls what
- an **inheritance graph** (`inherits` edges for `extends`, `implements`, and base classes)

That last one means "what extends `BaseController`?" is a single hop, and changing a base class
shows its subclasses in `blast_radius`.

The diagram below takes its colours from the same module `contextlake kb graph` and the
dashboard use, so node kinds look the same everywhere. Edge relations mostly match too:

- **10 have their own colour**: `calls`, `imports`, `contains`, `depends_on`, `publishes`,
  `tracked_by`, `documented_by`, `flow`, `exposes`, `calls_http`
- **2 use the default grey**: `inherits` and `references`
- **4 are real but not drawn**: `reads`, `writes`, `uses` and `transitions_to`. This page covers
  them further down.

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

Most files reach a grammar by their extension. Build files have none, so they are matched by
name instead: `Makefile`, `GNUmakefile`, `Dockerfile` and `Containerfile`.

The match runs on the stem before the first dot. `Makefile.am` and `Dockerfile.prod` both hit.
`MyMakefile` does not.

Make targets are worth extracting because they are the names a person types at a shell and a CI
job runs. That makes them the shortest answer to "what does this project expect of itself".

Not extracted: Make's own special targets (`.PHONY`, `.SUFFIXES`), and variables.

A Dockerfile gives you its build stages and the external images it builds on, kept apart. In
`FROM builder AS test`, `builder` is a stage declared earlier in the same file, not an image
anyone pulls.

`COPY --from=` references are **not** extracted. A stage name is local to its file, so resolving
those across files would wrongly link two unrelated `builder` stages.

The Dockerfile grammar is an [optional extra](installing.md), `[kb-dockerfile]`. Without it,
Dockerfiles are skipped and the run tells you which extra to install, rather than calling the
file unsupported.

On one public JavaScript tree, the full tier's extra kinds were 463 of its 783 nodes.

Two gaps, named here so you do not have to find them yourself:

- **Zig types are not extracted.** Zig declares a struct as `const Engine = struct {...}`, which
  is a variable declaration, not a definition node.
- **HTML `class=` attributes produce nothing yet.** A `class=` is a *use* of a name some
  stylesheet defines, and that link is not emitted.

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

C and C++ get extra handling, because both languages spread one thing across several files.

- **A method defined outside its class** (`ReturnType Class::method(...) { ... }`, at any `::`
  depth) is attached to its class across the whole repo, not just the file it sits in. So an
  out-of-line definition still appears under its class, even when the class is declared in a
  different header.
- **A `namespace { ... }` block is its own container**, the same as a class or a file.
- **Headers (`.h`) are parsed as C++.** A class declared in a header and defined in a matching
  `.cpp` shows up as one unit, instead of the class half going missing.
- **`.h` files are indexed if either `"c"` or `"cpp"` is enabled.** C and C++ headers are shared,
  so setting `languages = ["c"]` in `kb.toml` still picks them up. You do not need to list both.
- **Two definitions in opposite branches of one `#ifdef`/`#else` collapse into one node.**
  Without that, they would look like duplicate call targets.

Two things that deliberately do **not** collapse:

- A bare `#ifndef` header guard with no `#else`. Both copies sit in the same branch, so there is
  nothing to merge.
- Genuine overloads, anywhere. They stay separate definitions.

If a C or C++ shard was indexed before these fixes landed, `doctor` flags it so you know to
re-index. See [Health and maintenance](indexing-the-code-graph.md#health-and-maintenance).

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

**Measured, not asserted:** `tests/kb/fixtures/sql/` is a small, synthetic, hand-labelled
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

`.xsd` files build a schema graph. Every **global** component becomes a node. Every name one
component gives another becomes a `references` edge, resolved across files in the repo.

Two node kinds:

- **`schema_element`** for a global `xs:element`. This is the name a message, a document root or
  a SOAP body actually carries, so it is the name people search for.
- **`schema_type`** for a global `complexType`, `simpleType`, `group`, `attributeGroup` or
  `attribute`. Which one it was is kept in the `schema_construct` attribute, rather than
  splitting these into five separate kinds.

Both are semantically searchable.

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
- **Six extensions reach the config scanner**, not just `.xml`: `.config`, `.props`, `.targets`,
  `.settings` and `.plist` carry settings too, and `.config` is the canonical .NET one. Measured
  across 660 repositories, 1,023 `.config` files were contributing nothing while being exactly the
  files "where is this setting defined" is asked about.
- **`.resx`, project files and `.svg` are excluded on purpose.** `.resx` is localisation, and at
  roughly 22.9 settings per file across 3,963 files it would add about 91,000 nodes fleet-wide for
  translated labels nobody asks that question about. `.csproj`, `.vbproj` and `.nuspec` are build
  definitions, which the manifest extractor already owns along with the `depends_on` relation that
  belongs between a build file and a package. `.svg` is XML-shaped and is graphics.
- **A settings extension whose file is not XML costs nothing.** The scanner is a regex over markup,
  so an INI-style `.config` yields no settings rather than raising.

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

This adds no new node kinds, unlike XML Schema. The two choices look inconsistent, so here is
why they differ.

- Schema references resolve on **name alone**, across the whole repo. A schema type called
  `struct` would collide with C++.
- Calls and variable reads are filtered by **language family** first. `xsl` is its own family,
  so an `xsl:template` named `format` cannot resolve onto a Python `format`.

The isolation is already there, so no new kinds are needed.

Two limits, stated rather than left to be discovered. `<xsl:import>` and `<xsl:include>` are not
edges: the target is a relative href, and the only honest edge would point at a file node that
may not exist in the repository. XPath calls to an `xsl:function` are not edges either: the
function is a node, but the call sits inside a `select` expression and reading that needs an
XPath parser.

### Embedded SQL: Pro\*C

`.pc` files are C with `EXEC SQL` statements written into the source. They are read twice, and
the split is the whole design: the **C parse** sees the file with every `EXEC SQL` statement
blanked out, and the **dataflow pass** sees it intact.

The mask exists because of a measurement.

Fed straight to the C grammar, `EXEC SQL INCLUDE SQLCA;` and `EXEC SQL BEGIN DECLARE SECTION;`
parse as declarations. That produces `global_variable` nodes called `SQLCA`, `SQL` and
`SECTION`. None of those things exist, and bare identifiers elsewhere in the repo would resolve
`uses` edges onto them.

Two properties keep the masking safe:

- It preserves length and newlines, so every line number the C parse reports is still real.
- It blanks only the statements. Host variables declared between the declare-section markers are
  ordinary C and stay in the graph.

The dataflow pass still reads the SQL, because the SQL is the whole point. What an `EXEC SQL`
statement exists to say is which tables the file reads and writes.

It normalises table names with the same rule `.sql` files use for their `table` nodes. So an
`EXEC SQL SELECT ... FROM CUSTOMERS` and a `CREATE TABLE dbo.[Customers]` in another file land on
one node, and that rule lives in exactly one place.

`.pc` follows C for language filtering rather than having a flag of its own: `--languages c`
selects it, `--languages python` does not.

### Architecture decisions (ADRs)

A repo's own decision records become `adr` nodes in that repo's shard, one node per file.

**What counts as an ADR file:** any `.md` file with a directory named `adr`, `adrs` or
`decisions` anywhere in its path, matched case-insensitively. So `docs/adr/`,
`docs/decisions/`, `decisions/`, `adrs/` and `architecture/adr/` all qualify.

Each node takes its title from the file's first `# ` heading, or from the filename if there is
none.

**Why ADRs are treated differently from connector content.** An ADR is written by hand and
checked into git. It is a recorded fact, so it needs no attribution or hedging. Content pulled
from Jira or Figma comes over the network from a system this repo does not own, so it does.

What you get:

- `adr` nodes are semantically searchable.
- Their content feeds [wiki generation](generating-the-wiki.md) as a grounded "Recorded
  decisions" section, cited next to the repo's other facts.

What you do not get: edges to other nodes. An ADR that mentions a class by name is not a
verified reference the way an import or a call site is, so nothing is inferred from it.

### Entity state machines

A guarded assignment to a status, state or stage field becomes a `transitions_to` edge between
`state` nodes, labelled with the method that makes the change. For example:

    if order.status == Created: order.status = Paid

**Only guarded transitions are emitted.** The source state has to be established by a comparison
on the same field just before. That way a diagram never claims a transition the code does not
actually make.

Supported in Python, JS/TS and C#. Detection is regex-based, so every edge is marked `INFERRED`.

Render it with `contextlake kb graph --repo <repo> --format statediagram`, which produces a
Mermaid state diagram. See [Visualizing the graph](visualizing-the-graph.md).

`transitions_to` is deliberately **not** in `impact`'s default relation set: unlike a table schema a
query depends on, a state value is rarely a thing other code breaks against when it changes (renaming an
enum member is a language-level rename, not a graph-discoverable break); pass `--relation transitions_to`
explicitly if you do want that walk.

### Intra-repo dataflow: reads and writes

Application code that queries a table it never imports still shows up in the graph.

A literal `SELECT ... FROM`, `INSERT INTO`, `UPDATE ... SET` or `DELETE FROM` inside a string
becomes a `reads` or `writes` edge, from the file to the `table` or `view` node the SQL DDL
extractor already found. This works in any language, because embedded SQL text looks the same
wherever it sits. Names resolve across the whole repo, the same way a foreign-key `references`
edge does.

A query against a table this repo never defines is left unlinked. That is an honest miss, not a
guessed link.

`reads` and `writes` are in the default relation set for `impact`, so
`contextlake kb impact <table>` answers "what code touches this table" with no extra flags.

### Constants: their value, and every place it is read

A constant node records the declaration it was written with, so the graph answers what a value
actually is and not only that a name exists: `MAX_RETRY = 3`, `PAGE_SIZE = 50`,
`#define TIMEOUT 30`. It is stored as written, collapsed to one line and capped, and it is called
a declaration rather than a value because nothing has been parsed out of it.

Each place a constant's value is read becomes a `uses` edge from the file to the constant,
citing its own line. Like `calls`, this is stored **once per occurrence**, so "where is this read"
is answerable exhaustively rather than as one edge with an arbitrary line attached.

These are deliberately **not** counted as a use:

- the declaration itself
- a write, such as `TOTAL += 1` or `global TOTAL`
- an import
- an attribute, since `cfg.MAX_RETRY` reads an attribute of `cfg`

A bare name is also never matched against a class field. A data member is reached as `self.x` or
`this->x`, so a bare `x` is a local variable. On one public C++ tree, allowing that match
attributed 588 reads of a loop counter to a class member with the same name.

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

Each `depends_on` edge records what the manifest actually says, not only the package name:

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

## The graph, on this page

<img class="shot" src="graph.jpg" width="1360" height="834" data-embed="graph-embed.html"
  alt="The contextlake graph visualizer running on contextlake's own code: symbols laid out as a node graph with per-kind glyphs, a kind legend, a search field and a minimap in the corner.">
<p class="shot-cap">The model on this page, drawn. Node colour is the kind, edge colour is the relationship, and edge style is the confidence. It is the shipped visualizer, not a recording, and it runs
offline with no network calls.</p>

## See also

- [Indexing the code graph](indexing-the-code-graph.md)
- [Asking the graph](asking-the-graph.md)
- [Adding a language](adding-a-language.md)
- [Architecture and internals](internals.md)
