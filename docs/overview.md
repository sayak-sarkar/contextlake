# contextlake

For anyone new to contextlake who wants to know what it is and where to start.

contextlake is a local context layer for your AI tools. It mirrors your repositories to your
machine, indexes them into a knowledge graph, and serves that graph over MCP (the protocol AI
tools use to call external tools). Your agent then answers from real source instead of guessing.

The mirror and the knowledge layer run on your machine. A hosted model or a hosted connector is
opt-in.

<img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/pebble/overview.webp" alt="Pebble surfacing at dawn on a calm lake, holding the glowing context-pebble, to introduce what contextlake does." width="1200" height="800" loading="lazy">

## Start here

1. [Installing and upgrading](installing.md): pick a channel, run one command. It also holds the
   [versioning promise](installing.md#versioning-and-compatibility).
2. [Quickstart](../QUICKSTART.md): [try it on one repo with no setup](../QUICKSTART.md#try-it-on-one-repo-no-setup),
   then follow the whole fleet path.
3. [contextlake, explained](explained.md): what changes on your screen, and why it is built this way.

## The three layers

You adopt the layers one at a time. The mirror is useful on its own.

1. **Mirror.** Clone every repository you can reach into a local copy of its namespace tree.
   It works with GitLab, GitHub, Bitbucket, Gitea, Codeberg and Forgejo.
2. **Knowledge layer** (optional). Turn the mirror into a graph you can query, with semantic
   search, a wiki and connectors.
3. **Serve.** Expose the result over MCP, and as an offline graph viewer and dashboard.

## Where to go next

- **Configure it:** [Configuration](configuration.md) says where settings live and which one wins.
- **Build and use a knowledge base:** [Mirroring repositories](mirroring-repositories.md),
  [The knowledge layer](knowledge-layer.md), [Serving over MCP](serving-over-mcp.md) and
  [Using the dashboard](using-the-dashboard.md).
- **Operate it:** [Keeping it fresh](keeping-it-fresh.md), [Scheduling runs](scheduling.md) and
  [Troubleshooting](troubleshooting.md).
- **Look something up:** [Command reference](cli-reference.md) and [Console output](console-output.md).
- **Contribute:** [Contributing](../CONTRIBUTING.md) and [Writing style](style-guide.md).

## Commands

Run any command as `contextlake <command>`, and add `--help` for its own help. Verbs sit under
the noun they belong to: `mirror` for git repositories and `kb` for the knowledge layer.
`init`, `bootstrap`, `version`, `completion`, `doctor` and `schedule` sit at the top, because they
span both layers or neither. The command reference (linked above) lists every command, flag
and exit code.

## Local by default

contextlake has no telemetry, no analytics, no usage reporting and no crash reporting.

To check that a run stays local, add `--offline` (or set `CONTEXTLAKE_OFFLINE=1`). It refuses
every non-loopback connection this process makes, and keeps loopback open for the MCP server, the
dashboard and a local Ollama. The [Command reference](cli-reference.md#global-flags) says what
`--offline` covers and what it does not.
