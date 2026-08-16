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
``[[sources]] command``/``args``/``mcp_command``) are honoured **only** from the
global file or an explicit ``--config`` path, never from the auto-discovered
``.contextlake.kb.toml`` -- otherwise cloning a hostile repo and working inside
it is remote code execution. See ``kb/trust.py`` for the full rationale, and
``CONTEXTLAKE_NO_LOCAL_CONFIG=1`` to skip the discovered tier altogether.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..config import ConfigError, expand_path, find_ancestor_config  # noqa: F401 -- re-exported
from ..logging_setup import log
from .trust import EXECUTABLE_SOURCE_KEYS, is_executable_key, is_privileged_source

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
# 5,000,000 (5 MB), not 5 * 1024**2 (5 MiB, 5,242,880 bytes): docs/index-code-graph.md
# and docs/style-guide-formatting.md both document this knob as "5 MB", and this is
# the user-facing [kb] config value, so it wins over parse.py's binary-unit constant
# of the same name -- kb/parse.py imports this constant rather than defining its own,
# so a file between 5,000,000 and 5,242,880 bytes can no longer be skipped or parsed
# depending on which entry point (CLI config vs. a direct index_repo_dir call) it
# came through.
DEFAULT_MAX_FILE_BYTES = 5_000_000


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
    base_url: str = "http://127.0.0.1:11434"
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
    for src in (GLOBAL_CONFIG, local_config, config_path):
        # Provenance gate. A config file the user never named -- i.e. the one
        # found by walking up from cwd -- may not carry keys that become a
        # subprocess argv: cloning a hostile repo and cd-ing into it was
        # otherwise enough to get code execution on the next LLM-touching
        # command. Everything else in that file still applies. See kb/trust.py.
        privileged = is_privileged_source(src, config_path, global_config=GLOBAL_CONFIG)
        for table, values in _read_toml(src).items():
            if table == "kb" and "store_dir" in values:
                # Remember WHICH file set the store, not just what it was set to. The
                # merge is last-wins, so this ends up naming the file that decided it.
                store_dir_src = str(Path(expand_path(src)))
            if not privileged:
                values = _drop_executable_keys(table, values, src)
            if table in _SCALAR_TABLES:
                # Deep-merge by key: a local file setting only `model` must not
                # wipe out `enabled`/`provider` set globally under the same table.
                merged.setdefault(table, {}).update(values)
            else:
                # sources/rules (and any unrecognised table) stay wholesale-replaced,
                # per the documented precedence rule above.
                merged[table] = values

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
        index_workers=kb.get("index_workers", None),
        anonymize=_anonymize_value(kb.get("anonymize")),
        embeddings=EmbeddingsCfg(**merged.get("embeddings", {})),
        llm=LlmCfg(**merged.get("llm", {})),
        sources=[SourceCfg(**s) for s in merged.get("sources", [])],
        rules=[RuleCfg(**r) for r in merged.get("rules", [])],
        loaded_from=loaded_from,
        searched=searched,
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
_TABLES = {"kb", "embeddings", "llm", "sources", "rules"}
# Tables of scalar fields, deep-merged key-by-key across the precedence chain (see
# load_kb_config). sources/rules are list tables and stay wholesale-replaced by design.
_SCALAR_TABLES = {"kb", "embeddings", "llm"}


def _drop_executable_keys(table: str, values, source: str):
    """Strip argv-reaching keys out of one table of a non-privileged config file.

    Returns ``values`` unchanged (the same object, no copy) when there is nothing
    to drop, which is every table of every honest config -- the gate costs one
    scan on the normal path.
    """
    if table in _SCALAR_TABLES and isinstance(values, dict):
        dropped = [k for k, v in values.items() if is_executable_key(table, k, v)]
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
            _warn_untrusted(f"[{table}] {key}", source)
        if weakens_privacy:
            _warn_untrusted_privacy(source)
            dropped = [*dropped, "anonymize"]
        return {k: v for k, v in values.items() if k not in dropped}
    if table == "sources" and isinstance(values, list):
        # sources is a list table: screen each entry's dict. A source's transport
        # command spawns a process just like [llm] command does (see trust.py).
        cleaned, dropped_keys = [], set()
        for entry in values:
            bad = EXECUTABLE_SOURCE_KEYS & entry.keys() if isinstance(entry, dict) else set()
            dropped_keys |= bad
            cleaned.append({k: v for k, v in entry.items() if k not in bad} if bad else entry)
        if not dropped_keys:
            return values
        for key in sorted(dropped_keys):
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
