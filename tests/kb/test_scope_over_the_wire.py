"""Every registered tool, driven over a real socket, by three kinds of caller.

WHY THIS FILE EXISTS. The repo axis is enforced by a proxy over the store
(`kb/scoped_store.py`). Three other roads reach data without crossing it: raw SQL
through `.conn` (the three repo-graph flow tools), files on disk named by the
caller's own string (the wiki and generated-doc tools), and a cap applied before the
proxy's filter (the relevance floor). Every one of those passed a 5,000-test gate,
because no test drove a repo-scoped key through the HTTP app, and the tests that did
drive the HTTP app minted keys with no repo scope at all. The proxy's own tests use a
fake store and hand-written lambdas, so they could only ever see the proxy.

So this file asserts on what a caller RECEIVES over the wire, from every tool, and it
reads the tool list FROM THE REGISTRATION. A hand-written list passes while the next
tool that bypasses the proxy ships; enumerating the built server makes that tool show
up here the day it is registered, and fail if it leaks.

THE THREE PROPERTIES, each of which caught a shipped defect:

1. AGREEMENT. An unscoped key, the shared token on the same keyring server, and the
   shared token on a token-only server (which has no proxy at all, so it is the
   reference answer) return the SAME result from every tool. The first two alone
   cannot catch an over-denying proxy: both reach it with an empty scope and are
   over-denied identically. The bare-store server is what breaks that tie.
2. NO DENIED NAME. A key scoped to `alpha/*` never receives a denied repository's
   name or content from any tool, under arguments aimed at its own repo, at a denied
   repo, at a `__` alias of a denied repo, at a namespace whose cluster page narrates
   a denied repo, and at an allowed id whose file belongs to a denied repo.
3. POSITIVE CONTROL. The same key still receives real content about its own repo
   from every tool that can produce any. A scope that answered nothing to anyone
   passes property 2 for free.

The socket harness is `test_mcp_identity_propagates.bound_server`, reused rather than
re-invented, and the key file is a real one written by `keys.create`.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from datetime import date
from pathlib import Path

import pytest

from contextlake.kb import keyfile
from contextlake.kb import keys as keys_mod
from contextlake.kb import server as server_mod
from contextlake.kb.ids import make_id
from contextlake.kb.model import (
    EXTERNAL_REPO,
    PACKAGES_REPO,
    SHARED_REPO,
    SYSTEM_REPO,
    Confidence,
    Edge,
    Node,
    Provenance,
    Repo,
)
from contextlake.kb.server import build_http_app, build_server
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

SHARED = "shared-token-for-the-scope-wire-test"  # noqa: S105 - a test value

# --------------------------------------------------------------------------
# The fixture fleet. Every name is invented.
# --------------------------------------------------------------------------
A1 = "alpha/orders"         # granted
A2 = "alpha/shipping"       # granted: a second readable repo, so a flow filter
#                             that drops EVERY edge cannot pass the positive control
B = "bravo/payments"        # denied: outside the namespace
C = "alpha/hidden/vault"    # denied: three segments, and `alpha/*` is one segment
D = "alpha__ledger"         # denied: one segment, and its page's file name is the
#                             same as the one the ALLOWED id `alpha/ledger` resolves to
A3 = "alpha/ledger"         # granted, and indexed: the other half of that collision.
#                             Both ids encode to `alpha__ledger.md`, so the page on
#                             disk may be D's, and a gate that asks only "is the
#                             argument readable" serves it
SCOPE = "alpha/*"

# A denied repo's NAME. Present in a response, it is a leak unless the caller put it
# in its own arguments (an echoed argument tells the caller nothing it did not know).
DENIED_NAMES = ("bravo", "vault", "hidden/vault", "alpha__ledger")
# A denied repo's CONTENT. Never in any argument, so present in a response it is a
# leak under every variant.
DENIED_CONTENT = ("bravomark", "vaultmark", "ledgermark")

A_REFUND = "alpha_orders_refund"
A_SETTLE = "alpha_orders_settle"
A2_SHIP = "alpha_shipping_ship"
B_SETTLE = "bravo_payments_settle"
B_REFUND = "bravo_payments_refund"
C_SECRET = "alpha_hidden_vault_secret"
D_BOOK = "alpha_ledger_book"
INGEST_DOC = "ingest_notes_runbook"
JIRA_A = make_id("atlassian", "issue", "ORD-1")
JIRA_B = make_id("atlassian", "issue", "PAY-9")
SYSTEM_NODE = "system_api_example_test"


def _prov(file="x"):
    return Provenance(source_file=file, source_line=1, verified_at=date(2026, 10, 1))


def _edge(src, dst, relation, *, context=None):
    return Edge(src=src, dst=dst, relation=relation, confidence=Confidence.EXTRACTED,
                provenance=_prov(), context=context)


def _fn(node_id, repo, name, file):
    return Node(id=node_id, repo=repo, kind="function", name=name,
                qualified_name=name, file=file, line_start=1, line_end=3, lang="python")


def _repo_node(repo):
    return Node(id=make_id("repo", repo), repo=repo, kind="repo", name=repo)


def _populate(root: Path) -> Path:
    """Build the store, the clones and the generated pages. Returns the store path."""
    kb = root / "kb"
    store = SqliteStore(kb / "kb.sqlite")
    try:
        readmes = {A1: "ALPHAMARK-README for orders", A2: "shipping readme",
                   A3: "journal readme",
                   B: "BRAVOMARK-README", C: "VAULTMARK-README", D: "LEDGERMARK-README"}
        for i, (rid, text) in enumerate(readmes.items()):
            # Numbered, not slugged: A3 and D slug to the same name, by design.
            clone = root / "clones" / f"r{i}"
            clone.mkdir(parents=True)
            (clone / "README.md").write_text(text, encoding="utf-8")
            store.upsert_repo(Repo(id=rid, path=str(clone), head_commit="c1"))

        per_repo = {
            A1: [_repo_node(A1),
                 _fn(A_REFUND, A1, "RefundAlpha", "svc.py"),
                 # Its name only STARTS with "settle"; B holds the exact-name match.
                 # That ordering put B's node first at `limit=1` in the relevance
                 # floor, which then told this key the term was not indexed.
                 _fn(A_SETTLE, A1, "SettleOrders", "svc.py")],
            A2: [_repo_node(A2), _fn(A2_SHIP, A2, "ShipParcel", "ship.py")],
            B: [_repo_node(B),
                _fn(B_SETTLE, B, "Settle", "bravomark/pay.py"),
                _fn(B_REFUND, B, "RefundBravomark", "bravomark/pay.py")],
            C: [_repo_node(C), _fn(C_SECRET, C, "VaultmarkSecret", "vaultmark.py")],
            # No repo node for D: `make_id("repo", ...)` normalises `/` and `_`
            # alike, so D's and A3's repo nodes would be one id.
            D: [_fn(D_BOOK, D, "LedgermarkBook", "ledgermark.py")],
            A3: [_repo_node(A3), _fn("alpha_ledger_journal", A3, "JournalEntry",
                                     "journal.py")],
        }
        for rid, nodes in per_repo.items():
            store.upsert_nodes(rid, nodes)
            # The per-repo shard `get_repo_brief` and `graph_health` read from disk.
            write_shard(kb, GraphShard(repo=rid, head_commit="c1", nodes=nodes))
        # The three sentinel and no-repo partitions an UNSCOPED caller must still see.
        store.upsert_nodes("@ingest:notes", [Node(
            id=INGEST_DOC, repo="@ingest:notes", kind="document", name="RunbookSettle",
            file="notes/runbook.md")])
        store.upsert_nodes(EXTERNAL_REPO, [
            Node(id=JIRA_A, repo=EXTERNAL_REPO, kind="issue", name="ORD-1",
                 attrs={"title": "orders ticket", "url": "https://tracker.invalid/ORD-1"}),
            Node(id=JIRA_B, repo=EXTERNAL_REPO, kind="issue", name="PAY-9",
                 attrs={"title": "payments ticket",
                        "url": "https://tracker.invalid/PAY-9"}),
        ])
        store.upsert_nodes(SYSTEM_REPO, [Node(id=SYSTEM_NODE, repo=SYSTEM_REPO,
                                              kind="system", name="api.example.test")])
        pkgs = {"orders-client": "pkg_orders_client", "payments-sdk": "pkg_payments_sdk",
                "keys-kit": "pkg_keys_kit"}
        store.upsert_nodes(PACKAGES_REPO, [
            Node(id=nid, repo=PACKAGES_REPO, kind="package", name=name)
            for name, nid in pkgs.items()])
        store.upsert_nodes(SHARED_REPO, [
            Node(id=nid, repo=SHARED_REPO, kind=kind, name=nid)
            for nid, kind in (("ep_ship", "endpoint"), ("ep_pay", "endpoint"),
                              ("ep_keys", "endpoint"), ("topic_order", "topic"),
                              ("topic_audit", "topic"))])

        r = make_id
        store.upsert_edges(A1, [
            _edge(A_REFUND, A_SETTLE, "calls"),
            _edge(A_REFUND, B_REFUND, "calls"),          # into a denied repo
            _edge(r("repo", A1), "pkg_orders_client", "publishes"),
            _edge(r("repo", A1), "pkg_payments_sdk", "depends_on"),   # A1 -> B
            _edge(r("repo", A1), "ep_ship", "calls_http"),            # A1 -> A2
            _edge(r("repo", A1), "ep_pay", "calls_http"),             # A1 -> B
            _edge(r("repo", A1), "topic_order", "publishes_event"),   # A1 -> A2, B
            _edge(r("repo", A1), "topic_audit", "consumes_event"),    # C -> A1
            _edge(r("repo", A1), JIRA_A, "tracked_by"),
        ])
        store.upsert_edges(A2, [
            _edge(A2_SHIP, A_REFUND, "calls"),
            _edge(r("repo", A2), "pkg_orders_client", "depends_on"),  # A2 -> A1
            _edge(r("repo", A2), "ep_ship", "exposes"),
            _edge(r("repo", A2), "ep_keys", "calls_http"),           # A2 -> C
            _edge(r("repo", A2), "topic_order", "consumes_event"),
        ])
        store.upsert_edges(B, [
            _edge(B_SETTLE, A_SETTLE, "calls"),          # a denied caller of A
            _edge(r("repo", B), "pkg_payments_sdk", "publishes"),
            _edge(r("repo", B), "pkg_keys_kit", "depends_on"),       # B -> C
            _edge(r("repo", B), "ep_pay", "exposes"),
            _edge(r("repo", B), "topic_order", "consumes_event"),
            _edge(r("repo", B), JIRA_B, "tracked_by"),
        ])
        store.upsert_edges(C, [
            _edge(r("repo", C), "pkg_keys_kit", "publishes"),
            _edge(r("repo", C), "ep_keys", "exposes"),
            _edge(r("repo", C), "topic_audit", "publishes_event"),
        ])
    finally:
        store.close()

    pages = {A1: "ALPHAMARK", B: "BRAVOMARK", C: "VAULTMARK", D: "LEDGERMARK"}
    for rid, mark in pages.items():
        slug = rid.replace("/", "__")
        wiki = kb / "wiki" / f"{slug}.md"
        wiki.parent.mkdir(parents=True, exist_ok=True)
        wiki.write_text(f"# {rid}\n\nGenerated at commit `c1`.\n\n{mark}-WIKI\n",
                        encoding="utf-8")
        for kind in ("api", "design"):
            doc = kb / "docs" / kind / f"{slug}.md"
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text(f"# {rid}\n\n{mark}-{kind.upper()}\n", encoding="utf-8")
    # A cluster page for a namespace `alpha/*` admits, narrating a member it denies.
    cluster = kb / "wiki" / "_clusters" / "alpha__hidden.md"
    cluster.parent.mkdir(parents=True, exist_ok=True)
    cluster.write_text(f"# alpha/hidden\n\nMembers: {C}. VAULTMARK-CLUSTER\n",
                       encoding="utf-8")
    return kb / "kb.sqlite"


class _Embedder:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class _Vectors:
    """Returns every node in the REAL store, a denied one first.

    Holds the store path, not the proxy, so it answers the way the unwrapped vector
    store does in production: with ids the caller may not read. Non-empty on purpose:
    an empty one makes `_unpopulated_note` answer before any search runs, and every
    semantic answer would then be empty for every caller.
    """

    def __init__(self, store_path):
        self._path = store_path

    def _ids(self):
        st = SqliteStore(self._path)
        try:
            ids = [row[0] for row in st.conn.execute(
                "SELECT node_id FROM nodes ORDER BY node_id")]
        finally:
            st.close()
        ids.sort(key=lambda i: i != B_SETTLE)
        return ids

    def search(self, vec, k=10, repo=None):
        return [(nid, 1.0 - i / 100) for i, nid in enumerate(self._ids()[:k])]

    def has_any(self, repos=None):
        return True

    def count(self, repo=None):
        return len(self._ids())


# --------------------------------------------------------------------------
# Arguments, built per tool from its registered schema
# --------------------------------------------------------------------------
_ALPHA = {"repo": A1, "node_id": A_REFUND, "name": "RefundAlpha", "query": "settle",
          "package": "orders-client", "question": "what calls RefundAlpha",
          "src_id": A_REFUND, "dst_id": A2_SHIP}
_BRAVO = {"repo": B, "node_id": B_SETTLE, "name": "Settle", "query": "settle",
          "package": "payments-sdk", "question": "what calls Settle",
          "src_id": B_SETTLE, "dst_id": A_REFUND}
# Variants that differ only in `repo`, each aimed at one road around the scope.
_REPO_ONLY = {
    "alias": "alpha/hidden__vault",   # `*` admits it; its file is C's page
    "cluster": "alpha/hidden",        # `*` admits it; its cluster page narrates C
    "collide": "alpha/ledger",        # `*` admits it; its file is D's page
}
# Variants that differ only in `node_id`: the three partitions that belong to no
# repository. An unscoped caller must reach each one exactly as the bare store does;
# the predicate used to deny all three before it read "no patterns means allow".
_NODE_ONLY = {"ingest": INGEST_DOC, "system": SYSTEM_NODE, "external": JIRA_A}
# Passed even though optional: these tools answer "needs a symbol" with neither.
_ALWAYS = ("node_id",)


def _calls_for(tool_name, schema):
    """Every (variant, arguments) pair for one tool.

    A required parameter this table has no value for FAILS the run rather than being
    skipped. The next tool registered with a new parameter name then has to be given
    a value here, which is the point: a tool this test cannot drive is a tool it does
    not cover.
    """
    props = set(schema.get("properties", {}))
    required = list(schema.get("required", []))
    missing = [p for p in required if p not in _ALPHA]
    if missing:
        raise AssertionError(
            f"{tool_name} requires {missing}, which this test has no value for. Add "
            f"one to _ALPHA and _BRAVO so the tool is driven, not skipped.")
    wanted = [p for p in props if p in required or p in _ALWAYS]

    def build(values, *, with_repo):
        args = {p: values[p] for p in wanted}
        if with_repo and "repo" in props:
            args["repo"] = values["repo"]
        return args

    out = [("alpha", build(_ALPHA, with_repo=False)),
           ("bravo", build(_BRAVO, with_repo=True))]
    if "repo" in props:
        for variant, repo in _REPO_ONLY.items():
            out.append((variant, build({**_ALPHA, "repo": repo}, with_repo=True)))
    if "node_id" in props:
        for variant, nid in _NODE_ONLY.items():
            out.append((variant, build({**_ALPHA, "node_id": nid}, with_repo=False)))
    seen, unique = set(), []
    for variant, args in out:
        key = json.dumps(args, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append((variant, args))
    return unique


def _registry(store_path):
    """The tools and resources a LOCAL build registers: the reference catalogue.

    Local, deliberately: a networked build installs the filtered catalogue, and a
    completeness check against it would pass on a subset of what is registered.
    """
    st = SqliteStore(store_path)
    try:
        srv = build_server(st, embedder=_Embedder(), vector_store=_Vectors(store_path))
        tools = {t.name: t.parameters for t in srv._tool_manager.list_tools()}
        resources = sorted(str(r.uri) for r in asyncio.run(srv.list_resources()))
    finally:
        st.close()
    return tools, resources


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------
def _session(hostport, credential, requests):
    """One HTTP connection: `initialize`, then each request. Returns {label: body}."""
    host, port = hostport.split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=60)
    headers = {"Host": hostport, "Authorization": f"Bearer {credential}",
               "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    out = {}
    try:
        conn.request("POST", "/mcp", headers=headers, body=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "scope-wire", "version": "1"}}}))
        response = conn.getresponse()
        assert response.status == 200, (response.status, response.read())
        response.read()
        for i, (label, method, params) in enumerate(requests, start=2):
            conn.request("POST", "/mcp", headers=headers, body=json.dumps(
                {"jsonrpc": "2.0", "id": i, "method": method, "params": params}))
            response = conn.getresponse()
            raw = response.read()
            # A 429 or a 500 carries no denied name, so it would read as "no leak".
            # Every answer has to be a real JSON-RPC result before it counts.
            assert response.status == 200, (label, response.status, raw[:300])
            doc = json.loads(raw)
            assert "result" in doc, (label, doc)
            out[label] = doc["result"]
    finally:
        conn.close()
    return out


def _requests(tools):
    reqs = []
    for name, schema in sorted(tools.items()):
        for variant, args in _calls_for(name, schema):
            reqs.append(((name, variant), "tools/call",
                         {"name": name, "arguments": args}))
    reqs.append((("kb://stats", "alpha"), "resources/read", {"uri": "kb://stats"}))
    reqs.append((("tools/list", "-"), "tools/list", {}))
    return reqs


@pytest.fixture(scope="module")
def wire(tmp_path_factory):
    """Drive every call once per caller, over two real servers, and cache it all.

    HOME is pointed at a scratch dir for the whole drive: `--config` alone does not
    isolate a contextlake process from the operator's own `~/.contextlake`.
    """
    root = tmp_path_factory.mktemp("scope-wire")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(root / "home"))
        (root / "home").mkdir()
        server_mod.reset_refusal_log()
        server_mod._reset_identity_fault_log()

        store_path = _populate(root)
        tools, resources = _registry(store_path)
        reqs = _requests(tools)

        records = []
        _scoped_rec, scoped_key = keys_mod.create(records, "scoped")
        _open_rec, open_key = keys_mod.create(records, "unscoped")
        records[0].policy["repos"] = SCOPE
        # `external` too, so `get_repo_links` has a positive control: connector
        # content lives in `(external)`, which the repo patterns never reach.
        records[0].policy["external"] = True
        key_file = root / "keys.json"
        keyfile.write_document(key_file, [r.to_dict() for r in records])
        keyring = keyfile.Keyring.load(key_file)

        from test_mcp_identity_propagates import bound_server

        answers = {}
        store = SqliteStore(store_path)
        try:
            # Keyring AND shared token: both live, on ONE server, through the proxy.
            app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                                 token=SHARED, keyring=keyring, embedder=_Embedder(),
                                 vector_store=_Vectors(store_path))
            with bound_server(app) as hostport:
                answers["scoped"] = _session(hostport, scoped_key, reqs)
                answers["unscoped"] = _session(hostport, open_key, reqs)
                answers["token"] = _session(hostport, SHARED, reqs)
            # Token only: no keyring, so no grant source and NO PROXY. The bare store
            # is the reference every caller with no repo scope must agree with.
            app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                                 token=SHARED, embedder=_Embedder(),
                                 vector_store=_Vectors(store_path))
            with bound_server(app) as hostport:
                answers["bare"] = _session(hostport, SHARED, reqs)
        finally:
            store.close()
            server_mod.reset_refusal_log()
            server_mod._reset_identity_fault_log()
    return {"tools": tools, "resources": resources, "answers": answers,
            "labels": [label for label, _m, _p in reqs],
            "args": {label: params.get("arguments", {}) for label, _m, params in reqs}}


def _text(result) -> str:
    return json.dumps(result, sort_keys=True).lower()


# --------------------------------------------------------------------------
# Completeness: the wire catalogue is the registration
# --------------------------------------------------------------------------
def test_the_wire_catalogue_is_every_registered_tool(wire):
    """The calls below are built from the LOCAL registration. This proves the server
    on the wire serves that same set to an unscoped caller, so nothing registered is
    missing from the drive and nothing on the wire was never driven."""
    listed = {t["name"] for t in wire["answers"]["unscoped"][("tools/list", "-")]["tools"]}
    assert listed == set(wire["tools"]), (
        sorted(listed ^ set(wire["tools"])))
    assert wire["resources"] == ["kb://stats"], (
        "a resource was registered that this test does not read; add it to "
        f"`_requests`: {wire['resources']}")
    driven = {label[0] for label in wire["labels"]}
    assert set(wire["tools"]) <= driven
    # The semantic pair registers only with an embedder and a vector store. They are
    # the R07 tools, so their absence would be a silent hole in this file.
    assert {"semantic_search", "hybrid_search"} <= set(wire["tools"])


def _tool_labels():
    """Parametrize over the registration, computed once at collection."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        st = SqliteStore(Path(d) / "kb.sqlite")
        try:
            srv = build_server(st, embedder=_Embedder(),
                               vector_store=_Vectors(Path(d) / "kb.sqlite"))
            return sorted(t.name for t in srv._tool_manager.list_tools())
        finally:
            st.close()


_TOOLS = _tool_labels()


def _labels_of(wire, tool):
    return [label for label in wire["labels"] if label[0] == tool]


# --------------------------------------------------------------------------
# 1. Agreement: no repo scope means the answer the bare store gives
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool", [*_TOOLS, "kb://stats"])
def test_callers_with_no_repo_scope_get_the_bare_store_answer(wire, tool):
    """An unscoped key and the shared token, both on the keyring server, agree with
    the shared token on a server that has no proxy at all.

    Before the fix the first two were over-denied identically: the proxy's
    early-return methods served everything to an empty scope, while `get_node` ran
    the partition predicate, which denied `@ingest:`, `(external)` and `(system)`
    BEFORE it reached the "no patterns means allow" line. So `search_code` showed a
    node and `get_node` on the same id said null. Comparing the two keyring callers
    with each other could not see it; the bare-store server can.
    """
    answers = wire["answers"]
    differs = []
    for label in _labels_of(wire, tool):
        bare = answers["bare"][label]
        for caller in ("unscoped", "token"):
            if answers[caller][label] != bare:
                differs.append((caller, label[1], wire["args"][label],
                                _text(answers[caller][label])[:400], _text(bare)[:400]))
    assert differs == [], differs


# --------------------------------------------------------------------------
# 2. No denied name or content reaches the scoped key
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool", [*_TOOLS, "kb://stats"])
def test_a_scoped_key_never_receives_a_denied_repository(wire, tool):
    """Repo B (`bravo/...`), repo C (`alpha/hidden/vault`, which `alpha/*` does not
    reach) and repo D (`alpha__ledger`) are denied to a key scoped `alpha/*`.

    A denied NAME is allowed only when the caller sent it (an echo of the argument is
    not a disclosure). Denied CONTENT is never allowed: it appears in no argument.
    """
    leaks = []
    for label in _labels_of(wire, tool):
        args_text = json.dumps(wire["args"][label]).lower()
        body = _text(wire["answers"]["scoped"][label])
        for token in DENIED_NAMES + DENIED_CONTENT:
            if token in body and token not in args_text:
                at = body.index(token)
                leaks.append((label[1], wire["args"][label], token,
                              body[max(0, at - 120):at + 80]))
    assert leaks == [], leaks


# --------------------------------------------------------------------------
# 3. Positive control: the scoped key still gets its own repo
# --------------------------------------------------------------------------
# What the `alpha` variant must return to the key scoped `alpha/*`. Each is content
# that exists only because the scope ADMITTED something; an empty answer fails it.
def _admitted_counts(scoped, bare):
    """Store-wide counts for the scoped key: some rows, and fewer than the store holds.

    A substring such as `"nodes": ` matches `"nodes": 0`, so it passed for a key that
    was admitted nothing. These are values that MOVE between an admitted scope and an
    empty one. The three repositories `alpha/*` admits are A1, A2 and A3.
    """
    got, whole = scoped["structuredContent"], bare["structuredContent"]
    return got["repos"] == 3 and 0 < got["nodes"] < whole["nodes"]


_POSITIVE = {
    "graph_stats": _admitted_counts,
    "get_node": "refundalpha",
    "get_neighbors": A_SETTLE,
    "search_code": "settleorders",
    "find_definition": "svc.py",
    "find_callers": "shipparcel",
    "find_callees": "settleorders",
    "find_dependents": A2,
    "repo_dependencies": A2,
    "repo_flow": A2,
    "repo_event_flow": A2,
    "blast_radius": "shipparcel",
    "get_wiki": "alphamark-wiki",
    "get_generated_doc": "alphamark-api",
    "get_readme": "alphamark-readme",
    "get_repo_brief": '"found": true',
    "list_repos": A1,
    "get_repo_links": "ord-1",
    "graph_health": '"repos": 3',
    "shortest_path": '"found": true',
    "ask": "shipparcel",
    # The relevance floor. B holds the exact-name match for "settle", so a floor
    # that probes with `limit=1` and filters after found nothing for this key.
    "semantic_search": "settleorders",
    "hybrid_search": "settleorders",
}
# Tools with no positive control, each with the reason. Never silent.
_NO_POSITIVE = {
    "who_knows": "answers from `git log` of the clone, and this test may not run git",
    "get_fleet_doc": "refused to every scoped key by design (`_fleet_readable`): it "
                     "describes the whole fleet and has no repo argument to check",
}


def test_every_tool_has_a_positive_control_or_a_stated_reason():
    unclassified = set(_TOOLS) - set(_POSITIVE) - set(_NO_POSITIVE)
    assert unclassified == set(), (
        f"no positive control for {sorted(unclassified)}: add the content the scoped "
        "key must receive to _POSITIVE, or a reason to _NO_POSITIVE")


@pytest.mark.parametrize("tool", sorted(_POSITIVE))
def test_a_scoped_key_still_receives_its_own_repository(wire, tool):
    label = (tool, "alpha")
    result = wire["answers"]["scoped"][label]
    assert not result.get("isError"), (wire["args"][label], _text(result)[:400])
    expected = _POSITIVE[tool]
    if callable(expected):
        assert expected(result, wire["answers"]["bare"][label]), (
            wire["args"][label], _text(result)[:600])
    else:
        assert expected.lower() in _text(result), (
            wire["args"][label], _text(result)[:600])


def test_the_scoped_stats_resource_counts_only_admitted_nodes(wire):
    def counts(caller):
        result = wire["answers"][caller][("kb://stats", "alpha")]
        return json.loads(result["contents"][0]["text"])

    scoped, bare = counts("scoped"), counts("bare")
    assert scoped["repos"] == 3, scoped
    assert 0 < scoped["nodes"] < bare["nodes"], (scoped, bare)


# --------------------------------------------------------------------------
# The three fixes underneath, each asserted where it lives
# --------------------------------------------------------------------------
def test_no_patterns_reaches_every_partition_the_early_returns_serve():
    """R02 at the predicate. Empty patterns is "nobody wrote a scope", and the proxy's
    early returns serve such a caller every row. The predicate used to rule on
    sentinels and `@ingest:` before its "no patterns means allow" line, so `get_node`
    said null for a row `search` had just returned."""
    from contextlake.kb import scope

    for partition in ("@ingest:notes", EXTERNAL_REPO, SYSTEM_REPO, A1):
        assert scope.owns_partition(partition, []) is True, partition
    for sentinel in (EXTERNAL_REPO, SYSTEM_REPO):
        assert scope.sentinel_visible(sentinel, []) is True, sentinel
    # The litigated rules for a SCOPED caller stand: `**` reaches no `@ingest:`,
    # and `(external)` is its own axis even under `**`.
    assert scope.owns_partition("@ingest:notes", ["**"]) is False
    assert scope.owns_partition(EXTERNAL_REPO, ["**"], external=False) is False


class _Ring:
    def __init__(self, policies):
        self.policies = policies

    def reload_if_changed(self):
        return False

    def resolve(self, presented):  # pragma: no cover - not reached
        return None

    def policy_for(self, key_id):
        return self.policies.get(key_id)


@pytest.fixture
def two_repo_store(tmp_path):
    st = SqliteStore(tmp_path / "kb.sqlite")
    st.upsert_nodes(A1, [_fn(A_SETTLE, A1, "SettleOrders", "svc.py")])
    st.upsert_nodes(B, [_fn(B_SETTLE, B, "Settle", "bravomark/pay.py")])
    for rid in (A1, B):
        st.upsert_repo(Repo(id=rid, path=str(tmp_path / rid.replace("/", "__"))))
    try:
        yield st
    finally:
        st.close()


def test_a_key_record_that_disappears_mid_request_reads_nothing(two_repo_store):
    """R09. Driven through the PRODUCTION lambda shape, `grant_source.repo_scope(
    principal)`, not through `lambda: None`, which production never produced.

    The record is present when `check()` admits the request and gone (revoked then
    pruned) by the time the store is read. The repo axis used to read that as "no
    scope" and serve the whole fleet; the tool axis refused the same case.
    """
    from contextlake.kb import grants
    from contextlake.kb.scoped_store import (
        ScopedStore,
        open_request_scope,
        reset_request_scope,
    )

    principal = server_mod.Principal("k_gone")
    ring = _Ring({"k_gone": {}})
    gate = grants.GrantCheck(ring)
    proxy = ScopedStore(two_repo_store, lambda: gate.repo_scope(principal))

    # Positive control: a live record with no scope reads both repositories.
    token = open_request_scope()
    try:
        gate.check(principal, "list_repos")
        assert sorted(r.id for r in proxy.list_repos()) == sorted([A1, B])
    finally:
        reset_request_scope(token)

    token = open_request_scope()
    try:
        gate.check(principal, "list_repos")       # admitted while the record exists
        del ring.policies["k_gone"]               # ... and it is gone before the read
        assert gate.repo_scope(principal) is None
        assert proxy.list_repos() == []
        assert proxy.search("settle") == []
        assert proxy.get_node(A_SETTLE) is None
    finally:
        reset_request_scope(token)

    # The shared token has no record by design, and keeps reading everything.
    assert gate.repo_scope(server_mod.Principal(server_mod.SHARED_TOKEN_KEY_ID)) == (
        [], False)
    assert gate.repo_scope(None) is None


def test_a_scoped_search_fills_its_limit_from_rows_the_caller_may_read(two_repo_store):
    """R07. B holds the exact-name match for "settle" and ranks first; A holds a
    prefix match. Capped to one row BEFORE the filter, the scoped answer was empty,
    and the relevance floor then called the term absent."""
    from contextlake.kb.relevance import term_anchors
    from contextlake.kb.scoped_store import (
        ScopedStore,
        open_request_scope,
        reset_request_scope,
    )

    # The premise, on the bare store: B's row is the one `limit=1` returns.
    assert [n.id for n in two_repo_store.search("settle", limit=1)] == [B_SETTLE]

    proxy = ScopedStore(two_repo_store, lambda: ([SCOPE], False))
    token = open_request_scope()
    try:
        assert [n.id for n in proxy.search("settle", limit=1)] == [A_SETTLE]
        assert term_anchors(proxy, "settle") == ([], True)
        # A term only the denied repo holds is still absent for this key.
        assert proxy.search("bravomark", limit=5) == []
    finally:
        reset_request_scope(token)
