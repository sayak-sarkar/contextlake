"""Has the file moved under the citation we are about to hand an agent?

`freshness.py` asks whether the graph still describes the code on disk once per session,
per **repo**, keyed on the head commit and `PARSER_VERSION`. That is the right key for
derived artefacts and it cannot see the case that dominates in practice: an agent editing
files *between* index runs, inside the same commit. The graph says ``src/thing.cpp:412``,
twenty lines were inserted above it, and the answer still says 412. A confidently wrong
citation is worse than a miss, because the agent goes and reads it.

This module is that same question one level down -- per **file**, on the serving path --
and its whole job is to **disclose**, never to refuse. A drifted citation is still the
best pointer available; withholding it or raising would replace a slightly-off line number
with no answer at all (see ``tests/kb/test_server_contract.py`` for why a raise costs every
later answer, not just this one).

Two stages, because either one alone is useless here:

* **the gate** -- one ``stat()`` per distinct file, compared against the repo's
  ``indexed_at``. Cheap enough to run on every returned node.
* **the confirmation** -- only for files the gate flags: is the node's name still at the
  cited line? That check already exists, in ``kb/eval.py``'s :func:`verify_citations`, and
  is *called* here rather than reimplemented, so there is one definition of "does this
  citation still hold" in the package and two callers of it.

Why not the gate alone: this project's own mirror rewrites mtimes on content-identical
files (a ``git checkout`` between branches, a re-clone), so mtime-only would report drift
for nearly every citation in the fleet at once -- a guard that fires on everything is worth
exactly as much as one that fires on nothing. Why not the confirmation alone: it reads
files, and the serving path returns many nodes.

Three outcomes, never two. ``verified`` and ``stale`` are the interesting pair, but a file
that cannot be stat'd -- no local checkout, an unreadable mode, a repo that was registered
and never indexed -- is ``unverifiable``, and folding that into either one is how a guard
starts lying: called stale it invents drift on a machine that simply lacks the mirror,
called verified it certifies a check that never ran. The vocabulary is deliberately
``eval.py``'s (``checkout_missing``, ``file_missing``, ``file_unreadable``,
``line_out_of_range``, ``name_absent``) so one reason means one thing in both places.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIRM_BUDGET = 32
"""Confirming reads one request may spend before it stops confirming and says so.

The gate is O(distinct files) syscalls and is not budgeted -- a ``stat()`` is not worth
metering. The confirmation reads up to ``line_start`` lines of a file and only runs on
files that were written after indexing, so its cost is bounded by how much really changed;
this cap is what stops the pathological case (a fresh re-clone, where every file looks
modified and every node escalates) from turning one tool call into hundreds of file reads.

32 covers a whole default result set with room to spare: the node-returning tools cap at
``limit`` 20-50 and ``ask`` at ``k`` 8, and results concentrate on far fewer distinct files
than nodes. Past the cap, a flagged citation is reported ``stale`` with
``modified_after_index`` -- the gate's own verdict, unconfirmed -- because a budget nobody
is told about reads as a clean bill of health for work that never happened (the same rule
``freshness.py`` applies to its time budget)."""

STALE_REASONS = frozenset({
    "file_missing",        # the file the citation names is gone
    "line_out_of_range",   # the file shrank; the cited line does not exist any more
    "name_absent",         # the line exists and the symbol is no longer on it
    "modified_after_index",  # written after indexing, not confirmed (budget spent)
})
"""Every reason that means "do not trust this line number as written".

Enumerated rather than implied, because the mapping from ``eval.py``'s outcomes is where
this could quietly go wrong: :func:`verify_citations` also returns ``broken`` for
``no_citation`` and ``node_missing``, and **neither is drift** -- a node that never had a
line number has nothing that could have moved, and reporting it as stale would be the exact
class of plausible-but-wrong disclosure this package keeps having to fix. Those two are
mapped away from ``stale`` explicitly below.

This set is read on exactly one path: translating an ``eval.py`` verdict. Reasons this
module invents for itself do not belong in it -- ``content_unchecked`` is emitted as
*unverifiable*, and listing it here would say the opposite of what the code does while
never being reachable to prove either way."""

_STATUS_VERIFIED = "verified"
_STATUS_STALE = "stale"
_STATUS_UNVERIFIABLE = "unverifiable"


@dataclass
class SliceCheck:
    """One node's citation, weighed against the file on disk right now."""

    status: str        # "verified" | "stale" | "unverifiable"
    reason: str = ""   # "" when verified
    note: str | None = None  # the sentence the response carries; None when verified


_NOTES = {
    "file_missing": ("the file is no longer on disk, so this citation cannot be read at "
                     "all -- re-run `kb index` for this repo"),
    "line_out_of_range": ("the file was edited after indexing and is now shorter than the "
                          "cited line -- the line number is wrong, find the symbol by name"),
    "name_absent": ("the file was edited after indexing and this symbol is no longer at "
                    "the cited line -- the line number has moved, find it by name"),
    "modified_after_index": ("the file was edited after indexing and this citation was not "
                             "re-checked (the request's confirmation budget was spent) -- "
                             "treat the line number as approximate"),
    "content_unchecked": ("the file was edited after indexing, so what the graph records "
                          "about its contents may be out of date -- the path is still "
                          "right, the contents were not checked"),
    "checkout_missing": ("no readable local checkout is on record for this repo, so this "
                         "citation was NOT checked against disk"),
    "file_unreadable": ("the file could not be read, so this citation was NOT checked "
                        "against disk"),
    "index_time_unknown": ("this repo carries no index timestamp, so there is no baseline "
                           "to compare the file against -- this citation was NOT checked"),
    "confirm_inconclusive": ("the citation could not be re-checked against disk"),
    "probe_error": ("the staleness check itself failed, so this citation was NOT checked "
                    "against disk"),
}


def _note(reason: str) -> str:
    return _NOTES.get(reason, "this citation was NOT checked against disk")


def parse_indexed_at(stamp: str | None) -> int | None:
    """The index timestamp as epoch nanoseconds, or None when it cannot be read.

    Two spellings reach this: ``state.utcnow_iso`` writes an aware ``+00:00`` timestamp,
    and at least one caller writes a literal ``...Z``, which ``fromisoformat`` rejects
    before Python 3.11 while this package supports 3.10. A naive stamp is read as UTC,
    because ``utcnow_iso`` is the only writer that produces one in anger.

    None is a real answer, not a failure: it means "no baseline", which the probe reports
    as *unverifiable*. Guessing a baseline would silently certify every file under it.
    """
    if not stamp:
        return None
    text = stamp.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


@dataclass
class ProbeStats:
    """What the guard actually did, so "bounded" is a number rather than a claim."""

    checked: int = 0     # nodes with a citation that were weighed
    statted: int = 0     # stat() syscalls -- one per DISTINCT file, never per node
    escalated: int = 0   # confirming reads -- only for files the gate flagged
    cache_hits: int = 0  # nodes answered from the per-request caches
    budget_spent: int = 0  # flagged citations reported unconfirmed because the cap hit


@dataclass
class DriftProbe:
    """Per-request staleness checker for the nodes one response is about to carry.

    Per-request is the unit that matters: one file cited by twelve nodes costs one
    ``stat()``, and one node reached twice costs one check. Held for the life of a single
    tool call and thrown away, so it can never serve a verdict from a previous request --
    the whole point is that the file may have changed a second ago.
    """

    store: object
    confirm_budget: int = DEFAULT_CONFIRM_BUDGET
    stats: ProbeStats = field(default_factory=ProbeStats)
    _roots: dict = field(default_factory=dict)       # repo id -> Path | None
    _baselines: dict = field(default_factory=dict)   # repo id -> epoch ns | None
    _stats_cache: dict = field(default_factory=dict)  # abs path -> (mtime_ns | None, err)
    _by_node: dict = field(default_factory=dict)     # node id -> SliceCheck

    def check(self, node) -> SliceCheck | None:
        """Weigh one node's citation. None when the node has no citation to weigh.

        Never raises. This runs on the node-returning path of every tool, and a guard that
        can turn a good answer into a transport error would cost far more than the drift it
        reports (``tests/kb/test_server_contract.py``). Anything unexpected comes back as
        *unverifiable*, which is the honest reading of "the check did not complete".
        """
        try:
            return self._check(node)
        except Exception:  # noqa: BLE001 -- see the docstring: disclosure, never refusal
            return SliceCheck(_STATUS_UNVERIFIABLE, "probe_error", _note("probe_error"))

    # --- internals ---------------------------------------------------------------

    def _check(self, node) -> SliceCheck | None:
        if not getattr(node, "file", None):
            # No file, no citation, nothing that could have moved. Deliberately not
            # `eval.py`'s `no_citation` verdict: that harness is scoring whether an answer
            # is citable at all, this guard only speaks about citations that exist.
            return None
        cached = self._by_node.get(node.id)
        if cached is not None:
            self.stats.cache_hits += 1
            return cached
        out = self._weigh(node)
        self._by_node[node.id] = out
        self.stats.checked += 1
        return out

    def _weigh(self, node) -> SliceCheck:
        root = self._root(node.repo)
        if root is None:
            return _unverifiable("checkout_missing")
        baseline = self._baseline(node.repo)
        if baseline is None:
            return _unverifiable("index_time_unknown")
        mtime_ns, err = self._stat(root / node.file)
        if err == "file_missing":
            return _stale("file_missing")
        if err:
            return _unverifiable(err)
        if mtime_ns is not None and mtime_ns <= baseline:
            # The file has not been written since the graph was built. This is the
            # overwhelmingly common case and it costs one syscall.
            return SliceCheck(_STATUS_VERIFIED)
        return self._confirm(node)

    def _confirm(self, node) -> SliceCheck:
        """The file changed after indexing -- did this particular citation survive it?

        A no-op save, a branch checkout, a re-clone: all of them move an mtime without
        moving a line. Escalating to the real check is what keeps those from being reported
        as drift, and is the reason the gate can afford to be as blunt as it is.
        """
        if node.kind == "file" or not node.line_start:
            # A whole-file citation IS the path, and the path is still right -- but the
            # graph's record of what the file *contains* was built from an older version of
            # it, and confirming that would mean re-parsing, which this guard will not do
            # on the serving path. `eval.py` calls this verified because it is scoring
            # whether the citation resolves; the honest answer to *this* module's question
            # is that the contents were not checked.
            return _unverifiable("content_unchecked")
        if self.stats.escalated >= self.confirm_budget:
            self.stats.budget_spent += 1
            return _stale("modified_after_index")
        from ..eval import verify_citations  # local: the server path must not import eval

        self.stats.escalated += 1
        checks = verify_citations(self.store, [node.id])
        if not checks:
            return _unverifiable("confirm_inconclusive")
        reason = checks[0].reason
        if checks[0].status == "verified":
            return SliceCheck(_STATUS_VERIFIED)
        if reason in STALE_REASONS:
            return _stale(reason)
        # `unverifiable` passes through with its own reason; `node_missing` and
        # `no_citation` cannot be reached from here (the node came from the store and has a
        # file), and if they ever are, "I could not tell" is the safe reading -- calling a
        # node with no line number *stale* would invent drift that has no line to be on.
        return _unverifiable(reason if reason else "confirm_inconclusive")

    def _root(self, repo_id: str) -> Path | None:
        if repo_id not in self._roots:
            r = self.store.get_repo(repo_id)
            p = Path(r.path) if r and r.path else None
            self._roots[repo_id] = p if (p and p.is_dir()) else None
        return self._roots[repo_id]

    def _baseline(self, repo_id: str) -> int | None:
        if repo_id not in self._baselines:
            getter = getattr(self.store, "get_repo_indexed_at", None)
            self._baselines[repo_id] = parse_indexed_at(getter(repo_id) if getter else None)
        return self._baselines[repo_id]

    def _stat(self, path: Path) -> tuple[int | None, str]:
        """``(mtime_ns, error_reason)`` for one path -- one syscall per distinct file."""
        key = str(path)
        hit = self._stats_cache.get(key)
        if hit is None:
            self.stats.statted += 1
            try:
                hit = (os.stat(key).st_mtime_ns, "")
            except FileNotFoundError:
                hit = (None, "file_missing")
            except OSError:
                hit = (None, "file_unreadable")
            self._stats_cache[key] = hit
        return hit


def _stale(reason: str) -> SliceCheck:
    return SliceCheck(_STATUS_STALE, reason, _note(reason))


def _unverifiable(reason: str) -> SliceCheck:
    return SliceCheck(_STATUS_UNVERIFIABLE, reason, _note(reason))
