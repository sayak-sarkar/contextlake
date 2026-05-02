# Storage & the no-pollution invariant

contextlake keeps **everything it generates under a single store directory** — by default
`~/.contextlake/kb` (`DEFAULT_STORE_DIR`, `src/contextlake/kb/config.py`), overridable with
`store_dir` in your `kb.toml`. It never scatters files into your home directory, your current
working directory, or — most importantly — your synced repositories.

## INV-1 — generated artifacts never pollute a synced repo

> **No contextlake-generated file is ever written inside a mirrored/synced repo working tree.**

The mirror (the GitLab-fleet clone directory) holds *your repos, untouched*. The knowledge layer
contextlake builds from them lives entirely under the separate store. This is enforced by a
regression test — `tests/kb/test_no_repo_pollution.py` drives every generating command over a
temporary two-repo mirror and asserts each repo's working tree is byte-identical before and after.

## What lives under the store

| Path (under `store_dir`) | Contents |
|---|---|
| `index.sqlite` | the graph + FTS index (nodes, edges, provenance, confidence) |
| `graph/` | per-repo JSON graph shards |
| `history/<repo>/` | bitemporal history shards |
| `graphs/` | rendered visualizations (`graph`/`--site` HTML, PNG/DOT exports) |
| `wiki/` | generated LLM-wiki pages (when `wiki` has run) |
| `embeddings.sqlite` | semantic vectors (when `embed` has run) |

## The one deliberate carve-out: steering files

`steer` (and the steering stage of `bootstrap`) writes editor-config files — `AGENTS.md`,
`CLAUDE.md`, `.windsurfrules`, `.kiro/steering/`, `.mcp.json`, skills — that an IDE/agent must find
**at the workspace root it opens**, so these are written to the target you point `steer --out` at
(bootstrap uses the mirror root, which is not itself a repo). They are still never written inside an
individual synced repo tree under the mirror. When onboarding a *single* project you own, you choose
to steer into it explicitly.
