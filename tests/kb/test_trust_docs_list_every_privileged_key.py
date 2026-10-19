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

from contextlake.kb.trust import CREDENTIAL_PROVIDERS, PRIVILEGED_SOURCE_KEYS

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


# For each page, a phrase that only the paragraph listing what a discovered file can still
# do contains.
STILL_WORKS = (
    ("SECURITY.md", "The rest of that file still applies"),
    ("docs/configuration.md", "keep working from a local file"),
)


@pytest.mark.parametrize(("name", "marker"), STILL_WORKS)
def test_the_still_works_list_carves_out_every_credential_provider(name, marker):
    """configuration.md said "any non-`cli` LLM provider" keeps working from a discovered
    file. The code switches an `openai` or `anthropic` tier off for the run
    (REFUSE_DISCOVERED_CREDENTIAL_PROVIDER), so a reader who followed the page lost the tier.

    The carve-out can sit in the list's paragraph or in the next one, so both are searched.
    """
    paras = re.split(r"\n\s*\n", (ROOT / name).read_text(encoding="utf-8"))
    at = next((i for i, p in enumerate(paras) if marker in p), None)
    assert at is not None, f"{name} has no paragraph with {marker!r}, so the check is vacuous"
    window = "\n\n".join(paras[at:at + 2])
    missing = [pr for pr in sorted(CREDENTIAL_PROVIDERS) if f"`{pr}`" not in window
               and f'"{pr}"' not in window]
    assert not missing, (
        f"{name}: the paragraph that lists what still works from a discovered file, and "
        f"the one after it, do not name {missing}. kb/config.py switches those off")
    assert "non-`cli`" not in window
