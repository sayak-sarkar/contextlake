"""Derive likely owners / subject-matter experts for a repo (or sub-path) from its
git commit history.

Pure stdlib: shells out to ``git log`` and ranks contributors by a recency-weighted
blend of commit volume and lines changed. Offline — it reads only the local mirror,
so no names or emails are ever stored in this package; they are computed at call time
from whatever history the repo carries.

The score for each contributor is ``sum over their commits of (lines_changed + 1) *
0.5 ** (age_days / HALFLIFE)`` where ``age_days`` is measured from the *newest* commit
in the examined history (not wall-clock), keeping results deterministic and making
"who has been active here lately" win over a long-departed prolific author.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

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
_CACHE: dict[tuple, list] = {}
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
    when git is unavailable, the path isn't a repo, or there is no matching history.
    """
    # NOT named `key`: the aggregation loop below rebinds that to a contributor
    # email. The first version of this cache did use `key`, so by the time it
    # stored the result the variable held an email -- it wrote under a garbage key
    # and never once hit, while looking entirely correct. There is now a test that
    # asserts a second call does no git work.
    cache_key = (str(repo_path), subpath, limit, _head_sha(repo_path))
    if cache_key[3] is not None and cache_key in _CACHE:
        return list(_CACHE[cache_key])

    fmt = "%x1f%an%x1f%ae%x1f%at"

    def _walk(since_days: float | None) -> list:
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
        except (OSError, subprocess.SubprocessError):
            return []
        return list(_parse_log(proc.stdout)) if proc.returncode == 0 else []

    # Bounded first, then fall back. The fallback is not defensive padding: a repo whose
    # newest commit predates the window returns NOTHING from the bounded walk, and
    # answering "no owners" for a dormant-but-real repository would be a worse bug than
    # the slowness this bound removes. Same prefer-then-fall-back shape as the
    # internal-linkage candidate filter in the parser.
    rows = _walk(_WALK_DAYS) or _walk(None)
    if not rows:
        return []
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
        _CACHE[cache_key] = list(out)
    return out
