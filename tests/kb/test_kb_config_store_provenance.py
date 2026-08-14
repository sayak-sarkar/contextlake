"""`--config` must name the store, door 2: an existing config that sets no store_dir.

Split from ``tests/test_bootstrap_config_door.py`` because these import the knowledge
layer, and the core tier's tests may not (``tests/test_core_tier_has_no_kb_imports.py``).
Door 1 -- `bootstrap --config <a kb.toml>` -- stays there, since it drives the CLI as a
subprocess and imports nothing.

`load_kb_config` already hard-errors when `--config` does not EXIST, because it "can
point at a completely different (possibly production) store". A config that exists and
simply omits ``[kb] store_dir`` is merged over the global file and inherits the global
store -- the same hazard by a quieter route. Measured during an audit: four nodes written
into a production store by a command carrying an explicit --config.
"""

from __future__ import annotations


def test_a_config_that_sets_no_store_dir_says_which_file_chose_the_store(tmp_path):
    """Door 2. The command is allowed -- a --config that only tunes [embeddings] is a
    legitimate thing to write -- but it must say where the store came from, because
    silently inheriting a production store is what actually happened."""
    from contextlake.kb.config import load_kb_config

    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)
    partial = tmp_path / "partial.toml"
    partial.write_text('[embeddings]\nenabled = true\n', encoding="utf-8")

    said: list[str] = []
    import contextlake.logging_setup as ls
    orig = ls.log
    ls.log = lambda msg, *a, **k: said.append(str(msg))
    try:
        load_kb_config(str(partial))
    finally:
        ls.log = orig

    joined = "\n".join(said)
    assert "does not set [kb] store_dir" in joined, (
        f"nothing disclosed which store was used; said: {joined!r}")


def test_a_config_that_does_set_store_dir_stays_quiet(tmp_path):
    """The near-miss. A warning on every correct invocation is noise that gets ignored,
    which would make the disclosure above worthless within a week."""
    from contextlake.kb.config import load_kb_config

    full = tmp_path / "full.toml"
    full.write_text(f'[kb]\nstore_dir = "{(tmp_path / "s").as_posix()}"\n', encoding="utf-8")

    import contextlake.logging_setup as ls
    said: list[str] = []
    orig = ls.log
    ls.log = lambda msg, *a, **k: said.append(str(msg))
    try:
        load_kb_config(str(full))
    finally:
        ls.log = orig
    assert not any("does not set [kb] store_dir" in s for s in said), (
        "a config that DOES set store_dir was warned about anyway")
