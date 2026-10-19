"""Tests for the comment-preserving kb.toml source editor."""

import os
import threading
import time
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from contextlake.kb import config_edit
from contextlake.kb.config_edit import (
    add_source,
    read_sources,
    remove_source,
    set_source_enabled,
)


def _toml(p):
    return tomllib.loads(Path(p).read_text())


def test_add_source_preserves_comments(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text('# my config\n[kb]\nstore_dir = "~/x"  # keep me\n')
    add_source(str(cfg), {"type": "atlassian", "name": "jira", "mcp": "https://x"})
    text = cfg.read_text()
    assert "# my config" in text and "# keep me" in text          # comments preserved
    srcs = _toml(cfg)["sources"]
    assert srcs == [{"type": "atlassian", "name": "jira", "mcp": "https://x"}]


def test_add_source_upserts_by_name(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("[kb]\n")
    add_source(str(cfg), {"type": "atlassian", "name": "jira", "mcp": "a"})
    add_source(str(cfg), {"type": "atlassian", "name": "jira", "mcp": "b"})   # upsert
    srcs = _toml(cfg)["sources"]
    assert len(srcs) == 1 and srcs[0]["mcp"] == "b"


def test_add_source_upsert_preserves_other_keys_and_comments(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("[kb]\n")
    add_source(str(cfg), {"type": "atlassian", "name": "jira", "token_env": "JIRA_TOKEN"})

    text = cfg.read_text()
    text = text.replace('token_env = "JIRA_TOKEN"', 'token_env = "JIRA_TOKEN"  # keep me')
    cfg.write_text(text)

    add_source(str(cfg), {"type": "atlassian", "name": "jira", "mcp": "https://x"})

    text = cfg.read_text()
    assert "# keep me" in text                                    # comment survived
    src = _toml(cfg)["sources"][0]
    assert src["token_env"] == "JIRA_TOKEN"                        # prior key survived
    assert src["mcp"] == "https://x"                                # new key added


def test_remove_source(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("[kb]\n")
    add_source(str(cfg), {"type": "figma", "name": "designs"})
    assert remove_source(str(cfg), "designs") is True
    assert _toml(cfg).get("sources", []) == []
    assert remove_source(str(cfg), "designs") is False           # no-op


def test_set_enabled_toggle(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("[kb]\n")
    add_source(str(cfg), {"type": "gitlab", "name": "gl"})
    set_source_enabled(str(cfg), "gl", False)
    assert _toml(cfg)["sources"][0]["enabled"] is False


def test_read_sources_returns_raw_dicts(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text("[kb]\n")
    add_source(str(cfg), {"type": "figma", "name": "designs", "mcp": "https://f"})
    srcs = read_sources(str(cfg))
    assert srcs == [{"type": "figma", "name": "designs", "mcp": "https://f"}]


def test_read_sources_missing_file_returns_empty(tmp_path):
    cfg = tmp_path / "does-not-exist.toml"
    assert read_sources(str(cfg)) == []


def test_add_source_creates_parent_dirs_and_file(tmp_path):
    cfg = tmp_path / "nested" / "dir" / "kb.toml"
    add_source(str(cfg), {"type": "gitlab", "name": "gl"})
    assert cfg.exists()
    assert _toml(cfg)["sources"][0]["name"] == "gl"


def test_resolve_write_target_defaults_to_global_config(monkeypatch, tmp_path):
    """No explicit path, no local flag, and no local config anywhere in the
    ancestor chain -- falls back to global, same as before directory-scoped
    config existed."""
    from contextlake.kb import config as kbcfg
    from contextlake.kb.config_edit import resolve_write_target

    fake_global = tmp_path / "global-kb.toml"
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(fake_global))
    # Isolate the ancestor walk too -- an absolute nonexistent path is checked
    # directly (no walking), matching find_ancestor_config's isolation rule.
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "no-local.toml"))
    assert resolve_write_target(None) == fake_global


def test_resolve_write_target_honors_explicit_path(tmp_path):
    from contextlake.kb.config_edit import resolve_write_target

    explicit = tmp_path / "explicit.toml"
    assert resolve_write_target(str(explicit)) == explicit


def test_resolve_write_target_uses_an_existing_ancestor_local_config(monkeypatch, tmp_path):
    """Once a project has a local config anywhere in the ancestor chain, writes
    land there by default -- no need to re-pass --local every time."""
    from contextlake.kb import config as kbcfg
    from contextlake.kb.config_edit import resolve_write_target

    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "global-kb.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    root_local = tmp_path / ".contextlake.kb.toml"
    root_local.write_text("[kb]\n")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    assert resolve_write_target(None) == root_local


def test_resolve_write_target_local_flag_creates_in_cwd_when_none_exists(monkeypatch, tmp_path):
    """--local with no existing ancestor local config targets a new file in
    cwd, not the global config -- this is how a workspace gets its FIRST local
    config from `source add` alone, without running `init --local` first."""
    from contextlake.kb import config as kbcfg
    from contextlake.kb.config_edit import resolve_write_target

    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "global-kb.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    # relative, same as any other never-yet-created path -- resolve before comparing
    assert resolve_write_target(None, local=True).resolve() == cwd / ".contextlake.kb.toml"


def test_resolve_write_target_explicit_path_wins_over_local_flag(tmp_path):
    """An explicit --config always wins, even with --local also passed --
    --local is just a convenience default, not a second override tier."""
    from contextlake.kb.config_edit import resolve_write_target

    explicit = tmp_path / "explicit.toml"
    assert resolve_write_target(str(explicit), local=True) == explicit


def test_write_is_atomic_no_partial_file_left_and_content_intact(tmp_path):
    """`_write_document` writes via a temp sibling + os.replace, so a normal
    write leaves no stray temp file behind and the target has full content."""
    cfg = tmp_path / "kb.toml"
    add_source(str(cfg), {"type": "gitlab", "name": "gl"})
    add_source(str(cfg), {"type": "figma", "name": "designs", "mcp": "https://f"})

    assert not list(tmp_path.glob("*.tmp"))
    srcs = _toml(cfg)["sources"]
    assert [s["name"] for s in srcs] == ["gl", "designs"]


def test_two_edits_at_once_both_land(tmp_path, monkeypatch):
    """Each edit loads the whole file and writes the whole file. While the first has
    loaded and not yet written, the second must wait, or it loads the same bytes and its
    write drops the first one's source while both commands print success."""
    cfg = tmp_path / "kb.toml"
    paused = threading.Event()
    resume = threading.Event()
    first = []
    real_load = config_edit._load_document

    def slow_first_load(path):
        doc = real_load(path)
        if not first:
            first.append(1)
            paused.set()
            resume.wait(10)
        return doc

    monkeypatch.setattr(config_edit, "_load_document", slow_first_load)
    errors = []

    def run(name):
        try:
            add_source(str(cfg), {"type": "gitlab", "name": name})
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)

    one = threading.Thread(target=run, args=("one",))
    two = threading.Thread(target=run, args=("two",))
    one.start()
    assert paused.wait(10)
    two.start()
    time.sleep(0.3)   # long enough for `two` to load the still-empty file if nothing stops it
    resume.set()
    one.join(10)
    two.join(10)

    assert not errors
    assert sorted(s["name"] for s in _toml(cfg)["sources"]) == ["one", "two"]


def test_an_edit_gives_up_when_the_lock_stays_held(tmp_path, monkeypatch):
    """A holder that never lets go must not hang the command: it fails, says why and
    leaves the file as it was."""
    cfg = tmp_path / "kb.toml"
    add_source(str(cfg), {"type": "gitlab", "name": "gl"})
    before = cfg.read_text()
    monkeypatch.setattr(config_edit, "LOCK_TIMEOUT", 0.2)

    with config_edit._edit_lock(cfg), pytest.raises(TimeoutError, match="was not made"):
        add_source(str(cfg), {"type": "figma", "name": "designs"})

    assert cfg.read_text() == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_stale_temp_file_does_not_decide_the_mode_of_the_config(tmp_path):
    """The temp name used to be fixed, so a `kb.toml.tmp` left behind by a crash (or put
    there by someone else) kept its own mode through the write and the config was
    renamed over with it. The temp file is now created new, at 0600."""
    cfg = tmp_path / "kb.toml"
    stale = tmp_path / "kb.toml.tmp"
    stale.write_text("old")
    stale.chmod(0o666)

    add_source(str(cfg), {"type": "gitlab", "name": "gl"})

    assert cfg.stat().st_mode & 0o777 == 0o600
    assert stale.read_text() == "old"


def test_a_failed_write_leaves_no_temp_file_and_the_old_config(tmp_path, monkeypatch):
    cfg = tmp_path / "kb.toml"
    add_source(str(cfg), {"type": "gitlab", "name": "gl"})
    before = cfg.read_text()

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(config_edit.os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        add_source(str(cfg), {"type": "figma", "name": "designs"})

    assert not list(tmp_path.glob("*.tmp"))
    assert cfg.read_text() == before


def test_removing_from_a_missing_config_creates_nothing(tmp_path):
    cfg = tmp_path / "nested" / "kb.toml"

    assert remove_source(str(cfg), "gl") is False
    assert set_source_enabled(str(cfg), "gl", True) is False

    assert not (tmp_path / "nested").exists()


# --- a config that is a symbolic link --------------------------------------------------------
#
# `_write_document` ends in `os.replace`, which on a link removes the link and leaves a regular
# file in its place. A dotfiles setup (`~/.contextlake/kb.toml` linked to a file in a repo) kept
# its old content while the command printed success.

needs_symlinks = pytest.mark.skipif(os.name == "nt", reason="symbolic links need rights on Windows")


def _linked_config(tmp_path, content="# dotfiles\n[kb]\n"):
    behind = tmp_path / "dotfiles" / "kb.toml"
    behind.parent.mkdir()
    behind.write_text(content)
    link = tmp_path / "home" / "kb.toml"
    link.parent.mkdir()
    link.symlink_to(behind)
    return behind, link


@needs_symlinks
def test_every_edit_through_an_explicit_link_changes_the_file_behind_it(tmp_path):
    behind, link = _linked_config(tmp_path)

    add_source(str(link), {"type": "gitlab", "name": "gl"})
    assert link.is_symlink() and link.resolve() == behind.resolve()
    assert [s["name"] for s in _toml(behind)["sources"]] == ["gl"]
    assert "# dotfiles" in behind.read_text()

    assert set_source_enabled(str(link), "gl", False) is True
    assert _toml(behind)["sources"][0]["enabled"] is False

    assert remove_source(str(link), "gl") is True
    assert link.is_symlink() and not _toml(behind).get("sources")
    # The lock and the temp file sit beside the file that is written, not beside the link.
    assert (behind.parent / "kb.toml.lock").exists()
    assert sorted(p.name for p in link.parent.iterdir()) == ["kb.toml"]
    assert not list(behind.parent.glob("*.tmp"))


@needs_symlinks
def test_the_global_config_that_is_a_link_is_written_through(monkeypatch, tmp_path):
    from contextlake.kb import config as kbcfg

    behind, link = _linked_config(tmp_path)
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(link))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "no-local.toml"))

    add_source(None, {"type": "gitlab", "name": "gl"})

    assert link.is_symlink()
    assert [s["name"] for s in _toml(behind)["sources"]] == ["gl"]


@needs_symlinks
def test_the_wiki_model_is_saved_through_a_linked_config(tmp_path):
    behind, link = _linked_config(tmp_path, '# dotfiles\n[llm]\nprovider = "none"\n')

    config_edit.set_builtin_model(str(link), "acme/model")

    assert link.is_symlink()
    llm = _toml(behind)["llm"]
    assert llm == {"provider": "none", "builtin_model": "acme/model"}
    assert "# dotfiles" in behind.read_text()


@needs_symlinks
def test_a_link_to_a_file_that_does_not_exist_yet_creates_that_file(tmp_path):
    behind = tmp_path / "dotfiles" / "kb.toml"
    behind.parent.mkdir()
    link = tmp_path / "kb.toml"
    link.symlink_to(behind)

    add_source(str(link), {"type": "gitlab", "name": "gl"})

    assert link.is_symlink()
    assert [s["name"] for s in _toml(behind)["sources"]] == ["gl"]


@needs_symlinks
@pytest.mark.parametrize("local", [False, True])
def test_a_walked_up_config_that_is_a_link_is_replaced_and_never_written_through(
        monkeypatch, tmp_path, local):
    """A file found by walking up from the working directory can arrive in a cloned repo,
    so a link there must not send the write to a file elsewhere. The link is replaced, as
    before, and the file it pointed at is left alone."""
    from contextlake.kb import config as kbcfg

    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "global-kb.toml"))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", ".contextlake.kb.toml")
    outside = tmp_path / "elsewhere" / "target.toml"
    outside.parent.mkdir()
    outside.write_text("# not yours\n[kb]\n")
    before = outside.read_bytes()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".contextlake.kb.toml").symlink_to(outside)
    monkeypatch.chdir(project)

    add_source(None, {"type": "gitlab", "name": "gl"}, local=local)

    assert outside.read_bytes() == before
    written = project / ".contextlake.kb.toml"
    assert not written.is_symlink()
    assert [s["name"] for s in _toml(written)["sources"]] == ["gl"]
