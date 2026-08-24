# Document sources and RAG

Not everything worth retrieving lives in code. This page is the reference for
`contextlake kb ingest` and the `[[sources]]` block: which source types are built in, what each
one extracts, what it refuses to guess at, and every key you can set.

| Source type | Ships with | Reads |
| --- | --- | --- |
| `files` | core, no extra install | a folder of files on disk |
| `mcp` | core | resources, and optionally a search tool, on an MCP server |
| anything heavier | a plugin package | whatever the plugin's `iter_documents()` yields |

## Aggregating documents

Not everything lives in code. `contextlake kb ingest` pulls **external documents** into the same knowledge
layer, they become `kind="document"` graph nodes and, when embeddings are on, their bodies are embedded so
semantic search spans code *and* docs together:

```bash
contextlake kb ingest --path ./docs        # zero-config: ingest a folder of files
contextlake kb ingest --path ./docs --for-repo group/app   # …and link it to that repo's code
```

`--for-repo` names the **already-indexed repo the documents are about**. Every symbol a document mentions
by name gets a `documented_by` edge to that document, so "where is this function explained?" is a graph
hop instead of a search. Without it, documents are still stored and embedded, but they link to nothing.
The per-source equivalent is `for_repo = "group/app"` on a `[[sources]]` entry.

Sources follow a tiny seam, so common ones are **built-in and config-only** while anything heavier is a
**loosely-coupled plugin**: bake in the common, plugin the rest:

```toml
# kb.toml, built-in "files" source (no code, no extra install)
[[sources]]
type = "files"
name = "handbook"
path = "~/notes"
include = ["*.md", "*.txt"]
```

### PDFs: the text layer, and nothing pretending to be more

Design docs, RFCs and architecture decisions genuinely arrive as PDFs, so the `files` source reads
them as well. `*.pdf` is one of its default globs, and the text comes from the PDF's **text layer**
via `pypdf`, which rides in its own extra so the core stays a single dependency:

```bash
pip install "contextlake[kb-pdf]"
```

`[kb-pdf]` is deliberately not part of `[kb-full]`; see [the extras
table](installing.md#the-extras-and-which-one-you-want). If you set `include` yourself, list `"*.pdf"`
in it, a custom `include` replaces the defaults rather than adding to them.

What the PDF path does **not** do is the load-bearing half. It runs no OCR and no vision model
over a PDF's pages, and makes no network call. (Images ingested as their own files *are* OCR'd --
see below -- but a PDF's pages are not rasterised to reach that path.) A scanned or image-only PDF
has no text layer, and contextlake says so and stores nothing,
rather than aggregating an empty document that would look like knowledge in search results and in
the wiki:

```
files: skipping scan.pdf -- no extractable text (12 page(s) read, all empty). contextlake reads
  a PDF's text layer only; a scanned or image-only PDF has none and is not OCR'd.
```

Three other outcomes are just as loud, because a skipped PDF and a directory with no PDFs must
never look the same:

- **The extra is not installed.** One line per run, naming the count and the install command.
- **A PDF cannot be parsed at all.** Encrypted files are not decrypted.
- **A file is over `max_bytes`.**

That last one is the source's existing 1 MB cap, the same knob text files use. It does two jobs
here: it gates the file on disk, and it bounds the text pulled out of it.

Reading stops at the first page boundary past the cap. The document is kept and marked
`truncated`, so a 900-page PDF costs the pages that fit rather than the whole file. Raise
`max_bytes` on the source to take more.

### Images: read locally, or not at all

Screenshots, exported diagrams and photographed whiteboards carry text that is otherwise invisible
to search. `*.png`, `*.jpg`, `*.jpeg`, `*.webp` and `*.bmp` are default globs, and the text comes
from a **local** OCR engine in its own extra:

```bash
pip install "contextlake[kb-ocr]"
```

The choice of engine is the whole point of this feature.

The obvious way to read an image is to send it to a vision model. That would have made this the
first ingest path to leave local-first: every image would go over the network, one file at a
time, to a third party.

`[kb-ocr]` ships its models inside the wheel instead. A first run downloads nothing, and no image
leaves the machine. The offline boundary holds for images exactly as it does for code.

It is a large extra -- roughly 390 MB once onnxruntime and opencv land -- which is why it is
separate from `[kb]` and from `[kb-full]`, the same call `[kb-fastembed]` makes.

An OCR'd document is marked. Its node carries `ocr = true` in `attrs`, because OCR misreads and a
reader deciding how far to trust a line should not have to infer that from a file extension.

The quiet outcome is the common one and it is still loud: an image the engine reads no words in --
a logo, an icon, a photograph -- is reported and stored as nothing, rather than becoming an empty
document that looks like knowledge:

```
files: skipping logo.png -- the OCR engine read no text in it. An image with no words in it
  (a logo, an icon, a photograph) is expected to land here.
```

Without the extra, images are skipped with one line per run naming the count and the install
command, the same shape the PDF reader uses.

### Video: two layers, because they promise different things

A recorded design review holds two kinds of text, and contextlake reads them with two
separate extras so you can take one without the other. `*.mp4`, `*.mov`, `*.mkv` and `*.webm`
are default globs.

```bash
pip install "contextlake[kb-video]"        # decode + on-screen text
pip install "contextlake[kb-transcribe]"   # ...and the spoken track
```

**`[kb-video]`** decodes the file and runs sampled frames through the same local OCR engine
images use. It adds `av`, which bundles its own ffmpeg, so there is **no system package to
install first** and nothing is downloaded at runtime: as offline as image ingestion. It reads
the slides, the terminal and the UI.

**`[kb-transcribe]`** adds the spoken track via a local speech model. That model is fetched
once on first use and cached under `~/.contextlake/models`, the same way `[kb-local]`'s
embedder is -- a weaker offline promise than frame OCR's, which is exactly why it is a
separate extra rather than folded into the first. `CONTEXTLAKE_WHISPER_MODEL` picks the size;
the default is the smallest useful one, because a first run downloads it.

The work is bounded rather than the file. `max_bytes` gates every other type because file
size predicts how much text a document contributes; for a video it predicts resolution and
length instead, so a 1 MB cap would reject every real recording. What is capped is the
sampling: one frame every 5 seconds, at most 60 frames, so a two-hour recording costs the
same as a ten-minute one. Repeated on-screen lines are said once -- a slide holds still across
many samples, and keeping every hit would drown the transcript in its own echo.

Three outcomes are stated rather than implied:

- **No transcriber installed.** The video is still ingested from its frames, the document
  carries `transcribed = false`, and one line per run names the extra. "The meeting discussed
  nothing" and "nobody installed the speech model" must never look the same.
- **No audio track at all.** A screen recording with no microphone is ordinary, so it is
  reported as exactly that and not as a failed transcription.
- **Nothing readable either way.** Reported and stored as nothing, never as an empty document.

Page numbers survive the ingest. A page is to a PDF what a line number is to source code, so each
document carries `pages` (how many the file has), `pages_read` and `page_offsets` (the character
offset in the document's text where each page starts) in the `attrs` that land on its graph node.
The document's `uri` stays the plain file path, so it is still a citable path on disk.

**Writing a plugin** is one class with `iter_documents()` and one entry point, no fork, no core
dependency:

```toml
# in your plugin package's pyproject.toml
[project.entry-points."contextlake.sources"]
confluence = "my_pkg.sources:ConfluenceSource"
```

```python
from contextlake.kb.sources import Document          # the whole contract

class ConfluenceSource:
    def __init__(self, space=None, **_): self.space = space
    def iter_documents(self):
        yield Document(id="123", title="Runbook", text="...", uri="https://...")
```

`contextlake kb ingest` then discovers `type = "confluence"` automatically. Five sources ship built-in:
`files`, `web`, `api`, `graphql`, and `mcp`. **`web`** fetches URLs and ingests their readable text
(stdlib-only):

```toml
[[sources]]
type = "web"
name = "changelog"
urls = ["https://example.com/changelog", "https://example.com/roadmap"]
```

An **`api`** source ships built-in too: GET a JSON endpoint and map its records to documents, with any
bearer token read from an env var (never the config file):

```toml
[[sources]]
type = "api"
name = "tickets"
url = "https://api.example.com/v1/articles"
items = "data.articles"        # dotted path to the record list
text_field = "body"            # which key holds the document text
token_env = "EXAMPLE_API_TOKEN"  # bearer token comes from this env var
```

A **`graphql`** source ships built-in too: POST a query (+ optional variables) and map records in
the response to documents, the same way `api` maps a REST response:

```toml
[[sources]]
type = "graphql"
name = "issues"
url = "https://api.example.com/graphql"
query = "{ repository { issues { nodes { id title body } } } }"
items = "repository.issues.nodes"   # dotted path into the response, rooted at `data`
text_field = "body"
token_env = "EXAMPLE_API_TOKEN"     # bearer token comes from this env var
```

An **`mcp`** source ships built-in as well: contextlake connects as an MCP *client* (stdio or
streamable-HTTP) to another MCP server, lists its resources, and ingests each:

```toml
[[sources]]
type = "mcp"
name = "team-kb"
command = "uvx"                 # stdio transport: a server to launch...
args = ["some-mcp-server"]
# ...or an HTTP endpoint instead:
# url = "https://mcp.example.com/sse"
```

So contextlake both *serves* a knowledge graph over MCP and *consumes* other MCP servers' resources into
it: the loop closes on the same seam.

An `mcp` source may also declare a search *tool* rather than only reading its resources, and template
codebase-derived terms into the tool's arguments. This is what powers query-driven enrichment in the
`enrich` stage (above). Declare the tool name and an argument template with substitution placeholders:

```toml
[[sources]]
type = "mcp"
name = "team-search"
command = "uvx"
args = ["some-mcp-server"]
# Optional: call a search tool on the server, templating repo/symbol terms
tool = "search"                 # the tool name on the server
arg_template = { query = "{terms}" }  # {terms} substituted with codebase-derived terms
```

Both transports work with tool calling: `command` and `args` for stdio, or `url` for streamable-HTTP. The
tool is called with the templated arguments during enrichment, returning documents grounded to the
codebase's query context.

**Additional `[[sources]]` keys.** Beyond the per-type keys above, connector and ingest sources also
accept: `auth_dir`, an isolated OAuth-cache directory (set a distinct one per Atlassian org so their
`mcp-remote` caches never collide); `mcp_command`, a local stdio MCP command to launch instead of a remote
endpoint (e.g. `"figma-mcp --stdio"` or `"slack-mcp --stdio"`); `hosts`, the list of hostnames a Figma/Slack
source claims links for (defaults to `["figma.com"]`/`["slack.com"]`); `verify_tool`, the Slack MCP tool
name used for reachability checks (default `conversations_info`); `history_tool`, the Slack MCP tool name
used to read a channel's messages (default `conversations_history`); `group`, a GitLab group prefixed to each
repo's path to form the project id; and `per_page`, the API page size (default `50`).

## See also

- [Connecting and enriching](connecting-and-enriching.md)
- [Searching semantically](searching-semantically.md)
- [Embeddings and models](embedding-reference.md)
- [Configuration](configuration.md)
