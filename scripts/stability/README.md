# Stability harness: tier E output oracles, and tier F on Windows

Tier E checks named expected values in what contextlake prints and serves. It runs on a
synthetic four-repo fixture (Python, TypeScript, JavaScript, Go, SQL, ADRs, manifests) and
covers the CLI and the MCP server's output contract. Tier F runs the same oracles on a
Windows CI cell and compares the verdicts with a Linux cell.

None of this is a test suite. `pytest` does not collect it: the files sit outside `tests/`
and none is named `test_*.py`.

## Files

| File | What it does |
|---|---|
| `tier_e.py` | The oracles, the break-test, and the run. Takes a run function from its caller. |
| `fixture_e.py` | Builds the fixture with `pathlib` (bytes, LF) and commits it with fixed dates. |
| `mcp_client_e.py` | Starts `contextlake kb serve` over stdio in the target venv and records each tool call. |
| `ci_runner.py` | The run function for CI. Refuses to run unless `GITHUB_ACTIONS=true`. Run as a script, it prints the evidence lines. |
| `diff_e.py` | Prints a per-oracle diff of two `results.json` files. |

## How a run works

1. The fixture is built twice (with and without two docstrings) and committed. The author
   and commit dates are fixed, so the commit SHAs depend on the file bytes only.
2. Three stores are indexed: `main` (no embeddings) and two vector stores for the rank rows.
3. Each oracle compares one command's output with a named expected value.
4. Each oracle runs again against a wrong expected value. That break-test must fire. An
   oracle whose break-test does not fire is listed in `blind_oracles`.
5. `results.json` holds every row (verdict, expected, actual, first failure lines) and every
   call. One JSON file per contextlake call sits beside it.

A failing oracle is a result, so the exit code is 0. A crash, or an index that fails, exits
non-zero.

## Running it

On CI: dispatch the `stability` workflow. Its `version` input names the PyPI release (default
`9.8.3`). The `tier-e` job runs on `ubuntu-latest` and `windows-latest` and uploads
`tier-e-Linux` and `tier-e-Windows`. The `tier-e-diff` job prints the diff of the two.

On a developer machine, `ci_runner.py` refuses to run. Use a wrapper that passes a guarded
runner to `tier_e.main(...)`: its `run`, a target factory, a working root and a results root.

Compare two runs:

```
python scripts/stability/diff_e.py linux/results.json windows/results.json
```

## Embeddings and extras

- Install `contextlake[kb-full,kb-pdf]`. `kb` gives the parsers and the MCP server, and
  `kb-local` gives the builtin `model2vec` embedder that the vector rows (O3, C20) use.
- The model is `minishlab/potion-base-8M` at revision
  `bf8b056651a2c21b8d2565580b8569da283cab23`, the one the Linux runs used.
- The CI job downloads it in its own step, which needs network, and writes `refs/main` with
  that revision: a download by commit writes none, and an offline lookup needs it. The tier
  then copies the cache into its fake home and runs with `HF_HUB_OFFLINE=1`.
- O4 (`kb eval`) runs on the store without embeddings.

## Windows points, and the evidence the job prints

| Point | How the harness handles it | Evidence line |
|---|---|---|
| NTFS is case-insensitive | Both cells run the fixture in portable mode, which drops the one pair of names that differ only by case. | `file system ... is case-insensitive`, and `D05 pairs in this fixture` |
| Line endings | Files are written as bytes with `\n`. Each fixture repo sets `core.autocrlf=false` and `core.eol=lf` before the commit. | `core.autocrlf`, `core.eol` and `ls-files --eol` per repo. The commit heads match across the cells only if the bytes match. |
| Home directory | `Path.home()` reads `USERPROFILE` on Windows. The adapter sets `HOME`, `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA` and `LOCALAPPDATA` to the fake home. | `Path.home() in the child` |
| Executables | A venv keeps them in `Scripts\*.exe` on Windows and `bin/` elsewhere. The adapter and the MCP client name the path, with no PATH lookup. | `target exe`, `target python` |
| Path length | The working root is `$RUNNER_TEMP/f`. | `longest path under the root` |
| Stubs | Tier E reads no stub calls. The stub dir is created empty, and no shell stub is written. | `stub dir ... holds []` |
| Pipe encoding | On Windows the child writes UTF-8 to its pipes (`PYTHONIOENCODING`). `PYTHONUTF8` is not set, so contextlake's own file reads keep the Windows default. | `child env keys` |
| TOML paths | Store paths go into a TOML literal string, where a backslash is not an escape. | the index step succeeds |

## What it does not cover

- A cp1252 pipe: the child writes UTF-8 on Windows, so output that a cp1252 pipe cannot
  encode is not tested here.
- The two case-only D05 rows (`O8.file_node.ts-case-only`, `O8.mcp.file_node.ts-case-only`).
  They need a case-sensitive file system.
- macOS.
