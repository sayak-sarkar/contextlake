"""Configuration for the knowledge layer (generic, principle G1).

All deployment-specific facts — which Atlassian/Figma sites, which key->repo
maps, which glossary — live in a user TOML file loaded at runtime, never in this
package. The repo ships only ``examples/kb.toml.example`` with placeholders.

Precedence (later wins): built-in defaults -> ``~/.contextlake/kb.toml`` ->
``.contextlake.kb.toml`` (cwd) -> an explicit ``--config`` path. The ``[kb]``,
``[embeddings]``, and ``[llm]`` tables are deep-merged key-by-key, so a local file
setting only one field (e.g. ``[llm] model = "..."``) does not wipe out sibling
fields (``enabled``, ``provider``) set globally. ``sources`` and ``rules`` lists are
replaced wholesale by the highest-precedence file that sets them (predictable, no
surprise merging of list tables).

One exception cuts across that precedence: the handful of keys that end up in a
``subprocess`` argv (``[llm] command``/``args``/``provider = "cli"``,
``[[sources]] command``/``args``/``mcp_command``), plus the two that decide where
a request goes and which environment variable holds its credential
(``[llm]``/``[embeddings]`` ``base_url`` and ``api_key_env``), are honoured
**only** from the global file or an explicit ``--config`` path, never from the
auto-discovered ``.contextlake.kb.toml`` -- otherwise cloning a hostile repo and
working inside it is remote code execution, and one planted ``base_url`` line
sends every embedding request to a host that file's author chose. See
``kb/trust.py`` for the full rationale, and ``CONTEXTLAKE_NO_LOCAL_CONFIG=1`` to
skip the discovered tier altogether.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..config import ConfigError, expand_path, find_ancestor_config  # noqa: F401 -- re-exported
from ..logging_setup import log
from .trust import (
    CREDENTIAL_PROVIDERS,
    EGRESS_KEYS,
    EGRESS_TABLES,
    PRIVILEGED_SOURCE_KEYS,
    REFUSE_DISCOVERED_CREDENTIAL_PROVIDER,
    SCOPE_KEY,
    SOURCE_AUTH_KEYS,
    SOURCE_EGRESS_KEYS,
    is_privileged_source,
    requires_privileged_source,
    scopes_widen,
)

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

DEFAULT_STORE_DIR = "~/.contextlake/kb"
GLOBAL_CONFIG = "~/.contextlake/kb.toml"
LOCAL_CONFIG = ".contextlake.kb.toml"
# There is deliberately no DEFAULT_LANGUAGES list any more.
#
# It used to be ["csharp", "typescript", "python"], and `languages` defaulted to it --
# but nothing ever passed the value to the parser, so every install indexed all 14
# supported languages regardless. The list therefore described a filter that did not
# exist. Wiring the old default through would have silently stopped indexing c, cpp,
# go, java, javascript, kotlin, php, ruby, rust, scala and tsx for every user who never
# set the key, which is a far worse outcome than the dead setting.
#
# So `languages = None` now means "every language the parser supports", matching the
# behaviour users have always had, and an explicit list finally filters.
# The single source of truth for the indexer's oversize-file cutoff. Decimal
# 5,000,000 (5 MB), not 5 * 1024**2 (5 MiB, 5,242,880 bytes): docs/indexing-the-code-graph.md
# and docs/style-guide-formatting.md both document this knob as "5 MB", and this is
# the user-facing [kb] config value, so it wins over parse.py's binary-unit constant
# of the same name -- kb/parse.py imports this constant rather than defining its own,
# so a file between 5,000,000 and 5,242,880 bytes can no longer be skipped or parsed
# depending on which entry point (CLI config vs. a direct index_repo_dir call) it
# came through.
DEFAULT_MAX_FILE_BYTES = 5_000_000

#: Per-REPOSITORY estimated-memory budget, the companion to the per-FILE cap
#: above. A per-file cap cannot bound a repository that is wide rather than
#: deep: the tree that took a 15.4 GB machine down held 1,432 XML files whose
#: LARGEST was 3.57 MB, so `max_file_bytes` never fired once while the aggregate
#: reached 671 MB.
#:
#: 3 GB is taken from the fleet, not chosen: across 660 real repositories the
#: median estimate is ~0, p95 is 0.35 GB and p99 is 1.69 GB, then three
#: outliers sit at 6.09, 6.76 and 7.35 GB with a clear gap beneath them. 3 GB
#: refuses exactly those three.
#:
#: This bounds ONE repository. Peak for a run is this times the worker count,
#: so a smaller machine lowers this, `index_workers`, or both.
DEFAULT_MAX_REPO_MEMORY = 3 * 1024 ** 3


def default_store_dir() -> str:
    """The default knowledge-store location."""
    return DEFAULT_STORE_DIR


class SourceCfg(BaseModel):
    """A knowledge-source connector (Atlassian, Figma, …). Extra keys allowed so
    connector-specific options survive without a schema change."""

    model_config = ConfigDict(extra="allow")
    type: str
    name: str
    mcp: str | None = None
    enabled: bool = True
    # Generic MCP tool-calling (see kb/connectors/mcp_query.py): the search tool
    # name on the configured MCP server, and a dict of arguments to call it with
    # ({terms}/{query} placeholders get the caller's search terms templated in).
    tool: str | None = None
    arg_template: dict | None = None


class RuleCfg(BaseModel):
    """An association rule (branch_key, key_map, link_scrape, dependency, …)."""

    model_config = ConfigDict(extra="allow")
    type: str
    pattern: str | None = None
    file: str | None = None


class EmbeddingsCfg(BaseModel):
    """Semantic-search tier. Local-first and **on by default**; connector-specific
    keys are allowed so providers can carry extra options without a schema bump.

    On by default since 7.7.0. It was opt-in, and the effect was that a new user's
    natural-language questions returned nothing at all: measured on a real 39-repo
    store, every purely conceptual question came back empty, and the same questions
    answered correctly once vectors existed (name recall 0.375 -> 0.625). "Ask it in
    English" is most of what this product promises, and it was switched off.

    The cost is one embedding pass, and it is small: 7,719 vectors in ~1.7s on CPU with
    the built-in model. It is NOT paid at index time -- `kb index` does not embed;
    `kb embed` does, and `bootstrap` runs it as a stage -- so turning this on does not
    slow the command people run most.

    Local-first is unchanged. `provider = "auto"` still resolves to a local Ollama, then
    the built-in CPU embedder, and otherwise embeds nothing rather than reaching for a
    network service. With no embedder installed the run says exactly that and names the
    one-line fix (see `_embed_unavailable_hint`), which is the point of flipping the
    default rather than a reason against it: the failure is now loud instead of silent.
    """

    model_config = ConfigDict(extra="allow")
    enabled: bool = True
    # auto | ollama | openai | builtin. "auto" resolves to a reachable local
    # Ollama, else the built-in CPU embedder (needs the `kb-local` extra), else
    # skips. Extra keys (engine, cache_dir, model_revision) ride along via extra="allow".
    provider: str = "auto"
    model: str | None = None
    # the API endpoint. None means "not explicitly set" -> resolved per-provider
    # at read time (see embeddings.base.default_base_url), the same shape LlmCfg
    # already uses below and for the same measured reason: one declared literal
    # wins for every provider, so `provider = "openai"` with no base_url line
    # POSTed to the local Ollama port with the OPENAI_API_KEY value in the
    # Authorization header. api_key_env keeps its declared default because
    # OPENAI_API_KEY is right for the one provider that reads it here.
    base_url: str | None = None
    batch_size: int = 64
    vector_backend: str = "auto"  # auto | sqlite-vec | brute
    # sqlite-vec only: the vec0 chunk size (vectors scanned per chunk during KNN).
    # Larger values trade memory for fewer chunk boundaries on big stores; the
    # default suits most. Clamped to a multiple of 8; applied when the vector table
    # is first created (re-embed from scratch to change an existing store).
    vector_chunk_size: int = 1024
    api_key_env: str = "OPENAI_API_KEY"  # env var holding the key (never the key itself)


class LlmCfg(BaseModel):
    """Local-first LLM tier for the curated wiki and its verification council.
    Off by default; connector-specific keys allowed."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    # auto | ollama | openai | builtin | anthropic | cli. "auto" resolves to a reachable local
    # Ollama, else the built-in CPU LLM (needs the `llm-local` extra), else skips.
    provider: str = "auto"
    model: str | None = None
    # the API endpoint. None means "not explicitly set" -> resolved per-provider
    # at read time (see llm.base.default_base_url), for the same reason as
    # api_key_env below. A declared default can't work here: one literal wins for
    # every provider, so an unset base_url used to send provider = "anthropic"
    # traffic to the local Ollama port.
    base_url: str | None = None
    council_size: int = 3
    accept_score: float = 0.7
    # env var holding the key (never the key itself). None means "not explicitly
    # set" -> resolved per-provider at read time (see llm.base.default_api_key_env).
    # A model_validator can't do this defaulting: apply_llm_overrides() (the
    # `--llm PROVIDER` CLI flag path) sets provider by plain attribute assignment
    # on an already-constructed LlmCfg, and pydantic v2 does not re-run
    # validators on assignment (no validate_assignment=True), so a
    # construction-time default would silently keep the wrong provider's env var.
    api_key_env: str | None = None
    max_tokens: int = 4096  # anthropic/openai response cap; wiki pages are short
    # Seconds to wait for one generation. Declared rather than left to
    # `extra="allow"`, which is how it previously reached the client: it worked, but
    # it was invisible to the config docs and to validation, so nobody raising a
    # local-model budget could discover it. 300s is generous for a hosted model and
    # tight for a CPU-only local one -- see OllamaLlm's timeout message.
    timeout: float = 300.0
    command: str | None = None  # provider="cli": the agent CLI to invoke
    args: list[str] | None = None  # provider="cli": override the per-CLI preset args
    # Council reviewer override. Unset (the default) means the council reviews with
    # the very same client that generated the page -- exactly the historical
    # behavior. Set it to gate a cheap local generator with a stronger judge, e.g.
    # provider = "builtin" + review_provider = "anthropic". Opt-in on purpose: it
    # costs pages x council_size extra calls against the review provider, so it is
    # never inferred from a stray API key in the environment.
    review_provider: str | None = None
    review_model: str | None = None


class KbConfig(BaseModel):
    store_dir: str = DEFAULT_STORE_DIR
    # None (the default) means every supported language; a list restricts to it.
    languages: list[str] | None = None
    # Indexing scope. Skip machine-generated/derived files (designer.cs, *.min.js,
    # @generated headers, …) — graph noise derived from real sources — and code
    # files larger than max_file_bytes (data blobs / vendored bundles). Both are
    # logged, never silent. Set skip_generated=false / raise max_file_bytes to index them.
    skip_generated: bool = True
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    # Estimated peak memory one repository may cost before it is refused,
    # checked BEFORE any file is parsed. 0 disables the check.
    max_repo_memory: int = DEFAULT_MAX_REPO_MEMORY
    # Parallel workers for the per-repo parse (CPU-bound). None -> auto (cpu-1,
    # capped at 8). Set 1 to force serial.
    index_workers: int | None = None
    # Whether the dashboard hides contributor identities, external link URLs and
    # free-text prose (README and wiki bodies). "never" (the default) shows them;
    # "always" hides them on both the served dashboard and the `--site` export.
    #
    # An explicit setting rather than an inferred one, decided deliberately. The
    # standing intent was "on when the store holds repos the operator does not own",
    # and NOTHING in the data model can answer that: a `Repo` records id, path, host,
    # branch and commit, and no ownership. Every candidate signal is a guess, and the
    # most obvious one (does the repo id sit inside the configured mirror group)
    # inverts on the case that motivated the rule, since mirroring an organisation you
    # contribute to but do not own puts every repo INSIDE the group. So the operator
    # states their own answer once, and no inference can be wrong.
    #
    # `--anonymize` on the command line still forces it on for that run: a flag says
    # "this invocation", the setting says "this machine", and the stricter wins.
    anonymize: str = "never"
    embeddings: EmbeddingsCfg = Field(default_factory=EmbeddingsCfg)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    sources: list[SourceCfg] = Field(default_factory=list)
    rules: list[RuleCfg] = Field(default_factory=list)
    # Provenance of this config, for surfaces that need to tell "loaded nothing and
    # fell back to defaults" apart from "loaded a file that happens to be empty".
    # Those look identical in the merged result, which is how `doctor` came to print
    # a green "config loads" when no config existed anywhere. Recorded here, in the
    # one function that knows the precedence chain, rather than re-derived by each
    # caller, which would drift the moment the chain changes.
    loaded_from: list[str] = Field(default_factory=list)
    searched: list[str] = Field(default_factory=list)
    # Tables switched off because a config file found by walking up from the
    # current directory chose a credential-carrying provider for them (see
    # load_kb_config). Named for the outcome rather than for one cause: the
    # refusal used to need a refused base_url or api_key_env as well, and the
    # name `egress_refused` then described a tier that is off for a reason no
    # egress key was involved in.
    #
    # Not settable from TOML: KbConfig is built from explicit kwargs, nothing
    # splats `**kb`, and _KB_KEYS warns on an unknown [kb] key. Its reader is
    # `doctor`, which would otherwise tell the user to set `enabled = true` in
    # the file whose provider was refused, where setting it changes nothing.
    refused_tiers: tuple[str, ...] = ()

    @property
    def store_path(self) -> Path:
        return Path(expand_path(self.store_dir))


def _read_toml(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(expand_path(path))
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def load_kb_config(config_path: str | None = None) -> KbConfig:
    """Load and merge KB config from the precedence chain.

    An explicit ``--config`` path that doesn't exist is a hard error, not a silent
    no-op: without this, a typo'd or not-yet-created path falls through to the next
    file in the precedence chain -- typically ``~/.contextlake/kb.toml``, which can
    point at a completely different (possibly production) store than the one the
    caller meant to target. The other, auto-discovered files in the chain are
    legitimately optional and keep silently no-op'ing when absent.

    "Local" is the nearest ancestor directory's ``.contextlake.kb.toml``, walking
    up from cwd to the filesystem root (see ``find_ancestor_config``) -- a
    project-root config every subdirectory underneath it inherits.
    """
    if config_path and not Path(expand_path(config_path)).exists():
        raise ConfigError(
            f"--config path not found: {config_path}\n"
            "Refusing to fall back to the next config in the precedence chain "
            "(~/.contextlake/kb.toml, or the nearest ancestor directory's "
            ".contextlake.kb.toml), which may point at a different store than "
            "the one you meant to use."
        )
    local_config = find_ancestor_config(LOCAL_CONFIG)
    merged: dict = {}
    loaded_from: list[str] = []
    store_dir_src: str | None = None   # which file actually decided the store
    # Described rather than resolved, because the interesting case is the one with
    # nothing to resolve: when no ancestor carries a local config, find_ancestor_config
    # returns None, and simply omitting it would hide the fact that a
    # .contextlake.kb.toml in any parent directory would have been picked up.
    searched: list[str] = [
        str(Path(expand_path(GLOBAL_CONFIG))),
        str(Path(expand_path(local_config))) if local_config
        else f"{LOCAL_CONFIG} (searched this directory and every parent, up to filesystem root)",
    ]
    if config_path:
        searched.append(str(Path(expand_path(config_path))))
    for src in (GLOBAL_CONFIG, local_config, config_path):
        if src and Path(expand_path(src)).exists():
            loaded_from.append(str(Path(expand_path(src))))
    # Which tier had an egress key refused, and whether the provider that WON the
    # merge came from a file the user named. The gate may refuse a value; it may
    # not substitute a built-in default for one. Dropping [llm]
    # api_key_env = "PROJECT_KEY" and letting build_llm fall back to
    # OPENAI_API_KEY sends a broader secret than the file asked for; dropping
    # base_url = "http://127.0.0.1:1234/v1" sends OPENAI_API_KEY to
    # api.openai.com from a config that asked for loopback. Read only on the
    # revert path of REFUSE_DISCOVERED_CREDENTIAL_PROVIDER; see the tier-off
    # loop below.
    egress_refused: set[str] = set()
    provider_privileged: dict[str, bool] = {}
    for src in (GLOBAL_CONFIG, local_config, config_path):
        # Provenance gate. A config file the user never named -- i.e. the one
        # found by walking up from cwd -- may not carry keys that become a
        # subprocess argv, choose an endpoint, or name the environment variable
        # holding a credential: cloning a hostile repo and cd-ing into it was
        # otherwise enough to get code execution on the next LLM-touching
        # command, and to redirect every embedding request off the machine.
        # Everything else in that file still applies. See kb/trust.py.
        privileged = is_privileged_source(src, config_path, global_config=GLOBAL_CONFIG)
        for table, values in _read_toml(src).items():
            if table == "kb" and "store_dir" in values:
                # Remember WHICH file set the store, not just what it was set to. The
                # merge is last-wins, so this ends up naming the file that decided it.
                store_dir_src = str(Path(expand_path(src)))
            if not privileged:
                raw = values
                values = _drop_untrusted_keys(table, values, src)
                if table in EGRESS_TABLES and isinstance(raw, dict) and any(
                        k in EGRESS_KEYS and requires_privileged_source(table, k, v)
                        for k, v in raw.items()):
                    egress_refused.add(table)
            if table in EGRESS_TABLES and isinstance(values, dict) and "provider" in values:
                # Read AFTER the drop and overwritten per source, so this ends up
                # naming the file whose provider WON: a refused `provider = "cli"`
                # never won, and a discovered `provider = "anthropic"` overriding a
                # privileged global did.
                provider_privileged[table] = privileged
            if table in _SCALAR_TABLES:
                # Deep-merge by key: a local file setting only `model` must not
                # wipe out `enabled`/`provider` set globally under the same table.
                merged.setdefault(table, {}).update(values)
            else:
                # sources/rules (and any unrecognised table) stay wholesale-replaced,
                # per the documented precedence rule above.
                merged[table] = values

    # A config file found by directory walk may not aim a credential-carrying
    # tier it also chose. `provider` is the key that aims one, so a
    # non-privileged file winning it for [llm] or [embeddings] with a value in
    # CREDENTIAL_PROVIDERS switches that tier off. A privileged provider is
    # trusted with that provider's own defaults, so a global
    # `provider = "openai"` still builds when a discovered file's narrowing of
    # api_key_env is refused.
    #
    # The `or table in egress_refused` term is what the switch reverts TO, not
    # a second rule. With REFUSE_DISCOVERED_CREDENTIAL_PROVIDER set False the
    # condition is the older three-term one -- a tier goes off only when the
    # same non-privileged file also had base_url or api_key_env refused, where
    # dropping the key alone would have filled it from a default the user never
    # chose (the broad env var, or api.openai.com in place of a loopback
    # base_url). Written this way so flipping the switch reverts the widening
    # and leaves that older refusal standing; a bare `if not <switch>: continue`
    # would take both out.
    #
    # Written into `merged` after the loop, so a planted `enabled = true` cannot
    # survive it, and before the two model constructions below, which are the
    # only ones in src/.
    tiers_off: list[str] = []
    for table in sorted(EGRESS_TABLES):
        provider = (merged.get(table, {}).get("provider") or "").strip().lower()
        if (provider in CREDENTIAL_PROVIDERS
                and not provider_privileged.get(table, False)
                and (REFUSE_DISCOVERED_CREDENTIAL_PROVIDER or table in egress_refused)):
            merged.setdefault(table, {})["enabled"] = False
            tiers_off.append(table)
            _warn_tier_off(table)
    kb = merged.get("kb", {})
    _warn_unknown_config(kb, merged)
    if config_path:
        # The other half of the hard-error above. That one catches a --config that does
        # not EXIST; this one catches a --config that exists and simply does not set the
        # store -- which reaches the identical outcome by a quieter route, because the
        # merge then inherits `store_dir` from ~/.contextlake/kb.toml. Measured during
        # an audit: two commands carrying an explicit --config wrote six repository rows
        # into a production store, and nothing in the output said which store was used.
        #
        # A warning rather than a refusal: a --config that only tunes [embeddings] or
        # [llm] is a legitimate and common thing to write, so refusing would break real
        # usage to prevent a mistake. Naming the file that won is enough to make the
        # mistake visible the moment it happens.
        named = str(Path(expand_path(config_path)))
        if store_dir_src != named:
            from .. import style
            from ..logging_setup import log
            where = store_dir_src or "the built-in default"
            log(style.warn(
                f"{config_path} does not set [kb] store_dir, so the store comes from "
                f"{where}: {Path(expand_path(kb.get('store_dir', default_store_dir())))}"))
            log("  Set [kb] store_dir in that file to target a different store.")
    cfg = KbConfig(
        store_dir=kb.get("store_dir", default_store_dir()),
        languages=kb.get("languages") or None,
        skip_generated=kb.get("skip_generated", True),
        max_file_bytes=kb.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES),
        max_repo_memory=kb.get("max_repo_memory", DEFAULT_MAX_REPO_MEMORY),
        index_workers=kb.get("index_workers", None),
        anonymize=_anonymize_value(kb.get("anonymize")),
        embeddings=EmbeddingsCfg(**merged.get("embeddings", {})),
        llm=LlmCfg(**merged.get("llm", {})),
        sources=[SourceCfg(**s) for s in merged.get("sources", [])],
        rules=[RuleCfg(**r) for r in merged.get("rules", [])],
        loaded_from=loaded_from,
        searched=searched,
        refused_tiers=tuple(tiers_off),
    )
    # Registered here, where the store's location becomes known, rather than where
    # a store is opened. `_open_store` looked like the choke point and is not:
    # `doctor` constructs its own SqliteStore, so `--redact doctor` printed the
    # absolute store path in full. Every kb command resolves its config through
    # this function, including the ones that never open a store, so this is the
    # only place that fires for all of them. add_redactions is additive and
    # order-independent by design, so registering the same rule twice is free.
    from .. import observability
    observability.add_redactions(
        paths=[(cfg.store_path, "<store>")]
        + [(p, "<config>") for p in loaded_from])
    return cfg


# The keys that live under [kb]; the other KbConfig fields come from their own
# top-level tables. A key/table outside these is warned (a silent-ignore, like a
# `store` typo for `store_dir`, is how a whole run lands in the wrong place).
_KB_KEYS = {"store_dir", "languages", "skip_generated", "max_file_bytes", "index_workers",
            "anonymize"}
ANONYMIZE_VALUES = ("never", "always")
# `serve` is here and has NO KbConfig field, which is the point. Its one key,
# `keys_file`, is read by `keyfile._serve_keys_file`, which opens the same TOML
# files itself and applies its own privileged-source gate, so the value never
# passes through this merge. It was missing from the set, and one `kb serve`
# run then printed "unknown config table 'serve' (ignored)" on stdout while
# refusing the start over `[serve] keys_file` on stderr -- two lines, one run,
# opposite claims about the same table. The table is known and the value is
# honoured, so the warning was the wrong line. Keys INSIDE it are not checked
# the way `_KB_KEYS` checks `[kb]`, so a typo there is still silent.
_TABLES = {"kb", "embeddings", "llm", "sources", "rules", "serve"}
# Tables of scalar fields, deep-merged key-by-key across the precedence chain (see
# load_kb_config). sources/rules are list tables and stay wholesale-replaced by design.
_SCALAR_TABLES = {"kb", "embeddings", "llm"}


def _drop_untrusted_keys(table: str, values, source: str):
    """Strip the keys a non-privileged config file may not set out of one of its tables.

    Returns ``values`` unchanged (the same object, no copy) when there is nothing
    to drop, which is every table of every honest config -- the gate costs one
    scan on the normal path.
    """
    if table in _SCALAR_TABLES and isinstance(values, dict):
        dropped = [k for k, v in values.items()
                   if requires_privileged_source(table, k, v)]
        # `[kb] anonymize` may only be STRENGTHENED by a file found by walking up from
        # the current directory. The provenance hole this module documents is not only
        # about running a program: a checkout that ships `anonymize = "never"` turns off
        # the operator's own privacy setting for any dashboard they serve while sitting
        # in that directory, and contextlake clones repositories into the workspace
        # itself. Dropping the key outright would break honest directory-scoped config,
        # so only the weakening direction is refused; "always" from any file is honoured.
        weakens_privacy = (
            table == "kb" and str(values.get("anonymize", "always")).lower() != "always")
        if not dropped and not weakens_privacy:
            return values
        for key in dropped:
            if key in EGRESS_KEYS:
                _warn_untrusted_egress(f"[{table}] {key}", source)
            else:
                _warn_untrusted(f"[{table}] {key}", source)
        if weakens_privacy:
            _warn_untrusted_privacy(source)
            dropped = [*dropped, "anonymize"]
        return {k: v for k, v in values.items() if k not in dropped}
    if table == "sources" and isinstance(values, list):
        # sources is a list table: screen each entry's dict. A source entry decides
        # the same four things a scalar table does -- the argv of a spawned server,
        # the endpoint it is spawned against, the env var holding the token sent to
        # it, and the directory its OAuth refresh token is written to. See trust.py.
        #
        # `scopes` is folded into the same per-entry set rather than checked
        # separately, because the early return below fires on an empty set: a
        # widening `scopes` in an entry with no other gated key would otherwise
        # skip the whole branch and survive. Refused outright rather than
        # rewritten, which is the shape `[kb] anonymize` already uses above --
        # dropping it falls the connector back to DEFAULT_SCOPES.
        cleaned, dropped_keys = [], set()
        for entry in values:
            if isinstance(entry, dict):
                bad = PRIVILEGED_SOURCE_KEYS & entry.keys()
                if SCOPE_KEY in entry and scopes_widen(entry[SCOPE_KEY]):
                    bad = bad | {SCOPE_KEY}
            else:
                bad = set()
            dropped_keys |= bad
            cleaned.append({k: v for k, v in entry.items() if k not in bad} if bad else entry)
        if not dropped_keys:
            return values
        for key in sorted(dropped_keys):
            # One sentence per capability class (see trust.py). The generic one
            # says the key runs a program, which is true of command/args/
            # mcp_command and of nothing else here.
            if key in SOURCE_EGRESS_KEYS:
                _warn_untrusted_egress(f"[[sources]] {key}", source)
            elif key in SOURCE_AUTH_KEYS:
                _warn_untrusted_auth_dir(source)
            elif key == SCOPE_KEY:
                _warn_untrusted_scopes(source)
            else:
                _warn_untrusted(f"[[sources]] {key}", source)
        return cleaned
    return values


# (realpath of the offending file, "[table] key") pairs already reported this
# process. The message is a refusal the user has to act on, so burying it under
# copies of itself defeats it: a three-key block printed six identical warnings,
# because load_kb_config runs more than once per command and each load re-reads
# and re-screens the same file. Keyed on the resolved path, so two different
# untrusted files each setting `[llm] command` still warn once apiece -- "once
# per file" is the contract, not "once ever".
_WARNED_UNTRUSTED: set[tuple[str, str]] = set()


def _warn_untrusted(what: str, source: str) -> None:
    """Report a dropped key loudly and actionably -- never the value, which is
    attacker-supplied text that has no business in a log line. Reported once per
    (file, key); see :data:`_WARNED_UNTRUSTED`."""
    key = (str(Path(expand_path(source)).resolve()) if source else "", what)
    if key in _WARNED_UNTRUSTED:
        return
    _WARNED_UNTRUSTED.add(key)
    log(f"config: ignoring {what} from {source} -- a config file found by walking "
        "up from the current directory may not set keys that run a program. "
        f"Set it in {GLOBAL_CONFIG} instead, or pass `--config {source}` to say "
        "you meant this file. See SECURITY.md, 'Workspace trust'.",
        level=logging.WARNING)


def _warn_untrusted_egress(what: str, source: str) -> None:
    """The same refusal for `base_url` and `api_key_env`, which need their own sentence.

    The generic message says "may not set keys that run a program". Neither of these
    runs anything, so that sentence sends the reader looking for an exec they will
    not find. This one names what was actually refused: the endpoint, and the
    environment variable read for the credential sent to it."""
    key = (str(Path(expand_path(source)).resolve()) if source else "", what)
    if key in _WARNED_UNTRUSTED:
        return
    _WARNED_UNTRUSTED.add(key)
    log(f"config: ignoring {what} from {source} -- a config file found by walking up "
        "from the current directory may not choose where requests are sent, or which "
        "environment variable holds the API key. "
        f"Set it in {GLOBAL_CONFIG} instead, or pass `--config {source}` to say you "
        "meant this file. See SECURITY.md, 'Workspace trust'.",
        level=logging.WARNING)


def _warn_tier_off(table: str) -> None:
    """Report a tier switched off because a file the user never named aimed it.

    The per-key refusal names the key; this names the consequence, which is the
    only thing separating "contextlake refused this" from "you turned it off".

    Every remedy below was run against `load_kb_config` before it was written
    here. Two candidates are absent because they do NOT clear the refusal:

    - `set enabled = true`. That key lives in the discovered file, and the tier
      is switched off after the merge, so setting it there changes nothing.
    - "set [table] in the global config", on its own. The merge is last-wins and
      the discovered file is merged after the global one, so its `provider` line
      still wins and the tier goes off again on the next run. Deleting that line
      is the half that clears it; adding the global block without deleting it is
      the remedy SECURITY.md used to publish, and it produced the identical
      warning a second time.

    Keyed through :data:`_WARNED_UNTRUSTED` on the table alone, so it prints once
    per process rather than once per load_kb_config call.
    """
    ident = ("", f"[{table}] tier-off")
    if ident in _WARNED_UNTRUSTED:
        return
    _WARNED_UNTRUSTED.add(ident)
    # `--llm PROVIDER` is named for [llm] alone because that is the only tier
    # with the flag: apply_llm_overrides runs after the load and sets
    # enabled + provider on the built config (cmds/wiki.py, cmds/docs.py).
    # [embeddings] has no equivalent, so offering it there would be advice that
    # does nothing, which is the defect this message was rewritten to remove.
    by_flag = (" For [llm] only, `--llm PROVIDER` on `kb wiki`, `kb docs` or "
               "`bootstrap` also turns the tier on for that run."
               if table == "llm" else "")
    log(f"config: [{table}] is off for this run. A config file found by walking up "
        "from the current directory chose a provider that sends a credential, and a "
        "file you did not name may not aim a tier that carries your API key. That "
        f"also stops an honest project-local [{table}] block that names a remote "
        "provider, so such a block has to move. Two things clear it: delete the "
        f"[{table}] keys from that file and set them in {GLOBAL_CONFIG}, or pass "
        "`--config PATH` naming that file to say you meant it." + by_flag +
        f" Adding the block to {GLOBAL_CONFIG} while that file keeps its own "
        "`provider` line changes nothing. See SECURITY.md, 'Workspace trust'.",
        level=logging.WARNING)


def _warn_untrusted_auth_dir(source: str) -> None:
    """The refusal for ``[[sources]] auth_dir``, which needs its own sentence.

    It runs nothing and sends nothing. It is the directory ``mcp-remote`` writes
    the OAuth refresh token into (MCP_REMOTE_CONFIG_DIR, see
    connectors/atlassian.py), so what it decides is where a long-lived grant is
    stored -- which is what this says."""
    key = (str(Path(expand_path(source)).resolve()) if source else "", "[[sources]] auth_dir")
    if key in _WARNED_UNTRUSTED:
        return
    _WARNED_UNTRUSTED.add(key)
    log(f"config: ignoring [[sources]] auth_dir from {source} -- a config file found by "
        "walking up from the current directory may not choose the directory the OAuth "
        "refresh token is written to. "
        f"Set it in {GLOBAL_CONFIG} instead, or pass `--config {source}` to say you "
        "meant this file. See SECURITY.md, 'Workspace trust'.",
        level=logging.WARNING)


def _warn_untrusted_scopes(source: str) -> None:
    """The refusal for a widening ``[[sources]] scopes``.

    Strengthen-only, like ``[kb] anonymize``: a narrower scope from any file is
    honoured and never reaches here, so the message has to say the direction or
    it reads as `scopes` being unusable in a project-local file."""
    key = (str(Path(expand_path(source)).resolve()) if source else "", "[[sources]] scopes")
    if key in _WARNED_UNTRUSTED:
        return
    _WARNED_UNTRUSTED.add(key)
    log(f"config: ignoring [[sources]] scopes from {source} -- a config file found by "
        "walking up from the current directory may narrow the OAuth scope contextlake "
        "asks for, not widen it, and this value asks for more than the read-only "
        "default. "
        f"Set it in {GLOBAL_CONFIG} instead, or pass `--config {source}` to say you "
        "meant this file. See SECURITY.md, 'Workspace trust'.",
        level=logging.WARNING)


def _warn_untrusted_privacy(source: str) -> None:
    """The same refusal for `[kb] anonymize`, which needs its own sentence.

    The generic message says "may not set keys that run a program", which is not what
    happened and would send the reader looking for an exec they will not find. This one
    names the direction, because setting it to "always" from the same file is allowed."""
    key = (str(Path(expand_path(source)).resolve()) if source else "", "[kb] anonymize")
    if key in _WARNED_UNTRUSTED:
        return
    _WARNED_UNTRUSTED.add(key)
    log(f"config: ignoring [kb] anonymize from {source} -- a config file found by "
        "walking up from the current directory may turn anonymising ON, but never OFF. "
        f"Set it in {GLOBAL_CONFIG} instead, or pass `--config {source}` to say you "
        "meant this file. See SECURITY.md, 'Workspace trust'.",
        level=logging.WARNING)


def _anonymize_value(raw) -> str:
    """`[kb] anonymize` normalised, with an unrecognised value failing SAFE.

    A typo (`anonymize = "alway"`) resolves to "always", not to the "never" default.
    Deliberate, and the inverse of how every other unknown value here is treated: this
    one guards identities, and a misspelling that quietly showed them would be found by
    the person whose name was on the screen. The warning names the value's own spelling
    so the fix is obvious, and the operator is never worse off than they asked for.
    """
    if raw is None:
        return "never"
    value = str(raw).strip().lower()
    if value in ANONYMIZE_VALUES:
        return value
    log(f"config: [kb] anonymize = {raw!r} is not one of "
        f"{', '.join(ANONYMIZE_VALUES)}; anonymising ANYWAY, since an unreadable "
        "privacy setting must not be read as permission to show identities.",
        level=logging.WARNING)
    return "always"


def _warn_unknown_config(kb: dict, merged: dict) -> None:
    for k in kb:
        if k not in _KB_KEYS:
            log(f"config: unknown [kb] key {k!r} (ignored)", level=logging.WARNING)
    for t in merged:
        if t not in _TABLES:
            log(f"config: unknown config table {t!r} (ignored)", level=logging.WARNING)


def apply_llm_overrides(cfg: KbConfig, *, provider: str | None = None,
                        model: str | None = None) -> KbConfig:
    """Apply CLI ``--llm`` / ``--llm-model`` onto a loaded config's ``[llm]`` tier.

    Passing ``provider`` also enables the tier, so a two-line toml block collapses to a
    single flag (``wiki <repo> --llm builtin``). Mutates and returns ``cfg``.
    """
    if provider:
        cfg.llm.enabled = True
        cfg.llm.provider = provider
    if model:
        cfg.llm.model = model
    return cfg
