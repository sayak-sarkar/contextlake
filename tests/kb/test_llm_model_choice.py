"""The wiki model choice: asked on a terminal, the largest that fits otherwise, saved once.

Owner decision 5. The machine and the model cache are faked; the config file is real.
"""

from __future__ import annotations

import re
from argparse import Namespace
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from contextlake import netguard
from contextlake.kb.llm import model_check, model_choice
from contextlake.kb.llm.catalogue import DEFAULT, MODELS
from contextlake.kb.llm.machine import Machine

GB = 1_000_000_000
ONE_5B, THREE_B, FOURTEEN_B = MODELS[1], MODELS[2], MODELS[4]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """12 GB free (3B is the largest that fits), nothing on disk, no pages to plan."""
    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    state = {"machine": Machine(8, 16 * GB, 12 * GB, 500 * GB), "disk": {}}
    monkeypatch.setattr("contextlake.kb.llm.machine.read_machine",
                        lambda cache_dir: state["machine"])
    monkeypatch.setattr(model_check, "on_disk", lambda cache_dir: dict(state["disk"]))
    monkeypatch.setattr(model_check, "planned_pages", lambda store, store_dir, ids=None: (2, 0))
    state["config"] = tmp_path / "kb.toml"
    return state


def _cfg():
    return Namespace(llm=Namespace(cache_dir=None, council_size=3))


def _choose(world, *, ask, replies=(), monkeypatch=None):
    if replies:
        answers = iter(replies)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    lines = []
    chosen = model_choice.choose_wiki_model(None, None, _cfg(), config_path=str(world["config"]),
                                            ask=ask, log=lines.append)
    return chosen, "\n".join(lines)


def _saved(world):
    if not world["config"].exists():
        return None
    return tomllib.loads(world["config"].read_text()).get("llm", {}).get("builtin_model")


def test_no_terminal_takes_the_largest_that_fits_and_saves_it(world):
    chosen, out = _choose(world, ask=False)
    assert chosen is THREE_B
    assert "Using 3B (a 1.8 GB download), the largest that fits" in out
    assert _saved(world) == THREE_B.repo_id


def test_the_saved_choice_keeps_the_rest_of_the_file(world):
    world["config"].write_text('# mine\n[kb]\nstore_dir = "/s"\n\n[llm]\nprovider = "auto"\n')
    _choose(world, ask=False)
    text = world["config"].read_text()
    assert "# mine" in text and 'provider = "auto"' in text
    assert _saved(world) == THREE_B.repo_id


def test_offline_picks_only_a_model_on_disk(world, monkeypatch):
    monkeypatch.setenv(netguard.OFFLINE_ENV, "1")
    world["disk"] = {ONE_5B.repo_id: True}
    chosen, out = _choose(world, ask=False)
    assert chosen is ONE_5B and "already on disk" in out


def test_nothing_fits_falls_back_to_the_default_and_says_so(world):
    world["machine"] = Machine(8, 2 * GB, 1 * GB, 500 * GB)
    chosen, out = _choose(world, ask=False)
    assert chosen is DEFAULT and "no model fits" in out


def test_no_terminal_pick_does_not_prefer_the_model_on_disk(world):
    """The full Docker image bakes only the 0.5B. An unattended run still takes the largest
    size that fits and downloads it (owner decision 5), and the docs below say so."""
    world["disk"] = {DEFAULT.repo_id: True}
    chosen, out = _choose(world, ask=False)
    assert chosen is THREE_B and "a 1.8 GB download" in out


_REPO = Path(__file__).resolve().parents[2]


def _flat(rel: str) -> str:
    """A file as one line of words, so a sentence wrapped over lines or `#` comments matches."""
    return re.sub(r"\s+", " ", (_REPO / rel).read_text(encoding="utf-8").replace("#", " "))


def test_the_docs_say_the_docker_image_can_still_download_a_model():
    """A page that says the full image needs no model download at runtime sent a host with no
    network to a failed 3B download, which was then saved as its wiki model. The Dockerfile
    LABEL text is scanned too: it called the full image an "offline turnkey image"."""
    pages =[*sorted(p.relative_to(_REPO).as_posix() for p in (_REPO / "docs").glob("*.md")),
             "README.md", "QUICKSTART.md", "Dockerfile", "docker/prefetch_models.py"]
    claim = re.compile(r"no (?:wiki )?model download at runtime|nothing downloads at runtime"
                       r"|offline turnkey|air-gapped use(?! with `CONTEXTLAKE_OFFLINE)", re.I)
    bad = [f"{rel}: {m.group(0)}" for rel in pages for m in [claim.search(_flat(rel))] if m]
    assert not bad, "a page says the image never downloads a model:\n  " + "\n  ".join(bad)
    way_out = f"--llm-model {DEFAULT.repo_id}"
    for rel in ("docs/installing.md", "docs/model-providers.md"):
        assert way_out in _flat(rel), f"{rel} does not say how to stay on the baked model"


def test_on_a_terminal_enter_takes_the_largest_that_fits(world, monkeypatch):
    chosen, out = _choose(world, ask=True, replies=["", ""], monkeypatch=monkeypatch)
    assert chosen is THREE_B and "Whole run" in out
    assert _saved(world) == THREE_B.repo_id


def test_a_model_that_does_not_fit_needs_a_yes(world, monkeypatch):
    """14B is refused once ("n"), then 1.5B is picked and the save is declined."""
    chosen, _out = _choose(world, ask=True, replies=["5", "n", "2", "n"],
                           monkeypatch=monkeypatch)
    assert chosen is ONE_5B
    assert _saved(world) is None


def test_a_failed_save_does_not_stop_the_run(world, monkeypatch):
    def refuse(path, repo_id):
        raise OSError("read-only file system")

    monkeypatch.setattr("contextlake.kb.config_edit.set_builtin_model", refuse)
    chosen, out = _choose(world, ask=False)
    assert chosen is THREE_B and "Could not save" in out


# --- the kb wiki hook ------------------------------------------------------------------

def test_kb_wiki_picks_once_then_reuses_the_saved_model(world, tmp_path, monkeypatch):
    import contextlake.kb.llm as llm_pkg
    from contextlake.kb.cmds.wiki import cmd_wiki
    from contextlake.kb.llm.builtin import BuiltinLlm
    from contextlake.kb.model import Node, Repo
    from contextlake.kb.state import check_schema
    from contextlake.kb.store.shards import GraphShard, write_shard
    from contextlake.kb.store.sqlite_store import SqliteStore

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    world["config"].write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n\n'
                               '[embeddings]\nenabled = false\n\n'
                               '[llm]\nenabled = true\nprovider = "builtin"\n')
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="acme/api", path=str(tmp_path)))
    nodes = [Node(id=f"n{i}", repo="acme/api", kind="function", name=f"handle{i}",
                  file=f"app/f{i}.py") for i in range(4)]
    store.upsert_nodes("acme/api", nodes)
    store.close()
    write_shard(store_dir, GraphShard(repo="acme/api", head_commit="h1", nodes=nodes))

    built = []

    def build(cfg):
        from contextlake.kb.llm.base import _build_builtin_llm

        llm = _build_builtin_llm(cfg)   # the real reader of model and builtin_model
        assert isinstance(llm, BuiltinLlm)
        llm.preflight = lambda: None
        llm.generate = lambda prompt, *, system=None: '{"score": 0.95, "issues": []}' \
            if "Review lens" in prompt else __import__("wiki_doubles").sound_draft(prompt)
        built.append(llm)
        return llm

    monkeypatch.setattr(llm_pkg, "build_llm", build)
    monkeypatch.setattr("contextlake.kb.llm.model_choice.interactive", lambda: False)
    assert cmd_wiki(Namespace(config=str(world["config"]), force=True)) == 0
    assert built[-1].repo_id == THREE_B.repo_id
    assert _saved(world) == THREE_B.repo_id

    def no_second_pick(*a, **k):
        raise AssertionError("a saved model was picked again")

    monkeypatch.setattr(model_choice, "choose_wiki_model", no_second_pick)
    assert cmd_wiki(Namespace(config=str(world["config"]), force=True)) == 0
    assert built[-1].repo_id == THREE_B.repo_id


# --- init ------------------------------------------------------------------------------

def test_init_writes_the_chosen_model_as_an_active_llm_table():
    from contextlake.init_cmd import _kb_toml

    parsed = tomllib.loads(_kb_toml(True, "/s", THREE_B.repo_id))
    assert parsed["llm"] == {"enabled": True, "provider": "builtin",
                             "builtin_model": THREE_B.repo_id}
    assert "llm" not in tomllib.loads(_kb_toml(True, "/s"))


def test_init_offers_the_table_without_page_totals(world, monkeypatch, gls_logs):
    from contextlake import init_cmd

    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert init_cmd._pick_wiki_model() == THREE_B.repo_id
    assert "Fits" in gls_logs.text and "Whole run" not in gls_logs.text


# --- the saved key is the built-in model's alone --------------------------------------

def test_the_saved_size_reaches_only_the_builtin_model(monkeypatch):
    """Saved under `model`, the pick was sent to Ollama as a model name by `--llm ollama`,
    and `auto` asked Ollama for it and never picked Ollama again."""
    from contextlake.kb.config import LlmCfg
    from contextlake.kb.llm import base
    from contextlake.kb.llm.builtin import BuiltinLlm
    from contextlake.kb.llm.ollama import OllamaLlm

    saved = {"enabled": True, "builtin_model": THREE_B.repo_id}
    ollama = base.build_llm(LlmCfg(provider="ollama", **saved))
    assert isinstance(ollama, OllamaLlm) and ollama.model == "llama3.1"

    monkeypatch.setattr(base, "ollama_reachable", lambda *a, **k: True)
    monkeypatch.setattr(base, "ollama_has_model", lambda url, model: model == "llama3.1")
    assert isinstance(base.build_llm(LlmCfg(provider="auto", **saved)), OllamaLlm)

    builtin = base.build_llm(LlmCfg(provider="builtin", **saved))
    assert isinstance(builtin, BuiltinLlm) and builtin.repo_id == THREE_B.repo_id
    explicit = base.build_llm(LlmCfg(provider="builtin", model=ONE_5B.repo_id, **saved))
    assert explicit.repo_id == ONE_5B.repo_id, "an explicit model must win over the saved pick"
