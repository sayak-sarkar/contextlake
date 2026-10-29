"""Tests for configuration loading and precedence."""

import builtins
import os
import re
from pathlib import Path

import pytest

from contextlake.config import (
    DEFAULT_CONFIG,
    ConfigError,
    WrongConfigFile,
    find_ancestor_config,
    get_cache_paths,
    load_config,
)


def _isolate_globals(monkeypatch, tmp_path):
    """Point the global AND local config files at non-existent absolute paths
    so a test only ever sees the files it writes itself -- an absolute path
    is checked directly (see find_ancestor_config), not walked, so this also
    neutralizes the ancestor-directory search for tests that don't want it."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    monkeypatch.setattr("contextlake.config.LOCAL_CONFIG_FILE", str(tmp_path / "no-local.ini"))
    monkeypatch.chdir(tmp_path)


def test_defaults_when_no_files(tmp_path, monkeypatch):
    # Run from an empty dir with no global config so only defaults apply.
    _isolate_globals(monkeypatch, tmp_path)
    config = load_config()
    assert config["gitlab_group"] == DEFAULT_CONFIG["gitlab_group"]
    assert config["max_workers"] == DEFAULT_CONFIG["max_workers"]


def test_explicit_config_path_overrides_defaults(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    custom = tmp_path / "custom.ini"
    custom.write_text("[contextlake]\ngitlab_group = my-group\nmax_workers = 3\n")

    config = load_config(str(custom))
    assert config["gitlab_group"] == "my-group"
    assert config["max_workers"] == "3"
    # untouched keys still come from defaults
    assert config["clone_timeout"] == DEFAULT_CONFIG["clone_timeout"]


def test_config_path_values_are_tilde_expanded(tmp_path, monkeypatch):
    # Bug: a `~` in a config-file work_dir was treated as a literal directory,
    # so status/clone operated on a non-existent path and saw 0 local repos.
    _isolate_globals(monkeypatch, tmp_path)
    custom = tmp_path / "c.ini"
    custom.write_text("[contextlake]\nwork_dir = ~/repos\ncache_dir = ~/.cache/gs\n")
    config = load_config(str(custom))
    assert config["work_dir"] == os.path.expanduser("~/repos")
    assert config["cache_dir"] == os.path.expanduser("~/.cache/gs")
    assert "~" not in config["work_dir"]


@pytest.mark.parametrize("given", ["~/ws/cfg.ini", "$CFG_DIR/cfg.ini"])
def test_explicit_config_path_is_expanded_before_it_is_read(tmp_path, monkeypatch, given):
    """The existence check expanded `~` and `$VAR`, but the read used the text as typed. The
    check passed, the file was ignored, and the run used the defaults (work_dir ~/work) with
    a warning. `--config=~/ws/cfg.ini` reaches the program unexpanded in bash."""
    _isolate_globals(monkeypatch, tmp_path)
    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    (home / "ws" / "cfg.ini").write_text(
        "[contextlake]\ngitlab_group = my-group\nwork_dir = /srv/ws\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CFG_DIR", str(home / "ws"))

    config = load_config(given)

    assert (config["gitlab_group"], config["work_dir"]) == ("my-group", "/srv/ws")


def test_missing_explicit_config_path_is_a_hard_error(tmp_path, monkeypatch):
    """A --config path that doesn't exist must fail loudly, not silently fall
    through the precedence chain to ~/.contextlake.ini -- which can point at a
    completely different workspace than intended. Mirrors kb.toml's identical
    guard (see kb/test_kb_config.py's test of the same name)."""
    _isolate_globals(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "does-not-exist.ini"))


def test_missing_explicit_config_path_does_not_fall_back_to_global(tmp_path, monkeypatch):
    """The exact near-miss this guards against: a real global config exists (as
    it would on the user's own machine) and a typo'd/not-yet-created --config
    path must never silently resolve to it."""
    _isolate_globals(monkeypatch, tmp_path)
    real_global = tmp_path / "real-global.ini"
    real_global.write_text("[contextlake]\ngitlab_group = real-group\n")
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(real_global))
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "typo-d.ini"))


def test_explicit_config_with_a_misspelled_header_is_a_hard_error(tmp_path, monkeypatch):
    """Section names are case-sensitive, so `[ContextLake]` is not `[contextlake]`. The file
    was read as empty and the run went on with the defaults, or with the global file when
    one exists, and nothing said the named file had been ignored."""
    _isolate_globals(monkeypatch, tmp_path)
    real_global = tmp_path / "real-global.ini"
    real_global.write_text("[contextlake]\ngitlab_group = real-group\nwork_dir = /srv/global\n")
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(real_global))
    bad = tmp_path / "bad.ini"
    bad.write_text("[ContextLake]\ngitlab_group = my-group\nwork_dir = /srv/mine\n")

    with pytest.raises(ConfigError) as exc:
        load_config(str(bad))

    msg = str(exc.value)
    assert str(bad) in msg and "no [contextlake] section" in msg
    assert "Sections found: ContextLake" in msg
    assert not isinstance(exc.value, WrongConfigFile)
    assert exc.value.exit_code == 1


@pytest.mark.parametrize("make", ["empty", "devnull"])
def test_explicit_config_with_no_section_at_all_is_a_hard_error(tmp_path, monkeypatch, make):
    _isolate_globals(monkeypatch, tmp_path)
    path = "/dev/null"
    if make == "empty":
        path = str(tmp_path / "empty.ini")
        (tmp_path / "empty.ini").write_text("")

    with pytest.raises(ConfigError, match=r"Sections found: none"):
        load_config(path)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a mode bit that binds")
def test_unreadable_explicit_config_is_a_hard_error(tmp_path, monkeypatch):
    """ConfigParser.read() skips a file it cannot open without raising, so the run used the
    defaults. `kb` commands already failed on the same file with Permission denied."""
    _isolate_globals(monkeypatch, tmp_path)
    locked = tmp_path / "locked.ini"
    locked.write_text("[contextlake]\ngitlab_group = my-group\n")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("this account can read a mode 000 file")
        with pytest.raises(ConfigError, match=r"cannot be read: .*locked\.ini .*Permission denied"):
            load_config(str(locked))
    finally:
        locked.chmod(0o600)


def test_a_directory_as_explicit_config_is_a_hard_error(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match=r"cannot be read: .* \(Is a directory\)"):
        load_config(str(tmp_path))


def test_discovered_config_with_a_misspelled_header_is_still_skipped(tmp_path, monkeypatch):
    """Only a file the user named is held to the section rule. The one found by walking up
    from the current directory keeps being optional, as the docstring of load_config says."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    (tmp_path / ".contextlake.ini").write_text("[ContextLake]\ngitlab_group = nope\n")
    monkeypatch.chdir(tmp_path)

    assert load_config()["gitlab_group"] == DEFAULT_CONFIG["gitlab_group"]


def test_a_discovered_config_that_cannot_be_opened_is_skipped(tmp_path, monkeypatch):
    """Only a file the user named must be readable. A `.contextlake.ini` found by walking up
    that cannot be opened (here a directory) is skipped, as a missing one is."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    (tmp_path / ".contextlake.ini").mkdir()
    monkeypatch.chdir(tmp_path)

    assert load_config()["gitlab_group"] == DEFAULT_CONFIG["gitlab_group"]


@pytest.mark.parametrize("make, mark", [
    (lambda p: p.mkdir(), "absent"),
    (lambda p: p.write_text("[contextlake]\nmax_workers = 3\n"), "found"),
], ids=["directory", "file"])
def test_the_placeholder_warning_marks_a_discovered_directory_absent(
        tmp_path, monkeypatch, make, mark):
    """The warning lists each place it looked and whether a config was there. `exists` said
    [found] for a directory named `.contextlake.ini`, which the loader then skips, so the
    list claimed a config that was never read. A directory is not discovered, so the list
    holds the one line for a search that found nothing. A regular file is [found].

    Reads the lines handed to `config.log` rather than the logger's output: the package
    logger does not propagate, so a capture through it depends on test order."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    make(tmp_path / ".contextlake.ini")
    monkeypatch.chdir(tmp_path)
    lines = []
    monkeypatch.setattr("contextlake.config.log", lambda message, *a, **k: lines.append(message))

    load_config()

    listed = "\n".join(lines)
    assert "still the placeholder" in listed, "the warning did not fire"
    marks = re.findall(r"\[(found|absent)\] \S*\.contextlake\.ini\b", listed)
    assert marks == [mark], listed


def test_the_placeholder_warning_marks_a_global_directory_absent(tmp_path, monkeypatch):
    """The global config path is checked as a file as well. A directory named like it is
    skipped by the loader, so the list must not say [found] for it.

    The test above no longer reaches this check: the walk skips a directory, so no discovered
    path is a directory. The global path is the one a directory can still reach."""
    global_dir = tmp_path / "global.ini"
    global_dir.mkdir()
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(global_dir))
    monkeypatch.setattr("contextlake.config.LOCAL_CONFIG_FILE", str(tmp_path / "no-local.ini"))
    monkeypatch.chdir(tmp_path)
    lines = []
    monkeypatch.setattr("contextlake.config.log", lambda message, *a, **k: lines.append(message))

    load_config()

    listed = "\n".join(lines)
    assert "still the placeholder" in listed, "the warning did not fire"
    assert re.findall(r"\[(found|absent)\] \S*global\.ini\b", listed) == ["absent"], listed


_EXAMPLE_KB_TOML = (Path(__file__).resolve().parents[1] / "examples" / "kb.toml.example")

# A kb.toml with two `[[sources]]` blocks does not parse as an INI: the section name repeats.
# A multi-line array whose closing bracket is at column 0 does not parse either.
_TWO_SOURCES = ('[kb]\nstore_dir = "/srv/store"\n\n[[sources]]\ntype = "web"\nname = "a"\n\n'
                '[[sources]]\ntype = "web"\nname = "b"\n')
_MULTILINE_ARRAY = '[llm]\ncommand = "claude"\nargs = [\n"-p",\n]\n'


@pytest.mark.parametrize("name, text", [
    ("kb.toml", '[kb]\nstore_dir = "/srv/store"\n'),
    ("notes.cfg", "[llm]\nprovider = openai\n"),
    ("empty.toml", ""),
    ("two-sources.cfg", _TWO_SOURCES),
    ("two-sources.toml", _TWO_SOURCES),
    ("array.cfg", _MULTILINE_ARRAY),
    ("shipped-example.cfg", _EXAMPLE_KB_TOML.read_text(encoding="utf-8")),
], ids=["kb-toml", "llm-table", "empty-toml", "two-sources-by-header", "two-sources-by-name",
        "multiline-array", "shipped-example"])
def test_a_knowledge_config_as_explicit_config_exits_two_and_names_the_right_flag(
        tmp_path, monkeypatch, name, text):
    """The same mistake `bootstrap` has refused with exit 2 since it got `--kb-config`. It now
    comes from the loader, so `mirror` and `schedule` verbs refuse it too, whether or not the
    file happens to parse as an INI."""
    _isolate_globals(monkeypatch, tmp_path)
    kb = tmp_path / name
    kb.write_text(text)

    with pytest.raises(WrongConfigFile) as exc:
        load_config(str(kb))

    assert exc.value.exit_code == 2
    assert "mirror INI" in str(exc.value) and "--kb-config" in str(exc.value)


@pytest.mark.parametrize("name", ["mirror.ini", "mirror.toml"])
def test_a_broken_mirror_ini_gets_its_parse_error_whatever_its_name(
        tmp_path, monkeypatch, name):
    """A `.toml` name alone said "knowledge config", so a broken mirror INI saved as
    `mirror.toml` was sent to `--kb-config` instead of to its duplicate line."""
    _isolate_globals(monkeypatch, tmp_path)
    ini = tmp_path / name
    ini.write_text("[contextlake]\ngitlab_group = a\n[contextlake]\nwork_dir = b\n")

    with pytest.raises(ConfigError, match="a section appears twice") as exc:
        load_config(str(ini))

    assert not isinstance(exc.value, WrongConfigFile)
    assert str(ini) in str(exc.value)


@pytest.fixture
def update_calls(monkeypatch, tmp_path):
    """Records any `mirror update` that starts, and keeps it off the real clones.

    DEFAULT_CONFIG fixes `work_dir` when the module is imported, from the real HOME, and the
    conftest moves HOME only after that. A test that lets a mirror verb run on the defaults
    would fetch in every clone under the developer's real ~/work.
    """
    monkeypatch.setitem(DEFAULT_CONFIG, "work_dir", str(tmp_path / "ws"))
    calls = []

    def _update(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("update ran")

    monkeypatch.setattr("contextlake.cli.update_repositories", _update)
    return calls


def test_cli_stops_on_a_misspelled_explicit_header_and_runs_nothing(
        tmp_path, capsys, update_calls):
    """End to end: `mirror update --config bad.ini` used to print the placeholder warning,
    run update on the default workspace and exit 0."""
    from contextlake import cli

    bad = tmp_path / "bad.ini"
    bad.write_text("[ContextLake]\ngitlab_group = my-group\n")

    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "update", "--config", str(bad)])

    assert exc.value.code == 1
    assert "no [contextlake] section" in capsys.readouterr().out
    assert update_calls == []


@pytest.mark.parametrize("verb", [["mirror", "update"], ["schedule", "recommend"]])
def test_cli_exits_two_for_a_knowledge_config_on_both_load_paths(
        tmp_path, capsys, update_calls, verb):
    """`schedule` loads the config in its own branch of main(), apart from the mirror verbs."""
    from contextlake import cli

    kb = tmp_path / "kb.toml"
    kb.write_text('[kb]\nstore_dir = "/srv/store"\n')

    with pytest.raises(SystemExit) as exc:
        cli.main([*verb, "--config", str(kb)])

    assert exc.value.code == 2
    assert "--kb-config" in capsys.readouterr().out
    assert update_calls == []


# --- an INI parse error or a bad yes/no value names the file that caused it ---------------

@pytest.mark.parametrize("text, expected", [
    ("gitlab_group = a\n", "(line 1): a setting comes before any [contextlake] header"),
    ("[contextlake]\ngitlab_group = a\njunk line\n", '(line 3): a line is not "key = value"'),
    ("[contextlake]\ngitlab_group = a\n[contextlake]\n", "(line 3): a section appears twice"),
], ids=["no-header", "junk-line", "duplicate-section"])
def test_an_ini_syntax_error_is_a_message_that_names_the_file(
        tmp_path, monkeypatch, text, expected):
    """These left load_config as a raw traceback. The file is a discovered one here: the rule
    is for every file in the chain. A `%` in a value is literal text now, so it is no longer
    an error (test_a_percent_sign_in_a_value_is_read_as_written)."""
    _isolate_globals(monkeypatch, tmp_path)
    local = tmp_path / ".contextlake.ini"
    local.write_text(text)
    monkeypatch.setattr("contextlake.config.LOCAL_CONFIG_FILE", str(local))

    with pytest.raises(ConfigError) as exc:
        load_config()

    assert str(local) in str(exc.value) and expected in str(exc.value)


def test_an_ini_file_that_is_not_text_is_a_message_that_names_the_file(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    binary = tmp_path / "binary.ini"
    binary.write_bytes(b"[contextlake]\ngitlab_group = \xff\xfe\n")

    with pytest.raises(ConfigError, match=r"binary\.ini: it is not valid UTF-8"):
        load_config(str(binary))


def test_a_bad_yes_no_value_names_the_file_that_set_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from contextlake.cli import apply_cli_overrides

    _isolate_globals(monkeypatch, tmp_path)
    glob = tmp_path / "global.ini"
    glob.write_text("[contextlake]\ngitlab_group = g\nprotect_working_branches = yes\n")
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(glob))
    named = tmp_path / "named.ini"
    named.write_text("[contextlake]\ngitlab_group = g\ndry_run = sure\n")

    with pytest.raises(ConfigError) as exc:
        apply_cli_overrides(SimpleNamespace(), load_config(str(named)))

    msg = str(exc.value)
    assert "dry_run: 'sure' is not one of" in msg and msg.endswith(f"(set in {named})")

    glob.write_text("[contextlake]\nprotect_working_branches = maybe\n")   # now only this is bad
    named.write_text("[contextlake]\ngitlab_group = g\n")
    with pytest.raises(ConfigError, match=rf"protect_working_branches: 'maybe' .*{glob.name}"):
        apply_cli_overrides(SimpleNamespace(), load_config(str(named)))


def test_a_flag_on_the_command_line_still_wins_over_a_bad_file_value(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from contextlake.cli import apply_cli_overrides

    _isolate_globals(monkeypatch, tmp_path)
    named = tmp_path / "named.ini"
    named.write_text("[contextlake]\ngitlab_group = g\ndry_run = sure\n")

    config = apply_cli_overrides(SimpleNamespace(dry_run=True), load_config(str(named)))

    assert config["dry_run"] == "true"


def test_the_origin_map_is_set_by_load_config_and_not_by_a_file(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    named = tmp_path / "named.ini"
    named.write_text("[contextlake]\ngitlab_group = g\n_config_origin = forged\n")

    assert load_config(str(named))["_config_origin"]["gitlab_group"] == str(named)


def test_a_percent_sign_in_a_value_is_read_as_written(tmp_path, monkeypatch):
    """configparser's default interpolation read `%` as syntax, so `work_dir = /srv/50%off`
    raised InterpolationSyntaxError on every mirror command."""
    _isolate_globals(monkeypatch, tmp_path)
    ini = tmp_path / "pct.ini"
    ini.write_text("[contextlake]\nwork_dir = /srv/50%off\ngitlab_group = a%%b\n"
                   "branch = rel/%(x)s\n")

    config = load_config(str(ini))

    assert config["work_dir"] == "/srv/50%off"
    assert config["gitlab_group"] == "a%%b", "not collapsed to one %: no escape syntax"
    assert config["branch"] == "rel/%(x)s", "not expanded: no interpolation"


def test_a_work_dir_that_init_writes_with_a_percent_sign_round_trips(tmp_path, monkeypatch):
    """The writer (`init`) puts the path in the file unescaped; the reader must give it back."""
    from contextlake.init_cmd import _mirror_ini

    _isolate_globals(monkeypatch, tmp_path)
    work = str(tmp_path / "100%done")
    ini = tmp_path / "written.ini"
    ini.write_text(_mirror_ini(work, "gitlab", "demo-group"))

    assert load_config(str(ini))["work_dir"] == work


@pytest.mark.parametrize("text, line, reason", [
    ("work_dir = /srv/secret-value\n[contextlake]\n", 1, "before any [contextlake] header"),
    ("[contextlake]\ngitlab_group = a\nsecret_key = x\nsecret_key = secret-value\n", 4,
     "set twice"),
    ("[contextlake]\ngitlab_group = a\nsecret-value\n", 3, "not \"key = value\""),
], ids=["no-header", "duplicate-key", "no-equals"])
def test_an_unparsable_config_is_a_config_error_naming_file_and_line(
        tmp_path, monkeypatch, text, line, reason):
    """In 10.0.0rc4 each of these escaped as a raw configparser traceback. The message must name
    the file and line but not repeat the line or name a key: a config file's text is not echoed."""
    _isolate_globals(monkeypatch, tmp_path)
    ini = tmp_path / "broken.ini"
    ini.write_text(text)

    with pytest.raises(ConfigError) as exc:
        load_config(str(ini))

    msg = str(exc.value)
    assert str(ini) in msg
    assert f"(line {line})" in msg
    assert reason in msg
    assert "secret-value" not in msg
    assert "secret_key" not in msg and "gitlab_group" not in msg


def test_a_parse_error_is_raised_from_none(tmp_path, monkeypatch):
    """configparser's exception quotes the offending line. Chained to the ConfigError, it would
    print that line in any traceback, so the error is raised from None."""
    _isolate_globals(monkeypatch, tmp_path)
    ini = tmp_path / "broken.ini"
    ini.write_text("[contextlake]\ngitlab_group = a\njunk line\n")

    with pytest.raises(ConfigError) as exc:
        load_config(str(ini))

    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def _open_with_a_single_byte_default(monkeypatch):
    """Make `open()` without an encoding behave as it does on Windows (the ANSI code page) or
    under LC_ALL=C: a single-byte codec, not UTF-8. configparser opens the file this way;
    since Python 3.10 it passes the placeholder "locale" instead of None."""
    real_open = builtins.open

    def ansi_open(file, mode="r", *args, encoding=None, **kwargs):
        if "b" not in mode and encoding in (None, "locale"):
            encoding = "cp1252"
        return real_open(file, mode, *args, encoding=encoding, **kwargs)

    monkeypatch.setattr(builtins, "open", ansi_open)


def test_a_non_ascii_value_is_read_as_utf_8_under_a_single_byte_default(tmp_path, monkeypatch):
    """`init` writes the file as UTF-8, and the reader used the locale's encoding, so a
    `work_dir` with an accented letter decoded to a two-character mojibake: a different path."""
    _isolate_globals(monkeypatch, tmp_path)
    ini = tmp_path / "accent.ini"
    ini.write_bytes("[contextlake]\nwork_dir = /srv/Jos\u00e9-ws\n".encode("utf-8"))
    _open_with_a_single_byte_default(monkeypatch)

    assert load_config(str(ini))["work_dir"] == "/srv/Jos\u00e9-ws"


def test_a_utf_8_byte_order_mark_does_not_hide_the_section_header(tmp_path, monkeypatch):
    """Some Windows editors save UTF-8 with a BOM, which would otherwise glue to `[contextlake]`
    and read as a setting above the header."""
    _isolate_globals(monkeypatch, tmp_path)
    ini = tmp_path / "bom.ini"
    ini.write_bytes(b"\xef\xbb\xbf[contextlake]\nwork_dir = /srv/ws\n")

    assert load_config(str(ini))["work_dir"] == "/srv/ws"


def test_a_config_that_is_not_utf_8_is_a_config_error_naming_the_file(tmp_path, monkeypatch):
    """In 10.0.0rc4, under a UTF-8 default locale, a file saved in a single-byte code page
    raised UnicodeDecodeError: a traceback, and a message that quotes the bytes. Under a
    single-byte default locale that file was read without error. The message names the file
    and the fix, and not the value."""
    _isolate_globals(monkeypatch, tmp_path)
    ini = tmp_path / "ansi.ini"
    ini.write_bytes(b"[contextlake]\nwork_dir = /srv/Jos\xe9-secret\n")

    with pytest.raises(ConfigError) as exc:
        load_config(str(ini))

    msg = str(exc.value)
    assert str(ini) in msg
    assert "not valid UTF-8" in msg
    assert "secret" not in msg


def test_get_cache_paths_joins_dir_and_names():
    config = {"cache_dir": "/var/cache", "cache_file": "p.txt", "cache_json": "p.json"}
    text, js = get_cache_paths(config)
    assert text == "/var/cache/p.txt"
    assert js == "/var/cache/p.json"


# --- where the projects cache lands ----------------------------------------
#
# The cache lists every repository the account can enumerate, with clone URLs.
# It used to default to /tmp/gitlab_projects.txt: outside the user's home, so no
# HOME-based isolation reached it; world-readable on a shared host; and at a
# fully predictable path another user can pre-create or symlink.

def test_default_cache_lands_under_home_not_tmp(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    cache_file, cache_json = get_cache_paths(load_config())

    assert cache_file.startswith(str(home / ".cache" / "contextlake") + os.sep)
    assert cache_json.startswith(str(home / ".cache" / "contextlake") + os.sep)
    # The old default, verbatim (tmp_path itself lives under /tmp, so a bare
    # "not under /tmp" assertion would be meaningless here).
    assert cache_file != "/tmp/gitlab_projects.txt"


def test_default_cache_honours_xdg_cache_home(tmp_path, monkeypatch):
    """Read at call time, never frozen at import -- DEFAULT_CONFIG is a
    module-level literal, so an expanded value would pin the first HOME seen."""
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    cache_file, _ = get_cache_paths(load_config())

    assert cache_file.startswith(str(tmp_path / "xdg" / "contextlake") + os.sep)


def test_default_cache_directory_is_private(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    cache_file, _ = get_cache_paths(load_config())

    for directory in (os.path.dirname(cache_file),
                      os.path.dirname(os.path.dirname(cache_file))):
        assert os.stat(directory).st_mode & 0o777 == 0o700, directory


def test_default_cache_is_per_workspace_not_one_global_file(tmp_path, monkeypatch):
    """Two unrelated workspaces (and every config in a directory nest) shared
    one /tmp/gitlab_projects.txt, so a directory-scoped config was never
    actually isolated: `mirror status` could report another workspace's fleet
    as its own, from a cache it never wrote."""
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    a, _ = get_cache_paths({"work_dir": str(tmp_path / "a"), "gitlab_group": "team"})
    b, _ = get_cache_paths({"work_dir": str(tmp_path / "b"), "gitlab_group": "team"})
    assert a != b


def test_default_cache_also_separates_two_groups_in_one_workspace(tmp_path, monkeypatch):
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    work = str(tmp_path / "w")

    a, _ = get_cache_paths({"work_dir": work, "gitlab_group": "team-a"})
    b, _ = get_cache_paths({"work_dir": work, "gitlab_group": "team-b"})
    assert a != b


def test_default_cache_path_is_stable_across_calls(tmp_path, monkeypatch):
    """The whole point of a cache is that the next run finds it again."""
    _isolate_globals(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = {"work_dir": str(tmp_path / "w"), "gitlab_group": "team"}

    assert get_cache_paths(config) == get_cache_paths(dict(config))


def test_an_explicitly_configured_cache_dir_is_used_verbatim(tmp_path, monkeypatch):
    """Some users set cache_dir deliberately (including to /tmp), and the audit
    report documented at <cache_dir>/repo_audit.json is written alongside these
    -- naming the directory means "put it exactly here", no subdirectory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    chosen = tmp_path / "chosen"

    cache_file, cache_json = get_cache_paths({"cache_dir": str(chosen),
                                              "work_dir": str(tmp_path / "w"),
                                              "gitlab_group": "team"})

    assert cache_file == str(chosen / "gitlab_projects.txt")
    assert cache_json == str(chosen / "gitlab_projects.json")


def test_an_unwritable_cache_dir_warns_instead_of_raising(tmp_path, monkeypatch, gls_logs):
    """get_cache_paths runs on read paths too; a directory that cannot be made
    must surface as the real read/write error from the code touching the file,
    not as a mkdir traceback from a path helper."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    cache_file, _ = get_cache_paths({"cache_dir": str(blocker / "under")})

    assert cache_file.startswith(str(blocker))
    assert any("cache directory" in r.getMessage() for r in gls_logs.records)


def test_find_ancestor_config_walks_up_from_a_subdirectory(tmp_path):
    """A config at the project root must be discovered from a subdirectory
    several levels deep underneath it -- the whole point of "directory
    inheritance" is that you don't have to be in the exact directory that
    holds the file."""
    (tmp_path / ".contextlake.ini").write_text("[contextlake]\ngitlab_group = root\n")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    found = find_ancestor_config(".contextlake.ini", start=str(deep))
    assert found == str(tmp_path / ".contextlake.ini")


def test_find_ancestor_config_prefers_the_nearest_match(tmp_path):
    """Two ancestors both have the file -- the closer one wins, not the root."""
    (tmp_path / ".contextlake.ini").write_text("[contextlake]\ngitlab_group = root\n")
    mid = tmp_path / "a"
    mid.mkdir()
    (mid / ".contextlake.ini").write_text("[contextlake]\ngitlab_group = mid\n")
    deep = mid / "b"
    deep.mkdir()
    found = find_ancestor_config(".contextlake.ini", start=str(deep))
    assert found == str(mid / ".contextlake.ini")


def test_find_ancestor_config_returns_none_when_nothing_found(tmp_path):
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    assert find_ancestor_config(".contextlake.ini", start=str(deep)) is None


def test_find_ancestor_config_checks_an_absolute_filename_directly_no_walk(tmp_path):
    """An absolute path is the isolation mechanism every existing test uses
    (monkeypatch LOCAL_CONFIG_FILE to a specific tmp path) -- it must never
    walk, only check that exact path, or those tests would start seeing real
    ancestor files from wherever pytest happens to run."""
    missing = tmp_path / "does-not-exist.ini"
    assert find_ancestor_config(str(missing)) is None
    missing.write_text("[contextlake]\n")
    assert find_ancestor_config(str(missing)) == str(missing)


def test_load_config_inherits_local_config_from_a_parent_directory(tmp_path, monkeypatch):
    """End-to-end: cd into a subdirectory of a project that has a root-level
    .contextlake.ini, and load_config() must still pick it up."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    (tmp_path / ".contextlake.ini").write_text(
        "[contextlake]\ngitlab_group = inherited-group\n")
    deep = tmp_path / "sub" / "dir"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    config = load_config()
    assert config["gitlab_group"] == "inherited-group"


def test_a_directory_named_like_the_config_does_not_hide_the_one_in_a_parent(
        tmp_path, monkeypatch):
    """The walk looks for a file. A directory called `.contextlake.ini` in a child directory
    is not a config, so it must not stop the walk: the file in the parent still counts.
    `os.path.exists` said yes to the directory, `find_ancestor_config` returned it, the
    loader skipped it, and the parent's `gitlab_group` was never read."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    parent_file = tmp_path / ".contextlake.ini"
    parent_file.write_text("[contextlake]\ngitlab_group = parent-group\n")
    child = tmp_path / "child"
    (child / ".contextlake.ini").mkdir(parents=True)

    assert find_ancestor_config(".contextlake.ini", start=str(child)) == str(parent_file)

    monkeypatch.chdir(child)
    assert load_config()["gitlab_group"] == "parent-group"
