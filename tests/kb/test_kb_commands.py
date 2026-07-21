"""CLI integration tests for the knowledge-layer verbs (the Phase 2.0 DoD)."""

from pathlib import Path

import pytest

from contextlake.cli import main
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "fixtures" / "sample-graph.json"


def _kb_config(tmp_path) -> Path:
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    return cfg


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def test_index_then_query_round_trip(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0

    # the store on disk is populated
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    assert store.get_node("demo_app_orderservice").name == "OrderService"
    assert store.stats().edges == 1
    store.close()

    # query finds it (cited)
    capsys.readouterr()
    assert _run(["query", "OrderService", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "OrderService" in out and "demo/app" in out


def test_index_workspace_indexes_each_repo(tmp_path):
    ws = tmp_path / "ws"
    (ws / "r1" / ".git").mkdir(parents=True)
    (ws / "r1" / "a.py").write_text("def f():\n    pass\n")
    (ws / "r2" / ".git").mkdir(parents=True)
    (ws / "r2" / "b.py").write_text("class C:\n    def m(self):\n        pass\n")
    cfg = _kb_config(tmp_path)

    assert _run(["index", "--config", str(cfg), "--workspace", str(ws)]) == 0
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    assert {r.id for r in store.list_repos()} == {"r1", "r2"}
    assert store.nodes_by_name("f") and store.nodes_by_name("C")
    store.close()


def test_index_empty_workspace_fails_honestly(tmp_path, capsys):
    # 0 repos indexed = an empty graph no agent can cite from; that must be a
    # loud non-zero exit, not a green checkmark (it also makes bootstrap abort).
    ws = tmp_path / "empty-ws"
    ws.mkdir()
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--workspace", str(ws)]) == 1
    assert "No git repositories found" in capsys.readouterr().out


def test_index_without_source_indexes_cwd(tmp_path, monkeypatch):
    cfg = _kb_config(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("def widget():\n    pass\n")
    monkeypatch.chdir(proj)  # no --source/--workspace -> index the current directory
    assert _run(["index", "--config", str(cfg)]) == 0
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    assert store.nodes_by_name("widget")  # cwd got indexed
    assert {r.id for r in store.list_repos()} == {"proj"}  # repo id = cwd dir name
    store.close()


def test_index_missing_source_errors_cleanly(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    code = _run(["index", "--config", str(cfg), "--source", str(tmp_path / "nope.json")])
    out = capsys.readouterr().out
    assert code == 1
    assert "Traceback" not in out and "Cannot read" in out


def test_index_invalid_shard_errors_cleanly(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text('{"nodes": []}')  # valid JSON, missing required 'repo'
    code = _run(["index", "--config", str(cfg), "--source", str(bad)])
    out = capsys.readouterr().out
    assert code == 1
    assert "Traceback" not in out and "not a valid graph shard" in out


def test_query_without_text_is_usage_error(tmp_path):
    cfg = _kb_config(tmp_path)
    assert _run(["query", "--config", str(cfg)]) == 2


def test_doctor_reports_ok(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out.lower()
    assert "doctor" in out
    assert "fts5" in out
    assert code == 0  # git + fts5 present in the test environment


def test_doctor_reports_builtin_model_presence(tmp_path, capsys):
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{store_dir}"\n'
        '[embeddings]\nenabled = true\nprovider = "builtin"\n'
        '[llm]\nenabled = true\nprovider = "builtin"\n'
    )
    _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    # filesystem-only presence report, no download in the test
    assert "built-in embedder model" in out
    assert "potion-base-8M" in out
    assert "Qwen2.5-0.5B-Instruct-GGUF" in out
    assert "not downloaded" in out


def test_doctor_reports_per_source_reachability(tmp_path, capsys, monkeypatch):
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{store_dir}"\n'
        '[[sources]]\ntype = "atlassian"\nname = "jira"\nmcp = "https://x"\n'
        '[[sources]]\ntype = "figma"\nname = "designs"\nmcp = "https://y"\n'
    )
    # cmd_doctor imports verify_source lazily from source_cmd at call time (it
    # stays off commands.py's own import graph, see the tomlkit-eagerness
    # test below), so the patch target is source_cmd, not commands.
    from contextlake.kb import source_cmd

    calls = []

    def fake_verify_source(src, timeout=None):
        calls.append(timeout)
        if src.name == "jira":
            return True, "2 site(s) reachable"
        return False, "MCP configured, but design file 'X' was not reachable"

    monkeypatch.setattr(source_cmd, "verify_source", fake_verify_source)
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "jira" in out and "atlassian" in out and "2 site(s) reachable" in out
    assert "designs" in out and "figma" in out and "not reachable" in out
    # a source being unreachable is advisory -- it never fails doctor's verdict
    assert code == 0
    # doctor bounds every per-source reachability call so an unreachable
    # connector can't stall it at the connector's own default timeout
    assert calls == [8, 8]


def test_doctor_source_with_no_reachability_check_is_advisory_not_fatal(
        tmp_path, capsys):
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{store_dir}"\n'
        '[[sources]]\ntype = "gitlab"\nname = "gl"\n'
    )
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "gl" in out and "gitlab" in out
    assert "no reachability check" in out
    assert code == 0


def test_read_only_commands_do_not_import_tomlkit_eagerly(tmp_path):
    """commands.py must not pull in tomlkit merely by being imported, or by
    running a read-only command (query/index) -- config_edit's "read path
    stays dependency-light" contract only holds if commands.py's own imports
    of source_cmd/config_edit are lazy. Run in a subprocess: other tests in
    this session may already have imported tomlkit, which would make an
    in-process sys.modules check unreliable. The subprocess cwd is pinned to
    tmp_path (not the repo root), since a repo-root contextlake.py shadows
    the installed package for plain `python` invocations."""
    import subprocess
    import sys

    fixture = str(FIXTURE)
    code = (
        "import sys\n"
        "assert 'tomlkit' not in sys.modules\n"
        "import contextlake.kb.commands\n"
        "assert 'tomlkit' not in sys.modules, 'tomlkit imported by module import'\n"
        "from contextlake.cli import main\n"
        "import tempfile, os\n"
        "d = tempfile.mkdtemp()\n"
        "cfg = os.path.join(d, 'kb.toml')\n"
        "open(cfg, 'w').write('[kb]\\nstore_dir = \"' + d + '/kb\"\\n')\n"
        "try:\n"
        "    main(['index', '--config', cfg, '--source', " + repr(fixture) + "])\n"
        "except SystemExit as e:\n"
        "    assert e.code == 0\n"
        "try:\n"
        "    main(['query', 'OrderService', '--config', cfg])\n"
        "except SystemExit as e:\n"
        "    assert e.code == 0\n"
        "assert 'tomlkit' not in sys.modules, 'tomlkit imported by index/query'\n"
        "print('OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_embed_unavailable_hint_is_actionable():
    """The embed no-op must tell the user how to turn semantic search on."""
    import importlib.util

    from contextlake.kb.commands import _embed_unavailable_hint
    from contextlake.kb.config import EmbeddingsCfg

    off = _embed_unavailable_hint(EmbeddingsCfg(enabled=False))
    assert "enabled = true" in off                       # the opt-in step is always named
    on_no_engine = _embed_unavailable_hint(EmbeddingsCfg(enabled=True))

    if importlib.util.find_spec("model2vec") is None:
        assert "kb-full" in off                          # tell them to install the embedder
        assert "kb-full" in on_no_engine or "Ollama" in on_no_engine
    else:
        assert "doctor" in on_no_engine                  # engine present -> point at diagnostics


def test_index_workspace_repos_filter(tmp_path):
    # --repos scopes a workspace index to matching repos (glob/substring)
    ws = tmp_path / "ws"
    for r in ("team/api", "team/web", "billing/core", "billing/reports"):
        (ws / r).mkdir(parents=True)
        (ws / r / "m.py").write_text("class X:\n    pass\n")
        (ws / r / ".git").mkdir()
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--workspace", str(ws),
                 "--repos", "billing/*,team/api"]) == 0
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    assert {r.id for r in store.list_repos()} == {"billing/core", "billing/reports", "team/api"}
    store.close()


def test_index_workspace_repos_filter_no_match_fails(tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "team/api").mkdir(parents=True)
    (ws / "team/api" / "m.py").write_text("class X:\n    pass\n")
    (ws / "team/api" / ".git").mkdir()
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--workspace", str(ws),
                 "--repos", "zzz-nope"]) == 1
    assert "matching --repos" in capsys.readouterr().out


def test_owners_unknown_repo_suggests_close_id(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    # indexing the fixture creates repo id 'demo/app'
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    # a prefix-stripped id ('app') should point at the stored 'demo/app'
    rc = _run(["owners", "app", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "demo/app" in out and "Did you mean" in out


def test_graph_unknown_repo_suggests_close_id(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    rc = _run(["graph", "--repo", "demo/ap", "--format", "json", "--config", str(cfg)])
    captured = capsys.readouterr()  # json format redirects logs to stderr
    assert rc == 1
    assert "demo/app" in (captured.out + captured.err)


def test_query_no_match_multiword_hints_semantic_search(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    # a multi-word natural-language query with no keyword hit gets a semantic hint
    assert _run(["query", "how does the loyalty flow work", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "No matches" in out
    assert "embed" in out and "semantic" in out.lower()


def test_query_no_match_singleword_no_hint(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    # a single-token lookup (a symbol) stays quiet: no semantic-hint noise
    assert _run(["query", "NoSuchSymbol", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "No matches" in out
    assert "semantic" not in out.lower()
