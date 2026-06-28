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
    fmt = "%x1f%an%x1f%ae%x1f%at"
    cmd = ["git", "-C", str(repo_path), "log", "--no-merges", f"--format={fmt}", "--numstat"]
    if subpath:
        cmd += ["--", subpath]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    rows = list(_parse_log(proc.stdout))
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
    return owners[:limit]
