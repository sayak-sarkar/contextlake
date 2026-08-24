# MCP transports and limits

## Transports

`contextlake kb serve --transport <stdio|http|sse>` (default `stdio`):

- **`stdio`**, the default. The editor/agent spawns `contextlake kb serve` itself and talks to
  it over stdin/stdout; this is what `steer`-generated `.mcp.json`/`.vscode/mcp.json` entries use.
  No token, no network: the pipe belongs to the process that spawned it.
- **`http`**, Streamable HTTP, the MCP spec's current standard network transport (`--host`/
  `--port`, default `127.0.0.1:8765`). Point clients at `http://127.0.0.1:8765/mcp`, not the bare
  host:port: the endpoint is the `/mcp` path. Any other path, the root included, returns **401**
  rather than 404, because the bearer-auth middleware wraps the whole app and runs before routing.
  Prefer this transport for
  any new remote/network wiring. **Authenticated**, see below.
- **`sse`**, the older HTTP+SSE transport from the 2024-11-05 MCP spec revision. The current spec
  marks it deprecated in favor of Streamable HTTP, but still guides servers to keep offering it
  for clients that haven't moved off it yet; contextlake follows that guidance rather than
  dropping it. Its endpoint is `http://127.0.0.1:8765/sse`. Use `sse` only if your client
  specifically requires it (some clients, e.g. Devin's custom-MCP-server setup, list SSE as a
  distinct, separate option from HTTP), pick `http` first. Authenticated exactly like `http`.

### Stopping it

`Ctrl-C` stops every transport and exits `0`. Ending a server the documented way is not a
failure, so `kb serve` does not use the `130` an interrupted command usually exits with (see
[Reading the console output](console-output.md#what-it-exited-with)).

| How it stops | `stdio` | `http` / `sse` |
| --- | --- | --- |
| `Ctrl-C` (SIGINT) | `0` | `0` |
| `SIGTERM` (`systemctl stop`, `docker stop`, a supervisor) | `0` | `143` |

`stdio` installs its own handler for both signals through asyncio's wakeup fd, which is what makes
an **idle** server stoppable at all. Python only runs a signal handler at a bytecode boundary in
the main thread, and that thread is parked in the selector with no traffic coming, so without the
wakeup fd an interrupt sits unhandled until a request happens to arrive (`_run_stdio` in
`src/contextlake/kb/server.py`). Both signals then unwind through one path, which closes the store
and the vector store on the way out.

`143` on the network transports is `128 + 15`, the conventional "terminated by SIGTERM" code, and
it is what a supervisor reads as a clean stop rather than a crash. uvicorn owns those transports:
it handles the signal itself, drains connections and shuts the session manager down, then restores
the default handler and re-raises the signal, so the process reports the termination it was asked
for *after* the shutdown has already finished.

### Authenticating the network transports

The graph answers with real file paths, symbol names, docstrings and owner identities, so the
socket transports do not serve it to anyone who connects.

**A bearer token, printed once to stderr at startup:**

```
$ contextlake kb serve --transport http
✓ MCP server on http://127.0.0.1:8765/mcp  (Ctrl-C to stop)
  Bearer token: <a fresh 43-character token>
  Clients must send: Authorization: Bearer <token>
  Pin a stable one across restarts with $CONTEXTLAKE_MCP_TOKEN.
```

Every request needs `Authorization: Bearer <token>`; without it the server answers `401`. The
token goes to stderr only, never to stdout, never to the log file, so it does not outlive the
process anywhere you did not put it.

**Pin it for a client config.** A fresh token per launch is fine when you copy it by hand and
useless when a config file has to hold it. Set `CONTEXTLAKE_MCP_TOKEN` and the server uses that
value instead of minting one (an empty or whitespace-only value is treated as unset, and a fresh
token is minted, it never turns authentication off):

```bash
export CONTEXTLAKE_MCP_TOKEN='pick-your-own-long-random-string'
contextlake kb serve --transport http
```

**Origin and Host are validated** on every request, as the MCP spec requires for HTTP transports:
a request whose `Origin` is not the bound host (or a loopback address) gets `403`, and one whose
`Host` does not name this server gets `421`. That is what stops a web page you visit from
reaching your loopback MCP server through DNS rebinding.

**Non-loopback binds must be opted into.** `--host` outside `127.0.0.1` / `localhost` / `::1` is
refused unless you pass `--allow-remote`, and prints a warning when you do:

```bash
contextlake kb serve --transport http --host 0.0.0.0        # refused, exits 1
contextlake kb serve --transport http --host 0.0.0.0 --allow-remote
```

Nothing here is encrypted in transit. For anything beyond your own machine, prefer an SSH tunnel
to a loopback bind, or put TLS in front of it. Note also that a wildcard bind (`0.0.0.0`) only
answers requests whose `Host` is a loopback name, because the Host check has no way to know which
address you meant, bind the address clients will actually name (`--host 192.0.2.10`).

**There is an access log, and it is off by default.** These servers are loopback developer tools
whose console is already a command's output, so they stay quiet, but a server holding the whole
code graph should be able to answer "what did it serve, and to whom". `--access-log` turns on one
line per request (client address, request line, status).

For contextlake's own servers, `kb dashboard --serve`, `kb graph --serve`, `kb graph --site
--serve`, those lines go through the same logger as everything else, so they land in `--log-file`
and follow `--log-format json`, and the client-supplied request line is stripped of control
characters first. `kb serve`'s `http`/`sse` transports are served by uvicorn rather than by
contextlake's handler, so there the flag enables *uvicorn's* access log instead: its own format,
on stderr, alongside its startup banner.

**Devin is different: there's no repo file to wire.** Devin's MCP connections are configured at
the account/org level (`mcp.devin.ai`, with an API key and org header), not read from a file
committed to the repo it's working in, so contextlake cannot self-register as a Devin MCP
server the way it can for the clients above. Add `contextlake kb serve` there yourself, once, in
Devin's own MCP settings. What `contextlake kb steer` *does* give Devin (and any agent that reads
plain workspace context) is `AGENTS.md`: the portable part travels; the MCP wiring itself
doesn't.

## How many tool calls run at once

```bash
contextlake kb serve --tool-concurrency 4
CONTEXTLAKE_MCP_TOOL_CONCURRENCY=4 contextlake kb serve
```

**The default is `2`, and raising it makes the server slower.** That is the opposite of what a
concurrency knob usually does, so it is worth writing down why. The bound applies to every
transport, not just the network ones.

The MCP SDK runs every synchronous tool body through `anyio.to_thread.run_sync` with no limiter,
so it takes anyio's default of 40 worker threads. contextlake's tool bodies are graph traversals
over SQLite, and a traversal is not one query, it is thousands of small round trips through the
store. Forty threads interleaving those on one connection pool spend their time contending rather
than working, and what they are contending for is the store round-trips, not the Python: the same
traversal run over in-memory dictionaries does not degrade the same way. The default therefore has
to sit near the low end rather than merely below anyio's 40.

The bound is applied to the **tool bodies themselves**, not by shrinking that worker pool. The pool
is sized separately, to the bound plus a reserve for transport I/O, because the SDK's stdio
transport borrows worker threads for its own `readline` and `flush`: with the pool set to the bound
outright, `--tool-concurrency 1` left stdin holding the only token and the server answered nothing
at all. `tests/kb/test_serve_concurrency.py` pins both halves, that the bound still bounds
concurrent tool bodies and that stdio still answers at a limit of one.

**The cheap tools come out faster too**, which is the counterintuitive part and the reason not to
think of this as a throughput-for-latency trade. `search_code` and the other short lookups pay
store round-trips as well, just fewer of them, so in an unbounded burst they sit in exactly the
same contention as the traversals do. Bounding the pool takes that away from them rather than
making them queue for a slot.

**Two rather than one**, because a limit of one is only free when every call costs the same. Real
editor traffic mixes one slow call with many fast ones, and at a limit of one a single multi-second
traversal holds the only token while every cheap lookup waits behind it. Two keeps a slot free for
the cheap path while still keeping the server far away from the width where contention dominates.
One is a supported setting, not a trap: set it if your traffic is one caller at a time.

Precedence is the flag, then `$CONTEXTLAKE_MCP_TOOL_CONCURRENCY`, then the default. A value that
is not a positive integer is ignored rather than fatal, whichever of the two it came from
(`resolve_tool_concurrency`): this is a performance knob on a server your editor launches, and
refusing to start over a typo in a shell profile is worse than serving at the default.

## Every cited node says whether the file moved under it

The staleness contextlake tracked until now is per **repo**: has the head commit or the parser
version moved since this graph was built ([Keep it fresh](keeping-it-fresh.md)). That is the right
question for the graph as a whole and it is blind to the one that bites hardest in practice, an
agent editing files *between* index runs, inside the same commit. The graph says
`src/billing/refund.py:88`, twenty lines get inserted above it, and the answer still says 88. A
confidently wrong citation is worse than a miss, because the agent goes and reads it.

Every node a tool returns therefore carries **`citation_status`**, decided against the file on disk
as the answer is built:

| value | what it means |
|---|---|
| `verified` | the file has not been written since the repo was indexed |
| `stale` | it has, and the line number may have moved. The **file is still the right one**, so find the symbol by name |
| `unverifiable` | the citation could not be checked at all: no local checkout, an unreadable file, or a repo that carries no index timestamp |

When the status is not `verified` a **`citation_note`** says which of those it is, in a sentence
meant for the agent reading it. `unverifiable` is not a polite `verified`: it means nothing was
checked, and the two are kept apart for the same reason `kb eval --verify-citations` keeps them
apart ([Semantic search](searching-semantically.md#are-the-citations-real)). The answer is still returned
either way: the guard discloses, it never withholds a result or refuses.

**What it costs.** One `stat()` per *distinct file* in a response, not per node. Only files that
really were written after indexing escalate to a confirming read, which asks the same question
`--verify-citations` asks and shares its implementation. Measured on a real store, a full MCP call
costs about **1.7% more when nothing has changed** and 28.6% in the worst case where every file in
the response was modified, at roughly 1.5 tokens per node. Past 32 confirming reads in one request
the remainder are reported `stale` with `modified_after_index` rather than quietly passed. A
budget nobody is told about would read as a clean bill of health for work that never ran.

**"One request" means one call over the wire, including every leg of an `ask`.** `ask` routes to
several tools internally and they share one probe on purpose, so a file cited by three legs costs
one `stat()` rather than three. The budget is shared for the same reason, which is worth knowing
before reading it as per-verb.

The fields are `null` on surfaces that do not run the guard, which again is not a synonym for fine:
the dashboard reads the graph directly and does not install a probe.

`blast_radius` returns hits rather than nodes and carries the same two fields, so no verb hands
back a `file` and a `line` with nothing said about whether they still hold. `contextlake kb steer`
writes the same three-value explanation into the generated agent skills, so an agent that reads
only its steering files still knows what a `stale` result means.
