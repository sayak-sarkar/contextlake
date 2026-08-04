# contextlake

```markdown
# contextlake

## Overview
contextlake is a code repository designed as a local context layer, mirroring repositories and indexing them into a queryable knowledge graph over MCP. This allows tools to answer questions based on real source code instead of guessing.

## Setup & Run
The repository contains various scripts and files necessary for setup and operation:
- `Dockerfile`: For containerization.
- `pyproject.toml`: Configuration file for development environments.
- `src/contextlake/__main__.py`: Main entry point for context lake application.
- `README.md`: Introduction to the project and its features.
- Additional scripts like `docker/prefetch_models.py`, `run-contextlake.py` for running or managing the application.

## Architecture
The repository is organized into subsystems, each with their own wiki page:
- **tests**: Contains test fixtures, files related to testing the context layer functionality.
- **src/contextlake**: Main application codebase where core logic and functions reside. 

## Dependencies
Packages depended on by `contextlake` include:
- ContextLake (`contextlake`)
- Argcomplete, pytest, pytest-cov, ruff, hypothesis, pytest-timeout, pytest-xdist, pre-commit, build, twine, mcp, pydantic, tomli, tomlkit, tree-sitter
- Other libraries for Python 3.10+ and other languages.

## Gotchas
- Method `close` in `src/contextlake/kb/store/base.py`, with 283 callers.
- Function `close` in `site/cmdk.js`, also with 283 callers.
- Class `SqliteStore` in `src/contextlake/kb/store/sqlite_store.py`, with 185 callers.

## Architecture
Subsystems are:
- **tests**: Contains test files and fixtures for verifying context layer functionality.
- **src/contextlake**: Main application codebase including core logic and functions, also contains CLI tools (`cli.py`), configuration management (`config.py`), core modules (`core.py`), and command initialization (`init_cmd.py`). 
```

This structure is based solely on the facts provided.

---
*Generated from the knowledge graph of `contextlake` at commit `0f01c748883f8f1d7e497adf6683a9fff4fa2ac2` on 2026-08-04. Sources: `docker/prefetch_models.py`, `pyproject.toml`, `run-contextlake.py`, `site/build_docs.py`, `site/cmdk.js`, `site/tools/gen_diagrams.py`, `site/tools/gen_flows.py`, `site/tools/gen_icons_final.py`, `site/tools/gen_search_index.py`, `site/tools/gen_small_mark.py`. Grounded in 24/4769 file-backed symbols (0.5%). Subsystem pages: `site`, `src`, `tests`.*