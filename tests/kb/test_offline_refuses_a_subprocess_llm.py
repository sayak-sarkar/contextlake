"""Offline mode has to refuse the one LLM provider that spawns a process.

`netguard.install()` blocks outbound TCP inside this process. A subprocess owns its own
sockets, so it cannot be reached from there (netguard's own module docstring says so),
and `[llm] provider = "cli"` runs an agent CLI that phones out on its own. The refusal
therefore has to happen before the spawn, at the one place a CliLlm is constructed:
`build_llm`'s cli branch. wiki, docs, `dashboard --llm-chat` and `build_review_llm` all
funnel through it, so one check covers every IN-PROCESS build.

**The other half.** A command that spawns contextlake itself gets a child that runs that
check in its own process, and the child reads its own offline state, not the parent's.
`--offline` as a flag leaves nothing for it to read. `netguard.child_env()` at the spawn
is what carries the state across, and the tests at the bottom of this module cover the
four `kb`-side spawn sites: `kb/dashboard/mutations.py:wiki_generate_start`,
`kb/dashboard/mutations.py:docs_generate_start`, `kb/dashboard/mutations.py:mcp_start`
and `kb/cmds/refresh.py:_spawn_refresh`.

Other spawn sites take no offline signal. They are enumerated in `netguard`'s module
docstring, with the reason each is not a one-line fix. Kept in one place on purpose: an
earlier version of this docstring carried its own copy of the list, and the copy was
short by four sites. Read that list, do not restate it here.

**Why no test here patches sockets.** A socket assertion would prove nothing about the
thing under test. The defect is a child process, and a patched `socket.socket.connect`
in this interpreter is not inherited by one. So these tests assert on the BUILT CLIENT
(the return value of `build_llm`), which is what decides whether a spawn can happen at
all. Nothing here calls `generate()`, so no agent CLI is spawned in the passing run or
in the failing one.
"""

import os
import subprocess
import sys

import pytest

from contextlake import netguard
from contextlake.kb.config import LlmCfg
from contextlake.kb.llm.base import build_llm, build_review_llm


@pytest.fixture
def offline_env(monkeypatch):
    """Offline via the env var, with the install-once latch held down.

    The latch is reset because `offline()` also reads it, and a test that ran
    `netguard.install()` earlier in this worker would otherwise make every case here
    pass for the wrong reason."""
    monkeypatch.setattr(netguard, "_installed", False)
    monkeypatch.setenv(netguard.OFFLINE_ENV, "1")


def test_build_llm_returns_none_for_the_cli_provider_when_offline(offline_env):
    assert build_llm(LlmCfg(enabled=True, provider="cli", command="claude")) is None


def test_the_uppercase_spelling_is_refused_too(offline_env):
    """build_llm lowercases the provider before it dispatches, so the refusal has to sit
    below that. Pinned so a later refactor cannot move the check above the lowercasing."""
    assert build_llm(LlmCfg(enabled=True, provider="CLI")) is None


def test_the_refusal_names_the_provider_and_the_flag(offline_env, caplog):
    """The refusal line is the only thing that connects "no prose was written" to its
    cause, so its content is part of the fix."""
    import logging
    with caplog.at_level(logging.INFO, logger="contextlake"):
        assert build_llm(LlmCfg(enabled=True, provider="cli")) is None
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "cli llm provider" in text
    assert "--offline" in text
    assert netguard.OFFLINE_ENV in text


def test_a_loopback_provider_still_builds_when_offline(offline_env):
    """HONEST LABEL: this passes on the unfixed code too. It is a shape test for the fix,
    not a regression test for the defect. Offline means no egress, not no sockets, and a
    refusal written as "offline means no LLM" would break every local Ollama user. Its
    break-test (widen the refusal to every provider) fails for a reachable mistake."""
    llm = build_llm(LlmCfg(enabled=True, provider="ollama"))
    assert llm is not None
    assert llm.base_url.startswith("http://127.0.0.1")


def test_the_builtin_provider_still_builds_when_offline(offline_env):
    """The other local tier. It downloads a model on first use, which the in-process
    guard covers; it spawns nothing, so it is not this refusal's business."""
    llm = build_llm(LlmCfg(enabled=True, provider="builtin"))
    assert llm is not None
    assert type(llm).__name__ == "BuiltinLlm"


def test_an_offline_review_provider_falls_back_to_the_generator(offline_env):
    """One gate, not two. `build_review_llm` ends in `build_llm(review_cfg) or llm`, so
    the refusal at the resolution point makes a `cli` reviewer fall back to the
    generator. This is why no separate review_provider gate is added."""
    gen = build_llm(LlmCfg(enabled=True, provider="ollama"))
    reviewer = build_review_llm(
        LlmCfg(enabled=True, provider="ollama", review_provider="cli"), gen)
    assert reviewer is gen


def test_the_offline_flag_alone_refuses_the_cli_provider(tmp_path):
    """The wiring test, and the only one that covers `--offline` as a FLAG.

    It runs in a subprocess for a reason stated in netguard.install()'s docstring: the
    guard is one-way and process-wide, so driving `cli.main(["--offline", ...])` in
    process would leave every later test in this pytest worker offline. The env var is
    popped from the child's environment so only the flag can be the cause, and HOME is
    redirected so no developer's or runner's ~/.contextlake/kb.toml can supply [llm].

    LOAD-BEARING SAFETY PROPERTY: the store is left EMPTY on purpose. `cmd_docs` builds
    the LLM and then stops at "No indexed repos", so no agent CLI is spawned in the
    fixed run or the unfixed one. Indexing the fixture first, or moving this to
    `kb wiki`, would make the unfixed run spawn a real `claude`.
    """
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    env = dict(os.environ)
    env.pop(netguard.OFFLINE_ENV, None)
    env["HOME"] = str(tmp_path)

    r = subprocess.run(
        [sys.executable, "-m", "contextlake", "kb", "docs", "--config", str(cfg),
         "--llm", "cli", "--offline"],
        capture_output=True, text=True, timeout=120, cwd=tmp_path, env=env)

    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "offline mode" in out and "cli llm provider" in out, out
    assert "No indexed repos" in out, out


# --- the spawn boundary: a child of an offline parent must be offline too -----

@pytest.fixture
def offline_flag(monkeypatch):
    """Offline via the FLAG route only: the one-way latch, and no env var anywhere.

    This is the precondition the spawn defect needs. With `CONTEXTLAKE_OFFLINE=1` set,
    every child inherits offline mode through `dict(os.environ)` on its own, and these
    two tests would pass against a spawn site that propagates nothing."""
    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    monkeypatch.setattr(netguard, "_installed", True)


def _child_says_offline(env) -> bool:
    """Ask a real child, with the env the spawn site built, whether it is offline.

    Reading the dict would pass for a value the child never acts on. `env=None` means
    "inherit this process's environment", which is what an unfixed spawn site produces,
    and under `offline_flag` that child answers False."""
    probe = "from contextlake import netguard; print(netguard.offline())"
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                       timeout=120, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip() == "True"


def test_the_dashboard_wiki_spawn_carries_offline_to_its_child(
        offline_flag, tmp_path, monkeypatch):
    """The dashboard's Regenerate wiki button spawns `kb wiki`, which builds its own LLM
    in its own process. Without propagation that child constructs a CliLlm and runs an
    agent CLI while the dashboard it came from is offline."""
    from contextlake.kb.dashboard import mutations as mut

    seen = {}
    real_popen = mut.subprocess.Popen

    class _Proc:
        pid = 999999

    def _fake_popen(cmd, **kw):
        # Intercept ONLY the `kb wiki` spawn. The patch lands on the shared stdlib
        # module, so `_child_says_offline`'s own probe reaches this function too and
        # must be handed the real Popen, or the test measures the patch.
        if isinstance(cmd, list) and cmd[:3] == [sys.executable, "-m", "contextlake"]:
            seen["env"] = kw.get("env")
            return _Proc()
        return real_popen(cmd, **kw)

    monkeypatch.setattr(mut.subprocess, "Popen", _fake_popen)

    result = mut.wiki_generate_start(tmp_path)

    assert result["ok"], result
    assert _child_says_offline(seen["env"])


def test_the_dashboard_docs_spawn_carries_offline_to_its_child(
        offline_flag, tmp_path, monkeypatch):
    """The dashboard's Generate documents button spawns `kb docs`. That command is
    model-free by default, and it accepts --llm, so the child can build an LLM in
    its own process from its own config. Without propagation an offline dashboard
    spawns a child that does not know it is offline."""
    from contextlake.kb.dashboard import mutations as mut

    seen = {}
    real_popen = mut.subprocess.Popen

    class _Proc:
        pid = 999998

    def _fake_popen(cmd, **kw):
        # Intercept ONLY the `kb docs` spawn. The patch lands on the shared stdlib
        # module, so `_child_says_offline`'s own probe reaches this function too and
        # must be handed the real Popen, or the test measures the patch.
        if isinstance(cmd, list) and cmd[:3] == [sys.executable, "-m", "contextlake"]:
            seen["env"] = kw.get("env")
            return _Proc()
        return real_popen(cmd, **kw)

    monkeypatch.setattr(mut.subprocess, "Popen", _fake_popen)

    result = mut.docs_generate_start(tmp_path)

    assert result["ok"], result
    assert _child_says_offline(seen["env"])


def test_the_refresh_spawn_carries_offline_to_its_child(
        offline_flag, tmp_path, monkeypatch):
    """`kb refresh --refresh` spawns a detached shell running `kb index`, `kb embed` and
    `kb steer`. `kb embed` downloads a model and reaches a remote endpoint, so without
    propagation the in-process socket guard covers none of that work."""
    from contextlake.kb.cmds import refresh as refresh_mod

    seen = {}
    real_popen = refresh_mod.subprocess.Popen

    def _fake_popen(argv, **kw):
        # Intercept ONLY the detached shell. Same shared-module hazard as above: the
        # probe in `_child_says_offline` arrives here as well.
        if isinstance(argv, list) and argv[:2] == ["/bin/sh", "-c"]:
            seen["env"] = kw.get("env")
            return object()
        return real_popen(argv, **kw)

    monkeypatch.setattr(refresh_mod.subprocess, "Popen", _fake_popen)

    started, _logfile = refresh_mod._spawn_refresh(
        tmp_path, None, ["alpha"], embed=True)

    assert started
    assert _child_says_offline(seen["env"])


# --- what `kb wiki` says after the refusal ------------------------------------

def test_the_wiki_message_names_the_refusal_rather_than_calling_the_tier_disabled():
    """`build_llm` returning None does not mean the tier is off. Under offline mode with
    provider = "cli" the tier is ENABLED and was refused, and "LLM tier disabled" sends
    the reader to a setting that is already true."""
    from contextlake.kb.cmds.wiki import no_llm_message

    text = no_llm_message(LlmCfg(enabled=True, provider="cli"), True)

    assert "disabled" not in text
    assert "Offline mode refused the cli LLM provider" in text


def test_the_wiki_message_does_not_suggest_openai_during_an_offline_run():
    """The socket guard blocks api.openai.com, so suggesting it is advice that cannot
    work. Checked for both ways of reaching the no-LLM branch offline, because the
    disabled-tier branch carried the same line."""
    from contextlake.kb.cmds.wiki import no_llm_message

    refused = no_llm_message(LlmCfg(enabled=True, provider="cli"), True)
    tier_off = no_llm_message(LlmCfg(enabled=False, provider="ollama"), True)

    assert "openai" not in refused
    assert "openai" not in tier_off
    # The local tiers are still offered, or the message leaves no way forward.
    assert "builtin" in refused and "ollama" in refused
    assert "builtin" in tier_off and "ollama" in tier_off


def test_cmd_wiki_hands_the_message_the_real_offline_state(tmp_path, monkeypatch, caplog):
    """The WIRING, which the three message tests above do not reach.

    They call `no_llm_message` directly, so the call site is free to hand it any value.
    Measured: change `netguard.offline()` at wiki.py's call site to `False` and every one
    of them still passes, while a live offline run gets the pre-fix "LLM tier disabled"
    line back. This test drives `cmd_wiki` and reads what it logged.

    Preconditions stated, not inherited. The latch is set and the env var deleted, so the
    offline state comes from one source this test controls. `build_llm` is patched to
    return None at `contextlake.kb.llm`, which is where `cmd_wiki` imports it from
    (the import is inside the function, so patching the `wiki` module binds nothing).
    That also means no provider is constructed and nothing is spawned.
    """
    import logging
    from argparse import Namespace

    from contextlake.kb import llm as llm_mod
    from contextlake.kb.commands import cmd_wiki

    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    monkeypatch.setattr(netguard, "_installed", True)
    monkeypatch.setattr(llm_mod, "build_llm", lambda _cfg: None)

    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
                   '[llm]\nenabled = true\nprovider = "cli"\n')

    with caplog.at_level(logging.INFO, logger="contextlake"):
        assert cmd_wiki(Namespace(config=str(cfg))) == 0
    text = "\n".join(r.getMessage() for r in caplog.records)

    # Proves the capture saw the branch at all, before the negative assertion below.
    assert "Offline mode refused the cli LLM provider" in text, text
    assert "disabled" not in text, text


def test_the_online_wiki_message_is_unchanged():
    """GUARD, not a regression test: this passes on the unfixed code too. An online run
    still gets the full list and the enable-it hint, which is the behaviour the two
    tests above must not have widened into every run."""
    from contextlake.kb.cmds.wiki import no_llm_message

    text = no_llm_message(LlmCfg(enabled=False, provider="ollama"), False)

    assert "LLM tier disabled" in text
    assert "--llm builtin|ollama|openai" in text
    assert "[llm] enabled = true" in text


def test_the_dashboard_mcp_spawn_carries_offline_to_its_child(
        offline_flag, tmp_path, monkeypatch):
    """The third spawn site in the same boundary. `kb serve` answers dashboard chat
    turns, so it builds an LLM in its own process. The token this call mints must still
    reach the child, or offline propagation would have broken authentication."""
    from contextlake.kb.dashboard import mutations as mut
    from contextlake.kb.server import TOKEN_ENV

    seen = {}
    real_popen = mut.subprocess.Popen

    class _Proc:
        pid = 999999

        def poll(self):
            return None

    def _fake_popen(cmd, **kw):
        if isinstance(cmd, list) and cmd[:3] == [sys.executable, "-m", "contextlake"]:
            seen["env"] = kw.get("env")
            return _Proc()
        return real_popen(cmd, **kw)

    monkeypatch.setattr(mut.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(mut.time, "sleep", lambda _s: None)

    started = mut.mcp_start(tmp_path, port=8799)

    assert started["ok"], started
    assert seen["env"][TOKEN_ENV] == started["token"]
    assert _child_says_offline(seen["env"])
