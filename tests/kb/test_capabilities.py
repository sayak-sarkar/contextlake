"""One MCP server must never be told about another server's tool inventory.

The diagnostic in :mod:`contextlake.kb.capabilities` names tools out loud: it
tells an operator which tools a server offers now, and which command to run to
repoint a source at one of them. A reader acts on that line, so a line that
confidently names the wrong server's tools is worse than the generic warning it
replaced.

The record is keyed by :func:`contextlake.kb.mcp_client.server_key`. That key
used to stop at the program's basename whenever no argument was URL-shaped, so
two stdio servers run by one program (``uvx``, ``npx``, ``python``) shared one
record and the second source read back the first source's answer, by name.

Nothing here touches the network: the probe is stubbed, and every host, path,
program and source name is synthetic.
"""

from __future__ import annotations

import logging
import pathlib
import re

import pytest

from contextlake.kb import capabilities
from contextlake.kb.mcp_client import ToolInfo, ToolList
from contextlake.kb.resilience import reset_breakers

# Two servers, one program. This is the collision the key has to break.
_PROGRAM = "/usr/bin/uvx"
_ALPHA_ARGS = ["contextlake-mcp-alpha"]
_BETA_ARGS = ["contextlake-mcp-beta"]

_RENAME_SENTENCE = "is not in this MCP server's advertised list any more"


@pytest.fixture(autouse=True)
def _fresh_run():
    """The latch and the breakers are both process-wide, so state them here.

    Without this, a record latched by one test makes the next test's probe count
    read 0 for the wrong reason.
    """
    capabilities.reset_run()
    reset_breakers()
    yield
    capabilities.reset_run()
    reset_breakers()


@pytest.fixture
def logged():
    """Every message the package logger emits, once each.

    `gls_logs` attaches caplog's handler to the package logger while `propagate`
    is also True, so every line lands in the text twice. That is fine for "does
    this phrase appear" and wrong for "how many times" and for "which line
    belongs to which source". A handler of our own receives each emit once.
    """
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("contextlake")
    handler = _Collect(level=logging.INFO)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def _probe_by_argv(monkeypatch, advertised):
    """A probe that answers per stdio argv, and records every run.

    Answering per argv is what makes the test able to see the collision: if two
    sources share a record, the second one never asks and reads the first
    server's list.
    """
    calls: list[tuple[str, ...]] = []

    def _probe(*, command=None, args=(), url=None, env=None, timeout=8):
        key = tuple(str(a) for a in args)
        calls.append(key)
        names = advertised[key]
        return (capabilities.OK,
                ToolList(tools=[ToolInfo(n, "", {}) for n in names], truncated=False), "")

    monkeypatch.setattr(capabilities, "probe_tools", _probe)
    return calls


def _one_line(logged, needle):
    """The single logged line mentioning ``needle``."""
    hits = [m for m in logged if needle in m]
    assert len(hits) == 1, f"expected one line naming {needle!r}, got {hits}"
    return hits[0]


def test_one_program_two_servers_are_not_told_about_each_others_tools(monkeypatch, logged):
    """The damage, asserted on the line a user reads.

    Two stdio sources run by the same program, with different arguments and
    different tools. Each diagnostic must name its own server's tools and only
    those. Before the key carried the argv, the second source replayed the
    first's record and the line named the wrong inventory.
    """
    calls = _probe_by_argv(monkeypatch, {
        tuple(_ALPHA_ARGS): ["alpha_find"],
        tuple(_BETA_ARGS): ["beta_find"],
    })

    capabilities.explain_tool_failure(source="alpha-source", tool="search_alpha",
                                      command=_PROGRAM, args=_ALPHA_ARGS)
    capabilities.explain_tool_failure(source="beta-source", tool="search_beta",
                                      command=_PROGRAM, args=_BETA_ARGS)

    assert len(calls) == 2, "the second server replayed the first server's record"
    alpha_line = _one_line(logged, "alpha-source")
    beta_line = _one_line(logged, "beta-source")
    assert _RENAME_SENTENCE in alpha_line and _RENAME_SENTENCE in beta_line
    assert "alpha_find" in alpha_line and "beta_find" not in alpha_line
    assert "beta_find" in beta_line and "alpha_find" not in beta_line


def test_two_sources_on_one_stdio_server_still_share_one_answer(monkeypatch, logged):
    """The other direction: sharper identity must not split one server in two.

    Same program, same argv, same environment is the same spawned process, so it
    is one server and its tool list is one fact. A key that mixed in the source
    *name* would pass the test above and re-probe the same server once per
    source, which is the storm the latch exists to stop.
    """
    calls = _probe_by_argv(monkeypatch, {tuple(_ALPHA_ARGS): ["alpha_find"]})

    for source in ("wiki-a", "wiki-b"):
        capabilities.explain_tool_failure(source=source, tool="search_alpha",
                                          command=_PROGRAM, args=_ALPHA_ARGS)

    assert len(calls) == 1
    assert len([m for m in logged if _RENAME_SENTENCE in m]) == 2, \
        "each source still gets its own line; only the probe is shared"


def test_two_servers_split_by_environment_alone_answer_separately(monkeypatch, logged):
    """The `mcp-proxy` shape: one program, one argv, target host in the env."""
    answers = {"https://alpha.invalid/mcp": ["alpha_find"],
               "https://beta.invalid/mcp": ["beta_find"]}
    calls: list[str] = []

    def _probe(*, command=None, args=(), url=None, env=None, timeout=8):
        target = (env or {})["MCP_TARGET"]
        calls.append(target)
        return (capabilities.OK,
                ToolList(tools=[ToolInfo(n, "", {}) for n in answers[target]],
                         truncated=False), "")

    monkeypatch.setattr(capabilities, "probe_tools", _probe)

    for source, target in (("alpha-source", "https://alpha.invalid/mcp"),
                           ("beta-source", "https://beta.invalid/mcp")):
        capabilities.explain_tool_failure(
            source=source, tool="search", command="/usr/bin/mcp-proxy",
            args=(), env={"MCP_TARGET": target})

    assert len(calls) == 2
    assert "alpha_find" in _one_line(logged, "alpha-source")
    assert "beta_find" in _one_line(logged, "beta-source")


def test_reset_run_is_not_documented_as_a_bound_that_runs():
    """A documented mechanism with no caller reads as a bound that is in force.

    `reset_run` has no caller in `src/`, so the record is process-scoped and
    `MAX_AGE_SECONDS` is the only bound that runs. Both directions in one test:
    while there is no caller the module docstring must disclose that, and if
    `src/` ever gains one the disclosure must go.
    """
    module_path = pathlib.Path(capabilities.__file__).resolve()
    source = module_path.read_text(encoding="utf-8")
    # Guard against a vacuous pattern: prove the regex matches the definition
    # before concluding anything from it matching nothing elsewhere.
    assert re.search(r"\bdef reset_run\s*\(", source), "the pattern below matches nothing"

    src_root = module_path.parents[2]
    callers = sorted(
        str(path.relative_to(src_root))
        for path in src_root.rglob("*.py")
        if path.name != module_path.name
        and re.search(r"\breset_run\s*\(", path.read_text(encoding="utf-8", errors="ignore"))
    )

    doc = (capabilities.__doc__ or "").lower()
    disclosed = "no command in ``src/`` calls" in doc
    if callers:
        assert not disclosed, f"reset_run is now called by {callers}; drop the disclosure"
    else:
        assert disclosed, "reset_run has no caller in src/ and the module docstring must say so"
