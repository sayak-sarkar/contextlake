"""A discovered .contextlake.ini may not choose where the forge token goes.

The incident: load_config merges the nearest ancestor's .contextlake.ini, found by
walking up from cwd. A repository the mirror cloned can carry one. It set
gitlab_host to a host of its choosing and token_env to any variable in the
environment, and the next `mirror fetch` run from inside that tree sent the
variable's value there as PRIVATE-TOKEN. Reproduced against a local HTTP server
before the fix (see the exec3 mirror REPORT).

These tests use the real ancestor walk (LOCAL_CONFIG_FILE stays relative) from a
tree under tmp_path, with the global file pointed at a path the test controls.
"""

import os
from pathlib import Path

import pytest

from contextlake import config as cfgmod
from contextlake import core
from contextlake.config import (
    CACHE_LOCATION_KEYS,
    FORGE_CREDENTIAL_KEYS,
    FORGE_TOKEN_REFUSED,
    get_cache_paths,
    load_config,
)

PLANTED = (
    "[contextlake]\n"
    "gitlab_group = victim-group\n"
    "platform = gitlab\n"
    "work_dir = {work}\n"
    "gitlab_host = http://127.0.0.1:9\n"
    "api_base = http://127.0.0.1:9/api\n"
    "token_env = SOME_OTHER_SECRET\n"
    "gitlab_token_env = SOME_OTHER_SECRET\n"
)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A cloned repo holding a planted .contextlake.ini, cwd two levels inside it,
    and an empty global config. The secret and GITLAB_TOKEN are both set, so a
    None token can only come from the gate."""
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", str(tmp_path / "home" / ".contextlake.ini"))
    monkeypatch.setattr(cfgmod, "LOCAL_CONFIG_FILE", ".contextlake.ini")
    monkeypatch.delenv(cfgmod.NO_LOCAL_CONFIG_ENV, raising=False)
    repo = tmp_path / "work" / "cloned-repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    planted = repo / ".contextlake.ini"
    planted.write_text(PLANTED.format(work=tmp_path / "work"))
    monkeypatch.chdir(repo / "src" / "pkg")
    monkeypatch.setenv("SOME_OTHER_SECRET", "dummy-planted")
    monkeypatch.setenv("GITLAB_TOKEN", "dummy-gitlab")
    return planted


def _warnings(gls_logs):
    return [r.getMessage() for r in gls_logs.records if r.levelname == "WARNING"]


def test_discovered_file_cannot_choose_the_forge_host_or_token_variable(tree, gls_logs):
    config = load_config()

    for key in FORGE_CREDENTIAL_KEYS:
        assert key not in config, key
    # Where the REST call goes, and what it carries.
    assert core._gitlab_api_base(config) == "https://gitlab.com"
    assert core._gitlab_token(config) is None
    assert core._platform_token(config) is None
    assert core._git_auth_env(config) is None
    assert config[FORGE_TOKEN_REFUSED]

    warned = _warnings(gls_logs)
    for key in FORGE_CREDENTIAL_KEYS:
        assert any(f"ignoring {key} from {tree}" in w for w in warned), key
    # The value is the file author's text and never reaches the log.
    assert not any("127.0.0.1" in w or "SOME_OTHER_SECRET" in w for w in warned)


def test_the_planted_host_never_receives_a_request(tree, monkeypatch):
    """The conclusion, not only the config: fetch makes no HTTP request at all.

    With the token off, GitLab enumeration falls back to glab (stubbed here so
    no real forge is called). Before the fix this run called urlopen on the
    planted host with the secret as PRIVATE-TOKEN.
    """
    config = load_config()
    requests = []
    monkeypatch.setattr(core._OPENER, "open",
                        lambda req, **kw: requests.append(req) or pytest.fail("HTTP call"))
    glab_pages = []
    monkeypatch.setattr(core, "_fetch_projects_page_glab",
                        lambda group_enc, per_page, page: glab_pages.append(page) or [])

    core.fetch_gitlab_projects("victim-group", config)

    assert requests == []
    assert glab_pages == [1]


def test_honest_project_local_keys_keep_working(tmp_path, tree, gls_logs):
    """work_dir, gitlab_group and platform are what the owner's local files set."""
    config = load_config()
    assert config["gitlab_group"] == "victim-group"
    assert config["platform"] == "gitlab"
    assert config["work_dir"] == str(tmp_path / "work")


def test_owner_shaped_local_file_has_no_warning_and_keeps_the_token(
        tmp_path, tree, gls_logs):
    tree.write_text(f"[contextlake]\ngitlab_group = acme\nplatform = gitlab\n"
                    f"work_dir = {tmp_path / 'work'}\n")
    config = load_config()
    assert FORGE_TOKEN_REFUSED not in config
    assert core._gitlab_token(config) == "dummy-gitlab"
    assert not any("ignoring" in w for w in _warnings(gls_logs))


def test_default_section_is_gated_too(tree):
    """configparser folds [DEFAULT] into every section's view."""
    tree.write_text("[DEFAULT]\nGITLAB_HOST = http://127.0.0.1:9\n"
                    "[contextlake]\ngitlab_group = acme\n")
    config = load_config()
    assert "gitlab_host" not in config
    assert core._gitlab_api_base(config) == "https://gitlab.com"
    assert config[FORGE_TOKEN_REFUSED] == "gitlab_host"


def test_the_global_file_found_by_the_walk_is_still_the_global_file(tmp_path, monkeypatch,
                                                                    gls_logs):
    """Run from inside HOME, the ancestor walk returns ~/.contextlake.ini itself."""
    home = tmp_path / "home"
    (home / "projects" / "x").mkdir(parents=True)
    global_ini = home / ".contextlake.ini"
    global_ini.write_text("[contextlake]\ngitlab_group = acme\n"
                          "gitlab_host = gitlab.corp.example\ntoken_env = CORP_TOKEN\n")
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", str(global_ini))
    monkeypatch.setattr(cfgmod, "LOCAL_CONFIG_FILE", ".contextlake.ini")
    monkeypatch.delenv(cfgmod.NO_LOCAL_CONFIG_ENV, raising=False)
    monkeypatch.chdir(home / "projects" / "x")
    monkeypatch.setenv("CORP_TOKEN", "dummy-corp")

    config = load_config()
    found = cfgmod.find_ancestor_config(".contextlake.ini")
    assert os.path.realpath(found) == os.path.realpath(global_ini)
    assert config["gitlab_host"] == "gitlab.corp.example"
    assert core._gitlab_token(config) == "dummy-corp"
    assert FORGE_TOKEN_REFUSED not in config
    assert _warnings(gls_logs) == []


def test_naming_the_discovered_file_with_config_keeps_its_keys(tree, gls_logs):
    config = load_config(str(tree))
    assert config["gitlab_host"] == "http://127.0.0.1:9"
    assert FORGE_TOKEN_REFUSED not in config
    assert not any("ignoring" in w for w in _warnings(gls_logs))


def test_a_refused_key_the_global_file_also_sets_keeps_the_token(
        tmp_path, tree, gls_logs, monkeypatch):
    """The fallback is then a value the user chose, so nothing was substituted."""
    global_ini = tmp_path / "home" / ".contextlake.ini"
    global_ini.parent.mkdir(parents=True)
    global_ini.write_text("[contextlake]\ngitlab_host = gitlab.corp.example\n"
                          "api_base = https://gitlab.corp.example\n"
                          "token_env = CORP_TOKEN\ngitlab_token_env = CORP_TOKEN\n")
    monkeypatch.setenv("CORP_TOKEN", "dummy-corp")

    config = load_config()
    assert config["gitlab_host"] == "gitlab.corp.example"
    assert FORGE_TOKEN_REFUSED not in config
    assert core._gitlab_token(config) == "dummy-corp"
    warned = _warnings(gls_logs)
    assert any("ignoring gitlab_host" in w for w in warned)
    assert not any("forge token is off" in w for w in warned)


def test_gitlab_host_env_counts_as_a_chosen_host(tree, monkeypatch):
    tree.write_text("[contextlake]\ngitlab_group = acme\ngitlab_host = http://127.0.0.1:9\n")
    monkeypatch.setenv("GITLAB_HOST", "gitlab.corp.example")
    config = load_config()
    assert FORGE_TOKEN_REFUSED not in config
    assert core._gitlab_api_base(config) == "https://gitlab.corp.example"
    assert core._gitlab_token(config) == "dummy-gitlab"


def test_non_gitlab_platform_is_gated_the_same_way(tree, monkeypatch):
    tree.write_text("[contextlake]\ngitlab_group = acme\nplatform = gitea\n"
                    "api_base = http://127.0.0.1:9\ntoken_env = SOME_OTHER_SECRET\n")
    monkeypatch.setenv("GITEA_TOKEN", "dummy-gitea")
    config = load_config()
    assert core._platform_api_base(config) == "https://gitea.com"
    assert core._platform_token(config) is None
    assert core._git_auth_env(config) is None


def test_the_docs_trust_table_names_every_gated_key():
    """Tied to the constant, so a key added to the gate cannot drift out of the docs.
    Checked per table row, because prose mentioning a key would pass a bare search."""
    page = (Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text(
        encoding="utf-8")
    section = page.split("### The mirror config, `.contextlake.ini`", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.lstrip().startswith("|")]
    missing = [k for k in FORGE_CREDENTIAL_KEYS if not any(f"`{k}`" in r for r in rows)]
    assert rows and not missing, missing


def test_warning_text(tree, gls_logs):
    """The exact sentences the user sees, pinned so a reword is a decision."""
    tree.write_text("[contextlake]\ngitlab_group = acme\ngitlab_host = h\ntoken_env = T\n")
    load_config()
    warned = _warnings(gls_logs)
    cfg_file = cfgmod.CONFIG_FILE
    assert warned == [
        f"config: ignoring gitlab_host from {tree} -- a config file found by walking up "
        "from the current directory may not choose which host receives the forge token, "
        "or which environment variable holds it. "
        f"Set it in {cfg_file} instead, or pass `--config {tree}` to say you meant this "
        "file. The GITLAB_HOST environment variable also sets the host.",
        f"config: ignoring token_env from {tree} -- a config file found by walking up "
        "from the current directory may not choose which host receives the forge token, "
        "or which environment variable holds it. "
        f"Set it in {cfg_file} instead, or pass `--config {tree}` to say you meant this "
        "file.",
        f"config: the forge token is off for this run. {tree} set gitlab_host, token_env, "
        "and no file you chose sets those keys instead, so a built-in default would take "
        "their place: the platform's public host, or its standard token variable. Clones "
        "and fetches run without the token. To use it here, delete those keys from "
        f"{tree} and set those keys in {cfg_file}, or pass `--config {tree}`.",
    ]


# --- a discovered file may not choose where the project cache is written ------

@pytest.fixture
def victim(tmp_path):
    """A file the user did not choose to have rewritten, with known contents."""
    path = tmp_path / "victim.gitconfig"
    path.write_text("ORIGINAL\n")
    return path


def _write_the_caches(config):
    """What `mirror fetch` does last: write the project list to the configured paths."""
    cache_file, cache_json = get_cache_paths(config)
    core._write_caches({"demo/svc": {"ssh": "s", "http": "h", "default_branch": "main",
                                     "archived": False}}, cache_json, cache_file)
    return cache_file, cache_json


def test_discovered_file_cannot_point_the_cache_at_another_file(tmp_path, tree, victim):
    """An absolute cache_json used to win `os.path.join`, and the fetch replaced the file."""
    explicit = tmp_path / "explicit.ini"
    explicit.write_text(f"[contextlake]\ngitlab_group = acme\ncache_dir = {tmp_path / 'chosen'}\n")
    tree.write_text(f"[contextlake]\ngitlab_group = acme\ncache_json = {victim}\n"
                    f"cache_file = {victim}.txt\n")

    config = load_config(str(explicit))
    cache_file, cache_json = _write_the_caches(config)

    assert victim.read_text() == "ORIGINAL\n"
    assert not (tmp_path / "victim.gitconfig.txt").exists()
    assert cache_json == str(tmp_path / "chosen" / "gitlab_projects.json")
    assert cache_file == str(tmp_path / "chosen" / "gitlab_projects.txt")


def test_discovered_file_cannot_move_the_cache_directory_either(tmp_path, tree, victim):
    """cache_dir plus a bare name reaches the same file, so the name alone is not the gate."""
    tree.write_text(f"[contextlake]\ngitlab_group = acme\ncache_dir = {tmp_path}\n"
                    f"cache_json = {victim.name}\n")

    config = load_config()
    cache_file, cache_json = _write_the_caches(config)

    assert victim.read_text() == "ORIGINAL\n"
    assert cache_json.startswith(str(tmp_path / "isolated-home" / ".cache" / "contextlake"))
    assert cache_file.startswith(str(tmp_path / "isolated-home" / ".cache" / "contextlake"))


def test_refused_cache_keys_are_named_without_their_values_and_the_token_stays_on(
        tmp_path, tree, victim, gls_logs):
    tree.write_text(f"[contextlake]\ngitlab_group = acme\n"
                    f"cache_dir = {tmp_path / 'planted-cache-dir'}\n"
                    f"cache_json = {victim}\ncache_file = {victim}.txt\n")

    config = load_config()

    warned = _warnings(gls_logs)
    for key in CACHE_LOCATION_KEYS:
        assert any(f"ignoring {key} from {tree}" in w for w in warned), key
    assert not any("victim" in w or "planted-cache-dir" in w for w in warned)
    assert not any("forge token is off" in w for w in warned)
    assert FORGE_TOKEN_REFUSED not in config
    assert core._gitlab_token(config) == "dummy-gitlab"


def test_files_the_user_chose_keep_their_cache_keys(tmp_path, tree, gls_logs, monkeypatch):
    """Naming the discovered file with --config, or setting the keys globally, still works."""
    tree.write_text(f"[contextlake]\ngitlab_group = acme\ncache_dir = {tmp_path / 'proj'}\n"
                    "cache_json = proj.json\ncache_file = proj.txt\n")

    config = load_config(str(tree))
    assert get_cache_paths(config) == (str(tmp_path / "proj" / "proj.txt"),
                                       str(tmp_path / "proj" / "proj.json"))
    assert not any("ignoring" in w for w in _warnings(gls_logs))

    global_ini = tmp_path / "isolated-home-global.ini"
    global_ini.write_text(f"[contextlake]\ncache_dir = {tmp_path / 'mine'}\n"
                          "cache_json = mine.json\ncache_file = mine.txt\n")
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", str(global_ini))
    config = load_config()
    assert get_cache_paths(config) == (str(tmp_path / "mine" / "mine.txt"),
                                       str(tmp_path / "mine" / "mine.json"))
    warned = _warnings(gls_logs)
    for key in CACHE_LOCATION_KEYS:   # the discovered file is still refused beside the global one
        assert any(f"ignoring {key} from {tree}" in w for w in warned), key


def test_the_docs_trust_table_names_every_gated_cache_key():
    page = (Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text(
        encoding="utf-8")
    section = page.split("### The mirror config, `.contextlake.ini`", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.lstrip().startswith("|")]
    missing = [k for k in CACHE_LOCATION_KEYS if not any(f"`{k}`" in r for r in rows)]
    assert rows and not missing, missing
