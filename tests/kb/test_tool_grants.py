"""The access-control axes: what a key may call, and at which surface.

MEASURED FIRST, twice, on a live `kb serve --transport http --keys-only` server
with a key created `--tools none --repos nothing-matches/*`.

* 2026-09-05: `tools/list` answered with all 23 registered tools and
  `graph_stats` then ran and returned a result. The scope was recorded and read
  by nothing.
* 2026-09-06: `tools/list` answered with an empty list and `graph_stats` was
  refused, naming the group that would grant it.

This file is what stops that going back. Three properties, and each of them was
a way the first attempt could have shipped looking finished:

**One rule, four surfaces.** `tools/call` crosses `bounded_tool.guarded`;
`tools/list` and `resources/list` are async catalogue methods that cross
nothing; the `kb://stats` resource read crosses nothing either. A gate on one of
the four is a gate a caller walks around by using another, and only the first is
covered by the tool tests every other file in this directory writes.

**Fail closed on a name nobody classified.** The group table is hand-written and
the tool set grows. A 26th tool that nobody put in a group is denied to every
key, including one granted `all`, and the completeness test below reads the
BUILT server rather than a literal list so the gap is also loud.

**Positive controls everywhere.** A refusal assertion passes for free against a
check that denies everything, and a denial assertion passes for free against a
tool that returns nothing for an unrelated reason. Every denial here is paired
with the same call succeeding under a grant that allows it.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest
from mcp import Client

from contextlake.kb import grants
from contextlake.kb import server as server_mod
from contextlake.kb.server import (
    SHARED_TOKEN_KEY_ID,
    GrantDenied,
    Principal,
    build_server,
)
from contextlake.kb.store.sqlite_store import SqliteStore

PRINCIPAL = Principal("k_test")


class _Ring:
    """The read shape `GrantCheck` calls: one method, `policy_for`."""

    def __init__(self, policies: dict[str, dict | None]) -> None:
        self._policies = policies

    def reload_if_changed(self) -> bool:
        return False

    def resolve(self, presented):  # pragma: no cover - the gate is not exercised
        return None

    def policy_for(self, key_id: str):
        return self._policies.get(key_id)


def _check(policy):
    return grants.GrantCheck(_Ring({PRINCIPAL.key_id: policy}))


def _flatten(exc) -> str:
    """Every message in a nested ExceptionGroup, joined.

    `str(ExceptionGroup)` is "unhandled errors in a TaskGroup", which contains
    nothing about the refusal. An assertion on that string passes for a crash
    as readily as for the denial it claims to check.
    """
    parts = [str(exc)]
    for child in getattr(exc, "exceptions", ()):
        parts.append(_flatten(child))
    return " | ".join(parts)


@pytest.fixture
def store(tmp_path):
    st = SqliteStore(tmp_path / "kb.sqlite")
    try:
        yield st
    finally:
        st.close()


# ==========================================================================
# The group table against the server that was actually built
# ==========================================================================
def test_the_group_table_covers_every_registered_tool_and_resource(store):
    """A tool nobody classified is in no group, so `all` does not reach it.

    Driven off the BUILT server, and off its resources as well as its tools.
    `kb://stats` is registered with `@mcp.resource` and crosses no wrapper, so a
    tools-only enumeration would miss it and the `stats` group would gate three
    names out of four while reading complete.

    The runtime backstop for the same gap is asserted separately below: a test
    can be skipped, and the answer to an unclassified tool has to be `deny`
    whether or not anyone runs this.
    """
    class _Embedder:
        def embed(self, texts):
            return [[0.0] for _ in texts]

    class _Vectors:
        def search(self, vec, k=10, repo=None):
            return []

        def count(self, repo=None):
            return 0

    # A LOCAL build, deliberately. A networked one has the filtered catalogue
    # installed, so a completeness check written against it would pass on a
    # subset of what is registered and prove nothing.
    server = build_server(store, embedder=_Embedder(), vector_store=_Vectors())
    registered = {tool.name for tool in server._tool_manager.list_tools()}
    registered |= {
        str(resource.uri)
        for resource in asyncio.run(server.list_resources())
    }

    classified = set()
    for members in grants.TOOL_GROUPS.values():
        classified |= members

    assert registered - classified == set(), (
        "these names are registered on the server and are in no tool group, so "
        "no key can be granted them and `--tools all` does not reach them: "
        f"{sorted(registered - classified)}")
    assert classified - registered == set(), (
        "these names are in a tool group and are registered nowhere, so a "
        "group promises something the server does not offer: "
        f"{sorted(classified - registered)}")


def test_read_is_every_group_except_semantic(store):
    """`read` is the default an operator types, so what it covers is load-bearing.

    It includes `owners`, which the ticket's table did not. The reason is the
    all-legs rule below: `who_knows` is one of `ask`'s legs, so `owners` outside
    `read` would deny the flagship tool to every key created with the group the
    help text recommends.
    """
    granted = grants._expand("read")
    assert granted == set(grants.TOOL_GROUPS["graph"]) | set(
        grants.TOOL_GROUPS["search"]) | set(grants.TOOL_GROUPS["docs"]) | set(
        grants.TOOL_GROUPS["stats"]) | set(grants.TOOL_GROUPS["owners"])
    assert not (granted & grants.TOOL_GROUPS["semantic"])
    assert grants.ASK_LEGS <= granted, sorted(grants.ASK_LEGS - granted)


def test_the_ask_legs_match_the_tools_ask_actually_calls(store):
    """The leg list is hand-written; `ask`'s body is what decides.

    Read off the SOURCE of the registered function rather than trusted, because
    a ninth route added to `ask` with no entry here would be a tool a key
    granted `ask` reaches while its own grant denies it, which is the hole the
    all-legs rule exists to close.
    """
    server = build_server(store)
    source = inspect.getsource(server._tool_manager.get_tool("ask").fn)

    def calls(name: str) -> bool:
        """A BARE call, never an attribute access.

        `ask` calls `store.get_node(...)` and `store.list_repos(...)`, which
        share a name with two registered tools and are not them. A plain
        `f"{name}(" in source` test reads both as legs, which is a false
        positive in the safe direction and still a test nobody can trust.
        """
        return re.search(rf"(?<![\w.]){re.escape(name)}\s*\(", source) is not None

    for leg in sorted(grants.ASK_LEGS):
        assert calls(leg), (
            f"{leg} is listed as an `ask` leg and `ask` never calls it")

    registered = {tool.name for tool in server._tool_manager.list_tools()}
    called = {name for name in registered if name != "ask" and calls(name)}
    assert called <= grants.ASK_LEGS, (
        "`ask` calls tools that are not in ASK_LEGS, so a key granted `ask` "
        f"reaches them below the wrapper: {sorted(called - grants.ASK_LEGS)}")
    assert called == grants.ASK_LEGS, (
        "ASK_LEGS names tools `ask` no longer calls, so the all-legs rule "
        f"denies `ask` for nothing: {sorted(grants.ASK_LEGS - called)}")


# ==========================================================================
# The tools axis
# ==========================================================================
def test_an_unset_tools_axis_grants_every_tool():
    """An axis nobody wrote is not a scope, and reading it as deny breaks upgrade.

    `keys.create` stores `policy={}`, a bare `kb keys create alice` stores `{}`,
    and no verb edits the policy on a live key. Deny-by-default on an ABSENT
    axis would stop every key issued before this release with no route to widen
    one back. Deny-by-default is the rule WITHIN an axis someone scoped.
    """
    check = _check({})
    for name in ("graph_stats", "blast_radius", "who_knows", "ask"):
        check.check(PRINCIPAL, name)


def test_a_tool_outside_the_grant_is_refused_and_the_message_names_its_group():
    """The measured defect, at the call surface.

    The positive control is the same call under `--tools stats`. Without it this
    passes against a check that denies everything, which is not enforcement.
    """
    with pytest.raises(GrantDenied) as refusal:
        _check({"tools": "docs"}).check(PRINCIPAL, "graph_stats")
    message = str(refusal.value)
    assert "graph_stats" in message
    assert '"stats"' in message, (
        f"the refusal does not name the group that would grant it: {message!r}")

    _check({"tools": "stats"}).check(PRINCIPAL, "graph_stats")


def test_tools_none_grants_nothing_and_is_not_the_same_as_unset():
    """`none` and unset are opposite instructions and must not collapse.

    They stored identically before this work, which is why `--tools ""` is
    refused at create rather than dropped.
    """
    check = _check({"tools": "none"})
    for name in ("graph_stats", "get_wiki", "ask"):
        with pytest.raises(GrantDenied):
            check.check(PRINCIPAL, name)
    _check({}).check(PRINCIPAL, "graph_stats")


def test_a_value_is_comma_separated_case_folded_and_stripped():
    """The parse an operator's shell will actually hand over."""
    check = _check({"tools": " Docs , STATS "})
    check.check(PRINCIPAL, "get_wiki")
    check.check(PRINCIPAL, "graph_stats")
    with pytest.raises(GrantDenied):
        check.check(PRINCIPAL, "blast_radius")


def test_a_tool_in_no_group_is_denied_even_to_a_key_granted_all():
    """The runtime backstop behind the completeness test.

    A test can be skipped and a table is hand-written, so the answer for a name
    nobody classified has to be `deny` on its own. `all` unions the groups
    rather than meaning "everything", which is what makes it fail closed.

    The message says it is a server-side gap, because it is: no key the operator
    can mint would grant it, so "ask for a wider key" would be wrong advice.
    """
    with pytest.raises(GrantDenied) as refusal:
        _check({"tools": "all"}).check(PRINCIPAL, "a_tool_added_next_release")
    assert "no tool group" in str(refusal.value)


def test_an_unrecognised_group_in_a_key_file_narrows_and_never_widens():
    """A hand-edited or downgraded key file must not raise and must not widen.

    Raising would turn every request into a crash on the grant path; ignoring
    the unknown name and falling back to a default would widen a key the
    operator was narrowing. It contributes nothing, so the rest of the value
    still decides.
    """
    check = _check({"tools": "stats,group_from_the_future"})
    check.check(PRINCIPAL, "graph_stats")
    with pytest.raises(GrantDenied):
        check.check(PRINCIPAL, "blast_radius")


def test_ask_needs_every_leg_it_routes_to():
    """`ask` reaches eight siblings BELOW the wrapper, so it cannot be split.

    `bounded_tool` registers the wrapper and returns the bare function. A key
    granted `ask` and denied `blast_radius` would otherwise reach `blast_radius`
    through the impact route, and nothing would refuse it.

    The positive control is `read`, which holds all eight.
    """
    with pytest.raises(GrantDenied) as refusal:
        _check({"tools": "search"}).check(PRINCIPAL, "ask")
    message = str(refusal.value)
    assert "blast_radius" in message and "who_knows" in message, message

    _check({"tools": "read"}).check(PRINCIPAL, "ask")


# ==========================================================================
# The owners axis
# ==========================================================================
@pytest.mark.parametrize("value", ["pseudonymous", "hidden"])
@pytest.mark.parametrize("tool", ["who_knows", "ask"])
def test_an_owners_axis_below_real_refuses_the_identity_tools(value, tool):
    """A gate, not a transform. A predicate cannot pseudonymise.

    Serving real names to a key that asked for `pseudonymous` is the fail-open
    this axis exists to prevent, so it refuses instead. `ask` rides with
    `who_knows` because it routes to it by its bare name and nothing here can
    see which route a question took.

    The positive control is `owners=real`, which allows both.
    """
    with pytest.raises(GrantDenied) as refusal:
        _check({"tools": "read", "owners": value}).check(PRINCIPAL, tool)
    assert "identity" in str(refusal.value)

    _check({"tools": "read", "owners": "real"}).check(PRINCIPAL, tool)


def test_the_owners_axis_leaves_every_other_tool_alone():
    """It gates the identity tools and nothing else.

    Without this, an owners rule that refused everything would pass every
    refusal assertion above and be indistinguishable from one that worked.
    """
    check = _check({"owners": "hidden"})
    for name in ("graph_stats", "get_wiki", "blast_radius"):
        check.check(PRINCIPAL, name)


def test_an_owners_value_outside_the_three_refuses():
    """A key file nobody can read the intent of is not read as `show everything`."""
    with pytest.raises(GrantDenied):
        _check({"owners": "anonymised"}).check(PRINCIPAL, "who_knows")


# ==========================================================================
# The two branches where "no policy" is the whole question
# ==========================================================================
def test_a_missing_record_denies_and_an_empty_policy_allows():
    """`None` and `{}` are opposite grants and are never collapsed.

    A key revoked or pruned between the gate admitting a request and the grant
    being read resolves to no record. Returning `{}` there -- which is what a
    `policy_for` written as `record.policy or {}` would do -- hands a revoked
    key a full grant for the length of that request.
    """
    with pytest.raises(GrantDenied) as refusal:
        grants.check_tool_grant(PRINCIPAL, "graph_stats", None)
    assert "no record" in str(refusal.value)

    grants.check_tool_grant(PRINCIPAL, "graph_stats", {})


def test_the_shared_token_keeps_the_full_grant():
    """It has no key record, so it has no policy to read.

    Denying it would break every deployment that sets CONTEXTLAKE_MCP_TOKEN the
    moment this lands. The operator who set that token asked for one credential
    with no scope, not for a key whose scope cannot be written.
    """
    shared = Principal(SHARED_TOKEN_KEY_ID)
    check = grants.GrantCheck(_Ring({}))
    for name in ("graph_stats", "who_knows", "ask"):
        check.check(shared, name)


def test_no_principal_denies_rather_than_returning():
    """A check that answers `allowed` for nobody is the one bug this cannot have.

    Unreachable through `guarded`, which refuses an unset principal above the
    anchor. Kept as an assertion for a direct call.
    """
    with pytest.raises(GrantDenied):
        grants.check_tool_grant(None, "graph_stats", {"tools": "all"})


# ==========================================================================
# The three surfaces, on a built server
# ==========================================================================
def _networked(store, policy):
    return build_server(store, networked=True,
                        grant_source=_check(policy))


def test_the_catalogue_shows_only_what_the_key_may_call(store, monkeypatch):
    """`tools/list` crosses no wrapper, so the call gate does not reach it.

    An agent that sees a tool it may not call spends a turn and a refusal per
    tool discovering its grant, and one that sees `blast_radius` builds a plan
    around it and fails mid-plan rather than at the start.

    The positive control is the unscoped key: the same server, the same call,
    the full list. Without it a filter that returned nothing would pass.
    """
    monkeypatch.setattr(server_mod, "current_principal", lambda: PRINCIPAL)

    async def names(server):
        async with Client(server) as client:
            return sorted(tool.name for tool in (await client.list_tools()).tools)

    scoped = asyncio.run(names(_networked(store, {"tools": "stats"})))
    assert scoped == ["graph_health", "graph_stats", "list_repos"], scoped

    unscoped = asyncio.run(names(_networked(store, {})))
    assert len(unscoped) == 23, unscoped
    assert "blast_radius" in unscoped


def test_the_catalogue_is_empty_when_the_server_cannot_name_the_caller(store,
                                                                       monkeypatch):
    """The row that forces the filter to install under `networked`, not `_enforcing`.

    `tools/list` crosses no wrapper, so `IdentityUnset` cannot protect it. A
    filter written as "the full list when there is no grant" hands the whole
    catalogue to a networked server whose identity propagation broke, while
    every tool refuses and every tool test passes.

    Empty is the honest answer, and it is loud: a client with no tools stops.
    """
    monkeypatch.setattr(server_mod, "current_principal", lambda: None)
    server_mod._reset_identity_fault_log()

    async def run(server):
        async with Client(server) as client:
            return (await client.list_tools()).tools

    # Enforcing, and NOT enforcing: a token-only networked server has no grant
    # source at all, and it is the one this row is really about.
    assert asyncio.run(run(_networked(store, {}))) == []
    assert asyncio.run(run(build_server(store, networked=True))) == []
    server_mod._reset_identity_fault_log()


def test_a_denied_call_is_refused_over_the_wire_with_a_readable_reason(store,
                                                                      monkeypatch):
    """`GrantDenied` is a `ToolError`, so mcp re-raises its message.

    Anything else collapses to `Error executing tool <name>`, and a refusal the
    caller cannot read is a refusal it retries.

    The positive control is the granted tool on the same server: without it a
    server that refused everything would pass.
    """
    monkeypatch.setattr(server_mod, "current_principal", lambda: PRINCIPAL)
    server = _networked(store, {"tools": "stats"})

    async def call(name):
        async with Client(server) as client:
            return await client.call_tool(name, {})

    refused = asyncio.run(call("get_fleet_doc"))
    assert refused.is_error
    text = refused.content[0].text
    assert "get_fleet_doc" in text and '"docs"' in text, text

    allowed = asyncio.run(call("graph_stats"))
    assert not allowed.is_error, allowed.content[0].text


def test_the_resource_catalogue_is_filtered_the_same_way(store, monkeypatch):
    """The fourth surface. `resources/list` advertises `kb://stats`.

    Unfiltered, a key holding `--tools docs` is told about a URI whose read is
    then refused, which is the turn-per-refusal cost the tool filter exists to
    avoid, on the catalogue a reader is least likely to check. The read gate
    already refuses, so this is not a hole; it is the same rule reaching the
    surface that shares its shape.

    The positive control is the `stats` key: same server, same call, the
    resource present.
    """
    monkeypatch.setattr(server_mod, "current_principal", lambda: PRINCIPAL)

    async def uris(server):
        async with Client(server) as client:
            return [str(r.uri) for r in (await client.list_resources()).resources]

    assert asyncio.run(uris(_networked(store, {"tools": "docs"}))) == []
    assert asyncio.run(uris(_networked(store, {"tools": "stats"}))) == ["kb://stats"]
    assert asyncio.run(uris(_networked(store, {}))) == ["kb://stats"]

    # And empty when the server cannot name the caller, for the same reason
    # `tools/list` is: this catalogue crosses no wrapper either.
    monkeypatch.setattr(server_mod, "current_principal", lambda: None)
    server_mod._reset_identity_fault_log()
    assert asyncio.run(uris(build_server(store, networked=True))) == []
    server_mod._reset_identity_fault_log()


def test_the_stats_resource_is_gated_with_the_group_that_holds_it(store,
                                                                 monkeypatch):
    """A resource read is not a tool call, and it answers what `graph_stats` answers.

    Ungated, the `stats` group gates three names out of four and a caller reads
    the counts by asking for a URI instead of calling a tool.

    MEASURED 2026-09-06 against a bound socket with two live keys: the principal
    does reach this body, correct per key. The phase-0 identity measurement
    covered the synchronous tool path only, so it was not proven by that.
    """
    monkeypatch.setattr(server_mod, "current_principal", lambda: PRINCIPAL)

    async def read(server):
        async with Client(server) as client:
            return await client.read_resource("kb://stats")

    with pytest.raises(BaseException) as refusal:
        asyncio.run(read(_networked(store, {"tools": "docs"})))
    # The SDK wraps a handler error in nested anyio ExceptionGroups, so the
    # message is not in `str(value)`. Flattened, or this asserts on the wrapper
    # text and passes for any failure at all, including a crash.
    assert '"stats"' in _flatten(refusal.value), _flatten(refusal.value)

    allowed = asyncio.run(read(_networked(store, {"tools": "stats"})))
    assert "repos" in allowed.contents[0].text


# ==========================================================================
# stdio reads nothing
# ==========================================================================
def test_a_local_server_reads_no_policy_and_installs_no_filter(store, monkeypatch):
    """Local-first: stdio serves one user who already has the files.

    The switch is the CALLER's, never a ContextVar. Asserted by making the
    ContextVar read explode: a local build that touched it would fail here
    rather than quietly working because the variable happened to be unset.
    """
    def _explode():  # pragma: no cover - reached only on a regression
        raise AssertionError("stdio read the identity ContextVar")

    monkeypatch.setattr(server_mod, "current_principal", _explode)
    server = build_server(store)

    async def run():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            answer = await client.call_tool("graph_stats", {})
            resource = await client.read_resource("kb://stats")
            return tools, answer, resource

    tools, answer, resource = asyncio.run(run())
    assert len(tools) == 23
    assert not answer.is_error
    assert "repos" in resource.contents[0].text


def test_kb_keys_does_not_load_the_mcp_server(tmp_path):
    """`kb keys` reads the group table, and that must not drag in the SDK.

    `grants` therefore imports `kb.server` inside a function rather than at
    module scope. Measured 2026-09-06 before the change: importing `keys_cmd`
    loaded neither `contextlake.kb.server` nor `mcp`, and a module-scope import
    would have put the whole SDK on the startup path of every
    `kb keys create`.

    Run in a subprocess because this test session has already imported both.
    """
    import subprocess
    import sys

    code = (
        "import sys; import contextlake.kb.cmds.keys_cmd as k;"
        "import contextlake.kb.grants as g;"
        "assert g.ENFORCED_AXES == "
        "('tools', 'repos', 'external', 'owners', 'rate', 'burst', 'cost_budget');"
        "print('server' if 'contextlake.kb.server' in sys.modules else '-',"
        "      'mcp' if 'mcp' in sys.modules else '-')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "- -", (
        f"importing kb keys pulled in {out}; grants must import kb.server "
        "lazily")
