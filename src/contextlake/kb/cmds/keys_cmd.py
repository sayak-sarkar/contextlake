"""`contextlake kb keys` -- create, list, show, revoke, rotate, check and prune API keys.

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
``_open_store``. The ``LAST USED`` column reads ``never`` in this phase; S4.5.4
fills it from the JSONL usage file, read BY PATH, which opens no database.

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
from datetime import datetime, timezone
from typing import NamedTuple

from ... import style
from ...logging_setup import log, use_stderr
from .. import keyfile
from .. import keys as keys_mod

# The verbs, and the two groups the permission rules split on. The parser in
# `cli.py` carries the same list as its `choices=`; a test pins the two together,
# because a verb that parses and never dispatches looks like nothing at all.
#
# WRITE verbs refuse on a permission fault. READ verbs warn and carry on.
WRITE_ACTIONS = frozenset({"create", "revoke", "rotate", "prune"})
READ_ACTIONS = frozenset({"list", "show", "check"})
ACTIONS = tuple(sorted(WRITE_ACTIONS | READ_ACTIONS))

# `--json` is registered once on the `keys` parser, because the verb is a
# positional, so argparse accepts it on all seven. All seven now answer it.
#
# 9.0.0 shipped two emitters and a refusal on the other five, because a verb
# that took the flag, printed prose and exited 0 gave a script no way to tell
# the result apart from success. The refusal was the honest interim state, not
# the destination: `JSON_ACTIONS` and the guard that read it are gone, and a
# test runs every verb with `--json` and parses its stdout.
#
# Two rules hold across all seven, and both are enforced in `cmd_keys` rather
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

# THE POLICY IS RECORDED AND ENFORCED BY NOTHING, AND EVERY SURFACE SAYS SO.
#
# `--tools`, `--repos` and `--owners` are stored on the record and rendered
# back. No code reads them. `build_http_app` sets `grant_source = None`
# (`kb/server.py:2939`) and the tool wrapper carries only the two ANCHOR
# comments where the check will go (`kb/server.py:1064` and `:1089`).
#
# Measured on 2026-09-05, not reasoned about: a key created with
# `--tools none --repos nothing-matches/*` was presented to a live
# `kb serve --transport http --keys-only` server, and `tools/list` answered
# with all 23 registered tools, after which `graph_stats` ran and returned a
# result. The full transcript is on the S4.2.5 round-5 ticket.
#
# So a surface that renders the policy without `_NOT_ENFORCED` tells an
# operator their key is scoped when it is not, and they hand it out on that
# reading. That is worse than not offering the flags, which is why the label is
# not optional and why a test walks every surface looking for it.
#
# The axes start working in two later stories: S4.3-acl-2 (tools), -4 (owners)
# and -5 (repos) turn on the scope axes, and S4.4-ratelimit-1..4 turn on rate,
# burst and cost_budget. The printed lines say "a later release" rather than
# naming those ids, because a ticket id means nothing at an operator's
# terminal.
# One phrase, used two ways: in brackets beside a value, and as a clause in the
# note. `list` renders its policy in table columns with no room for a bracketed
# label, so the note is the only place the phrase can reach that surface, and a
# test asserts the phrase on all four verbs. Two spellings would let `list` pass
# the note assertion and fail the label one.
_NOT_ENFORCED = "recorded, not enforced"


def _enforcement_note() -> list[str]:
    """The three lines that go under every rendered policy, in every verb.

    Returned rather than logged so `create`, `show`, `check` and `list` all emit
    the same bytes. Four copies of a sentence drift, and the one that drifts is
    the one nobody re-reads.
    """
    return [
        f"  Scope and limits are {_NOT_ENFORCED}. Nothing in "
        f"{_grant_version()} enforces them:",
        "  this key can call every tool on every indexed repository, whatever "
        "the scope says.",
        "  Enforcement ships in a later release.",
    ]


class _Failure(Exception):
    """A failure with a machine-readable code and the fields that describe it.

    The code is a REQUIRED first argument, never a defaulted one. There are ten
    raise sites across the seven verbs, and a default would let an eleventh ship
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
    """The access-control and rate-limit block, stored as typed.

    Nothing here validates `--rate` or `--cost-budget`. `parse_duration`
    (`schedule/recommend.py:38`) reads `90d` and `24h` and refuses `60/min` and
    `30s/min`, and their parser, `parse_rate`, is phase 2's (S4.4.1). So this
    phase stores the string and the help text says so. When `parse_rate` lands it
    has to run over every stored value at keyring load and FAIL the load on a
    value it refuses, the way an unparseable file already does. A garbage rate
    quietly replaced by a default is an unlimited key that reads as limited.
    """
    policy = {}
    for flag in ("tools", "repos", "owners", "rate", "burst", "cost_budget"):
        value = getattr(args, flag, None)
        if value not in (None, ""):
            policy[flag] = value
    if getattr(args, "external", False):
        policy["external"] = True
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
    parts = [f"tools={policy.get('tools') or 'unset'}",
             f"repos={policy.get('repos') or 'unset'}",
             f"owners={policy.get('owners') or 'unset'}"]
    if policy.get("external"):
        parts.append("external=on")
    return "  ".join(parts) + f"  ({_NOT_ENFORCED})"


def _limits_line(record) -> str:
    policy = record.policy or {}
    rate = policy.get("rate")
    burst = policy.get("burst")
    cost = policy.get("cost_budget")
    if not any((rate, burst, cost)):
        # NOT "unset (the server's defaults apply)". There is no rate limiter in
        # this release, so naming a default implies a limit regime that does not
        # exist, and an operator reading it would believe an unlimited key is
        # bounded by something.
        return "unset"
    parts = []
    if rate:
        parts.append(f"{rate}" + (f" (burst {burst})" if burst else ""))
    elif burst:
        parts.append(f"burst {burst}")
    if cost:
        parts.append(f"{cost} cost")
    return " · ".join(parts) + f"  ({_NOT_ENFORCED})"


def _expiry_date(record) -> str:
    """The expiry as a date, or the literal `never`. Never a raw timestamp."""
    if not record.expires_at:
        return keys_mod.NEVER
    return record.expires_at.split("T")[0]


def _row(record, now):
    policy = record.policy or {}
    return {
        "id": record.id,
        "name": record.name,
        "state": record.state(now),
        "tools": str(policy.get("tools") or "-"),
        "repos": str(policy.get("repos") or "-"),
        "rate": str(policy.get("rate") or "-"),
        "expires": _expiry_date(record),
        # NOT `never`. Nothing records a use in this release, so `never` would be
        # a claim about a key that may have been used seconds ago, and an operator
        # reading the column would revoke the wrong key. `-` matches how an unset
        # policy axis renders, and the note under the table says why.
        # S4.5.4 fills this from the JSONL usage file, read by path from
        # `kb_config(args).store_path`, which opens no database.
        "last_used": "-",
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

# Nothing records a use in this release, so `last_used_at` is null for a reason
# a caller cannot see: "never used" and "nothing measures it" are the same null.
# That is the defect `schedule/report.py:34-41` already fixed once, where
# `floor_activity_seconds` was null for two different reasons, and the fix there
# was a sibling enum rather than an overloaded null. This is that sibling.
# S4.5.4 fills the pair from the JSONL usage file, read BY PATH, and the value
# becomes `never` or `measured`.
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


def _json_record(record, now, *, digest: bool = False) -> dict:
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
    payload["last_used_at"] = None
    payload["last_used_state"] = _LAST_USED_STATE
    return payload


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
    if as_json:
        payload = _json_record(record, now)
        payload["policy_enforced"] = False
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
                        ("limits", _limits_line(record)),
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
    rows = [_row(record, now) for record in records]
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
            entry = _json_record(by_id[row["id"]], now)
            entry.update(row)
            keys.append(entry)
        payload = {"path": str(path), "keys_file": str(path),
                   # `doc.present`, never `path.exists()`. `_load` refuses a
                   # symlink, a directory and an unstattable path on every verb,
                   # while `exists()` follows a symlink and answers True for a
                   # directory, so a fresh check could report `present: true`
                   # beside an error saying the path is a directory.
                   "present": doc.present,
                   "policy_enforced": False,
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
    # failure mode: a stale-looking `-` invites an operator to revoke a key that
    # is in daily use. Said here rather than in the cell, for the same reason.
    log("  LAST USED is not recorded in this release, so every key reads `-`.")
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
    if as_json:
        payload = _json_record(record, now, digest=True)
        payload["policy_enforced"] = False
        payload["keys_file"] = str(path)
        payload.update(_permission_block(loaded))
        _emit_json(payload, sort_keys=True)
        return 0
    log(f"{record.name}  ({record.id})")
    pairs = [("created", record.created_at.split("T")[0]),
             ("state", record.state(now)),
             ("expires", _expiry_date(record)),
             ("scope", _scope_line(record)),
             ("limits", _limits_line(record)),
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
        payload = _json_record(record, datetime.now(timezone.utc))
        payload["policy_enforced"] = False
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
        payload = {"old": _json_record(record, now),
                   "new": _json_record(new_record, now),
                   "overlap": overlap,
                   # The house duration pair, `interval`/`interval_seconds` in
                   # `schedule/report.py:27-33`.
                   "overlap_seconds": keys_mod.parse_duration(overlap),
                   "policy_enforced": False}
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
                    "removed_keys": [_json_record(r, now) for r in removed],
                    "remaining": len(records),
                    "changed": bool(removed),
                    "policy_enforced": False,
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
                   "policy_enforced": False,
                   "checked_locally": True,
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
    log(f"  limits  {_limits_line(record)}")
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


_DISPATCH = {
    "create": _cmd_create,
    "list": _cmd_list,
    "show": _cmd_show,
    "revoke": _cmd_revoke,
    "rotate": _cmd_rotate,
    "check": _cmd_check,
    "prune": _cmd_prune,
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
