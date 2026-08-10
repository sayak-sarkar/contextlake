"""`kb.toml`'s `languages` key must actually filter (G10).

It was validated as a known key, surfaced in the dashboard, and documented as a
filter -- and never passed to the parser, so every install indexed all supported
languages regardless of what it said. A setting that silently ignores the user.

The subtlety that makes this more than a one-line wire-up: the old default was
["csharp", "typescript", "python"]. Passing THAT through would have silently stopped
indexing c, cpp, go, java, javascript, kotlin, php, ruby, rust, scala and tsx for
every user who never set the key -- a far worse bug than the dead setting. So the
default now means "everything", and only an explicit list filters.
"""

import pathlib

from contextlake.kb.config import KbConfig
from contextlake.kb.parse import index_repo_dir

_FILES = {
    "a.py": b"def py_only():\n    pass\n",
    "b.cpp": b"void cpp_only() { }\n",
    "c.go": b"package main\nfunc goOnly() {}\n",
}


def _names(tmp_path: pathlib.Path, languages):
    for fn, body in _FILES.items():
        (tmp_path / fn).write_bytes(body)
    shard = index_repo_dir(str(tmp_path), "r", languages=languages)
    return {n.name for n in shard.nodes}


def test_the_default_config_indexes_everything(tmp_path):
    """The behaviour every existing install has today must not change."""
    assert KbConfig().languages is None
    names = _names(tmp_path, KbConfig().languages)
    assert {"py_only", "cpp_only", "goOnly"} <= names


def test_an_explicit_single_language_filters(tmp_path):
    names = _names(tmp_path, ["python"])
    assert "py_only" in names
    assert "cpp_only" not in names
    assert "goOnly" not in names


def test_an_explicit_pair_filters_to_both(tmp_path):
    names = _names(tmp_path, ["python", "go"])
    assert {"py_only", "goOnly"} <= names
    assert "cpp_only" not in names


def test_an_empty_list_in_toml_means_everything_not_nothing(tmp_path):
    """`languages = []` is far more likely to mean "I did not decide" than "index no
    code at all", and the latter would be a silent empty graph."""
    cfg = KbConfig.model_validate({"languages": []})
    names = _names(tmp_path, cfg.languages or None)
    assert {"py_only", "cpp_only", "goOnly"} <= names


def test_the_old_default_list_no_longer_exists():
    """Guard against reintroducing it: a three-language default that reaches the parser
    would drop eleven languages from every install that never set the key."""
    import contextlake.kb.config as config_mod
    assert not hasattr(config_mod, "DEFAULT_LANGUAGES")
