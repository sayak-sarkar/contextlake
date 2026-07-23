# Word and term reference

The lookup layer of the style guide: contextlake's house decisions for recurring word choices, a set of
before/after rewrites, and an A-to-Z term reference. When a choice comes up more than once, it belongs
here. This page is part of the [documentation style guide](style-guide.md).

## The house-style decision cache

A style guide's real value is removing per-instance deliberation. These are contextlake's recurring
micro-choices, so no contributor, and no agent, re-decides them.

- **The name:** `contextlake`, always one lowercase word, even at the start of a sentence. Reword rather
  than capitalize it. Never "Context Lake", "ContextLake", or "context lake".
- **The category noun:** "context layer". Not "tool", "platform", "framework", "knowledge base", or "data
  lake".
- **One term per concept.** These mean specific things; don't use them as loose synonyms:
  - **the graph** is the extracted node-and-edge structure.
  - **the knowledge layer** is the whole optional subsystem (`contextlake.kb`).
  - **the index** is the built store; **indexing** is the act of building it.
  - **a repo** in running prose (match the CLI), not "repository".
  - command names (`sync`, `index`, `graph`, `wiki`, `serve`) are lowercase and in `code`.
- **The language count:** "14 languages across 13 tree-sitter grammars" (`.tsx` shares the TypeScript
  grammar). Use this exact phrasing everywhere.
- **The lake metaphor** is a closed system (brand guidelines section 1.3): *deep* is the real, complete
  source underneath, and *clear* is the precise answer back. Never oceans, waves, fishing, drowning, or
  "data lake". Depth is calm and legible, never threatening.
- **Example values only:** `example.com` and `.org`, `127.0.0.1`, and the public `pallets` GitHub org we
  dogfood on. Never a real private host, token, or internal path.

## Worked examples

Rules are easier to follow when you can see the rewrite.

### Voice and word choice

| Instead of | Write |
|---|---|
| "contextlake allows you to index your repos." | "Use contextlake to index your repos." |
| "Simply run the command and you're done." | "Run the command." |
| "It's a powerful, seamless, next-gen context platform." | "contextlake indexes your repos into a graph and serves it over MCP." |
| "The graph is built by the indexer." | "The indexer builds the graph." |
| "It is important to note that `sync` is incremental." | "`sync` is incremental." |
| "This tool leverages embeddings to facilitate retrieval." | "It embeds your code so you can search it in plain language." |
| "Kill the running process." | "Stop the running process." |
| "Please click here to read the guide." | "Read the [indexing guide](index-code-graph.md)." |

### A heading

- **Instead of:** "Understanding how the knowledge layer works"
- **Write:** "The knowledge layer" (concept page) or "Building the knowledge layer" (how-to page)

### A code example, the four-part unit

Instead of a bare block, write the full unit so the reader knows what it does and what success looks like:

````markdown
To build the graph for every repo under a folder, run `index` with `--workspace`:

```bash
contextlake index --workspace ~/work
```

The run is incremental, so only repos whose HEAD moved are re-indexed. You should see a per-repo
summary ending in a line like `4 repos, 29 nodes, 28 edges`.
````

### A how-to skeleton

```markdown
# Wiring your editor

Connect your editor to contextlake over MCP so your assistant answers from your indexed repos.

## Prerequisites
- A built graph (`contextlake index`).
- One of: Claude Code, Windsurf, or Kiro.

## Steps
1. Run `contextlake steer` to write the MCP config and editor steering files.
2. Restart your editor so it picks up the new MCP server.

## Verification
Ask your assistant a cross-repo question. You should see it call a contextlake tool and cite the
files the answer came from.

## See also
- [Serve it to your editor](serve.md)
```

## A to Z

The house answer for contextlake's recurring word choices. When a term isn't here, defer to the Google
developer documentation word list, then Merriam-Webster.

- **abort.** Don't use for stopping a process. Use stop, cancel, end, or exit.
- **allowlist, blocklist.** Use these, never whitelist or blacklist. "denylist" is also fine.
- **and/or.** Rewrite. Pick "and", "or", or "A, B, or both".
- **CLI.** Spell out on first use per page (command-line interface), then use CLI.
- **click here.** Never use as link text. Describe the destination.
- **command.** You *run* a command, *pass* it a flag, and *set* a config key.
- **config, configuration.** Use "configuration" in prose and "config" only where it matches a file or
  flag. Be consistent within a page.
- **contextlake.** Always one lowercase word, even at the start of a sentence. Never "Context Lake",
  "ContextLake", or "context lake".
- **context layer.** The category noun for contextlake. Not "tool", "platform", or "data lake".
- **data lake.** Never. It reads as an enterprise warehouse and is off-brand.
- **deep, clear.** The two load-bearing metaphor words. Deep is the real, complete source; clear is the
  precise answer back.
- **e.g.** Write "for example" in running prose. Reserve "e.g." for inside parentheses.
- **easy, easily.** Avoid. If the reader is in the docs, it wasn't easy.
- **email.** One word, no hyphen.
- **enable, disable.** Spell both out; don't write "enable/disable".
- **enter, type.** "Enter" text by any method; use "type" only for the keyboard specifically.
- **file name.** Two words in prose, and use it as an adjective before "file": "the `kb.toml` file".
- **i.e.** Write "that is" in running prose. Reserve "i.e." for inside parentheses.
- **internet.** Lowercase.
- **just.** Avoid as a minimizer ("just run X"). It gaslights a stuck reader.
- **kill.** Don't use for processes. Use stop, cancel, or end.
- **leverage.** Use "use".
- **log in, login.** "Log in" is the verb; "login" is the noun or adjective.
- **MCP.** Spell out on first use per page (Model Context Protocol), then use MCP.
- **please.** Don't use in instructions.
- **repo, repository.** Use "repo" in running prose, matching the CLI. "repository" is fine in a formal
  first mention.
- **sanity check.** Use "confidence check" or "final check".
- **select, check.** You "select" a checkbox or an option; you don't "check" it.
- **set up, setup.** "Set up" is the verb; "setup" is the noun or adjective.
- **simply.** Avoid, like "just".
- **utilize.** Use "use".
- **via.** Use "with" or "through".
- **we, you.** Use "you" for the reader's actions. Reserve "we" for a genuine maintainer recommendation.
- **whitelist, blacklist.** See allowlist, blocklist.

## See also

- [Documentation style guide](style-guide.md)
- [Voice and tone](style-guide-voice.md)
- [Formatting](style-guide-formatting.md)
