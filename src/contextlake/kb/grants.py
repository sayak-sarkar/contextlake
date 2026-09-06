"""What a key may call, decided from the key's own policy block.

THIS MODULE IS NETWORK-ONLY AND IS IMPORTED NOWHERE AT MODULE SCOPE IN
``kb/server.py``. ``build_http_app`` imports it inside its own body, the same
deferred-import habit the keystore modules already use, so an stdio run never
loads it and local-first property P1 still holds: no grant model, no keyring, no
ContextVar read.

WHY A PREDICATE AND NOT A GRANT OBJECT. The check is callable at three surfaces
and only one of them is the tool wrapper. ``tools/call`` crosses
``bounded_tool.guarded``; ``tools/list`` is an async catalogue method that
crosses nothing; the ``kb://stats`` resource crosses nothing either. A grant
object plus a scoped store would need three different mechanisms for those three
surfaces. One function called from three places gives one vocabulary, one
refusal class, and one place to read the rule.

There is no cache, and that is deliberate rather than lazy. The check reads the
record through the same keyring object the gate resolved this request against,
after that gate ran ``reload_if_changed()``. So a key narrowed or revoked in the
file takes effect on the next request, with no invalidation window in which a
stale grant is still being honoured.

WHAT IS ENFORCED HERE AND WHAT IS NOT. ``tools`` and ``owners``. Not ``repos``,
not ``external``, not ``rate``, ``burst`` or ``cost_budget``. :data:`ENFORCED_AXES`
is the single statement of that, read by ``kb keys`` so the CLI cannot claim an
axis is live that this module does not check.

``repos`` is deferred, and the reason is a fact about the data rather than a
preference. A node id does not carry its repo: ``parse.symbol_id`` puts the repo
inside a SHA-256 digest, and ``make_id(repo_id, rel_path)`` runs
``ids.normalize_id``, which collapses ``/`` to ``_``, so ``team/api`` and
``team_api`` are indistinguishable in an id. A predicate cannot recover a repo
from a node id, and a prefix test on one is an unanchored string match deciding
an authorization question. Worse, three tools READ bounded and are not:
``repo_dependencies``, ``repo_flow`` and ``repo_event_flow`` take a required
``repo`` and return ``RepoEdgeOut`` rows whose ``src`` and ``dst`` are repo ids,
and ``_repo_side`` keeps a row when EITHER side matches. Correct row-level
scoping needs a store-layer filter, which is S4.3-acl-5.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - annotations only, never imported at runtime
    from .server import Principal


def _server():
    """``kb.server``, imported on first use and never at module scope.

    MEASURED 2026-09-06: `import contextlake.kb.cmds.keys_cmd` loads neither
    `contextlake.kb.server` nor `mcp`. `kb keys` reads :data:`ENFORCED_AXES` and
    :func:`validate_tools` from this module, so a module-scope
    `from .server import GrantDenied` would pull the whole MCP SDK into every
    `kb keys create`. The refusal class stays in `kb/server.py`, beside the
    `except` clause that classifies it; only the import moves.
    `test_kb_keys_does_not_load_the_mcp_server` is the guard.
    """
    from . import server

    return server


def _denied(message: str) -> Exception:
    """``GrantDenied(message)``, built through the lazy import above."""
    return _server().GrantDenied(message)


def _is_shared_token(key_id: str) -> bool:
    return key_id == _server().SHARED_TOKEN_KEY_ID


# The axes this module decides. Data, not prose: `kb keys` reads this list to
# label each axis, so "the CLI says enforced" and "the gate checks it" cannot
# drift into disagreeing. Adding an axis here without adding its rule below
# makes the CLI lie; the test that walks both is what stops that.
ENFORCED_AXES = ("tools", "owners", "rate", "burst", "cost_budget")

# The `kb://stats` resource, named here because it is gated like a tool but is
# not one. It is in the `stats` group: what it discloses is the repo, node and
# edge counts `graph_stats` already answers, so scoping one without the other
# would be a gate a caller walks around by reading a URI instead of calling a
# tool.
STATS_RESOURCE = "kb://stats"

# The groups an operator may name in `--tools`. Counted against the built
# server by `test_the_group_table_covers_every_registered_tool`, which reads
# `mcp._tool_manager` and the resource manager rather than a literal list, so a
# 26th tool that nobody classified turns that test red AND is denied at runtime
# by `_expand`'s fail-closed default below. Both, because a test can be skipped.
#
# `get_repo_links` sits in `docs` rather than `graph`: what it discloses is
# connector-fetched external links (issue trackers, wikis, design files), not
# code structure. It is the one assignment a reader may dispute, so the reason
# is written here rather than left to be re-derived.
TOOL_GROUPS: dict[str, frozenset[str]] = {
    "graph": frozenset({
        "get_node", "get_neighbors", "find_definition", "find_callers",
        "find_callees", "find_dependents", "repo_dependencies", "repo_flow",
        "repo_event_flow", "blast_radius", "shortest_path",
    }),
    "search": frozenset({"search_code", "ask"}),
    "docs": frozenset({
        "get_wiki", "get_generated_doc", "get_fleet_doc", "get_readme",
        "get_repo_brief", "get_repo_links",
    }),
    "stats": frozenset({"graph_stats", "list_repos", "graph_health",
                        STATS_RESOURCE}),
    "owners": frozenset({"who_knows"}),
    "semantic": frozenset({"semantic_search", "hybrid_search"}),
}

# `read` INCLUDES `owners`, and that is a change from the table on S4.3-acl-2.
# The reason is `ask`. `bounded_tool` registers the wrapper and returns the bare
# function, so `ask` reaches its eight legs below the wrapper and the only place
# it can be checked is at its own name. The rule below therefore refuses `ask`
# unless every leg is granted, and `who_knows` is one of those legs. With
# `owners` outside `read`, `--tools read` -- the default group, named in the
# help text -- would deny the flagship tool to every key that used it. What an
# `owners` grant costs is then governed by the OWNERS axis, which this module
# enforces, rather than by whether `who_knows` happened to be in the set.
_READ_GROUPS = ("graph", "search", "docs", "stats", "owners")

# Reserved names an operator types instead of a group.
ALL = "all"
READ = "read"
NONE = "none"
RESERVED = (ALL, READ, NONE)

# The names `ask` dispatches to, by their BARE function names, below the
# wrapper. Counted from `kb/server.py` on 2026-09-06; the comment at the access
# control anchor names the same eight. `test_the_ask_legs_match_what_ask_calls`
# reads the source so a ninth route added later cannot slip past this list.
ASK_LEGS = frozenset({
    "find_definition", "find_callers", "find_dependents", "blast_radius",
    "who_knows", "get_wiki", "get_repo_brief", "search_code",
})

# `--owners` values, from the flag's own `choices=`. `real` is the only one that
# lets an identity tool run: this release has no anonymiser on the network path,
# so a key that asked for pseudonyms is REFUSED rather than served real names.
# Serving real names to a key that asked for pseudonymous is the fail-open this
# axis exists to prevent, and it is the one a reader is most likely to "fix".
OWNERS_REAL = "real"
OWNERS_PSEUDONYMOUS = "pseudonymous"
OWNERS_HIDDEN = "hidden"
OWNERS_CHOICES = (OWNERS_REAL, OWNERS_PSEUDONYMOUS, OWNERS_HIDDEN)

# What the owners axis gates. `who_knows` is the identity tool. `ask` rides with
# it because `ask` routes to `who_knows` by its bare name and this module cannot
# see which route a question took; denying at the tool name is decidable from
# the name alone and is the fail-closed answer.
_IDENTITY_TOOLS = frozenset({"who_knows", "ask"})

# The methods a grant source calls on the keyring, checked once at build time.
# Kept separate from `KeyAuthMiddleware.KEYRING_METHODS`, which is the AUTH
# gate's contract: one list covering both would make a keyring usable for
# authentication depend on a method only the grant check needs.
KEYRING_METHODS = ("policy_for",)


class GroupError(ValueError):
    """An operator typed a `--tools` value this server has no group for.

    Raised at CREATE time by `kb keys`, so a typo cannot be minted onto a key.
    It is NOT what the server does with an unrecognised group in a hand-edited
    key file: there the value is denied, not refused, because refusing at the
    request would answer a call the operator meant to narrow.
    """


def parse_tools(value: str) -> list[str]:
    """The `--tools` string as the list of names the operator typed.

    Comma-separated, case-folded, surrounding whitespace stripped. Nothing else:
    no defaulting, no dropping of a name this module does not know. `_expand`
    decides what an unknown name means at request time and
    :func:`validate_tools` decides what it means at create time, and those two
    answers are deliberately different.
    """
    return [part.strip().casefold() for part in value.split(",") if part.strip()]


def validate_tools(value: str) -> list[str]:
    """The create-time check. Returns the parsed names or raises :class:`GroupError`.

    An empty value is refused rather than stored, and the message names `none`.
    The two are opposite instructions -- "I set no scope" and "I scope this key
    to nothing" -- and `_policy` drops an empty string, so a stored `""` would
    render as `unset` on `kb keys show` while the operator meant deny.
    """
    names = parse_tools(value)
    if not names:
        raise GroupError(
            "--tools was given an empty value. Leave the flag off to record no "
            "tool scope, or pass `--tools none` to scope the key to no tools "
            "at all. The two mean opposite things and an empty string cannot "
            "say which was meant.")
    known = set(TOOL_GROUPS) | set(RESERVED)
    unknown = [name for name in names if name not in known]
    if unknown:
        raise GroupError(
            f"--tools does not know {', '.join(sorted(unknown))}. "
            f"Groups: {', '.join(sorted(TOOL_GROUPS))}. "
            f"Reserved: {', '.join(RESERVED)}.")
    return names


def _expand(value: str) -> frozenset[str]:
    """The tool names a `--tools` value grants. Unknown names contribute nothing.

    FAIL-CLOSED IN BOTH DIRECTIONS, and both matter:

    * A name this server has no group for expands to the empty set rather than
      raising. A hand-edited or downgraded key file carrying a group a later
      release invented must narrow the key, never widen it and never turn every
      request into a 500 on the grant path.
    * A tool name absent from :data:`TOOL_GROUPS` is in no group, so `all` does
      not reach it either. That is the runtime backstop behind the completeness
      test: a 26th tool nobody classified is denied to every key rather than
      served unscoped while a skipped test would have said so.

    Groups are expanded PER CALL rather than compiled onto the record at create
    time. There is no stored set to go stale, so a key never carries a grant the
    running server no longer means; the cost is that a tool added to an existing
    group reaches a key issued before it existed, which the backstop above bounds
    to tools someone deliberately classified.
    """
    names = parse_tools(value)
    granted: set[str] = set()
    for name in names:
        if name == NONE:
            continue
        if name == ALL:
            for members in TOOL_GROUPS.values():
                granted |= members
        elif name == READ:
            for group in _READ_GROUPS:
                granted |= TOOL_GROUPS[group]
        else:
            granted |= TOOL_GROUPS.get(name, frozenset())
    return frozenset(granted)


def _check_tools(policy: Mapping[str, object], tool_name: str) -> None:
    """The tools axis. An ABSENT axis is not a scope and grants everything."""
    value = policy.get("tools")
    if value is None:
        # Nobody wrote a tool scope on this key, so there is nothing to deny
        # against. "All axes deny by default" is the rule WITHIN an axis someone
        # scoped, never a rule about an axis nobody wrote: every key issued
        # before this release stores `policy == {}` (keys.create), a bare
        # `kb keys create alice` stores `{}` too, and no verb edits the policy
        # on a live key. Reading an absent axis as deny would stop every one of
        # those keys at upgrade with no route to widen them back.
        return
    granted = _expand(str(value))
    if tool_name not in granted:
        raise _denied(_tool_denial(tool_name, granted))
    if tool_name == "ask":
        # `ask` is checked ONCE, here, and never again for the leg it dispatched
        # to: `bounded_tool` returns the bare function, so the eight legs run
        # below the wrapper. A key granted `ask` and denied `blast_radius` would
        # otherwise reach `blast_radius` through the impact route. Refusing
        # `ask` unless every leg is granted closes that at the only name this
        # module can see. Per-leg checking, which would let a narrower key use
        # the routes it does hold, is S4.3-acl-3.
        missing = sorted(ASK_LEGS - granted)
        if missing:
            raise _denied(
                f"ask is in this key's tool grant, but it routes questions to "
                f"other tools and calls them directly, so it cannot be granted "
                f"without them. Missing: {', '.join(missing)}. Add the groups "
                f"holding those tools, or use `--tools read`.")


def _tool_denial(tool_name: str, granted: frozenset[str]) -> str:
    """The refusal text, naming the group that would grant the tool.

    It distinguishes "no such tool" from "not yours" on purpose. A name this
    server does not register never reaches here at all, so the SDK's own
    unknown-tool error stands untouched; a registered tool outside the grant
    says so and names its group. An agent retries a transport-shaped error and
    does not retry a stated refusal, so collapsing the two costs the caller a
    retry loop against a door that will never open. Nothing is disclosed by
    naming a tool that exists: the catalogue ships in the package and the docs.
    """
    groups = sorted(name for name, members in TOOL_GROUPS.items()
                    if tool_name in members)
    if not groups:
        return (f"{tool_name} is in no tool group this server knows, so no key "
                "grants it. This is a server-side gap, not a problem with your "
                "key: report it rather than retrying.")
    where = " or ".join(f'"{name}"' for name in groups)
    # "reach", not "call". The `stats` group holds the `kb://stats` RESOURCE
    # beside three tools, and a resource is read rather than called, so one verb
    # has to cover both or the list beside it is wrong for one entry.
    have = ", ".join(sorted(granted)) if granted else "nothing"
    return (f"{tool_name} is not in this key's tool grant. It is in the "
            f"{where} group. This key may reach: {have}. Ask whoever issued "
            "this key to include that group.")


def _check_owners(policy: Mapping[str, object], tool_name: str) -> None:
    """The owners axis, enforced as a GATE and not as a transform.

    A predicate can allow or refuse; it cannot pseudonymise, because
    pseudonymising rewrites the answer and this check never touches one. So
    `pseudonymous` refuses rather than serving real names, and the message says
    why. An unrecognised value refuses too: a value outside the three is a key
    file nobody can read the intent of, and the safe reading of "I asked for
    something about identity" is not "show everything".
    """
    value = policy.get("owners")
    if value is None or tool_name not in _IDENTITY_TOOLS:
        return
    if str(value).strip().casefold() == OWNERS_REAL:
        return
    reason = (
        "this release has no anonymiser on the network path, so a key that "
        "asked for pseudonymous author identity is refused rather than served "
        "real names"
        if str(value).strip().casefold() == OWNERS_PSEUDONYMOUS
        else "this key's owners axis does not permit author identity")
    tail = ("" if tool_name == "who_knows" else
            " ask routes questions to who_knows and calls it directly, so it "
            "cannot be separated from it here.")
    raise _denied(
        f"{tool_name} discloses author identity and {reason} "
        f"(owners={value}).{tail}")


def check_tool_grant(principal: Principal | None, tool_name: str,
                     policy: Mapping[str, object] | None) -> None:
    """Raise :class:`GrantDenied` unless ``principal`` may call ``tool_name``.

    ``policy`` is the key record's policy block, or ``None`` for "no record".
    Those two are kept apart because they point opposite ways: an empty block is
    a key with no scope, which grants everything, while a missing record is a key
    that was revoked or pruned between the gate admitting the request and this
    check, which grants nothing. Collapsing them into one ``{}`` would turn a
    revoked key into a fully-granted one for the length of one request.

    Returns ``None`` on allow and raises on deny. Never a boolean: there are
    three call sites and a discarded return value is invisible in review and
    green in every test.
    """
    if principal is None:
        # Unreachable through `guarded`, which refuses an unset principal with
        # IdentityUnset above the anchor, and through the two other call sites,
        # which check first. Kept as an assertion for a direct call: a check
        # that answers "allowed" for nobody is the one bug this module must not
        # be able to have.
        raise _denied(
            "This server could not identify the caller, so it cannot decide "
            "what the caller is granted.")
    if _is_shared_token(principal.key_id):
        # The shared token has no key record and therefore no policy. Full
        # grant: denying it would break every deployment that sets
        # CONTEXTLAKE_MCP_TOKEN the moment this release lands, and the operator
        # who set that token asked for one credential with no scope, not for a
        # key whose scope is unwritable.
        return
    if policy is None:
        raise _denied(
            "This key resolved to no record when its grant was read. A key "
            "revoked or pruned while a request was in flight lands here. "
            "Present the key again; if it keeps failing it is no longer live.")
    _check_tools(policy, tool_name)
    _check_owners(policy, tool_name)


class GrantCheck:
    """The value ``build_http_app`` derives into ``build_server(grant_source=)``.

    One object rather than a bare callable because two surfaces ask different
    questions of the same rule. ``check`` answers "may this call proceed" and
    raises; ``visible`` answers "which of these names may this key call" and
    returns a list, for the catalogue. Both read the same policy through the same
    keyring, so a tool a key can see is a tool it can call.
    """

    def __init__(self, keyring) -> None:
        missing = [name for name in KEYRING_METHODS
                   if not callable(getattr(keyring, name, None))]
        if missing:
            # Build time, not request time. A keyring missing this would
            # otherwise raise AttributeError inside the grant check and turn
            # every authenticated tool call into a crash, on the one path that
            # must refuse cleanly or not at all.
            raise TypeError(
                f"keyring is missing {', '.join(missing)}. The grant check "
                "calls policy_for(key_id), which returns the record's policy "
                "block, or None when this keyring holds no such record. See "
                "contextlake.kb.keyfile.Keyring.")
        self._keyring = keyring

    def _policy(self, principal: Principal) -> Mapping[str, object] | None:
        return self._keyring.policy_for(principal.key_id)

    def check(self, principal: Principal | None, tool_name: str) -> None:
        """Raise :class:`GrantDenied` unless the call may proceed."""
        policy = None
        if principal is not None and not _is_shared_token(principal.key_id):
            policy = self._policy(principal)
        check_tool_grant(principal, tool_name, policy)

    def visible(self, principal: Principal | None,
                names: Iterable[str]) -> list[str]:
        """The subset of ``names`` this principal may call.

        Used for ``tools/list``. An agent that sees a tool it may not call spends
        a turn and a refusal per tool discovering its grant, and one that sees
        `blast_radius` builds a plan around it and fails mid-plan rather than at
        the start. A catalogue is metadata about which tools exist, not content
        drawn from the graph, so filtering it is not the answer-rewriting this
        module refuses to do.

        The cost, stated because it is real: a filtered list makes a refusal
        invisible, so an operator asking why the agent will not use a tool sees a
        shorter list and no reason. `kb keys show` prints the grant, and a
        call-time refusal names the group that would fix it.
        """
        kept = []
        for name in names:
            try:
                self.check(principal, name)
            except _server().GrantDenied:
                continue
            kept.append(name)
        return kept


def make_grant_check(keyring) -> GrantCheck | None:
    """The derivation ``build_http_app`` runs. ``None`` when there is no keyring.

    A token-only server has no records and so no policies to read, and a
    :class:`GrantCheck` over nothing would refuse every call. ``None`` here means
    ``_enforcing`` is False on that server, which is the honest state: nothing
    was scoped, so nothing is enforced.
    """
    if keyring is None:
        return None
    return GrantCheck(keyring)


def enforced_axes(policy: Mapping[str, object] | None) -> list[str]:
    """The axes recorded on THIS key that this server enforces.

    Per key rather than a flat server-wide list, so it composes with the derived
    `policy_enforced` boolean: a key with only `tools` set reads
    `["tools"]`, and a key that also names `repos` reads `["tools"]` with the
    boolean False, which is the pair a dashboard needs to render a scope column
    that is right on every row.

    `burst` IS CONDITIONAL, and a flat membership test gets it wrong. A burst is
    the request bucket's capacity, and there is no request bucket without a
    rate, so a burst recorded beside no rate (or beside `none`) binds nothing.
    Listed as enforced it would make `policy_enforced` True for a key that
    limits nothing, which is the label lying in the direction this work exists
    to stop. `parse_burst` refuses the combination at create time, so this only
    fires for a hand-edited file.
    """
    policy = policy or {}
    axes = [axis for axis in ENFORCED_AXES if policy.get(axis) is not None]
    rate = str(policy.get("rate") or "").strip().casefold()
    if not rate or rate == "none":
        axes = [axis for axis in axes if axis != "burst"]
    return axes


def policy_is_enforced(policy: Mapping[str, object] | None) -> bool:
    """Whether every axis recorded on this key is one this server enforces.

    An EMPTY policy reads False, not vacuously True. The fact an operator needs
    from this field is "is anything limiting this key", and the answer for a key
    with no axes at all is no: it can call every tool. A vacuous True on the key
    the default `kb keys create alice` mints is the shape where an absent field
    reads as a pass.
    """
    recorded = list(policy or {})
    if not recorded:
        return False
    # Built on `enforced_axes` rather than on ENFORCED_AXES directly, so the two
    # cannot disagree about one record. Read against the tuple, a key carrying
    # `burst` and no rate would answer True here and `[]` there.
    return set(recorded) <= set(enforced_axes(policy))
