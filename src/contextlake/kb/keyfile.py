"""Where the MCP API keys live on disk, how they are written, and who may read them.

This module owns the FILE and the READ API over it. It owns no key format and no
record: :mod:`contextlake.kb.keys` owns ``check_format``, ``digest`` and
``KeyRecord`` with its ``state(now)``, and this module calls into it. It owns no
middleware either: it hands :class:`Keyring` to ``KeyAuthMiddleware``
(``kb/server.py``), which calls ``reload_if_changed()`` and then
``resolve(presented)`` once per request, and nothing else.

**THE PATH RULE, decided once and read once.** A key-file path names ONE
directory entry, and every check in this module runs against that one entry.

* The path must be a REGULAR FILE. A symlink AT the key-file path is refused,
  and so is a directory, a fifo, a socket or a device node.
* The entry is read by ONE ``os.lstat``, in :func:`inspect_key_file`. Every
  other check takes that :class:`KeyFileState` instead of statting again.
* The parent is ``path.parent``, statted once at load time and once at write
  time. Never per request.

Refusing a symlinked key file is what makes the two masks below describe the
same directory. ``os.stat`` follows a link and describes the TARGET, while
``path.parent`` is lexical and describes the LINK's own directory, so before
this rule a 0600 file could pass the file mask on one directory and the parent
mask on another, and a key file living somewhere else entirely satisfied both.
An ancestor that is a symlink is fine and is not refused: the lstat of the entry
and the stat of ``path.parent`` then resolve through the same ancestor and land
in the same real directory.

The bytes are read with ``os.open(..., O_NOFOLLOW)`` and the freshness triple
comes from ``os.fstat`` on THAT descriptor. A symlink swapped in between the
check and the open fails the open rather than being followed, and the records
and the stamp always describe one inode.

**The states of a key file, and what each one does.** There are not two. Every
row is decided in one place, and no row may silently become an open server:

* **no file** (``ENOENT`` from the lstat) -- an empty keyring, and the ONLY
  state in which ``cmd_serve`` may mint a shared token. It may mint on it only
  when NOBODY NAMED THE PATH: an absent file at a path given by ``--keys-file``,
  ``$CONTEXTLAKE_KEYS_FILE`` or ``[serve] keys_file`` is a fault and
  ``cmd_serve`` refuses it, exit 1. Naming a path says the keys live there, so
  finding nothing there is a broken deployment and not a first start. A
  container whose volume mount did not appear is the case that decided it: it
  starts with the named path empty, and it used to be indistinguishable on
  stderr from a first start on a fresh machine, minting one unscoped token for a
  deployment that had asked for scoped keys. :func:`resolve_keys_file_with_source`
  carries WHO named the path; this module still answers ENOENT with an empty
  keyring, and the mint-or-refuse ruling stays ``cmd_serve``'s.
* **a dangling symlink**, **a symlink to elsewhere** (``S_ISLNK``) -- REFUSE.
* **not a regular file** (``S_ISREG`` false) -- REFUSE.
* **cannot be stat-ed** (any other ``OSError``, ``PermissionError`` included) --
  REFUSE. ``Path.exists()`` swallows ``PermissionError`` and answers False, so
  it is not used anywhere in this module or on the serve path.
* **cannot be read** (``OSError`` from the open or the read) -- REFUSE.
* **bad permissions** (either mask, or the owner check) -- REFUSE.
* **corrupt, or a schema this reader does not know** -- REFUSE.
* **valid, every key REVOKED** -- ``cmd_serve`` mints. Revoking is a deliberate
  act by a person at the terminal, so the operator who revoked their last key is
  not locked out with everyone else.
* **valid, every key EXPIRED** -- ``cmd_serve`` REFUSES, exit 1. An expiry date
  arriving is nobody's decision, and minting here turned a scoped server into an
  open one with no operator action at all.
* **valid, NO key records at all** -- ``cmd_serve`` REFUSES, exit 1. For a
  DIFFERENT reason than the row above, and the reason written here used to be
  false. Nothing schedules ``kb keys prune``: it runs when a person types it,
  so the calendar does not reach this state on its own. The refusal stands
  anyway, because a file holding no record admits nobody and reads on stderr
  like a first start, so minting on it turns a deployment that asked for scoped
  keys into an open one. WHO emptied the file does not change that.
* **valid, with live keys** -- scoped auth, and no shared token is minted.

Those four rows are :meth:`Keyring.key_status`, and the mint-or-refuse ruling on
each is ``cmd_serve``'s. A fifth row answers a file whose records this reader
cannot account for as live, revoked or expired: :data:`STATUS_UNKNOWN`, REFUSE.
No input reaches it while ``KeyRecord.state`` answers three words, and it exists
because the row it used to fall to was ``all REVOKED``, the one that mints.

A pinned ``CONTEXTLAKE_MCP_TOKEN`` changes the two refusals into a start that
mints nothing: see ``cmd_serve``.

Every REFUSE state is :class:`KeyFileError`, which ``cmd_serve`` turns into
exit 1. None of them falls back to minting.

On the RELOAD path every REFUSE state keeps the LAST GOOD SNAPSHOT and warns
once, **and so does "no file"**: a key file deleted under a running server is a
failure, not an empty keyring. Only :meth:`Keyring.load`, at start, answers
"no file" with an empty keyring.

The things it is built to stop, each of which has a test named after it.

**The downgrade attack.** ``cmd_serve`` mints and prints a shared bearer token on
every network start (``kb/cmds/serve.py``). If a key file that is present but
corrupt fell back to that path, anyone who can truncate the file would turn a
scoped deployment into one unscoped token printed on stderr. So **absent means
an empty keyring and the caller may mint; every other state raises**. The two
never collapse, and "absent" means ENOENT and nothing else -- not a broken
symlink, not a file this account cannot stat, not a file it cannot read.

**A key file another account can write.** The file holds digests, not keys, so
reading it hands over nothing. Writing it is the asset: an account that can write
can clear ``revoked_at`` on a tombstone, or append a record whose digest it chose.
Hence **two masks and an owner check, not one mask**:

* ``st_mode & 0o077`` on the key file, so the digests and the tombstones stay
  unreadable by other accounts;
* ``st_mode & 0o022`` on its PARENT DIRECTORY, because unlink and create are
  governed by the directory's write bit and not by the file's mode. A 0600 file
  inside a 0777 directory passes a file-only check while any account that can
  write the parent replaces the file wholesale.

The parent mask is ``0o022`` and not ``0o077`` on purpose. ``~/.contextlake`` is
0755 on a working install, which carries no group or other WRITE bit, so 0o022
passes there while 0o077 would refuse on a machine with nothing wrong with it.
Different mask, different question. This never reads the parent's READ bits.

The OWNER check is the third question, and neither mask asks it. Mode bits say
which OTHER accounts may write; they say nothing about the account that owns the
entry, which may write it whatever the mode says and may chmod it back
afterwards. So the key file and its parent must be owned by the effective uid of
this process, or by root. Root is allowed because root can write every file on
the machine already, so refusing a root-owned ``/etc/contextlake`` would report a
risk that admitting it does not add.

**Which caller enforces the parent mask, and when there is no key file yet.**
An ABSENT key file runs NO permission check on the serve path: :meth:`Keyring.load`
returns an empty keyring, and ``cmd_serve`` mints and starts, exit 0 -- when
nobody named the path, which is the row above. That is the settled rule and it
wins over the mask, because a first start on a machine that has never run
``kb keys create`` must not depend on anything the keystore does with a file that
is not there. So the directory in that state is gated by the
WRITE side instead: :func:`write_document` calls :func:`enforce` BEFORE it creates
anything, so ``kb keys create`` into a 0777 directory refuses and no key file is
ever born there. Once a file does exist -- created here, or mounted in by a
container pointed at a path like ``/tmp`` with ``--keys-file`` -- the parent is
checked on every start AND on every reload that sees the file change, from the
one implementation in :func:`permission_report`.

What that pair does NOT cover, said plainly because the sentence here used to
claim it covered everything. The check is the key file's own PARENT and no
higher. A world-writable GRANDPARENT loads without complaint, and an account
that can write it can rename the parent away and put its own parent, holding its
own key file, at that name. The residual is small for one reason and it is worth
knowing which: a world-writable ANCESTOR in normal use is ``/tmp``, and ``/tmp``
carries the sticky bit, which is what stops one account renaming another's entry
inside it. It is not checked because the ancestor walk would cost one stat per
level on a path that is pinned at two, and because the parent's own 0700 mode
under this module's writer is what an attacker has to get past first. If your key
file's ancestors are world-writable WITHOUT the sticky bit, this module does not
see it. ``test_a_world_writable_grandparent_is_not_checked`` pins that boundary
so the next reader finds the edge rather than assuming coverage.

**A permission check that locks the operator out of looking.** :func:`enforce`
raises, and the serve path and every WRITE verb call it. ``kb keys list`` calls
:func:`permission_report` instead, which never raises and returns **one line per
failing mask**, so a fixture failing both reports two.

**A check that silently passes where it means nothing.** ``os.chmod`` on Windows
sets only the read-only flag, so the two masks and the owner check are POSIX
only. On any other platform :func:`permission_report` reports no MASK fault and
sets :data:`PERMISSION_CHECK_SKIPPED` on ``skipped``. A check that quietly
passes reads as protection it does not provide.

**Who reads that note.** :meth:`Keyring.load` copies it to
:attr:`Keyring.permission_note`, and ``cmd_serve`` prints it on stderr on every
network start that loaded a key file (``kb/cmds/serve.py``). Written down
because a note with no reader is the same silent pass it was added to stop: the
string existed for a full round with nothing printing it, while this docstring
asserted a caller that did not exist.

The STATE half of the path rule is NOT POSIX-gated. A dangling symlink is not a
mode-bit question, so symlink, irregular and unstattable are refused on every
platform, and only the masks and the owner check are skipped.

Live reload, and how stale a decision can be: :meth:`Keyring.reload_if_changed`
compares ``(st_ino, st_size, st_mtime_ns)`` against the snapshot taken at load and
re-reads only when the triple moved. When it has moved, the file runs the SAME
:func:`permission_report` the load path runs -- both masks, both owner checks and
the path state -- so a running server never adopts a file :meth:`Keyring.load`
would have refused. Called before every ``resolve`` on the auth path, worst-case
staleness is **zero requests for anything that crosses the gate**. Two things it
does not cover, and both belong in ``SECURITY.md``:

1. a request already inside a tool body when the revocation lands runs to
   completion, because revocation gates admission and not work in flight;
2. on the ``sse`` transport the long-lived ``GET /sse`` stream is authorised once,
   at connect, so a stream opened before a revocation stays open until the client
   disconnects. Only the ``POST /messages/`` half is re-gated.

**The rollback ratchet, and what it does NOT cover.** The schema gate refuses a
file written by a NEWER contextlake. It does not refuse an OLDER COPY of the
file at the same schema version, and restoring yesterday's copy un-revokes every
key revoked since. So a live :class:`Keyring` remembers every digest it has seen
revoked and refuses a reload that brings one of them back live, keeping the last
good snapshot. This is a PARTIAL control and the residual is stated here rather
than left to be discovered: it holds for the life of one process only. Across a
restart the reader takes whatever is on disk, and the controls against an
attacker putting an old copy there are the parent mask and the owner check.
:func:`test_a_rollback_cannot_revive_a_revoked_key` covers the live-process half
and nothing else.

What the lookup leaks, stated because "no linear scan" is half the reason for it:
:meth:`Keyring.resolve` performs one ``dict.get`` on a SHA-256 digest and never
iterates the keyring, so the work done is **the same for a known key, a revoked
key and an unknown one**, and the iteration count cannot leak the match position.
It does not leak which key ids or which prefixes exist. What it does not hide is
the difference between a value that fails ``check_format`` (no hash, no lookup)
and one that reaches the dict, which is a property of the format and not a secret.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import secrets
import stat as stat_module
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

# Bumped when the on-disk shape changes in a way an older reader would get wrong.
# A file whose `version` is ABOVE this, or that carries no `version` at all, is
# refused rather than read: an older contextlake loading a newer file as schema 1
# drops the fields it does not know, and if `revoked_at` is one of them a revoked
# key goes live again with nothing raised.
SCHEMA_VERSION = 1

DEFAULT_KEYS_FILE = "~/.contextlake/mcp-keys.json"
KEYS_FILE_ENV = "CONTEXTLAKE_KEYS_FILE"

# WHO named the key-file path. `resolve_keys_file_with_source` returns one of
# these beside the path, and `cmd_serve` reads it to tell two states apart that
# look identical on disk:
#
#   nobody named a path and there is no file -> a first start. Mint.
#   somebody named a path and there is no file -> a fault. Refuse, exit 1.
#
# Naming a path is a deliberate act that says "the keys live here". A container
# whose volume mount did not appear reaches the serve path with the named path
# empty, and without this split it starts OPEN with stderr that cannot be told
# apart from a first start on a fresh machine.
SOURCE_CLI = "--keys-file"
SOURCE_ENV = f"${KEYS_FILE_ENV}"
SOURCE_CONFIG = "[serve] keys_file"
SOURCE_DEFAULT = "default"

# The three tiers above the default. A path from any of them was named by a
# person, so an absent file there is a fault and not a first start.
NAMED_SOURCES = (SOURCE_CLI, SOURCE_ENV, SOURCE_CONFIG)

# Group or other bits on the key file: those accounts can read the digests and
# the tombstones.
FILE_MASK = 0o077
# Group or other WRITE on the parent directory: those accounts can unlink the
# key file and put their own in its place. Deliberately not 0o077 -- see the
# module docstring.
PARENT_MASK = 0o022

# The mode the writer creates with, and the mode it tightens the parent to.
FILE_MODE = 0o600
PARENT_MODE = 0o700

POSIX = os.name == "posix"

# The states a key-file PATH can be in. One lstat decides which, in
# `inspect_key_file`, and nothing else in this module classifies a path.
ABSENT = "absent"
SYMLINK = "symlink"
IRREGULAR = "irregular"
UNSTATTABLE = "unstattable"
REGULAR = "file"

# The states the CONTENT of a valid key file can be in. The path states above
# answer "may this file be read at all"; these answer "what is in it, and may
# the server mint a shared token because of that". `Keyring.key_status` decides,
# and only `cmd_serve` reads it.
STATUS_LIVE = "live"
STATUS_NO_KEYS = "no-keys"
STATUS_ALL_REVOKED = "all-revoked"
STATUS_ALL_EXPIRED = "all-expired"
# No record is live and the records that are not live cannot be accounted for as
# revoked or expired. `KeyRecord.state` answering a fourth word is the only way
# in, so nothing reaches it today; it exists because the alternative default was
# STATUS_ALL_REVOKED, the one zero-live state that MINTS. A credential path must
# not mint by falling off the end of a chain of ifs.
STATUS_UNKNOWN = "unknown-state"

PERMISSION_CHECK_SKIPPED = (
    "SKIPPED the key-file permission checks: this is not a POSIX system, where "
    "os.chmod sets only the read-only flag, so the file mode says nothing about "
    "which accounts can read the digests or replace the file."
)

# The symbols this module needs from `contextlake.kb.keys`, which another story
# owns. Checked once, when a Keyring is built, so a keys module missing one of
# them names the contract instead of raising AttributeError inside a request --
# which would turn every authenticated call into a 500 on the one path that has
# to fail as 401 or not at all. `KeyAuthMiddleware.KEYRING_METHODS`
# (kb/server.py) is the same check on the other side of this seam.
KEYS_MODULE_CONTRACT = ("check_format", "digest", "KeyRecord")


class KeyFileError(RuntimeError):
    """The key file is present and unusable. The caller turns this into exit 1.

    Never raised for an ABSENT file. That case returns an empty keyring, and it
    is the only case that lets ``cmd_serve`` mint a shared token.
    """


# --- the one file accessor, and its counters -------------------------------
#
# Every touch of the key file goes through `_lstat`, `_dir_stat` and
# `_read_file`, so a test counts calls rather than inspecting the directory
# afterwards -- which cannot tell "opened once" from "opened a hundred times",
# and cannot tell a stat on the key file from a stat on its parent.
#
# The counter keys stay "stat" and "read", and an lstat is counted as a "stat".
# The op name is a COST label, not a syscall name: a third key would break
# `tests/kb/test_kb_server.py`, which compares the whole dict.
#
# `_COUNTS` is two integer increments per touch and is diagnostic only: nothing
# in this module reads it, and it is not synchronised across threads, so a lost
# increment under concurrency changes no behaviour. `_TRACE` is None in
# production and bounded by that; a test sets it to a list to pin the PATH each
# call was made with, because a count alone passes on a stat of the wrong file.
_COUNTS = {"stat": 0, "read": 0}
_TRACE: list[tuple[str, str]] | None = None


def _record_touch(op: str, path) -> None:
    _COUNTS[op] += 1
    if _TRACE is not None:
        _TRACE.append((op, str(path)))


def _lstat(path) -> os.stat_result:
    """``os.lstat`` on the key-file path, counted.

    ``lstat`` and not ``stat``: this has to describe the ENTRY AT THE PATH. A
    ``stat`` follows a symlink and describes the target, which is how the file
    mask and the parent mask came to describe two different directories.
    """
    _record_touch("stat", path)
    return os.lstat(path)


def _dir_stat(path) -> os.stat_result | None:
    """``os.stat`` on the parent directory, counted, or None when it is not there.

    ``stat`` and not ``lstat``: an ancestor symlink is allowed, and what matters
    is the mode and the owner of the directory the entry actually lives in.
    """
    _record_touch("stat", path)
    try:
        return os.stat(path)
    except OSError:
        return None


def _read_file(path) -> tuple[bytes, os.stat_result]:
    """The key file's bytes and the ``fstat`` of the descriptor they came from.

    ``O_NOFOLLOW`` so a symlink swapped in between :func:`inspect_key_file` and
    this open fails the open (``ELOOP``) instead of being followed.

    The stat comes from ``os.fstat`` on THIS descriptor, not from a second
    lstat, so the freshness triple and the records always describe one inode.
    A separate stat can describe inode A while the bytes come from inode B, and
    the next ``reload_if_changed`` then matches the stale stamp and serves the
    stale table for the life of the process.
    """
    _record_touch("read", path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks), st


def reset_counters(trace: list | None = None) -> None:
    """Zero the accessor counters. For tests; nothing in the module calls it."""
    global _TRACE
    _COUNTS["stat"] = 0
    _COUNTS["read"] = 0
    _TRACE = trace


def counters() -> dict[str, int]:
    """A copy of the accessor counters."""
    return dict(_COUNTS)


# --- where the file lives --------------------------------------------------


def _expand(value) -> Path:
    from ..config import expand_path

    return Path(expand_path(str(value)))


def default_keys_file() -> Path:
    return _expand(DEFAULT_KEYS_FILE)


def _read_toml(path) -> dict:
    """Parse one TOML file, or return an empty table if it cannot be read.

    ``tomllib`` is 3.11+; ``kb/config.py`` carries the same ``tomli`` fallback for
    3.10, which is still in ``requires-python`` and still a CI cell.
    """
    try:  # Python 3.11+
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]

    try:
        with open(_expand(path), "rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        # A malformed or unreadable config is `load_kb_config`'s error to report,
        # not this function's: refusing to serve because an unrelated table in
        # kb.toml has a typo would be a worse failure than falling back to the
        # default key-file path.
        return {}


def _serve_keys_file(config_path: str | None, warn) -> str | None:
    """``[serve] keys_file`` from a config file the user NAMED, or None.

    Privileged provenance only: the global ``~/.contextlake/kb.toml`` or an
    explicit ``--config``. A ``.contextlake.kb.toml`` found by walking up from the
    cwd is designed to sit inside a repository checkout, so a key file it named
    would be committed, and a file found by directory search must never be able to
    mint an identity. When one tries, this returns None and says so on one line.

    Provenance is decided by ``trust.is_privileged_source``, which is the same
    gate ``[llm] command`` and ``[[sources]] token_env`` already go through.
    """
    from . import config as kb_config
    from . import trust

    local = kb_config.find_ancestor_config(kb_config.LOCAL_CONFIG)
    if local and not trust.is_privileged_source(local, config_path):
        if _read_toml(local).get("serve", {}).get("keys_file"):
            warn(
                f"IGNORED [serve] keys_file in {local}: that file was found by "
                "walking up from the current directory, not named by you, so it "
                "sits inside a repository checkout and anything it points at gets "
                "committed. Using the default key file instead. Pass --config "
                f"{local} or set the key in ~/.contextlake/kb.toml to have it "
                "honoured."
            )
    for candidate in (config_path, kb_config.GLOBAL_CONFIG):
        if not candidate or not trust.is_privileged_source(candidate, config_path):
            continue
        value = _read_toml(candidate).get("serve", {}).get("keys_file")
        if value:
            return str(value)
    return None


def resolve_keys_file_with_source(cli_path=None, *, env=None, config_path=None,
                                  warn=None) -> tuple[Path, str]:
    """The key file this run uses, and WHICH TIER named it.

    ``--keys-file`` > ``$CONTEXTLAKE_KEYS_FILE`` > ``[serve] keys_file`` from a
    privileged config > ``~/.contextlake/mcp-keys.json``. The second element is
    :data:`SOURCE_CLI`, :data:`SOURCE_ENV`, :data:`SOURCE_CONFIG` or
    :data:`SOURCE_DEFAULT`.

    The source is returned rather than inferred by the caller. Comparing the
    resolved path against :func:`default_keys_file` reads as "nobody named it"
    for an operator who NAMED the default path in their config, and that is the
    one reading that must not fall into the minting branch.

    ``--keys-file`` exists because a container needs to point at a mounted path.
    That is also why the parent-directory mask matters most here: a container
    pointed at something like ``/tmp`` gets a world-writable parent, and the serve
    path never writes, so the 0700 tightening in :func:`write_document` never runs
    to fix it. The serve path checks that parent only when the key file is
    PRESENT; the empty-directory case is refused by the write verb instead. See
    the module docstring.

    **A SET BUT BLANK ``$CONTEXTLAKE_KEYS_FILE`` RAISES.** It is not "unset". A
    shell that expanded an unset variable into the value, or a container passing
    ``--env CONTEXTLAKE_KEYS_FILE=`` from an empty template, produces it, and
    falling through to the default path then puts an EMPTY default in front of
    ``cmd_serve``, which mints an unscoped shared token for a deployment that
    asked for a key file. ``$CONTEXTLAKE_MCP_TOKEN`` already answers the same
    input in the fail-closed direction (``server.resolve_token``: blank is not
    honoured as a token, a fresh one is minted), and two env vars that read one
    shell accident in opposite directions is the asymmetry this closes. Raised
    here and not in ``cmd_serve`` because ``kb keys create`` must not silently
    write to the default path on that input either.
    """
    warn = warn or _default_warn
    env = os.environ if env is None else env
    if cli_path:
        return _expand(cli_path), SOURCE_CLI
    raw_env = env.get(KEYS_FILE_ENV)
    # Read RAW, then strip. `(value or "").strip()` collapses unset and
    # set-but-blank into one branch, and that collapse is the whole defect.
    if raw_env is not None:
        from_env = raw_env.strip()
        if not from_env:
            raise KeyFileError(
                f"${KEYS_FILE_ENV} is set and blank. Refusing to fall back to "
                f"{default_keys_file()}: setting the variable says the keys live "
                "somewhere specific, and a blank value is a shell that expanded "
                "nothing, not a decision to use the default. Point it at the key "
                "file, or unset it."
            )
        return _expand(from_env), SOURCE_ENV
    configured = _serve_keys_file(config_path, warn)
    if configured:
        return _expand(configured), SOURCE_CONFIG
    return default_keys_file(), SOURCE_DEFAULT


def resolve_keys_file(cli_path=None, *, env=None, config_path=None, warn=None) -> Path:
    """The key file this run uses. :func:`resolve_keys_file_with_source` without
    the source, for callers that do not decide anything on it.

    One implementation, not two: the tier order lives above and this drops the
    second element. A second copy of the four-tier walk is how the resolver and
    a caller's idea of the resolver drift.
    """
    return resolve_keys_file_with_source(cli_path, env=env, config_path=config_path,
                                         warn=warn)[0]


# --- the one path resolver -------------------------------------------------


@dataclass(frozen=True)
class KeyFileState:
    """One ``os.lstat`` of the key-file path, classified. Read once, used by all.

    ``mode``, ``uid`` and ``stamp`` are filled only for :data:`REGULAR`, because
    they are the only state in which they mean anything: the mode of a symlink
    is 0777 on Linux and says nothing about what it points at.
    """

    path: Path
    kind: str
    mode: int | None = None
    uid: int | None = None
    stamp: tuple[int, int, int] | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        """True only for a regular file. Absent is not usable and not a fault."""
        return self.kind == REGULAR

    @property
    def refusal(self) -> str | None:
        """Why this state is refused, or None for absent and for a regular file."""
        if self.kind == SYMLINK:
            return (
                f"the key file {self.path} is a symlink. Refusing to follow it: "
                "the file mode would then describe the link's TARGET while the "
                "directory check describes the link's own directory, so a key "
                "file in a directory any account can write would pass both. "
                "Replace the link with the real file, or point --keys-file at "
                "the real file."
            )
        if self.kind == IRREGULAR:
            return (
                f"the key file {self.path} is not a regular file. Refusing: only "
                "a regular file can carry a mode this reader can reason about."
            )
        if self.kind == UNSTATTABLE:
            return (
                f"the key file {self.path} exists and cannot be examined: "
                f"{self.detail}. Refusing rather than treating it as absent: an "
                "absent file lets this server mint one unscoped shared token in "
                "place of every scoped key, and a file this account cannot stat "
                "is not an absent file."
            )
        return None


def inspect_key_file(path) -> KeyFileState:
    """Classify the key-file path. ONE ``os.lstat``, and the only classifier.

    Never raises, and never touches the parent. Callers that need the parent's
    mode ask :func:`permission_report`, which is a load-time and write-time
    cost and never a per-request one.

    ``FileNotFoundError`` is the ONLY error that means absent. Every other
    ``OSError`` -- ``PermissionError`` and ``ELOOP`` included -- comes back as
    :data:`UNSTATTABLE`, because ``Path.exists()`` swallowing a
    ``PermissionError`` into False is the second door to the downgrade the
    module docstring names.
    """
    path = Path(path)
    try:
        st = _lstat(path)
    except FileNotFoundError:
        return KeyFileState(path=path, kind=ABSENT)
    except OSError as exc:
        return KeyFileState(path=path, kind=UNSTATTABLE, detail=str(exc))
    if stat_module.S_ISLNK(st.st_mode):
        return KeyFileState(path=path, kind=SYMLINK)
    if not stat_module.S_ISREG(st.st_mode):
        return KeyFileState(path=path, kind=IRREGULAR)
    return KeyFileState(
        path=path,
        kind=REGULAR,
        mode=st.st_mode & 0o777,
        uid=st.st_uid,
        stamp=(st.st_ino, st.st_size, st.st_mtime_ns),
    )


# --- permissions -----------------------------------------------------------


@dataclass(frozen=True)
class PermissionReport:
    """What the two masks found. ``faults`` is one line per FAILING mask.

    Both masks are reported, never the first one found: an implementation that
    stops at the first fault hides the second, and the operator fixes one thing,
    re-runs, and is refused again.
    """

    faults: tuple[str, ...] = ()
    skipped: str | None = None


def _file_fault(path, mode: int | None) -> str | None:
    """The line for a key file other accounts can read, or None. ``mode`` is
    None when the file is absent, which is not a fault."""
    if mode is None or not mode & FILE_MASK:
        return None
    return (
        f"key file {path} is {mode:04o}: other accounts on this machine can read "
        f"the key digests and the revocation tombstones. run: chmod 600 {path}"
    )


def _parent_fault(parent, mode: int | None) -> str | None:
    """The line for a directory other accounts can write, or None.

    This is the fault a file-only check misses. Unlink and create are governed by
    the directory's write bit, so a 0600 key file inside a 0777 directory is
    replaceable wholesale by anyone who can write that directory.
    """
    if mode is None or not mode & PARENT_MASK:
        return None
    return (
        f"the directory holding the key file, {parent}, is {mode:04o}: another "
        "account can write it, so it can delete the key file and put its own in "
        "place -- clearing a revocation, or adding a key whose digest it chose. "
        f"run: chmod 700 {parent}"
    )


def _owner_fault(what: str, path, uid: int | None) -> str | None:
    """The line for an entry another account owns, or None.

    Neither mask asks this. Mode bits say which OTHER accounts may write; the
    OWNER may write whatever the mode says, and may chmod it back afterwards, so
    a 0600 key file owned by another account is not protected by being 0600.

    Root passes, because root can write every file on this machine already.
    """
    if uid is None:
        return None
    mine = os.geteuid()
    if uid == mine or uid == 0:
        return None
    return (
        f"{what} {path} is owned by uid {uid}, and this process runs as uid "
        f"{mine}. That account can write it whatever its mode says, and can set "
        "the mode back afterwards. Take ownership of it, or point --keys-file at "
        "a file you own."
    )


def permission_report(path, *, state: KeyFileState | None = None) -> PermissionReport:
    """What is wrong with the key file's location. Never raises.

    One line per FAILING check, in a fixed order: the path's STATE first, then
    the file mask, the file's owner, the parent mask and the parent's owner.

    This is what ``kb keys list`` calls: blocking the admin from seeing what
    exists is the wrong failure, and listing is the command they run to diagnose
    a refusal.

    ``state`` is the caller's already-taken :class:`KeyFileState`, so a caller
    that has inspected the path does not stat it twice. Without it this takes
    two stats, one on the key file and one on its parent.

    The STATE checks run on every platform. Only the two masks and the two owner
    checks are POSIX, and off POSIX ``skipped`` carries
    :data:`PERMISSION_CHECK_SKIPPED` saying so.
    """
    state = inspect_key_file(path) if state is None else state
    faults = [line for line in (state.refusal,) if line]
    if not POSIX:
        return PermissionReport(faults=tuple(faults),
                                skipped=PERMISSION_CHECK_SKIPPED)
    parent = state.path.parent
    parent_st = _dir_stat(parent)
    parent_mode = None if parent_st is None else parent_st.st_mode & 0o777
    parent_uid = None if parent_st is None else parent_st.st_uid
    for line in (
        _file_fault(state.path, state.mode),
        _owner_fault("the key file", state.path, state.uid),
        _parent_fault(parent, parent_mode),
        _owner_fault("the directory holding the key file", parent, parent_uid),
    ):
        if line:
            faults.append(line)
    return PermissionReport(faults=tuple(faults))


def enforce(path, *, state: KeyFileState | None = None) -> None:
    """Refuse to go on when any check fails. Raises :class:`KeyFileError`.

    Called on the serve path and by every ``kb keys`` verb that WRITES. Not by
    ``kb keys list``, which uses :func:`permission_report` and warns instead.

    One implementation, one call. :meth:`Keyring.load` had its own copy of the
    mask logic for a round, and gutting :func:`permission_report` then left both
    load-path refusal tests green.
    """
    report = permission_report(path, state=state)
    if report.faults:
        raise KeyFileError("\n".join(report.faults))


# --- the document on disk --------------------------------------------------


@dataclass(frozen=True)
class KeyFileDocument:
    """The parsed key file. ``present`` is False only when the file is absent.

    ``stamp`` is the ``(st_ino, st_size, st_mtime_ns)`` of the descriptor the
    BYTES came from, so a caller storing it as its freshness marker cannot end
    up with a stamp for one inode and records from another.
    """

    present: bool
    version: int
    keys: tuple[dict, ...]
    stamp: tuple[int, int, int] | None = None


def load_document(path, *, state: KeyFileState | None = None,
                  check_permissions: bool = True) -> KeyFileDocument:
    """Read and validate the key file.

    Absent -> an empty document with ``present=False``. This is the ONLY state
    that lets ``cmd_serve`` mint a shared token, which is why nothing else may
    return an empty result. Absent is decided by :func:`inspect_key_file`, once,
    and means ``ENOENT`` from the lstat and nothing else.

    Every other unusable state -- a symlink, a directory, a file this account
    cannot stat, one it cannot read, one that will not parse, one missing
    ``version``, one carrying a ``version`` above :data:`SCHEMA_VERSION` --
    raises :class:`KeyFileError`. Never an empty keyring: that is the downgrade
    attack, where truncating the file demotes a scoped deployment to one
    unscoped token on stderr.

    A file that VANISHES between the inspect and the open raises rather than
    reading as absent. The race is a rotate landing mid-start; refusing makes
    the operator retry, and the other direction would let an attacker win the
    race into a minted shared token.

    ``check_permissions`` is False for the callers that have already checked:
    :meth:`Keyring.load` and :meth:`Keyring.reload_if_changed`, which pass their
    own ``state`` in. The state's own refusal is still enforced in that case --
    it is not a permission question, and skipping it would reopen the
    broken-symlink door.
    """
    state = inspect_key_file(path) if state is None else state
    path = state.path
    if check_permissions:
        enforce(path, state=state)
    elif state.refusal:
        raise KeyFileError(state.refusal)
    if state.kind == ABSENT:
        return KeyFileDocument(present=False, version=SCHEMA_VERSION, keys=())
    try:
        raw, st = _read_file(path)
    except FileNotFoundError as exc:
        raise KeyFileError(
            f"the key file {path} was there a moment ago and is gone now: {exc}. "
            "Refusing rather than reading it as absent: an absent key file is "
            "the one state that mints an unscoped shared token."
        ) from exc
    except OSError as exc:
        extra = ""
        if getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK):
            extra = (" A symlink was put at this path between the check and the "
                     "open; O_NOFOLLOW refused to follow it.")
        raise KeyFileError(f"cannot read the key file {path}: {exc}.{extra}") from exc
    stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise KeyFileError(
            f"the key file {path} is present but could not be parsed as JSON: {exc}. "
            "Refusing to start: an unreadable key file is not an empty one, and "
            "falling back would mint one unscoped shared token in place of every "
            "scoped key."
        ) from exc
    if not isinstance(doc, dict):
        raise KeyFileError(
            f"the key file {path} is present but is not a JSON object "
            f"(found {type(doc).__name__})."
        )
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise KeyFileError(
            f"the key file {path} carries no usable schema version. This reader "
            f"writes version {SCHEMA_VERSION}. Refusing to guess: a file read at "
            "the wrong schema silently drops the fields this reader does not know, "
            "and if revoked_at is one of them a revoked key goes live again."
        )
    if version > SCHEMA_VERSION:
        raise KeyFileError(
            f"the key file {path} is schema version {version}, and this "
            f"contextlake reads version {SCHEMA_VERSION}. Upgrade contextlake, or "
            "point --keys-file at a file this version wrote."
        )
    keys = doc.get("keys")
    if not isinstance(keys, list) or any(not isinstance(k, dict) for k in keys):
        raise KeyFileError(
            f"the key file {path} has no usable 'keys' array of records."
        )
    return KeyFileDocument(present=True, version=version, keys=tuple(keys),
                           stamp=stamp)


def write_document(path, records, *, version: int = SCHEMA_VERSION) -> None:
    """Write the key file atomically at 0600, with the parent tightened to 0700.

    Copies ``kb/config_edit.py``'s ``_write_document``: ``os.open`` a temp sibling
    with the mode set AT CREATION, write, ``os.replace``. The mode is never set
    with a ``chmod`` after the rename -- ``os.replace`` keeps the temp file's
    mode, so a later chmod leaves a window in which the file is world-readable.

    ``os.replace`` is an atomic rename on POSIX, so a crash mid-write leaves the
    previous file's bytes intact rather than a truncated one that still parses.

    Both masks are enforced BEFORE anything is created. The parent is tightened
    afterwards, as a write-time side effect, the way ``config._ensure_cache_dir``
    already does -- it is not a start-time gate, and it never runs on the serve
    path, which does not write.

    The two halves of "create the directory" are :func:`_make_parents` and
    :func:`_tighten_parent`, and each replaced a one-liner that was wrong in a
    way :func:`enforce` cannot see:

    * ``path.parent.mkdir(parents=True)`` gives the mode to the LAST level only
      and creates the ancestors at 0o777, so under a permissive umask this call
      CREATED world-writable directories above the key file. ``enforce`` ran
      before them and ``_dir_stat`` answers None for a directory that is not
      there, so the check is vacuous in the one call that makes them.
    * ``os.chmod(path.parent, PARENT_MODE)`` follows a symlinked parent and
      changes the mode of the TARGET, a directory the operator did not name
      here.

    ``sort_keys=True`` so two writes of the same content produce the same bytes
    and a diff of the file reads.

    Three things about the TEMP SIBLING, each of which was a hole:

    * its name carries 16 hex characters from :mod:`secrets`, so it cannot be
      pre-created. The fixed ``<name>.tmp`` could be: a pre-existing file kept
      its own mode through ``O_CREAT`` without ``O_EXCL`` (a reviewer's run
      landed the key file at 0666), and a pre-existing SYMLINK was followed,
      clobbering whatever it pointed at and leaving the key file a symlink.
    * ``O_EXCL`` refuses an existing entry outright, and ``O_NOFOLLOW`` refuses
      a symlink. The random name makes the collision unreachable; the flags mean
      it is refused rather than followed if it happens anyway.
    * the data is ``fsync``-ed BEFORE the rename. ``os.replace`` is atomic with
      respect to a crash in the rename, not with respect to the write: without
      the fsync a power loss can make the rename durable ahead of the bytes and
      leave a key file of zeros that parses as nothing. The parent directory is
      fsync-ed after, so the rename itself survives.

    A failed rename unlinks the temp file. Leaving randomly-named leftovers
    behind would put one 0600 file per failure next to the key file forever.
    """
    path = Path(path)
    enforce(path)
    _make_parents(path.parent)
    _tighten_parent(path.parent)
    rows = [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in records]
    payload = json.dumps(
        {"version": version, "keys": rows}, indent=2, sort_keys=True,
    ) + "\n"
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(tmp, flags, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _make_parents(directory) -> None:
    """Create the key file's directory and every missing ancestor AT 0700.

    ``Path.mkdir(parents=True)`` creates the ANCESTORS with the default mode
    0o777 and applies the mode it was given to the LAST level only. Under a
    permissive umask that left world-writable directories above the key file,
    inside the one call whose stated job is refusing a world-writable parent.

    :func:`enforce` cannot catch them. It ran before this, and :func:`_dir_stat`
    answers None for a directory that is not there yet, so :func:`_parent_fault`
    is vacuous in exactly the call that creates one. The mode has to be right by
    construction instead, and ``os.mkdir(level, PARENT_MODE)`` is enough for
    that: the umask can only take bits AWAY, so no level can come out wider than
    0700 whatever the umask is.

    ``os.stat`` and not ``Path.exists()``: ``exists()`` swallows a
    ``PermissionError`` into False, which here would mean trying to create a
    directory that is already there and reporting the wrong error for it.

    ``FileExistsError`` is passed over. Another process creating the same level
    between the stat and the mkdir is a race this does not need to win: the
    directory exists either way, and its mode is then that process's business.
    """
    directory = Path(directory)
    missing = []
    probe = directory
    while True:
        try:
            os.stat(probe)
            break
        except FileNotFoundError:
            missing.append(probe)
            if probe.parent == probe:
                break
            probe = probe.parent
        except OSError as exc:
            raise KeyFileError(
                f"cannot examine {probe} on the way to the key file: {exc}."
            ) from exc
    for level in reversed(missing):
        try:
            os.mkdir(level, PARENT_MODE)
        except FileExistsError:
            pass


def _tighten_parent(directory) -> None:
    """Set the key file's directory to 0700, through a descriptor.

    ``os.chmod(path.parent, PARENT_MODE)`` FOLLOWS a symlink, so a linked
    ``~/.contextlake`` had the mode of its TARGET changed: a directory the
    operator did not name here and that holds other things. ``O_NOFOLLOW`` also
    closes the window a path-based chmod leaves between the check and the call,
    where the entry can be swapped for a link to somewhere else.

    A SYMLINKED parent skips the tightening rather than raising. :func:`enforce`
    ran first and statted the parent through the link, so the directory the key
    file lands in is already proven to carry no group or other write bit and to
    be owned by this account. The tightening is a write-time side effect and not
    a gate -- refusing here would refuse an operator who deliberately symlinks a
    dotfile directory, and would buy nothing the check above has not already
    proved. Every other ``OSError`` is raised, so a real failure to tighten is
    not swallowed.

    TWO errnos mean "the parent is a symlink", not one. POSIX specifies
    ``ELOOP`` for ``O_NOFOLLOW`` on a link, but Linux answers ``ENOTDIR`` when
    ``O_DIRECTORY`` is in the flags as well, which it is here. ``ENOTDIR`` also
    means a parent that is not a directory, and skipping is right for that
    case too: the temp-file open a few lines later fails with the error the
    operator needs, rather than this one masking it.
    """
    if not POSIX:
        return
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(directory, flags)
    except OSError as exc:
        if getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK,
                                           errno.ENOTDIR):
            return
        raise
    try:
        os.fchmod(fd, PARENT_MODE)
    finally:
        os.close(fd)


def _fsync_dir(directory) -> None:
    """Make the rename itself durable. Best effort, and never fatal.

    Directory fsync is not available everywhere -- Windows has no directory
    descriptor to open -- and a key file that is written but whose rename is not
    yet flushed is a smaller problem than a write verb that raises. The data
    fsync above is the one that is not optional.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --- the read API the auth area calls --------------------------------------


def _default_warn(message: str) -> None:
    _log.warning("%s", message)


def _default_now() -> datetime:
    """The keyring's own WALL clock.

    Separate from the rate limiter's ``now=time.monotonic``, which is injected
    into ``build_http_app``. Monotonic has no epoch and no meaning across
    processes, so an expiry compared against it lapses at a different wall-clock
    instant on every launch and every restart resets it.
    """
    return datetime.now(timezone.utc)


def _keys_module():
    """``contextlake.kb.keys``, imported late.

    Deferred so importing this module touches nothing and costs nothing on the
    stdio path, and so the import lands on the day ``keys.py`` does rather than
    at the top of a module that already exists.
    """
    from . import keys

    return keys


def _checked_keys_module(module):
    module = module if module is not None else _keys_module()
    missing = [name for name in KEYS_MODULE_CONTRACT if not hasattr(module, name)]
    if missing:
        raise KeyFileError(
            f"the key module {getattr(module, '__name__', module)!r} is missing "
            f"{', '.join(missing)}. The keyring needs check_format(value) -> bool, "
            "digest(key) -> sha256 hex, and KeyRecord with .from_dict(data), "
            ".to_dict(), .digest and .state(now)."
        )
    return module


class Keyring:
    """The digest lookup over one key file, with live reload.

    One ``dict[sha256_hex] -> record``, built at load and rebuilt at reload, never
    written from a lookup. Its size is bounded by the number of keys in the file
    and never by traffic: nothing an unauthenticated caller sends can make a map
    in this class grow.

    The lookup NEVER iterates the keyring comparing entries. Two reasons, and the
    second is the one that gets forgotten: it is O(n) on the auth path, and a loop
    that stops at the match makes the iteration count leak the match position. The
    value being looked up is a SHA-256 digest, which is already non-invertible, so
    one ``dict.get`` is both the fast answer and the non-leaking one, and a
    constant-time table is not a thing that needs building.
    """

    def __init__(self, path, *, now=None, warn=None, keys_module=None) -> None:
        self.path = Path(path)
        self._now = now or _default_now
        self._warn = warn or _default_warn
        self._keys = _checked_keys_module(keys_module)
        self._by_digest: dict[str, object] = {}
        self._stamp: tuple[int, int, int] | None = None
        self._present = False
        self._failing = False
        self._ever_present = False
        self._permission_note: str | None = None
        # Every digest this keyring has ever seen revoked. Grows from FILE
        # CONTENT and never from traffic, so the "nothing an unauthenticated
        # caller sends can make a map in this class grow" sentence above still
        # holds: an unknown key is one dict.get and is never recorded anywhere.
        self._revoked_seen: set[str] = set()

    # -- construction --

    @classmethod
    def load(cls, path, *, now=None, warn=None, keys_module=None) -> Keyring:
        """Build a keyring from ``path``.

        Raises :class:`KeyFileError` for every state the module docstring's
        table marks REFUSE. An ABSENT file gives an empty keyring and no error,
        which is the state that lets the caller mint, so it is also the one that
        runs no permission check: there is no file to protect yet, and refusing
        here would refuse a first start.

        Two ``os.stat`` calls, one read. The first is the lstat that classifies
        the path AND supplies the file mode and the owner, so the file mask costs
        nothing extra. The second reads the parent's mode and owner. The
        permission logic is :func:`permission_report`'s, called with the state
        already in hand -- this method had its own copy for a round, and the two
        drifted the moment one of them was touched.
        """
        ring = cls(path, now=now, warn=warn, keys_module=keys_module)
        state = inspect_key_file(ring.path)
        if state.kind == ABSENT:
            return ring
        report = permission_report(ring.path, state=state)
        if report.faults:
            raise KeyFileError("\n".join(report.faults))
        ring._permission_note = report.skipped
        ring._adopt(load_document(ring.path, state=state, check_permissions=False))
        return ring

    def _adopt(self, doc: KeyFileDocument) -> None:
        """Install a parsed document, or raise and leave the old one in place.

        The table is built and the ratchet is checked BEFORE anything on ``self``
        moves, so a document that fails either leaves the last good snapshot
        exactly as it was.

        The stamp comes from the document, which took it from the ``fstat`` of
        the descriptor the bytes came from. Never from the earlier lstat: those
        can be two different inodes under this module's own rename-based writer,
        and a stamp that describes an inode the records did not come from is
        matched by the next reload and the stale table is then served forever.
        """
        table = self._build(doc)
        self._guard_revocations(table)
        self._by_digest = table
        self._present = doc.present
        self._stamp = doc.stamp
        self._ever_present = self._ever_present or doc.present
        now = self._now()
        for digest, record in table.items():
            if record.state(now) == "revoked":
                self._revoked_seen.add(digest)

    def _guard_revocations(self, table: dict[str, object]) -> None:
        """Refuse a document that brings a revoked key back live.

        The schema gate refuses a NEWER file. It cannot see an OLDER COPY of the
        file at the same version, and restoring yesterday's copy un-revokes every
        key revoked since.

        A digest that has DISAPPEARED from the file is not a revival: it can no
        longer authenticate. Only a digest that is still there and no longer
        revoked is.

        Partial by construction, and the residual is in the module docstring:
        this holds for the life of one process, and a restart reads whatever is
        on disk.
        """
        if not self._revoked_seen:
            return
        now = self._now()
        revived = sorted(
            digest for digest in self._revoked_seen
            if digest in table and table[digest].state(now) != "revoked"
        )
        if not revived:
            return
        raise KeyFileError(
            f"the key file {self.path} now presents {len(revived)} key(s) this "
            "keyring already saw REVOKED as live again. That is what restoring "
            "an older copy of the file looks like, and the schema version cannot "
            "see it because the version did not change. Refusing the reload and "
            "keeping the keys loaded before it."
        )

    def _build(self, doc: KeyFileDocument) -> dict[str, object]:
        table: dict[str, object] = {}
        for data in doc.keys:
            try:
                record = self._keys.KeyRecord.from_dict(data)
            except Exception as exc:  # any record error means a bad file
                raise KeyFileError(
                    f"the key file {self.path} holds a record this version cannot "
                    f"read: {exc}"
                ) from exc
            digest = getattr(record, "digest", None)
            if not digest:
                raise KeyFileError(
                    f"a record in the key file {self.path} carries no digest."
                )
            if digest in table:
                raise KeyFileError(
                    f"the key file {self.path} holds two records for one key "
                    "digest, so a lookup could not say which one applies. "
                    "Refusing rather than picking one: the two may disagree on "
                    "revocation."
                )
            table[digest] = record
        return table

    # -- freshness --

    def reload_if_changed(self) -> bool:
        """Re-read the key file when it moved. Returns True if the map was rebuilt.

        **THE RELOAD PATH IS THE LOAD PATH.** When the file has moved, this runs
        :func:`permission_report` with the state it already took, which is the
        same call :meth:`load` makes and the same one :func:`enforce` wraps. Not
        a second copy of the mask logic: this method open-coded the FILE_MASK
        check for a round, so it saw a widened file and NOT a changed owner and
        NOT a widened parent, and a running server adopted a key file that
        :meth:`load` would have refused, with no warning and no test.

        What it costs, and what is deliberately cheaper. An UNCHANGED file is one
        ``os.lstat`` and returns before any permission work: that is the
        per-request cost and it is unchanged. A CHANGED file is two stats, the
        lstat plus one on the parent, which is what the load path pays.

        The residual that buys, stated rather than left to be found, and it is
        BOTH halves of the permission check and not only the parent. Every check
        this method runs is behind ``state.stamp != self._stamp``, so anything
        that changes a MODE or an OWNER without changing the file's content is
        invisible to a running server:

        * the parent directory widened, and
        * the key file's OWN mode widened, or its owner changed.

        Measured rather than reasoned, on this machine (WSL2; ext4 under
        ``/home`` and tmpfs under ``/tmp``, both the same answer): ``os.chmod``
        on the key file moves no field of ``(st_ino, st_size, st_mtime_ns)``,
        and neither does ``os.chown``; a content write moves it every time.
        ``st_ctime_ns`` is not a way out: on both of those filesystems a chmod
        did not move that either, so widening the triple to a quad would have
        bought a note that reads as coverage and provides none.
        ``test_a_chmod_alone_is_invisible_until_the_content_changes`` pins both
        directions.

        One bound covers both halves, and it is the reason this stays a
        documented residual rather than a per-request parent stat: a widened
        mode is not a key. To USE either widening an attacker has to WRITE the
        key file, a write moves the triple, and a moved triple runs the FULL
        report -- both masks, both owner checks and the path state -- before
        anything is adopted. So what is not seen is a widening nobody has used
        yet, on the file or on its parent. What the file's own widening does buy
        without a write is READING the digests and the tombstones, which hands
        over no key: the file holds SHA-256 digests, and that is the reason the
        file mask exists at all rather than the reason it is urgent.

        Freshness is ``(st_ino, st_size, st_mtime_ns)``, not mtime alone.
        ``st_ino`` is load-bearing because of THIS MODULE'S OWN WRITER: every
        write goes temp-sibling-then-``os.replace``, which lands a NEW INODE on
        the path each time. Two writes inside one coarse mtime tick therefore
        differ in the inode even when the size and the mtime are identical, and
        an mtime-only check would serve a revoked key until the clock ticked.

        A failed reload keeps the LAST GOOD SNAPSHOT and warns once per failure
        transition. Never an empty keyring, which admits nobody; never a skipped
        check, which admits everybody; and never a fallback to minting a shared
        token, which is the downgrade the load path splits on.

        **A key file that is DELETED under a live keyring is a failure, not an
        empty keyring.** It went through the success path for a round: the
        failure branch never ran, every client was locked out, and the operator
        got no warning at all. Deleting the file to turn keys off is a restart,
        which is where "absent" is answered with an empty keyring and a mint.
        """
        state = inspect_key_file(self.path)
        if state.kind == REGULAR and state.stamp == self._stamp:
            return False
        try:
            if state.kind == ABSENT:
                if not self._ever_present:
                    # Born absent and still absent. Nothing changed and there is
                    # nothing to keep, so this is not a failure.
                    return False
                raise KeyFileError(
                    f"the key file {self.path} has been DELETED. An empty keyring "
                    "here would lock out every client with no signal, and reading "
                    "it as 'no key file' would re-open the mint path on a running "
                    "server. Restore the file, or restart to serve without one."
                )
            report = permission_report(self.path, state=state)
            if report.faults:
                raise KeyFileError("\n".join(report.faults))
            self._adopt(load_document(self.path, state=state,
                                      check_permissions=False))
        except (OSError, KeyFileError) as exc:
            self._fail(
                f"the key file {self.path} changed and could not be reloaded: "
                f"{exc}. Still serving the keys loaded before the change; no key "
                "was added and none was un-revoked."
            )
            return False
        self._failing = False
        return True

    def _fail(self, message: str) -> None:
        """Warn on the transition into a failing state, not on every request."""
        if not self._failing:
            self._failing = True
            self._warn(message)

    # -- lookup --

    def resolve(self, presented):
        """``(record, state)`` for a key this file holds, or None.

        ``state`` is ``record.state(now)`` -- ``live``, ``revoked`` or
        ``expired``. A revoked or an expired key comes back WITH its state rather
        than as None, so the caller records the refusal class without a second
        lookup. Liveness is decided by the record and never re-implemented here:
        splitting it would put the refusal classes in one module and their
        evaluation in another, and the two would drift.

        ``None`` means unknown or malformed, and nothing about the presented value
        is retained.

        Takes ``bytes`` or ``str``. The ASGI gate hands over the raw header bytes,
        and a hostile value carrying non-ASCII must come back as None so the wire
        sees a 401. Letting a ``UnicodeDecodeError`` escape would surface it as a
        500 instead, which is the same trap the shared-token comparison already
        carries a comment about.
        """
        if isinstance(presented, (bytes, bytearray, memoryview)):
            try:
                value = bytes(presented).decode("ascii")
            except UnicodeDecodeError:
                return None
        elif isinstance(presented, str):
            value = presented
        else:
            return None
        if not self._keys.check_format(value):
            return None
        record = self._by_digest.get(self._keys.digest(value))
        if record is None:
            return None
        return record, record.state(self._now())

    # -- what the caller asks about the file --

    @property
    def present(self) -> bool:
        """Whether the key file exists. False means the caller may mint.

        False only for ``ENOENT``. Every other unusable state raised on the way
        here, so a caller reading this cannot mistake a broken symlink or an
        unreadable file for "there is no key file".
        """
        return self._present

    @property
    def permission_note(self) -> str | None:
        """:data:`PERMISSION_CHECK_SKIPPED` when the masks did not run, else None.

        ``cmd_serve`` prints it. This exists because the note had no reader for a
        round: off POSIX every permission check passed in silence while the
        module docstring described an operator being told.
        """
        return self._permission_note

    def __len__(self) -> int:
        return len(self._by_digest)

    def _state_counts(self) -> dict[str, int]:
        """How many records are live, revoked and expired, counted afresh.

        Never cached on the instance. The clock moves, and a memoised count
        would still report "live" after the last key lapsed, which is the defect
        this classification exists to catch.

        The three names are literals, matching ``_adopt`` and
        ``_guard_revocations`` above. They are ``keys.LIVE`` / ``REVOKED`` /
        ``EXPIRED``, which are not in :data:`KEYS_MODULE_CONTRACT`, so reading
        them off ``self._keys`` would raise on a keys module that satisfies the
        checked contract.
        """
        counts = {"live": 0, "revoked": 0, "expired": 0}
        now = self._now()
        for record in self._by_digest.values():
            state = record.state(now)
            counts[state] = counts.get(state, 0) + 1
        return counts

    def live_count(self) -> int:
        """How many records are neither revoked nor expired.

        Not on the auth path, so the iteration here costs a request nothing.

        Zero does NOT mean "treat the file as absent". Which of the three
        zero-live states this is decides whether ``cmd_serve`` may mint a shared
        token, and :meth:`key_status` is that answer.
        """
        return self._state_counts()["live"]

    def key_status(self) -> str:
        """Which content state this key file is in.

        One of :data:`STATUS_LIVE`, :data:`STATUS_ALL_REVOKED`,
        :data:`STATUS_ALL_EXPIRED`, :data:`STATUS_NO_KEYS`, or
        :data:`STATUS_UNKNOWN`. ``cmd_serve`` reads it once per start. Nothing on
        the auth path calls it, so it costs a request nothing, same as
        :meth:`live_count`.

        **NOTHING FALLS THROUGH TO THE MINTING BRANCH.** ``all-revoked`` is the
        one zero-live state that lets ``cmd_serve`` mint, and it was the last
        ``return`` of the chain, so any record state this method cannot name
        arrived there by arithmetic and minted an unscoped shared token. It is
        returned now only when the revoked records ACCOUNT FOR THE WHOLE FILE,
        and everything else answers :data:`STATUS_UNKNOWN`, which ``cmd_serve``
        refuses. Unreachable while ``KeyRecord.state`` answers three words; the
        default a credential path falls to is worth being right before an input
        reaches it, not after.

        **Zero live keys is three states, and only one of them may mint.** A
        file whose keys were all REVOKED got there because a person ran
        ``kb keys revoke`` on the last one; no timer can produce it. A file
        whose keys all EXPIRED got there because a date passed with nobody at
        the terminal. Minting on the second discards every scoped key and prints
        one unscoped shared token, which is a server turning open on its own.

        **Expired beats revoked HERE, the opposite of one record's
        ``KeyRecord.state``.** A record answers "why did this key stop working",
        and revocation is the answer an audit needs. The file answers "may this
        server downgrade itself", and one lapsed key is enough to say no. So a
        file holding a revoked key and an expired key reports
        :data:`STATUS_ALL_EXPIRED`.

        A record that was revoked AND whose expiry later lapsed reports
        ``revoked`` per record, so it counts as revoked here and a file of those
        still mints. That is the reading this wants: the key died by a decision,
        and the date arriving afterwards changed nothing.

        An ``expires_at`` this reader cannot parse reads as ``expired`` (see
        ``KeyRecord.state``), so a file of corrupt stamps refuses rather than
        mints. Fail-closed, and free.
        """
        counts = self._state_counts()
        if counts["live"]:
            return STATUS_LIVE
        if not self._by_digest:
            return STATUS_NO_KEYS
        if counts["expired"]:
            return STATUS_ALL_EXPIRED
        if counts["revoked"] == len(self._by_digest):
            return STATUS_ALL_REVOKED
        return STATUS_UNKNOWN
