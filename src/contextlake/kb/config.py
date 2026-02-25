"""Configuration for the knowledge layer (generic, principle G1).

All organization-specific facts — which Atlassian/Figma sites, which key->repo
maps, which glossary — live in a user TOML file loaded at runtime, never in this
package. The repo ships only ``examples/kb.toml.example`` with placeholders.

Precedence (later wins): built-in defaults -> ``~/.contextlake/kb.toml`` ->
``.contextlake.kb.toml`` (cwd) -> an explicit ``--config`` path. ``sources`` and
``rules`` lists are replaced wholesale by the highest-precedence file that sets
them (predictable, no surprise merging). The former ``~/.gitlab-sync/`` paths are
still read (just below their contextlake counterparts) so existing setups keep
working after the rename.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..config import expand_path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

DEFAULT_STORE_DIR = "~/.contextlake/kb"
GLOBAL_CONFIG = "~/.contextlake/kb.toml"
LOCAL_CONFIG = ".contextlake.kb.toml"
# Former gitlab-sync locations, still read so an existing store/config keeps working.
LEGACY_STORE_DIR = "~/.gitlab-sync/kb"
LEGACY_GLOBAL_CONFIG = "~/.gitlab-sync/kb.toml"
LEGACY_LOCAL_CONFIG = ".gitlab-sync.kb.toml"
DEFAULT_LANGUAGES = ["csharp", "typescript", "python"]


def default_store_dir() -> str:
    """The default knowledge-store location.

    Prefer the current ``~/.contextlake/kb``; fall back to a pre-existing legacy
    ``~/.gitlab-sync/kb`` store so an already-built index keeps working without a
    re-index after the rename.
    """
    if not Path(expand_path(DEFAULT_STORE_DIR)).exists() \
            and Path(expand_path(LEGACY_STORE_DIR)).exists():
        return LEGACY_STORE_DIR
    return DEFAULT_STORE_DIR


class SourceCfg(BaseModel):
    """A knowledge-source connector (Atlassian, Figma, …). Extra keys allowed so
    connector-specific options survive without a schema change."""

    model_config = ConfigDict(extra="allow")
    type: str
    name: str
    mcp: str | None = None


class RuleCfg(BaseModel):
    """An association rule (branch_key, key_map, link_scrape, dependency, …)."""

    model_config = ConfigDict(extra="allow")
    type: str
    pattern: str | None = None
    file: str | None = None


class EmbeddingsCfg(BaseModel):
    """Semantic-search tier. Local-first and off by default; connector-specific
    keys are allowed so providers can carry extra options without a schema bump."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    provider: str = "ollama"  # ollama | openai (openai-compatible)
    model: str | None = None
    base_url: str = "http://127.0.0.1:11434"
    batch_size: int = 64
    vector_backend: str = "auto"  # auto | sqlite-vec | brute
    api_key_env: str = "OPENAI_API_KEY"  # env var holding the key (never the key itself)


class LlmCfg(BaseModel):
    """Local-first LLM tier for the curated wiki and its verification council.
    Off by default; connector-specific keys allowed."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    provider: str = "ollama"  # ollama | openai (openai-compatible)
    model: str | None = None
    base_url: str = "http://127.0.0.1:11434"
    council_size: int = 3
    accept_score: float = 0.7
    api_key_env: str = "OPENAI_API_KEY"  # env var holding the key (never the key itself)


class KbConfig(BaseModel):
    store_dir: str = DEFAULT_STORE_DIR
    languages: list[str] = Field(default_factory=lambda: list(DEFAULT_LANGUAGES))
    embeddings: EmbeddingsCfg = Field(default_factory=EmbeddingsCfg)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    sources: list[SourceCfg] = Field(default_factory=list)
    rules: list[RuleCfg] = Field(default_factory=list)

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
    """Load and merge KB config from the precedence chain."""
    merged: dict = {}
    # Legacy gitlab-sync files are read just below their contextlake counterparts,
    # so an existing setup keeps working while a new file takes precedence.
    for src in (LEGACY_GLOBAL_CONFIG, GLOBAL_CONFIG,
                LEGACY_LOCAL_CONFIG, LOCAL_CONFIG, config_path):
        merged.update(_read_toml(src))

    kb = merged.get("kb", {})
    return KbConfig(
        store_dir=kb.get("store_dir", default_store_dir()),
        languages=kb.get("languages", list(DEFAULT_LANGUAGES)),
        embeddings=EmbeddingsCfg(**merged.get("embeddings", {})),
        llm=LlmCfg(**merged.get("llm", {})),
        sources=[SourceCfg(**s) for s in merged.get("sources", [])],
        rules=[RuleCfg(**r) for r in merged.get("rules", [])],
    )
