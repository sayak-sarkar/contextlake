"""An unreachable source must not read as an empty one.

The measured defect: four of nine source types swallowed every fetch exception with no
log line, so a wrong URL, an expired token, an HTTP 500, a proxy block and a genuinely
empty page all produced the same `✓ 0 documents`, exit 0. On a content pipeline that
means ingestion silently stops and nothing in CI can detect it.

`sources/files.py` was already the in-house standard -- it names every file it skips and
why -- so this is that standard applied to the network sources, plus a machine-readable
tally so `cmds/ingest.py` can tell "empty" from "broken" without parsing log lines.
"""

from __future__ import annotations

import pytest

from contextlake.kb.sources.api import ApiSource
from contextlake.kb.sources.graphql import GraphQLSource
from contextlake.kb.sources.web import WebSource


def _unreachable(monkeypatch, module):
    """Make the module's fetch raise the way a dead endpoint does."""
    def boom(*a, **k):
        raise OSError("Connection refused")
    monkeypatch.setattr(module, "urlopen", boom, raising=False)


@pytest.mark.parametrize("make", [
    lambda: WebSource(url="http://127.0.0.1:9/nope"),
    lambda: ApiSource(url="http://127.0.0.1:9/nope"),
    lambda: GraphQLSource(url="http://127.0.0.1:9/nope", query="{ x }"),
])
def test_an_unreachable_source_records_the_miss_instead_of_swallowing_it(make):
    """THE LOAD-BEARING ASSERTION. Port 9 (discard) refuses fast and deterministically.

    Before the fix every one of these yielded nothing and recorded nothing, which is
    indistinguishable from success on an empty source.
    """
    src = make()
    docs = list(src.iter_documents())

    assert docs == [], "the fixture should not reach anything"
    assert getattr(src, "failures", None), (
        f"{type(src).__name__} yielded nothing and recorded nothing — a caller cannot "
        "tell this from a source that is simply empty")
    target, reason = src.failures[0]
    assert target, "the miss must name what could not be read"
    assert reason and ":" in reason, (
        f"the reason must name the exception type, got {reason!r}")


def test_failures_reset_between_runs():
    """The near-miss: a stale failure from a previous call would make a later healthy
    run look broken, which is the same lie in the other direction."""
    src = WebSource(url="http://127.0.0.1:9/nope")
    list(src.iter_documents())
    assert src.failures
    src.urls = []                      # nothing to fetch, nothing to fail
    list(src.iter_documents())
    assert src.failures == [], "failures carried over from the previous run"


def test_a_reachable_but_empty_source_records_no_failure(monkeypatch):
    """The other near-miss, and the one that keeps the signal worth reading: an empty
    source is NOT a failure. If everything reported a miss, the field would be noise."""
    import contextlake.kb.sources.web as webmod

    class _Resp:
        headers = type("H", (), {"get_content_charset": staticmethod(lambda: "utf-8")})()

        def read(self):
            return b"<html><body></body></html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(webmod, "url_is_fetchable", lambda *a, **k: True)
    monkeypatch.setattr(webmod.urllib.request, "urlopen", lambda *a, **k: _Resp())

    src = WebSource(url="http://example.invalid/empty")
    assert list(src.iter_documents()) == []
    assert src.failures == [], "an empty page was reported as a failure"


# --- the escape hatch the release note promises must actually exist -----------------

def test_the_exit_zero_flag_is_a_pre_command_global_and_the_message_says_so():
    """The CHANGELOG promises `--exit-zero-on-partial` as the one-flag escape, so the
    flag has to work AND the message has to show where it goes.

    It is a pre-command global: `contextlake --exit-zero-on-partial kb ingest` exits 0,
    while `kb ingest --exit-zero-on-partial` is rejected by argparse with exit 2 -- a
    worse outcome than the exit code the user was trying to suppress. Naming the flag
    bare, as the first version of the message did, sends people to the broken form.
    """
    import inspect

    from contextlake.kb.cmds import ingest

    src = inspect.getsource(ingest.cmd_ingest)
    assert "contextlake --exit-zero-on-partial kb ingest" in src, (
        "the message names the flag without its position, so a user will type it after "
        "the subcommand and get an argparse error")
