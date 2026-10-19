"""Derive likely owners / subject-matter experts for a repo (or sub-path) from its
git commit history.

Pure stdlib for the work itself: it shells out to ``git log`` and ranks contributors by a
recency-weighted blend of commit volume and lines changed. Offline — it reads only the local mirror,
so no names or emails are ever stored in this package; they are computed at call time
from whatever history the repo carries.

The score for each contributor is ``sum over their commits of (lines_changed + 1) *
0.5 ** (age_days / HALFLIFE)`` where ``age_days`` is measured from the *newest* commit
in the examined history (not wall-clock), keeping results deterministic and making
"who has been active here lately" win over a long-departed prolific author.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import style
from ..logging_setup import log
from .repo_identity import is_own_gitdir

HALFLIFE_DAYS = 180.0
_US = "\x1f"  # unit separator: safe field/record delimiter (won't appear in names)

# How far back the log walk goes, expressed in half-lives so the bound follows the
# scoring rather than being a round number somebody liked.
#
# Measured on a 36,290-commit legacy repository: the unbounded
# `git log --numstat` walk took **67 seconds**, and the dashboard ran it
# SYNCHRONOUSLY ON EVERY repo-detail request -- 30 of the 41 seconds that request
# took. `--since=3.years` cut the same walk to 10 seconds.
#
# 12 half-lives is ~5.9 years, at which point a commit's weight is
# 0.5 ** 12 = 0.00024 -- under a fortieth of one percent of a fresh commit's. Nothing
# that far back can change a ranking, so walking it is pure cost.
_WALK_HALFLIVES = 12
_WALK_DAYS = HALFLIFE_DAYS * _WALK_HALFLIVES

# Owners change only when history does, so the answer is cacheable on the commit it was
# computed from. Keyed on HEAD rather than time: three separate callers (the dashboard
# panel, `kb owners`, and the MCP `who_knows` tool) each used to pay the full walk.
_CACHE: dict[tuple, tuple[list, str | None]] = {}
_CACHE_MAX = 64  # a fleet is many repos; bound it so a long-lived server cannot grow forever


def _head_sha(repo_path) -> str | None:
    """HEAD's sha, or None if this is not a readable repo. ~5ms, so cheap enough to
    pay on every call in exchange for a correct cache key."""
    try:
        p = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                           capture_output=True, text=True,
                           errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() or None if p.returncode == 0 else None


@dataclass
class Owner:
    name: str
    email: str
    commits: int
    lines: int
    last_active: str  # YYYY-MM-DD (UTC) of the contributor's most recent commit
    share: float      # 0..1 fraction of the total recency-weighted score
    score: float


class Ranking(list):
    """The ranked :class:`Owner` rows. A plain ``list`` to every caller that iterates,
    indexes or tests it for truth.

    ``gap`` says why the list is empty when git could not be asked, and is ``None`` when
    git was asked and answered. ``who_knows`` is the caller that has to tell the two
    apart: an empty history and a history nobody read are different facts.
    """

    gap: str | None = None


def _ranking(rows, gap: str | None = None) -> Ranking:
    ranking = Ranking(rows)
    ranking.gap = gap
    return ranking


def ranking_gap(owners) -> str | None:
    """Why ``owners`` is empty, or ``None`` when it holds rows.

    The words ``who_knows`` puts in ``ranking_gap``: the ranking's own ``gap`` when git could
    not be asked, else the sentence for a history that was read and matched nobody.
    ``kb owners --json`` reports this too, so a script can tell a missing clone from an
    empty history.
    """
    if owners:
        return None
    return (getattr(owners, "gap", None)
            or "its git history was read but attributed no commits to anyone")


# Why no history was read, per `reason` code, worded to follow "its" in `ranking_gap`.
# A reason code with no entry (git ran and found nothing) has no gap.
_GAPS = {
    "git_unavailable": "git is not installed or could not be run, so its git history was "
                       "not read",
    "missing": "its clone directory is not there, so its git history was never read",
    "not_own": "its path is not the root of a git checkout, so its git history was not "
               "read",
    "not_a_repo": "git could not read a history at its path (no repository, or no commits "
                  "yet)",
}


def _unreadable(repo_path) -> str:
    """The reason code for a path git history cannot be read from, or ``""``.

    ``git -C <path> log`` walks UP to the nearest repository. A plain directory inside
    another working tree (an extracted archive, a subfolder of a monorepo checkout, the
    bundled sample fleet inside a source tree) was therefore ranked by the contributors of
    the repository around it. The indexer makes the same check with the same function.
    """
    if shutil.which("git") is None:
        return "git_unavailable"
    if not os.path.isdir(repo_path):
        return "missing"
    if not is_own_gitdir(str(repo_path)):
        return "not_own"
    return ""


def anon_author(name: str | None, email: str | None, anonymizer=None) -> str:
    """A non-reversible pseudonym for a git author, ``Contributor`` and 4 hex.

    The hex is keyed: by ``anonymizer`` when given (one ``kb wiki`` run), else by the
    anonymizer of the output being built (one export or one server start), else by a key
    made once per process. The same person reads the same within one of those and
    differently across two. Until 2026-10 it was 4 hex of an unkeyed SHA-256 of the email:
    65,536 values, so anyone with a list of candidate emails could match a pseudonym to a
    person, and to the same person in every export.

    One implementation serves the dashboard, the chat and the wiki, so the panel and the
    chat agree within one server start. Keyed on the email first: a contributor whose
    display name changes stays one pseudonym.
    """
    from .anonymize import active, process_anonymizer

    return (anonymizer or active() or process_anonymizer()).author_label(name, email)


def _parse_log(out: str):
    """Yield ``(name, email, ts, lines_changed)`` per commit from the log stream.

    Each commit is a header line ``\\x1f<name>\\x1f<email>\\x1f<unixts>`` followed by
    zero or more ``--numstat`` rows (``added\\tdeleted\\tpath``; binary files show ``-``).
    """
    name = email = None
    ts = lines = 0
    have = False
    for line in out.splitlines():
        if line.startswith(_US):
            if have:
                yield name, email, ts, lines
            parts = line[1:].split(_US)
            name, email, ts = parts[0], parts[1], int(parts[2])
            lines = 0
            have = True
        elif line.strip() and have:
            a, d, *_ = line.split("\t")
            lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    if have:
        yield name, email, ts, lines


def compute_owners(repo_path, subpath: str | None = None, *,
                   limit: int = 10, timeout: int = 30) -> list[Owner]:
    """Rank likely owners/SMEs for ``repo_path`` (optionally restricted to ``subpath``).

    Returns up to ``limit`` :class:`Owner` rows, highest score first. Returns ``[]``
    when git is unavailable, the path isn't the root of its own repo, or there is no
    matching history. A :class:`Ranking`: its ``gap`` says which of those it was.
    """
    # NOT named `key`: the aggregation loop below rebinds that to a contributor
    # email. The first version of this cache did use `key`, so by the time it
    # stored the result the variable held an email -- it wrote under a garbage key
    # and never once hit, while looking entirely correct. There is now a test that
    # asserts a second call does no git work.
    cache_key = (str(repo_path), subpath, limit, _head_sha(repo_path))
    if cache_key[3] is not None and cache_key in _CACHE:
        rows, gap = _CACHE[cache_key]
        return _ranking(rows, gap)

    fmt = "%x1f%an%x1f%ae%x1f%at"

    def _walk(since_days: float | None) -> tuple[list, bool, str]:
        """``(rows, ok, reason)``. ``ok`` is False when git could not be asked at all.

        The ok/not-ok distinction drives control flow. The first version returned a bare
        list, so the caller's ``bounded or unbounded`` could not tell "this repo genuinely
        has no commits in the window" (a fast, correct empty) from "the walk timed out" (a
        30-second failure). It therefore ran the *unbounded* walk after a timeout, which
        is strictly more work on a repository that had just proved it cannot finish the
        cheaper one. Measured on real clones: 60.1 seconds, two timeouts back to back.

        ``reason`` exists because ``ok`` alone was being *narrated* as a timeout. Every
        one of the three failures collapsed into ``False``, and the caller then printed
        "could not be read within 30s ... This is a timeout, not an empty history" for
        all of them. A path that is not a git repository exits 128 in three
        milliseconds, and the warning claimed a 30-second timeout that never happened --
        stating a cause nobody observed, in a message written specifically to stop a
        different misreading. Seen 7 times in one site deploy over the bundled sample
        fleet, which is not a git repository at all."""
        cmd = ["git", "-C", str(repo_path), "log", "--no-merges",
               f"--format={fmt}", "--numstat"]
        if since_days is not None:
            # int(), NOT the float. `--since="2160.0 days ago"` is accepted by git,
            # exits 0, and returns ZERO commits -- it silently fails to parse. The
            # first version of this passed the float, so the bounded walk always came
            # back empty, the fallback always ran, and the net effect was TWO full
            # walks instead of one: slower than the code it replaced, while every
            # test passed because the fallback still produced the right answer.
            cmd.append(f"--since={int(since_days)} days ago")
        if subpath:
            cmd += ["--", subpath]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return [], False, "timeout"
        except (OSError, subprocess.SubprocessError):
            # git is not installed, or could not be spawned at all.
            return [], False, "git_unavailable"
        if proc.returncode != 0:
            # Overwhelmingly: not a git repository. Exits 128, in milliseconds.
            return [], False, "not_a_repo"
        return list(_parse_log(proc.stdout)), True, ""

    # Bounded first, then fall back. The fallback is not defensive padding: a repo whose
    # newest commit predates the window returns NOTHING from the bounded walk, and
    # answering "no owners" for a dormant-but-real repository would be a worse bug than
    # the slowness this bound removes. Same prefer-then-fall-back shape as the
    # internal-linkage candidate filter in the parser.
    rows, ok, reason = [], False, _unreadable(repo_path)
    if not reason:
        rows, ok, reason = _walk(_WALK_DAYS)
    if ok and not rows:
        # The bounded walk SUCCEEDED and found nothing, so this repo's newest commit
        # predates the window. That is the one case the unbounded fallback exists for:
        # answering "no owners" for a dormant-but-real repository would be worse than
        # the slowness the bound removes.
        rows, ok, reason = _walk(None)
    if not rows:
        # Cached either way, and that is the fix. Before this, a repo whose walk timed
        # out returned here without writing the cache, so every later request paid the
        # timeout again -- measured at 60.1s on a real clone, and the dashboard calls
        # this on every repo-detail request. The cache is per-process and keyed on HEAD,
        # so a restart or a new commit retries; that bounds how long a transient failure
        # can be remembered, which is what makes caching a failure acceptable here.
        if reason == "timeout":
            log(style.warn(
                f"owners: git history for {repo_path} could not be read within "
                f"{timeout}s, so no owners are reported for this commit. This is a "
                "timeout, not an empty history."))
        elif reason == "git_unavailable":
            log(style.warn(
                f"owners: git could not be run for {repo_path}, so no owners are "
                "reported. Install git, or ignore this if ownership is not wanted."))
        # `not_a_repo`, `missing` and `not_own` are deliberately silent. They are the
        # ordinary state of an indexed tree that was never a clone (the bundled sample
        # fleet, an extracted archive), the ranking's `gap` already says which one it
        # was, and warning per repository per request would bury the two failures above
        # -- which is what happened when all three shared one message.
        gap = (f"git could not read its history within {timeout}s, so no owners are "
               "reported (a timeout, not an empty history)" if reason == "timeout"
               else _GAPS.get(reason))
        if cache_key[3] is not None:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[cache_key] = ([], gap)
        return _ranking([], gap)
    newest = max(ts for _, _, ts, _ in rows)

    agg: dict[str, dict] = {}
    for name, email, ts, lines in rows:
        key = email or name
        a = agg.get(key)
        if a is None:
            a = agg[key] = {"name": name, "email": email, "commits": 0,
                            "lines": 0, "last": 0, "score": 0.0}
        age_days = max(0.0, (newest - ts) / 86400.0)
        a["commits"] += 1
        a["lines"] += lines
        a["score"] += (lines + 1) * (0.5 ** (age_days / HALFLIFE_DAYS))
        if ts >= a["last"]:          # keep the contributor's most recent display name
            a["name"] = name
            a["last"] = ts

    total = sum(a["score"] for a in agg.values()) or 1.0
    owners = [
        Owner(name=a["name"], email=a["email"], commits=a["commits"], lines=a["lines"],
              last_active=datetime.fromtimestamp(a["last"], timezone.utc).strftime("%Y-%m-%d"),
              share=a["score"] / total, score=a["score"])
        for a in agg.values()
    ]
    owners.sort(key=lambda o: -o.score)
    out = owners[:limit]
    if cache_key[3] is not None:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))     # oldest-inserted out; dicts keep order
        _CACHE[cache_key] = (list(out), None)
    return _ranking(out)
