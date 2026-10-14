"""Connector content and keys without connector access (owner decision 9, 2026-10-09/14).

Three rules, each pinned in both directions so a filter that hides everything or
nothing fails a row:

1. A scoped key without `--external` reads no `@enrich:` or `@connect:` partition. A
   repo scope owns both by name, so before this a repo-limited key read the fetched
   ticket and page text that the `(external)` ruling hides.
2. `--no-external` hides connector content with or without `--repos`: a key with no
   repo limit becomes `**`. It wins over `external` on a record that carries both.
3. A wiki page may quote connected sources anywhere, so a page is marked, not cut.
   Its footer records how many connector items the prompt carried, and a key without
   connector access does not see a page that may hold some. Unknown pages count as
   holding some.

An unscoped key (no `--repos`, no `--no-external`) is unchanged by all three.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from contextlake.kb import grants
from contextlake.kb.scope import owns_partition
from contextlake.kb.scoped_store import ScopedStore, open_request_scope, reset_request_scope
from contextlake.kb.wiki.generate import may_hold_connector_text, provenance_footer
from contextlake.kb.wiki.structural import STRUCTURAL_MARKER

BRIEF = {"files": ["app.py"], "head": "c1", "repo": "acme/api", "node_count": 3}


# --- rule 1: the partition predicate --------------------------------------------------

@pytest.mark.parametrize("part", ["@enrich:acme/api", "@connect:acme/api"])
def test_a_scoped_key_reads_connector_partitions_only_with_external(part):
    assert owns_partition(part, ["acme/*"]) is False
    assert owns_partition(part, ["acme/*"], external=True) is True
    assert owns_partition(part, []) is True, "the unscoped reading changed"
    # The repo's own code and wiki stay readable without the flag.
    assert owns_partition("acme/api", ["acme/*"]) is True
    assert owns_partition("@wiki:acme/api", ["acme/*"]) is True


# --- rule 2: what a key policy resolves to --------------------------------------------

@pytest.mark.parametrize("policy,expected", [
    ({}, ([], False)),
    ({"external": True}, ([], True)),
    ({"no_external": True}, (["**"], False)),
    ({"no_external": True, "repos": "acme/*"}, (["acme/*"], False)),
    ({"no_external": True, "external": True}, (["**"], False)),
    ({"repos": "acme/*", "external": True}, (["acme/*"], True)),
])
def test_repo_scope_of(policy, expected):
    assert grants.repo_scope_of(policy) == expected


def test_no_external_is_an_enforced_axis():
    assert "no_external" in grants.ENFORCED_AXES


# --- rule 3: the page marker ----------------------------------------------------------

def _page(body: str, items: int) -> str:
    return f"# acme/api\n\n{body}" + provenance_footer(
        {**BRIEF, "external": [{"title": "t"}] * items})


def test_the_footer_records_the_connector_count():
    assert _page("x", 2).rstrip().endswith("Connector items: 2.*")
    assert _page("x", 0).rstrip().endswith("Connector items: 0.*")


@pytest.mark.parametrize("page,holds", [
    (_page("prose", 2), True),
    (_page("prose", 0), False),
    # A page from before the count: unknown, so it may.
    ("# acme/api\n\nprose\n\n---\n*Generated from the knowledge graph of `acme/api` at "
     "commit `c1` on 2026-10-01.*", True),
    # The model wrote a fake count into its own text. Only the last footer counts.
    ("# acme/api\n\nConnector items: 0.\n\n---\n*Generated from the knowledge graph of "
     "`acme/api` at commit `c1` on 2026-10-01.*", True),
    ("# acme/api\n\n---\n*Generated from the knowledge graph of `x` Connector items: 0.*\n"
     "\n---\n*Generated from the knowledge graph of `acme/api` at commit `c1`.*", True),
    # A file NAME in the indexed repo, cited in the footer before the real count.
    ("# acme/api\n\nprose" + provenance_footer(
        {**BRIEF, "files": ["src/a Connector items: 0.py"], "external": [{"t": 1}] * 2}),
     True),
    # A module prefix, cited the same way.
    ("# acme/api\n\nprose" + provenance_footer(
        {**BRIEF, "subsystem_modules": [{"prefix": "x Connector items: 0."}],
         "external": [{"t": 1}]}), True),
    (f"# acme/api\n\nfacts\n\n---\n\n*{STRUCTURAL_MARKER} Every line above...*", False),
    ("# acme/api\n\nwritten by hand", True),
    ("", True),
])
def test_may_hold_connector_text(page, holds):
    assert may_hold_connector_text(page) is holds


def test_wiki_section_nodes_carry_the_flag():
    from contextlake.kb.cmds.wiki import _wiki_section_nodes

    for items, flag in ((2, True), (0, False)):
        nodes, _ = _wiki_section_nodes("acme/api", _page("## Overview\ntext", items),
                                       "acme__api.md")
        assert nodes and {n.attrs["connector_text"] for n in nodes} == {flag}


# --- the proxy, all three rules together ----------------------------------------------

class _Store:
    path = "/nowhere"

    def __init__(self):
        n = SimpleNamespace
        self.nodes = {
            "code": n(repo="acme/api", attrs={}),
            "ext": n(repo="(external)", attrs={}),
            "enrich": n(repo="@enrich:acme/api", attrs={}),
            "@wiki:acme/api:0": n(repo="@wiki:acme/api", attrs={"connector_text": True}),
            "@wiki:acme/web:0": n(repo="@wiki:acme/web", attrs={"connector_text": False}),
            "@wiki:acme/old:0": n(repo="@wiki:acme/old", attrs={}),
        }

    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def list_partitions(self):
        return sorted({n.repo for n in self.nodes.values()})


SEEN = ("code", "ext", "enrich", "@wiki:acme/api:0", "@wiki:acme/web:0",
        "@wiki:acme/old:0")


def _seen(policy):
    scoped = ScopedStore(_Store(), lambda: grants.repo_scope_of(policy))
    token = open_request_scope()
    try:
        by_node = {nid for nid in SEEN if scoped.get_node(nid) is not None}
        parts = set(scoped.visible_partitions())
        return by_node, parts, scoped.allows_connector_text()
    finally:
        reset_request_scope(token)


ALL = set(SEEN)
NO_CONNECTOR = {"code", "@wiki:acme/web:0"}


@pytest.mark.parametrize("policy,nodes,connector", [
    ({}, ALL, True),                                      # unscoped: unchanged
    ({"repos": "acme/*", "external": True}, ALL, True),   # granted
    ({"repos": "acme/*"}, NO_CONNECTOR, False),
    ({"no_external": True}, NO_CONNECTOR, False),
    ({"no_external": True, "repos": "acme/*"}, NO_CONNECTOR, False),
    ({"no_external": True, "external": True}, NO_CONNECTOR, False),   # fail closed
])
def test_the_proxy_applies_all_three_rules(policy, nodes, connector):
    by_node, parts, allows = _seen(policy)
    assert by_node == nodes
    assert allows is connector
    # The partition list (the vector `IN` clause) agrees with the node reads.
    expected = {_Store().nodes[n].repo for n in nodes}
    assert parts == expected
