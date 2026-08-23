# Adding a language

contextlake reads source with tree-sitter, and a language it understands is a handful of table entries
in one file plus a grammar package. This page is the ordered recipe: the nine edits, in the order to
make them, the parts of the repo to deliberately leave alone, and the commands that prove your grammar
works before you open a pull request.

The parser's own module docstring states the contract: "adding a language = registering its grammar
loader, file extensions, and a query in the tables below; the rest of the pipeline is
language-agnostic". That is true of the graph core. The four steps around it (the dependency, the
tests, the published count, and the regenerated site) are what turns a working parser into a shipped
language.

## Prerequisites

- **A development checkout**, set up as [CONTRIBUTING.md](../CONTRIBUTING.md#getting-set-up)
  describes. The knowledge layer is an optional extra, so you need `pip install -e ".[dev,kb]"`, not
  the core install.
- **A tree-sitter grammar on PyPI** for your language, published as a `tree-sitter-<lang>` wheel that
  exposes a `language()` function. If no maintained wheel exists, that is the blocker to solve first;
  contextlake does not vendor or compile grammars.
- **A language id**, which is the key every table below is keyed on and the value written onto every
  node's `lang` field. Use the plain lowercase language name (`kotlin`, `scala`, `php`).
- **A sample file** in your language, twenty or thirty lines covering a class, a method, a free
  function, an import, a call, and a base type. You will parse it repeatedly while writing the query.

One check before you commit to an id. The parser cache and the extraction-kind registry are both keyed
by string, and they were once the same variable, so a language named after a node kind silently
returned the wrong object. Confirm your id is not already a kind:

```bash
python -c "from contextlake.kb.kinds import KIND_REGISTRY; print('mylang' in KIND_REGISTRY)"
```

`False` is the answer you want. `data`, `local`, `output`, `state`, `table`, `variable` and `view` are
among the names already taken.

## The nine edits

Five of the nine are in one file, `src/contextlake/kb/parse.py`, and three of those five must land
together or the language fails one file at a time without failing the run. Read
[A partial registration is a silent skip](#a-partial-registration-is-a-silent-skip) before you start,
because it is the reason this list is ordered the way it is.

### 1. Declare the grammar dependency

In `pyproject.toml`, find the `kb` optional extra and its tree-sitter block, commented "Code parsing:
the tree-sitter runtime plus per-language grammar packages". Add your grammar to that list:

```toml
"tree-sitter-<lang>>=X.Y",
```

Pin the floor to the version whose `language()` entry point you actually import against, not to a
house number. The existing pins vary for that reason. Nothing else needs editing: the `kb-full` extra
is defined in terms of `kb`, so it inherits the grammar, and `make install` runs the same
`pip install -e ".[dev,kb]"` that CI does.

### 2. Map the file extensions

In `parse.py`, add one `LANG_BY_EXT` entry per extension your language uses. Several extensions may
map to one id, the way `.kt` and `.kts` both map to `kotlin`.

If your language is a superset of one already covered, reusing the existing id is a legitimate choice
that costs nothing else on this list. `.h`, `.cu` and `.cuh` are all read with the C++ grammar for
exactly that reason, and the in-table comments explain what each of those decisions does and does not
capture. Write the same kind of note if you make the same kind of call.

Nothing downstream needs an edit. The extension filter behind `kb.toml`'s `languages` setting, the
router that decides which extractor owns a file, and the walk that stamps `SourceFile.lang` all read
this one table.

### 3. Map node types to kinds

`_DEF_TYPES[lang]` maps a tree-sitter node type to a contextlake node kind. Every value must be a key
of `KIND_REGISTRY`; a value that is not fails the parity test in step 7 with a named error.

Collapsing several syntactic forms into one kind is normal and precedented. Go's `type_spec` becomes
`struct`, Ruby's `module` becomes `class`, Kotlin's `object_declaration` becomes `class`, and each of
those carries a comment saying why. Write yours the same way.

One subtlety is worth knowing before you choose: a function defined inside a definition whose kind is
exactly `class` is promoted from `function` to `method`. Map your class-like node to anything else and
its members stay kinded `function`, which is a quiet quality loss rather than an error.

If your language is an alias of one already registered, alias the row rather than retyping it, the way
`_DEF_TYPES["tsx"] = _DEF_TYPES["typescript"]` does.

### 4. Write the tree-sitter query

`_QUERIES[lang]` holds a tree-sitter query string, compiled lazily on first use. **The capture-name
vocabulary is closed.** The parser reads exactly five capture names and ignores everything else,
without a warning:

| Capture | What it produces |
| --- | --- |
| `@def` | a definition node, kinded through `_DEF_TYPES` |
| `@def_qi` | a C++-style out-of-line qualified definition (`Type Class::method()`). No other language needs it |
| `@import` | a `module` node and an `imports` edge |
| `@call` | an unresolved call reference, resolved repo-wide after every file is parsed |
| `@base` | an unresolved `inherits` reference |

Write one pattern per real syntactic shape rather than one clever pattern that tries to cover several.
The Kotlin query carries two separate `@base` patterns because a Kotlin supertype list holds two
different shapes, a constructor invocation and a bare interface name, and one pattern would have
silently dropped the other.

Two normalisations are already applied to every `@base` capture, so you do not need to handle either:
a generic supertype such as `Comparable<Order>` and a dotted one such as `com.example.Base` both
reduce to the bare name. If your language has neither shape, the strips are no-ops.

Alias a shared query the same way you aliased the kinds: `_QUERIES["tsx"] = _QUERIES["typescript"]`.

### 5. Load the grammar

`_language()` is an explicit `if`/`elif` chain of lazy imports ending in
`raise ValueError(f"unsupported language: {lang}")`. Add a branch:

```python
elif lang == "<lang>":
    import tree_sitter_<lang> as g
    fn = g.language
```

**Keep the import inside the function.** The laziness is load-bearing: the core tier of contextlake
must work with no tree-sitter installed at all, and two tests police that boundary.

Read the grammar package before you write `fn = g.language`. Some expose more than one entry point:
the TypeScript wheel provides `language_typescript` and `language_tsx`, and the PHP wheel provides
`language_php`.

The parser cache and the query cache need no change. Both are keyed by the same language id and pick
your language up for free.

### 6. Decide the interop family

This step is a decision, and "no edit" is one of its valid answers.

`_LANG_FAMILY` groups languages that may resolve `calls` and `inherits` references into each other's
definitions. Name resolution is repo-wide and name-based, so without the grouping a Python
`conn.close()` matched a JavaScript `close()`; the comment above the table records the measurement
that forced it, 282 false positives on one real repo, precision 1 in 282.

Add your language to a group **only if it genuinely interoperates** with the languages already in it.
The existing groups are `js` (JavaScript, TypeScript, TSX), `c` (C and C++), and `jvm` (Java, Kotlin,
Scala). A language absent from the table is its own group, which is exactly what you want for a
language that does not share a runtime with anything else, and is why Python, Go, Rust, C#, Ruby and
PHP are all absent.

Getting this wrong in the permissive direction is the expensive error. The false edges it creates are
stamped `INFERRED`, so they read as fact. When in doubt, leave it out.

### 7. Add the parse tests

Two edits in `tests/kb/test_kb_parse.py`, both required:

1. **A `test_parse_<lang>()`** that calls `parse_source(repo, path, src, lang)` directly on a source
   string and asserts what came back. `test_parse_kotlin` is the template worth imitating: it asserts
   every stream the parser produces (nodes by kind, module nodes from imports, callee names, base
   names) and, more importantly, it asserts the **negatives**. `"Order" not in by_kind["function"]`
   and `"Order" not in base_names` are what catch a query that captures one node too many. Write one
   negative per normalisation your language relies on.
2. **The extension assertion.** Add your extensions to the tuple in
   `test_lang_by_ext_covers_target_languages`, and add an explicit mapping assertion for a secondary
   extension if you have one, the way `assert LANG_BY_EXT[".kts"] == "kotlin"` does.

The kind-registry parity test needs no edit. It introspects `_DEF_TYPES` directly, so it starts
covering your language the moment the row exists, and fails immediately if you mapped a node type to a
kind nobody registered.

### 8. Update the published language count

The [word reference](style-guide-reference.md#the-house-style-decision-cache) makes the wording of the
language count a house rule, with one exact phrasing used everywhere. Adding a language moves it in
five hand-written places:

| File | What to change |
| --- | --- |
| `README.md` | the count in the feature list |
| `ROADMAP.md` | the count in the knowledge-graph bullet |
| `docs/indexing-the-code-graph.md` | the count in the [Languages](indexing-the-code-graph.md#languages) section, **and** a new row in the Ecosystem table |
| `docs/style-guide-reference.md` | the mandated phrasing itself |
| `site/build_docs.py` | the "Index the code graph" page subtitle |

The count distinguishes languages from grammars, because TypeScript and TSX share one. If your
language shares a grammar with an existing id, the two numbers move independently.

No test pins the phrase. This step is guarded by review, and it is the one most likely to be
forgotten.

### 9. Regenerate the site, then write the changelog

`site/llms.txt` and `site/llms-full.txt` are generated from the docs, and a test recomputes them and
compares byte for byte. Editing `README.md` or any `docs/*.md` fails that test until you regenerate:

```bash
python site/build_docs.py
```

Most of what the builder writes into `site/` is gitignored and rebuilt on every deploy, so the only
generated file your commit needs to carry is `site/llms-full.txt`. If you added a page rather than
editing one, add its rendered filename to `site/.gitignore` too, which lists the generated pages by
name. Do not run `site/deploy.sh`; it publishes to the `gh-pages` branch and has nothing to do with
this change.

Then add an entry under `[Unreleased]` in `CHANGELOG.md`, naming the grammar package you pinned. The
Kotlin entry is the precedent.

## Optional extras, and what skipping each one costs

None of these is required, and skipping all of them still ships a working language. Each has a stated
consequence, so you can decide rather than guess.

| Extra | Where | If you skip it |
| --- | --- | --- |
| Repo lettermark on graph pages | `_LANG_LABELS` in `kb/visualize/styling.py` | the repo node keeps the generic repo glyph, and your language never appears in the graph-page legend |
| Repo lettermark in the dashboard | `LANG_LABELS` in `kb/dashboard/static/dashboard.js` | the same, in the dashboard. This table is a hand-kept mirror of the Python one and **no test compares them**, so change both or neither |
| HTTP endpoints and clients | `_FAMILY` in `kb/flow/http.py` | no `endpoint` nodes and no `calls_http` edges. Most languages skip this: Java, Kotlin, Scala, Go, Rust, C, C++, Ruby and PHP all do |
| State machines | `_FAMILY` in `kb/flow/state.py` | no `state` nodes and no state diagram for your language |
| Frontend routes | `_WEB_LANGS` in `kb/flow/web.py` | no `route` nodes. Correct to skip unless your language really hosts a JS-style router |
| Docstrings and signatures | `_doc_sig` in `kb/parse.py` | signatures come from a `parameters` field lookup, docstrings from a Python first-statement string or a JSDoc / C# `///` leading comment. A language using another convention gets `doc=None`. It never raises, so this is a quality gap, not a failure |

Event and topic extraction needs **nothing**: it is a language-agnostic scan over the file text, so
topics work for your language the moment its files are indexed.

## If your language needs a new node kind

Most do not. The `_DEF_TYPES` tables across every supported language map into only `class`,
`interface`, `struct`, `enum`, `function`, `method` and `namespace`, and the parser adds `test`, `file`
and `module` of its own. (The five member-symbol kinds, `field`, `macro`, `typedef`, `enum_constant`
and `global_variable`, come from a separate C and C++ pass, not from `_DEF_TYPES`.) Mapping into that
first set is the cheap path and the normal one.

If you genuinely need a new kind, add a row to `KIND_REGISTRY` in `src/contextlake/kb/kinds.py`.
`KindSpec` is a frozen dataclass in which **no field has a default**, deliberately: a new kind cannot
be added without answering every question a consumer asks about it, and a reviewer sees all of those
answers in one hunk. Python refuses to construct a partial row, and a test asserts that no field ever
gains a default.

Four constraints on the values, each pinned by its own test:

- `color` must be lowercase `#rrggbb`. A kind without a colour gets no filter button on the graph
  page, so it cannot be isolated or hidden.
- `group` must be a member of `KIND_GROUP_ORDER`.
- `why_not_embeddable` must be non-empty when `embeddable` is `False`, and empty when it is `True`.
  Write `""`, never `None`: several older rows pass `None` and get away with it only because they are
  embeddable, and copying that shape onto a non-embeddable kind trades an informative test failure for
  an `AttributeError`.
- `embeddable=True` also means hand-editing the pinned `EMBEDDABLE_KINDS` frozenset in the same
  commit, and bumping `EMBED_CONTENT_VERSION`. That set feeds the per-kind embedding budget floors, so
  widening it evicts vectors that already exist and re-embedding is the only repair.

`glyph=None` is a legitimate answer and most newer rows use it. Setting a glyph means four copies must
agree (the registry, the styling projection, the dashboard sprite, and the dashboard's JS glyph map),
because registering a kind in the JS map is what **disables** the generic file-icon fallback for it.
Registering it there without the sprite renders a blank box, which is worse than being absent from
both.

## What not to touch

Three parts of the repo look like they should grow with a new language and should not.

**The golden parse shard**, `tests/kb/golden/parse_shard.json` and its `FIXTURE` dict. It only moves
if you add a file to that dict, and doing so means regenerating the shard, updating an exact
skip-counter string, and making a `PARSER_VERSION` judgement. Leave it alone and none of that arises.

**`PARSER_VERSION`.** The stamp signals "the shards of already-indexed repos changed", which makes
`doctor` recommend a fleet-wide re-index and makes `kb index` re-index every repo instead of treating
it as unchanged. A new language changes no existing repo's output. Bump it only if you also changed
shared definition, containment or resolution logic.

**The retrieval evaluation set**, `examples/fixtures/golden-queries.json` and
`examples/fixtures/eval-repo/`. This set is scored on every pull request against a declared hit-rate
floor, and it deliberately does not cover every language: Kotlin and Scala are both supported and both
absent. Adding fixture symbols without adding matching queries dilutes the corpus and can push the
score under the floor, and one of its tests requires a perfect hit rate for single-word queries.
Representing your language there is a separate, deliberate change that ratchets the floor up in the
same commit. It is not part of adding a language.

## Verify it

Run these in order. The first three are offline and touch nothing outside the repo and a scratch
directory.

### The grammar loads and the query compiles

```bash
python -c "
from contextlake.kb.parse import _language, _query, _parser
_language('<lang>'); _query('<lang>'); _parser('<lang>')
print('grammar + query OK')
"
```

```
grammar + query OK
```

This is the cheapest check and the one that separates the three most common failures from each other:
it proves the `pyproject.toml` entry installed, that your `_language` branch names the right entry
point, and that your query compiles against **that** grammar version.

The compile step is genuinely strict, which is why it is worth a contributor's time. A node type your
grammar does not have fails at construction, naming the row and column:

```
tree_sitter.QueryError: Invalid node type at row 0, column 1: no_such_node_type
```

A grammar upgrade that renames a node type therefore fails loudly here. Note the limit: this catches
node **types**, not capture names. A capture name the parser does not read compiles cleanly and stays
silent.

### The parser produces the nodes, calls and bases you expect

```bash
python -c "
from contextlake.kb.parse import parse_source
src = open('/path/to/sample.<ext>','rb').read()
nodes, edges, calls, inherits = parse_source('demo/<lang>', 'sample.<ext>', src, '<lang>')
for n in nodes: print(n.kind, n.name, n.qualified_name, n.line_start)
print('calls   ', sorted({c[1] for c in calls}))
print('inherits', sorted({i[1] for i in inherits}))
"
```

On a small Kotlin sample holding one class with one method, one import, one call and one base type:

```
file Svc.kt None None
class CatalogService Svc.kt::CatalogService 5
method process Svc.kt::CatalogService.process 6
module com.example.Base None None
calls    ['helper']
inherits ['Base']
```

Read all four streams. `class` and `method` prove your `_DEF_TYPES` mapping and the method promotion;
the `module` node proves `@import`; and the two lists prove `@call` and `@base` captured names with
**no** spurious extras. Package segments and generic type arguments appearing in `inherits` are the
two things to look for, because they are exactly what the normalisation strips.

### The test suite

```bash
python -m pytest tests/kb/test_kb_parse.py tests/kb/test_kind_registry_parity.py \
  tests/kb/test_languages_config.py tests/kb/test_kb_parse_golden.py \
  tests/kb/test_retrieval_quality.py tests/kb/test_dashboard_kind_glyph_parity.py -q
python -m pytest tests/test_llms_full_is_in_sync.py tests/test_no_emdash_in_docs.py \
  tests/test_doc_links_resolve.py -q
ruff check src tests
```

The golden and retrieval-quality tests are in that list not because they should change, but as proof
that they did not: they are the two a language addition can move by accident.

Then run the full gate, which is a copy of CI's own step, before you open anything:

```bash
make test
```

### End to end through the real CLI

**Do not run `kb index` bare.** It resolves configuration against your real `~/.contextlake/` and
would write into your live store. Point it at a throwaway config whose `store_dir` is somewhere
disposable:

```bash
mkdir -p /tmp/lang-check/demo-repo /tmp/lang-check/store
printf '[kb]\nstore_dir = "/tmp/lang-check/store"\n' > /tmp/lang-check/kb.toml
cp sample.<ext> /tmp/lang-check/demo-repo/
```

Index it, and read the count:

```bash
contextlake kb --config /tmp/lang-check/kb.toml index \
    --source /tmp/lang-check/demo-repo --repo demo/<lang>
```

```
Indexed demo/kotlin: 4 nodes, 3 edges (store totals: 4 nodes, 3 edges)
```

A non-zero node count proves the directory walk classified your extension as code and the parse did
not hit the blanket handler. Zero nodes means your extension never reached the parser.

Query a definition by name, and read the kind:

```bash
contextlake kb --config /tmp/lang-check/kb.toml query CatalogService
```

```
  demo/kotlin · Svc.kt:5 · class · CatalogService
  demo/kotlin · Svc.kt:6 · method · process
```

Each `repo · file:line · kind · name` hit proves a node reached the SQLite store with the right kind
and line. The method is a hit too because search matches qualified names, and `process` is qualified
`CatalogService.process`; that second line is expected, not an over-capture by your query.

Filter by kind, which is the strictest of the three:

```bash
contextlake kb --config /tmp/lang-check/kb.toml query --kind method process
```

```
  demo/kotlin · Svc.kt:6 · method · process
```

A hit here proves the kind string your `_DEF_TYPES` row produces is exactly the one the registry and
the store expect. A miss with the previous query still returning the node means you mapped it to a
kind you did not intend.

**A query that returns nothing after a successful index means the file was skipped, not that search is
broken.** Re-run the index with `-v` and look for `skip <file>: parse error:`.

## Pitfalls

Ordered by how likely you are to hit them. Every one is drawn from a comment, docstring or test
message in this repo.

### A partial registration is a silent skip

This is the headline, and it is the reason to run the cheap grammar check before the CLI. Every file's
parse is wrapped in a blanket handler, because one bad file must not abort a repo:

```python
except Exception as e:  # noqa: BLE001 - one bad file must not abort the repo
    log(f"  skip {sf.rel}: parse error: {e}")
```

So all three half-registered states collapse into one log line at normal verbosity, and the index
still reports success:

| What you forgot | What it raises |
| --- | --- |
| the `_language()` branch | `ValueError: unsupported language: <lang>` |
| the `_DEF_TYPES` row | `KeyError` on `_DEF_TYPES[lang]` |
| the `_QUERIES` row | `KeyError` on `_QUERIES[lang]` |

The symptom is a normal-looking summary with no nodes for your files. Land steps 3, 4 and 5 together,
run the grammar check first, and use `-v` to surface it afterwards.

### Extra capture names are dropped without an error

The parser reads `def`, `def_qi`, `import`, `call` and `base`. A query with `@decorator` or
`@annotation` compiles, runs, matches, and contributes nothing. There is no warning and no test that
would notice, so re-read your query against the capture table in step 4 rather than trusting a green
run.

### Never name a language after a node kind

The parser cache and the extraction-kind registry are keyed by string and were once the same
variable, which meant `_parser()` was inserting parser objects into the kind registry. It is harmless
only while no language shares a name with a kind. Run the `KIND_REGISTRY` check from the
[prerequisites](#prerequisites) before you settle on an id.

### The interop family is the expensive one to get wrong

Putting your language in a family it does not really interoperate with produces `INFERRED` `calls` and
`inherits` edges to same-named symbols in another language, and `INFERRED` reads as fact to everything
downstream. Omitting the language self-isolates it, which is always safe. See step 6.

### Aliasing a grammar is three edits, not one

If two ids share one grammar, all three tables need the pairing and each is a separate statement: the
`_DEF_TYPES` alias, the `_QUERIES` alias, and a distinct `_language()` branch naming the grammar's
other entry point. Missing the third is the first row of the silent-skip table above.

### Do not introduce anything order-dependent

Capture extraction is canonicalised on purpose: tree-sitter returns capture lists in an order that
varies run to run and even within one process, and a test pins shard bytes across five consecutive
indexes. Anything you add must not depend on capture order, and must never tiebreak on a node id,
which is pointer-derived.

### Never let an unhandled shape return empty

A qualifier-segment helper was once a membership test with no `else`, so an unrecognised segment
vanished and the resolver attached the symbol to whatever namespace matched next, a fabricated parent
that reads as fact. If your query or any helper you add meets a shape it does not recognise, make that
visible rather than returning nothing.

## Worked example: Kotlin

Kotlin was added late, is grammatically simple, and skips several of the optional steps, which is what
makes it a useful template: it shows which parts are genuinely required.

**The mandatory set, in the order above:**

| Step | File | What was added |
| --- | --- | --- |
| 1 | `pyproject.toml` | `"tree-sitter-kotlin>=1.1",` in the `kb` extra |
| 2 | `kb/parse.py`, `LANG_BY_EXT` | `".kt": "kotlin", ".kts": "kotlin",` |
| 3 | `kb/parse.py`, `_DEF_TYPES` | `class_declaration` and `object_declaration` to `class`, `function_declaration` to `function` |
| 4 | `kb/parse.py`, `_QUERIES` | three `@def` patterns, one `@import`, one `@call`, two `@base` |
| 5 | `kb/parse.py`, `_language()` | `elif lang == "kotlin": import tree_sitter_kotlin as g; fn = g.language` |
| 6 | `kb/parse.py`, `_LANG_FAMILY` | `"kotlin": "jvm",` alongside Java and Scala |
| 7 | `tests/kb/test_kb_parse.py` | `test_parse_kotlin()`, plus `".kt"`, `".kts"` and the `.kts` mapping assertion |
| 8 | five doc sites | the published count moved by one in all five |
| 9 | `site/llms-full.txt`, `CHANGELOG.md` | the regenerated site file, and a release note naming the grammar package |

**The optional set it took:** the lettermark in both `_LANG_LABELS` tables, Python and JavaScript.

**What it skipped, and correctly:** HTTP, state and route extraction, the golden fixture (so no
`PARSER_VERSION` bump), the retrieval evaluation set, and `KIND_REGISTRY`, since Kotlin introduced no
new kind at all.

The Kotlin query is the piece worth reading line by line. Its two `@base` patterns exist for two
different syntactic shapes, and its test asserts both of them plus a negative for each normalisation
the parser applies. One pattern per real shape, plus one negative per normalisation, is the standard
to hold your query to.

## See also

- [Index the code graph](indexing-the-code-graph.md#languages), what the graph looks like once your language
  is in it
- [CONTRIBUTING.md](../CONTRIBUTING.md), the development loop, commit style, and how to submit
- [Word and term reference](style-guide-reference.md), the house rules the docs edits in step 8 follow
- [Architecture and internals](internals.md), where the parser sits in the three layers
