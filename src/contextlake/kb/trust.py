"""Which config files are allowed to set keys that run a program, choose an endpoint,
or name a secret.

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
A file found by directory search is never privileged. The keys that decide what
runs, where a request goes, which environment variable holds its credential,
where an OAuth refresh token is written, and where the built-in models are
downloaded are dropped from it with a WARNING naming the file and the key. Two
keys are handled some other way, because dropping them leaves the wrong value
in place rather than no value: ``[[sources]] scopes`` may narrow the OAuth
grant and may not widen it, and ``[llm]``/``[embeddings]`` ``provider`` naming
one of the vendors that receive an API credential switches that tier off for
the run (see :data:`REFUSE_DISCOVERED_CREDENTIAL_PROVIDER`). ``[llm]
review_provider`` naming one of those vendors is dropped like any other key:
the council then reviews with the generator.

Only those keys are gated -- the file is *not* distrusted wholesale. A
project-local ``store_dir``, ``languages``, ``max_file_bytes``,
``[embeddings] provider``, ``[[rules]]``, or ``[llm] provider = "ollama"`` all
keep working exactly as before; directory-scoped config is the feature, and
blanket-ignoring the file would have broken it to fix a much narrower hole.

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

# --- the egress keys -------------------------------------------------------
#
# base_url and api_key_env decide where a request goes and which environment
# variable is read for its credential. Neither runs a program, and both were
# ungated for that reason, which left [embeddings] with no gate at all.
#
# base_url redirects a provider that is otherwise local. `[embeddings]` is
# enabled by default with `provider = "auto"` (kb/config.py), and
# `_resolve_auto_embedder` (embeddings/base.py) hands cfg.base_url to
# ollama_reachable, then to ollama_has_model, then to the OllamaEmbedder it
# builds, so one planted `base_url` line, with no provider line, points the
# default configuration at a host of the file author's choosing. `bootstrap`
# runs `kb embed` as a stage, so no opt-in stands between a clone and that.
# `[llm]` is the same key on prompt text: `provider = "ollama"` plus a planted
# base_url posts every wiki prompt, repository content included, to that host.
#
# api_key_env names any variable in the environment. The OpenAI and Anthropic
# clients put its value into an Authorization header on that same request
# (embeddings/openai.py, llm/openai.py, llm/anthropic.py), so a planted pair is
# an endpoint and a secret to send to it.
EGRESS_KEYS = frozenset({"base_url", "api_key_env"})
EGRESS_TABLES = frozenset({"llm", "embeddings"})

# --- the model-download key ------------------------------------------------
#
# `cache_dir` is where the built-in embedder and the built-in LLM put their
# models. Both loaders set HF_HOME to it for the rest of the process unless the
# user already set one (embeddings/builtin.py and llm/builtin.py `_ensure_model`,
# `os.environ.setdefault`), the fastembed engine reads and writes its model folders under it
# (embeddings/onnx_bge.py), and the download then writes `models--<repo>` folders under it
# (model_cache.py).
# It runs nothing and sends nothing, so it was ungated, and a discovered file
# could choose a folder this tool writes into. The mirror config gates the same
# idea in `config.CACHE_LOCATION_KEYS`.
#
# Dropping it is enough, with no tier-off: the loaders fall back to
# `~/.contextlake/models` (DEFAULT_CACHE_DIR), which is the user's own default
# and carries no credential.
MODEL_CACHE_KEYS = frozenset({"cache_dir"})

# The providers whose clients read api_key_env out of the environment and put
# its value into an auth header: embeddings/openai.py and llm/openai.py write
# Authorization: Bearer, llm/anthropic.py writes x-api-key. Every other
# provider (ollama, builtin, auto, cli) sends no credential, so a refused
# egress key there needs no credential action.
#
# Read by kb/config.py: dropping a key is not enough on its own, because the
# dropped value is then filled from a built-in default the user never chose.
# Dropping `api_key_env = "PROJECT_KEY"` falls back to OPENAI_API_KEY, which
# sends a BROADER secret than the file asked for; dropping
# `base_url = "http://127.0.0.1:1234/v1"` sends OPENAI_API_KEY to
# api.openai.com from a file that asked for loopback. So when a discovered
# file both chose one of these providers and had an egress key refused, the
# tier is off for the run (kb/config.py writes `enabled = False`).
CREDENTIAL_PROVIDERS = frozenset({"openai", "anthropic"})

# provider / review_provider are gated for the one value that spawns a process.
# "Every other provider talks HTTP and never execs anything" used to be the
# whole reason, and it left the HTTP itself ungated. The destination and the
# credential name are EGRESS_KEYS above, gated for [llm] and [embeddings]
# alike.
#
# What a refused base_url leaves a discovered `provider` reaching, measured:
# not "the loopback default and nothing else". The merge is key-by-key
# (kb/config.py), so the provider it names resolves against whatever base_url
# survived the merge -- the per-provider default (llm/base.py and
# embeddings/base.py `default_base_url`), or a base_url a privileged file set
# for a DIFFERENT provider. The bound is "a host the operator's own config
# named", not "loopback". A project-local `provider = "ollama"` keeps working
# either way, which is what this stays ungated for.
# review_provider needs no endpoint gate: build_review_llm re-resolves base_url
# and api_key_env to None (llm/base.py), so it cannot inherit a planted endpoint
# or key name, and with command/args dropped a `cli` reviewer can only reach the
# vetted `claude -p --safe-mode` preset. It does need a gate on the vendor it
# names. The vendor defaults apply, so `review_provider = "openai"` builds an
# OpenAILlm at api.openai.com that reads OPENAI_API_KEY, and every council call
# posts the source facts (the ownership section included) and the draft page to
# it. requires_privileged_source refuses that value from a discovered file.
#
# `provider` is NOT handled by dropping the key when it names a remote provider
# that carries a credential. Dropping `provider` alone leaves the tier aimed by
# whatever provider survived the merge, which is a different value the file's
# author did not pick. That case is handled by
# REFUSE_DISCOVERED_CREDENTIAL_PROVIDER below, which switches the whole tier off
# for the run. `review_provider` is the exception, and dropping it is enough:
# with no review_provider the council reviews with the generator, and the
# generator is a provider that sends no credential, one a file the user named
# chose, or a tier that is already off.
# It cannot be a post-merge tier-off, for two reasons. A privileged
# `review_provider = "anthropic"` would be lost with the discovered value. And
# `--llm PROVIDER` (apply_llm_overrides) turns a refused tier back on without
# touching review_provider, so a planted value would return under the flag.
_PROVIDER_KEYS = frozenset({"provider", "review_provider"})
_ARGV_PROVIDER = "cli"
# The value is normalised before the compare (see _provider_value) because the
# code this gate guards normalises too: build_llm (llm/base.py) and
# build_review_llm both lower() the provider before dispatch, so an exact-string
# gate let `provider = "CLI"` through and still built a CliLlm.

# The rule this module states, applied to the key that aims a tier: a config
# file found by directory walk may not aim a credential-carrying tier it also
# chose. When a non-privileged file wins `provider` for [llm] or [embeddings]
# and the winning value is in CREDENTIAL_PROVIDERS, kb/config.py writes
# `enabled = False` into the merged table for the run.
#
# One switch, so the decision can be reverted in one edit. Both settings cost
# something, and the cost is written here in both directions:
#
# True (the current setting). Cost: an honest project-local
# .contextlake.kb.toml that names `provider = "openai"` or
# `provider = "anthropic"` stops working. Its author moves that setting into
# ~/.contextlake/kb.toml, or passes `--config PATH` to name the file. The same
# happens when the project-local file repeats the provider the global config
# already set: provenance is last-wins on the `provider` key, so a repeat still
# wins it, and the two values are never compared. A project-local
# `[llm] review_provider = "openai"` or `"anthropic"` is dropped the same way,
# and its author moves it to ~/.contextlake/kb.toml or passes `--config PATH`.
#
# False. The behaviour before this constant: a tier goes off only when the same
# file ALSO had base_url or api_key_env refused. Cost: a discovered file that
# sets `provider` alone has nothing refused, so it picks the vendor that
# receives the operator's repository content and API quota, from a file the
# operator never named. With `enabled = true` on the line above it does more
# than pick a vendor for a tier the operator switched on -- LlmCfg.enabled
# defaults to False, so the file turns the tier ON and names the vendor.
# Reproduced on the tree before this switch existed: a discovered
# `[llm] enabled = true` + `provider = "openai"` built an OpenAILlm at
# https://api.openai.com/v1 reading OPENAI_API_KEY, with an Authorization
# header on the request. The same switch covers `review_provider`: False keeps
# a discovered `review_provider = "openai"`, which then picks the vendor that
# receives the source facts and the draft page of every council review.
#
# Local providers are untouched at either setting. ollama, builtin, auto and
# cli are not in CREDENTIAL_PROVIDERS, so directory-scoped config keeps working
# for every provider value that sends no credential.
REFUSE_DISCOVERED_CREDENTIAL_PROVIDER = True

# --- the [[sources]] keys --------------------------------------------------
#
# Four capability classes, not one. The name was EXECUTABLE_SOURCE_KEYS while
# it held the first class only, and reasoning from that name is how the other
# three stayed open: each was read as "not argv, so not this gate's problem".
#
# argv -- `command` / `args` / `mcp_command`. [[sources]] type="mcp" spawns its
# server over stdio (sources/mcp.py -> StdioServerParameters -> stdio_client),
# and connectors/mcp_query.py does the same for tool queries, so these are the
# [llm] command hole in a different table. The Figma/Slack connectors spell
# theirs `mcp_command` (connectors/orchestrate.py).
#
# endpoint -- `mcp`. connectors/orchestrate.py passes it as the mcp_url the
# `npx mcp-remote` OAuth bridge is pointed at, so a discovered file chose the
# host that receives the OAuth flow and every tool call on it. `kb connect` is
# a bootstrap stage, so no opt-in stands between a clone and that. This is the
# [llm]/[embeddings] base_url key under another name; it is NOT the `url` key
# below.
#
# credential name -- `token_env`. sources/api.py `_headers` and
# sources/graphql.py `_fetch` read `os.environ.get(token_env)` into an
# `Authorization: Bearer` header on the request they make, so a discovered
# entry naming both the variable and the host it is sent to is the same pair
# EGRESS_KEYS closed for [llm]/[embeddings], on the third table. Refusing it
# costs an honest directory-scoped api/graphql source its header, and costs it
# nothing else: both sites guard on `if self.token_env`, and both constructors
# default it to None, so the fetch degrades to unauthenticated rather than
# crashing. Asserted on the built source in tests/kb/test_config_trust.py.
#
# token store -- `auth_dir`. connectors/orchestrate.py hands it to the
# connector, which exports it as MCP_REMOTE_CONFIG_DIR (connectors/
# atlassian.py, figma.py, slack.py `_spawn`), which is the directory
# mcp-remote writes the OAuth refresh token into. An honest endpoint and
# honest scopes still hand the refreshable grant to a path the file chose.
#
# `env` stays ungated: it cannot spawn anything once `command` is gone.
#
# `url` stays ungated, deliberately, and it is a different key from `mcp`.
# Gating it would break ordinary directory-scoped web sources, which is the
# feature. Its compensating control is the ingest fetchers' http/https scheme
# allowlist (`sources/base.py:url_is_fetchable`), added because `file:///…` in
# a discovered config read local files into the graph. That control covers the
# INGEST FETCH boundary only, not the whole table: connectors/mcp_query.py
# passes `url` to mcp_client.call_tool, which connects to it over
# streamable-HTTP without consulting url_is_fetchable, so a discovered
# `[[sources]] url` still chooses that transport's host. The connector spawn
# reaches its host through `mcp`, which this set now gates.
# Split by class rather than written as one flat set, because kb/config.py
# picks the refusal sentence from the class. The generic sentence says the
# key "runs a program", which is true of three of these six and sends the
# reader of the other three hunting an exec that is not there.
SOURCE_ARGV_KEYS = frozenset({"command", "args", "mcp_command"})
# `search_url` is gated where `url` is not. `url` is a page an ingest READS; `search_url`
# is where `kb enrich` SENDS search terms built from the repo's own symbol names, so a
# discovered file setting it would choose a host that receives code-derived data.
SOURCE_EGRESS_KEYS = frozenset({"mcp", "token_env", "search_url"})
# `auth` and `user` join `auth_dir` here rather than being left ungated. Neither is a
# secret, and that is the point: they decide how the secret named by `token_env` is
# PRESENTED, so a discovered file could flip a bearer token into the username half of a
# Basic pair and send it somewhere it was never meant to go. `token_env` is already
# gated one line above; gating only the secret's NAME and not the scheme that spends it
# would be a read/write pair with one half done.
SOURCE_AUTH_KEYS = frozenset({"auth_dir", "auth", "user"})
PRIVILEGED_SOURCE_KEYS = SOURCE_ARGV_KEYS | SOURCE_EGRESS_KEYS | SOURCE_AUTH_KEYS

# `scopes` is strengthen-only rather than gated, matching what kb/config.py
# does with `[kb] anonymize`. It overrides the read-only OAuth scope the
# Atlassian connector asks for (connectors/atlassian.py DEFAULT_SCOPES), so a
# discovered file could widen the grant it is about to obtain. Narrowing is an
# honest thing for a project-local file to do and keeps working; anything not
# a subset of DEFAULT_SCOPES is refused and falls back to it.
SCOPE_KEY = "scopes"

# Keys are matched by exact name. A mis-cased one (`Command`, `Provider`) is not
# dropped and survives into LlmCfg.model_extra / SourceCfg.model_extra, because
# both models set extra="allow" for connector-specific keys (config.py).
# Nothing reads it: build_llm reads `cfg.command` (llm/base.py),
# _build_builtin_llm reads `cfg.cache_dir` (llm/base.py), the connectors read
# `extra.get("mcp_command")` (connectors/orchestrate.py), and MCPSource.__init__
# drops unknown kwargs into `**_` (sources/mcp.py). Every one is an exact
# lowercase lookup.
# That is a property of the readers, not of this gate, so it has to be rechecked
# when one changes. Match keys case-insensitively here if LlmCfg or SourceCfg
# gains populate_by_name or an alias generator, or if any reader lowercases a key
# before looking it up.
# A key allowlist was rejected: `cache_dir` reaches the built-in LLM as an
# undeclared extra (gated by name in MODEL_CACHE_KEYS), so an allowlist would
# have to name every present and future extras reader, and a missing entry
# would drop an honest key with no signal.


def requires_privileged_source(table: str, key: str, value: object) -> bool:
    """True if ``[table] key = value`` may be set only from a config file the user named.

    Five things a discovered file must not decide: the argv of a program
    contextlake runs (``[llm] command``/``args``, and ``provider = "cli"``), the
    endpoint a request goes to (``base_url``), the environment variable read for
    that request's credential (``api_key_env``), the vendor the council reviewer
    sends that credential to (``[llm] review_provider`` naming a member of
    :data:`CREDENTIAL_PROVIDERS`, while :data:`REFUSE_DISCOVERED_CREDENTIAL_PROVIDER`
    is set), and the folder the built-in models are downloaded into
    (``cache_dir``, see :data:`MODEL_CACHE_KEYS`).

    A ``False`` return means any discovered file may set the key. That makes this a
    denylist, and both ``[llm]`` and ``[embeddings]`` declare ``extra="allow"``
    (kb/config.py), so a connector key added later that names a URL, an env var or a
    folder it writes to is ungated until it is added to :data:`EGRESS_KEYS` or
    :data:`MODEL_CACHE_KEYS`.

    Scalar tables only; ``[[sources]]`` entries are dicts inside a list and are
    screened with :data:`PRIVILEGED_SOURCE_KEYS` and :func:`scopes_widen` instead.
    """
    if table in EGRESS_TABLES and (key in EGRESS_KEYS or key in MODEL_CACHE_KEYS):
        return True
    if table != "llm":
        return False
    if key in _EXECUTABLE_LLM_KEYS:
        return True
    if (key == "review_provider" and REFUSE_DISCOVERED_CREDENTIAL_PROVIDER
            and _provider_value(value) in CREDENTIAL_PROVIDERS):
        return True
    return key in _PROVIDER_KEYS and _provider_value(value) == _ARGV_PROVIDER


def scopes_widen(value: object) -> bool:
    """True if a ``[[sources]] scopes`` value asks for more than the read-only default.

    Compared as exact whitespace-separated tokens against
    ``connectors/atlassian.DEFAULT_SCOPES``. No lowercasing: OAuth scope tokens
    are case-sensitive, so a differently-cased token is a different scope and
    refusing it is the correct answer, not a false positive.

    Fails closed. A non-string value (a list, a number) is refused, because the
    connector does ``(scopes or DEFAULT_SCOPES).strip()`` and a value this
    function cannot read as a token set is one it cannot prove narrows.

    Imported late: ``connectors.atlassian`` reaches ``kb.mcp_client``, and this
    module is imported by ``kb.config`` at module load.
    """
    from .connectors.atlassian import DEFAULT_SCOPES

    if not isinstance(value, str):
        return True
    return not set(value.split()) <= set(DEFAULT_SCOPES.split())


def _provider_value(value: object) -> str:
    """A provider name normalised at least as hard as ``build_llm`` normalises it.

    ``build_llm``/``build_review_llm`` lowercase before dispatching (llm/base.py),
    so a case-sensitive compare here let ``provider = "CLI"`` through the gate and
    still build a ``CliLlm``. ``strip()`` is more than they do, on purpose: a gate
    narrower than its consumer reopens the moment the consumer's normalisation
    changes, and the cost of the extra width is a warning on a padded value that
    would not have built anything.
    """
    return value.strip().lower() if isinstance(value, str) else ""


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
