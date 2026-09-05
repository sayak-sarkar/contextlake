"""`contextlake kb serve` -- run the MCP server."""

from __future__ import annotations

import os
import sys

from ... import style
from ...logging_setup import log
from ..http_base import LOOPBACK_HOSTS
from ._common import (
    _open_store,
    kb_config,
)


def _announce_token(token: str, *, from_env: bool) -> None:
    """Tell the operator how to authenticate, exactly once, on stderr.

    Not through ``log()``: that reaches the configured rotating file handler
    (see logging_setup), and a credential that outlives the process in a log
    file is a worse leak than the unauthenticated socket this replaced. stdout
    is out for the same reason it is on stdio -- it is a protocol stream on one
    transport and pipeable output on the others, and a secret does not belong in
    either. A pinned ``CONTEXTLAKE_MCP_TOKEN`` is acknowledged but never echoed:
    the operator already has it, and printing it back only creates copies.
    """
    from ..server import TOKEN_ENV

    where = f"read from ${TOKEN_ENV}" if from_env else token
    print(f"  Bearer token: {where}", file=sys.stderr)
    print("  Clients must send: Authorization: Bearer <token>", file=sys.stderr)
    if not from_env:
        print(f"  Pin a stable one across restarts with ${TOKEN_ENV}.", file=sys.stderr)


class KeyFileRefused(Exception):
    """The key file exists and cannot be trusted. Never falls back to minting.

    Absent AT A PATH NOBODY NAMED may mint, and so may a file whose every key was
    REVOKED. Every other state means exit 1. Folding them into one
    ``try/except`` ships a downgrade attack: anyone who can corrupt or ``chmod``
    the key file would turn a scoped, per-key deployment into one unscoped token
    printed on stderr.

    "Absent" is ``ENOENT`` and nothing else. A dangling symlink, a path this
    account cannot stat and a file it cannot read are all PRESENT and refused;
    the full table is in ``kb/keyfile.py``'s module docstring. ENOENT itself is
    two rows, split on WHO named the path: see :func:`_load_keyring`.
    """


def _load_keyring(args):
    """The keyring for this network start, or None for a first start.

    None means ABSENT **at a path nobody named**, and nothing else. It used to
    mean two things -- absent, and a valid file with zero live keys -- and
    collapsing them is how an expiry date arriving discarded every scoped key and
    minted a shared token instead. A present file comes back as a keyring
    whatever its records say, and ``cmd_serve`` asks ``Keyring.key_status()``
    which content state it is in.

    **AN ABSENT FILE AT A NAMED PATH IS A FAULT, NOT A FIRST START.** Absent used
    to be one row whoever chose the path, so a ``--keys-file``,
    ``$CONTEXTLAKE_KEYS_FILE`` or ``[serve] keys_file`` pointing at nothing
    minted an unscoped shared token, printed it and exited 0. The case that
    decided it: a container whose volume mount did not appear starts with the
    named path empty, and its stderr is then indistinguishable from a first start on a
    fresh machine, so nothing tells the operator the deployment is open. Naming
    a path is a deliberate act that says the keys live there. Nobody naming one
    is the only first start, and it is the only absent row that may mint.
    ``keyfile.resolve_keys_file_with_source`` says which tier named it, and the
    refusal prints the tier and the path: an operator debugging this needs to
    see WHICH path was consulted, and the four tiers make that a real question.

    Raises :class:`KeyFileRefused` for every other state -- a symlink at the
    key-file path, a directory or a device, a file this account cannot stat, one
    it cannot read, one that will not parse, one carrying group or other bits,
    one sitting in a directory carrying group or other write bits, one owned by
    another account, or one written by a newer schema. The message names the
    file or the directory and its octal mode. ``keyfile``'s module docstring
    holds the full table.

    **The path is read ONCE, by ``Keyring.load``, and this function does no
    filesystem check of its own.** It used to ask ``path.exists()`` first, and
    that answers False for two states that are not absent: a DANGLING SYMLINK,
    and a file this process cannot stat (``Path.exists()`` swallows
    ``PermissionError``). Both then reached the mint path below, so a key file
    that could not be examined turned a scoped deployment into one unscoped
    shared token printed on stderr -- the downgrade this module exists to stop,
    through a door the absent/present split did not model. ``ring.present`` is
    the answer now, and it is False for ``ENOENT`` and nothing else.

    Loaded in ``cmd_serve`` and not in ``run_server``: ``run_server`` returns
    None and cannot produce an exit code, so a refusal raised inside it could
    not exit 1. It is also called before the banner, which is what makes the
    refusal reach the operator's terminal ahead of a line saying the server is
    up. The banner used to print first and this sentence used to say so.

    Imported inside the function, following the deferred-import habit the
    ``run_server`` import above already uses. Local-first property P1 asserts
    that no keystore module is in ``sys.modules`` after an stdio run, and the
    only caller of this function is behind the ``network`` flag.
    """
    from .. import keyfile

    try:
        # The resolve is INSIDE the guard: a set-but-blank
        # $CONTEXTLAKE_KEYS_FILE is refused there rather than falling back to
        # the default path, and a refusal that escaped this function would
        # reach cli.py's top-level guard and print a traceback instead of the
        # two operator lines below.
        path, source = keyfile.resolve_keys_file_with_source(
            getattr(args, "keys_file", None),
            config_path=getattr(args, "config", None))
        ring = keyfile.Keyring.load(path)
    except keyfile.KeyFileError as exc:
        raise KeyFileRefused(str(exc)) from exc
    if not ring.present:
        # ABSENT is answered by the one lstat `load` already took: no record is
        # parsed and no permission mask is read. There is no file to protect
        # yet, and a first start on a machine that has never run
        # `kb keys create` must not depend on anything the keystore does with a
        # file that is not there.
        if source in keyfile.NAMED_SOURCES:
            raise KeyFileRefused(
                f"{source} names {path} and there is no file there. Refusing to "
                "treat that as a first start: naming a path says the keys live "
                "at it, so nothing there is a broken deployment (a volume mount "
                "that did not appear is the usual one) and not a machine that "
                "has never issued a key. Starting would mint one UNSCOPED "
                "shared token for a deployment that asked for scoped keys, and "
                "the stderr would read like a first start.\n"
                "  Mount or restore the file, create the first key with "
                "contextlake kb keys create <name>, or drop the setting to "
                "start on the default location."
            )
        return None
    if ring.permission_note:
        # The reader for keyfile.PERMISSION_CHECK_SKIPPED. Off POSIX the two
        # masks and the owner check do not run, and a check that passes in
        # silence reads as protection it does not provide.
        print(f"  {ring.permission_note}", file=sys.stderr)
    return ring


def cmd_serve(args) -> int:
    # imported here so `query`/`index` don't load it
    from ..server import HTTP_TRANSPORTS, TOKEN_ENV, resolve_token, run_server

    # CLI exposes "http"; the MCP SDK calls it "streamable-http". "sse" (the
    # legacy HTTP+SSE transport) passes through unchanged -- it's a real,
    # separate transport in the SDK, not an alias for either of the other two.
    cli_transport = getattr(args, "transport", None)
    if cli_transport == "http":
        transport = "streamable-http"
    elif cli_transport == "sse":
        transport = "sse"
    else:
        transport = "stdio"
    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or 8765
    network = transport in HTTP_TRANSPORTS

    # Refused before the store is even opened: a bind this wide is a decision to
    # revisit, not a server to start and then reconsider. Every indexed file
    # path, symbol name, docstring and owner identity is readable through these
    # tools, so a non-loopback bind publishes the fleet's internals to whoever
    # can route to this machine. The bearer token below is the second layer, not
    # a substitute for meaning to do this.
    if network and host not in LOOPBACK_HOSTS and not getattr(args, "allow_remote", False):
        log(style.fail(f"--host {host!r} refused without --allow-remote: it would expose "
                       "the whole knowledge graph (file paths, symbol names, docstrings, "
                       "owner identities) to the network."))
        log("  Serve on 127.0.0.1 and tunnel to it, or pass --allow-remote if you "
            "genuinely mean to bind a network interface.")
        return 1

    store, store_dir = _open_store(args)
    vector_store = None
    interrupted = False
    try:
        # On stdio, stdout carries the MCP JSON-RPC stream — keep our logs off it.
        if transport == "stdio":
            from ...logging_setup import use_stderr

            use_stderr()

        # Expose semantic_search only when embeddings are enabled and a vector store exists.
        cfg = kb_config(args)
        embedder = None
        from ..embeddings import build_embedder

        candidate = build_embedder(cfg.embeddings)
        vec_path = store_dir / "embeddings.sqlite"
        if candidate is not None and vec_path.exists():
            from ..embeddings.store import build_vector_store

            embedder = candidate
            vector_store = build_vector_store(vec_path, backend=cfg.embeddings.vector_backend)
            # Report what the embedder can do *here*, not what the config asked
            # for. build_embedder hands back a candidate without loading
            # anything, so announcing the config's intent as a working capability
            # is how this banner came to read "Semantic search enabled" on a
            # machine where the engine was missing and every semantic/hybrid
            # query then failed at call time.
            from ..embeddings import embedder_runtime_state

            ready, why = embedder_runtime_state(embedder)
            if ready is False:
                log(style.warn(
                    f"semantic_search / hybrid_search are registered, but the embedder "
                    f"cannot load on this machine: {why}"))
                log(style.dim("  Every semantic or hybrid query will fail until that is "
                              "fixed; graph search and every other tool work without it"))
            else:
                log(f"Semantic search enabled ({vector_store.name} store, "
                    f"{vector_store.count()} vectors)")
                if ready is None:
                    log(style.dim(f"  {why}"))
        else:
            # Say so out loud: these two tools silently vanishing from the MCP tool
            # list otherwise reads as a broken server, not an unconfigured tier.
            why = ("no [embeddings] config" if candidate is None
                   else "no vector store yet — run: contextlake kb embed")
            log(style.dim(f"semantic_search / hybrid_search not registered ({why}); "
                          "graph search and every other tool work without them"))

        # An empty store starts, banners, and serves every tool it registered
        # (counted 2026-09-05: 23 always-on, plus semantic_search and
        # hybrid_search when an embedder and a vector store both exist); every
        # one of them then answers confidently with nothing, and `graph_health`
        # reports zero stale and zero dangling -- the exact output of a perfectly
        # healthy fleet. Same reasoning as the semantic-search line above: a tier
        # that is missing rather than broken has to say so out loud, and "the
        # graph is empty" is the more consequential of the two. Deliberately
        # after use_stderr(): on stdio, stdout is the JSON-RPC stream.
        if store.stats().repos == 0:
            log(style.warn("This store holds no indexed repository, so every tool will "
                           "answer and every answer will be empty."))
            log(style.dim("  Index one first: contextlake kb index"))

        token = None
        keyring = None
        if network:
            try:
                keyring = _load_keyring(args)
            except KeyFileRefused as exc:
                # stderr, not log(): on a network start use_stderr() has not
                # run (it is stdio-only, above), so log() writes to stdout.
                print(f"  Key file refused: {exc}", file=sys.stderr)
                print("  Fix it or move it aside. This server does not fall "
                      "back to minting a shared token: that would let anyone "
                      "who can write the file downgrade a per-key deployment "
                      "to one unscoped token.", file=sys.stderr)
                return 1
            # EVERY refusal below runs BEFORE any banner and before any
            # credential is drawn, and that is now true rather than intended:
            # the banner is printed at the bottom of this function, right above
            # run_server. A refusal that printed second put "MCP server on
            # http://..." on the terminal for a server that never started, and
            # one that drew a token first put a copy of a secret there too,
            # bought for nothing.
            from .. import keyfile

            keys_only = bool(getattr(args, "keys_only", False))
            env_token = (os.environ.get(TOKEN_ENV) or "").strip()
            if keys_only and env_token:
                print(f"  --keys-only refused: ${TOKEN_ENV} is set, and it "
                      "would bypass every per-key control. Unset the "
                      "variable, or drop --keys-only.", file=sys.stderr)
                return 1
            status = keyring.key_status() if keyring is not None else None

            # THE CALENDAR MUST NOT DOWNGRADE THIS SERVER. Zero live keys was
            # one state routed to the mint path, on the reasoning that an
            # operator who revoked their last key must not be locked out with
            # everyone else. That reasoning is about a DECISION. It says nothing
            # about the default 90-day expiry arriving while nobody is looking,
            # and that path discarded every scoped key, minted one unscoped
            # shared token, printed it and exited 0. So the states are split and
            # each is decided on what put the server there:
            #
            #   all REVOKED   mint. A person ran `kb keys revoke` on the last
            #                 live key. No timer can reach this state, and the
            #                 person who did it is at the terminal.
            #   all EXPIRED   REFUSE, exit 1. A date passed. Nobody chose it,
            #                 and a scoped server must not become an open one
            #                 on its own.
            #   no records    REFUSE, exit 1. Not because a timer emptied it:
            #                 `kb keys prune` runs when a person types it, and
            #                 nothing schedules it. Because a file holding no
            #                 record admits nobody and reads on stderr like a
            #                 first start, so minting on it turns a deployment
            #                 that asked for scoped keys into an open one. Who
            #                 emptied the file does not change that answer.
            #   unaccountable REFUSE, exit 1. Zero live keys and the rest cannot
            #                 be named as revoked or expired. Unreachable today;
            #                 handled first, below, so it can never fall into
            #                 either of the branches after it.
            #
            # A file with LIVE keys is untouched by all of it. The classifying
            # is `Keyring.key_status`, which carries why expired beats revoked
            # when a file holds both.
            if status == keyfile.STATUS_UNKNOWN:
                # The fifth row, and it refuses on both sides. Falling to the
                # `elif` below would have minted, and falling to the final
                # `else` would have started with a keyring holding no live key
                # and no token at all -- a total lockout at exit 0, which reads
                # as a healthy server. `Keyring.key_status` says why nothing
                # reaches this today.
                print(f"  Refusing to start: {keyring.path} holds records this "
                      "version cannot account for as live, revoked or expired, "
                      "so it cannot say whether any client could authenticate.",
                      file=sys.stderr)
                print("  Nothing is minted to cover that: the answer to a file "
                      "this reader does not understand is not an unscoped "
                      "token. Check the file was written by this contextlake.",
                      file=sys.stderr)
                return 1
            if status in (keyfile.STATUS_NO_KEYS, keyfile.STATUS_ALL_EXPIRED):
                what = ("every key in it has EXPIRED"
                        if status == keyfile.STATUS_ALL_EXPIRED
                        else "it holds no key records at all")
                if not env_token:
                    print(f"  Refusing to start: {keyring.path} is a valid key "
                          f"file, but {what}, so no client can authenticate.",
                          file=sys.stderr)
                    print("  No shared token is minted to cover that. A minted "
                          "token is unscoped, and a scoped deployment must not "
                          "turn into an open one because a date passed.",
                          file=sys.stderr)
                    print("  Issue a key and start again: contextlake kb keys "
                          "create <name>", file=sys.stderr)
                    return 1
                # The one start these two states are allowed, and it MINTS
                # NOTHING. ${TOKEN_ENV} is pinned, so an unscoped credential was
                # already live beside the scoped keys and its holder could read
                # the whole graph yesterday. The expiry granted nobody anything
                # new, and refusing here would take a working deployment down
                # over it. The defect being stopped is drawing a NEW unscoped
                # credential unattended, not starting on the operator's own.
                #
                # The keyring is still passed to the server: an expired key then
                # reads as expired rather than as unknown, and `kb keys create`
                # restores scoped auth on the next reload without a restart.
                print(f"  {keyring.path}: {what}, so no client can "
                      "authenticate with a key.", file=sys.stderr)
                print(f"  Starting anyway because ${TOKEN_ENV} is set. It is "
                      "unscoped, it bypasses every per-key limit and scope, "
                      'and usage is attributed to "shared-token".',
                      file=sys.stderr)
                print("  No shared token was minted. Issue a key to get scoped "
                      "auth back: contextlake kb keys create <name>",
                      file=sys.stderr)
                token = env_token
                _announce_token(token, from_env=True)
            elif keyring is None or status == keyfile.STATUS_ALL_REVOKED:
                # Read BEFORE `keyring` is cleared below, which would make every
                # start here look like a first one.
                first_start = keyring is None
                if keys_only:
                    print("  --keys-only refused: no key file with a live key "
                          "was found, so this server would mint a shared "
                          "token, which is what the flag exists to prevent. "
                          "Create one: contextlake kb keys create",
                          file=sys.stderr)
                    return 1
                if status == keyfile.STATUS_ALL_REVOKED:
                    # Constraint 3: whatever happens, the operator is told which
                    # state they are in. This is the one path that still mints,
                    # so it is the one that most needs saying out loud.
                    print(f"  Every key in {keyring.path} is REVOKED, so no "
                          "client can authenticate with a key.",
                          file=sys.stderr)
                    print("  Revoking is a deliberate act, so this start mints "
                          "a shared token rather than locking you out of your "
                          "own server. The token below is UNSCOPED.",
                          file=sys.stderr)
                    print("  Issue a key and restart to get scoped auth back: "
                          "contextlake kb keys create <name>", file=sys.stderr)
                # The minted token is the credential for this start, and a
                # keyring holding no live key admits nobody, so the server is
                # given none -- as it was before the states were split.
                keyring = None
                token, from_env = resolve_token()
                _announce_token(token, from_env=from_env)
                if first_start:
                    # The one route a new operator walks, and the only one that
                    # never named `kb keys`. Every refusal in this function
                    # names it, so the person who most needs a scoped key --
                    # someone on a first start with no key file yet -- was the
                    # one who never saw the command. One line: the token above
                    # is the answer to "how do I connect", and this is the
                    # answer to "how do I stop sharing it".
                    print("  That token is UNSCOPED and shared. Issue one key "
                          "per client instead: contextlake kb keys create "
                          "<name>", file=sys.stderr)
            else:
                # A key file with live keys SUPPRESSES the minted token. Without
                # this, every per-key control is bypassable by whoever read the
                # startup banner, and the banner prints by default.
                #
                # An operator-set CONTEXTLAKE_MCP_TOKEN stays live, because
                # setting it is a deliberate act. resolve_token() is not called
                # here: it MINTS when the variable is blank or unset, which is
                # the right answer for the no-key-file case above and the wrong
                # one here.
                token = env_token or None
                if token is None:
                    print(f"  {keyring.live_count()} keys loaded from "
                          f"{keyring.path}. No shared token: clients "
                          "authenticate with their own key.", file=sys.stderr)
                else:
                    # Deliberately NOT followed by "No shared token": one is
                    # set, and saying both in one banner is how an operator
                    # reads the bypass line as boilerplate.
                    print(f"  {keyring.live_count()} keys loaded from "
                          f"{keyring.path}.", file=sys.stderr)
                    print(f"  ${TOKEN_ENV} is set and bypasses every per-key "
                          "limit and scope. Usage is attributed to "
                          '"shared-token".', file=sys.stderr)
            if host not in LOOPBACK_HOSTS:
                log(style.warn(
                    f"--allow-remote: bound to {host}, so anyone who can route here and "
                    "holds the token above can read the entire graph. Nothing is "
                    "encrypted in transit — put TLS in front of it, or tunnel instead."))
                # Same reasoning as http_base.host_pinning_hint (say it once at
                # startup, not per-request), but deliberately not that function:
                # its wording describes the *stdlib* servers' allow-set
                # ({host:port, localhost:port}), which is narrower than the MCP
                # transport's ({127.0.0.1, localhost, [::1], <bound host>} on any
                # port). Reusing the text would misreport what is accepted here.
                if host in ("0.0.0.0", "::", ""):  # noqa: S104 - this is the wildcard-bind check
                    log(style.warn(
                        f"A wildcard bind ({host}) does not accept this machine's own "
                        "address in a Host header, so a remote client naming it gets "
                        "421. Bind that address instead "
                        "(--host <the-address-clients-will-use>)."))
        # THE LAST TWO LINES BEFORE THE SOCKET, and they sit here for that
        # reason. They used to print ABOVE the credential block, so a start that
        # was about to refuse printed "MCP server on http://..." and THEN the
        # refusal, and an operator reading the terminal top to bottom saw a
        # server that never existed. Nothing between here and run_server can
        # return, so a banner printed here is a banner for a start that goes
        # ahead. Measured before the move: `kb serve --transport http
        # --keys-only` on a machine with no key file printed the green banner
        # and then refused, exit 1.
        log(f"Serving knowledge graph over MCP ({transport})")
        # stdio has no bind address to report; the network transports do, and a
        # blocking server that never says where it listens reads as broken, not
        # busy. Neither network transport is mounted at the bare host:port --
        # each has its own path, and printing the bare root sends clients to a
        # 404. The paths are the SDK's own defaults, which contextlake does not
        # override: streamable_http_path="/mcp" and sse_path="/sse" (see
        # mcp.server.mcpserver.MCPServer.streamable_http_app / .sse_app).
        path = {"streamable-http": "/mcp", "sse": "/sse"}.get(transport)
        if path:
            log(style.ok(f"MCP server on http://{host}:{port}{path}  (Ctrl-C to stop)"))
        run_server(store, transport=transport, host=host, port=port,
                   embedder=embedder, vector_store=vector_store, token=token,
                   tool_concurrency=getattr(args, "tool_concurrency", None),
                   keyring=keyring)
        return 0
    except KeyboardInterrupt:
        # Ctrl-C is the documented way to stop this server (see the "Ctrl-C to
        # stop" log line above), same as graph --serve/--site and dashboard
        # --serve, each of which already reports its own stop message instead
        # of falling through to cli.py's generic "Operation cancelled" catch.
        log("Stopping MCP server")
        interrupted = True
        return 0
    finally:
        if vector_store is not None:
            vector_store.close()
        store.close()
        if interrupted:
            # The mcp SDK's stdio transport leaves a background thread blocked on
            # a stdin read; joining it during normal interpreter shutdown takes a
            # moment, and an impatient second/third Ctrl-C in that window lands
            # with no try/except left around it (we've already returned), surfacing
            # as a harmless but noisy "Exception ignored while joining a thread in
            # _thread._shutdown()" traceback fragment. Our own cleanup just ran
            # above; nothing meaningful is left to do, so skip Python's remaining
            # shutdown sequence (thread joins, atexit) rather than leave that
            # window open. Reproduced directly: 3 rapid SIGINTs before this fix
            # printed the traceback every time; after it, they never do.
            #
            # `os` is imported at module scope now (the key-file banner reads
            # the environment). A second import HERE would make `os` a local of
            # cmd_serve and break that read at the top of the function.
            os._exit(0)
