"""Comment-preserving ``kb.toml`` mutation for ``[[sources]]`` connector blocks.

The read path (``kb/config.py``) uses stdlib ``tomllib``, which is read-only.
Writing back a config that a user hand-edited (comments, formatting) requires a
round-trip-preserving parser, so this module uses ``tomlkit`` instead. Kept
separate from ``kb/config.py`` so the read path stays dependency-light.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import expand_path
from . import config as kb_config

try:
    import tomlkit
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "tomlkit is required to manage sources; install contextlake[kb]"
    ) from exc


def resolve_write_target(config_path: str | None) -> Path:
    """The kb.toml path to write: an explicit path, else the global config."""
    target = config_path if config_path else kb_config.GLOBAL_CONFIG
    return Path(expand_path(target))


def _load_document(path: Path):
    if not path.exists():
        return tomlkit.document()
    return tomlkit.parse(path.read_text())


def _write_document(path: Path, doc) -> None:
    """Write ``doc`` to ``path`` atomically: a temp sibling file then
    ``os.replace`` (atomic rename on POSIX), so a crash mid-write never leaves
    ``path`` truncated or half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(tomlkit.dumps(doc))
    os.replace(tmp, path)


def _sources_aot(doc):
    """The ``[[sources]]`` array-of-tables on ``doc``, creating it if absent."""
    if doc.get("sources") is None:
        doc["sources"] = tomlkit.aot()
    return doc["sources"]


def _find_source_index(aot, name: str) -> int | None:
    for i, item in enumerate(aot):
        if item.get("name") == name:
            return i
    return None


def add_source(config_path: str | None, source: dict) -> None:
    """Upsert a ``[[sources]]`` block keyed by ``source["name"]``.

    When a source with that name already exists, its keys are updated in
    place on the existing table rather than the table being replaced -- so
    other previously-set keys (e.g. ``token_env``, ``file_key``) and any
    inline comments survive a re-add that only touches a subset of keys.
    """
    path = resolve_write_target(config_path)
    doc = _load_document(path)
    aot = _sources_aot(doc)

    existing = _find_source_index(aot, source["name"])
    if existing is None:
        block = tomlkit.table()
        for key, value in source.items():
            block[key] = value
        aot.append(block)
    else:
        table = aot[existing]
        for key, value in source.items():
            table[key] = value

    _write_document(path, doc)


def remove_source(config_path: str | None, name: str) -> bool:
    """Delete the ``[[sources]]`` block named ``name``. False if absent."""
    path = resolve_write_target(config_path)
    doc = _load_document(path)
    aot = _sources_aot(doc)

    index = _find_source_index(aot, name)
    if index is None:
        return False

    del aot[index]
    _write_document(path, doc)
    return True


def set_source_enabled(config_path: str | None, name: str, enabled: bool) -> bool:
    """Toggle ``enabled`` on the named source block. False if absent."""
    path = resolve_write_target(config_path)
    doc = _load_document(path)
    aot = _sources_aot(doc)

    index = _find_source_index(aot, name)
    if index is None:
        return False

    aot[index]["enabled"] = enabled
    _write_document(path, doc)
    return True


def read_sources(config_path: str | None) -> list[dict]:
    """The raw ``[[sources]]`` dicts from the resolved file."""
    path = resolve_write_target(config_path)
    if not path.exists():
        return []
    doc = tomlkit.parse(path.read_text())
    aot = doc.get("sources")
    if aot is None:
        return []
    return [item.unwrap() for item in aot]
