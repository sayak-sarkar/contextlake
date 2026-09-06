"""`contextlake kb keys` -- issue, inspect, retire and account for the MCP API keys.

Every refusal `kb serve` prints on the key-file states it will not start on names
`contextlake kb keys create <name>`. Until this module landed that command did not
exist, so a server that refused named a way out the operator could not take.

Three rules hold this file together, and each of them was a real defect somewhere
else first.

**The key is printed ONCE, on stderr, and never through ``log()``.** The reasoning
is already written down at ``kb/cmds/serve.py:17-33`` for the shared bearer token.
``logging_setup.py:222`` builds a ``_ConsoleHandler(sys.stdout)`` and adds it
unconditionally, so ``log()`` always writes to stdout, and ``logging_setup.py:229``
adds a ``RotatingFileHandler`` whenever ``--log-file`` is set, so ``log()`` also
writes a credential into a 5 MB file with three backups that outlives the process.
``observability.redact`` rewrites workspace paths and repo names only, so it would
scrub neither. The key therefore goes through ``print(..., file=sys.stderr)``.

**No verb opens the store DATABASE.** ``_open_store`` (``kb/cmds/_common.py:104``)
constructs a ``SqliteStore``, runs ``check_schema`` and registers the store for
observability. A machine that has never run ``kb index`` has none of that, and
``kb keys list`` is the first command an operator runs. Nothing here imports
``_open_store``. ``LAST USED`` and the ``usage`` verb read the JSONL usage file
BY PATH, from ``kb_config(args).store_path``, which resolves a directory and
opens nothing.

**A write verb refuses on a permission fault; ``list`` warns and carries on.**
``keyfile.enforce`` raises, ``keyfile.permission_report`` reports. Blocking the
operator from seeing what exists is the wrong failure, and listing is the command
they run to diagnose the refusal they just hit.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
import sys
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from ... import style
from ...logging_setup import log, use_stderr
from .. import grants, keyfile
from .. import keys as keys_mod

# The verbs, and the two groups the permission rules split on. The parser in
# `cli.py` carries the same list as its `choices=`; a test pins the two together,
# because a verb that parses and never dispatches looks like nothing at all.
#
# WRITE verbs refuse on a permission fault. READ verbs warn and carry on.
WRITE_ACTIONS = frozenset({"create", "revoke", "rotate", "prune"})
READ_ACTIONS = frozenset({"list", "show", "check", "usage"})
ACTIONS = tuple(sorted(WRITE_ACTIONS | READ_ACTIONS))

# `--json` is registered once on the `keys` parser, because the verb is a
# positional, so argparse accepts it on all eight. All eight now answer it.
#
# 9.0.0 shipped two emitters and a refusal on the other five, because a verb
# that took the flag, printed prose and exited 0 gave a script no way to tell
# the result apart from success. The refusal was the honest interim state, not
# the destination: `JSON_ACTIONS` and the guard that read it are gone, and a
# test runs every verb with `--json` and parses its stdout.
#
# Two rules hold across all eight, and both are enforced in `cmd_keys` rather
# than in the handlers, because a rule each handler has to remember is a rule a
# ninth verb ships without:
#
# 1. `use_stderr()` runs ONCE, before dispatch. Every `log()` line then lands on
#    stderr, including the ones behind a `_BadUsage` raised inside a handler.
#    `_cmd_create` calls `use_stderr()` at its own `--print-key` check as well,
#    which covers `--print-key` WITHOUT `--json`.
# 2. stdout carries a document on EVERY exit path, success or failure. A caller
#    that asked for machine-readable output and got prose cannot act on it, and
#    that is as true of a failure as of a success.

# The five clients whose own current documentation was read on 2026-09-05 and
# shown to accept a custom header on a remote MCP server. Each source is quoted
# beside its block in `_client_block`. A client that is not on this list gets a
# refusal with a route, never a `Bearer` block in a file it will not read.
CLIENTS = ("claude-code", "cursor", "vscode", "windsurf", "zed")

# Named in `choices=` so argparse's bare "invalid choice" never lands here. Both
# are then refused with the reason and the route, because "invalid choice" says
# neither.
REFUSED_CLIENTS = ("claude-desktop", "claude-web")

_DATE_FORMAT = "%Y-%m-%d"

# PART OF THE POLICY IS ENFORCED NOW AND PART IS NOT, AND THE LABEL SAYS WHICH.
#
# `tools` and `owners` are read by `kb/grants.py` and checked on every call a
# networked server serves, and `rate`, `burst` and `cost_budget` are read by
# `kb/ratelimit.py` at the gate. `repos` and `external` are still stored and
# read by nothing.
#
# The label used to be one clause covering all six axes, and that is now a LIE
# IN BOTH DIRECTIONS. Blanket "recorded, not enforced" tells an operator their
# `--tools none` key can still call everything, so they revoke a key that was
# already safe; blanket "enforced" tells them `--rate 60/min` bounds a key that
# nothing rate-limits, so they hand it out. So the marker goes per axis, and
# which axes carry which marker is read from `grants.ENFORCED_AXES` rather than
# retyped here. Two lists in two modules drift, and this drift shows up as the
# CLI claiming an axis is live that the gate does not check.
#
# What the deferred axes cost, stated once so the note below can be short.
# `repos` bounds which repositories a key may NAME, not which an answer may come
# FROM, and it cannot be enforced correctly by a predicate: a node id does not
# carry its repo (`grants.py`'s module docstring has the measurement). `external`
# is a sentinel ruling on the repos axis, so it rides with it. Rate, burst and
# cost_budget went live with the rate limiter and are no longer in that list.
#
# One phrase, used two ways: in brackets beside a value, and as a clause in the
# note. `list` renders its policy in table columns with no room for a bracketed
# label, so the note is the only place the phrase can reach that surface, and a
# test asserts the phrase on all four verbs. Two spellings would let `list` pass
# the note assertion and fail the label one.
_NOT_ENFORCED = "recorded, not enforced"
_ENFORCED = "enforced"

# Every axis `--tools`..`--cost-budget` can write, in the order an operator
# meets them in `--help`. Derived against `grants.ENFORCED_AXES` rather than
# split by hand into two lists, so an axis that starts being enforced moves
# between the sentences by itself.
_ALL_AXES = ("tools", "repos", "owners", "external", "rate", "burst",
             "cost_budget")


def _unenforced_axes() -> tuple[str, ...]:
    return tuple(axis for axis in _ALL_AXES if axis not in grants.ENFORCED_AXES)


def _and_list(names) -> str:
    """``a, b and c``. Five axes joined by four "and"s read as one long word."""
    names = list(names)
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _enforcement_note() -> list[str]:
    """The three lines that go under every rendered policy, in every verb.

    Returned rather than logged so `create`, `show`, `check` and `list` all emit
    the same bytes. Four copies of a sentence drift, and the one that drifts is
    the one nobody re-reads.

    It is a statement about THE RELEASE, not about the key in front of it, so
    `list` can print one copy under a table of many keys. That is also why the
    unset clause is here: a bare `contextlake kb keys create alice` records no
    axis at all, and "a call outside them is refused" on its own would read as a
    restriction on a key that has none.

    The unenforced half names what those axes cost in plain terms. Naming them
    alone is not enough: an operator who reads "repos is recorded, not enforced"
    beside `repos=acme/**` still has to work out that the key reads every other
    repository, and that is the sentence they act on.
    """
    return [
        f"  This release enforces {_and_list(grants.ENFORCED_AXES)}: a value "
        "recorded there is checked on every",
        "  call, and an axis left unset records no scope and limits nothing.",
        # The phrase is embedded VERBATIM and mid-sentence rather than
        # sentence-cased. `list` renders its policy in table columns with no
        # room for a bracketed marker, so this note is the only place the phrase
        # reaches that surface, and a test asserts the one spelling on all four
        # verbs. A capitalised copy is a second spelling.
        f"  These are {_NOT_ENFORCED}: {', '.join(_unenforced_axes())}. So a "
        "key reads every indexed",
        "  repository, whatever those say.",
    ]


class _Failure(Exception):
    """A failure with a machine-readable code and the fields that describe it.

    The code is a REQUIRED first argument, never a defaulted one. There are ten
    raise sites across the eight verbs, and a default would let an eleventh ship
    `{"error": null}` with nothing failing.

    The sentence is not copied into the document as a `message` field. No house
    error document carries one: `use_stderr()` has already routed
    `log(style.fail(...))` to stderr, which is where the operator reads it.
    """

    def __init__(self, code: str, message: str, **fields):
        super().__init__(message)
        self.code = code
        self.fields = fields

    def __reduce__(self):
        """Rebuild from both arguments `__init__` needs, with `fields` restored.

        Python's default is `cls(*self.args)`, and `self.args` is the message
        alone, so the rebuild is one argument short and raises TypeError inside
        the unpickler. `kb keys` runs no process pool today, so nothing here
        crosses a boundary; the guard is blanket on purpose, because the class
        that skipped it is the one that broke a 656-repo run.

        The third element is the instance state, which is where `fields` comes
        back: keyword arguments cannot be passed positionally.
        """
        return (self.__class__, (self.code, self.args[0]), {"fields": self.fields})


class _BadUsage(_Failure):
    """A flag value this command cannot act on. Exit 2, the way argparse does.

    Raised rather than returned so the check sits where the value is read. The
    handler is the only place that turns it into an exit code, and it must not
    escape into `cli.py`'s top-level guard, which reports every exception as 1.
    """


class _NotFound(_Failure):
    """An id that is not in the key file. Exit 1: the request was well formed.

    Its own class rather than three inline emitters in `show`, `revoke` and
    `rotate`. Those three drift, and `cmd_keys` is the only place an exit code
    or an error document is made.
    """


# --------------------------------------------------------------------------
# Reading and writing the key file
# --------------------------------------------------------------------------


def _keys_path(args):
    """The key file this run reads. $CONTEXTLAKE_KEYS_FILE > [serve] keys_file > default.

    There is no `--keys-file` flag on `kb keys`: spec section 9 puts that flag on
    `kb serve` alone. A container names its mounted path with the environment
    variable, which `resolve_keys_file` reads first after the (absent) CLI tier.
    """
    return keyfile.resolve_keys_file(config_path=getattr(args, "config", None))


class _Loaded(NamedTuple):
    """What `_load` read, plus the warnings it printed on the way.

    `warnings` and `skipped` are returned as well as logged so a `--json` caller
    keeps them. They are `style.warn` lines on stderr, and under `--json` stderr
    is not the channel the caller is reading, so a document that dropped them
    would answer a question about the key file while hiding what is wrong with
    it. `kb keys list` is the command an operator runs after a server refuses to
    start, and a bad mask is often the reason it refused.

    Both are always empty for a WRITE verb: `_load(write=True)` raises on a
    fault rather than warning, so a write document has no branch that can carry
    one, and none of them declares the fields.
    """

    records: list
    doc: object
    warnings: tuple[str, ...]
    skipped: str | None


def _load(path, *, write: bool) -> _Loaded:
    """Records from the key file, with the permission rule for this verb applied.

    ``write`` picks the rule. A WRITE verb refuses on any fault. A READ verb
    warns, ONE LINE PER FAULT, and carries on: `keyfile.permission_report`
    reports every failing mask rather than the first, so an operator failing both
    the file mask and the parent mask fixes both from one run.

    The path's own STATE (a symlink, a directory, a file this account cannot stat
    or cannot read) is refused on every verb, read verbs included. It is not a
    permission question: an absent file is the one state that lets `kb serve`
    mint an unscoped shared token, and a file this account cannot examine is not
    an absent file.
    """
    state = keyfile.inspect_key_file(path)
    report = keyfile.permission_report(path, state=state)
    warnings: tuple[str, ...] = ()
    if write:
        if report.faults:
            raise keyfile.KeyFileError("\n".join(report.faults))
    else:
        # The state refusal is dropped from the warning set on purpose: it is not
        # a mask fault, and `load_document` below raises on it two lines later.
        # Warning about it first would print the same sentence twice and make
        # "one warning line per fault" count wrong.
        warnings = tuple(line for line in report.faults if line != state.refusal)
        for line in warnings:
            log(style.warn(line))
    if report.skipped:
        log(style.warn(report.skipped))
    doc = keyfile.load_document(path, state=state, check_permissions=False)
    records = [keys_mod.KeyRecord.from_dict(data) for data in doc.keys]
    return _Loaded(records, doc, warnings, report.skipped)


def _save(path, records) -> None:
    """Write the key file back. 0600 at creation, parent tightened to 0700.

    `keyfile.write_document` owns every byte of this: the temp sibling with the
    mode set at creation, the fsync before the rename, the atomic replace. Not
    `open(path, "w")`, which creates at the umask default and leaves a
    world-readable file holding key digests and revocation tombstones.
    """
    keyfile.write_document(path, records)


def _find(records, key_id: str):
    """The record with this id, or None. Exact match only.

    Never a prefix match. A prefix that matches two records has to pick one, and
    picking one on a revoke means the key the operator meant to kill is still
    live while the command reported success.
    """
    for record in records:
        if record.id == key_id:
            return record
    return None


def _unknown_id(key_id: str, path) -> str:
    return (f"no key with id {key_id} in {path}. Nothing was changed. "
            f"Run `contextlake kb keys list --all` to see every id, including "
            f"revoked and expired ones.")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _policy(args) -> dict:
    """The access-control and rate-limit block, stored as typed and VALIDATED.

    `--rate`, `--burst` and `--cost-budget` are refused at the flag now, the way
    `--tools` already is: a typo cannot be minted onto a key that then reads as
    limited. The value is still STORED as typed, so `60/min` comes back out of
    `kb keys show` spelled the way it went in.

    The same parser runs over every stored value when a keyring is loaded for
    serving, which is the half that matters: a garbage rate quietly replaced by
    a default is an unlimited key that reads as limited, and a key file can be
    hand-edited after creation.
    """
    policy = {}
    for flag in ("tools", "repos", "owners", "rate", "burst", "cost_budget"):
        value = getattr(args, flag, None)
        if value not in (None, ""):
            policy[flag] = value
    if getattr(args, "external", False):
        policy["external"] = True

    # `--tools ""` and `--repos ""` are REFUSED here rather than dropped above.
    # The drop is what the two lines above still do for every other axis, and it
    # makes an empty value indistinguishable from an unset one -- which is the
    # opposite instruction now that the tools axis is live. An unset axis grants
    # every tool; the operator who typed an empty string was narrowing.
    #
    # The refusal happens at the flag, not by storing `""`: `_scope_line` renders
    # `policy.get(axis) or 'unset'`, so a stored empty string would print `unset`
    # while meaning deny, which is the same collapse one layer down.
    for flag in ("tools", "repos"):
        if getattr(args, flag, None) == "":
            raise _BadUsage(
                f"empty_{flag}",
                f"--{flag} was given an empty value. Leave the flag off to "
                f"record no {flag} scope, or pass a value. An empty string "
                "cannot say which was meant.",
                value="")

    # Refused at CREATE, before anything is written. `--burst` with no `--rate`
    # lands here too: a burst is the request bucket's capacity and there is no
    # request bucket without a rate, so on its own it reads like a bound and
    # binds nothing.
    from .. import ratelimit

    # One try per axis, so the failure code names the axis the parser refused
    # rather than being recovered from the message text. Reading a class out of
    # a string is a substring match deciding a classification, and the axis is
    # already known here.
    def _parsed(axis: str, parse):
        try:
            return parse(policy.get(axis))
        except ValueError as exc:
            raise _BadUsage(f"bad_{axis}", str(exc),
                            value=str(policy.get(axis, ""))) from exc

    rate = _parsed("rate", ratelimit.parse_rate)
    # `--burst` is checked AGAINST the rate, so it runs second and the order is
    # load-bearing rather than cosmetic.
    _parsed("burst", lambda value: ratelimit.parse_burst(value, rate=rate))
    _parsed("cost_budget", ratelimit.parse_cost_budget)

    if "tools" in policy:
        # Refused at CREATE, so a typo cannot be minted onto a key that then
        # reads as scoped. This is deliberately NOT what the server does with an
        # unrecognised group in a hand-edited key file: there it is denied, not
        # refused, because refusing at the request would answer a call the
        # operator meant to narrow.
        try:
            grants.validate_tools(policy["tools"])
        except grants.GroupError as exc:
            raise _BadUsage("unknown_tool_group", str(exc),
                            value=policy["tools"]) from exc
    return policy


def _scope_line(record) -> str:
    """The three scope axes as stored, with the label that says they are inert.

    An UNSET axis reads `unset`, never `none` and never `default`. It used to
    read `tools=none  repos=none  owners=default`, so a bare
    `contextlake kb keys create alice` -- no flags, an empty policy dict --
    printed a line that reads as "no tools, no repositories" for a key that has
    every tool. `_row` rendered the same empty policy as `-` in the same
    release, which is two functions giving opposite readings of one record.

    `tools=none` still prints when the operator TYPED `--tools none`: that is
    their own word echoed back, and suppressing it would lose what they asked
    for. It is not enforced either, which is what the label is for.
    """
    policy = record.policy or {}
    parts = [_axis(policy, "tools"), _axis(policy, "repos"),
             _axis(policy, "owners")]
    if policy.get("external"):
        parts.append(f"external=on  ({_NOT_ENFORCED})")
    return "  ".join(parts)


def _axis(policy, axis: str) -> str:
    """One scope axis with the marker that belongs to THAT axis.

    The marker is per axis, not per line. One label after all three read as a
    claim about all three, so enforcing `tools` alone with the old line made it
    say `repos` and `owners` were live too.

    An UNSET axis carries no marker at all. It records no scope, so it makes no
    claim for a marker to qualify, and `tools=unset (recorded, not enforced)`
    would read as a restriction that is inert rather than as no restriction.
    """
    value = policy.get(axis)
    if not value:
        return f"{axis}=unset"
    if axis not in grants.ENFORCED_AXES:
        return f"{axis}={value}  ({_NOT_ENFORCED})"
    if axis == "owners" and str(value).strip().casefold() != grants.OWNERS_REAL:
        # The operator learns from `show` what they would otherwise learn from a
        # failed call. This release has no anonymiser on the network path, so a
        # key that asked for anything but real identity is REFUSED the identity
        # tools rather than served real names, and `(enforced)` on its own would
        # let an operator believe they are getting pseudonyms.
        return f"{axis}={value}  ({_ENFORCED}: who_knows and ask are refused)"
    return f"{axis}={value}  ({_ENFORCED})"


def _serve_defaults(args):
    """The parsed ``[serve]`` quota defaults, for rendering the effective value.

    TOML ONLY. `kb keys` opens no store database on any verb, and this keeps
    that: it reads the same privileged config files `[serve] keys_file` already
    comes from, through the same provenance gate, and touches nothing else.

    Warnings are dropped here, unlike at `kb serve`. An ignored local config is
    worth one line when a socket is about to open; it is noise on every `kb keys
    list`. An unparseable default falls back to "no default", which renders an
    inheriting key as unlimited -- the server refuses to start on that value, so
    the two surfaces disagree only about a configuration that cannot serve.
    """
    from .. import ratelimit

    try:
        table = keyfile.trusted_serve_table(getattr(args, "config", None),
                                            lambda line: None)
        return ratelimit.parse_serve_defaults(table)
    except (ValueError, OSError):
        return ratelimit.ServeDefaults()


def _usage_file(args) -> str:
    """The usage file this run reads.

    Two paths, two resolvers, and they must not be collapsed. The USAGE file
    sits beside the store, so it comes from ``kb_config(args).store_path``. The
    KEY file comes from ``_keys_path``, which walks
    ``$CONTEXTLAKE_KEYS_FILE`` then ``[serve] keys_file`` then the default.
    Resolving the key file from the store directory finds nothing, and the
    `usage` verb's exit codes then answer "no rows for this key" and "no such
    key" on the same branch.

    ``kb_config`` is imported here rather than at module scope: it lives beside
    ``_open_store``, and this module's second rule is that nothing here reaches
    for that.
    """
    from .. import usage as usage_mod
    from ._common import kb_config

    return usage_mod.usage_path(kb_config(args).store_path)


def _last_used(args):
    """The newest usage row per key. Never raises, and opens no database."""
    from .. import usage as usage_mod

    return usage_mod.LastUsed(_usage_file(args))


# What each axis inherits from, spelled the way it appears in kb.toml. Rendered
# into the line so an operator who reads "from [serve] default_rate" knows which
# key to edit, rather than being told a number with no address.
_LIMIT_CONFIG_KEYS = {"rate": "default_rate", "burst": "default_burst",
                      "cost_budget": "default_cost_budget"}


def _limits_line(record, defaults=None) -> str:
    """The three quota axes, each with WHERE ITS VALUE CAME FROM.

    The middle state is the one this function exists for. A key that names no
    rate can still be limited by ``[serve] default_rate``, and the old line
    printed a bare `unset` for it: an operator read that, believed the key was
    unlimited, and handed it out while the server was limiting it. Rendering the
    tier is what makes `unset` mean "unset AND unlimited" again.

    ``none`` is rendered as the operator's own word, not as `unset`. They typed
    a per-key opt-out and it beats the server default, which is the opposite
    instruction from having said nothing.
    """
    from .. import ratelimit

    policy = record.policy or {}
    defaults = defaults or ratelimit.ServeDefaults()
    limits = ratelimit.resolve_limits(policy, defaults)
    return " · ".join(_limit_axis(policy, axis, limits) for axis in _LIMIT_CONFIG_KEYS)


def _limit_axis(policy, axis: str, limits) -> str:
    """One quota axis, its effective value and its tier."""
    stored = policy.get(axis)
    typed = str(stored).strip() if stored not in (None, "") else ""
    effective = _effective_limit(limits, axis)
    if axis == "burst" and limits.rate is None:
        # A burst is the request bucket's capacity and there is no request
        # bucket without a rate. Whatever its source, it binds nothing here.
        shown = typed or "unset"
        return f"{axis}={shown}  (inert: no rate in force)"
    if typed.casefold() == "none":
        return f"{axis}=none  ({_ENFORCED}: no limit, set on the key)"
    if typed:
        return f"{axis}={typed}  ({_ENFORCED})"
    if effective is None:
        return f"{axis}=unset  (no limit)"
    if limits.sources.get(axis) == "config":
        return (f"{axis}=unset -> {effective} from "
                f"[serve] {_LIMIT_CONFIG_KEYS[axis]}  ({_ENFORCED})")
    # A rate is in force and nobody named a burst anywhere.
    return f"{axis}=unset -> {effective} built in  ({_ENFORCED})"


def _effective_block(record, defaults) -> dict:
    """The four resolved-quota fields, for a document that is not `_json_record`.

    `check` builds its own payload and renders a record that may be None, so it
    cannot reuse `_json_record`. Sharing this block is what keeps the two from
    drifting into two spellings of the same four fields.
    """
    from .. import ratelimit

    policy = record.policy if record else None
    limits = ratelimit.resolve_limits(policy, defaults)
    block = {f"effective_{axis}": _effective_limit(limits, axis)
             for axis in _LIMIT_CONFIG_KEYS}
    # With NO record there is nothing to inherit onto, so every tier reads
    # `unset` rather than `config`. Same reasoning as `policy_enforced` being
    # False and not null there: a caller must not read the server's default as
    # a fact about a key that does not exist.
    block["limits_source"] = (dict(limits.sources) if record else
                              {axis: "unset" for axis in _LIMIT_CONFIG_KEYS})
    if not record:
        block = {key: None if key.startswith("effective_") else value
                 for key, value in block.items()}
    return block


def _effective_limit(limits, axis: str) -> str | None:
    """One resolved axis as a STRING, or None for no limit.

    A string for all three, including ``burst``, so the four `--json` fields
    that carry them have one type between them: a consumer that renders a value
    beside its `limits_source` reads them the same way whichever axis it is on.
    """
    value = getattr(limits, axis)
    if value is None:
        return None
    # `burst` is an int; `rate` and `cost_budget` are `Rate` objects carrying
    # the operator's own spelling. Echoing `text` rather than re-rendering the
    # number is what lets them grep their key config for `60/min` and find it.
    return str(value) if axis == "burst" else value.text


def _expiry_date(record) -> str:
    """The expiry as a date, or the literal `never`. Never a raw timestamp."""
    if not record.expires_at:
        return keys_mod.NEVER
    return record.expires_at.split("T")[0]


def _row(record, now, defaults=None, last_used=None):
    policy = record.policy or {}
    # The EFFECTIVE rate, not the stored one. A key that names none can still be
    # limited by `[serve] default_rate`, and a `-` in this column for a limited
    # key is the same false reading `_limits_line` exists to stop, one surface
    # over. `_limits_line` under the table says which tier it came from.
    from .. import ratelimit

    limits = ratelimit.resolve_limits(policy, defaults or ratelimit.ServeDefaults())
    return {
        "id": record.id,
        "name": record.name,
        "state": record.state(now),
        "tools": str(policy.get("tools") or "-"),
        "repos": str(policy.get("repos") or "-"),
        "rate": _effective_limit(limits, "rate") or "-",
        "expires": _expiry_date(record),
        # THREE STATES, not two: `-` when no usage file exists (nothing
        # measures it), `never` when the file exists and holds no row for this
        # key, and a date when it does. One null for all three is what has an
        # operator revoke a key that is in daily use. `usage.LastUsed` owns the
        # rule; the note under the table says what `never` does not mean.
        "last_used": last_used.cell(record.id) if last_used else "-",
    }


_COLUMNS = (("id", "ID"), ("name", "NAME"), ("tools", "TOOLS"), ("repos", "REPOS"),
            ("rate", "RATE"), ("expires", "EXPIRES"), ("last_used", "LAST USED"))


def _table(rows) -> list[str]:
    widths = {field: len(title) for field, title in _COLUMNS}
    for row in rows:
        for field, _ in _COLUMNS:
            widths[field] = max(widths[field], len(row[field]))
    lines = ["  ".join(title.ljust(widths[field]) for field, title in _COLUMNS).rstrip()]
    for row in rows:
        lines.append("  ".join(row[field].ljust(widths[field])
                               for field, _ in _COLUMNS).rstrip())
    return lines


def _block(pairs) -> list[str]:
    """Label-left, value-left, one shared gutter. Spec section 9's sample shape.

    Deliberately NOT ``style.kv``, which aligns the value flush RIGHT of a shared
    column (`style.py:251-270`). That reads well for a status summary of short
    numbers, and badly here: `scope` is a 60-character line and `expires` is ten,
    so right-alignment pushes the dates out to the far margin and the block stops
    scanning as a block. The spec's own sample output is left-aligned.
    """
    width = max(len(label) for label, _ in pairs)
    return [f"  {label.ljust(width)}  {value}" for label, value in pairs]


def _file_note(path, doc) -> str:
    if not doc.present:
        return f"Key file: {path} (not created yet)"
    try:
        mode = stat_module.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return f"Key file: {path}"
    return f"Key file: {path} (mode {mode:04o})"


# --------------------------------------------------------------------------
# The JSON documents
# --------------------------------------------------------------------------

# `last_used_at` is null for two different reasons -- the key was never used,
# and nothing measured it -- so it travels with a sibling enum saying which.
# That is the defect `schedule/report.py:34-41` already fixed once, where
# `floor_activity_seconds` carried the same overloaded null. `usage.LastUsed`
# holds the three values; this is the fallback for a caller that looked the
# answer up nowhere.
_LAST_USED_STATE = "not-recorded"


def _emit_json(payload: dict, *, sort_keys: bool = False) -> None:
    """The document, on stdout, and nothing else.

    ``print``, never ``log()``: ``log()`` writes to stdout unconditionally AND
    into the ``--log-file`` copy whenever that flag is set, so a document routed
    through it would be duplicated into a rotating file.

    ``indent=2`` matches every other ``--json`` payload in this tree.
    ``sort_keys`` is True for ``show`` alone, because that is what 9.0.0
    shipped there and nowhere else.
    """
    print(json.dumps(payload, indent=2, sort_keys=sort_keys))


def _json_record(record, now, *, digest: bool = False, defaults=None,
                 last_used=None) -> dict:
    """One key as data. The same object in all seven documents.

    Every stamp is the ISO value as stored, never ``_expiry_date``'s truncated
    date and never ``_scope_line``'s rendering. Those two are DISPLAY strings,
    and they disagree with each other on an empty policy -- ``-`` in the table,
    ``unset`` in a block -- which is two readings of one record. A caller gets
    the record instead and picks neither.

    ``digest`` is opt-in, and only ``show`` asks for it, because that is where
    it shipped. Spreading a value the text surface deliberately withholds needs
    a named reader, and there is none.
    """
    payload = record.to_dict()
    if not digest:
        payload.pop("digest", None)
    payload["state"] = record.state(now)
    # The full minute stamp here, never the table's truncated date: the cell is
    # a display string and this is data.
    payload["last_used_at"] = last_used.at(record.id) if last_used else None
    payload["last_used_state"] = (last_used.state(record.id) if last_used
                                  else _LAST_USED_STATE)
    # PER RECORD, because `list` and `prune` render many and the answer differs
    # between them. `policy_enforced` at the top of a document says whether
    # everything that document shows is enforced; a caller rendering one row
    # needs the answer for that row, and a document-level boolean cannot give it.
    payload["enforced_axes"] = grants.enforced_axes(record.policy)
    payload["policy_enforced"] = grants.policy_is_enforced(record.policy)
    # THE RESOLVED QUOTA, beside the stored one rather than instead of it.
    # `policy` says what is written on the key; these say what the running
    # server will actually apply, which differ whenever a `[serve]` default is
    # set. A consumer that reads only `policy.rate` renders an inheriting key as
    # unlimited, which is the same misreading the text line carried.
    #
    # All three effective values are strings or null, and `limits_source` maps
    # each axis to `key`, `config` or `unset`. Three values, closed: a fourth
    # would have to be added here and to the renderer together.
    from .. import ratelimit

    limits = ratelimit.resolve_limits(record.policy, defaults or ratelimit.ServeDefaults())
    for axis in _LIMIT_CONFIG_KEYS:
        payload[f"effective_{axis}"] = _effective_limit(limits, axis)
    payload["limits_source"] = dict(limits.sources)
    return payload


def _enforced_flag(policies) -> bool:
    """The document-level ``policy_enforced``: is everything shown enforced?

    ``all()`` over an EMPTY set is True, and that answer is wrong here: a `list`
    with no keys, or a `prune` that removed none, would claim enforcement over
    nothing and a dashboard would render a green column for an empty table. An
    empty set means nothing shown is enforced, which is False.
    """
    policies = list(policies)
    return bool(policies) and all(grants.policy_is_enforced(p) for p in policies)


def _permission_block(loaded: _Loaded) -> dict:
    """What `_load` warned about, as data. READ verbs only.

    A write verb raises on a permission fault instead of warning, so it has no
    branch that could fill this and none of the four declares the fields. A
    field that is always the same value is a field a caller learns to skip.
    """
    return {
        "permission_ok": not loaded.warnings,
        "permission_warnings": list(loaded.warnings),
        "permission_checks_skipped": loaded.skipped,
    }


# --------------------------------------------------------------------------
# The --client blocks
# --------------------------------------------------------------------------
#
# Every block interpolates a VARIABLE. None of them inlines the key: a config
# file holding a live credential is the thing the display-once rule exists to
# avoid, and a block that pastes the key into it undoes the rule one copy later.
#
# Each of the five was read against that client's own current documentation on
# 2026-09-05, because a `Bearer` line in a file the client will not read it from
# is unsatisfiable, not merely unverified. The URLs are beside each block.
#
# [UNVERIFIED] Whether each client sends its configured header on EVERY request,
# including session establishment. No client was driven against a live server
# here. `anthropics/claude-code#29562` is a real bug in exactly that gap: headers
# stored, and not sent during session establishment.


_DEFAULT_URL = "http://127.0.0.1:8765/mcp"

# The three fields a setup script needs beside the rendered lines, one row per
# client, keyed the same way `_client_block` branches. A test pins the keys to
# `CLIENTS`, so a sixth client cannot ship with a block and no fields.
#
# The consumer is the one the ticket names: a script that writes
# `.vscode/mcp.json` itself. It needs the header NAME and the value to put in
# it, not a paragraph telling a person where to paste one.
#
# `zed` is the odd row and it is not a mistake: Zed reads the header literally,
# with no interpolation of any kind, so its template carries the placeholder its
# block prints and a script has to substitute the key itself.
_CLIENT_FIELDS = {
    "claude-code": ("Authorization", "Bearer ${CONTEXTLAKE_KEY}", ".mcp.json"),
    "cursor": ("Authorization", "Bearer ${env:CONTEXTLAKE_KEY}", "mcp.json"),
    "vscode": ("Authorization", "Bearer ${input:contextlake-key}",
               ".vscode/mcp.json"),
    "windsurf": ("Authorization", "Bearer ${env:CONTEXTLAKE_KEY}",
                 "mcp_config.json"),
    "zed": ("Authorization", "Bearer <the key above>", "settings.json"),
}


def _client_document(client: str, url) -> dict:
    """The `--client` block as fields, with the rendered lines beside them.

    `lines` is the list, unjoined, so a caller can write it into a file without
    splitting a string back apart. The `url` here is the RESOLVED one, so it
    matches what the lines print rather than what the operator typed.
    """
    resolved = url or _DEFAULT_URL
    header, template, config_path = _CLIENT_FIELDS[client]
    return {
        "client": client,
        "url": resolved,
        "header_name": header,
        "value_template": template,
        "config_path": config_path,
        "lines": _client_block(client, resolved),
    }


def _client_block(client: str, url: str) -> list[str]:
    url = url or _DEFAULT_URL
    if client == "claude-code":
        # code.claude.com/docs/en/mcp, read 2026-09-05: `claude mcp add
        # --transport http <name> <url> --header "Authorization: Bearer ..."`.
        # The CLI does NOT expand ${VAR} in a --header value; the SHELL expands
        # $CONTEXTLAKE_KEY inside the double quotes. The .mcp.json form below
        # does expand ${VAR}, which is the deviation from the ticket's wording.
        return [
            "Add it to Claude Code. The shell expands $CONTEXTLAKE_KEY, so the key",
            "never lands in the command you type:",
            "",
            "  export CONTEXTLAKE_KEY=<the key above>",
            "  claude mcp add --transport http contextlake \\",
            f"      {url} \\",
            '      --header "Authorization: Bearer $CONTEXTLAKE_KEY"',
            "",
            "In a checked-in .mcp.json the same header expands ${CONTEXTLAKE_KEY}:",
            "",
            '  { "mcpServers": { "contextlake": {',
            '      "type": "http",',
            f'      "url": "{url}",',
            '      "headers": { "Authorization": "Bearer ${CONTEXTLAKE_KEY}" } } } }',
        ]
    if client == "cursor":
        # cursor.com/docs/context/mcp, read 2026-09-05: url and headers, with
        # ${env:NAME} interpolation in both.
        return [
            "Add this to Cursor's mcp.json. ${env:} reads the value from the",
            "environment, so the key is not in the file:",
            "",
            '  { "mcpServers": { "contextlake": {',
            f'      "url": "{url}",',
            '      "headers": { "Authorization": "Bearer ${env:CONTEXTLAKE_KEY}" } } } }',
        ]
    if client == "vscode":
        # code.visualstudio.com/docs/agents/reference/mcp-configuration, read
        # 2026-09-05: a top-level "servers" key, "headers", and an "inputs" entry
        # with "type": "promptString" and "password": true, referenced as
        # ${input:<id>}.
        return [
            "Add this to .vscode/mcp.json. This is the best secret handling of the",
            "five: VS Code prompts once and stores the value outside the config",
            "file, so the key is never written into anything you can commit.",
            "",
            '  {',
            '    "inputs": [',
            '      { "type": "promptString", "id": "contextlake-key", "password": true }',
            '    ],',
            '    "servers": {',
            '      "contextlake": {',
            '        "type": "http",',
            f'        "url": "{url}",',
            '        "headers": { "Authorization": "Bearer ${input:contextlake-key}" }',
            '      }',
            '    }',
            '  }',
        ]
    if client == "windsurf":
        # docs.devin.ai/desktop/cascade/mcp (docs.windsurf.com redirects there),
        # read 2026-09-05: "serverUrl" plus a free-form "headers" object, with
        # ${env:NAME} shown in a header value.
        return [
            "Add this to Windsurf's mcp_config.json. ${env:} reads the value from",
            "the environment, so the key is not in the file:",
            "",
            '  { "mcpServers": { "contextlake": {',
            f'      "serverUrl": "{url}",',
            '      "headers": { "Authorization": "Bearer ${env:CONTEXTLAKE_KEY}" } } } }',
        ]
    # zed.dev/docs/ai/mcp, read 2026-09-05: a top-level "context_servers" key,
    # with "url" and "headers" on a remote server.
    return [
        "Add this to Zed's settings.json. Zed reads the header literally, so put",
        "the key in the file only if you can keep the file private:",
        "",
        '  { "context_servers": { "contextlake": {',
        f'      "url": "{url}",',
        '      "headers": { "Authorization": "Bearer <the key above>" } } } }',
    ]


def _refused_client(client: str) -> str:
    if client == "claude-web":
        return (
            "--client claude-web is refused. Claude on the web adds an MCP server "
            "through the Custom Connector UI, which is OAuth-only: there is no "
            "field to put a static Authorization header in, so a key issued for it "
            "could not be sent. Serve it behind an OAuth proxy, or use a client "
            "from: " + ", ".join(CLIENTS) + "."
        )
    return (
        "--client claude-desktop is refused. Whether Claude Desktop can send a "
        "static header is [UNVERIFIED]: PrefectHQ/fastmcp#1789 (open) says it "
        "cannot and anthropics/claude-ai-mcp#112 says it can, both read "
        "2026-09-02, and nothing here settled it. The route that does work is the "
        "mcp-remote stdio proxy, which holds the header and speaks stdio to the "
        "desktop app. Or use a client from: " + ", ".join(CLIENTS) + "."
    )


# --------------------------------------------------------------------------
# The verbs
# --------------------------------------------------------------------------


def _require_name(args, verb: str) -> str:
    name = getattr(args, "name", None)
    if not name:
        raise _BadUsage("missing_argument",
                        f"`kb keys {verb}` needs a name. "
                        f"Run: contextlake kb keys {verb} <name>",
                        usage=f"contextlake kb keys {verb} <name>")
    return name


def _print_key_wanted(args) -> bool:
    """Read `--print-key`, refuse a terminal, and clear stdout for the key.

    Shared by `create` and `rotate`. `--print-key` parses on both today, because
    it is declared once on the `keys` parser and the verb is a positional, so a
    second copy of this check in `rotate` is how the two drift apart.

    The `use_stderr()` here is what covers `--print-key` WITHOUT `--json`.
    `cmd_keys` hoists its own call for `--json`, and neither is redundant: one
    flag can be given without the other.
    """
    if not bool(getattr(args, "print_key", False)):
        return False
    if sys.stdout.isatty():
        raise _BadUsage(
            "print_key_on_tty",
            "--print-key refuses to write a key to a terminal: it exists so the "
            "key can be piped into a secret store, and on a terminal it lands in "
            "the scrollback instead. Pipe it (`... --print-key | pass insert -e "
            "contextlake`), or drop the flag and read it from stderr.")
    # stdout becomes the machine channel, so every human line moves to stderr.
    # The same convention `kb query --json` and `kb schedule --json` follow.
    use_stderr()
    return True


def _emit_key(key: str, *, print_key: bool) -> None:
    """The one place a plaintext key is written, and the whole rule in one call.

    ``--print-key`` sends the bare key to stdout for a pipe, and nothing else is
    on stdout because the caller routed ``log()`` to stderr first. Without it the
    key goes to stderr and stdout carries nothing. Never ``log()`` on either
    branch: it writes to stdout unconditionally and to the ``--log-file`` copy
    whenever that flag is set.
    """
    if print_key:
        print(key)
    else:
        print(f"  {key}", file=sys.stderr)


def _cmd_create(args) -> int:
    as_json = bool(getattr(args, "json", False))
    name = _require_name(args, "create")
    client = getattr(args, "client", None)
    if client in REFUSED_CLIENTS:
        raise _BadUsage("refused_client", _refused_client(client),
                        client=client, clients=list(CLIENTS))
    print_key = _print_key_wanted(args)

    path = _keys_path(args)
    loaded = _load(path, write=True)
    records = loaded.records

    out_file = getattr(args, "out", None)
    # The descriptor is opened BEFORE the record is minted, and this ordering is
    # the whole point of the call. `_save` used to run first, so
    # `create --out <existing path>` persisted a live record and THEN hit the
    # O_EXCL refusal: the record was in the file, its plaintext was gone
    # forever, and the command exited 2 saying nothing had worked.
    out_fd = _open_key_file(out_file) if out_file else None
    try:
        try:
            record, key = keys_mod.create(
                records, name, expires=getattr(args, "expires", None),
                policy=_policy(args), grant_version=_grant_version())
        except ValueError as exc:
            raise _BadUsage("bad_expires", str(exc),
                            value=getattr(args, "expires", None),
                            detail=str(exc)) from exc
        _save(path, records)
    except BaseException:
        # Nothing was minted, so the empty file this call created is a file the
        # operator never asked for, and leaving it makes the retry fail on
        # O_EXCL for a reason the first run caused.
        _discard_key_file(out_fd, out_file)
        raise
    if out_fd is not None:
        _write_key_fd(out_fd, key)

    now = datetime.now(timezone.utc)
    defaults = _serve_defaults(args)
    if as_json:
        payload = _json_record(record, now, defaults=defaults,
                               last_used=_last_used(args))
        payload.update(_key_channel(key, print_key=print_key, out_file=out_file))
        payload["changed"] = True
        payload["keys_file"] = str(path)
        payload["client"] = client or None
        payload["client_snippet"] = (
            _client_document(client, getattr(args, "url", None)) if client else None)
        if not print_key:
            # The key still has to reach the operator. Under `--print-key` it is
            # inside the document above and stdout is the only place it goes.
            _emit_key(key, print_key=False)
        _emit_json(payload)
        return 0

    log(f"Created key  {record.name}")
    for line in _block([("id", record.id),
                        ("scope", _scope_line(record)),
                        ("limits", _limits_line(record, defaults)),
                        ("expires", _expiry_date(record))]):
        log(line)
    log("")
    _emit_key(key, print_key=print_key)
    log("")
    log("  Shown once. It is stored as a SHA-256 digest and cannot be printed again.")
    log(f"  If it is lost, run: contextlake kb keys rotate {record.id}")
    # Creation is the moment the operator decides who to hand this to, so the
    # note lands here and not only on `show`. Someone who reads the scope block
    # above and never runs `show` is exactly the person who gives out a key
    # believing it is limited.
    log("")
    for line in _enforcement_note():
        log(line)
    if out_file:
        log(f"  Also written to {out_file} at mode 0600.")
    if client:
        log("")
        for line in _client_block(client, getattr(args, "url", None)):
            log(line)
    return 0


def _open_key_file(path):
    """Open the `--out` file at 0600, with the mode set AT CREATION.

    Not ``open(path, "w")``. Under the common 0022 umask that produces a
    world-readable 0644 file holding a live key, and every other display-once
    test in this story still passes while it does. The pattern is
    `keyfile.write_document`'s: ``os.open`` with the mode in the call,
    ``O_EXCL`` so an existing entry is refused rather than reused (a pre-existing
    file keeps its own mode through ``O_CREAT`` alone), and ``O_NOFOLLOW`` so a
    symlink at the path is refused rather than followed.

    Opening is SPLIT from writing so the refusal happens before a key exists.
    See the ordering comment in `_cmd_create`.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        raise _BadUsage(
            "out_file_exists",
            f"--out {path} could not be created: {exc}. It is created with "
            "O_EXCL, so an existing file at that path is refused rather than "
            "overwritten: overwriting one would destroy whatever is in it and "
            "could reuse its mode.", path=str(path), detail=str(exc)) from exc


def _write_key_fd(fd: int, key: str) -> None:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")


def _discard_key_file(fd, path) -> None:
    """Close and remove the file `_open_key_file` created. Never raises.

    Called when the key was never minted. Suppressing the errors is right here:
    this runs while another exception is on its way up, and a failure to tidy up
    must not replace the failure the operator has to read.
    """
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def _key_channel(key: str, *, print_key: bool, out_file) -> dict:
    """Where the plaintext went, as three fields. `create` and `rotate`.

    THE KEY IS NOT IN THE DOCUMENT BY DEFAULT, and the reason is
    `_open_key_file`'s. `contextlake kb keys create ci --json > provision.json`
    recreates the 0644 defect that helper does real work to avoid, at the
    caller's umask, silently, on the normal way to consume a `--json` surface. A
    redirect is a request for machine-readable output, not a decision about file
    modes.

    `--print-key` is the existing explicit "give me the secret on stdout"
    opt-in, and it already refuses a terminal. Under `--json` there is one thing
    on stdout, so the flag decides whether the key is INSIDE it rather than
    beside it, and the two flags stop competing for the same descriptor.

    `key_shown_on` is never "none": the key exists nowhere else afterwards, so
    it always goes somewhere.
    """
    return {
        "key": key if print_key else None,
        "key_shown_on": "stdout" if print_key else "stderr",
        "key_file": str(out_file) if out_file else None,
    }


def _grant_version() -> str:
    from ... import __version__

    return __version__


def _cmd_list(args) -> int:
    as_json = bool(getattr(args, "json", False))
    path = _keys_path(args)
    loaded = _load(path, write=False)
    records, doc = loaded.records, loaded.doc
    now = datetime.now(timezone.utc)
    defaults = _serve_defaults(args)
    # Read ONCE for the whole table, not once per row: the file is read by
    # path, and a read per key would open it as many times as there are keys.
    last_used = _last_used(args)
    rows = [_row(record, now, defaults, last_used) for record in records]
    live = [r for r in rows if r["state"] == keys_mod.LIVE]
    revoked = [r for r in rows if r["state"] == keys_mod.REVOKED]
    expired = [r for r in rows if r["state"] == keys_mod.EXPIRED]
    show_all = bool(getattr(args, "all", False))
    shown = rows if show_all else live

    if as_json:
        # `policy_enforced` is the JSON half of the label. A script reading
        # `{"tools": "none"}` out of a row has no other signal that the value
        # binds nothing, and a dashboard built on it would render a scope column
        # that is wrong on every row.
        #
        # Every row carries the RECORD as well as `_row`'s display strings. The
        # five display keys shipped in 9.0.0 and a caller reads them today, so
        # they stay: `tools`/`repos`/`rate` as `-` when unset, `expires` as a
        # date, `last_used` as `-`. They are frozen display strings, not data,
        # and they are the first thing to remove at the next major bump. Merging
        # the record UNDER them is safe because the three keys they share --
        # `id`, `name`, `state` -- hold identical values in both.
        by_id = {record.id: record for record in records}
        keys = []
        for row in shown:
            entry = _json_record(by_id[row["id"]], now, defaults=defaults,
                                 last_used=last_used)
            entry.update(row)
            keys.append(entry)
        payload = {"path": str(path), "keys_file": str(path),
                   # `doc.present`, never `path.exists()`. `_load` refuses a
                   # symlink, a directory and an unstattable path on every verb,
                   # while `exists()` follows a symlink and answers True for a
                   # directory, so a fresh check could report `present: true`
                   # beside an error saying the path is a directory.
                   "present": doc.present,
                   # The aggregate over the rows this document renders. Each row
                   # carries its own `policy_enforced` and `enforced_axes`,
                   # which is the answer a caller rendering one row needs.
                   "policy_enforced": _enforced_flag(
                       by_id[row["id"]].policy for row in shown),
                   # Echoes `--all`, so a caller can tell a filtered list from a
                   # complete one. Without it, `live: 2` beside two rows and
                   # `live: 2` beside five rows read the same.
                   "all": show_all}
        payload.update(_permission_block(loaded))
        payload.update({"live": len(live), "revoked": len(revoked),
                        "expired": len(expired), "keys": keys})
        _emit_json(payload)
        return 0

    for line in _table(shown):
        log(line)
    log("")
    hidden = f"{len(revoked)} revoked, {len(expired)} expired"
    tail = "" if show_all else " (--all to show)"
    log(f"{len(live)} live key(s). {hidden}{tail}.")
    log(_file_note(path, doc))
    # The TOOLS, REPOS and RATE columns carry no label of their own -- a table
    # cell has no room for one -- so the note carries it for all three.
    log("")
    for line in _enforcement_note():
        log(line)
    # LAST USED is a fourth unlabelled column with the same problem and a worse
    # failure mode: a wrong reading invites an operator to revoke a key that is
    # in daily use. Said here rather than in the cell, for the same reason.
    #
    # The `never` sentence is the load-bearing half. The usage file is capped,
    # so a key quiet for longer than the retained window reads `never` too, and
    # `never` alone would say "nobody has ever used this" about a key somebody
    # used last month.
    if not last_used.present:
        log("  LAST USED reads `-` on every key: no usage file at this store "
            "yet. A networked server writes one.")
    else:
        log("  LAST USED comes from the recorded usage. `never` means no row "
            "for that key in the file, which a key quiet longer than the "
            "retained window also reads as.")
    log(style.dim("  Per-key detail: contextlake kb keys usage"))
    return 0


def _cmd_show(args) -> int:
    as_json = bool(getattr(args, "json", False))
    key_id = _require_name(args, "show")
    path = _keys_path(args)
    loaded = _load(path, write=False)
    record = _find(loaded.records, key_id)
    if record is None:
        # Raised, not logged-and-returned. Before this the prose went to stdout
        # and `show --json k_nope` emitted NO document at all, because this
        # branch ran ahead of the `as_json` check.
        raise _NotFound("unknown_id", _unknown_id(key_id, path),
                        id=key_id, keys_file=str(path))
    now = datetime.now(timezone.utc)
    defaults = _serve_defaults(args)
    if as_json:
        payload = _json_record(record, now, digest=True, defaults=defaults,
                               last_used=_last_used(args))
        payload["keys_file"] = str(path)
        payload.update(_permission_block(loaded))
        _emit_json(payload, sort_keys=True)
        return 0
    log(f"{record.name}  ({record.id})")
    pairs = [("created", record.created_at.split("T")[0]),
             ("state", record.state(now)),
             ("expires", _expiry_date(record)),
             ("scope", _scope_line(record)),
             ("limits", _limits_line(record, defaults)),
             ("recorded by", f"contextlake {record.grant_version}"
                             if record.grant_version else "an unrecorded version")]
    if record.revoked_at:
        pairs.append(("revoked", record.revoked_at.split("T")[0]))
        pairs.append(("reason", record.revoked_reason or "not given"))
    if record.rotated_from:
        pairs.append(("rotated from", record.rotated_from))
    if record.rotated_to:
        pairs.append(("rotated to", record.rotated_to))
    for line in _block(pairs):
        log(line)
    # No repo-match count. A count of matching repositories needs the store
    # database, which no `kb keys` verb opens.
    #
    # This used to read "grant expanded at <version>. Tools added since are
    # denied; rotate to pick them up." Both halves were false. Nothing expands a
    # grant in this release (`_policy` stores the raw string the operator typed)
    # and nothing denies a tool. S4.3-acl-1 specifies that behaviour; it is
    # unbuilt, so the version is reported as the fact it is and the denial
    # sentence is gone.
    log("")
    for line in _enforcement_note():
        log(line)
    log("")
    log("  The key itself is not stored and cannot be shown.")
    return 0


def _cmd_revoke(args) -> int:
    as_json = bool(getattr(args, "json", False))
    key_id = _require_name(args, "revoke")
    path = _keys_path(args)
    records = _load(path, write=True).records
    record = _find(records, key_id)
    if record is None:
        # Exit 1, NOT the exit 0 no-op `kb source remove` documents
        # (`cli.py:1116-1117`, `source_cmd.py:256`). An admin scripting a
        # revocation reads the exit code, and "I revoked nothing" must never
        # read as success.
        raise _NotFound("unknown_id", _unknown_id(key_id, path),
                        id=key_id, keys_file=str(path))
    changed = keys_mod.revoke(records, record, reason=getattr(args, "reason", None))
    if changed:
        _save(path, records)
    if as_json:
        payload = _json_record(record, datetime.now(timezone.utc),
                               defaults=_serve_defaults(args),
                               last_used=_last_used(args))
        # THE FIELD THIS DOCUMENT EXISTS FOR. Both branches exit 0 and always
        # have, so "I revoked it" and "somebody else already had" are one exit
        # code, and the prose was the only thing that told them apart. On the
        # already-revoked branch `revoked_at` and `revoked_reason` above are the
        # ORIGINAL stamps: the first revocation is the audit answer.
        payload["changed"] = changed
        payload["keys_file"] = str(path)
        _emit_json(payload)
        return 0
    if not changed:
        log(f"{record.name} ({record.id}) was already revoked on "
            f"{(record.revoked_at or '').split('T')[0]}. Nothing changed.")
        return 0
    log(f"Revoked {record.name} ({record.id}). Effective on the next request; "
        "no restart needed.")
    log("Usage history is kept. Remove the record with: "
        "contextlake kb keys prune --before YYYY-MM-DD")
    return 0


def _cmd_rotate(args) -> int:
    as_json = bool(getattr(args, "json", False))
    key_id = _require_name(args, "rotate")
    # `--print-key` and `--out` are declared once on the `keys` parser, so both
    # already PARSED on rotate and both were ignored: a flag that parses, does
    # nothing and exits 0 is the same defect `--json` had here. Rotate is also
    # the verb where it bites hardest, because its new key exists nowhere else
    # and there was no machine route to it at all.
    print_key = _print_key_wanted(args)
    path = _keys_path(args)
    records = _load(path, write=True).records
    record = _find(records, key_id)
    if record is None:
        raise _NotFound("unknown_id", _unknown_id(key_id, path),
                        id=key_id, keys_file=str(path))
    # Resolved here, not read back from `args`: the CLI default is absent and
    # `keys_mod.DEFAULT_OVERLAP` fills it in, so what the operator typed and
    # what was applied differ on the default path.
    overlap = getattr(args, "overlap", None) or keys_mod.DEFAULT_OVERLAP
    # Parsed here as well as inside `keys_mod.rotate`, which reads `--overlap`
    # first and `--expires` second and raises the same ValueError for both. One
    # `except` around the call cannot say which flag was wrong, so a bad
    # `--expires` would come back coded `bad_overlap` and send the operator to
    # the flag they typed correctly. The re-parse costs one regex.
    try:
        keys_mod.parse_duration(overlap)
    except ValueError as exc:
        raise _BadUsage("bad_overlap", str(exc), value=overlap,
                        detail=str(exc)) from exc
    out_file = getattr(args, "out", None)
    out_fd = _open_key_file(out_file) if out_file else None
    try:
        try:
            new_record, key = keys_mod.rotate(
                records, record, overlap=overlap,
                expires=getattr(args, "expires", None))
        except ValueError as exc:
            # `--overlap` was parsed above, so what is left is `--expires`.
            raise _BadUsage("bad_expires", str(exc),
                            value=getattr(args, "expires", None),
                            detail=str(exc)) from exc
        _save(path, records)
    except BaseException:
        _discard_key_file(out_fd, out_file)
        raise
    if out_fd is not None:
        _write_key_fd(out_fd, key)

    if as_json:
        now = datetime.now(timezone.utc)
        defaults = _serve_defaults(args)
        last_used = _last_used(args)
        payload = {"old": _json_record(record, now, defaults=defaults,
                                       last_used=last_used),
                   "new": _json_record(new_record, now, defaults=defaults,
                                       last_used=last_used),
                   "overlap": overlap,
                   # The house duration pair, `interval`/`interval_seconds` in
                   # `schedule/report.py:27-33`.
                   "overlap_seconds": keys_mod.parse_duration(overlap),
                   # `rotate` copies the policy verbatim (`keys.py:673`), so
                   # both records answer the same. The aggregate is taken over
                   # both anyway, so a rotate that stops copying cannot leave
                   # this describing only the old one.
                   "policy_enforced": _enforced_flag(
                       (record.policy, new_record.policy))}
        payload.update(_key_channel(key, print_key=print_key, out_file=out_file))
        payload["changed"] = True
        payload["keys_file"] = str(path)
        if not print_key:
            _emit_key(key, print_key=False)
        _emit_json(payload)
        return 0

    log(f"Rotated {record.name}.")
    for line in _block([("old", f"{record.id}  expires {_expiry_date(record)}"),
                        ("new", f"{new_record.id}  expires "
                                f"{_expiry_date(new_record)}")]):
        log(line)
    log("")
    _emit_key(key, print_key=print_key)
    log("")
    log(f"  Hand this to the holder before {_expiry_date(record)}. "
        "Both keys work until then.")
    if out_file:
        log(f"  Also written to {out_file} at mode 0600.")
    return 0


def _cmd_prune(args) -> int:
    as_json = bool(getattr(args, "json", False))
    before = getattr(args, "before", None)
    if not before:
        raise _BadUsage(
            "missing_argument",
            "`kb keys prune` needs --before YYYY-MM-DD. It deletes records "
            "permanently, so the cutoff is typed, never defaulted.",
            usage="contextlake kb keys prune --before YYYY-MM-DD")
    try:
        cutoff = datetime.strptime(str(before), _DATE_FORMAT).replace(
            tzinfo=timezone.utc)
    except ValueError as exc:
        raise _BadUsage("bad_before",
                        f"--before {before!r} is not a date: use YYYY-MM-DD.",
                        value=str(before)) from exc
    path = _keys_path(args)
    records = _load(path, write=True).records
    removed = keys_mod.prune(records, cutoff)
    if removed:
        _save(path, records)
    if as_json:
        now = datetime.now(timezone.utc)
        # `removed` is the count and `removed_keys` its list, the house pair
        # naming from `lint.py:226-231` (`stale`/`stale_repos`). The count is
        # what makes the always-zero exit code usable: a scheduled cleanup that
        # deleted nothing and one that deleted forty both exit 0.
        _emit_json({"before": str(before),
                    "removed": len(removed),
                    "removed_keys": [_json_record(r, now,
                                                  defaults=_serve_defaults(args),
                                                  last_used=_last_used(args))
                                     for r in removed],
                    "remaining": len(records),
                    "changed": bool(removed),
                    "policy_enforced": _enforced_flag(r.policy for r in removed),
                    "keys_file": str(path)})
        return 0
    log(f"Pruned {len(removed)} record(s) that stopped working before {before}.")
    for record in removed:
        log(f"  {record.id}  {record.name}")
    if not removed:
        log("A live key is never pruned, whatever its creation date says.")
    return 0


def _cmd_check(args) -> int:
    as_json = bool(getattr(args, "json", False))
    if getattr(args, "name", None):
        # No `key` field on the error document, and no prefix of one. House
        # error documents echo the target (`owners.py:47-49` returns the repo
        # name back), and this is the deliberate exception: argv is refused
        # BECAUSE it lands in shell history and `ps`, so echoing it into the
        # caller's redirect is the same leak by another route.
        raise _BadUsage(
            "key_in_argv",
            "`kb keys check` reads the key from STDIN, never from the command "
            "line: a key in argv lands in shell history and shows in `ps` to "
            "every account on this machine. Run: "
            "printf '%s' \"$KEY\" | contextlake kb keys check")
    if sys.stdin.isatty():
        # `sys.stdin.read()` on a terminal blocks until EOF, and nothing here
        # prints a prompt first, so `contextlake kb keys check` typed on its own
        # hung with a blank screen until the operator found Ctrl-D. It reads as
        # a wedged command, not as a command waiting for input.
        #
        # Refuse rather than prompt. A prompt would echo the key into the
        # terminal scrollback, which is the same leak `--print-key` already
        # refuses a TTY over (`_cmd_create`), and this verb exists to be piped.
        raise _BadUsage(
            "stdin_is_tty",
            "`kb keys check` reads the key from STDIN and there is nothing "
            "piped in: on a terminal it would wait for end-of-file with no "
            "prompt. It does not prompt, because a typed key lands in the "
            "scrollback. Run: printf '%s' \"$KEY\" | contextlake kb keys check")
    presented = sys.stdin.read().strip()
    path = _keys_path(args)
    loaded = _load(path, write=False)
    records = loaded.records

    def _verdict(reason, record=None) -> dict:
        """The check document. `reason` separates what the exit code cannot.

        Malformed, unknown, revoked and expired are four distinct answers that
        all exit 1, and a CI gate that warns on `expired` and fails on `revoked`
        cannot act on the code alone. `reason` is the whole point of this
        surface.

        `checked_locally` is the machine form of `_checked_note`, and it is a
        SEPARATE claim from `policy_enforced`: one says this command asked
        nobody, the other says nothing enforces the policy however it is asked.
        Both print in text, so both are fields here.

        On `malformed` and `unknown` there is no record, so every record field
        is null and nothing echoes the presented key.
        """
        payload = {"valid": reason is None, "reason": reason,
                   "id": record.id if record else None,
                   "name": record.name if record else None,
                   "state": record.state(datetime.now(timezone.utc))
                            if record else None,
                   "expires_at": record.expires_at if record else None,
                   "policy": dict(record.policy or {}) if record else None,
                   "enforced_axes": (grants.enforced_axes(record.policy)
                                     if record else None),
                   # False with NO record, and not null: on `malformed` and
                   # `unknown` nothing is enforced because there is nothing to
                   # enforce, and a null here would make a caller testing
                   # `if not doc["policy_enforced"]` and one testing `is False`
                   # disagree about the same answer.
                   "policy_enforced": (grants.policy_is_enforced(record.policy)
                                       if record else False),
                   "checked_locally": True,
                   **_effective_block(record, _serve_defaults(args)),
                   "keys_file": str(path)}
        payload.update(_permission_block(loaded))
        return payload

    if not keys_mod.check_format(presented):
        if as_json:
            _emit_json(_verdict("malformed"))
            return 1
        log(style.fail("Not valid: malformed (bad prefix, length or checksum)."))
        log(_checked_note(path))
        return 1
    wanted = keys_mod.digest(presented)
    record = next((r for r in records if r.digest == wanted), None)
    if record is None:
        if as_json:
            _emit_json(_verdict("unknown"))
            return 1
        log(style.fail("Not valid: unknown. No record in the key file carries "
                       "this key's digest."))
        log(_checked_note(path))
        return 1
    state = record.state(datetime.now(timezone.utc))
    if state != keys_mod.LIVE:
        if as_json:
            _emit_json(_verdict(state, record))
            return 1
        log(style.fail(f"Not valid: {state}."))
        log(f"  {record.name} ({record.id})")
        log(_checked_note(path))
        return 1
    if as_json:
        _emit_json(_verdict(None, record))
        return 0
    log(style.ok(f"Valid   {record.name} ({record.id})"))
    log(f"  expires {_expiry_date(record)} · {_scope_line(record)}")
    log(f"  limits  {_limits_line(record, _serve_defaults(args))}")
    log(_checked_note(path))
    for line in _enforcement_note():
        log(line)
    return 0


def _checked_note(path) -> str:
    """What `check` actually did, and what it did NOT do.

    It does not say the scope was checked. There is no store here and no
    request was sent, so the scope lines above are the record's STORED policy
    rendered back, not an answer about what the server would allow.

    That is a different claim from `_enforcement_note`, and both lines print.
    This one says THIS COMMAND asked nobody. That one says NOTHING ENFORCES the
    policy, which stays true however the question is asked, including of a
    running server.
    """
    return (f"Checked locally against {path}. No request was made, and the scope "
            "above is what the record stores, not what a server answered.")


def _usage_readable(path) -> bool:
    """Whether the usage file can be opened at all.

    `usage.read_rows` swallows OSError and answers `[]`, which is the right
    answer for a file that does not exist and the WRONG report for one this
    account cannot read: "nothing was recorded" and "I could not read what was
    recorded" are two different things to tell an operator, and only one of
    them is their configuration working.
    """
    try:
        with open(path, encoding="utf-8"):
            return True
    except OSError:
        return False


def _since_cutoff(args):
    """`--since` as an absolute time, or None.

    `schedule.recommend.parse_duration` is the house parser: `45s`, `30m`,
    `2h`, `7d`. A second one here would drift from it, and it already refuses
    zero and negatives.
    """
    raw = getattr(args, "since", None)
    if not raw:
        return None
    from ...schedule.recommend import parse_duration

    try:
        seconds = parse_duration(raw)
    except ValueError as exc:
        raise _BadUsage("bad_since", str(exc), value=raw, detail=str(exc)) from exc
    return datetime.now(timezone.utc) - timedelta(seconds=float(seconds))


def _num(value) -> str:
    return f"{value:,}"


def _ms(value) -> str:
    """A duration cell. `-` when nothing was measured, never `0ms`.

    `0ms` is a measurement, and printing it where there is none is the same
    false reading the RATE column already carries a note about.
    """
    return "-" if value is None else f"{value}ms"


def _grid(headers, rows, *, right=()) -> list[str]:
    """One aligned block. Numeric columns right, so the digits line up."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]))
        return "  ".join(out).rstrip()

    return [line(headers)] + [line(row) for row in rows]


def _cmd_usage(args) -> int:
    """What the server recorded, per key and per tool.

    IT OPENS NO DATABASE. Two files, both read by path: the usage JSONL beside
    the store, and the key file, which is what separates "this key has no
    recorded calls" (exit 0) from "there is no such key" (exit 1). Both produce
    zero rows, and only the key file can tell them apart.
    """
    from .. import usage as usage_mod

    as_json = bool(getattr(args, "json", False))
    key_id = getattr(args, "name", None) or None
    path = _usage_file(args)
    since = _since_cutoff(args)
    readable = _usage_readable(path)
    rows = usage_mod.read_rows(path)
    # Lines this reader could not score, which is the file's own answer to
    # "nothing was recorded" versus "I could not read what was recorded". A row
    # from a newer contextlake carrying an outcome this version does not know is
    # dropped rather than mis-filed (see `usage._valid`), so the drop has to be
    # said out loud or the totals under-report in silence.
    unread = max(0, usage_mod.count_lines(path) - len(rows))

    # THE KEY FILE IS READ ON EVERY RUN, not only when an id was given. An
    # unreadable key file exits 1 on every other verb, and a `usage` that
    # reported happily over one would be the verb where a broken keyring reads
    # as healthy. With an id it also separates two answers that both produce
    # zero rows: an id nobody issued is a failed request (exit 1), and an
    # issued id with no traffic is a true zero (exit 0).
    keys_file = _keys_path(args)
    loaded = _load(keys_file, write=False)
    if key_id is not None:
        if _find(loaded.records, key_id) is None:
            raise _NotFound("unknown_id", _unknown_id(key_id, keys_file),
                            id=key_id, keys_file=str(keys_file))
        rows = [r for r in rows if r.get("key") == key_id]

    summary = usage_mod.summarize(rows, since=since)
    if as_json:
        _emit_json({"usage_file": path,
                    "present": os.path.exists(path),
                    "readable": readable,
                    "id": key_id,
                    "since": since.strftime(usage_mod.TS_FORMAT) if since else None,
                    "unread_lines": unread,
                    **summary})
        return 0

    if not readable and os.path.exists(path):
        log(style.warn(f"{path} could not be read, so this is what was already "
                       "recorded elsewhere, not the whole record."))
    if unread:
        log(style.warn(f"{unread} line(s) in {path} were skipped: truncated, or "
                       "written by a newer contextlake. Every number below is "
                       "short by whatever they held."))
    if not summary["calls"] and not summary["refused_total"]:
        if key_id is not None:
            log(f"{key_id} has no recorded calls.")
        elif not os.path.exists(path):
            log("Nothing recorded yet. Usage is recorded by a networked server "
                "(--transport http or sse), not on stdio.")
            log(style.dim(f"  It would be written to {path}"))
        else:
            log(f"No rows in {path} for this window.")
        return 0

    where = f" since {since.strftime(usage_mod.TS_FORMAT)}" if since else ""
    # The measured count is printed beside the call count, not folded into it.
    # A full buffer counts the call and drops its duration, so the percentiles
    # below are over a smaller population, and an operator reading
    # "40,000 calls  P50 12ms" has no other way to see that.
    log(f"Usage{where}: {_num(summary['calls'])} calls "
        f"({_num(summary['measured'])} timed)  {path}")
    log("")
    if summary["keys"]:
        rows_out = [(k["key"] or "-", _num(k["calls"]), _num(k["error"]),
                     _num(k["throttled"]), _num(k["denied"]),
                     _ms(k["p50"]), _ms(k["p95"]))
                    for k in summary["keys"]]
        for line in _grid(("KEY", "CALLS", "ERR", "THR", "DENY", "P50", "P95"),
                          rows_out, right=(1, 2, 3, 4, 5, 6)):
            log(line)
        log("")
    if summary["tools"]:
        for line in _grid(("TOOL", "CALLS"),
                          [(t["tool"], _num(t["calls"])) for t in summary["tools"]],
                          right=(1,)):
            log(line)
        log("")
        # `ask` routes to eight sibling tools by their bare names, below the
        # wrapper that writes these rows, so one `ask` over the wire is one row
        # named `ask`. Every tool it routes to is under-reported here by
        # however much `ask` sent it.
        log(style.dim("  One `ask` counts once, as `ask`. The eight tools it "
                      "routes to are under-counted by that much."))
        log("")
    if summary["refusals"]:
        log("Refused requests (never reached a tool)")
        for line in _grid(("", ""),
                          [(f"  {r['outcome']}", _num(r["n"]))
                           for r in summary["refusals"]]
                          + [("  total", _num(summary["refused_total"]))],
                          right=(1,))[1:]:
            log(line)
        if any(r["outcome"] == "identity_unset" for r in summary["refusals"]):
            # Not ordinary probing, and it reads like it in a list of refusal
            # classes. This server accepted a credential at the socket and then
            # lost it before the tool ran; every one of those calls answered
            # nothing.
            log(style.warn(
                "identity_unset is a fault in this server, not a bad key: it "
                "accepted a credential and then lost it before the tool ran. "
                "Every one of those calls answered nothing."))
    return 0


_DISPATCH = {
    "create": _cmd_create,
    "list": _cmd_list,
    "show": _cmd_show,
    "revoke": _cmd_revoke,
    "rotate": _cmd_rotate,
    "check": _cmd_check,
    "prune": _cmd_prune,
    "usage": _cmd_usage,
}


def cmd_keys(args) -> int:
    """Dispatch one `kb keys` verb and turn its failures into exit codes here.

    Every exit code is RETURNED, never raised. `cli.py:2387` wraps
    `kb_commands.dispatch` in a guard that reports any exception and exits 1, so
    a `ValueError` from a bad `--expires` escaping this function would exit 1
    where the spec's table says 2.
    """
    action = getattr(args, "action", None)
    as_json = bool(getattr(args, "json", False))
    if as_json:
        # ONCE, before dispatch, so no handler has to remember it and no
        # `_BadUsage` raised inside one can land on stdout. `_cmd_create` used
        # to call this itself AFTER its own two refusals, so
        # `create --json --client claude-desktop` would have put refusal prose
        # inside a caller's `> out.json`, with no test able to see it.
        use_stderr()
    handler = _DISPATCH.get(action)
    if handler is None:  # unreachable through argparse's choices=
        log(style.fail(f"unknown keys action: {action!r}"))
        if as_json:
            _emit_json({"error": "unknown_action", "action": action})
        return 2
    try:
        return handler(args)
    except _Failure as exc:
        log(style.fail(str(exc)))
        code = 2 if isinstance(exc, _BadUsage) else 1
        if as_json:
            _emit_json(_error_document(action, exc))
        return code
    except keyfile.KeyFileError as exc:
        log(style.fail(str(exc)))
        if as_json:
            _emit_json(_error_document(action, _Failure(
                "key_file_error", str(exc),
                keys_file=str(_keys_path(args)), detail=str(exc))))
        return 1


def _error_document(action, exc: _Failure) -> dict:
    """The error document, built in the one place an exit code is made.

    `check` gets two extra fields, and they are the reason this helper exists
    rather than a dict literal at each site. `check --json` answers with
    `valid: true|false`, so a caller reads `.valid`. An error there means the
    QUESTION WAS NOT ANSWERED, which is a third state, and `valid: false` would
    report a live key as bad because the key file could not be read. Null says
    unanswered.

    The documented idiom, and the one the docs give verbatim: test `.error`
    first, and only when it is absent test `.valid`.
    """
    payload = {"error": exc.code}
    payload.update(exc.fields)
    if action == "check":
        payload["valid"] = None
        payload["reason"] = None
    return payload
