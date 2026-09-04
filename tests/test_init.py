"""Tests for `contextlake init` — the guided config generator."""

import sys
from argparse import Namespace

import pytest

from contextlake import init_cmd, style
from contextlake.config import load_config


def _args(**over):
    # completion=False: shell-completion registration is its own concern with
    # its own dedicated tests below (test_init_shell_completion.py-style, in
    # this file's later section) that explicitly mock HOME/SHELL; every other
    # test here must never touch a real dotfile, on this or any CI machine.
    base = dict(platform=None, group=None, work_dir=None, store_dir=None, kb=None,
                embeddings=False, completion=False, skip_interactive=True, force=False)
    base.update(over)
    return Namespace(**base)


def _run(tmp_path, monkeypatch, **over):
    # Point the writer at an isolated HOME; stdin is not a TTY under pytest, so
    # cmd_init runs non-interactively regardless.
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    return init_cmd.cmd_init(_args(**over))


def test_init_writes_both_configs(tmp_path, monkeypatch):
    rc = _run(tmp_path, monkeypatch, platform="github", group="acme")
    assert rc == 0
    ini = (tmp_path / ".contextlake.ini").read_text()
    assert "platform = github" in ini
    assert "gitlab_group = acme" in ini
    kb = (tmp_path / ".contextlake/kb.toml").read_text()
    assert 'store_dir = "~/.contextlake/kb"' in kb
    assert "enabled = false" in kb  # embeddings off unless asked


def test_init_writes_owner_readable_only_configs(tmp_path, monkeypatch):
    """Generated configs are the user's own: the kb one is what `kb source add`
    later appends connector options to, and neither belongs to every account on
    the machine."""
    rc = _run(tmp_path, monkeypatch, platform="github", group="acme")
    assert rc == 0
    for path in ((tmp_path / ".contextlake.ini"), (tmp_path / ".contextlake/kb.toml")):
        assert oct(path.stat().st_mode & 0o777) == "0o600", path


def test_init_config_flag_redirects_both_generated_files(tmp_path, monkeypatch):
    """init is the one command that writes BOTH config files -- --config (every
    other command's isolation flag) must redirect them, not be silently
    ignored in favor of the real home-directory defaults."""
    # Point the defaults at a decoy path so the test fails loudly (writing to
    # the wrong place) if --config is still being ignored.
    decoy = tmp_path / "decoy"
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(decoy / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(decoy / ".contextlake/kb.toml"))

    custom = tmp_path / "isolated" / "myconfig.ini"
    rc = init_cmd.cmd_init(_args(config=str(custom), platform="github", group="acme"))
    assert rc == 0
    assert "platform = github" in custom.read_text()
    assert (custom.parent / "kb.toml").exists()
    assert not decoy.exists()


def test_init_local_flag_writes_to_cwd_not_global(tmp_path, monkeypatch):
    """--local writes a project-scoped config into cwd instead of ~/ -- found
    via dogfooding: `init` run inside a fresh project directory always fell
    through to the existing global config, with no way to create one scoped
    to just that project."""
    decoy = tmp_path / "decoy"
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(decoy / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(decoy / ".contextlake/kb.toml"))
    project = tmp_path / "myproject"
    project.mkdir()
    monkeypatch.chdir(project)

    rc = init_cmd.cmd_init(_args(local=True, platform="github", group="acme"))
    assert rc == 0
    assert "platform = github" in (project / ".contextlake.ini").read_text()
    assert (project / ".contextlake.kb.toml").exists()
    assert not decoy.exists()


def test_init_local_flag_scopes_store_dir_to_workspace(tmp_path, monkeypatch):
    """--local scopes the mirror workspace to cwd; the KB store should default
    to living alongside it too -- found via dogfooding: a `--local` config's
    kb.toml still pointed store_dir at the global ~/.contextlake/kb, so two
    separate --local workspaces on the same machine silently shared one store."""
    decoy = tmp_path / "decoy"
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(decoy / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(decoy / ".contextlake/kb.toml"))
    project = tmp_path / "myproject"
    project.mkdir()
    monkeypatch.chdir(project)

    rc = init_cmd.cmd_init(_args(local=True, platform="github", group="acme"))
    assert rc == 0
    kb = (project / ".contextlake.kb.toml").read_text()
    assert f'store_dir = "{project / ".contextlake" / "kb"}"' in kb


def test_init_explicit_store_dir_wins_over_local_default(tmp_path, monkeypatch):
    decoy = tmp_path / "decoy"
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(decoy / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(decoy / ".contextlake/kb.toml"))
    project = tmp_path / "myproject"
    project.mkdir()
    monkeypatch.chdir(project)

    custom_store = str(tmp_path / "shared-store")
    rc = init_cmd.cmd_init(
        _args(local=True, platform="github", group="acme", store_dir=custom_store)
    )
    assert rc == 0
    kb = (project / ".contextlake.kb.toml").read_text()
    assert f'store_dir = "{custom_store}"' in kb


def test_init_explicit_config_wins_over_local_flag(tmp_path, monkeypatch):
    """--config always wins, even with --local also passed -- --local is a
    convenience default, not a second override tier."""
    decoy = tmp_path / "decoy"
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(decoy / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(decoy / ".contextlake/kb.toml"))
    project = tmp_path / "myproject"
    project.mkdir()
    monkeypatch.chdir(project)

    custom = tmp_path / "elsewhere" / "myconfig.ini"
    rc = init_cmd.cmd_init(_args(config=str(custom), local=True, platform="github", group="acme"))
    assert rc == 0
    assert custom.exists()
    assert not (project / ".contextlake.ini").exists()
    assert not decoy.exists()


def test_init_writes_the_platform_even_when_it_is_the_default(tmp_path, monkeypatch):
    """The key used to be omitted whenever it equalled the default, on the
    reasoning that the default would supply it. Config LAYERS, so what actually
    supplied it was whatever file sat above this one: `init --local --platform
    gitlab` under a global `platform = github` produced a local config that said
    nothing about the platform, and `mirror clone` went to api.github.com."""
    _run(tmp_path, monkeypatch, platform="gitlab", group="acme")
    assert "platform = gitlab" in (tmp_path / ".contextlake.ini").read_text()


def test_a_generated_local_config_is_not_overridden_by_a_global_one(tmp_path, monkeypatch):
    """End to end, through the real precedence chain: the generated file has to
    win over a global config that names a different forge."""
    from contextlake import config as config_mod
    from contextlake.core import platform_name

    home = tmp_path / "home"
    home.mkdir()
    (home / ".contextlake.ini").write_text(
        "[contextlake]\nplatform = github\ngitlab_group = other\n")
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(home / ".contextlake.ini"))
    monkeypatch.chdir(project)
    assert init_cmd.cmd_init(
        _args(platform="gitlab", group="acme", local=True, kb=False)) == 0

    assert platform_name(config_mod.load_config()) == "gitlab"


def test_init_embeddings_flag_enables_semantic(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, group="acme", embeddings=True)
    assert "enabled = true" in (tmp_path / ".contextlake/kb.toml").read_text()


def test_init_no_kb_writes_only_mirror(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, group="acme", kb=False)
    assert (tmp_path / ".contextlake.ini").exists()
    assert not (tmp_path / ".contextlake/kb.toml").exists()


def test_init_does_not_clobber_without_force(tmp_path, monkeypatch):
    cfg = tmp_path / ".contextlake.ini"
    cfg.write_text("[contextlake]\nwork_dir = /keep/me\n")
    _run(tmp_path, monkeypatch, platform="github", group="acme")
    assert "/keep/me" in cfg.read_text()          # untouched
    assert "platform = github" not in cfg.read_text()


def test_init_force_overwrites(tmp_path, monkeypatch):
    cfg = tmp_path / ".contextlake.ini"
    cfg.write_text("[contextlake]\nwork_dir = /old\n")
    _run(tmp_path, monkeypatch, platform="github", group="acme", force=True)
    assert "gitlab_group = acme" in cfg.read_text()


def test_init_rejects_unknown_platform(tmp_path, monkeypatch):
    rc = _run(tmp_path, monkeypatch, platform="sourceforge", group="acme")
    assert rc == 2
    assert not (tmp_path / ".contextlake.ini").exists()


def test_init_rejects_missing_group_non_interactive(tmp_path, monkeypatch):
    """A `--skip-interactive` init with no --group must refuse rather than
    fabricate a placeholder group (e.g. "your-org") that silently produces an
    unusable config -- neither generated file should exist afterward."""
    rc = _run(tmp_path, monkeypatch, platform="github", group=None)
    assert rc == 2
    assert not (tmp_path / ".contextlake.ini").exists()
    assert not (tmp_path / ".contextlake/kb.toml").exists()


def test_init_never_writes_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret-value")
    _run(tmp_path, monkeypatch, platform="github", group="acme")
    text = (tmp_path / ".contextlake.ini").read_text()
    assert "ghp-secret-value" not in text  # auth is by env-var reference only


def test_generated_config_loads_and_drives_the_tool(tmp_path, monkeypatch):
    # The whole point: what init writes must be valid config the tool reads back.
    ini = tmp_path / ".contextlake.ini"
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(ini))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    init_cmd.cmd_init(_args(platform="bitbucket", group="acme", work_dir=str(tmp_path / "w")))

    cfg = load_config(str(ini))
    assert cfg.get("platform") == "bitbucket"
    assert cfg.get("gitlab_group") == "acme"


@pytest.mark.parametrize("platform,env", [
    ("gitlab", "GITLAB_TOKEN"), ("github", "GITHUB_TOKEN"),
    ("bitbucket", "BITBUCKET_TOKEN"), ("codeberg", "GITEA_TOKEN"),
])
def test_init_reports_the_right_token_env(tmp_path, monkeypatch, gls_logs, platform, env):
    monkeypatch.delenv(env, raising=False)
    _run(tmp_path, monkeypatch, platform=platform, group="acme")
    # the auth hint names the platform's token env var
    assert env in gls_logs.text


def test_init_next_hint_matches_semantic_choice(tmp_path, monkeypatch, gls_logs):
    # Enabling semantic search must recommend [kb-full] (which ships the embedder),
    # not [kb] — otherwise the very next `bootstrap` embed step fails for every repo.
    #
    # The install line only prints when the extra is ABSENT, and a dev checkout always
    # has it. Stated here rather than inherited: without this the assertion is about a
    # message that never appears, and it would pass or fail on what the runner has
    # installed rather than on which extra `init` chose.
    monkeypatch.setattr(init_cmd, "_kb_installed", lambda: False)
    _run(tmp_path, monkeypatch, group="acme", embeddings=True)
    assert 'contextlake[kb-full]' in gls_logs.text
    assert 'contextlake[kb]"' not in gls_logs.text  # the bare-kb hint must not appear


def test_init_next_hint_plain_kb_without_semantic(tmp_path, monkeypatch, gls_logs):
    monkeypatch.setattr(init_cmd, "_kb_installed", lambda: False)   # see the test above
    _run(tmp_path, monkeypatch, group="acme", embeddings=False)
    assert 'contextlake[kb]' in gls_logs.text
    assert 'kb-full' not in gls_logs.text


# --- optional connector prompt ------------------------------------------------

def test_init_non_interactive_skips_connector_prompt_and_writes_no_sources(
        tmp_path, monkeypatch):
    # --skip-interactive (the default in _args) means non-interactive: the
    # prompt is never reached at all.
    _run(tmp_path, monkeypatch, group="acme")
    kb = (tmp_path / ".contextlake/kb.toml").read_text()
    assert "[[sources]]" not in kb


def test_init_connector_prompt_declined_writes_no_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)
    # accept every prompt's own default -- including "Connect a data source?"
    # whose default is False -- exactly like a user who just hits enter throughout.
    monkeypatch.setattr(init_cmd, "_ask_yn", lambda prompt, default: default)
    monkeypatch.setattr(init_cmd, "_ask", lambda prompt, default: default)

    rc = init_cmd.cmd_init(_args(skip_interactive=False, group="acme"))
    assert rc == 0
    kb = (tmp_path / ".contextlake/kb.toml").read_text()
    assert "[[sources]]" not in kb


def test_init_connector_prompt_accepted_adds_source(tmp_path, monkeypatch):
    pytest.importorskip("tomlkit")
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)

    def fake_ask_yn(prompt, default):
        return True if "Connect a data source" in prompt else default

    def fake_ask(prompt, default):
        if "Source type" in prompt:
            return "atlassian"
        if "Source name" in prompt:
            return "jira"
        if "MCP server URL" in prompt:
            return "https://mcp.example.com"
        return default

    monkeypatch.setattr(init_cmd, "_ask_yn", fake_ask_yn)
    monkeypatch.setattr(init_cmd, "_ask", fake_ask)

    rc = init_cmd.cmd_init(_args(skip_interactive=False, group="acme"))
    assert rc == 0
    kb = (tmp_path / ".contextlake/kb.toml").read_text()
    assert 'name = "jira"' in kb
    assert 'type = "atlassian"' in kb
    assert 'mcp = "https://mcp.example.com"' in kb


def test_init_connector_prompt_loops_to_add_multiple_sources(tmp_path, monkeypatch):
    """`init` used to collect exactly one source per run -- adding a second
    meant re-running `contextlake source add` by hand. The prompt now loops
    ("Connect a data source now?" then "Connect another data source?") until
    declined, so several sources can be added in one pass."""
    pytest.importorskip("tomlkit")
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)

    calls = {"connect": 0}

    def fake_ask_yn(prompt, default):
        if "Connect a data source" in prompt or "Connect another data source" in prompt:
            calls["connect"] += 1
            return calls["connect"] <= 2  # accept twice, decline the third
        return default

    sources = iter([("atlassian", "jira"), ("figma", "design")])

    def fake_ask(prompt, default):
        if "Source type" in prompt:
            return _current[0]
        if "Source name" in prompt:
            return _current[1]
        if "MCP server URL" in prompt:
            return default  # accept the suggested default
        return default

    _current = [None, None]

    def fake_ask_wrapper(prompt, default):
        nonlocal _current
        if "Source type" in prompt:
            _current[:] = next(sources)
        return fake_ask(prompt, default)

    monkeypatch.setattr(init_cmd, "_ask_yn", fake_ask_yn)
    monkeypatch.setattr(init_cmd, "_ask", fake_ask_wrapper)

    rc = init_cmd.cmd_init(_args(skip_interactive=False, group="acme"))
    assert rc == 0
    kb = (tmp_path / ".contextlake/kb.toml").read_text()
    assert 'name = "jira"' in kb and 'type = "atlassian"' in kb
    assert 'name = "design"' in kb and 'type = "figma"' in kb
    # the suggested defaults were accepted, not left blank
    assert 'mcp = "https://mcp.atlassian.com/v1/mcp/authv2"' in kb
    assert 'mcp = "https://mcp.figma.com/mcp"' in kb


def test_init_local_connector_prompt_refuses_an_mcp_url_the_loader_would_drop(
        tmp_path, monkeypatch, gls_logs):
    """The other half of the same gate, kept next to the two rows above.

    `--local` writes `.contextlake.kb.toml`, a file contextlake finds by walking
    up from the working directory, and the trust gate strips `mcp` from such a
    file on the next load. So this run must refuse the URL, not tick over a key
    that will be gone.

    Paired here on purpose: the fix that lets the default (global) target
    through is one argument away from letting EVERY target through, and that
    spelling passes both rows above. `tests/kb/test_init_cmd.py` asserts the
    same refusal through a real `load_kb_config`; this row keeps both directions
    under the fixture these rows use.
    """
    pytest.importorskip("tomlkit")
    monkeypatch.chdir(tmp_path)  # --local writes relative paths, into cwd
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)
    # Registering completion appends to the real ~/.bashrc and writes a marker
    # under the real ~/.contextlake. Nothing in this row is about completion.
    monkeypatch.setattr(init_cmd, "_setup_shell_completion", lambda **_kw: None)

    def fake_ask_yn(prompt, default):
        if "Connect a data source" in prompt:
            return True
        if "Connect another data source" in prompt:
            return False
        return default

    def fake_ask(prompt, default):
        if "Source type" in prompt:
            return "atlassian"
        if "Source name" in prompt:
            return "jira"
        if "MCP server URL" in prompt:
            return "https://mcp.internal.example.net/v1/mcp"
        return default

    monkeypatch.setattr(init_cmd, "_ask_yn", fake_ask_yn)
    monkeypatch.setattr(init_cmd, "_ask", fake_ask)

    rc = init_cmd.cmd_init(_args(skip_interactive=False, local=True, group="acme"))
    out = style.strip_ansi(gls_logs.text)
    assert rc == 0
    assert "Refusing to write 'mcp'" in out
    assert "Added jira" not in out, "a refused source was reported as added"
    assert "[[sources]]" not in (tmp_path / ".contextlake.kb.toml").read_text()


def test_init_connector_prompt_never_asks_for_a_secret_value(tmp_path, monkeypatch):
    pytest.importorskip("tomlkit")
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)
    seen_prompts = []

    def fake_ask_yn(prompt, default):
        return True if "Connect a data source" in prompt else default

    def fake_ask(prompt, default):
        seen_prompts.append(prompt)
        if "Source type" in prompt:
            return "atlassian"
        if "Source name" in prompt:
            return "jira"
        return default

    monkeypatch.setattr(init_cmd, "_ask_yn", fake_ask_yn)
    monkeypatch.setattr(init_cmd, "_ask", fake_ask)

    init_cmd.cmd_init(_args(skip_interactive=False, group="acme"))
    assert not any(
        "token" in p.lower() or "secret" in p.lower() or "password" in p.lower()
        for p in seen_prompts
    )


# --- shell completion ------------------------------------------------------
# Dedicated section: every test here explicitly mocks HOME and SHELL so
# nothing ever touches a real dotfile, on this or any CI machine -- the exact
# hazard the default-False `completion` in _args()'s base dict (above) exists
# to prevent for every *other* test in this file.

def test_completion_default_on_writes_bash_eval_line(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    init_cmd._setup_shell_completion(interactive=False, default_on=True)
    rc = (tmp_path / ".bashrc").read_text()
    assert 'eval "$(register-python-argcomplete contextlake)"' in rc


def test_completion_zsh_adds_bashcompinit_first(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    init_cmd._setup_shell_completion(interactive=False, default_on=True)
    rc = (tmp_path / ".zshrc").read_text()
    assert "bashcompinit" in rc
    assert 'eval "$(register-python-argcomplete contextlake)"' in rc


def test_completion_is_idempotent_on_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    init_cmd._setup_shell_completion(interactive=False, default_on=True)
    init_cmd._setup_shell_completion(interactive=False, default_on=True)
    rc = (tmp_path / ".bashrc").read_text()
    assert rc.count("register-python-argcomplete contextlake") == 1


def test_completion_preserves_existing_rc_content(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    (tmp_path / ".bashrc").write_text("export PATH=/my/own/stuff:$PATH\n")
    init_cmd._setup_shell_completion(interactive=False, default_on=True)
    rc = (tmp_path / ".bashrc").read_text()
    assert "export PATH=/my/own/stuff:$PATH" in rc
    assert "register-python-argcomplete contextlake" in rc


def test_completion_off_does_not_touch_any_file(tmp_path, monkeypatch):
    """A decline still never touches the shell rc file -- only the tool's own
    ~/.contextlake/ decision marker (see the dedicated marker tests below),
    which is what lets a later zero-step check know the user already said no
    instead of silently registering completion behind their back."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    init_cmd._setup_shell_completion(interactive=False, default_on=False)
    assert not (tmp_path / ".bashrc").exists()
    assert (tmp_path / ".contextlake" / ".completion_setup_done").read_text().strip() \
        == "declined"


def test_completion_fish_writes_dedicated_completions_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    init_cmd._setup_shell_completion(interactive=False, default_on=True)
    path = tmp_path / ".config" / "fish" / "completions" / "contextlake.fish"
    assert path.exists()
    assert "contextlake" in path.read_text()


def test_completion_unrecognized_shell_warns_not_crashes(tmp_path, monkeypatch, gls_logs):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/tcsh")
    init_cmd._setup_shell_completion(interactive=False, default_on=True)  # must not raise
    assert "unrecognized shell" in gls_logs.text.lower()


def test_completion_registered_marker_names_the_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    init_cmd._setup_shell_completion(interactive=False, default_on=True)
    marker = tmp_path / ".contextlake" / ".completion_setup_done"
    assert marker.read_text().strip() == "registered:zsh"


def test_completion_unrecognized_shell_still_marks_decided(tmp_path, monkeypatch):
    """Even the "nothing sensible to do" branch must record a decision --
    otherwise a user on an unsupported shell would get re-nagged by the
    zero-step auto-check on every single command forever."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/tcsh")
    init_cmd._setup_shell_completion(interactive=False, default_on=True)
    marker = tmp_path / ".contextlake" / ".completion_setup_done"
    assert "unrecognized-shell" in marker.read_text()


# --- `contextlake completion` subcommand + zero-step auto-registration -----

def test_cmd_completion_registers_for_the_detected_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    init_cmd.cmd_completion(Namespace(shell=None))
    assert "register-python-argcomplete" in (tmp_path / ".bashrc").read_text()


def test_cmd_completion_shell_argument_overrides_SHELL_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    init_cmd.cmd_completion(Namespace(shell="bash"))
    assert (tmp_path / ".bashrc").exists()
    assert not (tmp_path / ".zshrc").exists()


def test_cmd_completion_rejects_an_unsupported_shell_name(tmp_path, monkeypatch, gls_logs):
    """Validated here, not via argparse `choices=` -- see cli.py's comment on
    the `shell` positional for why (a real cross-Python-version argparse
    break, caught by CI on 3.9-3.11, not local testing)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = init_cmd.cmd_completion(Namespace(shell="powershell"))
    assert rc == 2
    assert "unknown shell" in gls_logs.text.lower()
    assert not (tmp_path / ".bashrc").exists()
    assert not (tmp_path / ".zshrc").exists()


def test_auto_register_skips_when_already_decided(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTEXTLAKE_NO_AUTO_COMPLETION", raising=False)  # opt back in; see conftest
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    (tmp_path / ".contextlake").mkdir()
    (tmp_path / ".contextlake" / ".completion_setup_done").write_text("declined\n")
    init_cmd.maybe_auto_register_completion()
    assert not (tmp_path / ".bashrc").exists()


def test_auto_register_never_overrides_an_explicit_decline(tmp_path, monkeypatch):
    """The exact scenario the marker exists to prevent: `init --no-completion`
    said no, and a later command must not silently register anyway just
    because it looks (from the rc file alone) like nothing was ever decided."""
    monkeypatch.delenv("CONTEXTLAKE_NO_AUTO_COMPLETION", raising=False)  # opt back in; see conftest
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    init_cmd._setup_shell_completion(interactive=False, default_on=False)  # the decline
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    init_cmd.maybe_auto_register_completion()
    assert not (tmp_path / ".bashrc").exists()


def test_auto_register_skips_when_not_a_real_terminal(tmp_path, monkeypatch):
    """CI, Docker, and piped output all have no shell worth configuring and no
    one watching the log line -- must be a silent no-op, not a registration."""
    monkeypatch.delenv("CONTEXTLAKE_NO_AUTO_COMPLETION", raising=False)  # opt back in; see conftest
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    init_cmd.maybe_auto_register_completion()
    assert not (tmp_path / ".bashrc").exists()
    assert not (tmp_path / ".contextlake" / ".completion_setup_done").exists()


def test_auto_register_skips_silently_under_quiet(tmp_path, monkeypatch):
    """`_setup_shell_completion`'s own contract is to never mutate a dotfile
    silently -- under -q every one of its log() lines disappears, so
    `quiet=True` must skip the whole thing rather than register with zero
    visible notice. The marker must NOT be written either, so it's simply
    retried on the user's next non-quiet run instead of never happening."""
    monkeypatch.delenv("CONTEXTLAKE_NO_AUTO_COMPLETION", raising=False)  # opt back in; see conftest
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    init_cmd.maybe_auto_register_completion(quiet=True)
    assert not (tmp_path / ".bashrc").exists()
    assert not (tmp_path / ".contextlake" / ".completion_setup_done").exists()


def test_auto_register_skips_when_env_var_opts_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("CONTEXTLAKE_NO_AUTO_COMPLETION", "1")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    init_cmd.maybe_auto_register_completion()
    assert not (tmp_path / ".bashrc").exists()
    assert not (tmp_path / ".contextlake" / ".completion_setup_done").exists()


def test_auto_register_fires_once_in_a_real_terminal(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTEXTLAKE_NO_AUTO_COMPLETION", raising=False)  # opt back in; see conftest
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    init_cmd.maybe_auto_register_completion()
    assert "register-python-argcomplete" in (tmp_path / ".bashrc").read_text()
    # second call (e.g. the user's next command) must not re-append
    init_cmd.maybe_auto_register_completion()
    assert (tmp_path / ".bashrc").read_text().count("register-python-argcomplete") == 1


def _init_completion_env(tmp_path, monkeypatch):
    monkeypatch.setattr(init_cmd, "CONFIG_FILE", str(tmp_path / ".contextlake.ini"))
    monkeypatch.setattr(init_cmd, "_KB_CONFIG", str(tmp_path / ".contextlake/kb.toml"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/bash")


def test_init_non_interactive_does_not_touch_a_shell_rc(tmp_path, monkeypatch):
    """`init --skip-interactive` appended a completion block to the user's
    ~/.bashrc (or ~/.zshrc) without asking. Writing to a shell startup file is
    a side effect well outside what `init` implies, and --skip-interactive is
    precisely the flag that says nobody was asked. Real argparse leaves
    `completion` absent from the namespace when neither --completion nor
    --no-completion is given, which is what `del args.completion` simulates --
    cmd_init reads it via getattr(..., None), landing on None (not False)."""
    _init_completion_env(tmp_path, monkeypatch)
    args = _args(group="acme")
    del args.completion  # simulate the real CLI: unset, not explicitly False
    init_cmd.cmd_init(args)
    assert not (tmp_path / "home" / ".bashrc").exists()
    # And no decision marker: recording one here would permanently suppress the
    # offer a later interactive run (or maybe_auto_register_completion) makes.
    assert not (tmp_path / "home" / ".contextlake" / ".completion_setup_done").exists()


def test_init_non_interactive_registers_completion_when_asked(tmp_path, monkeypatch):
    """The explicit opt-in still works: --completion is the user choosing it,
    which is the whole difference from the silent default above."""
    _init_completion_env(tmp_path, monkeypatch)
    init_cmd.cmd_init(_args(group="acme", completion=True))
    rc = tmp_path / "home" / ".bashrc"
    assert rc.exists()
    assert "register-python-argcomplete contextlake" in rc.read_text()


def test_init_interactive_still_offers_completion(tmp_path, monkeypatch):
    """Interactive is where the user *is* asked, so the offer (defaulting to
    yes) must survive -- the fix narrows the non-interactive path only."""
    _init_completion_env(tmp_path, monkeypatch)
    monkeypatch.setattr(init_cmd, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # accept every default
    args = _args(group="acme", skip_interactive=False)
    del args.completion
    init_cmd.cmd_init(args)
    rc = tmp_path / "home" / ".bashrc"
    assert rc.exists()
    assert "register-python-argcomplete contextlake" in rc.read_text()
