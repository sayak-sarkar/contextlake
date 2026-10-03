"""The two trust tables must name every key `trust.PRIVILEGED_SOURCE_KEYS` gates.

e80aaf00 added `auth` and `user` to `SOURCE_AUTH_KEYS`. SECURITY.md (the security
statement of record) and docs/configuration.md kept listing `token_env` and `auth_dir`
only, so the written gate described two keys fewer than the code enforced. The error was
in the cautious direction, which is why nothing noticed: no test tied either table to the
constant, so the next key added to it would drift the same way.

The assertion is per key, against a table row, because a page that mentions `user`
anywhere in prose would satisfy a bare substring check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from contextlake.kb.trust import PRIVILEGED_SOURCE_KEYS

ROOT = Path(__file__).resolve().parents[2]
TABLES = ("SECURITY.md", "docs/configuration.md")


def _source_rows(page: str) -> list[str]:
    return [ln for ln in page.splitlines()
            if ln.lstrip().startswith("|") and "[[sources]]" in ln]


@pytest.mark.parametrize("name", TABLES)
def test_the_trust_table_names_every_privileged_source_key(name):
    rows = _source_rows((ROOT / name).read_text(encoding="utf-8"))
    assert rows, f"{name} has no `[[sources]]` table rows, so the check below is vacuous"
    missing = [
        key for key in sorted(PRIVILEGED_SOURCE_KEYS)
        if not any(re.search(r"`(?:\[\[sources\]\] )?" + re.escape(key) + r"`", row)
                   for row in rows)
    ]
    assert not missing, (
        f"{name} does not list {missing} among the gated `[[sources]]` keys that "
        "kb/trust.py refuses from a discovered config file")
