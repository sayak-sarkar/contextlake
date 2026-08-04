# contextlake

# Overview
This repository is a code repository for other engineers, providing a local context layer for AI tools. It mirrors your repositories, indexes them into a knowledge graph, and serves it over [MCP](https://modelcontextprotocol.io). Everything runs locally and offline, no code leaves your machine, and it carries no credentials of its own.

# Setup & Run
## Setup
To use this repository, you need to have Python 3.10+ installed. You can install it using pip:

```bash
pip install python
```

## Run
To run the code, you need to have Python 3.10+ installed. You can install it using pip:

```bash
pip install python
```

## Dependencies
The code depends on the following packages:

- contextlake: a local context layer for your AI tools
- argcomplete: a command-line interface for your tools
- pytest: a testing framework for your tools
- pytest-cov: a coverage tool for your tools
- ruff: a code formatter for your tools
- hypothesis: a hypothesis testing framework for your tools
- pytest-timeout: a timeout testing framework for your tools
- pytest-xdist: a parallel testing framework for your tools
- pre-commit: a pre-commit hook for your tools
- build: a build tool for your tools
- twine: a tool for your tools to upload their packages
- mcp: a model context protocol server
- pydantic: a model context protocol server
- tomli: a tool for your tools to read TOML files
- tomlkit: a tool for your tools to read TOML files
- tree-sitter: a tool for your tools to parse tree-sitter files
- tree-sitter-python: a tool for your tools to parse tree-sitter Python files
- tree-sitter-javascript: a tool for your tools to parse tree-sitter JavaScript files
- tree-sitter-c-sharp: a tool for your tools to parse tree-sitter C-sharp files

## Gotchas
* State only that each symbol above has that many callers in the graph and is therefore worth extra care/tests when changed — do not characterize WHY it has that many callers, and do not call it "foundational", "core", "critical infrastructure", or similar: the caller count is the only fact given, not an explanation of the symbol's role or importance.

## Architecture
The code is divided into three layers:

- Mirror: mirrors your repositories to your machine, indexes them into a queryable knowledge graph, and serves that graph to your editor over [MCP](https://modelcontextprotocol.io).
- Contextlake: a local context layer for your AI tools.
- Editor: a tool for your AI tools to edit your knowledge graph.

The mirror layer mirrors your repositories to your machine, indexes them into a knowledge graph, and serves that graph to your editor over [MCP](https://modelcontextprotocol.io). Everything runs locally and offline, no code leaves your machine, and it carries no credentials of its own.

The contextlake layer is a local context layer for your AI tools, providing a local context layer for your AI tools. It mirrors your repositories to your machine, indexes them into a knowledge graph, and serves that graph to your editor over [MCP](https://modelcontextprotocol.io). Everything runs locally and offline, no code leaves your machine, and it carries no credentials of its own.

The editor layer is a tool for your AI tools to edit your knowledge graph. It mirrors your repositories to your machine, indexes them into a knowledge graph, and serves that graph to your editor over [MCP](https://modelcontextprotocol.io). Everything runs locally and offline, no code leaves your machine, and it carries no credentials of its own.

The code is broken into subsystems, each with its own dedicated wiki page: tests, src, site. In the Architecture section, name and briefly describe each subsystem rather than attempting to summarize their internals here -- their own pages cover that in more depth.

---
*Generated from the knowledge graph of `contextlake` at commit `0f01c748883f8f1d7e497adf6683a9fff4fa2ac2` on 2026-08-04. Sources: `docker/prefetch_models.py`, `pyproject.toml`, `run-contextlake.py`, `site/build_docs.py`, `site/cmdk.js`, `site/tools/gen_diagrams.py`, `site/tools/gen_flows.py`, `site/tools/gen_icons_final.py`, `site/tools/gen_search_index.py`, `site/tools/gen_small_mark.py`. Grounded in 24/4769 file-backed symbols (0.5%). Subsystem pages: `site`, `src`, `tests`.*