# MCP transports and limits

The three transports `contextlake kb serve` speaks, how each one is authenticated, how many
tool calls it will run at once, and the provenance every cited node carries. Read this before
exposing the server on a socket, or when a client cannot reach an endpoint it can see.

| Transport | Endpoint | Authenticated | Use it when |
| --- | --- | --- | --- |
| `stdio` (default) | none, a pipe | not needed | Your editor spawns the server itself. |
| `http` | `http://127.0.0.1:8765/mcp` | bearer token | Any new remote or network wiring. |
| `sse` | `http://127.0.0.1:8765/sse` | bearer token | Only a client that requires it. |

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

`stdio` installs its own handler for both signals, through asyncio's wakeup fd. That is what
makes an **idle** server stoppable at all.

Here is why it is needed. Python only runs a signal handler at a bytecode boundary in the main
thread, and that thread is parked in the selector with no traffic coming. Without the wakeup fd,
an interrupt sits unhandled until a request happens to arrive. See `_run_stdio` in
`src/contextlake/kb/server.py`.

Both signals then unwind through one path, which closes the store and the vector store on the way
out.

`143` on the network transports is `128 + 15`, the conventional "terminated by SIGTERM" code, and
it is what a supervisor reads as a clean stop rather than a crash. uvicorn owns those transports:
it handles the signal itself, drains connections and shuts the session manager down, then restores
the default handler and re-raises the signal, so the process reports the termination it was asked
for *after* the shutdown has already finished.

### Authenticating the network transports

The graph answers with real file paths, symbol names, docstrings and owner identities, so the
socket transports do not serve it to anyone who connects.

#### A bearer token, printed once to stderr at startup

```
$ contextlake kb serve --transport http
  Bearer token: <a fresh 43-character token>
  Clients must send: Authorization: Bearer <token>
  Pin a stable one across restarts with $CONTEXTLAKE_MCP_TOKEN.
  That token is UNSCOPED and shared. Issue one key per client instead: contextlake kb keys create <name>
✓ MCP server on http://127.0.0.1:8765/mcp  (Ctrl-C to stop)
```

The credential comes first and the URL is last. The same step that decides the credential can
refuse the start, so a run that refuses prints the refusal and never prints the URL.

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

Behaviour differs by server.

**contextlake's own servers**: `kb dashboard --serve`, `kb graph --serve` and
`kb graph --site --serve`. Their access lines go through the same logger as everything else, so
they land in `--log-file` and follow `--log-format json`. The client-supplied request line is
stripped of control characters first.

**`kb serve`'s `http` and `sse` transports** are served by uvicorn, not by contextlake's handler.
There the flag enables *uvicorn's* access log instead: its own format, on stderr, next to its
startup banner.

**Devin is different: there's no repo file to wire.** Devin's MCP connections are configured at
the account/org level (`mcp.devin.ai`, with an API key and org header), not read from a file
committed to the repo it's working in, so contextlake cannot self-register as a Devin MCP
server the way it can for the clients above. Add `contextlake kb serve` there yourself, once, in
Devin's own MCP settings. What `contextlake kb steer` *does* give Devin (and any agent that reads
plain workspace context) is `AGENTS.md`: the portable part travels; the MCP wiring itself
doesn't.

## Per-key quotas

A key can carry a request rate and a compute budget, and both are enforced on a networked
server:

```bash
contextlake kb keys create ci --rate 60/min --burst 20 --cost-budget 30s/min
```

- **`--rate`** is how many requests the key may send: `60/min`, `5/sec`, `200/hour`.
- **`--burst`** is how many may arrive at once. It defaults to 20, the minimum is 4, and it
  needs a rate: on its own it is the capacity of a bucket that does not exist.
- **`--cost-budget`** is tool time the key may spend, as a duration per period: `30s/min`.
  Each call is charged how long its body ran, so one `ask` is charged for all eight tools it
  routes to. A request count would price that call as one.
- **`none`** on any of them means no limit on that axis, and beats a server default.

Over the quota the server answers `429` with `Retry-After` in whole seconds and a JSON-RPC
error naming the limit as you typed it:

```
HTTP/1.1 429 Too Many Requests
content-type: application/json
retry-after: 20

{"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"rate limit exceeded for this key: 3/min. retry in 20s"}}
```

The refusal happens at the gate, before the request reaches any tool, so `tools/list` and the
`kb://stats` resource are bounded too. A key with no valid credential gets `401` and is never
counted against any quota.

### Defaults for every key

Set them once in `~/.contextlake/kb.toml` (or a file you name with `--config`):

```toml
[serve]
default_rate = "60/min"
default_burst = "20"
default_cost_budget = "30s/min"
```

**All three are unset out of the box**, so nothing starts limiting a key that worked before.
The values above are a reasonable starting point, not what you get by default.

A key's own value wins; where it names nothing, the default applies. `kb keys show` says which
tier each value came from, so an inherited limit never reads as "unlimited":

```
limits   rate=unset -> 60/min from [serve] default_rate  (enforced) · burst=unset -> 20 built in  (enforced) · cost_budget=unset  (no limit)
```

`[serve]` is read only from `~/.contextlake/kb.toml` or a file you passed to `--config`. A
`.contextlake.kb.toml` found by walking up from the current directory is ignored with one line
saying so: that file sits inside a repository checkout, and a rate limit a checkout can rewrite
is not a limit.

**A shared token is bounded by `[serve] default_rate` and has no per-credential opt-out.** It
has no key record, so there is nowhere to write `none` on it. Unsetting the default unlimits
every key relying on it.

### What the quota does not do

- **It is not persisted.** Bucket state lives in the server process, so a restart refills every
  quota. Restarting the server needs operator access, which is a larger grant than any key holds.
- **It is not shared between processes.** Two `kb serve` processes behind one address give each
  key twice its quota. contextlake serves one process by design.
- **On the `sse` transport the message is lost and the session dies.** That client
  (`mcp/client/sse.py`) raises for status inside its writer task and swallows it, so the stream
  closes and the in-flight call never resolves; the next one reports a closed connection. If you
  see "connection closed" on `sse`, check the quota. `http` (streamable-http) delivers the 429
  and the session survives.

## What the server records

A network start writes one line per tool call to `<store_dir>/mcp-usage.jsonl`, and
`contextlake kb keys usage` reads it back.

```bash
contextlake kb keys usage                 # every key, every tool
contextlake kb keys usage --since 24h     # the last day
contextlake kb keys usage k_4f2a91        # one key
contextlake kb serve --transport http --no-usage   # record nothing
```

```
Usage: 140 calls (140 timed)  /home/you/.contextlake/kb/mcp-usage.jsonl

KEY       CALLS  ERR  THR  DENY    P50    P95
k_4f2a91    120    0    0     0   75ms  142ms
k_9c01de     20   20  500     0  423ms  843ms

TOOL             CALLS
ask                 55
find_definition     52
search_code         33

Refused requests (never reached a tool)
  throttled       500
  unknown          30
  identity_unset   12
  total           545
```

**A row carries six fields and nothing else:** the minute, the key id, the tool name, the
outcome, the tool time in whole milliseconds, and how many events the row stands for. No
query text, no symbol, no repository, no file path, no client address, and nothing at all
about the credential a refused caller presented. There is no field to put those in.

**Read the counts, not the lines.** A row for traffic the server never admitted carries a
count instead of one line each, so 500 refused requests are one line reading `500`. An
unauthenticated flood would otherwise evict every real row inside a minute. Per-key calls
(`ok`, `error`, `denied`) keep one row each, because a percentile needs the individual
values.

**Four things it deliberately does not measure:**

- **`ask` counts once, as `ask`.** It routes to eight sibling tools below the wrapper that
  writes these rows, so each of those eight is under-counted by however much `ask` sent it.
- **`tools/list` and the handshake are not calls.** They cross no tool wrapper. So `CALLS`
  counts tool calls and never HTTP requests. Those same requests can still be throttled,
  which is why `THR` and `CALLS` are not two views of one number.
- **`kb://stats` resource reads are not recorded.** That path crosses no wrapper either, and
  the only name available there is a caller-supplied URI.
- **stdio records nothing.** There is no caller to attribute a call to.

**Retention.** The file grows to 22,000 rows and is then trimmed back to the newest 20,000,
so the rewrite happens once per 2,000 rows rather than once per append. At about 105 bytes a
row that is 2.3 MB at its largest. A key quiet for longer than that window reads `never` in
`kb keys list`'s `LAST USED` column, which is why that column carries a note rather than
standing alone. Rebuilding the store discards the file with it.

**If the file cannot be written**, the server keeps serving and says so once on standard
error, naming the path. Recording never fails a request, and a full disk drops the batch
rather than growing the buffer; without the line an empty file would read as an idle server.

**Turning it off, and tuning it:** `--no-usage` on the command line, or in
`~/.contextlake/kb.toml` (or the file passed to `--config`):

```toml
[serve]
usage = false            # record nothing
usage_max_lines = 20000  # rows kept
usage_flush_seconds = 10 # how often the buffer is written
```

Those keys are read from a config you NAMED only, the same gate `[serve] keys_file` and the
quota defaults go through: a `.contextlake.kb.toml` found by walking up from the current
directory sits inside a repository checkout, and a retention cap that checkout can rewrite
is not a cap. A value the server cannot parse refuses the start and names the string.

## How many tool calls run at once

```bash
contextlake kb serve --tool-concurrency 4
CONTEXTLAKE_MCP_TOOL_CONCURRENCY=4 contextlake kb serve
```

**The default is `2`, and raising it makes the server slower.** That is the opposite of what a
concurrency knob usually does, so it is worth writing down why. The bound applies to every
transport, not only the network ones.

The MCP SDK runs every synchronous tool body through `anyio.to_thread.run_sync` with no limiter,
so it uses anyio's default of 40 worker threads.

That is far too many here, for a specific reason. contextlake's tool bodies are graph traversals
over SQLite, and a traversal is not one query. It is thousands of small round trips through the
store.

Forty threads interleaving those on one connection pool spend their time contending rather than
working. What they contend for is the store round trips, not the Python: the same traversal run
over in-memory dictionaries does not degrade this way.

So the default has to sit near the low end, not merely below anyio's 40.

The bound applies to the **tool bodies themselves**. It does not work by shrinking the worker
pool.

The pool is sized separately: the bound, plus a reserve for transport I/O. That reserve is
needed because the SDK's stdio transport borrows worker threads for its own `readline` and
`flush`.

Without it, the failure was total. With the pool set to the bound outright,
`--tool-concurrency 1` left stdin holding the only token and the server answered nothing at all.

`tests/kb/test_serve_concurrency.py` pins both halves: that the bound still bounds concurrent
tool bodies, and that stdio still answers at a limit of one.

**The cheap tools come out faster too**, which is the counterintuitive part and the reason not to
think of this as a throughput-for-latency trade. `search_code` and the other short lookups pay
store round-trips as well, fewer of them but the same kind, so in an unbounded burst they sit in the
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

Until now, contextlake tracked staleness per **repo**: has the head commit or the parser version
moved since this graph was built? See [Keeping it fresh](keeping-it-fresh.md).

That is the right question for the graph as a whole. It is also blind to the case that bites
hardest in practice: an agent editing files *between* index runs, inside the same commit.

Here is what that looks like. The graph says `src/telemetry/window.py:88`. Twenty lines get
inserted above it. The answer still says 88.

A confidently wrong citation is worse than a miss, because the agent goes and reads it.

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

## See also

- [Serving over MCP](serving-over-mcp.md)
- [Asking the graph](asking-the-graph.md)
- [Console output](console-output.md)
- [Troubleshooting](troubleshooting.md)
