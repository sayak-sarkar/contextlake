# Bootstrap and keep fresh

Rather than running each stage by hand, `bootstrap` chains them end to end, so a teammate goes from
nothing to a fully-wired workspace in one command, and it's safe to re-run on a schedule to stay current.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/pipeline-bootstrap.png" alt="The contextlake bootstrap pipeline: sync, then index, then connect, then embed, then enrich, then wiki, then steer." width="760">
</p>

`bootstrap` chains mirror repos → index → connect → embed → enrich → wiki → write editor steering,
skipping anything not enabled:

```bash
contextlake bootstrap --llm builtin
```

`--llm builtin` powers the wiki stage with a zero-setup CPU model, so this single command builds the
**whole** knowledge layer (graph, vectors, and wiki) for every repo. (`--llm ollama` | `openai` | `auto`
for higher-quality prose; the pre-command form `contextlake --llm builtin bootstrap` also works.) Without
any `--llm`, and with `[llm]` disabled in `kb.toml`, the wiki stage no-ops and the rest still runs. Because
everything generated lives under one `store_dir`, setting it to a folder in your workspace keeps the entire
knowledge base in a single, easy-to-access location.

Both config files are read from their default locations (`~/.contextlake.ini` and `~/.contextlake/kb.toml`,
or the nearest ancestor directory's `.contextlake.ini` / `.contextlake.kb.toml` if one exists -- see
[Directory-scoped config](configuration.md#directory-scoped-config)); pass `--config` to point elsewhere.
The valid `[kb]` keys are `store_dir`, `languages`, `skip_generated`, `max_file_bytes`, and `index_workers`
(plus the `[embeddings]`, `[llm]`, `[sources]`, `[rules]` tables); an unrecognized key or table is warned
and ignored, so a typo like `store` (for `store_dir`) is surfaced rather than silently dropping the run
into the wrong place. Likewise, an explicit `--config` path that doesn't exist is a hard error rather than
a silent fall-through to the next file in the precedence chain, which could point at a completely different
store than the one you meant to target. Skip stages with
`--no-sync` / `--no-embed` / `--no-wiki` / `--no-connect` / `--no-enrich`. For an isolated CLI, install with
`pipx install "contextlake[kb]"`, or run ad-hoc with `uvx`.

## Command composition

Every stage is standalone, idempotent, and composable. Use these flows to build exactly what you need:

| Use case | Command(s) |
|---|---|
| Blank to fully enriched workspace | `contextlake init` then `contextlake bootstrap` |
| Add a connector, re-enrich the wiki | `contextlake kb source add jira ...` then `contextlake kb enrich` then `contextlake kb wiki` |
| Single repo, enriched | `contextlake kb index .` then `contextlake kb source add ...` then `contextlake kb enrich` then `contextlake kb wiki` then `contextlake kb serve` |
| Refresh enrichment only | `contextlake kb enrich` then `contextlake kb wiki --force` |
| Manage or inspect sources | `contextlake kb source list` or `contextlake kb source test <name>` or `contextlake doctor` |
| Disable a noisy source | `contextlake kb source disable <name>` then re-run `contextlake kb enrich` |

`contextlake bootstrap` runs the full pipeline (mirror, index, connect, embed, enrich, wiki, steer) end to
end, so `init` plus `bootstrap` takes a blank workspace to a mirrored, indexed, embedded,
connector-enriched, wiki'd, editor-wired workspace in one command (skip enrich with `--no-enrich`).

## Keep it fresh on a schedule

`bootstrap` is incremental and branch-safe, so it's safe to run repeatedly, it re-mirrors, re-indexes only
the repos whose HEAD moved, refreshes the knowledge layer, and rewrites the steering, without touching an
in-progress working tree. Run it from cron:

```cron
*/30 * * * * contextlake bootstrap >> ~/.contextlake/refresh.log 2>&1
```

or as a systemd user timer, see [`examples/contextlake.service`](../examples/contextlake.service) and
[`examples/contextlake.timer`](../examples/contextlake.timer).

## Re-index on commit (git hook)

For continuous freshness without a schedule, install a git `post-commit` hook that re-indexes a repo the
moment you commit to it:

```bash
contextlake kb hook install                     # the repo in the current directory
contextlake kb hook install --workspace ~/src   # every git repo under a mirror
contextlake kb hook status  --workspace ~/src   # which repos are wired
contextlake kb hook uninstall                   # remove it (any pre-existing hook is kept)
```

The hook runs `contextlake kb index <repo>` detached (so the commit returns immediately) and re-uses the
repo's stored id, so it updates the same graph node rather than a duplicate. Mirror-wide syncing (fetch
new clones, prune) still belongs to `bootstrap` on a schedule; the hook keeps *local edits* current
between syncs.

If two contextlake processes ever target one store at once (say a `bootstrap` and a hook-triggered
`index`), the second takes an advisory single-writer lock (`<store_dir>/.contextlake.lock`) and refuses
rather than interleaving SQLite writes, naming the process that holds it. A lock left by a crashed run is
reclaimed automatically; override (rarely correct) with `CONTEXTLAKE_ALLOW_CONCURRENT=1`.

## See also

- [Index the code graph](index-code-graph.md)
- [Generate the wiki](generate-wiki.md)
- [Reading the console output](console-output.md)
