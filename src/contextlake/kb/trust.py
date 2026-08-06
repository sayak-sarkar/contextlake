"""Which config files are allowed to set keys that become a subprocess argv.

The incident this encodes: ``load_kb_config`` finds ``.contextlake.kb.toml`` by
walking cwd up to the filesystem root (``find_ancestor_config``), so merely
``cd``-ing into an untrusted checkout was enough to hand it ``[llm] provider =
"cli"`` + ``command = "/bin/sh"`` + ``args`` -- which ``kb/llm/cli.py`` passes
straight to ``subprocess.run`` on the next ``kb wiki`` / ``kb enrich`` /
``dashboard --llm-chat``. contextlake itself clones repositories into the
workspace (``mirror sync``, and the dashboard's ``add_repo`` mutation), so the
tool could plant the file itself. No user action beyond "work in this directory"
was required.

The gate is **provenance, not content**: a config file is privileged only when
the user named it (``--config``) or it is their own ``~/.contextlake/kb.toml``.
A file found by directory search is never privileged, and the few keys that
reach an argv are dropped from it with a WARNING naming the file and the key.

Only those keys are gated -- the file is *not* distrusted wholesale. A
project-local ``store_dir``, ``languages``, ``max_file_bytes``, ``[embeddings]``,
``[[rules]]``, or ``[llm] provider = "ollama"`` all keep working exactly as
before; directory-scoped config is the feature, and blanket-ignoring the file
would have broken it to fix a much narrower hole.

Deliberately **not** a trust registry (``contextlake trust <path>`` +
``~/.contextlake/trusted.json``): that is a new top-level CLI surface belonging
to neither the ``mirror`` nor the ``kb`` namespace, and a separate product
decision. This gate closes the hole on its own, and the escape hatch already
exists -- ``--config PATH`` is the user saying "I meant this file".
"""

from __future__ import annotations

import os

from ..config import expand_path

# --- the argv-reaching keys ------------------------------------------------
#
# [llm] command/args -> llm/base.py build_llm() -> CliLlm -> subprocess.run().
# `args` is gated exactly as hard as `command`: paired with the default command
# ("claude") an attacker-supplied args list is still attacker-controlled argv on
# a real, installed CLI.
_EXECUTABLE_LLM_KEYS = frozenset({"command", "args"})

# provider / review_provider only matter when they select the one provider that
# spawns a process. Every other provider talks HTTP and never execs anything, so
# a project-local `provider = "ollama"` must keep working -- gating the key
# unconditionally would break ordinary directory-scoped config for no security
# gain. review_provider is included for consistency (build_review_llm reaches
# the same CliLlm); with command/args already dropped it can only ever resolve
# to the vetted `claude -p --safe-mode` preset, never attacker code.
_PROVIDER_KEYS = frozenset({"provider", "review_provider"})
_ARGV_PROVIDER = "cli"

# [[sources]] type="mcp" spawns its server over stdio (sources/mcp.py ->
# StdioServerParameters -> stdio_client), and connectors/mcp_query.py does the
# same for tool queries, so `command`/`args` under a source are the identical
# hole in a different table; the Figma/Slack connectors spell theirs
# `mcp_command` (connectors/orchestrate.py). `url` and `env` are deliberately
# left alone: `url` is an HTTP endpoint (an exfiltration/SSRF question, a
# different class of problem than this one), and `env` cannot spawn anything
# once `command` is gone.
#
# "`url` is an HTTP endpoint" is now enforced rather than assumed: the ingest
# fetchers allowlist http/https (`sources/base.py:url_is_fetchable`). It was not,
# and `file:///…` in a discovered config read local files into the graph -- so the
# sentence above described an intent the code did not implement. Left ungated here
# on purpose: gating `url` would break ordinary directory-scoped web sources, and
# the scheme allowlist closes the disclosure half at the fetch boundary instead.
EXECUTABLE_SOURCE_KEYS = frozenset({"command", "args", "mcp_command"})


def is_executable_key(table: str, key: str, value: object) -> bool:
    """True if ``[table] key = value`` would end up in a ``subprocess`` argv.

    Scalar tables only; ``[[sources]]`` entries are dicts inside a list and are
    screened with :data:`EXECUTABLE_SOURCE_KEYS` instead.
    """
    if table != "llm":
        return False
    if key in _EXECUTABLE_LLM_KEYS:
        return True
    return key in _PROVIDER_KEYS and value == _ARGV_PROVIDER


def is_privileged_source(path: str | None, explicit_config_path: str | None,
                         *, global_config: str | None = None) -> bool:
    """True if ``path`` is a config file the user *chose*, not one merely found.

    Privileged: the global ``~/.contextlake/kb.toml``, and whatever the user
    passed to ``--config``. Everything else -- in practice the ancestor-discovered
    ``.contextlake.kb.toml`` -- is not.

    Note that ``--config .contextlake.kb.toml`` from inside an untrusted checkout
    *is* privileged. That is intended, not a bypass: naming the file on the
    command line is the explicit act this gate asks for. The hole being closed is
    a file taking effect without the user ever mentioning it.

    ``global_config`` defaults to ``kb.config.GLOBAL_CONFIG`` read at call time
    (not import time) so the module-attribute monkeypatch the config tests use to
    isolate the global tier still takes effect.
    """
    if not path:
        return False
    if global_config is None:
        from . import config as _kb_config  # late: kb.config imports this module

        global_config = _kb_config.GLOBAL_CONFIG
    return _same_file(path, global_config) or _same_file(path, explicit_config_path)


def _same_file(a: str | None, b: str | None) -> bool:
    """Whether two config paths name the same file.

    Both sides get ``~``/``$VAR`` expansion and ``realpath``: ``GLOBAL_CONFIG`` is
    stored unexpanded (``~/.contextlake/kb.toml``), the discovered local path is
    already absolute, and ``--config`` is whatever the user typed (relative,
    ``./x``, a symlink). A raw string compare would miss a legitimate match --
    and a security gate that fails open on a formatting difference is not a gate.
    """
    if not a or not b:
        return False
    return os.path.realpath(expand_path(a)) == os.path.realpath(expand_path(b))
