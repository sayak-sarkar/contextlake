"""Connector content over a real socket, for keys with and without connector access.

`test_scope_over_the_wire.py` proves the repo axis. This file adds the connector
canaries to the same fleet and drives every registered tool with five callers:

* `unscoped` -- no `--repos`, no `--no-external`. The control: unchanged, so it must
  agree with the shared token on a server with no proxy at all.
* `ext` -- `--repos alpha/*  --external`. Reads the connector canaries of its repos.
* `noext` -- `--repos alpha/*` alone. Reads none of them.
* `nox` -- `--no-external`, no repo limit. Reads every repository and none of them.
* `both` -- a hand-edited record with `no_external` AND `external`. Fails closed.

The canaries: an `@enrich:` document and its link, the `(external)` ticket titles, an
`@ingest:` document, wiki pages that may quote connected sources (unmarked, so
unknown), and their `@wiki:` section nodes. A page whose footer records 0 connector
items, and its section, are the positive control: a key without connector access
still reads it.
"""

from __future__ import annotations

import json

import pytest
import test_scope_over_the_wire as w

from contextlake.kb import keyfile
from contextlake.kb import keys as keys_mod
from contextlake.kb import server as server_mod
from contextlake.kb.model import Node
from contextlake.kb.server import build_http_app
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.wiki.generate import provenance_footer

ENRICH_DOC = "enrich_orders_doc"
WIKI_TAINTED = "@wiki:alpha/orders:0"
WIKI_CLEAN = "@wiki:alpha/shipping:0"

# Text that exists only in connector content or in a page that may quote it. In any
# answer to a key without connector access, each one is a leak.
CANARIES = ("enrichmark", "ord-1", "pay-9", "orders ticket", "payments ticket",
            "runbooksettle",
            "alphamark-wiki", "bravomark-wiki", "wikisectionmark", ENRICH_DOC)
RESTRICTED = ("noext", "nox", "both")


def _add_canaries(root, store_path):
    kb = root / "kb"
    st = SqliteStore(store_path)
    try:
        st.upsert_nodes("@enrich:alpha/orders", [Node(
            id=ENRICH_DOC, repo="@enrich:alpha/orders", kind="document",
            name="enrichmark notes", file="https://tracker.invalid/ENR-1",
            attrs={"snippet": "enrichmark snippet", "source": "api"})])
        st.upsert_edges("@enrich:alpha/orders",
                        [w._edge(w.A_REFUND, ENRICH_DOC, "documented_by")])
        for nid, rid, name, flag in (
                (WIKI_TAINTED, w.A1, "wikisectionmark orders", True),
                (WIKI_CLEAN, w.A2, "shipsection overview", False)):
            st.upsert_nodes(f"@wiki:{rid}", [Node(
                id=nid, repo=f"@wiki:{rid}", kind="wiki", name=name,
                file=f"wiki/{rid.replace('/', '__')}.md",
                attrs={"advisory": True, "source_repo": rid, "symbol_links": True,
                       "connector_text": flag})])
    finally:
        st.close()
    # A page generated with no connector items in its prompt.
    (kb / "wiki" / "alpha__shipping.md").write_text(
        "# alpha/shipping\n\nSHIPMARK-WIKI\n" + provenance_footer(
            {"files": ["ship.py"], "head": "c1", "repo": w.A2, "node_count": 1,
             "external": []}), encoding="utf-8")


def _call(name, **arguments):
    return (("extra", name, json.dumps(arguments, sort_keys=True)), "tools/call",
            {"name": name, "arguments": arguments})


# Aimed calls for the controls. The sweep below uses the shared request table, whose
# arguments never name a canary.
EXTRA = [
    _call("search_code", query="enrichmark"),
    _call("get_node", node_id=ENRICH_DOC),
    _call("get_node", node_id=w.JIRA_A),
    _call("get_wiki", repo=w.A1),
    _call("get_wiki", repo=w.A2),
    _call("get_wiki", repo=w.B),
    _call("search_code", query="wikisectionmark"),
    _call("search_code", query="shipsection"),
    _call("search_code", query="runbooksettle"),
    _call("search_code", query="settle"),
]


@pytest.fixture(scope="module")
def wire(tmp_path_factory):
    root = tmp_path_factory.mktemp("connector-wire")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(root / "home"))
        (root / "home").mkdir()
        server_mod.reset_refusal_log()
        server_mod._reset_identity_fault_log()

        store_path = w._populate(root)
        _add_canaries(root, store_path)
        tools, _resources = w._registry(store_path)
        reqs = w._requests(tools) + EXTRA

        records, creds = [], {}
        policies = {
            "unscoped": {},
            "ext": {"repos": w.SCOPE, "external": True},
            "noext": {"repos": w.SCOPE},
            "nox": {"no_external": True},
            "both": {"no_external": True, "external": True},
        }
        for name, policy in policies.items():
            record, creds[name] = keys_mod.create(records, name)
            record.policy.update(policy)
        key_file = root / "keys.json"
        keyfile.write_document(key_file, [r.to_dict() for r in records])
        keyring = keyfile.Keyring.load(key_file)

        from test_mcp_identity_propagates import bound_server

        answers = {}
        store = SqliteStore(store_path)
        try:
            app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                                 token=w.SHARED, keyring=keyring, embedder=w._Embedder(),
                                 vector_store=w._Vectors(store_path))
            with bound_server(app) as hostport:
                for name in policies:
                    answers[name] = w._session(hostport, creds[name], reqs)
            app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                                 token=w.SHARED, embedder=w._Embedder(),
                                 vector_store=w._Vectors(store_path))
            with bound_server(app) as hostport:
                answers["bare"] = w._session(hostport, w.SHARED, reqs)
        finally:
            store.close()
            server_mod.reset_refusal_log()
            server_mod._reset_identity_fault_log()
    return {"answers": answers, "labels": [label for label, _m, _p in reqs],
            "args": {label: params.get("arguments", {}) for label, _m, params in reqs}}


def _text(result) -> str:
    return json.dumps(result, sort_keys=True).lower()


def _extra(wire, caller, name, **arguments):
    return _text(wire["answers"][caller][("extra", name,
                                          json.dumps(arguments, sort_keys=True))])


def test_the_unscoped_key_still_gets_the_bare_store_answer(wire):
    """The control: no repo scope and no `--no-external` means nothing changed."""
    differs = [label for label in wire["labels"]
               if wire["answers"]["unscoped"][label] != wire["answers"]["bare"][label]]
    assert differs == []


@pytest.mark.parametrize("caller", RESTRICTED)
def test_a_key_without_connector_access_receives_no_connector_text(wire, caller):
    leaks = []
    for label in wire["labels"]:
        args_text = json.dumps(wire["args"][label]).lower()
        body = _text(wire["answers"][caller][label])
        for token in CANARIES:
            if token in body and token not in args_text:
                at = body.index(token)
                leaks.append((label, token, body[max(0, at - 100):at + 60]))
    assert leaks == [], leaks


@pytest.mark.parametrize("caller", ["unscoped", "ext"])
def test_a_key_with_connector_access_still_receives_it(wire, caller):
    """Positive control: without it the sweep above passes for a server that hides
    the canaries from everyone."""
    assert ENRICH_DOC in _extra(wire, caller, "search_code", query="enrichmark")
    assert "enrichmark" in _extra(wire, caller, "get_node", node_id=ENRICH_DOC)
    assert "ord-1" in _extra(wire, caller, "get_node", node_id=w.JIRA_A)
    assert "alphamark-wiki" in _extra(wire, caller, "get_wiki", repo=w.A1)
    assert WIKI_TAINTED in _extra(wire, caller, "search_code", query="wikisectionmark")


@pytest.mark.parametrize("caller", RESTRICTED)
def test_a_key_without_connector_access_still_reads_its_code_and_clean_pages(wire,
                                                                             caller):
    assert "shipmark-wiki" in _extra(wire, caller, "get_wiki", repo=w.A2)
    assert WIKI_CLEAN in _extra(wire, caller, "search_code", query="shipsection")
    withheld = _extra(wire, caller, "get_wiki", repo=w.A1)
    assert '"found": true' in withheld and "withheld" in withheld
    assert w.A_SETTLE.lower() in _extra(wire, caller, "search_code", query="settle")


@pytest.mark.parametrize("caller", ["nox", "both"])
def test_no_external_with_no_repo_limit_reads_every_repository(wire, caller):
    """`--no-external` takes connector content away, not repositories."""
    assert w.B_SETTLE.lower() in _extra(wire, caller, "search_code", query="settle")
    bravo = _extra(wire, caller, "get_wiki", repo=w.B)
    assert '"found": true' in bravo and "bravomark-wiki" not in bravo


def test_ingested_documents_reach_only_the_unscoped_key(wire):
    assert w.INGEST_DOC in _extra(wire, "unscoped", "search_code", query="runbooksettle")
    for caller in ("ext", *RESTRICTED):
        assert w.INGEST_DOC not in _extra(wire, caller, "search_code",
                                          query="runbooksettle")
