"""Run correlation, log redaction, and Prometheus metrics for unattended runs.

contextlake ships a systemd service + timer (``examples/``), so "nobody is
watching this run" is a supported mode -- but a run that nobody watches is only
useful if it leaves something behind to look at afterwards. This module is that
something:

* a **run id** every log line of one invocation carries, so the index -> connect
  -> embed -> wiki pipeline can be reassembled from an interleaved journal;
* a **redactor** that turns workspace paths and group names into stable
  placeholders, so a log file is shareable without a manual scrub pass;
* a **Prometheus textfile** writer, so the shipped timer is scrapeable through
  node_exporter's textfile collector with no exporter process of our own;
* the opt-in switch for the local HTTP servers' access log.

Deliberately stdlib-only and free of any ``contextlake`` import: this is
core-tier (see ``cli.py``/``core.py``), it must stay importable without the
``[kb]`` extra, and adding a client library for four gauges would
cost far more than writing the four lines of text format by hand.
"""

from __future__ import annotations

import contextvars
import hashlib
import os
import re
import tempfile
import time
import uuid

__all__ = [
    "access_log_enabled", "add_redactions", "add_repo_names", "command", "graph_counts",
    "new_run_id", "note_repo_activity", "note_store_path", "redact", "redaction_configured",
    "repo_activity", "reset_redactions", "run_id", "set_access_log", "set_command", "set_run_id",
    "write_textfile",
]

# ---------------------------------------------------------------------------
# Run correlation
# ---------------------------------------------------------------------------

# A ContextVar is the right primitive (it is per-context, so a future async or
# nested-run caller cannot clobber a peer's id), but PEP 567 gives every *thread*
# a fresh top-level Context: a value set in main() is invisible inside the
# ThreadPoolExecutor workers that do the actual per-repo mirroring in core.py --
# which is precisely where the interesting log lines are emitted. Hence the plain
# module-level fallback alongside it: a CLI process has exactly one run, so the
# process-wide value is the correct answer for any thread that has no context of
# its own. The ContextVar still wins where one has been set.
_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("contextlake_run_id", default="")
_COMMAND: contextvars.ContextVar[str] = contextvars.ContextVar("contextlake_command", default="")
_run_id_fallback = ""
_command_fallback = ""

# Lets an outer scheduler (systemd, cron, CI) stamp its own job id onto the run so
# contextlake's lines join up with the surrounding job's logs. Without it we mint one.
RUN_ID_ENV = "CONTEXTLAKE_RUN_ID"


def new_run_id() -> str:
    """A fresh run id, or the one the calling job pinned via ``CONTEXTLAKE_RUN_ID``."""
    pinned = (os.environ.get(RUN_ID_ENV) or "").strip()
    # Bounded and stripped of anything that would need quoting in a log line: this
    # value comes from the environment and is echoed into every JSON record.
    if pinned:
        return re.sub(r"[^A-Za-z0-9._:-]", "", pinned)[:64] or uuid.uuid4().hex[:12]
    return uuid.uuid4().hex[:12]


def set_run_id(value: str) -> None:
    global _run_id_fallback
    _run_id_fallback = value or ""
    _RUN_ID.set(_run_id_fallback)


def run_id() -> str:
    return _RUN_ID.get() or _run_id_fallback


def set_command(value: str) -> None:
    global _command_fallback
    _command_fallback = value or ""
    _COMMAND.set(_command_fallback)


def command() -> str:
    return _COMMAND.get() or _command_fallback


# ---------------------------------------------------------------------------
# Access log (the local HTTP servers)
# ---------------------------------------------------------------------------

_access_log = False


def set_access_log(enabled: bool) -> None:
    global _access_log
    _access_log = bool(enabled)


def access_log_enabled() -> bool:
    return _access_log


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Longest-first list of (literal, placeholder) directory prefixes, plus the bare
# literals (group names, forge hosts) to blank wherever they appear.
_prefixes: list[tuple[str, str]] = []
_literals: list[tuple[str, str]] = []

# Fallback for a path under a redacted root that no registered repo id claimed:
# whatever follows the workspace/group root is still repo-identifying, so it is
# digested wholesale. Skips a rest already rewritten to ``repo-<digest>`` by the
# registered-name pass, which would otherwise re-digest (and so destroy) the
# file path that pass deliberately preserved. ``<store>`` is not a root here --
# the filenames under it are contextlake's own, and its directory is already gone.
_UNDER_ROOT = re.compile(r"(<workspace>|<group>)/(?!repo-)([\w.@+-]+(?:/[\w.@+-]+)*)")

# Registered repo ids, and the alternation matching them. Bounded on both sides
# by "not a word/dot/dash" -- but deliberately *not* by "not a slash", so
# ``<workspace>/team/api/src/main.py`` matches ``team/api`` and keeps ``/src/
# main.py`` readable, while ``myteam/api`` does not match ``team/api``.
_repo_names: set = set()
_repo_pattern = None


def add_redactions(*, paths=(), literals=()) -> None:
    """Register more of what redaction should hide.

    ``paths`` is an iterable of ``(directory, placeholder)`` pairs (the workspace,
    the knowledge store, ``$HOME``); ``literals`` is ``(text, placeholder)`` pairs
    for values with no path shape -- the configured group/org name, the forge host.

    Additive, because the rules arrive from more than one place and at more than
    one time: the CLI knows ``$HOME`` immediately, the workspace and group only
    once the mirror config is loaded, and the knowledge store's location only when
    a kb command opens it. Sorted longest-first on every call so a nested
    directory is always replaced before its parent. Registering late is fine --
    the formatters call :func:`redact` per line rather than capturing rules when
    they are constructed.
    """
    global _prefixes, _literals
    _prefixes = sorted({**dict(_prefixes),
                        **{str(p).rstrip("/\\"): ph for p, ph in paths if p}}.items(),
                       key=lambda item: len(item[0]), reverse=True)
    _literals = sorted({**dict(_literals),
                        **{str(t): ph for t, ph in literals if t}}.items(),
                       key=lambda item: len(item[0]), reverse=True)


def add_repo_names(names) -> None:
    """Register repo ids so redaction hides them wherever they stand alone.

    An absolute path is only one of the ways a repository names itself in the
    output: a "Missing repositories:" list, a per-repo status line and the JSON
    ``repo`` field all carry the bare ``namespace/name``, with no workspace root
    in front of them to key off. The fleet's own repo list is the only reliable
    way to know those tokens, so the functions that load it hand it over here.

    Cheap when unused: the alternation is compiled on first :func:`redact` call
    and only rebuilt when the set actually grows.
    """
    global _repo_names, _repo_pattern
    new = {str(n) for n in names if n and str(n) not in (".", "..")}
    if new <= _repo_names:
        return
    _repo_names |= new
    _repo_pattern = None


def _repo_names_pattern():
    """The compiled alternation, built on demand and cached.

    Every alternative is a literal (``re.escape``), so there is no quantifier to
    backtrack through and no catastrophic-backtracking risk regardless of how
    many names are registered -- worth stating explicitly next to a pattern this
    large, since redaction is on by default for the log file (see
    tests/kb/test_redos.py for the project's stance on regex inputs). Longest
    first, so a nested id is never shadowed by a prefix of itself.

    Measured at fleet scale (480 repos, i.e. 960 alternatives once each repo's
    local and full path are registered): ~0.03 ms per line, which is noise
    against the git work that produced the line.
    """
    global _repo_pattern
    if _repo_pattern is None and _repo_names:
        alternatives = "|".join(re.escape(n) for n in
                                sorted(_repo_names, key=len, reverse=True))
        _repo_pattern = re.compile(r"(?<![\w.-])(?:" + alternatives + r")(?![\w.-])")
    return _repo_pattern


def reset_redactions() -> None:
    """Drop every registered rule (used by tests to isolate one from the next)."""
    global _prefixes, _literals, _repo_names, _repo_pattern
    _prefixes, _literals, _repo_names, _repo_pattern = [], [], set(), None


def redaction_configured() -> bool:
    return bool(_prefixes or _literals or _repo_names)


def _digest(text: str) -> str:
    return hashlib.blake2s(text.encode("utf-8", "replace"), digest_size=4).hexdigest()


def redact(text: str) -> str:
    """Replace workspace paths and group names with stable placeholders.

    Stable, because the point is a log you can still reason about: the same repo
    always becomes the same ``repo-1a2b3c4d``, so "the same three repos failed
    again" survives the scrub. Honest about its strength: this is *obfuscation for
    sharing*, not a cryptographic guarantee -- an unsalted digest of a short,
    guessable name is confirmable by anyone who guesses the name. It exists so a
    bug report can be pasted into a public issue without leaking an employer's
    repository tree, not to defend against a determined attacker.
    """
    if not text:
        return text
    for prefix, placeholder in _prefixes:
        text = text.replace(prefix, placeholder)
    for literal, placeholder in _literals:
        text = text.replace(literal, placeholder)
    pattern = _repo_names_pattern()
    if pattern is not None:
        text = pattern.sub(lambda m: "repo-" + _digest(m.group(0)), text)
    return _UNDER_ROOT.sub(lambda m: f"{m.group(1)}/repo-{_digest(m.group(2))}", text)


# ---------------------------------------------------------------------------
# Prometheus textfile output
# ---------------------------------------------------------------------------

_store_path = ""

# node_exporter's textfile collector re-reads whatever is in its directory, so a
# metric name we stop emitting simply goes stale rather than disappearing. Keeping
# the names in one place makes the "never write a 0 we did not measure" rule below
# checkable at a glance.
_DURATION = "contextlake_run_duration_seconds"
_EXIT_CODE = "contextlake_run_exit_code"
_REPOS = "contextlake_repos"
_NODES = "contextlake_graph_nodes"
_EDGES = "contextlake_graph_edges"
_LAST_SUCCESS = "contextlake_last_success_timestamp_seconds"

_HELP = {
    _DURATION: ("Wall-clock seconds the last contextlake run took.", "gauge"),
    _EXIT_CODE: ("Exit status of the last contextlake run (0 = success).", "gauge"),
    _REPOS: ("Repositories the last run handled, by outcome.", "gauge"),
    _NODES: ("Nodes in the local knowledge graph.", "gauge"),
    _EDGES: ("Edges in the local knowledge graph.", "gauge"),
    _LAST_SUCCESS: ("Unix time of the last contextlake run that exited 0.", "gauge"),
}


def note_store_path(path) -> None:
    """Record which knowledge store this run opened, so the graph gauges can be
    read at the end without the metrics writer having to import the [kb] layer."""
    global _store_path
    _store_path = str(path or "")


def graph_counts():
    """``(nodes, edges)`` for the store this run touched, or ``(None, None)``.

    Read through a plain stdlib ``sqlite3`` connection in read-only URI mode: a
    gauge must never be able to lock, migrate, or otherwise disturb a live store,
    and importing the knowledge layer just to publish two numbers would drag
    tree-sitter and the MCP SDK into every metrics write. ``None`` on any problem
    -- see :func:`write_textfile` for why that is emitted as *absence* rather
    than as zero.
    """
    if not _store_path or not os.path.exists(_store_path):
        return None, None
    import sqlite3

    conn = None
    try:
        uri = "file:" + _store_path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return int(nodes), int(edges)
    except Exception:  # noqa: BLE001 - a gauge is never worth failing a run over
        return None, None
    finally:
        # Explicit: `with sqlite3.connect(...)` manages a *transaction*, not the
        # connection, and would leave the handle open.
        if conn is not None:
            conn.close()


_repo_activity = (None, None)


def note_repo_activity(total, changed) -> None:
    """Record how many repositories this run saw and how many had moved.

    The incremental index gate already decides this per repo (``kb/cmds/index.py``
    compares HEAD and the parser version), but until now the count reached a log
    line and stopped. The scheduler needs it as a number: it is the activity
    half of the interval formula.

    Lives here rather than being returned up the call stack because the reader
    is ``_RunMetrics.write()`` in the core tier, and the writer is inside the
    optional knowledge layer. ``note_store_path`` solved the same problem the
    same way.
    """
    global _repo_activity
    _repo_activity = (total, changed)


def repo_activity():
    """``(total, changed)`` for this run, or ``(None, None)`` if nothing measured it.

    ``None`` is absence, not zero. A run that never indexed anything did not
    observe "nothing changed", and writing a 0 it did not measure is the rule
    ``write_textfile`` already follows.
    """
    return _repo_activity


def _escape(value: str) -> str:
    return str(value).replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _labels(pairs) -> str:
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in pairs if v not in (None, ""))
    return "{" + inner + "}" if inner else ""


def _previous_last_success(path: str, mine: str, *, replace_mine: bool):
    """Series lines for :data:`_LAST_SUCCESS` already in the file, to carry over.

    A failing run must not erase the record of when the mirror last worked --
    that timestamp is the whole basis of a "stale for six hours" alert -- so on
    failure our own series is carried over untouched and only a *successful* run
    replaces it. Lines belonging to other commands are always kept: several
    commands may share one textfile, and this run knows nothing about theirs.

    Only the *series* lines are carried, never their ``# HELP``/``# TYPE``: a
    second HELP line for one metric name makes the whole file unparseable.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    kept = []
    for line in lines:
        if not line.startswith(_LAST_SUCCESS + "{") and not line.startswith(_LAST_SUCCESS + " "):
            continue
        if replace_mine and line.startswith(_LAST_SUCCESS + mine + " "):
            continue  # this run succeeded, so it writes its own value below
        kept.append(line)
    return kept


def write_textfile(path, *, command_name, duration_seconds, exit_code,
                   repos=None, nodes=None, edges=None, now=None) -> str:
    """Write one Prometheus textfile-collector file, atomically. Returns the text.

    ``repos`` is a mapping of outcome -> count (contextlake's existing
    ``StageResult`` fields: ok / failed / skipped), read straight from what the
    stages already counted rather than recounted here.

    Two rules worth stating because getting them wrong is worse than having no
    metrics at all. First, **a value we could not measure is omitted, never
    written as 0** -- a ``mirror sync`` publishing ``contextlake_graph_nodes 0``
    reads as "the graph was wiped" and is exactly the sort of gauge that wakes
    someone up. Second, the file is written to a temporary neighbour and renamed
    into place, so a collector that scrapes mid-write sees the old file whole
    rather than half of the new one.
    """
    now = time.time() if now is None else now
    labels = _labels([("command", command_name)])
    lines = []

    def metric(name, series):
        help_text, kind = _HELP[name]
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(series)

    metric(_DURATION, [f"{_DURATION}{labels} {float(duration_seconds):.3f}"])
    metric(_EXIT_CODE, [f"{_EXIT_CODE}{labels} {int(exit_code)}"])
    if repos:
        metric(_REPOS, [f"{_REPOS}{_labels([('command', command_name), ('status', status)])} "
                        f"{int(count)}" for status, count in sorted(repos.items())])
    if nodes is not None:
        metric(_NODES, [f"{_NODES} {int(nodes)}"])
    if edges is not None:
        metric(_EDGES, [f"{_EDGES} {int(edges)}"])

    succeeded = int(exit_code) == 0
    carried = _previous_last_success(str(path), labels, replace_mine=succeeded)
    if succeeded:
        carried.append(f"{_LAST_SUCCESS}{labels} {int(now)}")
    if carried:
        metric(_LAST_SUCCESS, carried)

    text = "\n".join(lines) + "\n"
    target = str(path)
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".contextlake-metrics-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return text
